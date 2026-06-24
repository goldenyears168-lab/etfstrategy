# 盤中減碼準則 · Intraday exit playbook **v2**

**層級**：Order layer（下單層）輔助規則 · 非 Strategy layer 採納規格  
**回測依據**：72 檔宇宙 × 110 交易日（2026-01-02～06-22）· CORE4 · 持倉子集 · sync_dd3 環境日 · 6/23 真實分 K（2337/5347）· Bootstrap / beta 分離  
**v2 變更**：全宇宙一致性回測後 **停用 S3/S4**；S2 限環境；修正預期效果區間  
**免責**：研究用執行框架；實盤需 dry-run、滑價與 API 限流自行驗證。

---

## 1. 設計原則

1. **1 分 K / 即時報價**只負責「幾點賣」；**賣不賣**由結構（RRG / VCP）與**組合級閘門**決定。
2. **09:00～09:04 不賣**：集合競價與首根 K 低點不可靠（5347 6/23 假觸發實證）。
3. **非同步日**禁止機械 **-2%** 全倉賣出（宇宙回測勝率 ~41%，中位 save 為正 → 早賣略輸）。
4. 策略本質為 **beta timing（市場減倉）**，非選股 alpha；須搭配 **2330 / 台指** 弱勢確認。
5. 滑價預算 **≤ 0.5%**；流動性差的標的（3008、3264）提高門檻或改限價。
6. **v2**：分規則獨立回測後，**-2% 減碼路徑（S3/S4）正式停用**；僅保留結構性停損（S1）與環境限定 -3%（S2）。

---

## 2. 每日流程（時間軸）

| 時間 | 動作 | 說明 |
|------|------|------|
| **08:50** | 持倉快照 | 富邦 API → SQLite `order_holdings_snapshot`（見 §5） |
| **08:55** | 結構快取 | 載入昨收 RRG 象限、VCP state、VCP stop、10 日均線 |
| **09:00～09:04** | 僅記錄報價 | **不下賣單** |
| **09:05** | **組合閘門** | 判定 `portfolio_exit_mode` ON/OFF（§3.1） |
| **09:06～09:30** | 高頻監控 | **每 1 分鐘**一輪（§4） |
| **09:30～13:20** | 一般監控 | **每 5 分鐘**一輪 |
| **13:20 後** | 停止自動減碼 | 避免尾盤流動性與結算噪訊；改隔日處理 |

---

## 3. 決策規則

### 3.1 組合閘門（09:05 僅此一次必跑）

```
portfolio_exit_mode = ON  當且僅當：
  (A) CORE4（3264/2327/2449/3211）中 ≥2 檔 09:05 價 ≤ 昨收 × 0.98
  AND
  (B) 2330 09:05 價 ≤ 昨收 × 0.995
       （備援：台指 IX0001 前日收盤趨勢為轉弱 / 當日 gap ≤ -0.5%）
```

| 指標 | 回測參考（v2 宇宙） |
|------|---------------------|
| portfolio gate（上述定義） | 110 日中 **12 日** ON |
| sync_dd3（≥3 CORE4 盤中最大回撤 ≤-3%） | **28 日**；與 gate 重疊 7 日 |
| k=2 檔（CORE4） | precision ~85% 預測同步急跌日；recall ~46% |
| k=3 檔 | 過保守，recall ~20%，**不作唯一條件** |

`portfolio_exit_mode = OFF` 時：**禁止** S2（-3% 環境減碼）；S1 結構停損仍有效。

**v2 補充 — sync_dd3 環境標記**（盤中可事後確認，或 09:30 後標記供隔日參考）：

```
sync_dd3 = ON  當 CORE4 中 ≥3 檔 盤中 low ≤ 昨收 × 0.97
```

S2 在 v2 中**建議僅在 `sync_dd3=ON` 或 `portfolio_exit_mode=ON` 時啟用**（見 §3.3）。

### 3.2 標的結構分級（每日開盤前由昨收資料）

| 分級 | 條件（任一） | 代碼標籤 |
|------|--------------|----------|
| **弱** | RRG `weakening` 或 `lagging`；或 VCP `Overextended` | `tier=weak` |
| **中** | 非弱非強 | `tier=neutral` |
| **強** | RRG `leading` 或 `improving`（且非 Overextended） | `tier=strong` |

**額外禁止 -2% 機械規則**（歷史 E1 勝率 <50%）：`2327`、`3264`、`5347` — 僅允許 §3.3 S1 / S2。

### 3.3 單檔賣出觸發（僅 09:05 之後）· **v2**

使用即時價 `px`（富邦報價或最近 1 分 K 收盤）；觸及條件可用 `low` 但**不早於 09:05**。

