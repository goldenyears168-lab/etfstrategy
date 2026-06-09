#!/usr/bin/env python3
"""將 DB 內舊英文／自創 enum 遷移為 market_labels 中文存值。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from market_labels import (
    LEGACY_CHIP_TAG,
    LEGACY_ENTRY_SIGNAL,
    LEGACY_ENTRY_TAG,
    LEGACY_PM_BUCKET,
    LEGACY_WATCHLIST,
    normalize_chip_tag,
    normalize_entry_signal,
    normalize_entry_tag,
    normalize_pm_bucket,
    normalize_watchlist,
)
from stock_db import DEFAULT_DB_PATH, connect

# note 欄位：長字串優先替換
NOTE_REPLACEMENTS: tuple[tuple[str, str], ...] = (
    ("OVEREXTENDED+STRONG_TREND", "乖離過大＋量價齊揚"),
    ("OVEREXTENDED", "乖離過大"),
    ("STRONG_TREND", "量價齊揚"),
    ("SKIP_ENTRY", "暫不進場"),
    ("PULLBACK", "拉回"),
    ("BREAKOUT", "突破"),
    ("WAIT", "觀望"),
    ("RESEARCH", "觀察"),
    ("AVOID", "回避"),
    ("CANDIDATE", "候選"),
    (" · SKIP", " · 不列入"),
    (" · CANDIDATE", " · 候選"),
    (" · A", " · 首要觀察"),
    (" · B", " · 一般觀察"),
    ("三方共振", "外資、投信同步買超"),
    ("外資確認", "外資買超"),
    ("接刀警示", "外資賣超背離"),
    ("背離加碼", "籌碼背離"),
    ("同步減碼", "同步賣超"),
    ("法人法人中性", "法人中性"),
    ("法人法人法人中性", "法人中性"),
)


@dataclass
class MigrationStats:
    updated: dict[str, int] = field(default_factory=dict)

    def add(self, key: str, n: int = 1) -> None:
        self.updated[key] = self.updated.get(key, 0) + n

    def total(self) -> int:
        return sum(self.updated.values())


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def migrate_entry_tags_json(raw: str | None) -> tuple[str | None, bool]:
    if not raw or not raw.strip():
        return raw, False
    try:
        tags = json.loads(raw)
    except json.JSONDecodeError:
        return raw, False
    if not isinstance(tags, list):
        return raw, False
    new_tags = [normalize_entry_tag(str(t)) for t in tags]
    if new_tags == tags:
        return raw, False
    return json.dumps(new_tags, ensure_ascii=False), True


def migrate_metadata_json(raw: str | None) -> tuple[str | None, bool]:
    if not raw or not raw.strip():
        return raw, False
    try:
        meta = json.loads(raw)
    except json.JSONDecodeError:
        return raw, False
    if not isinstance(meta, dict):
        return raw, False
    changed = False
    if "entry_signal" in meta:
        new_sig = normalize_entry_signal(str(meta["entry_signal"]))
        if new_sig != meta["entry_signal"]:
            meta["entry_signal"] = new_sig
            changed = True
    if "chip_tag" in meta:
        new_chip = normalize_chip_tag(str(meta["chip_tag"]))
        if new_chip != meta["chip_tag"]:
            meta["chip_tag"] = new_chip
            changed = True
    if "entry_tags" in meta and isinstance(meta["entry_tags"], list):
        new_tags = [normalize_entry_tag(str(t)) for t in meta["entry_tags"]]
        if new_tags != meta["entry_tags"]:
            meta["entry_tags"] = new_tags
            changed = True
    if not changed:
        return raw, False
    return json.dumps(meta, ensure_ascii=False), True


def migrate_note_text(note: str | None) -> tuple[str | None, bool]:
    if not note:
        return note, False
    out = note
    for old, new in NOTE_REPLACEMENTS:
        out = out.replace(old, new)
    if out == note:
        return note, False
    return out, True


def _migrate_simple_column(
    conn: sqlite3.Connection,
    *,
    table: str,
    column: str,
    mapping: dict[str, str],
    stats: MigrationStats,
    dry_run: bool,
) -> None:
    if not _table_exists(conn, table):
        return
    for old, new in mapping.items():
        if old == new:
            continue
        cur = conn.execute(
            f"SELECT COUNT(*) AS n FROM {table} WHERE {column} = ?",
            (old,),
        ).fetchone()
        n = int(cur["n"]) if cur else 0
        if n <= 0:
            continue
        key = f"{table}.{column}:{old}→{new}"
        if not dry_run:
            conn.execute(
                f"UPDATE {table} SET {column} = ? WHERE {column} = ?",
                (new, old),
            )
        stats.add(key, n)


def migrate_market_labels(
    conn: sqlite3.Connection,
    *,
    dry_run: bool = False,
) -> MigrationStats:
    stats = MigrationStats()

    for table, col, mapping in (
        ("investment_scores", "watchlist", LEGACY_WATCHLIST),
        ("pm_watchlist", "watchlist", LEGACY_WATCHLIST),
        ("pm_watchlist", "entry_signal", LEGACY_ENTRY_SIGNAL),
        ("pm_watchlist", "chip_tag", LEGACY_CHIP_TAG),
        ("pm_watchlist", "pm_bucket", LEGACY_PM_BUCKET),
        ("portfolio_weights", "watchlist", LEGACY_WATCHLIST),
        ("portfolio_weights", "entry_signal", LEGACY_ENTRY_SIGNAL),
        ("portfolio_weights", "pm_bucket", LEGACY_PM_BUCKET),
    ):
        _migrate_simple_column(
            conn, table=table, column=col, mapping=mapping, stats=stats, dry_run=dry_run
        )

    for table in ("pm_watchlist", "portfolio_weights"):
        if not _table_exists(conn, table):
            continue
        rows = conn.execute(
            f"SELECT rowid, entry_tags_json FROM {table} WHERE entry_tags_json IS NOT NULL"
        ).fetchall()
        for row in rows:
            new_json, changed = migrate_entry_tags_json(row["entry_tags_json"])
            if not changed:
                continue
            stats.add(f"{table}.entry_tags_json", 1)
            if not dry_run:
                conn.execute(
                    f"UPDATE {table} SET entry_tags_json = ? WHERE rowid = ?",
                    (new_json, row["rowid"]),
                )

    if _table_exists(conn, "investment_scores"):
        rows = conn.execute(
            "SELECT rowid, metadata_json FROM investment_scores WHERE metadata_json IS NOT NULL"
        ).fetchall()
        for row in rows:
            new_json, changed = migrate_metadata_json(row["metadata_json"])
            if not changed:
                continue
            stats.add("investment_scores.metadata_json", 1)
            if not dry_run:
                conn.execute(
                    "UPDATE investment_scores SET metadata_json = ? WHERE rowid = ?",
                    (new_json, row["rowid"]),
                )

    for table in ("pm_watchlist", "portfolio_weights"):
        if not _table_exists(conn, table):
            continue
        rows = conn.execute(
            f"SELECT rowid, note FROM {table} WHERE note IS NOT NULL AND note != ''"
        ).fetchall()
        for row in rows:
            new_note, changed = migrate_note_text(row["note"])
            if not changed:
                continue
            stats.add(f"{table}.note", 1)
            if not dry_run:
                conn.execute(
                    f"UPDATE {table} SET note = ? WHERE rowid = ?",
                    (new_note, row["rowid"]),
                )

    if not dry_run and stats.total() > 0:
        conn.commit()
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(
        description="遷移 stocks.db 內觀察名單／價位型態／隔日等級／籌碼標籤至中文存值"
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只統計將更新的列數，不寫入",
    )
    args = parser.parse_args()
    conn = connect(args.db)
    try:
        stats = migrate_market_labels(conn, dry_run=args.dry_run)
    finally:
        conn.close()
    if not stats.updated:
        print("無需遷移（已是中文存值或表為空）")
        return 0
    mode = "（dry-run）" if args.dry_run else ""
    print(f"遷移完成{mode}，共 {stats.total()} 處更新：")
    for key, n in sorted(stats.updated.items()):
        print(f"  {key}: {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
