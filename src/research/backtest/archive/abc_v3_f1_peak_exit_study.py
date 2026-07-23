"""ABC v3+f1 · hold-period price peak · WMA20/5/3 pattern + exit-rule counterfactual."""

from __future__ import annotations

import sqlite3
import statistics
from collections import Counter
from datetime import datetime
from typing import Any, Callable

from research.backtest.archive.abc_v3_slot_sweep import (
    ABC_V3_HOLD_DAYS,
    _build_abc_v3_intraday_panel,
    collect_abc_v3_skip0900_period_candidates,
)
from research.backtest.dual_wma_signal_backtest import ROUND_TRIP_COST_PCT, _exit_frame_idx
from research.backtest.finpilot_local_backtest import load_price_panels
from research.backtest.triple_wma_pullback_sweep import _mv, _q, _rv, matches_w3_rv_hl_abc_v3_f1
from research.backtest.w3_rv_hl_winner_profile import extract_entry_features
from rrg_universe_snapshot import kbar_close_at_minute

PeakPredicate = Callable[[dict[str, Any], dict[str, Any]], bool]

DEFAULT_DATE_START = "2024-01-02"
DEFAULT_PROFIT_GROSS_PCT = 5.0
PROFIT_GROSS_GRID = (3.0, 5.0, 8.0)
MV_DECLINE_GRID = (1, 2)


def _pct(n: int, d: int) -> float:
    return round(100.0 * n / d, 1) if d else 0.0


def _mean(vals: list[float]) -> float | None:
    return round(statistics.mean(vals), 2) if vals else None


def _median(vals: list[float]) -> float | None:
    return round(statistics.median(vals), 2) if vals else None


def snap_peak_features(point: dict[str, Any] | None) -> dict[str, Any]:
    """RRG + spread snapshot at one poll frame."""
    if not point:
        return {}
    feats = extract_entry_features(point)
    feats.update(
        spread_class=point.get("spread_class"),
        w20q=_q(point, "w20"),
        w5q=_q(point, "w5"),
        w3q=_q(point, "w3"),
        w20_rv=_rv(point, "w20"),
        w5_rv=_rv(point, "w5"),
        w3_rv=_rv(point, "w3"),
        w20_mv=_mv(point, "w20"),
        w5_mv=_mv(point, "w5"),
        w3_mv=_mv(point, "w3"),
        spread_dist=point.get("spread_dist"),
        hook_extension_peak=bool(point.get("hook_extension_peak")),
        hook_complete=bool(point.get("hook_complete")),
    )
    return feats


def _wma_combo(feats: dict[str, Any]) -> str:
    return f"{feats.get('w20q')}/{feats.get('w5q')}/{feats.get('w3q')}"


def _dist_rows(rows: list[dict[str, Any]], key_fn: Callable[[dict[str, Any]], Any]) -> list[dict[str, Any]]:
    counts = Counter(key_fn(r) for r in rows)
    total = len(rows)
    return [
        {"key": key, "n": counts[key], "pct": _pct(counts[key], total)}
        for key, _ in counts.most_common(8)
    ]


