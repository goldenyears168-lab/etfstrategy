"""W3 RV high/low · winner/loser RRG entry profile · filter hypotheses."""

from __future__ import annotations

import sqlite3
import statistics
from datetime import datetime
from typing import Any, Callable

from market_benchmark import load_benchmark_close
from project_config import DEFAULT_ETF_CODES
from research.backtest.dual_wma_signal_backtest import (
    _build_stock_frame_maps,
    _exit_frame_idx,
    _trade_return,
)
from research.backtest.finpilot_local_backtest import load_price_panels
from research.backtest.triple_wma_pullback_sweep import (
    W2_SWEET_MV_MIN,
    W2_SWEET_RV_MAX,
    W2_SWEET_RV_MIN,
    W3_ABC_V3_W20_RV_MAX,
    W3_ABC_F1_W5_W3_MV_SPREAD_MAX,
    W3_ABC_W20_W3_SPREAD_MAX,
    W3_ABC_W5_RV_MAX,
    _backtest_named_matchers,
    _mv,
    _q,
    _rv,
    _summarize_trades,
    _w2_key,
    matches_triple_w3_rv_high_low,
    matches_w2_rv_mv_band,
    matches_w3_rv_hl_abc_v2,
    matches_w3_rv_hl_abc_v3,
    matches_w3_rv_hl_abc_v3_f1,
    micro_leg_key,
)
from rrg_universe_intraday_panel import (
    WMA_MICRO_LENGTH,
    WMA_ULTRA_LENGTH,
    build_dual_wma_intraday_trajectories,
)

LEGS = ("w20", "w5", "w3", "w2")
SPREAD_CLASSES = ("pullback", "aligned", "extension", "mixed")
INTRADAY_POLL_MINUTES = (
    "09:00",
    "09:30",
    "10:00",
    "10:30",
    "11:00",
    "11:30",
    "12:00",
    "12:30",
    "13:00",
    "13:30",
)


def _leg_snapshot(point: dict[str, Any], leg: str) -> dict[str, Any]:
    raw = point.get(leg) or {}
    rv = _rv(point, leg)
    mv = _mv(point, leg)
    return {
        "rv": rv,
        "mv": mv,
        "quadrant": _q(point, leg),
    }


def extract_entry_features(point: dict[str, Any], *, etf_hold_count: int = 0) -> dict[str, Any]:
    """RRG snapshot at entry for winner/loser profiling."""
    w20 = _leg_snapshot(point, "w20")
    w5 = _leg_snapshot(point, "w5")
    w3 = _leg_snapshot(point, "w3")
    w2 = _leg_snapshot(point, "w2")
    rv3 = w3.get("rv")
    rv5 = w5.get("rv")
    rv20 = w20.get("rv")
    rv2 = w2.get("rv")
    mv3 = w3.get("mv")
    mv5 = w5.get("mv")
    mv20 = w20.get("mv")
    mv2 = w2.get("mv")
    w20q = w20.get("quadrant") or "unknown"
    w5q = w5.get("quadrant") or "unknown"
    minute = str(point.get("minute") or "")
    out: dict[str, Any] = {
        "entry_minute": minute,
        "entry_session": (
            "open" if minute <= "10:00" else "close" if minute >= "13:00" else "mid"
        ),
        "spread_class": point.get("spread_class"),
        "spread_dist": point.get("spread_dist"),
        "spread_rs": point.get("spread_rs"),
        "spread_mom": point.get("spread_mom"),
        "trade_signal": point.get("trade_signal"),
        "hook_candidate": bool(point.get("hook_candidate")),
        "hook_complete": bool(point.get("hook_complete")),
        "hook_retrace_pct": point.get("hook_retrace_pct"),
        "hook_quality": point.get("hook_quality"),
        "etf_hold_count": etf_hold_count,
        "w20_w5_combo": f"{w20q}_{w5q}",
    }
    for leg in LEGS:
        snap = {"w20": w20, "w5": w5, "w3": w3, "w2": w2}[leg]
        for k, v in snap.items():
            out[f"{leg}_{k}"] = v
    if rv3 is not None:
        out["w3_dip_depth"] = round(100.0 - float(rv3), 3)
    if rv5 is not None and rv3 is not None:
        out["w5_w3_rv_spread"] = round(float(rv5) - float(rv3), 3)
    if rv20 is not None and rv3 is not None:
        out["w20_w3_rv_spread"] = round(float(rv20) - float(rv3), 3)
    if rv5 is not None and rv20 is not None:
        out["w5_w20_rv_spread"] = round(float(rv5) - float(rv20), 3)
    if mv5 is not None and mv3 is not None:
        out["w5_w3_mv_spread"] = round(float(mv5) - float(mv3), 3)
    if mv20 is not None and mv3 is not None:
        out["w20_w3_mv_spread"] = round(float(mv20) - float(mv3), 3)
    if mv5 is not None and mv20 is not None:
        out["w5_w20_mv_spread"] = round(float(mv5) - float(mv20), 3)
    if rv2 is not None and rv3 is not None:
        out["w2_w3_rv_spread"] = round(float(rv2) - float(rv3), 3)
    for prefix, rv, mv in (
        ("w20", rv20, mv20),
        ("w5", rv5, mv5),
        ("w3", rv3, mv3),
        ("w2", rv2, mv2),
    ):
        if mv is not None:
            out[f"{prefix}_mv_above_100"] = float(mv) > 100.0
            out[f"{prefix}_mv_above_101"] = float(mv) > 101.0
        if rv is not None:
            out[f"{prefix}_rv_above_101"] = float(rv) > 101.0
    if mv3 is not None:
        out["w3_mv_above_100"] = float(mv3) > 100.0
    if rv3 is not None:
        out["w3_rv_shallow"] = float(rv3) >= 99.5
        out["w3_rv_deep"] = float(rv3) < 99.0
    minute = out.get("entry_minute") or ""
    if minute in INTRADAY_POLL_MINUTES:
        out["entry_poll_idx"] = INTRADAY_POLL_MINUTES.index(minute)
    return out


def collect_matched_featured_trades(
    conn: sqlite3.Connection,
    *,
    dates: list[str],
    matcher: Callable[[dict[str, Any]], bool],
    exit_rule: str = "hold_3d",
    etf_codes: tuple[str, ...] = DEFAULT_ETF_CODES,
) -> list[dict[str, Any]]:
    frames, trajectories, meta = build_dual_wma_intraday_trajectories(
        conn,
        dates=dates,
        etf_codes=etf_codes,
        micro_lengths=(WMA_ULTRA_LENGTH, WMA_MICRO_LENGTH),
    )
    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index).astype(float)
    stock_maps = _build_stock_frame_maps(trajectories, frames)
    frame_ids = [f["id"] for f in frames]
    rows: list[dict[str, Any]] = []
    seen_day: set[tuple[str, str]] = set()

    for t in trajectories:
        sid = t["stock_id"]
        by_id = {p["frame_id"]: p for p in t["points"]}
        for i, fid in enumerate(frame_ids):
            p = by_id.get(fid)
            if not p or not matcher(p):
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
            feats = extract_entry_features(p, etf_hold_count=int(t.get("etf_hold_count") or 0))
            net = float(tr["net_pct"])
            rows.append(
                {
                    **tr,
                    **feats,
                    "entry_frame_idx": i,
                    "stock_id": sid,
                    "stock_name": t.get("stock_name") or "",
                    "entry_date": d,
                    "outcome": "win" if net > 0 else "loss",
                    "big_win": net >= 2.0,
                    "big_loss": net <= -2.0,
                }
            )
    return rows


def collect_w3_rv_hl_featured_trades(
    conn: sqlite3.Connection,
    *,
    dates: list[str],
    exit_rule: str = "hold_3d",
    etf_codes: tuple[str, ...] = DEFAULT_ETF_CODES,
) -> list[dict[str, Any]]:
    return collect_matched_featured_trades(
        conn,
        dates=dates,
        matcher=matches_triple_w3_rv_high_low,
        exit_rule=exit_rule,
        etf_codes=etf_codes,
    )


def collect_abc_v2_featured_trades(
    conn: sqlite3.Connection,
    *,
    dates: list[str],
    exit_rule: str = "hold_3d",
    etf_codes: tuple[str, ...] = DEFAULT_ETF_CODES,
) -> list[dict[str, Any]]:
    return collect_matched_featured_trades(
        conn,
        dates=dates,
        matcher=matches_w3_rv_hl_abc_v2,
        exit_rule=exit_rule,
        etf_codes=etf_codes,
    )


def collect_abc_v3_featured_trades(
    conn: sqlite3.Connection,
    *,
    dates: list[str],
    exit_rule: str = "hold_3d",
    etf_codes: tuple[str, ...] = DEFAULT_ETF_CODES,
) -> list[dict[str, Any]]:
    return collect_matched_featured_trades(
        conn,
        dates=dates,
        matcher=matches_w3_rv_hl_abc_v3,
        exit_rule=exit_rule,
        etf_codes=etf_codes,
    )


def _mean(vals: list[float]) -> float | None:
    return round(sum(vals) / len(vals), 3) if vals else None


def _median(vals: list[float]) -> float | None:
    return round(statistics.median(vals), 3) if vals else None


