#!/usr/bin/env python3
"""night|div_hh_weak_vol：block=['L','S'] 改成「不 block，掛遠一點」是否有 edge？

背景：production PAPER_RECIPE（final_v1_4_0_pv16_normal_dhwv_block）對
night|div_hh_weak_vol 這格是硬 block（不掛單），因為回測發現這格整體是負期望值。
使用者假設：如果不 block，而是把掛單距離拉得比原本（unblocked-style）base
hang_lo=18/hang_hi=32 更遠，只有更罕見、更極端的價格移動才會碰到成交，這些罕見
成交的期望值可能跟這格平均值不同（甚至為正）。這支腳本做 TRUE re-simulation
驗證這個假設 —— 不是事後過濾既有交易列表，而是真的把該格 block 拿掉、hang
距離改寬後，用 production frozen engine（tmf_channel.causal_engine，經
tmf_channel.engine facade）逐日重跑，其餘 15 格 cell 完全不動（deepcopy
PAPER_RECIPE['session_pv_book'] 後只改這一格）。

3 個候選寬度（相對 base 18/32 的倍數）：
  1.5x -> hang_lo=27, hang_hi=48
  2x   -> hang_lo=36, hang_hi=64
  3x   -> hang_lo=54, hang_hi=96

驗證窗口：4 個 sanctioned validation windows（w83 / julsep25 / octdec25 / janmar26）。

統計方法（day-clustered，rule #2）：每個「有該格夜盤成交」的交易日算一個
sum(pnl)，對這組 per-day 數字做 one-sample t-test（H0: mean=0），
n = 有成交的交易日數，不是全部交易日數。

⚠️ Research only，不動 production 程式／config。此腳本只讀
tmf_channel.cache_store / tmf_channel.engine / order.tmf_channel_config，
不 import src/order/*（下單層）以外的東西，也不寫任何 order-intent。
"""

from __future__ import annotations

import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from scipy import stats  # noqa: E402

from order.tmf_channel_config import PAPER_RECIPE  # noqa: E402
from tmf_channel.cache_store import list_days, load_day  # noqa: E402
from tmf_channel.engine import load_vixtwn_delta, simulate  # noqa: E402

CELL_SESSION = "night"
CELL_NAME = "div_hh_weak_vol"
BASE_LO, BASE_HI = 18.0, 32.0

WINDOWS = [
    ("w83", "tx_1m_fullnight_cache_full.json"),
    ("julsep25", "tx_1m_julsep_holdout_cache.json"),
    ("octdec25", "tx_1m_octdec_holdout_cache.json"),
    ("janmar26", "tx_1m_janmar_holdout_cache.json"),
]

CANDIDATES = [
    ("1.5x", 27.0, 48.0),
    ("2x", 36.0, 64.0),
    ("3x", 54.0, 96.0),
]


def build_recipe(hang_lo: float, hang_hi: float) -> dict[str, Any]:
    recipe = deepcopy(PAPER_RECIPE)
    book = recipe["session_pv_book"]
    cell = dict(book[CELL_SESSION][CELL_NAME])
    before = dict(cell)
    cell["block"] = []
    cell["hang_lo"] = hang_lo
    cell["hang_hi"] = hang_hi
    book[CELL_SESSION][CELL_NAME] = cell
    assert before.get("block") == ["L", "S"], f"unexpected baseline cell: {before}"
    # sanity: every OTHER cell must be byte-identical to live PAPER_RECIPE
    for sess in ("day", "night"):
        for name, live_cell in PAPER_RECIPE["session_pv_book"][sess].items():
            if sess == CELL_SESSION and name == CELL_NAME:
                continue
            assert book[sess][name] == live_cell, f"leaked change into {sess}|{name}"
    return recipe


def arrays_from_rows(day: str, rows: list[dict[str, Any]]):
    O = [float(r["o"]) for r in rows]
    H = [float(r["h"]) for r in rows]
    L = [float(r["l"]) for r in rows]
    C = [float(r["c"]) for r in rows]
    V = [float(r.get("v") or 0) for r in rows]
    T = [f"{day}T{r.get('t')}:00.000+08:00" for r in rows]
    return O, H, L, C, V, T


def is_night_time(hm: str) -> bool:
    return hm >= "15:00" or hm < "05:00"


def run_window(cache_name: str, recipe: dict[str, Any], vix: dict[str, float]):
    """Return {day: [matched trade dicts]} for this cache/window."""
    per_day: dict[str, list[dict[str, Any]]] = {}
    days = list_days(source=cache_name)
    for day in days:
        rows = load_day(day, source=cache_name)
        if not rows:
            continue
        O, H, L, C, V, T = arrays_from_rows(day, rows)
        trades, *_rest = simulate(O, H, L, C, V, T, recipe, vix_delta=vix)
        matched = [
            t
            for t in (trades or [])
            if t.get("regime_e") == CELL_NAME and is_night_time(str(t.get("et", "")))
        ]
        if matched:
            per_day[day] = matched
    return per_day


