"""C18acc+S2 legs · dual-WMA intraday annotation (Phase W-annot).

Back-annotate each executed leg with WMA(5)/WMA(20) spread geometry during hold,
and compare first W5 rotation-style exit vs S2 quad_force_exit timing.
"""

from __future__ import annotations

import sqlite3
import statistics
from dataclasses import dataclass
from typing import Any

from market_breadth_ma import build_breadth_panel
from market_benchmark import load_benchmark_close
from research.backtest.c18acc_rotation_selective_exit_sweep import (
    rotation_selective_exit_sweep_configs,
)
from research.backtest.dual_wma_signal_backtest import (
    _build_stock_frame_maps,
    _exit_frame_idx,
)
from research.backtest.finpilot_local_backtest import load_price_panels
from research.backtest.rrg_mono_backtest import build_fresh_mono_calendar
from research.backtest.rrg_mono_intraday_ab import LENGTH
from research.backtest.rrg_mono_intraday_exit import _trading_days_between
from research.backtest.rrg_mono_score_swap_c import (
    ScoreSwapCConfig,
    simulate_score_swap_c,
)
from rrg_rotation import compute_rrg_panel
from rrg_universe_intraday_panel import (
    DEEP_PULLBACK_MIN,
    SPREAD_ALIGNED_MAX,
    build_dual_wma_intraday_trajectories,
    classify_dual_wma_trade_signal,
)

S2_MIN_HOLD_DAYS = 5

W5_EXIT_RULES: tuple[tuple[str, str], ...] = (
    ("exit_w5_extension", "W5 extension（短線超前）"),
    ("exit_spread_aligned", "Spread 收斂 Aligned"),
    ("exit_w20_weakening", "W20 離開 Leading/Improving"),
    ("w5_lagging", "W5 進 Lagging"),
    ("w5_deep_pullback", "W20 Leading · W5 Lagging · 大 Pullback"),
)

ROTATION_RULE_IDS = frozenset({"exit_w20_weakening", "w5_lagging", "w5_deep_pullback"})


@dataclass(frozen=True)
class W5Trigger:
    rule_id: str
    label: str
    frame_idx: int
    trade_date: str
    minute: str
    spread_class: str | None
    w5_quad: str | None
    w20_quad: str | None
    spread_dist: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "label": self.label,
            "trade_date": self.trade_date,
            "minute": self.minute,
            "spread_class": self.spread_class,
            "w5_quad": self.w5_quad,
            "w20_quad": self.w20_quad,
            "spread_dist": self.spread_dist,
        }


def _s2_config(n_slots: int) -> ScoreSwapCConfig:
    from dataclasses import replace

    configs = {c.variant_id: c for c in rotation_selective_exit_sweep_configs()}
    return replace(configs["S2"], n_slots=max(1, int(n_slots)))


def load_c18acc_s2_periods(
    conn: sqlite3.Connection,
    *,
    date_start: str,
    date_end: str,
    n_slots: int = 3,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    cfg = _s2_config(n_slots)
    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index)
    rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench, length=LENGTH)
    full_dates = close.index.astype(str).tolist()
    trade_dates = [d for d in full_dates if date_start <= d <= date_end]
    if len(trade_dates) < 2:
        raise ValueError(f"need ≥2 trade dates in [{date_start}, {date_end}]")

    fresh_by_date = build_fresh_mono_calendar(conn, trade_dates)
    panel = build_breadth_panel(conn, date_start=date_start, date_end=date_end)
    zone_by_date = {str(r.trade_date): str(r.zone_200) for r in panel.itertuples()}

    periods, summary = simulate_score_swap_c(
        conn,
        trade_dates=trade_dates,
        full_dates=full_dates,
        close=close,
        bench=bench,
        fresh_by_date=fresh_by_date,
        zone_by_date=zone_by_date,
        config=cfg,
        rs_ratio=rs_ratio,
        rs_mom=rs_mom,
    )
    summary = dict(summary)
    summary["n_slots"] = n_slots
    summary["variant_id"] = "S2"
    return periods, summary


def _leg_hold_dates(full_dates: list[str], entry: str, exit_d: str) -> list[str]:
    if entry not in full_dates or exit_d not in full_dates:
        return []
    ei, xi = full_dates.index(entry), full_dates.index(exit_d)
    if ei > xi:
        return []
    return full_dates[ei : xi + 1]


def _entry_frame_idx(frames: list[dict[str, str]], entry_date: str) -> int:
    for i, f in enumerate(frames):
        if f["date"] > entry_date:
            return max(0, i)
        if f["date"] == entry_date:
            return i
    return 0


def _exit_frame_idx_for_date(frames: list[dict[str, str]], exit_date: str) -> int:
    last = 0
    for i, f in enumerate(frames):
        if f["date"] <= exit_date:
            last = i
        else:
            break
    return last


