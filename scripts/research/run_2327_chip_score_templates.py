#!/usr/bin/env python3
"""2327 國巨 · Chip score templates A/B/C vs Whale_T+1 & frozen Branch B.

Template A — fixed prior × 60d rolling z-score (NO weight fit)
Template B — traffic-light W1–W6 count ≥ thr (calendar day D)
Template C — volume-norm 60d percentile equal-weight (optional fixed tilt)

Protocol: standalone calendar · enter D+1 open · H∈{6,7} · COST=0.003 · β=1.15
IS < 2026-01-01 · OOS ≥ 2026-01-01 · non-overlap · Research only · 未採納

    PYTHONPATH=src .venv/bin/python scripts/research/run_2327_chip_score_templates.py
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
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
Z_WIN = 60

# Template A — FIXED prior (do NOT fit on same window)
W_A = {
    "foreign_sum_5d": 0.30,
    "big_holder_pct_chg": 0.25,
    "three_sum_5d": 0.15,
    "trust_net": 0.15,
    "dealer_self_net": 0.05,
    "lending_signed": 0.05,  # −lending_change → drop = bullish
    "short_sum_5d": 0.05,
}

# Template C optional empirical tilt (FIXED · no grid)
TILT_C = {
    "r_foreign": 0.35,
    "r_trust": 0.15,
    "r_bh": 0.30,
    "r_three": 0.20,
}

# IS threshold candidates (threshold only · never weights)
THR_A_ABS = (0.0, 0.5, 1.0)
THR_A_PCT = (0.80, 0.90)
THR_B = (2, 3)
THR_C = (0.70, 0.80, 0.90)
# ¬B but template "very high" for OR-style secondary
THR_A_VERY = 1.5
THR_C_VERY = 0.90
THR_B_VERY = 4  # count ≥4


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


def lag_date(cal: list[str], di: dict[str, int], sig: str, k: int) -> str | None:
    i = di.get(sig)
    if i is None or i < k:
        return None
    return cal[i - k]


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


def enrich_feat(feat: pd.DataFrame, vol_map: dict[str, float]) -> pd.DataFrame:
    g = feat.sort_values(["sid", "d"]).copy()
    g["volume"] = g["d"].map(vol_map).astype(float)
    g["volume_5d"] = (
        g.groupby("sid")["volume"]
        .transform(lambda s: s.fillna(0).rolling(5, min_periods=1).sum())
        .clip(lower=1.0)
    )
    # lending_signed: drop (negative change) → positive score contribution
    g["lending_signed"] = -g["lending_change"].fillna(0.0)
    if "lending_drop" not in g.columns:
        g["lending_drop"] = (
            (g.get("lending_drop_streak", pd.Series(0, index=g.index)).fillna(0) >= 1)
            | (g.get("lending_change", pd.Series(0, index=g.index)).fillna(0) < 0)
        ).astype(int)
    # trust_sum_5d for Template C
    g["trust_sum_5d"] = (
        g.groupby("sid")["trust_net"]
        .transform(lambda s: s.fillna(0).rolling(5, min_periods=1).sum())
    )
    return g


def rolling_z(s: pd.Series, win: int = Z_WIN) -> pd.Series:
    mu = s.rolling(win, min_periods=max(20, win // 3)).mean()
    sd = s.rolling(win, min_periods=max(20, win // 3)).std(ddof=0)
    return (s - mu) / sd.replace(0, np.nan)


def rolling_pct(s: pd.Series, win: int = Z_WIN) -> pd.Series:
    return s.rolling(win, min_periods=max(20, win // 3)).rank(pct=True)


def add_scores(feat: pd.DataFrame) -> pd.DataFrame:
    g = feat.sort_values(["sid", "d"]).copy()
    # ---- Template A z-scores ----
    for col in W_A:
        src = col
        g[f"z_{col}"] = g.groupby("sid")[src].transform(rolling_z)
    score_a = np.zeros(len(g), dtype=float)
    for col, w in W_A.items():
        score_a = score_a + w * g[f"z_{col}"].fillna(0.0).to_numpy()
    surge = g["margin_surge"].fillna(False).astype(bool).to_numpy()
    g["score_A"] = np.where(surge, np.nan, score_a)  # hard veto → no fire
    g["margin_surge_i"] = surge.astype(int)

    # ---- Template B buckets (calendar D · FROZEN W1–W6 adapted) ----
    ts = g["three_streak"].fillna(0)
    fs = g["foreign_streak"].fillna(0)
    f5 = g["foreign_sum_5d"].fillna(0)
    t5 = g["three_sum_5d"].fillna(0)
    trust = g["trust_net"].fillna(0)
    bh = g["big_holder_pct_chg"]
    ld = g["lending_drop_streak"].fillna(0)
    g["W1"] = ((ts >= 2) | (fs >= 2)).astype(int)
    g["W2"] = ((f5 > 0) | (t5 > 0)).astype(int)
    g["W3"] = ((f5 > 0) & (ts >= 2)).astype(int)
    g["W4"] = (trust > 0).astype(int)
    g["W5"] = (bh.notna() & (bh > 0)).astype(int)
    g["W6"] = (ld >= 1).astype(int)
    score_b = g[["W1", "W2", "W3", "W4", "W5", "W6"]].sum(axis=1).astype(float)
    g["score_B"] = np.where(surge, 0.0, score_b)  # veto → 0

    # ---- Template C rank fusion ----
    g["f_vol"] = g["foreign_sum_5d"].fillna(0) / g["volume_5d"]
    g["t_vol"] = g["trust_sum_5d"].fillna(0) / g["volume_5d"]
    g["r_foreign"] = g.groupby("sid")["f_vol"].transform(rolling_pct)
    g["r_trust"] = g.groupby("sid")["t_vol"].transform(rolling_pct)
    g["r_bh"] = g.groupby("sid")["big_holder_pct_chg"].transform(rolling_pct)
    g["r_three"] = g.groupby("sid")["three_streak"].transform(rolling_pct)
    eq = (g["r_foreign"] + g["r_trust"] + g["r_bh"] + g["r_three"]) / 4.0
    tilt = (
        TILT_C["r_foreign"] * g["r_foreign"]
        + TILT_C["r_trust"] * g["r_trust"]
        + TILT_C["r_bh"] * g["r_bh"]
        + TILT_C["r_three"] * g["r_three"]
    )
    g["score_C"] = np.where(surge, np.nan, eq)
    g["score_C_tilt"] = np.where(surge, np.nan, tilt)
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


def win_criteria(
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


def verdict_vs(full: dict, ref: dict, *, min_n: int = 8) -> str:
    if full["n"] == 0 or not pd.notna(full.get("mean")) or not pd.notna(ref.get("mean")):
        return "更差"
    dm = float(full["mean"]) - float(ref["mean"])
    ds = float(full["sum"]) - float(ref["sum"]) if pd.notna(full.get("sum")) and pd.notna(ref.get("sum")) else 0.0
    if dm >= 1.0 and full["n"] >= min_n:
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


def is_pick_score(sm: dict) -> float:
    """IS freeze score: prefer positive mean + adequate n."""
    mean = sm["mean"] if pd.notna(sm["mean"]) else -999.0
    n = sm["n"]
    return mean + 0.05 * min(n, 30) + (0.0 if n >= MIN_IS_N else -5.0)


def arm_metrics(
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
        "full_delta_whale": full["delta_vs_whale"],
        "full_p_le0": full["p_le0"],
        "OOS_n": oos["n"],
        "OOS_hit": oos["hit_rate"],
        "OOS_mean": oos["mean"],
        "OOS_sum": oos["sum"],
        "OOS_avg_daily": oos["avg_daily"],
        "OOS_delta_whale": oos["delta_vs_whale"],
        "trades_per_year": full["trades_per_year"],
        "_full": full,
        "_is": ism,
        "_oos": oos,
    }
    if extra:
        row.update(extra)
    return row, trades


def main() -> int:
    global SID, SEAT
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sid", default="2327", help="stock id (default 2327)")
    ap.add_argument("--d1", default=D1)
    args = ap.parse_args()
    SID = str(args.sid)
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
    d1 = str(args.d1)
    years = (pd.Timestamp(d1) - pd.Timestamp(D0)).days / 365.25

    t0 = time.time()
    OUT.mkdir(parents=True, exist_ok=True)
    log(f"{SID} chip templates A/B/C · seat={SEAT} · db={DEFAULT_DB_PATH}")

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
    vol_map = {
        str(r.d): float(r.volume) if pd.notna(r.volume) else np.nan
        for r in ohlc.itertuples()
        if str(r.sid) == SID
    }

    hs = HS_PATH if HS_PATH.exists() else None
    gov = GOV_PATH if GOV_PATH.exists() else None
    log("chip features + scores")
    feat = build_chip_feature_frame(
        conn, [SID], D0, d1, holding_shares_path=hs, gov_bank_path=gov
    )
    feat = enrich_feat(feat, vol_map)
    feat = add_scores(feat)
    feat_idx = feat.set_index(["sid", "d"])

    log(f"branch tape ({SEAT}) for frozen B")
    tape = fetch_tape(conn, cal)
    conn.close()
    tape = attach_amt(tape, px_close)
    tape_seat = tape[tape["tid"] == SEAT].copy()
    branch_amt: dict[str, float] = {}
    for r in tape_seat.itertuples():
        branch_amt[str(r.d)] = float(r.amt) if pd.notna(r.amt) else 0.0

    # ---- Panel: one row per calendar day with valid primary excess ----
    rows: list[dict] = []
    for d in cal:
        dm1 = lag_date(cal_ext, di, d, 1)
        a0 = float(branch_amt.get(d, 0.0) or 0.0)
        a1 = float(branch_amt.get(dm1, 0.0) or 0.0) if dm1 else 0.0
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
            for col in (
                "score_A",
                "score_B",
                "score_C",
                "score_C_tilt",
                "margin_surge_i",
                "W1",
                "W2",
                "W3",
                "W4",
                "W5",
                "W6",
            ):
                v = fr.get(col, np.nan)
                row[col] = float(v) if pd.notna(v) else np.nan
        except KeyError:
            for col in (
                "score_A",
                "score_B",
                "score_C",
                "score_C_tilt",
                "margin_surge_i",
                "W1",
                "W2",
                "W3",
                "W4",
                "W5",
                "W6",
            ):
                row[col] = np.nan
        rows.append(row)
    panel = pd.DataFrame(rows)
    panel = panel.loc[panel[f"ex_h{PRIMARY_HOLD}"].notna()].copy()
    is_mask = panel["split"] == "IS"
    log(f"panel n={len(panel)} IS={int(is_mask.sum())} OOS={int((~is_mask).sum())}")

    # ---- Frozen B: 8840 accel ∧ amt ≥ IS p90 ----
    a0 = panel["amt_d"]
    a1 = panel["amt_dm1"]
    accel = (a1 > 0) & (a0 > a1)
    pos_is = a0[is_mask & (a0 > 0)]
    thr90 = float(pos_is.quantile(0.90)) if len(pos_is) >= 30 else 1e8
    b_mask = accel & (a0 >= thr90)
    b_fires = set(panel.loc[b_mask, "d"].astype(str))
    panel["B"] = b_mask.astype(int)
    log(
        f"B=8840 accel∧p90 thr={thr90/1e8:.3f}億 · calendar fires={len(b_fires)} "
        f"(IS={int((b_mask & is_mask).sum())})"
    )

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

    # ---- IS threshold sweeps (weights FIXED) ----
    thr_sweep_rows: list[dict] = []
    frozen_thr: dict[str, dict] = {}

    def eval_thr(
        family: str,
        thr_id: str,
        thr_label: str,
        fire_mask: pd.Series,
        score_col: str,
    ) -> None:
        fires = set(panel.loc[fire_mask, "d"].astype(str))
        trades = standalone_trades(
            fires, PRIMARY_HOLD, cal_ext, di, ohlc_px, bench, thr_id, thr_id, D0, d1
        )
        is_xs = [t["excess_pct"] for t in trades if t["split"] == "IS"]
        sm = summarize(is_xs)
        cal_is = int((fire_mask & is_mask).sum())
        row = {
            "family": family,
            "thr_id": thr_id,
            "thr_label": thr_label,
            "score_col": score_col,
            "IS_n": sm["n"],
            "IS_mean": sm["mean"],
            "IS_hit": sm["hit_rate"],
            "IS_sum": sm["sum"],
            "n_calendar": len(fires),
            "n_calendar_IS": cal_is,
            "_score": is_pick_score(sm),
            "_fires": fires,
            "_mask": fire_mask,
        }
        thr_sweep_rows.append(row)
        log(
            f"  thr {family}/{thr_id}: IS n={sm['n']} mean={fmt_pct(sm['mean'])} "
            f"cal={len(fires)}"
        )

    # A absolute
    sa = panel["score_A"]
    for thr in THR_A_ABS:
        tid = f"A_gt{thr}"
        eval_thr("A", tid, f"score_A>{thr} ∧ ¬surge", sa.notna() & (sa > thr), "score_A")
    # A IS percentile
    sa_is = sa[is_mask & sa.notna()]
    for p in THR_A_PCT:
        qv = float(sa_is.quantile(p)) if len(sa_is) >= 30 else float("inf")
        tid = f"A_p{int(p*100)}"
        eval_thr(
            "A",
            tid,
            f"score_A≥IS-p{int(p*100)}({qv:.2f}) ∧ ¬surge",
            sa.notna() & (sa >= qv),
            "score_A",
        )

    # Template-B (traffic-light) count — family id TplB to avoid Branch-B clash
    sb = panel["score_B"].fillna(0)
    for thr in THR_B:
        tid = f"TplB_ge{thr}"
        eval_thr(
            "TplB",
            tid,
            f"W-count≥{thr} ∧ ¬surge",
            (sb >= thr) & (panel["margin_surge_i"].fillna(0) < 1),
            "score_B",
        )

    # C equal-weight
    sc = panel["score_C"]
    for thr in THR_C:
        tid = f"C_ge{thr}"
        eval_thr(
            "C",
            tid,
            f"score_C≥{thr} ∧ ¬surge",
            sc.notna() & (sc >= thr),
            "score_C",
        )
    # C tilt (secondary · same thr grid)
    sct = panel["score_C_tilt"]
    for thr in THR_C:
        tid = f"Ctilt_ge{thr}"
        eval_thr(
            "C_tilt",
            tid,
            f"score_C_tilt≥{thr} ∧ ¬surge",
            sct.notna() & (sct >= thr),
            "score_C_tilt",
        )

    thr_df = pd.DataFrame(
        [{k: v for k, v in r.items() if not k.startswith("_")} for r in thr_sweep_rows]
    )
    # Freeze best thr per family on IS
    for fam in ("A", "TplB", "C", "C_tilt"):
        cands = [r for r in thr_sweep_rows if r["family"] == fam]
        if not cands:
            continue
        best = max(cands, key=lambda r: r["_score"])
        frozen_thr[fam] = best
        log(
            f"IS-frozen {fam}: {best['thr_id']} "
            f"IS n={best['IS_n']} mean={fmt_pct(best['IS_mean'])}"
        )

    # ---- Build all arms for PRIMARY + H=6 ----
    all_arm_rows: list[dict] = []
    all_trades: list[dict] = []

    def push_arm(
        arm: str,
        label: str,
        family: str,
        fires: set[str],
        hold: int,
        extra: dict | None = None,
    ) -> dict:
        row, trades = arm_metrics(
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
            whale_xs_by_h[hold],
            n_cal,
            years,
            extra,
        )
        # attach vs B / win
        b_row_hold = next(
            (r for r in all_arm_rows if r["arm"] == "B_only" and r["hold"] == hold),
            None,
        )
        whale_f = summarize(whale_xs_by_h[hold]["full"])
        whale_o = summarize(whale_xs_by_h[hold]["OOS"])
        win, reason = win_criteria(
            row["_full"], row["_oos"], row["_is"], whale_f, whale_o
        )
        row["win"] = bool(win)
        row["win_reason"] = reason
        row["verdict_vs_whale"] = verdict_vs(row["_full"], whale_f)
        if b_row_hold is not None:
            row["verdict_vs_B"] = verdict_vs(row["_full"], b_row_hold["_full"])
            row["delta_vs_B_mean"] = float(row["_full"]["mean"]) - float(
                b_row_hold["_full"]["mean"]
            )
        else:
            row["verdict_vs_B"] = "—"
            row["delta_vs_B_mean"] = np.nan
        # strip internal
        out = {k: v for k, v in row.items() if not k.startswith("_")}
        all_arm_rows.append(row)  # keep with _ for later B refs
        all_trades.extend(trades)
        return out

    # Benchmarks first (so B exists for delta)
    for hold in HOLDS:
        # Whale as arm row
        w_fires = set(greens)
        # whale trades already computed; rebuild via arm for consistency of non-overlap?
        # Whale uses greens as signal dates — keep event-arm (may overlap differently).
        # Report whale from precomputed xs via fake arm metrics on greens set
        # BUT non-overlap may differ from event-arm. Prior scripts use event arm
        # without non-overlap filter on whale. Keep same: iterate greens sequentially
        # with non-overlap for fair calendar comparison? OR_SCORE uses sequential greens
        # without next_free skip across overlapping — actually excess_l1 per green independently.
        # Match OR_SCORE: whale trades listed independently (allow overlapping holds).
        w_trades = [t for t in whale_trades if t["hold"] == hold]
        full = metrics_block(w_trades, whale_xs_by_h[hold]["full"], "full", n_cal, years)
        ism = metrics_block(w_trades, whale_xs_by_h[hold]["IS"], "IS", n_cal, years)
        oos = metrics_block(w_trades, whale_xs_by_h[hold]["OOS"], "OOS", n_cal, years)
        wrow = {
            "arm": "Whale_T+1",
            "label": "Whale greens → T+1 open",
            "family": "benchmark",
            "hold": hold,
            "n_calendar": len(greens),
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
            "full_delta_whale": 0.0,
            "full_p_le0": np.nan,
            "OOS_n": oos["n"],
            "OOS_hit": oos["hit_rate"],
            "OOS_mean": oos["mean"],
            "OOS_sum": oos["sum"],
            "OOS_avg_daily": oos["avg_daily"],
            "OOS_delta_whale": 0.0,
            "trades_per_year": full["trades_per_year"],
            "win": False,
            "win_reason": "",
            "verdict_vs_whale": "基準",
            "verdict_vs_B": "—",
            "delta_vs_B_mean": np.nan,
            "_full": full,
            "_is": ism,
            "_oos": oos,
        }
        all_arm_rows.append(wrow)
        all_trades.extend(w_trades)

        push_arm(
            "B_only",
            f"8840 accel∧IS-p90({thr90/1e8:.2f}億)",
            "benchmark",
            b_fires,
            hold,
        )

    # Template arms + B-gate combos for each frozen family
    for fam, best in frozen_thr.items():
        fires_t = best["_fires"]
        thr_id = best["thr_id"]
        thr_label = best["thr_label"]
        mask_t = best["_mask"]
        # very-high mask for secondary OR
        if fam == "A":
            vh = sa.notna() & (sa >= THR_A_VERY)
            vh_lab = f"score_A≥{THR_A_VERY}"
        elif fam == "TplB":
            vh = (sb >= THR_B_VERY) & (panel["margin_surge_i"].fillna(0) < 1)
            vh_lab = f"W-count≥{THR_B_VERY}"
        elif fam == "C":
            vh = sc.notna() & (sc >= THR_C_VERY)
            vh_lab = f"score_C≥{THR_C_VERY}"
        else:  # C_tilt
            vh = sct.notna() & (sct >= THR_C_VERY)
            vh_lab = f"score_C_tilt≥{THR_C_VERY}"
        fires_vh = set(panel.loc[vh, "d"].astype(str))
        fam_disp = {"A": "TplA", "TplB": "TplB", "C": "TplC", "C_tilt": "TplC_tilt"}[fam]

        for hold in HOLDS:
            # (1) template alone
            push_arm(
                f"{fam}_alone__{thr_id}",
                f"{fam_disp} alone · {thr_label}",
                fam,
                fires_t,
                hold,
                {"thr_id": thr_id, "thr_label": thr_label},
            )
            # (2) BranchB ∨ template (expect dilute)
            push_arm(
                f"BrB_OR_{fam}__{thr_id}",
                f"BranchB ∨ {fam_disp}({thr_id})",
                f"gate_{fam}",
                b_fires | fires_t,
                hold,
                {"thr_id": thr_id},
            )
            # (3) BranchB ∧ template
            push_arm(
                f"BrB_AND_{fam}__{thr_id}",
                f"BranchB ∧ {fam_disp}({thr_id})",
                f"gate_{fam}",
                b_fires & fires_t,
                hold,
                {"thr_id": thr_id},
            )
            # (4) already have B_only
            # secondary: BranchB ∨ (¬BranchB ∧ very-high template)
            fires_or_vh = b_fires | (fires_vh - b_fires)
            push_arm(
                f"BrB_OR_{fam}_very__{thr_id}",
                f"BranchB ∨ (¬BrB∧{vh_lab})",
                f"gate_{fam}",
                fires_or_vh,
                hold,
                {"thr_id": thr_id, "very_label": vh_lab},
            )

    # Also report all thr candidates as arms (primary hold only) for CSV completeness
    for r in thr_sweep_rows:
        for hold in (PRIMARY_HOLD,):
            push_arm(
                f"thr_{r['thr_id']}",
                r["thr_label"],
                f"thr_{r['family']}",
                r["_fires"],
                hold,
                {"thr_id": r["thr_id"], "thr_label": r["thr_label"]},
            )

    # ---- Persist ----
    arms_export = []
    for r in all_arm_rows:
        arms_export.append({k: v for k, v in r.items() if not k.startswith("_")})
    arms_df = pd.DataFrame(arms_export)
    trades_df = pd.DataFrame(all_trades)
    thr_path = OUT / f"{SID}_chip_templates_thr_sweep.csv"
    arms_path = OUT / f"{SID}_chip_templates_arms.csv"
    trades_path = OUT / f"{SID}_chip_templates_trades.csv"
    panel_path = OUT / f"{SID}_chip_templates_panel.csv"
    thr_df.to_csv(thr_path, index=False)
    arms_df.to_csv(arms_path, index=False)
    trades_df.to_csv(trades_path, index=False)
    panel.to_csv(panel_path, index=False)

    # ---- Report ----
    # Primary comparison table
    primary = arms_df[
        (arms_df["hold"] == PRIMARY_HOLD)
        & (
            arms_df["family"].isin(
                [
                    "benchmark",
                    "A",
                    "TplB",
                    "C",
                    "C_tilt",
                    "gate_A",
                    "gate_TplB",
                    "gate_C",
                    "gate_C_tilt",
                ]
            )
            | arms_df["arm"].isin(["Whale_T+1", "B_only"])
        )
        & (~arms_df["arm"].astype(str).str.startswith("thr_"))
    ].copy()
    # Dedup by arm
    primary = primary.drop_duplicates(subset=["arm"], keep="first")

    b_full = next(r for r in all_arm_rows if r["arm"] == "B_only" and r["hold"] == PRIMARY_HOLD)
    w_full = next(r for r in all_arm_rows if r["arm"] == "Whale_T+1" and r["hold"] == PRIMARY_HOLD)

    def row_line(r: pd.Series) -> list[str]:
        return [
            str(r["arm"]),
            str(r["label"])[:48],
            str(int(r["full_n"])) if pd.notna(r["full_n"]) else "—",
            fmt_rate(r["full_hit"]),
            fmt_pct(r["full_mean"]),
            fmt_pct(r["full_sum"], 0),
            fmt_pct(r["OOS_mean"]),
            str(r.get("verdict_vs_B", "—")),
            str(r.get("verdict_vs_whale", "—")),
            "Y" if r.get("win") else "",
        ]

    # Verdict synthesis
    alone_arms = [
        r
        for r in all_arm_rows
        if r["hold"] == PRIMARY_HOLD
        and r["family"] in ("A", "TplB", "C", "C_tilt")
        and "_alone__" in r["arm"]
    ]
    fam_zh = {
        "A": "A（線性 z）",
        "TplB": "B（紅黃綠）",
        "C": "C（rank）",
        "C_tilt": "C_tilt（固定 tilt）",
    }

    # Honest Chinese verdicts
    verdicts: list[str] = []
    for fam in ("A", "TplB", "C", "C_tilt"):
        matches = [r for r in alone_arms if r["family"] == fam]
        if not matches:
            continue
        r = matches[0]
        vb = verdict_vs(r["_full"], b_full["_full"])
        vw = verdict_vs(r["_full"], w_full["_full"])
        verdicts.append(
            f"- **Template {fam_zh[fam]} alone**（{r.get('thr_id', r['arm'])}）："
            f"full n={r['full_n']} mean={fmt_pct(r['full_mean'])} "
            f"sum={fmt_pct(r['full_sum'], 0)} · vs 凍結BranchB：**{vb}** · vs Whale：**{vw}**"
        )

    # Gate notes
    gate_notes: list[str] = []
    for fam in ("A", "TplB", "C"):
        ors = [
            r
            for r in all_arm_rows
            if r["hold"] == PRIMARY_HOLD
            and r["arm"].startswith(f"BrB_OR_{fam}__")
            and "very" not in r["arm"]
        ]
        ands = [
            r
            for r in all_arm_rows
            if r["hold"] == PRIMARY_HOLD and r["arm"].startswith(f"BrB_AND_{fam}__")
        ]
        verys = [
            r
            for r in all_arm_rows
            if r["hold"] == PRIMARY_HOLD and r["arm"].startswith(f"BrB_OR_{fam}_very__")
        ]
        disp = fam_zh.get(fam, fam)
        if ors:
            r = ors[0]
            gate_notes.append(
                f"- BranchB∨Tpl{disp[0]}: n={r['full_n']} mean={fmt_pct(r['full_mean'])} "
                f"vs BranchB={r.get('verdict_vs_B')}（預期稀釋）"
            )
        if ands:
            r = ands[0]
            gate_notes.append(
                f"- BranchB∧Tpl{disp[0]}: n={r['full_n']} mean={fmt_pct(r['full_mean'])} "
                f"vs BranchB={r.get('verdict_vs_B')}（稀疏）"
            )
        if verys:
            r = verys[0]
            gate_notes.append(
                f"- BranchB∨(¬BrB∧{r.get('very_label', 'very-high')}): "
                f"n={r['full_n']} mean={fmt_pct(r['full_mean'])} "
                f"vs BranchB={r.get('verdict_vs_B')} · vs Whale={r.get('verdict_vs_whale')}"
            )

    # Overall Chinese
    improve_vs_b = [
        r
        for r in alone_arms
        if verdict_vs(r["_full"], b_full["_full"]) == "更好"
    ]
    improve_vs_w = [
        r
        for r in alone_arms
        if verdict_vs(r["_full"], w_full["_full"]) == "更好"
    ]
    very_arms = [
        r
        for r in all_arm_rows
        if r["hold"] == PRIMARY_HOLD and "_very__" in r["arm"]
    ]
    best_very = None
    if very_arms:
        best_very = max(
            very_arms,
            key=lambda r: (
                float(r["_full"]["mean"]) if pd.notna(r["_full"]["mean"]) else -999,
                r["_full"]["n"],
            ),
        )
    if improve_vs_w:
        overall = (
            f"**總評：Template {fam_zh[improve_vs_w[0]['family']]} alone 相對 Whale 更好**"
            f"（仍需看 n／IS；權重未擬合）"
        )
    elif improve_vs_b:
        overall = (
            f"**總評：Template {fam_zh[improve_vs_b[0]['family']]} alone 相對凍結 BranchB 更好，"
            f"但相對 Whale 未勝出 → 誠實判定「差不多／更差於 Whale」**"
        )
    else:
        overall = (
            "**總評：Template A／B／C alone 皆未能打贏凍結 BranchB（8840 accel≥p90）與 Whale_T+1**"
            " → 籌碼分數再給一次機會仍屬**更差**；延續 BRIEFING：分點主導、籌碼當黃燈。"
        )
        if best_very is not None:
            vb = best_very.get("verdict_vs_B", "—")
            vw = best_very.get("verdict_vs_whale", "—")
            overall += (
                f" 唯一次要亮點是 **{best_very['label']}**"
                f"（n={best_very['full_n']} mean={fmt_pct(best_very['full_mean'])} · "
                f"vs BranchB：**{vb}** · vs Whale：**{vw}**）"
                "——本質仍是分點閘＋極高籌碼加分，**不是**模板 alone 可取代分點。"
            )

    # W definition block
    w_def = md_table(
        ["桶", "日曆 D 條件（0/1）", "對齊"],
        [
            ["W1", "`three_streak≥2` ∨ `foreign_streak≥2`", "FROZEN W1a/W1b"],
            ["W2", "`foreign_sum_5d>0` ∨ `three_sum_5d>0`", "FROZEN W2"],
            ["W3", "`foreign_sum_5d>0` ∧ `three_streak≥2`", "FROZEN W3"],
            ["W4", "`trust_net>0`", "FROZEN W4"],
            ["W5", "`big_holder_pct_chg>0`", "FROZEN W5"],
            ["W6", "`lending_drop_streak≥1`", "FROZEN W6"],
            ["否決", "`margin_surge` → score_B=0", "Chen"],
        ],
    )
    wa_def = md_table(
        ["因子", "權重", "z 來源"],
        [
            ["foreign_sum_5d", "0.30", "60d rolling z"],
            ["big_holder_pct_chg", "0.25", "60d rolling z"],
            ["three_sum_5d", "0.15", "60d rolling z"],
            ["trust_net", "0.15", "60d rolling z"],
            ["dealer_self_net", "0.05", "60d rolling z"],
            ["−lending_change", "0.05", "60d rolling z"],
            ["short_sum_5d", "0.05", "60d rolling z"],
            ["margin_surge", "硬否決", "分數 NaN → 不開火"],
        ],
    )

    # Thr sweep table
    thr_md_rows = []
    for r in sorted(thr_sweep_rows, key=lambda x: (x["family"], -x["_score"])):
        mark = ""
        fr = frozen_thr.get(r["family"])
        if fr and fr["thr_id"] == r["thr_id"]:
            mark = "← IS凍結"
        thr_md_rows.append(
            [
                r["family"],
                r["thr_id"],
                r["thr_label"][:40],
                str(r["IS_n"]),
                fmt_pct(r["IS_mean"]),
                fmt_rate(r["IS_hit"]),
                str(r["n_calendar"]),
                mark,
            ]
        )

    primary_rows = [row_line(r) for _, r in primary.iterrows()]

    # H6 sensitivity for key arms
    h6 = arms_df[
        (arms_df["hold"] == 6)
        & (
            arms_df["arm"].isin(["Whale_T+1", "B_only"])
            | arms_df["arm"].astype(str).str.contains("_alone__")
        )
    ].drop_duplicates(subset=["arm"])
    h6_rows = [
        [
            str(r["arm"])[:40],
            str(int(r["full_n"])),
            fmt_pct(r["full_mean"]),
            fmt_pct(r["full_sum"], 0),
            fmt_pct(r["OOS_mean"]),
            str(r.get("verdict_vs_whale", "—")),
        ]
        for _, r in h6.iterrows()
    ]

    status = {
        "sid": SID,
        "generated": datetime.now().isoformat(timespec="seconds"),
        "d0": D0,
        "d1": d1,
        "oos": OOS,
        "cost": COST,
        "beta": BETA,
        "primary_hold": PRIMARY_HOLD,
        "b_thr90": thr90,
        "frozen_thr": {
            k: {
                "thr_id": v["thr_id"],
                "thr_label": v["thr_label"],
                "IS_n": v["IS_n"],
                "IS_mean": v["IS_mean"],
            }
            for k, v in frozen_thr.items()
        },
        "weights_A": W_A,
        "tilt_C": TILT_C,
        "whale_full_mean": whale_full["mean"],
        "b_full_mean": b_full["full_mean"],
        "elapsed_sec": round(time.time() - t0, 1),
    }
    (OUT / f"status_{SID}_chip_templates.json").write_text(
        json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    md = f"""# 2327 國巨 · Chip Score Templates A / B / C

