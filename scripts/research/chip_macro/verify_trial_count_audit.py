"""Independent verification of scripts/research/chip_macro/audit_trial_count.py
(see reports/research/chip-macro/RESEARCH_LOG.md Stage 11: that audit's N_eff /
"DSR now passes 0.95" claim was explicitly left unverified pending independent
review -- this IS that review).

--------------------------------------------------------------------------
Why the closed-form N_eff plug-in in audit_trial_count.py is NOT trustworthy
on its own
--------------------------------------------------------------------------
audit_trial_count.py's "AUDITED DSR" table computes an "effective independent
trial count" N_eff via two off-the-shelf correlation-adjustment heuristics
(spectral participation ratio; Kish mean-rho approximation) borrowed from
unrelated fields (genomics multiple-testing / survey statistics), then PLUGS
N_eff directly into Bailey & Lopez de Prado (2014)'s closed form

    SR* = sqrt(var_trials) * [ (1-gamma)*Phi^-1(1-1/N) + gamma*Phi^-1(1-1/(N*e)) ]

That closed form is an extreme-value APPROXIMATION to E[max of N i.i.d.
Normal(0, var_trials) draws]. Swapping in a small fractional N_eff (~4-14,
depending on which of 4 disagreeing heuristics you pick) computed from a
totally different statistic (eigenvalue dispersion of an 80x80 correlation
matrix) is an ANALOGY, not a derivation -- there is no proof anywhere that
this closed form, evaluated at N_eff, equals E[max of 80 CORRELATED draws]
with the actual empirical correlation structure. It could over- or
under-shoot in either direction. And the fact the 4 heuristics used already
disagree with each other by >3x (3.6-5.1 spectral/Kish, vs 11-14 Kaiser)
-- a spread that flips the headline verdict (DSR 0.79 vs 0.94-0.99) -- is
itself disqualifying: a "correction" this sensitive to arbitrary method
choice cannot be reported as a confirmed result.

--------------------------------------------------------------------------
What this script does instead
--------------------------------------------------------------------------
Rather than approximate-via-analogy, this directly Monte Carlos the quantity
Bailey & Lopez de Prado's closed form is ITSELF only approximating:
E[max SR] of M jointly-correlated null trials, using the REAL empirical
correlation matrix of the 80 unique Stage1-8 backtest trials (rebuilt here
from scratch, independently of audit_trial_count.py, as a cross-check that
the registry/dedup logic reproduces), with per-trial variance = var_trials
(the SAME homoscedastic-variance assumption the closed form itself makes --
one shared variance across all trials, not trial-specific).

This sidesteps the N_eff-as-proxy step entirely: SR*_sim = E[max] comes
directly from simulation over the true correlation structure, no small-N
closed-form extrapolation needed. We report DSR using SR*_sim, and (purely
as a comparison point against audit_trial_count.py's N_eff claims, NOT as a
new number to plug back into anything) back out what "closed-form-equivalent
N" would have reproduced SR*_sim via the iid formula.

Two correlation structures are tested for robustness:
  (a) raw OOS trial returns
  (b) beta-residualized (market-beta vs buy&hold removed) -- isolates
      idea-specific correlation from shared trend/market exposure, matching
      audit_trial_count.py's own robustness check.

No look-ahead is introduced: every position series below reuses the exact
info-known-at-close-t conventions of the original Stage 1-8 scripts. Panel
path note: RESEARCH_LOG.md / stage scripts point at panel.parquet, but only
panel.csv exists on disk (see audit_trial_count.py docstring) -- this script
reads the .csv, same as that one.
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
N_SIMS = 500_000
RNG = np.random.default_rng(20260807)


def rz(s, w):
    return (s - s.rolling(w, min_periods=w).mean()) / s.rolling(w, min_periods=w).std()


def signed_streak(s):
    sign = np.sign(s.fillna(0.0).values)
    out = np.zeros(len(sign)); run = 0; prev = 0.0
    for i, v in enumerate(sign):
        run = 0 if v == 0 else (run + 1 if v == prev else 1)
        out[i] = run * v; prev = v
    return pd.Series(out, index=s.index)


def lf(pos, day_ret, cost=COST_BPS):
    pos = pd.Series(pos, index=day_ret.index).astype(float).fillna(0.0)
    return pos * day_ret - pos.diff().abs().fillna(pos.abs()) * cost / 1e4


def strat_cc(pos, p, r_oc, r_cc, cost=COST_BPS):
    pos = pd.Series(pos, index=p.index).astype(float).fillna(0.0)
    pos_prev = pos.shift(1)
    fresh = (pos_prev > 0) & (pos.shift(2).fillna(0) == 0)
    base = pd.Series(np.where(fresh, r_oc, r_cc), index=p.index)
    return pos_prev * base - pos.diff().abs().fillna(pos.abs()) * cost / 1e4


def sr_star_closed_form(var_trials: float, N: float) -> float:
    return np.sqrt(var_trials) * (
        (1 - GAMMA) * norm.ppf(1 - 1.0 / N) + GAMMA * norm.ppf(1 - 1.0 / (N * np.e))
    )


def closed_form_equivalent_N(var_trials: float, sr_star_target: float) -> float:
    lo, hi = 1.001, 1e7
    for _ in range(200):
        mid = np.sqrt(lo * hi)
        if sr_star_closed_form(var_trials, mid) < sr_star_target:
            lo = mid
        else:
            hi = mid
    return mid


def psr(sr, n, g3, g4, sr_star):
    denom = np.sqrt(1 - g3 * sr + (g4 - 1) / 4.0 * sr**2)
    return norm.cdf((sr - sr_star) * np.sqrt(n - 1) / denom)


def nearest_psd_corr(C: np.ndarray):
    eigval, eigvec = np.linalg.eigh(C)
    n_neg = int((eigval < -1e-8).sum())
    eigval_clipped = np.clip(eigval, 1e-10, None)
    C_psd = eigvec @ np.diag(eigval_clipped) @ eigvec.T
    d = np.sqrt(np.diag(C_psd))
    C_psd = C_psd / np.outer(d, d)
    np.fill_diagonal(C_psd, 1.0)
    return C_psd, n_neg


def monte_carlo_sr_star(corr: np.ndarray, var_trials: float, n_sims: int, rng, batch: int = 20_000):
    C_psd, n_neg = nearest_psd_corr(corr)
    L = np.linalg.cholesky(C_psd)
    sd = np.sqrt(var_trials)
    M = corr.shape[0]
    max_srs = np.empty(n_sims)
    for start in range(0, n_sims, batch):
        b = min(batch, n_sims - start)
        z = rng.standard_normal((b, M))
        draws = (z @ L.T) * sd
        max_srs[start:start + b] = draws.max(axis=1)
    return max_srs, n_neg


def build_registry(p: pd.DataFrame, day_ret: pd.Series, is_mask: pd.Series) -> list[dict]:
    """Rebuild the same 80-unique-trial universe as audit_trial_count.py, from
    scratch, as an independent cross-check that the dedup/registry logic there
    is reproducible (not just re-reading its saved CSV)."""
    z60 = rz(p["fut_foreign_oi"], 60)
    z120 = rz(p["fut_foreign_oi"], 120)
    chg5 = p["fut_foreign_oi"].diff(5)
    doi = p["fut_foreign_doi"]
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

    registry: list[dict] = []
    def add(id_, pos):
        registry.append({"id": id_, "pos": pos})

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
        add(f"s1_ls_{name}", np.sign(pd.Series(s, index=p.index)))

    s2_cands = {
        "fut_foreign_oi_z60>0": z60 > 0, "fut_foreign_oi_z120>0": z120 > 0,
        "fut_foreign_oi_chg5>0": chg5 > 0, "fut_foreign_doi>0": doi > 0,
        "inst_total>0": inst > 0, "trust_streak<=-1(contra)": tstreak <= -1,
    }
    for name, bull in s2_cands.items():
        add(f"s2_lf_{name}", bull.astype(float))
        add(f"s2_ls_{name}", pd.Series(np.where(bull, 1.0, -1.0), index=p.index))

    for w in (40, 60, 90, 120, 180):
        zc = rz(p["fut_foreign_oi"], w)
        for t in (-0.5, -0.25, 0.0, 0.25, 0.5):
            add(f"s3_grid_w{w}_t{t}", (zc > t).astype(float))

    z60b, doib, chg5b = z60 > 0, doi > 0, chg5 > 0
    add("s3_and_z60_chg5", (z60b & chg5b).astype(float))
    add("s3_or_z60_chg5", (z60b | chg5b).astype(float))
    add("s3_maj_z60_doi_chg5", ((z60b.astype(int) + doib.astype(int) + chg5b.astype(int)) >= 2).astype(float))

    margin_bal, short_bal = p["margin_bal"], p["short_bal"]
    s4_cand_vals = {
        "margin_bal_z60": rz(margin_bal, 60), "margin_chg5_pct": margin_bal.pct_change(5, fill_method=None),
        "short_bal_z60": rz(short_bal, 60), "shortmargin_ratio_z60": rz(short_bal / margin_bal, 60),
        "fut_trust_oi_z60": rz(p["fut_trust_oi"], 60), "fut_dealer_oi_z60": rz(p["fut_dealer_oi"], 60),
        "dealer_hedge_z20": rz(p["dealer_hedge"], 20),
        "cashfut_diverg": (-np.sign(p["foreign"].fillna(0))) * p["fut_foreign_doi"],
    }
    s4_bulls = {}
    for name, v in s4_cand_vals.items():
        v = pd.Series(v, index=p.index)
        d = pd.DataFrame({"v": v, "f": day_ret}).dropna()
        ic_is = d[is_mask.reindex(d.index).fillna(False)]["v"].corr(d[is_mask.reindex(d.index).fillna(False)]["f"])
        direction = np.sign(ic_is) if pd.notna(ic_is) and ic_is != 0 else 1.0
        bull = (direction * v) > 0
        s4_bulls[name] = bull
        add(f"s4_{name}", bull.astype(float))
    comb = (chip.astype(float) + s4_bulls["margin_bal_z60"].astype(float)) / 2
    add("s4_champ_plus_margin_5050", comb)

    tail_pos = pd.Series(0.0, index=p.index)
    tail_pos[z60 >= z60.quantile(0.8)] = 1.0
    tail_pos[z60 <= z60.quantile(0.2)] = -1.0
    add("s6_tail_static_quantile", tail_pos)
    rq_hi = z60.rolling(252, min_periods=120).quantile(0.8)
    rq_lo = z60.rolling(252, min_periods=120).quantile(0.2)
    tail_pos_r = pd.Series(0.0, index=p.index)
    tail_pos_r[z60 >= rq_hi] = 1.0
    tail_pos_r[z60 <= rq_lo] = -1.0
    add("s6_tail_rolling_quantile", tail_pos_r)

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
        add(f"s6_rule_{name}", pos)
    s6_combos = {
        "long HIGH20 z60 only in bull": pd.Series(np.where((z60 >= z60.quantile(0.8)) & bull, 1.0, 0.0), index=p.index),
        "short LOW20 z60 only in bear": pd.Series(np.where((z60 <= z60.quantile(0.2)) & ~bull, -1.0, 0.0), index=p.index),
        "bull-long + bear-short(LOW20)": pd.Series(np.where(bull, 1.0, np.where(z60 <= z60.quantile(0.2), -1.0, 0.0)), index=p.index),
    }
    for name, pos in s6_combos.items():
        add(f"s6_combo_{name}", pos)

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
        add(f"s7_gate_{name}", pos)

    armed = (stage_s2 & tsmom).astype(float)
    size = pd.Series(0.0, index=p.index)
    size[(z60 > 0) & (z60 < 1)] = 0.5
    size[z60 >= 1] = 1.0
    pos_sys = armed * size
    add("s8_system_l0xl2", pos_sys)
    add("s8_l0_only", armed)
    add("s8_l2_only", size)
    rv = day_ret.rolling(20, min_periods=20).std() * ANN
    volscale = (0.15 / rv).clip(0, 1.0)
    add("s8_vol_scaled", armed * size * volscale.shift(1))

    return registry


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    p = pd.read_csv(PANEL).sort_values("date").reset_index(drop=True)
    n = len(p)
    is_cut = int(n * 0.7)
    oos = pd.Series(p.index >= is_cut, index=p.index)
    is_mask = pd.Series(p.index < is_cut, index=p.index)
    r_cc = p["ix_close"] / p["ix_close"].shift(1) - 1.0
    r_oc = p["ix_close"] / p["ix_open"] - 1.0
    day_ret = (p["ix_close"] / p["ix_open"] - 1.0).shift(-1)

    print(f"panel n={n}  {p['date'].iloc[0]} -> {p['date'].iloc[-1]}   OOS n={n - is_cut}")

    # ---- SYSTEM L0xL2 close-to-close OOS series (finalize_cc.py methodology) ----
    ma150 = p["ix_close"].rolling(150, min_periods=150).mean()
    slope_up = (ma150 - ma150.shift(25)) > 0
    tsmom = (p["ix_close"] / p["ix_close"].shift(126) - 1) > 0
    stage_s2 = (p["ix_close"] > ma150) & slope_up
    z60 = rz(p["fut_foreign_oi"], 60)
    armed = (stage_s2 & tsmom).astype(float)
    size = pd.Series(0.0, index=p.index)
    size[(z60 > 0) & (z60 < 1)] = 0.5
    size[z60 >= 1] = 1.0
    pos_sys = armed * size

    sys_ret_cc = strat_cc(pos_sys, p, r_oc, r_cc)
    sys_oos = sys_ret_cc[oos.reindex(sys_ret_cc.index).fillna(False)].dropna()
    sr_daily = sys_oos.mean() / sys_oos.std()
    n_oos = len(sys_oos)
    g3 = sys_oos.skew()
    g4 = sys_oos.kurtosis() + 3
    print(f"SYSTEM L0xL2 OOS: n={n_oos}  ann Sharpe={sr_daily*ANN:+.3f}  skew={g3:+.2f}  kurt={g4:.1f}")

    # ---- rebuild the 80-unique-trial registry from scratch (independent cross-check) ----
    registry = build_registry(p, day_ret, is_mask)
    df = pd.DataFrame(registry)
    sigs, canon_of = {}, {}
    for _, row in df.iterrows():
        pos = pd.Series(row["pos"], index=p.index).fillna(0.0).round(6)
        key = tuple(pos.values.tobytes())
        canon = sigs.setdefault(key, row["id"])
        canon_of[row["id"]] = canon
    unique_ids = list(sigs.values())
    uniq = df[df.id.isin(unique_ids)].copy()
    print(f"\nrebuilt registry independently: {len(df)} raw backtest specs -> "
          f"{len(uniq)} unique position series (cross-check vs audit_trial_count.py's 93 -> 80)")

    ret_series = {row["id"]: lf(row["pos"], day_ret) for _, row in uniq.iterrows()}
    ret_df = pd.DataFrame(ret_series)
    ret_oos = ret_df[oos.reindex(ret_df.index).fillna(False)].dropna(axis=1, thresh=50)
    M = ret_oos.shape[1]
    corr_raw = ret_oos.corr()

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

    oos_daily_sr = [ret_oos[c].mean() / ret_oos[c].std() for c in ret_oos.columns]
    var_trials = np.var(oos_daily_sr, ddof=1)
    print(f"M={M}  var_trials (independent recompute) = {var_trials:.6e}  "
          f"(audit_trial_count.py's saved value: 1.1476e-03)")

    # ---- closed-form rows at honest (non-correlation-adjusted) N choices ----
    rows = []
    for label, N in [
        ("N=160 (original hand-carried, hardcoded 'conservative')", 160),
        ("N=204 (audited raw: all screening+backtest+WF cells, no dedup)", 204),
        ("N=81 (audited unique backtests+WF, dedup'd -- RECOMMENDED honest baseline, no corr-adjustment)", M + 1),
        ("N=191 (unique backtests+WF + full IC-screening tally)", M + 1 + 110),
    ]:
        sr_star = sr_star_closed_form(var_trials, N)
        d = psr(sr_daily, n_oos, g3, g4, sr_star)
        rows.append({"method": label, "N_or_Neq": N, "sr_star_ann": sr_star * ANN, "dsr_oos": d})

    # ---- Monte Carlo, correlation-respecting, on RAW and RESIDUALIZED correlation ----
    print(f"\nMonte Carlo E[max SR] over {N_SIMS:,} draws, {M} correlated null trials:")
    for tag, corr in [("raw OOS returns", corr_raw.values), ("beta-residualized (mkt-beta removed)", corr_resid.values)]:
        max_srs, n_neg = monte_carlo_sr_star(corr, var_trials, N_SIMS, RNG)
        sr_star_sim = max_srs.mean()
        sr_star_sim_med = np.median(max_srs)
        N_eq = closed_form_equivalent_N(var_trials, sr_star_sim)
        d_mc = psr(sr_daily, n_oos, g3, g4, sr_star_sim)
        d_mc_med = psr(sr_daily, n_oos, g3, g4, sr_star_sim_med)
        sr_star_indep_M = sr_star_closed_form(var_trials, M)
        print(f"\n  [{tag}]  ({n_neg} tiny-negative eigenvalues clipped for PSD)")
        print(f"    SR*_sim mean-of-max (ann)   = {sr_star_sim*ANN:+.3f}   "
              f"vs closed-form-indep@N={M} (ann) = {sr_star_indep_M*ANN:+.3f}   "
              f"ratio={sr_star_sim/sr_star_indep_M:.3f}")
        print(f"    closed-form-equivalent N = {N_eq:.1f}   "
              f"(cf. audit_trial_count.py's heuristic N_eff: 3.6-5.1 spectral/Kish, 11-14 Kaiser)")
        print(f"    DSR OOS (mean-of-max, VALIDATED)   = {d_mc:.3f}  -> {'SURVIVES' if d_mc>0.95 else 'fails'} 5% bar")
        print(f"    DSR OOS (median-of-max, robustness) = {d_mc_med:.3f}  -> {'SURVIVES' if d_mc_med>0.95 else 'fails'} 5% bar")
        rows.append({"method": f"Monte Carlo mean-of-max, {tag} (VALIDATED)",
                     "N_or_Neq": round(N_eq, 1), "sr_star_ann": sr_star_sim * ANN, "dsr_oos": d_mc})
        rows.append({"method": f"Monte Carlo median-of-max, {tag} (robustness check)",
                     "N_or_Neq": round(N_eq, 1), "sr_star_ann": sr_star_sim_med * ANN, "dsr_oos": d_mc_med})

    print("\n" + "=" * 78)
    print("SUMMARY")
    print("=" * 78)
    for r in rows:
        print(f"  {r['method']:78s} N/Neq={str(r['N_or_Neq']):>8s}  DSR OOS={r['dsr_oos']:.3f}")

    out_df = pd.DataFrame(rows)
    out_df.to_csv(OUT / "dsr_trial_count_verified_results.csv", index=False)
    print(f"\n-> {OUT / 'dsr_trial_count_verified_results.csv'}")


if __name__ == "__main__":
    main()
