#!/usr/bin/env python3
"""回歸測試：`tx_channel_canonical_engine.py` 收斂了 session-end force-close 記帳缺口修正。

跑法：`PYTHONPATH=src .venv/bin/python -W ignore scripts/research/test_tx_channel_canonical_engine.py`

三項測試（見第十六輪 README 記帳缺口修正的驗收條件）：
  (a) 舊邏輯等價性——canonical引擎排除force-close尾部位後的交易list，要跟舊版
      `simulate_pnl_realistic`（`tx_channel_geometry_realism_check.py`，有bug那版）的
      交易list逐筆完全一致，證明重寫時沒有動到已驗證過的核心邏輯。
  (b) force-close尾部位跟手動修正版一致——跟 `tx_channel_session_end_accounting.py` 的
      `run_block`/`simulate_with_forceclose`（獨立實作）互相對帳。
  (c) 83天基準回歸數字要對上——window=233單獨 → -5,417.8pt；w34+w55+w89併行 → -12,310.3pt
      （第十六輪 README 記錄的修正後數字，day/night分開起算、cooldown=8、use_rsi_exit=False，
      atr_threshold用全部83天池化計算）。
"""

from __future__ import annotations

import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

sys.path.insert(0, str(Path(__file__).resolve().parent))

import tx_channel_canonical_engine as engine  # noqa: E402
from tx_channel_daynight_split import load_day_bars_with_sess  # noqa: E402
from tx_channel_geometry_multiday import load_days  # noqa: E402
from tx_channel_geometry_realism_check import simulate_pnl_realistic  # noqa: E402
from tx_channel_recalibrate import compute_global_atr_threshold  # noqa: E402
from tx_channel_session_end_accounting import run_block as legacy_run_block  # noqa: E402

TOL = 1e-6
WINDOWS_FOR_CROSSCHECK = [34, 55, 89, 233]

results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    status = "PASS" if ok else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail else ""))
    results.append((name, ok, detail))


def _fields_match(a: dict, b: dict) -> bool:
    if a["direction"] != b["direction"]:
        return False
    if a["entry_time"] != b["entry_time"] or a["exit_time"] != b["exit_time"]:
        return False
    for k in ("entry_price", "exit_price", "pnl"):
        if abs(float(a[k]) - float(b[k])) > TOL:
            return False
    return True


