#!/usr/bin/env python3
"""ft3_rel_20 trend × price: does the SLOPE add information over the LEVEL?

`ft3_rel_20 = sum(FT, 3d) / sum(|FT|, 20d)` is the Live TA column. Its daily
change is mechanically overlapping (a 3-day sum shifted by one day), so this
study separates:

  Phase A  diagnostics — autocorrelation, contemporaneous vs forward, and how
           much of the daily delta is mechanical rather than informative.
  Phase B  double sort — within each PIT level bucket, does slope still sort
           forward excess returns? This is the decisive incremental test.
  Phase C  quadrants — chip trend × price trend, each against a matched
           price-only control so momentum cannot masquerade as a chip finding.
  Phase D  robustness — yearly, hot/cold turnover regime, cross-stock breadth,
           and a moving-block bootstrap that respects overlapping labels.

Protocol follows the v3 lessons: features use only data ≤ T, outcomes are
excess vs a cross-sectional market proxy, and every chip claim is compared to
a price-only control.

Out: reports/research/chip-overlays/ft3_rel_trend/
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
DB = ROOT / "data" / "stocks.db"
OUT = ROOT / "reports" / "research" / "chip-overlays" / "ft3_rel_trend"
SOURCE = "finmind"
MIN_BARS = 1500
IS_START = "2019-01-01"
OOS = "2026-01-01"
HORIZONS = (1, 5, 10, 20)
PRIMARY_H = 10
FT3_DAYS = 3
ABS_DAYS = 20
ABS_MIN_DAYS = 10
SLOPE_WINDOWS = (3, 5, 10, 20, 60)
SUSTAIN_DAYS = (1, 3, 5, 10, 20)
PCTL_WINDOW = 252
PCTL_MIN = 120
DEEP_DIVE_SID = "2492"
BLOCK = 20
N_BOOT = 5000


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
            f"COALESCE(foreign_net,0)+COALESCE(investment_trust_net,0) AS ft "
            f"FROM stock_institutional_daily WHERE source=? AND stock_id IN ({ph})",
            conn,
            params=[SOURCE, *sids],
        )
    for d in (bars, inst):
        d["trade_date"] = pd.to_datetime(d["trade_date"])
    # 410 rows carry close=0 placeholders; left in, they produce infinite returns.
    bars = bars[bars["close"] > 0]
    df = bars.merge(inst, on=["stock_id", "trade_date"], how="left")
    return df.sort_values(["stock_id", "trade_date"]).reset_index(drop=True)


def _slope_weights(window: int) -> np.ndarray:
    """OLS slope of y on 0..w-1 expressed as a fixed weight vector."""
    x = np.arange(window, dtype=float)
    xc = x - x.mean()
    return xc / float(np.dot(xc, xc))


def _run_length(s: pd.Series) -> pd.Series:
    """Consecutive-True run length ending at each row."""
    b = s.astype(bool)
    return b.groupby((~b).cumsum()).cumsum()


def _rolling_slope(s: pd.Series, window: int) -> pd.Series:
    w = _slope_weights(window)
    return s.rolling(window, min_periods=window).apply(
        lambda y: float(np.dot(w, y)), raw=True
    )


def add_features(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    g = out.groupby("stock_id", sort=False)
    out["ret1"] = g["close"].pct_change(fill_method=None) * 100.0

    mkt = out.groupby("trade_date")["ret1"].median().rename("mkt_ret1")
    out = out.merge(mkt, on="trade_date", how="left")

    # Live TA definition, reproduced exactly: 3-day FT over 20-day abs FT.
    g = out.groupby("stock_id", sort=False)
    ft3 = g["ft"].transform(lambda s: s.rolling(FT3_DAYS, min_periods=FT3_DAYS).sum())
    abs20 = g["ft"].transform(
        lambda s: s.abs().rolling(ABS_DAYS, min_periods=ABS_MIN_DAYS).sum()
    )
    out["ft3"] = ft3
    out["r20"] = ft3 / abs20.replace(0, np.nan)

    g = out.groupby("stock_id", sort=False)
    out["r20_d1"] = g["r20"].diff(1)
    for w in SLOPE_WINDOWS:
        out[f"r20_slope{w}"] = g["r20"].transform(lambda s, w=w: _rolling_slope(s, w))
    out["r20_accel"] = out["r20_slope5"] - g["r20_slope5"].shift(5)
    out["r20_pos_streak"] = g["r20"].transform(
        lambda s: (s > 0).rolling(5, min_periods=5).sum()
    )
    out["chip_line"] = g["r20"].transform(lambda s: s.fillna(0).cumsum())

    # PIT trailing percentile of the level; never full-sample.
    out["r20_pctl"] = g["r20"].transform(
        lambda s: s.rolling(PCTL_WINDOW, min_periods=PCTL_MIN).rank(pct=True)
    )
    out["slope5_pctl"] = g["r20_slope5"].transform(
        lambda s: s.rolling(PCTL_WINDOW, min_periods=PCTL_MIN).rank(pct=True)
    )

    # Price trend side, including the MA3 the user proposed.
    out["ma3"] = g["close"].transform(lambda s: s.rolling(3, min_periods=3).mean())
    out["ma20"] = g["close"].transform(lambda s: s.rolling(20, min_periods=20).mean())
    g = out.groupby("stock_id", sort=False)
    out["ma3_chg"] = g["ma3"].pct_change(fill_method=None) * 100.0
    out["px_slope5"] = g["close"].transform(
        lambda s: _rolling_slope(np.log(s), 5) * 100.0
    )
    out["px_ma20_gap"] = (out["close"] / out["ma20"] - 1.0) * 100.0
    out["ret5"] = g["close"].pct_change(5, fill_method=None) * 100.0
    for w in (20, 60):
        out[f"px_slope{w}"] = g["close"].transform(
            lambda s, w=w: _rolling_slope(np.log(s), w) * 100.0
        )

    # SMFI doctrine: a one-day divergence is noise; only SUSTAINED multi-week
    # chip-vs-price divergence is claimed to matter. Count consecutive days.
    for w in (5, 20, 60):
        chip_up = out[f"r20_slope{w}"] > 0
        px_up = out[f"px_slope{w if w > 5 else 5}"] > 0
        for label, state in (
            ("accum", chip_up & (~px_up)),
            ("distrib", (~chip_up) & px_up),
            ("confirm_up", chip_up & px_up),
            ("confirm_dn", (~chip_up) & (~px_up)),
        ):
            col = f"{label}{w}"
            out[col] = state.fillna(False)
            # consecutive-day run length of the state, PIT by construction
            out[f"{col}_run"] = out.groupby("stock_id", sort=False)[col].transform(
                _run_length
            )

    # Turnover regime (v3 finding: institutional lead concentrates in hot tape).
    out["turnover"] = out["close"] * out["volume"]
    out["hot"] = out["turnover"] > g["turnover"].transform(
        lambda s: s.shift(1).rolling(PCTL_WINDOW, min_periods=PCTL_MIN).median()
    )

    # Excess forward returns vs the cross-sectional market proxy.
    mkt_cum = (1.0 + mkt / 100.0).cumprod().rename("mkt_cum")
    out = out.merge(mkt_cum, on="trade_date", how="left")
    g = out.groupby("stock_id", sort=False)
    for h in HORIZONS:
        fwd_stock = (g["close"].shift(-h) / out["close"] - 1.0) * 100.0
        fwd_mkt = (g["mkt_cum"].shift(-h) / out["mkt_cum"] - 1.0) * 100.0
        out[f"x{h}"] = fwd_stock - fwd_mkt
    return out


def phase_a_diagnostics(d: pd.DataFrame) -> dict:
    """How much of r20 and its delta is mechanical vs informative?"""
    s = d.dropna(subset=["r20"])
    autocorr = {
        f"lag{k}": round(float(s.groupby("stock_id")["r20"].apply(lambda x, k=k: x.autocorr(k)).median()), 4)
        for k in (1, 2, 3, 5, 10)
    }
    d1_auto = round(
        float(
            d.dropna(subset=["r20_d1"])
            .groupby("stock_id")["r20_d1"]
            .apply(lambda x: x.autocorr(1))
            .median()
        ),
        4,
    )
    # The daily delta of a 3-day sum is (ft_T - ft_{T-3}) / abs20 plus a
    # denominator effect: quantify how much is the numerator swap alone.
    g = d.groupby("stock_id", sort=False)
    mech = (d["ft"] - g["ft"].shift(3)) / (d["ft3"] / d["r20"]).replace(0, np.nan)
    share = pd.concat([d["r20_d1"], mech], axis=1).dropna()
    mech_r2 = round(float(share.corr().iloc[0, 1] ** 2), 4) if len(share) > 100 else None

    def corr(col: str, target: str) -> float | None:
        x = d[[col, target]].dropna()
        if len(x) < 500:
            return None
        return round(float(x[col].corr(x[target], method="spearman")), 4)

    contemporaneous = {
        c: corr(c, "ret1") for c in ("r20", "r20_d1", "r20_slope5", "r20_slope10")
    }
    forward = {
        c: {f"x{h}": corr(c, f"x{h}") for h in HORIZONS}
        for c in ("r20", "r20_d1", "r20_slope3", "r20_slope5", "r20_slope10", "r20_accel")
    }
    return {
        "r20_autocorr_median_across_stocks": autocorr,
        "r20_delta_autocorr_lag1": d1_auto,
        "delta_explained_by_numerator_swap_r2": mech_r2,
        "same_day_spearman_vs_ret1": contemporaneous,
        "forward_spearman": forward,
    }


def _summary(x: pd.Series) -> dict:
    x = x.dropna()
    if len(x) == 0:
        return {"n": 0}
    return {
        "n": int(len(x)),
        "med": round(float(x.median()), 3),
        "mean": round(float(x.mean()), 3),
        "win": round(float((x > 0).mean() * 100), 1),
    }


def phase_b_double_sort(d: pd.DataFrame, split: str) -> dict:
    """Within PIT level buckets, does slope still sort forward returns?

    If the answer is no, the T-1 delta column adds nothing over the level.
    """
    target = f"x{PRIMARY_H}"
    s = d.dropna(subset=["r20_pctl", "slope5_pctl", target])
    out: dict = {"split": split, "n_total": int(len(s))}
    level_bins = [(0.0, 1 / 3, "level_low"), (1 / 3, 2 / 3, "level_mid"), (2 / 3, 1.01, "level_high")]
    grid = {}
    for lo, hi, lname in level_bins:
        m = (s["r20_pctl"] >= lo) & (s["r20_pctl"] < hi)
        chunk = s[m]
        hi_slope = chunk[chunk["slope5_pctl"] >= 2 / 3][target]
        lo_slope = chunk[chunk["slope5_pctl"] < 1 / 3][target]
        grid[lname] = {
            "all": _summary(chunk[target]),
            "slope_high": _summary(hi_slope),
            "slope_low": _summary(lo_slope),
            "slope_spread": (
                round(float(hi_slope.median() - lo_slope.median()), 3)
                if len(hi_slope) and len(lo_slope)
                else None
            ),
        }
    # Mirror test: within slope buckets, does level still sort?
    mirror = {}
    for lo, hi, sname in [(0.0, 1 / 3, "slope_low"), (2 / 3, 1.01, "slope_high")]:
        m = (s["slope5_pctl"] >= lo) & (s["slope5_pctl"] < hi)
        chunk = s[m]
        hi_lvl = chunk[chunk["r20_pctl"] >= 2 / 3][target]
        lo_lvl = chunk[chunk["r20_pctl"] < 1 / 3][target]
        mirror[sname] = {
            "level_spread": (
                round(float(hi_lvl.median() - lo_lvl.median()), 3)
                if len(hi_lvl) and len(lo_lvl)
                else None
            ),
            "n_high": int(len(hi_lvl)),
            "n_low": int(len(lo_lvl)),
        }
    out["level_buckets"] = grid
    out["slope_buckets_level_spread"] = mirror
    return out


def phase_c_quadrants(d: pd.DataFrame, split: str) -> dict:
    """Chip trend × price trend, each against a matched price-only control."""
    target = f"x{PRIMARY_H}"
    s = d.dropna(subset=["r20_slope5", "ma3_chg", target])
    chip_up = s["r20_slope5"] > 0
    px_up = s["ma3_chg"] > 0
    quad = {
        "chip_up_price_up": chip_up & px_up,
        "chip_up_price_down": chip_up & (~px_up),
        "chip_down_price_up": (~chip_up) & px_up,
        "chip_down_price_down": (~chip_up) & (~px_up),
    }
    res = {"split": split, "baseline": _summary(s[target])}
    for name, m in quad.items():
        res[name] = _summary(s.loc[m, target])
    # Price-only controls: the quadrants must beat these to mean anything.
    res["CTRL_price_up_only"] = _summary(s.loc[px_up, target])
    res["CTRL_price_down_only"] = _summary(s.loc[~px_up, target])
    res["lift_vs_price_control"] = {
        "chip_up_price_up": round(
            float(s.loc[quad["chip_up_price_up"], target].median() - s.loc[px_up, target].median()), 3
        ),
        "chip_up_price_down": round(
            float(
                s.loc[quad["chip_up_price_down"], target].median()
                - s.loc[~px_up, target].median()
            ),
            3,
        ),
        "chip_down_price_up": round(
            float(
                s.loc[quad["chip_down_price_up"], target].median() - s.loc[px_up, target].median()
            ),
            3,
        ),
        "chip_down_price_down": round(
            float(
                s.loc[quad["chip_down_price_down"], target].median()
                - s.loc[~px_up, target].median()
            ),
            3,
        ),
    }
    return res


IC_FEATURES = (
    "r20",
    "r20_d1",
    "r20_slope5",
    "r20_slope20",
    "r20_slope60",
    "chip_line",
)


def _daily_ic(d: pd.DataFrame, feat: str, target: str, min_names: int = 30) -> pd.Series:
    """Per-day cross-sectional Spearman IC — the standard factor diagnostic."""
    s = d[["trade_date", feat, target]].dropna()
    if s.empty:
        return pd.Series(dtype=float)
    counts = s.groupby("trade_date")[feat].transform("size")
    s = s[counts >= min_names]
    if s.empty:
        return pd.Series(dtype=float)
    return s.groupby("trade_date").apply(
        lambda x: x[feat].corr(x[target], method="spearman"), include_groups=False
    )


def _ic_stats(ic: pd.Series) -> dict:
    ic = ic.dropna()
    if len(ic) < 30:
        return {"days": int(len(ic))}
    mean = float(ic.mean())
    std = float(ic.std(ddof=1))
    return {
        "days": int(len(ic)),
        "mean_ic": round(mean, 4),
        "ic_ir": round(mean / std, 3) if std > 0 else None,
        # IC series is near-iid across days; t on the daily IC series.
        "t_stat": round(mean / (std / np.sqrt(len(ic))), 2) if std > 0 else None,
        "pct_positive_days": round(float((ic > 0).mean() * 100), 1),
    }


def phase_d_cross_sectional_ic(d: pd.DataFrame) -> dict:
    """Rank IC per horizon, IS/OOS, and turnover regime + decay half-life."""
    is_m = (d["trade_date"] >= IS_START) & (d["trade_date"] < OOS)
    oos_m = d["trade_date"] >= OOS
    res: dict = {}
    for feat in IC_FEATURES:
        entry: dict = {}
        for h in HORIZONS:
            target = f"x{h}"
            entry[f"x{h}"] = {
                "IS": _ic_stats(_daily_ic(d[is_m], feat, target)),
                "OOS": _ic_stats(_daily_ic(d[oos_m], feat, target)),
            }
        # decay: fit IC(h) = IC0 * exp(-lambda*h) on IS mean ICs when positive
        ics = [
            (h, entry[f"x{h}"]["IS"].get("mean_ic"))
            for h in HORIZONS
            if entry[f"x{h}"]["IS"].get("mean_ic") is not None
        ]
        pos = [(h, v) for h, v in ics if v > 0]
        half_life = None
        if len(pos) >= 3:
            hs = np.array([h for h, _ in pos], dtype=float)
            vs = np.log(np.array([v for _, v in pos], dtype=float))
            lam = -np.polyfit(hs, vs, 1)[0]
            if lam > 0:
                half_life = round(float(np.log(2) / lam), 1)
        entry["is_ic_half_life_days"] = half_life
        res[feat] = entry
    # regime split on the primary horizon
    regime = {}
    for feat in ("r20", "r20_slope20"):
        regime[feat] = {
            "hot": _ic_stats(
                _daily_ic(d[d["hot"].fillna(False)], feat, f"x{PRIMARY_H}")
            ),
            "cold": _ic_stats(
                _daily_ic(d[~d["hot"].fillna(True)], feat, f"x{PRIMARY_H}")
            ),
        }
    res["_regime_primary_h"] = regime
    return res


def phase_e_sustained(d: pd.DataFrame) -> dict:
    """SMFI claim: only SUSTAINED chip-vs-price divergence carries signal."""
    target = f"x{PRIMARY_H}"
    is_m = (d["trade_date"] >= IS_START) & (d["trade_date"] < OOS)
    oos_m = d["trade_date"] >= OOS
    res: dict = {}
    for w in (5, 20, 60):
        for label in ("accum", "distrib"):
            col = f"{label}{w}_run"
            # matched price-only control: same price-trend side, any chip state
            px_col = f"px_slope{w if w > 5 else 5}"
            entry = {}
            for split, mask in (("IS", is_m), ("OOS", oos_m)):
                s = d[mask].dropna(subset=[col, px_col, target])
                if label == "accum":
                    ctrl = s.loc[s[px_col] <= 0, target]
                else:
                    ctrl = s.loc[s[px_col] > 0, target]
                by_run = {}
                for k in SUSTAIN_DAYS:
                    x = s.loc[s[col] >= k, target]
                    if len(x) < 100:
                        by_run[f">={k}d"] = {"n": int(len(x))}
                        continue
                    by_run[f">={k}d"] = {
                        **_summary(x),
                        "lift_vs_price_ctrl": round(
                            float(x.median() - ctrl.median()), 3
                        ),
                    }
                entry[split] = {"ctrl_med": round(float(ctrl.median()), 3), "runs": by_run}
            res[f"{label}_w{w}"] = entry
    return res


def phase_f_regime_verification(d: pd.DataFrame) -> dict:
    """The hot-tape effect is the only survivor — verify it properly.

    Splits IC by IS/OOS and by year, and reports the economic magnitude as a
    daily-rebalanced top-minus-bottom quintile spread rather than IC alone.
    """
    target = f"x{PRIMARY_H}"
    res: dict = {}
    for feat in ("r20", "r20_slope20"):
        hot = d[d["hot"].fillna(False)]
        cold = d[~d["hot"].fillna(True)]
        entry: dict = {}
        for name, sub in (("hot", hot), ("cold", cold)):
            is_m = (sub["trade_date"] >= IS_START) & (sub["trade_date"] < OOS)
            oos_m = sub["trade_date"] >= OOS
            entry[name] = {
                "IS": _ic_stats(_daily_ic(sub[is_m], feat, target)),
                "OOS": _ic_stats(_daily_ic(sub[oos_m], feat, target)),
            }
        # yearly IC in hot tape
        ic_hot = _daily_ic(hot, feat, target)
        if len(ic_hot):
            by_year = ic_hot.groupby(ic_hot.index.year).agg(["mean", "size"])
            entry["hot_by_year"] = {
                str(int(y)): {"mean_ic": round(float(r["mean"]), 4), "days": int(r["size"])}
                for y, r in by_year.iterrows()
                if int(y) >= int(IS_START[:4])
            }
        res[feat] = entry

    # Economic magnitude: daily quintile spread on r20 in hot tape.
    hot = d[d["hot"].fillna(False)].dropna(subset=["r20", target])
    counts = hot.groupby("trade_date")["r20"].transform("size")
    hot = hot[counts >= 30]
    q = hot.groupby("trade_date")["r20"].transform(
        lambda x: pd.qcut(x.rank(method="first"), 5, labels=False)
    )
    hot = hot.assign(q=q)
    daily = hot.groupby(["trade_date", "q"])[target].mean().unstack()
    spread = (daily[4] - daily[0]).dropna()
    is_s = spread[spread.index < OOS]
    oos_s = spread[spread.index >= OOS]
    res["_hot_quintile_spread_pp"] = {
        "IS": {
            "days": int(len(is_s)),
            "mean": round(float(is_s.mean()), 3),
            "t_stat": round(
                float(is_s.mean() / (is_s.std(ddof=1) / np.sqrt(len(is_s)))), 2
            )
            if len(is_s) > 30
            else None,
        },
        "OOS": {
            "days": int(len(oos_s)),
            "mean": round(float(oos_s.mean()), 3),
            "t_stat": round(
                float(oos_s.mean() / (oos_s.std(ddof=1) / np.sqrt(len(oos_s)))), 2
            )
            if len(oos_s) > 30
            else None,
        },
        "note": f"top-minus-bottom quintile of r20, hot tape only, {PRIMARY_H}d excess, overlapping",
    }
    return res


def _newey_west_t(x: np.ndarray, lag: int) -> float:
    """t-stat on the mean, corrected for the autocorrelation that overlapping
    h-day forward returns induce. Naive t is inflated by roughly sqrt(h)."""
    n = len(x)
    e = x - x.mean()
    s = (e @ e) / n
    for k in range(1, lag + 1):
        s += 2 * (1 - k / (lag + 1)) * ((e[k:] @ e[:-k]) / n)
    return float(x.mean() / np.sqrt(s / n))


def _block_boot_mean(x: np.ndarray, rng, block: int = BLOCK, n: int = N_BOOT) -> np.ndarray:
    n_blocks = int(np.ceil(len(x) / block))
    starts_max = len(x) - block
    out = np.empty(n)
    for i in range(n):
        starts = rng.integers(0, starts_max + 1, n_blocks)
        out[i] = np.concatenate([x[s : s + block] for s in starts])[: len(x)].mean()
    return out


def phase_g_overlap_corrected(d: pd.DataFrame) -> dict:
    """Headline number with honest inference: the hot-tape quintile spread."""
    target = f"x{PRIMARY_H}"
    hot = d[d["hot"].fillna(False)].dropna(subset=["r20", target])
    counts = hot.groupby("trade_date")["r20"].transform("size")
    hot = hot[counts >= 30]
    q = hot.groupby("trade_date")["r20"].transform(
        lambda x: pd.qcut(x.rank(method="first"), 5, labels=False)
    )
    hot = hot.assign(q=q)
    daily = hot.groupby(["trade_date", "q"])[target].mean().unstack()
    spread = (daily[4] - daily[0]).dropna()
    base = hot.groupby("trade_date")[target].mean()

    rng = np.random.default_rng(20260725)
    periods = {
        "full_pre_oos": spread[spread.index < OOS],
        "is_2019_2025": spread[(spread.index >= IS_START) & (spread.index < OOS)],
        "oos_2026": spread[spread.index >= OOS],
    }
    res: dict = {"periods": {}}
    for name, s in periods.items():
        if len(s) < 40:
            res["periods"][name] = {"days": int(len(s))}
            continue
        v = s.to_numpy(float)
        boot = _block_boot_mean(v, rng)
        res["periods"][name] = {
            "days": int(len(v)),
            "mean_pp": round(float(v.mean()), 3),
            "naive_t": round(float(v.mean() / (v.std(ddof=1) / np.sqrt(len(v)))), 2),
            "newey_west_t": round(_newey_west_t(v, PRIMARY_H), 2),
            "block_boot_ci95": [
                round(float(x), 3) for x in np.quantile(boot, [0.025, 0.975])
            ],
            "p_positive": round(float((boot > 0).mean()), 3),
        }
    res["by_year_mean_pp"] = {
        str(int(y)): {
            "days": int(len(g)),
            "mean_pp": round(float(g.mean()), 3),
            "newey_west_t": round(_newey_west_t(g.to_numpy(float), PRIMARY_H), 2),
        }
        for y, g in spread.groupby(spread.index.year)
        if len(g) >= 40
    }
    pre = daily[daily.index < OOS]
    b = base[base.index < OOS]
    res["quintile_vs_daily_mean_pp"] = {
        f"Q{i + 1}": round(float((pre[i] - b).dropna().mean()), 3) for i in range(5)
    }
    res["note"] = "Q5 = strongest ft3_rel_20; hot tape only; pre-OOS window"
    return res


def phase_h_deep_dive(d: pd.DataFrame) -> dict:
    """2492 under the same spec, with overlap-corrected bootstrap CIs."""
    from scipy import stats

    s = d[d["stock_id"] == DEEP_DIVE_SID].dropna(subset=["r20"]).reset_index(drop=True)
    rng = np.random.default_rng(int(DEEP_DIVE_SID))

    def boot_spearman(a: np.ndarray, b: np.ndarray) -> dict:
        m = np.isfinite(a) & np.isfinite(b)
        a, b = a[m], b[m]
        if len(a) < BLOCK * 5:
            return {"n": int(len(a))}
        n_blocks = int(np.ceil(len(a) / BLOCK))
        starts_max = len(a) - BLOCK
        out = np.empty(4000)
        for i in range(4000):
            starts = rng.integers(0, starts_max + 1, n_blocks)
            idx = np.concatenate([np.arange(x, x + BLOCK) for x in starts])[: len(a)]
            out[i] = stats.spearmanr(a[idx], b[idx]).statistic
        lo, hi = np.quantile(out, [0.025, 0.975])
        return {
            "n": int(len(a)),
            "spearman": round(float(stats.spearmanr(a, b).statistic), 4),
            "ci95": [round(float(lo), 4), round(float(hi), 4)],
            "significant": bool(lo > 0 or hi < 0),
        }

    feats = {}
    for feat in ("r20", "r20_d1", "r20_slope5", "r20_slope20"):
        feats[feat] = {
            f"x{h}": boot_spearman(s[feat].to_numpy(float), s[f"x{h}"].to_numpy(float))
            for h in HORIZONS
        }
    quint = {}
    x = s.dropna(subset=["r20_pctl", f"x{PRIMARY_H}"])
    for i, (lo, hi) in enumerate([(0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.01)]):
        c = x[(x["r20_pctl"] >= lo) & (x["r20_pctl"] < hi)][f"x{PRIMARY_H}"]
        quint[f"Q{i + 1}"] = _summary(c)
    return {
        "sid": DEEP_DIVE_SID,
        "n": int(len(s)),
        "hot_share": round(float(s["hot"].mean()), 3),
        "same_day_spearman": {
            "r20": round(float(s[["r20", "ret1"]].dropna().corr(method="spearman").iloc[0, 1]), 3),
            "r20_d1": round(
                float(s[["r20_d1", "ret1"]].dropna().corr(method="spearman").iloc[0, 1]), 3
            ),
        },
        "forward_bootstrap": feats,
        "own_history_quintiles": {**quint, "all": _summary(x[f"x{PRIMARY_H}"])},
        "note": "quintiles use the stock's own 252d PIT percentile of r20",
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    raw = load_universe()
    d = add_features(raw)
    is_m = (d["trade_date"] >= IS_START) & (d["trade_date"] < OOS)
    oos_m = d["trade_date"] >= OOS

    payload: dict = {
        "definition": {
            "ft3_rel_20": "sum(FT,3d) / sum(|FT|,20d), FT = foreign_net + investment_trust_net",
            "note": "the 20d abs window CONTAINS the 3 days, so daily deltas overlap",
            "primary_horizon": PRIMARY_H,
            "outcome": "excess forward return vs cross-sectional median market proxy",
            "is": [IS_START, OOS],
            "oos": [OOS, str(d["trade_date"].max().date())],
            "universe": int(d["stock_id"].nunique()),
            "rows": int(len(d)),
        },
        "phase_a_diagnostics": phase_a_diagnostics(d),
        "phase_b_double_sort": {
            "IS": phase_b_double_sort(d[is_m], "IS"),
            "OOS": phase_b_double_sort(d[oos_m], "OOS"),
        },
        "phase_c_quadrants": {
            "IS": phase_c_quadrants(d[is_m], "IS"),
            "OOS": phase_c_quadrants(d[oos_m], "OOS"),
        },
        "phase_d_cross_sectional_ic": phase_d_cross_sectional_ic(d),
        "phase_e_sustained_divergence": phase_e_sustained(d),
        "phase_f_regime_verification": phase_f_regime_verification(d),
        "phase_g_overlap_corrected": phase_g_overlap_corrected(d),
        "phase_h_deep_dive_2492": phase_h_deep_dive(d),
    }
    (OUT / "results.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"[ft3_rel_trend] wrote {OUT / 'results.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