| 優先序 | 規則 | 條件 | 動作 | v2 狀態 |
|--------|------|------|------|---------|
| **S1a** | VCP stop | 跌破 **VCP stop_loss** | 市價賣出 **100%** | **CAUTION** — 保留 |
| **S1b** | RRG weak | 昨收 RRG 已為 `weakening` 且 `px ≤ 昨收 × 0.97` | 市價賣出 **100%** | **CAUTION** — 保留（宇宙勝率最高 ~45%） |
| **S2** | 弱檔 -3% | `tier=weak` 且 `px ≤ 昨收 × 0.97` 且（`sync_dd3=ON` **或** `portfolio_exit_mode=ON`） | 市價賣出 **100%** | **CAUTION** — 限環境 |
| ~~**S3**~~ | ~~中性 -2%~~ | ~~`MODE=ON` 且 `tier=neutral` 且 `px ≤ 昨收 × 0.98`~~ | ~~賣 **50%**~~ | **DISABLE** — 勝率 26% |
| ~~**S4**~~ | ~~弱檔 -2%~~ | ~~`MODE=ON` 且 `tier=weak` 且 `px ≤ 昨收 × 0.98`~~ | ~~賣 **100%**~~ | **DISABLE** — 勝率 36% |
| **—** | 強檔 | `tier=strong` | **不主動賣**；僅 S1 | — |

**S1 適用範圍（v2）**：優先限 **持倉** 或 **CORE4**；全宇宙 72 檔套用時觸發過頻（n≈1,400）且勝率 ~42%，不建議無差別自動化。

**S2 適用範圍（v2）**：優先 **持倉 + sync_dd3** 或 **CORE4 + sync_dd3**；宇宙廣泛套用勝率仍 ~42%，Bootstrap CI 橫跨 0。

**10 日線**：`tier=strong` 若收盤跌破 10MA，改人工或隔日 S1 處理（盤中不自動）。

#### 分規則回測摘要（72 檔宇宙 · 獨立計 · 0.5% slip）

| 規則 | n | 勝率 | 中位 save | 旗標 |
|------|---|------|-----------|------|
| S1 VCP stop | 1,374 | 41.6% | +0.35% | CAUTION |
| S1 RRG weak | 354 | 45.2% | +0.35% | CAUTION |
| S2 弱檔 -3% | 1,283 | 42.2% | +0.48% | CAUTION |
| ~~S3 中性 -2%~~ | 31 | 25.8% | +0.73% | **DISABLE** |
| ~~S4 弱檔 -2%~~ | 312 | 36.2% | +0.88% | **DISABLE** |

關 S3/S4 後組合勝率 41.6% → **42.1%**（+0.5pp）；**無法突破 50%**。

### 3.4 不賣清單（硬性）

- 09:00～09:04
- `portfolio_exit_mode=OFF` **且** `sync_dd3=OFF` 時的 **S2**（-3% 環境減碼）
- ~~S3 / S4（-2% 減碼）~~ — **v2 全面停用**
- `2327`、`3264`、`5347` 的任何 -2% 路徑（v1 已禁，v2 無 S3/S4）
- 當日已觸發賣出 ≥ **2 檔**（避免組合衝擊）
- `ORDER_INTRADAY_EXIT_ENABLED≠1` 或 `--dry-run`

---

## 4. 監控頻率建議（回測對照）

| 時段 | 建議間隔 | 理由 |
|------|----------|------|
| 09:00～09:04 | 不賣；可 1 分鐘記錄 | 開盤噪訊 |
| **09:05** | **單次必跑** | 組合閘門唯一正式判定點 |
| **09:06～09:30** | **每 1 分鐘** | 急跌觸發中位 ~09:04–09:18；15 分鐘輪詢過慢 |
| 09:30～13:20 | **每 5 分鐘** | 與 E8 五輪詢回測接近；足夠覆蓋 -3% |
| 13:20 後 | 停止 | 非研究覆蓋區間 |

**不建議**全日每 1 秒 / 每 tick 監控：API 限流、與回測假設不符、邊際收益低。

**實作對照**（launchd 週一至五）：

```
08:50  order_holdings_snapshot.py
09:05  intraday_exit_watch.py --phase gate
09:06–09:30  每 1 分鐘  intraday_exit_watch.py --phase active
09:30–13:20  每 5 分鐘  intraday_exit_watch.py --phase active
```

可沿用 `config/order.yaml` 的 `schedule` 模式與 `ORDER_LAUNCHD_ENABLED` 閘門（與追價腳本相同安全習慣）。

---

## 5. SQLite 持倉快照（建議 schema）

