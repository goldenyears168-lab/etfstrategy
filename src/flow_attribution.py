#!/usr/bin/env python3
"""v0.3 ETF Flow Attribution：只讀 flow_events 快照（不 replay intent）。"""

from __future__ import annotations

import hashlib
import random
import sqlite3
from dataclasses import dataclass, field
from statistics import mean, median

from chip_narrative import compose_chip_narrative
from holdings_research import TW_SPOT_CODE
from project_config import (
    BASELINE_RANDOM_SEED,
    DEFAULT_FLOW_EVENT_LOOKBACK,
    FLOW_HORIZONS,
    FLOW_PRIMARY_HORIZONS,
    FLOW_VERSION,
)
from stock_db import list_flow_event_dates, load_flow_events, load_stock_beta_map

BENCHMARK_CODE = TW_SPOT_CODE
DEFAULT_BETA = 1.0
TRIM_INTENTS = frozenset({"TRIM_CORE", "TRIM_SATELLITE"})


def capm_alpha_pct(ret_pct: float, bench_pct: float, beta: float) -> float:
    return ret_pct - beta * bench_pct


def return_pct(close_t: float, close_t1: float) -> float:
    if close_t <= 0:
        return 0.0
    return (close_t1 - close_t) / close_t * 100.0


def _seed_for_date(event_date: str) -> int:
    s = f"{BASELINE_RANDOM_SEED}:{event_date}"
    return int(hashlib.sha256(s.encode()).hexdigest()[:16], 16)


def _intent_label(intent: str) -> str:
    if intent in TRIM_INTENTS:
        return "TRIM_ALL"
    return intent


def _beta_for_stock(beta_map: dict[str, sqlite3.Row], stock_id: str) -> float:
    row = beta_map.get(stock_id)
    if row is None or row["beta"] is None:
        return DEFAULT_BETA
    return float(row["beta"])


def _bench_close(conn: sqlite3.Connection, trade_date: str) -> float | None:
    row = conn.execute(
        """
        SELECT close FROM daily_bars
        WHERE code = ? AND date = ?
        ORDER BY CASE source WHEN 'tej' THEN 0 WHEN 'yahoo' THEN 1 ELSE 2 END
        LIMIT 1
        """,
        (BENCHMARK_CODE, trade_date),
    ).fetchone()
    if row is None:
        return None
    return float(row["close"])


def _stock_close(conn: sqlite3.Connection, stock_id: str, trade_date: str) -> float | None:
    row = conn.execute(
        """
        SELECT close FROM stock_daily_bars
        WHERE stock_id = ? AND trade_date = ? AND source = 'finmind'
        """,
        (stock_id, trade_date),
    ).fetchone()
    if row is None:
        return None
    return float(row["close"])


def _outcome_date_after_k(
    conn: sqlite3.Connection,
    signal_date: str,
    k: int,
) -> str | None:
    if k < 1:
        return None
    rows = conn.execute(
        """
        SELECT DISTINCT trade_date AS d
        FROM stock_daily_bars
        WHERE trade_date > ? AND source = 'finmind'
        ORDER BY d ASC
        LIMIT ?
        """,
        (signal_date, k),
    ).fetchall()
    if len(rows) < k:
        return None
    outcome = str(rows[k - 1]["d"])
    if _bench_close(conn, signal_date) is None or _bench_close(conn, outcome) is None:
        return None
    return outcome


