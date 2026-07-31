"""First-pass chip-signal information-content scan on the market panel.

For each candidate signal S (value known AFTER close of day t) we measure whether
it predicts TAIEX forward returns. Entry convention: open of t+1, exit close t+h.
  fwd_h(t) = ix_close[t+h] / ix_open[t+1] - 1        (no look-ahead)

Metrics per (signal, horizon):
  IC       = Pearson corr( S[t], fwd_h[t] )   -- direction-agnostic predictive value
  rank_IC  = Spearman corr                     -- robust to outliers/nonlinearity
  reported separately for IS (first 70% of dates) and OOS (last 30%).
A signal is only interesting if IS and OOS agree in sign and OOS |IC| is non-trivial.

Also runs a naive sign-based long/short (h=1 daily) and reports annualized Sharpe
IS/OOS, purely as a sanity cross-check on the IC.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
PANEL = ROOT / "data" / "research" / "chip_macro" / "panel.parquet"
OUT = ROOT / "reports" / "research" / "chip-macro"
HORIZONS = [1, 3, 5, 10, 20]
IS_FRAC = 0.70


def rolling_z(s: pd.Series, w: int) -> pd.Series:
    m = s.rolling(w, min_periods=w).mean()
    sd = s.rolling(w, min_periods=w).std()
    return (s - m) / sd


def signed_streak(s: pd.Series) -> pd.Series:
    """Signed consecutive same-sign run length (uses only current & past)."""
    sign = np.sign(s.fillna(0.0).values)
    out = np.zeros(len(sign))
    run = 0
    prev = 0.0
    for i, v in enumerate(sign):
        if v == 0:
            run = 0
        elif v == prev:
            run += 1
        else:
            run = 1
        out[i] = run * v
        prev = v
    return pd.Series(out, index=s.index)


def build_signals(p: pd.DataFrame) -> dict[str, pd.Series]:
    sig: dict[str, pd.Series] = {}
    # --- raw levels / flows (known at t) ---
    sig["foreign_cash"] = p["foreign"]
    sig["trust_cash"] = p["trust"]
    sig["dealer_cash"] = p["dealer"]
    sig["inst_total_cash"] = p["inst_total"]
    sig["fut_foreign_oi"] = p["fut_foreign_oi"]
    sig["fut_trust_oi"] = p["fut_trust_oi"]
    sig["fut_dealer_oi"] = p["fut_dealer_oi"]
    sig["fut_foreign_doi"] = p["fut_foreign_doi"]
    sig["fut_trust_doi"] = p["fut_trust_doi"]
    # --- smoothed flows ---
    sig["foreign_cash_ma5"] = p["foreign"].rolling(5, min_periods=5).mean()
    sig["foreign_cash_sum5"] = p["foreign"].rolling(5, min_periods=5).sum()
    sig["inst_total_ma5"] = p["inst_total"].rolling(5, min_periods=5).mean()
    # --- OI level change over 5d (accumulation trend) ---
    sig["fut_foreign_oi_chg5"] = p["fut_foreign_oi"].diff(5)
    # --- z-scores (mean-reversion candidates) ---
    sig["foreign_cash_z20"] = rolling_z(p["foreign"], 20)
    sig["inst_total_z20"] = rolling_z(p["inst_total"], 20)
    sig["fut_foreign_oi_z60"] = rolling_z(p["fut_foreign_oi"], 60)
    sig["fut_foreign_oi_z120"] = rolling_z(p["fut_foreign_oi"], 120)
    # --- streaks ---
    sig["foreign_streak"] = signed_streak(p["foreign"])
    sig["trust_streak"] = signed_streak(p["trust"])
    sig["fut_foreign_doi_streak"] = signed_streak(p["fut_foreign_doi"])
    # --- divergence: cash vs futures-change direction ---
    sig["div_cash_vs_futdoi"] = np.sign(p["foreign"].fillna(0)) * np.sign(
        p["fut_foreign_doi"].fillna(0)
    )
    # confirmation: cash sign * futures-OI sign (both aligned -> stronger)
    sig["confirm_cash_futoi"] = np.sign(p["foreign"].fillna(0)) * (
        p["foreign"].abs() * (np.sign(p["foreign"].fillna(0)) == np.sign(p["fut_foreign_oi"].fillna(0)))
    )
    return sig


def ic_row(s: pd.Series, fwd: pd.Series, is_mask: pd.Series):
    d = pd.DataFrame({"s": s, "f": fwd}).dropna()
    if len(d) < 30:
        return None
    ism = is_mask.reindex(d.index).fillna(False)
    out = {}
    for tag, sub in (("all", d), ("IS", d[ism]), ("OOS", d[~ism])):
        if len(sub) < 20:
            out[tag] = (np.nan, np.nan, len(sub))
            continue
        pear = sub["s"].corr(sub["f"])
        spear = sub["s"].corr(sub["f"], method="spearman")
        out[tag] = (pear, spear, len(sub))
    return out


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    p = pd.read_parquet(PANEL).sort_values("date").reset_index(drop=True)
    # forward returns: enter open t+1, exit close t+h
    entry = p["ix_open"].shift(-1)
    for h in HORIZONS:
        exit_ = p["ix_close"].shift(-h)
        p[f"fwd{h}"] = exit_ / entry - 1.0
    n = len(p)
    is_cut = int(n * IS_FRAC)
    is_mask = pd.Series(p.index < is_cut, index=p.index)
    is_end_date = p.loc[is_cut, "date"]
    print(f"panel n={n}  IS<{is_end_date} ({is_cut})  OOS>= ({n - is_cut})")

    signals = build_signals(p)
    rows = []
    for name, s in signals.items():
        for h in HORIZONS:
            r = ic_row(s, p[f"fwd{h}"], is_mask)
            if r is None:
                continue
            pe_all, sp_all, n_all = r["all"]
            pe_is, sp_is, n_is = r["IS"]
            pe_oos, sp_oos, n_oos = r["OOS"]
            rows.append(
                {
                    "signal": name,
                    "h": h,
                    "IC_all": pe_all,
                    "IC_IS": pe_is,
                    "IC_OOS": pe_oos,
                    "rankIC_OOS": sp_oos,
                    "n_oos": n_oos,
                    # robust: sign-agreement between IS and OOS
                    "agree": int(np.sign(pe_is) == np.sign(pe_oos)) if pd.notna(pe_is) and pd.notna(pe_oos) else 0,
                }
            )
    res = pd.DataFrame(rows)
    # rank: sign-consistent AND meaningful OOS IC
    res["score"] = res["agree"] * res["IC_OOS"].abs()
    res = res.sort_values("score", ascending=False)

    pd.set_option("display.float_format", lambda x: f"{x:+.4f}" if isinstance(x, float) else str(x))
    top = res.head(25)
    print("\n=== TOP by (IS/OOS sign-agreement) x |OOS IC| ===")
    print(top[["signal", "h", "IC_IS", "IC_OOS", "rankIC_OOS", "agree", "n_oos"]].to_string(index=False))

    res.to_csv(OUT / "ic_scan.csv", index=False)
    print(f"\nfull table -> {OUT / 'ic_scan.csv'}  ({len(res)} rows)")

    # quick long/short sanity for the best few (h=1 daily)
    print("\n=== sign-based L/S sanity (h=1, daily) IS/OOS annualized Sharpe ===")
    fwd1 = p["fwd1"]
    for name in top["signal"].unique()[:8]:
        s = signals[name]
        pos = np.sign(s).shift(0)  # position from info at t, applied to fwd1 (t+1)
        strat = (pos * fwd1).dropna()
        m = is_mask.reindex(strat.index).fillna(False)
        def shp(x):
            return x.mean() / x.std() * np.sqrt(252) if len(x) > 20 and x.std() > 0 else np.nan
        print(f"  {name:22s} IS_Shp={shp(strat[m]):+.2f}  OOS_Shp={shp(strat[~m]):+.2f}  "
              f"OOS_mean={strat[~m].mean()*100:+.3f}%")


if __name__ == "__main__":
    main()