def main() -> None:
    print("=== 準備資料：載入83天K棒（day+night, sess-tagged） ===")
    days = load_days()
    print(f"總天數: {len(days)}")
    all_bars = {d: load_day_bars_with_sess(d) for d in days}
    stripped = {d: all_bars[d][["Datetime", "Open", "High", "Low", "Close", "Volume"]] for d in days}
    atr_threshold_full = compute_global_atr_threshold(days, stripped)
    print(f"ATR濾網門檻（83天全樣本池化）: {atr_threshold_full:.2f}\n")

    # 抽樣天數：均勻間隔取15天，涵蓋樣本頭尾
    step = max(1, len(days) // 15)
    sample_days = days[::step][:15]
    if len(sample_days) < 15 and len(days) >= 15:
        sample_days = days[: min(15, len(days))]
    print(f"抽樣天數（測試 a/b 用）: {len(sample_days)} 天 -> {sample_days}\n")

    # --- 測試 (a)：舊邏輯等價性 ---
    print("=== 測試 (a) 舊邏輯等價性：canonical引擎(排除force-close) vs simulate_pnl_realistic ===")
    a_checked_blocks = 0
    a_checked_trades = 0
    a_mismatches = []
    for day in sample_days:
        for sess in ("day", "night"):
            seg = all_bars[day][all_bars[day]["sess"] == sess].reset_index(drop=True)
            if seg.empty:
                continue
            for w in WINDOWS_FOR_CROSSCHECK:
                ds = engine._build_signal(seg, w, atr_threshold_full, cooldown=8, use_rsi_exit=False)
                if ds is None:
                    continue
                _, old_trades = simulate_pnl_realistic(
                    ds, fill_lag_bars=engine.FILL_LAG_BARS, cost_pts_per_trade=engine.COST_PTS_PER_TRADE
                )
                full_trades = engine.simulate_block(seg, w, atr_threshold_full, cooldown=8, use_rsi_exit=False)
                core_trades = [t for t in full_trades if t["reason"] == "signal_exit"]

                a_checked_blocks += 1
                if len(old_trades) != len(core_trades):
                    a_mismatches.append(
                        f"{day}/{sess}/w={w}: 筆數不同 old={len(old_trades)} new={len(core_trades)}"
                    )
                    continue
                for ot, nt in zip(old_trades, core_trades):
                    a_checked_trades += 1
                    if not _fields_match(ot, nt):
                        a_mismatches.append(f"{day}/{sess}/w={w}: 交易欄位不同 old={ot} new={nt}")

    ok_a = len(a_mismatches) == 0 and a_checked_blocks > 0
    detail_a = f"{a_checked_blocks}個區塊、{a_checked_trades}筆交易逐筆比對"
    if a_mismatches:
        detail_a += f"；前3個不符: {a_mismatches[:3]}"
    record("(a) 舊邏輯等價性", ok_a, detail_a)

    # --- 測試 (b)：force-close 尾部位跟手動修正版一致 ---
    print("\n=== 測試 (b) force-close尾部位 vs tx_channel_session_end_accounting.run_block ===")
    b_checked_blocks = 0
    b_mismatches = []
    for day in sample_days:
        for sess in ("day", "night"):
            seg = all_bars[day][all_bars[day]["sess"] == sess].reset_index(drop=True)
            if seg.empty:
                continue
            for w in WINDOWS_FOR_CROSSCHECK:
                legacy_trades, legacy_tail = legacy_run_block(seg, w, atr_threshold_full)
                full_trades = engine.simulate_block(seg, w, atr_threshold_full, cooldown=8, use_rsi_exit=False)

                if not legacy_tail and not full_trades:
                    continue  # 兩邊都判定資料不足，一致，跳過
                if not legacy_tail or not full_trades:
                    b_mismatches.append(
                        f"{day}/{sess}/w={w}: 其中一邊沒有尾部位 legacy={bool(legacy_tail)} new={bool(full_trades)}"
                    )
                    continue

                new_tail = [t for t in full_trades if t["reason"] == "session_end_forceclose"]
                b_checked_blocks += 1
                if len(new_tail) != 1:
                    b_mismatches.append(f"{day}/{sess}/w={w}: canonical引擎尾部位數量非1 -> {len(new_tail)}")
                    continue
                if not _fields_match(legacy_tail[0], new_tail[0]):
                    b_mismatches.append(f"{day}/{sess}/w={w}: 尾部位不符 legacy={legacy_tail[0]} new={new_tail[0]}")

                # 順便對帳已計交易（legacy_run_block 是完全獨立第二份實作，可再交叉驗證一次(a)）
                if len(legacy_trades) != len([t for t in full_trades if t["reason"] == "signal_exit"]):
                    b_mismatches.append(
                        f"{day}/{sess}/w={w}: 已計交易筆數不符 legacy={len(legacy_trades)} "
                        f"new={len([t for t in full_trades if t['reason']=='signal_exit'])}"
                    )

    ok_b = len(b_mismatches) == 0 and b_checked_blocks > 0
    detail_b = f"{b_checked_blocks}個區塊的force-close尾部位逐一比對"
    if b_mismatches:
        detail_b += f"；前3個不符: {b_mismatches[:3]}"
    record("(b) force-close尾部位一致性", ok_b, detail_b)

    # --- 測試 (c)：83天基準回歸數字 ---
    print("\n=== 測試 (c) 83天基準回歸：window=233 與 w34+w55+w89 併行 ===")
    result_w233 = engine.run_portfolio(
        days, all_bars, windows=[233], atr_threshold=atr_threshold_full, cooldown=8, use_rsi_exit=False
    )
    result_combo = engine.run_portfolio(
        days, all_bars, windows=[34, 55, 89], atr_threshold=atr_threshold_full, cooldown=8, use_rsi_exit=False
    )

    target_w233 = -5417.8
    target_combo = -12310.3
    tol_pts = 50.0  # 小數點捨入範圍內即可，但方向與量級一定要對

    pnl_w233 = result_w233["total_pnl"]
    pnl_combo = result_combo["total_pnl"]
    print(f"window=233 單獨: 總損益={pnl_w233:,.1f}pt（{result_w233['n_trades']}筆）  目標={target_w233:,.1f}pt")
    print(f"w34+w55+w89 併行: 總損益={pnl_combo:,.1f}pt（{result_combo['n_trades']}筆）  目標={target_combo:,.1f}pt")

    ok_c1 = abs(pnl_w233 - target_w233) <= tol_pts
    ok_c2 = abs(pnl_combo - target_combo) <= tol_pts
    record(
        "(c1) window=233 對上 -5,417.8pt",
        ok_c1,
        f"實際={pnl_w233:,.1f}pt  誤差={pnl_w233 - target_w233:+.1f}pt（容忍±{tol_pts}pt）",
    )
    record(
        "(c2) w34+w55+w89 對上 -12,310.3pt",
        ok_c2,
        f"實際={pnl_combo:,.1f}pt  誤差={pnl_combo - target_combo:+.1f}pt（容忍±{tol_pts}pt）",
    )

    print("\n=== 總結 ===")
    n_pass = sum(1 for _, ok, _ in results if ok)
    n_total = len(results)
    for name, ok, detail in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    print(f"\n{n_pass}/{n_total} 項測試通過")
    if n_pass < n_total:
        sys.exit(1)


if __name__ == "__main__":
    main()
