#!/usr/bin/env python3
"""早盤即時期貨 gap（TX vs IX0001 昨收 · TE vs 前日 TE 結算）→ morning_risk_snapshot。"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from finmind_client import fetch_futures_snapshots
from stock_db import (
    DEFAULT_DB_PATH,
    connect,
    upsert_morning_risk_snapshot,
)

TW_SPOT_CODE = "IX0001"
TZ = ZoneInfo("Asia/Taipei")
DEFAULT_TX_SNAPSHOT_ID = "TXF"
DEFAULT_TE_SNAPSHOT_ID = "EXF"


def _snapshot_ids() -> tuple[str, str]:
    tx = os.environ.get("MORNING_TX_FUTURES_ID", DEFAULT_TX_SNAPSHOT_ID).strip() or DEFAULT_TX_SNAPSHOT_ID
    te = os.environ.get("MORNING_TE_FUTURES_ID", DEFAULT_TE_SNAPSHOT_ID).strip() or DEFAULT_TE_SNAPSHOT_ID
    return tx, te


def load_spot_prev_close(conn, trade_date: str) -> tuple[str | None, float | None]:
    row = conn.execute(
        """
        SELECT date, close
        FROM daily_bars
        WHERE code = ? AND date < ? AND close IS NOT NULL
        ORDER BY date DESC
        LIMIT 1
        """,
        (TW_SPOT_CODE, trade_date),
    ).fetchone()
    if row is None:
        return None, None
    return str(row[0]), float(row[1])


def _row_price(row: dict) -> float | None:
    for key in ("close", "price", "buy_price", "sell_price", "settlement_price"):
        val = row.get(key)
        if val is None or val == "":
            continue
        try:
            price = float(val)
        except (TypeError, ValueError):
            continue
        if price > 0:
            return price
    return None


def _matches_snapshot_id(row: dict, snapshot_id: str) -> bool:
    sid = snapshot_id.upper()
    for key in ("futures_id", "data_id"):
        val = str(row.get(key, "")).upper()
        if not val:
            continue
        if val == sid or val.startswith(sid):
            return True
    return False


def pick_snapshot_row(rows: list[dict], snapshot_id: str) -> dict | None:
    candidates = [r for r in rows if _matches_snapshot_id(r, snapshot_id)]
    if not candidates:
        return None
    with_price = [r for r in candidates if _row_price(r) is not None]
    pool = with_price or candidates
    return max(pool, key=lambda r: int(r.get("volume") or 0))


def load_prev_te_close(conn, trade_date: str) -> float | None:
    """前一日 TE 期貨參考價（tech_risk 隔夜快照）。"""
    try:
        row = conn.execute(
            """
            SELECT te_futures_price
            FROM tech_risk_daily_snapshot
            WHERE session_date <= ? AND te_futures_price IS NOT NULL
            ORDER BY session_date DESC
            LIMIT 1
            """,
            (trade_date,),
        ).fetchone()
    except Exception:
        return None
    if row is None or row[0] is None:
        return None
    return float(row[0])


def te_gap_from_row(te_row: dict | None, *, prev_te_close: float | None) -> float | None:
    if te_row is None:
        return None
    for key in ("spread_per", "spread", "change_percent", "change_rate"):
        val = te_row.get(key)
        if val is None or val == "":
            continue
        try:
            return round(float(val), 4)
        except (TypeError, ValueError):
            continue
    return gap_pct(_row_price(te_row), prev_te_close)


def gap_pct(price: float | None, prev_close: float | None) -> float | None:
    if price is None or prev_close is None or prev_close <= 0:
        return None
    return round((price - prev_close) / prev_close * 100.0, 4)


def build_morning_risk_row(
    conn,
    *,
    trade_date: str,
    captured_at: str,
    snapshot_rows: list[dict],
    tx_id: str,
    te_id: str,
    source: str = "finmind_snapshot",
    notes: str | None = None,
) -> dict | None:
    tw_spot_date, prev_close = load_spot_prev_close(conn, trade_date)
    if prev_close is None:
        return None

    tx_row = pick_snapshot_row(snapshot_rows, tx_id)
    te_row = pick_snapshot_row(snapshot_rows, te_id)
    tx_price = _row_price(tx_row) if tx_row else None
    te_price = _row_price(te_row) if te_row else None
    prev_te_close = load_prev_te_close(conn, trade_date)
    tx_gap = gap_pct(tx_price, prev_close)
    te_gap = te_gap_from_row(te_row, prev_te_close=prev_te_close)
    if te_gap is None:
        te_gap = gap_pct(te_price, prev_te_close)
    te_minus_tx = None
    if tx_gap is not None and te_gap is not None:
        te_minus_tx = round(te_gap - tx_gap, 4)

    note_parts: list[str] = []
    if tx_price is None:
        note_parts.append(f"{tx_id} 無 snapshot 價")
    if te_price is None:
        note_parts.append(f"{te_id} 無 snapshot 價")
    if notes:
        note_parts.append(notes)
    if tx_gap is None:
        return None

    return {
        "trade_date": trade_date,
        "captured_at": captured_at,
        "tw_spot_date": tw_spot_date,
        "tw_spot_code": TW_SPOT_CODE,
        "tw_spot_prev_close": prev_close,
        "tx_snapshot_id": tx_id,
        "tx_price": tx_price,
        "tx_contract_date": str(tx_row.get("contract_date", "")) if tx_row else None,
        "tx_gap_live_pct": tx_gap,
        "te_snapshot_id": te_id,
        "te_price": te_price,
        "te_contract_date": str(te_row.get("contract_date", "")) if te_row else None,
        "te_gap_live_pct": te_gap,
        "te_minus_tx_pct": te_minus_tx,
        "source": source,
        "notes": "; ".join(note_parts) if note_parts else None,
    }


def format_morning_risk_line(row) -> str:
    def pct(val) -> str:
        if val is None:
            return "—"
        return f"{float(val):+.2f}%"

    return (
        f"{row['trade_date']} {row['captured_at']}  "
        f"TX gap {pct(row['tx_gap_live_pct'])} @ {row['tx_price'] or '—'}  "
        f"TE gap {pct(row['te_gap_live_pct'])} @ {row['te_price'] or '—'}  "
        f"TE-TX {pct(row['te_minus_tx_pct'])}"
    )


def morning_radar_warnings(row) -> list[str]:
    warnings: list[str] = []
    tx_gap = row["tx_gap_live_pct"]
    if tx_gap is not None and abs(float(tx_gap)) >= 1.0:
        warnings.append(f"⚠ 台指 gap 偏大 ({float(tx_gap):+.2f}%) → 開盤波動風險")
    te_minus = row["te_minus_tx_pct"]
    if te_minus is not None and float(te_minus) >= 0.3:
        warnings.append(
            f"⚠ 電子期強於大盤 ({float(te_minus):+.2f}%) → 半導體開盤可能偏強"
        )
    elif te_minus is not None and float(te_minus) <= -0.3:
        warnings.append(
            f"⚠ 電子期弱於大盤 ({float(te_minus):+.2f}%) → 半導體開盤可能偏弱"
        )
    return warnings


def sync_morning_futures(
    db_path: Path,
    *,
    trade_date: str | None = None,
    dry_run: bool = False,
    quiet: bool = False,
) -> int:
    tx_id, te_id = _snapshot_ids()
    today = trade_date or datetime.now(TZ).date().isoformat()
    captured_at = datetime.now(TZ).replace(microsecond=0).isoformat()

    rows, err = fetch_futures_snapshots([tx_id, te_id])
    if err:
        raise RuntimeError(err)
    if not rows or pick_snapshot_row(rows, tx_id) is None:
        raise RuntimeError(f"FinMind 期貨 snapshot 無資料（{tx_id}）")

    conn = connect(db_path)
    try:
        payload = build_morning_risk_row(
            conn,
            trade_date=today,
            captured_at=captured_at,
            snapshot_rows=rows,
            tx_id=tx_id,
            te_id=te_id,
        )
        if payload is None:
            raise RuntimeError("無法組裝 morning_risk_snapshot（缺 IX0001 昨收或期貨價）")
        if dry_run:
            print(format_morning_risk_line(payload))
            return 1
        upsert_morning_risk_snapshot(conn, payload)
    finally:
        conn.close()

    if quiet:
        print(f"  morning_risk: {format_morning_risk_line(payload)}")
    else:
        print(f"  morning_risk 同步：{format_morning_risk_line(payload)}")
        print(
            f"    說明：TX gap = snapshot vs 前日 {TW_SPOT_CODE} 收盤；"
            "TE gap = snapshot spread 或 vs 前日 TE 結算"
        )
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="早盤即時 TX/TE gap → morning_risk_snapshot")
    parser.add_argument("--sync-db", action="store_true")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--trade-date", default=None, help="YYYY-MM-DD；預設今日（台北）")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    if not args.sync_db and not args.dry_run:
        parser.error("請加上 --sync-db 或 --dry-run")

    try:
        sync_morning_futures(
            args.db,
            trade_date=args.trade_date,
            dry_run=args.dry_run,
            quiet=args.quiet,
        )
    except RuntimeError as exc:
        print(f"  WARN morning_risk: {exc}", file=sys.stderr)
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"  WARN morning_risk: {exc}", file=sys.stderr)
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
