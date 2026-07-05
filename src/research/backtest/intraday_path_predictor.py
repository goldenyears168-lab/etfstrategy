"""1 分 K 轨迹预测 · standalone（非 RRG/C18acc 叠加）。

Phase 0：session 路径标签 + base rate · time-to-peak
Phase 1：事件触发（D-A 首次 spike）· 条件 lift · logit AUC · OOS
"""

from __future__ import annotations

import math
import sqlite3
import statistics
from dataclasses import dataclass
from typing import Any, Literal

from intraday_extension_radar import (
    NO_TRADE_BEFORE,
    OPENING_SPIKE_END,
    build_bar_snapshots,
)
from research.backtest.extension_peak_feature_analysis import (
    ObsRow,
    OutcomeKey,
    _outcome_true,
    evaluate_predicates,
    logistic_peak_regression,
)
from stock_db.kbar import load_kbar_day_bars

PathId = Literal["P-A", "P-B", "P-C", "P-D", "P-E"]

HYPOTHESES_TRAJ: dict[str, str] = {
    "H-TRAJ-0a": "各路径 base rate 年际差 < 8pp",
    "H-TRAJ-0b": "P-B spike-fade 无条件率 ∈ [8%, 15%]",
    "H-TRAJ-0c": "spike 日 time-to-peak median ≤ 15 分",
    "H-TRAJ-1": "climax+2σ 对 fade_30m≥2% · lift≥2× · FDR q<0.05",
    "H-TRAJ-2": "heat≥70 对 fade_30m · Wilson CI 下界 > base",
    "H-TRAJ-3": "multivariate logit AUC − heat-only AUC ≥ 0.03（OOS）",
    "H-TRAJ-4": "opening_spike 事件 · P(fade_30m≥2%) base ∈ [8%, 30%]",
    "H-TRAJ-5": "OOS 2026H1 · 最佳 predicate lift 不劣全样本 −30%",
}


@dataclass(frozen=True)
class SessionPath:
    stock_id: str
    trade_date: str
    n_bars: int
    session_return_pct: float
    peak_minute: str
    peak_ext_prev_pct: float
    minutes_to_peak: int
    fade_from_high_eod_pct: float
    vwap_crosses: int
    orb_failed: bool
    path_trend: bool
    path_spike_fade: bool
    path_chop: bool
    path_orb_failed: bool
    path_climax_peak_early: bool
    opening_spike_day: bool


def _prior_close(
    conn: sqlite3.Connection,
    stock_id: str,
    trade_date: str,
    full_dates: list[str],
) -> float | None:
    prior = [d for d in full_dates if d < trade_date]
    if not prior:
        return None
    row = conn.execute(
        "SELECT close FROM stock_daily_bars WHERE stock_id=? AND trade_date=?",
        (stock_id, prior[-1]),
    ).fetchone()
    return float(row[0]) if row else None


def _ma20(
    conn: sqlite3.Connection,
    stock_id: str,
    trade_date: str,
    full_dates: list[str],
) -> float | None:
    dates = [d for d in full_dates if d <= trade_date][-20:]
    if len(dates) < 10:
        return None
    rows = conn.execute(
        f"""
        SELECT close FROM stock_daily_bars
        WHERE stock_id=? AND trade_date IN ({",".join("?" * len(dates))})
        ORDER BY trade_date
        """,
        (stock_id, *dates),
    ).fetchall()
    if len(rows) < 10:
        return None
    return sum(float(r[0]) for r in rows) / len(rows)


def _minute_to_offset(minute: str, open_minute: str = "09:00:00") -> int:
    def _parts(m: str) -> tuple[int, int]:
        p = m.split(":")
        return int(p[0]), int(p[1])

    oh, om = _parts(open_minute)
    mh, mm = _parts(minute)
    return (mh * 60 + mm) - (oh * 60 + om)


