#!/usr/bin/env python3
"""Build FULL 00:00-23:59 1m bars for the 5 August 2026 trading days
(08-03..08-07) that tx_1m_fullnight_cache_full.json doesn't cover (it stops
2026-07-31) and tx_1m_tick_built_582d deliberately truncates to 08:45-23:59
(no 00:00-04:59 night-session tail).

Found 2026-08-09: dropping that tail isn't just "missing some extra
trading window" -- it changes the warmup context (rolling rvol window,
regime-classification history) available when the day session opens at
08:45, materially altering PV8 classification and hence trades/PnL for the
REST of the day too (same-day check: 2026-07-17 truncated=+1042.1pt vs
full=-1341.5pt, same trade count, wildly different economics). Any replay
comparison needs full-day bars to be trustworthy.

Writes to bars.sqlite under a NEW source label (tx_1m_tick_built_fullnight_aug)
-- does not touch tx_1m_tick_built_582d or any other existing source.

Reuses the exact same raw tick files and resample logic as
tx_channel_build_582d_dataset.py, just with DAY_START=00:00:00 instead of
08:45:00 (NIGHT_END stays 23:59:59 -- the raw tick files themselves don't
carry data past 23:59:59 for a given calendar date; the following day's
00:00-04:59 lives under the NEXT date's own tick file, exactly like
tx_1m_fullnight_cache_full.json's convention).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd

TICK_DIR = Path("/Users/jackm4/goldenstocks-data/cache/tmf_channel/finmind_tx_tick_by_day")
DB_PATH = Path("/Users/jackm4/goldenstocks-data/cache/tmf_channel/bars.sqlite")
NEW_SOURCE = "tx_1m_tick_built_fullnight_aug"

FULL_START = "00:00:00"
FULL_END = "23:59:59"

DAYS = ["2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07"]


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


def main() -> None:
    conn = sqlite3.connect(str(DB_PATH))
    ensure_table(conn)
    insert_sql = (
        "INSERT OR REPLACE INTO bars (source, day, t, o, h, l, c, v, sess) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )
    for day in DAYS:
        path = TICK_DIR / f"{day}.json"
        ticks = load_front_month_ticks(path)
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
                (NEW_SOURCE, day, t, float(r["open"]), float(r["high"]),
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
