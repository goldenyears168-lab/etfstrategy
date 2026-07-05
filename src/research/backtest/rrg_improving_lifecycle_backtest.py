"""RRG Improving lifecycle backtest · staged observation framework.

Stages (signal-day close, PIT):
  S0  fresh flip into Improving（前日非 improving · 當日 improving）
  S1  Improving 第 1–3 天（連續 improving streak）
  S2  Setup core（up_right · 4 日皆 improving · 非 mono_tier2 · mono_up=False
      · disp∈[0.25,0.85) · excess∈[-2%,-0.3%] · 排除當日漲≥1% / 跌≤-2%）
  S3  Improving 大漲（當日 improving · 漲幅≥burst_pct）
  S4  RV 98–99 overlay（RS-Ratio ∈ [98,99) · 即將 Leading）

Transitions:
  S2 → S3 next day（+5% lift vs unconditional）
  S3 → next day（依台指方向分層）
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Any

import numpy as np
import pandas as pd

from market_benchmark import load_benchmark_close
from project_config import DEFAULT_ETF_CODES
from research.backtest.finpilot_local_backtest import load_price_panels
from rrg_mono_daily_brief import _feat, _mono_tier2
from rrg_rotation import compute_rrg_panel
from stock_db import connect, load_etf_constituent_watchlist

BURST_PCT = 5.0
LOOKBACK_MIN = 4


@dataclass(frozen=True)
class StageStats:
    stage_id: str
    label: str
    n_events: int
    n_signal_days: int
    mean_per_day: float
    next_mean_pct: float
    next_std_pct: float
    next_median_pct: float
    next_win_rate: float
    p_next_ge_burst: float
    ew_portfolio_mean: float
    ew_portfolio_std: float
    ew_portfolio_win_rate: float
    same_day_mean_pct: float | None = None
    p_same_day_ge_burst: float | None = None


@dataclass(frozen=True)
class TransitionStats:
    transition_id: str
    label: str
    n_from: int
    n_hit: int
    hit_rate_pct: float
    baseline_improving_pct: float
    baseline_all_pct: float
    baseline_stockday_improving_burst_pct: float
    lift_vs_improving: float
    lift_vs_all: float
    lift_vs_stockday_imp_burst: float
    horizon_days: int


@dataclass(frozen=True)
class StratifiedStats:
    group_id: str
    label: str
    parent_stage: str
    n_events: int
    next_mean_pct: float
    next_win_rate: float


def _base_setup(f: dict[str, Any] | None) -> bool:
    return bool(
        f
        and f["trend"] == "up_right"
        and all(q == "improving" for q in f["quadrants"])
        and not _mono_tier2(f)
        and not f["mono_up"]
        and 0.25 <= f["disp"] < 0.85
    )


def _setup_core(sig_ret: float, excess: float) -> bool:
    if sig_ret >= 1.0 or sig_ret <= -2.0:
        return False
    return -2.0 <= excess <= -0.3


def _watch_setup(base: bool, streak: int, sig_ret: float) -> bool:
    return bool(base and streak >= 4 and -1.0 <= sig_ret <= 0.5 and sig_ret > -2.0)


@dataclass(frozen=True)
class ImprovingDayRow:
    date: str
    stock_id: str
    stock_name: str
    sig_ret: float
    excess: float
    end_q: str
    improve_streak: int
    rv: float
    rs_momentum: float
    disp: float
    disp_d: float
    session_gap_days: int
    base_setup: bool
    s2_setup_core: bool
    s3_burst: bool
    s2_rv_98_99: bool
    s3_rv_98_99: bool
    watch_setup: bool
    disp_contract: bool
    quadrants: tuple[str, ...]
    bench_today_ret: float
    signal_close: float
    sid_s3rv_hist_n: int = 0
    sid_s3rv_hist_mean: float = float("nan")
    sid_s3rv_hist_ok: bool = False
    prev_end_q: str = ""
    prev_disp_contract: bool = False
    prev_base_setup: bool = False


def _stock_names(conn: sqlite3.Connection, stock_ids: list[str]) -> dict[str, str]:
    if not stock_ids:
        return {}
    placeholders = ",".join("?" * len(stock_ids))
    rows = conn.execute(
        f"""
        SELECT stock_id, stock_name
        FROM etf_holdings
        WHERE stock_id IN ({placeholders})
        GROUP BY stock_id
        """,
        stock_ids,
    ).fetchall()
    out = {str(r[0]): str(r[1] or "") for r in rows}
    for sid in stock_ids:
        out.setdefault(sid, "")
    return out


def scan_improving_signal_day(
    conn: sqlite3.Connection | None = None,
    signal_date: str | None = None,
    *,
    etf_codes: tuple[str, ...] = DEFAULT_ETF_CODES,
    burst_pct: float = BURST_PCT,
    close: pd.DataFrame | None = None,
    bench: pd.Series | None = None,
) -> tuple[list[ImprovingDayRow], str]:
    """PIT scan for one signal-day close · ETF constituent watchlist."""
    conn = conn or connect()
    close = close if close is not None else load_price_panels(conn)[0]
    bench = bench if bench is not None else load_benchmark_close(conn)
    rs_ratio, rs_mom, quad = compute_rrg_panel(close, bench)
    full_dates = close.index.astype(str).tolist()
    if signal_date is None:
        signal_date = full_dates[-1]
    if signal_date not in full_dates:
        prior = [d for d in full_dates if d <= signal_date]
        if not prior:
            return [], signal_date
        signal_date = prior[-1]
    si = full_dates.index(signal_date)
    if si < LOOKBACK_MIN:
        return [], signal_date

    watch = load_etf_constituent_watchlist(conn, etf_codes)
    universe = sorted({w["stock_id"] for w in watch})
    names = _stock_names(conn, universe)

    d = full_dates[si]
    dp = full_dates[si - 1]
    dp2 = full_dates[si - 2]
    bp = bench.get(dp)
    bp2 = bench.get(dp2)
    bd = bench.get(d)
    if any(x is None or (isinstance(x, float) and np.isnan(x)) for x in [bp, bp2, bd]):
        return [], signal_date
    if bp2 <= 0 or bp <= 0 or bd <= 0:
        return [], signal_date

    bench_prev_ret = (bp / bp2 - 1) * 100
    bench_today_ret = (bd / bp - 1) * 100
    session_gap = (datetime.strptime(d, "%Y-%m-%d").date() - datetime.strptime(dp, "%Y-%m-%d").date()).days

    rows: list[ImprovingDayRow] = []
    for sid in universe:
        if sid not in close.columns:
            continue
        c0 = close.at[d, sid]
        cp = close.at[dp, sid]
        if any(pd.isna(x) or x <= 0 for x in [c0, cp]):
            continue

        sig_ret = (c0 / cp - 1) * 100
        excess = sig_ret - bench_prev_ret
        end_q = quad.at[d, sid]
        if pd.isna(end_q):
            continue
        end_q = str(end_q)
        streak = _improving_streak(quad, full_dates, si, sid)
        f = _feat(rs_ratio, rs_mom, full_dates, si, sid)
        if not f:
            continue
        rv = float(f["rs_ratio"])
        prev_f = _feat(rs_ratio, rs_mom, full_dates, si - 1, sid) if si >= LOOKBACK_MIN else None
        disp_d = float(f["disp"] - prev_f["disp"]) if prev_f else float("nan")
        base = _base_setup(f)
        s2 = base and _setup_core(sig_ret, excess)
        s3 = end_q == "improving" and sig_ret >= burst_pct
        s2_rv = s2 and not np.isnan(rv) and 98.0 <= rv < 99.0
        s3_rv = s3 and not np.isnan(rv) and 98.0 <= rv < 99.0
        disp_contract = not np.isnan(disp_d) and disp_d < -0.1
        prev_end_q_raw = quad.at[dp, sid] if sid in quad.columns else None
        prev_end_q = "" if pd.isna(prev_end_q_raw) else str(prev_end_q_raw)
        prev_f_day = _feat(rs_ratio, rs_mom, full_dates, si - 1, sid) if si >= LOOKBACK_MIN else None
        prev_f2 = _feat(rs_ratio, rs_mom, full_dates, si - 2, sid) if si >= LOOKBACK_MIN + 1 else None
        prev_disp_d = (
            float(prev_f_day["disp"] - prev_f2["disp"])
            if prev_f_day and prev_f2
            else float("nan")
        )
        prev_disp_contract = not np.isnan(prev_disp_d) and prev_disp_d < -0.1
        prev_base = _base_setup(prev_f_day) if prev_f_day else False

        rows.append(
            ImprovingDayRow(
                date=d,
                stock_id=sid,
                stock_name=names.get(sid, ""),
                sig_ret=round(sig_ret, 4),
                excess=round(excess, 4),
                end_q=end_q,
                improve_streak=streak,
                rv=round(rv, 4),
                rs_momentum=round(float(f["rs_momentum"]), 4),
                disp=round(float(f["disp"]), 4),
                disp_d=round(disp_d, 4) if not np.isnan(disp_d) else float("nan"),
                session_gap_days=session_gap,
                base_setup=base,
                s2_setup_core=s2,
                s3_burst=s3,
                s2_rv_98_99=s2_rv,
                s3_rv_98_99=s3_rv,
                watch_setup=_watch_setup(base, streak, sig_ret),
                disp_contract=disp_contract,
                quadrants=tuple(str(q) for q in f["quadrants"]),
                bench_today_ret=round(bench_today_ret, 4),
                signal_close=round(float(c0), 4),
                prev_end_q=prev_end_q,
                prev_disp_contract=prev_disp_contract,
                prev_base_setup=prev_base,
            )
        )
    return rows, signal_date


def _improving_streak(quad: pd.DataFrame, full_dates: list[str], si: int, sid: str) -> int:
    streak = 0
    for k in range(si, -1, -1):
        q = quad.at[full_dates[k], sid]
        if pd.isna(q) or q != "improving":
            break
        streak += 1
    return streak


def _prev_quadrant(quad: pd.DataFrame, full_dates: list[str], si: int, sid: str) -> str | None:
    if si < 1 or sid not in quad.columns:
        return None
    q = quad.at[full_dates[si - 1], sid]
    return None if pd.isna(q) else str(q)


def _summarize_stage(
    df: pd.DataFrame,
    *,
    stage_id: str,
    label: str,
    all_signal_days: int,
    same_day_col: str | None = None,
) -> StageStats:
    if df.empty:
        return StageStats(
            stage_id=stage_id,
            label=label,
            n_events=0,
            n_signal_days=0,
            mean_per_day=0.0,
            next_mean_pct=0.0,
            next_std_pct=0.0,
            next_median_pct=0.0,
            next_win_rate=0.0,
            p_next_ge_burst=0.0,
            ew_portfolio_mean=0.0,
            ew_portfolio_std=0.0,
            ew_portfolio_win_rate=0.0,
        )
    daily = df.groupby("date").size()
    port = df.groupby("date")["nxt_ret"].mean()
    same_mean = float(df[same_day_col].mean()) if same_day_col else None
    p_same = float((df[same_day_col] >= BURST_PCT).mean()) if same_day_col else None
    return StageStats(
        stage_id=stage_id,
        label=label,
        n_events=len(df),
        n_signal_days=int(df["date"].nunique()),
        mean_per_day=round(len(df) / all_signal_days, 3),
        next_mean_pct=round(float(df["nxt_ret"].mean()), 4),
        next_std_pct=round(float(df["nxt_ret"].std()), 4) if len(df) > 1 else 0.0,
        next_median_pct=round(float(df["nxt_ret"].median()), 4),
        next_win_rate=round(float((df["nxt_ret"] > 0).mean()), 4),
        p_next_ge_burst=round(float((df["nxt_ret"] >= BURST_PCT).mean()), 4),
        ew_portfolio_mean=round(float(port.mean()), 4),
        ew_portfolio_std=round(float(port.std()), 4) if len(port) > 1 else 0.0,
        ew_portfolio_win_rate=round(float((port > 0).mean()), 4),
        same_day_mean_pct=round(same_mean, 4) if same_mean is not None else None,
        p_same_day_ge_burst=round(p_same, 4) if p_same is not None else None,
    )


def build_lifecycle_events(
    conn: sqlite3.Connection | None = None,
    *,
    etf_codes: tuple[str, ...] = DEFAULT_ETF_CODES,
    burst_pct: float = BURST_PCT,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    conn = conn or connect()
    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn)
    rs_ratio, rs_mom, quad = compute_rrg_panel(close, bench)
    full_dates = close.index.astype(str).tolist()
    watch = load_etf_constituent_watchlist(conn, etf_codes)
    universe = sorted({w["stock_id"] for w in watch})

    rows: list[dict[str, Any]] = []
    for si in range(LOOKBACK_MIN, len(full_dates) - 1):
        d = full_dates[si]
        dp = full_dates[si - 1]
        dp2 = full_dates[si - 2]
        bp = bench.get(dp)
        bp2 = bench.get(dp2)
        bd = bench.get(d)
        bdn = bench.get(full_dates[si + 1])
        if any(x is None or (isinstance(x, float) and np.isnan(x)) for x in [bp, bp2, bd, bdn]):
            continue
        if bp2 <= 0 or bp <= 0 or bd <= 0:
            continue
        bench_prev_ret = (bp / bp2 - 1) * 100
        bench_today_ret = (bd / bp - 1) * 100
        bench_nxt_ret = (bdn / bd - 1) * 100
        session_gap = (
            datetime.strptime(d, "%Y-%m-%d").date() - datetime.strptime(dp, "%Y-%m-%d").date()
        ).days

        for sid in universe:
            if sid not in close.columns:
                continue
            c0 = close.at[d, sid]
            cp = close.at[dp, sid]
            c1 = close.at[full_dates[si + 1], sid]
            if any(pd.isna(x) or x <= 0 for x in [c0, cp, c1]):
                continue

            sig_ret = (c0 / cp - 1) * 100
            nxt_ret = (c1 / c0 - 1) * 100
            excess = sig_ret - bench_prev_ret
            end_q = quad.at[d, sid]
            if pd.isna(end_q):
                continue
            end_q = str(end_q)
            streak = _improving_streak(quad, full_dates, si, sid)
            prev_q = _prev_quadrant(quad, full_dates, si, sid)
            f = _feat(rs_ratio, rs_mom, full_dates, si, sid)
            rv = float(f["rs_ratio"]) if f else float("nan")

            prev_f = _feat(rs_ratio, rs_mom, full_dates, si - 1, sid) if si >= LOOKBACK_MIN else None
            disp_d = float(f["disp"] - prev_f["disp"]) if f and prev_f else float("nan")

            rows.append(
                {
                    "date": d,
                    "sid": sid,
                    "sig_ret": sig_ret,
                    "excess": excess,
                    "nxt_ret": nxt_ret,
                    "end_q": end_q,
                    "prev_q": prev_q,
                    "improve_streak": streak,
                    "rv": rv,
                    "disp_d": disp_d,
                    "bench_today_ret": bench_today_ret,
                    "bench_nxt_ret": bench_nxt_ret,
                    "session_gap_days": session_gap,
                    "base_setup": _base_setup(f),
                    "s0_fresh_flip": end_q == "improving" and prev_q != "improving",
                    "s1_early": end_q == "improving" and 1 <= streak <= 3,
                    "s2_setup_core": _base_setup(f) and _setup_core(sig_ret, excess),
                    "s3_burst": end_q == "improving" and sig_ret >= burst_pct,
                    "s4_rv_98_99": not np.isnan(rv) and 98.0 <= rv < 99.0,
                    "s2_rv_98_99": _base_setup(f)
                    and _setup_core(sig_ret, excess)
                    and not np.isnan(rv)
                    and 98.0 <= rv < 99.0,
                    "s3_rv_98_99": end_q == "improving"
                    and sig_ret >= burst_pct
                    and not np.isnan(rv)
                    and 98.0 <= rv < 99.0,
                    "disp_contract": not np.isnan(disp_d) and disp_d < -0.1,
                }
            )

    df = pd.DataFrame(rows)
    meta = {
        "universe_size": len(universe),
        "date_start": full_dates[LOOKBACK_MIN],
        "date_end": full_dates[-2],
        "max_bar_date": full_dates[-1],
        "n_signal_days": len(full_dates) - LOOKBACK_MIN - 1,
        "burst_pct": burst_pct,
    }
    return df, meta


def run_lifecycle_backtest(
    conn: sqlite3.Connection | None = None,
    *,
    burst_pct: float = BURST_PCT,
) -> dict[str, Any]:
    df, meta = build_lifecycle_events(conn, burst_pct=burst_pct)
    all_days = int(meta["n_signal_days"])
    if df.empty:
        return {"meta": meta, "stages": [], "transitions": [], "stratified": []}

    stages: list[StageStats] = []
    stage_defs: list[tuple[str, str, str, str | None]] = [
        ("S0", "s0_fresh_flip", "fresh flip → Improving（前日非 improving）", "sig_ret"),
        ("S1", "s1_early", "Improving 第 1–3 天", None),
        ("S2", "s2_setup_core", "Setup core 蓄力帶", None),
        ("S2a", "s2_rv_98_99", "Setup core + RV 98–99", None),
        ("S3", "s3_burst", f"Improving 大漲日 ≥{burst_pct}%", None),
        ("S3a", "s3_rv_98_99", f"Improving 大漲 + RV 98–99", None),
        ("S4", "s4_rv_98_99", "RV 98–99（全市場 improving 相關）", None),
    ]
    for sid, col, label, same_col in stage_defs:
        sub = df[df[col]]
        stages.append(_summarize_stage(sub, stage_id=sid, label=label, all_signal_days=all_days, same_day_col=same_col))

    # S1 streak breakdown
    streak_stats: list[dict[str, Any]] = []
    for streak in [1, 2, 3]:
        sub = df[df["improve_streak"] == streak]
        burst_sub = sub[sub["sig_ret"] >= burst_pct]
        streak_stats.append(
            {
                "streak_days": streak,
                "n_stock_days": len(sub),
                "p_burst_same_day": round(float((sub["sig_ret"] >= burst_pct).mean()), 4) if len(sub) else 0.0,
                "burst_n": len(burst_sub),
                "after_burst_next_mean": round(float(burst_sub["nxt_ret"].mean()), 4) if len(burst_sub) else None,
            }
        )

    # Transitions
    transitions: list[TransitionStats] = []
    # S0 same-day burst already in stage
    s0 = df[df["s0_fresh_flip"]]
    s0_rate = float((s0["sig_ret"] >= burst_pct).mean()) if len(s0) else 0.0
    imp = df[df["end_q"] == "improving"]
    imp_burst_rate = float(imp["sig_ret"].ge(burst_pct).mean()) if len(imp) else 0.0
    all_burst_rate = float(df["sig_ret"].ge(burst_pct).mean()) if len(df) else 0.0
    stock_day_burst_rate = float(df["s3_burst"].sum() / len(df)) if len(df) else 0.0

    def _transition(
        tid: str,
        label: str,
        n_from: int,
        n_hit: int,
        hit_rate: float,
        horizon: int,
    ) -> TransitionStats:
        return TransitionStats(
            transition_id=tid,
            label=label,
            n_from=n_from,
            n_hit=n_hit,
            hit_rate_pct=round(hit_rate * 100, 3),
            baseline_improving_pct=round(imp_burst_rate * 100, 3),
            baseline_all_pct=round(all_burst_rate * 100, 3),
            baseline_stockday_improving_burst_pct=round(stock_day_burst_rate * 100, 3),
            lift_vs_improving=round(hit_rate / imp_burst_rate, 3) if imp_burst_rate else 0.0,
            lift_vs_all=round(hit_rate / all_burst_rate, 3) if all_burst_rate else 0.0,
            lift_vs_stockday_imp_burst=round(hit_rate / stock_day_burst_rate, 3)
            if stock_day_burst_rate
            else 0.0,
            horizon_days=horizon,
        )

    s2 = df[df["s2_setup_core"]]
    s2_next_burst = float((s2["nxt_ret"] >= burst_pct).mean()) if len(s2) else 0.0
    transitions.append(
        _transition(
            "S0_same_day",
            "S0 翻進 Improving 當日 ≥burst",
            len(s0),
            int((s0["sig_ret"] >= burst_pct).sum()),
            s0_rate,
            0,
        )
    )
    transitions.append(
        _transition(
            "S2_to_burst_D1",
            "Setup core → 隔日漲幅 ≥burst",
            len(s2),
            int((s2["nxt_ret"] >= burst_pct).sum()),
            s2_next_burst,
            1,
        )
    )

    # 2-day burst after setup (signal day D: burst on D+1 or D+2)
    hit2 = 0
    pairs2 = 0
    date_list = sorted(df["date"].unique())
    date_to_next: dict[str, str | None] = {
        date_list[i]: date_list[i + 1] if i + 1 < len(date_list) else None for i in range(len(date_list))
    }
    for _, row in s2.iterrows():
        pairs2 += 1
        if row["nxt_ret"] >= burst_pct:
            hit2 += 1
            continue
        d2 = date_to_next.get(str(row["date"]))
        if not d2:
            continue
        row2 = df[(df["date"] == d2) & (df["sid"] == row["sid"])]
        if not row2.empty and float(row2.iloc[0]["sig_ret"]) >= burst_pct:
            hit2 += 1
    rate2 = hit2 / pairs2 if pairs2 else 0.0
    transitions.append(
        _transition(
            "S2_to_burst_2d",
            "Setup core → 兩日內任一日 ≥burst",
            pairs2,
            hit2,
            rate2,
            2,
        )
    )

    # S3 next day by market / streak / disp overlay
    stratified: list[StratifiedStats] = []
    strat_defs: list[tuple[str, str, str, pd.Series]] = [
        ("S3_mkt_up", "S3 大漲後 · 隔日台指上漲", "S3", df["s3_burst"] & (df["bench_nxt_ret"] > 0)),
        ("S3_mkt_down", "S3 大漲後 · 隔日台指下跌", "S3", df["s3_burst"] & (df["bench_nxt_ret"] < 0)),
        ("S3_streak1", "S3 大漲 · improving 第 1 天", "S3", df["s3_burst"] & (df["improve_streak"] == 1)),
        ("S3_streak_2_3", "S3 大漲 · improving 第 2–3 天", "S3", df["s3_burst"] & df["improve_streak"].between(2, 3)),
        ("S3_streak_6p", "S3 大漲 · improving ≥6 天", "S3", df["s3_burst"] & (df["improve_streak"] >= 6)),
        ("S2_disp_contract", "Setup core + disp 收斂 (Δ<-0.1)", "S2", df["s2_setup_core"] & df["disp_contract"]),
        ("S2_disp_expand", "Setup core + disp 擴張 (Δ>0.1)", "S2", df["s2_setup_core"] & (df["disp_d"] > 0.1)),
    ]
    for gid, label, parent, mask in strat_defs:
        sub = df.loc[mask]
        if len(sub) < 10:
            continue
        stratified.append(
            StratifiedStats(
                group_id=gid,
                label=label,
                parent_stage=parent,
                n_events=len(sub),
                next_mean_pct=round(float(sub["nxt_ret"].mean()), 4),
                next_win_rate=round(float((sub["nxt_ret"] > 0).mean()), 4),
            )
        )

    # Recent 252d slice
    recent_dates = sorted(df["date"].unique())[-252:]
    recent = df[df["date"].isin(recent_dates)]
    recent_stages = []
    for sid, col, label, same_col in stage_defs[:4]:
        sub = recent[recent[col]]
        recent_stages.append(
            asdict(_summarize_stage(sub, stage_id=sid, label=label, all_signal_days=len(recent_dates), same_day_col=same_col))
        )

    return {
        "meta": meta,
        "stages": [asdict(s) for s in stages],
        "streak_breakdown": streak_stats,
        "transitions": [asdict(t) for t in transitions],
        "stratified": [asdict(s) for s in stratified],
        "recent_252d": recent_stages,
        "generated_on": date.today().isoformat(),
    }


def render_lifecycle_markdown(result: dict[str, Any]) -> str:
    meta = result["meta"]
    lines = [
        "# RRG Improving lifecycle backtest",
        "",
        f"- Universe: ETF constituents · **{meta['universe_size']}** names",
        f"- Signal window: **{meta['date_start']}** → **{meta['date_end']}**",
        f"- Max bar: **{meta['max_bar_date']}** · burst threshold: **≥{meta['burst_pct']}%**",
        f"- Generated: {result.get('generated_on', '')}",
        "",
        "## Stages（信號日收盤 → 隔日收盤）",
        "",
        "| Stage | 標籤 | N | 日均檔數 | 隔日均值 | 隔日σ | 勝率 | P(隔日≥burst) | 等權組合均値 |",
        "|-------|------|---|---------|---------|------|------|--------------|------------|",
    ]
    for s in result["stages"]:
        extra = ""
        if s.get("p_same_day_ge_burst") is not None:
            extra = f" · 當日P(burst)={s['p_same_day_ge_burst']*100:.1f}%"
        lines.append(
            f"| {s['stage_id']} | {s['label']} | {s['n_events']} | {s['mean_per_day']:.2f} | "
            f"{s['next_mean_pct']:+.3f}% | {s['next_std_pct']:.2f}% | {s['next_win_rate']*100:.1f}% | "
            f"{s['p_next_ge_burst']*100:.2f}% | {s['ew_portfolio_mean']:+.3f}%{extra} |"
        )

    lines.extend(["", "## Transitions", ""])
    lines.append(
        "| ID | 路徑 | N | 命中 | 命中率 | 基準(Imp當日) | 基準(全市場日) | 基準(Imp burst/全日) | Lift×③ |"
    )
    lines.append("|----|------|---|------|--------|-------------|--------------|-------------------|--------|")
    for t in result["transitions"]:
        lines.append(
            f"| {t['transition_id']} | {t['label']} | {t['n_from']} | {t['n_hit']} | "
            f"{t['hit_rate_pct']:.2f}% | {t['baseline_improving_pct']:.2f}% | {t['baseline_all_pct']:.2f}% | "
            f"{t['baseline_stockday_improving_burst_pct']:.2f}% | {t['lift_vs_stockday_imp_burst']:.2f}x |"
        )
    lines.extend(
        [
            "",
            "_③ = 相對於「任意成分股交易日出現 Improving≥burst」的基準率（≈0.43%）_",
        ]
    )

    lines.extend(["", "## Improving streak × 當日 burst", ""])
    lines.append("| 連續天數 | 樣本 | P(當日≥burst) | burst後隔日均值 |")
    lines.append("|---------|------|--------------|----------------|")
    for sb in result.get("streak_breakdown", []):
        aft = sb["after_burst_next_mean"]
        aft_s = f"{aft:+.3f}%" if aft is not None else "—"
        lines.append(
            f"| {sb['streak_days']} | {sb['n_stock_days']} | {sb['p_burst_same_day']*100:.2f}% | {aft_s} |"
        )

    lines.extend(["", "## Stratified（子群）", ""])
    lines.append("| Group | N | 隔日均值 | 勝率 |")
    lines.append("|-------|---|---------|------|")
    for g in result.get("stratified", []):
        lines.append(
            f"| {g['label']} | {g['n_events']} | {g['next_mean_pct']:+.3f}% | {g['next_win_rate']*100:.1f}% |"
        )

    if result.get("recent_252d"):
        lines.extend(["", "## 近 252 交易日", ""])
        for s in result["recent_252d"]:
            lines.append(
                f"- **{s['stage_id']}** {s['label']}: n={s['n_events']} "
                f"mean/day={s['mean_per_day']:.2f} next={s['next_mean_pct']:+.3f}% "
                f"P(burst)={s['p_next_ge_burst']*100:.2f}%"
            )

    lines.append("")
    return "\n".join(lines)
