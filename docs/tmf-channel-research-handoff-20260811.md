# TMF Channel 研究交接筆記（2026-08-10 晚 ~ 2026-08-11 上午）

> 給下一個 session 接手用。這份是**這一輪 session 的完整戰果清單**，不是正式研究登記——
> 正式登記在 `config/research.yaml`，本輪成果已全數補登記（見下方 §6 的對照表）。開始前先看
> `git log --oneline -10` 確認這份筆記寫完後有沒有新 commit 蓋過這裡的狀態。
>
> **2026-08-11 修訂（第二版，改動很大）**：這份 handoff 的數字在同一晚被**兩批不同的資料
> 缺陷推翻了兩次**。
>
> - §5a 原本的「快取資料品質 bug」經全量稽核**證實為假陽性、已撤銷改寫**——快取是乾淨的。
> - 真正的元凶在 `src/tmf_channel/cache_store.py::load_day()`，而且有**兩個**缺陷（§5a-bis）：
>   **缺陷 A＝bar 順序被字典序打亂**（主宰一切、原本完全沒人發現）、缺陷 B＝session-date
>   時間戳讓 NQ/ES 閘門取到早 24 小時的值。**兩者都已修好並重跑。**
> - 重跑結果**推翻了 §4b**（原「★最高優先、本輪最強發現」，IS 已完全不顯著）、**下修了 §4a**，
>   並使 §3 表中多數 IS-based 判斷失去依據。**照實更新在各節，沒重跑的明確標「未重跑」。**
> - §2 的辨識方法改成查程式碼而非 commit 訊息，並已擴寫成本文件最重要的一段。

---

## 1. 已部署上線、不用再驗證的東西

以下都已經走 `scripts/order/tmf_cutover.sh` 部署、全套測試通過、直接查證 live 運作正常：

1. **NQ/ES 逐分鐘即時閘門**（causal_engine.py session_side_gate per-bar key + nq_gate.py
   連續錨點）——RECIPE_VERSION 現在是 `final_v1_5_0_pv16_continuous_gate`。
2. **下單層 3 項韌性修復**：broker 查詢失敗時不誤送新單、不誤刪已掛真單；查詢中斷不會
   意外重置 90 分鐘 max-hold 安全網時鐘；真倉位對不上 sim 追蹤時會用真實成交價重建保護單
   （`synthesize_lost_tracking_protect_rail`）。
3. **NQ/ES 小時K「未收完棒」修復**（`us_futures_overnight.price_at_or_before(min_age=1h)`）——
   修好之前，NQ 閘門在小時棒剛開始成型的頭幾分鐘會讀到還沒定案的部分數值，導致連續
   數小時誤判成 "none"。修好後才發現**這個 bug 讓當晚稍早所有回測數字（固定內圈、
   struct_disabled、三窗驗證）全部失真**，重跑後多數候選從「看起來顯著」變成「不顯著
   甚至翻負」。
   ⚠️ 這**只是本輪三個資料缺陷中的第一個**——後來又查出 `cache_store.load_day()` 的
   bar 順序與時間戳兩個缺陷（§5a-bis），影響更大。三者合起來才是本輪最大的方法論
   教訓，見下方 §2。
4. **委託改價用 amend（`modify_price`）取代 cancel+place**——價格漂移時直接改價，不再
   刪單重掛；只有「完全不想要了」才還是用取消。

---

## 2. 重大方法論教訓（★★ 全文最重要的一段，下一個 session 一定要先讀完再動任何數字）

**同一份 handoff 裡的數字，在一晚之內因為兩批不同的資料缺陷被推翻了兩次。**
第一次是 NQ 小時棒未收完（缺陷 0），第二次是 `cache_store.load_day()` 的 bar 順序與
時間戳（缺陷 A／B，見 §5a-bis）。每一次都讓「看起來顯著」的候選變成不顯著或翻負。

### 2.1 三個缺陷、三次翻盤

| # | 缺陷 | 具體翻盤案例 |
|---|---|---|
| 0 | NQ/ES 小時棒未收完（`price_at_or_before` 缺 `min_age`） | 「固定內圈（always_lo）+ 停用 struct_break」：修好前 OOS p=0.0003、三窗加權 **+10.14pt/日**；修好後 OOS p=0.047、三窗加權 **−11.48pt/日（轉負）** |
| **A** | **`load_day()` 的 `ORDER BY t` 是字典序，把 00:00–04:59 夜盤尾段從 session 的結尾提到開頭** | **100% 的交易日都改變**；July IS 17 日 **+1712 → −1659（正負號翻轉）** |
| B | `load_day()` 用 session date 組時間戳，NQ/ES 閘門取到早 24 小時的值 | 66.3% 的交易日改變；全 83 日 7380 → 5198 |

