#!/usr/bin/env python3
"""期交所盤中／夜盤即時報價快照（TAIFEX MIS）→ futures_intraday_snapshot.

為什麼是這個資料源
------------------
決策規則需要「16:45 當下的**聯電個股期**報價」與「隔日 08:45 日盤開盤價」。三個候選源
只有這個能同時滿足即時性與涵蓋率：

* FinMind ``taiwan_futures_snapshot``：即時，但**不涵蓋 CCF**（回 0 筆；CDF/TXF 可以）
* FinMind ``TaiwanFuturesTick``：只有日盤 08:45-13:44，**無夜盤**
* FinMind ``TaiwanFuturesDaily``：有 ``trading_session`` 分場，但只有**整場 OHLC**，
  取不到 16:45 這種時點
* Fubon ``futopt.intraday``：可以，但 mini 有常駐 ``tmf-channel-poll`` worker 重用單一
  session，16:45 正好在其窗內（``in_tmf_trade_window`` >= 15:00），另建 session 可能
  踢掉它（最壞一輪對帳失敗、~20s 後自癒，但沒必要冒）
* **期交所 MIS（本支採用）**：純 HTTP、無 session、日夜盤都有

Symbol 後綴（2026-08-10 實測）
------------------------------
* ``CCFH6-F`` → **日盤**時段；夜盤期間會凍結在 13:44:59 的收盤值
* ``CCFH6-M`` → **盤後（夜盤）**時段即時報價

兩者都要抓：靠 ``CTime`` 判斷哪一個是「當下」的。

前月合約
--------
不寫死。列出當月起連續 3 個月的代碼（月碼 A=1月 … L=12月，年碼取西元末位），
一次查詢後以 ``CTotalVolume`` 最大者為前月——轉倉自動跟上。

  PYTHONPATH=src .venv/bin/python scripts/tools/sync_taifex_intraday_snapshot.py --dry-run
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402

TAIPEI = timezone(timedelta(hours=8))
MIS_URL = "https://mis.taifex.com.tw/futures/api/getQuoteDetail"
HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Content-Type": "application/json",
    "Referer": "https://mis.taifex.com.tw/futures/",
}
MONTH_CODE = "ABCDEFGHIJKL"          # A=1月 … L=12月
PRODUCTS = ("TXF", "CCF")            # 台指期 / 聯電個股期
SESSIONS = {"F": "day", "M": "night"}
# 2026-09-03 擴充：biglot 隔夜訊號的 45 檔個股期貨（08:45 開盤價／13:45 收盤價是把
# 「已驗證的隔夜訊號」推向可執行的唯一資料缺口：13:30-13:45 進場基差、08:45 出場時點）。
# 代碼動態讀 _live_calib.json 的 fut_code；檔案缺失即退回 PRODUCTS（fail-open，不擋原功能）。
# 只在日盤兩個時點抓、只抓 -F（個股期夜盤 17:25 才開，-M 在這兩個時點無意義）。
UNIVERSE_LABELS = ("08:45", "13:45")


def _universe_products() -> list[str]:
    import json
    try:
        from stock_db import DATA_DIR
        cal = json.loads((DATA_DIR / "cache" / "pit_universe_tick" / "_live_calib.json").read_text())
        codes = sorted({r["fut_code"] for r in cal["universe"] if r.get("fut_code")})
        # calib 內兩碼者（如 IX/CY）為省略尾碼 F 的寫法；MIS 商品碼一律三碼
        codes = [c if len(c) >= 3 else c + "F" for c in codes]
        return [c for c in codes if c not in PRODUCTS]
    except Exception as exc:  # noqa: BLE001
        print(f"universe calib 載入失敗（退回預設商品）: {exc!r}", file=sys.stderr)
        return []
# 08:46 日盤開（出場價）· 13:46 日盤收（分母）· 15:01 夜盤開 · 16:45 決策時點（進場價）
CAPTURE_LABELS = ("08:45", "13:45", "15:00", "16:45")
LABEL_TOLERANCE_MIN = 15

_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS futures_intraday_snapshot (
    tw_session_date TEXT NOT NULL,
    capture_label TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    product TEXT NOT NULL,
    contract TEXT NOT NULL,
    session TEXT NOT NULL,
    spot_id TEXT,
    last_price REAL,
    open_price REAL,
    high_price REAL,
    low_price REAL,
    ref_price REAL,
    total_volume REAL,
    quote_time TEXT,
    source TEXT NOT NULL DEFAULT 'taifex_mis',
    synced_at TEXT NOT NULL,
    PRIMARY KEY (tw_session_date, capture_label, product, session, source)
);
CREATE INDEX IF NOT EXISTS idx_futures_intraday_date
    ON futures_intraday_snapshot (tw_session_date DESC, capture_label, product);
"""


