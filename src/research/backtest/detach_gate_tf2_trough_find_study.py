"""TF-2 · Detach Gate trough-find: does first gap_pulse=IMPROVE lead session trough?

Research topic: detach-gate-trough-find (hypothesis TF-2).
Study-only — does NOT wire Order-layer auto-rebuy.

After first frozen RED (or secondary first spread≤−0.6 in arm window), measure whether
first IMPROVE (or d_spread_30m −→+ flip) precedes the post-event TW trough.

Panel: Yahoo ^TWII × NQ=F 5m (~59d). Reuses enrich_poll_30m + frozen_sell_gate_spec.
"""

from __future__ import annotations

import json
import math
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from research.backtest.us_tw_30m_rolling_risk_gate_study import enrich_poll_30m
from research.backtest.us_tw_rolling_5m_detach_study import (
    build_poll_frame_5m,
    fetch_yahoo_5m_panel,
)
from research.backtest.us_tw_sell_only_live_readiness_study import (
    SellSpec,
    _detach_mask,
    _session_dates_5m,
    frozen_sell_gate_spec,
)

# Buy-near-trough band (enter − trough ≤ this) — mirror sell-near BP
NEAR_TROUGH_BP = 0.35
# Post-event drawdown ≥ this % counts as "meaningful trough"
MEANINGFUL_TROUGH_DD = 0.50
CASE_DAY = "2026-07-14"


def poll_hhmm_to_minutes(poll: str) -> int | None:
    """'HH:MM' → minutes from midnight; None if malformed."""
    try:
        parts = str(poll).strip().split(":")
        if len(parts) != 2:
            return None
        h, m = int(parts[0]), int(parts[1])
        if not (0 <= h <= 23 and 0 <= m <= 59):
            return None
        return h * 60 + m
    except (TypeError, ValueError):
        return None


def lead_minutes(signal_poll: str | None, trough_poll: str | None) -> float | None:
    """Minutes from signal → trough. Positive ⇒ signal before trough."""
    if not signal_poll or not trough_poll:
        return None
    a = poll_hhmm_to_minutes(signal_poll)
    b = poll_hhmm_to_minutes(trough_poll)
    if a is None or b is None:
        return None
    return float(b - a)


def secondary_spread_spec() -> SellSpec:
    """First spread_30m≤−0.6 in arm window (no tw_from_open down filter)."""
    return SellSpec(
        strategy_id="s5_w-0.6_c1_arm0940-1230_nospread_filter",
        panel="5m30",
        win_thr=-0.6,
        confirm_polls=1,
        max_tw_from_open=None,
        arm_after="09:40",
        arm_before="12:30",
        lookback_bars=6,
    )


def first_event_idx(poll: pd.DataFrame, spec: SellSpec) -> int | None:
    det = _detach_mask(poll, spec)
    idxs = list(poll.index[det])
    return int(idxs[0]) if idxs else None


def first_improve_idx(poll: pd.DataFrame, start_idx: int) -> int | None:
    """First gap_pulse=IMPROVE at or after start_idx (inclusive)."""
    if "gap_pulse" not in poll.columns:
        return None
    for i in poll.index:
        if int(i) < int(start_idx):
            continue
        if str(poll.loc[i, "gap_pulse"]) == "IMPROVE":
            return int(i)
    return None


def first_dspread_flip_idx(poll: pd.DataFrame, start_idx: int) -> int | None:
    """First d_spread_30m sign flip −→+ at or after start_idx (uses consecutive bars)."""
    if "d_spread_30m" not in poll.columns:
        return None
    prev: float | None = None
    for i in poll.index:
        if int(i) < int(start_idx):
            # still track prev so flip can use pre-event slope
            v = float(poll.loc[i, "d_spread_30m"])
            prev = v if math.isfinite(v) else prev
            continue
        v = float(poll.loc[i, "d_spread_30m"])
        if not math.isfinite(v):
            continue
        if prev is not None and math.isfinite(prev) and prev < 0 and v > 0:
            return int(i)
        prev = v
    return None


def post_event_trough_idx(poll: pd.DataFrame, event_idx: int) -> int:
    """Min tw_from_open on polls at/after event (inclusive)."""
    sub = poll.loc[poll.index >= event_idx]
    if sub.empty:
        return int(event_idx)
    return int(sub["tw_from_open"].idxmin())


