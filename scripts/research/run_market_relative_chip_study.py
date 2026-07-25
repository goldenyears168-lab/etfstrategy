#!/usr/bin/env python3
"""Market-relative chip × price: self vs market, matched same-tape days, reverse tests.

Blind spots this study closes vs ft3_rel_trend / divergence_v2:
  B1  Prior work de-meaned vs cross-sectional median stock FT — not official
      大盤外資+投信 (TaiwanStockTotalInstitutionalInvestors).
  B2  Never matched on the SAME market price day: 「大盤一樣跌，籌碼相對不同」.
  B3  Never systematically tested the CONTRARIAN / reverse-indicator hypothesis
      (high relative chip → subsequent underperformance), which Taiwan VAR and
      Chordia OIB literature both leave open.

Academic priors used as design, not as proof:
  - Taiwan threshold VAR (Lin et al.): foreign flow leads TSE mainly in hot /
    NASDAQ-up regimes; domestic trusts can negatively lead returns.
  - Chordia & Subrahmanyam: aggregate order imbalance is CONTRARIAN (buy after
    declines); imbalance levels persist but first differences reverse.
  - Campbell / Ramadorai daily institutional flows: institutions momentum-trade;
    net flow can positively OR negatively predict depending on construction.
  - In-repo skill: FT_ntd / market_gross (never stock_net/market_net).

Protocol
  - Features ≤ T close. Outcomes = stock excess vs IX0001 β-adjusted, T→T+h.
  - Market FT = official Foreign + Investment Trust net/gross NTD.
  - IS = [2019-01-01, 2026); OOS = [2026, end]. OOS read once.
  - Every chip claim vs a matched price/tape control; reverse arms reported.

Out: reports/research/chip-overlays/market_relative_chip/
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
DB = ROOT / "data" / "stocks.db"
OUT = ROOT / "reports" / "research" / "chip-overlays" / "market_relative_chip"
CACHE = OUT / "cache_market_ft_totals.csv"
SOURCE = "finmind"
MIN_BARS = 1500
IS_START = "2019-01-01"
OOS = "2026-01-01"
HORIZONS = (1, 5, 10)
PRIMARY_H = 5
FT3_DAYS = 3
ABS_DAYS = 20
ABS_MIN = 10
PCTL_WIN = 252
PCTL_MIN = 120
GROSS_FLOOR = 5e10  # NT$500億
DEEP = "2492"
BETA_WIN = 60


def load_market() -> pd.DataFrame:
    m = pd.read_csv(CACHE)
    m["date"] = pd.to_datetime(m["date"])
    m = m.rename(columns={"date": "trade_date"})
    m["mkt_ft"] = m["net_FT"]
    m["mkt_f"] = m["net_Foreign_Investor"]
    m["mkt_it"] = m["net_Investment_Trust"]
    m["mkt_gross"] = m["gross_FT"]
    # Official 大盤 ft3_rel_20 on market net FT (same definition as Live TA).
    m["mkt_ft3"] = m["mkt_ft"].rolling(FT3_DAYS, min_periods=FT3_DAYS).sum()
    m["mkt_abs20"] = m["mkt_ft"].abs().rolling(ABS_DAYS, min_periods=ABS_MIN).sum()
    m["m_r20"] = m["mkt_ft3"] / m["mkt_abs20"].replace(0, np.nan)
    m["m_r20_d1"] = m["m_r20"].diff(1)
    return m[
        [
            "trade_date",
            "mkt_ft",
            "mkt_f",
            "mkt_it",
            "mkt_gross",
            "m_r20",
            "m_r20_d1",
        ]
    ]


def load_universe() -> pd.DataFrame:
    uri = f"file:{DB.resolve()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        sids = [
            r[0]
            for r in conn.execute(
                "SELECT stock_id FROM stock_daily_bars WHERE source=? "
                "GROUP BY stock_id HAVING COUNT(*)>=?",
                (SOURCE, MIN_BARS),
            )
        ]
        ph = ",".join("?" * len(sids))
        bars = pd.read_sql_query(
            f"SELECT stock_id, trade_date, close, volume FROM stock_daily_bars "
            f"WHERE source=? AND stock_id IN ({ph}) ORDER BY stock_id, trade_date",
            conn,
            params=[SOURCE, *sids],
        )
        inst = pd.read_sql_query(
            f"SELECT stock_id, trade_date, "
            f"COALESCE(foreign_net,0) AS f_net, "
            f"COALESCE(investment_trust_net,0) AS it_net "
            f"FROM stock_institutional_daily WHERE source=? AND stock_id IN ({ph})",
            conn,
            params=[SOURCE, *sids],
        )
        ix = pd.read_sql_query(
            """SELECT date AS trade_date, close AS ix FROM daily_bars
               WHERE code='IX0001'
               ORDER BY date,
                 CASE source WHEN 'yahoo' THEN 1 WHEN 'finmind' THEN 2 ELSE 9 END""",
            conn,
        ).drop_duplicates("trade_date", keep="first")
    for d in (bars, inst, ix):
        d["trade_date"] = pd.to_datetime(d["trade_date"])
    bars = bars[bars["close"] > 0]
    df = bars.merge(inst, on=["stock_id", "trade_date"], how="left")
    df = df.merge(ix, on="trade_date", how="left")
    df = df.merge(load_market(), on="trade_date", how="left")
    return df.sort_values(["stock_id", "trade_date"]).reset_index(drop=True)


def _rolling_beta(y: pd.Series, x: pd.Series, win: int = BETA_WIN) -> pd.Series:
    out = np.full(len(y), np.nan)
    ya, xa = y.to_numpy(float), x.to_numpy(float)
    for i in range(win, len(y)):
        yy, xx = ya[i - win : i], xa[i - win : i]
        m = np.isfinite(yy) & np.isfinite(xx)
        if m.sum() < 40:
            continue
        xc = xx[m] - xx[m].mean()
        yc = yy[m] - yy[m].mean()
        v = float(np.dot(xc, xc))
        if v > 0:
            out[i] = float(np.dot(xc, yc) / v)
    return pd.Series(out, index=y.index).clip(0.2, 3.0)


def add_features(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    g = out.groupby("stock_id", sort=False)
    out["ret1"] = g["close"].pct_change(fill_method=None) * 100.0
    out["mkt_ret1"] = out["ix"].pct_change(fill_method=None) * 100.0

    # Per-stock β vs IX0001 (past-only), then excess forward return.
    betas = []
    for _, chunk in out.groupby("stock_id", sort=False):
        b = _rolling_beta(chunk["ret1"] / 100.0, chunk["mkt_ret1"] / 100.0)
        betas.append(b)
    out["beta"] = pd.concat(betas).sort_index()
    out["exret"] = out["ret1"] - out["beta"].shift(0) * out["mkt_ret1"]
    # shift beta one day so today's excess uses yesterday's β (PIT)
    out["beta_lag"] = g["beta"].shift(1)
    out["exret"] = out["ret1"] - out["beta_lag"] * out["mkt_ret1"]

    g = out.groupby("stock_id", sort=False)
    out["ft"] = out["f_net"] + out["it_net"]
    out["ft_ntd"] = out["ft"] * out["close"]
    out["f_ntd"] = out["f_net"] * out["close"]
    out["it_ntd"] = out["it_net"] * out["close"]

    # Live TA self metric
    ft3 = g["ft"].transform(lambda s: s.rolling(FT3_DAYS, min_periods=FT3_DAYS).sum())
    abs20 = g["ft"].transform(
        lambda s: s.abs().rolling(ABS_DAYS, min_periods=ABS_MIN).sum()
    )
    out["r20"] = ft3 / abs20.replace(0, np.nan)
    out["r20_d1"] = g["r20"].diff(1)

    # Relative-to-market intensity (skill SSOT; floor on market gross)
    out["ft_part"] = out["ft_ntd"] / out["mkt_gross"].replace(0, np.nan)
    out["ft_part_ok"] = out["mkt_gross"] >= GROSS_FLOOR
    out["f_part"] = out["f_ntd"] / out["mkt_gross"].replace(0, np.nan)
    out["it_part"] = out["it_ntd"] / out["mkt_gross"].replace(0, np.nan)

    # Self history PIT percentile AND relative-to-market residual
    out["r20_pctl"] = g["r20"].transform(
        lambda s: s.rolling(PCTL_WIN, min_periods=PCTL_MIN).rank(pct=True)
    )
    out["ft_part_pctl"] = (
        out.groupby("stock_id", sort=False)["ft_part"]
        .transform(lambda s: s.rolling(PCTL_WIN, min_periods=PCTL_MIN).rank(pct=True))
        .where(out["ft_part_ok"])
    )

    # Alignment / scoop / dump vs official 大盤法人
    out["mkt_buy"] = out["mkt_ft"] > 0
    out["stk_buy"] = out["ft_ntd"] > 0
    out["align"] = np.sign(out["ft_ntd"]) * np.sign(out["mkt_ft"])
    out["rel_scoop"] = (~out["mkt_buy"]) & out["stk_buy"]  # 大盤賣、個股買
    out["rel_dump"] = out["mkt_buy"] & (~out["stk_buy"])  # 大盤買、個股賣
    out["aligned_buy"] = out["mkt_buy"] & out["stk_buy"]
    out["aligned_sell"] = (~out["mkt_buy"]) & (~out["stk_buy"])

    # Gap: stock r20 minus market r20 (both same definition)
    out["r20_gap"] = out["r20"] - out["m_r20"]
    out["r20_gap_pctl"] = g["r20_gap"].transform(
        lambda s: s.rolling(PCTL_WIN, min_periods=PCTL_MIN).rank(pct=True)
    )

    # Tape regimes
    out["mkt_down"] = out["mkt_ret1"] < 0
    out["mkt_up"] = out["mkt_ret1"] > 0
    out["stk_down"] = out["ret1"] < 0
    out["stk_up"] = out["ret1"] > 0
    out["turnover"] = out["close"] * out["volume"]
    out["hot"] = out["turnover"] > g["turnover"].transform(
        lambda s: s.shift(1).rolling(PCTL_WIN, min_periods=PCTL_MIN).median()
    )

    # Forward β-excess return vs IX0001
    g = out.groupby("stock_id", sort=False)
    for h in HORIZONS:
        fwd_s = (g["close"].shift(-h) / out["close"] - 1.0) * 100.0
        fwd_m = (g["ix"].shift(-h) / out["ix"] - 1.0) * 100.0
        out[f"x{h}"] = fwd_s - out["beta_lag"] * fwd_m
    return out


def _summ(x: pd.Series) -> dict:
    x = x.dropna()
    if len(x) == 0:
        return {"n": 0}
    return {
        "n": int(len(x)),
        "med": round(float(x.median()), 3),
        "mean": round(float(x.mean()), 3),
        "win": round(float((x > 0).mean() * 100), 1),
    }


def evaluate_arms(d: pd.DataFrame, arms: dict[str, pd.Series], target: str) -> dict:
    base = d[target].dropna()
    out = {"baseline": _summ(base)}
    for name, mask in arms.items():
        m = mask.fillna(False) if mask.dtype != bool else mask
        x = d.loc[m, target].dropna()
        row = _summ(x)
        if row["n"]:
            row["lift_vs_all"] = round(float(x.median() - base.median()), 3)
        out[name] = row
    return out


def matched_spread(
    d: pd.DataFrame, a: pd.Series, b: pd.Series, target: str, min_n: int = 100
) -> dict:
    xa = d.loc[a.fillna(False), target].dropna()
    xb = d.loc[b.fillna(False), target].dropna()
    if len(xa) < min_n or len(xb) < min_n:
        return {"n_a": int(len(xa)), "n_b": int(len(xb)), "spread": None}
    return {
        "n_a": int(len(xa)),
        "n_b": int(len(xb)),
        "med_a": round(float(xa.median()), 3),
        "med_b": round(float(xb.median()), 3),
        "spread": round(float(xa.median() - xb.median()), 3),
        "win_a": round(float((xa > 0).mean() * 100), 1),
        "win_b": round(float((xb > 0).mean() * 100), 1),
    }


def phase_market_diagnostics(d: pd.DataFrame) -> dict:
    """大盤本身：外資／投信／FT 與 IX0001 同日／前瞻相關。"""
    m = d.drop_duplicates("trade_date").sort_values("trade_date")
    m = m.dropna(subset=["m_r20", "mkt_ret1"])
    for h in HORIZONS:
        m[f"m_fwd{h}"] = m["ix"].shift(-h) / m["ix"] - 1.0
    same = {}
    fwd = {}
    for name, series in [
        ("m_r20", m["m_r20"]),
        ("mkt_ft", m["mkt_ft"]),
        ("mkt_f", m["mkt_f"]),
        ("mkt_it", m["mkt_it"]),
    ]:
        same[name] = round(float(series.corr(m["mkt_ret1"], method="spearman")), 4)
        fwd[name] = {
            f"h{h}": round(float(series.corr(m[f"m_fwd{h}"], method="spearman")), 4)
            for h in HORIZONS
        }
    # reverse test: high m_r20 → subsequent market weakness?
    q_hi = m["m_r20"].rolling(PCTL_WIN, min_periods=PCTL_MIN).rank(pct=True)
    hi = m[q_hi >= 0.8][f"m_fwd{PRIMARY_H}"].dropna()
    lo = m[q_hi <= 0.2][f"m_fwd{PRIMARY_H}"].dropna()
    return {
        "same_day_spearman_vs_ix": same,
        "forward_spearman": fwd,
        "m_r20_hi_vs_lo_fwd": {
            "h": PRIMARY_H,
            "hi_med_pct": round(float(hi.median() * 100), 3) if len(hi) else None,
            "lo_med_pct": round(float(lo.median() * 100), 3) if len(lo) else None,
            "spread_hi_minus_lo": (
                round(float((hi.median() - lo.median()) * 100), 3)
                if len(hi) and len(lo)
                else None
            ),
            "n_hi": int(len(hi)),
            "n_lo": int(len(lo)),
            "note": "positive spread = momentum; negative = reverse indicator at market level",
        },
    }


def phase_matched_tape(d: pd.DataFrame, split: str) -> dict:
    """User's core claim: same market price day, relative chip differs → outcomes differ."""
    target = f"x{PRIMARY_H}"
    res = {"split": split, "horizon": PRIMARY_H}
    # 大盤跌價日
    md = d[d["mkt_down"].fillna(False)]
    res["mkt_down"] = {
        "baseline": _summ(md[target]),
        "rel_scoop_vs_aligned_sell": matched_spread(
            md, md["rel_scoop"], md["aligned_sell"], target
        ),
        "rel_scoop_vs_rel_dump": matched_spread(
            md, md["rel_scoop"], md["rel_dump"], target
        ),
        "aligned_sell_vs_all_mkt_down": matched_spread(
            md, md["aligned_sell"], pd.Series(True, index=md.index), target
        ),
        # reverse: on market-down, being a relative BUYER is BAD?
        "CTRL_stk_down_only": _summ(md.loc[md["stk_down"].fillna(False), target]),
        "CTRL_stk_up_only": _summ(md.loc[md["stk_up"].fillna(False), target]),
    }
    # 大盤漲價日
    mu = d[d["mkt_up"].fillna(False)]
    res["mkt_up"] = {
        "baseline": _summ(mu[target]),
        "rel_dump_vs_aligned_buy": matched_spread(
            mu, mu["rel_dump"], mu["aligned_buy"], target
        ),
        "aligned_buy_vs_rel_scoop": matched_spread(
            mu, mu["aligned_buy"], mu["rel_scoop"], target
        ),
    }
    # 個股跌價日（價格對照）內：相對吸籌 vs 相對出貨
    sd = d[d["stk_down"].fillna(False)]
    res["stk_down"] = {
        "baseline": _summ(sd[target]),
        "rel_scoop_vs_rel_dump": matched_spread(
            sd, sd["rel_scoop"], sd["rel_dump"], target
        ),
        "r20_gap_hi_vs_lo": matched_spread(
            sd,
            sd["r20_gap_pctl"] >= 0.8,
            sd["r20_gap_pctl"] <= 0.2,
            target,
        ),
    }
    return res


