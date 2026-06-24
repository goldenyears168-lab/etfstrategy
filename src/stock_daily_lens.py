"""Cross-layer stock_daily_lens builder · delta · signal_convergence · narrative."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from copytrade_l1h9_daily import signals_for_date
from lens_ui_copy import RRG_FRESH_ZH, badge_plain_zh, format_watchlist_count_zh
from holdings_research import (
    ConsensusStock,
    build_cross_etf_consensus,
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
    load_etf_constituent_watchlist,
    load_rrg_universe_scores,
    load_vcp_screen_v2_for_date,
    normalize_stock_name,
    upsert_lens_daily_alert,
)
from supabase_lens_sync import load_supabase_highlight_for_date
from vcp_funnel_screen import FUNNEL_MODEL_IDS

_TPE = ZoneInfo("Asia/Taipei")
_VCP_MIN_SCORE = float(VCP_PIVOT_GATE["min_composite"])
_VCP_STATES = tuple(VCP_PIVOT_GATE["execution_states"])
_CONVERGENCE_FIRE = 3
_RRG_QUADRANT_ZH = {
    "leading": "領先",
    "improving": "轉強",
    "weakening": "轉弱",
    "lagging": "落後",
}


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
    etf_add_codes: list[str] = field(default_factory=list)
    consensus_add: bool = False
    consensus_streak_days: int = 0
    breadth_zone_200: str | None = None
    trend_posture: str | None = None
    regime_aligned: bool = False
    rrg_quadrant: str | None = None
    rrg_quadrant_prev: str | None = None
    rrg_mono_fresh: bool = False
    rrg_tier2: bool = False
    rrg_rs_ratio: float | None = None
    rrg_rs_momentum: float | None = None
    rrg_rank: int | None = None
    rrg_total: int | None = None
    vcp_composite: float | None = None
    vcp_execution_state: str | None = None
    vcp_distance_pivot_pct: float | None = None
    copytrade_l1h9_signal: bool = False
    delta_new_to_watchlist: bool = False
    delta_rrg_quadrant_change: str | None = None
    delta_consensus_new_today: bool = False
    delta_any_signal: bool = False
    signal_convergence: int = 0
    lens_score: float = 0.0
    narrative_zh: str = ""
    highlight_tier: str = "none"
    featured_rank: int | None = None
    home_preview_rank: int | None = None
    strategy_group_rank: int | None = None
    badges_json: list[dict[str, str]] = field(default_factory=list)
    holdings_aligned: bool = True
    data_baseline_date: str = ""
    sources_json: dict[str, Any] = field(default_factory=dict)

    def to_db_dict(self) -> dict[str, Any]:
        return {
            "trade_date": self.trade_date,
            "stock_id": self.stock_id,
            "stock_name": self.stock_name,
            "etf_add_codes": self.etf_add_codes,
            "consensus_add": self.consensus_add,
            "consensus_streak_days": self.consensus_streak_days,
            "breadth_zone_200": self.breadth_zone_200,
            "trend_posture": self.trend_posture,
            "regime_aligned": self.regime_aligned,
            "rrg_quadrant": self.rrg_quadrant,
            "rrg_quadrant_prev": self.rrg_quadrant_prev,
            "rrg_mono_fresh": self.rrg_mono_fresh,
            "rrg_tier2": self.rrg_tier2,
            "rrg_rs_ratio": self.rrg_rs_ratio,
            "rrg_rs_momentum": self.rrg_rs_momentum,
            "rrg_rank": self.rrg_rank,
            "rrg_total": self.rrg_total,
            "vcp_composite": self.vcp_composite,
            "vcp_execution_state": self.vcp_execution_state,
            "vcp_distance_pivot_pct": self.vcp_distance_pivot_pct,
            "copytrade_l1h9_signal": self.copytrade_l1h9_signal,
            "delta_new_to_watchlist": self.delta_new_to_watchlist,
            "delta_rrg_quadrant_change": self.delta_rrg_quadrant_change,
            "delta_consensus_new_today": self.delta_consensus_new_today,
            "delta_any_signal": self.delta_any_signal,
            "signal_convergence": self.signal_convergence,
            "lens_score": self.lens_score,
            "narrative_zh": self.narrative_zh,
            "highlight_tier": self.highlight_tier,
            "featured_rank": self.featured_rank,
            "home_preview_rank": self.home_preview_rank,
            "strategy_group_rank": self.strategy_group_rank,
            "badges_json": self.badges_json,
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


def _load_context(conn: sqlite3.Connection, trade_date: str, *, light_regime: bool = False) -> LensContext:
    prev = previous_trading_date(conn, trade_date)
    holdings_aligned = resolve_aligned_change_window(conn, ETF_CODES_LISTED) is not None
    if light_regime:
        breadth_zone, trend_posture, breadth_delta = None, None, None
    else:
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


def _compute_rrg_universe_rank(
    rrg_rows: list[sqlite3.Row],
) -> tuple[int, dict[str, dict[str, Any]]]:
    """Full RRG universe rank by rs_ratio DESC · rs_momentum DESC · stock_id ASC."""
    eligible: list[sqlite3.Row] = []
    for row in rrg_rows:
        if row["rs_ratio"] is not None:
            eligible.append(row)
    eligible.sort(
        key=lambda r: (
            -float(r["rs_ratio"]),
            -float(r["rs_momentum"] or 0),
            str(r["stock_id"]),
        )
    )
    total = len(eligible)
    rank_map: dict[str, dict[str, Any]] = {}
    for idx, row in enumerate(eligible):
        sid = str(row["stock_id"])
        mom = row["rs_momentum"]
        rank_map[sid] = {
            "rrg_rs_ratio": float(row["rs_ratio"]),
            "rrg_rs_momentum": float(mom) if mom is not None else None,
            "rrg_rank": idx + 1,
        }
    return total, rank_map


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


def _constituent_name_map(
    conn: sqlite3.Connection,
    etf_codes: tuple[str, ...],
) -> dict[str, str]:
    """監控清單成員（load_etf_constituent_watchlist）→ stock_id → name。"""
    return {
        w["stock_id"]: normalize_stock_name(w.get("stock_name") or "")
        for w in load_etf_constituent_watchlist(conn, etf_codes)
    }


def _monitoring_pool(
    constituents: dict[str, str],
    consensus: dict[str, ConsensusStock],
    rrg: dict[str, sqlite3.Row],
    vcp: dict[str, sqlite3.Row],
) -> set[str]:
    """監控清單成員：loader 聯集為底，再併入當日訊號聯集（防漏）。"""
    pool = set(constituents.keys())
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
    if row.consensus_add:
        score += 25.0
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
    def to_rrg_label(text: str | None) -> str:
        if not text:
            return ""
        out = str(text)
        for key, label in _RRG_QUADRANT_ZH.items():
            out = out.replace(key, label)
            out = out.replace(key.title(), label)
            out = out.replace(key.upper(), label)
        return out

    prefixes: list[str] = []
    if row.delta_new_to_watchlist:
        prefixes.append("【新進觀察】")
    if row.delta_consensus_new_today:
        prefixes.append("【跨 ETF 共識加碼】")
    if row.delta_rrg_quadrant_change:
        prefixes.append(f"【RRG {to_rrg_label(row.delta_rrg_quadrant_change)}】")
    if row.consensus_streak_days >= 8 and row.consensus_add:
        prefixes.append(f"【已知事實·連續第{row.consensus_streak_days}日共識加碼】")

    head = "".join(prefixes)
    body = f"{row.stock_id} {row.stock_name}：".strip()

    parts: list[str] = []
    if row.consensus_add and row.etf_add_codes:
        codes = "+".join(row.etf_add_codes)
        parts.append(f"共識加碼（{codes}）")

    if row.rrg_quadrant:
        seg = f"RRG {to_rrg_label(row.rrg_quadrant)}"
        if row.rrg_mono_fresh:
            seg += f" {RRG_FRESH_ZH}"
        parts.append(seg)

    if row.vcp_composite is not None:
        seg = f"VCP 綜合分 {row.vcp_composite:.0f}"
        if row.vcp_distance_pivot_pct is not None:
            seg += f" 距突破價 {row.vcp_distance_pivot_pct:.1f}%"
        parts.append(seg)

    if row.regime_aligned:
        parts.append("大盤同向")
    elif row.breadth_zone_200 and row.stock_id:
        parts.append("大盤背離")

    text = head + body + " ".join(parts)
    if row.signal_convergence >= _CONVERGENCE_FIRE:
        text += f" · 四框架收斂 {row.signal_convergence}/4"
    return text.strip()


_FEATURED_LIMIT = 10
_HOME_PREVIEW_LIMIT = 6
_VCP_FEATURED_MIN = 60.0


def _is_good_quadrant_change(change: str | None) -> bool:
    if not change:
        return False
    parts = change.split("→")
    if len(parts) < 2:
        return False
    to_q = parts[1].strip().lower()
    return to_q in ("leading", "improving", "領先", "轉強")


def _is_meaningful_row(row: LensRow) -> bool:
    if row.rrg_quadrant == "leading" and row.rrg_mono_fresh:
        return True
    if (
        row.vcp_composite is not None
        and row.vcp_composite >= _VCP_FEATURED_MIN
        and row.vcp_execution_state
    ):
        return True
    if row.copytrade_l1h9_signal:
        return True
    return _is_good_quadrant_change(row.delta_rrg_quadrant_change)


def _is_positive_home_preview(row: LensRow) -> bool:
    change = row.delta_rrg_quadrant_change or ""
    if any(x in change.lower() for x in ("weakening", "lagging")) or any(
        x in change for x in ("轉弱", "落後")
    ):
        return False
    if row.narrative_zh and any(x in row.narrative_zh for x in ("減碼", "出清", "剔除")):
        return False
    return (
        row.delta_new_to_watchlist
        or row.consensus_add
        or row.delta_consensus_new_today
        or row.rrg_mono_fresh
        or row.copytrade_l1h9_signal
        or _is_good_quadrant_change(row.delta_rrg_quadrant_change)
    )


def _strategy_priority(row: LensRow) -> int:
    if row.rrg_mono_fresh:
        return 0
    if row.vcp_execution_state:
        return 1
    if row.copytrade_l1h9_signal:
        return 2
    return 3


def build_badges_json(row: LensRow) -> list[dict[str, str]]:
    badges: list[dict[str, str]] = []

    def _append(key: str, label_zh: str, tone: str) -> None:
        badges.append(
            {
                "key": key,
                "label_zh": label_zh,
                "plain_zh": badge_plain_zh(key, label_zh),
                "tone": tone,
            }
        )

    if row.delta_new_to_watchlist:
        _append("new_observation", "新進觀察", "accent")
    if row.consensus_add:
        _append("consensus_add", "ETF共識加碼", "primary")
    if row.delta_consensus_new_today and not row.consensus_add:
        _append("consensus_delta", "跨 ETF 共識加碼", "accent")
    if row.delta_rrg_quadrant_change:
        label = f"RRG {row.delta_rrg_quadrant_change}"
        _append("rrg_change", label, "accent")
    if row.rrg_mono_fresh:
        _append("rrg_fresh", RRG_FRESH_ZH, "accent")
    if row.copytrade_l1h9_signal:
        _append("copytrade", "跟單訊號", "primary")
    if row.delta_any_signal and not badges:
        _append("signal", "訊號", "accent")
    if row.highlight_tier == "watch":
        _append("watch", "持續關注", "secondary")
    return badges


def _apply_featured_ranks(rows: list[LensRow]) -> None:
    for row in rows:
        row.badges_json = build_badges_json(row)
        row.strategy_group_rank = _strategy_priority(row)

    meaningful = [r for r in rows if _is_meaningful_row(r)]
    meaningful.sort(
        key=lambda r: (
            _strategy_priority(r),
            -float(r.lens_score),
        ),
    )
    for rank, row in enumerate(meaningful[:_FEATURED_LIMIT], start=1):
        row.featured_rank = rank

    positive = [r for r in rows if _is_positive_home_preview(r)]
    positive.sort(
        key=lambda r: (
            int(r.delta_any_signal),
            1 if r.highlight_tier == "fire" else 0,
            r.signal_convergence,
            float(r.lens_score),
        ),
        reverse=True,
    )
    for rank, row in enumerate(positive[:_HOME_PREVIEW_LIMIT], start=1):
        row.home_preview_rank = rank


def _apply_facts(row: LensRow, stock: ConsensusStock | None) -> None:
    if stock is None:
        return
    row.stock_name = normalize_stock_name(stock.stock_name or row.stock_name)
    row.etf_add_codes = list(stock.etf_add_list)
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


def _prev_field(row: dict[str, Any] | sqlite3.Row, key: str, alt_key: str | None = None) -> Any:
    if isinstance(row, dict):
        if row.get(key) is not None:
            return row[key]
        if alt_key and row.get(alt_key) is not None:
            return row[alt_key]
        return None
    try:
        val = row[key]
        if val is not None:
            return val
    except (KeyError, IndexError):
        pass
    if alt_key:
        try:
            return row[alt_key]
        except (KeyError, IndexError):
            pass
    return None


def _apply_deltas(
    row: LensRow,
    prev_row: dict[str, Any] | sqlite3.Row | None,
    in_prev_pool: bool,
) -> None:
    row.delta_new_to_watchlist = not in_prev_pool
    if prev_row is not None:
        prev_q = _prev_field(prev_row, "rrg_quadrant")
        if prev_q and row.rrg_quadrant and str(prev_q) != str(row.rrg_quadrant):
            row.delta_rrg_quadrant_change = f"{prev_q}→{row.rrg_quadrant}"
            row.rrg_quadrant_prev = str(prev_q)
        if row.consensus_add:
            prev_streak = int(_prev_field(prev_row, "consensus_streak_days") or 0)
            if _prev_bool(prev_row, "consensus_add"):
                row.consensus_streak_days = prev_streak + 1
            else:
                row.consensus_streak_days = 1
    elif row.consensus_add:
        row.consensus_streak_days = 1

    row.delta_consensus_new_today = row.consensus_add and row.consensus_streak_days == 1

    row.delta_any_signal = bool(
        row.delta_new_to_watchlist
        or row.delta_consensus_new_today
        or row.delta_rrg_quadrant_change
    )


def _prev_bool(row: dict[str, Any] | sqlite3.Row, key: str) -> bool:
    val = _prev_field(row, key)
    if val is None:
        return False
    if isinstance(val, bool):
        return val
    return bool(int(val or 0))


def build_stock_daily_lens_rows(
    conn: sqlite3.Connection,
    trade_date: str,
    *,
    etf_codes: tuple[str, ...] = ETF_CODES_LISTED,
    prev_highlight_rows: list[dict[str, Any]] | None = None,
    light_regime: bool = False,
) -> list[LensRow]:
    ctx = _load_context(conn, trade_date, light_regime=light_regime)
    constituents = _constituent_name_map(conn, etf_codes)
    consensus = _consensus_map(conn, etf_codes)
    rrg_rows = load_rrg_universe_scores(conn, trade_date, "close")
    rrg = {str(r["stock_id"]): r for r in rrg_rows}
    rrg_total, rrg_rank_map = _compute_rrg_universe_rank(rrg_rows)
    vcp = _vcp_map(conn, ctx.vcp_as_of_date)
    l1h9_ids = _l1h9_signal_ids(conn, trade_date)
    pool = _monitoring_pool(constituents, consensus, rrg, vcp)

    prev_pool: set[str] = set()
    prev_by_stock: dict[str, dict[str, Any]] = {}
    if ctx.prev_trade_date:
        prev_rows = prev_highlight_rows
        if prev_rows is None:
            prev_rows = load_supabase_highlight_for_date(ctx.prev_trade_date)
        for prow in prev_rows:
            sid = str(prow["stock_id"])
            prev_pool.add(sid)
            prev_by_stock[sid] = prow

    rows: list[LensRow] = []
    for stock_id in sorted(pool):
        row = LensRow(trade_date=trade_date, stock_id=stock_id)
        if constituents.get(stock_id):
            row.stock_name = constituents[stock_id]
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
        rank_info = rrg_rank_map.get(stock_id)
        if rank_info:
            row.rrg_rs_ratio = rank_info["rrg_rs_ratio"]
            row.rrg_rs_momentum = rank_info["rrg_rs_momentum"]
            row.rrg_rank = rank_info["rrg_rank"]
        if rrg_total > 0:
            row.rrg_total = rrg_total
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
            "universe": format_watchlist_count_zh(len(constituents)),
            "facts": "ETF 公開持股檔",
            "regime": {"trade_date": trade_date},
            "rrg": {
                "label": f"RRG 收盤掃描 · {trade_date}",
            },
            "vcp": {
                "label": f"VCP 篩選 · 資料日 {ctx.vcp_as_of_date or trade_date}",
            },
            "delta_prev_trade_date": ctx.prev_trade_date,
        }
        rows.append(row)

    rows.sort(
        key=lambda r: (
            int(r.delta_any_signal),
            r.signal_convergence,
            r.lens_score,
        ),
        reverse=True,
    )
    _apply_featured_ranks(rows)
    return rows


def publish_stock_daily_highlight(
    conn: sqlite3.Connection,
    trade_date: str,
    *,
    etf_codes: tuple[str, ...] = ETF_CODES_LISTED,
    prev_highlight_rows: list[dict[str, Any]] | None = None,
) -> tuple[list[LensRow], dict[str, Any]]:
    """Build 全成分監控清單 → alert；不寫本機 SQLite stock_daily_lens。"""
    from lens_alert_digest import build_lens_daily_alert_from_rows

    rows = build_stock_daily_lens_rows(
        conn,
        trade_date,
        etf_codes=etf_codes,
        prev_highlight_rows=prev_highlight_rows,
    )
    alert = build_lens_daily_alert_from_rows(rows, trade_date)
    upsert_lens_daily_alert(conn, alert)
    return rows, alert


def resolve_lens_trade_date(
    conn: sqlite3.Connection,
    as_of: str | None = None,
) -> str | None:
    if as_of:
        return as_of
    return latest_trading_date(conn)
