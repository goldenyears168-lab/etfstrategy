"""DECISIVE TRADABILITY TEST — revenue family (rev_yoy_3m / rev_mom / rev_surprise /
equal-weight composite) under REAL Taiwan trading costs + MARKET-NEUTRAL form.

Question this settles: after real 台股 cost (賣方交易稅 0.3% + 手續費 0.1425%×0.4折 雙邊
+ 分層滑價) and monthly-rebalanced dollar-neutral L/S, does the rev family keep a
POSITIVE net Sharpe and pass Deflated-Sharpe? Is it a tradable stock-selection tilt
or a paper signal the costs eat? Does the mid-small concentration self-cancel against
its higher cost?

Design (iron rules):
  - Universe: 418 FinMind adj-close names with monthly revenue (already top-liquid).
    Size split by full-sample median daily 成交值: LARGE = top tercile, MIDSMALL = rest.
  - PIT: every revenue feature indexed by TEJ EWSALE annd (公告日), daily ffilled.
  - Form: monthly rebalance (last trading day → weights, entered t+1). Dollar-neutral
    top-decile long / bottom-decile short, equal weight, gross 1.0 (0.5/0.5), net 0.
  - Cost per rebalance (signed, position level):
      commission 0.1425%×0.4 = 0.0570%/side on ALL traded notional (buy & sell)
      slippage tier: LARGE 10bps/side, MIDSMALL 25bps/side on traded notional
      transaction tax 0.30% on the SELL notional only (現股 賣方稅)
  - IS/OOS 70/30 time split; annualized net Sharpe, CAGR, maxDD; annual turnover;
    Deflated-Sharpe with honest n_trials (forms×universes×2 directions); capacity from
    top-10 weights vs recent ADV; champion/market collinearity (corr vs TAIEX ret).
  - Gross (no-cost) reported alongside net to show how much cost eats.

Falsification-first: direction fixed on IS Rank-IC. Not investment advice.
"""
from __future__ import annotations
from pathlib import Path
import numpy as np
import pandas as pd
from scipy import stats

ROOT = Path(__file__).resolve().parents[3]
D = ROOT / "data" / "research" / "dashboard"
OUTCSV = ROOT / "reports" / "research" / "dashboard-completeness" / "rev_family_tradability_metrics.csv"
RNG = np.random.default_rng(42)

# ---- real Taiwan cost constants ----
COMM_SIDE = 0.001425 * 0.4          # 手續費 0.1425% × 0.4 折 per side  = 0.0570%
TAX_SELL  = 0.003                    # 現股 賣方交易稅 0.30% on sells
SLIP = {'LARGE': 0.0010, 'MIDSMALL': 0.0025}   # 半價差/滑價 per side
DECILE = 0.10
YOY_CLIP = (-90.0, 300.0)

# ---------------- load ----------------
pb = pd.read_parquet(D/'tej_revfam_pb.parquet')
sale = pd.read_parquet(D/'tej_revfam_sale.parquet')
adv = pd.read_parquet(D/'tej_revfam_advalue.parquet')
pb['date'] = pd.to_datetime(pb['date']); pb['close_adj'] = pd.to_numeric(pb['close_adj'], errors='coerce')
adv['date'] = pd.to_datetime(adv['date']); adv['tval'] = pd.to_numeric(adv['tval'], errors='coerce')
sale['annd'] = pd.to_datetime(sale['annd']); sale['period'] = pd.to_datetime(sale['period'])
sale['rev'] = pd.to_numeric(sale['rev'], errors='coerce'); sale['rev_yoy'] = pd.to_numeric(sale['rev_yoy'], errors='coerce')
sale = sale.dropna(subset=['annd','period']).sort_values(['stock_id','period'])

px = pb.pivot(index='date', columns='stock_id', values='close_adj').sort_index()
dates = px.index
stocks = list(px.columns)
ret = px.pct_change()

# daily trading value matrix (TWD)
tval = adv.pivot(index='date', columns='stock_id', values='tval').reindex(dates).reindex(columns=stocks)

# ---- size buckets by full-sample median daily 成交值 ----
med_adv = tval.median(axis=0)  # per stock median daily trading value
q_hi = med_adv.quantile(2/3)
size_bucket = {s: ('LARGE' if med_adv.get(s, np.nan) >= q_hi else 'MIDSMALL') for s in stocks}
slip_vec = pd.Series({s: SLIP[size_bucket[s]] for s in stocks})

