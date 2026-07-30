"""L0 x L1 combined-system capstone (project convergence).

WHAT THIS IS
  The whole-project convergence test. Two independently-validated pieces:
    L0 (WHEN, market timing) = system C from integrated_multigate:
         champion fut_foreign_oi z60>0  AND  close>rising MA200  AND  VIX 60d z<=2
    L1 (WHICH, stock selection) = rev_yoy_3m top-decile cross-sectional factor
         (DSR OOS 0.967, perm 0.002, Bonferroni 0.012 in tej_fundamental study)

  Combined system = hold the rev_yoy_3m top-decile basket ONLY on days L0 says
  risk-on; otherwise flat (cash). Long-only, equal-weight, weekly rebalance.

  Falsification question: does "timing x selection" beat each piece alone?
  Hypothesis to test: timing should CUT the drawdown, selection should ADD the
  return, so the combo should dominate on risk-adjusted (Sharpe / DSR) terms.

BASELINES
   (0) COMBINED        = L1 top-decile basket gated by L0 risk-on days
   (i) L1 always-in    = same basket, held every day (no timing)
  (ii) L0 on TAIEX     = system C market-timing on the index (no selection)
 (iii) B&H TAIEX       = index buy&hold
       B&H universe    = equal-weight 90-stock universe buy&hold

METHODOLOGY (project spine, falsification-first)
  - long-only top-decile (q=0.10), equal-weight, weekly (5d) block rebalance,
    t+1 entry (weights.shift(1)*ret), 4bps/side * turnover cost.
  - PIT: rev_yoy_3m = 3-month mean of monthly YoY, indexed by TEJ annd (announce
    date), daily ffill -> never uses a revenue figure before it was published.
  - L0 gate taken from integrated_multigate.parquet (already PIT / merge_asof VIX).
  - IS/OOS 70/30 time split on the tej window (price only 2021+).
  - permutation: shuffle rev_yoy_3m across stocks each OOS day, timing gate FIXED
    -> p-value that the SELECTION adds value on top of the timing.
  - Deflated-Sharpe (Bailey & Lopez de Prado) at honest n_trials (funnel-aware).
  - regime split bull/bear; collinearity of combined pnl with champion timing pnl.

Not investment advice. Research falsification only.
"""
from __future__ import annotations
from pathlib import Path
import numpy as np
import pandas as pd
from scipy.stats import norm

ROOT = Path(__file__).resolve().parents[3]
D = ROOT / "data" / "research" / "dashboard"
REPORTDIR = ROOT / "reports" / "research" / "dashboard-completeness"
OUTPARQ = D / "l0xl1_combined.parquet"
OUTCSV = REPORTDIR / "l0xl1_combined_metrics.csv"

COST = 0.0004  # 4bps per side
ANN = np.sqrt(252)
GAMMA = 0.5772156649
Q = 0.10       # top-decile long-only
RNG = np.random.default_rng(11)

# ---------- load ----------
pb = pd.read_parquet(D / "tej_fundamental_pb.parquet")     # daily close_adj, pb_ratio
sale = pd.read_parquet(D / "tej_fundamental_sale.parquet") # monthly rev_yoy + annd
mg = pd.read_parquet(D / "integrated_multigate.parquet")   # L0 system C gate + fwd
panel = pd.read_parquet(D.parent / "chip_macro" / "panel.parquet")

pb["date"] = pd.to_datetime(pb["date"])
sale["annd"] = pd.to_datetime(sale["annd"])
mg["date"] = pd.to_datetime(mg["date"])
panel["date"] = pd.to_datetime(panel["date"])

px = pb.pivot(index="date", columns="stock_id", values="close_adj").sort_index()
dates = px.index
stocks = list(px.columns)
ret = px.pct_change()  # ret.loc[t] = t-1 -> t

# ---------- L1 signal: rev_yoy_3m (PIT via annd, daily ffill) ----------
def rev_yoy_3m_mat():
    m = pd.DataFrame(index=dates, columns=stocks, dtype=float)
    for sid, g in sale.groupby("stock_id"):
        if sid not in stocks:
            continue
        g = g.dropna(subset=["rev_yoy"]).sort_values("annd")
        if len(g) == 0:
            continue
        roll3 = pd.Series(g["rev_yoy"].astype(float).values).rolling(3).mean().values
        s = pd.Series(roll3, index=pd.to_datetime(g["annd"].values))
        s = s[~s.index.duplicated(keep="last")].sort_index()
        m[sid] = s.reindex(dates, method="ffill")
    return m.astype(float)

sig = rev_yoy_3m_mat()

# ---------- L0 gate: system C, aligned to tej dates ----------
gate_series = mg.set_index("date")["pos_C_tech_vix_gated"].reindex(dates).ffill().fillna(0.0)
L0_on = gate_series > 0.5   # boolean risk-on per tej date