def day_clustered_stats(per_day: dict[str, list[dict[str, Any]]]):
    day_sums = [sum(t["pnl"] for t in trs) for trs in per_day.values()]
    n_days = len(day_sums)
    all_trades = [t for trs in per_day.values() for t in trs]
    n_trades = len(all_trades)
    if n_days >= 2:
        t_stat, p_val = stats.ttest_1samp(day_sums, 0.0)
    else:
        t_stat, p_val = float("nan"), float("nan")
    mean_day = sum(day_sums) / n_days if n_days else 0.0
    mean_trade = sum(t["pnl"] for t in all_trades) / n_trades if n_trades else 0.0
    wr = (
        100.0 * sum(1 for t in all_trades if t["pnl"] > 0) / n_trades
        if n_trades
        else 0.0
    )
    return {
        "t": t_stat,
        "p": p_val,
        "n_days": n_days,
        "n_trades": n_trades,
        "net": sum(t["pnl"] for t in all_trades),
        "mean_day": mean_day,
        "mean_trade": mean_trade,
        "wr": wr,
        "day_sums": day_sums,
        "all_trades": all_trades,
    }


def single_trade_removal(all_trades: list[dict[str, Any]]):
    if not all_trades:
        return None
    pnls = [t["pnl"] for t in all_trades]
    best_i = pnls.index(max(pnls))
    worst_i = pnls.index(min(pnls))
    rest_no_best = pnls[:best_i] + pnls[best_i + 1 :]
    rest_no_worst = pnls[:worst_i] + pnls[worst_i + 1 :]
    return {
        "best_pnl": pnls[best_i],
        "worst_pnl": pnls[worst_i],
        "mean_excl_best": sum(rest_no_best) / len(rest_no_best) if rest_no_best else 0.0,
        "mean_excl_worst": sum(rest_no_worst) / len(rest_no_worst) if rest_no_worst else 0.0,
        "net_excl_best": sum(rest_no_best),
        "net_excl_worst": sum(rest_no_worst),
    }


def main():
    vix = load_vixtwn_delta() or {}
    print(f"cell = {CELL_SESSION}|{CELL_NAME}  base(unblocked-style) hang=({BASE_LO},{BASE_HI})")
    print(f"live recipe_version = {PAPER_RECIPE.get('recipe_version')}")
    print()

    for label, hang_lo, hang_hi in CANDIDATES:
        recipe = build_recipe(hang_lo, hang_hi)
        print("=" * 78)
        print(f"CANDIDATE {label}: hang_lo={hang_lo}, hang_hi={hang_hi}")
        print("=" * 78)

        per_window: dict[str, dict[str, list[dict[str, Any]]]] = {}
        for wname, cache_name in WINDOWS:
            per_window[wname] = run_window(cache_name, recipe, vix)

        # pooled across all 4 windows
        pooled: dict[str, list[dict[str, Any]]] = {}
        for wname, pd_ in per_window.items():
            for day, trs in pd_.items():
                key = f"{wname}:{day}"
                pooled[key] = trs

        pooled_stats = day_clustered_stats(pooled)
        print(
            f"POOLED: n_days={pooled_stats['n_days']} n_trades={pooled_stats['n_trades']} "
            f"net={pooled_stats['net']:+.1f} mean/trade={pooled_stats['mean_trade']:+.1f} "
            f"mean/day={pooled_stats['mean_day']:+.1f} wr={pooled_stats['wr']:.1f}% "
            f"t={pooled_stats['t']:.3f} p={pooled_stats['p']:.4f}"
        )

        rem = single_trade_removal(pooled_stats["all_trades"])
        if rem:
            print(
                f"  single-trade-removal: best={rem['best_pnl']:+.1f} "
                f"(excl -> net={rem['net_excl_best']:+.1f}, mean/trade={rem['mean_excl_best']:+.1f}); "
                f"worst={rem['worst_pnl']:+.1f} "
                f"(excl -> net={rem['net_excl_worst']:+.1f}, mean/trade={rem['mean_excl_worst']:+.1f})"
            )

        print("  per-window:")
        for wname, _cache in WINDOWS:
            ws = day_clustered_stats(per_window[wname])
            print(
                f"    {wname:10s} n_days={ws['n_days']:2d} n_trades={ws['n_trades']:2d} "
                f"net={ws['net']:+8.1f} mean/trade={ws['mean_trade']:+7.1f} wr={ws['wr']:5.1f}%"
            )
            if ws["n_trades"]:
                for day, trs in per_window[wname].items():
                    tag = ",".join(f"{t['s']}{t['pnl']:+.1f}" for t in trs)
                    print(f"        {day}: {tag}")
        print()


if __name__ == "__main__":
    main()
