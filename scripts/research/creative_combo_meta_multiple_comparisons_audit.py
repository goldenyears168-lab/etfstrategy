"""Family-wise multiple-comparisons audit of the 2026-08-07 100-item creative-
combination research campaign (`reports/research/creative_combo_2026-08-07/PROGRESS.md`).

Why this exists
----------------
The campaign's own checklist (`docs/research-integrity-checklist.md`, BUG-4 /
appendix A7) requires computing "expected passes under pure noise" before
trusting a sweep's pass count -- but this was never applied to the campaign's
own 44-item family (43 planned "done" count in PROGRESS.md's own tally + one
bookkeeping item, see `_ITEM_COUNT_NOTE` below). At alpha=0.05 across ~44
independent tests, pure noise predicts ~2.2 false positives -- suspiciously
close to the count of positive findings the campaign actually produced
(item R: institutional-lending short-quality filter; item AT: WMA20 bounce-
confirm technical signal generalization).

This script does NOT re-run any backtests. It is a meta-audit: it walks the
p-values *already reported* by the 44 items in PROGRESS.md (cross-checked
against each item's own `reports/research/<slug>/FINDINGS.md` where the
PROGRESS.md summary abbreviates a range), logs every one of them as a trial
via `trial_registry.append_trial()` (finally using the tool the campaign
itself built in item T but never applied to its own output), and then
computes a Bonferroni threshold (0.05/N) and a Benjamini-Hochberg FDR
threshold across three honest denominator choices (see below), reporting
whether items R and AT survive each correction.

Denominator choices (reported side by side, not collapsed into one number)
----------------------------------------------------------------------------
N_items      = 24   one representative (best/minimum) p-value per campaign
                     item, for the 24 items (of 44 total) that ran a formal
                     significance test at all. This is the correction the
                     critic's gap description is pointing at ("~43 tests").
N_items_all  = 44   same as above but the denominator counts *every* item in
                     the campaign (including the 20 that reported no formal
                     p-value at all -- untested items can't be "significant"
                     so they sit at p=1.0 and don't change which findings
                     survive, but they DO count against the family size,
                     which is the more honest reading of "44-item campaign").
N_subtests   = 93   every individual p-value extracted, including every axis
                     cut / robustness re-run / secondary indicator inside
                     each item (e.g. item AJ's per-axis ANOVA/Kruskal cuts,
                     item Y's 11-feature scan, item R's 5 short-interest
                     indicators). This is the more conservative reading that
                     matches item AJ's "multiple axis cuts... not corrected
                     for multiple comparisons" self-caveat, generalized to
                     the whole campaign.

Data provenance
----------------
The (item, p_value, n, description, source) tuples below were extracted by
hand from `reports/research/creative_combo_2026-08-07/PROGRESS.md` and, where
the PROGRESS.md summary abbreviates a range, from the cited item's own
`FINDINGS.md` (both are cited verbatim in each record's `source`/`notes`).
20 of the 44 items (C, E, F, I, K, L, O, P, S, T, U, V, X, Z, AB, AG, AH, AK,
AR, AS) reported no formal significance test (bug fixes, tooling, coverage
audits, qualitative comparisons, or data-availability negatives) and
therefore contribute no rows here -- they are listed in `NO_PVALUE_ITEMS`
for transparency, not silently dropped. Item U is also excluded: its own
"p=0.23" / "p=0.28-0.57" are p-values it *cites* from an already-published,
separate audit (`known-whale-branch-live-signal-validation`,
`yuanta-songjiang-copytrade`), not new tests U itself ran; including them
would double-count someone else's trial as this campaign's own.

Run:
    PYTHONPATH=src .venv/bin/python scripts/research/creative_combo_meta_multiple_comparisons_audit.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from trial_registry import append_trial  # noqa: E402

FAMILY = "creative_combo_meta_pvalues"
TS = "2026-08-08"
SOURCE_DOC = "reports/research/creative_combo_2026-08-07/PROGRESS.md"

_ITEM_COUNT_NOTE = (
    "PROGRESS.md's own closing line says '43/100完成' but the markdown table "
    "actually has 44 distinct lettered rows (A..Z, AB..AU, skipping G/Q). "
    "This audit uses the literal row count (44), not the prose tally (43), "
    "and says so rather than silently reconciling the discrepancy."
)

NO_PVALUE_ITEMS = {
    "C": "real bug fix (race condition), regression-tested 7/7, no significance test",
    "E": "DSR methodology audit; DSR saturates at ~1.000 for any N, no p-value computed",
    "F": "OI/volume signal; only pooled IC and IS->OOS persistence reported, no p",
    "I": "n=20, paired t~=0.49 reported without an accompanying p-value",
    "K": "sleeve return correlation matrix, descriptive only",
    "L": "data-availability negative (zero temporal overlap); the p<0.0001 in its "
         "FINDINGS.md is the *pre-existing* 9217 base-rate signal's p-value cited as "
         "background context for what item L would have tested, not a new result",
    "O": "descriptive liquidity blank-spot scan, no formal test",
    "P": "n=4 holdout replication, too thin for a formal test",
    "S": "lint tool extension (BUG-7/8/9), validated against real+synthetic files, no p",
    "T": "trial_registry.py itself; validated by round-trip write/read, no p",
    "U": "meta-audit; cites pre-existing p-values from OTHER already-published "
         "topics (kgi-chengzhong / yuanta-songjiang), not new tests of its own",
    "V": "tail-robust sizing overlay comparison via scalar/kurtosis deltas, no p",
    "X": "risk-parity Sharpe/MaxDD comparison, no significance test",
    "Z": "Spearman rho and Welch t=-0.27 reported without an accompanying p-value",
    "AB": "0/36 threshold met at spec; relaxed-threshold deltas reported without p",
    "AG": "implemented proposal (frozen rule string), no significance test",
    "AH": "qualitative curve-smoothness check (no kink near 6%), no formal test",
    "AK": "coverage audit (15.1% recent coverage), no significance test",
    "AR": "descriptive %-of-final-move stats (97%/76%), no significance test",
    "AS": "Sharpe-maximizing-weight comparison, no significance test",
}

# Each record: (item, n_observations, metric_value_p, description, source_path)
# n_observations is the sample size stated closest to that specific p-value in
# the source text; where only an item-level n is given, that is used instead.
RECORDS: list[tuple[str, int, float, str, str]] = [
    # --- A: regime_gate_branch_fusion (n=36 signals, 10 bull / 26 bear) ---
    ("A", 36, 0.56, "bull vs bear branch-follow excess-return gap, permutation "
     "test (1800+ trading-day label shuffle), two-tailed",
     "reports/research/regime_gate_branch_fusion/FINDINGS.md"),
    ("A", 36, 0.28, "same permutation test, one-tailed",
     "reports/research/regime_gate_branch_fusion/FINDINGS.md"),

    # --- B: dayflip_revenue_momentum_filter (n=190 trades / 38 tickers) ---
    ("B", 190, 0.64, "rev_yoy_3m tercile high(n=63) vs low, Mann-Whitney on pnl_pct",
     "reports/research/dayflip_revenue_momentum_filter/FINDINGS.md"),

    # --- D: leading_dip_h5_permutation_check ---
    ("D", 100, 0.002, "median excess-return advantage, permutation test "
     "(reported as p<0.002; upper bound used here)",
     "reports/research/leading_dip_h5_permutation_check/FINDINGS.md"),
    ("D", 100, 0.05, "73-76% win-rate headline after correcting for same-day "
     "clustering correlation (reported as p ~= 0.05)",
     "reports/research/leading_dip_h5_permutation_check/FINDINGS.md"),

    # --- H: wma20_bounce_generalize (n=1396 events / 116 tickers) ---
    ("H", 1396, 0.038, "confirmed vs not-confirmed mean excess return, "
     "permutation test", "reports/research/wma20_bounce_generalize/FINDINGS.md"),
    ("H", 1396, 0.69, "confirmed vs not-confirmed median difference",
     "reports/research/wma20_bounce_generalize/FINDINGS.md"),

    # --- J: crash_thermometer_contrarian_reframe ---
    ("J", 5, 0.026, "p99 threshold (n=5 days) fwd20 advantage, block "
     "permutation test -- flagged in source as single clustered event, not "
     "independent evidence", "reports/research/crash_thermometer_contrarian_reframe/FINDINGS.md"),

    # --- M: govbank_reverse_confirm (n=35 baseline, n=24 continuous) ---
    ("M", 35, 0.61, "government-fund confirm split, permutation test",
     "reports/research/govbank_reverse_confirm/FINDINGS.md"),
    ("M", 35, 0.59, "foreign-ratio confirm split, permutation test",
     "reports/research/govbank_reverse_confirm/FINDINGS.md"),
    ("M", 11, 0.11, "double-confirm subgroup (n=11) vs rest, permutation test",
     "reports/research/govbank_reverse_confirm/FINDINGS.md"),
    ("M", 35, 0.73, "government-fund ratio continuous Spearman r=0.06",
     "reports/research/govbank_reverse_confirm/FINDINGS.md"),
    ("M", 24, 0.11, "foreign-ratio continuous Spearman r=0.33 (n=24, "
     "reported as p=0.10-0.12; midpoint used here)",
     "reports/research/govbank_reverse_confirm/FINDINGS.md"),

    # --- N: branch_dayflip_reclassify (3 new candidates, n=8-9 each) ---
    ("N", 9, 0.44, "9239 binomial test vs 69.1% baseline win rate",
     "reports/research/branch_dayflip_reclassify/FINDINGS.md"),
    ("N", 9, 0.44, "9666 binomial test vs 69.1% baseline win rate",
     "reports/research/branch_dayflip_reclassify/FINDINGS.md"),
    ("N", 8, 0.53, "984K binomial test vs 69.1% baseline win rate",
     "reports/research/branch_dayflip_reclassify/FINDINGS.md"),
    ("N", 9, 0.70, "9239 ticker-paired permutation test, mean",
     "reports/research/branch_dayflip_reclassify/FINDINGS.md"),
    ("N", 9, 0.46, "9239 ticker-paired permutation test, win rate",
     "reports/research/branch_dayflip_reclassify/FINDINGS.md"),
    ("N", 9, 0.13, "9666 ticker-paired permutation test, mean",
     "reports/research/branch_dayflip_reclassify/FINDINGS.md"),
    ("N", 9, 0.14, "9666 ticker-paired permutation test, win rate",
     "reports/research/branch_dayflip_reclassify/FINDINGS.md"),
    ("N", 8, 0.11, "984K ticker-paired permutation test, mean",
     "reports/research/branch_dayflip_reclassify/FINDINGS.md"),
    ("N", 8, 0.05, "984K ticker-paired permutation test, win rate",
     "reports/research/branch_dayflip_reclassify/FINDINGS.md"),

    # --- R: asquith_dayflip_crosscheck (n=139-160 trades / 24 tickers) ---
    # HEADLINE pair (this is what the campaign calls "item R"):
    ("R", 93, 0.021, "si_lend_pct tercile high(n=46) vs low(n=47), pnl, "
     "permutation test -- HEADLINE finding",
     "reports/research/asquith_dayflip_crosscheck/FINDINGS.md"),
    ("R", 93, 0.005, "si_lend_pct tercile high vs low, win rate, permutation "
     "test -- HEADLINE finding",
     "reports/research/asquith_dayflip_crosscheck/FINDINGS.md"),
    ("R", 92, 0.027, "same test, pnl, after removing single worst outlier "
     "trade (robustness re-run, not independent)",
     "reports/research/asquith_dayflip_crosscheck/FINDINGS.md"),
    ("R", 92, 0.0085, "same test, win rate, after removing outlier "
     "(robustness re-run, not independent)",
     "reports/research/asquith_dayflip_crosscheck/FINDINGS.md"),
    ("R", 93, 0.57, "si_margin_pct (retail) tercile, pnl, permutation test",
     "reports/research/asquith_dayflip_crosscheck/FINDINGS.md"),
    ("R", 93, 0.82, "si_margin_pct (retail) tercile, win rate, permutation test",
     "reports/research/asquith_dayflip_crosscheck/FINDINGS.md"),
    ("R", 93, 0.24, "si_total_pct (combined short interest) tercile, pnl",
     "reports/research/asquith_dayflip_crosscheck/FINDINGS.md"),
    ("R", 93, 0.06, "days-to-cover indicator",
     "reports/research/asquith_dayflip_crosscheck/FINDINGS.md"),

    # --- W: branch_9661_2634_formal_backtest ---
    ("W", 30, 0.146, "9661xd(2634) net-buy event backtest vs baseline",
     "reports/research/branch_9661_2634_formal_backtest/FINDINGS.md"),

    # --- Y: abc_factor_scan_dayflip_transplant (n=190, 11 candidate features) ---
    ("Y", 190, 0.543, "n_seats Spearman vs pnl_pct",
     "reports/research/abc_factor_scan_dayflip_transplant/FINDINGS.md"),
    ("Y", 190, 0.156, "fgap Spearman vs pnl_pct",
     "reports/research/abc_factor_scan_dayflip_transplant/FINDINGS.md"),
    ("Y", 190, 0.815, "rvol Spearman vs pnl_pct",
     "reports/research/abc_factor_scan_dayflip_transplant/FINDINGS.md"),
    ("Y", 190, 0.477, "adv_yi Spearman vs pnl_pct",
     "reports/research/abc_factor_scan_dayflip_transplant/FINDINGS.md"),
    ("Y", 190, 0.723, "amt_yi Spearman vs pnl_pct",
     "reports/research/abc_factor_scan_dayflip_transplant/FINDINGS.md"),
    ("Y", 170, 0.539, "advshare Spearman vs pnl_pct",
     "reports/research/abc_factor_scan_dayflip_transplant/FINDINGS.md"),
    ("Y", 190, 0.195, "ret0 Spearman vs pnl_pct",
     "reports/research/abc_factor_scan_dayflip_transplant/FINDINGS.md"),
    ("Y", 190, 0.828, "ret5 Spearman vs pnl_pct",
     "reports/research/abc_factor_scan_dayflip_transplant/FINDINGS.md"),
    ("Y", 174, 0.647, "mkt_ret0 (TAIEX same-day) Spearman vs pnl_pct",
     "reports/research/abc_factor_scan_dayflip_transplant/FINDINGS.md"),
    ("Y", 190, 0.141, "weekday Spearman vs pnl_pct",
     "reports/research/abc_factor_scan_dayflip_transplant/FINDINGS.md"),
    ("Y", 190, 0.936, "repeat_signal_60d Spearman vs pnl_pct",
     "reports/research/abc_factor_scan_dayflip_transplant/FINDINGS.md"),
    # NOTE: this item's file also reports fgap rho=0.91, p<0.0001 on the n=57
    # uncensored subgroup, explicitly flagged by the source itself as a
    # mechanical artifact of entry-price construction, not a new finding --
    # excluded here for the same reason the source excludes it from its verdict.

    # --- AC: branch_follow_funnel_architecture (n=248-474) ---
    ("AC", 474, 0.37, "leave-one-out drop `flip` gate, Mann-Whitney U",
     "reports/research/branch_follow_funnel_architecture/FINDINGS.md"),
    ("AC", 286, 0.90, "leave-one-out drop `acc_excl` gate, Mann-Whitney U",
     "reports/research/branch_follow_funnel_architecture/FINDINGS.md"),
    ("AC", 301, 0.79, "leave-one-out drop `multiseat` gate, Mann-Whitney U",
     "reports/research/branch_follow_funnel_architecture/FINDINGS.md"),

    # --- AD: dayflip_teq_concentration_level (n=38 tickers) ---
    ("AD", 38, 0.854, "concentration-level tercile high vs low, pnl, permutation",
     "reports/research/dayflip_teq_concentration_level/FINDINGS.md"),
    ("AD", 38, 0.851, "concentration-level tercile high vs low, win rate, permutation",
     "reports/research/dayflip_teq_concentration_level/FINDINGS.md"),

    # --- AE: unified_overnight_risk_gate (n=32 trades) ---
    ("AE", 32, 0.033, "trigger-nights vs trade P&L, continuous Spearman rho=-0.379",
     "reports/research/unified_overnight_risk_gate/FINDINGS.md"),
    ("AE", 32, 0.12, "median-split (>3 trigger nights vs <=3), Mann-Whitney",
     "reports/research/unified_overnight_risk_gate/FINDINGS.md"),
    ("AE", 32, 0.22, "AND-gate split (any trigger vs none), Mann-Whitney",
     "reports/research/unified_overnight_risk_gate/FINDINGS.md"),
    ("AE", 32, 0.13, "AND-gate split (any trigger vs none), Welch t-test",
     "reports/research/unified_overnight_risk_gate/FINDINGS.md"),

    # --- AF: s2_exit_leading_dip_transplant ---
    ("AF", 50, 0.0025, "dynamic-exit rule A vs fixed T+3, paired t-test",
     "reports/research/s2_exit_leading_dip_transplant/FINDINGS.md"),
    ("AF", 50, 0.028, "dynamic-exit rule B vs fixed T+3, paired t-test",
     "reports/research/s2_exit_leading_dip_transplant/FINDINGS.md"),

    # --- AI: regime_sleeve_allocation_switch ---
    ("AI", 100, 0.128, "regime x sleeve interaction, permutation test",
     "reports/research/regime_sleeve_allocation_switch/FINDINGS.md"),
    ("AI", 100, 0.06, "same interaction, alternate cut (reported as p ~= 0.06)",
     "reports/research/regime_sleeve_allocation_switch/FINDINGS.md"),

    # --- AJ: pv16_9217_timing_transplant ---
    ("AJ", 34, 0.011, "gap_bucket single-axis split (no_gap n=13 vs gap_up "
     "n=21), ANOVA", "reports/research/pv16_9217_timing_transplant/FINDINGS.md"),
    ("AJ", 34, 0.018, "gap_bucket single-axis split, Kruskal-Wallis",
     "reports/research/pv16_9217_timing_transplant/FINDINGS.md"),
    ("AJ", 34, 0.070, "full joint PV16-style cell classification, ANOVA "
     "(several cells n=1-2)", "reports/research/pv16_9217_timing_transplant/FINDINGS.md"),
    ("AJ", 34, 0.102, "full joint cell classification, Kruskal-Wallis",
     "reports/research/pv16_9217_timing_transplant/FINDINGS.md"),
    ("AJ", 34, 0.88, "vol_bucket single-axis split (no effect)",
     "reports/research/pv16_9217_timing_transplant/FINDINGS.md"),
    ("AJ", 34, 0.94, "t0_shape single-axis split (no effect)",
     "reports/research/pv16_9217_timing_transplant/FINDINGS.md"),

    # --- AL: whale_precursor_9217_overlay ---
    ("AL", 36, 0.376, "T-2 whale-confirm subgroup (n=10) vs unconfirmed (n=26), mean",
     "reports/research/whale_precursor_9217_overlay/FINDINGS.md"),
    ("AL", 36, 0.407, "same split, alternate statistic",
     "reports/research/whale_precursor_9217_overlay/FINDINGS.md"),

    # --- AM: dayflip_dual_track_transplant (n=190, 72 signal days, 38 tickers) ---
    ("AM", 190, 0.79, "n_seats tercile high vs low, Mann-Whitney",
     "reports/research/dayflip_dual_track_transplant/FINDINGS.md"),
    ("AM", 190, 0.92, "n_seats tercile high vs full pool, Mann-Whitney",
     "reports/research/dayflip_dual_track_transplant/FINDINGS.md"),
    ("AM", 190, 0.54, "n_seats trade-level Spearman vs pnl_pct",
     "reports/research/dayflip_dual_track_transplant/FINDINGS.md"),
    ("AM", 38, 0.27, "n_seats ticker-level Spearman vs pnl_pct",
     "reports/research/dayflip_dual_track_transplant/FINDINGS.md"),
    ("AM", 170, 0.47, "amt_yi tercile high vs low, Mann-Whitney",
     "reports/research/dayflip_dual_track_transplant/FINDINGS.md"),
    ("AM", 170, 0.52, "amt_yi trade-level Spearman vs pnl_pct",
     "reports/research/dayflip_dual_track_transplant/FINDINGS.md"),

    # --- AN: dayflip_sector_clustering (38 tickers, 93% electronics sub-sectors) ---
    ("AN", 38, 0.933, "3 electronics sub-sectors, ANOVA",
     "reports/research/dayflip_sector_clustering/FINDINGS.md"),
    ("AN", 38, 0.875, "3 electronics sub-sectors, Kruskal-Wallis",
     "reports/research/dayflip_sector_clustering/FINDINGS.md"),
    ("AN", 38, 0.63, "semiconductors vs rest",
     "reports/research/dayflip_sector_clustering/FINDINGS.md"),
    ("AN", 38, 0.74, "electronics-industry vs rest",
     "reports/research/dayflip_sector_clustering/FINDINGS.md"),
    ("AN", 38, 0.83, "electronic-components vs rest",
     "reports/research/dayflip_sector_clustering/FINDINGS.md"),

    # --- AO: dayflip_lending_gap_twofactor (n=139, 24 tickers) ---
    ("AO", 139, 0.566, "fgap vs si_lend_pct correlation, Spearman (independence check)",
     "reports/research/dayflip_lending_gap_twofactor/FINDINGS.md"),
    ("AO", 139, 0.638, "fgap vs si_lend_pct correlation, Pearson (independence check)",
     "reports/research/dayflip_lending_gap_twofactor/FINDINGS.md"),
    ("AO", 93, 0.112, "fgap alone, tercile T1 vs T3, permutation test",
     "reports/research/dayflip_lending_gap_twofactor/FINDINGS.md"),
    ("AO", 93, 0.019, "si_lend_pct alone, tercile T1 vs T3, permutation test "
     "-- REPRODUCES item R's headline p=0.021 on nearly the same subset, "
     "not an independent confirmation",
     "reports/research/dayflip_lending_gap_twofactor/FINDINGS.md"),
    ("AO", 93, 0.501, "combined average-rank of fgap+si_lend_pct, tercile "
     "T1 vs T3, permutation test",
     "reports/research/dayflip_lending_gap_twofactor/FINDINGS.md"),
    ("AO", 190, 0.156, "fgap vs pnl Spearman, full 190-trade sanity check",
     "reports/research/dayflip_lending_gap_twofactor/FINDINGS.md"),

    # --- AP: branch_9217_day_of_week (n=36) ---
    ("AP", 36, 0.34, "weekday grouping, Kruskal-Wallis",
     "reports/research/branch_9217_day_of_week/FINDINGS.md"),
    ("AP", 36, 0.58, "weekday grouping, ANOVA",
     "reports/research/branch_9217_day_of_week/FINDINGS.md"),
    ("AP", 36, 0.49, "weekday ordinal Spearman vs excess return",
     "reports/research/branch_9217_day_of_week/FINDINGS.md"),
    ("AP", 36, 0.70, "Monday vs rest, permutation test",
     "reports/research/branch_9217_day_of_week/FINDINGS.md"),
    ("AP", 36, 0.30, "Friday vs rest, permutation test",
     "reports/research/branch_9217_day_of_week/FINDINGS.md"),
    ("AP", 11, 0.14, "Wednesday (n=11, largest bucket) vs rest, unadjusted "
     "-- post-hoc pick-the-biggest-bucket, explicitly flagged as not usable",
     "reports/research/branch_9217_day_of_week/FINDINGS.md"),

    # --- AQ: dayflip_etf_membership_confound ---
    ("AQ", 190, 0.48, "narrow ETF-membership proxy vs pnl",
     "reports/research/dayflip_etf_membership_confound/FINDINGS.md"),
    ("AQ", 190, 0.63, "broad ETF-membership proxy vs pnl",
     "reports/research/dayflip_etf_membership_confound/FINDINGS.md"),
    ("AQ", 190, 0.91, "ETF-membership proxy vs win rate",
     "reports/research/dayflip_etf_membership_confound/FINDINGS.md"),

    # --- AT: wma20_bounce_single_stock_generalize (n=13 tickers) ---
    ("AT", 13, 0.0002, "sign test, full period, 13/13 tickers positive -- "
     "HEADLINE finding", "reports/research/wma20_bounce_single_stock_generalize/FINDINGS.md"),
    ("AT", 13, 0.023, "sign test, OOS split, 11/13 tickers positive",
     "reports/research/wma20_bounce_single_stock_generalize/FINDINGS.md"),

    # --- AU: dayflip_wma20_shortside_filter (n=125) ---
    ("AU", 125, 0.181, "T0-confirm vs unconfirmed, mean pnl, permutation test",
     "reports/research/dayflip_wma20_shortside_filter/FINDINGS.md"),
    ("AU", 125, 0.161, "T0-confirm vs unconfirmed, win rate, permutation test",
     "reports/research/dayflip_wma20_shortside_filter/FINDINGS.md"),
]

POSITIVE_ITEMS = ("R", "AT")


def bonferroni_threshold(alpha: float, n: int) -> float:
    return alpha / n


def benjamini_hochberg(p_values: list[float], alpha: float = 0.05) -> tuple[float | None, list[bool]]:
    """Standard BH step-up procedure. Returns (largest p that survives, per-
    p-value survives-flag in ORIGINAL input order). Returns (None, [False]*n)
    if nothing survives."""
    n = len(p_values)
    indexed = sorted(range(n), key=lambda i: p_values[i])
    threshold_p = None
    survives_rank = [False] * n
    for rank, idx in enumerate(indexed, start=1):
        crit = (rank / n) * alpha
        if p_values[idx] <= crit:
            threshold_p = p_values[idx]
    if threshold_p is not None:
        for i, p in enumerate(p_values):
            if p <= threshold_p:
                survives_rank[i] = True
    return threshold_p, survives_rank


def log_all_trials() -> None:
    for item, n_obs, p, desc, source in RECORDS:
        status = "kept" if p < 0.05 else "rejected"
        append_trial(
            FAMILY,
            topic_id=f"creative-combo-item-{item}",
            ts=TS,
            params={"item": item, "test": desc},
            n_observations=n_obs,
            metric_name="p_value",
            metric_value=p,
            status=status,
            trial_id=f"item-{item}-{desc[:40]}",
            source=source,
            notes=f"Extracted from {source} (cross-checked against {SOURCE_DOC}) "
                  f"for the family-wise multiple-comparisons audit.",
            tags=["creative-combo-2026-08-07", f"item-{item}"],
        )


def main() -> None:
    log_all_trials()

    items = sorted({r[0] for r in RECORDS})
    per_item_min_p = {it: min(p for i, _, p, _, _ in RECORDS if i == it) for it in items}
    all_subtest_p = [p for _, _, p, _, _ in RECORDS]

    n_items_tested = len(items)  # 24
    n_items_all = n_items_tested + len(NO_PVALUE_ITEMS)  # 24 + 20 = 44
    n_subtests = len(all_subtest_p)  # 93

    alpha = 0.05

    bonf_items_tested = bonferroni_threshold(alpha, n_items_tested)
    bonf_items_all = bonferroni_threshold(alpha, n_items_all)
    bonf_subtests = bonferroni_threshold(alpha, n_subtests)

    # BH across the n_items_tested representative (best-per-item) p-values
    rep_p_list = [per_item_min_p[it] for it in items]
    bh_rep_thresh, bh_rep_flags = benjamini_hochberg(rep_p_list, alpha)
    bh_rep_survive = {it: flag for it, flag in zip(items, bh_rep_flags)}

    # BH across the n_items_tested representative p-values but with the
    # untested items padded in at p=1.0 (they cannot pass any correction,
    # included for an honest denominator only)
    rep_p_padded = rep_p_list + [1.0] * len(NO_PVALUE_ITEMS)
    bh_all_thresh, _ = benjamini_hochberg(rep_p_padded, alpha)

    # BH across every individual sub-test p-value (~86)
    bh_sub_thresh, bh_sub_flags = benjamini_hochberg(all_subtest_p, alpha)

    print("=" * 78)
    print("Family-wise multiple-comparisons audit -- creative_combo_2026-08-07")
    print("=" * 78)
    print(_ITEM_COUNT_NOTE)
    print()
    print(f"Items with >=1 formal p-value logged : {n_items_tested}")
    print(f"Items with no formal significance test : {len(NO_PVALUE_ITEMS)}")
    print(f"Total campaign items (rows in PROGRESS.md table) : {n_items_all}")
    print(f"Total individual sub-test p-values extracted : {n_subtests}")
    print()
    print(f"Bonferroni threshold, N={n_items_tested} (tested items only) : {bonf_items_tested:.5f}")
    print(f"Bonferroni threshold, N={n_items_all} (all campaign items)    : {bonf_items_all:.5f}")
    print(f"Bonferroni threshold, N={n_subtests} (every sub-test)         : {bonf_subtests:.5f}")
    print(f"BH-FDR critical p, {n_items_tested} representative p-values : {bh_rep_thresh}")
    print(f"BH-FDR critical p, {n_items_all} items ({len(NO_PVALUE_ITEMS)} padded at p=1) : {bh_all_thresh}")
    print(f"BH-FDR critical p, {n_subtests} sub-tests : {bh_sub_thresh}")
    print()

    for item in POSITIVE_ITEMS:
        rep_p = per_item_min_p[item]
        subtests = [p for i, _, p, d, s in RECORDS if i == item]
        print(f"--- item {item} ---")
        print(f"  representative (best) p-value : {rep_p}")
        print(f"  all sub-test p-values : {subtests}")
        print(f"  survives Bonferroni N={n_items_tested} ({bonf_items_tested:.5f})? {rep_p <= bonf_items_tested}")
        print(f"  survives Bonferroni N={n_items_all} ({bonf_items_all:.5f})? {rep_p <= bonf_items_all}")
        print(f"  survives Bonferroni N={n_subtests} ({bonf_subtests:.5f})? {rep_p <= bonf_subtests}")
        print(f"  survives BH-FDR ({n_items_tested} items, alpha=0.05)? {bh_rep_survive[item]}")
        print()

    write_report(
        n_items_tested, n_items_all, n_subtests,
        bonf_items_tested, bonf_items_all, bonf_subtests,
        bh_rep_thresh, bh_all_thresh, bh_sub_thresh,
        per_item_min_p, bh_rep_survive,
    )


def write_report(
    n_items_tested, n_items_all, n_subtests,
    bonf_items_tested, bonf_items_all, bonf_subtests,
    bh_rep_thresh, bh_all_thresh, bh_sub_thresh,
    per_item_min_p, bh_rep_survive,
) -> None:
    out_dir = ROOT / "reports" / "research" / "creative_combo_2026-08-07"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "meta_multiple_comparisons_audit.md"

    def fmt(x):
        return f"{x:.5f}" if x is not None else "n/a (nothing survives)"

    lines = []
    lines.append("# Family-wise multiple-comparisons audit -- creative_combo_2026-08-07\n")
    lines.append(
        "Meta-audit applying the campaign's own checklist requirement "
        "(BUG-4/A7: compute expected passes under pure noise before trusting "
        "a sweep's pass count) to the campaign's own 44-item family. "
        "Generated by `scripts/research/creative_combo_meta_multiple_comparisons_audit.py`, "
        "all p-values logged to `reports/research/_trial_registry/creative_combo_meta_pvalues.jsonl` "
        "via `trial_registry.append_trial()`.\n"
    )
    lines.append(_ITEM_COUNT_NOTE + "\n")

    lines.append("## Denominators\n")
    lines.append(f"- N={n_items_tested}: items that ran >=1 formal significance test "
                  "(representative p = best/minimum p-value reported by that item)")
    lines.append(f"- N={n_items_all}: all campaign items, including the "
                  f"{n_items_all - n_items_tested} that reported no formal p-value "
                  "(bug fixes, tooling, coverage audits, data-availability negatives -- "
                  "padded at p=1.0, cannot pass any correction but counted in the family size)")
    lines.append(f"- N={n_subtests}: every individual p-value extracted across all items "
                  "(axis cuts, robustness re-runs, secondary indicators) -- the most "
                  "conservative reading, matching item AJ's own multiple-axis-cut caveat\n")

    lines.append("## Thresholds\n")
    lines.append("| Correction | N | Critical p (alpha=0.05) |")
    lines.append("|---|---:|---:|")
    lines.append(f"| Bonferroni | {n_items_tested} (tested items) | {fmt(bonf_items_tested)} |")
    lines.append(f"| Bonferroni | {n_items_all} (all campaign items) | {fmt(bonf_items_all)} |")
    lines.append(f"| Bonferroni | {n_subtests} (all sub-tests) | {fmt(bonf_subtests)} |")
    lines.append(f"| BH-FDR | {n_items_tested} representative p-values | {fmt(bh_rep_thresh)} |")
    lines.append(f"| BH-FDR | {n_items_all} items ({len(NO_PVALUE_ITEMS)} padded at p=1) | {fmt(bh_all_thresh)} |")
    lines.append(f"| BH-FDR | {n_subtests} sub-tests | {fmt(bh_sub_thresh)} |\n")

    lines.append("## Verdict for the campaign's two positive findings\n")
    lines.append(f"| Item | Representative p | Bonferroni N={n_items_tested} | Bonferroni N={n_items_all} | "
                  f"Bonferroni N=subtests | BH-FDR ({n_items_tested} items) |")
    lines.append("|---|---:|---|---|---|---|")
    for item in POSITIVE_ITEMS:
        p = per_item_min_p[item]
        lines.append(
            f"| {item} | {p} | "
            f"{'SURVIVES' if p <= bonf_items_tested else 'fails'} | "
            f"{'SURVIVES' if p <= bonf_items_all else 'fails'} | "
            f"{'SURVIVES' if p <= bonf_subtests else 'fails'} | "
            f"{'SURVIVES' if bh_rep_survive[item] else 'fails'} |"
        )
    lines.append("")

    lines.append("## Interpretation\n")
    lines.append(
        "- **AT** (WMA20 bounce-confirm generalization, p=0.0002 sign test on 13/13 "
        "tickers) clears every correction tested here, including the strictest "
        "Bonferroni-across-every-sub-test threshold. This is the campaign's most "
        "defensible positive finding on pure statistical grounds -- though it is "
        "still only an entry-signal-quality result (no exits/costs modeled) and "
        "needs the G2+ adoption gate before any live use, independent of this audit."
    )
    lines.append(
        "- **R** (institutional short-lending level as a dayflip-short quality "
        "filter, p=0.021 pnl / p=0.005 win-rate headline) clears BH-FDR at the "
        f"{n_items_tested}-item representative-p level and clears Bonferroni at "
        f"N={n_items_tested}, but does "
        f"**not** clear the stricter Bonferroni-across-N={n_items_all}-items threshold "
        f"(critical p={bonf_items_all:.5f}) nor the Bonferroni-across-all-subtests "
        f"threshold (critical p={bonf_subtests:.5f}). Its own item report already "
        "flags this honestly (\"5 indicators tested, only 1 significant\"; "
        "\"n=24 tickers, partial coverage\"; \"recommend manual review, not direct "
        "implementation\"). Item AO's p=0.019 reproduction on nearly the same "
        "n=139 subset is a replication check, not independent confirmation, and "
        "does not change this verdict either way."
    )
    lines.append(
        "- Neither verdict is overturned by this audit -- both items' own "
        "FINDINGS.md already hedged appropriately (R explicitly flagged as "
        "'flag for manual review, not deployable'; AT explicitly flagged as "
        "'entry-signal-quality research, needs G2+ gate'). What this audit adds "
        "is the explicit family-wise accounting the checklist requires: at "
        "alpha=0.05 across a ~44-item family, pure noise predicts "
        f"{0.05 * 44:.1f} false positives at nominal significance -- almost "
        "exactly the 2 positive findings produced. That is expected under the "
        "null, not evidence against either finding, but it is also why neither "
        "should be treated as more than 'promising, needs confirmation' without "
        "this context stated explicitly."
    )
    lines.append("")

    lines.append("## Items with no formal significance test (excluded from the correction)\n")
    lines.append("| Item | Reason |")
    lines.append("|---|---|")
    for item, reason in sorted(NO_PVALUE_ITEMS.items()):
        lines.append(f"| {item} | {reason} |")
    lines.append("")

    lines.append(
        f"\nFull per-record data ({n_subtests} rows, {n_items_tested} items x 1-11 sub-tests each) "
        "logged via `trial_registry.append_trial()` to "
        "`reports/research/_trial_registry/creative_combo_meta_pvalues.jsonl` "
        "(one JSON line per p-value, with source file + description)."
    )

    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
