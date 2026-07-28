# 跨期動能（3-12個月）策略草案 · 2026-07-28

> **狀態：research，尚未寫入 `config/strategy.yaml`**。本文件是 G6 採納報告的草稿，
> 供人工審閱與決定是否推進 G5（凍結規格）；不代表已核准上線。
>
> 8-section 採納報告的正式模板（`docs/readdy-regime-strategy-lineage.md §1.2`）已隨公開站退役
> 移到 `archives/RETIRED_readdy-regime-strategy-lineage.md`，本文件改仿 [`00981a-copytrade-research-methodology.md`](00981a-copytrade-research-methodology.md) 的結構撰寫。

## 0. 研究問題演進

1. 2026-07-27：稽核「大跌溫度計」（分點賣超日線訊號）發現 look-ahead bug，修正後樣本外判別力
   31-35%（接近或低於亂猜），結論為「無站得住的預測力」。
2. 2026-07-27：多 agent 研究 5 個新時間尺度候選訊號（3-12月動能、1-4週反轉、盤中30-60分鐘動能、
   波動度regime、10-20日CMF資金流），過程中發現存活者偏誤與除權息調整不完整兩個專案級資料缺陷。
3. 2026-07-28：針對第一輪最有希望的 CMF 資金流做深度驗證——全歷史 regime 分解後發現訊號方向
   不穩定（2015-2023顯著為負，僅2024-2026局部轉正），**不建議繼續投入**。
4. 2026-07-28：修復兩個資料缺陷（存活者偏誤：本機股票宇宙 1331→2504 檔，含106檔已下市股票；
   除權息調整：`stock_close_adjusted.adj_close_v2` 覆蓋率 86.2%，動能策略實際選股宇宙內 97.5%）。
5. 2026-07-28：用修正後資料重新驗證 3-12個月跨期動能——**全部8組 F×H 參數依然強顯著**
   （Newey-West t 2.29-4.97），且**按年拆解無 CMF 那種方向反轉問題**（12年中11年為正，僅2022
   為負，符合文獻已知的「momentum crash」現象，非資料異常）。本文件即為此訊號的策略化草案。

## 1. 訊號與回測規則（凍結協議）

**核心公式**：Jegadeesh-Titman skip-month cross-sectional momentum。

- Skip 期間 S = 21 個交易日（避開短期反轉污染，見已知結論③：1分K層級存在 Roll(1984)
  bid-ask bounce 短期反轉效應）
- 動能分數：`mom(t) = price(t-S) / price(t-S-F) - 1`，F ∈ {63, 126, 189, 252} 交易日
- 持有：`fwd(t,H) = price(t+H) / price(t) - 1`，H ∈ {21, 63} 交易日
- 每月第一個交易日 rebalance，對宇宙做五分位排序，價差 = mean(Q5) - mean(Q1)（原始回測為
  long-short 五分位；**正式策略草案改為 long-only top-N，見第5節**）

**宇宙**：point-in-time，每年初用「前12個月成交金額」排序取前90檔（排除 `00` 開頭 ETF），
年度重選——用修正後的擴充資料庫（含已下市股票）計算，2015-2026 共出現 279 檔不同股票、
其中 14 檔已在某年度進入前90大後續下市。

**價格**：`stock_close_adjusted.adj_close_v2`（缺值時 fallback 用未調整 `close`，動能所用
子宇宙內 97.5% 有調整值）。

**回測窗**：2015-01-01 ~ 2026-07-27（全歷史，非僅近兩年）。

**成本假設**：來回 0.585%（證交稅0.3% + 手續費×2，與專案其他回測一致的假設）。

## 2. 統計檢定結果

### 2.1 全歷史 8 組參數（Newey-West HAC，lag=H/21-1）

| F | H | n(月) | 平均價差 | NW t值 | 扣成本淨價差 |
|--:|--:|--:|--:|--:|--:|
| 63 | 21 | 138 | 1.94% | 3.72 | 1.36% |
| **126** | **21** | 138 | 2.45% | 4.58 | 1.86% |
| 189 | 21 | 138 | 2.49% | **4.97** | 1.90% |
| 252 | 21 | 138 | 1.63% | 3.18 | 1.05% |
| 63 | 63 | 136 | 5.60% | 3.66 | 5.01% |
| **126** | **63** | 136 | 7.26% | 4.22 | 6.67% |
| 189 | 63 | 136 | 6.29% | 3.83 | 5.71% |
| 252 | 63 | 136 | 4.07% | 2.29 | 3.49% |

