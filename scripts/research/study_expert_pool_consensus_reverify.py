#!/usr/bin/env python3
"""重新驗證 run_expert_pool_watch.py 的 POOLS（2344/8046/8358）共識規則。

背景：POOLS 用「回看1日共識≥2家 core 分點」跟單南電(8046)/金居(8358)，其中
9216(凱基-信義)、9268(凱基台北) 同時也出現在
reports/research/branch-footprint-screen/market_maker_branch_exclusion_v1.json
的17個「造市/綜合流量分點」排除清單裡（全勤率過高、買賣金額比近50/50、觸及股票數
過廣，H7/H14/H20 leg分解證明「效果隨天期增強」是統計假象）。

本腳本用完整歷史 (2024-07-01..最新) 重建三組 POOLS 共識事件，套用
scripts/research/build_expert_pool_trade_knowledge.py 完全相同的 build_signals()/
dedupe() 定義（直接動態載入該模組重用，不重寫規則），跑 L1H7 SSOT 協議
（T+1開/H7收/30bps/β=1.15/vs IX0001），並用
src/research/branch_signal_validation.py 的 permutation_test 做 5000 次「同股隨機
時機」placebo 檢定 + outlier_trim_sensitivity 敏感度檢查 + t-test/Wilcoxon/bootstrap
CI，交叉驗證原 P4-P6 網格搜尋
(reports/.../fubon_xindian_funnel/p6_grid_8046_8358.md) 挑出的「冠軍」規則能否通過
更嚴謹的統計檢定。

額外查證：9216（8046池）、9268（8358池）在「共識觸發日」vs「該分點在該股票上的
所有交易日」的 share_of_day_buy（該分點淨買金額 / 當日該股全市場淨買金額加總）是否
有系統性差異——測試「跟其他2家分點同時出現，是否篩掉了造市雜訊，只留下真正重要的
重疊日」這個假說。

Run（需 mini：stock_broker_branch_daily 只有 mini 有完整資料）：
  ssh -o BatchMode=yes mac-mini-lan "cd '/Users/jackm4/Documents/ETF/股票研究' && \
    source .venv/bin/activate 2>/dev/null; PYTHONPATH=src .venv/bin/python \
    scripts/research/study_expert_pool_consensus_reverify.py"

輸出：
  reports/research/branch-footprint-screen/study_expert_pool_consensus_reverify.json
  reports/research/branch-footprint-screen/study_expert_pool_consensus_reverify.md
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path
from statistics import NormalDist

import numpy as np
import pandas as pd
from scipy import stats

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from research.branch_signal_validation import (  # noqa: E402
    build_l1h7_signal_dict,
    outlier_trim_sensitivity,
    permutation_test,
)
from stock_db import DEFAULT_DB_PATH  # noqa: E402

# Extend window past the original P4-P6 freeze (..2026-07-17) to latest available data.
START = "2024-07-01"
END = "2026-07-27"
N_PERM = 5000
SEED = 20260728
OUT_JSON = (
    ROOT
    / "reports"
    / "research"
    / "branch-footprint-screen"
    / "study_expert_pool_consensus_reverify.json"
)
OUT_MD = OUT_JSON.with_suffix(".md")

MARKET_MAKER_IDS = {"9216", "9268"}  # under scrutiny in this study


def load_knowledge_mod():
    """Dynamically load build_expert_pool_trade_knowledge.py to reuse its exact
    build_signals()/dedupe()/build_ledger() logic (SSOT for the consensus rule +
    L1H7 protocol), without re-deriving it here."""
    path = ROOT / "scripts" / "research" / "build_expert_pool_trade_knowledge.py"
    spec = importlib.util.spec_from_file_location("epw_kb_reverify", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["epw_kb_reverify"] = mod
    assert spec.loader
    spec.loader.exec_module(mod)
    # Extend the frozen window to latest available data (full 2024-07..2026-07 history,
    # not just the original P4-P6 freeze that stopped at 2026-07-17).
    mod.START = START
    mod.END = END
    return mod


def load_watch_pools():
    path = ROOT / "scripts" / "research" / "run_expert_pool_watch.py"
    spec = importlib.util.spec_from_file_location("epw_watch_reverify", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["epw_watch_reverify"] = mod
    assert spec.loader
    spec.loader.exec_module(mod)
    return mod


def connect_ro(db_path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True, timeout=120)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=120000")
    return c


def bootstrap_ci(vals: np.ndarray, *, n_boot: int = 10000, seed: int = SEED) -> dict:
    rng = np.random.default_rng(seed)
    n = len(vals)
    if n == 0:
        return {"mean_ci95": [None, None], "median_ci95": [None, None]}
    means = np.empty(n_boot)
    medians = np.empty(n_boot)
    for i in range(n_boot):
        s = vals[rng.integers(0, n, size=n)]
        means[i] = np.mean(s)
        medians[i] = np.median(s)
    return {
        "mean_ci95": [float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))],
        "median_ci95": [float(np.percentile(medians, 2.5)), float(np.percentile(medians, 97.5))],
    }


def stat_tests(vals: np.ndarray) -> dict:
    n = len(vals)
    out: dict = {"n": int(n)}
    if n == 0:
        return out
    out["mean_pct"] = round(float(np.mean(vals)), 3)
    out["median_pct"] = round(float(np.median(vals)), 3)
    out["std_pct"] = round(float(np.std(vals, ddof=1)), 3) if n > 1 else None
    out["win_rate_pct"] = round(100.0 * float(np.mean(vals > 0)), 1)
    if n > 1 and np.std(vals) > 0:
        t_res = stats.ttest_1samp(vals, 0.0)
        out["t_stat"] = round(float(t_res.statistic), 4)
        out["t_p_two_sided"] = float(t_res.pvalue)
    else:
        out["t_stat"] = None
        out["t_p_two_sided"] = None
    try:
        w_res = stats.wilcoxon(vals)
        out["wilcoxon_stat"] = round(float(w_res.statistic), 4)
        out["wilcoxon_p_two_sided"] = float(w_res.pvalue)
    except ValueError:
        out["wilcoxon_stat"] = None
        out["wilcoxon_p_two_sided"] = None
    out.update(bootstrap_ci(vals))
    return out


def compute_share_of_day_buy(
    conn: sqlite3.Connection, stock_id: str, dates: list[str]
) -> dict[str, float | None]:
    """For each date, this branch's net buy amount / sum of all-branch positive
    net buy amounts that day for that stock (i.e. share of total net buying)."""
    out: dict[str, dict[str, float]] = {}
    if not dates:
        return {}
    ph = ",".join("?" * len(dates))
    rows = conn.execute(
        f"""
        SELECT b.trade_date, b.securities_trader_id, b.net, p.close
        FROM stock_broker_branch_daily b
        JOIN stock_daily_bars p
          ON p.stock_id=b.stock_id AND p.trade_date=b.trade_date AND p.source=b.source
        WHERE b.stock_id=? AND b.source='finmind' AND b.trade_date IN ({ph})
          AND p.close>0
        """,
        (stock_id, *dates),
    ).fetchall()
    by_date: dict[str, list[tuple[str, float]]] = {}
    for r in rows:
        d = r["trade_date"]
        amt = float(r["net"]) * float(r["close"])
        by_date.setdefault(d, []).append((r["securities_trader_id"], amt))
    return by_date


def share_stats_for_branch(
    conn: sqlite3.Connection, stock_id: str, tid: str, dates: list[str]
) -> dict[str, float | None]:
    by_date = compute_share_of_day_buy(conn, stock_id, dates)
    shares = {}
    for d, amts in by_date.items():
        total_buy = sum(a for _t, a in amts if a > 0)
        this = next((a for t, a in amts if t == tid), 0.0)
        if total_buy > 0:
            shares[d] = max(this, 0.0) / total_buy
        else:
            shares[d] = None
    return shares


def all_trading_dates_for_branch(
    conn: sqlite3.Connection, stock_id: str, tid: str
) -> list[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT trade_date FROM stock_broker_branch_daily
        WHERE stock_id=? AND source='finmind' AND securities_trader_id=?
          AND trade_date BETWEEN ? AND ?
        ORDER BY trade_date
        """,
        (stock_id, tid, START, END),
    ).fetchall()
    return [r["trade_date"] for r in rows]


