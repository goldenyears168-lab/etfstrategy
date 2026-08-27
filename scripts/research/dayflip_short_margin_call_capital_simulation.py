#!/usr/bin/env python3
"""dayflip-short post-dump 做多——資金/保證金模擬 + 保證金追繳(margin call)機制.

前一輪的資金排程模擬（dayflip_short_post_dump_long_capital_simulation.py，以及
沿用同一套迴圈的 dayflip_short_post_dump_long_final_combined.py）明確承認一個
限制：「沒有真的模擬保證金追繳(margin call)機制」——NAV 逐日 mark-to-market，
但無論帳戶虧到多深，所有未平倉部位都撐到移動停利5%或最長10日才會出場。真實
情況下，券商在帳戶淨值跌破維持保證金／自訂風控門檻時會強制砍倉，這裡補上這
個機制：

  逐日 mark-to-market 後，若 NAV 相對「歷史高點(running peak)」的回檔幅度超過
  門檻（測試15%/20%/25%/30%），就依「未實現損益最負」排序，強制在當天期貨收
  盤價平倉，一檔一檔砍，直到回檔幅度回到門檻之上，或已無未平倉部位。

進場訊號沿用上一輪驗證過的固定規則（滾動窗口相對弱勢訊號，15分鐘窗、0.3%落
後門檻、10分鐘收斂確認，見 dayflip_short_rolling_relative_dip_signal.py 的
walk-forward 結果）——這裡的重點是保證金追繳機制本身，不是重新調訊號。

跑法：資金規模(1M/2M/3M/5M NTD) × margin call 門檻(15%/20%/25%/30%，外加無
margin call 的 baseline) 全部交叉，報總報酬/最大回檔/年化Sharpe/margin call
發生天數/強制平倉次數，並跟 baseline（無margin call）對照，回答「加了margin
call 機制會不會顛覆前一輪『約2,000,000 NTD是甜蜜點』的結論」。

PYTHONPATH=src .venv/bin/python scripts/research/dayflip_short_margin_call_capital_simulation.py
"""

from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

import numpy as np

import stock_db
from stock_db.kbar import load_kbar_day_bars
from trial_registry import append_trial

ROOT = Path(__file__).resolve().parents[2]
TRADES_CSV = ROOT / "reports/research/branch-footprint-screen/dayflip_gapup_short/all_trades.csv"
FUT_CACHE_PATH = ROOT / "reports/research/branch-footprint-screen/dayflip_gapup_short/futures_daily_cache.json"

ROUND_TRIP_COST_PCT = 0.05
TRAIL_PCT = 5.0
MAX_HOLD_DAYS = 10
MARGIN_RATE = 0.135
LOT_SHARES = 2000
BENCH = "0050"
ROLLING_WINDOW_MIN = 15
LAG_THRESHOLD_PCT = 0.3  # walk-forward 驗證過的固定門檻，沿用不重掃
CONFIRM_MINUTES = 10
CAPITAL_SCENARIOS_NTD = (1_000_000, 2_000_000, 3_000_000, 5_000_000)
MARGIN_CALL_THRESHOLDS_PCT = (15.0, 20.0, 25.0, 30.0)  # NAV 相對歷史高點回檔超過此值就強制砍倉


def load_trades() -> list[dict]:
    with TRADES_CSV.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_minute_closes(con: sqlite3.Connection, stock_id: str, t01: str) -> dict[str, float]:
    raw = load_kbar_day_bars(con, stock_id, t01)
    return {
        b.minute[:5]: b.close
        for b in raw
        if "09:00" <= b.minute[:5] <= "13:30" and b.close and b.close > 0
    }


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


def _position_unrealized_pct(p: dict, close: float) -> float:
    return (close / p["entry_price"] - 1) * 100


