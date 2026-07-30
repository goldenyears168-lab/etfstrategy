# 狀態分格條件報酬表 — State-Grid Conditional Returns (interaction study)

家族: 把每日按 4 個先驗二元軸分格,讀各格 fwd20 條件報酬 + 校正格子數。
軸(先驗、無門檻搜尋,僅中位數分割): champ = z60(fut_foreign_oi)>0 / vix_hi = VIX>252d滾動中位 /
breadth_str = pct_above_ma200 > 252d滾動中位 / above_ma200 = ix_close>MA200。→ 2^4=16格。
樣本 2019-03-26→2026-07-27, n=1765 可用日。fwd20 = t+1開→t+20收(無前視)。基線 fwd20=+1.72%, win 65.5%。

## 核心發現(誠實)

**★ breadth(市場廣度)是唯一在已知 champ×MA200 regime 之上真正加分的軸。VIX high/low 無用。**

格內增量(S = champ+ & above_MA200, 本身 fwd20 +2.46%):
| 加條件 | n | fwd20 | win |
|---|---|---|---|
| breadth STRONG | 320 | **+3.42%** | 79% |
| breadth WEAK | 187 | +0.82% | 55% |
| vix HIGH | 160 | +1.43% | 58% |
| vix LOW | 347 | +2.93% | 76% |

- 同一 champ+/站上MA200 regime 內,**廣度強把 fwd20 漂移拉到 4 倍**(+3.42% vs +0.82%)。
- **格內廣度置換檢定**: 觀測(strStr−brWk)gap=+2.60%,隨機分割 5000 次 null≈0%,**p<0.0001** →
  是「廣度」本身、不是任意分割的重述。
- VIX 反而 **低 VIX 優於高 VIX**(+2.93% vs +1.43%),「買恐慌」在此不成立;VIX 只該當罕見恐慌 gate,
  不宜當連續 high/low regime 軸切漂移。
- 廣度效應在 champ+ 之外也在(champ−: brStr +1.91% vs brWk +0.76%),champ+ 內最大 → 通用漂移增強子。

## 多重檢定 + 可交易性(命門)

16格中 fwd20 NW-t(HAC lag20)排序前二皆過 Bonferroni(16格): C1111 +3.93% NW-t 5.22 p_bonf≈0、
C1011 +3.27% NW-t 4.43 p_bonf≈0。走查(擴張窗 40-55-70-85-100%): C1011 各 OOS 折 +2.25/+3.92/+6.40%
**全正且遞增**,跡象穩定。

但**可交易 overlapping 20日持有** Sharpe + Deflated-Sharpe(用同一 16格 trial 離散度懲罰, SR*_ann +1.06):
| 策略 | ann.Sharpe | DSR |
|---|---|---|
| champ+ only | +1.02 | 0.458 |
| champ+ × aMA200(已知regime) | +1.25 | 0.704 |
| champ+ × aMA200 × brStr(候選) | **+1.46** | **0.874** fail |
| champ+ × aMA200 × brWk(對照) | +0.25 | 0.013 |

Sharpe 單調上升 1.02→1.25→1.46、DSR 0.458→0.704→0.874,brWk 對照崩到 0.25 → 廣度強確實是漂移所在。
但候選 **DSR 0.874 仍未過 0.95** — 扣掉 16格搜尋的選擇性 null 後,增量 Sharpe 雖真但幅度落在門檻內。

## Verdict

- **存活(有保留)**: 市場廣度(%股站上自身MA200)強/弱,是 champ×MA200 regime 之上 **真實、非顯然、
  格內置換 p<0.0001 的漂移判別軸**。可部署為既有 champ×MA200 系統的 **信心縮放 / regime 精修 tilt**
  (廣度弱時漂移塌回基線甚至以下 → 該降曝險),**不是**獨立可部署新 alpha(嚴格 DSR 未過, 0.874)。
- **證偽**: VIX high/low 當漂移 regime 軸 = 無用(低 VIX 反優)。呼應「VIX 僅罕見恐慌 gate」既有結論。
- 與專案 6 輪 null + regime-conditioning 才是耐久洞見的模式一致: 交互 **精修**已知 regime、
  但不清越嚴格多重檢定成為新獨立 alpha。非投資建議。

檔案: data/research/dashboard/state_grid_cells.csv, state_grid_walkforward.csv, pct_ma200_breadth.parquet;
腳本 scripts/research/dashboard/state_grid_conditional_returns.py, state_grid_incremental_test.py。
