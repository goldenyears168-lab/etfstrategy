"""SQLite helpers for copytrade backtest tables (00981A 跟單研究)."""

from __future__ import annotations

import sqlite3

from stock_db.util import utc_now_iso

COPYTRADE_NLEGS_DDL = """
CREATE TABLE IF NOT EXISTS copytrade_nlegs_filter_compare (
    batch_id TEXT NOT NULL,
    etf_code TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    filter_id TEXT NOT NULL,
    filter_label TEXT,
    capital_ntd REAL NOT NULL,
    entry_lag_days INTEGER NOT NULL,
    hold_trading_days INTEGER NOT NULL,
    n_complete_days INTEGER NOT NULL DEFAULT 0,
    n_multi_leg_days INTEGER NOT NULL DEFAULT 0,
    n_legs INTEGER NOT NULL DEFAULT 0,
    n_signal_days_in_filter INTEGER NOT NULL DEFAULT 0,
    n_signal_days_excluded INTEGER NOT NULL DEFAULT 0,
    total_pnl_ntd REAL,
    total_alpha_ntd REAL,
    avg_day_return_pct REAL,
    win_rate_gross_pct REAL,
    win_rate_vs_bench_pct REAL,
    win_rate_alpha_pct REAL,
    recycled_n_cycles INTEGER,
    recycled_total_alpha_ntd REAL,
    recycled_total_pnl_ntd REAL,
    mean_excess_pct REAL,
    p_value_ttest REAL,
    p_value_wilcoxon REAL,
    leg_win_rate_gross_pct REAL,
    leg_n_complete INTEGER NOT NULL DEFAULT 0,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (batch_id, strategy_id, filter_id)
);

CREATE INDEX IF NOT EXISTS idx_copytrade_nlegs_cmp_batch
    ON copytrade_nlegs_filter_compare (batch_id, strategy_id);
"""

COPYTRADE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS copytrade_runs (
    run_id TEXT PRIMARY KEY,
    etf_code TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    strategy_label TEXT,
    capital_ntd REAL NOT NULL,
    entry_lag_days INTEGER NOT NULL DEFAULT 0,
    hold_trading_days INTEGER NOT NULL DEFAULT 1,
    cost_bps REAL NOT NULL DEFAULT 0,
    window_start TEXT,
    window_end TEXT,
    copytrade_version TEXT NOT NULL DEFAULT 'copytrade-v4',
    n_signal_days INTEGER NOT NULL DEFAULT 0,
    n_complete_days INTEGER NOT NULL DEFAULT 0,
    total_deployed_ntd REAL,
    total_pnl_ntd REAL,
    total_return_pct REAL,
    avg_day_return_pct REAL,
    win_rate_pct REAL,
    max_drawdown_pct REAL,
    total_bench_return_pct REAL,
    total_alpha_ntd REAL,
    message TEXT,
    synced_at TEXT NOT NULL,
    entry_price_mode TEXT NOT NULL DEFAULT 'open',
    batch_id TEXT,
    total_capm_alpha_ntd REAL,
    mean_excess_pct REAL,
    p_value_ttest REAL,
    p_value_wilcoxon REAL,
    t_stat REAL
);

CREATE TABLE IF NOT EXISTS copytrade_action_compare (
    batch_id TEXT NOT NULL,
    etf_code TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    action_filter TEXT NOT NULL,
    capital_ntd REAL NOT NULL,
    entry_lag_days INTEGER NOT NULL,
    hold_trading_days INTEGER NOT NULL,
    n_complete_days INTEGER NOT NULL DEFAULT 0,
    n_multi_leg_days INTEGER NOT NULL DEFAULT 0,
    n_legs INTEGER NOT NULL DEFAULT 0,
    total_pnl_ntd REAL,
    total_alpha_ntd REAL,
    avg_day_return_pct REAL,
    win_rate_gross_pct REAL,
    win_rate_vs_bench_pct REAL,
    win_rate_alpha_pct REAL,
    recycled_n_cycles INTEGER,
    recycled_total_alpha_ntd REAL,
    recycled_total_pnl_ntd REAL,
    mean_excess_pct REAL,
    p_value_ttest REAL,
    p_value_wilcoxon REAL,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (batch_id, strategy_id, action_filter)
);

CREATE TABLE IF NOT EXISTS copytrade_allocation_compare (
    batch_id TEXT NOT NULL,
    etf_code TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    allocation_mode TEXT NOT NULL,
    capital_ntd REAL NOT NULL,
    entry_lag_days INTEGER NOT NULL,
    hold_trading_days INTEGER NOT NULL,
    n_complete_days INTEGER NOT NULL DEFAULT 0,
    n_multi_leg_days INTEGER NOT NULL DEFAULT 0,
    total_pnl_ntd REAL,
    total_alpha_ntd REAL,
    avg_day_return_pct REAL,
    recycled_n_cycles INTEGER,
    recycled_total_alpha_ntd REAL,
    recycled_total_pnl_ntd REAL,
    mean_excess_pct REAL,
    p_value_ttest REAL,
    p_value_wilcoxon REAL,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (batch_id, strategy_id, allocation_mode)
);

CREATE TABLE IF NOT EXISTS copytrade_capital_cycle (
    batch_id TEXT NOT NULL,
    etf_code TEXT NOT NULL,
    entry_row TEXT NOT NULL,
    horizon INTEGER NOT NULL,
    capital_ntd REAL NOT NULL,
    strategy_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    n_signals INTEGER NOT NULL DEFAULT 0,
    unconstrained_total_alpha_ntd REAL,
    unconstrained_alpha_per_day REAL,
    marginal_unconstrained_alpha_ntd REAL,
    p_value_wilcoxon REAL,
    is_significant INTEGER NOT NULL DEFAULT 0,
    recycled_n_cycles INTEGER NOT NULL DEFAULT 0,
    recycled_total_alpha_ntd REAL,
    recycled_total_pnl_ntd REAL,
    recycled_locked_days INTEGER NOT NULL DEFAULT 0,
    alpha_per_locked_day REAL,
    alpha_per_cycle REAL,
    signal_capture_pct REAL,
    marginal_recycled_alpha_ntd REAL,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (batch_id, entry_row, horizon)
);

CREATE TABLE IF NOT EXISTS copytrade_capital_slots (
    batch_id TEXT NOT NULL,
    etf_code TEXT NOT NULL,
    entry_row TEXT NOT NULL,
    horizon INTEGER NOT NULL,
    capital_ntd REAL NOT NULL,
    n_slots INTEGER NOT NULL,
    per_signal_ntd REAL NOT NULL,
    slots_mode TEXT NOT NULL DEFAULT 'fixed',
    strategy_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    n_signals INTEGER NOT NULL DEFAULT 0,
    unconstrained_total_alpha_ntd REAL,
    p_value_wilcoxon REAL,
    is_significant INTEGER NOT NULL DEFAULT 0,
    recycled_n_cycles INTEGER NOT NULL DEFAULT 0,
    recycled_total_alpha_ntd REAL,
    recycled_total_pnl_ntd REAL,
    recycled_locked_days INTEGER NOT NULL DEFAULT 0,
    alpha_per_locked_day REAL,
    alpha_per_cycle REAL,
    signal_capture_pct REAL,
    peak_concurrent_slots INTEGER NOT NULL DEFAULT 0,
    marginal_recycled_alpha_ntd REAL,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (batch_id, entry_row, horizon, slots_mode, n_slots)
);

CREATE TABLE IF NOT EXISTS copytrade_regime_horizon (
    batch_id TEXT NOT NULL,
    etf_code TEXT NOT NULL,
    entry_row TEXT NOT NULL,
    bucket_field TEXT NOT NULL,
    bucket_value TEXT NOT NULL,
    horizon INTEGER NOT NULL,
    n_signal_days INTEGER NOT NULL DEFAULT 0,
    total_alpha_ntd REAL,
    mean_excess_pct REAL,
    p_value_wilcoxon REAL,
    is_significant INTEGER NOT NULL DEFAULT 0,
    marginal_total_alpha_ntd REAL,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (batch_id, entry_row, bucket_field, bucket_value, horizon)
);

CREATE TABLE IF NOT EXISTS copytrade_regime_signal_labels (
    batch_id TEXT NOT NULL,
    signal_date TEXT NOT NULL,
    trend_posture TEXT NOT NULL,
    exposure_decision TEXT NOT NULL,
    trend_posture_score INTEGER,
    top_risk_score INTEGER,
    composite_score REAL,
    ix_stage INTEGER,
    ix_trend_score REAL,
    tx_gap_pct REAL,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (batch_id, signal_date)
);

CREATE TABLE IF NOT EXISTS copytrade_regime_sweet_spots (
    batch_id TEXT NOT NULL,
    bucket_field TEXT NOT NULL,
    bucket_value TEXT NOT NULL,
    sweet_spot_h INTEGER NOT NULL,
    sweet_spot_total_alpha_ntd REAL,
    n_signal_days_at_sweet INTEGER,
    hold_through_h INTEGER,
    mean_excess_at_sweet REAL,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (batch_id, bucket_field, bucket_value)
);

CREATE TABLE IF NOT EXISTS copytrade_leg_decay (
    batch_id TEXT NOT NULL,
    etf_code TEXT NOT NULL,
    entry_lag_days INTEGER NOT NULL,
    bucket_field TEXT NOT NULL,
    bucket_value TEXT NOT NULL,
    horizon INTEGER NOT NULL,
    n_legs INTEGER NOT NULL DEFAULT 0,
    mean_excess_pct REAL,
    mean_alpha_ntd REAL,
    sum_alpha_ntd REAL,
    marginal_mean_excess_pct REAL,
    marginal_sum_alpha_ntd REAL,
    p_value_wilcoxon REAL,
    is_significant INTEGER NOT NULL DEFAULT 0,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (batch_id, bucket_field, bucket_value, horizon)
);

CREATE TABLE IF NOT EXISTS copytrade_leg_decay_knees (
    batch_id TEXT NOT NULL,
    bucket_field TEXT NOT NULL,
    bucket_value TEXT NOT NULL,
    peak_mean_excess_h INTEGER NOT NULL,
    peak_mean_excess_pct REAL,
    best_sum_alpha_h INTEGER NOT NULL,
    best_sum_alpha_ntd REAL,
    knee_h INTEGER NOT NULL,
    marginal_knee_h INTEGER,
    efficiency_h INTEGER,
    efficiency_alpha_per_day REAL,
    n_legs_at_peak INTEGER NOT NULL DEFAULT 0,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (batch_id, bucket_field, bucket_value)
);

CREATE TABLE IF NOT EXISTS copytrade_event_exit_policies (
    batch_id TEXT NOT NULL,
    etf_code TEXT NOT NULL,
    policy_id TEXT NOT NULL,
    policy_label TEXT,
    baseline_h INTEGER NOT NULL,
    n_legs INTEGER NOT NULL DEFAULT 0,
    n_complete INTEGER NOT NULL DEFAULT 0,
    n_triggered INTEGER NOT NULL DEFAULT 0,
    n_early_exit INTEGER NOT NULL DEFAULT 0,
    mean_alpha_ntd REAL,
    mean_excess_pct REAL,
    total_alpha_ntd REAL,
    vs_baseline_alpha_delta REAL,
    mean_paired_alpha_delta REAL,
    p_value_wilcoxon_paired REAL,
    rotation_capital_ntd REAL,
    rotation_recycled_alpha_ntd REAL,
    rotation_n_cycles INTEGER,
    rotation_capture_pct REAL,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (batch_id, policy_id)
);

CREATE TABLE IF NOT EXISTS copytrade_event_exit_legs (
    batch_id TEXT NOT NULL,
    policy_id TEXT NOT NULL,
    signal_date TEXT NOT NULL,
    stock_id TEXT NOT NULL,
    action TEXT,
    entry_date TEXT,
    planned_exit_date TEXT,
    actual_exit_date TEXT,
    exit_reason TEXT,
    triggered INTEGER NOT NULL DEFAULT 0,
    hold_days INTEGER,
    alpha_ntd REAL,
    baseline_alpha_ntd REAL,
    status TEXT,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (batch_id, policy_id, signal_date, stock_id)
);

CREATE TABLE IF NOT EXISTS copytrade_leg_attribution_buckets (
    batch_id TEXT NOT NULL,
    etf_code TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    bucket_field TEXT NOT NULL,
    bucket_value TEXT NOT NULL,
    n_legs INTEGER NOT NULL DEFAULT 0,
    mean_return_pct REAL,
    mean_excess_pct REAL,
    mean_alpha_ntd REAL,
    sum_alpha_ntd REAL,
    win_rate_return_pct REAL,
    win_rate_excess_pct REAL,
    p_value_wilcoxon_excess REAL,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (batch_id, bucket_field, bucket_value)
);

CREATE TABLE IF NOT EXISTS copytrade_leg_attribution_hypotheses (
    batch_id TEXT NOT NULL,
    hypothesis_id TEXT NOT NULL,
    label TEXT,
    verdict TEXT NOT NULL,
    n_a INTEGER,
    n_b INTEGER,
    mean_excess_a REAL,
    mean_excess_b REAL,
    p_value_wilcoxon REAL,
    summary_zh TEXT,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (batch_id, hypothesis_id)
);

CREATE TABLE IF NOT EXISTS copytrade_leg_attribution_correlations (
    batch_id TEXT NOT NULL,
    feature TEXT NOT NULL,
    n INTEGER NOT NULL DEFAULT 0,
    pearson_r REAL,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (batch_id, feature)
);

CREATE TABLE IF NOT EXISTS copytrade_leg_attribution_cases (
    batch_id TEXT NOT NULL,
    case_type TEXT NOT NULL,
    signal_date TEXT NOT NULL,
    stock_id TEXT NOT NULL DEFAULT '',
    trend_posture TEXT,
    tx_gap_pct REAL,
    n_legs INTEGER,
    day_alpha_ntd REAL,
    sector TEXT,
    theme TEXT,
    return_pct REAL,
    alpha_ntd REAL,
    overnight_gap_pct REAL,
    prior_5d_pct REAL,
    prior_10d_pct REAL,
    position_52w_pct REAL,
    skip_overextended INTEGER,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (batch_id, case_type, signal_date, stock_id)
);

