#!/usr/bin/env python3
"""M4-Step2：Minervini/Weinstein 濾網是不是「價格歷史長度」的代理變數？

songshan_m4_regime_quality_filters.py 發現 F1（Minervini 7 條全過）分層極強
（+6.2% vs -2.2%，勝率 75% vs 32%）。但 vectorized_minervini_criteria_count 對
歷史不足的股票會把 NaN 比較壓成 0 → 條件數機械性偏低。6449 鈺邦在 DB 裡只有
2026-04-20 起 82 根 K 線（255 檔補價只補了近期），所以它必然 crit=0。

本腳本把「歷史長度」拆出來當獨立變數，重測：
  G0：訊號日前可用 K 線 >=200 根 vs 不足（純資料涵蓋度）
  F1'：只在歷史充足子集內，Minervini 7/7 vs 未達（真正的技術面分層）
並對通過的子群跑 random-timing permutation（同股票隨機時機 null）。

DB 唯讀。用法：
  PYTHONPATH=src .venv/bin/python scripts/research/songshan_m4_coverage_confound_check.py
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np
import pandas as pd

from stock_db import DEFAULT_DB_PATH
from stock_db.connection import connect_ro
from research.branch_signal_validation import build_l1h7_signal_dict, permutation_test

OUT_DIR = ROOT / "reports" / "research" / "branch-footprint-screen"
SCRIPTS = ROOT / "scripts" / "research"
FEAT = OUT_DIR / "songshan_m4_event_features.csv"
N_PERM = 20_000
SEED = 20260817

MGEN = None


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def layer_stats(v: np.ndarray) -> dict:
    if len(v) == 0:
        return {"n": 0}
    return {"n": int(len(v)), "mean_pct": round(float(np.mean(v)), 3),
            "median_pct": round(float(np.median(v)), 3),
            "win_rate_pct": round(float((v > 0).mean() * 100), 1)}


def perm_diff(vals: np.ndarray, mask: np.ndarray, n_perm: int = N_PERM) -> dict:
    hi, lo = vals[mask], vals[~mask]
    if len(hi) < 2 or len(lo) < 2:
        return {"note": "layer too small"}
    om, omd = float(np.mean(hi) - np.mean(lo)), float(np.median(hi) - np.median(lo))
    rng = np.random.default_rng(SEED)
    cm = cmd = 0
    for _ in range(n_perm):
        idx = rng.permutation(len(vals))
        a, b = vals[idx[:len(hi)]], vals[idx[len(hi):]]
        cm += abs(np.mean(a) - np.mean(b)) >= abs(om) - 1e-12
        cmd += abs(np.median(a) - np.median(b)) >= abs(omd) - 1e-12
    return {"diff_mean_pp": round(om, 3), "diff_median_pp": round(omd, 3),
            "p_mean_two_sided": round((cm + 1) / (n_perm + 1), 5),
            "p_median_two_sided": round((cmd + 1) / (n_perm + 1), 5)}


def main() -> int:
    global MGEN
    MGEN = _load_module("mgen", SCRIPTS / "study_whale_branch_5d_net95_live_signal_validation.py")
    conn = connect_ro(DEFAULT_DB_PATH)
    df = pd.read_csv(FEAT, dtype={"stock_id": str})

    # --- 每個事件在訊號日前有幾根 K 線（DB 實際涵蓋度） ---
    nb = []
    for r in df.itertuples(index=False):
        n = conn.execute(
            "SELECT COUNT(*) FROM stock_daily_bars WHERE stock_id=? AND source='finmind' "
            "AND close>0 AND trade_date<=?", (r.stock_id, r.signal_date)
        ).fetchone()[0]
        first = conn.execute(
            "SELECT MIN(trade_date) FROM stock_daily_bars WHERE stock_id=? AND source='finmind' AND close>0",
            (r.stock_id,)
        ).fetchone()[0]
        nb.append((n, first))
    df["bars_before"] = [x[0] for x in nb]
    df["first_bar"] = [x[1] for x in nb]

    vals = df["r_adj_pct"].to_numpy()
    out = {}

    print("=" * 96)
    print("(A) 歷史長度 vs Minervini 條件數 交叉表")
    print("=" * 96)
    df["hist_ok"] = df["bars_before"] >= 200
    print(pd.crosstab(df["hist_ok"], df["minervini_crit"]))
    print("\n歷史不足的事件：")
    print(df[~df["hist_ok"]][["signal_date", "stock_id", "stock_name", "bars_before",
                              "first_bar", "minervini_crit", "wstage", "r_adj_pct"]].to_string(index=False))

    print("\n" + "=" * 96)
    print("(B) G0：資料涵蓋度本身就是分層嗎？")
    print("=" * 96)
    g0 = {"layer_hist_ok": layer_stats(vals[df["hist_ok"].to_numpy()]),
          "layer_hist_short": layer_stats(vals[~df["hist_ok"].to_numpy()]),
          **perm_diff(vals, df["hist_ok"].to_numpy())}
    print(json.dumps(g0, ensure_ascii=False, indent=1))
    out["G0_history_coverage"] = g0

    print("\n" + "=" * 96)
    print("(C) F1'：只在歷史充足子集內重測 Minervini 7/7")
    print("=" * 96)
    sub = df[df["hist_ok"]].reset_index(drop=True)
    sv = sub["r_adj_pct"].to_numpy()
    m = (sub["minervini_crit"] >= 7).to_numpy()
    f1 = {"subset_n": int(len(sub)),
          "layer_pass7": layer_stats(sv[m]), "layer_fail": layer_stats(sv[~m]),
          **perm_diff(sv, m)}
    print(json.dumps(f1, ensure_ascii=False, indent=1))
    out["F1_prime_minervini_within_hist_ok"] = f1

    m3 = (sub["wstage"] >= 2).to_numpy()
    f3 = {"subset_n": int(len(sub)),
          "layer_wstage2": layer_stats(sv[m3]), "layer_other": layer_stats(sv[~m3]),
          **perm_diff(sv, m3)}
    print("\nF3' Weinstein stage>=2（歷史充足子集內）:")
    print(json.dumps(f3, ensure_ascii=False, indent=1))
    out["F3_prime_weinstein_within_hist_ok"] = f3

    m6 = (sub["close_px"] <= sub["close_px"].median()).to_numpy()
    f6 = {"subset_n": int(len(sub)),
          "layer_px_lo": layer_stats(sv[m6]), "layer_px_hi": layer_stats(sv[~m6]),
          **perm_diff(sv, m6)}
    print("\nF6' 股價中位數分層（歷史充足子集內）:")
    print(json.dumps(f6, ensure_ascii=False, indent=1))
    out["F6_prime_price_within_hist_ok"] = f6

    print("\n" + "=" * 96)
    print("(D) random-timing permutation：各子群是否勝過同股票的隨機時機")
    print("=" * 96)
    ix_dict = build_l1h7_signal_dict(MGEN.load_ix(conn))
    cache: dict[str, dict] = {}

    def dicts_for(frame: pd.DataFrame) -> dict:
        d = {}
        for sid in sorted(frame["stock_id"].unique()):
            if sid not in cache:
                cache[sid] = build_l1h7_signal_dict(MGEN.load_stock_bars(conn, sid))
            d[sid] = cache[sid]
        return d

    subsets = {
        "base_all": df,
        "hist_ok": df[df["hist_ok"]],
        "minervini7_all": df[df["minervini_crit"] >= 7],
        "minervini7_within_hist_ok": sub[sub["minervini_crit"] >= 7],
        "wstage2_within_hist_ok": sub[sub["wstage"] >= 2],
        "px_le_median_all": df[df["close_px"] <= df["close_px"].median()],
        "minervini7_and_px_le_median": df[(df["minervini_crit"] >= 7)
                                          & (df["close_px"] <= df["close_px"].median())],
    }
    perm_out = {}
    for name, frame in subsets.items():
        if len(frame) < 5:
            perm_out[name] = {"n": len(frame), "note": "too small"}
            continue
        res = permutation_test(frame[["stock_id", "signal_date"]], dicts_for(frame),
                               ix_dict, n_perm=N_PERM, seed=SEED)
        perm_out[name] = {
            "n": res["n_events"],
            "obs_mean_pct": round(res["observed_mean_pct"], 3),
            "obs_median_pct": round(res["observed_median_pct"], 3),
            "p_mean_onesided": res["p_value_mean_onesided"],
            "p_median_onesided": res["p_value_median_onesided"],
        }
        print(name, json.dumps(perm_out[name], ensure_ascii=False))
    out["random_timing_permutation"] = perm_out

    # --- 去極值敏感度：F1' 子群 ---
    print("\n" + "=" * 96)
    print("(E) minervini7 子群去極值敏感度")
    print("=" * 96)
    g = df[df["minervini_crit"] >= 7].copy()
    trim = {}
    for k in (0, 3, 5):
        gg = g.assign(a=g["r_adj_pct"].abs()).sort_values("a", ascending=False).iloc[k:]
        trim[f"drop_top{k}"] = layer_stats(gg["r_adj_pct"].to_numpy())
    print(json.dumps(trim, ensure_ascii=False, indent=1))
    out["minervini7_trim"] = trim

    # --- 年度分解：minervini7 子群 ---
    yr = []
    for y, s in g.groupby(g["signal_date"].str[:4]):
        yr.append({"year": y, **layer_stats(s["r_adj_pct"].to_numpy())})
    print("\n[minervini7 年度分解]", json.dumps(yr, ensure_ascii=False))
    out["minervini7_yearly"] = yr

    df.to_csv(OUT_DIR / "songshan_m4_event_features_with_coverage.csv", index=False)
    (OUT_DIR / "songshan_m4_coverage_confound.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[OK] → {OUT_DIR / 'songshan_m4_coverage_confound.json'}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
