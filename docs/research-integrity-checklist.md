# 研究誠信檢查清單（Research Integrity Checklist）

> 繁中 SOP · 供 agent／人類研究者在**判定一個假說 `passed`／`rejected`、或把數字寫進
> `config/research.yaml` 之前**自我審查用。清單裡每一條都是本 repo 研究史裡**至少發生
> 過一次、且每次都要靠人工／agent review 才抓到**的方法論 bug——不是假設性風險。

**SSOT 來源**：`config/research.yaml` 內 `tmf-slow-pattern-cell`／
`tx-donchian-multiwindow-channel`／`tx-donchian-flat-default-bracket` 三條研究線的
`do_not` 區塊與 evidence 欄位（截至 2026-08-07）。案例 ID 可直接在 `config/research.yaml`
用 `grep -n "<hypothesis-id>"` 查到完整 evidence 段落。

**輕量第一道防線**：`scripts/research/lint_backtest_engine.py`（靜態掃描，見文末，
**不能取代**本清單的人工／agent review）。

**如何使用**：新回測腳本寫完、或任何一輪 evidence 要從 `pending` 定案為
`passed`/`rejected` 前，把下方 6 條核心清單＋附錄陷阱表過一遍；每條的「自我檢測方法」
盡量寫成可執行的 assert／擾動測試，不要只用眼睛看數字順不順眼。

---

## 七大核心 bug 類型

### BUG-1 · Session 結尾未平倉部位被靜默丟棄（Session-end accounting gap）

**問題描述**：回測引擎只結算「已翻倉平掉」的交易，session（或資料窗口）結尾仍持有的
部位從未被計入損益、也從未出現在交易列表裡。因為它「不存在於任何一筆 trade record」，
`n_signals`／`n_fills` 這類粗略計數可能剛好對得上（訊號數＝已平倉交易數），完全不會
報錯，數字看起來乾淨、其實漏了整段尾部風險。

**如何自我檢測**：
- **強制對帳不變量**：對每個 (day, session, sleeve) 區塊斷言
  `n_events == n_entered + n_skipped_in_position`，且 `n_entered` 必須等於「已平倉交易數
  ＋ 期末強制平倉數」，兩者缺一都要讓 assert fail，不能只挑其中一個。
- **期末強制平倉（force-close）測試**：對每個資料窗口的最後一根 bar，明確用收盤價
  force-close 所有still-open部位，重新計入損益，比較「有 force-close」vs「原始（無
  force-close）」兩個總損益——差距大就代表原引擎有這個洞。
- **窗口切分穩定性測試**：故意把同一份資料切成不同長度的子窗口重跑（例如 83 天 vs
  40+43 天兩段各自跑），如果總損益因為切法不同而系統性偏移（子窗口越多、缺口越大），
  就是尾部位漏記的訊號。
- 任何「always-in／stateful 反手」系統（訊號觸發時永遠持有部位，靠下一個訊號才換邊）
  天生**保證**每個窗口結尾有一個未平倉部位，這類引擎寫規格時要把「期末強制平倉或明確
  結轉」列為**硬性**規格項，不是可選的收尾動作。

**本 repo 案例**：
- `H-TXD-SESSION-END-ACCOUNTING`（`tx-donchian-multiwindow-channel`，status: passed
  ——即「這是真的 bug」）：83 天 ×2 session×3 sleeve＝498 個被丟尾部位，force-close
  誠實結算後合計 −92,174.2pt，把已計交易的 +79,863.9pt 翻成真實 −12,310.3pt；
  `H-TXD-WINDOW-RECALIBRATION`（window=233 單獨 +19,362.6→−5,417.8pt）與
  `H-TXD-MULTIWINDOW-PORTFOLIO` 因此雙雙從 passed 翻案為 rejected。
- `H-SC-EDGE-REVERT`（`tmf-slow-pattern-cell`）：7.3%（786/10,740）觸價進場因邊界條件
  被靜默丟棄，「同根K入出場檢查通過」這句話只檢查了倖存交易，天生看不到被丟掉的那批。
- `H-TXFB-FLAT-DEFAULT-BREAKS-PATH-DEPENDENCE`（`tx-donchian-flat-default-bracket`）是
  這個坑的**正面示範**：改用「進場即鎖定出場條件＋session 邊界強制平倉」的
  flat-default 架構，並用 996/996 對帳不變量全過來證明缺口在架構層面被根除。
- 同類歷史案例（repo 內其他研究線，供旁證非本次三個 topic）：H-RECOVERY 的
  `hang_anchor`、ASC 的 close-fill 記帳缺口——本 repo 至今已第四次在「結算細節」上
  翻車，模式高度重複。

---

### BUG-2 · 同根／自我循環 look-ahead（Same-bar circular look-ahead）