def run_simulation(
    signals: list[dict],
    fut_cache: dict,
    calendar: list[str],
    total_capital: float,
    margin_call_threshold_pct: float | None,
) -> dict:
    """逐日資金/保證金排程模擬，選配 margin call 強制砍倉機制.

    margin_call_threshold_pct=None 時完全等同前一輪 baseline（無margin call）。
    """
    signals_by_date: dict[str, list[dict]] = {}
    for s in signals:
        signals_by_date.setdefault(s["trade_date"], []).append(s)
    for d in signals_by_date:
        signals_by_date[d].sort(key=lambda s: -s["n_seats"])

    open_positions: list[dict] = []
    realized_pnl = 0.0
    skipped_for_capital = 0
    taken = 0
    nav_series = []
    utilization_series = []
    running_peak = total_capital
    margin_call_days = 0
    forced_liquidations = 0

    for day_idx, day in enumerate(calendar):
        margin_used = sum(p["margin"] for p in open_positions)
        available_margin = total_capital - margin_used
        utilization_series.append(margin_used / total_capital * 100)

        # (1) 一般出場：移動停利 / 最長持有天數
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

        # (2) 進場
        for s in signals_by_date.get(day, []):
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
            else:
                skipped_for_capital += 1

        # (3) mark-to-market NAV
        def _nav_now() -> float:
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
            return total_capital + realized_pnl + unrealized

        nav = _nav_now()

        # (4) margin call：若相對「今天之前的歷史高點」回檔超過門檻，強制砍最爛的部位
        if margin_call_threshold_pct is not None and running_peak > 0 and open_positions:
            dd_pct = (nav - running_peak) / running_peak * 100
            if dd_pct < -margin_call_threshold_pct:
                margin_call_days += 1
            while dd_pct < -margin_call_threshold_pct and open_positions:
                # 未實現損益最負的部位優先砍（用當天收盤價評估）
                worst = None
                worst_ret = None
                for p in open_positions:
                    m = fut_cache.get(p["stock"]) or {}
                    px = m.get(day)
                    if px is None or float(px[1]) <= 0:
                        continue
                    ret = _position_unrealized_pct(p, float(px[1]))
                    if worst_ret is None or ret < worst_ret:
                        worst_ret, worst = ret, p
                if worst is None:
                    break  # 剩下的部位當天沒有收盤價可用，砍不了，跳出避免死迴圈
                close = float((fut_cache.get(worst["stock"]) or {})[day][1])
                raw_ret = (close / worst["entry_price"] - 1) * 100
                net_ret = raw_ret - ROUND_TRIP_COST_PCT
                realized_pnl += worst["margin"] / MARGIN_RATE * (net_ret / 100)
                open_positions.remove(worst)
                forced_liquidations += 1
                nav = _nav_now()
                dd_pct = (nav - running_peak) / running_peak * 100

        running_peak = max(running_peak, nav)
        nav_series.append(nav)

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
        "taken": taken, "skipped_for_capital": skipped_for_capital,
        "total_ret_pct": total_ret_pct, "max_drawdown_pct": max_dd,
        "sharpe_annualized": sharpe_annualized,
        "avg_capital_utilization_pct": float(np.mean(utilization_series)) if utilization_series else 0.0,
        "margin_call_days": margin_call_days, "forced_liquidations": forced_liquidations,
    }


