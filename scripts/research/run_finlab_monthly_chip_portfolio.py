#!/usr/bin/env python3
"""FinLab-style monthly chip portfolio (faithful frequency / ranking protocol).

References:
  - finlab.finance/blog/institutional-investors-comparison
  - finlab.finance/blog/institutional-strategy
  - finlab.finance/blog/claude-code-finlab-quant-trading

Frozen protocol (Book approximation — no finlab.sim):
  - Universe: 4-digit TW stocks with ADV20 ≥ 1e7 (≈成交金額過濾)
  - Score = 10日累計買賣超 × 收盤價  (金額近似；FinLab 多用股數門檻+金額排序)
  - Monthly rebalance: last calendar trading day of month signal → hold next month
    equal-weight top N (default 15); enter next month first open proxy via
    month-end close → next month-end close (standard monthly close-to-close)
  - Arms:
      A_foreign: F10_amt top15 among F10_amt > threshold
      B_trust:   IT10_amt top15
      C_dealer:  D10_amt top15
      D_IT_Fdiv: IT10_amt high & F10_amt ≤ 0, top15 by IT10_amt
      E_3factor:  rank(IT60/vol60)+rank(IT>0 rate60)+rank(F60/vol60) top20, close>ma60
  - Benchmark: equal-weight all liquid names (same month returns)
  - Cost: 30bps round-trip per monthly turnover (full book assumed)

Out: reports/research/chip-overlays/faithful_finlab_monthly/
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
OUT = ROOT / "reports" / "research" / "chip-overlays" / "faithful_finlab_monthly"

START, WARMUP, END = "2024-07-01", "2024-01-01", "2026-07-24"
OOS_MONTH = "2026-01"
COST = 0.003  # 30bps
TOP_N = 15
TOP_N3 = 20


def load_liquid_panel(min_adv: float = 1e7) -> pd.DataFrame:
    conn = sqlite3.connect(f"file:{DB.resolve()}?mode=ro", uri=True)
    # Preselect liquid ids
    ids = [
        r[0]
        for r in conn.execute(
            """
            SELECT stock_id FROM (
              SELECT stock_id,
                     AVG(CASE WHEN amount>0 THEN amount ELSE close*volume END) adv
              FROM stock_daily_bars
              WHERE source='finmind' AND trade_date>=? AND trade_date<=?
                AND length(stock_id)=4 AND stock_id GLOB '[0-9][0-9][0-9][0-9]'
              GROUP BY stock_id HAVING COUNT(*)>=180 AND adv>=?
            )
            """,
            (START, END, min_adv),
        ).fetchall()
    ]
    print(f"liquid ids={len(ids)}")
    ph = ",".join("?" * len(ids))
    bars = pd.read_sql_query(
        f"""SELECT stock_id,trade_date,open,close,volume,amount
            FROM stock_daily_bars WHERE source='finmind' AND stock_id IN ({ph})
            AND trade_date>=? AND trade_date<=?""",
        conn,
        params=[*ids, WARMUP, END],
    )
    inst = pd.read_sql_query(
        f"""SELECT stock_id,trade_date,foreign_net,investment_trust_net,dealer_self_net
            FROM stock_institutional_daily WHERE source='finmind' AND stock_id IN ({ph})
            AND trade_date>=? AND trade_date<=?""",
        conn,
        params=[*ids, WARMUP, END],
    )
    conn.close()
    for d in (bars, inst):
        d["trade_date"] = pd.to_datetime(d["trade_date"])
    df = bars.merge(inst, on=["stock_id", "trade_date"], how="inner")
    df = df.sort_values(["stock_id", "trade_date"]).reset_index(drop=True)
    df["F"] = df["foreign_net"].astype(float)
    df["IT"] = df["investment_trust_net"].astype(float)
    df["D"] = df["dealer_self_net"].astype(float)
    dv = df["amount"].astype(float)
    miss = dv.isna() | (dv <= 0)
    df.loc[miss, "amount"] = df.loc[miss, "close"] * df.loc[miss, "volume"]
    g = df.groupby("stock_id", sort=False)
    df["F10"] = g["F"].transform(lambda s: s.rolling(10, min_periods=5).sum())
    df["IT10"] = g["IT"].transform(lambda s: s.rolling(10, min_periods=5).sum())
    df["D10"] = g["D"].transform(lambda s: s.rolling(10, min_periods=5).sum())
    df["F10_amt"] = df["F10"] * df["close"]
    df["IT10_amt"] = df["IT10"] * df["close"]
    df["D10_amt"] = df["D10"] * df["close"]
    df["vol60"] = g["volume"].transform(lambda s: s.rolling(60, min_periods=30).sum())
    df["IT60"] = g["IT"].transform(lambda s: s.rolling(60, min_periods=30).sum())
    df["F60"] = g["F"].transform(lambda s: s.rolling(60, min_periods=30).sum())
    df["IT_pos60"] = g["IT"].transform(lambda s: (s > 0).rolling(60, min_periods=30).mean())
    df["trust_int"] = df["IT60"] / df["vol60"].replace(0, np.nan)
    df["foreign_int"] = df["F60"] / df["vol60"].replace(0, np.nan)
    df["ma60"] = g["close"].transform(lambda s: s.rolling(60, min_periods=40).mean())
    df["month"] = df["trade_date"].dt.to_period("M").astype(str)
    return df


def month_end_snapshot(df: pd.DataFrame) -> pd.DataFrame:
    # last trading day per stock-month
    idx = df.groupby(["stock_id", "month"], sort=False)["trade_date"].idxmax()
    return df.loc[idx].copy()


def cross_ranks(me: pd.DataFrame) -> pd.DataFrame:
    me = me.copy()
    for col, out in [
        ("trust_int", "r_ti"),
        ("IT_pos60", "r_tp"),
        ("foreign_int", "r_fi"),
    ]:
        me[out] = me.groupby("month", sort=False)[col].rank(pct=True)
    me["score3"] = me["r_ti"] + me["r_tp"] + me["r_fi"]
    return me


def pick_top(me: pd.DataFrame, month: str, score_col: str, mask: pd.Series, n: int) -> list[str]:
    sub = me[(me["month"] == month) & mask.reindex(me.index).fillna(False)]
    if sub.empty:
        return []
    return (
        sub.sort_values(score_col, ascending=False)["stock_id"].head(n).astype(str).tolist()
    )


def month_returns(df: pd.DataFrame) -> pd.DataFrame:
    """Close-to-close return within each stock-month (first→last close)."""
    rows = []
    for (sid, month), g in df.groupby(["stock_id", "month"], sort=False):
        g = g.sort_values("trade_date")
        if len(g) < 5:
            continue
        r = float(g["close"].iloc[-1] / g["close"].iloc[0] - 1.0)
        rows.append(dict(stock_id=sid, month=month, ret=r))
    return pd.DataFrame(rows)


def run_arm(
    me: pd.DataFrame,
    mret: pd.DataFrame,
    months: list[str],
    picker,
) -> pd.DataFrame:
    """picker(me, month) -> list stock_ids held DURING that month (signal from prior month-end)."""
    rows = []
    for i, month in enumerate(months):
        if i == 0:
            continue
        prev = months[i - 1]
        held = picker(me, prev)
        if not held:
            continue
        book = mret[(mret["month"] == month) & (mret["stock_id"].isin(held))]
        bench = mret[mret["month"] == month]
        if book.empty or bench.empty:
            continue
        port = float(book["ret"].mean()) - COST
        ew = float(bench["ret"].mean())
        rows.append(
            dict(
                month=month,
                n=len(book),
                port=port,
                bench=ew,
                excess=port - ew,
                is_oos=month >= OOS_MONTH,
            )
        )
    return pd.DataFrame(rows)


def summarize_curve(curve: pd.DataFrame, arm: str) -> dict:
    if curve.empty:
        return dict(arm=arm, n_months=0)
    def pack(sub, tag):
        if sub.empty:
            return {}
        rets = sub["port"].to_numpy()
        ex = sub["excess"].to_numpy()
        wealth = np.cumprod(1.0 + rets)
        peak = np.maximum.accumulate(wealth)
        mdd = float((wealth / peak - 1.0).min())
        return {
            f"{tag}_n": int(len(sub)),
            f"{tag}_cagr_approx": float(wealth[-1] ** (12 / len(sub)) - 1.0) if len(sub) >= 3 else np.nan,
            f"{tag}_mean_month": float(rets.mean()),
            f"{tag}_med_excess": float(np.median(ex)),
            f"{tag}_mean_excess": float(ex.mean()),
            f"{tag}_hit": float((ex > 0).mean()),
            f"{tag}_mdd": mdd,
            f"{tag}_total": float(wealth[-1] - 1.0),
        }
    out = {"arm": arm, "n_months": int(len(curve))}
    out.update(pack(curve, "ALL"))
    out.update(pack(curve[curve["is_oos"]], "OOS"))
    out.update(pack(curve[~curve["is_oos"]], "IS"))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=TOP_N)
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)

    df = load_liquid_panel()
    me = cross_ranks(month_end_snapshot(df))
    mret = month_returns(df)
    months = sorted(m for m in me["month"].unique() if m >= START[:7] and m <= END[:7])
    print("months", months[0], "→", months[-1], "n", len(months))

    arms = {
        "A_follow_foreign": lambda me, m: pick_top(
            me, m, "F10_amt", (me["F10"] > 0) & (me["F10_amt"] > 0), args.top
        ),
        "B_follow_trust": lambda me, m: pick_top(
            me, m, "IT10_amt", (me["IT10"] > 0) & (me["IT10_amt"] > 0), args.top
        ),
        "C_follow_dealer": lambda me, m: pick_top(
            me, m, "D10_amt", (me["D10"] > 0) & (me["D10_amt"] > 0), args.top
        ),
        "D_IT_buy_F_sell": lambda me, m: pick_top(
            me, m, "IT10_amt", (me["IT10"] > 0) & (me["F10"] <= 0), args.top
        ),
        "E_3factor": lambda me, m: pick_top(
            me,
            m,
            "score3",
            (me["close"] > me["ma60"]) & me["score3"].notna(),
            TOP_N3,
        ),
    }

    curves = {}
    summaries = []
    for name, picker in arms.items():
        curve = run_arm(me, mret, months, picker)
        curves[name] = curve
        summaries.append(summarize_curve(curve, name))
        curve.to_csv(OUT / f"curve_{name}.csv", index=False)
        print(name, summarize_curve(curve, name))

    summary = pd.DataFrame(summaries)
    summary.to_csv(OUT / "monthly_summary.csv", index=False)
    meta = {
        "protocol": {
            "rebalance": "month-end signal → next month EW close-to-close",
            "cost_rt": COST,
            "top_n": args.top,
            "universe": "4-digit ADV>=1e7",
            "amount_proxy": "cum_net_shares_10d * close",
            "benchmark": "equal-weight liquid universe same month",
            "refs": [
                "https://finlab.finance/blog/institutional-investors-comparison",
                "https://finlab.finance/blog/institutional-strategy",
            ],
            "caveats": [
                "Not identical to finlab.sim (no exact fee schedule / ranking ties)",
                "IT×revenue arm omitted: revenue_yoy mostly 2026-only in Book DB",
                "0050 history sparse from 2025-05; use EW liquid bench",
            ],
        }
    }
    (OUT / "protocol.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    print(summary.to_string(index=False))
    print("wrote", OUT)


if __name__ == "__main__":
    main()