**問題描述**：一個號稱「因果（causal）」的量測，實際上在計算 `t` 時刻的值時用到了
`t` 時刻自己（或更晚）才會確定的資料（例如收盤價 `C[t]`、當根最後一筆 tick、或這根
K 收盤才會知道的樞紐點）。這種污染常常隱藏在滾動視窗公式的邊界條件裡，naive 單元測試
（只測「不同天資料互不影響」）抓不到，因為污染只發生在同一根 bar 內部。

**如何自我檢測**：
- **擾動測試（本清單最重要的單一技巧）**：在某個歷史時刻 `t` 注入一個異常值（改動
  `H[t]`／`L[t]`／`C[t]` 或某筆 tick 價），重新跑整個因果量測序列，斷言 `t` **之前**
  （`index < t`）算出來的值完全不變（逐位元相等）；如果 `sw_th_arr[t]`／`ER(t)` 這類
  「當根」自己的值也被影響，那就是同根循環污染；正確設計應該連 `index == t` 自己都
  不受影響（因為因果特徵理論上只能用 `< t` 的資料），最早受影響的 index 必須是
  `t+1` 或更晚。
- 對任何函式命名帶 `causal_*`／`*_causal`／`ex_ante` 的地方，逐一確認公式裡的每個
  index 是 `t-1` 或更早，不要相信函式名稱本身；分子分母都要檢查，不能只查其中一邊。
- 對「腿（leg）」「通道剛 lock 那根 bar」這類狀態機事件，特別檢查：判定「這根 bar 是否
  觸價 / 是否確認」時，用的是這根 bar 自己完整結束後才知道的資訊（如最後一筆 tick、
  收盤價本身），還是進場當下就已經鎖定的常數。
- **合成序列 + 真實資料各跑一次**：合成序列方便精確控制注入點與預期結果，真實資料則
  確保修復沒有引入新的邊界 bug；兩者都要過。

**本 repo 案例**：
- `H-SC-NET-VELOCITY-RECOGNITION-SPEED`（`tmf-slow-pattern-cell`）：Kaufman Efficiency
  Ratio `ER(t)` 最初實作用到了 `C[t]` 自己；修復後改成 `ER_causal(t,w)` 整個窗口往回移
  一根，分子分母嚴格只碰 `index ≤ t-1`。修復前擾動測試在 `t=100` 注入異常值時
  `sw_th_arr[100]` 當場被影響（20.0→39.036）；修復後 `sw_th_arr[100]` 完全不變，第一個
  受影響的 index 延後到 `t=101`，才算過關。
- `H-SC-TICK-TRIGGER`（`tmf-slow-pattern-cell`）：通道剛 lock 的那根 bar，用該 bar
  **自己最後一筆 tick** 的價格回頭判定該 bar 開頭是否觸價；修復（加 `ch_just_locked`
  guard）後又額外發現出場側（flip 通道重錨）還有 8 次同構污染，合計 16+8=24 次全部堵住
  後，原本 +2,356pt 的方向仍成立但縮水到 +1,363pt（−42%）。
- `H-SC-EDGE-REVERT`（`tmf-slow-pattern-cell`）：`leg_end` 強制平倉（13.8% 交易）用
  「這條腿自己的未來終點價格」在 mark；作者自己設計的「排除腿起點 5 根內進場」敏感度
  過濾完全沒篩到它——過濾篩選本身也要驗證有沒有覆蓋到已知的污染來源。
- `H-TXD-WINDOW-RECALIBRATION`（`tx-donchian-multiwindow-channel`）：ATR 濾網門檻原本
  用「當天全天含未來 K 棒的分布」算，修成「只用 IS 天池化計算一次固定值」後結論差異
  `<1%`——這次修復沒有推翻結論，但**修復本身仍是必要步驟**，不能因為「反正結論不變」
  就跳過驗證。

---

### BUG-3 · Fold-aggregate 後見之明（Fold-level hindsight leakage）

**問題描述**：用「一個 fold（或一段測試窗口）自己全部時間跨度的統計量」（例如整個
fold 120 天的年化波動率）去解釋／篩選這個 fold 的表現好壞，然後把這個統計量包裝成
「regime 條件」拿去做 walk-forward gate。問題在於：計算這個統計量的當下，你已經用到了
這個 fold**未來**全部的資料——這不是一個在任何一天都能即時算出來的量，是先看完整個
fold 的結果、再回頭幫它找一個「看起來像因果」的解釋。

**如何自我檢測**：
- **只用 `T-1`（或更早）已知資訊重新操作化**：任何 regime／gate 變數都要能寫成
  `value(t) = f(data[:t-1])`，不能是 `f(整個 fold 的資料)`；把描述性發現（block-level
  統計量）改寫成 trailing／expanding 版本（例如「fold 自己 120 天年化 vol」改成
  「trailing 120 天、`shift(1)`、causal」）後**重新跑一次**，確認結論還在。
