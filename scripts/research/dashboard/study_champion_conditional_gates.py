"""家族【條件閘門交互】: champion 訊號「只在 B 狀態才發」是否勝過 champion 單獨.

Champion = fut_foreign_oi z60 > 0  → 隔日 open→close 做多大盤 (t+1, 無前視).
Gate 交互 = champion AND (經濟邏輯條件 B). 每個 gate 只在 B 為真時才讓 champion 開倉;
其餘日空手 (pos=0). 比較「加條件 vs champion 單獨」的 walk-forward OOS Sharpe / maxDD / DSR.

嚴守核心紀律:
  1. 只測有經濟邏輯的條件 (條件化/雙確認), 不暴力枚舉.
  2. 多重檢定: 記錄本 agent 實際跑過的所有 gate-variant trial 數, DSR 用該數當懲罰 N.
  3. Walk-forward 擴張窗 (5 折) + 全 OOS(後 30%) 雙軌, 不是單一 IS/OOS.
  4. Permutation 同曝險 (在 champion-fire 日內重抽 gate-on 日).
  5. 每個存活者答: 是不是只是 champion / MA200-regime 的重述 (共線 + 殘差).

閘門 (全部 a-priori 門檻, 不在樣本內擬合方向, 降過擬合):
  G1 breadth  普漲: pct_above_ma50 > {0.40,0.50,0.60}
  G2 維持率斷頭區 (扣ETF): maint_exetf < {128,132,136}   (資料僅 2024-07+, 短)
  G2b 維持率(含ETF,2018+) z252 低尾: maint_z < {-1.0,-1.5}
  G3 VIX 非 spike: VIX < {22,26,30} 且 VIX 未 > 60d-90pct  (已知會贏, 對照組)
  G4 現貨外資與期貨同向: foreign_cash>0 ; foreign_cash z20>0
  G5 投信連買: trust>0 連 {2,3,5} 日
"""
from __future__ import annotations
from pathlib import Path
import numpy as np
import pandas as pd
from scipy.stats import norm

ROOT = Path(__file__).resolve().parents[3]
PANEL = ROOT / "data" / "research" / "chip_macro" / "panel.parquet"
MAINT_EXETF = ROOT / "data" / "research" / "chip_macro" / "margin_maintenance_exetf.csv"
MAINT_MKT = ROOT / "data" / "research" / "dashboard" / "margin_maintenance_data.parquet"
MACRO = ROOT / "data" / "research" / "dashboard" / "global_macro_data.parquet"
BREADTH = Path("/private/tmp/claude-501/-Users-jackm4-Documents-ETF-----/"
               "452df84e-ae89-4b92-9258-802727472d3b/scratchpad/breadth_daily.parquet")
OUTDIR = ROOT / "data" / "research" / "dashboard"
REPORT = ROOT / "reports" / "research" / "dashboard-completeness"

ANN = np.sqrt(252)
GAMMA = 0.5772156649
COST_BPS = 4.0
RNG = np.random.default_rng(11)
N_PERM = 2000


def rz(s, w):
    return (s - s.rolling(w, min_periods=w).mean()) / s.rolling(w, min_periods=w).std()


def sharpe(x):
    x = pd.Series(x).dropna()
    return float(x.mean() / x.std() * ANN) if len(x) > 20 and x.std() > 0 else np.nan


def lf(pos, day_ret, cost=COST_BPS):
    """Daily strategy return over the FULL index (flat days = 0). pos in {0,1}."""
    pos = pd.Series(pos, index=day_ret.index).astype(float).fillna(0.0)
    return (pos * day_ret - pos.diff().abs().fillna(pos.abs()) * cost / 1e4)


def maxdd(ret):
    r = pd.Series(ret).fillna(0.0)
    eq = r.cumsum()                       # additive (small daily) equity
    return float((eq - eq.cummax()).min())


