#!/usr/bin/env python3
"""dayflip-short post-dump 做多——限制「新進場速率」分散時間集中度，取代資金規則.

背景：前三輪資金/保證金相關嘗試（被動強平、主動限倉/留緩衝、每部位30萬保證金
上限配不同總資金）全部沒能把最大回檔壓到15%警戒區以下——根本原因是這批訊號
本來就會在同幾個「壞日子」集中出現（分點T0買超是相關性事件，不是隨機分散），
不管怎麼切資金規則，同一批相關性虧損打過來時佔資金的%就是壓不下去。

這輪換角度：不是限制「資金怎麼分配給已經決定要接的訊號」，是限制「同一段時間
最多接幾筆新訊號」——主動把訊號叢集本身打散，而不是被動地在虧損發生後應對。
規則：追蹤最近 ROLLING_DAYS 個交易日內已經新開的部位數，超過上限就把當天多出來
的候選訊號（照 n_seats 由大到小排序，只留排前面的）直接跳過不進場，即使保證金
理論上還夠。

沿用 1口/部位（不是上一輪的30萬保證金/部位，那個框架本身放大了單部位波動，這裡
先回到乾淨的1口基準，比較容易看出「限速率」本身的效果，不跟「放大部位」的效果
混在一起）、TX-real滾動相對弱勢訊號(15分/0.3%/10分)、移動停利5%/最長10日。

PYTHONPATH=src .venv/bin/python scripts/research/dayflip_short_entry_rate_limit_sweep.py
"""

from __future__ import annotations

import csv
import json
import os
import sqlite3
from pathlib import Path

import numpy as np

import stock_db
from stock_db.kbar import load_kbar_day_bars
from trial_registry import append_trial

ROOT = Path(__file__).resolve().parents[2]
TRADES_CSV = ROOT / "reports/research/branch-footprint-screen/dayflip_gapup_short/all_trades.csv"
FUT_CACHE_PATH = ROOT / "reports/research/branch-footprint-screen/dayflip_gapup_short/futures_daily_cache.json"
DATA_DIR = Path(os.environ.get("GOLDENSTOCKS_DATA_DIR", str(Path.home() / "goldenstocks-data")))
TX_BARS_DB = DATA_DIR / "cache" / "tmf_channel" / "bars.sqlite"
TX_SOURCE = "tx_1m_tick_built_582d"

ROUND_TRIP_COST_PCT = 0.05
TRAIL_PCT = 5.0
MAX_HOLD_DAYS = 10
MARGIN_RATE = 0.135
LOT_SHARES = 2000
ROLLING_WINDOW_MIN = 15
CONFIRM_MINUTES = 10
LAG_THRESHOLD_PCT = 0.3
MARGIN_CALL_DANGER_ZONE_PCT = 15.0

CAPITAL_SCENARIOS_NTD = (1_000_000, 2_000_000, 3_000_000)
# (rolling_days, max_new_entries_in_window)；None門檻=不限速率當基準
RATE_LIMITS = (
    None,
    (5, 3),
    (5, 5),
    (10, 5),
    (10, 8),
    (20, 10),
    (20, 15),
)


def load_trades() -> list[dict]:
    with TRADES_CSV.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_stock_minute_closes(con: sqlite3.Connection, stock_id: str, t01: str) -> dict[str, float]:
    raw = load_kbar_day_bars(con, stock_id, t01)
    return {
        b.minute[:5]: b.close
        for b in raw
        if "09:00" <= b.minute[:5] <= "13:30" and b.close and b.close > 0
    }


def load_tx_minute_closes(tx_con: sqlite3.Connection, t01: str) -> dict[str, float]:
    rows = tx_con.execute(
        "SELECT t, c FROM bars WHERE source=? AND day=? AND sess='day'", (TX_SOURCE, t01)
    ).fetchall()
    return {str(t)[:5]: float(c) for t, c in rows if c and float(c) > 0}


