#!/usr/bin/env python3
"""2327 國巨 · chip ∨ branch / score fusion vs frozen branch-only.

Standalone calendar (NOT Whale subset). Branch fire B = frozen
8840 two-day accel + amount ≥ IS p90. Chip fire C = IS-frozen among
ts1+bhchg / three_streak≥2 / three_streak≥1.

Arms: B · C · B∨C · B∧C · score grid (weights×thr ± continuous).

Research only · 未採納 · Book only.

    PYTHONPATH=src .venv/bin/python scripts/research/run_2327_chip_branch_or_score.py
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from itertools import product
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
SEAT = "8840"
SEAT_NAME = "玉山證券"
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
HOLDS = (6, 7)
PRIMARY_HOLD = 7
N_BOOT = 5000
RNG = np.random.default_rng(42)
MIN_IS_N = 8
CHIP_CANDIDATES = (
    ("ts1+bhchg", "three_streak≥1 ∧ big_holder_pct_chg>0"),
    ("ts2", "three_streak≥2 (W1)"),
    ("ts1", "three_streak≥1"),
)


def log(m: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {m}", flush=True)


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
    n_cal_days: int,
    years: float,
) -> dict:
    if split == "full":
        xs = [t["excess_pct"] for t in trades]
    elif split == "IS":
        xs = [t["excess_pct"] for t in trades if t["split"] == "IS"]
    else:
        xs = [t["excess_pct"] for t in trades if t["split"] == "OOS"]
    sm = summarize(xs)
    boot = bootstrap_delta(xs, whale_xs)
    avg_daily = (
        float(sm["sum"]) / n_cal_days if n_cal_days > 0 and pd.notna(sm["sum"]) else np.nan
    )
    tpy = float(sm["n"]) / years if years > 0 else np.nan
    return {
        **sm,
        "avg_daily": avg_daily,
        "trades_per_year": tpy,
        "delta_vs_whale": boot["delta_mean"],
        "ci_lo": boot["ci_lo"],
        "ci_hi": boot["ci_hi"],
        "p_le0": boot["p_delta_le0"],
        "whale_n": len(whale_xs),
        "whale_mean": float(np.mean(whale_xs)) if whale_xs else np.nan,
    }


def win_criteria_v2(
    full: dict,
    oos: dict,
    ism: dict,
    whale_full: dict,
    whale_oos: dict,
) -> tuple[bool, str]:
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


def verdict_vs_whale(full: dict, whale_full: dict) -> str:
    if full["n"] == 0 or not pd.notna(full.get("mean")):
        return "更差"
    dm = float(full["mean"]) - float(whale_full["mean"])
    ds = float(full["sum"]) - float(whale_full["sum"])
    if dm >= 1.0 and (full["n"] >= 8):
        return "更好"
    if dm <= -2.0 or (ds < -15 and dm < 0):
        return "更差"
    return "差不多"


def md_table(headers: list[str], rows: list[list[str]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "|" + "|".join(["---"] * len(headers)) + "|",
    ]
    for r in rows:
        lines.append("| " + " | ".join(r) + " |")
    return "\n".join(lines)


def chip_fire_mask(panel: pd.DataFrame, cid: str) -> pd.Series:
    ts = panel["ts_d"]
    bh = panel["bh_d"]
    if cid == "ts1+bhchg":
        return (ts >= 1) & (bh > 0)
    if cid == "ts2":
        return ts >= 2
    if cid == "ts1":
        return ts >= 1
    raise ValueError(cid)


def build_panel(
    cal: list[str],
    cal_ext: list[str],
    di: dict[str, int],
    branch_amt: dict[str, dict[str, float]],
    feat_idx: pd.DataFrame,
    ohlc_px: dict,
    bench: dict,
) -> pd.DataFrame:
    rows: list[dict] = []
    for d in cal:
        dm1 = lag_date(cal_ext, di, d, 1)
        a0 = float(branch_amt.get(d, {}).get(SEAT, 0.0) or 0.0)
        a1 = float(branch_amt.get(dm1, {}).get(SEAT, 0.0) or 0.0) if dm1 else 0.0
        row: dict = {
            "d": d,
            "dm1": dm1,
            "split": "IS" if d < OOS else "OOS",
            "amt_d": a0,
            "amt_dm1": a1,
        }
        for h in HOLDS:
            x, ed, xd = excess_l1(d, h, cal_ext, di, ohlc_px, bench)
            row[f"ex_h{h}"] = x
            row[f"entry_h{h}"] = ed
            row[f"exit_h{h}"] = xd
        try:
            fr = feat_idx.loc[(SID, d)]
            if isinstance(fr, pd.DataFrame):
                fr = fr.iloc[0]
            row["ts_d"] = float(fr.get("three_streak", 0) or 0)
            bh = fr.get("big_holder_pct_chg", np.nan)
            row["bh_d"] = float(bh) if pd.notna(bh) else np.nan
        except KeyError:
            row["ts_d"] = np.nan
            row["bh_d"] = np.nan
        rows.append(row)
    return pd.DataFrame(rows)


def arm_row(
    arm: str,
    label: str,
    family: str,
    hold: int,
    fires: set[str],
    cal_ext: list[str],
    di: dict[str, int],
    ohlc_px: dict,
    bench: dict,
    d1: str,
    whale_xs: dict[str, list[float]],
    n_cal: int,
    years: float,
    extra: dict | None = None,
) -> tuple[dict, list[dict]]:
    trades = standalone_trades(
        fires, hold, cal_ext, di, ohlc_px, bench, arm, arm, D0, d1
    )
    full = metrics_block(trades, whale_xs["full"], "full", n_cal, years)
    ism = metrics_block(trades, whale_xs["IS"], "IS", n_cal, years)
    oos = metrics_block(trades, whale_xs["OOS"], "OOS", n_cal, years)
    row = {
        "arm": arm,
        "label": label,
        "family": family,
        "hold": hold,
        "n_calendar": len(fires),
        "IS_n": ism["n"],
        "IS_hit": ism["hit_rate"],
        "IS_mean": ism["mean"],
        "IS_sum": ism["sum"],
        "IS_avg_daily": ism["avg_daily"],
        "full_n": full["n"],
        "full_hit": full["hit_rate"],
        "full_mean": full["mean"],
        "full_sum": full["sum"],
        "full_avg_daily": full["avg_daily"],
        "full_delta": full["delta_vs_whale"],
        "full_p_le0": full["p_le0"],
        "OOS_n": oos["n"],
        "OOS_hit": oos["hit_rate"],
        "OOS_mean": oos["mean"],
        "OOS_sum": oos["sum"],
        "OOS_avg_daily": oos["avg_daily"],
        "OOS_delta": oos["delta_vs_whale"],
        "trades_per_year": full["trades_per_year"],
    }
    if extra:
        row.update(extra)
    return row, trades


def main() -> int:
    global SID, SEAT, SEAT_NAME
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sid", default="2327", help="stock id (default 2327)")
    ap.add_argument("--d1", default=D1)
    args = ap.parse_args()
    SID = str(args.sid)
    # Resolve per-sid branch seat from trades core (2327 keeps 8840 when present).
    trades_path = TRADES_DIR / f"{SID}.json"
    if not trades_path.exists():
        raise SystemExit(f"missing trades/{SID}.json")
    doc = json.loads(trades_path.read_text(encoding="utf-8"))
    core = {str(k): str(v) for k, v in (doc.get("core") or {}).items()}
    if not core:
        raise SystemExit(f"empty core in trades/{SID}.json")
    if SID == "2327" and "8840" in core:
        SEAT = "8840"
    else:
        SEAT = next(iter(core.keys()))
    SEAT_NAME = core.get(SEAT, SEAT)

    d1 = str(args.d1)
    years = (pd.Timestamp(d1) - pd.Timestamp(D0)).days / 365.25

    t0 = time.time()
    OUT.mkdir(parents=True, exist_ok=True)
    log(f"{SID} chip∨branch / score · seat={SEAT} · db={DEFAULT_DB_PATH}")

    conn = connect_ro(DEFAULT_DB_PATH)
    whale = load_whale_events([SID])
    whale = whale[(whale["signal_date"] >= D0) & (whale["signal_date"] <= d1)].copy()
    greens = sorted(whale["signal_date"].astype(str).tolist())
    log(f"Whale greens (external only) n={len(greens)}")

    cal = [d for d in load_calendar(conn, D0, d1) if D0 <= d <= d1]
    cal_ext = load_calendar(conn, D0, "2026-08-31")
    di = {d: i for i, d in enumerate(cal_ext)}
    n_cal = len(cal)
    bench = load_bench(conn, D0, d1)
    ohlc = load_ohlc(conn, [SID], D0, d1)
    ohlc_px = {
        (str(r.sid), str(r.d)): (float(r.open), float(r.close))
        for r in ohlc.itertuples()
    }
    px_close = {(str(r.sid), str(r.d)): float(r.close) for r in ohlc.itertuples()}

    hs = HS_PATH if HS_PATH.exists() else None
    gov = GOV_PATH if GOV_PATH.exists() else None
    log("chip features")
    feat = build_chip_feature_frame(
        conn, [SID], D0, d1, holding_shares_path=hs, gov_bank_path=gov
    )
    feat = enrich_sums(feat)
    feat_idx = feat.set_index(["sid", "d"])

    log(f"branch tape ({SEAT})")
    tape = fetch_tape(conn, cal)
    conn.close()
    tape = attach_amt(tape, px_close)
    tape_seat = tape[tape["tid"] == SEAT].copy()
    branch_amt: dict[str, dict[str, float]] = {}
    for r in tape_seat.itertuples():
        branch_amt.setdefault(str(r.d), {})[SEAT] = float(r.amt) if pd.notna(r.amt) else 0.0

    panel = build_panel(cal, cal_ext, di, branch_amt, feat_idx, ohlc_px, bench)
    # keep days with valid primary excess
    panel = panel.loc[panel[f"ex_h{PRIMARY_HOLD}"].notna()].copy()
    is_mask = panel["split"] == "IS"
    log(f"panel n={len(panel)} IS={int(is_mask.sum())} OOS={int((~is_mask).sum())}")

    # ---- Branch fire B: accel ∧ amt≥IS-p90 ----
    a0 = panel["amt_d"]
    a1 = panel["amt_dm1"]
    accel = (a1 > 0) & (a0 > a1)
    pos_is = a0[is_mask & (a0 > 0)]
    thr90 = float(pos_is.quantile(0.90)) if len(pos_is) >= 30 else 1e8
    b_mask = accel & (a0 >= thr90)
    b_fires = set(panel.loc[b_mask, "d"].astype(str))
    log(
        f"B=8840 accel∧p90 thr={thr90/1e8:.3f}億 · calendar fires={len(b_fires)} "
        f"(IS={int((b_mask & is_mask).sum())})"
    )

    # continuous helpers (IS-fit z)
    amt_is = a0[is_mask & (a0 > 0)]
    amt_mu = float(amt_is.mean()) if len(amt_is) else 0.0
    amt_sd = float(amt_is.std(ddof=0)) if len(amt_is) > 1 else 1.0
    if amt_sd <= 0:
        amt_sd = 1.0
    accel_ratio = np.where(a1 > 0, np.minimum(a0 / a1, 3.0) / 3.0, 0.0)
    amt_z = ((a0 - amt_mu) / amt_sd).clip(-2, 3) / 3.0  # roughly [-0.67, 1]
    amt_z = np.where(a0 > 0, amt_z, 0.0)
    ts_term = (panel["ts_d"].fillna(0).clip(0, 5) / 5.0).to_numpy()
    panel = panel.assign(
        B=b_mask.astype(int),
        accel_ratio=accel_ratio,
        amt_z=amt_z,
        ts_term=ts_term,
    )

    # ---- Chip candidates: freeze best C on IS alone ----
    chip_is_rows: list[dict] = []
    chip_fires_map: dict[str, set[str]] = {}
    for cid, clabel in CHIP_CANDIDATES:
        cm = chip_fire_mask(panel, cid)
        fires = set(panel.loc[cm, "d"].astype(str))
        chip_fires_map[cid] = fires
        trades = standalone_trades(
            fires, PRIMARY_HOLD, cal_ext, di, ohlc_px, bench, f"C_{cid}", cid, D0, d1
        )
        is_xs = [t["excess_pct"] for t in trades if t["split"] == "IS"]
        sm = summarize(is_xs)
        score = (
            (sm["mean"] if pd.notna(sm["mean"]) else -999.0)
            + 0.05 * min(sm["n"], 30)
            + (0.0 if sm["n"] >= MIN_IS_N else -5.0)
        )
        chip_is_rows.append(
            {
                "cid": cid,
                "label": clabel,
                "IS_n": sm["n"],
                "IS_mean": sm["mean"],
                "IS_hit": sm["hit_rate"],
                "IS_sum": sm["sum"],
                "n_calendar": len(fires),
                "_score": score,
            }
        )
        log(
            f"  C cand {cid}: IS n={sm['n']} mean={fmt_pct(sm['mean'])} "
            f"cal={len(fires)}"
        )
    chip_is_df = pd.DataFrame(chip_is_rows).sort_values("_score", ascending=False)
    best_c = chip_is_df.iloc[0]
    c_id = str(best_c["cid"])
    c_label = str(best_c["label"])
    c_fires = chip_fires_map[c_id]
    c_mask = panel["d"].isin(c_fires)
    panel["C"] = c_mask.astype(int)
    log(f"frozen C on IS: {c_id} ({c_label})")

    # ---- Whale benchmark ----
    whale_trades: list[dict] = []
    whale_xs_by_h: dict[int, dict[str, list[float]]] = {
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
            whale_xs_by_h[hold]["full"].append(x)
            whale_xs_by_h[hold][sp].append(x)

    whale_full = summarize(whale_xs_by_h[PRIMARY_HOLD]["full"])
    whale_oos = summarize(whale_xs_by_h[PRIMARY_HOLD]["OOS"])
    whale_is = summarize(whale_xs_by_h[PRIMARY_HOLD]["IS"])
    log(
        f"Whale H7 full n={whale_full['n']} mean={whale_full['mean']:.2f} "
        f"sum={whale_full['sum']:.1f}"
    )

    # ---- Logical arms ----
    or_fires = b_fires | c_fires
    and_fires = b_fires & c_fires
    only_c_extra = c_fires - b_fires
    only_b = b_fires - c_fires
    both = and_fires
    log(
        f"OR calendar={len(or_fires)} AND={len(and_fires)} "
        f"B\\C={len(only_b)} C\\B={len(only_c_extra)}"
    )

    arm_defs: list[tuple[str, str, str, set[str], dict]] = [
        (
            "B_only",
            f"8840 accel∧IS-p90({thr90/1e8:.2f}億)",
            "branch",
            b_fires,
            {"thr90": thr90},
        ),
        (f"C_only__{c_id}", c_label, "chip", c_fires, {"c_id": c_id}),
        (
            f"B_OR_C__{c_id}",
            f"B ∨ C({c_id})",
            "or",
            or_fires,
            {"c_id": c_id},
        ),
        (
            f"B_AND_C__{c_id}",
            f"B ∧ C({c_id})",
            "and",
            and_fires,
            {"c_id": c_id},
        ),
    ]
    # also report other C-only for honesty
    for cid, clabel in CHIP_CANDIDATES:
        if cid == c_id:
            continue
        arm_defs.append(
            (f"C_only__{cid}", clabel, "chip_alt", chip_fires_map[cid], {"c_id": cid})
        )

    summary_rows: list[dict] = []
    all_trades: list[dict] = list(whale_trades)
    score_grid_rows: list[dict] = []

    for hold in HOLDS:
        wxs = whale_xs_by_h[hold]
        # whale row
        w_full = metrics_block(whale_trades, wxs["full"], "full", n_cal, years)
        # filter whale trades by hold
        wt = [t for t in whale_trades if t["hold"] == hold]
        w_full = metrics_block(wt, wxs["full"], "full", n_cal, years)
        w_is = metrics_block(wt, wxs["IS"], "IS", n_cal, years)
        w_oos = metrics_block(wt, wxs["OOS"], "OOS", n_cal, years)
        summary_rows.append(
            {
                "arm": "Whale_T+1",
                "label": "Whale greens → T+1 open",
                "family": "benchmark",
                "hold": hold,
                "n_calendar": len(greens),
                "IS_n": w_is["n"],
                "IS_hit": w_is["hit_rate"],
                "IS_mean": w_is["mean"],
                "IS_sum": w_is["sum"],
                "IS_avg_daily": w_is["avg_daily"],
                "full_n": w_full["n"],
                "full_hit": w_full["hit_rate"],
                "full_mean": w_full["mean"],
                "full_sum": w_full["sum"],
                "full_avg_daily": w_full["avg_daily"],
                "full_delta": 0.0,
                "full_p_le0": np.nan,
                "OOS_n": w_oos["n"],
                "OOS_hit": w_oos["hit_rate"],
                "OOS_mean": w_oos["mean"],
                "OOS_sum": w_oos["sum"],
                "OOS_avg_daily": w_oos["avg_daily"],
                "OOS_delta": 0.0,
                "trades_per_year": w_full["trades_per_year"],
                "win": False,
                "win_reason": "",
                "verdict": "基準",
            }
        )

        for arm, label, family, fires, extra in arm_defs:
            row, trades = arm_row(
                arm,
                label,
                family,
                hold,
                fires,
                cal_ext,
                di,
                ohlc_px,
                bench,
                d1,
                wxs,
                n_cal,
                years,
                extra,
            )
            full = {
                "n": row["full_n"],
                "mean": row["full_mean"],
                "sum": row["full_sum"],
                "p_le0": row["full_p_le0"],
            }
            oos = {"n": row["OOS_n"], "mean": row["OOS_mean"]}
            ism = {"n": row["IS_n"], "mean": row["IS_mean"]}
            wf = summarize(wxs["full"])
            wo = summarize(wxs["OOS"])
            met, reason = win_criteria_v2(full, oos, ism, wf, wo)
            row["win"] = met
            row["win_reason"] = reason
            row["verdict"] = verdict_vs_whale(full, wf)
            summary_rows.append(row)
            if hold == PRIMARY_HOLD:
                all_trades.extend(trades)
            log(
                f"  [{hold}] {arm}: full n={row['full_n']} mean={fmt_pct(row['full_mean'])} "
                f"sum={fmt_pct(row['full_sum'],0)} OOS={fmt_pct(row['OOS_mean'])} "
                f"→ {row['verdict']}"
            )

        # ---- Score grid (primary hold + H=6) ----
        # score = wb*I(B) + wc*I(C) + w_ts*ts/5 + w_ar*accel_ratio + w_az*amt_z
        # keep continuous weights in {0,1}; discrete wb,wc in {0,1,2}; thr in {1,2,3}
        Ib = panel["B"].to_numpy(dtype=float)
        Ic = panel["C"].to_numpy(dtype=float)
        ar = panel["accel_ratio"].to_numpy(dtype=float)
        az = panel["amt_z"].to_numpy(dtype=float)
        tst = panel["ts_term"].to_numpy(dtype=float)
        days = panel["d"].astype(str).to_numpy()

        for wb, wc, w_ts, w_cont, thr in product(
            (0, 1, 2), (0, 1, 2), (0, 1), (0, 1), (1, 2, 3)
        ):
            # skip pure zeros / redundant with B-only or C-only when no continuous
            if wb == 0 and wc == 0 and w_ts == 0 and w_cont == 0:
                continue
            # require at least one discrete signal weight when continuous-only
            score = (
                wb * Ib
                + wc * Ic
                + w_ts * tst
                + w_cont * (0.5 * ar + 0.5 * az)
            )
            fire_mask = score >= thr
            # need B or C contribution for fusion interest, or continuous+chip
            fires = set(days[fire_mask].tolist())
            if not fires:
                continue
            arm_id = f"score_wb{wb}_wc{wc}_wts{w_ts}_wct{w_cont}_thr{thr}"
            label = (
                f"score={wb}·B+{wc}·C+{w_ts}·ts/5+{w_cont}·(ar+az)/2 ≥{thr}"
            )
            row, trades = arm_row(
                arm_id,
                label,
                "score",
                hold,
                fires,
                cal_ext,
                di,
                ohlc_px,
                bench,
                d1,
                wxs,
                n_cal,
                years,
                {
                    "wb": wb,
                    "wc": wc,
                    "w_ts": w_ts,
                    "w_cont": w_cont,
                    "thr": thr,
                    "c_id": c_id,
                },
            )
            full = {
                "n": row["full_n"],
                "mean": row["full_mean"],
                "sum": row["full_sum"],
                "p_le0": row["full_p_le0"],
            }
            oos = {"n": row["OOS_n"], "mean": row["OOS_mean"]}
            ism = {"n": row["IS_n"], "mean": row["IS_mean"]}
            wf = summarize(wxs["full"])
            wo = summarize(wxs["OOS"])
            met, reason = win_criteria_v2(full, oos, ism, wf, wo)
            row["win"] = met
            row["win_reason"] = reason
            row["verdict"] = verdict_vs_whale(full, wf)
            score_grid_rows.append(row)
            if hold == PRIMARY_HOLD and (
                met
                or (
                    row["IS_n"] >= MIN_IS_N
                    and pd.notna(row["IS_mean"])
                    and row["IS_mean"] >= 0
                )
            ):
                # keep trades only for notable score arms to limit CSV size
                if met or row["full_mean"] == max(
                    (
                        r["full_mean"]
                        for r in score_grid_rows
                        if r["hold"] == PRIMARY_HOLD and pd.notna(r["full_mean"])
                    ),
                    default=-999,
                ):
                    all_trades.extend(trades)

    summary_df = pd.DataFrame(summary_rows)
    score_df = pd.DataFrame(score_grid_rows)
    # attach best score per hold into summary
    best_scores: list[dict] = []
    for hold in HOLDS:
        sub = score_df[score_df["hold"] == hold].copy()
        if sub.empty:
            continue
        # IS freeze pick: IS n≥8 & IS mean≥0, then full mean, then OOS mean
        ok = sub[
            (sub["IS_n"] >= MIN_IS_N)
            & (sub["IS_mean"].notna())
            & (sub["IS_mean"] >= 0)
        ].copy()
        pool = ok if not ok.empty else sub
        pool = pool.sort_values(
            ["win", "full_mean", "OOS_mean", "full_sum"],
            ascending=[False, False, False, False],
        )
        best = pool.iloc[0].to_dict()
        best_scores.append(best)
        summary_df = pd.concat([summary_df, pd.DataFrame([best])], ignore_index=True)

    # ---- OR dilution diagnostics (H=7) ----
    b_row = summary_df[
        (summary_df["arm"] == "B_only") & (summary_df["hold"] == PRIMARY_HOLD)
    ].iloc[0]
    or_row = summary_df[
        (summary_df["arm"] == f"B_OR_C__{c_id}") & (summary_df["hold"] == PRIMARY_HOLD)
    ].iloc[0]
    and_row = summary_df[
        (summary_df["arm"] == f"B_AND_C__{c_id}") & (summary_df["hold"] == PRIMARY_HOLD)
    ]
    and_row = and_row.iloc[0] if len(and_row) else None
    c_row = summary_df[
        (summary_df["arm"] == f"C_only__{c_id}") & (summary_df["hold"] == PRIMARY_HOLD)
    ].iloc[0]

    # trades that OR adds beyond B
    or_trades_h7 = standalone_trades(
        or_fires, PRIMARY_HOLD, cal_ext, di, ohlc_px, bench, "OR", "OR", D0, d1
    )
    b_trades_h7 = standalone_trades(
        b_fires, PRIMARY_HOLD, cal_ext, di, ohlc_px, bench, "B", "B", D0, d1
    )
    b_sig = {t["signal_date"] for t in b_trades_h7}
    extra_trades = [t for t in or_trades_h7 if t["signal_date"] not in b_sig]
    extra_xs = [t["excess_pct"] for t in extra_trades]
    extra_sm = summarize(extra_xs)
    dilution = (
        pd.notna(or_row["full_mean"])
        and pd.notna(b_row["full_mean"])
        and float(or_row["full_mean"]) < float(b_row["full_mean"]) - 0.5
        and int(or_row["full_n"]) > int(b_row["full_n"])
    )

    # pick overall fusion winner (OR / AND / score) vs Whale
    fusion = summary_df[
        (summary_df["hold"] == PRIMARY_HOLD)
        & (summary_df["family"].isin(["or", "and", "score"]))
    ].copy()
    wins = fusion[fusion["win"] == True]  # noqa: E712
    frozen = None
    if not wins.empty:
        wins = wins.sort_values(["full_mean", "OOS_mean"], ascending=False)
        frozen = wins.iloc[0].to_dict()
        # also require not worse than B-only on full mean by >2pp (fusion utility)
        if (
            pd.notna(frozen["full_mean"])
            and pd.notna(b_row["full_mean"])
            and float(frozen["full_mean"]) < float(b_row["full_mean"]) - 2.0
        ):
            log("win vs Whale but worse than B-only by >2pp — still record, flag")
    else:
        # near-best score with IS ok
        near = fusion[
            (fusion["IS_n"] >= MIN_IS_N)
            & (fusion["IS_mean"].notna())
            & (fusion["IS_mean"] >= 0)
        ].sort_values(["full_mean", "OOS_mean"], ascending=False)
        if not near.empty:
            log(f"no honest win; near-best fusion={near.iloc[0]['arm']}")

    # compare fusion to B
    def better_than_b(row: pd.Series | dict) -> str:
        fm = float(row["full_mean"]) if pd.notna(row["full_mean"]) else -999
        bm = float(b_row["full_mean"])
        om = float(row["OOS_mean"]) if pd.notna(row["OOS_mean"]) else -999
        bom = float(b_row["OOS_mean"]) if pd.notna(b_row["OOS_mean"]) else 0
        if fm >= bm + 0.5 and om >= bom - 1.0:
            return "更好"
        if fm <= bm - 1.5 or (int(row["full_n"]) > int(b_row["full_n"]) and fm < bm - 0.5):
            return "更差"
        return "差不多"

    # ---- write artifacts ----
    summary_df.to_csv(OUT / f"{SID}_chip_or_score_arms.csv", index=False)
    score_df.to_csv(OUT / f"{SID}_chip_or_score_grid.csv", index=False)
    pd.DataFrame(all_trades).to_csv(OUT / f"{SID}_chip_or_score_trades.csv", index=False)
    chip_is_df.drop(columns=["_score"], errors="ignore").to_csv(
        OUT / f"{SID}_chip_or_score_C_is_freeze.csv", index=False
    )
    panel[
        ["d", "split", "amt_d", "amt_dm1", "ts_d", "bh_d", "B", "C", "ex_h6", "ex_h7"]
    ].to_csv(OUT / f"{SID}_chip_or_score_panel.csv", index=False)

    status = {
        "sid": SID,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "B": {
            "rule": "seat_8840__accel_p90",
            "thr90": thr90,
            "n_calendar": len(b_fires),
        },
        "C_frozen": {"id": c_id, "label": c_label, "IS": best_c.to_dict()},
        "OR_dilution": {
            "dilutes_mean": bool(dilution),
            "extra_trades_n": extra_sm["n"],
            "extra_trades_mean": extra_sm["mean"],
            "extra_trades_sum": extra_sm["sum"],
            "B_full_mean": float(b_row["full_mean"]),
            "OR_full_mean": float(or_row["full_mean"]),
            "B_full_n": int(b_row["full_n"]),
            "OR_full_n": int(or_row["full_n"]),
        },
        "frozen_winner": None,
        "elapsed_s": round(time.time() - t0, 1),
    }

    if frozen is not None:
        uses_c = int(frozen.get("wc", 0) or 0) > 0 if frozen.get("family") == "score" else (
            "OR" in str(frozen.get("arm", "")) or "AND" in str(frozen.get("arm", ""))
        )
        frozen_payload = {
            "sid": SID,
            "name": "國巨",
            "protocol": {
                "decide": "D close: B=8840 accel∧IS-p90; C=IS-frozen chip",
                "enter": "D+1 open (L1)",
                "hold": PRIMARY_HOLD,
                "non_overlapping": True,
                "cost": COST,
                "beta": BETA,
                "window": f"{D0}..{d1}",
                "oos": OOS,
                "calendar": "standalone (not Whale subset)",
            },
            "B": {
                "seq_id": "seat_8840__accel_p90",
                "thr90": thr90,
                "label": f"8840 accel ∧ D≥IS-p90({thr90/1e8:.2f}億)",
            },
            "C": {"id": c_id, "label": c_label},
            "freeze_rule": {
                "arm": frozen["arm"],
                "label": frozen["label"],
                "family": frozen["family"],
                "win_reason": frozen.get("win_reason"),
                "uses_chip_C": bool(uses_c),
                "honesty_note": (
                    None
                    if uses_c
                    else "score winner has wc=0 — branch ± streak continuous, NOT chip∨branch fusion"
                ),
                "IS": {
                    "n": int(frozen["IS_n"]),
                    "mean": frozen["IS_mean"],
                    "hit": frozen["IS_hit"],
                    "sum": frozen["IS_sum"],
                },
                "full": {
                    "n": int(frozen["full_n"]),
                    "mean": frozen["full_mean"],
                    "hit": frozen["full_hit"],
                    "sum": frozen["full_sum"],
                    "delta_vs_whale": frozen["full_delta"],
                    "p_le0": frozen["full_p_le0"],
                },
                "OOS": {
                    "n": int(frozen["OOS_n"]),
                    "mean": frozen["OOS_mean"],
                    "hit": frozen["OOS_hit"],
                    "sum": frozen["OOS_sum"],
                },
                "vs_B_only": better_than_b(frozen),
            },
            "whale_benchmark_H7": {
                "full": whale_full,
                "OOS": whale_oos,
                "IS": whale_is,
            },
            "status": "win_vs_whale" if uses_c else "win_vs_whale_but_not_chip_fusion",
        }
        (OUT / f"{SID}_chip_or_score_frozen.json").write_text(
            json.dumps(frozen_payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        status["frozen_winner"] = frozen["arm"]
        log(f"wrote frozen winner {frozen['arm']}")
    else:
        p = OUT / f"{SID}_chip_or_score_frozen.json"
        if p.exists():
            p.unlink()
        status["frozen_winner"] = None
        status["status"] = "no_honest_fusion_win"
        log("no honest fusion winner vs Whale — removed stale frozen json if any")

    (OUT / f"status_{SID}_chip_or_score.json").write_text(
        json.dumps(status, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    # ---- markdown report ----
    best_score_h7 = next((x for x in best_scores if x["hold"] == PRIMARY_HOLD), None)
    best_score_h6 = next((x for x in best_scores if x["hold"] == 6), None)

    # overall verdict (honest: OR vs score vs true chip-fusion)
    or_vs_b = better_than_b(or_row)
    score_vs_b = better_than_b(best_score_h7) if best_score_h7 else "—"
    or_vs_w = str(or_row["verdict"])
    score_uses_c = bool(
        best_score_h7 is not None and int(best_score_h7.get("wc", 0) or 0) > 0
    )
    # best score that actually weights C (wc≥1)
    score_with_c = None
    sg_c = score_df[
        (score_df["hold"] == PRIMARY_HOLD)
        & (score_df["wc"] >= 1)
        & (score_df["IS_n"] >= MIN_IS_N)
        & (score_df["IS_mean"].notna())
        & (score_df["IS_mean"] >= 0)
    ].sort_values(["full_mean", "OOS_mean"], ascending=False)
    if not sg_c.empty:
        score_with_c = sg_c.iloc[0].to_dict()
    if dilution and or_vs_b == "更差":
        if best_score_h7 and score_vs_b == "更好" and not score_uses_c:
            overall = "OR 更差（稀釋）；Score 微勝 B 但未用 C（非籌碼融合）"
        elif score_with_c and better_than_b(score_with_c) == "更好":
            overall = "OR 更差；含 C 的 Score 微勝 B"
        else:
            overall = "更差（OR 稀釋；chip 融合未勝過 branch-only）"
    elif frozen is not None and better_than_b(frozen) == "更好":
        overall = "更好"
    elif or_vs_w == "差不多" and or_vs_b in ("差不多", "更差"):
        overall = "差不多／未勝過 branch-only"
    else:
        overall = or_vs_w if or_vs_b != "更差" else "更差"

    lines: list[str] = []
    lines.append("# 2327 國巨 · Chip ∨ Branch / Score 融合 vs 凍結 Branch-only")
    lines.append("")
    lines.append(f"- 生成：{datetime.now().isoformat(timespec='seconds')}")
    lines.append(
        "- Runner：`scripts/research/run_2327_chip_branch_or_score.py`"
    )
    lines.append(
        f"- 窗：{D0}..{d1}（≈{years:.2f}y）· OOS≥{OOS} · COST={COST} · β={BETA}×IX0001"
    )
    lines.append(
        "- 日曆：**standalone**（非 Whale 子集）· 進場 D+1 open · 非重疊 · 主 H=7（兼報 H=6）"
    )
    lines.append(
        f"- **B**：`seat_8840__accel_p90`（玉山 兩日加速 ∧ D 金額 ≥ IS 正向日 p90 ≈ {thr90/1e8:.2f}億）"
    )
    lines.append(f"- **C**（IS 凍結）：`{c_id}` = {c_label}")
    lines.append("- Research only · **未採納**進 Strategy／Order")
    lines.append("")
    lines.append("## 結論（先讀）")
    lines.append("")
    lines.append(f"**總評相對 Whale_T+1 / 凍結 B：{overall}**")
    lines.append("")
    lines.append(
        f"- Whale_T+1 H=7 full：n={whale_full['n']} · hit={fmt_rate(whale_full['hit_rate'])} · "
        f"mean={fmt_pct(whale_full['mean'])} · sum={fmt_pct(whale_full['sum'], 0)}"
    )
    lines.append(
        f"- **B only** full：n={int(b_row['full_n'])} · mean={fmt_pct(b_row['full_mean'])} · "
        f"sum={fmt_pct(b_row['full_sum'], 0)} · OOS mean={fmt_pct(b_row['OOS_mean'])} · "
        f"判定 vs Whale：**{b_row['verdict']}**"
    )
    lines.append(
        f"- **C only** (`{c_id}`) full：n={int(c_row['full_n'])} · "
        f"mean={fmt_pct(c_row['full_mean'])} · sum={fmt_pct(c_row['full_sum'], 0)} · "
        f"**{c_row['verdict']}**"
    )
    lines.append(
        f"- **B ∨ C** full：n={int(or_row['full_n'])} · mean={fmt_pct(or_row['full_mean'])} · "
        f"sum={fmt_pct(or_row['full_sum'], 0)} · vs B：**{or_vs_b}** · vs Whale：**{or_vs_w}**"
    )
    if and_row is not None:
        lines.append(
            f"- **B ∧ C** full：n={int(and_row['full_n'])} · "
            f"mean={fmt_pct(and_row['full_mean'])} · sum={fmt_pct(and_row['full_sum'], 0)} "
            f"（預期稀疏）· **{and_row['verdict']}**"
        )
    if best_score_h7:
        wc_note = (
            "（**wc=0，未用籌碼 C** — 實為 B ± streak 連續項）"
            if not score_uses_c
            else "（含 C 權重）"
        )
        lines.append(
            f"- **Score 最佳（IS 可凍結）**：`{best_score_h7['arm']}`{wc_note} · "
            f"full n={int(best_score_h7['full_n'])} · mean={fmt_pct(best_score_h7['full_mean'])} · "
            f"sum={fmt_pct(best_score_h7['full_sum'], 0)} · vs B：**{score_vs_b}** · "
            f"vs Whale：**{best_score_h7['verdict']}**"
        )
    if score_with_c:
        lines.append(
            f"- **Score 含 C 最佳**：`{score_with_c['arm']}` · "
            f"full n={int(score_with_c['full_n'])} · mean={fmt_pct(score_with_c['full_mean'])} · "
            f"sum={fmt_pct(score_with_c['full_sum'], 0)} · vs B：**{better_than_b(score_with_c)}** · "
            f"vs Whale：**{score_with_c['verdict']}**"
        )
    if dilution:
        lines.append(
            f"- ⚠️ **OR 稀釋 mean**：相對 B 多出非重疊成交 n={extra_sm['n']} · "
            f"extra mean={fmt_pct(extra_sm['mean'])} · extra sum={fmt_pct(extra_sm['sum'], 0)}；"
            f"OR mean {fmt_pct(or_row['full_mean'])} < B mean {fmt_pct(b_row['full_mean'])}"
        )
    else:
        lines.append(
            f"- OR 相對 B 的增量成交：n={extra_sm['n']} · mean={fmt_pct(extra_sm['mean'])} · "
            f"sum={fmt_pct(extra_sm['sum'], 0)}"
        )
    if frozen:
        lines.append(
            f"- 凍結：`{frozen['arm']}`（{frozen.get('win_reason')}）→ "
            f"`{SID}_chip_or_score_frozen.json`"
        )
    else:
        lines.append("- **無誠實融合勝出**（相對 Whale 勝出門檻）· 不寫凍結 json")
    lines.append("")
    lines.append("### 勝出門檻（與序列研究相同）")
    lines.append("")
    lines.append("1. OOS mean ≥ Whale OOS **且** OOS n≥5 **且** IS n≥8 **且** IS mean≥0")
    lines.append(
        "2. Full mean ≥ Whale full **且** bootstrap P(Δ≤0)≤0.15 **且** n≥12（IS mean≥0）"
    )
    lines.append(
        "3. Full sum 明顯更高（>+10%）**且** mean 不差過 2pp（n≥12 · IS mean≥0）"
    )
    lines.append("")
    lines.append("## C 候選 · IS 凍結")
    lines.append("")
    crow = []
    for _, r in chip_is_df.iterrows():
        mark = "← 凍結" if r["cid"] == c_id else ""
        crow.append(
            [
                str(r["cid"]),
                str(r["label"]),
                str(int(r["IS_n"])),
                fmt_pct(r["IS_mean"]),
                fmt_rate(r["IS_hit"]),
                fmt_pct(r["IS_sum"], 0),
                str(int(r["n_calendar"])),
                mark,
            ]
        )
    lines.append(
        md_table(
            ["cid", "label", "IS_n", "IS_mean%", "IS_hit", "IS_sum%", "cal", ""],
            crow,
        )
    )
    lines.append("")
    lines.append("## 主臂對照（H=7）")
    lines.append("")
    h7 = summary_df[
        (summary_df["hold"] == PRIMARY_HOLD)
        & (summary_df["family"].isin(["benchmark", "branch", "chip", "or", "and", "score"]))
    ].copy()
    # keep one score (best) + logical
    keep_arms = {
        "Whale_T+1",
        "B_only",
        f"C_only__{c_id}",
        f"B_OR_C__{c_id}",
        f"B_AND_C__{c_id}",
    }
    if best_score_h7:
        keep_arms.add(best_score_h7["arm"])
    h7 = h7[h7["arm"].isin(keep_arms)]
    # stable order
    order = [
        "Whale_T+1",
        "B_only",
        f"C_only__{c_id}",
        f"B_OR_C__{c_id}",
        f"B_AND_C__{c_id}",
    ]
    if best_score_h7:
        order.append(best_score_h7["arm"])
    h7["_ord"] = h7["arm"].map({a: i for i, a in enumerate(order)})
    h7 = h7.sort_values("_ord")

    def split_block(r: pd.Series, split: str) -> list[str]:
        if split == "full":
            return [
                str(int(r["full_n"])),
                fmt_rate(r["full_hit"]),
                fmt_pct(r["full_mean"]),
                fmt_pct(r["full_sum"], 0),
                fmt_pct(r["full_avg_daily"], 3),
                fmt_pct(r["full_delta"]),
                str(r["verdict"]),
            ]
        if split == "IS":
            return [
                str(int(r["IS_n"])),
                fmt_rate(r["IS_hit"]),
                fmt_pct(r["IS_mean"]),
                fmt_pct(r["IS_sum"], 0),
                fmt_pct(r["IS_avg_daily"], 3),
                "—",
                "—",
            ]
        return [
            str(int(r["OOS_n"])),
            fmt_rate(r["OOS_hit"]),
            fmt_pct(r["OOS_mean"]),
            fmt_pct(r["OOS_sum"], 0),
            fmt_pct(r["OOS_avg_daily"], 3),
            fmt_pct(r["OOS_delta"]),
            "—",
        ]

    for split, title in (("full", "Full"), ("IS", "IS"), ("OOS", "OOS")):
        lines.append(f"### {title}")
        lines.append("")
        rows = []
        for _, r in h7.iterrows():
            rows.append([str(r["arm"]), str(r["label"])[:40], *split_block(r, split)])
        lines.append(
            md_table(
                [
                    "arm",
                    "label",
                    "n",
                    "hit",
                    "mean%",
                    "sum%",
                    "avg_daily%",
                    "ΔvsWhale",
                    "判定",
                ],
                rows,
            )
        )
        lines.append("")

    lines.append("## 其他 C-only（對照）")
    lines.append("")
    alt = summary_df[
        (summary_df["hold"] == PRIMARY_HOLD) & (summary_df["family"] == "chip_alt")
    ]
    arows = []
    for _, r in alt.iterrows():
        arows.append(
            [
                str(r["arm"]),
                str(int(r["full_n"])),
                fmt_pct(r["full_mean"]),
                fmt_pct(r["full_sum"], 0),
                fmt_pct(r["OOS_mean"]),
                str(r["verdict"]),
            ]
        )
    if arows:
        lines.append(
            md_table(
                ["arm", "full_n", "full_mean%", "full_sum%", "OOS_mean%", "判定"],
                arows,
            )
        )
    else:
        lines.append("（無）")
    lines.append("")

    lines.append("## Score 網格 · IS Top（H=7 · IS n≥8 ∧ IS mean≥0）")
    lines.append("")
    sg = score_df[
        (score_df["hold"] == PRIMARY_HOLD)
        & (score_df["IS_n"] >= MIN_IS_N)
        & (score_df["IS_mean"].notna())
        & (score_df["IS_mean"] >= 0)
    ].sort_values(["full_mean", "OOS_mean"], ascending=False)
    srows = []
    for _, r in sg.head(12).iterrows():
        srows.append(
            [
                str(r["arm"]).replace("score_", ""),
                str(int(r["IS_n"])),
                fmt_pct(r["IS_mean"]),
                str(int(r["full_n"])),
                fmt_pct(r["full_mean"]),
                fmt_pct(r["full_sum"], 0),
                str(int(r["OOS_n"])),
                fmt_pct(r["OOS_mean"]),
                "Y" if r["win"] else "N",
                str(r["verdict"]),
            ]
        )
    if srows:
        lines.append(
            md_table(
                [
                    "params",
                    "IS_n",
                    "IS_mean",
                    "full_n",
                    "full_mean",
                    "full_sum",
                    "OOS_n",
                    "OOS_mean",
                    "win?",
                    "判定",
                ],
                srows,
            )
        )
    else:
        lines.append("（無滿足 IS 門檻的 score）")
    lines.append("")
    lines.append(
        f"網格大小：{len(score_df[score_df['hold']==PRIMARY_HOLD])} "
        "（wb,wc∈{0,1,2} · w_ts,w_cont∈{0,1} · thr∈{1,2,3}）"
    )
    lines.append("")

    lines.append("## H=6 敏感度（便宜加報）")
    lines.append("")
    h6_keep = summary_df[
        (summary_df["hold"] == 6)
        & (
            summary_df["arm"].isin(
                {
                    "Whale_T+1",
                    "B_only",
                    f"C_only__{c_id}",
                    f"B_OR_C__{c_id}",
                    f"B_AND_C__{c_id}",
                    *( [best_score_h6["arm"]] if best_score_h6 else [] ),
                }
            )
        )
    ]
    h6rows = []
    for _, r in h6_keep.iterrows():
        h6rows.append(
            [
                str(r["arm"]),
                str(int(r["full_n"])),
                fmt_pct(r["full_mean"]),
                fmt_pct(r["full_sum"], 0),
                fmt_pct(r["OOS_mean"]),
                str(r["verdict"]),
            ]
        )
    lines.append(
        md_table(
            ["arm", "full_n", "full_mean%", "full_sum%", "OOS_mean%", "判定"],
            h6rows,
        )
    )
    lines.append("")

    lines.append("## OR 組成拆解（日曆火）")
    lines.append("")
    lines.append(
        md_table(
            ["集合", "n_calendar"],
            [
                ["B only", str(len(b_fires))],
                ["C only", str(len(c_fires))],
                ["B ∩ C", str(len(both))],
                ["B ∪ C", str(len(or_fires))],
                ["C ∖ B（OR 增量）", str(len(only_c_extra))],
            ],
        )
    )
    lines.append("")
    lines.append(
        f"非重疊成交上，OR 相對 B 的**新增成交** mean={fmt_pct(extra_sm['mean'])} "
        f"（n={extra_sm['n']}）——"
        + (
            "偏雜訊、拉低 mean。"
            if dilution and (not pd.notna(extra_sm["mean"]) or extra_sm["mean"] < b_row["full_mean"])
            else "未明顯稀釋／或樣本過少。"
        )
    )
    lines.append("")
    lines.append("## 產物")
    lines.append("")
    lines.append(f"- `{SID}_CHIP_OR_SCORE.md`（本報告）")
    lines.append(f"- `{SID}_chip_or_score_arms.csv`")
    lines.append(f"- `{SID}_chip_or_score_grid.csv`")
    lines.append(f"- `{SID}_chip_or_score_trades.csv`")
    lines.append(f"- `{SID}_chip_or_score_panel.csv`")
    lines.append(f"- `{SID}_chip_or_score_C_is_freeze.csv`")
    lines.append(
        f"- `{SID}_chip_or_score_frozen.json`"
        + ("（有勝出）" if frozen else "（無）")
    )
    lines.append(f"- 耗時 ≈{time.time()-t0:.1f}s")
    lines.append("")

    report_path = OUT / f"{SID}_CHIP_OR_SCORE.md"
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log(f"wrote {report_path}")
    log(f"done in {time.time()-t0:.1f}s · overall={overall}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
