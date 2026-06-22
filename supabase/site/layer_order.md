---
page_id: layer_order
layer_id: order
title: 下單層
tab_label_zh: 下單層
tab_label_en: Order layer
sort_order: 5
role: 下單 intent、券商
web_v1: 不進網站
---

# 下單層

**Order layer（下單層）** 處理委託意圖、券商 API 連線、帳戶與委託提交。**不含於公開網站。**

| 項目 | 說明 |
|------|------|
| **角色** | 下單 intent · 券商 |
| **網站** | 不進網站 |

## 職責

- 將 [策略層](layer_strategy) 產出的訊號／篩選結果轉為可提交的委託意圖
- 管理券商連線與帳戶狀態
- 本機提交委託（非網頁端）

## 為何不在網站

安全與合規：憑證、帳戶、即時下單不暴露於匿名前端。策略層產出訊號／篩選後，由本機下單層接手。

此 tab 僅作架構說明，無日報延伸。
