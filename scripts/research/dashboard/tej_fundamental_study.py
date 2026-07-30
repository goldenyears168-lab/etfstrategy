"""TEJ fundamental cross-sectional factor study (dashboard dimension: tej_fundamental_factor).

Factors (all PIT via TEJ announcement dates, forward-filled to daily):
  1. rev_yoy      月營收YoY動能  (EWSALE rev_yoy, latest announced, annd-gated)
  2. rev_yoy_3m   月營收YoY 3個月均 (smoother momentum)
  3. rev_accel    月營收YoY 加速度 (latest - prior-3m mean)
  4. roe          ROE(A)稅後 品質  (EWIFINQ, annd-gated)
  5. pb_inv       1/PB 價值 (low PB = cheap = value; EWPRCD daily pb_ratio)
  6. composite    方向對齊後 z-score 等權合成

Design (mirrors relative_strength_study.py methodology):
  - daily cross-sectional rank on 90 liquid stocks, long top 20% / short bottom 20%,
    equal-weight, weekly (5d) rebalance, t+1 entry, 4bps/side * turnover cost.
  - direction fixed by sign of IS Rank-IC (falsification-first).
  - IS/OOS 70/30 time split; permutation (shuffle signal across stocks/day, 1000x, OOS Sharpe one-sided p).
  - regime split: bull = ix > rising MA200.
  - de-champion collinearity: corr(factor L/S daily ret, champion fut_foreign_oi z60 market-timing pnl).
  - regime-conditioned selection: trade only on days champion green (z60>0) AND bull.
"""
from __future__ import annotations
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
D = ROOT / "data" / "research" / "dashboard"
OUTCSV = ROOT / "reports" / "research" / "dashboard-completeness" / "tej_fundamental_metrics.csv"
RNG = np.random.default_rng(42)
COST = 0.0004  # 4bps per side

# ---------- load ----------
pb = pd.read_parquet(D/'tej_fundamental_pb.parquet')      # daily close_adj, pb_ratio
sale = pd.read_parquet(D/'tej_fundamental_sale.parquet')  # monthly rev_yoy + annd
fin = pd.read_parquet(D/'tej_fundamental_fin.parquet')    # quarterly roe + annd
panel = pd.read_parquet(D.parent/'chip_macro'/'panel.parquet')  # ix_close, fut_foreign_oi

pb['date'] = pd.to_datetime(pb['date'])
sale['annd'] = pd.to_datetime(sale['annd']); sale['period']=pd.to_datetime(sale['period'])
fin['annd'] = pd.to_datetime(fin['annd'])
panel['date'] = pd.to_datetime(panel['date'])

# price/return matrix (adjusted)
px = pb.pivot(index='date', columns='stock_id', values='close_adj').sort_index()
pbmat = pb.pivot(index='date', columns='stock_id', values='pb_ratio').sort_index()
dates = px.index
stocks = list(px.columns)
ret = px.pct_change()  # daily return, ret.loc[t] = t-1->t

# ---------- PIT forward-fill fundamentals onto trading dates ----------
def pit_daily(df, valcol, annd='annd'):
    """For each stock, on each trading date use the latest value whose annd <= date."""
    out = pd.DataFrame(index=dates, columns=stocks, dtype=float)
    for sid, g in df.groupby('stock_id'):
        if sid not in out.columns: continue
        g = g.dropna(subset=[valcol]).sort_values(annd)
        if len(g)==0: continue
        s = pd.Series(g[valcol].values, index=pd.to_datetime(g[annd].values))
        s = s[~s.index.duplicated(keep='last')].sort_index()
        out[sid] = s.reindex(dates, method='ffill')
    return out.astype(float)

# revenue YoY: latest announced monthly YoY, plus 3m smoothed & acceleration
# build per-stock monthly series (by annd) then daily ffill
def rev_features():
    latest = pd.DataFrame(index=dates, columns=stocks, dtype=float)
    sm3 = pd.DataFrame(index=dates, columns=stocks, dtype=float)
    accel = pd.DataFrame(index=dates, columns=stocks, dtype=float)
    for sid, g in sale.groupby('stock_id'):
        if sid not in stocks: continue
        g = g.dropna(subset=['rev_yoy']).sort_values('annd')
        if len(g)==0: continue
        yoy = g['rev_yoy'].astype(float).values
        annd = pd.to_datetime(g['annd'].values)
        roll3 = pd.Series(yoy).rolling(3).mean().values
        prior3 = pd.Series(yoy).shift(1).rolling(3).mean().values
        acc = yoy - prior3
        for mat, arr in [(latest,yoy),(sm3,roll3),(accel,acc)]:
            s = pd.Series(arr, index=annd)
            s = s[~s.index.duplicated(keep='last')].sort_index()
            mat[sid] = s.reindex(dates, method='ffill')
    return latest.astype(float), sm3.astype(float), accel.astype(float)

