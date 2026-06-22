---
page_id: daily_home
layer_id: website
title: 日報首頁規格
tab_label_zh: 日報首頁
tab_label_en: Daily home
sort_order: 7
role: Readdy / 路由 /
web_v1: 日報首頁 · KPI + 三卡
---

# 日報首頁規格 · Readdy `/`

本頁為 **消費者首屏** 與 **前端實作契約** 的 SSOT；文案對外入口仍為 [最新日報](/)。

## Hero

**標題（h1）**：`好時量化股市研究 · {trade_date}`

**副標（一句）**：`每日市場環境` — 供研究對照，非投資建議。


**`trade_date` 語意**：`daily_briefs.trade_date` = **台股最近交易日**（TEJ `IX0001` 日線），**不是**日曆今天。排程 sync **僅在台股交易日**推送；週末／假日略過，不會重推前一交易日、也不新增幽靈日報。

**盤中預警例外**：`rrg_mono_intraday` 的 `trade_date` = **資料基準日**（收盤 panel 最後完整交易日），`snapshot_json.session_date` = 產出日（job 執行日）。例：週一 6/22 13:00 產出、內容「大盤 tick：沿用昨收」→ `trade_date=2026-06-18`、`session_date=2026-06-22`。此 brief **不** 歸入「策略掃描」分區。

---

## 六格 KPI

首屏 **不 fetch** Regime `content_html`（避免載入 embed SVG）。KPI 由 `daily_briefs.content_md` 輕量 parse，或日後 `snapshot_json`。

| `data-metric` | 顯示標籤 | brief_type | 解析規則（content_md） |
|---------------|----------|------------|------------------------|
| `breadth_200ma` | 200MA 廣度 | `regime_daily` | `% above 200-day MA ([\d.]+)%` |
| `trend_posture` | Weinstein 階段 | `regime_daily` | `Weinstein Stage (\d)` 或 synopsis 趨勢句 |
| `rrg_health` | RRG 健康度 | `regime_daily` | `Leading \+ Improving: ([\d.]+)%` |
| `stage2_pass` | Stage 2 | `regime_daily` | `(?:template )?pass rate ([\d.]+)%` |
| `vcp_pass` | VCP 候選 | `vcp_funnel_specs` | variant 段數或表格列數 |
| `consensus_adds` | 共識加碼 | `etf_daily` | 見下方 · v1 可 fallback CLI 同步後 MD 區塊 |

**00981A 加碼**（選配第七格或併入 ETF 卡 teaser）：`etf_981a_adds` — 解析 `### 00981A` 區塊內 `新進|加碼` 列數。

