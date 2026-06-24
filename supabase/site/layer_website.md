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

**網站層** 展示每日日報與策略／研究靜態頁。資料來自 Supabase `daily_briefs` + `site_content`；文案 SSOT 為本目錄 `*.md`。

## 消費者導覽（Readdy 頂部 nav）

| Nav | 路由 | 內容 |
|-----|------|------|
| **今日** | `/` | 六格 KPI + 三 brief 入口 · [規格](daily_home) |
| **日報** | `/briefs` · `/briefs/{date}/…` | 市場環境 · ETF 持股 · VCP |
| **策略目錄** | `/strategies` · `strategy_catalog` · `layer_*` | 已採納規格 · 六層說明 · 研究案例 |

**不要** 用整頁 iframe 載入含 `<html>` 的 standalone 報告；Regime 詳頁用 `content_html` embed 片段即可。

## 每日三問 · 日報首頁

[最新日報](/) 對齊三份收盤 brief（詳細 KPI 與卡片契約見 [日報首頁規格](daily_home)）：

| 日報 | 日報回答 |
|------|----------|
| [市場環境](/) | 今天**市場環境** |
| [ETF 持股](/) | **00981A** 等今天有哪些**持股異動** · **跨 ETF 共識** |
| [VCP 漏斗研究](/) | 值得看的 **VCP 候選** |

歷史：[日報列表](/briefs) · 深度：[策略目錄](strategy_catalog)

## App Shell 要素

| 元件 | 說明 |
|------|------|
| 日期選擇 | 連至 `/briefs` 或 date picker · 全站 `trade_date` 一致 |
| `LayerBadge` | Facts / Regime / Research |
| `KpiTile` | 首屏六格 · Regime embed 四項摘要 |
| `DataTable` | ETF 異動 · VCP 候選 |

設計 token 與路由見 `docs/readdy-stock-intelligence-spec.txt`。

## 靜態頁

| 類型 | 入口 |
|------|------|
| 專案首頁 | [專案](project_home) |
| 日報首屏規格 | [日報首頁](daily_home) |
| 策略 | [策略目錄](strategy_catalog) |
| 研究 | [研究層](layer_research) |