def analyze_day(
    poll: pd.DataFrame,
    *,
    event_mode: str,
    spec: SellSpec,
) -> dict[str, Any]:
    """One session under a given event definition.

    Label (documented): post-event trough = argmin tw_from_open at polls ≥ event.
    Signal: first IMPROVE (primary) / first d_spread −→+ (secondary) ≥ event.
    """
    date_s = str(poll.attrs.get("date") or "")
    close_r = float(poll.attrs.get("close_from_open") or float("nan"))
    session_trough_r = float(poll.attrs.get("trough_from_open") or float("nan"))
    session_trough_poll = str(poll.attrs.get("trough_poll_close") or "")
    ptt = float(poll.attrs.get("peak_to_trough") or float("nan"))

    base: dict[str, Any] = {
        "date": date_s,
        "event_mode": event_mode,
        "event_spec_id": spec.strategy_id,
        "has_event": False,
        "event_poll": None,
        "event_tw_from_open": None,
        "event_spread_30m": None,
        "post_event_trough_poll": None,
        "post_event_trough_pct": None,
        "post_event_dd_pct": None,
        "meaningful_trough": False,
        "session_trough_poll": session_trough_poll or None,
        "session_trough_pct": round(session_trough_r, 4) if math.isfinite(session_trough_r) else None,
        "close_from_open_pct": round(close_r, 4) if math.isfinite(close_r) else None,
        "peak_to_trough_pct": round(ptt, 4) if math.isfinite(ptt) else None,
        "improve_poll": None,
        "improve_tw_from_open": None,
        "improve_lead_min": None,
        "improve_before_trough": None,
        "improve_headroom_vs_trough_pct": None,
        "improve_buy_near_trough": None,
        "improve_after_trough": None,
        "flip_poll": None,
        "flip_tw_from_open": None,
        "flip_lead_min": None,
        "flip_before_trough": None,
        "flip_headroom_vs_trough_pct": None,
        "flip_buy_near_trough": None,
        "flip_after_trough": None,
    }

    e_idx = first_event_idx(poll, spec)
    if e_idx is None:
        return base

    er = poll.loc[e_idx]
    event_tw = float(er["tw_from_open"])
    t_idx = post_event_trough_idx(poll, e_idx)
    tr = poll.loc[t_idx]
    trough_tw = float(tr["tw_from_open"])
    trough_poll = str(tr["poll"])
    dd = event_tw - trough_tw  # positive if market falls further after event
    meaningful = bool(dd >= MEANINGFUL_TROUGH_DD)

    base.update(
        {
            "has_event": True,
            "event_poll": str(er["poll"]),
            "event_tw_from_open": round(event_tw, 4),
            "event_spread_30m": round(float(er["spread_30m"]), 4),
            "post_event_trough_poll": trough_poll,
            "post_event_trough_pct": round(trough_tw, 4),
            "post_event_dd_pct": round(dd, 4),
            "meaningful_trough": meaningful,
        }
    )

    def _signal_block(sig_idx: int | None, prefix: str) -> dict[str, Any]:
        if sig_idx is None:
            return {
                f"{prefix}_poll": None,
                f"{prefix}_tw_from_open": None,
                f"{prefix}_lead_min": None,
                f"{prefix}_before_trough": None,
                f"{prefix}_headroom_vs_trough_pct": None,
                f"{prefix}_buy_near_trough": None,
                f"{prefix}_after_trough": None,
            }
        sr = poll.loc[sig_idx]
        sig_poll = str(sr["poll"])
        sig_tw = float(sr["tw_from_open"])
        lead = lead_minutes(sig_poll, trough_poll)
        # same-bar (lead==0) counts as before / at trough
        before = bool(lead is not None and lead >= 0)
        after = bool(lead is not None and lead < 0)
        headroom = sig_tw - trough_tw
        near = bool(before and 0.0 <= headroom <= NEAR_TROUGH_BP)
        return {
            f"{prefix}_poll": sig_poll,
            f"{prefix}_tw_from_open": round(sig_tw, 4),
            f"{prefix}_lead_min": round(lead, 1) if lead is not None else None,
            f"{prefix}_before_trough": before,
            f"{prefix}_headroom_vs_trough_pct": round(headroom, 4),
            f"{prefix}_buy_near_trough": near,
            f"{prefix}_after_trough": after,
        }

    base.update(_signal_block(first_improve_idx(poll, e_idx), "improve"))
    base.update(_signal_block(first_dspread_flip_idx(poll, e_idx), "flip"))
    return base