def _classify_session(
    snaps: tuple,
    *,
    stock_id: str,
    trade_date: str,
    prev_close: float,
    or_end: str = "09:15:00",
) -> SessionPath | None:
    if len(snaps) < 50:
        return None
    first = snaps[0]
    last = snaps[-1]
    session_ret = (last.close / prev_close - 1.0) * 100.0
    peak_idx = max(range(len(snaps)), key=lambda i: snaps[i].close)
    peak = snaps[peak_idx]
    peak_close = peak.close
    fade_eod = (last.close / peak_close - 1.0) * 100.0 if peak_close > 0 else 0.0

    or_snaps = [s for s in snaps if s.minute >= NO_TRADE_BEFORE and s.minute <= f"{or_end}"]
    or_high = max(s.close for s in or_snaps) if or_snaps else peak.close
    or_low = min(s.close for s in or_snaps) if or_snaps else first.close

    orb_failed = False
    broke_or = False
    for s in snaps:
        if s.minute <= f"{or_end}":
            continue
        if s.close > or_high:
            broke_or = True
        if broke_or and s.minute <= "10:00:00" and s.close < or_high:
            orb_failed = True
            break

    vwap_crosses = 0
    prev_below: bool | None = None
    for s in snaps:
        if prev_below is None:
            prev_below = s.below_vwap
            continue
        if s.below_vwap != prev_below:
            vwap_crosses += 1
        prev_below = s.below_vwap

    opening_spike = any(
        s.minute <= f"{OPENING_SPIKE_END}:00" and s.ext_prev_pct >= 3.0 for s in snaps
    )
    max_ext = max(s.ext_prev_pct for s in snaps)

    path_spike_fade = max_ext >= 4.0 and fade_eod <= -2.0
    path_trend = max_ext >= 4.0 and fade_eod > -2.0 and last.close >= last.vwap
    path_chop = vwap_crosses >= 3 and abs(session_ret) < 1.5
    path_orb_failed = orb_failed
    climax_early = False
    for i, s in enumerate(snaps):
        if s.minute > "10:00:00":
            break
        if (s.is_climax_bar or s.vol_z >= 2.0) and s.is_session_high:
            if abs(i - peak_idx) <= 15:
                climax_early = True
            break

    return SessionPath(
        stock_id=stock_id,
        trade_date=trade_date,
        n_bars=len(snaps),
        session_return_pct=round(session_ret, 3),
        peak_minute=peak.minute,
        peak_ext_prev_pct=round(peak.ext_prev_pct, 3),
        minutes_to_peak=_minute_to_offset(peak.minute),
        fade_from_high_eod_pct=round(fade_eod, 3),
        vwap_crosses=vwap_crosses,
        orb_failed=orb_failed,
        path_trend=path_trend,
        path_spike_fade=path_spike_fade,
        path_chop=path_chop,
        path_orb_failed=path_orb_failed,
        path_climax_peak_early=climax_early,
        opening_spike_day=opening_spike,
    )


def collect_session_paths(
    conn: sqlite3.Connection,
    *,
    date_start: str,
    date_end: str,
    min_bars: int = 50,
) -> list[SessionPath]:
    conn.row_factory = sqlite3.Row
    full_dates = [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT trade_date FROM stock_daily_bars ORDER BY trade_date"
        ).fetchall()
    ]
    sessions = conn.execute(
        """
        SELECT DISTINCT stock_id, trade_date FROM stock_kbar_1m
        WHERE trade_date >= ? AND trade_date <= ?
        ORDER BY trade_date, stock_id
        """,
        (date_start, date_end),
    ).fetchall()
    out: list[SessionPath] = []
    for row in sessions:
        sid, td = str(row["stock_id"]), str(row["trade_date"])
        prev = _prior_close(conn, sid, td, full_dates)
        if prev is None:
            continue
        bars = load_kbar_day_bars(conn, sid, td)
        if len(bars) < min_bars:
            continue
        ma20 = _ma20(conn, sid, td, full_dates)
        snaps = build_bar_snapshots(bars, prev_close=prev, ma20=ma20)
        sp = _classify_session(snaps, stock_id=sid, trade_date=td, prev_close=prev)
        if sp is None:
            continue
        out.append(sp)
    return out


def _obs_from_snap(
    *,
    sid: str,
    td: str,
    snaps: tuple,
    i: int,
    peak_idx: int,
    peak_close: float,
    last_close: float,
) -> ObsRow:
    s = snaps[i]
    future = snaps[i + 1 : i + 31]
    min_fwd = min(x.close for x in future) if future else s.close
    fade_30m = (min_fwd / s.close - 1.0) * 100.0
    fade_eod = (last_close / s.close - 1.0) * 100.0
    post_peak = i > peak_idx and bool(future) and all(x.close <= peak_close for x in future)
    fade_from_high = (s.close / peak_close - 1.0) * 100.0 if peak_close > 0 else 0.0
    opening = s.minute <= f"{OPENING_SPIKE_END}:00" and s.ext_prev_pct >= 3.0
    return ObsRow(
        stock_id=sid,
        trade_date=td,
        minute=s.minute,
        ext_prev_pct=s.ext_prev_pct,
        ext_vwap_sigma=s.ext_vwap_sigma,
        ext_ma20_pct=s.ext_ma20_pct,
        vol_z=s.vol_z,
        heat_score=s.heat_score,
        fade_from_high_pct=round(fade_from_high, 3),
        is_climax_bar=s.is_climax_bar,
        is_session_high=s.is_session_high,
        below_vwap=s.below_vwap,
        opening_spike=opening,
        is_session_peak=i == peak_idx,
        fade_30m_min_pct=fade_30m,
        fade_eod_pct=fade_eod,
        post_peak_no_hh_30m=post_peak,
    )