CREATE TABLE IF NOT EXISTS copytrade_etf_compare (
    batch_id TEXT NOT NULL,
    etf_code TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    capital_ntd REAL NOT NULL,
    per_signal_ntd REAL NOT NULL,
    hold_trading_days INTEGER NOT NULL,
    slots_mode TEXT NOT NULL,
    window_start TEXT,
    window_end TEXT,
    verdict TEXT NOT NULL,
    n_paired INTEGER NOT NULL DEFAULT 0,
    n_missing_etf INTEGER NOT NULL DEFAULT 0,
    win_rate_pct REAL,
    mean_diff_return_pct REAL,
    p_value_ttest REAL,
    p_value_wilcoxon REAL,
    cum_copytrade_pnl_ntd REAL,
    cum_etf_pnl_ntd REAL,
    diff_gross_ntd REAL,
    cum_alpha_tw_ntd REAL,
    n_executed INTEGER,
    signal_capture_pct REAL,
    peak_slots INTEGER,
    all_n_paired INTEGER,
    all_win_rate_pct REAL,
    all_mean_diff_return_pct REAL,
    all_p_value_wilcoxon REAL,
    all_diff_gross_ntd REAL,
    bh_entry_date TEXT,
    bh_exit_date TEXT,
    bh_return_pct REAL,
    bh_pnl_ntd REAL,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (batch_id, strategy_id)
);

CREATE TABLE IF NOT EXISTS copytrade_chip_filter_compare (
    batch_id TEXT NOT NULL,
    etf_code TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    filter_id TEXT NOT NULL,
    filter_label TEXT,
    capital_ntd REAL NOT NULL,
    entry_lag_days INTEGER NOT NULL,
    hold_trading_days INTEGER NOT NULL,
    n_complete_days INTEGER NOT NULL DEFAULT 0,
    n_multi_leg_days INTEGER NOT NULL DEFAULT 0,
    n_legs INTEGER NOT NULL DEFAULT 0,
    n_legs_with_chip INTEGER NOT NULL DEFAULT 0,
    total_pnl_ntd REAL,
    total_alpha_ntd REAL,
    avg_day_return_pct REAL,
    win_rate_gross_pct REAL,
    win_rate_vs_bench_pct REAL,
    win_rate_alpha_pct REAL,
    recycled_n_cycles INTEGER,
    recycled_total_alpha_ntd REAL,
    recycled_total_pnl_ntd REAL,
    mean_excess_pct REAL,
    p_value_ttest REAL,
    p_value_wilcoxon REAL,
    leg_win_rate_gross_pct REAL,
    leg_n_complete INTEGER NOT NULL DEFAULT 0,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (batch_id, strategy_id, filter_id)
);

CREATE TABLE IF NOT EXISTS copytrade_confluence_filter_compare (
    batch_id TEXT NOT NULL,
    etf_code TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    filter_id TEXT NOT NULL,
    filter_label TEXT,
    capital_ntd REAL NOT NULL,
    entry_lag_days INTEGER NOT NULL,
    hold_trading_days INTEGER NOT NULL,
    n_complete_days INTEGER NOT NULL DEFAULT 0,
    n_multi_leg_days INTEGER NOT NULL DEFAULT 0,
    n_legs INTEGER NOT NULL DEFAULT 0,
    n_legs_with_confluence INTEGER NOT NULL DEFAULT 0,
    total_pnl_ntd REAL,
    total_alpha_ntd REAL,
    avg_day_return_pct REAL,
    win_rate_gross_pct REAL,
    win_rate_vs_bench_pct REAL,
    win_rate_alpha_pct REAL,
    recycled_n_cycles INTEGER,
    recycled_total_alpha_ntd REAL,
    recycled_total_pnl_ntd REAL,
    mean_excess_pct REAL,
    p_value_ttest REAL,
    p_value_wilcoxon REAL,
    leg_win_rate_gross_pct REAL,
    leg_n_complete INTEGER NOT NULL DEFAULT 0,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (batch_id, strategy_id, filter_id)
);

CREATE TABLE IF NOT EXISTS copytrade_conviction_filter_compare (
    batch_id TEXT NOT NULL,
    etf_code TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    filter_id TEXT NOT NULL,
    filter_label TEXT,
    capital_ntd REAL NOT NULL,
    entry_lag_days INTEGER NOT NULL,
    hold_trading_days INTEGER NOT NULL,
    n_complete_days INTEGER NOT NULL DEFAULT 0,
    n_multi_leg_days INTEGER NOT NULL DEFAULT 0,
    n_legs INTEGER NOT NULL DEFAULT 0,
    n_legs_with_conviction INTEGER NOT NULL DEFAULT 0,
    total_pnl_ntd REAL,
    total_alpha_ntd REAL,
    avg_day_return_pct REAL,
    win_rate_gross_pct REAL,
    win_rate_vs_bench_pct REAL,
    win_rate_alpha_pct REAL,
    recycled_n_cycles INTEGER,
    recycled_total_alpha_ntd REAL,
    recycled_total_pnl_ntd REAL,
    mean_excess_pct REAL,
    p_value_ttest REAL,
    p_value_wilcoxon REAL,
    leg_win_rate_gross_pct REAL,
    leg_n_complete INTEGER NOT NULL DEFAULT 0,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (batch_id, strategy_id, filter_id)
);

CREATE TABLE IF NOT EXISTS copytrade_gap_filter_compare (
    batch_id TEXT NOT NULL,
    etf_code TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    filter_id TEXT NOT NULL,
    filter_label TEXT,
    capital_ntd REAL NOT NULL,
    entry_lag_days INTEGER NOT NULL,
    hold_trading_days INTEGER NOT NULL,
    n_complete_days INTEGER NOT NULL DEFAULT 0,
    n_multi_leg_days INTEGER NOT NULL DEFAULT 0,
    n_legs INTEGER NOT NULL DEFAULT 0,
    n_legs_with_gap INTEGER NOT NULL DEFAULT 0,
    total_pnl_ntd REAL,
    total_alpha_ntd REAL,
    avg_day_return_pct REAL,
    win_rate_gross_pct REAL,
    win_rate_vs_bench_pct REAL,
    win_rate_alpha_pct REAL,
    recycled_n_cycles INTEGER,
    recycled_total_alpha_ntd REAL,
    recycled_total_pnl_ntd REAL,
    mean_excess_pct REAL,
    p_value_ttest REAL,
    p_value_wilcoxon REAL,
    leg_win_rate_gross_pct REAL,
    leg_n_complete INTEGER NOT NULL DEFAULT 0,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (batch_id, strategy_id, filter_id)
);

CREATE TABLE IF NOT EXISTS copytrade_horizon_decay (
    batch_id TEXT NOT NULL,
    etf_code TEXT NOT NULL,
    entry_row TEXT NOT NULL,
    horizon INTEGER NOT NULL,
    strategy_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    n_complete INTEGER NOT NULL DEFAULT 0,
    total_pnl_ntd REAL,
    total_alpha_ntd REAL,
    total_capm_alpha_ntd REAL,
    mean_excess_pct REAL,
    p_value_ttest REAL,
    p_value_wilcoxon REAL,
    t_stat REAL,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (batch_id, strategy_id)
);

CREATE TABLE IF NOT EXISTS copytrade_leg_chip_snapshots (
    etf_code TEXT NOT NULL,
    signal_date TEXT NOT NULL,
    stock_id TEXT NOT NULL,
    foreign_net_5d REAL,
    foreign_net_5d_million REAL,
    margin_balance REAL,
    margin_growth_5d_pct REAL,
    foreign_net_5d_positive INTEGER NOT NULL DEFAULT 0,
    margin_cool INTEGER NOT NULL DEFAULT 0,
    chip_confirm_pass INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    synced_at TEXT NOT NULL,
    PRIMARY KEY (etf_code, signal_date, stock_id)
);

CREATE TABLE IF NOT EXISTS copytrade_leg_confluence_snapshots (
    etf_code TEXT NOT NULL,
    signal_date TEXT NOT NULL,
    stock_id TEXT NOT NULL,
    action TEXT NOT NULL,
    share_delta REAL NOT NULL,
    vcp_pass INTEGER NOT NULL DEFAULT 0,
    chunge_l4_pass INTEGER NOT NULL DEFAULT 0,
    p6_pass INTEGER NOT NULL DEFAULT 0,
    triple_pass INTEGER NOT NULL DEFAULT 0,
    vcp_score REAL,
    chunge_layers INTEGER NOT NULL DEFAULT 0,
    p6_source TEXT,
    status TEXT NOT NULL DEFAULT 'complete',
    synced_at TEXT NOT NULL,
    PRIMARY KEY (etf_code, signal_date, stock_id)
);

CREATE TABLE IF NOT EXISTS copytrade_leg_conviction_snapshots (
    etf_code TEXT NOT NULL,
    signal_date TEXT NOT NULL,
    stock_id TEXT NOT NULL,
    action TEXT NOT NULL,
    share_delta REAL NOT NULL,
    weight_delta REAL,
    metric_used TEXT,
    metric_value REAL,
    prior_pool TEXT,
    prior_n INTEGER NOT NULL DEFAULT 0,
    p70_threshold REAL,
    conviction_pass INTEGER NOT NULL DEFAULT 0,
    top_pct REAL NOT NULL DEFAULT 30.0,
    status TEXT NOT NULL DEFAULT 'complete',
    synced_at TEXT NOT NULL,
    PRIMARY KEY (etf_code, signal_date, stock_id)
);

CREATE TABLE IF NOT EXISTS copytrade_leg_limit_entry (
    etf_code TEXT NOT NULL,
    signal_date TEXT NOT NULL,
    stock_id TEXT NOT NULL,
    discount_pct REAL NOT NULL,
    entry_date TEXT,
    open_px REAL,
    low_px REAL,
    limit_px REAL,
    filled INTEGER NOT NULL DEFAULT 0,
    fill_px REAL,
    status TEXT NOT NULL DEFAULT 'pending',
    synced_at TEXT NOT NULL,
    PRIMARY KEY (etf_code, signal_date, stock_id, discount_pct)
);

CREATE TABLE IF NOT EXISTS copytrade_leg_opening_confirm (
    etf_code TEXT NOT NULL,
    signal_date TEXT NOT NULL,
    stock_id TEXT NOT NULL,
    entry_date TEXT,
    prev_close REAL,
    vol_0905_0915 REAL,
    vol_0905_0915_avg5 REAL,
    vol_ratio_vs_avg5 REAL,
    px_0915 REAL,
    confirm_entry_px REAL,
    price_ge_prev_close INTEGER NOT NULL DEFAULT 0,
    vol_confirm_pass INTEGER NOT NULL DEFAULT 0,
    opening_confirm_pass INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    synced_at TEXT NOT NULL,
    PRIMARY KEY (etf_code, signal_date, stock_id)
);

CREATE TABLE IF NOT EXISTS copytrade_leg_overnight_gaps (
    etf_code TEXT NOT NULL,
    signal_date TEXT NOT NULL,
    stock_id TEXT NOT NULL,
    entry_lag_days INTEGER NOT NULL DEFAULT 0,
    entry_date TEXT,
    signal_close REAL,
    entry_open REAL,
    overnight_gap_pct REAL,
    status TEXT NOT NULL DEFAULT 'pending',
    synced_at TEXT NOT NULL,
    PRIMARY KEY (etf_code, signal_date, stock_id, entry_lag_days)
);

CREATE TABLE IF NOT EXISTS copytrade_leg_ta_snapshots (
    etf_code TEXT NOT NULL,
    signal_date TEXT NOT NULL,
    stock_id TEXT NOT NULL,
    entry_pattern TEXT,
    entry_tags_json TEXT,
    above_ma60 INTEGER NOT NULL DEFAULT 0,
    uptrend_pullback INTEGER NOT NULL DEFAULT 0,
    skip_overextended INTEGER NOT NULL DEFAULT 0,
    has_strong_trend INTEGER NOT NULL DEFAULT 0,
    dist_ma20_pct REAL,
    dist_ma60_pct REAL,
    position_52w_pct REAL,
    overextended_thresh_pct REAL,
    status TEXT NOT NULL DEFAULT 'pending',
    synced_at TEXT NOT NULL,
    PRIMARY KEY (etf_code, signal_date, stock_id)
);

CREATE TABLE IF NOT EXISTS copytrade_leg_v8_snapshots (
    etf_code TEXT NOT NULL,
    signal_date TEXT NOT NULL,
    stock_id TEXT NOT NULL,
    action TEXT NOT NULL,
    share_delta REAL NOT NULL,
    rs_univ14 REAL,
    inv_weight_pct REAL,
    etf_add_consensus INTEGER NOT NULL DEFAULT 0,
    v8_eligible INTEGER NOT NULL DEFAULT 0,
    consensus_bypass INTEGER NOT NULL DEFAULT 0,
    tree_path_eligible INTEGER NOT NULL DEFAULT 0,
    eligible_reason TEXT,
    status TEXT NOT NULL DEFAULT 'complete',
    synced_at TEXT NOT NULL,
    PRIMARY KEY (etf_code, signal_date, stock_id)
);

CREATE TABLE IF NOT EXISTS copytrade_legs (
    run_id TEXT NOT NULL,
    signal_date TEXT NOT NULL,
    stock_id TEXT NOT NULL,
    stock_name TEXT,
    action TEXT NOT NULL,
    share_delta REAL,
    weight_delta REAL,
    entry_date TEXT,
    exit_date TEXT,
    entry_px REAL,
    exit_px REAL,
    allocated_ntd REAL NOT NULL DEFAULT 0,
    pnl_ntd REAL,
    return_pct REAL,
    gross_return_pct REAL,
    status TEXT NOT NULL DEFAULT 'complete',
    synced_at TEXT NOT NULL,
    PRIMARY KEY (run_id, signal_date, stock_id)
);

CREATE TABLE IF NOT EXISTS copytrade_limit_entry_compare (
    batch_id TEXT NOT NULL,
    etf_code TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    filter_id TEXT NOT NULL,
    filter_label TEXT,
    discount_pct REAL,
    capital_ntd REAL NOT NULL,
    entry_lag_days INTEGER NOT NULL,
    hold_trading_days INTEGER NOT NULL,
    n_complete_days INTEGER NOT NULL DEFAULT 0,
    n_multi_leg_days INTEGER NOT NULL DEFAULT 0,
    n_legs INTEGER NOT NULL DEFAULT 0,
    n_legs_filled INTEGER NOT NULL DEFAULT 0,
    fill_rate_pct REAL,
    total_pnl_ntd REAL,
    total_alpha_ntd REAL,
    avg_day_return_pct REAL,
    win_rate_gross_pct REAL,
    win_rate_vs_bench_pct REAL,
    win_rate_alpha_pct REAL,
    recycled_n_cycles INTEGER,
    recycled_total_alpha_ntd REAL,
    recycled_total_pnl_ntd REAL,
    mean_excess_pct REAL,
    p_value_ttest REAL,
    p_value_wilcoxon REAL,
    leg_win_rate_gross_pct REAL,
    leg_n_complete INTEGER NOT NULL DEFAULT 0,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (batch_id, strategy_id, filter_id)
);

