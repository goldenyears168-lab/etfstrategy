#!/usr/bin/env python3
"""聯電夜盤決策規則 — 前推紀錄器（research only · 只寫 ledger，永不下單）.

為什麼需要它
------------
規則本身仍是**假說**，不是策略。已知三個未解限制：
  1. 單股單路徑，無跨股票驗證
  2. 逐月符號翻轉 —— 2025-11~2026-03 五個月全負、2026-04~08 五個月全正
  3. 勝率僅 43-46%，靠尾部；最近 22 天最大單筆佔總額 43%

更根本的是：先前所有回測都用 **2303 現股** 當 CCF 的代理（進場用現股 13:30 收、
出場用現股 09:00 開），而失真點正好落在真正的兩個決策時點上 ——
CCF 夜盤 15:00 就開了，16:45 時已走 1h45m；CCF 日盤 08:45 開，比現股早 15 分鐘。
**那些數字在正確標的上不成立。**

而 16:45 與 08:45 這兩個時點事後補不回來（FinMind tick 只有日盤、daily 只有整場
OHLC），所以唯一路徑是前推累積。這支就是做這件事：每天記錄決策與結果，
累積 >= 40 個訊號日之前，不對這條規則做任何論斷。

決策規則（三因子等權，滾動 120 日百分位）
-----------------------------------------
  score = mean(pct(2303 當日漲跌), pct(CCF 夜盤 13:45收→16:45), pct(NQ 隔夜% @13:30))
  score >= 0.67 → 做多 ; <= 0.33 → 放空 ; 其餘不做

  進場：T 日 16:45 CCF 夜盤最後成交價（futures_intraday_snapshot label=16:45 session=night）
  出場：T+1 08:45 CCF 日盤開盤價（label=08:45 session=day）

  記帳成本：來回 2 tick。CCF tick 隨標的股價階梯變動（<100 元 0.1 / >=100 元 0.5），
  由當時 last_price 推導，不寫死。

用法
----
  # 收盤後記錄今日決策（16:46 之後）
  PYTHONPATH=src .venv/bin/python scripts/research/umc_night_forward_recorder.py --record

  # 隔日 08:46 之後結算尚未平倉的紀錄
  PYTHONPATH=src .venv/bin/python scripts/research/umc_night_forward_recorder.py --settle

  # 兩者都做（launchd 用；順序為先結算舊的、再記錄新的）
  PYTHONPATH=src .venv/bin/python scripts/research/umc_night_forward_recorder.py --settle --record
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402
from stock_db.connection import connect_ro  # noqa: E402

TAIPEI = timezone(timedelta(hours=8))
STOCK = "2303"
PRODUCT = "CCF"
LOOKBACK = 120           # 滾動百分位視窗
MIN_HIST = 60            # 少於這個天數不出手（分布還沒穩）
LONG_Q, SHORT_Q = 0.67, 0.33
CONTRACT_SHARES = 2000

_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS umc_night_forward_ledger (
    signal_date TEXT PRIMARY KEY,
    recorded_at TEXT NOT NULL,
    r0_pct REAL, ccf_night_pct REAL, nq_pct REAL,
    r0_rank REAL, ccf_night_rank REAL, nq_rank REAL,
    score REAL,
    position INTEGER NOT NULL,
    entry_price REAL, entry_contract TEXT,
    spot_close REAL,
    tick_size REAL, cost_pct REAL,
    exit_date TEXT, exit_price REAL, exit_contract TEXT,
    gross_pct REAL, net_pct REAL, net_ntd REAL,
    settled_at TEXT,
    note TEXT
);
"""


def _tick_size(price: float) -> float:
    """股票期貨最小升降單位（依標的股價階梯）。"""
    if price < 10:
        return 0.01
    if price < 50:
        return 0.05
    if price < 100:
        return 0.1
    if price < 500:
        return 0.5
    if price < 1000:
        return 1.0
    return 5.0


def _panel(conn) -> pd.DataFrame:
    """三因子面板。CCF 夜盤走勢只能從 futures_intraday_snapshot 取（無歷史回補管道）。"""
    px = pd.read_sql(
        f"select trade_date, close from stock_daily_bars where stock_id='{STOCK}' "
        "and source='finmind' and close>0 order by trade_date", conn
    ).drop_duplicates("trade_date").set_index("trade_date")
    px["r0"] = px.close.pct_change() * 100

    snap = pd.read_sql(
        "select tw_session_date d, capture_label, session, last_price, contract "
        f"from futures_intraday_snapshot where product='{PRODUCT}'", conn)
    night = snap[(snap.capture_label == "16:45") & (snap.session == "night")].set_index("d")
    dayc = snap[(snap.capture_label == "13:45") & (snap.session == "day")].set_index("d")
    ccf = pd.DataFrame({"ccf_1645": night.last_price, "ccf_contract": night.contract,
                        "ccf_1345": dayc.last_price})
    ccf["ccf_night"] = (ccf.ccf_1645 / ccf.ccf_1345 - 1) * 100

    nq = pd.read_sql(
        "select tw_session_date d, nq_overnight_pct from us_futures_overnight_snapshot "
        "where capture_label='13:30'", conn).drop_duplicates("d").set_index("d").nq_overnight_pct

    p = px.join(ccf).assign(nq=nq)
    return p


def _rank(series: pd.Series, upto: str) -> float | None:
    """PIT 百分位：只用 upto 之前（不含）的最近 LOOKBACK 個觀測。"""
    hist = series.loc[:upto].dropna()
    if len(hist) < MIN_HIST + 1:
        return None
    cur = hist.iloc[-1]
    window = hist.iloc[max(0, len(hist) - 1 - LOOKBACK):-1]
    return float((window < cur).mean())