rev_yoy, rev_yoy_3m, rev_accel = rev_features()
roe = pit_daily(fin, 'roe')
pb_inv = 1.0 / pbmat.replace(0, np.nan)  # low PB -> high 1/PB = cheap

def zscore_xs(mat):
    m = mat.sub(mat.mean(axis=1), axis=0)
    return m.div(mat.std(axis=1).replace(0,np.nan), axis=0)

FACTORS = {
    'rev_yoy': rev_yoy, 'rev_yoy_3m': rev_yoy_3m, 'rev_accel': rev_accel,
    'roe': roe, 'pb_inv': pb_inv,
}
# composite = equal-weight z of momentum(rev_yoy_3m)+quality(roe)+value(pb_inv)
composite = (zscore_xs(rev_yoy_3m).fillna(0)+zscore_xs(roe).fillna(0)+zscore_xs(pb_inv).fillna(0))
composite = composite.where(rev_yoy_3m.notna() | roe.notna() | pb_inv.notna())
FACTORS['composite'] = composite

# ---------- regime + champion ----------
pn = panel.set_index('date').reindex(dates).ffill()
ma200 = pn['ix_close'].rolling(200).mean()
bull = (pn['ix_close'] > ma200) & (ma200 > ma200.shift(20))
# champion: fut_foreign_oi z60 > 0 -> risk-on; market-timing pnl = pos_shift * ix_ret
x = pn['fut_foreign_oi']
z60 = (x - x.rolling(60).mean()) / x.rolling(60).std()
champ_on = (z60 > 0)
ix_ret = pn['ix_close'].pct_change()
champ_pnl = champ_on.shift(1).astype(float) * ix_ret  # daily champion timing return

# IS/OOS split (70/30 by trading date)
valid = dates[dates >= pd.Timestamp('2021-02-01')]
split = valid[int(len(valid)*0.70)]

def sharpe(x):
    x = x.dropna()
    return np.nan if x.std()==0 or len(x)<20 else x.mean()/x.std()*np.sqrt(252)

_block = np.arange(len(dates)) // 5              # weekly block id
_bstart_pos = np.array([np.where(_block==b)[0][0] for b in np.unique(_block)])
_bstart_dates = dates[_bstart_pos]

def longshort_ret(sig, direction, mask=None, q=0.2):
    """Vectorized equal-weight L/S: rank cross-section at weekly block-start, hold 5d,
    t+1 entry (weights.shift(1)*ret), 4bps/side*turnover cost."""
    s = sig * direction
    r = s.rank(axis=1, pct=True)                  # per-day cross-sectional percentile
    cnt = s.notna().sum(axis=1)
    long = (r >= (1-q)) & (cnt.values[:,None] >= 10)
    short = (r <= q) & (cnt.values[:,None] >= 10)
    nl = long.sum(axis=1).replace(0,np.nan); ns = short.sum(axis=1).replace(0,np.nan)
    w = long.div(nl,axis=0)*0.5 - short.div(ns,axis=0)*0.5
    w = w.fillna(0.0)
    # keep only block-start rows, forward-fill weights across the 5-day block
    wb = w.reindex(_bstart_dates).reindex(dates).ffill().fillna(0.0)
    turnover = (wb - wb.shift(1).fillna(0.0)).abs().sum(axis=1)
    turnover = turnover.where(dates.isin(_bstart_dates), 0.0)
    port = (wb.shift(1) * ret).sum(axis=1) - turnover*COST
    if mask is not None:
        port = port.where(mask.reindex(dates).fillna(False), 0.0)
    return port

def rank_ic(sig, fwd):
    ics=[]
    for d in dates:
        a=sig.loc[d]; b=fwd.loc[d]
        m=a.notna()&b.notna()
        if m.sum()>=10:
            ics.append(a[m].rank().corr(b[m].rank()))
    return pd.Series(ics, index=dates[:len(ics)]) if ics else pd.Series(dtype=float)

