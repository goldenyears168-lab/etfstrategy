#!/usr/bin/env python3
"""dayflip v2 shadow screen — 「漲太多 × 隔日沖席位」放空候選（observe only · 不下單）.

為什麼是 shadow 而不是直接改 v1：
  `reports/research/branch-footprint-screen/dayflip_gapup_short/FROZEN_SPEC_V1.json` 白紙黑字寫
  「凍結後禁止再調任何門檻。任何門檻變更＝新版本 v2，且必須重新開始前推期。」
  而 v1 自己的前推期只跑了 5 個訊號日（pass_criteria 要求 40）。同時開 v2 會讓兩條線都拿不到
  乾淨的前推期。所以本腳本**只產出候選清單並記錄結果，不下單、不碰 v1、不改任何 config**。

與 v1 的差別（這是 v2 的假設，尚未驗證）：
  v1 用**絕對** gap 門檻（T+1 期貨開盤 / T0 期貨收盤 − 1 >= 0.06）。
  v2 改用**相對延伸度**——當日全市場的橫斷面分位，而不是固定百分比。理由：絕對門檻在多頭
  尾聲會全市場觸發、在盤整期一筆都不觸發，訊號密度本身隨市場狀態漂移。

四層閘門：
  L0 可交易性 —— 必須在個股期貨宇宙內（否則放空不了）＋ 現股 ADV20 下限
  L1 延伸度   —— (close/MA20−1) 與近 5 日漲幅，取當日全市場**分位**
  L2 隔日沖席位 —— 高沖席（FROZEN_SPEC flip >= 0.4）近 N 日淨買金額，
                  **扣掉建倉腿**（沿用 v1 的 accumulation_exclusion：該席在該股 60 日
                  net_ratio >= 0.30 視為建倉、不計入倒貨壓力）
  L3 總經脈絡 —— 外資台指期 net OI 的 z60 ＋ NQ 隔夜。**目前只記錄不過濾**：
                  z60 是 T 日盤後 EOD、NQ 隔夜是 T+1 凌晨，兩者不同步，
                  在累積足夠樣本前不該拿來當 gate。

⚠️ 預先宣告的通過門檻（G1 預註記 · 在累積任何樣本前寫下，不得事後調整）：
  min_signal_days: 40
  day_median_pct:  > 0
  day_win_rate_pct: >= 60
  fail_action: 整條 v2 降級為 rejected，不調參搶救

  PYTHONPATH=src .venv/bin/python scripts/research/dayflip_v2_shadow_screen.py
  PYTHONPATH=src .venv/bin/python scripts/research/dayflip_v2_shadow_screen.py --asof 2026-08-17
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from stock_db import DATA_DIR, DEFAULT_DB_PATH  # noqa: E402

SOURCE = "finmind"
SPEC = ROOT / "reports/research/branch-footprint-screen/dayflip_gapup_short/FROZEN_SPEC_V1.json"
FUT_UNIVERSE = ROOT / "reports/research/branch-footprint-screen/dayflip_gapup_short/stock_futures_universe.json"
OUT_DIR = ROOT / "reports/research/branch-footprint-screen/dayflip_v2_shadow"

# --- 預先宣告，不得事後調整 ---
PASS_CRITERIA = {"min_signal_days": 40, "day_median_pct": "> 0", "day_win_rate_pct": ">= 60"}
HIGH_FLIP_MIN = 0.40          # 沿用 v1 凍結值
ACCUM_NET_RATIO = 0.30        # 沿用 v1 accumulation_exclusion
ACCUM_MIN_WINDOW_BUY = 1e8    # 沿用 v1 min_window_buy_ntd
SEAT_WINDOW_DAYS = 5
EXT_PCTL_MIN = 95.0           # 延伸度取當日全市場前 5%
ADV20_MIN_NTD = 3e8
TOP_N = 10


def connect_ro(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def load_seats() -> list[str]:
    vals = json.loads(SPEC.read_text(encoding="utf-8"))["seat_flip_table_frozen"]["values"]
    return [k for k, v in vals.items() if float(v) >= HIGH_FLIP_MIN]


def load_futures_universe() -> set[str]:
    return set(json.loads(FUT_UNIVERSE.read_text(encoding="utf-8"))["map"].keys())


def trade_dates(conn, asof: str | None, n: int) -> list[str]:
    sql = "SELECT DISTINCT trade_date FROM stock_daily_bars WHERE source=?"
    args: list = [SOURCE]
    if asof:
        sql += " AND trade_date<=?"
        args.append(asof)
    sql += " ORDER BY trade_date DESC LIMIT ?"
    args.append(n)
    return [r[0] for r in conn.execute(sql, args)]


def extension_panel(conn, days: list[str]) -> dict[str, dict]:
    """每檔的 close / MA20 / 近5日漲幅 / ADV20（只用 <= asof 的資料，PIT 乾淨）。"""
    asof, d5, d20 = days[0], days[5], days[19]
    rows = conn.execute(
        """
        SELECT stock_id,
               AVG(close) AS ma20,
               AVG(volume*close) AS adv20,
               MAX(CASE WHEN trade_date=? THEN close END) AS c_now,
               MAX(CASE WHEN trade_date=? THEN close END) AS c_5d,
               MAX(close) AS hi20
        FROM stock_daily_bars
        WHERE source=? AND trade_date BETWEEN ? AND ?
          AND length(stock_id)=4 AND stock_id GLOB '[0-9][0-9][0-9][0-9]'
          AND stock_id NOT GLOB '00*'
        GROUP BY stock_id
        HAVING COUNT(*)>=20 AND c_now>0 AND c_5d>0
        """,
        (asof, d5, SOURCE, d20, asof),
    ).fetchall()
    out = {}
    for r in rows:
        out[r["stock_id"]] = {
            "close": float(r["c_now"]),
            "r5_pct": (float(r["c_now"]) / float(r["c_5d"]) - 1) * 100,
            "vs_ma20_pct": (float(r["c_now"]) / float(r["ma20"]) - 1) * 100,
            "from_hi20_pct": (float(r["c_now"]) / float(r["hi20"]) - 1) * 100,
            "adv20_ntd": float(r["adv20"] or 0),
        }
    return out


def seat_pressure(conn, seats: list[str], days: list[str], asof: str) -> dict[str, dict]:
    """高沖席近 N 日淨買金額，扣掉建倉腿（該席在該股 60 日 net_ratio >= 0.30）。"""
    win = days[:SEAT_WINDOW_DAYS]
    ph_s, ph_d = ",".join("?" * len(seats)), ",".join("?" * len(win))
    raw = conn.execute(
        f"""
        SELECT b.securities_trader_id AS sid, b.stock_id,
               SUM(b.buy*p.close) AS buy_ntd, SUM(b.sell*p.close) AS sell_ntd
        FROM stock_broker_branch_daily b
        JOIN stock_daily_bars p ON p.stock_id=b.stock_id AND p.trade_date=b.trade_date AND p.source=?
        WHERE b.source=? AND b.securities_trader_id IN ({ph_s}) AND b.trade_date IN ({ph_d})
          AND length(b.stock_id)=4 AND b.stock_id GLOB '[0-9][0-9][0-9][0-9]'
          AND b.stock_id NOT GLOB '00*'
        GROUP BY 1,2
        """,
        (SOURCE, SOURCE, *seats, *win),
    ).fetchall()
    if not raw:
        return {}
    d60 = trade_dates(conn, asof, 60)
    since = d60[-1]
    accum = {
        (r["sid"], r["stock_id"])
        for r in conn.execute(
            f"""
            SELECT b.securities_trader_id AS sid, b.stock_id,
                   SUM(b.buy*p.close) AS bw,
                   (SUM(b.buy*p.close)-SUM(b.sell*p.close))/NULLIF(SUM(b.buy*p.close),0) AS nr
            FROM stock_broker_branch_daily b
            JOIN stock_daily_bars p ON p.stock_id=b.stock_id AND p.trade_date=b.trade_date AND p.source=?
            WHERE b.source=? AND b.securities_trader_id IN ({ph_s})
              AND b.trade_date BETWEEN ? AND ?
            GROUP BY 1,2 HAVING bw>=? AND nr>=?
            """,
            (SOURCE, SOURCE, *seats, since, asof, ACCUM_MIN_WINDOW_BUY, ACCUM_NET_RATIO),
        )
    }
    out: dict[str, dict] = {}
    for r in raw:
        key = (r["sid"], r["stock_id"])
        e = out.setdefault(r["stock_id"], {"buy": 0.0, "sell": 0.0, "seats": 0, "accum_excluded": 0})
        if key in accum:                      # 建倉腿：不是隔日沖倒貨壓力
            e["accum_excluded"] += 1
            continue
        e["buy"] += float(r["buy_ntd"] or 0)
        e["sell"] += float(r["sell_ntd"] or 0)
        e["seats"] += 1
    return out


def macro_context(conn, asof: str) -> dict:
    oi = [float(r[0]) for r in conn.execute(
        "SELECT net_oi_vol FROM futures_institutional_daily WHERE futures_id='TX' "
        "AND inst_name LIKE '%外資%' AND trade_date<=? ORDER BY trade_date DESC LIMIT 60", (asof,))]
    z = None
    if len(oi) >= 30:
        import statistics
        sd = statistics.pstdev(oi)
        z = (oi[0] - statistics.mean(oi)) / sd if sd else None
    nq = conn.execute(
        "SELECT tw_session_date, nq_overnight_pct, es_overnight_pct FROM us_futures_overnight_snapshot "
        "WHERE tw_session_date<=? ORDER BY tw_session_date DESC, captured_at DESC LIMIT 1", (asof,)).fetchone()
    return {
        "foreign_tx_net_oi": oi[0] if oi else None,
        "foreign_tx_net_oi_z60": round(z, 3) if z is not None else None,
        "foreign_tx_net_oi_delta_5d": (oi[0] - oi[4]) if len(oi) > 4 else None,
        "nq_overnight_pct": nq["nq_overnight_pct"] if nq else None,
        "es_overnight_pct": nq["es_overnight_pct"] if nq else None,
        "nq_asof": nq["tw_session_date"] if nq else None,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=Path(DEFAULT_DB_PATH))
    ap.add_argument("--asof", default="")
    ap.add_argument("--top", type=int, default=TOP_N)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    conn = connect_ro(args.db)
    try:
        days = trade_dates(conn, args.asof or None, 20)
        if len(days) < 20:
            print("交易日不足 20 日，無法計算"); return 1
        asof = days[0]
        seats = load_seats()
        fut = load_futures_universe()
        ext = extension_panel(conn, days)
        seatp = seat_pressure(conn, seats, days, asof)
        macro = macro_context(conn, asof)

        # L1 延伸度分位（當日全市場橫斷面，不是絕對門檻）
        pool = [v["vs_ma20_pct"] for v in ext.values()]
        pool.sort()
        def pctl(x: float) -> float:
            import bisect
            return bisect.bisect_left(pool, x) / len(pool) * 100

        rows = []
        for sid, e in ext.items():
            if sid not in fut:                       # L0
                continue
            if e["adv20_ntd"] < ADV20_MIN_NTD:
                continue
            p = pctl(e["vs_ma20_pct"])
            if p < EXT_PCTL_MIN:                     # L1
                continue
            sp = seatp.get(sid)
            if not sp:                               # L2
                continue
            net = sp["buy"] - sp["sell"]
            rows.append({
                "stock_id": sid, "close": round(e["close"], 2),
                "r5_pct": round(e["r5_pct"], 2),
                "vs_ma20_pct": round(e["vs_ma20_pct"], 2),
                "ext_pctl": round(p, 1),
                "from_hi20_pct": round(e["from_hi20_pct"], 2),
                "adv20_yi": round(e["adv20_ntd"] / 1e8, 1),
                "seat_buy_yi": round(sp["buy"] / 1e8, 2),
                "seat_sell_yi": round(sp["sell"] / 1e8, 2),
                "seat_net_yi": round(net / 1e8, 2),
                "n_seats": sp["seats"], "accum_excluded": sp["accum_excluded"],
            })
        rows.sort(key=lambda r: (-r["seat_net_yi"], -r["vs_ma20_pct"]))
        rows = rows[: args.top]

        payload = {
            "spec": "dayflip-v2-shadow", "status": "observe_only", "asof": asof,
            "pass_criteria_pre_declared": PASS_CRITERIA,
            "gates": {"high_flip_min": HIGH_FLIP_MIN, "ext_pctl_min": EXT_PCTL_MIN,
                      "adv20_min_ntd": ADV20_MIN_NTD, "seat_window_days": SEAT_WINDOW_DAYS,
                      "accum_net_ratio": ACCUM_NET_RATIO},
            "n_high_flip_seats": len(seats), "n_futures_universe": len(fut),
            "macro": macro, "candidates": rows,
        }
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        (OUT_DIR / f"shadow_{asof.replace('-','')}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2)); return 0

        print(f"dayflip v2 shadow · asof={asof} · observe only（不下單）")
        print(f"高沖席 {len(seats)} 席 · 期貨宇宙 {len(fut)} 檔 · 延伸度取全市場前 {100-EXT_PCTL_MIN:.0f}%")
        m = macro
        print(f"總經：外資TX net_oi={m['foreign_tx_net_oi']:,.0f} z60={m['foreign_tx_net_oi_z60']} "
              f"5日變化={m['foreign_tx_net_oi_delta_5d']:+,.0f}｜NQ隔夜={m['nq_overnight_pct']}% ({m['nq_asof']})")
        if not rows:
            print("\n無候選（今日無標的同時通過 L0/L1/L2）")
        else:
            print(f"\n{'股號':<6}{'收盤':>9}{'近5日%':>8}{'vs20MA':>8}{'分位':>6}"
                  f"{'ADV(億)':>9}{'席買':>7}{'席賣':>7}{'席淨':>7}{'席數':>5}{'剔建倉':>7}")
            for r in rows:
                print(f"{r['stock_id']:<6}{r['close']:>9.1f}{r['r5_pct']:>8.1f}{r['vs_ma20_pct']:>8.1f}"
                      f"{r['ext_pctl']:>6.0f}{r['adv20_yi']:>9.1f}{r['seat_buy_yi']:>7.2f}"
                      f"{r['seat_sell_yi']:>7.2f}{r['seat_net_yi']:>7.2f}{r['n_seats']:>5}{r['accum_excluded']:>7}")
        print(f"\n報告：{OUT_DIR / f'shadow_{asof.replace(chr(45),chr(45))}.json'}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
