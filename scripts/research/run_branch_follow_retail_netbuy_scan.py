#!/usr/bin/env python3
"""Branch-follow · retail-universe + net-buy Top1 scan (Round 2 · Fix-A).

Research-only. Uses existing local tape (no FinMind fetch). Frozen sub-protocol:
`reports/research/branch-footprint-screen/分點跟單全事件研究計畫.md` §10 Fix-A.

Differences vs Round 1 (`run_branch_follow_all_events_scan.py`, NOT modified here):
  - Universe: excludes branch names containing 自營／期貨／國際證券 (proprietary
    desks / futures desks / international-securities desks) from the same
    prefer-2y coverage base used by Round 1.
  - Event: net_buy_amt = (buy − sell) × close, qualify if net_buy_amt ≥ 0.30億
    AND it is that branch's single largest net_buy_amt that day (rank=1 among
    all that branch's stocks that day). No share/topK-OR clause.

Everything else (episode dedupe 5 calendar days, L1H7, 30bps cost, β=1.25×IX0001,
IS/OOS split, branch score = median r_adj over ALL qualifying events) is
unchanged from §2 of the frozen protocol, and low-level helpers are imported
read-only from the Round 1 script.

Outputs under reports/research/branch-footprint-screen/:
  branch_follow_retail_netbuy_is.csv
  branch_follow_retail_netbuy_oos.csv
  branch_follow_retail_netbuy_summary.md
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import run_branch_follow_all_events_scan as r1  # noqa: E402  (read-only reuse)

OUT = Path("reports/research/branch-footprint-screen")
SOURCE = "finmind"
DB_PATH = Path("data/stocks.db")
HOLD = 7
COST = 0.003
BENCHMARK = "IX0001"
BENCH_BETA = 1.25
EPISODE_GAP = 5
DEFAULT_IS = ("2024-07-01", "2025-12-31")
DEFAULT_OOS = ("2026-01-01", "2026-07-16")
DEFAULT_PROBE = ("2026-05-20", "2026-07-16")

RETAIL_EXCLUDE_RE = re.compile("自營|期貨|國際證券")

# strict main gate (§3 H1, unchanged)
GATE_STRICT = dict(is_med=2.0, is_n=30, oos_med=1.0, oos_n=15)
# diagnostic-only relaxed gate (§10 Fix-A) — reported, never a "pass"
GATE_DIAG = dict(is_med=1.0, is_n=20, oos_med=0.0, oos_n=10)


def is_retail_branch(name: str) -> bool:
    return not RETAIL_EXCLUDE_RE.search(name or "")


def build_retail_universe(
    conn,
    *,
    specialty_pool: Path,
    probe_start: str,
    probe_end: str,
    min_window_dates: int,
    prefer_2y_min_dates: int,
) -> tuple[pd.DataFrame, dict]:
    """Reuse Round 1's prefer-2y coverage base, then drop non-retail names."""
    pool_cov = r1.load_coverage_from_specialty_pool(specialty_pool)
    if len(pool_cov) > 0:
        uni = pool_cov.copy()
        src = "specialty_pool"
    else:
        uni = r1.load_local_universe(
            conn, probe_start=probe_start, probe_end=probe_end, min_dates=min_window_dates
        )
        uni["n_dates"] = uni["n_probe_dates"]
        src = "db_probe_fallback"
    prefer = uni[uni["n_dates"].fillna(0) >= prefer_2y_min_dates].copy()
    scan_uni = prefer if len(prefer) else uni
    excl_mask = scan_uni["name"].apply(lambda nm: not is_retail_branch(str(nm)))
    excluded = scan_uni.loc[excl_mask, ["id", "name"]].copy()
    retail_uni = scan_uni.loc[~excl_mask].copy()
    meta = {
        "universe_source": src,
        "n_prefer2y_base": int(len(scan_uni)),
        "n_excluded_nonretail": int(len(excluded)),
        "excluded_branches": excluded["name"].tolist(),
        "n_retail_universe": int(len(retail_uni)),
    }
    return retail_uni, meta


def significant_netbuys(df: pd.DataFrame, *, abs_floor: float) -> pd.DataFrame:
    """Qualify: net_buy_amt ≥ abs_floor AND rank=1 among that branch-day's stocks."""
    if df.empty:
        return df
    df = df.sort_values(
        ["securities_trader_id", "trade_date", "net_buy_amt"],
        ascending=[True, True, False],
    )
    df["rk"] = df.groupby(["securities_trader_id", "trade_date"])["net_buy_amt"].rank(
        method="first", ascending=False
    )
    mask = (df["net_buy_amt"] >= abs_floor) & (df["rk"] == 1)
    return df.loc[mask].copy()