def _w5_deep_pullback(point: dict[str, Any]) -> bool:
    w20q = (point.get("w20") or {}).get("quadrant")
    w5q = (point.get("w5") or {}).get("quadrant")
    sc = point.get("spread_class")
    dist = float(point.get("spread_dist") or 0)
    return (
        w20q == "leading"
        and w5q == "lagging"
        and sc == "pullback"
        and dist >= DEEP_PULLBACK_MIN
    )


def _scan_w5_triggers(
    *,
    frames: list[dict[str, str]],
    points_by_idx: list[dict[str, Any] | None],
    entry_date: str,
    exit_date: str,
    full_dates: list[str],
    min_hold_days: int = S2_MIN_HOLD_DAYS,
) -> list[W5Trigger]:
    entry_i = _entry_frame_idx(frames, entry_date)
    exit_i = _exit_frame_idx_for_date(frames, exit_date)
    min_date = entry_date
    if entry_date in full_dates:
        ei = full_dates.index(entry_date)
        if ei + min_hold_days < len(full_dates):
            min_date = full_dates[ei + min_hold_days]

    found: list[W5Trigger] = []
    seen_rules: set[str] = set()

    for rule_id, label in W5_EXIT_RULES:
        if rule_id in ("w5_lagging", "w5_deep_pullback"):
            trig_i: int | None = None
            for j in range(entry_i + 1, exit_i + 1):
                f = frames[j]
                if f["date"] < min_date:
                    continue
                p = points_by_idx[j]
                if not p:
                    continue
                hit = (
                    rule_id == "w5_lagging"
                    and (p.get("w5") or {}).get("quadrant") == "lagging"
                ) or (rule_id == "w5_deep_pullback" and _w5_deep_pullback(p))
                if hit:
                    trig_i = j
                    break
        else:
            trig_i = _exit_frame_idx(
                rule_id,
                entry_i,
                frames,
                points_by_idx,
                max_forward=max(exit_i - entry_i + 2, 10),
                aligned_max=SPREAD_ALIGNED_MAX,
            )
            if trig_i > exit_i:
                trig_i = None
            elif trig_i <= entry_i:
                trig_i = None
            elif frames[trig_i]["date"] < min_date:
                trig_i = None

        if trig_i is None or rule_id in seen_rules:
            continue
        p = points_by_idx[trig_i]
        if not p:
            continue
        seen_rules.add(rule_id)
        f = frames[trig_i]
        found.append(
            W5Trigger(
                rule_id=rule_id,
                label=label,
                frame_idx=trig_i,
                trade_date=f["date"],
                minute=f["minute"],
                spread_class=p.get("spread_class"),
                w5_quad=(p.get("w5") or {}).get("quadrant"),
                w20_quad=(p.get("w20") or {}).get("quadrant"),
                spread_dist=float(p["spread_dist"]) if p.get("spread_dist") is not None else None,
            )
        )

    found.sort(key=lambda t: (t.trade_date, t.minute, t.rule_id))
    return found


def _first_rotation_trigger(triggers: list[W5Trigger]) -> W5Trigger | None:
    rot = sorted(
        [t for t in triggers if t.rule_id in ROTATION_RULE_IDS],
        key=lambda t: (t.trade_date, t.minute, t.rule_id),
    )
    return rot[0] if rot else None


def _compare_timing(
    *,
    full_dates: list[str],
    entry_date: str,
    s2_exit_date: str | None,
    w5_trigger: W5Trigger | None,
) -> dict[str, Any]:
    if w5_trigger is None:
        return {
            "w5_leads_s2": None,
            "lead_trading_days": None,
            "timing_bucket": "no_w5_rotation",
        }
    w5_date = w5_trigger.trade_date
    if s2_exit_date is None:
        return {
            "w5_leads_s2": True,
            "lead_trading_days": None,
            "timing_bucket": "w5_only",
        }
    if w5_date < s2_exit_date:
        lead = _trading_days_between(full_dates, w5_date, s2_exit_date)
        return {
            "w5_leads_s2": True,
            "lead_trading_days": lead,
            "timing_bucket": "w5_earlier",
        }
    if w5_date == s2_exit_date:
        return {
            "w5_leads_s2": False,
            "lead_trading_days": 0,
            "timing_bucket": "same_day",
        }
    lag = _trading_days_between(full_dates, s2_exit_date, w5_date)
    return {
        "w5_leads_s2": False,
        "lead_trading_days": -lag,
        "timing_bucket": "s2_earlier",
    }


