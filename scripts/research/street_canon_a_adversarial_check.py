"""H-STREET-A 對抗覆核：獨立路徑重算（numpy 實作，不用原腳本的 pandas rolling/pivot 主線）。

檢查：
1. 獨立重算 N2_h5 / N3_h10 的 n_events 與 mean_excess（差 >10% 即 FLAWED）。
2. 2026-07-10 假交易日（台股颱風假、僅 128 列 yfinance 殭屍列）的污染量化，
   以及剔除該日後點估計變化。
3. 事件組負向點估計的月份/個股集中度。

用法::

    PYTHONPATH=src .venv/bin/python scripts/research/street_canon_a_adversarial_check.py
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import statsmodels.api as sm

ROOT = Path(__file__).resolve().parents[2]
CACHE = ROOT / "reports/research/chip-street-canon/cache"

WINDOW_START, TAPE_VALID_END, HOLDOUT_START = "2024-07-01", "2026-07-16", "2026-01-01"
FAKE_DAY = "2026-07-10"


def nw_t(y: np.ndarray, lag: int):
    y = y[~np.isnan(y)]
    if len(y) < 8:
        return (float(np.mean(y)) if len(y) else np.nan, np.nan)
    res = sm.OLS(y, np.ones_like(y)).fit(cov_type="HAC", cov_kwds={"maxlags": lag})
    return float(res.params[0]), float(res.tvalues[0])


def rolling_sum_full(a: np.ndarray, w: int) -> np.ndarray:
    """window w、要求全 w 格非 NaN 的 rolling sum（沿 axis=0）。"""
    valid = ~np.isnan(a)
    filled = np.where(valid, a, 0.0)
    cs = np.vstack([np.zeros((1, a.shape[1])), np.cumsum(filled, axis=0)])
    cv = np.vstack([np.zeros((1, a.shape[1]), dtype=int), np.cumsum(valid, axis=0)])
    out = np.full_like(a, np.nan, dtype=float)
    s = cs[w:] - cs[:-w]
    n = cv[w:] - cv[:-w]
    out[w - 1:] = np.where(n == w, s, np.nan)
    return out


def build(drop_fake: bool):
    top = pd.read_pickle(CACHE / "top15_daily.pkl")
    px = pd.read_pickle(CACHE / "price_panel.pkl")
    if drop_fake:
        px = px[px.trade_date != FAKE_DAY]
    cal = np.sort(px.trade_date.unique())
    stocks = np.sort(px.stock_id.unique())
    di = {d: i for i, d in enumerate(cal)}
    si = {s: i for i, s in enumerate(stocks)}
    T, S = len(cal), len(stocks)

    def grid(df, col, default=np.nan):
        a = np.full((T, S), default)
        rows = df.trade_date.map(di).values
        cols = df.stock_id.map(si).values
        m = ~pd.isna(rows) & ~pd.isna(cols)
        a[rows[m].astype(int), cols[m].astype(int)] = df[col].values[m]
        return a

    open_a = grid(px, "open")
    close_a = grid(px, "close")
    vol = grid(px, "volume") / 1000.0
    vol = np.where(vol > 0, vol, np.nan)
    top_in = top[top.stock_id.isin(si)]
    t15 = grid(top_in, "top15_net")

    num5, den5 = rolling_sum_full(t15, 5), rolling_sum_full(vol, 5)
    conc5 = np.where(np.isnan(den5) | (den5 <= 0), np.nan, num5 / den5)
    num20, den20 = rolling_sum_full(t15, 20), rolling_sum_full(vol, 20)
    conc20 = np.where(np.isnan(den20) | (den20 <= 0), np.nan, num20 / den20)

    d = np.full_like(conc5, np.nan)
    d[1:] = conc5[1:] - conc5[:-1]
    inc = (d > 0) & ~np.isnan(conc5) & ~np.isnan(np.vstack([np.full((1, S), np.nan), conc5[:-1]]))
    streak = np.zeros((T, S), dtype=int)
    for t in range(1, T):
        streak[t] = (streak[t - 1] + 1) * inc[t]
    streak[0] = inc[0].astype(int)
    reso = np.full((T, S), False)
    reso[5:] = (conc20[5:] - conc20[:-5]) > 0

    volf = np.where(np.isnan(vol), 0.0, vol)
    cs = np.vstack([np.zeros((1, S)), np.cumsum(volf, axis=0)])
    vol20 = np.full((T, S), np.nan)
    vol20[19:] = (cs[20:] - cs[:-20]) / 20.0
    univ = (close_a >= 10) & (vol20 > 300) & ~np.isnan(close_a)

    open_next = np.full((T, S), np.nan)
    open_next[:-1] = np.where(open_a[1:] > 0, open_a[1:], np.nan)
    return cal, stocks, streak, reso, univ, open_next, close_a


def run(drop_fake: bool):
    cal, stocks, streak, reso, univ, open_next, close_a = build(drop_fake)
    T, S = len(cal), len(stocks)
    win = (cal >= WINDOW_START) & (cal <= TAPE_VALID_END)
    hold = (cal >= HOLDOUT_START) & (cal <= TAPE_VALID_END)
    out = {}
    for N, h in [(2, 5), (3, 10)]:
        ret = np.full((T, S), np.nan)
        ret[:-h] = close_a[h:] / open_next[:-h] - 1.0
        valid = univ & ~np.isnan(ret)
        ev = (streak >= N) & reso & univ & valid & win[:, None]
        r = np.where(valid, ret, np.nan)
        sum_u = np.nansum(r, axis=1)
        cnt_u = valid.sum(axis=1)
        sum_e = np.nansum(np.where(ev, r, np.nan), axis=1)
        cnt_e = ev.sum(axis=1)
        with np.errstate(invalid="ignore", divide="ignore"):
            raw = np.where(cnt_e > 0, sum_e / cnt_e, np.nan)
            base = np.where((cnt_u - cnt_e) > 0, (sum_u - sum_e) / (cnt_u - cnt_e), np.nan)
        exc = raw - base
        m, t = nw_t(exc.copy(), lag=h)
        hm, ht = nw_t(np.where(hold, exc, np.nan).copy(), lag=h)
        # 月份集中度（事件日 exc 加權貢獻）與個股集中度（事件股次占比）
        months = pd.Series(cal).str[:7]
        exc_s = pd.Series(exc, index=months.values).dropna()
        by_month = exc_s.groupby(level=0).mean().sort_values()
        ev_per_stock = pd.Series(ev.sum(axis=0), index=stocks)
        topshare = ev_per_stock.nlargest(10).sum() / max(ev_per_stock.sum(), 1)
        out[f"N{N}_h{h}"] = {
            "n_events": int(ev.sum()),
            "n_event_days": int(np.sum(~np.isnan(exc))),
            "mean_excess_pct": round(m * 100, 4),
            "nw_t": round(t, 2),
            "holdout_mean_pct": round(hm * 100, 4),
            "holdout_t": round(ht, 2),
            "worst_month": by_month.index[0], "worst_month_exc_pct": round(by_month.iloc[0] * 100, 3),
            "best_month": by_month.index[-1], "best_month_exc_pct": round(by_month.iloc[-1] * 100, 3),
            "months_negative_frac": round(float((by_month < 0).mean()), 3),
            "top10_stock_event_share": round(float(topshare), 4),
        }
    return out


def main():
    res = {"as_spec(with 2026-07-10 fake day)": run(False),
           "dropped_2026-07-10": run(True)}
    # 污染量化：07-09 事件在 07-10 殭屍列上的進場
    px = pd.read_pickle(CACHE / "price_panel.pkl")
    zomb = px[px.trade_date == FAKE_DAY]
    res["fake_day_zombie_rows"] = int(len(zomb))
    res["fake_day_sources"] = zomb.source.value_counts().to_dict()
    print(json.dumps(res, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
