#!/usr/bin/env python3
"""TEJ-lite approximation: foreign trading participation share.

True TEJ conc_qfii = foreign-broker $ volume / total $ volume (broker channel).
Book approximation (no broker-channel tape):
  part_F = |F_net| * close / amount   (intensity of foreign net vs day dollar volume)
  Also signed: F_net * close / amount

Monthly: rank part_F among large-cap half (above median mcap or ADV), EW top decile
vs small-cap half — tests size-conditionality claim.

Out: reports/research/chip-overlays/faithful_tej_lite/
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
DB = ROOT / "data" / "stocks.db"
OUT = ROOT / "reports" / "research" / "chip-overlays" / "faithful_tej_lite"
START, WARMUP, END = "2024-07-01", "2024-01-01", "2026-07-24"
OOS_MONTH = "2026-01"
COST = 0.003


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(f"file:{DB.resolve()}?mode=ro", uri=True)
    ids = [
        r[0]
        for r in conn.execute(
            """
            SELECT stock_id FROM (
              SELECT stock_id, AVG(CASE WHEN amount>0 THEN amount ELSE close*volume END) adv
              FROM stock_daily_bars WHERE source='finmind' AND trade_date>=? AND trade_date<=?
                AND length(stock_id)=4 AND stock_id GLOB '[0-9][0-9][0-9][0-9]'
              GROUP BY stock_id HAVING COUNT(*)>=180 AND adv>=1e7
            )
            """,
            (START, END),
        ).fetchall()
    ]
    ph = ",".join("?" * len(ids))
    bars = pd.read_sql_query(
        f"SELECT stock_id,trade_date,close,volume,amount FROM stock_daily_bars WHERE source='finmind' AND stock_id IN ({ph}) AND trade_date>=? AND trade_date<=?",
        conn,
        params=[*ids, WARMUP, END],
    )
    inst = pd.read_sql_query(
        f"SELECT stock_id,trade_date,foreign_net FROM stock_institutional_daily WHERE source='finmind' AND stock_id IN ({ph}) AND trade_date>=? AND trade_date<=?",
        conn,
        params=[*ids, WARMUP, END],
    )
    mcap = pd.read_sql_query(
        f"SELECT stock_id,trade_date,market_value_ntd FROM stock_market_value_daily WHERE stock_id IN ({ph}) AND trade_date>=? AND trade_date<=?",
        conn,
        params=[*ids, WARMUP, END],
    )
    conn.close()
    for d in (bars, inst, mcap):
        d["trade_date"] = pd.to_datetime(d["trade_date"])
    df = bars.merge(inst, on=["stock_id", "trade_date"], how="inner")
    df = df.merge(mcap, on=["stock_id", "trade_date"], how="left")
    amt = df["amount"].astype(float)
    miss = amt.isna() | (amt <= 0)
    amt = amt.where(~miss, df["close"] * df["volume"])
    df["amt"] = amt
    df["part_F"] = (df["foreign_net"].astype(float).abs() * df["close"]) / df["amt"].replace(0, np.nan)
    df["part_F_signed"] = (df["foreign_net"].astype(float) * df["close"]) / df["amt"].replace(0, np.nan)
    df["month"] = df["trade_date"].dt.to_period("M").astype(str)
    # ADV proxy size
    df["adv20"] = (
        df.groupby("stock_id", sort=False)["amt"]
        .transform(lambda s: s.shift(1).rolling(20, min_periods=10).mean())
    )

    # month-end
    idx = df.groupby(["stock_id", "month"])["trade_date"].idxmax()
    me = df.loc[idx].copy()
    # size: within month median adv20
    me["large"] = me["adv20"] >= me.groupby("month")["adv20"].transform("median")

    # month returns
    mrows = []
    for (sid, month), g in df.groupby(["stock_id", "month"]):
        g = g.sort_values("trade_date")
        if len(g) < 5:
            continue
        mrows.append(dict(stock_id=sid, month=month, ret=float(g.close.iloc[-1] / g.close.iloc[0] - 1)))
    mret = pd.DataFrame(mrows)
    months = sorted(m for m in me.month.unique() if m >= START[:7])

    def curve(size_filter: str) -> pd.DataFrame:
        rows = []
        for i, month in enumerate(months):
            if i == 0:
                continue
            prev = months[i - 1]
            snap = me[me.month == prev]
            if size_filter == "large":
                snap = snap[snap.large.fillna(False)]
            elif size_filter == "small":
                snap = snap[~snap.large.fillna(True)]
            if snap.empty:
                continue
            thr = snap["part_F"].quantile(0.9)
            picks = snap.loc[snap["part_F"] >= thr, "stock_id"].astype(str).tolist()
            book = mret[(mret.month == month) & (mret.stock_id.isin(picks))]
            bench = mret[mret.month == month]
            if size_filter == "large":
                # approximate large bench
                large_ids = me.loc[(me.month == prev) & me.large.fillna(False), "stock_id"]
                bench = mret[(mret.month == month) & (mret.stock_id.isin(large_ids))]
            elif size_filter == "small":
                small_ids = me.loc[(me.month == prev) & ~me.large.fillna(True), "stock_id"]
                bench = mret[(mret.month == month) & (mret.stock_id.isin(small_ids))]
            if book.empty or bench.empty:
                continue
            port = float(book.ret.mean()) - COST
            ew = float(bench.ret.mean())
            rows.append(dict(month=month, n=len(book), port=port, bench=ew, excess=port - ew, oos=month >= OOS_MONTH))
        return pd.DataFrame(rows)

    summaries = []
    for label in ("all", "large", "small"):
        c = curve(label)
        c.to_csv(OUT / f"curve_{label}.csv", index=False)
        for tag, sub in (("ALL", c), ("OOS", c[c.oos]), ("IS", c[~c.oos])):
            if sub.empty:
                continue
            summaries.append(
                dict(
                    size=label,
                    split=tag,
                    n=len(sub),
                    mean_excess=float(sub.excess.mean()),
                    med_excess=float(sub.excess.median()),
                    hit=float((sub.excess > 0).mean()),
                    mean_port=float(sub.port.mean()),
                )
            )
        print(label, summaries[-3:] if len(summaries) >= 3 else summaries)

    pd.DataFrame(summaries).to_csv(OUT / "tej_lite_summary.csv", index=False)
    (OUT / "protocol.json").write_text(
        json.dumps(
            {
                "note": "APPROXIMATION of TEJ conc_qfii — uses |foreign_net|*px/amount not foreign-broker channel share",
                "refs": ["https://www.tejwin.com/insight/因子研究系列－透過外資集中度追蹤聰明錢足跡-上/"],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print("wrote", OUT)


if __name__ == "__main__":
    main()
