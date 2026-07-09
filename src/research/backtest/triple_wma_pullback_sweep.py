"""Triple-WMA pullback entry · W20+W5 strong · W3 weak vs lead_pullback_buy.

Hypothesis: WMA(3) dip while WMA(5)/WMA(20) remain strong captures intraday
relative lows better than dual-WMA lead_pullback (W20 Leading · W5-W20 spread).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any, Callable, Literal

from market_benchmark import load_benchmark_close
from project_config import DEFAULT_ETF_CODES
from research.backtest.dual_wma_signal_backtest import (
    ROUND_TRIP_COST_PCT,
    _build_stock_frame_maps,
    _exit_frame_idx,
    _matches_lead_pullback,
    _trade_return,
    default_lead_pullback_entry,
)
from research.backtest.finpilot_local_backtest import load_price_panels
from rrg_universe_intraday_panel import (
    WMA_LONG_LENGTH,
    WMA_MICRO_LENGTH,
    WMA_SHORT_LENGTH,
    WMA_ULTRA_LENGTH,
    build_dual_wma_intraday_trajectories,
)

def micro_leg_key(length: int) -> str:
    return f"w{length}"

_STRONG = frozenset({"leading", "improving"})
_WEAK = frozenset({"weakening", "lagging"})

PatternId = Literal[
    "triple_w3_dip",
    "triple_w3_dip_leading_only",
    "triple_w3_rv_high_low",
    "lead_pullback_buy",
]


def _q(point: dict[str, Any], leg: str) -> str | None:
    leg_d = point.get(leg) or {}
    q = leg_d.get("quadrant")
    return str(q) if q else None


def _rv(point: dict[str, Any], leg: str) -> float | None:
    v = (point.get(leg) or {}).get("rs_ratio")
    return float(v) if v is not None else None


def _mv(point: dict[str, Any], leg: str) -> float | None:
    v = (point.get(leg) or {}).get("rs_momentum")
    return float(v) if v is not None else None


def _w2_key() -> str:
    return micro_leg_key(WMA_ULTRA_LENGTH)


def matches_structural_w20_w5(p: dict[str, Any]) -> bool:
    """W20/W5 ∈ strong · RV>100."""
    if not p.get("fresh"):
        return False
    w20q, w5q = _q(p, "w20"), _q(p, "w5")
    rv20, rv5 = _rv(p, "w20"), _rv(p, "w5")
    if not w20q or not w5q or rv20 is None or rv5 is None:
        return False
    return w20q in _STRONG and w5q in _STRONG and rv20 > 100.0 and rv5 > 100.0


def matches_micro_trigger(p: dict[str, Any], *, micro_length: int) -> bool:
    """Structural W20+W5 high · micro ∈ weak OR micro RV<100."""
    key = micro_leg_key(micro_length)
    if not matches_structural_w20_w5(p):
        return False
    leg = p.get(key)
    if not leg:
        return False
    mq = _q(p, key)
    rv_m = _rv(p, key)
    if mq is None or rv_m is None:
        return False
    return mq in _WEAK or rv_m < 100.0


def matches_micro_rv_only_trigger(p: dict[str, Any], *, micro_length: int) -> bool:
    """Structural + micro RV<100 only (numeric dip)."""
    key = micro_leg_key(micro_length)
    if not matches_structural_w20_w5(p):
        return False
    rv_m = _rv(p, key)
    return rv_m is not None and rv_m < 100.0


def matches_w2_rv_threshold(p: dict[str, Any], *, rv_max: float) -> bool:
    """Structural W20+W5 · WMA2 RV < rv_max (strict numeric dip)."""
    key = micro_leg_key(WMA_ULTRA_LENGTH)
    if not matches_structural_w20_w5(p):
        return False
    rv_m = _rv(p, key)
    return rv_m is not None and rv_m < rv_max


DEFAULT_W2_RV_THRESHOLDS: tuple[float, ...] = (100.0, 99.7, 99.5, 99.3, 99.0, 98.8)

DEFAULT_W2_RV_MAX_GRID: tuple[float, ...] = (100.0, 100.5)
DEFAULT_W2_RV_MIN_GRID: tuple[float | None, ...] = (None, 99.3, 99.5, 99.7)
DEFAULT_W2_MV_MIN_GRID: tuple[float, ...] = (100.0, 100.5, 101.0, 101.5, 102.0)

W2_SWEET_RV_MIN = 99.5
W2_SWEET_RV_MAX = 100.5
W2_SWEET_MV_MIN = 100.0


def matches_w2_rv_mv_band(
    p: dict[str, Any],
    *,
    rv_min: float | None,
    rv_max: float,
    mv_min: float,
) -> bool:
    """Structural · WMA2 RV band + MV floor (higher RV/MV = shallower dip + stronger turn)."""
    key = _w2_key()
    if not matches_structural_w20_w5(p):
        return False
    rv_m = _rv(p, key)
    mv_m = _mv(p, key)
    if rv_m is None or mv_m is None:
        return False
    if rv_m >= rv_max or mv_m <= mv_min:
        return False
    if rv_min is not None and rv_m < rv_min:
        return False
    return True


def matches_w2_improving(p: dict[str, Any]) -> bool:
    """Structural · WMA2 Improving quadrant (RV≤100 · MV>100)."""
    key = _w2_key()
    if not matches_structural_w20_w5(p):
        return False
    return _q(p, key) == "improving"


def matches_w2_sweet_spot(p: dict[str, Any]) -> bool:
    """Structural · WMA2 RV∈[99.5,100.5) · MV>100 (grid sweet spot)."""
    return matches_w2_rv_mv_band(
        p,
        rv_min=W2_SWEET_RV_MIN,
        rv_max=W2_SWEET_RV_MAX,
        mv_min=W2_SWEET_MV_MIN,
    )


def matches_w3_rv_hl_w2_sweet(p: dict[str, Any]) -> bool:
    """W3 RV high/low trigger · W2 sweet-spot quality filter."""
    return matches_triple_w3_rv_high_low(p) and matches_w2_sweet_spot(p)


def matches_w3_rv_hl_w2_mv_only(p: dict[str, Any]) -> bool:
    """W3 RV high/low · W2 MV>100 only (looser micro filter)."""
    if not matches_triple_w3_rv_high_low(p):
        return False
    mv_m = _mv(p, _w2_key())
    return mv_m is not None and mv_m > W2_SWEET_MV_MIN


def matches_w3_rv_hl_w3_mv_gt_100(p: dict[str, Any]) -> bool:
    """W3 RV high/low · W3 MV>100 (momentum on trigger leg)."""
    if not matches_triple_w3_rv_high_low(p):
        return False
    mv_m = _mv(p, micro_leg_key(WMA_MICRO_LENGTH))
    return mv_m is not None and mv_m > 100.0


def w2_rv_mv_grid_id(
    *,
    rv_min: float | None,
    rv_max: float,
    mv_min: float,
) -> str:
    lo = "none" if rv_min is None else f"{rv_min:g}"
    return f"w2_rv_{lo}_{rv_max:g}_mv_gt_{mv_min:g}"


def matches_triple_w3_dip(p: dict[str, Any], *, leading_only: bool = False) -> bool:
    """W20+W5 strong · W3 weak · fresh K."""
    if not p.get("fresh"):
        return False
    w3 = p.get("w3")
    if not w3:
        return False
    w20q = _q(p, "w20")
    w5q = _q(p, "w5")
    w3q = _q(p, "w3")
    if not w20q or not w5q or not w3q:
        return False
    if leading_only:
        if w20q != "leading" or w5q != "leading":
            return False
    else:
        if w20q not in _STRONG or w5q not in _STRONG:
            return False
    return w3q in _WEAK


def matches_triple_w3_rv_high_low(p: dict[str, Any]) -> bool:
    """W20 RV>100 · W5 RV>100 · W3 RV<100 (user '都高/低' numeric)."""
    if not p.get("fresh") or not p.get("w3"):
        return False
    rv20, rv5, rv3 = _rv(p, "w20"), _rv(p, "w5"), _rv(p, "w3")
    if rv20 is None or rv5 is None or rv3 is None:
        return False
    return rv20 > 100.0 and rv5 > 100.0 and rv3 < 100.0


W3_ABC_W20_W3_SPREAD_MAX = 6.0
W3_ABC_W5_RV_MAX = 101.0
W3_ABC_V3_W20_RV_MAX = 104.0
W3_ABC_F1_W5_W3_MV_SPREAD_MAX = 0.01  # H-ABC-FILTER-1 · avoid W5 MV > W3 MV


def matches_w3_rv_hl_abc_v2(
    p: dict[str, Any],
    *,
    w20_w3_spread_max: float = W3_ABC_W20_W3_SPREAD_MAX,
    w5_rv_max: float = W3_ABC_W5_RV_MAX,
) -> bool:
    """W3 RV high/low · A+B+C: spread≤6 · Leading/Weakening · W5 RV≤101."""
    if not matches_triple_w3_rv_high_low(p):
        return False
    if _q(p, "w20") != "leading" or _q(p, "w5") != "weakening":
        return False
    rv20, rv5, rv3 = _rv(p, "w20"), _rv(p, "w5"), _rv(p, "w3")
    if rv20 is None or rv5 is None or rv3 is None:
        return False
    if float(rv5) > w5_rv_max:
        return False
    return float(rv20) - float(rv3) <= w20_w3_spread_max


def matches_w3_rv_hl_abc_v3(
    p: dict[str, Any],
    *,
    w20_w3_spread_max: float = W3_ABC_W20_W3_SPREAD_MAX,
    w5_rv_max: float = W3_ABC_W5_RV_MAX,
    w20_rv_max: float = W3_ABC_V3_W20_RV_MAX,
    require_lead_pullback: bool = True,
) -> bool:
    """ABC v2 · D+E: W20 RV≤104 · trade_signal=lead_pullback_buy."""
    if not matches_w3_rv_hl_abc_v2(
        p, w20_w3_spread_max=w20_w3_spread_max, w5_rv_max=w5_rv_max
    ):
        return False
    rv20 = _rv(p, "w20")
    if rv20 is None or float(rv20) > w20_rv_max:
        return False
    if require_lead_pullback and p.get("trade_signal") != "lead_pullback_buy":
        return False
    return True


def matches_w5_mv_lte_w3_mv(
    p: dict[str, Any],
    *,
    spread_max: float = W3_ABC_F1_W5_W3_MV_SPREAD_MAX,
) -> bool:
    """H-ABC-FILTER-1: W5 RS-momentum must not exceed W3 (no mid-term MV overhang)."""
    mv5, mv3 = _mv(p, "w5"), _mv(p, "w3")
    if mv5 is None or mv3 is None:
        return False
    return float(mv5) - float(mv3) < spread_max


def matches_w3_rv_hl_abc_v3_f1(
    p: dict[str, Any],
    *,
    w20_w3_spread_max: float = W3_ABC_W20_W3_SPREAD_MAX,
    w5_rv_max: float = W3_ABC_W5_RV_MAX,
    w20_rv_max: float = W3_ABC_V3_W20_RV_MAX,
    w5_w3_mv_spread_max: float = W3_ABC_F1_W5_W3_MV_SPREAD_MAX,
    require_lead_pullback: bool = True,
) -> bool:
    """ABC v3 + F: avoid W5 MV > W3 MV at entry poll."""
    if not matches_w3_rv_hl_abc_v3(
        p,
        w20_w3_spread_max=w20_w3_spread_max,
        w5_rv_max=w5_rv_max,
        w20_rv_max=w20_rv_max,
        require_lead_pullback=require_lead_pullback,
    ):
        return False
    return matches_w5_mv_lte_w3_mv(p, spread_max=w5_w3_mv_spread_max)


def matches_lead_pullback(p: dict[str, Any]) -> bool:
    ep = default_lead_pullback_entry()
    return _matches_lead_pullback(
        p,
        pullback_min=ep.pullback_min,
        pullback_max=ep.pullback_max,
        require_w5_improving=ep.require_w5_improving,
    )


def _pattern_fn(pattern_id: PatternId) -> Callable[[dict[str, Any]], bool]:
    fns: dict[PatternId, Callable[[dict[str, Any]], bool]] = {
        "triple_w3_dip": lambda p: matches_triple_w3_dip(p, leading_only=False),
        "triple_w3_dip_leading_only": lambda p: matches_triple_w3_dip(p, leading_only=True),
        "triple_w3_rv_high_low": matches_triple_w3_rv_high_low,
        "lead_pullback_buy": matches_lead_pullback,
    }
    return fns[pattern_id]


def _collect_pattern_events(
    trajectories: list[dict[str, Any]],
    frames: list[dict[str, str]],
    pattern_id: PatternId,
) -> list[tuple[str, int]]:
    match = _pattern_fn(pattern_id)
    frame_ids = [f["id"] for f in frames]
    events: list[tuple[str, int]] = []
    seen_day: set[tuple[str, str]] = set()
    for t in trajectories:
        sid = t["stock_id"]
        by_id = {p["frame_id"]: p for p in t["points"]}
        for i, fid in enumerate(frame_ids):
            p = by_id.get(fid)
            if not p or not match(p):
                continue
            d = frames[i]["date"]
            key = (sid, d)
            if key in seen_day:
                continue
            seen_day.add(key)
            events.append((sid, i))
    return events


def _summarize_trades(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"n": 0}
    rets = [float(r["net_pct"]) for r in rows]
    wins = sum(1 for x in rets if x > 0)
    return {
        "n": len(rows),
        "mean_ret_3d_net_pct": round(sum(rets) / len(rets), 3),
        "win_rate_pct": round(wins / len(rets) * 100, 2),
        "median_ret_3d_net_pct": round(sorted(rets)[len(rets) // 2], 3),
        "mean_excess_3d_pct": round(
            sum(float(r.get("excess_pct") or 0) for r in rows) / len(rows), 3
        ),
    }


def backtest_pullback_patterns(
    conn: sqlite3.Connection,
    *,
    dates: list[str],
    patterns: tuple[PatternId, ...] = (
        "triple_w3_dip",
        "triple_w3_dip_leading_only",
        "triple_w3_rv_high_low",
        "lead_pullback_buy",
    ),
    exit_rule: str = "hold_3d",
    etf_codes: tuple[str, ...] = DEFAULT_ETF_CODES,
) -> dict[str, Any]:
    frames, trajectories, meta = build_dual_wma_intraday_trajectories(
        conn,
        dates=dates,
        etf_codes=etf_codes,
        short_length=WMA_SHORT_LENGTH,
        long_length=WMA_LONG_LENGTH,
        micro_length=WMA_MICRO_LENGTH,
    )
    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index).astype(float)
    stock_maps = _build_stock_frame_maps(trajectories, frames)

    pattern_rows: dict[str, list[dict[str, Any]]] = {p: [] for p in patterns}
    overlap: dict[str, int] = {"triple_and_lead_pullback_same_day": 0}

    triple_events = set()
    lp_events = set()
    for pat in patterns:
        events = _collect_pattern_events(trajectories, frames, pat)
        for sid, ei in events:
            pts = stock_maps.get(sid)
            if not pts:
                continue
            exit_idx = _exit_frame_idx(exit_rule, ei, frames, pts)
            tr = _trade_return(conn, bench, stock_maps, sid, ei, exit_idx, frames)
            if tr is None:
                continue
            p0 = pts[ei] or {}
            row = {
                **tr,
                "stock_id": sid,
                "entry_date": frames[ei]["date"],
                "entry_minute": frames[ei]["minute"],
                "w20_q": _q(p0, "w20"),
                "w5_q": _q(p0, "w5"),
                "w3_q": _q(p0, "w3"),
                "spread_class": p0.get("spread_class"),
            }
            pattern_rows[pat].append(row)
            if pat == "triple_w3_dip":
                triple_events.add((sid, frames[ei]["date"]))
            if pat == "lead_pullback_buy":
                lp_events.add((sid, frames[ei]["date"]))

    overlap["triple_and_lead_pullback_same_day"] = len(triple_events & lp_events)
    overlap["triple_only"] = len(triple_events - lp_events)
    overlap["lead_pullback_only"] = len(lp_events - triple_events)

    summaries = {pat: _summarize_trades(rows) for pat, rows in pattern_rows.items()}
    return {
        "dates": dates,
        "meta": meta,
        "exit_rule": exit_rule,
        "round_trip_cost_pct": ROUND_TRIP_COST_PCT,
        "summaries": summaries,
        "overlap": overlap,
    }


def verdict_triple_pullback(payload: dict[str, Any]) -> dict[str, Any]:
    s = payload.get("summaries") or {}
    triple = s.get("triple_w3_dip") or {}
    leading = s.get("triple_w3_dip_leading_only") or {}
    rvhl = s.get("triple_w3_rv_high_low") or {}
    lp = s.get("lead_pullback_buy") or {}

    def better(a: dict[str, Any], b: dict[str, Any]) -> bool:
        if int(a.get("n") or 0) < 30 or int(b.get("n") or 0) < 30:
            return False
        return float(a.get("mean_ret_3d_net_pct") or -999) > float(
            b.get("mean_ret_3d_net_pct") or -999
        ) and float(a.get("win_rate_pct") or 0) >= float(b.get("win_rate_pct") or 0) - 5

    pick = "lead_pullback_buy"
    reasons: list[str] = []

    if better(triple, lp):
        pick = "triple_w3_dip"
        reasons.append("triple W3 dip beats lead_pullback on 3d net return + win rate")
    elif better(rvhl, lp):
        pick = "triple_w3_rv_high_low"
        reasons.append("RV-high/low triple pattern beats lead_pullback")
    else:
        reasons.append("lead_pullback_buy baseline ≥ triple W3 variants on 3d hold")

    if int(triple.get("n") or 0) > int(lp.get("n") or 0) * 1.5:
        reasons.append(
            f"triple fires more often (n={triple.get('n')} vs LP n={lp.get('n')}) — check overlap"
        )

    ov = payload.get("overlap") or {}
    return {
        "recommend_entry": pick,
        "reasons": reasons,
        "summary": (
            f"3d hold · triple_dip n={triple.get('n')} ret={triple.get('mean_ret_3d_net_pct')}% · "
            f"rv_high_low n={rvhl.get('n')} ret={rvhl.get('mean_ret_3d_net_pct')}% · "
            f"lead_pullback n={lp.get('n')} ret={lp.get('mean_ret_3d_net_pct')}% · "
            f"pick **{pick}**"
        ),
        "triple_leading_only": leading,
        "triple_rv_high_low": rvhl,
        "overlap": ov,
    }


def run_triple_wma_pullback_sweep(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2025-01-01",
    date_end: str | None = None,
    intraday_tail_days: int = 60,
    etf_codes: tuple[str, ...] = DEFAULT_ETF_CODES,
) -> dict[str, Any]:
    close, _, _ = load_price_panels(conn)
    full_dates = close.index.astype(str).tolist()
    end = date_end or full_dates[-1]
    trade_dates = [d for d in full_dates if date_start <= d <= end]
    if intraday_tail_days > 0 and len(trade_dates) > intraday_tail_days:
        sample_dates = trade_dates[-intraday_tail_days:]
    else:
        sample_dates = trade_dates

    bt = backtest_pullback_patterns(
        conn, dates=sample_dates, etf_codes=etf_codes
    )
    verdict = verdict_triple_pullback(bt)
    return {
        "schema": "triple_wma_pullback_sweep-v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "date_start": date_start,
        "date_end": end,
        "intraday_sample_dates": sample_dates,
        "micro_length": WMA_MICRO_LENGTH,
        "short_length": WMA_SHORT_LENGTH,
        "long_length": WMA_LONG_LENGTH,
        "backtest": bt,
        "verdict": verdict,
    }


def render_triple_wma_pullback_md(payload: dict[str, Any]) -> str:
    v = payload.get("verdict") or {}
    bt = payload.get("backtest") or {}
    s = bt.get("summaries") or {}
    lines = [
        "# Triple-WMA pullback · W20+W5 高 · W3 低",
        "",
        f"> {v.get('summary', '')}",
        "",
        f"- Sample: **{len(payload.get('intraday_sample_dates') or [])}** trade dates",
        f"- Legs: WMA({payload.get('micro_length')}) · WMA({payload.get('short_length')}) · "
        f"WMA({payload.get('long_length')}) · exit **{bt.get('exit_rule')}**",
        "",
        "## Pattern definitions",
        "",
        "| ID | 條件 |",
        "|----|------|",
        "| `triple_w3_dip` | W20∈{領先,轉強} · W5∈{領先,轉強} · W3∈{轉弱,落後} · fresh K |",
        "| `triple_w3_dip_leading_only` | W20=領先 · W5=領先 · W3∈{轉弱,落後} |",
        "| `triple_w3_rv_high_low` | W20 RV>100 · W5 RV>100 · W3 RV<100 |",
        "| `lead_pullback_buy` | 現行 dual-WMA · W20 Leading · spread pullback |",
        "",
        "## 3d hold outcomes",
        "",
        "| Pattern | n | mean ret net% | win% | mean excess% |",
        "|---------|--:|--------------:|-----:|-------------:|",
    ]
    for pid in (
        "triple_w3_dip",
        "triple_w3_dip_leading_only",
        "triple_w3_rv_high_low",
        "lead_pullback_buy",
    ):
        row = s.get(pid) or {}
        lines.append(
            f"| {pid} | {row.get('n', 0)} | {row.get('mean_ret_3d_net_pct', '—')} | "
            f"{row.get('win_rate_pct', '—')} | {row.get('mean_excess_3d_pct', '—')} |"
        )
    ov = v.get("overlap") or bt.get("overlap") or {}
    lines.extend(
        [
            "",
            "## Overlap (triple_w3_dip vs lead_pullback)",
            "",
            f"- Same stock-day: **{ov.get('triple_and_lead_pullback_same_day', 0)}**",
            f"- Triple only: **{ov.get('triple_only', 0)}**",
            f"- Lead pullback only: **{ov.get('lead_pullback_only', 0)}**",
            "",
            "## Verdict",
            "",
            f"- **Recommend**: {v.get('recommend_entry')}",
        ]
    )
    for r in v.get("reasons") or []:
        lines.append(f"- {r}")
    lines.extend(["", "模組：`scripts/run_triple_wma_pullback_sweep.py`"])
    return "\n".join(lines)


MicroPatternId = Literal[
    "trigger_w2",
    "trigger_w3",
    "trigger_w2_rv_only",
    "trigger_w3_rv_only",
]


def _micro_pattern_fn(pattern_id: MicroPatternId) -> Callable[[dict[str, Any]], bool]:
    return {
        "trigger_w2": lambda p: matches_micro_trigger(p, micro_length=WMA_ULTRA_LENGTH),
        "trigger_w3": lambda p: matches_micro_trigger(p, micro_length=WMA_MICRO_LENGTH),
        "trigger_w2_rv_only": lambda p: matches_micro_rv_only_trigger(
            p, micro_length=WMA_ULTRA_LENGTH
        ),
        "trigger_w3_rv_only": lambda p: matches_micro_rv_only_trigger(
            p, micro_length=WMA_MICRO_LENGTH
        ),
    }[pattern_id]


def backtest_micro_trigger_compare(
    conn: sqlite3.Connection,
    *,
    dates: list[str],
    exit_rule: str = "hold_3d",
    etf_codes: tuple[str, ...] = DEFAULT_ETF_CODES,
) -> dict[str, Any]:
    frames, trajectories, meta = build_dual_wma_intraday_trajectories(
        conn,
        dates=dates,
        etf_codes=etf_codes,
        micro_lengths=(WMA_ULTRA_LENGTH, WMA_MICRO_LENGTH),
    )
    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index).astype(float)
    stock_maps = _build_stock_frame_maps(trajectories, frames)

    patterns: tuple[MicroPatternId, ...] = (
        "trigger_w2",
        "trigger_w3",
        "trigger_w2_rv_only",
        "trigger_w3_rv_only",
    )
    pattern_rows: dict[str, list[dict[str, Any]]] = {p: [] for p in patterns}
    event_sets: dict[str, set[tuple[str, str]]] = {p: set() for p in patterns}

    for pat in patterns:
        match = _micro_pattern_fn(pat)
        frame_ids = [f["id"] for f in frames]
        seen_day: set[tuple[str, str]] = set()
        for t in trajectories:
            sid = t["stock_id"]
            by_id = {p["frame_id"]: p for p in t["points"]}
            for i, fid in enumerate(frame_ids):
                p = by_id.get(fid)
                if not p or not match(p):
                    continue
                d = frames[i]["date"]
                key = (sid, d)
                if key in seen_day:
                    continue
                seen_day.add(key)
                pts = stock_maps.get(sid)
                if not pts:
                    continue
                exit_idx = _exit_frame_idx(exit_rule, i, frames, pts)
                tr = _trade_return(conn, bench, stock_maps, sid, i, exit_idx, frames)
                if tr is None:
                    continue
                p0 = pts[i] or {}
                pattern_rows[pat].append({**tr, "stock_id": sid, "entry_date": d})
                event_sets[pat].add(key)

    summaries = {pat: _summarize_trades(rows) for pat, rows in pattern_rows.items()}
    w2, w3 = event_sets["trigger_w2"], event_sets["trigger_w3"]
    return {
        "dates": dates,
        "meta": meta,
        "exit_rule": exit_rule,
        "structural": (
            "W20/W5∈{leading,improving}·RV>100 · trigger: micro∈{weakening,lagging} OR RV<100"
        ),
        "summaries": summaries,
        "overlap": {
            "w2_w3_same_day": len(w2 & w3),
            "w2_only": len(w2 - w3),
            "w3_only": len(w3 - w2),
        },
    }


def verdict_micro_trigger_compare(bt: dict[str, Any]) -> dict[str, Any]:
    s = bt.get("summaries") or {}
    w2 = s.get("trigger_w2") or {}
    w3 = s.get("trigger_w3") or {}
    w2r = s.get("trigger_w2_rv_only") or {}
    w3r = s.get("trigger_w3_rv_only") or {}

    def score(row: dict[str, Any]) -> float:
        n = int(row.get("n") or 0)
        if n < 30:
            return -999.0
        ret = float(row.get("mean_ret_3d_net_pct") or 0)
        win = float(row.get("win_rate_pct") or 0)
        # penalize extreme fire rate (too many events)
        fire_penalty = 0.0
        if n > 1200:
            fire_penalty = (n - 1200) * 0.0005
        return ret + (win - 50) * 0.02 - fire_penalty

    candidates = {
        "WMA2 trigger": (w2, score(w2)),
        "WMA3 trigger": (w3, score(w3)),
        "WMA2 RV-only": (w2r, score(w2r)),
        "WMA3 RV-only": (w3r, score(w3r)),
    }
    reasons: list[str] = []
    best_name = max(candidates, key=lambda k: candidates[k][1])
    best_row, best_sc = candidates[best_name]

    w2_sc = score(w2)
    w3_sc = score(w3)
    if w2_sc >= w3_sc:
        recommend = "WMA2"
        reasons.append(
            f"Under quad∨RV trigger: WMA2 ret {w2.get('mean_ret_3d_net_pct')}% "
            f"win {w2.get('win_rate_pct')}% vs WMA3 {w3.get('mean_ret_3d_net_pct')}% "
            f"win {w3.get('win_rate_pct')}% (score {w2_sc:.2f} vs {w3_sc:.2f})"
        )
    else:
        recommend = "WMA3"
        reasons.append(
            f"WMA3 better composite score {w3_sc:.2f} vs WMA2 {w2_sc:.2f}"
        )

    if int(w2.get("n") or 0) > int(w3.get("n") or 0) * 1.15:
        reasons.append(
            f"WMA2 fires more ({w2.get('n')} vs {w3.get('n')}) — higher noise risk"
        )
    if float(w2r.get("mean_ret_3d_net_pct") or 0) > float(w3r.get("mean_ret_3d_net_pct") or 0):
        reasons.append(
            f"RV-only sub-variant: WMA2 ret {w2r.get('mean_ret_3d_net_pct')}% "
            f"(n={w2r.get('n')}) beats WMA3 RV-only {w3r.get('mean_ret_3d_net_pct')}% "
            f"(n={w3r.get('n')})"
        )

    ov = bt.get("overlap") or {}
    return {
        "recommend_trigger": recommend,
        "best_variant": best_name,
        "scores": {k: round(v[1], 2) for k, v in candidates.items()},
        "reasons": reasons,
        "summary": (
            f"Structural W20+W5 high · W2 n={w2.get('n')} ret={w2.get('mean_ret_3d_net_pct')}% "
            f"win={w2.get('win_rate_pct')}% · "
            f"W3 n={w3.get('n')} ret={w3.get('mean_ret_3d_net_pct')}% win={w3.get('win_rate_pct')}% · "
            f"recommend **{recommend}**"
        ),
        "overlap": ov,
    }


def run_micro_trigger_compare_sweep(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2025-01-01",
    date_end: str | None = None,
    intraday_tail_days: int = 60,
    etf_codes: tuple[str, ...] = DEFAULT_ETF_CODES,
) -> dict[str, Any]:
    close, _, _ = load_price_panels(conn)
    full_dates = close.index.astype(str).tolist()
    end = date_end or full_dates[-1]
    trade_dates = [d for d in full_dates if date_start <= d <= end]
    sample_dates = (
        trade_dates[-intraday_tail_days:]
        if intraday_tail_days > 0 and len(trade_dates) > intraday_tail_days
        else trade_dates
    )
    bt = backtest_micro_trigger_compare(conn, dates=sample_dates, etf_codes=etf_codes)
    verdict = verdict_micro_trigger_compare(bt)
    return {
        "schema": "triple_micro_trigger_compare-v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "date_start": date_start,
        "date_end": end,
        "intraday_sample_dates": sample_dates,
        "backtest": bt,
        "verdict": verdict,
    }


def render_micro_trigger_compare_md(payload: dict[str, Any]) -> str:
    v = payload.get("verdict") or {}
    bt = payload.get("backtest") or {}
    s = bt.get("summaries") or {}
    lines = [
        "# Micro trigger compare · WMA2 vs WMA3",
        "",
        f"> {v.get('summary', '')}",
        "",
        "## Structural (both patterns)",
        "",
        "- WMA20 ∈ {領先, 轉強} 且 RV > 100",
        "- WMA5 ∈ {領先, 轉強} 且 RV > 100",
        "",
        "## Trigger variants",
        "",
        "| ID | 觸發 |",
        "|----|------|",
        "| trigger_w2 / w3 | micro ∈ {轉弱,落後} **或** RV < 100 |",
        "| trigger_w2_rv_only / w3_rv_only | micro RV < 100 only |",
        "",
        f"- Exit: **{bt.get('exit_rule')}** · sample **{len(payload.get('intraday_sample_dates') or [])}** days",
        "",
        "## 3d hold outcomes",
        "",
        "| Pattern | n | mean ret net% | win% | mean excess% |",
        "|---------|--:|--------------:|-----:|-------------:|",
    ]
    for pid in ("trigger_w2", "trigger_w3", "trigger_w2_rv_only", "trigger_w3_rv_only"):
        row = s.get(pid) or {}
        lines.append(
            f"| {pid} | {row.get('n', 0)} | {row.get('mean_ret_3d_net_pct', '—')} | "
            f"{row.get('win_rate_pct', '—')} | {row.get('mean_excess_3d_pct', '—')} |"
        )
    ov = v.get("overlap") or bt.get("overlap") or {}
    lines.extend(
        [
            "",
            "## Overlap (trigger_w2 vs trigger_w3)",
            "",
            f"- Same stock-day: **{ov.get('w2_w3_same_day', 0)}**",
            f"- W2 only: **{ov.get('w2_only', 0)}**",
            f"- W3 only: **{ov.get('w3_only', 0)}**",
            "",
            "## Verdict",
            "",
            f"- **Recommend trigger leg**: {v.get('recommend_trigger')}",
            f"- Best variant: {v.get('best_variant')}",
        ]
    )
    for r in v.get("reasons") or []:
        lines.append(f"- {r}")
    lines.extend(["", "模組：`scripts/run_triple_micro_trigger_compare.py`"])
    return "\n".join(lines)


def backtest_w2_rv_threshold_grid(
    conn: sqlite3.Connection,
    *,
    dates: list[str],
    rv_thresholds: tuple[float, ...] = DEFAULT_W2_RV_THRESHOLDS,
    exit_rule: str = "hold_3d",
    etf_codes: tuple[str, ...] = DEFAULT_ETF_CODES,
    include_baselines: bool = True,
) -> dict[str, Any]:
    frames, trajectories, meta = build_dual_wma_intraday_trajectories(
        conn,
        dates=dates,
        etf_codes=etf_codes,
        micro_lengths=(WMA_ULTRA_LENGTH, WMA_MICRO_LENGTH),
    )
    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index).astype(float)
    stock_maps = _build_stock_frame_maps(trajectories, frames)

    matchers: list[tuple[str, Callable[[dict[str, Any]], bool]]] = [
        (f"w2_rv_lt_{thr:g}", lambda p, t=thr: matches_w2_rv_threshold(p, rv_max=t))
        for thr in rv_thresholds
    ]
    if include_baselines:
        matchers.extend(
            [
                ("lead_pullback_buy", matches_lead_pullback),
                ("triple_w3_rv_high_low", matches_triple_w3_rv_high_low),
            ]
        )

    pattern_rows: dict[str, list[dict[str, Any]]] = {pid: [] for pid, _ in matchers}
    frame_ids = [f["id"] for f in frames]

    for pid, match in matchers:
        seen_day: set[tuple[str, str]] = set()
        for t in trajectories:
            sid = t["stock_id"]
            by_id = {p["frame_id"]: p for p in t["points"]}
            for i, fid in enumerate(frame_ids):
                p = by_id.get(fid)
                if not p or not match(p):
                    continue
                d = frames[i]["date"]
                key = (sid, d)
                if key in seen_day:
                    continue
                seen_day.add(key)
                pts = stock_maps.get(sid)
                if not pts:
                    continue
                exit_idx = _exit_frame_idx(exit_rule, i, frames, pts)
                tr = _trade_return(conn, bench, stock_maps, sid, i, exit_idx, frames)
                if tr is None:
                    continue
                p0 = pts[i] or {}
                pattern_rows[pid].append(
                    {
                        **tr,
                        "stock_id": sid,
                        "entry_date": d,
                        "w2_rv": _rv(p0, micro_leg_key(WMA_ULTRA_LENGTH)),
                    }
                )

    summaries = {pid: _summarize_trades(rows) for pid, rows in pattern_rows.items()}
    return {
        "dates": dates,
        "meta": meta,
        "exit_rule": exit_rule,
        "structural": "W20/W5∈{leading,improving}·RV>100",
        "trigger": "WMA2 RV < threshold",
        "rv_thresholds": list(rv_thresholds),
        "summaries": summaries,
    }


def verdict_w2_rv_strict(
    bt: dict[str, Any],
    *,
    focus_threshold: float = 99.3,
    min_n: int = 15,
) -> dict[str, Any]:
    s = bt.get("summaries") or {}
    focus_key = f"w2_rv_lt_{focus_threshold:g}"
    focus = s.get(focus_key) or {}

    w2_rows: list[tuple[float, dict[str, Any]]] = []
    for thr in bt.get("rv_thresholds") or []:
        key = f"w2_rv_lt_{float(thr):g}"
        row = s.get(key) or {}
        if int(row.get("n") or 0) >= min_n:
            w2_rows.append((float(thr), row))

    def rank_score(row: dict[str, Any]) -> float:
        n = int(row.get("n") or 0)
        ret = float(row.get("mean_ret_3d_net_pct") or 0)
        win = float(row.get("win_rate_pct") or 0)
        return ret + (win - 50) * 0.02 + min(n, 200) * 0.0002

    best_thr, best_row = max(w2_rows, key=lambda x: rank_score(x[1])) if w2_rows else (None, {})
    lp = s.get("lead_pullback_buy") or {}
    w3 = s.get("triple_w3_rv_high_low") or {}

    reasons: list[str] = []
    recommend = focus_threshold
    if best_thr is not None and best_thr != focus_threshold:
        if rank_score(best_row) > rank_score(focus) + 0.15:
            recommend = best_thr
            reasons.append(
                f"Grid best W2 RV<{best_thr:g} (ret {best_row.get('mean_ret_3d_net_pct')}%) "
                f"beats focus {focus_threshold:g} (ret {focus.get('mean_ret_3d_net_pct')}%)"
            )
        else:
            reasons.append(f"Focus W2 RV<{focus_threshold:g} near grid best RV<{best_thr:g}")

    if int(focus.get("n") or 0) < min_n:
        reasons.append(f"Focus threshold n={focus.get('n')} < {min_n} — sparse sample")

    f_ret = float(focus.get("mean_ret_3d_net_pct") or 0)
    lp_ret = float(lp.get("mean_ret_3d_net_pct") or 0)
    if f_ret > lp_ret:
        reasons.append(f"W2 RV<{focus_threshold:g} beats lead_pullback ({f_ret}% vs {lp_ret}%)")
    else:
        reasons.append(f"lead_pullback still ≥ W2 RV<{focus_threshold:g} ({lp_ret}% vs {f_ret}%)")

    looser = s.get("w2_rv_lt_100") or s.get("w2_rv_lt_100.0") or {}
    if int(looser.get("n") or 0) > int(focus.get("n") or 0) * 1.5:
        reasons.append(
            f"Stricter RV<{focus_threshold:g} cuts n {looser.get('n')} → {focus.get('n')}"
        )

    return {
        "recommend_rv_max": recommend,
        "focus_threshold": focus_threshold,
        "focus": focus,
        "grid_best_threshold": best_thr,
        "grid_best": best_row,
        "lead_pullback": lp,
        "triple_w3_rv_high_low": w3,
        "reasons": reasons,
        "summary": (
            f"W2 RV<{focus_threshold:g} · n={focus.get('n')} "
            f"ret={focus.get('mean_ret_3d_net_pct')}% win={focus.get('win_rate_pct')}% · "
            f"grid best RV<{best_thr} ret={best_row.get('mean_ret_3d_net_pct')}% · "
            f"recommend study **RV<{recommend}**"
        ),
    }


def run_w2_rv_strict_sweep(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2025-01-01",
    date_end: str | None = None,
    intraday_tail_days: int = 60,
    rv_thresholds: tuple[float, ...] = DEFAULT_W2_RV_THRESHOLDS,
    focus_threshold: float = 99.3,
    etf_codes: tuple[str, ...] = DEFAULT_ETF_CODES,
) -> dict[str, Any]:
    close, _, _ = load_price_panels(conn)
    full_dates = close.index.astype(str).tolist()
    end = date_end or full_dates[-1]
    trade_dates = [d for d in full_dates if date_start <= d <= end]
    sample_dates = (
        trade_dates[-intraday_tail_days:]
        if intraday_tail_days > 0 and len(trade_dates) > intraday_tail_days
        else trade_dates
    )
    bt = backtest_w2_rv_threshold_grid(
        conn,
        dates=sample_dates,
        rv_thresholds=rv_thresholds,
        etf_codes=etf_codes,
    )
    verdict = verdict_w2_rv_strict(bt, focus_threshold=focus_threshold)
    return {
        "schema": "w2_rv_strict_sweep-v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "date_start": date_start,
        "date_end": end,
        "intraday_sample_dates": sample_dates,
        "focus_threshold": focus_threshold,
        "backtest": bt,
        "verdict": verdict,
    }


def render_w2_rv_strict_md(payload: dict[str, Any]) -> str:
    v = payload.get("verdict") or {}
    bt = payload.get("backtest") or {}
    s = bt.get("summaries") or {}
    focus = payload.get("focus_threshold", 99.3)
    lines = [
        f"# WMA2 strict dip · RV < {focus}",
        "",
        f"> {v.get('summary', '')}",
        "",
        "## Structural (fixed)",
        "",
        "- WMA20 ∈ {領先, 轉強} 且 RV > 100",
        "- WMA5 ∈ {領先, 轉強} 且 RV > 100",
        "",
        "## Trigger sweep · WMA2 RV < threshold",
        "",
        f"- Exit: **{bt.get('exit_rule')}** · sample **{len(payload.get('intraday_sample_dates') or [])}** days",
        "",
        "| threshold | n | mean ret net% | win% | median ret% | mean excess% |",
        "|----------|--:|--------------:|-----:|------------:|-------------:|",
    ]
    for thr in bt.get("rv_thresholds") or []:
        key = f"w2_rv_lt_{float(thr):g}"
        row = s.get(key) or {}
        mark = " **" if float(thr) == float(focus) else ""
        end_mark = "**" if float(thr) == float(focus) else ""
        lines.append(
            f"|{mark}{thr}{end_mark} | {row.get('n', 0)} | {row.get('mean_ret_3d_net_pct', '—')} | "
            f"{row.get('win_rate_pct', '—')} | {row.get('median_ret_3d_net_pct', '—')} | "
            f"{row.get('mean_excess_3d_pct', '—')} |"
        )
    for pid in ("lead_pullback_buy", "triple_w3_rv_high_low"):
        row = s.get(pid) or {}
        lines.append(
            f"| _{pid}_ | {row.get('n', 0)} | {row.get('mean_ret_3d_net_pct', '—')} | "
            f"{row.get('win_rate_pct', '—')} | {row.get('median_ret_3d_net_pct', '—')} | "
            f"{row.get('mean_excess_3d_pct', '—')} |"
        )
    lines.extend(["", "## Verdict", "", f"- **Study threshold**: RV < {v.get('recommend_rv_max')}", ""])
    for r in v.get("reasons") or []:
        lines.append(f"- {r}")
    lines.extend(["", "模組：`scripts/run_w2_rv_strict_sweep.py`"])
    return "\n".join(lines)


def _sweet_spot_score(row: dict[str, Any], *, min_n: int) -> float:
    n = int(row.get("n") or 0)
    if n < min_n:
        return -999.0
    ret = float(row.get("mean_ret_3d_net_pct") or 0)
    win = float(row.get("win_rate_pct") or 0)
    n_bonus = min(n, 120) * 0.003
    return ret + (win - 50.0) * 0.025 + n_bonus


def _build_w2_rv_mv_matchers(
    *,
    rv_max_grid: tuple[float, ...],
    rv_min_grid: tuple[float | None, ...],
    mv_min_grid: tuple[float, ...],
    include_baselines: bool,
) -> list[tuple[str, Callable[[dict[str, Any]], bool], dict[str, Any]]]:
    matchers: list[tuple[str, Callable[[dict[str, Any]], bool], dict[str, Any]]] = []
    for rv_max in rv_max_grid:
        for rv_min in rv_min_grid:
            for mv_min in mv_min_grid:
                pid = w2_rv_mv_grid_id(rv_min=rv_min, rv_max=rv_max, mv_min=mv_min)
                meta = {"rv_min": rv_min, "rv_max": rv_max, "mv_min": mv_min}
                matchers.append(
                    (
                        pid,
                        lambda p, lo=rv_min, hi=rv_max, mv=mv_min: matches_w2_rv_mv_band(
                            p, rv_min=lo, rv_max=hi, mv_min=mv
                        ),
                        meta,
                    )
                )
    if include_baselines:
        matchers.extend(
            [
                (
                    "w2_rv_lt_100",
                    lambda p: matches_w2_rv_threshold(p, rv_max=100.0),
                    {"baseline": "w2_rv"},
                ),
                (
                    "w2_improving_quad",
                    matches_w2_improving,
                    {"baseline": "w2_quad"},
                ),
                (
                    "w2_rv_lt_100_mv_gt_100",
                    lambda p: matches_w2_rv_mv_band(p, rv_min=None, rv_max=100.0, mv_min=100.0),
                    {"baseline": "w2_rv_mv"},
                ),
                (
                    "triple_w3_rv_high_low",
                    matches_triple_w3_rv_high_low,
                    {"baseline": "w3"},
                ),
                (
                    "lead_pullback_buy",
                    matches_lead_pullback,
                    {"baseline": "lp"},
                ),
            ]
        )
    return matchers


def backtest_w2_rv_mv_grid(
    conn: sqlite3.Connection,
    *,
    dates: list[str],
    rv_max_grid: tuple[float, ...] = DEFAULT_W2_RV_MAX_GRID,
    rv_min_grid: tuple[float | None, ...] = DEFAULT_W2_RV_MIN_GRID,
    mv_min_grid: tuple[float, ...] = DEFAULT_W2_MV_MIN_GRID,
    exit_rule: str = "hold_3d",
    etf_codes: tuple[str, ...] = DEFAULT_ETF_CODES,
    include_baselines: bool = True,
) -> dict[str, Any]:
    frames, trajectories, meta = build_dual_wma_intraday_trajectories(
        conn,
        dates=dates,
        etf_codes=etf_codes,
        micro_lengths=(WMA_ULTRA_LENGTH, WMA_MICRO_LENGTH),
    )
    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index).astype(float)
    stock_maps = _build_stock_frame_maps(trajectories, frames)

    matchers = _build_w2_rv_mv_matchers(
        rv_max_grid=rv_max_grid,
        rv_min_grid=rv_min_grid,
        mv_min_grid=mv_min_grid,
        include_baselines=include_baselines,
    )
    pattern_rows: dict[str, list[dict[str, Any]]] = {pid: [] for pid, _, _ in matchers}
    pattern_meta = {pid: m for pid, _, m in matchers}
    frame_ids = [f["id"] for f in frames]

    for pid, match, _ in matchers:
        seen_day: set[tuple[str, str]] = set()
        for t in trajectories:
            sid = t["stock_id"]
            by_id = {p["frame_id"]: p for p in t["points"]}
            for i, fid in enumerate(frame_ids):
                p = by_id.get(fid)
                if not p or not match(p):
                    continue
                d = frames[i]["date"]
                key = (sid, d)
                if key in seen_day:
                    continue
                seen_day.add(key)
                pts = stock_maps.get(sid)
                if not pts:
                    continue
                exit_idx = _exit_frame_idx(exit_rule, i, frames, pts)
                tr = _trade_return(conn, bench, stock_maps, sid, i, exit_idx, frames)
                if tr is None:
                    continue
                w2k = _w2_key()
                pattern_rows[pid].append(
                    {
                        **tr,
                        "stock_id": sid,
                        "entry_date": d,
                        "w2_rv": _rv(p, w2k),
                        "w2_mv": _mv(p, w2k),
                    }
                )

    summaries = {pid: _summarize_trades(rows) for pid, rows in pattern_rows.items()}
    grid_cells: list[dict[str, Any]] = []
    for pid, _, meta_row in matchers:
        if meta_row.get("baseline"):
            continue
        row = summaries.get(pid) or {}
        grid_cells.append({**meta_row, "id": pid, **row})

    return {
        "dates": dates,
        "meta": meta,
        "exit_rule": exit_rule,
        "structural": "W20/W5∈{leading,improving}·RV>100",
        "trigger": "WMA2 RV band · MV floor",
        "rv_max_grid": list(rv_max_grid),
        "rv_min_grid": [None if x is None else float(x) for x in rv_min_grid],
        "mv_min_grid": list(mv_min_grid),
        "grid_cells": grid_cells,
        "summaries": summaries,
    }


def verdict_w2_rv_mv_sweet_spot(
    bt: dict[str, Any],
    *,
    min_n: int = 25,
) -> dict[str, Any]:
    cells = bt.get("grid_cells") or []
    ranked = sorted(
        cells,
        key=lambda c: _sweet_spot_score(c, min_n=min_n),
        reverse=True,
    )
    eligible = [c for c in ranked if int(c.get("n") or 0) >= min_n]
    best = eligible[0] if eligible else (ranked[0] if ranked else {})
    top5 = eligible[:5]

    s = bt.get("summaries") or {}
    rv_only = s.get("w2_rv_lt_100") or {}
    improving = s.get("w2_improving_quad") or s.get("w2_rv_lt_100_mv_gt_100") or {}
    w3 = s.get("triple_w3_rv_high_low") or {}
    lp = s.get("lead_pullback_buy") or {}

    reasons: list[str] = []
    if best:
        reasons.append(
            f"Sweet spot: RV∈[{best.get('rv_min', '—')}, {best.get('rv_max')}) "
            f"MV>{best.get('mv_min')} · n={best.get('n')} "
            f"ret={best.get('mean_ret_3d_net_pct')}% win={best.get('win_rate_pct')}%"
        )
    if best and _sweet_spot_score(best, min_n=min_n) > _sweet_spot_score(rv_only, min_n=0):
        reasons.append(
            f"Beats w2_rv_lt_100 ({rv_only.get('mean_ret_3d_net_pct')}% · n={rv_only.get('n')})"
        )
    elif rv_only:
        reasons.append(
            f"RV-only baseline still competitive ({rv_only.get('mean_ret_3d_net_pct')}% · n={rv_only.get('n')})"
        )
    if improving and int(improving.get("n") or 0) > 0:
        reasons.append(
            f"w2_improving n={improving.get('n')} ret={improving.get('mean_ret_3d_net_pct')}%"
        )
    w3_ret = float(w3.get("mean_ret_3d_net_pct") or 0)
    best_ret = float(best.get("mean_ret_3d_net_pct") or 0) if best else 0
    if w3_ret > best_ret:
        reasons.append(f"W3 RV high/low champion still leads ({w3_ret}% vs best W2 {best_ret}%)")
    elif best_ret > w3_ret:
        reasons.append(f"Best W2 band beats W3 ({best_ret}% vs {w3_ret}%)")

    return {
        "min_n": min_n,
        "sweet_spot": best,
        "top5": top5,
        "w2_rv_lt_100": rv_only,
        "w2_improving": improving,
        "triple_w3_rv_high_low": w3,
        "lead_pullback_buy": lp,
        "reasons": reasons,
        "summary": (
            f"W2 RV/MV grid · sweet spot RV∈[{best.get('rv_min', '—')}, {best.get('rv_max')}) "
            f"MV>{best.get('mv_min')} · n={best.get('n')} ret={best.get('mean_ret_3d_net_pct')}% "
            f"win={best.get('win_rate_pct')}%"
            if best
            else "W2 RV/MV grid · no eligible sweet spot"
        ),
    }


def run_w2_rv_mv_sweep(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2025-01-01",
    date_end: str | None = None,
    intraday_tail_days: int = 60,
    rv_max_grid: tuple[float, ...] = DEFAULT_W2_RV_MAX_GRID,
    rv_min_grid: tuple[float | None, ...] = DEFAULT_W2_RV_MIN_GRID,
    mv_min_grid: tuple[float, ...] = DEFAULT_W2_MV_MIN_GRID,
    min_n: int = 25,
    etf_codes: tuple[str, ...] = DEFAULT_ETF_CODES,
) -> dict[str, Any]:
    close, _, _ = load_price_panels(conn)
    full_dates = close.index.astype(str).tolist()
    end = date_end or full_dates[-1]
    trade_dates = [d for d in full_dates if date_start <= d <= end]
    sample_dates = (
        trade_dates[-intraday_tail_days:]
        if intraday_tail_days > 0 and len(trade_dates) > intraday_tail_days
        else trade_dates
    )
    bt = backtest_w2_rv_mv_grid(
        conn,
        dates=sample_dates,
        rv_max_grid=rv_max_grid,
        rv_min_grid=rv_min_grid,
        mv_min_grid=mv_min_grid,
        etf_codes=etf_codes,
    )
    verdict = verdict_w2_rv_mv_sweet_spot(bt, min_n=min_n)
    return {
        "schema": "w2_rv_mv_sweet_spot-v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "date_start": date_start,
        "date_end": end,
        "intraday_sample_dates": sample_dates,
        "min_n": min_n,
        "backtest": bt,
        "verdict": verdict,
    }


def render_w2_rv_mv_sweep_md(payload: dict[str, Any]) -> str:
    v = payload.get("verdict") or {}
    bt = payload.get("backtest") or {}
    s = bt.get("summaries") or {}
    sweet = v.get("sweet_spot") or {}
    lines = [
        "# WMA2 RV/MV sweet spot sweep",
        "",
        f"> {v.get('summary', '')}",
        "",
        "## Structural (fixed)",
        "",
        "- WMA20 ∈ {領先, 轉強} 且 RV > 100",
        "- WMA5 ∈ {領先, 轉強} 且 RV > 100",
        "",
        "## Trigger grid",
        "",
        "- WMA2 **RV ∈ [rv_min, rv_max)** · **MV > mv_min**",
        f"- Exit: **{bt.get('exit_rule')}** · sample **{len(payload.get('intraday_sample_dates') or [])}** days",
        f"- min_n for sweet spot: **{payload.get('min_n', 25)}**",
        "",
        "### Top 5 cells (n ≥ min_n)",
        "",
        "| RV min | RV max | MV min | n | mean ret% | win% | median% |",
        "|-------:|-------:|-------:|--:|----------:|-----:|--------:|",
    ]
    for c in v.get("top5") or []:
        lo = c.get("rv_min")
        lo_s = "—" if lo is None else lo
        lines.append(
            f"| {lo_s} | {c.get('rv_max', '—')} | {c.get('mv_min', '—')} | "
            f"{c.get('n', 0)} | {c.get('mean_ret_3d_net_pct', '—')} | "
            f"{c.get('win_rate_pct', '—')} | {c.get('median_ret_3d_net_pct', '—')} |"
        )
    lines.extend(
        [
            "",
            "### Baselines",
            "",
            "| Pattern | n | mean ret% | win% |",
            "|---------|--:|----------:|-----:|",
        ]
    )
    for pid in (
        "w2_rv_lt_100",
        "w2_improving_quad",
        "w2_rv_lt_100_mv_gt_100",
        "lead_pullback_buy",
        "triple_w3_rv_high_low",
    ):
        row = s.get(pid) or {}
        lines.append(
            f"| {pid} | {row.get('n', 0)} | {row.get('mean_ret_3d_net_pct', '—')} | "
            f"{row.get('win_rate_pct', '—')} |"
        )
    lines.extend(["", "## Verdict", ""])
    if sweet:
        lines.append(
            f"- **Sweet spot**: RV ∈ [{sweet.get('rv_min', '—')}, {sweet.get('rv_max')}) "
            f"· MV > {sweet.get('mv_min')}"
        )
    for r in v.get("reasons") or []:
        lines.append(f"- {r}")
    lines.extend(["", "模組：`scripts/run_w2_rv_mv_sweep.py`"])
    return "\n".join(lines)


def _backtest_named_matchers(
    conn: sqlite3.Connection,
    *,
    dates: list[str],
    matchers: list[tuple[str, Callable[[dict[str, Any]], bool]]],
    exit_rule: str = "hold_3d",
    etf_codes: tuple[str, ...] = DEFAULT_ETF_CODES,
    frames: list[dict[str, str]] | None = None,
    trajectories: list[dict[str, Any]] | None = None,
    meta: dict[str, Any] | None = None,
    stock_maps: dict[str, list[dict[str, Any]]] | None = None,
    bench: Any | None = None,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any], list[dict[str, str]]]:
    if frames is None or trajectories is None:
        frames, trajectories, meta = build_dual_wma_intraday_trajectories(
            conn,
            dates=dates,
            etf_codes=etf_codes,
            micro_lengths=(WMA_ULTRA_LENGTH, WMA_MICRO_LENGTH),
        )
    if bench is None:
        close, _, _ = load_price_panels(conn)
        bench = load_benchmark_close(conn).reindex(close.index).astype(float)
    if stock_maps is None:
        stock_maps = _build_stock_frame_maps(trajectories, frames)
    panel_meta = meta or {}
    pattern_rows: dict[str, list[dict[str, Any]]] = {pid: [] for pid, _ in matchers}
    frame_ids = [f["id"] for f in frames]

    for pid, match in matchers:
        seen_day: set[tuple[str, str]] = set()
        for t in trajectories:
            sid = t["stock_id"]
            by_id = {p["frame_id"]: p for p in t["points"]}
            for i, fid in enumerate(frame_ids):
                p = by_id.get(fid)
                if not p or not match(p):
                    continue
                d = frames[i]["date"]
                key = (sid, d)
                if key in seen_day:
                    continue
                seen_day.add(key)
                pts = stock_maps.get(sid)
                if not pts:
                    continue
                exit_idx = _exit_frame_idx(exit_rule, i, frames, pts)
                tr = _trade_return(conn, bench, stock_maps, sid, i, exit_idx, frames)
                if tr is None:
                    continue
                pattern_rows[pid].append({**tr, "stock_id": sid, "entry_date": d})

    return pattern_rows, panel_meta, frames


def _event_keys(rows: list[dict[str, Any]]) -> set[tuple[str, str]]:
    return {(str(r["stock_id"]), str(r["entry_date"])) for r in rows}


def backtest_w3_w2_combo_compare(
    conn: sqlite3.Connection,
    *,
    dates: list[str],
    exit_rule: str = "hold_3d",
    etf_codes: tuple[str, ...] = DEFAULT_ETF_CODES,
) -> dict[str, Any]:
    matchers: list[tuple[str, Callable[[dict[str, Any]], bool]]] = [
        ("w2_sweet_spot", matches_w2_sweet_spot),
        ("triple_w3_rv_high_low", matches_triple_w3_rv_high_low),
        ("w3_rv_hl_w2_sweet", matches_w3_rv_hl_w2_sweet),
        ("w3_rv_hl_w2_mv_only", matches_w3_rv_hl_w2_mv_only),
        ("w3_rv_hl_w3_mv_gt_100", matches_w3_rv_hl_w3_mv_gt_100),
        ("lead_pullback_buy", matches_lead_pullback),
    ]
    pattern_rows, meta, _frames = _backtest_named_matchers(
        conn, dates=dates, matchers=matchers, exit_rule=exit_rule, etf_codes=etf_codes
    )
    summaries = {pid: _summarize_trades(rows) for pid, rows in pattern_rows.items()}
    w3_keys = _event_keys(pattern_rows.get("triple_w3_rv_high_low") or [])
    combo_keys = _event_keys(pattern_rows.get("w3_rv_hl_w2_sweet") or [])
    w2_keys = _event_keys(pattern_rows.get("w2_sweet_spot") or [])
    return {
        "dates": dates,
        "meta": meta,
        "exit_rule": exit_rule,
        "w2_sweet": f"RV∈[{W2_SWEET_RV_MIN},{W2_SWEET_RV_MAX})·MV>{W2_SWEET_MV_MIN}",
        "summaries": summaries,
        "overlap": {
            "w3_and_combo_same_day": len(w3_keys & combo_keys),
            "w3_only": len(w3_keys - combo_keys),
            "combo_only": len(combo_keys - w3_keys),
            "w2_sweet_and_w3_same_day": len(w2_keys & w3_keys),
            "w2_only": len(w2_keys - w3_keys),
        },
    }


def verdict_w3_w2_combo(
    bt: dict[str, Any],
    *,
    min_n: int = 20,
) -> dict[str, Any]:
    s = bt.get("summaries") or {}
    w3 = s.get("triple_w3_rv_high_low") or {}
    combo = s.get("w3_rv_hl_w2_sweet") or {}
    w2 = s.get("w2_sweet_spot") or {}
    w2_mv = s.get("w3_rv_hl_w2_mv_only") or {}
    w3_mv = s.get("w3_rv_hl_w3_mv_gt_100") or {}
    lp = s.get("lead_pullback_buy") or {}

    w3_ret = float(w3.get("mean_ret_3d_net_pct") or 0)
    combo_ret = float(combo.get("mean_ret_3d_net_pct") or 0)
    combo_n = int(combo.get("n") or 0)
    w3_n = int(w3.get("n") or 0)

    reasons: list[str] = []
    recommend = "triple_w3_rv_high_low"
    if combo_n >= min_n and combo_ret > w3_ret + 0.05:
        recommend = "w3_rv_hl_w2_sweet"
        reasons.append(
            f"Combo beats W3 alone ({combo_ret}% vs {w3_ret}%) · n {w3_n}→{combo_n}"
        )
    elif combo_n >= min_n:
        reasons.append(
            f"Combo does not beat W3 ({combo_ret}% vs {w3_ret}%) · n {w3_n}→{combo_n}"
        )
    else:
        reasons.append(f"Combo n={combo_n} < min_n={min_n}")

    ov = bt.get("overlap") or {}
    if w3_n > 0:
        pct = round(100.0 * ov.get("w3_and_combo_same_day", 0) / w3_n, 1)
        reasons.append(f"Combo captures {pct}% of W3 stock-days")

    if float(w2_mv.get("mean_ret_3d_net_pct") or -999) > combo_ret and int(w2_mv.get("n") or 0) >= min_n:
        reasons.append(
            f"Looser W2 MV-only filter ret={w2_mv.get('mean_ret_3d_net_pct')}% n={w2_mv.get('n')}"
        )

    return {
        "recommend": recommend,
        "w2_sweet_spot": w2,
        "triple_w3_rv_high_low": w3,
        "w3_rv_hl_w2_sweet": combo,
        "w3_rv_hl_w2_mv_only": w2_mv,
        "w3_rv_hl_w3_mv_gt_100": w3_mv,
        "lead_pullback_buy": lp,
        "overlap": ov,
        "reasons": reasons,
        "summary": (
            f"W3+W2 sweet · n={combo_n} ret={combo_ret}% win={combo.get('win_rate_pct')}% · "
            f"W3 alone n={w3_n} ret={w3_ret}% · recommend **{recommend}**"
        ),
    }


def run_w3_w2_combo_sweep(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2025-01-01",
    date_end: str | None = None,
    intraday_tail_days: int = 60,
    min_n: int = 20,
    etf_codes: tuple[str, ...] = DEFAULT_ETF_CODES,
) -> dict[str, Any]:
    close, _, _ = load_price_panels(conn)
    full_dates = close.index.astype(str).tolist()
    end = date_end or full_dates[-1]
    trade_dates = [d for d in full_dates if date_start <= d <= end]
    sample_dates = (
        trade_dates[-intraday_tail_days:]
        if intraday_tail_days > 0 and len(trade_dates) > intraday_tail_days
        else trade_dates
    )
    bt = backtest_w3_w2_combo_compare(conn, dates=sample_dates, etf_codes=etf_codes)
    verdict = verdict_w3_w2_combo(bt, min_n=min_n)
    return {
        "schema": "w3_w2_combo_compare-v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "date_start": date_start,
        "date_end": end,
        "intraday_sample_dates": sample_dates,
        "min_n": min_n,
        "backtest": bt,
        "verdict": verdict,
    }


def render_w3_w2_combo_md(payload: dict[str, Any]) -> str:
    v = payload.get("verdict") or {}
    bt = payload.get("backtest") or {}
    s = bt.get("summaries") or {}
    ov = v.get("overlap") or bt.get("overlap") or {}
    lines = [
        "# W3 trigger · W2 sweet-spot combo",
        "",
        f"> {v.get('summary', '')}",
        "",
        "## Patterns",
        "",
        f"- **W2 sweet spot**: {bt.get('w2_sweet')}",
        "- **W3 trigger**: W20/W5 RV>100 · W3 RV<100",
        "- **Combo**: W3 trigger **and** W2 sweet spot",
        "",
        f"- Exit: **{bt.get('exit_rule')}** · sample **{len(payload.get('intraday_sample_dates') or [])}** days",
        "",
        "## 3d hold outcomes",
        "",
        "| Pattern | n | mean ret net% | win% | median% | mean excess% |",
        "|---------|--:|--------------:|-----:|--------:|-------------:|",
    ]
    for pid, label in (
        ("w2_sweet_spot", "W2 sweet only"),
        ("triple_w3_rv_high_low", "W3 RV high/low"),
        ("w3_rv_hl_w2_sweet", "**W3 + W2 sweet**"),
        ("w3_rv_hl_w2_mv_only", "W3 + W2 MV>100"),
        ("w3_rv_hl_w3_mv_gt_100", "W3 + W3 MV>100"),
        ("lead_pullback_buy", "lead_pullback"),
    ):
        row = s.get(pid) or {}
        lines.append(
            f"| {label} | {row.get('n', 0)} | {row.get('mean_ret_3d_net_pct', '—')} | "
            f"{row.get('win_rate_pct', '—')} | {row.get('median_ret_3d_net_pct', '—')} | "
            f"{row.get('mean_excess_3d_pct', '—')} |"
        )
    lines.extend(
        [
            "",
            "## Overlap",
            "",
            f"- W3 ∩ combo same stock-day: **{ov.get('w3_and_combo_same_day', 0)}**",
            f"- W3 only (filtered out by W2): **{ov.get('w3_only', 0)}**",
            f"- W2 sweet ∩ W3 same day: **{ov.get('w2_sweet_and_w3_same_day', 0)}**",
            f"- W2 sweet only (no W3): **{ov.get('w2_only', 0)}**",
            "",
            "## Verdict",
            "",
            f"- **Recommend**: {v.get('recommend')}",
            "",
        ]
    )
    for r in v.get("reasons") or []:
        lines.append(f"- {r}")
    lines.extend(["", "模組：`scripts/run_w3_w2_combo_sweep.py`"])
    return "\n".join(lines)
