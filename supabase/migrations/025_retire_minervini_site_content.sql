-- Retire minervini-sepa-basket from public site registry (2026-06 cleanup).
-- Runtime SSOT: stock_research.site_content · strategy_performance_yearly

DELETE FROM stock_research.strategy_performance_yearly
WHERE strategy_id = 'minervini-sepa-basket';

DELETE FROM stock_research.site_content
WHERE page_id IN (
  'strategy_minervini_sepa_basket',
  'research_case_minervini_sepa'
);
