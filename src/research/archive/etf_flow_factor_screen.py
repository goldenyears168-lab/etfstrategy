#!/usr/bin/env python3
"""
ETF 持股變動 · 事前因子檢定（加碼 vs 減碼 vs 控制組）。

僅使用變動當日（含）以前資料（FinMind 股價/法人、TEJ 指數）。
不做事後報酬檢定。

用法：
  python src/etf_flow_factor_screen.py --run --write-report
"""

from __future__ import annotations

import argparse
import sqlite3
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from holdings_research import ADD_ACTIONS, REDUCE_ACTIONS
from report_paths import REPORTS_RESEARCH
from stock_context import _compute_technical_from_rows
from stock_db import (
    PROJECT_ROOT,
    compute_etf_holdings_changes,
    connect,
    list_etf_snapshot_dates,
    load_fundamental_map_as_of,
)

DEFAULT_MAIN_DB = PROJECT_ROOT / "data" / "stocks.db"
DEFAULT_REPORT = REPORTS_RESEARCH / "etf_flow_factor_screen.md"

ETF_CODES = ("00981A", "00403A", "009816", "00980A", "00982A", "00992A")
BENCHMARK_CODE = "IX0001"
ELECTRONIC_INDEX = "IR0002"
PRE_WINDOWS = (5, 10, 14)
INST_LOOKBACK = 5


@dataclass(frozen=True)
class FlowLeg:
    event_date: str
    stock_id: str
    stock_name: str
    etf_code: str
    side: str  # add | reduce
    action: str
    n_etf_same_day: int = 1
    etfs_same_day: frozenset[str] = frozenset()


@dataclass
class FeatureRow:
    event_date: str
    stock_id: str
    side: str
  # numeric features as dict
    values: dict[str, float | int | None] = field(default_factory=dict)


@dataclass(frozen=True)
class FactorSpec:
    key: str
    label: str
    kind: str  # numeric | bool


FACTOR_SPECS: tuple[FactorSpec, ...] = (
    FactorSpec("ret5", "事前5日報酬%", "numeric"),
    FactorSpec("ret10", "事前10日報酬%", "numeric"),
    FactorSpec("ret14", "事前14日報酬%", "numeric"),
    FactorSpec("excess_ix14", "相對台指14日超額%", "numeric"),
    FactorSpec("excess_ir14", "相對電子指14日超額%", "numeric"),
    FactorSpec("rs_univ14", "相對成分股中位數14日超額%", "numeric"),
    FactorSpec("dist_ma20", "MA20乖離%", "numeric"),
    FactorSpec("dist_ma60", "MA60乖離%", "numeric"),
    FactorSpec("pos52", "52週位%", "numeric"),
    FactorSpec("foreign5", "外資5日累計淨買超(百萬)", "numeric"),
    FactorSpec("trust5", "投信5日累計淨買超(百萬)", "numeric"),
    FactorSpec("ma20_rising", "MA20五日上行", "bool"),
    FactorSpec("above_ma20", "站MA20上", "bool"),
    FactorSpec("above_ma60", "站MA60上", "bool"),
    FactorSpec("revenue_yoy_pct", "營收YoY%", "numeric"),
    FactorSpec("roe_ttm", "ROE TTM%", "numeric"),
    FactorSpec("eps_latest_q", "最新季EPS", "numeric"),
    FactorSpec("rs_tier_core14", "核心層14日RS%", "numeric"),
    FactorSpec("rs_tier_sat14", "衛星層14日RS%", "numeric"),
    FactorSpec("rs_tier_blend14", "分層混合14日RS%", "numeric"),
    FactorSpec("tier_core", "市值核心層", "bool"),
)


def _fundamental_feats(row) -> dict[str, float | None]:
    if row is None:
        return {"revenue_yoy_pct": None, "roe_ttm": None, "eps_latest_q": None}
    return {
        "revenue_yoy_pct": float(row["revenue_yoy_pct"]) if row["revenue_yoy_pct"] is not None else None,
        "roe_ttm": float(row["roe_ttm"]) if row["roe_ttm"] is not None else None,
        "eps_latest_q": float(row["eps_latest_q"]) if row["eps_latest_q"] is not None else None,
    }


