# TradingView · Gary Antonacci Dual Momentum (§2.3)

Open-source Pine references saved for local audit. Source pages (download via TradingView UI → Source code):

| Script | URL | Role |
|--------|-----|------|
| Classic 12M Absolute Momentum | [rS6fZkn7](https://www.tradingview.com/script/rS6fZkn7-Classic-Dual-Momentum-12-Month-Absolute-Momentum-Antonacci/) | Absolute momentum filter |
| Dual Momentum Strategy | [wFRnnlQr](https://www.tradingview.com/script/wFRnnlQr-Dual-Momentum-Strategy/) | Full relative + absolute GEM |
| 12M Return Strategy | [7IWRmmC9](https://www.tradingview.com/script/7IWRmmC9-12M-Return-Strategy/) | Absolute-only variant |

Local `.pine` files recreate the published logic for diff/review. TW backtest lives in `src/dual_momentum_antonacci.py`.

**Project stance (§2.3 scorecard):** use **absolute momentum circuit breaker** (0050 12M < 0 → system-wide de-risk), not as primary GPS.

## Broad-momentum saved strategies (§2.3 broad_momentum)

| Registry ID | TV source | Config |
|-------------|-----------|--------|
| `minervini-sepa-basket` | Minervini SEPA System (mdelia100) | `config/broad_momentum_tv.yaml` |

**Regime only (not Strategy overlay):** [Market Breadth Toolkit (LuxAlgo)](https://www.tradingview.com/script/MDtwgiDy-Market-Breadth-Toolkit-LuxAlgo/) · `config/regime.yaml` `breadth_impulse` · validation `scripts/run_breadth_impulse_validation.py`

Backtest engine: `src/research/backtest/broad_momentum_tv_backtest.py` · Artifacts: `reports/{strategy_id}/backtest_summary.json`
