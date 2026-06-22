"""RRG universe snapshot — 全成分股 RRG 狀態寫入 rrg_universe_scores（盤中 / 收盤）。"""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from typing import Any, Literal

import pandas as pd

from finmind_client import fetch_tick_snapshots
from market_benchmark import latest_trading_date, load_benchmark_close
from project_config import DEFAULT_ETF_CODES
from research.backtest.finpilot_local_backtest import load_price_panels
from rrg_mono_daily_brief import (
    LENGTH,
    _feat,
    _fresh_mono,
    _mono_tier2,
    _tier2,
)
from rrg_mono_intraday_watch import (
    BENCH_TICK_IDS,
    _build_provisional_panels,
    _env_csv,
    _tick_map,
)
from rrg_rotation import compute_rrg_panel
from stock_db import (
    load_etf_constituent_watchlist,
    replace_rrg_universe_scores,
)

ScreenKind = Literal["intraday", "close"]


def _close_bars_ready(conn: sqlite3.Connection, as_of: str, *, min_bars: int = 50) -> bool:
    row = conn.execute(
        """
        SELECT COUNT(DISTINCT stock_id) AS n
        FROM stock_daily_bars
        WHERE source = 'finmind' AND trade_date = ?
        """,
        (as_of,),
    ).fetchone()
    return bool(row and int(row["n"] or 0) >= min_bars)


def _intraday_data_baseline(conn: sqlite3.Connection, session_date: str) -> str:
    prev = latest_trading_date(conn, on_or_before=session_date)
    return prev or session_date


def build_universe_rows_from_panels(
    conn: sqlite3.Connection,
    as_of: str,
    close: pd.DataFrame,
    bench: pd.Series,
    *,
    etf_codes: tuple[str, ...] = DEFAULT_ETF_CODES,
    data_baseline_date: str,
    tick_ok_by_id: dict[str, bool] | None = None,
) -> list[dict[str, Any]]:
    """對 watchlist 每一檔計算 RRG 特徵（含資料不足列）。"""
    bench = bench.reindex(close.index)
    rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench, length=LENGTH)
    daily_pct = close.pct_change(fill_method=None) * 100.0
    full_dates = close.index.astype(str).tolist()
    if as_of not in full_dates:
        raise RuntimeError(f"{as_of} 不在收盤價 panel")
    si = full_dates.index(as_of)

    watch = load_etf_constituent_watchlist(conn, etf_codes)
    name_map = {w["stock_id"]: w.get("stock_name", "") for w in watch}
    tick_ok = tick_ok_by_id or {}

    rows: list[dict[str, Any]] = []
    for sid in [w["stock_id"] for w in watch]:
        f = _feat(rs_ratio, rs_mom, full_dates, si, sid)
        pct = float(daily_pct.at[as_of, sid]) if sid in daily_pct.columns else None
        if pct != pct:
            pct = None

        base: dict[str, Any] = {
            "data_baseline_date": data_baseline_date,
            "stock_id": sid,
            "stock_name": name_map.get(sid, ""),
            "rs_ratio": None,
            "rs_momentum": None,
            "quadrant": None,
            "quadrants_json": None,
            "trend": None,
            "disp": None,
            "seg_last": None,
            "segs_json": None,
            "tier2": 0,
            "mono_tier2": 0,
            "mono_fresh": 0,
            "daily_pct": pct,
            "tick_ok": (
                1
                if tick_ok_by_id is not None and tick_ok_by_id.get(sid)
                else (0 if tick_ok_by_id is not None else None)
            ),
        }
        if f is None:
            rows.append(base)
            continue

        mono = _mono_tier2(f)
        fresh = _fresh_mono(rs_ratio, rs_mom, full_dates, si, sid) if mono else False
        quads = [q or "?" for q in f["quadrants"]]
        base.update(
            {
                "rs_ratio": float(f["rs_ratio"]),
                "rs_momentum": float(f["rs_momentum"]),
                "quadrant": f["end_q"],
                "quadrants_json": json.dumps(quads, ensure_ascii=False),
                "trend": f["trend"],
                "disp": float(f["disp"]),
                "seg_last": float(f["seg_last"]),
                "segs_json": json.dumps([float(x) for x in f["segs"]]),
                "tier2": int(_tier2(f)),
                "mono_tier2": int(mono),
                "mono_fresh": int(fresh),
            }
        )
        rows.append(base)
    return rows


def persist_universe_snapshot(
    conn: sqlite3.Connection,
    *,
    session_date: str,
    screen_kind: ScreenKind,
    rows: list[dict[str, Any]],
) -> int:
    return replace_rrg_universe_scores(
        conn,
        session_date=session_date,
        screen_kind=screen_kind,
        rows=rows,
    )


