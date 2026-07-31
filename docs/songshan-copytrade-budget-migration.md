# Songshan Copytrade 預算制修改說明

## 📋 修改摘要（2026-07-24）

**改動**：Songshan 跟單策略從**固定 1 張整股**改為**約 10 萬台幣預算制（零股）**。

---

## 🎯 新舊對比

| 項目 | 舊版（至 2026-07-23） | 新版（2026-07-24 起） |
|------|---------------------|---------------------|
| **數量方式** | 固定 1000 股（1 張） | 預算制：約 10 萬台幣 |
| **計算邏輯** | 無計算 | `qty = floor(budget / ask_price)` |
| **市場類型** | `common`（整股） | 動態：<1000 股用 `intraday_odd`（零股），≥1000 股用 `common` |
| **實際金額** | 股價 × 1000 | 股價 × 計算股數 ≈ 10 萬 |
| **配置參數** | `quantity_shares: 1000` | `budget_twd: 100000` |
| **環境變數** | `ORDER_SONGSHAN_COPYTRADE_QTY=1000` | `ORDER_SONGSHAN_COPYTRADE_BUDGET_TWD=100000` |

---

## 💰 計算範例（預算 = 10 萬台幣）

| 場景 | 買一價 | 股數 | 實際金額 | 市場類型 |
|------|--------|------|----------|---------|
| 低價股（50元） | 50.00 | 2000 | 100,000 | common（整股） |
| 中價股（100元） | 100.00 | 1000 | 100,000 | common（整股） |
| 高價股（200元） | 200.00 | 500 | 100,000 | intraday_odd（零股） |
| 華新科（52.4元） | 52.40 | 1908 | 99,979 | common（整股） |
| 聯發科（1200元） | 1200.00 | 83 | 99,600 | intraday_odd（零股） |
| 台積電（680元） | 680.00 | 147 | 99,960 | intraday_odd（零股） |

---

## 🔧 配置修改

### 1️⃣ **config/order.yaml**

```yaml
songshan-copytrade:
  enabled: true
  dry_run: true  # live: ORDER_SONGSHAN_COPYTRADE_DRY_RUN=0
  auto_submit: true
  budget_twd: 100000        # ✅ 新增：約 10 萬台幣
  quantity_shares: null     # ✅ 改為 null（fallback；優先用 budget_twd）
  market_type: intraday_odd # ✅ 預設零股（≥1000 股自動改 common）
  # ... 其他參數不變
```

### 2️⃣ **.env** 或 **order.env**（Mac mini）

```bash
# 新環境變數（優先）
ORDER_SONGSHAN_COPYTRADE_BUDGET_TWD=100000

# 舊環境變數（fallback，可選）
# ORDER_SONGSHAN_COPYTRADE_QTY=1000
```

**優先順序**：`env 變數` > `config/order.yaml` > `預設值 100000`

---

## 📐 計算規則

```python
# 1. 計算股數
qty = int(budget_twd / ask_price)

# 2. 檢查最小股數
if qty < 1:
    return "budget_too_low"  # 跳過不買

# 3. 決定市場類型
if qty < 1000:
    market_type = "intraday_odd"  # 零股
else:
    market_type = "common"         # 整股
```

---

## 🚀 如何啟用（Mac mini）

### **Step 1：更新配置**

確認 `config/order.yaml` 已有 `budget_twd: 100000`（已提交）。

### **Step 2：同步 `.env` 和 `order.env`**

在 Mac mini 上：

```bash
# 方式 A：直接編輯
nano ~/goldenstocks/.env
# 加入或修改：
ORDER_SONGSHAN_COPYTRADE_BUDGET_TWD=100000

# 方式 B：從 MacBook 同步
ssh mac-mini
cd ~/goldenstocks
git pull  # 拉取最新代碼
# 手動同步 .env（勿 git 提交）
```

### **Step 3：更新 `order.env`**（launchd 用）

```bash
# Mac mini 上
cd ~/goldenstocks
# 更新 ~/Library/Application\ Support/com.jackm4.goldenstocks/order.env
# 可用 scripts/install-launchd.sh 重新安裝（會自動從 .env upsert）
bash scripts/install-launchd.sh
```

### **Step 4：測試 dry-run**

```bash
# Mac mini 上
cd ~/goldenstocks
PYTHONPATH=src .venv-fubon/bin/python scripts/order/run_songshan_copytrade_poll.py \
  --date 2026-07-24 --time 09:25 --force-dry-run
```

**檢查 JSON 輸出**：
- `quantity_shares` 應該是計算值（非 1000）
- `budget_twd: 100000`
- `market_type: "intraday_odd"` 或 `"common"`（依股價）

### **Step 5：開 live**（確認後）

