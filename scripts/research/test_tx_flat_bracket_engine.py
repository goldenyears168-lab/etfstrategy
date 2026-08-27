#!/usr/bin/env python3
"""Phase 1：flat-default bracket引擎的對帳測試——批次版vs因果版83天bit-for-bit等價性
（比照第十三輪 `tx_channel_causal_engine.py` 的驗證方法論），加對帳不變量斷言，加
Option D（純time-stop，無價格停損）內部一致性檢查（kill criterion 2 的前置健檢）。

跑法：
  PYTHONPATH=src .venv/bin/python -W ignore scripts/research/test_tx_flat_bracket_engine.py
"""
from __future__ import annotations

import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tx_channel_daynight_split import load_day_bars_with_sess  # noqa: E402
from tx_channel_geometry_multiday import load_days  # noqa: E402
from tx_channel_recalibrate import compute_global_atr_threshold  # noqa: E402
from tx_flat_bracket_causal import run_causal_block  # noqa: E402
from tx_flat_bracket_engine import compute_signal_events, simulate_bracket_block  # noqa: E402

PARAM_SETS = [
    # (stop_pts, target_pts, time_stop_bars, label)
    (120.0, 120.0, 999, "A基準 1:1"),
    (80.0, 160.0, 999, "A 1:2"),
    (160.0, 120.0, 60, "A 較短time_stop"),
]
WINDOWS = [34, 89]


def test_reconciliation_invariant(days, all_bars) -> bool:
    """(a) 對帳不變量：每個(day,sess,window,param)區塊 n_events == n_entered + n_skipped，
    且 n_entered == len(trades)。跨所有區塊加總後斷言。"""
    print("=== (a) 對帳不變量（n_events = n_entered + n_skipped_in_position，n_entered = 交易筆數）===")
    all_ok = True
    for stop_pts, target_pts, time_stop_bars, label in PARAM_SETS:
        agg = dict(n_events=0, n_entered=0, n_skipped_in_position=0)
        n_trades = 0
        for day in days:
            day_bars = all_bars[day]
            for sess in ("day", "night"):
                seg = day_bars[day_bars["sess"] == sess].reset_index(drop=True)
                if seg.empty:
                    continue
                for w in WINDOWS:
                    trades, stats = simulate_bracket_block(
                        seg, w, ATR_THRESHOLD, stop_pts, target_pts, time_stop_bars
                    )
                    n_trades += len(trades)
                    for k in agg:
                        agg[k] += stats[k]
        ok = agg["n_events"] == agg["n_entered"] + agg["n_skipped_in_position"] and agg["n_entered"] == n_trades
        status = "PASS" if ok else "FAIL"
        print(
            f"  [{status}] {label}: events={agg['n_events']} entered={agg['n_entered']} "
            f"skipped={agg['n_skipped_in_position']} trades={n_trades}"
        )
        all_ok = all_ok and ok
    return all_ok


def test_batch_vs_causal(days, all_bars) -> bool:
    """(b) 批次版 vs 因果版：83天 × 2session × 2window × 3組參數，逐筆pnl比對必須bit-for-bit一致。"""
    print("\n=== (b) 批次版 vs 因果版 83天等價性 ===")
    n_blocks = 0
    n_match = 0
    mismatches = []
    for stop_pts, target_pts, time_stop_bars, label in PARAM_SETS:
        for day in days:
            day_bars = all_bars[day]
            for sess in ("day", "night"):
                seg = day_bars[day_bars["sess"] == sess].reset_index(drop=True)
                if seg.empty:
                    continue
                for w in WINDOWS:
                    n_blocks += 1
                    batch_trades, batch_stats = simulate_bracket_block(
                        seg, w, ATR_THRESHOLD, stop_pts, target_pts, time_stop_bars
                    )
                    causal_trades, causal_stats = run_causal_block(
                        seg, w, ATR_THRESHOLD, stop_pts, target_pts, time_stop_bars
                    )
                    match = (
                        len(batch_trades) == len(causal_trades)
                        and batch_stats == causal_stats
                        and all(
                            abs(a["pnl"] - b["pnl"]) < 1e-6 and a["reason"] == b["reason"]
                            for a, b in zip(batch_trades, causal_trades)
                        )
                    )
                    if match:
                        n_match += 1
                    else:
                        mismatches.append((label, day, sess, w))
    status = "PASS" if n_match == n_blocks else "FAIL"
    print(f"  [{status}] {n_match}/{n_blocks} 區塊完全一致")
    if mismatches:
        print(f"  不一致區塊（前10個）: {mismatches[:10]}")
    return n_match == n_blocks