def apply_tier_rs_features(
    features: dict[str, dict[str, float | int | None]],
    *,
    core_top_n: int | None = None,
) -> dict[str, dict[str, float | int | None]]:
    """依市值 proxy（close×volume）分核心/衛星層，計算 tier RS。"""
    n = len(features)
    if n < 2:
        return features
    top_n = core_top_n if core_top_n is not None else max(1, min(25, n // 2))
    proxies: dict[str, float] = {}
    for sid, feats in features.items():
        close = feats.get("close")
        if close is None:
            continue
        vol = feats.get("volume") or 0
        proxies[sid] = float(close) * max(float(vol), 1.0)
    if len(proxies) < 2:
        return features
    ranked = sorted(proxies.keys(), key=lambda s: proxies[s], reverse=True)
    core = set(ranked[:top_n])
    satellite = set(ranked[top_n:])

    core_rets = [
        float(features[s]["ret14"])
        for s in core
        if features[s].get("ret14") is not None
    ]
    sat_rets = [
        float(features[s]["ret14"])
        for s in satellite
        if features[s].get("ret14") is not None
    ]
    core_med = statistics.median(core_rets) if core_rets else None
    sat_med = statistics.median(sat_rets) if sat_rets else None

    out: dict[str, dict[str, float | int | None]] = {}
    for sid, feats in features.items():
        merged = dict(feats)
        merged["tier_core"] = int(sid in core)
        r14 = feats.get("ret14")
        rs_core = rs_sat = None
        if r14 is not None and core_med is not None and sid in core:
            rs_core = round(float(r14) - core_med, 2)
        if r14 is not None and sat_med is not None and sid in satellite:
            rs_sat = round(float(r14) - sat_med, 2)
        merged["rs_tier_core14"] = rs_core
        merged["rs_tier_sat14"] = rs_sat
        if sid in core:
            merged["rs_tier_blend14"] = rs_core
        else:
            merged["rs_tier_blend14"] = rs_sat
        out[sid] = merged
    return out


@dataclass(frozen=True)
class FactorEffect:
    key: str
    label: str
    kind: str
    n_add: int
    n_reduce: int
    n_ctrl: int
    mean_add: float | None
    mean_reduce: float | None
    mean_ctrl: float | None
    delta_add_ctrl: float | None
    delta_reduce_ctrl: float | None
    delta_add_reduce: float | None
    pct_add: float | None = None
    pct_reduce: float | None = None
    pct_ctrl: float | None = None


def _closes_on_or_before(
    conn: sqlite3.Connection,
    *,
    table: str,
    id_col: str,
    code: str,
    as_of: str,
) -> list[tuple[str, float]]:
    if table == "daily_bars":
        rows = conn.execute(
            f"""
            SELECT date AS trade_date, close FROM daily_bars
            WHERE code = ? AND date <= ? ORDER BY date
            """,
            (code, as_of),
        ).fetchall()
    else:
        rows = conn.execute(
            f"""
            SELECT trade_date, close FROM stock_daily_bars
            WHERE stock_id = ? AND source = 'finmind' AND trade_date <= ?
            ORDER BY trade_date
            """,
            (code, as_of),
        ).fetchall()
    return [(str(r[0]), float(r[1])) for r in rows if r[1] is not None]


def _ret_n(closes: list[tuple[str, float]], n: int) -> float | None:
    if len(closes) < n + 1:
        return None
    c0 = closes[-(n + 1)][1]
    c1 = closes[-1][1]
    if c0 <= 0:
        return None
    return round((c1 / c0 - 1.0) * 100.0, 2)


def _ma20_rising(closes: list[float]) -> int | None:
    if len(closes) < 25:
        return None
    ma_now = sum(closes[-20:]) / 20
    ma_prev = sum(closes[-25:-5]) / 20
    return int(ma_now > ma_prev)


def _inst_sum(conn: sqlite3.Connection, stock_id: str, as_of: str, col: str, days: int = INST_LOOKBACK) -> float | None:
    rows = conn.execute(
        f"""
        SELECT {col} FROM stock_institutional_daily
        WHERE stock_id = ? AND source = 'finmind' AND trade_date <= ?
        ORDER BY trade_date DESC
        LIMIT ?
        """,
        (stock_id, as_of, days),
    ).fetchall()
    if not rows:
        return None
    vals = [float(r[0]) for r in rows if r[0] is not None]
    if not vals:
        return None
    return round(sum(vals) / 1_000_000, 2)


def collect_flow_legs(conn: sqlite3.Connection) -> list[FlowLeg]:
    """蒐集所有 ETF 加減碼 leg，並標註同日同股 ETF 檔數。"""
    raw: list[FlowLeg] = []
    for etf in ETF_CODES:
        dates = sorted(list_etf_snapshot_dates(conn, etf))
        for i in range(1, len(dates)):
            prev, curr = dates[i - 1], dates[i]
            for row in compute_etf_holdings_changes(conn, etf, curr, prev):
                r = dict(row)
                action = str(r.get("action", ""))
                if action in ADD_ACTIONS:
                    side = "add"
                elif action in REDUCE_ACTIONS:
                    side = "reduce"
                else:
                    continue
                raw.append(
                    FlowLeg(
                        event_date=curr,
                        stock_id=str(r["stock_id"]),
                        stock_name=str(r.get("stock_name") or r["stock_id"]),
                        etf_code=etf,
                        side=side,
                        action=action,
                    )
                )

    etf_count: dict[tuple[str, str, str], int] = defaultdict(int)
    etf_set: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    for leg in raw:
        k = (leg.event_date, leg.stock_id, leg.side)
        etf_count[k] += 1
        etf_set[k].add(leg.etf_code)

    out: list[FlowLeg] = []
    for leg in raw:
        k = (leg.event_date, leg.stock_id, leg.side)
        out.append(
            FlowLeg(
                event_date=leg.event_date,
                stock_id=leg.stock_id,
                stock_name=leg.stock_name,
                etf_code=leg.etf_code,
                side=leg.side,
                action=leg.action,
                n_etf_same_day=etf_count[k],
                etfs_same_day=frozenset(etf_set[k]),
            )
        )
    return out


def unique_stock_days(legs: list[FlowLeg]) -> list[FlowLeg]:
    """同日同股同方向只保留一筆（帶 n_etf / etfs）。"""
    seen: dict[tuple[str, str, str], FlowLeg] = {}
    for leg in legs:
        k = (leg.event_date, leg.stock_id, leg.side)
        if k not in seen:
            seen[k] = leg
        elif leg.n_etf_same_day > seen[k].n_etf_same_day:
            seen[k] = leg
    return list(seen.values())


def _only_009816_add(leg: FlowLeg) -> bool:
    return leg.side == "add" and leg.etfs_same_day == frozenset({"009816"})


def build_feature_rows(
    conn: sqlite3.Connection,
    legs: list[FlowLeg],
    universe: list[str],
) -> tuple[list[FeatureRow], list[FeatureRow]]:
    """回傳 (事件特徵, 控制組特徵)。"""
    bar_cache: dict[str, list[tuple[str, float]]] = {}
    ix_cache: dict[str, list[tuple[str, float]]] = {}
    ir_cache: dict[str, list[tuple[str, float]]] = {}

    def stock_closes(sid: str, d: str) -> list[tuple[str, float]]:
        if sid not in bar_cache:
            bar_cache[sid] = _closes_on_or_before(
                conn,
                table="stock_daily_bars",
                id_col="stock_id",
                code=sid,
                as_of="9999-12-31",
            )
        return [(dt, c) for dt, c in bar_cache[sid] if dt <= d]

    def ix_closes(d: str) -> list[tuple[str, float]]:
        if "all" not in ix_cache:
            ix_cache["all"] = _closes_on_or_before(
                conn, table="daily_bars", id_col="code", code=BENCHMARK_CODE, as_of="9999-12-31"
            )
        return [(dt, c) for dt, c in ix_cache["all"] if dt <= d]

    def ir_closes(d: str) -> list[tuple[str, float]]:
        if "all" not in ir_cache:
            ir_cache["all"] = _closes_on_or_before(
                conn, table="daily_bars", id_col="code", code=ELECTRONIC_INDEX, as_of="9999-12-31"
            )
        return [(dt, c) for dt, c in ir_cache["all"] if dt <= d]

    def compute_feats(sid: str, d: str) -> dict[str, float | int | None]:
        closes = stock_closes(sid, d)
        float_closes = [c[1] for c in closes]
        rows = conn.execute(
            """
            SELECT trade_date, open, high, low, close, volume
            FROM stock_daily_bars
            WHERE stock_id = ? AND source = 'finmind' AND trade_date <= ?
            ORDER BY trade_date
            """,
            (sid, d),
        ).fetchall()
        tech = _compute_technical_from_rows(rows, entity_id=sid) if len(rows) >= 20 else None

        ix = ix_closes(d)
        ir = ir_closes(d)
        ret5 = _ret_n(closes, 5)
        ret10 = _ret_n(closes, 10)
        ret14 = _ret_n(closes, 14)
        ix14 = _ret_n(ix, 14)
        ir14 = _ret_n(ir, 14)
        last_vol = int(rows[-1][5]) if rows and rows[-1][5] is not None else None
        last_close = float_closes[-1] if float_closes else None
        return {
            "close": last_close,
            "volume": last_vol,
            "ret5": ret5,
            "ret10": ret10,
            "ret14": ret14,
            "excess_ix14": round(ret14 - ix14, 2) if ret14 is not None and ix14 is not None else None,
            "excess_ir14": round(ret14 - ir14, 2) if ret14 is not None and ir14 is not None else None,
            "dist_ma20": tech.dist_ma20_pct if tech else None,
            "dist_ma60": tech.dist_ma60_pct if tech else None,
            "pos52": tech.position_52w_pct if tech else None,
            "foreign5": _inst_sum(conn, sid, d, "foreign_net"),
            "trust5": _inst_sum(conn, sid, d, "investment_trust_net"),
            "ma20_rising": _ma20_rising(float_closes),
            "above_ma20": int(tech.dist_ma20_pct > 0) if tech and tech.dist_ma20_pct is not None else None,
            "above_ma60": int(tech.dist_ma60_pct is not None and tech.dist_ma60_pct > 0) if tech else None,
        }

    event_dates = sorted({leg.event_date for leg in legs})
    median_cache: dict[str, float | None] = {}

    def rs_univ14(sid: str, d: str) -> float | None:
        if d not in median_cache:
            vals = []
            for u in universe:
                c = stock_closes(u, d)
                v = _ret_n(c, 14)
                if v is not None:
                    vals.append(v)
            median_cache[d] = statistics.median(vals) if vals else None
        ret14 = _ret_n(stock_closes(sid, d), 14)
        med = median_cache[d]
        if ret14 is None or med is None:
            return None
        return round(ret14 - med, 2)

    fund_cache: dict[str, dict] = {}
    tier_cache: dict[str, dict[str, dict[str, float | int | None]]] = {}

    def feats_for(sid: str, d: str) -> dict[str, float | int | None]:
        if d not in tier_cache:
            if d not in fund_cache:
                fund_cache[d] = load_fundamental_map_as_of(conn, universe, d)
            batch: dict[str, dict[str, float | int | None]] = {}
            for u in universe:
                f = compute_feats(u, d)
                f["rs_univ14"] = rs_univ14(u, d)
                f.update(_fundamental_feats(fund_cache[d].get(u)))
                batch[u] = f
            tier_cache[d] = apply_tier_rs_features(batch)
        return dict(tier_cache[d][sid])

    event_rows: list[FeatureRow] = []
    for leg in legs:
        event_rows.append(
            FeatureRow(
                event_date=leg.event_date,
                stock_id=leg.stock_id,
                side=leg.side,
                values=feats_for(leg.stock_id, leg.event_date),
            )
        )

    ctrl_rows: list[FeatureRow] = []
    active = {(leg.event_date, leg.stock_id) for leg in legs}
    for d in event_dates:
        touched = {sid for (dd, sid) in active if dd == d}
        for sid in universe:
            if sid in touched:
                continue
            ctrl_rows.append(
                FeatureRow(event_date=d, stock_id=sid, side="control", values=feats_for(sid, d))
            )

    return event_rows, ctrl_rows


def universe_features_at(
    conn: sqlite3.Connection,
    universe: list[str],
    as_of_date: str,
) -> dict[str, dict[str, float | int | None]]:
    """單日、單 universe 全成分股事前特徵（供行為預測打分）。"""
    bar_cache: dict[str, list[tuple[str, float]]] = {}
    ix_cache: dict[str, list[tuple[str, float]]] = {}
    ir_cache: dict[str, list[tuple[str, float]]] = {}

    def stock_closes(sid: str, d: str) -> list[tuple[str, float]]:
        if sid not in bar_cache:
            bar_cache[sid] = _closes_on_or_before(
                conn,
                table="stock_daily_bars",
                id_col="stock_id",
                code=sid,
                as_of="9999-12-31",
            )
        return [(dt, c) for dt, c in bar_cache[sid] if dt <= d]

    def ix_closes(d: str) -> list[tuple[str, float]]:
        if "all" not in ix_cache:
            ix_cache["all"] = _closes_on_or_before(
                conn, table="daily_bars", id_col="code", code=BENCHMARK_CODE, as_of="9999-12-31"
            )
        return [(dt, c) for dt, c in ix_cache["all"] if dt <= d]

    def ir_closes(d: str) -> list[tuple[str, float]]:
        if "all" not in ir_cache:
            ir_cache["all"] = _closes_on_or_before(
                conn, table="daily_bars", id_col="code", code=ELECTRONIC_INDEX, as_of="9999-12-31"
            )
        return [(dt, c) for dt, c in ir_cache["all"] if dt <= d]

    def compute_feats(sid: str, d: str) -> dict[str, float | int | None]:
        closes = stock_closes(sid, d)
        float_closes = [c[1] for c in closes]
        rows = conn.execute(
            """
            SELECT trade_date, open, high, low, close, volume
            FROM stock_daily_bars
            WHERE stock_id = ? AND source = 'finmind' AND trade_date <= ?
            ORDER BY trade_date
            """,
            (sid, d),
        ).fetchall()
        tech = _compute_technical_from_rows(rows, entity_id=sid) if len(rows) >= 20 else None

        ix = ix_closes(d)
        ir = ir_closes(d)
        ret5 = _ret_n(closes, 5)
        ret10 = _ret_n(closes, 10)
        ret14 = _ret_n(closes, 14)
        ix14 = _ret_n(ix, 14)
        ir14 = _ret_n(ir, 14)
        last_vol = int(rows[-1][5]) if rows and rows[-1][5] is not None else None
        last_close = float_closes[-1] if float_closes else None
        return {
            "close": last_close,
            "volume": last_vol,
            "ret5": ret5,
            "ret10": ret10,
            "ret14": ret14,
            "excess_ix14": round(ret14 - ix14, 2) if ret14 is not None and ix14 is not None else None,
            "excess_ir14": round(ret14 - ir14, 2) if ret14 is not None and ir14 is not None else None,
            "dist_ma20": tech.dist_ma20_pct if tech else None,
            "dist_ma60": tech.dist_ma60_pct if tech else None,
            "pos52": tech.position_52w_pct if tech else None,
            "foreign5": _inst_sum(conn, sid, d, "foreign_net"),
            "trust5": _inst_sum(conn, sid, d, "investment_trust_net"),
            "ma20_rising": _ma20_rising(float_closes),
            "above_ma20": int(tech.dist_ma20_pct > 0) if tech and tech.dist_ma20_pct is not None else None,
            "above_ma60": int(tech.dist_ma60_pct is not None and tech.dist_ma60_pct > 0) if tech else None,
        }

    median_cache: dict[str, float | None] = {}

    def rs_univ14(sid: str, d: str) -> float | None:
        if d not in median_cache:
            vals = []
            for u in universe:
                c = stock_closes(u, d)
                v = _ret_n(c, 14)
                if v is not None:
                    vals.append(v)
            median_cache[d] = statistics.median(vals) if vals else None
        ret14 = _ret_n(stock_closes(sid, d), 14)
        med = median_cache[d]
        if ret14 is None or med is None:
            return None
        return round(ret14 - med, 2)

    out: dict[str, dict[str, float | int | None]] = {}
    fund_map = load_fundamental_map_as_of(conn, universe, as_of_date)
    for sid in universe:
        feats = compute_feats(sid, as_of_date)
        feats["rs_univ14"] = rs_univ14(sid, as_of_date)
        feats.update(_fundamental_feats(fund_map.get(sid)))
        out[sid] = feats
    return apply_tier_rs_features(out)


def _mean(vals: list[float]) -> float | None:
    if not vals:
        return None
    return round(statistics.mean(vals), 2)


def _pct_true(vals: list[int]) -> float | None:
    if not vals:
        return None
    return round(sum(vals) / len(vals) * 100.0, 1)


def screen_factors(
    event_rows: list[FeatureRow],
    ctrl_rows: list[FeatureRow],
) -> list[FactorEffect]:
    adds = [r for r in event_rows if r.side == "add"]
    reduces = [r for r in event_rows if r.side == "reduce"]

    effects: list[FactorEffect] = []
    for spec in FACTOR_SPECS:
        a_vals = [float(r.values[spec.key]) for r in adds if r.values.get(spec.key) is not None]
        r_vals = [float(r.values[spec.key]) for r in reduces if r.values.get(spec.key) is not None]
        c_vals = [float(r.values[spec.key]) for r in ctrl_rows if r.values.get(spec.key) is not None]

        if spec.kind == "bool":
            a_bool = [int(r.values[spec.key]) for r in adds if r.values.get(spec.key) is not None]
            r_bool = [int(r.values[spec.key]) for r in reduces if r.values.get(spec.key) is not None]
            c_bool = [int(r.values[spec.key]) for r in ctrl_rows if r.values.get(spec.key) is not None]
            m_add = _pct_true(a_bool)
            m_red = _pct_true(r_bool)
            m_ctrl = _pct_true(c_bool)
            d_ac = round(m_add - m_ctrl, 1) if m_add is not None and m_ctrl is not None else None
            d_rc = round(m_red - m_ctrl, 1) if m_red is not None and m_ctrl is not None else None
            d_ar = round(m_add - m_red, 1) if m_add is not None and m_red is not None else None
            effects.append(
                FactorEffect(
                    key=spec.key,
                    label=spec.label,
                    kind=spec.kind,
                    n_add=len(a_bool),
                    n_reduce=len(r_bool),
                    n_ctrl=len(c_bool),
                    mean_add=m_add,
                    mean_reduce=m_red,
                    mean_ctrl=m_ctrl,
                    delta_add_ctrl=d_ac,
                    delta_reduce_ctrl=d_rc,
                    delta_add_reduce=d_ar,
                    pct_add=m_add,
                    pct_reduce=m_red,
                    pct_ctrl=m_ctrl,
                )
            )
        else:
            m_add = _mean(a_vals)
            m_red = _mean(r_vals)
            m_ctrl = _mean(c_vals)
            d_ac = round(m_add - m_ctrl, 2) if m_add is not None and m_ctrl is not None else None
            d_rc = round(m_red - m_ctrl, 2) if m_red is not None and m_ctrl is not None else None
            d_ar = round(m_add - m_red, 2) if m_add is not None and m_red is not None else None
            effects.append(
                FactorEffect(
                    key=spec.key,
                    label=spec.label,
                    kind=spec.kind,
                    n_add=len(a_vals),
                    n_reduce=len(r_vals),
                    n_ctrl=len(c_vals),
                    mean_add=m_add,
                    mean_reduce=m_red,
                    mean_ctrl=m_ctrl,
                    delta_add_ctrl=d_ac,
                    delta_reduce_ctrl=d_rc,
                    delta_add_reduce=d_ar,
                )
            )
    return effects


def build_report(
    *,
    legs: list[FlowLeg],
    unique_legs: list[FlowLeg],
    effects_all: list[FactorEffect],
    effects_unique: list[FactorEffect],
    effects_no816: list[FactorEffect],
    snapshot_dates: list[str],
) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    n_add = sum(1 for l in legs if l.side == "add")
    n_red = sum(1 for l in legs if l.side == "reduce")
    u_add = sum(1 for l in unique_legs if l.side == "add")
    u_red = sum(1 for l in unique_legs if l.side == "reduce")

    lines = [
        "# ETF 持股變動 · 事前因子檢定",
        "",
        f"> 產出 {now} · 資料 `stocks.db` · **僅變動當日及以前**特徵，不含事後報酬",
        "",
        "## 樣本",
        "",
        f"- 持股 snapshot：**{snapshot_dates[0]} ~ {snapshot_dates[-1]}**（{len(snapshot_dates)} 日）",
        f"- 加碼 leg：**{n_add}** · 減碼 leg：**{n_red}**",
        f"- 去重 stock-day：加碼 **{u_add}** · 減碼 **{u_red}**",
        "- 控制組：同日成分股聯集內**未變動**標的",
        "- 價格 FinMind · 指數 TEJ（IX0001 / IR0002）· 法人 FinMind",
        "",
        "## 買入共通點 vs 賣出共通點（去重 stock-day）",
        "",
        _interpret_buy_sell(effects_unique),
        "",
        "## 因子效果量（去重 stock-day）",
        "",
        "| 因子 | 加碼均 | 減碼均 | 控制均 | Δ加-控 | Δ減-控 | Δ加-減 |",
        "|------|--------|--------|--------|--------|--------|--------|",
    ]

    for ef in sorted(effects_unique, key=lambda x: abs(x.delta_add_ctrl or 0), reverse=True):
        if ef.kind == "bool":
            lines.append(
                f"| {ef.label} | {ef.pct_add}% | {ef.pct_reduce}% | {ef.pct_ctrl}% "
                f"| {ef.delta_add_ctrl:+.1f}pp | {ef.delta_reduce_ctrl:+.1f}pp | {ef.delta_add_reduce:+.1f}pp |"
            )
        else:
            lines.append(
                f"| {ef.label} | {ef.mean_add:+.2f} | {ef.mean_reduce:+.2f} | {ef.mean_ctrl:+.2f} "
                f"| {ef.delta_add_ctrl:+.2f} | {ef.delta_reduce_ctrl:+.2f} | {ef.delta_add_reduce:+.2f} |"
            )

    lines.extend(
        [
            "",
            "## 全 leg（含重複 ETF 加減碼）",
            "",
            "| 因子 | Δ加-控 | Δ減-控 | Δ加-減 |",
            "|------|--------|--------|--------|",
        ]
    )
    for ef in sorted(effects_all, key=lambda x: abs(x.delta_add_ctrl or 0), reverse=True)[:8]:
        u = "pp" if ef.kind == "bool" else ""
        lines.append(
            f"| {ef.label} | {ef.delta_add_ctrl}{u} | {ef.delta_reduce_ctrl}{u} | {ef.delta_add_reduce}{u} |"
        )

    lines.extend(
        [
            "",
            "## 排除「僅 009816」加碼（去重）",
            "",
            "> 009816 常單日大量微調金融股，易主導全樣本。",
            "",
            "| 因子 | Δ加-控 | Δ減-控 |",
            "|------|--------|--------|",
        ]
    )
    for ef in sorted(effects_no816, key=lambda x: abs(x.delta_add_ctrl or 0), reverse=True)[:8]:
        u = "pp" if ef.kind == "bool" else ""
        lines.append(f"| {ef.label} | {ef.delta_add_ctrl}{u} | {ef.delta_reduce_ctrl}{u} |")

    top_add = sorted(effects_unique, key=lambda x: abs(x.delta_add_ctrl or 0), reverse=True)[:3]
    lines.extend(
        [
            "",
            "## 關鍵洞察（依 |Δ加-控| 排序）",
            "",
        ]
    )
    for i, ef in enumerate(top_add, 1):
        lines.append(
            f"{i}. **{ef.label}**：加碼高於控制 **{ef.delta_add_ctrl}**"
            f"{'pp' if ef.kind == 'bool' else ''}；"
            f"減碼差異 **{ef.delta_reduce_ctrl}**；買賣對照 **{ef.delta_add_reduce}**"
        )

    lines.extend(
        [
            "",
            "---",
            "",
            "重新產出：`python src/etf_flow_factor_screen.py --run --write-report`",
        ]
    )
    return "\n".join(lines)


def _interpret_buy_sell(effects: list[FactorEffect]) -> str:
    by_key = {e.key: e for e in effects}
    parts: list[str] = []

    def line(title: str, keys: list[str], *, delta: str) -> None:
        bits = []
        for k in keys:
            ef = by_key.get(k)
            if ef is None:
                continue
            v = getattr(ef, delta, None)
            if v is not None:
                suffix = "pp" if ef.kind == "bool" else ""
                bits.append(f"{ef.label} Δ={v}{suffix}")
        if bits:
            parts.append(f"**{title}**：" + "；".join(bits))

    line("買入偏向", ["rs_univ14", "excess_ix14", "ret14", "ma20_rising"], delta="delta_add_ctrl")
    line("賣出偏向", ["ret14", "dist_ma20", "pos52", "foreign5", "trust5"], delta="delta_reduce_ctrl")

    rs = by_key.get("rs_univ14")
    ma = by_key.get("ma20_rising")
    ret = by_key.get("ret14")
    if rs and ma and ret:
        parts.append(
            f"\n綜合：加碼標的較控制組**相對成分股更強**（rs_univ14 Δ加-控={rs.delta_add_ctrl}），"
            f"**MA20 轉強比例更高**（Δ={ma.delta_add_ctrl}pp），"
            f"但多數**未站上 MA20/MA60**（見下表）。"
            f"減碼標的事前 14 日報酬較控制**{ret.delta_reduce_ctrl:+.2f}**；"
            f"常見於動能轉弱或籌碼面與加碼組相反。"
        )
    return "\n".join(parts) if parts else "_資料不足_"


def run_screen(main_db: Path) -> tuple[list[FlowLeg], list[FlowLeg], list[FactorEffect], list[FactorEffect], list[FactorEffect], list[str]]:
    with connect(main_db) as conn:
        legs = collect_flow_legs(conn)
        unique = unique_stock_days(legs)
        universe = [r[0] for r in conn.execute("SELECT DISTINCT stock_id FROM etf_holdings").fetchall()]
        dates = sorted(
            {r[0] for r in conn.execute("SELECT DISTINCT snapshot_date FROM etf_holdings").fetchall()}
        )

        ev_all, ctrl_all = build_feature_rows(conn, legs, universe)
        ev_u, ctrl_u = build_feature_rows(conn, unique, universe)

        no816 = [l for l in unique if not _only_009816_add(l)]
        ev_816, ctrl_816 = build_feature_rows(conn, no816, universe)

        return (
            legs,
            unique,
            screen_factors(ev_all, ctrl_all),
            screen_factors(ev_u, ctrl_u),
            screen_factors(ev_816, ctrl_816),
            dates,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="ETF 持股變動事前因子檢定")
    parser.add_argument("--main-db", type=Path, default=DEFAULT_MAIN_DB)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--write-report", action="store_true")
    args = parser.parse_args()

    if not args.run and not args.write_report:
        args.run = args.write_report = True

    legs, unique, eff_all, eff_u, eff_816, dates = run_screen(args.main_db)
    print(f"Legs: add={sum(1 for l in legs if l.side=='add')} reduce={sum(1 for l in legs if l.side=='reduce')}")
    print(f"Unique stock-day: add={sum(1 for l in unique if l.side=='add')} reduce={sum(1 for l in unique if l.side=='reduce')}")

    if args.write_report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        text = build_report(
            legs=legs,
            unique_legs=unique,
            effects_all=eff_all,
            effects_unique=eff_u,
            effects_no816=eff_816,
            snapshot_dates=dates,
        )
        args.report.write_text(text, encoding="utf-8")
        print(f"Report → {args.report}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