```bash
# .env 或 order.env
ORDER_SONGSHAN_COPYTRADE_DRY_RUN=0
ORDER_SONGSHAN_COPYTRADE_AUTO_SUBMIT=1
# 重新載入 launchd
bash scripts/install-launchd.sh
```

---

## 🧪 測試清單

### ✅ **Dry-run 測試**

```bash
# 1. 測試計算邏輯（不同股價）
PYTHONPATH=src .venv-fubon/bin/python scripts/order/run_songshan_copytrade_poll.py \
  --date 2026-07-24 --time 09:25 --force-dry-run

# 2. 檢查 snapshot JSON
cat reports/order/snapshots/songshan_copytrade_latest.json
```

**預期輸出關鍵欄位**：
```json
{
  "entries": [
    {
      "quantity_shares": 1908,  // 計算值（非固定 1000）
      "ask": 52.4,
      "budget_twd": 100000,
      "market_type": "common",  // 或 intraday_odd
      "status": "dry_run"
    }
  ]
}
```

### ✅ **Ledger 檢查**

Live 送單後：

```bash
cat data/order/songshan_copytrade_ledger.json
```

確認：
- `quantity_shares` 是動態計算值
- `market_type` 正確（零股/整股）
- `broker.is_success: true`

---

## ⚠️ 注意事項

### 1️⃣ **不影響既有邏輯**

下列規則**完全保留**：
- ✅ 09:25–09:40 窗口
- ✅ 25m nonfail 閘門（fail_break 跳過）
- ✅ 凱基-松山 9217 分點
- ✅ 5 日買入 ≥ 0.5 億 + 淨比 ≥ 95%
- ✅ !mega 黑名單
- ✅ `already_handled` 防重複
- ✅ 郵件通知

### 2️⃣ **fallback 機制**

如果 `budget_twd` 和 `quantity_shares` **都未設定**：
- 預設 `budget_twd = 100000`

如果只想用固定股數（舊模式）：
```yaml
songshan-copytrade:
  budget_twd: null
  quantity_shares: 1000
```

### 3️⃣ **預算不足處理**

若 `qty < 1`（如預算 1 萬買聯發科 1200 元）：
- Status: `"budget_too_low"`
- **不送單**
- Ledger **不記錄**（避免 burn T+1 slot）

### 4️⃣ **Mac mini 同步步驟**

修改 `.env` 後：
1. 同步到 `~/Library/Application Support/com.jackm4.goldenstocks/order.env`
2. 重新載入 launchd：`bash scripts/install-launchd.sh`
3. 或手動：`launchctl unload` → `launchctl load`

---

## 📁 修改檔案清單

| 檔案 | 修改內容 |
|------|---------|
| `src/order/songshan_copytrade_config.py` | 新增 `budget_twd` 欄位，`quantity_shares` 改為可選 |
| `src/order/songshan_copytrade_order.py` | 加入 `qty = int(budget / ask)` 計算邏輯 |
| `config/order.yaml` | `budget_twd: 100000`, `quantity_shares: null`, `market_type: intraday_odd` |
| `.env.example` | `ORDER_SONGSHAN_COPYTRADE_BUDGET_TWD=100000` |

---

## 🔍 Troubleshooting

### ❌ **問題：仍然買 1000 股**

**原因**：環境變數未更新或 `order.env` 未同步。

**解決**：
```bash
# Mac mini
grep SONGSHAN .env
grep SONGSHAN ~/Library/Application\ Support/com.jackm4.goldenstocks/order.env
# 確認有 BUDGET_TWD=100000
bash scripts/install-launchd.sh  # 重新同步
```

### ❌ **問題：snapshot 無 `budget_twd` 欄位**

**原因**：代碼未更新。

**解決**：
```bash
# Mac mini
cd ~/goldenstocks
git pull
```

### ❌ **問題：零股送單失敗**

**原因**：富邦 API 零股限制（如 qty < 1 或非交易時段）。

**解決**：檢查 broker error message，確認：
- 時段：09:00–13:30
- 股數 ≥ 1
- 價格合理（非漲跌停鎖單）

---

## 📊 回測驗證（可選）

如需驗證預算制與固定股數的歷史績效差異：

```bash
# 運行回測腳本（需自行撰寫）
PYTHONPATH=src .venv/bin/python scripts/research/run_songshan_budget_vs_qty_backtest.py
```

---

## 📞 支援

如有問題：
1. 查看 ledger：`cat data/order/songshan_copytrade_ledger.json`
2. 查看 snapshot：`cat reports/order/snapshots/songshan_copytrade_latest.json`
3. 查看 launchd log：`cat logs/replay/launchd_songshan-copytrade_*.log`（若啟用）
4. 手動 dry-run 測試（見上方 Step 4）

---

**版本**：v1.0 · 2026-07-24  
**Git commit**：`2b8cea1`  
**相關文件**：`docs/order-layer-prd.md` §1.2 · `config/job_registry.yaml`