CREATE TABLE IF NOT EXISTS copytrade_macro_filter_compare (
    batch_id TEXT NOT NULL,
    etf_code TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    filter_id TEXT NOT NULL,
    filter_label TEXT,
    capital_ntd REAL NOT NULL,
    entry_lag_days INTEGER NOT NULL,
    hold_trading_days INTEGER NOT NULL,
    n_complete_days INTEGER NOT NULL DEFAULT 0,
    n_skipped_risk_days INTEGER NOT NULL DEFAULT 0,
    n_risk_days_in_baseline INTEGER NOT NULL DEFAULT 0,
    total_pnl_ntd REAL,
    total_alpha_ntd REAL,
    avg_day_return_pct REAL,
    win_rate_gross_pct REAL,
    win_rate_vs_bench_pct REAL,
    win_rate_alpha_pct REAL,
    recycled_n_cycles INTEGER,
    recycled_total_alpha_ntd REAL,
    recycled_total_pnl_ntd REAL,
    mean_excess_pct REAL,
    p_value_ttest REAL,
    p_value_wilcoxon REAL,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (batch_id, strategy_id, filter_id)
);

CREATE TABLE IF NOT EXISTS copytrade_macro_gap_snapshots (
    entry_date TEXT NOT NULL PRIMARY KEY,
    tx_gap_pct REAL,
    te_gap_pct REAL,
    te_minus_tx_pct REAL,
    tx_gap_source TEXT NOT NULL DEFAULT 'none',
    is_tx_risk INTEGER NOT NULL DEFAULT 0,
    is_te_weak_vs_tx INTEGER NOT NULL DEFAULT 0,
    is_macro_risk INTEGER NOT NULL DEFAULT 0,
    synced_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS copytrade_opening_filter_compare (
    batch_id TEXT NOT NULL,
    etf_code TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    filter_id TEXT NOT NULL,
    filter_label TEXT,
    capital_ntd REAL NOT NULL,
    entry_lag_days INTEGER NOT NULL,
    hold_trading_days INTEGER NOT NULL,
    n_complete_days INTEGER NOT NULL DEFAULT 0,
    n_multi_leg_days INTEGER NOT NULL DEFAULT 0,
    n_legs INTEGER NOT NULL DEFAULT 0,
    n_legs_with_opening INTEGER NOT NULL DEFAULT 0,
    total_pnl_ntd REAL,
    total_alpha_ntd REAL,
    avg_day_return_pct REAL,
    win_rate_gross_pct REAL,
    win_rate_vs_bench_pct REAL,
    win_rate_alpha_pct REAL,
    recycled_n_cycles INTEGER,
    recycled_total_alpha_ntd REAL,
    recycled_total_pnl_ntd REAL,
    mean_excess_pct REAL,
    p_value_ttest REAL,
    p_value_wilcoxon REAL,
    leg_win_rate_gross_pct REAL,
    leg_n_complete INTEGER NOT NULL DEFAULT 0,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (batch_id, strategy_id, filter_id)
);

CREATE TABLE IF NOT EXISTS copytrade_recheck_compare (
    batch_id TEXT NOT NULL,
    etf_code TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    recheck_id TEXT NOT NULL,
    variant_id TEXT NOT NULL,
    variant_label TEXT,
    capital_ntd REAL NOT NULL,
    entry_lag_days INTEGER NOT NULL,
    hold_trading_days INTEGER NOT NULL,
    n_complete_days INTEGER NOT NULL DEFAULT 0,
    n_multi_leg_days INTEGER NOT NULL DEFAULT 0,
    n_legs INTEGER NOT NULL DEFAULT 0,
    total_pnl_ntd REAL,
    total_alpha_ntd REAL,
    avg_day_return_pct REAL,
    win_rate_gross_pct REAL,
    win_rate_vs_bench_pct REAL,
    win_rate_alpha_pct REAL,
    recycled_n_cycles INTEGER,
    recycled_total_alpha_ntd REAL,
    recycled_total_pnl_ntd REAL,
    mean_excess_pct REAL,
    p_value_ttest REAL,
    p_value_wilcoxon REAL,
    leg_win_rate_gross_pct REAL,
    leg_n_complete INTEGER NOT NULL DEFAULT 0,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (batch_id, strategy_id, recheck_id, variant_id)
);

CREATE TABLE IF NOT EXISTS copytrade_research_conclusions (
    batch_id TEXT NOT NULL,
    etf_code TEXT NOT NULL,
    analysis_type TEXT NOT NULL,
    entry_row TEXT,
    metric_key TEXT NOT NULL,
    horizon INTEGER,
    metric_value REAL,
    conclusion_zh TEXT NOT NULL,
    details_json TEXT,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (batch_id, analysis_type, metric_key, entry_row)
);

CREATE TABLE IF NOT EXISTS copytrade_signal_days (
    run_id TEXT NOT NULL,
    signal_date TEXT NOT NULL,
    entry_date TEXT,
    exit_date TEXT,
    n_legs INTEGER NOT NULL DEFAULT 0,
    deployed_ntd REAL NOT NULL DEFAULT 0,
    pnl_ntd REAL,
    return_pct REAL,
    bench_return_pct REAL,
    alpha_ntd REAL,
    capm_alpha_ntd REAL,
    portfolio_beta REAL,
    status TEXT NOT NULL DEFAULT 'complete',
    synced_at TEXT NOT NULL,
    PRIMARY KEY (run_id, signal_date)
);

CREATE TABLE IF NOT EXISTS copytrade_ta_filter_compare (
    batch_id TEXT NOT NULL,
    etf_code TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    filter_id TEXT NOT NULL,
    filter_label TEXT,
    capital_ntd REAL NOT NULL,
    entry_lag_days INTEGER NOT NULL,
    hold_trading_days INTEGER NOT NULL,
    n_complete_days INTEGER NOT NULL DEFAULT 0,
    n_multi_leg_days INTEGER NOT NULL DEFAULT 0,
    n_legs INTEGER NOT NULL DEFAULT 0,
    n_legs_with_ta INTEGER NOT NULL DEFAULT 0,
    total_pnl_ntd REAL,
    total_alpha_ntd REAL,
    avg_day_return_pct REAL,
    win_rate_gross_pct REAL,
    win_rate_vs_bench_pct REAL,
    win_rate_alpha_pct REAL,
    recycled_n_cycles INTEGER,
    recycled_total_alpha_ntd REAL,
    recycled_total_pnl_ntd REAL,
    mean_excess_pct REAL,
    p_value_ttest REAL,
    p_value_wilcoxon REAL,
    leg_win_rate_gross_pct REAL,
    leg_n_complete INTEGER NOT NULL DEFAULT 0,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (batch_id, strategy_id, filter_id)
);

CREATE TABLE IF NOT EXISTS copytrade_v8_filter_compare (
    batch_id TEXT NOT NULL,
    etf_code TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    filter_id TEXT NOT NULL,
    filter_label TEXT,
    capital_ntd REAL NOT NULL,
    entry_lag_days INTEGER NOT NULL,
    hold_trading_days INTEGER NOT NULL,
    n_complete_days INTEGER NOT NULL DEFAULT 0,
    n_multi_leg_days INTEGER NOT NULL DEFAULT 0,
    n_legs INTEGER NOT NULL DEFAULT 0,
    n_legs_with_v8 INTEGER NOT NULL DEFAULT 0,
    total_pnl_ntd REAL,
    total_alpha_ntd REAL,
    avg_day_return_pct REAL,
    win_rate_gross_pct REAL,
    win_rate_vs_bench_pct REAL,
    win_rate_alpha_pct REAL,
    recycled_n_cycles INTEGER,
    recycled_total_alpha_ntd REAL,
    recycled_total_pnl_ntd REAL,
    mean_excess_pct REAL,
    p_value_ttest REAL,
    p_value_wilcoxon REAL,
    leg_win_rate_gross_pct REAL,
    leg_n_complete INTEGER NOT NULL DEFAULT 0,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (batch_id, strategy_id, filter_id)
);

CREATE INDEX IF NOT EXISTS idx_copytrade_action_cmp_batch
    ON copytrade_action_compare (batch_id, strategy_id);

CREATE INDEX IF NOT EXISTS idx_copytrade_alloc_cmp_batch
    ON copytrade_allocation_compare (batch_id, strategy_id);

CREATE INDEX IF NOT EXISTS idx_copytrade_capital_cycle_batch
    ON copytrade_capital_cycle (batch_id, entry_row);

CREATE INDEX IF NOT EXISTS idx_copytrade_capital_slots_batch
    ON copytrade_capital_slots (batch_id, entry_row, slots_mode);

CREATE INDEX IF NOT EXISTS idx_copytrade_regime_horizon_batch
    ON copytrade_regime_horizon (batch_id, bucket_field, bucket_value);

CREATE INDEX IF NOT EXISTS idx_copytrade_leg_decay_batch
    ON copytrade_leg_decay (batch_id, bucket_field, bucket_value);

CREATE INDEX IF NOT EXISTS idx_copytrade_event_exit_batch
    ON copytrade_event_exit_policies (batch_id, policy_id);

CREATE INDEX IF NOT EXISTS idx_copytrade_leg_attrib_batch
    ON copytrade_leg_attribution_buckets (batch_id, bucket_field);

CREATE INDEX IF NOT EXISTS idx_copytrade_etf_compare_batch
    ON copytrade_etf_compare (batch_id, strategy_id);

CREATE INDEX IF NOT EXISTS idx_copytrade_chip_cmp_batch
    ON copytrade_chip_filter_compare (batch_id, strategy_id);

CREATE INDEX IF NOT EXISTS idx_copytrade_conclusions_batch
    ON copytrade_research_conclusions (batch_id, analysis_type);

CREATE INDEX IF NOT EXISTS idx_copytrade_confluence_cmp_batch
    ON copytrade_confluence_filter_compare (batch_id, strategy_id);

CREATE INDEX IF NOT EXISTS idx_copytrade_conviction_cmp_batch
    ON copytrade_conviction_filter_compare (batch_id, strategy_id);

CREATE INDEX IF NOT EXISTS idx_copytrade_decay_batch
    ON copytrade_horizon_decay (batch_id, entry_row, horizon);

CREATE INDEX IF NOT EXISTS idx_copytrade_gap_cmp_batch
    ON copytrade_gap_filter_compare (batch_id, strategy_id);

CREATE INDEX IF NOT EXISTS idx_copytrade_leg_chip_confirm
    ON copytrade_leg_chip_snapshots (chip_confirm_pass, foreign_net_5d_positive);

CREATE INDEX IF NOT EXISTS idx_copytrade_leg_confluence_triple
    ON copytrade_leg_confluence_snapshots (triple_pass, action);

CREATE INDEX IF NOT EXISTS idx_copytrade_leg_conviction_pass
    ON copytrade_leg_conviction_snapshots (conviction_pass, action);

CREATE INDEX IF NOT EXISTS idx_copytrade_leg_ta_pattern
    ON copytrade_leg_ta_snapshots (entry_pattern, skip_overextended);

CREATE INDEX IF NOT EXISTS idx_copytrade_leg_v8_eligible
    ON copytrade_leg_v8_snapshots (v8_eligible, action);

CREATE INDEX IF NOT EXISTS idx_copytrade_legs_stock
    ON copytrade_legs (stock_id, signal_date DESC);

CREATE INDEX IF NOT EXISTS idx_copytrade_limit_entry_cmp_batch
    ON copytrade_limit_entry_compare (batch_id, strategy_id);

CREATE INDEX IF NOT EXISTS idx_copytrade_limit_entry_filled
    ON copytrade_leg_limit_entry (discount_pct, filled, entry_date);

CREATE INDEX IF NOT EXISTS idx_copytrade_macro_cmp_batch
    ON copytrade_macro_filter_compare (batch_id, strategy_id);

CREATE INDEX IF NOT EXISTS idx_copytrade_macro_gap_risk
    ON copytrade_macro_gap_snapshots (is_macro_risk, entry_date);

CREATE INDEX IF NOT EXISTS idx_copytrade_opening_cmp_batch
    ON copytrade_opening_filter_compare (batch_id, strategy_id);

CREATE INDEX IF NOT EXISTS idx_copytrade_opening_confirm_pass
    ON copytrade_leg_opening_confirm (opening_confirm_pass, entry_date);

CREATE INDEX IF NOT EXISTS idx_copytrade_overnight_gap_entry
    ON copytrade_leg_overnight_gaps (etf_code, entry_date);

CREATE INDEX IF NOT EXISTS idx_copytrade_recheck_cmp_batch
    ON copytrade_recheck_compare (batch_id, recheck_id);

CREATE INDEX IF NOT EXISTS idx_copytrade_runs_etf
    ON copytrade_runs (etf_code, strategy_id);

CREATE INDEX IF NOT EXISTS idx_copytrade_signal_days_run
    ON copytrade_signal_days (run_id, signal_date DESC);

CREATE INDEX IF NOT EXISTS idx_copytrade_ta_cmp_batch
    ON copytrade_ta_filter_compare (batch_id, strategy_id);

CREATE INDEX IF NOT EXISTS idx_copytrade_v8_cmp_batch
    ON copytrade_v8_filter_compare (batch_id, strategy_id);

CREATE INDEX IF NOT EXISTS idx_copytrade_runs_batch
    ON copytrade_runs (batch_id, synced_at DESC);

CREATE INDEX IF NOT EXISTS idx_copytrade_legs_run
    ON copytrade_legs (run_id, signal_date, stock_id);

CREATE INDEX IF NOT EXISTS idx_copytrade_event_exit_legs_batch
    ON copytrade_event_exit_legs (batch_id, policy_id, signal_date);

CREATE INDEX IF NOT EXISTS idx_copytrade_leg_attrib_cases_batch
    ON copytrade_leg_attribution_cases (batch_id, case_type, signal_date);

CREATE INDEX IF NOT EXISTS idx_copytrade_leg_attrib_hyp_batch
    ON copytrade_leg_attribution_hypotheses (batch_id, hypothesis_id);

CREATE INDEX IF NOT EXISTS idx_copytrade_leg_attrib_corr_batch
    ON copytrade_leg_attribution_correlations (batch_id, feature);

CREATE INDEX IF NOT EXISTS idx_copytrade_leg_decay_knees_batch
    ON copytrade_leg_decay_knees (batch_id, bucket_field, bucket_value);

CREATE INDEX IF NOT EXISTS idx_copytrade_regime_labels_batch
    ON copytrade_regime_signal_labels (batch_id, signal_date);

CREATE INDEX IF NOT EXISTS idx_copytrade_regime_sweet_batch
    ON copytrade_regime_sweet_spots (batch_id, bucket_field);

CREATE TABLE IF NOT EXISTS copytrade_nlegs_filter_compare (
    batch_id TEXT NOT NULL,
    etf_code TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    filter_id TEXT NOT NULL,
    filter_label TEXT,
    capital_ntd REAL NOT NULL,
    entry_lag_days INTEGER NOT NULL,
    hold_trading_days INTEGER NOT NULL,
    n_complete_days INTEGER NOT NULL DEFAULT 0,
    n_multi_leg_days INTEGER NOT NULL DEFAULT 0,
    n_legs INTEGER NOT NULL DEFAULT 0,
    n_signal_days_in_filter INTEGER NOT NULL DEFAULT 0,
    n_signal_days_excluded INTEGER NOT NULL DEFAULT 0,
    total_pnl_ntd REAL,
    total_alpha_ntd REAL,
    avg_day_return_pct REAL,
    win_rate_gross_pct REAL,
    win_rate_vs_bench_pct REAL,
    win_rate_alpha_pct REAL,
    recycled_n_cycles INTEGER,
    recycled_total_alpha_ntd REAL,
    recycled_total_pnl_ntd REAL,
    mean_excess_pct REAL,
    p_value_ttest REAL,
    p_value_wilcoxon REAL,
    leg_win_rate_gross_pct REAL,
    leg_n_complete INTEGER NOT NULL DEFAULT 0,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (batch_id, strategy_id, filter_id)
);

CREATE INDEX IF NOT EXISTS idx_copytrade_nlegs_cmp_batch
    ON copytrade_nlegs_filter_compare (batch_id, strategy_id);
"""

COPYTRADE_VERSION = "copytrade-v4"


def ensure_copytrade_schema(conn: sqlite3.Connection) -> None:
    """Ensure all copytrade_* tables and indexes exist."""
    conn.executescript(COPYTRADE_SCHEMA_SQL)
    _migrate_copytrade_leg_decay_knees(conn)
    _migrate_copytrade_trend_posture_columns(conn)
    conn.commit()


def _migrate_copytrade_trend_posture_columns(conn: sqlite3.Connection) -> None:
    renames = (
        ("copytrade_regime_signal_labels", "regime_name", "trend_posture"),
        ("copytrade_regime_signal_labels", "regime_score", "trend_posture_score"),
        ("copytrade_leg_attribution_cases", "regime_name", "trend_posture"),
    )
    for table, old_col, new_col in renames:
        try:
            cols = {
                r["name"]
                for r in conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
        except sqlite3.OperationalError:
            continue
        if cols and old_col in cols and new_col not in cols:
            conn.execute(
                f"ALTER TABLE {table} RENAME COLUMN {old_col} TO {new_col}"
            )


def _migrate_copytrade_leg_decay_knees(conn: sqlite3.Connection) -> None:
    cols = {
        r["name"]
        for r in conn.execute("PRAGMA table_info(copytrade_leg_decay_knees)").fetchall()
    }
    if not cols:
        return
    alters = {
        "marginal_knee_h": "INTEGER",
        "efficiency_h": "INTEGER",
        "efficiency_alpha_per_day": "REAL",
    }
    for name, typ in alters.items():
        if name not in cols:
            conn.execute(
                f"ALTER TABLE copytrade_leg_decay_knees ADD COLUMN {name} {typ}"
            )


def persist_copytrade_bundle(
    conn: sqlite3.Connection,
    *,
    run_row: dict,
    signal_days: list[dict],
    legs: list[dict],
) -> str:
    """寫入一筆跟單回測 run（同 run_id 先刪後插）。"""
    run_id = str(run_row["run_id"])
    synced_at = utc_now_iso()
    conn.execute("DELETE FROM copytrade_legs WHERE run_id = ?", (run_id,))
    conn.execute("DELETE FROM copytrade_signal_days WHERE run_id = ?", (run_id,))
    conn.execute("DELETE FROM copytrade_runs WHERE run_id = ?", (run_id,))

    defaults = {
        "strategy_label": None,
        "window_start": None,
        "window_end": None,
        "copytrade_version": COPYTRADE_VERSION,
        "n_signal_days": 0,
        "n_complete_days": 0,
        "total_deployed_ntd": None,
        "total_pnl_ntd": None,
        "total_return_pct": None,
        "avg_day_return_pct": None,
        "win_rate_pct": None,
        "max_drawdown_pct": None,
        "total_bench_return_pct": None,
        "total_alpha_ntd": None,
        "total_capm_alpha_ntd": None,
        "mean_excess_pct": None,
        "p_value_ttest": None,
        "p_value_wilcoxon": None,
        "t_stat": None,
        "batch_id": None,
        "message": None,
        "entry_price_mode": "open",
        "cost_bps": 0,
    }
    payload = {**defaults, **run_row, "synced_at": synced_at}
    conn.execute(
        """
        INSERT INTO copytrade_runs (
            run_id, etf_code, strategy_id, strategy_label,
            capital_ntd, entry_lag_days, hold_trading_days, entry_price_mode, cost_bps,
            window_start, window_end, copytrade_version,
            n_signal_days, n_complete_days,
            total_deployed_ntd, total_pnl_ntd, total_return_pct,
            avg_day_return_pct, win_rate_pct, max_drawdown_pct,
            total_bench_return_pct, total_alpha_ntd, total_capm_alpha_ntd,
            mean_excess_pct, p_value_ttest, p_value_wilcoxon, t_stat, batch_id,
            message, synced_at
        ) VALUES (
            :run_id, :etf_code, :strategy_id, :strategy_label,
            :capital_ntd, :entry_lag_days, :hold_trading_days, :entry_price_mode, :cost_bps,
            :window_start, :window_end, :copytrade_version,
            :n_signal_days, :n_complete_days,
            :total_deployed_ntd, :total_pnl_ntd, :total_return_pct,
            :avg_day_return_pct, :win_rate_pct, :max_drawdown_pct,
            :total_bench_return_pct, :total_alpha_ntd, :total_capm_alpha_ntd,
            :mean_excess_pct, :p_value_ttest, :p_value_wilcoxon, :t_stat, :batch_id,
            :message, :synced_at
        )
        """,
        payload,
    )

    if signal_days:
        rows = [{**r, "run_id": run_id, "synced_at": synced_at} for r in signal_days]
        conn.executemany(
            """
            INSERT INTO copytrade_signal_days (
                run_id, signal_date, entry_date, exit_date,
                n_legs, deployed_ntd, pnl_ntd, return_pct,
                bench_return_pct, alpha_ntd, capm_alpha_ntd, portfolio_beta,
                status, synced_at
            ) VALUES (
                :run_id, :signal_date, :entry_date, :exit_date,
                :n_legs, :deployed_ntd, :pnl_ntd, :return_pct,
                :bench_return_pct, :alpha_ntd, :capm_alpha_ntd, :portfolio_beta,
                :status, :synced_at
            )
            """,
            rows,
        )

    if legs:
        leg_rows = [{**r, "run_id": run_id, "synced_at": synced_at} for r in legs]
        conn.executemany(
            """
            INSERT INTO copytrade_legs (
                run_id, signal_date, stock_id, stock_name, action,
                share_delta, weight_delta, entry_date, exit_date,
                entry_px, exit_px, allocated_ntd, pnl_ntd,
                return_pct, gross_return_pct, status, synced_at
            ) VALUES (
                :run_id, :signal_date, :stock_id, :stock_name, :action,
                :share_delta, :weight_delta, :entry_date, :exit_date,
                :entry_px, :exit_px, :allocated_ntd, :pnl_ntd,
                :return_pct, :gross_return_pct, :status, :synced_at
            )
            """,
            leg_rows,
        )

    conn.commit()
    return run_id


def load_copytrade_runs(
    conn: sqlite3.Connection,
    *,
    etf_code: str | None = None,
) -> list[sqlite3.Row]:
    if etf_code:
        return conn.execute(
            """
            SELECT * FROM copytrade_runs
            WHERE etf_code = ?
            ORDER BY synced_at DESC, strategy_id
            """,
            (etf_code,),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM copytrade_runs ORDER BY synced_at DESC, etf_code, strategy_id"
    ).fetchall()


def load_copytrade_signal_days_for_run(
    conn: sqlite3.Connection,
    run_id: str,
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT * FROM copytrade_signal_days
        WHERE run_id = ?
        ORDER BY signal_date ASC
        """,
        (run_id,),
    ).fetchall()


