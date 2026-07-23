#!/usr/bin/env python3
"""2327 國巨 · 獨立籌碼×分點策略（不綁 Whale 綠燈）掃參與凍結.

Research only · 未採納 · Book only.

Protocol (PIT):
  - Decision day D close: chip as-of D; branch on D and D−1
    (D = T−1, D−1 = T−2 relative to entry E = D+1)
  - If rule fires → enter D+1 open (L1); hold H; non-overlapping per sid
  - Whale_T+1 is an *external* benchmark arm (expert-pool greens → T+1 open),
    not an event filter / paired subset

    PYTHONPATH=src .venv/bin/python scripts/research/run_2327_standalone_chip_branch.py
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
FLOOR_0P5 = 5e7
FLOOR_1 = 1e8
EXTRA_SEATS = ("9100", "9665", "9600", "9869", "989P", "8888")
MIN_IS_N = 8
MIN_IS_N_FALLBACK = 5
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
WINDOWS = ("d", "dm1", "both", "either")
FLOORS = (
    ("net", 0.0, "net>0"),
    ("0p5", FLOOR_0P5, "≥0.5億"),
    ("1yi", FLOOR_1, "≥1億"),
)


def log(m: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {m}", flush=True)


def is_foreign(name: str) -> bool:
    return any(k in (name or "") for k in FOREIGN_KW)


# ---- chip atoms ---------------------------------------------------------


@dataclass(frozen=True)
class Atom:
    id: str
    group: str
    label: str


ATOMS: list[Atom] = [
    Atom("ts1", "ts", "three_streak≥1"),
    Atom("ts2", "ts", "three_streak≥2"),
    Atom("ts3", "ts", "three_streak≥3"),
    Atom("fsum5", "fsum", "foreign_sum_5d>0"),
    Atom("ssum5", "ssum", "three_sum_5d>0"),
    Atom("trust", "trust", "trust_net>0"),
    Atom("ldrop", "ldrop", "lending_drop"),
    Atom("bhchg", "bhchg", "big_holder_pct_chg>0"),
]
ATOM_BY_ID = {a.id: a for a in ATOMS}

# Focused chip grid (include prior freeze + W1-like ts2)
CHIP_RULES: list[tuple[str, ...]] = [
    ("ts1",),
    ("ts2",),
    ("ts3",),
    ("fsum5",),
    ("ssum5",),
    ("trust",),
    ("ldrop",),
    ("bhchg",),
    ("ts1", "bhchg"),  # prior freeze
    ("ts2", "bhchg"),
    ("ts1", "fsum5"),
    ("ts2", "fsum5"),
    ("ts1", "ssum5"),
    ("ts2", "ldrop"),
    ("ts1", "trust"),
    ("fsum5", "bhchg"),
    ("ssum5", "bhchg"),
    ("trust", "bhchg"),
    ("ldrop", "bhchg"),
    ("ts2", "ssum5"),
]


def enrich_sums(feat: pd.DataFrame) -> pd.DataFrame:
    g = feat.sort_values(["sid", "d"]).copy()
    for col_src, col_out, w in (
        ("foreign_net", "foreign_sum_3d", 3),
        ("three_buy_net", "three_sum_3d", 3),
    ):
        if col_src not in g.columns:
            g[col_out] = np.nan
            continue
        g[col_out] = g.groupby("sid")[col_src].transform(
            lambda s: s.rolling(w, min_periods=1).sum()
        )
    if "lending_drop" not in g.columns:
        g["lending_drop"] = (
            (g.get("lending_drop_streak", pd.Series(0, index=g.index)).fillna(0) >= 1)
            | (g.get("lending_change", pd.Series(0, index=g.index)).fillna(0) < 0)
        ).astype(int)
    return g


def atom_mask(feat_row: pd.Series, atom_id: str) -> bool:
    if atom_id in ("ts1", "ts2", "ts3"):
        return float(feat_row.get("three_streak", 0) or 0) >= int(atom_id[-1])
    if atom_id == "fsum5":
        return float(feat_row.get("foreign_sum_5d", 0) or 0) > 0
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


def chip_label(atoms: tuple[str, ...]) -> str:
    return " ∧ ".join(ATOM_BY_ID[a].label for a in atoms)


def chip_id(atoms: tuple[str, ...]) -> str:
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
    """Enter next open after signal_d; exit cal[i+hold] close."""
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
        }
    return {
        "arm": label,
        "n": n,
        "hit_rate": float((a > 0).mean()),
        "med_excess": float(np.median(a)),
        "mean_excess": float(a.mean()),
        "sum_excess": float(a.sum()),
    }


def bootstrap_delta(a: list[float], b: list[float]) -> dict:
    """Unpaired bootstrap on mean excess (different populations OK)."""
    aa = np.asarray(a, dtype=float)
    bb = np.asarray(b, dtype=float)
    if aa.size < 5 or bb.size < 5:
        return {
            "delta_mean": float(aa.mean() - bb.mean()) if aa.size and bb.size else np.nan,
            "ci_lo": np.nan,
            "ci_hi": np.nan,
            "p_delta_le0": np.nan,
            "n_a": int(aa.size),
            "n_b": int(bb.size),
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
    }


def verdict_word(boot: dict) -> str:
    n_min = min(boot.get("n_a", 0), boot.get("n_b", 0))
    dm = boot.get("delta_mean", np.nan)
    if np.isnan(dm):
        return "差不多"
    lo, hi = boot.get("ci_lo", np.nan), boot.get("ci_hi", np.nan)
    if n_min < 8:
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
        t = tape.copy()
        t["amt"] = []
        return t
    t = tape.copy()
    t["close"] = [px.get((r.sid, r.d), np.nan) for r in t.itertuples()]
    t["amt"] = t["net"] * t["close"]
    return t


def lag_date(cal: list[str], di: dict[str, int], sig: str, k: int) -> str | None:
    i = di.get(sig)
    if i is None or i < k:
        return None
    return cal[i - k]


def seat_ok(branch_amt: dict[str, dict[str, float]], d: str, tid: str, floor: float) -> bool:
    amt = branch_amt.get(d, {}).get(tid, 0.0)
    if floor <= 0:
        return amt > 0
    return amt >= floor


def any_seat_ok(
    branch_amt: dict[str, dict[str, float]],
    d: str,
    tids: set[str],
    floor: float,
) -> bool:
    return any(seat_ok(branch_amt, d, t, floor) for t in tids)


def window_ok(
    branch_amt: dict[str, dict[str, float]],
    d: str,
    dm1: str | None,
    tids: set[str],
    floor: float,
    window: str,
) -> bool:
    """OR within seats on a day; AND across days for both."""
    if window == "d":
        return any_seat_ok(branch_amt, d, tids, floor)
    if window == "dm1":
        return bool(dm1) and any_seat_ok(branch_amt, dm1, tids, floor)
    if window == "both":
        return (
            bool(dm1)
            and any_seat_ok(branch_amt, d, tids, floor)
            and any_seat_ok(branch_amt, dm1, tids, floor)
        )
    if window == "either":
        return any_seat_ok(branch_amt, d, tids, floor) or (
            bool(dm1) and any_seat_ok(branch_amt, dm1, tids, floor)
        )
    return False


def two_seat_window_ok(
    branch_amt: dict[str, dict[str, float]],
    d: str,
    dm1: str | None,
    t_a: str,
    t_b: str,
    floor: float,
    window: str,
) -> bool:
    """Both seats must fire on the required day(s)."""
    if window == "d":
        return seat_ok(branch_amt, d, t_a, floor) and seat_ok(branch_amt, d, t_b, floor)
    if window == "dm1":
        return (
            bool(dm1)
            and seat_ok(branch_amt, dm1, t_a, floor)
            and seat_ok(branch_amt, dm1, t_b, floor)
        )
    if window == "both":
        return (
            bool(dm1)
            and seat_ok(branch_amt, d, t_a, floor)
            and seat_ok(branch_amt, d, t_b, floor)
            and seat_ok(branch_amt, dm1, t_a, floor)
            and seat_ok(branch_amt, dm1, t_b, floor)
        )
    if window == "either":
        ok_d = seat_ok(branch_amt, d, t_a, floor) and seat_ok(branch_amt, d, t_b, floor)
        ok_m = (
            bool(dm1)
            and seat_ok(branch_amt, dm1, t_a, floor)
            and seat_ok(branch_amt, dm1, t_b, floor)
        )
        return ok_d or ok_m
    return False


def win_label(w: str) -> str:
    return {"d": "D", "dm1": "D−1", "both": "D∧D−1", "either": "D∨D−1"}.get(w, w)


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


def metrics_row(
    trades: list[dict],
    rule: str,
    label: str,
    family: str,
    hold: int,
    split: str,
    whale_xs: list[float],
    years: float,
    extra: dict | None = None,
) -> dict:
    if split == "full":
        xs = [t["excess_pct"] for t in trades]
    elif split == "IS":
        xs = [t["excess_pct"] for t in trades if t["split"] == "IS"]
    else:
        xs = [t["excess_pct"] for t in trades if t["split"] == "OOS"]
    sm = summarize(xs, rule)
    boot = bootstrap_delta(xs, whale_xs) if xs and whale_xs else {
        "delta_mean": np.nan,
        "ci_lo": np.nan,
        "ci_hi": np.nan,
        "p_delta_le0": np.nan,
        "n_a": sm["n"],
        "n_b": len(whale_xs),
    }
    row = {
        "rule": rule,
        "label": label,
        "family": family,
        "hold": hold,
        "split": split,
        "n": sm["n"],
        "hit_rate": sm["hit_rate"],
        "med": sm["med_excess"],
        "mean": sm["mean_excess"],
        "sum": sm["sum_excess"],
        "trades_per_year": sm["n"] / years if years > 0 and sm["n"] else np.nan,
        "delta_vs_whale": boot["delta_mean"],
        "ci_lo": boot["ci_lo"],
        "ci_hi": boot["ci_hi"],
        "p_le0": boot["p_delta_le0"],
        "verdict": "基準" if family == "benchmark_whale" else verdict_word(boot),
        "whale_n": len(whale_xs),
        "whale_mean": float(np.mean(whale_xs)) if whale_xs else np.nan,
    }
    if extra:
        row.update(extra)
    return row


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


def is_score(row: pd.Series) -> float:
    """IS freeze score: mean primary; soft bonus for hit & n."""
    mean = float(row["mean"]) if pd.notna(row["mean"]) else -999.0
    hit = float(row["hit_rate"]) if pd.notna(row["hit_rate"]) else 0.0
    n = int(row["n"]) if pd.notna(row["n"]) else 0
    return mean + 1.5 * hit + 0.05 * min(n, 30)


# ---- main ---------------------------------------------------------------


def main() -> int:
    global D0, OOS, SID, EXTRA_SEATS
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sid", default="2327", help="stock id (default 2327)")
    ap.add_argument("--d0", default=D0, help="window start (IS begins here)")
    ap.add_argument("--d1", default=D1, help="window end date")
    ap.add_argument("--oos", default=OOS, help="OOS cut (dates >= cut are OOS)")
    args = ap.parse_args()
    SID = str(args.sid)
    if SID != "2327":
        EXTRA_SEATS = ()
    D0 = str(args.d0)
    d1 = str(args.d1)
    OOS = str(args.oos)
    years = (pd.Timestamp(d1) - pd.Timestamp(D0)).days / 365.25

    t0 = time.time()
    OUT.mkdir(parents=True, exist_ok=True)
    log(f"{SID} standalone chip×branch · db={DEFAULT_DB_PATH} · D0={D0} OOS={OOS}")

    doc = json.loads((TRADES_DIR / f"{SID}.json").read_text(encoding="utf-8"))
    core = {str(k): str(v) for k, v in (doc.get("core") or {}).items()}
    if not core:
        raise SystemExit(f"missing core seats in trades/{SID}.json")
    stock_name = str(doc.get("stock_name") or SID)
    champ_set = set(core.keys())
    seats: list[str] = list(dict.fromkeys([*core.keys(), *EXTRA_SEATS]))
    name_map: dict[str, str] = dict(core)

    conn = connect_ro(DEFAULT_DB_PATH)
    whale = load_whale_events([SID])
    whale = whale[(whale["signal_date"] >= D0) & (whale["signal_date"] <= d1)].copy()
    greens = sorted(whale["signal_date"].astype(str).tolist())
    log(f"Whale greens (external benchmark) n={len(greens)}")

    cal = [d for d in load_calendar(conn, D0, d1) if D0 <= d <= d1]
    cal_ext = load_calendar(conn, D0, "2026-08-31")
    di = {d: i for i, d in enumerate(cal_ext)}
    # use cal_ext indices but only fire on window days in `cal`
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
    has_bh = (
        feat["big_holder_pct_chg"].notna().any()
        if "big_holder_pct_chg" in feat.columns
        else False
    )
    log(f"feat rows={len(feat)} big_holder={has_bh}")

    log("load full branch tape")
    tape = fetch_tape(conn, cal)
    conn.close()
    tape = attach_amt(tape, px_close)
    tape["is_foreign"] = tape["tname"].map(is_foreign)
    tape_dom = tape[~tape["is_foreign"]].copy()
    for r in tape_dom.itertuples():
        if r.tid not in name_map and r.tname:
            name_map[str(r.tid)] = str(r.tname)
    log(f"tape domestic rows={len(tape_dom)} days={tape_dom['d'].nunique()}")

    branch_amt: dict[str, dict[str, float]] = {}
    for r in tape_dom.itertuples():
        branch_amt.setdefault(str(r.d), {})[str(r.tid)] = (
            float(r.amt) if pd.notna(r.amt) else 0.0
        )

    # decision-day → (D, D−1)
    day_lags: dict[str, tuple[str, str | None]] = {}
    for d in cal:
        day_lags[d] = (d, lag_date(cal_ext, di, d, 1))

    # ---- Whale benchmark trades (no non-overlap skip; ledger events) ----
    whale_trades: list[dict] = []
    whale_xs_by_hold: dict[int, dict[str, list[float]]] = {
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
            whale_xs_by_hold[hold]["full"].append(x)
            whale_xs_by_hold[hold][sp].append(x)

    # ---- precompute chip fire days ----
    log("precompute chip fire days")
    chip_fires: dict[str, set[str]] = {}
    chip_labels: dict[str, str] = {}
    for atoms in CHIP_RULES:
        rid = chip_id(atoms)
        chip_labels[rid] = chip_label(atoms)
        fires: set[str] = set()
        for d in cal:
            if rule_mask_on_day(feat_idx, d, atoms):
                fires.add(d)
        chip_fires[rid] = fires
        log(f"  chip {rid}: calendar fires={len(fires)}")

    # ---- precompute branch fire days ----
    packs: list[tuple[str, str, set[str] | tuple[str, str]]] = []
    packs.append(("champ", "any champion", champ_set))
    for tid in seats:
        packs.append((f"seat_{tid}", f"{tid}:{name_map.get(tid, tid)}", {tid}))
    for a, b in combinations(seats, 2):
        packs.append((f"pair_{a}_{b}", f"{a}+{b}", (a, b)))

    log(f"branch packs={len(packs)} × windows={len(WINDOWS)} × floors={len(FLOORS)}")
    branch_fires: dict[str, set[str]] = {}
    branch_meta: dict[str, dict] = {}
    for pid, plab, pobj in packs:
        for wid, floor, flab in FLOORS:
            for window in WINDOWS:
                rid = f"{pid}__{window}__{wid}"
                fires: set[str] = set()
                for d in cal:
                    _, dm1 = day_lags[d]
                    if isinstance(pobj, tuple):
                        ok = two_seat_window_ok(
                            branch_amt, d, dm1, pobj[0], pobj[1], floor, window
                        )
                    else:
                        ok = window_ok(branch_amt, d, dm1, pobj, floor, window)
                    if ok:
                        fires.add(d)
                branch_fires[rid] = fires
                branch_meta[rid] = {
                    "label": f"{plab} · {win_label(window)} · {flab}",
                    "pack_id": pid,
                    "window": window,
                    "floor_id": wid,
                    "n_calendar": len(fires),
                }

    # ---- build rule specs ----
    # family: chip_only | branch_only | combo
    specs: list[dict] = []
    for cid, fires in chip_fires.items():
        specs.append(
            {
                "rule": f"chip__{cid}",
                "label": f"chip:{chip_labels[cid]}",
                "family": "chip_only",
                "fires": fires,
                "chip_id": cid,
                "branch_id": "",
            }
        )
    for brid, fires in branch_fires.items():
        meta = branch_meta[brid]
        specs.append(
            {
                "rule": f"br__{brid}",
                "label": f"branch:{meta['label']}",
                "family": "branch_only",
                "fires": fires,
                "chip_id": "",
                "branch_id": brid,
            }
        )
    # combos: key chips × all branch (keep grid focused)
    combo_chips = ("ts1+bhchg", "ts2", "ts1", "ts2+bhchg", "ssum5+bhchg")
    for cid in combo_chips:
        if cid not in chip_fires:
            continue
        cf = chip_fires[cid]
        for brid, bf in branch_fires.items():
            fires = cf & bf
            meta = branch_meta[brid]
            specs.append(
                {
                    "rule": f"combo__{cid}__{brid}",
                    "label": f"({chip_labels[cid]}) ∧ ({meta['label']})",
                    "family": "combo",
                    "fires": fires,
                    "chip_id": cid,
                    "branch_id": brid,
                }
            )
    log(f"rule specs={len(specs)}")

    # ---- evaluate ----
    summary_rows: list[dict] = []
    trade_rows: list[dict] = []
    # Always keep trades for whale + baselines + freeze candidates later
    keep_trade_rules: set[str] = {"Whale_T+1"}

    for hold in HOLDS:
        for split in ("full", "IS", "OOS"):
            summary_rows.append(
                metrics_row(
                    [t for t in whale_trades if t["hold"] == hold],
                    "Whale_T+1",
                    "expert-pool 綠燈 → T+1 開盤",
                    "benchmark_whale",
                    hold,
                    split,
                    whale_xs_by_hold[hold][split],
                    years,
                )
            )

    # baselines: chip ts1+bhchg, champ≥0.5 on D∨D−1
    baseline_specs = [
        s
        for s in specs
        if s["rule"]
        in (
            "chip__ts1+bhchg",
            "br__champ__either__0p5",
            "br__champ__d__0p5",
        )
    ]
    for sp in baseline_specs:
        keep_trade_rules.add(sp["rule"])

    log("evaluate all rules (H=7 IS first for freeze, then full grid)")
    # Evaluate PRIMARY_HOLD for all rules; H=10 only for top/baselines later
    is_candidates: list[dict] = []
    for i, sp in enumerate(specs):
        if i and i % 400 == 0:
            log(f"  evaluated {i}/{len(specs)}")
        trades = standalone_trades(
            sp["fires"],
            PRIMARY_HOLD,
            cal_ext,
            di,
            ohlc_px,
            bench,
            sp["family"],
            sp["rule"],
            D0,
            d1,
        )
        for split in ("full", "IS", "OOS"):
            row = metrics_row(
                trades,
                sp["rule"],
                sp["label"],
                sp["family"],
                PRIMARY_HOLD,
                split,
                whale_xs_by_hold[PRIMARY_HOLD][split],
                years,
                extra={
                    "chip_id": sp["chip_id"],
                    "branch_id": sp["branch_id"],
                    "n_calendar_fires": len(sp["fires"]),
                },
            )
            summary_rows.append(row)
            if split == "IS":
                is_candidates.append(row)
        if sp["rule"] in keep_trade_rules:
            trade_rows.extend(trades)

    # freeze on IS
    is_df = pd.DataFrame(is_candidates)
    pool = is_df[is_df["n"] >= MIN_IS_N].copy()
    used_min_n = MIN_IS_N
    if pool.empty:
        pool = is_df[is_df["n"] >= MIN_IS_N_FALLBACK].copy()
        used_min_n = MIN_IS_N_FALLBACK
        log(f"WARN: no IS n≥{MIN_IS_N}; fallback n≥{MIN_IS_N_FALLBACK}")
    if pool.empty:
        pool = is_df[is_df["n"] > 0].copy()
        used_min_n = 1
        log("WARN: freezing on any IS n>0")
    pool = pool.copy()
    pool["_score"] = pool.apply(is_score, axis=1)
    # Prefer combo slightly when scores close: already in score via mean
    # Soft prefer combo/branch over chip_only by tiny family bonus
    fam_bonus = {"combo": 0.3, "branch_only": 0.15, "chip_only": 0.0}
    pool["_score"] = pool["_score"] + pool["family"].map(fam_bonus).fillna(0)
    pool = pool.sort_values(
        by=["_score", "mean", "hit_rate", "n"],
        ascending=[False, False, False, False],
    )
    best_is = pool.iloc[0]
    best_rule = str(best_is["rule"])
    log(
        f"FROZEN IS: {best_rule} n={int(best_is['n'])} mean={best_is['mean']:+.2f} "
        f"hit={best_is['hit_rate']:.0%} score={best_is['_score']:.2f} (min_n={used_min_n})"
    )

    # Top-15 IS for report + H=10 eval
    top_is = pool.head(15)
    top_rules = set(top_is["rule"].tolist()) | keep_trade_rules | {best_rule}

    # Re-eval H=10 + collect trades for top rules
    spec_by_rule = {s["rule"]: s for s in specs}
    for rid in sorted(top_rules):
        if rid == "Whale_T+1":
            continue
        sp = spec_by_rule.get(rid)
        if sp is None:
            continue
        for hold in HOLDS:
            trades = standalone_trades(
                sp["fires"],
                hold,
                cal_ext,
                di,
                ohlc_px,
                bench,
                sp["family"],
                sp["rule"],
                D0,
                d1,
            )
            trade_rows.extend(trades)
            if hold == PRIMARY_HOLD:
                continue  # already in summary
            for split in ("full", "IS", "OOS"):
                summary_rows.append(
                    metrics_row(
                        trades,
                        sp["rule"],
                        sp["label"],
                        sp["family"],
                        hold,
                        split,
                        whale_xs_by_hold[hold][split],
                        years,
                        extra={
                            "chip_id": sp["chip_id"],
                            "branch_id": sp["branch_id"],
                            "n_calendar_fires": len(sp["fires"]),
                        },
                    )
                )

    # n-ladder (calendar + non-overlap H7)
    def n_overlap(fires: set[str]) -> int:
        return len(
            standalone_trades(
                fires, PRIMARY_HOLD, cal_ext, di, ohlc_px, bench, "x", "x", D0, d1
            )
        )

    ladder = [
        ("calendar trading days", len(cal), None),
        (
            "chip ts1+bhchg calendar",
            len(chip_fires.get("ts1+bhchg", set())),
            n_overlap(chip_fires.get("ts1+bhchg", set())),
        ),
        (
            "chip ts2 (three_streak≥2) calendar",
            len(chip_fires.get("ts2", set())),
            n_overlap(chip_fires.get("ts2", set())),
        ),
        (
            "champ D∨D−1 ≥0.5億 calendar",
            len(branch_fires.get("champ__either__0p5", set())),
            n_overlap(branch_fires.get("champ__either__0p5", set())),
        ),
        (
            "champ D ≥0.5億 calendar",
            len(branch_fires.get("champ__d__0p5", set())),
            n_overlap(branch_fires.get("champ__d__0p5", set())),
        ),
        (
            "chip∧champ≥0.5 D (ts1+bhchg∧champ@D)",
            len(
                chip_fires.get("ts1+bhchg", set())
                & branch_fires.get("champ__d__0p5", set())
            ),
            n_overlap(
                chip_fires.get("ts1+bhchg", set())
                & branch_fires.get("champ__d__0p5", set())
            ),
        ),
        (
            "pair_9100_9665 D∨D−1 net",
            len(branch_fires.get("pair_9100_9665__either__net", set())),
            n_overlap(branch_fires.get("pair_9100_9665__either__net", set())),
        ),
        ("Whale greens (external)", len(greens), len(greens)),
    ]
    # frozen rule ladder entry
    best_sp = spec_by_rule[best_rule]
    ladder.append(
        (
            f"FROZEN {best_rule}",
            len(best_sp["fires"]),
            n_overlap(best_sp["fires"]),
        )
    )

    # ---- write artifacts ----
    cs_df = pd.DataFrame(summary_rows).drop_duplicates(
        subset=["rule", "hold", "split"], keep="last"
    )
    # prefer denser rows (with chip_id) when dup
    tr_df = pd.DataFrame(trade_rows).drop_duplicates(
        subset=["rule", "hold", "signal_date", "entry_date"], keep="last"
    )
    grid_path = OUT / f"{SID}_standalone_sweep_grid.csv"
    trades_path = OUT / f"{SID}_standalone_trades.csv"
    top_path = OUT / f"{SID}_standalone_top_is.csv"
    frozen_path = OUT / f"{SID}_standalone_frozen.json"
    report_path = OUT / f"{SID}_STANDALONE_STRATEGY.md"
    status_path = OUT / f"status_{SID}_standalone.json"

    cs_df.to_csv(grid_path, index=False)
    tr_df.to_csv(trades_path, index=False)
    top_is.drop(columns=["_score"], errors="ignore").to_csv(top_path, index=False)

    def pick(rule: str, hold: int, split: str) -> dict | None:
        sub = cs_df[
            (cs_df["rule"] == rule) & (cs_df["hold"] == hold) & (cs_df["split"] == split)
        ]
        if sub.empty:
            return None
        return sub.iloc[0].to_dict()

    best_full = pick(best_rule, PRIMARY_HOLD, "full")
    best_oos = pick(best_rule, PRIMARY_HOLD, "OOS")
    best_h10 = pick(best_rule, 10, "full")
    whale_full = pick("Whale_T+1", PRIMARY_HOLD, "full")
    whale_oos = pick("Whale_T+1", PRIMARY_HOLD, "OOS")
    base_chip = pick("chip__ts1+bhchg", PRIMARY_HOLD, "full")
    base_champ = pick("br__champ__either__0p5", PRIMARY_HOLD, "full")

    frozen = {
        "sid": SID,
        "name": stock_name,
        "protocol": {
            "decide": "D close: chip@D; branch on D and D−1 (T−1 / T−2 vs entry)",
            "enter": "D+1 open (L1)",
            "hold": PRIMARY_HOLD,
            "non_overlapping": True,
            "cost": COST,
            "beta": BETA,
            "window": f"{D0}..{d1}",
            "oos": OOS,
            "note": "Independent calendar — NOT filtered by Whale greens",
        },
        "freeze_rule": {
            "rule": best_rule,
            "label": str(best_is["label"]),
            "family": str(best_is["family"]),
            "chip_id": str(best_is.get("chip_id") or ""),
            "branch_id": str(best_is.get("branch_id") or ""),
            "is_n_min_used": used_min_n,
            "IS": {k: (None if isinstance(v, float) and np.isnan(v) else v)
                   for k, v in best_is.drop(labels=["_score"], errors="ignore").items()},
            "full": best_full,
            "OOS": best_oos,
            "H10_full": best_h10,
        },
        "benchmarks": {
            "Whale_T+1_full": whale_full,
            "Whale_T+1_OOS": whale_oos,
            "chip_ts1_bhchg_full": base_chip,
            "champ_either_0p5_full": base_champ,
        },
        "n_ladder": [
            {"definition": a, "n_calendar": b, "n_nonoverlap_H7": c}
            for a, b, c in ladder
        ],
        "seats": {s: name_map.get(s, s) for s in seats},
        "core_champions": core,
        "grid_n_specs": len(specs),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }

    def _json_safe(o):
        if isinstance(o, dict):
            return {k: _json_safe(v) for k, v in o.items()}
        if isinstance(o, list):
            return [_json_safe(v) for v in o]
        if isinstance(o, (np.floating, float)):
            if np.isnan(o):
                return None
            return float(o)
        if isinstance(o, (np.integer,)):
            return int(o)
        if isinstance(o, (np.bool_,)):
            return bool(o)
        return o

    frozen_path.write_text(
        json.dumps(_json_safe(frozen), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # ---- report ----
    lines: list[str] = []
    lines.append(f"# {SID} {stock_name} · 獨立籌碼×分點策略（不綁 Whale）")
    lines.append("")
    lines.append(f"- 生成：{datetime.now().isoformat(timespec='seconds')}")
    lines.append(
        f"- Runner：`scripts/research/run_2327_standalone_chip_branch.py`"
    )
    lines.append(
        f"- 窗：{D0}..{d1}（≈{years:.2f}y）· OOS≥{OOS} · COST={COST} · β={BETA}×IX0001"
    )
    lines.append(
        "- 協議：**D 收盤**看籌碼@D + 分點@D/D−1 → **D+1 開盤**進 · 持 H · 同股非重疊"
    )
    lines.append(
        "- Whale_T+1 僅作**外部對照臂**（綠燈→T+1 開），**非**事件濾網／配對子集"
    )
    lines.append("- Research only · **未採納**進 Strategy／Order")
    lines.append("")
    lines.append("## 結論（先讀）")
    lines.append("")
    v_full = (best_full or {}).get("verdict", "差不多")
    lines.append(
        f"**總評相對 Whale_T+1 跟單（mean excess · 不同母體）：{v_full}**"
    )
    lines.append("")
    lines.append(
        f"- **凍結規則**：`{best_rule}`  \n"
        f"  {best_is['label']}"
    )
    if best_full:
        lines.append(
            f"- 凍結 H=7 full：n={int(best_full['n'])} · hit={fmt_rate(best_full['hit_rate'])} · "
            f"med={fmt_pct(best_full['med'])} · mean={fmt_pct(best_full['mean'])} · "
            f"sum={fmt_pct(best_full['sum'], 0)} · "
            f"≈{best_full.get('trades_per_year', float('nan')):.1f} 筆/年"
        )
        lines.append(
            f"- vs Whale Δmean={fmt_pct(best_full['delta_vs_whale'])} pp · "
            f"CI [{fmt_pct(best_full['ci_lo'])}, {fmt_pct(best_full['ci_hi'])}] · "
            f"P(Δ≤0)={fmt_p(best_full['p_le0'])}"
        )
    if whale_full:
        lines.append(
            f"- Whale_T+1 H=7 full：n={int(whale_full['n'])} · hit={fmt_rate(whale_full['hit_rate'])} · "
            f"mean={fmt_pct(whale_full['mean'])} · sum={fmt_pct(whale_full['sum'], 0)}"
        )
    lines.append(
        "- 誠實：筆數多≠更好；以 **mean excess** 為主，sum 僅輔助。"
        " 兩臂母體不同，bootstrap 為非配對。"
    )
    lines.append("")
    lines.append("## 協議（PIT）")
    lines.append("")
    lines.append("| 項目 | 定義 |")
    lines.append("|------|------|")
    lines.append("| 決策日 D | 收盤後判定 |")
    lines.append("| 籌碼 | as-of **D** |")
    lines.append("| 分點 | **D** 與 **D−1**（相對進場日 E=D+1 即 T−1 / T−2） |")
    lines.append("| 進場 | **D+1 開盤** |")
    lines.append("| 持有 | H=7（主）· H=10（敏感度） |")
    lines.append("| 重疊 | 持倉中跳過新訊號 |")
    lines.append("| 凍結 | **IS**（D < 2026-01-01）mean / hit / n≥8 優先 |")
    lines.append("")
    lines.append("## 樣本階梯（獨立日曆）")
    lines.append("")
    lines.append("| definition | n_calendar | n_nonoverlap_H7 |")
    lines.append("|------------|----------:|----------------:|")
    for a, b, c in ladder:
        c_s = "—" if c is None else str(c)
        lines.append(f"| {a} | {b} | {c_s} |")
    lines.append("")
    lines.append("## Whale_T+1 外部基準")
    lines.append("")
    lines.append("| hold | split | n | hit | med% | mean% | sum% |")
    lines.append("|-----:|-------|--:|----:|-----:|------:|-----:|")
    for hold in HOLDS:
        for split in ("full", "IS", "OOS"):
            r = pick("Whale_T+1", hold, split)
            if not r:
                continue
            lines.append(
                f"| {hold} | {split} | {int(r['n'])} | {fmt_rate(r['hit_rate'])} | "
                f"{fmt_pct(r['med'])} | {fmt_pct(r['mean'])} | {fmt_pct(r['sum'], 0)} |"
            )
    lines.append("")
    lines.append("## IS Top（凍結依據 · H=7）")
    lines.append("")
    lines.append(
        "| rank | rule | family | n | hit | mean% | med% | sum% | ΔvsWhale |"
    )
    lines.append(
        "|-----:|------|--------|--:|----:|------:|-----:|-----:|---------:|"
    )
    for i, (_, r) in enumerate(top_is.iterrows(), 1):
        mark = " ←凍結" if r["rule"] == best_rule else ""
        lines.append(
            f"| {i} | `{r['rule']}`{mark} | {r['family']} | {int(r['n'])} | "
            f"{fmt_rate(r['hit_rate'])} | {fmt_pct(r['mean'])} | {fmt_pct(r['med'])} | "
            f"{fmt_pct(r['sum'], 0)} | {fmt_pct(r['delta_vs_whale'])} |"
        )
    lines.append("")
    lines.append("## 凍結規則 · full / OOS / H=10")
    lines.append("")
    lines.append("| hold | split | n | hit | med% | mean% | sum% | Δmean | 95%CI | P(Δ≤0) | 判定 |")
    lines.append("|-----:|-------|--:|----:|-----:|------:|-----:|------:|------:|-------:|------|")
    for hold in HOLDS:
        for split in ("full", "IS", "OOS"):
            r = pick(best_rule, hold, split)
            if not r:
                continue
            lines.append(
                f"| {hold} | {split} | {int(r['n'])} | {fmt_rate(r['hit_rate'])} | "
                f"{fmt_pct(r['med'])} | {fmt_pct(r['mean'])} | {fmt_pct(r['sum'], 0)} | "
                f"{fmt_pct(r['delta_vs_whale'])} | "
                f"[{fmt_pct(r['ci_lo'])}, {fmt_pct(r['ci_hi'])}] | "
                f"{fmt_p(r['p_le0'])} | {r['verdict']} |"
            )
    lines.append("")
    lines.append("## 對照臂（同 H/cost/β · 不同母體）")
    lines.append("")
    lines.append("| arm | split | n | hit | mean% | sum% | ΔvsWhale | 判定 |")
    lines.append("|-----|-------|--:|----:|------:|-----:|---------:|------|")
    for rid, lab in (
        ("Whale_T+1", "Whale_T+1"),
        (best_rule, "Standalone best"),
        ("chip__ts1+bhchg", "chip ts1+bhchg"),
        ("br__champ__either__0p5", "champ D∨D−1 ≥0.5億"),
    ):
        for split in ("full", "OOS"):
            r = pick(rid, PRIMARY_HOLD, split)
            if not r:
                continue
            lines.append(
                f"| {lab} | {split} | {int(r['n'])} | {fmt_rate(r['hit_rate'])} | "
                f"{fmt_pct(r['mean'])} | {fmt_pct(r['sum'], 0)} | "
                f"{fmt_pct(r['delta_vs_whale'])} | {r['verdict']} |"
            )
    lines.append("")
    lines.append("## 方法備註")
    lines.append("")
    lines.append(
        "- 掃參格子：籌碼規則 "
        f"{len(CHIP_RULES)} · 分點 packs×窗×floor={len(branch_fires)} · "
        f"combo chips={len(combo_chips)} · 合計 specs≈{len(specs)}"
    )
    lines.append(
        "- 凍結：IS 優先 n≥8，分數 = mean + 1.5×hit + 0.05×min(n,30) + 家族微加成"
    )
    lines.append(
        "- vs Whale：非配對 bootstrap（mean excess）；n 不同屬預期"
    )
    lines.append(
        f"- 產物：`{grid_path.name}` · `{trades_path.name}` · "
        f"`{top_path.name}` · `{frozen_path.name}`"
    )
    lines.append("")

    report_path.write_text("\n".join(lines), encoding="utf-8")
    status_path.write_text(
        json.dumps(
            {
                "ok": True,
                "frozen_rule": best_rule,
                "verdict_vs_whale": v_full,
                "elapsed_s": round(time.time() - t0, 1),
                "report": str(report_path.relative_to(ROOT)),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    log(f"wrote {report_path}")
    log(f"done in {time.time() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
