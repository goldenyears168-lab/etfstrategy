"""Overnight gap · seg_last level（日 K + 13:00 盤中）· 條件分布校準。

預測目標：G = (O_{T+1} − C_T) / C_T × 100（%）。
特徵（PIT @ T 13:00）：
  · S_d — 日 K RRG seg_last，as-of T−1 收盤
  · S_i — 盤中 RRG seg_last @13:00（scale 或 full_rrg）

不含 Top3 選股；僅分布估計與假設檢定。
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np
import pandas as pd
from scipy import stats

from market_benchmark import load_benchmark_close
from research.backtest.finpilot_local_backtest import load_price_panels
from research.backtest.rrg_mono_backtest import build_fresh_mono_calendar
from research.backtest.rrg_mono_intraday_ab import _kbar_px, intraday_price_scale
from rrg_mono_daily_brief import LENGTH, LOOKBACK, _feat, _fresh_mono, _mono_tier2
from rrg_rotation import compute_rrg_panel

UniverseMode = Literal["all_kbar", "fresh_mono"]
IntradaySegMode = Literal["scale", "full_rrg"]
SNAPSHOT_MINUTE = "13:00:00"

HYPOTHESIS_IDS = ("H1_sd_quintile", "H2_si_quintile", "H3_kruskal_sd", "H4_partial_si")


@dataclass(frozen=True)
class GapBucketStats:
    n: int
    mean_gap_pct: float
    std_gap_pct: float
    median_gap_pct: float
    p_gap_up: float
    p_gap_ge_1: float
    p_gap_le_neg_1: float
    q10: float
    q25: float
    q50: float
    q75: float
    q90: float

    def to_dict(self) -> dict[str, float | int]:
        return {
            "n": self.n,
            "mean_gap_pct": self.mean_gap_pct,
            "std_gap_pct": self.std_gap_pct,
            "median_gap_pct": self.median_gap_pct,
            "P_gap_up": self.p_gap_up,
            "P_gap_ge_1pct": self.p_gap_ge_1,
            "P_gap_le_neg1pct": self.p_gap_le_neg_1,
            "q10": self.q10,
            "q25": self.q25,
            "q50": self.q50,
            "q75": self.q75,
            "q90": self.q90,
        }


def _bucket_stats(gaps: list[float] | np.ndarray) -> GapBucketStats | None:
    arr = np.asarray(gaps, dtype=float)
    arr = arr[np.isfinite(arr)]
    if len(arr) == 0:
        return None
    n = len(arr)
    qs = np.quantile(arr, [0.1, 0.25, 0.5, 0.75, 0.9])
    return GapBucketStats(
        n=n,
        mean_gap_pct=round(float(arr.mean()), 4),
        std_gap_pct=round(float(arr.std(ddof=1)) if n > 1 else 0.0, 4),
        median_gap_pct=round(float(np.median(arr)), 4),
        p_gap_up=round(float((arr > 0).mean()) * 100, 2),
        p_gap_ge_1=round(float((arr >= 1.0).mean()) * 100, 2),
        p_gap_le_neg_1=round(float((arr <= -1.0).mean()) * 100, 2),
        q10=round(float(qs[0]), 4),
        q25=round(float(qs[1]), 4),
        q50=round(float(qs[2]), 4),
        q75=round(float(qs[3]), 4),
        q90=round(float(qs[4]), 4),
    )


def _assign_quintile(values: pd.Series, *, n_bins: int = 5) -> pd.Series:
    ranked = values.rank(method="first")
    return pd.qcut(ranked, n_bins, labels=False, duplicates="drop")


def _overnight_gap_pct(
    open_df: pd.DataFrame,
    close_df: pd.DataFrame,
    stock_id: str,
    trade_date: str,
    full_dates: list[str],
) -> float | None:
    if trade_date not in full_dates:
        return None
    si = full_dates.index(trade_date)
    if si + 1 >= len(full_dates):
        return None
    nxt = full_dates[si + 1]
    if nxt not in open_df.index or trade_date not in close_df.index:
        return None
    if stock_id not in open_df.columns or stock_id not in close_df.columns:
        return None
    o = float(open_df.at[nxt, stock_id])
    c = float(close_df.at[trade_date, stock_id])
    if not (np.isfinite(o) and np.isfinite(c) and c > 0):
        return None
    return (o - c) / c * 100.0


def _intraday_seg_scale(
    conn: sqlite3.Connection,
    close: pd.DataFrame,
    *,
    trade_date: str,
    stock_id: str,
    seg_daily: float,
    kbar_cache: dict,
) -> float | None:
    if trade_date not in close.index or stock_id not in close.columns:
        return None
    c = float(close.at[trade_date, stock_id])
    if c <= 0:
        return None
    px = _kbar_px(conn, stock_id, trade_date, SNAPSHOT_MINUTE, kbar_cache, close)
    return float(seg_daily) * intraday_price_scale(c, px)


def _intraday_seg_full_rrg(
    conn: sqlite3.Connection,
    close: pd.DataFrame,
    bench: pd.Series,
    full_dates: list[str],
    *,
    trade_date: str,
    stock_id: str,
    kbar_cache: dict,
) -> float | None:
    """13:00 價格代入當日 close · 重算 RRG · 取 seg_last（不過 fresh 濾網）。"""
    if trade_date not in full_dates:
        return None
    si = full_dates.index(trade_date)
    if si < LOOKBACK:
        return None
    px = _kbar_px(conn, stock_id, trade_date, SNAPSHOT_MINUTE, kbar_cache, close)
    if px is None or px <= 0:
        return None
    prov = close.copy()
    bench_p = bench.reindex(prov.index).astype(float)
    prov.at[trade_date, stock_id] = float(px)
    rs_r2, rs_m2, _ = compute_rrg_panel(prov, bench_p, length=LENGTH)
    f = _feat(rs_r2, rs_m2, full_dates, si, stock_id)
    return float(f["seg_last"]) if f else None


def _batch_intraday_seg_full_rrg(
    conn: sqlite3.Connection,
    close: pd.DataFrame,
    bench: pd.Series,
    full_dates: list[str],
    trade_date: str,
    stock_ids: list[str],
    kbar_cache: dict,
) -> dict[str, float]:
    """單交易日 · 批次代入 13:00 價 · 一次 compute_rrg_panel。"""
    if trade_date not in full_dates:
        return {}
    si = full_dates.index(trade_date)
    if si < LOOKBACK:
        return {}
    prov = close[[s for s in stock_ids if s in close.columns]].copy()
    if prov.empty:
        return {}
    bench_p = bench.reindex(prov.index).astype(float)
    active = list(prov.columns)
    for sid in active:
        px = _kbar_px(conn, sid, trade_date, SNAPSHOT_MINUTE, kbar_cache, close)
        if px is not None and px > 0:
            prov.at[trade_date, sid] = float(px)
    rs_r2, rs_m2, _ = compute_rrg_panel(prov, bench_p, length=LENGTH)
    out: dict[str, float] = {}
    for sid in active:
        f = _feat(rs_r2, rs_m2, full_dates, si, sid)
        if f is not None:
            out[sid] = float(f["seg_last"])
    return out


def collect_overnight_gap_observations(
    conn: sqlite3.Connection,
    *,
    date_start: str,
    date_end: str,
    universe: UniverseMode = "all_kbar",
    intraday_seg_mode: IntradaySegMode = "scale",
) -> pd.DataFrame:
    close, opn, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index)
    full_dates = close.index.astype(str).tolist()
    rs_r, rs_m, _ = compute_rrg_panel(close, bench, length=LENGTH)

    if universe == "fresh_mono":
        sessions = [
            str(r[0])
            for r in conn.execute(
                """
                SELECT DISTINCT trade_date FROM stock_kbar_1m
                WHERE trade_date >= ? AND trade_date <= ?
                ORDER BY trade_date
                """,
                (date_start, date_end),
            ).fetchall()
        ]
        signal_dates = sorted(
            {
                full_dates[full_dates.index(td) - 1]
                for td in sessions
                if td in full_dates and full_dates.index(td) >= LOOKBACK + 1
            }
        )
        mono_cal = build_fresh_mono_calendar(conn, signal_dates)
        pairs: list[tuple[str, str]] = []
        for td in sessions:
            if td not in full_dates:
                continue
            si = full_dates.index(td)
            if si < LOOKBACK + 1:
                continue
            sig = full_dates[si - 1]
            for row in mono_cal.get(sig, []):
                pairs.append((row.stock_id, td))
    else:
        pairs = [
            (str(r[0]), str(r[1]))
            for r in conn.execute(
                """
                SELECT DISTINCT stock_id, trade_date FROM stock_kbar_1m
                WHERE trade_date >= ? AND trade_date <= ?
                """,
                (date_start, date_end),
            ).fetchall()
        ]

    kbar_cache: dict = {}
    rows: list[dict[str, Any]] = []

    by_day: dict[str, list[str]] = defaultdict(list)
    for stock_id, trade_date in pairs:
        if trade_date not in full_dates:
            continue
        si = full_dates.index(trade_date)
        if si < LOOKBACK + 1 or si + 1 >= len(full_dates):
            continue
        by_day[trade_date].append(stock_id)

    for trade_date in sorted(by_day.keys()):
        si = full_dates.index(trade_date)
        signal_date = full_dates[si - 1]
        sids = by_day[trade_date]
        seg_i_map: dict[str, float] = {}
        if intraday_seg_mode == "full_rrg":
            seg_i_map = _batch_intraday_seg_full_rrg(
                conn, close, bench, full_dates, trade_date, sids, kbar_cache
            )

        for stock_id in sids:
            gap = _overnight_gap_pct(opn, close, stock_id, trade_date, full_dates)
            if gap is None:
                continue
            feat = _feat(rs_r, rs_m, full_dates, si - 1, stock_id)
            if feat is None:
                continue
            seg_d = float(feat["seg_last"])
            if intraday_seg_mode == "full_rrg":
                seg_i = seg_i_map.get(stock_id)
            else:
                seg_i = _intraday_seg_scale(
                    conn,
                    close,
                    trade_date=trade_date,
                    stock_id=stock_id,
                    seg_daily=seg_d,
                    kbar_cache=kbar_cache,
                )
            if seg_i is None:
                continue
            rows.append(
                {
                    "trade_date": trade_date,
                    "signal_date": signal_date,
                    "stock_id": stock_id,
                    "overnight_gap_pct": gap,
                    "seg_last_daily": seg_d,
                    "seg_last_intraday_1300": seg_i,
                    "fresh_mono": bool(
                        _fresh_mono(rs_r, rs_m, full_dates, si - 1, stock_id)
                        and _mono_tier2(feat)
                    ),
                }
            )

    return pd.DataFrame(rows)


def _quintile_table(df: pd.DataFrame, col: str) -> dict[str, Any]:
    sub = df[[col, "overnight_gap_pct"]].dropna().copy()
    if len(sub) < 50:
        return {"n": len(sub), "buckets": {}, "insufficient": True}
    sub["q"] = _assign_quintile(sub[col])
    buckets: dict[str, Any] = {}
    for q in sorted(sub["q"].unique()):
        gaps = sub.loc[sub["q"] == q, "overnight_gap_pct"].tolist()
        st = _bucket_stats(gaps)
        if st is None:
            continue
        lo = float(sub.loc[sub["q"] == q, col].min())
        hi = float(sub.loc[sub["q"] == q, col].max())
        buckets[str(int(q))] = {
            "seg_range": [round(lo, 4), round(hi, 4)],
            **st.to_dict(),
        }
    top = sub[sub["q"] == sub["q"].max()]["overnight_gap_pct"]
    bot = sub[sub["q"] == sub["q"].min()]["overnight_gap_pct"]
    _, mw_p = stats.mannwhitneyu(top, bot, alternative="two-sided")
    rho, sp_p = stats.spearmanr(sub[col], sub["overnight_gap_pct"])
    return {
        "n": len(sub),
        "spread_top_minus_bottom_pp": round(float(top.mean() - bot.mean()), 4),
        "mannwhitney_p": float(mw_p),
        "spearman_rho": round(float(rho), 4),
        "spearman_p": float(sp_p),
        "buckets": buckets,
    }


def _joint_grid(df: pd.DataFrame) -> dict[str, Any]:
    sub = df.dropna(subset=["seg_last_daily", "seg_last_intraday_1300", "overnight_gap_pct"]).copy()
    if len(sub) < 100:
        return {"n": len(sub), "cells": {}, "insufficient": True}
    sub["q_d"] = _assign_quintile(sub["seg_last_daily"])
    sub["q_i"] = _assign_quintile(sub["seg_last_intraday_1300"])
    cells: dict[str, Any] = {}
    for (qd, qi), grp in sub.groupby(["q_d", "q_i"], observed=True):
        st = _bucket_stats(grp["overnight_gap_pct"].tolist())
        if st is None or st.n < 5:
            continue
        cells[f"{int(qd)}_{int(qi)}"] = st.to_dict()
    return {"n": len(sub), "cells": cells}


def _run_hypothesis_tests(df: pd.DataFrame) -> dict[str, Any]:
    sub = df.dropna(subset=["seg_last_daily", "seg_last_intraday_1300", "overnight_gap_pct"]).copy()
    out: dict[str, Any] = {}
    if len(sub) < 50:
        return {"insufficient": True, "n": len(sub)}

    sd = _quintile_table(sub, "seg_last_daily")
    si = _quintile_table(sub, "seg_last_intraday_1300")
    out["H1_sd_quintile"] = {
        "statement": "S_d 五分位 · 隔日 gap 分布不同",
        "spread_pp": sd.get("spread_top_minus_bottom_pp"),
        "mannwhitney_p": sd.get("mannwhitney_p"),
        "spearman_rho": sd.get("spearman_rho"),
        "spearman_p": sd.get("spearman_p"),
        "n": sd.get("n"),
    }
    out["H2_si_quintile"] = {
        "statement": "S_i@13:00 五分位 · 隔日 gap 分布不同",
        "spread_pp": si.get("spread_top_minus_bottom_pp"),
        "mannwhitney_p": si.get("mannwhitney_p"),
        "spearman_rho": si.get("spearman_rho"),
        "spearman_p": si.get("spearman_p"),
        "n": si.get("n"),
    }

    groups = [g["overnight_gap_pct"].values for _, g in sub.assign(q=_assign_quintile(sub["seg_last_daily"])).groupby("q")]
    if len(groups) >= 2:
        h_stat, kw_p = stats.kruskal(*groups)
        out["H3_kruskal_sd"] = {
            "statement": "S_d 五分位 · Kruskal-Wallis",
            "H": round(float(h_stat), 4),
            "p": float(kw_p),
        }

    from sklearn.linear_model import LinearRegression

    x_d = sub[["seg_last_daily"]].values
    resid_i = sub["seg_last_intraday_1300"].values - LinearRegression().fit(x_d, sub["seg_last_intraday_1300"]).predict(x_d)
    resid_g = sub["overnight_gap_pct"].values - LinearRegression().fit(x_d, sub["overnight_gap_pct"]).predict(x_d)
    rho, p = stats.spearmanr(resid_i, resid_g)
    out["H4_partial_si"] = {
        "statement": "控制 S_d 後 · S_i 殘差 vs gap 殘差",
        "partial_spearman_rho": round(float(rho), 4),
        "p": float(p),
    }

    mw_ps = [sd.get("mannwhitney_p"), si.get("mannwhitney_p")]
    mw_ps = [p for p in mw_ps if p is not None and np.isfinite(p)]
    if mw_ps:
        out["bh_fdr_mannwhitney"] = {
            k: float(v)
            for k, v in zip(
                ("H1_sd_quintile", "H2_si_quintile"),
                stats.false_discovery_control(mw_ps),
            )
        }

    alpha = 0.05
    out["significant_at_005"] = {
        hid: bool(out[hid].get("mannwhitney_p", out[hid].get("p", 1.0)) < alpha)
        for hid in ("H1_sd_quintile", "H2_si_quintile", "H3_kruskal_sd", "H4_partial_si")
        if hid in out
    }
    return out


def lookup_gap_distribution(
    payload: dict[str, Any],
    *,
    seg_daily: float,
    seg_intraday: float,
) -> dict[str, Any] | None:
    """依 S_d / S_i 查 joint grid（优先 OOS train 界）。"""
    prod = payload.get("production_lookup") or {}
    joint = prod.get("joint_grid") or payload.get("joint_grid") or {}
    cells = joint.get("cells") or {}
    if not cells:
        return None
    bounds_d = (prod.get("quintile_bounds") or payload.get("quintile_bounds") or {}).get(
        "seg_last_daily"
    ) or []
    bounds_i = (prod.get("quintile_bounds") or payload.get("quintile_bounds") or {}).get(
        "seg_last_intraday_1300"
    ) or []
    if len(bounds_d) < 2 or len(bounds_i) < 2:
        return None

    def _q(val: float, bounds: list[float]) -> int:
        for i in range(len(bounds) - 1):
            if bounds[i] <= val <= bounds[i + 1]:
                return i
        return len(bounds) - 2

    qd = _q(seg_daily, bounds_d)
    qi = _q(seg_intraday, bounds_i)
    cell = cells.get(f"{qd}_{qi}")
    if cell is None:
        return None
    return {"q_daily": qd, "q_intraday": qi, **cell}


def _quintile_bounds(df: pd.DataFrame, col: str) -> list[float]:
    sub = df[col].dropna()
    if sub.empty:
        return []
    qs = [float(sub.quantile(q)) for q in np.linspace(0, 1, 6)]
    return qs


def _split_train_test(
    df: pd.DataFrame,
    train_ratio: float,
) -> tuple[str, str, pd.DataFrame, pd.DataFrame]:
    dates = sorted(df["trade_date"].unique())
    if len(dates) < 10:
        return dates[0], dates[-1], df, df.iloc[0:0]
    cut_i = max(1, min(len(dates) - 2, int(len(dates) * train_ratio)))
    cut_date = dates[cut_i - 1]
    train = df[df["trade_date"] <= cut_date].copy()
    test = df[df["trade_date"] > cut_date].copy()
    test_start = str(test["trade_date"].min()) if not test.empty else cut_date
    return cut_date, test_start, train, test


def _assign_quintile_from_bounds(values: pd.Series, bounds: list[float]) -> pd.Series:
    if len(bounds) < 2:
        return pd.Series(np.nan, index=values.index)
    return pd.cut(
        values,
        bins=bounds,
        labels=False,
        include_lowest=True,
        duplicates="drop",
    ).astype("float")


def _quintile_table_fixed_bounds(
    df: pd.DataFrame,
    col: str,
    bounds: list[float],
) -> dict[str, Any]:
    sub = df[[col, "overnight_gap_pct"]].dropna().copy()
    if len(sub) < 20 or len(bounds) < 2:
        return {"n": len(sub), "buckets": {}, "insufficient": True}
    sub["q"] = _assign_quintile_from_bounds(sub[col], bounds)
    sub = sub.dropna(subset=["q"])
    if len(sub) < 20:
        return {"n": len(sub), "buckets": {}, "insufficient": True}
    sub["q"] = sub["q"].astype(int)
    buckets: dict[str, Any] = {}
    for q in sorted(sub["q"].unique()):
        gaps = sub.loc[sub["q"] == q, "overnight_gap_pct"].tolist()
        st = _bucket_stats(gaps)
        if st is None:
            continue
        buckets[str(int(q))] = {
            "seg_bounds": [round(bounds[int(q)], 4), round(bounds[int(q) + 1], 4)],
            **st.to_dict(),
        }
    top = sub[sub["q"] == sub["q"].max()]["overnight_gap_pct"]
    bot = sub[sub["q"] == sub["q"].min()]["overnight_gap_pct"]
    _, mw_p = stats.mannwhitneyu(top, bot, alternative="two-sided") if len(top) >= 5 and len(bot) >= 5 else (0, 1.0)
    rho, sp_p = stats.spearmanr(sub[col], sub["overnight_gap_pct"]) if len(sub) >= 10 else (0.0, 1.0)
    return {
        "n": len(sub),
        "spread_top_minus_bottom_pp": round(float(top.mean() - bot.mean()), 4) if len(top) and len(bot) else None,
        "mannwhitney_p": float(mw_p),
        "spearman_rho": round(float(rho), 4) if np.isfinite(rho) else None,
        "spearman_p": float(sp_p) if np.isfinite(sp_p) else None,
        "buckets": buckets,
        "bounds_source": "train",
    }


def _joint_grid_fixed_bounds(
    df: pd.DataFrame,
    bounds_d: list[float],
    bounds_i: list[float],
) -> dict[str, Any]:
    sub = df.dropna(subset=["seg_last_daily", "seg_last_intraday_1300", "overnight_gap_pct"]).copy()
    if len(sub) < 20:
        return {"n": len(sub), "cells": {}, "insufficient": True}
    sub["q_d"] = _assign_quintile_from_bounds(sub["seg_last_daily"], bounds_d)
    sub["q_i"] = _assign_quintile_from_bounds(sub["seg_last_intraday_1300"], bounds_i)
    sub = sub.dropna(subset=["q_d", "q_i"])
    cells: dict[str, Any] = {}
    for (qd, qi), grp in sub.groupby(["q_d", "q_i"], observed=True):
        st = _bucket_stats(grp["overnight_gap_pct"].tolist())
        if st is None or st.n < 3:
            continue
        cells[f"{int(qd)}_{int(qi)}"] = st.to_dict()
    return {"n": len(sub), "cells": cells, "bounds_source": "train"}


def _brier_score(y_true: np.ndarray, y_prob: np.ndarray) -> float:
    y_true = np.asarray(y_true, dtype=float)
    y_prob = np.clip(np.asarray(y_prob, dtype=float), 1e-6, 1 - 1e-6)
    return float(np.mean((y_prob - y_true) ** 2))


def _oos_prob_calibration(
    train_df: pd.DataFrame,
    test_df: pd.DataFrame,
    bounds_d: list[float],
) -> dict[str, Any]:
    """Test：以 train 五分位 P(gap>0) 预测 test 事件。"""
    train_tbl = _quintile_table_fixed_bounds(train_df, "seg_last_daily", bounds_d)
    train_buckets = train_tbl.get("buckets") or {}
    if not train_buckets or test_df.empty:
        return {"insufficient": True}

    base_p = float((train_df["overnight_gap_pct"] > 0).mean())
    probs: list[float] = []
    actuals: list[float] = []
    base_probs: list[float] = []

    sub = test_df.dropna(subset=["seg_last_daily", "overnight_gap_pct"]).copy()
    sub["q"] = _assign_quintile_from_bounds(sub["seg_last_daily"], bounds_d)
    sub = sub.dropna(subset=["q"])
    for _, row in sub.iterrows():
        q = str(int(row["q"]))
        bucket = train_buckets.get(q)
        if not bucket:
            continue
        p = float(bucket.get("P_gap_up", 50.0)) / 100.0
        probs.append(p)
        base_probs.append(base_p)
        actuals.append(1.0 if row["overnight_gap_pct"] > 0 else 0.0)

    if len(actuals) < 20:
        return {"insufficient": True, "n": len(actuals)}

    y = np.array(actuals)
    b_model = _brier_score(y, np.array(probs))
    b_base = _brier_score(y, np.array(base_probs))
    return {
        "n": len(actuals),
        "brier_model": round(b_model, 6),
        "brier_baseline": round(b_base, 6),
        "brier_improvement": round(b_base - b_model, 6),
        "model_better": b_model < b_base,
    }


def _oos_quintile_stability(
    train_df: pd.DataFrame,
    test_df: pd.DataFrame,
    bounds_d: list[float],
) -> dict[str, Any]:
    train_tbl = _quintile_table_fixed_bounds(train_df, "seg_last_daily", bounds_d)
    test_tbl = _quintile_table_fixed_bounds(test_df, "seg_last_daily", bounds_d)
    rows: list[dict[str, Any]] = []
    for q in sorted(set((train_tbl.get("buckets") or {}).keys()) | set((test_tbl.get("buckets") or {}).keys())):
        tr = (train_tbl.get("buckets") or {}).get(q, {})
        te = (test_tbl.get("buckets") or {}).get(q, {})
        if not tr or not te:
            continue
        tr_m = tr.get("mean_gap_pct")
        te_m = te.get("mean_gap_pct")
        if tr_m is None or te_m is None:
            continue
        rows.append(
            {
                "q": int(q),
                "train_mean_gap": tr_m,
                "test_mean_gap": te_m,
                "delta_pp": round(float(te_m) - float(tr_m), 4),
                "train_n": tr.get("n"),
                "test_n": te.get("n"),
            }
        )
    if not rows:
        return {"insufficient": True}
    deltas = [abs(r["delta_pp"]) for r in rows]
    return {
        "by_quintile": rows,
        "mean_abs_delta_pp": round(float(np.mean(deltas)), 4),
        "max_abs_delta_pp": round(float(np.max(deltas)), 4),
    }


def _build_oos_split(df: pd.DataFrame, train_ratio: float) -> dict[str, Any]:
    cut_date, test_start, train_df, test_df = _split_train_test(df, train_ratio)
    bounds_d = _quintile_bounds(train_df, "seg_last_daily")
    bounds_i = _quintile_bounds(train_df, "seg_last_intraday_1300")

    train_block: dict[str, Any] = {
        "n": len(train_df),
        "date_start": str(train_df["trade_date"].min()) if not train_df.empty else None,
        "date_end": cut_date,
        "seg_last_daily": _quintile_table(train_df, "seg_last_daily"),
        "seg_last_intraday_1300": _quintile_table(train_df, "seg_last_intraday_1300"),
        "joint_grid": _joint_grid(train_df),
        "hypothesis_tests": _run_hypothesis_tests(train_df),
        "quintile_bounds": {
            "seg_last_daily": bounds_d,
            "seg_last_intraday_1300": bounds_i,
        },
    }

    test_block: dict[str, Any] = {
        "n": len(test_df),
        "date_start": test_start,
        "date_end": str(test_df["trade_date"].max()) if not test_df.empty else None,
        "seg_last_daily": _quintile_table_fixed_bounds(test_df, "seg_last_daily", bounds_d),
        "seg_last_intraday_1300": _quintile_table_fixed_bounds(
            test_df, "seg_last_intraday_1300", bounds_i
        ),
        "joint_grid": _joint_grid_fixed_bounds(test_df, bounds_d, bounds_i),
        "hypothesis_tests": _run_hypothesis_tests(test_df),
    }

    return {
        "train_ratio": train_ratio,
        "cut_date": cut_date,
        "train": train_block,
        "test": test_block,
        "prob_calibration": _oos_prob_calibration(train_df, test_df, bounds_d),
        "quintile_stability": _oos_quintile_stability(train_df, test_df, bounds_d),
        "lookup_source": "train",
    }


def calibrate_overnight_gap_seg(
    conn: sqlite3.Connection,
    *,
    date_start: str,
    date_end: str,
    universe: UniverseMode = "all_kbar",
    intraday_seg_mode: IntradaySegMode = "scale",
    oos_train_ratio: float | None = 0.7,
) -> dict[str, Any]:
    df = collect_overnight_gap_observations(
        conn,
        date_start=date_start,
        date_end=date_end,
        universe=universe,
        intraday_seg_mode=intraday_seg_mode,
    )
    if df.empty:
        return {
            "date_start": date_start,
            "date_end": date_end,
            "universe": universe,
            "intraday_seg_mode": intraday_seg_mode,
            "n_observations": 0,
            "error": "no observations",
        }

    overall = _bucket_stats(df["overnight_gap_pct"].tolist())
    fresh_n = int(df["fresh_mono"].sum()) if "fresh_mono" in df.columns else 0

    payload: dict[str, Any] = {
        "date_start": date_start,
        "date_end": date_end,
        "snapshot_minute": SNAPSHOT_MINUTE,
        "universe": universe,
        "intraday_seg_mode": intraday_seg_mode,
        "n_observations": len(df),
        "n_fresh_mono": fresh_n,
        "n_stocks": int(df["stock_id"].nunique()),
        "n_trade_dates": int(df["trade_date"].nunique()),
        "overall": overall.to_dict() if overall else {},
        "seg_last_daily": _quintile_table(df, "seg_last_daily"),
        "seg_last_intraday_1300": _quintile_table(df, "seg_last_intraday_1300"),
        "joint_grid": _joint_grid(df),
        "hypothesis_tests": _run_hypothesis_tests(df),
        "quintile_bounds": {
            "seg_last_daily": _quintile_bounds(df, "seg_last_daily"),
            "seg_last_intraday_1300": _quintile_bounds(df, "seg_last_intraday_1300"),
        },
        "method_note": (
            "S_d = 日 K RRG seg_last（T−1 PIT）；"
            f"S_i = 13:00 盤中 seg_last（{intraday_seg_mode}）；"
            "G = (T+1 open − T close) / T close × 100。"
        ),
    }

    if fresh_n >= 50:
        fm = df[df["fresh_mono"]]
        payload["fresh_mono_subset"] = {
            "n": len(fm),
            "seg_last_daily": _quintile_table(fm, "seg_last_daily"),
            "hypothesis_tests": _run_hypothesis_tests(fm),
        }

    if oos_train_ratio is not None and 0.0 < oos_train_ratio < 1.0 and len(df) >= 80:
        oos = _build_oos_split(df, oos_train_ratio)
        payload["oos"] = oos
        payload["production_lookup"] = {
            "source": "oos_train",
            "quintile_bounds": oos["train"]["quintile_bounds"],
            "joint_grid": oos["train"]["joint_grid"],
            "cut_date": oos["cut_date"],
        }

    return payload


def render_calibration_md(payload: dict[str, Any]) -> str:
    ds, de = payload["date_start"], payload["date_end"]
    lines = [
        "# Overnight gap · seg_last level 分布校準",
        "",
        f"區間：{ds} .. {de} · 快照：**{payload.get('snapshot_minute', SNAPSHOT_MINUTE)}**",
        f"Universe：**{payload.get('universe')}** · 盤中 S_i 模式：**{payload.get('intraday_seg_mode')}**",
        f"觀測：**{payload.get('n_observations')}** · 標的 **{payload.get('n_stocks')}** · "
        f"交易日 **{payload.get('n_trade_dates')}** · fresh mono **{payload.get('n_fresh_mono')}**",
        "",
        payload.get("method_note", ""),
        "",
    ]

    ov = payload.get("overall") or {}
    if ov:
        lines += [
            "## 全樣本 overnight gap",
            "",
            f"- mean **{ov.get('mean_gap_pct')}%** · std **{ov.get('std_gap_pct')}%** · "
            f"P(gap>0) **{ov.get('P_gap_up')}%**",
            "",
        ]

    ht = payload.get("hypothesis_tests") or {}
    if ht and not ht.get("insufficient"):
        lines += ["## 假設檢定（α=0.05）", ""]
        sig = ht.get("significant_at_005") or {}
        for hid in HYPOTHESIS_IDS:
            block = ht.get(hid)
            if not block:
                continue
            mark = "✓" if sig.get(hid) else "✗"
            lines.append(f"- **{hid}** {mark} · {block.get('statement', '')}")
            if "spread_pp" in block:
                lines.append(
                    f"  spread Q5−Q1 **{block['spread_pp']} pp** · "
                    f"MW p={block.get('mannwhitney_p', '—'):.4g} · "
                    f"ρ={block.get('spearman_rho', '—')}"
                )
            elif "H" in block:
                lines.append(f"  H={block.get('H')} · p={block.get('p', '—'):.4g}")
            elif "partial_spearman_rho" in block:
                lines.append(f"  partial ρ={block.get('partial_spearman_rho')} · p={block.get('p', '—'):.4g}")
        fdr = ht.get("bh_fdr_mannwhitney")
        if fdr:
            lines.append(f"- BH-FDR（MW）：{fdr}")
        lines.append("")

    oos = payload.get("oos") or {}
    if oos:
        lines += [
            "## OOS 时间切分（train 校准 → test 验证）",
            "",
            f"- train ratio **{oos.get('train_ratio')}** · cut **{oos.get('cut_date')}** · "
            f"lookup 来源：**{oos.get('lookup_source')}**",
            "",
        ]
        tr = oos.get("train") or {}
        te = oos.get("test") or {}
        lines.append(
            f"- train **{tr.get('n')}** obs ({tr.get('date_start')} .. {tr.get('date_end')}) · "
            f"test **{te.get('n')}** obs ({te.get('date_start')} .. {te.get('date_end')})"
        )
        pc = oos.get("prob_calibration") or {}
        if not pc.get("insufficient"):
            lines.append(
                f"- Brier：model **{pc.get('brier_model')}** · baseline **{pc.get('brier_baseline')}** · "
                f"Δ **{pc.get('brier_improvement')}** · "
                f"{'model 较优' if pc.get('model_better') else '未优于 baseline'}"
            )
        stab = oos.get("quintile_stability") or {}
        if not stab.get("insufficient"):
            lines.append(
                f"- 五分位 mean gap 稳定性：mean |Δ| **{stab.get('mean_abs_delta_pp')} pp** · "
                f"max |Δ| **{stab.get('max_abs_delta_pp')} pp**"
            )
        te_ht = (te.get("hypothesis_tests") or {})
        te_sig = te_ht.get("significant_at_005") or {}
        if te_ht and not te_ht.get("insufficient"):
            lines.append("- test 假说：")
            for hid in HYPOTHESIS_IDS:
                ok = "✓" if te_sig.get(hid) else "✗"
                block = te_ht.get(hid) or {}
                if "spread_pp" in block:
                    lines.append(
                        f"  - {hid} {ok} spread **{block.get('spread_pp')} pp** · "
                        f"p={block.get('mannwhitney_p', block.get('p', 1)):.4g}"
                    )
        lines.append("")

    for key, title in (
        ("seg_last_daily", "S_d · 日 K seg_last（T−1）"),
        ("seg_last_intraday_1300", "S_i · 盤中 seg_last @13:00"),
    ):
        block = payload.get(key) or {}
        lines += [f"## {title} · 五分位 gap 分布", ""]
        if block.get("insufficient"):
            lines.append("_樣本不足_")
            lines.append("")
            continue
        lines.append(
            f"spread Q5−Q1 **{block.get('spread_top_minus_bottom_pp')} pp** · "
            f"MW p={block.get('mannwhitney_p', 0):.4g}"
        )
        lines.append("")
        lines.append("| Q | seg 区间 | n | mean gap | P(gap>0) | q10 | q50 | q90 |")
        lines.append("|---|---------|---|----------|----------|-----|-----|-----|")
        for q, b in sorted((block.get("buckets") or {}).items(), key=lambda x: int(x[0])):
            rng = b.get("seg_range") or ["?", "?"]
            lines.append(
                f"| {q} | {rng[0]}–{rng[1]} | {b.get('n')} | {b.get('mean_gap_pct')}% "
                f"| {b.get('P_gap_up')}% | {b.get('q10')}% | {b.get('q50')}% | {b.get('q90')}% |"
            )
        lines.append("")

    joint = payload.get("joint_grid") or {}
    n_cells = len(joint.get("cells") or {})
    lines += [
        f"## Joint grid（S_d × S_i）· {n_cells} 格（n≥5）",
        "",
        "查表：`lookup_gap_distribution(payload, seg_daily=…, seg_intraday=…)`",
        "",
        "---",
        "模組：`scripts/calibrate_overnight_gap_seg.py`",
        "",
    ]
    return "\n".join(lines)