- **對照組**：causal trailing 版本算出來的分組結果，如果跟「fold-aggregate」版本完全
  一致（例如同樣的日子被判定為同一組），要高度懷疑——真正因果的量測通常會產生跟
  after-the-fact 版本不同的分組，因為它看不到未來。
- **門檻單調性壓力測試**：不要只看門檻在某個區間（例如 50%~80%）單調改善就下結論，
  要往兩端推（例如推到 85%/90%）確認趨勢會不會反轉；如果某個 fold 在任何門檻下的
  「通過天數」都不隨門檻收緊而變化，代表這個 gate 根本沒有真的碰到那個 fold，改善
  只是其他 fold 佔比機械上升造成的假象。
- **檢查「多數 fold 轉正」而非只看「OOS 總損益轉正」**：少數 fold 的巨大正貢獻可以
  蓋過多數 fold 持續的虧損，讓總和看起來轉正，但這不代表 gate 真的解決了問題 fold。

**本 repo 案例**：
- `H-TXFB-FOLD2-DIAGNOSIS-AND-LONGVOL-GATE`（`tx-donchian-flat-default-bracket`）：診斷
  階段發現「120 天 fold-aggregate realized vol 能完全分開 5 個 fold」（losers 全部
  16.8~21.3%、winners 全部 >32.8%）；但把它改寫成 causal trailing 120 天版本（`shift(1)`、
  IS expanding 百分位門檻）後，**直接證偽**——Fold1、Fold2 的通過天數在 0/30/40/50/60
  百分位門檻下**完全沒有變化**（永遠 120/120 天），代表這兩個 fold 從來不曾被 causal
  方式判定為「低 vol」，乾淨分離是後見之明假象。
- `H-TXFB-VOL-REGIME-GATE`（同 topic）：trailing 20 天 vol 門檻從 50% 拉到 80% 時 OOS
  總損益單調改善（−1,467.7pt→+1,571.2pt），乍看像 regime story 成立；但拉到 85%/90%
  後趨勢反轉，且決定性診斷是 Fold5 在 90% 門檻下仍有 97/120 天通過——收緊門檻只是不斷
  排除 Fold1/2/4 的交易日、讓 Fold5 佔比越來越高，不是真的把各 fold 內部篩成正報酬。
- `H-TXFB-VIXTWN-TREND-REGIME-GATE`（同 topic）：OOS 總損益隨門檻收緊持續改善到
  +7,906.1pt，這次 Fold5 內部確實有被篩出更好子集（不是純佔比假象），但 kill
  criterion（design doc 要求「多數 fold 正報酬」）在所有 6 個門檻下都沒過——正報酬
  fold 數量固定停在 2/5，Fold1/2/4 從未轉正，2/5 不是多數。

---

### BUG-4 · 統計顯著性漏做或雙重標準（Missing / double-standard significance testing）

**問題描述**：兩種變體都出現過——(a) 完全沒做正式統計檢定，只比較點數總和的正負號，
把「總和是正的」直接讀成「有 edge」；(b) 有做檢定，但校正方法（例如 Newey-West HAC
自相關校正）只挑對自己有利的方向套用，或用寬鬆標準對待負向結果、嚴格標準對待正向
結果（反之亦然）。

**如何自我檢測**：
- **逐日／逐筆層級的正式檢定是預設動作，不是額外選項**：naive t-test 之外，一律補算
  逐日序列的 lag-1 自相關；只要 `|自相關| > 0.1` 這種量級，就要用 Newey-West HAC（掃過
  多個 `maxlags`，例如 1/5/10/20）重算 p 值，不能只信 naive p。
- **統一門檻，不因方向放寬或收緊**：預先定義「HAC 校正後 p<0.05 在全部測試的 maxlags
  下都成立，才算穩健顯著」，正向結果與負向結果套用完全同一套標準；寫 verdict 文字前，
  反問自己「如果這個方向反過來，我還會用同樣的措辭嗎」。
- **正確區分兩種「不顯著」的敘事**：p 值不顯著只代表「現有樣本量的檢定力不足以區分
  『真的沒有 edge』與『有一個小到目前偵測不到的 edge』」，**不等於**「證明了沒有
  edge」；但也不能反過來讀成「所以可能有正面 edge」——要看點估計在所有穩健性排列
  （拿掉最慘 1/3/5 天等）下的符號是否一致，符號穩定不變但統計不顯著，才是誠實的
  「證據不足」定性，不是「可能有正面訊號」。
- **算多重比較的基準機率**：sweep 多組參數／多個特徵時，先算「純雜訊下預期會有幾個
  通過門檻」（例如 5 摺獨立丟硬幣「至少 4 摺同號」機率約 37.5%，測 8 個特徵預期
  ~3 個純運氣通過），拿實際通過數跟這個基準比較，不能只看「有幾個通過」的絕對數字。

