"""SQLite DDL and schema migrations."""
from __future__ import annotations

import sqlite3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_bars (
    code TEXT NOT NULL,
    date TEXT NOT NULL,
    open REAL,
    high REAL,
    low REAL,
    close REAL NOT NULL,
    adj_close REAL,
    volume INTEGER,
    spread REAL,
    source TEXT NOT NULL DEFAULT 'finmind',
    synced_at TEXT NOT NULL,
    PRIMARY KEY (code, date, source)
);

CREATE TABLE IF NOT EXISTS etf_holdings_meta (
    etf_code TEXT NOT NULL,
    snapshot_date TEXT NOT NULL,
    nav REAL,
    holding_count INTEGER NOT NULL,
    source TEXT NOT NULL DEFAULT 'ezmoney',
    source_edit_at TEXT,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (etf_code, snapshot_date)
);

CREATE TABLE IF NOT EXISTS etf_holdings (
    etf_code TEXT NOT NULL,
    snapshot_date TEXT NOT NULL,
    stock_id TEXT NOT NULL,
    stock_name TEXT,
    shares REAL NOT NULL,
    weight_pct REAL,
    amount REAL,
    source TEXT NOT NULL DEFAULT 'ezmoney',
    source_edit_at TEXT,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (etf_code, snapshot_date, stock_id)
);

CREATE INDEX IF NOT EXISTS idx_etf_holdings_date
    ON etf_holdings (etf_code, snapshot_date);

CREATE TABLE IF NOT EXISTS benchmark_constituents_meta (
    benchmark_code TEXT NOT NULL,
    snapshot_date TEXT NOT NULL,
    holding_count INTEGER NOT NULL,
    source TEXT NOT NULL DEFAULT 'yuanta_html',
    synced_at TEXT NOT NULL,
    PRIMARY KEY (benchmark_code, snapshot_date)
);

CREATE TABLE IF NOT EXISTS benchmark_constituents (
    benchmark_code TEXT NOT NULL,
    snapshot_date TEXT NOT NULL,
    stock_id TEXT NOT NULL,
    stock_name TEXT,
    weight_pct REAL,
    source TEXT NOT NULL DEFAULT 'yuanta_html',
    synced_at TEXT NOT NULL,
    PRIMARY KEY (benchmark_code, snapshot_date, stock_id)
);

CREATE INDEX IF NOT EXISTS idx_benchmark_constituents_date
    ON benchmark_constituents (benchmark_code, snapshot_date);

CREATE TABLE IF NOT EXISTS etf_daily_signal_snapshot (
    code TEXT NOT NULL,
    snapshot_date TEXT NOT NULL,
    close_price REAL,
    foreign_net REAL,
    investment_trust_net REAL,
    dealer_self_net REAL,
    three_institution_net REAL,
    source TEXT NOT NULL,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (code, snapshot_date, source)
);

CREATE TABLE IF NOT EXISTS stock_beta (
    stock_id TEXT NOT NULL,
    name TEXT,
    market TEXT NOT NULL,
    beta REAL,
    beta_window TEXT NOT NULL,
    benchmark TEXT,
    source TEXT NOT NULL,
    as_of_date TEXT NOT NULL,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (stock_id, source, beta_window)
);

CREATE INDEX IF NOT EXISTS idx_stock_beta_market
    ON stock_beta (market, stock_id);

CREATE TABLE IF NOT EXISTS tech_risk_daily_snapshot (
    session_date TEXT NOT NULL,
    us_trade_date TEXT,
    tsm_close REAL,
    tsm_daily_return_pct REAL,
    tsm_ma5 REAL,
    tsm_ma10 REAL,
    tsm_vs_ma5_pct REAL,
    tsm_vs_ma10_pct REAL,
    tsm_above_ma5 INTEGER,
    tsm_above_ma10 INTEGER,
    sox_close REAL,
    sox_daily_return_pct REAL,
    sox_ma5 REAL,
    sox_above_ma5 INTEGER,
    smh_close REAL,
    smh_daily_return_pct REAL,
    semi_benchmark TEXT NOT NULL DEFAULT 'SOX',
    tw_spot_date TEXT,
    tw_spot_code TEXT NOT NULL DEFAULT 'IX0001',
    tw_spot_prev_close REAL,
    tx_futures_id TEXT,
    tx_contract_date TEXT,
    tx_futures_price REAL,
    tx_futures_session TEXT,
    tx_gap_pct REAL,
    te_futures_id TEXT,
    te_contract_date TEXT,
    te_futures_price REAL,
    te_futures_session TEXT,
    te_overnight_pct REAL,
    notes TEXT,
    source_us TEXT NOT NULL DEFAULT 'yahoo',
    source_tw TEXT NOT NULL DEFAULT 'finmind',
    synced_at TEXT NOT NULL,
    PRIMARY KEY (session_date)
);