def collect_netbuy_events(
    conn,
    *,
    ids: list[str],
    start: str,
    end: str,
    abs_floor: float,
) -> pd.DataFrame:
    """Stream months → net-buy Top1 event rows for the retail universe."""
    id_set = set(ids)
    chunks: list[pd.DataFrame] = []
    months = r1.month_starts(start, end)
    print(f"streaming {len(months)} months for {len(ids)} retail branches …", flush=True)
    for mi, (a, b) in enumerate(months, 1):
        t0 = time.time()
        raw = pd.read_sql_query(
            """
            SELECT securities_trader_id, trade_date, stock_id, net,
                   securities_trader
            FROM stock_broker_branch_daily
            WHERE source=?
              AND trade_date >= ? AND trade_date <= ?
              AND stock_id != '__EMPTY__'
              AND length(stock_id)=4
              AND stock_id GLOB '[0-9][0-9][0-9][0-9]'
            """,
            conn,
            params=[SOURCE, a, b],
        )
        if raw.empty:
            print(f"  [{mi}/{len(months)}] {a}..{b} empty", flush=True)
            continue
        raw["securities_trader_id"] = raw["securities_trader_id"].astype(str)
        raw = raw[raw["securities_trader_id"].isin(id_set)]
        if raw.empty:
            print(
                f"  [{mi}/{len(months)}] {a}..{b} no universe rows "
                f"({time.time()-t0:.1f}s)",
                flush=True,
            )
            continue
        closes = r1.load_closes_month(conn, a, b)
        close_df = pd.DataFrame(
            [(sid, d, px) for (sid, d), px in closes.items()],
            columns=["stock_id", "trade_date", "close"],
        )
        raw["stock_id"] = raw["stock_id"].astype(str)
        raw["trade_date"] = raw["trade_date"].astype(str)
        if close_df.empty:
            print(
                f"  [{mi}/{len(months)}] {a}..{b} no closes ({time.time()-t0:.1f}s)",
                flush=True,
            )
            continue
        raw = raw.merge(close_df, on=["stock_id", "trade_date"], how="inner")
        raw["net_buy_amt"] = raw["net"].astype(float) * raw["close"].astype(float)
        raw = raw[np.isfinite(raw["net_buy_amt"])]
        sig = significant_netbuys(raw, abs_floor=abs_floor)
        if not sig.empty:
            chunks.append(
                sig[
                    [
                        "securities_trader_id",
                        "securities_trader",
                        "trade_date",
                        "stock_id",
                        "net_buy_amt",
                        "rk",
                    ]
                ]
            )
        print(
            f"  [{mi}/{len(months)}] {a}..{b} raw={len(raw):,} "
            f"sig={len(sig):,} ({time.time()-t0:.1f}s)",
            flush=True,
        )
    if not chunks:
        return pd.DataFrame(
            columns=[
                "securities_trader_id",
                "securities_trader",
                "trade_date",
                "stock_id",
                "net_buy_amt",
                "rk",
            ]
        )
    return pd.concat(chunks, ignore_index=True)


def summarize_branch_events_ext(events: list[dict]) -> dict:
    """Aggregate evaluable event r_adj for one branch, incl. P75/max diagnostics."""
    xs = [e["r_adj"] for e in events if e.get("r_adj") is not None]
    stocks = {e["stock_id"] for e in events if e.get("r_adj") is not None}
    n = len(xs)
    if n == 0:
        return {
            "n": 0,
            "median_pct": None,
            "mean_pct": None,
            "win_rate_pct": None,
            "n_stocks": 0,
            "p75_pct": None,
            "max_pct": None,
        }
    arr = np.asarray(xs, dtype=float)
    return {
        "n": n,
        "median_pct": float(np.median(arr) * 100.0),
        "mean_pct": float(np.mean(arr) * 100.0),
        "win_rate_pct": float(np.mean(arr > 0) * 100.0),
        "n_stocks": len(stocks),
        "p75_pct": float(np.percentile(arr, 75) * 100.0),
        "max_pct": float(np.max(arr) * 100.0),
    }