def phase_self_vs_market(d: pd.DataFrame, split: str) -> dict:
    """Self-history r20 vs market-relative ft_part / r20_gap — which sorts better?"""
    target = f"x{PRIMARY_H}"
    arms = {
        "SELF_r20_hi": d["r20_pctl"] >= 0.8,
        "SELF_r20_lo": d["r20_pctl"] <= 0.2,
        "MKTREL_part_hi": (d["ft_part_pctl"] >= 0.8) & d["ft_part_ok"],
        "MKTREL_part_lo": (d["ft_part_pctl"] <= 0.2) & d["ft_part_ok"],
        "MKTREL_gap_hi": d["r20_gap_pctl"] >= 0.8,
        "MKTREL_gap_lo": d["r20_gap_pctl"] <= 0.2,
        "ALIGN_buy": d["aligned_buy"],
        "ALIGN_sell": d["aligned_sell"],
        "REL_scoop": d["rel_scoop"],
        "REL_dump": d["rel_dump"],
    }
    res = evaluate_arms(d, arms, target)
    # spreads (momentum reading: hi − lo; reverse if negative)
    res["spreads"] = {
        "self_r20_hi_minus_lo": matched_spread(
            d, arms["SELF_r20_hi"], arms["SELF_r20_lo"], target
        ),
        "mktrel_part_hi_minus_lo": matched_spread(
            d, arms["MKTREL_part_hi"], arms["MKTREL_part_lo"], target
        ),
        "mktrel_gap_hi_minus_lo": matched_spread(
            d, arms["MKTREL_gap_hi"], arms["MKTREL_gap_lo"], target
        ),
        "scoop_minus_dump": matched_spread(
            d, arms["REL_scoop"], arms["REL_dump"], target
        ),
    }
    res["split"] = split
    return res