- 生成：{datetime.now().isoformat(timespec="seconds")}
- Runner：`scripts/research/run_2327_chip_score_templates.py`
- 窗：{D0}..{d1}（≈{years:.2f}y）· OOS≥{OOS} · COST={COST} · β={BETA}×IX0001
- 日曆：**standalone**（非 Whale 子集）· 進場 D+1 open · 非重疊 · 主 H={PRIMARY_HOLD}（兼報 H=6）
- **基準**：Whale_T+1 · 凍結 Branch B = `8840 accel∧IS-p90`（thr≈{thr90/1e8:.2f}億）
- 權重：**固定先驗／等權** · **禁止同窗擬合 w** · 僅 IS 掃門檻
- Research only · **未採納**進 Strategy／Order
- 可行性：Doable as repeatable template test · NOT a promise to beat Whale

## 結論（先讀）

{overall}

{chr(10).join(verdicts)}

### B-gate 次要臂

{chr(10).join(gate_notes) if gate_notes else "- （無）"}

### 勝出門檻（與序列／OR 研究相同）

1. OOS mean ≥ Whale OOS **且** OOS n≥5 **且** IS n≥8 **且** IS mean≥0
2. Full mean ≥ Whale full **且** bootstrap P(Δ≤0)≤0.15 **且** n≥12（IS mean≥0）
3. Full sum 明顯更高（>+10%）**且** mean 不差過 2pp（n≥12 · IS mean≥0）

