#!/usr/bin/env python3
"""chip-orthogonal-rebuild 步驟 3：正交分群 + AND 閘門（H-REBUILD-2）。

輸入：reports/research/chip-orthogonal-rebuild/panel.pkl（A 面板，裁決勝方）
輸出：reports/research/chip-orthogonal-rebuild/and_gate.json

裁決後存活者（|neutral_t|>=3，A 值）：
  F1 z1 −5.20 · F2 zp −3.29（F1/F2 同源借券系統→群內只留 F1）
  F6 retail −4.56 · F7 margin +8.52（覆蓋偏誤警示）· F8 inst +3.75

步驟：
  (2) 兩兩逐日橫斷面 Spearman rank rho（日均）；|rho|>=0.5 或同資料源→同群，
      群內留 |t| 最大者。
  (3) 存活正交對 AND 閘門：長腳=兩因子皆落各自「偏多側」20% 分位、
      短腳=皆落「偏空側」20%；日 spread=mean r_oc(long)−mean r_oc(short)，
      NW(lag5) t；基準=同一交集樣本上「最好單一成分」的符號調整五分位價差。
      贏 = 價差更大 且 t >= 單因子 t 的 80%。另報 OR 閘門與等權平均（描述性）。

方向（偏多側）：F1 低、F6 低（負向因子）；F7 高、F8 高（正向因子）。
"""
from __future__ import annotations

import json
from itertools import combinations
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "reports/research/chip-orthogonal-rebuild"
PANEL = OUT_DIR / "panel.pkl"
NW_LAG, MIN_DAY_N, MIN_LEG_N = 5, 30, 3
Q = 0.2   # 偏多/偏空側分位

# 存活者（裁決後 A 值）：col → (id, neutral_t, direction, source)
SURV = {
    "z1":     ("F1", -5.20, -1, "twse_sbl(借券)"),
    "zp":     ("F2", -3.29, -1, "twse_sbl(借券)"),
    "retail": ("F6", -4.56, -1, "tdcc/finmind(集保週頻)"),
    "margin": ("F7", +8.52, +1, "twse_mi_margn/finmind(融資)"),
    "inst":   ("F8", +3.75, +1, "twse_t86/tpex(法人)"),
}
SAME_SOURCE_GROUPS = [["z1", "zp"]]   # 借券系統同源


def nw_t(x):
    x = np.asarray(x, float); x = x[~np.isnan(x)]; n = len(x)
    if n < 20: return np.nan, np.nan
    m = x.mean(); e = x - m; s = (e @ e) / n
    for k in range(1, NW_LAG + 1):
        s += 2 * (1 - k / (NW_LAG + 1)) * ((e[k:] @ e[:-k]) / n)
    return m, m / np.sqrt(max(s, 1e-18) / n)


def daily_spearman(df, a, b):
    rhos = []
    for _, g in df.groupby("trade_date", sort=True):
        g = g.dropna(subset=[a, b])
        if len(g) < MIN_DAY_N: continue
        ra, rb = g[a].rank().to_numpy(), g[b].rank().to_numpy()
        sa, sb = ra.std(), rb.std()
        if sa == 0 or sb == 0: continue
        rhos.append(float(np.corrcoef(ra, rb)[0, 1]))
    return (float(np.mean(rhos)), len(rhos)) if rhos else (np.nan, 0)


def quintile_spread_signed(g, col, direction):
    """符號調整五分位價差（bull 分位 − bear 分位），比例。"""
    br = g[col].rank(method="first", pct=True)
    if direction < 0: br = 1.0 - br
    q5, q1 = g.r_oc[br > 0.8], g.r_oc[br <= 0.2]
    if len(q5) == 0 or len(q1) == 0: return np.nan
    return float(q5.mean() - q1.mean())


