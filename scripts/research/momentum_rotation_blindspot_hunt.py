"""2026-08-13：使用者要求「像柯南辦案一樣找盲點」——今天一整天都在同一個框架
裡打轉（同一個突破+量能訊號、同一套trailing stop+preemption機制，只調參數／
只反方向），從來沒有拆開來看這幾件事：

  A. 4窗口合併平均會不會蓋掉「其實只有某個regime有edge」的真相？
  B. 從來沒看過「進場時間點」這個維度——開盤剛突破 vs 盤中後段突破，勝率/期望值
     是否系統性不同？這個策略的volume baseline是expanding median，開盤前幾筆
     的baseline天生不穩，可能系統性製造假訊號。
  C. trailing stop + preemption這套「怎麼出場/怎麼換手」的複雜機制，會不會
     本身就在吃掉一個更簡單、更乾淨的訊號？拆開來看：如果完全不管理部位，
     單純看「訊號後固定N分鐘，價格有沒有繼續朝原方向走」，訊號本身到底有沒有
     資訊量？
  D. 12檔同時訂閱、單槽位輪動——這些訊號會不會高度同時發生（代表其實都在
     反應同一個大盤/類股共同因子，不是各自獨立的個股動能），讓「挑分數最高」
     變成偽獨立的重複下注？

這裡不是重新設計交易規則，是純粹的診斷/探索，找出「為什麼今天所有修修補補
都在打平附近」背後有沒有被平均數蓋住的真相。
"""

from __future__ import annotations

import sys
from collections import defaultdict

import numpy as np

sys.path.insert(0, "scripts/research")
from momentum_rotation_redesign_search import WINDOWS, load_window  # noqa: E402


def simulate_day_diag(
    stock_day_data: dict,
    *,
    breakout_pct: float = 0.5, trail_pct: float = 1.0, vol_confirm_mult: float = 1.5,
    rearm_pct: float = 0.25, min_overshoot_pct: float = 0.15, min_vol_ratio: float = 1.5,
    preempt_mult: float = 2.0,
) -> tuple[list[dict], list[dict]]:
    """回傳(trades, all_candidates)——all_candidates含每一個「訊號當下」的紀錄
    (不管有沒有真的成交)，用來做D的同時性分析；trades是實際成交紀錄(含entry_hour
    供B分析)。"""
    merged: list[tuple] = []
    meta: dict = {}
    for sid, (times, prices, volumes) in stock_day_data.items():
        if prices.size < 2:
            continue
        open_price = float(prices[0])
        meta[sid] = {
            "open": open_price,
            "long_trigger": open_price * (1 + breakout_pct / 100.0),
            "short_trigger": open_price * (1 - breakout_pct / 100.0),
            "rearm_hi": open_price * (1 + rearm_pct / 100.0),
            "rearm_lo": open_price * (1 - rearm_pct / 100.0),
        }
        for k in range(1, len(times)):
            merged.append((times[k], sid, float(prices[k]), float(volumes[k])))
    merged.sort(key=lambda x: x[0])

    vol_history = {sid: [] for sid in meta}
    armed = {sid: True for sid in meta}
    last_price = {sid: meta[sid]["open"] for sid in meta}
    trades: list[dict] = []
    all_candidates: list[dict] = []
    position: dict | None = None

    for t, sid, p, v in merged:
        st = meta[sid]
        last_price[sid] = p
        vh = vol_history[sid]
        baseline = max(np.median(vh), 1e-9) if vh else 1.0
        vh.append(v)

        is_held = position is not None and position["sid"] == sid
        if is_held:
            if position["direction"] == "long":
                position["peak_trough"] = max(position["peak_trough"], p)
                stop = position["peak_trough"] * (1 - trail_pct / 100.0)
                hit = p <= stop
            else:
                position["peak_trough"] = min(position["peak_trough"], p)
                stop = position["peak_trough"] * (1 + trail_pct / 100.0)
                hit = p >= stop
            if hit:
                exit_price = float(p)
                ret_pct = (
                    (exit_price - position["fill"]) / position["fill"] * 100.0
                    if position["direction"] == "long"
                    else (position["fill"] - exit_price) / position["fill"] * 100.0
                )
                trades.append({**position, "exit_time": t, "exit": exit_price, "ret_pct": ret_pct, "reason": "trail_stop"})
                position = None
                armed[sid] = False
            continue

        if not armed[sid]:
            if st["rearm_lo"] <= p <= st["rearm_hi"]:
                armed[sid] = True
            continue

        price_hits_long = p >= st["long_trigger"]
        price_hits_short = p <= st["short_trigger"]
        if not (price_hits_long or price_hits_short) or v < vol_confirm_mult * baseline:
            continue
        direction = "long" if price_hits_long else "short"
        trigger = st["long_trigger"] if direction == "long" else st["short_trigger"]
        overshoot = abs(p - trigger) / st["open"] * 100.0
        vol_ratio = v / baseline
        all_candidates.append({"t": t, "sid": sid, "direction": direction, "vol_hist_len": len(vh)})
        if overshoot < min_overshoot_pct or vol_ratio < min_vol_ratio:
            continue
        score = overshoot * vol_ratio
        fill = float(p)
        candidate = {
            "sid": sid, "direction": direction, "fill": fill, "entry": fill,
            "entry_time": t, "entry_score": score, "peak_trough": fill,
            "overshoot": overshoot, "vol_ratio": vol_ratio, "vol_hist_len": len(vh),
        }

        if position is None:
            position = candidate
            armed[sid] = False
        elif score >= preempt_mult * position["entry_score"]:
            held_sid = position["sid"]
            exit_price = last_price[held_sid]
            ret_pct = (
                (exit_price - position["fill"]) / position["fill"] * 100.0
                if position["direction"] == "long"
                else (position["fill"] - exit_price) / position["fill"] * 100.0
            )
            trades.append({**position, "exit_time": t, "exit": exit_price, "ret_pct": ret_pct, "reason": "preempted"})
            armed[held_sid] = False
            position = candidate
            armed[sid] = False

    if position is not None:
        exit_price = last_price[position["sid"]]
        ret_pct = (
            (exit_price - position["fill"]) / position["fill"] * 100.0
            if position["direction"] == "long"
            else (position["fill"] - exit_price) / position["fill"] * 100.0
        )
        trades.append({**position, "exit_time": "day_end", "exit": exit_price, "ret_pct": ret_pct, "reason": "day_end_forced"})
    return trades, all_candidates