def collect_spike_event_observations(
    conn: sqlite3.Connection,
    *,
    date_start: str,
    date_end: str,
    min_spike_pct: float = 3.0,
    min_bars: int = 50,
) -> list[ObsRow]:
    """D-A：每 session 首次 ext_prev≥min_spike 的 1m 事件（PIT @ T）。"""
    conn.row_factory = sqlite3.Row
    full_dates = [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT trade_date FROM stock_daily_bars ORDER BY trade_date"
        ).fetchall()
    ]
    sessions = conn.execute(
        """
        SELECT DISTINCT stock_id, trade_date FROM stock_kbar_1m
        WHERE trade_date >= ? AND trade_date <= ?
        """,
        (date_start, date_end),
    ).fetchall()
    rows: list[ObsRow] = []
    for row in sessions:
        sid, td = str(row["stock_id"]), str(row["trade_date"])
        prev = _prior_close(conn, sid, td, full_dates)
        if prev is None:
            continue
        bars = load_kbar_day_bars(conn, sid, td)
        if len(bars) < min_bars:
            continue
        ma20 = _ma20(conn, sid, td, full_dates)
        snaps = build_bar_snapshots(bars, prev_close=prev, ma20=ma20)
        if len(snaps) < 30:
            continue
        peak_idx = max(range(len(snaps)), key=lambda j: snaps[j].close)
        peak_close = snaps[peak_idx].close
        last_close = snaps[-1].close
        for i, s in enumerate(snaps):
            if s.ext_prev_pct < min_spike_pct:
                continue
            obs = _obs_from_snap(
                sid=sid,
                td=td,
                snaps=snaps,
                i=i,
                peak_idx=peak_idx,
                peak_close=peak_close,
                last_close=last_close,
            )
            rows.append(obs)
            break
    return rows


def _path_base_rates(sessions: list[SessionPath]) -> dict[str, float]:
    n = len(sessions)
    if not n:
        return {}
    fields = {
        "P-A_trend": "path_trend",
        "P-B_spike_fade": "path_spike_fade",
        "P-C_chop": "path_chop",
        "P-D_orb_failed": "path_orb_failed",
        "P-E_climax_peak_early": "path_climax_peak_early",
    }
    return {
        k: round(sum(1 for s in sessions if getattr(s, attr)) / n * 100, 2)
        for k, attr in fields.items()
    }


def _yearly_rates(sessions: list[SessionPath], attr: str) -> dict[str, float]:
    by_year: dict[str, list[SessionPath]] = {}
    for s in sessions:
        y = s.trade_date[:4]
        by_year.setdefault(y, []).append(s)
    return {
        y: round(sum(1 for x in ss if getattr(x, attr)) / len(ss) * 100, 2)
        if ss
        else 0.0
        for y, ss in sorted(by_year.items())
    }


def _sigmoid(z: float) -> float:
    if z >= 0:
        e = math.exp(-z)
        return 1 / (1 + e)
    e = math.exp(z)
    return e / (1 + e)


def _fit_logit(X: list[list[float]], y: list[float], *, steps: int = 50) -> list[float]:
    beta = [0.0] * len(X[0])
    n = len(y)
    lr = 0.05 / math.sqrt(max(n / 5000, 1))
    for _ in range(steps):
        grad = [0.0] * len(beta)
        for xi, yi in zip(X, y):
            z = sum(b * x for b, x in zip(beta, xi))
            err = _sigmoid(z) - yi
            for j, xj in enumerate(xi):
                grad[j] += err * xj
        for j in range(len(beta)):
            beta[j] -= lr * grad[j] / n
    return beta


def _predict_proba(beta: list[float], X: list[list[float]]) -> list[float]:
    return [_sigmoid(sum(b * x for b, x in zip(beta, xi))) for xi in X]


