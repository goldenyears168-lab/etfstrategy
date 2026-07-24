#!/usr/bin/env python3
"""Soft-weight simulation: expert greens × FT3 confirmation multipliers.

Arms on same trade set (L1H7 r_adj already in knowledge trades):
  W0  equal weight all greens (baseline mean of r_adj)
  W1  FT3>0 → 1.2× notional proxy (scale return), else 0.7×
  W2  hard keep only FT3>0
  W3  hard drop FT3<=0 (same as W2)

Reports OOS med/mean of scaled returns and hit.
Out: reports/research/chip-overlays/essence_confirmation/soft_weight_sim.csv
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
DB = ROOT / "data" / "stocks.db"
TRADES = (
    ROOT
    / "reports"
    / "research"
    / "branch-footprint-screen"
    / "expert_pool"
    / "knowledge"
    / "trades"
)
OUT = ROOT / "reports" / "research" / "chip-overlays" / "essence_confirmation"
OOS = "2026-01-01"


def main() -> None:
    rows = []
    for fp in TRADES.glob("*.json"):
        d = json.loads(fp.read_text(encoding="utf-8"))
        sid = str(d["stock_id"])
        for t in d.get("trades") or []:
            rows.append(
                {
                    "stock_id": sid,
                    "signal_date": t.get("signal_date"),
                    "r_adj_pct": t.get("r_adj_pct"),
                }
            )
    tr = pd.DataFrame(rows).dropna()
    tr["signal_date"] = pd.to_datetime(tr["signal_date"])
    tr["r_adj_pct"] = tr["r_adj_pct"].astype(float)
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
    bars = pd.read_sql_query(
        f"""SELECT stock_id,trade_date FROM stock_daily_bars WHERE source='finmind'
            AND stock_id IN ({ph}) AND trade_date>=? AND trade_date<=?""",
        conn,
        params=[*sids, d0, d1],
    )
    conn.close()
    for d in (inst, bars):
        d["trade_date"] = pd.to_datetime(d["trade_date"])
    chip = bars.merge(inst, on=["stock_id", "trade_date"], how="left").sort_values(
        ["stock_id", "trade_date"]
    )
    chip["FT"] = chip.foreign_net.fillna(0) + chip.investment_trust_net.fillna(0)
    chip["FT3"] = chip.groupby("stock_id")["FT"].transform(
        lambda s: s.rolling(3, min_periods=2).sum()
    )
    m = tr.merge(
        chip.rename(columns={"trade_date": "signal_date"})[
            ["stock_id", "signal_date", "FT3"]
        ],
        on=["stock_id", "signal_date"],
        how="left",
    )
    m["confirm"] = m["FT3"] > 0

    def pack(sub: pd.DataFrame, name: str) -> dict:
        return dict(
            arm=name,
            n=len(sub),
            med=float(sub.r.median()),
            mean=float(sub.r.mean()),
            win=float((sub.r > 0).mean()),
        )

    out_rows = []
    for split, ss in (
        ("ALL", m),
        ("OOS", m[m.signal_date >= OOS]),
        ("IS", m[m.signal_date < OOS]),
    ):
        base = ss.assign(r=ss.r_adj_pct)
        soft = ss.assign(
            r=np.where(ss.confirm, ss.r_adj_pct * 1.2, ss.r_adj_pct * 0.7)
        )
        hard = ss.loc[ss.confirm].assign(r=ss.loc[ss.confirm, "r_adj_pct"])
        for name, frame in (
            ("W0_baseline", base),
            ("W1_soft_1p2_0p7", soft),
            ("W2_hard_FT3_only", hard),
        ):
            row = pack(frame, name)
            row["split"] = split
            if name != "W0_baseline":
                row["delta_med"] = row["med"] - pack(base, "b")["med"]
            else:
                row["delta_med"] = 0.0
            out_rows.append(row)

    res = pd.DataFrame(out_rows)
    OUT.mkdir(parents=True, exist_ok=True)
    res.to_csv(OUT / "soft_weight_sim.csv", index=False)
    print(res[res.split == "OOS"].to_string(index=False))


if __name__ == "__main__":
    main()