def aggregate_tf2(day_rows: list[dict[str, Any]], *, signal: str = "improve") -> dict[str, Any]:
    """Aggregate lead / precision / FPR for one signal family."""
    prefix = signal  # "improve" | "flip"
    events = [r for r in day_rows if r.get("has_event")]
    n_days = len(day_rows)
    n_events = len(events)
    with_sig = [r for r in events if r.get(f"{prefix}_poll")]
    meaningful = [r for r in events if r.get("meaningful_trough")]
    no_mt = [r for r in events if not r.get("meaningful_trough")]

    leads = [
        float(r[f"{prefix}_lead_min"])
        for r in with_sig
        if r.get(f"{prefix}_lead_min") is not None
        and r.get("meaningful_trough")
        and r.get(f"{prefix}_before_trough")
    ]
    all_leads = [
        float(r[f"{prefix}_lead_min"])
        for r in with_sig
        if r.get(f"{prefix}_lead_min") is not None and r.get("meaningful_trough")
    ]
    before = [r for r in with_sig if r.get("meaningful_trough") and r.get(f"{prefix}_before_trough")]
    after = [r for r in with_sig if r.get("meaningful_trough") and r.get(f"{prefix}_after_trough")]
    # Precision-like: among meaningful-trough event days with signal, fraction before trough
    n_mt_sig = len([r for r in with_sig if r.get("meaningful_trough")])
    precision_before = (len(before) / n_mt_sig) if n_mt_sig else float("nan")

    heads = [
        float(r[f"{prefix}_headroom_vs_trough_pct"])
        for r in before
        if r.get(f"{prefix}_headroom_vs_trough_pct") is not None
    ]
    near_n = sum(1 for r in before if r.get(f"{prefix}_buy_near_trough"))
    buy_near_rate = (near_n / len(before)) if before else float("nan")

    # FPR: IMPROVE on no-meaningful-trough days / or signal after trough on meaningful days
    fpr_no_mt = (
        sum(1 for r in no_mt if r.get(f"{prefix}_poll")) / len(no_mt) if no_mt else float("nan")
    )
    fpr_after = (len(after) / n_mt_sig) if n_mt_sig else float("nan")

    return {
        "signal": signal,
        "n_panel_days": n_days,
        "n_event_days": n_events,
        "n_with_signal": len(with_sig),
        "n_meaningful_trough": len(meaningful),
        "n_mt_with_signal": n_mt_sig,
        "median_lead_min_before": round(float(np.median(leads)), 1) if leads else None,
        "mean_lead_min_before": round(float(np.mean(leads)), 1) if leads else None,
        "median_lead_min_all_mt": round(float(np.median(all_leads)), 1) if all_leads else None,
        "fraction_signal_before_trough": round(precision_before, 4) if math.isfinite(precision_before) else None,
        "median_headroom_vs_trough_pct": round(float(np.median(heads)), 4) if heads else None,
        "buy_near_trough_rate": round(buy_near_rate, 4) if math.isfinite(buy_near_rate) else None,
        "fpr_improve_no_meaningful_trough": round(fpr_no_mt, 4) if math.isfinite(fpr_no_mt) else None,
        "fpr_signal_after_trough": round(fpr_after, 4) if math.isfinite(fpr_after) else None,
        "n_before": len(before),
        "n_after": len(after),
    }


class _PollBundle(dict):
    """dict[str, DataFrame] with panel provenance for VFP meta."""

    _detach_panel_meta: dict[str, Any]

    def __init__(self, data: dict[str, pd.DataFrame], meta: dict[str, Any]):
        super().__init__(data)
        self._detach_panel_meta = meta


def load_day_polls(*, date_end: str | date | None = None) -> dict[str, pd.DataFrame]:
    end = date.fromisoformat(str(date_end)) if date_end else date.today()
    tw = fetch_yahoo_5m_panel("^TWII", end)
    nq = fetch_yahoo_5m_panel("NQ=F", end)
    dates = _session_dates_5m(tw, nq)
    polls: dict[str, pd.DataFrame] = {}
    for d in dates:
        pf = build_poll_frame_5m(d, tw, nq, window_min=30)
        if pf is None or len(pf) < 7:
            continue
        polls[d] = enrich_poll_30m(pf)
    panel_meta = {
        "tw_source": getattr(tw, "attrs", {}).get("panel_source"),
        "nq_source": getattr(nq, "attrs", {}).get("panel_source"),
        "tw_proxy": (
            "IX0001 (^TWII) — FinMind index K N/A; "
            "0050 FinMind is longer liquid proxy but not auto-swapped"
        ),
    }
    return _PollBundle(polls, panel_meta)


