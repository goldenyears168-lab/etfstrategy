"""Carlo Zarattini intraday replication · shared TW spot utilities.

Research layer only · faithful US→TW session mapping · PIT-safe daily features.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd

from stock_db import DEFAULT_DB_PATH, load_kbar_day_bars

SESSION_OPEN = "09:00:00"
SESSION_CLOSE = "13:30:00"
OR_MINUTES = 5
DEFAULT_COST_RT = 0.00585
DEFAULT_LEVERAGE = 2.0
RISK_PCT = 0.01
INITIAL_AUM_NTD = 750_000.0
IS_OOS_RATIO = 0.7
TRACK2_OOS_MIN_N = 60

# Finmind cross-section universe (Track 4/5)
MIN_CONTINUOUS_DAYS = 50
CONTINUOUS_START = "2026-01-02"
FINMIND_XSEC_EXCLUDE_IDS = ("0050",)

# 0050 ETF daily lives under IR0002 in daily_bars (Yahoo 0050.TW)
BENCHMARK_DAILY_CODE = "IR0002"
BENCHMARK_INDEX_CODE = "IX0001"


UniverseMode = Literal["single", "finmind_xsec"]


@dataclass(frozen=True)
class UniverseSpec:
    """Research universe · single stock or Finmind cross-section."""

    mode: UniverseMode
    stock_id: str | None = None
    continuous_start: str = CONTINUOUS_START
    min_continuous_days: int = MIN_CONTINUOUS_DAYS
    exclude_ids: tuple[str, ...] = FINMIND_XSEC_EXCLUDE_IDS

    @classmethod
    def single(cls, stock_id: str) -> UniverseSpec:
        return cls(mode="single", stock_id=stock_id)

    @classmethod
    def finmind_cross_section(cls) -> UniverseSpec:
        return cls(mode="finmind_xsec")


@dataclass(frozen=True)
class TradeRecord:
    trade_date: str
    direction: Literal["long", "short"]
    entry: float
    exit: float
    stop: float
    gross_pct: float
    net_pct: float
    r_multiple: float
    exit_reason: str
    shares: int
    pnl_ntd: float


def filter_session(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = df[(df["minute"] >= SESSION_OPEN) & (df["minute"] <= SESSION_CLOSE)].copy()
    return out.sort_values("minute").reset_index(drop=True)


def _minutes_from_open(minute: str) -> int:
    parts = minute.split(":")
    h, m = int(parts[0]), int(parts[1])
    return (h - 9) * 60 + m


def aggregate_5m(day_1m: pd.DataFrame) -> pd.DataFrame:
    """Anchor 5m bars at 09:00 · first bar = 09:00–09:04."""
    day = filter_session(day_1m)
    if day.empty:
        return pd.DataFrame()
    day = day.copy()
    day["mfo"] = day["minute"].map(_minutes_from_open)
    day["bar_idx"] = day["mfo"] // OR_MINUTES
    agg = (
        day.groupby("bar_idx", as_index=False)
        .agg(
            minute=("minute", "first"),
            open=("open", "first"),
            high=("high", "max"),
            low=("low", "min"),
            close=("close", "last"),
            volume=("volume", "sum"),
        )
        .sort_values("minute")
        .reset_index(drop=True)
    )
    return agg


def session_vwap(day_1m: pd.DataFrame) -> pd.Series:
    day = filter_session(day_1m).sort_values("minute").reset_index(drop=True)
    typical = (day["high"] + day["low"] + day["close"]) / 3.0
    vol = day["volume"].fillna(0).astype(float)
    cum_pv = (typical * vol).cumsum()
    cum_v = vol.cumsum().replace(0, np.nan)
    return cum_pv / cum_v


def load_1m_frames(
    conn: sqlite3.Connection,
    stock_id: str,
    *,
    source: str | None = None,
) -> pd.DataFrame:
    sources = (source,) if source else ("finmind", "yahoo")
    rows: list[sqlite3.Row] = []
    for src in sources:
        rows = conn.execute(
            """
            SELECT trade_date, minute, open, high, low, close, volume
            FROM stock_kbar_1m
            WHERE stock_id = ? AND source = ?
            ORDER BY trade_date, minute
            """,
            (stock_id, src),
        ).fetchall()
        if rows:
            break
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=["trade_date", "minute", "open", "high", "low", "close", "volume"])
    return filter_session(df)


def _load_stock_ids(
    conn: sqlite3.Connection,
    spec: UniverseSpec | None = None,
) -> list[str]:
    """Return stock IDs for the given universe spec."""
    spec = spec or UniverseSpec.finmind_cross_section()
    if spec.mode == "single":
        if not spec.stock_id:
            raise ValueError("single mode requires stock_id")
        return [spec.stock_id]
    exclude = ", ".join("?" * len(spec.exclude_ids))
    rows = conn.execute(
        f"""
        SELECT stock_id, COUNT(DISTINCT trade_date) AS days
        FROM stock_kbar_1m
        WHERE source = 'finmind'
          AND stock_id NOT IN ({exclude})
          AND trade_date >= ?
        GROUP BY stock_id
        HAVING days >= ?
        ORDER BY days DESC
        """,
        (*spec.exclude_ids, spec.continuous_start, spec.min_continuous_days),
    ).fetchall()
    return [r["stock_id"] for r in rows]


def _load_stock_1m(
    conn: sqlite3.Connection,
    stock_id: str,
    *,
    spec: UniverseSpec | None = None,
    continuous_start: str | None = None,
) -> pd.DataFrame:
    spec = spec or UniverseSpec.finmind_cross_section()
    start = continuous_start or spec.continuous_start
    rows = conn.execute(
        """
        SELECT trade_date, minute, open, high, low, close, volume
        FROM stock_kbar_1m
        WHERE stock_id = ? AND source = 'finmind' AND trade_date >= ?
        ORDER BY trade_date, minute
        """,
        (stock_id, start),
    ).fetchall()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=["trade_date", "minute", "open", "high", "low", "close", "volume"])
    return filter_session(df)


def _sanitize_intraday_json(obj: Any) -> Any:
    if isinstance(obj, float) and not np.isfinite(obj):
        return None
    if isinstance(obj, dict):
        return {k: _sanitize_intraday_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_intraday_json(v) for v in obj]
    return obj


def write_intraday_research_json(result: dict[str, Any], out_path: Path) -> None:
    """Write research JSON · NaN/Inf → null."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(_sanitize_intraday_json(result), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