def record(conn, day: str | None) -> int:
    p = _panel(conn)
    day = day or max(d for d in p.index if pd.notna(p.at[d, "ccf_1645"]))
    row = p.loc[day]
    missing = [k for k in ("r0", "ccf_night", "nq") if pd.isna(row.get(k))]
    if missing:
        print(f"SKIP {day}: 缺輸入 {missing}（排程尚未累積足夠天數屬正常）")
        return 0
    ranks = {k: _rank(p[k], day) for k in ("r0", "ccf_night", "nq")}
    if any(v is None for v in ranks.values()):
        print(f"SKIP {day}: 滾動視窗不足 {MIN_HIST} 天 —— {ranks}")
        return 0
    score = float(np.mean(list(ranks.values())))
    pos = 1 if score >= LONG_Q else (-1 if score <= SHORT_Q else 0)
    entry = float(row.ccf_1645)
    tick = _tick_size(entry)
    cost = 2 * tick / entry * 100 + 0.02          # 來回 2 tick + 稅費概估
    rec = dict(signal_date=day, recorded_at=datetime.now(tz=TAIPEI).isoformat(),
               r0_pct=float(row.r0), ccf_night_pct=float(row.ccf_night), nq_pct=float(row.nq),
               r0_rank=ranks["r0"], ccf_night_rank=ranks["ccf_night"], nq_rank=ranks["nq"],
               score=score, position=pos, entry_price=entry,
               entry_contract=str(row.ccf_contract), spot_close=float(row.close),
               tick_size=tick, cost_pct=cost, note=None)
    conn.executescript(_TABLE_DDL)
    cols = ",".join(rec)
    conn.execute(f"INSERT OR REPLACE INTO umc_night_forward_ledger ({cols}) "
                 f"VALUES ({','.join(':' + k for k in rec)})", rec)
    conn.commit()
    act = {1: "做多", -1: "放空", 0: "不做"}[pos]
    print(f"{day} score={score:.3f} → {act}　"
          f"r0={row.r0:+.2f}%(p{ranks['r0']*100:.0f}) "
          f"夜盤={row.ccf_night:+.2f}%(p{ranks['ccf_night']*100:.0f}) "
          f"NQ={row.nq:+.2f}%(p{ranks['nq']*100:.0f})")
    print(f"  進場 {rec['entry_contract']} @ {entry}　tick={tick} 來回成本 {cost:.2f}%")
    return 0


def settle(conn) -> int:
    conn.executescript(_TABLE_DDL)
    open_rows = pd.read_sql(
        "select * from umc_night_forward_ledger where settled_at is null order by signal_date", conn)
    if open_rows.empty:
        print("無待結算紀錄")
        return 0
    snap = pd.read_sql(
        "select tw_session_date d, last_price, contract from futures_intraday_snapshot "
        f"where product='{PRODUCT}' and capture_label='08:45' and session='day'", conn
    ).drop_duplicates("d").set_index("d")
    days = sorted(snap.index)
    n = 0
    for _, r in open_rows.iterrows():
        nxt = [d for d in days if d > r.signal_date]
        if not nxt:
            continue
        ex_d = nxt[0]
        ex_p = float(snap.at[ex_d, "last_price"])
        gross = (ex_p / r.entry_price - 1) * 100 * (r.position or 0)
        net = gross - (r.cost_pct if r.position else 0.0)
        ntd = net / 100 * r.entry_price * CONTRACT_SHARES if r.position else 0.0
        conn.execute(
            "UPDATE umc_night_forward_ledger SET exit_date=?, exit_price=?, exit_contract=?, "
            "gross_pct=?, net_pct=?, net_ntd=?, settled_at=? WHERE signal_date=?",
            (ex_d, ex_p, str(snap.at[ex_d, "contract"]), gross, net, ntd,
             datetime.now(tz=TAIPEI).isoformat(), r.signal_date))
        act = {1: "多", -1: "空", 0: "—"}[int(r.position)]
        print(f"  結算 {r.signal_date} {act} {r.entry_price} → {ex_p}（{ex_d}）"
              f" 毛 {gross:+.2f}% 淨 {net:+.2f}% = NT${ntd:,.0f}/口")
        n += 1
    conn.commit()
    if n:
        d = pd.read_sql("select * from umc_night_forward_ledger where settled_at is not null", conn)
        a = d[d.position != 0]
        print(f"\n累計：已結算 {len(d)} 天　出手 {len(a)} 次"
              + (f"　淨累計 {a.net_pct.sum():+.2f}%　勝率 {(a.net_pct > 0).mean() * 100:.0f}%"
                 if len(a) else ""))
        print(f"⚠️ 樣本 {len(a)}/40 —— 未達 40 個訊號日之前不對這條規則做任何論斷")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="聯電夜盤決策前推紀錄器（不下單）")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--record", action="store_true")
    ap.add_argument("--settle", action="store_true")
    ap.add_argument("--date", default=None)
    ap.add_argument("--report", action="store_true", help="只印 ledger 現況")
    args = ap.parse_args(argv)

    if args.report:
        conn = connect_ro(args.db)
        try:
            d = pd.read_sql("select * from umc_night_forward_ledger order by signal_date", conn)
        except Exception:
            print("ledger 尚未建立")
            return 0
        finally:
            conn.close()
        print(d.to_string(index=False) if len(d) else "ledger 為空")
        return 0

    if not (args.record or args.settle):
        ap.error("需指定 --record / --settle / --report")
    conn = connect(args.db)
    try:
        rc = 0
        if args.settle:
            rc |= settle(conn)
        if args.record:
            rc |= record(conn, args.date)
    finally:
        conn.close()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
