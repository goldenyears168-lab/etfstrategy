"""Audit: how many independent trials were actually run across Stage 1-8 of the
chip-macro (fut_foreign_oi_z60) research line, and what does that imply for the
Deflated Sharpe Ratio (Bailey & Lopez de Prado 2014) of the final SYSTEM L0xL2?

Context / what this fixes:
  The existing DSR chain hardcodes N_trials by hand-carrying a running total across
  scripts: eval_deflated_sharpe.py N=110+25=135 -> eval_stage6_tail_regime.py
  N=135+11=146 -> eval_stage7_weinstein.py N=146+7=153 -> build_layered_system.py /
  finalize_cc.py N=160 (rounded up, "conservative", never derived). This script:
    1. Rebuilds an explicit registry of every distinct rule actually evaluated in
       Stage 1-8 (not just the ones that fed the hand-carried counter).
    2. PROGRAMMATICALLY detects exact-duplicate trials (same position series under
       a different label -- this happens, see findings) instead of trusting labels.
    3. Reconstructs each unique trial's daily return series and computes the
       empirical correlation matrix, then derives an effective-independent-trial
       count N_eff via the spectral participation ratio of that correlation matrix
       (a standard way to correct multiple-testing counts for correlated tests --
       Cheverud 1942/Nyholt-style; conceptually the analogue of Harvey & Liu 2015's
       "haircut Sharpe ratio" correlation adjustment applied to Bailey-Lopez de
       Prado's N term).
    4. Recomputes DSR for the final close-to-close SYSTEM L0xL2 OOS Sharpe at
       several N choices (hardcoded 160, audited raw count, audited deduped count,
       correlation-adjusted N_eff) using the SAME dsr()/var_trials machinery as
       eval_deflated_sharpe.py / finalize_cc.py, for apples-to-apples comparison.

No look-ahead is introduced: every position series below reuses the exact
info-known-at-close-t conventions of the original stage scripts.

NOTE ON DATA FILE: RESEARCH_LOG.md and every stage script point PANEL at
`panel.parquet`, but only `panel.csv` exists on disk (confirmed: reading the
.parquet path raises FileNotFoundError as of this audit). This script reads the
.csv directly. The panel has grown from 1,986 rows (logged 2026-07-29 cutoff) to
1,992 rows (through 2026-08-06) since the last recorded run, so absolute Sharpe/
DSR figures here will differ slightly (a few bp) from RESEARCH_LOG.md's 0.869 --
this audit reproduces that number to within ~0.03 using the identical formula and
N=160 as a sanity check (see OUTPUT section "reproduction check") before applying
the corrected N.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import norm

ROOT = Path(__file__).resolve().parents[3]
PANEL = ROOT / "data" / "research" / "chip_macro" / "panel.csv"
OUT = ROOT / "reports" / "research" / "chip-macro"
ANN = np.sqrt(252)
GAMMA = 0.5772156649
COST_BPS = 4.0


def rz(s: pd.Series, w: int) -> pd.Series:
    return (s - s.rolling(w, min_periods=w).mean()) / s.rolling(w, min_periods=w).std()


def signed_streak(s: pd.Series) -> pd.Series:
    sign = np.sign(s.fillna(0.0).values)
    out = np.zeros(len(sign)); run = 0; prev = 0.0
    for i, v in enumerate(sign):
        run = 0 if v == 0 else (run + 1 if v == prev else 1)
        out[i] = run * v; prev = v
    return pd.Series(out, index=s.index)


def sharpe(x: pd.Series) -> float:
    x = pd.Series(x).dropna()
    return x.mean() / x.std() * ANN if len(x) > 20 and x.std() > 0 else np.nan


def lf(pos: pd.Series, day_ret: pd.Series, cost: float = COST_BPS) -> pd.Series:
    """Open(t+1)->close(t+1) daily-rebalanced return, the canonical convention used
    throughout Stage 1-7 (before the 2026-07-29 close-to-close benchmark fix)."""
    pos = pd.Series(pos, index=day_ret.index).astype(float).fillna(0.0)
    return (pos * day_ret - pos.diff().abs().fillna(pos.abs()) * cost / 1e4)


def strat_cc(pos: pd.Series, p: pd.DataFrame, r_oc: pd.Series, r_cc: pd.Series, cost: float = COST_BPS) -> pd.Series:
    """Close-to-close (overnight-held) convention, exactly matching finalize_cc.py."""
    pos = pd.Series(pos, index=p.index).astype(float).fillna(0.0)
    pos_prev = pos.shift(1)
    fresh = (pos_prev > 0) & (pos.shift(2).fillna(0) == 0)
    base = pd.Series(np.where(fresh, r_oc, r_cc), index=p.index)
    return pos_prev * base - pos.diff().abs().fillna(pos.abs()) * cost / 1e4


def dsr(ret: pd.Series, var_trials: float, N: float) -> float:
    r = pd.Series(ret).dropna().values
    n = len(r); sr = r.mean() / r.std()
    g3 = pd.Series(r).skew(); g4 = pd.Series(r).kurtosis() + 3
    sr_star = np.sqrt(var_trials) * ((1 - GAMMA) * norm.ppf(1 - 1 / N) + GAMMA * norm.ppf(1 - 1 / (N * np.e)))
    return norm.cdf((sr - sr_star) * np.sqrt(n - 1) / np.sqrt(1 - g3 * sr + (g4 - 1) / 4 * sr ** 2))


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    p = pd.read_csv(PANEL).sort_values("date").reset_index(drop=True)
    n = len(p)
    is_cut = int(n * 0.7)
    oos = pd.Series(p.index >= is_cut, index=p.index)
    day_ret = (p["ix_close"] / p["ix_open"] - 1.0).shift(-1)  # canonical o->c, t+1
    r_cc = p["ix_close"] / p["ix_close"].shift(1) - 1.0
    r_oc = p["ix_close"] / p["ix_open"] - 1.0
    print(f"panel: n={n}  {p['date'].iloc[0]} -> {p['date'].iloc[-1]}  "
          f"(RESEARCH_LOG.md logged 1,986 rows thru 2026-07-29; live pull now has "
          f"{n} rows thru {p['date'].iloc[-1]} -- path also silently switched "
          f".parquet->.csv, see module docstring)")
    print(f"IS/OOS split: IS<{p.loc[is_cut,'date']}  OOS n={n-is_cut}\n")

    # ---------------- shared base signals ----------------
    z60 = rz(p["fut_foreign_oi"], 60)
    z120 = rz(p["fut_foreign_oi"], 120)
    chg5 = p["fut_foreign_oi"].diff(5)
    doi = p["fut_foreign_doi"]
    doi_z20 = rz(p["fut_foreign_doi"], 20)
    tstreak = signed_streak(p["trust"])
    inst = p["inst_total"]
    ma150 = p["ix_close"].rolling(150, min_periods=150).mean()
    ma200 = p["ix_close"].rolling(200, min_periods=200).mean()
    slope_up = (ma150 - ma150.shift(25)) > 0
    tsmom = (p["ix_close"] / p["ix_close"].shift(126) - 1) > 0
    bull200 = p["ix_close"] > ma200
    stage_s2 = (p["ix_close"] > ma150) & slope_up
    stage_s1 = (p["ix_close"] <= ma150) & slope_up
    vol_expand = p["ix_money"] > p["ix_money"].rolling(50, min_periods=50).mean()
    chip = z60 > 0

    # =========================================================================
    # REGISTRY: every distinct rule actually evaluated, tagged by stage/category.
    # category:
    #   screening_ic   = Stage-1 IC scan cell (signal x horizon), a univariate look,
    #                     not a costed backtest -> counted for the raw tally, not
    #                     reconstructed into a return series (see note below).
    #   backtest       = a real position rule with a computable, costed Sharpe --
    #                     these are the actual candidates that "could have won".
    #   diagnostic     = robustness/significance check of an ALREADY-selected rule
    #                     (sub-period splits, permutation tests) -- doesn't add a
    #                     new candidate to the selection pool, so NOT counted as a
    #                     trial toward N.
    #   walkforward    = honest sequential re-fit procedure; its OOS number is
    #                     already out-of-sample by construction. Counted as ONE
    #                     trial (the procedure itself), not deflated further.
    # =========================================================================
    registry: list[dict] = []

    def add(id_, stage, category, desc, pos=None):
        registry.append({"id": id_, "stage": stage, "category": category, "desc": desc, "pos": pos})

    # ---- Stage 1: IC scan (22 signals x 5 horizons = 110 screening cells) ----
    ic_signals = [
        "foreign_cash", "trust_cash", "dealer_cash", "inst_total_cash",
        "fut_foreign_oi", "fut_trust_oi", "fut_dealer_oi", "fut_foreign_doi",
        "fut_trust_doi", "foreign_cash_ma5", "foreign_cash_sum5", "inst_total_ma5",
        "fut_foreign_oi_chg5", "foreign_cash_z20", "inst_total_z20",
        "fut_foreign_oi_z60", "fut_foreign_oi_z120", "foreign_streak",
        "trust_streak", "fut_foreign_doi_streak", "div_cash_vs_futdoi",
        "confirm_cash_futoi",
    ]
    for s in ic_signals:
        for h in (1, 3, 5, 10, 20):
            add(f"s1_ic_{s}_h{h}", "1", "screening_ic", f"IC({s}, h={h})", pos=None)

    # Stage-1 also runs real sign-based L/S backtests (h=1) on the top-8 IC signals.
    # We represent this using ALL 22 base signals at h=1 (the top-8 in the original
    # script are a data-dependent subset of these 22; using all 22 makes the
    # correlation/effective-N step below a superset, i.e. conservative -- it can
    # only push N_eff DOWN relative to using just 8, never inflate it artificially).
    s1_signal_series = {
        "foreign_cash": p["foreign"], "trust_cash": p["trust"], "dealer_cash": p["dealer"],
        "inst_total_cash": p["inst_total"], "fut_foreign_oi": p["fut_foreign_oi"],
        "fut_trust_oi": p["fut_trust_oi"], "fut_dealer_oi": p["fut_dealer_oi"],
        "fut_foreign_doi": p["fut_foreign_doi"], "fut_trust_doi": p["fut_trust_doi"],
        "foreign_cash_ma5": p["foreign"].rolling(5, min_periods=5).mean(),
        "foreign_cash_sum5": p["foreign"].rolling(5, min_periods=5).sum(),
        "inst_total_ma5": p["inst_total"].rolling(5, min_periods=5).mean(),
        "fut_foreign_oi_chg5": p["fut_foreign_oi"].diff(5),
        "foreign_cash_z20": rz(p["foreign"], 20), "inst_total_z20": rz(p["inst_total"], 20),
        "fut_foreign_oi_z60": z60, "fut_foreign_oi_z120": z120,
        "foreign_streak": signed_streak(p["foreign"]), "trust_streak": tstreak,
        "fut_foreign_doi_streak": signed_streak(p["fut_foreign_doi"]),
        "div_cash_vs_futdoi": np.sign(p["foreign"].fillna(0)) * np.sign(p["fut_foreign_doi"].fillna(0)),
        "confirm_cash_futoi": np.sign(p["foreign"].fillna(0)) * (
            p["foreign"].abs() * (np.sign(p["foreign"].fillna(0)) == np.sign(p["fut_foreign_oi"].fillna(0)))),
    }
    for name, s in s1_signal_series.items():
        pos_ls = np.sign(pd.Series(s, index=p.index))
        add(f"s1_ls_{name}", "1", "backtest", f"sign({name}) L/S h=1", pos=pos_ls)

    # ---- Stage 2: 6 signals x {L/F, L/S} = 12 backtests ----
    s2_cands = {
        "fut_foreign_oi_z60>0": z60 > 0,
        "fut_foreign_oi_z120>0": z120 > 0,
        "fut_foreign_oi_chg5>0": chg5 > 0,
        "fut_foreign_doi>0": doi > 0,
        "inst_total>0": inst > 0,
        "trust_streak<=-1(contra)": tstreak <= -1,
    }
    for name, bull in s2_cands.items():
        pos_lf = bull.astype(float)
        add(f"s2_lf_{name}", "2", "backtest", f"{name} [L/F]", pos=pos_lf)
        pos_ls = pd.Series(np.where(bull, 1.0, -1.0), index=p.index)
        add(f"s2_ls_{name}", "2", "backtest", f"{name} [L/S]", pos=pos_ls)

    # ---- Stage 3.A sub-period robustness: diagnostic on the ALREADY-chosen
    #      champion (z60>0 L/F), not a new candidate. Not counted. ----
    add("s3_subperiod_diagnostic", "3A", "diagnostic", "6 sub-period Sharpe cuts of champion (no new rule)", pos=None)

    # ---- Stage 3.B parameter grid: 5 windows x 5 thresholds = 25 backtests ----
    for w in (40, 60, 90, 120, 180):
        zc = rz(p["fut_foreign_oi"], w)
        for t in (-0.5, -0.25, 0.0, 0.25, 0.5):
            add(f"s3_grid_w{w}_t{t}", "3B", "backtest", f"z{w}>{t} [L/F]", pos=(zc > t).astype(float))

    # ---- Stage 3.C walk-forward (threshold re-fit each 63d fold): ONE procedure ----
    add("s3_walkforward", "3C", "walkforward", "rolling 504/63 walk-forward, threshold re-fit live", pos=None)

    # ---- Stage 3.D factor-independence combos: 3 backtests ----
    z60b, doib, chg5b = z60 > 0, doi > 0, chg5 > 0
    add("s3_and_z60_chg5", "3D", "backtest", "AND(z60>0, chg5>0)", pos=(z60b & chg5b).astype(float))
    add("s3_or_z60_chg5", "3D", "backtest", "OR(z60>0, chg5>0)", pos=(z60b | chg5b).astype(float))
    add("s3_maj_z60_doi_chg5", "3D", "backtest", "MAJ(z60>0,doi>0,chg5>0)>=2",
        pos=((z60b.astype(int) + doib.astype(int) + chg5b.astype(int)) >= 2).astype(float))
    # Stage 3.E permutation test: significance check, not a new candidate. Not counted.
    add("s3_permutation_diagnostic", "3E", "diagnostic", "permutation p-value of champion (no new rule)", pos=None)

    # ---- Stage 4: 8 new hypotheses (direction fixed from IS IC) + 1 combo ----
    margin_bal, short_bal = p["margin_bal"], p["short_bal"]
    s4_cand_vals = {
        "margin_bal_z60": rz(margin_bal, 60),
        "margin_chg5_pct": margin_bal.pct_change(5),
        "short_bal_z60": rz(short_bal, 60),
        "shortmargin_ratio_z60": rz(short_bal / margin_bal, 60),
        "fut_trust_oi_z60": rz(p["fut_trust_oi"], 60),
        "fut_dealer_oi_z60": rz(p["fut_dealer_oi"], 60),
        "dealer_hedge_z20": rz(p["dealer_hedge"], 20),
        "cashfut_diverg": (-np.sign(p["foreign"].fillna(0))) * p["fut_foreign_doi"],
    }
    ismask = pd.Series(p.index < is_cut, index=p.index)
    s4_bulls = {}
    for name, v in s4_cand_vals.items():
        v = pd.Series(v, index=p.index)
        d = pd.DataFrame({"v": v, "f": day_ret}).dropna()
        ic_is = d[ismask.reindex(d.index).fillna(False)]["v"].corr(d[ismask.reindex(d.index).fillna(False)]["f"])
        direction = np.sign(ic_is) if pd.notna(ic_is) and ic_is != 0 else 1.0
        bull = (direction * v) > 0
        s4_bulls[name] = bull
        add(f"s4_{name}", "4", "backtest", f"{name} dir={int(direction)} [L/F]", pos=bull.astype(float))
    # 50/50 combo with champion (only the "independent" winner margin_bal_z60 was combined)
    comb = (chip.astype(float) + s4_bulls["margin_bal_z60"].astype(float)) / 2
    add("s4_champ_plus_margin_5050", "4", "backtest", "champ+margin_bal_z60 50/50", pos=comb)

    # ---- Stage 6 ----
    # EXP1 tail-conditioning descriptive 2x2 buckets (z60 level, doi z20 change):
    # non-costed conditional means, a "look" not a backtest. Not counted as a
    # trial (matches original script's own treatment -- it never fed these into N).
    add("s6_tail_bucket_diagnostic", "6-EXP1", "diagnostic",
        "2x2 conditional-mean table x2 signals (8 cells, descriptive only)", pos=None)
    tail_pos = pd.Series(0.0, index=p.index)
    tail_pos[z60 >= z60.quantile(0.8)] = 1.0
    tail_pos[z60 <= z60.quantile(0.2)] = -1.0
    add("s6_tail_static_quantile", "6-EXP1", "backtest", "tail L/S static full-sample quantile", pos=tail_pos)
    rq_hi = z60.rolling(252, min_periods=120).quantile(0.8)
    rq_lo = z60.rolling(252, min_periods=120).quantile(0.2)
    tail_pos_r = pd.Series(0.0, index=p.index)
    tail_pos_r[z60 >= rq_hi] = 1.0
    tail_pos_r[z60 <= rq_lo] = -1.0
    add("s6_tail_rolling_quantile", "6-EXP1", "backtest", "tail L/S rolling-quantile (no lookahead)", pos=tail_pos_r)

    bull = bull200
    s6_rules = {
        "z60>0 always (champ)": (z60 > 0).astype(float),
        "z60>0 ONLY in bull": ((z60 > 0) & bull).astype(float),
        "z60>0 ONLY in bear": ((z60 > 0) & ~bull).astype(float),
        "long if bull else flat (MA only)": bull.astype(float),
        "z60>0 AND bull (both filters)": ((z60 > 0) & bull).astype(float),
        "bull, but flat if z60<0 (chip veto)": (bull & (z60 > 0)).astype(float),
    }
    for name, pos in s6_rules.items():
        add(f"s6_rule_{name}", "6-EXP3", "backtest", name, pos=pos)
    s6_combos = {
        "long HIGH20 z60 only in bull": pd.Series(np.where((z60 >= z60.quantile(0.8)) & bull, 1.0, 0.0), index=p.index),
        "short LOW20 z60 only in bear": pd.Series(np.where((z60 <= z60.quantile(0.2)) & ~bull, -1.0, 0.0), index=p.index),
        "bull-long + bear-short(LOW20)": pd.Series(np.where(bull, 1.0, np.where(z60 <= z60.quantile(0.2), -1.0, 0.0)), index=p.index),
    }
    for name, pos in s6_combos.items():
        add(f"s6_combo_{name}", "6-EXP3", "backtest", name, pos=pos)

    # ---- Stage 7: 7 regime-gate comparisons ----
    s7_gates = {
        "chip alone (no gate)": chip.astype(float),
        "chip x close>MA200 (Stage-6 win)": (chip & bull200).astype(float),
        "chip x Stage2": (chip & stage_s2).astype(float),
        "chip x (Stage2 or Stage1)": (chip & (stage_s2 | stage_s1)).astype(float),
        "chip x Stage2 x vol-expand": (chip & stage_s2 & vol_expand).astype(float),
        "Stage2 only (no chip)": stage_s2.astype(float),
        "close>MA200 only (no chip)": bull200.astype(float),
    }
    for name, pos in s7_gates.items():
        add(f"s7_gate_{name}", "7", "backtest", name, pos=pos)

    # ---- Stage 8: layered system ----
    armed = (stage_s2 & tsmom).astype(float)
    size = pd.Series(0.0, index=p.index)
    size[(z60 > 0) & (z60 < 1)] = 0.5
    size[z60 >= 1] = 1.0
    pos_sys = armed * size
    add("s8_system_l0xl2", "8", "backtest", "SYSTEM L0xL2 (armed x size)", pos=pos_sys)
    add("s8_l0_only", "8", "backtest", "L0 regime only (Stage2 AND TSMOM126)", pos=armed)
    add("s8_l2_only", "8", "backtest", "L2 chip-size ladder only", pos=size)
    rv = day_ret.rolling(20, min_periods=20).std() * ANN
    volscale = (0.15 / rv).clip(0, 1.0)
    pos_vs = (armed * size * volscale.shift(1))
    add("s8_vol_scaled", "8", "backtest", "vol-scaled variant (15% target)", pos=pos_vs)

    # =========================================================================
    # Tally raw counts by category
    # =========================================================================
    df = pd.DataFrame(registry)
    n_screening = (df.category == "screening_ic").sum()
    n_diagnostic = (df.category == "diagnostic").sum()
    n_walkforward = (df.category == "walkforward").sum()
    n_backtest_raw = (df.category == "backtest").sum()

    print("=" * 78)
    print("RAW REGISTRY TALLY (every cell actually computed, before dedup)")
    print("=" * 78)
    print(f"  Stage-1 IC-scan screening cells (signal x horizon, not costed backtests): {n_screening}")
    print(f"  Diagnostic checks on an already-chosen rule (sub-period, permutation, "
          f"tail-bucket table) -- NOT new candidates, excluded from N: {n_diagnostic}")
    print(f"  Walk-forward procedures (honest sequential OOS by construction, counted as 1): {n_walkforward}")
    print(f"  Real costed backtest trials (candidate rules that could have been picked): {n_backtest_raw}")
    print(f"  RAW total 'looks' (screening + backtest + walkforward, excl. diagnostics) = "
          f"{n_screening + n_backtest_raw + n_walkforward}")

    # =========================================================================
    # Programmatic exact-duplicate detection among 'backtest' trials
    # =========================================================================
    bt = df[df.category == "backtest"].reset_index(drop=True)
    sigs = {}
    canon_of = {}
    dup_groups: dict[str, list[str]] = {}
    for _, row in bt.iterrows():
        pos = pd.Series(row["pos"], index=p.index).fillna(0.0).round(6)
        key = tuple(pos.values.tobytes())
        if key in sigs:
            canon = sigs[key]
        else:
            canon = row["id"]
            sigs[key] = canon
        canon_of[row["id"]] = canon
        dup_groups.setdefault(canon, []).append(row["id"])

    n_unique_backtest = len(sigs)
    n_dup = n_backtest_raw - n_unique_backtest
    print("\n" + "=" * 78)
    print("PROGRAMMATIC DUPLICATE DETECTION (exact-identical position series)")
    print("=" * 78)
    print(f"  {n_backtest_raw} raw backtest specs -> {n_unique_backtest} unique position series "
          f"({n_dup} exact duplicates under a different label)")
    for canon, members in dup_groups.items():
        if len(members) > 1:
            labels = bt.set_index("id").loc[members, "desc"].tolist()
            print(f"    DUP x{len(members)}: {labels}")

    # keep one representative per duplicate group
    unique_ids = list(sigs.values())
    uniq = bt[bt.id.isin(unique_ids)].copy()

    # =========================================================================
    # Compute OOS daily-return series + Sharpe for each unique backtest trial
    # =========================================================================
    ret_series = {}
    sharpes_oos = {}
    sharpes_full = {}
    for _, row in uniq.iterrows():
        r = lf(row["pos"], day_ret)
        ret_series[row["id"]] = r
        sharpes_full[row["id"]] = sharpe(r)
        sharpes_oos[row["id"]] = sharpe(r[oos.reindex(r.index).fillna(False)])

    uniq["sharpe_full"] = uniq["id"].map(sharpes_full)
    uniq["sharpe_oos"] = uniq["id"].map(sharpes_oos)

    # =========================================================================
    # Correlation matrix -> spectral effective-N (participation ratio) on OOS
    # daily returns of the unique trial set
    # =========================================================================
    ret_df = pd.DataFrame({k: v for k, v in ret_series.items()})
    ret_oos = ret_df[oos.reindex(ret_df.index).fillna(False)].dropna(axis=1, thresh=50)
    corr = ret_oos.corr()
    M = corr.shape[0]
    eigvals = np.linalg.eigvalsh(corr.values)
    eigvals = np.clip(eigvals, 0, None)
    participation_ratio_Neff = (eigvals.sum() ** 2) / (eigvals ** 2).sum()
    offdiag = corr.values[~np.eye(M, dtype=bool)]
    rho_bar = np.nanmean(offdiag)
    kish_Neff = M / (1 + (M - 1) * rho_bar)

    print("\n" + "=" * 78)
    print("CORRELATION STRUCTURE AMONG UNIQUE BACKTEST TRIALS (OOS daily returns)")
    print("=" * 78)
    print(f"  M (unique reconstructable trials with valid OOS series) = {M}")
    print(f"  mean pairwise |correlation| off-diagonal = {np.nanmean(np.abs(offdiag)):.3f}")
    print(f"  mean pairwise correlation (signed) rho_bar = {rho_bar:.3f}")
    print(f"  spectral participation-ratio N_eff = trace(C)^2 / sum(eig^2) = {participation_ratio_Neff:.1f}")
    print(f"  Kish-style N_eff = M / (1+(M-1)*rho_bar) = {kish_Neff:.1f}")

    # -------------------------------------------------------------------
    # Robustness check: the raw-return correlation above conflates two very
    # different things -- (a) genuine redundancy between similar TRADING
    # IDEAS, and (b) the fact that EVERY L/F timing rule here trades the
    # same trending underlying (TAIEX 2018-2026), so they all inherit
    # correlated market-beta exposure regardless of how different the idea
    # is. (b) is a structural feature of "many timing overlays on one
    # instrument", not evidence the SEARCH itself was narrow. To separate
    # them, residualize each trial's return against buy-and-hold (OLS beta)
    # and recompute N_eff on the residuals -- this isolates the part of each
    # trial that is genuinely idea-specific (differential timing skill) from
    # shared market exposure.
    # -------------------------------------------------------------------
    bh_oos = day_ret[oos.reindex(day_ret.index).fillna(False)].reindex(ret_oos.index)
    resid = {}
    for col in ret_oos.columns:
        y = ret_oos[col]
        d = pd.DataFrame({"y": y, "x": bh_oos}).dropna()
        if len(d) < 50 or d["x"].std() == 0:
            continue
        beta = np.cov(d["y"], d["x"])[0, 1] / np.var(d["x"])
        resid[col] = y - beta * bh_oos
    resid_df = pd.DataFrame(resid).dropna(axis=1, thresh=50)
    corr_resid = resid_df.corr()
    Mr = corr_resid.shape[0]
    eig_r = np.clip(np.linalg.eigvalsh(corr_resid.values), 0, None)
    participation_ratio_Neff_resid = (eig_r.sum() ** 2) / (eig_r ** 2).sum()
    offdiag_r = corr_resid.values[~np.eye(Mr, dtype=bool)]
    rho_bar_resid = np.nanmean(offdiag_r)
    kish_Neff_resid = Mr / (1 + (Mr - 1) * rho_bar_resid)
    print(f"\n  -- robustness: same calc on BETA-RESIDUALIZED returns (market-beta removed) --")
    print(f"  M = {Mr}  mean pairwise correlation rho_bar = {rho_bar_resid:.3f}")
    print(f"  spectral participation-ratio N_eff (residualized) = {participation_ratio_Neff_resid:.1f}")
    print(f"  Kish-style N_eff (residualized) = {kish_Neff_resid:.1f}")

    # Kaiser-criterion cross-check: how many eigenvalues > 1 (i.e. explain more
    # variance than a single average trial) -- a completely different, standard
    # PCA heuristic for "how many real independent components", used here only
    # to sanity-check the participation-ratio number above.
    n_kaiser = int((eigvals > 1).sum())
    n_kaiser_resid = int((eig_r > 1).sum())
    top3_var_share = np.sort(eigvals)[::-1][:3].sum() / eigvals.sum()
    print(f"\n  cross-check: eigenvalues>1 (Kaiser) on raw returns = {n_kaiser} of {M}; "
          f"on residualized = {n_kaiser_resid} of {Mr}")
    print(f"  top-3 eigenvalues explain {top3_var_share*100:.0f}% of total variance across "
          f"the {M} unique trials (raw)")

    # =========================================================================
    # var_trials for the DSR formula, computed over the SAME deduped unique set
    # (daily, not annualized -- matches eval_deflated_sharpe.py's convention)
    # =========================================================================
    oos_daily_sr = [sharpes_oos[i] / ANN for i in ret_oos.columns if pd.notna(sharpes_oos.get(i))]
    var_trials_audited = np.var(oos_daily_sr, ddof=1)

    # =========================================================================
    # Recompute DSR for the final SYSTEM (close-to-close, matches finalize_cc.py)
    # =========================================================================
    sys_ret_cc = strat_cc(pos_sys, p, r_oc, r_cc)
    sys_full_shp = sharpe(sys_ret_cc)
    sys_oos_shp = sharpe(sys_ret_cc[oos.reindex(sys_ret_cc.index).fillna(False)])

    # reproduce the ORIGINAL chain's var_trials (7-trial close-to-close list from
    # finalize_cc.py) as a sanity check that this script's engine matches theirs
    orig_trials = [strat_cc(x, p, r_oc, r_cc) for x in [
        chip.astype(float), (chip & bull200).astype(float), (chip & stage_s2).astype(float),
        stage_s2.astype(float), armed, size, pos_sys]]
    var_t_orig = np.var([sharpe(t[oos.reindex(t.index).fillna(False)]) / ANN for t in orig_trials], ddof=1)

    print("\n" + "=" * 78)
    print("REPRODUCTION CHECK (finalize_cc.py methodology, current data pull)")
    print("=" * 78)
    print(f"  SYSTEM L0xL2 (close-to-close): full Sharpe={sys_full_shp:+.3f}  OOS Sharpe={sys_oos_shp:+.3f}")
    print(f"  var_trials (original 7-trial close-to-close list) = {var_t_orig:.3e}")
    print(f"  DSR @ N=160 (as recorded, hand-carried chain): "
          f"full={dsr(sys_ret_cc, var_t_orig, 160):.3f}  OOS={dsr(sys_ret_cc[oos.reindex(sys_ret_cc.index).fillna(False)], var_t_orig, 160):.3f}"
          f"   (RESEARCH_LOG.md logged OOS 0.869 on 2026-07-29 data; this is the same "
          f"formula on {n} rows through {p['date'].iloc[-1]})")

    print("\n" + "=" * 78)
    print("AUDITED DSR — SYSTEM L0xL2 OOS, at each candidate N (audited var_trials)")
    print("=" * 78)
    n_choices = {
        "N=160 (original hand-carried, hardcoded 'conservative')": 160,
        f"N={n_screening + n_backtest_raw + n_walkforward} (audited RAW: all screening+backtest+WF cells, no dedup)":
            n_screening + n_backtest_raw + n_walkforward,
        f"N={n_unique_backtest + n_walkforward} (audited UNIQUE backtests, dedup'd, + WF; excludes IC-screening cells)":
            n_unique_backtest + n_walkforward,
        f"N={n_screening + n_unique_backtest + n_walkforward} (audited UNIQUE backtests+WF + full IC-screening tally)":
            n_screening + n_unique_backtest + n_walkforward,
        f"N_eff={participation_ratio_Neff:.0f} (correlation-adjusted, spectral participation ratio)":
            max(participation_ratio_Neff, 2.0),
        f"N_eff={kish_Neff:.0f} (correlation-adjusted, Kish mean-rho approximation)":
            max(kish_Neff, 2.0),
        f"N_eff={participation_ratio_Neff_resid:.0f} (correlation-adjusted, spectral, BETA-RESIDUALIZED -- preferred)":
            max(participation_ratio_Neff_resid, 2.0),
        f"N_eff={kish_Neff_resid:.0f} (correlation-adjusted, Kish, BETA-RESIDUALIZED)":
            max(kish_Neff_resid, 2.0),
        f"N_eff={n_kaiser} (Kaiser eigenvalue>1 cross-check, raw)": max(n_kaiser, 2),
        f"N_eff={n_kaiser_resid} (Kaiser eigenvalue>1 cross-check, residualized)": max(n_kaiser_resid, 2),
    }
    results = []
    for label, N in n_choices.items():
        d_full = dsr(sys_ret_cc, var_trials_audited, N)
        d_oos = dsr(sys_ret_cc[oos.reindex(sys_ret_cc.index).fillna(False)], var_trials_audited, N)
        print(f"  {label:78s} full DSR={d_full:.3f}  OOS DSR={d_oos:.3f}  -> "
              f"{'SURVIVES' if d_oos > 0.95 else 'fails'} 5% bar")
        results.append({"N_label": label, "N": N, "dsr_full": d_full, "dsr_oos": d_oos,
                         "var_trials": var_trials_audited})

    # =========================================================================
    # Save outputs
    # =========================================================================
    reg_out = df.drop(columns=["pos"]).copy()
    reg_out["is_unique_backtest"] = reg_out["id"].map(lambda i: i in unique_ids)
    reg_out["canonical_duplicate_of"] = reg_out["id"].map(lambda i: canon_of.get(i, ""))
    reg_out["sharpe_full"] = reg_out["id"].map(sharpes_full)
    reg_out["sharpe_oos"] = reg_out["id"].map(sharpes_oos)
    reg_out.to_csv(OUT / "dsr_trial_count_audit_registry.csv", index=False)

    pd.DataFrame(results).to_csv(OUT / "dsr_trial_count_audit_results.csv", index=False)
    corr.to_csv(OUT / "dsr_trial_count_audit_correlation_matrix.csv")

    print(f"\n-> {OUT / 'dsr_trial_count_audit_registry.csv'}")
    print(f"-> {OUT / 'dsr_trial_count_audit_results.csv'}")
    print(f"-> {OUT / 'dsr_trial_count_audit_correlation_matrix.csv'}")


if __name__ == "__main__":
    main()
