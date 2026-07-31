"""Stage-2 deep dive on the winning chip signals from the IC scan.

Controls for the elephant in the room: 2018-2026 TAIEX is a strong bull market, so
any mostly-long signal looks good. We therefore report every strategy AGAINST
buy-and-hold (B&H) on the same window, and split IS/OOS.

Backtest convention (no look-ahead):
  position at t (from chip info known after close t) is applied to the
  open(t+1)->close(t+1) daily return. Daily rebalance. h=1.
  Multi-day holds are approximated by holding the position while the signal
  condition persists (state-based), so turnover is realistic.

Variants per signal:
  L/F  = long when bullish else flat (market-timing overlay)
  L/S  = long when bullish else short
Costs: round-trip bps applied on position changes.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
PANEL = ROOT / "data" / "research" / "chip_macro" / "panel.parquet"
OUT = ROOT / "reports" / "research" / "chip-macro"
IS_FRAC = 0.70
COST_BPS = 4.0  # round-trip, per full position flip (TAIEX-ETF/futures realistic ~ few bps)


def rolling_z(s, w):
    return (s - s.rolling(w, min_periods=w).mean()) / s.rolling(w, min_periods=w).std()


def signed_streak(s):
    sign = np.sign(s.fillna(0.0).values)
    out = np.zeros(len(sign)); run = 0; prev = 0.0
    for i, v in enumerate(sign):
        run = 0 if v == 0 else (run + 1 if v == prev else 1)
        out[i] = run * v; prev = v
    return pd.Series(out, index=s.index)


def metrics(daily_ret: pd.Series, label: str):
    x = daily_ret.dropna()
    if len(x) < 20 or x.std() == 0:
        return {"label": label, "n": len(x), "sharpe": np.nan, "cagr": np.nan,
                "ann_vol": np.nan, "maxdd": np.nan, "hit": np.nan}
    ann = np.sqrt(252)
    sharpe = x.mean() / x.std() * ann
    cum = (1 + x).cumprod()
    cagr = cum.iloc[-1] ** (252 / len(x)) - 1
    dd = (cum / cum.cummax() - 1).min()
    return {"label": label, "n": len(x), "sharpe": sharpe, "cagr": cagr,
            "ann_vol": x.std() * ann, "maxdd": dd, "hit": (x > 0).mean()}


def run_strategy(pos: pd.Series, day_ret: pd.Series, cost_bps: float) -> pd.Series:
    pos = pos.fillna(0.0)
    turn = pos.diff().abs().fillna(pos.abs())
    cost = turn * (cost_bps / 1e4)
    return pos * day_ret - cost


def main():
    p = pd.read_parquet(PANEL).sort_values("date").reset_index(drop=True)
    # tradable daily return: open(t+1) -> close(t+1); assign to row t (decision day)
    day_ret_next = (p["ix_close"] / p["ix_open"] - 1.0).shift(-1)
    n = len(p); is_cut = int(n * IS_FRAC)
    is_mask = p.index < is_cut
    print(f"n={n}  IS end={p.loc[is_cut,'date']}  OOS n={n-is_cut}\n")

    # --- benchmark: always long ---
    bh = day_ret_next.copy()

    # --- candidate positions (bullish=+1) ---
    z60 = rolling_z(p["fut_foreign_oi"], 60)
    z120 = rolling_z(p["fut_foreign_oi"], 120)
    chg5 = p["fut_foreign_oi"].diff(5)
    doi = p["fut_foreign_doi"]
    tstreak = signed_streak(p["trust"])
    inst = p["inst_total"]

    cands = {
        "fut_foreign_oi_z60>0":     np.where(z60 > 0, 1.0, np.nan),
        "fut_foreign_oi_z120>0":    np.where(z120 > 0, 1.0, np.nan),
        "fut_foreign_oi_chg5>0":    np.where(chg5 > 0, 1.0, np.nan),
        "fut_foreign_doi>0":        np.where(doi > 0, 1.0, np.nan),
        "inst_total>0":             np.where(inst > 0, 1.0, np.nan),
        "trust_streak<=-1(contra)": np.where(tstreak <= -1, 1.0, np.nan),  # long when trust selling
    }

    rows = []
    def add(label, dret):
        for tag, mask in (("IS", is_mask), ("OOS", ~is_mask)):
            m = metrics(dret[mask], f"{label}|{tag}")
            m["strat"] = label; m["seg"] = tag
            rows.append(m)

    add("BUY&HOLD", bh)
    for name, bull in cands.items():
        bull = pd.Series(bull, index=p.index)
        # L/F: bullish -> +1 else 0
        pos_lf = bull.where(bull == 1.0, 0.0)
        add(f"{name} [L/F]", run_strategy(pos_lf, day_ret_next, COST_BPS))
        # L/S: bullish -> +1 else -1  (only where signal is defined)
        pos_ls = bull.copy()
        pos_ls[bull.isna()] = -1.0
        pos_ls = pos_ls.where(~(z60.isna() & (name.startswith("fut_foreign_oi_z"))), 0.0) if False else pos_ls
        add(f"{name} [L/S]", run_strategy(pos_ls, day_ret_next, COST_BPS))

    res = pd.DataFrame(rows)
    piv = res.pivot_table(index="strat", columns="seg", values=["sharpe", "cagr", "maxdd"])
    # order by OOS sharpe
    order = res[res.seg == "OOS"].set_index("strat")["sharpe"].sort_values(ascending=False).index
    piv = piv.reindex(order)
    pd.set_option("display.float_format", lambda x: f"{x:+.2f}")
    print("=== IS/OOS metrics (OOS-sharpe sorted) ===")
    out = piv.copy()
    out.columns = [f"{a}_{b}" for a, b in out.columns]
    print(out[["sharpe_IS", "sharpe_OOS", "cagr_IS", "cagr_OOS", "maxdd_OOS"]].to_string())

    # per-year sharpe for the single best L/F overlay vs B&H
    best = order[0]
    print(f"\n=== per-year daily-return sharpe: {best} vs BUY&HOLD ===")
    if best != "BUY&HOLD":
        name = best.split(" [")[0]; mode = best.split("[")[1].rstrip("]")
        bull = pd.Series(cands[name], index=p.index)
        if mode == "L/F":
            pos = bull.where(bull == 1.0, 0.0)
        else:
            pos = bull.copy(); pos[bull.isna()] = -1.0
        dret = run_strategy(pos, day_ret_next, COST_BPS)
        yr = p["date"].str[:4]
        cmp = pd.DataFrame({"yr": yr, "strat": dret, "bh": bh}).dropna()
        for y, g in cmp.groupby("yr"):
            ss = g["strat"].mean()/g["strat"].std()*np.sqrt(252) if g["strat"].std()>0 else np.nan
            sb = g["bh"].mean()/g["bh"].std()*np.sqrt(252) if g["bh"].std()>0 else np.nan
            print(f"  {y}: strat={ss:+.2f}  B&H={sb:+.2f}  (n={len(g)})")

    res.to_csv(OUT / "stage2_metrics.csv", index=False)
    print(f"\n-> {OUT/'stage2_metrics.csv'}")


if __name__ == "__main__":
    main()
