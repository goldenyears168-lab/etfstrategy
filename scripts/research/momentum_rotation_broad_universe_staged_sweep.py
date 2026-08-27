"""2026-08-13：micro VCP在190檔broad universe上首次用stock-split holdout驗證
（coil+趨勢：train 38.2%→holdout 36.4%，比baseline 27.5%高快9個百分點，
沒有像純coil那樣崩回baseline，是今天第一個真正撐住的候選）。使用者要求
「調整參數看看最佳參數在哪裡」——這裡做分階段sweep，避免5個維度一次全開
組合爆炸：

  Stage 1：粗掃 contraction_ratio × min_coil_sec（coil的核心兩個維度），
           trend_lookback_min/vol_mult/move_thresh_pct固定用今天已知的
           代表值。
  Stage 2：用Stage 1選出的最佳(ratio, min_coil)，掃trend_lookback_min
           （2/3/5/8分鐘）找最佳趨勢視窗長度。
  Stage 3：用Stage 1+2選出的最佳點，掃vol_mult × move_thresh_pct（爆量
           偵測本身的門檻）。

全程都在TRAIN組（95檔）上sweep選參數，最後把三階段疊加出的最終最佳點，
只套用到HOLDOUT組（完全沒看過的95檔）驗證一次，跟今天全部候選同一個
「不能用同一份資料選參數又驗證」的紀律。
"""

from __future__ import annotations

import sys

sys.path.insert(0, "scripts/research")
from momentum_rotation_broad_universe_coil_trend_test import (  # noqa: E402
    RANDOM_SEED,
    aggregate_hit_rate,
    load_broad_universe,
)

STAGE1_RATIO_GRID = [0.3, 0.4, 0.5, 0.6, 0.7, 0.8]
STAGE1_MIN_COIL_GRID = [2.0, 3.0, 5.0, 8.0, 10.0, 15.0]
STAGE2_TREND_GRID = [2.0, 3.0, 5.0, 8.0]
STAGE3_VOL_MULT_GRID = [1.5, 2.0, 2.5, 3.0, 4.0]
STAGE3_MOVE_THRESH_GRID = [0.1, 0.15, 0.2, 0.3]

MIN_SIGNALS = 30  # 少於這個數字的組合不列入候選，避免雜訊點被誤選成"最佳"


