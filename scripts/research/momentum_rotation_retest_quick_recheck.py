"""2026-08-13：retest-entry候選（buf=0.55% timeout=240s）在套用exit_price真實
tick價修正後的快速覆核——先只跑推薦參數點的head-to-head，不跑完整3階段sweep，
確認修正後這個候選還站不站得住，再決定要不要花時間做完整sweep+holdout。
"""

from __future__ import annotations

import sys

sys.path.insert(0, "scripts/research")
from momentum_rotation_retest_entry import WINDOWS, load_window, run_variant  # noqa: E402


def main() -> None:
    print("載入4窗口資料...")
    windows_data = {wname: load_window(wdate) for wname, wdate in WINDOWS.items()}
    base = dict(breakout_pct=0.5, trail_pct=1.0, vol_confirm_mult=1.5, rearm_pct=0.25,
                min_overshoot_pct=0.15, min_vol_ratio=1.5, preempt_mult=2.0)

    print("\n=== baseline（現行，兩個bug都已修正） ===")
    run_variant("baseline", windows_data, use_retest=False, **base)

    print("\n=== retest buf=0.55% timeout=240s（現在套用exit_price修正） ===")
    run_variant("retest(buf=0.55,timeout=240)", windows_data, use_retest=True,
                **base, retest_buffer_pct=0.55, retest_timeout_sec=240, max_extension_pct=3.0)


if __name__ == "__main__":
    main()
