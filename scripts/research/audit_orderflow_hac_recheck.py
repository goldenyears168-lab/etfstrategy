#!/usr/bin/env python3
"""Independent re-verification of H-NS-TICK-ORDERFLOW-OPEN-WINDOW OOS HAC(10) claim.
Reads orderflow_panel.csv directly (does not touch original pipeline scripts),
rebuilds n_tick_ratio OOS trade P&L from scratch using an independently written
IS/OOS split + backtest + HAC routine, then cross-checks against statsmodels HAC
using both the manual Newey-West formula and an alternate library path.
"""
import numpy as np
import pandas as pd
from scipy import stats
import statsmodels.api as sm

PANEL = "/Users/jackm4/goldenstocks/reports/research/tmf_newsignal/orderflow_panel.csv"
COST = 2.0
COL = "w0845_0900__n_tick_ratio"

panel = pd.read_csv(PANEL).sort_values("date").reset_index(drop=True)
n = len(panel)
n_is = int(round(n * 0.70))
panel_is = panel.iloc[:n_is].reset_index(drop=True)
panel_oos = panel.iloc[n_is:].reset_index(drop=True)
print(f"n={n} n_is={len(panel_is)} n_oos={len(panel_oos)}")
print("IS date range:", panel_is['date'].iloc[0], panel_is['date'].iloc[-1])
print("OOS date range:", panel_oos['date'].iloc[0], panel_oos['date'].iloc[-1])

# direction_sign independently re-derived from IS overall spearman IC (must match reported -1)
df_is = panel_is[[COL, "return_pts"]].dropna()
rho, _ = stats.spearmanr(df_is[COL], df_is["return_pts"])
direction_sign = 1 if rho > 0 else -1
print(f"IS overall spearman IC={rho:.4f}  direction_sign={direction_sign}")

def backtest(seg):
    feat = seg[COL]
    valid = seg[feat.notna()].copy()
    valid["raw_dir"] = np.sign(valid[COL])
    traded = valid[valid["raw_dir"] != 0].copy()
    traded["trade_dir"] = direction_sign * traded["raw_dir"]
    traded["net_pts"] = traded["trade_dir"] * traded["return_pts"] - COST
    return traded[["date", "net_pts"]].reset_index(drop=True)

oos_trades = backtest(panel_oos)
print(f"\nOOS n_fills={len(oos_trades)} mean={oos_trades['net_pts'].mean():.4f} "
      f"median={oos_trades['net_pts'].median():.2f} total={oos_trades['net_pts'].sum():.1f} "
      f"win_rate={(oos_trades['net_pts']>0).mean():.4f}")

net = oos_trades["net_pts"].to_numpy()
t, p = stats.ttest_1samp(net, 0.0)
print(f"plain t-test: t={t:.4f} p={p:.5f}")

for lag in (1, 5, 10):
    m = sm.OLS(net, np.ones((len(net), 1))).fit(cov_type="HAC", cov_kwds={"maxlags": lag})
    print(f"statsmodels HAC(maxlags={lag}): coef={m.params[0]:.4f} t={m.tvalues[0]:.4f} p={m.pvalues[0]:.5f}")

# Manual Newey-West variance of the mean, independent implementation (no statsmodels)
def manual_nw_se(x, maxlag):
    xn = x - x.mean()
    T = len(xn)
    gamma0 = np.sum(xn * xn) / T
    s = gamma0
    for lag in range(1, maxlag + 1):
        w = 1 - lag / (maxlag + 1)
        gamma = np.sum(xn[lag:] * xn[:-lag]) / T
        s += 2 * w * gamma
    var_mean = s / T
    return np.sqrt(var_mean)

for lag in (1, 5, 10):
    se = manual_nw_se(net, lag)
    tstat = net.mean() / se
    pval = 2 * (1 - stats.t.cdf(abs(tstat), df=len(net) - 1))
    pval_norm = 2 * (1 - stats.norm.cdf(abs(tstat)))
    print(f"manual NW(maxlag={lag}): se={se:.4f} t={tstat:.4f} p(t-dist,df=n-1)={pval:.5f} p(normal)={pval_norm:.5f}")
