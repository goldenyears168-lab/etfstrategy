#!/usr/bin/env python3
"""
同步科技風險三層指標至 SQLite（建議併入 daily_sync）：

  1. TSM ADR（Yahoo）— 日報酬、相對 MA5/MA10 位置
  2. 費半 ^SOX / 備援 SMH（Yahoo）— 全球半導體 beta
  3. 台指期 TX、電子期 TE（FinMind TaiwanFuturesDaily）— 相對 IX0001 現貨 gap

寫入：
  daily_bars（TSM_ADR / SOX / SMH 日線）
  tech_risk_daily_snapshot（衍生欄位，供開盤前風險檔位參考）
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf

from finmind_client import fetch_finmind
from stock_db import DEFAULT_DB_PATH, connect, upsert_daily_bars, upsert_tech_risk_daily_snapshots

SOURCE_US = "yahoo"
SOURCE_TW = "finmind"
TW_SPOT_CODE = "IX0001"

US_BAR_CODES: dict[str, str] = {
    "TSM_ADR": "TSM",
    "SOX": "^SOX",
    "SMH": "SMH",
}

FUTURES_IDS = ("TX", "TE")


def fetch_yahoo_closes(symbols: dict[str, str], start: date, end: date) -> dict[str, pd.Series]:
    """code -> close series (index=date)."""
    yahoo_syms = " ".join(symbols.values())
    raw = yf.download(
        yahoo_syms,
        start=start.isoformat(),
        end=(end + timedelta(days=1)).isoformat(),
        progress=False,
        auto_adjust=True,
        threads=False,
    )
    if raw.empty:
        return {}
    closes = raw["Close"]
    if isinstance(closes, pd.Series):
        closes = closes.to_frame()
    if isinstance(closes.columns, pd.MultiIndex):
        closes.columns = closes.columns.droplevel(1)

    out: dict[str, pd.Series] = {}
    inv = {v: k for k, v in symbols.items()}
    for col in closes.columns:
        code = inv.get(str(col), str(col))
        series = closes[col].dropna()
        if not series.empty:
            out[code] = series
    return out


def closes_to_daily_bars(code: str, series: pd.Series) -> list[dict]:
    rows: list[dict] = []
    prev_close: float | None = None
    for idx, close in series.items():
        if pd.isna(close):
            continue
        close_f = float(close)
        spread = None
        if prev_close is not None and prev_close != 0:
            spread = round((close_f - prev_close) / prev_close * 100, 4)
        rows.append(
            {
                "code": code,
                "date": idx.strftime("%Y-%m-%d"),
                "open": None,
                "high": None,
                "low": None,
                "close": close_f,
                "volume": None,
                "spread": spread,
                "source": SOURCE_US,
            }
        )
        prev_close = close_f
    return rows


def pct_return(series: pd.Series, on_date: str) -> float | None:
    if on_date not in series.index.strftime("%Y-%m-%d"):
        return None
    idx = series.index[series.index.strftime("%Y-%m-%d") == on_date][0]
    pos = series.index.get_loc(idx)
    if pos < 1:
        return None
    prev = float(series.iloc[pos - 1])
    curr = float(series.iloc[pos])
    if prev == 0:
        return None
    return round((curr - prev) / prev * 100, 4)


def ma_and_position(series: pd.Series, on_date: str, window: int) -> tuple[float | None, float | None, int | None]:
    if on_date not in series.index.strftime("%Y-%m-%d"):
        return None, None, None
    idx = series.index[series.index.strftime("%Y-%m-%d") == on_date][0]
    hist = series.loc[:idx].tail(window)
    if len(hist) < window:
        return None, None, None
    ma = float(hist.mean())
    close = float(series.loc[idx])
    vs_pct = round((close - ma) / ma * 100, 4) if ma else None
    above = 1 if close >= ma else 0
    return ma, vs_pct, above


def fetch_finmind_futures(futures_id: str, start: date, end: date) -> list[dict]:
    return fetch_finmind("TaiwanFuturesDaily", futures_id, start, end)


def _is_near_contract(contract_date: str) -> bool:
    return "/" not in contract_date and len(contract_date) >= 6


def pick_futures_row(
    rows: list[dict],
    session_date: str,
    prefer_sessions: tuple[str, ...],
) -> dict | None:
    """近月合約：同 session 內 volume 最大且 close>0。"""
    candidates = [
        r
        for r in rows
        if r.get("date") == session_date
        and r.get("close", 0) > 0
        and _is_near_contract(str(r.get("contract_date", "")))
    ]
    if not candidates:
        return None
    for session in prefer_sessions:
        subset = [r for r in candidates if r.get("trading_session") == session]
        if subset:
            return max(subset, key=lambda r: int(r.get("volume") or 0))
    return max(candidates, key=lambda r: int(r.get("volume") or 0))


def futures_gap_price(
    futures_rows: list[dict],
    futures_id: str,
    session_date: str,
    spot_prev_date: str,
) -> tuple[dict | None, str]:
    """開盤前參考：當日盤後 → 前日盤後 → 當日日盤 open。"""
    by_id = [r for r in futures_rows if r.get("futures_id") == futures_id]
    order = [
        (session_date, ("after_market",)),
        (spot_prev_date, ("after_market",)),
        (session_date, ("position",)),
    ]
    for row_date, sessions in order:
        row = pick_futures_row(by_id, row_date, sessions)
        if row:
            label = f"{row_date}:{row['trading_session']}"
            return row, label
    return None, ""


def load_spot_closes(conn, code: str, before_date: str | None = None) -> list[tuple[str, float]]:
    sql = """
        SELECT date, close
        FROM daily_bars
        WHERE code = ?
        ORDER BY date DESC
    """
    params: list[str] = [code]
    if before_date:
        sql = """
            SELECT date, close
            FROM daily_bars
            WHERE code = ? AND date < ?
            ORDER BY date DESC
        """
        params.append(before_date)
    rows = conn.execute(sql, params).fetchall()
    return [(r[0], float(r[1])) for r in rows if r[1] is not None]


def list_tw_session_dates(conn, limit: int) -> list[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT date
        FROM daily_bars
        WHERE code = ? AND source IN ('tej', 'yahoo')
        ORDER BY date DESC
        LIMIT ?
        """,
        (TW_SPOT_CODE, limit),
    ).fetchall()
    return [r[0] for r in reversed(rows)]