def _eligible_stocks(conn: sqlite3.Connection, trade_date: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT stock_id
        FROM stock_daily_bars
        WHERE trade_date = ? AND source = 'finmind'
        ORDER BY stock_id
        """,
        (trade_date,),
    ).fetchall()
    return [str(r["stock_id"]) for r in rows]


def _event_capm_alpha(
    conn: sqlite3.Connection,
    *,
    event_date: str,
    stock_id: str,
    horizon: int,
    beta_map: dict[str, sqlite3.Row],
) -> tuple[float | None, str]:
    outcome_date = _outcome_date_after_k(conn, event_date, horizon)
    if outcome_date is None:
        return None, "missing_outcome"
    c0 = _stock_close(conn, stock_id, event_date)
    c1 = _stock_close(conn, stock_id, outcome_date)
    b0 = _bench_close(conn, event_date)
    b1 = _bench_close(conn, outcome_date)
    if c0 is None or c1 is None or b0 is None or b1 is None:
        return None, "missing_bar"
    ret = return_pct(c0, c1)
    bench = return_pct(b0, b1)
    beta = _beta_for_stock(beta_map, stock_id)
    return capm_alpha_pct(ret, bench, beta), "complete"


@dataclass(frozen=True)
class FlowOutcome:
    event_date: str
    stock_id: str
    stock_name: str
    net_side: str
    consensus: str
    intent: str
    conviction: float
    source_etfs: str
    horizon: int
    capm_alpha_pct: float | None
    status: str


@dataclass
class GroupStats:
    label: str
    horizon: int
    n: int
    mean_capm: float | None
    median_capm: float | None
    hit_rate: float | None


@dataclass
class CoverageRow:
    horizon: int
    expected: int
    available: int


@dataclass
class FlowAttributionResult:
    flow_version: str
    window_start: str | None = None
    window_end: str | None = None
    event_dates: list[str] = field(default_factory=list)
    outcomes: list[FlowOutcome] = field(default_factory=list)
    coverage: list[CoverageRow] = field(default_factory=list)
    groups_net_side: list[GroupStats] = field(default_factory=list)
    groups_consensus: list[GroupStats] = field(default_factory=list)
    groups_intent: list[GroupStats] = field(default_factory=list)
    random_baseline: list[GroupStats] = field(default_factory=list)
    boss_gate: str = ""
    message: str | None = None


def _group_stats(
    outcomes: list[FlowOutcome],
    *,
    key_fn,
    horizon: int,
) -> list[GroupStats]:
    buckets: dict[str, list[float]] = {}
    for o in outcomes:
        if o.horizon != horizon or o.status != "complete" or o.capm_alpha_pct is None:
            continue
        buckets.setdefault(key_fn(o), []).append(o.capm_alpha_pct)
    rows: list[GroupStats] = []
    for label in sorted(buckets):
        vals = buckets[label]
        rows.append(
            GroupStats(
                label=label,
                horizon=horizon,
                n=len(vals),
                mean_capm=mean(vals),
                median_capm=median(vals),
                hit_rate=100.0 * sum(1 for v in vals if v > 0) / len(vals),
            )
        )
    return rows


def _random_baseline(
    conn: sqlite3.Connection,
    *,
    event_dates: list[str],
    events_by_date: dict[str, list[sqlite3.Row]],
    horizons: tuple[int, ...],
    beta_map: dict[str, sqlite3.Row],
) -> list[GroupStats]:
    by_horizon: dict[int, list[float]] = {h: [] for h in horizons}
    for event_date in event_dates:
        day_rows = events_by_date.get(event_date, [])
        n = len(day_rows)
        if n == 0:
            continue
        pool = _eligible_stocks(conn, event_date)
        if len(pool) < n:
            continue
        rng = random.Random(_seed_for_date(event_date))
        picks = rng.sample(pool, n)
        for stock_id in picks:
            for h in horizons:
                alpha, status = _event_capm_alpha(
                    conn,
                    event_date=event_date,
                    stock_id=stock_id,
                    horizon=h,
                    beta_map=beta_map,
                )
                if status == "complete" and alpha is not None:
                    by_horizon[h].append(alpha)
    rows: list[GroupStats] = []
    for h in horizons:
        vals = by_horizon[h]
        rows.append(
            GroupStats(
                label="Random Control",
                horizon=h,
                n=len(vals),
                mean_capm=mean(vals) if vals else None,
                median_capm=median(vals) if vals else None,
                hit_rate=(
                    100.0 * sum(1 for v in vals if v > 0) / len(vals) if vals else None
                ),
            )
        )
    return rows


def _boss_gate(add_stats: list[GroupStats]) -> str:
    primary = [s for s in add_stats if s.horizon in FLOW_PRIMARY_HORIZONS and s.n > 0]
    if not primary:
        return "資料不足：無 H+3/H+5 加碼樣本，尚無法判定 ETF Flow Alpha。"
    parts: list[str] = []
    for s in primary:
        m = s.mean_capm
        if m is None:
            parts.append(f"H+{s.horizon} 無有效樣本")
            continue
        sign = "正" if m > 0 else "非正"
        parts.append(f"H+{s.horizon} 加碼 Mean CAPM α {m:+.2f}%（{sign}）N={s.n}")
    verdict = "；".join(parts)
    positives = [s for s in primary if s.mean_capm is not None and s.mean_capm > 0]
    if len(positives) == len([s for s in primary if s.mean_capm is not None]):
        return f"**通過（敘述性）**：{verdict}"
    if positives:
        return f"**部分通過**：{verdict}"
    return f"**未通過（敘述性）**：{verdict}"


def run_flow_attribution(
    conn: sqlite3.Connection,
    *,
    as_of: str | None = None,
    lookback: int = DEFAULT_FLOW_EVENT_LOOKBACK,
    flow_version: str = FLOW_VERSION,
) -> FlowAttributionResult:
    if as_of is None:
        row = conn.execute(
            "SELECT MAX(event_date) AS d FROM flow_events WHERE flow_version = ?",
            (flow_version,),
        ).fetchone()
        as_of = str(row["d"]) if row and row["d"] else None
    if not as_of:
        return FlowAttributionResult(
            flow_version=flow_version,
            message="尚無 flow_events 資料；請先跑 ② 收盤持股雷達（--intent）落地快照。",
        )

    event_dates = list_flow_event_dates(
        conn, flow_version=flow_version, as_of=as_of, lookback=lookback
    )
    if not event_dates:
        return FlowAttributionResult(
            flow_version=flow_version,
            message=f"窗口內無 flow_events（as_of={as_of} lookback={lookback}）。",
        )

    rows = load_flow_events(conn, flow_version=flow_version, event_dates=event_dates)
    beta_map, _beta_as_of = load_stock_beta_map(conn)
    events_by_date: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        events_by_date.setdefault(str(r["event_date"]), []).append(r)

    outcomes: list[FlowOutcome] = []
    for r in rows:
        for h in FLOW_HORIZONS:
            alpha, status = _event_capm_alpha(
                conn,
                event_date=str(r["event_date"]),
                stock_id=str(r["stock_id"]),
                horizon=h,
                beta_map=beta_map,
            )
            outcomes.append(
                FlowOutcome(
                    event_date=str(r["event_date"]),
                    stock_id=str(r["stock_id"]),
                    stock_name=str(r["stock_name"] or r["stock_id"]),
                    net_side=str(r["net_side"]),
                    consensus=str(r["consensus"]),
                    intent=str(r["intent"]),
                    conviction=float(r["conviction"]),
                    source_etfs=str(r["source_etfs"] or ""),
                    horizon=h,
                    capm_alpha_pct=alpha,
                    status=status,
                )
            )

    coverage: list[CoverageRow] = []
    expected = len(rows)
    for h in FLOW_HORIZONS:
        avail = sum(
            1
            for o in outcomes
            if o.horizon == h and o.status == "complete"
        )
        coverage.append(CoverageRow(horizon=h, expected=expected, available=avail))

    groups_net: list[GroupStats] = []
    groups_con: list[GroupStats] = []
    groups_int: list[GroupStats] = []
    for h in FLOW_HORIZONS:
        groups_net.extend(
            _group_stats(
                outcomes,
                key_fn=lambda o: o.net_side,
                horizon=h,
            )
        )
        groups_con.extend(
            _group_stats(
                outcomes,
                key_fn=lambda o: o.consensus,
                horizon=h,
            )
        )
        groups_int.extend(
            _group_stats(
                outcomes,
                key_fn=lambda o: _intent_label(o.intent),
                horizon=h,
            )
        )

    add_stats = [
        s
        for s in groups_net
        if s.label == "add" and s.horizon in FLOW_PRIMARY_HORIZONS
    ]
    random_baseline = _random_baseline(
        conn,
        event_dates=event_dates,
        events_by_date=events_by_date,
        horizons=FLOW_HORIZONS,
        beta_map=beta_map,
    )

    return FlowAttributionResult(
        flow_version=flow_version,
        window_start=event_dates[0],
        window_end=event_dates[-1],
        event_dates=event_dates,
        outcomes=outcomes,
        coverage=coverage,
        groups_net_side=groups_net,
        groups_consensus=groups_con,
        groups_intent=groups_int,
        random_baseline=random_baseline,
        boss_gate=_boss_gate(add_stats),
    )


def _fmt_pct(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v:+.2f}%"


def format_flow_section(
    result: FlowAttributionResult,
    conn: sqlite3.Connection | None = None,
) -> list[str]:
    if result.message:
        return [
            "## §0 ETF Flow Attribution（v0.3）",
            "",
            f"> {result.message}",
            "",
        ]

    lines = [
        "## §0 ETF Flow Attribution（v0.3）",
        "",
        f"> 只讀 `flow_events` · {result.flow_version} · 窗口 {result.window_start} ~ {result.window_end}",
        f"> event-days {len(result.event_dates)} · CAPM α = R−β·R_m · 基準 {BENCHMARK_CODE}",
        f"> Random Baseline：**Fixed Seed**（`BASELINE_RANDOM_SEED={BASELINE_RANDOM_SEED}` + event_date）",
        "",
        "### Boss Gate（H+3 / H+5 · 加碼組）",
        "",
        result.boss_gate,
        "",
        "### Coverage（Survivor Bias 警示）",
        "",
        "| Horizon | Expected | Available |",
        "|---------|----------|-----------|",
    ]
    for c in result.coverage:
        lines.append(f"| H+{c.horizon} | {c.expected} | {c.available} |")
    lines.append("")

    def _table(title: str, groups: list[GroupStats]) -> None:
        lines.extend([f"### {title}", ""])
        lines.append("| Horizon | Group | N | Mean CAPM α | Median | Hit% |")
        lines.append("|---------|-------|---|-------------|--------|------|")
        for g in sorted(groups, key=lambda x: (x.horizon, x.label)):
            hit = f"{g.hit_rate:.0f}%" if g.hit_rate is not None else "—"
            lines.append(
                f"| H+{g.horizon} | {g.label} | {g.n} | {_fmt_pct(g.mean_capm)} | "
                f"{_fmt_pct(g.median_capm)} | {hit} |"
            )
        lines.append("")

    _table("淨方向（net_side）", result.groups_net_side)
    _table("共識（consensus）", result.groups_consensus)
    _table("意圖（intent · TRIM 合併為 TRIM_ALL）", result.groups_intent)
    _table("Random Baseline（固定 Seed 對照）", result.random_baseline)

    if conn is not None:
        lines.extend(_chip_attribution_section(conn, result))
    return lines


def _chip_attribution_section(
    conn: sqlite3.Connection,
    result: FlowAttributionResult,
) -> list[str]:
    """最近加碼事件 × 融資／借券／當沖敘事（Sprint 2）。"""
    if not result.outcomes:
        return []
    adds = [
        o
        for o in result.outcomes
        if o.net_side == "add" and o.horizon == 1 and o.status == "complete"
    ]
    if not adds:
        return []
    seen: set[str] = set()
    rows: list[FlowOutcome] = []
    for o in reversed(adds):
        if o.stock_id in seen:
            continue
        seen.add(o.stock_id)
        rows.append(o)
        if len(rows) >= 10:
            break
    if not rows:
        return []

    section: list[str] = []
    table_rows: list[str] = []
    for o in rows:
        narrative = compose_chip_narrative(
            conn, o.stock_id, etf_net_side="add", trade_date=o.event_date
        )
        if not narrative:
            continue
        table_rows.append(
            f"| {o.event_date} | {o.stock_id} | {o.intent} | {narrative} |"
        )
    if not table_rows:
        return []
    section.extend(
        [
            "",
            "### 籌碼警示（最近加碼 · Gate 相關）",
            "",
            "| 事件日 | 代號 | 意圖 | 說明 |",
            "|--------|------|------|------|",
        ]
    )
    section.extend(table_rows)
    return section
