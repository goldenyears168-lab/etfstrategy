#!/usr/bin/env python3
"""Branch-follow · contract grid + relative-universe gate — Round 2 Fix-B.

Research-only. Uses existing local tape (no FinMind fetch). Frozen sub-protocol:
`reports/research/branch-footprint-screen/分點跟單全事件研究計畫.md` §10 Fix-B.

Isolates two Round-1 design choices while holding the D1' event definition FIXED
(unchanged from Round 1 / §2):
  buy_amt = buy × close ≥ abs_floor (default 0.30億)
  AND (buy_share ≥ share_floor OR day-rank(buy_amt) ≤ top_k)
  episode: same branch+stock, 5 calendar days, keep first (frozen, computed once)

Grid varies only the *contract* and *decision gate*:
  hold H ∈ {7, 12, 20} trading days (T+1 open entry → H-day close exit)
  benchmark multiplier β ∈ {1.0, 1.25} × IX0001
  decision gate: RELATIVE to each (H,β) combo's own IS/OOS universe distribution
    pass ⇔ IS median rank ≥ P75 of IS universe AND IS n≥30
         AND OOS median rank ≥ P90 of OOS universe AND OOS n≥15

Branch score = median r_adj over ALL qualifying events for that (H,β) (never
best (B,S)). Stock cost 30bps round-trip on every leg regardless of H.

Universe (documented per mission step 2): reuses the Round-1 "prefer2y"
247-branch subset via `specialty_candidate_pool.csv` tape coverage columns
(same helper as Round 1, imported read-only — Round-1 script is NOT modified).

Outputs under reports/research/branch-footprint-screen/:
  branch_follow_contract_grid_details.csv   (long format, all 6 combos)
  branch_follow_contract_grid_summary.md
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_branch_follow_all_events_scan import (  # noqa: E402  (read-only reuse)
    branch_score_table,
    build_calendar,
    collect_d1_events,
    connect_ro,
    episode_first_dates,
    l1h7_one,
    load_benchmark_ohlc,
    load_coverage_from_specialty_pool,
    load_local_universe,
    load_ohlc,
    spearman_rho,
)

OUT = Path("reports/research/branch-footprint-screen")
DB_PATH = Path("data/stocks.db")
BENCHMARK = "IX0001"
COST = 0.003  # 30 bps round-trip, every leg regardless of H
DEFAULT_IS = ("2024-07-01", "2025-12-31")
DEFAULT_OOS = ("2026-01-01", "2026-07-16")
DEFAULT_PROBE = ("2026-05-20", "2026-07-16")
EPISODE_GAP = 5
HOLDS = (7, 12, 20)
BETAS = (1.0, 1.25)
IS_PCT_GATE = 0.75
IS_N_GATE = 30
OOS_PCT_GATE = 0.90
OOS_N_GATE = 15
BOOTSTRAP_N = 2000
BOOTSTRAP_SEED = 20260720


def episode_signals(sig: pd.DataFrame, episode_gap: int) -> list[dict]:
    """Episode-dedupe per (B,S) once; independent of H/β so cached across grid."""
    out: list[dict] = []
    if sig.empty:
        return out
    name_map = (
        sig.groupby("securities_trader_id")["securities_trader"]
        .agg(lambda s: s.dropna().astype(str).iloc[0] if len(s.dropna()) else "")
        .to_dict()
    )
    for (bid, sid), g in sig.groupby(
        ["securities_trader_id", "stock_id"], sort=False
    ):
        dates = episode_first_dates(g["trade_date"].astype(str).tolist(), episode_gap)
        for d in dates:
            out.append(
                {
                    "branch_id": str(bid),
                    "branch_name": str(name_map.get(bid, "")),
                    "stock_id": str(sid),
                    "signal_date": d,
                }
            )
    return out


def evaluate_combo(
    signals: list[dict],
    *,
    cal: list[str],
    idx: dict[str, int],
    ohlc: dict[tuple[str, str], tuple[float, float]],
    benchmark_ohlc: dict[str, tuple[float, float]],
    hold: int,
    beta: float,
) -> list[dict]:
    events: list[dict] = []
    for s in signals:
        out = l1h7_one(
            s["signal_date"],
            s["stock_id"],
            cal=cal,
            idx=idx,
            ohlc=ohlc,
            benchmark_ohlc=benchmark_ohlc,
            bench_beta=beta,
            hold=hold,
            cost=COST,
        )
        events.append(
            {
                "branch_id": s["branch_id"],
                "branch_name": s["branch_name"],
                "stock_id": s["stock_id"],
                "signal_date": s["signal_date"],
                "r_stock": None if out is None else out[0],
                "r_bench": None if out is None else out[1],
                "r_adj": None if out is None else out[2],
            }
        )
    return events


def add_pct_rank(df: pd.DataFrame, col: str = "median_pct") -> pd.DataFrame:
    """Percentile rank within the scored (n>=1) universe; higher median = higher pct."""
    if df.empty:
        df = df.copy()
        df["pct_rank"] = pd.Series(dtype=float)
        return df
    scored = df[df["n"] >= 1].copy()
    scored["pct_rank"] = scored[col].rank(pct=True)
    unscored = df[df["n"] < 1].copy()
    unscored["pct_rank"] = np.nan
    return pd.concat([scored, unscored], ignore_index=True)


def bootstrap_p_median_gt0(
    xs: list[float], *, n_boot: int = BOOTSTRAP_N, seed: int = BOOTSTRAP_SEED
) -> float | None:
    """One-sided bootstrap p for H0: median<=0 vs H1: median>0.

    p = P(bootstrap median <= 0); small p ⇒ evidence median > 0.
    """
    arr = np.asarray([x for x in xs if x is not None], dtype=float)
    n = len(arr)
    if n < 2:
        return None
    rng = np.random.default_rng(seed)
    idx_mat = rng.integers(0, n, size=(n_boot, n))
    boot_medians = np.median(arr[idx_mat], axis=1)
    return float(np.mean(boot_medians <= 0.0))


def combo_relative_passers(
    is_df: pd.DataFrame,
    oos_df: pd.DataFrame,
) -> pd.DataFrame:
    if is_df.empty or oos_df.empty:
        return pd.DataFrame()
    a = is_df.rename(
        columns={
            "n": "is_n",
            "median_pct": "is_median_pct",
            "pct_rank": "is_pct_rank",
        }
    )[["branch_id", "branch_name", "is_n", "is_median_pct", "is_pct_rank"]]
    b = oos_df.rename(
        columns={
            "n": "oos_n",
            "median_pct": "oos_median_pct",
            "pct_rank": "oos_pct_rank",
            "win_rate_pct": "oos_win_rate_pct",
            "n_stocks": "oos_n_stocks",
        }
    )[
        [
            "branch_id",
            "oos_n",
            "oos_median_pct",
            "oos_pct_rank",
            "oos_win_rate_pct",
            "oos_n_stocks",
        ]
    ]
    m = a.merge(b, on="branch_id", how="inner")
    mask = (
        (m["is_n"] >= IS_N_GATE)
        & (m["is_pct_rank"] >= IS_PCT_GATE)
        & (m["oos_n"] >= OOS_N_GATE)
        & (m["oos_pct_rank"] >= OOS_PCT_GATE)
    )
    return m.loc[mask].sort_values("oos_median_pct", ascending=False).reset_index(drop=True)


def build_long_details(
    combo_results: list[dict],
) -> pd.DataFrame:
    rows: list[dict] = []
    for cr in combo_results:
        H, beta = cr["hold"], cr["beta"]
        is_df = cr["is_df"]
        oos_df = cr["oos_df"]
        passer_ids = set(cr["passers"]["branch_id"].astype(str)) if len(cr["passers"]) else set()
        is_idx = is_df.set_index("branch_id") if not is_df.empty else pd.DataFrame()
        oos_idx = oos_df.set_index("branch_id") if not oos_df.empty else pd.DataFrame()
        all_ids = set(is_idx.index.astype(str)) | set(oos_idx.index.astype(str))
        for bid in sorted(all_ids):
            is_row = is_idx.loc[bid] if bid in is_idx.index else None
            oos_row = oos_idx.loc[bid] if bid in oos_idx.index else None
            rows.append(
                {
                    "branch_id": bid,
                    "H": H,
                    "beta": beta,
                    "is_median": None if is_row is None else is_row["median_pct"],
                    "is_n": None if is_row is None else int(is_row["n"]),
                    "oos_median": None if oos_row is None else oos_row["median_pct"],
                    "oos_n": None if oos_row is None else int(oos_row["n"]),
                    "is_pct_rank": None if is_row is None else is_row["pct_rank"],
                    "oos_pct_rank": None if oos_row is None else oos_row["pct_rank"],
                    "passed_relative_gate": bid in passer_ids,
                }
            )
    return pd.DataFrame(rows)


def write_summary_md(
    path: Path,
    *,
    protocol: dict,
    coverage: dict,
    combo_results: list[dict],
    h1_verdict: str,
    best_combo: dict | None,
) -> None:
    lines = [
        "# 分點跟單 · 契約網格 + 相對宇宙門檻（Round 2 Fix-B）",
        "",
        "Research only · 事件定義沿用 §2/Round1 原始 D1'（不變）；只換進出契約與決策門檻，"
        "用以隔離契約效應。詳細協議見"
        "`分點跟單全事件研究計畫.md` §10 Fix-B。",
        "",
        "## 凍結協議",
        "",
        f"- 事件 D1'（不變）：`buy_amt ≥ {protocol['abs_floor']/1e8:.2f}億` 且 "
        f"(share≥{protocol['share_floor']:.0%} 或 day-rank≤{protocol['top_k']})",
        f"- episode：{protocol['episode_gap_days']} 曆日同分點同股去重（保留首日；只算一次，跨網格共用）",
        f"- 網格：H∈{{{', '.join(str(h) for h in HOLDS)}}} 交易日 × β∈{{{', '.join(str(b) for b in BETAS)}}}×"
        f"{protocol['benchmark']}（共 {len(HOLDS)*len(BETAS)} 組合）",
        f"- 成本：個股 round-trip {protocol['cost_bps']}bps（每組合每腿）",
        f"- IS：{protocol['is_start']}‥{protocol['is_end']}；OOS：{protocol['oos_start']}‥{protocol['oos_end']}",
        "- 決策門檻（相對宇宙，非絕對 pp）：IS 中位排名 ≥ 該組合 IS 宇宙 P75 且 IS n≥30，"
        "**且** OOS 中位排名 ≥ 該組合 OOS 宇宙 P90 且 OOS n≥15",
        "",
        "## 宇宙／覆蓋",
        "",
        f"- 分點宇宙（沿用 Round1 prefer2y 子集）：**{coverage['n_universe']}**（來源："
        f"{coverage['universe_source']}）",
        f"- D1' 原始列（去重前，跨全窗一次性掃描）：{coverage['n_sig_rows']}",
        f"- episode 後事件（跨全窗，H/β 無關，一次計算共用於 6 組合）：{coverage['n_episode_events']}",
        "",
        "## 逐組合結果",
        "",
        "| H | β | IS 宇宙n(scored) | OOS 宇宙n(scored) | 相對門檻通過家數 | Spearman ρ(IS rank vs OOS rank) |",
        "|--:|--:|-----------------:|------------------:|-----------------:|---------------------------------:|",
    ]
    for cr in combo_results:
        rho_str = "NA" if cr["rho"] is None else f"{cr['rho']:.3f}"
        lines.append(
            f"| {cr['hold']} | {cr['beta']:g} | {cr['is_scored_n']} | {cr['oos_scored_n']} | "
            f"{len(cr['passers'])} | {rho_str} |"
        )

    lines += ["", "## 相對門檻通過分點（逐組合）", ""]
    any_passers = False
    for cr in combo_results:
        if cr["passers"].empty:
            continue
        any_passers = True
        lines += [
            f"### H={cr['hold']} · β={cr['beta']:g}",
            "",
            "| 分點 | IS n | IS 中位% | IS pct_rank | OOS n | OOS 中位% | OOS pct_rank | OOS 勝率% | bootstrap p(median>0) |",
            "|------|-----:|---------:|------------:|------:|----------:|-------------:|----------:|-----------------------:|",
        ]
        for r in cr["passers"].itertuples():
            bp = cr["bootstrap"].get(str(r.branch_id))
            bp_str = "NA" if bp is None else f"{bp:.4f}"
            lines.append(
                f"| {r.branch_name} (`{r.branch_id}`) | {int(r.is_n)} | {r.is_median_pct:+.2f} | "
                f"{r.is_pct_rank:.3f} | {int(r.oos_n)} | {r.oos_median_pct:+.2f} | "
                f"{r.oos_pct_rank:.3f} | {r.oos_win_rate_pct:.1f} | {bp_str} |"
            )
        lines.append("")
    if not any_passers:
        lines += ["（所有 6 組合皆無相對門檻通過分點）", ""]

    # Aggregate bootstrap robustness across all passer-combo rows (honesty check:
    # relative-gate P75/P90 thresholds are near-mechanical on a ~247-branch noisy
    # universe — bootstrap tells us how many survive an actual significance test).
    all_boot_p: list[float] = []
    sig_rows: list[tuple[str, int, float, float]] = []
    for cr in combo_results:
        for r in cr["passers"].itertuples() if len(cr["passers"]) else []:
            p = cr["bootstrap"].get(str(r.branch_id))
            if p is None:
                continue
            all_boot_p.append(p)
            if p < 0.05:
                sig_rows.append((f"{r.branch_name} (`{r.branch_id}`)", cr["hold"], cr["beta"], p))
    n_boot_total = len(all_boot_p)
    n_boot_sig = sum(1 for p in all_boot_p if p < 0.05)
    lines += [
        "## Bootstrap 穩健性檢驗（誠實檢查：相對門檻是否只是機械通過）",
        "",
        f"- 所有組合相對門檻通過的分點－組合列（passer-combo rows）共 **{n_boot_total}** 筆",
        f"- 其中 bootstrap OOS median>0 單尾 p&lt;0.05 者：**{n_boot_sig}** 筆"
        f"（{0 if n_boot_total == 0 else n_boot_sig/n_boot_total*100:.1f}%）",
        "",
    ]
    if sig_rows:
        lines += [
            "| 分點 | H | β | bootstrap p |",
            "|------|--:|--:|------------:|",
        ]
        for name, h, b, p in sig_rows:
            lines.append(f"| {name} | {h} | {b:g} | {p:.4f} |")
        lines.append("")
    else:
        lines += ["（無任何通過分點在 bootstrap 下達 p<0.05）", ""]
    lines += [
        f"約 5% 的通過列本就預期單靠機率達到 p&lt;0.05（多重比較）；"
        f"實測 {0 if n_boot_total == 0 else n_boot_sig/n_boot_total*100:.1f}% "
        + (
            "接近或低於此基準率，顯示相對門檻通過**多屬機械式通過**（P75/P90 定義本身"
            "保證每組合約有一批分點落在門檻之上），非真正可重複的統計邊際。"
            if n_boot_total == 0 or n_boot_sig / max(n_boot_total, 1) <= 0.10
            else "略高於此基準率，但仍應以 bootstrap 通過名單為主，不宜直接採信全部相對門檻通過名單。"
        ),
        "",
    ]

    lines += [
        "## H1-CONTRACT-GRID 判定",
        "",
        f"**Verdict: {h1_verdict}**",
        "",
        "通過標準：至少一組 (H,β) 有 ≥5 家相對門檻通過分點。",
        "",
    ]
    if best_combo is not None:
        lines += [
            f"最佳組合：H={best_combo['hold']}、β={best_combo['beta']:g}、"
            f"通過家數={len(best_combo['passers'])}。",
            "",
        ]

    lines += [
        "## 排名穩定度（Spearman ρ，IS rank vs OOS rank）隨 H/β 變化",
        "",
        "| H | β=1.0 | β=1.25 |",
        "|--:|------:|-------:|",
    ]
    by_h: dict[int, dict[float, float | None]] = defaultdict(dict)
    for cr in combo_results:
        by_h[cr["hold"]][cr["beta"]] = cr["rho"]
    for h in HOLDS:
        row = by_h.get(h, {})
        v10 = row.get(1.0)
        v125 = row.get(1.25)
        s10 = "NA" if v10 is None else f"{v10:.3f}"
        s125 = "NA" if v125 is None else f"{v125:.3f}"
        lines.append(f"| {h} | {s10} | {s125} |")

    lines += [
        "",
        "## 與 Round1 單契約絕對門檻的對比",
        "",
        "| | Round1（H7·β1.25·絕對門檻 IS≥+2%/OOS≥+1%） | Round2 Fix-B（6 組合·相對宇宙門檻 P75/P90） |",
        "|---|---|---|",
        f"| H1 通過家數 | 0 | {sum(len(cr['passers']) for cr in combo_results) if any_passers else 0}"
        f"（{sum(1 for cr in combo_results if len(cr['passers'])>=5)} 組合達 ≥5 家門檻） |",
        "| 事件定義 D1' | 相同 | 相同（凍結，不變） |",
        "| 契約 | 單一 H7/β1.25 | 網格 H∈{7,12,20}×β∈{1.0,1.25} |",
        "| 門檻 | 絕對 pp（IS≥+2%、OOS≥+1%） | 相對宇宙百分位（IS P75、OOS P90） |",
        "",
        "## 結論",
        "",
        (
            "字面上（依「≥5 家相對門檻通過」的預先寫死標準），契約與相對門檻設計變更"
            "**改變**了 Round1「選不到分點」的結論：見上表通過家數。"
            if any_passers
            else "契約與相對門檻設計變更**未改變** Round1「選不到分點」的結論："
        ),
        (
            "但 bootstrap 穩健性檢驗顯示這批「通過」多屬**機械式通過**——P75/P90 定義"
            "本身在 247 家分點的宇宙下幾乎保證每組合都會有一批分點落在門檻之上，"
            f"而其中僅 {n_boot_sig}/{n_boot_total} 筆（約"
            f"{0 if n_boot_total == 0 else n_boot_sig/n_boot_total*100:.1f}%）"
            "在 bootstrap OOS median>0 單尾檢定下 p<0.05，接近純機率下多重比較預期的偽陽性率。"
            "**誠實結論**：相對宇宙門檻確實比絕對 pp 門檻更容易「選到」分點"
            "（證實 Round1 質疑之一——絕對門檻在弱環境下過嚴），但目前尚未有足夠統計證據"
            "支持這些通過分點是真正可重複、可規模化的跟單訊號；不應直接把「相對門檻通過名單」"
            "當候選池餵給 H3 共識層，應以 bootstrap 顯著（p<0.05）名單為優先，"
            "且需更多獨立窗口／穩健性檢定。"
            if any_passers
            else "6 組合下相對門檻通過家數均 <5（見上表），支持 Round1 的方向性結論——"
            "即便放寬為相對宇宙門檻並掃描 H/β 網格，仍選不到穩定跟單分點池；"
            "契約單一性與絕對門檻並非 Round1 拒絕 H1 的主因。"
        ),
        "",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Branch-follow contract grid (H×β) + relative-universe gate"
    )
    ap.add_argument("--probe-start", default=DEFAULT_PROBE[0])
    ap.add_argument("--probe-end", default=DEFAULT_PROBE[1])
    ap.add_argument("--is-start", default=DEFAULT_IS[0])
    ap.add_argument("--is-end", default=DEFAULT_IS[1])
    ap.add_argument("--oos-start", default=DEFAULT_OOS[0])
    ap.add_argument("--oos-end", default=DEFAULT_OOS[1])
    ap.add_argument("--abs-floor", type=float, default=3e7)
    ap.add_argument("--share-floor", type=float, default=0.10)
    ap.add_argument("--top-k", type=int, default=3)
    ap.add_argument("--episode-gap-days", type=int, default=EPISODE_GAP)
    ap.add_argument("--min-window-dates", type=int, default=40)
    ap.add_argument("--prefer-2y-min-dates", type=int, default=400)
    ap.add_argument(
        "--universe",
        choices=("prefer2y", "all_probe"),
        default="prefer2y",
    )
    ap.add_argument("--benchmark", default=BENCHMARK)
    ap.add_argument(
        "--bar-end",
        default=None,
        help="OHLC calendar end (default: oos-end)",
    )
    ap.add_argument(
        "--specialty-pool",
        type=Path,
        default=OUT / "specialty_candidate_pool.csv",
    )
    ap.add_argument("--db", type=Path, default=DB_PATH)
    args = ap.parse_args()

    bar_end = args.bar_end or args.oos_end
    OUT.mkdir(parents=True, exist_ok=True)
    conn = connect_ro(args.db)

    print("loading universe (reuse Round1 prefer2y coverage) …", flush=True)
    pool_cov = load_coverage_from_specialty_pool(args.specialty_pool)
    if len(pool_cov) > 0:
        uni = pool_cov.copy()
        universe_source = f"specialty_candidate_pool.csv tape_* cols (m=2), n={len(uni)}"
    else:
        uni = load_local_universe(
            conn,
            probe_start=args.probe_start,
            probe_end=args.probe_end,
            min_dates=args.min_window_dates,
        )
        uni["n_dates"] = uni["n_probe_dates"]
        universe_source = f"DB probe scan (min_dates>={args.min_window_dates})"
    prefer = uni[uni["n_dates"].fillna(0) >= args.prefer_2y_min_dates].copy()
    scan_uni = prefer if (args.universe == "prefer2y" and len(prefer)) else uni
    ids = scan_uni["id"].astype(str).tolist()
    name_map = dict(zip(scan_uni["id"].astype(str), scan_uni["name"].astype(str)))
    print(f"scan universe n={len(ids)} ({universe_source})", flush=True)

    window_start = min(args.is_start, args.oos_start)
    window_end = max(args.is_end, args.oos_end)

    print("collecting D1' events (frozen, unchanged from Round1) …", flush=True)
    sig = collect_d1_events(
        conn,
        ids=ids,
        start=window_start,
        end=window_end,
        abs_floor=args.abs_floor,
        share_floor=args.share_floor,
        top_k=args.top_k,
    )
    print(f"D1' rows={len(sig):,}", flush=True)
    for bid, nm in (
        sig.groupby("securities_trader_id")["securities_trader"]
        .agg(lambda s: s.dropna().astype(str).iloc[0] if len(s.dropna()) else "")
        .items()
        if not sig.empty
        else []
    ):
        if nm:
            name_map[str(bid)] = str(nm)

    print("episode dedupe (once, H/β-independent) …", flush=True)
    signals = episode_signals(sig, args.episode_gap_days)
    print(f"episode signals={len(signals):,}", flush=True)

    print("building calendar + OHLC + benchmark (cached across grid) …", flush=True)
    cal = build_calendar(conn, window_start, bar_end)
    idx = {d: i for i, d in enumerate(cal)}
    stock_ids = sorted({s["stock_id"] for s in signals})
    ohlc = load_ohlc(conn, stock_ids, window_start, bar_end)
    benchmark_ohlc = load_benchmark_ohlc(conn, args.benchmark, window_start, bar_end)
    conn.close()

    combo_results: list[dict] = []
    for H in HOLDS:
        for beta in BETAS:
            t0 = time.time()
            events = evaluate_combo(
                signals,
                cal=cal,
                idx=idx,
                ohlc=ohlc,
                benchmark_ohlc=benchmark_ohlc,
                hold=H,
                beta=beta,
            )
            is_df = branch_score_table(
                events,
                window_start=args.is_start,
                window_end=args.is_end,
                name_fallback=name_map,
            )
            oos_df = branch_score_table(
                events,
                window_start=args.oos_start,
                window_end=args.oos_end,
                name_fallback=name_map,
            )
            is_df = add_pct_rank(is_df)
            oos_df = add_pct_rank(oos_df)
            passers = combo_relative_passers(is_df, oos_df)

            # OOS event r_adj lists per branch, for bootstrap (only for passers)
            oos_events_by_branch: dict[str, list[float]] = defaultdict(list)
            for e in events:
                if (
                    e.get("r_adj") is not None
                    and args.oos_start <= e["signal_date"] <= args.oos_end
                ):
                    oos_events_by_branch[e["branch_id"]].append(e["r_adj"] * 100.0)
            bootstrap: dict[str, float | None] = {}
            for bid in passers["branch_id"].astype(str) if len(passers) else []:
                bootstrap[bid] = bootstrap_p_median_gt0(oos_events_by_branch.get(bid, []))

            # Spearman: IS rank vs OOS rank, branches scored (n>=1) in both windows
            merged_rank = is_df[is_df["n"] >= 1][["branch_id", "rank"]].merge(
                oos_df[oos_df["n"] >= 1][["branch_id", "rank"]],
                on="branch_id",
                suffixes=("_is", "_oos"),
            )
            rho = (
                spearman_rho(
                    merged_rank["rank_is"].astype(float).tolist(),
                    merged_rank["rank_oos"].astype(float).tolist(),
                )
                if len(merged_rank) >= 3
                else None
            )

            combo_results.append(
                {
                    "hold": H,
                    "beta": beta,
                    "is_df": is_df,
                    "oos_df": oos_df,
                    "is_scored_n": int((is_df["n"] >= 1).sum()) if not is_df.empty else 0,
                    "oos_scored_n": int((oos_df["n"] >= 1).sum()) if not oos_df.empty else 0,
                    "passers": passers,
                    "bootstrap": bootstrap,
                    "rho": rho,
                    "n_rank_overlap": int(len(merged_rank)),
                }
            )
            print(
                f"combo H={H} beta={beta:g}: is_scored={combo_results[-1]['is_scored_n']} "
                f"oos_scored={combo_results[-1]['oos_scored_n']} passers={len(passers)} "
                f"rho={rho} ({time.time()-t0:.1f}s)",
                flush=True,
            )

    details = build_long_details(combo_results)
    details_path = OUT / "branch_follow_contract_grid_details.csv"
    details.to_csv(details_path, index=False)
    print(f"wrote {details_path} ({len(details)} rows)", flush=True)

    passer_counts = [len(cr["passers"]) for cr in combo_results]
    qualifying_combos = [cr for cr in combo_results if len(cr["passers"]) >= 5]
    if qualifying_combos:
        best_combo = max(qualifying_combos, key=lambda cr: len(cr["passers"]))
        h1_verdict = (
            f"PASSED · {len(qualifying_combos)}/{len(combo_results)} 組合 ≥5 家通過 · "
            f"最佳 H={best_combo['hold']} β={best_combo['beta']:g} n={len(best_combo['passers'])}"
        )
    elif max(passer_counts) >= 1:
        best_combo = combo_results[int(np.argmax(passer_counts))]
        h1_verdict = (
            f"THIN · 無組合達 ≥5 家 · 最多 n={max(passer_counts)} "
            f"(H={best_combo['hold']} β={best_combo['beta']:g})"
        )
    else:
        best_combo = None
        h1_verdict = "REJECTED · 6 組合皆 0 家相對門檻通過"

    protocol = {
        "abs_floor": args.abs_floor,
        "share_floor": args.share_floor,
        "top_k": args.top_k,
        "episode_gap_days": args.episode_gap_days,
        "cost_bps": int(COST * 1e4),
        "benchmark": args.benchmark,
        "is_start": args.is_start,
        "is_end": args.is_end,
        "oos_start": args.oos_start,
        "oos_end": args.oos_end,
    }
    coverage = {
        "n_universe": len(ids),
        "universe_source": universe_source,
        "n_sig_rows": int(len(sig)),
        "n_episode_events": int(len(signals)),
    }
    summary_path = OUT / "branch_follow_contract_grid_summary.md"
    write_summary_md(
        summary_path,
        protocol=protocol,
        coverage=coverage,
        combo_results=combo_results,
        h1_verdict=h1_verdict,
        best_combo=best_combo,
    )
    print(f"wrote {summary_path}", flush=True)

    meta = {
        "protocol": protocol,
        "coverage": coverage,
        "h1_contract_grid": {
            "verdict": h1_verdict,
            "per_combo_passers": {
                f"H{cr['hold']}_b{cr['beta']:g}": len(cr["passers"]) for cr in combo_results
            },
            "per_combo_rho": {
                f"H{cr['hold']}_b{cr['beta']:g}": cr["rho"] for cr in combo_results
            },
        },
        "outputs": {
            "details_csv": str(details_path),
            "summary_md": str(summary_path),
        },
    }
    (OUT / "_branch_follow_contract_grid_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print("=== DONE ===", flush=True)
    print(f"H1-CONTRACT-GRID: {h1_verdict}", flush=True)


if __name__ == "__main__":
    main()
