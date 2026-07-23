"""Early TW–NQ divergence alert study on Yahoo 5m bars.

Sweeps open+{10,15,20,30,45,60}m horizons × thresholds for:
  - spread = TW ret(from open) − NQ ret(TW window)
  - TW alone from open
  - combo / two-stage confirms

Window limited by Yahoo 5m ~60d. Labels = session peak→trough ≥ thr.
"""

from __future__ import annotations

import json
import math
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from research.backtest.us_tw_divergence_sell_gate_study import (
    GateRule,
    TZ_ET,
    TZ_TW,
    _triggered,
    classify_hypothesis,
    evaluate_rule,
    fetch_yahoo_ohlc,
    session_intraday_max_dd_from_peak,
    slice_tw_session,
    slice_us_during_tw,
)

HORIZONS_MIN = (10, 15, 20, 30, 45, 60)
SPREAD_THRESHOLDS = (-0.3, -0.4, -0.5, -0.6, -0.8, -1.0, -1.2, -1.5)
TW_THRESHOLDS = (-0.3, -0.4, -0.5, -0.6, -0.8, -1.0, -1.2, -1.5)
EVENT_THRESHOLDS = (2.0, 2.5, 3.0)


def _px_at_or_before(df: pd.DataFrame, ts: pd.Timestamp, col: str = "close") -> float | None:
    if df is None or df.empty or col not in df.columns:
        return None
    local = df.copy()
    if local.index.tz is None:
        local.index = local.index.tz_localize(ts.tz)
    else:
        local.index = local.index.tz_convert(ts.tz)
    sub = local.loc[local.index <= ts]
    if sub.empty:
        return None
    v = float(sub.iloc[-1][col])
    return v if v > 0 else None


def _ret_pct(a: float | None, b: float | None) -> float:
    if a is None or b is None or not (a > 0) or not (b > 0):
        return float("nan")
    return (b - a) / a * 100.0


def build_session_calendar(tw_5m: pd.DataFrame) -> list[str]:
    if tw_5m.empty:
        return []
    local = tw_5m.copy()
    if local.index.tz is None:
        local.index = local.index.tz_localize(TZ_TW)
    else:
        local.index = local.index.tz_convert(TZ_TW)
    dates = sorted(
        {
            ts.strftime("%Y-%m-%d")
            for ts in local.index
            if "09:00" <= ts.strftime("%H:%M") <= "13:30"
        }
    )
    return dates