def find_rolling_dip_signal(stock_closes: dict, bench_closes: dict) -> tuple[str, float] | None:
    minutes = sorted(set(stock_closes) & set(bench_closes))
    if len(minutes) < 50:
        return None
    rolling_lag = {}
    for i, m in enumerate(minutes):
        if i < ROLLING_WINDOW_MIN:
            continue
        m0 = minutes[i - ROLLING_WINDOW_MIN]
        stock_ret = (stock_closes[m] / stock_closes[m0] - 1) * 100
        bench_ret = (bench_closes[m] / bench_closes[m0] - 1) * 100
        rolling_lag[m] = stock_ret - bench_ret
    lag_minutes = sorted(rolling_lag)
    if not lag_minutes:
        return None
    worst_idx, worst_val = None, 0.0
    for i, m in enumerate(lag_minutes):
        if rolling_lag[m] < -LAG_THRESHOLD_PCT and rolling_lag[m] < worst_val:
            worst_val = rolling_lag[m]
            worst_idx = i
    if worst_idx is None:
        return None
    worst_minute = lag_minutes[worst_idx]
    for i in range(worst_idx + 1, len(lag_minutes)):
        m = lag_minutes[i]
        if (i - worst_idx) >= CONFIRM_MINUTES and rolling_lag[m] > rolling_lag[worst_minute] * 0.5:
            return m, stock_closes[m]
    return None


def _t01_stock_close(con: sqlite3.Connection, stock_id: str, trade_date: str) -> float | None:
    row = con.execute(
        "SELECT close FROM stock_daily_bars WHERE stock_id=? AND trade_date=? AND source='finmind' AND close>0",
        (stock_id, trade_date),
    ).fetchone()
    return float(row[0]) if row else None


def estimate_margin_ntd(price: float) -> float:
    return price * LOT_SHARES * MARGIN_RATE


def build_calendar(con: sqlite3.Connection, start: str, end: str) -> list[str]:
    rows = con.execute(
        "SELECT DISTINCT trade_date FROM stock_daily_bars "
        "WHERE stock_id='0050' AND source='finmind' AND trade_date BETWEEN ? AND ? ORDER BY trade_date",
        (start, end),
    ).fetchall()
    return [str(r[0]) for r in rows]


