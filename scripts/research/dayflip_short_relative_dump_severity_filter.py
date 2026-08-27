#!/usr/bin/env python3
"""用「相對大盤的倒貨嚴重程度」當篩選，不是當進場時機訊號.

使用者的邏輯：分點T0買超200萬，個股相對大盤會偏高；隔天隔日沖客把這200萬倒出來，
個股相對大盤會偏低——倒得越兇（相對大盤越弱），代表賣壓越大、洗越乾淨，之後
籌碼面訊號重新發揮作用的訊號應該越可靠。

跟上一輪不同：上一輪是把「大盤校正」做進盤中『進場時機』訊號（結果沒有比較好）；
這一輪測的是把「T0+1當天相對大盤的弱勢程度」當『篩選』——只挑相對大盤跌最兇的
那批訊號股來做，看是不是真的比隨便挑（或挑跌最少的）表現更好。

方法：算每筆交易 T0+1 當天『個股報酬 - 0050同日報酬』（相對大盤超額報酬，越負
代表當天被倒貨倒得越兇），依這個指標分三組（最弱1/3、中間1/3、最強1/3），
各自跑移動停利5%回測比較。

PYTHONPATH=src .venv/bin/python scripts/research/dayflip_short_relative_dump_severity_filter.py
"""

from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

import numpy as np
from scipy import stats

import stock_db
from stock_db.kbar import load_kbar_day_bars
from trial_registry import append_trial

ROOT = Path(__file__).resolve().parents[2]
TRADES_CSV = ROOT / "reports/research/branch-footprint-screen/dayflip_gapup_short/all_trades.csv"
FUT_CACHE_PATH = ROOT / "reports/research/branch-footprint-screen/dayflip_gapup_short/futures_daily_cache.json"

ROUND_TRIP_COST_PCT = 0.05
TRAIL_PCT = 5.0
MAX_HOLD_DAYS = 10
REBOUND_THRESHOLD_PCT = 1.5
MIN_MINUTES_OFF_LOW = 15
BENCH = "0050"


def load_trades() -> list[dict]:
    with TRADES_CSV.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def find_entry_price(con: sqlite3.Connection, stock_id: str, t01: str) -> tuple[float, str] | None:
    raw = load_kbar_day_bars(con, stock_id, t01)
    bars = [
        (b.minute[:5], b.low, b.close)
        for b in raw
        if "09:00" <= b.minute[:5] <= "13:30" and b.low and b.low > 0 and b.close
    ]
    if len(bars) < 50:
        return None
    running_low = bars[0][1]
    running_low_idx = 0
    for i, (minute, low, close) in enumerate(bars):
        if low < running_low:
            running_low = low
            running_low_idx = i
        if (i - running_low_idx) >= MIN_MINUTES_OFF_LOW and (close / running_low - 1) * 100 >= REBOUND_THRESHOLD_PCT:
            return close, "intraday_signal"
    return bars[-1][2], "close_fallback"


def _close_on(con: sqlite3.Connection, stock_id: str, trade_date: str) -> float | None:
    row = con.execute(
        "SELECT close FROM stock_daily_bars WHERE stock_id=? AND trade_date=? AND source='finmind' AND close>0",
        (stock_id, trade_date),
    ).fetchone()
    return float(row[0]) if row else None


def _prev_close(con: sqlite3.Connection, stock_id: str, trade_date: str) -> float | None:
    row = con.execute(
        "SELECT close FROM stock_daily_bars WHERE stock_id=? AND trade_date<? AND source='finmind' AND close>0 "
        "ORDER BY trade_date DESC LIMIT 1",
        (stock_id, trade_date),
    ).fetchone()
    return float(row[0]) if row else None


def simulate_trailing(fut_cache: dict, stock_id: str, t01: str, entry_frac_of_close: float) -> float | None:
    m = fut_cache.get(stock_id) or {}
    dates = sorted(m)
    if t01 not in dates:
        return None
    i0 = dates.index(t01)
    if i0 + MAX_HOLD_DAYS >= len(dates):
        return None
    fut_close_t01 = float(m[t01][1])
    if fut_close_t01 <= 0:
        return None
    fut_entry = fut_close_t01 * entry_frac_of_close
    peak = fut_entry
    for h in range(1, MAX_HOLD_DAYS + 1):
        d = dates[i0 + h]
        px = float(m[d][1])
        if px <= 0:
            return None
        peak = max(peak, px)
        pullback = (peak - px) / peak * 100
        if pullback >= TRAIL_PCT or h == MAX_HOLD_DAYS:
            return (px / fut_entry - 1) * 100 - ROUND_TRIP_COST_PCT
    return None


def metrics(rets: list[float]) -> dict:
    if not rets:
        return {"n": 0}
    arr = np.array(rets)
    win_rate = float(np.mean(arr > 0))
    mean_ret = float(arr.mean())
    std_ret = float(arr.std())
    sharpe_like = mean_ret / std_ret if std_ret > 0 else float("nan")
    return {"n": len(arr), "win_rate": win_rate, "mean_ret_pct": mean_ret, "sharpe_like": sharpe_like}


