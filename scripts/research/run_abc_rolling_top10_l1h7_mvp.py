#!/usr/bin/env python3
"""PIT rolling broker-branch Copytrade MVP.

Example:
  .venv/bin/python scripts/research/run_abc_rolling_top10_l1h7_mvp.py

The rank is refreshed every 40 trading days.  Each anchor uses only outcomes
whose H7 exit close was observable by that anchor, then freezes the selected
branches and per-branch rules until the next anchor.
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from statistics import mean, median
from typing import Iterable

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = ROOT / "data/stocks.db"
DEFAULT_UNIVERSE = (
    ROOT
    / "reports/research/branch-footprint-screen/_abc_local_all_universe.json"
)
DEFAULT_OUT_DIR = ROOT / "reports/research/branch-footprint-screen"

SOURCE = "finmind"
BENCHMARK = "IX0001"
WINDOW_START = "2024-07-01"
RERANK_DAYS = 40
MIN_EFFECTIVE_DAYS = 20
TOP_BRANCHES = 10
HOLD_DAYS = 7
COST_RATE = 0.003
ABS_GRID = (
    0.3e8,
    0.5e8,
    0.7e8,
    1e8,
    1.5e8,
    2e8,
    3e8,
    5e8,
    8e8,
    10e8,
    12e8,
    15e8,
)
SHARE_GRID = (0.12, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.50)
TARGET_N_LOW = 5
TARGET_N_HIGH = 10


@dataclass(frozen=True)
class TopBuy:
    branch_id: str
    trade_date: str
    stock_id: str
    buy_shares: float
    buy_amt: float
    buy_share: float


@dataclass(frozen=True)
class Outcome:
    signal_date: str
    stock_id: str
    entry_date: str
    exit_date: str
    entry_px: float
    exit_px: float
    benchmark_entry_px: float
    benchmark_exit_px: float
    stock_return_pct: float
    benchmark_return_pct: float
    excess_pct: float


@dataclass(frozen=True)
class BranchRule:
    branch_id: str
    branch_name: str
    tier: str
    effective_days: int
    abs_floor: float
    share_floor: float
    n: int
    median_excess_pct: float
    mean_excess_pct: float


def connect_readonly(path: Path, retries: int = 8) -> sqlite3.Connection:
    """Open a read-only DB, retrying transient rollback-journal locks."""
    uri = f"file:{path.resolve()}?mode=ro"
    last_error: Exception | None = None
    for attempt in range(retries):
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
            return conn
        except sqlite3.OperationalError as exc:
            last_error = exc
            if conn is not None:
                conn.close()
            time.sleep(min(2**attempt, 30))
    raise RuntimeError(f"could not open read-only database {path}: {last_error}")


def query_with_retry(
    conn: sqlite3.Connection,
    sql: str,
    params: Iterable[object] = (),
    retries: int = 6,
) -> list[sqlite3.Row]:
    """Execute a read query with bounded retries for a busy database."""
    for attempt in range(retries):
        try:
            return conn.execute(sql, tuple(params)).fetchall()
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt == retries - 1:
                raise
            time.sleep(min(2**attempt, 30))
    raise AssertionError("unreachable")


def load_universe(path: Path) -> list[dict[str, str]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"universe must be a JSON list: {path}")
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw:
        branch_id = str(item["id"])
        if branch_id in seen:
            continue
        seen.add(branch_id)
        out.append(
            {
                "id": branch_id,
                "name": str(item.get("name") or branch_id),
                "tier": str(item.get("tier") or ""),
            }
        )
    if not out:
        raise ValueError(f"empty branch universe: {path}")
    return out


def load_calendar(
    conn: sqlite3.Connection, start: str, end: str
) -> list[str]:
    rows = query_with_retry(
        conn,
        """
        SELECT trade_date
        FROM stock_daily_bars
        WHERE stock_id='2330' AND source=? AND trade_date BETWEEN ? AND ?
          AND open>0 AND close>0
        ORDER BY trade_date
        """,
        (SOURCE, start, end),
    )
    dates = [str(row[0]) for row in rows]
    if len(dates) < RERANK_DAYS + HOLD_DAYS + 1:
        raise RuntimeError(f"only {len(dates)} usable trading dates")
    return dates


def load_benchmark(
    conn: sqlite3.Connection, start: str, end: str
) -> dict[str, tuple[float, float]]:
    """Load IX0001 open/close, preferring Yahoo then TEJ then FinMind."""
    rows = query_with_retry(
        conn,
        """
        SELECT date, open, close
        FROM daily_bars
        WHERE code=? AND date BETWEEN ? AND ? AND open>0 AND close>0
        ORDER BY date,
          CASE source
            WHEN 'yahoo' THEN 0
            WHEN 'tej' THEN 1
            WHEN 'finmind' THEN 2
            ELSE 3
          END
        """,
        (BENCHMARK, start, end),
    )
    out: dict[str, tuple[float, float]] = {}
    for row in rows:
        out.setdefault(str(row[0]), (float(row[1]), float(row[2])))
    if not out:
        raise RuntimeError(f"no local {BENCHMARK} open/close rows")
    return out


def load_stock_ohlc(
    conn: sqlite3.Connection, start: str, end: str
) -> dict[tuple[str, str], tuple[float, float]]:
    rows = query_with_retry(
        conn,
        """
        SELECT stock_id, trade_date, open, close
        FROM stock_daily_bars
        WHERE source=? AND trade_date BETWEEN ? AND ?
          AND length(stock_id)=4
          AND stock_id GLOB '[0-9][0-9][0-9][0-9]'
          AND open>0 AND close>0
        """,
        (SOURCE, start, end),
    )
    return {
        (str(row[0]), str(row[1])): (float(row[2]), float(row[3]))
        for row in rows
    }


def load_top_buys(
    conn: sqlite3.Connection,
    branch_ids: list[str],
    start: str,
    end: str,
) -> dict[str, dict[str, TopBuy]]:
    """Precompute one priced amount-Top1 row per (branch, date)."""
    placeholders = ",".join("?" for _ in branch_ids)
    sql = f"""
        WITH priced AS (
          SELECT b.securities_trader_id AS branch_id,
                 b.trade_date,
                 b.stock_id,
                 b.buy AS buy_shares,
                 b.buy * p.close AS buy_amt
          FROM stock_broker_branch_daily b
          JOIN stock_daily_bars p
            ON p.stock_id=b.stock_id
           AND p.trade_date=b.trade_date
           AND p.source=?
          WHERE b.source=?
            AND b.securities_trader_id IN ({placeholders})
            AND b.trade_date BETWEEN ? AND ?
            AND b.buy>0
            AND p.close>0
            AND length(b.stock_id)=4
            AND b.stock_id GLOB '[0-9][0-9][0-9][0-9]'
        ),
        ranked AS (
          SELECT *,
                 SUM(buy_amt) OVER (
                   PARTITION BY branch_id, trade_date
                 ) AS day_buy_amt,
                 ROW_NUMBER() OVER (
                   PARTITION BY branch_id, trade_date
                   ORDER BY buy_amt DESC, stock_id
                 ) AS row_num
          FROM priced
        )
        SELECT branch_id, trade_date, stock_id, buy_shares, buy_amt,
               buy_amt/day_buy_amt AS buy_share
        FROM ranked
        WHERE row_num=1 AND day_buy_amt>0
        ORDER BY branch_id, trade_date
    """
    rows = query_with_retry(
        conn,
        sql,
        (SOURCE, SOURCE, *branch_ids, start, end),
    )
    out: dict[str, dict[str, TopBuy]] = defaultdict(dict)
    for row in rows:
        event = TopBuy(
            branch_id=str(row[0]),
            trade_date=str(row[1]),
            stock_id=str(row[2]),
            buy_shares=float(row[3]),
            buy_amt=float(row[4]),
            buy_share=float(row[5]),
        )
        out[event.branch_id][event.trade_date] = event
    return dict(out)


def outcome_for_event(
    event: TopBuy,
    calendar: list[str],
    date_index: dict[str, int],
    ohlc: dict[tuple[str, str], tuple[float, float]],
    benchmark: dict[str, tuple[float, float]],
) -> Outcome | None:
    signal_i = date_index.get(event.trade_date)
    if signal_i is None:
        return None
    entry_i = signal_i + 1
    exit_i = entry_i + HOLD_DAYS - 1
    if exit_i >= len(calendar):
        return None
    entry_date = calendar[entry_i]
    exit_date = calendar[exit_i]
    entry_row = ohlc.get((event.stock_id, entry_date))
    exit_row = ohlc.get((event.stock_id, exit_date))
    benchmark_entry = benchmark.get(entry_date)
    benchmark_exit = benchmark.get(exit_date)
    if not entry_row or not exit_row or not benchmark_entry or not benchmark_exit:
        return None
    entry_px = entry_row[0]
    exit_px = exit_row[1]
    benchmark_entry_px = benchmark_entry[0]
    benchmark_exit_px = benchmark_exit[1]
    stock_return = exit_px / entry_px - 1.0 - COST_RATE
    benchmark_return = benchmark_exit_px / benchmark_entry_px - 1.0
    return Outcome(
        signal_date=event.trade_date,
        stock_id=event.stock_id,
        entry_date=entry_date,
        exit_date=exit_date,
        entry_px=entry_px,
        exit_px=exit_px,
        benchmark_entry_px=benchmark_entry_px,
        benchmark_exit_px=benchmark_exit_px,
        stock_return_pct=stock_return * 100.0,
        benchmark_return_pct=benchmark_return * 100.0,
        excess_pct=(stock_return - benchmark_return) * 100.0,
    )


def distance_to_n_band(n: int) -> int:
    if n < TARGET_N_LOW:
        return TARGET_N_LOW - n
    if n > TARGET_N_HIGH:
        return n - TARGET_N_HIGH
    return 0


def choose_rule(
    branch_id: str,
    branch_name: str,
    tier: str,
    events: list[TopBuy],
    effective_days: int,
    outcomes: dict[tuple[str, str], Outcome | None],
    anchor: str,
) -> BranchRule | None:
    candidates: list[BranchRule] = []
    for abs_floor in ABS_GRID:
        for share_floor in SHARE_GRID:
            excess = []
            for event in events:
                if event.buy_amt < abs_floor or event.buy_share < share_floor:
                    continue
                outcome = outcomes.get((event.trade_date, event.stock_id))
                # PIT: an anchor may score only exits already known by its close.
                if outcome is None or outcome.exit_date > anchor:
                    continue
                excess.append(outcome.excess_pct)
            if not excess:
                continue
            candidates.append(
                BranchRule(
                    branch_id=branch_id,
                    branch_name=branch_name,
                    tier=tier,
                    effective_days=effective_days,
                    abs_floor=abs_floor,
                    share_floor=share_floor,
                    n=len(excess),
                    median_excess_pct=median(excess),
                    mean_excess_pct=mean(excess),
                )
            )
    if not candidates:
        return None
    in_band = [
        row
        for row in candidates
        if TARGET_N_LOW <= row.n <= TARGET_N_HIGH
    ]
    pool = in_band or candidates
    return max(
        pool,
        key=lambda row: (
            -distance_to_n_band(row.n),
            row.median_excess_pct,
            row.mean_excess_pct,
            row.n,
            row.abs_floor,
            row.share_floor,
        ),
    )


def build_epochs(
    universe: list[dict[str, str]],
    calendar: list[str],
    top_buys: dict[str, dict[str, TopBuy]],
    outcomes: dict[tuple[str, str], Outcome | None],
) -> list[dict[str, object]]:
    names = {row["id"]: row["name"] for row in universe}
    tiers = {row["id"]: row["tier"] for row in universe}
    anchors = list(range(RERANK_DAYS - 1, len(calendar), RERANK_DAYS))
    epochs: list[dict[str, object]] = []
    previous: set[str] = set()
    for epoch_no, anchor_i in enumerate(anchors, 1):
        anchor = calendar[anchor_i]
        window_dates = calendar[anchor_i - RERANK_DAYS + 1 : anchor_i + 1]
        window_set = set(window_dates)
        rules: list[BranchRule] = []
        for branch_id in names:
            dated = top_buys.get(branch_id, {})
            events = [dated[day] for day in window_dates if day in dated]
            effective_days = len(events)
            if effective_days < MIN_EFFECTIVE_DAYS:
                continue
            rule = choose_rule(
                branch_id,
                names[branch_id],
                tiers[branch_id],
                events,
                effective_days,
                outcomes,
                anchor,
            )
            if rule is not None:
                rules.append(rule)
        rules.sort(
            key=lambda row: (
                -row.median_excess_pct,
                -row.mean_excess_pct,
                -row.n,
                row.branch_id,
            )
        )
        top10 = rules[:TOP_BRANCHES]
        current = {row.branch_id for row in top10}
        epochs.append(
            {
                "epoch": epoch_no,
                "anchor": anchor,
                "window_start": window_dates[0],
                "window_end": window_dates[-1],
                "effective_calendar_days": len(window_set),
                "eligible_branches": len(rules),
                "top10": top10,
                "retained": len(previous & current) if previous else None,
                "added": len(current - previous) if previous else None,
                "dropped": len(previous - current) if previous else None,
            }
        )
        previous = current
    return epochs


def epoch_by_signal_date(
    epochs: list[dict[str, object]], calendar: list[str]
) -> dict[str, dict[str, object]]:
    """Map only post-anchor dates; anchor signals are conservatively skipped."""
    date_index = {day: i for i, day in enumerate(calendar)}
    out: dict[str, dict[str, object]] = {}
    for index, epoch in enumerate(epochs):
        anchor_i = date_index[str(epoch["anchor"])]
        next_anchor_i = (
            date_index[str(epochs[index + 1]["anchor"])]
            if index + 1 < len(epochs)
            else len(calendar)
        )
        for day in calendar[anchor_i + 1 : next_anchor_i]:
            out[day] = epoch
    return out


def simulate(
    calendar: list[str],
    epochs: list[dict[str, object]],
    top_buys: dict[str, dict[str, TopBuy]],
    outcomes: dict[tuple[str, str], Outcome | None],
) -> tuple[list[dict[str, object]], dict[str, float | int | None]]:
    date_index = {day: i for i, day in enumerate(calendar)}
    epoch_map = epoch_by_signal_date(epochs, calendar)
    trades: list[dict[str, object]] = []
    next_scan_i = RERANK_DAYS
    occupied_days: set[str] = set()
    candidate_days = 0
    no_signal_days = 0
    missing_future_days = 0
    for signal_i in range(RERANK_DAYS, len(calendar)):
        signal_date = calendar[signal_i]
        if signal_i < next_scan_i:
            continue
        epoch = epoch_map.get(signal_date)
        if epoch is None:
            continue
        candidate_days += 1
        ranked_rules = list(epoch["top10"])
        signals: list[tuple[int, BranchRule, TopBuy]] = []
        for rank, rule in enumerate(ranked_rules, 1):
            event = top_buys.get(rule.branch_id, {}).get(signal_date)
            if event is None:
                continue
            if event.buy_amt >= rule.abs_floor and event.buy_share >= rule.share_floor:
                signals.append((rank, rule, event))
        if not signals:
            no_signal_days += 1
            continue

        counts = Counter(event.stock_id for _, _, event in signals)
        max_consensus = max(counts.values())
        tied = {stock for stock, count in counts.items() if count == max_consensus}
        chosen_rank, chosen_rule, chosen_event = min(
            (row for row in signals if row[2].stock_id in tied),
            key=lambda row: row[0],
        )
        outcome = outcomes.get((signal_date, chosen_event.stock_id))
        if outcome is None:
            missing_future_days += 1
            continue

        entry_i = date_index[outcome.entry_date]
        exit_i = date_index[outcome.exit_date]
        occupied_days.update(calendar[entry_i : exit_i + 1])
        next_scan_i = exit_i + 1
        supporters = [
            rule.branch_name
            for _, rule, event in signals
            if event.stock_id == chosen_event.stock_id
        ]
        trades.append(
            {
                "epoch": epoch["epoch"],
                "anchor": epoch["anchor"],
                "signal_date": signal_date,
                "entry_date": outcome.entry_date,
                "exit_date": outcome.exit_date,
                "stock_id": chosen_event.stock_id,
                "consensus": max_consensus,
                "supporting_branches": "；".join(supporters),
                "tie_break_rank": chosen_rank,
                "tie_break_branch": chosen_rule.branch_name,
                "buy_amt_ntd": round(chosen_event.buy_amt, 2),
                "buy_share": round(chosen_event.buy_share, 6),
                "entry_open": outcome.entry_px,
                "exit_close": outcome.exit_px,
                "benchmark_entry_open": outcome.benchmark_entry_px,
                "benchmark_exit_close": outcome.benchmark_exit_px,
                "stock_return_pct": round(outcome.stock_return_pct, 6),
                "benchmark_return_pct": round(outcome.benchmark_return_pct, 6),
                "excess_pct": round(outcome.excess_pct, 6),
            }
        )

    equity = 1.0
    peak = 1.0
    max_drawdown = 0.0
    for trade in trades:
        equity *= 1.0 + float(trade["stock_return_pct"]) / 100.0
        peak = max(peak, equity)
        max_drawdown = min(max_drawdown, equity / peak - 1.0)
        trade["equity_after"] = round(equity, 8)

    eval_dates = set(calendar[RERANK_DAYS:])
    flat_days = len(eval_dates - occupied_days)
    stats: dict[str, float | int | None] = {
        "n_trades": len(trades),
        "candidate_scan_days": candidate_days,
        "no_signal_days": no_signal_days,
        "missing_future_days": missing_future_days,
        "evaluation_days": len(eval_dates),
        "occupied_days": len(eval_dates & occupied_days),
        "flat_days": flat_days,
        "flat_day_share_pct": (
            100.0 * flat_days / len(eval_dates) if eval_dates else None
        ),
        "equity_total_return_pct": (equity - 1.0) * 100.0,
        "equity_max_drawdown_pct": max_drawdown * 100.0,
    }
    if trades:
        returns = [float(row["stock_return_pct"]) for row in trades]
        excess = [float(row["excess_pct"]) for row in trades]
        stats.update(
            {
                "win_rate_pct": 100.0 * sum(value > 0 for value in returns) / len(returns),
                "excess_win_rate_pct": (
                    100.0 * sum(value > 0 for value in excess) / len(excess)
                ),
                "median_return_pct": median(returns),
                "mean_return_pct": mean(returns),
                "median_excess_pct": median(excess),
                "mean_excess_pct": mean(excess),
            }
        )
    else:
        stats.update(
            {
                "win_rate_pct": None,
                "excess_win_rate_pct": None,
                "median_return_pct": None,
                "mean_return_pct": None,
                "median_excess_pct": None,
                "mean_excess_pct": None,
            }
        )
    return trades, stats


def fmt(value: object, digits: int = 2) -> str:
    if value is None:
        return "—"
    return f"{float(value):.{digits}f}"


def write_trades(path: Path, trades: list[dict[str, object]]) -> None:
    fields = [
        "epoch",
        "anchor",
        "signal_date",
        "entry_date",
        "exit_date",
        "stock_id",
        "consensus",
        "supporting_branches",
        "tie_break_rank",
        "tie_break_branch",
        "buy_amt_ntd",
        "buy_share",
        "entry_open",
        "exit_close",
        "benchmark_entry_open",
        "benchmark_exit_close",
        "stock_return_pct",
        "benchmark_return_pct",
        "excess_pct",
        "equity_after",
    ]
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(trades)


def write_summary(
    path: Path,
    *,
    db_path: Path,
    universe_path: Path,
    universe: list[dict[str, str]],
    calendar: list[str],
    top_buys: dict[str, dict[str, TopBuy]],
    benchmark: dict[str, tuple[float, float]],
    epochs: list[dict[str, object]],
    trades: list[dict[str, object]],
    stats: dict[str, float | int | None],
    elapsed_seconds: float,
) -> None:
    churn = [int(row["retained"]) for row in epochs if row["retained"] is not None]
    average_retained = mean(churn) if churn else None
    complete_tape_branches = sum(
        len(top_buys.get(row["id"], {})) >= RERANK_DAYS for row in universe
    )
    db_note = (
        " (consistent local snapshot used because the source DB was busy)"
        if db_path.resolve() != DEFAULT_DB.resolve()
        else ""
    )
    lines = [
        "# ABC rolling Top10 L1H7 Copytrade MVP",
        "",
        "## Verdict",
        "",
        (
            f"Completed **{len(trades)} trades** over `{calendar[0]}`～"
            f"`{calendar[-1]}`. This is a Research layer（研究層）result, "
            "not an adopted strategy."
        ),
        "",
        "## Key stats",
        "",
        "| Metric | Result |",
        "|---|---:|",
        f"| Trades | {stats['n_trades']} |",
        f"| Gross win rate | {fmt(stats['win_rate_pct'])}% |",
        f"| Excess win rate vs IX0001 | {fmt(stats['excess_win_rate_pct'])}% |",
        f"| Median stock return (net 30bps) | {fmt(stats['median_return_pct'])}% |",
        f"| Mean stock return (net 30bps) | {fmt(stats['mean_return_pct'])}% |",
        f"| Median excess vs IX0001 | {fmt(stats['median_excess_pct'])}% |",
        f"| Mean excess vs IX0001 | {fmt(stats['mean_excess_pct'])}% |",
        f"| Compounded single-slot return | {fmt(stats['equity_total_return_pct'])}% |",
        f"| Realized-trade equity max DD (rough) | {fmt(stats['equity_max_drawdown_pct'])}% |",
        f"| Flat-day share | {fmt(stats['flat_day_share_pct'])}% |",
        "",
        "## Frozen protocol",
        "",
        f"- Universe: `{universe_path.relative_to(ROOT)}` · {len(universe)} A/B/C branches.",
        f"- Local data: `{db_path}`{db_note} · no network fetch; "
        f"{complete_tape_branches}/{len(universe)} "
        f"branches have at least {RERANK_DAYS} priced Top1 dates over the full window.",
        f"- Calendar: {len(calendar)} trading days; first {RERANK_DAYS} are warm-up.",
        "- Every 40 trading days, the anchor scores `[A-39, A]`; only H7 exits known "
        "by anchor close are eligible, preventing forward-outcome leakage.",
        "- Per branch: priced day-T amount Top1, `buy_amt = buy_shares × close`, "
        "and `buy_share = buy_amt / priced positive day buy amount`.",
        "- Adaptive rule grid: abs∩share; prefer N=5–10 and maximize median excess. "
        "If none are in-band, minimize distance to the band, then maximize median excess.",
        "- Branches with fewer than 20 effective priced Top1 days in an anchor window "
        "are excluded from that leaderboard.",
        "- Frozen Top10 consensus chooses the most-supported stock; ties use the "
        "highest-ranked branch.",
        "- Signal T → T+1 open; hold 7 trading days inclusive; exit close; 30bps "
        "stock cost; one slot; exit day cannot open another position.",
        "- Anchor-day signals are conservatively skipped because frozen rules apply "
        "only after the anchor.",
        "",
        "## Epoch turnover",
        "",
        f"- Epochs: {len(epochs)}; average retained Top10 after first epoch: "
        f"{fmt(average_retained, 1)}/10.",
        "",
        "| Epoch | Anchor | Eligible | Retained | Added | Top10 (rule N) |",
        "|---:|---|---:|---:|---:|---|",
    ]
    for epoch in epochs:
        labels = ", ".join(
            f"{row.branch_name}({row.n})" for row in epoch["top10"]
        )
        lines.append(
            f"| {epoch['epoch']} | {epoch['anchor']} | {epoch['eligible_branches']} | "
            f"{epoch['retained'] if epoch['retained'] is not None else '—'} | "
            f"{epoch['added'] if epoch['added'] is not None else '—'} | {labels} |"
        )
    lines.extend(
        [
            "",
            "## Coverage and caveats",
            "",
            f"- Benchmark coverage: {len(benchmark)}/{len(calendar)} calendar dates "
            "have usable IX0001 open/close; missing stock or benchmark execution rows "
            "are skipped.",
            f"- Flat-day calculation: {stats['flat_days']}/{stats['evaluation_days']} "
            "evaluation trading days; occupied days are entry through exit inclusive.",
            "- Max DD is computed on realized, compounded trade-end equity; it does "
            "not capture intratrade drawdown.",
            "- The same 40-day window selects both each branch's rule and the Top10. "
            "A 96-cell grid with N=5–10 is highly selection-optimistic despite PIT-safe "
            "timing; treat this as an MVP hypothesis generator, not OOS proof.",
            "- Tape coverage is uneven. The per-anchor 20-effective-day gate avoids "
            "ranking very thin branches but does not make coverage uniform.",
            "- Always-first-epoch-champion baseline was skipped to keep the MVP focused; "
            "a later frozen-rule baseline and nested OOS comparison remain useful.",
            "",
            "## Reproduction",
            "",
            "```bash",
            "PYTHONPATH=src .venv/bin/python "
            "scripts/research/run_abc_rolling_top10_l1h7_mvp.py",
            "```",
            "",
            f"Run time: {elapsed_seconds / 60.0:.1f} minutes.",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--universe", type=Path, default=DEFAULT_UNIVERSE)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--start", default=WINDOW_START)
    parser.add_argument("--end", default=None)
    args = parser.parse_args()

    started = time.monotonic()
    universe = load_universe(args.universe)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    conn = connect_readonly(args.db)
    try:
        if args.end is None:
            row = query_with_retry(
                conn,
                "SELECT MAX(trade_date) FROM stock_daily_bars WHERE source=?",
                (SOURCE,),
            )
            end = str(row[0][0])
        else:
            end = str(args.end)
        print(f"loading calendar and prices {args.start}..{end}", flush=True)
        calendar = load_calendar(conn, args.start, end)
        benchmark = load_benchmark(conn, calendar[0], calendar[-1])
        ohlc = load_stock_ohlc(conn, calendar[0], calendar[-1])
        print(
            f"loading priced Top1 for {len(universe)} branches "
            f"({len(ohlc):,} OHLC rows)",
            flush=True,
        )
        top_buys = load_top_buys(
            conn,
            [row["id"] for row in universe],
            calendar[0],
            calendar[-1],
        )
    finally:
        conn.close()

    date_index = {day: index for index, day in enumerate(calendar)}
    outcomes: dict[tuple[str, str], Outcome | None] = {}
    for dated in top_buys.values():
        for event in dated.values():
            key = (event.trade_date, event.stock_id)
            if key not in outcomes:
                outcomes[key] = outcome_for_event(
                    event, calendar, date_index, ohlc, benchmark
                )
    print(
        f"scoring anchors from {sum(len(v) for v in top_buys.values()):,} "
        "branch-date Top1 rows",
        flush=True,
    )
    epochs = build_epochs(universe, calendar, top_buys, outcomes)
    trades, stats = simulate(calendar, epochs, top_buys, outcomes)

    stem = "abc_rolling_top10_l1h7_mvp"
    trades_path = args.out_dir / f"{stem}_trades.csv"
    summary_path = args.out_dir / f"{stem}_summary.md"
    write_trades(trades_path, trades)
    write_summary(
        summary_path,
        db_path=args.db,
        universe_path=args.universe,
        universe=universe,
        calendar=calendar,
        top_buys=top_buys,
        benchmark=benchmark,
        epochs=epochs,
        trades=trades,
        stats=stats,
        elapsed_seconds=time.monotonic() - started,
    )
    print(f"wrote {trades_path}")
    print(f"wrote {summary_path}")
    print(
        "trades={n_trades} win={win}% med_excess={med}% "
        "mean_excess={avg}% max_dd={dd}% flat={flat}%".format(
            n_trades=stats["n_trades"],
            win=fmt(stats["win_rate_pct"]),
            med=fmt(stats["median_excess_pct"]),
            avg=fmt(stats["mean_excess_pct"]),
            dd=fmt(stats["equity_max_drawdown_pct"]),
            flat=fmt(stats["flat_day_share_pct"]),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