**本 repo 案例**：
- `H-SC-STAT-SIGNIFICANCE-GAP`（`tmf-slow-pattern-cell`，方法論假說）：本研究線至今
  所有 `rejected`/`passed` 判定，全部只比較點數總和正負號，從未做過正式檢定；補做後
  59 天配對差異 `mean=−7.5pt, se=61.0pt, t=−0.123, p=0.90`，之前「IS 組 −296.6% 大幅
  劣化、OOS 組 +253.8% 改善」的巨幅逆轉，可能只是日與日之間巨大變異度裡的隨機抽樣。
  第八輪擴大到 484 天乾淨 OOS 資料，naive `p=0.029`（顯著負），但逐日序列 lag-1
  自相關 `+0.335`，Newey-West 校正後 p 值回升到 `0.058–0.091`，不再顯著——引用
  「p=0.029、SIGNIFICANT NEGATIVE」是 `do_not_v2` 明確列出的禁止行為。
- `H-SC-FLIP-STOPLOSS-TAILCUT`（`tmf-slow-pattern-cell`）：統計方法審查者抓到雙重
  標準本尊——build agent 把無停損基準標記「SIGNIFICANT NEGATIVE·統計上可信」，但
  HAC 校正後三個 maxlags 的 p 值（0.058/0.090/0.075）全部超過 0.05；同一支腳本的
  判定邏輯會把「naive 顯著、HAC 不顯著」的**正報酬**結果降級為「僅 naive 顯著」，
  卻沒有對**負報酬**結果套用同一套降級標準。
- `H-TXD-SESSION-END-ACCOUNTING` 補做輪（`tx-donchian-multiwindow-channel`）：用同一套
  「HAC p<0.05 在全部 maxlags 都成立」的唯一標準判定 w233／組合兩個序列，結果都是
  「不顯著」——正確定性為「83 天樣本量下無法區分『沒有 edge』與『有一個小到測不到的
  負 edge』」，並在 `do_not` 明確列出「不要反過來讀成可能有正面 edge」（因為 lag-1
  自相關實測為負，不是正自相關撐高 naive p 值的情境）。

---

### BUG-5 · Fill clamp／選擇性有利成交價（Favorable-price fill clamp）

**問題描述**：回測引擎在「實際下一根開盤價」與「訊號當根觸及的邊緣／中線／停損價」
之間，單方向取對交易方**更有利**的那一個當作成交價。這類 bug 特別陰險，因為它不會
讓 `n_signals`／`n_fills` 對不上、也不會被「同根K入出場」這種 bar-index 分離檢查抓到
——後者只檢查「進出場不是同一根K」，不檢查「成交價本身是否誠實」，兩者是完全獨立的
兩件事。

**如何自我檢測**：
- **誠實 next_open 重跑對照**：把所有 favorable-price 判斷邏輯替換成強制用「訊號確認
  後下一根 bar 的開盤價」成交，重新跑一次，跟原始版本比較差距；如果差距佔宣稱獲利的
  比例很大（本 repo 案例是 88%），原始版本的「edge」多數是回測機制假象。
- **顯式 grep 可疑 pattern**：搜尋成交價／出場價賦值附近有沒有 `min(`／`max(` 在兩個
  候選價格之間選擇、或任何「取較有利」的條件分支/註解；凡是「進出場價格有兩個候選、
  程式挑一個」的地方都要人工確認挑選邏輯不偏袒交易方向。
- **同根K檢查 ≠ 成交價誠實檢查**：「入出場不是同一根bar」這類 bar-index 分離檢查
  過關，不代表成交價本身沒問題，兩者必須分開驗證，不能因為前者通過就跳過後者。
- **勝率／均值 sanity check**：誠實成交後的勝率若低於銅板（<50%）而 clamp 版本勝率
  明顯更高，這本身就是 clamp 存在的間接證據。

**本 repo 案例**：
- `H-SC-EDGE-REVERT`（`tmf-slow-pattern-cell`）：表面數字 83 天、9,954 筆交易，淨賺
  146,312.7pt、勝率 53.99%，「同根K入出場」技術上通過。3/3 獨立審查者找到：程式碼在
  「實際下一根開盤價」與「訊號當根觸及的邊緣/中線/停損價」之間單方向取對交易方更有利
  的那個（clamp），貢獻 128,786.5pt，占宣稱淨利的 **88%**。拿掉 clamp、改用誠實
  next_open 重新模擬後：淨利崩到 11,022.0pt（跌 92.5%）、勝率掉到 **44.14%（低於
  銅板）**。

---

### BUG-6 · 自動生成的 caveats／verdict 標籤本身有 bug（Buggy auto-generated verdict labels）