# ---------------- build PIT daily revenue features ----------------
def daily_ffill(per_stock):
    mat = pd.DataFrame(index=dates, columns=stocks, dtype=float)
    for sid, s in per_stock.items():
        if sid not in stocks or s is None or len(s) == 0:
            continue
        s = s[~s.index.duplicated(keep='last')].sort_index()
        mat[sid] = s.reindex(dates, method='ffill')
    return mat.astype(float)

yoy3m = {}; mom = {}; surprise = {}
for sid, g in sale.groupby('stock_id'):
    if sid not in stocks:
        continue
    g = g.sort_values('period')
    annd = pd.to_datetime(g['annd'].values)
    rev = g['rev'].astype(float).values
    yoy = g['rev_yoy'].clip(*YOY_CLIP).astype(float).values
    n = len(g)
    # rev_yoy_3m
    r3 = pd.Series(yoy).rolling(3).mean().values
    yoy3m[sid] = pd.Series(r3, index=annd)
    # MoM 環比
    with np.errstate(divide='ignore', invalid='ignore'):
        mm = rev[1:]/rev[:-1] - 1.0
    mm = np.concatenate([[np.nan], mm]); mm[~np.isfinite(mm)] = np.nan
    mom[sid] = pd.Series(mm, index=annd)
    # surprise vs trailing-12m log-linear trend (excl current), standardized
    sur = np.full(n, np.nan); lrev = np.log(np.where(rev > 0, rev, np.nan))
    for i in range(n):
        if i-12 < 0:
            continue
        yv = lrev[i-12:i]; mfin = np.isfinite(yv)
        if mfin.sum() < 10:
            continue
        xh = np.arange(len(yv)); b = np.polyfit(xh[mfin], yv[mfin], 1)
        pred = np.polyval(b, len(yv)); resid = yv[mfin] - np.polyval(b, xh[mfin])
        sd = resid.std(ddof=1)
        if not np.isfinite(lrev[i]) or sd == 0 or not np.isfinite(sd):
            continue
        sur[i] = (lrev[i]-pred)/sd
    surprise[sid] = pd.Series(sur, index=annd)

YOY3M = daily_ffill(yoy3m); MOM = daily_ffill(mom); SUR = daily_ffill(surprise)

# composite = equal-weight cross-sectional rank of the three legs (orthogonal per prior finding)
def xs_rank(mat):
    return mat.rank(axis=1, pct=True)
COMPO = (xs_rank(YOY3M) + xs_rank(MOM) + xs_rank(SUR)) / 3.0

FORMS = {'rev_yoy_3m': YOY3M, 'rev_mom': MOM, 'rev_surprise': SUR, 'composite': COMPO}

# ---------------- machinery ----------------
valid = dates[dates >= pd.Timestamp('2021-04-01')]
split = valid[int(len(valid)*0.70)]

# monthly rebalance dates = last trading day of each month
_m = pd.Series(dates, index=dates).groupby([dates.year, dates.month]).max().values
rebal_dates = pd.DatetimeIndex(sorted(pd.DatetimeIndex(_m)))

def sharpe(x):
    x = x.dropna()
    return np.nan if len(x) < 20 or x.std() == 0 else x.mean()/x.std()*np.sqrt(252)

def maxdd(x):
    x = x.fillna(0.0)
    eq = (1+x).cumprod()
    return float((eq/eq.cummax() - 1).min())

def cagr(x):
    x = x.dropna()
    if len(x) < 20:
        return np.nan
    yrs = len(x)/252.0
    tot = (1+x).prod()
    return float(tot**(1/yrs) - 1) if tot > 0 else np.nan

def build_weights(sig, direction, universe_mask, q=DECILE):
    """Monthly dollar-neutral top/bottom decile weights within a universe subset.
    universe_mask: bool Series over stocks (which stocks are eligible)."""
    elig = pd.Series(universe_mask, index=stocks)
    s_all = sig * direction
    # weights only defined on rebalance dates, then held (ffill) until next rebalance
    W = pd.DataFrame(0.0, index=rebal_dates, columns=stocks)
    for d in rebal_dates:
        row = s_all.loc[d].where(elig)
        r = row.rank(pct=True)
        cnt = row.notna().sum()
        if cnt < 20:
            continue
        long = r >= (1-q); short = r <= q
        nl = long.sum(); ns = short.sum()
        if nl == 0 or ns == 0:
            continue
        w = pd.Series(0.0, index=stocks)
        w[long] = 0.5/nl; w[short] = -0.5/ns
        W.loc[d] = w.values
    return W