BENCHMARK_DAILY_SOURCES = ("finmind", "tej", "yahoo")


def load_daily_bars(
    conn: sqlite3.Connection,
    code: str,
    *,
    sources: tuple[str, ...] = BENCHMARK_DAILY_SOURCES,
) -> pd.DataFrame:
    merged: dict[str, dict[str, float]] = {}
    for src in sources:
        rows = conn.execute(
            """
            SELECT date AS trade_date, open, high, low, close, volume
            FROM daily_bars
            WHERE code = ? AND source = ?
            ORDER BY date
            """,
            (code, src),
        ).fetchall()
        for row in rows:
            td = str(row["trade_date"])
            if td not in merged:
                merged[td] = {
                    "open": row["open"],
                    "high": row["high"],
                    "low": row["low"],
                    "close": row["close"],
                    "volume": row["volume"],
                }
    if not merged:
        return pd.DataFrame()
    df = pd.DataFrame.from_dict(merged, orient="index")
    df.index.name = "trade_date"
    return df.sort_index()


def atr14(daily: pd.DataFrame, trade_date: str) -> float | None:
    if trade_date not in daily.index:
        return None
    idx = daily.index.get_loc(trade_date)
    if idx < 14:
        return None
    window = daily.iloc[idx - 14 : idx]
    tr = window["high"] - window["low"]
    return float(tr.mean())


