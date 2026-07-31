"""融資維持率(整戶擔保維持率 / 斷頭風險溫度計)— falsification-first study.

WHAT THIS IS
------------
The 大盤融資維持率 (market margin-maintenance ratio) is the retail-leverage
"斷頭風險溫度計": maintenance = Σ(融資部位現值) / 大盤融資金額 ×100%. 130% is the
統一追繳線, ~166% the fresh-position level. Media/tools (wantgoo, cmoney, TEJ,
FinLab) track a single market line; low/falling values flag margin-call stress
and — after a flush — contrarian mean-reversion bottoms.

DATA — NOW GROUND-TRUTH (2026-07-30)
------------------------------------
This study runs on the OFFICIAL market maintenance line from FinMind
`TaiwanTotalExchangeMarginMaintenance` (整戶擔保維持率, market-level), backfilled
2018-01-02 -> 2026-07-29 (2082 rows, no gaps) and cached at
`data/research/dashboard/margin_maintenance_data.parquet`. This REPLACES the old
panel-price proxy. The panel (`margin_bal`, `ix_close`, `fut_foreign_oi`) is used
only for the collinearity controls, regime gate, and champion benchmark.

HOW TO RUN
----------
  cd /Users/jackm4/goldenstocks
  .venv/bin/python scripts/research/dashboard/margin_maintenance_study.py
  # add --refresh to re-pull FinMind (otherwise uses the cached parquet)

METHOD (aligned to chip-macro methodology)
------------------------------------------
  * Direction fixed from IN-SAMPLE IC only (no OOS peek).
  * 70/30 chronological IS/OOS split; report IC + long/flat Sharpe both windows.
  * Permutation test vs same-exposure random entries.
  * Collinearity control: residualize maintenance z60 against (a) pure price
    momentum (ix_close/MA60) and (b) raw margin balance — the project's core
    warning that "many chip signals are momentum in disguise".
  * regime-conditioning: bull = ix_close > rising MA200.
  * Champion reference & collinearity: fut_foreign_oi z60 > 0.
  * Deflated-Sharpe haircut over the ~8 signal forms searched.

lead_lag: LAGGING / COINCIDENT (contrarian only at deep tails), non-leading.
layer: L2 chip-core "retail-leverage stress" sub-node, gated by L0 regime.
"""
from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
PANEL = ROOT / "data" / "research" / "chip_macro" / "panel.parquet"
MM_CACHE = ROOT / "data" / "research" / "dashboard" / "margin_maintenance_data.parquet"
OUT = ROOT / "reports" / "research" / "dashboard-completeness"
COST_BPS = 4.0
ANN = np.sqrt(252)
RNG = np.random.default_rng(11)
FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"


def _finmind_token() -> str | None:
    env = ROOT / ".env"
    if not env.exists():
        return None
    for line in env.read_text().splitlines():
        m = re.match(r"\s*FINMIND_TOKEN\s*=\s*(.+)", line)
        if m:
            return m.group(1).strip().strip('"').strip("'")
    return None


def fetch_finmind_market_maintenance(start: str = "2018-01-01") -> pd.DataFrame:
    """FinMind 大盤整戶擔保維持率 ground-truth line.

    dataset: TaiwanTotalExchangeMarginMaintenance
    Returns columns: date, maintenance_pct.  Market-level (1 call), quota-safe.
    """
    import requests

    tok = _finmind_token()
    if not tok:
        raise RuntimeError("FINMIND_TOKEN not found in .env")
    r = requests.get(
        FINMIND_URL,
        params={"dataset": "TaiwanTotalExchangeMarginMaintenance",
                "start_date": start, "token": tok},
        timeout=60,
    )
    r.raise_for_status()
    d = r.json().get("data", [])
    if not d:
        raise RuntimeError(f"FinMind returned no rows: {r.json().get('msg')}")
    df = pd.DataFrame(d).rename(columns={"TotalExchangeMarginMaintenance": "maintenance_pct"})
    df["date"] = pd.to_datetime(df["date"])
    return df[["date", "maintenance_pct"]].sort_values("date").reset_index(drop=True)


