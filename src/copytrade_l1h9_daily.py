#!/usr/bin/env python3
"""00981A 跟單 L1H9 · 每日篩選 brief（新進／加碼訊號）。"""

from __future__ import annotations

import argparse
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

from copytrade.signals import (
    ADD_ACTIONS,
    CopytradeSignal,
    iter_copytrade_signals,
    snapshot_pairs,
)
from holdings_research import build_cross_etf_consensus
from market_benchmark import resolve_brief_trade_date
from project_config import ETF_CODES_HOLDINGS
from report_paths import REPORTS_DIR
from stock_db import DEFAULT_DB_PATH, connect, list_etf_snapshot_dates

STRATEGY_ID = "00981a-l1h9"
ETF_CODE = "00981A"
HOLD_DAYS = 9
N_SLOTS = 9

ACTION_ZH = {
    "新进": "新進",
    "加码": "加碼",
}


def _latest_signal_date(conn: sqlite3.Connection) -> str | None:
    dates = list_etf_snapshot_dates(conn, ETF_CODE)
    pairs = snapshot_pairs(dates, backfill=False)
    if not pairs:
        return None
    return pairs[0][1]


def signals_for_date(
    conn: sqlite3.Connection,
    trade_date: str,
) -> tuple[str, str, list[CopytradeSignal]]:
    dates = list_etf_snapshot_dates(conn, ETF_CODE)
    for score_date, outcome_date in snapshot_pairs(dates, backfill=True):
        if outcome_date != trade_date:
            continue
        out: list[CopytradeSignal] = []
        for sig in iter_copytrade_signals(
            conn,
            ETF_CODE,
            window_start=trade_date,
            window_end=trade_date,
        ):
            if sig.signal_date == trade_date and sig.action in ADD_ACTIONS:
                out.append(sig)
        out.sort(key=lambda s: (s.action != "新进", s.stock_id))
        return score_date, outcome_date, out
    return "", "", []


def _consensus_add_set(
    conn: sqlite3.Connection,
    etf_codes: tuple[str, ...],
) -> set[str]:
    return {
        row.stock_id
        for row in build_cross_etf_consensus(conn, etf_codes)
        if row.etf_add >= 2
    }


def build_copytrade_l1h9_markdown(
    conn: sqlite3.Connection,
    *,
    as_of: str | None = None,
    etf_codes: tuple[str, ...] = ETF_CODES_HOLDINGS,
) -> tuple[str, dict[str, Any]]:
    trade_date = as_of or _latest_signal_date(conn) or date.today().isoformat()
    score_date, outcome_date, signals = signals_for_date(conn, trade_date)
    consensus = _consensus_add_set(conn, etf_codes)

    lines: list[str] = [
        f"# ETF00981A 跟單策略 · {trade_date}",
        "",
        "## 摘要",
        "",
        f"- **策略**：00981A **新進／加碼** → 隔日開盤 · **持 {HOLD_DAYS} 交易日** · **{N_SLOTS} 槽**",
        f"- **訊號日**：{trade_date}",
    ]
    if score_date and outcome_date:
        lines.append(f"- **持股區間**：{score_date} → {outcome_date}")
    lines.append(f"- **異動檔數**：**{len(signals)}**")
    if signals:
        consensus_hits = [s for s in signals if s.stock_id in consensus]
        lines.append(
            f"- **跨 ETF 共識加碼（≥2 檔）**：{len(consensus_hits)} 檔"
        )
    else:
        lines.append("- **跨 ETF 共識加碼（≥2 檔）**：0 檔")
    lines.extend(["", "## 新進／加碼異動", ""])

    if not signals:
        if outcome_date and outcome_date != trade_date:
            lines.append(
                f"_最新 snapshot 訊號日為 **{outcome_date}**，與請求日 {trade_date} 不同；"
                "或今日無新進／加碼。_"
            )
        else:
            lines.append("_今日無符合 L1H9 的新進／加碼訊號。_")
    else:
        lines.extend(
            [
                "| 代號 | 名稱 | 動作 | 股數差 | 權重差 | 共識≥2 |",
                "|------|------|------|--------|--------|--------|",
            ]
        )
        for sig in signals:
            action = ACTION_ZH.get(sig.action, sig.action)
            share_s = f"{int(sig.share_delta):+d}"
            wt_s = (
                f"{sig.weight_delta:+.2f}%"
                if sig.weight_delta is not None
                else "—"
            )
            hit = "是" if sig.stock_id in consensus else ""
            lines.append(
                f"| {sig.stock_id} | {sig.stock_name} | {action} | "
                f"{share_s} | {wt_s} | {hit} |"
            )

    lines.extend(
        [
            "",
            "---",
            f"模組：`copytrade_l1h9_daily.py` · strategy `{STRATEGY_ID}` · **非下單建議**",
        ]
    )
    meta = {
        "strategy_id": STRATEGY_ID,
        "trade_date": trade_date,
        "signal_count": len(signals),
        "consensus_count": sum(1 for s in signals if s.stock_id in consensus),
    }
    return "\n".join(lines) + "\n", meta


def write_copytrade_l1h9_reports(
    conn: sqlite3.Connection,
    *,
    as_of: str | None = None,
    reports_dir: Path = REPORTS_DIR,
) -> list[Path]:
    md, meta = build_copytrade_l1h9_markdown(conn, as_of=as_of)
    trade_date = str(meta["trade_date"])
    stamp = trade_date.replace("-", "")
    reports_dir.mkdir(parents=True, exist_ok=True)
    dated = reports_dir / f"{stamp}_copytrade_l1h9_daily.md"
    latest = reports_dir / "copytrade_l1h9_daily.md"
    dated.write_text(md, encoding="utf-8")
    latest.write_text(md, encoding="utf-8")
    return [dated, latest]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="00981A L1H9 每日篩選 brief")
    parser.add_argument("--date", default="", help="YYYY-MM-DD（預設最新 snapshot 訊號日）")
    parser.add_argument("--write-reports", action="store_true", help="寫入 reports/daily/")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    conn = connect(DEFAULT_DB_PATH)
    try:
        as_of = args.date or None
        if args.write_reports and not as_of:
            as_of = resolve_brief_trade_date(conn, date.today()).isoformat()
        if args.write_reports:
            paths = write_copytrade_l1h9_reports(conn, as_of=as_of)
            if not args.quiet:
                for p in paths:
                    print(f"Wrote {p}")
        else:
            md, meta = build_copytrade_l1h9_markdown(conn, as_of=as_of)
            if not args.quiet:
                print(md)
            else:
                print(
                    f"{meta['trade_date']}: {meta['signal_count']} signals "
                    f"({meta['consensus_count']} consensus)"
                )
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
