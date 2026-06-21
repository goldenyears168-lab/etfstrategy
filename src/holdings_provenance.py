"""ETF 持股快照溯源：原始檔、抓取時間、同日版本比對。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from stock_db import DATA_DIR, insert_etf_holdings_fetch_log, load_latest_etf_holdings_fetch, utc_now_iso

RAW_ROOT = DATA_DIR / "holdings_raw"


def _resolve_raw_path(raw_path: str) -> Path:
    path = Path(raw_path)
    if path.is_file():
        return path
    return DATA_DIR.parent / raw_path


def _holdings_hash(holdings: list[dict]) -> str:
    payload = [
        {
            "stock_id": h["stock_id"],
            "shares": h["shares"],
            "weight_pct": h.get("weight_pct"),
            "amount": h.get("amount"),
        }
        for h in sorted(holdings, key=lambda x: x["stock_id"])
    ]
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def _compare_holdings(prev: list[dict], curr: list[dict]) -> tuple[int, int, int, str]:
    prev_map = {h["stock_id"]: h for h in prev}
    curr_map = {h["stock_id"]: h for h in curr}
    added = sorted(set(curr_map) - set(prev_map))
    removed = sorted(set(prev_map) - set(curr_map))
    changed = sorted(
        sid
        for sid in set(prev_map) & set(curr_map)
        if prev_map[sid]["shares"] != curr_map[sid]["shares"]
        or prev_map[sid].get("weight_pct") != curr_map[sid].get("weight_pct")
    )
    parts: list[str] = []
    if added:
        parts.append(f"+{len(added)}")
    if removed:
        parts.append(f"-{len(removed)}")
    if changed:
        parts.append(f"~{len(changed)}")
    summary = ", ".join(parts) if parts else "identical"
    return len(added), len(removed), len(changed), summary


def save_raw_snapshot(
    *,
    etf_code: str,
    snapshot_date: str,
    source: str,
    source_edit_at: str,
    nav: float | None,
    holdings: list[dict],
    fetched_at: str | None = None,
) -> tuple[Path, str]:
    fetched_at = fetched_at or utc_now_iso()
    safe_ts = fetched_at.replace(":", "").replace("+", "p")
    content_hash = _holdings_hash(holdings)
    out_dir = RAW_ROOT / etf_code / snapshot_date
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{safe_ts}_{content_hash[:12]}.json"
    body = {
        "etf_code": etf_code,
        "snapshot_date": snapshot_date,
        "source": source,
        "source_edit_at": source_edit_at,
        "nav": nav,
        "fetched_at": fetched_at,
        "holdings": holdings,
    }
    out_path.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
    return out_path, content_hash


def record_holdings_fetch(
    conn: sqlite3.Connection,
    *,
    etf_code: str,
    snapshot_date: str,
    source: str,
    source_edit_at: str,
    nav: float | None,
    holdings: list[dict],
    sync_status: str,
) -> int:
    fetched_at = utc_now_iso()
    raw_path, content_hash = save_raw_snapshot(
        etf_code=etf_code,
        snapshot_date=snapshot_date,
        source=source,
        source_edit_at=source_edit_at,
        nav=nav,
        holdings=holdings,
        fetched_at=fetched_at,
    )
    prev_row = load_latest_etf_holdings_fetch(conn, etf_code, snapshot_date)
    prev_fetch_id = int(prev_row["fetch_id"]) if prev_row else None
    rows_added = rows_removed = rows_changed = 0
    diff_summary = "first_fetch"
    if prev_row and prev_row["content_hash"] != content_hash:
        prev_path = _resolve_raw_path(str(prev_row["raw_path"]))
        prev_holdings_local: list[dict] | None = None
        if prev_path.is_file():
            prev_body = json.loads(prev_path.read_text(encoding="utf-8"))
            prev_holdings_local = prev_body.get("holdings") or []
        if prev_holdings_local is not None:
            rows_added, rows_removed, rows_changed, diff_summary = _compare_holdings(
                prev_holdings_local, holdings
            )
        sync_status = "version_diff"
    elif prev_row and prev_row["content_hash"] == content_hash:
        diff_summary = "unchanged"
    return insert_etf_holdings_fetch_log(
        conn,
        {
            "etf_code": etf_code,
            "snapshot_date": snapshot_date,
            "source": source,
            "fetched_at": fetched_at,
            "source_edit_at": source_edit_at,
            "holding_count": len(holdings),
            "nav": nav,
            "content_hash": content_hash,
            "raw_path": str(raw_path.relative_to(DATA_DIR.parent)),
            "sync_status": sync_status,
            "prev_fetch_id": prev_fetch_id,
            "diff_summary": diff_summary,
            "rows_added": rows_added,
            "rows_removed": rows_removed,
            "rows_changed": rows_changed,
        },
    )
