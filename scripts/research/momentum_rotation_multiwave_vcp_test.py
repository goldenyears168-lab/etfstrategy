"""2026-08-14：對照原作者Minervini VCP方法論後找到的盲點——今天稍早的coil
判斷只比較「最近一段 vs 再往前一段」單一次前後比較，不是VCP真正的定義。
VCP的核心是**連續2~4次逐次變淺的拉回**（每一次的震幅都比前一次更小、通常
量也更縮），代表賣壓一波比一波弱，這裡重寫成真正的多波收縮偵測：

把coil_lookback_sec秒切成n_waves個等長子區間（依時間先後排列），逐一計算
每個子區間的「震幅」(該區間內最高-最低)跟成交量。要求**連續**n_waves個
區間的震幅是遞減的（每一段≤前一段×wave_contraction_ratio），量能也同樣要求
遞減——這才是「一波比一波安靜」的多波收縮，不是單一次的前後比較。

每個子區間都要求至少MIN_TICKS_PER_BUCKET筆tick（密度門檻，避免稀疏資料
被誤判），沿用今天已經修正過的教訓。

跟稍早一樣：先用訊號層級命中率測試（train/holdout按股票切分，190檔broad
universe，已排除價差合約污染），有戲了才進一步包進完整交易模擬算損益。
"""

from __future__ import annotations

import random
import sys
from datetime import datetime, timedelta

import numpy as np

sys.path.insert(0, "scripts/research")
from momentum_rotation_broad_universe_coil_trend_test import (  # noqa: E402
    CONTINUATION_SEC,
    MOVE_THRESH_PCT,
    RANDOM_SEED,
    TREND_LOOKBACK_MIN,
    VOL_MULT,
    WINDOW_SEC,
    load_broad_universe,
)

MIN_TICKS_PER_BUCKET = 3
N_WAVES_GRID = [2, 3]
COIL_LOOKBACK_SEC_GRID = [6.0, 9.0, 12.0, 18.0]
WAVE_CONTRACTION_RATIO_GRID = [0.5, 0.6, 0.7, 0.8]
MIN_SIGNALS = 25


