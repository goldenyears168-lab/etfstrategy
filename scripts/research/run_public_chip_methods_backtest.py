#!/usr/bin/env python3
"""Backtest public chip methods (FinLab / 森洋 / FinMind-style / common rules).

Universe: holdings-6 and/or expert-pool adopted.
Horizon: T+1 open→close (t1oc) and close→close H=5 (h5).
Signal uses only date≤T data; trade from T+1 open where applicable.
OOS cut: 2026-01-01.

Out: reports/research/chip-overlays/public_methods/
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
DB = ROOT / "data" / "stocks.db"
OUT = ROOT / "reports" / "research" / "chip-overlays" / "public_methods"
REGISTRY = (
    ROOT
    / "reports"
    / "research"
    / "branch-footprint-screen"
    / "expert_pool"
    / "EVAL_REGISTRY.json"
)

HOLDINGS = ["2327", "3189", "3653", "6505", "8046", "2492"]
START, WARMUP, END = "2024-07-01", "2024-03-01", "2026-07-24"
OOS = "2026-01-01"
MIN_N = 15
COST_BPS = 30.0  # round-trip proxy on h5 when evaluating tradable

SOURCES = {
    "M1_finlab_follow_IT10": "FinLab · 跟投信10日累計買超",
    "M2_finlab_IT_buy_F_sell": "FinLab · 投信買+外資賣（土洋對作）",
    "M3_finlab_IT_rev": "FinLab · 投信累計×營收YoY（薄覆蓋）",
    "M4_finlab_3factor_top": "FinLab blog · 投信強度/持續+外資強度 橫截面前段",
    "M5_finmind_climax_fade": "FinMind Follower 風格 · 法人高潮後偏空（反做）",
    "M6_senyang_sync3": "森洋 · 三大法人同步買超",
    "M7_senyang_Fbuy_margin_down": "森洋 · 外資買+融資減少（籌碼集中）",
    "M8_margin_reduce_short_up": "森洋融資券 · 資減券增連續",
    "M9_foreign_streak5": "常見教學 · 外資連買≥5日",
    "M10_IT_streak3": "常見教學 · 投信連買≥3日",
    "M11_foreign_holding_up": "集保/FinMind · 外資持股比上升",
    "M12_contrarian_F_sell_streak": "常見反做 · 外資連賣≥5日（逆向做多）",
}


def adopted_ids() -> list[str]:
    raw = json.loads(REGISTRY.read_text(encoding="utf-8"))
    return sorted(
        {
            r["stock_id"]
            for r in raw.get("stocks", [])
            if r.get("status") == "adopted" and str(r.get("stock_id", "")).isdigit()
        }
    )


def load(sids: list[str]) -> pd.DataFrame:
    conn = sqlite3.connect(f"file:{DB.resolve()}?mode=ro", uri=True)
    ph = ",".join("?" * len(sids))
    bars = pd.read_sql_query(
        f"""SELECT stock_id,trade_date,open,close,volume,amount
            FROM stock_daily_bars WHERE source='finmind' AND stock_id IN ({ph})
            AND trade_date>=? AND trade_date<=?""",
        conn,
        params=[*sids, WARMUP, END],
    )
    inst = pd.read_sql_query(
        f"""SELECT stock_id,trade_date,foreign_net,investment_trust_net,dealer_self_net
            FROM stock_institutional_daily WHERE source='finmind' AND stock_id IN ({ph})
            AND trade_date>=? AND trade_date<=?""",
        conn,
        params=[*sids, WARMUP, END],
    )
    margin = pd.read_sql_query(
        f"""SELECT stock_id,trade_date,margin_change,short_change,margin_balance,short_balance
            FROM stock_margin_daily WHERE stock_id IN ({ph})
            AND trade_date>=? AND trade_date<=?""",
        conn,
        params=[*sids, WARMUP, END],
    )
    sh = pd.read_sql_query(
        f"""SELECT stock_id,trade_date,foreign_remaining_ratio
            FROM stock_shareholding_daily WHERE stock_id IN ({ph})
            AND trade_date>=? AND trade_date<=?""",
        conn,
        params=[*sids, WARMUP, END],
    )
    fund = pd.read_sql_query(
        f"""SELECT stock_id,as_of_date,revenue_yoy_pct FROM stock_fundamental
            WHERE stock_id IN ({ph}) AND revenue_yoy_pct IS NOT NULL""",
        conn,
        params=sids,
    )
    conn.close()
    for d in (bars, inst, margin, sh):
        d["trade_date"] = pd.to_datetime(d["trade_date"])
    fund["as_of_date"] = pd.to_datetime(fund["as_of_date"])

    df = bars.merge(inst, on=["stock_id", "trade_date"], how="inner")
    df = df.merge(margin, on=["stock_id", "trade_date"], how="left")
    df = df.merge(sh, on=["stock_id", "trade_date"], how="left")
    df = df.sort_values(["stock_id", "trade_date"]).reset_index(drop=True)

    # PIT revenue: as_of_date available that day (merge_asof per stock)
    parts = []
    for sid, g in df.groupby("stock_id", sort=False):
        g = g.sort_values("trade_date")
        f = fund[fund.stock_id == sid].sort_values("as_of_date")
        if f.empty:
            g["revenue_yoy_pct"] = np.nan
        else:
            g = pd.merge_asof(
                g,
                f[["as_of_date", "revenue_yoy_pct"]].rename(
                    columns={"as_of_date": "trade_date"}
                ),
                on="trade_date",
                direction="backward",
            )
        parts.append(g)
    df = pd.concat(parts, ignore_index=True)

    df["F"] = df["foreign_net"].astype(float)
    df["IT"] = df["investment_trust_net"].astype(float)
    df["D"] = df["dealer_self_net"].astype(float)
    df["FT"] = df["F"] + df["IT"]
    g = df.groupby("stock_id", sort=False)
    df["vol"] = df["volume"].astype(float)
    df["dv"] = df["amount"].astype(float)
    miss = df["dv"].isna() | (df["dv"] <= 0)
    df.loc[miss, "dv"] = df.loc[miss, "close"].astype(float) * df.loc[miss, "vol"]
    df["adv20"] = g["dv"].transform(lambda s: s.shift(1).rolling(20, min_periods=10).mean())
    df["liquid"] = df["adv20"] > 1e7  # ~1000萬

    # rolling chip
    df["IT10"] = g["IT"].transform(lambda s: s.rolling(10, min_periods=5).sum())
    df["F10"] = g["F"].transform(lambda s: s.rolling(10, min_periods=5).sum())
    df["D10"] = g["D"].transform(lambda s: s.rolling(10, min_periods=5).sum())
    df["vol60"] = g["vol"].transform(lambda s: s.rolling(60, min_periods=30).sum())
    df["IT60"] = g["IT"].transform(lambda s: s.rolling(60, min_periods=30).sum())
    df["F60"] = g["F"].transform(lambda s: s.rolling(60, min_periods=30).sum())
    df["IT_pos_rate60"] = g["IT"].transform(
        lambda s: (s > 0).rolling(60, min_periods=30).mean()
    )
    df["trust_intensity"] = df["IT60"] / df["vol60"].replace(0, np.nan)
    df["foreign_intensity"] = df["F60"] / df["vol60"].replace(0, np.nan)

    # z of FT for climax
    mu = g["FT"].transform(lambda s: s.shift(1).rolling(21, min_periods=10).mean())
    sd = g["FT"].transform(lambda s: s.shift(1).rolling(21, min_periods=10).std())
    df["ft_z"] = (df["FT"] - mu) / sd.replace(0, np.nan)

    # streaks
    def streak_pos(s: pd.Series) -> pd.Series:
        pos = s > 0
        return pos.groupby((~pos).cumsum()).cumsum()

    def streak_neg(s: pd.Series) -> pd.Series:
        neg = s < 0
        return neg.groupby((~neg).cumsum()).cumsum()

    df["F_streak"] = g["F"].transform(streak_pos)
    df["IT_streak"] = g["IT"].transform(streak_pos)
    df["F_sell_streak"] = g["F"].transform(streak_neg)

    # margin streaks
    df["m_down"] = g["margin_change"].transform(
        lambda s: (s < 0).groupby((s >= 0).cumsum()).cumsum()
    )
    df["s_up"] = g["short_change"].transform(
        lambda s: (s > 0).groupby((s <= 0).cumsum()).cumsum()
    )

    df["fr_ratio"] = df["foreign_remaining_ratio"].astype(float)
    df["fr_up5"] = g["fr_ratio"].transform(lambda s: s - s.shift(5))

    # forward returns
    o1 = g["open"].shift(-1)
    c1 = g["close"].shift(-1)
    c5 = g["close"].shift(-5)
    df["t1oc"] = (c1 / o1 - 1.0) * 100.0
    df["h5"] = (c5 / df["close"] - 1.0) * 100.0
    for col in ("t1oc", "h5"):
        df.loc[df[col].abs() > 40, col] = np.nan

    df["ma60"] = g["close"].transform(lambda s: s.rolling(60, min_periods=40).mean())
    df["above_ma60"] = df["close"] > df["ma60"]
    return df


def add_cross_section_ranks(df: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional percentile ranks within each day (universe-local)."""
    df = df.copy()
    for col, out in [
        ("trust_intensity", "r_trust_int"),
        ("IT_pos_rate60", "r_trust_pers"),
        ("foreign_intensity", "r_foreign_int"),
        ("IT10", "r_IT10"),
        ("F10", "r_F10"),
    ]:
        df[out] = df.groupby("trade_date", sort=False)[col].rank(pct=True)
    df["score3"] = df["r_trust_int"] + df["r_trust_pers"] + df["r_foreign_int"]
    df["r_score3"] = df.groupby("trade_date", sort=False)["score3"].rank(pct=True)
    return df


