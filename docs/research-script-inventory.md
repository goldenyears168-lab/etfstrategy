# Research script inventory · 腳本盤點表

| Field | Value |
|-------|-------|
| Version | 2026-07-09 · Phase C |
| SSOT | `config/research.yaml` · `config/strategy.yaml` · `config/pipeline_scripts.yaml` |
| Graduation gates | `config/research.yaml` → `graduation_gates` |
| Archived runners | `scripts/research/archive/`（77 支 · 對應 `status: archived` topic） |

## 分類定義

| 分類 | 說明 | 登錄位置 |
|------|------|----------|
| **strategy** | 已採納策略 · 回測或 production screen | `config/strategy.yaml` |
| **research** | 探索 topic · sweep · 假說 | `config/research.yaml` → `topics.*` |
| **research/archive** | Phase B 封存 runner · 手動重跑 | `scripts/research/archive/` + yaml `run_scripts` |
| **pipeline** | daily_sync / launchd · 非 alpha | `config/pipeline_scripts.yaml` |
| **delete** | 無 SSOT · 無下游依賴 · 已移除 | — |

---

## Pipeline / infra（9 · 已登錄）

| Script | 分類 | 觸發 |
|--------|------|------|
| `run_rrg_universe_close.py` | pipeline | daily_sync · `RUN_RRG_UNIVERSE_CLOSE` |
| `run_vcp_funnel_close.py` | pipeline | daily_sync · `RUN_VCP_FUNNEL_CLOSE` |
| `run_vcp_funnel_intraday.py` | pipeline | launchd 13:00 |
| `run_stock_daily_lens.py` | pipeline | daily_sync · `RUN_STOCK_DAILY_LENS` |
| `run_copytrade_l1h9_daily_brief.py` | pipeline | daily_sync · `copytrade_l1h9_screen` |
| `run_rrg_mono_intraday_watch.py` | pipeline | launchd 13:00 |
| `run_mutual_fund_disclosure_watch.py` | pipeline | launchd |
| `run_sync_mutual_fund_holdings.py` | pipeline | manual |
| `run_market_breadth_report.py` | regime | manual · breadth HTML |

---

## 原 36 orphan · 處置摘要

| Script | 處置 | 理由 |
|--------|------|------|
| `run_rrg_universe_close.py` | **pipeline** | daily_close 節點 |
| `run_vcp_funnel_close.py` | **pipeline** | VCP close screen |
| `run_vcp_funnel_intraday.py` | **pipeline** | VCP intraday screen |
| `run_stock_daily_lens.py` | **pipeline** | Lens → Supabase |
| `run_copytrade_l1h9_daily_brief.py` | **pipeline** | L1H9 daily brief wrapper |
| `run_rrg_mono_intraday_watch.py` | **pipeline** | hold7 盤中預警 |
| `run_mutual_fund_disclosure_watch.py` | **pipeline** | ACDD04 披露監控 |
| `run_sync_mutual_fund_holdings.py` | **pipeline** | 主動基金持股 sync |
| `run_market_breadth_report.py` | **regime** | 廣度 HTML 工具 |
| `run_s04_freq_compare.py` | **research** | → `factor-validation-s04` |
| `run_s04_monthly_tri_compare.py` | **research** | → `factor-validation-s04` |
| `run_c18_bavg_*` · `run_c18_dl*` · `run_c18_mom` · `run_c18acc_*` (8) | **delete** | C18acc 已 graduated · 一次性 sweep |
| `run_c13_*` (2) | **delete** | score-swap 子 sweep · 已採納 C18acc |
| `run_rrg_mono_c0_vs_a_proof.py` | **delete** | C0 證明已完成 |
| `run_rrg_mono_c4_validation.py` | **delete** | 近窗驗證已完成 |
| `run_rrg_mono_empty_fresh_fallback.py` | **delete** | hold7 子研究 · 未採納 |
| `run_rrg_mono_execution_timing_backtest.py` | **delete** | 時點對照 · 未採納 |
| `run_rrg_mono_volume_tier_study.py` | **delete** | 成交量分層 · 未採納 |
| `run_vcp_breadth_zone_backtest.py` | **delete** | VCP×廣度 · 一次性 |
| `run_vcp_expert_entry_sweep.py` | **delete** | VCP 1m 進場 sweep · 重跑時重建 |
| `run_chunge_l4_calibration.py` | **delete** | L4 校準 · 已 archive |
| `run_pullback_regime_backtest.py` | **delete** |  abandoned · 無 topic |
| `run_inst_flow_backtest.py` | **delete** | abandoned · 無 topic |
| `run_acdd04_copytrade_backtest.py` | **delete** | 主動基金實驗 · 無 topic |
| `run_rrg_rotation_backtest.py` | **delete** | 月頻 rotation 演示 · `rrg_rotation` 模組保留 |