def main() -> None:
    print("載入TAIFEX全市場個股期貨archive...")
    universe = load_broad_universe()
    print(f"  {len(universe)}檔通過流動性門檻")

    import random
    codes = sorted(universe.keys())
    rng = random.Random(RANDOM_SEED)
    shuffled = codes[:]
    rng.shuffle(shuffled)
    split = len(shuffled) // 2
    train_codes, holdout_codes = shuffled[:split], shuffled[split:]
    train_universe = {c: universe[c] for c in train_codes}
    holdout_universe = {c: universe[c] for c in holdout_codes}
    print(f"  train組{len(train_codes)}檔 / holdout組{len(holdout_codes)}檔（種子{RANDOM_SEED}）")

    print(f"\n=== Stage 1: contraction_ratio x min_coil_sec 粗掃（{len(STAGE1_RATIO_GRID)}x{len(STAGE1_MIN_COIL_GRID)}={len(STAGE1_RATIO_GRID)*len(STAGE1_MIN_COIL_GRID)}組，require_trend=True，trend固定3分鐘）===")
    best1 = None
    for cr in STAGE1_RATIO_GRID:
        for mc in STAGE1_MIN_COIL_GRID:
            hr, n = aggregate_hit_rate(train_universe, contraction_ratio=cr, min_coil_sec=mc,
                                        require_trend=True, trend_lookback_min=3.0)
            flag = ""
            if n >= MIN_SIGNALS and (best1 is None or hr > best1[2]):
                best1 = (cr, mc, hr, n)
                flag = " <- 目前最佳"
            print(f"  ratio={cr} min_coil={mc}s: 命中率={hr*100:.1f}% n={n}{flag}")
    if best1 is None:
        print("Stage 1找不到樣本數>=30的組合，停止")
        return
    cr_best, mc_best, hr1, n1 = best1
    print(f"\nStage 1最佳: ratio={cr_best} min_coil={mc_best}s 命中率={hr1*100:.1f}% n={n1}")

    print(f"\n=== Stage 2: trend_lookback_min 掃描（固定ratio={cr_best}, min_coil={mc_best}s）===")
    best2 = (3.0, hr1, n1)
    for tl in STAGE2_TREND_GRID:
        hr, n = aggregate_hit_rate(train_universe, contraction_ratio=cr_best, min_coil_sec=mc_best,
                                    require_trend=True, trend_lookback_min=tl)
        flag = ""
        if n >= MIN_SIGNALS and hr > best2[1]:
            best2 = (tl, hr, n)
            flag = " <- 目前最佳"
        print(f"  trend_lookback={tl}min: 命中率={hr*100:.1f}% n={n}{flag}")
    tl_best, hr2, n2 = best2
    print(f"\nStage 2最佳: trend_lookback={tl_best}min 命中率={hr2*100:.1f}% n={n2}")

    print(f"\n=== Stage 3: vol_mult x move_thresh_pct 掃描（固定ratio={cr_best}, min_coil={mc_best}s, trend={tl_best}min）===")
    best3 = (2.5, 0.15, hr2, n2)
    for vm in STAGE3_VOL_MULT_GRID:
        for mt in STAGE3_MOVE_THRESH_GRID:
            hr, n = aggregate_hit_rate(train_universe, contraction_ratio=cr_best, min_coil_sec=mc_best,
                                        require_trend=True, trend_lookback_min=tl_best,
                                        vol_mult=vm, move_thresh_pct=mt)
            flag = ""
            if n >= MIN_SIGNALS and hr > best3[2]:
                best3 = (vm, mt, hr, n)
                flag = " <- 目前最佳"
            print(f"  vol_mult={vm}x move_thresh={mt}%: 命中率={hr*100:.1f}% n={n}{flag}")
    vm_best, mt_best, hr3, n3 = best3
    print(f"\nStage 3最佳: vol_mult={vm_best}x move_thresh={mt_best}% 命中率={hr3*100:.1f}% n={n3}")

    print("\n" + "=" * 90)
    print(f"=== 三階段疊加最終最佳點: ratio={cr_best} min_coil={mc_best}s trend={tl_best}min "
          f"vol_mult={vm_best}x move_thresh={mt_best}% ===")
    print(f"TRAIN組命中率(全參數同時套用重新驗算): ", end="")
    hr_final_train, n_final_train = aggregate_hit_rate(
        train_universe, contraction_ratio=cr_best, min_coil_sec=mc_best,
        require_trend=True, trend_lookback_min=tl_best, vol_mult=vm_best, move_thresh_pct=mt_best,
    )
    print(f"{hr_final_train*100:.1f}% (n={n_final_train})")

    hr_final_hold, n_final_hold = aggregate_hit_rate(
        holdout_universe, contraction_ratio=cr_best, min_coil_sec=mc_best,
        require_trend=True, trend_lookback_min=tl_best, vol_mult=vm_best, move_thresh_pct=mt_best,
    )
    hr_base_hold, n_base_hold = aggregate_hit_rate(
        holdout_universe, contraction_ratio=1.0, min_coil_sec=3.0, require_trend=False,
    )
    print(f"HOLDOUT組命中率(完全沒看過的95檔，只套用一次): {hr_final_hold*100:.1f}% (n={n_final_hold})")
    print(f"HOLDOUT組對照(無濾網純爆量): {hr_base_hold*100:.1f}% (n={n_base_hold})")
    print(f"\n類化程度: train {hr_final_train*100:.1f}% -> holdout {hr_final_hold*100:.1f}% "
          f"(落差{(hr_final_train-hr_final_hold)*100:+.1f}個百分點)")


if __name__ == "__main__":
    main()