def signals(df: pd.DataFrame) -> dict[str, pd.Series]:
    liq = df["liquid"].fillna(False)
    return {
        "M1_finlab_follow_IT10": liq & (df["IT10"] > 0) & (df["r_IT10"] >= 0.8),
        "M2_finlab_IT_buy_F_sell": liq & (df["IT10"] > 0) & (df["F10"] <= 0),
        "M3_finlab_IT_rev": liq
        & (df["IT10"] > 0)
        & (df["r_IT10"] >= 0.7)
        & (df["revenue_yoy_pct"] > 20),
        "M4_finlab_3factor_top": liq
        & df["above_ma60"].fillna(False)
        & (df["r_score3"] >= 0.8),
        # climax fade: signal day abnormal buy → expect weak forward (bearish for long)
        "M5_finmind_climax_fade": liq & (df["ft_z"] >= 2.0) & (df["FT"] > 0),
        "M6_senyang_sync3": liq & (df["F"] > 0) & (df["IT"] > 0) & (df["D"] > 0),
        "M7_senyang_Fbuy_margin_down": liq
        & (df["F"] > 0)
        & (df["margin_change"] < 0),
        "M8_margin_reduce_short_up": liq & (df["m_down"] >= 3) & (df["s_up"] >= 3),
        "M9_foreign_streak5": liq & (df["F_streak"] >= 5),
        "M10_IT_streak3": liq & (df["IT_streak"] >= 3),
        "M11_foreign_holding_up": liq & (df["fr_up5"] > 0.3),  # +0.3pp in 5d
        "M12_contrarian_F_sell_streak": liq & (df["F_sell_streak"] >= 5),
    }


