#!/usr/bin/env python3
"""2327 國巨 · 個股籌碼規則掃參 × 分點前兆 · Decide_T−2 / Enter_T−1 vs Whale_T+1.

Research only · 未採納 · Book only.
Protocol:
  - Whale_T+1: green consensus day T → enter T+1 open (L1), hold H
  - Candidate: rule true on decision day D (=T−2 for event-tied) → enter D+1 open
  - Cost 30bps · β=1.15×IX0001 · window ≈2024-07-01..data end · OOS≥2026-01-01

  PYTHONPATH=src .venv/bin/python scripts/research/run_2327_chip_branch_t2_t1_sweep.py
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
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
HOLDS = (7, 10)
PRIMARY_HOLD = 7
N_BOOT = 5000
RNG = np.random.default_rng(42)
YEARS = (pd.Timestamp(D1) - pd.Timestamp(D0)).days / 365.25
FLOOR_0P5 = 5e7
FLOOR_1 = 1e8
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
MIN_STANDALONE_IS_N = 5
TOP_BRANCH_SEATS = 5


def log(m: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {m}", flush=True)


def is_foreign(name: str) -> bool:
    return any(k in (name or "") for k in FOREIGN_KW)


# ---- chip atoms / rules -------------------------------------------------


@dataclass(frozen=True)
class Atom:
    id: str
    group: str  # conflict group; same group → at most one
    label: str


ATOMS: list[Atom] = [
    Atom("fs1", "fs", "foreign_streak≥1"),
    Atom("fs2", "fs", "foreign_streak≥2"),
    Atom("fs3", "fs", "foreign_streak≥3"),
    Atom("ts1", "ts", "three_streak≥1"),
    Atom("ts2", "ts", "three_streak≥2"),
    Atom("ts3", "ts", "three_streak≥3"),
    Atom("fsum3", "fsum", "foreign_sum_3d>0"),
    Atom("fsum5", "fsum", "foreign_sum_5d>0"),
    Atom("ssum3", "ssum", "three_sum_3d>0"),
    Atom("ssum5", "ssum", "three_sum_5d>0"),
    Atom("trust", "trust", "trust_net>0"),
    Atom("ldrop", "ldrop", "lending_drop"),
    Atom("bhchg", "bhchg", "big_holder_pct_chg>0"),
]
ATOM_BY_ID = {a.id: a for a in ATOMS}


def build_rule_grid() -> list[tuple[str, ...]]:
    """Combinations of 1–3 atoms; at most one per conflict group."""
    out: list[tuple[str, ...]] = []
    ids = [a.id for a in ATOMS]
    for k in (1, 2, 3):
        for comb in combinations(ids, k):
            groups = [ATOM_BY_ID[i].group for i in comb]
            if len(groups) != len(set(groups)):
                continue
            out.append(comb)
    return out


def enrich_sums(feat: pd.DataFrame) -> pd.DataFrame:
    g = feat.sort_values(["sid", "d"]).copy()
    for col_src, col_out, w in (
        ("foreign_net", "foreign_sum_3d", 3),
        ("three_buy_net", "three_sum_3d", 3),
    ):
        if col_src not in g.columns:
            g[col_out] = np.nan
            continue
        g[col_out] = (
            g.groupby("sid")[col_src]
            .transform(lambda s: s.rolling(w, min_periods=1).sum())
        )
    if "lending_drop" not in g.columns:
        g["lending_drop"] = (
            (g.get("lending_drop_streak", pd.Series(0, index=g.index)).fillna(0) >= 1)
            | (g.get("lending_change", pd.Series(0, index=g.index)).fillna(0) < 0)
        ).astype(int)
    return g


def atom_mask(feat_row: pd.Series, atom_id: str) -> bool:
    """Evaluate one atom. Exact-id match only (avoid fsum* matching fs*)."""
    if atom_id in ("fs1", "fs2", "fs3"):
        thr = int(atom_id[-1])
        return float(feat_row.get("foreign_streak", 0) or 0) >= thr
    if atom_id in ("ts1", "ts2", "ts3"):
        thr = int(atom_id[-1])
        return float(feat_row.get("three_streak", 0) or 0) >= thr
    if atom_id == "fsum3":
        return float(feat_row.get("foreign_sum_3d", 0) or 0) > 0
    if atom_id == "fsum5":
        return float(feat_row.get("foreign_sum_5d", 0) or 0) > 0
    if atom_id == "ssum3":
        return float(feat_row.get("three_sum_3d", 0) or 0) > 0
    if atom_id == "ssum5":
        return float(feat_row.get("three_sum_5d", 0) or 0) > 0
    if atom_id == "trust":
        return float(feat_row.get("trust_net", 0) or 0) > 0
    if atom_id == "ldrop":
        return bool(int(feat_row.get("lending_drop", 0) or 0))
    if atom_id == "bhchg":
        v = feat_row.get("big_holder_pct_chg", np.nan)
        return bool(pd.notna(v) and float(v) > 0)
    return False


def rule_mask_on_day(feat_idx: pd.DataFrame, d: str, atoms: tuple[str, ...]) -> bool:
    try:
        row = feat_idx.loc[(SID, d)]
    except KeyError:
        return False
    if isinstance(row, pd.DataFrame):
        row = row.iloc[0]
    return all(atom_mask(row, a) for a in atoms)


def rule_label(atoms: tuple[str, ...]) -> str:
    return " ∧ ".join(ATOM_BY_ID[a].label for a in atoms)


def rule_id(atoms: tuple[str, ...]) -> str:
    return "+".join(atoms)


# ---- returns ------------------------------------------------------------


def excess_l1(
    signal_d: str,
    hold: int,
    cal: list[str],
    di: dict[str, int],
    ohlc_px: dict[tuple[str, str], tuple[float, float]],
    bench: dict[str, float],
) -> tuple[float | None, str | None, str | None]:
    """Enter next open after signal_d; exit hold bars later (calendar index of entry)."""
    i = di.get(signal_d)
    if i is None or i + 1 >= len(cal):
        return None, None, None
    ed = cal[i + 1]
    j = di[ed]
    if j + hold - 1 >= len(cal) and j + hold >= len(cal):
        # exit = entry_idx + (hold-1) sessions later? L1H7: entry T+1, exit at
        # calendar[i+hold] when signal is T (i), i.e. hold sessions from signal.
        # Match prior scripts: xd = cal[i + hold] with i = signal index.
        pass
    # Prior convention (run_chip_t0_vs_whale_t1): signal T → entry cal[i+1],
    # exit cal[i+hold]  (so hold=7 means exit on 7th calendar day after signal,
    # which is ~6 sessions after entry). Keep identical.
    if i + hold >= len(cal):
        return None, None, None
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


def summarize(xs: list[float], label: str = "") -> dict:
    a = np.asarray(xs, dtype=float)
    n = int(a.size)
    if n == 0:
        return {
            "arm": label,
            "n": 0,
            "hit_rate": np.nan,
            "med_excess": np.nan,
            "mean_excess": np.nan,
            "sum_excess": np.nan,
            "per_trade_mean": np.nan,
        }
    return {
        "arm": label,
        "n": n,
        "hit_rate": float((a > 0).mean()),
        "med_excess": float(np.median(a)),
        "mean_excess": float(a.mean()),
        "sum_excess": float(a.sum()),
        "per_trade_mean": float(a.mean()),
    }


def bootstrap_delta(a: list[float], b: list[float], paired: bool = False) -> dict:
    aa = np.asarray(a, dtype=float)
    bb = np.asarray(b, dtype=float)
    if paired:
        if aa.size != bb.size or aa.size < 3:
            d0 = float(aa.mean() - bb.mean()) if aa.size and bb.size else np.nan
            return {
                "delta_mean": d0,
                "ci_lo": np.nan,
                "ci_hi": np.nan,
                "p_delta_le0": np.nan,
                "n_a": int(aa.size),
                "n_b": int(bb.size),
                "paired": True,
            }
        diffs = aa - bb
        d0 = float(diffs.mean())
        idx = RNG.integers(0, diffs.size, size=(N_BOOT, diffs.size))
        deltas = diffs[idx].mean(axis=1)
        return {
            "delta_mean": d0,
            "ci_lo": float(np.percentile(deltas, 2.5)),
            "ci_hi": float(np.percentile(deltas, 97.5)),
            "p_delta_le0": float((deltas <= 0).mean()),
            "n_a": int(aa.size),
            "n_b": int(bb.size),
            "paired": True,
        }
    if aa.size < 5 or bb.size < 5:
        return {
            "delta_mean": float(aa.mean() - bb.mean()) if aa.size and bb.size else np.nan,
            "ci_lo": np.nan,
            "ci_hi": np.nan,
            "p_delta_le0": np.nan,
            "n_a": int(aa.size),
            "n_b": int(bb.size),
            "paired": False,
        }
    d0 = float(aa.mean() - bb.mean())
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
        "paired": False,
    }


def verdict_word(boot: dict) -> str:
    """Honest Chinese verdict; small-n → soft directional only."""
    n_min = min(boot.get("n_a", 0), boot.get("n_b", 0))
    dm = boot.get("delta_mean", np.nan)
    if np.isnan(dm):
        return "差不多"
    lo, hi = boot.get("ci_lo", np.nan), boot.get("ci_hi", np.nan)
    if n_min < 8:
        # too thin for significance — soft label only
        if abs(dm) < 1.0:
            return "差不多"
        return "更好（n偏小）" if dm > 0 else "更差（n偏小）"
    if not np.isnan(lo) and lo > 0:
        return "更好"
    if not np.isnan(hi) and hi < 0:
        return "更差"
    p = boot.get("p_delta_le0", np.nan)
    if not np.isnan(p):
        if dm >= 1.0 and p <= 0.10 and n_min >= 15:
            return "更好"
        if dm <= -1.0 and p >= 0.90 and n_min >= 15:
            return "更差"
    if abs(dm) < 1.0 or (not np.isnan(lo) and lo <= 0 <= hi):
        return "差不多"
    return "更好" if dm > 0 else "更差"


# ---- branch tape --------------------------------------------------------


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
    if tape.empty:
        tape = tape.copy()
        tape["amt"] = []
        return tape
    t = tape.copy()
    t["close"] = [px.get((r.sid, r.d), np.nan) for r in t.itertuples()]
    t["amt"] = t["net"] * t["close"]
    return t


def lag_date(cal: list[str], di: dict[str, int], sig: str, k: int) -> str | None:
    i = di.get(sig)
    if i is None or i < k:
        return None
    return cal[i - k]


# ---- trading helpers ----------------------------------------------------


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
    """Non-overlapping: fire on D → enter D+1 open; skip while in hold window."""
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
                "lens": "standalone",
                "split": "IS" if d < OOS else "OOS",
            }
        )
    return rows


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


# ---- main ---------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--d1", default=D1, help="window end date")
    args = ap.parse_args()
    d1 = str(args.d1)

    t0 = time.time()
    OUT.mkdir(parents=True, exist_ok=True)
    log(f"2327 chip×branch T−2/T−1 sweep · db={DEFAULT_DB_PATH}")

    doc = json.loads((TRADES_DIR / f"{SID}.json").read_text(encoding="utf-8"))
    core = {str(k): str(v) for k, v in (doc.get("core") or {}).items()}
    log(f"core seats n={len(core)}: {core}")

    conn = connect_ro(DEFAULT_DB_PATH)
    whale = load_whale_events([SID])
    whale = whale[
        (whale["signal_date"] >= D0) & (whale["signal_date"] <= d1)
    ].copy()
    greens = sorted(whale["signal_date"].astype(str).tolist())
    n_is_g = sum(1 for g in greens if g < OOS)
    n_oos_g = sum(1 for g in greens if g >= OOS)
    log(f"green events n={len(greens)} IS={n_is_g} OOS={n_oos_g}")

    cal = [d for d in load_calendar(conn, D0, d1) if D0 <= d <= d1]
    # extend calendar a bit for exits
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
    has_bh = feat["big_holder_pct_chg"].notna().any() if "big_holder_pct_chg" in feat.columns else False
    log(f"feat rows={len(feat)} big_holder_coverage={has_bh}")

    # full calendar tape for 2327 (standalone + ranking)
    log("load full branch tape for 2327")
    tape = fetch_tape(conn, cal)
    conn.close()
    tape = attach_amt(tape, px_close)
    tape["is_foreign"] = tape["tname"].map(is_foreign)
    tape_dom = tape[~tape["is_foreign"]].copy()
    name_map: dict[str, str] = {**core}
    for r in tape_dom.itertuples():
        if r.tid not in name_map and r.tname:
            name_map[str(r.tid)] = str(r.tname)
    log(f"tape domestic rows={len(tape_dom)} days={tape_dom['d'].nunique()}")

    # branch day maps: d -> tid -> amt
    branch_amt: dict[str, dict[str, float]] = {}
    for r in tape_dom.itertuples():
        branch_amt.setdefault(str(r.d), {})[str(r.tid)] = float(r.amt) if pd.notna(r.amt) else 0.0

    def branch_fire(d: str, tids: set[str] | None, floor: float) -> bool:
        am = branch_amt.get(d, {})
        if tids is None:
            return any(v >= floor for v in am.values())
        return any(am.get(t, 0.0) >= floor for t in tids)

    # ---- Whale_T+1 baseline trades ----
    whale_trades: list[dict] = []
    for hold in HOLDS:
        for T in greens:
            x, ed, xd = excess_l1(T, hold, cal_ext, di, ohlc_px, bench)
            if x is None:
                continue
            whale_trades.append(
                {
                    "sid": SID,
                    "signal_date": T,
                    "entry_date": ed,
                    "exit_date": xd,
                    "arm": "Whale_T+1",
                    "rule": "",
                    "hold": hold,
                    "excess_pct": x,
                    "lens": "event",
                    "split": "IS" if T < OOS else "OOS",
                }
            )
    whale_by_hold = {
        h: [r for r in whale_trades if r["hold"] == h] for h in HOLDS
    }
    log(f"whale H7 trades={len(whale_by_hold[7])}")

    # ---- Chip rule grid: precompute fire days ----
    grid = build_rule_grid()
    if not has_bh:
        grid = [g for g in grid if "bhchg" not in g]
    log(f"chip rule grid size={len(grid)}")

    # vectorized-ish: for each calendar day, evaluate all atoms once
    day_atom: dict[str, dict[str, bool]] = {}
    for d in cal:
        try:
            row = feat_idx.loc[(SID, d)]
        except KeyError:
            continue
        if isinstance(row, pd.DataFrame):
            row = row.iloc[0]
        day_atom[d] = {a.id: atom_mask(row, a.id) for a in ATOMS}

    def fire_days_for(atoms: tuple[str, ...], day_lo: str, day_hi: str) -> set[str]:
        out: set[str] = set()
        for d, am in day_atom.items():
            if d < day_lo or d > day_hi:
                continue
            if all(am.get(a, False) for a in atoms):
                out.add(d)
        return out

    # green event T−2 decision days
    green_t2: dict[str, str | None] = {}  # T -> T-2
    for T in greens:
        green_t2[T] = lag_date(cal_ext, di, T, 2)

    # control days for false-alarm / branch lift: non-green ±2 window
    green_near: set[str] = set()
    for T in greens:
        i = di.get(T)
        if i is None:
            continue
        for j in range(max(0, i - 2), min(len(cal_ext), i + 3)):
            if cal_ext[j] in cal:
                green_near.add(cal_ext[j])
    quiet_days = [d for d in cal if d not in green_near and d in day_atom]

    # ---- Sweep A: chip rules on standalone IS ----
    sweep_rows: list[dict] = []
    for atoms in grid:
        rid = rule_id(atoms)
        fires = fire_days_for(atoms, D0, d1)
        # event-tied coverage @ T−2
        hit_t2 = 0
        elig = 0
        for T, d2 in green_t2.items():
            if d2 is None:
                continue
            elig += 1
            if d2 in fires:
                hit_t2 += 1
        fa = sum(1 for d in quiet_days if d in fires)
        fa_rate = fa / len(quiet_days) if quiet_days else np.nan

        for hold in (PRIMARY_HOLD,):
            # standalone IS/OOS/full
            for split, lo, hi in (
                ("IS", D0, "2025-12-31"),
                ("OOS", OOS, d1),
                ("full", D0, d1),
            ):
                tr = standalone_trades(
                    fires, hold, cal_ext, di, ohlc_px, bench, "Chip_D_Enter_D1", rid, lo, hi
                )
                xs = [r["excess_pct"] for r in tr]
                sm = summarize(xs)
                # event-tied paired: among greens with rule@T−2, enter T−1 vs whale T+1
                cand_xs: list[float] = []
                wh_xs: list[float] = []
                for T, d2 in green_t2.items():
                    if d2 is None or d2 not in fires:
                        continue
                    if split == "IS" and T >= OOS:
                        continue
                    if split == "OOS" and T < OOS:
                        continue
                    # enter at T−1 = day after T−2 = cal[di[T]-1]
                    iT = di.get(T)
                    if iT is None or iT < 1:
                        continue
                    t_m1 = cal_ext[iT - 1]
                    # excess from signal = T−2 → enter T−1 open, exit relative to T−2
                    cx, _, _ = excess_l1(d2, hold, cal_ext, di, ohlc_px, bench)
                    wx, _, _ = excess_l1(T, hold, cal_ext, di, ohlc_px, bench)
                    if cx is None or wx is None:
                        continue
                    # verify entry is T−1
                    if di[d2] + 1 < len(cal_ext) and cal_ext[di[d2] + 1] != t_m1:
                        pass  # still use excess_l1(d2) which enters d2+1
                    cand_xs.append(cx)
                    wh_xs.append(wx)
                boot_ev = bootstrap_delta(cand_xs, wh_xs, paired=True)
                wh_stand = [
                    r["excess_pct"]
                    for r in whale_by_hold[hold]
                    if split == "full" or r["split"] == split
                ]
                boot_st = bootstrap_delta(xs, wh_stand, paired=False)

                sweep_rows.append(
                    {
                        "rule_id": rid,
                        "rule_label": rule_label(atoms),
                        "n_atoms": len(atoms),
                        "hold": hold,
                        "split": split,
                        "coverage_t2": hit_t2 / elig if elig else np.nan,
                        "n_green_hit_t2": hit_t2,
                        "n_green_elig": elig,
                        "false_alarm_days": fa,
                        "false_alarm_rate": fa_rate,
                        "standalone_n": sm["n"],
                        "standalone_hit": sm["hit_rate"],
                        "standalone_med": sm["med_excess"],
                        "standalone_mean": sm["mean_excess"],
                        "standalone_sum": sm["sum_excess"],
                        "event_n": len(cand_xs),
                        "event_cand_mean": float(np.mean(cand_xs)) if cand_xs else np.nan,
                        "event_whale_mean": float(np.mean(wh_xs)) if wh_xs else np.nan,
                        "event_delta_mean": boot_ev["delta_mean"],
                        "event_p_le0": boot_ev["p_delta_le0"],
                        "standalone_vs_whale_delta": boot_st["delta_mean"],
                        "standalone_vs_whale_p_le0": boot_st["p_delta_le0"],
                        "whale_n_split": len(wh_stand),
                        "whale_mean_split": float(np.mean(wh_stand)) if wh_stand else np.nan,
                    }
                )

    sweep_df = pd.DataFrame(sweep_rows)
    sweep_path = OUT / "2327_chip_branch_sweep_grid.csv"
    sweep_df.to_csv(sweep_path, index=False)
    log(f"wrote sweep grid n={len(sweep_df)} → {sweep_path.name}")

    # Freeze: prefer rules that actually hit green T−2 (coverage>0), then IS mean
    is_df = sweep_df[sweep_df["split"] == "IS"].copy()
    cand = is_df[is_df["standalone_n"] >= MIN_STANDALONE_IS_N].copy()
    if cand.empty:
        cand = is_df[is_df["standalone_n"] > 0].copy()
        log("WARN: no IS rule meets min standalone n; using all IS with n>0")
    covered = cand[cand["coverage_t2"].fillna(0) > 0]
    if len(covered) >= 3:
        cand = covered
        log(f"freeze pool: IS rules with T−2 coverage>0 n={len(cand)}")
    else:
        log(
            "WARN: few IS rules with green T−2 coverage; "
            "freeze may optimize standalone only"
        )
    tight = cand[cand["false_alarm_rate"].fillna(1) <= 0.40]
    if len(tight) >= 3:
        cand = tight
    # score: mean, then coverage, then med, then lower FA
    cand = cand.assign(
        _score=cand["standalone_mean"].fillna(-999)
        + 2.0 * cand["coverage_t2"].fillna(0)
        - 0.5 * cand["false_alarm_rate"].fillna(1)
    ).sort_values(
        by=["_score", "standalone_mean", "coverage_t2"],
        ascending=[False, False, False],
    )
    best_chip_row = cand.iloc[0]
    best_chip_id = str(best_chip_row["rule_id"])
    best_chip_atoms = tuple(best_chip_id.split("+"))
    log(
        f"FROZEN chip={best_chip_id} IS n={int(best_chip_row['standalone_n'])} "
        f"mean={best_chip_row['standalone_mean']:+.2f} "
        f"cov={best_chip_row['coverage_t2']:.0%} "
        f"FA={best_chip_row['false_alarm_rate']:.0%}"
    )

    # Top chip alternatives for report
    top_chip = cand.head(10)

    # ---- Branch ranking @ T−2 on 2327 ----
    # lift = P(active@T−2 | green) / P(active | quiet)
    branch_rank_rows: list[dict] = []
    tids_all = sorted({t for am in branch_amt.values() for t in am})
    for floor, floor_name in ((FLOOR_0P5, "0.5億"), (FLOOR_1, "1億"), (0.0, "net>0")):
        for tid in tids_all:
            hit = 0
            elig = 0
            for T, d2 in green_t2.items():
                if d2 is None:
                    continue
                elig += 1
                amt = branch_amt.get(d2, {}).get(tid, 0.0)
                ok = amt > 0 if floor <= 0 else amt >= floor
                if ok:
                    hit += 1
            ctrl_hit = 0
            for d in quiet_days:
                amt = branch_amt.get(d, {}).get(tid, 0.0)
                ok = amt > 0 if floor <= 0 else amt >= floor
                if ok:
                    ctrl_hit += 1
            p_e = hit / elig if elig else np.nan
            p_c = ctrl_hit / len(quiet_days) if quiet_days else np.nan
            lift = (p_e / p_c) if p_c and p_c > 0 else (np.inf if hit else np.nan)
            branch_rank_rows.append(
                {
                    "tid": tid,
                    "name": name_map.get(tid, ""),
                    "floor": floor_name,
                    "is_champion": tid in core,
                    "event_hit": hit,
                    "event_n": elig,
                    "coverage": p_e,
                    "ctrl_hit": ctrl_hit,
                    "ctrl_n": len(quiet_days),
                    "ctrl_rate": p_c,
                    "lift": lift if lift != np.inf else 99.0,
                }
            )
    br_df = pd.DataFrame(branch_rank_rows)
    br_path = OUT / "2327_branch_rank_t2.csv"
    br_df.to_csv(br_path, index=False)

    # pick top seats @ 0.5億: require ≥2 hits; rank lift then coverage
    br05 = br_df[br_df["floor"] == "0.5億"].copy()
    br05_hit = br05[br05["event_hit"] >= 2].sort_values(
        ["lift", "coverage", "event_hit"], ascending=[False, False, False]
    )
    top_seats = br05_hit.head(TOP_BRANCH_SEATS)["tid"].tolist()
    # always include champions with ≥1 hit @ T−2
    for tid in core:
        sub = br05[br05["tid"] == tid]
        if not sub.empty and int(sub.iloc[0]["event_hit"]) >= 1 and tid not in top_seats:
            top_seats.append(tid)
    log(f"top branch seats @T−2 0.5億: {[(t, name_map.get(t,'')) for t in top_seats[:8]]}")

    # ---- Branch rules + combos with frozen chip ----
    branch_rules: list[tuple[str, str, object]] = []
    # any champion ≥0.5億
    champ_set = set(core.keys())
    branch_rules.append(
        (
            "champ_0p5",
            "any champion ≥0.5億",
            lambda d, _cs=champ_set: branch_fire(d, _cs, FLOOR_0P5),
        )
    )
    branch_rules.append(
        (
            "champ_1yi",
            "any champion ≥1億",
            lambda d, _cs=champ_set: branch_fire(d, _cs, FLOOR_1),
        )
    )
    for tid in top_seats:  # includes lift top + champion seats with T−2 hits
        nm = name_map.get(tid, tid)
        branch_rules.append(
            (
                f"seat_{tid}_0p5",
                f"{tid}:{nm} ≥0.5億",
                lambda d, _t=tid: branch_fire(d, {_t}, FLOOR_0P5),
            )
        )

    frozen_fires = fire_days_for(best_chip_atoms, D0, d1)

    combo_defs: list[tuple[str, str, set[str]]] = []
    # chip alone
    combo_defs.append(("chip_only", rule_label(best_chip_atoms), frozen_fires))
    for brid, brlab, brfn in branch_rules:
        bf = {d for d in cal if brfn(d)}
        combo_defs.append((brid, brlab, bf))
        combo_defs.append(
            (f"combo_{brid}", f"({rule_label(best_chip_atoms)}) ∧ ({brlab})", frozen_fires & bf)
        )

    # evaluate combos
    all_trades: list[dict] = list(whale_trades)
    combo_summary: list[dict] = []

    for cid, clab, fires in combo_defs:
        # event-tied
        for hold in HOLDS:
            for split in ("full", "IS", "OOS"):
                cand_xs = []
                wh_xs = []
                paired_rows = []
                for T, d2 in green_t2.items():
                    if d2 is None:
                        continue
                    if split == "IS" and T >= OOS:
                        continue
                    if split == "OOS" and T < OOS:
                        continue
                    if d2 not in fires:
                        continue
                    cx, ced, cxd = excess_l1(d2, hold, cal_ext, di, ohlc_px, bench)
                    wx, wed, wxd = excess_l1(T, hold, cal_ext, di, ohlc_px, bench)
                    if cx is None or wx is None:
                        continue
                    cand_xs.append(cx)
                    wh_xs.append(wx)
                    paired_rows.append(
                        {
                            "sid": SID,
                            "signal_date": d2,
                            "green_T": T,
                            "entry_date": ced,
                            "exit_date": cxd,
                            "arm": "Decide_T-2_Enter_T-1",
                            "rule": cid,
                            "hold": hold,
                            "excess_pct": cx,
                            "whale_excess_pct": wx,
                            "lens": "event_tied",
                            "split": "IS" if T < OOS else "OOS",
                        }
                    )
                boot = bootstrap_delta(cand_xs, wh_xs, paired=True)
                sm = summarize(cand_xs)
                hit_cov = sum(
                    1
                    for T, d2 in green_t2.items()
                    if d2 and d2 in fires
                    and (split == "full" or (split == "IS" and T < OOS) or (split == "OOS" and T >= OOS))
                )
                n_g = sum(
                    1
                    for T in greens
                    if split == "full"
                    or (split == "IS" and T < OOS)
                    or (split == "OOS" and T >= OOS)
                )
                combo_summary.append(
                    {
                        "rule": cid,
                        "label": clab,
                        "lens": "event_tied",
                        "hold": hold,
                        "split": split,
                        "n": sm["n"],
                        "coverage": hit_cov / n_g if n_g else np.nan,
                        "hit_rate": sm["hit_rate"],
                        "med": sm["med_excess"],
                        "mean": sm["mean_excess"],
                        "sum": sm["sum_excess"],
                        "delta_vs_whale": boot["delta_mean"],
                        "ci_lo": boot["ci_lo"],
                        "ci_hi": boot["ci_hi"],
                        "p_le0": boot["p_delta_le0"],
                        "verdict": verdict_word(boot),
                        "whale_paired_mean": float(np.mean(wh_xs)) if wh_xs else np.nan,
                    }
                )
                if hold == PRIMARY_HOLD and split == "full":
                    all_trades.extend(paired_rows)

            # standalone
            for split, lo, hi in (
                ("full", D0, d1),
                ("IS", D0, "2025-12-31"),
                ("OOS", OOS, d1),
            ):
                tr = standalone_trades(
                    fires,
                    hold,
                    cal_ext,
                    di,
                    ohlc_px,
                    bench,
                    "Decide_D_Enter_D1",
                    cid,
                    lo,
                    hi,
                )
                for r in tr:
                    r["rule_label"] = clab
                if hold == PRIMARY_HOLD and split == "full":
                    all_trades.extend(tr)
                xs = [r["excess_pct"] for r in tr]
                wh_xs = [
                    r["excess_pct"]
                    for r in whale_by_hold[hold]
                    if split == "full" or r["split"] == split
                ]
                boot = bootstrap_delta(xs, wh_xs, paired=False)
                sm = summarize(xs)
                fa = sum(1 for d in quiet_days if d in fires)
                combo_summary.append(
                    {
                        "rule": cid,
                        "label": clab,
                        "lens": "standalone",
                        "hold": hold,
                        "split": split,
                        "n": sm["n"],
                        "coverage": np.nan,
                        "hit_rate": sm["hit_rate"],
                        "med": sm["med_excess"],
                        "mean": sm["mean_excess"],
                        "sum": sm["sum_excess"],
                        "delta_vs_whale": boot["delta_mean"],
                        "ci_lo": boot["ci_lo"],
                        "ci_hi": boot["ci_hi"],
                        "p_le0": boot["p_delta_le0"],
                        "verdict": verdict_word(boot),
                        "whale_n": len(wh_xs),
                        "whale_mean": float(np.mean(wh_xs)) if wh_xs else np.nan,
                        "false_alarm_days": fa,
                        "false_alarm_rate": fa / len(quiet_days) if quiet_days else np.nan,
                        "per_trade_vs_whale": (
                            sm["mean_excess"] - float(np.mean(wh_xs))
                            if xs and wh_xs
                            else np.nan
                        ),
                    }
                )

    # whale summary rows
    for hold in HOLDS:
        for split in ("full", "IS", "OOS"):
            xs = [
                r["excess_pct"]
                for r in whale_by_hold[hold]
                if split == "full" or r["split"] == split
            ]
            sm = summarize(xs, "Whale_T+1")
            combo_summary.append(
                {
                    "rule": "Whale_T+1",
                    "label": "綠燈 T → L1 開盤",
                    "lens": "baseline",
                    "hold": hold,
                    "split": split,
                    "n": sm["n"],
                    "coverage": 1.0,
                    "hit_rate": sm["hit_rate"],
                    "med": sm["med_excess"],
                    "mean": sm["mean_excess"],
                    "sum": sm["sum_excess"],
                    "delta_vs_whale": 0.0,
                    "ci_lo": 0.0,
                    "ci_hi": 0.0,
                    "p_le0": np.nan,
                    "verdict": "基準",
                }
            )

    cs_df = pd.DataFrame(combo_summary)
    trades_df = pd.DataFrame(all_trades)

    # pick best combo: prefer event-tied with n>=3, else n>=2; require coverage>=0.15
    et = cs_df[
        (cs_df.lens == "event_tied")
        & (cs_df.hold == PRIMARY_HOLD)
        & (cs_df.split == "full")
        & (cs_df.rule != "Whale_T+1")
    ].copy()
    et_ok = et[(et["n"] >= 3) & (et["coverage"].fillna(0) >= 0.15)].sort_values(
        "delta_vs_whale", ascending=False
    )
    if et_ok.empty:
        et_ok = et[et["n"] >= 2].sort_values("delta_vs_whale", ascending=False)
    best_combo_row = et_ok.iloc[0] if not et_ok.empty else None

    st = cs_df[
        (cs_df.lens == "standalone")
        & (cs_df.hold == PRIMARY_HOLD)
        & (cs_df.split == "full")
        & (
            cs_df.rule.str.startswith("combo_")
            | (cs_df.rule == "chip_only")
            | cs_df.rule.str.startswith("champ_")
            | cs_df.rule.str.startswith("seat_")
        )
    ].copy()
    # Prefer chip/combo with n>=5; rank by delta then sum
    st_ok = st[st["n"] >= MIN_STANDALONE_IS_N].sort_values(
        ["delta_vs_whale", "sum"], ascending=[False, False]
    )
    best_stand_row = st_ok.iloc[0] if not st_ok.empty else (st.iloc[0] if not st.empty else None)

    # freeze artifact
    frozen = {
        "sid": SID,
        "name": "國巨",
        "protocol": {
            "decide": "T-2 / D close",
            "enter": "D+1 open (L1)",
            "hold": PRIMARY_HOLD,
            "cost": COST,
            "beta": BETA,
            "window": f"{D0}..{d1}",
            "oos": OOS,
        },
        "n_green": len(greens),
        "n_green_IS": n_is_g,
        "n_green_OOS": n_oos_g,
        "best_chip_rule_id": best_chip_id,
        "best_chip_rule_label": rule_label(best_chip_atoms),
        "best_chip_IS": best_chip_row.to_dict(),
        "core_champions": core,
        "top_branch_seats_t2": [
            {
                "tid": t,
                "name": name_map.get(t, ""),
                **br05[br05.tid == t].iloc[0][
                    ["coverage", "lift", "event_hit"]
                ].to_dict(),
            }
            for t in top_seats
            if not br05[br05.tid == t].empty
        ],
        "best_combo_event": best_combo_row.to_dict() if best_combo_row is not None else None,
        "best_standalone": best_stand_row.to_dict() if best_stand_row is not None else None,
    }
    frozen_path = OUT / "2327_frozen_rule.json"
    frozen_path.write_text(
        json.dumps(frozen, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )

    trades_path = OUT / "2327_chip_branch_trades.csv"
    summary_path = OUT / "2327_chip_branch_combo_summary.csv"
    trades_df.to_csv(trades_path, index=False)
    cs_df.to_csv(summary_path, index=False)
    top_chip.to_csv(OUT / "2327_chip_top_is.csv", index=False)

    # ---- Markdown report ----
    md = build_report(
        greens=greens,
        n_is_g=n_is_g,
        n_oos_g=n_oos_g,
        core=core,
        name_map=name_map,
        best_chip_id=best_chip_id,
        best_chip_atoms=best_chip_atoms,
        best_chip_row=best_chip_row,
        top_chip=top_chip,
        br05=br05,
        top_seats=top_seats,
        cs_df=cs_df,
        best_combo_row=best_combo_row,
        best_stand_row=best_stand_row,
        has_bh=has_bh,
        d1=d1,
        grid_n=len(grid),
        quiet_n=len(quiet_days),
    )
    md_path = OUT / "2327_CHIP_BRANCH_T2_T1.md"
    md_path.write_text(md, encoding="utf-8")

    status = {
        "phase": "done",
        "at": datetime.now().isoformat(timespec="seconds"),
        "elapsed_min": round((time.time() - t0) / 60, 2),
        "best_chip": best_chip_id,
        "best_combo": None if best_combo_row is None else str(best_combo_row["rule"]),
        "n_green": len(greens),
        "out": str(md_path),
    }
    (OUT / "status_2327_chip_branch.json").write_text(
        json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log(f"DONE → {md_path} ({status['elapsed_min']} min)")
    return 0


def build_report(
    *,
    greens: list[str],
    n_is_g: int,
    n_oos_g: int,
    core: dict[str, str],
    name_map: dict[str, str],
    best_chip_id: str,
    best_chip_atoms: tuple[str, ...],
    best_chip_row: pd.Series,
    top_chip: pd.DataFrame,
    br05: pd.DataFrame,
    top_seats: list[str],
    cs_df: pd.DataFrame,
    best_combo_row: pd.Series | None,
    best_stand_row: pd.Series | None,
    has_bh: bool,
    d1: str,
    grid_n: int,
    quiet_n: int,
) -> str:
    lines: list[str] = []
    lines.append("# 2327 國巨 · 籌碼×分點 Decide_T−2 / Enter_T−1 vs Whale_T+1\n\n")
    lines.append(f"- 生成：{datetime.now().isoformat(timespec='seconds')}\n")
    lines.append("- Runner：`scripts/research/run_2327_chip_branch_t2_t1_sweep.py`\n")
    lines.append(
        f"- 窗：{D0}..{d1}（≈{YEARS:.2f}y）· OOS≥{OOS} · COST={COST} · β={BETA}×IX0001\n"
    )
    lines.append(
        "- 進場慣例：**L1 開盤**（決策日 D 收盤判定 → 次一交易日開盤進；"
        "事件對齊時 D=T−2 → 進 T−1 開）\n"
    )
    lines.append(
        f"- 綠燈事件 n={len(greens)}（IS={n_is_g} / OOS={n_oos_g}）—"
        "**樣本偏薄，顯著性勿過度解讀**\n"
    )
    lines.append(
        f"- 籌碼掃參格子={grid_n} · big_holder 可用={has_bh} · "
        f"安靜對照日={quiet_n}\n"
    )
    lines.append("- Research only · **未採納**進 Strategy／Order\n\n")

    # Verdict
    whale_full = cs_df[
        (cs_df.rule == "Whale_T+1")
        & (cs_df.hold == PRIMARY_HOLD)
        & (cs_df.split == "full")
        & (cs_df.lens == "baseline")
    ]
    wh = whale_full.iloc[0] if not whale_full.empty else None

    lines.append("## 結論（先讀）\n\n")

    # Honest composite: standalone must not lose badly; event n usually tiny
    stand_verdict = (
        str(best_stand_row["verdict"]) if best_stand_row is not None else "—"
    )
    event_verdict = (
        str(best_combo_row["verdict"]) if best_combo_row is not None else "—"
    )
    # Overall: cannot claim replace whale unless standalone clearly better with n>=15
    overall = "差不多／未證實可取代"
    if best_stand_row is not None:
        sn = int(best_stand_row["n"])
        sdelta = float(best_stand_row["delta_vs_whale"]) if pd.notna(best_stand_row["delta_vs_whale"]) else 0.0
        if sn >= 15 and sdelta >= 1.0 and stand_verdict.startswith("更好"):
            overall = "更好"
        elif sn >= 8 and sdelta <= -1.0 and (
            stand_verdict.startswith("更差") or "更差" in stand_verdict
        ):
            overall = "更差（日曆獨立）"
        elif best_stand_row is not None and wh is not None:
            # compare sum: if whale sum much larger, say 更差 for replace purpose
            if float(wh["sum"]) > 0 and float(best_stand_row.get("sum") or 0) < 0.7 * float(wh["sum"]):
                overall = "更差（日曆合計不及跟單）"

    lines.append(
        f"**總評相對原本跟單 Whale_T+1：{overall}**"
        f"（綠燈僅 n={len(greens)}，IS={n_is_g}；不可因小樣本配對 Δ 宣稱採納）。\n\n"
    )
    if wh is not None:
        lines.append(
            f"- Whale_T+1 基準 H={PRIMARY_HOLD} full：n={int(wh['n'])} · "
            f"hit={fmt_rate(wh['hit_rate'])} · med={fmt_pct(wh['med'])} · "
            f"mean={fmt_pct(wh['mean'])} · sum={fmt_pct(wh['sum'], 0)}\n"
        )
    if best_combo_row is not None:
        lines.append(
            f"- **A 事件對齊**最佳：`{best_combo_row['rule']}` = {best_combo_row['label']} → "
            f"{event_verdict} · n={int(best_combo_row['n'])} · "
            f"覆蓋={fmt_rate(best_combo_row['coverage'])} · "
            f"Δmean={fmt_pct(best_combo_row['delta_vs_whale'])} pp · "
            f"CI [{fmt_pct(best_combo_row['ci_lo'])}, {fmt_pct(best_combo_row['ci_hi'])}] · "
            f"P(Δ≤0)={fmt_p(best_combo_row['p_le0'])}\n"
        )
    if best_stand_row is not None:
        lines.append(
            f"- **B 日曆獨立**對照：`{best_stand_row['rule']}` → {stand_verdict} · "
            f"n={int(best_stand_row['n'])} · mean={fmt_pct(best_stand_row['mean'])} · "
            f"sum={fmt_pct(best_stand_row['sum'], 0)} · "
            f"Δmean vs Whale={fmt_pct(best_stand_row['delta_vs_whale'])} pp\n"
        )
    lines.append(
        f"- 凍結籌碼規則：**`{best_chip_id}`** = {rule_label(best_chip_atoms)}\n"
        f"  （IS 日曆 n={int(best_chip_row['standalone_n'])} · "
        f"mean={fmt_pct(best_chip_row['standalone_mean'])} · "
        f"綠燈 T−2 覆蓋={fmt_rate(best_chip_row['coverage_t2'])} · "
        f"安靜日誤報率={fmt_rate(best_chip_row['false_alarm_rate'])}）\n"
    )
    lines.append(
        "- 冠軍席："
        + "、".join(f"{tid} {nm}" for tid, nm in core.items())
        + "\n"
    )
    lines.append(
        "- T−2 高 lift 分點（≥0.5億，event_hit≥2）："
        + "、".join(
            f"{t} {name_map.get(t,'')} "
            f"(cov={fmt_rate(br05[br05.tid==t].iloc[0]['coverage'] if not br05[br05.tid==t].empty else np.nan)}, "
            f"lift={br05[br05.tid==t].iloc[0]['lift']:.1f})"
            for t in top_seats[:5]
            if not br05[br05.tid == t].empty
        )
        + "\n\n"
    )

    if n_is_g < 5:
        lines.append(
            f"> ⚠️ 2327 綠燈 IS 僅 **{n_is_g}** 筆，事件對齊掃參**無法**在 IS 上可靠凍結；"
            "籌碼規則以**日曆級 IS（且要求綠燈 T−2 覆蓋>0）**凍結，"
            "事件對齊指標以 full／OOS 報告。lift≫1 常因安靜日幾乎不出現（稀有事件）。\n\n"
        )

    # Baseline table
    lines.append("## Whale_T+1 基準（綠燈 T → L1 開）\n\n")
    lines.append("| hold | split | n | hit | med% | mean% | sum% |\n")
    lines.append("|-----:|-------|--:|----:|-----:|------:|-----:|\n")
    for hold in HOLDS:
        for split in ("full", "IS", "OOS"):
            m = cs_df[
                (cs_df.rule == "Whale_T+1")
                & (cs_df.hold == hold)
                & (cs_df.split == split)
                & (cs_df.lens == "baseline")
            ]
            if m.empty:
                continue
            r = m.iloc[0]
            lines.append(
                f"| {hold} | {split} | {int(r['n'])} | {fmt_rate(r['hit_rate'])} | "
                f"{fmt_pct(r['med'])} | {fmt_pct(r['mean'])} | {fmt_pct(r['sum'], 0)} |\n"
            )
    lines.append("\n")

    # Top chip IS
    lines.append("## 籌碼掃參 · IS Top（日曆獨立 · 凍結依據）\n\n")
    lines.append(
        "| rank | rule | n | hit | mean% | med% | sum% | T−2覆蓋 | 誤報率 | "
        "OOS mean% | event Δ |\n"
    )
    lines.append(
        "|-----:|------|--:|----:|------:|-----:|-----:|--------:|-------:|"
        "---------:|--------:|\n"
    )
    for i, (_, r) in enumerate(top_chip.iterrows(), 1):
        lines.append(
            f"| {i} | `{r['rule_id']}` | {int(r['standalone_n'])} | "
            f"{fmt_rate(r['standalone_hit'])} | {fmt_pct(r['standalone_mean'])} | "
            f"{fmt_pct(r['standalone_med'])} | {fmt_pct(r['standalone_sum'], 0)} | "
            f"{fmt_rate(r['coverage_t2'])} | {fmt_rate(r['false_alarm_rate'])} | "
            f"— | {fmt_pct(r['event_delta_mean'])} |\n"
        )
    lines.append(
        f"\n凍結 = rank1 `{best_chip_id}`。"
        " event Δ = 綠燈命中子集上（Cand@T−1 − Whale@T+1）配對均值（IS 列；n 可能極小）。\n\n"
    )

    # Branch rank
    lines.append("## 分點 T−2 排名（≥0.5億 · vs 安靜日 lift）\n\n")
    lines.append("| tid | 名稱 | 冠軍 | coverage | lift | n_hit/n |\n")
    lines.append("|-----|------|:----:|---------:|-----:|--------:|\n")
    # champions first, then lift top
    champ_rows = br05[br05["is_champion"]].sort_values(
        ["event_hit", "lift"], ascending=[False, False]
    )
    show = br05[br05["event_hit"] >= 1].sort_values(
        ["lift", "coverage"], ascending=[False, False]
    ).head(10)
    seen: set[str] = set()
    for _, r in pd.concat([champ_rows, show], ignore_index=True).iterrows():
        tid = str(r["tid"])
        if tid in seen:
            continue
        seen.add(tid)
        lines.append(
            f"| {r['tid']} | {r['name']} | {'Y' if r['is_champion'] else ''} | "
            f"{fmt_rate(r['coverage'])} | {r['lift']:.2f} | "
            f"{int(r['event_hit'])}/{int(r['event_n'])} |\n"
        )
        if len(seen) >= 15:
            break
    lines.append("\n")

    # Event-tied tables
    lines.append("## A) 事件對齊 · 同綠燈：規則@T−2 → 進 T−1 vs Whale_T+1\n\n")
    lines.append(f"### H={PRIMARY_HOLD}\n\n")
    lines.append(
        "| rule | split | n | 覆蓋 | hit | mean% | Δ vs Whale | 95%CI | P(Δ≤0) | 判定 |\n"
    )
    lines.append(
        "|------|-------|--:|-----:|----:|------:|-----------:|------:|-------:|------|\n"
    )
    et = cs_df[
        (cs_df.lens == "event_tied") & (cs_df.hold == PRIMARY_HOLD)
    ].sort_values(["split", "delta_vs_whale"], ascending=[True, False])
    for _, r in et.iterrows():
        lines.append(
            f"| `{r['rule']}` | {r['split']} | {int(r['n'])} | {fmt_rate(r['coverage'])} | "
            f"{fmt_rate(r['hit_rate'])} | {fmt_pct(r['mean'])} | "
            f"{fmt_pct(r['delta_vs_whale'])} | "
            f"[{fmt_pct(r['ci_lo'])}, {fmt_pct(r['ci_hi'])}] | "
            f"{fmt_p(r['p_le0'])} | {r['verdict']} |\n"
        )
    lines.append("\n")

    # Standalone
    lines.append("## B) 日曆獨立 · 規則@D → 進 D+1（非重疊）vs Whale_T+1\n\n")
    lines.append(f"### H={PRIMARY_HOLD}\n\n")
    lines.append(
        "| rule | split | n | hit | mean% | med% | sum% | "
        "Δmean vs Whale | per-trade Δ | P(Δ≤0) | 判定 |\n"
    )
    lines.append(
        "|------|-------|--:|----:|------:|-----:|-----:|"
        "---------------:|-----------:|-------:|------|\n"
    )
    st = cs_df[
        (cs_df.lens == "standalone") & (cs_df.hold == PRIMARY_HOLD)
    ].sort_values(["split", "delta_vs_whale"], ascending=[True, False])
    # also insert whale for reference per split
    for split in ("full", "IS", "OOS"):
        wm = cs_df[
            (cs_df.rule == "Whale_T+1")
            & (cs_df.hold == PRIMARY_HOLD)
            & (cs_df.split == split)
            & (cs_df.lens == "baseline")
        ]
        if not wm.empty:
            r = wm.iloc[0]
            lines.append(
                f"| Whale_T+1 | {split} | {int(r['n'])} | {fmt_rate(r['hit_rate'])} | "
                f"{fmt_pct(r['mean'])} | {fmt_pct(r['med'])} | {fmt_pct(r['sum'], 0)} | "
                f"0 | 0 | — | 基準 |\n"
            )
        sub = st[st.split == split]
        for _, r in sub.iterrows():
            ptd = r.get("per_trade_vs_whale", np.nan)
            lines.append(
                f"| `{r['rule']}` | {split} | {int(r['n'])} | {fmt_rate(r['hit_rate'])} | "
                f"{fmt_pct(r['mean'])} | {fmt_pct(r['med'])} | {fmt_pct(r['sum'], 0)} | "
                f"{fmt_pct(r['delta_vs_whale'])} | {fmt_pct(ptd)} | "
                f"{fmt_p(r['p_le0'])} | {r['verdict']} |\n"
            )
    lines.append("\n")

    # H10 sensitivity brief
    lines.append("## 敏感度 H=10（摘要）\n\n")
    lines.append("| rule | lens | split | n | mean% | Δ vs Whale | 判定 |\n")
    lines.append("|------|------|-------|--:|------:|-----------:|------|\n")
    h10 = cs_df[
        (cs_df.hold == 10)
        & (cs_df.split == "full")
        & (
            cs_df.rule.isin(
                ["Whale_T+1", "chip_only"]
                + (
                    [str(best_combo_row["rule"])]
                    if best_combo_row is not None
                    else []
                )
            )
        )
    ]
    for _, r in h10.iterrows():
        lines.append(
            f"| `{r['rule']}` | {r['lens']} | full | {int(r['n'])} | "
            f"{fmt_pct(r['mean'])} | {fmt_pct(r['delta_vs_whale'])} | {r['verdict']} |\n"
        )
    lines.append("\n")

    lines.append("## 方法備註\n\n")
    lines.append(
        "- Whale_T+1：expert-pool knowledge `trades/2327.json` 綠燈日 T → **次日開盤**進、"
        f"持約 H 日（與既有 L1H{PRIMARY_HOLD} 同構：exit=`cal[i_signal+H]`）。\n"
    )
    lines.append(
        "- Decide_T−2_Enter_T−1：僅用 ≤T−2 資料；籌碼／分點在 T−2 收盤判定，"
        "T−1 開盤進場。\n"
    )
    lines.append(
        "- 日曆獨立：任意日 D 規則成立 → D+1 開進；同股非重疊持倉窗。\n"
    )
    lines.append(
        "- 判定「更好／更差」：n≥8 且 bootstrap CI 不跨 0，或 |Δ|≥1pp 且 "
        "P 門檻＋n≥15；**n 偏小時僅點估計方向，標為軟判定**。\n"
    )
    lines.append(
        "- 產物：`2327_chip_branch_sweep_grid.csv` · `2327_frozen_rule.json` · "
        "`2327_branch_rank_t2.csv` · `2327_chip_branch_trades.csv` · "
        "`2327_chip_branch_combo_summary.csv`\n"
    )
    return "".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