def load_maintenance(refresh: bool = False) -> pd.DataFrame:
    if refresh or not MM_CACHE.exists():
        df = fetch_finmind_market_maintenance()
        MM_CACHE.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(MM_CACHE, index=False)
        print(f"[fetch] cached {len(df)} rows -> {MM_CACHE}")
    else:
        df = pd.read_parquet(MM_CACHE)
    return df


def rz(s: pd.Series, w: int) -> pd.Series:
    return (s - s.rolling(w, min_periods=w).mean()) / s.rolling(w, min_periods=w).std()


def sharpe(x) -> float:
    x = pd.Series(x).dropna()
    return x.mean() / x.std() * ANN if len(x) > 20 and x.std() > 0 else np.nan


def lf(bull, day_ret, cost=COST_BPS) -> pd.Series:
    pos = pd.Series(bull, index=day_ret.index).astype(float).fillna(0.0)
    return pos * day_ret - pos.diff().abs().fillna(pos.abs()) * (cost / 1e4)


def perm_p(bull, day_ret, mask, n=3000):
    dr = day_ret[mask].reset_index(drop=True)
    pos = pd.Series(bull, index=day_ret.index)[mask].astype(float).reset_index(drop=True)
    v = dr.notna()
    dr, pos = dr[v], pos[v]
    actual = sharpe(pos.values * dr.values)
    k, N = int(pos.sum()), len(pos)
    if k == 0 or k == N:
        return actual, np.nan
    drv = dr.values
    null = np.array([
        sharpe(np.isin(np.arange(N), RNG.choice(N, k, False)).astype(float) * drv)
        for _ in range(n)
    ])
    return actual, float((null >= actual).mean())


def deflated_sharpe(ret, n_trials: int):
    """Bailey/López de Prado deflated Sharpe (approx). Returns (DSR, annSR, ann_sr0)."""
    from scipy import stats as st

    x = pd.Series(ret).dropna()
    T = len(x)
    if T < 30 or x.std() == 0:
        return np.nan, np.nan, np.nan
    sr = x.mean() / x.std()
    sk = st.skew(x)
    ku = st.kurtosis(x, fisher=False)
    sr_std = np.sqrt((1 - sk * sr + (ku - 1) / 4 * sr ** 2) / (T - 1))
    emc = 0.5772
    z = st.norm.ppf(1 - 1.0 / n_trials) + emc * (
        st.norm.ppf(1 - 1.0 / (n_trials * np.e)) - st.norm.ppf(1 - 1.0 / n_trials))
    sr0 = sr_std * z
    dsr = st.norm.cdf((sr - sr0) / sr_std)
    return float(dsr), float(sr * ANN), float(sr0 * ANN)