def exit_rule_specs() -> list[dict[str, Any]]:
    """Named exit predicates · first hit after entry within hold window."""

    def _entry_feats(row: dict[str, Any]) -> dict[str, Any]:
        return row.get("features") or {}

    specs: list[tuple[str, str, PeakPredicate]] = [
        (
            "spread_extension",
            "spread_class = extension",
            lambda p, _fe: p.get("spread_class") == "extension",
        ),
        (
            "w5_leading_extension",
            "extension + W5 Leading",
            lambda p, _fe: p.get("spread_class") == "extension"
            and (p.get("w5") or {}).get("quadrant") == "leading",
        ),
        (
            "hook_extension_peak",
            "hook_extension_peak",
            lambda p, _fe: bool(p.get("hook_extension_peak")),
        ),
        (
            "hook_complete",
            "hook_complete (peak→aligned)",
            lambda p, _fe: bool(p.get("hook_complete")),
        ),
        (
            "w20_weakening",
            "W20 weakening/lagging",
            lambda p, _fe: (p.get("w20") or {}).get("quadrant") in ("weakening", "lagging"),
        ),
        (
            "spread_aligned",
            "spread_class = aligned",
            lambda p, _fe: p.get("spread_class") == "aligned",
        ),
        (
            "w3_rv_below_100",
            "W3 RV < 100",
            lambda p, _fe: (_rv(p, "w3") or 999.0) < 100.0,
        ),
        (
            "w3_rv_gte_103",
            "W3 RV ≥ 103",
            lambda p, _fe: (_rv(p, "w3") or 0.0) >= 103.0,
        ),
        (
            "w3_rv_gte_102_5",
            "W3 RV ≥ 102.5",
            lambda p, _fe: (_rv(p, "w3") or 0.0) >= 102.5,
        ),
        (
            "w20_rv_gte_105",
            "W20 RV ≥ 105",
            lambda p, _fe: (_rv(p, "w20") or 0.0) >= 105.0,
        ),
        (
            "w3_delta_gte_3",
            "W3 RV − entry ≥ 3",
            lambda p, fe: (_rv(p, "w3") or 0.0) - float(fe.get("w3_rv") or 0.0) >= 3.0,
        ),
        (
            "pullback_to_mixed",
            "pullback → mixed",
            lambda p, fe: fe.get("spread_class") == "pullback" and p.get("spread_class") == "mixed",
        ),
        (
            "all_leading_q",
            "W20/W5/W3 all Leading",
            lambda p, _fe: all(
                (p.get(leg) or {}).get("quadrant") == "leading" for leg in ("w20", "w5", "w3")
            ),
        ),
        (
            "w5_mv_gte_102",
            "W5 momentum ≥ 102",
            lambda p, _fe: (_mv(p, "w5") or 0.0) >= 102.0,
        ),
    ]
    return [{"id": sid, "label": label, "predicate": pred, "family": "wma"} for sid, label, pred in specs]


def _mv_declining(
    points: list[dict[str, Any] | None],
    frame_idx: int,
    leg: str,
    *,
    consecutive: int = 1,
) -> bool:
    """MV strictly down for `consecutive` poll steps ending at frame_idx."""
    if consecutive < 1 or frame_idx < consecutive:
        return False
    prev_mv: float | None = None
    for k in range(frame_idx - consecutive + 1, frame_idx + 1):
        point = points[k]
        if not point:
            return False
        mv = _mv(point, leg)
        if mv is None:
            return False
        cur = float(mv)
        if prev_mv is not None and cur >= prev_mv:
            return False
        prev_mv = cur
    return True


def _mv_declining_any(
    points: list[dict[str, Any] | None],
    frame_idx: int,
    legs: tuple[str, ...],
    *,
    consecutive: int = 1,
) -> bool:
    return any(_mv_declining(points, frame_idx, leg, consecutive=consecutive) for leg in legs)


def profit_gated_exit_rule_specs(
    *,
    profit_grid: tuple[float, ...] = PROFIT_GROSS_GRID,
    decline_grid: tuple[int, ...] = MV_DECLINE_GRID,
) -> list[dict[str, Any]]:
    """User hypothesis · gross target + W3/W5 MV rollover."""
    specs: list[dict[str, Any]] = []
    for profit in profit_grid:
        p = int(profit) if profit == int(profit) else profit
        for decline in decline_grid:
            d_suffix = f"d{decline}"
            for leg, label_leg in (("w3", "W3"), ("w5", "W5")):
                rid = f"profit{p:g}_{leg}_mv_{d_suffix}"
                specs.append(
                    {
                        "id": rid,
                        "label": f"gross≥{p:g}% · {label_leg} MV ↓×{decline}",
                        "family": "profit_gated",
                        "profit_min_gross_pct": profit,
                        "legs": (leg,),
                        "decline_polls": decline,
                    }
                )
            rid = f"profit{p:g}_w3_or_w5_mv_{d_suffix}"
            specs.append(
                {
                    "id": rid,
                    "label": f"gross≥{p:g}% · W3 or W5 MV ↓×{decline}",
                    "family": "profit_gated",
                    "profit_min_gross_pct": profit,
                    "legs": ("w3", "w5"),
                    "decline_polls": decline,
                    "legs_mode": "any",
                }
            )
            specs.append(
                {
                    "id": f"profit{p:g}_w3_or_w5_mv_{d_suffix}_to_mixed",
                    "label": f"gross≥{p:g}% · MV↓×{decline} · pullback→mixed",
                    "family": "profit_gated",
                    "profit_min_gross_pct": profit,
                    "legs": ("w3", "w5"),
                    "decline_polls": decline,
                    "legs_mode": "any",
                    "require_pullback_to_mixed": True,
                }
            )
    for leg in ("w3", "w5"):
        specs.append(
            {
                "id": f"{leg}_mv_d1_ungated",
                "label": f"{leg.upper()} MV ↓×1 (no profit gate)",
                "family": "profit_gated",
                "profit_min_gross_pct": None,
                "legs": (leg,),
                "decline_polls": 1,
            }
        )
    return specs


