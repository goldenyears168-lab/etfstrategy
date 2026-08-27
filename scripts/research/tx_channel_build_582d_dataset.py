#!/usr/bin/env python3
"""從原始 tick 資料重建 1 分K，把 tx-donchian-regime 研究線用的樣本從 83 天（
tx_1m_daynight_cache.json）擴充到全部可用的 tick 快取天數（~582 個交易日，
/Users/jackm4/goldenstocks-data/cache/tmf_channel/finmind_tx_tick_by_day/*.json）。

Session 邊界刻意跟既有 `tx_1m_daynight_cache.json` 完全一致（日盤 08:45:00-13:44:59、
夜盤 15:00:00-23:59:59，不跨到隔天凌晨）——這條研究線 16 輪全部的 window 大小／ATR 門檻／
5摺切分都是相對這個 session 長度（日盤300分鐘、夜盤540分鐘）校準的，換 session 定義會讓
所有既有結論失去對照基準。原始 tick 裡確實有 00:00-04:59 的成交（已驗證，例如
2026-04-02 這天有 11,743 筆），這裡刻意捨棄，不是遺漏。

近月主力合約判定：同一天 `contract_date` 可能有多個到期月同時掛牌（含價差組合，如
"202604/202609"，先濾掉），流動性最好（tick 筆數最多）的月份視為主力。

寫入既有的 production cache `bars.sqlite` 的 `bars` 表，用新 source 標籤
`tx_1m_tick_built_582d`，不動任何既有 source 的資料。可重複執行（INSERT OR REPLACE）。
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pandas as pd

TICK_DIR = Path("/Users/jackm4/goldenstocks-data/cache/tmf_channel/finmind_tx_tick_by_day")
DB_PATH = Path("/Users/jackm4/goldenstocks-data/cache/tmf_channel/bars.sqlite")
NEW_SOURCE = "tx_1m_tick_built_582d"

DAY_START = "08:45:00"
DAY_END = "13:44:59"
NIGHT_START = "15:00:00"
NIGHT_END = "23:59:59"

# 單日 tick 筆數若低於此門檻，視為資料品質可疑（例如假日誤留檔、資料源缺漏），仍寫入但在
# 報告中列出供人工複核。
LOW_TICK_WARN_THRESHOLD = 2000


def load_front_month_ticks(path: Path) -> tuple[pd.DataFrame | None, dict]:
    """讀單一 tick 檔案，濾掉價差組合，回傳流動性最好（tick 筆數最多）月份的 ticks。"""
    try:
        rows = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        return None, {"error": f"read/parse failed: {exc}"}

    if not rows:
        return None, {"error": "empty file"}

    df = pd.DataFrame(rows)
    if "contract_date" not in df.columns or df.empty:
        return None, {"error": "missing contract_date / empty"}

    n_raw = len(df)
    df = df[~df["contract_date"].astype(str).str.contains("/")]
    if df.empty:
        return None, {"error": "all rows are spread contracts", "n_raw": n_raw}

    counts = df["contract_date"].value_counts()
    front = counts.idxmax()
    front_df = df[df["contract_date"] == front].copy()
    front_df["dt"] = pd.to_datetime(front_df["date"])
    front_df = front_df.sort_values("dt")

    meta = {
        "n_raw": n_raw,
        "n_outright": int(len(df)),
        "front_contract": str(front),
        "n_front": int(len(front_df)),
        "n_other_contracts": int(len(counts) - 1),
    }
    return front_df[["dt", "price", "volume"]], meta


def resample_session(ticks: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    """把一個 session 窗口內的 tick resample 成 1 分K（Open=首筆 High=最高 Low=最低
    Close=末筆 Volume=加總）。"""
    s = ticks.set_index("dt")
    s = s.between_time(start, end)
    if s.empty:
        return pd.DataFrame()
    ohlc = s["price"].resample("1min").ohlc()
    vol = s["volume"].resample("1min").sum()
    out = ohlc.join(vol.rename("volume")).dropna(subset=["open"])
    return out


def bars_to_rows(bars: pd.DataFrame, day: str, sess: str, source: str) -> list[tuple]:
    rows = []
    for ts, r in bars.iterrows():
        t = ts.strftime("%H:%M")
        rows.append((source, day, t, float(r["open"]), float(r["high"]), float(r["low"]),
                     float(r["close"]), float(r["volume"]), sess))
    return rows


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS bars (
          source TEXT NOT NULL,
          day TEXT NOT NULL,
          t TEXT NOT NULL,
          o REAL, h REAL, l REAL, c REAL, v REAL,
          sess TEXT,
          PRIMARY KEY (source, day, t)
        )"""
    )


