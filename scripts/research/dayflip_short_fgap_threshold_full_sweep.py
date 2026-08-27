#!/usr/bin/env python3
"""短邊fgap門檻的完整比較：固定門檻掃描(3~12%) vs 自適應(門檻隨夜盤動能調整).

背景：使用者說「應該是各種都比較看看，因為畢竟做空還是自適應比較符合邏輯」。
之前只在既有的all_trades.csv/single_pick_tradelog.csv（本身已經用6%門檻篩過）
上面做濾網式的post-hoc檢驗，沒辦法回答「如果門檻本身設成別的值會怎樣」——
因為低於6%的候選根本沒被記錄下來。這裡重新對74個訊號日呼叫
build_candidates()（正式production函式，不套用跳空門檻）拿到完整候選池，
自己算每檔的fgap，才能真的掃描不同門檻。

方法：
  1) 對74個訊號日重建完整候選池(含fgap<6%的)，用futures_daily_cache.json
     的T0+1開盤價/收盤價/最低價算fgap跟模擬短邊出場（開盤進場、-2%觸價
     回補或收盤強制平倉，5bps成本——跟pick_signal()/single_pick_tradelog.csv
     同一套規則）。
  2) 固定門檻掃描：3/4/5/6/7/8/9/10/12%，每個門檻每天用
     pick_rule=smallest_qualifying_gap（門檻以上最小跳空）挑一檔，
     walk-forward(70/30)驗證。
  3) 自適應門檻：effective_threshold(day) = base + k×夜盤動能(T0)，
     掃描(base,k)組合，同樣walk-forward驗證，跟固定門檻掃描出的最佳值比較。

PYTHONPATH=src:scripts/research .venv/bin/python scripts/research/dayflip_short_fgap_threshold_full_sweep.py
"""

from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

import numpy as np

from order.dayflip_short_signal import build_candidates, last_close

ROOT = Path(__file__).resolve().parents[2]
SHORT_TRADELOG_CSV = ROOT / "reports/research/branch-footprint-screen/dayflip_gapup_short/single_pick_tradelog.csv"
FUT_CACHE_PATH = ROOT / "reports/research/branch-footprint-screen/dayflip_gapup_short/futures_daily_cache.json"
TX_BARS_DB = Path.home() / "goldenstocks-data" / "cache" / "tmf_channel" / "bars.sqlite"
TX_SOURCE = "tx_1m_tick_built_582d"

COVER_TARGET_PCT = 0.02
ROUND_TRIP_COST_PCT = 0.05
FIXED_THRESHOLDS_PCT = (3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 12.0)
ADAPTIVE_BASE_PCT = (4.0, 5.0, 6.0, 7.0)
ADAPTIVE_K = (0.0, 0.5, 1.0, 1.5)  # effective_threshold = base + k*night_return
MIN_TRAIN_N = 20  # 這裡樣本天生比原本74筆單一挑選更稀疏（有些門檻某些天沒有合格候選），降低守門標準但仍要求最小樣本


def night_return_pct(con: sqlite3.Connection, t0: str) -> float:
    day_close = con.execute(
        "SELECT c FROM bars WHERE source=? AND sess='day' AND day=? ORDER BY t DESC LIMIT 1",
        (TX_SOURCE, t0),
    ).fetchone()
    night_close = con.execute(
        "SELECT c FROM bars WHERE source=? AND sess='night' AND day=? ORDER BY t DESC LIMIT 1",
        (TX_SOURCE, t0),
    ).fetchone()
    if not day_close or not night_close:
        return 0.0
    dc = float(day_close[0])
    if dc <= 0:
        return 0.0
    return (float(night_close[0]) / dc - 1) * 100