def _price_at(
    cache: dict[tuple[str, str, str], float | None],
    conn: sqlite3.Connection,
    stock_id: str,
    trade_date: str,
    minute: str,
) -> float | None:
    key = (stock_id, trade_date, minute)
    if key not in cache:
        cache[key] = kbar_close_at_minute(conn, stock_id, trade_date, minute)
    return cache[key]


def collect_peak_trade_rows(
    conn: sqlite3.Connection,
    candidates: list[dict[str, Any]],
    *,
    frames: list[dict[str, str]],
    stock_maps: dict[str, list[dict[str, Any] | None]],
    hold_days: int = ABC_V3_HOLD_DAYS,
) -> list[dict[str, Any]]:
    """For each ABC f1 candidate · find price MFE frame + WMA snapshot."""
    exit_rule = f"hold_{hold_days}d"
    price_cache: dict[tuple[str, str, str], float | None] = {}
    rows: list[dict[str, Any]] = []

    for cand in candidates:
        stock_id = str(cand["stock_id"])
        entry_idx = int(cand["entry_frame_idx"])
        points = stock_maps.get(stock_id)
        if not points:
            continue
        exit_idx = _exit_frame_idx(exit_rule, entry_idx, frames, points)
        if exit_idx <= entry_idx:
            continue

        entry_frame = frames[entry_idx]
        exit_frame = frames[exit_idx]
        entry_px = float(cand.get("entry_px") or 0) or _price_at(
            price_cache, conn, stock_id, entry_frame["date"], entry_frame["minute"]
        )
        exit_px = float(cand.get("exit_px") or 0) or _price_at(
            price_cache, conn, stock_id, exit_frame["date"], exit_frame["minute"]
        )
        if not entry_px or not exit_px:
            continue

        peak_idx = entry_idx
        peak_px = float(entry_px)
        for j in range(entry_idx, exit_idx + 1):
            frame = frames[j]
            px = _price_at(price_cache, conn, stock_id, frame["date"], frame["minute"])
            if px and px > peak_px:
                peak_px = float(px)
                peak_idx = j

        mfe_pct = (peak_px / entry_px - 1.0) * 100.0
        exit_gross_pct = (exit_px / entry_px - 1.0) * 100.0
        entry_feats = cand.get("features") or {}
        peak_feats = snap_peak_features(points[peak_idx])
        exit_feats = snap_peak_features(points[exit_idx])

        rows.append(
            {
                "stock_id": stock_id,
                "stock_name": cand.get("stock_name") or "",
                "entry_date": entry_frame["date"],
                "entry_minute": entry_frame["minute"],
                "exit_date": exit_frame["date"],
                "exit_minute": exit_frame["minute"],
                "entry_frame_idx": entry_idx,
                "exit_frame_idx": exit_idx,
                "peak_frame_idx": peak_idx,
                "entry_px": round(entry_px, 4),
                "exit_px": round(exit_px, 4),
                "peak_px": round(peak_px, 4),
                "net_pct": round(exit_gross_pct - ROUND_TRIP_COST_PCT, 3),
                "mfe_pct": round(mfe_pct, 3),
                "giveback_pct": round(mfe_pct - exit_gross_pct, 3),
                "peak_is_exit": peak_idx == exit_idx,
                "peak_before_exit_polls": exit_idx - peak_idx,
                "features": entry_feats,
                "peak_feats": peak_feats,
                "exit_feats": exit_feats,
                "w3_rv_delta": round(
                    float(peak_feats.get("w3_rv") or 0.0) - float(entry_feats.get("w3_rv") or 0.0),
                    3,
                ),
                "w20_rv_delta": round(
                    float(peak_feats.get("w20_rv") or 0.0) - float(entry_feats.get("w20_rv") or 0.0),
                    3,
                ),
                "peak_spread_transition": f"{entry_feats.get('spread_class')}→{peak_feats.get('spread_class')}",
                "peak_wma_combo": _wma_combo(peak_feats),
            }
        )
    return rows