**問題描述**：回測腳本自動下的「穩健性標籤」（如
`robustness_verdict = "OOS_improvement_holds_up_at_or_above_IS_level"`）本身的判斷
邏輯有 bug（常見是正負號邊界條件沒處理），把本應是**最可疑**的結果（例如「IS 大幅
劣化、OOS 大幅改善、方向完全相反」）系統性誤分類成**最樂觀**的標籤。因為標籤是自動
生成、語氣讀起來很有把握，容易被直接引用而不去查逐日明細。

**如何自我檢測**：
- **凡是自動生成的 verdict／caveat／robustness 標籤，一律視為「未驗證的聲稱」**，
  在引用進 evidence 或 do_not 之前，必須人工（或另一個獨立 agent）重算判斷邏輯裡的
  每一個分支條件，尤其是「其中一邊為負數」這類邊界情況（例如
  `oos_pct < is_pct*0.5` 這種公式，當 `is_pct` 為負時，右式只會更負，導致任何正
  `oos_pct` 都自動落入最樂觀分類）。
- **拿判斷邏輯本身的原始碼跑幾組手算案例**：構造「IS 正、OOS 正」「IS 負、OOS 正」
  「IS 正、OOS 負」「IS 負、OOS 負」四種組合的假數字，代入判斷函式，確認分類結果
  符合直覺，尤其要故意測「其中一邊為負」的情況。
- **逐日明細比對**：任何「聲稱有改善」的結果，都要抽查逐日/逐筆明細，確認總和不是
  被少數個位數極端日主導（若拿掉 top1~top4 天結論就反轉，代表這不是廣泛分佈的優勢）。
- **敘事與底層數據交叉核對**：script 自動生成的文字敘述（例如「三組都在某個 bar 分岔」
  「某筆虧損是因為某個跳空造成」）要用逐筆交易紀錄獨立核對，不能假設敘事本身正確
  ——本 repo 案例證明敘事可能引用錯誤的價位或錯誤的分岔點。

**本 repo 案例**：
- `H-SC-TICK-TRIGGER`（`tmf-slow-pattern-cell`）：擴大樣本後 IS 組 tick 大幅劣於 bar
  （delta −296.6%）、OOS 組仍正向（+253.8%），方向完全相反；腳本自動下的
  `robustness_verdict` 標籤 `"OOS_improvement_holds_up_at_or_above_IS_level"` 被審查者
  判定是 bug 產生的誤導性標籤——判斷邏輯 `oos_pct < is_pct*0.5` 在 `is_pct` 為負時失效，
  沒有處理「IS 端根本沒有 improvement 可言」的情境。
- `H-SC-FLIP-STOPLOSS-TAILCUT`（`tmf-slow-pattern-cell`）：build agent 的「根因追查」
  敘事把某筆 −436pt 虧損歸咎於「17618→19260(~1642pt)跳空」，但審查者核對後發現實際
  進場價是 18826、出場 19260，真正的移動是 2 根 bar 內約 434pt；敘事還誇大成「三組
  都在 bar217 分岔」，實際上只有其中一組在 bar217 分岔，其餘到 bar421 都跟基準逐筆
  相同。

---

### BUG-7 · 尾部/強制平倉交易掩蓋結構性虧損（Tail/forced-close trades masking a structural loss）

**問題描述**：PnL headline 轉正，但拆開來看其實是少數幾筆 session-end 強制平倉／
long-tail 持倉貢獻了絕大多數正報酬，其餘絕大多數交易（正常訊號出場的那批）合計是
穩定虧損。這跟 BUG-6（自動 verdict 標籤誤判）不同：就算沒有任何自動標籤 bug、就算
逐日 t 檢定/HAC 校正都做對了，只看「淨值轉正」本身仍會誤判——因為多數交易的結構性
虧損被少數不具代表性的尾部事件抵銷，不是廣泛分佈的優勢。BUG-6 的「丟棄 top1~4 天」
檢查能抓到「單日主導」，但抓不到「單一*交易類型*主導」這個更細的變體——如果那幾筆
尾部交易分散在不同天，逐日層級的丟棄測試不會觸發，必須拆到逐筆 exit_reason 才看得到。

**如何自我檢測**：
- **依 exit_reason／出場類型拆解 PnL，不是只看總和**：至少拆成「訊號正常出場」vs
  「session-end 強制平倉」vs「停損」vs「達標」等類別，分別算各類別的筆數與淨損益；
  若正報酬集中在筆數占比很小的某一類（尤其是 session-end 強制平倉），要高度懷疑。
- **算「拿掉該類別後的淨損益」**：若拿掉 session-end 強制平倉這批交易後，剩餘交易的
  淨損益由正轉負（或由打平轉顯著負），代表 headline 數字是尾部事件撐出來的假象，
  不是可交易的 edge。