def phase_reverse_and_regime(d: pd.DataFrame) -> dict:
    """Explicit reverse-indicator tests + hot/cold × market-tape."""
    target = f"x{PRIMARY_H}"
    is_m = (d["trade_date"] >= IS_START) & (d["trade_date"] < OOS)
    oos_m = d["trade_date"] >= OOS
    res = {}
    for split, mask in (("IS", is_m), ("OOS", oos_m)):
        s = d[mask]
        entry = {}
        # If high relative chip is a REVERSE indicator, hi−lo spread is negative.
        for label, hi, lo in (
            (
                "self_r20",
                s["r20_pctl"] >= 0.8,
                s["r20_pctl"] <= 0.2,
            ),
            (
                "mktrel_gap",
                s["r20_gap_pctl"] >= 0.8,
                s["r20_gap_pctl"] <= 0.2,
            ),
            (
                "mktrel_part",
                (s["ft_part_pctl"] >= 0.8) & s["ft_part_ok"],
                (s["ft_part_pctl"] <= 0.2) & s["ft_part_ok"],
            ),
        ):
            entry[label] = matched_spread(s, hi, lo, target)
            # classify
            sp = entry[label].get("spread")
            if sp is None:
                entry[label]["reading"] = "INSUFFICIENT"
            elif sp >= 0.3:
                entry[label]["reading"] = "MOMENTUM"
            elif sp <= -0.3:
                entry[label]["reading"] = "REVERSE"
            else:
                entry[label]["reading"] = "FLAT"
        # regime × tape
        for tape, tmask in (
            ("mkt_down", s["mkt_down"].fillna(False)),
            ("mkt_up", s["mkt_up"].fillna(False)),
            ("hot", s["hot"].fillna(False)),
            ("cold", (~s["hot"]).fillna(False)),
        ):
            sub = s[tmask]
            entry[f"scoop_vs_dump_in_{tape}"] = matched_spread(
                sub, sub["rel_scoop"], sub["rel_dump"], target, min_n=50
            )
        res[split] = entry
    return res