def simulate_exit_rule(
    conn: sqlite3.Connection,
    rows: list[dict[str, Any]],
    *,
    frames: list[dict[str, str]],
    stock_maps: dict[str, list[dict[str, Any] | None]],
    rule_id: str,
    predicate: PeakPredicate,
    min_polls_after_entry: int = 1,
) -> dict[str, Any]:
    """Counterfactual · exit at first predicate hit else hold-3d exit."""
    price_cache: dict[tuple[str, str, str], float | None] = {}
    nets: list[float] = []
    saves: list[float] = []
    triggered = 0

    for row in rows:
        stock_id = str(row["stock_id"])
        entry_idx = int(row["entry_frame_idx"])
        exit_idx = int(row["exit_frame_idx"])
        points = stock_maps[stock_id]
        entry_frame = frames[entry_idx]
        exit_frame = frames[exit_idx]
        entry_px = float(row["entry_px"])
        exit_px = float(row["exit_px"])
        entry_feats = row.get("features") or {}

        hit_idx: int | None = None
        for j in range(entry_idx + min_polls_after_entry, exit_idx + 1):
            point = points[j]
            if point and predicate(point, entry_feats):
                hit_idx = j
                break

        if hit_idx is None:
            nets.append(float(row["net_pct"]))
            continue

        triggered += 1
        hit_frame = frames[hit_idx]
        hit_px = _price_at(price_cache, conn, stock_id, hit_frame["date"], hit_frame["minute"])
        if not hit_px:
            nets.append(float(row["net_pct"]))
            continue
        rule_net = (hit_px / entry_px - 1.0) * 100.0 - ROUND_TRIP_COST_PCT
        nets.append(rule_net)
        saves.append(float(row["net_pct"]) - rule_net)

    n = len(nets)
    return {
        "rule_id": rule_id,
        "n": n,
        "trigger_rate_pct": _pct(triggered, n),
        "mean_net_pct": _mean(nets),
        "median_net_pct": _median(nets),
        "win_rate_pct": _pct(sum(1 for x in nets if x > 0), n),
        "mean_save_vs_hold_pct": _mean(saves),
        "family": "wma",
    }


def simulate_profit_gated_exit_rule(
    conn: sqlite3.Connection,
    rows: list[dict[str, Any]],
    *,
    frames: list[dict[str, str]],
    stock_maps: dict[str, list[dict[str, Any] | None]],
    rule_id: str,
    profit_min_gross_pct: float | None,
    legs: tuple[str, ...],
    decline_polls: int = 1,
    legs_mode: str = "any",
    require_pullback_to_mixed: bool = False,
    min_polls_after_entry: int = 1,
) -> dict[str, Any]:
    """Gross-profit gate + MV rollover · first hit else hold-3d."""
    price_cache: dict[tuple[str, str, str], float | None] = {}
    nets: list[float] = []
    saves: list[float] = []
    triggered = 0

    for row in rows:
        stock_id = str(row["stock_id"])
        entry_idx = int(row["entry_frame_idx"])
        exit_idx = int(row["exit_frame_idx"])
        points = stock_maps[stock_id]
        entry_px = float(row["entry_px"])
        entry_feats = row.get("features") or {}

        hit_idx: int | None = None
        for j in range(entry_idx + min_polls_after_entry, exit_idx + 1):
            frame = frames[j]
            px = _price_at(price_cache, conn, stock_id, frame["date"], frame["minute"])
            if not px:
                continue
            gross_pct = (px / entry_px - 1.0) * 100.0
            if profit_min_gross_pct is not None and gross_pct < profit_min_gross_pct:
                continue
            if legs_mode == "any":
                mv_ok = _mv_declining_any(points, j, legs, consecutive=decline_polls)
            else:
                mv_ok = all(_mv_declining(points, j, leg, consecutive=decline_polls) for leg in legs)
            if not mv_ok:
                continue
            point = points[j]
            if require_pullback_to_mixed and point:
                if entry_feats.get("spread_class") != "pullback" or point.get("spread_class") != "mixed":
                    continue
            hit_idx = j
            break

        if hit_idx is None:
            nets.append(float(row["net_pct"]))
            continue

        triggered += 1
        hit_frame = frames[hit_idx]
        hit_px = _price_at(price_cache, conn, stock_id, hit_frame["date"], hit_frame["minute"])
        if not hit_px:
            nets.append(float(row["net_pct"]))
            continue
        rule_net = (hit_px / entry_px - 1.0) * 100.0 - ROUND_TRIP_COST_PCT
        nets.append(rule_net)
        saves.append(float(row["net_pct"]) - rule_net)

    n = len(nets)
    return {
        "rule_id": rule_id,
        "n": n,
        "trigger_rate_pct": _pct(triggered, n),
        "mean_net_pct": _mean(nets),
        "median_net_pct": _median(nets),
        "win_rate_pct": _pct(sum(1 for x in nets if x > 0), n),
        "mean_save_vs_hold_pct": _mean(saves),
        "family": "profit_gated",
        "profit_min_gross_pct": profit_min_gross_pct,
        "decline_polls": decline_polls,
        "legs": list(legs),
    }