def run_event_panel(
    polls: dict[str, pd.DataFrame],
    *,
    event_mode: str,
    spec: SellSpec,
) -> list[dict[str, Any]]:
    return [analyze_day(polls[d], event_mode=event_mode, spec=spec) for d in sorted(polls)]


def render_markdown(payload: dict[str, Any]) -> str:
    meta = payload.get("meta") or {}
    primary = payload.get("primary_frozen_red") or {}
    secondary = payload.get("secondary_spread_arm") or {}
    p_imp = (primary.get("agg_improve") or {})
    p_flip = (primary.get("agg_flip") or {})
    s_imp = (secondary.get("agg_improve") or {})
    case = payload.get("case_20260714") or {}
    lines = [
        "# TF-2 · gap_pulse IMPROVE → session trough lead",
        "",
        f"- Topic: `detach-gate-trough-find` · hypothesis **TF-2**",
        f"- Layer: Research only · **no** Order-layer auto-rebuy",
        f"- Panel: `{meta.get('tw_source') or '^TWII'}` × `{meta.get('nq_source') or 'NQ=F'}` 5m · "
        f"{meta.get('window')} · n={meta.get('n_days')}",
        f"- TW choice: `{meta.get('tw_proxy') or 'IX0001 (^TWII)'}`",
        f"- Event primary: frozen RED `{meta.get('frozen_spec_id')}`",
        f"- Event secondary: first `spread_30m≤−0.6` arm 09:40–12:30 (no open-down filter)",
        f"- Label: **post-event trough** = argmin `tw_from_open` on polls ≥ event (inclusive)",
        f"- Signal primary: first `gap_pulse=IMPROVE` ≥ event",
        f"- Signal secondary: first `d_spread_30m` sign flip −→+ ≥ event",
        f"- Meaningful trough: post-event DD ≥ {MEANINGFUL_TROUGH_DD}%",
        f"- Near-trough band: enter − trough ≤ {NEAR_TROUGH_BP} pp",
        "",
        "## Primary · frozen RED",
        "",
        f"| metric | IMPROVE | d_spread flip |",
        f"|--------|---------|---------------|",
        f"| n event days | {p_imp.get('n_event_days')} | {p_flip.get('n_event_days')} |",
        f"| n meaningful trough | {p_imp.get('n_meaningful_trough')} | {p_flip.get('n_meaningful_trough')} |",
        f"| n with signal (MT) | {p_imp.get('n_mt_with_signal')} | {p_flip.get('n_mt_with_signal')} |",
        f"| median lead min (before) | {p_imp.get('median_lead_min_before')} | {p_flip.get('median_lead_min_before')} |",
        f"| fraction before trough | {p_imp.get('fraction_signal_before_trough')} | {p_flip.get('fraction_signal_before_trough')} |",
        f"| median headroom vs trough % | {p_imp.get('median_headroom_vs_trough_pct')} | {p_flip.get('median_headroom_vs_trough_pct')} |",
        f"| buy-near-trough rate | {p_imp.get('buy_near_trough_rate')} | {p_flip.get('buy_near_trough_rate')} |",
        f"| FPR (sig on no-MT days) | {p_imp.get('fpr_improve_no_meaningful_trough')} | {p_flip.get('fpr_improve_no_meaningful_trough')} |",
        f"| FPR (sig after trough) | {p_imp.get('fpr_signal_after_trough')} | {p_flip.get('fpr_signal_after_trough')} |",
        "",
        "## Secondary · spread≤−0.6 arm (wider sample)",
        "",
        f"| metric | IMPROVE |",
        f"|--------|---------|",
        f"| n event days | {s_imp.get('n_event_days')} |",
        f"| n meaningful trough | {s_imp.get('n_meaningful_trough')} |",
        f"| n with signal (MT) | {s_imp.get('n_mt_with_signal')} |",
        f"| median lead min (before) | {s_imp.get('median_lead_min_before')} |",
        f"| fraction before trough | {s_imp.get('fraction_signal_before_trough')} |",
        f"| median headroom vs trough % | {s_imp.get('median_headroom_vs_trough_pct')} |",
        f"| buy-near-trough rate | {s_imp.get('buy_near_trough_rate')} |",
        f"| FPR (sig on no-MT days) | {s_imp.get('fpr_improve_no_meaningful_trough')} |",
        f"| FPR (sig after trough) | {s_imp.get('fpr_signal_after_trough')} |",
        "",
        f"## Case · {CASE_DAY}",
        "",
    ]
    if not case:
        lines.append(f"- **Not in panel** (Yahoo 5m window may exclude {CASE_DAY}).")
    else:
        c_red = case.get("frozen_red") or {}
        lines.extend(
            [
                f"- Event RED @ **{c_red.get('event_poll')}** · TW {c_red.get('event_tw_from_open')}% · "
                f"spread {c_red.get('event_spread_30m')}",
                f"- Post-RED trough @ **{c_red.get('post_event_trough_poll')}** · "
                f"TW {c_red.get('post_event_trough_pct')}% · DD {c_red.get('post_event_dd_pct')}%",
                f"- First IMPROVE @ **{c_red.get('improve_poll')}** · lead {c_red.get('improve_lead_min')} min · "
                f"before={c_red.get('improve_before_trough')} · headroom {c_red.get('improve_headroom_vs_trough_pct')}%",
                f"- First d_spread flip @ **{c_red.get('flip_poll')}** · lead {c_red.get('flip_lead_min')} min",
            ]
        )
    lines.extend(
        [
            "",
            "## Caveats",
            "",
            "- Yahoo 5m history ~59 trading days; n event / meaningful-trough is small.",
            "- Post-event trough ≠ full-session trough when low prints before RED.",
            "- IMPROVE is a 5m Δspread pulse — high cadence ⇒ elevated FPR on quiet days.",
            "- Research hypothesis only; **not** Strategy/Order 採納.",
            "",
        ]
    )
    return "\n".join(lines)


