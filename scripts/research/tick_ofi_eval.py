#!/usr/bin/env python3
"""OFI 先導試驗的評估 —— 與 chip_lab 同口徑，可直接跟前面 43 個因子比較。

## 兩層檢定

第一層 **IC**：逐日橫斷面 Spearman(因子, 中性化後的隔日開→收)，
              再看平均 IC 的 t（按日聚類）。先導樣本 69 檔 × 55 日，
              可偵測 |IC| > 0.032。

第二層 **中性化**：報酬先對 波動/跳空/市值/週轉率 五分位虛擬變數
              ＋ 波動×跳空交互 做逐日迴歸取殘差。
              未中性化的 IC 會被低波動溢酬與跳空回歸污染（前面已量到
              +0.334%/日 t=7.03 與 +0.278%/日 t=11.03）。

⚠️ 先導樣本每日只有 ~69 檔，中性化用五分位虛擬變數會吃掉太多自由度，
   改用連續變數迴歸（log市值、vol60、turn、gap）+ 二次項。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

sys.path.insert(0, str(Path(__file__).resolve().parent))
from importlib.machinery import SourceFileLoader

LAB = SourceFileLoader("lab", str(Path(__file__).resolve().parent / "chip_lab.py")).load_module()
P = SourceFileLoader("p", str(Path(__file__).resolve().parent / "tick_ofi_pilot.py")).load_module()


def neutral_small(g: pd.DataFrame) -> np.ndarray:
    """小橫斷面用連續變數 + 二次項，不用虛擬變數（自由度不夠）。"""
    X = [np.ones(len(g))]
    for c in ("vol60", "gap", "turn"):
        v = g[c].to_numpy(float)
        v = (v - np.nanmean(v)) / (np.nanstd(v) + 1e-12)
        X += [v, v ** 2]
    m = np.log(g.mcap.to_numpy(float))
    m = (m - m.mean()) / (m.std() + 1e-12)
    X += [m, m ** 2]
    X = np.column_stack(X)
    y = g.oc.to_numpy(float)
    ok = np.isfinite(X).all(1) & np.isfinite(y)
    out = np.full(len(g), np.nan)
    if ok.sum() < 25:
        return out
    b, *_ = np.linalg.lstsq(X[ok], y[ok], rcond=None)
    out[ok] = y[ok] - X[ok] @ b
    return out


def main() -> int:
    f = pd.read_pickle(LAB.DIR / "tick_ofi_pilot.pkl").drop_duplicates(["stock_id", "trade_date"])
    d = LAB.load()
    m = d.merge(f, on=["stock_id", "trade_date"], how="inner")
    assert not m.duplicated(["stock_id", "trade_date"]).any(), "merge 後有重複"
    print(f"配對 {len(m):,} 個 stock-day · {m.trade_date.nunique()} 日 · "
          f"每日均 {len(m)/m.trade_date.nunique():.0f} 檔\n")
    m["oc_n"] = np.concatenate([neutral_small(g) for _, g in m.groupby("trade_date", sort=True)])

    print(f"{'因子':<14}{'覆蓋':>7}{'IC(原始)':>10}{'t':>7}{'IC(中性後)':>11}{'t':>7}"
          f"{'IC>0比例':>9}{'判定':>8}")
    rows = []
    for c in P.FEATS:
        r = {"f": c}
        for y, tag in (("oc", "raw"), ("oc_n", "neu")):
            ic = (m.dropna(subset=[c, y]).groupby("trade_date")
                    .apply(lambda g: g[c].corr(g[y], method="spearman")
                           if len(g) >= 25 else np.nan, include_groups=False).dropna())
            if len(ic) < 20:
                r[f"ic_{tag}"] = r[f"t_{tag}"] = np.nan
                continue
            r[f"ic_{tag}"] = ic.mean()
            r[f"t_{tag}"] = ic.mean() / (ic.std(ddof=1) / np.sqrt(len(ic)))
            r[f"pos_{tag}"] = (ic > 0).mean() * 100
            r["n_day"] = len(ic)
        cov = m[c].notna().mean() * 100
        sig = "★" if abs(r.get("t_neu", 0) or 0) > 2 else ""
        print(f"  {c:<12}{cov:>6.0f}%{r.get('ic_raw', np.nan):>+10.4f}"
              f"{r.get('t_raw', np.nan):>+7.2f}{r.get('ic_neu', np.nan):>+11.4f}"
              f"{r.get('t_neu', np.nan):>+7.2f}{r.get('pos_neu', np.nan):>8.0f}%{sig:>8}")
        rows.append(r)
    res = pd.DataFrame(rows)
    res.to_csv(LAB.DIR / "tick_ofi_ic.csv", index=False)
    best = res.reindex(res.t_neu.abs().sort_values(ascending=False).index).iloc[0]
    print(f"\n最強：{best.f}　中性後 IC {best.ic_neu:+.4f}　t={best.t_neu:+.2f}"
          f"（{int(best.n_day)} 日）")
    print(f"先導靈敏度：可偵測 |IC| > {1/np.sqrt(len(m)/m.trade_date.nunique())/np.sqrt(res.n_day.max())*2:.4f}")
    print("\n【判準】若最強因子中性後 |t| < 2 → 不值得投 25 小時做完整面板。")
    print("【對照】前面測過的最強因子：sbl_pct K=5 中性後 t=+3.95（但那是 848 日）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