def summarize_peak_patterns(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Cohort tables · entry→peak deltas · spread transitions."""
    if not rows:
        return {"n": 0}

    nets = [float(r["net_pct"]) for r in rows]
    winners = [r for r in rows if float(r["net_pct"]) > 0]
    losers = [r for r in rows if float(r["net_pct"]) <= 0]
    big_wins = [r for r in rows if float(r["net_pct"]) >= 5.0]
    big_giveback = [r for r in rows if float(r["giveback_pct"]) >= 3.0 and float(r["mfe_pct"]) >= 3.0]
    top_slice = rows[: max(1, len(rows) // 7)]

    def _delta_stats(sub: list[dict[str, Any]], field: str) -> float | None:
        vals = [float(r[field]) for r in sub if r.get(field) is not None]
        return _mean(vals)

    return {
        "n": len(rows),
        "mean_net_pct": _mean(nets),
        "median_net_pct": _median(nets),
        "win_rate_pct": _pct(len(winners), len(rows)),
        "mean_mfe_pct": _mean([float(r["mfe_pct"]) for r in rows]),
        "mean_giveback_pct": _mean([float(r["giveback_pct"]) for r in rows]),
        "peak_at_exit_pct": _pct(sum(1 for r in rows if r.get("peak_is_exit")), len(rows)),
        "oracle_peak_exit_mean_pct": _mean(
            [float(r["mfe_pct"]) - ROUND_TRIP_COST_PCT for r in rows]
        ),
        "top15pct_peak_spread": _dist_rows(top_slice, lambda r: (r.get("peak_feats") or {}).get("spread_class")),
        "top15pct_wma_combo": _dist_rows(top_slice, lambda r: r.get("peak_wma_combo")),
        "big_win_n": len(big_wins),
        "big_win_peak_spread": _dist_rows(big_wins, lambda r: (r.get("peak_feats") or {}).get("spread_class")),
        "big_win_wma_combo": _dist_rows(big_wins, lambda r: r.get("peak_wma_combo")),
        "big_giveback_n": len(big_giveback),
        "big_giveback_peak_spread": _dist_rows(
            big_giveback, lambda r: (r.get("peak_feats") or {}).get("spread_class")
        ),
        "win_peak_spread": _dist_rows(winners, lambda r: (r.get("peak_feats") or {}).get("spread_class")),
        "lose_peak_spread": _dist_rows(losers, lambda r: (r.get("peak_feats") or {}).get("spread_class")),
        "spread_transitions_big_win": _dist_rows(big_wins, lambda r: r.get("peak_spread_transition")),
        "spread_transitions_all": _dist_rows(rows, lambda r: r.get("peak_spread_transition")),
        "delta_big_win": {
            "w3_rv_delta": _delta_stats(big_wins, "w3_rv_delta"),
            "w20_rv_delta": _delta_stats(big_wins, "w20_rv_delta"),
            "peak_w3_rv": _mean([float((r.get("peak_feats") or {}).get("w3_rv") or 0) for r in big_wins]),
            "peak_w20_rv": _mean([float((r.get("peak_feats") or {}).get("w20_rv") or 0) for r in big_wins]),
            "peak_w5_rv": _mean([float((r.get("peak_feats") or {}).get("w5_rv") or 0) for r in big_wins]),
            "mean_mfe_pct": _mean([float(r["mfe_pct"]) for r in big_wins]),
            "mean_giveback_pct": _mean([float(r["giveback_pct"]) for r in big_wins]),
        },
        "delta_losers": {
            "w3_rv_delta": _delta_stats(losers, "w3_rv_delta"),
            "w20_rv_delta": _delta_stats(losers, "w20_rv_delta"),
            "mean_giveback_pct": _mean([float(r["giveback_pct"]) for r in losers]),
        },
    }


def top_trade_examples(rows: list[dict[str, Any]], *, n: int = 10) -> list[dict[str, Any]]:
    ordered = sorted(rows, key=lambda r: float(r["net_pct"]), reverse=True)
    out: list[dict[str, Any]] = []
    for row in ordered[:n]:
        peak = row.get("peak_feats") or {}
        entry = row.get("features") or {}
        out.append(
            {
                "stock_id": row["stock_id"],
                "stock_name": row.get("stock_name") or "",
                "entry_date": row["entry_date"],
                "entry_minute": row["entry_minute"],
                "net_pct": row["net_pct"],
                "mfe_pct": row["mfe_pct"],
                "giveback_pct": row["giveback_pct"],
                "entry_spread": entry.get("spread_class"),
                "peak_spread": peak.get("spread_class"),
                "exit_spread": (row.get("exit_feats") or {}).get("spread_class"),
                "peak_wma_combo": row.get("peak_wma_combo"),
                "peak_rv": {
                    "w20": peak.get("w20_rv"),
                    "w5": peak.get("w5_rv"),
                    "w3": peak.get("w3_rv"),
                },
                "peak_mv": {
                    "w20": peak.get("w20_mv"),
                    "w5": peak.get("w5_mv"),
                    "w3": peak.get("w3_mv"),
                },
                "peak_before_exit_polls": row.get("peak_before_exit_polls"),
            }
        )
    return out


def run_exit_rule_grid(
    conn: sqlite3.Connection,
    rows: list[dict[str, Any]],
    *,
    frames: list[dict[str, str]],
    stock_maps: dict[str, list[dict[str, Any] | None]],
) -> list[dict[str, Any]]:
    baseline = _mean([float(r["net_pct"]) for r in rows])
    grid: list[dict[str, Any]] = [
        {
            "rule_id": "hold_3d_baseline",
            "label": "hold 3d → 13:30",
            "n": len(rows),
            "trigger_rate_pct": 0.0,
            "mean_net_pct": baseline,
            "median_net_pct": _median([float(r["net_pct"]) for r in rows]),
            "win_rate_pct": _pct(sum(1 for r in rows if float(r["net_pct"]) > 0), len(rows)),
            "mean_save_vs_hold_pct": 0.0,
            "delta_vs_baseline_pp": 0.0,
            "family": "baseline",
        }
    ]
    base = baseline or 0.0

    def _attach_delta(result: dict[str, Any], label: str) -> dict[str, Any]:
        rule_mean = float(result.get("mean_net_pct") or 0.0)
        return {
            **result,
            "label": label,
            "delta_vs_baseline_pp": round(rule_mean - base, 3),
        }

    for spec in exit_rule_specs():
        grid.append(
            _attach_delta(
                simulate_exit_rule(
                    conn,
                    rows,
                    frames=frames,
                    stock_maps=stock_maps,
                    rule_id=str(spec["id"]),
                    predicate=spec["predicate"],
                ),
                str(spec["label"]),
            )
        )
    for spec in profit_gated_exit_rule_specs():
        grid.append(
            _attach_delta(
                simulate_profit_gated_exit_rule(
                    conn,
                    rows,
                    frames=frames,
                    stock_maps=stock_maps,
                    rule_id=str(spec["id"]),
                    profit_min_gross_pct=spec.get("profit_min_gross_pct"),
                    legs=tuple(spec.get("legs") or ()),
                    decline_polls=int(spec.get("decline_polls") or 1),
                    legs_mode=str(spec.get("legs_mode") or "any"),
                    require_pullback_to_mixed=bool(spec.get("require_pullback_to_mixed")),
                ),
                str(spec["label"]),
            )
        )
    grid.sort(key=lambda x: float(x.get("mean_net_pct") or 0.0), reverse=True)
    return grid


def run_abc_v3_f1_peak_exit_study(
    conn: sqlite3.Connection,
    *,
    date_start: str = DEFAULT_DATE_START,
    date_end: str | None = None,
    intraday_tail_days: int = 90,
    long_window: bool = False,
) -> dict[str, Any]:
    """Build dual-WMA panel · ABC f1 events · peak WMA morphology + exit counterfactuals."""
    close, _, _ = load_price_panels(conn)
    full_dates = close.index.astype(str).tolist()
    end = date_end or full_dates[-1]
    trade_dates = [d for d in full_dates if date_start <= d <= end]
    if long_window:
        sample_dates = trade_dates
    elif intraday_tail_days > 0 and len(trade_dates) > intraday_tail_days:
        sample_dates = trade_dates[-intraday_tail_days:]
    else:
        sample_dates = trade_dates
    if len(sample_dates) < 5:
        raise ValueError(f"insufficient sample dates: {len(sample_dates)}")

    frames, trajectories, panel_meta, stock_maps, bench = _build_abc_v3_intraday_panel(
        conn, dates=sample_dates
    )
    candidates, _, _ = collect_abc_v3_skip0900_period_candidates(
        conn,
        dates=sample_dates,
        matcher=matches_w3_rv_hl_abc_v3_f1,
        frames=frames,
        trajectories=trajectories,
        meta=panel_meta,
        stock_maps=stock_maps,
        bench=bench,
    )
    rows = collect_peak_trade_rows(
        conn, candidates, frames=frames, stock_maps=stock_maps, hold_days=ABC_V3_HOLD_DAYS
    )
    summary = summarize_peak_patterns(rows)
    exit_grid = run_exit_rule_grid(conn, rows, frames=frames, stock_maps=stock_maps)

    profit_rules = [r for r in exit_grid if r.get("family") == "profit_gated"]
    best_profit = max(profit_rules, key=lambda x: float(x.get("mean_net_pct") or 0.0), default=None)
    best_rule = next(
        (r for r in exit_grid if r.get("rule_id") != "hold_3d_baseline"),
        None,
    )
    oracle = float(summary.get("oracle_peak_exit_mean_pct") or 0.0)
    baseline = float(summary.get("mean_net_pct") or 0.0)
    reasons = [
        f"n={summary.get('n')} · hold3d mean {baseline}% · MFE {summary.get('mean_mfe_pct')}% · "
        f"giveback {summary.get('mean_giveback_pct')}%",
        f"oracle price-peak exit upper bound {oracle}% · gap {round(oracle - baseline, 2)}pp",
        "big-win peak: W3 RV rebound + leading/leading/leading · spread stays pullback/mixed",
        "extension / hook_extension_peak triggers ≈0% in hold window",
    ]
    if best_rule:
        reasons.append(
            f"best overall rule {best_rule.get('rule_id')} mean {best_rule.get('mean_net_pct')}% "
            f"Δ {best_rule.get('delta_vs_baseline_pp')}pp · trigger {best_rule.get('trigger_rate_pct')}%"
        )
    if best_profit:
        reasons.append(
            f"best profit-gated {best_profit.get('rule_id')} mean {best_profit.get('mean_net_pct')}% "
            f"Δ {best_profit.get('delta_vs_baseline_pp')}pp · trigger {best_profit.get('trigger_rate_pct')}%"
        )

    return {
        "schema": "abc_v3_f1_peak_exit_study-v2",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "contract": {
            "matcher": "w3_rv_hl_abc_v3_f1 · skip09 · min_poll_idx>=1",
            "exit_baseline": f"hold_{ABC_V3_HOLD_DAYS}d → 13:30",
            "peak_definition": "max intraday close from entry poll through scheduled exit poll",
            "cost_round_trip_pct": ROUND_TRIP_COST_PCT,
        },
        "window": {
            "date_start": sample_dates[0],
            "date_end": sample_dates[-1],
            "n_trading_days": len(sample_dates),
            "long_window": long_window,
            "intraday_tail_days": None if long_window else intraday_tail_days,
            "requested_date_start": date_start,
            "requested_date_end": end,
        },
        "panel_meta": {
            "n_frames": len(frames),
            "n_candidates": len(candidates),
            "n_trades": len(rows),
        },
        "summary": summary,
        "exit_rules": exit_grid,
        "profit_gated_rules": sorted(
            profit_rules,
            key=lambda x: float(x.get("mean_net_pct") or 0.0),
            reverse=True,
        ),
        "top_trades": top_trade_examples(rows, n=12),
        "verdict": {
            "summary": (
                f"ABC v3+f1 peak study · n={summary.get('n')} · hold3d {baseline}% · "
                f"oracle {oracle}% · best {((best_rule or {}).get('rule_id') or '—')} · "
                f"best profit-gated {((best_profit or {}).get('rule_id') or '—')}"
            ),
            "reasons": reasons,
            "recommend_rules": [
                "profit5_w3_or_w5_mv_d1",
                "profit5_w3_or_w5_mv_d2",
                "w3_delta_gte_3",
                "pullback_to_mixed",
            ],
            "avoid_rules": ["w3_rv_below_100", "all_leading_q", "spread_extension", "w3_mv_d1_ungated"],
        },
    }


def render_abc_v3_f1_peak_exit_study_md(payload: dict[str, Any]) -> str:
    w = payload.get("window") or {}
    c = payload.get("contract") or {}
    s = payload.get("summary") or {}
    v = payload.get("verdict") or {}
    panel = payload.get("panel_meta") or {}

    lines = [
        "# ABC v3+f1 · 价峰 WMA 形态 + 出场规则",
        "",
        f"> {v.get('summary', '')}",
        "",
        "## 契约",
        "",
        f"- Matcher: **{c.get('matcher')}**",
        f"- 基线出场: **{c.get('exit_baseline')}**",
        f"- 价峰定义: {c.get('peak_definition')}",
        f"- 窗口: **{w.get('date_start')} → {w.get('date_end')}** ({w.get('n_trading_days')} 交易日)",
        f"- 样本: candidates={panel.get('n_candidates')} · trades={panel.get('n_trades')}",
        "",
        "## 整体",
        "",
        f"- 均净报酬 **{s.get('mean_net_pct')}%** · 胜率 {s.get('win_rate_pct')}%",
        f"- 均 MFE **{s.get('mean_mfe_pct')}%** · 均回吐 **{s.get('giveback_pct') if False else s.get('mean_giveback_pct')}%**",
        f"- 价峰=出场 poll: {s.get('peak_at_exit_pct')}%",
        f"- 理论价峰出场上限 **{s.get('oracle_peak_exit_mean_pct')}%**",
        "",
        "## 大赚单（≥5%）价峰形态",
        "",
    ]

    def _spread_table(key: str) -> list[str]:
        rows = s.get(key) or []
        if not rows:
            return ["（无）", ""]
        out = ["| key | n | % |", "|-----|--:|--:|"]
        for row in rows:
            out.append(f"| {row.get('key')} | {row.get('n')} | {row.get('pct')} |")
        out.append("")
        return out

    lines.extend(_spread_table("big_win_peak_spread"))
    lines.append("### WMA 象限组合")
    lines.extend(_spread_table("big_win_wma_combo"))
    delta = s.get("delta_big_win") or {}
    lines.extend(
        [
            "### 进场→价峰变化（大赚单）",
            "",
            f"- W3 RV delta **{delta.get('w3_rv_delta')}** · W20 RV delta **{delta.get('w20_rv_delta')}**",
            f"- 价峰 RV 均值: W20={delta.get('peak_w20_rv')} · W5={delta.get('peak_w5_rv')} · W3={delta.get('peak_w3_rv')}",
            "",
            "## 出场规则反事实（WMA 形态）",
            "",
            "| rule | trigger% | mean net% | Δ vs hold | win% | save vs hold |",
            "|------|----------:|----------:|----------:|-----:|-------------:|",
        ]
    )
    for row in payload.get("exit_rules") or []:
        if row.get("family") == "profit_gated":
            continue
        lines.append(
            f"| {row.get('rule_id')} | {row.get('trigger_rate_pct')} | {row.get('mean_net_pct')} | "
            f"{row.get('delta_vs_baseline_pp')} | {row.get('win_rate_pct')} | "
            f"{row.get('mean_save_vs_hold_pct')} |"
        )

    lines.extend(
        [
            "",
            "## 止盈层 · gross 达标 + MV 连降（用户假设）",
            "",
            "| rule | trigger% | mean net% | Δ vs hold | win% | save vs hold |",
            "|------|----------:|----------:|----------:|-----:|-------------:|",
        ]
    )
    for row in payload.get("profit_gated_rules") or payload.get("exit_rules") or []:
        if row.get("family") != "profit_gated":
            continue
        lines.append(
            f"| {row.get('rule_id')} | {row.get('trigger_rate_pct')} | {row.get('mean_net_pct')} | "
            f"{row.get('delta_vs_baseline_pp')} | {row.get('win_rate_pct')} | "
            f"{row.get('mean_save_vs_hold_pct')} |"
        )

    lines.extend(["", "## Top trades", "", "| 代码 | 进场 | net% | MFE% | 回吐% | 价峰 spread | WMA combo |", "|------|------|-----:|-----:|------:|-------------|-----------|"])
    for row in payload.get("top_trades") or []:
        lines.append(
            f"| {row.get('stock_id')} | {row.get('entry_date')} {row.get('entry_minute')} | "
            f"{row.get('net_pct')} | {row.get('mfe_pct')} | {row.get('giveback_pct')} | "
            f"{row.get('peak_spread')} | {row.get('peak_wma_combo')} |"
        )

    lines.extend(["", "## Verdict", ""])
    for reason in v.get("reasons") or []:
        lines.append(f"- {reason}")
    rec = v.get("recommend_rules") or []
    avoid = v.get("avoid_rules") or []
    if rec:
        lines.append(f"- 可继续验证: {', '.join(rec)}")
    if avoid:
        lines.append(f"- 避免单独使用: {', '.join(avoid)}")
    lines.extend(["", "模組：`scripts/research/archive/run_abc_v3_f1_peak_exit_study.py`", ""])
    return "\n".join(lines)
