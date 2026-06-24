#!/usr/bin/env python3
"""Literature-representative exit rules on lens watchlist · walk-forward check."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from statistics import mean, median
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from stock_db import DEFAULT_DB_PATH, connect

from backtest_intraday_exit_universe import (
    MIN_BARS,
    bootstrap_ci,
    build_day_contexts,
    fill_after,
    first_hit_after,
    sync_dd3_day,
    trading_dates,
    vcp_state,
    load_bars,
    prev_close,
)

StrategyFn = Callable  # (conn, sid, d, ctx, bars, pc) -> float | None


def win_rate(saves: list[float]) -> float:
    return sum(1 for s in saves if s < 0) / len(saves) if saves else float("nan")


def summarize(label: str, saves: list[float]) -> str:
    if not saves:
        return f"  {label}: n=0"
    return (
        f"  {label}: n={len(saves)} win={win_rate(saves):.1%} "
        f"med={median(saves):+.2f}% mean={mean(saves):+.2f}%"
    )


def lens_universe(conn, min_full_days: int) -> list[str]:
    rows = conn.execute(
        """
        WITH lens AS (SELECT DISTINCT stock_id FROM lens_daily_highlight),
        fd AS (
          SELECT stock_id, COUNT(*) AS days FROM (
            SELECT stock_id, trade_date, MAX(n) AS mx FROM (
              SELECT stock_id, trade_date, source, COUNT(*) AS n
              FROM stock_kbar_1m GROUP BY stock_id, trade_date, source
            ) GROUP BY stock_id, trade_date HAVING MAX(n) >= ?
          ) GROUP BY stock_id
        )
        SELECT l.stock_id FROM lens l
        JOIN fd ON l.stock_id = fd.stock_id
        WHERE fd.days >= ?
        ORDER BY l.stock_id
        """,
        (MIN_BARS, min_full_days),
    ).fetchall()
    return [str(r[0]) for r in rows]


def save_on_hit(bars: list[dict], pc: float, level: float) -> float | None:
    tm = first_hit_after(bars, level)
    if not tm:
        return None
    fill = fill_after(bars, tm)
    return (float(bars[-1]["close"]) - fill) / pc * 100


def atr14(conn, sid: str, d: str) -> float | None:
    rows = conn.execute(
        """
        SELECT high, low, close FROM stock_daily_bars
        WHERE stock_id = ? AND trade_date <= ?
        ORDER BY trade_date DESC LIMIT 15
        """,
        (sid, d),
    ).fetchall()
    if len(rows) < 15:
        return None
    rows = list(reversed(rows))
    trs: list[float] = []
    for i in range(1, len(rows)):
        h, lo, pc = float(rows[i][0]), float(rows[i][1]), float(rows[i - 1][2])
        trs.append(max(h - lo, abs(h - pc), abs(lo - pc)))
    return sum(trs[-14:]) / 14 if len(trs) >= 14 else None


def strat_fixed_3pct_all(_conn, _sid, _d, _ctx, bars, pc) -> float | None:
    return save_on_hit(bars, pc, pc * 0.97)


def strat_arratia_regime(conn, sid, d, ctx, bars, pc) -> float | None:
    """Arratia & Dorador (2019): fixed % stop in falling-market regime."""
    if not sync_dd3_day(conn, d) and not ctx.mode_on:
        return None
    return save_on_hit(bars, pc, pc * 0.97)


def strat_vcp_stop(conn, _sid, d, _ctx, bars, pc) -> float | None:
    """Minervini VCP structural stop."""
    _, stop = vcp_state(conn, _sid, d)
    if not stop or stop <= 0:
        return None
    return save_on_hit(bars, pc, stop)


def strat_atr15(conn, sid, d, _ctx, bars, pc) -> float | None:
    a = atr14(conn, sid, d)
    if not a or a <= 0:
        return None
    level = pc - 1.5 * a
    if level <= 0:
        return None
    return save_on_hit(bars, pc, level)


STRATEGIES: dict[str, StrategyFn] = {
    "fixed_3pct_all": strat_fixed_3pct_all,
    "aratia_regime_3pct": strat_arratia_regime,
    "vcp_structural_stop": strat_vcp_stop,
    "atr15_stop": strat_atr15,
}


def collect_saves(
    conn,
    dates: list[str],
    universe: list[str],
    contexts: dict,
    fn: StrategyFn,
    stock_filter: frozenset[str] | None = None,
) -> tuple[list[float], dict[str, list[float]]]:
    saves: list[float] = []
    by_stock: dict[str, list[float]] = defaultdict(list)
    for d in dates:
        ctx = contexts[d]
        for sid in universe:
            if stock_filter is not None and sid not in stock_filter:
                continue
            bars = load_bars(conn, sid, d)
            pc = prev_close(conn, sid, d)
            if len(bars) < MIN_BARS or not pc:
                continue
            s = fn(conn, sid, d, ctx, bars, pc)
            if s is not None:
                saves.append(s)
                by_stock[sid].append(s)
    return saves, by_stock


def stable_flag(ci: dict) -> bool:
    return bool(ci and ci["obs_median"] < 0 and ci["med_p97.5"] < 0)


def walk_forward(
    conn,
    dates: list[str],
    universe: list[str],
    contexts: dict,
    strategy_key: str,
    *,
    train_ratio: float = 0.55,
    min_train_n: int = 5,
    win_threshold: float = 0.55,
) -> dict:
    fn = STRATEGIES[strategy_key]
    split = max(1, int(len(dates) * train_ratio))
    train_dates, test_dates = dates[:split], dates[split:]
    _, by_train = collect_saves(conn, train_dates, universe, contexts, fn)
    picked = frozenset(
        sid
        for sid, s in by_train.items()
        if len(s) >= min_train_n and win_rate(s) > win_threshold
    )
    train_saves, _ = collect_saves(conn, train_dates, universe, contexts, fn, picked)
    test_saves, by_test = collect_saves(conn, test_dates, universe, contexts, fn, picked)
    all_test, _ = collect_saves(conn, test_dates, universe, contexts, fn)

    per_stock_test = {
        sid: {"n": len(s), "win_rate": round(win_rate(s), 4), "median_save": round(median(s), 4)}
        for sid, s in by_test.items()
        if s
    }
    return {
        "strategy": strategy_key,
        "train_dates": f"{train_dates[0]}..{train_dates[-1]}",
        "test_dates": f"{test_dates[0]}..{test_dates[-1]}",
        "picked_stocks": sorted(picked),
        "picked_n": len(picked),
        "win_threshold": win_threshold,
        "min_train_n": min_train_n,
        "train": {
            "n": len(train_saves),
            "win_rate": round(win_rate(train_saves), 4) if train_saves else None,
            "median_save": round(median(train_saves), 4) if train_saves else None,
        },
        "test_picked": {
            "n": len(test_saves),
            "win_rate": round(win_rate(test_saves), 4) if test_saves else None,
            "median_save": round(median(test_saves), 4) if test_saves else None,
        },
        "test_all_universe": {
            "n": len(all_test),
            "win_rate": round(win_rate(all_test), 4) if all_test else None,
            "median_save": round(median(all_test), 4) if all_test else None,
        },
        "overfit_risk": (
            train_saves
            and test_saves
            and win_rate(test_saves) < win_threshold - 0.05
        ),
        "per_stock_test": per_stock_test,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Literature exit rules on lens watchlist")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--start", default="2026-01-02")
    parser.add_argument("--end", default="2026-06-22")
    parser.add_argument("--min-full-days", type=int, default=20)
    parser.add_argument("--bootstrap-n", type=int, default=5000)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-walk-forward", action="store_true")
    args = parser.parse_args()

    conn = connect()
    conn.row_factory = sqlite3.Row
    universe = lens_universe(conn, args.min_full_days)
    dates = trading_dates(conn, args.start, args.end)
    contexts = build_day_contexts(conn, dates, universe)
    sync_n = sum(1 for d in dates if sync_dd3_day(conn, d))

    print("=" * 72)
    print(
        f"LITERATURE EXIT · lens watchlist · {args.start}..{args.end} · "
        f"stocks={len(universe)} · days={len(dates)} · sync_dd3={sync_n}"
    )
    print("  save<0 = early exit beats hold-to-close · slip 0.5%")

    results: dict = {"period": f"{args.start}..{args.end}", "universe_n": len(universe), "strategies": {}}

    for key, fn in STRATEGIES.items():
        saves, by_stock = collect_saves(conn, dates, universe, contexts, fn)
        ci = bootstrap_ci(saves, n_boot=args.bootstrap_n)
        ok = sum(1 for s in by_stock.values() if len(s) >= 3 and win_rate(s) > 0.5)
        tot = sum(1 for s in by_stock.values() if len(s) >= 3)

        print(f"\n【{key}】")
        print(summarize("overall", saves))
        if ci:
            print(
                f"    Bootstrap median CI [{ci['med_p2.5']:+.2f}%, {ci['med_p97.5']:+.2f}%] "
                f"P(save<0)={ci['p_exit_helps']:.1%} stable={stable_flag(ci)}"
            )
        print(f"    stocks win>50% (n≥3): {ok}/{tot}")

        # conditional: close <= -3%
        bad_saves = []
        for d in dates:
            ctx = contexts[d]
            for sid in universe:
                bars = load_bars(conn, sid, d)
                pc = prev_close(conn, sid, d)
                if len(bars) < MIN_BARS or not pc:
                    continue
                s = fn(conn, sid, d, ctx, bars, pc)
                if s is None:
                    continue
                cret = (float(bars[-1]["close"]) / pc - 1) * 100
                if cret <= -3:
                    bad_saves.append(s)
        if bad_saves:
            print(
                f"    when close≤-3%: n={len(bad_saves)} "
                f"win={win_rate(bad_saves):.1%} med={median(bad_saves):+.2f}%"
            )

        results["strategies"][key] = {
            "n": len(saves),
            "win_rate": round(win_rate(saves), 4) if saves else None,
            "median_save": round(median(saves), 4) if saves else None,
            "stable": stable_flag(ci),
            "stocks_win_gt_50pct": f"{ok}/{tot}",
        }

    if not args.no_walk_forward:
        print("\n" + "=" * 72)
        print("【Walk-forward】train 前 55% 日 · 選 train win>55% & n≥5 個股 · test 後 45%")
        wf_results = []
        for key in ("aratia_regime_3pct", "vcp_structural_stop"):
            wf = walk_forward(conn, dates, universe, contexts, key)
            wf_results.append(wf)
            print(f"\n  {key}:")
            print(f"    picked {wf['picked_n']} stocks: {', '.join(wf['picked_stocks'][:12])}", end="")
            if wf["picked_n"] > 12:
                print(f" … +{wf['picked_n'] - 12}", end="")
            print()
            tr, te = wf["train"], wf["test_picked"]
            print(
                f"    train: n={tr['n']} win={tr['win_rate']:.1%} med={tr['median_save']:+.2f}%"
                if tr["n"]
                else "    train: n=0"
            )
            print(
                f"    test (picked): n={te['n']} win={te['win_rate']:.1%} med={te['median_save']:+.2f}%"
                if te["n"]
                else "    test (picked): n=0"
            )
            ta = wf["test_all_universe"]
            print(
                f"    test (all lens): n={ta['n']} win={ta['win_rate']:.1%} "
                f"med={ta['median_save']:+.2f}%"
            )
            print(f"    overfit_risk={wf['overfit_risk']}")
        results["walk_forward"] = wf_results

    conn.close()

    if args.json:
        print("\n--- JSON ---")
        print(json.dumps(results, ensure_ascii=False, indent=2))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