def run_simulation(
    signals: list[dict], fut_cache: dict, calendar: list[str], total_capital: float,
    *, rate_limit: tuple[int, int] | None = None,
) -> dict:
    """rate_limit=(rolling_days, max_new_entries)：過去rolling_days個交易日內
    新開部位數超過max_new_entries，當天多出來的候選(照n_seats排序)直接跳過。"""
    signals_by_date: dict[str, list[dict]] = {}
    for s in signals:
        signals_by_date.setdefault(s["trade_date"], []).append(s)
    for d in signals_by_date:
        signals_by_date[d].sort(key=lambda s: -s["n_seats"])

    open_positions: list[dict] = []
    entry_day_log: list[int] = []  # day_idx of every new entry (for rate-limit window lookup)
    realized_pnl = 0.0
    skipped_for_capital = 0
    skipped_for_rate = 0
    taken = 0
    nav_series = []
    max_concurrent_seen = 0

    for day_idx, day in enumerate(calendar):
        margin_used = sum(p["margin"] for p in open_positions)
        available_margin = total_capital - margin_used

        still_open = []
        for p in open_positions:
            m = fut_cache.get(p["stock"]) or {}
            px = m.get(day)
            if px is None:
                still_open.append(p)
                continue
            close = float(px[1])
            if close <= 0:
                still_open.append(p)
                continue
            p["peak"] = max(p["peak"], close)
            pullback = (p["peak"] - close) / p["peak"] * 100
            hold_days = day_idx - p["entry_day_idx"]
            if pullback >= TRAIL_PCT or hold_days >= MAX_HOLD_DAYS:
                raw_ret = (close / p["entry_price"] - 1) * 100
                net_ret = raw_ret - ROUND_TRIP_COST_PCT
                realized_pnl += p["margin"] / MARGIN_RATE * (net_ret / 100)
                available_margin += p["margin"]
            else:
                still_open.append(p)
        open_positions = still_open

        if rate_limit is not None:
            rolling_days, max_new = rate_limit
            window_start = day_idx - rolling_days
            entries_in_window = sum(1 for d in entry_day_log if d > window_start)
        else:
            entries_in_window = 0
            max_new = None

        for s in signals_by_date.get(day, []):
            if rate_limit is not None and entries_in_window >= max_new:
                skipped_for_rate += 1
                continue
            m = fut_cache.get(s["stock"]) or {}
            if day not in m:
                continue
            fut_close = float(m[day][1])
            if fut_close <= 0:
                continue
            entry_price = fut_close * s["entry_frac"]
            margin = estimate_margin_ntd(entry_price)
            if margin <= available_margin:
                open_positions.append({
                    "stock": s["stock"], "entry_price": entry_price, "entry_day_idx": day_idx,
                    "margin": margin, "peak": entry_price,
                })
                available_margin -= margin
                taken += 1
                entry_day_log.append(day_idx)
                if rate_limit is not None:
                    entries_in_window += 1
                max_concurrent_seen = max(max_concurrent_seen, len(open_positions))
            else:
                skipped_for_capital += 1

        unrealized = 0.0
        for p in open_positions:
            m = fut_cache.get(p["stock"]) or {}
            px = m.get(day)
            if px is None:
                continue
            close = float(px[1])
            if close <= 0:
                continue
            notional = p["margin"] / MARGIN_RATE
            unrealized += notional * (close / p["entry_price"] - 1)
        nav_series.append(total_capital + realized_pnl + unrealized)

    nav_arr = np.array(nav_series)
    daily_ret = np.diff(nav_arr) / nav_arr[:-1]
    daily_ret = daily_ret[np.isfinite(daily_ret)]
    peak = np.maximum.accumulate(nav_arr)
    dd = (nav_arr - peak) / peak * 100
    max_dd = float(dd.min()) if len(dd) else 0.0
    total_ret_pct = (nav_arr[-1] / total_capital - 1) * 100 if len(nav_arr) else 0.0
    sharpe_annualized = (
        float(daily_ret.mean() / daily_ret.std() * np.sqrt(252)) if len(daily_ret) > 1 and daily_ret.std() > 0
        else float("nan")
    )
    return {
        "taken": taken,
        "skipped_for_capital": skipped_for_capital,
        "skipped_for_rate": skipped_for_rate,
        "total_ret_pct": total_ret_pct,
        "max_drawdown_pct": max_dd,
        "sharpe_annualized": sharpe_annualized,
        "max_concurrent_seen": max_concurrent_seen,
        "near_margin_call_zone": max_dd <= -MARGIN_CALL_DANGER_ZONE_PCT,
    }


def _fmt_row(label: str, r: dict) -> str:
    return (
        f"{label:>26} {r['taken']:>6} {r['skipped_for_capital']:>8} {r['skipped_for_rate']:>8} "
        f"{r['max_concurrent_seen']:>6} {r['total_ret_pct']:>10.1f} {r['sharpe_annualized']:>10.3f} "
        f"{r['max_drawdown_pct']:>10.1f} {('是' if r['near_margin_call_zone'] else '否'):>6}"
    )


