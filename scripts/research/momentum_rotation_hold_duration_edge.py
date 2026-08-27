"""2026-08-13：使用者觀察到今天真實7筆交易裡，4筆被搶佔平倉的持倉時間都在
15秒以內（2049=12.0s/-0.510%、2376=5.9s/-0.258%、2455=6.1s/-0.386%、
3035=11.2s/-0.560%），全部虧損——懷疑「秒級被搶佔」本身就是一個可辨識的
壞訊號（進場後幾乎立刻被更強訊號取代，代表當下是共同因子帶動的連環反應，
不是個股獨立動能），而不是「訊號方向錯了」。

這裡不是重新測「反方向」（那個已經在75天樣本上被推翻），是測一個更精確的
機制假說：用完整75天真實資料，把每一筆交易按「這筆倉位實際持有多久才出場」
分桶，看短持有時間（尤其是被搶佔、不是移動停利/收盤強平）的交易，勝率/報酬
是否系統性比長持有時間差——如果成立，代表edge不在「要不要反向」，而在
「偵測/迴避連環搶佔本身」（例如：進場後極短時間內就被搶佔的部位，不計入
損益、或該筆的分數不該被信任）。
"""

from __future__ import annotations

import sys
from collections import defaultdict
from datetime import datetime

import numpy as np

sys.path.insert(0, "scripts/research")
from momentum_rotation_final_ev_std_report import simulate_day  # noqa: E402
from momentum_rotation_redesign_search import WINDOWS, load_window  # noqa: E402

DURATION_BUCKETS = [
    ("<15s", 0, 15),
    ("15-60s", 15, 60),
    ("1-5min", 60, 300),
    ("5-15min", 300, 900),
    (">15min", 900, float("inf")),
]


def _duration_sec(entry_time: str, exit_time: str) -> float | None:
    if exit_time == "day_end":
        return None
    try:
        return (datetime.fromisoformat(exit_time) - datetime.fromisoformat(entry_time)).total_seconds()
    except (ValueError, TypeError):
        return None


def main() -> None:
    print("載入4窗口資料...")
    windows_data = {wname: load_window(wdate) for wname, wdate in WINDOWS.items()}

    base = dict(breakout_pct=0.5, trail_pct=1.0, vol_confirm_mult=1.5, rearm_pct=0.25,
                min_overshoot_pct=0.15, max_overshoot_pct=999.0, min_vol_ratio=1.5,
                preempt_mult=2.0, fade=False)

    all_trades = []
    for _wname, (all_by_stock, all_days) in windows_data.items():
        for d in all_days:
            day_data = {sid: days[d] for sid, days in all_by_stock.items() if d in days}
            if len(day_data) < 3:
                continue
            all_trades.extend(simulate_day(day_data, **base))

    print(f"\n總交易數={len(all_trades)}")

    # === 全部reason一起，按持有時長分桶 ===
    print("\n=== 全部出場原因，按持有時長分桶 ===")
    bucketed: dict[str, list[float]] = defaultdict(list)
    for t in all_trades:
        dur = _duration_sec(t["entry_time"], t["exit_time"])
        if dur is None:
            continue
        for label, lo, hi in DURATION_BUCKETS:
            if lo <= dur < hi:
                bucketed[label].append(t["ret_pct"])
                break
    for label, _lo, _hi in DURATION_BUCKETS:
        rets = np.array(bucketed[label])
        if len(rets) == 0:
            print(f"  {label:8s}: 無交易")
            continue
        win = float(np.mean(rets > 0) * 100)
        breakeven = (rets.sum() / len(rets)) * 100
        print(f"  {label:8s}: n={len(rets):5d} 勝率={win:5.1f}% 均值={rets.mean():+.4f}% 損平={breakeven:6.1f}bps")

    # === 只看「preempted」出場（今天7筆全是這個reason），按持有時長分桶 ===
    print("\n=== 只看 preempted 出場（今天真實7筆的reason），按持有時長分桶 ===")
    bucketed_pre: dict[str, list[float]] = defaultdict(list)
    for t in all_trades:
        if t.get("reason") != "preempted":
            continue
        dur = _duration_sec(t["entry_time"], t["exit_time"])
        if dur is None:
            continue
        for label, lo, hi in DURATION_BUCKETS:
            if lo <= dur < hi:
                bucketed_pre[label].append(t["ret_pct"])
                break
    for label, _lo, _hi in DURATION_BUCKETS:
        rets = np.array(bucketed_pre[label])
        if len(rets) == 0:
            print(f"  {label:8s}: 無交易")
            continue
        win = float(np.mean(rets > 0) * 100)
        breakeven = (rets.sum() / len(rets)) * 100
        print(f"  {label:8s}: n={len(rets):5d} 勝率={win:5.1f}% 均值={rets.mean():+.4f}% 損平={breakeven:6.1f}bps")

    # === 假說驗證：排除「極短持有(<15s)被搶佔」的交易後，其餘交易表現如何？===
    print("\n=== 假說：排除<15s就被搶佔的交易，剩下的交易表現 ===")
    filtered = []
    excluded = []
    for t in all_trades:
        dur = _duration_sec(t["entry_time"], t["exit_time"])
        if t.get("reason") == "preempted" and dur is not None and dur < 15:
            excluded.append(t)
        else:
            filtered.append(t)
    for name, trades in [("排除後(保留)", filtered), ("被排除的(<15s preempted)", excluded)]:
        rets = np.array([t["ret_pct"] for t in trades])
        if len(rets) == 0:
            print(f"  {name}: 無交易")
            continue
        win = float(np.mean(rets > 0) * 100)
        breakeven = (rets.sum() / len(rets)) * 100
        print(f"  {name}: n={len(rets):5d}({len(rets)/len(all_trades)*100:.1f}%) 勝率={win:5.1f}% "
              f"均值={rets.mean():+.4f}% 損平={breakeven:6.1f}bps")


if __name__ == "__main__":
    main()