BEARISH = {"M5_finmind_climax_fade"}  # useful if forward excess < 0


def eval_method(
    df: pd.DataFrame, name: str, mask: pd.Series, split: str
) -> list[dict]:
    sub = df[df["trade_date"] >= START].copy()
    if split == "OOS":
        sub = sub[sub["trade_date"] >= OOS]
    elif split == "IS":
        sub = sub[sub["trade_date"] < OOS]
    m = mask.reindex(sub.index).fillna(False)
    rows = []
    for sid, g in sub.groupby("stock_id", sort=False):
        gm = m.loc[g.index]
        for horizon in ("t1oc", "h5"):
            base = g[horizon].dropna()
            if base.empty:
                continue
            base_mean = float(base.mean())
            ev = g.loc[gm, horizon].dropna()
            n = int(len(ev))
            if n < MIN_N:
                rows.append(
                    dict(
                        method=name,
                        stock_id=sid,
                        split=split,
                        horizon=horizon,
                        n=n,
                        mean=np.nan,
                        base=base_mean,
                        excess=np.nan,
                        hit=np.nan,
                        useful=False,
                    )
                )
                continue
            mean = float(ev.mean())
            excess = mean - base_mean
            hit = float((ev > 0).mean())
            if name in BEARISH:
                useful = excess < -0.15  # soft directional for risk filter
            else:
                useful = excess > 0.15
            rows.append(
                dict(
                    method=name,
                    stock_id=sid,
                    split=split,
                    horizon=horizon,
                    n=n,
                    mean=mean,
                    base=base_mean,
                    excess=excess,
                    hit=hit,
                    useful=useful,
                )
            )
    return rows


