# Strategy layer · 策略層 catalog

SSOT：`config/strategy.yaml` · frozen specs · parallel strategies · no ensemble.

## Principles

- adopted specs only · graduated from research topics
- parallel strategies · no ensemble weighting
- frozen params · backtest JSON as evidence

## Strategies

### 00981A copytrade L1H9

- **id**: `00981a-l1h9` · **enabled**: `False` · **schedule**: manual
- 00981A 新进/加码 跟單 · T+1 開 · 9 槽 · hold9 · 手動 screen / 回測。
- backtest: `reports/research/00981a-copytrade/l1h9_slot_backtest_2026.json`

### RRG mono · seg_last · 3-slot hold7

- **id**: `rrg-mono-hold7` · **enabled**: `False` · **schedule**: launchd
- RRG mono fresh · seg_last · 3 槽 hold7 · 獨立 launchd 16:40。
- backtest: `reports/research/rrg/rrg_mono_hold7_slot_backtest_2026.json`

### VCP Pivot Gate

- **id**: `vcp-pivot-gate` · **enabled**: `False` · **schedule**: launchd
- VCP funnel · near pivot · breakout close · 5 槽 hold20 · launchd 13:00。
- backtest: `reports/research/vcp/vcp_pivot_gate_slot_backtest_2026.json`

### VCP Coil Close

- **id**: `vcp-coil-close` · **enabled**: `False` · **schedule**: launchd
- VCP funnel 變體 · 訊號日 close 進場 · 5 槽 hold20 · 與 Pivot Gate 共用 launchd。
- backtest: `reports/research/vcp/vcp_coil_close_slot_backtest_2026.json`

### Minervini SEPA Trend Template basket

- **id**: `minervini-sepa-basket` · **enabled**: `False` · **schedule**: ad_hoc
- 月末等權 Stage 2 basket · Minervini 7/7 bulk · ad-hoc 回測。
