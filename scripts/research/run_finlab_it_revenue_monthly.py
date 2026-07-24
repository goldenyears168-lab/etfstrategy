#!/usr/bin/env python3
"""FinLab-faithful monthly: 投信買超 × 營收動能 (PIT via create_time).

Arms (equal-weight top10, monthly, 30bps, liquid universe):
  R0  revenue-only (3M avg at 12M high AND YoY>20%)
  I0  IT10_amt top10 among IT10>0 (chip-only control)
  IR  R0 pool ∩ ranked by IT10_amt top10  (FinLab composite)
  IRdiv R0 ∧ IT10>0 ∧ F10<=0 ranked by IT10

PIT: month revenue usable on create_time (else date). Signal month M uses
only revenue with available_date <= month-end.

Bench: EW liquid month return.
Out: reports/research/chip-overlays/faithful_finlab_it_revenue/
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
DB = ROOT / "data" / "stocks.db"
REV = ROOT / "reports" / "research" / "chip-overlays" / "cache" / "month_revenue_liquid.parquet"
OUT = ROOT / "reports" / "research" / "chip-overlays" / "faithful_finlab_it_revenue"
START, END = "2024-07-01", "2026-07-20"
OOS_MONTH = "2026-01"
COST = 0.003
TOP = 10
YOY = 20.0


def load_prices_inst(sids: list[str]) -> pd.DataFrame:
    conn = sqlite3.connect(f"file:{DB.resolve()}?mode=ro", uri=True)
    ph = ",".join("?" * len(sids))
    bars = pd.read_sql_query(
        f"""SELECT stock_id,trade_date,close,volume,amount
            FROM stock_daily_bars WHERE source='finmind' AND stock_id IN ({ph})
            AND trade_date>='2024-01-01' AND trade_date<=?""",
        conn,
        params=[*sids, END],
    )
    inst = pd.read_sql_query(
        f"""SELECT stock_id,trade_date,foreign_net,investment_trust_net
            FROM stock_institutional_daily WHERE source='finmind' AND stock_id IN ({ph})
            AND trade_date>='2024-01-01' AND trade_date<=?""",
        conn,
        params=[*sids, END],
    )
    conn.close()
    for d in (bars, inst):
        d["trade_date"] = pd.to_datetime(d["trade_date"])
    df = bars.merge(inst, on=["stock_id", "trade_date"], how="inner").sort_values(
        ["stock_id", "trade_date"]
    )
    dv = df["amount"].astype(float)
    miss = dv.isna() | (dv <= 0)
    df.loc[miss, "amount"] = df.loc[miss, "close"] * df.loc[miss, "volume"]
    g = df.groupby("stock_id", sort=False)
    df["IT10"] = g["investment_trust_net"].transform(
        lambda s: s.astype(float).rolling(10, min_periods=5).sum()
    )
    df["F10"] = g["foreign_net"].transform(
        lambda s: s.astype(float).rolling(10, min_periods=5).sum()
    )
    df["IT10_amt"] = df["IT10"] * df["close"]
    df["vol20"] = g["volume"].transform(
        lambda s: s.astype(float).rolling(20, min_periods=10).mean()
    )
    df["month"] = df["trade_date"].dt.to_period("M").astype(str)
    return df


def build_rev_features(rev: pd.DataFrame) -> pd.DataFrame:
    r = rev.copy()
    r["stock_id"] = r["stock_id"].astype(str)
    r["available"] = pd.to_datetime(
        np.where(r["create_time"].astype(str).str.len() >= 8, r["create_time"], r["date"])
    )
    r["revenue"] = r["revenue"].astype(float)
    r = r.sort_values(["stock_id", "revenue_year", "revenue_month"])
    # YoY
    key = r[["stock_id", "revenue_year", "revenue_month", "revenue"]].rename(
        columns={"revenue": "rev_ly"}
    )
    key["revenue_year"] = key["revenue_year"] + 1
    r = r.merge(key, on=["stock_id", "revenue_year", "revenue_month"], how="left")
    r["yoy"] = (r["revenue"] / r["rev_ly"] - 1.0) * 100.0
    # 3M avg
    r["rev3"] = r.groupby("stock_id")["revenue"].transform(
        lambda s: s.rolling(3, min_periods=3).mean()
    )
    r["rev3_max12"] = r.groupby("stock_id")["rev3"].transform(
        lambda s: s.rolling(12, min_periods=6).max()
    )
    r["rev_high"] = (r["rev3"] >= r["rev3_max12"] * 0.999) & r["rev3"].notna()
    r["rev_ok"] = r["rev_high"] & (r["yoy"] > YOY)
    return r


def month_end(df: pd.DataFrame) -> pd.DataFrame:
    idx = df.groupby(["stock_id", "month"])["trade_date"].idxmax()
    return df.loc[idx].copy()


def month_returns(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for (sid, month), g in df.groupby(["stock_id", "month"]):
        g = g.sort_values("trade_date")
        if len(g) < 5:
            continue
        rows.append(
            dict(
                stock_id=sid,
                month=month,
                ret=float(g["close"].iloc[-1] / g["close"].iloc[0] - 1.0),
            )
        )
    return pd.DataFrame(rows)


def attach_rev_pit(me: pd.DataFrame, rev: pd.DataFrame) -> pd.DataFrame:
    """For each stock-month-end, latest rev_ok / yoy with available<=month_end."""
    parts = []
    rev = rev.sort_values(["stock_id", "available"])
    for sid, g in me.groupby("stock_id"):
        r = rev[rev.stock_id == sid][
            ["available", "yoy", "rev_ok", "rev3", "revenue_year", "revenue_month"]
        ].rename(columns={"available": "trade_date"})
        if r.empty:
            gg = g.copy()
            gg["yoy"] = np.nan
            gg["rev_ok"] = False
        else:
            gg = pd.merge_asof(
                g.sort_values("trade_date"),
                r.sort_values("trade_date"),
                on="trade_date",
                direction="backward",
            )
            gg["rev_ok"] = gg["rev_ok"].fillna(False)
        parts.append(gg)
    return pd.concat(parts, ignore_index=True)


def pick(me: pd.DataFrame, month: str, mask: pd.Series, score: str, n: int) -> list[str]:
    sub = me[(me.month == month) & mask.reindex(me.index).fillna(False)]
    if sub.empty:
        return []
    return sub.sort_values(score, ascending=False)["stock_id"].head(n).astype(str).tolist()


def run_curve(me, mret, months, picker) -> pd.DataFrame:
    rows = []
    for i, month in enumerate(months):
        if i == 0:
            continue
        held = picker(me, months[i - 1])
        book = mret[(mret.month == month) & (mret.stock_id.isin(held))]
        bench = mret[mret.month == month]
        if book.empty or bench.empty:
            continue
        port = float(book.ret.mean()) - COST
        ew = float(bench.ret.mean())
        rows.append(
            dict(
                month=month,
                n=len(book),
                port=port,
                bench=ew,
                excess=port - ew,
                oos=month >= OOS_MONTH,
            )
        )
    return pd.DataFrame(rows)


def summarize(curve: pd.DataFrame, arm: str) -> dict:
    out = {"arm": arm, "n_months": int(len(curve))}
    for tag, sub in (
        ("ALL", curve),
        ("OOS", curve[curve.oos]),
        ("IS", curve[~curve.oos]),
    ):
        if sub.empty:
            continue
        rets = sub.port.to_numpy()
        ex = sub.excess.to_numpy()
        wealth = np.cumprod(1 + rets)
        peak = np.maximum.accumulate(wealth)
        out.update(
            {
                f"{tag}_n": len(sub),
                f"{tag}_mean_month": float(rets.mean()),
                f"{tag}_med_excess": float(np.median(ex)),
                f"{tag}_mean_excess": float(ex.mean()),
                f"{tag}_hit": float((ex > 0).mean()),
                f"{tag}_mdd": float((wealth / peak - 1).min()),
                f"{tag}_total": float(wealth[-1] - 1),
            }
        )
    return out


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    if not REV.exists():
        raise SystemExit(f"missing revenue cache: {REV} · run cache_finmind_month_revenue.py first")
    rev_raw = pd.read_parquet(REV)
    rev = build_rev_features(rev_raw)
    sids = sorted(rev.stock_id.astype(str).unique())
    print(f"revenue stocks={len(sids)} rows={len(rev)} rev_ok_rate={rev.rev_ok.mean():.2%}")

    df = load_prices_inst(sids)
    # liquid filter overlap
    df = df[df.vol20 > 300_000]
    me = attach_rev_pit(month_end(df), rev)
    mret = month_returns(df)
    months = sorted(m for m in me.month.unique() if m >= START[:7] and m <= END[:7])

    arms = {
        "R0_revenue_only": lambda me, m: pick(
            me, m, me.rev_ok & (me.vol20 > 300_000), "yoy", TOP
        ),
        "I0_IT_only": lambda me, m: pick(
            me, m, (me.IT10 > 0) & (me.vol20 > 300_000), "IT10_amt", TOP
        ),
        "IR_IT_x_revenue": lambda me, m: pick(
            me, m, me.rev_ok & (me.IT10 > 0) & (me.vol20 > 300_000), "IT10_amt", TOP
        ),
        "IRdiv_IT_buy_F_sell_x_rev": lambda me, m: pick(
            me,
            m,
            me.rev_ok & (me.IT10 > 0) & (me.F10 <= 0) & (me.vol20 > 300_000),
            "IT10_amt",
            TOP,
        ),
    }

    summaries = []
    for name, picker in arms.items():
        curve = run_curve(me, mret, months, picker)
        curve.to_csv(OUT / f"curve_{name}.csv", index=False)
        s = summarize(curve, name)
        summaries.append(s)
        print(name, {k: s[k] for k in s if k.startswith("OOS") or k == "arm"})

    pd.DataFrame(summaries).to_csv(OUT / "summary.csv", index=False)
    meta = {
        "yoy_threshold": YOY,
        "top": TOP,
        "cost": COST,
        "pit": "create_time else FinMind date",
        "refs": [
            "https://finlab.finance/blog/institutional-strategy",
            "https://finlab.finance/blog/institutional-investors-comparison",
        ],
        "note": "Approximation of FinLab sim; bench=EW liquid overlap with revenue cache",
    }
    (OUT / "protocol.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("wrote", OUT)


if __name__ == "__main__":
    main()