CREATE TABLE IF NOT EXISTS morning_risk_snapshot (
    trade_date TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    tw_spot_date TEXT,
    tw_spot_code TEXT NOT NULL DEFAULT 'IX0001',
    tw_spot_prev_close REAL,
    tx_snapshot_id TEXT,
    tx_price REAL,
    tx_contract_date TEXT,
    tx_gap_live_pct REAL,
    te_snapshot_id TEXT,
    te_price REAL,
    te_contract_date TEXT,
    te_gap_live_pct REAL,
    te_minus_tx_pct REAL,
    source TEXT NOT NULL DEFAULT 'finmind_snapshot',
    notes TEXT,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (trade_date)
);

CREATE INDEX IF NOT EXISTS idx_morning_risk_trade_date
    ON morning_risk_snapshot (trade_date DESC);

CREATE TABLE IF NOT EXISTS intraday_1m_bars (
    symbol TEXT NOT NULL,
    ts TEXT NOT NULL,
    open_1m REAL,
    high_1m REAL,
    low_1m REAL,
    close_1m REAL,
    volume_1m REAL,
    cum_volume REAL,
    vwap_day REAL,
    day_return REAL,
    rel_volume_est REAL,
    ret_vs_index_day REAL,
    order_imbalance_1 REAL,
    position_in_day_range REAL,
    breakout_flag INTEGER,
    source TEXT NOT NULL,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (symbol, ts, source)
);

CREATE TABLE IF NOT EXISTS intraday_signals (
    symbol TEXT NOT NULL,
    ts TEXT NOT NULL,
    buy_signal INTEGER NOT NULL,
    etf_hold_count INTEGER,
    rel_volume_est REAL,
    ret_vs_index_day REAL,
    position_in_day_range REAL,
    order_imbalance_1 REAL,
    breakout_flag INTEGER,
    reason TEXT,
    source TEXT NOT NULL,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (symbol, ts, source)
);

CREATE INDEX IF NOT EXISTS idx_intraday_signals_ts
    ON intraday_signals (ts, buy_signal);

CREATE TABLE IF NOT EXISTS stock_daily_bars (
    stock_id TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    open REAL,
    high REAL,
    low REAL,
    close REAL NOT NULL,
    adj_close REAL,
    volume INTEGER,
    source TEXT NOT NULL DEFAULT 'finmind',
    synced_at TEXT NOT NULL,
    PRIMARY KEY (stock_id, trade_date, source)
);

CREATE INDEX IF NOT EXISTS idx_stock_daily_bars_date
    ON stock_daily_bars (trade_date, stock_id);

CREATE TABLE IF NOT EXISTS stock_institutional_daily (
    stock_id TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    close_price REAL,
    foreign_net REAL,
    investment_trust_net REAL,
    dealer_self_net REAL,
    three_institution_net REAL,
    source TEXT NOT NULL DEFAULT 'finmind',
    synced_at TEXT NOT NULL,
    PRIMARY KEY (stock_id, trade_date, source)
);

CREATE INDEX IF NOT EXISTS idx_stock_institutional_date
    ON stock_institutional_daily (trade_date, stock_id);

CREATE TABLE IF NOT EXISTS investment_scores (
    stock_id TEXT NOT NULL,
    as_of_date TEXT NOT NULL,
    score_version TEXT NOT NULL,
    stock_name TEXT,
    smart_money REAL NOT NULL,
    catalyst REAL NOT NULL,
    expectation REAL NOT NULL,
    fundamental REAL NOT NULL,
    risk REAL NOT NULL,
    investment_score REAL NOT NULL,
    watchlist TEXT NOT NULL,
    pool_reason TEXT,
    money_rank INTEGER,
    event_rank INTEGER,
    position_intent TEXT,
    tech_risk_flag TEXT,
    metadata_json TEXT,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (stock_id, as_of_date, score_version)
);

CREATE INDEX IF NOT EXISTS idx_investment_scores_date
    ON investment_scores (as_of_date, watchlist, investment_score DESC);

