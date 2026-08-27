"""2026-08-13：使用者的新方向——不是「等回測才進場」（那是延遲進場、換乾淨度），
是反過來：**進場判斷盡量快、只想抓動能延續的前幾秒鐘**，快進快出，比稍早的
fixed_horizon_test（1/3/5/10/15/30分鐘）粒度細很多。這裡兩件事一起測：

  1. 秒級持有：訊號成立當下立刻進場（原方向、不等回測），固定持有N秒
     （1~60秒）後不管賺賠強制出場——純粹測「動能延續的前幾秒有沒有資訊量」，
     跟fixed_horizon_test同一個「訊號本身」邏輯，只是換成秒級解析度。
  2. 確認門檻的鬆緊（使用者問「能不能減少思考判斷時間」）：現行門檻是
     vol_confirm_mult=1.5×基準量能 + min_overshoot_pct=0.15%（衝過trigger至少
     0.15%）——這裡也測「門檻放更鬆（反應更快，犧牲確認品質）」vs
     「門檻拉更緊（等更久才確認，換更乾淨的訊號）」，看秒級動能edge會不會
     隨確認嚴格度變化。

⚠️ tick資料本身只有整秒解析度（CSV原始欄位，見reports/research/
expert_pool_futures_tick/*.csv），沒有更細的次秒級時間戳——1秒以下的持有無法
用這份資料驗證，這是資料本身的限制，不是没做。
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta

import numpy as np

sys.path.insert(0, "scripts/research")
from momentum_rotation_redesign_search import WINDOWS, load_window  # noqa: E402


def second_scalp_test(
    stock_day_data: dict, horizons_sec: list[float], *,
    breakout_pct: float = 0.5, vol_confirm_mult: float = 1.5,
    min_overshoot_pct: float = 0.15, rearm_pct: float = 0.25,
) -> dict[float, list[float]]:
    """跟blindspot_hunt.fixed_horizon_test同一個精神：訊號後固定N秒方向對不對，
    不管理部位(可重疊、非可執行策略，只問訊號本身的資訊量)。"""
    out: dict[float, list[float]] = {h: [] for h in horizons_sec}
    for sid, (times, prices, volumes) in stock_day_data.items():
        if prices.size < 2:
            continue
        open_price = float(prices[0])
        long_trigger = open_price * (1 + breakout_pct / 100.0)
        short_trigger = open_price * (1 - breakout_pct / 100.0)
        rearm_hi = open_price * (1 + rearm_pct / 100.0)
        rearm_lo = open_price * (1 - rearm_pct / 100.0)
        armed = True
        vol_hist: list[float] = []
        for k in range(1, len(times)):
            p, v = float(prices[k]), float(volumes[k])
            base = max(np.median(vol_hist), 1e-9) if vol_hist else 1.0
            vol_hist.append(v)
            if not armed:
                if rearm_lo <= p <= rearm_hi:
                    armed = True
                continue
            hits_long, hits_short = p >= long_trigger, p <= short_trigger
            if not (hits_long or hits_short) or v < vol_confirm_mult * base:
                continue
            trigger = long_trigger if hits_long else short_trigger
            overshoot = abs(p - trigger) / open_price * 100.0
            if overshoot < min_overshoot_pct:
                continue
            direction = "long" if hits_long else "short"
            armed = False
            t0 = datetime.fromisoformat(times[k])
            for h in horizons_sec:
                deadline = t0 + timedelta(seconds=h)
                future_idx = None
                for j in range(k + 1, len(times)):
                    if datetime.fromisoformat(times[j]) >= deadline:
                        future_idx = j
                        break
                if future_idx is None:
                    continue
                p_future = float(prices[future_idx])
                ret = (p_future - p) / p * 100.0 if direction == "long" else (p - p_future) / p * 100.0
                out[h].append(ret)
    return out


def run_confirm_variant(name: str, windows_data: dict, horizons: list[float], **kwargs) -> None:
    agg: dict[float, list[float]] = {h: [] for h in horizons}
    for _wname, (all_by_stock, all_days) in windows_data.items():
        for d in all_days:
            day_data = {sid: days[d] for sid, days in all_by_stock.items() if d in days}
            if len(day_data) < 3:
                continue
            res = second_scalp_test(day_data, horizons, **kwargs)
            for h in horizons:
                agg[h].extend(res[h])
    print(f"\n--- {name} ---")
    for h in horizons:
        rets = np.array(agg[h])
        if len(rets) == 0:
            print(f"  {h:5.0f}秒後: 無資料")
            continue
        win = float(np.mean(rets > 0) * 100)
        breakeven = (rets.sum() / len(rets)) * 100
        print(f"  {h:5.0f}秒後: n={len(rets):5d} 勝率={win:5.1f}% 均值={rets.mean():+.4f}% "
              f"標準差={rets.std():.4f}% 損平={breakeven:6.1f}bps")


def main() -> None:
    print("載入4窗口資料...")
    windows_data = {wname: load_window(wdate) for wname, wdate in WINDOWS.items()}
    horizons = [1, 2, 3, 5, 10, 15, 20, 30, 45, 60]

    print("=" * 80)
    print("A. 秒級動能延續：現行確認門檻（vol_confirm_mult=1.5x, min_overshoot=0.15%）")
    print("=" * 80)
    run_confirm_variant("現行門檻", windows_data, horizons,
                         vol_confirm_mult=1.5, min_overshoot_pct=0.15)

    print("\n" + "=" * 80)
    print("B. 放鬆確認門檻（減少思考判斷時間：只要一過trigger、量能持平即可，不用等overshoot）")
    print("=" * 80)
    run_confirm_variant("鬆門檻(vol1.0x,overshoot0%)", windows_data, horizons,
                         vol_confirm_mult=1.0, min_overshoot_pct=0.0)

    print("\n" + "=" * 80)
    print("C. 拉緊確認門檻（等更久、換更乾淨的訊號）")
    print("=" * 80)
    run_confirm_variant("緊門檻(vol2.5x,overshoot0.4%)", windows_data, horizons,
                         vol_confirm_mult=2.5, min_overshoot_pct=0.4)

    print("\n" + "=" * 80)
    print("D. 極緊門檻（vol3.5x,overshoot0.6%）")
    print("=" * 80)
    run_confirm_variant("極緊門檻(vol3.5x,overshoot0.6%)", windows_data, horizons,
                         vol_confirm_mult=3.5, min_overshoot_pct=0.6)


if __name__ == "__main__":
    main()