def phase_2492(d: pd.DataFrame) -> dict:
    s = d[d["stock_id"] == DEEP].copy()
    is_m = (s["trade_date"] >= IS_START) & (s["trade_date"] < OOS)
    oos_m = s["trade_date"] >= OOS
    target = f"x{PRIMARY_H}"
    out = {}
    for split, mask in (("IS", is_m), ("OOS", oos_m), ("ALL", s["trade_date"] >= IS_START)):
        sub = s[mask]
        md = sub[sub["mkt_down"].fillna(False)]
        sd = sub[sub["stk_down"].fillna(False)]
        out[split] = {
            "n": int(len(sub)),
            "matched_mkt_down_scoop_vs_aligned_sell": matched_spread(
                md, md["rel_scoop"], md["aligned_sell"], target, min_n=20
            ),
            "matched_stk_down_scoop_vs_dump": matched_spread(
                sd, sd["rel_scoop"], sd["rel_dump"], target, min_n=20
            ),
            "self_r20_hi_lo": matched_spread(
                sub, sub["r20_pctl"] >= 0.8, sub["r20_pctl"] <= 0.2, target, min_n=20
            ),
            "gap_hi_lo": matched_spread(
                sub,
                sub["r20_gap_pctl"] >= 0.8,
                sub["r20_gap_pctl"] <= 0.2,
                target,
                min_n=20,
            ),
            "arms": evaluate_arms(
                sub,
                {
                    "rel_scoop": sub["rel_scoop"],
                    "rel_dump": sub["rel_dump"],
                    "aligned_buy": sub["aligned_buy"],
                    "aligned_sell": sub["aligned_sell"],
                },
                target,
            ),
        }
    return out