CREATE TABLE IF NOT EXISTS pm_watchlist (
    stock_id TEXT NOT NULL,
    as_of_date TEXT NOT NULL,
    score_version TEXT NOT NULL,
    stock_name TEXT,
    investment_score REAL NOT NULL,
    watchlist TEXT NOT NULL,
    entry_signal TEXT NOT NULL,
    entry_tags_json TEXT,
    chip_tag TEXT,
    pm_bucket TEXT NOT NULL,
    flow_score REAL,
    chip_score REAL,
    tech_score REAL,
    catalyst_score REAL,
    fundamental_score REAL,
    note TEXT,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (stock_id, as_of_date, score_version)
);

CREATE INDEX IF NOT EXISTS idx_pm_watchlist_date
    ON pm_watchlist (as_of_date, pm_bucket, investment_score DESC);

CREATE TABLE IF NOT EXISTS stock_fundamental (
    stock_id TEXT NOT NULL,
    as_of_date TEXT NOT NULL,
    pe REAL,
    pb REAL,
    roe_ttm REAL,
    eps_ttm REAL,
    eps_latest_q REAL,
    roe_latest_q REAL,
    dividend_yield REAL,
    revenue_yoy_pct REAL,
    revenue_mom_accel_pp REAL,
    source TEXT NOT NULL DEFAULT 'finmind',
    synced_at TEXT NOT NULL,
    PRIMARY KEY (stock_id, as_of_date, source)
);

CREATE TABLE IF NOT EXISTS stock_consensus (
    stock_id TEXT NOT NULL,
    as_of_date TEXT NOT NULL,
    metric TEXT NOT NULL,
    consensus_value REAL NOT NULL,
    source TEXT NOT NULL DEFAULT 'finmind',
    synced_at TEXT NOT NULL,
    PRIMARY KEY (stock_id, as_of_date, metric, source)
);

CREATE TABLE IF NOT EXISTS stock_financial_history (
    stock_id TEXT NOT NULL,
    period_date TEXT NOT NULL,
    period_type TEXT NOT NULL,
    metric TEXT NOT NULL,
    value REAL NOT NULL,
    source TEXT NOT NULL DEFAULT 'finmind',
    synced_at TEXT NOT NULL,
    PRIMARY KEY (stock_id, period_date, period_type, metric, source)
);

CREATE INDEX IF NOT EXISTS idx_stock_financial_history_stock
    ON stock_financial_history (stock_id, metric, period_date DESC);

CREATE TABLE IF NOT EXISTS catalyst_events (
    event_id TEXT NOT NULL,
    stock_id TEXT NOT NULL,
    event_date TEXT NOT NULL,
    catalyst_type TEXT NOT NULL,
    headline TEXT NOT NULL,
    polarity TEXT NOT NULL DEFAULT 'NEUTRAL',
    explains_etf_add TEXT NOT NULL DEFAULT 'NONE',
    confidence INTEGER NOT NULL DEFAULT 50,
    sources_json TEXT,
    source TEXT NOT NULL DEFAULT 'manual',
    ingested_at TEXT NOT NULL,
    PRIMARY KEY (event_id)
);

CREATE INDEX IF NOT EXISTS idx_catalyst_events_stock_date
    ON catalyst_events (stock_id, event_date DESC);

CREATE TABLE IF NOT EXISTS research_memos (
    memo_date TEXT NOT NULL,
    stock_id TEXT NOT NULL,
    rank INTEGER NOT NULL,
    watchlist TEXT NOT NULL,
    investment_score REAL,
    body_md TEXT NOT NULL,
    context_json TEXT,
    llm_used INTEGER NOT NULL DEFAULT 0,
    audit_passed INTEGER NOT NULL DEFAULT 1,
    audit_notes TEXT,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (memo_date, stock_id)
);

CREATE TABLE IF NOT EXISTS flow_events (
    event_date TEXT NOT NULL,
    prev_date TEXT NOT NULL,
    stock_id TEXT NOT NULL,
    stock_name TEXT,
    net_side TEXT NOT NULL,
    consensus TEXT NOT NULL,
    intent TEXT NOT NULL,
    conviction REAL NOT NULL,
    implied_flow_ntd REAL,
    etf_count INTEGER NOT NULL,
    source_etfs TEXT NOT NULL,
    flow_version TEXT NOT NULL,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (event_date, stock_id, flow_version)
);

CREATE INDEX IF NOT EXISTS idx_flow_events_date
    ON flow_events (event_date DESC, net_side);

