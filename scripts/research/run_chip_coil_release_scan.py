#!/usr/bin/env python3
"""Track F-α · Chip Coil (蓄能) → Burst Lift scan.

Plan SSOT: reports/research/chip-overlays/RESEARCH_PLANS.md · Track F
Topic: chip-overlay-coil-release

CoilState @ t uses only dates < t (PriceQuiet + ChipCoil on lag window).
Burst in future H days is outcome only.

Usage:
  .venv/bin/python scripts/research/run_chip_coil_release_scan.py
  .venv/bin/python scripts/research/run_chip_coil_release_scan.py --universe holdings
  .venv/bin/python scripts/research/run_chip_coil_release_scan.py --universe expert
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
DB = ROOT / "data" / "stocks.db"
OUT = ROOT / "reports" / "research" / "chip-overlays" / "coil_release"
REGISTRY = (
    ROOT
    / "reports"
    / "research"
    / "branch-footprint-screen"
    / "expert_pool"
    / "EVAL_REGISTRY.json"
)

HOLDINGS = {
    "2327": "國巨",
    "3189": "景碩",
    "3653": "健策",
    "6505": "台塑化",
    "8046": "南電",
    "2492": "華新科",
}

START, WARMUP, END = "2024-07-01", "2024-04-01", "2026-07-24"
OOS = "2026-01-01"
BURST_PCT = 3.0
H_LIST = (3, 5, 10)
W_LIST = (5, 10, 20)
Q_LIST = (0.7, 0.8)
THETA_LIST = (0.03, 0.05)  # |R_W| quiet


def adopted_ids() -> list[str]:
    raw = json.loads(REGISTRY.read_text(encoding="utf-8"))
    return sorted(
        {
            r["stock_id"]
            for r in raw.get("stocks", [])
            if r.get("status") == "adopted" and str(r.get("stock_id", "")).isdigit()
        }
    )


def load_panel(sids: list[str]) -> pd.DataFrame:
    conn = sqlite3.connect(f"file:{DB.resolve()}?mode=ro", uri=True)
    ph = ",".join("?" * len(sids))
    bars = pd.read_sql_query(
        f"""
        SELECT stock_id, trade_date, open, high, low, close, volume, amount
        FROM stock_daily_bars
        WHERE source='finmind' AND stock_id IN ({ph})
          AND trade_date>=? AND trade_date<=?
        """,
        conn,
        params=[*sids, WARMUP, END],
    )
    inst = pd.read_sql_query(
        f"""
        SELECT stock_id, trade_date, foreign_net, investment_trust_net
        FROM stock_institutional_daily
        WHERE source='finmind' AND stock_id IN ({ph})
          AND trade_date>=? AND trade_date<=?
        """,
        conn,
        params=[*sids, WARMUP, END],
    )
    mcap = pd.read_sql_query(
        f"""
        SELECT stock_id, trade_date, market_value_ntd AS market_value
        FROM stock_market_value_daily
        WHERE stock_id IN ({ph}) AND trade_date>=? AND trade_date<=?
        """,
        conn,
        params=[*sids, WARMUP, END],
    )
    conn.close()
    for d in (bars, inst, mcap):
        d["trade_date"] = pd.to_datetime(d["trade_date"])
    df = bars.merge(inst, on=["stock_id", "trade_date"], how="inner")
    df = df.merge(mcap, on=["stock_id", "trade_date"], how="left")
    df = df.sort_values(["stock_id", "trade_date"]).reset_index(drop=True)
    df["F"] = df["foreign_net"].astype(float)
    df["IT"] = df["investment_trust_net"].astype(float)
    df["FT"] = df["F"] + df["IT"]
    # dollar ADV fallback when mcap missing
    dv = df["amount"].astype(float)
    miss = dv.isna() | (dv <= 0)
    dv = dv.where(~miss, df["close"].astype(float) * df["volume"].astype(float))
    df["dv"] = dv
    df["adv20"] = (
        df.groupby("stock_id", sort=False)["dv"]
        .transform(lambda s: s.shift(1).rolling(20, min_periods=10).mean())
    )
    # flow intensity: prefer mcap, else /ADV
    px = df["close"].astype(float)
    mv = df["market_value"].astype(float)
    x_mcap = (df["FT"] * px) / mv.replace(0, np.nan)
    x_adv = (df["FT"] * px) / df["adv20"].replace(0, np.nan)
    df["x"] = x_mcap.where(mv.notna() & (mv > 0), x_adv)
    df["ret"] = df.groupby("stock_id", sort=False)["close"].pct_change()
    return df


def enrich_forward(df: pd.DataFrame) -> pd.DataFrame:
    parts: list[pd.DataFrame] = []
    for _, g in df.groupby("stock_id", sort=False):
        g = g.copy()
        # max future daily ret over H (exclusive of today; starts tomorrow)
        fut = g["ret"].shift(-1)
        for h in H_LIST:
            # max of next h daily returns
            mx = fut.copy()
            for k in range(2, h + 1):
                mx = np.maximum(mx, g["ret"].shift(-k))
            g[f"maxret_h{h}"] = mx
            g[f"burst_h{h}"] = mx >= (BURST_PCT / 100.0)
            # cumulative H-day close-to-close from t close
            g[f"cum_h{h}"] = g["close"].shift(-h) / g["close"] - 1.0
        parts.append(g)
    return pd.concat(parts, ignore_index=True)


def add_coil_flags(
    df: pd.DataFrame, w: int, q: float, theta: float
) -> pd.DataFrame:
    parts: list[pd.DataFrame] = []
    for _, g in df.groupby("stock_id", sort=False):
        g = g.copy()
        # ChipCoil: sum of past W days of x (exclude today) high vs past 120d
        x_lag = g["x"].shift(1)
        coil_sum = x_lag.rolling(w, min_periods=max(3, w // 2)).sum()
        # rank of coil_sum vs its own past 120 (past only)
        past = coil_sum.shift(1)
        thr = past.rolling(120, min_periods=40).quantile(q)
        chip_coil = coil_sum >= thr

        # PriceQuiet: |P_{t-1}/P_{t-1-w}-1| <= theta (window ending yesterday)
        p_end = g["close"].shift(1)
        p_start = g["close"].shift(1 + w)
        abs_rw = (p_end / p_start - 1.0).abs()
        quiet = abs_rw <= theta

        g["chip_coil"] = chip_coil.fillna(False)
        g["price_quiet"] = quiet.fillna(False)
        g["coil_state"] = g["chip_coil"] & g["price_quiet"]
        # ablations / contamination
        g["coil_only"] = g["chip_coil"]
        g["quiet_only"] = g["price_quiet"]
        # contamination: chip coil + NOT quiet (buying into move)
        g["coil_loud"] = g["chip_coil"] & ~g["price_quiet"]
        parts.append(g)
    out = pd.concat(parts, ignore_index=True)
    return out


def lift_table(
    df: pd.DataFrame,
    flag_col: str,
    split: str,
    min_n: int = 15,
) -> list[dict]:
    rows: list[dict] = []
    sub = df[df["trade_date"] >= START].copy()
    if split == "OOS":
        sub = sub[sub["trade_date"] >= OOS]
    elif split == "IS":
        sub = sub[sub["trade_date"] < OOS]

    for sid, g in sub.groupby("stock_id", sort=False):
        base = {h: float(g[f"burst_h{h}"].mean()) for h in H_LIST}
        m = g[flag_col].fillna(False)
        n = int(m.sum())
        if n < min_n:
            continue
        ev = g.loc[m]
        for h in H_LIST:
            p = float(ev[f"burst_h{h}"].mean())
            b = base[h]
            lift = p / b if b and b > 0 else np.nan
            false_coil = float((~ev[f"burst_h{h}"] & (ev[f"cum_h{h}"] <= 0)).mean())
            # time to first burst day among events that burst
            ttbs = []
            rets = g["ret"].to_numpy()
            idx = np.flatnonzero(m.to_numpy())
            for i in idx:
                hit = None
                for k in range(1, h + 1):
                    j = i + k
                    if j >= len(rets):
                        break
                    if rets[j] >= BURST_PCT / 100.0:
                        hit = k
                        break
                if hit is not None:
                    ttbs.append(hit)
            rows.append(
                {
                    "stock_id": sid,
                    "split": split,
                    "flag": flag_col,
                    "n_coil": n,
                    "h": h,
                    "p_burst": p,
                    "p_base": b,
                    "lift": lift,
                    "false_coil": false_coil,
                    "ttb_med": float(np.median(ttbs)) if ttbs else np.nan,
                    "n_burst": int(ev[f"burst_h{h}"].sum()),
                }
            )
    return rows


def scan_universe(sids: list[str], label: str) -> pd.DataFrame:
    panel = load_panel(sids)
    panel = enrich_forward(panel)
    all_rows: list[dict] = []
    grid_meta: list[dict] = []
    for w in W_LIST:
        for q in Q_LIST:
            for theta in THETA_LIST:
                tagged = add_coil_flags(panel, w, q, theta)
                for split in ("ALL", "IS", "OOS"):
                    for flag in (
                        "coil_state",
                        "coil_only",
                        "quiet_only",
                        "coil_loud",
                    ):
                        rows = lift_table(tagged, flag, split, min_n=12)
                        for r in rows:
                            r.update({"universe": label, "W": w, "q": q, "theta": theta})
                            all_rows.append(r)
                # grid summary ALL coil_state H=5
                rs = [
                    r
                    for r in all_rows
                    if r["W"] == w
                    and r["q"] == q
                    and r["theta"] == theta
                    and r["flag"] == "coil_state"
                    and r["split"] == "ALL"
                    and r["h"] == 5
                    and r["universe"] == label
                ]
                if rs:
                    lifts = [r["lift"] for r in rs if np.isfinite(r["lift"])]
                    grid_meta.append(
                        {
                            "universe": label,
                            "W": w,
                            "q": q,
                            "theta": theta,
                            "n_stocks": len(rs),
                            "med_lift_h5": float(np.median(lifts)) if lifts else np.nan,
                            "mean_lift_h5": float(np.mean(lifts)) if lifts else np.nan,
                            "frac_lift_ge_1_5": float(np.mean([x >= 1.5 for x in lifts]))
                            if lifts
                            else np.nan,
                            "med_n": float(np.median([r["n_coil"] for r in rs])),
                        }
                    )
    return pd.DataFrame(all_rows), pd.DataFrame(grid_meta)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--universe",
        choices=("holdings", "expert", "both"),
        default="both",
    )
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)

    frames = []
    grids = []
    if args.universe in ("holdings", "both"):
        r, g = scan_universe(list(HOLDINGS), "holdings")
        frames.append(r)
        grids.append(g)
        print("=== HOLDINGS grid (coil_state ALL H=5) ===")
        print(g.sort_values("med_lift_h5", ascending=False).to_string(index=False))
    if args.universe in ("expert", "both"):
        ids = adopted_ids()
        print(f"expert adopted n={len(ids)}")
        r, g = scan_universe(ids, "expert")
        frames.append(r)
        grids.append(g)
        print("=== EXPERT grid (coil_state ALL H=5) top ===")
        print(
            g.sort_values("med_lift_h5", ascending=False).head(12).to_string(index=False)
        )

    detail = pd.concat(frames, ignore_index=True)
    grid = pd.concat(grids, ignore_index=True)
    detail.to_csv(OUT / "f_alpha_lift_by_stock.csv", index=False)
    grid.to_csv(OUT / "f_alpha_grid_summary.csv", index=False)

    # champion pick: max med_lift among grids with frac>=0.4 and med_n>=20
    cand = grid[
        (grid["flag"] == "coil_state") if "flag" in grid.columns else slice(None)
    ]
    # grid has no flag; already coil_state only
    ok = grid[(grid["med_n"] >= 20) & grid["med_lift_h5"].notna()].copy()
    ok = ok.sort_values(["universe", "med_lift_h5"], ascending=[True, False])
    ok.to_csv(OUT / "f_alpha_grid_ranked.csv", index=False)

    # H-F2 snapshot for best holdings grid row
    h = grid[grid["universe"] == "holdings"].sort_values(
        "med_lift_h5", ascending=False
    )
    summary = {
        "burst_pct": BURST_PCT,
        "oos": OOS,
        "holdings_best": h.head(3).to_dict(orient="records") if len(h) else [],
        "expert_best": grid[grid["universe"] == "expert"]
        .sort_values("med_lift_h5", ascending=False)
        .head(3)
        .to_dict(orient="records"),
        "paths": {
            "detail": str(OUT / "f_alpha_lift_by_stock.csv"),
            "grid": str(OUT / "f_alpha_grid_summary.csv"),
        },
    }
    (OUT / "f_alpha_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("wrote", OUT)


if __name__ == "__main__":
    main()