def main() -> int:
    kb = load_knowledge_mod()
    watch = load_watch_pools()
    conn = connect_ro()

    ix_bars = kb.load_ix(conn)
    ix_dict = build_l1h7_signal_dict([(d, o, c) for d, o, c in ix_bars])

    results: dict[str, dict] = {}
    md_sections: list[str] = []

    for sid in ("2344", "8046", "8358"):
        spec = watch.POOLS[sid]
        core = dict(spec.core)
        floor = float(spec.net_floor)
        floor_label = spec.floor_label
        mode = "回看1日共識"

        ledger = kb.build_ledger(
            conn, sid, spec.stock_name, core, mode, floor, floor_label,
            policy="H10_skip (reverify default; policy不影響進出場, 僅研究註記)",
            thin=False, tier="reverify", blurb="",
        )
        if not ledger:
            results[sid] = {"error": "insufficient bars"}
            continue

        trades = ledger["trades"]
        r_pct = np.array([t["r_pct"] for t in trades], dtype=float)
        r_adj = np.array([t["r_adj_pct"] for t in trades], dtype=float)

        real_events = pd.DataFrame(
            {"stock_id": [sid] * len(trades), "signal_date": [t["signal_date"] for t in trades]}
        )
        stock_bars = kb.load_stock(conn, sid)
        stock_dict = build_l1h7_signal_dict([(d, o, c) for d, o, c in stock_bars])

        perm = (
            permutation_test(
                real_events, {sid: stock_dict}, ix_dict, n_perm=N_PERM, seed=SEED
            )
            if len(trades) > 0
            else None
        )
        trim = outlier_trim_sensitivity(r_adj) if len(trades) > 0 else None

        res = {
            "stock_id": sid,
            "stock_name": spec.stock_name,
            "core": core,
            "net_floor": floor,
            "floor_label": floor_label,
            "mode": mode,
            "window": f"{START}..{END}",
            "n_raw_signal_days": None,  # filled below
            "n_trades_after_dedupe": len(trades),
            "real_r_pct": stat_tests(r_pct),
            "real_r_adj_pct": stat_tests(r_adj),
            "permutation_test_r_adj": perm,
            "outlier_trim_sensitivity_r_adj": trim,
            "trades": trades,
        }
        results[sid] = res

        md_sections.append(f"## {spec.stock_name}（{sid}）· core={', '.join(f'{v}({k})' for k, v in core.items())} · 門檻{floor_label}\n")
        md_sections.append(f"- window: `{START}..{END}` (原P4-P6凍結至2026-07-17；本次延伸至最新)")
        md_sections.append(f"- n (dedupe 5日後) = **{len(trades)}**")
        rt = res["real_r_adj_pct"]
        md_sections.append(
            f"- r_adj: mean={rt.get('mean_pct')}% median={rt.get('median_pct')}% "
            f"win_rate={rt.get('win_rate_pct')}% "
            f"t_p={rt.get('t_p_two_sided')} wilcoxon_p={rt.get('wilcoxon_p_two_sided')} "
            f"bootstrap_mean_ci95={rt.get('mean_ci95')} bootstrap_median_ci95={rt.get('median_ci95')}"
        )
        if perm:
            md_sections.append(
                f"- permutation (n_perm={perm['n_perm']}): observed_mean={perm['observed_mean_pct']:.3f}% "
                f"observed_median={perm['observed_median_pct']:.3f}% "
                f"placebo_mean_of_means={perm['placebo_mean_of_means_pct']:.3f}% "
                f"(std={perm['placebo_std_of_means_pct']:.3f}) "
                f"p_mean={perm['p_value_mean_onesided']:.4f} p_median={perm['p_value_median_onesided']:.4f}"
            )
        if trim:
            md_sections.append(f"- outlier trim sensitivity: {trim}")
        md_sections.append("")

    # --- Step 3 extra: 9216 (8046) / 9268 (8358) consensus-day vs typical-day share_of_day_buy ---
    md_sections.append("## 9216/9268：共識日 vs 平常日 share_of_day_buy\n")
    scrutiny = {
        "8046": ("9216", "凱基-信義"),
        "8358": ("9268", "凱基台北"),
    }
    share_results: dict[str, dict] = {}
    for sid, (tid, name) in scrutiny.items():
        trades = results.get(sid, {}).get("trades") or []
        consensus_dates = [t["signal_date"] for t in trades]
        all_dates = all_trading_dates_for_branch(conn, sid, tid)
        typical_dates = [d for d in all_dates if d not in set(consensus_dates)]

        share_cons = share_stats_for_branch(conn, sid, tid, consensus_dates)
        share_typ = share_stats_for_branch(conn, sid, tid, typical_dates)

        cons_vals = np.array([v for v in share_cons.values() if v is not None])
        typ_vals = np.array([v for v in share_typ.values() if v is not None])

        mw = None
        if len(cons_vals) > 0 and len(typ_vals) > 0:
            try:
                mw_res = stats.mannwhitneyu(cons_vals, typ_vals, alternative="two-sided")
                mw = {"stat": float(mw_res.statistic), "p_two_sided": float(mw_res.pvalue)}
            except ValueError:
                mw = None

        share_results[sid] = {
            "trader_id": tid,
            "trader_name": name,
            "n_consensus_days": int(len(cons_vals)),
            "n_typical_days": int(len(typ_vals)),
            "consensus_day_share_mean": round(float(np.mean(cons_vals)), 4) if len(cons_vals) else None,
            "consensus_day_share_median": round(float(np.median(cons_vals)), 4) if len(cons_vals) else None,
            "typical_day_share_mean": round(float(np.mean(typ_vals)), 4) if len(typ_vals) else None,
            "typical_day_share_median": round(float(np.median(typ_vals)), 4) if len(typ_vals) else None,
            "mann_whitney_u": mw,
        }
        md_sections.append(
            f"- **{name}（{tid}）** on {sid}: 共識日 n={len(cons_vals)} "
            f"share_mean={share_results[sid]['consensus_day_share_mean']} "
            f"share_median={share_results[sid]['consensus_day_share_median']} "
            f"| 平常交易日 n={len(typ_vals)} "
            f"share_mean={share_results[sid]['typical_day_share_mean']} "
            f"share_median={share_results[sid]['typical_day_share_median']} "
            f"| Mann-Whitney p={mw['p_two_sided'] if mw else None}"
        )
    md_sections.append("")

    out = {
        "generated_at": pd.Timestamp.now().isoformat(),
        "protocol": {
            "entry": "T+1 open",
            "exit": "H7 close",
            "cost_bps": 30,
            "beta": 1.15,
            "bench": "IX0001",
            "consensus_rule": "回看1日共識（D-1∪D 在場≥2 且今日≥1 達floor）",
            "dedupe_days": 5,
            "window": f"{START}..{END}",
            "n_perm": N_PERM,
            "seed": SEED,
        },
        "pools": {
            sid: {k: v for k, v in r.items() if k != "trades"} for sid, r in results.items()
        },
        "pools_trades_detail": {sid: r.get("trades") for sid, r in results.items()},
        "consensus_day_vs_typical_day_share": share_results,
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")

    md = ["# POOLS 共識重新驗證 · L1H7 + Permutation · " + pd.Timestamp.now().strftime("%Y-%m-%d"), ""]
    md += md_sections
    OUT_MD.write_text("\n".join(md) + "\n", encoding="utf-8")

    print(f"wrote {OUT_JSON}")
    print(f"wrote {OUT_MD}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