def fixed_horizon_test(stock_day_data: dict, horizons_min: list[int], breakout_pct: float = 0.5,
                        vol_confirm_mult: float = 1.5, rearm_pct: float = 0.25) -> dict:
    """C：完全不管理部位，訊號後固定N分鐘看方向對不對——純訊號資訊量測試，
    不含trailing stop/preemption/搶佔的任何干擾。每個訊號獨立算(可重疊)，不是
    模擬一個真實可執行的策略，只是問「這個事件本身有沒有資訊量」。"""
    out: dict[int, list[float]] = {h: [] for h in horizons_min}
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
            direction = "long" if hits_long else "short"
            armed = False
            t0 = times[k]
            for h in horizons_min:
                deadline = _add_minutes(t0, h)
                future_idx = None
                for j in range(k + 1, len(times)):
                    if times[j] >= deadline:
                        future_idx = j
                        break
                if future_idx is None:
                    continue
                p_future = float(prices[future_idx])
                ret = (p_future - p) / p * 100.0 if direction == "long" else (p - p_future) / p * 100.0
                out[h].append(ret)
    return out


def _add_minutes(iso_t: str, minutes: int) -> str:
    """原本用.isoformat()（'T'分隔）重組時間戳，但tick CSV的times[]是空白分隔
    （見reports/research/expert_pool_futures_tick/*.csv：'2026-07-13 09:06:42'）
    ——'T'(0x54) > ' '(0x20)，字串比較times[j] >= deadline會因為分隔符不同永遠
    判False，導致C段「無資料」，不是真的沒有訊號。改用str()（datetime預設就是
    空白分隔）對齊格式。"""
    from datetime import datetime, timedelta
    dt = datetime.fromisoformat(iso_t)
    return str(dt + timedelta(minutes=minutes))


