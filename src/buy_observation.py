"""Buy observation board · multi-pool intraday C0 scale · config/buy_observation.yaml."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import pandas as pd
import sqlite3
import yaml

from project_config import DEFAULT_ETF_CODES
from research.backtest.finpilot_local_backtest import load_price_panels
from research.backtest.rrg_mono_backtest import build_mono_up_fresh_calendar
from research.backtest.rrg_mono_intraday_ab import DEFAULT_C_SWEEP, rank_shortlist_scale
from research.backtest.rrg_mono_swap_exit_b import build_mono_tier2_calendar
from rrg_improving_watch import build_improving_observation_pool
from rrg_mono_daily_brief import ScanRow
from rrg_universe_intraday_panel import (
    build_abc_v3_f1_observation_pool,
    build_abc_v3_skip09_observation_pool,
    build_dual_wma_lead_pullback_observation_pool,
)
from rrg_mono_swap_accel_screen import (
    _parse_pool_override,
    _signal_date_for_session,
    _kbar_px_at,
    load_or_lock_pool,
)
from stock_db import PROJECT_ROOT

BUY_OBSERVATION_PATH = PROJECT_ROOT / "config" / "buy_observation.yaml"

PoolRole = Literal["adopted", "research", "experimental"]
PoolSource = Literal[
    "rrg_fresh_mono",
    "rrg_mono_tier2",
    "rrg_mono_up_fresh",
    "rrg_improving_watch_setup",
    "rrg_improving_s3_rv",
    "rrg_improving_s3_exec",
    "rrg_improving_s3_burst",
    "rrg_improving_s2_adjacent",
    "rrg_improving_rule_b",
    "rrg_improving_arch_a",
    "rrg_improving_arch_b",
    "rrg_improving_arch_c",
    "dual_wma_lead_pullback",
    "abc_v3_f1_pullback",
    "abc_v3_skip09_pullback",
]


@dataclass(frozen=True)
class BuyObservationPoolSpec:
    id: str
    title: str
    role: PoolRole
    source: PoolSource
    top_n: int
    confirm_bars: int
    notify: bool
    strategy_ref: str = ""
    observe_only: bool = False
    enabled: bool = True


@dataclass(frozen=True)
class BuyObservationTopRow:
    stock_id: str
    stock_name: str
    seg_last: float
    price: float | None
    confirm: int
    note: str = ""
    w3_mom: float | None = None  # 盤中 dual-wma / abc-v3：MV3
    w5_mom: float | None = None  # 盤中 dual-wma / abc-v3：MV5


@dataclass
class PoolObservation:
    pool_id: str
    title: str
    role: str
    notify: bool
    pool_n: int
    pool_as_of: str
    top_rows: list[BuyObservationTopRow] = field(default_factory=list)
    entry_stock_id: str = ""
    entry_stock_name: str = ""
    entry_price: float = 0.0
    entry_reason: str = ""
    skip_note: str = ""


def _slice_top_n(rows: list[Any], top_n: int) -> list[Any]:
    """top_n <= 0 means no display/notify cap (show all pool hits)."""
    if top_n <= 0:
        return rows
    return rows[:top_n]


def load_buy_observation_config(path: Path | None = None) -> tuple[dict[str, Any], list[BuyObservationPoolSpec]]:
    p = path or BUY_OBSERVATION_PATH
    if not p.is_file():
        return {}, []
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    defaults = raw.get("defaults") or {}
    pools_raw = raw.get("pools") or []
    specs: list[BuyObservationPoolSpec] = []
    for item in pools_raw:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        if item.get("enabled") is False:
            continue
        specs.append(
            BuyObservationPoolSpec(
                id=str(item["id"]),
                title=str(item.get("title") or item["id"]),
                role=str(item.get("role") or "research"),
                source=str(item.get("source") or "rrg_fresh_mono"),
                top_n=int(
                    item["top_n"]
                    if item.get("top_n") is not None
                    else defaults.get("top_n", 0)
                ),
                confirm_bars=int(item.get("confirm_bars") or defaults.get("confirm_bars") or 1),
                notify=bool(item.get("notify", defaults.get("notify", False))),
                strategy_ref=str(item.get("strategy_ref") or ""),
                observe_only=bool(item.get("observe_only", defaults.get("observe_only", False))),
                enabled=True,
            )
        )
    return raw, specs


def _pool_confirm_state(state: dict[str, Any], pool_id: str) -> dict[str, Any]:
    pools = state.setdefault("pools", {})
    if pool_id not in pools or not isinstance(pools.get(pool_id), dict):
        pools[pool_id] = {"entry_confirm": {}}
    pools[pool_id].setdefault("entry_confirm", {})
    return pools[pool_id]


def build_observation_pool(
    conn: sqlite3.Connection,
    spec: BuyObservationPoolSpec,
    *,
    session: str,
    poll_minute: str = "",
    close: pd.DataFrame,
    bench: pd.Series,
    ephemeral: dict[str, Any],
) -> tuple[list[ScanRow], str]:
    override_ids = _parse_pool_override(session)
    if spec.source == "rrg_fresh_mono":
        pool, pool_as_of, _, _ = load_or_lock_pool(
            ephemeral,
            conn,
            session=session,
            close=close,
            bench=bench,
            override_ids=override_ids,
        )
        return pool, pool_as_of or ""

    if spec.source == "dual_wma_lead_pullback":
        if not poll_minute:
            return [], session
        return build_dual_wma_lead_pullback_observation_pool(
            conn, session=session, poll_minute=poll_minute
        )

    if spec.source == "abc_v3_f1_pullback":
        if not poll_minute:
            return [], session
        return build_abc_v3_f1_observation_pool(
            conn, session=session, poll_minute=poll_minute
        )

    if spec.source == "abc_v3_skip09_pullback":
        if not poll_minute:
            return [], session
        return build_abc_v3_skip09_observation_pool(
            conn, session=session, poll_minute=poll_minute
        )

    full_dates = close.index.astype(str).tolist()
    signal_as_of = _signal_date_for_session(full_dates, session)
    if not signal_as_of:
        return [], ""

    if spec.source == "rrg_mono_tier2":
        cal = build_mono_tier2_calendar(conn, [signal_as_of], close=close, bench=bench)
        return list(cal.get(signal_as_of) or []), signal_as_of

    if spec.source == "rrg_mono_up_fresh":
        cal = build_mono_up_fresh_calendar(
            conn, [signal_as_of], etf_codes=DEFAULT_ETF_CODES, close=close, bench=bench
        )
        return list(cal.get(signal_as_of) or []), signal_as_of

    if spec.source in (
        "rrg_improving_watch_setup",
        "rrg_improving_s3_rv",
        "rrg_improving_s3_burst",
        "rrg_improving_s2_adjacent",
        "rrg_improving_rule_b",
        "rrg_improving_arch_a",
        "rrg_improving_arch_b",
        "rrg_improving_arch_c",
    ):
        return build_improving_observation_pool(
            conn, spec.source, session=session, close=close, bench=bench
        )

    return [], signal_as_of


def evaluate_pool_observation(
    conn: sqlite3.Connection,
    spec: BuyObservationPoolSpec,
    *,
    pool: list[ScanRow],
    pool_as_of: str,
    session: str,
    poll_minute: str,
    close: pd.DataFrame,
    kbar_cache: dict[tuple[str, str], tuple[tuple[str, float], ...]],
    confirm_state: dict[str, Any],
    c0_cfg: Any,
) -> PoolObservation:
    obs = PoolObservation(
        pool_id=spec.id,
        title=spec.title,
        role=spec.role,
        notify=spec.notify,
        pool_n=len(pool),
        pool_as_of=pool_as_of,
    )
    if not pool:
        obs.skip_note = "池為空"
        return obs

    if spec.observe_only:
        ranked = sorted(
            pool,
            key=lambda r: float(r.composite_score if r.composite_score is not None else r.seg_last),
            reverse=True,
        )
        pit = pool_as_of or session
        for row in _slice_top_n(ranked, spec.top_n):
            intraday_sources = (
                "dual_wma_lead_pullback",
                "abc_v3_f1_pullback",
                "abc_v3_skip09_pullback",
            )
            px: float | None = None
            if spec.source in intraday_sources:
                px = _kbar_px_at(conn, row.stock_id, session, poll_minute, close, kbar_cache)
            elif pit in close.index.astype(str) and row.stock_id in close.columns:
                raw = close.at[pit, row.stock_id]
                if raw is not None and not pd.isna(raw) and float(raw) > 0:
                    px = float(raw)
            note_parts = []
            if spec.source in intraday_sources:
                w20q = row.quadrants[0] if row.quadrants else "?"
                w5q = row.quadrants[1] if len(row.quadrants) > 1 else "?"
                note_parts.append(f"spread={row.seg_last:.1f}")
                note_parts.append(f"W20={w20q}")
                note_parts.append(f"W5={w5q}")
                if spec.source == "abc_v3_f1_pullback":
                    note_parts.append("f1")
                elif spec.source == "abc_v3_skip09_pullback":
                    note_parts.append("v3")
            elif row.daily_pct is not None:
                note_parts.append(f"sig={row.daily_pct:+.2f}%")
                note_parts.append(f"RV={row.rs_ratio:.1f}")
            else:
                note_parts.append(f"RV={row.rs_ratio:.1f}")
            obs.top_rows.append(
                BuyObservationTopRow(
                    stock_id=row.stock_id,
                    stock_name=row.stock_name,
                    seg_last=float(row.rs_ratio),
                    price=px,
                    confirm=0,
                    note=" · ".join(note_parts),
                    w3_mom=row.w3_mom,
                    w5_mom=row.w5_mom,
                )
            )
        obs.skip_note = "observe-only · 無 C0 進場"
        return obs

    ranked = rank_shortlist_scale(
        pool,
        conn=conn,
        close=close,
        trade_date=session,
        minute=poll_minute,
        kbar_cache=kbar_cache,
    )
    if not ranked:
        obs.skip_note = "C0 scale 無排名"
        return obs

    confirm: dict[str, int] = confirm_state.setdefault("entry_confirm", {})
    top_ids = {r.stock_id for r in ranked[:3]}
    for sid in list(confirm.keys()):
        if sid not in top_ids:
            confirm[sid] = 0

    need = int(spec.confirm_bars or c0_cfg.confirm_bars)
    for row in _slice_top_n(ranked, spec.top_n):
        px = _kbar_px_at(conn, row.stock_id, session, poll_minute, close, kbar_cache)
        if row.stock_id in top_ids:
            confirm[row.stock_id] = confirm.get(row.stock_id, 0) + 1
        obs.top_rows.append(
            BuyObservationTopRow(
                stock_id=row.stock_id,
                stock_name=row.stock_name,
                seg_last=float(row.seg_last),
                price=float(px) if px is not None else None,
                confirm=int(confirm.get(row.stock_id, 0)),
            )
        )

    for row in ranked:
        sid = row.stock_id
        if confirm.get(sid, 0) < need:
            continue
        px = _kbar_px_at(conn, sid, session, poll_minute, close, kbar_cache)
        if px is None or px <= 0:
            continue
        obs.entry_stock_id = sid
        obs.entry_stock_name = row.stock_name
        obs.entry_price = float(px)
        obs.entry_reason = f"C0 scale @ {poll_minute} confirm={need}"
        break

    return obs


def role_label_zh(role: str) -> str:
    return {"adopted": "採納", "research": "研究", "experimental": "實驗"}.get(role, role)
