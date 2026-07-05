"""TW100 research data quality gates (Research layer · pre-Alpha158)."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from stock_universe import TW100_UNIVERSE_ID, load_universe_constituents
from universe_config import tw100_config

DEFAULT_MIN_BARS_WFA = 611  # 504 train + 100 valid + 7 embargo
DEFAULT_CHIP_LOOKBACK_DAYS = 400  # Phase 2 P2-1 target


def _chip_sparse_stocks(
    conn: sqlite3.Connection,
    stock_ids: list[str],
    *,
    window_start: str,
    window_end: str,
    min_rows: int = 200,
) -> list[str]:
    """TW100 stocks with sparse margin/lending/daytrade coverage (Phase 2 gate)."""
    if not stock_ids:
        return []
    ph = ",".join("?" * len(stock_ids))
    sparse: list[str] = []
    tables = ("stock_margin_daily", "stock_lending_daily", "stock_daytrade_daily")
    for sid in stock_ids:
        counts = [
            int(
                conn.execute(
                    f"""
                    SELECT COUNT(*) FROM {table}
                    WHERE stock_id = ? AND trade_date >= ? AND trade_date <= ?
                    """,
                    (sid, window_start, window_end),
                ).fetchone()[0]
            )
            for table in tables
        ]
        if min(counts) < min_rows:
            sparse.append(sid)
    return sparse


@dataclass(frozen=True)
class Tw100DataQualityReport:
    snapshot_date: str
    n_constituents: int
    min_bars_required: int
    bars_ok: bool
    short_history: tuple[str, ...]
    etf_leaks: tuple[str, ...]
    no_institutional: tuple[str, ...]
    chip_sparse: tuple[str, ...]
    amount_gaps: tuple[tuple[str, int], ...]
    passed: bool
    errors: tuple[str, ...]
    warnings: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_date": self.snapshot_date,
            "n_constituents": self.n_constituents,
            "min_bars_required": self.min_bars_required,
            "bars_ok": self.bars_ok,
            "short_history": list(self.short_history),
            "etf_leaks": list(self.etf_leaks),
            "no_institutional": list(self.no_institutional),
            "chip_sparse": list(self.chip_sparse),
            "amount_gaps": [{"stock_id": s, "null_days": n} for s, n in self.amount_gaps],
            "passed": self.passed,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
        }


def audit_tw100_data_quality(
    conn: sqlite3.Connection,
    *,
    snapshot_date: str | None = None,
    window_start: str | None = None,
    window_end: str | None = None,
    min_bars: int = DEFAULT_MIN_BARS_WFA,
) -> Tw100DataQualityReport:
    cfg = tw100_config()
    snap = snapshot_date or cfg["research_snapshot_date"]
    start = window_start or cfg["research_backfill_start"]
    end = window_end or snap
    exclude = set(cfg["exclude_stock_ids"])
    min_constituents = cfg["min_constituent_count"]

    meta = conn.execute(
        """
        SELECT constituent_count FROM universe_snapshot_meta
        WHERE universe_id = ? AND snapshot_date = ?
        """,
        (TW100_UNIVERSE_ID, snap),
    ).fetchone()
    watch = load_universe_constituents(conn, TW100_UNIVERSE_ID, snap)
    stock_ids = [w["stock_id"] for w in watch]

    errors: list[str] = []
    warnings: list[str] = []

    if not meta:
        errors.append(f"snapshot {snap} 不存在")
    elif int(meta["constituent_count"]) < min_constituents:
        errors.append(
            f"snapshot {snap} 成分數 {meta['constituent_count']} < min {min_constituents}"
        )
    if len(stock_ids) < min_constituents:
        errors.append(f"constituents 僅 {len(stock_ids)} 檔 < min {min_constituents}")

    etf_leaks = tuple(sorted(s for s in stock_ids if s in exclude))
    if etf_leaks:
        errors.append(f"ETF 混入 TW100: {', '.join(etf_leaks)}")

    short_history: list[str] = []
    amount_gaps: list[tuple[str, int]] = []
    if stock_ids:
        ph = ",".join("?" * len(stock_ids))
        bar_rows = conn.execute(
            f"""
            SELECT stock_id, COUNT(*) AS n, MIN(trade_date) AS bar_min
            FROM stock_daily_bars
            WHERE stock_id IN ({ph}) AND source = 'finmind'
              AND trade_date >= ? AND trade_date <= ?
            GROUP BY stock_id
            """,
            [*stock_ids, start, end],
        ).fetchall()
        bars_by_id = {str(r["stock_id"]): int(r["n"]) for r in bar_rows}
        bar_min_by_id = {str(r["stock_id"]): str(r["bar_min"]) for r in bar_rows if r["bar_min"]}
        for sid in stock_ids:
            n = bars_by_id.get(sid, 0)
            if n >= min_bars:
                continue
            first = bar_min_by_id.get(sid)
            if first and first > start:
                warnings.append(
                    f"{sid} 上市/資料起始 {first} · bars={n} < {min_bars}（完整可用歷史 · 不阻擋）"
                )
                continue
            short_history.append(sid)
            null_amt = conn.execute(
                """
                SELECT COUNT(*) FROM stock_daily_bars
                WHERE stock_id = ? AND source = 'finmind'
                  AND trade_date >= ? AND trade_date <= ?
                  AND amount IS NULL
                """,
                (sid, start, end),
            ).fetchone()[0]
            if null_amt:
                amount_gaps.append((sid, int(null_amt)))

    no_institutional: list[str] = []
    if stock_ids:
        ph = ",".join("?" * len(stock_ids))
        inst_ids = {
            str(r["stock_id"])
            for r in conn.execute(
                f"""
                SELECT DISTINCT stock_id FROM stock_institutional_daily
                WHERE stock_id IN ({ph}) AND source = 'finmind'
                """,
                stock_ids,
            ).fetchall()
        }
        for sid in stock_ids:
            if sid not in inst_ids:
                no_institutional.append(sid)

    if short_history:
        errors.append(
            f"{len(short_history)} 檔 bars < {min_bars}（{start}～{end}）: "
            + ", ".join(short_history[:8])
            + ("…" if len(short_history) > 8 else "")
        )

    null_adj = 0
    if stock_ids:
        ph = ",".join("?" * len(stock_ids))
        null_adj = int(
            conn.execute(
                f"""
                SELECT COUNT(*) FROM stock_daily_bars
                WHERE stock_id IN ({ph}) AND source = 'finmind'
                  AND trade_date >= ? AND trade_date <= ?
                  AND adj_close IS NULL
                """,
                [*stock_ids, start, end],
            ).fetchone()[0]
        )
        total_bars = int(
            conn.execute(
                f"""
                SELECT COUNT(*) FROM stock_daily_bars
                WHERE stock_id IN ({ph}) AND source = 'finmind'
                  AND trade_date >= ? AND trade_date <= ?
                """,
                [*stock_ids, start, end],
            ).fetchone()[0]
        )
        if total_bars and null_adj / total_bars > 0.01:
            errors.append(
                f"adj_close NULL {null_adj}/{total_bars} "
                f"({null_adj / total_bars:.1%}) · Alpha158 需要復權 · 重跑 sync_tw100_market"
            )
        elif null_adj:
            warnings.append(f"adj_close NULL {null_adj} 日（<{1}% · 可接受）")
    for sid, n in amount_gaps:
        warnings.append(f"{sid} amount NULL × {n} 日（窗內）")
    for sid in no_institutional:
        warnings.append(f"{sid} 無法人資料（FinMind 缺口 · 籌碼因子可能缺）")

    chip_sparse = tuple(
        _chip_sparse_stocks(
            conn,
            stock_ids,
            window_start=start,
            window_end=end,
            min_rows=min(200, max(50, min_bars // 3)),
        )
        if stock_ids
        else ()
    )
    for sid in chip_sparse:
        warnings.append(
            f"{sid} 籌碼表（margin/lending/daytrade）覆蓋稀疏 · Phase 2 前需 sync_stock_chip_daily"
        )

    bars_ok = not short_history
    passed = not errors
    return Tw100DataQualityReport(
        snapshot_date=snap,
        n_constituents=len(stock_ids),
        min_bars_required=min_bars,
        bars_ok=bars_ok,
        short_history=tuple(short_history),
        etf_leaks=etf_leaks,
        no_institutional=tuple(no_institutional),
        chip_sparse=chip_sparse,
        amount_gaps=tuple(amount_gaps),
        passed=passed,
        errors=tuple(errors),
        warnings=tuple(warnings),
    )