def main(refresh: bool = False):
    p = pd.read_parquet(PANEL).sort_values("date").reset_index(drop=True)
    p["date"] = pd.to_datetime(p["date"])
    mm = load_maintenance(refresh)
    df = p.merge(mm, on="date", how="inner").sort_values("date").reset_index(drop=True)

    day_ret = (df["ix_close"] / df["ix_open"] - 1.0).shift(-1)
    n = len(df)
    is_cut = int(n * 0.7)
    ismask = pd.Series(df.index < is_cut, index=df.index)
    oos = pd.Series(df.index >= is_cut, index=df.index)

    mm_lv = df["maintenance_pct"]
    mr_z60 = rz(mm_lv, 60)
    mr_z120 = rz(mm_lv, 120)
    mom = df["ix_close"] / df["ix_close"].rolling(60, min_periods=60).mean()
    mom_z60 = rz(mom, 60)
    mb_z60 = rz(df["margin_bal"], 60)
    ma200 = df["ix_close"].rolling(200, min_periods=200).mean()
    bull_regime = (df["ix_close"] > ma200) & (ma200 > ma200.shift(20))
    champ_bull = rz(df["fut_foreign_oi"], 60) > 0
    champ_ret = lf(champ_bull, day_ret)

    print(f"GROUND-TRUTH merged n={n}  {df.date.min().date()} -> {df.date.max().date()}  IS/OOS cut@{is_cut}")
    print(f"maintenance real: median {mm_lv.median():.1f}  range [{mm_lv.min():.1f},{mm_lv.max():.1f}]")
    print(f"corr(MR_z60, priceMom_z60)   = {mr_z60.corr(mom_z60):+.3f}  (high => level is price-momentum transform)")
    print(f"corr(MR_z60, margin_bal_z60) = {mr_z60.corr(mb_z60):+.3f}")
    print(f"corr(MR_level, ix_close)     = {mm_lv.corr(df['ix_close']):+.3f}\n")

    def ic(sig, mask):
        d = pd.DataFrame({"s": sig, "r": day_ret})[mask].dropna()
        return d["s"].corr(d["r"]), len(d)

    ic_is, n_is = ic(mr_z60, ismask)
    ic_oos, n_oos = ic(mr_z60, oos)
    direction = np.sign(ic_is) if pd.notna(ic_is) and ic_is != 0 else 1.0
    ic120_is, _ = ic(mr_z120, ismask)
    ic120_oos, _ = ic(mr_z120, oos)
    print("=== maintenance IC (higher MR = healthier collateral) ===")
    print(f"  MR_z60  IC_IS = {ic_is:+.4f}(n={n_is})  IC_OOS = {ic_oos:+.4f}(n={n_oos})  dir={int(direction):+d}")
    print(f"  MR_z120 IC_IS = {ic120_is:+.4f}         IC_OOS = {ic120_oos:+.4f}")

    dd = pd.DataFrame({"mr": mr_z60, "mom": mom_z60, "r": day_ret}).dropna()
    beta = np.polyfit(dd["mom"], dd["mr"], 1)[0]
    resid = dd["mr"] - beta * dd["mom"]
    ic_raw = dd["mr"].corr(dd["r"])
    ic_res = resid.corr(dd["r"])
    dd2 = pd.DataFrame({"mr": mr_z60, "mb": mb_z60, "r": day_ret}).dropna()
    b2 = np.polyfit(dd2["mb"], dd2["mr"], 1)[0]
    ic_res_mb = (dd2["mr"] - b2 * dd2["mb"]).corr(dd2["r"])
    print("\n=== collinearity control (is the edge just price momentum?) ===")
    print(f"  IC raw MR_z60              = {ic_raw:+.4f}")
    print(f"  IC MR_z60 | price-mom out  = {ic_res:+.4f}  (shrink {100*(1-abs(ic_res)/abs(ic_raw)):.0f}%)")
    print(f"  IC MR_z60 | margin_bal out = {ic_res_mb:+.4f}  (info beyond 融資餘額)")

    directed = direction * mr_z60
    bull_sig = directed > 0
    r = lf(bull_sig, day_ret)
    bh_oos = sharpe(day_ret[oos])
    _, p_oos = perm_p(bull_sig, day_ret, oos)
    print("\n=== long/flat (direction from IS) ===")
    print(f"  IS  Sharpe = {sharpe(r[ismask]):+.2f}")
    print(f"  OOS Sharpe = {sharpe(r[oos]):+.2f}  (B&H OOS {bh_oos:+.2f}, champion OOS {sharpe(champ_ret[oos]):+.2f})")
    print(f"  OOS permutation p (vs same-exposure random) = {p_oos:.4f}")

    print("\n=== regime conditioning ===")
    for lbl, m in [("BULL", bull_regime), ("BEAR/side", ~bull_regime)]:
        rr = r[m.reindex(r.index).fillna(False)]
        print(f"  {lbl:9s} Sharpe={sharpe(rr):+.2f}  n={int(m.sum())}")

    print("\n=== contrarian tail test (flush -> bounce) ===")
    fwd10 = df["ix_close"].shift(-10) / df["ix_close"] - 1.0
    fwd20 = df["ix_close"].shift(-20) / df["ix_close"] - 1.0
    for q in (0.05, 0.10, 0.20):
        thr = mr_z60.quantile(q)
        low = mr_z60 <= thr
        print(f"  MR_z60<=p{int(q*100):02d} ({int(low.sum())}d): "
              f"fwd10 {fwd10[low].mean():+.3%} (base {fwd10.mean():+.3%}, lift {fwd10[low].mean()-fwd10.mean():+.3%}) | "
              f"fwd20 lift {fwd20[low].mean()-fwd20.mean():+.3%}")
    print("  -- absolute-level folk thresholds (fwd20) --")
    for th in (140, 150, 160):
        low = mm_lv <= th
        if low.sum() < 10:
            print(f"     MR<={th}: only {int(low.sum())}d (skip)")
            continue
        print(f"     MR<={th} ({int(low.sum())}d) fwd20 {fwd20[low].mean():+.3%} "
              f"(base {fwd20.mean():+.3%}, lift {fwd20[low].mean()-fwd20.mean():+.3%})")

    dsr, ann_sr, ann_sr0 = deflated_sharpe(r[oos], n_trials=8)
    print("\n=== Deflated-Sharpe (OOS long/flat, ~8 signal forms searched) ===")
    print(f"  DSR = {dsr:.3f}  (obs annSR {ann_sr:+.2f}, expected-max-under-null annSR {ann_sr0:+.2f}) "
          f"=> {'PASS' if dsr > 0.95 else 'FAIL'} @0.95")

    print("\n=== signal collinearity vs champion ===")
    print(f"  corr(MR_z60, fut_foreign_oi_z60) = {mr_z60.corr(rz(df['fut_foreign_oi'], 60)):+.3f}")
    print(f"  corr(long/flat pos, champ pos)   = {pd.Series(bull_sig).astype(float).corr(champ_bull.astype(float)):+.3f}")

    OUT.mkdir(parents=True, exist_ok=True)
    summ = pd.DataFrame([{
        "n": n, "start": str(df.date.min().date()), "end": str(df.date.max().date()),
        "IC_IS": ic_is, "IC_OOS": ic_oos, "corr_priceMom": mr_z60.corr(mom_z60),
        "IC_raw": ic_raw, "IC_resid_mom": ic_res, "IC_resid_marginbal": ic_res_mb,
        "OOS_Sharpe": sharpe(r[oos]), "BH_OOS": bh_oos, "champ_OOS": sharpe(champ_ret[oos]),
        "OOS_perm_p": p_oos, "DSR": dsr, "tail_p05_fwd20_lift": fwd20[mr_z60 <= mr_z60.quantile(0.05)].mean() - fwd20.mean(),
        "corr_champ_sig": mr_z60.corr(rz(df["fut_foreign_oi"], 60)),
    }])
    summ.to_csv(OUT / "margin_maintenance_metrics.csv", index=False)
    print(f"\n-> {OUT/'margin_maintenance_metrics.csv'}")