def main() -> None:
    trades = load_trades()
    con = sqlite3.connect(f"file:{stock_db.DEFAULT_DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    fut_cache = json.loads(FUT_CACHE_PATH.read_text(encoding="utf-8"))

    prepared = []
    for t in trades:
        sid, t01 = t["stock"], t["trade_date"]
        entry = find_entry_price(con, sid, t01)
        if entry is None:
            continue
        entry_price, _ = entry
        day_close = _close_on(con, sid, t01)
        if day_close is None or day_close <= 0:
            continue

        stock_prev = _prev_close(con, sid, t01)
        bench_now = _close_on(con, BENCH, t01)
        bench_prev = _prev_close(con, BENCH, t01)
        if not stock_prev or not bench_now or not bench_prev:
            continue
        stock_ret = (day_close / stock_prev - 1) * 100
        bench_ret = (bench_now / bench_prev - 1) * 100
        relative_dump_severity = stock_ret - bench_ret  # 越負代表相對大盤跌越兇（倒貨越乾淨）

        net_ret = simulate_trailing(fut_cache, sid, t01, entry_price / day_close)
        if net_ret is None:
            continue
        prepared.append({"stock": sid, "trade_date": t01, "net_ret_pct": net_ret,
                          "relative_dump_severity": relative_dump_severity})

    print(f"=== 相對大盤倒貨嚴重程度 → 之後移動停利報酬，有沒有關係 ===")
    print(f"可分析: {len(prepared)}/{len(trades)}\n")

    severities = [p["relative_dump_severity"] for p in prepared]
    rets = [p["net_ret_pct"] for p in prepared]
    rho, p_corr = stats.spearmanr(severities, rets)
    print(f"相對倒貨嚴重程度 vs 後續淨報酬：Spearman rho={rho:+.3f}, p={p_corr:.4f}")
    print("（rho為負代表『相對大盤跌越兇，後續報酬越高』，符合使用者的假說；"
          "rho接近0或正代表沒有這個關係）\n")

    prepared_sorted = sorted(prepared, key=lambda p: p["relative_dump_severity"])
    n = len(prepared_sorted)
    tercile = n // 3
    groups = {
        "倒貨最兇（相對大盤最弱）1/3": prepared_sorted[:tercile],
        "中段1/3": prepared_sorted[tercile:2 * tercile],
        "倒貨最輕（相對大盤最強）1/3": prepared_sorted[2 * tercile:],
    }
    print("--- 三分位比較 ---")
    for label, grp in groups.items():
        m = metrics([g["net_ret_pct"] for g in grp])
        sev_range = (min(g["relative_dump_severity"] for g in grp), max(g["relative_dump_severity"] for g in grp))
        print(
            f"  {label}（相對大盤超額報酬範圍 {sev_range[0]:+.1f}%~{sev_range[1]:+.1f}%）: "
            f"n={m.get('n',0)} 勝率={m.get('win_rate',0)*100:.0f}% "
            f"平均淨報酬={m.get('mean_ret_pct',0):+.3f}% sharpe_like={m.get('sharpe_like',float('nan')):.3f}"
        )

    worst_third = [g["net_ret_pct"] for g in prepared_sorted[:tercile]]
    best_third = [g["net_ret_pct"] for g in prepared_sorted[2 * tercile:]]
    u, p_mw = stats.mannwhitneyu(worst_third, best_third, alternative="two-sided")
    print(f"\n倒貨最兇 vs 倒貨最輕 兩組差異：Mann-Whitney p={p_mw:.4f}")

    print(
        "\n⚠️ 限制：\n"
        "  1) 這裡測的是『T0+1全天相對大盤表現』當篩選指標，不是盤中即時可算的\n"
        "     timing訊號——當天收盤才知道全天相對表現，等於是「今天倒貨倒得兇，\n"
        "     明天才進場」的篩選規則，不是同一天當下判斷。\n"
        "  2) 沒有做walk-forward切分驗證（只是相關性/分組比較），純粹回答\n"
        "     「這個假說在資料裡站不站得住腳」，還沒到能直接拿來下單的規則。"
    )

    append_trial(
        "dayflip_short_gapup_short",
        topic_id="relative-dump-severity-vs-forward-return",
        ts="2026-08-09",
        params={"benchmark": BENCH},
        n_observations=len(prepared),
        metric_name="spearman_rho_severity_vs_forward_return",
        metric_value=float(rho),
        status="kept" if p_corr < 0.05 and rho < 0 else "rejected",
        source=__file__,
        notes=(
            f"使用者假說：T0+1相對大盤跌越兇(倒貨越乾淨)，後續報酬越好。n={len(prepared)}，"
            f"Spearman rho={rho:+.3f} p={p_corr:.4f}；三分位比較見腳本輸出。"
        ),
        tags=["dayflip-short", "post-dump", "long-side", "relative-severity", "market-relative"],
    )
    print("\n(已記入 reports/research/_trial_registry/dayflip_short_gapup_short.jsonl)")


if __name__ == "__main__":
    main()
