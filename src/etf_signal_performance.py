"""ETF 經理人訊號事後勝率（flow_events · 加碼 leg · 20 交易日）。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from flow_attribution import _outcome_date_after_k, _stock_close, return_pct
from project_config import FLOW_VERSION

DEFAULT_HORIZON_DAYS = 20
MIN_SAMPLE = 3


@dataclass(frozen=True)
class EtfSignalPerformance:
    etf_code: str
    horizon_days: int
    sample_n: int
    win_rate_pct: float | None
    mean_return_pct: float | None

    def to_dict(self) -> dict:
        return {
            "etf_code": self.etf_code,
            "horizon_days": self.horizon_days,
            "sample_n": self.sample_n,
            "win_rate_pct": self.win_rate_pct,
            "mean_return_pct": self.mean_return_pct,
        }


def _forward_return_pct(
    conn: sqlite3.Connection,
    stock_id: str,
    event_date: str,
    horizon: int,
) -> float | None:
    outcome = _outcome_date_after_k(conn, event_date, horizon)
    if outcome is None:
        return None
    c0 = _stock_close(conn, stock_id, event_date)
    c1 = _stock_close(conn, stock_id, outcome)
    if c0 is None or c1 is None:
        return None
    return return_pct(c0, c1)


def build_etf_signal_performance(
    conn: sqlite3.Connection,
    etf_codes: tuple[str, ...],
    *,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
    flow_version: str = FLOW_VERSION,
) -> list[EtfSignalPerformance]:
    """各 ETF 在歷史「加碼」事件上的 H+20 勝率（報酬>0）。"""
    try:
        rows = conn.execute(
            """
            SELECT event_date, stock_id, net_side, source_etfs
            FROM flow_events
            WHERE flow_version = ? AND net_side = 'add'
            ORDER BY event_date ASC
            """,
            (flow_version,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []

    returns_by_etf: dict[str, list[float]] = {c: [] for c in etf_codes}
    for row in rows:
        etfs = [c for c in str(row["source_etfs"] or "").split("|") if c]
        ret = _forward_return_pct(
            conn, str(row["stock_id"]), str(row["event_date"]), horizon_days
        )
        if ret is None:
            continue
        for etf in etfs:
            if etf in returns_by_etf:
                returns_by_etf[etf].append(ret)

    out: list[EtfSignalPerformance] = []
    for etf in etf_codes:
        vals = returns_by_etf.get(etf, [])
        n = len(vals)
        if n < MIN_SAMPLE:
            out.append(
                EtfSignalPerformance(
                    etf_code=etf,
                    horizon_days=horizon_days,
                    sample_n=n,
                    win_rate_pct=None,
                    mean_return_pct=round(sum(vals) / n, 2) if vals else None,
                )
            )
            continue
        wins = sum(1 for v in vals if v > 0)
        out.append(
            EtfSignalPerformance(
                etf_code=etf,
                horizon_days=horizon_days,
                sample_n=n,
                win_rate_pct=round(wins / n * 100.0, 1),
                mean_return_pct=round(sum(vals) / n, 2),
            )
        )
    out.sort(
        key=lambda r: (r.win_rate_pct is None, -(r.win_rate_pct or 0)),
    )
    return out
