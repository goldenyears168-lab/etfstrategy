#!/usr/bin/env python3
"""【底部雙/三確認疊加】事件級研究 — tracker 現有雙確認燈的正式驗證。

經濟假設(唯一一條,非暴力枚舉):
  真正的底需要「多個獨立的投降/紓緩訊號同時對齊」,任一單獨訊號都易假訊號。
  疊加的邊際價值 = 三/四確認 vs 各單一確認 vs 隨機同曝險。

四條腿(皆有經濟邏輯):
  CAP  融資投降  : margin_bal 單日巨量失血 → z60(日變動) 深負 (量的投降)
  COV  外資回補  : fut_foreign_doi 連 >=2 日 > 0 (領先資金回補)
  VIXF VIX紓緩   : 近5日出現恐慌尖峰、今日自峰回落且下降 (恐懼消退)
  MC   維持率追繳: 扣除ETF維持率 < 130 (價的投降/斷頭) — 僅 2024-07+ 短樣本

長史層 (2018-06+): CAP × COV × VIXF (三腿皆有全史)
短史層 (2024-07+): + MC 第四腿 (3崩盤,定性+permutation)

fwd 報酬避前視: 訊號於 t 日收盤成立 → t+1 開盤進場 → t+h 收盤出場
  fwd_h[t] = ix_close[t+h] / ix_open[t+1] - 1

紀律:
  - 誠實回報所有試過的設定 (TRIALS)。Deflated-Sharpe 用全部 trial 數懲罰。
  - Walk-forward 擴張窗檢查符號一致性 (稀疏事件→誠實標注零事件折)。
  - permutation 同曝險。
  - 每個存活者答: 是不是只是 champion(外資期貨positioning) / 動能 的重述?
"""
from __future__ import annotations
import numpy as np, pandas as pd
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
RNG = np.random.default_rng(20260731)
NPERM = 5000
TRIALS: list[str] = []   # 誠實記錄每一個評估過的 (config × horizon)


def log_trial(tag: str):
    TRIALS.append(tag)


# ----------------------------------------------------------------------------- build
def build():
    p = pd.read_parquet(ROOT / 'data/research/chip_macro/panel.parquet').copy()
    p['date'] = pd.to_datetime(p['date'])
    g = pd.read_parquet(ROOT / 'data/research/dashboard/global_macro_data.parquet')[['date', 'VIX']].copy()
    g['date'] = pd.to_datetime(g['date'])
    df = p.merge(g, on='date', how='left').sort_values('date').reset_index(drop=True)

    # 前視安全的 fwd: t+1 開盤 → t+h 收盤
    for h in (5, 10, 20):
        df[f'fwd{h}'] = df['ix_close'].shift(-h) / df['ix_open'].shift(-1) - 1

    # --- CAP 融資投降: margin_bal 日變動 z60 深負
    d = df['margin_bal'].pct_change()
    z = (d - d.rolling(60).mean()) / d.rolling(60).std()
    df['margin_z60'] = z

    # --- COV 外資期貨回補: fut_foreign_doi 連 >=2 日 > 0
    pos = (df['fut_foreign_doi'] > 0).fillna(False).values
    streak = np.zeros(len(df), int)
    for i in range(len(df)):
        streak[i] = streak[i-1] + 1 if i > 0 and pos[i] else (1 if pos[i] else 0)
    df['cover_streak'] = streak
    df['COV'] = df['cover_streak'] >= 2

    # --- VIXF: 近5日恐慌尖峰 + 今日自峰回落且下降
    vix = df['VIX']
    r60m, r60s = vix.rolling(60).mean(), vix.rolling(60).std()
    peak5 = vix.rolling(5).max()
    spike_recent = peak5 >= (r60m + 1.5 * r60s)
    fade_now = (vix < vix.shift(1)) & (vix <= 0.85 * peak5)
    df['VIXF'] = (spike_recent & fade_now).fillna(False)

    # --- champion regime (共線檢查用): 外資期貨OI z60 > 0
    foi = df['fut_foreign_oi']
    df['champ_z60'] = (foi - foi.rolling(60).mean()) / foi.rolling(60).std()
    df['CHAMP'] = df['champ_z60'] > 0
    # --- MA200 上彎 regime (共線檢查用)
    ma200 = df['ix_close'].rolling(200).mean()
    df['REGIME'] = (df['ix_close'] > ma200) & (ma200 > ma200.shift(20))
    return df