def main() -> None:
    trades = load_trades()
    con = sqlite3.connect(f"file:{stock_db.DEFAULT_DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    fut_cache = json.loads(FUT_CACHE_PATH.read_text(encoding="utf-8"))

    signals = []
    for t in trades:
        sid, t01 = t["stock"], t["trade_date"]
        stock_closes = load_minute_closes(con, sid, t01)
        bench_closes = load_minute_closes(con, BENCH, t01)
        sig = find_rolling_dip_signal(stock_closes, bench_closes)
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

    print("=== 資金/保證金排程模擬 + margin call 強制砍倉機制 ===")
    print(f"進場：滾動相對弱勢訊號({LAG_THRESHOLD_PCT:.1f}%門檻) · 出場：移動停利{TRAIL_PCT:.0f}%/最長{MAX_HOLD_DAYS}日")
    print(f"訊號數: {len(signals)}/{len(trades)}\n")

    calendar = build_calendar(con, min(s["trade_date"] for s in signals), "2026-08-07")
    print(f"交易日曆: {calendar[0]} ~ {calendar[-1]}（{len(calendar)}天）\n")

    all_results: dict[tuple[float, float | None], dict] = {}
    header = (
        f"{'總資金(NTD)':>14} {'margin-call門檻':>16} {'成交':>6} {'因資金跳過':>10} "
        f"{'總報酬%':>10} {'年化Sharpe':>10} {'最大回檔%':>10} {'MC天數':>8} {'強制平倉':>8}"
    )
    print(header)
    for cap in CAPITAL_SCENARIOS_NTD:
        baseline = run_simulation(signals, fut_cache, calendar, cap, None)
        all_results[(cap, None)] = baseline
        print(
            f"{cap:>14,} {'無(baseline)':>16} {baseline['taken']:>6} {baseline['skipped_for_capital']:>10} "
            f"{baseline['total_ret_pct']:>10.1f} {baseline['sharpe_annualized']:>10.3f} "
            f"{baseline['max_drawdown_pct']:>10.1f} {baseline['margin_call_days']:>8} {baseline['forced_liquidations']:>8}"
        )
        for th in MARGIN_CALL_THRESHOLDS_PCT:
            result = run_simulation(signals, fut_cache, calendar, cap, th)
            all_results[(cap, th)] = result
            print(
                f"{cap:>14,} {th:>15.0f}% {result['taken']:>6} {result['skipped_for_capital']:>10} "
                f"{result['total_ret_pct']:>10.1f} {result['sharpe_annualized']:>10.3f} "
                f"{result['max_drawdown_pct']:>10.1f} {result['margin_call_days']:>8} {result['forced_liquidations']:>8}"
            )
        print()

    # 逐資金規模比較 baseline vs margin-call 各門檻，看甜蜜點(2,000,000 NTD)結論會不會變
    print("=== baseline(無margin call) vs margin-call門檻 差異摘要 ===")
    for cap in CAPITAL_SCENARIOS_NTD:
        base = all_results[(cap, None)]
        print(f"\n資金 {cap:,} NTD baseline: 總報酬{base['total_ret_pct']:+.1f}% "
              f"Sharpe{base['sharpe_annualized']:.3f} 最大回檔{base['max_drawdown_pct']:.1f}%")
        for th in MARGIN_CALL_THRESHOLDS_PCT:
            r = all_results[(cap, th)]
            d_ret = r["total_ret_pct"] - base["total_ret_pct"]
            d_dd = r["max_drawdown_pct"] - base["max_drawdown_pct"]
            d_sharpe = r["sharpe_annualized"] - base["sharpe_annualized"]
            print(f"  門檻{th:>4.0f}%: 總報酬Δ{d_ret:+7.1f}%pt · 最大回檔Δ{d_dd:+6.1f}%pt · "
                  f"SharpeΔ{d_sharpe:+.3f} · MC發生{r['margin_call_days']}天 · 強制平倉{r['forced_liquidations']}次")

    # 找出「約2,000,000 NTD是甜蜜點」是否仍成立：在每個margin-call門檻下，
    # 比較各資金規模的Sharpe排序，看2M是否仍是最高（或接近最高）
    print("\n=== 甜蜜點(2,000,000 NTD)結論穩健性檢查（各margin-call門檻下的Sharpe排序）===")
    two_m_still_best_count = 0
    checks = 0
    for th in (None,) + MARGIN_CALL_THRESHOLDS_PCT:
        by_cap_sharpe = {cap: all_results[(cap, th)]["sharpe_annualized"] for cap in CAPITAL_SCENARIOS_NTD}
        best_cap = max(by_cap_sharpe, key=lambda c: by_cap_sharpe[c] if np.isfinite(by_cap_sharpe[c]) else -999)
        label = "baseline" if th is None else f"門檻{th:.0f}%"
        print(f"  {label}: Sharpe最高資金規模 = {best_cap:,} NTD "
              f"(Sharpe={by_cap_sharpe[best_cap]:.3f}) · 2,000,000 NTD Sharpe={by_cap_sharpe[2_000_000]:.3f}")
        checks += 1
        if best_cap == 2_000_000:
            two_m_still_best_count += 1

    sweet_spot_holds = two_m_still_best_count == checks
    print(
        f"\n結論：加了margin call機制後，2,000,000 NTD在{two_m_still_best_count}/{checks}個情境"
        f"（baseline + {len(MARGIN_CALL_THRESHOLDS_PCT)}個門檻）中仍是Sharpe最高的資金規模——"
        f"{'甜蜜點結論未被顛覆' if sweet_spot_holds else '甜蜜點結論在部分margin-call情境下不再成立，需重新檢視'}。"
    )

    print(
        "\n⚠️ 限制：\n"
        "  1) margin call門檻用「NAV相對歷史高點回檔%」這種簡化定義，不是真實券商的\n"
        "     維持保證金率(如原始保證金的75%)機制——後者需要逐倉維持保證金資料，\n"
        "     本模擬沒有那個資料，用整體NAV回檔近似。\n"
        "  2) 強制平倉排序用「未實現損益最負」，一次只砍到回檔幅度回到門檻之上即停，\n"
        "     不是全部砍光；這個排序規則跟前一輪『搶資金用n_seats排序』一樣，沒有另外\n"
        "     驗證是不是比其他排序（例如FIFO、部位規模）更貼近真實券商行為。\n"
        "  3) 進場訊號、出場規則跟前一輪完全相同（沒有為了margin call重新調參），\n"
        "     這裡驗證的只是margin call機制本身對既有策略的影響。\n"
        "  4) 同前幾輪：保證金用13.5%概估、無融資利息/機會成本、多訊號搶資金仍用\n"
        "     n_seats排序。"
    )

    best_cap_ref = 2_000_000
    ref_result = all_results[(best_cap_ref, 20.0)]
    append_trial(
        "dayflip_short_gapup_short",
        topic_id="capital-sim-with-margin-call",
        ts="2026-08-09",
        params={
            "capital_scenarios_ntd": list(CAPITAL_SCENARIOS_NTD),
            "margin_call_thresholds_pct": list(MARGIN_CALL_THRESHOLDS_PCT),
            "trail_pct": TRAIL_PCT, "margin_rate": MARGIN_RATE,
            "entry": "rolling_relative_dip_0.3pct", "liquidation_priority": "most_negative_unrealized_pnl",
        },
        n_observations=len(signals),
        metric_name=f"total_ret_pct_at_{best_cap_ref}_ntd_mc20pct",
        metric_value=ref_result["total_ret_pct"],
        status="kept" if sweet_spot_holds else "rejected",
        source=__file__,
        notes=(
            f"補上margin call(保證金追繳)強制砍倉機制，前一輪明確承認的限制。"
            f"NAV相對歷史高點回檔超過門檻(15/20/25/30%)時，依未實現損益最負排序強制砍倉。"
            f"2,000,000 NTD規模在margin-call 20%門檻下：總報酬{ref_result['total_ret_pct']:+.1f}%、"
            f"Sharpe{ref_result['sharpe_annualized']:.3f}、最大回檔{ref_result['max_drawdown_pct']:.1f}%、"
            f"margin call發生{ref_result['margin_call_days']}天、強制平倉{ref_result['forced_liquidations']}次。"
            f"甜蜜點(2,000,000 NTD)結論在{two_m_still_best_count}/{checks}個情境下"
            f"{'仍成立' if sweet_spot_holds else '不成立，需重新檢視'}。"
        ),
        tags=["dayflip-short", "post-dump", "long-side", "capital-simulation", "margin-call", "risk-management"],
    )
    print("\n(已記入 reports/research/_trial_registry/dayflip_short_gapup_short.jsonl)")


if __name__ == "__main__":
    main()