**缺陷 A 是主宰一切的那個，而且原本完全沒人發現。** 所有回測都跑在**時序被打亂**的 bar 上。

PAPER_RECIPE 的 before/after（單位 pt）：

| 窗口 | 缺陷 A legacy | 缺陷 A fixed | delta | 缺陷 B before | 缺陷 B after |
|---|---|---|---|---|---|
| 全 83 日 | −3605 | −3041 | +564 | 7380 | 5198（−2182） |
| **July IS 17 日** | **+1712** | **−1659** | **−3371** | 2453 | 1617 |
| OOS 66 日 | −5317 | −1382 | +3935 | 4927 | 3581 |

缺陷 B 的方向也值得記住：**NQ 閘門看起來的價值，有一部分來自那個早 24 小時的查詢。**

### 2.2 三個必須內化的教訓

1. **「修好資料只會讓真訊號更乾淨」是錯的直覺，而且已被明確否證。** §4b 原本推測
   「修好後 IC 會更顯著」——實測是**IC 變弱**（IS 三個 horizon 全部從顯著掉到不顯著，
   60m 甚至正負號翻轉）。修資料是**證偽工具**，不是**加分工具**；抱著「修完會更好看」
   的預期去修，只會在結果變差時想找理由淡化。
2. **先驗證「資料的形狀」，再驗證「策略的統計」。** bar 排序、日期歸屬、棒是否收完——
   這三件事都不會報錯、不會拋例外、不會出現在任何 p 值裡，但足以讓正負號翻轉。
   新腳本第一件事該做的是印出前後 5 根 bar 的時間戳確認是**單調遞增**的。
3. **不要盲目套用「午夜後 = 隔天」。** 實測 `tx_1m_fullnight_cache_full.json` 的尾段是
   day+1，但 `tx_1m_tick_built_fullnight_aug` 是**同一天**（詳見 §5a）。猜錯的代價是
   1901pt。現在 `cache_store.py` 是 per-source 慣例註冊表，未註冊來源直接 `KeyError`
   而不是猜——**保持這個 fail-loud 行為，不要為了讓新來源跑得動而加預設值。**

### 2.3 引用舊數字前的檢查清單

**任何要引用這份 handoff（或更早）數字之前**，先確認三個修復都在位：

```bash
grep -n "def price_at_or_before" -A3 src/us_futures_overnight.py   # 缺陷 0：簽名要有 min_age
grep -n "_chronological\|_POST_MIDNIGHT_CONVENTION" src/tmf_channel/cache_store.py  # 缺陷 A/B
git diff --stat src/us_futures_overnight.py src/tmf_channel/cache_store.py
```

三者缺一，該批數字就不可信、必須重跑。

**關於缺陷 0 的提交狀態（2026-08-11 更新，原本寫「查 commit 訊息」是無效的）**：這個修復當時**尚未提交**，
`git log --grep` 查不到任何相關 commit（已於 2026-08-11 覆核：`git log --oneline -15` 最新是
`a4e7eba`，含 `f5b7de9 feat(tmf): channel Final v1.5.0`，但沒有 forming-bar 字樣的 commit；
`git diff --stat src/us_futures_overnight.py` 顯示 +21/−2 的未提交變更）。所以只能看程式碼：
`price_at_or_before(intraday, dt_et, *, min_age=...)` 帶 `min_age` 參數＝已修復；沒有＝
工作區被還原或換了 checkout。缺陷 A／B 的修復（`cache_store.py`）當時同樣是未提交的
工作區變更。若之後有人把它們提交了，記得回來把這段改成 commit hash。

---

## 3. 研究發現 — 已拒絕的候選（不要重做，除非有新論點）