def day_label_and_features(
    session_date: str,
    *,
    tw_5m: pd.DataFrame,
    nq_5m: pd.DataFrame,
    event_threshold: float,
) -> dict[str, Any] | None:
    tw_sess = slice_tw_session(tw_5m, session_date)
    nq_sess = slice_us_during_tw(nq_5m, session_date)
    if len(tw_sess) < 3 or len(nq_sess) < 2:
        return None

    open_tw = pd.Timestamp(f"{session_date} 09:00", tz=TZ_TW)
    # TW open: prefer first bar open in session
    tw_open = float(tw_sess.iloc[0]["open"])
    nq_open = _px_at_or_before(nq_5m, open_tw.astimezone(TZ_ET), "close")
    if nq_open is None:
        nq_open = float(nq_sess.iloc[0]["open"]) if "open" in nq_sess.columns else float(nq_sess.iloc[0]["close"])

    intra = session_intraday_max_dd_from_peak(tw_sess)
    peak_tt = float(intra["peak_to_trough_pct"]) if pd.notna(intra["peak_to_trough_pct"]) else float("nan")
    row: dict[str, Any] = {
        "date": session_date,
        "tw_bars_5m": int(len(tw_sess)),
        "nq_bars_5m": int(len(nq_sess)),
        "tw_peak_to_trough_pct": peak_tt,
        "event_magnitude": peak_tt,
        "is_event": bool(peak_tt >= event_threshold) if math.isfinite(peak_tt) else False,
        "tw_open": tw_open,
        "nq_open": float(nq_open),
    }

    # path after open for remaining-DD analysis
    tw_close = float(tw_sess.iloc[-1]["close"])
    tw_low = float(tw_sess["low"].min())
    row["tw_close_from_open_pct"] = _ret_pct(tw_open, tw_close)
    row["tw_open_to_low_pct"] = _ret_pct(tw_open, tw_low)

    first_hit: dict[str, float | None] = {}
    for h in HORIZONS_MIN:
        ts = open_tw + timedelta(minutes=h)
        # only evaluate if session has reached this clock time
        if tw_sess.index.max() < ts:
            tw_px = nq_px = None
        else:
            tw_px = _px_at_or_before(tw_sess, ts, "close")
            nq_px = _px_at_or_before(nq_sess, ts.astimezone(nq_sess.index.tz), "close")
            if nq_px is None:
                nq_px = _px_at_or_before(nq_5m, ts.astimezone(TZ_ET), "close")
        tw_r = _ret_pct(tw_open, tw_px)
        nq_r = _ret_pct(float(nq_open), nq_px)
        spread = tw_r - nq_r if math.isfinite(tw_r) and math.isfinite(nq_r) else float("nan")
        row[f"tw_ret_{h}m"] = tw_r
        row[f"nq_ret_{h}m"] = nq_r
        row[f"spread_{h}m"] = spread
        # remaining drawdown after decision time (open→low after ts, vs price at ts)
        if tw_px and math.isfinite(tw_px):
            after = tw_sess.loc[tw_sess.index > ts]
            if after.empty:
                rem = 0.0
            else:
                rem_low = float(after["low"].min())
                rem = max(0.0, (tw_px - rem_low) / tw_px * 100.0)
            row[f"remain_dd_after_{h}m"] = rem
        else:
            row[f"remain_dd_after_{h}m"] = float("nan")

    # first time spread crosses key levels (clock minutes from 09:00)
    for thr in (-0.4, -0.6, -0.8, -1.0, -1.2):
        hit_min = None
        for ts in tw_sess.index.sort_values():
            mins = int((ts - open_tw).total_seconds() // 60)
            if mins < 5 or mins > 180:
                continue
            tw_px = float(tw_sess.loc[ts, "close"])
            nq_px = _px_at_or_before(nq_sess, ts.astimezone(nq_sess.index.tz), "close")
            if nq_px is None:
                nq_px = _px_at_or_before(nq_5m, ts.astimezone(TZ_ET), "close")
            sp = _ret_pct(tw_open, tw_px) - _ret_pct(float(nq_open), nq_px)
            if math.isfinite(sp) and sp <= thr:
                hit_min = mins
                break
        key = f"first_hit_spread_le_{str(thr).replace('.', 'p').replace('-', 'm')}m"
        # cleaner key
        row[f"first_hit_min_spread_le_{abs(thr):g}"] = hit_min
        first_hit[str(thr)] = hit_min

    # two-stage: 10m spread ≤ thr AND 20m still ≤ thr
    for thr in (-0.4, -0.6, -0.8, -1.0):
        s10 = row.get("spread_10m")
        s20 = row.get("spread_20m")
        ok = (
            isinstance(s10, float)
            and isinstance(s20, float)
            and math.isfinite(s10)
            and math.isfinite(s20)
            and s10 <= thr
            and s20 <= thr
        )
        row[f"stage_10_20_le_{abs(thr):g}"] = 1.0 if ok else 0.0

    # TW alone + NQ not confirming (NQ > -0.2 while TW weak)
    for h in (10, 15, 20, 30):
        tw_r = row.get(f"tw_ret_{h}m")
        nq_r = row.get(f"nq_ret_{h}m")
        solo = (
            isinstance(tw_r, float)
            and isinstance(nq_r, float)
            and math.isfinite(tw_r)
            and math.isfinite(nq_r)
            and tw_r <= -0.6
            and nq_r >= -0.25
        )
        row[f"solo_tw_le0p6_nq_ok_{h}m"] = 1.0 if solo else 0.0

    return row


def _score_oos(ev: dict[str, Any]) -> float:
    o = ev.get("oos") or {}
    p, r = o.get("precision") or 0.0, o.get("recall") or 0.0
    fpr = o.get("false_alarm_rate")
    if fpr is not None and fpr > 0.35:
        return -1.0
    if p + r == 0:
        return 0.0
    # prefer earlier lead in name via small bonus encoded outside
    return 2 * p * r / (p + r) - 0.15 * (fpr or 0.0)


def classify_early(eval_row: dict[str, Any]) -> str:
    """Slightly relaxed vs sell-all classify due to ~60d Yahoo 5m sample."""
    oos = eval_row.get("oos") or {}
    full = eval_row.get("full") or {}
    # Prefer full-sample when OOS events < 5
    use = oos if (oos.get("n_events") or 0) >= 5 else full
    prec = use.get("precision")
    rec = use.get("recall")
    fpr = use.get("false_alarm_rate")
    n_ev = use.get("n_events") or 0
    n_trig = use.get("n_triggers") or 0
    if prec is None or rec is None or n_ev < 3:
        return "weak"
    if prec >= 0.35 and rec >= 0.35 and (fpr is None or fpr <= 0.10) and n_trig >= 4:
        return "accepted"
    if prec >= 0.20 and rec >= 0.30 and (fpr is None or fpr <= 0.20):
        return "weak"
    return "rejected"


def sweep_feature_grid(
    features: pd.DataFrame,
    events: pd.DataFrame,
    *,
    is_end: str,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []

    # Spread at each horizon
    for h in HORIZONS_MIN:
        feat = f"spread_{h}m"
        if feat not in features.columns:
            continue
        for thr in SPREAD_THRESHOLDS:
            rule = GateRule(
                hypothesis_id=f"E-spread-{h}m",
                name=f"spread@{h}m≤{thr}",
                feature=feat,
                direction="lt",
                threshold=thr,
                lead_label=f"09:{h:02d}" if h < 60 else "10:00",
            )
            ev = evaluate_rule(features, events, rule, is_end=is_end)
            # attach remaining DD on triggers (full sample)
            m = events[["date", "is_event", "event_magnitude"]].merge(
                features[["date", feat, f"remain_dd_after_{h}m"]], on="date", how="inner"
            )
            m["trig"] = m[feat].apply(
                lambda v: _triggered(float(v) if pd.notna(v) else float("nan"), "lt", thr)
            )
            rem = m.loc[m["trig"], f"remain_dd_after_{h}m"]
            ev["remain_dd_after_trig_median"] = (
                None if rem.empty else round(float(rem.median()), 3)
            )
            ev["remain_dd_after_trig_mean"] = None if rem.empty else round(float(rem.mean()), 3)
            ev["horizon_min"] = h
            ev["family"] = "spread"
            ev["status"] = classify_early(ev)
            ev["score"] = _score_oos(ev) + max(0, (60 - h) / 60.0) * 0.05  # slight earliness bonus
            results.append(ev)

    # TW alone
    for h in HORIZONS_MIN:
        feat = f"tw_ret_{h}m"
        if feat not in features.columns:
            continue
        for thr in TW_THRESHOLDS:
            rule = GateRule(
                hypothesis_id=f"E-tw-{h}m",
                name=f"TW@{h}m≤{thr}",
                feature=feat,
                direction="lt",
                threshold=thr,
                lead_label=f"09:{h:02d}" if h < 60 else "10:00",
            )
            ev = evaluate_rule(features, events, rule, is_end=is_end)
            rem = features.merge(events[["date"]], on="date")
            mask = rem[feat].apply(
                lambda v: _triggered(float(v) if pd.notna(v) else float("nan"), "lt", thr)
            )
            rem_s = rem.loc[mask, f"remain_dd_after_{h}m"]
            ev["remain_dd_after_trig_median"] = (
                None if rem_s.empty else round(float(rem_s.median()), 3)
            )
            ev["horizon_min"] = h
            ev["family"] = "tw_alone"
            ev["status"] = classify_early(ev)
            ev["score"] = _score_oos(ev) + max(0, (60 - h) / 60.0) * 0.05
            results.append(ev)

    # Solo-kill style flags
    for h in (10, 15, 20, 30):
        feat = f"solo_tw_le0p6_nq_ok_{h}m"
        if feat not in features.columns:
            continue
        rule = GateRule(
            hypothesis_id=f"E-solo-{h}m",
            name=f"soloTW≤-0.6 & NQ≥-0.25 @{h}m",
            feature=feat,
            direction="gt",
            threshold=0.5,
            lead_label=f"09:{h:02d}",
        )
        ev = evaluate_rule(features, events, rule, is_end=is_end)
        ev["horizon_min"] = h
        ev["family"] = "solo"
        ev["status"] = classify_early(ev)
        ev["score"] = _score_oos(ev) + max(0, (60 - h) / 60.0) * 0.05
        results.append(ev)

    # Two-stage confirm
    for thr in (-0.4, -0.6, -0.8, -1.0):
        feat = f"stage_10_20_le_{abs(thr):g}"
        if feat not in features.columns:
            continue
        rule = GateRule(
            hypothesis_id="E-stage-10-20",
            name=f"10m&20m spread≤{thr}",
            feature=feat,
            direction="gt",
            threshold=0.5,
            lead_label="09:20",
        )
        ev = evaluate_rule(features, events, rule, is_end=is_end)
        ev["horizon_min"] = 20
        ev["family"] = "two_stage"
        ev["status"] = classify_early(ev)
        ev["score"] = _score_oos(ev) + 0.03
        results.append(ev)

    results.sort(key=lambda e: e.get("score") or -1, reverse=True)
    return results


def case_study_day(features: pd.DataFrame, session_date: str) -> dict[str, Any]:
    sub = features[features["date"] == session_date]
    if sub.empty:
        return {"date": session_date, "present": False}
    r = sub.iloc[0].to_dict()
    checks = []
    for h in HORIZONS_MIN:
        sp = r.get(f"spread_{h}m")
        tw = r.get(f"tw_ret_{h}m")
        checks.append(
            {
                "horizon_min": h,
                "tw_ret": None if not isinstance(sp, float) and not isinstance(tw, float) else (
                    None if not (isinstance(tw, float) and math.isfinite(tw)) else round(tw, 3)
                ),
                "nq_ret": None
                if not (isinstance(r.get(f"nq_ret_{h}m"), float) and math.isfinite(r.get(f"nq_ret_{h}m")))
                else round(float(r[f"nq_ret_{h}m"]), 3),
                "spread": None
                if not (isinstance(sp, float) and math.isfinite(sp))
                else round(float(sp), 3),
                "user_rule_spread_le_0.6": bool(isinstance(sp, float) and sp <= -0.6),
                "h2_like_spread_le_1.2": bool(isinstance(sp, float) and sp <= -1.2),
                "remain_dd_after": None
                if not (
                    isinstance(r.get(f"remain_dd_after_{h}m"), float)
                    and math.isfinite(r.get(f"remain_dd_after_{h}m"))
                )
                else round(float(r[f"remain_dd_after_{h}m"]), 3),
            }
        )
    return {
        "date": session_date,
        "present": True,
        "is_event_3pct": bool(r.get("is_event")),
        "peak_to_trough_pct": r.get("tw_peak_to_trough_pct"),
        "first_hit_min": {
            k.replace("first_hit_min_spread_le_", ""): r.get(k)
            for k in r
            if str(k).startswith("first_hit_min_spread_le_")
        },
        "horizons": checks,
    }


def _save_heatmap(path: Path, grid: pd.DataFrame, title: str) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(8.5, 4.8))
    im = ax.imshow(grid.values, aspect="auto", cmap="RdYlGn", vmin=0, vmax=0.6)
    ax.set_xticks(range(len(grid.columns)))
    ax.set_xticklabels([str(c) for c in grid.columns])
    ax.set_yticks(range(len(grid.index)))
    ax.set_yticklabels([str(i) for i in grid.index])
    ax.set_xlabel("threshold (spread ≤)")
    ax.set_ylabel("horizon (minutes after open)")
    ax.set_title(title)
    for i in range(grid.shape[0]):
        for j in range(grid.shape[1]):
            v = grid.values[i, j]
            if np.isfinite(v):
                ax.text(j, i, f"{v:.2f}", ha="center", va="center", fontsize=8)
    fig.colorbar(im, ax=ax, fraction=0.046, label="full-sample precision")
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=140)
    plt.close(fig)


