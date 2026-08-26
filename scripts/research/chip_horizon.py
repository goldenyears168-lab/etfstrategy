#!/usr/bin/env python3
"""多天期籌碼訊號 —— 把持有期從 T+1 拉長，成本攤提後參數重配。

**動機**：2026-08-26 的 2,314 組態實驗證明 corr(淨值, 換手) = −0.975，
換手解釋 95.1% 的淨值變異。持有 K 天等於把一趟 0.471% 攤到 K 天，
這是「換尺度不換公式」——工作流判定唯一還沒被堵死的方向。

**參數必須重配**：T+5 的 IC 結構與 T+1 不同（快變數的訊號 1 天就衰減完，
慢變數要 5~20 天才展開），沿用 T+1 的權重等於用錯的先驗。

## 口徑
· 進場 open(T+1)、出場 close(T+K)，必須是 K 個連續交易日
· 中性化：波動/跳空/市值/週轉率 五分位虛擬變數 + 波動×跳空交互，
  逐日對 K 日報酬迴歸取殘差
· 成本：每趟 0.471%（證交稅 0.3% + 手續費 6 折），依實際換手計
· **重疊窗**：每日建倉、持有 K 日 → 報酬重疊，t 值一律按建倉日聚類
  並做 Newey-West(lag=K) 校正，否則會嚴重高估
· 換手：每 K 日換一次倉，日均換手 = 單次換手 / K
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
COST = 0.471
FR = 0.058
HORIZONS = (1, 2, 3, 5, 10, 20)


def add_horizons(d: pd.DataFrame) -> pd.DataFrame:
    """K 日報酬：open(T+1) → close(T+K)，必須是連續 K 個交易日。"""
    d = d.sort_values(["stock_id", "trade_date"]).copy()
    dates = np.sort(d.trade_date.unique())
    idx = {t: i for i, t in enumerate(dates)}
    d["_i"] = d.trade_date.map(idx)
    g = d.groupby("stock_id", group_keys=False)
    for k in HORIZONS:
        c_k = g.close.shift(-k)
        i_k = g._i.shift(-k)
        ok = (i_k - d._i) == k          # 中間沒有跳日
        r = c_k / d.nx_open - 1
        d[f"r{k}"] = r.where(ok & d.nx_open.notna())
    return d.drop(columns=["_i"])


def neutral_k(d: pd.DataFrame, k: int) -> pd.Series:
    col = f"r{k}"
    x = d.dropna(subset=[col]).copy()
    for c in LAB.CONTROLS:
        x[f"q_{c}"] = x.groupby("trade_date")[c].transform(
            lambda s: pd.qcut(s.rank(method="first"), 5, labels=False, duplicates="drop"))
    x = x.dropna(subset=[f"q_{c}" for c in LAB.CONTROLS])
    out = pd.Series(np.nan, index=d.index)
    for _, g in x.groupby("trade_date"):
        if len(g) < 120:
            continue
        P = [np.ones((len(g), 1))]
        for c in LAB.CONTROLS:
            P.append(pd.get_dummies(g[f"q_{c}"].astype(int), drop_first=True).to_numpy(float))
        P.append(pd.get_dummies(g.q_vol60.astype(int) * 5 + g.q_gap.astype(int),
                                drop_first=True).to_numpy(float))
        X = np.column_stack(P)
        y = g[col].to_numpy()
        try:
            b, *_ = np.linalg.lstsq(X, y, rcond=None)
            out.loc[g.index] = y - X @ b
        except np.linalg.LinAlgError:
            pass
    return out


def nw_t(s: pd.Series, lag: int) -> float:
    a = np.asarray(s.dropna(), float)
    n = len(a)
    if n < 20:
        return np.nan
    e = a - a.mean()
    v = (e @ e) / n
    for L in range(1, lag + 1):
        v += 2 * (1 - L / (lag + 1)) * ((e[L:] @ e[:-L]) / n)
    return a.mean() / np.sqrt(v / n) if v > 0 else np.nan


def ic_table(d: pd.DataFrame, basis: str = "xs") -> pd.DataFrame:
    """各因子在各天期的 IC —— 這是重配參數的依據。"""
    rows = []
    for name in LAB.FACTORS:
        f = LAB.signed(d, name, basis)
        if f.isna().all():
            continue
        for k in HORIZONS:
            col = f"n{k}"
            x = pd.DataFrame({"t": d.trade_date, "f": f, "y": d[col]}).dropna()
            if len(x) < 5000:
                continue
            ic = x.groupby("t").apply(lambda g: g.f.corr(g.y, method="spearman"),
                                      include_groups=False).dropna()
            if len(ic) < 100:
                continue
            rows.append({"factor": name, "k": k, "ic": ic.mean(),
                         "t_nw": nw_t(ic, k), "pos": (ic > 0).mean() * 100, "n": len(ic)})
    return pd.DataFrame(rows)


def evaluate_k(d: pd.DataFrame, score: pd.Series, k: int, *,
               form: int = 250) -> dict:
    """每日建倉、持有 K 日。換手按每 K 日換一次計，成本攤到 K 天。"""
    col = f"n{k}"
    x = d.assign(_s=score).dropna(subset=["_s", col]).copy()
    dates = np.sort(x.trade_date.unique())
    if len(dates) < form + 60:
        return {"error": "樣本不足"}
    oos = set(dates[form:])
    # ⚠️ 換手必須量「K 天前的名單 vs 今天」，不能量 1 天再除以 K。
    # 持有 K 天的組合每天替換 1/K 部位，換掉的是 K 天前那批；
    # K 天的名單變動一定大於 1 天，除以 K 會系統性低估成本。
    rows, hist = [], []
    for t, g in x.groupby("trade_date", sort=True):
        if len(g) < 120:
            continue
        n = max(3, int(round(len(g) * FR)))
        q = g.sort_values("_s", ascending=False)
        L = set(q.stock_id.head(n))
        turn_k = len(L - hist[-k]) / n if len(hist) >= k else np.nan
        hist.append(L)
        if t in oos:
            long_r = q[col].head(n)
            rows.append({"trade_date": t, "long": long_r.mean(),
                         "short": -q[col].tail(n).mean(),
                         "hit": (long_r > 0).mean(),
                         "turn": turn_k})
    r = pd.DataFrame(rows)
    if len(r) < 60:
        return {"error": "OOS 不足"}
    gl = r.long.mean() * 100
    # 重疊窗：t 用 NW(lag=k) 校正
    t_nw = nw_t(r.long, k)
    # turn 已是「K 天名單變動」；重疊組合每天換 1/K 的部位 → 日均換手 = turn/K
    tau_daily = r.turn.mean() / k
    net_day = gl / k - tau_daily * COST     # gross 也攤到每日
    return {"n_days": len(r), "hit": r.hit.mean() * 100,
            "gross_trade": gl, "gross_day": gl / k, "t_nw": t_nw,
            "turn_rebal": r.turn.mean() * 100, "turn_daily": tau_daily * 100,
            "net_day": net_day, "net_ann": net_day * 242,
            "breakeven": (gl / k) / tau_daily if tau_daily > 0 else np.nan}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--basis", default="xs")
    args = ap.parse_args()
    d = LAB.load()
    d = add_horizons(d)
    print(f"面板 {len(d):,} · {d.trade_date.nunique()} 日", flush=True)
    print("計算各天期中性化殘差…", flush=True)
    for k in HORIZONS:
        d[f"n{k}"] = neutral_k(d, k)
        print(f"  K={k} 完成（{d[f'n{k}'].notna().sum():,} 列）", flush=True)
    d.to_pickle(LAB.DIR / "chip_horizon_panel.pkl")
    ic = ic_table(d, args.basis)
    ic.to_csv(LAB.DIR / "horizon_ic.csv", index=False)
    print("\n=== 各因子 IC × 天期（basis=xs，NW 校正）===")
    p = ic.pivot(index="factor", columns="k", values="ic")
    tt = ic.pivot(index="factor", columns="k", values="t_nw")
    print(f"{'因子':<12}" + "".join(f"{'K='+str(k):>16}" for k in HORIZONS))
    for f in p.index:
        line = f"{f:<12}"
        for k in HORIZONS:
            v = p.loc[f, k] if k in p.columns else np.nan
            t = tt.loc[f, k] if k in tt.columns else np.nan
            line += f"{v:>+9.4f}{t:>+7.2f}" if pd.notna(v) else " " * 16
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