- **同時做 BUG-6 的逐日丟棄測試與本項的逐筆類型拆解**：兩者互補，任一項顯示「主導
  力量集中在極少數樣本／類型」都足以推翻「這是廣泛分佈的正報酬」的敘述。

**本 repo 案例**：
- `H-SC-SAR-CONVERTER` MODE B（`tmf-slow-pattern-cell`）：−910pt「接近打平」，拆開後
  其實是反手鏈結構虧損 −58,508pt 與少數尾部趨勢日 +46,938pt 湊巧相消，不是穩定 edge。
- `H-SC-COARSE-CATALOG-WALKFORWARD`（`tmf-slow-pattern-cell`，2026-08-08）：held-out
  +240pt／681筆表面數字，拆開 exit_reason 後：571 筆停損＋69 筆達標（共 640 筆，
  94% 交易量）合計結構性虧損 −10,490pt，全靠 41 筆 session-end 強制平倉貢獻
  +10,730pt 撐住——與 SAR-CONVERTER MODE B 同一種樣態，第一位審查者只做了逐日丟棄
  測試（發現拿掉單一最大日即翻負），第二位審查者才拆出 exit_reason 層級的真正根因，
  兩者互補才完整揭露問題。

---

## 附錄：本三條研究線裡另外重複出現的陷阱（次要但仍值得列入 checklist）

