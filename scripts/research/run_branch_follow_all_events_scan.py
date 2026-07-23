#!/usr/bin/env python3
"""Branch-follow · all-events scan (anti best-pair).

Research-only. Uses existing local tape (no FinMind fetch).

Frozen protocol (D1' / L1H7 / β=1.25):
  buy_amt = buy × close ≥ abs_floor (default 0.30億)
  AND (buy_share ≥ share_floor OR day-rank(buy_amt) ≤ top_k)
  episode: same branch+stock, 5 calendar days, keep first
  return: T+1 open → hold 7 trading days close, stock cost 30bps
  excess: r_adj = r_stock − bench_beta × r_IX0001
  branch score = median r_adj over ALL evaluable events (NOT best (B,S))

IS / OOS split by signal date. Outputs under
reports/research/branch-footprint-screen/:
  branch_follow_all_events_is.csv
  branch_follow_all_events_oos.csv
  branch_follow_all_events_summary.md
  branch_follow_consensus_overlay.md  (only if H1 passers ≥1)
  _branch_follow_coverage.json
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import time
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

OUT = Path("reports/research/branch-footprint-screen")
SOURCE = "finmind"
DB_PATH = Path("data/stocks.db")
HOLD = 7
COST = 0.003  # 30 bps round-trip
BENCHMARK = "IX0001"
DEFAULT_IS = ("2024-07-01", "2025-12-31")
DEFAULT_OOS = ("2026-01-01", "2026-07-16")
DEFAULT_PROBE = ("2026-05-20", "2026-07-16")
EPISODE_GAP = 5


def connect_ro(db_path: Path = DB_PATH) -> sqlite3.Connection:
    p = db_path.resolve()
    conn = sqlite3.connect(f"file:{p}?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def month_starts(start: str, end: str) -> list[tuple[str, str]]:
    """Inclusive calendar-month slices clipped to [start, end]."""
    cur = pd.Timestamp(start).to_period("M").to_timestamp()
    end_ts = pd.Timestamp(end)
    out: list[tuple[str, str]] = []
    while cur <= end_ts:
        m_end = (cur + pd.offsets.MonthEnd(0)).normalize()
        a = max(cur, pd.Timestamp(start)).strftime("%Y-%m-%d")
        b = min(m_end, end_ts).strftime("%Y-%m-%d")
        out.append((a, b))
        cur = (cur + pd.offsets.MonthBegin(1)).normalize()
        if cur.day != 1:
            cur = cur.to_period("M").to_timestamp()
    return out


def load_local_universe(
    conn: sqlite3.Connection,
    *,
    probe_start: str,
    probe_end: str,
    min_dates: int,
) -> pd.DataFrame:
    df = pd.read_sql_query(
        """
        SELECT securities_trader_id AS id,
               MAX(securities_trader) AS name,
               COUNT(DISTINCT trade_date) AS n_probe_dates
        FROM stock_broker_branch_daily
        WHERE source=?
          AND stock_id != '__EMPTY__'
          AND trade_date >= ? AND trade_date <= ?
        GROUP BY securities_trader_id
        HAVING COUNT(DISTINCT trade_date) >= ?
        """,
        conn,
        params=[SOURCE, probe_start, probe_end, min_dates],
    )
    df["id"] = df["id"].astype(str)
    df["name"] = df["name"].fillna("")
    return df


def load_coverage_from_specialty_pool(path: Path) -> pd.DataFrame:
    """Reuse specialty_candidate_pool tape_* columns (avoids full-table GROUP BY)."""
    if not path.exists():
        return pd.DataFrame(columns=["id", "name", "n_dates", "min_d", "max_d", "n_probe_dates"])
    df = pd.read_csv(path)
    df = df[df["m_threshold"] == 2].copy()
    df["id"] = df["branch_id"].astype(str)
    out = (
        df.groupby("id", as_index=False)
        .agg(
            name=("branch_name", "first"),
            n_dates=("tape_n_dates", "max"),
            min_d=("tape_min", "min"),
            max_d=("tape_max", "max"),
        )
    )
    out["n_probe_dates"] = out["n_dates"]  # placeholder; overwritten if probe scan runs
    return out


def significant_buys(
    df: pd.DataFrame,
    *,
    abs_floor: float,
    share_floor: float,
    top_k: int,
) -> pd.DataFrame:
    if df.empty:
        return df
    df = df.sort_values(
        ["securities_trader_id", "trade_date", "buy_amt"],
        ascending=[True, True, False],
    )
    df["day_buy"] = df.groupby(["securities_trader_id", "trade_date"])[
        "buy_amt"
    ].transform("sum")
    df["buy_share"] = df["buy_amt"] / df["day_buy"].replace(0, np.nan)
    df["rk"] = df.groupby(["securities_trader_id", "trade_date"])["buy_amt"].rank(
        method="first", ascending=False
    )
    mask = (df["buy_amt"] >= abs_floor) & (
        (df["buy_share"] >= share_floor) | (df["rk"] <= top_k)
    )
    return df.loc[mask].copy()


def episode_first_dates(
    dates: list[str], gap_days: int = EPISODE_GAP
) -> list[str]:
    """Keep first signal of each episode; gap > gap_days starts a new episode."""
    if not dates:
        return []
    ds = sorted(set(dates))
    kept = [ds[0]]
    prev = pd.Timestamp(ds[0])
    for d in ds[1:]:
        cur = pd.Timestamp(d)
        if (cur - prev).days > gap_days:
            kept.append(d)
            prev = cur
        # else same episode → drop
    return kept


def build_calendar(conn: sqlite3.Connection, start: str, end: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT trade_date FROM stock_daily_bars
        WHERE source=? AND stock_id='2330'
          AND trade_date >= ? AND trade_date <= ?
        ORDER BY trade_date
        """,
        (SOURCE, start, end),
    ).fetchall()
    return [str(r[0]) for r in rows]


