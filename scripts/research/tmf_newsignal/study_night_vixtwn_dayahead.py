#!/usr/bin/env python3
"""夜盤淨位移 + 前日VIXTWN水準 → 隔日日盤方向 假說研究。

假說：夜盤 (15:00-23:59, 反映美股隔夜走勢) 的淨位移方向/幅度，加上前一日
VIXTWN收盤水準/變化，能否預測隔日台指日盤 (08:45-13:45) open-to-close 報酬。

因果結構（嚴格）：
  Date D 的訊號 = night_ret_D (15:00-23:59 close - open) + vix_level_D (VIXTWN
    close on D) + vix_chg_D (VIXTWN close D - close D-1)
  → 全部在 D 收盤（23:59 或更早）已知，早於 D+1 日盤開盤 08:45。
  目標 = day_ret_{D+1} = day_close_{D+1} - day_open_{D+1}

  D+1 一律取 tx cache 排序後「下一個存在的 key」，也就是 cache 裡下一個實際
  交易日，不管中間跳過幾天（換月/國定假日），順序本身保證因果（D+1 的 08:45
  bar 在時間上必然晚於 D 的 23:59 bar），不需要額外對齊行事曆。

輸出：
  reports/research/tmf_newsignal/panel.csv          -- 逐日因果特徵 + 目標
  reports/research/tmf_newsignal/ic_results.json     -- 5-fold causal Spearman IC
  reports/research/tmf_newsignal/backtest_results.json -- 誠實回測 + 顯著性檢定
  reports/research/tmf_newsignal/perturbation_test.json -- 自我驗證因果性
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats as sstats

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "src"))

from stock_db import DEFAULT_DB_PATH  # noqa: E402

CACHE_PATH = REPO_ROOT / "reports/research/channel_lab/tx_1m_2yr_extension_cache.json"
OUT_DIR = REPO_ROOT / "reports/research/tmf_newsignal"
COST_PT = 2.0  # round-trip fixed cost, points (TMF micro-channel repo convention)
N_FOLDS = 5
IS_FRACTION = 0.7


# ----------------------------------------------------------------------------
# Step 1: load raw 1m cache -> per-date session summary (day + night)
# ----------------------------------------------------------------------------
def load_session_summary(cache: dict[str, list[dict]]) -> pd.DataFrame:
    rows = []
    for date_key in sorted(cache.keys()):
        bars = cache[date_key]
        day_bars = [b for b in bars if b["sess"] == "day"]
        night_bars = [b for b in bars if b["sess"] == "night"]
        # bonus/exploratory secondary target window: first ~60min of day session
        # (08:45-09:45). Use the last bar with t <= "09:45" (causal within the
        # session itself — only used as an *alternative target*, never as a
        # predictor, so no look-ahead concern).
        first60_bars = [b for b in day_bars if b["t"] <= "09:45"]
        row = {
            "date": date_key,
            "day_open": day_bars[0]["o"] if day_bars else np.nan,
            "day_close": day_bars[-1]["c"] if day_bars else np.nan,
            "day_first60_close": first60_bars[-1]["c"] if first60_bars else np.nan,
            "day_first_t": day_bars[0]["t"] if day_bars else None,
            "day_last_t": day_bars[-1]["t"] if day_bars else None,
            "n_day_bars": len(day_bars),
            "night_open": night_bars[0]["o"] if night_bars else np.nan,
            "night_close": night_bars[-1]["c"] if night_bars else np.nan,
            "night_first_t": night_bars[0]["t"] if night_bars else None,
            "night_last_t": night_bars[-1]["t"] if night_bars else None,
            "n_night_bars": len(night_bars),
        }
        rows.append(row)
    df = pd.DataFrame(rows).set_index("date")
    df["day_ret"] = df["day_close"] - df["day_open"]
    df["day_ret_first60"] = df["day_first60_close"] - df["day_open"]
    df["night_ret"] = df["night_close"] - df["night_open"]
    return df


def load_vixtwn(db_path: str) -> pd.Series:
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            "SELECT date, close FROM market_vix_daily "
            "WHERE symbol='VIXTWN' AND source='computed' ORDER BY date"
        )
        rows = cur.fetchall()
    finally:
        conn.close()
    s = pd.Series({d: c for d, c in rows}, name="vix_close").sort_index()
    return s


# ----------------------------------------------------------------------------
# Step 2: build causal panel — D's night+VIX -> D+1's day session return
# ----------------------------------------------------------------------------
def build_panel(sess: pd.DataFrame, vix: pd.Series) -> pd.DataFrame:
    dates = list(sess.index)  # sorted ISO strings -> chronological order
    rows = []
    for i in range(len(dates) - 1):
        d, d_next = dates[i], dates[i + 1]
        r = sess.loc[d]
        r_next = sess.loc[d_next]

        vix_d = vix.get(d, np.nan)
        # causal as-of lookup for previous VIX close strictly before d's own date key
        prior_idx = vix.index[vix.index < d]
        vix_prev = vix.loc[prior_idx[-1]] if len(prior_idx) else np.nan

        rows.append(
            {
                "date_D": d,
                "date_Dplus1": d_next,
                "night_ret_D": r["night_ret"],
                "night_open_D": r["night_open"],
                "night_close_D": r["night_close"],
                "n_night_bars_D": r["n_night_bars"],
                "vix_level_D": vix_d,
                "vix_chg_D": (vix_d - vix_prev) if pd.notna(vix_d) and pd.notna(vix_prev) else np.nan,
                "day_open_Dplus1": r_next["day_open"],
                "day_close_Dplus1": r_next["day_close"],
                "day_ret_Dplus1": r_next["day_ret"],
                "day_ret_first60_Dplus1": r_next["day_ret_first60"],
            }
        )
    panel = pd.DataFrame(rows)
    panel["night_sign_D"] = np.sign(panel["night_ret_D"])
    # raw interaction (no demeaning/standardization here — kept as a single raw
    # column so both the IC screen and the backtest use the exact same values;
    # the backtest's own IS-only z-scoring handles centering downstream).
    panel["interaction_night_x_vix_D"] = panel["night_ret_D"] * panel["vix_level_D"]
    return panel


def filter_valid(panel: pd.DataFrame) -> pd.DataFrame:
    required = [
        "night_ret_D",
        "vix_level_D",
        "vix_chg_D",
        "day_ret_Dplus1",
        "day_open_Dplus1",
        "day_close_Dplus1",
    ]
    valid = panel.dropna(subset=required).reset_index(drop=True)
    return valid


# ----------------------------------------------------------------------------
# Step 3: perturbation self-test — proves signal(D) is unaffected by
# mutating bars strictly at/after D+1's day session (and vice versa for
# VIX: mutating VIX at date >= D_next must not change vix_level_D/vix_chg_D).
# ----------------------------------------------------------------------------
def perturbation_test(cache: dict, vix: pd.Series) -> dict:
    import copy

    cache2 = copy.deepcopy(cache)
    dates_sorted = sorted(cache2.keys())
    # pick a target date that has both day+night in the middle of the sample
    target_date = dates_sorted[len(dates_sorted) // 2]
    # mutate every bar of the *day* session on target_date (this date acts as
    # "D+1" for the *previous* key in sorted order)
    for b in cache2[target_date]:
        if b["sess"] == "day":
            b["o"] += 500.0
            b["h"] += 500.0
            b["l"] += 500.0
            b["c"] += 500.0

    sess_orig = load_session_summary(cache)
    sess_mut = load_session_summary(cache2)
    panel_orig = build_panel(sess_orig, vix)
    panel_mut = build_panel(sess_mut, vix)

    # find the row where date_Dplus1 == target_date -> D's signal must be identical
    row_o = panel_orig[panel_orig["date_Dplus1"] == target_date]
    row_m = panel_mut[panel_mut["date_Dplus1"] == target_date]
    signal_cols = ["night_ret_D", "night_open_D", "night_close_D", "vix_level_D", "vix_chg_D"]
    signal_unaffected = bool(
        len(row_o) == 1
        and len(row_m) == 1
        and np.allclose(
            row_o[signal_cols].to_numpy(dtype=float),
            row_m[signal_cols].to_numpy(dtype=float),
            equal_nan=True,
        )
    )
    # and the target itself for that row (day_ret_Dplus1) MUST have changed
    # (open shifted by 500 too, so open-to-close unaffected in this synthetic
    # shift; use a bar-specific perturbation instead: change only the last bar's
    # close so open-to-close target changes but leaves earlier bars alone)
    cache3 = copy.deepcopy(cache)
    day_bars3 = [b for b in cache3[target_date] if b["sess"] == "day"]
    if day_bars3:
        day_bars3[-1]["c"] += 777.0
    sess_mut3 = load_session_summary(cache3)
    panel_mut3 = build_panel(sess_mut3, vix)
    row_m3 = panel_mut3[panel_mut3["date_Dplus1"] == target_date]
    target_changed = bool(
        len(row_m3) == 1
        and not np.isclose(
            float(row_m3["day_ret_Dplus1"].iloc[0]), float(row_o["day_ret_Dplus1"].iloc[0])
        )
    )
    signal_cols_row = row_m3[signal_cols].to_numpy(dtype=float)
    signal_still_unaffected_by_c_shift = bool(
        np.allclose(signal_cols_row, row_o[signal_cols].to_numpy(dtype=float), equal_nan=True)
    )

    # reverse-direction check: mutate VIX at date >= target_date's *next* trading
    # date and confirm vix_level_D / vix_chg_D for D=target_date unaffected.
    vix2 = vix.copy()
    later_idx = vix2.index[vix2.index > target_date]
    if len(later_idx):
        vix2.loc[later_idx[0]] = vix2.loc[later_idx[0]] + 999.0
    panel_vix_mut = build_panel(sess_orig, vix2)
    row_vix_mut = panel_vix_mut[panel_vix_mut["date_D"] == target_date]
    row_orig_d = panel_orig[panel_orig["date_D"] == target_date]
    vix_signal_unaffected_by_future = bool(
        len(row_vix_mut) == 1
        and len(row_orig_d) == 1
        and np.isclose(
            float(row_vix_mut["vix_level_D"].iloc[0]), float(row_orig_d["vix_level_D"].iloc[0])
        )
        and np.isclose(
            float(row_vix_mut["vix_chg_D"].iloc[0]), float(row_orig_d["vix_chg_D"].iloc[0])
        )
    )

    return {
        "target_date_perturbed": target_date,
        "test_1_shift_whole_day_session_by_500pt": {
            "description": "整段D+1日盤OHLC平移+500，D的訊號(night_ret/vix_level/vix_chg)必須完全不變",
            "signal_unaffected": signal_unaffected,
        },
        "test_2_shift_last_bar_close_only": {
            "description": "只動D+1日盤最後一根bar的close(+777)，目標day_ret_Dplus1必須改變，但D的訊號仍不變",
            "target_changed_as_expected": target_changed,
            "signal_still_unaffected": signal_still_unaffected_by_c_shift,
        },
        "test_3_future_vix_mutation": {
            "description": "把D之後最近一個VIXTWN交易日的值改動+999，D當天的vix_level_D/vix_chg_D必須不變",
            "signal_unaffected": vix_signal_unaffected_by_future,
        },
        "all_passed": bool(
            signal_unaffected
            and target_changed
            and signal_still_unaffected_by_c_shift
            and vix_signal_unaffected_by_future
        ),
    }


# ----------------------------------------------------------------------------
# Step 4: 5-fold causal (chronological, non-shuffled) Spearman IC
# ----------------------------------------------------------------------------
def chronological_folds(n: int, k: int) -> list[tuple[int, int]]:
    edges = np.linspace(0, n, k + 1).astype(int)
    return [(int(edges[i]), int(edges[i + 1])) for i in range(k)]


def spearman_ic_folds(x: np.ndarray, y: np.ndarray, k: int = N_FOLDS) -> dict:
    n = len(x)
    folds = chronological_folds(n, k)
    fold_ics = []
    for (a, b) in folds:
        xi, yi = x[a:b], y[a:b]
        if len(xi) < 5 or np.all(xi == xi[0]):
            fold_ics.append(None)
            continue
        rho, pval = sstats.spearmanr(xi, yi)
        fold_ics.append(float(rho))
    valid = [v for v in fold_ics if v is not None]
    n_pos = sum(1 for v in valid if v > 0)
    n_neg = sum(1 for v in valid if v < 0)
    passed = (n_pos >= 4) or (n_neg >= 4)
    direction = 1 if n_pos > n_neg else (-1 if n_neg > n_pos else 0)
    return {
        "fold_ics": fold_ics,
        "n_folds_valid": len(valid),
        "n_folds_positive": n_pos,
        "n_folds_negative": n_neg,
        "passed_4of5_same_sign": bool(passed),
        "majority_direction": direction,
        "fold_boundaries_row_idx": folds,
    }


def run_ic_screen(panel: pd.DataFrame) -> dict:
    y = panel["day_ret_Dplus1"].to_numpy(dtype=float)
    features = {
        "night_ret_D": panel["night_ret_D"].to_numpy(dtype=float),
        "night_sign_D": panel["night_sign_D"].to_numpy(dtype=float),
        "vix_level_D": panel["vix_level_D"].to_numpy(dtype=float),
        "vix_chg_D": panel["vix_chg_D"].to_numpy(dtype=float),
        "interaction_night_x_vix_D": panel["interaction_night_x_vix_D"].to_numpy(dtype=float),
    }
    out = {}
    for name, x in features.items():
        out[name] = spearman_ic_folds(x, y)
    # bonus / exploratory: |night_ret| vs |day_ret_next| volatility spillover
    out["_bonus_abs_night_ret_vs_abs_day_ret"] = spearman_ic_folds(
        np.abs(panel["night_ret_D"].to_numpy(dtype=float)), np.abs(y)
    )
    # bonus / exploratory: alternate target window — first ~60min of day
    # session instead of full open-to-close (tests whether predictive power,
    # if any, concentrates early and gets washed out by midday noise)
    y60 = panel["day_ret_first60_Dplus1"].to_numpy(dtype=float)
    valid60 = ~np.isnan(y60)
    for name, x in list(features.items()):
        out[f"_bonus_first60window_{name}"] = spearman_ic_folds(x[valid60], y60[valid60])
    return out


# ----------------------------------------------------------------------------
# Step 5: honest backtest (IS-determined direction, OOS unchanged)
# ----------------------------------------------------------------------------
def newey_west_test(x: np.ndarray, maxlags: tuple[int, ...] = (1, 5, 10)) -> dict:
    import statsmodels.api as sm

    n = len(x)
    naive_t, naive_p = sstats.ttest_1samp(x, 0.0)
    out = {
        "n": n,
        "mean": float(np.mean(x)),
        "std": float(np.std(x, ddof=1)),
        "naive_t_stat": float(naive_t),
        "naive_p_value": float(naive_p),
        "hac": {},
    }
    exog = np.ones((n, 1))
    for lag in maxlags:
        model = sm.OLS(x, exog).fit(cov_type="HAC", cov_kwds={"maxlags": lag})
        out["hac"][str(lag)] = {
            "t_stat": float(model.tvalues[0]),
            "p_value": float(model.pvalues[0]),
        }
    out["hac_significant_all_maxlags_p_lt_0.05"] = bool(
        all(out["hac"][str(lag)]["p_value"] < 0.05 for lag in maxlags)
    )
    return out


def backtest_feature(
    panel: pd.DataFrame, feature_col: str, ic_screen_row: dict, cost_pt: float = COST_PT
) -> dict:
    n = len(panel)
    is_n = int(round(n * IS_FRACTION))
    is_df = panel.iloc[:is_n].reset_index(drop=True)
    oos_df = panel.iloc[is_n:].reset_index(drop=True)

    # direction rule determined on IS ONLY: re-run 5-fold IC restricted to IS
    x_is = is_df[feature_col].to_numpy(dtype=float)
    y_is = is_df["day_ret_Dplus1"].to_numpy(dtype=float)
    is_ic = spearman_ic_folds(x_is, y_is)
    direction_sign = is_ic["majority_direction"]
    if direction_sign == 0:
        direction_sign = 1  # degenerate tie fallback; documented below

    is_mean = float(np.mean(x_is))
    is_std = float(np.std(x_is, ddof=1))

    def trade_frame(df: pd.DataFrame) -> pd.DataFrame:
        x = df[feature_col].to_numpy(dtype=float)
        z = (x - is_mean) / is_std if is_std > 0 else np.zeros_like(x)
        pos = direction_sign * np.sign(z)
        entry = df["day_open_Dplus1"].to_numpy(dtype=float)
        exit_ = df["day_close_Dplus1"].to_numpy(dtype=float)
        gross = pos * (exit_ - entry)
        net = np.where(pos != 0, gross - cost_pt, 0.0)
        out_df = df.copy()
        out_df["position"] = pos
        out_df["gross_pts"] = gross
        out_df["net_pts"] = net
        out_df["is_fill"] = pos != 0
        return out_df

    is_trades = trade_frame(is_df)
    oos_trades = trade_frame(oos_df)
    all_trades = pd.concat([is_trades, oos_trades], ignore_index=True)

    def segment_stats(df: pd.DataFrame, label: str) -> dict:
        n_signals = len(df)
        n_fills = int(df["is_fill"].sum())
        n_skipped = n_signals - n_fills
        n_closed = n_fills  # same-session exit design, zero carry by construction
        assert n_signals == n_fills + n_skipped
        assert n_fills == n_closed
        filled = df[df["is_fill"]]
        net = filled["net_pts"].to_numpy(dtype=float)
        stats_block = {}
        if len(net) >= 8:
            stats_block = newey_west_test(net)
        return {
            "segment": label,
            "n_signals": n_signals,
            "n_fills": n_fills,
            "n_skipped": n_skipped,
            "n_closed": n_closed,
            "total_net_pts": float(net.sum()) if len(net) else 0.0,
            "mean_net_pts_per_trade": float(net.mean()) if len(net) else None,
            "win_rate": float((net > 0).mean()) if len(net) else None,
            "significance": stats_block,
        }

    return {
        "feature": feature_col,
        "direction_rule": {
            "is_fold_ic": is_ic,
            "direction_sign_applied": direction_sign,
            "is_mean_used_for_zscore": is_mean,
            "is_std_used_for_zscore": is_std,
            "note": "z-score anchor (mean/std) 與方向都只用 IS 段決定，OOS 段套用同一組凍結參數，不重新估計",
        },
        "IS": segment_stats(is_trades, "IS"),
        "OOS": segment_stats(oos_trades, "OOS"),
        "COMBINED": segment_stats(all_trades, "COMBINED (IS+OOS, 供參考不是confirmatory)"),
    }


# ----------------------------------------------------------------------------
def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    cache = json.loads(CACHE_PATH.read_text())
    vix = load_vixtwn(str(DEFAULT_DB_PATH))

    sess = load_session_summary(cache)
    panel_raw = build_panel(sess, vix)
    panel = filter_valid(panel_raw)

    panel.to_csv(OUT_DIR / "night_vixtwn_dayahead_panel.csv", index=False)

    coverage = {
        "cache_dates_total": int(len(sess)),
        "cache_date_range": [sess.index.min(), sess.index.max()],
        "dates_missing_night": sess[sess["n_night_bars"] == 0].index.tolist(),
        "dates_missing_day": sess[sess["n_day_bars"] == 0].index.tolist(),
        "vixtwn_computed_range": [str(vix.index.min()), str(vix.index.max())],
        "vixtwn_computed_n_rows": int(len(vix)),
        "panel_raw_rows": int(len(panel_raw)),
        "panel_valid_rows_after_dropna": int(len(panel)),
        "panel_valid_date_range": [panel["date_D"].min(), panel["date_D"].max()] if len(panel) else None,
        "note": (
            "panel_raw_rows = len(cache_dates)-1 (每個D配對下一個存在的D+1 key)；"
            "valid筆數 < raw是因為部分D缺夜盤(11天)或VIXTWN在該日或前一交易日無資料"
        ),
    }

    perturb = perturbation_test(cache, vix)
    (OUT_DIR / "night_vixtwn_dayahead_perturbation_test.json").write_text(json.dumps(perturb, indent=2, ensure_ascii=False))

    ic_results = run_ic_screen(panel)
    (OUT_DIR / "night_vixtwn_dayahead_ic_results.json").write_text(
        json.dumps({"coverage": coverage, "ic": ic_results}, indent=2, ensure_ascii=False, default=str)
    )

    core_features = ["night_ret_D", "night_sign_D", "vix_level_D", "vix_chg_D", "interaction_night_x_vix_D"]
    passed_features = [f for f in core_features if ic_results[f]["passed_4of5_same_sign"]]

    backtests = {}
    for feat in passed_features:
        backtests[feat] = backtest_feature(panel, feat, ic_results[feat])

    result = {
        "coverage": coverage,
        "perturbation_test": perturb,
        "ic_screen": ic_results,
        "features_passed_ic_screen": passed_features,
        "backtests": backtests,
        "config": {
            "cost_pt_round_trip": COST_PT,
            "n_folds": N_FOLDS,
            "is_fraction": IS_FRACTION,
        },
    }
    (OUT_DIR / "night_vixtwn_dayahead_backtest_results.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False, default=str)
    )

    print(json.dumps(coverage, indent=2, ensure_ascii=False))
    print("\n=== perturbation_test.all_passed ===", perturb["all_passed"])
    print("\n=== IC screen (pass = 4/5 folds same sign) ===")
    for f in ic_results.keys():
        r = ic_results[f]
        print(
            f"{f:32s} pos={r['n_folds_positive']} neg={r['n_folds_negative']} "
            f"valid={r['n_folds_valid']} PASS={r['passed_4of5_same_sign']} "
            f"fold_ics={[None if v is None else round(v,3) for v in r['fold_ics']]}"
        )
    print("\nfeatures passed IC screen ->", passed_features)
    if not passed_features:
        print("\n無特徵通過IC篩選門檻，不進行回測（依規格：只對通過IC門檻的特徵做回測）。")
    else:
        for feat, bt in backtests.items():
            print(f"\n--- backtest: {feat} ---")
            print(json.dumps(bt, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