### 2.2 Regime 分解（F=126，關鍵防呆檢查——CMF 就是死在這一步）

| 年 | H=21 均價差 | H=21 正月% | H=63 均價差 | H=63 正月% |
|--:|--:|--:|--:|--:|
| 2015 | +2.17% | 75% | +4.66% | 67% |
| 2016 | +0.15% | 58% | +0.97% | 58% |
| 2017 | +2.55% | 67% | +7.57% | 83% |
| 2018 [BEAR] | +2.39% | 75% | +5.52% | 67% |
| 2019 | +1.90% | 75% | +0.87% | 67% |
| 2020 [COVID] | +3.05% | 67% | +11.80% | 75% |
| 2021 | +4.58% | 75% | +19.05% | 83% |
| 2022 [BEAR] | **-0.50%** | 50% | **-2.30%** | 42% |
| 2023 | +3.21% | 83% | +9.89% | 83% |
| 2024 | +2.58% | 75% | +5.57% | 67% |
| 2025 | +2.96% | 67% | +11.74% | 67% |
| 2026(6-4月) | +6.27% | 50% | +20.63% | 100% |

**12 年中 11 年為正，唯一負值是 2022**（台股電子權值股升息+庫存去化雙重打擊的空頭年）——
文獻上「momentum crash」是動能策略的已知風險特徵（急速反轉時動能策略系統性受傷，非本策略
獨有的資料異常），不是隨機雜訊，此結果**跟 CMF 的「12年裡8年反向、近期才轉正」完全不同**。

**IS(2015-2023) vs OOS(2024-2026)**：H=21 IS t=4.13／OOS t=2.27（方向一致，OOS均值3.47%
還略高於IS的2.17%）；H=63 IS t=5.12／OOS t=3.24（同樣方向一致、OOS更強）。**沒有 CMF 那種
「長期顯著為負、近期才反轉為正」的警訊。**

## 3. Long-only 回測 + Alpha/市場中性檢查（2026-07-28 補做，已完成）

Long-only（僅 Q5/top-N 贏家腳，不做空）、F=126、point-in-time 宇宙（含存活者偏誤修正），
分別對「同期宇宙均值」與「IX0001 大盤」算超額報酬：

| top_n | H | 絕對報酬(扣成本) | vs宇宙均值 excess(NW t) | vs大盤 excess(NW t) | beta(對大盤) |
|--:|--:|--:|--:|--:|--:|
| 18 | 21 | 2.33% | +1.13%(3.46) | +1.52%(3.43) | 1.26 |
| 10 | 21 | 2.93% | +1.73%(3.61) | +2.11%(3.76) | 1.30 |
| 8 | 21 | 2.92% | +1.72%(2.91) | +2.10%(3.23) | 1.29 |
| 5 | 21 | 3.12% | +1.92%(2.40) | +2.31%(2.70) | 1.38 |
| 18 | 63 | 8.64% | +3.53%(3.35) | +5.21%(3.76) | 1.47 |
| 10 | 63 | 10.25% | +5.14%(2.78) | +6.82%(3.29) | 1.52 |
| 8 | 63 | 11.41% | +6.30%(2.76) | +7.97%(3.20) | 1.58 |
| 5 | 63 | 12.86% | +7.76%(2.59) | +9.43%(2.92) | 1.65 |

**Long-only 版本訊號成立**：「相對同期宇宙均值」的超額報酬（同一批股票池內比較，天生排除
掉大部分共同市場曝險）全部顯著（t=2.4-3.6），確認動能挑股本身有真實增量，不只是搭上大盤
順風車。

**但發現一個原本沒預期到的風險：beta 對大盤持續偏高（1.26-1.65），且檔數越少 beta 越高**。
「相對大盤」的超額報酬數字（未做 beta 調整）因此會高估真正的選股 alpha——一部分只是「beta>1
在這段以多頭為主的樣本期間自動放大報酬」。這也解釋了第2.2節唯一的負值年份（2022空頭）：
不只是動能訊號本身在急速反轉年失靈，高 beta 曝險會同時放大虧損，兩個效應疊加。**上線前必須
設計對應風控（例如 regime filter 或依大盤 regime 動態減碼），不能假設這個策略是市場中性的。**

