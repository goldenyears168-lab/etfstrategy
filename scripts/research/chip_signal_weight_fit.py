#!/usr/bin/env python3
"""籌碼訊號的權重擬合與命中率檢定 —— 等權 vs 方向別權重，walk-forward 驗證。

**問題**：`chip_daily_horizon_null_test.py` 的評分是等權（每個訊號 ±1）。
權重應該有依據，而不是拍腦袋。本腳本用資料擬合，並回答「加權能不能提高命中率」。

**方法**

1. 把每個三態訊號 S_i ∈ {−1 多, 0 中性, +1 空} 拆成**兩個方向別 dummy**：
   ``bear_i = 1[S_i=+1]``、``bull_i = 1[S_i=−1]``。5 個訊號 → 10 個迴歸元。
   這樣多空可以有不同權重（實測兩側的持續性本來就不對稱）。
2. **Walk-forward**：以交易日為序，用前 ``--train`` 天擬合 OLS（被解釋變數＝隔日
   超額報酬），對接下來 ``--step`` 天預測，滾動前進。每次只用過去資料，無前視。
3. 預測值 = 期望超額報酬。依當日橫斷面分位切成 看多／中性／看空 三帶。
4. 與等權評分在同一組 OOS 日期上比較。

**判準**（比命中率更該看的）
* ``hit`` 命中率：看多帶中超額為正的比例、看空帶中超額為負的比例。⚠️ 超額報酬
  右偏、無條件時 56% 為負，故看空側的命中率天生就比看多側高——**必須跟
  無條件基準比，不能看絕對值**。
* ``spread`` 多空帶的每日超額價差與 t 值——這才是經濟意義上的判準。

**已知限制**：訊號定義本身（含「當日無借券成交算偏多」那個事後選擇）沿用
`chip_daily_horizon_null_test.build_score`，未重新預先登錄；權重擬合只是在既有
定義上做，不會修好定義層的問題。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

SIGNALS = ["S1", "S2", "S3", "S4", "S5"]


def _tstat(s: pd.Series) -> float:
    return s.mean() / (s.std(ddof=1) / np.sqrt(len(s)))


def make_dummies(d: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    x = d.copy()
    cols = []
    for s in SIGNALS:
        x[f"{s}_bear"] = (x[s] == 1).astype(float)
        x[f"{s}_bull"] = (x[s] == -1).astype(float)
        cols += [f"{s}_bear", f"{s}_bull"]
    return x, cols


def fit_ols(X: np.ndarray, y: np.ndarray) -> np.ndarray:
    X1 = np.column_stack([np.ones(len(X)), X])
    beta, *_ = np.linalg.lstsq(X1, y, rcond=None)
    return beta


def walk_forward(d: pd.DataFrame, ret_col: str, train: int, step: int
                 ) -> tuple[pd.DataFrame, pd.DataFrame]:
    """回傳 (帶預測值的 OOS 面板, 每期權重)。"""
    d, cols = make_dummies(d)
    d = d.dropna(subset=[ret_col] + cols)
    dates = np.sort(d.trade_date.unique())
    out, weights = [], []
    i = train
    while i < len(dates):
        tr = d[d.trade_date.isin(dates[max(0, i - train):i])]
        te = d[d.trade_date.isin(dates[i:i + step])]
        if len(tr) < 500 or te.empty:
            i += step
            continue
        beta = fit_ols(tr[cols].to_numpy(), tr[ret_col].to_numpy())
        pred = beta[0] + te[cols].to_numpy() @ beta[1:]
        t = te.copy()
        t["pred"] = pred
        out.append(t)
        weights.append(dict(zip(cols, beta[1:], strict=True),
                            fold_start=dates[i], n_train=len(tr)))
        i += step
    return (pd.concat(out, ignore_index=True) if out else pd.DataFrame(),
            pd.DataFrame(weights))


def bands(x: pd.DataFrame, col: str, q: float) -> pd.Series:
    """每日橫斷面依 col 切三帶。

    ``col`` 一律定義為「**越大越看多**」——``pred`` 是預期超額報酬（高＝該漲），
    等權版則用 ``-net``（淨多分）。故前 q 分位標看多、後 q 標看空。
    """
    def f(g):
        lo, hi = g[col].quantile(q), g[col].quantile(1 - q)
        return pd.Series(np.where(g[col] >= hi, "看多",
                         np.where(g[col] <= lo, "看空", "中性")), index=g.index)
    return x.groupby("trade_date", group_keys=False).apply(f, include_groups=False)


def evaluate(x: pd.DataFrame, band_col: str, ret_col: str, label: str) -> dict:
    bull, bear = x[x[band_col] == "看多"], x[x[band_col] == "看空"]
    base_pos = (x[ret_col] > 0).mean()
    hit_bull = (bull[ret_col] > 0).mean()
    hit_bear = (bear[ret_col] < 0).mean()
    sp = x.groupby("trade_date").apply(
        lambda g: g[g[band_col] == "看多"][ret_col].mean()
        - g[g[band_col] == "看空"][ret_col].mean(), include_groups=False).dropna()
    return {
        "版本": label,
        "看多命中%": round(hit_bull * 100, 2),
        "看空命中%": round(hit_bear * 100, 2),
        "看多 lift": round(hit_bull / base_pos, 3),
        "看空 lift": round(hit_bear / (1 - base_pos), 3),
        "多空價差%/日": round(sp.mean() * 100, 4),
        "t": round(_tstat(sp), 2),
        "n日": len(sp),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--panel", type=Path, required=True, help="build_score 後的 pickle")
    ap.add_argument("--ret", default="fwd_ex", help="被解釋變數欄位（隔日超額報酬）")
    ap.add_argument("--train", type=int, default=250, help="訓練窗（交易日）")
    ap.add_argument("--step", type=int, default=25, help="每次前進幾天")
    ap.add_argument("--q", type=float, default=0.25, help="三帶切點分位")
    args = ap.parse_args()

    d = pd.read_pickle(args.panel)
    if args.ret not in d.columns:
        raise SystemExit(f"面板缺少 {args.ret} 欄")
    print(f"面板：{len(d):,} 個 stock-day · {d.stock_id.nunique():,} 檔 · "
          f"{d.trade_date.nunique():,} 日 · {d.trade_date.min()}~{d.trade_date.max()}")
    print(f"walk-forward：訓練窗 {args.train} 日 · 每次前進 {args.step} 日\n")

    oos, w = walk_forward(d, args.ret, args.train, args.step)
    if oos.empty:
        raise SystemExit("樣本不足以做 walk-forward")
    oos["eq"] = oos[SIGNALS].sum(axis=1) * -1        # 等權：淨多分（越大越看多）
    oos["band_w"] = bands(oos, "pred", args.q)
    oos["band_e"] = bands(oos, "eq", args.q)

    base_pos = (oos[args.ret] > 0).mean()
    print(f"OOS 期間無條件基準：超額為正 {base_pos * 100:.2f}% / 為負 {(1 - base_pos) * 100:.2f}%")
    print(f"OOS 樣本 {len(oos):,} 個 stock-day · {oos.trade_date.nunique()} 日 · "
          f"{w.shape[0]} 個 fold\n")
    res = pd.DataFrame([
        evaluate(oos, "band_e", args.ret, "等權（現況）"),
        evaluate(oos, "band_w", args.ret, "方向別權重（walk-forward）"),
    ])
    print(res.to_string(index=False))

    print("\n=== 擬合出來的權重（各 fold 平均，單位：隔日超額報酬 %）===")
    cols = [c for c in w.columns if c.endswith(("_bear", "_bull"))]
    m = (w[cols].mean() * 100).round(4).rename("平均係數")
    sd = (w[cols].std() * 100).round(4).rename("跨 fold 標準差")
    tbl = pd.concat([m, sd], axis=1)
    tbl["穩定性 |mean|/sd"] = (tbl.平均係數.abs() / tbl["跨 fold 標準差"]).round(2)
    name = {"S1": "Δ借券賣出餘額", "S2": "佔股本分位", "S3": "券源使用率變化",
            "S4": "融券餘額變化", "S5": "借券費率 vs 20日中位"}
    tbl.index = [f"{name[i.split('_')[0]]}·{'空' if i.endswith('bear') else '多'}"
                 for i in tbl.index]
    print(tbl.sort_values("平均係數").to_string())
    return 0


if __name__ == "__main__":
    sys.exit(main())
