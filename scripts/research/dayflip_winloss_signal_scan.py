#!/usr/bin/env python3
"""賺錢/賠錢日子的訊號掃描——使用者要求：(1)按個股拆開看賺賠模式
(2)測試台指期夜盤(T0 15:00-23:59，僅此區間有完整歷史資料，00:00-05:00
缺口見下方caveat)漲跌方向+走勢特徵是否影響T+1績效。

方法：
  1) 個股層級賺賠拆解（純描述性統計，不是IC測試）：對長邊219筆按stock_id
     分組看勝率/均報酬，找出重複表現好/差的個股。
  2) TX夜盤特徵（T0 15:00-23:59）跟T+1績效的IC測試：
     a) 夜盤淨變動%（close-open）
     b) 夜盤最大波動幅度%（high-low）
     c) 夜盤趨勢一致性（正報酬分鐘數佔比）
     每個都做全樣本+walk-forward+permutation test，只測這3個預先選定的
     特徵，避免多重比較。

⚠️ 資料限制：tx_1m_tick_built_582d的night session只涵蓋15:00-23:59，
00:00-05:00這段目前資料庫沒有完整歷史涵蓋（只有2026-08-03之後的
tx_1m_tick_built_fullnight_aug有較完整資料，範圍太窄不能用在74/219筆
歷史樣本上）——這裡測的「夜盤」只是前半段，不是完整夜盤，如果訊號存在
但主要發生在00:00-05:00，這裡會測不到，是已知盲點不是造假。

PYTHONPATH=src:scripts/research .venv/bin/python scripts/research/dayflip_winloss_signal_scan.py
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import numpy as np
from scipy.stats import spearmanr

import stock_db

ROOT = Path(__file__).resolve().parents[2]
LONG_RESULTS = ROOT / "reports/research/dayflip_fgap_calibration/post_dump_long_rolling_dip_results.json"
DAY_POOL = ROOT / "reports/research/dayflip_fgap_calibration/day_pool_full_74d.json"
TX_DB = Path.home() / "goldenstocks-data" / "cache" / "tmf_channel" / "bars.sqlite"
TX_SOURCE = "tx_1m_tick_built_582d"
FGAP_FLOOR_LONG = 4.0


def tx_night_features(tx_con: sqlite3.Connection, t0: str) -> dict | None:
    rows = tx_con.execute(
        "SELECT t, c FROM bars WHERE source=? AND sess='night' AND day=? AND c>0 ORDER BY t",
        (TX_SOURCE, t0),
    ).fetchall()
    if len(rows) < 30:
        return None
    closes = [r[1] for r in rows]
    net_change_pct = (closes[-1] / closes[0] - 1) * 100
    max_c, min_c = max(closes), min(closes)
    range_pct = (max_c - min_c) / closes[0] * 100
    up_minutes = sum(1 for i in range(1, len(closes)) if closes[i] > closes[i - 1])
    down_minutes = sum(1 for i in range(1, len(closes)) if closes[i] < closes[i - 1])
    total_moves = up_minutes + down_minutes
    trend_consistency = (up_minutes / total_moves * 100) if total_moves else 50.0
    return {
        "tx_night_net_change_pct": net_change_pct,
        "tx_night_range_pct": range_pct,
        "tx_night_trend_consistency_pct": trend_consistency,
    }


def walk_forward_ic(pairs: list[tuple], label: str) -> None:
    pairs_sorted = pairs  # 已按呼叫端排序
    n_train = int(len(pairs_sorted) * 0.7)
    train, test = pairs_sorted[:n_train], pairs_sorted[n_train:]
    for sub_label, group in [("全樣本", pairs_sorted), ("train(前70%)", train), ("test(後30%)", test)]:
        if len(group) < 10:
            print(f"  {sub_label}: n={len(group)} 樣本太少跳過")
            continue
        xs = np.array([g[0] for g in group])
        ys = np.array([g[1] for g in group])
        ic, pval = spearmanr(xs, ys)
        print(f"  {sub_label}: n={len(group)} IC={ic:.3f} p={pval:.3f}")

    xs_all = np.array([p[0] for p in pairs_sorted])
    ys_all = np.array([p[1] for p in pairs_sorted])
    real_ic, _ = spearmanr(xs_all, ys_all)
    rng = np.random.default_rng(20260812)
    perm_ics = []
    for _ in range(3000):
        shuffled = rng.permutation(ys_all)
        perm_ic, _ = spearmanr(xs_all, shuffled)
        perm_ics.append(abs(perm_ic))
    perm_p = float(np.mean(np.array(perm_ics) >= abs(real_ic)))
    print(f"  permutation p={perm_p:.3f}\n")


def main() -> None:
    long_trades = json.loads(LONG_RESULTS.read_text(encoding="utf-8"))
    long_sub = [t for t in long_trades if t["fgap"] >= FGAP_FLOOR_LONG]

    print("=" * 70)
    print("第一部分：多單個股層級賺賠拆解（描述性統計）")
    print("=" * 70)
    by_stock: dict[str, list[float]] = {}
    for t in long_sub:
        by_stock.setdefault(t["stock_id"], []).append(t["ret"])
    stock_stats = []
    for sid, rets in by_stock.items():
        if len(rets) < 3:
            continue
        arr = np.array(rets)
        win_rate = float(np.mean(arr > 0)) * 100
        stock_stats.append((sid, len(rets), arr.mean(), win_rate))
    stock_stats.sort(key=lambda x: -x[2])
    print(f"{'股票':<8}{'筆數':>6}{'均報酬%':>10}{'勝率%':>8}")
    print("--- 表現最好(n>=3) ---")
    for sid, n, mean, win in stock_stats[:8]:
        print(f"{sid:<8}{n:>6}{mean:>10.2f}{win:>8.1f}")
    print("--- 表現最差(n>=3) ---")
    for sid, n, mean, win in stock_stats[-8:]:
        print(f"{sid:<8}{n:>6}{mean:>10.2f}{win:>8.1f}")

    print("\n" + "=" * 70)
    print("第二部分：TX夜盤(T0 15:00-23:59)特徵 vs T+1績效（多單子集）")
    print("=" * 70)
    tx_con = sqlite3.connect(f"file:{TX_DB}?mode=ro", uri=True)
    enriched = []
    n_missing = 0
    for t in long_sub:
        feat = tx_night_features(tx_con, t["t0"])
        if feat is None:
            n_missing += 1
            continue
        enriched.append({**t, **feat})
    tx_con.close()
    print(f"可比對: {len(enriched)}/{len(long_sub)}筆 (缺{n_missing}筆，多為夜盤資料不足30根)\n")

    enriched_sorted = sorted(enriched, key=lambda t: (t["entry_day"], t["entry_minute"]))
    for feat_name, feat_label in [
        ("tx_night_net_change_pct", "夜盤淨變動%(收-開)"),
        ("tx_night_range_pct", "夜盤最大波動幅度%"),
        ("tx_night_trend_consistency_pct", "夜盤趨勢一致性%(正報酬分鐘佔比)"),
    ]:
        print(f"--- {feat_label} vs 後續報酬(ret) ---")
        pairs = [(t[feat_name], t["ret"]) for t in enriched_sorted]
        walk_forward_ic(pairs, feat_label)

    print("=" * 70)
    print("第三部分：TX夜盤特徵 vs 空單T+1績效（day_pool，同一套夜盤特徵）")
    print("=" * 70)
    day_pool = json.loads(DAY_POOL.read_text(encoding="utf-8"))
    tx_con = sqlite3.connect(f"file:{TX_DB}?mode=ro", uri=True)
    GAP_RANK_WEIGHT = 0.75

    def net_ret(r, entry_px):
        target = entry_px * 0.98
        exit_px = target if r["low_px"] <= target else r["close_px"]
        return (entry_px - exit_px) / entry_px * 100 - 0.05

    def pick_blend(qual):
        by_gap = sorted(qual, key=lambda r: r["fgap"])
        gap_rank = {id(r): i + 1 for i, r in enumerate(by_gap)}
        by_seats = sorted(qual, key=lambda r: -r["n_seats"])
        seat_rank = {id(r): i + 1 for i, r in enumerate(by_seats)}
        return min(qual, key=lambda r: GAP_RANK_WEIGHT * gap_rank[id(r)] + (1 - GAP_RANK_WEIGHT) * seat_rank[id(r)])

    short_pairs_by_feat: dict[str, list] = {"tx_night_net_change_pct": [], "tx_night_range_pct": [], "tx_night_trend_consistency_pct": []}
    short_dates_order = []
    for t0, pool in sorted(day_pool.items()):
        qual = [r for r in pool if 7.0 <= r["fgap"] < 9.0]
        if not qual:
            continue
        best = pick_blend(qual)
        ret = net_ret(best, best["open_px"])
        feat = tx_night_features(tx_con, t0)
        if feat is None:
            continue
        for k in short_pairs_by_feat:
            short_pairs_by_feat[k].append((feat[k], ret))
        short_dates_order.append(t0)
    tx_con.close()
    n_short = len(short_pairs_by_feat["tx_night_net_change_pct"])
    print(f"可比對: {n_short}筆空單訊號日\n")
    for feat_name, feat_label in [
        ("tx_night_net_change_pct", "夜盤淨變動%(收-開)"),
        ("tx_night_range_pct", "夜盤最大波動幅度%"),
        ("tx_night_trend_consistency_pct", "夜盤趨勢一致性%"),
    ]:
        print(f"--- {feat_label} vs 空單後續報酬 ---")
        walk_forward_ic(short_pairs_by_feat[feat_name], feat_label)


if __name__ == "__main__":
    main()
