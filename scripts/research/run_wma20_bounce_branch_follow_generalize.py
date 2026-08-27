#!/usr/bin/env python3
"""WMA20 反彈確認濾器 · 跨股票分點跟單候選宇宙 generalization test（Research · book-only）。

Question：`run_2327_wma20_bounce_confirm_study.py` 只在單股 2327 上驗證「盤中 WMA20
反彈確認」(AND：連續2根站上 WMA20 且 緩衝幅度>0.3%，edge-triggered) 優於 naive 單根
穿越 baseline（full win_rate 36.8% vs 33.8%；OOS 39.2% vs 34.2%）。沒人驗過這是否能
generalize 到更廣的分點跟單候選宇宙（不只 2327）。

Method（沿用既有 SSOT，不重新發明）：
  1. Entry-quality 濾器定義 100% 照搬
     scripts/research/run_2327_wma20_bounce_confirm_study.py 的
     `and_persist_and_buffer` variant：WMA20（5分K、盤中連續）、buffer=0.3%、
     persistence=2根、edge-triggered（狀態剛由 False→True）。
  2. 候選宇宙 = 分點跟單「凍結協議」D1'/L1H7/β1.25（見
     reports/research/branch-footprint-screen/branch_follow_all_events_summary.md，
     腳本 scripts/research/run_branch_follow_all_events_scan.py）套用在本 repo 反覆
     出現、被當作「已知候選分點」討論的 4 個分點：9217（凱基-松山，songshan-copytrade
     現正 live）、9227、9661（富邦-新店）、9801（元大-松江）——見
     config/order.yaml songshan-copytrade 與 config/research.yaml 多處
     「已知好分點（9217/9227/9661/9801）」註記。
     事件 D1'：buy_amt≥0.30億 且 (share≥10% 或 day-rank≤3)；episode：5曆日同分點
     同股去重；進出：L1H7（T+1開盤進場、hold 7 交易日收盤出場）、成本30bps、
     β=1.25×IX0001 超額。
  3. 對每個 D1' 事件的 L1H7 進場日（T+1），檢查該股當天盤中是否出現
     and_persist_and_buffer edge（09:00–13:30 session、WMA20 跨日連續）。
     觸發 → confirmed；未觸發 → not_confirmed。僅需該股在 stock_kbar_5m 有覆蓋
     才能判定，缺 5m 資料的事件會被排除並在報告中列出排除數。
  4. 比較 confirmed vs not_confirmed 兩組 L1H7 r_adj（win_rate / mean / median），
     用兩樣本 label-shuffle permutation test（5000 次、seed=20260728，比照
     src/research/branch_signal_validation.py 的 permutation 慣例）檢定均值/中位數
     差異的顯著性。

Read-only DB. Research only；不寫 config/order.yaml 或 config/strategy.yaml。

用法::

  PYTHONPATH=src .venv/bin/python \
    scripts/research/run_wma20_bounce_branch_follow_generalize.py --write
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from rrg_rotation import wma  # noqa: E402
from stock_db import DEFAULT_DB_PATH  # noqa: E402

OUT_DIR = ROOT / "reports" / "research" / "wma20_bounce_generalize"
SCHEMA = "wma20_bounce_branch_follow_generalize-v1"

# ---------------------------------------------------------------------------
# WMA20 bounce-confirm signal — copied verbatim (params + logic) from
# run_2327_wma20_bounce_confirm_study.py so the filter definition matches
# exactly what was validated on 2327.
# ---------------------------------------------------------------------------
WMA_LENGTH = 20
BUFFER_PCT = 0.003  # 0.3%
PERSISTENCE_BARS = 2
SESSION_MINUTE_LO = "09:00"
SESSION_MINUTE_HI = "13:30"

# ---------------------------------------------------------------------------
# Branch-follow frozen protocol (D1' / L1H7 / beta=1.25) — copied verbatim
# (constants + core functions) from run_branch_follow_all_events_scan.py.
# ---------------------------------------------------------------------------
SOURCE = "finmind"
HOLD = 7
COST = 0.003  # 30 bps round-trip
BENCHMARK = "IX0001"
BENCH_BETA = 1.25
EPISODE_GAP = 5
ABS_FLOOR = 3e7  # 0.30億
SHARE_FLOOR = 0.10
TOP_K = 3

KNOWN_SEAT_IDS = ("9217", "9227", "9661", "9801")
KNOWN_SEAT_NAMES = {
    "9217": "凱基-松山",
    "9227": "凱基-台北",
    "9661": "富邦-新店",
    "9801": "元大-松江",
}

WINDOW_START = "2024-07-01"
WINDOW_END = "2026-07-16"  # matches frozen protocol OOS end
BAR_END = "2026-08-03"  # need daily-bar buffer for L1H7 exits after signal window

N_PERM = 5000
PERM_SEED = 20260728  # matches src/research/branch_signal_validation.py convention


def connect_ro(db_path: Path) -> sqlite3.Connection:
    p = db_path.resolve()
    conn = sqlite3.connect(f"file:{p}?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def month_starts(start: str, end: str) -> list[tuple[str, str]]:
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


def significant_buys(df: pd.DataFrame, *, abs_floor: float, share_floor: float, top_k: int) -> pd.DataFrame:
    if df.empty:
        return df
    df = df.sort_values(["securities_trader_id", "trade_date", "buy_amt"], ascending=[True, True, False])
    df["day_buy"] = df.groupby(["securities_trader_id", "trade_date"])["buy_amt"].transform("sum")
    df["buy_share"] = df["buy_amt"] / df["day_buy"].replace(0, np.nan)
    df["rk"] = df.groupby(["securities_trader_id", "trade_date"])["buy_amt"].rank(method="first", ascending=False)
    mask = (df["buy_amt"] >= abs_floor) & ((df["buy_share"] >= share_floor) | (df["rk"] <= top_k))
    return df.loc[mask].copy()


def episode_first_dates(dates: list[str], gap_days: int = EPISODE_GAP) -> list[str]:
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
    return kept


def build_calendar(conn: sqlite3.Connection, start: str, end: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT trade_date FROM stock_daily_bars
        WHERE source=? AND stock_id='2330' AND trade_date >= ? AND trade_date <= ?
        ORDER BY trade_date
        """,
        (SOURCE, start, end),
    ).fetchall()
    return [str(r[0]) for r in rows]