> ⚠️⚠️ **2026-08-11 補註（重要）：下表數字絕大多數「未重跑」，且其判斷依據已被 §2 的缺陷 A
> 動搖。**
>
> - 產生下表的 30 支 cell-tune／always_lo／hang 腳本、`tmf_nq_gate_momentum_confirm_test`、
>   `tmf_nq_width_calib_sweep`、`struct_break_bar_vs_tick_swing`、wyckoff retest、`gt5_r1`
>   **都已修好，但都還沒重跑**（各自耗時且依賴網路）。下表數字先原樣保留，**不要當成
>   結論引用**。
> - 為什麼特別危險：缺陷 A 讓 **July IS 17 日從 +1712 翻成 −1659**。下表有一半的「拒絕」
>   理由建立在 IS 的表現或 IS/OOS 方向比較上，**那個比較的基準已經不存在了**。
> - **「拒絕」這個結論本身多半仍安全**（重跑後只會更難通過，不會憑空變好），但**理由欄的
>   數字不可引用**。真正需要警戒的是反向：若有人想用下表某列「IS 曾經正向」當作重啟論點，
>   那個論點已經無效。

| 候選 | 結論 | 關鍵數字 |
|---|---|---|
| `night\|expand_dn` 全解封 | 拒絕 | 三窗驗證方向不一致，加權平均接近零/負 |
| 固定內圈（always_lo，取代智慧選點） | 拒絕（bug修好後） | OOS p=0.047（原本以為p=0.049即可疑），三窗轉負 |
| 固定內圈 + 停用 struct_break | 拒絕（bug修好後） | 見 §2 |
| NQ 寬度校正（`nq_calib` always_nq/div_tx） | 拒絕 | IS 看似正向，OOS 全部翻負 |
| `day\|expand_up` 重調 hang_lo/hi | 拒絕 | 被 max_lots 排擠稀釋成雜訊 |
| `day\|expand_dn` 重調 hang_lo/hi | 拒絕 | IS/OOS 方向相反 |
| `night\|div_hh_weak_vol` 拆邊測試（L-only／S-only） | 拒絕 | S-only 甚至被**證實**顯著負（OOS p=0.015）——現在的全封鎖是對的 |
| `night\|expand_dn` 拆邊測試 | 拒絕 | L-only 結構性零筆交易；S-only IS/OOS 一度顯著但三窗一負一零，仍拒絕 |
| `night\|normal` 全解封／L-only解封 | 拒絕 | 單獨這格是正的（+503/+502pt），但因為是最常見regime，解封後靠 `max_lots=1` 排擠掉其他cell機會，portfolio層級不顯著（p=0.47~0.50） |
| `day\|normal` 新增 L-side 封鎖 | 不採用 | 方向一致但不顯著（IS p=0.15/OOS p=0.48） |
| NQ 閘門「動能確認」混合設計（跟前收比 + 最近1-3小時動能同向） | 拒絕 | IS 最好僅 p=0.15，OOS 直接歸零（p=0.88） |
| 台美「5根K棒」MA偏離差（棒數對齊、時間錯位版本） | 拒絕（設計缺陷） | IC 全部不顯著（p=0.22~0.40）——**這版本本身有bug，時間尺度沒對齊**，不代表這個方向沒用，見 §4 |

---

## 4. 未完成的發現 — 留給下一個 session 繼續

> 2026-08-11 補註：本節原標題是「**最有價值的**未完成發現」。重跑後兩個候選都被下修
> （§4b 幅度很大），已拿掉這個形容——現階段最有價值的其實是 §6 待辦 1 的「補跑」。

### 4a. `night|climax_up` 多方解封（次要優先；2026-08-11 已部分重跑、數字下修）

- 目前 `block=["L","S"]`（全封鎖）。改成 `block=["S"]`（只開多方）。

**已重跑（缺陷 A＋B 全修後）——原始交易與 tick 確認**：

| 項目 | 修復前 | 修復後 |
|---|---|---|
| IS 筆數／PnL | 12 筆／1074pt | 12 筆／**1006pt** |
| OOS 筆數／PnL | 34 筆／971pt | **37 筆／696pt** |
| IS tick 確認率 | 12/12 | 12/12 |
| OOS tick 確認率 | **26/34** | **37/37** |
| tick 確認 PnL | 1029pt | **696pt** |

- **好消息**：原本 8 筆對不上的，正是 §5a 那批**幽靈交易**（2026-04-02 00:36、2026-05-07 ×2、
  2026-06-26 01:11，加上 04-07 ×2 與 06-22 ×2 的 `no_ticks_in_bar`）。修復後**全部乾淨確認**，
  tick 確認率 100%。所以「12 筆 100% tick 確認」對 IS **仍然成立**，不是 bar 級假象。
