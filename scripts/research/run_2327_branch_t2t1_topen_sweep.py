#!/usr/bin/env python3
"""2327 國巨 · 分點 T−2/T−1 → 綠燈 T 開盤進場 vs Whale_T+1.

Research only · 未採納 · Book only.

Protocol (user revised):
  - Green day T = expert-pool consensus (same whale events as chip×branch study)
  - Features: domestic branch tape on T−2 and T−1 only (PIT; decision before T open)
  - Candidate: rule fires → enter **T open**; hold H; COST=0.003; β=1.15×IX0001
  - Baseline Whale_T+1: green T → enter **T+1 open** (L1; matches prior
    ``run_2327_chip_branch_t2_t1_sweep.py`` / L0_VS_L1 convention — not close)
  - Secondary baseline: always enter T open on every green

    PYTHONPATH=src .venv/bin/python scripts/research/run_2327_branch_t2t1_topen_sweep.py
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
PRIOR_FROZEN = OUT / "2327_frozen_rule.json"
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
# Prior report high-lift seats (≥0.5億 · event_hit≥2) + champions
EXTRA_SEATS = ("9100", "9665", "9600", "9869", "989P")
FROZEN_CHIP = ("ts1", "bhchg")  # as-of T−2 only
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
WINDOWS = ("t2", "t1", "both", "either")
FLOORS = (
    ("net", 0.0, "net>0"),
    ("0p5", FLOOR_0P5, "≥0.5億"),
    ("1yi", FLOOR_1, "≥1億"),
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
    """Whale_T+1: enter next open after signal_d; exit cal[i+hold] close."""
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


def excess_t_open(
    T: str,
    hold: int,
    cal: list[str],
    di: dict[str, int],
    ohlc_px: dict[tuple[str, str], tuple[float, float]],
    bench: dict[str, float],
) -> tuple[float | None, str | None, str | None]:
    """Enter T open; exit cal[i+hold] close (same exit calendar as Whale_T+1)."""
    i = di.get(T)
    if i is None or i + hold >= len(cal):
        return None, None, None
    xd = cal[i + hold]
    er = ohlc_px.get((SID, T))
    xr = ohlc_px.get((SID, xd))
    be, bx = bench.get(T), bench.get(xd)
    if not er or not xr or not be or not bx:
        return None, None, None
    eo, _ = er
    _, xp = xr
    if eo <= 0 or xp <= 0 or be <= 0 or bx <= 0:
        return None, None, None
    sr = xp / eo - 1.0 - COST
    br = bx / be - 1.0
    return (sr - BETA * br) * 100.0, T, xd


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


# ---- branch / chip ------------------------------------------------------


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
    d2: str | None,
    d1: str | None,
    tids: set[str],
    floor: float,
    window: str,
) -> bool:
    """Seat-set fires on required lag day(s). OR within seats on a day; AND for both."""
    if window == "t2":
        return bool(d2) and any_seat_ok(branch_amt, d2, tids, floor)
    if window == "t1":
        return bool(d1) and any_seat_ok(branch_amt, d1, tids, floor)
    if window == "both":
        return (
            bool(d2)
            and bool(d1)
            and any_seat_ok(branch_amt, d2, tids, floor)
            and any_seat_ok(branch_amt, d1, tids, floor)
        )
    if window == "either":
        return (bool(d2) and any_seat_ok(branch_amt, d2, tids, floor)) or (
            bool(d1) and any_seat_ok(branch_amt, d1, tids, floor)
        )
    return False


def two_seat_window_ok(
    branch_amt: dict[str, dict[str, float]],
    d2: str | None,
    d1: str | None,
    t_a: str,
    t_b: str,
    floor: float,
    window: str,
) -> bool:
    """Both seats must fire on the required day(s) (AND of seats)."""
    if window == "t2":
        return (
            bool(d2)
            and seat_ok(branch_amt, d2, t_a, floor)
            and seat_ok(branch_amt, d2, t_b, floor)
        )
    if window == "t1":
        return (
            bool(d1)
            and seat_ok(branch_amt, d1, t_a, floor)
            and seat_ok(branch_amt, d1, t_b, floor)
        )
    if window == "both":
        return (
            bool(d2)
            and bool(d1)
            and seat_ok(branch_amt, d2, t_a, floor)
            and seat_ok(branch_amt, d2, t_b, floor)
            and seat_ok(branch_amt, d1, t_a, floor)
            and seat_ok(branch_amt, d1, t_b, floor)
        )
    if window == "either":
        # either day has both seats firing
        ok2 = (
            bool(d2)
            and seat_ok(branch_amt, d2, t_a, floor)
            and seat_ok(branch_amt, d2, t_b, floor)
        )
        ok1 = (
            bool(d1)
            and seat_ok(branch_amt, d1, t_a, floor)
            and seat_ok(branch_amt, d1, t_b, floor)
        )
        return ok2 or ok1
    return False


def chip_ts1_bhchg(feat_idx: pd.DataFrame, d: str | None) -> bool:
    if d is None:
        return False
    try:
        row = feat_idx.loc[(SID, d)]
    except KeyError:
        return False
    if isinstance(row, pd.DataFrame):
        row = row.iloc[0]
    ts = float(row.get("three_streak", 0) or 0)
    bh = row.get("big_holder_pct_chg", np.nan)
    return ts >= 1 and bool(pd.notna(bh) and float(bh) > 0)


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


def win_label(w: str) -> str:
    return {
        "t2": "T−2",
        "t1": "T−1",
        "both": "T−2∧T−1",
        "either": "T−2∨T−1",
    }.get(w, w)


# ---- main ---------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--d1", default=D1, help="window end date")
    args = ap.parse_args()
    d1 = str(args.d1)

    t0 = time.time()
    OUT.mkdir(parents=True, exist_ok=True)
    log(f"2327 branch T−2/T−1 → T-open sweep · db={DEFAULT_DB_PATH}")

    doc = json.loads((TRADES_DIR / f"{SID}.json").read_text(encoding="utf-8"))
    core = {str(k): str(v) for k, v in (doc.get("core") or {}).items()}
    champ_set = set(core.keys())

    # seats = champions ∪ prior high-lift
    seats: list[str] = list(dict.fromkeys([*core.keys(), *EXTRA_SEATS]))
    name_map: dict[str, str] = dict(core)
    if PRIOR_FROZEN.exists():
        prior = json.loads(PRIOR_FROZEN.read_text(encoding="utf-8"))
        for row in prior.get("top_branch_seats_t2") or []:
            tid = str(row.get("tid", ""))
            if tid and tid not in seats:
                seats.append(tid)
            if tid and row.get("name"):
                name_map[tid] = str(row["name"])
    log(f"seats n={len(seats)}: {[(s, name_map.get(s, '')) for s in seats]}")

    conn = connect_ro(DEFAULT_DB_PATH)
    whale = load_whale_events([SID])
    whale = whale[(whale["signal_date"] >= D0) & (whale["signal_date"] <= d1)].copy()
    greens = sorted(whale["signal_date"].astype(str).tolist())
    n_is_g = sum(1 for g in greens if g < OOS)
    n_oos_g = sum(1 for g in greens if g >= OOS)
    log(f"green events n={len(greens)} IS={n_is_g} OOS={n_oos_g}")
    if n_is_g <= 1:
        log("WARN: IS greens ≈1 — do not freeze on IS event metrics")

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
    log("build chip features (optional assist ts1+bhchg @ T−2)")
    feat = build_chip_feature_frame(
        conn, [SID], D0, d1, holding_shares_path=hs, gov_bank_path=gov
    )
    feat_idx = feat.set_index(["sid", "d"])

    # PIT decision days only (T−2 / T−1 around greens) — no full calendar tape
    need_dates: set[str] = set()
    for T in greens:
        for k in (1, 2):
            ld = lag_date(cal_ext, di, T, k)
            if ld:
                need_dates.add(ld)
    log("load branch tape")
    tape = fetch_tape(conn, sorted(need_dates))
    conn.close()
    tape = attach_amt(tape, px_close)
    tape["is_foreign"] = tape["tname"].map(is_foreign)
    tape_dom = tape[~tape["is_foreign"]].copy()
    for r in tape_dom.itertuples():
        if r.tid not in name_map and r.tname:
            name_map[str(r.tid)] = str(r.tname)

    branch_amt: dict[str, dict[str, float]] = {}
    for r in tape_dom.itertuples():
        branch_amt.setdefault(str(r.d), {})[str(r.tid)] = (
            float(r.amt) if pd.notna(r.amt) else 0.0
        )

    green_lags: dict[str, tuple[str | None, str | None]] = {}
    for T in greens:
        green_lags[T] = (
            lag_date(cal_ext, di, T, 2),
            lag_date(cal_ext, di, T, 1),
        )

    # ---- baselines ----
    whale_by: dict[tuple[int, str], dict[str, float]] = {}  # (hold, T) -> excess
    topen_by: dict[tuple[int, str], dict[str, float]] = {}
    baseline_rows: list[dict] = []
    for hold in HOLDS:
        for T in greens:
            wx, wed, wxd = excess_l1(T, hold, cal_ext, di, ohlc_px, bench)
            tx, ted, txd = excess_t_open(T, hold, cal_ext, di, ohlc_px, bench)
            if wx is not None:
                whale_by[(hold, T)] = {"x": wx, "ed": wed, "xd": wxd}
            if tx is not None:
                topen_by[(hold, T)] = {"x": tx, "ed": ted, "xd": txd}

    for hold in HOLDS:
        for split in ("full", "IS", "OOS"):
            wh = [
                whale_by[(hold, T)]["x"]
                for T in greens
                if (hold, T) in whale_by
                and (
                    split == "full"
                    or (split == "IS" and T < OOS)
                    or (split == "OOS" and T >= OOS)
                )
            ]
            to = [
                topen_by[(hold, T)]["x"]
                for T in greens
                if (hold, T) in topen_by
                and (
                    split == "full"
                    or (split == "IS" and T < OOS)
                    or (split == "OOS" and T >= OOS)
                )
            ]
            # paired always_T_open vs Whale on overlapping greens
            paired_t, paired_w = [], []
            for T in greens:
                if split == "IS" and T >= OOS:
                    continue
                if split == "OOS" and T < OOS:
                    continue
                if (hold, T) in topen_by and (hold, T) in whale_by:
                    paired_t.append(topen_by[(hold, T)]["x"])
                    paired_w.append(whale_by[(hold, T)]["x"])
            boot_p = bootstrap_delta(paired_t, paired_w, paired=True)
            sm_w = summarize(wh, "Whale_T+1")
            sm_t = summarize(to, "Always_T_open")
            baseline_rows.append(
                {
                    "rule": "Whale_T+1",
                    "label": "綠燈 T → T+1 開盤 (L1)",
                    "lens": "baseline",
                    "hold": hold,
                    "split": split,
                    "n": sm_w["n"],
                    "coverage": 1.0,
                    "hit_rate": sm_w["hit_rate"],
                    "med": sm_w["med_excess"],
                    "mean": sm_w["mean_excess"],
                    "sum": sm_w["sum_excess"],
                    "delta_vs_whale": 0.0,
                    "ci_lo": 0.0,
                    "ci_hi": 0.0,
                    "p_le0": np.nan,
                    "verdict": "基準",
                    "paired_whale_mean": sm_w["mean_excess"],
                }
            )
            baseline_rows.append(
                {
                    "rule": "Always_T_open",
                    "label": "每筆綠燈皆 T 開盤進",
                    "lens": "baseline",
                    "hold": hold,
                    "split": split,
                    "n": sm_t["n"],
                    "coverage": 1.0,
                    "hit_rate": sm_t["hit_rate"],
                    "med": sm_t["med_excess"],
                    "mean": sm_t["mean_excess"],
                    "sum": sm_t["sum_excess"],
                    "delta_vs_whale": boot_p["delta_mean"],
                    "ci_lo": boot_p["ci_lo"],
                    "ci_hi": boot_p["ci_hi"],
                    "p_le0": boot_p["p_delta_le0"],
                    "verdict": verdict_word(boot_p),
                    "paired_whale_mean": float(np.mean(paired_w)) if paired_w else np.nan,
                }
            )
    log(
        f"Whale H7 full mean={baseline_rows[0]['mean']:+.2f} "
        f"Always_T_open Δ={baseline_rows[1]['delta_vs_whale']:+.2f}"
    )

    # ---- build rule specs ----
    # seat packs: single seats, any-champion, 2-seat AND combos among seats
    packs: list[tuple[str, str, set[str] | tuple[str, str]]] = []
    packs.append(("champ", "any champion", champ_set))
    for tid in seats:
        packs.append((f"seat_{tid}", f"{tid}:{name_map.get(tid, tid)}", {tid}))
    for a, b in combinations(seats, 2):
        packs.append(
            (
                f"pair_{a}_{b}",
                f"{a}+{b}",
                (a, b),
            )
        )

    rules: list[dict] = []
    for pid, plab, pobj in packs:
        for win in WINDOWS:
            for fid, floor, flab in FLOORS:
                for chip_and in (False, True):
                    rid = f"{pid}__{win}__{fid}"
                    lab = f"{plab} · {win_label(win)} · {flab}"
                    if chip_and:
                        rid += "__chip"
                        lab = f"({lab}) ∧ chip@T−2(ts1+bhchg)"
                    rules.append(
                        {
                            "rule": rid,
                            "label": lab,
                            "pack_id": pid,
                            "pack": pobj,
                            "window": win,
                            "floor": floor,
                            "floor_id": fid,
                            "chip_and": chip_and,
                            "is_pair": isinstance(pobj, tuple),
                        }
                    )
    log(f"rule grid n={len(rules)}")

    def rule_fires(T: str, spec: dict) -> bool:
        d2, d1_ = green_lags[T]
        pobj = spec["pack"]
        if spec["is_pair"]:
            t_a, t_b = pobj  # type: ignore[misc]
            ok = two_seat_window_ok(
                branch_amt, d2, d1_, t_a, t_b, spec["floor"], spec["window"]
            )
        else:
            ok = window_ok(
                branch_amt, d2, d1_, pobj, spec["floor"], spec["window"]  # type: ignore[arg-type]
            )
        if not ok:
            return False
        if spec["chip_and"]:
            return chip_ts1_bhchg(feat_idx, d2)
        return True

    # ---- evaluate ----
    summary_rows: list[dict] = list(baseline_rows)
    trade_rows: list[dict] = []

    for hold in HOLDS:
        for T in greens:
            if (hold, T) not in whale_by:
                continue
            w = whale_by[(hold, T)]
            trade_rows.append(
                {
                    "sid": SID,
                    "green_T": T,
                    "signal_date": T,
                    "entry_date": w["ed"],
                    "exit_date": w["xd"],
                    "arm": "Whale_T+1",
                    "rule": "Whale_T+1",
                    "hold": hold,
                    "excess_pct": w["x"],
                    "lens": "baseline",
                    "split": "IS" if T < OOS else "OOS",
                }
            )
            if (hold, T) in topen_by:
                t = topen_by[(hold, T)]
                trade_rows.append(
                    {
                        "sid": SID,
                        "green_T": T,
                        "signal_date": T,
                        "entry_date": t["ed"],
                        "exit_date": t["xd"],
                        "arm": "Always_T_open",
                        "rule": "Always_T_open",
                        "hold": hold,
                        "excess_pct": t["x"],
                        "whale_excess_pct": w["x"],
                        "lens": "baseline",
                        "split": "IS" if T < OOS else "OOS",
                    }
                )

    for spec in rules:
        rid, lab = spec["rule"], spec["label"]
        for hold in HOLDS:
            for split in ("full", "IS", "OOS"):
                cand_xs: list[float] = []
                wh_xs: list[float] = []
                hit_Ts: list[str] = []
                for T in greens:
                    if split == "IS" and T >= OOS:
                        continue
                    if split == "OOS" and T < OOS:
                        continue
                    if not rule_fires(T, spec):
                        continue
                    if (hold, T) not in topen_by or (hold, T) not in whale_by:
                        continue
                    cx = topen_by[(hold, T)]["x"]
                    wx = whale_by[(hold, T)]["x"]
                    cand_xs.append(cx)
                    wh_xs.append(wx)
                    hit_Ts.append(T)
                    if hold == PRIMARY_HOLD and split == "full":
                        trade_rows.append(
                            {
                                "sid": SID,
                                "green_T": T,
                                "signal_date": T,
                                "t2": green_lags[T][0],
                                "t1": green_lags[T][1],
                                "entry_date": topen_by[(hold, T)]["ed"],
                                "exit_date": topen_by[(hold, T)]["xd"],
                                "arm": "Branch_T2T1_Enter_T_open",
                                "rule": rid,
                                "hold": hold,
                                "excess_pct": cx,
                                "whale_excess_pct": wx,
                                "lens": "event_tied",
                                "split": "IS" if T < OOS else "OOS",
                            }
                        )

                n_g = sum(
                    1
                    for T in greens
                    if split == "full"
                    or (split == "IS" and T < OOS)
                    or (split == "OOS" and T >= OOS)
                )
                boot = bootstrap_delta(cand_xs, wh_xs, paired=True)
                sm = summarize(cand_xs)
                # unpaired full-arm vs all-green Whale (different n)
                all_wh = [
                    whale_by[(hold, T)]["x"]
                    for T in greens
                    if (hold, T) in whale_by
                    and (
                        split == "full"
                        or (split == "IS" and T < OOS)
                        or (split == "OOS" and T >= OOS)
                    )
                ]
                boot_full = bootstrap_delta(cand_xs, all_wh, paired=False)
                summary_rows.append(
                    {
                        "rule": rid,
                        "label": lab,
                        "pack_id": spec["pack_id"],
                        "window": spec["window"],
                        "floor_id": spec["floor_id"],
                        "chip_and": int(spec["chip_and"]),
                        "lens": "event_tied",
                        "hold": hold,
                        "split": split,
                        "n": sm["n"],
                        "coverage": sm["n"] / n_g if n_g else np.nan,
                        "hit_rate": sm["hit_rate"],
                        "med": sm["med_excess"],
                        "mean": sm["mean_excess"],
                        "sum": sm["sum_excess"],
                        "delta_vs_whale": boot["delta_mean"],
                        "ci_lo": boot["ci_lo"],
                        "ci_hi": boot["ci_hi"],
                        "p_le0": boot["p_delta_le0"],
                        "verdict": verdict_word(boot),
                        "paired_whale_mean": float(np.mean(wh_xs)) if wh_xs else np.nan,
                        "full_vs_whale_delta": boot_full["delta_mean"],
                        "full_vs_whale_p_le0": boot_full["p_delta_le0"],
                        "whale_n_all": len(all_wh),
                        "whale_mean_all": float(np.mean(all_wh)) if all_wh else np.nan,
                    }
                )

    cs_df = pd.DataFrame(summary_rows)
    trades_df = pd.DataFrame(trade_rows)

    # ---- pick best (event-tied H=7 full; primary=branch, chip secondary) ----
    et = cs_df[
        (cs_df.lens == "event_tied")
        & (cs_df.hold == PRIMARY_HOLD)
        & (cs_df.split == "full")
    ].copy()
    # Prefer branch-only; require n≥3, coverage≥15%; maximize paired Δ then sum
    pool = et[(et["chip_and"] == 0) & (et["n"] >= 3) & (et["coverage"].fillna(0) >= 0.15)]
    if pool.empty:
        pool = et[(et["chip_and"] == 0) & (et["n"] >= 2)]
    if pool.empty:
        pool = et[et["n"] >= 2]
    pool = pool.sort_values(
        ["delta_vs_whale", "sum", "coverage"], ascending=[False, False, False]
    )
    best_row = pool.iloc[0] if not pool.empty else None

    # best with chip assist among those that beat branch-only Δ (or top chip)
    chip_pool = et[
        (et["chip_and"] == 1) & (et["n"] >= 3) & (et["coverage"].fillna(0) >= 0.15)
    ]
    if chip_pool.empty:
        chip_pool = et[(et["chip_and"] == 1) & (et["n"] >= 2)]
    chip_pool = chip_pool.sort_values(
        ["delta_vs_whale", "sum"], ascending=[False, False]
    )
    best_chip_row = chip_pool.iloc[0] if not chip_pool.empty else None

    # OOS check for best
    best_oos = None
    if best_row is not None:
        oos_m = cs_df[
            (cs_df.rule == best_row["rule"])
            & (cs_df.hold == PRIMARY_HOLD)
            & (cs_df.split == "OOS")
            & (cs_df.lens == "event_tied")
        ]
        if not oos_m.empty:
            best_oos = oos_m.iloc[0]

    always = cs_df[
        (cs_df.rule == "Always_T_open")
        & (cs_df.hold == PRIMARY_HOLD)
        & (cs_df.split == "full")
    ].iloc[0]
    whale_full = cs_df[
        (cs_df.rule == "Whale_T+1")
        & (cs_df.hold == PRIMARY_HOLD)
        & (cs_df.split == "full")
    ].iloc[0]

    frozen = {
        "sid": SID,
        "name": "國巨",
        "protocol": {
            "features": "branch T-2 and/or T-1 (domestic); optional chip ts1+bhchg @ T-2",
            "decide_before": "T open",
            "enter": "T open",
            "baseline_whale": "T+1 open (L1; matches prior 2327 / L0_VS_L1)",
            "hold": PRIMARY_HOLD,
            "cost": COST,
            "beta": BETA,
            "window": f"{D0}..{d1}",
            "oos": OOS,
            "exit": "cal[i+hold] close (same calendar exit as Whale_T+1)",
        },
        "n_green": len(greens),
        "n_green_IS": n_is_g,
        "n_green_OOS": n_oos_g,
        "warn_IS_thin": n_is_g <= 1,
        "core_champions": core,
        "seats": {t: name_map.get(t, "") for t in seats},
        "frozen_chip_assist": "+".join(FROZEN_CHIP),
        "best_rule": best_row.to_dict() if best_row is not None else None,
        "best_rule_oos": best_oos.to_dict() if best_oos is not None else None,
        "best_chip_assist": best_chip_row.to_dict() if best_chip_row is not None else None,
        "baselines": {
            "Whale_T+1": whale_full.to_dict(),
            "Always_T_open": always.to_dict(),
        },
    }
    frozen_path = OUT / "2327_branch_t2t1_topen_frozen.json"
    frozen_path.write_text(
        json.dumps(frozen, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )

    sweep_path = OUT / "2327_branch_t2t1_topen_sweep.csv"
    trades_path = OUT / "2327_branch_t2t1_topen_trades.csv"
    cs_df.to_csv(sweep_path, index=False)
    trades_df.to_csv(trades_path, index=False)

    # top table for report (H=7 full event_tied)
    top = (
        et.sort_values(["delta_vs_whale", "sum"], ascending=[False, False])
        .head(25)
        .copy()
    )
    top.to_csv(OUT / "2327_branch_t2t1_topen_top25.csv", index=False)

    md = build_report(
        greens=greens,
        n_is_g=n_is_g,
        n_oos_g=n_oos_g,
        core=core,
        seats=seats,
        name_map=name_map,
        cs_df=cs_df,
        best_row=best_row,
        best_oos=best_oos,
        best_chip_row=best_chip_row,
        whale_full=whale_full,
        always=always,
        d1=d1,
        n_rules=len(rules),
    )
    md_path = OUT / "2327_BRANCH_T2T1_TOPEN.md"
    md_path.write_text(md, encoding="utf-8")

    status = {
        "phase": "done",
        "at": datetime.now().isoformat(timespec="seconds"),
        "elapsed_min": round((time.time() - t0) / 60, 2),
        "best_rule": None if best_row is None else str(best_row["rule"]),
        "best_delta": None if best_row is None else float(best_row["delta_vs_whale"]),
        "n_green": len(greens),
        "n_rules": len(rules),
        "out": str(md_path),
    }
    (OUT / "status_2327_branch_t2t1_topen.json").write_text(
        json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log(f"DONE → {md_path} best={status['best_rule']} Δ={status['best_delta']} ({status['elapsed_min']} min)")
    return 0


def build_report(
    *,
    greens: list[str],
    n_is_g: int,
    n_oos_g: int,
    core: dict[str, str],
    seats: list[str],
    name_map: dict[str, str],
    cs_df: pd.DataFrame,
    best_row: pd.Series | None,
    best_oos: pd.Series | None,
    best_chip_row: pd.Series | None,
    whale_full: pd.Series,
    always: pd.Series,
    d1: str,
    n_rules: int,
) -> str:
    lines: list[str] = []
    lines.append("# 2327 國巨 · 分點 T−2/T−1 → T 開盤 vs Whale_T+1\n\n")
    lines.append(f"- 生成：{datetime.now().isoformat(timespec='seconds')}\n")
    lines.append("- Runner：`scripts/research/run_2327_branch_t2t1_topen_sweep.py`\n")
    lines.append(
        f"- 窗：{D0}..{d1}（≈{YEARS:.2f}y）· OOS≥{OOS} · COST={COST} · β={BETA}×IX0001\n"
    )
    lines.append(
        "- 進場慣例：候選臂 **T 開盤**；基準 **Whale_T+1 = T+1 開盤（L1）**"
        "（對齊 `run_2327_chip_branch_t2_t1_sweep.py`／L0_VS_L1，非收盤）\n"
    )
    lines.append(
        "- 決策：僅用 T−2／T−1 分點（+ 可選凍結籌碼 `ts1+bhchg` @ T−2）；"
        "PIT 可在 T 開盤前完成\n"
    )
    lines.append(
        f"- 綠燈事件 n={len(greens)}（IS={n_is_g} / OOS={n_oos_g}）—"
        "**樣本偏薄；IS≈1 時不可用 IS 凍結**\n"
    )
    lines.append(f"- 掃參格子={n_rules} · 席位={len(seats)}\n")
    lines.append("- Research only · **未採納**進 Strategy／Order\n\n")

    # Verdict
    if best_row is not None:
        v = str(best_row["verdict"])
        lines.append("## 結論（先讀）\n\n")
        lines.append(
            f"**總評相對 Whale_T+1：{v}**"
            f"（配對 n={int(best_row['n'])} · 覆蓋={fmt_rate(best_row['coverage'])}）\n\n"
        )
        lines.append(
            f"- 最佳規則：`{best_row['rule']}` — {best_row['label']}\n"
        )
        lines.append(
            f"- 事件對齊 H=7 full：cand mean={fmt_pct(best_row['mean'])} · "
            f"配對 Whale mean={fmt_pct(best_row['paired_whale_mean'])} · "
            f"Δmean={fmt_pct(best_row['delta_vs_whale'])} pp · "
            f"CI [{fmt_pct(best_row['ci_lo'])}, {fmt_pct(best_row['ci_hi'])}] · "
            f"P(Δ≤0)={fmt_p(best_row['p_le0'])}\n"
        )
        if best_oos is not None:
            lines.append(
                f"- OOS 同規則：n={int(best_oos['n'])} · mean={fmt_pct(best_oos['mean'])} · "
                f"Δ={fmt_pct(best_oos['delta_vs_whale'])} · 判定={best_oos['verdict']}\n"
            )
        lines.append(
            f"- Whale_T+1 full H=7：n={int(whale_full['n'])} · hit={fmt_rate(whale_full['hit_rate'])} · "
            f"med={fmt_pct(whale_full['med'])} · mean={fmt_pct(whale_full['mean'])} · "
            f"sum={fmt_pct(whale_full['sum'], 0)}\n"
        )
        lines.append(
            f"- Always_T_open（每筆綠燈）：mean={fmt_pct(always['mean'])} · "
            f"Δ vs Whale={fmt_pct(always['delta_vs_whale'])} · 判定={always['verdict']}\n"
        )
        if best_chip_row is not None:
            lines.append(
                f"- 籌碼輔助最佳：`{best_chip_row['rule']}` · n={int(best_chip_row['n'])} · "
                f"Δ={fmt_pct(best_chip_row['delta_vs_whale'])} · 判定={best_chip_row['verdict']}\n"
            )
        lines.append(
            "\n> ⚠️ 綠燈僅 12 筆、IS=1；掃參格子大 → 最佳規則有過擬合風險。"
            "判定「更好（n偏小）」≠可採納。\n"
        )
    else:
        lines.append("## 結論\n\n無合格規則（n 過薄）。\n")

    # Baselines table
    lines.append("\n## 基準（H=7）\n\n")
    lines.append("| arm | split | n | hit | med% | mean% | sum% | Δ vs Whale | 判定 |\n")
    lines.append("|-----|-------|--:|----:|-----:|------:|-----:|-----------:|------|\n")
    for rule in ("Whale_T+1", "Always_T_open"):
        for split in ("full", "IS", "OOS"):
            r = cs_df[
                (cs_df.rule == rule)
                & (cs_df.hold == PRIMARY_HOLD)
                & (cs_df.split == split)
            ]
            if r.empty:
                continue
            row = r.iloc[0]
            dlt = "—" if rule == "Whale_T+1" else fmt_pct(row["delta_vs_whale"])
            lines.append(
                f"| {rule} | {split} | {int(row['n'])} | {fmt_rate(row['hit_rate'])} | "
                f"{fmt_pct(row['med'])} | {fmt_pct(row['mean'])} | {fmt_pct(row['sum'], 0)} | "
                f"{dlt} | {row['verdict']} |\n"
            )

    # H=10 whale quick
    lines.append("\n## Whale_T+1（H=10 附）\n\n")
    lines.append("| split | n | hit | mean% | sum% |\n")
    lines.append("|-------|--:|----:|------:|-----:|\n")
    for split in ("full", "IS", "OOS"):
        r = cs_df[
            (cs_df.rule == "Whale_T+1")
            & (cs_df.hold == 10)
            & (cs_df.split == split)
        ]
        if r.empty:
            continue
        row = r.iloc[0]
        lines.append(
            f"| {split} | {int(row['n'])} | {fmt_rate(row['hit_rate'])} | "
            f"{fmt_pct(row['mean'])} | {fmt_pct(row['sum'], 0)} |\n"
        )

    lines.append("\n## 席位\n\n")
    lines.append("| tid | 名稱 | 冠軍 |\n")
    lines.append("|-----|------|:----:|\n")
    for tid in seats:
        y = "Y" if tid in core else ""
        lines.append(f"| {tid} | {name_map.get(tid, '')} | {y} |\n")

    # Top rules — n≥3 only (n=1 Δ 噪音不進表)
    lines.append("\n## 事件對齊 Top（H=7 full · n≥3 · 配對 Δ vs Whale_T+1）\n\n")
    lines.append(
        "| rank | rule | n | 覆蓋 | hit | mean% | Δ | CI | P(Δ≤0) | chip | 判定 |\n"
    )
    lines.append(
        "|-----:|------|--:|-----:|----:|------:|--:|----|------:|:----:|------|\n"
    )
    et = cs_df[
        (cs_df.lens == "event_tied")
        & (cs_df.hold == PRIMARY_HOLD)
        & (cs_df.split == "full")
        & (cs_df["n"] >= 3)
    ].sort_values(["delta_vs_whale", "sum"], ascending=[False, False])
    for i, (_, row) in enumerate(et.head(20).iterrows(), 1):
        ci = f"[{fmt_pct(row['ci_lo'])}, {fmt_pct(row['ci_hi'])}]"
        chip_flag = int(row["chip_and"]) if pd.notna(row.get("chip_and")) else 0
        lines.append(
            f"| {i} | `{row['rule']}` | {int(row['n'])} | {fmt_rate(row['coverage'])} | "
            f"{fmt_rate(row['hit_rate'])} | {fmt_pct(row['mean'])} | "
            f"{fmt_pct(row['delta_vs_whale'])} | {ci} | {fmt_p(row['p_le0'])} | "
            f"{'Y' if chip_flag else ''} | {row['verdict']} |\n"
        )

    if best_row is not None:
        lines.append("\n## 凍結最佳規則\n\n")
        lines.append("```json\n")
        lines.append(
            json.dumps(
                {
                    "rule": best_row["rule"],
                    "label": best_row["label"],
                    "n": int(best_row["n"]),
                    "coverage": float(best_row["coverage"]),
                    "mean": float(best_row["mean"]),
                    "delta_vs_whale": float(best_row["delta_vs_whale"]),
                    "verdict": best_row["verdict"],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        lines.append("\n```\n")
        lines.append(
            f"\n產物：`2327_branch_t2t1_topen_frozen.json` · "
            f"`2327_branch_t2t1_topen_sweep.csv` · "
            f"`2327_branch_t2t1_topen_trades.csv`\n"
        )

    lines.append("\n## 方法備註\n\n")
    lines.append(
        "- 配對 Δ = 同綠燈子集上（T_open_arm − Whale_T+1）；"
        "full_vs_whale_delta 為濾後臂 vs **全部**綠燈 Whale（n 不同）\n"
    )
    lines.append(
        "- 兩席 pair = 兩席同日皆達門檻（AND）；champ = 任一冠軍席達門檻（OR）\n"
    )
    lines.append(
        "- 出場與 Whale 同日 `cal[i+hold] close`，故 T 開盤臂多持有綠燈當日\n"
    )
    return "".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