def load_ohlc(conn: sqlite3.Connection, stock_ids: list[str], start: str, end: str) -> dict[tuple[str, str], tuple[float, float]]:
    if not stock_ids:
        return {}
    out: dict[tuple[str, str], tuple[float, float]] = {}
    chunk = 400
    for i in range(0, len(stock_ids), chunk):
        part = stock_ids[i : i + chunk]
        ph = ",".join("?" * len(part))
        rows = conn.execute(
            f"""
            SELECT stock_id, trade_date, open, close FROM stock_daily_bars
            WHERE source=? AND stock_id IN ({ph}) AND trade_date >= ? AND trade_date <= ?
              AND open > 0 AND close > 0
            """,
            [SOURCE, *part, start, end],
        ).fetchall()
        for a, b, c, d in rows:
            out[(str(a), str(b))] = (float(c), float(d))
    return out


def load_closes_month(conn: sqlite3.Connection, start: str, end: str) -> dict[tuple[str, str], float]:
    rows = conn.execute(
        """
        SELECT stock_id, trade_date, close FROM stock_daily_bars
        WHERE source=? AND trade_date >= ? AND trade_date <= ? AND close > 0
          AND length(stock_id)=4 AND stock_id GLOB '[0-9][0-9][0-9][0-9]'
        """,
        (SOURCE, start, end),
    ).fetchall()
    return {(str(a), str(b)): float(c) for a, b, c in rows}


