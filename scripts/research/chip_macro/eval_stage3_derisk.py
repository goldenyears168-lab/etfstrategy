"""Stage-3: de-risk the champion signal  fut_foreign_oi_z60>0 [L/F].

Five independent stress tests:
  A. Sub-period robustness (halves, thirds) + reverse split (train-late / test-early).
  B. Parameter-sensitivity grid: does a WIDE plateau of (z-window, threshold) work,
     or only the hand-picked (60, 0)?  Plateau => robust; single cell => overfit.
  C. Walk-forward with rolling threshold RE-FIT (pick best trailing-Sharpe threshold,
     apply forward) -> honest out-of-sample where the hyperparameter is chosen live.
  D. Factor independence: is z60 the same factor as doi / chg5?  Correlate their
     daily strategy returns; test whether combining adds anything.
  E. Significance: permutation test vs same-exposure RANDOM timing. Does the SIGNAL's
     choice of WHICH 42% of days to be long beat random selection of 42% of days?

No look-ahead: position at t (info known after close t) applied to open(t+1)->close(t+1).
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
PANEL = ROOT / "data" / "research" / "chip_macro" / "panel.parquet"
OUT = ROOT / "reports" / "research" / "chip-macro"
COST_BPS = 4.0
ANN = np.sqrt(252)
RNG = np.random.default_rng(12345)


def rolling_z(s, w):
    return (s - s.rolling(w, min_periods=w).mean()) / s.rolling(w, min_periods=w).std()


def sharpe(x):
    x = pd.Series(x).dropna()
    return x.mean() / x.std() * ANN if len(x) > 20 and x.std() > 0 else np.nan


def lf_returns(bull: pd.Series, day_ret: pd.Series, cost_bps=COST_BPS) -> pd.Series:
    pos = bull.astype(float).fillna(0.0)
    cost = pos.diff().abs().fillna(pos.abs()) * (cost_bps / 1e4)
    return pos * day_ret - cost


def main():
    p = pd.read_parquet(PANEL).sort_values("date").reset_index(drop=True)
    day_ret = (p["ix_close"] / p["ix_open"] - 1.0).shift(-1)  # decision at t -> t+1 o->c
    bh = day_ret
    fut = p["fut_foreign_oi"]

    # ---------- A. sub-period robustness ----------
    z60 = rolling_z(fut, 60)
    strat = lf_returns(z60 > 0, day_ret)
    print("=== A. sub-period Sharpe (z60>0 L/F vs B&H) ===")
    idx = p.index
    blocks = {
        "H1 (early half)": idx < len(p) // 2,
        "H2 (late half)":  idx >= len(p) // 2,
        "T1 2018-2020":    p["date"] < "2021-01-01",
        "T2 2021-2023":    (p["date"] >= "2021-01-01") & (p["date"] < "2024-01-01"),
        "T3 2024-2026":    p["date"] >= "2024-01-01",
        "REVERSE-OOS 2018-2021 (train-late)": p["date"] < "2022-01-01",
    }
    for name, m in blocks.items():
        print(f"  {name:38s} strat={sharpe(strat[m]):+.2f}  B&H={sharpe(bh[m]):+.2f}")

    # ---------- B. parameter sensitivity ----------
    print("\n=== B. param grid  OOS Sharpe (window x threshold), OOS=last 30% ===")
    n = len(p); is_cut = int(n * 0.7); oos = idx >= is_cut
    wins = [40, 60, 90, 120, 180]
    thrs = [-0.5, -0.25, 0.0, 0.25, 0.5]
    hdr = "  win\\thr " + " ".join(f"{t:>6.2f}" for t in thrs)
    print(hdr)
    grid = {}
    for w in wins:
        z = rolling_z(fut, w)
        cells = []
        for t in thrs:
            s = lf_returns(z > t, day_ret)
            val = sharpe(s[oos]); grid[(w, t)] = val
            cells.append(f"{val:+6.2f}")
        print(f"  {w:>4d}   " + " ".join(cells))
    vals = np.array([v for v in grid.values() if pd.notna(v)])
    print(f"  plateau: {(vals>0.8).mean()*100:.0f}% of cells OOS Sharpe>0.8 ; "
          f"min={vals.min():+.2f} median={np.median(vals):+.2f} max={vals.max():+.2f}")

    # ---------- C. walk-forward with rolling threshold re-fit ----------
    print("\n=== C. walk-forward (504 train / 63 test, threshold re-fit live) ===")
    z = rolling_z(fut, 60)
    train, test = 504, 63
    thr_grid = np.round(np.arange(-0.75, 0.76, 0.25), 2)
    wf = pd.Series(index=p.index, dtype=float)
    chosen = []
    start = z.first_valid_index() or 0
    i = start + train
    while i < n:
        tr = slice(i - train, i)
        # pick threshold maximizing trailing net Sharpe on the training slice
        best_t, best_s = 0.0, -1e9
        for t in thr_grid:
            s = lf_returns((z > t).iloc[tr], day_ret.iloc[tr])
            sc = sharpe(s)
            if pd.notna(sc) and sc > best_s:
                best_s, best_t = sc, t
        te = slice(i, min(i + test, n))
        wf.iloc[te] = lf_returns((z > best_t).iloc[te], day_ret.iloc[te]).values
        chosen.append(best_t)
        i += test
    wf_valid = wf.dropna()
    wf_mask = wf_valid.index
    print(f"  WF concatenated OOS Sharpe = {sharpe(wf_valid):+.2f}  "
          f"(vs B&H {sharpe(bh.loc[wf_mask]):+.2f})  n={len(wf_valid)}")
    ct = pd.Series(chosen).value_counts().sort_index()
    print(f"  thresholds chosen live: {dict(ct)}  (median={np.median(chosen):+.2f})")

    # ---------- D. factor independence ----------
    print("\n=== D. factor independence (daily strat-return correlation) ===")
    s_z60 = lf_returns(z60 > 0, day_ret)
    s_doi = lf_returns(p["fut_foreign_doi"] > 0, day_ret)
    s_chg5 = lf_returns(fut.diff(5) > 0, day_ret)
    fr = pd.DataFrame({"z60": s_z60, "doi": s_doi, "chg5": s_chg5}).dropna()
    print(fr.corr().round(2).to_string())
    # combined overlays
    z60b, doib, chg5b = z60 > 0, p["fut_foreign_doi"] > 0, fut.diff(5) > 0
    combos = {
        "AND(z60,chg5)": z60b & chg5b,
        "OR(z60,chg5)":  z60b | chg5b,
        "MAJ(z60,doi,chg5)": (z60b.astype(int) + doib.astype(int) + chg5b.astype(int)) >= 2,
    }
    print("  combined OOS Sharpe:")
    print(f"    z60 alone            {sharpe(s_z60[oos]):+.2f}")
    for name, b in combos.items():
        print(f"    {name:20s} {sharpe(lf_returns(b, day_ret)[oos]):+.2f}")

    # ---------- E. permutation test vs same-exposure random timing ----------
    print("\n=== E. permutation: signal timing vs random same-exposure timing ===")
    for seg_name, mask in (("FULL", pd.Series(True, index=p.index)), ("OOS", pd.Series(oos, index=p.index))):
        dr = day_ret[mask].reset_index(drop=True)
        pos = (z60 > 0)[mask].astype(float).reset_index(drop=True)
        valid = dr.notna()
        dr, pos = dr[valid], pos[valid]
        actual = sharpe(pos.values * dr.values)
        k = int(pos.sum()); N = len(pos)
        null = np.empty(2000)
        drv = dr.values
        for j in range(2000):
            r = np.zeros(N); r[RNG.choice(N, k, replace=False)] = 1.0
            null[j] = sharpe(r * drv)
        pval = (null >= actual).mean()
        print(f"  {seg_name}: actual Sharpe={actual:+.2f}  random median={np.median(null):+.2f}  "
              f"95pct={np.quantile(null,0.95):+.2f}  p={pval:.3f}")


if __name__ == "__main__":
    main()
