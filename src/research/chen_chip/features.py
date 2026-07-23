"""Chip feature engineering.

Consecutive-buy / foreign-follow ideas faithful to
vendor/tw-institutional-stocker/src/analysis/backtest.py
(InstitutionalAccumulationStrategy, ForeignFollowingStrategy).
COT z/index via cot_bridge (cftc-cot formulas).

Enriched (research): lending / daytrade / foreign shareholding /
institutional side pivot / optional HoldingSharesPer + 八大行庫 caches.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

from research.chen_chip.adapters_db import (
    load_daytrade,
    load_institutional,
    load_institutional_side_wide,
    load_lending,
    load_margin,
    load_shareholding,
    load_tx_foreign_futures,
)
from research.chen_chip.cot_bridge import enrich_net_oi_cot


def _streak_positive(s: pd.Series) -> pd.Series:
    pos = (s > 0).astype(int)
    groups = (pos != pos.shift()).cumsum()
    return pos.groupby(groups).cumsum() * pos


def _streak_negative(s: pd.Series) -> pd.Series:
    neg = (s < 0).astype(int)
    groups = (neg != neg.shift()).cumsum()
    return neg.groupby(groups).cumsum() * neg


def _merge_optional(feat: pd.DataFrame, extra: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    if extra.empty:
        for c in cols:
            if c not in feat.columns:
                feat[c] = np.nan
        return feat
    keep = ["sid", "d"] + [c for c in cols if c in extra.columns]
    return feat.merge(extra[keep], on=["sid", "d"], how="left")


def load_holding_shares_cache(path: Path | None) -> pd.DataFrame:
    """Weekly concentration cache → daily-ready frame (caller ffill)."""
    if path is None or not path.exists():
        return pd.DataFrame()
    df = pd.read_csv(path)
    if df.empty:
        return df
    df["sid"] = df["sid"].astype(str)
    df["d"] = df["d"].astype(str)
    return df


def load_gov_bank_cache(path: Path | None) -> pd.DataFrame:
    if path is None or not path.exists():
        return pd.DataFrame()
    df = pd.read_csv(path)
    if df.empty:
        return df
    df["sid"] = df["sid"].astype(str)
    df["d"] = df["d"].astype(str)
    return df


def build_chip_feature_frame(
    conn: sqlite3.Connection,
    stock_ids: list[str],
    d0: str,
    d1: str,
    *,
    holding_shares_path: Path | None = None,
    gov_bank_path: Path | None = None,
) -> pd.DataFrame:
    inst = load_institutional(conn, stock_ids, d0, d1)
    if inst.empty:
        return pd.DataFrame()
    marg = load_margin(conn, stock_ids, d0, d1)
    lend = load_lending(conn, stock_ids, d0, d1)
    dt = load_daytrade(conn, stock_ids, d0, d1)
    sh = load_shareholding(conn, stock_ids, d0, d1)
    side = load_institutional_side_wide(conn, stock_ids, d0, d1)
    tx = load_tx_foreign_futures(conn, d0, d1)
    tx = enrich_net_oi_cot(tx) if not tx.empty else tx
    hs_cache = load_holding_shares_cache(holding_shares_path)
    gov_cache = load_gov_bank_cache(gov_bank_path)

    parts: list[pd.DataFrame] = []
    for sid, g in inst.groupby("sid"):
        g = g.sort_values("d").copy()
        # Vendor accumulation uses foreign+trust+dealer net per day
        g["three_buy_net"] = g["foreign_net"] + g["trust_net"] + g["dealer_net"]
        g["foreign_streak"] = _streak_positive(g["foreign_net"])
        g["trust_streak"] = _streak_positive(g["trust_net"])
        g["three_streak"] = _streak_positive(g["three_buy_net"])
        # Foreign following (shares); FinMind nets are typically in shares
        g["foreign_net_k"] = g["foreign_net"] / 1000.0
        # 20d percentile of foreign net (for relative threshold)
        g["foreign_pctl20"] = g["foreign_net"].rolling(20, min_periods=5).rank(pct=True)
        # Rolling sum windows (momentum-like)
        for w in (5, 20):
            g[f"foreign_sum_{w}d"] = g["foreign_net"].rolling(w, min_periods=1).sum()
            g[f"three_sum_{w}d"] = g["three_buy_net"].rolling(w, min_periods=1).sum()
        parts.append(g)
    feat = pd.concat(parts, ignore_index=True)

    feat = _merge_optional(
        feat,
        marg,
        ["margin_change", "margin_balance", "short_change", "short_balance"],
    )
    feat = _merge_optional(
        feat, lend, ["lending_balance", "lending_change", "fee_rate"]
    )
    feat = _merge_optional(
        feat, dt, ["daytrade_volume", "total_volume", "daytrade_ratio_pct"]
    )
    feat = _merge_optional(
        feat,
        sh,
        ["foreign_remaining_shares", "foreign_remaining_ratio", "shares_issued"],
    )
    feat = _merge_optional(
        feat,
        side,
        [
            "side_foreign",
            "side_trust",
            "side_dealer_self",
            "side_dealer_hedge",
            "side_foreign_dealer",
        ],
    )

    # Margin surge filter: change vs 20d abs median
    feat["margin_chg_abs_med20"] = (
        feat.groupby("sid")["margin_change"]
        .transform(lambda s: s.abs().rolling(20, min_periods=5).median())
    )
    feat["margin_surge"] = (
        feat["margin_change"].fillna(0) > 2.0 * feat["margin_chg_abs_med20"].fillna(np.inf)
    )

    # Enriched per-stock transforms
    out_parts: list[pd.DataFrame] = []
    for sid, g in feat.groupby("sid"):
        g = g.sort_values("d").copy()
        g["short_streak"] = _streak_positive(g["short_change"].fillna(0))
        g["lending_drop_streak"] = _streak_negative(g["lending_change"].fillna(0))
        g["lending_sum_5d"] = g["lending_change"].fillna(0).rolling(5, min_periods=1).sum()
        g["short_sum_5d"] = g["short_change"].fillna(0).rolling(5, min_periods=1).sum()
        g["foreign_ratio_chg"] = g["foreign_remaining_ratio"].diff()
        g["foreign_ratio_chg_5d"] = g["foreign_remaining_ratio"].diff(5)
        g["foreign_shares_chg"] = g["foreign_remaining_shares"].diff()
        g["dt_ratio_pctl20"] = (
            g["daytrade_ratio_pct"].rolling(20, min_periods=5).rank(pct=True)
        )
        g["dealer_hedge_net"] = g["side_dealer_hedge"]
        g["dealer_self_net"] = g["side_dealer_self"]
        g["dealer_hedge_streak"] = _streak_positive(g["side_dealer_hedge"].fillna(0))
        g["dealer_self_streak"] = _streak_positive(g["side_dealer_self"].fillna(0))
        g["side_foreign_dealer_net"] = g["side_foreign_dealer"]
        # Hedging vs self divergence (dealer inventory vs hedge book)
        g["dealer_book_spread"] = g["side_dealer_self"].fillna(0) - g["side_dealer_hedge"].fillna(0)
        out_parts.append(g)
    feat = pd.concat(out_parts, ignore_index=True)

    # HoldingSharesPer cache (weekly) → asof merge / ffill within stock
    if not hs_cache.empty:
        hs_cols = [c for c in hs_cache.columns if c not in ("sid", "d")]
        merged = []
        for sid, g in feat.groupby("sid"):
            g = g.sort_values("d").copy()
            h = hs_cache[hs_cache["sid"] == sid].sort_values("d").copy()
            if h.empty:
                for c in hs_cols:
                    g[c] = np.nan
            else:
                g["_dt"] = pd.to_datetime(g["d"])
                h["_dt"] = pd.to_datetime(h["d"])
                g = pd.merge_asof(
                    g.sort_values("_dt"),
                    h[["_dt"] + hs_cols].sort_values("_dt"),
                    on="_dt",
                    direction="backward",
                )
                g = g.drop(columns=["_dt"])
            merged.append(g)
        feat = pd.concat(merged, ignore_index=True)
    else:
        for c in ("big_holder_pct", "retail_holder_pct", "big_holder_pct_chg"):
            if c not in feat.columns:
                feat[c] = np.nan

    # 八大行庫 net cache
    if not gov_cache.empty:
        gcols = [c for c in ("gov_bank_net", "gov_bank_buy", "gov_bank_sell") if c in gov_cache.columns]
        feat = feat.merge(gov_cache[["sid", "d"] + gcols], on=["sid", "d"], how="left")
        feat["gov_bank_streak"] = (
            feat.groupby("sid")["gov_bank_net"]
            .transform(lambda s: _streak_positive(s.fillna(0)))
        )
        feat["gov_bank_sum_5d"] = (
            feat.groupby("sid")["gov_bank_net"]
            .transform(lambda s: s.fillna(0).rolling(5, min_periods=1).sum())
        )
    else:
        feat["gov_bank_net"] = np.nan
        feat["gov_bank_streak"] = 0
        feat["gov_bank_sum_5d"] = np.nan

    if not tx.empty:
        tx_cols = [
            "d",
            "net_oi_vol",
            "net_oi_vol_zscore",
            "net_oi_vol_cot_index",
            "tx_foreign_net_pos_streak",
            "tx_foreign_net_rising_streak",
        ]
        keep = [c for c in tx_cols if c in tx.columns]
        feat = feat.merge(tx[keep], on="d", how="left")
    else:
        feat["net_oi_vol"] = np.nan
        feat["net_oi_vol_zscore"] = np.nan
        feat["net_oi_vol_cot_index"] = np.nan
        feat["tx_foreign_net_pos_streak"] = 0
        feat["tx_foreign_net_rising_streak"] = 0

    return feat
