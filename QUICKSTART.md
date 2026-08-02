# QUICKSTART · 第一次上手

> 目標：5 分鐘內確認「你在哪台機器」→ 裝好環境 →**不需任何付費金鑰**就跑通第一次驗證。
> 深入細節見 [docs/system-overview.md](docs/system-overview.md) 與 [CLAUDE.md](CLAUDE.md)。

---

## 0. 你在哪台機器？（先確認，這是硬邊界）

這份 repo 在兩台 Mac 各有獨立 checkout，動手前先確認自己在哪台：

```bash
scutil --get ComputerName                # 「minim4的Mac mini」= 主力機（開發＋生產）
launchctl list | grep -c goldenstocks    # >0 表示掛著 live 排程 → 這台是 mini
```

| 機器 | 角色 | 你能做什麼 |
|------|------|-----------|
| **Mac mini** | 主力機（唯一工作站兼生產機） | 改碼、研究、測試、`git commit＋push`、live launchd、送單。日常透過 SSH 進去做事 |
| **MacBook** | pull-only 異地備援（涼快備援機） | 只 `git pull --ff-only`；**永不** commit/push、**不裝** launchd |

Git 方向單向：**mini push、Book pull**。single source of truth = mini。

---

## 1. 前置

- **Python 3.13**（主環境）。
- `git clone` 這個 repo。
- **`GOLDENSTOCKS_DATA_DIR`**：可變狀態根目錄（`.env`、`data/`、`logs/`）刻意搬出 git tree（mini 為 `~/goldenstocks-data`），避免機密／大檔進版控。新程式碼讀寫 DB／log／ledger 一律走 `stock_db.DATA_DIR` / `DEFAULT_DB_PATH`，不要硬寫 `PROJECT_ROOT/data`。第一次驗證安裝可先不設（用預設）。

---

## 2. 主環境

```bash
cd ~/goldenstocks
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

### venv 對照表

| venv | 用途 | 何時才需要 |
|------|------|-----------|
| `.venv` | 主環境（Python 3.13） | 一律需要 |
| `.venv-fubon` | 富邦 Neo 下單 SDK wheel | 只有真的要送單（Order layer）才裝 |
| `.venv-qlib` | qlib 研究（Python 3.11） | 只有跑 qlib 研究才裝 |

後兩個是特殊用途，**新人第一次上手用不到**。

---

## 3. 不需付費金鑰的 first successful run（驗證安裝成功）

以下兩步**不需** `data/stocks.db`、也**不需** TEJ / FinMind 金鑰，純驗證安裝是否 OK：

```bash
# (1) 跑測試（排除 archived backtest）——全綠 = 安裝 OK
.venv/bin/pytest tests/ --ignore=tests/research/archive -q

# (2) config 一致性健檢（registry vs RUN_* 是否對得上）
PYTHONPATH=src .venv/bin/python src/pipeline_gates.py list-mismatches
```

`src/` 是 flat import root（不是 package），手寫腳本一律 `PYTHONPATH=src .venv/bin/python scripts/...`。

---

## 4. 要真資料才需要金鑰

要真的抓市場資料、跑收盤管線時，才需要金鑰：

```bash
cp .env.example .env
# 編輯 .env：填 TEJ_API_KEY、FINMIND_TOKEN
SYNC_PROFILE=slim scripts/daily_sync.sh --holdings-report   # 只跑 ingest + Facts + Regime
```

---

## 5. 深入閱讀順序

1. [docs/system-overview.md](docs/system-overview.md) —— 機器／資料／排程全貌
2. [docs/architecture.md](docs/architecture.md) —— 產品分層、收盤主線、SSOT 表
3. [docs/src-map.md](docs/src-map.md) —— `src/` L0–L5 與 import 規則
4. [docs/agent-brief.md](docs/agent-brief.md) —— 任務→先讀→可能改對照

---

## 6. 邊界警告

- **不要在 MacBook commit/push** —— Book 只 pull，會造成兩台分叉。
- **不要碰 Order live 旗標** —— 5 支 order job 三重鎖住（dry-run / disabled / master off）；恢復送單必須是使用者明確直接的指示。
- 生產 SQLite（mini `~/goldenstocks-data/data/stocks.db`，~40GB）**預設唯讀查詢**，寫入／`VACUUM` 會鎖住正在跑的排程。