def main() -> None:
    with SHORT_TRADELOG_CSV.open(encoding="utf-8") as f:
        signal_dates = sorted({row["signal_date"] for row in csv.DictReader(f)})

    fut_cache = json.loads(FUT_CACHE_PATH.read_text(encoding="utf-8"))
    tx_con = sqlite3.connect(f"file:{TX_BARS_DB}?mode=ro", uri=True)

    print(f"=== 重建{len(signal_dates)}個訊號日的完整候選池（含fgap<6%）===")
    day_pool: dict[str, list[dict]] = {}
    day_night_return: dict[str, float] = {}
    for i, t0 in enumerate(signal_dates):
        candidates = build_candidates(t0)
        day_night_return[t0] = night_return_pct(tx_con, t0)
        rows = []
        for c in candidates:
            t0_close = last_close(c.stock_id, t0)
            m = fut_cache.get(c.stock_id) or {}
            # trade_date(T0+1) 需要從calendar找——用fut_cache自己排序的日期序列找t0之後最近一筆
            dates_sorted = sorted(m)
            if t0 not in dates_sorted:
                continue
            idx = dates_sorted.index(t0)
            if idx + 1 >= len(dates_sorted):
                continue
            t01 = dates_sorted[idx + 1]
            row = m.get(t01)
            if not row or t0_close is None or t0_close <= 0:
                continue
            open_px, close_px, _high, low_px = float(row[0]), float(row[1]), float(row[2]), float(row[3])
            if open_px <= 0:
                continue
            fgap = (open_px / t0_close - 1) * 100
            target = open_px * (1 - COVER_TARGET_PCT)
            if low_px <= target:
                exit_px, how = target, "觸價回補"
            else:
                exit_px, how = close_px, "收盤平倉"
            net_ret = (open_px - exit_px) / open_px * 100 - ROUND_TRIP_COST_PCT
            rows.append({
                "stock": c.stock_id, "n_seats": c.n_seats, "fgap": fgap,
                "net_ret_pct": net_ret, "how": how,
            })
        day_pool[t0] = rows
        if (i + 1) % 20 == 0:
            print(f"  進度 {i + 1}/{len(signal_dates)}")

    total_candidates = sum(len(v) for v in day_pool.values())
    print(f"完成，總候選數(不分門檻): {total_candidates}\n")

    split_idx = int(len(signal_dates) * 0.7)
    train_dates = signal_dates[:split_idx]
    test_dates = signal_dates[split_idx:]

    def pick_and_simulate(dates: list[str], threshold_fn) -> list[float]:
        """threshold_fn(t0) -> 當天有效門檻%；每天挑門檻以上最小fgap那筆."""
        rets = []
        for t0 in dates:
            th = threshold_fn(t0)
            qualifying = [r for r in day_pool.get(t0, []) if r["fgap"] >= th]
            if not qualifying:
                continue
            picked = min(qualifying, key=lambda r: r["fgap"])
            rets.append(picked["net_ret_pct"])
        return rets

    def metrics(rets: list[float]) -> dict:
        if not rets:
            return {"n": 0}
        arr = np.array(rets)
        return {
            "n": len(arr), "win_rate": float(np.mean(arr > 0)),
            "mean_ret_pct": float(arr.mean()), "std": float(arr.std()),
            "sharpe_like": float(arr.mean() / arr.std()) if arr.std() > 0 else float("nan"),
        }

    print("=== A) 固定門檻掃描（walk-forward 70/30）===")
    print(f"{'門檻%':>6} | {'訓練n':>6} {'訓練勝率':>8} {'訓練均pnl':>10} {'訓練sharpe':>10} | "
          f"{'測試n':>6} {'測試勝率':>8} {'測試均pnl':>10} {'測試sharpe':>10}")
    fixed_results = {}
    for th in FIXED_THRESHOLDS_PCT:
        train_rets = pick_and_simulate(train_dates, lambda t0, th=th: th)
        test_rets = pick_and_simulate(test_dates, lambda t0, th=th: th)
        m_train, m_test = metrics(train_rets), metrics(test_rets)
        fixed_results[th] = (m_train, m_test)
        print(f"{th:>6.1f} | {m_train.get('n',0):>6} {m_train.get('win_rate',0)*100:>7.0f}% "
              f"{m_train.get('mean_ret_pct',0):>+9.3f}% {m_train.get('sharpe_like',float('nan')):>10.3f} | "
              f"{m_test.get('n',0):>6} {m_test.get('win_rate',0)*100:>7.0f}% "
              f"{m_test.get('mean_ret_pct',0):>+9.3f}% {m_test.get('sharpe_like',float('nan')):>10.3f}")

    eligible = [th for th in FIXED_THRESHOLDS_PCT if fixed_results[th][0].get("n", 0) >= MIN_TRAIN_N]
    if not eligible:
        eligible = list(FIXED_THRESHOLDS_PCT)
    best_fixed = max(eligible, key=lambda th: fixed_results[th][0].get("sharpe_like", -999) or -999)
    print(f"\n訓練期選出最佳固定門檻: {best_fixed}%（測試期表現見上表）\n")

    print("=== B) 自適應門檻掃描：effective = base + k×夜盤動能（walk-forward）===")
    print(f"{'base%':>6} {'k':>5} | {'訓練n':>6} {'訓練均pnl':>10} {'訓練sharpe':>10} | "
          f"{'測試n':>6} {'測試均pnl':>10} {'測試sharpe':>10}")
    adaptive_results = {}
    for base in ADAPTIVE_BASE_PCT:
        for k in ADAPTIVE_K:
            def th_fn(t0, base=base, k=k):
                return base + k * day_night_return.get(t0, 0.0)
            train_rets = pick_and_simulate(train_dates, th_fn)
            test_rets = pick_and_simulate(test_dates, th_fn)
            m_train, m_test = metrics(train_rets), metrics(test_rets)
            adaptive_results[(base, k)] = (m_train, m_test)
            print(f"{base:>6.1f} {k:>5.1f} | {m_train.get('n',0):>6} "
                  f"{m_train.get('mean_ret_pct',0):>+9.3f}% {m_train.get('sharpe_like',float('nan')):>10.3f} | "
                  f"{m_test.get('n',0):>6} {m_test.get('mean_ret_pct',0):>+9.3f}% "
                  f"{m_test.get('sharpe_like',float('nan')):>10.3f}")

    eligible_a = [k for k, v in adaptive_results.items() if v[0].get("n", 0) >= MIN_TRAIN_N]
    if not eligible_a:
        eligible_a = list(adaptive_results.keys())
    best_adaptive = max(eligible_a, key=lambda k: adaptive_results[k][0].get("sharpe_like", -999) or -999)
    print(f"\n訓練期選出最佳自適應組合: base={best_adaptive[0]}% k={best_adaptive[1]}\n")

    print("=== 最終對照：現行6%固定 vs 訓練期選出的最佳固定 vs 訓練期選出的最佳自適應（全部看測試期樣本外）===")
    m6 = metrics(pick_and_simulate(test_dates, lambda t0: 6.0))
    m_best_fixed = fixed_results[best_fixed][1]
    m_best_adaptive = adaptive_results[best_adaptive][1]
    print(f"現行6%固定:        n={m6.get('n',0):>3} 均pnl={m6.get('mean_ret_pct',0):+.3f}% "
          f"sharpe_like={m6.get('sharpe_like',float('nan')):.3f}")
    print(f"最佳固定({best_fixed}%):   n={m_best_fixed.get('n',0):>3} "
          f"均pnl={m_best_fixed.get('mean_ret_pct',0):+.3f}% "
          f"sharpe_like={m_best_fixed.get('sharpe_like',float('nan')):.3f}")
    print(f"最佳自適應({best_adaptive[0]}%+{best_adaptive[1]}×夜盤): n={m_best_adaptive.get('n',0):>3} "
          f"均pnl={m_best_adaptive.get('mean_ret_pct',0):+.3f}% "
          f"sharpe_like={m_best_adaptive.get('sharpe_like',float('nan')):.3f}")

    print(
        "\n⚠️ 限制：\n"
        "  1) 候選池是重新對DB呼叫build_candidates()當場重建，跟all_trades.csv/\n"
        "     single_pick_tradelog.csv是獨立的兩次計算，數字可能有微小落差\n"
        "     （例如分點資料若有更新），但方法論一致。\n"
        "  2) 部分門檻/自適應組合在某些天可能完全沒有合格候選(qualifying為空)，\n"
        "     被跳過，不同門檻之間的n因此不完全可比——已在表格裡列出n方便判斷。\n"
        "  3) walk-forward只切一次70/30，不是k-fold交叉驗證，訓練期選出的\n"
        "     『最佳』本身就有overfitting風險，這裡的重點是『測試期樣本外\n"
        "     表現差不差』，不是訓練期數字本身。\n"
        "  4) 這是對pick_rule=smallest_qualifying_gap這個規則本身做門檻掃描，\n"
        "     沒有同時測試換掉排序規則（例如改用n_seats最多、或別的排序法）。"
    )


if __name__ == "__main__":
    main()