def load_us_series_from_db(
    conn,
    codes: tuple[str, ...],
    start: date,
) -> dict[str, pd.Series]:
    """自 daily_bars 補齊 Yahoo 即時拉取可能落後的美股收盤。"""
    out: dict[str, pd.Series] = {}
    for code in codes:
        rows = conn.execute(
            """
            SELECT date, close
            FROM daily_bars
            WHERE code = ? AND source = ? AND date >= ? AND close IS NOT NULL
            ORDER BY date
            """,
            (code, SOURCE_US, start.isoformat()),
        ).fetchall()
        if not rows:
            continue
        idx = pd.to_datetime([r[0] for r in rows])
        out[code] = pd.Series([float(r[1]) for r in rows], index=idx)
    return out


def merge_us_series(
    live: dict[str, pd.Series],
    stored: dict[str, pd.Series],
) -> dict[str, pd.Series]:
    merged: dict[str, pd.Series] = {}
    for code in set(live) | set(stored):
        parts: list[pd.Series] = []
        if code in live and not live[code].empty:
            parts.append(live[code])
        if code in stored and not stored[code].empty:
            parts.append(stored[code])
        if not parts:
            continue
        series = pd.concat(parts).sort_index()
        series = series[~series.index.duplicated(keep="last")]
        merged[code] = series
    return merged


def latest_us_trade_date(tsm: pd.Series, session_date: str) -> str | None:
    """取台股 session 開盤前一夜美股收盤日（嚴格早於 session_date）。"""
    if tsm.empty:
        return None
    session = pd.Timestamp(session_date)
    eligible = tsm.index[tsm.index < session]
    if eligible.empty:
        return tsm.index[-1].strftime("%Y-%m-%d")
    return eligible[-1].strftime("%Y-%m-%d")