def load_ohlc(
    conn: sqlite3.Connection,
    stock_ids: list[str],
    start: str,
    end: str,
) -> dict[tuple[str, str], tuple[float, float]]:
    if not stock_ids:
        return {}
    out: dict[tuple[str, str], tuple[float, float]] = {}
    # chunk stock ids to keep SQL reasonable
    chunk = 400
    for i in range(0, len(stock_ids), chunk):
        part = stock_ids[i : i + chunk]
        ph = ",".join("?" * len(part))
        rows = conn.execute(
            f"""
            SELECT stock_id, trade_date, open, close
            FROM stock_daily_bars
            WHERE source=? AND stock_id IN ({ph})
              AND trade_date >= ? AND trade_date <= ?
              AND open > 0 AND close > 0
            """,
            [SOURCE, *part, start, end],
        ).fetchall()
        for a, b, c, d in rows:
            out[(str(a), str(b))] = (float(c), float(d))
    return out


def load_closes_month(
    conn: sqlite3.Connection, start: str, end: str
) -> dict[tuple[str, str], float]:
    rows = conn.execute(
        """
        SELECT stock_id, trade_date, close
        FROM stock_daily_bars
        WHERE source=? AND trade_date >= ? AND trade_date <= ?
          AND close > 0
          AND length(stock_id)=4
          AND stock_id GLOB '[0-9][0-9][0-9][0-9]'
        """,
        (SOURCE, start, end),
    ).fetchall()
    return {(str(a), str(b)): float(c) for a, b, c in rows}


def load_benchmark_ohlc(
    conn: sqlite3.Connection, code: str, start: str, end: str
) -> dict[str, tuple[float, float]]:
    rows = conn.execute(
        """
        SELECT date, open, close
        FROM daily_bars
        WHERE code=? AND date>=? AND date<=?
          AND open>0 AND close>0
        ORDER BY date,
          CASE source
            WHEN 'yahoo' THEN 0
            WHEN 'tej' THEN 1
            WHEN 'finmind' THEN 2
            ELSE 3
          END
        """,
        (code, start, end),
    ).fetchall()
    out: dict[str, tuple[float, float]] = {}
    for d, op, cl in rows:
        out.setdefault(str(d), (float(op), float(cl)))
    if not out:
        raise RuntimeError(f"local benchmark returned no rows: {code}")
    return out


def l1h7_one(
    sig: str,
    stock_id: str,
    *,
    cal: list[str],
    idx: dict[str, int],
    ohlc: dict[tuple[str, str], tuple[float, float]],
    benchmark_ohlc: dict[str, tuple[float, float]],
    bench_beta: float,
    hold: int = HOLD,
    cost: float = COST,
) -> tuple[float, float, float] | None:
    i = idx.get(sig)
    if i is None:
        return None
    entry_i = i + 1
    exit_i = entry_i + (hold - 1)
    if exit_i >= len(cal):
        return None
    entry_d = cal[entry_i]
    exit_d = cal[exit_i]
    eo = ohlc.get((stock_id, entry_d))
    xc = ohlc.get((stock_id, exit_d))
    be = benchmark_ohlc.get(entry_d)
    bx = benchmark_ohlc.get(exit_d)
    if eo is None or xc is None or be is None or bx is None:
        return None
    entry_px, _ = eo
    _, exit_px = xc
    b_entry, _ = be
    _, b_exit = bx
    if entry_px <= 0 or exit_px <= 0 or b_entry <= 0 or b_exit <= 0:
        return None
    stock_ret = exit_px / entry_px - 1.0 - cost
    bench_ret = b_exit / b_entry - 1.0
    return stock_ret, bench_ret, stock_ret - float(bench_beta) * bench_ret


