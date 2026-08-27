"""Risk-parity vs equal-weight vs fixed-budget sleeve allocation — item X.

Question: item K (live_sleeve_correlation_matrix) found near-zero same-day
correlation between the equity sleeves and a meaningfully NEGATIVE
tmf-micro-channel <-> songshan-copytrade correlation. Nobody has asked
whether the current ad hoc fixed-budget allocation (config/order.yaml:
leading-dip 20k/entry, songshan 100k, dayflip-short 300k margin cap, TMF
max_lots=1 with no stated TWD budget) is actually a sensible way to size
these sleeves relative to each other, versus a risk-parity (inverse-vol)
or naive equal-weight scheme.

Read-only: reuses the exact same source CSVs/JSON that item K already
validated as real-dated trade logs (no new backtests, no DB access).

Method:
  1. Build each sleeve's daily % return series (return on ITS OWN allocated
     capital, attributed to trade exit_date; 0 on no-trade days) over the
     overlapping trading-day calendar [2025-07-01, 2026-07-01] — the
     intersection of all four sleeves' available history, bounded by TMF's
     start (2025-07-01) and dayflip-short's end (2026-07-01).
  2. Compute each sleeve's daily-return volatility (population stdev) over
     that calendar.
  3. Three weighting schemes (weights sum to 1, applied to a shared notional
     capital C so portfolio_return(t) = sum_i w_i * r_i(t)):
       - equal-weight: 0.25 each
       - risk-parity: w_i proportional to 1/vol_i
       - fixed-budget proxy: w_i proportional to each sleeve's stated
         order.yaml capital figure (leading-dip budget_twd=20000; songshan
         budget_twd=100000; dayflip-short margin_cap_twd=300000 [a hard
         ceiling, not typical utilization — likely overstates its true
         share]; TMF has NO stated TWD budget in order.yaml [only
         max_lots=1] — proxied here with an EXTERNAL, non-repo-sourced
         estimate of NT$12,000 typical micro-TAIEX-futures original margin
         per lot; this number is an assumption, not a repo fact).
  4. Simulate each scheme's combined daily return series, report annualized
     Sharpe (mean/std * sqrt(252)), annualized vol (std * sqrt(252)), and
     max drawdown (on cumulative additive return curve — NOT compounded,
     appropriate given the small daily magnitudes here).

Honesty caveat (inherited from item K): exact-date overlap between sleeve
pairs is 8-30 trades. Any correlation / vol estimate built on that is noisy;
a risk-parity scheme derived from it inherits that fragility. This script
reports numbers, not a recommendation to reallocate capital.
"""
import csv
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path

ROOT = Path("/Users/jackm4/goldenstocks")
OUT_DIR = ROOT / "reports/research/risk_parity_allocation"
OUT_DIR.mkdir(parents=True, exist_ok=True)

TMF_POINT_VALUE_NTD = 10.0  # src/tmf_channel/blotter.py:TMF_POINT_VALUE_NTD
TMF_ASSUMED_MARGIN_NTD = 12000.0  # EXTERNAL estimate, not in repo config — see docstring

# Fixed-budget proxy sourced from config/order.yaml (read-only reference)
BUDGET_TWD = {
    "leading-dip": 20000.0,          # order.yaml:56 budget_twd (per entry)
    "songshan-copytrade": 100000.0,  # order.yaml:107 budget_twd
    "dayflip-futures-short": 300000.0,  # order.yaml:139 margin_cap_twd (hard ceiling, not avg utilization)
    "tmf-micro-channel": TMF_ASSUMED_MARGIN_NTD,  # NOT in order.yaml — external estimate
}

WINDOW_START = "2025-07-01"
WINDOW_END = "2026-07-01"


def load_leading_dip():
    path = ROOT / "reports/research/rrg/20260715_leading_dip_events.csv"
    rows = list(csv.DictReader(open(path)))
    return [{"exit_date": r["exit_date"], "ret_pct": float(r["px3"])} for r in rows]


def load_dayflip():
    path = ROOT / "reports/research/branch-footprint-screen/dayflip_gapup_short/single_pick_tradelog.csv"
    rows = list(csv.DictReader(open(path)))
    return [{"exit_date": r["trade_date"], "ret_pct": float(r["pnl_pct"])} for r in rows]


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
    return daynets  # date -> net points