| # | 陷阱 | 自我檢測要點 | 案例 |
|---|------|------|------|
| A1 | **架構論證 ≠ 已驗證**——「這個設計理論上應該解決問題」不能替代「跑數字證明它解決了」 | 任何架構性改動都要跑對帳不變量／bit-for-bit equivalence 測試，不能只憑論證採信 | `H-TXFB-FLAT-DEFAULT-BREAKS-PATH-DEPENDENCE`：架構論證正確，但因果版 debug 出兩個同根bar邊界 bug，修完才達到 996/996 全過 |
| A2 | **單次 70/30 切分 vs 多摺滾動驗證** | 任何「找到正報酬設定」的結論，都要用至少 3~5 摺獨立滾動驗證，不能只做一次切分 | `H-TXD-WINDOW-RECALIBRATION`：window=55 一次性 OOS 驗證正報酬，3 摺滾動驗證後在 Fold3 轉虧，證明只是切分運氣 |
| A3 | **「分散」其實是重疊部位加碼**——多個訊號源各自獨立開倉，但持倉時間高度同方向重疊，本質是同一趨勢訊號的加碼倉位，不是真正分散 | 算「同方向重疊時間佔比」；佔比高時風控要用「一個大部位」的邏輯，不能用獨立分散部位的邏輯 | `H-TXD-MULTIWINDOW-PORTFOLIO`：w34+w55+w89 三個window 72.7% 持倉時間同方向重疊 |
| A4 | **跨 session rolling window 污染** | rolling window 通道／指標若跨過收盤→開盤的空檔繼續滾動，會把不連續時段的價格關係混在一起；預設應該day/night分開重新起算 | `H-TXD-SESSION-CONTINUITY-BUG`：day/night 分開後 w34+w55+w89 組合總損益從 36,680.2pt 幾乎翻倍到 79,863.9pt |
| A5 | **對單一 fold／regime 窗口 curve-fit** | 明確定義「多數 fold 必須同號」這類 kill criterion，任一 fold 正負號翻轉就要視為觸發，不能只看 OOS 總和 | `H-TXFB-OPTION-A/B-*-BRACKET-EDGE`：候選只在 Fold3/Fold5 正報酬，其餘 3 個 fold 全負，kill criterion 5 觸發 |
| A6 | **測試組間切法不一致卻宣稱同一套方法論** | 多組對照實驗要先確認 fold/切分方式完全一致，才能互相比較通過率 | `H-SC-ENTRY-FEATURE-SIZING` do_not：testA/B/C 三組實際用了三種不互相可比的摺切法，敘事卻說「套用同一套方法論」 |
| A7 | **多重比較未算基準機率** | sweep N 個參數/特徵前，先算純雜訊下預期通過數，拿實測通過數對照 | `H-SC-ENTRY-FEATURE-SIZING`：8 個特徵，5摺「至少4摺同號」雜訊基準機率 ~37.5%（預期 ~3 個純運氣通過），實測 0 個乾淨通過 |
| A8 | **樣本 n 計算錯誤／重複計算** | 日夜盤、多 sleeve 若共用同一批交易日，不能各自算成獨立樣本；先確認「獨立樣本數」而非「記錄筆數」 | `H-SC-COARSE-CATALOG-CAUSAL`：聲稱 n=162，實際日盤 83 筆與夜盤 79 筆來自同一批 ~83 個交易日，真正獨立樣本規模只有 ~83 天 |
| A9 | **字典/門檻在同批資料上校準又評分（無 held-out）** | 校準期與測試期必須分離，字典/門檻只能用校準期資料 fit | `H-SC-COARSE-CATALOG-CAUSAL`：8 類字典用同一批 83 天資料校準、再拿同一批 83 天打分，無 walk-forward |
| A10 | **敘事與底層數據矛盾** | 任何「XX 不影響 YY」這類敘述性宣稱，都要拿原始 JSON/CSV 逐一核對 | `H-SC-DISTANCE-RATIO` do_not：script 聲稱「target_frac 不影響交易次數」，與 JSON 自身數據矛盾 |
| A11 | **研究資料來源 vs 產線下單契約身分不一致** | 確認研究用的 data_id／乘數，跟 `config/order.yaml` 實際下單契約是同一個標的；乘數換算錯誤會扭曲資金結論 | `H-TXD-INSTRUMENT-IDENTITY`：研究資料=TX（200元/點），產線下單=TMF（10元/點），README 誤用 TX 乘數算出的「連1口都養不起」結論換算後基本消失 |
| A12 | **Bar 級近似的敏感度取決於引擎架構，且資料覆蓋率本身可能有缺口** | 「bar 近似不影響結果」的結論只對特定架構（收盤觸發+次根開盤成交）成立，intrabar 觸價機制要獨立用 tick 級複驗；另外要單獨確認資料本身有沒有截斷（例如夜盤只到 23:59 缺 00:00-05:00） | `H-TXD-NIGHT-TICK-COVERAGE-GAP`：bar cache 夜盤全部 83 天硬截斷在 23:59，完整夜盤 vs 截斷窗口比較下 w233 符號直接翻轉（−2,018.7pt → +663.2pt） |
| A14 | **join 失敗＝靜默丟樣本，宇宙被砍掉而不自知（且跨期覆蓋率不同 → regime 結論被混淆）** | 任何 `事件 JOIN 價格／基準` 的步驟，都要印出「join 成功率」與「每期 distinct 標的數」；覆蓋率若隨時間變動，任何跨期比較都先當作被混淆，補齊資料後再下結論 | `dayflip-futures-short`（2026-08-07）：`stock_daily_bars` 的 finmind 來源只涵蓋 676 檔、每日 174~375 檔（**不是全市場**）；訊號第一步「買進金額＝股數×收盤價」把沒有價格的股票靜默丟棄，實測 9661 在 2026-05 的 23,152 個 (股票,日) 組合只有 **28.3%** 拿得到價格。且覆蓋率 2024H2 僅 106/355 檔、2026H1 168/355 檔，一度把「2024 年 0 訊號」誤判為覆蓋率假象。補齊 355 檔期貨標的價格（+104,707 列 / +97 檔，2024H2 覆蓋 106→326）後重跑，2024H2 仍為 0 訊號日、跳空分佈本身即證實為 regime 效應，原歸因才成立 |
| A15 | **凍結規格要做宇宙敏感度測試** | 規格凍結後若底層資料宇宙變動（補資料、換來源），要在門檻完全不動的前提下重跑一次；結果一致才算規格不是靠特定宇宙撐起來的。此測試**不算重新調參，也不算 holdout** | `dayflip-futures-short`：宇宙 229→326 檔後，62 天/日均0.999%/t5.03 → 66 天/日均1.005%/t5.32，門檻未動、結論不變 |
| A13 | **G1 事後補登記 vs 事先預註記** | 假說與驗證計畫要先寫進 `config/research.yaml`／design doc 再開始跑數，不要跑完 10+ 輪才回頭補登記 | `tx-donchian-multiwindow-channel` G1 failed（先跑完才補登記）；`tx-donchian-flat-default-bracket` 用 design doc 修正此問題，G1 passed |
| A16 | **對零檢定 ≠ 對基準檢定——無條件基準沒算，就不知道自己在量什麼** | 任何「顯著異於零」的宣稱，都要先算**完全不挑股、不挑日**的同協議無條件基準，再拿事件組跟**基準**比、不是跟零比。基準本身若非零（多數 excess-return 口徑都非零），「p 值極小」只證明樣本夠大，不證明有訊號。特別注意固定 β 調整：在權值股主導的多頭裡，`stock − 1.15 × 市值加權指數` 對**中位數個股**是系統性過度扣減 | `branch_fade_veto.json`（2026-08-18 查出）：24 席「BH-FDR 顯著負向」的 fade 名單，其中 9268 凱基-台北 n=20,588、median −2.114%、**p=1.09e-57**。但同協議（L1H7・T+1開盤→T+7收盤・30bps・β=1.15）的**無條件基準**是 n=1,120,570、**median −1.829%、勝率 36.4%** —— 9268 只差基準 0.29pp，而且**勝率 37.4% 還比基準高**；9800 元大勝率 39.6% 更高。整份名單量到的是 β=1.15 的過度扣減，不是分點行為。同一個錯誤也解釋了全分點宇宙掃描為何「BH-FDR 顯著 11 個、**全部負向、無一正向**」——基準是 −1.83%，那個掃描在結構上不可能找到正向候選 |