def main() -> None:
    trades = load_trades()
    con = sqlite3.connect(f"file:{stock_db.DEFAULT_DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    tx_con = sqlite3.connect(f"file:{TX_BARS_DB}?mode=ro", uri=True)
    fut_cache = json.loads(FUT_CACHE_PATH.read_text(encoding="utf-8"))

    signals = []
    for t in trades:
        sid, t01 = t["stock"], t["trade_date"]
        stock_closes = load_stock_minute_closes(con, sid, t01)
        tx_closes = load_tx_minute_closes(tx_con, t01)
        if len(tx_closes) < 50 or len(stock_closes) < 50:
            continue
        sig = find_rolling_dip_signal(stock_closes, tx_closes)
        if sig is None:
            continue
        _, sig_price = sig
        day_close = _t01_stock_close(con, sid, t01)
        if day_close is None or day_close <= 0:
            continue
        signals.append({
            "stock": sid, "trade_date": t01, "entry_frac": sig_price / day_close,
            "n_seats": int(t["n_seats"]),
        })

    print("=== 限制新進場速率（分散時間集中度）vs 資金規則 ===")
    print(f"訊號: {len(signals)}/{len(trades)} 筆有效\n")
    if not signals:
        raise SystemExit("no signals produced")

    calendar = build_calendar(con, min(s["trade_date"] for s in signals), "2026-08-09")
    print(f"交易日曆: {calendar[0]} ~ {calendar[-1]}（{len(calendar)}天）\n")

    header = (
        f"{'情境':>26} {'成交':>6} {'因資金跳過':>8} {'因速率跳過':>8} "
        f"{'最大同時在場':>6} {'總報酬%':>10} {'年化Sharpe':>10} {'最大回檔%':>10} {'逼近追繳區':>6}"
    )

    best = None
    for cap in CAPITAL_SCENARIOS_NTD:
        print(f"--- 總資金 {cap:,} NTD ---")
        print(header)
        for rl in RATE_LIMITS:
            label = "無速率限制(基準)" if rl is None else f"{rl[0]}日內最多{rl[1]}筆"
            r = run_simulation(signals, fut_cache, calendar, cap, rate_limit=rl)
            print(_fmt_row(label, r))
            if not r["near_margin_call_zone"] and (best is None or r["sharpe_annualized"] > best[2]["sharpe_annualized"]):
                best = (cap, rl, r)
        print()

    print(
        "⚠️ 限制：TX-real訊號(15分/0.3%/10分，未在此重掃)、1口/部位、移動停利5%/最長"
        "10日、13.5%保證金概估、5bps成本未經滑價實測。速率限制本身沒有做walk-forward"
        "切分驗證（這是風控機制不是預測參數，但候選組合仍是掃出來的，樣本外穩健性"
        "未獨立確認）。"
    )

    if best:
        cap, rl, r = best
        label = "無速率限制" if rl is None else f"{rl[0]}日內最多{rl[1]}筆"
        print(f"\n找到能避開追繳警戒區的最佳組合：總資金{cap:,}NTD + {label} "
              f"(Sharpe={r['sharpe_annualized']:.3f}, 最大回檔={r['max_drawdown_pct']:.1f}%)")
    else:
        print("\n沒有任何組合能避開15%追繳警戒區。")

    append_trial(
        "dayflip_short_gapup_short",
        topic_id="entry-rate-limit-vs-capital-rules",
        ts="2026-08-09",
        params={"capital_scenarios_ntd": list(CAPITAL_SCENARIOS_NTD), "rate_limits_tested": str(RATE_LIMITS)},
        n_observations=len(signals),
        metric_name="found_safe_combo",
        metric_value=1.0 if best else 0.0,
        status="kept" if best else "rejected",
        source=__file__,
        notes=(
            "限制新進場速率（過去N日內最多開幾個新部位，超過就跳過n_seats較低的候選）"
            "取代資金規則本身，主動分散訊號叢集造成的相關性回檔。" + (
                f"找到：{best[0]:,}NTD + {'無限制' if best[1] is None else f'{best[1][0]}日內最多{best[1][1]}筆'}"
                f"，Sharpe={best[2]['sharpe_annualized']:.3f}，最大回檔={best[2]['max_drawdown_pct']:.1f}%。"
                if best else "掃過的組合全數仍逼近或落在15%追繳警戒區，速率限制本身沒有解決問題。"
            )
        ),
        tags=["dayflip-short", "post-dump", "long-side", "entry-rate-limit", "capital-sizing"],
    )
    print("\n(已記入 reports/research/_trial_registry/dayflip_short_gapup_short.jsonl)")


if __name__ == "__main__":
    main()
