#!/usr/bin/env python3
"""M4-Step4：事件叢集結構、有效樣本數、與「同族群 N 天內不再進場」的機制檢定.

三個問題：
  (1) 53 筆 L1H7 事件的持有期互相重疊多少？月度聚合後的有效樣本是多少？
  (2) 「同族群第幾筆」是否有 dose-response（第1筆 > 第2筆 > 第3筆+）？
      —— 這是去重規則有沒有機制、還是曲線擬合的分水嶺。
  (3) 族群定義（同股票 / 官方產業 / 電子複合體 / 報酬相關分群）哪個才有訊號？

DB 唯讀。用法：
  PYTHONPATH=src .venv/bin/python scripts/research/songshan_m4_clustering_and_effective_n.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np
import pandas as pd
from scipy import stats

from stock_db import DEFAULT_DB_PATH
from stock_db.connection import connect_ro

OUT_DIR = ROOT / "reports" / "research" / "branch-footprint-screen"
FEAT = OUT_DIR / "songshan_m4_event_features_true_history.csv"
N_PERM = 20_000
SEED = 20260817


def layer_stats(v: np.ndarray) -> dict:
    if len(v) == 0:
        return {"n": 0}
    return {"n": int(len(v)), "mean_pct": round(float(np.mean(v)), 3),
            "median_pct": round(float(np.median(v)), 3),
            "win_rate_pct": round(float((v > 0).mean() * 100), 1)}


def perm_mean_diff(vals: np.ndarray, mask: np.ndarray, n_perm: int = N_PERM) -> float:
    hi, lo = vals[mask], vals[~mask]
    if len(hi) < 2 or len(lo) < 2:
        return float("nan")
    obs = abs(float(np.mean(hi) - np.mean(lo)))
    rng = np.random.default_rng(SEED)
    cnt = 0
    for _ in range(n_perm):
        i = rng.permutation(len(vals))
        cnt += abs(np.mean(vals[i[:len(hi)]]) - np.mean(vals[i[len(hi):]])) >= obs - 1e-12
    return round((cnt + 1) / (n_perm + 1), 5)


def bh_fdr(pairs):
    items = sorted([(k, p) for k, p in pairs if p == p], key=lambda x: x[1])
    n = len(items)
    out, run = {}, float("inf")
    for k, q in reversed([(k, p * n / (i + 1)) for i, (k, p) in enumerate(items)]):
        run = min(run, q)
        out[k] = round(min(run, 1.0), 5)
    return out


def main() -> int:
    conn = connect_ro(DEFAULT_DB_PATH)
    df = pd.read_csv(FEAT, dtype={"stock_id": str}).sort_values("signal_date").reset_index(drop=True)
    cal = [r[0] for r in conn.execute(
        "SELECT trade_date FROM stock_daily_bars WHERE stock_id='2330' AND source='finmind' "
        "AND trade_date BETWEEN '2024-07-01' AND '2026-08-17' AND close>0 ORDER BY trade_date")]
    ci = {d: i for i, d in enumerate(cal)}
    out: dict = {}

    # 報酬相關分群（120 日日報酬 · 單連結 · corr>=0.6），用 Step3 的 FinMind 價格快取
    px = pd.read_parquet(OUT_DIR / "songshan_m4_finmind_price_cache.parquet")
    cl_close = px.pivot_table(index="trade_date", columns="stock_id",
                              values="close", aggfunc="last").sort_index().tail(121)
    cmat = cl_close.pct_change().corr()
    parent = {s: s for s in cmat.columns}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    ids = list(cmat.columns)
    for a_i, a in enumerate(ids):
        for b in ids[a_i + 1:]:
            v = cmat.loc[a, b]
            if pd.notna(v) and v >= 0.6:
                ra, rb = find(a), find(b)
                if ra != rb:
                    parent[ra] = rb
    roots: dict[str, int] = {}
    cc = {s: roots.setdefault(find(s), len(roots)) for s in ids}
    df["corr_cluster"] = df["stock_id"].map(lambda s: f"C{cc.get(s, -1)}")

    print("=" * 96)
    print("(1) 持有期重疊 / 有效樣本數")
    print("=" * 96)
    iv = [(pd.Timestamp(a), pd.Timestamp(b)) for a, b in zip(df["entry_date"], df["exit_date"])]
    n = len(iv)
    conc = np.zeros(n)
    for i in range(n):
        for j in range(n):
            s, e = max(iv[i][0], iv[j][0]), min(iv[i][1], iv[j][1])
            conc[i] += ((e - s).days + 1) > 0
    df["ym"] = df["signal_date"].str[:7]
    mo = df.groupby("ym")["r_adj_pct"].agg(["size", "mean"])
    t, p = stats.ttest_1samp(mo["mean"], 0)
    w = stats.wilcoxon(mo["mean"])
    ov = {
        "n_events": int(n),
        "mean_concurrent_positions": round(float(conc.mean()), 2),
        "pct_overlapping_another": round(float((conc > 1).mean() * 100), 1),
        "n_signal_months": int(len(mo)),
        "monthly_mean_pct": round(float(mo["mean"].mean()), 3),
        "monthly_t_p": round(float(p), 4),
        "monthly_wilcoxon_p": round(float(w.pvalue), 4),
        "max_events_in_one_month": int(mo["size"].max()),
    }
    print(json.dumps(ov, ensure_ascii=False, indent=1))
    print(mo.round(3).to_string())
    out["overlap_and_effective_n"] = ov
    out["monthly_table"] = json.loads(mo.reset_index().to_json(orient="records"))

    print("\n" + "=" * 96)
    print("(2)(3) 同族群第幾筆的 dose-response")
    print("=" * 96)

    def ordinal(key: str, win: int) -> list[int]:
        res = []
        for _, r in df.iterrows():
            t0 = ci[r["signal_date"]]
            prior = df[(df[key] == r[key]) & (df["signal_date"] < r["signal_date"])]
            res.append(1 + sum(1 for d in prior["signal_date"] if t0 - ci[d] <= win))
        return res

    ord_out = {}
    for key, win in [("stock_id", 10), ("stock_id", 20), ("industry", 10),
                     ("industry", 20), ("elec_bucket", 20), ("corr_cluster", 20)]:
        col = ordinal(key, win)
        df[f"ord_{key}_{win}"] = col
        s = pd.Series(col)
        tbl = {}
        for k, sub in df.groupby(s.clip(upper=3).values):
            tbl[f"ordinal_{int(k)}{'+' if k == 3 else ''}"] = layer_stats(sub["r_adj_pct"].to_numpy())
        m = (s == 1).to_numpy()
        pv = perm_mean_diff(df["r_adj_pct"].to_numpy(), m)
        rec = {"group": key, "window_trading_days": win, "by_ordinal": tbl,
               "first": layer_stats(df.loc[m, "r_adj_pct"].to_numpy()),
               "rest": layer_stats(df.loc[~m, "r_adj_pct"].to_numpy()),
               "p_first_vs_rest_two_sided": pv}
        ord_out[f"{key}_win{win}"] = rec
        print(f"\n--- {key} / {win} 交易日 ---")
        for k, v in tbl.items():
            print(f"  {k}: {v}")
        print(f"  首筆 {rec['first']['mean_pct']:+.2f}% (n={rec['first']['n']}) vs "
              f"後續 {rec['rest']['mean_pct']:+.2f}% (n={rec['rest']['n']})  perm p={pv}")
    q = bh_fdr([(k, v["p_first_vs_rest_two_sided"]) for k, v in ord_out.items()])
    for k, v in ord_out.items():
        v["q_bh"] = q.get(k)
    print("\n[BH-FDR over %d ordinal variants]" % len(ord_out),
          json.dumps({k: v["q_bh"] for k, v in ord_out.items()}, ensure_ascii=False))
    out["ordinal_dose_response"] = ord_out

    print("\n" + "=" * 96)
    print("(4) 規則模擬：同官方產業 20 交易日內已進場過就跳過")
    print("=" * 96)
    keep, last = [], {}
    for r in df.itertuples(index=False):
        i = ci[r.signal_date]
        g = r.industry
        if g in last and (i - last[g]) <= 20:
            keep.append(False)
            continue
        keep.append(True)
        last[g] = i
    df["kept_ind20"] = keep
    k = df[df["kept_ind20"]]["r_adj_pct"].to_numpy()
    d = df[~df["kept_ind20"]]["r_adj_pct"].to_numpy()
    print("kept:", layer_stats(k), "\ndropped:", layer_stats(d))
    print("被擋掉的事件：")
    print(df[~df["kept_ind20"]][["signal_date", "stock_id", "stock_name", "industry",
                                 "close_px", "r_adj_pct"]].to_string(index=False))
    out["rule_industry_gap20"] = {"kept": layer_stats(k), "dropped": layer_stats(d),
                                  "kept_pct": round(float(np.mean(keep)) * 100, 1)}

    df.to_csv(OUT_DIR / "songshan_m4_event_features_final.csv", index=False)
    (OUT_DIR / "songshan_m4_clustering.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[OK] → {OUT_DIR / 'songshan_m4_clustering.json'}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