---

## Template 定義

### A — 線性 z-score（固定先驗）

{wa_def}

`score_A = Σ w·z` · fire = score≥thr ∧ ¬margin_surge

### B — 紅黃綠加總（日曆 D）

{w_def}

`score_B = W1+…+W6` · fire = score≥thr（IS 掃 2/3）∧ ¬margin_surge

### C — rank 融合（成交量正規化 · 60d pct）

| 因子 | 定義 |
|------|------|
| r_foreign | pct_rank_60d(foreign_sum_5d / volume_5d) |
| r_trust | pct_rank_60d(trust_sum_5d / volume_5d) |
| r_bh | pct_rank_60d(big_holder_pct_chg) |
| r_three | pct_rank_60d(three_streak) |
| score_C | 等權平均 |
| score_C_tilt | 固定 tilt {TILT_C}（不掃 w） |

---

## IS 門檻掃（只選 thr · 不擬合權重）

{md_table(["family", "thr_id", "label", "IS_n", "IS_mean%", "IS_hit", "cal", ""], thr_md_rows)}

---

## 主臂對照（H={PRIMARY_HOLD}）

基準：Whale full n={whale_full["n"]} mean={fmt_pct(whale_full["mean"])} sum={fmt_pct(whale_full["sum"], 0)} ·
B only full n={b_full["full_n"]} mean={fmt_pct(b_full["full_mean"])} sum={fmt_pct(b_full["full_sum"], 0)}

