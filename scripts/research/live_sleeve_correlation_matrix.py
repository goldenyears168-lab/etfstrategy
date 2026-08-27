"""Cross-sleeve return correlation check — item K.

Question: leading-dip(+mid), dayflip-futures-short, tmf-micro-channel and
songshan-copytrade were each validated in isolation against their own
backtest. Nobody has checked whether their *realized* return streams
actually move together. If they do, the live book has hidden concentrated
risk even though each sleeve looks fine on its own.

This script reconstructs the longest available REAL-DATED return series for
each sleeve from existing research artifacts (no new backtests, no DB
access, read-only), aligns them on the calendar, and reports:
  - pairwise overlap n (trade-level exact-date overlap; honest even if 0-3)
  - pairwise Pearson correlation at monthly-aggregated granularity (the only
    granularity where all four have enough overlapping mass to be
    non-degenerate)
  - worst-month tail co-movement check for each sleeve

Sources used (see final report for caveats on how close each is to the
*currently* live spec):
  - leading-dip: reports/research/rrg/20260715_leading_dip_events.csv
    (structure-core event log behind the frozen `leading_dip` spec;
    entry date + T+3 exit date + px3 stock return %)
  - dayflip-futures-short: reports/research/branch-footprint-screen/
    dayflip_gapup_short/single_pick_tradelog.csv (single-pick-per-day trade
    log matching the live pick_rule=smallest_qualifying_gap /
    single_trade_per_day=true spec)
  - tmf-micro-channel: reports/research/channel_lab/ma2_baseline_daynets.json
    (continuous daily net-point series across 4 stitched windows,
    2025-07-01..2026-07-31, from the channel-lab baseline recipe lineage
    behind the adopted Final v1.4.0 spec)
  - songshan-copytrade: reports/research/branch-footprint-screen/
    ab58_xMega_copytrade/legs/consensus_solo_songshan_core_R_song_H7.csv
    (branch 9217 single-branch consensus legs, hold~7, matching order.yaml
    branch_id=9217 / hold_days=7)
"""
import csv
import json
import statistics
from collections import defaultdict
from pathlib import Path

ROOT = Path("/Users/jackm4/goldenstocks")
OUT_DIR = ROOT / "reports/research/live_sleeve_correlation_matrix"
OUT_DIR.mkdir(parents=True, exist_ok=True)


def load_leading_dip():
    path = ROOT / "reports/research/rrg/20260715_leading_dip_events.csv"
    rows = list(csv.DictReader(open(path)))
    trades = []
    for r in rows:
        trades.append({
            "entry_date": r["date"],
            "exit_date": r["exit_date"],
            "ret_pct": float(r["px3"]),
        })
    return trades


def load_dayflip():
    path = ROOT / "reports/research/branch-footprint-screen/dayflip_gapup_short/single_pick_tradelog.csv"
    rows = list(csv.DictReader(open(path)))
    trades = []
    for r in rows:
        trades.append({
            "entry_date": r["trade_date"],
            "exit_date": r["trade_date"],
            "ret_pct": float(r["pnl_pct"]),
        })
    return trades


def load_songshan():
    path = (
        ROOT
        / "reports/research/branch-footprint-screen/ab58_xMega_copytrade/legs/"
        "consensus_solo_songshan_core_R_song_H7.csv"
    )
    rows = list(csv.DictReader(open(path)))
    trades = []
    for r in rows:
        trades.append({
            "entry_date": r["entry_date"],
            "exit_date": r["exit_date"],
            "ret_pct": float(r["stock_pct"]),
        })
    return trades


def load_tmf_daynets():
    path = ROOT / "reports/research/channel_lab/ma2_baseline_daynets.json"
    d = json.load(open(path))
    daynets = {}
    for window, day_dict in d.items():
        for date, net in day_dict.items():
            daynets[date] = daynets.get(date, 0.0) + float(net)
    return daynets  # date -> net points (0 on no-trade days, real trading calendar)


def trades_to_daily(trades):
    """Attribute each trade's return to its exit_date (when P&L is booked).
    Multiple trades exiting same day are averaged (equal-weight sleeve)."""
    by_date = defaultdict(list)
    for t in trades:
        by_date[t["exit_date"]].append(t["ret_pct"])
    return {d: statistics.mean(v) for d, v in by_date.items()}


