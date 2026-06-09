#!/usr/bin/env python3
"""
盤中 1 分鐘監控原型：ETF 成分股 union + 量價／委買委賣 imbalance + binary buy_signal。

資料來源（擇一，見 --source）：
  finmind  — taiwan_stock_tick_snapshot（Sponsor+有效 token；約 10 秒更新；僅最佳一檔）
  yahoo    — 1 分 K 研究備援（無五檔；延遲可能 15 分鐘）

無法用現有方案取得：盤中三大法人、當日 ETF 持股調倉、完整五檔（需券商／交易所直連）。

用法：
  .venv/bin/python intraday_monitor.py --once --source yahoo --max-symbols 10
  .venv/bin/python intraday_monitor.py --loop --interval 60 --source finmind
  .venv/bin/python intraday_monitor.py --once --source finmind --signal-after 10:30
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, time as dt_time
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

from price_adapter import fetch_finmind_tick_rows
from stock_db import (
    DEFAULT_DB_PATH,
    connect,
    load_etf_constituent_watchlist,
    upsert_intraday_1m_bars,
    upsert_intraday_signals,
)

TZ = ZoneInfo("Asia/Taipei")
INDEX_FINMIND_ID = "001"
YAHOO_INDEX = "^TWII"
SESSION_MINUTES = 265  # 09:00–13:25 約 265 分鐘
BATCH_SIZE = 40


@dataclass
class DailyContext:
    avg_volume_20d: float
    high_20d: float
    prev_close: float


@dataclass
class SymbolState:
    prev_close: float
    day_open: float | None = None
    day_high: float = 0.0
    day_low: float = 1e18
    cum_volume: float = 0.0
    cum_amount: float = 0.0
    last_total_volume: float | None = None
    last_close: float | None = None
    minute_bucket: str | None = None
    bar_open: float | None = None
    bar_high: float | None = None
    bar_low: float | None = None
    bar_volume: float = 0.0
    aggressive_buy_vol: float = 0.0
    aggressive_total_vol: float = 0.0



def is_trading_window(now: datetime | None = None) -> bool:
    now = now or datetime.now(TZ)
    if now.weekday() >= 5:
        return False
    t = now.time()
    return dt_time(9, 0) <= t <= dt_time(13, 25)


def floor_minute_ts(now: datetime) -> str:
    floored = now.replace(second=0, microsecond=0)
    return floored.strftime("%Y-%m-%d %H:%M:%S")


def load_env_file(root: Path) -> None:
    env_path = root / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        os.environ.setdefault(key.strip(), val.strip())


def load_daily_context(symbols: list[str], lookback_days: int = 25) -> dict[str, DailyContext]:
    """Yahoo 日線：20 日均量、20 日高、昨收。"""
    out: dict[str, DailyContext] = {}
    for sid in symbols:
        ticker = f"{sid}.TW"
        try:
            hist = yf.Ticker(ticker).history(period=f"{lookback_days}d", interval="1d", auto_adjust=False)
        except Exception:
            continue
        if hist is None or hist.empty or len(hist) < 2:
            continue
        hist = hist.dropna(subset=["Close", "Volume"])
        if len(hist) < 2:
            continue
        vol = hist["Volume"].tail(20)
        out[sid] = DailyContext(
            avg_volume_20d=float(vol.mean()),
            high_20d=float(hist["High"].tail(20).max()),
            prev_close=float(hist["Close"].iloc[-2]),
        )
    return out


def fetch_index_day_return_yahoo() -> float:
    try:
        hist = yf.Ticker(YAHOO_INDEX).history(period="5d", interval="1d", auto_adjust=False)
        if hist is None or len(hist) < 2:
            return 0.0
        prev = float(hist["Close"].iloc[-2])
        last = float(hist["Close"].iloc[-1])
        if prev <= 0:
            return 0.0
        return last / prev - 1.0
    except Exception:
        return 0.0


def fetch_finmind_snapshots(symbols: list[str]) -> tuple[list[dict], str | None]:
    return fetch_finmind_tick_rows(symbols, batch_size=BATCH_SIZE)


def fetch_yahoo_1m_bars(symbols: list[str]) -> dict[str, pd.Series]:
    """回傳各股最新 1 分 K 列（Close, Volume, 當日累積量）。"""
    out: dict[str, pd.Series] = {}
    for sid in symbols:
        try:
            df = yf.Ticker(f"{sid}.TW").history(period="1d", interval="1m", auto_adjust=False)
        except Exception:
            continue
        if df is None or df.empty:
            continue
        if hasattr(df.index, "tz") and df.index.tz is not None:
            df = df.tz_convert(TZ)
        out[sid] = df.iloc[-1]
    return out


def order_imbalance_1(buy_vol: float, sell_vol: float) -> float:
    total = buy_vol + sell_vol
    if total <= 0:
        return 0.0
    return (buy_vol - sell_vol) / total


def session_elapsed_fraction(now: datetime) -> float:
    open_dt = now.replace(hour=9, minute=0, second=0, microsecond=0)
    if now <= open_dt:
        return 0.01
    elapsed = (now - open_dt).total_seconds() / 60.0
    return min(max(elapsed / SESSION_MINUTES, 0.01), 1.0)


def estimate_rel_volume(cum_volume: float, avg_vol_20d: float, now: datetime) -> float:
    expected = avg_vol_20d * session_elapsed_fraction(now)
    if expected <= 0:
        return 0.0
    return cum_volume / expected


def calc_buy_signal(
    *,
    etf_hold_count: int,
    rel_volume: float,
    ret_vs_index: float,
    close: float,
    vwap_day: float,
    position_in_range: float,
    order_imb: float,
    breakout: bool,
    thresholds: argparse.Namespace,
) -> tuple[int, str]:
    cond_pool = (etf_hold_count >= thresholds.min_etf_hold) or (
        etf_hold_count >= thresholds.min_etf_consensus
    )
    cond_volume = rel_volume > thresholds.rel_volume_min
    cond_strength = ret_vs_index > thresholds.ret_vs_index_min
    cond_price = close > vwap_day and position_in_range > thresholds.position_in_range_min
    cond_book = order_imb > thresholds.order_imbalance_min
    cond_breakout = breakout

    flags = {
        "pool": cond_pool,
        "volume": cond_volume,
        "strength": cond_strength,
        "price": cond_price,
        "book": cond_book,
        "breakout": cond_breakout,
    }
    buy = int(all(flags.values()))
    reason = ",".join(k for k, v in flags.items() if v) or "none"
    return buy, reason


@dataclass
class MonitorRun:
    source: str
    states: dict[str, SymbolState] = field(default_factory=dict)
    daily_ctx: dict[str, DailyContext] = field(default_factory=dict)
    index_day_return: float = 0.0


def process_finmind_row(
    row: dict,
    watch: dict[str, dict],
    run: MonitorRun,
    now: datetime,
    thresholds: argparse.Namespace,
) -> tuple[dict | None, dict | None]:
    sid = str(row.get("stock_id", "")).strip()
    if sid not in watch:
        return None, None
    ctx = run.daily_ctx.get(sid)
    if ctx is None:
        return None, None

    close = float(row.get("close") or 0)
    if close <= 0:
        return None, None

    total_vol = float(row.get("total_volume") or 0)
    total_amt = float(row.get("total_amount") or 0)
    buy_vol = float(row.get("buy_volume") or 0)
    sell_vol = float(row.get("sell_volume") or 0)
    tick_type = str(row.get("TickType", "0"))

    st = run.states.setdefault(
        sid,
        SymbolState(prev_close=ctx.prev_close),
    )
    if st.day_open is None:
        st.day_open = float(row.get("open") or close)
    st.day_high = max(st.day_high, float(row.get("high") or close), close)
    st.day_low = min(st.day_low, float(row.get("low") or close), close)

    vol_1m = 0.0
    if st.last_total_volume is not None and total_vol >= st.last_total_volume:
        vol_1m = total_vol - st.last_total_volume
    st.last_total_volume = total_vol
    st.cum_volume = total_vol
    st.cum_amount = total_amt

    if tick_type == "2" and vol_1m > 0:
        st.aggressive_buy_vol += vol_1m
        st.aggressive_total_vol += vol_1m
    elif tick_type == "1" and vol_1m > 0:
        st.aggressive_total_vol += vol_1m

    ts = floor_minute_ts(now)
    vwap_day = total_amt / total_vol if total_vol > 0 else close
    day_ret = close / ctx.prev_close - 1 if ctx.prev_close > 0 else 0.0
    idx_ret = run.index_day_return
    ret_vs = day_ret - idx_ret
    rel_vol = estimate_rel_volume(total_vol, ctx.avg_volume_20d, now)
    rng = max(st.day_high - st.day_low, 1e-6)
    pos_range = (close - st.day_low) / rng
    imb = order_imbalance_1(buy_vol, sell_vol)
    breakout = close > ctx.high_20d

    bar_row = {
        "symbol": sid,
        "ts": ts,
        "open_1m": close,
        "high_1m": close,
        "low_1m": close,
        "close_1m": close,
        "volume_1m": vol_1m,
        "cum_volume": total_vol,
        "vwap_day": vwap_day,
        "day_return": day_ret,
        "rel_volume_est": rel_vol,
        "ret_vs_index_day": ret_vs,
        "order_imbalance_1": imb,
        "position_in_day_range": pos_range,
        "breakout_flag": int(breakout),
        "source": run.source,
    }

    buy, reason = calc_buy_signal(
        etf_hold_count=watch[sid]["etf_hold_count"],
        rel_volume=rel_vol,
        ret_vs_index=ret_vs,
        close=close,
        vwap_day=vwap_day,
        position_in_range=pos_range,
        order_imb=imb,
        breakout=breakout,
        thresholds=thresholds,
    )
    sig_row = {
        "symbol": sid,
        "ts": ts,
        "buy_signal": buy,
        "etf_hold_count": watch[sid]["etf_hold_count"],
        "rel_volume_est": rel_vol,
        "ret_vs_index_day": ret_vs,
        "position_in_day_range": pos_range,
        "order_imbalance_1": imb,
        "breakout_flag": int(breakout),
        "reason": reason,
        "source": run.source,
    }
    return bar_row, sig_row


def process_yahoo_row(
    sid: str,
    row: pd.Series,
    df: pd.DataFrame,
    watch: dict[str, dict],
    run: MonitorRun,
    now: datetime,
    thresholds: argparse.Namespace,
) -> tuple[dict | None, dict | None]:
    ctx = run.daily_ctx.get(sid)
    if ctx is None:
        return None, None
    close = float(row["Close"])
    if close <= 0:
        return None, None

    if hasattr(df.index, "tz") and df.index.tz is not None:
        day_df = df[df.index.date == now.date()]
    else:
        day_df = df

    cum_vol = float(day_df["Volume"].sum()) if not day_df.empty else float(row.get("Volume", 0))
    cum_amt = float((day_df["Close"] * day_df["Volume"]).sum()) if not day_df.empty else 0.0
    vol_1m = float(row.get("Volume", 0))

    st = run.states.setdefault(sid, SymbolState(prev_close=ctx.prev_close))
    if st.day_open is None and not day_df.empty:
        st.day_open = float(day_df["Open"].iloc[0])
    st.day_high = float(day_df["High"].max()) if not day_df.empty else close
    st.day_low = float(day_df["Low"].min()) if not day_df.empty else close
    st.cum_volume = cum_vol

    ts = floor_minute_ts(now)
    vwap_day = cum_amt / cum_vol if cum_vol > 0 else close
    day_ret = close / ctx.prev_close - 1 if ctx.prev_close > 0 else 0.0
    ret_vs = day_ret - run.index_day_return
    rel_vol = estimate_rel_volume(cum_vol, ctx.avg_volume_20d, now)
    rng = max(st.day_high - st.day_low, 1e-6)
    pos_range = (close - st.day_low) / rng
    imb = 0.0
    breakout = close > ctx.high_20d

    bar_row = {
        "symbol": sid,
        "ts": ts,
        "open_1m": float(row["Open"]),
        "high_1m": float(row["High"]),
        "low_1m": float(row["Low"]),
        "close_1m": close,
        "volume_1m": vol_1m,
        "cum_volume": cum_vol,
        "vwap_day": vwap_day,
        "day_return": day_ret,
        "rel_volume_est": rel_vol,
        "ret_vs_index_day": ret_vs,
        "order_imbalance_1": imb,
        "position_in_day_range": pos_range,
        "breakout_flag": int(breakout),
        "source": run.source,
    }
    buy, reason = calc_buy_signal(
        etf_hold_count=watch[sid]["etf_hold_count"],
        rel_volume=rel_vol,
        ret_vs_index=ret_vs,
        close=close,
        vwap_day=vwap_day,
        position_in_range=pos_range,
        order_imb=imb,
        breakout=breakout,
        thresholds=thresholds,
    )
    sig_row = {
        "symbol": sid,
        "ts": ts,
        "buy_signal": buy,
        "etf_hold_count": watch[sid]["etf_hold_count"],
        "rel_volume_est": rel_vol,
        "ret_vs_index_day": ret_vs,
        "position_in_day_range": pos_range,
        "order_imbalance_1": imb,
        "breakout_flag": int(breakout),
        "reason": reason,
        "source": run.source,
    }
    return bar_row, sig_row


def run_cycle(
    conn,
    watch: list[dict],
    args: argparse.Namespace,
) -> int:
    now = datetime.now(TZ)
    if not args.ignore_hours and not is_trading_window(now):
        print(f"非交易時段（{now.strftime('%H:%M')} TST），略過。加 --ignore-hours 可強制執行。")
        return 0

    signal_after = dt_time.fromisoformat(args.signal_after)
    before_cutoff = now.time() <= dt_time(13, 0)
    allow_signal = now.time() >= signal_after and (before_cutoff or args.ignore_hours)

    symbols = [w["stock_id"] for w in watch]
    if args.max_symbols > 0:
        symbols = symbols[: args.max_symbols]
    watch_map = {w["stock_id"]: w for w in watch if w["stock_id"] in symbols}

    print(f"監控 {len(symbols)} 檔成分股 | source={args.source} | ts={floor_minute_ts(now)}")

    run = MonitorRun(source=args.source)
    run.daily_ctx = load_daily_context(symbols)
    run.index_day_return = fetch_index_day_return_yahoo()
    print(f"  日線 context 載入 {len(run.daily_ctx)}/{len(symbols)} 檔 | 大盤日報酬≈{run.index_day_return:+.2%}")

    bar_rows: list[dict] = []
    sig_rows: list[dict] = []

    if args.source == "finmind":
        snaps, err = fetch_finmind_snapshots(symbols)
        if err:
            print(f"  FinMind 失敗：{err}")
            if args.fallback_yahoo:
                print("  改用 Yahoo 1m…")
                args.source = "yahoo"
            else:
                return 1
        else:
            idx_snaps, _ = fetch_finmind_snapshots([INDEX_FINMIND_ID])
            if idx_snaps:
                try:
                    idx_close = float(idx_snaps[0].get("close") or 0)
                    idx_prev = float(idx_snaps[0].get("average_price") or idx_close) - float(
                        idx_snaps[0].get("change_price") or 0
                    )
                    if idx_prev > 0:
                        run.index_day_return = idx_close / idx_prev - 1
                except (TypeError, ValueError):
                    pass
            for row in snaps:
                bar, sig = process_finmind_row(row, watch_map, run, now, args)
                if bar:
                    bar_rows.append(bar)
                if sig and allow_signal:
                    sig_rows.append(sig)

    if args.source == "yahoo":
        for sid in symbols:
            try:
                df = yf.Ticker(f"{sid}.TW").history(period="1d", interval="1m", auto_adjust=False)
            except Exception:
                continue
            if df is None or df.empty:
                continue
            bar, sig = process_yahoo_row(sid, df.iloc[-1], df, watch_map, run, now, args)
            if bar:
                bar_rows.append(bar)
            if sig and allow_signal:
                sig_rows.append(sig)

    if args.persist:
        upsert_intraday_1m_bars(conn, bar_rows)
        upsert_intraday_signals(conn, sig_rows)

    buys = [s for s in sig_rows if s["buy_signal"] == 1]
    print(f"  寫入 bars={len(bar_rows)} signals={len(sig_rows)} | buy_signal=1: {len(buys)} 檔")
    if buys:
        print("  --- buy_signal=1 ---")
        for s in sorted(buys, key=lambda x: -x.get("rel_volume_est", 0))[:20]:
            print(
                f"    {s['symbol']} hold_etfs={s['etf_hold_count']} "
                f"rel_vol={s['rel_volume_est']:.2f} ret_vs_idx={s['ret_vs_index_day']:+.2%} "
                f"pos={s['position_in_day_range']:.2f} imb={s['order_imbalance_1']:+.2f} "
                f"({s['reason']})"
            )
    elif not allow_signal:
        if now.time() < signal_after:
            print(f"  （尚未到 {args.signal_after}，僅更新 bars）")
        elif not before_cutoff and not args.ignore_hours:
            print("  （已過 13:00 信號截止；僅更新 bars）")

    return 0


def print_feasibility() -> None:
    print(
        """