def dsr(ret, var_trials, N):
    r = pd.Series(ret).dropna().values
    if len(r) < 30 or r.std() == 0:
        return np.nan, np.nan
    n = len(r); sr = r.mean() / r.std()
    g3 = pd.Series(r).skew(); g4 = pd.Series(r).kurtosis() + 3
    sr_star = np.sqrt(var_trials) * ((1 - GAMMA) * norm.ppf(1 - 1/N) + GAMMA * norm.ppf(1 - 1/(N*np.e)))
    denom = np.sqrt(1 - g3*sr + (g4-1)/4*sr**2)
    return float(norm.cdf((sr - sr_star) * np.sqrt(n - 1) / denom)), float(sr*ANN)


def perm_gate(champ_fire, gate_on, day_ret, mask):
    """Same-exposure permutation: within champion-fire days (in mask), the gate keeps
    k of them. Randomly re-pick k champion-fire days -> null Sharpe distribution of the
    gated strategy. Answers: does the SELECTION (this specific condition) beat a random
    subset of the same size of champion's own trading days?"""
    idx = day_ret.index[mask]
    dr = day_ret.reindex(idx)
    fire = pd.Series(champ_fire, index=day_ret.index).reindex(idx).astype(bool).values
    on = pd.Series(gate_on, index=day_ret.index).reindex(idx).astype(bool).values
    fire_pos = np.where(fire)[0]
    if len(fire_pos) == 0:
        return np.nan, np.nan, 0
    gated = fire & on
    k = int(gated.sum())
    drv = dr.values
    actual = sharpe(np.where(gated, drv, 0.0))
    if k == 0 or k == len(fire_pos) or not np.isfinite(actual):
        return actual, np.nan, k
    null = np.empty(N_PERM)
    base = np.zeros(len(drv))
    for i in range(N_PERM):
        pick = RNG.choice(fire_pos, k, replace=False)
        p = base.copy(); p[pick] = 1.0
        null[i] = sharpe(p * drv)
    return actual, float((null >= actual).mean()), k


