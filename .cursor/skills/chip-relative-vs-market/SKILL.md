---
name: chip-relative-vs-market
description: >-
  Relative chip×price analysis vs market (FT gross participation, rolling-β
  excess, alignment, residual z). Use for 華新科/2492 chip knowledge base,
  relative institutional intensity, T+1/T+5 chip prediction backtests, or when
  fixing pathological stock_net/market_net ratios.
---

# Relative chip vs market（相對籌碼 · Research）

Research only · **未採納**不寫 `config/strategy.yaml` · Book only。  
知識庫錨點（華新科）：`reports/research/chip-overlays/2492_knowledge/`。

## When to use

- 華新科／2492 籌碼分析、相對大盤強弱
- 「法人占比」口徑爭議、net/net 爆掉
- 問哪些籌碼情境可預測 **T+1**／**五個交易日**
- 要重跑／迭代命中率：`run_2492_chip_relative_day_backtest.py`

**不要**：把單日故事當因果；用籌碼當唯一主策略；雙機跑 Order。

## 方法硬規則（v2）

| 用 | 禁用 |
|----|------|
| `FT_ntd / market_FT_gross`（占市場總買賣） | `stock_net / market_net` |
| 滾動 60d **β 超額** | 只講股−盤且假設 β=1 當唯一真相 |
| `align_FT = sign(stock)×sign(market)` | 只講「法人買了」不問市場 |
| expanding 分位／過去窗 z | 全樣本分位（前視） |
| OOS 閘門後才稱「可預測」 | 只報樣本內命中率 |

地板：市場 FT 當日總買賣 < NT$500億 → **不報占比**。

特徵細節：[methodology.md](methodology.md)。

## Workflow

```
Task Progress:
- [ ] 1. Read 2492_knowledge/BRIEFING.md + LEARNED_RULES.md
- [ ] 2. Rebuild panel / rescore if data or rules changed
- [ ] 3. Report only OOS-kept regimes for T+1 / T+5
- [ ] 4. If unreasonable → adjust catalogue in rules.py → re-run → update knowledge
```

### Rebuild（MacBook）

```bash
PYTHONPATH=src .venv/bin/python scripts/research/run_2492_chip_relative_day_backtest.py
```

- Cache: `2492_knowledge/cache_market_ft_totals.csv`（缺則打 FinMind）
- Out: `panel_daily.csv` · `daily_signals.csv` · `rule_scores.json` · `LEARNED_RULES.md` · `BRIEFING.md`

### 解讀輸出

1. **BRIEFING**：口訣 + 本輪 OOS 可預測情境  
2. **LEARNED_RULES**：Pass / Reject 表；Reject 原因必讀  
3. **daily_signals**：逐日觸發（一年每一交易日）  
4. Chat 回覆：先講 Pass 規則與命中／lift，再講如何提高命中（稀疏＋組合）

## 預測標籤（誠實口徑）

- 特徵：日 T 收盤價 + 日 T 法人（EOD）  
- `fwd1_r`：T 收 → T+1 收  
- `fwd5_r`：T 收 → T+5 收  
- 輔：`fwd1_oc`（T+1 開→收）較接近「隔夜決策」

同日共現（T 籌碼 vs T 報酬）**不是**預測；技能預設報告 **fwd**。

## 提高命中率（允許的調參）

1. 加嚴：相對出貨 ∧ 價漲籌賣 ∧ |占比|高  
2. 加嚴：同向買 ∧ expanding 分位≥0.8 ∧ 價跌承接  
3. 只在觸發日預測（寧可 n 小）  
4. 分開報多／空命中，不混成「準確率」  
5. IS 只探索、**OOS 才凍結**；過關少於 2 條才允許一次放寬閘門（腳本已內建）

禁止：對同一 OOS 反覆掃閾值直到過關還宣稱樣本外。

## 與其他 skill

| Skill | 關係 |
|-------|------|
| `tier-a-custom-chip-funnel` | 分點×籌碼進場挑戰 Whale；本 skill = **相對市場法人**描述／濾網 |
| `branch-specialist-funnel` | 專家池／watch；2492 為 `skipped_hard0` → 改走大漲前兆分點 |
| `chip-overlays/RESEARCH_PLANS.md` | Plan A FT雙強濾網；本 skill 補「相對市場」層 |

## 華新科大漲前分點 × 籌碼（進階）

計畫 SSOT：`reports/research/chip-overlays/2492_knowledge/SURGE_BRANCH_RESEARCH_PLANS.md`  
Track P 腳本：`scripts/research/run_2492_surge_branch_precursor.py` → `2492_knowledge/surge_branch/`

流程：大漲事件 → T−1/T−2 top 買超席 → 與 chip Tier A **AND 融合**測 T+1 → 無 OOS 增益則停。

## 延伸多檔

換 `sid` 時：複製 runner 參數或加 CLI；知識庫另開 `{sid}_knowledge/`，**勿**把 2492 規則直接宣稱通用。