```sql
-- 每日開盤前自富邦 API 寫入
CREATE TABLE IF NOT EXISTS order_holdings_snapshot (
    snapshot_date   TEXT NOT NULL,   -- YYYY-MM-DD
    stock_id        TEXT NOT NULL,
    stock_name      TEXT,
    shares          REAL NOT NULL,
    avg_cost        REAL,
    prev_close      REAL,            -- 昨收（TEJ/Yahoo/DB）
    rrg_quadrant    TEXT,             -- 昨收 RRG
    vcp_state       TEXT,
    vcp_stop_loss   REAL,
    structure_tier  TEXT,             -- weak | neutral | strong
    source          TEXT DEFAULT 'fubon',
    synced_at       TEXT,
    PRIMARY KEY (snapshot_date, stock_id)
);

CREATE TABLE IF NOT EXISTS order_intraday_exit_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date      TEXT NOT NULL,
    checked_at      TEXT NOT NULL,
    stock_id        TEXT,
    event           TEXT,            -- gate_on | gate_off | trigger_s1a | trigger_s1b | trigger_s2 | skip
    detail_json     TEXT,
    dry_run         INTEGER DEFAULT 1
);
```

**昨收來源優先**：`stock_daily_bars` → 富邦昨收 API。

---

## 6. 富邦 API 整合要點

1. **08:50** `查詢庫存` → 寫入 `order_holdings_snapshot`（僅 `shares > 0`）。
2. **每輪監控** `查詢報價`（或訂閱即時）→ 與 `prev_close`、結構分級比對。
3. **下單** 走既有 `order-intent-v1` → `scripts/order/submit_intents.py`（先 `--dry-run`）。
4. **賣出委託**：盤中減碼建議 **市價 ROD** 或 **限價 = 現價 × 0.995**（模擬 0.5% 滑價）。
5. **日誌**：每輪寫入 `order_intraday_exit_log` + `reports/order/snapshots/`。

環境變數建議：

```bash
ORDER_INTRADAY_EXIT_ENABLED=0   # 1 才實際送單
ORDER_INTRADAY_EXIT_DRY_RUN=1     # 預設 dry-run
ORDER_INTRADAY_EXIT_DISABLE_S3=1  # v2 預設 1（停用 -2% 減碼）
ORDER_INTRADAY_EXIT_DISABLE_S4=1  # v2 預設 1
```

---

## 7. 預期效果（誠實區間 · v2）

| 情境 | 預期 |
|------|------|
| 全宇宙 playbook（v1 含 S3/S4） | 勝率 ~42%；中位 save **+0.4%**（早賣略輸） |
| v2 關 S3/S4 | 勝率 ~42%；邊際 +0.5pp，**統計仍無顯著優勢** |
| sync_dd3 + S2 + 持倉 | n≈68；勝率 ~41%；Bootstrap P(早賣有益) **~6%** |
| sync_dd3 + S2 + CORE4 | n≈66；勝率 ~42%；CI 橫跨 0 |
| S1 RRG weak（全宇宙） | 勝率最高 ~45%；仍 <50% |
| 非同步日 | S2 不觸發；僅 S1 結構停損 |
| 6/23 型（開高後崩） | 09:05 閘門不開；2337 依 S2 **09:18** -3% 才賣 |
| 開盤異常型（5347） | 09:05 前不賣可避免假觸發 |

**結論**：v2 是**風險修剪**（去掉明顯有害規則），不是**alpha 升級**。盤中機械減碼在本樣本下屬 beta timing，不應期待穩定 >50% 勝率。

---

## 8. 實作檢查清單

- [ ] `order_holdings_snapshot` migration
- [ ] `scripts/order/sync_holdings_snapshot.py`（富邦 → SQLite）
- [ ] `scripts/order/intraday_exit_watch.py`（gate + S1a/S1b/S2；**S3/S4 預設關**）
- [ ] launchd plist（08:50 + 09:05–13:20）
- [ ] 至少 5 個交易日 dry-run 對照 log
- [ ] `ORDER_INTRADAY_EXIT_ENABLED=1` 前人工複核

---

## 9. 快速決策卡（v2）

```
08:50  持倉入庫 + 結構分級
09:05  CORE4≥2檔≤-2% 且 2330≤-0.5%？ → MODE=ON/OFF
09:06+ 破 VCP stop？ → S1a 賣100%（持倉/CORE4）
       RRG已weakening 且 ≤-3%？ → S1b 賣100%
       MODE=ON 或 sync_dd3 且 弱檔≤-3%？ → S2 賣100%
       強檔？ → 不賣（除非 S1）
       ~~中性≤-2%？~~ → v2 停用
       2327/3264/5347？ → 無 -2% 路徑
```

---

## 10. 相關腳本

| 腳本 | 用途 |
|------|------|
| `scripts/stress_test_intraday_exit.py` | 9 項壓力測試 |
| `scripts/stress_test_intraday_exit_phase2.py` | Bootstrap CI · beta/alpha · 6/23 驗證 |
| `scripts/backtest_intraday_exit_universe.py` | **v2** 全宇宙分規則回測 · `--json` 輸出旗標 |

```bash
PYTHONPATH=src python3 scripts/backtest_intraday_exit_universe.py
PYTHONPATH=src python3 scripts/backtest_intraday_exit_universe.py --json
```