def _auc(y_true: list[float], scores: list[float]) -> float:
    pairs = sorted(zip(scores, y_true), key=lambda x: x[0])
    n_pos = sum(y_true)
    n_neg = len(y_true) - n_pos
    if n_pos <= 0 or n_neg <= 0:
        return float("nan")
    rank_sum = 0.0
    for i, (_, yi) in enumerate(pairs, start=1):
        if yi > 0.5:
            rank_sum += i
    u = rank_sum - n_pos * (n_pos + 1) / 2
    return u / (n_pos * n_neg)


def _feature_vec(o: ObsRow) -> list[float]:
    return [
        1.0,
        o.ext_prev_pct / 10.0,
        o.ext_vwap_sigma,
        o.vol_z,
        o.heat_score / 100.0,
        float(o.is_climax_bar),
        float(o.is_session_high),
        float(o.opening_spike),
        float(o.below_vwap),
    ]


def _heat_only_score(o: ObsRow) -> float:
    return o.heat_score / 100.0


def logit_fade_oos(
    rows: list[ObsRow],
    *,
    oos_start: str = "2026-01-01",
) -> dict[str, Any]:
    train = [r for r in rows if r.trade_date < oos_start]
    test = [r for r in rows if r.trade_date >= oos_start]
    if len(train) < 200 or len(test) < 30:
        return {
            "skipped": True,
            "reason": "insufficient train/test split",
            "train_n": len(train),
            "test_n": len(test),
        }

    y_train = [1.0 if r.fade_30m_min_pct <= -2.0 else 0.0 for r in train]
    y_test = [1.0 if r.fade_30m_min_pct <= -2.0 else 0.0 for r in test]
    X_train = [_feature_vec(r) for r in train]
    X_test = [_feature_vec(r) for r in test]
    beta = _fit_logit(X_train, y_train)
    p_test = _predict_proba(beta, X_test)
    auc_multi = _auc(y_test, p_test)
    heat_test = [_heat_only_score(r) for r in test]
    auc_heat = _auc(y_test, heat_test)
    return {
        "train_n": len(train),
        "test_n": len(test),
        "test_base_rate_pct": round(sum(y_test) / len(y_test) * 100, 2),
        "auc_multivariate": round(auc_multi, 4),
        "auc_heat_only": round(auc_heat, 4),
        "delta_auc": round(auc_multi - auc_heat, 4),
    }


def _evaluate_traj_hypotheses(
    *,
    sessions: list[SessionPath],
    events: list[ObsRow],
    eval_spike: dict[str, list[dict[str, Any]]],
    logit_oos: dict[str, Any],
    oos_start: str,
) -> dict[str, dict[str, Any]]:
    rates = _path_base_rates(sessions)
    p_b = rates.get("P-B_spike_fade", 0)
    yearly_pb = _yearly_rates(sessions, "path_spike_fade")
    yr_vals = list(yearly_pb.values())
    yr_spread = max(yr_vals) - min(yr_vals) if yr_vals else 0

    spike_days = [s for s in sessions if s.peak_ext_prev_pct >= 4]
    ttp_open = [
        s.minutes_to_peak
        for s in sessions
        if s.opening_spike_day and s.peak_ext_prev_pct >= 4
    ]
    med_ttp = statistics.median(ttp_open) if ttp_open else None
    med_ttp_all = statistics.median([s.minutes_to_peak for s in spike_days]) if spike_days else None

    fade_eval = eval_spike.get("fade_30m_ge2", [])
    climax_row = next((r for r in fade_eval if r["predicate"] == "climax+2σ"), None)
    heat_row = next((r for r in fade_eval if r["predicate"] == "heat≥70"), None)

    opening_events = [e for e in events if e.opening_spike]
    ob = (
        sum(1 for e in opening_events if e.fade_30m_min_pct <= -2.0) / len(opening_events) * 100
        if opening_events
        else None
    )

    full_best = fade_eval[0] if fade_eval else {}
    oos_events = [e for e in events if e.trade_date >= oos_start]
    oos_eval = evaluate_predicates(oos_events, outcome="fade_30m_ge2", spike_only=False, min_n=30)
    oos_best = oos_eval[0] if oos_eval else {}

    return {
        "H-TRAJ-0a": {"pass": yr_spread < 8, "yearly_P-B_pct": yearly_pb, "spread_pp": yr_spread},
        "H-TRAJ-0b": {"pass": 8 <= p_b <= 15, "P-B_pct": p_b},
        "H-TRAJ-0c": {
            "pass": med_ttp is not None and med_ttp <= 15,
            "median_minutes_opening_spike_to_peak": med_ttp,
            "median_minutes_all_spike_days": med_ttp_all,
        },
        "H-TRAJ-1": {
            "pass": bool(
                climax_row
                and climax_row.get("lift", 0) >= 2
                and climax_row.get("q_fdr", 1) < 0.05
            ),
            "row": climax_row,
        },
        "H-TRAJ-2": {
            "pass": bool(
                heat_row
                and heat_row.get("ci_lo", 0) > heat_row.get("P_base", 0)
            ),
            "row": heat_row,
        },
        "H-TRAJ-3": {
            "pass": (logit_oos.get("delta_auc") or -999) >= 0.03,
            "logit_oos": logit_oos,
        },
        "H-TRAJ-4": {
            "pass": ob is not None and 8 <= ob <= 30,
            "opening_event_fade_pct": round(ob, 2) if ob is not None else None,
            "n_opening_events": len(opening_events),
        },
        "H-TRAJ-5": {
            "pass": (
                full_best.get("lift", 0) > 0
                and (oos_best.get("lift") or 0) >= full_best.get("lift", 0) * 0.7
            ),
            "full_best": full_best.get("predicate"),
            "full_lift": full_best.get("lift"),
            "oos_lift": oos_best.get("lift"),
        },
    }


