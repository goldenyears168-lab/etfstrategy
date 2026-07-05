---
page_id: strategy_minervini_sepa_basket
layer_id: strategy
strategy_id: minervini-sepa-basket
title: Minervini SEPA basket
tab_label_zh: Minervini SEPA
tab_label_en: Minervini SEPA
sort_order: 20
role: 已採納凍結規格 · 月末等權 basket
web_v1: 策略獨立頁
icon: ri-line-chart-line
description_short: Trend Template 7/7 · 月末等權 · 獨立 16:35 launchd
brief_types:
  - minervini_sepa_basket_daily
---

# Minervini SEPA basket

← [策略目錄](strategy_catalog)

**節奏** · 週一至五 **16:35** 獨立 launchd · 僅 **月末交易日** 產調倉 intent

## 策略定義

月末等權持有 **Minervini Trend Template 7/7**（bulk 掃描不含 RS）的 Stage 2 個股；無合格標則空倉。

| 項目 | 值 |
|------|-----|
| 宇宙 | ETF 成分股 `stock_daily_bars`（約 133 檔） |
| 調倉 | 月末最後交易日 · 等權 |
| 門檻 | Trend Template ≥ 7/8（RS 省略） |
| 下單 | **dry-run intent** · 人工確認後 `submit_intents.py` |
| 通知 | 組合變動時 `RUN_MINERVINI_SEPA_EMAIL=1` 寄信 |

## 與其他策略

- 與 **VCP funnel**（突破池）互補，非 ensemble。
- 與 **RRG / Copytrade** 無共用槽位。

## 手動

```bash
PYTHONPATH=src .venv/bin/python scripts/run_minervini_sepa_daily_brief.py
.venv-fubon/bin/python scripts/order/submit_intents.py \
  reports/order/intents/minervini-sepa-basket_<date>.json --dry-run
```