def split_dates_is_oos(dates: list[str], ratio: float = IS_OOS_RATIO) -> str | None:
    if not dates:
        return None
    uniq = sorted(set(dates))
    split_idx = int(len(uniq) * ratio)
    if split_idx <= 0 or split_idx >= len(uniq):
        return None
    return uniq[split_idx]


def _size_shares(aum: float, entry: float, stop: float, leverage: float) -> int:
    if not np.isfinite(aum) or aum <= 0 or not np.isfinite(entry) or entry <= 0:
        return 0
    risk_per_share = abs(entry - stop)
    if not np.isfinite(risk_per_share) or risk_per_share <= 0:
        return 0
    by_risk = int(aum * RISK_PCT / risk_per_share)
    by_lev = int(leverage * aum / entry)
    return max(0, min(by_risk, by_lev))


StopMode = Literal["range", "atr5", "atr10"]
ExitMode = Literal["eod", "10r"]

# ──────────────────────────────────────────────────────────────────────────────
# Taiwan-adapted helpers (Track 3)
# ──────────────────────────────────────────────────────────────────────────────

TW_StopMode = Literal["range", "fixed1pct", "fixed2pct", "atr_self20", "atr_self50"]


def atr14_from_1m(k1m_all: pd.DataFrame, trade_date: str) -> float | None:
    """14-day intraday ATR (high-low per day) from the full 1m dataset.

    Uses prior-day data only (PIT-safe).  Returns None if fewer than 14
    trading days are available before trade_date.
    """
    past = k1m_all[k1m_all["trade_date"] < trade_date]
    if past.empty:
        return None
    day_ranges = past.groupby("trade_date").apply(
        lambda g: g["high"].max() - g["low"].min(), include_groups=False
    )
    dates_sorted = sorted(day_ranges.index)
    if len(dates_sorted) < 14:
        return None
    window = dates_sorted[-14:]
    return float(day_ranges.loc[window].mean())


