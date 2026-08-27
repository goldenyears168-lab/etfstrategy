"""2026-08-13：使用者問「一開始所挑的股票就是要去找這種常常有機會有動能延續
的，那要怎麼去找出這些標的」——這裡直接測試：用TRAIN窗口統計每檔股票的
「5秒微爆量訊號→接下來報酬延續」命中率，排名後只挑分數最高的K檔，套到完全
沒看過的HOLDOUT窗口上跑（單槽位輪動範圍縮小成篩選後的子集），看選股本身有沒有
可攜性（不是同一份資料上選、同一份資料上驗證的循環論證）。

跟稍早已經被推翻的「流動性子集」（今天blindspot workflow測過，tick密度最高
的3/6/8檔反而更差）不同——這裡篩選依據是「動能延續命中率」本身，不是成交量
大小，是直接針對使用者這次問題的假說。

命中率定義：對每個標的，在TRAIN窗口的全部tick上，用固定的5秒視窗+量能突破
偵測(不sweep，用一組中性參數：move_thresh=0.15%, vol_mult=2.0)找出全部候選
訊號，量測訊號後continuation_sec秒的報酬方向是否跟訊號方向一致（純訊號資訊量
測試，不含部位管理），命中率=正確方向的比例。
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta

import numpy as np

sys.path.insert(0, "scripts/research")
from momentum_breakout_strategy import simulate_portfolio_day as baseline_simulate  # noqa: E402
from momentum_rotation_5s_microburst_holdout_cv import simulate_day_microburst  # noqa: E402
from momentum_rotation_redesign_search import WINDOWS, load_window  # noqa: E402

MOVE_THRESH, VOL_MULT, CONTINUATION_SEC = 0.15, 2.0, 8.0
TOPK_GRID = [3, 4, 5, 6, 8]
BASE_MICROBURST = dict(trail_pct=1.0, preempt_mult=2.0, window_sec=5.0, cooldown_sec=10.0,
                        move_thresh_pct=0.15, vol_mult=2.5, hold_sec=8.0)
BASELINE_PARAMS = dict(breakout_pct=0.5, trail_pct=1.0, vol_confirm_mult=1.5, rearm_pct=0.25,
                        min_overshoot_pct=0.15, min_vol_ratio=1.5, preempt_mult=2.0)


def per_stock_hit_rate(all_by_stock: dict, all_days: list) -> dict[str, tuple[float, int]]:
    """回傳 {sid: (命中率, 訊號數)}——用TRAIN窗口的原始tick，量測每檔股票
    自己的5秒微爆量訊號後continuation_sec秒方向對不對，不含部位管理。"""
    hits: dict[str, list[int]] = {sid: [] for sid in all_by_stock}
    for sid, days in all_by_stock.items():
        for d in all_days:
            if d not in days:
                continue
            times, prices, volumes = days[d]
            dts = [datetime.fromisoformat(t) for t in times]
            buf: list[tuple] = []
            vol_hist: list[float] = []
            win_td = timedelta(seconds=5.0)
            last_signal = None
            cool_td = timedelta(seconds=10.0)
            for k in range(len(dts)):
                t, p, v = dts[k], float(prices[k]), float(volumes[k])
                buf.append((t, p, v))
                while buf and (t - buf[0][0]) > win_td:
                    buf.pop(0)
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
                if abs(move_pct) < MOVE_THRESH or vol_burst < VOL_MULT:
                    continue
                direction = 1 if move_pct > 0 else -1
                last_signal = t
                deadline = t + timedelta(seconds=CONTINUATION_SEC)
                future_idx = None
                for j in range(k + 1, len(dts)):
                    if dts[j] >= deadline:
                        future_idx = j
                        break
                if future_idx is None:
                    continue
                p_future = float(prices[future_idx])
                ret = (p_future - p) * direction
                hits[sid].append(1 if ret > 0 else 0)
    return {sid: (float(np.mean(v)) if v else 0.0, len(v)) for sid, v in hits.items()}


def _run_microburst_subset(windows_subset: dict, allowed_sids: set[str] | None) -> dict:
    all_trades, per_day = [], {}
    for _wname, (all_by_stock, all_days) in windows_subset.items():
        for d in all_days:
            day_data = {sid: days[d] for sid, days in all_by_stock.items() if d in days
                        and (allowed_sids is None or sid in allowed_sids)}
            if len(day_data) < 2:
                continue
            trades = simulate_day_microburst(day_data, **BASE_MICROBURST)
            all_trades.extend(trades)
            per_day[d] = per_day.get(d, 0.0) + sum(t["ret_pct"] for t in trades)
    rets = np.array([t["ret_pct"] for t in all_trades]) if all_trades else np.array([])
    day_rets = np.array(list(per_day.values())) if per_day else np.array([])
    if len(rets) < 10 or len(day_rets) == 0 or day_rets.std() == 0:
        return {"n": len(rets), "risk_adj": float("-inf"), "breakeven_bps": 0.0, "win_rate": 0.0}
    breakeven = (rets.sum() / len(rets)) * 100
    win = float(np.mean(rets > 0) * 100)
    day_mean, day_std = float(day_rets.mean()), float(day_rets.std())
    return {"n": len(rets), "risk_adj": day_mean / day_std, "breakeven_bps": breakeven, "win_rate": win}


def _run_baseline_subset(windows_subset: dict, allowed_sids: set[str] | None) -> dict:
    all_trades, per_day = [], {}
    for _wname, (all_by_stock, all_days) in windows_subset.items():
        for d in all_days:
            day_data = {sid: days[d] for sid, days in all_by_stock.items() if d in days
                        and (allowed_sids is None or sid in allowed_sids)}
            if len(day_data) < 2:
                continue
            trades = baseline_simulate(day_data, **BASELINE_PARAMS)
            all_trades.extend(trades)
            per_day[d] = per_day.get(d, 0.0) + sum(t["ret_pct"] for t in trades)
    rets = np.array([t["ret_pct"] for t in all_trades]) if all_trades else np.array([])
    day_rets = np.array(list(per_day.values())) if per_day else np.array([])
    if len(rets) < 10 or len(day_rets) == 0 or day_rets.std() == 0:
        return {"n": len(rets), "risk_adj": float("-inf"), "breakeven_bps": 0.0, "win_rate": 0.0}
    breakeven = (rets.sum() / len(rets)) * 100
    win = float(np.mean(rets > 0) * 100)
    day_mean, day_std = float(day_rets.mean()), float(day_rets.std())
    return {"n": len(rets), "risk_adj": day_mean / day_std, "breakeven_bps": breakeven, "win_rate": win}


def main() -> None:
    print("載入4窗口資料...")
    all_windows = {wname: load_window(wdate) for wname, wdate in WINDOWS.items()}
    wnames = list(all_windows.keys())

    fold_results = []
    for holdout_name in wnames:
        train_windows = {k: v for k, v in all_windows.items() if k != holdout_name}
        holdout_windows = {holdout_name: all_windows[holdout_name]}
        print(f"\n### Fold: holdout={holdout_name} · train={list(train_windows.keys())} ###")

        # 在TRAIN窗口(合併3個)上算每檔股票的命中率排名
        combined_by_stock: dict[str, dict] = {}
        for _wname, (all_by_stock, all_days) in train_windows.items():
            for sid, days in all_by_stock.items():
                combined_by_stock.setdefault(sid, {}).update(days)
        combined_days = sorted({d for days in combined_by_stock.values() for d in days})
        hit_rates = per_stock_hit_rate(combined_by_stock, combined_days)
        ranked = sorted(hit_rates.items(), key=lambda kv: -kv[1][0])
        print("  TRAIN命中率排名: " + ", ".join(f"{sid}({hr*100:.1f}%,n={n})" for sid, (hr, n) in ranked))

        best = None
        for k in TOPK_GRID:
            top_sids = {sid for sid, _ in ranked[:k]}
            m = _run_microburst_subset(train_windows, top_sids)
            if best is None or m["risk_adj"] > best[1]["risk_adj"]:
                best = (k, m)
        k_best, train_m = best
        top_sids_final = {sid for sid, _ in ranked[:k_best]}
        print(f"  train最佳: top{k_best}檔={sorted(top_sids_final)} "
              f"(train risk-adj={train_m['risk_adj']:.3f} 損平={train_m['breakeven_bps']:.1f}bps)")

        holdout_screened = _run_microburst_subset(holdout_windows, top_sids_final)
        holdout_all12 = _run_microburst_subset(holdout_windows, None)
        holdout_baseline = _run_baseline_subset(holdout_windows, None)
        print(f"  >>> HOLDOUT({holdout_name}) 篩選top{k_best}檔+5s微爆量: n={holdout_screened['n']:4d} "
              f"勝率={holdout_screened['win_rate']:5.1f}% 損平={holdout_screened['breakeven_bps']:6.1f}bps risk-adj={holdout_screened['risk_adj']:+.3f}")
        print(f"  >>> HOLDOUT({holdout_name}) 全12檔+5s微爆量    : n={holdout_all12['n']:4d} "
              f"勝率={holdout_all12['win_rate']:5.1f}% 損平={holdout_all12['breakeven_bps']:6.1f}bps risk-adj={holdout_all12['risk_adj']:+.3f}")
        print(f"  >>> HOLDOUT({holdout_name}) baseline(現行規格)  : n={holdout_baseline['n']:4d} "
              f"勝率={holdout_baseline['win_rate']:5.1f}% 損平={holdout_baseline['breakeven_bps']:6.1f}bps risk-adj={holdout_baseline['risk_adj']:+.3f}")
        fold_results.append({"holdout": holdout_name, "k": k_best, "screened": holdout_screened,
                              "all12": holdout_all12, "baseline": holdout_baseline})

    print("\n" + "=" * 100)
    print("=== 4折總結：篩選後子集 vs 全12檔（同樣用5s微爆量規則）===")
    n_wins_vs_all12 = 0
    n_wins_vs_baseline = 0
    for r in fold_results:
        beats_all12 = r["screened"]["risk_adj"] > r["all12"]["risk_adj"]
        beats_base = r["screened"]["risk_adj"] > r["baseline"]["risk_adj"]
        n_wins_vs_all12 += int(beats_all12)
        n_wins_vs_baseline += int(beats_base)
        print(f"  {r['holdout']:12s} (top{r['k']}): 篩選risk-adj={r['screened']['risk_adj']:+.3f}  "
              f"vs 全12檔risk-adj={r['all12']['risk_adj']:+.3f}({'篩選贏' if beats_all12 else '篩選輸'})  "
              f"vs baseline risk-adj={r['baseline']['risk_adj']:+.3f}({'篩選贏' if beats_base else '篩選輸'})")
    print(f"\n  篩選子集 vs 全12檔: {n_wins_vs_all12}/4 折篩選較好")
    print(f"  篩選子集 vs baseline: {n_wins_vs_baseline}/4 折篩選較好")


if __name__ == "__main__":
    main()