=== 日內監控可行性（你的現有 API）===

可做（研究／原型）：
  • ETF 成分股 union（~124 檔）每 1 分鐘輪詢
  • FinMind taiwan_stock_tick_snapshot（需 Sponsor + 有效 FINMIND_TOKEN）
    - 最佳一檔 buy/sell 量價、累積量、TickType 主動買賣推估
    - 約 10 秒更新；可 data_id 批次降低 quota
  • Yahoo 1m K（備援）：OHLCV + 累積量；無委託簿
  • 相對放量（cum_vol / 20日均量×盤中時間比例）、突破 20 日高、相對大盤

做不到（除非加券商／交易所 API）：
  • 盤中三大法人、當日主動 ETF 持股調倉
  • 完整五檔委買委賣（FinMind 僅一檔；order_imbalance_5 需另接）
  • 逐筆 tick 重建（snapshot 是累積快照非完整 tick log）

建議：
  1. 修復 FINMIND_TOKEN → 確認 Sponsor 後用 --source finmind
  2. 盤中 --loop --interval 60；13:00 前看 buy_signal；不下單（本腳本無下單）
  3. 盤後仍跑 daily_sync 驗證 ETF/法人是否跟上
"""
    )


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    load_env_file(root)

    parser = argparse.ArgumentParser(description="ETF 成分股盤中 1 分鐘監控原型")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--once", action="store_true", help="執行一次")
    parser.add_argument("--loop", action="store_true", help="交易時段內每 interval 秒循環")
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--source", choices=("finmind", "yahoo"), default="yahoo")
    parser.add_argument("--fallback-yahoo", action="store_true", default=True)
    parser.add_argument("--no-fallback-yahoo", action="store_false", dest="fallback_yahoo")
    parser.add_argument("--max-symbols", type=int, default=0, help="0=全部 watchlist")
    parser.add_argument("--persist", action="store_true", help="寫入 intraday_* 表")
    parser.add_argument("--ignore-hours", action="store_true")
    parser.add_argument("--signal-after", default="10:30", help="此時間後才輸出 buy_signal")
    parser.add_argument("--feasibility", action="store_true", help="印可行性說明後結束")
    parser.add_argument("--rel-volume-min", type=float, default=1.5)
    parser.add_argument("--ret-vs-index-min", type=float, default=0.008)
    parser.add_argument("--position-in-range-min", type=float, default=0.7)
    parser.add_argument("--order-imbalance-min", type=float, default=0.1)
    parser.add_argument("--min-etf-hold", type=int, default=1)
    parser.add_argument("--min-etf-consensus", type=int, default=2)
    args = parser.parse_args()

    if args.feasibility:
        print_feasibility()
        return 0

    if not args.once and not args.loop:
        parser.error("請指定 --once 或 --loop（或 --feasibility）")

    conn = connect(args.db)
    watch = load_etf_constituent_watchlist(conn)
    if not watch:
        print("watchlist 為空：請先跑 daily_sync 同步 etf_holdings", file=sys.stderr)
        return 1
    print(f"watchlist：{len(watch)} 檔（最新 ETF 持股聯集）")

    if args.loop:
        while True:
            if is_trading_window() or args.ignore_hours:
                run_cycle(conn, watch, args)
            time.sleep(max(args.interval, 15))
    return run_cycle(conn, watch, args)


if __name__ == "__main__":
    raise SystemExit(main())
