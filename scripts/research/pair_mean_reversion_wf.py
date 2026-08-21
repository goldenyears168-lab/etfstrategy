#!/usr/bin/env python3
"""台股高相關配對的價差均值回歸 —— walk-forward + 扣成本檢定。

對應 `config/research.yaml` 的 topic ``pair-mean-reversion-tw``（G1 已登錄，
含 provenance 誠實標註：本主題起於一個 in-sample 觀察，非乾淨預先登錄）。

**PIT 紀律**：每個交易日 t 的 z-score 只用 ``t 以前`` 的價差資料估平均與標準差；
配對選擇（``trailing_top_corr`` 模式）也只用形成期之前的相關矩陣。訊號在 t 日
收盤產生，部位在 t+1 日建立，報酬計 t+1 日——與 chip 線同一套口徑。

**成本**：多空兩腳，每腳來回 = 手續費 0.1425%×2 ＋ 賣出證交稅 0.3% = 0.585%。
只在**部位變動**時計費，不假設每日全額換手。

**兩個模式**
* ``--pairs fixed`` 只跑 ``--fixed-pair``（預設 9914,9921）
* ``--pairs top-corr`` 每個形成期用過去 ``--form-days`` 日的報酬相關，挑出
  相關最高的 ``--n-pairs`` 組互斥配對，往前交易 ``--trade-days`` 日
"""
from __future__ import annotations

import argparse
import itertools
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROUND_TRIP = 0.001425 * 2 + 0.003          # 單腳來回 0.585%