def precompute_rvol(k1m_all: pd.DataFrame, or_minutes: int = 15) -> pd.Series:
    """Precompute rolling 14-day relative OR volume for all dates.

    Returns a Series indexed by trade_date with rvol values (NaN if < 14 prior days).
    Call once per variant to avoid O(n²) repeated filtering.
    """
    end_h = 9 + (or_minutes // 60)
    end_m = or_minutes % 60
    cutoff = f"{end_h:02d}:{end_m:02d}:00"

    sess = k1m_all[(k1m_all["minute"] >= SESSION_OPEN) & (k1m_all["minute"] < cutoff)]
    or_vols: pd.Series = sess.groupby("trade_date")["volume"].sum().sort_index()

    # Rolling 14-day average using prior days only (shift by 1)
    roll14 = or_vols.shift(1).rolling(14, min_periods=14).mean()
    rvol = or_vols / roll14
    return rvol  # NaN where not enough history


def or_end_minute(or_minutes: int) -> str:
    """Session-open-relative OR window end minute (exclusive)."""
    h_open = int(SESSION_OPEN[:2])
    m_open = int(SESSION_OPEN[3:5])
    end_total_minutes = h_open * 60 + m_open + or_minutes
    end_h = end_total_minutes // 60
    end_m = end_total_minutes % 60
    return f"{end_h:02d}:{end_m:02d}:00"


def compute_or_bars(day_1m: pd.DataFrame, or_minutes: int) -> pd.DataFrame:
    """Return 1m bars within the OR window [SESSION_OPEN, SESSION_OPEN+or_minutes)."""
    end_str = or_end_minute(or_minutes)
    return day_1m[(day_1m["minute"] >= SESSION_OPEN) & (day_1m["minute"] < end_str)].copy()


def passes_prev_day_bull_filter(
    prev_day_close: float | None,
    prev_day_open: float | None,
    enabled: bool,
) -> bool:
    if not enabled:
        return True
    if prev_day_close is None or prev_day_open is None:
        return False
    return prev_day_close > prev_day_open


def _find_long_breakout_entry(
    day: pd.DataFrame,
    *,
    or_high: float,
    or_end_str: str,
) -> tuple[float, str] | None:
    post_or = day[day["minute"] >= or_end_str].reset_index(drop=True)
    if post_or.empty:
        return None
    for _, bar in post_or.iterrows():
        if float(bar["high"]) >= or_high:
            entry = max(float(bar["open"]), or_high)
            return entry, str(bar["minute"])
    return None


def _simulate_long_range_stop_eod(
    day: pd.DataFrame,
    *,
    entry: float,
    entry_minute: str,
    stop: float,
    cost_rt: float,
) -> dict[str, Any]:
    risk = entry - stop
    rest = day[day["minute"] >= entry_minute].reset_index(drop=True)
    exit_px = float(day.iloc[-1]["close"])
    exit_reason = "eod"
    for _, bar in rest.iterrows():
        if float(bar["low"]) <= stop:
            exit_px, exit_reason = stop, "stop"
            break
    gross = (exit_px - entry) / entry
    net = gross - cost_rt
    return {
        "gross_pct": gross * 100,
        "net_pct": net * 100,
        "exit": exit_reason,
        "r_mult": (exit_px - entry) / risk if risk > 0 else 0.0,
    }


def simulate_orb_long_range_pooled(
    day_1m: pd.DataFrame,
    *,
    or_minutes: int,
    min_or_range_pct: float,
    prev_day_close: float | None,
    prev_day_open: float | None,
    prev_day_bull_filter: bool,
    cost_rt: float,
    min_or_bars_divisor: int = 3,
) -> dict[str, Any] | None:
    """Track-4 pooled per-trade ORB long · range stop · EOD exit."""
    day = day_1m.sort_values("minute").reset_index(drop=True)
    or_bars = compute_or_bars(day, or_minutes)
    if len(or_bars) < max(1, or_minutes // min_or_bars_divisor):
        return None

    or_high = float(or_bars["high"].max())
    or_low = float(or_bars["low"].min())
    day_open = float(or_bars.iloc[0]["open"])
    if day_open <= 0 or or_high <= 0:
        return None

    or_range_pct = (or_high - or_low) / day_open
    if or_range_pct < min_or_range_pct:
        return None

    if not passes_prev_day_bull_filter(prev_day_close, prev_day_open, prev_day_bull_filter):
        return None

    entry_pair = _find_long_breakout_entry(day, or_high=or_high, or_end_str=or_end_minute(or_minutes))
    if entry_pair is None:
        return None
    entry, entry_minute = entry_pair

    stop = or_low
    if entry - stop <= 0:
        return None

    result = _simulate_long_range_stop_eod(
        day,
        entry=entry,
        entry_minute=entry_minute,
        stop=stop,
        cost_rt=cost_rt,
    )
    result["or_range_pct"] = or_range_pct * 100
    return result


def _aggregate_or_window(day_1m: pd.DataFrame, or_minutes: int) -> pd.DataFrame:
    """Aggregate 1m bars into the OR window [SESSION_OPEN, SESSION_OPEN+or_minutes)."""
    return compute_or_bars(day_1m, or_minutes)


def simulate_orb_tw_session(
    day_1m: pd.DataFrame,
    *,
    trade_date: str,
    prev_close: float | None = None,
    rvol_today: float | None = None,
    atr14_val: float | None = None,
    or_minutes: int = 15,
    stop_mode: TW_StopMode = "range",
    gap_filter_pct: float = 0.0,
    rvol_min: float = 0.0,
    min_or_range_pct: float = 0.0,
    aum: float = INITIAL_AUM_NTD,
    leverage: float = DEFAULT_LEVERAGE,
    cost_rt: float = DEFAULT_COST_RT,
) -> TradeRecord | None:
    """Taiwan-adapted ORB · long-only · breakout (stop-buy) entry.

    Key differences from the QQQ spec:
    - Long-only (no shorts; ETF bull-market assumption)
    - Breakout entry: enter only when price pierces OR_high after OR close
    - OR window: configurable (15 or 30 min by default)
    - Stop: range / fixed-pct / ATR derived from the stock's own 1m data
    - Optional filters: gap-up, minimum OR range, Relative Volume

    Pass pre-computed rvol_today and atr14_val to avoid O(n²) per-call computation.
    """
    day = filter_session(day_1m).sort_values("minute").reset_index(drop=True)
    or_bars = _aggregate_or_window(day, or_minutes)
    if len(or_bars) < max(1, or_minutes // 2):
        return None

    or_high = float(or_bars["high"].max())
    or_low = float(or_bars["low"].min())
    day_open = float(or_bars.iloc[0]["open"])

    if day_open <= 0 or or_high <= 0:
        return None

    or_range_pct = (or_high - or_low) / day_open
    if or_range_pct < min_or_range_pct:
        return None

    # Gap filter (long-only → require gap-up if threshold > 0)
    if gap_filter_pct > 0:
        if prev_close is None or prev_close <= 0:
            return None
        gap = (day_open - prev_close) / prev_close
        if gap < gap_filter_pct:
            return None

    # Relative volume filter (use precomputed rvol_today)
    if rvol_min > 0:
        if rvol_today is None or np.isnan(rvol_today) or rvol_today < rvol_min:
            return None

    # Find breakout bar (first bar after OR that pierces OR_high)
    entry_pair = _find_long_breakout_entry(day, or_high=or_high, or_end_str=or_end_minute(or_minutes))
    if entry_pair is None:
        return None
    entry, entry_minute = entry_pair

    # Stop loss
    if stop_mode == "range":
        stop = or_low
    elif stop_mode == "fixed1pct":
        stop = entry * 0.99
    elif stop_mode == "fixed2pct":
        stop = entry * 0.98
    elif stop_mode in ("atr_self20", "atr_self50"):
        atr_val = atr14_val
        if atr_val is None or atr_val <= 0:
            return None
        mult = 0.20 if stop_mode == "atr_self20" else 0.50
        stop = entry - mult * atr_val
    else:
        return None

    risk = entry - stop
    if risk <= 0:
        return None

    # Simulate bar-by-bar from entry bar
    rest = day[day["minute"] >= entry_minute].reset_index(drop=True)
    exit_px = float(day.iloc[-1]["close"])
    exit_reason = "eod"

    for _, bar in rest.iterrows():
        lo = float(bar["low"])
        if lo <= stop:
            exit_px, exit_reason = stop, "stop"
            break

    gross = (exit_px - entry) / entry
    net = gross - cost_rt
    shares = _size_shares(aum, entry, stop, leverage)
    pnl = shares * (exit_px - entry) - shares * entry * cost_rt

    return TradeRecord(
        trade_date=trade_date,
        direction="long",
        entry=entry,
        exit=exit_px,
        stop=stop,
        gross_pct=gross,
        net_pct=net,
        r_multiple=(exit_px - entry) / risk if risk > 0 else 0.0,
        exit_reason=exit_reason,
        shares=shares,
        pnl_ntd=pnl,
    )


def simulate_orb_qqq_session(
    day_1m: pd.DataFrame,
    *,
    trade_date: str,
    atr14_val: float | None,
    stop_mode: StopMode = "range",
    exit_mode: ExitMode = "eod",
    aum: float = INITIAL_AUM_NTD,
    leverage: float = DEFAULT_LEVERAGE,
    cost_rt: float = DEFAULT_COST_RT,
    allow_short: bool = True,
) -> TradeRecord | None:
    """2023 QQQ ORB spec · TW session · second 5m bar market entry."""
    bars5 = aggregate_5m(day_1m)
    if len(bars5) < 2:
        return None

    or_bar = bars5.iloc[0]
    entry_bar = bars5.iloc[1]
    if or_bar["open"] <= 0 or entry_bar["open"] <= 0:
        return None

    bullish = or_bar["close"] > or_bar["open"]
    bearish = or_bar["close"] < or_bar["open"]
    if not bullish and not bearish:
        return None

    direction: Literal["long", "short"] = "long" if bullish else "short"
    if direction == "short" and not allow_short:
        return None

    entry = float(entry_bar["open"])
    entry_minute = str(entry_bar["minute"])

    if stop_mode == "range":
        stop = float(or_bar["low"]) if direction == "long" else float(or_bar["high"])
    elif stop_mode in ("atr5", "atr10"):
        if atr14_val is None or atr14_val <= 0:
            return None
        mult = 0.05 if stop_mode == "atr5" else 0.10
        dist = mult * atr14_val
        stop = entry - dist if direction == "long" else entry + dist
    else:
        return None

    risk = abs(entry - stop)
    if risk <= 0:
        return None

    target: float | None = None
    if exit_mode == "10r":
        target = entry + 10 * risk if direction == "long" else entry - 10 * risk

    day = filter_session(day_1m)
    rest = day[day["minute"] >= entry_minute].reset_index(drop=True)
    exit_px = float(day.iloc[-1]["close"])
    exit_reason = "eod"

    for _, bar in rest.iterrows():
        hi, lo = float(bar["high"]), float(bar["low"])
        if direction == "long":
            if lo <= stop:
                exit_px, exit_reason = stop, "stop"
                break
            if target is not None and hi >= target:
                exit_px, exit_reason = target, "target_10r"
                break
        else:
            if hi >= stop:
                exit_px, exit_reason = stop, "stop"
                break
            if target is not None and lo <= target:
                exit_px, exit_reason = target, "target_10r"
                break

    gross = (exit_px - entry) / entry if direction == "long" else (entry - exit_px) / entry
    net = gross - cost_rt
    r_mult = ((exit_px - entry) / risk) if direction == "long" else ((entry - exit_px) / risk)
    shares = _size_shares(aum, entry, stop, leverage)
    pnl = shares * (exit_px - entry) if direction == "long" else shares * (entry - exit_px)
    pnl -= shares * entry * cost_rt

    return TradeRecord(
        trade_date=trade_date,
        direction=direction,
        entry=entry,
        exit=exit_px,
        stop=stop,
        gross_pct=gross,
        net_pct=net,
        r_multiple=r_mult,
        exit_reason=exit_reason,
        shares=shares,
        pnl_ntd=pnl,
    )


def summarize_trades(
    trades: list[TradeRecord],
    *,
    split_date: str | None,
    label: str,
    oos_min_n: int = TRACK2_OOS_MIN_N,
) -> dict[str, Any]:
    if not trades:
        return {"label": label, "n": 0, "verdict": "NO TRADES", "is": {}, "oos": {}}

    rows = [
        {
            "date": t.trade_date,
            "gross": t.gross_pct,
            "net": t.net_pct,
            "r": t.r_multiple,
            "pnl": t.pnl_ntd,
            "exit": t.exit_reason,
            "dir": t.direction,
        }
        for t in trades
    ]
    df = pd.DataFrame(rows).sort_values("date").reset_index(drop=True)

    if split_date is None:
        split_date = split_dates_is_oos(df["date"].tolist()) or df.iloc[int(len(df) * IS_OOS_RATIO)]["date"]

    is_df = df[df["date"] < split_date]
    oos_df = df[df["date"] >= split_date]

    def _block(t: pd.DataFrame) -> dict[str, Any]:
        if t.empty:
            return {}
        net = t["net"]
        cum = float((1 + net).prod() - 1)
        avg_r = t["r"].replace([np.inf, -np.inf], np.nan).dropna()
        return {
            "n": int(len(t)),
            "date_start": str(t["date"].iloc[0]),
            "date_end": str(t["date"].iloc[-1]),
            "win_rate_pct": round(float((net > 0).mean() * 100), 1),
            "avg_gross_pct": round(float(t["gross"].mean() * 100), 3),
            "avg_net_pct": round(float(net.mean() * 100), 3),
            "total_return_pct": round(cum * 100, 2),
            "sharpe": round(float(net.mean() / (net.std() + 1e-9) * (252**0.5)), 2),
            "avg_r": round(float(avg_r.mean()), 3) if len(avg_r) else None,
            "stop_rate_pct": round(float((t["exit"] == "stop").mean() * 100), 1),
        }

    is_s = _block(is_df)
    oos_s = _block(oos_df)
    n_oos = oos_s.get("n", 0)
    avg_net_oos = oos_s.get("avg_net_pct", -99)
    verdict = "PASS" if n_oos >= oos_min_n and avg_net_oos > 0 else "FAIL"

    cap_hit = sum(1 for t in trades if t.shares > 0 and t.shares == int(DEFAULT_LEVERAGE * INITIAL_AUM_NTD / t.entry))

    return {
        "label": label,
        "n": len(df),
        "split_date": split_date,
        "is": is_s,
        "oos": oos_s,
        "verdict": verdict,
        "leverage_cap_hits": cap_hit,
    }


def benchmark_buy_hold(
    daily: pd.DataFrame,
    dates: list[str],
    *,
    split_date: str | None,
) -> dict[str, Any]:
    if daily.empty or not dates:
        return {}
    sub = daily.loc[daily.index.isin(dates)].sort_index()
    if len(sub) < 2:
        return {}
    rets = sub["close"].pct_change().dropna()
    if split_date:
        is_r = rets[rets.index < split_date]
        oos_r = rets[rets.index >= split_date]
    else:
        split_date = split_dates_is_oos(list(rets.index)) or rets.index[int(len(rets) * IS_OOS_RATIO)]
        is_r = rets[rets.index < split_date]
        oos_r = rets[rets.index >= split_date]

    def _bh(r: pd.Series) -> dict[str, float]:
        if r.empty:
            return {}
        total = float((1 + r).prod() - 1)
        return {
            "n_days": int(len(r)),
            "total_return_pct": round(total * 100, 2),
            "sharpe": round(float(r.mean() / (r.std() + 1e-9) * (252**0.5)), 2),
        }

    return {
        "split_date": split_date,
        "is": _bh(is_r),
        "oos": _bh(oos_r),
        "full": _bh(rets),
    }


def kbar_coverage(conn: sqlite3.Connection, stock_id: str) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT MIN(trade_date) AS d0, MAX(trade_date) AS d1,
               COUNT(DISTINCT trade_date) AS nd, COUNT(*) AS nb
        FROM stock_kbar_1m WHERE stock_id = ?
        """,
        (stock_id,),
    ).fetchone()
    if not row or not row["d0"]:
        return {"stock_id": stock_id, "days": 0, "bars": 0}
    return {
        "stock_id": stock_id,
        "min_date": row["d0"],
        "max_date": row["d1"],
        "days": int(row["nd"] or 0),
        "bars": int(row["nb"] or 0),
    }


def day_1m_from_db(conn: sqlite3.Connection, stock_id: str, trade_date: str) -> pd.DataFrame:
    bars = load_kbar_day_bars(conn, stock_id, trade_date)
    if not bars:
        return pd.DataFrame()
    return pd.DataFrame(
        {
            "minute": [b.minute for b in bars],
            "open": [b.open for b in bars],
            "high": [b.high for b in bars],
            "low": [b.low for b in bars],
            "close": [b.close for b in bars],
            "volume": [b.volume for b in bars],
        }
    )