fwd5 = px.shift(-5)/px - 1  # forward 5d return for IC
rows=[]
sig_dir={}
for name, sig in FACTORS.items():
    ic_all = rank_ic(sig, fwd5)
    ic_is = ic_all[ic_all.index < split].mean()
    ic_oos = ic_all[ic_all.index >= split].mean()
    direction = 1.0 if (ic_is if not np.isnan(ic_is) else 0) >= 0 else -1.0
    sig_dir[name]=direction
    port = longshort_ret(sig, direction)
    is_p = port[port.index < split]; oos_p = port[port.index >= split]
    sh_is, sh_oos = sharpe(is_p), sharpe(oos_p)
    sh_bull = sharpe(port.where(bull,0.0)); sh_bear = sharpe(port.where(~bull,0.0))
    ret_ann_is = is_p.mean()*252*100; ret_ann_oos=oos_p.mean()*252*100
    # permutation on OOS: shuffle signal across stocks each day, keep exposure
    obs = sh_oos; cnt=0; N=500
    oos_mask = (dates>=split)
    for _ in range(N):
        perm = sig.copy()
        arr = perm.values.copy()
        for r in np.where(oos_mask)[0]:
            row=arr[r]; idx=np.where(~np.isnan(row))[0]
            if len(idx)>1: arr[r, idx]=RNG.permutation(row[idx])
        perm = pd.DataFrame(arr, index=dates, columns=stocks)
        pp=longshort_ret(perm, direction)
        pp=pp[pp.index>=split]
        if sharpe(pp)>=obs: cnt+=1
    perm_p=(cnt+1)/(N+1)
    # champion collinearity
    corr_champ = port.corr(champ_pnl)
    rows.append(dict(factor=name, dir=int(direction), ic_is=round(ic_is,4), ic_oos=round(ic_oos,4),
        ret_ann_is=round(ret_ann_is,3), ret_ann_oos=round(ret_ann_oos,3),
        sharpe_is=round(sh_is,3), sharpe_oos=round(sh_oos,3),
        sharpe_bull=round(sh_bull,3), sharpe_bear=round(sh_bear,3),
        perm_p_oos=round(perm_p,4), corr_champ=round(corr_champ,3)))
    print(f"{name:12s} dir={int(direction):+d} IC_is={ic_is:+.4f} IC_oos={ic_oos:+.4f} "
          f"Sh_is={sh_is:+.2f} Sh_oos={sh_oos:+.2f} bull={sh_bull:+.2f} bear={sh_bear:+.2f} "
          f"p={perm_p:.3f} corrChamp={corr_champ:+.2f}")

# ---------- regime-conditioned selection: composite, only champ-green & bull days ----------
comp = FACTORS['composite']
port_comp = longshort_ret(comp, sig_dir['composite'])
gate = champ_on & bull
port_gate = port_comp.where(gate.reindex(dates).fillna(False), 0.0)
oos_gate = port_gate[port_gate.index>=split]
print(f"\ncomposite gated(champ-green x bull): Sh_all={sharpe(port_gate):+.2f} Sh_oos={sharpe(oos_gate):+.2f} "
      f"days_on={int(gate.reindex(dates).fillna(False).sum())}/{len(dates)}")
# baseline: equal-weight universe B&H
bh = ret.mean(axis=1)
print(f"universe B&H Sharpe all={sharpe(bh):+.2f} oos={sharpe(bh[bh.index>=split]):+.2f}")

df = pd.DataFrame(rows)
df.to_csv(OUTCSV, index=False)
# append gated + baseline rows
extra = pd.DataFrame([
    dict(factor='composite_gated', dir=1, sharpe_is=round(sharpe(port_gate[port_gate.index<split]),3),
         sharpe_oos=round(sharpe(oos_gate),3), sharpe_bull=round(sharpe(port_gate.where(bull,0.0)),3)),
    dict(factor='universe_bh', dir=1, sharpe_oos=round(sharpe(bh[bh.index>=split]),3),
         sharpe_is=round(sharpe(bh[bh.index<split]),3)),
])
pd.concat([df,extra], ignore_index=True).to_csv(OUTCSV, index=False)
print(f"\nsplit IS<{split.date()}<=OOS  |  {len(dates)} trading days {dates[0].date()}->{dates[-1].date()}")
print(f"wrote {OUTCSV}")
