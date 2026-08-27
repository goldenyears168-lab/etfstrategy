"""item AM (wave 7) — does leading-dip's quality/coverage dual-track design transplant
onto dayflip-futures-short?

Reuses the 190-trade dayflip-short reconstruction from
reports/research/dayflip_revenue_momentum_filter/trades_with_revyoy.csv
(joined with reports/research/branch-footprint-screen/dayflip_gapup_short/all_trades.csv
for amt_yi/advshare/etc., same join key as item Y).

Read-only research script. Does not touch config/order.yaml, config/strategy.yaml,
or src/order/dayflip_short_*.py. Output under
reports/research/dayflip_dual_track_transplant/.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
from scipy import stats

ROOT = Path(__file__).resolve().parents[2]
REVYOY = ROOT / "reports/research/dayflip_revenue_momentum_filter/trades_with_revyoy.csv"
ALLTR = ROOT / "reports/research/branch-footprint-screen/dayflip_gapup_short/all_trades.csv"
OUTDIR = ROOT / "reports/research/dayflip_dual_track_transplant"
OUTDIR.mkdir(parents=True, exist_ok=True)


def block(df: pd.DataFrame, label: str) -> dict:
    n = len(df)
    win = float((df["pnl_pct"] > 0).mean() * 100)
    tp = float((df["pnl_pct"] >= 1.94).mean() * 100)  # hit the +1.95% take-profit
    mean = float(df["pnl_pct"].mean())
    med = float(df["pnl_pct"].median())
    days = int(df["signal_date"].nunique())
    return {
        "label": label,
        "n": n,
        "days": days,
        "mean_pnl_pct": round(mean, 4),
        "median_pnl_pct": round(med, 4),
        "win_rate_pct": round(win, 2),
        "tp_hit_rate_pct": round(tp, 2),
    }


def main() -> None:
    rv = pd.read_csv(REVYOY)
    at = pd.read_csv(ALLTR)
    rv["stock"] = rv["stock"].astype(str)
    at["stock"] = at["stock"].astype(str)
    m = rv.merge(
        at[["signal_date", "stock", "amt_yi", "advshare", "share_vol", "fut_adv", "rvol"]],
        on=["signal_date", "stock"],
        how="left",
    )
    m.to_csv(OUTDIR / "trades_quality_coverage_pool.csv", index=False)

    results: dict = {"n_total": len(m), "days_total": int(m["signal_date"].nunique()),
                      "stocks_total": int(m["stock"].nunique())}

    results["full_population"] = block(m, "full (190-trade candidate pool)")

    # --- tercile split by n_seats (populated for all 190) ---
    q1, q2 = m["n_seats"].quantile([1 / 3, 2 / 3])
    low = m[m["n_seats"] <= q1]
    mid = m[(m["n_seats"] > q1) & (m["n_seats"] <= q2)]
    high = m[m["n_seats"] > q2]
    results["n_seats_tercile"] = {
        "cuts": [float(q1), float(q2)],
        "low": block(low, "low n_seats"),
        "mid": block(mid, "mid n_seats"),
        "high_quality": block(high, "high n_seats (quality)"),
        "mwu_high_vs_low_p": float(stats.mannwhitneyu(high["pnl_pct"], low["pnl_pct"]).pvalue),
        "mwu_high_vs_full_p": float(stats.mannwhitneyu(high["pnl_pct"], m["pnl_pct"]).pvalue),
        "spearman_trade_level": {
            "rho": float(stats.spearmanr(m["n_seats"], m["pnl_pct"]).statistic),
            "p": float(stats.spearmanr(m["n_seats"], m["pnl_pct"]).pvalue),
        },
    }
    by_stock = m.groupby("stock").agg(n_seats_med=("n_seats", "median"), pnl_mean=("pnl_pct", "mean"))
    sp_stock = stats.spearmanr(by_stock["n_seats_med"], by_stock["pnl_mean"])
    results["n_seats_tercile"]["spearman_stock_level"] = {
        "n_stocks": int(len(by_stock)), "rho": float(sp_stock.statistic), "p": float(sp_stock.pvalue)
    }

    # --- tercile split by amt_yi (available on 170/190; missing = 20 post-freeze forward-test rows) ---
    sub = m.dropna(subset=["amt_yi"]).copy()
    q1a, q2a = sub["amt_yi"].quantile([1 / 3, 2 / 3])
    lo_a = sub[sub["amt_yi"] <= q1a]
    mid_a = sub[(sub["amt_yi"] > q1a) & (sub["amt_yi"] <= q2a)]
    hi_a = sub[sub["amt_yi"] > q2a]
    results["amt_yi_tercile"] = {
        "n_available": int(len(sub)),
        "cuts": [float(q1a), float(q2a)],
        "low": block(lo_a, "low amt_yi"),
        "mid": block(mid_a, "mid amt_yi"),
        "high_quality": block(hi_a, "high amt_yi (quality)"),
        "mwu_high_vs_low_p": float(stats.mannwhitneyu(hi_a["pnl_pct"], lo_a["pnl_pct"]).pvalue),
        "spearman": {
            "rho": float(stats.spearmanr(sub["amt_yi"], sub["pnl_pct"]).statistic),
            "p": float(stats.spearmanr(sub["amt_yi"], sub["pnl_pct"]).pvalue),
        },
    }

    # --- the decisive test: dayflip is already single-pick/day. Simulate swapping the
    # production single-pick rule (smallest qualifying gap) for a quality-track rule
    # (highest n_seats, or highest amt_yi) on the SAME candidate pool/days. ---
    def pick_by(df: pd.DataFrame, col: str, ascending: bool) -> pd.DataFrame:
        return df.sort_values(col, ascending=ascending).groupby("signal_date").head(1)

    current_pick = pick_by(m, "fgap", True)  # production rule: smallest qualifying gap
    quality_pick_seats = pick_by(m, "n_seats", False)  # quality-track candidate: most seats
    quality_pick_amt = pick_by(sub, "amt_yi", False)  # quality-track candidate: largest buy amount

    overlap = pd.merge(
        current_pick[["signal_date", "stock"]], quality_pick_seats[["signal_date", "stock"]],
        on=["signal_date", "stock"],
    )
    results["single_pick_swap_test"] = {
        "current_production_rule_min_fgap": block(current_pick, "current (min fgap) single-pick"),
        "quality_rule_max_n_seats": block(quality_pick_seats, "quality (max n_seats) single-pick"),
        "quality_rule_max_amt_yi": block(quality_pick_amt, "quality (max amt_yi) single-pick, amt_yi-days only"),
        "overlap_days_current_vs_quality_seats": int(len(overlap)),
        "overlap_pct": round(100 * len(overlap) / current_pick["signal_date"].nunique(), 1),
    }

    with open(OUTDIR / "summary.json", "w") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    print(json.dumps(results, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
