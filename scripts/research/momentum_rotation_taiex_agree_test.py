"""2026-08-13：使用者假說——單股期貨的動能訊號，如果當下跟台指（TX）同向，
勝率/報酬應該更好（呼應盲點分析D：84.2%訊號60秒內群聚，代表大多是共同因子
驅動，不是個股獨立動能——這裡直接測試「共同因子」本身，用TX方向當confirm）。

NQ同向測試目前做不了：NQ/ES歷史分鐘資料只從2026-08-05開始累積（見
scripts/research/nq_es_1m_daily_accumulate.py），涵蓋不到本檔用的4個回測窗口
中的任何一個，只有window4最後5天有一點點重疊——樣本太小不足以下結論，這裡
先不做，誠實跳過。

TX資料來源：~/goldenstocks-data/cache/tmf_channel/bars.sqlite，
source='tx_1m_tick_built_582d'，sess='day'，1分鐘K，t欄位是'HH:MM'(Asia/Taipei)。
用「訊號當下那一分鐘之前已經收盤的那根TX 1分K」跟「N分鐘前的TX 1分K」比較
決定TX短線方向，避免用到訊號當下還沒收盤的TX K（look-ahead）。
"""

from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime

import numpy as np

sys.path.insert(0, "scripts/research")
from momentum_rotation_redesign_search import WINDOWS, load_window  # noqa: E402

_DATA_DIR = os.environ.get("GOLDENSTOCKS_DATA_DIR") or os.path.expanduser("~/goldenstocks-data")
_BARS_DB = os.path.join(_DATA_DIR, "cache", "tmf_channel", "bars.sqlite")


def load_taiex_days(days: list[str]) -> dict[str, list[tuple[str, float]]]:
    conn = sqlite3.connect(f"file:{_BARS_DB}?mode=ro", uri=True)
    out: dict[str, list[tuple[str, float]]] = {}
    try:
        cur = conn.cursor()
        for d in days:
            rows = cur.execute(
                "SELECT t, c FROM bars WHERE source='tx_1m_tick_built_582d' AND sess='day' AND day=? ORDER BY t",
                (d,),
            ).fetchall()
            if rows:
                out[d] = [(t, float(c)) for t, c in rows]
    finally:
        conn.close()
    return out


def _taiex_direction(taiex_day: list[tuple[str, float]], hhmmss: str, lookback_min: int) -> str | None:
    """用嚴格早於當前分鐘、已收盤的TX bar；跟lookback_min分鐘前的bar比較方向。"""
    hhmm_now = hhmmss[:5]
    idx_before = None
    for i, (t, _c) in enumerate(taiex_day):
        if t < hhmm_now:
            idx_before = i
        else:
            break
    if idx_before is None or idx_before - lookback_min < 0:
        return None
    close_now = taiex_day[idx_before][1]
    close_prior = taiex_day[idx_before - lookback_min][1]
    if close_now > close_prior:
        return "up"
    if close_now < close_prior:
        return "down"
    return None


def second_scalp_with_taiex(
    stock_day_data: dict, taiex_day: list[tuple[str, float]] | None, horizons_sec: list[float],
    *, breakout_pct: float = 0.5, vol_confirm_mult: float = 1.5, min_overshoot_pct: float = 0.15,
    rearm_pct: float = 0.25, taiex_lookback_min: int = 2,
) -> dict[str, dict[float, list[float]]]:
    buckets: dict[str, dict[float, list[float]]] = {
        "agree": {h: [] for h in horizons_sec},
        "disagree": {h: [] for h in horizons_sec},
        "no_taiex_data": {h: [] for h in horizons_sec},
    }
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
            t_str = times[k]

            if taiex_day is None:
                bucket_key = "no_taiex_data"
            else:
                tx_dir = _taiex_direction(taiex_day, t_str[11:19], taiex_lookback_min)
                if tx_dir is None:
                    bucket_key = "no_taiex_data"
                elif (direction == "long" and tx_dir == "up") or (direction == "short" and tx_dir == "down"):
                    bucket_key = "agree"
                else:
                    bucket_key = "disagree"

            t0 = datetime.fromisoformat(t_str)
            for h in horizons_sec:
                from datetime import timedelta
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
                buckets[bucket_key][h].append(ret)
    return buckets


def _report(label: str, rets_by_h: dict[float, list[float]], horizons: list[float]) -> None:
    print(f"\n--- {label} ---")
    for h in horizons:
        rets = np.array(rets_by_h[h])
        if len(rets) == 0:
            print(f"  {h:5.0f}秒後: 無資料")
            continue
        win = float(np.mean(rets > 0) * 100)
        breakeven = (rets.sum() / len(rets)) * 100
        print(f"  {h:5.0f}秒後: n={len(rets):5d} 勝率={win:5.1f}% 均值={rets.mean():+.4f}% 損平={breakeven:6.1f}bps")


def main() -> None:
    print("載入4窗口股票資料...")
    windows_data = {wname: load_window(wdate) for wname, wdate in WINDOWS.items()}
    print("載入TX大盤1分K...")
    all_days_needed = sorted({d for _wname, (_stk, days) in windows_data.items() for d in days})
    taiex_by_day = load_taiex_days(all_days_needed)
    n_have = sum(1 for d in all_days_needed if d in taiex_by_day)
    print(f"  {n_have}/{len(all_days_needed)} 個交易日有TX 1分K資料")

    horizons = [1, 2, 3, 5, 10, 15, 20, 30, 45, 60]
    agg: dict[str, dict[float, list[float]]] = {
        "agree": {h: [] for h in horizons}, "disagree": {h: [] for h in horizons},
        "no_taiex_data": {h: [] for h in horizons},
    }
    for _wname, (all_by_stock, all_days) in windows_data.items():
        for d in all_days:
            day_data = {sid: days[d] for sid, days in all_by_stock.items() if d in days}
            if len(day_data) < 3:
                continue
            taiex_day = taiex_by_day.get(d)
            buckets = second_scalp_with_taiex(day_data, taiex_day, horizons)
            for key in agg:
                for h in horizons:
                    agg[key][h].extend(buckets[key][h])

    print(f"\n訊號分布：agree(跟TX同向)={len(agg['agree'][horizons[0]])}筆 "
          f"disagree(跟TX反向)={len(agg['disagree'][horizons[0]])}筆 "
          f"no_data={len(agg['no_taiex_data'][horizons[0]])}筆")
    _report("A. 跟TX同向 (agree)", agg["agree"], horizons)
    _report("B. 跟TX反向 (disagree)", agg["disagree"], horizons)
    _report("C. 無TX資料可比對 (no_taiex_data)", agg["no_taiex_data"], horizons)


if __name__ == "__main__":
    main()