def portfolio(sig, direction, universe_mask, apply_cost=True, q=DECILE):
    """Return (daily_net_ret Series, turnover_oneway_per_rebal Series, W)."""
    W = build_weights(sig, direction, universe_mask, q)
    # daily held weights: entered t+1 after each rebalance, held to next
    Wd = W.reindex(dates).ffill().shift(1).fillna(0.0)   # shift(1)=t+1 entry
    port_gross = (Wd * ret).sum(axis=1)
    # cost on rebalance execution days (the day weights change = rebal_date+1 entry day)
    cost_daily = pd.Series(0.0, index=dates)
    to_series = pd.Series(0.0, index=rebal_dates)
    prev = pd.Series(0.0, index=stocks)
    for d in rebal_dates:
        w_new = W.loc[d]
        dw = w_new - prev
        traded = dw.abs()                       # one-way traded notional per stock
        sell_notional = (-dw).clip(lower=0.0)   # notional sold (tax base)
        comm = traded.sum() * COMM_SIDE
        slip = (traded * slip_vec).sum()
        tax = sell_notional.sum() * TAX_SELL
        c = comm + slip + tax
        to_series[d] = traded.sum()
        # charge on the entry day (first trading day after d)
        after = dates[dates > d]
        if len(after) > 0:
            cost_daily[after[0]] += c
        prev = w_new
    port_net = port_gross - (cost_daily if apply_cost else 0.0)
    return port_net, to_series, W, port_gross

def dsr(port, n_trials, sr_trials):
    x = port.dropna(); x = x[x != 0]; T = len(x)
    if T < 30:
        return np.nan
    sr = x.mean()/x.std()
    sk = stats.skew(x); ku = stats.kurtosis(x, fisher=False)
    var_sr = np.var([s for s in sr_trials if np.isfinite(s)], ddof=1) if len(sr_trials) > 1 else (1.0/T)
    if var_sr <= 0:
        var_sr = 1.0/T
    N = max(n_trials, 2); e = np.e; g = 0.5772156649
    sr0 = np.sqrt(var_sr)*((1-g)*stats.norm.ppf(1-1.0/N)+g*stats.norm.ppf(1-1.0/(N*e)))
    denom = np.sqrt(1 - sk*sr + (ku-1)/4.0*sr**2)
    z = (sr-sr0)*np.sqrt(T-1)/denom
    return float(stats.norm.cdf(z))

def rank_ic_is(sig):
    ics = []
    fwd = px.shift(-21)/px - 1   # monthly-horizon fwd for direction sniff
    for d in rebal_dates:
        if d >= split:
            continue
        a = sig.loc[d]; b = fwd.loc[d]; m = a.notna() & b.notna()
        if m.sum() >= 20:
            ics.append(a[m].rank().corr(b[m].rank()))
    return np.nanmean(ics) if ics else 0.0

# market/champion collinearity proxy = TAIEX daily return
try:
    pan = pd.read_parquet(ROOT/'data'/'research'/'chip_macro'/'panel.parquet')
    pan['date'] = pd.to_datetime(pan['date'])
    mkt = pan.set_index('date')['ix_close'].pct_change().reindex(dates)
except Exception:
    mkt = pd.Series(np.nan, index=dates)

UNIVERSES = {
    'ALL':      pd.Series(True, index=stocks),
    'LARGE':    pd.Series({s: size_bucket[s]=='LARGE' for s in stocks}),
    'MIDSMALL': pd.Series({s: size_bucket[s]=='MIDSMALL' for s in stocks}),
}

# direction per form (falsification-first, IS IC on ALL universe)
DIRS = {}
for name, sig in FORMS.items():
    ic = rank_ic_is(sig)
    DIRS[name] = 1.0 if ic >= 0 else -1.0

# ---- first pass: collect Sharpe trials for DSR var estimate ----
sr_trials = []
cache = {}
for name, sig in FORMS.items():
    for uname, umask in UNIVERSES.items():
        net, to, W, gross = portfolio(sig, DIRS[name], umask.values, apply_cost=True)
        cache[(name, uname)] = (net, to, W, gross)
        x = net.dropna(); x = x[x != 0]
        if len(x) >= 30:
            sr_trials.append(x.mean()/x.std())
N_TRIALS = len(FORMS) * len(UNIVERSES) * 2   # forms × universes × 2 direction choices

