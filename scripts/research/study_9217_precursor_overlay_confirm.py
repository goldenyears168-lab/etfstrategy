#!/usr/bin/env python3
"""9217 決定性訊號 × whale_chip_precursor W3 overlay confirm/veto 檢定.

背景（item AL）：whale_chip_precursor 研究（reports/research/whale_chip_precursor/
FROZEN_PRECURSOR_RULES.md，規則 W3）在 Tier-A 股票（2327/2303/3443/6669/2383/2408/
3189/3037）上找到：T-2 `foreign_sum_5d > 0 AND three_streak >= 2`（外資+自營+投信三大法人
淨買超金額 5 日 rolling sum；三大法人合計淨買連續天數）對照事件 vs 安靜日 lift ≈1.87x，
但日曆級 precision 僅 0.09-0.12，因此文件明文「禁止當主訊號」，只能疊加在一個已有
precision 的主訊號上做 confirm/veto。

這支腳本第一次把 W3 疊在 9217（凱基-松山）自己的決定性訊號上測（
reports/research/branch-footprint-screen/whale_9217_5dnet95_trades.csv,
scan_5d_net95 rising-edge, n=36, L1H7 protocol COST=0.003 HOLD=7 BETA=1.15 bench=IX0001,
r_adj_pct 欄位）：這個訊號脈絡跟 W3 原本測試的 Tier-A 情境不同——9217 訊號本身已有
不錯的 precision（full-sample win_rate 66.7%），這裡是測 W3 能否在一個已經不弱的訊號上
「錦上添花」，而不是像原研究那樣去救一個弱訊號。

方法：對每一筆 9217 事件，重用 research.chen_chip.features.build_chip_feature_frame
（跟 whale_chip_precursor enriched runner 完全一樣的 three_streak / foreign_sum_5d
計算）算出該 stock_id 全歷史的 chip features，用 trading calendar 找出 T-2（T=signal_date），
PIT-safe 讀 foreign_sum_5d[T-2] 與 three_streak[T-2]，判定 W3 是否成立，
分 confirmed vs not-confirmed 兩桶比較 forward r_adj_pct（mean/median/win-rate），
並用 permutation test（差值檢定）估 p-value。

Read-only DB. 不動 order/strategy config.

輸出：
  reports/research/whale_precursor_9217_overlay/9217_w3_overlay_join.csv
  reports/research/whale_precursor_9217_overlay/9217_w3_overlay_summary.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np
import pandas as pd

from research.chen_chip.adapters_db import connect_ro, load_calendar  # noqa: E402
from research.chen_chip.features import build_chip_feature_frame  # noqa: E402

TRADES_CSV = ROOT / "reports/research/branch-footprint-screen/whale_9217_5dnet95_trades.csv"
OUT_DIR = ROOT / "reports/research/whale_precursor_9217_overlay"
OUT_DIR.mkdir(parents=True, exist_ok=True)
OUT_JOIN_CSV = OUT_DIR / "9217_w3_overlay_join.csv"
OUT_SUMMARY_JSON = OUT_DIR / "9217_w3_overlay_summary.json"

FEAT_D0 = "2024-01-01"  # well before earliest signal (2025-02-19); gives streak/rolling calcs stable history
FEAT_D1 = "2026-08-01"


def log(m: str) -> None:
    print(f"[9217-w3-overlay] {m}", flush=True)


def permutation_pvalue(vals: pd.Series, mask: pd.Series, n_perm: int = 20000, seed: int = 42) -> float | None:
    """Two-sided permutation test on the mean-difference between mask==True vs False groups."""
    v = vals[mask.notna() & vals.notna()]
    m = mask[mask.notna() & vals.notna()]
    if m.nunique() < 2 or len(v) < 4:
        return None
    obs = v[m].mean() - v[~m].mean()
    rng = np.random.default_rng(seed)
    arr = v.to_numpy()
    n_true = int(m.to_numpy().sum())
    idx_all = np.arange(len(arr))
    diffs = np.empty(n_perm)
    for i in range(n_perm):
        perm_idx = rng.permutation(idx_all)
        grp = perm_idx[:n_true]
        other = perm_idx[n_true:]
        diffs[i] = arr[grp].mean() - arr[other].mean()
    p = float((np.abs(diffs) >= abs(obs)).mean())
    return p


def summarize(sub: pd.Series) -> dict:
    v = sub.dropna()
    n = len(v)
    if n == 0:
        return {"n": 0, "mean_pct": None, "median_pct": None, "win_rate_pct": None}
    return {
        "n": n,
        "mean_pct": round(float(v.mean()), 3),
        "median_pct": round(float(v.median()), 3),
        "win_rate_pct": round(float((v > 0).mean()) * 100, 1),
    }


def main() -> int:
    trades = pd.read_csv(TRADES_CSV, dtype={"stock_id": str})
    log(f"loaded {len(trades)} 9217 decisive trades from {TRADES_CSV.name}")

    conn = connect_ro()
    calendar = load_calendar(conn, FEAT_D0, FEAT_D1)
    idx = {d: i for i, d in enumerate(calendar)}
    log(f"calendar: {len(calendar)} trading days {calendar[0]}..{calendar[-1]}")

    stock_ids = sorted(trades["stock_id"].unique().tolist())
    log(f"building chip feature frame for {len(stock_ids)} stock_ids: {stock_ids}")
    feat = build_chip_feature_frame(conn, stock_ids, FEAT_D0, FEAT_D1)
    conn.close()
    if feat.empty:
        raise RuntimeError("build_chip_feature_frame returned empty — check stock_institutional_daily coverage")
    log(f"feat frame: {len(feat)} rows, {feat['sid'].nunique()} distinct sid")

    feat_map = {(r["sid"], r["d"]): r for _, r in feat.iterrows()}

    rows = []
    for _, r in trades.iterrows():
        sid = r["stock_id"]
        sig = r["signal_date"]
        if sig not in idx:
            log(f"  SKIP {sid} {sig}: signal_date not in calendar")
            continue
        i0 = idx[sig]
        if i0 - 2 < 0:
            log(f"  SKIP {sid} {sig}: not enough history for T-2")
            continue
        t2 = calendar[i0 - 2]
        key = (sid, t2)
        frow = feat_map.get(key)
        if frow is None:
            log(f"  SKIP {sid} {sig}: no chip feature row at T-2={t2}")
            continue
        foreign_sum_5d = frow.get("foreign_sum_5d")
        three_streak = frow.get("three_streak")
        w3_confirm = bool(
            pd.notna(foreign_sum_5d) and pd.notna(three_streak)
            and float(foreign_sum_5d) > 0 and float(three_streak) >= 2
        )
        rows.append({
            "signal_date": sig,
            "stock_id": sid,
            "t2_date": t2,
            "foreign_sum_5d_t2": foreign_sum_5d,
            "three_streak_t2": three_streak,
            "w3_confirm": w3_confirm,
            "entry_date": r["entry_date"],
            "exit_date": r["exit_date"],
            "r_pct": r["r_pct"],
            "r_ix_pct": r["r_ix_pct"],
            "r_adj_pct": r["r_adj_pct"],
        })

    join_df = pd.DataFrame(rows)
    join_df.to_csv(OUT_JOIN_CSV, index=False)
    log(f"wrote {OUT_JOIN_CSV} n={len(join_df)} (dropped {len(trades) - len(join_df)} for missing T-2 data)")

    vals = join_df["r_adj_pct"]
    mask_confirm = join_df["w3_confirm"] == True  # noqa: E712

    s_confirm = summarize(vals[mask_confirm])
    s_not = summarize(vals[~mask_confirm])
    p = permutation_pvalue(vals, mask_confirm)
    s_all = summarize(vals)

    # Wilcoxon rank-sum (Mann-Whitney) as a second, distribution-free check
    from scipy import stats as sstats
    mw_p = None
    if s_confirm["n"] >= 2 and s_not["n"] >= 2:
        try:
            mw = sstats.mannwhitneyu(
                vals[mask_confirm].dropna(), vals[~mask_confirm].dropna(), alternative="two-sided"
            )
            mw_p = float(mw.pvalue)
        except Exception as exc:  # noqa: BLE001
            log(f"mannwhitneyu failed: {exc}")

    summary = {
        "protocol": {
            "primary_signal": "9217 scan_5d_net95 rising-edge, n=36, L1H7 (cost=0.003 hold=7 beta=1.15 bench=IX0001)",
            "overlay_rule": "W3: T-2 foreign_sum_5d>0 AND three_streak>=2 (reused verbatim from "
                             "research.chen_chip.features.build_chip_feature_frame, whale_chip_precursor origin)",
            "metric": "r_adj_pct (L1H7 beta-adjusted excess return vs IX0001)",
        },
        "n_trades_total": int(len(trades)),
        "n_joined": int(len(join_df)),
        "n_dropped_missing_t2": int(len(trades) - len(join_df)),
        "all_events_baseline": s_all,
        "w3_confirmed": s_confirm,
        "w3_not_confirmed": s_not,
        "permutation_p_value_diff_of_means": p,
        "mannwhitney_p_value": mw_p,
    }
    OUT_SUMMARY_JSON.write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    log(f"wrote {OUT_SUMMARY_JSON}")
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
