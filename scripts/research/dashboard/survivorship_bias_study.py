"""Survivorship-bias attack (project-global blind spot).

The dashboard-completeness universe (90 hand-picked liquid survivors for the TEJ
fundamental study; the liquid-finmind universe for RS) is 100% survivors. This
script reconstructs the DELISTED cohort of Taiwan 4-digit equities (FinMind
TaiwanStockDelisting) and quantifies how much survivorship inflates the two
factors that scored best in Phase-3:
  - rev_yoy_3m  (TEJ fundamental momentum, DSR OOS 0.967)
  - Mansfield RS (Weinstein Stage RS oscillator)

METHOD (falsification-first, honest terminal accounting)
  1. Delisting list -> 4-digit equities delisted 2018-01+ (RS window) / 2021+ (rev).
  2. FinMind price (raw close, to last trading day) + monthly revenue per name.
  3. Snapshot each delisted name 21 trading days before its LAST price ("still
     alive, pre-death"). Compute Mansfield RSM and rev_yoy_3m at that snapshot.
  4. Assign each to a long/short/neutral leg by ranking its RSM against the 90
     survivors' RSM cross-section on the same date (top20%=long, bot20%=short).
  5. Terminal return = snapshot_close -> last_close (what a holder actually eats).
     NOTE: for bankruptcy/fraud names the exchange halts before residual value
     goes to ~0, so measured losses are LOWER BOUNDS -> any inflation we find is
     a floor, not a ceiling.
  6. Survivorship bias = the P&L contribution these names WOULD have added to the
     RS long-short but which the survivor-only backtest structurally omits:
        long-leg name  -> + terminal_return
        short-leg name -> - terminal_return
     Net negative => survivor backtest OVERSTATES alpha (classic inflation).

  Non-investment-advice: research code only.
"""
from __future__ import annotations
import time, sqlite3
from pathlib import Path
import numpy as np
import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[3]
DB = ROOT / "data" / "stocks.db"
CACHE = ROOT / "data" / "research" / "dashboard" / "survivorship"
CACHE.mkdir(parents=True, exist_ok=True)
OUTDIR = ROOT / "reports" / "research" / "dashboard-completeness"
API = "https://api.finmindtrade.com/api/v4/data"

# 90-stock survivor universe (identical to tej_fundamental_fetch.py)
SURV = ['2330','2454','2603','2317','2303','2327','3037','3661','2408','2308',
 '2382','3017','2344','3443','3231','3481','6669','8046','3008','3711','2337','8299',
 '2313','2383','2345','3034','3105','2368','6488','3189','2376','2618','2409','2449',
 '4958','6274','1519','2301','2357','3665','2882','1303','2881','3653','2379','2891',
 '5274','6415','3529','6223','2002','5347','2412','3293','3533','2360','2356','2059',
 '6239','3081','2404','1101','5871','6139','2884','1590','2886','2883','1216','4938',
 '2887','3044','2885','3045','3036','2892','2890','2912','2395','4904','6505','3702',
 '6269','2880','2834','5536','5880','5876','2801','2207']

RSM_WIN = 50   # Mansfield SMA window (trading days)
PRE = 21       # snapshot lookback before last price


def tok():
    for l in open(ROOT/'.env'):
        if l.strip().startswith('FINMIND_TOKEN'):
            return l.split('=',1)[1].strip().strip('"').strip("'")


def fm(dataset, data_id, start='2016-06-01', t=None):
    r = requests.get(API, params={'dataset':dataset,'data_id':data_id,
                                   'start_date':start,'token':t}, timeout=60)
    d = r.json().get('data', [])
    return pd.DataFrame(d) if d else pd.DataFrame()


def get_delist_equities(t):
    f = CACHE/'delisting.parquet'
    if f.exists():
        df = pd.read_parquet(f)
    else:
        df = fm('TaiwanStockDelisting', '', t=t)  # full table (no data_id)
        r = requests.get(API, params={'dataset':'TaiwanStockDelisting','token':t}, timeout=60)
        df = pd.DataFrame(r.json()['data'])
        df.to_parquet(f, index=False)
    df['date'] = pd.to_datetime(df['date'])
    eq = df[df.stock_id.str.match(r'^[1-9]\d{3}$')].copy()   # 4-digit real equities
    eq = eq[eq.date >= '2018-01-01'].sort_values('date').reset_index(drop=True)
    return eq


def load_prices(ids, t):
    """FinMind raw close per delisted id, cached."""
    out = {}
    for i, sid in enumerate(ids):
        f = CACHE/f'px_{sid}.parquet'
        if f.exists():
            df = pd.read_parquet(f)
        else:
            df = fm('TaiwanStockPrice', sid, t=t)
            df.to_parquet(f, index=False)
            time.sleep(0.25)
        if len(df):
            s = df[['date','close']].copy()
            s['date'] = pd.to_datetime(s['date'])
            out[sid] = s.set_index('date')['close'].sort_index()
    return out


def load_rev(ids, t):
    out = {}
    for sid in ids:
        f = CACHE/f'rev_{sid}.parquet'
        if f.exists():
            df = pd.read_parquet(f)
        else:
            df = fm('TaiwanStockMonthRevenue', sid, t=t)
            df.to_parquet(f, index=False)
            time.sleep(0.25)
        if len(df):
            df['date'] = pd.to_datetime(df['date'])
            out[sid] = df.sort_values('date')
    return out


def rsm_series(px, ix):
    """Mansfield RS oscillator on a raw-close series vs index."""
    px = px[~px.index.duplicated(keep='last')].sort_index()
    rl = (px / ix.reindex(px.index).ffill())
    return (rl / rl.rolling(RSM_WIN).mean() - 1.0) * 100.0


