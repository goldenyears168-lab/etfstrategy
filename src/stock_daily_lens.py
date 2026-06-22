"""Cross-layer stock_daily_lens builder · delta · signal_convergence · narrative."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from copytrade_l1h9_daily import signals_for_date
from holdings_research import (
    ConsensusStock,
    build_cross_etf_consensus,
    fmt_ntd_short,
    resolve_aligned_change_window,
)
from market_benchmark import latest_trading_date, list_trading_dates, previous_trading_date
from market_breadth_ma import build_breadth_panel
from project_config import ETF_CODES_LISTED
from research.backtest.chunge_funnel_backtest import (
    MINERVINI_NEAR_PIVOT_STATES,
    VCP_PIVOT_GATE,
)
from stock_db import (
    delete_stock_daily_lens_for_date,
    load_rrg_universe_scores,
    load_stock_daily_lens_for_date,
    load_stock_daily_lens_row,
    load_vcp_screen_v2_for_date,
    normalize_stock_name,
    upsert_stock_daily_lens_rows,
)
from vcp_funnel_screen import FUNNEL_MODEL_IDS

_TPE = ZoneInfo("Asia/Taipei")
_VCP_MIN_SCORE = float(VCP_PIVOT_GATE["min_composite"])
_VCP_STATES = tuple(VCP_PIVOT_GATE["execution_states"])
_CONVERGENCE_FIRE = 3


@dataclass
class LensContext:
    trade_date: str
    prev_trade_date: str | None
    holdings_aligned: bool
    breadth_zone_200: str | None
    trend_posture: str | None
    breadth_delta_5d: float | None
    data_baseline_date: str
    vcp_as_of_date: str | None


@dataclass
class LensStockFacts:
    consensus: ConsensusStock | None = None


@dataclass
class LensOverlay:
    rrg_quadrant: str | None = None
    rrg_mono_fresh: bool = False
    rrg_tier2: bool = False
    vcp_composite: float | None = None
    vcp_execution_state: str | None = None
    vcp_distance_pivot_pct: float | None = None
    copytrade_l1h9_signal: bool = False
    regime_aligned: bool = False
    stock_return_20d: float | None = None


@dataclass
class LensRow:
    trade_date: str
    stock_id: str
    stock_name: str = ""
    etf_add_count: int = 0
    etf_reduce_count: int = 0
    etf_add_codes: list[str] = field(default_factory=list)
    etf_flow_ntd: float | None = None
    share_delta_total: float = 0.0
    growth_pct: float | None = None
    consensus_add: bool = False
    consensus_streak_days: int = 0
    breadth_zone_200: str | None = None
    trend_posture: str | None = None
    regime_aligned: bool = False
    rrg_quadrant: str | None = None
    rrg_quadrant_prev: str | None = None
    rrg_mono_fresh: bool = False
    rrg_tier2: bool = False
    vcp_composite: float | None = None
    vcp_execution_state: str | None = None
    vcp_distance_pivot_pct: float | None = None
    copytrade_l1h9_signal: bool = False
    delta_new_to_watchlist: bool = False
    delta_rrg_quadrant_change: str | None = None
    delta_consensus_new_today: bool = False
    delta_score_change: float | None = None
    delta_any_signal: bool = False
    signal_convergence: int = 0
    lens_score: float = 0.0
    narrative_zh: str = ""
    highlight_tier: str = "none"
    holdings_aligned: bool = True
    data_baseline_date: str = ""
    sources_json: dict[str, Any] = field(default_factory=dict)

    def to_db_dict(self) -> dict[str, Any]:
        return {
            "trade_date": self.trade_date,
            "stock_id": self.stock_id,
            "stock_name": self.stock_name,
            "etf_add_count": self.etf_add_count,
            "etf_reduce_count": self.etf_reduce_count,
            "etf_add_codes": self.etf_add_codes,
            "etf_flow_ntd": self.etf_flow_ntd,
            "share_delta_total": self.share_delta_total,
            "growth_pct": self.growth_pct,
            "consensus_add": self.consensus_add,
            "consensus_streak_days": self.consensus_streak_days,
            "breadth_zone_200": self.breadth_zone_200,
            "trend_posture": self.trend_posture,
            "regime_aligned": self.regime_aligned,
            "rrg_quadrant": self.rrg_quadrant,
            "rrg_quadrant_prev": self.rrg_quadrant_prev,
            "rrg_mono_fresh": self.rrg_mono_fresh,
            "rrg_tier2": self.rrg_tier2,
            "vcp_composite": self.vcp_composite,
            "vcp_execution_state": self.vcp_execution_state,
            "vcp_distance_pivot_pct": self.vcp_distance_pivot_pct,
            "copytrade_l1h9_signal": self.copytrade_l1h9_signal,
            "delta_new_to_watchlist": self.delta_new_to_watchlist,
            "delta_rrg_quadrant_change": self.delta_rrg_quadrant_change,
            "delta_consensus_new_today": self.delta_consensus_new_today,
            "delta_score_change": self.delta_score_change,
            "delta_any_signal": self.delta_any_signal,
            "signal_convergence": self.signal_convergence,
            "lens_score": self.lens_score,
            "narrative_zh": self.narrative_zh,
            "highlight_tier": self.highlight_tier,
            "holdings_aligned": self.holdings_aligned,
            "data_baseline_date": self.data_baseline_date,
            "sources_json": self.sources_json,
        }


def _regime_axes(conn: sqlite3.Connection, trade_date: str) -> tuple[str | None, str | None]:
    try:
        from regime_snapshot_json import build_regime_snapshot_json

        payload = build_regime_snapshot_json(conn, trade_date)
        axes = payload.get("axes") or {}
        b = axes.get("breadth_zone_200") or {}
        t = axes.get("trend_posture") or {}
        zone = b.get("zone") or b.get("zone_id")
        posture = t.get("posture") or t.get("posture_id")
        return (str(zone) if zone else None, str(posture) if posture else None)
    except Exception:
        return None, None


def _breadth_delta_5d(conn: sqlite3.Connection, trade_date: str) -> float | None:
    try:
        panel = build_breadth_panel(conn, date_end=trade_date)
    except RuntimeError:
        return None
    if panel.empty or "pct_above_200" not in panel.columns:
        return None
    sub = panel[panel["trade_date"] <= trade_date].tail(6)
    if len(sub) < 2:
        return None
    return float(sub["pct_above_200"].iloc[-1] - sub["pct_above_200"].iloc[0])


def _resolve_vcp_as_of(conn: sqlite3.Connection, trade_date: str) -> str | None:
    row = conn.execute(
        """
        SELECT MAX(as_of_date) AS d FROM vcp_screen_scores_v2
        WHERE as_of_date <= ? AND model_id IN ({})
        """.format(",".join("?" * len(FUNNEL_MODEL_IDS))),
        (trade_date, *FUNNEL_MODEL_IDS),
    ).fetchone()
    if row and row["d"]:
        return str(row["d"])
    return None


def _stock_close_on(conn: sqlite3.Connection, stock_id: str, day: str) -> float | None:
    row = conn.execute(
        """
        SELECT close FROM daily_bars
        WHERE code = ? AND date = ?
        ORDER BY CASE source WHEN 'tej' THEN 0 WHEN 'yahoo' THEN 1 ELSE 2 END
        LIMIT 1
        """,
        (stock_id, day),
    ).fetchone()
    if row and row["close"] is not None:
        return float(row["close"])
    return None


def _stock_return_20d(
    conn: sqlite3.Connection,
    stock_id: str,
    as_of: str,
) -> float | None:
    dates = list_trading_dates(conn, end=as_of, limit=21)
    if len(dates) < 21:
        return None
    start = dates[0]
    c0 = _stock_close_on(conn, stock_id, start)
    c1 = _stock_close_on(conn, stock_id, as_of)
    if c0 is None or c1 is None or c0 <= 0:
        return None
    return (c1 / c0 - 1.0) * 100.0


def _regime_aligned_for_stock(
    stock_return_20d: float | None,
    breadth_delta_5d: float | None,
) -> bool:
    if stock_return_20d is None or breadth_delta_5d is None:
        return False
    if stock_return_20d == 0 or breadth_delta_5d == 0:
        return False
    return (stock_return_20d > 0) == (breadth_delta_5d > 0)


def _load_context(conn: sqlite3.Connection, trade_date: str) -> LensContext:
    prev = previous_trading_date(conn, trade_date)
    holdings_aligned = resolve_aligned_change_window(conn, ETF_CODES_LISTED) is not None
    breadth_zone, trend_posture = _regime_axes(conn, trade_date)
    breadth_delta = _breadth_delta_5d(conn, trade_date)
    vcp_as_of = _resolve_vcp_as_of(conn, trade_date)

    rrg_rows = load_rrg_universe_scores(conn, trade_date, "close")
    data_baseline = trade_date
    if rrg_rows:
        data_baseline = str(rrg_rows[0]["data_baseline_date"] or trade_date)
    elif vcp_as_of:
        data_baseline = vcp_as_of

    return LensContext(
        trade_date=trade_date,
        prev_trade_date=prev,
        holdings_aligned=holdings_aligned,
        breadth_zone_200=breadth_zone,
        trend_posture=trend_posture,
        breadth_delta_5d=breadth_delta,
        data_baseline_date=data_baseline,
        vcp_as_of_date=vcp_as_of,
    )


def _consensus_map(
    conn: sqlite3.Connection,
    etf_codes: tuple[str, ...],
) -> dict[str, ConsensusStock]:
    return {row.stock_id: row for row in build_cross_etf_consensus(conn, etf_codes)}


def _rrg_map(
    conn: sqlite3.Connection,
    trade_date: str,
) -> dict[str, sqlite3.Row]:
    rows = load_rrg_universe_scores(conn, trade_date, "close")
    return {str(r["stock_id"]): r for r in rows}


def _vcp_map(
    conn: sqlite3.Connection,
    as_of_date: str | None,
) -> dict[str, sqlite3.Row]:
    if not as_of_date:
        return {}
    out: dict[str, sqlite3.Row] = {}
    for model_id in FUNNEL_MODEL_IDS:
        for row in load_vcp_screen_v2_for_date(
            conn,
            as_of_date,
            model_id=model_id,
            min_score=_VCP_MIN_SCORE,
            execution_states=_VCP_STATES,
        ):
            sid = str(row["stock_id"])
            prev = out.get(sid)
            if prev is None or float(row["composite_score"]) > float(prev["composite_score"]):
                out[sid] = row
    return out


def _l1h9_signal_ids(conn: sqlite3.Connection, trade_date: str) -> set[str]:
    _, _, signals = signals_for_date(conn, trade_date)
    return {sig.stock_id for sig in signals}


def _union_pool(
    consensus: dict[str, ConsensusStock],
    rrg: dict[str, sqlite3.Row],
    vcp: dict[str, sqlite3.Row],
) -> set[str]:
    pool: set[str] = set()
    for sid, row in consensus.items():
        if row.etf_add > 0 or row.etf_reduce > 0:
            pool.add(sid)
    for sid, row in rrg.items():
        if int(row["mono_fresh"] or 0):
            pool.add(sid)
    pool.update(vcp.keys())
    return pool


def _compute_convergence(row: LensRow) -> int:
    score = 0
    if row.consensus_add:
        score += 1
    if row.regime_aligned:
        score += 1
    if (row.rrg_quadrant or "").lower() == "leading" and row.rrg_mono_fresh:
        score += 1
    if (
        row.vcp_composite is not None
        and row.vcp_composite >= _VCP_MIN_SCORE
        and row.vcp_execution_state in _VCP_STATES
    ):
        score += 1
    return score


def _compute_lens_score(row: LensRow) -> float:
    score = 0.0
    score += row.etf_add_count * 10.0
    if row.consensus_add:
        score += 25.0
    if row.etf_flow_ntd:
        score += min(abs(row.etf_flow_ntd) / 1e8, 20.0)
    if row.vcp_composite:
        score += row.vcp_composite * 0.2
    if row.rrg_mono_fresh:
        score += 15.0
    if (row.rrg_quadrant or "").lower() == "leading":
        score += 10.0
    if row.consensus_streak_days >= 8:
        score *= 0.5
    return round(score, 2)


def build_narrative_zh(row: LensRow) -> str:
    prefixes: list[str] = []
    if row.delta_new_to_watchlist:
        prefixes.append("【新進觀察】")
    if row.delta_consensus_new_today:
        prefixes.append("【今日首次共識】")
    if row.delta_rrg_quadrant_change:
        prefixes.append(f"【RRG {row.delta_rrg_quadrant_change}】")
    if row.consensus_streak_days >= 8 and row.consensus_add:
        prefixes.append(f"【已知事實·連續第{row.consensus_streak_days}日共識】")

    head = "".join(prefixes)
    body = f"{row.stock_id} {row.stock_name}：".strip()

    parts: list[str] = []
    if row.consensus_add and row.etf_add_codes:
        codes = "+".join(row.etf_add_codes)
        flow = fmt_ntd_short(row.etf_flow_ntd)
        seg = f"共識加碼（{codes}）"
        if flow:
            seg += f" {flow}"
        parts.append(seg)
    elif row.etf_add_count == 1 and row.etf_add_codes:
        parts.append(f"單檔加碼（{row.etf_add_codes[0]}）")
    elif row.etf_reduce_count > 0:
        parts.append("ETF 減碼異動")

    if row.rrg_quadrant:
        seg = f"RRG {row.rrg_quadrant}"
        if row.rrg_mono_fresh:
            seg += " fresh"
        parts.append(seg)

    if row.vcp_composite is not None:
        seg = f"VCP 綜合分 {row.vcp_composite:.0f}"
        if row.vcp_distance_pivot_pct is not None:
            seg += f" 距樞紐 {row.vcp_distance_pivot_pct:.1f}%"
        parts.append(seg)

    if row.regime_aligned:
        parts.append("體制同向")
    elif row.breadth_zone_200 and row.stock_id:
        parts.append("體制背離")

    text = head + body + " ".join(parts)
    if row.signal_convergence >= _CONVERGENCE_FIRE:
        text += f" · 四框架收斂 {row.signal_convergence}/4"
    return text.strip()


def _apply_facts(row: LensRow, stock: ConsensusStock | None) -> None:
    if stock is None:
        return
    row.stock_name = normalize_stock_name(stock.stock_name or row.stock_name)
    row.etf_add_count = stock.etf_add
    row.etf_reduce_count = stock.etf_reduce
    row.etf_add_codes = list(stock.etf_add_list)
    row.etf_flow_ntd = stock.flow_ntd
    row.share_delta_total = stock.share_delta_total
    row.growth_pct = stock.growth_pct
    row.consensus_add = stock.etf_add >= 2


def _apply_overlay(
    row: LensRow,
    ctx: LensContext,
    rrg: sqlite3.Row | None,
    vcp: sqlite3.Row | None,
    l1h9: bool,
    stock_return: float | None,
) -> None:
    row.breadth_zone_200 = ctx.breadth_zone_200
    row.trend_posture = ctx.trend_posture
    row.holdings_aligned = ctx.holdings_aligned
    row.data_baseline_date = ctx.data_baseline_date
    row.regime_aligned = _regime_aligned_for_stock(stock_return, ctx.breadth_delta_5d)
    row.copytrade_l1h9_signal = l1h9

    if rrg is not None:
        row.rrg_quadrant = str(rrg["quadrant"]) if rrg["quadrant"] else None
        row.rrg_mono_fresh = bool(int(rrg["mono_fresh"] or 0))
        row.rrg_tier2 = bool(int(rrg["tier2"] or 0))
        if not row.stock_name and rrg["stock_name"]:
            row.stock_name = normalize_stock_name(str(rrg["stock_name"]))

    if vcp is not None:
        row.vcp_composite = float(vcp["composite_score"])
        row.vcp_execution_state = str(vcp["execution_state"])
        dist = vcp["distance_from_pivot_pct"]
        row.vcp_distance_pivot_pct = float(dist) if dist is not None else None
        if not row.stock_name and vcp["stock_name"]:
            row.stock_name = normalize_stock_name(str(vcp["stock_name"]))


def _apply_deltas(
    row: LensRow,
    prev_row: sqlite3.Row | None,
    in_prev_pool: bool,
) -> None:
    row.delta_new_to_watchlist = not in_prev_pool
    if prev_row is not None:
        prev_q = prev_row["rrg_quadrant"]
        if prev_q and row.rrg_quadrant and str(prev_q) != str(row.rrg_quadrant):
            row.delta_rrg_quadrant_change = f"{prev_q}→{row.rrg_quadrant}"
            row.rrg_quadrant_prev = str(prev_q)
        if row.consensus_add:
            prev_streak = int(prev_row["consensus_streak_days"] or 0)
            if int(prev_row["consensus_add"] or 0):
                row.consensus_streak_days = prev_streak + 1
            else:
                row.consensus_streak_days = 1
        row.delta_score_change = round(
            row.lens_score - float(prev_row["lens_score"] or 0),
            2,
        )
    elif row.consensus_add:
        row.consensus_streak_days = 1

    row.delta_consensus_new_today = row.consensus_add and row.consensus_streak_days == 1

    row.delta_any_signal = bool(
        row.delta_new_to_watchlist
        or row.delta_consensus_new_today
        or row.delta_rrg_quadrant_change
    )


def build_stock_daily_lens_rows(
    conn: sqlite3.Connection,
    trade_date: str,
    *,
    etf_codes: tuple[str, ...] = ETF_CODES_LISTED,
) -> list[LensRow]:
    ctx = _load_context(conn, trade_date)
    consensus = _consensus_map(conn, etf_codes)
    rrg = _rrg_map(conn, trade_date)
    vcp = _vcp_map(conn, ctx.vcp_as_of_date)
    l1h9_ids = _l1h9_signal_ids(conn, trade_date)
    pool = _union_pool(consensus, rrg, vcp)

    prev_pool: set[str] = set()
    prev_by_stock: dict[str, sqlite3.Row] = {}
    if ctx.prev_trade_date:
        for prow in load_stock_daily_lens_for_date(conn, ctx.prev_trade_date):
            sid = str(prow["stock_id"])
            prev_pool.add(sid)
            prev_by_stock[sid] = prow

    rows: list[LensRow] = []
    for stock_id in sorted(pool):
        row = LensRow(trade_date=trade_date, stock_id=stock_id)
        _apply_facts(row, consensus.get(stock_id))
        stock_ret = _stock_return_20d(conn, stock_id, ctx.data_baseline_date)
        _apply_overlay(
            row,
            ctx,
            rrg.get(stock_id),
            vcp.get(stock_id),
            stock_id in l1h9_ids,
            stock_ret,
        )
        row.signal_convergence = _compute_convergence(row)
        row.lens_score = _compute_lens_score(row)
        _apply_deltas(row, prev_by_stock.get(stock_id), stock_id in prev_pool)
        row.signal_convergence = _compute_convergence(row)
        row.lens_score = _compute_lens_score(row)
        if row.signal_convergence >= _CONVERGENCE_FIRE:
            row.highlight_tier = "fire"
        elif row.signal_convergence >= 2 or row.delta_any_signal:
            row.highlight_tier = "watch"
        row.narrative_zh = build_narrative_zh(row)
        row.sources_json = {
            "facts": "holdings_research.build_cross_etf_consensus",
            "regime": {"trade_date": trade_date},
            "rrg": {
                "table": "rrg_universe_scores",
                "screen_kind": "close",
                "session_date": trade_date,
            },
            "vcp": {
                "table": "vcp_screen_scores_v2",
                "as_of_date": ctx.vcp_as_of_date,
            },
            "delta_prev_trade_date": ctx.prev_trade_date,
        }
        rows.append(row)

    rows.sort(
        key=lambda r: (
            int(r.delta_any_signal),
            r.signal_convergence,
            r.delta_score_change or 0.0,
            r.lens_score,
        ),
        reverse=True,
    )
    return rows


def persist_stock_daily_lens(
    conn: sqlite3.Connection,
    trade_date: str,
    *,
    etf_codes: tuple[str, ...] = ETF_CODES_LISTED,
) -> int:
    rows = build_stock_daily_lens_rows(conn, trade_date, etf_codes=etf_codes)
    delete_stock_daily_lens_for_date(conn, trade_date)
    if not rows:
        return 0
    return upsert_stock_daily_lens_rows(conn, [r.to_db_dict() for r in rows])


def resolve_lens_trade_date(
    conn: sqlite3.Connection,
    as_of: str | None = None,
) -> str | None:
    if as_of:
        return as_of
    return latest_trading_date(conn)