# ----------------------------------------------------------------------------- stats
def perm_p(mask: pd.Series, fwd: pd.Series) -> float:
    v = fwd.notna() & mask.notna()
    pool = fwd[v].values
    mk = mask[v].values.astype(bool)
    k = int(mk.sum())
    if k < 3 or len(pool) - k < 3:
        return np.nan
    obs = pool[mk].mean()
    cnt = sum(RNG.choice(pool, k, replace=False).mean() >= obs for _ in range(NPERM))
    return (cnt + 1) / (NPERM + 1)


def overlay_sharpe(df: pd.DataFrame, mask: pd.Series, h: int) -> float:
    """把事件當成 timing overlay: 訊號成立 → 持有接下來 h 日 (t+1 開盤起)。
    以每日 log 疊加後年化 Sharpe (重疊事件平均曝險)。稀疏事件→僅作次要穩健度。"""
    n = len(df)
    exposure = np.zeros(n)
    idx = np.where(mask.fillna(False).values)[0]
    for i in idx:
        s, e = i + 1, min(i + h, n - 1)
        if s <= e:
            exposure[s:e+1] += 1
    daily = df['ix_close'].pct_change().shift(-0).values  # ix_close[t]/ix_close[t-1]-1 realised on day t
    # 使用開→收? 簡化: overlay 用日收益, exposure 標準化為 0/1 (是否在持倉)
    inpos = (exposure > 0).astype(float)
    r = np.nan_to_num(daily) * inpos
    if inpos.sum() < 20:
        return np.nan
    mu, sd = r[inpos > 0].mean(), r[inpos > 0].std()
    if sd == 0:
        return np.nan
    return mu / sd * np.sqrt(252)


def deflated_sharpe(sr: float, n_obs: int, n_trials: int) -> float:
    """Deflated Sharpe Ratio (Bailey & Lopez de Prado). 回傳 DSR (機率 SR>0)。
    以 n_trials 個試驗中最大期望 SR 為 benchmark。假設常態化簡版。"""
    if not np.isfinite(sr) or n_obs < 30:
        return np.nan
    from scipy.stats import norm
    # 期望最大 SR (from N trials, iid N(0,1) daily→年化), 用近似 E[max]
    emax = (1 - np.euler_gamma) * norm.ppf(1 - 1.0/max(n_trials,1)) + \
           np.euler_gamma * norm.ppf(1 - 1.0/(max(n_trials,1)*np.e))
    sr_d = sr / np.sqrt(252)           # 反年化成日 SR benchmark 尺度
    sr0 = emax / np.sqrt(n_obs)        # benchmark 日 SR 門檻
    dsr = norm.cdf((sr_d - sr0) * np.sqrt(n_obs - 1))
    return dsr


def summ(df, mask, tag, horizons=(5, 10, 20)):
    m = mask.fillna(False)
    n = int(m.sum())
    if n == 0:
        print(f"  {tag:32s}: 無事件")
        return {}
    out = {}
    line = f"  {tag:32s} (n={n:3d}):"
    for h in horizons:
        log_trial(f"{tag}|h{h}")
        s = df[f'fwd{h}'][m & df[f'fwd{h}'].notna()]
        p = perm_p(m, df[f'fwd{h}'])
        line += f"  h{h} {s.mean()*100:+.2f}%/命中{(s>0).mean()*100:2.0f}%/p={p:.3f}"
        out[h] = (s.mean(), (s > 0).mean(), p, len(s))
    print(line)
    return out


# ----------------------------------------------------------------------------- walk-forward
def walk_forward(df, mask, h, folds=3):
    """擴張窗 3 折: 各折內事件的 fwd 均值符號一致性。稀疏→誠實標零事件折。"""
    m = mask.fillna(False)
    dates = df['date']
    edges = pd.date_range(dates.min(), dates.max(), periods=folds+1)
    signs = []
    detail = []
    for i in range(folds):
        w = (dates >= edges[i]) & (dates <= edges[i+1])
        s = df[f'fwd{h}'][w & m & df[f'fwd{h}'].notna()]
        if len(s) < 3:
            detail.append(f"F{i+1}(n={len(s)}:—)")
            continue
        mu = s.mean()
        signs.append(np.sign(mu))
        detail.append(f"F{i+1}(n={len(s)}:{mu*100:+.1f}%)")
    consistent = len(signs) >= 2 and all(x == signs[0] for x in signs) and signs[0] > 0
    return consistent, "  ".join(detail)


