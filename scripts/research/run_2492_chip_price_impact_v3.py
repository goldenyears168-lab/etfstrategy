#!/usr/bin/env python3
"""2492 chip×price v3: flow innovation, price impact, and incremental OOS value.

Research only. Signal-day features use data available by close T. Forward
labels are evaluation-only. Fixed annual walk-forward avoids threshold mining.
"""
from __future__ import annotations

import os

import json
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
DB = (Path(os.environ.get("GOLDENSTOCKS_DATA_DIR") or ROOT) / "data" / "stocks.db")
OUT = ROOT / "reports/research/chip-overlays/2492_knowledge/price_impact_v3"
SID = "2492"
AR_WIN = 252
IMPACT_WIN = 252
RIDGE = 10.0
TEST_START_YEAR = 2022
HORIZONS = (1, 5)


def _rolling_ar_innovation(s: pd.Series, lags: int = 5) -> pd.Series:
    """One-step innovation from a past-only rolling AR model."""
    a = s.to_numpy(dtype=float)
    out = np.full(len(a), np.nan)
    for i in range(AR_WIN + lags, len(a)):
        rows, y = [], []
        for t in range(i - AR_WIN, i):
            x = a[t - lags : t]
            if np.isfinite(a[t]) and np.isfinite(x).all():
                rows.append(np.r_[1.0, x[::-1]])
                y.append(a[t])
        now = a[i - lags : i]
        if len(y) >= 160 and np.isfinite(a[i]) and np.isfinite(now).all():
            coef = np.linalg.lstsq(np.asarray(rows), np.asarray(y), rcond=None)[0]
            out[i] = a[i] - float(np.dot(np.r_[1.0, now[::-1]], coef))
    return pd.Series(out, index=s.index)


def _rolling_impact_residual(
    ret: pd.Series, foreign_innov: pd.Series, trust_innov: pd.Series
) -> tuple[pd.Series, pd.Series]:
    """Past-only return~flow price impact and today's unabsorbed response."""
    y = ret.to_numpy(dtype=float)
    x1 = foreign_innov.to_numpy(dtype=float)
    x2 = trust_innov.to_numpy(dtype=float)
    residual = np.full(len(y), np.nan)
    fitted = np.full(len(y), np.nan)
    for i in range(IMPACT_WIN, len(y)):
        sl = slice(i - IMPACT_WIN, i)
        yy, xx1, xx2 = y[sl], x1[sl], x2[sl]
        ok = np.isfinite(yy) & np.isfinite(xx1) & np.isfinite(xx2)
        if ok.sum() < 160 or not all(np.isfinite(v) for v in (y[i], x1[i], x2[i])):
            continue
        x = np.column_stack([np.ones(ok.sum()), xx1[ok], xx2[ok]])
        coef = np.linalg.lstsq(x, yy[ok], rcond=None)[0]
        fitted[i] = float(np.dot([1.0, x1[i], x2[i]], coef))
        residual[i] = y[i] - fitted[i]
    return pd.Series(fitted, index=ret.index), pd.Series(residual, index=ret.index)


def load_panel() -> pd.DataFrame:
    uri = f"file:{DB.resolve()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        bars = pd.read_sql_query(
            """SELECT trade_date AS date, open, high, low, close, volume
               FROM stock_daily_bars
               WHERE source='finmind' AND stock_id=? ORDER BY trade_date""",
            conn,
            params=[SID],
        )
        inst = pd.read_sql_query(
            """SELECT trade_date AS date, foreign_net, investment_trust_net
               FROM stock_institutional_daily
               WHERE source='finmind' AND stock_id=? ORDER BY trade_date""",
            conn,
            params=[SID],
        )
        margin = pd.read_sql_query(
            """SELECT trade_date AS date, margin_balance
               FROM stock_margin_daily WHERE stock_id=? ORDER BY trade_date""",
            conn,
            params=[SID],
        )
        index = pd.read_sql_query(
            """SELECT date, close AS index_close FROM daily_bars
               WHERE code='IX0001'
               ORDER BY date,
                 CASE source WHEN 'yahoo' THEN 1 WHEN 'finmind' THEN 2 ELSE 9 END""",
            conn,
        ).drop_duplicates("date")
    df = bars.merge(inst, on="date", how="left")
    df = df.merge(margin, on="date", how="left").merge(index, on="date", how="left")
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date").reset_index(drop=True)


