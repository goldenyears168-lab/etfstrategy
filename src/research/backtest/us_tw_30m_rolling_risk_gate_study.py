"""TW↔NQ rolling 30m (6×5m) risk gate — no open-anchor for DETACH.

Research layer only. Objective priority (user):
  1) miss few ≥3% crash days (high recall)
  2) avoid selling near session trough
  3) avoid reentering before trough (buy high before low)
False alarms are secondary — rebuy OK.

PIT-safe: polls use completed 5m bars only. Proxy ^TWII × NQ=F.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.font_manager as fm
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


def _setup_cjk_font() -> None:
    """Prefer a CJK-capable face so calendar fig titles render on macOS."""
    available = {f.name for f in fm.fontManager.ttflist}
    for name in ("PingFang TC", "PingFang HK", "Heiti TC", "Arial Unicode MS", "Noto Sans CJK TC"):
        if name in available:
            plt.rcParams["font.sans-serif"] = [name, "DejaVu Sans"]
            plt.rcParams["axes.unicode_minus"] = False
            return

from research.backtest.us_tw_divergence_sell_gate_study import TZ_TW, slice_us_during_tw
from research.backtest.us_tw_rolling_5m_detach_study import (
    NOTIONAL,
    TW_CLOSE,
    TW_OPEN,
    build_poll_frame_5m,
    fetch_yahoo_5m_panel,
)

# 6 × 5m bars ≈ 30 minutes (DETACH)
LOOKBACK_MIN = 30
N_BARS = LOOKBACK_MIN // 5  # 6
SELL_NEAR_TROUGH_BP = 0.35  # exit within 35bp of trough = "sold at low"
EVENT_PTT = 3.0
GAP_EPS = 0.05  # pct-pt for IMPROVE/WORSEN on Δspread_30m

WIN30_GRID = (-0.5, -0.6, -0.8, -1.0, -1.2, -1.5)
CONFIRM_GRID = (1, 2)
# DETACH cores for rebuy overlays
FOCUS_DETACH = ((-0.6, 2), (-0.6, 1))
# User: cooldown ≥ 1h; softer δ; rebuy on short window (5/10m) so 7/14 ~11:25–12:00 attainable
COOLDOWN_POLLS = (12, 14, 16)  # 60 / 70 / 80 minutes
OUTPERFORM_MARGIN = (0.05, 0.10, 0.15)
REBUY_WINDOW_MIN = (5, 10)  # not 30m — midday bounce shows on short window first
FEE_RT_HAIRCUT_PCT = 0.10
TARGET_REBUY_LO = "11:25"
TARGET_REBUY_HI = "12:00"


@dataclass(frozen=True)
class RiskGateSpec:
    strategy_id: str
    name: str
    win30_thr: float
    confirm_polls: int = 1
    reenter: bool = False
    # repair_mode: none | co_sync | legacy
    repair_mode: str = "none"
    repair_same_sign_min: float = 0.8
    repair_delta_30: float = 0.3
    repair_min_polls_after: int = 0
    outperform_margin: float = 0.0
    repair_window_min: int = 30  # lookback for both↑ / outperform (5 or 10 for co_sync)
    require_tw_pos: bool = True
    require_nq_pos: bool = False
    require_tw_gt_nq: bool = True
    block_reenter_new_low: bool = True


def registered_grid() -> list[RiskGateSpec]:
    """Finite grid: detach-only + co_sync(≥60m cool, soft δ, short rebuy window)."""
    specs: list[RiskGateSpec] = []

    def add(**kw: Any) -> None:
        specs.append(RiskGateSpec(**kw))

    for thr in WIN30_GRID:
        for conf in CONFIRM_GRID:
            tag = f"_c{conf}" if conf > 1 else ""
            add(
                strategy_id=f"rg30_det{thr}{tag}",
                name=f"DETACH spread_30m≤{thr}×{conf} · no reenter",
                win30_thr=thr,
                confirm_polls=conf,
                reenter=False,
                repair_mode="none",
            )

    for thr, conf in FOCUS_DETACH:
        tag = f"_c{conf}" if conf > 1 else ""
        for wait in COOLDOWN_POLLS:
            for marg in OUTPERFORM_MARGIN:
                for rw in REBUY_WINDOW_MIN:
                    add(
                        strategy_id=f"rg30_det{thr}{tag}_co_w{wait}_m{marg}_r{rw}",
                        name=(
                            f"DETACH 30m≤{thr}×{conf} + REBUY@{rw}m both↑ TW≥NQ+{marg}"
                            f" after {wait}×5m (≥{wait * 5}m)"
                        ),
                        win30_thr=thr,
                        confirm_polls=conf,
                        reenter=True,
                        repair_mode="co_sync",
                        repair_min_polls_after=wait,
                        outperform_margin=marg,
                        repair_window_min=rw,
                        require_tw_pos=True,
                        require_nq_pos=True,
                        require_tw_gt_nq=True,
                        block_reenter_new_low=True,
                    )

    seen: set[str] = set()
    out: list[RiskGateSpec] = []
    for s in specs:
        if s.strategy_id in seen:
            continue
        seen.add(s.strategy_id)
        out.append(s)
    return out


def enrich_poll_30m(poll: pd.DataFrame, *, short_frames: dict[int, pd.DataFrame] | None = None) -> pd.DataFrame:
    """30m DETACH frame + ROC / gap-pulse dashboard + optional short-window rebuy cols."""
    out = poll.copy()
    out["spread_30m"] = out["spread_win"]
    # 5m bar ROC ≈ change vs prior poll (proxy when 1m unavailable on 60d panel)
    out["tw_bar_roc"] = out["tw_from_open"].diff()
    out["nq_bar_roc"] = out["nq_from_open"].diff()
    out["d_spread_30m"] = out["spread_30m"].diff()

    def _gap_pulse(x: float) -> str:
        if not math.isfinite(x):
            return "NA"
        if x <= -GAP_EPS:
            return "WORSEN"
        if x >= GAP_EPS:
            return "IMPROVE"
        return "FLAT"

    def _sync_pulse(tw_r: float, nq_r: float) -> str:
        if not (math.isfinite(tw_r) and math.isfinite(nq_r)):
            return "NA"
        if abs(tw_r) < 0.02 and abs(nq_r) < 0.02:
            return "QUIET"
        st = np.sign(tw_r)
        sn = np.sign(nq_r)
        if st == 0 or sn == 0:
            return "ONE_SIDE"
        if st == sn:
            return "FOLLOW"
        return "OPPOSE"

    out["gap_pulse"] = [_gap_pulse(float(x)) for x in out["d_spread_30m"]]
    out["sync_pulse"] = [
        _sync_pulse(float(a), float(b)) for a, b in zip(out["tw_bar_roc"], out["nq_bar_roc"])
    ]

    if short_frames:
        for wmin, sdf in short_frames.items():
            if sdf is None or "poll" not in sdf.columns:
                continue
            m = sdf.set_index("poll")[["tw_win", "nq_win", "spread_win"]].rename(
                columns={
                    "tw_win": f"tw_win_{wmin}",
                    "nq_win": f"nq_win_{wmin}",
                    "spread_win": f"spread_{wmin}",
                }
            )
            out = out.join(m, on="poll")

    out.attrs.update(dict(poll.attrs))
    i_min = int(out["tw_from_open"].idxmin())
    out.attrs["trough_poll_close"] = str(out.loc[i_min, "poll"])
    out.attrs["trough_idx"] = i_min
    return out


def _detach_mask(poll: pd.DataFrame, spec: RiskGateSpec) -> pd.Series:
    det = poll["spread_30m"] <= spec.win30_thr
    if spec.confirm_polls <= 1:
        return det.fillna(False)
    run = det.astype(int).groupby((~det).cumsum()).cumsum()
    return (run >= spec.confirm_polls).fillna(False)


def _rebuy_wins(row: pd.Series, spec: RiskGateSpec) -> tuple[float, float]:
    w = int(spec.repair_window_min or 30)
    if w in (5, 10) and f"tw_win_{w}" in row.index:
        return float(row[f"tw_win_{w}"]), float(row[f"nq_win_{w}"])
    return float(row["tw_win"]), float(row["nq_win"])


def _repair_ok(
    row: pd.Series,
    *,
    detach_spread_30: float,
    trough_spread_30: float,
    detach_tw: float,
    post_detach_tw_low: float,
    spec: RiskGateSpec,
    rebound_cushion: float = 0.25,
) -> bool:
    """Rebuy only after L2 clear (caller). co_sync uses repair_window_min (often 5/10m)."""
    if not spec.reenter or spec.repair_mode in ("none", ""):
        return False

    tw_w, nq_w = _rebuy_wins(row, spec)
    if not (math.isfinite(tw_w) and math.isfinite(nq_w)):
        return False

    tw_now = float(row["tw_from_open"])
    if spec.block_reenter_new_low:
        # Must bounce off post-detach trough (NOT require above exit price —
        # crash days often exit early then make lower lows before repair).
        if tw_now < post_detach_tw_low + rebound_cushion:
            return False

    if spec.repair_mode == "co_sync":
        if not (tw_w > 0 and nq_w > 0):
            return False
        if tw_w < nq_w + float(spec.outperform_margin):
            return False
        if float(row["spread_30m"]) < detach_spread_30 - 0.05:
            return False
        return True

    if spec.repair_mode == "legacy":
        ss = float(row.get("same_sign", float("nan")))
        if not (math.isfinite(ss) and ss >= spec.repair_same_sign_min):
            return False
        if spec.require_tw_pos and not (tw_w > 0):
            return False
        if spec.require_tw_gt_nq and not (tw_w > nq_w):
            return False
        delta = float(row["spread_30m"]) - trough_spread_30
        if delta < spec.repair_delta_30:
            return False
        if float(row["spread_30m"]) < detach_spread_30 + 0.05:
            return False
        return True

    return False


def simulate_day(poll: pd.DataFrame, spec: RiskGateSpec) -> dict[str, Any]:
    close_r = float(poll.attrs["close_from_open"])
    trough_r = float(poll.attrs["trough_from_open"])
    ptt = float(poll.attrs["peak_to_trough"])
    date_s = str(poll.attrs["date"])
    trough_idx = int(poll.attrs.get("trough_idx", poll["tw_from_open"].idxmin()))
    trough_poll = str(poll.attrs.get("trough_poll_close", poll.loc[trough_idx, "poll"]))

    det = _detach_mask(poll, spec)
    idxs = list(poll.index[det])
    base = {
        "date": date_s,
        "strategy_id": spec.strategy_id,
        "triggered": False,
        "detach_poll": None,
        "detach_tw_from_open": None,
        "spread_30m_at_detach": None,
        "reentered": False,
        "reenter_poll": None,
        "reenter_tw_from_open": None,
        "pnl_hold_close_pct": round(close_r, 4),
        "pnl_strat_pct": round(close_r, 4),
        "trough_from_open_pct": round(trough_r, 4),
        "trough_poll_close": trough_poll,
        "peak_to_trough_pct": round(ptt, 4),
        "exit_headroom_vs_trough_pct": None,
        "sell_near_trough": False,
        "reenter_before_trough": False,
        "saved_vs_close_pct": 0.0,
        "saved_vs_close_ntd": 0.0,
        "false_alarm_opp_cost_pct": 0.0,
        "false_alarm_opp_cost_ntd": 0.0,
        "is_event3": ptt >= EVENT_PTT,
    }
    if not idxs:
        return base

    i0 = idxs[0]
    row0 = poll.loc[i0]
    exit_r = float(row0["tw_from_open"])
    det_s30 = float(row0["spread_30m"])
    headroom = exit_r - trough_r
    sell_near = bool(headroom <= SELL_NEAR_TROUGH_BP)

    reentered = False
    reenter_poll = None
    reenter_tw = None
    reenter_before = False
    pnl_strat = exit_r

    if spec.reenter:
        after = poll.loc[i0:]
        trough_s30 = float(row0["spread_30m"])
        post_tw_low = exit_r
        positions = list(after.index)
        for k, j in enumerate(positions):
            row = poll.loc[j]
            trough_s30 = min(trough_s30, float(row["spread_30m"]))
            post_tw_low = min(post_tw_low, float(row["tw_from_open"]))
            if k < spec.repair_min_polls_after:
                continue
            if _repair_ok(
                row,
                detach_spread_30=det_s30,
                trough_spread_30=trough_s30,
                detach_tw=exit_r,
                post_detach_tw_low=post_tw_low,
                spec=spec,
            ):
                reentered = True
                reenter_poll = str(row["poll"])
                reenter_tw = float(row["tw_from_open"])
                reenter_before = bool(j < trough_idx)
                pnl_strat = exit_r + (close_r - reenter_tw)
                break

    loss_hold = max(0.0, -close_r)
    loss_strat = max(0.0, -pnl_strat)
    saved = loss_hold - loss_strat
    opp = max(0.0, close_r - exit_r) if (not reentered and close_r > exit_r) else 0.0

    return {
        **base,
        "triggered": True,
        "detach_poll": str(row0["poll"]),
        "detach_tw_from_open": round(exit_r, 4),
        "spread_30m_at_detach": round(det_s30, 4),
        "reentered": reentered,
        "reenter_poll": reenter_poll,
        "reenter_tw_from_open": None if reenter_tw is None else round(reenter_tw, 4),
        "pnl_strat_pct": round(pnl_strat, 4),
        "exit_headroom_vs_trough_pct": round(headroom, 4),
        "sell_near_trough": sell_near,
        "reenter_before_trough": reenter_before,
        "saved_vs_close_pct": round(saved, 4),
        "saved_vs_close_ntd": round(saved / 100.0 * NOTIONAL, 2),
        "false_alarm_opp_cost_pct": round(opp, 4),
        "false_alarm_opp_cost_ntd": round(opp / 100.0 * NOTIONAL, 2),
    }


def score_strategy(day_rows: list[dict[str, Any]], spec: RiskGateSpec) -> dict[str, Any]:
    if not day_rows:
        return {"strategy_id": spec.strategy_id, "n_days": 0}

    events = [r for r in day_rows if r["is_event3"]]
    nonev = [r for r in day_rows if not r["is_event3"]]
    trig = [r for r in day_rows if r["triggered"]]
    miss = [r for r in events if not r["triggered"]]
    event_trig = [r for r in events if r["triggered"]]

    sold_near = [r for r in event_trig if r["sell_near_trough"]]
    re_before = [r for r in event_trig if r.get("reentered") and r.get("reenter_before_trough")]
    headrooms = [float(r["exit_headroom_vs_trough_pct"]) for r in event_trig if r["exit_headroom_vs_trough_pct"] is not None]

    saved = sum(float(r["saved_vs_close_ntd"]) for r in day_rows)
    opp = sum(float(r["false_alarm_opp_cost_ntd"]) for r in day_rows)
    net = saved - opp
    n_trig_actions = sum(1 + (1 if r.get("reentered") else 0) for r in trig)
    fee = n_trig_actions * (FEE_RT_HAIRCUT_PCT / 100.0) * NOTIONAL  # crude: haircut% of notional per action

    recall = (len(event_trig) / len(events)) if events else float("nan")
    miss_rate = (len(miss) / len(events)) if events else float("nan")
    fpr = (sum(1 for r in nonev if r["triggered"]) / len(nonev)) if nonev else float("nan")
    sell_near_rate = (len(sold_near) / len(event_trig)) if event_trig else 0.0
    re_before_rate = (len(re_before) / len(event_trig)) if event_trig else 0.0
    med_headroom = float(np.median(headrooms)) if headrooms else float("nan")

    re_rate = (
        sum(1 for r in trig if r.get("reentered")) / len(trig) if trig else 0.0
    )

    return {
        "strategy_id": spec.strategy_id,
        "name": spec.name,
        "win30_thr": spec.win30_thr,
        "confirm_polls": spec.confirm_polls,
        "reenter": spec.reenter,
        "repair_mode": spec.repair_mode,
        "outperform_margin": spec.outperform_margin if spec.repair_mode == "co_sync" else None,
        "repair_window_min": spec.repair_window_min if spec.reenter else None,
        "repair_same_sign_min": spec.repair_same_sign_min if spec.repair_mode == "legacy" else None,
        "repair_delta_30": spec.repair_delta_30 if spec.repair_mode == "legacy" else None,
        "repair_min_polls_after": spec.repair_min_polls_after if spec.reenter else None,
        "n_days": len(day_rows),
        "n_events3": len(events),
        "n_triggers": len(trig),
        "trigger_rate": round(len(trig) / len(day_rows), 4),
        "reenter_rate_on_triggers": round(re_rate, 4),
        "event_recall": round(recall, 4) if math.isfinite(recall) else None,
        "miss_rate": round(miss_rate, 4) if math.isfinite(miss_rate) else None,
        "false_trigger_rate_on_nonevent": round(fpr, 4) if math.isfinite(fpr) else None,
        "sell_near_trough_rate_on_events": round(sell_near_rate, 4),
        "reenter_before_trough_rate_on_events": round(re_before_rate, 4),
        "median_exit_headroom_vs_trough_pct": round(med_headroom, 4) if math.isfinite(med_headroom) else None,
        "net_edge_ntd": round(net, 2),
        "net_edge_after_fee_ntd": round(net - fee, 2),
        "saved_vs_close_ntd": round(saved, 2),
        "opp_cost_ntd": round(opp, 2),
        "missed_event_dates": [r["date"] for r in miss],
    }


def pick_champion(score_df: pd.DataFrame) -> tuple[pd.Series, str]:
    """L2 priority: high recall → low sell-at-trough → low pre-trough reenter → net_edge.
    Prefer flatten-only when otherwise tied on recall/timing."""
    df = score_df.copy()
    df["_rec"] = df["event_recall"].fillna(-1)
    df["_near"] = df["sell_near_trough_rate_on_events"].fillna(1)
    df["_early"] = df["reenter_before_trough_rate_on_events"].fillna(1)
    df["_flat"] = (~df["reenter"].fillna(False)).astype(int)
    df["_edge"] = df["net_edge_ntd"].fillna(-1e18)
    df = df.sort_values(
        by=["_rec", "_near", "_early", "_flat", "_edge"],
        ascending=[False, True, True, False, False],
    )
    return df.iloc[0], "recall_then_timing_then_flatten_then_edge"


def pick_co_sync_rebuy(
    score_df: pd.DataFrame,
    *,
    champ_thr: float,
    champ_confirm: int,
    day_sims: dict[str, dict[str, dict[str, Any]]] | None = None,
) -> pd.Series | None:
    """Prefer ≥60m cool + soft δ; favor 7/14 rebuy in 11:25–12:00 when available."""
    m = score_df[
        (score_df["repair_mode"] == "co_sync")
        & (score_df["reenter"] == True)  # noqa: E712
        & (score_df["win30_thr"] == champ_thr)
        & (score_df["confirm_polls"] == champ_confirm)
        & (score_df["repair_min_polls_after"].fillna(0) >= 12)
    ].copy()
    if m.empty:
        m = score_df[
            (score_df["repair_mode"] == "co_sync")
            & (score_df["reenter"] == True)  # noqa: E712
            & (score_df["repair_min_polls_after"].fillna(0) >= 12)
        ].copy()
    if m.empty:
        return None

    def _hit_714(sid: str) -> int:
        if not day_sims:
            return 0
        row = (day_sims.get(sid) or {}).get("2026-07-14") or {}
        p = row.get("reenter_poll")
        if not p:
            return 0
        return 1 if TARGET_REBUY_LO <= str(p) <= TARGET_REBUY_HI else 0

    m["_hit714"] = [_hit_714(str(s)) for s in m["strategy_id"]]
    m["_early"] = m["reenter_before_trough_rate_on_events"].fillna(1)
    m["_edge"] = m["net_edge_ntd"].fillna(-1e18)
    m["_wait"] = m["repair_min_polls_after"].fillna(0)
    if "outperform_margin" in m.columns:
        m["_marg"] = m["outperform_margin"].fillna(0)
    else:
        m["_marg"] = 0.0
    m = m.sort_values(
        by=["_hit714", "_early", "_edge", "_wait", "_marg"],
        ascending=[False, True, False, False, True],
    )
    return m.iloc[0]


def run_study(
    *,
    date_end: str | None = None,
    out_dir: Path | None = None,
) -> dict[str, Any]:
    end_d = date.fromisoformat(date_end) if date_end else date.today()
    tw = fetch_yahoo_5m_panel("^TWII", end_d)
    nq = fetch_yahoo_5m_panel("NQ=F", end_d)
    if tw.empty or nq.empty:
        raise RuntimeError("Yahoo 5m panel empty for ^TWII or NQ=F")

    local = tw.copy()
    if local.index.tz is None:
        local.index = local.index.tz_localize(TZ_TW)
    else:
        local.index = local.index.tz_convert(TZ_TW)
    tw_dates = sorted(
        {
            ts.strftime("%Y-%m-%d")
            for ts in local.index
            if TW_OPEN <= ts.strftime("%H:%M") <= TW_CLOSE
        }
    )
    tw_dates = [d for d in tw_dates if len(slice_us_during_tw(nq, d)) >= 3]
    specs = registered_grid()

    # build day frames once (30m DETACH + 5/10m rebuy + ROC pulse)
    day_polls: dict[str, pd.DataFrame] = {}
    for d in tw_dates:
        pf = build_poll_frame_5m(d, tw, nq, window_min=LOOKBACK_MIN)
        if pf is None or len(pf) < N_BARS + 1:
            continue
        shorts: dict[int, pd.DataFrame] = {}
        for wmin in (5, 10):
            sf = build_poll_frame_5m(d, tw, nq, window_min=wmin)
            if sf is not None:
                shorts[wmin] = sf
        day_polls[d] = enrich_poll_30m(pf, short_frames=shorts)

    dates = sorted(day_polls.keys())
    if not dates:
        raise RuntimeError("No 5m session frames built")

    cut = dates[max(1, int(len(dates) * 2 / 3))]
    all_scores = []
    day_sims: dict[str, dict[str, dict[str, Any]]] = {}

    for spec in specs:
        rows = []
        day_sims[spec.strategy_id] = {}
        for d in dates:
            out = simulate_day(day_polls[d], spec)
            rows.append(out)
            day_sims[spec.strategy_id][d] = out
        sc = score_strategy(rows, spec)
        # IS/OOS
        is_rows = [r for r in rows if r["date"] < cut]
        oos_rows = [r for r in rows if r["date"] >= cut]
        sc["is_net_edge_ntd"] = score_strategy(is_rows, spec)["net_edge_ntd"] if is_rows else None
        sc["oos_net_edge_ntd"] = score_strategy(oos_rows, spec)["net_edge_ntd"] if oos_rows else None
        sc["is_event_recall"] = score_strategy(is_rows, spec)["event_recall"] if is_rows else None
        sc["oos_event_recall"] = score_strategy(oos_rows, spec)["event_recall"] if oos_rows else None
        all_scores.append(sc)

    score_df = pd.DataFrame(all_scores)
    best, pick_rule = pick_champion(score_df)
    champ_id = str(best["strategy_id"])
    champ_spec = next(s for s in specs if s.strategy_id == champ_id)
    champ_thr = float(best.get("win30_thr") or -0.6)
    champ_conf = int(best.get("confirm_polls") or 2)
    rebuy_s = pick_co_sync_rebuy(
        score_df,
        champ_thr=champ_thr,
        champ_confirm=champ_conf,
        day_sims=day_sims,
    )
    rebuy_row = None if rebuy_s is None else rebuy_s.to_dict()
    rebuy_spec = (
        None
        if rebuy_row is None
        else next((s for s in specs if s.strategy_id == rebuy_row["strategy_id"]), None)
    )

    # high-recall band (≥0.9 if any; else ≥0.8)
    for floor in (0.9, 0.8, 0.7):
        band = score_df[score_df["event_recall"].fillna(0) >= floor]
        if len(band):
            high_recall_floor = floor
            high_recall_top = band.sort_values(
                by=["sell_near_trough_rate_on_events", "reenter_before_trough_rate_on_events", "net_edge_ntd"],
                ascending=[True, True, False],
            ).head(10)
            break
    else:
        high_recall_floor = None
        high_recall_top = score_df.head(0)

    co_sync_top = (
        score_df[score_df["repair_mode"] == "co_sync"]
        .sort_values(
            by=["reenter_before_trough_rate_on_events", "net_edge_ntd"],
            ascending=[True, False],
        )
        .head(12)
    )

    def _timeline(pf: pd.DataFrame, spec: RiskGateSpec) -> list[dict[str, Any]]:
        det = _detach_mask(pf, spec)
        rows_tl: list[dict[str, Any]] = []
        latched = False
        trough_s30 = None
        det_s30 = None
        det_tw = None
        post_tw_low = None
        polls_since = 0
        for i, row in pf.iterrows():
            st = "SYNC"
            if bool(det.loc[i]):
                st = "DETACH"
                if not latched:
                    latched = True
                    det_s30 = float(row["spread_30m"])
                    det_tw = float(row["tw_from_open"])
                    post_tw_low = det_tw
                    trough_s30 = det_s30
                    polls_since = 0
                else:
                    polls_since += 1
                    trough_s30 = min(trough_s30, float(row["spread_30m"]))
                    post_tw_low = min(post_tw_low, float(row["tw_from_open"]))
            elif latched:
                polls_since += 1
                trough_s30 = min(trough_s30, float(row["spread_30m"]))
                post_tw_low = min(post_tw_low, float(row["tw_from_open"]))
                if (
                    spec.reenter
                    and polls_since >= spec.repair_min_polls_after
                    and _repair_ok(
                        row,
                        detach_spread_30=det_s30,
                        trough_spread_30=trough_s30,
                        detach_tw=det_tw,
                        post_detach_tw_low=post_tw_low,
                        spec=spec,
                    )
                ):
                    st = "REPAIRING"
            tw_r, nq_r = _rebuy_wins(row, spec) if spec.reenter else (float(row["tw_win"]), float(row["nq_win"]))
            d_s = float(row["d_spread_30m"]) if math.isfinite(float(row.get("d_spread_30m", float("nan")))) else float("nan")
            rows_tl.append(
                {
                    "poll": str(row["poll"]),
                    "state": st,
                    "tw_from_open": round(float(row["tw_from_open"]), 4),
                    "nq_from_open": round(float(row["nq_from_open"]), 4),
                    "spread_30m": round(float(row["spread_30m"]), 4),
                    "d_spread_30m": None if not math.isfinite(d_s) else round(d_s, 4),
                    "gap_pulse": str(row.get("gap_pulse", "NA")),
                    "tw_bar_roc": None
                    if not math.isfinite(float(row.get("tw_bar_roc", float("nan"))))
                    else round(float(row["tw_bar_roc"]), 4),
                    "nq_bar_roc": None
                    if not math.isfinite(float(row.get("nq_bar_roc", float("nan"))))
                    else round(float(row["nq_bar_roc"]), 4),
                    "sync_pulse": str(row.get("sync_pulse", "NA")),
                    "tw_win_rb": round(tw_r, 4) if math.isfinite(tw_r) else None,
                    "nq_win_rb": round(nq_r, 4) if math.isfinite(nq_r) else None,
                    "outperform_rb": round(tw_r - nq_r, 4)
                    if math.isfinite(tw_r) and math.isfinite(nq_r)
                    else None,
                }
            )
        return rows_tl

    # cases: event days + 7/14
    event_dates = sorted({d for d, pf in day_polls.items() if float(pf.attrs["peak_to_trough"]) >= EVENT_PTT})
    focus = sorted(set(event_dates) | {"2026-07-14", "2026-07-07"})

    cases = []
    for d in focus:
        if d not in day_polls:
            continue
        pf = day_polls[d]
        row_c = simulate_day(pf, champ_spec)
        row_r = simulate_day(pf, rebuy_spec) if rebuy_spec is not None else None
        comps = []
        for sid in filter(
            None,
            [
                champ_id,
                None if rebuy_row is None else rebuy_row.get("strategy_id"),
                "rg30_det-0.6_c2",
            ],
        ):
            sp = next((s for s in specs if s.strategy_id == sid), None)
            if sp is None:
                continue
            comps.append(simulate_day(pf, sp))
        cases.append(
            {
                "date": d,
                "peak_to_trough": round(float(pf.attrs["peak_to_trough"]), 4),
                "close_from_open": round(float(pf.attrs["close_from_open"]), 4),
                "trough_from_open": round(float(pf.attrs["trough_from_open"]), 4),
                "trough_poll_close": pf.attrs.get("trough_poll_close"),
                "champion": row_c,
                "rebuy": row_r,
                "comparators": comps,
                "timeline": _timeline(pf, champ_spec) if d in ("2026-07-14", "2026-07-07") else [],
                "timeline_rebuy": _timeline(pf, rebuy_spec)
                if rebuy_spec is not None and d in ("2026-07-14", "2026-07-07")
                else [],
            }
        )

    # figures
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        fig_dir = out_dir / "figs"
        fig_dir.mkdir(exist_ok=True)
        fig, ax = plt.subplots(figsize=(7.2, 4.4))
        ax.scatter(
            score_df["event_recall"],
            score_df["sell_near_trough_rate_on_events"],
            c=score_df["net_edge_ntd"],
            cmap="RdYlGn",
            s=28,
            alpha=0.85,
        )
        ax.scatter(
            [best["event_recall"]],
            [best["sell_near_trough_rate_on_events"]],
            s=120,
            facecolors="none",
            edgecolors="k",
            lw=2,
            label="L2 champ",
        )
        ax.set_xlabel("event recall (≥3% PTT)")
        ax.set_ylabel("sell-near-trough rate (on triggered events)")
        ax.set_title("30m rolling gate · recall vs sell-at-low")
        ax.legend(loc="best")
        fig.tight_layout()
        fig.savefig(fig_dir / "recall_vs_sell_near.png", dpi=120)
        plt.close(fig)

        if "2026-07-14" in day_polls:
            pf = day_polls["2026-07-14"]
            fig, ax = plt.subplots(figsize=(8.5, 4.2))
            x = list(range(len(pf)))
            ax.plot(x, pf["tw_from_open"], label="TW from open", color="#2980b9")
            ax.plot(x, pf["nq_from_open"], label="NQ from open", color="#8e44ad")
            ax.plot(x, pf["spread_30m"], label="spread_30m (6×5m)", color="#c0392b", ls="--")
            row_c = next(c["champion"] for c in cases if c["date"] == "2026-07-14")
            row_r = next(c.get("rebuy") for c in cases if c["date"] == "2026-07-14")
            polls = list(pf["poll"])
            if row_c.get("detach_poll") in polls:
                i = polls.index(row_c["detach_poll"])
                ax.axvline(i, color="#e67e22", ls=":", lw=1.8, label=f"DETACH {row_c['detach_poll']}")
            if row_r and row_r.get("reenter_poll") in polls:
                i = polls.index(row_r["reenter_poll"])
                ax.axvline(i, color="#3dd6c6", ls=":", lw=1.8, label=f"REBUY {row_r['reenter_poll']}")
            tp = pf.attrs.get("trough_poll_close")
            if tp in polls:
                ax.axvline(polls.index(tp), color="#7f8c8d", ls="--", lw=1.2, label=f"trough~{tp}")
            ax.set_xticks(x[::4])
            ax.set_xticklabels([polls[i] for i in x[::4]], rotation=45, ha="right", fontsize=8)
            ax.legend(loc="best", fontsize=8)
            ax.set_title("2026-07-14 · 30m gate + co_sync rebuy")
            fig.tight_layout()
            fig.savefig(fig_dir / "case_20260714.png", dpi=120)
            plt.close(fig)

    n_events = sum(1 for pf in day_polls.values() if float(pf.attrs["peak_to_trough"]) >= EVENT_PTT)
    meta = {
        "window": f"{dates[0]} → {dates[-1]}",
        "n_days": len(dates),
        "n_events3": n_events,
        "lookback": f"{LOOKBACK_MIN}m = {N_BARS}×5m bars",
        "detach_anchor": "rolling spread_30m only (NOT open cum)",
        "rebuy_standard": (
            "after L2 clear · cooldown ≥60m · both↑ on 5/10m window · TW≥NQ+δ(≤0.15) · no new TW low"
        ),
        "presentation": (
            "pulse dashboard: tw/nq_bar_roc + sync_pulse(FOLLOW/OPPOSE) + "
            "d_spread_30m + gap_pulse(IMPROVE/WORSEN/FLAT)"
        ),
        "sources": "^TWII 5m · NQ=F 5m Yahoo",
        "notional_ntd": NOTIONAL,
        "registered_grid_size": len(specs),
        "pick_rule": pick_rule,
        "sell_near_trough_threshold_pct": SELL_NEAR_TROUGH_BP,
        "is_oos_cut": cut,
        "is": {
            "start": dates[0],
            "end": dates[dates.index(cut) - 1] if cut in dates and dates.index(cut) > 0 else dates[0],
            "n": sum(1 for d in dates if d < cut),
        },
        "oos": {"start": cut, "end": dates[-1], "n": sum(1 for d in dates if d >= cut)},
        "user_priority": "miss_sell > sell_at_trough > buy_before_trough > false_alarm",
        "graduation": "REJECT Order-layer auto · Research risk-control standard only",
    }

    top = score_df.sort_values(
        by=["event_recall", "sell_near_trough_rate_on_events", "reenter_before_trough_rate_on_events", "net_edge_ntd"],
        ascending=[False, True, True, False],
    ).head(15)

    spot714 = next((c for c in cases if c["date"] == "2026-07-14"), None)
    spot707 = next((c for c in cases if c["date"] == "2026-07-07"), None)

    wait_m = None if not rebuy_row else rebuy_row.get("repair_min_polls_after")
    marg_m = None if not rebuy_row else rebuy_row.get("outperform_margin")
    rw_m = None if not rebuy_row else rebuy_row.get("repair_window_min")
    standards = {
        "L1_watch": {
            "rule": f"spread_30m ≤ {champ_thr}（單拍）",
            "action": "停加碼、準備出清",
        },
        "L2_gate_on": {
            "rule": str(best.get("name")),
            "strategy_id": champ_id,
            "action": "出清／大幅減碼（近30分相對 NQ 脫鉤確認）",
            "note": "主軌可不接回；State 鎖存≠每一拍都在脫鉤",
            "metrics": {
                "event_recall": best.get("event_recall"),
                "miss_rate": best.get("miss_rate"),
                "sell_near_trough_rate": best.get("sell_near_trough_rate_on_events"),
                "reenter_before_trough_rate": best.get("reenter_before_trough_rate_on_events"),
                "median_exit_headroom_pct": best.get("median_exit_headroom_vs_trough_pct"),
                "fpr_nonevent": best.get("false_trigger_rate_on_nonevent"),
                "net_edge_ntd": best.get("net_edge_ntd"),
            },
        },
        "L2R_optional_rebuy": {
            "enabled": rebuy_row is not None,
            "strategy_id": None if not rebuy_row else rebuy_row.get("strategy_id"),
            "rule": None if not rebuy_row else rebuy_row.get("name"),
            "definition": (
                f"當天已 L2 出清 · 冷卻 ≥{int(wait_m) * 5 if wait_m else 60} 分（{wait_m}×5m）· "
                f"近 {rw_m}m：tw>0 且 nq>0 · tw ≥ nq + {marg_m} · "
                f"自出清後谷底反彈 ≥0.25%"
                if rebuy_row
                else None
            ),
            "action": "分批試接；接回窗用短窗（非30m）才能捕捉谷後彈",
            "metrics": None
            if not rebuy_row
            else {
                "event_recall": rebuy_row.get("event_recall"),
                "reenter_before_trough_rate": rebuy_row.get("reenter_before_trough_rate_on_events"),
                "sell_near_trough_rate": rebuy_row.get("sell_near_trough_rate_on_events"),
                "reenter_rate_on_triggers": rebuy_row.get("reenter_rate_on_triggers"),
                "net_edge_ntd": rebuy_row.get("net_edge_ntd"),
                "fpr_nonevent": rebuy_row.get("false_trigger_rate_on_nonevent"),
                "cooldown_polls": wait_m,
                "cooldown_minutes": None if wait_m is None else int(wait_m) * 5,
                "outperform_margin": marg_m,
                "repair_window_min": rw_m,
                "hit_714_1125_1200": bool(
                    rebuy_row
                    and (day_sims.get(str(rebuy_row.get("strategy_id"))) or {})
                    .get("2026-07-14", {})
                    .get("reenter_poll")
                    and TARGET_REBUY_LO
                    <= str(
                        (day_sims.get(str(rebuy_row.get("strategy_id"))) or {})
                        .get("2026-07-14", {})
                        .get("reenter_poll")
                    )
                    <= TARGET_REBUY_HI
                ),
            },
        },
        "how_to_read_pulse": {
            "simple": [
                "① 第一個 DETACH → 開始風控",
                "② gap_pulse：相對上一拍，傷口 IMPROVE／WORSEN／FLAT",
                "③ sync_pulse + tw/nq_bar_roc：這一棒是 FOLLOW 還是 OPPOSE（方向）",
                "④ 第一個 REPAIRING（接回條件）→ 可試接；交錯=修復不穩",
            ],
            "note": "Yahoo 60d 面板用 5m bar ROC 代理 1m 變化率；真 1m 儀表需另接 intraday 源",
        },
        "reenter_rule": {
            "enabled": rebuy_row is not None,
            "conditions": None
            if not rebuy_row
            else (
                f"co_sync `{rebuy_row.get('strategy_id')}`: clear · wait≥{wait_m}· "
                f"@{rw_m}m both↑ · TW≥NQ+{marg_m} · block new low"
            ),
            "action": "分批試接；主軌可只出清不接",
        },
        "do_not": [
            "冷卻不到 1 小時就接回",
            "用 30m 雙漲硬套谷後彈（7/14 的 11:25 在 30m 仍雙小跌）",
            "把長時間 DETACH 當成每分鐘都在脫鉤（多半是鎖存）",
            "Order 自動全平",
        ],
    }

    payload = {
        "meta": meta,
        "registered_grid": {
            "size": len(specs),
            "win30_grid": list(WIN30_GRID),
            "confirm_grid": list(CONFIRM_GRID),
            "focus_detach": [list(x) for x in FOCUS_DETACH],
            "cooldown_polls": list(COOLDOWN_POLLS),
            "cooldown_minutes": [w * 5 for w in COOLDOWN_POLLS],
            "outperform_margin": list(OUTPERFORM_MARGIN),
            "rebuy_window_min": list(REBUY_WINDOW_MIN),
            "repair_modes": ["none", "co_sync"],
            "target_rebuy_window_714": f"{TARGET_REBUY_LO}–{TARGET_REBUY_HI}",
        },
        "best_strategy": best.to_dict(),
        "best_rebuy_co_sync": rebuy_row,
        "top_strategies": top.to_dict(orient="records"),
        "top_co_sync_rebuy": co_sync_top.to_dict(orient="records"),
        "high_recall_band": {
            "floor": high_recall_floor,
            "top": high_recall_top.to_dict(orient="records") if len(high_recall_top) else [],
        },
        "risk_control_standard": standards,
        "spotlight_20260714": {
            "detach_poll": (spot714 or {}).get("champion", {}).get("detach_poll"),
            "rebuy_poll": ((spot714 or {}).get("rebuy") or {}).get("reenter_poll"),
            "trough_poll_close": (spot714 or {}).get("trough_poll_close"),
            "exit_headroom": (spot714 or {}).get("champion", {}).get("exit_headroom_vs_trough_pct"),
            "sell_near_trough": (spot714 or {}).get("champion", {}).get("sell_near_trough"),
            "rebuy_before_trough": ((spot714 or {}).get("rebuy") or {}).get("reenter_before_trough"),
            "champion": (spot714 or {}).get("champion"),
            "rebuy": (spot714 or {}).get("rebuy"),
            "timeline": (spot714 or {}).get("timeline"),
            "timeline_rebuy": (spot714 or {}).get("timeline_rebuy"),
        },
        "spotlight_20260707": {
            "detach_poll": (spot707 or {}).get("champion", {}).get("detach_poll"),
            "rebuy_poll": ((spot707 or {}).get("rebuy") or {}).get("reenter_poll"),
            "trough_poll_close": (spot707 or {}).get("trough_poll_close"),
            "champion": (spot707 or {}).get("champion"),
            "rebuy": (spot707 or {}).get("rebuy"),
            "timeline": (spot707 or {}).get("timeline"),
            "timeline_rebuy": (spot707 or {}).get("timeline_rebuy"),
        },
        "cases": cases,
    }

    md = render_markdown(payload)
    if out_dir is not None:
        (out_dir / "summary.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        (out_dir / "README.md").write_text(md, encoding="utf-8")

    return {"payload": payload, "markdown": md, "score_df": score_df}


def render_markdown(payload: dict[str, Any]) -> str:
    m = payload["meta"]
    b = payload["best_strategy"]
    std = payload["risk_control_standard"]
    spot = payload.get("spotlight_20260714") or {}
    spot707 = payload.get("spotlight_20260707") or {}
    lines = [
        "# TW↔NQ rolling 30m (6×5m) risk gate · risk-control standard",
        "",
        f"- Window: `{m['window']}` · days={m['n_days']} · events≥3%={m['n_events3']}",
        f"- DETACH anchor: **{m['detach_anchor']}** · lookback **{m['lookback']}**",
        f"- REBUY: **{m.get('rebuy_standard')}**",
        f"- Sources: {m['sources']} · Notional {m['notional_ntd']:,} NTD",
        f"- Grid size: **{m['registered_grid_size']}** · pick_rule=`{m['pick_rule']}`",
        f"- Priority: `{m['user_priority']}`",
        f"- IS/OOS cut `{m['is_oos_cut']}` · {m['graduation']}",
        "",
        "## Risk-control standard（操作漏斗）",
        "",
        f"**L1 WATCH:** {std['L1_watch']['rule']} → {std['L1_watch']['action']}",
        "",
        f"**L2 GATE ON（主出清）:** `{std['L2_gate_on']['strategy_id']}`",
        f"- {std['L2_gate_on']['rule']}",
        f"- Action: {std['L2_gate_on']['action']}",
        f"- Note: {std['L2_gate_on'].get('note')}",
        f"- Metrics: recall={std['L2_gate_on']['metrics'].get('event_recall')} · "
        f"miss={std['L2_gate_on']['metrics'].get('miss_rate')} · "
        f"sell@trough={std['L2_gate_on']['metrics'].get('sell_near_trough_rate')} · "
        f"buy_before_trough={std['L2_gate_on']['metrics'].get('reenter_before_trough_rate')} · "
        f"med headroom={std['L2_gate_on']['metrics'].get('median_exit_headroom_pct')}% · "
        f"FPR={std['L2_gate_on']['metrics'].get('fpr_nonevent')} · "
        f"net_edge={std['L2_gate_on']['metrics'].get('net_edge_ntd')} NTD",
        "",
    ]
    l2r = std.get("L2R_optional_rebuy") or {}
    if l2r.get("enabled"):
        met = l2r.get("metrics") or {}
        lines += [
            f"**L2R 接回（co_sync）:** `{l2r.get('strategy_id')}`",
            f"- {l2r.get('definition')}",
            f"- 7/14 目標窗 11:25–12:00 hit={met.get('hit_714_1125_1200')}",
            f"- Metrics: buy_pre={met.get('reenter_before_trough_rate')} · "
            f"edge={met.get('net_edge_ntd')} · cool={met.get('cooldown_minutes')}m · "
            f"δ={met.get('outperform_margin')} · rebuy_win={met.get('repair_window_min')}m",
            "",
        ]
    howto = std.get("how_to_read_pulse") or {}
    if howto.get("simple"):
        lines += ["**怎麼讀（簡單）:**", ""]
        for s in howto["simple"]:
            lines.append(f"- {s}")
        if howto.get("note"):
            lines.append(f"- _{howto['note']}_")
        lines.append("")
    lines += [
        f"**Reenter:** {std['reenter_rule']['conditions']} → {std['reenter_rule']['action']}",
        "",
        "**不要做：**",
    ]
    for x in std["do_not"]:
        lines.append(f"- {x}")

    lines += [
        "",
        "## Verdict",
        "",
        f"**L2 Champion:** `{b.get('strategy_id')}` — {b.get('name')}",
        f"- Event recall: **{b.get('event_recall')}** (miss {b.get('miss_rate')})",
        f"- Sell-near-trough rate: **{b.get('sell_near_trough_rate_on_events')}**",
        f"- Median exit headroom vs trough: **{b.get('median_exit_headroom_vs_trough_pct')}%**",
        f"- FPR non-event: {b.get('false_trigger_rate_on_nonevent')} · net_edge **{b.get('net_edge_ntd')}** NTD",
        f"- Missed event dates: {b.get('missed_event_dates')}",
        "",
    ]
    br = payload.get("best_rebuy_co_sync") or {}
    if br:
        lines += [
            f"**L2R co_sync:** `{br.get('strategy_id')}` — {br.get('name')}",
            f"- buy_pre_trough={br.get('reenter_before_trough_rate_on_events')} · "
            f"net_edge={br.get('net_edge_ntd')} · wait={br.get('repair_min_polls_after')} · "
            f"δ={br.get('outperform_margin')}",
            "",
        ]
    lines += [
        "解讀：出清用 rolling 30m 相對 NQ；接回必須當天已出清、冷卻後、前六區間與 NQ **同向漲** 且台股漲幅 **≥ NQ+δ**。"
        "Research only。",
        "",
        "## Spotlight · 2026-07-14",
        "",
        f"- DETACH: **{spot.get('detach_poll')}** · trough~**{spot.get('trough_poll_close')}** · "
        f"REBUY: **{spot.get('rebuy_poll')}**",
        f"- exit headroom: {spot.get('exit_headroom')}% · sell_near={spot.get('sell_near_trough')} · "
        f"rebuy_before_trough={spot.get('rebuy_before_trough')}",
        "",
        "## Spotlight · 2026-07-07 (late kill)",
        "",
        f"- DETACH: **{spot707.get('detach_poll')}** · trough~**{spot707.get('trough_poll_close')}** · "
        f"REBUY: **{spot707.get('rebuy_poll')}**",
        "",
        "## Top co_sync rebuy",
        "",
        "| ID | Buy@pre-trough | Reenter rate | Net edge | Wait | δ |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for r in payload.get("top_co_sync_rebuy") or []:
        lines.append(
            f"| `{r.get('strategy_id')}` | {r.get('reenter_before_trough_rate_on_events')} | "
            f"{r.get('reenter_rate_on_triggers')} | {r.get('net_edge_ntd')} | "
            f"{r.get('repair_min_polls_after')} | {r.get('outperform_margin')} |"
        )

    lines += [
        "",
        "## Top by recall → timing → edge",
        "",
        "| ID | Recall | Miss | Sell@trough | Buy@pre-trough | Headroom% | FPR | Net edge |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in payload.get("top_strategies") or []:
        lines.append(
            f"| `{r.get('strategy_id')}` | {r.get('event_recall')} | {r.get('miss_rate')} | "
            f"{r.get('sell_near_trough_rate_on_events')} | {r.get('reenter_before_trough_rate_on_events')} | "
            f"{r.get('median_exit_headroom_vs_trough_pct')} | {r.get('false_trigger_rate_on_nonevent')} | "
            f"{r.get('net_edge_ntd')} |"
        )

    hr = payload.get("high_recall_band") or {}
    lines += ["", f"## High-recall band (floor={hr.get('floor')})", ""]
    if not hr.get("top"):
        lines.append("_No strategy reached the recall floor band._")
    else:
        lines.append("| ID | Recall | Sell@trough | Buy@pre-trough | Net edge |")
        lines.append("|---|---:|---:|---:|---:|")
        for r in hr["top"]:
            lines.append(
                f"| `{r.get('strategy_id')}` | {r.get('event_recall')} | "
                f"{r.get('sell_near_trough_rate_on_events')} | {r.get('reenter_before_trough_rate_on_events')} | "
                f"{r.get('net_edge_ntd')} |"
            )

    lines += ["", "## Case studies", ""]
    for c in payload.get("cases") or []:
        ch = c.get("champion") or {}
        rb = c.get("rebuy") or {}
        lines += [
            f"### {c['date']} · PTT {c.get('peak_to_trough')}% · close {c.get('close_from_open')}% · "
            f"trough {c.get('trough_from_open')}% (~{c.get('trough_poll_close')})",
            "",
            f"- L2 DETACH **{ch.get('detach_poll')}** @ TW {ch.get('detach_tw_from_open')}% · "
            f"spread_30m={ch.get('spread_30m_at_detach')} · headroom={ch.get('exit_headroom_vs_trough_pct')}% · "
            f"sell_near={ch.get('sell_near_trough')}",
            f"- L2R REBUY **{rb.get('reenter_poll')}** @ TW {rb.get('reenter_tw_from_open')}% · "
            f"before_trough={rb.get('reenter_before_trough')}",
            "",
        ]
        tl = c.get("timeline_rebuy") or c.get("timeline") or []
        if tl:
            lines += [
                "| Poll | State | gap | sync | ΔTW棒 | ΔNQ棒 | Δs30 | rb_TW | rb_NQ | rb− |",
                "|---|---|---|---|---:|---:|---:|---:|---:|---:|",
            ]
            for row in tl:
                lines.append(
                    f"| {row['poll']} | **{row['state']}** | {row.get('gap_pulse')} | {row.get('sync_pulse')} | "
                    f"{row.get('tw_bar_roc')} | {row.get('nq_bar_roc')} | {row.get('d_spread_30m')} | "
                    f"{row.get('tw_win_rb')} | {row.get('nq_win_rb')} | {row.get('outperform_rb')} |"
                )
            lines.append("")

    lines += [
        "## Method",
        "",
        f"- DETACH: rolling `spread_30m = TW%−NQ%` over last **{N_BARS}** five-minute bars ≤ thr × confirm.",
        "- REBUY (co_sync): day already cleared · cooldown N×5m · tw_win>0 ∧ nq_win>0 · "
        "tw_win ≥ nq_win + δ · no new TW low vs detach.",
        f"- Sell-near-trough: exit_headroom ≤ **{SELL_NEAR_TROUGH_BP}%**.",
        "- Research layer（研究層）only — not Order layer（下單層）auto sell-all.",
        "",
    ]
    return "\n".join(lines)


# ── Fixed live research standard (calendar replay) ─────────────────────────
def champion_l2_spec() -> RiskGateSpec:
    return RiskGateSpec(
        strategy_id="rg30_det-0.6_c2",
        name="DETACH spread_30m≤-0.6×2 · no reenter",
        win30_thr=-0.6,
        confirm_polls=2,
        reenter=False,
        repair_mode="none",
    )


def champion_l2r_spec() -> RiskGateSpec:
    return RiskGateSpec(
        strategy_id="rg30_det-0.6_c2_co_w16_m0.1_r5",
        name="DETACH 30m≤-0.6×2 + REBUY@5m both↑ TW≥NQ+0.1 after 16×5m",
        win30_thr=-0.6,
        confirm_polls=2,
        reenter=True,
        repair_mode="co_sync",
        repair_min_polls_after=16,
        outperform_margin=0.1,
        repair_window_min=5,
        require_tw_pos=True,
        require_nq_pos=True,
        require_tw_gt_nq=True,
        block_reenter_new_low=True,
    )


def _confirm_label(row: dict[str, Any]) -> str:
    """Human label: does gate action align with day outcome?"""
    ev = bool(row.get("is_event3"))
    trig = bool(row.get("triggered"))
    if trig and ev:
        return "OK_EVENT"  # triggered on ≥3% day
    if trig and not ev:
        return "FALSE_ALARM"
    if (not trig) and ev:
        return "MISS_EVENT"
    return "QUIET_OK"


def run_daily_gate_calendar(
    *,
    date_end: str | None = None,
    out_dir: Path | None = None,
) -> dict[str, Any]:
    """Replay L2 / L2R on every available TW session (Yahoo 5m ≈59d, not full 90d)."""
    end_d = date.fromisoformat(date_end) if date_end else date.today()
    tw = fetch_yahoo_5m_panel("^TWII", end_d)
    nq = fetch_yahoo_5m_panel("NQ=F", end_d)
    if tw.empty or nq.empty:
        raise RuntimeError("Yahoo 5m panel empty")

    local = tw.copy()
    if local.index.tz is None:
        local.index = local.index.tz_localize(TZ_TW)
    else:
        local.index = local.index.tz_convert(TZ_TW)
    tw_dates = sorted(
        {
            ts.strftime("%Y-%m-%d")
            for ts in local.index
            if TW_OPEN <= ts.strftime("%H:%M") <= TW_CLOSE
        }
    )
    tw_dates = [d for d in tw_dates if len(slice_us_during_tw(nq, d)) >= 3]

    l2 = champion_l2_spec()
    l2r = champion_l2r_spec()
    rows_l2: list[dict[str, Any]] = []
    rows_l2r: list[dict[str, Any]] = []

    for d in tw_dates:
        pf = build_poll_frame_5m(d, tw, nq, window_min=LOOKBACK_MIN)
        if pf is None or len(pf) < N_BARS + 1:
            continue
        shorts: dict[int, pd.DataFrame] = {}
        for wmin in (5, 10):
            sf = build_poll_frame_5m(d, tw, nq, window_min=wmin)
            if sf is not None:
                shorts[wmin] = sf
        poll = enrich_poll_30m(pf, short_frames=shorts)
        a = simulate_day(poll, l2)
        b = simulate_day(poll, l2r)
        a["confirm"] = _confirm_label(a)
        b["confirm"] = _confirm_label(b)
        # pulse at detach / rebuy
        if a.get("detach_poll"):
            r0 = poll.loc[poll["poll"] == a["detach_poll"]]
            if len(r0):
                a["gap_pulse_at_detach"] = str(r0.iloc[0].get("gap_pulse", "NA"))
                a["sync_pulse_at_detach"] = str(r0.iloc[0].get("sync_pulse", "NA"))
        if b.get("reenter_poll"):
            r1 = poll.loc[poll["poll"] == b["reenter_poll"]]
            if len(r1):
                b["gap_pulse_at_rebuy"] = str(r1.iloc[0].get("gap_pulse", "NA"))
                b["sync_pulse_at_rebuy"] = str(r1.iloc[0].get("sync_pulse", "NA"))
        rows_l2.append(a)
        rows_l2r.append(b)

    if not rows_l2r:
        raise RuntimeError("No session days for calendar")

    df = pd.DataFrame(rows_l2r).sort_values("date")
    df_l2 = pd.DataFrame(rows_l2).sort_values("date")

    # cumulative equity (pct of notional, compounded as sum of daily %)
    hold_eq = (df["pnl_hold_close_pct"].astype(float) / 100.0 * NOTIONAL).cumsum()
    strat_eq = (df["pnl_strat_pct"].astype(float) / 100.0 * NOTIONAL).cumsum()
    flat_eq = (df_l2["pnl_strat_pct"].astype(float) / 100.0 * NOTIONAL).cumsum()

    n_trig = int(df["triggered"].sum())
    n_rebuy = int(df["reentered"].sum())
    n_event = int(df["is_event3"].sum())
    n_ok = int((df["confirm"] == "OK_EVENT").sum())
    n_fa = int((df["confirm"] == "FALSE_ALARM").sum())
    n_miss = int((df["confirm"] == "MISS_EVENT").sum())
    saved_total = float(df["saved_vs_close_ntd"].sum())
    # vs hold: positive saved = 少賠／多賺
    top_save = (
        df[df["saved_vs_close_ntd"] > 0]
        .sort_values("saved_vs_close_ntd", ascending=False)
        .head(12)
        .to_dict(orient="records")
    )
    top_hurt = (
        df[df["saved_vs_close_ntd"] < 0]
        .sort_values("saved_vs_close_ntd", ascending=True)
        .head(8)
        .to_dict(orient="records")
    )

    payload = {
        "meta": {
            "title": "TW↔NQ 30m gate · daily calendar replay",
            "note_90d": "Yahoo 5m max ≈59 calendar days; not full 90d — panel is max available.",
            "window": f"{df['date'].iloc[0]} → {df['date'].iloc[-1]}",
            "n_days": int(len(df)),
            "n_events3": n_event,
            "l2_id": l2.strategy_id,
            "l2r_id": l2r.strategy_id,
            "l2r_rule": l2r.name,
            "notional_ntd": NOTIONAL,
            "sources": "^TWII 5m · NQ=F 5m Yahoo",
        },
        "summary": {
            "gate_trigger_days": n_trig,
            "rebuy_days": n_rebuy,
            "trigger_rate": round(n_trig / len(df), 4),
            "confirm_OK_EVENT": n_ok,
            "confirm_FALSE_ALARM": n_fa,
            "confirm_MISS_EVENT": n_miss,
            "confirm_QUIET_OK": int((df["confirm"] == "QUIET_OK").sum()),
            "saved_vs_hold_total_ntd": round(saved_total, 2),
            "hold_cum_pnl_ntd": round(float(hold_eq.iloc[-1]), 2),
            "l2_flat_cum_pnl_ntd": round(float(flat_eq.iloc[-1]), 2),
            "l2r_cum_pnl_ntd": round(float(strat_eq.iloc[-1]), 2),
            "edge_l2r_vs_hold_ntd": round(float(strat_eq.iloc[-1] - hold_eq.iloc[-1]), 2),
            "edge_l2_vs_hold_ntd": round(float(flat_eq.iloc[-1] - hold_eq.iloc[-1]), 2),
        },
        "top_saved_days": top_save,
        "top_hurt_days": top_hurt,
        "days": df.to_dict(orient="records"),
        "days_l2_flat": df_l2.to_dict(orient="records"),
        "equity": {
            "dates": list(df["date"]),
            "hold_cum_ntd": [round(float(x), 2) for x in hold_eq],
            "l2_flat_cum_ntd": [round(float(x), 2) for x in flat_eq],
            "l2r_cum_ntd": [round(float(x), 2) for x in strat_eq],
        },
    }

    if out_dir is not None:
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        fig_dir = out_dir / "figs"
        fig_dir.mkdir(exist_ok=True)
        _setup_cjk_font()

        # Equity
        fig, ax = plt.subplots(figsize=(9.5, 4.2))
        x = list(range(len(df)))
        ax.plot(x, hold_eq, label="無風控（抱到收盤）", color="#8fa3b5", lw=1.8)
        ax.plot(x, flat_eq, label="L2 只出清不接回", color="#e67e22", lw=1.6)
        ax.plot(x, strat_eq, label="L2+L2R 出清後可接回", color="#3dd6c6", lw=2.0)
        ax.axhline(0, color="#445", lw=0.8)
        ax.set_title("累積 PnL（1M NTD 名義）· 逐日加總")
        step = max(1, len(x) // 10)
        ax.set_xticks(x[::step])
        ax.set_xticklabels([df["date"].iloc[i][5:] for i in x[::step]], rotation=45, ha="right", fontsize=8)
        ax.legend(loc="best", fontsize=8)
        ax.set_ylabel("NTD")
        fig.tight_layout()
        fig.savefig(fig_dir / "equity_vs_hold.png", dpi=130)
        plt.close(fig)

        # Daily saved
        fig, ax = plt.subplots(figsize=(9.5, 3.6))
        colors = ["#3dd6c6" if v >= 0 else "#e74c3c" for v in df["saved_vs_close_ntd"]]
        ax.bar(x, df["saved_vs_close_ntd"], color=colors, width=0.85)
        ax.axhline(0, color="#445", lw=0.8)
        ax.set_title("相對無風控：當日少賠／少賺（正=閘門比較好）")
        ax.set_xticks(x[::step])
        ax.set_xticklabels([df["date"].iloc[i][5:] for i in x[::step]], rotation=45, ha="right", fontsize=8)
        ax.set_ylabel("NTD")
        fig.tight_layout()
        fig.savefig(fig_dir / "daily_saved_vs_hold.png", dpi=130)
        plt.close(fig)

        # Trigger markers on hold path
        fig, ax = plt.subplots(figsize=(9.5, 3.6))
        ax.plot(x, df["pnl_hold_close_pct"], color="#8fa3b5", lw=1.2, label="收盤相對開盤 %")
        for i, r in enumerate(df.to_dict(orient="records")):
            if r["triggered"]:
                ax.axvline(i, color="#e67e22", alpha=0.25, lw=1)
            if r.get("reentered"):
                ax.scatter([i], [r["pnl_hold_close_pct"]], color="#3dd6c6", s=28, zorder=3)
        ax.axhline(0, color="#445", lw=0.8)
        ax.set_title("出清日（橘豎線）· 接回日（青點）疊在當日收盤損益上")
        ax.set_xticks(x[::step])
        ax.set_xticklabels([df["date"].iloc[i][5:] for i in x[::step]], rotation=45, ha="right", fontsize=8)
        ax.legend(loc="best", fontsize=8)
        fig.tight_layout()
        fig.savefig(fig_dir / "trigger_overlay.png", dpi=130)
        plt.close(fig)

        (out_dir / "calendar.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )

    return payload
