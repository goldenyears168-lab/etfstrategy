"""
Design B: beta-corrected residual-return slope feature for dayflip post-dump-long.

For each candidate trade (fgap >= 4% subset, n=219):
  1. Estimate alpha/beta via OLS of stock daily return on 0050 daily return,
     using the 60 trading days strictly BEFORE t0 (PIT-safe: known well before
     entry on entry_day = t0+1).
  2. Using 1-min bars for the stock and 0050 on entry_day, compute rolling
     15-minute returns at each available minute, then residual(t) =
     stock_ret15(t) - (alpha_scaled + beta * mkt_ret15(t)), where
     alpha_scaled = alpha * 15/390.
  3. Within the 15-minute window immediately preceding entry_minute (inclusive),
     collect residual(t) values and fit a linear regression of residual vs.
     minute-index; the slope is the feature.
  4. IC (Spearman) of feature vs. trade ret, full-sample + walk-forward (70/30
     by entry_day+entry_minute order) + permutation test (3000 resamples).
"""
import json
import sqlite3
from datetime import datetime, timedelta

import numpy as np
from scipy import stats

import stock_db
from stock_db.kbar import load_kbar_day_bars

DATA_PATH = "reports/research/dayflip_fgap_calibration/post_dump_long_rolling_dip_results.json"
MIN_DAILY_OBS = 30  # minimum aligned daily-return pairs to trust beta/alpha
LOOKBACK_DAYS = 60
WINDOW_MIN = 15
MIN_RESID_POINTS = 5  # minimum residual(t) points in-window to fit a slope

con = sqlite3.connect(f"file:{stock_db.DEFAULT_DB_PATH}?mode=ro", uri=True)
con.row_factory = sqlite3.Row


def daily_closes_before(stock_id: str, before_date: str, limit: int = 200):
    cur = con.execute(
        """
        SELECT trade_date, close FROM stock_daily_bars
        WHERE stock_id = ? AND source = 'finmind' AND trade_date < ?
        ORDER BY trade_date DESC LIMIT ?
        """,
        (stock_id, before_date, limit),
    )
    rows = cur.fetchall()
    rows = list(reversed(rows))  # ascending
    return {r["trade_date"]: r["close"] for r in rows}


def estimate_alpha_beta(stock_id: str, t0: str):
    stock_px = daily_closes_before(stock_id, t0, limit=LOOKBACK_DAYS + 5)
    mkt_px = daily_closes_before("0050", t0, limit=LOOKBACK_DAYS + 5)
    common_dates = sorted(set(stock_px) & set(mkt_px))
    if len(common_dates) < MIN_DAILY_OBS + 1:
        return None
    stock_series = [stock_px[d] for d in common_dates]
    mkt_series = [mkt_px[d] for d in common_dates]
    stock_ret = np.diff(stock_series) / np.array(stock_series[:-1])
    mkt_ret = np.diff(mkt_series) / np.array(mkt_series[:-1])
    if len(stock_ret) > LOOKBACK_DAYS:
        stock_ret = stock_ret[-LOOKBACK_DAYS:]
        mkt_ret = mkt_ret[-LOOKBACK_DAYS:]
    if len(stock_ret) < MIN_DAILY_OBS:
        return None
    # OLS: stock_ret = alpha + beta * mkt_ret
    X = np.vstack([np.ones_like(mkt_ret), mkt_ret]).T
    coef, *_ = np.linalg.lstsq(X, stock_ret, rcond=None)
    alpha, beta = coef[0], coef[1]
    return alpha, beta, len(stock_ret)


_kbar_cache: dict = {}


def get_minute_closes(stock_id: str, trade_date: str):
    key = (stock_id, trade_date)
    if key in _kbar_cache:
        return _kbar_cache[key]
    bars = load_kbar_day_bars(con, stock_id, trade_date)
    out = {}
    for b in bars:
        m = b.minute[:5]
        if "09:00" <= m <= "13:30":
            out[m] = b.close
    _kbar_cache[key] = out
    return out


def to_dt(hhmm: str):
    return datetime.strptime(hhmm, "%H:%M")