def run_intraday_universe_snapshot(
    conn: sqlite3.Connection,
    *,
    session_date: str | None = None,
    etf_codes: tuple[str, ...] | None = None,
) -> tuple[int, str, int]:
    """盤中 tick + provisional close → SQLite（screen_kind=intraday）。"""
    session = session_date or date.today().isoformat()
    codes = etf_codes or _env_csv("RRG_MONO_ETF_CODES", DEFAULT_ETF_CODES)
    baseline = _intraday_data_baseline(conn, session)

    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn)
    universe = [w["stock_id"] for w in load_etf_constituent_watchlist(conn, codes)]

    tick_rows, _tick_error = fetch_tick_snapshots(universe)
    stock_ticks = _tick_map(tick_rows)
    tick_ok_by_id = {sid: True for sid in stock_ticks}

    bench_rows, _ = fetch_tick_snapshots(list(BENCH_TICK_IDS))
    bench_ticks = _tick_map(bench_rows)
    bench_px = None
    for bid in BENCH_TICK_IDS:
        if bid in bench_ticks:
            bench_px = bench_ticks[bid]
            break

    close_prov, bench_prov, tick_n, _uni_n = _build_provisional_panels(
        close, bench, session, stock_ticks, bench_px
    )
    rows = build_universe_rows_from_panels(
        conn,
        session,
        close_prov,
        bench_prov,
        etf_codes=codes,
        data_baseline_date=baseline,
        tick_ok_by_id=tick_ok_by_id,
    )
    n = persist_universe_snapshot(
        conn, session_date=session, screen_kind="intraday", rows=rows
    )
    return n, session, tick_n


def persist_intraday_universe_from_panels(
    conn: sqlite3.Connection,
    *,
    session_date: str,
    close_prov: pd.DataFrame,
    bench_prov: pd.Series,
    stock_ticks: dict[str, float],
    etf_codes: tuple[str, ...],
) -> int:
    """盤中 provisional panel 已就緒時寫入（避免重複 tick API）。"""
    baseline = _intraday_data_baseline(conn, session_date)
    rows = build_universe_rows_from_panels(
        conn,
        session_date,
        close_prov,
        bench_prov,
        etf_codes=etf_codes,
        data_baseline_date=baseline,
        tick_ok_by_id={sid: True for sid in stock_ticks},
    )
    return persist_universe_snapshot(
        conn, session_date=session_date, screen_kind="intraday", rows=rows
    )


def run_close_universe_snapshot(
    conn: sqlite3.Connection,
    *,
    session_date: str | None = None,
    etf_codes: tuple[str, ...] | None = None,
) -> tuple[int, str | None]:
    """收盤 K → SQLite（screen_kind=close）；需當日 stock_daily_bars。"""
    session = session_date or date.today().isoformat()
    as_of = latest_trading_date(conn, on_or_before=session) or session
    if not _close_bars_ready(conn, as_of):
        return 0, None

    codes = etf_codes or _env_csv("RRG_MONO_ETF_CODES", DEFAULT_ETF_CODES)
    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index)
    if as_of not in close.index.astype(str):
        return 0, None

    rows = build_universe_rows_from_panels(
        conn,
        as_of,
        close,
        bench,
        etf_codes=codes,
        data_baseline_date=as_of,
        tick_ok_by_id=None,
    )
    for r in rows:
        r["tick_ok"] = 1
    n = persist_universe_snapshot(
        conn, session_date=session, screen_kind="close", rows=rows
    )
    return n, session


def main(argv: list[str] | None = None) -> int:
    import argparse

    from project_dotenv import load_project_dotenv
    from stock_db import connect

    load_project_dotenv()
    parser = argparse.ArgumentParser(description="RRG universe snapshot")
    parser.add_argument("--intraday", action="store_true")
    parser.add_argument("--close", action="store_true")
    parser.add_argument("--as-of", default=None, help="session_date YYYY-MM-DD")
    args = parser.parse_args(argv)

    if not args.intraday and not args.close:
        parser.error("specify --intraday or --close")

    conn = connect()
    try:
        if args.intraday:
            n, session, tick_n = run_intraday_universe_snapshot(
                conn, session_date=args.as_of
            )
            print(f"RRG universe intraday: session={session} rows={n} tick={tick_n}")
        else:
            n, session = run_close_universe_snapshot(conn, session_date=args.as_of)
            if not session:
                print("RRG universe close: skipped（無足夠當日 K 線）")
                return 1
            print(f"RRG universe close: session={session} rows={n}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
