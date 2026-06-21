# Backtest Spec（研究軌回測規格）

> 繁中說明 · 對外以業界英文術語為準（Walk-forward OOS、Max Drawdown、Precision@K）

本文件說明 **多條並行 alpha tracks** 的 **per-track backtest spec**（**multi-alpha track competition · no ensemble weighting**）。

**機器可讀 SSOT**：[`config/strategy.yaml`](../config/strategy.yaml)（`strategies.*.backtest`）  
**Loader**：`src/strategy_config.load_strategy_config()`  
**JSON 格式**：`src/research/backtest/slot_backtest_summary.py`

**已移除**：跨軌 league table（`track_evaluation.py` · `evaluation_contract.yaml` · `signal_review` daily 審計）。各軌改由 `run_*_backtest.py` 各自產 summary JSON。

---

## 0. Glossary

**完整術語表**：[terminology.md](./terminology.md)

| 業界術語 | 說明 | 本專案對應 |
|----------|------|------------|
| **IC Decay** | valid → backtest IC 衰減 | `factor_validation` train/valid split |
| **Quantile spread / monotonicity** | 分位數組合收益遞增 | `factor_validation` |
| **Lookahead bias prevention** | T 日交易僅用 T-1 收盤特徵 | VCP / copytrade 隱含 |
| **Walk-forward OOS** | 滾動樣本外 | VCP benchmark（封存） |

---

## 1. Design principles

1. **Score semantics differ by track** — 不強制所有軌共用 Rank IC。
2. **Per-track backtest spec** — 每軌在 `strategy.yaml` 定義 `spec_type` · metrics · JSON 路徑。
3. **No league table** — 無 cross-track 排名表；各軌 VFP 獨立對照 JSON / 報告。
4. **Benchmark**：台股現貨 `IX0001`（slot 策略以 excess vs bench 為主）。

---

## 2. 產出流程

```text
config/strategy.yaml  →  strategies.*.backtest
        ↓
run_*_backtest.py / write_copytrade_slot_summary.py
        ↓
reports/research/**/{track}_slot_backtest_*.json
        ↓
（可選）strategy hub / 手動閱讀
```

| Track ID | Run script | Summary JSON |
|----------|------------|--------------|
| `00981a-l1h9` | `scripts/run_00981a_copytrade_backtest.py` · `write_copytrade_slot_summary.py` | `reports/research/00981a-copytrade/l1h9_slot_backtest_2026.json` |
| `rrg-mono-hold7` | `scripts/run_rrg_mono_breadth_backtest.py` | `reports/research/rrg/rrg_mono_hold7_slot_backtest_2026.json` |
| `vcp-pivot-gate` | `scripts/run_chunge_funnel_backtest.py` | `reports/research/vcp/vcp_pivot_gate_slot_backtest_2026.json` |
| `vcp-coil-close` | `scripts/run_chunge_funnel_backtest.py` | `reports/research/vcp/vcp_coil_close_slot_backtest_2026.json` |
| `minervini-sepa-basket` | `scripts/run_broad_momentum_tv_backtest.py` | `reports/minervini-sepa-basket/backtest_summary.json` |

共用模組：`research.backtest.slot_backtest_summary`（`SlotBacktestConfig` · `build_summary_payload` · `write_slot_backtest_summary`）。

---

## 3. Per-track specification（現行）

### `00981a-l1h9` · `slot_strategy_backtest`

| Field | Value |
|-------|-------|
| Alpha type | ETF holdings copytrade |
| Spec | L1H9 · 9 slots · L1 open / hold9 |
| Module | `copytrade/signals` · `copytrade_backtest` |
| Metrics | n_periods, win_rate_vs_bench_pct, mean_excess_pct, mean_return_pct |

方法論：[00981a-copytrade-research-methodology.md](./00981a-copytrade-research-methodology.md)

### `rrg-mono-hold7` · `slot_strategy_backtest`

| Field | Value |
|-------|-------|
| Alpha type | RRG mono fresh · seg_last |
| Slots / hold | 3 slots · hold7 |
| Module | `rrg_mono_backtest` |

### `vcp-pivot-gate` / `vcp-coil-close` · `slot_strategy_backtest`

| Field | Value |
|-------|-------|
| Alpha type | VCP funnel |
| Slots / hold | 5 slots · hold20 |
| Module | `chunge_funnel_backtest` |
| Sweep | `scripts/run_chunge_funnel_sweep.py` |

### `minervini-sepa-basket` · `stock_basket_backtest`

Ad-hoc Stage 2 basket · 見 `config/broad_momentum_tv.yaml`。

---

## 4. 已退役（勿再引用）

| 項目 | 說明 |
|------|------|
| `p6-tier-flow` · `cross_sectional_ic` | `score_engine` / `signal_review` daily 鏈已移除 |
| `track_evaluation.py` | 跨軌 summary markdown |
| `evaluation_contract.yaml` | 併入 `strategy.yaml` |
| `00981a-v9-hybrid` · `qlib-tw-factor` | 見 PRD §10 |

---

## 5. Modules

| Module | Role |
|--------|------|
| `src/strategy_config.py` | Load `strategy.yaml`（採納規格） |
| `src/research_config.py` | Load `research.yaml`（探索主題） |
| `src/research/backtest/slot_backtest_summary.py` | Slot JSON schema + writers |
| `src/research/backtest/copytrade_backtest.py` | L1H9 回測 |
| `src/research/backtest/chunge_funnel_backtest.py` | VCP funnel 回測 |
| `src/factor_validation.py` | Alphalens-style factor IC（Phase 2 · 獨立） |

營運：[daily-operations.md](./daily-operations.md)