---

## 已刪除模組（連帶）

| 模組 | 理由 |
|------|------|
| `src/research/backtest/pullback_regime_backtest.py` | 無 topic · 無採納 |
| `src/research/backtest/inst_flow_backtest.py` | 無 topic |
| `src/research/backtest/inst_flow_981a_overlap.py` | 僅 inst_flow 用 |
| `src/research/backtest/mutual_fund_copytrade.py` | ACDD04 實驗 |
| `src/research/backtest/rrg_mono_volume_tier.py` | 僅 volume tier study |

---

## Phase B · archived runners（2026-07-09）

| 項目 | 說明 |
|------|------|
| 目錄 | `scripts/research/archive/` |
| 觸發 | **非** daily_sync 主線 · 手動或 sweep CLI |
| yaml | `config/research.yaml` · `status: archived` 的 `run_scripts` 已指向 archive 路徑 |
| pipeline 例外 | `RUN_MARKET_PROBE=1` 時 daily_sync 仍呼叫 archive 內 `run_market_probe_radar.py` / `backfill_probe_kbar.py` |
| 保留 | `src/research/backtest/` 不搬 · ABC/C18acc 引擎仍引用 |

Active / graduated runner 仍在 `scripts/` 根目錄（例：`run_triple_wma_pullback_sweep.py` · `run_c18acc_abc_dual_sleeve_phase*.py`）。

---

## Phase C · pipeline 簡化（2026-07-09）

| 項目 | 變更 |
|------|------|
| `RUN_RRG_IMPROVING_WATCH` | 預設 **0**（`.env.example` · `daily_sync.sh` · `pipeline_gates.py`） |
| `RUN_MARKET_PROBE_RADAR` / `RUN_PROBE_KBAR_BACKFILL` | 預設 **0** |
| `config/buy_observation.yaml` | RRG Improving 池 `enabled: false`（定義保留 · radar 不算） |
| orphan runner | 登錄 `run_scripts_followup` / `compare_scripts` · 22 支移入 archive |
| 剩餘 orphan | 3 支 infra（`run_daily_sync` · `run_launchd_replay` · `run_signal_radar_replay`）→ `pipeline_scripts.yaml` |

手動重開 improving：`RUN_RRG_IMPROVING_WATCH=1` + 將 buy_observation 池 `enabled: true`。

---

## 驗證

```bash
cd "<project-root>"
PYTHONPATH=src python3 -m pytest tests/test_research_config.py -q
PYTHONPATH=src python3 <<'PY'
import glob, yaml
from pathlib import Path
with open("config/research.yaml") as f: r = yaml.safe_load(f)
with open("config/strategy.yaml") as f: s = yaml.safe_load(f)
with open("config/pipeline_scripts.yaml") as f: p = yaml.safe_load(f)
reg = set()
for t in r["topics"].values():
    for x in t.get("run_scripts") or []: reg.add(str(x))
    for x in t.get("run_scripts_followup") or []: reg.add(str(x))
for st in s["strategies"].values():
    for k in ("run_script","screen_script"):
        if st.get(k): reg.add(st[k])
for sc in p["scripts"].values(): reg.add(sc["path"])
# archived topic scripts live under archive/
archived_paths = {
    x for t in r["topics"].values() if t.get("status") == "archived"
    for x in (t.get("run_scripts") or [])
}
missing = sorted(p for p in archived_paths if not Path(p).is_file())
print("missing archived scripts:", missing or "none")
root_runs = set(glob.glob("scripts/run_*.py"))
reg_names = {Path(x).name for x in reg if not x.startswith("scripts/research/archive/")}
orphans = sorted(Path(x).name for x in root_runs if Path(x).name not in reg_names)
print("root orphans:", len(orphans), orphans[:20] if orphans else "none")
PY
```

---

## Sweep runner（trial registry）

| 模組 | 用途 |
|------|------|
| `src/research/sweep_runner.py` | topic → family → trial → run |
| `scripts/run_research_sweep.py` | CLI 模板 |
| `config/sweep_trial_registry.example.yaml` | JSON schema |

```bash
PYTHONPATH=src .venv/bin/python scripts/run_research_sweep.py \\
  --topic c18acc-snapshot-1300 --family alpha-sweep --dry-run
```

---

## 統一回測比較層

| 模組 | 用途 |
|------|------|
| `config/backtest_standard.yaml` | 五軌 adapter · 窗口 · 成本 |
| `scripts/run_unified_backtest_comparison.py` | league table |

```bash
PYTHONPATH=src .venv/bin/python scripts/run_unified_backtest_comparison.py
```