def build_snapshot_row(
    session_date: str,
    us_series: dict[str, pd.Series],
    conn,
    futures_by_id: dict[str, list[dict]],
) -> dict | None:
    spot_rows = load_spot_closes(conn, TW_SPOT_CODE, before_date=session_date)
    if not spot_rows:
        return None
    tw_spot_date, tw_spot_prev_close = spot_rows[0]

    us_date = latest_us_trade_date(us_series.get("TSM_ADR", pd.Series(dtype=float)), session_date)
    if not us_date:
        return None

    tsm = us_series.get("TSM_ADR", pd.Series(dtype=float))
    sox = us_series.get("SOX", pd.Series(dtype=float))
    smh = us_series.get("SMH", pd.Series(dtype=float))

    tsm_ma5, tsm_vs_ma5, tsm_above_ma5 = ma_and_position(tsm, us_date, 5)
    tsm_ma10, tsm_vs_ma10, tsm_above_ma10 = ma_and_position(tsm, us_date, 10)
    sox_ma5, _, sox_above_ma5 = ma_and_position(sox, us_date, 5)

    sox_ret = pct_return(sox, us_date)
    smh_ret = pct_return(smh, us_date)
    semi_benchmark = "SOX"
    if sox_ret is None and smh_ret is not None:
        semi_benchmark = "SMH"

    tx_row, tx_sess = futures_gap_price(
        futures_by_id.get("TX", []), "TX", session_date, tw_spot_date
    )
    te_row, te_sess = futures_gap_price(
        futures_by_id.get("TE", []), "TE", session_date, tw_spot_date
    )

    tx_gap_pct = None
    tx_price = None
    tx_contract = None
    if tx_row and tw_spot_prev_close:
        tx_price = float(tx_row["close"])
        tx_contract = str(tx_row.get("contract_date", ""))
        tx_gap_pct = round((tx_price - tw_spot_prev_close) / tw_spot_prev_close * 100, 4)

    te_overnight_pct = None
    te_price = None
    te_contract = None
    if te_row:
        te_price = float(te_row["close"])
        te_contract = str(te_row.get("contract_date", ""))
        sp = te_row.get("spread_per")
        if sp is not None and sp != "":
            te_overnight_pct = round(float(sp), 4)

    def close_on(series: pd.Series, d: str) -> float | None:
        hits = series.index[series.index.strftime("%Y-%m-%d") == d]
        if len(hits) == 0:
            return None
        return float(series.loc[hits[-1]])

    tsm_close = close_on(tsm, us_date)

    notes_parts: list[str] = []
    if tx_gap_pct is None:
        notes_parts.append("tx_gap 缺期貨或現貨")
    if te_overnight_pct is None:
        notes_parts.append("te 用 FinMind spread_per 或缺資料")

    return {
        "session_date": session_date,
        "us_trade_date": us_date,
        "tsm_close": tsm_close,
        "tsm_daily_return_pct": pct_return(tsm, us_date),
        "tsm_ma5": tsm_ma5,
        "tsm_ma10": tsm_ma10,
        "tsm_vs_ma5_pct": tsm_vs_ma5,
        "tsm_vs_ma10_pct": tsm_vs_ma10,
        "tsm_above_ma5": tsm_above_ma5,
        "tsm_above_ma10": tsm_above_ma10,
        "sox_close": close_on(sox, us_date),
        "sox_daily_return_pct": sox_ret,
        "sox_ma5": sox_ma5,
        "sox_above_ma5": sox_above_ma5,
        "smh_close": close_on(smh, us_date),
        "smh_daily_return_pct": smh_ret,
        "semi_benchmark": semi_benchmark,
        "tw_spot_date": tw_spot_date,
        "tw_spot_code": TW_SPOT_CODE,
        "tw_spot_prev_close": tw_spot_prev_close,
        "tx_futures_id": "TX",
        "tx_contract_date": tx_contract,
        "tx_futures_price": tx_price,
        "tx_futures_session": tx_sess or None,
        "tx_gap_pct": tx_gap_pct,
        "te_futures_id": "TE",
        "te_contract_date": te_contract,
        "te_futures_price": te_price,
        "te_futures_session": te_sess or None,
        "te_overnight_pct": te_overnight_pct,
        "notes": "; ".join(notes_parts) if notes_parts else None,
        "source_us": SOURCE_US,
        "source_tw": SOURCE_TW,
    }


def _fmt_pct(val: float | None) -> str:
    return f"{val:+.2f}%" if val is not None else "—"