def annotate_leg(
    period: dict[str, Any],
    *,
    frames: list[dict[str, str]],
    stock_maps: dict[str, list[dict[str, Any] | None]],
    full_dates: list[str],
) -> dict[str, Any]:
    sid = str(period["stock_id"])
    entry = str(period["entry_date"])
    exit_d = str(period["exit_date"])
    points_by_idx = stock_maps.get(sid) or [None] * len(frames)

    s2_force = str(period.get("exit_reason") or "") == "quad_force_exit"
    hold_days = int(period.get("hold_days") or _trading_days_between(full_dates, entry, exit_d))

    triggers = _scan_w5_triggers(
        frames=frames,
        points_by_idx=points_by_idx,
        entry_date=entry,
        exit_date=exit_d,
        full_dates=full_dates,
    )
    first_rot = _first_rotation_trigger(triggers)
    timing = _compare_timing(
        full_dates=full_dates,
        entry_date=entry,
        s2_exit_date=exit_d if s2_force else None,
        w5_trigger=first_rot,
    )

    entry_signals: list[dict[str, Any]] = []
    entry_i = _entry_frame_idx(frames, entry)
    exit_i = _exit_frame_idx_for_date(frames, exit_d)
    for j in range(entry_i, min(exit_i + 1, len(frames))):
        p = points_by_idx[j]
        if not p:
            continue
        sig = classify_dual_wma_trade_signal(p)
        if sig:
            f = frames[j]
            entry_signals.append(
                {
                    "trade_date": f["date"],
                    "minute": f["minute"],
                    "trade_signal": sig,
                    "spread_class": p.get("spread_class"),
                }
            )

    return {
        "leg_id": f"{entry}|{sid}|{period.get('slot', 0)}",
        "stock_id": sid,
        "stock_name": period.get("stock_name", ""),
        "entry_date": entry,
        "exit_date": exit_d,
        "hold_days": hold_days,
        "return_pct": period.get("return_pct"),
        "excess_pct": period.get("excess_pct"),
        "exit_reason": period.get("exit_reason"),
        "s2_force_exit": s2_force,
        "quad_force_exit_streak": period.get("quad_force_exit_streak"),
        "w5_triggers": [t.to_dict() for t in triggers],
        "w5_first_rotation": first_rot.to_dict() if first_rot else None,
        "w5_entry_signals_in_hold": entry_signals,
        "timing_vs_s2": timing,
    }


def _collect_annot_dates(periods: list[dict[str, Any]], full_dates: list[str]) -> list[str]:
    dates: set[str] = set()
    for p in periods:
        entry, exit_d = str(p["entry_date"]), str(p["exit_date"])
        for d in _leg_hold_dates(full_dates, entry, exit_d):
            dates.add(d)
    return sorted(dates)


def run_c18acc_s2_dual_wma_annot(
    conn: sqlite3.Connection,
    *,
    date_start: str,
    date_end: str,
    n_slots: int = 3,
) -> dict[str, Any]:
    periods, s2_summary = load_c18acc_s2_periods(
        conn, date_start=date_start, date_end=date_end, n_slots=n_slots
    )
    close, _, _ = load_price_panels(conn)
    full_dates = close.index.astype(str).tolist()

    stock_ids = sorted({str(p["stock_id"]) for p in periods})
    annot_dates = _collect_annot_dates(periods, full_dates)
    if not annot_dates:
        raise ValueError("no hold dates for annotation")

    print(
        f"W-annot: {len(periods)} legs · {len(stock_ids)} stocks · "
        f"{len(annot_dates)} trade dates …",
        flush=True,
    )
    frames, trajectories, traj_meta = build_dual_wma_intraday_trajectories(
        conn,
        dates=annot_dates,
        stock_ids=stock_ids,
    )
    stock_maps = _build_stock_frame_maps(trajectories, frames)

    legs = [
        annotate_leg(
            p,
            frames=frames,
            stock_maps=stock_maps,
            full_dates=full_dates,
        )
        for p in periods
    ]

    n_s2 = sum(1 for lg in legs if lg["s2_force_exit"])
    n_w5_rot = sum(1 for lg in legs if lg["w5_first_rotation"])
    n_w5_only = sum(
        1 for lg in legs if lg["w5_first_rotation"] and not lg["s2_force_exit"]
    )
    n_s2_only = sum(
        1 for lg in legs if lg["s2_force_exit"] and not lg["w5_first_rotation"]
    )
    n_both = sum(
        1 for lg in legs if lg["s2_force_exit"] and lg["w5_first_rotation"]
    )

    s2_subset = [lg for lg in legs if lg["s2_force_exit"]]
    w5_earlier = sum(
        1 for lg in s2_subset if lg["timing_vs_s2"].get("timing_bucket") == "w5_earlier"
    )
    same_day = sum(
        1 for lg in s2_subset if lg["timing_vs_s2"].get("timing_bucket") == "same_day"
    )
    s2_earlier = sum(
        1 for lg in s2_subset if lg["timing_vs_s2"].get("timing_bucket") == "s2_earlier"
    )
    lead_days = [
        lg["timing_vs_s2"]["lead_trading_days"]
        for lg in s2_subset
        if lg["timing_vs_s2"].get("timing_bucket") == "w5_earlier"
        and lg["timing_vs_s2"].get("lead_trading_days") is not None
    ]

    rule_counts: dict[str, int] = {}
    for lg in legs:
        for t in lg["w5_triggers"]:
            rule_counts[t["rule_id"]] = rule_counts.get(t["rule_id"], 0) + 1

    return {
        "schema": "c18acc_s2_dual_wma_annot-v1",
        "phase": "W-annot",
        "date_start": date_start,
        "date_end": date_end,
        "n_slots": n_slots,
        "s2_summary": s2_summary,
        "trajectory_meta": traj_meta,
        "n_legs": len(legs),
        "n_stocks": len(stock_ids),
        "n_annot_dates": len(annot_dates),
        "cohort": {
            "n_s2_force_exit": n_s2,
            "n_w5_rotation_signal": n_w5_rot,
            "n_w5_only": n_w5_only,
            "n_s2_only": n_s2_only,
            "n_both": n_both,
            "among_s2_force": {
                "n": len(s2_subset),
                "w5_earlier": w5_earlier,
                "same_day": same_day,
                "s2_earlier": s2_earlier,
                "median_lead_days_when_w5_earlier": round(statistics.median(lead_days), 2)
                if lead_days
                else None,
            },
            "w5_rule_hit_counts": rule_counts,
        },
        "legs": legs,
    }