def _save_case_path(path: Path, case: dict[str, Any]) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    if not case.get("present"):
        return
    hs = case["horizons"]
    xs = [h["horizon_min"] for h in hs]
    fig, ax = plt.subplots(figsize=(8, 4.2))
    ax.plot(xs, [h["tw_ret"] for h in hs], "o-", label="TW from open", color="#0b6e4f")
    ax.plot(xs, [h["nq_ret"] for h in hs], "o-", label="NQ in TW window", color="#c44e52")
    ax.plot(xs, [h["spread"] for h in hs], "s--", label="spread TW−NQ", color="#2c3e50")
    ax.axhline(-0.6, color="#e67e22", ls=":", label="user −0.6%")
    ax.axhline(-1.2, color="#8e44ad", ls=":", label="H2 −1.2%")
    ax.axhline(0, color="#999", lw=0.8)
    ax.set_xlabel("minutes after 09:00")
    ax.set_ylabel("%")
    ax.set_title(f"{case['date']} · early path (5m) · peak→trough={case.get('peak_to_trough_pct'):.2f}%")
    ax.legend(fontsize=8)
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=140)
    plt.close(fig)


def run_early_alert_study(
    *,
    date_start: str | None = None,
    date_end: str | None = None,
    is_end: str | None = None,
    event_threshold: float = 3.0,
    out_dir: Path,
    case_dates: tuple[str, ...] = ("2026-07-14",),
) -> dict[str, Any]:
    end_d = date.fromisoformat(date_end) if date_end else date.today()
    # Yahoo 5m ~60 calendar days
    start_d = date.fromisoformat(date_start) if date_start else (end_d - timedelta(days=59))
    out_dir.mkdir(parents=True, exist_ok=True)
    fig_dir = out_dir / "figs"
    fig_dir.mkdir(parents=True, exist_ok=True)

    tw_5m = fetch_yahoo_ohlc("^TWII", start_d, end_d, interval="5m")
    nq_5m = fetch_yahoo_ohlc("NQ=F", start_d, end_d, interval="5m")
    dates = []
    for d in build_session_calendar(tw_5m):
        if len(slice_us_during_tw(nq_5m, d)) >= 2:
            dates.append(d)

    rows = []
    for d in dates:
        row = day_label_and_features(
            d, tw_5m=tw_5m, nq_5m=nq_5m, event_threshold=event_threshold
        )
        if row:
            rows.append(row)
    features = pd.DataFrame(rows)
    if features.empty:
        raise RuntimeError("No overlapping TW/NQ 5m sessions")

    # default IS/OOS split: midpoint of available dates
    dates_sorted = features["date"].astype(str).tolist()
    if is_end is None:
        mid = dates_sorted[len(dates_sorted) // 2]
        is_end = mid
    events = features[["date", "is_event", "event_magnitude"]].copy()

    # sensitivity event rates
    sens = {}
    for thr in EVENT_THRESHOLDS:
        flag = features["tw_peak_to_trough_pct"] >= thr
        sens[f"peak_to_trough_ge_{thr:g}"] = {
            "n_events": int(flag.sum()),
            "rate": round(float(flag.mean()), 4),
        }

    grid_results = sweep_feature_grid(features, events, is_end=is_end)

    # precision heatmap for spread family (full sample)
    heat = []
    for h in HORIZONS_MIN:
        row_h = []
        for thr in SPREAD_THRESHOLDS:
            hit = next(
                (
                    e
                    for e in grid_results
                    if e.get("family") == "spread"
                    and e.get("horizon_min") == h
                    and e["rule"]["threshold"] == thr
                ),
                None,
            )
            row_h.append((hit or {}).get("full", {}).get("precision"))
        heat.append(row_h)
    heat_df = pd.DataFrame(heat, index=list(HORIZONS_MIN), columns=list(SPREAD_THRESHOLDS))
    heat_path = fig_dir / "spread_precision_heatmap.png"
    _save_heatmap(
        heat_path, heat_df.astype(float), "Full-sample precision · spread ≤ thr @ horizon"
    )

    # recall heatmap
    heat_r = []
    for h in HORIZONS_MIN:
        row_h = []
        for thr in SPREAD_THRESHOLDS:
            hit = next(
                (
                    e
                    for e in grid_results
                    if e.get("family") == "spread"
                    and e.get("horizon_min") == h
                    and e["rule"]["threshold"] == thr
                ),
                None,
            )
            row_h.append((hit or {}).get("full", {}).get("recall"))
        heat_r.append(row_h)
    heat_r_df = pd.DataFrame(heat_r, index=list(HORIZONS_MIN), columns=list(SPREAD_THRESHOLDS))
    heat_r_path = fig_dir / "spread_recall_heatmap.png"
    _save_heatmap(
        heat_r_path, heat_r_df.astype(float), "Full-sample recall · spread ≤ thr @ horizon"
    )

    # FPR heatmap
    heat_f = []
    for h in HORIZONS_MIN:
        row_h = []
        for thr in SPREAD_THRESHOLDS:
            hit = next(
                (
                    e
                    for e in grid_results
                    if e.get("family") == "spread"
                    and e.get("horizon_min") == h
                    and e["rule"]["threshold"] == thr
                ),
                None,
            )
            row_h.append((hit or {}).get("full", {}).get("false_alarm_rate"))
        heat_f.append(row_h)
    heat_f_df = pd.DataFrame(heat_f, index=list(HORIZONS_MIN), columns=list(SPREAD_THRESHOLDS))
    heat_f_path = fig_dir / "spread_fpr_heatmap.png"
    _save_heatmap(
        heat_f_path, heat_f_df.astype(float), "Full-sample FPR · spread ≤ thr @ horizon"
    )

    # user rule explicit + H2@60m baseline on 5m
    user_rule = next(
        (
            e
            for e in grid_results
            if e.get("family") == "spread"
            and e.get("horizon_min") == 10
            and e["rule"]["threshold"] == -0.6
        ),
        None,
    )
    h2_5m = next(
        (
            e
            for e in grid_results
            if e.get("family") == "spread"
            and e.get("horizon_min") == 60
            and e["rule"]["threshold"] == -1.2
        ),
        None,
    )

    # top candidates: prefer status accepted/weak, horizon<=30, fpr<=0.2
    def keep(e: dict[str, Any]) -> bool:
        full = e.get("full") or {}
        fpr = full.get("false_alarm_rate")
        return (fpr is None or fpr <= 0.25) and (full.get("n_triggers") or 0) >= 2

    tops = [e for e in grid_results if keep(e)][:12]

    cases = []
    plot_paths = [str(heat_path), str(heat_r_path), str(heat_f_path)]
    for cd in case_dates:
        case = case_study_day(features, cd)
        cases.append(case)
        if case.get("present"):
            p = fig_dir / f"case_{cd.replace('-', '')}_early_path.png"
            _save_case_path(p, case)
            plot_paths.append(str(p))

    # event-day first-hit distribution (NaN = never hit → count as miss for by-Xm rates)
    ev_days = features[features["is_event"]].copy()
    first_hit_summary = {}
    for thr in (0.4, 0.6, 0.8, 1.0, 1.2):
        col = f"first_hit_min_spread_le_{thr:g}"
        if col not in ev_days.columns:
            continue
        s = pd.to_numeric(ev_days[col], errors="coerce")
        hit = s.dropna()
        n_ev = int(len(ev_days))
        first_hit_summary[f"le_{-thr:g}"] = {
            "n_hit": int(hit.shape[0]),
            "n_events": n_ev,
            "median_min_among_hits": None if hit.empty else float(hit.median()),
            "p25_min_among_hits": None if hit.empty else float(hit.quantile(0.25)),
            "p75_min_among_hits": None if hit.empty else float(hit.quantile(0.75)),
            "hit_by_10m": float(((s <= 10) & s.notna()).sum() / n_ev) if n_ev else None,
            "hit_by_20m": float(((s <= 20) & s.notna()).sum() / n_ev) if n_ev else None,
            "hit_by_30m": float(((s <= 30) & s.notna()).sum() / n_ev) if n_ev else None,
            "never_hit_rate": float(s.isna().mean()) if n_ev else None,
        }

    # also re-label at 2.5% for robustness note
    features_25 = features.copy()
    features_25["is_event"] = features_25["tw_peak_to_trough_pct"] >= 2.5
    events_25 = features_25[["date", "is_event", "event_magnitude"]]
    user_25 = evaluate_rule(
        features_25,
        events_25,
        GateRule("E-user-10m-25", "spread@10m≤-0.6 (evt≥2.5)", "spread_10m", "lt", -0.6, "09:10"),
        is_end=is_end,
    )
    user_25["status"] = classify_early(user_25)

    payload = {
        "meta": {
            "date_start": dates_sorted[0],
            "date_end": dates_sorted[-1],
            "is_end": is_end,
            "n_days": len(features),
            "n_events_primary": int(events["is_event"].sum()),
            "event_threshold_pct": event_threshold,
            "tw_source": "^TWII 5m Yahoo",
            "nq_source": "NQ=F 5m Yahoo",
            "horizons_min": list(HORIZONS_MIN),
            "note": (
                "Yahoo 5m ≈60 calendar days only; not the ~2y 1h panel. "
                "Early alerts validated here; H2@60m remains the longer-history candidate."
            ),
        },
        "sensitivity": sens,
        "first_hit_on_events": first_hit_summary,
        "user_rule_10m_m0p6": user_rule,
        "h2_analog_60m_m1p2_on_5m": h2_5m,
        "user_rule_on_event_ge_2p5": user_25,
        "top_candidates": tops[:8],
        "all_spread_grid_summary": [
            {
                "horizon_min": e.get("horizon_min"),
                "threshold": e["rule"]["threshold"],
                "full": e.get("full"),
                "oos": e.get("oos"),
                "status": e.get("status"),
                "remain_dd_after_trig_median": e.get("remain_dd_after_trig_median"),
                "score": e.get("score"),
            }
            for e in grid_results
            if e.get("family") == "spread"
        ],
        "cases": cases,
        "plot_paths": plot_paths,
        "events": features.loc[
            features["is_event"],
            ["date", "tw_peak_to_trough_pct", "spread_10m", "spread_20m", "spread_30m", "spread_60m"],
        ].to_dict(orient="records"),
    }

    # persist tables
    features.to_csv(out_dir / "features_5m.csv", index=False)
    pd.DataFrame(
        [
            {
                "family": e.get("family"),
                "horizon_min": e.get("horizon_min"),
                "rule": e["rule"]["name"],
                "threshold": e["rule"]["threshold"],
                "status": e.get("status"),
                "full_prec": (e.get("full") or {}).get("precision"),
                "full_rec": (e.get("full") or {}).get("recall"),
                "full_fpr": (e.get("full") or {}).get("false_alarm_rate"),
                "full_trig": (e.get("full") or {}).get("n_triggers"),
                "oos_prec": (e.get("oos") or {}).get("precision"),
                "oos_rec": (e.get("oos") or {}).get("recall"),
                "oos_fpr": (e.get("oos") or {}).get("false_alarm_rate"),
                "remain_dd_med": e.get("remain_dd_after_trig_median"),
                "score": e.get("score"),
            }
            for e in grid_results
        ]
    ).to_csv(out_dir / "rules_grid.csv", index=False)

    md = render_markdown(payload)
    (out_dir / "report.md").write_text(md, encoding="utf-8")
    (out_dir / "summary.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    return {"payload": payload, "markdown": md, "features": features}


def render_markdown(payload: dict[str, Any]) -> str:
    meta = payload["meta"]
    lines = [
        "# US–TW early alert · 5m horizon sweep",
        "",
        f"- Window: `{meta['date_start']}` → `{meta['date_end']}` · IS end `{meta['is_end']}`",
        f"- Sample: **{meta['n_days']}** TW sessions with NQ 5m overlap · events≥{meta['event_threshold_pct']}%: **{meta['n_events_primary']}**",
        f"- Sources: {meta['tw_source']} · {meta['nq_source']}",
        f"- Note: {meta['note']}",
        "",
        "## Verdict",
        "",
    ]
    user = payload.get("user_rule_10m_m0p6") or {}
    h2 = payload.get("h2_analog_60m_m1p2_on_5m") or {}
    tops = payload.get("top_candidates") or []
    if user:
        uf, uo = user.get("full") or {}, user.get("oos") or {}
        lines += [
            f"### User rule: `spread_10m ≤ −0.6` @ 09:10 · status=`{user.get('status')}`",
            f"- Full: prec={uf.get('precision')} · rec={uf.get('recall')} · FPR={uf.get('false_alarm_rate')} · trig={uf.get('n_triggers')} · events={uf.get('n_events')}",
            f"- OOS: prec={uo.get('precision')} · rec={uo.get('recall')} · FPR={uo.get('false_alarm_rate')}",
            f"- Trigger-day remaining DD after 09:10 (median): {user.get('remain_dd_after_trig_median')}%",
            "",
        ]
    if h2:
        hf, ho = h2.get("full") or {}, h2.get("oos") or {}
        lines += [
            f"### H2 analog on 5m: `spread_60m ≤ −1.2` @ 10:00 · status=`{h2.get('status')}`",
            f"- Full: prec={hf.get('precision')} · rec={hf.get('recall')} · FPR={hf.get('false_alarm_rate')} · trig={hf.get('n_triggers')}",
            f"- OOS: prec={ho.get('precision')} · rec={ho.get('recall')} · FPR={ho.get('false_alarm_rate')}",
            "",
        ]
    lines += [
        "### Better early candidates (sorted)",
        "",
        "| Family | Rule | Status | Full prec | Full rec | Full FPR | RemainDD med | Horizon |",
        "|---|---|---|---:|---:|---:|---:|---:|",
    ]
    for e in tops[:8]:
        f = e.get("full") or {}
        lines.append(
            f"| {e.get('family')} | `{e['rule']['name']}` | {e.get('status')} | "
            f"{f.get('precision')} | {f.get('recall')} | {f.get('false_alarm_rate')} | "
            f"{e.get('remain_dd_after_trig_median')} | {e.get('horizon_min')} |"
        )
    lines += ["", "## Event-day: how early does spread first hit?", ""]
    for k, v in (payload.get("first_hit_on_events") or {}).items():
        lines.append(
            f"- spread {k}: among hits median **{v.get('median_min_among_hits')}** min · "
            f"by10m={v.get('hit_by_10m')} · by20m={v.get('hit_by_20m')} · by30m={v.get('hit_by_30m')} · "
            f"never_hit={v.get('never_hit_rate')} (n_events={v.get('n_events')}, n_hit={v.get('n_hit')})"
        )
    lines += ["", "## Sensitivity (event rate)", ""]
    for k, v in (payload.get("sensitivity") or {}).items():
        lines.append(f"- {k}: n={v.get('n_events')} · rate={v.get('rate')}")
    lines += ["", "## Case studies", ""]
    for case in payload.get("cases") or []:
        if not case.get("present"):
            lines.append(f"- {case.get('date')}: missing in 5m panel")
            continue
        lines.append(
            f"### {case['date']} · event={case.get('is_event_3pct')} · "
            f"peak→trough={case.get('peak_to_trough_pct'):.2f}%"
        )
        lines.append("")
        lines.append("| +min | TW | NQ | spread | ≤−0.6? | ≤−1.2? | remain DD |")
        lines.append("|---:|---:|---:|---:|---|---|---:|")
        for h in case.get("horizons") or []:
            lines.append(
                f"| {h['horizon_min']} | {h['tw_ret']} | {h['nq_ret']} | {h['spread']} | "
                f"{h['user_rule_spread_le_0.6']} | {h['h2_like_spread_le_1.2']} | {h['remain_dd_after']} |"
            )
        lines.append(f"- first hit minutes: `{case.get('first_hit_min')}`")
        lines.append("")
    lines += [
        "## Recommendation（分層警報 · 5m overlay）",
        "",
        "樣本僅 Yahoo 5m ≈39 日 / 8 個 ≥3% 事件，**不能**取代 ~2y 的 H2@60m。建議疊加而非替換：",
        "",
        "1. **黃燈（軟）· 09:10**：`spread_10m ≤ −0.6` — 用戶提議規則。本窗 prec≈0.44 · FPR≈16% → 只做停加碼／提高警覺，不當 sell-all。",
        "2. **橙燈（較可用的 10m）· 09:10**：`spread_10m ≤ −1.2` **或** `TW_ret_10m ≤ −1.0` — 同窗假警較低（FPR≈6–10%），今日 7/14 也會觸發。",
        "3. **紅燈確認 · 09:20**：`spread_10m≤−0.8` **且** `spread_20m≤−0.8`（two-stage）— 本窗 accepted 一檔，較單純 −0.6 乾淨。",
        "4. **保留 H2 · 10:00**：`spread_60m ≤ −1.2` 仍是假警最低的主閘門（5m 子樣本上也最乾淨）。",
        "5. **漏接型**：如 2026-07-07，早盤 spread 未弱、午後才殺 → 任何 10–30m 規則都會 miss；早警報不是萬靈丹。",
        "",
        "## Terminology",
        "",
        "Research layer（研究層）only — not Order layer（下單層）auto sell-all.",
        "",
    ]
    return "\n".join(lines) + "\n"