def pair_gates(df, a, b):
    ida, ta, da, _ = SURV[a]
    idb, tb, db, _ = SURV[b]
    and_sp, or_sp, wavg_sp = [], [], []
    n_long, n_short, n_int, n_skipped = [], [], [], 0
    single_sp = {a: [], b: []}
    for _, g in df.groupby("trade_date", sort=True):
        g = g.dropna(subset=[a, b, "r_oc"])
        if len(g) < MIN_DAY_N: continue
        bra = g[a].rank(method="first", pct=True)
        brb = g[b].rank(method="first", pct=True)
        if da < 0: bra = 1.0 - bra
        if db < 0: brb = 1.0 - brb
        bull_a, bear_a = bra > 1 - Q, bra <= Q
        bull_b, bear_b = brb > 1 - Q, brb <= Q
        # AND
        L, S = g.r_oc[bull_a & bull_b], g.r_oc[bear_a & bear_b]
        n_int.append(len(g))
        if len(L) >= MIN_LEG_N and len(S) >= MIN_LEG_N:
            and_sp.append(float(L.mean() - S.mean()))
            n_long.append(len(L)); n_short.append(len(S))
        else:
            n_skipped += 1
        # OR（衝突股＝一因子偏多另一偏空 → 兩腳都剔除）
        Lo = g.r_oc[(bull_a | bull_b) & ~(bear_a | bear_b)]
        So = g.r_oc[(bear_a | bear_b) & ~(bull_a | bull_b)]
        if len(Lo) >= MIN_LEG_N and len(So) >= MIN_LEG_N:
            or_sp.append(float(Lo.mean() - So.mean()))
        # 等權平均 rank 五分位
        comp = (bra + brb) / 2.0
        cr = comp.rank(method="first", pct=True)
        Lw, Sw = g.r_oc[cr > 0.8], g.r_oc[cr <= 0.2]
        if len(Lw) and len(Sw):
            wavg_sp.append(float(Lw.mean() - Sw.mean()))
        # 單因子基準（同一交集樣本）
        single_sp[a].append(quintile_spread_signed(g, a, da))
        single_sp[b].append(quintile_spread_signed(g, b, db))

    and_m, and_t = nw_t(and_sp)
    or_m, or_t = nw_t(or_sp)
    w_m, w_t = nw_t(wavg_sp)
    sa_m, sa_t = nw_t(single_sp[a])
    sb_m, sb_t = nw_t(single_sp[b])
    # 最好單一成分 = 交集樣本上符號調整價差較大者
    if np.nan_to_num(sa_m) >= np.nan_to_num(sb_m):
        best_id, best_m, best_t = ida, sa_m, sa_t
    else:
        best_id, best_m, best_t = idb, sb_m, sb_t
    beats = bool(np.isfinite(and_m) and np.isfinite(best_m)
                 and and_m > best_m and np.isfinite(and_t)
                 and and_t >= 0.8 * best_t)
    return {
        "pair": f"{ida}x{idb}",
        "n_days_intersection": len(n_int),
        "avg_intersection_n": round(float(np.mean(n_int)), 1) if n_int else 0,
        "and": {"spread_pct": round(and_m * 100, 4) if np.isfinite(and_m) else None,
                "t": round(and_t, 2) if np.isfinite(and_t) else None,
                "n_days": len(and_sp), "n_days_skipped_thin_cell": n_skipped,
                "avg_n_long": round(float(np.mean(n_long)), 1) if n_long else 0,
                "avg_n_short": round(float(np.mean(n_short)), 1) if n_short else 0},
        "or": {"spread_pct": round(or_m * 100, 4) if np.isfinite(or_m) else None,
               "t": round(or_t, 2) if np.isfinite(or_t) else None,
               "n_days": len(or_sp)},
        "wavg": {"spread_pct": round(w_m * 100, 4) if np.isfinite(w_m) else None,
                 "t": round(w_t, 2) if np.isfinite(w_t) else None,
                 "n_days": len(wavg_sp)},
        "singles_on_intersection": {
            ida: {"spread_pct": round(sa_m * 100, 4), "t": round(sa_t, 2)},
            idb: {"spread_pct": round(sb_m * 100, 4), "t": round(sb_t, 2)}},
        "best_single": {"id": best_id, "spread_pct": round(best_m * 100, 4),
                        "t": round(best_t, 2)},
        "beats_best_single": beats,
    }


def main():
    panel = pd.read_pickle(PANEL)
    uni = panel[panel["in_universe"]].dropna(subset=["r_oc"]).copy()
    cols = list(SURV)

    # (2) 兩兩逐日 Spearman
    corr = {}
    for a, b in combinations(cols, 2):
        rho, nd = daily_spearman(uni, a, b)
        corr[f"{SURV[a][0]}({a})x{SURV[b][0]}({b})"] = {
            "rho_daily_mean": round(rho, 4), "n_days": nd}
        print(f"rho {SURV[a][0]}x{SURV[b][0]} = {rho:+.3f} ({nd}d)")

    # 分群：同源 or |rho|>=0.5
    drop = set()
    reasons = {}
    for grp in SAME_SOURCE_GROUPS:
        keep = max(grp, key=lambda c: abs(SURV[c][1]))
        for c in grp:
            if c != keep:
                drop.add(c)
                reasons[SURV[c][0]] = f"同源(借券)群內 |t| 低於 {SURV[keep][0]}"
    for a, b in combinations(cols, 2):
        key = f"{SURV[a][0]}({a})x{SURV[b][0]}({b})"
        if abs(corr[key]["rho_daily_mean"]) >= 0.5 and a not in drop and b not in drop:
            loser = min((a, b), key=lambda c: abs(SURV[c][1]))
            drop.add(loser)
            reasons[SURV[loser][0]] = f"|rho|>=0.5 與 {SURV[a if loser==b else b][0]}"
    ortho = [c for c in cols if c not in drop]
    print("正交存活:", [SURV[c][0] for c in ortho], "剔除:", reasons)

    # (3) AND 閘門：所有正交對
    gates = [pair_gates(uni, a, b) for a, b in combinations(ortho, 2)]

    out = {
        "input": "panel.pkl（A 面板；DISPUTED 裁決 A 勝，見 dispute_probe.json）",
        "survivors_adjudicated": {SURV[c][0]: {"col": c, "neutral_t": SURV[c][1],
                                               "direction": SURV[c][2],
                                               "source": SURV[c][3]} for c in cols},
        "pairwise_daily_spearman": corr,
        "grouping": {"dropped": reasons,
                     "orthogonal_set": [SURV[c][0] for c in ortho]},
        "gate_spec": {"quantile_side": Q, "min_leg_n": MIN_LEG_N,
                      "min_day_n": MIN_DAY_N, "nw_lag": NW_LAG,
                      "win_rule": "AND 價差 > 最好單一成分交集價差 且 t >= 0.8×單因子 t"},
        "gates": gates,
    }
    (OUT_DIR / "and_gate.json").write_text(json.dumps(out, ensure_ascii=False, indent=2))
    for gres in gates:
        print(f"{gres['pair']}: AND {gres['and']['spread_pct']}%/d t={gres['and']['t']} "
              f"nL={gres['and']['avg_n_long']} nS={gres['and']['avg_n_short']} "
              f"| best {gres['best_single']['id']} {gres['best_single']['spread_pct']}% "
              f"t={gres['best_single']['t']} | beats={gres['beats_best_single']} "
              f"| OR t={gres['or']['t']} wavg t={gres['wavg']['t']}")
    print("→", OUT_DIR / "and_gate.json")


if __name__ == "__main__":
    main()