def trades_to_daily_pct(trades):
    by_date = defaultdict(list)
    for t in trades:
        by_date[t["exit_date"]].append(t["ret_pct"])
    return {d: statistics.mean(v) for d, v in by_date.items()}  # pct points, e.g. 1.95 == 1.95%


def build_calendar(tmf_daynets):
    return sorted(d for d in tmf_daynets if WINDOW_START <= d <= WINDOW_END)


def aligned_return_series(daily_pct_dict, calendar):
    """Return list of decimal returns (0.0195 == 1.95%) aligned to calendar, 0 on no-trade days."""
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


def normalize(d):
    total = sum(d.values())
    return {k: v / total for k, v in d.items()}


def main():
    ld_trades = load_leading_dip()
    df_trades = load_dayflip()
    ss_trades = load_songshan()
    tmf_daynets = load_tmf_daynets()

    ld_daily_pct = trades_to_daily_pct(ld_trades)
    df_daily_pct = trades_to_daily_pct(df_trades)
    ss_daily_pct = trades_to_daily_pct(ss_trades)

    calendar = build_calendar(tmf_daynets)

    series = {
        "leading-dip": aligned_return_series(ld_daily_pct, calendar),
        "dayflip-futures-short": aligned_return_series(df_daily_pct, calendar),
        "songshan-copytrade": aligned_return_series(ss_daily_pct, calendar),
        "tmf-micro-channel": aligned_tmf_return_series(tmf_daynets, calendar, TMF_ASSUMED_MARGIN_NTD),
    }

    sleeve_stats = {}
    for name, xs in series.items():
        n_active = sum(1 for x in xs if x != 0.0)
        sleeve_stats[name] = {
            "n_calendar_days": len(xs),
            "n_active_days": n_active,
            "daily_vol_stdev": pstdev(xs),
            "annualized_vol": pstdev(xs) * math.sqrt(252),
            "mean_daily_return": statistics.mean(xs),
            "own_sharpe": sharpe(xs),
        }

    inv_vol = {}
    for name, s in sleeve_stats.items():
        v = s["daily_vol_stdev"]
        inv_vol[name] = (1.0 / v) if v > 0 else 0.0
    risk_parity_w = normalize(inv_vol)

    equal_w = {name: 0.25 for name in series}

    fixed_budget_w = normalize(BUDGET_TWD)

    schemes = {
        "equal_weight": equal_w,
        "risk_parity": risk_parity_w,
        "fixed_budget_proxy": fixed_budget_w,
    }

    names = list(series.keys())
    n_days = len(calendar)
    portfolio_results = {}
    for scheme_name, weights in schemes.items():
        combined = []
        for t in range(n_days):
            combined.append(sum(weights[name] * series[name][t] for name in names))
        portfolio_results[scheme_name] = {
            "weights": weights,
            "annualized_sharpe": sharpe(combined),
            "daily_vol_stdev": pstdev(combined),
            "annualized_vol": pstdev(combined) * math.sqrt(252),
            "mean_daily_return": statistics.mean(combined),
            "max_drawdown": max_drawdown(combined),
            "cumulative_return": sum(combined),
        }

    report = {
        "window": {"start": WINDOW_START, "end": WINDOW_END, "n_calendar_days": n_days},
        "assumptions": {
            "tmf_assumed_margin_ntd_per_lot": TMF_ASSUMED_MARGIN_NTD,
            "tmf_assumed_margin_source": "external estimate, NOT in config/order.yaml (order.yaml only states max_lots=1)",
            "dayflip_budget_source": "order.yaml margin_cap_twd=300000 is a hard ceiling, not average utilization — likely overstates fixed-budget share",
            "return_combination": "portfolio_return(t) = sum_i weight_i * sleeve_i_own_capital_return(t); additive (non-compounded) cumulative curve for max_drawdown",
        },
        "sleeve_stats": sleeve_stats,
        "schemes": portfolio_results,
    }
    out_path = OUT_DIR / "risk_parity_report.json"
    json.dump(report, open(out_path, "w"), indent=2, ensure_ascii=False)
    print(f"wrote {out_path}")
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