def main():
    t = tok()
    ix = pd.read_sql("select date,close from daily_bars where code='IX0001' order by date",
                     sqlite3.connect(DB))
    ix['date'] = pd.to_datetime(ix['date']); ix = ix.set_index('date')['close']
    ix = ix[~ix.index.duplicated(keep='last')].sort_index()

    eq = get_delist_equities(t)
    print(f"delisted 4-digit equities 2018+: {len(eq)}")

    # ---- survivor RSM cross-section (for leg assignment) ----
    con = sqlite3.connect(DB)
    q = "select stock_id,trade_date,close from stock_daily_bars where source='finmind' and stock_id in (%s)" % ",".join("?"*len(SURV))
    sdf = pd.read_sql(q, con, params=SURV)
    sdf['trade_date'] = pd.to_datetime(sdf['trade_date'])
    spx = sdf.pivot(index='trade_date', columns='stock_id', values='close').sort_index()
    surv_rsm = pd.DataFrame({s: rsm_series(spx[s], ix) for s in spx.columns})

    # ---- delisted cohort ----
    px = load_prices(list(eq.stock_id), t)
    rev = load_rev(list(eq.stock_id[eq.date>='2021-01-01']), t)

    rows = []
    for _, r in eq.iterrows():
        sid = r.stock_id
        if sid not in px or len(px[sid]) < RSM_WIN+PRE+5:
            rows.append(dict(stock_id=sid, name=r.stock_name, delist=r.date,
                             note='insufficient_price')); continue
        s = px[sid]
        rsm = rsm_series(s, ix)
        last_dt = s.index[-1]
        snap_dt = s.index[-1-PRE]
        rsm_pre = rsm.loc[snap_dt]
        term_ret = s.iloc[-1]/s.loc[snap_dt] - 1.0            # snapshot -> last close
        ret_250 = s.iloc[-1]/s.iloc[max(0,len(s)-1-250)] - 1.0
        # leg by percentile vs survivors on snap date
        if snap_dt in surv_rsm.index:
            base = surv_rsm.loc[snap_dt].dropna()
        else:
            base = surv_rsm.reindex([snap_dt], method='ffill').iloc[0].dropna()
        pct = (base < rsm_pre).mean() if len(base) else np.nan
        leg = 'long' if pct>=0.8 else ('short' if pct<=0.2 else 'neutral')
        # rev_yoy_3m at snapshot
        ry3 = np.nan
        if sid in rev:
            rv = rev[sid]
            rv = rv[rv.date <= snap_dt]
            if len(rv) >= 15:
                rv = rv.copy()
                rv['yoy'] = rv['revenue'] / rv['revenue'].shift(12) - 1.0
                ry3 = rv['yoy'].tail(3).mean()
        rows.append(dict(stock_id=sid, name=r.stock_name, delist=r.date,
                         last_price_dt=last_dt, rsm_pre=rsm_pre, rsm_pctile=pct,
                         leg=leg, term_ret=term_ret, ret_250=ret_250,
                         rev_yoy_3m=ry3, note=''))

    res = pd.DataFrame(rows)
    res.to_csv(OUTDIR/'survivorship_cohort.csv', index=False)
    good = res[res.note==''].copy()
    print(f"\nusable delisted names: {len(good)} / {len(res)}")

    # ---- survivorship-bias P&L accounting (RS long-short) ----
    good['contrib'] = np.where(good.leg=='long', good.term_ret,
                        np.where(good.leg=='short', -good.term_ret, 0.0))
    legcount = good.leg.value_counts().to_dict()
    print("\nleg assignment (RSM percentile vs survivors):", legcount)
    print(f"median terminal return (snap->last) all: {good.term_ret.median():+.3f}")
    for lg in ['long','short','neutral']:
        g = good[good.leg==lg]
        if len(g):
            print(f"  {lg:7s} n={len(g):2d}  term_ret mean={g.term_ret.mean():+.3f} "
                  f"median={g.term_ret.median():+.3f}  missed_contrib mean={g.contrib.mean():+.3f}")
    traded = good[good.leg!='neutral']
    print(f"\n== RS survivorship bias ==")
    print(f"traded (long+short) delisted names: {len(traded)}")
    print(f"net missed P&L per traded name (mean contrib): {traded.contrib.mean():+.4f}")
    print(f"total missed P&L (sum contrib): {traded.contrib.sum():+.3f}")
    print("  interpretation: mean<0 => survivor-only RS backtest OVERSTATES alpha")

    # ---- rev_yoy_3m survivorship ----
    rv = good.dropna(subset=['rev_yoy_3m'])
    print(f"\n== rev_yoy_3m survivorship (n={len(rv)} with revenue) ==")
    if len(rv):
        print(f"delisted-cohort rev_yoy_3m at snapshot: mean={rv.rev_yoy_3m.mean():+.3f} "
              f"median={rv.rev_yoy_3m.median():+.3f}")
        hi = rv[rv.rev_yoy_3m > rv.rev_yoy_3m.median()]
        lo = rv[rv.rev_yoy_3m <= rv.rev_yoy_3m.median()]
        print(f"  high-rev-momentum half term_ret mean={hi.term_ret.mean():+.3f} (would be LONGed)")
        print(f"  low-rev-momentum  half term_ret mean={lo.term_ret.mean():+.3f} (would be SHORTed)")
        # correlation rev_yoy_3m vs terminal return among dying names
        c = rv[['rev_yoy_3m','term_ret']].corr().iloc[0,1]
        print(f"  corr(rev_yoy_3m, term_ret) among dying names: {c:+.3f}")

    good.to_csv(OUTDIR/'survivorship_cohort_usable.csv', index=False)
    print(f"\nwrote {OUTDIR/'survivorship_cohort.csv'}")
    return good, traded, rv


if __name__ == '__main__':
    main()