# ===========================================================================
# Remaining SCAFFOLD: per-stock "near-call breadth" (the one leading form).
# Needs a FULL margin universe (local stock_margin_daily covers only ~176 names,
# mostly stale) — NOT provided by the market-level ground-truth line above.
# ===========================================================================
def near_call_breadth(db_path: Path, call_line: float = 140.0) -> pd.DataFrame:
    """SCAFFOLD: the ONE genuinely-leading (precursor) form worth building.

    Per-stock maintenance -> share of margin market-value sitting below `call_line`
    (near the 130% 追繳線). Rising near-call breadth is the precondition for a
    forced-sell cascade. Needs full-universe per-stock maintenance
    (Σ張×close / 融資金額 per stock) from data/replica or a FinMind universe
    backfill. Evaluate against forward flush events, NOT next-day return.
    """
    raise NotImplementedError(
        "IMPLEMENTED 2026-07-30 in margin_nearcall_breadth_study.py (replica-based, "
        "~145 large/mid caps). Verdict: bearish-cascade-precursor hypothesis FALSIFIED; "
        "direction is contrarian (extreme near-call breadth = capitulation-bottom marker, "
        "fwd20 +3.5% lift @p95). Event-level, ~7 clustered crises, DSR/perm fail as "
        "systematic. Universe still large-cap-only; small-cap retail backfill outstanding."
    )


if __name__ == "__main__":
    main(refresh="--refresh" in sys.argv)