CREATE TABLE IF NOT EXISTS signal_review_runs (
    run_id TEXT PRIMARY KEY,
    review_date TEXT NOT NULL,
    window_start TEXT,
    window_end TEXT,
    score_version TEXT NOT NULL,
    capital_ntd REAL NOT NULL,
    lookback_trading_days INTEGER NOT NULL,
    lookback_event_days INTEGER NOT NULL,
    benchmark_code TEXT NOT NULL DEFAULT 'IX0001',
    review_version TEXT NOT NULL DEFAULT 'signal-review-v1',
    skipped_outcomes INTEGER NOT NULL DEFAULT 0,
    beta_as_of TEXT,
    message TEXT,
    signal_dates_json TEXT NOT NULL DEFAULT '[]',
    ic_by_date_json TEXT NOT NULL DEFAULT '{}',
    synced_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_signal_review_runs_date
    ON signal_review_runs (review_date DESC, synced_at DESC);

CREATE TABLE IF NOT EXISTS signal_outcomes (
    run_id TEXT NOT NULL,
    as_of_date TEXT NOT NULL,
    stock_id TEXT NOT NULL,
    horizon INTEGER NOT NULL DEFAULT 1,
    stock_name TEXT,
    outcome_date TEXT,
    pm_bucket TEXT,
    entry_signal TEXT,
    chip_tag TEXT,
    investment_score REAL,
    ret_pct REAL,
    bench_ret_pct REAL,
    alpha_pct REAL,
    capm_alpha_pct REAL,
    beta REAL,
    status TEXT NOT NULL DEFAULT 'complete',
    synced_at TEXT NOT NULL,
    PRIMARY KEY (run_id, as_of_date, stock_id, horizon)
);

CREATE INDEX IF NOT EXISTS idx_signal_outcomes_run
    ON signal_outcomes (run_id, as_of_date);

CREATE TABLE IF NOT EXISTS signal_paper_days (
    run_id TEXT NOT NULL,
    signal_day TEXT NOT NULL,
    outcome_day TEXT,
    deployed_ntd REAL NOT NULL DEFAULT 0,
    pnl_ntd REAL,
    day_return_pct REAL,
    bench_return_pct REAL,
    alpha_ntd REAL,
    capm_alpha_ntd REAL,
    portfolio_beta REAL,
    status TEXT NOT NULL DEFAULT 'complete',
    synced_at TEXT NOT NULL,
    PRIMARY KEY (run_id, signal_day)
);

CREATE TABLE IF NOT EXISTS signal_paper_horizons (
    run_id TEXT NOT NULL,
    signal_day TEXT NOT NULL,
    horizon INTEGER NOT NULL,
    deployed_ntd REAL NOT NULL DEFAULT 0,
    outcome_day TEXT,
    pnl_ntd REAL,
    return_pct REAL,
    bench_return_pct REAL,
    alpha_ntd REAL,
    capm_alpha_ntd REAL,
    portfolio_beta REAL,
    status TEXT NOT NULL DEFAULT 'complete',
    synced_at TEXT NOT NULL,
    PRIMARY KEY (run_id, signal_day, horizon)
);

CREATE TABLE IF NOT EXISTS vcp_screen_scores_v2 (
    stock_id TEXT NOT NULL,
    as_of_date TEXT NOT NULL,
    model_id TEXT NOT NULL,
    stock_name TEXT,
    composite_score REAL NOT NULL,
    rating TEXT NOT NULL,
    execution_state TEXT NOT NULL,
    entry_ready INTEGER NOT NULL DEFAULT 0,
    pattern_type TEXT,
    pivot_price REAL,
    distance_from_pivot_pct REAL,
    stop_loss REAL,
    risk_pct REAL,
    valid_vcp INTEGER,
    metadata_json TEXT,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (stock_id, as_of_date, model_id)
);
CREATE INDEX IF NOT EXISTS idx_vcp_screen_v2_date
    ON vcp_screen_scores_v2 (as_of_date, composite_score DESC);

CREATE TABLE IF NOT EXISTS rrg_universe_scores (
    session_date TEXT NOT NULL,
    screen_kind TEXT NOT NULL,
    data_baseline_date TEXT NOT NULL,
    stock_id TEXT NOT NULL,
    stock_name TEXT,
    rs_ratio REAL,
    rs_momentum REAL,
    quadrant TEXT,
    quadrants_json TEXT,
    trend TEXT,
    disp REAL,
    seg_last REAL,
    segs_json TEXT,
    tier2 INTEGER NOT NULL DEFAULT 0,
    mono_tier2 INTEGER NOT NULL DEFAULT 0,
    mono_fresh INTEGER NOT NULL DEFAULT 0,
    daily_pct REAL,
    tick_ok INTEGER,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (session_date, screen_kind, stock_id)
);
CREATE INDEX IF NOT EXISTS idx_rrg_universe_session
    ON rrg_universe_scores (session_date, screen_kind);
CREATE INDEX IF NOT EXISTS idx_rrg_universe_stock
    ON rrg_universe_scores (stock_id, session_date DESC);

CREATE TABLE IF NOT EXISTS stock_opening_session_stats (
    stock_id TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    vol_0905_0915 REAL,
    px_0915 REAL,
    px_0905 REAL,
    n_ticks_window INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'finmind_tick',
    synced_at TEXT NOT NULL,
    PRIMARY KEY (stock_id, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_stock_opening_session_date
    ON stock_opening_session_stats (trade_date, stock_id);

CREATE TABLE IF NOT EXISTS etf_behavior_predictions (
    etf_code TEXT NOT NULL,
    as_of_date TEXT NOT NULL,
    stock_id TEXT NOT NULL,
    model_id TEXT NOT NULL,
    model_version TEXT NOT NULL,
    score REAL,
    rank_n INTEGER,
    universe_n INTEGER,
    features_json TEXT,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (etf_code, as_of_date, stock_id, model_id)
);
CREATE INDEX IF NOT EXISTS idx_etf_behavior_pred_date
    ON etf_behavior_predictions (etf_code, as_of_date DESC);

CREATE TABLE IF NOT EXISTS etf_behavior_validation (
    etf_code TEXT NOT NULL,
    score_date TEXT NOT NULL,
    outcome_date TEXT NOT NULL,
    model_id TEXT NOT NULL,
    model_version TEXT NOT NULL,
    add_cohort TEXT NOT NULL DEFAULT 'all',
    eval_mode TEXT NOT NULL DEFAULT 'top_k',
    k INTEGER NOT NULL,
    n_universe INTEGER NOT NULL,
    n_actual_adds INTEGER NOT NULL,
    precision_at_k REAL,
    recall_at_k REAL,
    mean_rank_pct REAL,
    median_rank_pct REAL,
    ndcg_at_k REAL,
    random_precision REAL,
    lift_vs_random REAL,
    top_k_json TEXT,
    hit_json TEXT,
    missed_json TEXT,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (etf_code, score_date, outcome_date, model_id, add_cohort, eval_mode)
);
CREATE INDEX IF NOT EXISTS idx_etf_behavior_val_outcome
    ON etf_behavior_validation (outcome_date DESC, etf_code);

CREATE TABLE IF NOT EXISTS etf_holdings_fetch_log (
    fetch_id INTEGER PRIMARY KEY AUTOINCREMENT,
    etf_code TEXT NOT NULL,
    snapshot_date TEXT NOT NULL,
    source TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    source_edit_at TEXT,
    holding_count INTEGER NOT NULL DEFAULT 0,
    nav REAL,
    content_hash TEXT NOT NULL,
    raw_path TEXT NOT NULL,
    sync_status TEXT NOT NULL,
    prev_fetch_id INTEGER,
    diff_summary TEXT,
    rows_added INTEGER NOT NULL DEFAULT 0,
    rows_removed INTEGER NOT NULL DEFAULT 0,
    rows_changed INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_etf_holdings_fetch
    ON etf_holdings_fetch_log (etf_code, snapshot_date, fetch_id DESC);

CREATE TABLE IF NOT EXISTS stock_margin_daily (
    stock_id TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    margin_balance REAL,
    margin_change REAL,
    short_balance REAL,
    short_change REAL,
    source TEXT NOT NULL,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (stock_id, trade_date, source)
);

CREATE TABLE IF NOT EXISTS stock_lending_daily (
    stock_id TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    lending_balance REAL,
    lending_change REAL,
    fee_rate REAL,
    source TEXT NOT NULL,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (stock_id, trade_date, source)
);

CREATE TABLE IF NOT EXISTS stock_daytrade_daily (
    stock_id TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    daytrade_volume REAL,
    total_volume REAL,
    daytrade_ratio_pct REAL,
    source TEXT NOT NULL,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (stock_id, trade_date, source)
);

CREATE TABLE IF NOT EXISTS stock_branch_daily (
    stock_id TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    buy_top5_net REAL,
    sell_top5_net REAL,
    smart_net REAL,
    retail_net REAL,
    branch_count INTEGER,
    source TEXT NOT NULL,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (stock_id, trade_date, source)
);

CREATE TABLE IF NOT EXISTS stock_block_trade (
    stock_id TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    block_volume REAL,
    block_amount REAL,
    block_count INTEGER,
    source TEXT NOT NULL,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (stock_id, trade_date, source)
);

CREATE TABLE IF NOT EXISTS us_daily_bars (
    ticker TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    open REAL,
    high REAL,
    low REAL,
    close REAL,
    adj_close REAL,
    volume REAL,
    source TEXT NOT NULL,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (ticker, trade_date, source)
);

CREATE TABLE IF NOT EXISTS stock_corporate_actions (
    symbol_key TEXT NOT NULL,
    ex_date TEXT NOT NULL,
    action_type TEXT NOT NULL,
    amount REAL,
    split_numerator REAL,
    split_denominator REAL,
    split_ratio TEXT,
    source TEXT NOT NULL DEFAULT 'yahoo',
    synced_at TEXT NOT NULL,
    PRIMARY KEY (symbol_key, ex_date, action_type, source)
);

CREATE INDEX IF NOT EXISTS idx_stock_corporate_actions_date
    ON stock_corporate_actions (ex_date DESC, symbol_key);

CREATE TABLE IF NOT EXISTS qlib_tw_factor_scores (
    stock_id TEXT NOT NULL,
    as_of_date TEXT NOT NULL,
    model_id TEXT NOT NULL,
    stock_name TEXT,
    composite_score REAL NOT NULL,
    rank_n INTEGER NOT NULL,
    feature_date TEXT NOT NULL,
    features_json TEXT,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (stock_id, as_of_date, model_id)
);
CREATE INDEX IF NOT EXISTS idx_qlib_tw_factor_date
    ON qlib_tw_factor_scores (as_of_date DESC, model_id, rank_n);

CREATE TABLE IF NOT EXISTS flow_event_legs (
    event_date TEXT NOT NULL,
    prev_date TEXT NOT NULL,
    stock_id TEXT NOT NULL,
    etf_id TEXT NOT NULL,
    stock_name TEXT,
    action TEXT NOT NULL,
    shares_delta REAL,
    value_delta REAL,
    weight_delta REAL,
    price_before_5d REAL,
    return_before_5d REAL,
    sector TEXT,
    theme TEXT,
    flow_tape_regime TEXT,
    flow_version TEXT NOT NULL,
    return_after_1d REAL,
    alpha_after_1d REAL,
    return_after_3d REAL,
    alpha_after_3d REAL,
    return_after_5d REAL,
    alpha_after_5d REAL,
    return_after_10d REAL,
    alpha_after_10d REAL,
    return_after_20d REAL,
    alpha_after_20d REAL,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (event_date, stock_id, etf_id, flow_version)
);
CREATE INDEX IF NOT EXISTS idx_flow_event_legs_date
    ON flow_event_legs (event_date DESC, flow_version);

CREATE TABLE IF NOT EXISTS mutual_fund_holdings_meta (
    fund_code TEXT NOT NULL,
    snapshot_date TEXT NOT NULL,
    fund_name TEXT,
    disclosure_type TEXT NOT NULL,
    fund_size_billion REAL,
    holding_count INTEGER NOT NULL,
    source TEXT NOT NULL,
    source_edit_at TEXT,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (fund_code, snapshot_date, disclosure_type)
);

CREATE TABLE IF NOT EXISTS mutual_fund_holdings (
    fund_code TEXT NOT NULL,
    snapshot_date TEXT NOT NULL,
    disclosure_type TEXT NOT NULL,
    stock_id TEXT NOT NULL,
    stock_name TEXT,
    rank_no INTEGER,
    shares REAL,
    weight_pct REAL,
    amount REAL,
    asset_type TEXT,
    source TEXT NOT NULL,
    source_edit_at TEXT,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (fund_code, snapshot_date, disclosure_type, stock_id)
);

CREATE INDEX IF NOT EXISTS idx_mutual_fund_holdings_date
    ON mutual_fund_holdings (fund_code, snapshot_date);

CREATE TABLE IF NOT EXISTS rrg_narrow_backtest_runs (
    run_id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    regime_filter TEXT NOT NULL DEFAULT 'narrow_leadership_momentum',
    year_start TEXT NOT NULL,
    year_end TEXT NOT NULL,
    factor_mode TEXT NOT NULL DEFAULT 'rolling',
    top_n INTEGER NOT NULL DEFAULT 10,
    min_vol INTEGER NOT NULL DEFAULT 3000000,
    rrg_length INTEGER NOT NULL DEFAULT 20,
    benchmark_code TEXT NOT NULL DEFAULT 'IX0001',
    entry_price_mode TEXT NOT NULL DEFAULT 'open',
    horizons_json TEXT NOT NULL DEFAULT '[10,30,45]',
    signal_dates_total INTEGER NOT NULL DEFAULT 0,
    notes TEXT,
    synced_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_rrg_narrow_backtest_runs_synced
    ON rrg_narrow_backtest_runs (synced_at DESC);

CREATE TABLE IF NOT EXISTS rrg_narrow_backtest_summary (
    run_id TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    strategy_label TEXT NOT NULL,
    hold_days INTEGER NOT NULL,
    n_periods INTEGER NOT NULL,
    n_skipped INTEGER NOT NULL DEFAULT 0,
    mean_return_pct REAL,
    mean_bench_pct REAL,
    mean_excess_pct REAL,
    total_excess_pct REAL,
    win_rate_vs_bench_pct REAL,
    win_rate_gross_pct REAL,
    window_start TEXT,
    window_end TEXT,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (run_id, strategy_id, hold_days)
);

CREATE TABLE IF NOT EXISTS rrg_narrow_backtest_periods (
    run_id TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    hold_days INTEGER NOT NULL,
    signal_date TEXT NOT NULL,
    entry_date TEXT,
    exit_date TEXT,
    n_stocks INTEGER NOT NULL DEFAULT 0,
    picks_json TEXT NOT NULL DEFAULT '[]',
    return_pct REAL,
    bench_return_pct REAL,
    excess_pct REAL,
    beat_bench INTEGER,
    gross_win INTEGER,
    status TEXT NOT NULL DEFAULT 'complete',
    skip_reason TEXT,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (run_id, strategy_id, hold_days, signal_date)
);

CREATE INDEX IF NOT EXISTS idx_rrg_narrow_backtest_periods_run
    ON rrg_narrow_backtest_periods (run_id, strategy_id, hold_days);

CREATE TABLE IF NOT EXISTS rrg_narrow_regime_calendar (
    run_id TEXT NOT NULL,
    eval_date TEXT NOT NULL,
    year TEXT NOT NULL,
    momentum_structure TEXT NOT NULL,
    dispersion_20d REAL,
    rolling_m1_20d REAL,
    top30_intra_std REAL,
    realized_vol_20d REAL,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (run_id, eval_date)
);

CREATE TABLE IF NOT EXISTS rrg_narrow_regime_year_stats (
    run_id TEXT NOT NULL,
    year TEXT NOT NULL,
    narrow_extreme_days INTEGER NOT NULL DEFAULT 0,
    narrow_moderate_days INTEGER NOT NULL DEFAULT 0,
    total_trading_days INTEGER NOT NULL DEFAULT 0,
    notes TEXT,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (run_id, year)
);

CREATE INDEX IF NOT EXISTS idx_daily_bars_date
    ON daily_bars (date DESC, code);

CREATE INDEX IF NOT EXISTS idx_etf_daily_signal_date
    ON etf_daily_signal_snapshot (code, snapshot_date DESC);

CREATE INDEX IF NOT EXISTS idx_etf_holdings_meta_latest
    ON etf_holdings_meta (etf_code, snapshot_date DESC);

CREATE INDEX IF NOT EXISTS idx_intraday_1m_symbol_ts
    ON intraday_1m_bars (symbol, ts DESC);

CREATE INDEX IF NOT EXISTS idx_stock_fundamental_date
    ON stock_fundamental (as_of_date DESC, stock_id);

CREATE INDEX IF NOT EXISTS idx_stock_consensus_date
    ON stock_consensus (as_of_date DESC, stock_id, metric);

CREATE INDEX IF NOT EXISTS idx_stock_margin_date
    ON stock_margin_daily (trade_date, stock_id);

CREATE INDEX IF NOT EXISTS idx_stock_lending_date
    ON stock_lending_daily (trade_date, stock_id);

CREATE INDEX IF NOT EXISTS idx_stock_daytrade_date
    ON stock_daytrade_daily (trade_date, stock_id);

CREATE INDEX IF NOT EXISTS idx_us_daily_bars_date
    ON us_daily_bars (trade_date DESC, ticker);

CREATE INDEX IF NOT EXISTS idx_mutual_fund_meta_date
    ON mutual_fund_holdings_meta (fund_code, snapshot_date DESC);

CREATE INDEX IF NOT EXISTS idx_research_memos_stock
    ON research_memos (stock_id, memo_date DESC);

CREATE INDEX IF NOT EXISTS idx_rrg_narrow_summary_run
    ON rrg_narrow_backtest_summary (run_id, strategy_id, hold_days);

CREATE INDEX IF NOT EXISTS idx_rrg_narrow_regime_cal_date
    ON rrg_narrow_regime_calendar (run_id, eval_date);

CREATE INDEX IF NOT EXISTS idx_rrg_narrow_year_stats_run
    ON rrg_narrow_regime_year_stats (run_id, year);

CREATE TABLE IF NOT EXISTS lens_daily_highlight (
    trade_date TEXT NOT NULL,
    stock_id TEXT NOT NULL,
    row_json TEXT NOT NULL,
    lens_score REAL NOT NULL DEFAULT 0,
    highlight_tier TEXT NOT NULL DEFAULT 'none',
    rrg_quadrant TEXT,
    rrg_mono_fresh INTEGER NOT NULL DEFAULT 0,
    rrg_tier2 INTEGER NOT NULL DEFAULT 0,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (trade_date, stock_id)
);

CREATE INDEX IF NOT EXISTS idx_lens_daily_highlight_date
    ON lens_daily_highlight (trade_date, lens_score DESC);

CREATE TABLE IF NOT EXISTS stock_kbar_1m (
    stock_id TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    minute TEXT NOT NULL,
    open REAL,
    high REAL,
    low REAL,
    close REAL NOT NULL,
    volume INTEGER,
    source TEXT NOT NULL DEFAULT 'finmind',
    synced_at TEXT NOT NULL,
    PRIMARY KEY (stock_id, trade_date, minute, source)
);

CREATE INDEX IF NOT EXISTS idx_stock_kbar_1m_date
    ON stock_kbar_1m (trade_date, stock_id);

CREATE TABLE IF NOT EXISTS lens_daily_alert (
    trade_date TEXT PRIMARY KEY,
    total_count INTEGER NOT NULL DEFAULT 0,
    fire_count INTEGER NOT NULL DEFAULT 0,
    delta_new_count INTEGER NOT NULL DEFAULT 0,
    consensus_add_count INTEGER NOT NULL DEFAULT 0,
    headline_zh TEXT NOT NULL,
    items_json TEXT NOT NULL DEFAULT '[]',
    computed_at TEXT NOT NULL
);
"""


def _drop_retired_execution_tables(conn: sqlite3.Connection) -> None:
    for table in (
        "order_intents",
        "execution_eval_runs",
        "portfolio_weights",
        "portfolio_positions",
        "portfolio_books",
    ):
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.commit()


def _migrate_schema(conn: sqlite3.Connection) -> None:
    """既有 DB 補欄位（CREATE IF NOT EXISTS 不會自動 ALTER）。"""
    migrations: list[tuple[str, str, str]] = [
        (
            "stock_fundamental",
            "eps_latest_q",
            "ALTER TABLE stock_fundamental ADD COLUMN eps_latest_q REAL",
        ),
        (
            "stock_fundamental",
            "roe_latest_q",
            "ALTER TABLE stock_fundamental ADD COLUMN roe_latest_q REAL",
        ),
        (
            "pm_watchlist",
            "entry_tags_json",
            "ALTER TABLE pm_watchlist ADD COLUMN entry_tags_json TEXT",
        ),
        (
            "lens_daily_alert",
            "total_count",
            "ALTER TABLE lens_daily_alert ADD COLUMN total_count INTEGER NOT NULL DEFAULT 0",
        ),
        (
            "lens_daily_alert",
            "consensus_add_count",
            "ALTER TABLE lens_daily_alert ADD COLUMN consensus_add_count INTEGER NOT NULL DEFAULT 0",
        ),
        ("daily_bars", "adj_close", "ALTER TABLE daily_bars ADD COLUMN adj_close REAL"),
        (
            "stock_daily_bars",
            "adj_close",
            "ALTER TABLE stock_daily_bars ADD COLUMN adj_close REAL",
        ),
        ("us_daily_bars", "adj_close", "ALTER TABLE us_daily_bars ADD COLUMN adj_close REAL"),
    ]
    for table, col, ddl in migrations:
        try:
            cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        except sqlite3.OperationalError:
            continue
        if cols and col not in cols:
            conn.execute(ddl)
    conn.commit()
    _migrate_flow_tape_regime_column(conn)
    _drop_retired_stock_daily_lens_table(conn)
    _drop_retired_execution_tables(conn)


def _drop_retired_stock_daily_lens_table(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TABLE IF EXISTS stock_daily_lens")
    conn.commit()


def _migrate_flow_tape_regime_column(conn: sqlite3.Connection) -> None:
    try:
        cols = {
            row[1] for row in conn.execute("PRAGMA table_info(flow_event_legs)").fetchall()
        }
    except sqlite3.OperationalError:
        return
    if cols and "market_regime" in cols and "flow_tape_regime" not in cols:
        conn.execute(
            "ALTER TABLE flow_event_legs RENAME COLUMN market_regime TO flow_tape_regime"
        )
        conn.commit()
