# scripts/research — 回測與一次性研究

**不在** `daily_sync.sh` 收盤鏈內。需要時手動執行。

## Copytrade / 00981A

| 檔案 | 用途 |
|------|------|
| `run_00981a_copytrade_backtest.py` | L1/L1H9 跟單矩陣、filter 研究 |
| `run_00981a_holdings_rrg_audit.py` | 持股 × RRG 稽核 |
| `run_00981a_hypothesis_daily.py` | → 已併入 `run_00981a_daily_brief.py` |
| `run_behavior_oos_audit.py` | 行為 OOS |
| `run_acdd04_copytrade_backtest.py` | 主動基金跟單 |

## RRG / momentum / breadth

| 檔案 | 用途 |
|------|------|
| `run_rrg_rotation_backtest.py` | RRG rotation |
| `run_rrg_mono_breadth_backtest.py` | mono hold7 × Breadth zone |
| `run_broad_momentum_tv_backtest.py` | Minervini SEPA basket |
| `run_breadth_impulse_validation.py` | Zweig/Deemer Regime 診斷 validation |
| `run_market_breadth_report.py` | **Breadth zone** HTML → `reports/research/breadth/` |
| `render_rrg_universe_html.py` | RRG → `research/rrg/` · 00981A → `research/00981a-copytrade/` |
| `render_strategy_hub_html.py` | **策略入口** → `reports/research/strategy_hub.html` |
| `organize_research_html.py` | 將散落的 HTML 移入子目錄，並在 `research/` 根建立 **symlink** 別名 |
| `run_dual_momentum_antonacci_backtest.py` | Antonacci dual momentum |
| `run_pullback_regime_backtest.py` | Pullback regime |

## VCP（daily · 非校準）

| 檔案 | 用途 |
|------|------|
| `run_vcp_daily_brief.py` | VCP daily brief |
| `run_vcp_intraday_watch.py` | 盤中 watch |
| `run_chunge_l4_calibration.py` | 春哥 L4（archive · 一次性） |

VCP 文獻校準 / benchmark 已封存 → `scripts/research/archive/` · `src/research/archive/vcp_calibration/`

## FinPilot / S04 / tw_stocker 對照

| 檔案 | 用途 |
|------|------|
| `run_finpilot_vs_l1_compare.py` | FinPilot vs L1 |
| `run_tw_stocker_vs_l1_compare.py` | tw_stocker vs L1 |
| `run_s04_*.py` | S04 layer / mom sweep |
| `run_inst_flow_backtest.py` | 法人 flow |

## 其他

| 檔案 | 用途 |
|------|------|
| `audit_behavior_stats.py` | 行為統計稽核 |
| `backfill_historical_constituents.py` | 歷史成分股 |
| `run_sync_mutual_fund_holdings.py` | 主動基金持股 sync |