def branch_score_table_ext(
    events: list[dict],
    *,
    window_start: str,
    window_end: str,
    name_fallback: dict[str, str],
) -> pd.DataFrame:
    from collections import defaultdict

    by_b: dict[str, list[dict]] = defaultdict(list)
    for e in events:
        if window_start <= e["signal_date"] <= window_end:
            by_b[e["branch_id"]].append(e)
    rows = []
    for bid, evs in by_b.items():
        s = summarize_branch_events_ext(evs)
        rows.append(
            {
                "branch_id": bid,
                "branch_name": name_fallback.get(
                    bid, evs[0].get("branch_name", "") if evs else ""
                ),
                "n": s["n"],
                "median_pct": s["median_pct"],
                "mean_pct": s["mean_pct"],
                "win_rate_pct": s["win_rate_pct"],
                "n_stocks": s["n_stocks"],
                "p75_pct": s["p75_pct"],
                "max_pct": s["max_pct"],
            }
        )
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df = df.sort_values(
        ["median_pct", "n"], ascending=[False, False], na_position="last"
    ).reset_index(drop=True)
    df["rank"] = np.arange(1, len(df) + 1)
    return df


def gate_passers(
    is_df: pd.DataFrame,
    oos_df: pd.DataFrame,
    *,
    is_med: float,
    is_n: int,
    oos_med: float,
    oos_n: int,
) -> pd.DataFrame:
    if is_df.empty or oos_df.empty:
        return pd.DataFrame()
    a = is_df.rename(
        columns={
            "n": "is_n",
            "median_pct": "is_median_pct",
            "mean_pct": "is_mean_pct",
            "win_rate_pct": "is_win_rate_pct",
            "n_stocks": "is_n_stocks",
            "p75_pct": "is_p75_pct",
            "max_pct": "is_max_pct",
        }
    )
    b = oos_df.rename(
        columns={
            "n": "oos_n",
            "median_pct": "oos_median_pct",
            "mean_pct": "oos_mean_pct",
            "win_rate_pct": "oos_win_rate_pct",
            "n_stocks": "oos_n_stocks",
            "p75_pct": "oos_p75_pct",
            "max_pct": "oos_max_pct",
            "rank": "oos_rank",
        }
    )
    m = a.merge(
        b[
            [
                "branch_id",
                "oos_n",
                "oos_median_pct",
                "oos_mean_pct",
                "oos_win_rate_pct",
                "oos_n_stocks",
                "oos_p75_pct",
                "oos_max_pct",
                "oos_rank",
            ]
        ],
        on="branch_id",
        how="inner",
    )
    mask = (
        (m["is_n"] >= is_n)
        & (m["is_median_pct"] >= is_med)
        & (m["oos_n"] >= oos_n)
        & (m["oos_median_pct"] >= oos_med)
    )
    return m.loc[mask].sort_values("oos_median_pct", ascending=False)


