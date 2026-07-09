"""Overnight regime panel · PIT-safe · SOX / SPY / FF_OI for C18acc gate research."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from market_benchmark import load_benchmark_close
from research.backtest.finmind_tx_foreign_common import load_foreign_net_oi

DEFAULT_SOX_RISK_OFF_PCT = -1.5
DEFAULT_SPY_RISK_OFF_PCT = -1.0
DEFAULT_SOX_OPEN_TRAP_PCT = 1.5
DEFAULT_FF_OI_Z_EXTREME = -2.0
FF_OI_Z_WINDOW = 60


@dataclass(frozen=True)
class OvernightRegimeThresholds:
    sox_risk_off_pct: float = DEFAULT_SOX_RISK_OFF_PCT
    spy_risk_off_pct: float = DEFAULT_SPY_RISK_OFF_PCT
    sox_open_trap_pct: float = DEFAULT_SOX_OPEN_TRAP_PCT
    ff_oi_z_extreme: float = DEFAULT_FF_OI_Z_EXTREME


def _load_sox_smh_tsm_from_daily_bars(conn: sqlite3.Connection) -> pd.DataFrame:
    try:
        rows = conn.execute(
            """
            SELECT code, date, close
            FROM daily_bars
            WHERE code IN ('SOX', 'SMH', 'TSM_ADR') AND close IS NOT NULL
            ORDER BY code, date
            """
        ).fetchall()
    except sqlite3.OperationalError:
        return pd.DataFrame()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=["code", "date", "close"])
    df["spread"] = df.groupby("code")["close"].pct_change(fill_method=None) * 100.0
    df = df.dropna(subset=["spread"])
    wide = df.pivot(index="date", columns="code", values="spread")
    wide = wide.rename(
        columns={
            "SOX": "sox_t1_ret_pct",
            "SMH": "smh_t1_ret_pct",
            "TSM_ADR": "tsm_t1_ret_pct",
        }
    )
    return wide.reset_index().rename(columns={"date": "us_date"})


def _align_us_ret_to_tw_session(
    tw_dates: list[str],
    us_ret: pd.DataFrame,
    *,
    value_cols: tuple[str, ...],
) -> pd.DataFrame:
    """Map each TW session to the latest US row with us_date < session_date."""
    if us_ret.empty:
        return pd.DataFrame({"session_date": tw_dates})
    us = us_ret.set_index("us_date").sort_index()
    rows: list[dict[str, Any]] = []
    for sd in tw_dates:
        avail = us.index[us.index < sd]
        row: dict[str, Any] = {"session_date": sd, "us_trade_date": None}
        if len(avail) == 0:
            for col in value_cols:
                row[col] = None
        else:
            u = avail[-1]
            row["us_trade_date"] = str(u)
            src = us.loc[u]
            for col in value_cols:
                v = src.get(col) if hasattr(src, "get") else src[col]
                row[col] = float(v) if pd.notna(v) else None
        rows.append(row)
    return pd.DataFrame(rows)


def _load_tech_risk_rows(conn: sqlite3.Connection) -> pd.DataFrame:
    try:
        rows = conn.execute(
            """
            SELECT session_date, us_trade_date,
                   tsm_daily_return_pct, sox_daily_return_pct, smh_daily_return_pct,
                   tx_gap_pct, te_overnight_pct, semi_benchmark
            FROM tech_risk_daily_snapshot
            ORDER BY session_date
            """
        ).fetchall()
    except sqlite3.OperationalError:
        return pd.DataFrame()
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(
        rows,
        columns=[
            "session_date",
            "us_trade_date",
            "tsm_t1_ret_pct",
            "sox_t1_ret_pct",
            "smh_t1_ret_pct",
            "tx_gap_pct",
            "te_overnight_pct",
            "semi_benchmark",
        ],
    )


def _load_spy_t1_by_session(conn: sqlite3.Connection, session_dates: list[str]) -> pd.Series:
    """Map TW session_date → prior US close-to-close return (%)."""
    rows = conn.execute(
        """
        SELECT trade_date, close
        FROM us_daily_bars
        WHERE ticker = 'SPY'
        ORDER BY trade_date
        """
    ).fetchall()
    if not rows:
        return pd.Series(dtype=float)
    spy = pd.Series({r[0]: float(r[1]) for r in rows}, dtype=float).sort_index()
    spy_ret = spy.pct_change() * 100.0

    tech = _load_tech_risk_rows(conn)
    us_by_session: dict[str, float | None] = {}
    if not tech.empty:
        for _, row in tech.iterrows():
            usd = row.get("us_trade_date")
            if pd.isna(usd) or not usd:
                continue
            usd = str(usd)
            us_by_session[str(row["session_date"])] = (
                float(spy_ret.loc[usd]) if usd in spy_ret.index and pd.notna(spy_ret.loc[usd]) else None
            )

    out: dict[str, float | None] = {}
    for sd in session_dates:
        if sd in us_by_session:
            out[sd] = us_by_session[sd]
            continue
        avail = spy_ret.index[spy_ret.index < sd]
        out[sd] = float(spy_ret.loc[avail[-1]]) if len(avail) else None
    return pd.Series(out, dtype=object)


def _load_ix_t1_spread(conn: sqlite3.Connection, session_dates: list[str]) -> pd.Series:
    bench = load_benchmark_close(conn)
    if bench.empty:
        return pd.Series(dtype=float)
    spread = bench.pct_change(fill_method=None) * 100.0
    return spread.reindex(session_dates)


def _foreign_oi_z(conn: sqlite3.Connection, session_dates: list[str]) -> tuple[pd.Series, pd.Series]:
    net = load_foreign_net_oi(conn)
    if net.empty:
        empty = pd.Series(dtype=float)
        return empty, empty
    # At TW session T use latest FF OI on or before prior TW session.
    full_cal = sorted(set(net.index.astype(str).tolist() + session_dates))
    net_full = net.reindex(full_cal).ffill()
    z = (net_full - net_full.rolling(FF_OI_Z_WINDOW, min_periods=20).mean()) / net_full.rolling(
        FF_OI_Z_WINDOW, min_periods=20
    ).std()
    oi_at: dict[str, float | None] = {}
    z_at: dict[str, float | None] = {}
    for sd in session_dates:
        prior = [d for d in full_cal if d < sd]
        if not prior:
            oi_at[sd] = None
            z_at[sd] = None
            continue
        p = prior[-1]
        oi_at[sd] = float(net_full.loc[p]) if pd.notna(net_full.loc[p]) else None
        z_at[sd] = float(z.loc[p]) if p in z.index and pd.notna(z.loc[p]) else None
    return pd.Series(oi_at, dtype=object), pd.Series(z_at, dtype=object)


def load_overnight_regime_panel(
    conn: sqlite3.Connection,
    session_dates: list[str],
    *,
    thresholds: OvernightRegimeThresholds | None = None,
) -> pd.DataFrame:
    """One row per TW session_date · signals known before the open on that date."""
    th = thresholds or OvernightRegimeThresholds()
    dates = sorted(session_dates)
    tech = _load_tech_risk_rows(conn)
    tech_idx = (
        tech.set_index("session_date")
        if not tech.empty
        else pd.DataFrame(index=dates)
    )

    us_bars = _load_sox_smh_tsm_from_daily_bars(conn)
    us_aligned = _align_us_ret_to_tw_session(
        dates,
        us_bars,
        value_cols=("sox_t1_ret_pct", "smh_t1_ret_pct", "tsm_t1_ret_pct"),
    )
    us_idx = us_aligned.set_index("session_date") if not us_aligned.empty else pd.DataFrame()

    ix_t1 = _load_ix_t1_spread(conn, dates)
    spy_t1 = _load_spy_t1_by_session(conn, dates)
    ff_oi, ff_z = _foreign_oi_z(conn, dates)

    rows: list[dict[str, Any]] = []
    for sd in dates:
        tr = tech_idx.loc[sd] if sd in tech_idx.index else None
        ua = us_idx.loc[sd] if not us_idx.empty and sd in us_idx.index else None

        def _pick(col_tech: str, col_us: str) -> float | None:
            if tr is not None and col_tech in tr.index and pd.notna(tr.get(col_tech)):
                return float(tr[col_tech])
            if ua is not None and col_us in ua.index and pd.notna(ua.get(col_us)):
                return float(ua[col_us])
            return None

        sox = _pick("sox_t1_ret_pct", "sox_t1_ret_pct")
        smh = _pick("smh_t1_ret_pct", "smh_t1_ret_pct")
        tsm = _pick("tsm_t1_ret_pct", "tsm_t1_ret_pct")
        tx_gap = None if tr is None or pd.isna(tr.get("tx_gap_pct")) else float(tr["tx_gap_pct"])
        te_on = None if tr is None or pd.isna(tr.get("te_overnight_pct")) else float(tr["te_overnight_pct"])
        usd = None
        if tr is not None and pd.notna(tr.get("us_trade_date")):
            usd = str(tr["us_trade_date"])
        elif ua is not None and pd.notna(ua.get("us_trade_date")):
            usd = str(ua["us_trade_date"])

        ix_v = float(ix_t1.loc[sd]) if sd in ix_t1.index and pd.notna(ix_t1.loc[sd]) else None
        spy_v = spy_t1.get(sd)
        spy_v = float(spy_v) if spy_v is not None and spy_v == spy_v else None
        oi_v = ff_oi.get(sd)
        oi_v = float(oi_v) if oi_v is not None and oi_v == oi_v else None
        z_v = ff_z.get(sd)
        z_v = float(z_v) if z_v is not None and z_v == z_v else None

        risk_sox = sox is not None and sox < th.sox_risk_off_pct
        risk_spy = spy_v is not None and spy_v < th.spy_risk_off_pct
        risk_ix = ix_v is not None and ix_v < 0
        ff_ext = z_v is not None and z_v < th.ff_oi_z_extreme
        combo = risk_sox or ff_ext
        open_trap_proxy = sox is not None and sox > th.sox_open_trap_pct

        rows.append(
            {
                "session_date": sd,
                "us_trade_date": usd,
                "sox_t1_ret_pct": round(sox, 4) if sox is not None else None,
                "smh_t1_ret_pct": round(smh, 4) if smh is not None else None,
                "tsm_t1_ret_pct": round(tsm, 4) if tsm is not None else None,
                "spy_t1_ret_pct": round(spy_v, 4) if spy_v is not None else None,
                "ix_t1_spread_pct": round(ix_v, 4) if ix_v is not None else None,
                "tx_gap_pct": round(tx_gap, 4) if tx_gap is not None else None,
                "te_overnight_pct": round(te_on, 4) if te_on is not None else None,
                "ff_oi_net": round(oi_v, 2) if oi_v is not None else None,
                "ff_oi_z": round(z_v, 4) if z_v is not None else None,
                "risk_off_sox": risk_sox,
                "risk_off_spy": risk_spy,
                "risk_off_ix": risk_ix,
                "ff_oi_extreme_short": ff_ext,
                "combo_risk_off": combo,
                "open_trap_proxy_sox_up": open_trap_proxy,
            }
        )
    return pd.DataFrame(rows)


def regime_row_at(panel: pd.DataFrame, session_date: str) -> dict[str, Any] | None:
    sub = panel.loc[panel["session_date"] == session_date]
    if sub.empty:
        return None
    return sub.iloc[0].to_dict()


def mark_open_trap_days(
    panel: pd.DataFrame,
    ix_daily_ret_pct: pd.Series,
    *,
    sox_up_pct: float = DEFAULT_SOX_OPEN_TRAP_PCT,
    ix_down_pct: float = -1.0,
) -> pd.Series:
    """Ex-post label: SOX T-1 up + TW session close down > |ix_down_pct|."""
    flags: dict[str, bool] = {}
    for _, row in panel.iterrows():
        sd = str(row["session_date"])
        sox = row.get("sox_t1_ret_pct")
        ix = ix_daily_ret_pct.get(sd)
        if sox is None or ix is None or (isinstance(sox, float) and np.isnan(sox)):
            flags[sd] = False
            continue
        flags[sd] = float(sox) > sox_up_pct and float(ix) <= ix_down_pct
    return pd.Series(flags)