def run_intraday_path_predictor(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2024-01-01",
    date_end: str = "2026-06-26",
    oos_start: str = "2026-01-01",
    min_spike_pct: float = 3.0,
) -> dict[str, Any]:
    sessions = collect_session_paths(conn, date_start=date_start, date_end=date_end)
    events = collect_spike_event_observations(
        conn,
        date_start=date_start,
        date_end=date_end,
        min_spike_pct=min_spike_pct,
    )
    opening_events = [e for e in events if e.opening_spike]

    outcomes: list[OutcomeKey] = [
        "fade_30m_ge2",
        "fade_eod_ge3",
        "post_peak_no_hh_30m",
        "is_session_peak",
    ]
    eval_events: dict[str, list[dict[str, Any]]] = {}
    eval_opening: dict[str, list[dict[str, Any]]] = {}
    for oc in outcomes:
        eval_events[oc] = evaluate_predicates(events, outcome=oc, spike_only=False, min_n=80)
        eval_opening[oc] = evaluate_predicates(
            opening_events, outcome=oc, spike_only=False, min_n=40
        )

    logit_oos = logit_fade_oos(events, oos_start=oos_start)
    logit_peak = logistic_peak_regression(events, spike_only=False, max_samples=50_000)

    hypo = _evaluate_traj_hypotheses(
        sessions=sessions,
        events=events,
        eval_spike=eval_events,
        logit_oos=logit_oos,
        oos_start=oos_start,
    )

    base_event = {
        oc: round(sum(1 for r in events if _outcome_true(r, oc)) / len(events) * 100, 2)
        for oc in outcomes
    } if events else {}

    spike_days = sum(1 for s in sessions if s.peak_ext_prev_pct >= 4)
    ttp_open = [
        s.minutes_to_peak
        for s in sessions
        if s.opening_spike_day and s.peak_ext_prev_pct >= 4
    ]
    ttp_all = [s.minutes_to_peak for s in sessions if s.peak_ext_prev_pct >= 4]

    top_fade = eval_events.get("fade_30m_ge2", [])[:8]

    return {
        "date_start": date_start,
        "date_end": date_end,
        "oos_start": oos_start,
        "min_spike_event_pct": min_spike_pct,
        "n_sessions": len(sessions),
        "n_spike_events": len(events),
        "n_opening_spike_events": len(opening_events),
        "n_spike_days": spike_days,
        "path_base_rates_pct": _path_base_rates(sessions),
        "event_outcome_base_pct": base_event,
        "time_to_peak_spike_days": {
            "median_min_opening_spike": statistics.median(ttp_open) if ttp_open else None,
            "median_min_all_spike": statistics.median(ttp_all) if ttp_all else None,
            "mean_min_all_spike": round(statistics.mean(ttp_all), 1) if ttp_all else None,
        },
        "predicate_eval_events": eval_events,
        "predicate_eval_opening": eval_opening,
        "logit_fade_oos": logit_oos,
        "logit_peak_events": logit_peak,
        "top_fade_predicates": top_fade,
        "hypotheses": HYPOTHESES_TRAJ,
        "hypothesis_results": hypo,
        "design": (
            "Universe=stock_kbar_1m 全覆蓋 · 非 RRG/C18acc。"
            "Phase0=session 路径标签 · Phase1=D-A 首次 spike 事件 1m PIT 预测 fade/peak。"
        ),
    }