def summarize_branch_events(
    events: list[dict],
) -> dict:
    """Aggregate evaluable event r_adj for one branch."""
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
        }
    arr = np.asarray(xs, dtype=float)
    return {
        "n": n,
        "median_pct": float(np.median(arr) * 100.0),
        "mean_pct": float(np.mean(arr) * 100.0),
        "win_rate_pct": float(np.mean(arr > 0) * 100.0),
        "n_stocks": len(stocks),
    }


def spearman_rho(x: list[float], y: list[float]) -> float | None:
    if len(x) < 3:
        return None
    a = pd.Series(x, dtype=float)
    b = pd.Series(y, dtype=float)
    rho = a.corr(b, method="spearman")
    if rho is None or (isinstance(rho, float) and math.isnan(rho)):
        return None
    return float(rho)


def load_specialty_p1_ranks(path: Path) -> pd.DataFrame:
    """Short-window best-pair ranks from specialty_candidate_pool (m=2)."""
    df = pd.read_csv(path)
    df = df[df["m_threshold"] == 2].copy()
    df["branch_id"] = df["branch_id"].astype(str)
    # Prefer rows with a numeric l1h7_rank (scored pairs)
    df = df[df["l1h7_rank"].notna()].copy()
    df["l1h7_rank"] = df["l1h7_rank"].astype(float)
    # one row per branch (pool already branch-level best)
    df = df.sort_values("l1h7_rank").drop_duplicates("branch_id", keep="first")
    return df[["branch_id", "branch_name", "priority", "l1h7_rank", "best_excess_l1h7_mean_pct"]]


def density_matched_control(
    oos_df: pd.DataFrame,
    passer_ids: list[str],
    *,
    tol: float = 0.30,
) -> dict:
    """Compare passer OOS medians vs density-matched non-passers (similar n)."""
    scored = oos_df[oos_df["n"] >= 1].copy()
    if scored.empty or not passer_ids:
        return {"feasible": False, "reason": "no scored branches or no passers"}
    passers = scored[scored["branch_id"].isin(passer_ids)]
    others = scored[~scored["branch_id"].isin(passer_ids)]
    matched_medians: list[float] = []
    for _, row in passers.iterrows():
        n = int(row["n"])
        lo, hi = n * (1 - tol), n * (1 + tol)
        pool = others[(others["n"] >= lo) & (others["n"] <= hi)]
        if pool.empty:
            continue
        matched_medians.append(float(pool["median_pct"].median()))
    univ_med = float(scored["median_pct"].median())
    passer_med = float(passers["median_pct"].median()) if len(passers) else None
    ctrl_med = float(np.median(matched_medians)) if matched_medians else None
    out = {
        "feasible": True,
        "universe_median_pct": univ_med,
        "passer_median_of_medians_pct": passer_med,
        "density_matched_control_median_pct": ctrl_med,
        "n_passers": int(len(passers)),
        "n_density_matches": int(len(matched_medians)),
        "edge_vs_universe_pp": (
            None if passer_med is None else passer_med - univ_med
        ),
        "edge_vs_density_pp": (
            None
            if passer_med is None or ctrl_med is None
            else passer_med - ctrl_med
        ),
    }
    return out