- **壞消息**：**PnL 與整個 OOS 側都縮水**（OOS tick 確認 PnL 1029 → 696pt，−32%）。

**⚠️ 未重跑（不要引用，也不要假裝跑過）**：下列**候選減基準的 delta 數字**沒有重跑——
產生它們的腳本無法辨識，step1／step2 只輸出該 cell 的原始交易，不輸出 delta：

- IS `+64.00pt/日, t=2.361, p=0.028`
- OOS `+13.00pt/日, p=0.128`
- 三窗驗證 julsep25 `−0.42`(p=0.67,7筆)／octdec25 `+2.58`(p=0.20,7筆)／janmar26 `+10.07`(p=0.11,19筆)

考慮到原始 PnL 已下修、且 OOS 筆數從 34 變成 37（樣本組成本身變了），**這些 delta 幾乎
確定會變差，只是不知道差多少**。在重跑之前，§4a 不能宣稱任何顯著性。

- **建議（不變，但理由更強）**：不上線。等真實交易累積讓樣本變厚後重驗；同時要先把上面
  那批 delta 數字補跑出來。腳本：`scripts/research/tmf_night_climax_up_lonly_tick_step1.py`／
  `tmf_night_climax_up_lonly_tick_step2.py`。

### 4b. 台美「5小時」MA偏離差 — ~~★最高優先，本輪最強發現~~ → **降級：IS 已崩，只剩 OOS 短 horizon**

> ⚠️⚠️ **2026-08-11 重跑後大改。原本寫「今晚統計上最強的原始訊號、值得優先接手」——
> 修好缺陷 A 之後 IS 三個 horizon 全部掉到不顯著，60m 甚至正負號翻轉。**
> **「修好資料後 IC 會更顯著」這個當初的推測，已被明確否證：IC 是變弱，不是變乾淨。**

訊號定義不變（棒數對齊 bug 的修正仍然有效）：

- `tw_dev(t) = (C_tx[t] - MA(C_tx, 過去5小時1分K)) / MA * 100`
- `us_dev(t) = (NQ現價 - MA(NQ, 過去5根已收完小時K)) / MA * 100`
- `spread(t) = tw_dev(t) - us_dev(t)`

**IC before/after（spread 預測未來 TX 報酬，均值回歸方向＝負相關）**：

| 窗口·horizon | 原始 IC (p) | 完全修復後 IC (p) |
|---|---|---|
| IS 15m | −0.1034 (0.000947) | **−0.0252 (0.238)** ← 不顯著 |
| IS 30m | −0.1181 (0.00315) | **−0.0073 (0.793)** ← 不顯著 |
| IS 60m | −0.1286 (0.0216) | **+0.0111 (0.789)** ← **正負號翻轉**且不顯著 |
| OOS 15m | −0.0931 (1.33e-9) | −0.0579 (**2.34e-5**) ← 存活但變弱 |
| OOS 30m | −0.1064 (5.16e-7) | −0.0552 (**0.00148**) ← 存活但變弱 |
| OOS 60m | −0.0997 (0.00046) | **−0.0368 (0.126)** ← 不顯著 |

**這個比較是可信的**：重跑腳本的 `original` 對照組**精確重現**了本文件原本發表的 p 值
（IS 0.000947／0.00315／0.0216；OOS 1.33e-9／5.16e-7／0.00046），所以 before/after 是
同一支腳本、同一組資料下的乾淨對照，不是兩批不可比的數字。

**⚠️ 反直覺但關鍵的細節（一定要讀，否則會誤判成因）**：只修時間戳的中間版本
（`ts_fixed_only`）與 `original` **逐位元完全相同**。原因是在被打亂的順序下，午夜後的 bar
落在索引 0–299，而 IC 迴圈從 `range(TW_WINDOW_MIN=300, n)` 開始——它們被當成 MA burn-in
**靜靜吃掉了**，所以 **`us_dev` 端從來沒被污染過**。真正驅動 §4b 那個 IC 的，是
**缺陷 A 改變了「哪些 bar 進入樣本」**，而不是閘門取值錯誤。

**現在的定位**：

- **IS 完全不顯著，只剩 OOS 15m／30m 存活。** 對一個**在 IS 上形成的假說**來說，這是
  **方向正好相反**的證據形態——正常應該是 IS 強、OOS 打折；現在變成 IS 沒有、OOS 有，
  這更像是 OOS 窗（66 天）本身的樣本特性，而不是一個穩健的跨期訊號。
