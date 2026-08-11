#!/usr/bin/env python3
"""2026-08-11: companion to tx_tick_daily_accumulate.py -- once a new day's
raw ticks land in finmind_tx_tick_by_day/, resample it into full
00:00-23:59 1m bars under source=tx_1m_tick_built_fullnight_aug (same
resample logic as tx_channel_build_august_fullnight.py, which only covered
the fixed 08-03..08-07 range). Auto-discovers any day present in the raw
tick cache but missing from bars.sqlite under this source, so it stays
current as tx_tick_daily_accumulate.py adds new days. Idempotent
(INSERT OR REPLACE); safe to re-run.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd

TICK_DIR = Path("/Users/jackm4/goldenstocks-data/cache/tmf_channel/finmind_tx_tick_by_day")
DB_PATH = Path("/Users/jackm4/goldenstocks-data/cache/tmf_channel/bars.sqlite")
SOURCE = "tx_1m_tick_built_fullnight_aug"
FULL_START = "00:00:00"
FULL_END = "23:59:59"
EARLIEST_DAY = "2026-08-03"  # this source's established start; older days live in other sources


def load_front_month_ticks(path: Path) -> pd.DataFrame | None:
    rows = pd.read_json(path)
    if rows.empty or "contract_date" not in rows.columns:
        return None
    df = rows[~rows["contract_date"].astype(str).str.contains("/")]
    if df.empty:
        return None
    front = df["contract_date"].value_counts().idxmax()
    front_df = df[df["contract_date"] == front].copy()
    front_df["dt"] = pd.to_datetime(front_df["date"])
    return front_df.sort_values("dt")[["dt", "price", "volume"]]


def resample_full(ticks: pd.DataFrame) -> pd.DataFrame:
    s = ticks.set_index("dt").between_time(FULL_START, FULL_END)
    if s.empty:
        return pd.DataFrame()
    ohlc = s["price"].resample("1min").ohlc()
    vol = s["volume"].resample("1min").sum()
    return ohlc.join(vol.rename("volume")).dropna(subset=["open"])


def sess_for_hhmm(hm: str) -> str:
    return "night" if (hm >= "15:00" or hm < "05:00") else "day"


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS bars (
          source TEXT NOT NULL, day TEXT NOT NULL, t TEXT NOT NULL,
          o REAL, h REAL, l REAL, c REAL, v REAL, sess TEXT,
          PRIMARY KEY (source, day, t)
        )"""
    )


def already_built(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT DISTINCT day FROM bars WHERE source=?", (SOURCE,)).fetchall()
    return {d for (d,) in rows}


def main() -> None:
    conn = sqlite3.connect(str(DB_PATH))
    ensure_table(conn)
    built = already_built(conn)
    available = sorted(p.stem for p in TICK_DIR.glob("*.json") if p.stem >= EARLIEST_DAY)
    todo = [d for d in available if d not in built]
    if not todo:
        print("no new days to build -- bars.sqlite is current with the tick cache.")
        conn.close()
        return

    insert_sql = (
        "INSERT OR REPLACE INTO bars (source, day, t, o, h, l, c, v, sess) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )
    for day in todo:
        ticks = load_front_month_ticks(TICK_DIR / f"{day}.json")
        if ticks is None or ticks.empty:
            print(f"{day}: no usable ticks, skipped")
            continue
        bars = resample_full(ticks)
        if bars.empty:
            print(f"{day}: resample produced 0 bars, skipped")
            continue
        rows = []
        for ts, r in bars.iterrows():
            t = ts.strftime("%H:%M")
            rows.append(
                (SOURCE, day, t, float(r["open"]), float(r["high"]),
                 float(r["low"]), float(r["close"]), float(r["volume"]),
                 sess_for_hhmm(t))
            )
        conn.executemany(insert_sql, rows)
        conn.commit()
        print(f"{day}: wrote {len(rows)} bars ({rows[0][2]}..{rows[-1][2]})")
    conn.close()
    print("DONE")


if __name__ == "__main__":
    main()
