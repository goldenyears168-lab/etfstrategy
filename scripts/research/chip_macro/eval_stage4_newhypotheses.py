"""Stage-4: search for a signal INDEPENDENT of the foreign-futures factor.

Stage-3 showed the winners are one factor (foreign futures positioning). A new
signal is only valuable if it (a) has OOS edge over B&H AND (b) is weakly
correlated with the champion's daily strategy returns -> genuine diversification.

Direction of each signal is fixed from the IN-SAMPLE IC sign only (no OOS peek):
  directed = sign(IC_IS) * zscore(value);  L/F bullish when directed > 0.

New hypotheses tested (beyond foreign futures):
  - 融資餘額 margin_bal  (retail leverage; expected contrarian)
  - 融資 5d %chg         (leverage acceleration)
  - 融券餘額 short_bal   (short interest / squeeze fuel)
  - 券資比 short/margin
  - 投信/自營 期貨 OI    (domestic institution positioning)
  - 避險 dealer_hedge    (dealer hedging as fear proxy)
  - 現貨/期貨 背離        foreign cash vs foreign futures-change divergence
Champion (reference): fut_foreign_oi z60 > 0.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
PANEL = ROOT / "data" / "research" / "chip_macro" / "panel.parquet"
OUT = ROOT / "reports" / "research" / "chip-macro"
COST_BPS = 4.0
ANN = np.sqrt(252)
RNG = np.random.default_rng(7)


def rz(s, w):
    return (s - s.rolling(w, min_periods=w).mean()) / s.rolling(w, min_periods=w).std()


def sharpe(x):
    x = pd.Series(x).dropna()
    return x.mean() / x.std() * ANN if len(x) > 20 and x.std() > 0 else np.nan


def lf(bull, day_ret, cost=COST_BPS):
    pos = pd.Series(bull, index=day_ret.index).astype(float).fillna(0.0)
    return pos * day_ret - pos.diff().abs().fillna(pos.abs()) * (cost / 1e4)


def perm_p(bull, day_ret, mask, n=2000):
    dr = day_ret[mask].reset_index(drop=True)
    pos = pd.Series(bull, index=day_ret.index)[mask].astype(float).reset_index(drop=True)
    v = dr.notna(); dr, pos = dr[v], pos[v]
    actual = sharpe(pos.values * dr.values)
    k, N = int(pos.sum()), len(pos)
    if k == 0 or k == N:
        return actual, np.nan
    drv = dr.values
    null = np.array([sharpe((lambda r: r)(np.isin(np.arange(N), RNG.choice(N, k, False)).astype(float)) * drv) for _ in range(n)])
    return actual, (null >= actual).mean()


def main():
    p = pd.read_parquet(PANEL).sort_values("date").reset_index(drop=True)
    day_ret = (p["ix_close"] / p["ix_open"] - 1.0).shift(-1)
    n = len(p); is_cut = int(n * 0.7)
    ismask = pd.Series(p.index < is_cut, index=p.index)
    oos = pd.Series(p.index >= is_cut, index=p.index)

    # champion reference returns
    champ_bull = rz(p["fut_foreign_oi"], 60) > 0
    champ_ret = lf(champ_bull, day_ret)

    # candidate raw values (higher = more of the thing)
    short_bal = p["short_bal"]
    cand_vals = {
        "margin_bal_z60":   rz(p["margin_bal"], 60),
        "margin_chg5_pct":  p["margin_bal"].pct_change(5),
        "short_bal_z60":    rz(short_bal, 60),
        "shortmargin_ratio_z60": rz(short_bal / p["margin_bal"], 60),
        "fut_trust_oi_z60": rz(p["fut_trust_oi"], 60),
        "fut_dealer_oi_z60": rz(p["fut_dealer_oi"], 60),
        "dealer_hedge_z20": rz(p["dealer_hedge"], 20),
        # divergence: continuous = foreign cash sign flipped * futures doi (captures cash-sell+fut-buy)
        "cashfut_diverg":   (-np.sign(p["foreign"].fillna(0))) * p["fut_foreign_doi"],
    }

    rows = []
    for name, v in cand_vals.items():
        v = pd.Series(v, index=p.index)
        d = pd.DataFrame({"v": v, "f": day_ret}).dropna()
        if len(d) < 100:
            continue
        ic_is = d[ismask.reindex(d.index).fillna(False)]["v"].corr(
            d[ismask.reindex(d.index).fillna(False)]["f"])
        direction = np.sign(ic_is) if pd.notna(ic_is) and ic_is != 0 else 1.0
        directed = direction * v
        bull = directed > 0
        r = lf(bull, day_ret)
        oos_shp = sharpe(r[oos])
        corr_champ = pd.DataFrame({"a": r, "b": champ_ret}).dropna().corr().iloc[0, 1]
        rows.append({
            "signal": name, "dir": int(direction), "IC_IS": ic_is,
            "OOS_Sharpe": oos_shp, "corr_vs_champ": corr_champ,
            "exposure": bull.reindex(day_ret.index).fillna(False).mean(),
        })

    res = pd.DataFrame(rows).sort_values("OOS_Sharpe", ascending=False)
    bh_oos = sharpe(day_ret[oos])
    print(f"B&H OOS Sharpe = {bh_oos:+.2f} ; champion OOS = {sharpe(champ_ret[oos]):+.2f}\n")
    pd.set_option("display.float_format", lambda x: f"{x:+.3f}" if isinstance(x, float) else str(x))
    print("=== new-hypothesis scan (direction fixed from IS) ===")
    print(res.to_string(index=False))

    # flag independent + additive
    indep = res[(res["OOS_Sharpe"] > bh_oos) & (res["corr_vs_champ"].abs() < 0.4)]
    print("\n=== INDEPENDENT (OOS>B&H and |corr vs champ|<0.4) ===")
    if indep.empty:
        print("  none — no new factor beats B&H while staying independent of foreign-futures.")
    else:
        print(indep.to_string(index=False))
        for _, r in indep.iterrows():
            v = pd.Series(cand_vals[r["signal"]], index=p.index)
            bull = (r["dir"] * v) > 0
            a, pv = perm_p(bull, day_ret, oos)
            print(f"  {r['signal']}: OOS perm p={pv:.3f}")
            # combine 50/50 with champion
            comb = ((champ_bull.astype(float) + bull.astype(float)) / 2)  # avg position 0/0.5/1
            cr = (comb * day_ret - comb.diff().abs().fillna(comb.abs()) * COST_BPS / 1e4)
            print(f"    champ+{r['signal']} 50/50 combo OOS Sharpe={sharpe(cr[oos]):+.2f} "
                  f"(champ alone {sharpe(champ_ret[oos]):+.2f})")

    res.to_csv(OUT / "stage4_newhyp.csv", index=False)
    print(f"\n-> {OUT/'stage4_newhyp.csv'}")


if __name__ == "__main__":
    main()
