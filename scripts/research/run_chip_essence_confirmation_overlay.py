#!/usr/bin/env python3
"""Essence study: chip as CONFIRMATION on branch-primary expert greens.

Author intent (FinLab / E大 / 森洋):
  - Branch / specialist footprint = primary smart-money ID layer
  - Institutional prints = collective confirmation (not standalone alpha)
  - Margin / 集保 = retail vs concentration hygiene

This runner does NOT invent a new institutional sleeve. It overlays
institutional / margin / big-holder confirmation on existing expert-pool
green trades and measures Δ med L1H7 r_adj.

Out: reports/research/chip-overlays/essence_confirmation/
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
DB = ROOT / "data" / "stocks.db"
OUT = ROOT / "reports" / "research" / "chip-overlays" / "essence_confirmation"
TRADES = (
    ROOT
    / "reports"
    / "research"
    / "branch-footprint-screen"
    / "expert_pool"
    / "knowledge"
    / "trades"
)
HS_EXPERT = (
    ROOT
    / "reports"
    / "research"
    / "chip-overlays"
    / "cache"
    / "holding_shares_per_expert.csv"
)
HS_TIER_A = (
    ROOT
    / "reports"
    / "research"
    / "whale_chip_precursor"
    / "cache"
    / "holding_shares_per_tier_a.csv"
)
HS = HS_EXPERT if HS_EXPERT.exists() else HS_TIER_A
OOS = "2026-01-01"
GATE_DELTA = 1.0
GATE_N = 80


def load_trades() -> pd.DataFrame:
    rows = []
    for fp in TRADES.glob("*.json"):
        d = json.loads(fp.read_text(encoding="utf-8"))
        sid = str(d.get("stock_id"))
        for t in d.get("trades") or []:
            rows.append(
                {
                    "stock_id": sid,
                    "signal_date": t.get("signal_date") or t.get("date"),
                    "r_adj_pct": t.get("r_adj_pct")
                    if t.get("r_adj_pct") is not None
                    else t.get("radj_pct"),
                }
            )
    tr = pd.DataFrame(rows).dropna(subset=["signal_date", "r_adj_pct"])
    tr["signal_date"] = pd.to_datetime(tr["signal_date"])
    tr["r_adj_pct"] = tr["r_adj_pct"].astype(float)
    return tr


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    tr = load_trades()
    sids = sorted(tr.stock_id.unique())
    ph = ",".join("?" * len(sids))
    d0, d1 = str(tr.signal_date.min().date()), str(tr.signal_date.max().date())
    conn = sqlite3.connect(f"file:{DB.resolve()}?mode=ro", uri=True)
    inst = pd.read_sql_query(
        f"""SELECT stock_id,trade_date,foreign_net,investment_trust_net
            FROM stock_institutional_daily WHERE source='finmind'
            AND stock_id IN ({ph}) AND trade_date>=? AND trade_date<=?""",
        conn,
        params=[*sids, d0, d1],
    )
    margin = pd.read_sql_query(
        f"""SELECT stock_id,trade_date,margin_change FROM stock_margin_daily
            WHERE stock_id IN ({ph}) AND trade_date>=? AND trade_date<=?""",
        conn,
        params=[*sids, d0, d1],
    )
    bars = pd.read_sql_query(
        f"""SELECT stock_id,trade_date FROM stock_daily_bars WHERE source='finmind'
            AND stock_id IN ({ph}) AND trade_date>=? AND trade_date<=?""",
        conn,
        params=[*sids, d0, d1],
    )
    conn.close()
    for d in (inst, margin, bars):
        d["trade_date"] = pd.to_datetime(d["trade_date"])
    chip = (
        bars.merge(inst, on=["stock_id", "trade_date"], how="left")
        .merge(margin, on=["stock_id", "trade_date"], how="left")
        .sort_values(["stock_id", "trade_date"])
    )
    g = chip.groupby("stock_id", sort=False)
    chip["F"] = chip["foreign_net"].fillna(0.0)
    chip["IT"] = chip["investment_trust_net"].fillna(0.0)
    chip["FT"] = chip["F"] + chip["IT"]
    chip["IT3"] = g["IT"].transform(lambda s: s.rolling(3, min_periods=2).sum())
    chip["F3"] = g["F"].transform(lambda s: s.rolling(3, min_periods=2).sum())
    chip["FT3"] = chip["IT3"] + chip["F3"]
    chip["IT_streak"] = g["IT"].transform(
        lambda s: (s > 0).groupby((s <= 0).cumsum()).cumsum()
    )
    chip["m_down"] = chip["margin_change"] < 0
    chip["m3_down"] = g["margin_change"].transform(
        lambda s: (s < 0).rolling(3, min_periods=2).sum() >= 2
    )
    mu = g["FT"].transform(lambda s: s.rolling(21, min_periods=10).mean())
    sd = g["FT"].transform(lambda s: s.rolling(21, min_periods=10).std())
    chip["ft_climax"] = chip["FT"] > (mu + 2 * sd)

    hs = pd.read_csv(HS)
    hs["stock_id"] = hs["sid"].astype(str).str.zfill(4)
    hs["hs_date"] = pd.to_datetime(hs["d"])
    hs["big_chg"] = hs["big_holder_pct_chg"].astype(float)
    hs = hs.sort_values(["stock_id", "hs_date"])

    m = tr.merge(
        chip.rename(columns={"trade_date": "signal_date"}),
        on=["stock_id", "signal_date"],
        how="left",
    )
    parts = []
    for sid, gtr in m.groupby("stock_id"):
        h = hs[hs.stock_id == str(sid)]
        if h.empty:
            gg = gtr.copy()
            gg["big_chg"] = np.nan
        else:
            gg = pd.merge_asof(
                gtr.sort_values("signal_date"),
                h[["hs_date", "big_chg"]].rename(columns={"hs_date": "signal_date"}),
                on="signal_date",
                direction="backward",
            )
        parts.append(gg)
    m = pd.concat(parts, ignore_index=True)

    arms = {
        "baseline_green": pd.Series(True, index=m.index),
        "confirm_FT3_pos": m["FT3"] > 0,
        "confirm_IT_streak3": m["IT_streak"] >= 3,
        "confirm_FT_pos_margin_down": (m["FT"] > 0) & m["m_down"].fillna(False),
        "confirm_FT3_pos_m3down": (m["FT3"] > 0) & m["m3_down"].fillna(False),
        "diverge_FT3_nonpos": m["FT3"] <= 0,
        "finlab_IT_buy_F_sell": (m["IT3"] > 0) & (m["F3"] <= 0),
        "confirm_big_holder_up": m["big_chg"] > 0,
        "confirm_FT3_and_big_up": (m["FT3"] > 0) & (m["big_chg"] > 0),
        "kept_without_FT_climax": ~m["ft_climax"].fillna(False),
    }

    rows = []
    for split, ss in (
        ("ALL", m),
        ("IS", m[m.signal_date < OOS]),
        ("OOS", m[m.signal_date >= OOS]),
    ):
        base_med = float(ss["r_adj_pct"].median())
        for name, mask in arms.items():
            ev = ss.loc[mask.reindex(ss.index).fillna(False), "r_adj_pct"].dropna()
            if len(ev) < 15:
                rows.append(
                    dict(
                        split=split,
                        arm=name,
                        n=len(ev),
                        med=np.nan,
                        mean=np.nan,
                        win=np.nan,
                        delta_med=np.nan,
                        pass_gate=False,
                    )
                )
                continue
            med = float(ev.median())
            delta = med - base_med
            rows.append(
                dict(
                    split=split,
                    arm=name,
                    n=int(len(ev)),
                    med=med,
                    mean=float(ev.mean()),
                    win=float((ev > 0).mean()),
                    delta_med=delta,
                    pass_gate=bool(
                        split == "OOS"
                        and name != "baseline_green"
                        and delta >= GATE_DELTA
                        and len(ev) >= GATE_N
                        and not name.startswith("diverge")
                    ),
                )
            )
    res = pd.DataFrame(rows)
    res.to_csv(OUT / "confirmation_overlay_summary.csv", index=False)
    print(res[res.split == "OOS"].sort_values("delta_med", ascending=False).to_string(index=False))
    print("wrote", OUT)


if __name__ == "__main__":
    main()
