---
page_id: strategy_rrg_mono_swap_accel_extension
layer_id: strategy
strategy_id: rrg-mono-swap-accel-extension
title: C18acc Extension overlay
tab_label_zh: C18acc 出場 overlay
tab_label_en: C18acc extension
sort_order: 14
role: 已採納 overlay · combo_spike 出場
web_v1: 策略獨立頁
icon: ri-pulse-line
description_short: I36 combo_spike · 持倉出場 · 獨立 1m poll
parent_strategy: rrg-mono-swap-accel
research_page_id: research_case_rrg_mono_swap_accel
brief_types:
  - c18acc_extension_daily
---

# C18acc Extension overlay

← [策略目錄](strategy_catalog) · 母策略 [RRG 四日加速換倉](strategy_rrg_mono_swap_accel)

**節奏** · 週一至五 **09:06–13:20** 獨立 launchd · 每 **1 分鐘**（與 C18acc 進/換倉 poll 分離）

## 策略定義

對 **C18acc 持倉**（`data/rrg_c18acc_slots.json`）監控 extension 出場：

| 項目 | 值 |
|------|-----|
| 模式 | **combo_spike**（I36 採納） |
| spike | ext_prev ≥ 4% |
| fade | 自尖峰回落 ≥ 1% |
| min_hold | 5 交易日 |
| 下單 | dry-run intent · **不自動送單** |
| 通知 | 觸發出場時 `RUN_C18ACC_EXTENSION_EMAIL=1` |

## 研究追蹤

`config/research.yaml` · `c18acc-extension-radar` 仍 **active / wfa**（季末 CI · Regime 分層），overlay 規格已凍結於本策略。

## 手動

```bash
PYTHONPATH=src .venv/bin/python scripts/run_c18acc_extension_screen.py
```
