# 每日排程速查（infra SOP）

> **非 Facts product layer** — 本文件是 launchd / 手動腳本排程；事實產物見 `reports/daily/etf-daily/`（**Facts layer** · `layer: facts`）。  
> 架構：[architecture.md](./architecture.md) · 術語：[terminology.md](./terminology.md)

## 排程

| # | 名稱 | 時間 | 入口 |
|---|------|------|------|
| VCP | Pivot Gate / Coil Close brief | 13:00 | `scripts/launchd/vcp-funnel-specs.command` |
| ②a | RRG mono 收盤前預警 | 13:00 | `scripts/launchd/rrg-mono-intraday-watch.command` |
| ② | 收盤 ETF 日報 | 16:30 | `scripts/1630收盤雷達.command` |
| ②b | RRG mono 收盤確認 | 16:40 | `scripts/launchd/rrg-mono-scan.command` |
| ③ | 週日補庫 | 週日 20:00 | `scripts/2000週日補庫.command` |

## ② 收盤閱讀順序

1. **`reports/daily/etf-daily/daily_brief.md`** — 各 ETF 持股變化（00981A 新进/加码 等）
2. **`reports/daily/regime/daily_brief.md`** — Regime 四格雷達
3. **`reports/research/breadth/*_market_breadth_ma_*.html`**（可選）— Breadth zone
4. `reports/daily/vcp_funnel_specs_daily_brief.md`（13:00 · Pivot Gate + Coil Close）
5. `reports/daily/rrg_mono_intraday_watch.md`（13:00 後，候選預警）
6. `reports/daily/rrg_mono_daily.md`（16:40 後，收盤確認）

## 手動研究

- **00981A L1H9 跟單回測**：`scripts/run_00981a_copytrade_backtest.py --strategy L1H9`
- 方法論：`docs/00981a-copytrade-research-methodology.md`

## `.env`（摘）

```bash
RUN_SCORE_ENGINE=0
RUN_VCP_FUNNEL=1
```

（`RUN_SCORE_ENGINE` 已退役；收盤主線僅 ETF 日報。）
