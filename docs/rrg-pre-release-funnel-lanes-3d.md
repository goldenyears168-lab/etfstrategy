# RRG Pre-release Funnel Lanes · 3 日持有 中文解說

> **SSOT 設定檔**：[`config/rrg_pre_release_funnel_lanes_3d.yaml`](../config/rrg_pre_release_funnel_lanes_3d.yaml)  
> **Layer**：research（研究層 · 未採納至 `config/strategy.yaml`）  
> **回測模組**：`src/research/backtest/rrg_pre_release_funnel_lanes_3d.py`  
> **Runner**：`scripts/run_rrg_pre_release_funnel_3d_backtest.py`  
> **產物**：`reports/research/rrg/pre_release_funnel_lanes_3d_YYYYMMDD.{md,json}`  
> **隔日版（勿混用）**：[`docs/rrg-pre-release-funnel-lanes.md`](./rrg-pre-release-funnel-lanes.md)  
> **前置研究**：[`docs/rrg-improving-lifecycle-research.md`](./rrg-improving-lifecycle-research.md)

---

## 1. 與隔日版的核心差異

| 項目 | 隔日版 | **3 日版（本文件）** |
|------|--------|---------------------|
| Outcome | `nxt_ret`（D→D+1） | **`ret_3d`（D→D+3 交易日）** |
| 主 KPI | 隔日勝率 · 隔日均報酬 | **3 日勝率 · 3 日累積均報酬** |
| hist 定義 | 過去 S3 burst 隔日報酬 | **過去 pre-release 命中之 ret_3d 平均 > 0** |
| 設定檔 | `rrg_pre_release_funnel_lanes.yaml` | **`rrg_pre_release_funnel_lanes_3d.yaml`** |

> 均報酬為 **3 日累積 %**，不是日均，也不是年化 CAGR。

---

## 2. Outcome 定義

```text
ret_1d = close[D+1] / close[D] − 1
ret_2d = close[D+2] / close[D] − 1
ret_3d = close[D+3] / close[D] − 1   ← 主 KPI
win_3d = ret_3d > 0
min_ret_3d = D+1～D+3 最差收盤相對 D 收的跌幅
```

- **訊號日 D**：全部進場條件在 D 收盤可算（PIT）  
- **進場基準價**：D 收盤  
- **出場基準價**：D+3 收盤（第 3 個**交易日**）  
- D+3 超出資料範圍者排除

---

## 3. 母池（與隔日版相同）

```text
(end_q ∈ {improving, lagging})
∧ sig_ret < 5%
∧ ( end_q = improving
    ∨ ( end_q = lagging ∧ (disp_contract ∨ sig_ret > 0.5%) ) )
```

**2015–2026 基線（3 日持有）**：

| 指標 | 母池 |
|------|------|
| n | 121,173 stock-days |
| win_3d | **49.4%** |
| mean_3d | **+0.22%** |
| mean_1d | +0.06% |
| P(ret_3d ≤ −5%) | 6.6% |
| P(ret_3d ≤ −8%) | 2.4% |

---

## 4. 2026-07-01 回測摘要

### A. Union 覆蓋

| 組合 | 日曆天/年 | 日層 win_3d | 日層 mean_3d | 日層 mean_1d |
|------|-----------|-------------|--------------|--------------|
| 母池 | — | 49.4% | +0.22% | +0.06% |
| **Strict union（R01–R30）** | **21.8** | **56.2%** | **+0.58%** | +0.48% |
| **Strict + Coverage（+C01–C08）** | **29.5** | **58.3%** | **+0.92%** | +0.42% |

**覆蓋目標 50–80 日/年：⚠️ 未達標**（3 日版 strict 車道條件過嚴 · coverage 僅發現 8 條）。  
後續可放寬 `jaccard_max` 或降低 `mean_min` 以擴 coverage。

### B. Strict 精品車道（3 日 mean 前列 · n≥15）

| ID | rule（摘要） | win_3d | mean_3d | mean_1d | n |
|----|-------------|--------|---------|---------|---|
| **R02** | b2+elag+dip+rv98+gap1 | 61.1% | **+3.16%** | +1.54% | 18 |
| **R17** | b0+disp+base+st46+q35+gap1 | 73.3% | **+2.79%** | +1.34% | 15 |
| **R24** | b2+hist+dip+ex+rv98 | 75.0% | **+2.49%** | +0.64% | 28 |
| **R07** | b2+disp+plag+ex+rv98 | 62.5% | **+2.40%** | +0.95% | 24 |
| **R15** | b2+dip+flat+ex+rv98+gap1 | 57.9% | **+2.30%** | +0.92% | 19 |
| **R05** | b2+disp+base+st46+rv98+gap1 | 75.0% | **+2.02%** | +1.47% | 20 |

白話：**RV98 + 台指強勢（b2）+ disp↓ / excess 帶** 在 3 日持有下 edge 最穩；mean_3d 約為隔日版的 2–3 倍，但 win_3d 門檻需 ≥58% 才算 strict 合格。

### C. Coverage 車道（本次 discovery · 8 條）

| ID | rule | win_3d | mean_3d | d/yr |
|----|------|--------|---------|------|
| C01 | b0+disp+hist+q35+rv98+st7p | 62.8% | +2.07% | 3.7 |
| C02 | b1+dip+flat+hist+plag+st13 | 64.4% | +2.11% | 3.7 |
| C06 | b1+disp+gap1+hist+q35+st46 | 68.6% | +2.19% | 2.9 |
| C08 | b2+disp+hist+no23+q45+rv98 | 58.3% | +2.41% | 1.7 |

---

