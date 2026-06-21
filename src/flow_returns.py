"""Flow 事件報酬／Alpha 共用計算（flow_event_legs）。"""

from __future__ import annotations

import sqlite3

from holdings_research import TW_SPOT_CODE
from investment_themes import stock_theme
from project_config import FLOW_HORIZONS

BENCHMARK_CODE = TW_SPOT_CODE
DEFAULT_BETA = 1.0

THEME_TO_SECTOR: dict[str, str] = {
    "AI_SEMIS": "半導體",
    "ADV_PACKAGING": "半導體",
    "AI_SERVER": "電子",
    "AI_COOLING": "電子",
    "AI_POWER": "電子",
    "AI_NETWORK": "電子",
    "SEMI_EQUIP": "半導體設備",
    "MEMORY": "半導體",
    "MOBILE_OPTICS": "光電",
    "CYCLE_CHEM": "化學",
    "CYCLE_STEEL": "鋼鐵",
    "FINANCIAL": "金融",
    "DEFENSIVE_TELCO": "電信",
    "CONSUMER": "消費",
    "HARDWARE": "電子",
    "SHIPPING": "航運",
    "UNKNOWN": "其他",
}


def capm_alpha_pct(ret_pct: float, bench_pct: float, beta: float) -> float:
    return ret_pct - beta * bench_pct


def return_pct(price_t: float, price_t1: float) -> float:
    if price_t <= 0:
        return 0.0
    return (price_t1 - price_t) / price_t * 100.0


def sector_for_stock(stock_id: str) -> str:
    theme = stock_theme(stock_id)
    return THEME_TO_SECTOR.get(theme, "其他")


def _beta_for_stock(beta_map: dict[str, sqlite3.Row], stock_id: str) -> float:
    row = beta_map.get(stock_id)
    if row is None or row["beta"] is None:
        return DEFAULT_BETA
    return float(row["beta"])


def _bench_close(conn: sqlite3.Connection, trade_date: str) -> float | None:
    row = conn.execute(
        """
        SELECT close FROM daily_bars
        WHERE code = ? AND date = ?
        ORDER BY CASE source WHEN 'tej' THEN 0 WHEN 'yahoo' THEN 1 ELSE 2 END
        LIMIT 1
        """,
        (BENCHMARK_CODE, trade_date),
    ).fetchone()
    if row is None:
        return None
    return float(row["close"])


def stock_close(conn: sqlite3.Connection, stock_id: str, trade_date: str) -> float | None:
    row = conn.execute(
        """
        SELECT close FROM stock_daily_bars
        WHERE stock_id = ? AND trade_date = ? AND source = 'finmind'
        """,
        (stock_id, trade_date),
    ).fetchone()
    if row is None:
        return None
    return float(row["close"])


def stock_open(conn: sqlite3.Connection, stock_id: str, trade_date: str) -> float | None:
    row = conn.execute(
        """
        SELECT open, close FROM stock_daily_bars
        WHERE stock_id = ? AND trade_date = ? AND source = 'finmind'
        """,
        (stock_id, trade_date),
    ).fetchone()
    if row is None:
        return None
    if row["open"] is not None and float(row["open"]) > 0:
        return float(row["open"])
    if row["close"] is not None:
        return float(row["close"])
    return None


def stock_high(conn: sqlite3.Connection, stock_id: str, trade_date: str) -> float | None:
    row = conn.execute(
        """
        SELECT high FROM stock_daily_bars
        WHERE stock_id = ? AND trade_date = ? AND source = 'finmind'
        """,
        (stock_id, trade_date),
    ).fetchone()
    if row is None or row["high"] is None:
        return None
    high = float(row["high"])
    return high if high > 0 else None


def stock_low(conn: sqlite3.Connection, stock_id: str, trade_date: str) -> float | None:
    row = conn.execute(
        """
        SELECT low FROM stock_daily_bars
        WHERE stock_id = ? AND trade_date = ? AND source = 'finmind'
        """,
        (stock_id, trade_date),
    ).fetchone()
    if row is None or row["low"] is None:
        return None
    low = float(row["low"])
    return low if low > 0 else None


def trading_dates_after(
    conn: sqlite3.Connection,
    anchor_date: str,
    *,
    count: int,
    inclusive_anchor: bool = False,
) -> list[str]:
    if count < 1:
        return []
    if inclusive_anchor:
        sql = """
            SELECT DISTINCT trade_date AS d
            FROM stock_daily_bars
            WHERE source = 'finmind' AND trade_date >= ?
            ORDER BY d ASC
            LIMIT ?
        """
    else:
        sql = """
            SELECT DISTINCT trade_date AS d
            FROM stock_daily_bars
            WHERE source = 'finmind' AND trade_date > ?
            ORDER BY d ASC
            LIMIT ?
        """
    rows = conn.execute(sql, (anchor_date, count)).fetchall()
    return [str(r["d"]) for r in rows]