def test_option_d_diagnostic(days, all_bars) -> None:
    """(c) Option D：無價格停損（stop=target=極大值），只靠time_stop=999(=session內不會觸發)
    +session_end強制平倉——每筆交易都等於「進場後扛到收盤」。報告勝率/均pnl/P&L集中度，
    跟第十六輪已知的『被丟尾部位』特徵（均-185.1pt、勝率24.5%）做方向性比對（非精確數值
    對照——新架構的進場母體跟舊架構的『每日最後一次翻倉』母體不是同一個populaton，見設計
    文件 kill criterion 2 討論）。"""
    print("\n=== (c) Option D 診斷（純time-stop，健檢用，非採納候選）===")
    huge = 1e9
    all_trades = []
    for day in days:
        day_bars = all_bars[day]
        for sess in ("day", "night"):
            seg = day_bars[day_bars["sess"] == sess].reset_index(drop=True)
            if seg.empty:
                continue
            for w in WINDOWS:
                trades, _ = simulate_bracket_block(seg, w, ATR_THRESHOLD, huge, huge, 999)
                all_trades.extend(trades)
    if not all_trades:
        print("  無交易產生。")
        return
    import pandas as pd

    df = pd.DataFrame(all_trades)
    total = df["pnl"].sum()
    win_rate = (df["pnl"] > 0).mean() * 100
    top5pct_n = max(1, int(len(df) * 0.05))
    top5_share = df.nlargest(top5pct_n, "pnl")["pnl"].sum() / df["pnl"].abs().sum() * 100 if total != 0 else 0.0
    print(f"  交易數={len(df)}  總損益={total:,.1f}pt  均pnl={df['pnl'].mean():.1f}pt  勝率={win_rate:.1f}%")
    print(f"  reason分布:\n{df['reason'].value_counts().to_string()}")
    print(f"  top5%交易佔總絕對損益比例: {top5_share:.1f}%")
    print("  （方向性比對：新架構母體=每次突破事件都進場扛到收盤，跟舊架構『只有最後一次")
    print("  翻倉未平倉』的母體不同，數值不必相等，但都應呈現負skew/低勝率的性質。）")


def main() -> None:
    global ATR_THRESHOLD
    days = load_days()
    all_bars = {d: load_day_bars_with_sess(d) for d in days}
    stripped = {d: all_bars[d][["Datetime", "Open", "High", "Low", "Close", "Volume"]] for d in days}
    ATR_THRESHOLD = compute_global_atr_threshold(days, stripped)
    print(f"樣本：{len(days)}天（{days[0]} ~ {days[-1]}），ATR門檻={ATR_THRESHOLD:.2f}\n")

    ok_a = test_reconciliation_invariant(days, all_bars)
    ok_b = test_batch_vs_causal(days, all_bars)
    test_option_d_diagnostic(days, all_bars)

    print(f"\n=== 結果：對帳不變量={'PASS' if ok_a else 'FAIL'}  批次vs因果等價={'PASS' if ok_b else 'FAIL'} ===")
    if not (ok_a and ok_b):
        print("⚠️ Kill criterion 1 觸發：Phase 1 對帳失敗，不得進入 Phase 2。")
        sys.exit(1)


if __name__ == "__main__":
    main()