def main() -> None:
    files = sorted(TICK_DIR.glob("*.json"))
    print(f"tick 檔案總數: {len(files)}  ({files[0].stem} ~ {files[-1].stem})")

    conn = sqlite3.connect(str(DB_PATH))
    ensure_table(conn)

    n_written_days = 0
    n_skipped = 0
    n_day_bars_total = 0
    n_night_bars_total = 0
    low_tick_days = []
    skipped_days = []
    no_night_days = []
    no_day_days = []

    insert_sql = (
        "INSERT OR REPLACE INTO bars (source, day, t, o, h, l, c, v, sess) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )

    for i, path in enumerate(files):
        day = path.stem
        ticks, meta = load_front_month_ticks(path)
        if ticks is None:
            n_skipped += 1
            skipped_days.append({"day": day, **meta})
            continue

        day_bars = resample_session(ticks, DAY_START, DAY_END)
        night_bars = resample_session(ticks, NIGHT_START, NIGHT_END)

        if day_bars.empty and night_bars.empty:
            n_skipped += 1
            skipped_days.append({"day": day, "error": "no ticks in either session window", **meta})
            continue

        rows: list[tuple] = []
        if not day_bars.empty:
            rows.extend(bars_to_rows(day_bars, day, "day", NEW_SOURCE))
            n_day_bars_total += len(day_bars)
        else:
            no_day_days.append(day)
        if not night_bars.empty:
            rows.extend(bars_to_rows(night_bars, day, "night", NEW_SOURCE))
            n_night_bars_total += len(night_bars)
        else:
            no_night_days.append(day)

        conn.executemany(insert_sql, rows)
        n_written_days += 1

        if meta.get("n_front", 0) < LOW_TICK_WARN_THRESHOLD:
            low_tick_days.append({"day": day, **meta})

        if (i + 1) % 100 == 0:
            conn.commit()
            print(f"  ...{i + 1}/{len(files)} 檔處理完（已寫入 {n_written_days} 天）")

    conn.commit()
    conn.close()

    print("\n=== 建置完成 ===")
    print(f"source: {NEW_SOURCE}")
    print(f"寫入天數: {n_written_days}  跳過: {n_skipped}")
    if n_written_days:
        print(f"日盤 bar 總數: {n_day_bars_total}  平均每天 {n_day_bars_total / n_written_days:.1f}")
        print(f"夜盤 bar 總數: {n_night_bars_total}  平均每天 {n_night_bars_total / n_written_days:.1f}")

    if skipped_days:
        print(f"\n跳過的檔案（{len(skipped_days)}）:")
        for d in skipped_days[:30]:
            print(f"  {d}")
        if len(skipped_days) > 30:
            print(f"  ...其餘 {len(skipped_days) - 30} 筆省略")

    if no_day_days:
        print(f"\n無日盤資料的天數（{len(no_day_days)}）: {no_day_days[:20]}"
              + (" ...(截斷)" if len(no_day_days) > 20 else ""))
    if no_night_days:
        print(f"\n無夜盤資料的天數（{len(no_night_days)}）: {no_night_days[:20]}"
              + (" ...(截斷)" if len(no_night_days) > 20 else ""))

    if low_tick_days:
        print(f"\n近月主力合約 tick 筆數偏低（<{LOW_TICK_WARN_THRESHOLD}，可能資料品質異常，"
              f"{len(low_tick_days)} 天）:")
        for d in low_tick_days[:30]:
            print(f"  {d}")
        if len(low_tick_days) > 30:
            print(f"  ...其餘 {len(low_tick_days) - 30} 筆省略")

    out_dir = Path(__file__).resolve().parents[2] / "reports" / "research" / "tx-donchian-regime"
    out_dir.mkdir(parents=True, exist_ok=True)
    summary_path = out_dir / "build_582d_dataset_summary.json"
    summary_path.write_text(json.dumps(dict(
        source=NEW_SOURCE,
        n_files=len(files),
        n_written_days=n_written_days,
        n_skipped=n_skipped,
        n_day_bars_total=n_day_bars_total,
        n_night_bars_total=n_night_bars_total,
        skipped_days=skipped_days,
        no_day_days=no_day_days,
        no_night_days=no_night_days,
        low_tick_days=low_tick_days,
    ), indent=2, ensure_ascii=False, default=str))
    print(f"\nsaved summary: {summary_path}")


if __name__ == "__main__":
    sys.exit(main())