# market series aligned to tej dates
pn = panel.set_index("date").reindex(dates).ffill()
ix_ret = pn["ix_close"].pct_change()
ma200 = pn["ix_close"].rolling(200).mean()
bull = (pn["ix_close"] > ma200) & (ma200 > ma200.shift(20))
x = pn["fut_foreign_oi"]
z60 = (x - x.rolling(60).mean()) / x.rolling(60).std()
champ_pnl = (z60 > 0).shift(1).astype(float) * ix_ret  # champion timing daily pnl

# ---------- IS/OOS split (match tej_fundamental_study) ----------
valid = dates[dates >= pd.Timestamp("2021-02-01")]
split = valid[int(len(valid) * 0.70)]
oos_mask = pd.Series(dates >= split, index=dates)

def sharpe(s):
    s = pd.Series(s).dropna()
    return np.nan if len(s) < 20 or s.std() == 0 else s.mean() / s.std() * ANN

def maxdd(s):
    e = (1 + pd.Series(s).fillna(0)).cumprod()
    return float((e / e.cummax() - 1).min())

def deflated_sharpe(r, n_trials):
    r = pd.Series(r).dropna().values
    n = len(r)
    if n < 30 or r.std() == 0:
        return np.nan
    sr = r.mean() / r.std()
    g3 = pd.Series(r).skew()
    g4 = pd.Series(r).kurtosis() + 3.0
    sr_star = np.sqrt(1.0 / n) * (
        (1 - GAMMA) * norm.ppf(1 - 1.0 / n_trials)
        + GAMMA * norm.ppf(1 - 1.0 / (n_trials * np.e))
    )
    denom = np.sqrt(1 - g3 * sr + (g4 - 1) / 4.0 * sr**2)
    return float(norm.cdf((sr - sr_star) * np.sqrt(n - 1) / denom))

# ---------- weekly-block long-only top-decile weights ----------
_block = np.arange(len(dates)) // 5
_bstart_pos = np.array([np.where(_block == b)[0][0] for b in np.unique(_block)])
_bstart_dates = dates[_bstart_pos]

def topdecile_weights(signal, direction=1.0, q=Q):
    s = signal * direction
    r = s.rank(axis=1, pct=True)
    cnt = s.notna().sum(axis=1)
    long = (r >= (1 - q)) & (cnt.values[:, None] >= 10)
    nl = long.sum(axis=1).replace(0, np.nan)
    w = long.div(nl, axis=0).fillna(0.0)  # equal-weight, fully invested when held
    wb = w.reindex(_bstart_dates).reindex(dates).ffill().fillna(0.0)
    return wb

def portfolio(weights):
    turnover = (weights - weights.shift(1).fillna(0.0)).abs().sum(axis=1)
    return (weights.shift(1) * ret).sum(axis=1) - turnover * COST

# direction from IS Rank-IC (falsification-first)
fwd5 = px.shift(-5) / px - 1
def rank_ic_is(signal):
    ics = []
    for d in dates[dates < split]:
        a, b = signal.loc[d], fwd5.loc[d]
        m = a.notna() & b.notna()
        if m.sum() >= 10:
            ics.append(a[m].rank().corr(b[m].rank()))
    return float(np.nanmean(ics)) if ics else 0.0
ic_is = rank_ic_is(sig)
DIRECTION = 1.0 if ic_is >= 0 else -1.0

# ---------- build the four/five series ----------
w_sel = topdecile_weights(sig, DIRECTION)          # L1 basket weights (always-in)
w_comb = w_sel.mul(L0_on.astype(float), axis=0)    # gated by L0 risk-on days

r_L1 = portfolio(w_sel)                            # (i) selection always-in
r_comb = portfolio(w_comb)                         # (0) combined timing x selection
r_L0 = mg.set_index("date")["ret_C_tech_vix_gated"].reindex(dates).fillna(0.0)  # (ii)
bh_ix = ix_ret                                     # (iii) index B&H
bh_uni = ret.mean(axis=1)                          # equal-weight universe B&H

SERIES = {
    "0_COMBINED_L0xL1": r_comb,
    "i_L1_selection_alwaysin": r_L1,
    "ii_L0_timing_TAIEX": r_L0,
    "iii_BH_TAIEX": bh_ix,
    "iv_BH_universe_ew": bh_uni,
}

# honest n_trials: rev factor funnel(6) x timing systems(4) ~ 24 conservative;
# 6 = the factor search that produced rev_yoy_3m (main).
N_MAIN, N_CONS = 6, 24

rows = []
for name, r in SERIES.items():
    r_oos = r[oos_mask.values]
    r_is = r[~oos_mask.values]
    rows.append(dict(
        system=name,
        expo_oos=round(float((r_oos != 0).mean()), 3),
        IS_Sharpe=round(sharpe(r_is), 3),
        OOS_Sharpe=round(sharpe(r_oos), 3),
        OOS_ann_ret_pct=round(float(r_oos.mean()) * 252 * 100, 3),
        OOS_maxDD=round(maxdd(r_oos), 3),
        OOS_Calmar=round(float(r_oos.mean()) * 252 / abs(maxdd(r_oos)), 2) if maxdd(r_oos) < 0 else np.nan,
        OOS_bull_Sh=round(sharpe(r.where(bull, np.nan)[oos_mask.values]), 3),
        OOS_bear_Sh=round(sharpe(r.where(~bull, np.nan)[oos_mask.values]), 3),
        DSR_nt6=round(deflated_sharpe(r_oos, N_MAIN), 3),
        DSR_nt24=round(deflated_sharpe(r_oos, N_CONS), 3),
        corr_champ=round(float(pd.Series(r).corr(champ_pnl)), 3),
    ))
