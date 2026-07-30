# 【條件閘門交互】champion 只在 B 狀態才發 — 證偽報告

**問題**: champion(外資台指期 `fut_foreign_oi` z60>0 → 隔日 open→close 做多)加上一個
「只在 B 狀態才開倉」的經濟邏輯條件, 是否勝過 champion 單獨? (regime / VIX 已知會贏, 找新的)

**方法**: fwd = t+1 open→close(無前視); gate 為 a-priori 門檻(不在樣本內擬合方向);
walk-forward 5 折擴張窗 + 全 OOS(後30%); permutation 同曝險(在 champion-fire 日內重抽等量 gate-on 日);
DSR 懲罰 N = 本 agent 實跑之全部 16 個 gate-variant. 成本 4bps/turnover.

**champion 基準**: full Sharpe +0.78 / OOS +1.12 / maxDD −0.147 / fire 41.9% / 折 −0.37,+0.55,+2.58,−0.07,+2.45.

## 存活門檻 (全部須過): OOS>champ & wf_pos≥0.6 & wf_min>0 & perm_p<0.10 & DSR>0.95 & exp≥5%

**存活: 無.**

## 各家族實跑結果

| 家族 | 最佳變體 | fire | full/OOS Sharpe | maxDD | wf_pos | perm_p | DSR | 判讀 |
|---|---|---|---|---|---|---|---|---|
| G3 VIX 非spike (已知) | VIX<26&noSpike | 630 | +1.26/+1.37 | −0.056 | 100% | **0.001** | 0.533 | 真的讓 champion 更好(Sharpe↑, maxDD砍半, 5折全正, 選日勝隨機)—但**是已知的 regime gate, 且誠實 16-trial DSR 仍 0.53 未過** |
| G1 市場廣度普漲 | ma50>0.50 | 404 | +0.60/+0.84 | −0.082 | 100% | 0.325 | 0.238 | 只砍曝險、報酬≤champ; perm 不顯著 → **動能的重述, 非新技能**. ma50>0.40 的 OOS+1.80 是單窗假象(wf_min−1.84, perm 0.18) |
| G2 維持率斷頭(扣ETF) | <136 | 2–4 | ≈0 | — | 0% | ns | ~0 | 資料僅 2024-07+, <130 區極罕, **樣本枯竭無法成立** |
| G2b 維持率(含ETF)低尾 | z<−1.5 | 109 | −0.05/+0.06 | −0.130 | 33% | 0.84 | 0.03 | 斷頭區 t+1 仍偏跌 → **傷 champion** |
| G4 現貨外資×期貨同向 | cash>0 | 451 | +0.37/+0.11 | −0.108 | 60% | 0.31 | 0.03 | 雙確認**削掉**期貨領先的好日子, OOS 崩到 +0.11 → 無雙確認 edge |
| G5 投信連買 | streak≥5 | 257 | +0.60/+1.10 | −0.134 | 80% | 0.24 | 0.38 | OOS 貼近但**永遠 ≤ champ**, perm 不顯著 → 只是重選偏多日 |

## 結論
唯一真正改善 champion 的條件是**已知的 VIX-非spike gate**(perm_p=0.001 確認選日有技能、maxDD 砍半、5折全正),
但在本 agent 誠實的 16-trial 多重檢定下 DSR 僅 0.53, 不足以當**獨立可部署** alpha, 且非新發現.
其餘四個經濟邏輯條件(廣度普漲、維持率斷頭、現貨-期貨雙確認、投信連買)**沒有一個**在
walk-forward + permutation + 嚴格 DSR 下勝過 champion 單獨: 廣度/投信只是砍曝險的動能重述,
斷頭區反而傷 champion, 現貨雙確認削掉期貨領先的好日. 呼應既有 6 輪 null. 非投資建議.

輸出: `data/research/dashboard/champion_conditional_gates.parquet` ·
`reports/research/dashboard-completeness/champion_conditional_gates.csv` ·
腳本 `scripts/research/dashboard/study_champion_conditional_gates.py`.

**資料修正副產品**: 既有 `scripts/research/dashboard/breadth_study.py` 的 `load_breadth()` 用
`(w>ma50).notna()` 當分母, 把「無 50日 MA 歷史」的個股也算進分母 → `pct_above_ma50` 被壓到最高只 0.32
(實際應到 ~0.96, median 0.49). 本研究已在本地快取用正確遮罩重算; 該 bug 值得回頭修 breadth_study.py.
