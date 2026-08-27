"""Item AS — TMF + songshan two-asset budget-weight sweep.

Question: item K found tmf-micro-channel <-> songshan-copytrade have a
meaningfully NEGATIVE correlation (monthly -0.36, same-day-exact -0.59,
n=10/30). Item X's fixed-budget-proxy scheme already implicitly overweights
songshan vs TMF (songshan has a real budget_twd=100000 in config/order.yaml;
TMF has NO stated TWD budget, only max_lots=1 — item X proxied TMF at an
EXTERNAL, non-repo estimate of NT$12,000-40,000 margin/lot). Neither the
budget-migration doc (docs/songshan-copytrade-budget-migration.md) nor
order.yaml's TMF max_lots=1 cap reference TMF's return series or the
cross-sleeve correlation at all — songshan's budget_twd=100000 was set purely
from songshan's own historical trade sizing conventions (see doc: "改動：
Songshan 跟單策略從固定 1 張整股改為約 10 萬台幣預算制"), with zero mention of
portfolio-level diversification.

This script reuses the exact same source CSVs/JSON as item K/X (read-only,
no new backtests, no DB access) to build TMF-only and songshan-only daily
return series on THEIR OWN allocated capital, then sweeps a two-asset
(TMF + songshan only) weight from songshan=10%..90% to see whether a
different split than what current budgets imply would improve combined
Sharpe/vol/maxDD, exploiting the negative correlation.
"""
import csv
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path

ROOT = Path("/Users/jackm4/goldenstocks")
OUT_DIR = ROOT / "reports/research/songshan_budget_diversification"
OUT_DIR.mkdir(parents=True, exist_ok=True)

TMF_POINT_VALUE_NTD = 10.0  # src/tmf_channel/blotter.py:TMF_POINT_VALUE_NTD
TMF_MARGIN_SCENARIOS = {
    "margin_12k": 12000.0,  # item X base case (external estimate, not in order.yaml)
    "margin_40k": 40000.0,  # item X robustness check
}

WINDOW_START = "2025-07-01"
WINDOW_END = "2026-07-01"


def load_songshan():
    path = (
        ROOT
        / "reports/research/branch-footprint-screen/ab58_xMega_copytrade/legs/"
        "consensus_solo_songshan_core_R_song_H7.csv"
    )
    rows = list(csv.DictReader(open(path)))
    return [{"exit_date": r["exit_date"], "ret_pct": float(r["stock_pct"])} for r in rows]


def load_tmf_daynets():
    path = ROOT / "reports/research/channel_lab/ma2_baseline_daynets.json"
    d = json.load(open(path))
    daynets = {}
    for _window, day_dict in d.items():
        for date, net in day_dict.items():
            daynets[date] = daynets.get(date, 0.0) + float(net)
    return daynets


def trades_to_daily_pct(trades):
    by_date = defaultdict(list)
    for t in trades:
        by_date[t["exit_date"]].append(t["ret_pct"])
    return {d: statistics.mean(v) for d, v in by_date.items()}


def build_calendar(tmf_daynets):
    return sorted(d for d in tmf_daynets if WINDOW_START <= d <= WINDOW_END)


def aligned_return_series(daily_pct_dict, calendar):
    return [daily_pct_dict.get(d, 0.0) / 100.0 for d in calendar]


def aligned_tmf_return_series(tmf_daynets_pts, calendar, margin_ntd):
    return [tmf_daynets_pts.get(d, 0.0) * TMF_POINT_VALUE_NTD / margin_ntd for d in calendar]


def pstdev(xs):
    return statistics.pstdev(xs) if len(xs) > 1 else 0.0


def sharpe(xs, periods_per_year=252):
    if not xs:
        return None
    mu = statistics.mean(xs)
    sd = pstdev(xs)
    if sd == 0:
        return None
    return (mu / sd) * math.sqrt(periods_per_year)


def max_drawdown(xs):
    cum = 0.0
    peak = 0.0
    mdd = 0.0
    for x in xs:
        cum += x
        peak = max(peak, cum)
        mdd = min(mdd, cum - peak)
    return mdd


def sample_pearson(xs, ys):
    n = len(xs)
    if n < 2:
        return None
    mx, my = statistics.mean(xs), statistics.mean(ys)
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if sx == 0 or sy == 0:
        return None
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return cov / (sx * sy)


def main():
    ss_trades = load_songshan()
    tmf_daynets = load_tmf_daynets()
    ss_daily_pct = trades_to_daily_pct(ss_trades)
    calendar = build_calendar(tmf_daynets)

    ss_series = aligned_return_series(ss_daily_pct, calendar)

    report = {
        "window": {"start": WINDOW_START, "end": WINDOW_END, "n_calendar_days": len(calendar)},
        "current_budget_implied_weight": {},
        "sweep": {},
    }

    for scenario_name, margin in TMF_MARGIN_SCENARIOS.items():
        tmf_series = aligned_tmf_return_series(tmf_daynets, calendar, margin)

        n_days = len(calendar)
        daily_corr = sample_pearson(tmf_series, ss_series)

        # current budget-migration-implied weight: songshan has a real
        # budget_twd=100000 in order.yaml; TMF has none (max_lots=1 only) —
        # proxy TMF capital at this scenario's external margin estimate.
        songshan_budget = 100000.0
        implied_songshan_w = songshan_budget / (songshan_budget + margin)
        report["current_budget_implied_weight"][scenario_name] = {
            "tmf_margin_assumed": margin,
            "songshan_budget_twd": songshan_budget,
            "implied_songshan_weight": implied_songshan_w,
            "implied_tmf_weight": 1.0 - implied_songshan_w,
        }

        sweep_results = []
        for w_pct in range(10, 91, 10):
            w_ss = w_pct / 100.0
            w_tmf = 1.0 - w_ss
            combined = [w_tmf * tmf_series[t] + w_ss * ss_series[t] for t in range(n_days)]
            sweep_results.append(
                {
                    "songshan_weight": w_ss,
                    "tmf_weight": w_tmf,
                    "annualized_sharpe": sharpe(combined),
                    "annualized_vol": pstdev(combined) * math.sqrt(252),
                    "mean_daily_return": statistics.mean(combined),
                    "max_drawdown": max_drawdown(combined),
                    "cumulative_return": sum(combined),
                }
            )

        best = max(
            (r for r in sweep_results if r["annualized_sharpe"] is not None),
            key=lambda r: r["annualized_sharpe"],
        )

        report["sweep"][scenario_name] = {
            "daily_pearson_r_tmf_songshan": daily_corr,
            "tmf_standalone_sharpe": sharpe(tmf_series),
            "songshan_standalone_sharpe": sharpe(ss_series),
            "results": sweep_results,
            "best_by_sharpe": best,
        }

    out_path = OUT_DIR / "tmf_songshan_sweep_report.json"
    json.dump(report, open(out_path, "w"), indent=2, ensure_ascii=False)
    print(f"wrote {out_path}")
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