def _contract_codes(today: date, n_months: int = 3) -> list[str]:
    out = []
    y, m = today.year, today.month
    for _ in range(n_months):
        out.append(f"{MONTH_CODE[m - 1]}{y % 10}")
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return out


def _nearest_label(hm: str) -> str | None:
    now_min = int(hm[:2]) * 60 + int(hm[3:])
    passed = [x for x in CAPTURE_LABELS if x <= hm]
    if not passed:
        return None
    lab = passed[-1]
    lab_min = int(lab[:2]) * 60 + int(lab[3:])
    return lab if (now_min - lab_min) <= LABEL_TOLERANCE_MIN else None


def _fetch(symbol_ids: list[str]) -> list[dict]:
    """分批查詢（禮貌節流；單批失敗只丟該批，不擋整輪）。"""
    import time
    out: list[dict] = []
    for i in range(0, len(symbol_ids), 50):
        batch = symbol_ids[i:i + 50]
        try:
            r = requests.post(MIS_URL, json={"SymbolID": batch}, timeout=20, headers=HEADERS)
            r.raise_for_status()
            payload = r.json()
            if payload.get("RtCode") != "0":
                raise RuntimeError(f"MIS RtCode={payload.get('RtCode')} {payload.get('RtMsg')}")
            out += [q for q in (payload.get("RtData") or {}).get("QuoteList") or []
                    if q.get("SymbolID")]
        except Exception as exc:  # noqa: BLE001
            if i == 0:
                raise          # 第一批（含 TXF/CCF 核心）失敗仍視為 BLOCKER
            print(f"WARN: MIS 批次 {i//50+1} 失敗（{len(batch)} symbols）: {exc}", file=sys.stderr)
        if i + 50 < len(symbol_ids):
            time.sleep(0.35)
    return out


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _send_open_mail(rows: list[dict], *, preview: bool) -> None:
    """45 檔個股期貨 08:45 開盤信：期貨開盤價與隱含跳空（vs 現貨昨收）。"""
    import json
    from datetime import timedelta

    from stock_db import DATA_DIR

    now = datetime.now(tz=TAIPEI)
    cal = json.loads((DATA_DIR / "cache" / "pit_universe_tick" / "_live_calib.json").read_text())
    code2meta = {}
    for r in cal["universe"]:
        c = r.get("fut_code") or ""
        code2meta[c if len(c) >= 3 else c + "F"] = (r["sid"], r["name"])
    day_rows = [r for r in rows if r["session"] == "day" and r["product"] in code2meta]
    pc: dict[str, float] = {}
    try:
        import sqlite3
        con = sqlite3.connect(f"file:{DEFAULT_DB_PATH}?mode=ro", uri=True)
        sids = [code2meta[r["product"]][0] for r in day_rows]
        since = (now.date() - timedelta(days=12)).isoformat()
        q = ("SELECT stock_id, trade_date, close FROM stock_daily_bars "
             f"WHERE trade_date >= ? AND stock_id IN ({','.join('?' * len(sids))}) "
             "ORDER BY trade_date")
        for sid, d, c in con.execute(q, [since] + sids):
            if d < now.strftime("%Y-%m-%d") and c:
                pc[sid] = float(c)
        con.close()
    except Exception as exc:  # noqa: BLE001
        print(f"WARN: 昨收載入失敗，隱含跳空欄缺: {exc!r}", file=sys.stderr)
    recs = []
    for r in day_rows:
        sid, nm = code2meta[r["product"]]
        px = r.get("open_price") or r.get("last_price")
        ref = r.get("ref_price")
        recs.append((sid, nm, px,
                     (px / ref - 1) * 100 if (px and ref) else None,
                     (px / pc[sid] - 1) * 100 if (px and pc.get(sid)) else None,
                     r.get("total_volume") or 0))
    recs.sort(key=lambda x: (x[4] is None, -(x[4] or 0)))
    txf = next((r for r in rows if r["product"] == "TXF" and r["session"] == "day"), None)
    L = [f"【08:46 · 個股期貨開盤】{now:%Y-%m-%d %H:%M}　覆蓋 {sum(1 for x in recs if x[2])}"
         f"/{len(code2meta)} 檔",
         "隱含跳空＝期貨開盤 vs 現貨昨收；現貨 09:00 才開盤，此為提前 14 分鐘的預覽", ""]
    if txf and (txf.get("open_price") or txf.get("last_price")) and txf.get("ref_price"):
        tp = txf.get("open_price") or txf.get("last_price")
        L.append(f"TXF {txf['contract']} 開盤 {tp:g}（vs 昨結 {(tp / txf['ref_price'] - 1) * 100:+.2f}%）")
        L.append("")
    L.append(f"{'代號':<6}{'名稱':<10}{'期貨開盤':>9}{'vs昨結%':>8}{'隱含跳空%':>10}{'量':>7}")
    for sid, nm, px, chg, gap, vol in recs:
        L.append(f"{sid:<6}{nm:<10}{(f'{px:g}' if px else 'n/a'):>9}"
                 f"{(f'{chg:+.2f}' if chg is not None else 'n/a'):>8}"
                 f"{(f'{gap:+.2f}' if gap is not None else 'n/a'):>10}{vol:>7,.0f}")
    L += ["", "· 用途：昨日 13:30 名單的出場預覽（出場基準＝現貨 09:00 開盤競價）與今晨風向",
          "· 09:00 biglot worker 會再寄一封同源版本；關閉：.env RUN_FUTOPT_OPEN_MAIL=0"]
    body = chr(10).join(L)
    subj = f"個股期貨開盤 {now:%m/%d} 08:46"
    if preview:
        print("---- 開盤信預覽 ----")
        print(subj)
        print(body)
        return
    from notify_email import send_alert
    send_alert(subj, body)
    print("open-mail sent", flush=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="期交所盤中/夜盤報價快照")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--products", nargs="*", default=list(PRODUCTS))
    ap.add_argument("--label", default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--mail-preview", action="store_true", help="只印 08:45 開盤信不寄送（測試）")
    args = ap.parse_args(argv)

    now = datetime.now(tz=TAIPEI)
    label = args.label or _nearest_label(now.strftime("%H:%M"))
    if label is None:
        print(f"SKIP: {now:%H:%M} 不在 {CAPTURE_LABELS} 的 {LABEL_TOLERANCE_MIN} 分鐘容忍窗內",
              file=sys.stderr)
        return 0
    # 夜盤 00:00-05:00 仍屬前一日開出的那一場
    session_date = (now.date() - timedelta(days=1)) if now.hour < 6 else now.date()

    codes = _contract_codes(now.date())
    products = list(args.products)
    symbols = [f"{p}{c}-{s}" for p in products for c in codes for s in SESSIONS]
    if label in UNIVERSE_LABELS and args.products == list(PRODUCTS):   # 未手動指定商品時才擴充
        ext = _universe_products()
        products += ext
        symbols += [f"{p}{c}-F" for p in ext for c in codes]
        print(f"universe 擴充：+{len(ext)} 檔個股期（僅日盤）")
    try:
        quotes = _fetch(symbols)
    except Exception as exc:  # noqa: BLE001
        print(f"BLOCKER: MIS 查詢失敗 {exc}", file=sys.stderr)
        return 1

    rows: list[dict] = []
    for product in products:
        for suffix, sess_name in SESSIONS.items():
            cand = [q for q in quotes
                    if q["SymbolID"].startswith(product) and q["SymbolID"].endswith(f"-{suffix}")
                    and _f(q.get("CLastPrice")) is not None]
            if not cand:
                continue
            front = max(cand, key=lambda q: _f(q.get("CTotalVolume")) or 0.0)
            rows.append({
                "tw_session_date": session_date.isoformat(),
                "capture_label": label,
                "captured_at": now.isoformat(),
                "product": product,
                "contract": front["SymbolID"],
                "session": sess_name,
                "spot_id": front.get("SpotID") or None,
                "last_price": _f(front.get("CLastPrice")),
                "open_price": _f(front.get("COpenPrice")),
                "high_price": _f(front.get("CHighPrice")),
                "low_price": _f(front.get("CLowPrice")),
                "ref_price": _f(front.get("CRefPrice")),
                "total_volume": _f(front.get("CTotalVolume")),
                "quote_time": f"{front.get('CDate','')} {front.get('CTime','')}".strip(),
                "source": "taifex_mis",
            })

    if not rows:
        print("BLOCKER: 零筆報價", file=sys.stderr)
        return 1
    # 08:45 開盤信（2026-09-04 起，與 biglot worker 的 09:00 版同源；本封搶先於採集完成當下寄出）
    import os as _os
    if args.mail_preview or (label == "08:45" and not args.dry_run
                             and _os.environ.get("RUN_FUTOPT_OPEN_MAIL", "1") == "1"):
        try:
            _send_open_mail(rows, preview=args.mail_preview)
        except Exception as exc:  # noqa: BLE001
            print(f"WARN: 開盤信失敗（不擋快照）: {exc!r}", file=sys.stderr)
    for r in rows:
        print(f"  {r['tw_session_date']} {r['capture_label']} {r['contract']:10s} "
              f"{r['session']:5s} last={r['last_price']} vol={r['total_volume']} "
              f"quote_time={r['quote_time']}")
    if args.dry_run:
        print(f"dry-run: {len(rows)} 筆未寫入")
        return 0

    conn = connect(args.db)
    try:
        # 表由本支自建、不 bump SCHEMA_VERSION——bump 會讓下一個 connect() 的行程對 40GB
        # 生產 DB 跑完整 executescript + migration，常駐 TMF worker 在跑時風險不必要。
        conn.executescript(_TABLE_DDL)
        synced_at = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
        for r in rows:
            r["synced_at"] = synced_at
        conn.executemany(
            """
            INSERT INTO futures_intraday_snapshot (
                tw_session_date, capture_label, captured_at, product, contract, session,
                spot_id, last_price, open_price, high_price, low_price, ref_price,
                total_volume, quote_time, source, synced_at
            ) VALUES (
                :tw_session_date, :capture_label, :captured_at, :product, :contract, :session,
                :spot_id, :last_price, :open_price, :high_price, :low_price, :ref_price,
                :total_volume, :quote_time, :source, :synced_at
            )
            ON CONFLICT(tw_session_date, capture_label, product, session, source) DO UPDATE SET
                captured_at=excluded.captured_at, contract=excluded.contract,
                spot_id=excluded.spot_id, last_price=excluded.last_price,
                open_price=excluded.open_price, high_price=excluded.high_price,
                low_price=excluded.low_price, ref_price=excluded.ref_price,
                total_volume=excluded.total_volume, quote_time=excluded.quote_time,
                synced_at=excluded.synced_at
            """,
            rows,
        )
        conn.commit()
        print(f"Wrote {len(rows)} rows · label={label} · session={session_date}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
