#!/usr/bin/env python3
"""Short-window (B,S) discovery scan → specialist-card candidate pool.

Research-only. Uses existing tape (no FinMind fetch).

Discovery event D1' (pre-registered, wide):
  buy_amt = buy × close
  buy_amt >= abs_floor
  AND (buy_share >= share_floor OR day-rank(buy_amt) <= top_k)

Candidate gate (short window):
  any (B,S) with n_days >= m (default m=2)

Deep-backfill priority (soft, same short window — NOT specialist-card proof):
  for each qualifying (B,S), compute L1H7 mean (T+1 open, hold 7, 30bps)
  compute excess = stock net return - bench_beta * local IX0001 same-window return
  branch score = best pair excess mean among pairs with n_days>=m and n_ok>=2
  P1 / P2 / P3 tiers for backfill order only

Outputs under reports/research/branch-footprint-screen/:
  specialty_short_bs_pairs.csv
  specialty_candidate_pool.csv
  specialty_candidate_pool_summary.md
  _specialty_candidate_universe_m{m}.json
  _specialty_backfill_priority.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from stock_db import connect

OUT = Path("reports/research/branch-footprint-screen")
SOURCE = "finmind"
DEFAULT_PROBE = ("2026-05-20", "2026-07-16")
HOLD = 7
COST = 0.003  # 30 bps round-trip
BENCHMARK = "IX0001"


def load_fingerprint(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df["tier_letter"] = df["tier"].astype(str).str[0]
    return df


def load_local_window_universe(
    conn,
    *,
    start: str,
    end: str,
    min_dates: int,
) -> pd.DataFrame:
    """Return branches with usable local tape on enough probe-window dates."""
    df = pd.read_sql_query(
        """
        SELECT securities_trader_id AS id,
               MAX(securities_trader) AS name,
               COUNT(DISTINCT trade_date) AS n_window_dates
        FROM stock_broker_branch_daily
        WHERE source=?
          AND stock_id != '__EMPTY__'
          AND trade_date >= ? AND trade_date <= ?
        GROUP BY securities_trader_id
        HAVING COUNT(DISTINCT trade_date) >= ?
        """,
        conn,
        params=[SOURCE, start, end, min_dates],
    )
    df["id"] = df["id"].astype(str)
    df["name"] = df["name"].fillna("")
    df["tier"] = "U-local"
    df["tier_letter"] = "U"
    df["region"] = "local"
    return df


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


def episode_count(dates: list[str], k: int) -> int:
    if not dates:
        return 0
    ds = sorted(set(dates))
    n_ep = 1
    prev = ds[0]
    for d in ds[1:]:
        delta = (pd.Timestamp(d) - pd.Timestamp(prev)).days
        if delta > k:
            n_ep += 1
        prev = d
    return n_ep


def build_calendar(conn, start: str, end: str) -> list[str]:
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
    conn, stock_ids: list[str], start: str, end: str
) -> dict[tuple[str, str], tuple[float, float]]:
    if not stock_ids:
        return {}
    ph = ",".join("?" * len(stock_ids))
    rows = conn.execute(
        f"""
        SELECT stock_id, trade_date, open, close
        FROM stock_daily_bars
        WHERE source=? AND stock_id IN ({ph})
          AND trade_date >= ? AND trade_date <= ?
          AND open > 0 AND close > 0
        """,
        [SOURCE, *stock_ids, start, end],
    ).fetchall()
    return {(str(a), str(b)): (float(c), float(d)) for a, b, c, d in rows}


def load_benchmark_ohlc(
    conn,
    code: str,
    start: str,
    end: str,
) -> dict[str, tuple[float, float]]:
    """Load local IX0001 open/close, preferring Yahoo rows with valid open."""
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


def l1h7_rets_for_pair(
    dates: list[str],
    stock_id: str,
    *,
    cal: list[str],
    idx: dict[str, int],
    ohlc: dict[tuple[str, str], tuple[float, float]],
    benchmark_ohlc: dict[str, tuple[float, float]],
    bench_beta: float = 1.0,
) -> tuple[list[float], list[float], list[float]]:
    rets: list[float] = []
    benchmark_rets: list[float] = []
    excess_rets: list[float] = []
    n_cal = len(cal)
    beta = float(bench_beta)
    for sig in dates:
        i = idx.get(sig)
        if i is None:
            continue
        entry_i = i + 1  # L1 = T+1
        exit_i = entry_i + (HOLD - 1)  # inclusive hold days from entry
        if exit_i >= n_cal:
            continue
        entry_d = cal[entry_i]
        exit_d = cal[exit_i]
        eo = ohlc.get((stock_id, entry_d))
        xc = ohlc.get((stock_id, exit_d))
        bench_eo = benchmark_ohlc.get(entry_d)
        bench_xc = benchmark_ohlc.get(exit_d)
        if eo is None or xc is None or bench_eo is None or bench_xc is None:
            continue
        entry_px, _ = eo
        _, exit_px = xc
        benchmark_entry_px, _ = bench_eo
        _, benchmark_exit_px = bench_xc
        if (
            entry_px <= 0
            or exit_px <= 0
            or benchmark_entry_px <= 0
            or benchmark_exit_px <= 0
        ):
            continue
        stock_ret = exit_px / entry_px - 1.0 - COST
        benchmark_ret = benchmark_exit_px / benchmark_entry_px - 1.0
        rets.append(stock_ret)
        benchmark_rets.append(benchmark_ret)
        excess_rets.append(stock_ret - beta * benchmark_ret)
    return rets, benchmark_rets, excess_rets


def assign_priority_by_rank(
    best_map: dict[str, dict],
    *,
    p1_frac: float = 0.20,
    p2_frac: float = 0.30,
) -> None:
    """Regime-adaptive tiers: top 20% → P1, next 30% → P2, rest → P3.

    Absolute +3% is useless in a hot 40d window (almost everyone clears it).
    Mutates best_map in place.
    """
    scored = [
        (bid, info)
        for bid, info in best_map.items()
        if info.get("best_excess_l1h7_mean_pct") is not None
        and info.get("best_n_ok_l1h7", 0) >= 2
    ]
    scored.sort(
        key=lambda x: (
            -(x[1]["best_excess_l1h7_mean_pct"] or -999),
            -x[1].get("best_n_ok_l1h7", 0),
            -x[1].get("best_n_days", 0),
        )
    )
    n = len(scored)
    n_p1 = max(1, int(round(n * p1_frac))) if n else 0
    n_p2 = max(0, int(round(n * p2_frac))) if n else 0
    for i, (bid, info) in enumerate(scored):
        if i < n_p1:
            info["priority"] = "P1"
        elif i < n_p1 + n_p2:
            info["priority"] = "P2"
        else:
            info["priority"] = "P3"
        info["l1h7_rank"] = i + 1
        info["l1h7_rank_n"] = n
    for bid, info in best_map.items():
        if "priority" not in info:
            info["priority"] = "P3"
            info["l1h7_rank"] = None
            info["l1h7_rank_n"] = n


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe-start", default=DEFAULT_PROBE[0])
    ap.add_argument("--probe-end", default=DEFAULT_PROBE[1])
    ap.add_argument("--abs-floor", type=float, default=3e7)
    ap.add_argument("--share-floor", type=float, default=0.10)
    ap.add_argument("--top-k", type=int, default=3)
    ap.add_argument("--episode-gap-days", type=int, default=5)
    ap.add_argument(
        "--fingerprint",
        type=Path,
        default=OUT / "taiwan_branch_footprint_rank40d_refreshed.csv",
    )
    ap.add_argument(
        "--universe-source",
        choices=("fingerprint", "local40"),
        default="fingerprint",
        help="fingerprint non-Z universe, or all branches with complete local probe tape",
    )
    ap.add_argument(
        "--min-window-dates",
        type=int,
        default=40,
        help="minimum usable probe-window dates for --universe-source local40",
    )
    ap.add_argument("--m-list", default="2,3,4")
    ap.add_argument("--two-year-days", type=int, default=496)
    ap.add_argument(
        "--bar-end",
        default="2026-07-16",
        help="OHLC end for L1H7 exit (need hold buffer after probe-end)",
    )
    ap.add_argument("--benchmark", default=BENCHMARK)
    ap.add_argument(
        "--bench-beta",
        type=float,
        default=1.0,
        help="excess = stock_net - bench_beta * benchmark same-window return",
    )
    args = ap.parse_args()
    m_list = [int(x) for x in str(args.m_list).split(",") if x.strip()]
    default_m = m_list[0] if m_list else 2
    bench_beta = float(args.bench_beta)

    OUT.mkdir(parents=True, exist_ok=True)
    conn = connect()
    if args.universe_source == "local40":
        non_z = load_local_window_universe(
            conn,
            start=args.probe_start,
            end=args.probe_end,
            min_dates=args.min_window_dates,
        )
        universe_label = (
            f"本地完整短窗（usable dates ≥ {args.min_window_dates}）"
        )
    else:
        fp = load_fingerprint(args.fingerprint)
        non_z = fp[fp["tier_letter"] != "Z"].copy()
        universe_label = "指紋非 Z（A/B/C/D）"
    ids = non_z["id"].astype(str).tolist()
    name_map = dict(zip(non_z["id"].astype(str), non_z["name"].astype(str)))
    tier_map = dict(zip(non_z["id"].astype(str), non_z["tier"].astype(str)))
    region_map = dict(zip(non_z["id"].astype(str), non_z["region"].astype(str)))

    placeholders = ",".join("?" * len(ids))
    q = f"""
        SELECT b.securities_trader_id, b.trade_date, b.stock_id, b.buy,
               b.buy * p.close AS buy_amt
        FROM stock_broker_branch_daily b
        JOIN stock_daily_bars p
          ON p.stock_id=b.stock_id
         AND p.trade_date=b.trade_date
         AND p.source=?
        WHERE b.source=?
          AND b.securities_trader_id IN ({placeholders})
          AND b.trade_date >= ? AND b.trade_date <= ?
          AND b.stock_id != '__EMPTY__'
          AND p.close > 0
          AND length(b.stock_id)=4
          AND b.stock_id GLOB '[0-9][0-9][0-9][0-9]'
    """
    print(
        f"loading tape for {len(ids)} non-Z branches "
        f"{args.probe_start}..{args.probe_end} …",
        flush=True,
    )
    raw = pd.read_sql_query(
        q, conn, params=[SOURCE, SOURCE, *ids, args.probe_start, args.probe_end]
    )
    print(f"rows={len(raw):,}", flush=True)

    stock_names: dict[str, str] = {}
    for sql in (
        "SELECT stock_id, name FROM stock_beta WHERE name IS NOT NULL AND name != ''",
        "SELECT stock_id, stock_name FROM universe_constituents "
        "WHERE stock_name IS NOT NULL AND stock_name != ''",
    ):
        try:
            for a, b in conn.execute(sql).fetchall():
                sid = str(a)
                if sid not in stock_names and b:
                    stock_names[sid] = str(b)
        except Exception:  # noqa: BLE001
            pass

    cov = pd.read_sql_query(
        f"""
        SELECT securities_trader_id AS id,
               COUNT(DISTINCT trade_date) AS n_dates,
               MIN(trade_date) AS min_d,
               MAX(trade_date) AS max_d
        FROM stock_broker_branch_daily
        WHERE source=?
          AND stock_id != '__EMPTY__'
          AND securities_trader_id IN ({placeholders})
        GROUP BY securities_trader_id
        """,
        conn,
        params=[SOURCE, *ids],
    )
    cov_map = {str(r.id): int(r.n_dates) for r in cov.itertuples()}
    cov_min = {str(r.id): str(r.min_d) for r in cov.itertuples()}
    cov_max = {str(r.id): str(r.max_d) for r in cov.itertuples()}

    cal = build_calendar(conn, args.probe_start, args.bar_end)
    idx = {d: i for i, d in enumerate(cal)}

    sig = significant_buys(
        raw,
        abs_floor=args.abs_floor,
        share_floor=args.share_floor,
        top_k=args.top_k,
    )
    print(f"significant event rows={len(sig):,}", flush=True)

    stock_ids = sorted(sig["stock_id"].astype(str).unique().tolist())
    print(f"loading OHLC for {len(stock_ids)} stocks …", flush=True)
    ohlc = load_ohlc(conn, stock_ids, args.probe_start, args.bar_end)
    print(f"loading local benchmark {args.benchmark} …", flush=True)
    benchmark_ohlc = load_benchmark_ohlc(
        conn, args.benchmark, args.probe_start, args.bar_end
    )
    conn.close()
    missing_benchmark_dates = sorted(set(cal) - set(benchmark_ohlc))
    if missing_benchmark_dates:
        raise RuntimeError(
            f"benchmark missing {len(missing_benchmark_dates)} market dates: "
            f"{missing_benchmark_dates[:5]}"
        )

    pairs: list[dict] = []
    for (bid, sid), g in sig.groupby(["securities_trader_id", "stock_id"], sort=False):
        dates = sorted(g["trade_date"].astype(str).unique().tolist())
        n_days = len(dates)
        n_ep = episode_count(dates, args.episode_gap_days)
        rets, benchmark_rets, excess_rets = l1h7_rets_for_pair(
            dates,
            str(sid),
            cal=cal,
            idx=idx,
            ohlc=ohlc,
            benchmark_ohlc=benchmark_ohlc,
            bench_beta=bench_beta,
        )
        n_ok = len(rets)
        mean_pct = float(np.mean(rets) * 100) if n_ok else float("nan")
        benchmark_mean_pct = (
            float(np.mean(benchmark_rets) * 100) if n_ok else float("nan")
        )
        excess_mean_pct = (
            float(np.mean(excess_rets) * 100) if n_ok else float("nan")
        )
        win = float(np.mean([r > 0 for r in rets]) * 100) if n_ok else float("nan")
        excess_win = (
            float(np.mean([r > 0 for r in excess_rets]) * 100)
            if n_ok
            else float("nan")
        )
        pairs.append(
            {
                "branch_id": str(bid),
                "branch_name": name_map.get(str(bid), ""),
                "region": region_map.get(str(bid), ""),
                "tier": tier_map.get(str(bid), ""),
                "stock_id": str(sid),
                "stock_name": stock_names.get(str(sid), ""),
                "n_days": n_days,
                "n_episodes": n_ep,
                "n_ok_l1h7": n_ok,
                "l1h7_mean_pct": mean_pct,
                "benchmark_l1h7_mean_pct": benchmark_mean_pct,
                "excess_l1h7_mean_pct": excess_mean_pct,
                "l1h7_win_pct": win,
                "excess_l1h7_win_pct": excess_win,
                "buy_amt_med_億": float(g["buy_amt"].median()) / 1e8,
                "buy_share_med": float(g["buy_share"].median()),
                "rk_med": float(g["rk"].median()),
                "first_date": dates[0],
                "last_date": dates[-1],
            }
        )

    pairs_df = pd.DataFrame(pairs).sort_values(
        ["excess_l1h7_mean_pct", "n_days", "n_episodes"],
        ascending=[False, False, False],
    )
    pairs_path = OUT / "specialty_short_bs_pairs.csv"
    pairs_df.to_csv(pairs_path, index=False)

    # Branch-level best pair among m-qualified with usable L1H7
    def branch_best_for_m(m: int) -> dict[str, dict]:
        out: dict[str, dict] = {}
        sub = pairs_df[pairs_df["n_days"] >= m]
        for bid, g in sub.groupby("branch_id"):
            usable = g[g["n_ok_l1h7"] >= 2].copy()
            if usable.empty:
                # fall back: most frequent pair, no L1H7 score
                row = g.sort_values(["n_days", "n_episodes"], ascending=False).iloc[0]
                out[str(bid)] = {
                    "best_stock_id": str(row.stock_id),
                    "best_stock_name": str(row.stock_name),
                    "best_n_days": int(row.n_days),
                    "best_n_episodes": int(row.n_episodes),
                    "best_n_ok_l1h7": int(row.n_ok_l1h7),
                    "best_l1h7_mean_pct": None,
                    "best_benchmark_l1h7_mean_pct": None,
                    "best_excess_l1h7_mean_pct": None,
                    "n_qualifying_stocks": int(len(g)),
                }
                continue
            row = usable.sort_values(
                ["excess_l1h7_mean_pct", "n_days"], ascending=[False, False]
            ).iloc[0]
            mean = float(row.l1h7_mean_pct)
            benchmark_mean = float(row.benchmark_l1h7_mean_pct)
            excess_mean = float(row.excess_l1h7_mean_pct)
            out[str(bid)] = {
                "best_stock_id": str(row.stock_id),
                "best_stock_name": str(row.stock_name),
                "best_n_days": int(row.n_days),
                "best_n_episodes": int(row.n_episodes),
                "best_n_ok_l1h7": int(row.n_ok_l1h7),
                "best_l1h7_mean_pct": mean,
                "best_benchmark_l1h7_mean_pct": benchmark_mean,
                "best_excess_l1h7_mean_pct": excess_mean,
                "n_qualifying_stocks": int(len(g)),
            }
        assign_priority_by_rank(out)
        return out

    pool_rows: list[dict] = []
    summary_lines = [
        "# 分點專家卡 · 短窗候選池（Phase 0）",
        "",
        f"- 窗：`{args.probe_start}` .. `{args.probe_end}`",
        f"- 宇宙：{universe_label} = **{len(ids)}**",
        f"- 發現事件 D1'：`buy_amt ≥ {args.abs_floor/1e8:.2f}億` 且 "
        f"(`buy_share ≥ {args.share_floor:.0%}` 或 當日 rank ≤ {args.top_k})",
        f"- 候選門檻：同股顯著買進 **n_days ≥ {default_m}**（預設）",
        f"- 優先序：同窗該檔 excess L1H7（個股含 {COST*1e4:.0f}bps − "
        f"{bench_beta:g}×{args.benchmark} 同窗報酬）→ **只排序深補，不成卡**",
        f"- `(B,S)` 配對數：{len(pairs_df):,}",
        "",
        "## 候選池人數",
        "",
        "| m | 候選分點 | P1 | P2 | P3 | 其中 D | 預估尚缺 API 日* |",
        "|---|----------|----|----|----|--------|------------------|",
    ]

    priority_payload: dict[str, list] = {"P1": [], "P2": [], "P3": []}

    for m in m_list:
        best_map = branch_best_for_m(m)
        bids = sorted(best_map.keys())
        counts = {"P1": 0, "P2": 0, "P3": 0}
        n_d = 0
        need = 0
        for b in bids:
            info = best_map[b]
            pr = info["priority"]
            counts[pr] += 1
            if tier_map.get(b, "").startswith("D"):
                n_d += 1
            api_need = max(0, args.two_year_days - cov_map.get(b, 0))
            need += api_need
            row = {
                "m_threshold": m,
                "priority": pr,
                "branch_id": b,
                "branch_name": name_map.get(b, ""),
                "region": region_map.get(b, ""),
                "tier": tier_map.get(b, ""),
                "best_stock_id": info["best_stock_id"],
                "best_stock_name": info["best_stock_name"],
                "best_n_days": info["best_n_days"],
                "best_n_episodes": info["best_n_episodes"],
                "best_n_ok_l1h7": info["best_n_ok_l1h7"],
                "best_l1h7_mean_pct": info["best_l1h7_mean_pct"],
                "best_benchmark_l1h7_mean_pct": info[
                    "best_benchmark_l1h7_mean_pct"
                ],
                "best_excess_l1h7_mean_pct": info[
                    "best_excess_l1h7_mean_pct"
                ],
                "l1h7_rank": info.get("l1h7_rank"),
                "n_qualifying_stocks": info["n_qualifying_stocks"],
                "tape_n_dates": cov_map.get(b, 0),
                "tape_min": cov_min.get(b, ""),
                "tape_max": cov_max.get(b, ""),
                "est_api_days_to_2y": api_need,
            }
            pool_rows.append(row)
            if m == default_m:
                priority_payload[pr].append(
                    {
                        "id": b,
                        "name": name_map.get(b, ""),
                        "region": region_map.get(b, ""),
                        "tier": tier_map.get(b, ""),
                        "best_stock_id": info["best_stock_id"],
                        "best_stock_name": info["best_stock_name"],
                        "best_l1h7_mean_pct": info["best_l1h7_mean_pct"],
                        "best_benchmark_l1h7_mean_pct": info[
                            "best_benchmark_l1h7_mean_pct"
                        ],
                        "best_excess_l1h7_mean_pct": info[
                            "best_excess_l1h7_mean_pct"
                        ],
                        "best_n_days": info["best_n_days"],
                        "best_n_ok_l1h7": info["best_n_ok_l1h7"],
                        "l1h7_rank": info.get("l1h7_rank"),
                        "est_api_days_to_2y": api_need,
                    }
                )
        summary_lines.append(
            f"| ≥{m} | {len(bids)} | {counts['P1']} | {counts['P2']} | "
            f"{counts['P3']} | {n_d} | ~{need:,} |"
        )

    # sort each priority by excess L1H7 desc
    for pr in ("P1", "P2", "P3"):
        priority_payload[pr].sort(
            key=lambda x: (
                x["best_excess_l1h7_mean_pct"] is None,
                -(x["best_excess_l1h7_mean_pct"] or -999),
            )
        )

    summary_lines += [
        "",
        f"\\*預估：每分點目標 `{args.two_year_days}` 交易日 − 現有 distinct 有資料日。",
        "",
        "## 優先序定義（深補用，非正式專家卡）",
        "",
        "| 優先 | 條件（在 m 達標且 n_ok≥2 的分點內） |",
        "|------|--------------------------------------|",
        f"| P1 | 短窗最佳 `(B,S)` excess L1H7（vs {bench_beta:g}×{args.benchmark}）**前 20%** |",
        "| P2 | 其後 **30%** |",
        "| P3 | 其餘 m 達標（含 L1H7 樣本不足） |",
        "",
        f"- excess = 個股 L1H7（含成本）− {bench_beta:g}×{args.benchmark} 同一進出窗報酬。",
        "- 用**分位**排序；仍保留絕對 L1H7 供閱讀。",
        "- **全部 m 達標都進候選池**；P1→P2→P3 只決定兩年深補順序。",
        "- 短窗 L1H7 好看 ≠ 成卡；正式卡仍要定義窗專長差／同股差／跨期。",
        "",
        f"## 預設 m≥{default_m} · P1 Top 25（深補第一波）",
        "",
        "| 分點 | 指紋 | 最佳標的 | n_days | n_ok | 個股% | 大盤% | 超額% | 缺API日 |",
        "|------|------|----------|--------|------|-------|-------|-------|---------|",
    ]
    for r in priority_payload["P1"][:25]:
        mean = r["best_excess_l1h7_mean_pct"]
        mean_s = f"{mean:+.1f}" if mean is not None else "n/a"
        summary_lines.append(
            f"| {r['name']} (`{r['id']}`) | {str(r['tier'])[:1]} | "
            f"{r['best_stock_name']} ({r['best_stock_id']}) | "
            f"{r['best_n_days']} | — | {mean_s} | {r['est_api_days_to_2y']} |"
        )

    # fix n_ok in table - look up from payload we have best_n_days only; add from pool
    # Actually regenerate P1 top from pool_rows for accuracy
    pool_df = pd.DataFrame(pool_rows)
    pool_path = OUT / "specialty_candidate_pool.csv"
    pool_df.to_csv(pool_path, index=False)

    p1 = pool_df[
        (pool_df["m_threshold"] == default_m) & (pool_df["priority"] == "P1")
    ].sort_values("best_excess_l1h7_mean_pct", ascending=False)
    # rewrite P1 section with n_ok
    # find and replace the P1 table - easier rebuild last section
    head = "\n".join(summary_lines)
    # truncate at P1 header and rewrite
    marker = f"## 預設 m≥{default_m} · P1 Top 25（深補第一波）"
    head = head.split(marker)[0] + marker + "\n\n"
    lines = [
        head.rstrip(),
        "",
        "| 分點 | 指紋 | 最佳標的 | n_days | n_ok | 個股% | 大盤% | 超額% | 缺API日 |",
        "|------|------|----------|--------|------|-------|-------|-------|---------|",
    ]
    for r in p1.head(25).itertuples():
        lines.append(
            f"| {r.branch_name} (`{r.branch_id}`) | {str(r.tier)[:1]} | "
            f"{r.best_stock_name} ({r.best_stock_id}) | "
            f"{r.best_n_days} | {r.best_n_ok_l1h7} | "
            f"{r.best_l1h7_mean_pct:+.1f} | "
            f"{r.best_benchmark_l1h7_mean_pct:+.1f} | "
            f"{r.best_excess_l1h7_mean_pct:+.1f} | "
            f"{r.est_api_days_to_2y} |"
        )

    # top pairs by excess L1H7 among m>=default
    lines += [
        "",
        f"## Top `(B,S)` by excess L1H7 vs {bench_beta:g}×{args.benchmark}"
        f"（n_days≥{default_m} 且 n_ok≥2，前 30）",
        "",
        "| 分點 | 標的 | n_days | n_ok | 個股% | 大盤% | 超額% | 超額勝% |",
        "|------|------|--------|------|-------|-------|-------|---------|",
    ]
    top_pairs = pairs_df[
        (pairs_df["n_days"] >= default_m) & (pairs_df["n_ok_l1h7"] >= 2)
    ].sort_values("excess_l1h7_mean_pct", ascending=False)
    for r in top_pairs.head(30).itertuples():
        lines.append(
            f"| {r.branch_name} (`{r.branch_id}`) | "
            f"{r.stock_name} ({r.stock_id}) | {r.n_days} | {r.n_ok_l1h7} | "
            f"{r.l1h7_mean_pct:+.1f} | {r.benchmark_l1h7_mean_pct:+.1f} | "
            f"{r.excess_l1h7_mean_pct:+.1f} | {r.excess_l1h7_win_pct:.0f} |"
        )

    md_path = OUT / "specialty_candidate_pool_summary.md"
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    default_pool = pool_df[pool_df["m_threshold"] == default_m].copy()
    uni = []
    for r in default_pool.sort_values(
        ["priority", "best_excess_l1h7_mean_pct"],
        ascending=[True, False],
    ).itertuples():
        uni.append(
            {
                "id": r.branch_id,
                "name": r.branch_name,
                "region": r.region,
                "tier": r.tier,
                "priority": r.priority,
                "best_stock_id": r.best_stock_id,
                "best_l1h7_mean_pct": (
                    None
                    if r.best_l1h7_mean_pct != r.best_l1h7_mean_pct
                    else float(r.best_l1h7_mean_pct)
                ),
                "best_excess_l1h7_mean_pct": (
                    None
                    if r.best_excess_l1h7_mean_pct
                    != r.best_excess_l1h7_mean_pct
                    else float(r.best_excess_l1h7_mean_pct)
                ),
                "est_api_days_to_2y": int(r.est_api_days_to_2y),
            }
        )
    uni_path = OUT / f"_specialty_candidate_universe_m{default_m}.json"
    uni_path.write_text(json.dumps(uni, ensure_ascii=False, indent=2), encoding="utf-8")

    prio_path = OUT / "_specialty_backfill_priority.json"
    prio_path.write_text(
        json.dumps(
            {
                "m": default_m,
                "probe": [args.probe_start, args.probe_end],
                "l1h7": {
                    "hold": HOLD,
                    "cost": COST,
                    "entry": "T+1 open",
                    "benchmark": args.benchmark,
                    "bench_beta": bench_beta,
                    "priority_metric": (
                        f"stock_net_return - {bench_beta:g} * benchmark_return"
                    ),
                },
                "tiers": {
                    "P1": "top 20% by short-window best-pair excess L1H7 (n_ok>=2)",
                    "P2": "next 30%",
                    "P3": "rest of m-qualified (still candidate)",
                },
                "note": "priority for 2y backfill order only; not specialist-card admission",
                "counts": {k: len(v) for k, v in priority_payload.items()},
                "est_api_days": {
                    k: int(sum(x["est_api_days_to_2y"] for x in v))
                    for k, v in priority_payload.items()
                },
                "queues": priority_payload,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    expanded_p1 = [
        x
        for x in priority_payload["P1"]
        if x["est_api_days_to_2y"] >= 50
    ]
    expanded_p1_path = OUT / "_specialty_p1_2y_expanded_universe.json"
    expanded_p1_path.write_text(
        json.dumps(expanded_p1, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    meta = {
        "probe": [args.probe_start, args.probe_end],
        "universe_source": args.universe_source,
        "universe_label": universe_label,
        "n_non_z": len(ids),
        "abs_floor": args.abs_floor,
        "share_floor": args.share_floor,
        "top_k": args.top_k,
        "benchmark": args.benchmark,
        "bench_beta": bench_beta,
        "n_pairs": int(len(pairs_df)),
        "default_m": default_m,
        "n_candidates_default_m": int(len(default_pool)),
        "priority_counts": {k: len(v) for k, v in priority_payload.items()},
        "outputs": {
            "pairs": str(pairs_path),
            "pool": str(pool_path),
            "summary": str(md_path),
            "universe": str(uni_path),
            "priority": str(prio_path),
            "expanded_p1_2y_universe": str(expanded_p1_path),
        },
        "n_expanded_p1_2y": len(expanded_p1),
    }
    (OUT / "specialty_candidate_scan_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(meta, ensure_ascii=False, indent=2), flush=True)
    print(f"wrote {md_path}", flush=True)


if __name__ == "__main__":
    main()
