"""TW↔NQ sell-only risk gate · live-readiness stress (no rebuy).

Dual panel:
  - 5m / rolling 30m: operational L2 (Yahoo ~60d)
  - 1h / rolling 60m: long-sample stress (Yahoo ~730d)

Research layer only. Objective (user): sell 就好 — 壓偽警、少漏真殺、可人工出清。
Does NOT graduate Order-layer auto sell-all by default.

Confidence gates (sell-flat ops memo):
  G1 recall_event ≥ 0.75
  G2 fpr_nonevent ≤ 0.22
  G3 trigger_rate ≤ 0.28
  G4 catastrophic_miss = 0  (event∧¬trigger∧close≤−2%)
  G5 sell_near_trough_on_events ≤ 0.25
  G6 edge_vs_hold_ntd > 0
Both panels should pass for 「有信心做人工出清 SOP」; auto Order still needs separate adoption.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from research.backtest.us_tw_divergence_sell_gate_study import (
    TZ_ET,
    TZ_TW,
    TW_CLOSE,
    TW_OPEN,
    fetch_yahoo_ohlc,
    slice_tw_session,
    slice_us_during_tw,
)
from research.backtest.us_tw_rolling_5m_detach_study import (
    NOTIONAL,
    build_poll_frame_5m,
    fetch_yahoo_5m_panel,
)
from research.backtest.us_tw_30m_rolling_risk_gate_study import enrich_poll_30m

EVENT_PTT = 3.0
SELL_NEAR_TROUGH_BP = 0.35
CATASTROPHIC_CLOSE = -2.0
FEE_RT_HAIRCUT_PCT = 0.10

# Confidence rubrics — trough-first（會接回 → 對收盤 edge 次要）
# Primary: catch events early above trough; never sell into the low.
GATE_RECALL = 0.70
GATE_FPR = 0.22
GATE_TRIG_RATE = 0.35
GATE_SELL_NEAR = 0.0  # hard: no sell@trough on event days
GATE_MED_HEADROOM = 1.0  # median exit − trough ≥ 1.0%
GATE_MIN_HEADROOM = 0.70  # worst event exit − trough ≥ 0.70%
# Auto Order would need stricter bars; kept for reporting only
AUTO_RECALL = 0.85
AUTO_FPR = 0.10
AUTO_TRIG_RATE = 0.15
AUTO_MED_HEADROOM = 1.5


@dataclass(frozen=True)
class SellSpec:
    strategy_id: str
    panel: str  # "5m30" | "1h60"
    win_thr: float
    confirm_polls: int = 2
    require_tw_win_neg: bool = False
    require_tw_from_open_neg: bool = False
    # Must already be down ≤ X% from open (None = off)
    max_tw_from_open: float | None = None
    arm_after: str | None = None  # e.g. "09:40"
    arm_before: str | None = None  # e.g. "12:30"
    lookback_bars: int = 6  # 5m:6≈30m · 1h:1≈60m


def _ret(a: float, b: float) -> float:
    if not (math.isfinite(a) and math.isfinite(b) and a != 0):
        return float("nan")
    return (b / a - 1.0) * 100.0


def build_poll_frame_1h(
    session_date: str,
    tw_1h: pd.DataFrame,
    nq_1h: pd.DataFrame,
) -> pd.DataFrame | None:
    """Hourly poll during TW session; spread_win = last completed hour TW−NQ %."""
    tw = slice_tw_session(tw_1h, session_date)
    nq = slice_us_during_tw(nq_1h, session_date)
    if len(tw) < 2 or len(nq) < 1:
        return None

    tw = tw.copy()
    if tw.index.tz is None:
        tw.index = tw.index.tz_localize(TZ_TW)
    else:
        tw.index = tw.index.tz_convert(TZ_TW)
    nq = nq.copy()
    if nq.index.tz is None:
        nq.index = nq.index.tz_localize(TZ_ET)
    else:
        nq.index = nq.index.tz_convert(TZ_ET)
    nq_tw = nq.copy()
    nq_tw.index = nq_tw.index.tz_convert(TZ_TW)
    nq_close = nq_tw["close"].sort_index()

    nq_aligned = []
    for ts in tw.index:
        sub = nq_close.loc[:ts]
        nq_aligned.append(float(sub.iloc[-1]) if len(sub) else float("nan"))
    out = pd.DataFrame(
        {
            "tw_open": tw["open"].astype(float).values,
            "tw_high": tw["high"].astype(float).values,
            "tw_low": tw["low"].astype(float).values,
            "tw_close": tw["close"].astype(float).values,
            "nq_close": nq_aligned,
        },
        index=tw.index,
    ).dropna()
    if len(out) < 2:
        return None

    tw0 = float(out["tw_open"].iloc[0])
    nq0 = float(out["nq_close"].iloc[0])
    out["tw_from_open"] = (out["tw_close"] / tw0 - 1.0) * 100.0
    out["nq_from_open"] = (out["nq_close"] / nq0 - 1.0) * 100.0

    rows = []
    for i in range(1, len(out)):
        # 1 completed hour ≈ 60m lookback
        tw_w = _ret(float(out["tw_open"].iloc[i]), float(out["tw_close"].iloc[i]))
        nq_w = _ret(float(out["nq_close"].iloc[i - 1]), float(out["nq_close"].iloc[i]))
        # also rolling prior+current (≈120m) for optional thr stability — use 1h primary
        ts = out.index[i]
        rows.append(
            {
                "ts": ts,
                "poll": ts.strftime("%H:%M"),
                "tw_from_open": float(out["tw_from_open"].iloc[i]),
                "nq_from_open": float(out["nq_from_open"].iloc[i]),
                "spread_30m": tw_w - nq_w if math.isfinite(tw_w) and math.isfinite(nq_w) else float("nan"),
                "tw_win": tw_w,
                "nq_win": nq_w,
            }
        )
    if not rows:
        return None
    poll = pd.DataFrame(rows)
    trough_from_open = float((out["tw_low"].min() / tw0 - 1.0) * 100.0)
    close_from_open = float(out["tw_from_open"].iloc[-1])
    peak_to_trough = float(
        (out["tw_high"].max() - out["tw_low"].min()) / out["tw_high"].max() * 100.0
    )
    poll.attrs["close_from_open"] = close_from_open
    poll.attrs["trough_from_open"] = trough_from_open
    poll.attrs["peak_to_trough"] = peak_to_trough
    poll.attrs["date"] = session_date
    poll.attrs["trough_idx"] = int(poll["tw_from_open"].idxmin()) if len(poll) else 0
    poll.attrs["trough_poll_close"] = (
        str(poll.loc[poll.attrs["trough_idx"], "poll"]) if len(poll) else None
    )
    return poll


def frozen_sell_gate_spec() -> SellSpec:
    """Frozen 5m Detach Gate RED — trough-first (rebuy assumed later).

    Objective: maximize exit−trough headroom on ≥3% days; sell_near_trough=0.
    Not optimized vs hold-to-close (close recovery is OK if you rebuy).

    Rule: spread_30m ≤ -0.6 ×1 · tw_from_open ≤ -0.5 · arm 09:40–12:30
    Evidence (Yahoo 5m ~49d): recall≈0.82 · sell@trough=0 · med headroom≈1.6% ·
    2026-07-14 exit 09:40 · headroom≈+2.13% vs trough 11:15.
    """
    return SellSpec(
        strategy_id="s5_w-0.6_c1_down-0.5_arm0940-1230",
        panel="5m30",
        win_thr=-0.6,
        confirm_polls=1,
        max_tw_from_open=-0.5,
        arm_after="09:40",
        arm_before="12:30",
        lookback_bars=6,
    )


def sell_only_grid_5m() -> list[SellSpec]:
    specs: list[SellSpec] = []
    # Always include frozen first
    frozen = frozen_sell_gate_spec()
    specs.append(frozen)
    for thr in (-0.5, -0.6, -0.8, -1.0, -1.2):
        for conf in (1, 2, 3):
            for open_cap in (None, -0.3, -0.5, -0.8):
                for arm in (
                    (None, None),
                    ("09:40", "12:30"),
                    ("09:40", "13:00"),
                    ("09:50", "12:00"),
                    ("09:45", "12:30"),
                ):
                    sid = f"s5_w{thr}_c{conf}"
                    if open_cap is not None:
                        sid += f"_down{open_cap}"
                    if arm[0]:
                        sid += f"_arm{arm[0].replace(':', '')}-{arm[1].replace(':', '')}"
                    if sid == frozen.strategy_id:
                        continue
                    specs.append(
                        SellSpec(
                            strategy_id=sid,
                            panel="5m30",
                            win_thr=thr,
                            confirm_polls=conf,
                            max_tw_from_open=open_cap,
                            arm_after=arm[0],
                            arm_before=arm[1],
                            lookback_bars=6,
                        )
                    )
    return specs


def sell_only_grid_1h() -> list[SellSpec]:
    """Optional / unused by default — user ops is 5m-only."""
    specs: list[SellSpec] = []
    for thr in (-0.4, -0.5, -0.6, -0.8, -1.0, -1.2):
        for conf in (1, 2):
            for open_cap in (None, -0.5, -0.8, -1.0):
                for arm in ((None, None), ("09:00", "12:00"), ("10:00", "13:00")):
                    sid = f"s1h_w{thr}_c{conf}"
                    if open_cap is not None:
                        sid += f"_down{open_cap}"
                    if arm[0]:
                        sid += f"_arm{arm[0].replace(':', '')}-{arm[1].replace(':', '')}"
                    specs.append(
                        SellSpec(
                            strategy_id=sid,
                            panel="1h60",
                            win_thr=thr,
                            confirm_polls=conf,
                            max_tw_from_open=open_cap,
                            arm_after=arm[0],
                            arm_before=arm[1],
                            lookback_bars=1,
                        )
                    )
    return specs


def _detach_mask(poll: pd.DataFrame, spec: SellSpec) -> pd.Series:
    det = poll["spread_30m"] <= spec.win_thr
    if spec.require_tw_win_neg and "tw_win" in poll.columns:
        det = det & (poll["tw_win"] < 0)
    if spec.require_tw_from_open_neg:
        det = det & (poll["tw_from_open"] < 0)
    if spec.max_tw_from_open is not None:
        det = det & (poll["tw_from_open"] <= float(spec.max_tw_from_open))
    if spec.arm_after is not None:
        det = det & (poll["poll"] >= spec.arm_after)
    if spec.arm_before is not None:
        det = det & (poll["poll"] <= spec.arm_before)
    if spec.confirm_polls <= 1:
        return det.fillna(False)
    run = det.astype(int).groupby((~det).cumsum()).cumsum()
    return (run >= spec.confirm_polls).fillna(False)


def simulate_sell_flat(poll: pd.DataFrame, spec: SellSpec) -> dict[str, Any]:
    close_r = float(poll.attrs["close_from_open"])
    trough_r = float(poll.attrs["trough_from_open"])
    ptt = float(poll.attrs["peak_to_trough"])
    date_s = str(poll.attrs["date"])
    trough_poll = str(poll.attrs.get("trough_poll_close") or "")

    det = _detach_mask(poll, spec)
    idxs = list(poll.index[det])
    base = {
        "date": date_s,
        "strategy_id": spec.strategy_id,
        "panel": spec.panel,
        "triggered": False,
        "detach_poll": None,
        "detach_tw_from_open": None,
        "spread_at_detach": None,
        "pnl_hold_close_pct": round(close_r, 4),
        "pnl_strat_pct": round(close_r, 4),
        "edge_vs_hold_pct": 0.0,
        "trough_from_open_pct": round(trough_r, 4),
        "trough_poll_close": trough_poll,
        "peak_to_trough_pct": round(ptt, 4),
        "exit_headroom_vs_trough_pct": None,
        "sell_near_trough": False,
        "false_alarm_opp_cost_pct": 0.0,
        "is_event3": ptt >= EVENT_PTT,
        "catastrophic_miss": False,
        "close_still_down_miss": False,
    }
    if not idxs:
        miss_cat = bool(ptt >= EVENT_PTT and close_r <= CATASTROPHIC_CLOSE)
        miss_down = bool(ptt >= EVENT_PTT and close_r < 0)
        return {
            **base,
            "catastrophic_miss": miss_cat,
            "close_still_down_miss": miss_down,
        }

    i0 = idxs[0]
    row0 = poll.loc[i0]
    exit_r = float(row0["tw_from_open"])
    det_s = float(row0["spread_30m"])
    headroom = exit_r - trough_r
    sell_near = bool(headroom <= SELL_NEAR_TROUGH_BP)
    # sell flat: no rebuy
    pnl_strat = exit_r
    edge = pnl_strat - close_r
    opp = max(0.0, close_r - exit_r)
    return {
        **base,
        "triggered": True,
        "detach_poll": str(row0["poll"]),
        "detach_tw_from_open": round(exit_r, 4),
        "spread_at_detach": round(det_s, 4),
        "pnl_strat_pct": round(pnl_strat, 4),
        "edge_vs_hold_pct": round(edge, 4),
        "exit_headroom_vs_trough_pct": round(headroom, 4),
        "sell_near_trough": sell_near,
        "false_alarm_opp_cost_pct": round(opp, 4),
    }


def score_sell(day_rows: list[dict[str, Any]], spec: SellSpec) -> dict[str, Any]:
    if not day_rows:
        return {"strategy_id": spec.strategy_id, "n_days": 0}

    n = len(day_rows)
    events = [r for r in day_rows if r["is_event3"]]
    nonev = [r for r in day_rows if not r["is_event3"]]
    trig = [r for r in day_rows if r["triggered"]]
    miss = [r for r in events if not r["triggered"]]
    event_trig = [r for r in events if r["triggered"]]
    fa = [r for r in nonev if r["triggered"]]

    recall = (len(event_trig) / len(events)) if events else float("nan")
    fpr = (len(fa) / len(nonev)) if nonev else float("nan")
    trig_rate = len(trig) / n
    sell_near_rate = (
        sum(1 for r in event_trig if r["sell_near_trough"]) / len(event_trig) if event_trig else float("nan")
    )
    heads = [
        float(r["exit_headroom_vs_trough_pct"])
        for r in event_trig
        if r["exit_headroom_vs_trough_pct"] is not None
    ]
    edge_ntd = sum(float(r["edge_vs_hold_pct"]) for r in day_rows) / 100.0 * NOTIONAL
    fee = len(trig) * (FEE_RT_HAIRCUT_PCT / 100.0) * NOTIONAL
    opp_ntd = sum(float(r["false_alarm_opp_cost_pct"]) for r in day_rows) / 100.0 * NOTIONAL
    cat_miss = sum(1 for r in day_rows if r.get("catastrophic_miss"))
    down_miss = sum(1 for r in miss if float(r["pnl_hold_close_pct"]) < 0)
    edge_after = edge_ntd - fee

    med_h = float(np.median(heads)) if heads else float("nan")
    mean_h = float(np.mean(heads)) if heads else float("nan")
    min_h = float(np.min(heads)) if heads else float("nan")
    saved_vs_trough_ntd = (sum(heads) / 100.0 * NOTIONAL) if heads else 0.0

    gates = {
        "G1_recall": bool(math.isfinite(recall) and recall >= GATE_RECALL),
        "G2_fpr": bool(math.isfinite(fpr) and fpr <= GATE_FPR),
        "G3_trig_rate": bool(trig_rate <= GATE_TRIG_RATE),
        "G4_cat_miss0": cat_miss == 0,
        "G5_sell_near0": bool(math.isfinite(sell_near_rate) and sell_near_rate <= GATE_SELL_NEAR),
        "G6_med_headroom": bool(math.isfinite(med_h) and med_h >= GATE_MED_HEADROOM),
        "G7_min_headroom": bool(math.isfinite(min_h) and min_h >= GATE_MIN_HEADROOM),
    }
    n_pass = sum(1 for v in gates.values() if v)

    auto_ok = bool(
        math.isfinite(recall)
        and recall >= AUTO_RECALL
        and math.isfinite(fpr)
        and fpr <= AUTO_FPR
        and trig_rate <= AUTO_TRIG_RATE
        and cat_miss == 0
        and math.isfinite(sell_near_rate)
        and sell_near_rate <= 0.0
        and math.isfinite(med_h)
        and med_h >= AUTO_MED_HEADROOM
    )

    return {
        "strategy_id": spec.strategy_id,
        "panel": spec.panel,
        "win_thr": spec.win_thr,
        "confirm_polls": spec.confirm_polls,
        "require_tw_win_neg": spec.require_tw_win_neg,
        "require_tw_from_open_neg": spec.require_tw_from_open_neg,
        "max_tw_from_open": spec.max_tw_from_open,
        "arm_after": spec.arm_after,
        "arm_before": spec.arm_before,
        "n_days": n,
        "n_events3": len(events),
        "n_triggers": len(trig),
        "trigger_rate": round(trig_rate, 4),
        "event_recall": round(recall, 4) if math.isfinite(recall) else None,
        "miss_n": len(miss),
        "miss_dates": [r["date"] for r in miss],
        "miss_still_down_n": down_miss,
        "catastrophic_miss_n": cat_miss,
        "fpr_nonevent": round(fpr, 4) if math.isfinite(fpr) else None,
        "false_alarm_n": len(fa),
        "sell_near_trough_rate_on_events": round(sell_near_rate, 4) if math.isfinite(sell_near_rate) else None,
        "median_exit_headroom_pct": round(med_h, 4) if math.isfinite(med_h) else None,
        "mean_exit_headroom_pct": round(mean_h, 4) if math.isfinite(mean_h) else None,
        "min_exit_headroom_pct": round(min_h, 4) if math.isfinite(min_h) else None,
        "saved_vs_trough_ntd": round(saved_vs_trough_ntd, 2),
        "edge_vs_hold_ntd": round(edge_ntd, 2),
        "edge_after_fee_ntd": round(edge_after, 2),
        "opp_cost_ntd": round(opp_ntd, 2),
        "objective": "trough_headroom_first · rebuy_assumed",
        "gates": gates,
        "gates_passed": n_pass,
        "gates_total": len(gates),
        "live_ops_memo_ok": n_pass == len(gates),
        "order_auto_ok": auto_ok,
    }


def _rank_key(s: dict[str, Any]) -> tuple:
    """Trough-first: ops_ok → min/mean headroom → recall → low FPR."""
    return (
        0 if s.get("live_ops_memo_ok") else 1,
        -int(s.get("gates_passed") or 0),
        -(float(s.get("min_exit_headroom_pct") or -9)),
        -(float(s.get("mean_exit_headroom_pct") or -9)),
        -(float(s.get("median_exit_headroom_pct") or -9)),
        -(float(s.get("event_recall") or 0)),
        float(s.get("fpr_nonevent") or 1),
        float(s.get("trigger_rate") or 1),
    )


def _session_dates_5m(tw: pd.DataFrame, nq: pd.DataFrame) -> list[str]:
    local = tw.copy()
    if local.index.tz is None:
        local.index = local.index.tz_localize(TZ_TW)
    else:
        local.index = local.index.tz_convert(TZ_TW)
    dates = sorted(
        {
            ts.strftime("%Y-%m-%d")
            for ts in local.index
            if TW_OPEN <= ts.strftime("%H:%M") <= TW_CLOSE
        }
    )
    return [d for d in dates if len(slice_us_during_tw(nq, d)) >= 3]


def _session_dates_1h(tw: pd.DataFrame, nq: pd.DataFrame) -> list[str]:
    local = tw.copy()
    if local.index.tz is None:
        local.index = local.index.tz_localize(TZ_TW)
    else:
        local.index = local.index.tz_convert(TZ_TW)
    dates = sorted(
        {
            ts.strftime("%Y-%m-%d")
            for ts in local.index
            if TW_OPEN <= ts.strftime("%H:%M") <= TW_CLOSE
        }
    )
    out = []
    for d in dates:
        if len(slice_tw_session(tw, d)) >= 2 and len(slice_us_during_tw(nq, d)) >= 1:
            out.append(d)
    return out


def run_panel_5m(specs: list[SellSpec], end: date) -> tuple[list[dict], dict[str, pd.DataFrame]]:
    tw = fetch_yahoo_5m_panel("^TWII", end)
    nq = fetch_yahoo_5m_panel("NQ=F", end)
    dates = _session_dates_5m(tw, nq)
    polls: dict[str, pd.DataFrame] = {}
    for d in dates:
        pf = build_poll_frame_5m(d, tw, nq, window_min=30)
        if pf is None or len(pf) < 7:
            continue
        polls[d] = enrich_poll_30m(pf)

    scored = []
    day_by_sid: dict[str, list[dict]] = {s.strategy_id: [] for s in specs}
    for d, poll in polls.items():
        for s in specs:
            day_by_sid[s.strategy_id].append(simulate_sell_flat(poll, s))
    for s in specs:
        scored.append(score_sell(day_by_sid[s.strategy_id], s))
    meta = {
        "n_days": len(polls),
        "window": f"{min(polls)} → {max(polls)}" if polls else None,
        "n_events3": sum(
            1 for p in polls.values() if float(p.attrs["peak_to_trough"]) >= EVENT_PTT
        ),
    }
    return scored, {"meta": meta, "day_by_sid": day_by_sid}


def run_panel_1h(specs: list[SellSpec], end: date, *, lookback_days: int = 500) -> tuple[list[dict], dict]:
    start = end - timedelta(days=lookback_days)
    tw = fetch_yahoo_ohlc("^TWII", start, end, interval="1h")
    nq = fetch_yahoo_ohlc("NQ=F", start, end, interval="1h")
    dates = _session_dates_1h(tw, nq)
    polls: dict[str, pd.DataFrame] = {}
    for d in dates:
        pf = build_poll_frame_1h(d, tw, nq)
        if pf is None or len(pf) < 2:
            continue
        polls[d] = pf

    scored = []
    day_by_sid: dict[str, list[dict]] = {s.strategy_id: [] for s in specs}
    for d, poll in polls.items():
        for s in specs:
            day_by_sid[s.strategy_id].append(simulate_sell_flat(poll, s))
    for s in specs:
        scored.append(score_sell(day_by_sid[s.strategy_id], s))

    # IS/OOS mid split on dates
    sorted_d = sorted(polls.keys())
    cut = sorted_d[len(sorted_d) // 2] if sorted_d else None
    is_oos = {}
    if cut:
        for s in specs:
            is_rows = [r for r in day_by_sid[s.strategy_id] if r["date"] < cut]
            oos_rows = [r for r in day_by_sid[s.strategy_id] if r["date"] >= cut]
            is_oos[s.strategy_id] = {
                "is": score_sell(is_rows, s),
                "oos": score_sell(oos_rows, s),
                "cut": cut,
            }

    meta = {
        "n_days": len(polls),
        "window": f"{min(polls)} → {max(polls)}" if polls else None,
        "n_events3": sum(1 for p in polls.values() if float(p.attrs["peak_to_trough"]) >= EVENT_PTT),
        "is_oos_cut": cut,
    }
    return scored, {"meta": meta, "is_oos": is_oos, "day_by_sid": day_by_sid, "polls": polls}


def _case_rows(rows: list[dict] | None) -> list[dict]:
    if not rows:
        return []
    out: list[dict] = []
    ev = [r for r in rows if r.get("is_event3")]
    for r in sorted(ev, key=lambda x: -float(x.get("edge_vs_hold_pct") or 0))[:8]:
        out.append({"kind": "event_edge", **r})
    for r in rows:
        if r.get("catastrophic_miss"):
            out.append({"kind": "cat_miss", **r})
    miss_down = [
        r
        for r in rows
        if r.get("is_event3") and (not r.get("triggered")) and float(r.get("pnl_hold_close_pct") or 0) < 0
    ]
    for r in miss_down[:5]:
        out.append({"kind": "miss_still_down", **r})
    return out


def _split_is_oos(day_rows: list[dict], spec: SellSpec) -> dict[str, Any]:
    dates = sorted({r["date"] for r in day_rows})
    if len(dates) < 10:
        return {"cut": None, "is": None, "oos": None}
    cut = dates[len(dates) // 2]
    is_rows = [r for r in day_rows if r["date"] < cut]
    oos_rows = [r for r in day_rows if r["date"] >= cut]
    return {
        "cut": cut,
        "is": score_sell(is_rows, spec),
        "oos": score_sell(oos_rows, spec),
    }


def _confidence_verdict_5m(
    champ: dict[str, Any],
    *,
    is_oos: dict[str, Any] | None,
) -> dict[str, Any]:
    """5m-schedule-only confidence (user: no 1h requirement)."""
    ok = bool(champ.get("live_ops_memo_ok"))
    auto = bool(champ.get("order_auto_ok"))
    oos = (is_oos or {}).get("oos") or {}
    oos_recall = oos.get("event_recall")
    oos_fee = oos.get("edge_after_fee_ntd")  # retained for report only; not a gate
    oos_near = oos.get("sell_near_trough_rate_on_events")
    oos_med_h = oos.get("median_exit_headroom_pct")
    oos_ok = bool(
        oos
        and oos_recall is not None
        and float(oos_recall) >= 0.60
        and int(oos.get("catastrophic_miss_n") or 0) == 0
        and oos_near is not None
        and float(oos_near) <= 0.0
        and oos_med_h is not None
        and float(oos_med_h) >= 0.80
    )

    if auto and oos_ok:
        level = "USABLE_5M_STRICT"
        note = (
            "5m Detach Gate trough-first：全樣本+OOS 皆過。"
            "目標是賣在谷底之上（可接回）；非對收盤比價。自動下單仍關。"
        )
        confidence = 0.78
    elif ok and oos_ok:
        level = "USABLE_5M"
        note = (
            "Detach Gate（台美脫鉤閘門）可作 5m 人工 RED 出清："
            "評分以 exit−trough headroom 為主（假定稍後接回）；"
            "對收盤略輸可接受。自動下單關閉。"
        )
        confidence = 0.74
    elif ok:
        level = "USABLE_5M_WEAK_OOS"
        note = "全樣本 trough 門檻過關但後半 OOS 偏弱——可用、須複盤。"
        confidence = 0.60
    else:
        level = "NOT_READY"
        note = "5m trough 門檻未過；維持觀察。"
        confidence = 0.35

    return {
        "level": level,
        "confidence_0_1": confidence,
        "for_human_ops_sop": level.startswith("USABLE_5M"),
        "for_order_auto_sell_all": False,
        "note": note,
        "panel": "5m_only",
        "oos_ok": oos_ok,
        "auto_bar_5m": auto,
    }


def run_study(
    *,
    date_end: str | None = None,
    out_dir: Path | None = None,
    include_1h: bool = False,
) -> dict[str, Any]:
    end = date.fromisoformat(date_end) if date_end else date.today()
    specs5 = sell_only_grid_5m()
    scored5, pack5 = run_panel_5m(specs5, end)

    ranked5 = sorted(scored5, key=_rank_key)
    best5 = ranked5[0] if ranked5 else {}
    passed5 = [s for s in ranked5 if s.get("live_ops_memo_ok")]

    frozen = frozen_sell_gate_spec()
    frozen_row = next((s for s in scored5 if s["strategy_id"] == frozen.strategy_id), None)
    # Prefer frozen if it passes; else best passer; else best ranked
    if frozen_row and frozen_row.get("live_ops_memo_ok"):
        champ5 = frozen_row
    elif passed5:
        champ5 = passed5[0]
    else:
        champ5 = best5

    day_rows = (pack5.get("day_by_sid") or {}).get(champ5.get("strategy_id") or "") or []
    # rebuild day rows for frozen via matching strategy_id in specs
    champ_spec = next((s for s in specs5 if s.strategy_id == champ5.get("strategy_id")), frozen)
    is_oos = _split_is_oos(day_rows, champ_spec)
    verdict = _confidence_verdict_5m(champ5, is_oos=is_oos)

    base_row = next((s for s in scored5 if s["strategy_id"] == "s5_w-0.6_c2"), None)

    live_rule = {
        "action": "HUMAN flatten decision on RED — not auto Order",
        "panel_live": "5m · every completed 5m bar",
        "strategy_id": champ5.get("strategy_id"),
        "frozen": champ5.get("strategy_id") == frozen.strategy_id,
        "rule": (
            f"spread_30m≤{champ5.get('win_thr')} ×{champ5.get('confirm_polls')} polls"
            + (
                f" · tw_from_open≤{champ5.get('max_tw_from_open')}"
                if champ5.get("max_tw_from_open") is not None
                else ""
            )
            + (
                f" · arm {champ5.get('arm_after')}–{champ5.get('arm_before')}"
                if champ5.get("arm_after")
                else ""
            )
            + " · NO rebuy"
        ),
        "yellow_watch": "spread_30m≤-0.6 ×2 (no down/arm) · escalate attention only",
        "rebuy": "disabled",
    }

    # Calendar rows for champion
    calendar_days = sorted(day_rows, key=lambda r: r["date"])

    payload = {
        "meta": {
            "title": "TW↔NQ sell-only · 5m schedule live readiness",
            "date_end": end.isoformat(),
            "notional_ntd": NOTIONAL,
            "schedule": "5m polls only (Yahoo ^TWII × NQ=F)",
            "include_1h": include_1h,
            "confidence_gates": {
                "objective": "trough_headroom_first · rebuy_assumed",
                "G1_recall": GATE_RECALL,
                "G2_fpr": GATE_FPR,
                "G3_trig_rate": GATE_TRIG_RATE,
                "G4_cat_miss0": True,
                "G5_sell_near0": GATE_SELL_NEAR,
                "G6_med_headroom": GATE_MED_HEADROOM,
                "G7_min_headroom": GATE_MIN_HEADROOM,
                "oos_soft": {
                    "recall": 0.60,
                    "sell_near0": True,
                    "med_headroom": 0.80,
                    "cat_miss0": True,
                },
            },
            "product_name": {
                "en": "Detach Gate",
                "zh": "台美脫鉤閘門",
            },
            "tiered_sop": {
                "YELLOW_watch": live_rule["yellow_watch"],
                "RED_human_flatten": live_rule["rule"],
            },
            "panel_5m": pack5.get("meta"),
            "graduation": "Research → human 5m SOP · not Order auto sell-all",
        },
        "verdict": verdict,
        "live_rule_candidate": live_rule,
        "frozen_spec": {
            "strategy_id": frozen.strategy_id,
            "win_thr": frozen.win_thr,
            "confirm_polls": frozen.confirm_polls,
            "max_tw_from_open": frozen.max_tw_from_open,
            "arm_after": frozen.arm_after,
            "arm_before": frozen.arm_before,
        },
        "champion_5m": champ5,
        "baseline_5m_s5_w-0.6_c2": base_row,
        "top_5m": ranked5[:15],
        "passed_5m": passed5[:10],
        "cases_5m": _case_rows(day_rows),
        "is_oos_5m": is_oos,
        "calendar_days": calendar_days,
    }

    if include_1h:
        specs1 = sell_only_grid_1h()
        scored1, pack1 = run_panel_1h(specs1, end, lookback_days=520)
        ranked1 = sorted(scored1, key=_rank_key)
        payload["champion_1h"] = ranked1[0] if ranked1 else {}
        payload["top_1h"] = ranked1[:15]
        payload["meta"]["panel_1h"] = pack1.get("meta")

    md = render_markdown(payload)
    if out_dir is not None:
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        fig_dir = out_dir / "figs"
        fig_dir.mkdir(exist_ok=True)
        _plot_scatter(ranked5, fig_dir / "5m_recall_vs_fpr.png", "5m30 sell-only")
        _plot_calendar(calendar_days, fig_dir / "5m_daily_edge.png")
        (out_dir / "payload.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        (out_dir / "report.md").write_text(md, encoding="utf-8")

    return {"payload": payload, "markdown": md}


def _plot_calendar(days: list[dict], path: Path) -> None:
    if not days:
        return
    fig, ax = plt.subplots(figsize=(9.5, 3.6))
    x = list(range(len(days)))
    edges = [float(d.get("edge_vs_hold_pct") or 0) / 100.0 * NOTIONAL for d in days]
    colors = ["#3dd6c6" if e >= 0 else "#e74c3c" for e in edges]
    ax.bar(x, edges, color=colors, width=0.85)
    for i, d in enumerate(days):
        if d.get("triggered"):
            ax.axvline(i, color="#e67e22", alpha=0.2, lw=1)
    ax.axhline(0, color="#445", lw=0.8)
    ax.set_title("5m sell-flat · daily edge vs hold (NTD) · orange = trigger day")
    step = max(1, len(x) // 10)
    ax.set_xticks(x[::step])
    ax.set_xticklabels([days[i]["date"][5:] for i in x[::step]], rotation=45, ha="right", fontsize=8)
    ax.set_ylabel("NTD")
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)


def _plot_scatter(rows: list[dict], path: Path, title: str) -> None:
    if not rows:
        return
    fig, ax = plt.subplots(figsize=(7.5, 4.5))
    xs, ys, cs = [], [], []
    for r in rows:
        if r.get("fpr_nonevent") is None or r.get("event_recall") is None:
            continue
        xs.append(r["fpr_nonevent"])
        ys.append(r["event_recall"])
        cs.append("#3dd6c6" if r.get("live_ops_memo_ok") else "#8fa3b5")
    ax.scatter(xs, ys, c=cs, s=28, alpha=0.85)
    ax.axhline(GATE_RECALL, color="#e67e22", ls="--", lw=0.9)
    ax.axvline(GATE_FPR, color="#e67e22", ls="--", lw=0.9)
    ax.set_xlabel("FPR (non-event)")
    ax.set_ylabel("Event recall")
    ax.set_title(title)
    ax.set_xlim(0, max(0.6, max(xs) if xs else 0.6))
    ax.set_ylim(0, 1.05)
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)


def render_markdown(payload: dict[str, Any]) -> str:
    m = payload["meta"]
    v = payload["verdict"]
    c5 = payload.get("champion_5m") or {}
    base = payload.get("baseline_5m_s5_w-0.6_c2") or {}
    live = payload.get("live_rule_candidate") or {}
    frozen = payload.get("frozen_spec") or {}
    is_oos = payload.get("is_oos_5m") or {}
    lines = [
        f"# {m['title']}",
        "",
        f"- End: `{m['date_end']}` · Notional {m['notional_ntd']:,.0f} NTD",
        f"- Schedule: **{m.get('schedule')}**",
        f"- 5m panel: {m.get('panel_5m')}",
        f"- Graduation: {m.get('graduation')}",
        "",
        "## Verdict",
        "",
        f"**Level:** `{v.get('level')}` · confidence≈{v.get('confidence_0_1')}",
        f"- Human 5m SOP: **{v.get('for_human_ops_sop')}**",
        f"- Order auto sell-all: **{v.get('for_order_auto_sell_all')}**",
        f"- OOS soft OK: **{v.get('oos_ok')}**",
        f"- {v.get('note')}",
        "",
        "## Frozen / live rule（賣而不接回 · 每5分）",
        "",
        f"- Frozen id: `{frozen.get('strategy_id')}`",
        f"- Live id: `{live.get('strategy_id')}` · frozen_match={live.get('frozen')}",
        f"- {live.get('rule')}",
        f"- YELLOW: {live.get('yellow_watch')}",
        "",
        "## Champion 5m",
        "",
        "| id | recall | FPR | trig% | cat | fee-edge | gates |",
        "|---|---:|---:|---:|---:|---:|---|",
        (
            f"| `{c5.get('strategy_id')}` | {c5.get('event_recall')} | {c5.get('fpr_nonevent')} | "
            f"{c5.get('trigger_rate')} | {c5.get('catastrophic_miss_n')} | "
            f"{c5.get('edge_after_fee_ntd')} | {c5.get('gates_passed')}/{c5.get('gates_total')} |"
        ),
        "",
        "## 5m IS / OOS",
        "",
        f"- Cut: `{is_oos.get('cut')}`",
    ]
    for lab in ("is", "oos"):
        s = is_oos.get(lab) or {}
        if not s:
            continue
        lines.append(
            f"- **{lab.upper()}**: recall={s.get('event_recall')} FPR={s.get('fpr_nonevent')} "
            f"trig={s.get('trigger_rate')} feeE={s.get('edge_after_fee_ntd')} "
            f"cat={s.get('catastrophic_miss_n')} ops_ok={s.get('live_ops_memo_ok')}"
        )
    lines += ["", "## Baseline (−0.6×2)", ""]
    if base:
        lines.append(
            f"- recall={base.get('event_recall')} FPR={base.get('fpr_nonevent')} "
            f"trig={base.get('trigger_rate')} feeE={base.get('edge_after_fee_ntd')} "
            f"ops_ok={base.get('live_ops_memo_ok')}"
        )
    lines += [
        "",
        "## Top 5m",
        "",
        "| id | recall | FPR | trig | fee-edge | gates | ops |",
        "|---|---:|---:|---:|---:|---:|---|",
    ]
    for r in payload.get("top_5m") or []:
        lines.append(
            f"| `{r.get('strategy_id')}` | {r.get('event_recall')} | {r.get('fpr_nonevent')} | "
            f"{r.get('trigger_rate')} | {r.get('edge_after_fee_ntd')} | "
            f"{r.get('gates_passed')}/{r.get('gates_total')} | {r.get('live_ops_memo_ok')} |"
        )
    lines += [
        "",
        "## Method",
        "",
        "- 5m rolling 30m spread only · sell-flat · no rebuy · no 1h requirement.",
        "- Event = session peak→trough ≥3%.",
        "- Fee haircut 0.10% notional per trigger day.",
        "- Research → human 5m SOP · not Order auto.",
        "",
    ]
    return "\n".join(lines)