def format_snapshot_line(row: dict) -> str:
    sox_r = row.get("sox_daily_return_pct")
    if sox_r is None:
        sox_r = row.get("smh_daily_return_pct")
    ma5 = row.get("tsm_above_ma5")
    ma10 = row.get("tsm_above_ma10")
    return (
        f"{row['session_date']} | TSM ADR {_fmt_pct(row.get('tsm_daily_return_pct'))} "
        f"(MA5={'上' if ma5 else '下' if ma5 is not None else '?'}"
        f" MA10={'上' if ma10 else '下' if ma10 is not None else '?'}) | "
        f"{row.get('semi_benchmark')} {_fmt_pct(sox_r)} | "
        f"TX gap {_fmt_pct(row.get('tx_gap_pct'))} ({row.get('tx_futures_session') or '—'}) | "
        f"TE o/n {_fmt_pct(row.get('te_overnight_pct'))}"
    )


def sync_tech_risk(
    db_path: Path,
    history_days: int,
    session_limit: int,
    dry_run: bool = False,
    *,
    quiet: bool = False,
) -> int:
    end = date.today()
    start = end - timedelta(days=history_days + 15)

    live_us = fetch_yahoo_closes(US_BAR_CODES, start, end)
    conn = connect(db_path)
    try:
        stored_us = load_us_series_from_db(conn, tuple(US_BAR_CODES.keys()), start)
        us_series = merge_us_series(live_us, stored_us)
        if "TSM_ADR" not in us_series:
            raise RuntimeError("Yahoo 無 TSM ADR 資料")

        bar_rows: list[dict] = []
        for code, series in us_series.items():
            bar_rows.extend(closes_to_daily_bars(code, series))

        futures_by_id: dict[str, list[dict]] = {}
        for fid in FUTURES_IDS:
            futures_by_id[fid] = fetch_finmind_futures(fid, start, end)

        if not dry_run:
            upsert_daily_bars(conn, bar_rows)
        session_dates = list_tw_session_dates(conn, session_limit)
        if not session_dates:
            session_dates = [end.isoformat()]
        today_iso = end.isoformat()
        if today_iso not in session_dates:
            # 開盤前 IX0001 尚無當日收盤，仍須組裝「今日」snapshot（對應昨夜美股收盤）
            session_dates.append(today_iso)

        snapshots: list[dict] = []
        for session_date in session_dates:
            row = build_snapshot_row(session_date, us_series, conn, futures_by_id)
            if row:
                snapshots.append(row)

        if not snapshots:
            raise RuntimeError("無法組裝 tech_risk_daily_snapshot")

        if dry_run:
            print(format_snapshot_line(snapshots[-1]))
            return len(snapshots)

        count = upsert_tech_risk_daily_snapshots(conn, snapshots)
    finally:
        conn.close()

    latest = snapshots[-1]
    if quiet:
        print(f"  tech_risk: {count} sessions, latest {format_snapshot_line(latest)}")
    else:
        print(f"  tech_risk 同步：{count} 個台股交易日")
        print(f"    {format_snapshot_line(latest)}")
        print(
            f"    說明：TX gap = 期貨價 vs 前日 {TW_SPOT_CODE} 收盤；"
            "TE o/n = 電子期 FinMind spread_per（盤後/近月）"
        )
    return count


def main() -> int:
    parser = argparse.ArgumentParser(description="同步科技風險三層指標至 SQLite")
    parser.add_argument("--sync-db", action="store_true", help="寫入 daily_bars + tech_risk_daily_snapshot")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--history-days", type=int, default=90, help="Yahoo/期貨回溯天數")
    parser.add_argument(
        "--session-limit",
        type=int,
        default=30,
        help="依 IX0001 交易日回補 snapshot 筆數（預設 30）",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    if not args.sync_db and not args.dry_run:
        parser.error("請加上 --sync-db 或 --dry-run")

    try:
        sync_tech_risk(
            args.db,
            args.history_days,
            args.session_limit,
            dry_run=args.dry_run,
            quiet=args.quiet,
        )
    except requests.HTTPError as exc:
        print(f"  WARN tech_risk: FinMind 不可用（{exc}）", file=sys.stderr)
        return 0
    except RuntimeError as exc:
        print(f"  WARN tech_risk: {exc}", file=sys.stderr)
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"  WARN tech_risk: {exc}", file=sys.stderr)
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
