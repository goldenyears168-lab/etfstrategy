"""ABC v3+f1 · separate take-profit / stop-loss · WMA gap + RV · hold 5d.

TP and SL use independent parameters · first chronological trigger wins · else hold to cap.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from research.backtest.archive.c18acc_kinematic_gap_rv3_study import (
    GapRv3Params,
    _gap_ok,
    _rv3_ok,
    _pct_hit_target,
    _summarize_portfolio,
)
from research.backtest.w3_rv_hl_winner_profile import _mean

SCHEMA = "abc_v3_f1_kinematic_tp_sl-v1"

Leg = dict[str, Any]
Snap = dict[str, Any]
RvMode = Literal["none", "drv", "poll_drop", "drv_and_poll", "drv_or_poll"]


@dataclass(frozen=True)
class TakeProfitParams:
    tp_mode: Literal["gap_rv", "rv_only", "trail_giveback"] = "gap_rv"
    gap_logic: Literal["or", "w20_w5_only"] = "or"
    gw5_min: float = 6.0
    gw3_min: float = 2.0
    gap_expand_min: float = 0.0
    rv_leg: Literal["w3", "w5"] = "w3"
    rv_mode: RvMode = "poll_drop"
    drv_min: float = 0.35
    min_ret_pct: float = 8.0
    trail_giveback_pp: float = 2.0
    consecutive: int = 1

    @property
    def variant_id(self) -> str:
        if self.tp_mode == "trail_giveback":
            return (
                f"TP_trail_gb{self.trail_giveback_pp:g}_r{int(self.min_ret_pct)}"
                f"_c{self.consecutive}"
            )
        if self.tp_mode == "rv_only":
            return (
                f"TP_rv{self.rv_leg}{self.rv_mode}_d{int(self.drv_min*100)}"
                f"_r{int(self.min_ret_pct)}_c{self.consecutive}"
            )
        return (
            f"TP_{self.gap_logic}_g5{int(self.gw5_min)}_g3{self.gw3_min:g}"
            f"_rv{self.rv_leg}{self.rv_mode}_d{int(self.drv_min*100)}"
            f"_r{int(self.min_ret_pct)}_c{self.consecutive}"
        )


@dataclass(frozen=True)
class StopLossParams:
    sl_mode: Literal["kinematic", "underwater"] = "kinematic"
    dmv_w5_min: float = 0.35
    dmv_w3_min: float = 0.0
    drv_w5_min: float = 0.25
    drv_w3_min: float = 0.0
    rv_leg: Literal["w3", "w5", "both"] = "w5"
    rv_mode: RvMode = "drv"
    require_w5_lagging: bool = False
    require_w3_lagging: bool = False
    gap_w20_w5_collapse: float = 0.0
    arm_ret_max_pct: float = 5.0
    consecutive: int = 1

    @property
    def variant_id(self) -> str:
        lag = ""
        if self.require_w5_lagging:
            lag += "w5L"
        if self.require_w3_lagging:
            lag += "w3L"
        return (
            f"SL_{self.sl_mode}_dmv5{int(self.dmv_w5_min*100)}_drv5{int(self.drv_w5_min*100)}"
            f"_rv{self.rv_leg}{self.rv_mode}_{lag or 'x'}"
            f"_arm{int(self.arm_ret_max_pct)}_c{self.consecutive}"
        )


@dataclass(frozen=True)
class TpSlCombo:
    tp: TakeProfitParams
    sl: StopLossParams

    @property
    def variant_id(self) -> str:
        return f"{self.tp.variant_id}__{self.sl.variant_id}"


def _gap_params_from_tp(tp: TakeProfitParams) -> GapRv3Params:
    return GapRv3Params(
        gap_logic="or" if tp.gap_logic == "or" else "w20_w5_only",
        gw5_min=tp.gw5_min,
        gw3_min=tp.gw3_min,
        gap_expand_min=tp.gap_expand_min,
        rv3_mode="none",
        drv3_min=0.0,
        dmv3_min=0.0,
        min_ret_pct=0.0,
        require_pullback=False,
        dist_min=0.0,
        consecutive=1,
    )


def _rv_snap_ok(
    snap: Snap,
    prev: Snap | None,
    entry: Snap,
    *,
    leg: Literal["w3", "w5"],
    mode: RvMode,
    drv_min: float,
) -> bool:
    if mode == "none":
        return True
    key_drv = f"drv_{leg}"
    key_rv = f"{leg}_rv"
    drv = snap.get(key_drv)
    rv = snap.get(key_rv)
    prev_rv = prev.get(key_rv) if prev else None
    ent_rv = entry.get(key_rv)
    poll_drop = (
        rv is not None
        and prev_rv is not None
        and float(rv) < float(prev_rv) - 1e-9
    )
    drv_bad = drv is not None and float(drv) <= -drv_min + 1e-9
    if mode == "drv":
        return drv_bad
    if mode == "poll_drop":
        return poll_drop
    if mode == "drv_and_poll":
        return drv_bad and poll_drop
    if mode == "drv_or_poll":
        return drv_bad or poll_drop
    if mode == "below_entry":
        return (
            rv is not None
            and ent_rv is not None
            and float(rv) < float(ent_rv) - 1e-9
        )
    return False


def _tp_snap_ok(
    snap: Snap,
    prev: Snap | None,
    entry: Snap,
    tp: TakeProfitParams,
    *,
    running_peak_ret: float,
) -> bool:
    ret = float(snap.get("ret_from_entry_pct") or 0)
    if ret < tp.min_ret_pct - 1e-9:
        return False
    if tp.tp_mode == "trail_giveback":
        return ret <= running_peak_ret - tp.trail_giveback_pp + 1e-9
    if tp.tp_mode == "rv_only":
        return _rv_snap_ok(
            snap, prev, entry, leg=tp.rv_leg, mode=tp.rv_mode, drv_min=tp.drv_min
        )
    gp = _gap_params_from_tp(tp)
    if not _gap_ok(snap, entry, gp):
        return False
    return _rv_snap_ok(
        snap, prev, entry, leg=tp.rv_leg, mode=tp.rv_mode, drv_min=tp.drv_min
    )


def _sl_snap_ok(snap: Snap, prev: Snap | None, entry: Snap, sl: StopLossParams) -> bool:
    ret = float(snap.get("ret_from_entry_pct") or 0)
    if sl.sl_mode == "underwater":
        if ret > 0.0 + 1e-9:
            return False
    elif ret > sl.arm_ret_max_pct + 1e-9:
        return False
    d5 = snap.get("dmv_w5")
    d3 = snap.get("dmv_w3")
    if sl.dmv_w5_min > 0 and (d5 is None or float(d5) > -sl.dmv_w5_min + 1e-9):
        return False
    if sl.dmv_w3_min > 0 and (d3 is None or float(d3) > -sl.dmv_w3_min + 1e-9):
        return False
    if sl.require_w5_lagging and str(snap.get("w5_quad") or "").lower() != "lagging":
        return False
    if sl.require_w3_lagging and str(snap.get("w3_quad") or "").lower() != "lagging":
        return False
    if sl.gap_w20_w5_collapse > 0:
        gap = snap.get("gap_mv_w20_w5")
        ent = entry.get("gap_mv_w20_w5")
        if gap is None or ent is None:
            return False
        if float(gap) > float(ent) - sl.gap_w20_w5_collapse:
            return False
    legs: tuple[Literal["w3", "w5"], ...]
    if sl.rv_leg == "both":
        legs = ("w3", "w5")
    else:
        legs = (sl.rv_leg,)
    for leg in legs:
        if not _rv_snap_ok(
            snap,
            prev,
            entry,
            leg=leg,
            mode=sl.rv_mode,
            drv_min=sl.drv_w5_min if leg == "w5" else sl.drv_w3_min or sl.drv_w5_min,
        ):
            return False
    return True


def evaluate_leg_tp_sl(leg: Leg, combo: TpSlCombo) -> dict[str, Any]:
    timeline = leg.get("timeline") or []
    if len(timeline) < 2:
        hold = float(leg.get("return_pct") or 0)
        return {"fired": False, "exit_ret_pct": hold, "source": "hold"}
    entry = leg.get("entry_t0") or timeline[0]
    peak_i = int(leg.get("peak_frame_idx") or 0)
    peak_ret = float(leg.get("peak_ret_pct") or 0)
    hold_ret = float(leg.get("return_pct") or 0)

    tp_streak = sl_streak = 0
    running_peak_ret = float(entry.get("ret_from_entry_pct") or 0)
    for i in range(1, len(timeline)):
        prev = timeline[i - 1]
        snap = timeline[i]
        ret_i = float(snap.get("ret_from_entry_pct") or 0)
        running_peak_ret = max(running_peak_ret, ret_i)
        sl_ok = _sl_snap_ok(snap, prev, entry, combo.sl)
        tp_ok = _tp_snap_ok(
            snap, prev, entry, combo.tp, running_peak_ret=running_peak_ret
        )
        sl_streak = sl_streak + 1 if sl_ok else 0
        tp_streak = tp_streak + 1 if tp_ok else 0
        if sl_streak >= combo.sl.consecutive:
            exit_ret = float(snap.get("ret_from_entry_pct") or 0)
            return {
                "fired": True,
                "source": "SL",
                "trigger_idx": i,
                "exit_ret_pct": round(exit_ret, 3),
                "vs_peak_pp": round(exit_ret - peak_ret, 3),
                "capture_pct": round(100.0 * exit_ret / peak_ret, 1) if peak_ret > 0.5 else None,
                "at_or_before_peak": i <= peak_i,
            }
        if tp_streak >= combo.tp.consecutive:
            exit_ret = float(snap.get("ret_from_entry_pct") or 0)
            return {
                "fired": True,
                "source": "TP",
                "trigger_idx": i,
                "exit_ret_pct": round(exit_ret, 3),
                "vs_peak_pp": round(exit_ret - peak_ret, 3),
                "capture_pct": round(100.0 * exit_ret / peak_ret, 1) if peak_ret > 0.5 else None,
                "at_or_before_peak": i <= peak_i,
            }
    return {
        "fired": False,
        "source": "hold",
        "exit_ret_pct": hold_ret,
        "vs_peak_pp": round(hold_ret - peak_ret, 3),
        "capture_pct": round(100.0 * hold_ret / peak_ret, 1) if peak_ret > 0.5 else None,
    }


def _summarize_tp_sl(
    legs: list[Leg],
    results: list[dict[str, Any]],
    *,
    variant_id: str,
    combo: TpSlCombo | None = None,
) -> dict[str, Any]:
    hold_mean = _mean([float(l["return_pct"]) for l in legs])
    all_rets = [float(r["exit_ret_pct"]) for r in results]
    fired = [r for r in results if r.get("fired")]
    tp_f = [r for r in fired if r.get("source") == "TP"]
    sl_f = [r for r in fired if r.get("source") == "SL"]
    summary: dict[str, Any] = {
        "variant_id": variant_id,
        "n_legs": len(legs),
        "n_fired": len(fired),
        "fire_rate_pct": round(100.0 * len(fired) / len(legs), 1) if legs else 0.0,
        "n_tp": len(tp_f),
        "n_sl": len(sl_f),
        "tp_rate_pct": round(100.0 * len(tp_f) / len(legs), 1) if legs else 0.0,
        "sl_rate_pct": round(100.0 * len(sl_f) / len(legs), 1) if legs else 0.0,
        "mean_all_legs_ret_pct": _mean(all_rets),
        "delta_vs_hold_pp": round(_mean(all_rets) - hold_mean, 3),
        "hold_baseline_mean_pct": hold_mean,
        "pct_legs_hit_8": _pct_hit_target([{"return_pct": x} for x in all_rets], 8.0),
        "pct_legs_hit_10": _pct_hit_target([{"return_pct": x} for x in all_rets], 10.0),
    }
    if tp_f:
        summary["mean_exit_on_tp_pct"] = _mean([float(r["exit_ret_pct"]) for r in tp_f])
    if sl_f:
        summary["mean_exit_on_sl_pct"] = _mean([float(r["exit_ret_pct"]) for r in sl_f])
    if combo is not None:
        summary["tp_params"] = combo.tp.variant_id
        summary["sl_params"] = combo.sl.variant_id
    return summary


def _tp_grid() -> list[TakeProfitParams]:
    out: list[TakeProfitParams] = []
    for gw5 in (4.0, 5.0, 6.0, 7.0):
        for gw3 in (1.0, 1.5, 2.0):
            for rv_mode in ("poll_drop", "drv_or_poll"):
                for min_ret in (3.0, 4.0, 5.0, 6.0, 8.0, 10.0):
                    out.append(
                        TakeProfitParams(
                            tp_mode="gap_rv",
                            gw5_min=gw5,
                            gw3_min=gw3,
                            rv_leg="w3",
                            rv_mode=rv_mode,
                            drv_min=0.25,
                            min_ret_pct=min_ret,
                            consecutive=1,
                        )
                    )
    for rv_mode in ("poll_drop", "drv_or_poll"):
        for min_ret in (4.0, 6.0, 8.0, 10.0):
            for rv_leg in ("w3", "w5"):
                out.append(
                    TakeProfitParams(
                        tp_mode="rv_only",
                        rv_leg=rv_leg,
                        rv_mode=rv_mode,
                        drv_min=0.25,
                        min_ret_pct=min_ret,
                        consecutive=1,
                    )
                )
    for gb in (1.0, 1.5, 2.0, 2.5, 3.0):
        for min_ret in (6.0, 8.0, 10.0):
            out.append(
                TakeProfitParams(
                    tp_mode="trail_giveback",
                    trail_giveback_pp=gb,
                    min_ret_pct=min_ret,
                    consecutive=1,
                )
            )
    return out


def _sl_grid() -> list[StopLossParams]:
    out: list[StopLossParams] = []
    # Prefer underwater-only SL; kinematic SL rarely helps on ABC.
    for dmv5 in (0.35, 0.45):
        for drv5 in (0.25, 0.35):
            for rv_mode in ("drv", "drv_or_poll"):
                out.append(
                    StopLossParams(
                        sl_mode="underwater",
                        dmv_w5_min=dmv5,
                        drv_w5_min=drv5,
                        rv_leg="w5",
                        rv_mode=rv_mode,
                        consecutive=2,
                    )
                )
    out.append(
        StopLossParams(
            sl_mode="kinematic",
            dmv_w5_min=0.45,
            drv_w5_min=0.35,
            arm_ret_max_pct=0.0,
            consecutive=2,
        )
    )
    return out


NEVER_SL = StopLossParams(
    dmv_w5_min=0.0,
    drv_w5_min=99.0,
    arm_ret_max_pct=-999.0,
    consecutive=99,
)
NEVER_TP = TakeProfitParams(
    gw5_min=999.0,
    gw3_min=999.0,
    min_ret_pct=999.0,
    consecutive=99,
)


def _top_tp_sl_pairs(
    legs: list[Leg],
    *,
    top_tp: int = 12,
    top_sl: int = 12,
) -> list[TpSlCombo]:
    tp_rows: list[tuple[float, TakeProfitParams]] = []
    for tp in _tp_grid():
        results = [
            evaluate_leg_tp_sl(leg, TpSlCombo(tp=tp, sl=NEVER_SL)) for leg in legs
        ]
        m = _mean([float(r["exit_ret_pct"]) for r in results])
        tp_rows.append((m, tp))
    tp_rows.sort(key=lambda x: -x[0])

    sl_rows: list[tuple[float, StopLossParams]] = []
    for sl in _sl_grid():
        results = [
            evaluate_leg_tp_sl(leg, TpSlCombo(tp=NEVER_TP, sl=sl)) for leg in legs
        ]
        m = _mean([float(r["exit_ret_pct"]) for r in results])
        sl_rows.append((m, sl))
    sl_rows.sort(key=lambda x: -x[0])

    combos: list[TpSlCombo] = []
    seen: set[str] = set()
    for _, tp in tp_rows[:top_tp]:
        for _, sl in sl_rows[:top_sl]:
            c = TpSlCombo(tp=tp, sl=sl)
            if c.variant_id not in seen:
                seen.add(c.variant_id)
                combos.append(c)
    return combos


def run_abc_f1_tp_sl_study(
    legs: list[Leg],
    *,
    target_pct: float = 8.0,
    hold_days: int = 5,
) -> dict[str, Any]:
    if not legs:
        raise ValueError("no legs")
    hold_only = _summarize_tp_sl(
        legs,
        [{"fired": False, "exit_ret_pct": float(l["return_pct"])} for l in legs],
        variant_id="hold_only",
    )

    tp_only_rows: list[dict[str, Any]] = []
    for tp in _tp_grid():
        results = [
            evaluate_leg_tp_sl(leg, TpSlCombo(tp=tp, sl=NEVER_SL)) for leg in legs
        ]
        tp_only_rows.append(
            _summarize_tp_sl(legs, results, variant_id=tp.variant_id, combo=TpSlCombo(tp=tp, sl=NEVER_SL))
        )
    tp_only_rows.sort(key=lambda s: -(float(s.get("mean_all_legs_ret_pct") or 0)))
    best_tp_only = tp_only_rows[0] if tp_only_rows else {}

    combos = _top_tp_sl_pairs(legs)
    summaries: list[dict[str, Any]] = []
    for combo in combos:
        results = [evaluate_leg_tp_sl(leg, combo) for leg in legs]
        summaries.append(
            _summarize_tp_sl(legs, results, variant_id=combo.variant_id, combo=combo)
        )
    summaries.sort(key=lambda s: -(float(s.get("mean_all_legs_ret_pct") or 0)))

    # Also test champion-like fixed combos
    fixed = [
        TpSlCombo(
            tp=TakeProfitParams(
                tp_mode="rv_only",
                rv_leg="w3",
                rv_mode="poll_drop",
                min_ret_pct=6.0,
            ),
            sl=StopLossParams(sl_mode="underwater", dmv_w5_min=0.35, drv_w5_min=0.25),
        ),
        TpSlCombo(
            tp=TakeProfitParams(
                tp_mode="trail_giveback",
                trail_giveback_pp=2.0,
                min_ret_pct=8.0,
            ),
            sl=StopLossParams(sl_mode="underwater", dmv_w5_min=0.35, drv_w5_min=0.25),
        ),
        TpSlCombo(
            tp=TakeProfitParams(gw5_min=6.0, gw3_min=2.0, min_ret_pct=8.0, rv_mode="poll_drop"),
            sl=StopLossParams(
                sl_mode="kinematic",
                dmv_w5_min=0.35,
                drv_w5_min=0.25,
                arm_ret_max_pct=2.0,
                require_w5_lagging=False,
            ),
        ),
    ]
    for combo in fixed:
        if combo.variant_id not in {s["variant_id"] for s in summaries}:
            results = [evaluate_leg_tp_sl(leg, combo) for leg in legs]
            summaries.append(
                _summarize_tp_sl(legs, results, variant_id=combo.variant_id, combo=combo)
            )
    summaries.sort(key=lambda s: -(float(s.get("mean_all_legs_ret_pct") or 0)))

    best = summaries[0] if summaries else {}
    if float(best_tp_only.get("mean_all_legs_ret_pct") or 0) > float(
        best.get("mean_all_legs_ret_pct") or 0
    ):
        best = {**best_tp_only, "note": "tp_only_beats_tp_sl_combo"}
    hit_target = [s for s in summaries + tp_only_rows if float(s.get("mean_all_legs_ret_pct") or 0) >= target_pct]

    return {
        "schema": SCHEMA,
        "hold_days": hold_days,
        "n_legs": len(legs),
        "target_pct": target_pct,
        "hold_only": hold_only,
        "best_tp_only": best_tp_only,
        "n_combos_tested": len(summaries),
        "best_combo": best,
        "combos_at_or_above_target": hit_target[:10],
        "top_combos": summaries[:20],
        "top_tp_only": tp_only_rows[:10],
    }


def save_legs_cache(legs: list[Leg], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"schema": "abc_f1_kinematic_legs-v1", "legs": legs}, ensure_ascii=False),
        encoding="utf-8",
    )


def load_legs_cache(path: Path) -> list[Leg]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload.get("legs") or []


def render_abc_f1_tp_sl_study_md(payload: dict[str, Any]) -> str:
    target = float(payload.get("target_pct") or 8.0)
    hold = payload.get("hold_only") or {}
    best = payload.get("best_combo") or {}
    best_tp = payload.get("best_tp_only") or {}
    top = payload.get("top_combos") or []
    top_tp = payload.get("top_tp_only") or []
    hit = payload.get("combos_at_or_above_target") or []

    lines = [
        f"# ABC v3+f1 · 止盈 / 止損 分離 · hold **{payload.get('hold_days')}d**",
        "",
        f"**{payload.get('n_legs')}** legs · 目標 portfolio 均 **≥{target:g}%** · "
        f"測試 **{payload.get('n_combos_tested')}** 組合",
        "",
        "## Hold-only baseline",
        "",
        f"- 均報酬 **{hold.get('mean_all_legs_ret_pct')}%** · ≥8% **{hold.get('pct_legs_hit_8')}%** · "
        f"≥10% **{hold.get('pct_legs_hit_10')}%**",
        "",
        f"## 目標 **{target:g}%**",
        "",
    ]
    if hit:
        lines.append(f"**可達** — {len(hit)} 組合 ≥ {target:g}%")
        for s in hit[:5]:
            lines.append(
                f"- `{s.get('variant_id')}` · 均 **{s.get('mean_all_legs_ret_pct')}%** · "
                f"TP {s.get('tp_rate_pct')}% / SL {s.get('sl_rate_pct')}%"
            )
    else:
        lines.append(
            f"**未達 {target:g}%** — 最佳 **{best.get('mean_all_legs_ret_pct')}%** "
            f"(Δ {best.get('delta_vs_hold_pp')}pp)"
        )
    lines.extend(
        [
            "",
            "## 最佳止盈（無止損）",
            "",
            f"- **均報酬** **{best_tp.get('mean_all_legs_ret_pct')}%** · Δ hold {best_tp.get('delta_vs_hold_pp')}pp · "
            f"觸發 {best_tp.get('fire_rate_pct')}%",
            f"- `{best_tp.get('tp_params', best_tp.get('variant_id'))}`",
            "",
            "## 最佳 TP+SL 組合",
            "",
            f"- **均報酬** **{best.get('mean_all_legs_ret_pct')}%** · Δ hold {best.get('delta_vs_hold_pp')}pp",
            f"- **TP** {best.get('n_tp')} ({best.get('tp_rate_pct')}%) · 賣出均 {best.get('mean_exit_on_tp_pct', '—')}%",
            f"- **SL** {best.get('n_sl')} ({best.get('sl_rate_pct')}%) · 賣出均 {best.get('mean_exit_on_sl_pct', '—')}%",
            f"- `{best.get('tp_params', '—')}` + `{best.get('sl_params', '—')}`",
            "",
            "## Top 10 TP-only",
            "",
            "| # | 全 leg 均 | Δ | 觸發率 | ≥8% |",
            "|---|---------|---|--------|-----|",
        ]
    )
    for i, s in enumerate(top_tp[:10], 1):
        lines.append(
            f"| {i} | **{s.get('mean_all_legs_ret_pct')}%** | {s.get('delta_vs_hold_pp')}pp | "
            f"{s.get('fire_rate_pct')}% | {s.get('pct_legs_hit_8')}% |"
        )
    lines.extend(
        [
            "",
            "## Top 10 TP+SL",
            "",
            "| # | 全 leg 均 | Δ | TP% | SL% | TP均 | SL均 | ≥8% |",
            "|---|---------|---|-----|-----|------|------|-----|",
        ]
    )
    for i, s in enumerate(top[:10], 1):
        lines.append(
            f"| {i} | **{s.get('mean_all_legs_ret_pct')}%** | {s.get('delta_vs_hold_pp')}pp | "
            f"{s.get('tp_rate_pct')}% | {s.get('sl_rate_pct')}% | "
            f"{s.get('mean_exit_on_tp_pct', '—')}% | {s.get('mean_exit_on_sl_pct', '—')}% | "
            f"{s.get('pct_legs_hit_8')}% |"
        )
    lines.extend(
        [
            "",
            "## 設計說明",
            "",
            "- **止盈 TP**：`gap_rv` · `rv_only`（W3/W5 RV 領先 peak）· `trail_giveback`（從 leg 內高點回吐）",
            "- **止損 SL**：`underwater`（僅 ret≤0）· `kinematic`（ret≤arm 時 dmv/drv）",
            "- 同一 poll **SL 優先於 TP** · 否則 hold 到第 5 天",
        ]
    )
    return "\n".join(lines)