- **不再是「最高優先」。** 若下一個 session 時間有限，這條的優先度應低於「把 §3／§4a 那批
  未重跑的腳本補跑完」——因為那批補跑會**同時**告訴我們整個 16-cell 系統修復後長什麼樣。

**如果還是要往下走，先做這件事**：把 OOS 66 天**再切成兩半**做子窗一致性檢查。若 OOS
15m／30m 的 IC 在兩個子窗方向一致且都不接近零，才值得重新考慮「獨立均值回歸策略」那條路；
若只有其中一半在撐，就可以正式判死。

- 原本的「當閘門用」結論**維持不變、且不需重測**：「取代」版四個門檻(0.2~0.5) IS/OOS 一致
  正向但不顯著（IS 最好 p=0.107，OOS p=0.33~0.35）；「交集」版更差，IS 連 p<0.20 篩選線
  都沒進。⚠️ 但注意這兩批數字**也是未重跑的**（`tmf_spread_gate_backtest.py` 與
  `tmf_spread_gate_combine_test.py` 是**雙重受害者**、已修好但未重跑）——既然原始 IC 都
  弱化了，這兩個本來就不顯著的整合方式只會更差，所以結論方向安全，但數字不可引用。
- 腳本：`scripts/research/tmf_tw_us_ma5h_deviation_spread_ic.py`（IC 驗證，已修對＋已重跑）、
  `tmf_spread_gate_backtest.py`（取代版，已修未跑）、`tmf_spread_gate_combine_test.py`
  （交集版，已修未跑）。
- 舊的、棒數不對齊的版本 `tmf_tw_us_ma5_deviation_spread_ic.py` **不要參考**，是設計錯誤
  的版本，已被上面修正版取代。

---

## 5. 資料缺陷與結構性問題（5a／5a-bis 已處理完畢，5b 仍是活的結構限制）

### 5a. ~~`tx_1m_fullnight_cache_full.json` 夜盤尾段資料品質 bug~~ → **假陽性，已撤銷**（2026-08-11 全量稽核）

> ⚠️ 這節原本宣稱快取本身有「近千點價差」的資料錯誤。**該結論是錯的，已被全量稽核推翻。**
> 保留這段是為了讓下一個 session 不要再踩同一個坑：**看到夜盤尾段的巨大價差，先懷疑
> 「日期歸屬」，不要先懷疑「資料錯」。**

**原本以為是什麼**：逐tick驗證 `night|climax_up` 候選時，發現 bar 快取的 00:00–04:59 夜盤
尾段在 2026-04-02 00:36／2026-06-26 01:11／2026-05-07 00:00 跟真實 tick 差了 85–1062 點，
遠超過已知的 bar/tick 正常噪訊（平均約1.3點、最大約38點），因此判定為快取資料錯誤。

**實際上是什麼**：**快取本身乾淨，缺陷在讀取層（`src/tmf_channel/cache_store.py::load_day()`）。**
注意用詞：後來查清楚**兩個真正的缺陷（bar 排序、session-date 時間戳）都在 `load_day()` 裡，
都不在快取檔案裡**——下面「bit-exact」那段關於快取本身的結論不變，完整成因見 §5a-bis。

- 全量稽核：83 sessions／94,226 根 bar；其中 00:00–04:59 夜盤尾段的 **24,532 根 bar 對 tick
  逐根 bit-exact，最大偏離 0.0 點**。**受污染日期清單是空的，不需排除任何日期。**
- bar 自帶 `cal` 欄位（該分鐘的真實日曆日），但
  `scripts/research/tmf_night_climax_up_lonly_tick_step1.py::load_arrays()` 用
  `T = f"{day}T{r['t']}:00.000+08:00"`，把午夜後的 bar 一律蓋上 session date，時間戳因此
  **早 24 小時**。拿真實 4/02 的 tick 去對一根其實屬於 4/03 的 bar，自然差近千點。
- 三個原記錄案例的正確對照：

  | 案例 | session date 歸屬（錯） | cal date 歸屬（對） |
  |---|---|---|
  | 2026-04-02 00:36 | dev **1062pt** | dev **0.0** |
  | 2026-06-26 01:11 | dev **499pt** | dev **0.0** |
  | 2026-05-07 00:00 | dev **85pt** | dev **0.0** |

  原文提到的「471 筆 tick、33765–33917」精確重現為 **2026-04-02 00:30–00:45** 這個窗
  （n=471、min=33765）——是分鐘窗對錯了，不是價格錯了。