def load_benchmark_ohlc(conn: sqlite3.Connection, code: str, start: str, end: str) -> dict[str, tuple[float, float]]:
    rows = conn.execute(
        """
        SELECT date, open, close FROM daily_bars
        WHERE code=? AND date>=? AND date<=? AND open>0 AND close>0
        ORDER BY date,
          CASE source WHEN 'yahoo' THEN 0 WHEN 'tej' THEN 1 WHEN 'finmind' THEN 2 ELSE 3 END
        """,
        (code, start, end),
    ).fetchall()
    out: dict[str, tuple[float, float]] = {}
    for d, op, cl in rows:
        out.setdefault(str(d), (float(op), float(cl)))
    return out


def collect_d1_events(
    conn: sqlite3.Connection, *, ids: list[str], start: str, end: str,
    abs_floor: float, share_floor: float, top_k: int,
) -> pd.DataFrame:
    id_set = set(ids)
    chunks: list[pd.DataFrame] = []
    months = month_starts(start, end)
    for a, b in months:
        raw = pd.read_sql_query(
            """
            SELECT securities_trader_id, trade_date, stock_id, buy, securities_trader
            FROM stock_broker_branch_daily
            WHERE source=? AND trade_date >= ? AND trade_date <= ?
              AND stock_id != '__EMPTY__' AND length(stock_id)=4
              AND stock_id GLOB '[0-9][0-9][0-9][0-9]'
            """,
            conn, params=[SOURCE, a, b],
        )
        if raw.empty:
            continue
        raw["securities_trader_id"] = raw["securities_trader_id"].astype(str)
        raw = raw[raw["securities_trader_id"].isin(id_set)]
        if raw.empty:
            continue
        closes = load_closes_month(conn, a, b)
        close_df = pd.DataFrame([(sid, d, px) for (sid, d), px in closes.items()], columns=["stock_id", "trade_date", "close"])
        raw["stock_id"] = raw["stock_id"].astype(str)
        raw["trade_date"] = raw["trade_date"].astype(str)
        if close_df.empty:
            continue
        raw = raw.merge(close_df, on=["stock_id", "trade_date"], how="inner")
        raw["buy_amt"] = raw["buy"].astype(float) * raw["close"].astype(float)
        raw = raw[np.isfinite(raw["buy_amt"]) & (raw["buy_amt"] > 0)]
        sig = significant_buys(raw, abs_floor=abs_floor, share_floor=share_floor, top_k=top_k)
        if not sig.empty:
            chunks.append(sig[["securities_trader_id", "securities_trader", "trade_date", "stock_id", "buy_amt", "buy_share", "rk"]])
    if not chunks:
        return pd.DataFrame(columns=["securities_trader_id", "securities_trader", "trade_date", "stock_id", "buy_amt", "buy_share", "rk"])
    return pd.concat(chunks, ignore_index=True)


@dataclass
class L1H7Result:
    entry_date: str
    exit_date: str
    r_stock: float
    r_bench: float
    r_adj: float


def l1h7_one(sig: str, stock_id: str, *, cal: list[str], idx: dict[str, int], ohlc: dict, benchmark_ohlc: dict, bench_beta: float) -> L1H7Result | None:
    i = idx.get(sig)
    if i is None:
        return None
    entry_i = i + 1
    exit_i = entry_i + (HOLD - 1)
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
    stock_ret = exit_px / entry_px - 1.0 - COST
    bench_ret = b_exit / b_entry - 1.0
    return L1H7Result(entry_date=entry_d, exit_date=exit_d, r_stock=stock_ret, r_bench=bench_ret, r_adj=stock_ret - float(bench_beta) * bench_ret)