def collect_d1_events(
    conn: sqlite3.Connection,
    *,
    ids: list[str],
    start: str,
    end: str,
    abs_floor: float,
    share_floor: float,
    top_k: int,
) -> pd.DataFrame:
    """Stream months → D1' event rows for universe ids."""
    id_set = set(ids)
    chunks: list[pd.DataFrame] = []
    months = month_starts(start, end)
    print(f"streaming {len(months)} months for {len(ids)} branches …", flush=True)
    for mi, (a, b) in enumerate(months, 1):
        t0 = time.time()
        raw = pd.read_sql_query(
            """
            SELECT securities_trader_id, trade_date, stock_id, buy,
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
        closes = load_closes_month(conn, a, b)
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
        raw["buy_amt"] = raw["buy"].astype(float) * raw["close"].astype(float)
        raw = raw[np.isfinite(raw["buy_amt"]) & (raw["buy_amt"] > 0)]
        sig = significant_buys(
            raw,
            abs_floor=abs_floor,
            share_floor=share_floor,
            top_k=top_k,
        )
        if not sig.empty:
            chunks.append(
                sig[
                    [
                        "securities_trader_id",
                        "securities_trader",
                        "trade_date",
                        "stock_id",
                        "buy_amt",
                        "buy_share",
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
                "buy_amt",
                "buy_share",
                "rk",
            ]
        )
    return pd.concat(chunks, ignore_index=True)


def evaluate_events(
    sig: pd.DataFrame,
    *,
    cal: list[str],
    idx: dict[str, int],
    ohlc: dict[tuple[str, str], tuple[float, float]],
    benchmark_ohlc: dict[str, tuple[float, float]],
    bench_beta: float,
    episode_gap: int,
) -> list[dict]:
    """Episode-dedupe per (B,S) then L1H7 excess for each kept signal."""
    events: list[dict] = []
    if sig.empty:
        return events
    name_map = (
        sig.groupby("securities_trader_id")["securities_trader"]
        .agg(lambda s: s.dropna().astype(str).iloc[0] if len(s.dropna()) else "")
        .to_dict()
    )
    for (bid, sid), g in sig.groupby(
        ["securities_trader_id", "stock_id"], sort=False
    ):
        dates = episode_first_dates(
            g["trade_date"].astype(str).tolist(), episode_gap
        )
        for d in dates:
            out = l1h7_one(
                d,
                str(sid),
                cal=cal,
                idx=idx,
                ohlc=ohlc,
                benchmark_ohlc=benchmark_ohlc,
                bench_beta=bench_beta,
            )
            row = {
                "branch_id": str(bid),
                "branch_name": str(name_map.get(bid, "")),
                "stock_id": str(sid),
                "signal_date": d,
                "r_stock": None if out is None else out[0],
                "r_bench": None if out is None else out[1],
                "r_adj": None if out is None else out[2],
            }
            events.append(row)
    return events


def branch_score_table(
    events: list[dict],
    *,
    window_start: str,
    window_end: str,
    name_fallback: dict[str, str],
) -> pd.DataFrame:
    by_b: dict[str, list[dict]] = defaultdict(list)
    for e in events:
        if window_start <= e["signal_date"] <= window_end:
            by_b[e["branch_id"]].append(e)
    rows = []
    for bid, evs in by_b.items():
        s = summarize_branch_events(evs)
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


def h1_passers(
    is_df: pd.DataFrame,
    oos_df: pd.DataFrame,
    *,
    is_med: float = 2.0,
    is_n: int = 30,
    oos_med: float = 1.0,
    oos_n: int = 15,
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
        }
    )
    b = oos_df.rename(
        columns={
            "n": "oos_n",
            "median_pct": "oos_median_pct",
            "mean_pct": "oos_mean_pct",
            "win_rate_pct": "oos_win_rate_pct",
            "n_stocks": "oos_n_stocks",
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


def consensus_overlay(
    events: list[dict],
    passer_ids: list[str],
    *,
    oos_start: str,
    oos_end: str,
    ks: tuple[int, ...] = (3, 5),
) -> pd.DataFrame:
    """Same-day same-stock consensus among H1 passer branches (OOS signals)."""
    passer_set = set(passer_ids)
    oos_ev = [
        e
        for e in events
        if e["branch_id"] in passer_set
        and oos_start <= e["signal_date"] <= oos_end
        and e.get("r_adj") is not None
    ]
    rows = []
    # solo arm
    solo = [e["r_adj"] for e in oos_ev]
    rows.append(_arm_row("solo_all_events", solo, n_signals=len(solo)))

    # group by (date, stock)
    by_ds: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for e in oos_ev:
        by_ds[(e["signal_date"], e["stock_id"])].append(e)

    for k in ks:
        # consensus day: ≥K distinct passer branches
        cons_rets: list[float] = []
        for (_d, _s), group in by_ds.items():
            bids = {g["branch_id"] for g in group}
            if len(bids) >= k:
                # one return per (date, stock): median across triggering branches
                cons_rets.append(float(np.median([g["r_adj"] for g in group])))
        rows.append(
            _arm_row(f"consensus_k{k}_median", cons_rets, n_signals=len(cons_rets))
        )
        # weighted: each triggering branch event kept
        weighted = [
            g["r_adj"]
            for (_d, _s), group in by_ds.items()
            for g in group
            if len({x["branch_id"] for x in group}) >= k
        ]
        rows.append(
            _arm_row(f"consensus_k{k}_weighted", weighted, n_signals=len(weighted))
        )
    return pd.DataFrame(rows)


def _arm_row(name: str, xs: list[float], *, n_signals: int) -> dict:
    if not xs:
        return {
            "arm": name,
            "n": 0,
            "median_pct": None,
            "mean_pct": None,
            "win_rate_pct": None,
            "n_signals": n_signals,
        }
    arr = np.asarray(xs, dtype=float)
    return {
        "arm": name,
        "n": int(len(arr)),
        "median_pct": float(np.median(arr) * 100.0),
        "mean_pct": float(np.mean(arr) * 100.0),
        "win_rate_pct": float(np.mean(arr > 0) * 100.0),
        "n_signals": n_signals,
    }


def write_summary_md(
    path: Path,
    *,
    protocol: dict,
    coverage: dict,
    is_df: pd.DataFrame,
    oos_df: pd.DataFrame,
    passers: pd.DataFrame,
    h2: dict,
    h4: dict,
    h1_verdict: str,
    h2_verdict: str,
    h4_verdict: str,
) -> None:
    lines = [
        "# 分點跟單 · 全事件掃描結果",
        "",
        "Research only · 評估單位 = 分點 **全部**合格事件中位超額（禁止 best `(B,S)` 排序鍵）。",
        "",
        "## 凍結協議",
        "",
        f"- 事件 D1'：`buy_amt ≥ {protocol['abs_floor']/1e8:.2f}億` 且 "
        f"(share≥{protocol['share_floor']:.0%} 或 day-rank≤{protocol['top_k']})",
        f"- episode：{protocol['episode_gap_days']} 曆日同分點同股去重（保留首日）",
        f"- 進出：L1H{protocol['hold']} · 成本 {protocol['cost_bps']}bps · "
        f"β={protocol['bench_beta']:g}×{protocol['benchmark']}",
        f"- IS：{protocol['is_start']}‥{protocol['is_end']}",
        f"- OOS：{protocol['oos_start']}‥{protocol['oos_end']}",
        f"- 宇宙：probe usable≥{protocol['min_window_dates']} 日；"
        f"主分析 2y 優先（n_dates≥{protocol['prefer_2y_min_dates']}）",
        "",
        "## 覆蓋",
        "",
        f"- probe 宇宙分點：{coverage['n_universe']}",
        f"- 2y 優先子集：{coverage['n_prefer_2y']}",
        f"- 短窗缺口（僅 probe、缺 2y）：{coverage['n_short_only']}",
        f"- D1' 原始列（去重前）：{coverage['n_sig_rows']}",
        f"- episode 後事件：{coverage['n_episode_events']}",
        f"- 可評估（有 L1H7）：{coverage['n_evaluable']}",
        "",
        "## H1 · 可選分點？",
        "",
        f"**Verdict: {h1_verdict}**",
        "",
        "門檻：IS median≥+2% 且 n≥30 **且** OOS median≥+1% 且 n≥15；需 ≥5 家通過才算規模化通過。",
        "",
        f"- 通過家數：**{len(passers)}**",
        "",
    ]
    if len(passers):
        lines += [
            "| 分點 | IS n | IS 中位% | OOS n | OOS 中位% | OOS 勝率% | OOS 股票數 |",
            "|------|-----:|---------:|------:|----------:|----------:|----------:|",
        ]
        for r in passers.itertuples():
            lines.append(
                f"| {r.branch_name} (`{r.branch_id}`) | {int(r.is_n)} | "
                f"{r.is_median_pct:+.2f} | {int(r.oos_n)} | "
                f"{r.oos_median_pct:+.2f} | {r.oos_win_rate_pct:.1f} | "
                f"{int(r.oos_n_stocks)} |"
            )
        lines.append("")
    else:
        lines += ["（無通過分點）", ""]

    lines += [
        "### IS Top10（全事件中位，診斷用）",
        "",
        "| rank | 分點 | n | median% | win% | stocks |",
        "|-----:|------|--:|--------:|-----:|-------:|",
    ]
    for r in is_df.head(10).itertuples():
        med = "NA" if pd.isna(r.median_pct) else f"{r.median_pct:+.2f}"
        win = "NA" if pd.isna(r.win_rate_pct) else f"{r.win_rate_pct:.1f}"
        lines.append(
            f"| {int(r.rank)} | {r.branch_name} (`{r.branch_id}`) | "
            f"{int(r.n)} | {med} | {win} | {int(r.n_stocks)} |"
        )
    lines += [
        "",
        "### OOS Top10（全事件中位，主結論用）",
        "",
        "| rank | 分點 | n | median% | win% | stocks |",
        "|-----:|------|--:|--------:|-----:|-------:|",
    ]
    for r in oos_df.head(10).itertuples():
        med = "NA" if pd.isna(r.median_pct) else f"{r.median_pct:+.2f}"
        win = "NA" if pd.isna(r.win_rate_pct) else f"{r.win_rate_pct:.1f}"
        lines.append(
            f"| {int(r.rank)} | {r.branch_name} (`{r.branch_id}`) | "
            f"{int(r.n)} | {med} | {win} | {int(r.n_stocks)} |"
        )

    lines += [
        "",
        "## H2 · 相對宇宙／密度對照",
        "",
        f"**Verdict: {h2_verdict}**",
        "",
        "```json",
        json.dumps(h2, ensure_ascii=False, indent=2),
        "```",
        "",
        "## H4 · 短窗 best-pair P1 vs 全事件 OOS 排名",
        "",
        f"**Verdict: {h4_verdict}**",
        "",
        f"- Spearman ρ = {h4.get('spearman_rho')}",
        f"- n overlapping ranked branches = {h4.get('n')}",
        f"- 通過標準：ρ < 0.3（低相關 → 短窗 P1 不宜當選人鍵）",
        "",
        "## 負結果附錄（為何不用短窗 Top10）",
        "",
        "短窗 best `(B,S)`（如雷虎／富鼎故事）是**事後諸葛**選股；本掃描以分點"
        "全部出手中位超額為唯一選人分數。若 H4 ρ 低，則形式化支持「短窗 P1 ≠ "
        "可跟單分點」。若 H1=0，結論為：以現行 D1'+L1H7+β1.25，**選不到**穩定"
        "跟單分點，停止加規則曲線擬合。",
        "",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Branch-follow all-events scan (median r_adj, β=1.25)"
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
    ap.add_argument(
        "--prefer-2y-min-dates",
        type=int,
        default=400,
        help="prefer branches with ≥ this many tape dates for main scan",
    )
    ap.add_argument(
        "--universe",
        choices=("prefer2y", "all_probe"),
        default="prefer2y",
        help="prefer2y: only long-coverage branches; all_probe: all local40",
    )
    ap.add_argument("--benchmark", default=BENCHMARK)
    ap.add_argument("--bench-beta", type=float, default=1.25)
    ap.add_argument(
        "--bar-end",
        default=None,
        help="OHLC calendar end (default: oos-end); need hold buffer after last signal",
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

    print("loading universe (prefer specialty pool coverage) …", flush=True)
    pool_cov = load_coverage_from_specialty_pool(args.specialty_pool)
    if len(pool_cov) > 0:
        uni = pool_cov.copy()
        # specialty pool is already probe-qualified (m=2 local40 path)
        print(f"universe from specialty pool n={len(uni)}", flush=True)
    else:
        uni = load_local_universe(
            conn,
            probe_start=args.probe_start,
            probe_end=args.probe_end,
            min_dates=args.min_window_dates,
        )
        uni["n_dates"] = uni["n_probe_dates"]
        uni["min_d"] = None
        uni["max_d"] = None
        print(f"probe universe from DB n={len(uni)}", flush=True)
    prefer = uni[uni["n_dates"].fillna(0) >= args.prefer_2y_min_dates].copy()
    short_only = uni[uni["n_dates"].fillna(0) < args.prefer_2y_min_dates].copy()
    if args.universe == "prefer2y":
        scan_uni = prefer if len(prefer) else uni
        print(
            f"scan universe=prefer2y n={len(scan_uni)} "
            f"(short_only deferred={len(short_only)})",
            flush=True,
        )
    else:
        scan_uni = uni
        print(f"scan universe=all_probe n={len(scan_uni)}", flush=True)

    ids = scan_uni["id"].astype(str).tolist()
    name_map = dict(zip(scan_uni["id"].astype(str), scan_uni["name"].astype(str)))

    coverage_payload = {
        "n_universe": int(len(uni)),
        "n_prefer_2y": int(len(prefer)),
        "n_short_only": int(len(short_only)),
        "scan_mode": args.universe,
        "n_scanned": int(len(ids)),
        "short_only_ids_sample": short_only["id"].astype(str).head(20).tolist(),
        "note": (
            "Short-probe-only branches deferred when --universe prefer2y; "
            "they cannot meet IS n≥30 under current tape. No FinMind dual-run."
        ),
    }
    (OUT / "_branch_follow_coverage.json").write_text(
        json.dumps(coverage_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    window_start = min(args.is_start, args.oos_start)
    window_end = max(args.is_end, args.oos_end)
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

    # names from tape
    if not sig.empty:
        for bid, nm in (
            sig.groupby("securities_trader_id")["securities_trader"]
            .agg(lambda s: s.dropna().astype(str).iloc[0] if len(s.dropna()) else "")
            .items()
        ):
            if nm:
                name_map[str(bid)] = str(nm)

    cal_start = window_start
    # need lookback? entry is T+1 so calendar from window_start is enough
    # but hold needs days after window_end
    print("building calendar + OHLC + benchmark …", flush=True)
    cal = build_calendar(conn, cal_start, bar_end)
    idx = {d: i for i, d in enumerate(cal)}
    stock_ids = sorted(sig["stock_id"].astype(str).unique().tolist()) if len(sig) else []
    ohlc = load_ohlc(conn, stock_ids, cal_start, bar_end)
    benchmark_ohlc = load_benchmark_ohlc(
        conn, args.benchmark, cal_start, bar_end
    )
    conn.close()

    print("episode dedupe + L1H7 …", flush=True)
    events = evaluate_events(
        sig,
        cal=cal,
        idx=idx,
        ohlc=ohlc,
        benchmark_ohlc=benchmark_ohlc,
        bench_beta=args.bench_beta,
        episode_gap=args.episode_gap_days,
    )
    n_eval = sum(1 for e in events if e.get("r_adj") is not None)
    print(
        f"episode events={len(events):,} evaluable={n_eval:,}",
        flush=True,
    )
    coverage_payload.update(
        {
            "n_sig_rows": int(len(sig)),
            "n_episode_events": int(len(events)),
            "n_evaluable": int(n_eval),
        }
    )
    (OUT / "_branch_follow_coverage.json").write_text(
        json.dumps(coverage_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
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
    is_path = OUT / "branch_follow_all_events_is.csv"
    oos_path = OUT / "branch_follow_all_events_oos.csv"
    is_df.to_csv(is_path, index=False)
    oos_df.to_csv(oos_path, index=False)
    print(f"wrote {is_path} ({len(is_df)} rows)", flush=True)
    print(f"wrote {oos_path} ({len(oos_df)} rows)", flush=True)

    passers = h1_passers(is_df, oos_df)
    n_pass = len(passers)
    if n_pass >= 5:
        h1_verdict = f"PASSED (≥5) · n={n_pass}"
    elif n_pass >= 1:
        h1_verdict = f"THIN (1–4) · n={n_pass} · 不宣稱可規模化"
    else:
        h1_verdict = "REJECTED · n=0 · 選不到穩定跟單分點"

    # H2
    h2 = density_matched_control(
        oos_df, passers["branch_id"].astype(str).tolist() if n_pass else []
    )
    if n_pass == 0:
        h2_verdict = "SKIPPED (H1=0)"
        h2["feasible"] = False
    else:
        edge_u = h2.get("edge_vs_universe_pp")
        edge_d = h2.get("edge_vs_density_pp")
        ok_u = edge_u is not None and edge_u >= 1.0
        ok_d = edge_d is not None and edge_d >= 1.0
        if ok_u or ok_d:
            h2_verdict = (
                f"PASSED · vs_universe={edge_u}pp · vs_density={edge_d}pp"
            )
        else:
            h2_verdict = (
                f"REJECTED · vs_universe={edge_u}pp · vs_density={edge_d}pp"
            )

    # H4
    h4: dict = {"spearman_rho": None, "n": 0}
    if args.specialty_pool.exists() and not oos_df.empty:
        p1 = load_specialty_p1_ranks(args.specialty_pool)
        merged = oos_df.merge(p1, on="branch_id", how="inner")
        merged = merged[merged["median_pct"].notna() & merged["l1h7_rank"].notna()]
        # rank OOS by median desc (already have rank); correlate with short-window rank
        if len(merged) >= 3:
            rho = spearman_rho(
                merged["l1h7_rank"].astype(float).tolist(),
                merged["rank"].astype(float).tolist(),
            )
            h4 = {
                "spearman_rho": rho,
                "n": int(len(merged)),
                "note": (
                    "x=short-window best-pair l1h7_rank (1=best); "
                    "y=OOS all-event median rank (1=best)"
                ),
            }
    rho = h4.get("spearman_rho")
    if rho is None:
        h4_verdict = "INCONCLUSIVE (insufficient overlap)"
    elif abs(rho) < 0.3:
        h4_verdict = f"PASSED (ρ={rho:.3f} < 0.3) · 短窗 P1 與全事件 OOS 低相關"
    else:
        h4_verdict = (
            f"REJECTED narrative (ρ={rho:.3f} ≥ 0.3) · "
            "短窗 P1 與全事件 OOS 並非無關，需修正敘事"
        )

    protocol = {
        "abs_floor": args.abs_floor,
        "share_floor": args.share_floor,
        "top_k": args.top_k,
        "episode_gap_days": args.episode_gap_days,
        "hold": HOLD,
        "cost_bps": int(COST * 1e4),
        "benchmark": args.benchmark,
        "bench_beta": args.bench_beta,
        "is_start": args.is_start,
        "is_end": args.is_end,
        "oos_start": args.oos_start,
        "oos_end": args.oos_end,
        "min_window_dates": args.min_window_dates,
        "prefer_2y_min_dates": args.prefer_2y_min_dates,
        "universe": args.universe,
    }
    summary_path = OUT / "branch_follow_all_events_summary.md"
    write_summary_md(
        summary_path,
        protocol=protocol,
        coverage=coverage_payload,
        is_df=is_df,
        oos_df=oos_df,
        passers=passers,
        h2=h2,
        h4=h4,
        h1_verdict=h1_verdict,
        h2_verdict=h2_verdict,
        h4_verdict=h4_verdict,
    )
    print(f"wrote {summary_path}", flush=True)

    # Phase 3 consensus
    cons_path = OUT / "branch_follow_consensus_overlay.md"
    if n_pass >= 1:
        cons = consensus_overlay(
            events,
            passers["branch_id"].astype(str).tolist(),
            oos_start=args.oos_start,
            oos_end=args.oos_end,
        )
        solo = cons[cons["arm"] == "solo_all_events"]
        solo_med = (
            None
            if solo.empty or pd.isna(solo.iloc[0]["median_pct"])
            else float(solo.iloc[0]["median_pct"])
        )
        # H3: any consensus arm median ≥ solo
        h3_pass = False
        best_cons = None
        for _, row in cons.iterrows():
            if row["arm"] == "solo_all_events":
                continue
            if row["median_pct"] is None or (
                isinstance(row["median_pct"], float) and math.isnan(row["median_pct"])
            ):
                continue
            if solo_med is None or float(row["median_pct"]) >= solo_med:
                h3_pass = True
                best_cons = row
                break
        h3_verdict = (
            "PASSED · 共識臂 OOS 中位 ≥ 單分點臂"
            if h3_pass
            else "REJECTED · 共識未優於單分點；主規則維持單分點全事件"
        )
        lines = [
            "# 分點跟單 · 跨分點共識 overlay（H3）",
            "",
            f"候選池 = H1 通過分點 n={n_pass}。",
            f"OOS：{args.oos_start}‥{args.oos_end}。K∈{{3,5}}。",
            "",
            f"**H3 Verdict: {h3_verdict}**",
            "",
            "| arm | n | median% | mean% | win% |",
            "|-----|--:|--------:|------:|-----:|",
        ]
        for r in cons.itertuples():
            med = "NA" if r.median_pct is None or pd.isna(r.median_pct) else f"{r.median_pct:+.2f}"
            mean = "NA" if r.mean_pct is None or pd.isna(r.mean_pct) else f"{r.mean_pct:+.2f}"
            win = "NA" if r.win_rate_pct is None or pd.isna(r.win_rate_pct) else f"{r.win_rate_pct:.1f}"
            lines.append(
                f"| {r.arm} | {int(r.n)} | {med} | {mean} | {win} |"
            )
        if best_cons is not None:
            lines += ["", f"Best consensus arm: `{best_cons['arm']}`"]
        lines += [
            "",
            "若 H3 失敗：主規則維持「單分點全事件」，不採納共識加權當主規則。",
            "",
        ]
        cons_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        # stash h3 for yaml note
        (OUT / "_branch_follow_h3.json").write_text(
            json.dumps(
                {
                    "verdict": h3_verdict,
                    "passed": h3_pass,
                    "table": cons.to_dict(orient="records"),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"wrote {cons_path}", flush=True)
    else:
        cons_path.write_text(
            "\n".join(
                [
                    "# 分點跟單 · 跨分點共識 overlay（H3）",
                    "",
                    "**SKIPPED / REJECTED precondition**: H1 passers = 0。",
                    "依計畫：不進入共識加權；結案拒絕可選跟單分點池。",
                    "",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"wrote {cons_path} (H1=0 skip)", flush=True)

    meta = {
        "protocol": protocol,
        "coverage": coverage_payload,
        "h1": {"verdict": h1_verdict, "n_pass": n_pass},
        "h2": {"verdict": h2_verdict, **h2},
        "h4": {"verdict": h4_verdict, **h4},
        "outputs": {
            "is_csv": str(is_path),
            "oos_csv": str(oos_path),
            "summary_md": str(summary_path),
            "consensus_md": str(cons_path),
        },
    }
    (OUT / "_branch_follow_all_events_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print("=== DONE ===", flush=True)
    print(f"H1: {h1_verdict}", flush=True)
    print(f"H2: {h2_verdict}", flush=True)
    print(f"H4: {h4_verdict}", flush=True)


if __name__ == "__main__":
    main()