def _numeric_lifts(
    wins: list[dict[str, Any]],
    losses: list[dict[str, Any]],
    fields: tuple[str, ...],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for field in fields:
        wv = [float(r[field]) for r in wins if r.get(field) is not None]
        lv = [float(r[field]) for r in losses if r.get(field) is not None]
        if len(wv) < 20 or len(lv) < 20:
            continue
        mw, ml = _mean(wv), _mean(lv)
        rows.append(
            {
                "field": field,
                "mean_win": mw,
                "mean_loss": ml,
                "lift": round((mw or 0) - (ml or 0), 3),
                "median_win": _median(wv),
                "median_loss": _median(lv),
            }
        )
    rows.sort(key=lambda x: abs(float(x.get("lift") or 0)), reverse=True)
    return rows


def _quad_rates(rows: list[dict[str, Any]], leg: str) -> dict[str, float]:
    n = len(rows)
    if n == 0:
        return {}
    counts: dict[str, int] = {}
    for r in rows:
        q = r.get(f"{leg}_quadrant") or "unknown"
        counts[str(q)] = counts.get(str(q), 0) + 1
    return {q: round(c / n * 100, 1) for q, c in sorted(counts.items())}


def _spread_rates(rows: list[dict[str, Any]]) -> dict[str, float]:
    n = len(rows)
    if n == 0:
        return {}
    counts: dict[str, int] = {}
    for r in rows:
        sc = r.get("spread_class") or "unknown"
        counts[str(sc)] = counts.get(str(sc), 0) + 1
    return {q: round(c / n * 100, 1) for q, c in sorted(counts.items())}


def _categorical_rates(rows: list[dict[str, Any]], field: str) -> dict[str, float]:
    n = len(rows)
    if n == 0:
        return {}
    counts: dict[str, int] = {}
    for r in rows:
        key = str(r.get(field) or "none")
        counts[key] = counts.get(key, 0) + 1
    return {k: round(c / n * 100, 1) for k, c in sorted(counts.items())}


def _categorical_compare(
    wins: list[dict[str, Any]],
    losses: list[dict[str, Any]],
    field: str,
) -> dict[str, dict[str, float]]:
    return {"win_pct": _categorical_rates(wins, field), "loss_pct": _categorical_rates(losses, field)}


def _combo_compare(
    wins: list[dict[str, Any]],
    losses: list[dict[str, Any]],
    field: str,
) -> dict[str, Any]:
    win_r = _categorical_rates(wins, field)
    loss_r = _categorical_rates(losses, field)
    keys = sorted(set(win_r) | set(loss_r))
    rows: list[dict[str, Any]] = []
    for k in keys:
        wp = win_r.get(k, 0.0)
        lp = loss_r.get(k, 0.0)
        rows.append(
            {
                "combo": k,
                "win_pct": wp,
                "loss_pct": lp,
                "delta_pp": round(wp - lp, 1),
            }
        )
    rows.sort(key=lambda x: abs(float(x["delta_pp"])), reverse=True)
    return {"rows": rows}


def _cohen_d(pos: list[float], neg: list[float]) -> float | None:
    if len(pos) < 20 or len(neg) < 20:
        return None
    m1, m2 = sum(pos) / len(pos), sum(neg) / len(neg)
    if len(pos) < 2 or len(neg) < 2:
        return None
    v1 = statistics.pvariance(pos)
    v2 = statistics.pvariance(neg)
    pooled = ((v1 + v2) / 2) ** 0.5
    if pooled < 1e-9:
        return 0.0
    return round((m1 - m2) / pooled, 3)


def _factor_specs() -> list[tuple[str, str, Callable[[dict[str, Any]], bool]]]:
    """Binary factors for winner/loser discrimination scan."""
    def _rv(field: str, op: str, thr: float) -> Callable[[dict[str, Any]], bool]:
        def _fn(r: dict[str, Any]) -> bool:
            v = r.get(field)
            if v is None:
                return False
            return float(v) >= thr if op == "gte" else float(v) <= thr if op == "lte" else False

        return _fn

    def _flag(field: str) -> Callable[[dict[str, Any]], bool]:
        return lambda r: bool(r.get(field))

    def _quad(leg: str, q: str) -> Callable[[dict[str, Any]], bool]:
        return lambda r: r.get(f"{leg}_quadrant") == q

    def _combo(c: str) -> Callable[[dict[str, Any]], bool]:
        return lambda r: r.get("w20_w5_combo") == c

    def _signal(s: str) -> Callable[[dict[str, Any]], bool]:
        return lambda r: r.get("trade_signal") == s

    specs: list[tuple[str, str, Callable[[dict[str, Any]], bool]]] = [
        ("w20_leading", "W20 Leading", _quad("w20", "leading")),
        ("w20_weakening", "W20 Weakening", _quad("w20", "weakening")),
        ("w20_improving", "W20 Improving", _quad("w20", "improving")),
        ("w5_leading", "W5 Leading", _quad("w5", "leading")),
        ("w5_weakening", "W5 Weakening", _quad("w5", "weakening")),
        ("w5_improving", "W5 Improving", _quad("w5", "improving")),
        ("w5_lagging", "W5 Lagging", _quad("w5", "lagging")),
        ("combo_leading_leading", "W20 Leading · W5 Leading", _combo("leading_leading")),
        ("combo_leading_improving", "W20 Leading · W5 Improving", _combo("leading_improving")),
        ("combo_leading_weakening", "W20 Leading · W5 Weakening", _combo("leading_weakening")),
        ("combo_weakening_leading", "W20 Weakening · W5 Leading", _combo("weakening_leading")),
        ("combo_weakening_improving", "W20 Weakening · W5 Improving", _combo("weakening_improving")),
        ("w20_mv_gt_100", "W20 MV>100", _flag("w20_mv_above_100")),
        ("w20_mv_gt_101", "W20 MV>101", _flag("w20_mv_above_101")),
        ("w5_mv_gt_100", "W5 MV>100", _flag("w5_mv_above_100")),
        ("w5_mv_gt_101", "W5 MV>101", _flag("w5_mv_above_101")),
        ("w3_mv_gt_100", "W3 MV>100", _flag("w3_mv_above_100")),
        ("w20_rv_lte_105", "W20 RV≤105", _rv("w20_rv", "lte", 105.0)),
        ("w20_rv_lte_105.5", "W20 RV≤105.5", _rv("w20_rv", "lte", 105.5)),
        ("w20_rv_lte_104", "W20 RV≤104", _rv("w20_rv", "lte", 104.0)),
        ("w5_rv_lte_101", "W5 RV≤101", _rv("w5_rv", "lte", 101.0)),
        ("w5_rv_lte_100.5", "W5 RV≤100.5", _rv("w5_rv", "lte", 100.5)),
        ("w20_w3_spread_lte_6", "W20−W3 RV≤6", _rv("w20_w3_rv_spread", "lte", 6.0)),
        ("w20_w3_spread_lte_5.5", "W20−W3 RV≤5.5", _rv("w20_w3_rv_spread", "lte", 5.5)),
        ("w5_w20_rv_neg", "W5 RV < W20 RV", _rv("w5_w20_rv_spread", "lte", 0.0)),
        ("w5_w20_rv_neg_1", "W5−W20 RV≤−1", _rv("w5_w20_rv_spread", "lte", -1.0)),
        ("spread_rs_lte_neg4", "spread_rs≤−4", _rv("spread_rs", "lte", -4.0)),
        ("spread_rs_lte_neg3", "spread_rs≤−3", _rv("spread_rs", "lte", -3.0)),
        ("spread_dist_lte_6", "spread_dist≤6", _rv("spread_dist", "lte", 6.0)),
        ("w5_w3_mv_pos", "W5 MV > W3 MV", _rv("w5_w3_mv_spread", "gte", 0.01)),
        ("w20_w3_mv_pos", "W20 MV > W3 MV", _rv("w20_w3_mv_spread", "gte", 0.01)),
        ("w3_rv_shallow", "W3 RV≥99.5", _flag("w3_rv_shallow")),
        ("w3_rv_deep", "W3 RV<99", _flag("w3_rv_deep")),
        ("entry_open", "進場 ≤10:00", lambda r: r.get("entry_session") == "open"),
        ("entry_mid", "進場 10:30–12:30", lambda r: r.get("entry_session") == "mid"),
        ("entry_close", "進場 ≥13:00", lambda r: r.get("entry_session") == "close"),
        ("sig_lead_pullback", "trade_signal=lead_pullback", _signal("lead_pullback_buy")),
        ("sig_imp_extension", "trade_signal=imp_extension", _signal("imp_extension")),
        ("sig_none", "無 trade_signal", lambda r: not r.get("trade_signal")),
        ("hook_candidate", "hook_candidate", _flag("hook_candidate")),
        ("hook_complete", "hook_complete", _flag("hook_complete")),
        ("w2_mv_gt_100", "W2 MV>100", _flag("w2_mv_above_100")),
        ("etf_hold_ge2", "ETF hold≥2", lambda r: int(r.get("etf_hold_count") or 0) >= 2),
    ]
    return specs


def scan_factor_discriminators(
    trades: list[dict[str, Any]],
    *,
    min_pos: int = 30,
    min_neg: int = 30,
) -> list[dict[str, Any]]:
    """Rank binary factors by mean net% lift (pos vs neg)."""
    rows: list[dict[str, Any]] = []
    for fid, label, pred in _factor_specs():
        pos = [t for t in trades if pred(t)]
        neg = [t for t in trades if not pred(t)]
        if len(pos) < min_pos or len(neg) < min_neg:
            continue
        pos_rets = [float(t["net_pct"]) for t in pos]
        neg_rets = [float(t["net_pct"]) for t in neg]
        mp, mn = _mean(pos_rets), _mean(neg_rets)
        wp = round(100.0 * sum(1 for x in pos_rets if x > 0) / len(pos_rets), 1)
        wn = round(100.0 * sum(1 for x in neg_rets if x > 0) / len(neg_rets), 1)
        loss_pos = round(100.0 * sum(1 for t in pos if t.get("outcome") == "loss") / len(pos), 1)
        loss_neg = round(100.0 * sum(1 for t in neg if t.get("outcome") == "loss") / len(neg), 1)
        rows.append(
            {
                "id": fid,
                "label": label,
                "n_pos": len(pos),
                "n_neg": len(neg),
                "mean_ret_pos": mp,
                "mean_ret_neg": mn,
                "lift": round((mp or 0) - (mn or 0), 3),
                "win_rate_pos": wp,
                "win_rate_neg": wn,
                "loss_rate_pos": loss_pos,
                "loss_rate_neg": loss_neg,
                "cohen_d": _cohen_d(pos_rets, neg_rets),
            }
        )
    rows.sort(key=lambda x: abs(float(x.get("lift") or 0)), reverse=True)
    return rows


def backtest_discriminator_filters(
    trades: list[dict[str, Any]],
    discriminators: list[dict[str, Any]],
    *,
    top_n: int = 8,
) -> dict[str, Any]:
    """Backtest top positive-lift discriminator factors (pos side = filter)."""
    specs = {fid: fn for fid, _lbl, fn in _factor_specs()}
    summaries: dict[str, Any] = {"baseline_all": _summarize_trades(trades)}
    for row in discriminators[:top_n]:
        fid = str(row.get("id"))
        fn = specs.get(fid)
        if not fn:
            continue
        subset = [t for t in trades if fn(t)]
        summaries[fid] = _summarize_trades(subset)
    return {"summaries": summaries}


def profile_winners_losers(trades: list[dict[str, Any]]) -> dict[str, Any]:
    wins = [t for t in trades if t.get("outcome") == "win"]
    losses = [t for t in trades if t.get("outcome") == "loss"]
    numeric_fields = (
        "w20_rv",
        "w20_mv",
        "w5_rv",
        "w5_mv",
        "w3_rv",
        "w3_mv",
        "w2_rv",
        "w2_mv",
        "w3_dip_depth",
        "w5_w3_rv_spread",
        "w20_w3_rv_spread",
        "w5_w20_rv_spread",
        "w5_w3_mv_spread",
        "w20_w3_mv_spread",
        "w5_w20_mv_spread",
        "w2_w3_rv_spread",
        "spread_dist",
        "spread_rs",
        "spread_mom",
        "hook_retrace_pct",
        "hook_quality",
        "etf_hold_count",
    )
    lifts = _numeric_lifts(wins, losses, numeric_fields)
    quad_compare = {
        leg: {
            "win_pct": _quad_rates(wins, leg),
            "loss_pct": _quad_rates(losses, leg),
        }
        for leg in ("w20", "w5", "w3", "w2")
    }
    combo_compare = _combo_compare(wins, losses, "w20_w5_combo")
    signal_compare = _categorical_compare(wins, losses, "trade_signal")
    session_compare = _categorical_compare(wins, losses, "entry_session")
    return {
        "n": len(trades),
        "n_win": len(wins),
        "n_loss": len(losses),
        "win_rate_pct": round(len(wins) / len(trades) * 100, 2) if trades else 0,
        "mean_ret_win": _mean([float(t["net_pct"]) for t in wins]),
        "mean_ret_loss": _mean([float(t["net_pct"]) for t in losses]),
        "numeric_lifts": lifts,
        "w20_quadrant": quad_compare.get("w20") or {},
        "w5_quadrant": quad_compare.get("w5") or {},
        "w3_quadrant": quad_compare.get("w3") or {},
        "w2_quadrant": quad_compare.get("w2") or {},
        "w20_w5_combo": combo_compare,
        "trade_signal": signal_compare,
        "entry_session": session_compare,
        "factor_discriminators": scan_factor_discriminators(trades),
        "spread_class": {
            "win_pct": _spread_rates(wins),
            "loss_pct": _spread_rates(losses),
        },
        "w3_mv_above_100": {
            "win_pct": round(
                100.0 * sum(1 for t in wins if t.get("w3_mv_above_100")) / max(len(wins), 1),
                1,
            ),
            "loss_pct": round(
                100.0 * sum(1 for t in losses if t.get("w3_mv_above_100")) / max(len(losses), 1),
                1,
            ),
        },
        "w3_rv_shallow": {
            "win_pct": round(
                100.0 * sum(1 for t in wins if t.get("w3_rv_shallow")) / max(len(wins), 1),
                1,
            ),
            "loss_pct": round(
                100.0 * sum(1 for t in losses if t.get("w3_rv_shallow")) / max(len(losses), 1),
                1,
            ),
        },
    }


def _matches_w3_rv_hl_w3_mv_gt(p: dict[str, Any], *, floor: float) -> bool:
    if not matches_triple_w3_rv_high_low(p):
        return False
    mv3 = _mv(p, micro_leg_key(WMA_MICRO_LENGTH))
    return mv3 is not None and mv3 > floor


def _matches_w3_rv_hl_w3_rv_min(p: dict[str, Any], *, rv_min: float) -> bool:
    if not matches_triple_w3_rv_high_low(p):
        return False
    rv3 = _rv(p, micro_leg_key(WMA_MICRO_LENGTH))
    return rv3 is not None and rv3 >= rv_min


def _matches_w3_rv_hl_w3_improving(p: dict[str, Any]) -> bool:
    return _matches_w3_rv_hl_w3_mv_gt(p, floor=100.0)


def _matches_w3_rv_hl_avoid_lagging_w3(p: dict[str, Any]) -> bool:
    if not matches_triple_w3_rv_high_low(p):
        return False
    return _q(p, micro_leg_key(WMA_MICRO_LENGTH)) != "lagging"


def _matches_w3_rv_hl_spread_pullback(p: dict[str, Any]) -> bool:
    if not matches_triple_w3_rv_high_low(p):
        return False
    return p.get("spread_class") == "pullback"


def _matches_w3_rv_hl_w5_mv_gt(p: dict[str, Any], *, floor: float) -> bool:
    if not matches_triple_w3_rv_high_low(p):
        return False
    mv5 = _mv(p, "w5")
    return mv5 is not None and mv5 > floor


def FILTER_HYPOTHESES() -> list[tuple[str, str, Callable[[dict[str, Any]], bool]]]:
    """Candidate filters derived from winner/loser profile research."""
    return [
        ("baseline_w3_rv_hl", "W3 RV high/low (baseline)", matches_triple_w3_rv_high_low),
        (
            "w3_improving",
            "W3 RV<100 · MV>100 (Improving)",
            _matches_w3_rv_hl_w3_improving,
        ),
        (
            "w3_mv_gt_100.5",
            "W3 MV > 100.5",
            lambda p: _matches_w3_rv_hl_w3_mv_gt(p, floor=100.5),
        ),
        (
            "w3_rv_gte_99.5",
            "W3 RV ≥ 99.5 (shallow dip)",
            lambda p: _matches_w3_rv_hl_w3_rv_min(p, rv_min=99.5),
        ),
        (
            "w3_improving_shallow",
            "W3 Improving · RV ≥ 99.5",
            lambda p: _matches_w3_rv_hl_w3_improving(p)
            and _matches_w3_rv_hl_w3_rv_min(p, rv_min=99.5),
        ),
        (
            "avoid_w3_lagging",
            "W3 ∉ Lagging",
            _matches_w3_rv_hl_avoid_lagging_w3,
        ),
        (
            "w5_mv_gt_100",
            "W5 MV > 100",
            lambda p: _matches_w3_rv_hl_w5_mv_gt(p, floor=100.0),
        ),
        (
            "spread_pullback",
            "W5-W20 spread pullback",
            _matches_w3_rv_hl_spread_pullback,
        ),
        (
            "w3_improving_w5_mv",
            "W3 Improving · W5 MV>100",
            lambda p: _matches_w3_rv_hl_w3_improving(p)
            and _matches_w3_rv_hl_w5_mv_gt(p, floor=100.0),
        ),
        (
            "w3_shallow_w2_sweet",
            "W3 RV≥99.5 · W2 sweet spot",
            lambda p: _matches_w3_rv_hl_w3_rv_min(p, rv_min=99.5)
            and matches_w2_rv_mv_band(
                p,
                rv_min=W2_SWEET_RV_MIN,
                rv_max=W2_SWEET_RV_MAX,
                mv_min=W2_SWEET_MV_MIN,
            ),
        ),
        (
            "w20_w3_spread_lte_6",
            "W20-W3 RV spread ≤ 6 (less extended)",
            lambda p: False,  # post-hoc via _trade_matches_filter only
        ),
        (
            "w20_rv_lte_105.5",
            "W20 RV ≤ 105.5 (avoid chase)",
            lambda p: False,
        ),
    ]


def _trade_matches_filter(trade: dict[str, Any], filter_id: str) -> bool:
    """Post-hoc filter on collected entry features (same stock-day events as profile)."""
    if filter_id == "baseline_w3_rv_hl":
        return True
    rv3 = trade.get("w3_rv")
    mv3 = trade.get("w3_mv")
    mv5 = trade.get("w5_mv")
    if filter_id == "w3_improving":
        return bool(mv3 is not None and float(mv3) > 100.0)
    if filter_id == "w3_mv_gt_100.5":
        return bool(mv3 is not None and float(mv3) > 100.5)
    if filter_id == "w3_rv_gte_99.5":
        return bool(trade.get("w3_rv_shallow"))
    if filter_id == "w3_improving_shallow":
        return bool(trade.get("w3_rv_shallow") and mv3 is not None and float(mv3) > 100.0)
    if filter_id == "avoid_w3_lagging":
        return trade.get("w3_quadrant") != "lagging"
    if filter_id == "w5_mv_gt_100":
        return bool(mv5 is not None and float(mv5) > 100.0)
    if filter_id == "spread_pullback":
        return trade.get("spread_class") == "pullback"
    if filter_id == "w3_improving_w5_mv":
        return (
            mv3 is not None
            and float(mv3) > 100.0
            and mv5 is not None
            and float(mv5) > 100.0
        )
    if filter_id == "w3_shallow_w2_sweet":
        w2_rv = trade.get("w2_rv")
        w2_mv = trade.get("w2_mv")
        if w2_rv is None or w2_mv is None or rv3 is None:
            return False
        return (
            float(rv3) >= W2_SWEET_RV_MIN
            and float(w2_rv) < W2_SWEET_RV_MAX
            and float(w2_mv) > W2_SWEET_MV_MIN
            and float(rv3) >= 99.5
        )
    if filter_id == "w20_w3_spread_lte_6":
        sp = trade.get("w20_w3_rv_spread")
        return sp is not None and float(sp) <= 6.0
    if filter_id == "w20_rv_lte_105.5":
        rv20 = trade.get("w20_rv")
        return rv20 is not None and float(rv20) <= 105.5
    return False


def backtest_w3_filter_hypotheses_from_trades(
    trades: list[dict[str, Any]],
) -> dict[str, Any]:
    summaries: dict[str, Any] = {}
    for pid, _label, _ in FILTER_HYPOTHESES():
        subset = [t for t in trades if _trade_matches_filter(t, pid)]
        summaries[pid] = _summarize_trades(subset)
    return {
        "summaries": summaries,
        "filters": [{"id": pid, "label": label} for pid, label, _ in FILTER_HYPOTHESES()],
    }


def backtest_w3_filter_hypotheses(
    conn: sqlite3.Connection,
    *,
    dates: list[str],
    exit_rule: str = "hold_3d",
    etf_codes: tuple[str, ...] = DEFAULT_ETF_CODES,
) -> dict[str, Any]:
    trades = collect_w3_rv_hl_featured_trades(
        conn, dates=dates, exit_rule=exit_rule, etf_codes=etf_codes
    )
    out = backtest_w3_filter_hypotheses_from_trades(trades)
    return {"dates": dates, "exit_rule": exit_rule, **out}


def verdict_w3_winner_filters(
    profile: dict[str, Any],
    filter_bt: dict[str, Any],
    *,
    min_n: int = 40,
) -> dict[str, Any]:
    s = filter_bt.get("summaries") or {}
    base = s.get("baseline_w3_rv_hl") or {}
    base_ret = float(base.get("mean_ret_3d_net_pct") or 0)
    base_n = int(base.get("n") or 0)

    ranked: list[tuple[str, dict[str, Any]]] = []
    for pid, _label, _ in FILTER_HYPOTHESES():
        if pid == "baseline_w3_rv_hl":
            continue
        row = s.get(pid) or {}
        n = int(row.get("n") or 0)
        if n < min_n:
            continue
        ret = float(row.get("mean_ret_3d_net_pct") or 0)
        win = float(row.get("win_rate_pct") or 0)
        score = ret + (win - 50) * 0.02 + min(n, 300) * 0.0003
        ranked.append((pid, {**row, "score": round(score, 4), "delta_ret": round(ret - base_ret, 3)}))
    ranked.sort(key=lambda x: float(x[1].get("score") or 0), reverse=True)
    best_id, best = ranked[0] if ranked else ("", {})

    lifts = profile.get("numeric_lifts") or []
    top_lift = lifts[0] if lifts else {}

    reasons: list[str] = []
    if top_lift:
        reasons.append(
            f"Top numeric lift: {top_lift.get('field')} "
            f"win={top_lift.get('mean_win')} loss={top_lift.get('mean_loss')} "
            f"Δ={top_lift.get('lift')}"
        )
    w3mv = profile.get("w3_mv_above_100") or {}
    reasons.append(
        f"W3 MV>100: win {w3mv.get('win_pct')}% vs loss {w3mv.get('loss_pct')}%"
    )
    if best:
        reasons.append(
            f"Best filter {best_id}: n={best.get('n')} ret={best.get('mean_ret_3d_net_pct')}% "
            f"Δvs base {best.get('delta_ret')}%"
        )
    recommend = best_id if best and float(best.get("delta_ret") or 0) > 0.1 else "baseline_w3_rv_hl"

    return {
        "recommend": recommend,
        "baseline": base,
        "best_filter": {"id": best_id, **best} if best else {},
        "ranked_filters": [{"id": pid, **row} for pid, row in ranked[:8]],
        "reasons": reasons,
        "hypotheses": [
            {
                "id": "H-W3W-1",
                "statement": "W3 Improving (MV>100) at entry lifts 3d net vs baseline",
                "status": "pending",
            },
            {
                "id": "H-W3W-2",
                "statement": "W3 shallow dip (RV≥99.5) beats deep dip on 3d net",
                "status": "pending",
            },
            {
                "id": "H-W3W-3",
                "statement": "Avoid W3 Lagging quadrant at entry improves win rate",
                "status": "pending",
            },
            {
                "id": "H-W3W-4",
                "statement": "Less extended entry (W20-W3 RV spread≤6) lifts 3d net vs baseline",
                "status": "pending",
            },
        ],
        "summary": (
            f"W3 HL profile n={profile.get('n')} win={profile.get('win_rate_pct')}% · "
            f"best filter **{recommend}**"
        ),
    }


def run_w3_winner_profile_study(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2025-01-01",
    date_end: str | None = None,
    intraday_tail_days: int = 60,
    min_n: int = 40,
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
    trades = collect_w3_rv_hl_featured_trades(conn, dates=sample_dates, etf_codes=etf_codes)
    profile = profile_winners_losers(trades)
    disc = profile.get("factor_discriminators") or []
    disc_bt = backtest_discriminator_filters(trades, disc, top_n=10)
    filter_bt = backtest_w3_filter_hypotheses_from_trades(trades)
    filter_bt["dates"] = sample_dates
    filter_bt["exit_rule"] = "hold_3d"
    verdict = verdict_w3_winner_filters(profile, filter_bt, min_n=min_n)

    # Update hypothesis status from filter results
    s = filter_bt.get("summaries") or {}
    base_ret = float((s.get("baseline_w3_rv_hl") or {}).get("mean_ret_3d_net_pct") or 0)
    hyps = verdict.get("hypotheses") or []
    for h in hyps:
        fid = {
            "H-W3W-1": "w3_improving",
            "H-W3W-2": "w3_rv_gte_99.5",
            "H-W3W-3": "avoid_w3_lagging",
            "H-W3W-4": "w20_w3_spread_lte_6",
        }.get(str(h.get("id")))
        row = s.get(fid or "") or {}
        n = int(row.get("n") or 0)
        ret = float(row.get("mean_ret_3d_net_pct") or 0)
        if n < min_n:
            h["status"] = "insufficient_n"
        elif ret > base_ret + 0.05:
            h["status"] = "passed"
        else:
            h["status"] = "rejected"
        h["n"] = n
        h["mean_ret_3d_net_pct"] = row.get("mean_ret_3d_net_pct")

    return {
        "schema": "w3_rv_hl_winner_profile-v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "date_start": date_start,
        "date_end": end,
        "intraday_sample_dates": sample_dates,
        "min_n": min_n,
        "profile": profile,
        "discriminator_backtest": disc_bt,
        "filter_backtest": filter_bt,
        "verdict": verdict,
        "sample_trades_n": len(trades),
    }


def render_w3_winner_profile_md(payload: dict[str, Any]) -> str:
    prof = payload.get("profile") or {}
    v = payload.get("verdict") or {}
    bt = payload.get("filter_backtest") or {}
    s = bt.get("summaries") or {}
    lines = [
        "# W3 RV high/low · winner/loser profile",
        "",
        f"> {v.get('summary', '')}",
        "",
        "## Sample",
        "",
        f"- Trades: **{prof.get('n')}** · win **{prof.get('n_win')}** / loss **{prof.get('n_loss')}**",
        f"- Win rate: **{prof.get('win_rate_pct')}%**",
        f"- Mean ret: win **{prof.get('mean_ret_win')}%** · loss **{prof.get('mean_ret_loss')}%**",
        f"- Days: **{len(payload.get('intraday_sample_dates') or [])}** · exit **hold_3d**",
        "",
        "## 贏家 vs 輸家 · 數值特徵（進場時）",
        "",
        "| 特徵 | mean 贏 | mean 輸 | lift | median 贏 | median 輸 |",
        "|------|--------:|--------:|-----:|----------:|----------:|",
    ]
    for row in prof.get("numeric_lifts") or []:
        lines.append(
            f"| {row.get('field')} | {row.get('mean_win')} | {row.get('mean_loss')} | "
            f"{row.get('lift')} | {row.get('median_win')} | {row.get('median_loss')} |"
        )
    w3mv = prof.get("w3_mv_above_100") or {}
    shallow = prof.get("w3_rv_shallow") or {}
    lines.extend(
        [
            "",
            "## W20 / W5 象限分布 %（結構腿）",
            "",
            "| 腿 | 象限 | 贏家% | 輸家% | Δpp |",
            "|----|------|------:|------:|----:|",
        ]
    )
    for leg in ("w20", "w5"):
        qd = prof.get(f"{leg}_quadrant") or {}
        wq, lq = qd.get("win_pct") or {}, qd.get("loss_pct") or {}
        for q in sorted(set(wq) | set(lq)):
            wp, lp = wq.get(q, 0), lq.get(q, 0)
            lines.append(f"| {leg} | {q} | {wp} | {lp} | {round(wp - lp, 1)} |")

    lines.extend(
        [
            "",
            "## W20×W5 象限組合 %",
            "",
            "| 組合 | 贏家% | 輸家% | Δpp |",
            "|------|------:|------:|----:|",
        ]
    )
    for row in (prof.get("w20_w5_combo") or {}).get("rows") or []:
        lines.append(
            f"| {row.get('combo')} | {row.get('win_pct')} | {row.get('loss_pct')} | "
            f"{row.get('delta_pp')} |"
        )

    lines.extend(
        [
            "",
            "## trade_signal / 進場時段 %",
            "",
            "| 類別 | 值 | 贏家% | 輸家% |",
            "|------|-----|------:|------:|",
        ]
    )
    for field, title in (("trade_signal", "signal"), ("entry_session", "session")):
        cat = prof.get(field) or {}
        wq, lq = cat.get("win_pct") or {}, cat.get("loss_pct") or {}
        for q in sorted(set(wq) | set(lq)):
            lines.append(f"| {title} | {q} | {wq.get(q, 0)} | {lq.get(q, 0)} |")

    lines.extend(
        [
            "",
            "## 關鍵二元特徵（W3 與 MV）",
            "",
            f"- **W3 MV > 100**：贏家 {w3mv.get('win_pct')}% · 輸家 {w3mv.get('loss_pct')}%",
            f"- **W3 RV ≥ 99.5**：贏家 {shallow.get('win_pct')}% · 輸家 {shallow.get('loss_pct')}%",
            "",
            "## W3 象限分布 %",
            "",
            "| 象限 | 贏家% | 輸家% |",
            "|------|------:|------:|",
        ]
    )
    w3q = prof.get("w3_quadrant") or {}
    win_q = w3q.get("win_pct") or {}
    loss_q = w3q.get("loss_pct") or {}
    for q in sorted(set(win_q) | set(loss_q)):
        lines.append(f"| {q} | {win_q.get(q, 0)} | {loss_q.get(q, 0)} |")

    sp = prof.get("spread_class") or {}
    lines.extend(["", "## Spread class %", "", "| class | 贏家% | 輸家% |", "|-------|------:|------:|"])
    for q in SPREAD_CLASSES:
        lines.append(
            f"| {q} | {(sp.get('win_pct') or {}).get(q, 0)} | "
            f"{(sp.get('loss_pct') or {}).get(q, 0)} |"
        )

    lines.extend(
        [
            "",
            "## 因子鑑別掃描（二元 · 依 mean ret lift 排序）",
            "",
            "| 因子 | n+ | mean+ | win+ | n− | mean− | win− | lift | d |",
            "|------|---:|------:|-----:|---:|------:|-----:|-----:|--:|",
        ]
    )
    for row in (prof.get("factor_discriminators") or [])[:25]:
        lines.append(
            f"| {row.get('label')} | {row.get('n_pos')} | {row.get('mean_ret_pos')} | "
            f"{row.get('win_rate_pos')} | {row.get('n_neg')} | {row.get('mean_ret_neg')} | "
            f"{row.get('win_rate_neg')} | {row.get('lift')} | {row.get('cohen_d', '—')} |"
        )

    disc_bt = payload.get("discriminator_backtest") or {}
    if disc_bt.get("summaries"):
        lines.extend(
            [
                "",
                "## Top 鑑別因子回測",
                "",
                "| Filter | n | mean ret% | win% | median% |",
                "|--------|--:|----------:|-----:|--------:|",
            ]
        )
        for pid, row in disc_bt.get("summaries", {}).items():
            lines.append(
                f"| {pid} | {row.get('n', 0)} | {row.get('mean_ret_3d_net_pct', '—')} | "
                f"{row.get('win_rate_pct', '—')} | {row.get('median_ret_3d_net_pct', '—')} |"
            )

    lines.extend(
        [
            "",
            "## 過濾假說回測",
            "",
            "| Filter | n | mean ret% | win% | median% | Δ vs base |",
            "|--------|--:|----------:|-----:|--------:|----------:|",
        ]
    )
    base_ret = float((s.get("baseline_w3_rv_hl") or {}).get("mean_ret_3d_net_pct") or 0)
    for pid, label, _ in FILTER_HYPOTHESES():
        row = s.get(pid) or {}
        ret = float(row.get("mean_ret_3d_net_pct") or 0) if row.get("n") else None
        delta = round(ret - base_ret, 3) if ret is not None and row.get("n") else "—"
        lines.append(
            f"| {label} | {row.get('n', 0)} | {row.get('mean_ret_3d_net_pct', '—')} | "
            f"{row.get('win_rate_pct', '—')} | {row.get('median_ret_3d_net_pct', '—')} | {delta} |"
        )

    lines.extend(["", "## 假說判定", ""])
    for h in v.get("hypotheses") or []:
        lines.append(
            f"- **{h.get('id')}** ({h.get('status')}): {h.get('statement')} · "
            f"n={h.get('n')} ret={h.get('mean_ret_3d_net_pct')}%"
        )
    lines.extend(["", "## Verdict", "", f"- **Recommend**: {v.get('recommend')}", ""])
    for r in v.get("reasons") or []:
        lines.append(f"- {r}")
    lines.extend(["", "模組：`scripts/run_w3_winner_profile.py`"])
    return "\n".join(lines)


def _matches_w3_abc_a(p: dict[str, Any]) -> bool:
    if not matches_triple_w3_rv_high_low(p):
        return False
    rv20, rv3 = _rv(p, "w20"), _rv(p, micro_leg_key(WMA_MICRO_LENGTH))
    return rv20 is not None and rv3 is not None and float(rv20) - float(rv3) <= W3_ABC_W20_W3_SPREAD_MAX


def _matches_w3_abc_b(p: dict[str, Any]) -> bool:
    if not matches_triple_w3_rv_high_low(p):
        return False
    return _q(p, "w20") == "leading" and _q(p, "w5") == "weakening"


def _matches_w3_abc_c(p: dict[str, Any]) -> bool:
    if not matches_triple_w3_rv_high_low(p):
        return False
    rv5 = _rv(p, "w5")
    return rv5 is not None and float(rv5) <= W3_ABC_W5_RV_MAX


def backtest_w3_abc_v2_oos(
    conn: sqlite3.Connection,
    *,
    dates: list[str],
    exit_rule: str = "hold_3d",
    etf_codes: tuple[str, ...] = DEFAULT_ETF_CODES,
) -> dict[str, Any]:
    matchers: list[tuple[str, Callable[[dict[str, Any]], bool]]] = [
        ("baseline_w3_rv_hl", matches_triple_w3_rv_high_low),
        ("abc_v2_full", matches_w3_rv_hl_abc_v2),
        ("abc_a_spread_lte_6", _matches_w3_abc_a),
        ("abc_b_leading_weakening", _matches_w3_abc_b),
        ("abc_c_w5_rv_lte_101", _matches_w3_abc_c),
    ]
    pattern_rows, meta, _ = _backtest_named_matchers(
        conn, dates=dates, matchers=matchers, exit_rule=exit_rule, etf_codes=etf_codes
    )
    summaries = {pid: _summarize_trades(rows) for pid, rows in pattern_rows.items()}
    base = summaries.get("baseline_w3_rv_hl") or {}
    abc = summaries.get("abc_v2_full") or {}
    base_ret = float(base.get("mean_ret_3d_net_pct") or 0)
    abc_ret = float(abc.get("mean_ret_3d_net_pct") or 0)
    return {
        "dates": dates,
        "meta": meta,
        "exit_rule": exit_rule,
        "spec": {
            "A": f"W20−W3 RV ≤ {W3_ABC_W20_W3_SPREAD_MAX}",
            "B": "W20 Leading · W5 Weakening",
            "C": f"W5 RV ≤ {W3_ABC_W5_RV_MAX}",
        },
        "summaries": summaries,
        "delta_abc_vs_base": round(abc_ret - base_ret, 3),
    }


def verdict_w3_abc_v2_oos(bt: dict[str, Any], *, min_n: int = 30) -> dict[str, Any]:
    s = bt.get("summaries") or {}
    base = s.get("baseline_w3_rv_hl") or {}
    abc = s.get("abc_v2_full") or {}
    base_n = int(base.get("n") or 0)
    abc_n = int(abc.get("n") or 0)
    base_ret = float(base.get("mean_ret_3d_net_pct") or 0)
    abc_ret = float(abc.get("mean_ret_3d_net_pct") or 0)
    reasons: list[str] = []
    passed = False
    if abc_n >= min_n and abc_ret > base_ret + 0.05:
        passed = True
        reasons.append(f"ABC v2 OOS beats baseline ({abc_ret}% vs {base_ret}%) · n {base_n}→{abc_n}")
    elif abc_n >= min_n:
        reasons.append(f"ABC v2 OOS does not beat baseline ({abc_ret}% vs {base_ret}%)")
    else:
        reasons.append(f"ABC v2 n={abc_n} < min_n={min_n}")
    med = float(abc.get("median_ret_3d_net_pct") or 0)
    reasons.append(f"ABC v2 median={med}% · win={abc.get('win_rate_pct')}%")
    return {
        "passed": passed,
        "recommend": "w3_rv_hl_abc_v2" if passed else "triple_w3_rv_high_low",
        "baseline": base,
        "abc_v2": abc,
        "reasons": reasons,
        "summary": (
            f"OOS ABC v2 · n={abc_n} ret={abc_ret}% win={abc.get('win_rate_pct')}% · "
            f"baseline n={base_n} ret={base_ret}% · "
            f"{'**passed**' if passed else 'hold baseline'}"
        ),
    }


def run_w3_abc_v2_oos_study(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2025-01-01",
    date_end: str | None = None,
    intraday_tail_days: int = 90,
    min_n: int = 30,
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
    bt = backtest_w3_abc_v2_oos(conn, dates=sample_dates, etf_codes=etf_codes)
    verdict = verdict_w3_abc_v2_oos(bt, min_n=min_n)
    return {
        "schema": "w3_rv_hl_abc_v2_oos-v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "date_start": date_start,
        "date_end": end,
        "intraday_sample_dates": sample_dates,
        "intraday_tail_days": intraday_tail_days,
        "min_n": min_n,
        "backtest": bt,
        "verdict": verdict,
    }


def render_w3_abc_v2_oos_md(payload: dict[str, Any]) -> str:
    v = payload.get("verdict") or {}
    bt = payload.get("backtest") or {}
    s = bt.get("summaries") or {}
    spec = bt.get("spec") or {}
    lines = [
        "# W3 RV high/low · ABC v2 OOS",
        "",
        f"> {v.get('summary', '')}",
        "",
        "## Matcher `w3_rv_hl_abc_v2`",
        "",
        "- **Base**: W20 RV>100 · W5 RV>100 · W3 RV<100",
        f"- **A**: {spec.get('A')}",
        f"- **B**: {spec.get('B')}",
        f"- **C**: {spec.get('C')}",
        "",
        f"- Sample: **{len(payload.get('intraday_sample_dates') or [])}** trading days "
        f"(tail **{payload.get('intraday_tail_days')}**)",
        f"- Exit: **{bt.get('exit_rule')}**",
        "",
        "## Outcomes",
        "",
        "| Pattern | n | mean ret net% | win% | median% | mean excess% |",
        "|---------|--:|--------------:|-----:|--------:|-------------:|",
    ]
    labels = {
        "baseline_w3_rv_hl": "baseline W3 RV high/low",
        "abc_v2_full": "**ABC v2 (A∩B∩C)**",
        "abc_a_spread_lte_6": "A only · spread≤6",
        "abc_b_leading_weakening": "B only · Ld/We",
        "abc_c_w5_rv_lte_101": "C only · W5 RV≤101",
    }
    for pid in labels:
        row = s.get(pid) or {}
        lines.append(
            f"| {labels[pid]} | {row.get('n', 0)} | {row.get('mean_ret_3d_net_pct', '—')} | "
            f"{row.get('win_rate_pct', '—')} | {row.get('median_ret_3d_net_pct', '—')} | "
            f"{row.get('mean_excess_3d_pct', '—')} |"
        )
    lines.extend(["", "## Verdict", "", f"- **Recommend**: {v.get('recommend')}", ""])
    for r in v.get("reasons") or []:
        lines.append(f"- {r}")
    lines.extend(["", "模組：`scripts/run_w3_abc_v2_oos.py`"])
    return "\n".join(lines)


def _matches_w3_abc_d(p: dict[str, Any]) -> bool:
    """ABC v2 + D: W20 RV ≤ 104."""
    if not matches_w3_rv_hl_abc_v2(p):
        return False
    rv20 = _rv(p, "w20")
    return rv20 is not None and float(rv20) <= W3_ABC_V3_W20_RV_MAX


def _matches_w3_abc_e(p: dict[str, Any]) -> bool:
    """ABC v2 + E: lead_pullback_buy trade_signal."""
    if not matches_w3_rv_hl_abc_v2(p):
        return False
    return p.get("trade_signal") == "lead_pullback_buy"


def backtest_w3_abc_v3_oos(
    conn: sqlite3.Connection,
    *,
    dates: list[str],
    exit_rule: str = "hold_3d",
    etf_codes: tuple[str, ...] = DEFAULT_ETF_CODES,
    frames: list[dict[str, str]] | None = None,
    trajectories: list[dict[str, Any]] | None = None,
    meta: dict[str, Any] | None = None,
    stock_maps: dict[str, list[dict[str, Any]]] | None = None,
    bench: Any | None = None,
) -> dict[str, Any]:
    matchers: list[tuple[str, Callable[[dict[str, Any]], bool]]] = [
        ("baseline_w3_rv_hl", matches_triple_w3_rv_high_low),
        ("abc_v2_full", matches_w3_rv_hl_abc_v2),
        ("abc_v3_full", matches_w3_rv_hl_abc_v3),
        ("abc_v3_f1", matches_w3_rv_hl_abc_v3_f1),
        ("abc_d_w20_rv_lte_104", _matches_w3_abc_d),
        ("abc_e_lead_pullback", _matches_w3_abc_e),
    ]
    pattern_rows, panel_meta, _ = _backtest_named_matchers(
        conn,
        dates=dates,
        matchers=matchers,
        exit_rule=exit_rule,
        etf_codes=etf_codes,
        frames=frames,
        trajectories=trajectories,
        meta=meta,
        stock_maps=stock_maps,
        bench=bench,
    )
    summaries = {pid: _summarize_trades(rows) for pid, rows in pattern_rows.items()}
    base = summaries.get("baseline_w3_rv_hl") or {}
    v2 = summaries.get("abc_v2_full") or {}
    v3 = summaries.get("abc_v3_full") or {}
    f1 = summaries.get("abc_v3_f1") or {}
    base_ret = float(base.get("mean_ret_3d_net_pct") or 0)
    v2_ret = float(v2.get("mean_ret_3d_net_pct") or 0)
    v3_ret = float(v3.get("mean_ret_3d_net_pct") or 0)
    f1_ret = float(f1.get("mean_ret_3d_net_pct") or 0)
    return {
        "dates": dates,
        "meta": panel_meta,
        "exit_rule": exit_rule,
        "spec": {
            "base": "W20 RV>100 · W5 RV>100 · W3 RV<100",
            "A": f"W20−W3 RV ≤ {W3_ABC_W20_W3_SPREAD_MAX}",
            "B": "W20 Leading · W5 Weakening",
            "C": f"W5 RV ≤ {W3_ABC_W5_RV_MAX}",
            "D": f"W20 RV ≤ {W3_ABC_V3_W20_RV_MAX}",
            "E": "trade_signal = lead_pullback_buy",
            "F": f"W5−W3 MV < {W3_ABC_F1_W5_W3_MV_SPREAD_MAX} (avoid W5 MV > W3 MV)",
        },
        "summaries": summaries,
        "delta_v2_vs_base": round(v2_ret - base_ret, 3),
        "delta_v3_vs_v2": round(v3_ret - v2_ret, 3),
        "delta_v3_vs_base": round(v3_ret - base_ret, 3),
        "delta_f1_vs_v3": round(f1_ret - v3_ret, 3),
        "delta_f1_vs_base": round(f1_ret - base_ret, 3),
    }


def verdict_w3_abc_v3_oos(bt: dict[str, Any], *, min_n: int = 30) -> dict[str, Any]:
    s = bt.get("summaries") or {}
    base = s.get("baseline_w3_rv_hl") or {}
    v2 = s.get("abc_v2_full") or {}
    v3 = s.get("abc_v3_full") or {}
    f1 = s.get("abc_v3_f1") or {}
    base_n = int(base.get("n") or 0)
    v2_n = int(v2.get("n") or 0)
    v3_n = int(v3.get("n") or 0)
    f1_n = int(f1.get("n") or 0)
    base_ret = float(base.get("mean_ret_3d_net_pct") or 0)
    v2_ret = float(v2.get("mean_ret_3d_net_pct") or 0)
    v3_ret = float(v3.get("mean_ret_3d_net_pct") or 0)
    f1_ret = float(f1.get("mean_ret_3d_net_pct") or 0)
    reasons: list[str] = []
    passed = False
    f1_passed = False
    recommend = "triple_w3_rv_high_low"
    if v3_n >= min_n and v3_ret > v2_ret + 0.05:
        passed = True
        recommend = "w3_rv_hl_abc_v3"
        reasons.append(f"ABC v3 OOS beats v2 ({v3_ret}% vs {v2_ret}%) · n {v2_n}→{v3_n}")
    elif v2_n >= min_n and v2_ret > base_ret + 0.05:
        recommend = "w3_rv_hl_abc_v2"
        reasons.append(f"ABC v3 does not beat v2 ({v3_ret}% vs {v2_ret}%) · hold v2")
    elif v3_n >= min_n:
        reasons.append(f"ABC v3 OOS does not beat v2 ({v3_ret}% vs {v2_ret}%)")
    else:
        reasons.append(f"ABC v3 n={v3_n} < min_n={min_n}")
    if f1_n >= max(10, min_n // 2) and f1_ret >= v3_ret - 0.05:
        f1_passed = f1_ret > v3_ret or (
            f1_ret >= v3_ret - 0.02 and int(f1.get("win_rate_pct") or 0) >= int(v3.get("win_rate_pct") or 0)
        )
        reasons.append(
            f"ABC v3+f1 · n={f1_n} ret={f1_ret}% vs v3 {v3_ret}% · "
            f"win {f1.get('win_rate_pct')}% vs {v3.get('win_rate_pct')}% · "
            f"{'**f1 ok**' if f1_passed else 'f1 weaker'}"
        )
    reasons.append(
        f"baseline n={base_n} ret={base_ret}% · v2 n={v2_n} ret={v2_ret}% · "
        f"v3 median={v3.get('median_ret_3d_net_pct')}% win={v3.get('win_rate_pct')}%"
    )
    return {
        "passed": passed,
        "f1_passed": f1_passed,
        "recommend": recommend,
        "baseline": base,
        "abc_v2": v2,
        "abc_v3": v3,
        "abc_v3_f1": f1,
        "reasons": reasons,
        "summary": (
            f"OOS ABC v3 · n={v3_n} ret={v3_ret}% · f1 n={f1_n} ret={f1_ret}% · "
            f"v2 n={v2_n} ret={v2_ret}% · baseline n={base_n} ret={base_ret}% · "
            f"{'**passed**' if passed else 'hold v2 or baseline'}"
        ),
    }


def run_w3_abc_v3_oos_study(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2025-01-01",
    date_end: str | None = None,
    intraday_tail_days: int = 90,
    min_n: int = 30,
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
    bt = backtest_w3_abc_v3_oos(conn, dates=sample_dates, etf_codes=etf_codes)
    verdict = verdict_w3_abc_v3_oos(bt, min_n=min_n)
    return {
        "schema": "w3_rv_hl_abc_v3_oos-v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "date_start": date_start,
        "date_end": end,
        "intraday_sample_dates": sample_dates,
        "intraday_tail_days": intraday_tail_days,
        "min_n": min_n,
        "backtest": bt,
        "verdict": verdict,
    }


def render_w3_abc_v3_oos_md(payload: dict[str, Any]) -> str:
    v = payload.get("verdict") or {}
    bt = payload.get("backtest") or {}
    s = bt.get("summaries") or {}
    spec = bt.get("spec") or {}
    lines = [
        "# W3 RV high/low · ABC v3 OOS",
        "",
        f"> {v.get('summary', '')}",
        "",
        "## Matcher `w3_rv_hl_abc_v3`",
        "",
        f"- **Base**: {spec.get('base')}",
        f"- **A**: {spec.get('A')}",
        f"- **B**: {spec.get('B')}",
        f"- **C**: {spec.get('C')}",
        f"- **D**: {spec.get('D')}",
        f"- **E**: {spec.get('E')}",
        f"- **F** (`abc_v3_f1`): {spec.get('F')}",
        "",
        f"- Sample: **{len(payload.get('intraday_sample_dates') or [])}** trading days "
        f"(tail **{payload.get('intraday_tail_days')}**)",
        f"- Exit: **{bt.get('exit_rule')}**",
        "",
        "## Outcomes",
        "",
        "| Pattern | n | mean ret net% | win% | median% | mean excess% |",
        "|---------|--:|--------------:|-----:|--------:|-------------:|",
    ]
    labels = {
        "baseline_w3_rv_hl": "baseline W3 RV high/low",
        "abc_v2_full": "ABC v2 (A∩B∩C)",
        "abc_v3_full": "**ABC v3 (A∩B∩C∩D∩E)**",
        "abc_v3_f1": "**ABC v3+f1 (A∩B∩C∩D∩E∩F)**",
        "abc_d_w20_rv_lte_104": "v2 + D · W20 RV≤104",
        "abc_e_lead_pullback": "v2 + E · lead_pullback",
    }
    for pid in labels:
        row = s.get(pid) or {}
        lines.append(
            f"| {labels[pid]} | {row.get('n', 0)} | {row.get('mean_ret_3d_net_pct', '—')} | "
            f"{row.get('win_rate_pct', '—')} | {row.get('median_ret_3d_net_pct', '—')} | "
            f"{row.get('mean_excess_3d_pct', '—')} |"
        )
    lines.extend(
        [
            "",
            f"- Δ v3 vs v2 mean ret: **{bt.get('delta_v3_vs_v2')}** pp",
            f"- Δ v3 vs baseline mean ret: **{bt.get('delta_v3_vs_base')}** pp",
            f"- Δ f1 vs v3 mean ret: **{bt.get('delta_f1_vs_v3')}** pp",
            "",
            "## Verdict",
            "",
            f"- **Recommend**: {v.get('recommend')}",
            "",
        ]
    )
    for r in v.get("reasons") or []:
        lines.append(f"- {r}")
    lines.extend(["", "模組：`scripts/run_w3_abc_v3_oos.py`"])
    return "\n".join(lines)


ABC_V3_ENTRY_GATE_SPECS: tuple[tuple[str, int, str], ...] = (
    ("first_trigger", 0, "ABC v3 · first trigger"),
    ("skip_0900", 1, "≥09:30 (skip 09:00)"),
    ("gte_1030", 3, "≥10:30"),
    ("gte_1100", 4, "≥11:00"),
    ("gte_1200", 6, "≥12:00"),
)


def _poll_idx_for_frame(frames: list[dict[str, str]], frame_i: int) -> int | None:
    minute = str((frames[frame_i] or {}).get("minute") or "")
    if minute in INTRADAY_POLL_MINUTES:
        return INTRADAY_POLL_MINUTES.index(minute)
    return None


def backtest_abc_v3_entry_gate_sweep(
    conn: sqlite3.Connection,
    *,
    dates: list[str],
    exit_rule: str = "hold_3d",
    etf_codes: tuple[str, ...] = DEFAULT_ETF_CODES,
) -> dict[str, Any]:
    """ABC v3 · execution gate sweep (min poll idx · first match at/after gate)."""
    frames, trajectories, meta = build_dual_wma_intraday_trajectories(
        conn,
        dates=dates,
        etf_codes=etf_codes,
        micro_lengths=(WMA_ULTRA_LENGTH, WMA_MICRO_LENGTH),
    )
    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index).astype(float)
    stock_maps = _build_stock_frame_maps(trajectories, frames)
    frame_ids = [f["id"] for f in frames]
    pattern_rows: dict[str, list[dict[str, Any]]] = {
        gid: [] for gid, _, _ in ABC_V3_ENTRY_GATE_SPECS
    }
    seen_by_gate: dict[str, set[tuple[str, str]]] = {gid: set() for gid, _, _ in ABC_V3_ENTRY_GATE_SPECS}

    for t in trajectories:
        sid = t["stock_id"]
        by_id = {p["frame_id"]: p for p in t["points"]}
        pts = stock_maps.get(sid)
        if not pts:
            continue
        for i, fid in enumerate(frame_ids):
            poll_idx = _poll_idx_for_frame(frames, i)
            if poll_idx is None:
                continue
            p = by_id.get(fid)
            if not p or not matches_w3_rv_hl_abc_v3(p):
                continue
            d = frames[i]["date"]
            for gid, min_poll, _ in ABC_V3_ENTRY_GATE_SPECS:
                if poll_idx < min_poll:
                    continue
                key = (sid, d)
                if key in seen_by_gate[gid]:
                    continue
                seen_by_gate[gid].add(key)
                exit_idx = _exit_frame_idx(exit_rule, i, frames, pts)
                tr = _trade_return(conn, bench, stock_maps, sid, i, exit_idx, frames)
                if tr is None:
                    continue
                minute = str(p.get("minute") or frames[i].get("minute") or "")
                pattern_rows[gid].append(
                    {
                        **tr,
                        "stock_id": sid,
                        "entry_date": d,
                        "entry_minute": minute,
                        "entry_poll_idx": poll_idx,
                    }
                )
            # All gates share the same first qualifying poll for this stock-day; break outer
            # only after we've evaluated — actually we must NOT break early because
            # different gates may accept different first polls on the same day.
            # Continue scanning frames; seen_day per gate handles first-match per gate.

    summaries = {gid: _summarize_trades(rows) for gid, rows in pattern_rows.items()}
    baseline = summaries.get("first_trigger") or {}
    baseline_ret = float(baseline.get("mean_ret_3d_net_pct") or 0)
    baseline_n = int(baseline.get("n") or 0)
    gates: list[dict[str, Any]] = []
    for gid, min_poll, label in ABC_V3_ENTRY_GATE_SPECS:
        row = summaries.get(gid) or {}
        ret = float(row.get("mean_ret_3d_net_pct") or 0)
        gates.append(
            {
                "id": gid,
                "min_poll_idx": min_poll,
                "min_poll_minute": INTRADAY_POLL_MINUTES[min_poll] if min_poll < len(INTRADAY_POLL_MINUTES) else "—",
                "label": label,
                **row,
                "delta_vs_first": round(ret - baseline_ret, 3),
                "fill_rate_pct": round(100.0 * int(row.get("n") or 0) / baseline_n, 1) if baseline_n else None,
            }
        )

    first_rows = pattern_rows.get("first_trigger") or []
    post_hoc_mid = [
        t
        for t in first_rows
        if str(t.get("entry_minute") or "") > "10:00" and str(t.get("entry_minute") or "") < "13:00"
    ]
    post_hoc_gte_1100 = [
        t for t in first_rows if int(t.get("entry_poll_idx") or -1) >= 4
    ]

    return {
        "dates": dates,
        "meta": meta,
        "exit_rule": exit_rule,
        "matcher": "w3_rv_hl_abc_v3",
        "gate_specs": [
            {"id": gid, "min_poll_idx": mp, "label": lbl} for gid, mp, lbl in ABC_V3_ENTRY_GATE_SPECS
        ],
        "summaries": summaries,
        "gates": gates,
        "baseline_first_trigger": baseline,
        "post_hoc_slices": {
            "entry_mid_session": _summarize_trades(post_hoc_mid),
            "entry_gte_1100": _summarize_trades(post_hoc_gte_1100),
        },
    }


def verdict_abc_v3_entry_gate_sweep(bt: dict[str, Any], *, min_n: int = 15) -> dict[str, Any]:
    baseline = bt.get("baseline_first_trigger") or {}
    base_ret = float(baseline.get("mean_ret_3d_net_pct") or 0)
    base_n = int(baseline.get("n") or 0)
    best: dict[str, Any] | None = None
    for row in bt.get("gates") or []:
        if row.get("id") == "first_trigger":
            continue
        n = int(row.get("n") or 0)
        ret = float(row.get("mean_ret_3d_net_pct") or 0)
        if n < min_n:
            continue
        if best is None or ret > float(best.get("mean_ret_3d_net_pct") or 0):
            best = row
    reasons: list[str] = []
    passed = False
    recommend = "first_trigger"
    if best and float(best.get("mean_ret_3d_net_pct") or 0) > base_ret + 0.05:
        passed = True
        recommend = str(best.get("id") or "first_trigger")
        reasons.append(
            f"Gate {best.get('label')} beats first trigger "
            f"({best.get('mean_ret_3d_net_pct')}% vs {base_ret}%) · "
            f"n {base_n}→{best.get('n')} · fill {best.get('fill_rate_pct')}%"
        )
    elif best:
        reasons.append(
            f"Best gate {best.get('label')} does not beat first trigger "
            f"({best.get('mean_ret_3d_net_pct')}% vs {base_ret}%)"
        )
    else:
        reasons.append(f"No gated variant reached min_n={min_n}")
    post = bt.get("post_hoc_slices") or {}
    mid = post.get("entry_mid_session") or {}
    reasons.append(
        f"post-hoc mid slice (not execution): n={mid.get('n')} ret={mid.get('mean_ret_3d_net_pct')}%"
    )
    return {
        "passed": passed,
        "recommend": recommend,
        "baseline": baseline,
        "best_gate": best,
        "reasons": reasons,
        "summary": (
            f"ABC v3 entry gate · baseline n={base_n} ret={base_ret}% · "
            f"best={'**' + str((best or {}).get('id')) + '**' if best else '—'} "
            f"{'**passed**' if passed else 'hold first trigger'}"
        ),
    }


def run_abc_v3_entry_gate_study(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2025-01-01",
    date_end: str | None = None,
    intraday_tail_days: int = 30,
    min_n: int = 15,
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
    bt = backtest_abc_v3_entry_gate_sweep(conn, dates=sample_dates, etf_codes=etf_codes)
    verdict = verdict_abc_v3_entry_gate_sweep(bt, min_n=min_n)
    return {
        "schema": "abc_v3_entry_gate_sweep-v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "date_start": date_start,
        "date_end": end,
        "intraday_sample_dates": sample_dates,
        "intraday_tail_days": intraday_tail_days,
        "min_n": min_n,
        "backtest": bt,
        "verdict": verdict,
    }


def render_abc_v3_entry_gate_md(payload: dict[str, Any]) -> str:
    v = payload.get("verdict") or {}
    bt = payload.get("backtest") or {}
    post = bt.get("post_hoc_slices") or {}
    lines = [
        "# ABC v3 · entry gate sweep (execution)",
        "",
        f"> {v.get('summary', '')}",
        "",
        "## 機制",
        "",
        "- Matcher：**ABC v3** (A∩B∩C∩D∩E)",
        "- **Execution gate**：當日跳過 poll idx < gate 的格子，**第一個**符合的 poll 進場",
        "- 與 winner profile「事後 mid 切片」不同 — 此為真實延後執行回測",
        "",
        f"- Sample: **{len(payload.get('intraday_sample_dates') or [])}** trading days "
        f"(tail **{payload.get('intraday_tail_days')}**)",
        f"- Exit: **{bt.get('exit_rule')}**",
        "",
        "## Execution gate outcomes",
        "",
        "| Gate | min poll | n | fill% | mean ret% | win% | median% | Δ vs first |",
        "|------|----------|--:|------:|----------:|-----:|--------:|-----------:|",
    ]
    for row in bt.get("gates") or []:
        lines.append(
            f"| {row.get('label')} | {row.get('min_poll_minute')} | {row.get('n', 0)} | "
            f"{row.get('fill_rate_pct', '—')} | {row.get('mean_ret_3d_net_pct', '—')} | "
            f"{row.get('win_rate_pct', '—')} | {row.get('median_ret_3d_net_pct', '—')} | "
            f"{row.get('delta_vs_first', '—')} |"
        )
    mid = post.get("entry_mid_session") or {}
    g11 = post.get("entry_gte_1100") or {}
    lines.extend(
        [
            "",
            "## 對照 · 事後切片（非執行規則）",
            "",
            "同一 `first_trigger` 樣本的事後分組，**不可**當延後執行預期：",
            "",
            f"- entry mid session (10:30–12:30)：n={mid.get('n')} ret={mid.get('mean_ret_3d_net_pct')}% win={mid.get('win_rate_pct')}%",
            f"- entry ≥11:00 post-hoc：n={g11.get('n')} ret={g11.get('mean_ret_3d_net_pct')}% win={g11.get('win_rate_pct')}%",
            "",
            "## Verdict",
            "",
            f"- **Recommend**: {v.get('recommend')}",
            "",
        ]
    )
    for r in v.get("reasons") or []:
        lines.append(f"- {r}")
    lines.extend(["", "模組：`scripts/run_abc_v3_entry_gate_sweep.py`"])
    return "\n".join(lines)


ABC_V3_ADOPTED_MIN_POLL_IDX = 1  # skip 09:00 poll
HOLD_DAYS_SWEEP = tuple(range(2, 9))


def collect_abc_v3_skip0900_entry_events(
    trajectories: list[dict[str, Any]],
    frames: list[dict[str, str]],
    *,
    min_poll_idx: int = ABC_V3_ADOPTED_MIN_POLL_IDX,
) -> list[dict[str, Any]]:
    """First ABC v3 match per stock-day at/after min_poll_idx."""
    frame_ids = [f["id"] for f in frames]
    events: list[dict[str, Any]] = []
    seen_day: set[tuple[str, str]] = set()
    for t in trajectories:
        sid = t["stock_id"]
        by_id = {p["frame_id"]: p for p in t["points"]}
        for i, fid in enumerate(frame_ids):
            poll_idx = _poll_idx_for_frame(frames, i)
            if poll_idx is None or poll_idx < min_poll_idx:
                continue
            p = by_id.get(fid)
            if not p or not matches_w3_rv_hl_abc_v3(p):
                continue
            d = frames[i]["date"]
            key = (sid, d)
            if key in seen_day:
                continue
            seen_day.add(key)
            events.append(
                {
                    "stock_id": sid,
                    "entry_frame_idx": i,
                    "entry_date": d,
                    "entry_minute": str(p.get("minute") or frames[i].get("minute") or ""),
                    "entry_poll_idx": poll_idx,
                }
            )
    return events


def _summarize_hold_days_trades(rows: list[dict[str, Any]], *, hold_days: int) -> dict[str, Any]:
    base = _summarize_trades(rows)
    if not rows:
        return {**base, "hold_days": hold_days, "mean_daily_ret_net_pct": None, "mean_daily_excess_pct": None}
    daily_rets = [float(r["net_pct"]) / hold_days for r in rows]
    daily_excess = [float(r.get("excess_pct") or 0) / hold_days for r in rows]
    return {
        **base,
        "hold_days": hold_days,
        "mean_ret_net_pct": base.get("mean_ret_3d_net_pct"),
        "mean_daily_ret_net_pct": round(sum(daily_rets) / len(daily_rets), 4),
        "mean_daily_excess_pct": round(sum(daily_excess) / len(daily_excess), 4),
    }


def backtest_abc_v3_skip0900_hold_sweep(
    conn: sqlite3.Connection,
    *,
    dates: list[str],
    hold_days: tuple[int, ...] = HOLD_DAYS_SWEEP,
    etf_codes: tuple[str, ...] = DEFAULT_ETF_CODES,
) -> dict[str, Any]:
    """ABC v3 + skip 09:00 · hold 2–8 trading days → 13:30 exit."""
    frames, trajectories, meta = build_dual_wma_intraday_trajectories(
        conn,
        dates=dates,
        etf_codes=etf_codes,
        micro_lengths=(WMA_ULTRA_LENGTH, WMA_MICRO_LENGTH),
    )
    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index).astype(float)
    stock_maps = _build_stock_frame_maps(trajectories, frames)
    entries = collect_abc_v3_skip0900_entry_events(trajectories, frames)
    summaries: dict[str, Any] = {}
    rows_by_hold: dict[int, list[dict[str, Any]]] = {}

    for hd in hold_days:
        exit_rule = f"hold_{hd}d"
        rows: list[dict[str, Any]] = []
        for ev in entries:
            sid = str(ev["stock_id"])
            i = int(ev["entry_frame_idx"])
            pts = stock_maps.get(sid)
            if not pts:
                continue
            exit_idx = _exit_frame_idx(exit_rule, i, frames, pts)
            tr = _trade_return(conn, bench, stock_maps, sid, i, exit_idx, frames)
            if tr is None:
                continue
            rows.append({**tr, **ev, "hold_days": hd})
        rows_by_hold[hd] = rows
        summaries[str(hd)] = _summarize_hold_days_trades(rows, hold_days=hd)

    hold_rows = [
        {"hold_days": hd, **(summaries.get(str(hd)) or {})}
        for hd in hold_days
    ]
    hold_rows.sort(key=lambda x: float(x.get("mean_daily_ret_net_pct") or 0), reverse=True)

    return {
        "dates": dates,
        "meta": meta,
        "matcher": "w3_rv_hl_abc_v3",
        "execution_gate": f"min_poll_idx>={ABC_V3_ADOPTED_MIN_POLL_IDX} (skip 09:00)",
        "exit_template": "hold_{n}d → 13:30",
        "n_entry_events": len(entries),
        "hold_days_sweep": list(hold_days),
        "summaries": summaries,
        "by_hold_days": hold_rows,
    }


def verdict_abc_v3_hold_sweep(bt: dict[str, Any], *, min_n: int = 30) -> dict[str, Any]:
    rows = list(bt.get("by_hold_days") or [])
    eligible = [r for r in rows if int(r.get("n") or 0) >= min_n]
    best_daily = max(eligible, key=lambda x: float(x.get("mean_daily_ret_net_pct") or 0), default=None)
    best_total = max(eligible, key=lambda x: float(x.get("mean_ret_net_pct") or 0), default=None)
    hold3 = next((r for r in rows if int(r.get("hold_days") or 0) == 3), None)
    reasons: list[str] = []
    recommend_hold = 3
    if best_daily:
        recommend_hold = int(best_daily.get("hold_days") or 3)
        reasons.append(
            f"Sweet spot (mean daily ret): hold {best_daily.get('hold_days')}d · "
            f"daily {best_daily.get('mean_daily_ret_net_pct')}% · "
            f"total {best_daily.get('mean_ret_net_pct')}% · n={best_daily.get('n')}"
        )
    if best_total and best_total is not best_daily:
        reasons.append(
            f"Best total ret: hold {best_total.get('hold_days')}d · "
            f"total {best_total.get('mean_ret_net_pct')}% · "
            f"daily {best_total.get('mean_daily_ret_net_pct')}%"
        )
    if hold3:
        reasons.append(
            f"baseline hold 3d: total {hold3.get('mean_ret_net_pct')}% · "
            f"daily {hold3.get('mean_daily_ret_net_pct')}%"
        )
    return {
        "recommend_hold_days": recommend_hold,
        "best_daily": best_daily,
        "best_total": best_total,
        "reasons": reasons,
        "summary": (
            f"ABC v3 skip09:00 hold sweep · sweet spot hold **{recommend_hold}d** · "
            f"daily {((best_daily or {}).get('mean_daily_ret_net_pct'))}% · "
            f"n_entries={bt.get('n_entry_events')}"
        ),
    }


def run_abc_v3_hold_sweep_study(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2025-01-01",
    date_end: str | None = None,
    intraday_tail_days: int = 90,
    min_n: int = 30,
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
    bt = backtest_abc_v3_skip0900_hold_sweep(conn, dates=sample_dates, etf_codes=etf_codes)
    verdict = verdict_abc_v3_hold_sweep(bt, min_n=min_n)
    return {
        "schema": "abc_v3_skip0900_hold_sweep-v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "date_start": date_start,
        "date_end": end,
        "intraday_sample_dates": sample_dates,
        "intraday_tail_days": intraday_tail_days,
        "min_n": min_n,
        "backtest": bt,
        "verdict": verdict,
    }


def render_abc_v3_hold_sweep_md(payload: dict[str, Any]) -> str:
    v = payload.get("verdict") or {}
    bt = payload.get("backtest") or {}
    lines = [
        "# ABC v3 + skip 09:00 · hold days sweep",
        "",
        f"> {v.get('summary', '')}",
        "",
        "## 規格",
        "",
        f"- Matcher: **{bt.get('matcher')}**",
        f"- Execution: **{bt.get('execution_gate')}**",
        f"- Exit: **{bt.get('exit_template')}**",
        f"- Entry events: **{bt.get('n_entry_events')}**",
        f"- Sample: **{len(payload.get('intraday_sample_dates') or [])}** trading days "
        f"(tail **{payload.get('intraday_tail_days')}**)",
        "",
        "## Hold 2–8 日 · 報酬",
        "",
        "| hold | n | mean total ret% | mean daily ret% | mean daily excess% | win% | median total% |",
        "|-----:|--:|----------------:|----------------:|-------------------:|-----:|--------------:|",
    ]
    for row in sorted(bt.get("by_hold_days") or [], key=lambda x: int(x.get("hold_days") or 0)):
        lines.append(
            f"| {row.get('hold_days')} | {row.get('n', 0)} | {row.get('mean_ret_net_pct', '—')} | "
            f"**{row.get('mean_daily_ret_net_pct', '—')}** | {row.get('mean_daily_excess_pct', '—')} | "
            f"{row.get('win_rate_pct', '—')} | {row.get('median_ret_3d_net_pct', '—')} |"
        )
    lines.extend(
        [
            "",
            f"- **Sweet spot (mean daily ret)**: hold **{v.get('recommend_hold_days')}d**",
            "",
            "## Verdict",
            "",
        ]
    )
    for r in v.get("reasons") or []:
        lines.append(f"- {r}")
    lines.extend(["", "模組：`scripts/run_abc_v3_hold_sweep.py`"])
    return "\n".join(lines)


def profile_entry_timing(trades: list[dict[str, Any]]) -> dict[str, Any]:
    """Intraday entry poll distribution · win vs loss."""
    wins = [t for t in trades if t.get("outcome") == "win"]
    losses = [t for t in trades if t.get("outcome") == "loss"]
    n = len(trades)
    by_minute: list[dict[str, Any]] = []
    for minute in INTRADAY_POLL_MINUTES:
        w_n = sum(1 for t in wins if t.get("entry_minute") == minute)
        l_n = sum(1 for t in losses if t.get("entry_minute") == minute)
        t_n = w_n + l_n
        by_minute.append(
            {
                "minute": minute,
                "poll_idx": INTRADAY_POLL_MINUTES.index(minute),
                "n": t_n,
                "pct_all": round(100.0 * t_n / n, 1) if n else 0,
                "win_n": w_n,
                "loss_n": l_n,
                "win_rate_pct": round(100.0 * w_n / t_n, 1) if t_n else None,
            }
        )
    uniform_pct = round(100.0 / len(INTRADAY_POLL_MINUTES), 1)
    win_idx = [int(t["entry_poll_idx"]) for t in wins if t.get("entry_poll_idx") is not None]
    loss_idx = [int(t["entry_poll_idx"]) for t in losses if t.get("entry_poll_idx") is not None]
    open_n = sum(1 for t in trades if t.get("entry_session") == "open")
    mid_n = sum(1 for t in trades if t.get("entry_session") == "mid")
    close_n = sum(1 for t in trades if t.get("entry_session") == "close")
    return {
        "intraday": True,
        "poll_interval_min": 30,
        "poll_window": "09:00–13:30",
        "polls_per_day": len(INTRADAY_POLL_MINUTES),
        "rule": "每檔每天第一個符合條件的 poll 進場（非收盤單）",
        "uniform_pct_per_poll": uniform_pct,
        "is_uniform": False,
        "mean_poll_idx_win": _mean([float(x) for x in win_idx]),
        "mean_poll_idx_loss": _mean([float(x) for x in loss_idx]),
        "by_minute": by_minute,
        "session_pct": {
            "open": round(100.0 * open_n / n, 1) if n else 0,
            "mid": round(100.0 * mid_n / n, 1) if n else 0,
            "close": round(100.0 * close_n / n, 1) if n else 0,
        },
        "session_win_rate": {
            sess: round(
                100.0
                * sum(1 for t in trades if t.get("entry_session") == sess and t.get("outcome") == "win")
                / max(sum(1 for t in trades if t.get("entry_session") == sess), 1),
                1,
            )
            for sess in ("open", "mid", "close")
        },
    }


def _abc_v2_factor_specs() -> list[tuple[str, str, Callable[[dict[str, Any]], bool]]]:
    """Extra filters on top of ABC v2 for winner/loser scan."""
    def _rv(field: str, op: str, thr: float) -> Callable[[dict[str, Any]], bool]:
        def _fn(r: dict[str, Any]) -> bool:
            v = r.get(field)
            if v is None:
                return False
            return float(v) >= thr if op == "gte" else float(v) <= thr if op == "lte" else False

        return _fn

    return [
        ("w20_w3_spread_lte_5", "W20−W3 RV≤5", _rv("w20_w3_rv_spread", "lte", 5.0)),
        ("w20_w3_spread_lte_5.5", "W20−W3 RV≤5.5", _rv("w20_w3_rv_spread", "lte", 5.5)),
        ("w20_rv_lte_104", "W20 RV≤104", _rv("w20_rv", "lte", 104.0)),
        ("w20_rv_lte_105", "W20 RV≤105", _rv("w20_rv", "lte", 105.0)),
        ("w5_rv_lte_100.5", "W5 RV≤100.5", _rv("w5_rv", "lte", 100.5)),
        ("w5_rv_lte_100.8", "W5 RV≤100.8", _rv("w5_rv", "lte", 100.8)),
        ("w3_rv_shallow", "W3 RV≥99.5", lambda r: bool(r.get("w3_rv_shallow"))),
        ("spread_dist_lte_5.5", "spread_dist≤5.5", _rv("spread_dist", "lte", 5.5)),
        ("spread_rs_lte_neg4", "spread_rs≤−4", _rv("spread_rs", "lte", -4.0)),
        ("entry_open", "進場 ≤10:00", lambda r: r.get("entry_session") == "open"),
        ("entry_mid", "進場 mid", lambda r: r.get("entry_session") == "mid"),
        ("entry_close", "進場 ≥13:00", lambda r: r.get("entry_session") == "close"),
        ("entry_poll_lte_3", "poll idx≤3 (≤10:30)", lambda r: int(r.get("entry_poll_idx") or 99) <= 3),
        ("entry_poll_gte_6", "poll idx≥6 (≥12:00)", lambda r: int(r.get("entry_poll_idx") or -1) >= 6),
        ("sig_lead_pullback", "lead_pullback signal", lambda r: r.get("trade_signal") == "lead_pullback_buy"),
    ]


def scan_abc_v2_discriminators(
    trades: list[dict[str, Any]],
    *,
    min_pos: int = 25,
    min_neg: int = 25,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for fid, label, pred in _abc_v2_factor_specs():
        pos = [t for t in trades if pred(t)]
        neg = [t for t in trades if not pred(t)]
        if len(pos) < min_pos or len(neg) < min_neg:
            continue
        pos_rets = [float(t["net_pct"]) for t in pos]
        neg_rets = [float(t["net_pct"]) for t in neg]
        mp, mn = _mean(pos_rets), _mean(neg_rets)
        wp = round(100.0 * sum(1 for x in pos_rets if x > 0) / len(pos_rets), 1)
        rows.append(
            {
                "id": fid,
                "label": label,
                "n_pos": len(pos),
                "mean_ret_pos": mp,
                "win_rate_pos": wp,
                "lift": round((mp or 0) - (mn or 0), 3),
            }
        )
    rows.sort(key=lambda x: float(x.get("lift") or 0), reverse=True)
    return rows


def backtest_abc_v2_refined_filters(trades: list[dict[str, Any]]) -> dict[str, Any]:
    specs = [("abc_v2_baseline", "ABC v2 baseline", lambda t: True)] + list(_abc_v2_factor_specs())
    summaries: dict[str, Any] = {}
    for fid, _label, pred in specs:
        subset = [t for t in trades if pred(t)]
        summaries[fid] = _summarize_trades(subset)
    return {"summaries": summaries}


def run_abc_v2_winner_profile_study(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2025-01-01",
    date_end: str | None = None,
    intraday_tail_days: int = 90,
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
    trades = collect_abc_v2_featured_trades(conn, dates=sample_dates, etf_codes=etf_codes)
    profile = profile_winners_losers(trades)
    timing = profile_entry_timing(trades)
    profile["entry_timing"] = timing
    profile["abc_v2_discriminators"] = scan_abc_v2_discriminators(trades, min_pos=min_n, min_neg=min_n)
    refined = backtest_abc_v2_refined_filters(trades)
    base = refined["summaries"].get("abc_v2_baseline") or {}
    ranked = sorted(
        [
            (fid, row)
            for fid, row in refined["summaries"].items()
            if fid != "abc_v2_baseline" and int(row.get("n") or 0) >= min_n
        ],
        key=lambda x: float(x[1].get("mean_ret_3d_net_pct") or 0),
        reverse=True,
    )
    best_id, best = ranked[0] if ranked else ("", {})
    base_ret = float(base.get("mean_ret_3d_net_pct") or 0)
    verdict = {
        "recommend": best_id if best and float(best.get("mean_ret_3d_net_pct") or 0) > base_ret + 0.05 else "abc_v2_baseline",
        "baseline": base,
        "best_refined": {"id": best_id, **best} if best else {},
        "summary": (
            f"ABC v2 profile n={profile.get('n')} · timing mean poll idx "
            f"win={timing.get('mean_poll_idx_win')} loss={timing.get('mean_poll_idx_loss')} · "
            f"best refined **{best_id}**"
        ),
    }
    return {
        "schema": "abc_v2_winner_profile-v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "date_start": date_start,
        "date_end": end,
        "intraday_sample_dates": sample_dates,
        "intraday_tail_days": intraday_tail_days,
        "profile": profile,
        "refined_backtest": refined,
        "verdict": verdict,
    }


def render_abc_v2_winner_profile_md(payload: dict[str, Any]) -> str:
    prof = payload.get("profile") or {}
    timing = prof.get("entry_timing") or {}
    v = payload.get("verdict") or {}
    refined = payload.get("refined_backtest") or {}
    rs = refined.get("summaries") or {}
    lines = [
        "# ABC v2 · winner/loser profile + 進場時段",
        "",
        f"> {v.get('summary', '')}",
        "",
        "## 是否盤中交易？",
        "",
        "- **是**：每 **30 分鐘** poll 一次（09:00–13:30，共 10 格）",
        "- 條件成立當下用 **intraday 價** 算 RRG · 該 poll 視為進場",
        "- 每檔股票 **每天最多 1 筆**（當日第一個符合的 poll）",
        "- 出場：持有 **3 個交易日** 至第 3 日 **13:30**",
        "",
        "## 進場時段是否平均？",
        "",
        f"- 若完全平均：每 poll 約 **{timing.get('uniform_pct_per_poll')}%**",
        f"- mean poll index：贏家 **{timing.get('mean_poll_idx_win')}** · 輸家 **{timing.get('mean_poll_idx_loss')}** "
        f"(0=09:00 … 9=13:30)",
        "",
        "### 各 poll 分布",
        "",
        "| 時間 | poll# | n | 占全部% | win% |",
        "|------|------:|--:|--------:|-----:|",
    ]
    for row in timing.get("by_minute") or []:
        lines.append(
            f"| {row.get('minute')} | {row.get('poll_idx')} | {row.get('n')} | "
            f"{row.get('pct_all')} | {row.get('win_rate_pct', '—')} |"
        )
    sp = timing.get("session_pct") or {}
    sw = timing.get("session_win_rate") or {}
    lines.extend(
        [
            "",
            f"- Session：open **{sp.get('open')}%** (win {sw.get('open')}%) · "
            f"mid **{sp.get('mid')}%** (win {sw.get('mid')}%) · "
            f"close **{sp.get('close')}%** (win {sw.get('close')}%)",
            "",
            "## 贏家 vs 輸家 · 數值特徵",
            "",
            "| 特徵 | mean 贏 | mean 輸 | lift |",
            "|------|--------:|--------:|-----:|",
        ]
    )
    for row in (prof.get("numeric_lifts") or [])[:20]:
        lines.append(
            f"| {row.get('field')} | {row.get('mean_win')} | {row.get('mean_loss')} | {row.get('lift')} |"
        )
    lines.extend(
        [
            "",
            "## W20×W5 組合 %",
            "",
            "| 組合 | 贏家% | 輸家% | Δpp |",
            "|------|------:|------:|----:|",
        ]
    )
    for row in (prof.get("w20_w5_combo") or {}).get("rows") or []:
        lines.append(
            f"| {row.get('combo')} | {row.get('win_pct')} | {row.get('loss_pct')} | {row.get('delta_pp')} |"
        )
    lines.extend(
        [
            "",
            "## ABC v2 再優化因子",
            "",
            "| 因子 | n+ | mean ret% | win% | lift |",
            "|------|---:|----------:|-----:|-----:|",
        ]
    )
    for row in prof.get("abc_v2_discriminators") or []:
        lines.append(
            f"| {row.get('label')} | {row.get('n_pos')} | {row.get('mean_ret_pos')} | "
            f"{row.get('win_rate_pos')} | {row.get('lift')} |"
        )
    lines.extend(
        [
            "",
            "## 精煉濾網回測",
            "",
            "| Filter | n | mean ret% | win% | median% |",
            "|--------|--:|----------:|-----:|--------:|",
        ]
    )
    label_map = {"abc_v2_baseline": "ABC v2 baseline"}
    for fid, lbl, _ in _abc_v2_factor_specs():
        label_map[fid] = lbl
    for fid in ["abc_v2_baseline"] + [f for f, _, _ in _abc_v2_factor_specs()]:
        row = rs.get(fid) or {}
        if not row.get("n"):
            continue
        lines.append(
            f"| {label_map.get(fid, fid)} | {row.get('n', 0)} | {row.get('mean_ret_3d_net_pct', '—')} | "
            f"{row.get('win_rate_pct', '—')} | {row.get('median_ret_3d_net_pct', '—')} |"
        )
    lines.extend(["", "## Verdict", "", f"- **Recommend**: {v.get('recommend')}", ""])
    best = v.get("best_refined") or {}
    if best:
        lines.append(
            f"- Best refined: {best.get('id')} n={best.get('n')} ret={best.get('mean_ret_3d_net_pct')}%"
        )
    lines.extend(["", "模組：`scripts/run_abc_v2_winner_profile.py`"])
    return "\n".join(lines)