def add_features(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    d["ret"] = d["close"].pct_change(fill_method=None)
    d["gap_ret"] = d["open"] / d["close"].shift(1) - 1.0
    d["intraday_ret"] = d["close"] / d["open"] - 1.0
    d["next_gap"] = d["open"].shift(-1) / d["close"] - 1.0
    d["next_intraday"] = d["close"].shift(-1) / d["open"].shift(-1) - 1.0
    d["mkt_ret"] = d["index_close"].pct_change(fill_method=None)
    beta = d["ret"].rolling(252, min_periods=160).cov(d["mkt_ret"])
    beta /= d["mkt_ret"].rolling(252, min_periods=160).var()
    d["exret"] = d["ret"] - beta.shift(1).clip(0.2, 3.0) * d["mkt_ret"]

    # Monetary net flow / prior 20d average traded value: stock-size comparable.
    d["turnover_ntd"] = d["close"] * d["volume"]
    adv20 = d["turnover_ntd"].shift(1).rolling(20, min_periods=15).mean()
    d["f_part"] = d["foreign_net"] * d["close"] / adv20
    d["it_part"] = d["investment_trust_net"] * d["close"] / adv20
    d["ft_part"] = d["f_part"] + d["it_part"]

    # Institutions are heterogeneous; do not cancel foreign and trust blindly.
    d["f_innov"] = _rolling_ar_innovation(d["f_part"])
    d["it_innov"] = _rolling_ar_innovation(d["it_part"])
    d["impact_fit"], d["impact_resid"] = _rolling_impact_residual(
        d["exret"], d["f_innov"], d["it_innov"]
    )

    for n in (3, 5, 20):
        d[f"ft_sum{n}"] = d["ft_part"].rolling(n, min_periods=n).sum()
    d["f_streak3"] = (d["f_part"] > 0).rolling(3, min_periods=3).sum()
    d["it_streak3"] = (d["it_part"] > 0).rolling(3, min_periods=3).sum()
    d["ret5"] = d["close"].pct_change(5)
    d["ret20"] = d["close"].pct_change(20)
    d["ma20_gap"] = d["close"] / d["close"].rolling(20).mean() - 1.0
    d["vol_ratio"] = d["volume"] / d["volume"].shift(1).rolling(20).mean()
    d["margin_chg5"] = d["margin_balance"].pct_change(5, fill_method=None)
    # Threshold-VAR literature: institutions lead in hot markets, follow in cold.
    prior_turnover_median = (
        d["turnover_ntd"].shift(1).rolling(252, min_periods=160).median()
    )
    d["hot_market"] = d["turnover_ntd"] > prior_turnover_median

    for h in HORIZONS:
        stock_fwd = d["close"].shift(-h) / d["close"] - 1.0
        market_fwd = d["index_close"].shift(-h) / d["index_close"] - 1.0
        d[f"fwd{h}_ex"] = stock_fwd - beta.shift(1).clip(0.2, 3.0) * market_fwd
    return d


PRICE_FEATURES = ["ret", "ret5", "ret20", "ma20_gap", "vol_ratio"]
CHIP_FEATURES = [
    "f_part",
    "it_part",
    "ft_sum3",
    "ft_sum5",
    "ft_sum20",
    "f_innov",
    "it_innov",
    "impact_fit",
    "impact_resid",
    "f_streak3",
    "it_streak3",
    "margin_chg5",
]


def _ridge_predict(
    train: pd.DataFrame, test: pd.DataFrame, features: list[str], target: str
) -> np.ndarray:
    xtr = train[features].to_numpy(float)
    ytr = train[target].to_numpy(float)
    xte = test[features].to_numpy(float)
    ok = np.isfinite(ytr) & np.isfinite(xtr).all(axis=1)
    xtr, ytr = xtr[ok], ytr[ok]
    mu, sd = xtr.mean(axis=0), xtr.std(axis=0)
    sd[sd == 0] = 1.0
    ztr, zte = (xtr - mu) / sd, (xte - mu) / sd
    x = np.column_stack([np.ones(len(ztr)), ztr])
    penalty = np.eye(x.shape[1]) * RIDGE
    penalty[0, 0] = 0.0
    coef = np.linalg.solve(x.T @ x + penalty, x.T @ ytr)
    return np.column_stack([np.ones(len(zte)), zte]) @ coef


def _score(y: pd.Series, pred: pd.Series) -> dict:
    ok = y.notna() & pred.notna()
    y, pred = y[ok], pred[ok]
    rank_ic = float(y.corr(pred, method="spearman"))
    q20, q80 = pred.quantile([0.2, 0.8])
    spread = float(y[pred >= q80].mean() - y[pred <= q20].mean())
    direction = float(((pred > 0) == (y > 0)).mean())
    return {
        "n": int(len(y)),
        "rank_ic": round(rank_ic, 4),
        "top_bottom_spread_pct": round(spread * 100, 3),
        "direction_hit_pct": round(direction * 100, 1),
    }


def walk_forward(d: pd.DataFrame, h: int) -> tuple[pd.DataFrame, dict]:
    target = f"fwd{h}_ex"
    required = list(dict.fromkeys(PRICE_FEATURES + CHIP_FEATURES + [target]))
    usable = d.dropna(subset=required).copy()
    pieces = []
    yearly = {}
    for year in range(TEST_START_YEAR, int(d["date"].dt.year.max()) + 1):
        test = usable[usable["date"].dt.year == year].copy()
        # Purge overlapping labels immediately before each annual test.
        cut = pd.Timestamp(f"{year}-01-01")
        train = usable[usable["date"] < cut].iloc[:-h]
        if len(train) < 500 or len(test) < 20:
            continue
        test["pred_price"] = _ridge_predict(train, test, PRICE_FEATURES, target)
        test["pred_full"] = _ridge_predict(
            train, test, PRICE_FEATURES + CHIP_FEATURES, target
        )
        yearly[str(year)] = {
            "price": _score(test[target], test["pred_price"]),
            "full": _score(test[target], test["pred_full"]),
        }
        pieces.append(test[["date", target, "pred_price", "pred_full"]])
    pred = pd.concat(pieces, ignore_index=True)
    return pred, {
        "pooled_price": _score(pred[target], pred["pred_price"]),
        "pooled_full": _score(pred[target], pred["pred_full"]),
        "yearly": yearly,
    }


def theory_event_study(d: pd.DataFrame, h: int) -> dict:
    """Annual PIT thresholds for literature-derived, not data-mined, events."""
    target = f"fwd{h}_ex"
    rows: dict[str, list[pd.Series]] = {}
    for year in range(TEST_START_YEAR, int(d["date"].dt.year.max()) + 1):
        cut = pd.Timestamp(f"{year}-01-01")
        train = d[(d["date"] < cut) & d[target].notna()].iloc[:-h]
        test = d[(d["date"].dt.year == year) & d[target].notna()].copy()
        if len(train) < 500 or len(test) < 20:
            continue
        train_ft_innov = train["f_innov"] + train["it_innov"]
        test_ft_innov = test["f_innov"] + test["it_innov"]
        q = {
            "f_hi": train["f_innov"].quantile(0.8),
            "it_hi": train["it_innov"].quantile(0.8),
            "ft_hi": train_ft_innov.quantile(0.8),
            "ft_lo": train_ft_innov.quantile(0.2),
        }
        masks = {
            "foreign_surprise_buy": test["f_innov"] >= q["f_hi"],
            "trust_surprise_buy": test["it_innov"] >= q["it_hi"],
            "ft_surprise_buy": test_ft_innov >= q["ft_hi"],
            "ft_surprise_sell": test_ft_innov <= q["ft_lo"],
            # Hasbrouck/Kyle: unpredictable buy with less price response than
            # the past impact model expects = latent, not-yet-reflected demand.
            "underreacted_surprise_buy": (test_ft_innov >= q["ft_hi"])
            & (test["impact_resid"] < 0),
            "absorbed_surprise_buy": (test_ft_innov >= q["ft_hi"])
            & (test["impact_resid"] >= 0),
            # Threshold-VAR condition, fixed from prior 252d turnover median.
            "hot_ft_surprise_buy": (test_ft_innov >= q["ft_hi"])
            & test["hot_market"],
            "cold_ft_surprise_buy": (test_ft_innov >= q["ft_hi"])
            & (~test["hot_market"]),
            # User's original economic claim, now using unexpected flow.
            "price_down_surprise_buy": (test["exret"] < 0)
            & (test_ft_innov >= q["ft_hi"]),
        }
        for name, mask in masks.items():
            rows.setdefault(name, []).append(test.loc[mask.fillna(False), target])
    baseline = d[(d["date"].dt.year >= TEST_START_YEAR) & d[target].notna()][target]
    result = {}
    for name, pieces in rows.items():
        x = pd.concat(pieces).dropna()
        result[name] = {
            "n": int(len(x)),
            "mean_pct": round(float(x.mean() * 100), 3),
            "median_pct": round(float(x.median() * 100), 3),
            "hit_pct": round(float((x > 0).mean() * 100), 1),
            "median_lift_vs_all_pct": round(
                float((x.median() - baseline.median()) * 100), 3
            ),
        }
    return result


def hot_tail_study(d: pd.DataFrame, h: int) -> dict:
    """Prior-threshold high-vs-low FT flow in hot-turnover sessions."""
    target = f"fwd{h}_ex"
    high_all, low_all, yearly = [], [], {}
    for year in range(2018, int(d["date"].dt.year.max()) + 1):
        cut = pd.Timestamp(f"{year}-01-01")
        train = d[d["date"] < cut]
        test = d[(d["date"].dt.year == year) & d["hot_market"].fillna(False)]
        low_cut, high_cut = train["ft_part"].quantile([0.2, 0.8])
        high = test.loc[test["ft_part"] >= high_cut, target].dropna()
        low = test.loc[test["ft_part"] <= low_cut, target].dropna()
        high_all.append(high)
        low_all.append(low)
        if len(high) >= 5 and len(low) >= 5:
            yearly[str(year)] = {
                "n_high": int(len(high)),
                "n_low": int(len(low)),
                "median_spread_pct": round(
                    float((high.median() - low.median()) * 100), 3
                ),
            }
    high, low = pd.concat(high_all), pd.concat(low_all)
    return {
        "n_high": int(len(high)),
        "n_low": int(len(low)),
        "high_median_pct": round(float(high.median() * 100), 3),
        "low_median_pct": round(float(low.median() * 100), 3),
        "median_spread_pct": round(float((high.median() - low.median()) * 100), 3),
        "high_hit_pct": round(float((high > 0).mean() * 100), 1),
        "low_hit_pct": round(float((low > 0).mean() * 100), 1),
        "positive_spread_years": f"{sum(v['median_spread_pct'] > 0 for v in yearly.values())}/{len(yearly)}",
        "yearly": yearly,
    }


def branch_episode_audit() -> dict:
    """Correct pseudo-replication in existing H7 branch studies."""
    path = (
        ROOT
        / "reports/research/chip-overlays/2492_knowledge/surge_branch"
        / "l1h7_lift_bakeoff_legs.csv"
    )
    if not path.exists():
        return {}
    legs = pd.read_csv(path, parse_dates=["signal_date"])
    rng = np.random.default_rng(2492)
    result = {}
    arms = (
        "A7_watch_k2_not_dump_H7",
        "A2_watch_k2_H7",
        "A6_watch_k2_nogap_H7",
    )
    for arm in arms:
        result[arm] = {}
        for split in ("IS", "OOS"):
            x = legs[(legs["arm"] == arm) & (legs["split"] == split)].sort_values(
                "signal_date"
            )
            episode, last, episode_id = [], None, -1
            for date in x["signal_date"]:
                if last is None or (date - last).days > 10:
                    episode_id += 1
                episode.append(episode_id)
                last = date
            if not episode:
                continue
            x = x.assign(episode=episode)
            values = x.groupby("episode")["r_adj"].mean().to_numpy()
            boot = np.array(
                [
                    np.median(rng.choice(values, len(values), replace=True))
                    for _ in range(10_000)
                ]
            )
            result[arm][split] = {
                "raw_n": int(len(x)),
                "independent_episodes": int(len(values)),
                "episode_median_pct": round(float(np.median(values) * 100), 3),
                "bootstrap_ci95_pct": [
                    round(float(v * 100), 3)
                    for v in np.quantile(boot, [0.025, 0.975])
                ],
                "bootstrap_positive_probability": round(float((boot > 0).mean()), 3),
            }
    return result


def diagnostics(d: pd.DataFrame) -> dict:
    cols = ["f_part", "it_part", "ft_part", "f_innov", "it_innov"]
    same = {}
    leads = {}
    for c in cols:
        same[c] = {
            "pearson_exret": round(float(d[c].corr(d["exret"])), 4),
            "spearman_exret": round(float(d[c].corr(d["exret"], method="spearman")), 4),
        }
        leads[c] = {
            f"fwd{h}_spearman": round(
                float(d[c].corr(d[f"fwd{h}_ex"], method="spearman")), 4
            )
            for h in HORIZONS
        }
    decomposition = {}
    for c in cols:
        decomposition[c] = {
            "same_intraday": round(
                float(d[c].corr(d["intraday_ret"], method="spearman")), 4
            ),
            "same_gap": round(float(d[c].corr(d["gap_ret"], method="spearman")), 4),
            "next_gap": round(float(d[c].corr(d["next_gap"], method="spearman")), 4),
            "next_intraday": round(
                float(d[c].corr(d["next_intraday"], method="spearman")), 4
            ),
        }
    lead_lag = {}
    for c in ("f_part", "it_part", "ft_part"):
        lead_lag[c] = {}
        for lag in range(-5, 6):
            # lag<0: return occurs before flow; lag>0: flow precedes return.
            lead_lag[c][str(lag)] = round(
                float(d[c].corr(d["exret"].shift(-lag), method="spearman")), 4
            )
    regime = {}
    for name, mask in {
        "hot": d["hot_market"].fillna(False),
        "cold": (~d["hot_market"]).fillna(False),
    }.items():
        regime[name] = {
            "n": int(mask.sum()),
            "same_day_ft": round(
                float(d.loc[mask, "ft_part"].corr(d.loc[mask, "exret"], method="spearman")),
                4,
            ),
            "fwd1_ft": round(
                float(
                    d.loc[mask, "ft_part"].corr(
                        d.loc[mask, "fwd1_ex"], method="spearman"
                    )
                ),
                4,
            ),
            "fwd5_ft": round(
                float(
                    d.loc[mask, "ft_part"].corr(
                        d.loc[mask, "fwd5_ex"], method="spearman"
                    )
                ),
                4,
            ),
        }
    return {
        "same_day": same,
        "forward": leads,
        "session_decomposition": decomposition,
        "lead_lag": lead_lag,
        "turnover_regime": regime,
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    d = add_features(load_panel())
    result = {
        "protocol": {
            "sid": SID,
            "data_start": str(d["date"].min().date()),
            "data_end": str(d["date"].max().date()),
            "rows": int(len(d)),
            "flow_unit": "net NTD / prior-20d average traded NTD",
            "flow_model": "past-only rolling AR(5), 252d",
            "price_impact": "past-only rolling exret ~ foreign/trust innovations, 252d",
            "evaluation": "fixed annual walk-forward, 2022+, ridge=10, purged by horizon",
            "academic_logic": [
                "Kyle (1985): informed traders split parent orders",
                "Hasbrouck (1988/1991): unpredictable flow drives price impact",
                "Chordia et al.: separate contemporaneous impact, persistence, reversal",
                "TWSE evidence: marketable order imbalance and trader type matter",
                "Taiwan threshold VAR: foreign flow leads mainly in hot markets",
            ],
        },
        "diagnostics": diagnostics(d),
        "walk_forward": {},
        "theory_events": {},
        "hot_tail_spread": {},
        "branch_episode_audit": branch_episode_audit(),
    }
    exports = []
    for h in HORIZONS:
        pred, score = walk_forward(d, h)
        result["walk_forward"][f"h{h}"] = score
        result["theory_events"][f"h{h}"] = theory_event_study(d, h)
        result["hot_tail_spread"][f"h{h}"] = hot_tail_study(d, h)
        pred["horizon"] = h
        exports.append(pred)
    pd.concat(exports, ignore_index=True).to_csv(OUT / "predictions.csv", index=False)
    (OUT / "results.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
