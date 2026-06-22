---
page_id: research_case_rrg_mono
layer_id: research
research_topic: rrg-mono-breadth-study
graduated_strategy_id: rrg-mono-hold7
title: 示範 · RRG 單軌研究
tab_label_zh: 案例 · RRG
tab_label_en: 案例 · RRG
sort_order: 32
role: parameter grid · breadth stratification
web_v1: 研究示範
---

# 示範案例 · RRG 單軌（持7日）研究

← [研究層](layer_research) · 採納 [RRG 單軌](strategy_rrg_mono_hold7)

**研究主題** · RRG 單軌廣度分層 · 參數格掃描

---

## 1 · 研究問題

在 ETF 成分宇宙內，**相對強度輪動（RRG）** 單軌 **fresh** 四日軌跡能否預測 **+7 日** 相對 **台指** 超額？**持有期 / 槽位 / 排序 / 廣度分區** 如何設？

---

## 2 · 參數掃描（探索 → 採納）

| 維度 | 探索空間 | 採納 | 拒絕/保留 |
|------|----------|------|-----------|
| 軌跡 | 一階 / 二階 / 非單軌 | **fresh + 二階** | 非單軌 |
| 回看 | 3 / 4 / 5 日 | **4** | — |
| 持有 | 5 / 7 / 10 / 20 | **7** | 持20日槽占用長 |
| 槽位 | 1 / 3 / 5 | **3** | 與 VCP 5 槽錯開 |
| 排序 | 依軌跡末段 / 其他 | **依軌跡末段** | — |
| 進場 | 收盤 / 隔日開盤 | **第 4 日收盤** | — |

---

## 3 · 全樣本績效（2026 上半年 · 作 VCP 對照基準）

本研究結果同時作 **VCP選股策略掃描** 的 **RRG 對照基準**：

| 指標 | 值 |
|------|-----|
| 成交筆數 | 39 |
| 均超額% | +6.997% |
| 總超額% | +272.89% |
| 勝率% | 58.97% |

→ VCP 掃描門檻：候選需均超額 ≥ ~7% 且總超額 ≥ ~273%（或部分對照分）。

---

## 廣度分區分層

<!-- AUTO:rrg-breadth:start -->
策略：**單軌濾網 + fresh 訊號 + 依軌跡排序 + 3 槽 + 持有 7 日**（第 4 日收盤進場 / 第 11 日收盤出場）

## 區間獨立回測（僅該區間日可開新倉）

| 排名 | 200MA 區間 | 成交筆數 | 勝率 vs 基準 | 均超額 |
|------|-----------|---------|-------------|--------|
| 1 | **強勢** | 13 | 61.54% | 5.7975% |
| 2 | **過熱** | 31 | 41.94% | 2.6625% |
| 3–5 | 超賣/偏弱/中性 | 0 | — | — |

## 全樣本 · 依進場日分桶

| 200MA 區間 | 樣本 | 勝率 vs 基準 | 均超額 |
|-----------|---|-------------|--------|
| 強勢 | 11 | 63.64% | 7.6709% |
| 過熱 | 28 | 57.14% | 6.7327% |

全樣本：39 筆 · 均超額 6.9973% · 勝率 58.97%
<!-- AUTO:rrg-breadth:end -->

**研究結論**：2026 上半年交易集中 **強勢／過熱** · 分層見 [最新市場環境](/) · [環境層](layer_regime)。

---

## 互動素材 · RRG 軌跡時間軸

回測產物含 **RRG 軌跡時間軸** 互動 HTML（inline JSON + `<script>`，單檔約 1–2MB）。範例檔名：`20260620_rrg_mono_hold7_slots_rrg_timeline_2026.html`。

| 交付方式 | 說明 |
|----------|------|
| **Supabase Storage + iframe** | 上傳至 public bucket · Readdy 以 `<iframe src="…">` 嵌入研究附錄 · **v1 推薦** |
| **JSON + React 元件** | 從 HTML 抽出軌跡 JSON · 站內原生渲染 · v2 正規 |
| ~~`site_content.content_html`~~ | **不可行** — DOMPurify 會移除 script |

產生：`python scripts/render_rrg_universe_html.py` → `reports/research/rrg/`。網站嵌入契約見 [日報首頁規格 · 互動研究素材](daily_home#互動研究素材-rrg-timeline)。

---

## 5 · 採納摘要

| 研究 | → 策略 |
|------|--------|
| 單軌 fresh + 依軌跡末段 + 持7日 + 3 槽 | [RRG 單軌](strategy_rrg_mono_hold7) |
| 日頻篩選 | 收盤後掃描 |
