# 文檔清理報告：Readdy 公開站退役（2026-07-23）

## 📋 修改摘要

已完成所有文檔更新，移除或標注已退役的 Readdy 公開站相關內容。

---

## ✅ 已修改的文檔（4 個核心文件）

### 1️⃣ **README.md**
- ✅ 移除 "唯讀公開研究站（Readdy + Supabase）"
- ✅ 加入版本號：v2.1（2026-07-24）
- ✅ 加入重大變更歷史區塊
- ✅ 更新免責聲明（不提公開站）

**修改前**：
```markdown
台股量化交易研究系統：... + 唯讀公開研究站（**Readdy + Supabase**）。
> **免責**：... 公開站僅唯讀展示，不進行下單。
```

**修改後**：
```markdown
**當前版本**：v2.1（2026-07-24）
台股量化交易研究系統：... + Mac mini 自動下單執行層。
> ⚠️ **重大變更歷史**：
> - 2026-07-24: Songshan copytrade 改為預算制
> - 2026-07-23: 公開站 Readdy 退役（移至私人 ops 後台）
```

---

### 2️⃣ **docs/PRD.md**
- ✅ 版本號升級：2.0 → 2.1，加入最後更新日期
- ✅ 產品定位從「三個部分」改為「兩個核心部分」
- ✅ §7 公開站章節改為「已退役」並指向私人 ops 後台
- ✅ §8 每日排程移除 "Supabase sync"
- ✅ §11 已移除章節加入退役日期欄位（表格化）
- ✅ §12 非目標標注公開站已退役
- ✅ §13 成功標準刪除公開站健康檢查（第 4 點）

**關鍵修改**：
```markdown
## 1. 產品定位
台股量化交易研究系統，由**兩個核心部分**組成：
1. **本地研究 OS**
2. **下單執行層（本機 infra）**

**產出形式**：
- 📊 本地 markdown 報告（reports/daily/）
- 🔐 私人 ops 後台（非公開展示站）
```

---

### 3️⃣ **docs/architecture.md**
- ✅ 文檔開頭加入警告框
- ✅ §公開站 IA 整章標注 RETIRED
- ✅ 舊架構表格加入刪除線
- ✅ 指向私人 ops 後台與封存位置

**修改前**：
```markdown
## 公開站 IA（Readdy · Supabase 為 runtime SSOT）
| 層 | Runtime SSOT | Authoring |
...
```

**修改後**：
```markdown
## 公開站 IA（**RETIRED 2026-07-23**）

> ⚠️ **已退役**：原公開站 Readdy 已於 2026-07-23 退役並清空。
> 現行為**私人運維後台**，非公開展示站。

**舊架構（已封存）**：
| 層 | Runtime SSOT（已清空） | Authoring（已停用） |
| ~~stock_research.daily_briefs~~ | ~~Python sync~~ |
```

---

### 4️⃣ **docs/agent-brief.md**
- ✅ Readdy 公開站條目標記為 RETIRED
- ✅ 加入刪除線與退役日期

**修改前**：
```markdown
| **Readdy 公開站** | docs/architecture.md § Readdy | 前端 · Supabase publish 腳本 |
```

**修改後**：
```markdown
| ~~**Readdy 公開站**~~ | **RETIRED 2026-07-23** | ~~前端 · Supabase publish 腳本~~ |
```

---

## 📦 已移動的文檔（4 個 → archives/）

所有 Readdy 相關文檔移至 `archives/` 並加上 `RETIRED_` 前綴：

| 原位置 | 新位置 |
|--------|--------|
| `docs/readdy-regime-strategy-lineage.md` | `archives/RETIRED_readdy-regime-strategy-lineage.md` |
| `docs/readdy-view-ready-migration.md` | `archives/RETIRED_readdy-view-ready-migration.md` |
| `docs/readdy-stock-intelligence-spec.txt` | `archives/RETIRED_readdy-stock-intelligence-spec.txt` |
| `docs/homepage-copy-backend-plan.md` | `archives/RETIRED_homepage-copy-backend-plan.md` |

---

## 🔍 已解決的混淆點

| # | 問題 | 解決方案 |
|---|------|---------|
| 1 | PRD 說有「三個部分」但公開站已退役 | 改為「兩個核心部分」 |
| 2 | README 提到 Readdy + Supabase | 完全移除，改為私人 ops 後台說明 |
| 3 | 環境變數標記 RETIRED 但文檔仍說要 sync | 文檔移除 Supabase sync 提及 |
| 4 | 成功標準包含「公開站不 stale」 | 刪除該項，標注為 RETIRED |
| 5 | 缺少退役日期與版本追蹤 | 加入版本號 2.1 與重大變更歷史 |
| 6 | readdy-*.md 文件名誤導 | 移至 archives/ 並加 RETIRED_ 前綴 |
| 7 | Agent brief 仍指向 Readdy | 標注為 RETIRED 2026-07-23 |
| 8 | Architecture 整章公開站內容過時 | 整章改為 RETIRED，加警告框 |

---

## 📊 修改統計

| 指標 | 數量 |
|------|------|
| 修改的核心文檔 | 4 個 |
| 移動到 archives/ 的文檔 | 4 個 |
| 刪除的過時引用 | 10+ 處 |
| 新增的警告框 | 3 個 |
| 新增的退役日期標注 | 8 處 |

---

## ✅ 現在的文檔狀態

### **清晰標注的退役資訊**
- ✅ 版本號明確（v2.1 · 2026-07-24）
- ✅ 重大變更歷史可追蹤
- ✅ 退役日期統一（2026-07-23）
- ✅ 警告框醒目（⚠️ 符號）
- ✅ 過時文檔已封存（archives/RETIRED_*）

### **正確的產品定位**
- ✅ 兩個核心部分（本地研究 OS + 下單執行層）
- ✅ 私人 ops 後台（非公開展示站）
- ✅ 本地 markdown 報告為主要產出

### **環境變數與文檔一致**
- ✅ `.env.example` 標記 RETIRED
- ✅ PRD/README 不再提 Supabase sync
- ✅ Architecture 明確說明已退役

---

## 🎯 未來建議

### **保持文檔同步**
1. 重大架構變更時同步更新版本號
2. 在 README 維護重大變更歷史
3. 退役功能立即標注 RETIRED + 日期

### **標準格式**
```markdown
> ⚠️ **RETIRED YYYY-MM-DD**：簡短說明為何退役。
> 現行替代方案：...
> 詳見：archives/RETIRED_*.md
```

### **版本追蹤**
```markdown
**當前版本**：vX.Y（YYYY-MM-DD）
**重大變更歷史**：
- YYYY-MM-DD: 變更說明
```

---

## 📝 Git 提交記錄

```bash
commit 93923f0
docs: 清理公開站 Readdy 過時文檔（2026-07-23 已退役）

- 8 files changed, 81 insertions(+), 38 deletions(-)
- 已推送至 origin/main
```

---

**完成日期**：2026-07-25  
**修改者**：Cursor Cloud Agent  
**驗證狀態**：✅ 所有文檔已同步更新並推送
