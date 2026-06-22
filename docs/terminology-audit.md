# Terminology audit · 清障清單

> 對照 [terminology.md](./terminology.md) · 2026-06-21  
> Cursor agent 規則：`.cursor/rules/terminology.mdc`  
> **狀態：2026-06-21 清障完成**

**圖例**：✅ 已修 · ⏸ 刻意保留（compat）

---

## P0 · 文件與產物敘述

| 狀態 | 位置 | 修正 |
|------|------|------|
| ✅ | Operations → **Facts layer** | `strategies.yaml` · terminology · architecture |
| ✅ | `docs/PRD.md` §12 / §9 | etf-daily + regime-daily · strategy.yaml + research.yaml |
| ✅ | `docs/00981a-copytrade-research-methodology.md` | Optimal hold (H*) · Trend posture stratification |
| ✅ | `reports/samples/README.md` | canonical 路徑 |
| ✅ | `reports/*_00981a_regime_horizon_l1.md` | 重跑 `--analyze-regime-horizon --write-report` |

---

## P1 · 報告生成器

| 狀態 | 位置 | 修正 |
|------|------|------|
| ✅ | `copytrade_regime_horizon.py` | English report headers |
| ✅ | `copytrade_backtest.py` | Optimal hold (H*) |
| ✅ | `inst_flow_backtest.py` · `inst_flow_981a_overlap.py` | Optimal hold (H*) |
| ⏸ | `copytrade_regime_horizon.py` | `regime_name` in bucket loop（讀舊 batch） |

---

## P1 · Regime 層 user-facing

| 狀態 | 位置 | 修正 |
|------|------|------|
| ✅ | regime daily / pipeline / strategies / daily_sync | Regime four-axis diagnostic |

---

## P2 · 程式符號

| 狀態 | 位置 | 修正 |
|------|------|------|
| ✅ | `flow_returns.flow_tape_regime()` | canonical；`market_regime()` deprecated alias |
| ✅ | `flow_event_legs.flow_tape_regime` | schema + `_migrate_flow_tape_regime_column` |
| ✅ | `sync_flow_event_legs.py` | payload key `flow_tape_regime` |
| ⏸ | `stage_analysis` deprecated aliases | `trend_posture` canonical |
| ⏸ | `regime_config` LEGACY paths | fallback only |
| ⏸ | copytrade DB migration DDL | one-time RENAME |

---

## 驗證

```bash
rg -n '甜蜜点|甜蜜點|Regime 分层|research_digest\.md' \
  --glob '!docs/terminology*.md' --glob '!reports/**' .

PYTHONPATH=src .venv/bin/python -m unittest \
  tests.test_research_config tests.test_copytrade_regime_horizon -v

PYTHONPATH=src .venv/bin/python scripts/run_00981a_copytrade_backtest.py \
  --analyze-regime-horizon --write-report
```

---

## 相關

- [terminology.md](./terminology.md) — SSOT · **§10 用語對照總表**
- `.cursor/rules/terminology.mdc` — agent 規則
