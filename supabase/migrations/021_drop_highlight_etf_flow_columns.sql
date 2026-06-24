-- Drop unused ETF flow metrics and highlight score delta from stock_daily_highlight.
-- Facts-layer flow detail remains in etf_daily snapshot (daily_briefs).

alter table stock_research.stock_daily_highlight
  drop column if exists etf_add_count,
  drop column if exists etf_reduce_count,
  drop column if exists etf_flow_ntd,
  drop column if exists share_delta_total,
  drop column if exists growth_pct,
  drop column if exists delta_score_change;