**跨 ETF 共識**：`consensus_adds` = 同日 **≥2 檔 ETF 同步加碼** 的標的檔數。邏輯 SSOT：`holdings_research.build_cross_etf_consensus()` · 說明見 [事實層 · 跨 ETF 共識](layer_facts#跨-etf-共識)。

### 版面

| 斷點 | 配置 |
|------|------|
| ≥720px | 3×2 或 6 欄 KPI strip |
| <720px | 2×3 grid |

Synopsis 一句：Regime MD `Daily synopsis` 首段，置於 KPI 下方。

---

## 三張入口卡

| 卡 | 標題 | Teaser 來源 | 連結 |
|----|------|-------------|------|
| A | 市場環境日報 | 四軸 KPI 摘要 + synopsis 前 120 字 | `/briefs/{date}/regime` |
| B | ETF 持股日報 | `持股同步 **6/6**` · 有變化 ETF chips · **共識 ≥2** 檔數 | `/briefs/{date}/etf` |
| C | VCP選股策略 | variant 數 · Top composite | `/briefs/{date}/vcp` |

**策略目錄**、**研究案例** 不放首屏主 CTA — 置頂 nav「方法論」→ [策略目錄](strategy_catalog)。

---

## 今日亮點（`stock_daily_highlight` · 首屏 Layer 1）

> **CX SSOT** · 今日亮點對外文案 · 資料表：`stock_daily_highlight` · `daily_highlight_alert`  
> 用語對照總表：[terminology.md](../docs/terminology.md) §10.2

收盤後首屏主區塊回答：**相較昨日，哪些標的出現跨層結構變化？** 不是 buy list。

### 區塊標題

| 元素 | 對外文案 | 勿用 |
|------|----------|------|
| h2 | **今日亮點** | 今日亮點 · 收盤情報 · 今日必看 |
| 副標（選配） | 跨 ETF 持股、市場廣度、RRG、VCP 之昨日對照 | （勿用英文產品代號） |

### 統計 chip（讀當日 `stock_daily_highlight`）

| chip | 文案格式 | 欄位 |
|------|----------|------|
| 清單規模 | **清單內 N 檔** | `count(*)` |
| 新進 | **新進觀察 N** | `delta_new_to_watchlist` |
| 收斂 | **四框架收斂 N** | `highlight_tier = fire` |

勿用：**監控標的**、**共識**（此處指四框架收斂，非 ETF 共識加碼）。

### 篩選 tab

| tab | 文案 | filter |
|-----|------|--------|
| 預設 | **今日異動** | `delta_any_signal = true` |
| 全量 | **全部** | 無 |
| 次級 | **持續關注** | `highlight_tier = watch` |

勿用：**今日訊號**、英文 **Watch**、**僅 delta**（對使用者說「異動」）。

### 排序

| 選項 | 文案 | sort key |
|------|------|----------|
| 預設 | **變化優先** | `delta_any_signal` → `signal_convergence` → `highlight_score` |
| 次選 | 收斂程度 | `signal_convergence` desc |
| 次選 | 參考分 | `highlight_score` desc |

勿用：**變化優先**、裸技術欄位名（技術欄位名，非台灣讀者用語）。

### 空狀態（`delta_any_signal` 筆數 = 0）

| 元素 | 文案 |
|------|------|
| 標題 | **今日無結構變化** |
| 說明 | 相較昨日，監控清單內尚無新異動。可切換「全部」查看完整清單。 |
| CTA | **查看完整清單** |

勿用：今日無結構變化 · 無異動 · 查看全部 · 監控池。

### Alert 條 · Email（`daily_highlight_alert.headline_zh`）

由今日亮點 headline 產出器（`format_headline_zh()`）產出，範例：

- `今日亮點：3 檔四框架收斂 · 2 檔新進觀察`
- `今日亮點：7 檔新進觀察`
- `今日亮點：今日無結構變化`

句型規則：**今日亮點** + **N 檔 + 動作**；檔數與動作中間不插入「監控清單／池」；日期見 `trade_date` 欄，headline 不重複；**0 檔亦發送**（見 [修改計畫書](../docs/修改計畫書.md) §13.4）。

### 路由

| 路徑 | 內容 |
|------|------|
| `/` Layer 0–1 | Alert 條 + 今日亮點列表 |
| 今日亮點全頁 | 全列表 + 監控清單 drill-down（路由 slug：`highlights`） |

三卡（市場環境／ETF／VCP）降級為 **深度閱讀**，視覺弱於 Layer 1。

---

## 策略每日篩選（`daily_briefs` · 第二層）

除 **環境／事實／VCP 探索** 外，各 **已採納策略** 另有獨立 `brief_type`，內容為 **當日篩選結果**（候選表、訊號表、槽位狀態）。Readdy 路由建議：`/briefs/{date}/strategy/{brief_type}`。

| brief_type | 策略 | schedule | 當日顯示什麼 |
|------------|------|----------|--------------|
| `copytrade_l1h9` | [ETF00981A 跟單策略](strategy_00981a_l1h9) | 16:30 | 00981A **新進／加碼** 訊號表 · 共識≥2 標記 |
| `rrg_mono_daily` | [RRG 單軌](strategy_rrg_mono_hold7) | 16:30 | mono fresh 候選 · 依軌跡排序 |
| `vcp_pivot_gate` | [VCP 突破確認](strategy_vcp_pivot_gate) | 13:00 | near pivot 候選 Top N |
| `vcp_coil_close` | [VCP 訊號收盤](strategy_vcp_coil_close) | 13:00 | 同上池 · 訊號日收盤進場變體 |
| `vcp_funnel_specs` | VCP 合併（研究用） | 13:00 | 兩變體合併 MD |

**未納入策略掃描**（獨立分區 · 盤中預警）：

| brief_type | 策略 | schedule | 說明 |
|------------|------|----------|------|
| `rrg_mono_intraday` | [RRG 單軌](strategy_rrg_mono_hold7) | 13:00 | 盤中 tick 重算 mono 候選 · **非收盤掃描** · 16:30 `rrg_mono_daily` 為準 |

### RRG universe 全檔狀態（`rrg_universe_scores`）

每檔一列 · **不** 塞進 `daily_briefs.snapshot_json`。查詢鍵：`session_date`（產出日）+ `screen_kind`（`intraday` | `close`）。

```js
supabase.schema('stock_research')
  .from('rrg_universe_scores')
  .select('*')
  .eq('session_date', '2026-06-22')
  .eq('screen_kind', 'close')
```

| screen_kind | 產出時段 | `data_baseline_date` |
|-------------|----------|----------------------|
| `intraday` | 13:00 | 上一交易日收盤 K |
| `close` | 16:30 | 當日收盤 K |

**未納入日報**：`minervini-sepa-basket` 為 **月頻** ad-hoc 回測，無每日 screen。

### `snapshot_json` 路由契約

策略篩選 brief 帶：

```json
{
  "contract": "strategy-screen-v1",
  "strategy_id": "rrg-mono-hold7",
  "title_zh": "RRG 單軌（持7日）",
  "layer": "strategy"
}
```

Readdy 可用 `strategy_id` 連結 `site_content` 的 `strategy_*` 凍結規格頁。

**回測參考**（選股列旁提示 · 策略層凍結規格 · 非當日保證）：

```json
"backtest_reference": {
  "spec_type": "slot_strategy_backtest",
  "window": { "start": "2026-01-01", "end": "2026-12-31" },
  "n_periods": 39,
  "win_rate_vs_bench_pct": 58.97,
  "historical_win_rate_vs_bench_pct": 58.97,
  "mean_excess_pct": 7.0,
  "expected_excess_pct": 7.0,
  "mean_return_pct": null,
  "expected_return_pct": null,
  "hold_days": 7,
  "n_slots": 3,
  "source": "reports/research/rrg/rrg_mono_hold7_slot_backtest_2026.json"
}
```

前端建議：策略掃描表加欄 **歷史勝率%** · **每筆均超額%**（讀 `backtest_reference`）；個股列若無 per-stock 回測則顯示策略層 `backtest_reference` 參考值。

**盤中預警** brief 帶：

```json
{
  "contract": "intraday-watch-v1",
  "strategy_id": "rrg-mono-hold7",
  "title_zh": "RRG 盤中預警",
  "layer": "strategy",
  "session_date": "2026-06-22",
  "data_baseline_date": "2026-06-18"
}
```

### 查詢範例

**策略掃描**（收盤後）：

```typescript
const { data } = await supabase
  .from('daily_briefs')
  .select('brief_type, title, content_md, snapshot_json, synced_at')
  .eq('trade_date', date)
  .eq('snapshot_json->>contract', 'strategy-screen-v1')
```

**盤中預警**（可與基準日同頁，但 UI 分區不同）：

```typescript
const { data } = await supabase
  .from('daily_briefs')
  .select('brief_type, title, content_md, snapshot_json, synced_at')
  .eq('trade_date', date)
  .eq('snapshot_json->>contract', 'intraday-watch-v1')
```

日報總覽頁 `/briefs/{date}` 建議分區：**環境**（regime · etf）· **策略掃描**（`strategy-screen-v1`）· **盤中預警**（`intraday-watch-v1`）· **研究探索**（vcp_funnel_specs）。

---

## 資料查詢（輕量）

```typescript
const { data } = await supabase
  .from('daily_briefs')
  .select('trade_date, brief_type, title, content_md, synced_at')
  .eq('trade_date', latestDate)
```

Regime 詳頁才 fetch `content_html`（embed 片段）。

---

## 互動研究素材 · RRG timeline

**不** 將含 `<script>` 的 standalone HTML 寫入 `site_content.content_html`（DOMPurify 會 strip script）。

| 方案 | 用途 | 狀態 |
|------|------|------|
| **Storage + iframe** | RRG 軌跡時間軸（~1.8MB） | v1 最快 · 見 [RRG 研究案例](research_case_rrg_mono#互動素材-rrg-軌跡時間軸) |
| **JSON + React** | 同資料結構化渲染 | v2 正規 |

產物路徑（本機）：`reports/research/rrg/*_rrg_timeline_*.html` · 產生器 `scripts/render_rrg_universe_html.py`。

---

## 與 etfedge.xyz 對照

| 維度 | etfedge.xyz | 本站 |
|------|-------------|------|
| 主題 | 全市場主動 ETF 地圖 · AUM · 產業流 | 六檔 ETF + Regime + 已採納策略 |
| 首屏 | 共識 · 加碼榜 | 六格 KPI + 三 brief · **共識 ≥2** 檔 |
| 深度 | 個股／產業敘事 | 四軸市場環境 · 歷史回測 · VCP選股策略 |
| 邊界 | 資料展示 | 明確 **非下單 · 非 live gate** |

---

## 相關頁

- [專案首頁](project_home) · 對外敘事
- [網站層](layer_website) · App Shell 與 tab
- Readdy 完整規格：`docs/readdy-stock-intelligence-spec.txt`（repo 內 · 不 sync）