def month_of(date_str):
    return date_str[:7]


def to_monthly(daily_dict):
    by_month = defaultdict(list)
    for d, v in daily_dict.items():
        by_month[month_of(d)].append(v)
    return {m: sum(v) for m, v in by_month.items()}  # sum = total pts/pct booked that month


def pearson(xs, ys):
    n = len(xs)
    if n < 2:
        return None
    mx, my = statistics.mean(xs), statistics.mean(ys)
    sx = statistics.pstdev(xs)
    sy = statistics.pstdev(ys)
    if sx == 0 or sy == 0:
        return None
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / n
    return cov / (sx * sy)


def main():
    ld_trades = load_leading_dip()
    df_trades = load_dayflip()
    ss_trades = load_songshan()
    tmf_daily = load_tmf_daynets()

    ld_daily = trades_to_daily(ld_trades)
    df_daily = trades_to_daily(df_trades)
    ss_daily = trades_to_daily(ss_trades)

    series = {
        "leading-dip": ld_daily,
        "dayflip-futures-short": df_daily,
        "tmf-micro-channel": tmf_daily,
        "songshan-copytrade": ss_daily,
    }

    report = {"exact_date_overlap": {}, "monthly_corr": {}, "monthly_overlap_n": {},
              "date_range": {}, "worst_months": {}}

    for name, d in series.items():
        dates = sorted(d.keys())
        report["date_range"][name] = [dates[0], dates[-1], len(dates)]

    names = list(series.keys())
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a, b = names[i], names[j]
            common = sorted(set(series[a]) & set(series[b]))
            xs = [series[a][d] for d in common]
            ys = [series[b][d] for d in common]
            r_daily = pearson(xs, ys)
            report["exact_date_overlap"][f"{a}__{b}"] = {
                "n": len(common),
                "dates": common,
                "daily_pearson_r": r_daily,
            }

    monthly = {name: to_monthly(d) for name, d in series.items()}
    for name, m in monthly.items():
        report["worst_months"][name] = sorted(m.items(), key=lambda kv: kv[1])[:3]

    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a, b = names[i], names[j]
            common_m = sorted(set(monthly[a]) & set(monthly[b]))
            xs = [monthly[a][m] for m in common_m]
            ys = [monthly[b][m] for m in common_m]
            r = pearson(xs, ys)
            report["monthly_corr"][f"{a}__{b}"] = r
            report["monthly_overlap_n"][f"{a}__{b}"] = len(common_m)
            report["monthly_corr"][f"{a}__{b}_pairs"] = list(zip(common_m, xs, ys))

    # dump full monthly series for transparency
    report["monthly_series"] = monthly

    out_path = OUT_DIR / "correlation_report.json"
    with open(out_path, "w") as f:
        json.dump(report, f, indent=1, ensure_ascii=False, default=str)

    # print concise console summary
    print("=== date ranges (n events/trade-days) ===")
    for name, (d0, d1, n) in report["date_range"].items():
        print(f"{name:24s} {d0} .. {d1}  n={n}")

    print("\n=== exact-date overlap n + same-day Pearson r (trade-level) ===")
    for k, v in report["exact_date_overlap"].items():
        print(f"{k:55s} n={v['n']}  same_day_r={v['daily_pearson_r']}")

    print("\n=== monthly-aggregated Pearson correlation ===")
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a, b = names[i], names[j]
            r = report["monthly_corr"][f"{a}__{b}"]
            n = report["monthly_overlap_n"][f"{a}__{b}"]
            print(f"{a:24s} vs {b:24s} r={r} (n_months={n})")

    print("\n=== worst 3 months per sleeve (month, total ret) ===")
    for name, worst in report["worst_months"].items():
        print(name)
        for m, v in worst:
            row = {other: monthly[other].get(m) for other in names if other != name}
            print(f"   {m}: {v:.2f}   others_that_month={row}")

    print(f"\nfull JSON: {out_path}")


if __name__ == "__main__":
    main()