## 4. Graduation Gates 現況（`config/research.yaml` G1-G6）

| Gate | 狀態 | 說明 |
|---|---|---|
| G1 preregistered_hypothesis | 部分通過 | 公式與 F/H sweep grid 在首次回測前已由文獻（Jegadeesh-Titman）明確定義，但未正式登錄到 `config/research.yaml` topics.* 結構 |
| G2 oos_holdout | 部分通過 | IS/OOS 分割已做且方向一致、OOS 更強，但尚未依慣例寫成 report_dir 底下的獨立 JSON/MD artifact |
| G3 regime_stratification | **通過** | 見第2.2節，12年中11年為正，唯一負值有文獻解釋（momentum crash），非隨機失敗 |
| G4 rejection_registry | 部分通過 | 同批研究已文件化拒絕4個相近時間尺度候選（見 `reports/research/multiscale-signals/`），但動能本身的「相近變體」（如不同F/H組合）全部通過，沒有需要記錄拒絕理由的 near-variant |
| G5 frozen_spec | **未做** | 本文件即草案，尚未寫入 `config/strategy.yaml`，待人工核准 |
| G6 adoption_report | 進行中 | 本文件 |

**尚缺**：正式登錄 research.yaml topic（G1完整化）、獨立 OOS artifact 檔（G2完整化）。
Market-neutral 檢查已完成（第3節），結果是「有增量但 beta 偏高」，非乾淨通過。

## 5. 正式規格草案（long-only，已用真實帳戶資金校正，尚未凍結）

**原始回測是 long-short 五分位（每腳約18檔），但這個專案的下單層完全沒有放空功能**
（`src/order/fubon_orders.py` 的 `_map_bs_action` 只認 buy/sell，sell 是出場不是開空倉）
**——正式規格改成 long-only**，只做 top-N 贏家腳（第3節已完成回測）。

**資金規模**（2026-07-28 用 `.venv-fubon/bin/python scripts/order/fubon_login_test.py --snapshot`
查詢真實帳戶）：可用現金 **NT$1,155,164**，帳上已有持倉（2327/2492/3189/3653/6505/8046，
可能與現有策略相關）。這比 repo 裡個別策略單筆預算（1-2萬台幣）暗示的量級大得多——但這是
**全帳戶共用**的可用資金，跟 C18acc/Leading Dip/松山跟單/EP-gate 一起競爭，不是這個新策略
獨享的額度。**要撥多少給這個新策略，是需要人工決定的資金分配問題，本文件只給出建議區間，
不代自行拍板。**

建議區間（供討論）：top_n=8-10（第3節裡 NW t 最高、beta 相對可控的組合），每檔預算
2-5萬台幣（略高於 C18acc/Leading Dip 的2萬慣例，但仍保守），對應總曝險約 20-50 萬台幣，
留下多數可用現金給現有策略與緩衝。

草案參數（待決定）：

```yaml
# 草案 — 尚未寫入 config/strategy.yaml，僅供討論
momentum-l1:
  title: 跨期動能 · Long-only Top-N（草案）
  kind: research_adopted   # 待 G5 通過後才能改
  enabled: false            # 草案階段強制 false
  schedule: monthly_first_trading_day
  hold_days: 21             # 或 63（見第2.1節：H=21換手快但單期報酬小，H=63反之）
  formation_days: 126       # 或 189（NW t 最高組合，見第2.1節）
  skip_days: 21
  universe:
    method: trailing_12m_dollar_volume_top90
    exclude_prefix: "00"
    rebalance: annual
  portfolio:
    top_n: 8-10               # 建議區間，見上方；待人工核准實際資金分配
    budget_twd_per_slot: 20000-50000  # 建議區間；待人工核准
    long_only: true            # 無放空基礎設施，強制
  risk:
    regime_filter: TODO        # 第6節第4點：2022型momentum crash需要的風控，本草案尚未設計
  backtest:
    source_summary: reports/research/multiscale-signals/multiscale_signal_blindspot_research_20260728.md
    metrics:
      nw_t_range_long_short: [2.29, 4.97]
      nw_t_range_long_only_vs_universe: [2.40, 3.76]
      beta_vs_ix0001: [1.26, 1.65]
      regime_years_positive: "11/12"
```

## 6. 限制與實務風險（務必讀）