- 反向對照（刻意用 session date 歸屬）：午夜後 **97%** 的 bar 被標記異常、平均偏離 805pt、
  最大 5523pt，另有 5,466 分鐘映射到週末／假日而查無 tick——**系統性而非零星，正是
  「日期標籤錯」的指紋**（真實資料損壞會是稀疏、隨機的）。
- 稽核腳本：`scripts/research/audit_tx_1m_fullnight_cache_quality.py`（37 秒跑完全量，含
  `--attribution` 開關可重現兩種歸屬）；產物
  `reports/research/channel_lab/audit_tx_1m_fullnight_cache_quality.json` 與
  `..._session_attribution.json`。

**誠實的不確定性（不要當成「快取已保證正確」）**：這是**自洽性檢查**——快取就是從同一批
FinMind tick 建的，dev=0 有一部分是建構上必然。已用三個獨立於 tick 的結構檢查補強：
`cal` offset **83/83 全是 +1 天**、23:59→00:00 接縫最大 13pt、tail 分鐘齊全；並人工檢視過
最大異常夜 **2026-06-05**（確認是真實崩跌，不是資料錯亂）。但**無法排除 FinMind 來源本身
的錯誤**。

**現在該怎麼做**：繼續用這份快取。讀取層已修好，**一律走 `cache_store.load_day()`，不要
自己開檔／自己組時間戳**。

**⚠️ 一個差點造成新災難的細節：不同來源的午夜後慣例是相反的。** 修復時沒有盲目套用
「午夜後 = +1 天」，而是逐來源實測：

| 來源 | 00:00–04:59 的慣例 | 套錯的代價 |
|---|---|---|
| `tx_1m_fullnight_cache_full.json`／`tx_1m_fullnight_cache.json` | **day+1**（83/83、43/43 sessions 驗證） | — |
| `tx_1m_tick_built_fullnight_aug` | **同一天**（同日對齊 dev **0.0pt**） | 套 day+1 則差 **1901pt** |

所以 `cache_store.py` 現在是 **per-source 慣例註冊表**（`_POST_MIDNIGHT_CONVENTION`），
**未註冊來源直接 `KeyError`，不猜**。盲目套 +1 會毀掉 8 月那份來源。
**請保持這個 fail-loud 行為**——加新快取來源時去註冊表登記，不要加預設值。

**附帶提醒（不是污染，但影響隔日跳空類特徵）**：4 個結算日的日盤 bar 已切到次月合約，
跨日價差含轉倉價差——2026-04-15 **+268pt**、05-20 **+172pt**、06-17 **+83pt**、
07-15 **+328pt**。

### 5a-bis. ⭐ 真正的元凶：`cache_store.load_day()` 的**兩個**缺陷（已修復、已部分重跑）

**缺陷 A — bar 順序（主宰一切，原本完全沒人發現）**

`bars.sqlite` 沒有 `cal` 欄位，`load_day` 只回傳 `t,o,h,l,c,v,sess`，所以 **`cal` 根本從未
送達任何腳本**；更嚴重的是 `ORDER BY t` 是**字典序**——`"00:03" < "08:46"`——於是
00:00–04:59 的夜盤尾段被從 session 的**結尾**提到了**開頭**。
**等於所有回測都跑在時序被打亂的 bar 上。** 100% 的交易日都受影響，量級見 §2.1
（July IS 17 日 +1712 → −1659，正負號翻轉）。

**缺陷 B — session-date 時間戳（原本以為的那個）**

`continuous_gate_for_day()` 等處把合成時間戳丟進 `fromisoformat()` 去查 NQ/ES 快照，
於是回測 replay 在 00:00–04:59 這 26% 的 bar 上，**NQ/ES 閘門取到早 24 小時的數值**。
66.3% 的交易日受影響。方向上是**取到過時資料**，**不構成 look-ahead 洩漏**。
副產品結論：**NQ 閘門看起來的價值，有一部分來自那個早 24 小時的查詢。**

**生產線始終不受影響**：`src/tmf_channel/causal_engine.py:481` 用的是券商 candle 的真實
`Datetime`，只有回測 replay 用合成時間戳；`causal_engine` 也從不把 `T` parse 成時刻。

**修復範圍（供追溯）**

