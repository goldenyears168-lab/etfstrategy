"""TW↔NQ 5m state machine · SYNC / DETACH / REVERSAL / REPAIRING · constrained sweep.

Research layer only. Reuses poll-frame builders from us_tw_rolling_5m_detach_study.
PIT-safe: each poll uses only completed bars ≤ poll time.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

from research.backtest.us_tw_divergence_sell_gate_study import TZ_TW, slice_us_during_tw
from research.backtest.us_tw_rolling_5m_detach_study import (
    NOTIONAL,
    TW_CLOSE,
    TW_OPEN,
    StrategySpec,
    _detach_mask,
    build_poll_frame_5m,
    fetch_yahoo_5m_panel,
    score_strategy,
)


# ── Registered constrained grid (finite; documented in payload) ───────────
CUM_GRID = (-0.8, -1.0, -1.2)
WIN_GRID = (-0.4, -0.6, -0.8)
WINDOW_GRID = (5, 10)
CONFIRM_GRID = (1, 2)
CORR_DETACH_GRID = (None, 0.0, 0.2)
REPAIR_CORR_MIN = (0.2, 0.4)
REPAIR_SAME_SIGN_MIN = (0.6, 0.8)
REPAIR_DELTA_CUM = (0.2, 0.4, 0.6)
# Hour-2 refine (narrow cores only — not full cartesian)
FOCUS_CUM = (-1.0, -1.2)
FOCUS_WIN = (-0.6, -0.8)
REFINE_DELTA_CUM = (0.3, 0.5)
REFINE_CUSHION = (None, 0.2, 0.4)
REFINE_DUAL_SYNC = ((0.3, 0.7), (0.4, 0.6))  # (corr_min, same_sign_min) AND
FPR_SOFT_CAP = 0.30
FEE_RT_HAIRCUT_PCT = 0.10  # 10bp round-trip sensitivity (exit+reenter)


@dataclass(frozen=True)
class StateStrategySpec:
    strategy_id: str
    name: str
    # detach
    cum_thr: float | None = None
    win_thr: float | None = None
    corr_thr: float | None = None
    confirm_polls: int = 1
    window_min: int = 5
    # repair / reenter
    reenter_on_repair: bool = False
    require_tw_pos: bool = True  # tw_win > 0
    require_tw_gt_nq: bool = True  # tw_win > nq_win
    repair_corr_min: float | None = None
    repair_same_sign_min: float | None = None
    repair_sync_and: bool = False  # if True and both thresholds set: require AND
    repair_delta_cum: float = 0.3
    repair_cum_cushion: float | None = None  # optional: cum must rise above detach_thr + cushion
    repair_window_min: int | None = None  # cross-window repair features (else = window_min)
    # legacy repair (prior study: spread_win ≥ thr & Δcum ≥ 0.3)
    legacy_repair: bool = False
    repair_win_thr: float = 0.25

    def to_legacy_detach(self) -> StrategySpec:
        return StrategySpec(
            strategy_id=self.strategy_id,
            name=self.name,
            cum_thr=self.cum_thr,
            win_thr=self.win_thr,
            corr_thr=self.corr_thr,
            confirm_polls=self.confirm_polls,
            reenter_on_repair=False,
            repair_win_thr=self.repair_win_thr,
            window_min=self.window_min,
        )

    @property
    def effective_repair_window(self) -> int:
        return int(self.repair_window_min) if self.repair_window_min else int(self.window_min)


def registered_grid() -> list[StateStrategySpec]:
    """Finite registered strategy grid — call before search; size is recorded."""
    specs: list[StateStrategySpec] = []

    def _add(sid: str, name: str, **kw: Any) -> None:
        specs.append(StateStrategySpec(strategy_id=sid, name=name, **kw))

    # A) Detach-only: cum
    for cum in CUM_GRID:
        for conf in CONFIRM_GRID:
            for w in WINDOW_GRID:
                tag = f"c{conf}" if conf > 1 else ""
                _add(
                    f"detach_cum{cum}{tag}_w{w}",
                    f"DETACH cum≤{cum}×{conf} · win={w}m · no reenter",
                    cum_thr=cum,
                    confirm_polls=conf,
                    window_min=w,
                )

    # B) Detach-only: win
    for win in WIN_GRID:
        for w in WINDOW_GRID:
            _add(
                f"detach_win{win}_w{w}",
                f"DETACH spread_win≤{win} · win={w}m · no reenter",
                win_thr=win,
                window_min=w,
            )

    # C) Detach-only: combo cum OR win
    for cum in CUM_GRID:
        for win in WIN_GRID:
            for conf in CONFIRM_GRID:
                for w in WINDOW_GRID:
                    tag = f"_c{conf}" if conf > 1 else ""
                    _add(
                        f"detach_cum{cum}_win{win}{tag}_w{w}",
                        f"DETACH cum≤{cum} OR win≤{win}×{conf} · win={w}m",
                        cum_thr=cum,
                        win_thr=win,
                        confirm_polls=conf,
                        window_min=w,
                    )

    # D) Detach-only: cum OR (corr & tw↓) — corr∈{0.0,0.2} only (skip None channel)
    for cum in CUM_GRID:
        for corr in (0.0, 0.2):
            for w in WINDOW_GRID:
                _add(
                    f"detach_cum{cum}_corr{corr}_w{w}",
                    f"DETACH cum≤{cum} OR (corr≤{corr}&tw↓) · win={w}m",
                    cum_thr=cum,
                    corr_thr=corr,
                    window_min=w,
                )

    # E) Rich repair reenter — only on combo cum OR win (constrained)
    for cum in CUM_GRID:
        for win in WIN_GRID:
            for conf in CONFIRM_GRID:
                for w in WINDOW_GRID:
                    # sync via corr
                    for rc in REPAIR_CORR_MIN:
                        for dc in REPAIR_DELTA_CUM:
                            tag = f"_c{conf}" if conf > 1 else ""
                            _add(
                                f"sm_cum{cum}_win{win}{tag}_w{w}_rc{rc}_dc{dc}",
                                f"DETACH cum≤{cum}|win≤{win} + REPAIR corr≥{rc} Δcum≥{dc} · w={w}m",
                                cum_thr=cum,
                                win_thr=win,
                                confirm_polls=conf,
                                window_min=w,
                                reenter_on_repair=True,
                                repair_corr_min=rc,
                                repair_delta_cum=dc,
                            )
                    # sync via same-sign
                    for ss in REPAIR_SAME_SIGN_MIN:
                        for dc in REPAIR_DELTA_CUM:
                            tag = f"_c{conf}" if conf > 1 else ""
                            _add(
                                f"sm_cum{cum}_win{win}{tag}_w{w}_ss{ss}_dc{dc}",
                                f"DETACH cum≤{cum}|win≤{win} + REPAIR ss≥{ss} Δcum≥{dc} · w={w}m",
                                cum_thr=cum,
                                win_thr=win,
                                confirm_polls=conf,
                                window_min=w,
                                reenter_on_repair=True,
                                repair_same_sign_min=ss,
                                repair_delta_cum=dc,
                            )

    # F) Prior baseline (legacy repair) for apples-to-apples
    _add(
        "baseline_combo_cum1_win06_reenter_w10",
        "PRIOR combo cum≤-1 OR win≤-0.6 + legacy REENTER · w=10m",
        cum_thr=-1.0,
        win_thr=-0.6,
        window_min=10,
        reenter_on_repair=True,
        legacy_repair=True,
        repair_win_thr=0.25,
        repair_delta_cum=0.3,
        require_tw_pos=False,
        require_tw_gt_nq=False,
    )
    _add(
        "baseline_combo_cum1_win06_reenter_w5",
        "PRIOR combo cum≤-1 OR win≤-0.6 + legacy REENTER · w=5m",
        cum_thr=-1.0,
        win_thr=-0.6,
        window_min=5,
        reenter_on_repair=True,
        legacy_repair=True,
        repair_win_thr=0.25,
        repair_delta_cum=0.3,
        require_tw_pos=False,
        require_tw_gt_nq=False,
    )

    # G) Hour-2 refine — dual-AND sync + cushion on focus cores only
    for cum in FOCUS_CUM:
        for win in FOCUS_WIN:
            for conf in CONFIRM_GRID:
                for w in WINDOW_GRID:
                    tag = f"_c{conf}" if conf > 1 else ""
                    for rc, ss in REFINE_DUAL_SYNC:
                        for dc in REFINE_DELTA_CUM:
                            for cush in REFINE_CUSHION:
                                cush_tag = f"_cu{cush}" if cush is not None else ""
                                _add(
                                    f"h2_cum{cum}_win{win}{tag}_w{w}_rc{rc}_ss{ss}_dc{dc}{cush_tag}",
                                    f"H2 DETACH cum≤{cum}|win≤{win} + REPAIR corr≥{rc}&ss≥{ss} Δcum≥{dc}"
                                    f"{f' cush{cush}' if cush is not None else ''} · w={w}m",
                                    cum_thr=cum,
                                    win_thr=win,
                                    confirm_polls=conf,
                                    window_min=w,
                                    reenter_on_repair=True,
                                    repair_corr_min=rc,
                                    repair_same_sign_min=ss,
                                    repair_sync_and=True,
                                    repair_delta_cum=dc,
                                    repair_cum_cushion=cush,
                                )

    # H) Cross-window: detach lookback ≠ repair lookback (narrow)
    for cum, win in ((-1.0, -0.6), (-1.0, -0.8), (-1.2, -0.6)):
        for conf in CONFIRM_GRID:
            for det_w, rep_w in ((5, 10), (10, 5)):
                tag = f"_c{conf}" if conf > 1 else ""
                for rc in (0.3, 0.4):
                    for dc in (0.3, 0.5):
                        _add(
                            f"xw_cum{cum}_win{win}{tag}_d{det_w}r{rep_w}_rc{rc}_dc{dc}",
                            f"XW DETACH@{det_w}m cum≤{cum}|win≤{win} + REPAIR@{rep_w}m corr≥{rc} Δcum≥{dc}",
                            cum_thr=cum,
                            win_thr=win,
                            confirm_polls=conf,
                            window_min=det_w,
                            repair_window_min=rep_w,
                            reenter_on_repair=True,
                            repair_corr_min=rc,
                            repair_delta_cum=dc,
                        )

    # dedupe
    seen: set[str] = set()
    out: list[StateStrategySpec] = []
    for s in specs:
        if s.strategy_id in seen:
            continue
        seen.add(s.strategy_id)
        out.append(s)
    return out


def attach_repair_window(base: pd.DataFrame, alt: pd.DataFrame) -> pd.DataFrame:
    """Overlay alt-window path/sync cols as r_* for cross-window repair (PIT by poll)."""
    out = base.copy()
    cols = ["tw_win", "nq_win", "spread_win", "corr", "same_sign"]
    present = [c for c in cols if c in alt.columns]
    if not present or "poll" not in alt.columns:
        return out
    m = alt.set_index("poll")[present].add_prefix("r_")
    out = out.join(m, on="poll")
    # join can drop attrs — restore session meta from base
    out.attrs.update(dict(base.attrs))
    return out


def _feat(row: pd.Series, name: str) -> float:
    """Prefer r_* repair-window feature when present and finite."""
    raw = row.get(f"r_{name}")
    try:
        if raw is not None:
            v = float(raw)
            if math.isfinite(v):
                return v
    except (TypeError, ValueError):
        pass
    try:
        return float(row.get(name, float("nan")))
    except (TypeError, ValueError):
        return float("nan")


def _sync_ok(row: pd.Series, spec: StateStrategySpec) -> bool:
    """Sync recovering: corr≥τ and/or same_sign≥τ."""
    c = _feat(row, "corr")
    ss = _feat(row, "same_sign")
    c_ok = spec.repair_corr_min is not None and math.isfinite(c) and c >= spec.repair_corr_min
    ss_ok = (
        spec.repair_same_sign_min is not None
        and math.isfinite(ss)
        and ss >= spec.repair_same_sign_min
    )
    if spec.repair_sync_and and spec.repair_corr_min is not None and spec.repair_same_sign_min is not None:
        return bool(c_ok and ss_ok)
    if spec.repair_corr_min is not None and spec.repair_same_sign_min is not None:
        return bool(c_ok or ss_ok)
    if spec.repair_corr_min is not None:
        return bool(c_ok)
    if spec.repair_same_sign_min is not None:
        return bool(ss_ok)
    return True


def _repair_hit(
    row: pd.Series,
    *,
    detach_cum: float,
    trough_cum: float,
    spec: StateStrategySpec,
) -> bool:
    delta = float(row["cum_spread"]) - trough_cum
    if spec.legacy_repair:
        sw = _feat(row, "spread_win")
        return bool(
            math.isfinite(sw)
            and sw >= spec.repair_win_thr
            and float(row["cum_spread"]) > detach_cum + 0.3
        )
    tw_w = _feat(row, "tw_win")
    nq_w = _feat(row, "nq_win")
    if not (math.isfinite(tw_w) and math.isfinite(nq_w)):
        return False
    if spec.require_tw_gt_nq and not (tw_w > nq_w):
        return False
    if spec.require_tw_pos and not (tw_w > 0):
        return False
    if not _sync_ok(row, spec):
        return False
    if delta < spec.repair_delta_cum:
        return False
    if spec.repair_cum_cushion is not None and spec.cum_thr is not None:
        if float(row["cum_spread"]) < spec.cum_thr + spec.repair_cum_cushion:
            return False
    return True


def _is_reversal(row: pd.Series, prev: pd.Series | None) -> bool:
    """NQ turns while TW lags (narrative / soft flag)."""
    if prev is None:
        return False
    nq_w = float(row.get("nq_win", float("nan")))
    tw_w = float(row.get("tw_win", float("nan")))
    prev_nq = float(prev.get("nq_win", float("nan")))
    if not all(math.isfinite(x) for x in (nq_w, tw_w, prev_nq)):
        return False
    # NQ flips up while TW still down / lagging
    if prev_nq <= 0 < nq_w and tw_w < nq_w and tw_w < 0.1:
        return True
    # NQ flips down hard while TW not following as much (solo NQ risk)
    if prev_nq >= 0 > nq_w and tw_w > nq_w + 0.15:
        return True
    return False


def label_states(poll: pd.DataFrame, spec: StateStrategySpec) -> pd.DataFrame:
    """PIT state labels per poll. Mutual priority: DETACH > REPAIRING > REVERSAL > SYNC.

    Latch: once DETACH fires, remain detached until REPAIRING (reenter latch clear).
    """
    det = _detach_mask(poll, spec.to_legacy_detach())
    states: list[str] = []
    latched = False
    detach_cum = float("nan")
    trough_cum = float("nan")
    first_detach_poll: str | None = None
    repair_polls: list[str] = []
    prev: pd.Series | None = None

    for i in poll.index:
        row = poll.loc[i]
        poll_s = str(row["poll"])
        if not latched:
            if bool(det.loc[i]):
                latched = True
                detach_cum = float(row["cum_spread"])
                trough_cum = detach_cum
                first_detach_poll = first_detach_poll or poll_s
                states.append("DETACH")
            elif _is_reversal(row, prev):
                states.append("REVERSAL")
            else:
                # SYNC if corr/same-sign ok and |spread| not severe; else SYNC soft
                states.append("SYNC")
        else:
            trough_cum = min(trough_cum, float(row["cum_spread"]))
            if _repair_hit(row, detach_cum=detach_cum, trough_cum=trough_cum, spec=spec):
                states.append("REPAIRING")
                repair_polls.append(poll_s)
                if spec.reenter_on_repair:
                    latched = False  # allow reenter; next detach can fire again
                    detach_cum = float("nan")
                    trough_cum = float("nan")
            else:
                states.append("DETACH")
        prev = row

    out = poll.copy()
    out["state"] = states
    out.attrs["first_detach_poll"] = first_detach_poll
    out.attrs["repair_polls"] = repair_polls
    return out


def simulate_day_state(poll: pd.DataFrame, spec: StateStrategySpec) -> dict[str, Any]:
    """Long from open; exit on first DETACH; optional reenter on first REPAIRING."""
    close_r = float(poll.attrs["close_from_open"])
    trough_r = float(poll.attrs["trough_from_open"])
    ptt = float(poll.attrs["peak_to_trough"])
    date_s = str(poll.attrs["date"])

    labeled = label_states(poll, spec)
    # first transition into DETACH
    first_detach_i = None
    for i in labeled.index:
        if labeled.loc[i, "state"] == "DETACH":
            first_detach_i = i
            break

    if first_detach_i is None:
        return {
            "date": date_s,
            "strategy_id": spec.strategy_id,
            "triggered": False,
            "detach_poll": None,
            "detach_tw_from_open": None,
            "cum_spread_at_detach": None,
            "corr_at_detach": None,
            "spread_win_at_detach": None,
            "reentered": False,
            "reenter_poll": None,
            "repair_polls": [],
            "states_compact": _compact_states(labeled),
            "pnl_hold_close_pct": round(close_r, 4),
            "pnl_strat_pct": round(close_r, 4),
            "trough_from_open_pct": round(trough_r, 4),
            "peak_to_trough_pct": round(ptt, 4),
            "saved_vs_close_pct": 0.0,
            "saved_vs_trough_pct": round(max(0.0, -trough_r - max(0.0, -close_r)), 4),
            "saved_vs_close_ntd": 0.0,
            "saved_vs_trough_ntd": round(max(0.0, -trough_r - max(0.0, -close_r)) / 100.0 * NOTIONAL, 2),
            "false_alarm_opp_cost_pct": 0.0,
            "false_alarm_opp_cost_ntd": 0.0,
            "fee_haircut_ntd": 0.0,
            "n_round_trips": 0,
        }

    row0 = labeled.loc[first_detach_i]
    exit_r = float(row0["tw_from_open"])
    reentered = False
    reenter_poll = None
    pnl_strat = exit_r
    n_rt = 1  # exit
    repair_polls: list[str] = []

    if spec.reenter_on_repair:
        after = labeled.loc[first_detach_i:]
        for j in after.index:
            if after.loc[j, "state"] == "REPAIRING":
                reentered = True
                reenter_poll = str(after.loc[j, "poll"])
                repair_polls.append(reenter_poll)
                re_r = float(after.loc[j, "tw_from_open"])
                pnl_strat = exit_r + (close_r - re_r)
                n_rt += 1  # reenter
                break
        # collect later repair labels for case study (even if already reentered)
        for j in after.index:
            if after.loc[j, "state"] == "REPAIRING":
                p = str(after.loc[j, "poll"])
                if p not in repair_polls:
                    repair_polls.append(p)

    loss_hold = max(0.0, -close_r)
    loss_strat = max(0.0, -pnl_strat)
    saved_vs_close = loss_hold - loss_strat
    loss_trough = max(0.0, -trough_r)
    loss_stop = max(0.0, -exit_r)
    saved_vs_trough = loss_trough - loss_stop
    opp = max(0.0, close_r - exit_r) if (not reentered and close_r > exit_r) else 0.0
    fee_ntd = n_rt * (FEE_RT_HAIRCUT_PCT / 100.0) * NOTIONAL / 2.0  # per leg of RT haircut

    return {
        "date": date_s,
        "strategy_id": spec.strategy_id,
        "triggered": True,
        "detach_poll": str(row0["poll"]),
        "detach_tw_from_open": round(exit_r, 4),
        "cum_spread_at_detach": round(float(row0["cum_spread"]), 4),
        "corr_at_detach": None
        if not math.isfinite(float(row0["corr"]))
        else round(float(row0["corr"]), 4),
        "spread_win_at_detach": None
        if not math.isfinite(float(row0["spread_win"]))
        else round(float(row0["spread_win"]), 4),
        "reentered": reentered,
        "reenter_poll": reenter_poll,
        "repair_polls": repair_polls,
        "states_compact": _compact_states(labeled),
        "pnl_hold_close_pct": round(close_r, 4),
        "pnl_strat_pct": round(pnl_strat, 4),
        "trough_from_open_pct": round(trough_r, 4),
        "peak_to_trough_pct": round(ptt, 4),
        "saved_vs_close_pct": round(saved_vs_close, 4),
        "saved_vs_trough_pct": round(saved_vs_trough, 4),
        "saved_vs_close_ntd": round(saved_vs_close / 100.0 * NOTIONAL, 2),
        "saved_vs_trough_ntd": round(saved_vs_trough / 100.0 * NOTIONAL, 2),
        "false_alarm_opp_cost_pct": round(opp, 4),
        "false_alarm_opp_cost_ntd": round(opp / 100.0 * NOTIONAL, 2),
        "fee_haircut_ntd": round(fee_ntd, 2),
        "n_round_trips": n_rt,
    }


def _compact_states(labeled: pd.DataFrame) -> list[dict[str, str]]:
    """Collapse consecutive identical states into runs for timeline HTML."""
    if labeled.empty:
        return []
    runs: list[dict[str, str]] = []
    cur = str(labeled.iloc[0]["state"])
    start = str(labeled.iloc[0]["poll"])
    end = start
    for _, row in labeled.iloc[1:].iterrows():
        st = str(row["state"])
        p = str(row["poll"])
        if st == cur:
            end = p
        else:
            runs.append({"state": cur, "from": start, "to": end})
            cur, start, end = st, p, p
    runs.append({"state": cur, "from": start, "to": end})
    return runs


def pick_champion(sum_df: pd.DataFrame) -> tuple[dict[str, Any], str]:
    """Hard preference: FPR≤0.30 if any qualify; else best net_edge with caveat."""
    if sum_df.empty:
        return {}, "empty"
    df = sum_df.copy()
    fpr_col = "false_trigger_rate_on_nonevent"
    qualified = df[df[fpr_col].fillna(1.0) <= FPR_SOFT_CAP]
    if len(qualified):
        best = qualified.sort_values("net_edge_ntd", ascending=False).iloc[0].to_dict()
        return best, f"fpr_cap_{FPR_SOFT_CAP}"
    best = df.sort_values("net_edge_ntd", ascending=False).iloc[0].to_dict()
    return best, "best_net_edge_fpr_exceeds_cap"


def _score_split(rows: list[dict[str, Any]], dates: list[str]) -> dict[str, Any]:
    subset = [r for r in rows if r["date"] in set(dates)]
    return score_strategy(subset)


def run_study(
    *,
    date_end: str | None = None,
    out_dir: Path,
    case_dates: list[str] | None = None,
) -> dict[str, Any]:
    end_d = date.fromisoformat(date_end) if date_end else date.today()
    out_dir.mkdir(parents=True, exist_ok=True)
    fig_dir = out_dir / "figs"
    fig_dir.mkdir(parents=True, exist_ok=True)

    tw = fetch_yahoo_5m_panel("^TWII", end_d)
    nq = fetch_yahoo_5m_panel("NQ=F", end_d)
    if tw.empty or nq.empty:
        raise RuntimeError("Yahoo 5m panel empty for ^TWII or NQ=F")

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
    dates = [d for d in dates if len(slice_us_during_tw(nq, d)) >= 3]

    if case_dates is None:
        case_dates = [
            "2026-07-14",
            "2026-07-07",
            "2026-06-08",
            "2026-06-26",
            "2026-06-05",
            "2026-05-28",
            "2026-06-10",
            "2026-06-11",
        ]

    specs = registered_grid()
    n_h2 = sum(1 for s in specs if s.strategy_id.startswith("h2_"))
    n_xw = sum(1 for s in specs if s.strategy_id.startswith("xw_"))
    grid_meta = {
        "n_strategies": len(specs),
        "n_hour2_dual_sync": n_h2,
        "n_cross_window": n_xw,
        "cum_grid": list(CUM_GRID),
        "win_grid": list(WIN_GRID),
        "window_grid": list(WINDOW_GRID),
        "confirm_grid": list(CONFIRM_GRID),
        "corr_detach_grid": list(CORR_DETACH_GRID),
        "repair_corr_min": list(REPAIR_CORR_MIN),
        "repair_same_sign_min": list(REPAIR_SAME_SIGN_MIN),
        "repair_delta_cum": list(REPAIR_DELTA_CUM),
        "refine_dual_sync": [list(x) for x in REFINE_DUAL_SYNC],
        "refine_delta_cum": list(REFINE_DELTA_CUM),
        "refine_cushion": list(REFINE_CUSHION),
        "fpr_soft_cap": FPR_SOFT_CAP,
        "fee_rt_haircut_pct": FEE_RT_HAIRCUT_PCT,
        "axes_note": (
            "Pass1: detach cum/win/combo/corr; repair corr|ss + Δcum + tw>nq&tw>0; "
            "legacy baselines. "
            "Hour2: dual-AND sync + cushion on focus cores; cross-window detach≠repair lookback."
        ),
    }

    polls_cache: dict[tuple[str, int], pd.DataFrame] = {}
    day_meta = []
    for d in dates:
        for w in WINDOW_GRID:
            pf = build_poll_frame_5m(d, tw, nq, window_min=w)
            if pf is not None:
                polls_cache[(d, w)] = pf
        if (d, 5) in polls_cache:
            pf = polls_cache[(d, 5)]
            day_meta.append(
                {
                    "date": d,
                    "peak_to_trough_pct": float(pf.attrs["peak_to_trough"]),
                    "close_from_open_pct": float(pf.attrs["close_from_open"]),
                    "trough_from_open_pct": float(pf.attrs["trough_from_open"]),
                    "is_event3": float(pf.attrs["peak_to_trough"]) >= 3.0,
                }
            )
    day_meta_df = pd.DataFrame(day_meta)

    def _poll_for(d: str, spec: StateStrategySpec) -> pd.DataFrame | None:
        pf = polls_cache.get((d, spec.window_min))
        if pf is None:
            return None
        rw = spec.effective_repair_window
        if rw != spec.window_min:
            alt = polls_cache.get((d, rw))
            if alt is not None:
                return attach_repair_window(pf, alt)
        return pf

    # IS / OOS chronological split (2/3 · 1/3)
    n = len(dates)
    cut = max(1, int(n * 2 / 3))
    is_dates = dates[:cut]
    oos_dates = dates[cut:]

    all_rows: list[dict[str, Any]] = []
    summaries = []
    for spec in specs:
        rows = []
        for d in dates:
            pf = _poll_for(d, spec)
            if pf is None:
                continue
            rows.append(simulate_day_state(pf, spec))
        all_rows.extend(rows)
        sm = score_strategy(rows)
        sm["strategy_id"] = spec.strategy_id
        sm["name"] = spec.name
        sm["window_min"] = spec.window_min
        sm["repair_window_min"] = spec.effective_repair_window
        sm["confirm_polls"] = spec.confirm_polls
        sm["reenter"] = spec.reenter_on_repair
        sm["cum_thr"] = spec.cum_thr
        sm["win_thr"] = spec.win_thr
        sm["corr_thr"] = spec.corr_thr
        sm["legacy_repair"] = spec.legacy_repair
        sm["repair_corr_min"] = spec.repair_corr_min
        sm["repair_same_sign_min"] = spec.repair_same_sign_min
        sm["repair_sync_and"] = spec.repair_sync_and
        sm["repair_delta_cum"] = spec.repair_delta_cum
        sm["repair_cum_cushion"] = spec.repair_cum_cushion
        # fee sensitivity
        fee_sum = float(sum(r.get("fee_haircut_ntd", 0.0) or 0.0 for r in rows))
        sm["sum_fee_haircut_ntd"] = round(fee_sum, 2)
        sm["net_edge_after_fee_ntd"] = round(float(sm.get("net_edge_ntd") or 0) - fee_sum, 2)
        # IS/OOS
        sm_is = _score_split(rows, is_dates)
        sm_oos = _score_split(rows, oos_dates)
        sm["is_net_edge_ntd"] = sm_is.get("net_edge_ntd")
        sm["oos_net_edge_ntd"] = sm_oos.get("net_edge_ntd")
        sm["is_fpr"] = sm_is.get("false_trigger_rate_on_nonevent")
        sm["oos_fpr"] = sm_oos.get("false_trigger_rate_on_nonevent")
        sm["is_recall"] = sm_is.get("event_recall")
        sm["oos_recall"] = sm_oos.get("event_recall")
        summaries.append(sm)

    sum_df = pd.DataFrame(summaries)
    if sum_df.empty or "net_edge_ntd" not in sum_df.columns:
        raise RuntimeError("No strategy rows — check 5m calendar overlap")
    sum_df = sum_df.sort_values("net_edge_ntd", ascending=False)
    best, pick_rule = pick_champion(sum_df)

    # baseline compare
    baseline_id = "baseline_combo_cum1_win06_reenter_w10"
    baseline_row = None
    if baseline_id in set(sum_df["strategy_id"]):
        baseline_row = sum_df[sum_df["strategy_id"] == baseline_id].iloc[0].to_dict()

    focus_ids: list[str] = []
    if best:
        focus_ids.append(str(best["strategy_id"]))
    for sid in (
        baseline_id,
        "baseline_combo_cum1_win06_reenter_w5",
        "detach_cum-1.0_win-0.6_w10",
        "detach_cum-1.0_win-0.6_w5",
    ):
        if sid in set(sum_df["strategy_id"]) and sid not in focus_ids:
            focus_ids.append(sid)
    # top rich-repair / hour2 / xw besides champ
    for sid in sum_df["strategy_id"].tolist():
        if any(sid.startswith(p) for p in ("sm_", "h2_", "xw_")) and sid not in focus_ids:
            focus_ids.append(sid)
        if len(focus_ids) >= 8:
            break

    case_payload = []
    for d in case_dates:
        if (d, 5) not in polls_cache:
            case_payload.append({"date": d, "present": False})
            continue
        pf5 = polls_cache[(d, 5)]
        entry: dict[str, Any] = {
            "date": d,
            "present": True,
            "peak_to_trough_pct": float(pf5.attrs["peak_to_trough"]),
            "close_from_open_pct": float(pf5.attrs["close_from_open"]),
            "trough_from_open_pct": float(pf5.attrs["trough_from_open"]),
            "strategy_outcomes": {},
            "timelines": {},
        }
        for sid in focus_ids:
            spec = next(s for s in specs if s.strategy_id == sid)
            pf_w = _poll_for(d, spec)
            if pf_w is None:
                continue
            entry["strategy_outcomes"][sid] = simulate_day_state(pf_w, spec)
            labeled = label_states(pf_w, spec)
            cols = [
                "poll",
                "tw_from_open",
                "nq_from_open",
                "cum_spread",
                "tw_win",
                "nq_win",
                "spread_win",
                "corr",
                "same_sign",
                "state",
            ]
            present_cols = [c for c in cols if c in labeled.columns]
            entry["timelines"][sid] = labeled[present_cols].round(3).to_dict(orient="records")
        case_payload.append(entry)
        champ_out = entry["strategy_outcomes"].get(focus_ids[0]) if focus_ids else None
        champ_w = specs_by_id(specs, focus_ids[0]).window_min if focus_ids else 5
        pf_plot = polls_cache.get((d, champ_w), pf5)
        _save_case_fig(
            fig_dir / f"case_{d.replace('-', '')}_state.png",
            pf_plot,
            champ_out,
            entry["timelines"].get(focus_ids[0]) if focus_ids else None,
        )

    def _spotlight(day: str) -> dict[str, Any] | None:
        for c in case_payload:
            if c.get("date") == day and c.get("present") and focus_ids:
                sid = focus_ids[0]
                o = c["strategy_outcomes"].get(sid) or {}
                return {
                    "date": day,
                    "champ_id": sid,
                    "detach_poll": o.get("detach_poll"),
                    "reenter_poll": o.get("reenter_poll"),
                    "repair_polls": o.get("repair_polls"),
                    "states_compact": o.get("states_compact"),
                    "timeline": c["timelines"].get(sid),
                    "peak_to_trough_pct": c.get("peak_to_trough_pct"),
                    "close_from_open_pct": c.get("close_from_open_pct"),
                    "trough_from_open_pct": c.get("trough_from_open_pct"),
                }
        return None

    spotlight_714 = _spotlight("2026-07-14")
    spotlight_707 = _spotlight("2026-07-07")

    oos_holds = False
    if best and best.get("is_net_edge_ntd") is not None and best.get("oos_net_edge_ntd") is not None:
        oos_holds = float(best["oos_net_edge_ntd"]) > 0 and (
            float(best["is_net_edge_ntd"]) > 0
        )

    pd.DataFrame(all_rows).to_csv(out_dir / "day_strategy_pnl.csv", index=False)
    sum_df.to_csv(out_dir / "strategy_summary.csv", index=False)
    day_meta_df.to_csv(out_dir / "day_meta.csv", index=False)

    payload = {
        "meta": {
            "date_start": dates[0] if dates else None,
            "date_end": dates[-1] if dates else None,
            "n_days": len(dates),
            "n_events3": int(day_meta_df["is_event3"].sum()) if len(day_meta_df) else 0,
            "notional_ntd": NOTIONAL,
            "cadence": "5m bar poll · state machine SYNC/DETACH/REVERSAL/REPAIRING",
            "sources": "^TWII 5m · NQ=F 5m Yahoo (IX0001 local 5m not required; ^TWII proxy)",
            "objective": "maximize net_edge_ntd = sum(saved_vs_close) − sum(false_alarm_opp_cost)",
            "pick_rule": pick_rule,
            "fpr_soft_cap": FPR_SOFT_CAP,
            "is_dates": {"start": is_dates[0] if is_dates else None, "end": is_dates[-1] if is_dates else None, "n": len(is_dates)},
            "oos_dates": {"start": oos_dates[0] if oos_dates else None, "end": oos_dates[-1] if oos_dates else None, "n": len(oos_dates)},
            "oos_holds_positive": oos_holds,
            "graduation": "REJECT auto · Research evidence only · not Order-layer auto sell-all",
            "hour2_note": "dual-AND sync + cum cushion + cross-window detach≠repair lookback on focus cores",
        },
        "registered_grid": grid_meta,
        "best_strategy": best,
        "baseline_prior": baseline_row,
        "repair_helped_vs_baseline": (
            None
            if not (best and baseline_row)
            else float(best.get("net_edge_ntd") or 0) > float(baseline_row.get("net_edge_ntd") or 0)
        ),
        "top_strategies": sum_df.head(15).to_dict(orient="records"),
        "top_under_fpr_cap": sum_df[sum_df["false_trigger_rate_on_nonevent"].fillna(1) <= FPR_SOFT_CAP]
        .head(10)
        .to_dict(orient="records"),
        "spotlight_20260714": spotlight_714,
        "spotlight_20260707": spotlight_707,
        "cases": case_payload,
        "day_meta": day_meta_df.to_dict(orient="records"),
        "plot_paths": sorted(str(p) for p in fig_dir.glob("case_*.png")),
    }
    md = render_markdown(payload)
    (out_dir / "report.md").write_text(md, encoding="utf-8")
    (out_dir / "summary.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    return {"payload": payload, "markdown": md}


def specs_by_id(specs: list[StateStrategySpec], sid: str) -> StateStrategySpec:
    return next(s for s in specs if s.strategy_id == sid)


def _save_case_fig(
    path: Path,
    pf: pd.DataFrame,
    best_out: dict[str, Any] | None,
    timeline: list[dict[str, Any]] | None,
) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(10, 4.4))
    x = list(range(len(pf)))
    ax.plot(x, pf["tw_from_open"], label="TW from open", color="#0b6e4f")
    ax.plot(x, pf["nq_from_open"], label="NQ from open", color="#c44e52", alpha=0.85)
    ax.plot(x, pf["cum_spread"], label="cum_spread", color="#2c3e50", ls="--")
    if best_out and best_out.get("detach_poll"):
        hits = pf.index[pf["poll"] == best_out["detach_poll"]].tolist()
        if hits:
            i = list(pf.index).index(hits[0])
            ax.axvline(i, color="#e67e22", ls=":", lw=1.6, label=f"DETACH {best_out['detach_poll']}")
    if best_out and best_out.get("reenter_poll"):
        hits = pf.index[pf["poll"] == best_out["reenter_poll"]].tolist()
        if hits:
            i = list(pf.index).index(hits[0])
            ax.axvline(i, color="#3dd6c6", ls=":", lw=1.6, label=f"REPAIR {best_out['reenter_poll']}")
    # shade REPAIRING spans from timeline if present
    if timeline:
        state_by_poll = {r["poll"]: r["state"] for r in timeline}
        for i, p in enumerate(pf["poll"].tolist()):
            if state_by_poll.get(str(p)) == "REPAIRING":
                ax.axvspan(i - 0.5, i + 0.5, color="#3dd6c6", alpha=0.12)
            elif state_by_poll.get(str(p)) == "DETACH":
                ax.axvspan(i - 0.5, i + 0.5, color="#e67e22", alpha=0.06)
    ax.axhline(0, color="#999", lw=0.7)
    step = max(1, len(pf) // 8)
    ax.set_xticks(x[::step])
    ax.set_xticklabels([pf.iloc[i]["poll"] for i in x[::step]], rotation=45, ha="right")
    ax.set_ylabel("%")
    ax.set_title(f"{pf.attrs.get('date')} · 5m state · PTT={pf.attrs.get('peak_to_trough'):.2f}%")
    ax.legend(fontsize=8)
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=130)
    plt.close(fig)


def render_markdown(payload: dict[str, Any]) -> str:
    m = payload["meta"]
    best = payload.get("best_strategy") or {}
    grid = payload.get("registered_grid") or {}
    base = payload.get("baseline_prior") or {}
    spot = payload.get("spotlight_20260714") or {}
    lines = [
        "# TW↔NQ 5m state machine · SYNC / DETACH / REVERSAL / REPAIRING",
        "",
        f"- Window: `{m.get('date_start')}` → `{m.get('date_end')}` · days={m.get('n_days')} · events≥3%={m.get('n_events3')}",
        f"- Cadence: {m.get('cadence')}",
        f"- Sources: {m.get('sources')}",
        f"- Notional: **{m.get('notional_ntd'):,.0f} NTD**/day (gross; fee haircut sensitivity {grid.get('fee_rt_haircut_pct')}bp/leg noted)",
        f"- Registered grid size: **{grid.get('n_strategies')}** strategies · pick_rule=`{m.get('pick_rule')}` · FPR soft cap={m.get('fpr_soft_cap')}",
        f"- IS: {m.get('is_dates')} · OOS: {m.get('oos_dates')} · OOS holds positive={m.get('oos_holds_positive')}",
        f"- Graduation: {m.get('graduation')}",
        "",
        "## Verdict · Research layer only",
        "",
    ]
    if best:
        lines += [
            f"**Champion:** `{best.get('strategy_id')}` — {best.get('name')}",
            f"- Net edge: **{best.get('net_edge_ntd')} NTD** "
            f"(saved {best.get('sum_saved_vs_close_ntd')} − opp {best.get('sum_opp_cost_ntd')})",
            f"- After fee haircut: {best.get('net_edge_after_fee_ntd')} NTD",
            f"- Trigger rate {best.get('trigger_rate')} · event recall {best.get('event_recall')} · "
            f"FPR non-event {best.get('false_trigger_rate_on_nonevent')}",
            f"- IS net_edge={best.get('is_net_edge_ntd')} · OOS net_edge={best.get('oos_net_edge_ntd')} "
            f"(IS FPR={best.get('is_fpr')} · OOS FPR={best.get('oos_fpr')})",
            "",
        ]
    if base:
        helped = payload.get("repair_helped_vs_baseline")
        lines += [
            f"**Prior baseline** `combo_cum1_win06_reenter_w10` proxy id=`{base.get('strategy_id')}`: "
            f"net_edge={base.get('net_edge_ntd')} · FPR={base.get('false_trigger_rate_on_nonevent')} · "
            f"recall={base.get('event_recall')}",
            f"- Rich repair helped vs baseline: **{helped}**",
            "",
            "解讀：FPR 軟帽 0.30 若無候選達標，champion 仍取最高 net_edge 但**拒絕**採納到 Order layer（下單層）。"
            "樣本 ~60 日 Yahoo 5m；upper-bound vs trough 非現金除非在低點前離場。",
            "",
        ]
    if spot:
        lines += [
            "## Spotlight · 2026-07-14",
            "",
            f"- Strategy: `{spot.get('champ_id')}`",
            f"- DETACH poll: **{spot.get('detach_poll')}**",
            f"- First REPAIR / reenter: **{spot.get('reenter_poll')}**",
            f"- All REPAIRING polls: {spot.get('repair_polls')}",
            f"- State runs: `{spot.get('states_compact')}`",
            "",
        ]
    spot707 = payload.get("spotlight_20260707") or {}
    if spot707:
        lines += [
            "## Spotlight · 2026-07-07 (late-kill trap)",
            "",
            f"- PTT {spot707.get('peak_to_trough_pct')}% · close {spot707.get('close_from_open_pct')}% · trough {spot707.get('trough_from_open_pct')}%",
            f"- DETACH: **{spot707.get('detach_poll')}** · REPAIR/reenter: **{spot707.get('reenter_poll')}**",
            f"- State runs: `{spot707.get('states_compact')}`",
            "",
        ]
    lines += [
        "## Top strategies",
        "",
        "| ID | Net edge | After fee | Saved | Opp | Recall | FPR | IS | OOS | Reenter |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for r in payload.get("top_strategies") or []:
        lines.append(
            f"| `{r.get('strategy_id')}` | {r.get('net_edge_ntd')} | {r.get('net_edge_after_fee_ntd')} | "
            f"{r.get('sum_saved_vs_close_ntd')} | {r.get('sum_opp_cost_ntd')} | {r.get('event_recall')} | "
            f"{r.get('false_trigger_rate_on_nonevent')} | {r.get('is_net_edge_ntd')} | {r.get('oos_net_edge_ntd')} | "
            f"{r.get('reenter')} |"
        )
    lines += ["", "## Top under FPR≤0.30 (if any)", "", "| ID | Net edge | FPR | Recall |", "|---|---:|---:|---:|"]
    capped = payload.get("top_under_fpr_cap") or []
    if not capped:
        lines.append("| — | — | — | no candidate under FPR soft cap |")
    else:
        for r in capped:
            lines.append(
                f"| `{r.get('strategy_id')}` | {r.get('net_edge_ntd')} | "
                f"{r.get('false_trigger_rate_on_nonevent')} | {r.get('event_recall')} |"
            )
    lines += ["", "## Case studies", ""]
    for c in payload.get("cases") or []:
        if not c.get("present"):
            lines.append(f"### {c.get('date')} · missing in 5m panel")
            continue
        lines.append(
            f"### {c['date']} · PTT {c['peak_to_trough_pct']:.2f}% · "
            f"close {c['close_from_open_pct']:.2f}% · trough {c['trough_from_open_pct']:.2f}%"
        )
        lines.append("")
        lines.append("| Strategy | Detach | Repair/Reenter | Exit TW% | Saved NTD | Opp NTD |")
        lines.append("|---|---|---|---:|---:|---:|")
        for sid, o in (c.get("strategy_outcomes") or {}).items():
            lines.append(
                f"| `{sid}` | {o.get('detach_poll')} | {o.get('reenter_poll')} | "
                f"{o.get('detach_tw_from_open')} | {o.get('saved_vs_close_ntd')} | "
                f"{o.get('false_alarm_opp_cost_ntd', 0)} |"
            )
        # 7/14 full state table for champion
        if c["date"] == "2026-07-14":
            champ = (payload.get("spotlight_20260714") or {}).get("champ_id")
            tl = (c.get("timelines") or {}).get(champ) if champ else None
            if tl:
                lines += ["", f"#### State timeline (`{champ}`)", "", "| Poll | State | TW% | NQ% | cum_spread | tw_win | nq_win | corr | ss |", "|---|---|---:|---:|---:|---:|---:|---:|---:|"]
                for row in tl:
                    lines.append(
                        f"| {row.get('poll')} | **{row.get('state')}** | {row.get('tw_from_open')} | "
                        f"{row.get('nq_from_open')} | {row.get('cum_spread')} | {row.get('tw_win')} | "
                        f"{row.get('nq_win')} | {row.get('corr')} | {row.get('same_sign')} |"
                    )
        lines.append("")
    lines += [
        "## Method notes",
        "",
        "- States (priority): DETACH > REPAIRING > REVERSAL > SYNC.",
        "- DETACH latch until REPAIRING clears (when reenter on).",
        "- Rich repair: sync (corr≥τ or same_sign≥τ) AND tw_win>nq_win AND tw_win>0 AND Δcum≥ρ from detach trough.",
        "- Legacy repair baseline: spread_win≥0.25 AND cum > detach_cum+0.3 (prior study).",
        "- PIT: polls use only completed 5m bars ≤ poll time; Yahoo ^TWII proxy for TW.",
        "- Research layer（研究層）evidence only — not Order layer（下單層）auto sell-all.",
        "",
    ]
    return "\n".join(lines) + "\n"
