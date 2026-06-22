#!/usr/bin/env python3
"""ETF 日報：各檔 ETF 成分股持股變化（L1 shares 差分）。"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import date
from pathlib import Path
from typing import Any

from holdings_research import (
    build_etf_holdings_changes_block,
    fmt_ntd_short,
)
from market_benchmark import latest_trading_date
from project_config import ETF_CODES_HOLDINGS, ETF_CODES_LISTED, parse_etf_codes
from report_paths import REPORTS_DIR, daily_track_dir, ensure_daily_dir
from stock_db import DEFAULT_DB_PATH, connect, list_etf_snapshot_dates

STRATEGY_ID = "etf-daily"
ACTION_LABEL = {
    "新进": "新進",
    "加码": "加碼",
    "减码": "減碼",
    "出清": "出清",
    "不变": "不變",
}


def _sync_counts(
    conn: sqlite3.Connection,
    etf_codes: tuple[str, ...],
) -> tuple[int, int, list[str]]:
    synced = 0
    parts: list[str] = []
    for code in etf_codes:
        dates = list_etf_snapshot_dates(conn, code)
        if dates:
            synced += 1
            parts.append(f"{code} {dates[0]}")
    return synced, len(etf_codes), parts


def _holdings_sync_summary(
    conn: sqlite3.Connection,
    etf_codes: tuple[str, ...],
) -> dict[str, Any]:
    listed_codes = tuple(c for c in ETF_CODES_LISTED if c in etf_codes)
    optional_codes = tuple(c for c in etf_codes if c not in ETF_CODES_LISTED)
    listed_synced, listed_total, listed_parts = _sync_counts(conn, listed_codes)
    optional_synced, optional_total, optional_parts = (
        _sync_counts(conn, optional_codes) if optional_codes else (0, 0, [])
    )
    return {
        "listed_synced": listed_synced,
        "listed_total": listed_total or len(ETF_CODES_LISTED),
        "listed_parts": listed_parts,
        "optional_synced": optional_synced,
        "optional_total": optional_total,
        "optional_parts": optional_parts,
    }


def _format_change_line(change: dict) -> str:
    """Legacy helper for tests."""
    action = str(change.get("action") or "")
    label = ACTION_LABEL.get(action, action)
    flow_s = fmt_ntd_short(change.get("flow_ntd")) or "—"
    share = change.get("share_delta")
    share_s = f"{int(share):+d}" if share is not None else "—"
    return f"{change['stock_id']} {label} {share_s} flow {flow_s}"


def build_etf_daily_markdown(
    conn: sqlite3.Connection,
    etf_codes: tuple[str, ...],
    *,
    as_of: str | None = None,
) -> str:
    ref = as_of or latest_trading_date(conn) or date.today().isoformat()
    sync = _holdings_sync_summary(conn, etf_codes)
    blocks = build_etf_holdings_changes_block(conn, etf_codes, changed_only=True)

    changed_etfs: list[str] = []
    unchanged_etfs: list[str] = []
    skipped_etfs: list[str] = []

    lines: list[str] = [
        f"# ETF 日報 · {ref}",
        "",
        "## 摘要",
        "",
        (
            "- 持股同步（已掛牌）："
            f"**{sync['listed_synced']}/{sync['listed_total']}** 檔"
        ),
    ]
    if sync["listed_parts"]:
        lines.append(f"- 最新 snapshot：{', '.join(sync['listed_parts'])}")
    if sync["optional_total"]:
        opt_note = (
            f"{sync['optional_synced']}/{sync['optional_total']} 有 snapshot"
            if sync["optional_synced"]
            else "無 snapshot（掛牌前 optional · 不計入 VFP）"
        )
        codes = ", ".join(c for c in etf_codes if c not in ETF_CODES_LISTED)
        lines.append(f"- 掛牌前 optional（{codes}）：{opt_note}")
    lines.append("")

    for block in blocks:
        code = block["etf_code"]
        note = block.get("note")
        changes = block.get("changes") or []
        if note:
            skipped_etfs.append(f"{code}（{note}）")
            continue
        if not changes:
            unchanged_etfs.append(code)
        else:
            changed_etfs.append(code)

    if changed_etfs:
        lines.append(f"- **今日有成分變化**：{', '.join(changed_etfs)}")
    else:
        lines.append("- **今日無成分變化**（或官網尚未更新）")
    if unchanged_etfs:
        lines.append(f"- 無變化：{', '.join(unchanged_etfs)}")
    if skipped_etfs:
        lines.append(f"- 略過：{', '.join(skipped_etfs)}")
    lines.append("")

    lines.extend(["## 各 ETF 持股變化", ""])

    for block in blocks:
        code = block["etf_code"]
        prev_d = block.get("prev_date")
        curr_d = block.get("curr_date")
        note = block.get("note")
        changes = block.get("changes") or []

        if note:
            lines.extend([f"### {code}", "", f"_{note}_", "", ""])
            continue

        window = (
            f"{prev_d} → {curr_d}"
            if prev_d and curr_d
            else "—"
        )
        lines.append(f"### {code}（{window}）")
        lines.append("")

        if not changes:
            lines.extend(["無持股變化。", "", ""])
            continue

        lines.extend(
            [
                "| 代號 | 名稱 | 動作 | 股數差 | 權重差 | flow |",
                "|------|------|------|--------|--------|------|",
            ]
        )
        for ch in sorted(
            changes,
            key=lambda c: abs(float(c.get("flow_ntd") or 0)),
            reverse=True,
        ):
            action = str(ch.get("action") or "")
            label = ACTION_LABEL.get(action, action)
            share = ch.get("share_delta")
            share_s = f"{int(share):+d}" if share is not None else "—"
            wt = ch.get("weight_delta_pp")
            wt_s = f"{wt:+.2f}" if wt is not None else "—"
            flow_s = fmt_ntd_short(ch.get("flow_ntd")) or "—"
            lines.append(
                f"| {ch['stock_id']} | {ch.get('stock_name') or ''} | {label} | "
                f"{share_s} | {wt_s} | {flow_s} |"
            )
        lines.append("")

    lines.append(
        "_資料來源：`etf_holdings` shares 差分 · 官網未更新時該檔 Skip，無法事後補 snapshot。_"
    )
    lines.append("")
    return "\n".join(lines)


def write_etf_daily_reports(
    conn: sqlite3.Connection,
    etf_codes: tuple[str, ...],
    *,
    reports_dir: Path = REPORTS_DIR,
    track_dir: Path | None = None,
    as_of: str | None = None,
) -> list[Path]:
    ref = as_of or date.today().isoformat()
    stamp = ref.replace("-", "")
    text = build_etf_daily_markdown(conn, etf_codes, as_of=ref)
    ensure_daily_dir()
    out_track = track_dir or daily_track_dir(STRATEGY_ID)
    out_track.mkdir(parents=True, exist_ok=True)

    paths = [
        out_track / "daily_brief.md",
        reports_dir / f"{stamp}_etf_daily.md",
    ]
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return paths


def print_terminal_summary(
    conn: sqlite3.Connection,
    etf_codes: tuple[str, ...],
    *,
    as_of: str | None = None,
) -> None:
    ref = as_of or date.today().isoformat()
    sync = _holdings_sync_summary(conn, etf_codes)
    blocks = build_etf_holdings_changes_block(conn, etf_codes, changed_only=True)

    print("")
    print("==============================================")
    print(f"  ETF 日報 · {ref}")
    print("==============================================")
    print(
        f"  持股同步（已掛牌） {sync['listed_synced']}/{sync['listed_total']} 檔"
    )
    if sync["optional_total"] and not sync["optional_synced"]:
        codes = ", ".join(c for c in etf_codes if c not in ETF_CODES_LISTED)
        print(f"  掛牌前 optional {codes} — 無 snapshot")
    for block in blocks:
        code = block["etf_code"]
        note = block.get("note")
        changes = block.get("changes") or []
        if note:
            print(f"  {code}  —  {note}")
            continue
        if not changes:
            print(f"  {code}  無持股變化")
            continue
        adds = sum(1 for c in changes if c["action"] in ("新进", "加码"))
        reds = sum(1 for c in changes if c["action"] in ("减码", "出清"))
        top = sorted(
            changes,
            key=lambda c: abs(float(c.get("flow_ntd") or 0)),
            reverse=True,
        )[:3]
        top_s = " · ".join(
            f"{c['stock_id']} {fmt_ntd_short(c.get('flow_ntd')) or '—'}"
            for c in top
            if c.get("flow_ntd") is not None
        )
        tail = f"  top flow {top_s}" if top_s else ""
        print(f"  {code}  加{adds}減{reds}{tail}")

    rel = f"reports/daily/{STRATEGY_ID}/daily_brief.md"
    print(f"  完整報告 → {rel}")
    print("")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ETF 日報（成分股持股變化）")
    parser.add_argument("--etf-codes", default=None, help="Comma-separated ETF codes")
    parser.add_argument("--db-path", type=Path, default=None)
    parser.add_argument("--as-of", default=None, help="Report date YYYY-MM-DD")
    parser.add_argument("--write-reports", action="store_true")
    parser.add_argument("--human", action="store_true", help="Terminal summary")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    codes = parse_etf_codes(args.etf_codes, default=ETF_CODES_HOLDINGS)
    conn = connect(args.db_path or DEFAULT_DB_PATH)
    ref = args.as_of or date.today().isoformat()

    if args.write_reports:
        paths = write_etf_daily_reports(conn, codes, as_of=ref)
        if not args.quiet and not args.human:
            for p in paths:
                print(f"Wrote {p}")

    if args.human:
        print_terminal_summary(conn, codes, as_of=ref)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
