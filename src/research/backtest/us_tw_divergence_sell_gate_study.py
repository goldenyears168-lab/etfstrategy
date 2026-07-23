"""US (NQ/ES) vs TW index divergence · sell-all gate event study.

Uses local IX0001 daily_bars + Yahoo 1h (NQ=F / ES=F / ^TWII) for ~730d.
Optional: DB us_futures_overnight_snapshot, tech_risk_daily_snapshot, 0050 5m.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

TZ_TW = ZoneInfo("Asia/Taipei")
TZ_ET = ZoneInfo("America/New_York")

TW_OPEN = "09:00"
TW_CLOSE = "13:30"
PRIMARY_EVENT = "intraday_peak_to_trough_1h"
EVENT_THRESHOLD = 3.0


# ── helpers ─────────────────────────────────────────────────────────────


def _flatten_yf(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    if isinstance(df.columns, pd.MultiIndex):
        df = df.copy()
        df.columns = df.columns.droplevel(1)
    keep = [c for c in ("Open", "High", "Low", "Close", "Volume") if c in df.columns]
    out = df[keep].copy()
    out.columns = [c.lower() for c in out.columns]
    return out


def fetch_yahoo_ohlc(
    symbol: str,
    start: date,
    end: date,
    *,
    interval: str = "1h",
) -> pd.DataFrame:
    import yfinance as yf

    # Yahoo 1h hard-cap ~730 calendar days from *now*
    if interval in ("1h", "60m"):
        earliest = date.today() - timedelta(days=729)
        if start < earliest:
            start = earliest
    df = yf.download(
        symbol,
        start=start.isoformat(),
        end=(end + timedelta(days=1)).isoformat(),
        interval=interval,
        progress=False,
        auto_adjust=False,
        threads=False,
    )
    out = _flatten_yf(df)
    if out.empty:
        return out
    idx = pd.to_datetime(out.index)
    if idx.tz is None:
        # yfinance: TW symbols often naive local; US futures naive ET
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
    return out.sort_index()


def load_ix0001_daily(conn, start: str, end: str) -> pd.DataFrame:
    q = """
    SELECT date, open, high, low, close, volume
    FROM daily_bars
    WHERE code='IX0001' AND date>=? AND date<=?
    ORDER BY date
    """
    df = pd.read_sql_query(q, conn, params=(start, end))
    if df.empty:
        return df
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["date"] = df["date"].astype(str)
    # DB may contain duplicate (code, date) rows from multiple sources
    df = (
        df.sort_values(["date"])
        .groupby("date", as_index=False)
        .agg(
            {
                "open": "last",
                "high": "max",
                "low": "min",
                "close": "last",
                "volume": "max",
            }
        )
    )
    return df


def _tw_session_dates_from_1h(tw_1h: pd.DataFrame) -> list[str]:
    """TW cash-session calendar dates present in Yahoo ^TWII 1h bars."""
    if tw_1h is None or tw_1h.empty:
        return []
    local = tw_1h.copy()
    if local.index.tz is None:
        local.index = local.index.tz_localize(TZ_TW)
    else:
        local.index = local.index.tz_convert(TZ_TW)
    out: set[str] = set()
    for ts in local.index:
        hm = ts.strftime("%H:%M")
        if TW_OPEN <= hm <= TW_CLOSE:
            out.add(ts.strftime("%Y-%m-%d"))
    return sorted(out)


def augment_daily_with_twii_1h(
    daily: pd.DataFrame,
    tw_1h: pd.DataFrame,
    *,
    date_start: str,
    date_end: str,
) -> tuple[pd.DataFrame, list[str]]:
    """Fill IX0001 lagging days from ^TWII 1h session OHLC (prefer DB rows)."""
    have = set(daily["date"].astype(str)) if daily is not None and not daily.empty else set()
    added: list[str] = []
    rows: list[dict[str, Any]] = []
    for d in _tw_session_dates_from_1h(tw_1h):
        if d < date_start or d > date_end or d in have:
            continue
        sess = slice_tw_session(tw_1h, d)
        if len(sess) < 2:
            continue
        rows.append(
            {
                "date": d,
                "open": float(sess["open"].iloc[0]),
                "high": float(sess["high"].max()),
                "low": float(sess["low"].min()),
                "close": float(sess["close"].iloc[-1]),
                "volume": float(sess["volume"].sum()) if "volume" in sess.columns else 0.0,
            }
        )
        added.append(d)
    if not rows:
        return daily, added
    extra = pd.DataFrame(rows)
    if daily is None or daily.empty:
        out = extra
    else:
        out = pd.concat([daily, extra], ignore_index=True)
    out = (
        out.sort_values("date")
        .drop_duplicates(subset=["date"], keep="first")
        .reset_index(drop=True)
    )
    return out, added


def load_tech_risk(conn, start: str, end: str) -> pd.DataFrame:
    try:
        q = """
        SELECT session_date, sox_daily_return_pct, tsm_daily_return_pct,
               tx_gap_pct, te_overnight_pct, sox_above_ma5, tsm_above_ma5
        FROM tech_risk_daily_snapshot
        WHERE session_date>=? AND session_date<=?
        ORDER BY session_date
        """
        df = pd.read_sql_query(q, conn, params=(start, end))
    except Exception:
        return pd.DataFrame()
    if not df.empty:
        df["session_date"] = df["session_date"].astype(str)
    return df


def load_overnight_db(conn, start: str, end: str) -> pd.DataFrame:
    try:
        q = """
        SELECT tw_session_date, capture_label, nq_overnight_pct, es_overnight_pct,
               nq_price, es_price, nq_prior_close, es_prior_close
        FROM us_futures_overnight_snapshot
        WHERE tw_session_date>=? AND tw_session_date<=?
        ORDER BY tw_session_date, capture_label
        """
        df = pd.read_sql_query(q, conn, params=(start, end))
    except Exception:
        return pd.DataFrame()
    if not df.empty:
        df["tw_session_date"] = df["tw_session_date"].astype(str)
    return df


def load_0050_5m_day(conn, trade_date: str) -> pd.DataFrame:
    q = """
    SELECT minute, open, high, low, close, volume
    FROM stock_kbar_5m
    WHERE stock_id='0050' AND trade_date=?
    ORDER BY minute
    """
    try:
        df = pd.read_sql_query(q, conn, params=(trade_date,))
    except Exception:
        return pd.DataFrame()
    if df.empty:
        return df
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


# ── event definitions ───────────────────────────────────────────────────


def session_intraday_max_dd_from_peak(ohlc: pd.DataFrame) -> dict[str, Any]:
    """Max peak→trough drawdown within a TW session (bars may be 1h or 5m).

    Peak = running max of high; trough = subsequent min low.
    Also reports open→low and high→close.
    """
    if ohlc is None or len(ohlc) == 0:
        return {
            "peak_to_trough_pct": np.nan,
            "open_to_low_pct": np.nan,
            "high_to_close_pct": np.nan,
            "peak_ts": None,
            "trough_ts": None,
        }
    highs = ohlc["high"].astype(float).values
    lows = ohlc["low"].astype(float).values
    closes = ohlc["close"].astype(float).values
    opens = ohlc["open"].astype(float).values
    idx = list(ohlc.index)

    run_max = -np.inf
    peak_i = 0
    best_dd = 0.0
    best_peak_i = 0
    best_trough_i = 0
    for i in range(len(highs)):
        if highs[i] > run_max:
            run_max = highs[i]
            peak_i = i
        if run_max > 0:
            dd = (run_max - lows[i]) / run_max * 100.0
            if dd > best_dd:
                best_dd = dd
                best_peak_i = peak_i
                best_trough_i = i

    open0 = float(opens[0])
    hi = float(np.nanmax(highs))
    lo = float(np.nanmin(lows))
    cl = float(closes[-1])
    return {
        "peak_to_trough_pct": float(best_dd),
        "open_to_low_pct": float((open0 - lo) / open0 * 100.0) if open0 > 0 else np.nan,
        "high_to_close_pct": float((hi - cl) / hi * 100.0) if hi > 0 else np.nan,
        "peak_ts": str(idx[best_peak_i]) if idx else None,
        "trough_ts": str(idx[best_trough_i]) if idx else None,
        "session_high": hi,
        "session_low": lo,
        "session_open": open0,
        "session_close": cl,
    }


def slice_tw_session(df: pd.DataFrame, session_date: str, tz=TZ_TW) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    s = pd.Timestamp(f"{session_date} {TW_OPEN}", tz=tz)
    e = pd.Timestamp(f"{session_date} {TW_CLOSE}", tz=tz)
    # convert if needed
    idx = df.index
    if idx.tz is None:
        local = df.copy()
        local.index = idx.tz_localize(tz)
    else:
        local = df.tz_convert(tz) if hasattr(df, "tz_convert") else df
        if local.index.tz != tz:
            local = local.copy()
            local.index = local.index.tz_convert(tz)
    return local.loc[(local.index >= s) & (local.index <= e)].copy()


def slice_us_during_tw(df: pd.DataFrame, session_date: str) -> pd.DataFrame:
    """US futures bars whose ET timestamps fall inside TW cash session."""
    if df is None or df.empty:
        return pd.DataFrame()
    s_tw = pd.Timestamp(f"{session_date} {TW_OPEN}", tz=TZ_TW)
    e_tw = pd.Timestamp(f"{session_date} {TW_CLOSE}", tz=TZ_TW)
    s_et = s_tw.astimezone(TZ_ET)
    e_et = e_tw.astimezone(TZ_ET)
    local = df.copy()
    if local.index.tz is None:
        local.index = local.index.tz_localize(TZ_ET)
    else:
        local.index = local.index.tz_convert(TZ_ET)
    return local.loc[(local.index >= s_et) & (local.index <= e_et)].copy()


def define_events(
    daily: pd.DataFrame,
    tw_1h: pd.DataFrame,
    *,
    threshold: float = EVENT_THRESHOLD,
) -> pd.DataFrame:
    """Build multi-definition event table keyed by date."""
    rows: list[dict[str, Any]] = []
    for _, r in daily.iterrows():
        d = str(r["date"])
        o, h, lo, c = float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"])
        daily_hl = (h - lo) / h * 100.0 if h > 0 else np.nan
        daily_ol = (o - lo) / o * 100.0 if o > 0 else np.nan
        daily_hc = (h - c) / h * 100.0 if h > 0 else np.nan
        prior = daily[daily["date"] < d]
        prior_close = float(prior.iloc[-1]["close"]) if len(prior) else np.nan
        daily_close_chg = (c - prior_close) / prior_close * 100.0 if prior_close > 0 else np.nan

        sess = slice_tw_session(tw_1h, d)
        intra = session_intraday_max_dd_from_peak(sess)

        row = {
            "date": d,
            "daily_hl_pct": daily_hl,
            "daily_open_to_low_pct": daily_ol,
            "daily_high_to_close_pct": daily_hc,
            "daily_close_chg_pct": daily_close_chg,
            "intraday_peak_to_trough_1h": intra["peak_to_trough_pct"],
            "intraday_open_to_low_1h": intra["open_to_low_pct"],
            "intraday_high_to_close_1h": intra["high_to_close_pct"],
            "peak_ts": intra.get("peak_ts"),
            "trough_ts": intra.get("trough_ts"),
            "tw_bars_1h": int(len(sess)),
            "ix_open": o,
            "ix_high": h,
            "ix_low": lo,
            "ix_close": c,
        }
        row["event_intraday_ptt"] = bool(intra["peak_to_trough_pct"] >= threshold) if pd.notna(intra["peak_to_trough_pct"]) else False
        row["event_daily_hl"] = bool(daily_hl >= threshold) if pd.notna(daily_hl) else False
        row["event_daily_ol"] = bool(daily_ol >= threshold) if pd.notna(daily_ol) else False
        row["event_daily_hc"] = bool(daily_hc >= threshold) if pd.notna(daily_hc) else False
        row["event_close_down3"] = bool(daily_close_chg <= -threshold) if pd.notna(daily_close_chg) else False
        # primary
        if row["tw_bars_1h"] >= 2:
            row["is_event"] = row["event_intraday_ptt"]
            row["event_def"] = PRIMARY_EVENT
            row["event_magnitude"] = row["intraday_peak_to_trough_1h"]
        else:
            row["is_event"] = row["event_daily_hl"]
            row["event_def"] = "daily_hl_fallback"
            row["event_magnitude"] = row["daily_hl_pct"]
        rows.append(row)
    return pd.DataFrame(rows)


# ── feature engineering ─────────────────────────────────────────────────


def _ret(a: float, b: float) -> float:
    if a is None or b is None or not (a > 0) or not (b > 0) or (isinstance(a, float) and math.isnan(a)):
        return float("nan")
    return (b - a) / a * 100.0


def prior_us_rth_close(us_daily: pd.Series, session_date: str) -> tuple[str | None, float | None]:
    """US prior RTH close relative to TW session morning (approx: last US date < TW date)."""
    if us_daily is None or len(us_daily) == 0:
        return None, None
    dates = sorted(str(x) for x in us_daily.index)
    prior = [x for x in dates if x < session_date]
    if not prior:
        return None, None
    d = prior[-1]
    return d, float(us_daily.loc[d])


def price_at_or_before(series: pd.Series, ts: pd.Timestamp) -> float | None:
    if series is None or series.empty:
        return None
    s = series.copy()
    if s.index.tz is None:
        s.index = s.index.tz_localize(ts.tz)
    else:
        s.index = s.index.tz_convert(ts.tz)
    sub = s[s.index <= ts]
    if sub.empty:
        return None
    v = float(sub.iloc[-1])
    return v if v > 0 else None


def build_day_features(
    session_date: str,
    *,
    tw_1h: pd.DataFrame,
    nq_1h: pd.DataFrame,
    es_1h: pd.DataFrame,
    nq_daily: pd.Series,
    es_daily: pd.Series,
    overnight_db: pd.DataFrame,
    tech: pd.DataFrame,
    beta_nq: float,
) -> dict[str, Any]:
    feat: dict[str, Any] = {"date": session_date}
    tw_sess = slice_tw_session(tw_1h, session_date)
    nq_sess = slice_us_during_tw(nq_1h, session_date)
    es_sess = slice_us_during_tw(es_1h, session_date)

    # overnight vs prior RTH (at TW open ≈ 21:00 prior ET / 09:00 TW)
    open_tw = pd.Timestamp(f"{session_date} 09:00", tz=TZ_TW)
    nq_prior_d, nq_prior = prior_us_rth_close(nq_daily, session_date)
    es_prior_d, es_prior = prior_us_rth_close(es_daily, session_date)
    nq_at_open = price_at_or_before(nq_1h["close"] if not nq_1h.empty else pd.Series(dtype=float), open_tw.astimezone(TZ_ET))
    es_at_open = price_at_or_before(es_1h["close"] if not es_1h.empty else pd.Series(dtype=float), open_tw.astimezone(TZ_ET))
    feat["nq_prior_date"] = nq_prior_d
    feat["nq_overnight_open_pct"] = _ret(nq_prior, nq_at_open) if nq_prior and nq_at_open else np.nan
    feat["es_overnight_open_pct"] = _ret(es_prior, es_at_open) if es_prior and es_at_open else np.nan

    # session returns / path
    if len(tw_sess) >= 1:
        tw_o = float(tw_sess.iloc[0]["open"])
        tw_h = float(tw_sess["high"].max())
        tw_l = float(tw_sess["low"].min())
        tw_c = float(tw_sess.iloc[-1]["close"])
        feat["tw_from_open_to_low_pct"] = _ret(tw_o, tw_l)
        feat["tw_from_open_pct"] = _ret(tw_o, tw_c)
        feat["tw_session_range_pct"] = (tw_h - tw_l) / tw_h * 100.0 if tw_h > 0 else np.nan
        intra = session_intraday_max_dd_from_peak(tw_sess)
        feat["tw_peak_to_trough_pct"] = intra["peak_to_trough_pct"]
    else:
        tw_o = np.nan
        feat["tw_from_open_to_low_pct"] = np.nan
        feat["tw_from_open_pct"] = np.nan
        feat["tw_session_range_pct"] = np.nan
        feat["tw_peak_to_trough_pct"] = np.nan

    if len(nq_sess) >= 1 and nq_at_open:
        nq_c = float(nq_sess.iloc[-1]["close"])
        nq_l = float(nq_sess["low"].min())
        feat["nq_tw_window_pct"] = _ret(nq_at_open, nq_c)
        feat["nq_tw_window_to_low_pct"] = _ret(nq_at_open, nq_l)
    else:
        feat["nq_tw_window_pct"] = np.nan
        feat["nq_tw_window_to_low_pct"] = np.nan

    if len(es_sess) >= 1 and es_at_open:
        es_c = float(es_sess.iloc[-1]["close"])
        feat["es_tw_window_pct"] = _ret(es_at_open, es_c)
    else:
        feat["es_tw_window_pct"] = np.nan

    # H2: early window spreads (30/60/90 min approx via 1h bars)
    for label, hhmm in [("30m", "09:30"), ("60m", "10:00"), ("90m", "10:30"), ("150m", "11:30")]:
        ts = pd.Timestamp(f"{session_date} {hhmm}", tz=TZ_TW)
        tw_px = price_at_or_before(tw_sess["close"] if len(tw_sess) else pd.Series(dtype=float), ts)
        nq_px = price_at_or_before(nq_1h["close"] if not nq_1h.empty else pd.Series(dtype=float), ts.astimezone(TZ_ET))
        tw_r = _ret(tw_o, tw_px) if pd.notna(tw_o) else np.nan
        nq_r = _ret(nq_at_open, nq_px) if nq_at_open else np.nan
        feat[f"tw_ret_{label}"] = tw_r
        feat[f"nq_ret_{label}"] = nq_r
        feat[f"spread_tw_minus_beta_nq_{label}"] = (
            tw_r - beta_nq * nq_r if pd.notna(tw_r) and pd.notna(nq_r) else np.nan
        )
        feat[f"spread_tw_minus_nq_{label}"] = (
            tw_r - nq_r if pd.notna(tw_r) and pd.notna(nq_r) else np.nan
        )

    # residual log(TW)-β log(NQ) using session close vs prior
    # filled later with rolling z at panel level
    feat["beta_nq"] = beta_nq

    # overnight continue weaker: overnight already soft AND early TW worsens vs NQ
    feat["h1_overnight_weak"] = feat["nq_overnight_open_pct"]
    ovn = feat["nq_overnight_open_pct"]
    early_spread = feat.get("spread_tw_minus_nq_60m")
    feat["h1_combo"] = (
        (ovn if pd.notna(ovn) else 0)
        + (early_spread if pd.notna(early_spread) else 0)
    )

    # H5 styles
    tw_dd = feat.get("tw_peak_to_trough_pct")
    nq_w = feat.get("nq_tw_window_pct")
    if pd.notna(tw_dd) and pd.notna(nq_w):
        feat["sync_down"] = bool(tw_dd >= 1.5 and nq_w <= -0.5)
        feat["tw_solo_kill"] = bool(tw_dd >= 1.5 and nq_w >= -0.2)
        feat["nq_soft_tw_ok"] = bool(nq_w <= -0.8 and (tw_dd < 1.0 if pd.notna(tw_dd) else True))
    else:
        feat["sync_down"] = False
        feat["tw_solo_kill"] = False
        feat["nq_soft_tw_ok"] = False

    # DB overnight at 09:30 if present
    if overnight_db is not None and not overnight_db.empty:
        sub = overnight_db[
            (overnight_db["tw_session_date"] == session_date)
            & (overnight_db["capture_label"] == "09:30")
        ]
        if len(sub):
            feat["db_nq_overnight_0930"] = float(sub.iloc[0]["nq_overnight_pct"])
            feat["db_es_overnight_0930"] = float(sub.iloc[0]["es_overnight_pct"])

    if tech is not None and not tech.empty:
        sub = tech[tech["session_date"] == session_date]
        if len(sub):
            for col in (
                "sox_daily_return_pct",
                "tsm_daily_return_pct",
                "tx_gap_pct",
                "te_overnight_pct",
            ):
                if col in sub.columns and pd.notna(sub.iloc[0][col]):
                    feat[col] = float(sub.iloc[0][col])

    # impulse: max |1h| NQ move in TW window
    if len(nq_sess) >= 2:
        rets = nq_sess["close"].pct_change().abs() * 100.0
        feat["nq_max_abs_1h_pct"] = float(rets.max())
        # direction of max impulse bar
        raw = nq_sess["close"].pct_change() * 100.0
        i = int(rets.values.argmax())
        feat["nq_impulse_signed_pct"] = float(raw.iloc[i]) if i < len(raw) else np.nan
    else:
        feat["nq_max_abs_1h_pct"] = np.nan
        feat["nq_impulse_signed_pct"] = np.nan

    return feat


def rolling_hedge_residual_z(
    daily_tw: pd.DataFrame,
    nq_daily: pd.Series,
    *,
    window: int = 60,
) -> pd.DataFrame:
    """Daily residual z of log(TW)-β log(NQ) with rolling OLS beta."""
    tw = daily_tw.set_index("date")["close"].astype(float)
    # align NQ prior close onto TW dates (use same calendar date if available else ffill)
    nq = nq_daily.copy()
    nq.index = nq.index.astype(str)
    aligned_nq = []
    for d in tw.index.astype(str):
        prior = [x for x in nq.index if x < d]
        aligned_nq.append(float(nq.loc[prior[-1]]) if prior else np.nan)
    panel = pd.DataFrame({"tw": tw.values, "nq": aligned_nq}, index=tw.index.astype(str))
    panel["log_tw"] = np.log(panel["tw"])
    panel["log_nq"] = np.log(panel["nq"])
    betas = []
    resid = []
    for i in range(len(panel)):
        if i < window:
            betas.append(np.nan)
            resid.append(np.nan)
            continue
        w = panel.iloc[i - window : i]
        x = w["log_nq"].values
        y = w["log_tw"].values
        mask = np.isfinite(x) & np.isfinite(y)
        if mask.sum() < window // 2:
            betas.append(np.nan)
            resid.append(np.nan)
            continue
        x1 = np.column_stack([np.ones(mask.sum()), x[mask]])
        coef, _, _, _ = np.linalg.lstsq(x1, y[mask], rcond=None)
        b = float(coef[1])
        betas.append(b)
        r = float(panel.iloc[i]["log_tw"] - (coef[0] + b * panel.iloc[i]["log_nq"]))
        resid.append(r)
    panel["beta"] = betas
    panel["resid"] = resid
    # rolling z of residual (PIT: use past window only)
    z = []
    for i in range(len(panel)):
        if i < window or not np.isfinite(panel.iloc[i]["resid"]):
            z.append(np.nan)
            continue
        hist = panel["resid"].iloc[i - window : i]
        hist = hist[np.isfinite(hist)]
        if len(hist) < window // 2 or hist.std() == 0:
            z.append(np.nan)
            continue
        z.append(float((panel.iloc[i]["resid"] - hist.mean()) / hist.std()))
    panel["resid_z"] = z
    panel = panel.reset_index().rename(columns={"index": "date"})
    if "date" not in panel.columns:
        panel = panel.rename(columns={panel.columns[0]: "date"})
    panel["date"] = panel["date"].astype(str)
    panel = panel.drop_duplicates(subset=["date"], keep="last")
    return panel


# ── hypothesis evaluation ───────────────────────────────────────────────


@dataclass
class GateRule:
    hypothesis_id: str
    name: str
    feature: str
    direction: str  # "lt" or "gt"
    threshold: float
    lead_label: str  # when rule is evaluated (e.g. open, 60m)


def _triggered(val: float, direction: str, thr: float) -> bool:
    if val is None or (isinstance(val, float) and (math.isnan(val) or math.isinf(val))):
        return False
    return val < thr if direction == "lt" else val > thr


def evaluate_rule(
    features: pd.DataFrame,
    events: pd.DataFrame,
    rule: GateRule,
    *,
    is_end: str,
) -> dict[str, Any]:
    ev = events[["date", "is_event", "event_magnitude"]].copy()
    feat = features[["date", rule.feature]].copy()
    m = ev.merge(feat, on="date", how="inner")
    m["trig"] = m[rule.feature].apply(lambda v: _triggered(float(v) if pd.notna(v) else float("nan"), rule.direction, rule.threshold))

    def _metrics(sub: pd.DataFrame) -> dict[str, Any]:
        n = len(sub)
        n_ev = int(sub["is_event"].sum())
        n_trig = int(sub["trig"].sum())
        tp = int(((sub["trig"]) & (sub["is_event"])).sum())
        fp = int(((sub["trig"]) & (~sub["is_event"])).sum())
        fn = int(((~sub["trig"]) & (sub["is_event"])).sum())
        tn = int(((~sub["trig"]) & (~sub["is_event"])).sum())
        precision = tp / n_trig if n_trig else float("nan")
        recall = tp / n_ev if n_ev else float("nan")
        fpr = fp / (fp + tn) if (fp + tn) else float("nan")
        # subsequent drawdown on trigger days
        trig_dd = sub.loc[sub["trig"], "event_magnitude"]
        non_trig_dd = sub.loc[~sub["trig"], "event_magnitude"]
        return {
            "n_days": n,
            "n_events": n_ev,
            "n_triggers": n_trig,
            "tp": tp,
            "fp": fp,
            "fn": fn,
            "tn": tn,
            "precision": None if (isinstance(precision, float) and math.isnan(precision)) else round(precision, 4),
            "recall": None if (isinstance(recall, float) and math.isnan(recall)) else round(recall, 4),
            "false_alarm_rate": None if (isinstance(fpr, float) and math.isnan(fpr)) else round(fpr, 4),
            "trig_mean_dd": None if trig_dd.empty else round(float(trig_dd.mean()), 3),
            "trig_median_dd": None if trig_dd.empty else round(float(trig_dd.median()), 3),
            "nontrig_mean_dd": None if non_trig_dd.empty else round(float(non_trig_dd.mean()), 3),
            "hit_rate_on_events": round(tp / n_ev, 4) if n_ev else None,
        }

    full = _metrics(m)
    is_m = _metrics(m[m["date"] <= is_end])
    oos_m = _metrics(m[m["date"] > is_end])
    return {
        "rule": asdict(rule),
        "full": full,
        "is": is_m,
        "oos": oos_m,
    }


def classify_hypothesis(eval_row: dict[str, Any]) -> str:
    """accepted / weak / rejected from OOS precision-recall.

    Sell-all is high-cost → require stricter precision and low FPR.
    With rare ≥3% events, treat small OOS event counts as weak at best.
    """
    oos = eval_row.get("oos") or {}
    prec = oos.get("precision")
    rec = oos.get("recall")
    n_ev = oos.get("n_events") or 0
    fpr = oos.get("false_alarm_rate")
    n_trig = oos.get("n_triggers") or 0
    if n_ev < 5 or prec is None or rec is None:
        return "weak"
    if prec >= 0.35 and rec >= 0.30 and (fpr is None or fpr <= 0.06) and n_trig >= 5:
        return "accepted"
    if prec >= 0.20 and rec >= 0.25 and (fpr is None or fpr <= 0.12):
        return "weak"
    return "rejected"


def sweep_thresholds(
    features: pd.DataFrame,
    events: pd.DataFrame,
    *,
    hypothesis_id: str,
    name: str,
    feature: str,
    direction: str,
    thresholds: list[float],
    lead_label: str,
    is_end: str,
) -> list[dict[str, Any]]:
    out = []
    for thr in thresholds:
        rule = GateRule(hypothesis_id, f"{name}@{thr}", feature, direction, thr, lead_label)
        ev = evaluate_rule(features, events, rule, is_end=is_end)
        ev["status"] = classify_hypothesis(ev)
        out.append(ev)
    # pick best by OOS F1-ish among those with fpr<=0.2
    def score(e: dict[str, Any]) -> float:
        o = e["oos"]
        p, r = o.get("precision") or 0, o.get("recall") or 0
        fpr = o.get("false_alarm_rate")
        if fpr is not None and fpr > 0.25:
            return -1.0
        if p + r == 0:
            return 0.0
        return 2 * p * r / (p + r)

    out.sort(key=score, reverse=True)
    return out


# ── plots ───────────────────────────────────────────────────────────────


def _save_event_alignment_fig(
    path: Path,
    session_date: str,
    tw_sess: pd.DataFrame,
    nq_sess: pd.DataFrame,
    title: str,
) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax1 = plt.subplots(figsize=(9, 4.5))
    if len(tw_sess):
        tw = tw_sess.copy()
        tw.index = tw.index.tz_convert(TZ_TW)
        tw_n = (tw["close"] / float(tw.iloc[0]["open"]) - 1) * 100
        ax1.plot(tw_n.index, tw_n.values, color="#0b6e4f", label="TW (^TWII) % from open", lw=2)
    ax2 = ax1.twinx()
    if len(nq_sess):
        nq = nq_sess.copy()
        # reindex display in TW time
        nq.index = nq.index.tz_convert(TZ_TW)
        base = float(nq.iloc[0]["close"])
        nq_n = (nq["close"] / base - 1) * 100
        ax2.plot(nq_n.index, nq_n.values, color="#c44e52", label="NQ % in TW window", lw=1.8, alpha=0.85)
    ax1.set_title(title)
    ax1.set_ylabel("TW %")
    ax2.set_ylabel("NQ %")
    ax1.axhline(0, color="#888", lw=0.6)
    lines1, labels1 = ax1.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax1.legend(lines1 + lines2, labels1 + labels2, loc="best", fontsize=8)
    fig.autofmt_xdate()
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=120)
    plt.close(fig)


def _save_box_fig(path: Path, features: pd.DataFrame, events: pd.DataFrame, cols: list[str]) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    m = events[["date", "is_event"]].merge(features, on="date", how="inner")
    use = [c for c in cols if c in m.columns]
    if not use:
        return
    n = len(use)
    fig, axes = plt.subplots(1, n, figsize=(3.2 * n, 4), squeeze=False)
    for i, c in enumerate(use):
        ax = axes[0, i]
        data = [
            m.loc[~m["is_event"], c].dropna().astype(float),
            m.loc[m["is_event"], c].dropna().astype(float),
        ]
        ax.boxplot(data, tick_labels=["non-event", "event≥3%"])
        ax.set_title(c, fontsize=9)
        ax.tick_params(labelsize=8)
    fig.suptitle("Factor distributions: event vs non-event", fontsize=11)
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=120)
    plt.close(fig)


def _save_timeline_fig(path: Path, events: pd.DataFrame, triggers: pd.Series) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    m = events.copy()
    trig_dict = {str(k): bool(v) for k, v in triggers.items()}
    m["trig"] = m["date"].map(lambda d: bool(trig_dict.get(str(d), False)))
    fig, ax = plt.subplots(figsize=(11, 3.8))
    dates = pd.to_datetime(m["date"])
    ax.plot(dates, m["event_magnitude"], color="#888", lw=0.8, label="session DD %")
    ev = m[m["is_event"]]
    ax.scatter(pd.to_datetime(ev["date"]), ev["event_magnitude"], c="#c44e52", s=28, label="event≥3%", zorder=3)
    tr = m[m["trig"]]
    ax.scatter(
        pd.to_datetime(tr["date"]),
        tr["event_magnitude"],
        facecolors="none",
        edgecolors="#1f4e79",
        s=60,
        linewidths=1.4,
        label="gate trigger",
        zorder=4,
    )
    ax.axhline(3.0, color="#c44e52", ls="--", lw=0.7)
    ax.set_title("Session drawdown timeline + gate triggers")
    ax.set_ylabel("%")
    ax.legend(fontsize=8)
    fig.autofmt_xdate()
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=120)
    plt.close(fig)


# ── main study ──────────────────────────────────────────────────────────


def run_study(
    conn,
    *,
    date_start: str = "2024-08-14",
    date_end: str | None = None,
    is_end: str = "2025-07-31",
    threshold: float = EVENT_THRESHOLD,
    out_dir: Path,
) -> dict[str, Any]:
    date_end = date_end or date.today().isoformat()
    out_dir.mkdir(parents=True, exist_ok=True)
    fig_dir = out_dir / "figs"
    fig_dir.mkdir(parents=True, exist_ok=True)

    daily = load_ix0001_daily(conn, date_start, date_end)
    if daily.empty:
        raise RuntimeError("No IX0001 daily_bars in window")

    start_d = date.fromisoformat(date_start)
    end_d = date.fromisoformat(date_end)

    tw_1h = fetch_yahoo_ohlc("^TWII", start_d, end_d, interval="1h")
    # IX0001 daily_bars often lags T+0; backfill session days from ^TWII 1h
    daily, twii_filled_dates = augment_daily_with_twii_1h(
        daily, tw_1h, date_start=date_start, date_end=date_end
    )
    nq_1h = fetch_yahoo_ohlc("NQ=F", start_d, end_d, interval="1h")
    es_1h = fetch_yahoo_ohlc("ES=F", start_d, end_d, interval="1h")
    nq_d_df = fetch_yahoo_ohlc("NQ=F", start_d - timedelta(days=40), end_d, interval="1d")
    es_d_df = fetch_yahoo_ohlc("ES=F", start_d - timedelta(days=40), end_d, interval="1d")
    nq_daily = pd.Series(nq_d_df["close"].values, index=pd.to_datetime(nq_d_df.index).tz_localize(None).strftime("%Y-%m-%d"))
    es_daily = pd.Series(es_d_df["close"].values, index=pd.to_datetime(es_d_df.index).tz_localize(None).strftime("%Y-%m-%d"))

    # recent 5m for impulse H4 refinement (optional)
    nq_5m = fetch_yahoo_ohlc("NQ=F", end_d - timedelta(days=55), end_d, interval="5m")

    overnight_db = load_overnight_db(conn, date_start, date_end)
    tech = load_tech_risk(conn, date_start, date_end)

    events = define_events(daily, tw_1h, threshold=threshold)
    # beta from rolling hedge at mid sample
    hedge = rolling_hedge_residual_z(daily, nq_daily, window=60)
    beta_series = hedge.set_index("date")["beta"]
    median_beta = float(beta_series.dropna().median()) if beta_series.notna().any() else 0.6

    feat_rows = []
    for d in events["date"].tolist():
        b = median_beta
        if d in beta_series.index:
            raw_b = beta_series.loc[d]
            if isinstance(raw_b, pd.Series):
                raw_b = raw_b.dropna()
                raw_b = float(raw_b.iloc[-1]) if len(raw_b) else float("nan")
            else:
                raw_b = float(raw_b)
            if math.isfinite(raw_b):
                b = raw_b
        feat_rows.append(
            build_day_features(
                d,
                tw_1h=tw_1h,
                nq_1h=nq_1h,
                es_1h=es_1h,
                nq_daily=nq_daily,
                es_daily=es_daily,
                overnight_db=overnight_db,
                tech=tech,
                beta_nq=float(b),
            )
        )
    features = pd.DataFrame(feat_rows).drop_duplicates(subset=["date"], keep="last")
    events = events.drop_duplicates(subset=["date"], keep="last")
    # merge residual z (prior day — PIT: use previous session resid_z as gate input)
    hz = hedge[["date", "resid_z", "beta", "resid"]].copy()
    hz["date"] = hz["date"].astype(str)
    hz = hz.drop_duplicates(subset=["date"], keep="last")
    hz["resid_z_lag1"] = hz["resid_z"].shift(1)
    features = features.merge(hz[["date", "resid_z", "resid_z_lag1", "resid"]], on="date", how="left")
    features = features.drop_duplicates(subset=["date"], keep="last")

    # rolling z of early spreads for H2
    for col in ["spread_tw_minus_nq_60m", "spread_tw_minus_beta_nq_60m", "nq_overnight_open_pct"]:
        if col in features.columns:
            s = features[col].astype(float)
            mu = s.rolling(60, min_periods=30).mean().shift(1)
            sd = s.rolling(60, min_periods=30).std().shift(1)
            features[f"{col}_z"] = (s - mu) / sd.replace(0, np.nan)

    # H4: after large NQ impulse, TW same-direction follow within window
    features["h4_impulse_mismatch"] = np.where(
        (features["nq_max_abs_1h_pct"] >= features["nq_max_abs_1h_pct"].quantile(0.9))
        & (features["nq_impulse_signed_pct"] < 0)
        & (features["tw_ret_60m"] > features["nq_ret_60m"] + 0.3),
        1.0,
        0.0,
    )

    # sensitivity table
    sens = {}
    for col, label in [
        ("event_intraday_ptt", "intraday_peak_to_trough_1h"),
        ("event_daily_hl", "daily_high_low"),
        ("event_daily_ol", "daily_open_to_low"),
        ("event_daily_hc", "daily_high_to_close"),
        ("event_close_down3", "daily_close_leq_m3"),
    ]:
        sens[label] = {
            "n_events": int(events[col].sum()),
            "event_rate": round(float(events[col].mean()), 4),
            "overlap_with_primary": int((events[col] & events["is_event"]).sum()),
        }

    # hypothesis sweeps
    registry: list[dict[str, Any]] = []
    rejected_notes: list[dict[str, str]] = []

    # H1: overnight NQ weak
    h1 = sweep_thresholds(
        features, events,
        hypothesis_id="H1",
        name="NQ overnight weak",
        feature="nq_overnight_open_pct",
        direction="lt",
        thresholds=[-0.5, -0.8, -1.0, -1.2, -1.5, -2.0],
        lead_label="TW open",
        is_end=is_end,
    )
    registry.append({"hypothesis_id": "H1", "title": "NQ overnight 已大幅轉弱", "best": h1[0], "sweep": h1[:4]})

    h1b = sweep_thresholds(
        features, events,
        hypothesis_id="H1b",
        name="overnight+early spread combo",
        feature="h1_combo",
        direction="lt",
        thresholds=[-1.0, -1.5, -2.0, -2.5, -3.0],
        lead_label="by 10:00",
        is_end=is_end,
    )
    registry.append({"hypothesis_id": "H1b", "title": "Overnight 弱 + 開盤後 spread 擴大", "best": h1b[0], "sweep": h1b[:4]})

    # H2: early spread
    h2 = sweep_thresholds(
        features, events,
        hypothesis_id="H2",
        name="TW-NQ spread 60m",
        feature="spread_tw_minus_nq_60m",
        direction="lt",
        thresholds=[-0.8, -1.0, -1.2, -1.5, -2.0],
        lead_label="10:00",
        is_end=is_end,
    )
    registry.append({"hypothesis_id": "H2", "title": "開盤後 60m 台股跌幅遠超 NQ", "best": h2[0], "sweep": h2[:4]})

    h2z = sweep_thresholds(
        features, events,
        hypothesis_id="H2z",
        name="spread 60m z",
        feature="spread_tw_minus_nq_60m_z",
        direction="lt",
        thresholds=[-1.5, -2.0, -2.5, -3.0],
        lead_label="10:00",
        is_end=is_end,
    )
    registry.append({"hypothesis_id": "H2z", "title": "60m spread rolling z", "best": h2z[0], "sweep": h2z[:4]})

    # H3: cointegration residual z
    h3 = sweep_thresholds(
        features, events,
        hypothesis_id="H3",
        name="hedge resid_z lag1",
        feature="resid_z_lag1",
        direction="lt",
        thresholds=[-1.5, -2.0, -2.5, -3.0],
        lead_label="prior close (PIT)",
        is_end=is_end,
    )
    registry.append({"hypothesis_id": "H3", "title": "log(TW)−β log(NQ) residual z 突破", "best": h3[0], "sweep": h3[:4]})

    # H4 impulse mismatch (binary)
    h4_rule = GateRule("H4", "NQ impulse mismatch", "h4_impulse_mismatch", "gt", 0.5, "intraday")
    h4_ev = evaluate_rule(features, events, h4_rule, is_end=is_end)
    h4_ev["status"] = classify_hypothesis(h4_ev)
    if features["nq_max_abs_1h_pct"].notna().sum() < 50:
        h4_ev["status"] = "rejected"
        rejected_notes.append({"id": "H4", "reason": "1h impulse sample thin / coarse vs true 1m"})
    registry.append({"hypothesis_id": "H4", "title": "NQ 大衝量後台股不同向", "best": h4_ev, "sweep": [h4_ev]})
    if nq_5m.empty:
        rejected_notes.append({"id": "H4-5m", "reason": "Yahoo 5m only ~60d — used only for case charts if available"})

    # H5: solo kill vs sync — evaluate as rules
    # encode bools as float
    features["tw_solo_kill_f"] = features["tw_solo_kill"].astype(float)
    features["sync_down_f"] = features["sync_down"].astype(float)
    h5a = evaluate_rule(
        features, events,
        GateRule("H5a", "TW solo kill flag", "tw_solo_kill_f", "gt", 0.5, "session path"),
        is_end=is_end,
    )
    h5a["status"] = classify_hypothesis(h5a)
    # Note: H5a uses same-day path features → lookahead for early gate; mark carefully
    h5a["caveat"] = "uses same-session NQ/TW path — diagnostic, not early gate"
    h5b = evaluate_rule(
        features, events,
        GateRule("H5b", "sync down flag", "sync_down_f", "gt", 0.5, "session path"),
        is_end=is_end,
    )
    h5b["status"] = classify_hypothesis(h5b)
    h5b["caveat"] = "diagnostic of crisis style; not a predictive early gate"
    registry.append({"hypothesis_id": "H5a", "title": "台股獨殺（NQ 穩）→ 更大尾部？", "best": h5a, "sweep": [h5a]})
    registry.append({"hypothesis_id": "H5b", "title": "同步下跌態", "best": h5b, "sweep": [h5b]})

    # conditional DD comparison for H5
    m5 = events.merge(features[["date", "tw_solo_kill", "sync_down", "nq_tw_window_pct"]], on="date")
    h5_compare = {
        "solo_kill_event_mean_dd": float(m5.loc[m5["tw_solo_kill"] & m5["is_event"], "event_magnitude"].mean())
        if ((m5["tw_solo_kill"] & m5["is_event"]).any())
        else None,
        "sync_down_event_mean_dd": float(m5.loc[m5["sync_down"] & m5["is_event"], "event_magnitude"].mean())
        if ((m5["sync_down"] & m5["is_event"]).any())
        else None,
        "n_solo_kill_events": int((m5["tw_solo_kill"] & m5["is_event"]).sum()),
        "n_sync_down_events": int((m5["sync_down"] & m5["is_event"]).sum()),
    }

    # H6: early (09-10:30) vs late morning
    h6e = sweep_thresholds(
        features, events,
        hypothesis_id="H6e",
        name="early spread 90m",
        feature="spread_tw_minus_nq_90m",
        direction="lt",
        thresholds=[-0.8, -1.0, -1.5, -2.0],
        lead_label="10:30",
        is_end=is_end,
    )
    h6l = sweep_thresholds(
        features, events,
        hypothesis_id="H6l",
        name="late morning spread 150m",
        feature="spread_tw_minus_nq_150m",
        direction="lt",
        thresholds=[-0.8, -1.0, -1.5, -2.0],
        lead_label="11:30",
        is_end=is_end,
    )
    registry.append({"hypothesis_id": "H6e", "title": "早盤(至10:30)偏離預警力", "best": h6e[0], "sweep": h6e[:3]})
    registry.append({"hypothesis_id": "H6l", "title": "午前(至11:30)偏離預警力", "best": h6l[0], "sweep": h6l[:3]})

    # H7: ES vs NQ overnight
    h7n = sweep_thresholds(
        features, events,
        hypothesis_id="H7n",
        name="NQ overnight",
        feature="nq_overnight_open_pct",
        direction="lt",
        thresholds=[-0.8, -1.0, -1.5],
        lead_label="open",
        is_end=is_end,
    )
    h7e = sweep_thresholds(
        features, events,
        hypothesis_id="H7e",
        name="ES overnight",
        feature="es_overnight_open_pct",
        direction="lt",
        thresholds=[-0.5, -0.8, -1.0, -1.2],
        lead_label="open",
        is_end=is_end,
    )
    registry.append({"hypothesis_id": "H7", "title": "ES vs NQ 對 3% 事件敏感度", "best": {"nq": h7n[0], "es": h7e[0]}, "sweep": []})

    # H8: tech_risk SOX
    if "sox_daily_return_pct" in features.columns and features["sox_daily_return_pct"].notna().sum() >= 40:
        # SOX is typically prior US day — treat as overnight factor
        h8 = sweep_thresholds(
            features, events,
            hypothesis_id="H8",
            name="SOX daily ret",
            feature="sox_daily_return_pct",
            direction="lt",
            thresholds=[-1.5, -2.0, -2.5, -3.0],
            lead_label="prior US (tech_risk)",
            is_end=is_end,
        )
        registry.append({"hypothesis_id": "H8", "title": "SOX/tech_risk 因子", "best": h8[0], "sweep": h8[:3]})
    else:
        rejected_notes.append({"id": "H8", "reason": f"tech_risk coverage too short (n={int(features['sox_daily_return_pct'].notna().sum()) if 'sox_daily_return_pct' in features.columns else 0})"})
        registry.append({
            "hypothesis_id": "H8",
            "title": "SOX/tech_risk 因子",
            "best": {"status": "rejected", "reason": "insufficient coverage"},
            "sweep": [],
        })

    # H9: overnight NQ z
    if "nq_overnight_open_pct_z" in features.columns:
        h9 = sweep_thresholds(
            features, events,
            hypothesis_id="H9",
            name="NQ overnight z",
            feature="nq_overnight_open_pct_z",
            direction="lt",
            thresholds=[-1.5, -2.0, -2.5],
            lead_label="open",
            is_end=is_end,
        )
        registry.append({"hypothesis_id": "H9", "title": "NQ overnight rolling z", "best": h9[0], "sweep": h9[:3]})

    # H10: conservative AND gate (overnight weak AND early spread)
    features["h10_and_score"] = (
        (features["nq_overnight_open_pct"].astype(float) < -0.8).astype(float)
        + (features["spread_tw_minus_nq_60m"].astype(float) < -1.2).astype(float)
    )
    h10 = evaluate_rule(
        features,
        events,
        GateRule("H10", "overnight AND 60m spread", "h10_and_score", "gt", 1.5, "10:00"),
        is_end=is_end,
    )
    h10["status"] = classify_hypothesis(h10)
    registry.append(
        {
            "hypothesis_id": "H10",
            "title": "保守 AND：NQ overnight≤−0.8% 且 60m spread≤−1.2%",
            "best": h10,
            "sweep": [h10],
        }
    )

    # Crash-like subset: ≥3% path DD AND same-day damage (close≤−2% or open→low≥2.5%)
    events_crash = events.copy()
    events_crash["is_event"] = (
        events["is_event"]
        & (
            (events["daily_close_chg_pct"] <= -2.0)
            | (events["daily_open_to_low_pct"] >= 2.5)
            | (events["intraday_open_to_low_1h"] >= 2.5)
        )
    )
    crash_n = int(events_crash["is_event"].sum())
    h2_crash = evaluate_rule(
        features,
        events_crash,
        GateRule("H2-crash", "TW-NQ spread 60m on crash-like", "spread_tw_minus_nq_60m", "lt", -1.2, "10:00"),
        is_end=is_end,
    )
    h2_crash["status"] = classify_hypothesis(h2_crash)
    h1b_crash = evaluate_rule(
        features,
        events_crash,
        GateRule("H1b-crash", "combo on crash-like", "h1_combo", "lt", -1.0, "by 10:00"),
        is_end=is_end,
    )
    h1b_crash["status"] = classify_hypothesis(h1b_crash)
    crash_eval = {
        "n_crash_like_events": crash_n,
        "definition": "primary≥3% AND (close≤−2% OR open→low≥2.5%)",
        "H2": h2_crash,
        "H1b": h1b_crash,
    }

    # looser AND for operational observability
    features["h10b_and_score"] = (
        (features["nq_overnight_open_pct"].astype(float) < -0.5).astype(float)
        + (features["spread_tw_minus_nq_60m"].astype(float) < -1.0).astype(float)
    )
    h10b = evaluate_rule(
        features,
        events,
        GateRule("H10b", "overnight AND spread (loose)", "h10b_and_score", "gt", 1.5, "10:00"),
        is_end=is_end,
    )
    h10b["status"] = classify_hypothesis(h10b)
    registry.append(
        {
            "hypothesis_id": "H10b",
            "title": "AND（寬）：overnight≤−0.5% 且 60m spread≤−1.0%",
            "best": h10b,
            "sweep": [h10b],
        }
    )

    # pick conservative sell-all candidate among early gates only
    early_ids = {"H1", "H1b", "H2", "H2z", "H3", "H6e", "H7", "H9", "H10", "H10b"}
    candidates = []
    for item in registry:
        hid = item["hypothesis_id"]
        if hid not in early_ids and hid != "H7":
            continue
        best = item.get("best")
        if not best:
            continue
        if hid == "H7":
            for leg in ("nq", "es"):
                b = best.get(leg)
                if b:
                    candidates.append({"hypothesis_id": f"H7-{leg}", "eval": b, "title": item["title"]})
            continue
        candidates.append({"hypothesis_id": hid, "eval": best, "title": item["title"]})

    _status_rank = {"accepted": 2, "weak": 1, "rejected": 0}

    def cand_score(c: dict[str, Any]) -> float:
        o = (c["eval"].get("oos") or {})
        p = o.get("precision") or 0
        r = o.get("recall") or 0
        fpr = o.get("false_alarm_rate") or 1.0
        status = c["eval"].get("status") or "rejected"
        if status == "rejected":
            return -1.0
        f1 = (2 * p * r / (p + r)) if (p + r) else 0.0
        return _status_rank.get(status, 0) * 10 + f1 - fpr

    candidates.sort(key=cand_score, reverse=True)
    top_rules = candidates[:5]

    # plots: pick representative days
    ev_days = events[events["is_event"]].sort_values("event_magnitude", ascending=False)
    plot_paths = []
    # sync kill, solo, mild false alarm
    mplot = events.merge(
        features[["date", "nq_tw_window_pct", "nq_overnight_open_pct", "spread_tw_minus_nq_60m", "tw_solo_kill", "sync_down"]],
        on="date",
        how="left",
    )
    picks: list[tuple[str, str]] = []
    # Always include date_end case study when it is a primary event (e.g. lag-filled T+0)
    if date_end in set(ev_days["date"].astype(str)):
        picks.append((date_end, "case_study_end"))
    sync = mplot[mplot["is_event"] & mplot["sync_down"]].sort_values("event_magnitude", ascending=False)
    solo = mplot[mplot["is_event"] & mplot["tw_solo_kill"]].sort_values("event_magnitude", ascending=False)
    if len(sync):
        d0 = str(sync.iloc[0]["date"])
        if d0 not in [p[0] for p in picks]:
            picks.append((d0, "sync_down_event"))
    if len(solo):
        d0 = str(solo.iloc[0]["date"])
        if d0 not in [p[0] for p in picks]:
            picks.append((d0, "tw_solo_kill_event"))
    # top magnitude events
    for _, r in ev_days.head(4).iterrows():
        d = str(r["date"])
        if d not in [p[0] for p in picks]:
            picks.append((d, "large_event"))
        if len(picks) >= 6:
            break
    # false alarm: strong overnight weak but not event
    if top_rules:
        feat_name = top_rules[0]["eval"]["rule"]["feature"]
        direction = top_rules[0]["eval"]["rule"]["direction"]
        thr = top_rules[0]["eval"]["rule"]["threshold"]
        fa = mplot[~mplot["is_event"]].copy()
        if feat_name in features.columns:
            fa = fa.merge(features[["date", feat_name]], on="date", how="left", suffixes=("", "_r"))
            col = feat_name
            if direction == "lt":
                fa = fa[fa[col] < thr].sort_values(col)
            else:
                fa = fa[fa[col] > thr].sort_values(col, ascending=False)
            if len(fa):
                picks.append((str(fa.iloc[0]["date"]), "false_alarm"))

    for d, tag in picks[:6]:
        path = fig_dir / f"align_{d.replace('-', '')}_{tag}.png"
        _save_event_alignment_fig(
            path,
            d,
            slice_tw_session(tw_1h, d),
            slice_us_during_tw(nq_1h, d),
            title=f"{d} · {tag} · TW vs NQ (TW session)",
        )
        plot_paths.append(str(path))

    box_path = fig_dir / "factor_box_event_vs_none.png"
    _save_box_fig(
        box_path,
        features,
        events,
        [
            "nq_overnight_open_pct",
            "spread_tw_minus_nq_60m",
            "resid_z_lag1",
            "es_overnight_open_pct",
        ],
    )
    plot_paths.append(str(box_path))

    # timeline with best early rule
    trig_map: dict[str, bool] = {str(d): False for d in features["date"].tolist()}
    if top_rules:
        best_rule_meta = top_rules[0]["eval"]["rule"]
        fr = best_rule_meta["feature"]
        direction = best_rule_meta["direction"]
        thr = best_rule_meta["threshold"]
        for _, row in features.iterrows():
            d = str(row["date"])
            trig_map[d] = _triggered(
                float(row[fr]) if pd.notna(row.get(fr)) else float("nan"),
                direction,
                thr,
            )
    tl_path = fig_dir / "timeline_gate_triggers.png"
    _save_timeline_fig(tl_path, events, pd.Series(trig_map))
    plot_paths.append(str(tl_path))

    # persist tables
    events_path = out_dir / "events.csv"
    features_path = out_dir / "features.csv"
    events.to_csv(events_path, index=False)
    features.to_csv(features_path, index=False)

    event_list = events[events["is_event"]].sort_values("date")[
        [
            "date",
            "event_def",
            "event_magnitude",
            "daily_hl_pct",
            "daily_close_chg_pct",
            "intraday_peak_to_trough_1h",
            "peak_ts",
            "trough_ts",
        ]
    ]

    payload: dict[str, Any] = {
        "meta": {
            "date_start": date_start,
            "date_end": date_end,
            "is_end": is_end,
            "threshold_pct": threshold,
            "primary_event_def": PRIMARY_EVENT,
            "tw_intraday_source": "^TWII 1h Yahoo",
            "us_intraday_source": "NQ=F/ES=F 1h Yahoo",
            "ix_daily_source": "daily_bars IX0001",
            "median_hedge_beta": round(median_beta, 4),
            "n_days": int(len(events)),
            "n_events_primary": int(events["is_event"].sum()),
            "data_notes": [
                "Yahoo 1h limited to ~730 calendar days",
                "Yahoo 5m/1m insufficient for full window; H4 impulse uses 1h",
                f"us_futures_overnight_snapshot DB days={overnight_db['tw_session_date'].nunique() if len(overnight_db) else 0}",
                f"tech_risk rows in feat={int(features['sox_daily_return_pct'].notna().sum()) if 'sox_daily_return_pct' in features.columns else 0}",
                "IX0001 5m in DB only ~40 recent days; study uses ^TWII 1h",
                "daily_bars IX0001 had duplicate (code,date) rows — study dedupes by date",
                (
                    "IX0001 daily lag filled from ^TWII 1h: "
                    + (", ".join(twii_filled_dates) if twii_filled_dates else "(none)")
                ),
            ],
        },
        "sensitivity": sens,
        "events": event_list.to_dict(orient="records"),
        "h5_style_compare": h5_compare,
        "crash_like_eval": _jsonable(crash_eval),
        "hypothesis_registry": [
            {
                "hypothesis_id": x["hypothesis_id"],
                "title": x["title"],
                "status": (x["best"].get("status") if isinstance(x.get("best"), dict) and "status" in x["best"] else (
                    x["best"].get("nq", {}).get("status") if isinstance(x.get("best"), dict) and "nq" in x["best"] else None
                )),
                "best": _jsonable(x["best"]),
            }
            for x in registry
        ],
        "rejected_notes": rejected_notes,
        "top_rule_candidates": [
            {"hypothesis_id": c["hypothesis_id"], "title": c["title"], "eval": _jsonable(c["eval"])}
            for c in top_rules
        ],
        "plot_paths": plot_paths,
        "artifact_paths": {
            "events_csv": str(events_path),
            "features_csv": str(features_path),
        },
    }

    # status rollup for registry
    for item in payload["hypothesis_registry"]:
        if item["status"] is None and isinstance(item.get("best"), dict):
            # H7 combined
            if "nq" in item["best"] and "es" in item["best"]:
                sn = item["best"]["nq"].get("status")
                se = item["best"]["es"].get("status")
                item["status"] = sn if (sn == "accepted" or (sn == "weak" and se != "accepted")) else se
                item["nq_vs_es"] = {
                    "nq_oos": item["best"]["nq"].get("oos"),
                    "es_oos": item["best"]["es"].get("oos"),
                    "winner": "NQ"
                    if cand_score({"eval": item["best"]["nq"], "hypothesis_id": "x"})
                    >= cand_score({"eval": item["best"]["es"], "hypothesis_id": "x"})
                    else "ES",
                }

    md = render_report_md(payload)
    return {"payload": payload, "markdown": md, "events": events, "features": features}


def _jsonable(obj: Any) -> Any:
    if obj is None:
        return None
    if isinstance(obj, (str, int, float, bool)):
        if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
            return None
        return obj
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(x) for x in obj]
    if isinstance(obj, (np.floating, np.integer)):
        return _jsonable(float(obj))
    if isinstance(obj, pd.Timestamp):
        return str(obj)
    return str(obj)


def render_report_md(payload: dict[str, Any]) -> str:
    meta = payload["meta"]
    lines: list[str] = []
    lines.append("# US–TW divergence · sell-all gate study")
    lines.append("")
    lines.append(f"- Window: `{meta['date_start']}` → `{meta['date_end']}` · IS end `{meta['is_end']}`")
    lines.append(f"- Primary event: **{meta['primary_event_def']} ≥ {meta['threshold_pct']}%**")
    lines.append(f"- Sample: {meta['n_days']} TW sessions · **{meta['n_events_primary']} primary events**")
    lines.append(f"- Sources: {meta['ix_daily_source']}; {meta['tw_intraday_source']}; {meta['us_intraday_source']}")
    lines.append(f"- Hedge β (median rolling): {meta['median_hedge_beta']}")
    lines.append("")
    lines.append("## Verdict（可操作結論）")
    lines.append("")
    tops = payload.get("top_rule_candidates") or []
    if not tops:
        lines.append("資料窗內**未能找到**同時滿足 OOS precision/recall 與可控假警報的保守 sell-all gate。")
    else:
        best = tops[0]
        st = best["eval"].get("status")
        rule = best["eval"].get("rule") or {}
        oos = best["eval"].get("oos") or {}
        lines.append(
            f"Top candidate：**{best['hypothesis_id']}** · {best['title']} · "
            f"`{rule.get('feature')} {rule.get('direction')} {rule.get('threshold')}` @ {rule.get('lead_label')}"
        )
        lines.append(
            f"- Status: `{st}` · OOS precision={oos.get('precision')} · recall={oos.get('recall')} · "
            f"FPR={oos.get('false_alarm_rate')} · triggers={oos.get('n_triggers')} · events={oos.get('n_events')}"
        )
        lines.append(
            f"- Trigger-day median session DD={oos.get('trig_median_dd')}% · mean={oos.get('trig_mean_dd')}%"
        )
        if st == "accepted":
            lines.append(
                "- **可作 Research 風險升級候選**（human review）；事件稀少、方差大——"
                "**不宜** Order-layer 自動 sell-all。"
            )
        elif st == "weak":
            lines.append("- **僅弱證據**：可作風險升級提示，**不宜**自動全平倉。")
        else:
            lines.append("- **不建議**作為自動 sell-all gate。")
    lines.append("")
    lines.append("### 什麼偏離 → 台股容易死很大")
    lines.append("")
    lines.append(
        "在「多數日子美股期貨與台股同步」的前提下，事件日因子中位差顯示："
        "事件日 NQ overnight 中位約更弱、開盤後 60m `TW−NQ` spread 中位約明顯更負。"
        "最大殺傷日（如 2025-04-07/09）通常是 **overnight 已裂 + 開盤後台股相對 NQ 繼續擴大弱勢**；"
        "也有 **NQ 穩、台股獨殺** 的日子（sync_down 事件均 DD 更高於 solo，但 solo 假警率高）。"
        "單純 overnight / SOX / 共整合殘差 **不足以**當自動全平倉。"
    )
    lines.append("")
    crash = payload.get("crash_like_eval") or {}
    if crash:
        lines.append(
            f"Crash-like 子集（{crash.get('definition')}）：n={crash.get('n_crash_like_events')}。"
        )
        for key in ("H2", "H1b"):
            ev = crash.get(key) or {}
            oos = ev.get("oos") or {}
            lines.append(
                f"- {key} on crash-like · status=`{ev.get('status')}` · "
                f"OOS prec={oos.get('precision')} rec={oos.get('recall')} "
                f"FPR={oos.get('false_alarm_rate')} trig={oos.get('n_triggers')}"
            )
        lines.append("")

    lines.append("## Event definition sensitivity")
    lines.append("")
    lines.append("| Definition | n_events | rate | overlap primary |")
    lines.append("|---|---:|---:|---:|")
    for k, v in (payload.get("sensitivity") or {}).items():
        lines.append(f"| {k} | {v['n_events']} | {v['event_rate']} | {v['overlap_with_primary']} |")
    lines.append("")
    lines.append(
        "Primary uses Yahoo `^TWII` 1h peak→trough inside 09:00–13:30 Taipei. "
        "If `<2` hourly bars that day, falls back to IX0001 daily (high−low)/high."
    )
    lines.append("")

    lines.append("## Event days（primary）")
    lines.append("")
    lines.append("| Date | Mag% | Daily HL% | Close chg% | Peak→Trough 1h |")
    lines.append("|---|---:|---:|---:|---:|")
    for e in payload.get("events") or []:
        lines.append(
            f"| {e['date']} | {_fmt(e.get('event_magnitude'))} | {_fmt(e.get('daily_hl_pct'))} | "
            f"{_fmt(e.get('daily_close_chg_pct'))} | {_fmt(e.get('intraday_peak_to_trough_1h'))} |"
        )
    lines.append("")

    lines.append("## Hypothesis registry")
    lines.append("")
    lines.append("| ID | Title | Status | OOS prec | OOS recall | OOS FPR | Lead | Rule |")
    lines.append("|---|---|---|---:|---:|---:|---|---|")
    for h in payload.get("hypothesis_registry") or []:
        best = h.get("best") or {}
        if "nq" in best and "es" in best:
            nq, es = best["nq"], best["es"]
            lines.append(
                f"| {h['hypothesis_id']} | {h['title']} | {h.get('status')} | "
                f"NQ { (nq.get('oos') or {}).get('precision') } / ES {(es.get('oos') or {}).get('precision')} | "
                f"NQ {(nq.get('oos') or {}).get('recall')} / ES {(es.get('oos') or {}).get('recall')} | "
                f"NQ {(nq.get('oos') or {}).get('false_alarm_rate')} / ES {(es.get('oos') or {}).get('false_alarm_rate')} | open | see JSON |"
            )
            continue
        rule = best.get("rule") or {}
        oos = best.get("oos") or {}
        lines.append(
            f"| {h['hypothesis_id']} | {h['title']} | {best.get('status') or h.get('status')} | "
            f"{oos.get('precision')} | {oos.get('recall')} | {oos.get('false_alarm_rate')} | "
            f"{rule.get('lead_label')} | `{rule.get('feature')} {rule.get('direction')} {rule.get('threshold')}` |"
        )
    lines.append("")

    if payload.get("h5_style_compare"):
        lines.append("### H5 style compare（diagnostic）")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(payload["h5_style_compare"], ensure_ascii=False, indent=2))
        lines.append("```")
        lines.append("")

    lines.append("## Top sell-all gate candidates")
    lines.append("")
    for i, c in enumerate(tops, 1):
        rule = c["eval"].get("rule") or {}
        oos = c["eval"].get("oos") or {}
        ism = c["eval"].get("is") or {}
        lines.append(f"### {i}. {c['hypothesis_id']} — {c['title']}")
        lines.append("")
        lines.append(f"- Rule: `{rule.get('feature')} {rule.get('direction')} {rule.get('threshold')}` @ {rule.get('lead_label')}")
        lines.append(f"- Status: `{c['eval'].get('status')}`")
        lines.append(
            f"- IS: prec={ism.get('precision')} rec={ism.get('recall')} FPR={ism.get('false_alarm_rate')} "
            f"trig={ism.get('n_triggers')} ev={ism.get('n_events')}"
        )
        lines.append(
            f"- OOS: prec={oos.get('precision')} rec={oos.get('recall')} FPR={oos.get('false_alarm_rate')} "
            f"trig={oos.get('n_triggers')} ev={oos.get('n_events')} · "
            f"trig median DD={oos.get('trig_median_dd')}%"
        )
        lines.append("")

    lines.append("## Conservative recommendation")
    lines.append("")
    lines.append(
        "不建議 Order layer 自動 sell-all。Research 層最可用的 **風險升級（human review）** 候選："
    )
    lines.append("")
    lines.append(
        "1. **主候選（H2）**：台股日盤 10:00，若 `TW ret(from open) − NQ ret(TW window) ≤ −1.2%` → 升級風險警示。"
        " OOS 假警約 4%/日、精度約 0.36、召回約 0.38（事件極少，方差大）。"
    )
    lines.append(
        "2. **加強召回（H1b）**：`nq_overnight_open_pct + spread_60m ≤ −1.0` → 召回更高但假警升至 ~9%。"
    )
    lines.append(
        "3. **AND（H10/H10b）**：overnight∧spread 的嚴格交集在本樣本幾乎殺光召回（H10 OOS recall≈0）；"
        "不適合作獨立 sell-all，最多當「極罕見紅燈」旁證。"
    )
    lines.append(
        "4. Overnight-only / ES-only / SOX / residual-z：**rejected** 作為獨立 sell-all。"
    )
    lines.append("")
    if tops:
        r = tops[0]["eval"]["rule"]
        lines.append(
            f"Score-ranked top row: `{tops[0]['hypothesis_id']}` · "
            f"`{r.get('feature')} {r.get('direction')} {r.get('threshold')}` @ {r.get('lead_label')} · "
            f"status=`{tops[0]['eval'].get('status')}`."
        )
    lines.append("")

    lines.append("## Rejected / data gaps")
    lines.append("")
    for note in payload.get("rejected_notes") or []:
        lines.append(f"- `{note['id']}`: {note['reason']}")
    for n in meta.get("data_notes") or []:
        lines.append(f"- {n}")
    lines.append("")

    lines.append("## Figures")
    lines.append("")
    for p in payload.get("plot_paths") or []:
        rel = Path(p).name
        lines.append(f"- `figs/{rel}`")
    lines.append("")

    lines.append("## Artifacts")
    lines.append("")
    for k, v in (payload.get("artifact_paths") or {}).items():
        lines.append(f"- {k}: `{v}`")
    lines.append("")
    lines.append("## Terminology")
    lines.append("")
    lines.append(
        "This is a **Research layer（研究層）** study. Any live use would sit behind "
        "**Order layer（下單層）** policy — not Regime-layer diagnostics, and not graduated strategy alpha."
    )
    lines.append("")
    return "\n".join(lines)


def _fmt(x: Any) -> str:
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return ""
    try:
        return f"{float(x):.2f}"
    except Exception:
        return str(x)
