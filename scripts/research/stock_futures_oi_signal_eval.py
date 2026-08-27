#!/usr/bin/env python3
"""Item F — does per-stock futures open-interest z-score predict THAT stock's
forward return? (transplant of the champion fut_foreign_oi_z60 methodology
from index-level down to individual stock futures.)

Inputs:
  - reports/research/stock_futures_oi_signal/futures_oi_cache.json
    {stock_id: {date: [open_interest, volume, futures_close, contract_date]}}
    built by scripts/research/stock_futures_oi_fetch.py (front-month = highest
    volume position-session contract each day).
  - stock_db.stock_daily_bars (read-only, local SQLite) for the underlying
    stock's open/close.

Method (mirrors scripts/research/chip_macro/eval_signals.py):
  oi_z60[t]  = rolling z-score of open_interest over trailing 60 trading days
               (PIT: only uses oi[t-59..t], known after close of day t)
  fwd_h[t]   = stock_close[t+h] / stock_open[t+1] - 1   (enter open t+1, no look-ahead)
  IC         = Pearson corr(oi_z60, fwd_h), pooled across all stock-days, and
               per-stock average, with a chronological 70/30 IS/OOS split on
               calendar date (same convention as build_panel.py/eval_signals.py).
  Sanity L/S: position = sign(oi_z60), equal-weighted daily portfolio across
               all stocks with a live signal that day -> annualized Sharpe.

Output: reports/research/stock_futures_oi_signal/eval_results.json + prints
summary table used to write FINDINGS.md.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from stock_db import connection

ROOT = Path(__file__).resolve().parents[2]
CACHE = ROOT / "reports/research/stock_futures_oi_signal/futures_oi_cache.json"
OUT_DIR = ROOT / "reports/research/stock_futures_oi_signal"
HORIZONS = [5, 10, 20]
Z_WIN = 60
IS_FRAC = 0.70


def rolling_z(s: pd.Series, w: int) -> pd.Series:
    m = s.rolling(w, min_periods=w).mean()
    sd = s.rolling(w, min_periods=w).std()
    return (s - m) / sd


def load_oi_cache() -> dict[str, pd.DataFrame]:
    raw = json.loads(CACHE.read_text())
    out = {}
    for sid, byd in raw.items():
        if not byd:
            continue
        rows = [
            {"date": d, "oi": v[0], "fut_vol": v[1], "fut_close": v[2]}
            for d, v in byd.items()
        ]
        df = pd.DataFrame(rows).sort_values("date").reset_index(drop=True)
        out[sid] = df
    return out


def load_stock_bars(conn, stock_ids: list[str], start: str, end: str) -> dict[str, pd.DataFrame]:
    out = {}
    for sid in stock_ids:
        cur = conn.execute(
            "SELECT trade_date, open, close FROM stock_daily_bars "
            "WHERE stock_id = ? AND trade_date BETWEEN ? AND ? ORDER BY trade_date",
            (sid, start, end),
        )
        rows = cur.fetchall()
        if not rows:
            out[sid] = pd.DataFrame(columns=["date", "open", "close"])
            continue
        df = pd.DataFrame([tuple(r) for r in rows], columns=["date", "open", "close"])
        out[sid] = df
    return out


def build_stock_panel(sid: str, oi_df: pd.DataFrame, bars: pd.DataFrame) -> pd.DataFrame:
    m = pd.merge(oi_df, bars, on="date", how="inner").sort_values("date").reset_index(drop=True)
    if len(m) < Z_WIN + max(HORIZONS) + 10:
        return pd.DataFrame()
    m["oi_z60"] = rolling_z(m["oi"], Z_WIN)
    entry = m["open"].shift(-1)
    for h in HORIZONS:
        exit_ = m["close"].shift(-h)
        m[f"fwd{h}"] = exit_ / entry - 1.0
    m["stock_id"] = sid
    return m


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    oi_cache = load_oi_cache()
    stock_ids = sorted(oi_cache.keys())
    print(f"stocks with OI cache: {len(stock_ids)}")

    all_dates = sorted({d for df in oi_cache.values() for d in df["date"]})
    start, end = all_dates[0], all_dates[-1]
    conn = connection.connect_ro()
    bars = load_stock_bars(conn, stock_ids, start, end)

    panels = []
    coverage = []
    for sid in stock_ids:
        p = build_stock_panel(sid, oi_cache[sid], bars[sid])
        if p.empty:
            coverage.append((sid, 0))
            continue
        panels.append(p)
        coverage.append((sid, len(p)))

    panel = pd.concat(panels, ignore_index=True)
    panel = panel.sort_values(["date", "stock_id"]).reset_index(drop=True)
    n_stocks_used = panel["stock_id"].nunique()
    print(f"stocks with usable panel (>= {Z_WIN + max(HORIZONS) + 10} rows): {n_stocks_used}")
    print(f"total stock-days in pooled panel (pre-NaN-drop): {len(panel)}")

    dates_sorted = sorted(panel["date"].unique())
    n_dates = len(dates_sorted)
    is_cut_date = dates_sorted[int(n_dates * IS_FRAC)]
    print(f"chronological IS/OOS cut date: {is_cut_date}  ({n_dates} unique dates)")
    is_mask = panel["date"] < is_cut_date

    results = {"coverage": coverage, "is_cut_date": is_cut_date, "n_dates": n_dates, "horizons": {}}

    for h in HORIZONS:
        col = f"fwd{h}"
        d = panel[["stock_id", "date", "oi_z60", col]].dropna()
        ism = is_mask.reindex(d.index)
        is_d, oos_d = d[ism], d[~ism]

        def stats(sub: pd.DataFrame) -> dict:
            if len(sub) < 30:
                return {"n": len(sub), "pearson_ic": None, "spearman_ic": None}
            pear = sub["oi_z60"].corr(sub[col])
            spear = sub["oi_z60"].corr(sub[col], method="spearman")
            return {"n": len(sub), "pearson_ic": round(float(pear), 4), "spearman_ic": round(float(spear), 4)}

        # per-stock IC then average (robust to a few names dominating pooled IC)
        per_stock_is, per_stock_oos = [], []
        for sid, g in is_d.groupby("stock_id"):
            if len(g) >= 40:
                per_stock_is.append(g["oi_z60"].corr(g[col]))
        for sid, g in oos_d.groupby("stock_id"):
            if len(g) >= 30:
                per_stock_oos.append(g["oi_z60"].corr(g[col]))

        results["horizons"][h] = {
            "pooled_all": stats(d),
            "pooled_IS": stats(is_d),
            "pooled_OOS": stats(oos_d),
            "per_stock_mean_IC_IS": round(float(np.nanmean(per_stock_is)), 4) if per_stock_is else None,
            "per_stock_mean_IC_OOS": round(float(np.nanmean(per_stock_oos)), 4) if per_stock_oos else None,
            "n_stocks_IS": len(per_stock_is),
            "n_stocks_OOS": len(per_stock_oos),
        }
        print(f"\n=== h={h} ===")
        print(json.dumps(results["horizons"][h], indent=2))

    # sign-based L/S sanity, h=5 (shortest, least overlap-heavy for a quick daily-rebalance proxy)
    h_sanity = 5
    d = panel[["stock_id", "date", "oi_z60", f"fwd{h_sanity}"]].dropna().copy()
    d["pos"] = np.sign(d["oi_z60"])
    d["pnl"] = d["pos"] * d[f"fwd{h_sanity}"] / h_sanity  # rough daily-equivalent, non-overlapping ignored
    daily = d.groupby("date")["pnl"].mean()
    ism2 = pd.Series(daily.index < is_cut_date, index=daily.index)

    def sharpe(x: pd.Series) -> float | None:
        if len(x) < 30 or x.std() == 0:
            return None
        return round(float(x.mean() / x.std() * np.sqrt(252)), 3)

    ls_sanity = {
        "IS_sharpe": sharpe(daily[ism2]),
        "OOS_sharpe": sharpe(daily[~ism2]),
        "IS_mean_daily_pct": round(float(daily[ism2].mean() * 100), 5) if ism2.any() else None,
        "OOS_mean_daily_pct": round(float(daily[~ism2].mean() * 100), 5) if (~ism2).any() else None,
        "n_days_IS": int(ism2.sum()),
        "n_days_OOS": int((~ism2).sum()),
    }
    results["ls_sanity_h5_sign"] = ls_sanity
    print("\n=== sign(oi_z60) L/S sanity, h=5, equal-weight daily ===")
    print(json.dumps(ls_sanity, indent=2))

    (OUT_DIR / "eval_results.json").write_text(json.dumps(results, indent=2, ensure_ascii=False))
    panel.to_parquet(OUT_DIR / "panel.parquet")
    print(f"\nwrote {OUT_DIR / 'eval_results.json'} and panel.parquet")


if __name__ == "__main__":
    main()