def multiwave_hit_rate(
    days: dict[str, tuple[list, list, list]], *,
    n_waves: int, coil_lookback_sec: float, wave_contraction_ratio: float,
    require_trend: bool = True, trend_lookback_min: float = TREND_LOOKBACK_MIN,
) -> tuple[float, int]:
    hits: list[int] = []
    bucket_sec = coil_lookback_sec / n_waves
    trend_buf_span = trend_lookback_min * 60.0

    for _d, (times, prices, volumes) in days.items():
        dts = [datetime.fromisoformat(t) for t in times]
        n = len(dts)
        buf: list[tuple] = []
        coil_buf: list[tuple] = []
        trend_buf: list[tuple] = []
        vol_hist: list[float] = []
        last_signal: datetime | None = None
        win_td = timedelta(seconds=WINDOW_SEC)
        coil_td = timedelta(seconds=coil_lookback_sec)
        trend_td = timedelta(seconds=trend_buf_span)
        cool_td = timedelta(seconds=10.0)

        for k in range(n):
            t, p, v = dts[k], float(prices[k]), float(volumes[k])
            buf.append((t, p, v))
            while buf and (t - buf[0][0]) > win_td:
                buf.pop(0)
            coil_buf.append((t, p, v))
            while coil_buf and (t - coil_buf[0][0]) > coil_td:
                coil_buf.pop(0)
            trend_buf.append((t, p, v))
            while trend_buf and (t - trend_buf[0][0]) > trend_td:
                trend_buf.pop(0)

            window_vol = sum(r[2] for r in buf)
            if len(buf) < 2:
                vol_hist.append(window_vol)
                continue
            baseline = max(np.median(vol_hist), 1e-9) if vol_hist else 1.0
            vol_hist.append(window_vol)

            if last_signal is not None and (t - last_signal) < cool_td:
                continue
            oldest_p = buf[0][1]
            if oldest_p <= 0:
                continue
            move_pct = (p - oldest_p) / oldest_p * 100.0
            vol_burst = window_vol / baseline
            if abs(move_pct) < MOVE_THRESH_PCT or vol_burst < VOL_MULT:
                continue

            # === 多波收縮偵測：把coil_lookback_sec切成n_waves個子區間 ===
            if len(coil_buf) < n_waves * MIN_TICKS_PER_BUCKET:
                continue
            bucket_start = t - timedelta(seconds=coil_lookback_sec)
            buckets: list[list[tuple]] = [[] for _ in range(n_waves)]
            valid_buckets = True
            for row in coil_buf:
                offset = (row[0] - bucket_start).total_seconds()
                idx = int(offset // bucket_sec)
                if idx < 0 or idx >= n_waves:
                    continue
                buckets[idx].append(row)
            if any(len(b) < MIN_TICKS_PER_BUCKET for b in buckets):
                valid_buckets = False
            if not valid_buckets:
                continue
            ranges = [max(r[1] for r in b) - min(r[1] for r in b) for b in buckets]
            vols = [sum(r[2] for r in b) for b in buckets]
            if any(r <= 0 for r in ranges) or any(vv <= 0 for vv in vols):
                continue
            # 要求「連續遞減」：每一段(由舊到新)都<=前一段*wave_contraction_ratio
            is_narrowing = all(
                ranges[i] <= ranges[i - 1] * wave_contraction_ratio
                and vols[i] <= vols[i - 1] * wave_contraction_ratio
                for i in range(1, n_waves)
            )
            if not is_narrowing:
                continue

            direction = 1 if move_pct > 0 else -1
            if require_trend:
                if len(trend_buf) < 2:
                    continue
                span = (trend_buf[-1][0] - trend_buf[0][0]).total_seconds()
                if span < trend_buf_span * 0.5:
                    continue
                trend_dir = 1 if trend_buf[-1][1] > trend_buf[0][1] else (-1 if trend_buf[-1][1] < trend_buf[0][1] else 0)
                if trend_dir != direction:
                    continue

            last_signal = t
            deadline = t + timedelta(seconds=CONTINUATION_SEC)
            future_idx = None
            for j in range(k + 1, n):
                if dts[j] >= deadline:
                    future_idx = j
                    break
            if future_idx is None:
                continue
            p_future = float(prices[future_idx])
            ret = (p_future - p) * direction
            hits.append(1 if ret > 0 else 0)

    return (float(np.mean(hits)) if hits else 0.0, len(hits))


def aggregate(universe_subset: dict, **kwargs) -> tuple[float, int]:
    total_hits, total_n = 0.0, 0
    for _code, days in universe_subset.items():
        hr, n = multiwave_hit_rate(days, **kwargs)
        total_hits += hr * n
        total_n += n
    return (total_hits / total_n if total_n else 0.0, total_n)


def main() -> None:
    print("載入TAIFEX全市場個股期貨archive...")
    universe = load_broad_universe()
    print(f"  {len(universe)}檔通過流動性門檻")

    codes = sorted(universe.keys())
    rng = random.Random(RANDOM_SEED)
    shuffled = codes[:]
    rng.shuffle(shuffled)
    split = len(shuffled) // 2
    train_codes, holdout_codes = shuffled[:split], shuffled[split:]
    train_universe = {c: universe[c] for c in train_codes}
    holdout_universe = {c: universe[c] for c in holdout_codes}
    print(f"  train組{len(train_codes)}檔 / holdout組{len(holdout_codes)}檔（種子{RANDOM_SEED}）\n")

    hr0, n0 = aggregate(train_universe, n_waves=1, coil_lookback_sec=1.0, wave_contraction_ratio=1.0, require_trend=False)
    print(f"對照(單波、無收縮要求、無趨勢——約略等於純爆量): train命中率={hr0*100:.1f}% n={n0}")

    print(f"\nsweep grid: n_waves={N_WAVES_GRID} x coil_lookback_sec={COIL_LOOKBACK_SEC_GRID} x "
          f"wave_contraction_ratio={WAVE_CONTRACTION_RATIO_GRID} "
          f"({len(N_WAVES_GRID)*len(COIL_LOOKBACK_SEC_GRID)*len(WAVE_CONTRACTION_RATIO_GRID)}組)")
    best = None
    for nw in N_WAVES_GRID:
        for cl in COIL_LOOKBACK_SEC_GRID:
            for wr in WAVE_CONTRACTION_RATIO_GRID:
                hr, n = aggregate(train_universe, n_waves=nw, coil_lookback_sec=cl, wave_contraction_ratio=wr)
                flag = ""
                if n >= MIN_SIGNALS and (best is None or hr > best[3]):
                    best = (nw, cl, wr, hr, n)
                    flag = " <- 目前最佳"
                print(f"  n_waves={nw} lookback={cl}s ratio={wr}: 命中率={hr*100:.1f}% n={n}{flag}")

    if best is None:
        print(f"\n找不到樣本數>={MIN_SIGNALS}的組合，多波收縮條件太嚴格，資料量不足以驗證")
        return

    nw_b, cl_b, wr_b, train_hr, train_n = best
    print(f"\n最佳點: n_waves={nw_b} coil_lookback={cl_b}s wave_contraction_ratio={wr_b} "
          f"(train命中率={train_hr*100:.1f}% n={train_n})")

    hold_hr, hold_n = aggregate(holdout_universe, n_waves=nw_b, coil_lookback_sec=cl_b, wave_contraction_ratio=wr_b)
    hold_hr0, hold_n0 = aggregate(holdout_universe, n_waves=1, coil_lookback_sec=1.0, wave_contraction_ratio=1.0, require_trend=False)
    print(f"\nHOLDOUT組（完全沒看過的{len(holdout_codes)}檔）: 命中率={hold_hr*100:.1f}% n={hold_n}")
    print(f"HOLDOUT對照(單波無收縮): 命中率={hold_hr0*100:.1f}% n={hold_n0}")
    print(f"\n類化程度: train {train_hr*100:.1f}% -> holdout {hold_hr*100:.1f}% "
          f"(落差{(train_hr-hold_hr)*100:+.1f}個百分點)")


if __name__ == "__main__":
    main()