def main():
    p = pd.read_parquet(PANEL).sort_values("date").reset_index(drop=True)
    p["date"] = pd.to_datetime(p["date"])
    day_ret = (p["ix_close"] / p["ix_open"] - 1.0).shift(-1)   # t+1 open->close

    # ---- champion
    z60 = rz(p["fut_foreign_oi"], 60)
    champ_fire = (z60 > 0)

    # ---- merge auxiliary daily series onto panel dates
    bd = pd.read_parquet(BREADTH); bd["date"] = pd.to_datetime(bd["date"])
    p = p.merge(bd[["date", "net_breadth", "pct_above_ma50", "pct_above_ma200"]], on="date", how="left")

    me = pd.read_csv(MAINT_EXETF); me["date"] = pd.to_datetime(me["date"])
    p = p.merge(me.rename(columns={"maint": "maint_exetf"}), on="date", how="left")

    mm = pd.read_parquet(MAINT_MKT); mm["date"] = pd.to_datetime(mm["date"])
    p = p.merge(mm.rename(columns={"maintenance_pct": "maint_mkt"}), on="date", how="left")
    p["maint_mkt_z"] = rz(p["maint_mkt"], 252)

    mac = pd.read_parquet(MACRO); mac["date"] = pd.to_datetime(mac["date"])
    p = p.merge(mac[["date", "VIX"]], on="date", how="left")
    p["VIX"] = p["VIX"].ffill()
    p["vix_p90"] = p["VIX"].rolling(60, min_periods=40).quantile(0.90)

    # regime
    ma200 = p["ix_close"].rolling(200, min_periods=200).mean()
    bull = (p["ix_close"] > ma200)

    # dual-confirm cash foreign
    foreign_z20 = rz(p["foreign"], 20)
    # trust consecutive-buy streak
    tb = (p["trust"] > 0).astype(int)
    streak = tb * 0
    c = 0
    for i in range(len(tb)):
        c = c + 1 if tb.iloc[i] == 1 else 0
        streak.iloc[i] = c

    # ------------- define gate variants (a-priori)
    gates = {}
    for thr in (0.40, 0.50, 0.60):
        gates[f"G1_breadth_ma50>{thr:.2f}"] = (p["pct_above_ma50"] > thr)
    for thr in (128, 132, 136):
        gates[f"G2_maint_exetf<{thr}"] = (p["maint_exetf"] < thr)
    for thr in (-1.0, -1.5):
        gates[f"G2b_maintmkt_z<{thr}"] = (p["maint_mkt_z"] < thr)
    for thr in (22, 26, 30):
        gates[f"G3_VIX<{thr}&noSpike"] = (p["VIX"] < thr) & (p["VIX"] <= p["vix_p90"])
    gates["G4_cashForeign>0"] = (p["foreign"] > 0)
    gates["G4b_cashForeign_z20>0"] = (foreign_z20 > 0)
    for k in (2, 3, 5):
        gates[f"G5_trustStreak>={k}"] = (streak >= k)

    n_trials = len(gates)                      # honest DSR N (all variants tried)

    # ---- champion baseline
    champ_ret = lf(champ_fire, day_ret)
    n = len(p)
    oos_mask = pd.Series(p.index >= int(n * 0.7), index=p.index)

    # var of OOS Sharpe across gate trials (for DSR trial-dispersion)
    oos_srs = []
    for g in gates.values():
        r = lf(champ_fire & g.fillna(False), day_ret)
        ro = r[oos_mask]
        s = sharpe(ro)
        if np.isfinite(s):
            oos_srs.append(s / ANN)     # daily
    var_trials = float(np.var(oos_srs, ddof=1)) if len(oos_srs) > 2 else 1e-4

    # walk-forward expanding OOS: 5 folds after a 40% burn-in
    burn = int(n * 0.40)
    fold_edges = np.linspace(burn, n, 6).astype(int)
    folds = [(fold_edges[i], fold_edges[i + 1]) for i in range(5)]

    def wf_sharpe(pos):
        r = lf(pos, day_ret)
        out = []
        for a, b in folds:
            m = (p.index >= a) & (p.index < b)
            out.append(sharpe(r[m]))
        return out

    champ_wf = wf_sharpe(champ_fire)

    rows = []
    print("=" * 100)
    print(f"CHAMPION baseline: fullSharpe={sharpe(champ_ret):+.2f}  OOS={sharpe(champ_ret[oos_mask]):+.2f}  "
          f"maxDD={maxdd(champ_ret):.3f}  fire%={champ_fire.mean():.2%}")
    print(f"champion walk-forward folds: " + "  ".join(f"{s:+.2f}" for s in champ_wf))
    dsr_champ, _ = dsr(champ_ret[oos_mask], var_trials, n_trials)
    print(f"champion OOS DSR (vs {n_trials} gate trials) = {dsr_champ:.3f}")
    print("=" * 100)

    for name, g in gates.items():
        g = g.fillna(False)
        # coverage: on how many champion-fire days is the gate valid (non-NaN in raw)
        valid = g.notna()
        gated_fire = champ_fire & g
        cov_days = int(gated_fire.sum())
        exposure = gated_fire.mean()
        gret = lf(gated_fire, day_ret)
        full_s = sharpe(gret); oos_s = sharpe(gret[oos_mask])
        dd = maxdd(gret)
        wf = wf_sharpe(gated_fire)
        wf_valid = [s for s in wf if np.isfinite(s)]
        wf_min = min(wf_valid) if wf_valid else np.nan
        wf_pos = np.mean([s > 0 for s in wf_valid]) if wf_valid else np.nan
        d_oos, _ = dsr(gret[oos_mask], var_trials, n_trials)
        # permutation over the WHOLE sample (same exposure within champion-fire days)
        actual, pval, k = perm_gate(champ_fire, g, day_ret, np.ones(n, bool))
        # improvement vs champion on the same days the gate is active? -> Sharpe uplift
        beats_full = (full_s - sharpe(champ_ret)) if np.isfinite(full_s) else np.nan
        rows.append(dict(gate=name, fire_days=cov_days, exposure=round(float(exposure), 3),
                         full_Sharpe=round(full_s, 3) if np.isfinite(full_s) else np.nan,
                         OOS_Sharpe=round(oos_s, 3) if np.isfinite(oos_s) else np.nan,
                         d_vs_champ_full=round(beats_full, 3) if np.isfinite(beats_full) else np.nan,
                         maxDD=round(dd, 3), wf_min=round(wf_min, 3) if np.isfinite(wf_min) else np.nan,
                         wf_pos_frac=round(float(wf_pos), 2) if np.isfinite(wf_pos) else np.nan,
                         perm_p=round(pval, 3) if np.isfinite(pval) else np.nan,
                         DSR_oos=round(d_oos, 3) if np.isfinite(d_oos) else np.nan))
        print(f"[{name:28s}] fire={cov_days:4d} exp={exposure:5.1%} "
              f"full={full_s:+.2f} OOS={oos_s:+.2f} dChamp={beats_full:+.2f} "
              f"maxDD={dd:+.3f} wf_min={wf_min:+.2f} wf_pos={wf_pos:.0%} "
              f"perm_p={pval if np.isfinite(pval) else float('nan'):.3f} DSR={d_oos:.3f}")

    res = pd.DataFrame(rows)
    res["champ_full_Sharpe"] = round(sharpe(champ_ret), 3)
    res["champ_OOS_Sharpe"] = round(sharpe(champ_ret[oos_mask]), 3)
    res["champ_maxDD"] = round(maxdd(champ_ret), 3)
    res["n_trials"] = n_trials
    OUTDIR.mkdir(parents=True, exist_ok=True)
    res.to_parquet(OUTDIR / "champion_conditional_gates.parquet")
    res.to_csv(REPORT / "champion_conditional_gates.csv", index=False)

    # ------------- survivor deep-dive: collinearity vs champion & regime
    print("\n" + "=" * 100)
    print("SURVIVOR SCREEN (need: OOS>champ_OOS, wf_pos>=0.6, wf_min>0, perm_p<0.10, DSR>0.95, exposure>=5%)")
    print("=" * 100)
    champ_oos_s = sharpe(champ_ret[oos_mask])
    surv = res[(res.OOS_Sharpe > champ_oos_s) & (res.wf_pos_frac >= 0.6) &
               (res.wf_min > 0) & (res.perm_p < 0.10) & (res.DSR_oos > 0.95) &
               (res.exposure >= 0.05)].copy()
    if len(surv) == 0:
        print("  無存活組合. 沒有條件閘門在 walk-forward + 嚴格 DSR 下勝過 champion 單獨.")
    else:
        for _, r in surv.iterrows():
            g = gates[r.gate].fillna(False)
            gated = (champ_fire & g)
            # collinearity of the gate-on flag with champion-fire and with bull regime
            corr_champ = np.corrcoef(g.astype(float), champ_fire.astype(float))[0, 1]
            corr_bull = np.corrcoef(g.astype(float).fillna(0), bull.astype(float).fillna(0))[0, 1]
            print(f"  SURVIVOR {r.gate}: OOS={r.OOS_Sharpe:+.2f} vs champ {champ_oos_s:+.2f}  "
                  f"corr(gate,champ_fire)={corr_champ:+.2f}  corr(gate,bull)={corr_bull:+.2f}")

    print(f"\n-> {OUTDIR / 'champion_conditional_gates.parquet'}")
    print(f"-> {REPORT / 'champion_conditional_gates.csv'}")


if __name__ == "__main__":
    main()