def summarize(detail: pd.DataFrame, universe: str) -> pd.DataFrame:
    rows = []
    for method, src in SOURCES.items():
        for split in ("ALL", "OOS"):
            for horizon in ("t1oc", "h5"):
                d = detail[
                    (detail.method == method)
                    & (detail.split == split)
                    & (detail.horizon == horizon)
                    & detail.excess.notna()
                ]
                if d.empty:
                    continue
                ex = d["excess"]
                rows.append(
                    dict(
                        universe=universe,
                        method=method,
                        source=src,
                        split=split,
                        horizon=horizon,
                        n_stocks=int(len(d)),
                        med_excess=float(ex.median()),
                        mean_excess=float(ex.mean()),
                        frac_pos=float((ex > 0).mean()),
                        frac_useful=float(d["useful"].mean()),
                        med_n=float(d["n"].median()),
                        h5_net_cost=float(ex.median() - COST_BPS / 100.0)
                        if horizon == "h5"
                        else np.nan,
                    )
                )
    return pd.DataFrame(rows)


def run_universe(sids: list[str], label: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    df = load(sids)
    df = add_cross_section_ranks(df)
    sigs = signals(df)
    rows = []
    for name, mask in sigs.items():
        for split in ("ALL", "IS", "OOS"):
            rows.extend(eval_method(df, name, mask, split))
    detail = pd.DataFrame(rows)
    detail["universe"] = label
    summary = summarize(detail, label)
    return detail, summary


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe", choices=("holdings", "expert", "both"), default="both")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)

    details, summaries = [], []
    if args.universe in ("holdings", "both"):
        d, s = run_universe(HOLDINGS, "holdings")
        details.append(d)
        summaries.append(s)
    if args.universe in ("expert", "both"):
        d, s = run_universe(adopted_ids(), "expert")
        details.append(d)
        summaries.append(s)

    detail = pd.concat(details, ignore_index=True)
    summary = pd.concat(summaries, ignore_index=True)
    detail.to_csv(OUT / "method_detail_by_stock.csv", index=False)
    summary.to_csv(OUT / "method_summary.csv", index=False)

    # leaderboard: expert OOS h5 by med_excess
    board = summary[
        (summary.universe == "expert")
        & (summary.split == "OOS")
        & (summary.horizon == "h5")
    ].sort_values("med_excess", ascending=False)
    board.to_csv(OUT / "leaderboard_expert_oos_h5.csv", index=False)

    board_h = summary[
        (summary.universe == "holdings")
        & (summary.split == "OOS")
        & (summary.horizon == "h5")
    ].sort_values("med_excess", ascending=False)

    print("=== EXPERT OOS H5 leaderboard ===")
    print(
        board[
            [
                "method",
                "med_excess",
                "mean_excess",
                "frac_pos",
                "frac_useful",
                "n_stocks",
                "med_n",
                "h5_net_cost",
            ]
        ].to_string(index=False)
    )
    print("\n=== HOLDINGS OOS H5 ===")
    print(
        board_h[
            ["method", "med_excess", "frac_pos", "n_stocks", "med_n"]
        ].to_string(index=False)
    )

    meta = {
        "sources": SOURCES,
        "oos": OOS,
        "cost_bps": COST_BPS,
        "refs": [
            "https://finlab.finance/blog/institutional-investors-comparison",
            "https://finlab.finance/blog/institutional-strategy",
            "https://finlab.finance/blog/claude-code-finlab-quant-trading",
            "https://allmoneyinone.com/taiwan-stock-chip-analysis/",
            "https://allmoneyinone.com/chip-margin-trading-practical-strategy/",
            "https://finmind.github.io/tutor/TaiwanMarket/Chip/",
        ],
    }
    (OUT / "methods_catalog.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("wrote", OUT)


if __name__ == "__main__":
    main()
