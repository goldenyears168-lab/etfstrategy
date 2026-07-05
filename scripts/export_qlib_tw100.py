#!/usr/bin/env python3
"""Export TW100 stock_daily_bars → qlib directory layout (minimal OHLCV+amount+factor)."""

from __future__ import annotations

import argparse
import struct
import sys
from datetime import date
from pathlib import Path

import numpy as np

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from stock_db import DEFAULT_DB_PATH, PROJECT_ROOT, connect
from stock_universe import TW100_UNIVERSE_ID, load_universe_constituents

DEFAULT_OUT = PROJECT_ROOT / "data" / "qlib" / "tw100"
EXPORT_FIELDS = ("open", "high", "low", "close", "volume", "amount", "vwap", "factor")


def _write_bin(path: Path, values: list[float], start_index: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    arr = np.array(values, dtype="<f4")
    with path.open("wb") as f:
        f.write(struct.pack("<f", float(start_index)))
        f.write(arr.tobytes())


def export_qlib_tw100(
    db_path: Path,
    out_dir: Path,
    *,
    start: str | None = None,
    end: str | None = None,
    snapshot_date: str | None = None,
) -> dict[str, int]:
    conn = connect(db_path)
    try:
        watch = load_universe_constituents(conn, TW100_UNIVERSE_ID, snapshot_date)
        if not watch:
            raise RuntimeError("TW100 universe 為空：先跑 scripts/sync_tw100_market.py --refresh-universe")
        stock_ids = [w["stock_id"] for w in watch]

        date_rows = conn.execute(
            """
            SELECT DISTINCT trade_date
            FROM stock_daily_bars
            WHERE stock_id IN ({})
              AND source = 'finmind'
              AND (? IS NULL OR trade_date >= ?)
              AND (? IS NULL OR trade_date <= ?)
            ORDER BY trade_date
            """.format(",".join("?" * len(stock_ids))),
            [*stock_ids, start, start, end, end],
        ).fetchall()
        calendar = [str(r["trade_date"]) for r in date_rows]
        if not calendar:
            raise RuntimeError("無 K 線可匯出（確認已 backfill TW100）")
        cal_index = {d: i for i, d in enumerate(calendar)}

        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "calendars").mkdir(exist_ok=True)
        (out_dir / "instruments").mkdir(exist_ok=True)
        (out_dir / "features").mkdir(exist_ok=True)
        (out_dir / "calendars" / "day.txt").write_text("\n".join(calendar) + "\n", encoding="utf-8")

        inst_lines: list[str] = []
        field_counts: dict[str, int] = {f: 0 for f in EXPORT_FIELDS}

        for stock_id in stock_ids:
            rows = conn.execute(
                """
                SELECT trade_date, open, high, low, close, adj_close, volume, amount
                FROM stock_daily_bars
                WHERE stock_id = ? AND source = 'finmind'
                  AND trade_date >= ? AND trade_date <= ?
                ORDER BY trade_date
                """,
                (stock_id, calendar[0], calendar[-1]),
            ).fetchall()
            if not rows:
                continue
            by_date = {str(r["trade_date"]): r for r in rows}
            present = [d for d in calendar if d in by_date]
            if not present:
                continue
            start_i = cal_index[present[0]]
            end_i = cal_index[present[-1]]
            inst_lines.append(f"{stock_id}\t{present[0]}\t{present[-1]}")

            series: dict[str, list[float]] = {f: [] for f in EXPORT_FIELDS}
            for d in calendar[start_i : end_i + 1]:
                row = by_date.get(d)
                if row is None:
                    for f in EXPORT_FIELDS:
                        series[f].append(float("nan"))
                    continue
                close = float(row["close"])
                adj = row["adj_close"]
                vol = row["volume"]
                amt = row["amount"]
                o = row["open"]
                h = row["high"]
                lo = row["low"]
                ratio = (float(adj) / close) if adj is not None and close else None
                # Qlib Alpha158 expects adjusted OHLC in feature bins; factor recovers nominal.
                if ratio is not None:
                    series["open"].append(float(o) * ratio if o is not None else float("nan"))
                    series["high"].append(float(h) * ratio if h is not None else float("nan"))
                    series["low"].append(float(lo) * ratio if lo is not None else float("nan"))
                    series["close"].append(float(adj))
                    series["factor"].append(ratio)
                else:
                    series["open"].append(float(o) if o is not None else float("nan"))
                    series["high"].append(float(h) if h is not None else float("nan"))
                    series["low"].append(float(lo) if lo is not None else float("nan"))
                    series["close"].append(close)
                    series["factor"].append(1.0)
                series["volume"].append(float(vol) if vol is not None else float("nan"))
                series["amount"].append(float(amt) if amt is not None else float("nan"))
                if vol and amt:
                    series["vwap"].append(float(amt) / float(vol))
                else:
                    series["vwap"].append(float("nan"))

            for field, vals in series.items():
                _write_bin(
                    out_dir / "features" / stock_id.lower() / f"{field.lower()}.day.bin",
                    vals,
                    start_i,
                )
                field_counts[field] += 1

        (out_dir / "instruments" / "all.txt").write_text("\n".join(inst_lines) + "\n", encoding="utf-8")
        return {
            "calendar_days": len(calendar),
            "instruments": len(inst_lines),
            **field_counts,
        }
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="SQLite TW100 → data/qlib/tw100/")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--start", default=None, help="YYYY-MM-DD")
    parser.add_argument("--end", default=None, help="YYYY-MM-DD")
    parser.add_argument("--snapshot-date", default=None, help="TW100 成分 snapshot")
    args = parser.parse_args()

    stats = export_qlib_tw100(
        args.db,
        args.out,
        start=args.start,
        end=args.end,
        snapshot_date=args.snapshot_date,
    )
    print(
        f"qlib export → {args.out}: calendar={stats['calendar_days']} "
        f"instruments={stats['instruments']} fields={EXPORT_FIELDS}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
