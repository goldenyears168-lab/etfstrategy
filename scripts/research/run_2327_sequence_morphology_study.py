#!/usr/bin/env python3
"""2327 國巨 · sequence morphology reverse-engineering vs Whale_T+1.

Outcome-based labels (not consensus greens) → mine seat/chip sequences on
D−1/D (T−2/T−1 vs entry E=D+1) by lift → standalone calendar backtest.

Research only · 未採納 · Book only · no Whale-subset filters.

    PYTHONPATH=src .venv/bin/python scripts/research/run_2327_sequence_morphology_study.py
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from itertools import combinations
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from research.chen_chip.adapters_db import (  # noqa: E402
    connect_ro,
    load_bench,
    load_calendar,
    load_ohlc,
)
from research.chen_chip.features import build_chip_feature_frame  # noqa: E402
from research.chen_chip.whale_events import load_whale_events  # noqa: E402
from stock_db import DEFAULT_DB_PATH  # noqa: E402

SID = "2327"
OUT = ROOT / "reports/research/whale_chip_precursor"
CACHE = OUT / "cache"
HS_PATH = CACHE / "holding_shares_per_tier_a.csv"
GOV_PATH = CACHE / "gov_bank_net_tier_a.csv"
TRADES_DIR = (
    ROOT
    / "reports/research/branch-footprint-screen/expert_pool/knowledge/trades"
)
SOURCE = "finmind"
D0, D1, OOS = "2024-07-01", "2026-07-16", "2026-01-01"
COST, BETA = 0.003, 1.15
HOLDS = (5, 7, 10)
PRIMARY_HOLD = 7
N_BOOT = 5000
RNG = np.random.default_rng(42)
FLOOR_0P5 = 5e7
FLOOR_1 = 1e8
EXTRA_SEATS = ("9100", "9665", "9600", "9869", "989P")
PRIOR_FROZEN = "br__pair_918e_9665__both__net"
MIN_IS_N = 8
MIN_LIFT_N_GOOD = 5
TOP_BT = 50
FOREIGN_KW = (
    "美商",
    "摩根",
    "瑞銀",
    "美林",
    "港商",
    "港麥",
    "麥格理",
    "野村",
    "瑞士",
    "花旗",
    "渣打",
    "香港",
    "新加坡",
    "大和",
    "法銀",
    "德意志",
    "巴克萊",
    "高盛",
    "美銀",
)


def log(m: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {m}", flush=True)


def is_foreign(name: str) -> bool:
    return any(k in (name or "") for k in FOREIGN_KW)


# ---- returns ------------------------------------------------------------


def excess_l1(
    signal_d: str,
    hold: int,
    cal: list[str],
    di: dict[str, int],
    ohlc_px: dict[tuple[str, str], tuple[float, float]],
    bench: dict[str, float],
) -> tuple[float | None, str | None, str | None]:
    i = di.get(signal_d)
    if i is None or i + 1 >= len(cal) or i + hold >= len(cal):
        return None, None, None
    ed = cal[i + 1]
    xd = cal[i + hold]
    er = ohlc_px.get((SID, ed))
    xr = ohlc_px.get((SID, xd))
    be, bx = bench.get(ed), bench.get(xd)
    if not er or not xr or not be or not bx:
        return None, None, None
    eo, _ = er
    _, xp = xr
    if eo <= 0 or xp <= 0 or be <= 0 or bx <= 0:
        return None, None, None
    sr = xp / eo - 1.0 - COST
    br = bx / be - 1.0
    return (sr - BETA * br) * 100.0, ed, xd


def summarize(xs: list[float]) -> dict:
    a = np.asarray(xs, dtype=float)
    n = int(a.size)
    if n == 0:
        return {
            "n": 0,
            "hit_rate": np.nan,
            "med": np.nan,
            "mean": np.nan,
            "sum": np.nan,
        }
    return {
        "n": n,
        "hit_rate": float((a > 0).mean()),
        "med": float(np.median(a)),
        "mean": float(a.mean()),
        "sum": float(a.sum()),
    }


def bootstrap_delta(a: list[float], b: list[float]) -> dict:
    aa = np.asarray(a, dtype=float)
    bb = np.asarray(b, dtype=float)
    if aa.size == 0 or bb.size == 0:
        return {
            "delta_mean": np.nan,
            "ci_lo": np.nan,
            "ci_hi": np.nan,
            "p_delta_le0": np.nan,
            "n_a": int(aa.size),
            "n_b": int(bb.size),
        }
    d0 = float(aa.mean() - bb.mean())
    if aa.size < 5 or bb.size < 5:
        return {
            "delta_mean": d0,
            "ci_lo": np.nan,
            "ci_hi": np.nan,
            "p_delta_le0": np.nan,
            "n_a": int(aa.size),
            "n_b": int(bb.size),
        }
    idx_a = RNG.integers(0, aa.size, size=(N_BOOT, aa.size))
    idx_b = RNG.integers(0, bb.size, size=(N_BOOT, bb.size))
    deltas = aa[idx_a].mean(axis=1) - bb[idx_b].mean(axis=1)
    return {
        "delta_mean": d0,
        "ci_lo": float(np.percentile(deltas, 2.5)),
        "ci_hi": float(np.percentile(deltas, 97.5)),
        "p_delta_le0": float((deltas <= 0).mean()),
        "n_a": int(aa.size),
        "n_b": int(bb.size),
    }


def fmt_pct(v: float | None, digits: int = 1) -> str:
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "—"
    return f"{v:+.{digits}f}"


def fmt_rate(v: float | None) -> str:
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "—"
    return f"{100.0 * v:.0f}%"


def fmt_p(v: float | None) -> str:
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "—"
    return f"{v:.2f}"


# ---- tape ---------------------------------------------------------------


def fetch_tape(conn, dates: list[str]) -> pd.DataFrame:
    if not dates:
        return pd.DataFrame(
            columns=["d", "sid", "tid", "tname", "buy", "sell", "net", "amt"]
        )
    uniq = sorted(set(dates))
    parts: list[pd.DataFrame] = []
    chunk = 40
    for i in range(0, len(uniq), chunk):
        batch = uniq[i : i + chunk]
        ph = ",".join("?" * len(batch))
        df = pd.read_sql_query(
            f"""
            SELECT trade_date AS d, stock_id AS sid,
                   securities_trader_id AS tid, securities_trader AS tname,
                   buy, sell, net
            FROM stock_broker_branch_daily
            WHERE source=? AND stock_id=? AND trade_date IN ({ph})
            """,
            conn,
            params=[SOURCE, SID, *batch],
        )
        if not df.empty:
            parts.append(df)
    if not parts:
        return pd.DataFrame(
            columns=["d", "sid", "tid", "tname", "buy", "sell", "net", "amt"]
        )
    out = pd.concat(parts, ignore_index=True)
    for c in ("buy", "sell", "net"):
        out[c] = pd.to_numeric(out[c], errors="coerce").fillna(0.0)
    out["sid"] = out["sid"].astype(str)
    out["tid"] = out["tid"].astype(str)
    out["d"] = out["d"].astype(str)
    return out


def attach_amt(tape: pd.DataFrame, px: dict[tuple[str, str], float]) -> pd.DataFrame:
    t = tape.copy()
    t["close"] = [px.get((r.sid, r.d), np.nan) for r in t.itertuples()]
    t["amt"] = t["net"] * t["close"]
    return t


def lag_date(cal: list[str], di: dict[str, int], sig: str, k: int) -> str | None:
    i = di.get(sig)
    if i is None or i < k:
        return None
    return cal[i - k]


def enrich_sums(feat: pd.DataFrame) -> pd.DataFrame:
    g = feat.sort_values(["sid", "d"]).copy()
    if "lending_drop" not in g.columns:
        g["lending_drop"] = (
            (g.get("lending_drop_streak", pd.Series(0, index=g.index)).fillna(0) >= 1)
            | (g.get("lending_change", pd.Series(0, index=g.index)).fillna(0) < 0)
        ).astype(int)
    return g


# ---- trading ------------------------------------------------------------


def standalone_trades(
    fire_days: set[str],
    hold: int,
    cal: list[str],
    di: dict[str, int],
    ohlc_px: dict,
    bench: dict,
    arm: str,
    rule: str,
    day_lo: str,
    day_hi: str,
) -> list[dict]:
    rows: list[dict] = []
    next_free = -1
    for j, d in enumerate(cal):
        if d < day_lo or d > day_hi:
            continue
        if d not in fire_days:
            continue
        if j < next_free:
            continue
        x, ed, xd = excess_l1(d, hold, cal, di, ohlc_px, bench)
        if x is None or ed is None or xd is None:
            continue
        exit_j = di.get(xd)
        if exit_j is None:
            continue
        next_free = exit_j + 1
        rows.append(
            {
                "sid": SID,
                "signal_date": d,
                "entry_date": ed,
                "exit_date": xd,
                "arm": arm,
                "rule": rule,
                "hold": hold,
                "excess_pct": x,
                "split": "IS" if d < OOS else "OOS",
            }
        )
    return rows


def metrics_block(
    trades: list[dict],
    whale_xs: list[float],
    split: str,
) -> dict:
    if split == "full":
        xs = [t["excess_pct"] for t in trades]
    elif split == "IS":
        xs = [t["excess_pct"] for t in trades if t["split"] == "IS"]
    else:
        xs = [t["excess_pct"] for t in trades if t["split"] == "OOS"]
    sm = summarize(xs)
    boot = bootstrap_delta(xs, whale_xs) if xs and whale_xs else {
        "delta_mean": np.nan,
        "ci_lo": np.nan,
        "ci_hi": np.nan,
        "p_delta_le0": np.nan,
        "n_a": sm["n"],
        "n_b": len(whale_xs),
    }
    return {
        "split": split,
        **sm,
        "delta_vs_whale": boot["delta_mean"],
        "ci_lo": boot["ci_lo"],
        "ci_hi": boot["ci_hi"],
        "p_le0": boot["p_delta_le0"],
        "whale_n": len(whale_xs),
        "whale_mean": float(np.mean(whale_xs)) if whale_xs else np.nan,
    }


def win_criteria(
    full: dict,
    oos: dict,
    whale_full_mean: float,
    whale_oos_mean: float,
) -> tuple[bool, str]:
    """Return (met, which)."""
    # A: OOS mean ≥ Whale OOS AND OOS n≥5
    if (
        oos["n"] >= 5
        and pd.notna(oos["mean"])
        and pd.notna(whale_oos_mean)
        and oos["mean"] >= whale_oos_mean
    ):
        return True, "OOS_mean≥Whale_OOS_n≥5"
    # B: Full mean ≥ Whale full AND P(Δ≤0)≤0.15 AND n≥12
    if (
        full["n"] >= 12
        and pd.notna(full["mean"])
        and pd.notna(whale_full_mean)
        and full["mean"] >= whale_full_mean
        and pd.notna(full["p_le0"])
        and full["p_le0"] <= 0.15
    ):
        return True, "full_mean≥Whale_P≤0.15_n≥12"
    # C: Full sum clearly higher with mean not much worse (≤2pp worse)
    if (
        full["n"] >= 12
        and pd.notna(full["sum"])
        and pd.notna(full["mean"])
        and pd.notna(whale_full_mean)
        and full["sum"] >= whale_full_mean * 12 * 1.15  # rough: whale sum ~94
        and full["mean"] >= whale_full_mean - 2.0
    ):
        # need whale sum for honest compare — pass via full["whale_sum"] if set
        wsum = full.get("whale_sum")
        if wsum is not None and pd.notna(wsum) and full["sum"] > float(wsum) * 1.10:
            if full["mean"] >= whale_full_mean - 2.0:
                return True, "full_sum↑_mean_not_much_worse"
    return False, ""


def win_criteria_v2(
    full: dict,
    oos: dict,
    ism: dict,
    whale_full: dict,
    whale_oos: dict,
) -> tuple[bool, str]:
    """Honest win: OOS criterion requires IS mean≥0 (anti-regime-luck)."""
    is_ok = (
        ism.get("n", 0) >= MIN_IS_N
        and pd.notna(ism.get("mean"))
        and float(ism["mean"]) >= 0.0
    )
    if (
        is_ok
        and oos["n"] >= 5
        and pd.notna(oos["mean"])
        and pd.notna(whale_oos["mean"])
        and oos["mean"] >= whale_oos["mean"]
    ):
        return True, "OOS_mean≥Whale_OOS_n≥5_IS≥0"
    if (
        is_ok
        and full["n"] >= 12
        and pd.notna(full["mean"])
        and pd.notna(whale_full["mean"])
        and full["mean"] >= whale_full["mean"]
        and pd.notna(full["p_le0"])
        and full["p_le0"] <= 0.15
    ):
        return True, "full_mean≥Whale_P≤0.15_n≥12"
    if (
        is_ok
        and full["n"] >= 12
        and pd.notna(full["sum"])
        and pd.notna(whale_full["sum"])
        and pd.notna(full["mean"])
        and pd.notna(whale_full["mean"])
        and full["sum"] > float(whale_full["sum"]) * 1.10
        and full["mean"] >= float(whale_full["mean"]) - 2.0
    ):
        return True, "full_sum↑_mean_not_much_worse"
    return False, ""


# ---- sequence feature construction --------------------------------------


def seat_amt(branch_amt: dict[str, dict[str, float]], d: str | None, tid: str) -> float:
    if not d:
        return 0.0
    return float(branch_amt.get(d, {}).get(tid, 0.0) or 0.0)


def seq_sign(amt: float, floor: float = 0.0) -> str:
    if floor > 0:
        if amt >= floor:
            return "+"
        if amt <= -floor:
            return "-"
        return "0"
    if amt > 0:
        return "+"
    if amt < 0:
        return "-"
    return "0"


def build_day_panel(
    cal: list[str],
    cal_ext: list[str],
    di: dict[str, int],
    seats: list[str],
    branch_amt: dict[str, dict[str, float]],
    feat_idx: pd.DataFrame,
    ohlc_px: dict,
    bench: dict,
    holds: tuple[int, ...],
) -> pd.DataFrame:
    """One row per decision day D with forward excess labels + seat amts."""
    rows: list[dict] = []
    for d in cal:
        dm1 = lag_date(cal_ext, di, d, 1)
        dm2 = lag_date(cal_ext, di, d, 2)
        row: dict = {"d": d, "dm1": dm1, "dm2": dm2, "split": "IS" if d < OOS else "OOS"}
        for h in holds:
            x, ed, xd = excess_l1(d, h, cal_ext, di, ohlc_px, bench)
            row[f"ex_h{h}"] = x
            row[f"entry_h{h}"] = ed
            row[f"exit_h{h}"] = xd
        for tid in seats:
            a0 = seat_amt(branch_amt, d, tid)
            a1 = seat_amt(branch_amt, dm1, tid)
            a2 = seat_amt(branch_amt, dm2, tid)
            row[f"amt_{tid}_d"] = a0
            row[f"amt_{tid}_dm1"] = a1
            row[f"amt_{tid}_dm2"] = a2
        # chip path D and D−1
        for label, day in (("d", d), ("dm1", dm1)):
            if not day:
                row[f"ts_{label}"] = np.nan
                row[f"fs_{label}"] = np.nan
                row[f"ss_{label}"] = np.nan
                row[f"bh_{label}"] = np.nan
                continue
            try:
                fr = feat_idx.loc[(SID, day)]
                if isinstance(fr, pd.DataFrame):
                    fr = fr.iloc[0]
                row[f"ts_{label}"] = float(fr.get("three_streak", 0) or 0)
                row[f"fs_{label}"] = float(fr.get("foreign_sum_5d", 0) or 0)
                row[f"ss_{label}"] = float(fr.get("three_sum_5d", 0) or 0)
                bh = fr.get("big_holder_pct_chg", np.nan)
                row[f"bh_{label}"] = float(bh) if pd.notna(bh) else np.nan
            except KeyError:
                row[f"ts_{label}"] = np.nan
                row[f"fs_{label}"] = np.nan
                row[f"ss_{label}"] = np.nan
                row[f"bh_{label}"] = np.nan
        # stock-day rank among seats (by amt)
        amts_d = {t: seat_amt(branch_amt, d, t) for t in seats}
        amts_m = {t: seat_amt(branch_amt, dm1, t) for t in seats} if dm1 else {}
        ranked = sorted(amts_d.items(), key=lambda kv: kv[1], reverse=True)
        for rank_i, (tid, _) in enumerate(ranked, start=1):
            row[f"rank_{tid}_d"] = rank_i
        for tid in seats:
            if f"rank_{tid}_d" not in row:
                row[f"rank_{tid}_d"] = len(seats) + 1
        # champ / multi-seat aggregates
        champ = seats[:5]  # first 5 are core champions when ordered that way
        row["champ_net_d"] = sum(1 for t in champ if amts_d.get(t, 0) > 0)
        row["champ_0p5_d"] = sum(1 for t in champ if amts_d.get(t, 0) >= FLOOR_0P5)
        row["any_0p5_d"] = sum(1 for t in seats if amts_d.get(t, 0) >= FLOOR_0P5)
        row["any_1yi_d"] = sum(1 for t in seats if amts_d.get(t, 0) >= FLOOR_1)
        if dm1:
            row["champ_net_dm1"] = sum(1 for t in champ if amts_m.get(t, 0) > 0)
            row["champ_0p5_dm1"] = sum(1 for t in champ if amts_m.get(t, 0) >= FLOOR_0P5)
        else:
            row["champ_net_dm1"] = 0
            row["champ_0p5_dm1"] = 0
        rows.append(row)
    return pd.DataFrame(rows)


def fire_sequences_iter1(
    panel: pd.DataFrame,
    seats: list[str],
    name_map: dict[str, str],
) -> dict[str, tuple[set[str], str, str]]:
    """seq_id → (fire_days, label, family)."""
    out: dict[str, tuple[set[str], str, str]] = {}

    def add(sid: str, label: str, family: str, mask: pd.Series) -> None:
        days = set(panel.loc[mask, "d"].astype(str).tolist())
        out[sid] = (days, label, family)

    # per-seat 2-day morphologies
    for tid in seats:
        nm = name_map.get(tid, tid)
        a0 = panel[f"amt_{tid}_d"]
        a1 = panel[f"amt_{tid}_dm1"]
        # ++ net>0 both days
        add(
            f"seat_{tid}__pp",
            f"{tid}({nm}): ++ (D−1∧D net>0)",
            "seat_cont",
            (a1 > 0) & (a0 > 0),
        )
        # 0+ : quiet then buy
        add(
            f"seat_{tid}__0p",
            f"{tid}({nm}): 0+ (D−1≤0 → D net>0)",
            "seat_cont",
            (a1 <= 0) & (a0 > 0),
        )
        # +accel
        add(
            f"seat_{tid}__accel",
            f"{tid}({nm}): +accel (D−1>0 ∧ D>D−1)",
            "seat_cont",
            (a1 > 0) & (a0 > a1),
        )
        # +pullback still net
        add(
            f"seat_{tid}__pb_net",
            f"{tid}({nm}): +pullback-still-net (0<D<D−1)",
            "seat_cont",
            (a1 > 0) & (a0 > 0) & (a0 < a1),
        )
        # 0.5億 sequences
        add(
            f"seat_{tid}__0p5_pp",
            f"{tid}({nm}): 0.5億 ++",
            "seat_inten",
            (a1 >= FLOOR_0P5) & (a0 >= FLOOR_0P5),
        )
        add(
            f"seat_{tid}__0p5_d",
            f"{tid}({nm}): ≥0.5億 @D",
            "seat_inten",
            a0 >= FLOOR_0P5,
        )
        add(
            f"seat_{tid}__0p5_dm1",
            f"{tid}({nm}): ≥0.5億 @D−1",
            "seat_inten",
            a1 >= FLOOR_0P5,
        )
        add(
            f"seat_{tid}__1yi_d",
            f"{tid}({nm}): ≥1億 @D",
            "seat_inten",
            a0 >= FLOOR_1,
        )
        add(
            f"seat_{tid}__1yi_pp",
            f"{tid}({nm}): 1億 ++",
            "seat_inten",
            (a1 >= FLOOR_1) & (a0 >= FLOOR_1),
        )
        # top-rank on D
        add(
            f"seat_{tid}__rank1_d",
            f"{tid}({nm}): amt rank#1 @D among seats",
            "seat_rank",
            panel[f"rank_{tid}_d"] == 1,
        )
        add(
            f"seat_{tid}__rank12_d_net",
            f"{tid}({nm}): rank≤2 @D ∧ net>0",
            "seat_rank",
            (panel[f"rank_{tid}_d"] <= 2) & (a0 > 0),
        )

    # multi-seat same-day co-buy (pairs among seats, focused)
    focus = seats[:12]
    for a, b in combinations(focus, 2):
        na, nb = name_map.get(a, a), name_map.get(b, b)
        a0a, a0b = panel[f"amt_{a}_d"], panel[f"amt_{b}_d"]
        a1a, a1b = panel[f"amt_{a}_dm1"], panel[f"amt_{b}_dm1"]
        add(
            f"cobuy_{a}_{b}__d_net",
            f"co-buy {a}+{b} @D net>0",
            "multi",
            (a0a > 0) & (a0b > 0),
        )
        add(
            f"cobuy_{a}_{b}__d_0p5",
            f"co-buy {a}+{b} @D ≥0.5億",
            "multi",
            (a0a >= FLOOR_0P5) & (a0b >= FLOOR_0P5),
        )
        add(
            f"cobuy_{a}_{b}__both_net",
            f"co-buy {a}+{b} D∧D−1 net>0",
            "multi",
            (a0a > 0) & (a0b > 0) & (a1a > 0) & (a1b > 0),
        )
        # staggered: A on D−1, B on D
        add(
            f"stag_{a}_then_{b}",
            f"stagger {a}@D-1→{b}@D net",
            "multi",
            (a1a > 0) & (a0b > 0),
        )
        add(
            f"stag_{b}_then_{a}",
            f"stagger {b}@D-1→{a}@D net",
            "multi",
            (a1b > 0) & (a0a > 0),
        )

    # chip path
    ts_d, ts_m = panel["ts_d"], panel["ts_dm1"]
    fs_d, fs_m = panel["fs_d"], panel["fs_dm1"]
    ss_d, ss_m = panel["ss_d"], panel["ss_dm1"]
    bh_d = panel["bh_d"]
    add("chip_ts_up", "three_streak↑ D−1→D", "chip", (ts_d > ts_m) & ts_d.notna() & ts_m.notna())
    add("chip_ts_ge1", "three_streak≥1 @D", "chip", ts_d >= 1)
    add("chip_ts_ge2", "three_streak≥2 @D", "chip", ts_d >= 2)
    add(
        "chip_ss_turn_pos",
        "three_sum_5d turn+ (D−1≤0→D>0)",
        "chip",
        (ss_m <= 0) & (ss_d > 0),
    )
    add(
        "chip_fs_turn_pos",
        "foreign_sum_5d turn+ (D−1≤0→D>0)",
        "chip",
        (fs_m <= 0) & (fs_d > 0),
    )
    add("chip_ss_pos", "three_sum_5d>0 @D", "chip", ss_d > 0)
    add("chip_bh_pos", "big_holder_pct_chg>0 @D", "chip", bh_d > 0)
    add(
        "chip_ts1_bh",
        "ts≥1 ∧ bhchg>0 @D (prior-like)",
        "chip",
        (ts_d >= 1) & (bh_d > 0),
    )

    # chip × seat combos (top seats only)
    top_seats = seats[:8]
    for tid in top_seats:
        nm = name_map.get(tid, tid)
        a0 = panel[f"amt_{tid}_d"]
        a1 = panel[f"amt_{tid}_dm1"]
        add(
            f"chip_ts_up__seat_{tid}_pp",
            f"ts↑ ∧ {tid}({nm}):++",
            "chip_x_seat",
            (ts_d > ts_m) & (a1 > 0) & (a0 > 0),
        )
        add(
            f"chip_ts_ge1__seat_{tid}_0p5_d",
            f"ts≥1 ∧ {tid}≥0.5億@D",
            "chip_x_seat",
            (ts_d >= 1) & (a0 >= FLOOR_0P5),
        )
        add(
            f"chip_bh__seat_{tid}_accel",
            f"bhchg>0 ∧ {tid}:accel",
            "chip_x_seat",
            (bh_d > 0) & (a1 > 0) & (a0 > a1),
        )
        add(
            f"chip_ss_turn__seat_{tid}_d_net",
            f"ss_turn+ ∧ {tid} net@D",
            "chip_x_seat",
            (ss_m <= 0) & (ss_d > 0) & (a0 > 0),
        )

    # multi-champ intensity
    add("champ_ge2_net_d", "≥2 champion seats net>0 @D", "multi", panel["champ_net_d"] >= 2)
    add(
        "champ_ge2_0p5_d",
        "≥2 champion seats ≥0.5億 @D",
        "multi",
        panel["champ_0p5_d"] >= 2,
    )
    add(
        "champ_net_pp",
        "≥1 champ net D∧D−1",
        "multi",
        (panel["champ_net_d"] >= 1) & (panel["champ_net_dm1"] >= 1),
    )
    add(
        "any_0p5_pp",
        "any seat ≥0.5億 D∧D−1",
        "multi",
        (panel["any_0p5_d"] >= 1)
        & (panel[[f"amt_{t}_dm1" for t in seats]].ge(FLOOR_0P5).any(axis=1)),
    )
    add("any_1yi_d", "any seat ≥1億 @D", "multi", panel["any_1yi_d"] >= 1)

    # prior frozen as control arm (918e+9665 both days net)
    if "918e" in seats and "9665" in seats:
        add(
            PRIOR_FROZEN,
            "prior freeze: 918e+9665 D∧D−1 net>0",
            "prior_frozen",
            (panel["amt_918e_d"] > 0)
            & (panel["amt_9665_d"] > 0)
            & (panel["amt_918e_dm1"] > 0)
            & (panel["amt_9665_dm1"] > 0),
        )

    return out


def seat_amt_row(r: pd.Series, tid: str, which: str) -> float:
    return float(r.get(f"amt_{tid}_{which}", 0) or 0)


def fire_sequences_iter2(
    panel: pd.DataFrame,
    seats: list[str],
    name_map: dict[str, str],
) -> dict[str, tuple[set[str], str, str]]:
    """Widen: 3-day sequences, more thr, accumulation episodes, early-path."""
    out: dict[str, tuple[set[str], str, str]] = {}

    def add(sid: str, label: str, family: str, mask: pd.Series) -> None:
        days = set(panel.loc[mask, "d"].astype(str).tolist())
        out[sid] = (days, label, family)

    focus = seats[:10]
    for tid in focus:
        nm = name_map.get(tid, tid)
        a0 = panel[f"amt_{tid}_d"]
        a1 = panel[f"amt_{tid}_dm1"]
        a2 = panel[f"amt_{tid}_dm2"]
        # +++ three-day buy
        add(
            f"seat_{tid}__ppp",
            f"{tid}({nm}): +++ (D−2..D net>0)",
            "seat_3d",
            (a2 > 0) & (a1 > 0) & (a0 > 0),
        )
        # rising 3d
        add(
            f"seat_{tid}__rise3",
            f"{tid}({nm}): rising 3d (D−2<D−1<D, all>0)",
            "seat_3d",
            (a2 > 0) & (a1 > a2) & (a0 > a1),
        )
        # accumulate then hold (2–5d proxy: dm2..d all net, peak earlier)
        add(
            f"seat_{tid}__accum_hold",
            f"{tid}({nm}): accum then hold (+++ ∧ D≤D−1)",
            "seat_3d",
            (a2 > 0) & (a1 > 0) & (a0 > 0) & (a0 <= a1),
        )
        # 0.5億 then continue net
        add(
            f"seat_{tid}__0p5_then_net",
            f"{tid}({nm}): ≥0.5億@D−1 → net@D",
            "seat_3d",
            (a1 >= FLOOR_0P5) & (a0 > 0),
        )
        # break path (for early-exit style: fire on ++ but NOT if D drops to sell)
        # entry rule: ++ with D still ≥0.5*D−1
        add(
            f"seat_{tid}__pp_stable",
            f"{tid}({nm}): ++ stable (D≥0.5×D−1)",
            "seat_3d",
            (a1 > 0) & (a0 > 0) & (a0 >= 0.5 * a1),
        )

    # broader multi
    for a, b in combinations(focus[:8], 2):
        a0a, a0b = panel[f"amt_{a}_d"], panel[f"amt_{b}_d"]
        a1a, a1b = panel[f"amt_{a}_dm1"], panel[f"amt_{b}_dm1"]
        a2a, a2b = panel[f"amt_{a}_dm2"], panel[f"amt_{b}_dm2"]
        add(
            f"stag3_{a}_then_{b}",
            f"3d stagger {a}@D-2 → {b}@D net",
            "multi_3d",
            (a2a > 0) & (a0b > 0),
        )
        add(
            f"cobuy_either_0p5_pp_{a}_{b}",
            f"{a}+{b} either≥0.5 both days",
            "multi_3d",
            ((a0a >= FLOOR_0P5) | (a0b >= FLOOR_0P5))
            & ((a1a >= FLOOR_0P5) | (a1b >= FLOOR_0P5)),
        )

    # chip × 3d
    ts_d, ts_m = panel["ts_d"], panel["ts_dm1"]
    ss_d, ss_m = panel["ss_d"], panel["ss_dm1"]
    bh_d = panel["bh_d"]
    for tid in focus[:6]:
        nm = name_map.get(tid, tid)
        a0 = panel[f"amt_{tid}_d"]
        a1 = panel[f"amt_{tid}_dm1"]
        a2 = panel[f"amt_{tid}_dm2"]
        add(
            f"chip_ts2__seat_{tid}_ppp",
            f"ts≥2 ∧ {tid}:+++",
            "chip_x_3d",
            (ts_d >= 2) & (a2 > 0) & (a1 > 0) & (a0 > 0),
        )
        add(
            f"chip_bh__seat_{tid}_rise3",
            f"bhchg>0 ∧ {tid}:rise3",
            "chip_x_3d",
            (bh_d > 0) & (a2 > 0) & (a1 > a2) & (a0 > a1),
        )
        add(
            f"chip_ss_pos__seat_{tid}_0p5_then",
            f"ss>0 ∧ {tid}:0.5→net",
            "chip_x_3d",
            (ss_d > 0) & (a1 >= FLOOR_0P5) & (a0 > 0),
        )
        add(
            f"chip_ts_up__seat_{tid}_0p5_d",
            f"ts↑ ∧ {tid}≥0.5億@D",
            "chip_x_3d",
            (ts_d > ts_m) & (a0 >= FLOOR_0P5),
        )

    # accumulation episode proxy: any seat with rising buy 2–3d then good —
    # as entry: any of focus seats rise3 OR (pp & accel)
    rise_any = pd.Series(False, index=panel.index)
    for tid in focus:
        a0 = panel[f"amt_{tid}_d"]
        a1 = panel[f"amt_{tid}_dm1"]
        a2 = panel[f"amt_{tid}_dm2"]
        rise_any = rise_any | ((a2 > 0) & (a1 > a2) & (a0 > a1))
    add("any_seat_rise3", "any focus seat rising 3d", "episode", rise_any)

    pp_accel_any = pd.Series(False, index=panel.index)
    for tid in focus:
        a0 = panel[f"amt_{tid}_d"]
        a1 = panel[f"amt_{tid}_dm1"]
        pp_accel_any = pp_accel_any | ((a1 > 0) & (a0 > a1))
    add("any_seat_accel", "any focus seat +accel", "episode", pp_accel_any)

    # chip turn + any accel
    add(
        "chip_ss_turn__any_accel",
        "ss_turn+ ∧ any seat accel",
        "episode",
        (ss_m <= 0) & (ss_d > 0) & pp_accel_any,
    )
    add(
        "chip_ts_up__any_0p5_d",
        "ts↑ ∧ any seat ≥0.5億@D",
        "episode",
        (ts_d > ts_m) & (panel["any_0p5_d"] >= 1),
    )
    add(
        "chip_bh__champ_ge2_net",
        "bhchg>0 ∧ ≥2 champ net@D",
        "episode",
        (bh_d > 0) & (panel["champ_net_d"] >= 2),
    )

    return out


def fire_sequences_iter3(
    panel: pd.DataFrame,
    seats: list[str],
    name_map: dict[str, str],
    good_mask_col: str,
) -> dict[str, tuple[set[str], str, str]]:
    """Third pass: intensity ∩ continuity ∩ chip; IS-p75 amt on ++."""
    out: dict[str, tuple[set[str], str, str]] = {}

    def add(sid: str, label: str, family: str, mask: pd.Series) -> None:
        days = set(panel.loc[mask, "d"].astype(str).tolist())
        out[sid] = (days, label, family)

    ts_d, ts_m = panel["ts_d"], panel["ts_dm1"]
    ss_d, ss_m = panel["ss_d"], panel["ss_dm1"]
    bh_d = panel["bh_d"]
    focus = seats[:10]
    is_mask = panel["split"] == "IS"

    # dual-seat accel / ++ with floor
    for a, b in combinations(focus[:7], 2):
        aa0, ab0 = panel[f"amt_{a}_d"], panel[f"amt_{b}_d"]
        aa1, ab1 = panel[f"amt_{a}_dm1"], panel[f"amt_{b}_dm1"]
        add(
            f"dual_accel_{a}_{b}",
            f"dual accel {a}&{b}",
            "tight",
            (aa1 > 0) & (aa0 > aa1) & (ab1 > 0) & (ab0 > ab1),
        )
        add(
            f"dual_pp_0p5_{a}_{b}",
            f"dual ++ one≥0.5 {a}/{b}",
            "tight",
            (aa1 > 0)
            & (aa0 > 0)
            & (ab1 > 0)
            & (ab0 > 0)
            & ((aa0 >= FLOOR_0P5) | (ab0 >= FLOOR_0P5)),
        )
        add(
            f"accel_0p5_{a}_cobuy_{b}",
            f"{a}:accel≥0.5 ∧ {b} net@D",
            "tight",
            (aa1 > 0) & (aa0 > aa1) & (aa0 >= FLOOR_0P5) & (ab0 > 0),
        )

    add(
        "any_1yi_pp",
        "any seat ≥1億 D∧D−1",
        "tight",
        panel[[f"amt_{t}_d" for t in seats]].ge(FLOOR_1).any(axis=1)
        & panel[[f"amt_{t}_dm1" for t in seats]].ge(FLOOR_1).any(axis=1),
    )
    add(
        "champ_0p5_both_days",
        "champ ≥0.5億 D∧D−1",
        "tight",
        (panel["champ_0p5_d"] >= 1) & (panel["champ_0p5_dm1"] >= 1),
    )
    add(
        "champ_0p5_d__ts_ge2",
        "champ≥0.5@D ∧ ts≥2",
        "tight",
        (panel["champ_0p5_d"] >= 1) & (ts_d >= 2),
    )
    add(
        "champ_0p5_d__ss_turn",
        "champ≥0.5@D ∧ ss_turn+",
        "tight",
        (panel["champ_0p5_d"] >= 1) & (ss_m <= 0) & (ss_d > 0),
    )
    add(
        "champ_ge2_0p5__bh",
        "≥2 champ≥0.5@D ∧ bhchg>0",
        "tight",
        (panel["champ_0p5_d"] >= 2) & (bh_d > 0),
    )
    add(
        "champ_0p5_accel_any",
        "any champ accel ∧ ≥0.5@D",
        "tight",
        _any_champ_accel_0p5(panel, seats[:5]),
    )

    # IS-p75 amt on ++ / accel (threshold fit on IS only)
    for tid in focus:
        nm = name_map.get(tid, tid)
        a0 = panel[f"amt_{tid}_d"]
        a1 = panel[f"amt_{tid}_dm1"]
        pos_is = a0[is_mask & (a0 > 0)]
        thr75 = float(pos_is.quantile(0.75)) if len(pos_is) >= 20 else FLOOR_0P5
        thr90 = float(pos_is.quantile(0.90)) if len(pos_is) >= 30 else FLOOR_1
        add(
            f"seat_{tid}__pp_hi",
            f"{tid}({nm}): ++ ∧ D≥IS-p75({thr75/1e8:.2f}億)",
            "tight",
            (a1 > 0) & (a0 >= thr75),
        )
        add(
            f"seat_{tid}__accel_hi",
            f"{tid}({nm}): accel ∧ D≥IS-p75",
            "tight",
            (a1 > 0) & (a0 > a1) & (a0 >= thr75),
        )
        add(
            f"seat_{tid}__accel_p90",
            f"{tid}({nm}): accel ∧ D≥IS-p90({thr90/1e8:.2f}億)",
            "tight",
            (a1 > 0) & (a0 > a1) & (a0 >= thr90),
        )
        add(
            f"seat_{tid}__accel_0p5",
            f"{tid}({nm}): accel ∧ D≥0.5億",
            "tight",
            (a1 > 0) & (a0 > a1) & (a0 >= FLOOR_0P5),
        )
        add(
            f"seat_{tid}__accel_0p5__ts1",
            f"{tid}({nm}): accel≥0.5 ∧ ts≥1",
            "tight",
            (a1 > 0) & (a0 > a1) & (a0 >= FLOOR_0P5) & (ts_d >= 1),
        )
        add(
            f"seat_{tid}__accel_0p5__bh",
            f"{tid}({nm}): accel≥0.5 ∧ bhchg>0",
            "tight",
            (a1 > 0) & (a0 > a1) & (a0 >= FLOOR_0P5) & (bh_d > 0),
        )
        add(
            f"seat_{tid}__pp_0p5__ts_up",
            f"{tid}({nm}): ++≥0.5@D ∧ ts↑",
            "tight",
            (a1 > 0) & (a0 >= FLOOR_0P5) & (ts_d > ts_m),
        )

    # rank#1 accel
    for tid in focus[:8]:
        nm = name_map.get(tid, tid)
        a0 = panel[f"amt_{tid}_d"]
        a1 = panel[f"amt_{tid}_dm1"]
        add(
            f"seat_{tid}__rank1_accel",
            f"{tid}({nm}): rank#1 ∧ accel",
            "tight",
            (panel[f"rank_{tid}_d"] == 1) & (a1 > 0) & (a0 > a1),
        )

    _ = good_mask_col
    return out


def _any_champ_accel_0p5(panel: pd.DataFrame, champs: list[str]) -> pd.Series:
    m = pd.Series(False, index=panel.index)
    for tid in champs:
        a0 = panel[f"amt_{tid}_d"]
        a1 = panel[f"amt_{tid}_dm1"]
        m = m | ((a1 > 0) & (a0 > a1) & (a0 >= FLOOR_0P5))
    return m


def compute_lift(
    seqs: dict[str, tuple[set[str], str, str]],
    panel: pd.DataFrame,
    good_col: str,
    split: str = "IS",
) -> pd.DataFrame:
    sub = panel[panel["split"] == split].copy() if split != "full" else panel.copy()
    good = sub[sub[good_col] == 1]
    bad = sub[sub[good_col] == 0]
    n_g, n_b = len(good), len(bad)
    base_rate = n_g / len(sub) if len(sub) else np.nan
    rows = []
    good_days = set(good["d"].astype(str))
    bad_days = set(bad["d"].astype(str))
    for sid, (fires, label, family) in seqs.items():
        g_hit = len(fires & good_days)
        b_hit = len(fires & bad_days)
        # also count quiet? we use only labeled days with valid excess
        prec = g_hit / (g_hit + b_hit) if (g_hit + b_hit) else np.nan
        sens = g_hit / n_g if n_g else np.nan
        rate_g = g_hit / n_g if n_g else np.nan
        rate_b = b_hit / n_b if n_b else np.nan
        lift = rate_g / rate_b if rate_b and rate_b > 0 else (np.inf if rate_g else np.nan)
        rows.append(
            {
                "seq_id": sid,
                "label": label,
                "family": family,
                "split": split,
                "good_col": good_col,
                "n_good": n_g,
                "n_bad": n_b,
                "base_rate": base_rate,
                "g_hit": g_hit,
                "b_hit": b_hit,
                "n_fire_labeled": g_hit + b_hit,
                "precision": prec,
                "sensitivity": sens,
                "lift": lift if np.isfinite(lift) else 99.0,
                "n_calendar": len(fires),
            }
        )
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    # score for ranking: lift * precision * log1p(g_hit)
    df["lift_score"] = (
        df["lift"].clip(upper=20)
        * df["precision"].fillna(0)
        * np.log1p(df["g_hit"].clip(lower=0))
    )
    return df.sort_values(["lift_score", "lift", "precision"], ascending=False)


# ---- report -------------------------------------------------------------


def md_table(headers: list[str], rows: list[list[str]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "|" + "|".join(["---"] * len(headers)) + "|",
    ]
    for r in rows:
        lines.append("| " + " | ".join(r) + " |")
    return "\n".join(lines)


def main() -> int:
    global SID, EXTRA_SEATS, PRIOR_FROZEN
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sid", default="2327", help="stock id (default 2327)")
    ap.add_argument("--d1", default=D1)
    ap.add_argument("--top-bt", type=int, default=TOP_BT)
    args = ap.parse_args()
    SID = str(args.sid)
    if SID != "2327":
        EXTRA_SEATS = ()
        PRIOR_FROZEN = ""
    d1 = str(args.d1)
    top_bt = int(args.top_bt)
    years = (pd.Timestamp(d1) - pd.Timestamp(D0)).days / 365.25

    t0 = time.time()
    OUT.mkdir(parents=True, exist_ok=True)
    log(f"{SID} sequence morphology · db={DEFAULT_DB_PATH}")

    doc = json.loads((TRADES_DIR / f"{SID}.json").read_text(encoding="utf-8"))
    core = {str(k): str(v) for k, v in (doc.get("core") or {}).items()}
    if not core:
        raise SystemExit(f"empty core in trades/{SID}.json")
    stock_name = str(doc.get("stock_name") or SID)
    champ_list = list(core.keys())
    name_map: dict[str, str] = dict(core)

    conn = connect_ro(DEFAULT_DB_PATH)
    whale = load_whale_events([SID])
    whale = whale[(whale["signal_date"] >= D0) & (whale["signal_date"] <= d1)].copy()
    greens = sorted(whale["signal_date"].astype(str).tolist())
    log(f"Whale greens (external benchmark only) n={len(greens)}")

    cal = [d for d in load_calendar(conn, D0, d1) if D0 <= d <= d1]
    cal_ext = load_calendar(conn, D0, "2026-08-31")
    di = {d: i for i, d in enumerate(cal_ext)}
    bench = load_bench(conn, D0, d1)
    ohlc = load_ohlc(conn, [SID], D0, d1)
    ohlc_px = {
        (str(r.sid), str(r.d)): (float(r.open), float(r.close))
        for r in ohlc.itertuples()
    }
    px_close = {(str(r.sid), str(r.d)): float(r.close) for r in ohlc.itertuples()}

    hs = HS_PATH if HS_PATH.exists() else None
    gov = GOV_PATH if GOV_PATH.exists() else None
    log("build chip features")
    feat = build_chip_feature_frame(
        conn, [SID], D0, d1, holding_shares_path=hs, gov_bank_path=gov
    )
    feat = enrich_sums(feat)
    feat_idx = feat.set_index(["sid", "d"])

    log("load branch tape")
    tape = fetch_tape(conn, cal)
    conn.close()
    tape = attach_amt(tape, px_close)
    tape["is_foreign"] = tape["tname"].map(is_foreign)
    tape_dom = tape[~tape["is_foreign"]].copy()
    for r in tape_dom.itertuples():
        if r.tid not in name_map and r.tname:
            name_map[str(r.tid)] = str(r.tname)

    branch_amt: dict[str, dict[str, float]] = {}
    seat_activity: dict[str, float] = {}
    for r in tape_dom.itertuples():
        d, tid = str(r.d), str(r.tid)
        amt = float(r.amt) if pd.notna(r.amt) else 0.0
        branch_amt.setdefault(d, {})[tid] = amt
        if amt > 0:
            seat_activity[tid] = seat_activity.get(tid, 0.0) + amt

    # seats = champions + top active + extras
    top_active = [
        t
        for t, _ in sorted(seat_activity.items(), key=lambda kv: -kv[1])
        if t not in core
    ][:8]
    seats = list(dict.fromkeys([*champ_list, *EXTRA_SEATS, *top_active]))
    log(f"seats n={len(seats)}: {seats}")

    # ---- outcome panel ----
    log("build day panel + forward excess labels")
    panel = build_day_panel(
        cal, cal_ext, di, seats, branch_amt, feat_idx, ohlc_px, bench, HOLDS
    )
    # labels for primary H=7
    ex = panel["ex_h7"]
    valid = ex.notna()
    panel = panel.loc[valid].copy()
    med_is = float(panel.loc[panel["split"] == "IS", "ex_h7"].median())
    thr_defs = {
        "gt0": 0.0,
        "gt3": 3.0,
        "gt5": 5.0,
        "medp": med_is,  # IS median as thr (applied to all for labeling; lift on IS)
    }
    for name, thr in thr_defs.items():
        panel[f"good_{name}"] = (panel["ex_h7"] >= thr).astype(int)
    log(
        f"panel n={len(panel)} IS={int((panel.split=='IS').sum())} "
        f"OOS={int((panel.split=='OOS').sum())} med_IS_ex={med_is:.2f}"
    )
    for name, thr in thr_defs.items():
        g = int(panel.loc[panel["split"] == "IS", f"good_{name}"].sum())
        log(f"  IS good_{name} (thr={thr:.2f}): {g}")

    panel.to_csv(OUT / f"{SID}_seq_day_panel.csv", index=False)

    # ---- Whale + prior arms ----
    whale_trades: list[dict] = []
    whale_xs: dict[int, dict[str, list[float]]] = {
        h: {"full": [], "IS": [], "OOS": []} for h in HOLDS
    }
    for hold in HOLDS:
        for T in greens:
            x, ed, xd = excess_l1(T, hold, cal_ext, di, ohlc_px, bench)
            if x is None:
                continue
            sp = "IS" if T < OOS else "OOS"
            whale_trades.append(
                {
                    "sid": SID,
                    "signal_date": T,
                    "entry_date": ed,
                    "exit_date": xd,
                    "arm": "Whale_T+1",
                    "rule": "Whale_T+1",
                    "hold": hold,
                    "excess_pct": x,
                    "split": sp,
                }
            )
            whale_xs[hold]["full"].append(x)
            whale_xs[hold][sp].append(x)

    whale_full = summarize(whale_xs[PRIMARY_HOLD]["full"])
    whale_oos = summarize(whale_xs[PRIMARY_HOLD]["OOS"])
    whale_is = summarize(whale_xs[PRIMARY_HOLD]["IS"])
    log(
        f"Whale H7 full n={whale_full['n']} mean={whale_full['mean']:.2f} "
        f"sum={whale_full['sum']:.1f}"
    )

    # ---- iterations ----
    iterations_log: list[dict] = []
    all_lift_rows: list[pd.DataFrame] = []
    all_bt_rows: list[dict] = []
    trade_keep: list[dict] = list(whale_trades)
    best_overall: dict | None = None
    beat = False
    beat_reason = ""

    label_priority = ["gt0", "medp", "gt3", "gt5"]  # mine across

    for it in (1, 2, 3):
        log(f"======== ITERATION {it} ========")
        if it == 1:
            seqs = fire_sequences_iter1(panel, seats, name_map)
        elif it == 2:
            seqs = fire_sequences_iter2(panel, seats, name_map)
        else:
            seqs = fire_sequences_iter3(panel, seats, name_map, "good_gt0")
        log(f"  sequences defined: {len(seqs)}")

        # lift across label defs on IS; pool top candidates
        lift_frames = []
        for lab in label_priority:
            lf = compute_lift(seqs, panel, f"good_{lab}", "IS")
            lf["iteration"] = it
            lift_frames.append(lf)
        lift_all = pd.concat(lift_frames, ignore_index=True)
        all_lift_rows.append(lift_all)

        # pick top by lift_score per label, then unique seq_ids
        candidates: list[str] = []
        for lab in label_priority:
            sub = lift_all[
                (lift_all["good_col"] == f"good_{lab}")
                & (lift_all["g_hit"] >= MIN_LIFT_N_GOOD)
                & (lift_all["n_calendar"] >= 5)
            ]
            top = sub.head(max(15, top_bt // 4))
            for sid in top["seq_id"]:
                if sid not in candidates:
                    candidates.append(sid)
        # always include prior frozen if present
        if PRIOR_FROZEN in seqs and PRIOR_FROZEN not in candidates:
            candidates.append(PRIOR_FROZEN)
        # also top by calendar precision regardless
        for lab in ("gt0", "gt3"):
            sub = lift_all[lift_all["good_col"] == f"good_{lab}"].head(20)
            for sid in sub["seq_id"]:
                if sid not in candidates:
                    candidates.append(sid)
        candidates = candidates[: max(top_bt, 40)]
        log(f"  backtest candidates: {len(candidates)}")

        # backtest each candidate H=7 (all splits) + H=5,10 for top later
        it_results = []
        for sid in candidates:
            fires, label, family = seqs[sid]
            trades = standalone_trades(
                fires,
                PRIMARY_HOLD,
                cal_ext,
                di,
                ohlc_px,
                bench,
                f"seq_it{it}",
                sid,
                D0,
                d1,
            )
            full = metrics_block(trades, whale_xs[PRIMARY_HOLD]["full"], "full")
            ism = metrics_block(trades, whale_xs[PRIMARY_HOLD]["IS"], "IS")
            oos = metrics_block(trades, whale_xs[PRIMARY_HOLD]["OOS"], "OOS")
            met, reason = win_criteria_v2(full, oos, ism, whale_full, whale_oos)
            row = {
                "iteration": it,
                "seq_id": sid,
                "label": label,
                "family": family,
                "n_calendar": len(fires),
                "hold": PRIMARY_HOLD,
                "IS_n": ism["n"],
                "IS_mean": ism["mean"],
                "IS_hit": ism["hit_rate"],
                "IS_sum": ism["sum"],
                "full_n": full["n"],
                "full_mean": full["mean"],
                "full_hit": full["hit_rate"],
                "full_med": full["med"],
                "full_sum": full["sum"],
                "full_delta": full["delta_vs_whale"],
                "full_ci_lo": full["ci_lo"],
                "full_ci_hi": full["ci_hi"],
                "full_p_le0": full["p_le0"],
                "OOS_n": oos["n"],
                "OOS_mean": oos["mean"],
                "OOS_hit": oos["hit_rate"],
                "OOS_sum": oos["sum"],
                "OOS_delta": oos["delta_vs_whale"],
                "win": met,
                "win_reason": reason,
            }
            it_results.append(row)
            all_bt_rows.append(row)

            def _score(r: dict) -> float:
                """Prefer IS≥8 & IS mean≥0, then OOS mean, then full mean/sum."""
                if r["IS_n"] < MIN_IS_N or not pd.notna(r["IS_mean"]):
                    return -9999.0
                pen = 0.0 if r["IS_mean"] >= 0 else -20.0
                oos_m = r["OOS_mean"] if pd.notna(r["OOS_mean"]) and r["OOS_n"] >= 3 else -5.0
                full_m = r["full_mean"] if pd.notna(r["full_mean"]) else -999.0
                full_s = r["full_sum"] if pd.notna(r["full_sum"]) else 0.0
                return pen + full_m + 0.5 * oos_m + 0.02 * full_s + 0.05 * min(r["IS_n"], 30)

            if met:
                beat = True
                beat_reason = reason
                if best_overall is None or _score(row) > _score(best_overall):
                    best_overall = {**row, "trades": trades}
                log(f"  ★ BEAT via {sid}: {reason}")
            elif best_overall is None or _score(row) > _score(best_overall):
                best_overall = {**row, "trades": trades}

        iterations_log.append(
            {
                "iteration": it,
                "n_seqs": len(seqs),
                "n_candidates": len(candidates),
                "n_with_is8": sum(1 for r in it_results if r["IS_n"] >= MIN_IS_N),
                "best_full_mean": max(
                    (r["full_mean"] for r in it_results if pd.notna(r["full_mean"])),
                    default=np.nan,
                ),
                "any_win": any(r["win"] for r in it_results),
            }
        )
        # continue all iterations even after a win — seek stronger
        soft = [r for r in it_results if r["win"]]
        if soft:
            log(f"  wins this iter: {[s['seq_id'] for s in soft[:8]]}")

    # ---- evaluate prior frozen + best across holds ----
    log("finalize arms + H=5/10 sensitivity")
    prior_fires: set[str] = set()
    if "918e" in seats and "9665" in seats:
        m = (
            (panel["amt_918e_d"] > 0)
            & (panel["amt_9665_d"] > 0)
            & (panel["amt_918e_dm1"] > 0)
            & (panel["amt_9665_dm1"] > 0)
        )
        prior_fires = set(panel.loc[m, "d"].astype(str))

    all_seqs_final: dict[str, tuple[set[str], str, str]] = {}
    all_seqs_final.update(fire_sequences_iter1(panel, seats, name_map))
    all_seqs_final.update(fire_sequences_iter2(panel, seats, name_map))
    all_seqs_final.update(fire_sequences_iter3(panel, seats, name_map, "good_gt0"))

    # Prefer true wins; else score-based best
    beat = False
    beat_reason = ""
    wins = [r for r in all_bt_rows if r.get("win")]
    if wins:
        wins.sort(
            key=lambda r: (
                r["full_mean"] if pd.notna(r["full_mean"]) else -999,
                r["OOS_mean"] if pd.notna(r["OOS_mean"]) else -999,
            ),
            reverse=True,
        )
        best_overall = {**wins[0]}
        beat = True
        beat_reason = wins[0]["win_reason"]
        log(f"selected WINNER {best_overall['seq_id']}: {beat_reason}")
    elif best_overall:
        full = {
            "n": best_overall["full_n"],
            "mean": best_overall["full_mean"],
            "sum": best_overall["full_sum"],
            "p_le0": best_overall["full_p_le0"],
        }
        oos = {"n": best_overall["OOS_n"], "mean": best_overall["OOS_mean"]}
        ism = {"n": best_overall["IS_n"], "mean": best_overall["IS_mean"]}
        met, reason = win_criteria_v2(full, oos, ism, whale_full, whale_oos)
        if met:
            beat = True
            beat_reason = reason

    best_id = best_overall["seq_id"] if best_overall else None
    best_fires: set[str] = set()
    if best_id and best_id in all_seqs_final:
        best_fires = all_seqs_final[best_id][0]
        trade_keep.extend(
            standalone_trades(
                best_fires,
                PRIMARY_HOLD,
                cal_ext,
                di,
                ohlc_px,
                bench,
                "standalone_best",
                best_id,
                D0,
                d1,
            )
        )

    summary_arms: list[dict] = []
    for hold in HOLDS:
        for split, xs in (
            ("full", whale_xs[hold]["full"]),
            ("IS", whale_xs[hold]["IS"]),
            ("OOS", whale_xs[hold]["OOS"]),
        ):
            sm = summarize(xs)
            summary_arms.append(
                {
                    "arm": "Whale_T+1",
                    "rule": "Whale_T+1",
                    "hold": hold,
                    "split": split,
                    **sm,
                    "delta_vs_whale": 0.0,
                    "p_le0": np.nan,
                    "ci_lo": np.nan,
                    "ci_hi": np.nan,
                }
            )
        ptr = standalone_trades(
            prior_fires, hold, cal_ext, di, ohlc_px, bench, "prior", PRIOR_FROZEN, D0, d1
        )
        trade_keep.extend(ptr)
        for split in ("full", "IS", "OOS"):
            mb = metrics_block(ptr, whale_xs[hold][split], split)
            summary_arms.append(
                {
                    "arm": "Prior_918e+9665",
                    "rule": PRIOR_FROZEN,
                    "hold": hold,
                    **mb,
                }
            )
        if best_fires:
            btr = standalone_trades(
                best_fires,
                hold,
                cal_ext,
                di,
                ohlc_px,
                bench,
                "standalone_best",
                best_id or "best",
                D0,
                d1,
            )
            trade_keep.extend(btr)
            for split in ("full", "IS", "OOS"):
                mb = metrics_block(btr, whale_xs[hold][split], split)
                summary_arms.append(
                    {
                        "arm": "Standalone_best",
                        "rule": best_id,
                        "label": best_overall.get("label") if best_overall else "",
                        "hold": hold,
                        **mb,
                    }
                )

    # ---- CSVs ----
    lift_cat = pd.concat(all_lift_rows, ignore_index=True) if all_lift_rows else pd.DataFrame()
    lift_cat.to_csv(OUT / f"{SID}_seq_lift_ranks.csv", index=False)
    bt_df = pd.DataFrame(all_bt_rows)
    if not bt_df.empty:
        bt_df = bt_df.sort_values(
            ["win", "full_mean", "OOS_mean", "IS_n"],
            ascending=[False, False, False, False],
        )
    bt_df.to_csv(OUT / f"{SID}_seq_backtest_summary.csv", index=False)
    pd.DataFrame(summary_arms).to_csv(OUT / f"{SID}_seq_arms_summary.csv", index=False)
    trades_df = pd.DataFrame(trade_keep)
    if not trades_df.empty:
        trades_df = trades_df.drop_duplicates(
            subset=["rule", "hold", "signal_date"], keep="last"
        )
    trades_df.to_csv(OUT / f"{SID}_seq_trades.csv", index=False)

    frozen = None
    if beat and best_overall:
        frozen = {
            "sid": SID,
            "name": stock_name,
            "protocol": {
                "decide": "D close: sequences on D−1/D (T−2/T−1 vs entry)",
                "enter": "D+1 open (L1)",
                "hold": PRIMARY_HOLD,
                "non_overlapping": True,
                "cost": COST,
                "beta": BETA,
                "window": f"{D0}..{d1}",
                "oos": OOS,
                "labels": "outcome-based forward excess (not Whale greens)",
            },
            "freeze_rule": {
                "seq_id": best_overall["seq_id"],
                "label": best_overall["label"],
                "family": best_overall["family"],
                "iteration": best_overall["iteration"],
                "win_reason": beat_reason,
                "IS": {
                    "n": best_overall["IS_n"],
                    "mean": best_overall["IS_mean"],
                    "hit": best_overall["IS_hit"],
                    "sum": best_overall["IS_sum"],
                },
                "full": {
                    "n": best_overall["full_n"],
                    "mean": best_overall["full_mean"],
                    "hit": best_overall["full_hit"],
                    "sum": best_overall["full_sum"],
                    "delta_vs_whale": best_overall["full_delta"],
                    "p_le0": best_overall["full_p_le0"],
                },
                "OOS": {
                    "n": best_overall["OOS_n"],
                    "mean": best_overall["OOS_mean"],
                    "hit": best_overall["OOS_hit"],
                    "sum": best_overall["OOS_sum"],
                },
            },
            "whale_benchmark_H7": {
                "full": whale_full,
                "OOS": whale_oos,
                "IS": whale_is,
            },
        }
        (OUT / f"{SID}_seq_frozen.json").write_text(
            json.dumps(frozen, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    elif (OUT / f"{SID}_seq_frozen.json").exists():
        # remove stale freeze from prior false-positive run
        (OUT / f"{SID}_seq_frozen.json").unlink()
        log(f"removed stale {SID}_seq_frozen.json (no honest win)")

    # top lift snapshot for report
    top_lift = (
        lift_cat[lift_cat["good_col"] == "good_gt0"]
        .drop_duplicates("seq_id")
        .head(25)
        if not lift_cat.empty
        else pd.DataFrame()
    )
    top_bt_is = (
        bt_df[(bt_df["IS_n"] >= MIN_IS_N) & (bt_df["IS_mean"] >= 0)].head(15)
        if not bt_df.empty
        else pd.DataFrame()
    )
    if top_bt_is.empty and not bt_df.empty:
        top_bt_is = bt_df[bt_df["IS_n"] >= MIN_IS_N].head(15)
    if top_bt_is.empty and not bt_df.empty:
        top_bt_is = bt_df.head(15)

    # near-misses: closest to Whale on OOS or full among IS≥8 & IS mean≥0
    near = pd.DataFrame()
    if not bt_df.empty:
        near = bt_df[(bt_df["IS_n"] >= MIN_IS_N) & (bt_df["IS_mean"] >= 0)].copy()
        if not near.empty:
            near["gap_oos"] = near["OOS_mean"] - whale_oos["mean"]
            near["gap_full"] = near["full_mean"] - whale_full["mean"]
            near = near.sort_values(["gap_oos", "gap_full"], ascending=False).head(8)

    # ---- markdown report ----
    gen = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    verdict = "贏了" if beat else "還沒贏"
    best_plain = (
        best_overall["label"]
        if best_overall
        else "（無合格 IS n≥8 候選）"
    )

    def arm_row(arm_name: str, hold: int, split: str) -> dict | None:
        for r in summary_arms:
            if r["arm"] == arm_name and r["hold"] == hold and r["split"] == split:
                return r
        return None

    lines: list[str] = []
    lines.append(f"# {SID} {stock_name} · Sequence morphology 挑戰 Whale_T+1")
    lines.append("")
    lines.append(f"- 生成：{gen}")
    lines.append("- Runner：`scripts/research/run_2327_sequence_morphology_study.py`")
    lines.append(
        f"- 窗：{D0}..{d1}（≈{years:.2f}y）· OOS≥{OOS} · COST={COST} · β={BETA}×IX0001"
    )
    lines.append(
        "- 標籤：**outcome-based** forward excess（E=D+1 open · H=7 主）· **非**共識綠燈"
    )
    lines.append(
        "- 協議：D 收盤看 D−1/D 序列 → D+1 開盤進 · 非重疊 · Whale_T+1 僅外部對照"
    )
    lines.append("- Research only · **未採納**進 Strategy／Order")
    lines.append("")
    lines.append("## 結論（先讀）")
    lines.append("")
    lines.append(f"**總評：{verdict}**")
    lines.append("")
    if beat:
        lines.append(f"- 勝出條件：`{beat_reason}`")
        lines.append(f"- 最佳序列：`{best_overall['seq_id']}` — {best_plain}")
    else:
        lines.append(
            f"- 最佳近似（未達勝出門檻）：`{best_overall['seq_id'] if best_overall else '—'}` — {best_plain}"
        )
        if beat_reason:
            lines.append(f"- 備註：{beat_reason}")
    lines.append(
        f"- Whale_T+1 H=7 full：n={whale_full['n']} · hit={fmt_rate(whale_full['hit_rate'])} · "
        f"mean={fmt_pct(whale_full['mean'])} · sum={fmt_pct(whale_full['sum'], 0)}"
    )
    if best_overall:
        lines.append(
            f"- Standalone best H=7 full：n={best_overall['full_n']} · "
            f"hit={fmt_rate(best_overall['full_hit'])} · mean={fmt_pct(best_overall['full_mean'])} · "
            f"sum={fmt_pct(best_overall['full_sum'], 0)} · "
            f"Δ={fmt_pct(best_overall['full_delta'])} · P(Δ≤0)={fmt_p(best_overall['full_p_le0'])}"
        )
        lines.append(
            f"- Standalone best OOS：n={best_overall['OOS_n']} · "
            f"mean={fmt_pct(best_overall['OOS_mean'])} · "
            f"（Whale OOS mean={fmt_pct(whale_oos['mean'])}）"
        )
    lines.append("")
    lines.append("### 勝出門檻（需至少一條）")
    lines.append("")
    lines.append("1. OOS mean ≥ Whale OOS **且** OOS n≥5 **且** IS n≥8 **且** IS mean≥0")
    lines.append("2. Full mean ≥ Whale full mean **且** bootstrap P(Δ≤0)≤0.15 **且** n≥12（IS mean≥0）")
    lines.append("3. Full sum 明顯更高（>+10%）**且** mean 不差過 2pp（n≥12 · IS mean≥0）")
    lines.append("")
    lines.append("## 標籤定義（outcome）")
    lines.append("")
    lab_rows = []
    for name, thr in thr_defs.items():
        is_g = int(panel.loc[panel["split"] == "IS", f"good_{name}"].sum())
        oo_g = int(panel.loc[panel["split"] == "OOS", f"good_{name}"].sum())
        lab_rows.append(
            [
                f"good_{name}",
                f"ex_h7 ≥ {thr:.2f}%",
                str(is_g),
                str(oo_g),
            ]
        )
    lines.append(
        md_table(
            ["label", "定義", "IS n_good", "OOS n_good"],
            lab_rows,
        )
    )
    lines.append("")
    lines.append("## 迭代紀錄")
    lines.append("")
    it_rows = []
    for it in iterations_log:
        it_rows.append(
            [
                str(it["iteration"]),
                str(it["n_seqs"]),
                str(it["n_candidates"]),
                str(it["n_with_is8"]),
                fmt_pct(it["best_full_mean"]),
                "Y" if it["any_win"] else "N",
            ]
        )
    lines.append(
        md_table(
            ["iter", "seqs", "BT cand", "IS n≥8", "best full mean", "any soft-win"],
            it_rows,
        )
    )
    lines.append("")
    lines.append(
        "- **Iter1**：2-day continuity / intensity / co-buy / stagger / chip path / chip×seat"
    )
    lines.append(
        "- **Iter2**：3-day +++ / rise3 / accum / path-stable / episode aggregates"
    )
    lines.append(
        "- **Iter3**：tight dual-accel / dual++0.5 / champ×chip / seat ++ hi-amt"
    )
    lines.append("")
    lines.append("## 對照臂（H=7）")
    lines.append("")
    arm_tbl = []
    for arm in ("Whale_T+1", "Prior_918e+9665", "Standalone_best"):
        for split in ("full", "IS", "OOS"):
            r = arm_row(arm, PRIMARY_HOLD, split)
            if not r:
                continue
            arm_tbl.append(
                [
                    arm,
                    split,
                    str(r["n"]),
                    fmt_rate(r.get("hit_rate")),
                    fmt_pct(r.get("med")),
                    fmt_pct(r.get("mean")),
                    fmt_pct(r.get("sum"), 0),
                    fmt_pct(r.get("delta_vs_whale")),
                    fmt_p(r.get("p_le0")),
                ]
            )
    lines.append(
        md_table(
            ["arm", "split", "n", "hit", "med%", "mean%", "sum%", "ΔvsWhale", "P(Δ≤0)"],
            arm_tbl,
        )
    )
    lines.append("")
    lines.append("## IS Top（lift · good_gt0 · 去重）")
    lines.append("")
    if top_lift.empty:
        lines.append("（無）")
    else:
        lr = []
        for i, r in enumerate(top_lift.head(15).itertuples(), 1):
            lr.append(
                [
                    str(i),
                    str(r.seq_id)[:40],
                    str(r.family),
                    str(int(r.g_hit)),
                    f"{r.precision:.2f}" if pd.notna(r.precision) else "—",
                    f"{r.lift:.2f}" if pd.notna(r.lift) else "—",
                    str(int(r.n_calendar)),
                ]
            )
        lines.append(
            md_table(
                ["#", "seq", "family", "g_hit", "prec", "lift", "n_cal"],
                lr,
            )
        )
    lines.append("")
    lines.append("## Standalone 回測 Top（IS n≥8 · IS mean≥0 · H=7）")
    lines.append("")
    if top_bt_is.empty:
        lines.append("（無）")
    else:
        br = []
        for i, r in enumerate(top_bt_is.head(12).itertuples(), 1):
            br.append(
                [
                    str(i),
                    str(r.seq_id)[:36],
                    str(int(r.IS_n)),
                    fmt_pct(r.IS_mean),
                    str(int(r.full_n)),
                    fmt_pct(r.full_mean),
                    fmt_pct(r.full_sum, 0),
                    str(int(r.OOS_n)),
                    fmt_pct(r.OOS_mean),
                    "Y" if r.win else "N",
                ]
            )
        lines.append(
            md_table(
                [
                    "#",
                    "seq",
                    "IS_n",
                    "IS_mean",
                    "full_n",
                    "full_mean",
                    "full_sum",
                    "OOS_n",
                    "OOS_mean",
                    "win?",
                ],
                br,
            )
        )
    lines.append("")
    lines.append("## Near-miss（相對 Whale · IS≥8 ∧ IS mean≥0）")
    lines.append("")
    if near.empty:
        lines.append("（無）")
    else:
        nr = []
        for r in near.itertuples():
            nr.append(
                [
                    str(r.seq_id)[:36],
                    fmt_pct(r.OOS_mean),
                    fmt_pct(r.gap_oos),
                    fmt_pct(r.full_mean),
                    fmt_pct(r.gap_full),
                    fmt_pct(r.full_sum, 0),
                    str(int(r.OOS_n)),
                ]
            )
        lines.append(
            md_table(
                ["seq", "OOS_mean", "ΔOOS", "full_mean", "Δfull", "full_sum", "OOS_n"],
                nr,
            )
        )
    lines.append("")
    lines.append(
        "> 註：首輪曾出現 `seat_8840__accel` OOS mean>+Whale 但 IS mean&lt;0 —— "
        "已視為**假勝出**（regime luck），勝出門檻改為要求 IS mean≥0。"
    )
    lines.append("")
    lines.append("## H=5 / H=10 敏感度（最佳臂）")
    lines.append("")
    sens = []
    for hold in HOLDS:
        for arm in ("Whale_T+1", "Standalone_best"):
            r = arm_row(arm, hold, "full")
            if not r:
                continue
            sens.append(
                [
                    str(hold),
                    arm,
                    str(r["n"]),
                    fmt_pct(r.get("mean")),
                    fmt_pct(r.get("sum"), 0),
                    fmt_rate(r.get("hit_rate")),
                ]
            )
    lines.append(md_table(["H", "arm", "n", "mean%", "sum%", "hit"], sens))
    lines.append("")
    lines.append("## 方法備註")
    lines.append("")
    lines.append(
        f"- 席位：冠軍 {champ_list} + EXTRA {list(EXTRA_SEATS)} + top-active by buy amt"
    )
    lines.append(f"- seats used n={len(seats)}")
    lines.append("- Lift：IS only · P(seq|good)/P(seq|bad)；回測獨立日曆非重疊")
    lines.append("- 不做 Whale 綠燈子集濾網；不做 n<5 宣稱勝出")
    lines.append(
        f"- 產物：`{SID}_seq_lift_ranks.csv` · `{SID}_seq_backtest_summary.csv` · "
        f"`{SID}_seq_trades.csv` · `{SID}_seq_arms_summary.csv` · `{SID}_seq_day_panel.csv`"
        + (f" · `{SID}_seq_frozen.json`" if frozen else " · （無凍結：未達勝出門檻）")
    )
    lines.append("")
    lines.append("## 嘗試過但未採用的方向（本輪）")
    lines.append("")
    lines.append("- 事件綁定 Whale 子集（前輪失敗，本輪禁止）")
    lines.append("- 平坦 net>0 / 0.5億 門檻掃參當「最佳」（前輪已做）")
    lines.append("- 本輪改為：**序列形態**（++ / accel / stagger / 3d rise / chip×seq）")
    if not beat:
        lines.append(
            "- **誠實結論**：在 outcome-label + 非重疊 L1H7 + IS freeze 約束下，"
            "尚未找到相對 Whale_T+1（full mean≈"
            f"{fmt_pct(whale_full['mean'])} · sum≈{fmt_pct(whale_full['sum'], 0)}）"
            "同時滿足勝出門檻的獨立序列規則。"
        )

    report_path = OUT / f"{SID}_SEQUENCE_BEAT_WHALE.md"
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log(f"wrote {report_path}")

    status = {
        "sid": SID,
        "verdict": verdict,
        "beat": beat,
        "beat_reason": beat_reason,
        "best_seq": best_overall["seq_id"] if best_overall else None,
        "best_label": best_overall["label"] if best_overall else None,
        "whale_full_mean": whale_full["mean"],
        "best_full_mean": best_overall["full_mean"] if best_overall else None,
        "elapsed_sec": round(time.time() - t0, 1),
    }
    (OUT / f"status_{SID}_sequence.json").write_text(
        json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log(f"DONE verdict={verdict} elapsed={status['elapsed_sec']}s")
    print(json.dumps(status, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
