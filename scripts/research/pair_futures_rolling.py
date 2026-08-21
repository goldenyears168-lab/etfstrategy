#!/usr/bin/env python3
"""個股期貨可交易標的的配對均值回歸 —— 近期形成期 + 滾動月度 OOS。

對應 topic ``pair-mean-reversion-tw``。相對前一支 `pair_cointegration_search.py`
的三個改動，每個都有事前理由：

1. **宇宙換成個股期貨標的**（267 檔對到股票代號）。前面已證實真正的瓶頸是
   交易成本 vs 半衰期不匹配（半衰期中位 78.8 日、現股雙腳成本 1.17%/次），
   而個股期貨雙腳成本約 **0.068%**，便宜約 17 倍——直接攻擊瓶頸本身。
2. **形成期改用近期滾動窗**（預設 trailing 6 個月）。使用者指出配對關係會
   隨月份變化，用 2011-2019 選、2020-2026 驗證等於假設關係 7 年不變。
3. **驗證改成滾動月度累積**。單月 20~40 日無法區分訊號與雜訊（要在 40 天內
   達到 t=3.0，日均需為日波動的 0.47 倍），故把每個月的 OOS 串起來累積樣本
   ——既不假設舊關係還在，也不被單月雜訊騙。

⚠️ 成本用期貨口徑，**不含滑價與流動性衝擊**。個股期貨深度遠淺於現股，實際
可執行性需另外驗證（本腳本不處理）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

FUT_ROUND_TRIP = 0.00034          # 單腳來回（手續費+期交稅，20 萬名目估）


def mr_stats(y: np.ndarray, x: np.ndarray) -> tuple[float, float, float]:
    X = np.column_stack([np.ones(len(x)), x])
    b = np.linalg.lstsq(X, y, rcond=None)[0]
    e = y - X @ b
    de, el = np.diff(e), e[:-1]
    if len(de) < 30 or el.std() == 0:
        return np.nan, np.nan, np.nan
    Z = np.column_stack([np.ones(len(el)), el])
    c = np.linalg.lstsq(Z, de, rcond=None)[0]
    r = de - Z @ c
    se = np.sqrt((r @ r / (len(de) - 2)) * np.linalg.inv(Z.T @ Z)[1, 1])
    phi = c[1]
    t = phi / se if se > 0 else np.nan
    hl = -np.log(2) / np.log1p(phi) if -1 < phi < 0 else np.nan
    return b[1], t, hl


def select(lp: np.ndarray, names: list[str], *, min_t: float, hl_lo: float,
           hl_hi: float, n_pairs: int) -> list[tuple[str, str, float, float]]:
    cand = []
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            beta, t, hl = mr_stats(lp[:, i], lp[:, j])
            if np.isfinite(t) and t <= -min_t and np.isfinite(hl) and hl_lo <= hl <= hl_hi:
                cand.append((t, names[i], names[j], beta, hl))
    cand.sort()
    used, out = set(), []
    for t, a, b, beta, hl in cand:
        if a in used or b in used:
            continue
        out.append((a, b, beta, hl))
        used.update([a, b])
        if len(out) >= n_pairs:
            break
    return out


def trade(px: pd.DataFrame, a: str, b: str, beta: float, dates,
          *, z_win: int, entry_z: float, exit_z: float, max_hold: int,
          cost: float) -> pd.Series:
    sp = np.log(px[a]) - beta * np.log(px[b])
    z = ((sp - sp.rolling(z_win, min_periods=z_win).mean())
         / sp.rolling(z_win, min_periods=z_win).std()).shift(1)
    dsp = sp.diff()
    pos, held, rows = 0.0, 0, []
    for dt in dates:
        zz = z.get(dt, np.nan)
        prev = pos
        if not np.isfinite(zz):
            pos, held = 0.0, 0
            rows.append((dt, 0.0, abs(prev) * cost))
            continue
        if pos != 0.0:
            held += 1
            if abs(zz) <= exit_z or held >= max_hold:
                pos, held = 0.0, 0
        if pos == 0.0 and abs(zz) >= entry_z:
            pos, held = -np.sign(zz), 0
        rows.append((dt, pos * dsp.get(dt, 0.0), abs(pos - prev) * cost))
    o = pd.DataFrame(rows, columns=["dt", "g", "c"]).set_index("dt")
    return (o.g - o.c).rename(f"{a}/{b}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--panel", type=Path, required=True)
    ap.add_argument("--fut-map", type=Path, default=Path("/tmp/fut2stock.json"))
    ap.add_argument("--start", default="2024-01-01")
    ap.add_argument("--form-months", type=int, default=6)
    ap.add_argument("--min-t", type=float, default=3.0)
    ap.add_argument("--hl-lo", type=float, default=2.0)
    ap.add_argument("--hl-hi", type=float, default=30.0)
    ap.add_argument("--n-pairs", type=int, default=10)
    ap.add_argument("--z-win", type=int, default=40)
    ap.add_argument("--entry-z", type=float, default=2.0)
    ap.add_argument("--exit-z", type=float, default=0.5)
    ap.add_argument("--hold-mult", type=float, default=3.0)
    ap.add_argument("--cost", type=float, default=FUT_ROUND_TRIP)
    args = ap.parse_args()

    sids = sorted(set(json.load(open(args.fut_map)).values()))
    d = pd.read_pickle(args.panel)
    d = d[d.stock_id.isin(sids) & (d.trade_date >= args.start)].dropna(subset=["close"])
    px = d.pivot_table(index="trade_date", columns="stock_id", values="close").ffill()
    px = px.dropna(axis=1, thresh=int(len(px) * 0.95))
    print(f"個股期標的 {len(sids)} 檔 → 面板可用 {px.shape[1]} 檔 · "
          f"{px.shape[0]:,} 日 · {px.index.min()}~{px.index.max()}")
    print(f"成本口徑：雙腳 {args.cost*2*100:.3f}%／次（期貨）\n")

    months = sorted({d[:7] for d in px.index})
    legs_all, sel_log = [], []
    for k in range(args.form_months, len(months)):
        form_ms = months[k - args.form_months:k]
        test_m = months[k]
        form = px[[d[:7] in form_ms for d in px.index]].dropna(axis=1)
        test_dates = [d for d in px.index if d[:7] == test_m]
        if form.shape[1] < 20 or len(form) < 80 or not test_dates:
            continue
        pairs = select(np.log(form.to_numpy()), list(form.columns),
                       min_t=args.min_t, hl_lo=args.hl_lo, hl_hi=args.hl_hi,
                       n_pairs=args.n_pairs)
        sel_log.append((test_m, len(pairs)))
        for a, b, beta, hl in pairs:
            legs_all.append(trade(px, a, b, beta, test_dates, z_win=args.z_win,
                                  entry_z=args.entry_z, exit_z=args.exit_z,
                                  max_hold=max(2, int(round(args.hold_mult * hl))),
                                  cost=args.cost))
    if not legs_all:
        print("無任何月份選出配對")
        return 0
    sl = pd.DataFrame(sel_log, columns=["月", "配對數"])
    print(f"滾動月數 {len(sl)} · 每月選出配對數 中位 {sl.配對數.median():.0f} "
          f"（0 的月份 {(sl.配對數==0).sum()} 個）")
    port = pd.concat(legs_all, axis=1)
    eq = port.groupby(level=0).mean().mean(axis=1).dropna() if port.index.duplicated().any() \
        else port.mean(axis=1).dropna()
    t = eq.mean() / (eq.std(ddof=1) / np.sqrt(len(eq)))
    print(f"\n{'='*70}\n【滾動 OOS 累積】{len(eq):,} 交易日\n{'='*70}")
    print(f"  日均 {eq.mean()*100:+.4f}% · 年化 {eq.mean()*252*100:+.2f}% · "
          f"t = {t:+.2f} · Sharpe {eq.mean()/eq.std()*np.sqrt(252):+.2f}")
    print(f"  門檻 |t| >= 3.0 → {'通過' if abs(t)>=3 else '未通過'}")
    by_m = eq.groupby(eq.index.str[:7]).mean()*252*100
    print(f"\n  逐月年化報酬（{len(by_m)} 個月，為正 {(by_m>0).sum()} 個）：")
    print("   " + "  ".join(f"{m[2:]}:{v:+.0f}%" for m, v in by_m.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