def main() -> None:
    print("載入4窗口資料...")
    windows_data = {wname: load_window(wdate) for wname, wdate in WINDOWS.items()}

    # === A: 每個窗口分開報，不合併 ===
    print("\n" + "=" * 70)
    print("A. 每個窗口分開看（現行baseline規格）—— 會不會被4窗口平均蓋住真相？")
    print("=" * 70)
    for wname, (all_by_stock, all_days) in windows_data.items():
        all_trades = []
        for d in all_days:
            day_data = {sid: days[d] for sid, days in all_by_stock.items() if d in days}
            if len(day_data) < 3:
                continue
            trades, _ = simulate_day_diag(day_data)
            all_trades.extend(trades)
        rets = np.array([t["ret_pct"] for t in all_trades])
        if len(rets) == 0:
            print(f"  {wname:12s}: 無交易")
            continue
        win = float(np.mean(rets > 0) * 100)
        gross = rets.sum() / len(all_days)
        breakeven = (rets.sum() / len(rets)) * 100
        print(f"  {wname:12s}: n={len(rets):4d} 天數={len(all_days):3d} 勝率={win:5.1f}% "
              f"gross日均={gross:+7.3f}% 損平={breakeven:6.1f}bps")

    # === B: 依進場時段分組 ===
    print("\n" + "=" * 70)
    print("B. 依進場時段分組（現行baseline規格，4窗口合併）—— 開盤vs盤中後段有沒有差？")
    print("=" * 70)
    hour_bucket: dict[str, list[float]] = defaultdict(list)
    for _wname, (all_by_stock, all_days) in windows_data.items():
        for d in all_days:
            day_data = {sid: days[d] for sid, days in all_by_stock.items() if d in days}
            if len(day_data) < 3:
                continue
            trades, _ = simulate_day_diag(day_data)
            for tr in trades:
                hh = tr["entry_time"][11:13] if len(tr["entry_time"]) > 13 else "??"
                hour_bucket[hh].append(tr["ret_pct"])
    for hh in sorted(hour_bucket):
        rets = np.array(hour_bucket[hh])
        win = float(np.mean(rets > 0) * 100)
        breakeven = (rets.sum() / len(rets)) * 100
        print(f"  {hh}時: n={len(rets):4d} 勝率={win:5.1f}% 單筆均值={rets.mean():+.4f}% 損平={breakeven:6.1f}bps")

    # === C: 純訊號資訊量測試（不含部位管理）===
    print("\n" + "=" * 70)
    print("C. 純訊號測試：完全不管理部位，訊號後固定N分鐘方向對不對？（可重疊、非可執行策略）")
    print("=" * 70)
    horizons = [1, 3, 5, 10, 15, 30]
    agg: dict[int, list[float]] = {h: [] for h in horizons}
    for _wname, (all_by_stock, all_days) in windows_data.items():
        for d in all_days:
            day_data = {sid: days[d] for sid, days in all_by_stock.items() if d in days}
            if len(day_data) < 3:
                continue
            res = fixed_horizon_test(day_data, horizons)
            for h in horizons:
                agg[h].extend(res[h])
    for h in horizons:
        rets = np.array(agg[h])
        if len(rets) == 0:
            print(f"  {h:2d}分鐘後: 無資料")
            continue
        win = float(np.mean(rets > 0) * 100)
        print(f"  {h:2d}分鐘後: n={len(rets):5d} 勝率={win:5.1f}% 均值={rets.mean():+.4f}% 標準差={rets.std():.4f}%")

    # === D: 訊號同時性（跨標的相關性）===
    print("\n" + "=" * 70)
    print("D. 訊號同時性：候選訊號是不是常常好幾檔同一時間一起發生？（共同因子嫌疑）")
    print("=" * 70)
    total_candidates = 0
    clustered = 0  # 同一天、60秒內有≥2檔同時發出候選訊號
    for _wname, (all_by_stock, all_days) in windows_data.items():
        for d in all_days:
            day_data = {sid: days[d] for sid, days in all_by_stock.items() if d in days}
            if len(day_data) < 3:
                continue
            _trades, cands = simulate_day_diag(day_data)
            total_candidates += len(cands)
            from datetime import datetime
            times_parsed = sorted(datetime.fromisoformat(c["t"]) for c in cands)
            for i, ti in enumerate(times_parsed):
                near = sum(1 for tj in times_parsed if 0 < abs((tj - ti).total_seconds()) <= 60)
                if near >= 1:
                    clustered += 1
    pct = clustered / total_candidates * 100 if total_candidates else 0
    print(f"  候選訊號總數={total_candidates}，60秒內有其他標的同時發訊號的比例={pct:.1f}%")
    print("  （比例高代表訊號很可能共享同一個大盤/類股層級的驅動因子，不是各自獨立的個股動能）")


if __name__ == "__main__":
    main()