def run_study(*, date_end: str | date | None = None, out_dir: Path | None = None) -> dict[str, Any]:
    polls = load_day_polls(date_end=date_end)
    frozen = frozen_sell_gate_spec()
    secondary = secondary_spread_spec()

    primary_rows = run_event_panel(polls, event_mode="frozen_red", spec=frozen)
    secondary_rows = run_event_panel(polls, event_mode="spread_arm", spec=secondary)

    primary_block = {
        "agg_improve": aggregate_tf2(primary_rows, signal="improve"),
        "agg_flip": aggregate_tf2(primary_rows, signal="flip"),
        "days": primary_rows,
    }
    secondary_block = {
        "agg_improve": aggregate_tf2(secondary_rows, signal="improve"),
        "agg_flip": aggregate_tf2(secondary_rows, signal="flip"),
        "days": secondary_rows,
    }

    case: dict[str, Any] = {}
    if CASE_DAY in polls:
        case = {
            "frozen_red": analyze_day(polls[CASE_DAY], event_mode="frozen_red", spec=frozen),
            "spread_arm": analyze_day(polls[CASE_DAY], event_mode="spread_arm", spec=secondary),
        }

    panel_meta = getattr(polls, "_detach_panel_meta", {}) or {}
    meta = {
        "n_days": len(polls),
        "window": f"{min(polls)} → {max(polls)}" if polls else None,
        "frozen_spec_id": frozen.strategy_id,
        "secondary_spec_id": secondary.strategy_id,
        "label": "post_event_trough_tw_from_open",
        "meaningful_trough_dd_pct": MEANINGFUL_TROUGH_DD,
        "near_trough_bp": NEAR_TROUGH_BP,
        "topic": "detach-gate-trough-find",
        "hypothesis": "TF-2",
        "layer": "research",
        "order_rebuy": False,
        "primary_case_day": CASE_DAY,
        "case_in_panel": CASE_DAY in polls,
        **panel_meta,
    }

    payload: dict[str, Any] = {
        "meta": meta,
        "primary_frozen_red": {
            "agg_improve": primary_block["agg_improve"],
            "agg_flip": primary_block["agg_flip"],
            "days_with_event": [r for r in primary_rows if r.get("has_event")],
        },
        "secondary_spread_arm": {
            "agg_improve": secondary_block["agg_improve"],
            "agg_flip": secondary_block["agg_flip"],
            "days_with_event": [r for r in secondary_rows if r.get("has_event")],
        },
        "case_20260714": case,
    }

    markdown = render_markdown(payload)
    if out_dir is not None:
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "tf2_summary.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        (out_dir / "README.md").write_text(markdown, encoding="utf-8")
        # slim daily table for audit
        pd.DataFrame(primary_rows).to_csv(out_dir / "primary_frozen_red_days.csv", index=False)
        pd.DataFrame(secondary_rows).to_csv(out_dir / "secondary_spread_arm_days.csv", index=False)

    return {"payload": payload, "markdown": markdown, "polls": polls}
