"""2026-08-14：查證Minervini原文後發現，VCP在他的SEPA框架裡只是其中一塊，
另一塊「相對強度領先(RS rating 80-99、市場領漲股)」是**非談判**的前提——他
自己寫過「技術面完美但沒基本面/領先地位支撐的圖形，通常幾週內就會破功」。
今天稍早橫斷面相對強度是**單獨當訊號**測試失敗的(8-agent workflow, 1勝3負)，
這裡改成**當VCP的前置篩選條件**：候選標的當下的「今日至今報酬率」要落在
全部標的的前/後段百分位（做多要求是當下領漲的，做空要求是當下領跌的），
才承認這是「真正的leadership VCP」，不是任何安靜之後爆量都算數。

相對強度用「今日至今報酬率」(latest_price/open_price - 1)當proxy，每一個
tick事件都更新該標的的latest_price，跨標的比較用「當下每檔各自最後已知的
報酬率」排名（tick資料本身非同步，這是唯一可行的近似）。

沿用momentum_rotation_multiwave_vcp_test.py的多波收縮偵測(n_waves=2,
coil_lookback=12s, wave_contraction_ratio=0.5)+3分鐘趨勢方向一致，多加這一層
leadership percentile篩選，train/holdout按股票切分驗證。
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

N_WAVES = 2
COIL_LOOKBACK_SEC = 12.0
WAVE_CONTRACTION_RATIO = 0.5
MIN_TICKS_PER_BUCKET = 3
LEADERSHIP_PCTL_GRID = [0.5, 0.6, 0.7, 0.8]  # 要求落在前/後這個百分位以外才算leader/laggard
MIN_UNIVERSE_KNOWN = 20  # 至少要有這麼多檔股票有已知報酬率才能排名，避免開盤瞬間樣本太少排名沒意義
MIN_SIGNALS = 20


def universe_day_multiwave_leadership_hits(
    day_data: dict[str, tuple], *, leadership_pctl: float,
) -> tuple[int, int]:
    """跨標的合併單日tick，逐筆更新每檔的latest_price，coil+趨勢訊號成立時
    多檢查一層leadership percentile。回傳(命中數, 訊號數)。"""
    merged: list[tuple] = []
    open_price: dict[str, float] = {}
    for sid, (times, prices, volumes) in day_data.items():
        if len(times) < 2:
            continue
        open_price[sid] = float(prices[0])
        for k in range(len(times)):
            merged.append((datetime.fromisoformat(times[k]), sid, float(prices[k]), float(volumes[k])))
    merged.sort(key=lambda x: x[0])

    latest_price: dict[str, float] = {}
    buf: dict[str, list[tuple]] = {sid: [] for sid in day_data}
    coil_buf: dict[str, list[tuple]] = {sid: [] for sid in day_data}
    trend_buf: dict[str, list[tuple]] = {sid: [] for sid in day_data}
    vol_hist: dict[str, list[float]] = {sid: [] for sid in day_data}
    last_signal: dict[str, datetime | None] = {sid: None for sid in day_data}
    price_series: dict[str, list[float]] = {sid: list(prices) for sid, (_t, prices, _v) in day_data.items()}
    time_series: dict[str, list[datetime]] = {
        sid: [datetime.fromisoformat(t) for t in times] for sid, (times, _p, _v) in day_data.items()
    }
    idx_by_sid: dict[str, int] = {sid: 0 for sid in day_data}

    win_td = timedelta(seconds=WINDOW_SEC)
    coil_td = timedelta(seconds=COIL_LOOKBACK_SEC)
    trend_td = timedelta(seconds=TREND_LOOKBACK_MIN * 60.0)
    cool_td = timedelta(seconds=10.0)
    bucket_sec = COIL_LOOKBACK_SEC / N_WAVES

    n_signals, n_hits = 0, 0

    for t, sid, p, v in merged:
        latest_price[sid] = p
        b = buf[sid]
        b.append((t, p, v))
        while b and (t - b[0][0]) > win_td:
            b.pop(0)
        cb = coil_buf[sid]
        cb.append((t, p, v))
        while cb and (t - cb[0][0]) > coil_td:
            cb.pop(0)
        tb = trend_buf[sid]
        tb.append((t, p, v))
        while tb and (t - tb[0][0]) > trend_td:
            tb.pop(0)

        window_vol = sum(r[2] for r in b)
        if len(b) < 2:
            vol_hist[sid].append(window_vol)
            continue
        baseline = max(np.median(vol_hist[sid]), 1e-9) if vol_hist[sid] else 1.0
        vol_hist[sid].append(window_vol)

        if last_signal[sid] is not None and (t - last_signal[sid]) < cool_td:
            continue
        oldest_p = b[0][1]
        if oldest_p <= 0:
            continue
        move_pct = (p - oldest_p) / oldest_p * 100.0
        vol_burst = window_vol / baseline
        if abs(move_pct) < MOVE_THRESH_PCT or vol_burst < VOL_MULT:
            continue

        # 多波收縮
        if len(cb) < N_WAVES * MIN_TICKS_PER_BUCKET:
            continue
        bucket_start = t - timedelta(seconds=COIL_LOOKBACK_SEC)
        buckets: list[list[tuple]] = [[] for _ in range(N_WAVES)]
        for row in cb:
            offset = (row[0] - bucket_start).total_seconds()
            idx = int(offset // bucket_sec)
            if 0 <= idx < N_WAVES:
                buckets[idx].append(row)
        if any(len(bb) < MIN_TICKS_PER_BUCKET for bb in buckets):
            continue
        ranges = [max(r[1] for r in bb) - min(r[1] for r in bb) for bb in buckets]
        vols = [sum(r[2] for r in bb) for bb in buckets]
        if any(r <= 0 for r in ranges) or any(vv <= 0 for vv in vols):
            continue
        if not all(
            ranges[i] <= ranges[i - 1] * WAVE_CONTRACTION_RATIO
            and vols[i] <= vols[i - 1] * WAVE_CONTRACTION_RATIO
            for i in range(1, N_WAVES)
        ):
            continue

        direction = 1 if move_pct > 0 else -1

        # 趨勢方向一致
        if len(tb) < 2:
            continue
        span = (tb[-1][0] - tb[0][0]).total_seconds()
        if span < TREND_LOOKBACK_MIN * 60.0 * 0.5:
            continue
        trend_dir = 1 if tb[-1][1] > tb[0][1] else (-1 if tb[-1][1] < tb[0][1] else 0)
        if trend_dir != direction:
            continue

        # === Leadership percentile：跟其他標的當下報酬率排名比較 ===
        rets_now = {
            s: (latest_price[s] - open_price[s]) / open_price[s]
            for s in day_data if s in open_price and s in latest_price
        }
        if len(rets_now) < MIN_UNIVERSE_KNOWN:
            continue
        my_ret = rets_now.get(sid)
        if my_ret is None:
            continue
        all_rets = sorted(rets_now.values())
        rank = sum(1 for r in all_rets if r <= my_ret) / len(all_rets)
        is_leader = rank >= leadership_pctl if direction == 1 else rank <= (1 - leadership_pctl)
        if not is_leader:
            continue

        last_signal[sid] = t
        n_signals += 1

        deadline = t + timedelta(seconds=CONTINUATION_SEC)
        ts = time_series[sid]
        ps = price_series[sid]
        start_idx = idx_by_sid[sid]
        while start_idx < len(ts) and ts[start_idx] <= t:
            start_idx += 1
        future_idx = None
        for j in range(start_idx, len(ts)):
            if ts[j] >= deadline:
                future_idx = j
                break
        idx_by_sid[sid] = start_idx
        if future_idx is None:
            continue
        p_future = ps[future_idx]
        ret = (p_future - p) * direction
        if ret > 0:
            n_hits += 1

    return n_hits, n_signals


def aggregate(universe_subset: dict, leadership_pctl: float) -> tuple[float, int]:
    all_days = sorted({d for days in universe_subset.values() for d in days})
    total_hits, total_n = 0, 0
    for d in all_days:
        day_data = {sid: days[d] for sid, days in universe_subset.items() if d in days}
        if len(day_data) < MIN_UNIVERSE_KNOWN:
            continue
        h, n = universe_day_multiwave_leadership_hits(day_data, leadership_pctl=leadership_pctl)
        total_hits += h
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

    print("對照(不篩leadership，等於今天的多波VCP版本): ", end="")
    hr0, n0 = aggregate(train_universe, leadership_pctl=0.0)
    print(f"train命中率={hr0*100:.1f}% n={n0}")

    print(f"\nsweep grid: leadership_pctl={LEADERSHIP_PCTL_GRID}")
    best = None
    for lp in LEADERSHIP_PCTL_GRID:
        hr, n = aggregate(train_universe, leadership_pctl=lp)
        flag = ""
        if n >= MIN_SIGNALS and (best is None or hr > best[1]):
            best = (lp, hr, n)
            flag = " <- 目前最佳"
        print(f"  leadership_pctl>={lp}: 命中率={hr*100:.1f}% n={n}{flag}")

    if best is None:
        print(f"\n找不到樣本數>={MIN_SIGNALS}的組合，leadership前提太嚴格")
        return

    lp_best, train_hr, train_n = best
    print(f"\n最佳點: leadership_pctl>={lp_best} (train命中率={train_hr*100:.1f}% n={train_n})")

    hold_hr, hold_n = aggregate(holdout_universe, leadership_pctl=lp_best)
    hold_hr0, hold_n0 = aggregate(holdout_universe, leadership_pctl=0.0)
    print(f"\nHOLDOUT組（完全沒看過的{len(holdout_codes)}檔）: 命中率={hold_hr*100:.1f}% n={hold_n}")
    print(f"HOLDOUT對照(不篩leadership,純多波VCP): 命中率={hold_hr0*100:.1f}% n={hold_n0}")
    print(f"\n類化程度: train {train_hr*100:.1f}% -> holdout {hold_hr*100:.1f}% "
          f"(落差{(train_hr-hold_hr)*100:+.1f}個百分點)")


if __name__ == "__main__":
    main()
