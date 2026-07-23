"""Rolling 5m poll · TW↔NQ detach/repair strategies · avoided-loss study.

Panel: Yahoo ^TWII × NQ=F 5m (~60d). Case overlay: 1m when available.
Metric: notional long-from-open; stop at first DETACHED; savings vs hold-to-close / vs trough.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from research.backtest.us_tw_divergence_sell_gate_study import (
    TZ_ET,
    TZ_TW,
    fetch_yahoo_ohlc,
    slice_tw_session,
    slice_us_during_tw,
)

NOTIONAL = 1_000_000.0  # NTD for "止損金額" reporting
TW_OPEN = "09:00"
TW_CLOSE = "13:30"


@dataclass(frozen=True)
class StrategySpec:
    strategy_id: str
    name: str
    # detach rule
    cum_thr: float | None = None  # cum_spread <= thr
    win_thr: float | None = None  # spread_win <= thr
    corr_thr: float | None = None  # corr <= thr and tw_win < 0
    confirm_polls: int = 1  # consecutive polls required
    # repair / reentry (optional)
    reenter_on_repair: bool = False
    repair_win_thr: float = 0.25
    window_min: int = 5  # 5 or 10 lookback


def _ret(a: float, b: float) -> float:
    if not (a > 0 and b > 0):
        return float("nan")
    return (b - a) / a * 100.0


def build_poll_frame_5m(
    session_date: str,
    tw_5m: pd.DataFrame,
    nq_5m: pd.DataFrame,
    *,
    window_min: int = 5,
) -> pd.DataFrame | None:
    tw = slice_tw_session(tw_5m, session_date)
    nq = slice_us_during_tw(nq_5m, session_date)
    if len(tw) < 4 or len(nq) < 3:
        return None

    # resample NQ onto TW bar ends (asof)
    tw = tw.copy()
    if tw.index.tz is None:
        tw.index = tw.index.tz_localize(TZ_TW)
    else:
        tw.index = tw.index.tz_convert(TZ_TW)
    nq = nq.copy()
    if nq.index.tz is None:
        nq.index = nq.index.tz_localize(TZ_ET)
    else:
        nq.index = nq.index.tz_convert(TZ_ET)
    nq_tw = nq.copy()
    nq_tw.index = nq_tw.index.tz_convert(TZ_TW)
    nq_close = nq_tw["close"].sort_index()
    # align each TW bar to last NQ <= bar time
    nq_aligned = []
    for ts in tw.index:
        sub = nq_close.loc[:ts]
        nq_aligned.append(float(sub.iloc[-1]) if len(sub) else float("nan"))
    out = pd.DataFrame(
        {
            "tw_open": tw["open"].astype(float).values,
            "tw_high": tw["high"].astype(float).values,
            "tw_low": tw["low"].astype(float).values,
            "tw_close": tw["close"].astype(float).values,
            "nq_close": nq_aligned,
        },
        index=tw.index,
    ).dropna()
    if len(out) < 4:
        return None

    tw0 = float(out["tw_open"].iloc[0])
    nq0 = float(out["nq_close"].iloc[0])
    out["tw_from_open"] = (out["tw_close"] / tw0 - 1.0) * 100.0
    out["nq_from_open"] = (out["nq_close"] / nq0 - 1.0) * 100.0
    out["cum_spread"] = out["tw_from_open"] - out["nq_from_open"]
    out["tw_bar"] = out["tw_close"].pct_change() * 100.0
    out["nq_bar"] = out["nq_close"].pct_change() * 100.0

    n_bars = max(1, window_min // 5)
    rows = []
    for i in range(n_bars, len(out)):
        win = out.iloc[i - n_bars : i + 1]
        # window returns from first open/close pair in lookback
        tw_w = _ret(float(win["tw_close"].iloc[0]), float(win["tw_close"].iloc[-1]))
        nq_w = _ret(float(win["nq_close"].iloc[0]), float(win["nq_close"].iloc[-1]))
        # better: from start-of-window open
        tw_w = _ret(float(win["tw_open"].iloc[0]), float(win["tw_close"].iloc[-1]))
        nq_w = _ret(float(win["nq_close"].iloc[0]), float(win["nq_close"].iloc[-1]))
        a = win["tw_bar"].dropna()
        b = win["nq_bar"].reindex(a.index).dropna()
        common = a.index.intersection(b.index)
        corr = float(a.loc[common].corr(b.loc[common])) if len(common) >= 3 else float("nan")
        # same-sign direction rate within lookback (sync proxy)
        if len(common) >= 2:
            sa = np.sign(a.loc[common].to_numpy())
            sb = np.sign(b.loc[common].to_numpy())
            # ignore flat bars in either series
            mask = (sa != 0) & (sb != 0)
            same_sign = float(np.mean(sa[mask] == sb[mask])) if mask.any() else float("nan")
        else:
            same_sign = float("nan")
        ts = out.index[i]
        rows.append(
            {
                "ts": ts,
                "poll": ts.strftime("%H:%M"),
                "tw_px": float(out["tw_close"].iloc[i]),
                "nq_px": float(out["nq_close"].iloc[i]),
                "tw_from_open": float(out["tw_from_open"].iloc[i]),
                "nq_from_open": float(out["nq_from_open"].iloc[i]),
                "cum_spread": float(out["cum_spread"].iloc[i]),
                "tw_win": tw_w,
                "nq_win": nq_w,
                "spread_win": tw_w - nq_w if math.isfinite(tw_w) and math.isfinite(nq_w) else float("nan"),
                "corr": corr,
                "same_sign": same_sign,
            }
        )
    if not rows:
        return None
    poll = pd.DataFrame(rows)
    # session extremes from bar highs/lows
    trough_from_open = float((out["tw_low"].min() / tw0 - 1.0) * 100.0)
    close_from_open = float(out["tw_from_open"].iloc[-1])
    peak_to_trough = float(
        (out["tw_high"].max() - out["tw_low"].min()) / out["tw_high"].max() * 100.0
    )
    poll.attrs["tw0"] = tw0
    poll.attrs["close_from_open"] = close_from_open
    poll.attrs["trough_from_open"] = trough_from_open
    poll.attrs["peak_to_trough"] = peak_to_trough
    poll.attrs["date"] = session_date
    return poll


def _detach_mask(poll: pd.DataFrame, spec: StrategySpec) -> pd.Series:
    ok = pd.Series(True, index=poll.index)
    parts = []
    if spec.cum_thr is not None:
        parts.append(poll["cum_spread"] <= spec.cum_thr)
    if spec.win_thr is not None:
        parts.append(poll["spread_win"] <= spec.win_thr)
    if spec.corr_thr is not None:
        parts.append((poll["corr"] <= spec.corr_thr) & (poll["tw_win"] < 0))
    if not parts:
        return pd.Series(False, index=poll.index)
    # OR across configured conditions (any severity channel)
    det = parts[0]
    for p in parts[1:]:
        det = det | p
    # consecutive confirm
    if spec.confirm_polls <= 1:
        return det.fillna(False)
    run = det.astype(int).groupby((~det).cumsum()).cumsum()
    return (run >= spec.confirm_polls).fillna(False)


def simulate_day(poll: pd.DataFrame, spec: StrategySpec) -> dict[str, Any]:
    """Long from open; flatten at first detach; optional reenter on repair."""
    close_r = float(poll.attrs["close_from_open"])
    trough_r = float(poll.attrs["trough_from_open"])
    ptt = float(poll.attrs["peak_to_trough"])
    date_s = str(poll.attrs["date"])

    det = _detach_mask(poll, spec)
    detach_idxs = list(poll.index[det])
    if not detach_idxs:
        # no stop → same as hold
        pnl_strat = close_r
        saved_vs_close = 0.0
        saved_vs_trough = max(0.0, (-trough_r) - max(0.0, -close_r))  # no early stop
        return {
            "date": date_s,
            "strategy_id": spec.strategy_id,
            "triggered": False,
            "detach_poll": None,
            "detach_tw_from_open": None,
            "cum_spread_at_detach": None,
            "corr_at_detach": None,
            "spread_win_at_detach": None,
            "reentered": False,
            "reenter_poll": None,
            "pnl_hold_close_pct": round(close_r, 4),
            "pnl_strat_pct": round(pnl_strat, 4),
            "trough_from_open_pct": round(trough_r, 4),
            "peak_to_trough_pct": round(ptt, 4),
            "saved_vs_close_pct": 0.0,
            "saved_vs_trough_pct": round(max(0.0, -trough_r - max(0.0, -close_r)), 4),
            "saved_vs_close_ntd": 0.0,
            "saved_vs_trough_ntd": round(max(0.0, -trough_r - max(0.0, -close_r)) / 100.0 * NOTIONAL, 2),
            "false_alarm_opp_cost_pct": 0.0,
            "false_alarm_opp_cost_ntd": 0.0,
        }

    i0 = detach_idxs[0]
    row0 = poll.loc[i0]
    exit_r = float(row0["tw_from_open"])
    reentered = False
    reenter_poll = None
    pnl_strat = exit_r

    if spec.reenter_on_repair:
        after = poll.loc[i0:]
        repair = (after["spread_win"] >= spec.repair_win_thr) & (
            after["cum_spread"] > float(row0["cum_spread"]) + 0.3
        )
        hits = np.flatnonzero(repair.fillna(False).to_numpy())
        if len(hits):
            j = repair.index[int(hits[0])]
            reentered = True
            reenter_poll = str(poll.loc[j, "poll"])
            re_r = float(poll.loc[j, "tw_from_open"])
            pnl_strat = exit_r + (close_r - re_r)

    # savings: reduction in loss vs hold-to-close
    loss_hold = max(0.0, -close_r)
    loss_stop = max(0.0, -exit_r)
    loss_strat = max(0.0, -pnl_strat)
    saved_vs_close = loss_hold - loss_strat
    loss_trough = max(0.0, -trough_r)
    saved_vs_trough = loss_trough - loss_stop
    opp = max(0.0, close_r - exit_r) if (not reentered and close_r > exit_r) else 0.0

    return {
        "date": date_s,
        "strategy_id": spec.strategy_id,
        "triggered": True,
        "detach_poll": str(row0["poll"]),
        "detach_tw_from_open": round(exit_r, 4),
        "cum_spread_at_detach": round(float(row0["cum_spread"]), 4),
        "corr_at_detach": None if not math.isfinite(float(row0["corr"])) else round(float(row0["corr"]), 4),
        "spread_win_at_detach": None
        if not math.isfinite(float(row0["spread_win"]))
        else round(float(row0["spread_win"]), 4),
        "reentered": reentered,
        "reenter_poll": reenter_poll,
        "pnl_hold_close_pct": round(close_r, 4),
        "pnl_strat_pct": round(pnl_strat, 4),
        "trough_from_open_pct": round(trough_r, 4),
        "peak_to_trough_pct": round(ptt, 4),
        "saved_vs_close_pct": round(saved_vs_close, 4),
        "saved_vs_trough_pct": round(saved_vs_trough, 4),
        "saved_vs_close_ntd": round(saved_vs_close / 100.0 * NOTIONAL, 2),
        "saved_vs_trough_ntd": round(saved_vs_trough / 100.0 * NOTIONAL, 2),
        "false_alarm_opp_cost_pct": round(opp, 4),
        "false_alarm_opp_cost_ntd": round(opp / 100.0 * NOTIONAL, 2),
    }


def strategy_catalog(window_min: int = 5) -> list[StrategySpec]:
    specs: list[StrategySpec] = []
    # cum-only
    for thr in (-0.6, -0.8, -1.0, -1.2, -1.5):
        specs.append(
            StrategySpec(
                f"cum{thr}_w{window_min}",
                f"DETACH cum≤{thr} · win={window_min}m",
                cum_thr=thr,
                window_min=window_min,
            )
        )
        specs.append(
            StrategySpec(
                f"cum{thr}_c2_w{window_min}",
                f"DETACH cum≤{thr}×2poll · win={window_min}m",
                cum_thr=thr,
                confirm_polls=2,
                window_min=window_min,
            )
        )
    # win-only
    for thr in (-0.4, -0.6, -0.8, -1.0):
        specs.append(
            StrategySpec(
                f"win{thr}_w{window_min}",
                f"DETACH spread_win≤{thr} · win={window_min}m",
                win_thr=thr,
                window_min=window_min,
            )
        )
    # corr detach
    for thr in (0.0, 0.2):
        specs.append(
            StrategySpec(
                f"corr{thr}_w{window_min}",
                f"DETACH corr≤{thr} & tw_win<0 · win={window_min}m",
                corr_thr=thr,
                window_min=window_min,
            )
        )
    # combos (OR)
    specs.append(
        StrategySpec(
            f"combo_cum1_win06_w{window_min}",
            f"DETACH cum≤-1.0 OR win≤-0.6 · win={window_min}m",
            cum_thr=-1.0,
            win_thr=-0.6,
            window_min=window_min,
        )
    )
    specs.append(
        StrategySpec(
            f"combo_cum08_corr02_w{window_min}",
            f"DETACH cum≤-0.8 OR (corr≤0.2&tw↓) · win={window_min}m",
            cum_thr=-0.8,
            corr_thr=0.2,
            window_min=window_min,
        )
    )
    # with reentry
    specs.append(
        StrategySpec(
            f"cum-1.0_reenter_w{window_min}",
            f"DETACH cum≤-1.0 + REENTER repair · win={window_min}m",
            cum_thr=-1.0,
            reenter_on_repair=True,
            repair_win_thr=0.25,
            window_min=window_min,
        )
    )
    specs.append(
        StrategySpec(
            f"combo_cum1_win06_reenter_w{window_min}",
            f"DETACH cum≤-1 OR win≤-0.6 + REENTER · win={window_min}m",
            cum_thr=-1.0,
            win_thr=-0.6,
            reenter_on_repair=True,
            window_min=window_min,
        )
    )
    return specs


def score_strategy(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {}
    df = pd.DataFrame(rows)
    n = len(df)
    trig = df[df["triggered"]]
    # event days: peak_to_trough >= 3
    ev = df[df["peak_to_trough_pct"] >= 3.0]
    nev = df[df["peak_to_trough_pct"] < 3.0]
    net_saved_close = float(df["saved_vs_close_ntd"].sum())
    net_saved_trough = float(df["saved_vs_trough_ntd"].sum())
    if "false_alarm_opp_cost_ntd" in df.columns:
        opp = float(df["false_alarm_opp_cost_ntd"].fillna(0).sum())
    else:
        opp = float((df["false_alarm_opp_cost_pct"].fillna(0) / 100.0 * NOTIONAL).sum())
    net_edge_ntd = net_saved_close - opp
    return {
        "n_days": n,
        "n_triggers": int(df["triggered"].sum()),
        "trigger_rate": round(float(df["triggered"].mean()), 4),
        "n_events3": int(len(ev)),
        "event_recall": round(float(ev["triggered"].mean()), 4) if len(ev) else None,
        "false_trigger_rate_on_nonevent": round(float(nev["triggered"].mean()), 4) if len(nev) else None,
        "sum_saved_vs_close_ntd": round(net_saved_close, 2),
        "sum_saved_vs_trough_ntd": round(net_saved_trough, 2),
        "sum_opp_cost_ntd": round(opp, 2),
        "net_edge_ntd": round(net_edge_ntd, 2),  # primary objective
        "mean_saved_vs_close_pct_on_trig": round(float(trig["saved_vs_close_pct"].mean()), 4)
        if len(trig)
        else None,
        "median_detach_poll": None
        if trig.empty or trig["detach_poll"].isna().all()
        else str(trig["detach_poll"].mode().iloc[0])
        if len(trig["detach_poll"].mode())
        else None,
    }


# Yahoo chart symbol → local stock_kbar_5m id (Detach Gate / US–TW 5m studies)
_YAHOO_5M_LOCAL_ID: dict[str, str] = {
    "^TWII": "IX0001",  # FinMind has no IX0001 5m; Yahoo seed + daily accumulate
    "NQ=F": "NQ=F",  # Yahoo-only (FinMind no NQ futures 5m)
}
_LOCAL_5M_MIN_DAYS = 40  # prefer local when calendar coverage is long enough
# Prefer local only when the requested session day already has bars (TW wall clock).
# Avoids stale multi-week panels that stop at T-1 while live polls need T.
_LOCAL_5M_MIN_SESSION_BARS = 3


def load_local_5m_panel(stock_id: str, end: date) -> pd.DataFrame:
    """Load ``stock_kbar_5m`` as OHLC panel (lowercase cols).

    DB minutes are Asia/Taipei wall clock (Yahoo sync converts ET→TW on write).
    For US symbols (``NQ=F`` / ``ES=F``), convert timestamps back to ET so
    ``slice_us_during_tw`` keeps working.
    """
    from stock_db import DEFAULT_DB_PATH, connect

    conn = connect(DEFAULT_DB_PATH)
    try:
        rows = conn.execute(
            """
            SELECT trade_date, minute, open, high, low, close, volume, source
            FROM stock_kbar_5m
            WHERE stock_id = ? AND trade_date <= ?
            ORDER BY trade_date, minute,
              CASE source WHEN 'finmind' THEN 0 WHEN 'yahoo' THEN 1 ELSE 2 END
            """,
            (stock_id, end.isoformat()),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return pd.DataFrame()

    by_key: dict[tuple[str, str], Any] = {}
    for r in rows:
        key = (str(r["trade_date"]), str(r["minute"]))
        if key not in by_key:
            by_key[key] = r

    is_us = stock_id in ("NQ=F", "ES=F") or stock_id.endswith("=F")
    records: list[dict[str, float]] = []
    idx: list[pd.Timestamp] = []
    for (td, minute), r in sorted(by_key.items()):
        ts_tw = pd.Timestamp(f"{td} {minute}", tz=TZ_TW)
        ts = ts_tw.astimezone(TZ_ET) if is_us else ts_tw
        idx.append(ts)

        def _f(v: object) -> float:
            try:
                return float(v) if v is not None else float("nan")
            except (TypeError, ValueError):
                return float("nan")

        records.append(
            {
                "open": _f(r["open"]),
                "high": _f(r["high"]),
                "low": _f(r["low"]),
                "close": _f(r["close"]),
                "volume": _f(r["volume"]) if r["volume"] is not None else float("nan"),
            }
        )
    out = pd.DataFrame(records, index=pd.DatetimeIndex(idx))
    out.attrs["panel_source"] = f"local:{stock_id}"
    return out.sort_index()


def _panel_n_calendar_days(df: pd.DataFrame) -> int:
    if df is None or df.empty:
        return 0
    days: set[str] = set()
    for ts in df.index:
        t = pd.Timestamp(ts)
        if t.tzinfo is None:
            days.add(t.strftime("%Y-%m-%d"))
        else:
            days.add(t.tz_convert(TZ_TW).strftime("%Y-%m-%d"))
    return len(days)


def _panel_session_day_bar_count(df: pd.DataFrame, end: date) -> int:
    """Count bars whose Asia/Taipei calendar date equals ``end``.

    US futures panels are indexed in ET; converting to TW maps the TW cash
    session onto the correct session day (e.g. 21:00 ET prior evening → next
    TW morning).
    """
    if df is None or df.empty:
        return 0
    day = end.isoformat()
    n = 0
    for ts in df.index:
        t = pd.Timestamp(ts)
        if t.tzinfo is None:
            wall = t.strftime("%Y-%m-%d")
        else:
            wall = t.tz_convert(TZ_TW).strftime("%Y-%m-%d")
        if wall == day:
            n += 1
    return n


def _local_5m_panel_fresh_enough(local: pd.DataFrame, end: date) -> bool:
    """True when local history is long enough *and* covers the session day."""
    if local is None or local.empty:
        return False
    if _panel_n_calendar_days(local) < _LOCAL_5M_MIN_DAYS:
        return False
    return _panel_session_day_bar_count(local, end) >= _LOCAL_5M_MIN_SESSION_BARS


def fetch_yahoo_5m_panel(symbol: str, end: date) -> pd.DataFrame:
    """Prefer local ``stock_kbar_5m`` when fresh enough; else Yahoo ~60d live.

    Local is used only when calendar coverage ≥40d **and** the requested
    session day already has ≥3 bars (TW wall clock). Stale multi-week panels
    that stop at T-1 fall through to Yahoo so Detach Gate can poll intraday.

    TW leg: ``^TWII`` → local ``IX0001`` (Yahoo seed; FinMind index K unavailable).
    Liquid FinMind proxy ``0050`` is longer-history but not auto-substituted here so
    Detach Gate stays aligned with live index polls.
    """
    local_id = _YAHOO_5M_LOCAL_ID.get(symbol)
    if local_id:
        local = load_local_5m_panel(local_id, end)
        if _local_5m_panel_fresh_enough(local, end):
            end_ts = pd.Timestamp(end.isoformat(), tz=local.index.tz) + pd.Timedelta(days=1)
            return local.loc[local.index < end_ts].sort_index()

    import yfinance as yf

    from research.backtest.us_tw_divergence_sell_gate_study import _flatten_yf

    df = yf.download(
        symbol,
        period="59d",
        interval="5m",
        progress=False,
        auto_adjust=False,
        threads=False,
    )
    out = _flatten_yf(df)
    if out.empty:
        return out
    idx = pd.to_datetime(out.index)
    if idx.tz is None:
        if symbol.startswith("^TW") or symbol.endswith(".TW"):
            idx = idx.tz_localize(TZ_TW)
        else:
            idx = idx.tz_localize(TZ_ET)
    else:
        if symbol.startswith("^TW") or symbol.endswith(".TW"):
            idx = idx.tz_convert(TZ_TW)
        else:
            idx = idx.tz_convert(TZ_ET)
    out.index = idx
    # clip to date_end inclusive
    end_ts = pd.Timestamp(end.isoformat(), tz=out.index.tz) + pd.Timedelta(days=1)
    out = out.loc[out.index < end_ts]
    out.attrs["panel_source"] = f"yahoo_live:{symbol}"
    return out.sort_index()


def run_study(
    *,
    date_end: str | None = None,
    out_dir: Path,
    case_dates: list[str] | None = None,
) -> dict[str, Any]:
    end_d = date.fromisoformat(date_end) if date_end else date.today()
    out_dir.mkdir(parents=True, exist_ok=True)
    fig_dir = out_dir / "figs"
    fig_dir.mkdir(parents=True, exist_ok=True)

    tw = fetch_yahoo_5m_panel("^TWII", end_d)
    nq = fetch_yahoo_5m_panel("NQ=F", end_d)
    if tw.empty or nq.empty:
        raise RuntimeError("Yahoo 5m panel empty for ^TWII or NQ=F")

    # calendar
    local = tw.copy()
    if local.index.tz is None:
        local.index = local.index.tz_localize(TZ_TW)
    else:
        local.index = local.index.tz_convert(TZ_TW)
    dates = sorted(
        {
            ts.strftime("%Y-%m-%d")
            for ts in local.index
            if TW_OPEN <= ts.strftime("%H:%M") <= TW_CLOSE
        }
    )
    dates = [d for d in dates if len(slice_us_during_tw(nq, d)) >= 3]

    # default case list: HTML showcase + all ≥3% in panel
    if case_dates is None:
        case_dates = [
            "2026-07-14",
            "2026-07-07",
            "2026-06-08",
            "2026-06-26",
            "2026-06-05",
            "2026-05-28",
            "2026-06-10",
            "2026-06-11",
        ]

    specs = strategy_catalog(5) + strategy_catalog(10)
    # dedupe by id
    seen = set()
    uniq: list[StrategySpec] = []
    for s in specs:
        if s.strategy_id in seen:
            continue
        seen.add(s.strategy_id)
        uniq.append(s)
    specs = uniq

    # build polls per (date, window)
    polls_cache: dict[tuple[str, int], pd.DataFrame] = {}
    day_meta = []
    for d in dates:
        for w in (5, 10):
            pf = build_poll_frame_5m(d, tw, nq, window_min=w)
            if pf is not None:
                polls_cache[(d, w)] = pf
        if (d, 5) in polls_cache:
            pf = polls_cache[(d, 5)]
            day_meta.append(
                {
                    "date": d,
                    "peak_to_trough_pct": float(pf.attrs["peak_to_trough"]),
                    "close_from_open_pct": float(pf.attrs["close_from_open"]),
                    "trough_from_open_pct": float(pf.attrs["trough_from_open"]),
                    "is_event3": float(pf.attrs["peak_to_trough"]) >= 3.0,
                }
            )
    day_meta_df = pd.DataFrame(day_meta)

    # simulate all strategies
    all_rows: list[dict[str, Any]] = []
    summaries = []
    for spec in specs:
        rows = []
        for d in dates:
            pf = polls_cache.get((d, spec.window_min))
            if pf is None:
                continue
            rows.append(simulate_day(pf, spec))
        all_rows.extend(rows)
        sm = score_strategy(rows)
        sm["strategy_id"] = spec.strategy_id
        sm["name"] = spec.name
        sm["window_min"] = spec.window_min
        sm["confirm_polls"] = spec.confirm_polls
        sm["reenter"] = spec.reenter_on_repair
        sm["cum_thr"] = spec.cum_thr
        sm["win_thr"] = spec.win_thr
        sm["corr_thr"] = spec.corr_thr
        summaries.append(sm)

    sum_df = pd.DataFrame(summaries)
    if sum_df.empty or "net_edge_ntd" not in sum_df.columns:
        raise RuntimeError("No strategy rows — check 5m calendar overlap")
    sum_df = sum_df.sort_values("net_edge_ntd", ascending=False)
    best = sum_df.iloc[0].to_dict() if len(sum_df) else {}

    # case deep-dives for best + a few specs
    focus_ids = []
    if best:
        focus_ids.append(best["strategy_id"])
    for sid in (
        "cum-1.0_w5",
        "cum-1.0_c2_w5",
        "combo_cum1_win06_w5",
        "win-0.6_w5",
        "cum-1.0_reenter_w5",
    ):
        if sid in set(sum_df["strategy_id"]) and sid not in focus_ids:
            focus_ids.append(sid)

    case_payload = []
    for d in case_dates:
        if (d, 5) not in polls_cache:
            case_payload.append({"date": d, "present": False})
            continue
        pf = polls_cache[(d, 5)]
        entry = {
            "date": d,
            "present": True,
            "peak_to_trough_pct": float(pf.attrs["peak_to_trough"]),
            "close_from_open_pct": float(pf.attrs["close_from_open"]),
            "trough_from_open_pct": float(pf.attrs["trough_from_open"]),
            "polls": pf[["poll", "tw_from_open", "nq_from_open", "cum_spread", "spread_win", "corr"]]
            .round(3)
            .to_dict(orient="records"),
            "strategy_outcomes": {},
        }
        for sid in focus_ids:
            spec = next(s for s in specs if s.strategy_id == sid)
            pf_w = polls_cache.get((d, spec.window_min))
            if pf_w is None:
                continue
            entry["strategy_outcomes"][sid] = simulate_day(pf_w, spec)
        case_payload.append(entry)
        # plot cum_spread path
        _save_case_fig(fig_dir / f"case_{d.replace('-', '')}_poll5.png", pf, entry["strategy_outcomes"].get(focus_ids[0]) if focus_ids else None)

    # persist
    pd.DataFrame(all_rows).to_csv(out_dir / "day_strategy_pnl.csv", index=False)
    sum_df.to_csv(out_dir / "strategy_summary.csv", index=False)
    day_meta_df.to_csv(out_dir / "day_meta.csv", index=False)

    payload = {
        "meta": {
            "date_start": dates[0] if dates else None,
            "date_end": dates[-1] if dates else None,
            "n_days": len(dates),
            "n_events3": int(day_meta_df["is_event3"].sum()) if len(day_meta_df) else 0,
            "notional_ntd": NOTIONAL,
            "cadence": "5m bar poll",
            "sources": "^TWII 5m · NQ=F 5m Yahoo",
            "objective": "maximize net_edge_ntd = sum(saved_vs_close) − sum(false_alarm_opp_cost)",
        },
        "best_strategy": best,
        "top_strategies": sum_df.head(12).to_dict(orient="records"),
        "bottom_strategies": sum_df.tail(5).to_dict(orient="records"),
        "cases": case_payload,
        "day_meta": day_meta_df.to_dict(orient="records"),
        "plot_paths": sorted(str(p) for p in fig_dir.glob("case_*.png")),
    }
    md = render_markdown(payload)
    (out_dir / "report.md").write_text(md, encoding="utf-8")
    (out_dir / "summary.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    return {"payload": payload, "markdown": md}


def _save_case_fig(path: Path, pf: pd.DataFrame, best_out: dict[str, Any] | None) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(9, 4.2))
    x = list(range(len(pf)))
    ax.plot(x, pf["tw_from_open"], label="TW from open", color="#0b6e4f")
    ax.plot(x, pf["nq_from_open"], label="NQ from open", color="#c44e52", alpha=0.85)
    ax.plot(x, pf["cum_spread"], label="cum_spread", color="#2c3e50", ls="--")
    if best_out and best_out.get("detach_poll"):
        hits = pf.index[pf["poll"] == best_out["detach_poll"]].tolist()
        if hits:
            i = list(pf.index).index(hits[0])
            ax.axvline(i, color="#e67e22", ls=":", label=f"DETACH {best_out['detach_poll']}")
    ax.axhline(0, color="#999", lw=0.7)
    step = max(1, len(pf) // 8)
    ax.set_xticks(x[::step])
    ax.set_xticklabels([pf.iloc[i]["poll"] for i in x[::step]], rotation=45, ha="right")
    ax.set_ylabel("%")
    ax.set_title(f"{pf.attrs.get('date')} · 5m polls · PTT={pf.attrs.get('peak_to_trough'):.2f}%")
    ax.legend(fontsize=8)
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=130)
    plt.close(fig)


def render_markdown(payload: dict[str, Any]) -> str:
    m = payload["meta"]
    best = payload.get("best_strategy") or {}
    lines = [
        "# TW↔NQ rolling 5m poll · detach/repair · avoided-loss sweep",
        "",
        f"- Window: `{m.get('date_start')}` → `{m.get('date_end')}` · days={m.get('n_days')} · events≥3%={m.get('n_events3')}",
        f"- Cadence: {m.get('cadence')} · Sources: {m.get('sources')}",
        f"- Notional: **{m.get('notional_ntd'):,.0f} NTD** per day (gross, no fees)",
        f"- Objective: **{m.get('objective')}**",
        "",
        "## Verdict · is it real?",
        "",
    ]
    if best:
        lines += [
            f"**Best by net edge:** `{best.get('strategy_id')}` — {best.get('name')}",
            f"- Net edge: **{best.get('net_edge_ntd')} NTD** over sample "
            f"(saved_vs_close {best.get('sum_saved_vs_close_ntd')} − opp_cost {best.get('sum_opp_cost_ntd')})",
            f"- Upper-bound saved vs trough: {best.get('sum_saved_vs_trough_ntd')} NTD (not cash unless stop before low)",
            f"- Trigger rate {best.get('trigger_rate')} · event recall {best.get('event_recall')} · "
            f"false trig on non-event {best.get('false_trigger_rate_on_nonevent')}",
            "",
            "解讀：`net_edge_ntd>0` 代表在「開盤做多、觸發就平」假設下，扣掉假警報少賺之後仍有淨省虧；"
            "樣本僅 ~60 日 Yahoo 5m，只能當 Research 證據，不能當 Order 自動全平依據。",
            "",
        ]
    lines += ["## Top strategies", "", "| ID | Net edge NTD | Saved vs close | Opp cost | Recall≥3% | FPR non-ev | Trigs |", "|---|---:|---:|---:|---:|---:|---:|"]
    for r in payload.get("top_strategies") or []:
        lines.append(
            f"| `{r.get('strategy_id')}` | {r.get('net_edge_ntd')} | {r.get('sum_saved_vs_close_ntd')} | "
            f"{r.get('sum_opp_cost_ntd')} | {r.get('event_recall')} | {r.get('false_trigger_rate_on_nonevent')} | "
            f"{r.get('n_triggers')} |"
        )
    lines += ["", "## Case studies", ""]
    for c in payload.get("cases") or []:
        if not c.get("present"):
            lines.append(f"### {c.get('date')} · missing in 5m panel")
            continue
        lines.append(
            f"### {c['date']} · PTT {c['peak_to_trough_pct']:.2f}% · "
            f"close {c['close_from_open_pct']:.2f}% · trough {c['trough_from_open_pct']:.2f}%"
        )
        lines.append("")
        lines.append("| Strategy | Detach | Exit TW% | Saved vs close NTD | Opp cost NTD | Reenter |")
        lines.append("|---|---|---:|---:|---:|---|")
        for sid, o in (c.get("strategy_outcomes") or {}).items():
            lines.append(
                f"| `{sid}` | {o.get('detach_poll')} | {o.get('detach_tw_from_open')} | "
                f"{o.get('saved_vs_close_ntd')} | {o.get('false_alarm_opp_cost_ntd', 0)} | {o.get('reenter_poll')} |"
            )
        lines.append("")
    lines += [
        "## Method notes",
        "",
        "- DETACH channels: cum_spread / spread_win / corr break (OR).",
        "- Confirm=2 requires two consecutive polls.",
        "- REENTER: after detach, first poll with spread_win≥0.25 and cum rising ≥0.3pp.",
        "- saved_vs_close = reduction in loss vs hold-to-close; opp_cost = closed higher after early exit.",
        "- Research layer only.",
        "",
    ]
    return "\n".join(lines) + "\n"