def load_copytrade_legs_for_run(
    conn: sqlite3.Connection,
    run_id: str,
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT * FROM copytrade_legs
        WHERE run_id = ?
        ORDER BY signal_date ASC, stock_id
        """,
        (run_id,),
    ).fetchall()


def persist_copytrade_horizon_decay(
    conn: sqlite3.Connection,
    batch_id: str,
    rows: list[dict],
) -> int:
    """批次寫入 L×H decay 顯著性表（同 batch_id 先刪後插）。"""
    if not rows:
        return 0
    synced_at = utc_now_iso()
    conn.execute(
        "DELETE FROM copytrade_horizon_decay WHERE batch_id = ?",
        (batch_id,),
    )
    payload = [{**r, "batch_id": batch_id, "synced_at": synced_at} for r in rows]
    conn.executemany(
        """
        INSERT INTO copytrade_horizon_decay (
            batch_id, etf_code, entry_row, horizon, strategy_id, run_id,
            n_complete, total_pnl_ntd, total_alpha_ntd, total_capm_alpha_ntd,
            mean_excess_pct, p_value_ttest, p_value_wilcoxon, t_stat, synced_at
        ) VALUES (
            :batch_id, :etf_code, :entry_row, :horizon, :strategy_id, :run_id,
            :n_complete, :total_pnl_ntd, :total_alpha_ntd, :total_capm_alpha_ntd,
            :mean_excess_pct, :p_value_ttest, :p_value_wilcoxon, :t_stat, :synced_at
        )
        """,
        payload,
    )
    conn.commit()
    return len(payload)


def load_copytrade_horizon_decay(
    conn: sqlite3.Connection,
    batch_id: str,
    *,
    entry_row: str | None = None,
) -> list[sqlite3.Row]:
    if entry_row:
        return conn.execute(
            """
            SELECT * FROM copytrade_horizon_decay
            WHERE batch_id = ? AND entry_row = ?
            ORDER BY horizon ASC
            """,
            (batch_id, entry_row),
        ).fetchall()
    return conn.execute(
        """
        SELECT * FROM copytrade_horizon_decay
        WHERE batch_id = ?
        ORDER BY entry_row ASC, horizon ASC
        """,
        (batch_id,),
    ).fetchall()


def persist_copytrade_capital_cycle(
    conn: sqlite3.Connection,
    batch_id: str,
    rows: list[dict],
) -> None:
    """寫入有限資金週轉分析（同 batch 先刪後插）。"""
    synced_at = utc_now_iso()
    conn.execute(
        "DELETE FROM copytrade_capital_cycle WHERE batch_id = ?",
        (batch_id,),
    )
    if not rows:
        conn.commit()
        return
    conn.executemany(
        """
        INSERT INTO copytrade_capital_cycle (
            batch_id, etf_code, entry_row, horizon, capital_ntd,
            strategy_id, run_id, n_signals,
            unconstrained_total_alpha_ntd, unconstrained_alpha_per_day,
            marginal_unconstrained_alpha_ntd,
            p_value_wilcoxon, is_significant,
            recycled_n_cycles, recycled_total_alpha_ntd, recycled_total_pnl_ntd,
            recycled_locked_days, alpha_per_locked_day, alpha_per_cycle,
            signal_capture_pct, marginal_recycled_alpha_ntd, synced_at
        ) VALUES (
            :batch_id, :etf_code, :entry_row, :horizon, :capital_ntd,
            :strategy_id, :run_id, :n_signals,
            :unconstrained_total_alpha_ntd, :unconstrained_alpha_per_day,
            :marginal_unconstrained_alpha_ntd,
            :p_value_wilcoxon, :is_significant,
            :recycled_n_cycles, :recycled_total_alpha_ntd, :recycled_total_pnl_ntd,
            :recycled_locked_days, :alpha_per_locked_day, :alpha_per_cycle,
            :signal_capture_pct, :marginal_recycled_alpha_ntd, :synced_at
        )
        """,
        [{**r, "batch_id": batch_id, "synced_at": synced_at} for r in rows],
    )
    conn.commit()


def persist_copytrade_capital_slots(
    conn: sqlite3.Connection,
    batch_id: str,
    rows: list[dict],
) -> None:
    """寫入固定槽位資金分析（同 batch + slots_mode 先刪後插）。"""
    synced_at = utc_now_iso()
    modes = {str(r.get("slots_mode") or "fixed") for r in rows}
    for mode in modes:
        conn.execute(
            """
            DELETE FROM copytrade_capital_slots
            WHERE batch_id = ? AND slots_mode = ?
            """,
            (batch_id, mode),
        )
    if not rows:
        conn.commit()
        return
    conn.executemany(
        """
        INSERT INTO copytrade_capital_slots (
            batch_id, etf_code, entry_row, horizon, capital_ntd,
            n_slots, per_signal_ntd, slots_mode,
            strategy_id, run_id, n_signals,
            unconstrained_total_alpha_ntd,
            p_value_wilcoxon, is_significant,
            recycled_n_cycles, recycled_total_alpha_ntd, recycled_total_pnl_ntd,
            recycled_locked_days, alpha_per_locked_day, alpha_per_cycle,
            signal_capture_pct, peak_concurrent_slots,
            marginal_recycled_alpha_ntd, synced_at
        ) VALUES (
            :batch_id, :etf_code, :entry_row, :horizon, :capital_ntd,
            :n_slots, :per_signal_ntd, :slots_mode,
            :strategy_id, :run_id, :n_signals,
            :unconstrained_total_alpha_ntd,
            :p_value_wilcoxon, :is_significant,
            :recycled_n_cycles, :recycled_total_alpha_ntd, :recycled_total_pnl_ntd,
            :recycled_locked_days, :alpha_per_locked_day, :alpha_per_cycle,
            :signal_capture_pct, :peak_concurrent_slots,
            :marginal_recycled_alpha_ntd, :synced_at
        )
        """,
        [{**r, "batch_id": batch_id, "synced_at": synced_at} for r in rows],
    )
    conn.commit()


def load_copytrade_capital_slots(
    conn: sqlite3.Connection,
    batch_id: str,
    *,
    entry_row: str | None = None,
    slots_mode: str | None = None,
) -> list[sqlite3.Row]:
    clauses = ["batch_id = ?"]
    params: list[object] = [batch_id]
    if entry_row:
        clauses.append("entry_row = ?")
        params.append(entry_row)
    if slots_mode:
        clauses.append("slots_mode = ?")
        params.append(slots_mode)
    where = " AND ".join(clauses)
    return conn.execute(
        f"""
        SELECT * FROM copytrade_capital_slots
        WHERE {where}
        ORDER BY entry_row ASC, horizon ASC
        """,
        params,
    ).fetchall()


def persist_copytrade_regime_horizon(
    conn: sqlite3.Connection,
    batch_id: str,
    summary_rows: list[dict],
    *,
    label_rows: list[dict] | None = None,
    sweet_spots: list[dict] | None = None,
) -> None:
    synced_at = utc_now_iso()
    conn.execute("DELETE FROM copytrade_regime_horizon WHERE batch_id = ?", (batch_id,))
    conn.execute(
        "DELETE FROM copytrade_regime_signal_labels WHERE batch_id = ?", (batch_id,)
    )
    conn.execute(
        "DELETE FROM copytrade_regime_sweet_spots WHERE batch_id = ?", (batch_id,)
    )
    if summary_rows:
        conn.executemany(
            """
            INSERT INTO copytrade_regime_horizon (
                batch_id, etf_code, entry_row, bucket_field, bucket_value,
                horizon, n_signal_days, total_alpha_ntd, mean_excess_pct,
                p_value_wilcoxon, is_significant, marginal_total_alpha_ntd, synced_at
            ) VALUES (
                :batch_id, :etf_code, :entry_row, :bucket_field, :bucket_value,
                :horizon, :n_signal_days, :total_alpha_ntd, :mean_excess_pct,
                :p_value_wilcoxon, :is_significant, :marginal_total_alpha_ntd, :synced_at
            )
            """,
            [{**r, "batch_id": batch_id, "synced_at": synced_at} for r in summary_rows],
        )
    if label_rows:
        conn.executemany(
            """
            INSERT INTO copytrade_regime_signal_labels (
                batch_id, signal_date, trend_posture, exposure_decision,
                trend_posture_score, top_risk_score, composite_score,
                ix_stage, ix_trend_score, tx_gap_pct, synced_at
            ) VALUES (
                :batch_id, :signal_date, :trend_posture, :exposure_decision,
                :trend_posture_score, :top_risk_score, :composite_score,
                :ix_stage, :ix_trend_score, :tx_gap_pct, :synced_at
            )
            """,
            [{**r, "batch_id": batch_id, "synced_at": synced_at} for r in label_rows],
        )
    if sweet_spots:
        conn.executemany(
            """
            INSERT INTO copytrade_regime_sweet_spots (
                batch_id, bucket_field, bucket_value,
                sweet_spot_h, sweet_spot_total_alpha_ntd,
                n_signal_days_at_sweet, hold_through_h, mean_excess_at_sweet, synced_at
            ) VALUES (
                :batch_id, :bucket_field, :bucket_value,
                :sweet_spot_h, :sweet_spot_total_alpha_ntd,
                :n_signal_days_at_sweet, :hold_through_h, :mean_excess_at_sweet, :synced_at
            )
            """,
            [{**r, "batch_id": batch_id, "synced_at": synced_at} for r in sweet_spots],
        )
    conn.commit()


def persist_copytrade_leg_attribution(
    conn: sqlite3.Connection,
    batch_id: str,
    *,
    bucket_rows: list[dict],
    hypotheses: list[dict],
    correlations: list[dict],
    case_rows: list[dict],
    meta: dict | None = None,
) -> None:
    synced_at = utc_now_iso()
    for table in (
        "copytrade_leg_attribution_buckets",
        "copytrade_leg_attribution_hypotheses",
        "copytrade_leg_attribution_correlations",
        "copytrade_leg_attribution_cases",
    ):
        conn.execute(f"DELETE FROM {table} WHERE batch_id = ?", (batch_id,))
    if bucket_rows:
        conn.executemany(
            """
            INSERT INTO copytrade_leg_attribution_buckets (
                batch_id, etf_code, strategy_id, bucket_field, bucket_value,
                n_legs, mean_return_pct, mean_excess_pct, mean_alpha_ntd,
                sum_alpha_ntd, win_rate_return_pct, win_rate_excess_pct,
                p_value_wilcoxon_excess, synced_at
            ) VALUES (
                :batch_id, :etf_code, :strategy_id, :bucket_field, :bucket_value,
                :n_legs, :mean_return_pct, :mean_excess_pct, :mean_alpha_ntd,
                :sum_alpha_ntd, :win_rate_return_pct, :win_rate_excess_pct,
                :p_value_wilcoxon_excess, :synced_at
            )
            """,
            [{**r, "batch_id": batch_id, "synced_at": synced_at} for r in bucket_rows],
        )
    if hypotheses:
        conn.executemany(
            """
            INSERT INTO copytrade_leg_attribution_hypotheses (
                batch_id, hypothesis_id, label, verdict,
                n_a, n_b, mean_excess_a, mean_excess_b,
                p_value_wilcoxon, summary_zh, synced_at
            ) VALUES (
                :batch_id, :hypothesis_id, :label, :verdict,
                :n_a, :n_b, :mean_excess_a, :mean_excess_b,
                :p_value_wilcoxon, :summary_zh, :synced_at
            )
            """,
            [{**h, "batch_id": batch_id, "synced_at": synced_at} for h in hypotheses],
        )
    if correlations:
        conn.executemany(
            """
            INSERT INTO copytrade_leg_attribution_correlations (
                batch_id, feature, n, pearson_r, synced_at
            ) VALUES (
                :batch_id, :feature, :n, :pearson_r, :synced_at
            )
            """,
            [{**c, "batch_id": batch_id, "synced_at": synced_at} for c in correlations],
        )
    case_db_rows: list[dict] = []
    for cr in case_rows:
        if "stock_id" in cr:
            case_db_rows.append(
                {
                    "batch_id": batch_id,
                    "case_type": cr["case_type"],
                    "signal_date": cr["signal_date"],
                    "stock_id": cr["stock_id"],
                    "trend_posture": None,
                    "tx_gap_pct": None,
                    "n_legs": None,
                    "day_alpha_ntd": None,
                    "sector": cr.get("sector"),
                    "theme": cr.get("theme"),
                    "return_pct": cr.get("return_pct"),
                    "alpha_ntd": cr.get("alpha_ntd"),
                    "overnight_gap_pct": cr.get("overnight_gap_pct"),
                    "prior_5d_pct": cr.get("prior_5d_pct"),
                    "prior_10d_pct": cr.get("prior_10d_pct"),
                    "position_52w_pct": cr.get("position_52w_pct"),
                    "skip_overextended": cr.get("skip_overextended"),
                    "synced_at": synced_at,
                }
            )
        else:
            case_db_rows.append(
                {
                    "batch_id": batch_id,
                    "case_type": cr["case_type"],
                    "signal_date": cr["signal_date"],
                    "stock_id": "",
                    "trend_posture": cr.get("trend_posture") or cr.get("regime_name"),
                    "tx_gap_pct": cr.get("tx_gap_pct"),
                    "n_legs": cr.get("n_legs"),
                    "day_alpha_ntd": cr.get("day_alpha_ntd"),
                    "sector": None,
                    "theme": None,
                    "return_pct": None,
                    "alpha_ntd": None,
                    "overnight_gap_pct": None,
                    "prior_5d_pct": None,
                    "prior_10d_pct": None,
                    "position_52w_pct": None,
                    "skip_overextended": None,
                    "synced_at": synced_at,
                }
            )
    if case_db_rows:
        conn.executemany(
            """
            INSERT INTO copytrade_leg_attribution_cases (
                batch_id, case_type, signal_date, stock_id,
                trend_posture, tx_gap_pct, n_legs, day_alpha_ntd,
                sector, theme, return_pct, alpha_ntd,
                overnight_gap_pct, prior_5d_pct, prior_10d_pct,
                position_52w_pct, skip_overextended, synced_at
            ) VALUES (
                :batch_id, :case_type, :signal_date, :stock_id,
                :trend_posture, :tx_gap_pct, :n_legs, :day_alpha_ntd,
                :sector, :theme, :return_pct, :alpha_ntd,
                :overnight_gap_pct, :prior_5d_pct, :prior_10d_pct,
                :position_52w_pct, :skip_overextended, :synced_at
            )
            """,
            case_db_rows,
        )
    conn.commit()


def persist_copytrade_etf_compare(
    conn: sqlite3.Connection,
    batch_id: str,
    summary: dict[str, object],
) -> None:
    synced_at = utc_now_iso()
    primary = summary.get("rotation_executed") or summary["all_signals"]
    all_row = summary["all_signals"]
    bh: dict = summary.get("buy_hold") or {}  # type: ignore[assignment]
    conn.execute(
        "DELETE FROM copytrade_etf_compare WHERE batch_id = ? AND strategy_id = ?",
        (batch_id, summary["strategy_id"]),
    )
    conn.execute(
        """
        INSERT INTO copytrade_etf_compare (
            batch_id, etf_code, strategy_id, run_id,
            capital_ntd, per_signal_ntd, hold_trading_days, slots_mode,
            window_start, window_end, verdict,
            n_paired, n_missing_etf, win_rate_pct, mean_diff_return_pct,
            p_value_ttest, p_value_wilcoxon,
            cum_copytrade_pnl_ntd, cum_etf_pnl_ntd, diff_gross_ntd, cum_alpha_tw_ntd,
            n_executed, signal_capture_pct, peak_slots,
            all_n_paired, all_win_rate_pct, all_mean_diff_return_pct,
            all_p_value_wilcoxon, all_diff_gross_ntd,
            bh_entry_date, bh_exit_date, bh_return_pct, bh_pnl_ntd,
            synced_at
        ) VALUES (
            :batch_id, :etf_code, :strategy_id, :run_id,
            :capital_ntd, :per_signal_ntd, :hold_trading_days, :slots_mode,
            :window_start, :window_end, :verdict,
            :n_paired, :n_missing_etf, :win_rate_pct, :mean_diff_return_pct,
            :p_value_ttest, :p_value_wilcoxon,
            :cum_copytrade_pnl_ntd, :cum_etf_pnl_ntd, :diff_gross_ntd, :cum_alpha_tw_ntd,
            :n_executed, :signal_capture_pct, :peak_slots,
            :all_n_paired, :all_win_rate_pct, :all_mean_diff_return_pct,
            :all_p_value_wilcoxon, :all_diff_gross_ntd,
            :bh_entry_date, :bh_exit_date, :bh_return_pct, :bh_pnl_ntd,
            :synced_at
        )
        """,
        {
            "batch_id": batch_id,
            "etf_code": summary["etf_code"],
            "strategy_id": summary["strategy_id"],
            "run_id": summary["run_id"],
            "capital_ntd": summary["capital_ntd"],
            "per_signal_ntd": summary["per_signal_ntd"],
            "hold_trading_days": summary["hold_trading_days"],
            "slots_mode": summary["slots_mode"],
            "window_start": summary.get("window_start"),
            "window_end": summary.get("window_end"),
            "verdict": summary["verdict"],
            "n_paired": primary.n_paired,
            "n_missing_etf": primary.n_missing_etf,
            "win_rate_pct": primary.win_rate_pct,
            "mean_diff_return_pct": primary.mean_diff_return_pct,
            "p_value_ttest": primary.p_value_ttest,
            "p_value_wilcoxon": primary.p_value_wilcoxon,
            "cum_copytrade_pnl_ntd": primary.cum_copytrade_pnl_ntd,
            "cum_etf_pnl_ntd": primary.cum_etf_pnl_ntd,
            "diff_gross_ntd": primary.diff_gross_ntd,
            "cum_alpha_tw_ntd": primary.cum_alpha_tw_ntd,
            "n_executed": primary.n_executed,
            "signal_capture_pct": primary.signal_capture_pct,
            "peak_slots": primary.peak_slots,
            "all_n_paired": all_row.n_paired,
            "all_win_rate_pct": all_row.win_rate_pct,
            "all_mean_diff_return_pct": all_row.mean_diff_return_pct,
            "all_p_value_wilcoxon": all_row.p_value_wilcoxon,
            "all_diff_gross_ntd": all_row.diff_gross_ntd,
            "bh_entry_date": bh.get("entry_date"),
            "bh_exit_date": bh.get("exit_date"),
            "bh_return_pct": bh.get("return_pct"),
            "bh_pnl_ntd": bh.get("pnl_ntd"),
            "synced_at": synced_at,
        },
    )
    conn.commit()


def persist_copytrade_event_exit(
    conn: sqlite3.Connection,
    batch_id: str,
    summaries: list[dict],
    *,
    leg_rows: list[dict] | None = None,
) -> None:
    synced_at = utc_now_iso()
    conn.execute(
        "DELETE FROM copytrade_event_exit_policies WHERE batch_id = ?", (batch_id,)
    )
    conn.execute("DELETE FROM copytrade_event_exit_legs WHERE batch_id = ?", (batch_id,))
    if summaries:
        conn.executemany(
            """
            INSERT INTO copytrade_event_exit_policies (
                batch_id, etf_code, policy_id, policy_label, baseline_h,
                n_legs, n_complete, n_triggered, n_early_exit,
                mean_alpha_ntd, mean_excess_pct, total_alpha_ntd,
                vs_baseline_alpha_delta, mean_paired_alpha_delta,
                p_value_wilcoxon_paired,
                rotation_capital_ntd, rotation_recycled_alpha_ntd,
                rotation_n_cycles, rotation_capture_pct, synced_at
            ) VALUES (
                :batch_id, :etf_code, :policy_id, :policy_label, :baseline_h,
                :n_legs, :n_complete, :n_triggered, :n_early_exit,
                :mean_alpha_ntd, :mean_excess_pct, :total_alpha_ntd,
                :vs_baseline_alpha_delta, :mean_paired_alpha_delta,
                :p_value_wilcoxon_paired,
                :rotation_capital_ntd, :rotation_recycled_alpha_ntd,
                :rotation_n_cycles, :rotation_capture_pct, :synced_at
            )
            """,
            [{**r, "batch_id": batch_id, "synced_at": synced_at} for r in summaries],
        )
    if leg_rows:
        conn.executemany(
            """
            INSERT INTO copytrade_event_exit_legs (
                batch_id, policy_id, signal_date, stock_id, action,
                entry_date, planned_exit_date, actual_exit_date, exit_reason,
                triggered, hold_days, alpha_ntd, baseline_alpha_ntd, status, synced_at
            ) VALUES (
                :batch_id, :policy_id, :signal_date, :stock_id, :action,
                :entry_date, :planned_exit_date, :actual_exit_date, :exit_reason,
                :triggered, :hold_days, :alpha_ntd, :baseline_alpha_ntd, :status, :synced_at
            )
            """,
            [{**r, "batch_id": batch_id, "synced_at": synced_at} for r in leg_rows],
        )
    conn.commit()


def persist_copytrade_leg_decay(
    conn: sqlite3.Connection,
    batch_id: str,
    curve_rows: list[dict],
    *,
    knees: list[dict] | None = None,
) -> None:
    synced_at = utc_now_iso()
    conn.execute("DELETE FROM copytrade_leg_decay WHERE batch_id = ?", (batch_id,))
    conn.execute("DELETE FROM copytrade_leg_decay_knees WHERE batch_id = ?", (batch_id,))
    if curve_rows:
        conn.executemany(
            """
            INSERT INTO copytrade_leg_decay (
                batch_id, etf_code, entry_lag_days, bucket_field, bucket_value,
                horizon, n_legs, mean_excess_pct, mean_alpha_ntd, sum_alpha_ntd,
                marginal_mean_excess_pct, marginal_sum_alpha_ntd,
                p_value_wilcoxon, is_significant, synced_at
            ) VALUES (
                :batch_id, :etf_code, :entry_lag_days, :bucket_field, :bucket_value,
                :horizon, :n_legs, :mean_excess_pct, :mean_alpha_ntd, :sum_alpha_ntd,
                :marginal_mean_excess_pct, :marginal_sum_alpha_ntd,
                :p_value_wilcoxon, :is_significant, :synced_at
            )
            """,
            [{**r, "batch_id": batch_id, "synced_at": synced_at} for r in curve_rows],
        )
    if knees:
        conn.executemany(
            """
            INSERT INTO copytrade_leg_decay_knees (
                batch_id, bucket_field, bucket_value,
                peak_mean_excess_h, peak_mean_excess_pct,
                best_sum_alpha_h, best_sum_alpha_ntd,
                knee_h, marginal_knee_h, efficiency_h, efficiency_alpha_per_day,
                n_legs_at_peak, synced_at
            ) VALUES (
                :batch_id, :bucket_field, :bucket_value,
                :peak_mean_excess_h, :peak_mean_excess_pct,
                :best_sum_alpha_h, :best_sum_alpha_ntd,
                :knee_h, :marginal_knee_h, :efficiency_h, :efficiency_alpha_per_day,
                :n_legs_at_peak, :synced_at
            )
            """,
            [{**r, "batch_id": batch_id, "synced_at": synced_at} for r in knees],
        )
    conn.commit()


def load_copytrade_capital_cycle(
    conn: sqlite3.Connection,
    batch_id: str,
    *,
    entry_row: str | None = None,
) -> list[sqlite3.Row]:
    if entry_row:
        return conn.execute(
            """
            SELECT * FROM copytrade_capital_cycle
            WHERE batch_id = ? AND entry_row = ?
            ORDER BY horizon ASC
            """,
            (batch_id, entry_row),
        ).fetchall()
    return conn.execute(
        """
        SELECT * FROM copytrade_capital_cycle
        WHERE batch_id = ?
        ORDER BY entry_row ASC, horizon ASC
        """,
        (batch_id,),
    ).fetchall()


def persist_copytrade_research_conclusions(
    conn: sqlite3.Connection,
    batch_id: str,
    rows: list[dict],
    *,
    replace_types: tuple[str, ...] | None = None,
) -> None:
    """寫入研究結論；replace_types 指定時只覆寫該類型。"""
    synced_at = utc_now_iso()
    if replace_types:
        placeholders = ",".join("?" * len(replace_types))
        conn.execute(
            f"""
            DELETE FROM copytrade_research_conclusions
            WHERE batch_id = ? AND analysis_type IN ({placeholders})
            """,
            (batch_id, *replace_types),
        )
    else:
        conn.execute(
            "DELETE FROM copytrade_research_conclusions WHERE batch_id = ?",
            (batch_id,),
        )
    if not rows:
        conn.commit()
        return
    conn.executemany(
        """
        INSERT INTO copytrade_research_conclusions (
            batch_id, etf_code, analysis_type, entry_row,
            metric_key, horizon, metric_value, conclusion_zh, details_json, synced_at
        ) VALUES (
            :batch_id, :etf_code, :analysis_type, :entry_row,
            :metric_key, :horizon, :metric_value, :conclusion_zh, :details_json, :synced_at
        )
        """,
        [{**r, "batch_id": batch_id, "synced_at": synced_at} for r in rows],
    )
    conn.commit()


def load_copytrade_research_conclusions(
    conn: sqlite3.Connection,
    batch_id: str,
    *,
    analysis_type: str | None = None,
) -> list[sqlite3.Row]:
    if analysis_type:
        return conn.execute(
            """
            SELECT * FROM copytrade_research_conclusions
            WHERE batch_id = ? AND analysis_type = ?
            ORDER BY entry_row, metric_key
            """,
            (batch_id, analysis_type),
        ).fetchall()
    return conn.execute(
        """
        SELECT * FROM copytrade_research_conclusions
        WHERE batch_id = ?
        ORDER BY analysis_type, entry_row, metric_key
        """,
        (batch_id,),
    ).fetchall()


def persist_copytrade_allocation_compare(
    conn: sqlite3.Connection,
    batch_id: str,
    rows: list[dict],
) -> None:
    synced_at = utc_now_iso()
    conn.execute(
        "DELETE FROM copytrade_allocation_compare WHERE batch_id = ?",
        (batch_id,),
    )
    if not rows:
        conn.commit()
        return
    conn.executemany(
        """
        INSERT INTO copytrade_allocation_compare (
            batch_id, etf_code, strategy_id, allocation_mode, capital_ntd,
            entry_lag_days, hold_trading_days,
            n_complete_days, n_multi_leg_days,
            total_pnl_ntd, total_alpha_ntd, avg_day_return_pct,
            recycled_n_cycles, recycled_total_alpha_ntd, recycled_total_pnl_ntd,
            mean_excess_pct, p_value_ttest, p_value_wilcoxon, synced_at
        ) VALUES (
            :batch_id, :etf_code, :strategy_id, :allocation_mode, :capital_ntd,
            :entry_lag_days, :hold_trading_days,
            :n_complete_days, :n_multi_leg_days,
            :total_pnl_ntd, :total_alpha_ntd, :avg_day_return_pct,
            :recycled_n_cycles, :recycled_total_alpha_ntd, :recycled_total_pnl_ntd,
            :mean_excess_pct, :p_value_ttest, :p_value_wilcoxon, :synced_at
        )
        """,
        [{**r, "batch_id": batch_id, "synced_at": synced_at} for r in rows],
    )
    conn.commit()


def load_copytrade_allocation_compare(
    conn: sqlite3.Connection,
    batch_id: str,
    *,
    strategy_id: str | None = None,
) -> list[sqlite3.Row]:
    if strategy_id:
        return conn.execute(
            """
            SELECT * FROM copytrade_allocation_compare
            WHERE batch_id = ? AND strategy_id = ?
            ORDER BY allocation_mode
            """,
            (batch_id, strategy_id),
        ).fetchall()
    return conn.execute(
        """
        SELECT * FROM copytrade_allocation_compare
        WHERE batch_id = ?
        ORDER BY strategy_id, allocation_mode
        """,
        (batch_id,),
    ).fetchall()


def persist_copytrade_nlegs_filter_compare(
    conn: sqlite3.Connection,
    batch_id: str,
    rows: list[dict],
) -> None:
    synced_at = utc_now_iso()
    conn.execute(
        "DELETE FROM copytrade_nlegs_filter_compare WHERE batch_id = ?",
        (batch_id,),
    )
    if not rows:
        conn.commit()
        return
    conn.executemany(
        """
        INSERT INTO copytrade_nlegs_filter_compare (
            batch_id, etf_code, strategy_id, filter_id, filter_label,
            capital_ntd, entry_lag_days, hold_trading_days,
            n_complete_days, n_multi_leg_days, n_legs,
            n_signal_days_in_filter, n_signal_days_excluded,
            total_pnl_ntd, total_alpha_ntd, avg_day_return_pct,
            win_rate_gross_pct, win_rate_vs_bench_pct, win_rate_alpha_pct,
            recycled_n_cycles, recycled_total_alpha_ntd, recycled_total_pnl_ntd,
            mean_excess_pct, p_value_ttest, p_value_wilcoxon,
            leg_win_rate_gross_pct, leg_n_complete, synced_at
        ) VALUES (
            :batch_id, :etf_code, :strategy_id, :filter_id, :filter_label,
            :capital_ntd, :entry_lag_days, :hold_trading_days,
            :n_complete_days, :n_multi_leg_days, :n_legs,
            :n_signal_days_in_filter, :n_signal_days_excluded,
            :total_pnl_ntd, :total_alpha_ntd, :avg_day_return_pct,
            :win_rate_gross_pct, :win_rate_vs_bench_pct, :win_rate_alpha_pct,
            :recycled_n_cycles, :recycled_total_alpha_ntd, :recycled_total_pnl_ntd,
            :mean_excess_pct, :p_value_ttest, :p_value_wilcoxon,
            :leg_win_rate_gross_pct, :leg_n_complete, :synced_at
        )
        """,
        [{**r, "batch_id": batch_id, "synced_at": synced_at} for r in rows],
    )
    conn.commit()


def load_copytrade_nlegs_filter_compare(
    conn: sqlite3.Connection,
    batch_id: str,
    *,
    strategy_id: str | None = None,
) -> list[sqlite3.Row]:
    if strategy_id:
        return conn.execute(
            """
            SELECT * FROM copytrade_nlegs_filter_compare
            WHERE batch_id = ? AND strategy_id = ?
            ORDER BY filter_id
            """,
            (batch_id, strategy_id),
        ).fetchall()
    return conn.execute(
        """
        SELECT * FROM copytrade_nlegs_filter_compare
        WHERE batch_id = ?
        ORDER BY strategy_id, filter_id
        """,
        (batch_id,),
    ).fetchall()


def load_copytrade_action_compare(
    conn: sqlite3.Connection,
    batch_id: str,
    *,
    strategy_id: str | None = None,
) -> list[sqlite3.Row]:
    if strategy_id:
        return conn.execute(
            """
            SELECT * FROM copytrade_action_compare
            WHERE batch_id = ? AND strategy_id = ?
            ORDER BY action_filter
            """,
            (batch_id, strategy_id),
        ).fetchall()
    return conn.execute(
        """
        SELECT * FROM copytrade_action_compare
        WHERE batch_id = ?
        ORDER BY strategy_id, action_filter
        """,
        (batch_id,),
    ).fetchall()


def load_copytrade_gap_filter_compare(
    conn: sqlite3.Connection,
    batch_id: str,
    *,
    strategy_id: str | None = None,
) -> list[sqlite3.Row]:
    if strategy_id:
        return conn.execute(
            """
            SELECT * FROM copytrade_gap_filter_compare
            WHERE batch_id = ? AND strategy_id = ?
            ORDER BY filter_id
            """,
            (batch_id, strategy_id),
        ).fetchall()
    return conn.execute(
        """
        SELECT * FROM copytrade_gap_filter_compare
        WHERE batch_id = ?
        ORDER BY strategy_id, filter_id
        """,
        (batch_id,),
    ).fetchall()


def load_copytrade_leg_chip_snapshots(
    conn: sqlite3.Connection,
    etf_code: str,
    *,
    status: str | None = "complete",
) -> list[sqlite3.Row]:
    if status:
        return conn.execute(
            """
            SELECT * FROM copytrade_leg_chip_snapshots
            WHERE etf_code = ? AND status = ?
            ORDER BY signal_date, stock_id
            """,
            (etf_code, status),
        ).fetchall()
    return conn.execute(
        """
        SELECT * FROM copytrade_leg_chip_snapshots
        WHERE etf_code = ?
        ORDER BY signal_date, stock_id
        """,
        (etf_code,),
    ).fetchall()


def load_copytrade_leg_confluence_snapshots(
    conn: sqlite3.Connection,
    etf_code: str,
    *,
    status: str | None = "complete",
) -> list[sqlite3.Row]:
    if status:
        return conn.execute(
            """
            SELECT * FROM copytrade_leg_confluence_snapshots
            WHERE etf_code = ? AND status = ?
            ORDER BY signal_date, stock_id
            """,
            (etf_code, status),
        ).fetchall()
    return conn.execute(
        """
        SELECT * FROM copytrade_leg_confluence_snapshots
        WHERE etf_code = ?
        ORDER BY signal_date, stock_id
        """,
        (etf_code,),
    ).fetchall()


def load_copytrade_leg_conviction_snapshots(
    conn: sqlite3.Connection,
    etf_code: str,
    *,
    status: str | None = "complete",
) -> list[sqlite3.Row]:
    if status:
        return conn.execute(
            """
            SELECT * FROM copytrade_leg_conviction_snapshots
            WHERE etf_code = ? AND status = ?
            ORDER BY signal_date, stock_id
            """,
            (etf_code, status),
        ).fetchall()
    return conn.execute(
        """
        SELECT * FROM copytrade_leg_conviction_snapshots
        WHERE etf_code = ?
        ORDER BY signal_date, stock_id
        """,
        (etf_code,),
    ).fetchall()


def load_copytrade_leg_limit_entry(
    conn: sqlite3.Connection,
    etf_code: str,
    discount_pct: float,
    *,
    status: str | None = "complete",
) -> list[sqlite3.Row]:
    if status:
        return conn.execute(
            """
            SELECT * FROM copytrade_leg_limit_entry
            WHERE etf_code = ? AND discount_pct = ? AND status = ?
            ORDER BY signal_date, stock_id
            """,
            (etf_code, discount_pct, status),
        ).fetchall()
    return conn.execute(
        """
        SELECT * FROM copytrade_leg_limit_entry
        WHERE etf_code = ? AND discount_pct = ?
        ORDER BY signal_date, stock_id
        """,
        (etf_code, discount_pct),
    ).fetchall()


def load_copytrade_leg_opening_confirm(
    conn: sqlite3.Connection,
    etf_code: str,
    *,
    status: str | None = "complete",
) -> list[sqlite3.Row]:
    if status:
        return conn.execute(
            """
            SELECT * FROM copytrade_leg_opening_confirm
            WHERE etf_code = ? AND status = ?
            ORDER BY signal_date, stock_id
            """,
            (etf_code, status),
        ).fetchall()
    return conn.execute(
        """
        SELECT * FROM copytrade_leg_opening_confirm
        WHERE etf_code = ?
        ORDER BY signal_date, stock_id
        """,
        (etf_code,),
    ).fetchall()


def load_copytrade_macro_filter_compare(
    conn: sqlite3.Connection,
    batch_id: str,
    *,
    strategy_id: str | None = None,
) -> list[sqlite3.Row]:
    if strategy_id:
        return conn.execute(
            """
            SELECT * FROM copytrade_macro_filter_compare
            WHERE batch_id = ? AND strategy_id = ?
            ORDER BY filter_id
            """,
            (batch_id, strategy_id),
        ).fetchall()
    return conn.execute(
        """
        SELECT * FROM copytrade_macro_filter_compare
        WHERE batch_id = ?
        ORDER BY strategy_id, filter_id
        """,
        (batch_id,),
    ).fetchall()


def load_copytrade_macro_gap_snapshots(
    conn: sqlite3.Connection,
    *,
    entry_date: str | None = None,
) -> list[sqlite3.Row]:
    if entry_date:
        row = conn.execute(
            "SELECT * FROM copytrade_macro_gap_snapshots WHERE entry_date = ?",
            (entry_date,),
        ).fetchone()
        return [row] if row else []
    return conn.execute(
        "SELECT * FROM copytrade_macro_gap_snapshots ORDER BY entry_date"
    ).fetchall()


def load_copytrade_overnight_gaps(
    conn: sqlite3.Connection,
    etf_code: str,
    *,
    entry_lag_days: int = 0,
    status: str | None = "complete",
) -> list[sqlite3.Row]:
    if status:
        return conn.execute(
            """
            SELECT * FROM copytrade_leg_overnight_gaps
            WHERE etf_code = ? AND entry_lag_days = ? AND status = ?
            ORDER BY signal_date, stock_id
            """,
            (etf_code, entry_lag_days, status),
        ).fetchall()
    return conn.execute(
        """
        SELECT * FROM copytrade_leg_overnight_gaps
        WHERE etf_code = ? AND entry_lag_days = ?
        ORDER BY signal_date, stock_id
        """,
        (etf_code, entry_lag_days),
    ).fetchall()


def persist_copytrade_action_compare(
    conn: sqlite3.Connection,
    batch_id: str,
    rows: list[dict],
) -> None:
    synced_at = utc_now_iso()
    conn.execute(
        "DELETE FROM copytrade_action_compare WHERE batch_id = ?",
        (batch_id,),
    )
    if not rows:
        conn.commit()
        return
    conn.executemany(
        """
        INSERT INTO copytrade_action_compare (
            batch_id, etf_code, strategy_id, action_filter,
            capital_ntd, entry_lag_days, hold_trading_days,
            n_complete_days, n_multi_leg_days, n_legs,
            total_pnl_ntd, total_alpha_ntd, avg_day_return_pct,
            win_rate_gross_pct, win_rate_vs_bench_pct, win_rate_alpha_pct,
            recycled_n_cycles, recycled_total_alpha_ntd, recycled_total_pnl_ntd,
            mean_excess_pct, p_value_ttest, p_value_wilcoxon, synced_at
        ) VALUES (
            :batch_id, :etf_code, :strategy_id, :action_filter,
            :capital_ntd, :entry_lag_days, :hold_trading_days,
            :n_complete_days, :n_multi_leg_days, :n_legs,
            :total_pnl_ntd, :total_alpha_ntd, :avg_day_return_pct,
            :win_rate_gross_pct, :win_rate_vs_bench_pct, :win_rate_alpha_pct,
            :recycled_n_cycles, :recycled_total_alpha_ntd, :recycled_total_pnl_ntd,
            :mean_excess_pct, :p_value_ttest, :p_value_wilcoxon, :synced_at
        )
        """,
        [{**r, "batch_id": batch_id, "synced_at": synced_at} for r in rows],
    )
    conn.commit()


def persist_copytrade_chip_filter_compare(
    conn: sqlite3.Connection,
    batch_id: str,
    rows: list[dict],
) -> None:
    synced_at = utc_now_iso()
    conn.execute(
        "DELETE FROM copytrade_chip_filter_compare WHERE batch_id = ?",
        (batch_id,),
    )
    if not rows:
        conn.commit()
        return
    conn.executemany(
        """
        INSERT INTO copytrade_chip_filter_compare (
            batch_id, etf_code, strategy_id, filter_id, filter_label,
            capital_ntd, entry_lag_days, hold_trading_days,
            n_complete_days, n_multi_leg_days, n_legs, n_legs_with_chip,
            total_pnl_ntd, total_alpha_ntd, avg_day_return_pct,
            win_rate_gross_pct, win_rate_vs_bench_pct, win_rate_alpha_pct,
            recycled_n_cycles, recycled_total_alpha_ntd, recycled_total_pnl_ntd,
            mean_excess_pct, p_value_ttest, p_value_wilcoxon,
            leg_win_rate_gross_pct, leg_n_complete, synced_at
        ) VALUES (
            :batch_id, :etf_code, :strategy_id, :filter_id, :filter_label,
            :capital_ntd, :entry_lag_days, :hold_trading_days,
            :n_complete_days, :n_multi_leg_days, :n_legs, :n_legs_with_chip,
            :total_pnl_ntd, :total_alpha_ntd, :avg_day_return_pct,
            :win_rate_gross_pct, :win_rate_vs_bench_pct, :win_rate_alpha_pct,
            :recycled_n_cycles, :recycled_total_alpha_ntd, :recycled_total_pnl_ntd,
            :mean_excess_pct, :p_value_ttest, :p_value_wilcoxon,
            :leg_win_rate_gross_pct, :leg_n_complete, :synced_at
        )
        """,
        [{**r, "batch_id": batch_id, "synced_at": synced_at} for r in rows],
    )
    conn.commit()


def persist_copytrade_confluence_filter_compare(
    conn: sqlite3.Connection,
    batch_id: str,
    rows: list[dict],
) -> None:
    synced_at = utc_now_iso()
    conn.execute(
        "DELETE FROM copytrade_confluence_filter_compare WHERE batch_id = ?",
        (batch_id,),
    )
    if not rows:
        conn.commit()
        return
    conn.executemany(
        """
        INSERT INTO copytrade_confluence_filter_compare (
            batch_id, etf_code, strategy_id, filter_id, filter_label,
            capital_ntd, entry_lag_days, hold_trading_days,
            n_complete_days, n_multi_leg_days, n_legs, n_legs_with_confluence,
            total_pnl_ntd, total_alpha_ntd, avg_day_return_pct,
            win_rate_gross_pct, win_rate_vs_bench_pct, win_rate_alpha_pct,
            recycled_n_cycles, recycled_total_alpha_ntd, recycled_total_pnl_ntd,
            mean_excess_pct, p_value_ttest, p_value_wilcoxon,
            leg_win_rate_gross_pct, leg_n_complete, synced_at
        ) VALUES (
            :batch_id, :etf_code, :strategy_id, :filter_id, :filter_label,
            :capital_ntd, :entry_lag_days, :hold_trading_days,
            :n_complete_days, :n_multi_leg_days, :n_legs, :n_legs_with_confluence,
            :total_pnl_ntd, :total_alpha_ntd, :avg_day_return_pct,
            :win_rate_gross_pct, :win_rate_vs_bench_pct, :win_rate_alpha_pct,
            :recycled_n_cycles, :recycled_total_alpha_ntd, :recycled_total_pnl_ntd,
            :mean_excess_pct, :p_value_ttest, :p_value_wilcoxon,
            :leg_win_rate_gross_pct, :leg_n_complete, :synced_at
        )
        """,
        [{**r, "batch_id": batch_id, "synced_at": synced_at} for r in rows],
    )
    conn.commit()


def persist_copytrade_conviction_filter_compare(
    conn: sqlite3.Connection,
    batch_id: str,
    rows: list[dict],
) -> None:
    synced_at = utc_now_iso()
    conn.execute(
        "DELETE FROM copytrade_conviction_filter_compare WHERE batch_id = ?",
        (batch_id,),
    )
    if not rows:
        conn.commit()
        return
    conn.executemany(
        """
        INSERT INTO copytrade_conviction_filter_compare (
            batch_id, etf_code, strategy_id, filter_id, filter_label,
            capital_ntd, entry_lag_days, hold_trading_days,
            n_complete_days, n_multi_leg_days, n_legs, n_legs_with_conviction,
            total_pnl_ntd, total_alpha_ntd, avg_day_return_pct,
            win_rate_gross_pct, win_rate_vs_bench_pct, win_rate_alpha_pct,
            recycled_n_cycles, recycled_total_alpha_ntd, recycled_total_pnl_ntd,
            mean_excess_pct, p_value_ttest, p_value_wilcoxon,
            leg_win_rate_gross_pct, leg_n_complete, synced_at
        ) VALUES (
            :batch_id, :etf_code, :strategy_id, :filter_id, :filter_label,
            :capital_ntd, :entry_lag_days, :hold_trading_days,
            :n_complete_days, :n_multi_leg_days, :n_legs, :n_legs_with_conviction,
            :total_pnl_ntd, :total_alpha_ntd, :avg_day_return_pct,
            :win_rate_gross_pct, :win_rate_vs_bench_pct, :win_rate_alpha_pct,
            :recycled_n_cycles, :recycled_total_alpha_ntd, :recycled_total_pnl_ntd,
            :mean_excess_pct, :p_value_ttest, :p_value_wilcoxon,
            :leg_win_rate_gross_pct, :leg_n_complete, :synced_at
        )
        """,
        [{**r, "batch_id": batch_id, "synced_at": synced_at} for r in rows],
    )
    conn.commit()


def persist_copytrade_gap_filter_compare(
    conn: sqlite3.Connection,
    batch_id: str,
    rows: list[dict],
) -> None:
    synced_at = utc_now_iso()
    conn.execute(
        "DELETE FROM copytrade_gap_filter_compare WHERE batch_id = ?",
        (batch_id,),
    )
    if not rows:
        conn.commit()
        return
    conn.executemany(
        """
        INSERT INTO copytrade_gap_filter_compare (
            batch_id, etf_code, strategy_id, filter_id, filter_label,
            capital_ntd, entry_lag_days, hold_trading_days,
            n_complete_days, n_multi_leg_days, n_legs, n_legs_with_gap,
            total_pnl_ntd, total_alpha_ntd, avg_day_return_pct,
            win_rate_gross_pct, win_rate_vs_bench_pct, win_rate_alpha_pct,
            recycled_n_cycles, recycled_total_alpha_ntd, recycled_total_pnl_ntd,
            mean_excess_pct, p_value_ttest, p_value_wilcoxon,
            leg_win_rate_gross_pct, leg_n_complete, synced_at
        ) VALUES (
            :batch_id, :etf_code, :strategy_id, :filter_id, :filter_label,
            :capital_ntd, :entry_lag_days, :hold_trading_days,
            :n_complete_days, :n_multi_leg_days, :n_legs, :n_legs_with_gap,
            :total_pnl_ntd, :total_alpha_ntd, :avg_day_return_pct,
            :win_rate_gross_pct, :win_rate_vs_bench_pct, :win_rate_alpha_pct,
            :recycled_n_cycles, :recycled_total_alpha_ntd, :recycled_total_pnl_ntd,
            :mean_excess_pct, :p_value_ttest, :p_value_wilcoxon,
            :leg_win_rate_gross_pct, :leg_n_complete, :synced_at
        )
        """,
        [{**r, "batch_id": batch_id, "synced_at": synced_at} for r in rows],
    )
    conn.commit()


def persist_copytrade_leg_chip_snapshots(
    conn: sqlite3.Connection,
    rows: list[dict],
) -> int:
    if not rows:
        return 0
    synced_at = utc_now_iso()
    conn.executemany(
        """
        INSERT INTO copytrade_leg_chip_snapshots (
            etf_code, signal_date, stock_id,
            foreign_net_5d, foreign_net_5d_million,
            margin_balance, margin_growth_5d_pct,
            foreign_net_5d_positive, margin_cool, chip_confirm_pass,
            status, synced_at
        ) VALUES (
            :etf_code, :signal_date, :stock_id,
            :foreign_net_5d, :foreign_net_5d_million,
            :margin_balance, :margin_growth_5d_pct,
            :foreign_net_5d_positive, :margin_cool, :chip_confirm_pass,
            :status, :synced_at
        )
        ON CONFLICT(etf_code, signal_date, stock_id) DO UPDATE SET
            foreign_net_5d = excluded.foreign_net_5d,
            foreign_net_5d_million = excluded.foreign_net_5d_million,
            margin_balance = excluded.margin_balance,
            margin_growth_5d_pct = excluded.margin_growth_5d_pct,
            foreign_net_5d_positive = excluded.foreign_net_5d_positive,
            margin_cool = excluded.margin_cool,
            chip_confirm_pass = excluded.chip_confirm_pass,
            status = excluded.status,
            synced_at = excluded.synced_at
        """,
        [{**r, "synced_at": synced_at} for r in rows],
    )
    conn.commit()
    return len(rows)


def persist_copytrade_leg_confluence_snapshots(
    conn: sqlite3.Connection,
    rows: list[dict],
) -> int:
    if not rows:
        return 0
    synced_at = utc_now_iso()
    conn.executemany(
        """
        INSERT INTO copytrade_leg_confluence_snapshots (
            etf_code, signal_date, stock_id, action, share_delta,
            vcp_pass, chunge_l4_pass, p6_pass, triple_pass,
            vcp_score, chunge_layers, p6_source, status, synced_at
        ) VALUES (
            :etf_code, :signal_date, :stock_id, :action, :share_delta,
            :vcp_pass, :chunge_l4_pass, :p6_pass, :triple_pass,
            :vcp_score, :chunge_layers, :p6_source, :status, :synced_at
        )
        ON CONFLICT(etf_code, signal_date, stock_id) DO UPDATE SET
            action = excluded.action,
            share_delta = excluded.share_delta,
            vcp_pass = excluded.vcp_pass,
            chunge_l4_pass = excluded.chunge_l4_pass,
            p6_pass = excluded.p6_pass,
            triple_pass = excluded.triple_pass,
            vcp_score = excluded.vcp_score,
            chunge_layers = excluded.chunge_layers,
            p6_source = excluded.p6_source,
            status = excluded.status,
            synced_at = excluded.synced_at
        """,
        [{**r, "synced_at": synced_at} for r in rows],
    )
    conn.commit()
    return len(rows)


def persist_copytrade_leg_conviction_snapshots(
    conn: sqlite3.Connection,
    rows: list[dict],
) -> int:
    if not rows:
        return 0
    synced_at = utc_now_iso()
    conn.executemany(
        """
        INSERT INTO copytrade_leg_conviction_snapshots (
            etf_code, signal_date, stock_id, action,
            share_delta, weight_delta, metric_used, metric_value,
            prior_pool, prior_n, p70_threshold, conviction_pass, top_pct,
            status, synced_at
        ) VALUES (
            :etf_code, :signal_date, :stock_id, :action,
            :share_delta, :weight_delta, :metric_used, :metric_value,
            :prior_pool, :prior_n, :p70_threshold, :conviction_pass, :top_pct,
            :status, :synced_at
        )
        ON CONFLICT(etf_code, signal_date, stock_id) DO UPDATE SET
            action = excluded.action,
            share_delta = excluded.share_delta,
            weight_delta = excluded.weight_delta,
            metric_used = excluded.metric_used,
            metric_value = excluded.metric_value,
            prior_pool = excluded.prior_pool,
            prior_n = excluded.prior_n,
            p70_threshold = excluded.p70_threshold,
            conviction_pass = excluded.conviction_pass,
            top_pct = excluded.top_pct,
            status = excluded.status,
            synced_at = excluded.synced_at
        """,
        [{**r, "synced_at": synced_at} for r in rows],
    )
    conn.commit()
    return len(rows)


def persist_copytrade_leg_limit_entry(
    conn: sqlite3.Connection,
    rows: list[dict],
) -> int:
    if not rows:
        return 0
    synced_at = utc_now_iso()
    conn.executemany(
        """
        INSERT INTO copytrade_leg_limit_entry (
            etf_code, signal_date, stock_id, discount_pct,
            entry_date, open_px, low_px, limit_px,
            filled, fill_px, status, synced_at
        ) VALUES (
            :etf_code, :signal_date, :stock_id, :discount_pct,
            :entry_date, :open_px, :low_px, :limit_px,
            :filled, :fill_px, :status, :synced_at
        )
        ON CONFLICT(etf_code, signal_date, stock_id, discount_pct) DO UPDATE SET
            entry_date = excluded.entry_date,
            open_px = excluded.open_px,
            low_px = excluded.low_px,
            limit_px = excluded.limit_px,
            filled = excluded.filled,
            fill_px = excluded.fill_px,
            status = excluded.status,
            synced_at = excluded.synced_at
        """,
        [{**r, "synced_at": synced_at} for r in rows],
    )
    conn.commit()
    return len(rows)


def persist_copytrade_leg_opening_confirm(
    conn: sqlite3.Connection,
    rows: list[dict],
) -> int:
    if not rows:
        return 0
    synced_at = utc_now_iso()
    conn.executemany(
        """
        INSERT INTO copytrade_leg_opening_confirm (
            etf_code, signal_date, stock_id, entry_date,
            prev_close, vol_0905_0915, vol_0905_0915_avg5, vol_ratio_vs_avg5,
            px_0915, confirm_entry_px, price_ge_prev_close,
            vol_confirm_pass, opening_confirm_pass, status, synced_at
        ) VALUES (
            :etf_code, :signal_date, :stock_id, :entry_date,
            :prev_close, :vol_0905_0915, :vol_0905_0915_avg5, :vol_ratio_vs_avg5,
            :px_0915, :confirm_entry_px, :price_ge_prev_close,
            :vol_confirm_pass, :opening_confirm_pass, :status, :synced_at
        )
        ON CONFLICT(etf_code, signal_date, stock_id) DO UPDATE SET
            entry_date = excluded.entry_date,
            prev_close = excluded.prev_close,
            vol_0905_0915 = excluded.vol_0905_0915,
            vol_0905_0915_avg5 = excluded.vol_0905_0915_avg5,
            vol_ratio_vs_avg5 = excluded.vol_ratio_vs_avg5,
            px_0915 = excluded.px_0915,
            confirm_entry_px = excluded.confirm_entry_px,
            price_ge_prev_close = excluded.price_ge_prev_close,
            vol_confirm_pass = excluded.vol_confirm_pass,
            opening_confirm_pass = excluded.opening_confirm_pass,
            status = excluded.status,
            synced_at = excluded.synced_at
        """,
        [{**r, "synced_at": synced_at} for r in rows],
    )
    conn.commit()
    return len(rows)


def persist_copytrade_limit_entry_compare(
    conn: sqlite3.Connection,
    batch_id: str,
    rows: list[dict],
) -> None:
    synced_at = utc_now_iso()
    conn.execute(
        "DELETE FROM copytrade_limit_entry_compare WHERE batch_id = ?",
        (batch_id,),
    )
    if not rows:
        conn.commit()
        return
    conn.executemany(
        """
        INSERT INTO copytrade_limit_entry_compare (
            batch_id, etf_code, strategy_id, filter_id, filter_label,
            discount_pct, capital_ntd, entry_lag_days, hold_trading_days,
            n_complete_days, n_multi_leg_days, n_legs, n_legs_filled, fill_rate_pct,
            total_pnl_ntd, total_alpha_ntd, avg_day_return_pct,
            win_rate_gross_pct, win_rate_vs_bench_pct, win_rate_alpha_pct,
            recycled_n_cycles, recycled_total_alpha_ntd, recycled_total_pnl_ntd,
            mean_excess_pct, p_value_ttest, p_value_wilcoxon,
            leg_win_rate_gross_pct, leg_n_complete, synced_at
        ) VALUES (
            :batch_id, :etf_code, :strategy_id, :filter_id, :filter_label,
            :discount_pct, :capital_ntd, :entry_lag_days, :hold_trading_days,
            :n_complete_days, :n_multi_leg_days, :n_legs, :n_legs_filled, :fill_rate_pct,
            :total_pnl_ntd, :total_alpha_ntd, :avg_day_return_pct,
            :win_rate_gross_pct, :win_rate_vs_bench_pct, :win_rate_alpha_pct,
            :recycled_n_cycles, :recycled_total_alpha_ntd, :recycled_total_pnl_ntd,
            :mean_excess_pct, :p_value_ttest, :p_value_wilcoxon,
            :leg_win_rate_gross_pct, :leg_n_complete, :synced_at
        )
        """,
        [{**r, "batch_id": batch_id, "synced_at": synced_at} for r in rows],
    )
    conn.commit()


def persist_copytrade_macro_filter_compare(
    conn: sqlite3.Connection,
    batch_id: str,
    rows: list[dict],
) -> None:
    synced_at = utc_now_iso()
    conn.execute(
        "DELETE FROM copytrade_macro_filter_compare WHERE batch_id = ?",
        (batch_id,),
    )
    if not rows:
        conn.commit()
        return
    conn.executemany(
        """
        INSERT INTO copytrade_macro_filter_compare (
            batch_id, etf_code, strategy_id, filter_id, filter_label,
            capital_ntd, entry_lag_days, hold_trading_days,
            n_complete_days, n_skipped_risk_days, n_risk_days_in_baseline,
            total_pnl_ntd, total_alpha_ntd, avg_day_return_pct,
            win_rate_gross_pct, win_rate_vs_bench_pct, win_rate_alpha_pct,
            recycled_n_cycles, recycled_total_alpha_ntd, recycled_total_pnl_ntd,
            mean_excess_pct, p_value_ttest, p_value_wilcoxon, synced_at
        ) VALUES (
            :batch_id, :etf_code, :strategy_id, :filter_id, :filter_label,
            :capital_ntd, :entry_lag_days, :hold_trading_days,
            :n_complete_days, :n_skipped_risk_days, :n_risk_days_in_baseline,
            :total_pnl_ntd, :total_alpha_ntd, :avg_day_return_pct,
            :win_rate_gross_pct, :win_rate_vs_bench_pct, :win_rate_alpha_pct,
            :recycled_n_cycles, :recycled_total_alpha_ntd, :recycled_total_pnl_ntd,
            :mean_excess_pct, :p_value_ttest, :p_value_wilcoxon, :synced_at
        )
        """,
        [{**r, "batch_id": batch_id, "synced_at": synced_at} for r in rows],
    )
    conn.commit()


def persist_copytrade_macro_gap_snapshots(
    conn: sqlite3.Connection,
    rows: list[dict],
) -> int:
    if not rows:
        return 0
    synced_at = utc_now_iso()
    conn.executemany(
        """
        INSERT INTO copytrade_macro_gap_snapshots (
            entry_date, tx_gap_pct, te_gap_pct, te_minus_tx_pct, tx_gap_source,
            is_tx_risk, is_te_weak_vs_tx, is_macro_risk, synced_at
        ) VALUES (
            :entry_date, :tx_gap_pct, :te_gap_pct, :te_minus_tx_pct, :tx_gap_source,
            :is_tx_risk, :is_te_weak_vs_tx, :is_macro_risk, :synced_at
        )
        ON CONFLICT(entry_date) DO UPDATE SET
            tx_gap_pct = excluded.tx_gap_pct,
            te_gap_pct = excluded.te_gap_pct,
            te_minus_tx_pct = excluded.te_minus_tx_pct,
            tx_gap_source = excluded.tx_gap_source,
            is_tx_risk = excluded.is_tx_risk,
            is_te_weak_vs_tx = excluded.is_te_weak_vs_tx,
            is_macro_risk = excluded.is_macro_risk,
            synced_at = excluded.synced_at
        """,
        [{**r, "synced_at": synced_at} for r in rows],
    )
    conn.commit()
    return len(rows)


def persist_copytrade_opening_filter_compare(
    conn: sqlite3.Connection,
    batch_id: str,
    rows: list[dict],
) -> None:
    synced_at = utc_now_iso()
    conn.execute(
        "DELETE FROM copytrade_opening_filter_compare WHERE batch_id = ?",
        (batch_id,),
    )
    if not rows:
        conn.commit()
        return
    conn.executemany(
        """
        INSERT INTO copytrade_opening_filter_compare (
            batch_id, etf_code, strategy_id, filter_id, filter_label,
            capital_ntd, entry_lag_days, hold_trading_days,
            n_complete_days, n_multi_leg_days, n_legs, n_legs_with_opening,
            total_pnl_ntd, total_alpha_ntd, avg_day_return_pct,
            win_rate_gross_pct, win_rate_vs_bench_pct, win_rate_alpha_pct,
            recycled_n_cycles, recycled_total_alpha_ntd, recycled_total_pnl_ntd,
            mean_excess_pct, p_value_ttest, p_value_wilcoxon,
            leg_win_rate_gross_pct, leg_n_complete, synced_at
        ) VALUES (
            :batch_id, :etf_code, :strategy_id, :filter_id, :filter_label,
            :capital_ntd, :entry_lag_days, :hold_trading_days,
            :n_complete_days, :n_multi_leg_days, :n_legs, :n_legs_with_opening,
            :total_pnl_ntd, :total_alpha_ntd, :avg_day_return_pct,
            :win_rate_gross_pct, :win_rate_vs_bench_pct, :win_rate_alpha_pct,
            :recycled_n_cycles, :recycled_total_alpha_ntd, :recycled_total_pnl_ntd,
            :mean_excess_pct, :p_value_ttest, :p_value_wilcoxon,
            :leg_win_rate_gross_pct, :leg_n_complete, :synced_at
        )
        """,
        [{**r, "batch_id": batch_id, "synced_at": synced_at} for r in rows],
    )
    conn.commit()


def persist_copytrade_overnight_gaps(
    conn: sqlite3.Connection,
    rows: list[dict],
) -> int:
    if not rows:
        return 0
    synced_at = utc_now_iso()
    conn.executemany(
        """
        INSERT INTO copytrade_leg_overnight_gaps (
            etf_code, signal_date, stock_id, entry_lag_days,
            entry_date, signal_close, entry_open, overnight_gap_pct,
            status, synced_at
        ) VALUES (
            :etf_code, :signal_date, :stock_id, :entry_lag_days,
            :entry_date, :signal_close, :entry_open, :overnight_gap_pct,
            :status, :synced_at
        )
        ON CONFLICT(etf_code, signal_date, stock_id, entry_lag_days) DO UPDATE SET
            entry_date = excluded.entry_date,
            signal_close = excluded.signal_close,
            entry_open = excluded.entry_open,
            overnight_gap_pct = excluded.overnight_gap_pct,
            status = excluded.status,
            synced_at = excluded.synced_at
        """,
        [{**r, "synced_at": synced_at} for r in rows],
    )
    conn.commit()
    return len(rows)


def persist_copytrade_recheck_compare(
    conn: sqlite3.Connection,
    batch_id: str,
    rows: list[dict],
) -> None:
    synced_at = utc_now_iso()
    conn.execute(
        "DELETE FROM copytrade_recheck_compare WHERE batch_id = ?",
        (batch_id,),
    )
    if not rows:
        conn.commit()
        return
    conn.executemany(
        """
        INSERT INTO copytrade_recheck_compare (
            batch_id, etf_code, strategy_id, recheck_id, variant_id, variant_label,
            capital_ntd, entry_lag_days, hold_trading_days,
            n_complete_days, n_multi_leg_days, n_legs,
            total_pnl_ntd, total_alpha_ntd, avg_day_return_pct,
            win_rate_gross_pct, win_rate_vs_bench_pct, win_rate_alpha_pct,
            recycled_n_cycles, recycled_total_alpha_ntd, recycled_total_pnl_ntd,
            mean_excess_pct, p_value_ttest, p_value_wilcoxon,
            leg_win_rate_gross_pct, leg_n_complete, synced_at
        ) VALUES (
            :batch_id, :etf_code, :strategy_id, :recheck_id, :variant_id, :variant_label,
            :capital_ntd, :entry_lag_days, :hold_trading_days,
            :n_complete_days, :n_multi_leg_days, :n_legs,
            :total_pnl_ntd, :total_alpha_ntd, :avg_day_return_pct,
            :win_rate_gross_pct, :win_rate_vs_bench_pct, :win_rate_alpha_pct,
            :recycled_n_cycles, :recycled_total_alpha_ntd, :recycled_total_pnl_ntd,
            :mean_excess_pct, :p_value_ttest, :p_value_wilcoxon,
            :leg_win_rate_gross_pct, :leg_n_complete, :synced_at
        )
        """,
        [{**r, "batch_id": batch_id, "synced_at": synced_at} for r in rows],
    )
    conn.commit()