# ---------------------------------------------------------------------------
# WMA20 bounce-confirm indicator (verbatim logic from the 2327 study script)
# ---------------------------------------------------------------------------
def load_close_bars_multi(conn: sqlite3.Connection, stock_ids: list[str]) -> dict[str, pd.DataFrame]:
    """Load continuous 5m close series (09:00-13:30) for many stocks in one query."""
    out: dict[str, pd.DataFrame] = {}
    if not stock_ids:
        return out
    chunk = 200
    frames = []
    for i in range(0, len(stock_ids), chunk):
        part = stock_ids[i : i + chunk]
        ph = ",".join("?" * len(part))
        q = (
            f"SELECT stock_id, trade_date, substr(minute,1,5) AS minute, close "
            f"FROM stock_kbar_5m WHERE stock_id IN ({ph})"
        )
        frames.append(pd.read_sql(q, conn, params=part))
    df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=["stock_id", "trade_date", "minute", "close"])
    df = df[(df["minute"] >= SESSION_MINUTE_LO) & (df["minute"] <= SESSION_MINUTE_HI)]
    df = df.drop_duplicates(["stock_id", "trade_date", "minute"], keep="last")
    df = df.sort_values(["stock_id", "trade_date", "minute"]).reset_index(drop=True)
    df["close"] = df["close"].astype(float)
    for sid, g in df.groupby("stock_id", sort=False):
        out[str(sid)] = g.reset_index(drop=True)
    return out


def _first_true_consecutive_end(mask: pd.Series, *, bars: int) -> pd.Series:
    k = max(1, int(bars))
    if k == 1:
        return mask.fillna(False).astype(bool)
    m = mask.fillna(False).astype(bool)
    run = m.copy()
    for shift in range(1, k):
        run = run & m.shift(shift, fill_value=False)
    return run


def _edge(state: pd.Series) -> pd.Series:
    s = state.fillna(False).astype(bool)
    return s & ~s.shift(1, fill_value=False)


def build_confirm_flag_by_date(bars: pd.DataFrame) -> dict[str, bool]:
    """Per trade_date: did and_persist_and_buffer edge fire at any point during session?"""
    if bars.empty:
        return {}
    out = bars.copy()
    out["wma20"] = wma(out["close"], WMA_LENGTH)
    above = out["close"] > out["wma20"]
    buffer_state = out["close"] > out["wma20"] * (1.0 + BUFFER_PCT)
    persist_state = _first_true_consecutive_end(above, bars=PERSISTENCE_BARS)
    and_state = persist_state & buffer_state
    out["and_edge"] = _edge(and_state)
    return out.groupby("trade_date")["and_edge"].any().to_dict()


# ---------------------------------------------------------------------------
# Permutation test (two-sample label shuffle)
# ---------------------------------------------------------------------------
def permutation_test_two_sample(a: np.ndarray, b: np.ndarray, *, n_perm: int, seed: int) -> dict:
    """Shuffle group labels across pooled values; empirical two-sided p for mean & median diff."""
    rng = np.random.default_rng(seed)
    obs_mean_diff = float(np.mean(a) - np.mean(b))
    obs_median_diff = float(np.median(a) - np.median(b))
    pooled = np.concatenate([a, b])
    n_a = len(a)
    perm_mean_diffs = np.empty(n_perm)
    perm_median_diffs = np.empty(n_perm)
    for p in range(n_perm):
        idx = rng.permutation(len(pooled))
        pa = pooled[idx[:n_a]]
        pb = pooled[idx[n_a:]]
        perm_mean_diffs[p] = np.mean(pa) - np.mean(pb)
        perm_median_diffs[p] = np.median(pa) - np.median(pb)
    p_mean = float(np.mean(np.abs(perm_mean_diffs) >= abs(obs_mean_diff)))
    p_median = float(np.mean(np.abs(perm_median_diffs) >= abs(obs_median_diff)))
    return {
        "n_perm": n_perm,
        "seed": seed,
        "obs_mean_diff_pp": obs_mean_diff * 100.0,
        "obs_median_diff_pp": obs_median_diff * 100.0,
        "p_mean_two_sided": p_mean,
        "p_median_two_sided": p_median,
    }