---

## 使用 SOP（把假說定案為 passed/rejected 前的最後一關）

在把任何一輪 evidence 寫進 `config/research.yaml` 之前，逐項確認：

- [ ] **BUG-1**：有沒有 session/窗口結尾未平倉部位？做過 force-close 誠實結算對照嗎？
- [ ] **BUG-2**：因果特徵有沒有做過擾動測試（改動 `t` 的資料，確認 `t` 之前不受影響）？
- [ ] **BUG-3**：任何 regime/gate 變數是不是用了整個 fold 未來的資料？改寫成 causal
      trailing 版本重跑過了嗎？
- [ ] **BUG-4**：做過逐日/逐筆的正式顯著性檢定嗎？有沒有做自相關校正（Newey-West
      HAC）？正負向結果用了同一套門檻嗎？
- [ ] **BUG-5**：成交價是不是誠實的 next_open？有沒有在多個候選價格間偏袒交易方向？
- [ ] **BUG-6**：任何自動生成的 verdict/caveat 標籤，有沒有人工重算過判斷邏輯的邊界
      情況（尤其是「其中一邊為負」）？
- [ ] **BUG-7**：PnL 轉正有沒有依 exit_reason／出場類型拆解？拿掉 session-end 強制
      平倉／尾部交易後，剩餘交易是不是仍是結構性虧損？
- [ ] 附錄 A1–A16 裡有沒有適用本輪的項目？

全部過關才寫 `status: passed`／`rejected` 並附上完整 evidence；任何一項沒做，寧可標
`status: pending` 並在 `next_step` 寫清楚還缺什麼，不要用「反正結論方向看起來合理」
帶過。

---

## 對應工具

`scripts/research/lint_backtest_engine.py`——對一支回測腳本原始碼做**靜態關鍵字/正則
掃描**（不是 AST 語意分析），偵測 BUG-1/2/4/5/6 相關的「明顯缺席」訊號（例如完全沒有
對帳 assert、完全沒提到 HAC/Newey、`causal_*` 函式裡找不到 `shift(` 或 `-1` 偏移、
成交價賦值附近有可疑的 `min()`/`max()` 選擇等）。用法：

```bash
PYTHONPATH=src .venv/bin/python scripts/research/lint_backtest_engine.py <path/to/script.py>
```

**這個工具的偵測能力有明確上限，讀清楚再用**：

- 純字串/正則比對，**不理解程式語意**——看到 `shift(1)` 字樣就算過關，即使它出現在
  完全無關的變數上；看到 `min(`/`max(` 就標記可疑，即使它跟成交價毫無關係，會有
  false positive 也會有 false negative。
- **抓不到** BUG-3（fold-aggregate 後見之明）——這需要理解「這個統計量是不是用了未來
  資料」的語意，regex 做不到，只能靠人工 review 或擾動測試。
- **抓不到** BUG-7（尾部/強制平倉交易掩蓋結構性虧損）——這需要實際執行程式、把交易
  按 exit_reason 拆解比較，靜態掃描原始碼看不到任何字串訊號能區分「正報酬廣泛分佈」
  跟「正報酬集中在少數強制平倉交易」，只能靠人工／agent review 執行後的交易明細。
- **抓不到**同根循環 look-ahead 的實際邏輯錯誤（BUG-2）——只能檢查「`causal` 命名的
  函式附近有沒有出現位移語法」這種弱訊號，抓不到「位移量算錯」或「只有分子位移、
  分母忘了位移」這類真正的 bug。
- **抓不到**敘事與數據矛盾（附錄 A10）、樣本 n 計算錯誤（附錄 A8）、fold 切法不一致
  （附錄 A6）——這些都需要交叉比對數字或理解實驗設計，超出關鍵字掃描的能力範圍。
- 只掃單一檔案的原始碼，**不執行程式、不看實際跑出來的數字**——一支完全沒有 bug、
  甚至已經通過人工 review 的腳本，如果沒寫 HAC 相關字樣（例如因為它 import 了一個
  共用模組來做顯著性檢定，關鍵字不在這支檔案裡），會被誤判為缺少檢定；反之，寫了
  `HAC`/`newey` 字樣不代表真的算對了。

**結論**：這個 lint 只是「有沒有明顯忘記做某件事」的第一道防線，用來提醒作者「這裡
可能漏了什麼」，**通過 lint 完全不代表回測誠信沒問題**，本清單前六節的人工／agent
review（尤其是擾動測試、force-close 對照、causal trailing 重寫）才是真正的把關步驟。