def write_summary_md(
    path: Path,
    *,
    protocol: dict,
    universe_meta: dict,
    coverage: dict,
    is_df: pd.DataFrame,
    oos_df: pd.DataFrame,
    strict_passers: pd.DataFrame,
    diag_passers: pd.DataFrame,
    h1_verdict: str,
) -> None:
    univ_is_med = float(is_df["median_pct"].median()) if len(is_df) else None
    univ_oos_med = float(oos_df["median_pct"].median()) if len(oos_df) else None

    lines = [
        "# 分點跟單 · 零售宇宙 + 淨買 Top1 掃描結果（Round 2 · Fix-A）",
        "",
        "Research only · 評估單位 = 分點 **全部**合格事件中位超額（禁止 best `(B,S)` 排序鍵）。",
        "凍結子協議：`分點跟單全事件研究計畫.md` §10 Fix-A（勿改 Round1 腳本/CSV）。",
        "",
        "## 與 Round1（gross-buy）的差異",
        "",
        "| | Round1（`branch_follow_all_events_*`） | Fix-A（本檔） |",
        "|---|---|---|",
        "| 宇宙 | prefer2y 全宇宙（含自營／期貨） | prefer2y **剔除**自營／期貨／國際證券 |",
        "| 事件 | `buy_amt≥0.30億` 且 (share≥10% 或 day-rank≤3) | `net_buy_amt=(buy−sell)×close≥0.30億` 且 **當日該分點 rank=1**（無 share/topK OR） |",
        "| episode／進出／成本／β／IS-OOS／評分 | 相同 | 相同 |",
        "",
        "## 凍結協議",
        "",
        f"- 事件：`net_buy_amt ≥ {protocol['abs_floor']/1e8:.2f}億` 且當日該分點 net_buy_amt rank=1",
        f"- episode：{protocol['episode_gap_days']} 曆日同分點同股去重（保留首日）",
        f"- 進出：L1H{protocol['hold']} · 成本 {protocol['cost_bps']}bps · "
        f"β={protocol['bench_beta']:g}×{protocol['benchmark']}",
        f"- IS：{protocol['is_start']}‥{protocol['is_end']}",
        f"- OOS：{protocol['oos_start']}‥{protocol['oos_end']}",
        "",
        "## 宇宙（零售過濾）",
        "",
        f"- prefer2y 基底（同 Round1 覆蓋來源）：{universe_meta['n_prefer2y_base']}",
        f"- 剔除非零售（自營／期貨／國際證券）：{universe_meta['n_excluded_nonretail']}",
        f"- 零售掃描宇宙：**{universe_meta['n_retail_universe']}**",
        f"- 剔除名單：{'、'.join(universe_meta['excluded_branches']) or '（無）'}",
        "",
        "## 覆蓋",
        "",
        f"- 淨買 Top1 原始列（去重前）：{coverage['n_sig_rows']}",
        f"- episode 後事件：{coverage['n_episode_events']}",
        f"- 可評估（有 L1H7）：{coverage['n_evaluable']}",
        "",
        "## H1 · 可選分點？（主決策，門檻沿用 §3）",
        "",
        f"**Verdict: {h1_verdict}**",
        "",
        "門檻：IS median≥+2% 且 n≥30 **且** OOS median≥+1% 且 n≥15；需 ≥5 家通過才算規模化通過。",
        "",
        f"- 通過家數：**{len(strict_passers)}**",
        "",
    ]
    if len(strict_passers):
        lines += [
            "| 分點 | IS n | IS 中位% | OOS n | OOS 中位% | OOS 勝率% | OOS P75% | OOS max% | OOS 股票數 |",
            "|------|-----:|---------:|------:|----------:|----------:|---------:|---------:|----------:|",
        ]
        for r in strict_passers.itertuples():
            lines.append(
                f"| {r.branch_name} (`{r.branch_id}`) | {int(r.is_n)} | "
                f"{r.is_median_pct:+.2f} | {int(r.oos_n)} | "
                f"{r.oos_median_pct:+.2f} | {r.oos_win_rate_pct:.1f} | "
                f"{r.oos_p75_pct:+.2f} | {r.oos_max_pct:+.2f} | {int(r.oos_n_stocks)} |"
            )
        lines.append("")
    else:
        lines += ["（無通過分點）", ""]

    lines += [
        "### 診斷用寬鬆門檻（非選到，僅報數量）",
        "",
        "門檻：IS 中位≥+1% 且 n≥20 **且** OOS 中位≥0% 且 n≥10。",
        "",
        f"- 診斷通過家數（不當「選到」）：**{len(diag_passers)}**",
        "",
    ]

    lines += [
        "### IS Top10（全事件中位，診斷用）",
        "",
        "| rank | 分點 | n | median% | win% | P75% | max% | stocks |",
        "|-----:|------|--:|--------:|-----:|-----:|-----:|-------:|",
    ]
    for r in is_df.head(10).itertuples():
        med = "NA" if pd.isna(r.median_pct) else f"{r.median_pct:+.2f}"
        win = "NA" if pd.isna(r.win_rate_pct) else f"{r.win_rate_pct:.1f}"
        p75 = "NA" if pd.isna(r.p75_pct) else f"{r.p75_pct:+.2f}"
        mx = "NA" if pd.isna(r.max_pct) else f"{r.max_pct:+.2f}"
        lines.append(
            f"| {int(r.rank)} | {r.branch_name} (`{r.branch_id}`) | "
            f"{int(r.n)} | {med} | {win} | {p75} | {mx} | {int(r.n_stocks)} |"
        )
    lines += [
        "",
        "### OOS Top10（全事件中位，主結論用）",
        "",
        "| rank | 分點 | n | median% | win% | P75% | max% | stocks |",
        "|-----:|------|--:|--------:|-----:|-----:|-----:|-------:|",
    ]
    for r in oos_df.head(10).itertuples():
        med = "NA" if pd.isna(r.median_pct) else f"{r.median_pct:+.2f}"
        win = "NA" if pd.isna(r.win_rate_pct) else f"{r.win_rate_pct:.1f}"
        p75 = "NA" if pd.isna(r.p75_pct) else f"{r.p75_pct:+.2f}"
        mx = "NA" if pd.isna(r.max_pct) else f"{r.max_pct:+.2f}"
        lines.append(
            f"| {int(r.rank)} | {r.branch_name} (`{r.branch_id}`) | "
            f"{int(r.n)} | {med} | {win} | {p75} | {mx} | {int(r.n_stocks)} |"
        )

    lines += [
        "",
        "## 宇宙中位（分點中位之中位）",
        "",
        f"- IS：{'NA' if univ_is_med is None else f'{univ_is_med:+.2f}%'}",
        f"- OOS：{'NA' if univ_oos_med is None else f'{univ_oos_med:+.2f}%'}",
        "",
        "## 對照 Round1（gross-buy，混合宇宙）結論",
        "",
        "Round1（`branch_follow_all_events_summary.md`）：H1 = **REJECTED**（n_pass=0），"
        "宇宙中位 IS −0.60%／OOS −2.16%；唯一過 IS 門檻的 960T（富邦自營）OOS 翻負 −3.23%。",
        "",
        (
            f"Fix-A（零售 + 淨買 Top1）：H1 = **{'PASSED' if len(strict_passers) >= 5 else ('THIN' if len(strict_passers) >= 1 else 'REJECTED')}**"
            f"（n_pass={len(strict_passers)}），宇宙中位 IS "
            f"{'NA' if univ_is_med is None else f'{univ_is_med:+.2f}%'}／OOS "
            f"{'NA' if univ_oos_med is None else f'{univ_oos_med:+.2f}%'}。"
        ),
        (
            "→ 剔除自營/期貨/國際證券並改用淨買 rank=1 事件"
            + (
                "**改變**了 Round1 的『選不到』結論。"
                if len(strict_passers) >= 1
                else "**未改變** Round1 的『選不到』結論；已排除宇宙污染／毛買噪音兩項嫌疑，仍非唯一原因。"
            )
        ),
        "",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Branch-follow retail-universe + net-buy Top1 scan (Fix-A)"
    )
    ap.add_argument("--probe-start", default=DEFAULT_PROBE[0])
    ap.add_argument("--probe-end", default=DEFAULT_PROBE[1])
    ap.add_argument("--is-start", default=DEFAULT_IS[0])
    ap.add_argument("--is-end", default=DEFAULT_IS[1])
    ap.add_argument("--oos-start", default=DEFAULT_OOS[0])
    ap.add_argument("--oos-end", default=DEFAULT_OOS[1])
    ap.add_argument("--abs-floor", type=float, default=3e7)
    ap.add_argument("--episode-gap-days", type=int, default=EPISODE_GAP)
    ap.add_argument("--min-window-dates", type=int, default=40)
    ap.add_argument("--prefer-2y-min-dates", type=int, default=400)
    ap.add_argument("--benchmark", default=BENCHMARK)
    ap.add_argument("--bench-beta", type=float, default=BENCH_BETA)
    ap.add_argument("--bar-end", default=None)
    ap.add_argument(
        "--specialty-pool", type=Path, default=OUT / "specialty_candidate_pool.csv"
    )
    ap.add_argument("--db", type=Path, default=DB_PATH)
    args = ap.parse_args()

    bar_end = args.bar_end or args.oos_end
    OUT.mkdir(parents=True, exist_ok=True)
    conn = r1.connect_ro(args.db)

    print("building retail universe (prefer2y base minus 自營/期貨/國際證券) …", flush=True)
    retail_uni, universe_meta = build_retail_universe(
        conn,
        specialty_pool=args.specialty_pool,
        probe_start=args.probe_start,
        probe_end=args.probe_end,
        min_window_dates=args.min_window_dates,
        prefer_2y_min_dates=args.prefer_2y_min_dates,
    )
    print(f"retail universe: {universe_meta}", flush=True)

    ids = retail_uni["id"].astype(str).tolist()
    name_map = dict(zip(retail_uni["id"].astype(str), retail_uni["name"].astype(str)))

    window_start = min(args.is_start, args.oos_start)
    window_end = max(args.is_end, args.oos_end)
    sig = collect_netbuy_events(
        conn, ids=ids, start=window_start, end=window_end, abs_floor=args.abs_floor
    )
    print(f"net-buy Top1 rows={len(sig):,}", flush=True)

    if not sig.empty:
        for bid, nm in (
            sig.groupby("securities_trader_id")["securities_trader"]
            .agg(lambda s: s.dropna().astype(str).iloc[0] if len(s.dropna()) else "")
            .items()
        ):
            if nm:
                name_map[str(bid)] = str(nm)

    print("building calendar + OHLC + benchmark …", flush=True)
    cal = r1.build_calendar(conn, window_start, bar_end)
    idx = {d: i for i, d in enumerate(cal)}
    stock_ids = sorted(sig["stock_id"].astype(str).unique().tolist()) if len(sig) else []
    ohlc = r1.load_ohlc(conn, stock_ids, window_start, bar_end)
    benchmark_ohlc = r1.load_benchmark_ohlc(conn, args.benchmark, window_start, bar_end)
    conn.close()

    print("episode dedupe + L1H7 …", flush=True)
    events = r1.evaluate_events(
        sig,
        cal=cal,
        idx=idx,
        ohlc=ohlc,
        benchmark_ohlc=benchmark_ohlc,
        bench_beta=args.bench_beta,
        episode_gap=args.episode_gap_days,
    )
    n_eval = sum(1 for e in events if e.get("r_adj") is not None)
    print(f"episode events={len(events):,} evaluable={n_eval:,}", flush=True)

    coverage = {
        "n_sig_rows": int(len(sig)),
        "n_episode_events": int(len(events)),
        "n_evaluable": int(n_eval),
    }

    is_df = branch_score_table_ext(
        events, window_start=args.is_start, window_end=args.is_end, name_fallback=name_map
    )
    oos_df = branch_score_table_ext(
        events, window_start=args.oos_start, window_end=args.oos_end, name_fallback=name_map
    )
    is_path = OUT / "branch_follow_retail_netbuy_is.csv"
    oos_path = OUT / "branch_follow_retail_netbuy_oos.csv"
    is_df.to_csv(is_path, index=False)
    oos_df.to_csv(oos_path, index=False)
    print(f"wrote {is_path} ({len(is_df)} rows)", flush=True)
    print(f"wrote {oos_path} ({len(oos_df)} rows)", flush=True)

    strict_passers = gate_passers(is_df, oos_df, **GATE_STRICT)
    diag_passers = gate_passers(is_df, oos_df, **GATE_DIAG)
    n_pass = len(strict_passers)
    if n_pass >= 5:
        h1_verdict = f"PASSED (≥5) · n={n_pass}"
    elif n_pass >= 1:
        h1_verdict = f"THIN (1–4) · n={n_pass} · 不宣稱可規模化"
    else:
        h1_verdict = "REJECTED · n=0 · 選不到穩定跟單分點"

    protocol = {
        "abs_floor": args.abs_floor,
        "episode_gap_days": args.episode_gap_days,
        "hold": HOLD,
        "cost_bps": int(COST * 1e4),
        "benchmark": args.benchmark,
        "bench_beta": args.bench_beta,
        "is_start": args.is_start,
        "is_end": args.is_end,
        "oos_start": args.oos_start,
        "oos_end": args.oos_end,
    }
    summary_path = OUT / "branch_follow_retail_netbuy_summary.md"
    write_summary_md(
        summary_path,
        protocol=protocol,
        universe_meta=universe_meta,
        coverage=coverage,
        is_df=is_df,
        oos_df=oos_df,
        strict_passers=strict_passers,
        diag_passers=diag_passers,
        h1_verdict=h1_verdict,
    )
    print(f"wrote {summary_path}", flush=True)

    meta = {
        "protocol": protocol,
        "universe": universe_meta,
        "coverage": coverage,
        "h1_strict": {"verdict": h1_verdict, "n_pass": n_pass},
        "h1_diagnostic_relaxed": {"n_pass": int(len(diag_passers)), "gate": GATE_DIAG},
        "outputs": {
            "is_csv": str(is_path),
            "oos_csv": str(oos_path),
            "summary_md": str(summary_path),
        },
    }
    (OUT / "_branch_follow_retail_netbuy_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print("=== DONE ===", flush=True)
    print(f"H1 strict: {h1_verdict}", flush=True)
    print(f"H1 diagnostic-relaxed n_pass: {len(diag_passers)}", flush=True)


if __name__ == "__main__":
    main()