def render_c18acc_s2_dual_wma_annot_md(payload: dict[str, Any]) -> str:
    c = payload["cohort"]
    among = c["among_s2_force"]
    lines = [
        "# C18acc+S2 · dual-WMA 回溯註解（Phase W-annot）",
        "",
        f"區間：**{payload['date_start']}** .. **{payload['date_end']}** · "
        f"{payload['n_slots']} 槽 · **{payload['n_legs']}** legs",
        "",
        f"dual-WMA 回溯：{payload['n_stocks']} 檔 · {payload['n_annot_dates']} 交易日 · "
        f"{payload['trajectory_meta'].get('n_frames', '—')} 盤中幀",
        "",
        "## 隊列摘要",
        "",
        f"| 指標 | 值 |",
        f"|------|-----|",
        f"| S2 force exit legs | {c['n_s2_force_exit']} |",
        f"| W5 rotation 訊號（min_hold 後） | {c['n_w5_rotation_signal']} |",
        f"| 僅 W5（無 S2） | {c['n_w5_only']} |",
        f"| 僅 S2（無 W5 rotation） | {c['n_s2_only']} |",
        f"| 兩者皆有 | {c['n_both']} |",
        "",
        "## S2 force 子樣本 · W5 是否更早",
        "",
        f"- 樣本 n = **{among['n']}**",
        f"- W5 rotation **更早**：{among['w5_earlier']}",
        f"- **同日**：{among['same_day']}",
        f"- S2 **更早**（或 W5 未觸發 rotation）：{among['s2_earlier']}",
        f"- W5 領先中位數（交易日）：{among['median_lead_days_when_w5_earlier']}",
        "",
        "## W5 規則命中次數（leg 級 · 可重複）",
        "",
        "| rule | hits |",
        "|------|------|",
    ]
    for rid, label in W5_EXIT_RULES:
        lines.append(f"| {rid} | {c['w5_rule_hit_counts'].get(rid, 0)} |")

    lines += [
        "",
        "## S2 force legs 明細",
        "",
        "| stock | entry | s2_exit | W5 首個 rotation | 領先日 | bucket |",
        "|-------|-------|---------|------------------|--------|--------|",
    ]
    for lg in payload["legs"]:
        if not lg["s2_force_exit"]:
            continue
        w5 = lg.get("w5_first_rotation") or {}
        tv = lg["timing_vs_s2"]
        lines.append(
            f"| {lg['stock_id']} | {lg['entry_date']} | {lg['exit_date']} "
            f"| {w5.get('rule_id', '—')} @ {w5.get('trade_date', '—')} "
            f"| {tv.get('lead_trading_days', '—')} | {tv.get('timing_bucket')} |"
        )

    lines += [
        "",
        "## 解讀",
        "",
        "- **W5 rotation** = `w5_lagging` · `w5_deep_pullback` · `exit_w20_weakening` 三者最早者。",
        "- **領先日** = W5 rotation 訊號日至 S2 force exit 日之交易日差（僅 w5_earlier）。",
        "- 盤中 poll 30m；進場仍為 C18acc 收盤 · S2 為日線 close。",
        "",
        "---",
        "模組：`scripts/run_c18acc_s2_dual_wma_annot.py`",
        "",
    ]
    return "\n".join(lines)
