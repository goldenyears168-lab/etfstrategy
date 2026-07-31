"""分點訊號驗證共用工具：L1H7 protocol helpers + permutation/placebo 顯著性檢定。

背景：whale-branch-l1h7-discovery 研究一路下來，t-test/Wilcoxon 在小樣本（n=20~40）
厚尾（少數極端值主導點估計）情境下的顯著性判斷屢次不穩健（去除最大3-5筆極端值後
顯著性常常消失）。這裡提供一個 permutation/placebo 檢定：對每個真實訊號事件，從
「同一檔股票」的全部合法訊號日中隨機抽一個當作假訊號日，重複多次建立 null 分布，
看真實觀察值落在第幾百分位——不依賴常態/漸進假設，比 t-test 更適合這種資料。

不依賴任何 DB 連線；呼叫端自行從 stock_daily_bars / daily_bars 撈 bars 後傳入。

用法（呼叫端負責 DB 存取，這個模組只做純計算）：
    from research.branch_signal_validation import (
        build_l1h7_signal_dict, r_adj_for_signal_date, permutation_test,
    )
    stock_dict = build_l1h7_signal_dict(bars_2337)   # bars: [(date, open, close), ...] 已排序
    ix_dict = build_l1h7_signal_dict(ix_bars)
    result = permutation_test(real_events_df, {"2337": stock_dict, ...}, ix_dict)
"""

from __future__ import annotations

import numpy as np
import pandas as pd

COST_DEFAULT = 0.003
HOLD_DEFAULT = 7
BETA_DEFAULT = 1.15


def build_l1h7_signal_dict(
    bars: list[tuple[str, float, float]], hold: int = HOLD_DEFAULT
) -> dict[str, tuple[float, float]]:
    """對一支股票已排序的 bars=[(date,open,close),...]，算出每個可能訊號日的
    (T+1開盤進場價, 第hold個交易日收盤出場價)。回傳 dict: signal_date -> (entry_open, exit_close)。
    """
    n = len(bars)
    out: dict[str, tuple[float, float]] = {}
    for i in range(n - 1):
        entry_i = i + 1
        exit_i = entry_i + hold - 1
        if exit_i >= n:
            continue
        sig_d = bars[i][0]
        ent_o = bars[entry_i][1]
        if not ent_o or ent_o <= 0:
            continue
        exit_c = bars[exit_i][2]
        out[sig_d] = (ent_o, exit_c)
    return out


def r_adj_for_signal_date(
    stock_dict: dict[str, tuple[float, float]],
    ix_dict: dict[str, tuple[float, float]],
    signal_date: str,
    cost: float = COST_DEFAULT,
    beta: float = BETA_DEFAULT,
) -> float | None:
    """單一訊號日的 L1H7 beta 調整後超額報酬（百分比）。任一邊缺資料回傳 None。"""
    s = stock_dict.get(signal_date)
    if s is None:
        return None
    b = ix_dict.get(signal_date)
    if b is None:
        return None
    entry_open, exit_close = s
    ix_open, ix_close = b
    r_s = exit_close / entry_open - 1 - cost
    r_ix = ix_close / ix_open - 1
    return (r_s - beta * r_ix) * 100.0


def permutation_test(
    real_events: pd.DataFrame,
    stock_dicts: dict[str, dict[str, tuple[float, float]]],
    ix_dict: dict[str, tuple[float, float]],
    n_perm: int = 5000,
    seed: int = 20260728,
    cost: float = COST_DEFAULT,
    beta: float = BETA_DEFAULT,
) -> dict:
    """對每個真實事件，從同一檔股票的全部合法訊號日（排除真實事件自己的日期）隨機
    抽一個當假訊號日，重複 n_perm 次建立 null 分布。保留股票組成與事件筆數不變，
    只隨機化『時機』——測的是「這個分點選的時機，是否比同一批股票上的隨機時機更好」。

    real_events: DataFrame，至少要有 stock_id, signal_date 兩欄。
    回傳：n_events、觀察值 mean/median、null分布統計量、單尾經驗p值（mean與median各一個）。
    """
    rng = np.random.default_rng(seed)

    real_vals: list[float] = []
    stock_ids: list[str] = []
    for row in real_events.itertuples(index=False):
        sid = str(row.stock_id)
        v = r_adj_for_signal_date(stock_dicts.get(sid, {}), ix_dict, str(row.signal_date), cost, beta)
        if v is not None:
            real_vals.append(v)
            stock_ids.append(sid)
    real_vals_arr = np.array(real_vals)
    n_events = len(real_vals_arr)
    if n_events == 0:
        raise ValueError("沒有任何真實事件算得出 r_adj，檢查 stock_dicts/ix_dict 是否涵蓋對應日期")

    obs_mean = float(np.mean(real_vals_arr))
    obs_median = float(np.median(real_vals_arr))

    real_dates_by_stock: dict[str, set[str]] = {}
    for row in real_events.itertuples(index=False):
        real_dates_by_stock.setdefault(str(row.stock_id), set()).add(str(row.signal_date))

    candidate_pools: dict[str, list[str]] = {}
    for sid in set(stock_ids):
        excl = real_dates_by_stock.get(sid, set())
        pool = [d for d in stock_dicts.get(sid, {}).keys() if d not in excl]
        candidate_pools[sid] = pool

    perm_means = np.full(n_perm, np.nan)
    perm_medians = np.full(n_perm, np.nan)
    for p in range(n_perm):
        vals: list[float] = []
        for sid in stock_ids:
            pool = candidate_pools.get(sid)
            if not pool:
                continue
            d = pool[rng.integers(len(pool))]
            v = r_adj_for_signal_date(stock_dicts[sid], ix_dict, d, cost, beta)
            if v is not None:
                vals.append(v)
        if vals:
            perm_means[p] = float(np.mean(vals))
            perm_medians[p] = float(np.median(vals))

    valid_means = perm_means[~np.isnan(perm_means)]
    valid_medians = perm_medians[~np.isnan(perm_medians)]

    p_mean = float(np.mean(valid_means >= obs_mean)) if len(valid_means) else float("nan")
    p_median = float(np.mean(valid_medians >= obs_median)) if len(valid_medians) else float("nan")

    return {
        "n_events": int(n_events),
        "n_perm": int(n_perm),
        "observed_mean_pct": obs_mean,
        "observed_median_pct": obs_median,
        "placebo_mean_of_means_pct": float(np.mean(valid_means)) if len(valid_means) else None,
        "placebo_std_of_means_pct": float(np.std(valid_means)) if len(valid_means) else None,
        "placebo_mean_of_medians_pct": float(np.mean(valid_medians)) if len(valid_medians) else None,
        "placebo_std_of_medians_pct": float(np.std(valid_medians)) if len(valid_medians) else None,
        "p_value_mean_onesided": p_mean,
        "p_value_median_onesided": p_median,
        "n_valid_perms": int(len(valid_means)),
    }


def outlier_trim_sensitivity(
    real_events_r_adj: np.ndarray, trim_ns: tuple[int, ...] = (3, 5)
) -> dict:
    """去除最大|r_adj|的N筆後重算 mean/median/n，標準化這個一路都在用的敏感度檢查。"""
    out = {}
    order = np.argsort(-np.abs(real_events_r_adj))
    for k in trim_ns:
        keep = np.delete(real_events_r_adj, order[:k])
        out[f"drop_top{k}"] = {
            "n": int(len(keep)),
            "mean_pct": float(np.mean(keep)) if len(keep) else None,
            "median_pct": float(np.median(keep)) if len(keep) else None,
        }
    return out