def rolling_15min_return(closes: dict, minute_str: str):
    t = to_dt(minute_str)
    t_prev = (t - timedelta(minutes=WINDOW_MIN)).strftime("%H:%M")
    if minute_str not in closes or t_prev not in closes:
        return None
    c0 = closes[t_prev]
    c1 = closes[minute_str]
    if c0 == 0:
        return None
    return c1 / c0 - 1.0


def compute_feature(record):
    stock_id = record["stock_id"]
    t0 = record["t0"]
    entry_day = record["entry_day"]
    entry_minute = record["entry_minute"]

    ab = estimate_alpha_beta(stock_id, t0)
    if ab is None:
        return None
    alpha, beta, n_daily = ab
    alpha_scaled = alpha * 15.0 / 390.0

    stock_closes = get_minute_closes(stock_id, entry_day)
    mkt_closes = get_minute_closes("0050", entry_day)
    if not stock_closes or not mkt_closes:
        return None

    entry_dt = to_dt(entry_minute)
    window_start_dt = entry_dt - timedelta(minutes=WINDOW_MIN)

    resid_points = []
    t = window_start_dt
    idx = 0
    while t <= entry_dt:
        m = t.strftime("%H:%M")
        s_r15 = rolling_15min_return(stock_closes, m)
        mk_r15 = rolling_15min_return(mkt_closes, m)
        if s_r15 is not None and mk_r15 is not None:
            resid = s_r15 - (alpha_scaled + beta * mk_r15)
            resid_points.append((idx, resid))
        t += timedelta(minutes=1)
        idx += 1

    if len(resid_points) < MIN_RESID_POINTS:
        return None

    xs = np.array([p[0] for p in resid_points], dtype=float)
    ys = np.array([p[1] for p in resid_points], dtype=float)
    slope, intercept, r, p, se = stats.linregress(xs, ys)

    return {
        "stock_id": stock_id,
        "t0": t0,
        "entry_day": entry_day,
        "entry_minute": entry_minute,
        "alpha": alpha,
        "beta": beta,
        "n_daily": n_daily,
        "n_resid_points": len(resid_points),
        "resid_slope": slope,
        "ret": record["ret"],
    }


def main():
    data = json.load(open(DATA_PATH))
    sub = [d for d in data if d["fgap"] >= 4]
    print(f"candidate n = {len(sub)}")

    results = []
    for r in sub:
        feat = compute_feature(r)
        if feat is not None:
            results.append(feat)

    print(f"usable n = {len(results)}")
    if len(results) < 20:
        print("too few usable records, aborting")
        return

    # sort by entry_day, entry_minute for walk-forward split
    results.sort(key=lambda x: (x["entry_day"], x["entry_minute"]))

    slopes = np.array([r["resid_slope"] for r in results])
    rets = np.array([r["ret"] for r in results])

    def ic_stats(x, y):
        rho, p = stats.spearmanr(x, y)
        return rho, p

    full_ic, full_p = ic_stats(slopes, rets)

    n = len(results)
    split = int(n * 0.7)
    train_slopes, test_slopes = slopes[:split], slopes[split:]
    train_rets, test_rets = rets[:split], rets[split:]

    train_ic, train_p = ic_stats(train_slopes, train_rets)
    test_ic, test_p = ic_stats(test_slopes, test_rets)

    # permutation test on full sample
    rng = np.random.default_rng(42)
    n_perm = 3000
    perm_ics = np.empty(n_perm)
    y = rets.copy()
    for i in range(n_perm):
        y_perm = rng.permutation(y)
        rho, _ = stats.spearmanr(slopes, y_perm)
        perm_ics[i] = rho
    perm_p = np.mean(np.abs(perm_ics) >= np.abs(full_ic))

    out = {
        "n_candidates": len(sub),
        "n_usable": n,
        "full_ic": full_ic,
        "full_p": full_p,
        "train_n": split,
        "train_ic": train_ic,
        "train_p": train_p,
        "test_n": n - split,
        "test_ic": test_ic,
        "test_p": test_p,
        "perm_p": perm_p,
    }
    print(json.dumps(out, indent=2))

    with open("/tmp/dayflip_designB_result.json", "w") as f:
        json.dump({"summary": out, "records": results}, f, indent=2, default=str)


if __name__ == "__main__":
    main()