1. ~~無放空基礎設施~~ **已補測（第3節）**：long-only top-N vs 宇宙均值/大盤的獨立回測已完成，
   訊號在扣掉共同市場曝險後依然顯著，但發現 beta 偏高的新風險（見下一點）。
2. **Beta 偏高，非市場中性**（第3節新發現）：long-only 版本對大盤 beta 落在 1.26-1.65，
   檔數越少 beta 越高。「vs大盤」的超額報酬數字部分是 beta 放大效果，不是純選股 alpha；
   2022 年的負報酬（第2.2節）可能同時是動能失靈+高beta放大虧損兩個效應疊加。**上線前需要
   設計風控規則處理這個風險**（例如大盤 regime 轉空時強制減碼或退出），本草案尚未包含具體規則。
3. **帳戶規模已確認（第5節）**，但「這個新策略該分到多少現有可用資金」仍是待人工決定的
   問題，不是資料能回答的——已有持倉可能牽涉現有策略，需要人工盤點才能算出真正閒置額度。
4. **除權息調整殘缺 13.8%**：`stock_close_adjusted` 覆蓋率 86.2%（動能子宇宙內97.5%已經
   不錯，但非100%），剩餘缺口用未調整 `close` fallback，除權息密集的個股/年份可能有殘餘失真。
5. **2022 型 momentum crash 風險是真實、非資料異常**：策略在急速反轉年份會系統性受傷，
   結合第2點的高beta，須設計對應的風控規則，本草案尚未包含具體規則設計。
6. **與現有 live 策略的資金/時間衝突未檢查**：C18acc/Leading Dip/松山跟單都在搶同一個
   帳戶可用現金，這個新策略如果要上線，需要明確的資金分配決策，不能默認疊加。
7. **月頻 rebalance 的執行細節未定義**：用哪個時間點下單（開盤價？如回測假設）、下單方式
   （比照 leading dip 的 chase_ask？）、月底/月初遇到假日如何處理，都還沒有具體規格。
8. **beta 計算方式較粗略**：第3節的 beta 是用「期間報酬」（月/季）算的簡單迴歸係數，
   不是逐日報酬迴歸，樣本點少（H=21時138點、H=63時136點），精確度有限，僅供方向性參考。

## 7. 建議下一步（依優先順序）

1. ~~補做 long-only vs 大盤基準的獨立回測~~ **已完成（第3節，2026-07-28）**。
2. **設計風控規則處理高beta/momentum crash風險**（第6節第2、5點）——目前最大的未解問題，
   建議方向：大盤 regime 轉空（比照 Detach Gate 或 breadth zone 概念）時強制減碼/停止新增倉位。
3. 人工盤點帳戶真正閒置資金（已知可用現金 115.5萬，但需扣掉其他策略的潛在動用額度），
   決定最終 top_n 與單筆預算。
4. 決定 F/H 最終參數（126/21 較高頻但NW-t略低於189/21；126/63較低頻、換手成本較低但持倉期長）。
5. 正式登錄 `config/research.yaml` 補齊 G1/G2/G4，完成 G3（已通過）。
6. G5：以上都確認後，才寫入 `config/strategy.yaml`（需要使用者明確核准，這是會影響真實
   下單行為的變更）。

## 附錄：程式碼與資料

- 資料修正腳本：`scripts/research/backfill_delisted_stock_universe.py`（已執行，已 commit 待確認）
- 動能重新驗證腳本：`/private/tmp/claude-501/.../scratchpad/momentum_revalidation_fixed.py`（唯讀，未寫入 repo）
- Regime 分解腳本：`/private/tmp/claude-501/.../scratchpad/momentum_regime_check.py`（唯讀，未寫入 repo）
- Long-only 回測腳本：`/private/tmp/claude-501/.../scratchpad/momentum_longonly_backtest.py`（唯讀，未寫入 repo；注意：IX0001 不在 `stock_daily_bars`，需用 `src/market_benchmark.py` 的 `load_benchmark_close`）
- 帳戶餘額查詢：`.venv-fubon/bin/python scripts/order/fubon_login_test.py --snapshot`（2026-07-28 查得可用現金 NT$1,155,164，唯讀查詢，未下單）
- 完整多尺度研究報告：`reports/research/multiscale-signals/multiscale_signal_blindspot_research_20260728.md`