- 123 個檔案分流，**36 個真受害者全部修好**：A1 30 個走 `continuous_gate_for_day`、
  A2 4 個 inline NQ/ES 查詢、A3 2 個 raw-tick 軸，加上 §4b 三支（其中
  `tmf_spread_gate_backtest.py`／`tmf_spread_gate_combine_test.py` 是**雙重受害**）。
- 76 個判定無害並**記錄理由**（多數是 label-only）。
- 1 個 `tmf_order_layer_aware_replay.py` **只因預設 `--source` 沒有午夜後 bar 而安全**——
  安全性依賴 runtime flag 而非程式碼，**已標記、未改**。這是目前唯一的已知殘留風險：
  有人換個 `--source` 就會重新引爆。
- `ruff check src tests` 乾淨；**143 支 TMF 測試通過**，含 6 支新測試釘住 per-source 慣例
  與 fail-loud 行為。

**狀態：已修復、已重跑一部分。** 已重跑的是 §4b 的 IC 與 §4a 的原始交易／tick 確認。
**未重跑**的清單見 §3 補註與 §4a 的「未重跑」段落——那些數字仍然不可引用。

### 5b. `night|normal` 排擠效應（結構性瓶頸，非bug，但要記住）

`max_lots=1` 讓「常見regime」解封後會佔用大部分時間的唯一倉位額度，排擠掉「罕見但個別
更好」的cell機會。這是為什麼 `night|normal`（最常見）解封後單獨看是賺錢的、但整體
portfolio效果被稀釋掉；反過來 `night|climax_up`（罕見）解封受排擠的影響小很多。
**以後篩新候選時，出現頻率越高的regime，門檻要設得越高才划算**——高頻regime的邊際
效益天生會被 max_lots=1 打折扣。

---

## 6. 待辦 — 登記狀態（2026-08-11 更新：§3／§4 已補登記）

原本這節寫「§3、§4 的所有假說都還沒登記」。**現已補登記完成**，`config/research.yaml`
新增三個主題：

| topic id | 對應本文 | status／phase |
|---|---|---|
| `tmf-tw-us-ma5h-deviation-spread` | §4b（**已從★最高優先降級**） | 登記時為 `active`／`oos_holdout`；**重跑後 IS 已全部不顯著**，phase 須下修 |
| `tmf-night-climax-up-long-unblock` | §4a | `paused`／`oos_holdout`（樣本天生薄；重跑後 PnL 再下修） |
| `tmf-cell-unblock-and-nq-gate-variants` | §3 的 12 個被拒絕候選 | 見 yaml（表中數字未重跑） |

加上更早登記的 `tx-channel-amp-persistence`／`tx-channel-amp-uncertainty-gate`（振幅研究），
本輪成果都已進 SSOT。**下一個 session 請以 `config/research.yaml` 為準**，這份 handoff 只是
敘事版；兩者衝突時信 yaml。（`config/research.yaml` 由另一個 agent 同步更新缺陷 A／B 的
重跑結果，本文件只動 handoff 本身。）

**剩下的待辦（依優先序）**：

1. **★ 補跑那批「已修好但未重跑」的腳本**——這是現在最高價值的一件事，因為它會同時
   告訴我們整個 16-cell 系統修復後長什麼樣。清單：30 支 cell-tune／always_lo／hang、
   `struct_break_bar_vs_tick_swing`、wyckoff retest、`gt5_r1`、
   `tmf_nq_gate_momentum_confirm_test`、`tmf_nq_width_calib_sweep`、
   `tmf_spread_gate_backtest.py`、`tmf_spread_gate_combine_test.py`。都耗時且依賴網路，
   建議排隊分批跑。
2. **補跑 §4a 的 delta 數字**（候選減基準），目前完全缺；沒有它 §4a 不能宣稱任何顯著性。
3. **處理 `tmf_order_layer_aware_replay.py`** —— 它現在的安全性依賴預設 `--source`
   而非程式碼，應該改成走 `cache_store.load_day()` 或加 fail-loud 檢查。
4. §4b 若還要走，先做 OOS 66 天的**子窗一致性檢查**（見 §4b），別直接投入「獨立均值回歸
   策略」的開發——那是新開一條策略線的規模，證據強度目前撐不起來。
5. 重跑結果出來後，回頭更新 §3／§4a／§4b 與對應 topic 的 `phase`；若某個主題被證偽，
   照 `config/research.yaml` 的慣例標成 `rejected` 並寫下否證理由，**不要靜靜刪掉**。
