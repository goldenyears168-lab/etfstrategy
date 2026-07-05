"""Overnight / hold · RRG Signal Test（Optuma / Kempenaer 官網路径 · 大宇宙）。

Y = 相对基准 IX0001 超额（%），非绝对 gap。
检验：每次信号 → forward excess → 全样本 mean + t / Wilcoxon + bootstrap · OOS 时间切分。
Universe：all_kbar + 13:00 scale（全成分有 1m kbar 者）；不与 fresh_mono 策略池耦合。
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from typing import Any, Literal

import numpy as np
import pandas as pd
from scipy import stats

from analytics.bench import bench_open
from market_benchmark import load_benchmark_close
from research.backtest.finpilot_local_backtest import load_price_panels
from research.backtest.overnight_gap_seg_calibration import (
    _assign_quintile,
    _assign_quintile_from_bounds,
    _batch_intraday_seg_full_rrg,
    _quintile_bounds,
    _split_train_test,
)
from research.backtest.rrg_mono_backtest import build_fresh_mono_calendar
from research.backtest.rrg_mono_intraday_ab import _kbar_px, intraday_price_scale
from rrg_mono_daily_brief import LENGTH, LOOKBACK, _feat, _fresh_mono, _mono_tier2
from rrg_rotation import compute_rrg_panel

UniverseMode = Literal["all_kbar", "fresh_mono"]
IntradaySegMode = Literal["scale", "full_rrg"]
Horizon = Literal["overnight", "hold_1", "hold_3", "hold_5", "hold_7"]

SNAPSHOT_MINUTE = "13:00:00"
BENCHMARK = "IX0001"

QUADRANTS = ("leading", "weakening", "lagging", "improving")

# Optuma / Kempenaer 官網 Signal Test 事件（大宇宙 · 無策略池篩選）
AUTHOR_SIGNAL_KINDS = (
    "lagging_up_left",
    "improving_up_right",
    "leading_up_right",
    "weakening_down_right",
    "leading",
    "weakening",
    "lagging",
    "improving",
    "enter_improving",
    "enter_lagging",
    "enter_leading",
    "enter_weakening",
    "seg_last_q4",
    "seg_last_q1",
)

# 向後相容別名
SIGNAL_KINDS = AUTHOR_SIGNAL_KINDS

OPTUMA_PAIR_COMPARISONS: tuple[tuple[str, str, str], ...] = (
    ("lagging_up_left", "leading", "Lagging·heading NE vs Leading（Optuma 早買）"),
    ("improving_up_right", "leading", "Improving·UR vs Leading"),
    ("enter_improving", "enter_leading", "Enter Improving vs Enter Leading"),
    ("seg_last_q4", "seg_last_q1", "seg_last Q4 vs Q1"),
)

HORIZON_LABELS: dict[Horizon, str] = {
    "overnight": "T 收盤 → T+1 開盤 · 相對 IX0001",
    "hold_1": "T 收盤 → T+1 收盤",
    "hold_3": "T 收盤 → T+3 收盤",
    "hold_5": "T 收盤 → T+5 收盤",
    "hold_7": "T 收盤 → T+7 收盤",
}

DEFAULT_HORIZONS: tuple[Horizon, ...] = (
    "overnight",
    "hold_1",
    "hold_3",
    "hold_5",
    "hold_7",
)


def _stock_overnight_pct(
    close_df: pd.DataFrame,
    open_df: pd.DataFrame,
    stock_id: str,
    trade_date: str,
    next_date: str,
) -> float | None:
    if trade_date not in close_df.index or next_date not in open_df.index:
        return None
    if stock_id not in close_df.columns or stock_id not in open_df.columns:
        return None
    c = float(close_df.at[trade_date, stock_id])
    o = float(open_df.at[next_date, stock_id])
    if not (np.isfinite(c) and np.isfinite(o) and c > 0):
        return None
    return (o / c - 1.0) * 100.0


def _stock_close_pct(
    close_df: pd.DataFrame,
    stock_id: str,
    start: str,
    end: str,
) -> float | None:
    if start not in close_df.index or end not in close_df.index:
        return None
    if stock_id not in close_df.columns:
        return None
    c0 = float(close_df.at[start, stock_id])
    c1 = float(close_df.at[end, stock_id])
    if not (np.isfinite(c0) and np.isfinite(c1) and c0 > 0):
        return None
    return (c1 / c0 - 1.0) * 100.0


def _bench_overnight_pct(
    conn: sqlite3.Connection,
    trade_date: str,
    next_date: str,
) -> float | None:
    from analytics.bench import bench_close

    c = bench_close(conn, trade_date)
    o = bench_open(conn, next_date)
    if c is None or o is None or c <= 0:
        return None
    return (o / c - 1.0) * 100.0


def _bench_close_pct(conn: sqlite3.Connection, start: str, end: str) -> float | None:
    from analytics.bench import bench_close

    c0 = bench_close(conn, start)
    c1 = bench_close(conn, end)
    if c0 is None or c1 is None or c0 <= 0:
        return None
    return (c1 / c0 - 1.0) * 100.0


def _event_flags(feat: dict[str, Any], *, seg_q: int | None) -> dict[str, bool]:
    end_q = str(feat.get("end_q") or "")
    trend = str(feat.get("trend") or "")
    quads = feat.get("quadrants") or []
    prev_q = str(quads[-2]) if len(quads) >= 2 else ""

    flags: dict[str, bool] = {}
    for q in QUADRANTS:
        flags[q] = end_q == q
    flags["lagging_up_left"] = end_q == "lagging" and trend == "up_left"
    flags["improving_up_right"] = end_q == "improving" and trend == "up_right"
    flags["leading_up_right"] = end_q == "leading" and trend == "up_right"
    flags["weakening_down_right"] = end_q == "weakening" and trend == "down_right"
    if prev_q:
        for q in QUADRANTS:
            flags[f"enter_{q}"] = prev_q != q and end_q == q
    else:
        for q in QUADRANTS:
            flags[f"enter_{q}"] = False
    flags["seg_last_q4"] = seg_q == 4 if seg_q is not None else False
    flags["seg_last_q1"] = seg_q == 1 if seg_q is not None else False
    return flags


def _forward_return_pair(
    conn: sqlite3.Connection,
    close_df: pd.DataFrame,
    open_df: pd.DataFrame,
    full_dates: list[str],
    *,
    stock_id: str,
    trade_date: str,
    horizon: Horizon,
) -> tuple[float | None, float | None]:
    if trade_date not in full_dates:
        return None, None
    si = full_dates.index(trade_date)
    if horizon == "overnight":
        if si + 1 >= len(full_dates):
            return None, None
        nxt = full_dates[si + 1]
        rs = _stock_overnight_pct(close_df, open_df, stock_id, trade_date, nxt)
        rb = _bench_overnight_pct(conn, trade_date, nxt)
    else:
        hold = {"hold_1": 1, "hold_3": 3, "hold_5": 5, "hold_7": 7}[horizon]
        if si + hold >= len(full_dates):
            return None, None
        exit_d = full_dates[si + hold]
        rs = _stock_close_pct(close_df, stock_id, trade_date, exit_d)
        rb = _bench_close_pct(conn, trade_date, exit_d)
    return rs, rb


def _forward_excess(
    conn: sqlite3.Connection,
    close_df: pd.DataFrame,
    open_df: pd.DataFrame,
    full_dates: list[str],
    *,
    stock_id: str,
    trade_date: str,
    horizon: Horizon,
) -> float | None:
    rs, rb = _forward_return_pair(
        conn,
        close_df,
        open_df,
        full_dates,
        stock_id=stock_id,
        trade_date=trade_date,
        horizon=horizon,
    )
    if rs is None or rb is None:
        return None
    return rs - rb


def _compound_pct(returns_pct: list[float]) -> float:
    eq = 1.0
    for r in returns_pct:
        eq *= 1.0 + float(r) / 100.0
    return round((eq - 1.0) * 100.0, 4)


def _max_drawdown_pct(returns_pct: list[float]) -> float:
    if not returns_pct:
        return 0.0
    eq = 1.0
    peak = 1.0
    mdd = 0.0
    for r in returns_pct:
        eq *= 1.0 + float(r) / 100.0
        peak = max(peak, eq)
        mdd = min(mdd, (eq / peak - 1.0) * 100.0)
    return round(mdd, 4)


def collect_signal_events(
    conn: sqlite3.Connection,
    *,
    date_start: str,
    date_end: str,
    universe: UniverseMode = "all_kbar",
    intraday_seg_mode: IntradaySegMode = "scale",
    seg_q_bounds: list[float] | None = None,
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

    by_day: dict[str, list[str]] = defaultdict(list)
    for sid, td in pairs:
        if td not in full_dates:
            continue
        si = full_dates.index(td)
        if si < LOOKBACK + 1:
            continue
        by_day[td].append(sid)

    kbar_cache: dict = {}
    rows: list[dict[str, Any]] = []

    for trade_date in sorted(by_day.keys()):
        si = full_dates.index(trade_date)
        signal_date = full_dates[si - 1]
        sids = by_day[trade_date]
        seg_i_map: dict[str, float] = {}
        if intraday_seg_mode == "full_rrg":
            seg_i_map = _batch_intraday_seg_full_rrg(
                conn, close, bench, full_dates, trade_date, sids, kbar_cache
            )

        day_seg: list[tuple[str, float]] = []
        day_feats: dict[str, dict[str, Any]] = {}
        for sid in sids:
            feat = _feat(rs_r, rs_m, full_dates, si - 1, sid)
            if feat is None:
                continue
            day_feats[sid] = feat
            day_seg.append((sid, float(feat["seg_last"])))

        seg_q_map: dict[str, int | None] = {}
        if day_seg:
            seg_series = pd.Series({s: v for s, v in day_seg})
            if seg_q_bounds and len(seg_q_bounds) >= 2:
                q = _assign_quintile_from_bounds(seg_series, seg_q_bounds)
            else:
                q = _assign_quintile(seg_series)
            for sid, qi in q.items():
                seg_q_map[sid] = int(qi) if pd.notna(qi) else None

        for sid in sids:
            feat = day_feats.get(sid)
            if feat is None:
                continue
            if universe == "fresh_mono":
                fresh = bool(
                    _fresh_mono(rs_r, rs_m, full_dates, si - 1, sid) and _mono_tier2(feat)
                )
                if not fresh:
                    continue
            seg_d = float(feat["seg_last"])
            if intraday_seg_mode == "full_rrg":
                seg_rank = seg_i_map.get(sid)
            else:
                px = _kbar_px(conn, sid, trade_date, SNAPSHOT_MINUTE, kbar_cache, close)
                c = float(close.at[trade_date, sid]) if sid in close.columns else None
                seg_rank = (
                    seg_d * intraday_price_scale(c, px)
                    if c and c > 0 and px
                    else seg_d
                )
            flags = _event_flags(feat, seg_q=seg_q_map.get(sid))
            rec: dict[str, Any] = {
                "trade_date": trade_date,
                "signal_date": signal_date,
                "stock_id": sid,
                "seg_last_daily": seg_d,
                "seg_last_rank": seg_rank,
                "end_q": feat.get("end_q"),
                "trend": feat.get("trend"),
                **{f"ev_{k}": v for k, v in flags.items()},
            }
            for h in HORIZON_LABELS:
                rs, rb = _forward_return_pair(
                    conn,
                    close,
                    opn,
                    full_dates,
                    stock_id=sid,
                    trade_date=trade_date,
                    horizon=h,
                )
                rec[f"ret_{h}"] = rs
                rec[f"bench_ret_{h}"] = rb
                rec[f"ex_{h}"] = (rs - rb) if rs is not None and rb is not None else None
            rows.append(rec)

    return pd.DataFrame(rows)


def _bootstrap_mean_ci(values: list[float], *, n_boot: int = 2000, seed: int = 42) -> dict[str, float]:
    arr = np.asarray(values, dtype=float)
    if len(arr) < 10:
        return {}
    rng = np.random.default_rng(seed)
    boots = [float(rng.choice(arr, size=len(arr), replace=True).mean()) for _ in range(n_boot)]
    return {
        "ci2.5": round(float(np.percentile(boots, 2.5)), 4),
        "ci97.5": round(float(np.percentile(boots, 97.5)), 4),
    }


def bh_fdr_correct(p_values: list[float]) -> list[float]:
    """Benjamini-Hochberg FDR q-values, same order as input p_values."""
    m = len(p_values)
    if m == 0:
        return []
    order = sorted(range(m), key=lambda i: p_values[i])
    q = [1.0] * m
    for rank0, idx in enumerate(order):
        q[idx] = min(p_values[idx] * m / (rank0 + 1), 1.0)
    cur_min = 1.0
    for idx in reversed(order):
        cur_min = min(cur_min, q[idx])
        q[idx] = cur_min
    return q


def quarterly_stability(
    df: pd.DataFrame,
    kind: str,
    *,
    horizon: Horizon = "overnight",
) -> dict[str, Any]:
    """Per-calendar-quarter signal-test breakdown for stability auditing."""
    col_ev, col_ex = f"ev_{kind}", f"ex_{horizon}"
    if col_ev not in df.columns or col_ex not in df.columns:
        return {}
    sub = df[df[col_ev].astype(bool)].dropna(subset=[col_ex]).copy()
    if sub.empty:
        return {}
    sub["_quarter"] = pd.to_datetime(sub["trade_date"]).dt.to_period("Q").astype(str)
    result: dict[str, Any] = {}
    for qlabel, grp in sub.groupby("_quarter"):
        s = summarize_excess_series(grp[col_ex].tolist())
        result[str(qlabel)] = {
            "n": s.get("n"),
            "mean_excess_pct": s.get("mean_excess_pct"),
            "win_rate_pct": s.get("win_rate_pct"),
            "cohen_d": s.get("cohen_d"),
            "p_value_ttest": s.get("p_value_ttest"),
            "p_value_wilcoxon": s.get("p_value_wilcoxon"),
            "significant_005": s.get("significant_005"),
        }
    return result


def summarize_excess_series(excess: list[float]) -> dict[str, Any]:
    arr = [float(x) for x in excess if x is not None and np.isfinite(x)]
    if len(arr) < 3:
        return {"n": len(arr), "insufficient": True}
    mean_ex = float(np.mean(arr))
    t_stat, p_t = stats.ttest_1samp(arr, 0.0)
    try:
        _, p_w = stats.wilcoxon(arr)
    except ValueError:
        p_w = None
    win = sum(1 for x in arr if x > 0) / len(arr) * 100
    std_arr = float(np.std(arr, ddof=1))
    out = {
        "n": len(arr),
        "mean_excess_pct": round(mean_ex, 4),
        "median_excess_pct": round(float(np.median(arr)), 4),
        "std_excess_pct": round(std_arr, 4),
        "win_rate_pct": round(win, 2),
        "t_stat": round(float(t_stat), 4),
        "p_value_ttest": float(p_t),
        "p_value_wilcoxon": float(p_w) if p_w is not None else None,
        "significant_005": bool(p_t < 0.05),
        **_bootstrap_mean_ci(arr),
    }
    out["cohen_d"] = round(mean_ex / std_arr, 4) if std_arr > 0 else 0.0
    wins_count = sum(1 for x in arr if x > 0)
    _wz = (wins_count / len(arr) - 0.5) / np.sqrt(0.25 / len(arr))
    out["win_rate_z"] = round(_wz, 4)
    out["p_value_binom_winrate"] = float(2.0 * stats.norm.sf(abs(_wz)))
    return out


def run_signal_test_for_kind(
    df: pd.DataFrame,
    kind: str,
    *,
    horizon: Horizon = "overnight",
) -> dict[str, Any]:
    col_ev = f"ev_{kind}"
    col_ex = f"ex_{horizon}"
    if col_ev not in df.columns or col_ex not in df.columns:
        return {"insufficient": True}
    sub = df[df[col_ev].astype(bool)].dropna(subset=[col_ex])
    summary = summarize_excess_series(sub[col_ex].tolist())
    summary["signal_kind"] = kind
    summary["horizon"] = horizon
    summary["label"] = f"{kind} · {HORIZON_LABELS[horizon]}"
    return summary


def compare_signal_pair(
    df: pd.DataFrame,
    kind_a: str,
    kind_b: str,
    *,
    horizon: Horizon = "overnight",
    label: str = "",
) -> dict[str, Any]:
    col_a, col_b = f"ev_{kind_a}", f"ev_{kind_b}"
    col_ex = f"ex_{horizon}"
    if col_a not in df.columns or col_b not in df.columns:
        return {"insufficient": True}
    a = df[df[col_a].astype(bool)].dropna(subset=[col_ex])[col_ex].tolist()
    b = df[df[col_b].astype(bool)].dropna(subset=[col_ex])[col_ex].tolist()
    if len(a) < 3 or len(b) < 3:
        return {"insufficient": True, "n_a": len(a), "n_b": len(b)}
    sa = summarize_excess_series(a)
    sb = summarize_excess_series(b)
    t_stat, p_diff = stats.ttest_ind(a, b, equal_var=False)
    out = {
        "kind_a": kind_a,
        "kind_b": kind_b,
        "horizon": horizon,
        "label": label or f"{kind_a} vs {kind_b}",
        "n_a": sa["n"],
        "n_b": sb["n"],
        "mean_a_pct": sa["mean_excess_pct"],
        "mean_b_pct": sb["mean_excess_pct"],
        "mean_diff_pct": round(float(sa["mean_excess_pct"]) - float(sb["mean_excess_pct"]), 4),
        "a_beats_b": bool(sa["mean_excess_pct"] > sb["mean_excess_pct"]),
        "t_stat_welch": round(float(t_stat), 4),
        "p_value_diff": float(p_diff),
        "significant_005": bool(p_diff < 0.05),
    }
    return out


def run_all_signal_tests(
    df: pd.DataFrame,
    *,
    horizons: tuple[Horizon, ...] = DEFAULT_HORIZONS,
    kinds: tuple[str, ...] = AUTHOR_SIGNAL_KINDS,
) -> dict[str, Any]:
    results: dict[str, Any] = {}
    for kind in kinds:
        results[kind] = {}
        for h in horizons:
            results[kind][h] = run_signal_test_for_kind(df, kind, horizon=h)
    return results


def run_oos_signal_tests(
    df: pd.DataFrame,
    train_ratio: float = 0.7,
    *,
    kinds: tuple[str, ...] = AUTHOR_SIGNAL_KINDS,
    primary_horizon: Horizon = "overnight",
) -> dict[str, Any]:
    cut, _, train_df, test_df = _split_train_test(df, train_ratio)
    bounds = _quintile_bounds(train_df, "seg_last_daily")
    # Re-tag seg_last_q4 on test with train bounds
    if not test_df.empty and bounds:
        test_copy = test_df.copy()
        q = _assign_quintile_from_bounds(test_copy["seg_last_daily"], bounds)
        test_copy["ev_seg_last_q4"] = q == 4
        test_df = test_copy

    out: dict[str, Any] = {
        "train_ratio": train_ratio,
        "cut_date": cut,
        "train_n": len(train_df),
        "test_n": len(test_df),
        "seg_q_bounds_train": bounds,
        "train": {},
        "test": {},
    }
    for kind in kinds:
        out["train"][kind] = run_signal_test_for_kind(train_df, kind, horizon=primary_horizon)
        out["test"][kind] = run_signal_test_for_kind(test_df, kind, horizon=primary_horizon)
    return out


def run_equal_weight_portfolio_by_signal(
    df: pd.DataFrame,
    kind: str,
    *,
    horizon: Horizon = "overnight",
) -> dict[str, Any]:
    """官網 portfolio 路徑：每日觸發該信號標的等權平均 excess。"""
    col_ev, col_ex = f"ev_{kind}", f"ex_{horizon}"
    if col_ev not in df.columns:
        return {"insufficient": True}
    daily: list[float] = []
    for _, grp in df.groupby("trade_date"):
        sub = grp[grp[col_ev].astype(bool)].dropna(subset=[col_ex])
        if sub.empty:
            continue
        daily.append(float(sub[col_ex].mean()))
    summary = summarize_excess_series(daily)
    summary["portfolio"] = f"ew_daily_{kind}"
    summary["horizon"] = horizon
    summary["n_days"] = len(daily)
    summary["signal_kind"] = kind
    return summary


BACKTEST_SIGNAL_KINDS = (
    "lagging_up_left",
    "improving_up_right",
    "leading_up_right",
    "leading",
    "weakening",
    "enter_improving",
    "enter_weakening",
    "seg_last_q4",
)


def simulate_overnight_ew_compound(
    df: pd.DataFrame,
    kind: str,
    *,
    cash_when_empty: bool = True,
) -> dict[str, Any]:
    """T 收盤等權進場 → T+1 開盤出場；無信號日持現（0%）。"""
    col_ev, col_ret, col_bench = "ev_{}".format(kind), "ret_overnight", "bench_ret_overnight"
    if col_ev not in df.columns:
        return {"insufficient": True}
    dates = sorted(df["trade_date"].astype(str).unique())
    strat: list[float] = []
    bench: list[float] = []
    invested = 0
    for td in dates:
        sub = df[(df["trade_date"].astype(str) == td) & df[col_ev].astype(bool)].dropna(
            subset=[col_ret, col_bench]
        )
        if sub.empty:
            if cash_when_empty:
                strat.append(0.0)
                bench.append(0.0)
            continue
        invested += 1
        strat.append(float(sub[col_ret].mean()))
        bench.append(float(sub[col_bench].iloc[0]))
    if len(strat) < 3:
        return {"insufficient": True, "n_days": len(strat)}
    excess_daily = [s - b for s, b in zip(strat, bench)]
    return {
        "signal_kind": kind,
        "horizon": "overnight",
        "entry": "T close → T+1 open · EW",
        "n_calendar_days": len(dates),
        "n_invested_days": invested,
        "invested_pct": round(invested / len(dates) * 100, 2),
        "total_return_pct": _compound_pct(strat),
        "bench_total_return_pct": _compound_pct(bench),
        "excess_total_return_pct": round(
            _compound_pct(strat) - _compound_pct(bench), 4
        ),
        "max_drawdown_pct": _max_drawdown_pct(strat),
        "mean_daily_return_pct": round(float(np.mean(strat)), 4),
        "win_rate_pct": round(sum(1 for x in strat if x > 0) / len(strat) * 100, 2),
    }


def simulate_hold7_nonoverlap_ew(
    df: pd.DataFrame,
    kind: str,
) -> dict[str, Any]:
    """有信號日 T close 等權進場 → T+7 close；持倉期間不重複開新倉。"""
    col_ev, col_ret, col_bench = "ev_{}".format(kind), "ret_hold_7", "bench_ret_hold_7"
    if col_ev not in df.columns:
        return {"insufficient": True}
    dates = sorted(df["trade_date"].astype(str).unique())
    full_dates = dates  # subset calendar
    strat_blocks: list[float] = []
    bench_blocks: list[float] = []
    busy_until: str | None = None
    n_trades = 0
    for td in dates:
        if busy_until is not None and td <= busy_until:
            continue
        sub = df[(df["trade_date"].astype(str) == td) & df[col_ev].astype(bool)].dropna(
            subset=[col_ret, col_bench]
        )
        if sub.empty:
            continue
        strat_blocks.append(float(sub[col_ret].mean()))
        bench_blocks.append(float(sub[col_bench].iloc[0]))
        n_trades += 1
        si = full_dates.index(td)
        if si + 7 < len(full_dates):
            busy_until = full_dates[si + 7]
    if n_trades < 2:
        return {"insufficient": True, "n_trades": n_trades}
    return {
        "signal_kind": kind,
        "horizon": "hold_7",
        "entry": "T close → T+7 close · EW · non-overlap",
        "n_trades": n_trades,
        "total_return_pct": _compound_pct(strat_blocks),
        "bench_total_return_pct": _compound_pct(bench_blocks),
        "excess_total_return_pct": round(
            _compound_pct(strat_blocks) - _compound_pct(bench_blocks), 4
        ),
        "mean_trade_return_pct": round(float(np.mean(strat_blocks)), 4),
        "win_rate_pct": round(sum(1 for x in strat_blocks if x > 0) / len(strat_blocks) * 100, 2),
    }


def simulate_bench_overnight_all_days(df: pd.DataFrame) -> dict[str, Any]:
    """樣本日曆上 IX0001 每日 overnight 複利（對照）。"""
    dates = sorted(df["trade_date"].astype(str).unique())
    bench: list[float] = []
    for td in dates:
        row = df[df["trade_date"].astype(str) == td].dropna(subset=["bench_ret_overnight"])
        if row.empty:
            continue
        bench.append(float(row["bench_ret_overnight"].iloc[0]))
    if len(bench) < 3:
        return {"insufficient": True}
    return {
        "signal_kind": "IX0001",
        "horizon": "overnight",
        "n_days": len(bench),
        "total_return_pct": _compound_pct(bench),
    }


def run_author_backtests(df: pd.DataFrame) -> dict[str, Any]:
    overnight = {
        k: simulate_overnight_ew_compound(df, k) for k in BACKTEST_SIGNAL_KINDS
    }
    hold7 = {k: simulate_hold7_nonoverlap_ew(df, k) for k in BACKTEST_SIGNAL_KINDS}
    overnight["IX0001_calendar"] = simulate_bench_overnight_all_days(df)
    return {"overnight_ew_compound": overnight, "hold7_nonoverlap_ew": hold7}


def run_author_portfolios(
    df: pd.DataFrame,
    *,
    kinds: tuple[str, ...] = (
        "lagging_up_left",
        "improving_up_right",
        "leading",
        "enter_improving",
        "seg_last_q4",
    ),
    horizon: Horizon = "overnight",
) -> dict[str, Any]:
    return {k: run_equal_weight_portfolio_by_signal(df, k, horizon=horizon) for k in kinds}


def run_large_universe_signal_test(
    conn: sqlite3.Connection,
    *,
    date_start: str,
    date_end: str,
    oos_train_ratio: float = 0.7,
) -> dict[str, Any]:
    """大宇宙 Optuma Signal Test（all_kbar + 13:00 scale）。"""
    df = collect_signal_events(
        conn,
        date_start=date_start,
        date_end=date_end,
        universe="all_kbar",
        intraday_seg_mode="scale",
    )
    comparisons = [
        compare_signal_pair(df, a, b, horizon="overnight", label=label)
        for a, b, label in OPTUMA_PAIR_COMPARISONS
    ]
    comparisons += [
        compare_signal_pair(df, a, b, horizon="hold_7", label=f"{label} · hold_7")
        for a, b, label in OPTUMA_PAIR_COMPARISONS
    ]
    signal_tests = run_all_signal_tests(df)

    # BH FDR correction across all overnight signals (addresses multiple-comparison bias)
    _ov_kinds = [
        k for k in AUTHOR_SIGNAL_KINDS
        if not (signal_tests.get(k) or {}).get("overnight", {}).get("insufficient")
        and "p_value_ttest" in ((signal_tests.get(k) or {}).get("overnight") or {})
    ]
    if _ov_kinds:
        _ov_ps = [float(signal_tests[k]["overnight"]["p_value_ttest"]) for k in _ov_kinds]
        _ov_qs = bh_fdr_correct(_ov_ps)
        for k, q_val in zip(_ov_kinds, _ov_qs):
            signal_tests[k]["overnight"]["q_fdr_bh"] = float(q_val)

    return {
        "method": "Optuma Signal Test · 大宇宙 all_kbar · relative excess vs IX0001",
        "universe": "all_kbar + scale @13:00",
        "date_start": date_start,
        "date_end": date_end,
        "benchmark": BENCHMARK,
        "signal_kinds": list(AUTHOR_SIGNAL_KINDS),
        "horizons": list(DEFAULT_HORIZONS),
        "n_events_rows": len(df),
        "signal_tests": signal_tests,
        "pair_comparisons": comparisons,
        "oos": run_oos_signal_tests(df, oos_train_ratio),
        "portfolios_ew_overnight": run_author_portfolios(df, horizon="overnight"),
        "portfolios_ew_hold7": run_author_portfolios(df, horizon="hold_7"),
        "backtests": run_author_backtests(df),
        "q4_quarterly_stability": quarterly_stability(df, "seg_last_q4", horizon="overnight"),
    }


def run_official_pipeline(
    conn: sqlite3.Connection,
    *,
    date_start: str,
    date_end: str,
    oos_train_ratio: float = 0.7,
) -> dict[str, Any]:
    """向後相容：等同 run_large_universe_signal_test。"""
    return run_large_universe_signal_test(
        conn,
        date_start=date_start,
        date_end=date_end,
        oos_train_ratio=oos_train_ratio,
    )


def render_signal_test_md(payload: dict[str, Any]) -> str:
    lines = [
        "# RRG · Optuma Signal Test（大宇宙 · 相对 IX0001）",
        "",
        f"区间：{payload['date_start']} .. {payload['date_end']} · 基准 **{payload['benchmark']}**",
        f"Universe：**{payload.get('universe', 'all_kbar + scale')}** · 事件列 **{payload.get('n_events_rows', '—')}** 行",
        "",
        payload.get("method", ""),
        "",
    ]

    st = payload.get("signal_tests") or {}
    lines += ["## Signal Test · 全事件 forward excess", ""]
    lines.append("| 信号 | horizon | n | mean excess | win% | Cohen d | t p | BH q | Wilcoxon p | OOS test p |")
    lines.append("|------|---------|---|-------------|------|---------|-----|------|-----------|------------|")
    oos = payload.get("oos") or {}
    oos_test = oos.get("test") or {}
    for kind in AUTHOR_SIGNAL_KINDS:
        for h in DEFAULT_HORIZONS:
            s = (st.get(kind) or {}).get(h) or {}
            if s.get("insufficient"):
                continue
            ot_p = (oos_test.get(kind) or {}).get("p_value_ttest", "—")
            if h != "overnight":
                ot_p = "—"
            cohen = s.get("cohen_d", "")
            bh_q = s.get("q_fdr_bh", "")
            p_w = s.get("p_value_wilcoxon", "")
            bh_q_str = (
                f"{bh_q:.2e}" if isinstance(bh_q, float) and bh_q < 0.001
                else (f"{bh_q:.4g}" if isinstance(bh_q, float) else "—")
            )
            p_w_str = f"{p_w:.4g}" if isinstance(p_w, float) else "—"
            lines.append(
                f"| {kind} | {h} | {s.get('n')} | {s.get('mean_excess_pct')}% "
                f"| {s.get('win_rate_pct')}% | {cohen} | {s.get('p_value_ttest', 0):.4g} "
                f"| {bh_q_str} | {p_w_str} | {ot_p} |"
            )
    lines.append("")

    comps = payload.get("pair_comparisons") or []
    if comps:
        lines += ["## Optuma 配对比较（Welch t · A mean − B mean）", ""]
        lines.append("| 比较 | horizon | n_A | n_B | mean_A | mean_B | diff | p | A>B |")
        lines.append("|------|---------|-----|-----|--------|--------|------|---|-----|")
        for c in comps:
            if c.get("insufficient"):
                continue
            lines.append(
                f"| {c.get('label')} | {c.get('horizon')} | {c.get('n_a')} | {c.get('n_b')} "
                f"| {c.get('mean_a_pct')}% | {c.get('mean_b_pct')}% | {c.get('mean_diff_pct')}% "
                f"| {c.get('p_value_diff', 0):.4g} | {c.get('a_beats_b')} |"
            )
        lines.append("")

    for pf_key, pf_title in (
        ("portfolios_ew_overnight", "等权 Portfolio · overnight"),
        ("portfolios_ew_hold7", "等权 Portfolio · hold_7"),
    ):
        pf_block = payload.get(pf_key) or {}
        if not pf_block:
            continue
        lines += [f"## {pf_title}", ""]
        for kind, pf in pf_block.items():
            if not isinstance(pf, dict) or pf.get("insufficient"):
                continue
            lines.append(
                f"- **{kind}**：mean **{pf.get('mean_excess_pct')}%** · "
                f"win {pf.get('win_rate_pct')}% · n_days={pf.get('n_days')} · "
                f"p={pf.get('p_value_ttest', 0):.4g}"
            )
        lines.append("")

    bt = payload.get("backtests") or {}
    ov = bt.get("overnight_ew_compound") or {}
    h7 = bt.get("hold7_nonoverlap_ew") or {}
    if ov or h7:
        lines += ["## 回测报酬率（复利）", ""]
        lines += [
            "规则：**overnight** = 有信号日 T 收盘等权 → T+1 开盘，无信号持现；",
            "**hold_7** = 有信号日 T 收盘等权 → T+7 收盘，持仓期间不重叠开新仓。",
            "",
        ]
        if ov:
            lines += ["### Overnight 复利", ""]
            lines.append("| 信号 | 总报酬 | 基准复利 | 超额 | 投入日% | maxDD | 胜率 |")
            lines.append("|------|--------|----------|------|---------|-------|------|")
            for kind, r in ov.items():
                if not isinstance(r, dict) or r.get("insufficient"):
                    continue
                lines.append(
                    f"| {kind} | {r.get('total_return_pct')}% | {r.get('bench_total_return_pct')}% "
                    f"| {r.get('excess_total_return_pct')}% | {r.get('invested_pct')}% "
                    f"| {r.get('max_drawdown_pct')}% | {r.get('win_rate_pct')}% |"
                )
            lines.append("")
        if h7:
            lines += ["### Hold 7 非重叠复利", ""]
            lines.append("| 信号 | 总报酬 | 基准复利 | 超额 | 成交笔 | 均笔 | 胜率 |")
            lines.append("|------|--------|----------|------|--------|------|------|")
            for kind, r in h7.items():
                if not isinstance(r, dict) or r.get("insufficient"):
                    continue
                lines.append(
                    f"| {kind} | {r.get('total_return_pct')}% | {r.get('bench_total_return_pct')}% "
                    f"| {r.get('excess_total_return_pct')}% | {r.get('n_trades')} "
                    f"| {r.get('mean_trade_return_pct')}% | {r.get('win_rate_pct')}% |"
                )
            lines.append("")

    qstab = payload.get("q4_quarterly_stability") or {}
    if qstab:
        lines += ["## seg_last Q4 · 季度穩定性（overnight · 相對 IX0001）", ""]
        lines.append("| 季度 | n | mean excess% | win% | Cohen d | t p | Wilcoxon p | 顯著? |")
        lines.append("|------|---|-------------|------|---------|-----|-----------|------|")
        for ql, s in sorted(qstab.items()):
            if not s:
                continue
            pw = s.get("p_value_wilcoxon")
            pw_str = f"{pw:.3g}" if isinstance(pw, float) else "—"
            sig = "✓" if s.get("significant_005") else "✗"
            lines.append(
                f"| {ql} | {s.get('n')} | {s.get('mean_excess_pct')}% "
                f"| {s.get('win_rate_pct')}% | {s.get('cohen_d')} "
                f"| {s.get('p_value_ttest', 0):.3g} | {pw_str} | {sig} |"
            )
        lines.append("")

    lines += ["---", "模組：`scripts/run_overnight_rrg_signal_test.py`", ""]
    return "\n".join(lines)