def entry_date_after_signal(conn: sqlite3.Connection, signal_date: str) -> str | None:
    dates = trading_dates_after(conn, signal_date, count=1)
    return dates[0] if dates else None


def exit_close_date_from_entry(
    conn: sqlite3.Connection,
    entry_date: str,
    hold_days: int,
) -> str | None:
    dates = trading_dates_after(
        conn,
        entry_date,
        count=hold_days,
        inclusive_anchor=True,
    )
    if len(dates) < hold_days:
        return None
    return dates[hold_days - 1]


def flow_tape_regime(conn: sqlite3.Connection, event_date: str) -> str | None:
    """Flow tape regime：IX0001 近 20 日報酬粗分（多頭 / 空頭 / 震盪）。

    Not Regime layer · not Trend posture. See docs/terminology.md §6.
    """
    rows = conn.execute(
        """
        SELECT date, close FROM daily_bars
        WHERE code = ? AND date <= ?
          AND source IN ('tej', 'yahoo')
        ORDER BY date DESC
        LIMIT 21
        """,
        (BENCHMARK_CODE, event_date),
    ).fetchall()
    if len(rows) < 21:
        return None
    latest = float(rows[0]["close"])
    past = float(rows[20]["close"])
    if past <= 0:
        return None
    ret20 = (latest - past) / past * 100.0
    if ret20 >= 3.0:
        return "多頭"
    if ret20 <= -3.0:
        return "空頭"
    return "震盪"


def market_regime(conn: sqlite3.Connection, event_date: str) -> str | None:
    """Deprecated alias — use flow_tape_regime()."""
    return flow_tape_regime(conn, event_date)


def pre_event_return(
    conn: sqlite3.Connection,
    stock_id: str,
    event_date: str,
    *,
    lookback_days: int = 5,
) -> tuple[float | None, float | None]:
    dates = conn.execute(
        """
        SELECT DISTINCT trade_date AS d
        FROM stock_daily_bars
        WHERE source = 'finmind' AND trade_date <= ?
        ORDER BY d DESC
        LIMIT ?
        """,
        (event_date, lookback_days + 1),
    ).fetchall()
    if len(dates) < lookback_days + 1:
        return None, None
    start_date = str(dates[-1]["d"])
    end_date = str(dates[0]["d"])
    c0 = stock_close(conn, stock_id, start_date)
    c1 = stock_close(conn, stock_id, end_date)
    if c0 is None or c1 is None:
        return c1, None
    return c1, return_pct(c0, c1)


def post_event_returns(
    conn: sqlite3.Connection,
    *,
    event_date: str,
    stock_id: str,
    beta_map: dict[str, sqlite3.Row],
) -> dict[str, float | None]:
    """以 event_date 收盤為基準的 close-to-close 報酬（flow_event_legs 用）。"""
    out: dict[str, float | None] = {}
    beta = _beta_for_stock(beta_map, stock_id)
    for h in FLOW_HORIZONS:
        dates = trading_dates_after(conn, event_date, count=h)
        if len(dates) < h:
            out[f"return_after_{h}d"] = None
            out[f"alpha_after_{h}d"] = None
            continue
        outcome = dates[h - 1]
        c0 = stock_close(conn, stock_id, event_date)
        c1 = stock_close(conn, stock_id, outcome)
        b0 = _bench_close(conn, event_date)
        b1 = _bench_close(conn, outcome)
        if c0 is None or c1 is None or b0 is None or b1 is None:
            out[f"return_after_{h}d"] = None
            out[f"alpha_after_{h}d"] = None
            continue
        ret = return_pct(c0, c1)
        bench = return_pct(b0, b1)
        out[f"return_after_{h}d"] = ret
        out[f"alpha_after_{h}d"] = capm_alpha_pct(ret, bench, beta)
    return out


def open_to_close_trade_return(
    conn: sqlite3.Connection,
    *,
    stock_id: str,
    entry_date: str,
    hold_days: int,
    beta_map: dict[str, sqlite3.Row],
) -> tuple[float | None, float | None]:
    """隔日開盤買入、持有 hold_days 個交易日（baseline 用）。"""
    exit_date = exit_close_date_from_entry(conn, entry_date, hold_days)
    if exit_date is None:
        return None, None
    entry_px = stock_open(conn, stock_id, entry_date)
    exit_px = stock_close(conn, stock_id, exit_date)
    b_entry = _bench_close(conn, entry_date)
    b_exit = _bench_close(conn, exit_date)
    if entry_px is None or exit_px is None or b_entry is None or b_exit is None:
        return None, None
    ret = return_pct(entry_px, exit_px)
    bench = return_pct(b_entry, b_exit)
    beta = _beta_for_stock(beta_map, stock_id)
    return ret, capm_alpha_pct(ret, bench, beta)