def render_intraday_path_predictor_md(payload: dict[str, Any]) -> str:
    hypo = payload.get("hypothesis_results") or {}
    rates = payload.get("path_base_rates_pct") or {}
    ttp = payload.get("time_to_peak_spike_days") or {}
    logit = payload.get("logit_fade_oos") or {}

    lines = [
        "# 1 分 K 轨迹预测 · Phase 0+1（standalone）",
        "",
        f"**区间**：{payload.get('date_start')} .. {payload.get('date_end')}  ",
        f"**OOS**：{payload.get('oos_start')} .. {payload.get('date_end')}  ",
        f"**Sessions**：{payload.get('n_sessions')} · **Spike 事件（D-A）**：{payload.get('n_spike_events')} "
        f"（opening {payload.get('n_opening_spike_events')}）",
        "",
        payload.get("design", ""),
        "",
        "## Phase 0 · Session 路径 base rate（%）",
        "",
        "| 路径 | 说明 | 比率 |",
        "|------|------|------|",
        "| P-A trend | 曾 ext≥4% · EOD fade<2% · 收 VWAP 上 | "
        f"{rates.get('P-A_trend', '—')} |",
        "| P-B spike-fade | 曾 ext≥4% · EOD fade≥2% | "
        f"{rates.get('P-B_spike_fade', '—')} |",
        "| P-C chop | VWAP 穿越≥3 · |日涨跌|<1.5% | "
        f"{rates.get('P-C_chop', '—')} |",
        "| P-D ORB failed | 突破 OR 后 45m 内收回 | "
        f"{rates.get('P-D_orb_failed', '—')} |",
        "| P-E climax peak early | 10:00 前 climax@high 且近 peak | "
        f"{rates.get('P-E_climax_peak_early', '—')} |",
        "",
        f"**Spike 日 time-to-peak**：opening spike median **{ttp.get('median_min_opening_spike')}** min · "
        f"all spike median {ttp.get('median_min_all_spike')} min",
        "",
        "## Phase 1 · 事件触发 outcome base（首次 ext≥3%）",
        "",
        "| Outcome | 事件样本 % |",
        "|---------|------------|",
    ]
    for oc, pct in (payload.get("event_outcome_base_pct") or {}).items():
        labels = {
            "fade_30m_ge2": "P(fade_30m≥2%)",
            "fade_eod_ge3": "P(EOD fade≥3%)",
            "post_peak_no_hh_30m": "P(高后30m无新高)",
            "is_session_peak": "P(当根=日高)",
        }
        lines.append(f"| {labels.get(oc, oc)} | {pct} |")

    lines += [
        "",
        "## Top predicates · fade_30m≥2%（事件样本）",
        "",
        "| predicate | n | P% | lift | q_fdr | sig |",
        "|-----------|---|-----|------|-------|-----|",
    ]
    for r in payload.get("top_fade_predicates") or []:
        lines.append(
            f"| {r.get('predicate')} | {r.get('n')} | {r.get('P')} | {r.get('lift')} "
            f"| {r.get('q_fdr')} | {'✓' if r.get('significant') else ''} |"
        )

    lines += [
        "",
        "## Logit OOS · fade_30m≥2%",
        "",
        f"- train n={logit.get('train_n')} · test n={logit.get('test_n')} · base={logit.get('test_base_rate_pct')}%",
        f"- AUC multivariate **{logit.get('auc_multivariate')}** · heat-only {logit.get('auc_heat_only')} · "
        f"Δ **{logit.get('delta_auc')}**",
        "",
        "## 假设检验",
        "",
        "| ID | 假设 | 结果 |",
        "|----|------|------|",
    ]
    for hid, text in (payload.get("hypotheses") or {}).items():
        r = hypo.get(hid, {})
        lines.append(f"| {hid} | {text} | {'✓' if r.get('pass') else '✗'} |")

    passed = sum(1 for r in hypo.values() if r.get("pass"))
    lines += [
        "",
        f"**通过 {passed}/{len(hypo)}** · 下一步 Phase 2：path-gate × ORB/VWAP standalone 回测（H-TRAJ-6…9）",
        "",
        "---",
        "模块：`scripts/run_intraday_path_predictor.py`",
        "",
    ]
    return "\n".join(lines)
