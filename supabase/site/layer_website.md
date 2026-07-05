---
page_id: layer_website
layer_id: website
title: 網站層
tab_label_zh: 網站層
tab_label_en: 網站層
sort_order: 6
role: 唯讀 presentation
web_v1: 本網站
---

# 網站層

**網站層** 展示每日日報與策略靜態頁。資料來自 Supabase `daily_briefs` + `site_content`（**runtime SSOT**）。本檔為 git authoring mirror；推送見 [README](README)。

## 消費者導覽（Readdy 頂部 nav · canonical）

| Nav | 路由 | 內容 |
|-----|------|------|
| **今日** | `/` | 表一 · Regime × 五軌 screen · **今日亮點** |
| **日報** | `/briefs` · `/briefs/{date}/…` | 市場環境 · ETF 持股 · VCP |
| **策略目錄** | `/strategies` | 五軌索引 · 績效對照 · 凍結規格 · 採納報告入口 |
| **关于** | `/about` | 專案說明 · **產品六層** 方法論附錄（非主 nav） |

**不用**：「策略中心」· 頂層「方法論」群組 · 獨立 Research 層 nav · `/pages/strategy_catalog`（redirect → `/strategies#…`）。

## 表一 · 深連規則

| 點擊 | 去向 |
|------|------|
| 環境摘要列 | 當日 Regime brief（`/briefs/{date}/regime` 或首頁 Regime 區） |
| 策略 screen 儲存格 | 當日該軌 brief tab |
| 策略名稱（若可點） | `/strategies/:strategy_id` **凍結規格** |

## 每日三問 · 日報首頁

[最新日報](/) 對齊三份收盤 brief（KPI 契約見 [日報首頁規格](daily_home)）：

| 日報 | 日報回答 |
|------|----------|
| [市場環境](/) | 今天**市場環境** |
| [ETF 持股](/) | **00981A** 等今天有哪些**持股異動** |
| [VCP 漏斗研究](/) | 值得看的 **VCP 候選**（探索漏斗 · ≠ 凍結 screen） |

歷史：[日報列表](/briefs) · 深度：[策略目錄](strategy_catalog)

## 產品六層（方法論 · 附錄）

六層說明供 **关于** 頁閱讀，解釋 Facts → Regime → Research → Strategy 分工；**不是**第二套主導航：

| 層 | 頁面 |
|----|------|
| 事實層 | [layer_facts](layer_facts) |
| 環境層 | [layer_regime](layer_regime) |
| 研究層 | [layer_research](layer_research) — 方法論 only · 案例經策略 **採納報告** |
| 策略層 | [layer_strategy](layer_strategy) · [策略目錄](strategy_catalog) |
| 下單層 | [layer_order](layer_order) — 本機 infra · 不進公開站 |
| 網站層 | 本頁 |