{md_table(
    ["arm", "label", "n", "hit", "mean%", "sum%", "OOS_mean%", "vsB", "vsWhale", "win"],
    primary_rows,
)}

---

## H=6 敏感度（alone + 基準）

{md_table(["arm", "n", "mean%", "sum%", "OOS_mean%", "vsWhale"], h6_rows)}

---

## 產物

| 檔 | 路徑 |
|----|------|
| 本報告 | `{SID}_CHIP_TEMPLATES_ABC.md` |
| 門檻掃 | `{SID}_chip_templates_thr_sweep.csv` |
| 臂績效 | `{SID}_chip_templates_arms.csv` |
| 成交 | `{SID}_chip_templates_trades.csv` |
| Panel | `{SID}_chip_templates_panel.csv` |
| 狀態 | `status_{SID}_chip_templates.json` |

## 誠實結語（中文）

模板 A／B／C 是 `CHIP_WEIGHT_INSPIRATION.md` 的**民俗＋本倉 lift 先驗**，本輪只在 IS 選門檻、**沒有**擬合權重。
若 alone 打不贏凍結 B（8840 accel≥p90）與 Whale_T+1，應記為 **更差／差不多**，不要把教程式 0.5/0.3/0.2 或紅黃綠≥2 當成可採納進場。
B∨template 預期稀釋；B∧template 常過稀。籌碼分數較適合作**黃燈／加分**，不是國巨獨立開倉主策略。

elapsed={status["elapsed_sec"]}s
"""
    md_path = OUT / f"{SID}_CHIP_TEMPLATES_ABC.md"
    md_path.write_text(md, encoding="utf-8")
    log(f"wrote {md_path}")
    log(f"done in {time.time()-t0:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
