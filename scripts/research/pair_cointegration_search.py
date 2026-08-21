#!/usr/bin/env python3
"""台股配對交易：共整合篩選（IS）→ 嚴格 OOS 驗證。

對應 topic ``pair-mean-reversion-tw`` 的 ``H-PAIR-COINT-OOS``（跑數前已登錄）。

**為什麼不能只看 IS**：1,109 檔約 61 萬組配對，靠運氣即可得到 |t|>5 的組合。
本腳本把期間硬切成兩段——**配對篩選只用 IS，績效只報 OOS**，OOS 期在篩選
階段完全未被觸碰。

**判準改用共整合而非相關**：``H-PAIR-GENERAL`` 已證偽「相關最高的配對做均值
回歸」（walk-forward 淨 t=−5.41，系統性虧損）。本次改用 Engle-Granger 殘差的
均值回歸強度與半衰期。

**流程**
1. IS 期對每組配對做 ``log(Pa) = α + β·log(Pb) + ε``，取殘差 ε
2. 由 ``Δε_t = φ·ε_{t-1} + u`` 估均值回歸速度；半衰期 = ``-ln2/ln(1+φ)``
3. 以 φ 的 t 值（≈ ADF 無漂移版）與半衰期範圍篩選，取前 N 組互斥配對
4. OOS 期用 rolling z-score（只含過去）交易，扣雙腳成本，報 OOS 統計
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROUND_TRIP = 0.001425 * 2 + 0.003


def mr_stats(y: np.ndarray, x: np.ndarray) -> tuple[float, float, float, float]:
    """回傳 (beta, phi_t, half_life, resid_sd)。y/x 為 log price。"""
    X = np.column_stack([np.ones(len(x)), x])
    b = np.linalg.lstsq(X, y, rcond=None)[0]
    e = y - X @ b
    de, el = np.diff(e), e[:-1]
    if el.std() == 0 or len(de) < 50:
        return np.nan, np.nan, np.nan, np.nan
    Z = np.column_stack([np.ones(len(el)), el])
    c = np.linalg.lstsq(Z, de, rcond=None)[0]
    r = de - Z @ c
    s2 = r @ r / (len(de) - 2)
    se = np.sqrt(s2 * np.linalg.inv(Z.T @ Z)[1, 1])
    phi = c[1]
    t = phi / se if se > 0 else np.nan
    hl = -np.log(2) / np.log1p(phi) if -1 < phi < 0 else np.nan
    return b[1], t, hl, e.std()


def screen(px_is: pd.DataFrame, *, min_t: float, hl_lo: float, hl_hi: float,
           n_pairs: int) -> pd.DataFrame:
    cols = list(px_is.columns)
    lp = np.log(px_is.to_numpy())
    rows = []
    for i in range(len(cols)):
        yi = lp[:, i]
        for j in range(i + 1, len(cols)):
            beta, t, hl, sd = mr_stats(yi, lp[:, j])
            if not np.isfinite(t) or t > -min_t:      # φ 應顯著為負
                continue
            if not np.isfinite(hl) or not (hl_lo <= hl <= hl_hi):
                continue
            rows.append((cols[i], cols[j], beta, t, hl, sd))
    df = pd.DataFrame(rows, columns=["a", "b", "beta", "phi_t", "half_life", "resid_sd"])
    if df.empty:
        return df
    df = df.sort_values("phi_t")                       # 越負越強
    used: set[str] = set()
    keep = []
    for _, r in df.iterrows():
        if r.a in used or r.b in used:
            continue
        keep.append(r)
        used.update([r.a, r.b])
        if len(keep) >= n_pairs:
            break
    return pd.DataFrame(keep).reset_index(drop=True)


def trade(px: pd.DataFrame, a: str, b: str, beta: float, dates: pd.Index, *,
          z_win: int, entry_z: float, exit_z: float, max_hold: int) -> pd.Series:
    sp = np.log(px[a]) - beta * np.log(px[b])
    z = ((sp - sp.rolling(z_win, min_periods=z_win).mean())
         / sp.rolling(z_win, min_periods=z_win).std()).shift(1)
    dsp = sp.diff()                                     # 價差的日變動 = 部位報酬
    pos, held, rows = 0.0, 0, []
    for dt in dates:
        zz = z.get(dt, np.nan)
        prev = pos
        if not np.isfinite(zz):
            pos, held = 0.0, 0
            rows.append((dt, 0.0, abs(prev) * ROUND_TRIP))
            continue
        if pos != 0.0:
            held += 1
            if abs(zz) <= exit_z or held >= max_hold:
                pos, held = 0.0, 0
        if pos == 0.0 and abs(zz) >= entry_z:
            pos, held = -np.sign(zz), 0
        rows.append((dt, pos * dsp.get(dt, 0.0), abs(pos - prev) * ROUND_TRIP))
    o = pd.DataFrame(rows, columns=["dt", "g", "c"]).set_index("dt")
    return (o.g - o.c).rename(f"{a}/{b}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--panel", type=Path, required=True)
    ap.add_argument("--is-end", default="2019-12-31")
    ap.add_argument("--min-liq", type=float, default=2e6, help="IS 期 20 日均量中位下限（股）")
    ap.add_argument("--min-cover", type=float, default=0.95,
                    help="IS/OOS 兩段各自的最低資料覆蓋率")
    ap.add_argument("--min-t", type=float, default=4.0)
    ap.add_argument("--hl-lo", type=float, default=5.0)
    ap.add_argument("--hl-hi", type=float, default=60.0)
    ap.add_argument("--n-pairs", type=int, default=20)
    ap.add_argument("--z-win", type=int, default=60)
    ap.add_argument("--entry-z", type=float, default=2.0)
    ap.add_argument("--exit-z", type=float, default=0.5)
    ap.add_argument("--max-hold", type=int, default=20)
    ap.add_argument("--hold-mult", type=float, default=None,
                    help="給定時 max_hold = hold_mult × 該配對的 IS 半衰期（取代 --max-hold）")
    args = ap.parse_args()

    d = pd.read_pickle(args.panel).dropna(subset=["close"])
    px = d.pivot_table(index="trade_date", columns="stock_id", values="close")
    liq = d.pivot_table(index="trade_date", columns="stock_id", values="vol20")
    is_px = px[px.index <= args.is_end]
    oos_dates = px.index[px.index > args.is_end]
    oos_px = px[px.index > args.is_end]
    ok = ((is_px.notna().mean() >= args.min_cover)
          & (oos_px.notna().mean() >= args.min_cover)
          & (liq[liq.index <= args.is_end].median() >= args.min_liq))
    cols = [c for c in px.columns if bool(ok.get(c, False))]
    # 少量缺值用前值補（配對交易的價差需要連續序列）
    px = px[cols].ffill()
    is_px = px[px.index <= args.is_end]
    print(f"IS {is_px.index.min()}~{args.is_end}（{len(is_px):,} 日）· "
          f"OOS {oos_dates.min()}~{oos_dates.max()}（{len(oos_dates):,} 日）")
    print(f"通過完整歷史＋流動性篩選：{len(cols):,} 檔 → {len(cols)*(len(cols)-1)//2:,} 組配對\n")

    sel = screen(is_px.dropna(axis=1), min_t=args.min_t, hl_lo=args.hl_lo,
                 hl_hi=args.hl_hi, n_pairs=args.n_pairs)
    if sel.empty:
        print("IS 期無配對通過篩選")
        return 0
    print(f"IS 選出 {len(sel)} 組（判準：φ 的 t <= -{args.min_t}、"
          f"半衰期 {args.hl_lo}~{args.hl_hi} 日、互斥）")
    print("⚠️ 以下 IS 統計量僅供追溯，不得引用為績效\n")
    print(sel.assign(phi_t=sel.phi_t.round(2), half_life=sel.half_life.round(1),
                     beta=sel.beta.round(3)).head(20).to_string(index=False))

    legs = []
    for _, r in sel.iterrows():
        mh = (int(round(args.hold_mult * r.half_life)) if args.hold_mult
              else args.max_hold)
        legs.append(trade(px, r.a, r.b, r.beta, oos_dates, z_win=args.z_win,
                          entry_z=args.entry_z, exit_z=args.exit_z, max_hold=max(2, mh)))
    port = pd.concat(legs, axis=1)
    eq = port.mean(axis=1).dropna()
    t = eq.mean() / (eq.std(ddof=1) / np.sqrt(len(eq)))
    print(f"\n{'='*72}\n【OOS 組合績效】{len(eq):,} 交易日 · {len(sel)} 組等權\n{'='*72}")
    print(f"  日均 {eq.mean()*100:+.4f}% · 年化 {eq.mean()*252*100:+.2f}% · "
          f"t = {t:+.2f} · Sharpe {eq.mean()/eq.std()*np.sqrt(252):+.2f}")
    print(f"  門檻 |t| >= 3.0 → {'通過' if abs(t)>=3 else '未通過'}")
    per = pd.DataFrame({"pair": [c for c in port.columns],
                        "OOS年化%": [port[c].dropna().mean()*252*100 for c in port.columns],
                        "OOS_t": [port[c].dropna().mean()/(port[c].dropna().std(ddof=1)/np.sqrt(port[c].dropna().size)) for c in port.columns]})
    print(f"\n  單組 OOS 為正的比例：{(per['OOS年化%']>0).mean()*100:.0f}%"
          f"（{(per['OOS年化%']>0).sum()}/{len(per)}）")
    print(per.round(2).sort_values("OOS_t", ascending=False).to_string(index=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