def yearly_robustness(d: pd.DataFrame) -> dict:
    target = f"x{PRIMARY_H}"
    d = d[d["trade_date"] >= IS_START].copy()
    d["year"] = d["trade_date"].dt.year
    rows = {}
    for y, chunk in d.groupby("year"):
        md = chunk[chunk["mkt_down"].fillna(False)]
        rows[int(y)] = {
            "mkt_down_scoop_vs_aligned_sell": matched_spread(
                md, md["rel_scoop"], md["aligned_sell"], target, min_n=30
            ),
            "gap_hi_minus_lo": matched_spread(
                chunk,
                chunk["r20_gap_pctl"] >= 0.8,
                chunk["r20_gap_pctl"] <= 0.2,
                target,
                min_n=30,
            ),
        }
    return rows


def phase_intensity_and_agents(d: pd.DataFrame) -> dict:
    """Binary scoop may be too coarse — condition on intensity; split F vs IT."""
    target = f"x{PRIMARY_H}"
    is_m = (d["trade_date"] >= IS_START) & (d["trade_date"] < OOS)
    oos_m = d["trade_date"] >= OOS
    res: dict = {}
    for split, mask in (("IS", is_m), ("OOS", oos_m)):
        s = d[mask]
        md = s[s["mkt_down"].fillna(False)]
        mu = s[s["mkt_up"].fillna(False)]
        strong_scoop = md["rel_scoop"] & (md["ft_part_pctl"] >= 0.8) & md["ft_part_ok"]
        weak_scoop = md["rel_scoop"] & (md["ft_part_pctl"] <= 0.5) & md["ft_part_ok"]
        f_scoop = md["rel_scoop"] & (md["f_ntd"] > 0) & (md["it_ntd"] <= 0)
        it_scoop = md["rel_scoop"] & (md["it_ntd"] > 0) & (md["f_ntd"] <= 0)
        both_scoop = md["rel_scoop"] & (md["f_ntd"] > 0) & (md["it_ntd"] > 0)
        res[split] = {
            "strong_scoop_vs_aligned_sell": matched_spread(
                md, strong_scoop, md["aligned_sell"], target, min_n=50
            ),
            "strong_scoop_vs_weak_scoop": matched_spread(
                md, strong_scoop, weak_scoop, target, min_n=50
            ),
            "f_only_scoop_vs_aligned_sell": matched_spread(
                md, f_scoop, md["aligned_sell"], target, min_n=50
            ),
            "it_only_scoop_vs_aligned_sell": matched_spread(
                md, it_scoop, md["aligned_sell"], target, min_n=50
            ),
            "both_scoop_vs_aligned_sell": matched_spread(
                md, both_scoop, md["aligned_sell"], target, min_n=50
            ),
            "mkt_up_strong_aligned_buy_vs_dump": matched_spread(
                mu,
                mu["aligned_buy"] & (mu["ft_part_pctl"] >= 0.8) & mu["ft_part_ok"],
                mu["rel_dump"],
                target,
                min_n=50,
            ),
        }
    return res


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    if not CACHE.exists():
        raise SystemExit(f"missing market FT cache: {CACHE}")
    raw = load_universe()
    d = add_features(raw)
    is_m = (d["trade_date"] >= IS_START) & (d["trade_date"] < OOS)
    oos_m = d["trade_date"] >= OOS

    payload = {
        "protocol": {
            "market_ft": "TaiwanStockTotalInstitutionalInvestors Foreign+IT NTD",
            "self_metric": "ft3_rel_20 = sum(FT,3)/sum(|FT|,20)",
            "market_metric": "same definition on official market net_FT",
            "relative": "ft_part = stock_FT_NTD / market_gross_FT; r20_gap = r20 - m_r20",
            "outcome": f"β-excess vs IX0001, primary h={PRIMARY_H}",
            "is": [IS_START, OOS],
            "oos": [OOS, str(d["trade_date"].max().date())],
            "universe": int(d["stock_id"].nunique()),
            "rows": int(len(d)),
            "academic_priors": [
                "Taiwan threshold VAR: foreign leads TSE mainly in hot/NASDAQ-up regimes",
                "Chordia OIB: aggregate imbalance is contrarian after market moves",
                "Campbell/Ramadorai: daily institutional flow persists; sign of predictability contested",
                "In-repo skill: never stock_net/market_net; use gross participation",
            ],
        },
        "phase_market_diagnostics": phase_market_diagnostics(d),
        "phase_self_vs_market": {
            "IS": phase_self_vs_market(d[is_m], "IS"),
            "OOS": phase_self_vs_market(d[oos_m], "OOS"),
        },
        "phase_matched_tape": {
            "IS": phase_matched_tape(d[is_m], "IS"),
            "OOS": phase_matched_tape(d[oos_m], "OOS"),
        },
        "phase_reverse_and_regime": phase_reverse_and_regime(d),
        "phase_intensity_and_agents": phase_intensity_and_agents(d),
        "phase_2492": phase_2492(d),
        "yearly_robustness": yearly_robustness(d),
    }
    (OUT / "results.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"[market_relative_chip] wrote {OUT / 'results.json'}")
    # Quick console verdict
    for split in ("IS", "OOS"):
        m = payload["phase_matched_tape"][split]["mkt_down"]["rel_scoop_vs_aligned_sell"]
        print(f"  {split} mkt_down scoop−aligned_sell spread={m.get('spread')} n={m.get('n_a')}/{m.get('n_b')}")
        r = payload["phase_reverse_and_regime"][split]
        for k in ("self_r20", "mktrel_gap", "mktrel_part"):
            print(f"  {split} {k}: spread={r[k].get('spread')} → {r[k].get('reading')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
