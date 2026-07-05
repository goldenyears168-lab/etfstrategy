# TradingView · Gary Antonacci Dual Momentum (§2.3)

Open-source Pine references saved for local audit. Source pages (download via TradingView UI → Source code):

| Script | URL | Role |
|--------|-----|------|
| Classic 12M Absolute Momentum | [rS6fZkn7](https://www.tradingview.com/script/rS6fZkn7-Classic-Dual-Momentum-12-Month-Absolute-Momentum-Antonacci/) | Absolute momentum filter |
| Dual Momentum Strategy | [wFRnnlQr](https://www.tradingview.com/script/wFRnnlQr-Dual-Momentum-Strategy/) | Full relative + absolute GEM |
| 12M Return Strategy | [7IWRmmC9](https://www.tradingview.com/script/7IWRmmC9-12M-Return-Strategy/) | Absolute-only variant |

Local `.pine` files recreate the published logic for diff/review.

**Project stance (§2.3 scorecard):** use **absolute momentum circuit breaker** (0050 12M < 0 → system-wide de-risk), not as primary GPS.

**Regime only (not Strategy overlay):** [Market Breadth Toolkit (LuxAlgo)](https://www.tradingview.com/script/MDtwgiDy-Market-Breadth-Toolkit-LuxAlgo/) · `config/regime.yaml` `breadth_impulse`
