"""Build PIT-safe relative chip×price panel for one stock vs IX0001 / market FT.

Forbidden primary metric: stock_net / market_net (explodes near zero).
Preferred: stock_FT_NTD / market_FT_gross, rolling-β excess, align sign, resid z.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

GROSS_FLOOR_NTD = 5e10  # NT$500億
PART_WINSOR = 0.05
BETA_WIN = 60
Z_WIN = 21


def _rolling_beta(y: pd.Series, x: pd.Series, win: int = BETA_WIN) -> pd.Series:
    out = np.full(len(y), np.nan)
    yy_a = y.to_numpy(dtype=float)
    xx_a = x.to_numpy(dtype=float)
    for i in range(len(y)):
        if i < win:
            continue
        yy = yy_a[i - win : i]
        xx = xx_a[i - win : i]
        m = np.isfinite(yy) & np.isfinite(xx)
        if m.sum() < max(40, win // 2):
            continue
        xxm = xx[m] - xx[m].mean()
        yym = yy[m] - yy[m].mean()
        v = float(np.dot(xxm, xxm))
        if v <= 0:
            continue
        out[i] = float(np.dot(xxm, yym) / v)
    return pd.Series(out, index=y.index).clip(0.2, 3.0)


def _rolling_resid(y: pd.Series, x: pd.Series, win: int = 60) -> pd.Series:
    resid = np.full(len(y), np.nan)
    yy_a = y.to_numpy(dtype=float)
    xx_a = x.to_numpy(dtype=float)
    for i in range(len(y)):
        if i < win:
            continue
        yy = yy_a[i - win : i]
        xx = xx_a[i - win : i]
        m = np.isfinite(yy) & np.isfinite(xx)
        if m.sum() < 40:
            continue
        X = np.column_stack([np.ones(int(m.sum())), xx[m]])
        b, _, _, _ = np.linalg.lstsq(X, yy[m], rcond=None)
        if np.isfinite(yy_a[i]) and np.isfinite(xx_a[i]):
            resid[i] = yy_a[i] - (b[0] + b[1] * xx_a[i])
    return pd.Series(resid, index=y.index)


def load_market_ft_totals(
    *,
    start: date,
    end: date,
    cache_path: Path | None = None,
    refresh: bool = False,
) -> pd.DataFrame:
    """Market Foreign+IT net/gross NTD from FinMind (cached)."""
    if cache_path is not None and cache_path.exists() and not refresh:
        cached = pd.read_csv(cache_path)
        if {"date", "net_FT", "gross_FT"}.issubset(cached.columns):
            return cached

    from dotenv import load_dotenv
    from finmind_client import fetch_finmind_dataset

    load_dotenv(Path(".env"))
    raw = fetch_finmind_dataset(
        "TaiwanStockTotalInstitutionalInvestors",
        start=start,
        end=end,
        timeout=120,
    )
    rows: list[dict] = []
    for r in raw:
        name = r.get("name")
        if name not in ("Foreign_Investor", "Investment_Trust", "Dealer_self"):
            continue
        b = float(r.get("buy") or 0)
        s = float(r.get("sell") or 0)
        rows.append(
            {
                "date": str(r["date"])[:10],
                "name": name,
                "buy": b,
                "sell": s,
                "net": b - s,
                "gross": b + s,
            }
        )
    mkt = pd.DataFrame(rows)
    if mkt.empty:
        raise RuntimeError("empty TaiwanStockTotalInstitutionalInvestors")
    net_w = mkt.pivot(index="date", columns="name", values="net")
    gross_w = mkt.pivot(index="date", columns="name", values="gross")
    net_w.columns = [f"net_{c}" for c in net_w.columns]
    gross_w.columns = [f"gross_{c}" for c in gross_w.columns]
    out = net_w.join(gross_w).reset_index()
    out["net_FT"] = out["net_Foreign_Investor"] + out["net_Investment_Trust"]
    out["gross_FT"] = out["gross_Foreign_Investor"] + out["gross_Investment_Trust"]
    if cache_path is not None:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        out.to_csv(cache_path, index=False)
    return out


def build_relative_chip_panel(
    conn: sqlite3.Connection,
    *,
    sid: str,
    start: str,
    end: str,
    warmup: str,
    market_totals: pd.DataFrame,
    rs_anchor: str | None = None,
) -> pd.DataFrame:
    """Return daily panel with features ≤ T and forward labels (for eval only)."""
    bars = pd.read_sql_query(
        """
        SELECT trade_date AS date, open, high, low, close, volume
        FROM stock_daily_bars
        WHERE source='finmind' AND stock_id=? AND trade_date>=? AND trade_date<=?
        ORDER BY 1
        """,
        conn,
        params=[sid, warmup, end],
    )
    inst = pd.read_sql_query(
        """
        SELECT trade_date AS date, foreign_net, investment_trust_net, dealer_self_net
        FROM stock_institutional_daily
        WHERE source='finmind' AND stock_id=? AND trade_date>=? AND trade_date<=?
        ORDER BY 1
        """,
        conn,
        params=[sid, warmup, end],
    )
    ix = pd.read_sql_query(
        """
        SELECT date, close AS ix FROM daily_bars
        WHERE code='IX0001' AND date>=? AND date<=?
        ORDER BY date,
          CASE source WHEN 'yahoo' THEN 1 WHEN 'finmind' THEN 2 ELSE 9 END
        """,
        conn,
        params=[warmup, end],
    ).drop_duplicates("date", keep="first")

    mkt = market_totals.copy()
    keep = [
        c
        for c in mkt.columns
        if c
        in {
            "date",
            "net_FT",
            "gross_FT",
            "net_Foreign_Investor",
            "net_Investment_Trust",
            "gross_Foreign_Investor",
            "gross_Investment_Trust",
        }
    ]
    mkt = mkt[keep]

    df = (
        bars.merge(inst, on="date", how="left")
        .merge(ix, on="date", how="left")
        .merge(mkt, on="date", how="left")
    )
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)

    df["r"] = df["close"].pct_change(fill_method=None)
    df["rm"] = df["ix"].pct_change(fill_method=None)
    df["beta"] = _rolling_beta(df["r"], df["rm"], BETA_WIN)
    df["excess_b1"] = df["r"] - df["rm"]
    df["excess_beta"] = df["r"] - df["beta"] * df["rm"]

    df["logRS"] = np.log(df["close"]) - np.log(df["ix"])
    anchor = rs_anchor or start
    rs0_rows = df.loc[df["date"] >= pd.Timestamp(anchor), "logRS"].dropna()
    if rs0_rows.empty:
        raise ValueError(f"no RS anchor rows for {sid} on/after {anchor}")
    rs0 = float(rs0_rows.iloc[0])
    df["RS100"] = 100.0 * np.exp(df["logRS"] - rs0)
    df["RS_mom10"] = (df["logRS"] - df["logRS"].shift(10)) * 100.0

    df["F_ntd"] = df["foreign_net"] * df["close"]
    df["IT_ntd"] = df["investment_trust_net"] * df["close"]
    df["FT_ntd"] = df["F_ntd"] + df["IT_ntd"]
    df["FT_share_net"] = df["FT_ntd"] / df["net_FT"].replace(0, np.nan)  # deprecated
    df["FT_part_gross"] = df["FT_ntd"] / df["gross_FT"].replace(0, np.nan)
    df["FT_part_ok"] = df["gross_FT"] >= GROSS_FLOOR_NTD
    df["FT_part_w"] = df["FT_part_gross"].clip(-PART_WINSOR, PART_WINSOR)
    df["FT_part_bp"] = df["FT_part_gross"] * 1e4

    mu = df["FT_ntd"].shift(1).rolling(Z_WIN, min_periods=Z_WIN).mean()
    sd = df["FT_ntd"].shift(1).rolling(Z_WIN, min_periods=Z_WIN).std()
    df["FT_z"] = (df["FT_ntd"] - mu) / sd.replace(0, np.nan)

    df["align_FT"] = np.sign(df["FT_ntd"]) * np.sign(df["net_FT"])
    df["FT_resid"] = _rolling_resid(df["FT_ntd"], df["net_FT"], 60)
    rmu = df["FT_resid"].shift(1).rolling(Z_WIN, min_periods=Z_WIN).mean()
    rsd = df["FT_resid"].shift(1).rolling(Z_WIN, min_periods=Z_WIN).std()
    df["FT_resid_z"] = (df["FT_resid"] - rmu) / rsd.replace(0, np.nan)

    # Price–chip divergence flags (same-day descriptive; used as T features)
    df["price_up"] = df["r"] > 0
    df["chip_buy"] = df["FT_ntd"] > 0
    df["chip_sell"] = df["FT_ntd"] < 0
    df["mkt_ft_buy"] = df["net_FT"] > 0
    df["rel_dump"] = (df["net_FT"] > 0) & (df["FT_ntd"] < 0)  # market buy, stock sell
    df["rel_scoop"] = (df["net_FT"] < 0) & (df["FT_ntd"] > 0)  # market sell, stock buy
    df["aligned_buy"] = (df["net_FT"] > 0) & (df["FT_ntd"] > 0)
    df["aligned_sell"] = (df["net_FT"] < 0) & (df["FT_ntd"] < 0)
    df["bull_div"] = (df["r"] > 0) & (df["FT_ntd"] < 0)  # price up, chip sell
    df["bear_div"] = (df["r"] < 0) & (df["FT_ntd"] > 0)  # price down, chip buy

    # Expanding percentile of FT_part (past-only; min 60 obs) — PIT safe
    part = df["FT_part_gross"].where(df["FT_part_ok"])
    df["FT_part_pct"] = part.expanding(min_periods=60).apply(
        lambda s: float(pd.Series(s).rank(pct=True).iloc[-1])
        if np.isfinite(s.iloc[-1])
        else np.nan,
        raw=False,
    )
    abs_part = part.abs()
    df["FT_part_abs_pct"] = abs_part.expanding(min_periods=60).apply(
        lambda s: float(pd.Series(s).rank(pct=True).iloc[-1])
        if np.isfinite(s.iloc[-1])
        else np.nan,
        raw=False,
    )

    # Forward labels (evaluation only; not features)
    # Conservatively: chip T known after close → outcome from T close to T+k close.
    for k in (1, 5):
        df[f"fwd{k}_r"] = df["close"].shift(-k) / df["close"] - 1.0
        # sum of next k daily beta-excess (approx path)
        ex = df["excess_beta"]
        fwd = pd.Series(np.nan, index=df.index, dtype=float)
        for i in range(len(df) - k):
            window = ex.iloc[i + 1 : i + 1 + k]
            if window.notna().sum() == k:
                fwd.iloc[i] = float((1.0 + window).prod() - 1.0)
        df[f"fwd{k}_ex"] = fwd
        # open→close on T+1 (more executable if decide overnight)
        if k == 1:
            nxt_o = df["open"].shift(-1)
            nxt_c = df["close"].shift(-1)
            df["fwd1_oc"] = nxt_c / nxt_o - 1.0

    df["sid"] = sid
    df["built_at"] = datetime.now().isoformat(timespec="seconds")
    return df