# ---- capacity helper: recent ADV (last 60 trading days median) ----
recent_adv = tval.tail(60).median(axis=0)   # TWD/day per stock

def capacity(W, frac_adv=0.10):
    """Max AUM (TWD) so that no single position on the latest rebalance exceeds
    frac_adv of that stock's recent daily 成交值. Position notional = |w| × AUM."""
    w_last = W.loc[W.abs().sum(axis=1) > 0].iloc[-1].abs()
    w_last = w_last[w_last > 0]
    caps = []
    for s, w in w_last.items():
        a = recent_adv.get(s, np.nan)
        if np.isfinite(a) and w > 0:
            caps.append(frac_adv * a / w)   # AUM s.t. w*AUM = frac*ADV
    return float(np.min(caps)) if caps else np.nan

# ---------------- evaluate ----------------
rows = []
for name, sig in FORMS.items():
    for uname, umask in UNIVERSES.items():
        net, to, W, gross = cache[(name, uname)]
        is_n = net[net.index < split]; oos_n = net[net.index >= split]
        is_g = gross[gross.index < split]; oos_g = gross[gross.index >= split]
        # annual turnover: sum one-way traded per year, /2 = conventional replaced fraction
        yrs = (dates.max()-dates.min()).days/365.25
        ann_turn = float(to.sum()/2.0/yrs)
        # permutation OOS on net (shuffle signal across stocks each rebal day, keep exposure)
        obs = sharpe(oos_n); cnt = 0; Nperm = 300
        base = (sig*DIRS[name]).copy()
        for _ in range(Nperm):
            arr = base.copy()
            for d in rebal_dates:
                if d < split:
                    continue
                row = arr.loc[d]; ix = row.index[row.notna()]
                if len(ix) > 1:
                    arr.loc[d, ix] = RNG.permutation(row[ix].values)
            pnet, _, _, _ = portfolio(arr, 1.0, umask.values, apply_cost=True)
            if sharpe(pnet[pnet.index >= split]) >= obs:
                cnt += 1
        perm_p = (cnt+1)/(Nperm+1)
        dsr_net = dsr(net, N_TRIALS, sr_trials)
        cap = capacity(W)
        mkt_corr = float(net.reindex(dates).corr(mkt))
        cost_drag = float((gross.dropna().mean()-net.dropna().mean())*252)  # annualized cost drag
        rows.append(dict(
            form=name, universe=uname, dir=int(DIRS[name]),
            sharpe_is_gross=round(sharpe(is_g),3), sharpe_oos_gross=round(sharpe(oos_g),3),
            sharpe_is_net=round(sharpe(is_n),3), sharpe_oos_net=round(sharpe(oos_n),3),
            ann_ret_net=round(cagr(net),4), maxdd_net=round(maxdd(net),4),
            perm_p_oos=round(perm_p,4), dsr_net=round(dsr_net,4) if np.isfinite(dsr_net) else np.nan,
            ann_turnover=round(ann_turn,2), cost_drag_ann=round(cost_drag,4),
            capacity_musd=round(cap/1e6/32.0,1) if np.isfinite(cap) else np.nan,  # ~USD mn (32 TWD/USD)
            capacity_twd_mn=round(cap/1e6,0) if np.isfinite(cap) else np.nan,
            mkt_corr=round(mkt_corr,3),
            n_stocks=int(umask.sum()),
        ))
        print(f"{name:12s} {uname:9s} netSh IS {sharpe(is_n):+.2f} OOS {sharpe(oos_n):+.2f} | "
              f"grossOOS {sharpe(oos_g):+.2f} | turn {ann_turn:.1f}x drag {cost_drag*100:+.1f}%/y | "
              f"perm {perm_p:.3f} DSR {dsr_net:.3f} cap {cap/1e6:.0f}M mktρ {mkt_corr:+.2f}", flush=True)

out = pd.DataFrame(rows)
OUTCSV.parent.mkdir(parents=True, exist_ok=True)
out.to_csv(OUTCSV, index=False)
print(f"\nsplit={split.date()} N_TRIALS={N_TRIALS} n_stocks={len(stocks)} "
      f"LARGE={sum(1 for s in stocks if size_bucket[s]=='LARGE')} "
      f"MIDSMALL={sum(1 for s in stocks if size_bucket[s]=='MIDSMALL')}")
print(f"median ADV LARGE-cut={q_hi/1e8:.2f}億/day")
print(f"wrote {OUTCSV}")