## 5. June 2026 · Improving 9 筆 case study

這 9 筆為「Improving 象限 · 連 3 日收高 · 合計 >15%」的**事後 outcome**，與 pre-release 進場邏輯不同。

### 5.1 漲幅起點（D = streak 第一日）

| 代號 | 區間 | 合計% | D 日 sig_ret | end_q | 在 pre-release 池？ |
|------|------|-------|-------------|-------|---------------------|
| 2474 可成 | 06-01~06-03 | +26.3% | **+9.83%** | improving | ❌ burst |
| 7734 印能 | 06-16~06-18 | +23.4% | **+9.90%** | lagging | ❌ burst |
| 6515 穎崴 | 06-11~06-15 | +22.1% | **+7.32%** | improving | ❌ burst |
| 2887 台新新光金 | 06-01~06-03 | +21.2% | **+5.35%** | improving | ❌ burst |
| 2880 華南金 | 06-01~06-03 | +19.3% | **+7.70%** | improving | ❌ burst |
| 2337 旺宏 | 06-12~06-16 | +18.9% | +4.64% | lagging | ✅ 在池 · 無 lane 命中 |
| 8996 高力 | 06-05~06-09 | +18.8% | +0.45% | improving | ✅ 在池 · 無 lane 命中 |
| 2887 | 06-02~06-04 | +16.1% | **+6.10%** | improving | ❌ burst |
| 3443 創意 | 06-15~06-17 | +15.9% | **+9.93%** | lagging | ❌ burst |

**7/9 筆在 streak 首日已 burst（≥5%）**——屬 S3 釋放路徑，不是 pre-release 可進場日。

### 5.2 真 pre-burst 進場（D−1 · 前一日收盤）

若改在 **streak 前一日** 以 D 收盤買入、持有 3 日：

| 代號 | D−1 | sig_ret | end_q | **ret_3d（若 D−1 進）** | lane 命中 |
|------|-----|---------|-------|------------------------|-----------|
| 2474 | 05-26 | +4.56% | lagging | +4.36% | — |
| 7734 | 06-12 | −9.74% | lagging | +20.50% | — |
| 6515 | 06-09 | +2.74% | lagging | +11.38% | — |
| 2887 | 05-29 | +1.52% | improving | **+21.20%** | — |
| 2880 | 05-29 | +3.04% | lagging | +19.34% | — |
| 2337 | 06-11 | +3.70% | lagging | +18.93% | — |
| 8996 | 06-04 | +2.28% | improving | **+18.75%** | — |
| 3443 | 06-11 | +1.99% | lagging | +19.85% | — |

**重點**：D−1 雖在 pre-release 池且 3 日報酬極佳，但 **無一筆命中現有 strict/coverage lane**——主因是 **06 初台指強勢日 b2 門檻** 與 **hist / disp / plag 組合** 未同時滿足。這 9 筆屬 **「廣譜 beta 爆發」** 而非漏斗車道可復刻的 pre-release 精品。

### 5.3 對 3 日研究的啟示

1. **Improving 大漲 streak 多從 burst 日開始**，pre-release 框架天然漏接（by design）。  
2. **3 日持有** 若從 D−1 進場，edge 遠高於母池——但需 **更寬的 lagging+improving 過渡 lane**（非現有 b2 精品）。  
3. **8996 / 2887** 型：長 improving streak（8–13 天）+ 低 sig_ret D−1 → 3 日 ret 最佳；可研究 **st7p + imp + 非 b2** 新 coverage lane。  
4. **7734 / 3443** 型：D−1 深跌後 V 轉——現有 dip/elag lane 理論適用，但需 **bench 環境放寬**（非 b2 日）。

---

## 6. 假說（3 日 vs 隔日）

| 因子 | 3 日 horizon 觀察 |
|------|------------------|
| **b2（台指 ≥+2%）** | 仍重要，但 edge 分散到 Day2–3；mean_1d / mean_3d 比約 0.4–0.6 |
| **st13 / st1** | 3 日持有優於隔日；修復早段有發酵時間 |
| **rv98 + plag** | mean_3d +2%～+3% · 樣本小但穩 |
| **hist（3 日版）** | 需 ≥3 次 pre-release ret_3d 史績；與 burst hist 不可混用 |
| **st7p（late streak）** | coverage C01/C03 顯示 late improving + rv98 對 3 日有效 |

**品質 vs 覆蓋**：strict 車道 3 日 win 多在 55–75%，但 union 僅 ~22 日/年；要達 50–80 日/年需新增 **b0/b1 寬鬆 coverage** 或 **improving-only imp+st7p** 車道。

---

## 7. 可復刻檢查清單

- [x] 進場條件全部在 D 收盤可算  
- [x] outcome = D→D+3 交易日  
- [x] hist 改為 pre-release ret_3d 史績  
- [x] 獨立 yaml / 模組 / 報告（未混用隔日版）  
- [ ] Union 50–80 日/年（待下一輪 discovery 參數調整）

---

## 8. 執行方式

```bash
cd "<project-root>"
PYTHONPATH=src python3 scripts/run_rrg_pre_release_funnel_3d_backtest.py
# 寫回 coverage SSOT：
PYTHONPATH=src python3 scripts/run_rrg_pre_release_funnel_3d_backtest.py --write-coverage
# 僅用 yaml 既有 coverage、不做 discovery：
PYTHONPATH=src python3 scripts/run_rrg_pre_release_funnel_3d_backtest.py --no-discovery
```

測試：

```bash
PYTHONPATH=src python3 -m pytest tests/test_rrg_pre_release_funnel_lanes_3d.py -q
```
