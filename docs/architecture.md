# Architecture · Research OS

> **Single source of truth**：`config/pipelines/daily_close.yaml`（workflow DAG）、`scripts/daily_sync.sh`、launchd 排程。

## Pipeline

```text
Data / Ingest（infra · sync_* → stocks.db）
  → Facts · ETF 持股變化（etf_daily_report · layer: facts）
  → Regime four-axis diagnostic（regime_daily_brief · layer: regime）
  → Research（explore · sweep · 假說 · config/research.yaml）
  → Strategy（adopted specs · screen / backtest · config/strategy.yaml）
```

**已退役**：multi-track `research_digest` · `research_os` · `p6-tier-flow` daily 鏈。

---

## Terminology（市場狀態用語）

**完整規範**：[terminology.md](./terminology.md)（SSOT · 中英 canonical · 禁止混用）

| Canonical term | 中文 | 輸出 |
|----------------|------|------|
| **Trend posture** | Weinstein 階段 | `broadening` / `concentration` / … |
| **Breadth zone** | 廣度區間 | `oversold` … `overbought` |

> 口語「強勢／過熱」若指 **200MA 廣度五格**，必寫 **Breadth zone**。

---

## 收盤主線（`daily_close`）

| Strategy ID | Layer | 問題 | 模組 |
|-------------|-------|------|------|
| `etf-daily` | **facts** | 各 ETF 持股變化（L1 shares 差分 · 只報事實） | `etf_daily_report` |
| `regime-daily` | regime | Regime four-axis diagnostic（非 alpha） | `regime_daily_brief` |

產物：

- `reports/daily/etf-daily/daily_brief.md`
- `reports/daily/regime/daily_brief.md`
- `reports/daily/{date}_etf_daily.md`

Registry：`config/strategies.yaml`（`layer: facts` · `layer: regime`）

各層 **VFP 句**（驗收標準）見 [terminology.md](./terminology.md) §1.1。

---

## 採納策略（Strategy layer · 非 daily close）

| ID | 問題 | 模組 | 排程 |
|----|------|------|------|
| `00981a-l1h9` | 00981A 新进/加码 跟單 · T+1 開 · 持 9 日 | `copytrade/signals` · `copytrade_backtest` | manual |
| `rrg-mono-hold7` | RRG mono 槽位 hold7 | `rrg_mono_daily_brief` | launchd 16:40 |
| `vcp-pivot-gate` / `vcp-coil-close` | VCP funnel | `vcp_funnel_specs_daily` | launchd 13:00 |
| `minervini-sepa-basket` | Stage 2 basket 回測 | `broad_momentum_tv_backtest` | ad_hoc |

**SSOT**

| 檔案 | 用途 |
|------|------|
| [`config/strategy.yaml`](../config/strategy.yaml) | 採納規格 · `strategies.*.backtest` |
| [`config/strategies.yaml`](../config/strategies.yaml) | Registry · publish 路徑 · `layer: strategy` |
| [`config/research.yaml`](../config/research.yaml) | 探索主題 · sweep · 矩陣（採納前） |

Backtest spec 說明：[evaluation-contract.md](./evaluation-contract.md)

Copytrade 採納規格：**L1H9** · [00981a-copytrade-research-methodology.md](./00981a-copytrade-research-methodology.md)

---

## 報告目錄

| 路徑 | 用途 |
|------|------|
| `reports/daily/` | 排程產物（Facts · Regime · launchd brief 根檔） |
| `reports/research/` | 回測 JSON · 廣度 HTML · copytrade 深度研究 |
| `reports/samples/` | 可提交格式範例（版控） |
| [reports/README.md](../reports/README.md) | 目錄索引 |

程式常數：`src/report_paths.py`

---

## 已移除

- **`00981a-copytrade-l1`** track 殼（改 **`00981a-l1h9` / L1H9**）
- **`00981a-v9-hybrid`** · **`qlib-tw-factor`**
- **Swing 軌** · **E0 執行軌**（現行下單層見 `src/order/` · `config/order.yaml`）
- **Multi-track digest** · **`track_evaluation`** · **`evaluation_contract.yaml`**

Copytrade 研究保留：`scripts/run_00981a_copytrade_backtest.py` · [00981a-copytrade-research-methodology.md](./00981a-copytrade-research-methodology.md)

---

## 相關文件

| 文件 | 內容 |
|------|------|
| [terminology.md](./terminology.md) | 術語規範 SSOT |
| [src-map.md](./src-map.md) | `src/` 模組分層（L0–L5） |
| [daily-operations.md](./daily-operations.md) | infra 排程 SOP |
| [PRD.md](./PRD.md) | 產品範圍（living doc） |
| [evaluation-contract.md](./evaluation-contract.md) | Backtest spec · per-track JSON |
