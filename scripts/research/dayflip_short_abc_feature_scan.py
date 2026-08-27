#!/usr/bin/env python3
"""dayflip-short candidate quality — ABC v3/F1式多因子系統掃描（research only）.

背景：dayflip-short (`src/order/dayflip_short_signal.py::build_candidates`) 目前候選
篩選只用分點買超金額(>=30M)、席位翻臉率(>=0.4)、ADV流動性、60日建倉排除——沒有對候選
做「品質評分」。ABC v3/F1（台指期進場擇時）是本repo另一套成熟的「系統掃描多個候選特徵
→逐一檢定顯著性」框架。本腳本借用同樣精神（非其TX期貨專用code），對190筆dayflip-short
交易重建的candidate做一次特徵電池式單變量篩選，看有沒有目前沒用到、但真的有訊號的特徵。

標的資料：reports/research/dayflip_revenue_momentum_filter/trades_with_revyoy.csv (190筆,
190筆逐trade PnL已知)。既有特徵(rvol/adv_yi/amt_yi/advshare/mfe/ret0/ret5)取自
reports/research/branch-footprint-screen/dayflip_gapup_short/all_trades.csv（170/190筆有
覆蓋），缺的20筆(2026-07以後forward-test)用相同公式(run_branch_dayflip_feature_profile.py
的定義)重算補齊，確保n=190全覆蓋。

PIT：所有特徵只用signal_date(T0)收盤後可得的資訊（T0收盤價/量、T0以前20日窗），
T0+1才是實際下單日——與build_candidates()盤前算候選的時序一致。

輸出：reports/research/abc_factor_scan_dayflip_transplant/feature_scan_results.csv/json
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path
from statistics import mean, stdev

import pandas as pd
from scipy import stats

import stock_db

ROOT = stock_db.PROJECT_ROOT
TRADES_CSV = ROOT / "reports/research/dayflip_revenue_momentum_filter/trades_with_revyoy.csv"
ALLTRADES_CSV = ROOT / "reports/research/branch-footprint-screen/dayflip_gapup_short/all_trades.csv"
OUT_DIR = ROOT / "reports/research/abc_factor_scan_dayflip_transplant"
SOURCE = "finmind"


def connect() -> sqlite3.Connection:
    return sqlite3.connect(f"file:{stock_db.DEFAULT_DB_PATH}?mode=ro", uri=True)


def load_bars(conn: sqlite3.Connection, stock_ids: list[str], lo: str, hi: str):
    bars: dict[str, dict[str, tuple]] = {}
    q = ",".join("?" * len(stock_ids))
    for sid, d, o, h, lw, c, v, amt in conn.execute(
        f"SELECT stock_id, trade_date, open, high, low, close, volume, amount "
        f"FROM stock_daily_bars WHERE source=? AND stock_id IN ({q}) "
        f"AND trade_date BETWEEN ? AND ? AND close>0",
        (SOURCE, *stock_ids, lo, hi),
    ):
        bars.setdefault(str(sid), {})[str(d)] = (
            float(o or 0), float(h or 0), float(lw or 0), float(c),
            float(v or 0), float(amt or 0),
        )
    return bars


def load_ix(conn: sqlite3.Connection, lo: str, hi: str) -> dict[str, tuple[float, float]]:
    ix: dict[str, tuple[float, float]] = {}
    rank = {"yahoo": 0, "tej": 1, "finmind": 2}
    best: dict[str, int] = {}
    for d, o, c, src in conn.execute(
        "SELECT date, open, close, source FROM daily_bars "
        "WHERE code='IX0001' AND date BETWEEN ? AND ? AND open>0 AND close>0",
        (lo, hi),
    ):
        r = rank.get(str(src), 9)
        if str(d) not in best or r < best[str(d)]:
            best[str(d)] = r
            ix[str(d)] = (float(o), float(c))
    return ix


def compute_missing(row, bars, ix):
    """對all_trades.csv沒覆蓋到的trade，用相同公式(feature_profile script)重算."""
    sid, d = row["stock"], row["signal_date"]
    ds = sorted(bars.get(sid, {}))
    if d not in ds:
        return None
    i = ds.index(d)
    if i < 21:
        return None
    o0, h0, l0, c0, v0, amt0 = bars[sid][d]
    prev_c = bars[sid][ds[i - 1]][3]
    ret0 = c0 / prev_c - 1 if prev_c else None
    ret5 = c0 / bars[sid][ds[i - 5]][3] - 1 if i >= 5 else None
    vols = [bars[sid][x][4] for x in ds[i - 20:i]]
    avg_v = mean(vols) if vols else 0.0
    rvol = v0 / avg_v if avg_v > 0 else None
    amts = [bars[sid][x][5] for x in ds[i - 20:i]]
    adv = mean(amts) if amts else 0.0
    return dict(ret0=ret0, ret5=ret5, rvol=rvol, adv_yi=adv / 1e8 if adv else None,
                amt_yi=(v0 * c0) / 1e8 if v0 and c0 else None)


def main() -> None:
    trades = pd.read_csv(TRADES_CSV, dtype={"stock": str})
    alltr = pd.read_csv(ALLTRADES_CSV, dtype={"stock": str})
    merged = trades.merge(
        alltr[["signal_date", "stock", "rvol", "adv_yi", "amt_yi", "advshare",
               "share_vol", "mfe", "ret0", "ret5"]],
        on=["signal_date", "stock"], how="left",
    )

    conn = connect()
    sids = sorted(set(trades["stock"]))
    lo = "2025-06-01"
    hi = "2026-08-06"
    bars = load_bars(conn, sids, lo, hi)
    ix = load_ix(conn, lo, hi)

    # 補齊 all_trades.csv 未覆蓋(2026-07以後forward-test)的列
    for idx, r in merged.iterrows():
        if pd.isna(r["rvol"]):
            filled = compute_missing(r, bars, ix)
            if filled:
                for k, v in filled.items():
                    if v is not None:
                        merged.at[idx, k] = v

    # 額外特徵：day-of-week、repeat-signal(過去20交易日內同標的是否出現過)、市場當日報酬(IX0001 T0)
    dow_map = {}
    repeat_flag = []
    mkt_ret0 = []
    stock_hist: dict[str, list[str]] = {}
    trades_sorted = merged.sort_values("signal_date").reset_index(drop=True)
    for _, r in trades_sorted.iterrows():
        sid, d = r["stock"], r["signal_date"]
        hist = stock_hist.get(sid, [])
        # 過去60日曆天內是否曾出現過(同一資料集內)
        recent = [h for h in hist if (date.fromisoformat(d) - date.fromisoformat(h)).days <= 60]
        repeat_flag.append(1 if recent else 0)
        stock_hist.setdefault(sid, []).append(d)
    trades_sorted["repeat_signal_60d"] = repeat_flag
    trades_sorted["dow"] = trades_sorted["signal_date"].apply(
        lambda d: date.fromisoformat(d).weekday()
    )

    def ix_ret0(d):
        ds = sorted(ix)
        if d not in ds:
            return None
        i = ds.index(d)
        if i < 1:
            return None
        prev_c = ix[ds[i - 1]][1]
        c0 = ix[d][1]
        return c0 / prev_c - 1 if prev_c else None

    trades_sorted["mkt_ret0"] = trades_sorted["signal_date"].apply(ix_ret0)
    trades_sorted["fgap_excess"] = trades_sorted["fgap"] - 6.0  # 超過6%門檻的幅度

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    trades_sorted.to_csv(OUT_DIR / "feature_table.csv", index=False)

    # ---- 單變量檢定 ----
    outcome = trades_sorted["pnl_pct"]
    candidate_features = [
        "n_seats", "fgap", "fgap_excess", "rvol", "adv_yi", "amt_yi", "advshare",
        "share_vol", "ret0", "ret5", "mkt_ret0", "dow", "repeat_signal_60d",
    ]
    results = []
    for feat in candidate_features:
        sub = trades_sorted[[feat, "pnl_pct"]].dropna()
        n = len(sub)
        if n < 20:
            results.append(dict(feature=feat, n=n, spearman_rho=None, p_value=None,
                                 note="n太小跳過"))
            continue
        rho, p = stats.spearmanr(sub[feat], sub["pnl_pct"])
        # 三分位/二分位(binary特徵)切法補充
        if sub[feat].nunique() <= 2:
            g0 = sub[sub[feat] == sub[feat].min()]["pnl_pct"]
            g1 = sub[sub[feat] == sub[feat].max()]["pnl_pct"]
            tstat, tp = stats.mannwhitneyu(g1, g0, alternative="two-sided") if len(g0) > 0 and len(g1) > 0 else (None, None)
            extra = dict(group_low_n=len(g0), group_low_mean=round(g0.mean(), 3) if len(g0) else None,
                         group_high_n=len(g1), group_high_mean=round(g1.mean(), 3) if len(g1) else None,
                         mannwhitney_p=round(tp, 4) if tp is not None else None)
        else:
            q = pd.qcut(sub[feat], 3, duplicates="drop")
            grp = sub.groupby(q, observed=True)["pnl_pct"]
            tercile_means = {str(k): round(v.mean(), 3) for k, v in grp}
            lo_g = sub[sub[feat] <= sub[feat].quantile(1/3)]["pnl_pct"]
            hi_g = sub[sub[feat] >= sub[feat].quantile(2/3)]["pnl_pct"]
            tstat, tp = stats.mannwhitneyu(hi_g, lo_g, alternative="two-sided")
            extra = dict(tercile_means=tercile_means, mannwhitney_hi_vs_lo_p=round(tp, 4))
        results.append(dict(feature=feat, n=n, spearman_rho=round(rho, 4), p_value=round(p, 4),
                             **extra))

    (OUT_DIR / "feature_scan_results.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(json.dumps(results, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
