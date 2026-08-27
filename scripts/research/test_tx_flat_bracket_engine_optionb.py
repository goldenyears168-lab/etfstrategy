#!/usr/bin/env python3
"""選項B（ATR倍數停損）對帳測試——批次vs因果83天bit-for-bit等價性 + 對帳不變量。
結構照抄選項A的測試（`test_tx_flat_bracket_engine.py`），只換成 k_stop/k_target 網格。

跑法：
  PYTHONPATH=src .venv/bin/python -W ignore scripts/research/test_tx_flat_bracket_engine_optionb.py
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
from tx_flat_bracket_causal_optionb import run_causal_block_optionb  # noqa: E402
from tx_flat_bracket_engine_optionb import simulate_bracket_block_optionb  # noqa: E402

PARAM_SETS = [
    (10.0, 20.0, 999, "B k_stop10 ratio2.0"),
    (15.0, 15.0, 999, "B k_stop15 ratio1.0"),
    (20.0, 15.0, 60, "B k_stop20 較短time_stop"),
]
WINDOWS = [34, 89]


def main() -> None:
    days = load_days()
    all_bars = {d: load_day_bars_with_sess(d) for d in days}
    stripped = {d: all_bars[d][["Datetime", "Open", "High", "Low", "Close", "Volume"]] for d in days}
    atr_threshold = compute_global_atr_threshold(days, stripped)
    print(f"樣本：{len(days)}天，ATR門檻={atr_threshold:.2f}\n")

    print("=== (a) 對帳不變量 ===")
    ok_a = True
    for k_stop, k_target, tsb, label in PARAM_SETS:
        agg = dict(n_events=0, n_entered=0, n_skipped_in_position=0)
        n_trades = 0
        for day in days:
            for sess in ("day", "night"):
                seg = all_bars[day][all_bars[day]["sess"] == sess].reset_index(drop=True)
                if seg.empty:
                    continue
                for w in WINDOWS:
                    trades, stats = simulate_bracket_block_optionb(seg, w, atr_threshold, k_stop, k_target, tsb)
                    n_trades += len(trades)
                    for k in agg:
                        agg[k] += stats[k]
        ok = agg["n_events"] == agg["n_entered"] + agg["n_skipped_in_position"] and agg["n_entered"] == n_trades
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}: {agg} trades={n_trades}")
        ok_a = ok_a and ok

    print("\n=== (b) 批次版 vs 因果版 83天等價性 ===")
    n_blocks = n_match = 0
    mismatches = []
    for k_stop, k_target, tsb, label in PARAM_SETS:
        for day in days:
            for sess in ("day", "night"):
                seg = all_bars[day][all_bars[day]["sess"] == sess].reset_index(drop=True)
                if seg.empty:
                    continue
                for w in WINDOWS:
                    n_blocks += 1
                    bt, bs = simulate_bracket_block_optionb(seg, w, atr_threshold, k_stop, k_target, tsb)
                    ct, cs = run_causal_block_optionb(seg, w, atr_threshold, k_stop, k_target, tsb)
                    match = (
                        len(bt) == len(ct)
                        and bs == cs
                        and all(abs(a["pnl"] - b["pnl"]) < 1e-6 and a["reason"] == b["reason"] for a, b in zip(bt, ct))
                    )
                    if match:
                        n_match += 1
                    else:
                        mismatches.append((label, day, sess, w))
    ok_b = n_match == n_blocks
    print(f"  [{'PASS' if ok_b else 'FAIL'}] {n_match}/{n_blocks} 區塊完全一致")
    if mismatches:
        print(f"  不一致（前10）: {mismatches[:10]}")

    print(f"\n=== 結果：不變量={'PASS' if ok_a else 'FAIL'}  等價={'PASS' if ok_b else 'FAIL'} ===")
    if not (ok_a and ok_b):
        sys.exit(1)


if __name__ == "__main__":
    main()