res = pd.DataFrame(rows)

# ---------- permutation: does SELECTION add value on top of TIMING? ----------
# shuffle rev_yoy_3m across stocks each OOS day, keep L0 gate fixed, recompute combo
obs = sharpe(r_comb[oos_mask.values])
N = 500
cnt = 0
arr0 = sig.values
oos_idx = np.where(oos_mask.values)[0]
for _ in range(N):
    a = arr0.copy()
    for rI in oos_idx:
        row = a[rI]
        idx = np.where(~np.isnan(row))[0]
        if len(idx) > 1:
            a[rI, idx] = RNG.permutation(row[idx])
    perm = pd.DataFrame(a, index=dates, columns=stocks)
    wp = topdecile_weights(perm, DIRECTION).mul(L0_on.astype(float), axis=0)
    rp = portfolio(wp)[oos_mask.values]
    if sharpe(rp) >= obs:
        cnt += 1
perm_p_selection = (cnt + 1) / (N + 1)

# ---------- output ----------
lines = []
def out(s=""):
    print(s); lines.append(s)

out(f"tej window {dates[0].date()} -> {dates[-1].date()}  n={len(dates)}  "
    f"universe={len(stocks)} stocks")
out(f"IS < {split.date()} <= OOS   OOS n={int(oos_mask.sum())}")
out(f"L1 rev_yoy_3m IS Rank-IC={ic_is:+.4f} -> direction={int(DIRECTION):+d}")
out(f"L0 risk-on coverage: all={float(L0_on.mean()):.1%}  "
    f"OOS={float(L0_on[oos_mask.values].mean()):.1%}")
out("")
pd.set_option("display.width", 200)
pd.set_option("display.max_columns", 30)
out(res.to_string(index=False))
out("")
out(f"PERMUTATION (selection adds value given timing): "
    f"OOS combined Sharpe={obs:+.2f}  p={perm_p_selection:.4f}  (N={N})")
out("")

# head-to-head verdict deltas
def g(nm, col):
    return float(res.loc[res.system == nm, col].iloc[0])
comb = "0_COMBINED_L0xL1"
out("=== DID TIMING x SELECTION WIN? ===")
out(f"  combo OOS Sharpe {g(comb,'OOS_Sharpe'):+.2f} vs L1-alone {g('i_L1_selection_alwaysin','OOS_Sharpe'):+.2f} "
    f"(dSharpe={g(comb,'OOS_Sharpe')-g('i_L1_selection_alwaysin','OOS_Sharpe'):+.2f})")
out(f"  combo OOS Sharpe {g(comb,'OOS_Sharpe'):+.2f} vs L0-alone {g('ii_L0_timing_TAIEX','OOS_Sharpe'):+.2f} "
    f"(dSharpe={g(comb,'OOS_Sharpe')-g('ii_L0_timing_TAIEX','OOS_Sharpe'):+.2f})")
out(f"  combo OOS Sharpe {g(comb,'OOS_Sharpe'):+.2f} vs B&H TAIEX {g('iii_BH_TAIEX','OOS_Sharpe'):+.2f}")
out(f"  combo maxDD {g(comb,'OOS_maxDD'):+.3f} vs L1-alone maxDD {g('i_L1_selection_alwaysin','OOS_maxDD'):+.3f} "
    f"(timing cut DD by {g(comb,'OOS_maxDD')-g('i_L1_selection_alwaysin','OOS_maxDD'):+.3f})")
out(f"  combo OOS ann-ret {g(comb,'OOS_ann_ret_pct'):+.2f}% vs L0-alone {g('ii_L0_timing_TAIEX','OOS_ann_ret_pct'):+.2f}% "
    f"(selection added {g(comb,'OOS_ann_ret_pct')-g('ii_L0_timing_TAIEX','OOS_ann_ret_pct'):+.2f}pp)")
out("")

# persist
keep = pd.DataFrame({"date": dates})
keep["L0_on"] = L0_on.values
keep["bull"] = bull.values
keep["oos"] = oos_mask.values
for name, r in SERIES.items():
    keep[f"ret_{name}"] = pd.Series(r).values
keep.to_parquet(OUTPARQ, index=False)
res.to_csv(OUTCSV, index=False)
out(f"-> {OUTPARQ}")
out(f"-> {OUTCSV}")

Path(REPORTDIR / "_l0xl1_console.txt").write_text("\n".join(lines))
