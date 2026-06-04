---
name: etf-holdings-intent
description: >-
  Interpret ETF holdings changes and Position Intent (L1–L6) from stocks.db.
  Use when running --changes --intent, editing signal_engine, position_intent,
  comment_engine, or explaining aligned cohort / rotation / consensus.
disable-model-invocation: true
---

# ETF 持股行為 · Intent 報告

## 觸發

收盤後 **② 收盤持股雷達**，或手動：

```bash
cd "<project-root>"
export PYTHONPATH=src
.venv/bin/python src/sync_etf_holdings.py \
  --etf-codes 00981A,00403A,009816,00407A,00980A,00982A,00992A \
  --changes --intent
```

## 模組（均在 `src/`）

| 模組 | 職責 |
|------|------|
| `stock_db.py` | `compute_etf_holdings_changes`（**shares** 差分，非 weight） |
| `holdings_research.py` | 對齊 cohort、跨 ETF 變動表 |
| `signal_engine.py` | L1–L6、`StockSignal` |
| `position_intent.py` | L2 加權共識、Position Intent |
| `comment_engine.py` | 主句註解（Intent > L8.5 > L2/L3 > L6 > L1） |
| `investment_themes.py` | 靜態主題表 |

## 規則要點

1. **對齊 cohort**：僅共用同一 `prev→curr` 的 ETF 子集做輪動／共識；報告標註未納入 ETF。
2. **官網未更新**：`Skipped write: unchanged snapshot` 為正常，非失敗。
3. **漏跑一天**：少一個 snapshot 日，**無法事後補**持股 API。
4. **Analyze 無 API**：改 intent 邏輯只讀 DB，不在報告時重打 TEJ/官網。

## 測試

```bash
export PYTHONPATH=src
.venv/bin/python -m unittest tests.test_signal_engine -v
```

## 規格

- [docs/PRD.md](../../../docs/PRD.md) §6–§10、§22