# ----------------------------------------------------------------------------- main
def main():
    df = build()
    span = f"{df['date'].min().date()}→{df['date'].max().date()}"
    print("=" * 100)
    print(f"【底部多重確認疊加】長史層 CAP×COV×VIXF   n={len(df)}  {span}")
    for h in (5, 10, 20):
        b = df[f'fwd{h}']
        print(f"  Baseline fwd{h} = {b.mean()*100:+.2f}% (命中 {(b>0).mean()*100:.0f}%)")
    print()

    # ---- 單一確認 (benchmark)
    print("[單一確認]")
    CAP15 = df['margin_z60'] < -1.5
    CAP20 = df['margin_z60'] < -2.0
    r_cap15 = summ(df, CAP15, "CAP 融資投降 z60<-1.5")
    r_cap20 = summ(df, CAP20, "CAP 融資投降 z60<-2.0")
    r_cov = summ(df, df['COV'], "COV 外資期貨回補≥2日")
    r_vixf = summ(df, df['VIXF'], "VIXF VIX尖峰後回落")

    # ---- 雙確認
    print("\n[雙確認]")
    r_cc = summ(df, CAP15 & df['COV'],  "CAP×COV")
    r_cv = summ(df, CAP15 & df['VIXF'], "CAP×VIXF")
    r_ov = summ(df, df['COV'] & df['VIXF'], "COV×VIXF")

    # ---- 三確認
    print("\n[三確認]")
    TRIPLE = CAP15 & df['COV'] & df['VIXF']
    r_tri = summ(df, TRIPLE, "CAP×COV×VIXF (三確認)")
    # 放寬窗版本: 三腿在 ±3 日窗內任一日成立 (事件對齊本就不會同一天)
    def within(sig, w=3):
        return sig.rolling(2*w+1, center=True, min_periods=1).max().astype(bool)
    TRIPLE_W = within(CAP15) & within(df['COV']) & within(df['VIXF'])
    r_triw = summ(df, TRIPLE_W, "CAP×COV×VIXF (±3日窗對齊)")

    # ---- walk-forward on candidates
    print("\n[Walk-forward 擴張窗 3折 符號一致性] (fwd10, 也看 fwd20)")
    CAPCOV = CAP15 & df['COV']
    for tag, mk in [("CAP×COV ★", CAPCOV),
                    ("COV單獨", df['COV']),
                    ("CAP單獨", CAP15),
                    ("三確認±3日窗", TRIPLE_W)]:
        ok10, d10 = walk_forward(df, mk, 10)
        ok20, d20 = walk_forward(df, mk, 20)
        print(f"  {tag:14s} h10 {'一致↑' if ok10 else '不一致 '}  {d10}")
        print(f"  {' ':14s} h20 {'一致↑' if ok20 else '不一致 '}  {d20}")

    # ---- 共線/殘差: 是不是 champion / regime 的重述?
    print("\n[共線檢查: 疊加訊號 vs champion(外資期貨positioning) / MA200-regime]")
    for tag, mk in [("CAP×COV ★", CAPCOV), ("COV", df['COV']), ("CAP", CAP15),
                    ("三±3窗", TRIPLE_W)]:
        m = mk.fillna(False)
        n = int(m.sum())
        if n == 0:
            continue
        p_champ = df['CHAMP'][m].mean()
        p_reg = df['REGIME'][m].mean()
        base_champ = df['CHAMP'].mean(); base_reg = df['REGIME'].mean()
        print(f"  {tag:10s}(n={n:3d}): 落在champ-on {p_champ*100:.0f}%(基{base_champ*100:.0f}%) | "
              f"落在regime-on {p_reg*100:.0f}%(基{base_reg*100:.0f}%)")

    # ---- 決定性條件測試: CAP×COV 在 champion-OFF 時是否仍有效?
    #      若 CAP×COV 只是 champion 重述 → champ-OFF 子集應失效; 若仍強 → 增量真實。
    print("\n[決定性: CAP×COV × champion狀態 — 是否只是champion重述?] fwd10/fwd20")
    for cond, lbl in [(df['CHAMP'], "champ-ON"), (~df['CHAMP'], "champ-OFF")]:
        summ(df, CAPCOV & cond.fillna(False), f"CAP×COV & {lbl}", horizons=(10, 20))
    # CAP 提供的增量: COV∩champ-off vs (CAP×COV)∩champ-off
    print("[增量: champ-OFF 內, 加不加 CAP 融資投降腿]")
    summ(df, df['COV'] & (~df['CHAMP']).fillna(False), "COV & champ-OFF (無CAP)", horizons=(10, 20))
    summ(df, CAPCOV & (~df['CHAMP']).fillna(False), "CAP×COV & champ-OFF (有CAP)", horizons=(10, 20))

    # ---- overlay Sharpe + DSR (次要穩健度; 稀疏事件保留)
    print("\n[Overlay Sharpe + Deflated-Sharpe] (h=10 持倉overlay; DSR懲罰=全trial數)")
    n_trials = len(TRIALS)
    cands = [("CAP×COV ★", CAPCOV), ("COV", df['COV']), ("CAP", CAP15),
             ("三±3窗", TRIPLE_W)]
    dsr_rows = []
    for tag, mk in cands:
        sr = overlay_sharpe(df, mk, 10)
        n_obs = int((mk.fillna(False)).sum()) * 10
        dsr = deflated_sharpe(sr, n_obs, n_trials) if np.isfinite(sr) else np.nan
        print(f"  {tag:10s}: overlay SR={sr:.2f}  DSR={dsr:.3f} (n_trials={n_trials})")
        dsr_rows.append((tag, sr, dsr))

    # ==================================================================== 短史層 MC
    print("\n" + "=" * 100)
    ex = pd.read_csv(ROOT / 'data/research/chip_macro/margin_maintenance_exetf.csv')
    ex['date'] = pd.to_datetime(ex['date'])
    ex = ex.rename(columns={'maint': 'maint_ex'})
    d2 = df.merge(ex[['date', 'maint_ex']], on='date', how='inner').reset_index(drop=True)
    # 短史需重算 fwd (合併後 index 變)
    for h in (5, 10, 20):
        d2[f'fwd{h}'] = d2['ix_close'].shift(-h) / d2['ix_open'].shift(-1) - 1
    print(f"【第四腿 MC 維持率追繳】短史層 n={len(d2)}  {d2['date'].min().date()}→{d2['date'].max().date()} (3崩盤)")
    for h in (5, 10, 20):
        b = d2[f'fwd{h}']
        print(f"  Baseline fwd{h} = {b.mean()*100:+.2f}%")
    print("[單/多確認 短史]")
    CAP15b = d2['margin_z60'] < -1.5
    for X in (140, 130):
        summ(d2, d2['maint_ex'] < X, f"MC 維持率<{X}")
    summ(d2, (d2['maint_ex'] < 130) & d2['COV'], "MC<130 × COV")
    summ(d2, (d2['maint_ex'] < 130) & d2['VIXF'], "MC<130 × VIXF")
    summ(d2, (d2['maint_ex'] < 130) & CAP15b, "MC<130 × CAP")
    QUAD = (d2['maint_ex'] < 130) & d2['COV'] & d2['VIXF']
    summ(d2, QUAD, "MC<130 × COV × VIXF (三重)")
    QUAD4 = (d2['maint_ex'] < 130) & d2['COV'] & d2['VIXF'] & CAP15b
    summ(d2, QUAD4, "四確認 MC×COV×VIXF×CAP")

    # 三崩盤定性
    print("\n[三崩盤 各確認燈時序]")
    for name, s, e in [('2024/08', '2024-07-25', '2024-08-30'),
                       ('2025/04', '2025-03-25', '2025-05-05'),
                       ('2026/07', '2026-07-10', '2026-07-29')]:
        w = d2[(d2['date'] >= s) & (d2['date'] <= e)]
        mmin = w['maint_ex'].min()
        n_mc = int((w['maint_ex'] < 130).sum())
        n_cov = int(w['COV'].sum()); n_vixf = int(w['VIXF'].sum()); n_cap = int((w['margin_z60'] < -1.5).sum())
        print(f"  {name}: 維持率谷底{mmin:.1f}%  |燈次數 MC<130:{n_mc} COV:{n_cov} VIXF:{n_vixf} CAP:{n_cap}")

    last = d2.iloc[-1]
    print(f"\n[當下] 維持率{last['maint_ex']:.1f}% margin_z60={last['margin_z60']:+.2f} "
          f"外資回補{int(last['cover_streak'])}日 VIXF={'✓' if last['VIXF'] else '✗'}")

    print(f"\n>>> 誠實 TRIALS 總數 = {len(TRIALS)}")
    return df, d2, dsr_rows


if __name__ == '__main__':
    main()