def bucket_summary(vals: np.ndarray) -> dict:
    n = len(vals)
    if n == 0:
        return {"n": 0, "win_rate_pct": None, "mean_pct": None, "median_pct": None}
    return {
        "n": int(n),
        "win_rate_pct": round(float(np.mean(vals > 0) * 100.0), 2),
        "mean_pct": round(float(np.mean(vals) * 100.0), 4),
        "median_pct": round(float(np.median(vals) * 100.0), 4),
    }


def run_study(db_path: Path) -> dict:
    conn = connect_ro(db_path)
    try:
        print("collecting D1' events for known seats …", flush=True)
        sig = collect_d1_events(
            conn, ids=list(KNOWN_SEAT_IDS), start=WINDOW_START, end=WINDOW_END,
            abs_floor=ABS_FLOOR, share_floor=SHARE_FLOOR, top_k=TOP_K,
        )
        print(f"D1' rows={len(sig):,}", flush=True)

        # episode dedupe per (branch, stock)
        episodes: list[tuple[str, str, str]] = []  # (branch_id, stock_id, signal_date)
        for (bid, sid), g in sig.groupby(["securities_trader_id", "stock_id"], sort=False):
            dates = episode_first_dates(g["trade_date"].astype(str).tolist())
            for d in dates:
                episodes.append((str(bid), str(sid), d))
        n_episodes = len(episodes)
        print(f"episode events={n_episodes:,}", flush=True)

        cal = build_calendar(conn, WINDOW_START, BAR_END)
        idx = {d: i for i, d in enumerate(cal)}
        stock_ids = sorted({sid for _, sid, _ in episodes})
        ohlc = load_ohlc(conn, stock_ids, WINDOW_START, BAR_END)
        benchmark_ohlc = load_benchmark_ohlc(conn, BENCHMARK, WINDOW_START, BAR_END)

        print("computing L1H7 returns …", flush=True)
        records = []
        for bid, sid, d in episodes:
            res = l1h7_one(d, sid, cal=cal, idx=idx, ohlc=ohlc, benchmark_ohlc=benchmark_ohlc, bench_beta=BENCH_BETA)
            if res is None:
                continue
            records.append({
                "branch_id": bid, "branch_name": KNOWN_SEAT_NAMES.get(bid, bid),
                "stock_id": sid, "signal_date": d,
                "entry_date": res.entry_date, "exit_date": res.exit_date,
                "r_stock": res.r_stock, "r_bench": res.r_bench, "r_adj": res.r_adj,
            })
        n_evaluable = len(records)
        print(f"L1H7-evaluable events={n_evaluable:,}", flush=True)

        # which stocks have 5m coverage
        avail_stocks = pd.read_sql(
            "SELECT DISTINCT stock_id FROM stock_kbar_5m WHERE stock_id IN ({})".format(
                ",".join("?" * len(stock_ids))
            ),
            conn, params=stock_ids,
        )["stock_id"].astype(str).tolist() if stock_ids else []
        avail_set = set(avail_stocks)
        print(f"stocks with 5m coverage: {len(avail_set)}/{len(stock_ids)}", flush=True)

        kept = [r for r in records if r["stock_id"] in avail_set]
        dropped_no_5m = n_evaluable - len(kept)
        print(f"events with 5m-covered stock: {len(kept):,} (dropped {dropped_no_5m:,} no-5m-coverage)", flush=True)

        need_stocks = sorted({r["stock_id"] for r in kept})
        print(f"loading 5m bars for {len(need_stocks)} stocks …", flush=True)
        t0 = time.time()
        bars_by_stock = load_close_bars_multi(conn, need_stocks)
        print(f"  loaded in {time.time()-t0:.1f}s", flush=True)

        print("building confirm flags …", flush=True)
        confirm_by_stock: dict[str, dict[str, bool]] = {}
        for sid, bars in bars_by_stock.items():
            confirm_by_stock[sid] = build_confirm_flag_by_date(bars)

        final = []
        dropped_no_entry_bar = 0
        for r in kept:
            flags = confirm_by_stock.get(r["stock_id"], {})
            if r["entry_date"] not in flags:
                dropped_no_entry_bar += 1
                continue
            r2 = dict(r)
            r2["confirmed"] = bool(flags[r["entry_date"]])
            final.append(r2)
        print(f"final analyzable events={len(final):,} (dropped {dropped_no_entry_bar:,} missing 5m bar on entry day)", flush=True)

    finally:
        conn.close()

    df = pd.DataFrame(final)
    if df.empty:
        raise SystemExit("no analyzable events — check DB coverage")

    confirmed = df[df["confirmed"]]
    not_confirmed = df[~df["confirmed"]]

    r_adj_confirmed = confirmed["r_adj"].to_numpy(dtype=float)
    r_adj_not = not_confirmed["r_adj"].to_numpy(dtype=float)

    perm = None
    if len(r_adj_confirmed) >= 2 and len(r_adj_not) >= 2:
        perm = permutation_test_two_sample(r_adj_confirmed, r_adj_not, n_perm=N_PERM, seed=PERM_SEED)

    by_branch = (
        df.groupby(["branch_id", "branch_name", "confirmed"])["r_adj"]
        .agg(n="count", median_pct=lambda s: float(np.median(s) * 100.0), win_rate_pct=lambda s: float(np.mean(s > 0) * 100.0))
        .reset_index()
    )

    payload = {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "protocol": {
            "seats": {bid: KNOWN_SEAT_NAMES[bid] for bid in KNOWN_SEAT_IDS},
            "d1_event": {"abs_floor_twd": ABS_FLOOR, "share_floor": SHARE_FLOOR, "top_k": TOP_K, "episode_gap_days": EPISODE_GAP},
            "l1h7": {"hold_days": HOLD, "cost_bps": int(COST * 1e4), "benchmark": BENCHMARK, "bench_beta": BENCH_BETA},
            "wma20_bounce_confirm": {
                "wma_length": WMA_LENGTH, "buffer_pct": BUFFER_PCT, "persistence_bars": PERSISTENCE_BARS,
                "session": f"{SESSION_MINUTE_LO}-{SESSION_MINUTE_HI}",
                "variant": "and_persist_and_buffer (same winning variant as 2327 study)",
                "checked_on": "L1H7 entry day (T+1), edge fired anywhere in session",
            },
            "window": {"start": WINDOW_START, "end": WINDOW_END, "bar_end": BAR_END},
        },
        "coverage": {
            "d1_rows": int(len(sig)),
            "episode_events": n_episodes,
            "l1h7_evaluable": n_evaluable,
            "dropped_no_5m_coverage_stock": int(dropped_no_5m),
            "dropped_no_5m_bar_on_entry_day": int(dropped_no_entry_bar),
            "final_analyzable": int(len(df)),
            "n_distinct_stocks_final": int(df["stock_id"].nunique()),
        },
        "buckets": {
            "confirmed": bucket_summary(r_adj_confirmed),
            "not_confirmed": bucket_summary(r_adj_not),
            "all": bucket_summary(df["r_adj"].to_numpy(dtype=float)),
        },
        "permutation_test_r_adj": perm,
        "by_branch": by_branch.to_dict(orient="records"),
    }
    return payload, df