def load_wide(panel: Path, start: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    d = pd.read_pickle(panel)
    d = d[d.trade_date >= start].dropna(subset=["ret", "close"])
    ret = d.pivot_table(index="trade_date", columns="stock_id", values="ret")
    px = d.pivot_table(index="trade_date", columns="stock_id", values="close")
    return ret, px


def pick_pairs(ret: pd.DataFrame, n_pairs: int, min_obs: int = 200) -> list[tuple[str, str]]:
    """形成期報酬相關最高、且彼此不重複用到同一檔的 n 組配對。"""
    r = ret.dropna(axis=1, thresh=min_obs)
    if r.shape[1] < 4:
        return []
    c = r.corr()
    np.fill_diagonal(c.values, np.nan)
    # corr() 兩軸同名（stock_id），直接 reset_index 會撞名
    c.index.name, c.columns.name = "a", "b"
    cand = c.stack().rename("rho").reset_index()
    cand = cand[cand.a < cand.b].sort_values("rho", ascending=False)
    used: set[str] = set()
    out: list[tuple[str, str]] = []
    for _, row in cand.iterrows():
        if row.a in used or row.b in used:
            continue
        out.append((row.a, row.b))
        used.update([row.a, row.b])
        if len(out) >= n_pairs:
            break
    return out


def trade_pair(ret: pd.DataFrame, a: str, b: str, dates: pd.Index, *,
               z_win: int, entry_z: float, exit_z: float, max_hold: int) -> pd.Series:
    """回傳該配對在 dates 上的每日淨報酬（已扣成本）。

    價差定義為 ``log(1+ret_a) - log(1+ret_b)`` 的累積，z-score 用 rolling window
    （只含過去）。z 為正代表 a 相對強 → 空 a 多 b（押回歸）。
    """
    if a not in ret.columns or b not in ret.columns:
        return pd.Series(dtype=float)
    sp = np.log1p(ret[a].clip(-0.5, 0.5)) - np.log1p(ret[b].clip(-0.5, 0.5))
    cum = sp.cumsum()
    mu = cum.rolling(z_win, min_periods=z_win).mean()
    sd = cum.rolling(z_win, min_periods=z_win).std()
    z = ((cum - mu) / sd).shift(1)          # 只用 t-1 以前 → t 日可用

    pos = 0.0
    held = 0
    rows = []
    for dt in dates:
        if dt not in z.index or not np.isfinite(z.get(dt, np.nan)):
            rows.append((dt, 0.0, 0.0))
            pos, held = 0.0, 0
            continue
        zz = z.loc[dt]
        prev = pos
        if pos != 0.0:
            held += 1
            if abs(zz) <= exit_z or held >= max_hold or np.sign(zz) != -np.sign(pos):
                pos, held = 0.0, 0
        if pos == 0.0 and abs(zz) >= entry_z:
            pos = -np.sign(zz)              # z 高（a 強）→ 空 a 多 b → pos=-1
            held = 0
        # 當日報酬：pos=+1 表示 多a空b
        r = pos * sp.get(dt, 0.0)
        cost = abs(pos - prev) * ROUND_TRIP  # 兩腳一起變動，abs 差即換手比例
        rows.append((dt, r, cost))
    out = pd.DataFrame(rows, columns=["dt", "gross", "cost"]).set_index("dt")
    return (out.gross - out.cost).rename(f"{a}/{b}")


def walk_forward(ret: pd.DataFrame, *, mode: str, fixed_pair: tuple[str, str],
                 form_days: int, trade_days: int, n_pairs: int,
                 z_win: int, entry_z: float, exit_z: float, max_hold: int) -> pd.Series:
    dates = ret.index
    i = max(form_days, z_win)
    chunks = []
    while i < len(dates):
        form = ret.iloc[max(0, i - form_days):i]
        seg = dates[i:i + trade_days]
        pairs = [fixed_pair] if mode == "fixed" else pick_pairs(form, n_pairs)
        if pairs:
            legs = [trade_pair(ret.iloc[:i + trade_days], a, b, seg,
                               z_win=z_win, entry_z=entry_z, exit_z=exit_z,
                               max_hold=max_hold) for a, b in pairs]
            legs = [x for x in legs if not x.empty]
            if legs:
                chunks.append(pd.concat(legs, axis=1).mean(axis=1))
        i += trade_days
    return pd.concat(chunks).sort_index() if chunks else pd.Series(dtype=float)


def summarize(s: pd.Series, label: str) -> dict:
    s = s.dropna()
    if len(s) < 60:
        return {}
    t = s.mean() / (s.std(ddof=1) / np.sqrt(len(s)))
    return {"設定": label, "日均%": round(s.mean() * 100, 4),
            "年化%": round(s.mean() * 252 * 100, 2),
            "t": round(t, 2), "Sharpe": round(s.mean() / s.std() * np.sqrt(252), 2),
            "勝率%": round((s > 0).mean() * 100, 1),
            "交易日": len(s)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--panel", type=Path, required=True)
    ap.add_argument("--start", default="2011-01-01")
    ap.add_argument("--pairs", choices=["fixed", "top-corr", "both"], default="both")
    ap.add_argument("--fixed-pair", default="9914,9921")
    ap.add_argument("--form-days", type=int, default=250)
    ap.add_argument("--trade-days", type=int, default=60)
    ap.add_argument("--n-pairs", type=int, default=20)
    ap.add_argument("--grid", action="store_true", help="跑門檻網格")
    args = ap.parse_args()

    ret, _ = load_wide(args.panel, args.start)
    print(f"面板 {ret.shape[0]:,} 日 × {ret.shape[1]:,} 檔 · {ret.index.min()}~{ret.index.max()}")
    a, b = args.fixed_pair.split(",")
    grid = (list(itertools.product([1.0, 1.5, 2.0, 2.5], [0.0, 0.5], [3, 5, 10, 20], [60, 120]))
            if args.grid else [(1.5, 0.5, 10, 60)])
    modes = ["fixed", "top-corr"] if args.pairs == "both" else [args.pairs]

    rows = []
    for mode in modes:
        for ez, xz, mh, zw in grid:
            s = walk_forward(ret, mode=mode, fixed_pair=(a, b),
                             form_days=args.form_days, trade_days=args.trade_days,
                             n_pairs=args.n_pairs, z_win=zw, entry_z=ez,
                             exit_z=xz, max_hold=mh)
            r = summarize(s, f"{mode} z>={ez} exit<={xz} hold<={mh} win={zw}")
            if r:
                rows.append(r)
    df = pd.DataFrame(rows)
    pd.set_option("display.width", 200)
    print(df.sort_values("t", ascending=False).to_string(index=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
