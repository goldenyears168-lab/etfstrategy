"""09:05 組合閘門 · intraday-exit-playbook v2 · dry-run advisory."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from order.morning_holdings_brief import CORE4
from stock_db import LOGS_DIR, PROJECT_ROOT, load_order_holdings_snapshot_rows
from stock_db.kbar import kbar_sql_minute
from stock_db.order_holdings import append_intraday_exit_log

_TZ = ZoneInfo("Asia/Taipei")
_GATE_MINUTE = "09:05"
_BENCHMARK_ID = "2330"
_SKIP_NO_SNAPSHOT = "no order_holdings_snapshot for session"
_SKIP_NO_KBAR = "no 09:05 kbar for 2330 after sync · gate inconclusive"


@dataclass(frozen=True)
class GateSymbolCheck:
    stock_id: str
    stock_name: str
    prev_close: float | None
    price_0905: float | None
    threshold: float | None
    triggered: bool
    role: str


@dataclass
class PortfolioGateResult:
    session_date: str
    checked_at: str
    poll_minute: str = _GATE_MINUTE
    portfolio_exit_mode: bool | None = None
    gate_a_count: int = 0
    gate_b_triggered: bool = False
    checks: list[GateSymbolCheck] = field(default_factory=list)
    skip_reason: str | None = None
    kbar_sync_n: int = 0
    dry_run: bool = True

    @property
    def evaluated(self) -> bool:
        return self.skip_reason is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_date": self.session_date,
            "checked_at": self.checked_at,
            "poll_minute": self.poll_minute,
            "portfolio_exit_mode": self.portfolio_exit_mode,
            "evaluated": self.evaluated,
            "gate_a_count": self.gate_a_count,
            "gate_b_triggered": self.gate_b_triggered,
            "kbar_sync_n": self.kbar_sync_n,
            "checks": [
                {
                    "stock_id": c.stock_id,
                    "stock_name": c.stock_name,
                    "prev_close": c.prev_close,
                    "price_0905": c.price_0905,
                    "threshold": c.threshold,
                    "triggered": c.triggered,
                    "role": c.role,
                }
                for c in self.checks
            ],
            "skip_reason": self.skip_reason,
            "dry_run": self.dry_run,
        }


def gate_watch_stock_ids(snapshot_rows: list[Any] | None = None) -> list[str]:
    """2330 + CORE4 · gate 判斷所需 5m K 標的。"""
    return sorted({_BENCHMARK_ID, *CORE4})


def sync_gate_kbar(
    conn: sqlite3.Connection,
    session_date: str,
    *,
    snapshot_rows: list[Any] | None = None,
) -> int:
    from rrg_mono_swap_accel_screen import sync_watchlist_kbar

    return sync_watchlist_kbar(
        conn,
        gate_watch_stock_ids(snapshot_rows),
        session_date,
        poll_minute=_GATE_MINUTE,
    )


def _price_at_minute(
    conn: sqlite3.Connection,
    stock_id: str,
    session: str,
    minute: str,
) -> float | None:
    row = conn.execute(
        """
        SELECT close FROM stock_kbar_5m
        WHERE stock_id = ? AND trade_date = ? AND minute <= ?
        ORDER BY minute DESC LIMIT 1
        """,
        (stock_id, session, kbar_sql_minute(minute)),
    ).fetchone()
    if row and row[0] is not None:
        return float(row[0])
    return None


def evaluate_portfolio_gate(
    conn: sqlite3.Connection,
    *,
    session_date: str,
    snapshot_rows: list[Any] | None = None,
    minute: str = _GATE_MINUTE,
    dry_run: bool = True,
) -> PortfolioGateResult:
    checked_at = datetime.now(_TZ).replace(microsecond=0).isoformat()
    result = PortfolioGateResult(
        session_date=session_date,
        checked_at=checked_at,
        poll_minute=minute,
        dry_run=dry_run,
    )
    rows = snapshot_rows or load_order_holdings_snapshot_rows(conn, session_date)
    if not rows:
        result.skip_reason = _SKIP_NO_SNAPSHOT
        result.portfolio_exit_mode = None
        return result

    by_id = {r.stock_id: r for r in rows}
    checks: list[GateSymbolCheck] = []

    bench = by_id.get(_BENCHMARK_ID)
    bench_prev = bench.prev_close if bench else None
    if bench_prev is None:
        from order.morning_holdings_brief import _prev_close

        bench_prev, _ = _prev_close(conn, _BENCHMARK_ID, as_of_session=session_date)
    bench_px = _price_at_minute(conn, _BENCHMARK_ID, session_date, minute)
    bench_threshold = round(bench_prev * 0.995, 2) if bench_prev else None
    gate_b = bool(
        bench_px is not None and bench_threshold is not None and bench_px <= bench_threshold
    )
    checks.append(
        GateSymbolCheck(
            stock_id=_BENCHMARK_ID,
            stock_name="台積電",
            prev_close=bench_prev,
            price_0905=bench_px,
            threshold=bench_threshold,
            triggered=gate_b,
            role="benchmark_b",
        )
    )

    gate_a = 0
    for sid in sorted(CORE4):
        row = by_id.get(sid)
        prev = row.prev_close if row else None
        if prev is None and sid in by_id:
            prev = by_id[sid].prev_close
        if prev is None:
            from order.morning_holdings_brief import _prev_close

            prev, _ = _prev_close(conn, sid, as_of_session=session_date)
        px = _price_at_minute(conn, sid, session_date, minute)
        threshold = round(prev * 0.98, 2) if prev else None
        triggered = bool(px is not None and threshold is not None and px <= threshold)
        if triggered:
            gate_a += 1
        name = row.stock_name if row else sid
        checks.append(
            GateSymbolCheck(
                stock_id=sid,
                stock_name=name,
                prev_close=prev,
                price_0905=px,
                threshold=threshold,
                triggered=triggered,
                role="core4_a·held" if triggered and sid in by_id else ("core4_a" if triggered else "core4"),
            )
        )

    result.checks = checks
    result.gate_a_count = gate_a
    result.gate_b_triggered = gate_b
    result.portfolio_exit_mode = gate_a >= 2 and gate_b
    return result


def evaluate_portfolio_gate_with_sync(
    conn: sqlite3.Connection,
    *,
    session_date: str,
    snapshot_rows: list[Any] | None = None,
    minute: str = _GATE_MINUTE,
    dry_run: bool = True,
    sync_kbar: bool = True,
) -> PortfolioGateResult:
    rows = snapshot_rows or load_order_holdings_snapshot_rows(conn, session_date)
    sync_n = 0
    if sync_kbar and rows:
        sync_n = sync_gate_kbar(conn, session_date, snapshot_rows=rows)
    result = evaluate_portfolio_gate(
        conn,
        session_date=session_date,
        snapshot_rows=rows,
        minute=minute,
        dry_run=dry_run,
    )
    result.kbar_sync_n = sync_n
    if result.skip_reason:
        result.portfolio_exit_mode = None
        return result

    bench_px = next((c.price_0905 for c in result.checks if c.stock_id == _BENCHMARK_ID), None)
    if bench_px is None:
        result.skip_reason = _SKIP_NO_KBAR
        result.portfolio_exit_mode = None
        result.gate_a_count = 0
        result.gate_b_triggered = False
        for check in result.checks:
            if check.triggered:
                pass  # checks immutable; re-build not needed for report
    return result


def _mode_label(result: PortfolioGateResult) -> str:
    if not result.evaluated:
        return "—（略過 · 無有效 09:05 價）"
    if result.portfolio_exit_mode:
        return "ON"
    return "OFF"


def format_portfolio_gate_report(result: PortfolioGateResult) -> str:
    lines = [
        f"# 09:05 組合閘門 · {result.session_date}",
        "",
        f"檢查時間：{result.checked_at} · minute {result.poll_minute}",
        f"**portfolio_exit_mode={_mode_label(result)}**",
        f"- 5m K sync：**{result.kbar_sync_n}** bars",
    ]
    if result.evaluated:
        lines.extend(
            [
                f"- CORE4 觸發 A：{result.gate_a_count} 檔（需 ≥2 · 不論是否持倉）",
                f"- 2330 觸發 B：{'是' if result.gate_b_triggered else '否'}",
            ]
        )
    if result.skip_reason:
        lines.append(f"- 略過：**{result.skip_reason}**")
        lines.append("- → S2 環境減碼今日不可用 · 請 09:10 後重跑 gate 或等 sell-radar")
    lines.append("")
    lines.append("| 代號 | 角色 | 09:05 | 閾值 | 觸發 |")
    lines.append("|------|------|------:|-----:|:----:|")
    for c in result.checks:
        px = f"{c.price_0905:.2f}" if c.price_0905 is not None else "—"
        th = f"{c.threshold:.2f}" if c.threshold is not None else "—"
        mark = "✓" if c.triggered else "—"
        lines.append(f"| {c.stock_id} | {c.role} | {px} | {th} | {mark} |")
    lines.append("")
    lines.append("advisory · dry-run · 不送單")
    return "\n".join(lines)


def default_gate_report_path(session_date: str) -> Any:
    stamp = session_date.replace("-", "")
    return PROJECT_ROOT / "reports" / "order" / "snapshots" / f"intraday_gate_{stamp}.md"


def default_gate_daily_log_path(session_date: str) -> Any:
    stamp = session_date.replace("-", "")
    return LOGS_DIR / "intraday" / f"intraday_exit_gate_{stamp}.log"


def gate_evaluation_final(conn: sqlite3.Connection, session_date: str) -> bool:
    """True when gate already produced gate_on/gate_off (not merely gate_skip)."""
    row = conn.execute(
        """
        SELECT 1 FROM order_intraday_exit_log
        WHERE trade_date = ? AND phase = 'gate'
          AND event IN ('gate_on', 'gate_off')
        LIMIT 1
        """,
        (session_date,),
    ).fetchone()
    return row is not None


def append_gate_daily_log(result: PortfolioGateResult, *, path: Any | None = None) -> Any:
    out = path or default_gate_daily_log_path(result.session_date)
    out.parent.mkdir(parents=True, exist_ok=True)
    text = format_portfolio_gate_report(result)
    prefix = "\n\n" if out.exists() and out.stat().st_size > 0 else ""
    with out.open("a", encoding="utf-8") as fh:
        fh.write(prefix + text)
        if not text.endswith("\n"):
            fh.write("\n")
    return out


def persist_portfolio_gate(conn: sqlite3.Connection, result: PortfolioGateResult) -> None:
    if not result.evaluated:
        append_intraday_exit_log(
            conn,
            trade_date=result.session_date,
            checked_at=result.checked_at,
            phase="gate",
            event="gate_skip",
            detail=result.to_dict(),
            dry_run=result.dry_run,
        )
        return
    append_intraday_exit_log(
        conn,
        trade_date=result.session_date,
        checked_at=result.checked_at,
        phase="gate",
        event="gate_on" if result.portfolio_exit_mode else "gate_off",
        detail=result.to_dict(),
        dry_run=result.dry_run,
    )
    for check in result.checks:
        if not check.triggered:
            continue
        append_intraday_exit_log(
            conn,
            trade_date=result.session_date,
            checked_at=result.checked_at,
            phase="gate",
            stock_id=check.stock_id,
            event=f"{check.role}_trigger",
            detail={
                "price_0905": check.price_0905,
                "threshold": check.threshold,
            },
            dry_run=result.dry_run,
        )


def write_portfolio_gate_report(result: PortfolioGateResult, path: Any | None = None) -> Any:
    out = path or default_gate_report_path(result.session_date)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(format_portfolio_gate_report(result), encoding="utf-8")
    return out