def render_md(payload: dict) -> str:
    b = payload["buckets"]
    cov = payload["coverage"]
    perm = payload["permutation_test_r_adj"]
    lines = [
        "# WMA20 反彈確認濾器 · 分點跟單候選宇宙 generalization test",
        "",
        f"- 候選宇宙：分點 {list(payload['protocol']['seats'].values())}（{list(payload['protocol']['seats'].keys())}）",
        f"- 窗口：{payload['protocol']['window']['start']} ~ {payload['protocol']['window']['end']}",
        f"- D1' 事件：buy_amt≥{ABS_FLOOR/1e8:.2f}億 且 (share≥{SHARE_FLOOR:.0%} 或 rank≤{TOP_K})；"
        f"episode {EPISODE_GAP} 曆日去重；L1H{HOLD}、成本{int(COST*1e4)}bps、β={BENCH_BETA}×{BENCHMARK}",
        f"- 濾器：WMA{WMA_LENGTH} AND persist({PERSISTENCE_BARS}根)+buffer({BUFFER_PCT*100:.1f}%)，"
        "edge-triggered，於 L1H7 進場日（T+1）盤中檢查（照搬 2327 study 的 and_persist_and_buffer variant）",
        "",
        "## 覆蓋",
        "",
        f"- D1' 原始列：{cov['d1_rows']:,}",
        f"- episode 後事件：{cov['episode_events']:,}",
        f"- L1H7 可評估：{cov['l1h7_evaluable']:,}",
        f"- 排除（股票無 5m 覆蓋）：{cov['dropped_no_5m_coverage_stock']:,}",
        f"- 排除（進場日無 5m bar）：{cov['dropped_no_5m_bar_on_entry_day']:,}",
        f"- **最終可分析事件**：{cov['final_analyzable']:,}（{cov['n_distinct_stocks_final']} 檔股票）",
        "",
        "## Confirmed vs Not-confirmed（L1H7 r_adj，β=1.25 超額）",
        "",
        "| bucket | n | win_rate% | mean_r_adj% | median_r_adj% |",
        "|---|---:|---:|---:|---:|",
        f"| confirmed | {b['confirmed']['n']} | {b['confirmed']['win_rate_pct']} | {b['confirmed']['mean_pct']} | {b['confirmed']['median_pct']} |",
        f"| not_confirmed | {b['not_confirmed']['n']} | {b['not_confirmed']['win_rate_pct']} | {b['not_confirmed']['mean_pct']} | {b['not_confirmed']['median_pct']} |",
        f"| all | {b['all']['n']} | {b['all']['win_rate_pct']} | {b['all']['mean_pct']} | {b['all']['median_pct']} |",
        "",
    ]
    if perm:
        lines += [
            "## Permutation test（label-shuffle, n_perm={}, seed={}）".format(perm["n_perm"], perm["seed"]),
            "",
            f"- observed mean diff (confirmed − not_confirmed): {perm['obs_mean_diff_pp']:+.4f}pp · p={perm['p_mean_two_sided']:.4f}",
            f"- observed median diff (confirmed − not_confirmed): {perm['obs_median_diff_pp']:+.4f}pp · p={perm['p_median_two_sided']:.4f}",
            "",
        ]
    else:
        lines += ["## Permutation test", "", "SKIPPED（某一組樣本數<2）", ""]

    lines += ["## 按分點拆解", "", "| 分點 | confirmed | n | median_r_adj% | win_rate% |", "|---|---|---:|---:|---:|"]
    for row in payload["by_branch"]:
        lines.append(
            f"| {row['branch_name']} (`{row['branch_id']}`) | {row['confirmed']} | {row['n']} | "
            f"{row['median_pct']:.2f} | {row['win_rate_pct']:.1f} |"
        )
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="WMA20 bounce-confirm filter generalization test (research)")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args(argv)

    if not args.db.exists():
        print(f"BLOCKER: database not found: {args.db}", file=sys.stderr)
        return 2

    payload, df = run_study(args.db)
    md = render_md(payload)
    print(md)

    if args.write:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d")
        out = OUT_DIR / f"{stamp}_wma20_bounce_branch_follow_generalize"
        out.with_suffix(".json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        out.with_suffix(".md").write_text(md + "\n", encoding="utf-8")
        df.to_csv(out.with_suffix(".events.csv"), index=False)
        print(f"\nwrote {out.with_suffix('.json')}")
        print(f"wrote {out.with_suffix('.md')}")
        print(f"wrote {out.with_suffix('.events.csv')}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
