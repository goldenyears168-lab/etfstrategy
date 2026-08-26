#!/usr/bin/env python3
"""反向工程分點的選股條件 —— 用籌碼因子還原「他們會重倉什麼」。

問題定義：給定訊號日 T 之前可得的籌碼因子，能不能預測某分點在
T~T+4 這五天累計淨買 ≥ 門檻？若能，迴歸係數本身就是他們的選股公式。

⚠️ 這是在還原**選股條件**不是**進出時機**。2026-08-26 已驗證 9661 的
單日淨額對隔日／5 日報酬皆無預測力（t=−0.81/−0.02），所以就算還原度高，
也不代表跟著做會賺。

方法：
· 目標 = 5 日累計淨買金額 ≥ TH（二元）
· 特徵 = chip_lab 的 23 個因子，統一 basis='xs'（當日橫斷面百分位）
· **walk-forward logistic regression**：每季重估，只用過去資料
· 還原吻合度用三個指標一起看，單看 AUC 會被類別不平衡騙：
    AUC          排序能力
    Top-N 捕獲率  模型分數前 N 名裡，真實事件佔了他們全部事件的幾成
    Lift         捕獲率 ÷ 基準率（隨機猜的話等於 1）
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from importlib.machinery import SourceFileLoader

LAB = SourceFileLoader("lab", str(Path(__file__).resolve().parent / "chip_lab.py")).load_module()
from stock_db import connect_ro   # noqa: E402


def branch_amounts(trader: str, start: str) -> pd.DataFrame:
    """該分點逐日淨額（元）。索引 (securities_trader_id, trade_date) 走得動。"""
    c = connect_ro()
    br = pd.read_sql_query(
        """SELECT trade_date, stock_id, buy, sell, net
             FROM stock_broker_branch_daily
            WHERE securities_trader_id=? AND trade_date>=?""", c, params=(trader, start))
    px = pd.read_sql_query(
        """SELECT stock_id, trade_date, source, close FROM stock_daily_bars
            WHERE trade_date>=? AND close>0""", c, params=(start,))
    px["rk"] = px.source.map({"finmind": 0, "twse_mi_index": 1, "tpex_daily": 2}).fillna(9)
    px = (px.sort_values("rk").drop_duplicates(["stock_id", "trade_date"])
            .drop(columns=["rk", "source"]))
    d = br.merge(px, on=["stock_id", "trade_date"], how="inner")
    for c_ in ("buy", "sell", "net"):
        d[f"{c_}_amt"] = d[c_] * d.close
    return d[["stock_id", "trade_date", "buy_amt", "sell_amt", "net_amt"]]


def rolling_forward(d: pd.DataFrame, k: int) -> pd.DataFrame:
    """T~T+k-1 的累計淨額（含當日）。必須是連續交易日。"""
    dates = np.sort(d.trade_date.unique())
    idx = {t: i for i, t in enumerate(dates)}
    d = d.sort_values(["stock_id", "trade_date"]).copy()
    d["_i"] = d.trade_date.map(idx)
    # 展開成 stock × date 的完整格子（缺的日子淨額 = 0，代表當天沒進出）
    full = (d.set_index(["stock_id", "trade_date"]).net_amt
              .unstack(fill_value=0.0).reindex(columns=dates, fill_value=0.0))
    fwd = full.rolling(k, axis=1, min_periods=k).sum().shift(-(k - 1), axis=1)
    out = fwd.stack().rename(f"net{k}").reset_index()
    out.columns = ["stock_id", "trade_date", f"net{k}"]
    return out


def wf_logit(X: np.ndarray, y: np.ndarray, grp: np.ndarray, *,
             refit_every: int = 60, warm: int = 250) -> tuple[np.ndarray, pd.DataFrame]:
    """walk-forward logistic regression（自寫 IRLS，避免 sklearn 依賴）。

    每 refit_every 日用**截至前一日**的資料重估係數，只對之後的日子預測。
    """
    ud = np.unique(grp)
    pred = np.full(len(y), np.nan)
    coefs = []
    for start in range(warm, len(ud), refit_every):
        cut = ud[start]
        tr = grp < cut
        te = (grp >= cut) & (grp < ud[min(start + refit_every, len(ud) - 1)])
        if tr.sum() < 5000 or y[tr].sum() < 50 or te.sum() == 0:
            continue
        Xt, yt = X[tr], y[tr]
        w = np.zeros(Xt.shape[1])
        for _ in range(30):                       # IRLS + 微量 L2 避免分離
            p = 1 / (1 + np.exp(-np.clip(Xt @ w, -30, 30)))
            W = np.clip(p * (1 - p), 1e-6, None)
            z = Xt @ w + (yt - p) / W
            A = Xt.T @ (Xt * W[:, None]) + 1e-3 * np.eye(Xt.shape[1])
            w_new = np.linalg.solve(A, Xt.T @ (z * W))
            if np.max(np.abs(w_new - w)) < 1e-6:
                w = w_new
                break
            w = w_new
        pred[te] = 1 / (1 + np.exp(-np.clip(X[te] @ w, -30, 30)))
        coefs.append({"from": cut, **{f"b{i}": v for i, v in enumerate(w)}})
    return pred, pd.DataFrame(coefs)


def auc(y: np.ndarray, s: np.ndarray) -> float:
    m = ~np.isnan(s)
    y, s = y[m], s[m]
    if y.sum() == 0 or y.sum() == len(y):
        return np.nan
    r = pd.Series(s).rank().to_numpy()
    n1, n0 = y.sum(), len(y) - y.sum()
    return (r[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--trader", default="9661")
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--th", type=float, default=5e8)
    ap.add_argument("--start", default="2023-01-01")
    args = ap.parse_args()

    d = LAB.load()
    d = d[d.trade_date >= args.start]
    br = branch_amounts(args.trader, args.start)
    fwd = rolling_forward(br, args.k)
    m = d.merge(fwd, on=["stock_id", "trade_date"], how="left")
    m[f"net{args.k}"] = m[f"net{args.k}"].fillna(0.0)
    m["y_buy"] = (m[f"net{args.k}"] >= args.th).astype(int)
    m["y_sell"] = (m[f"net{args.k}"] <= -args.th).astype(int)
    print(f"分點 {args.trader}　面板 {len(m):,} stock-day · {m.trade_date.nunique()} 日")
    print(f"{args.k} 日累計淨買 ≥ {args.th/1e8:.0f} 億：{m.y_buy.sum():,} 筆 "
          f"（基準率 {m.y_buy.mean()*100:.3f}%）")
    print(f"{args.k} 日累計淨賣 ≤ −{args.th/1e8:.0f} 億：{m.y_sell.sum():,} 筆 "
          f"（基準率 {m.y_sell.mean()*100:.3f}%）\n")

    names = [n for n in LAB.FACTORS if LAB.FACTORS[n][0] in m.columns]
    F = pd.DataFrame({n: LAB.norm(m, LAB.FACTORS[n][0], "xs") for n in names})
    # 加三個描述性欄位：它們不是「籌碼」但顯然是選股條件的一部分
    for c_, lab_ in (("mcap", "市值"), ("vol60", "波動"), ("turn", "週轉")):
        F[lab_] = LAB.norm(m, c_, "xs")
    F = F.fillna(0.0)
    cols = list(F.columns)
    X = np.column_stack([np.ones(len(F)), F.to_numpy()])
    grp = m.trade_date.to_numpy()

    for tgt, lab_ in (("y_buy", "淨買"), ("y_sell", "淨賣")):
        y = m[tgt].to_numpy()
        pred, coefs = wf_logit(X, y, grp)
        a = auc(y, pred)
        ok = ~np.isnan(pred)
        base = y[ok].mean()
        print(f"=== 還原「{lab_} ≥{args.th/1e8:.0f}億／{args.k}日」===")
        print(f"  可評估 {ok.sum():,} 筆，其中真實事件 {int(y[ok].sum()):,}（基準率 {base*100:.3f}%）")
        print(f"  AUC = {a:.4f}")
        s = pd.Series(pred[ok]); yy = pd.Series(y[ok])
        for topn in (0.01, 0.05, 0.10, 0.20):
            th = s.quantile(1 - topn)
            cap = yy[s >= th].sum() / yy.sum()
            prec = yy[s >= th].mean()
            print(f"    模型分數前 {topn*100:>4.0f}%：捕獲 {cap*100:>5.1f}% 的事件　"
                  f"精確率 {prec*100:>5.2f}%　lift {cap/topn:>4.1f}×")
        if len(coefs):
            avg = coefs[[c for c in coefs.columns if c.startswith("b")]].mean()
            w = pd.Series(avg.to_numpy()[1:], index=cols).sort_values(key=abs, ascending=False)
            print(f"  還原出的公式（walk-forward {len(coefs)} 次重估的平均係數，"
                  f"特徵皆為當日橫斷面百分位 ∈[-1,1]）：")
            for n, v in w.head(12).items():
                print(f"    {n:<14}{v:>+8.3f}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
