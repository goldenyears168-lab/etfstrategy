#!/usr/bin/env python3
"""momentum-rotation 成本調整後日均報酬重新驗證（2026-08-13健檢後補做）.

背景：config/order.yaml 的 momentum-rotation notes 原本寫「回測保證金打滿版本日均
90%/天已確認是資料洩漏，可信估計約3.58%/天，見
reports/research/momentum_breakout_expert_pool_futures_summary.md「可信的數字」
段落」——健檢時直接去查那份文件，**完全找不到「可信的數字」這個段落標題，也找不到
3.58這個數字**（repo內grep "3.58"也查無任何momentum-rotation相關結果）。文件裡
唯一有出處的類似數字是「純FCFS排隊規則4.19%/天」（第38行），跟3.58不是同一個數字
也不同方法論。也就是說撐著這個策略「報酬不是資料洩漏假象」信心的關鍵數字，先前
查無實據。

本腳本用**現行部署設定**（12檔UNIVERSE、單槽位輪動+動態搶佔，跟
src/order/momentum_rotation_signal.py的UNIVERSE逐一核對過完全一致）、直接
import研究SSOT `momentum_breakout_strategy.simulate_portfolio_day`（不是重新
實作，訊號/出場邏輯保證跟已驗證版本一致），對4個完全獨立的市場窗口（IS漲盤/OOS
拉回/W3盤整/W4）重新跑一次，並且**這次真的把成本疊上去**——用文件自己記錄過的
Roll(1984)隱含價差估計範圍（5~29bps）跟壓力測試上限（76bps）當基準，掃過整個
成本區間看報酬曲線長什麼樣子。

結論（詳見執行輸出）：
  - Gross（未扣成本）：4窗口合併75天/1248筆交易，日均+22.8%，每個獨立窗口
    單獨看都是正的（12.4%~35.5%），勝率84.8%~91.3%全部窗口一致——這個效應本身
    不是憑空捏造，4個完全不重疊的市場狀態下都站得住腳。
  - 套用文件自己記錄過的成本範圍（5~76bps來回）：淨日均落在約+10.2%~+22.0%
    （全4窗合併）或+7.1%~+16.9%（排除IS只看OOS/W3/W4，避免調參偏誤的更保守
    估計）。
  - 若要讓淨日均剛好等於3.58%，來回成本必須達到約100~116bps——**遠超文件記錄
    過的5~76bps範圍**，沒有找到任何證據支持這麼高的成本假設。
  - 3.58%這個數字唯一勉強對得上的組合（純巧合或可能是原始推導方式）：排除IS、
    只看OOS/W3/W4、套用~100bps成本，得到+3.73%，接近但不精確等於3.58%——不能
    確認這就是原始來源，只是誠實記錄這是我能找到最接近的重建路徑。

PYTHONPATH=src:scripts/research .venv/bin/python scripts/research/momentum_rotation_cost_adjusted_reaudit.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, "scripts/research")
from momentum_breakout_strategy import (  # noqa: E402
    UNIVERSE,
    load_day_bars_with_times,
    simulate_portfolio_day,
)

TICK_DIR = Path("reports/research/expert_pool_futures_tick")
WINDOWS = {
    "IS(漲盤)": "2026-07-13_2026-08-11",
    "OOS(拉回)": "2025-10-20_2025-11-24",
    "W3(盤整)": "2025-11-28_2025-12-19",
    "W4": "2026-02-09_2026-03-06",
}
COST_BPS_SCENARIOS = [5, 10, 20, 29, 50, 76, 100, 150]
OUT_PATH = Path("reports/research/momentum_rotation_cost_adjusted_reaudit.json")


def load_window(wdate: str) -> tuple[dict, list[str]]:
    all_by_stock: dict[str, dict] = {}
    for sid in UNIVERSE:
        matches = list(TICK_DIR.glob(f"*{sid}_*{wdate}*.csv"))
        if not matches:
            continue
        days: dict = {}
        for p in matches:
            days.update(load_day_bars_with_times(p))
        all_by_stock[sid] = days
    all_days = sorted(set().union(*[set(d.keys()) for d in all_by_stock.values()]))
    return all_by_stock, all_days


def main() -> None:
    results_by_window: dict[str, dict] = {}
    for wname, wdate in WINDOWS.items():
        all_by_stock, all_days = load_window(wdate)
        taken = []
        for d in all_days:
            day_data = {sid: days[d] for sid, days in all_by_stock.items() if d in days}
            for trade in simulate_portfolio_day(day_data):
                trade["name"] = UNIVERSE[trade["sid"]][1]
                taken.append(trade)
        results_by_window[wname] = {"trades": taken, "n_days": len(all_days)}
        rets = np.array([t["ret_pct"] for t in taken])
        n_preempt = sum(1 for t in taken if t["reason"] == "preempted")
        print(f"{wname}: n_days={len(all_days)} n_trades={len(taken)} "
              f"gross_day_mean={rets.sum()/len(all_days):.3f}% "
              f"win={np.mean(rets>0)*100:.1f}% n_preempt={n_preempt}")

    all_trades = [t for w in results_by_window.values() for t in w["trades"]]
    all_days_n = sum(w["n_days"] for w in results_by_window.values())
    rets = np.array([t["ret_pct"] for t in all_trades])
    gross = rets.sum() / all_days_n
    n_per_day = len(rets) / all_days_n
    print(f"\n=== 4窗口合併（{all_days_n}天，{len(rets)}筆交易，{n_per_day:.2f}筆/天）===")
    print(f"gross日均={gross:.3f}%")
    for c in COST_BPS_SCENARIOS:
        net = (rets - c / 100.0).sum() / all_days_n
        print(f"  {c:>4}bps -> 淨日均={net:+.3f}%  勝率(扣成本後)={np.mean(rets - c/100.0 > 0)*100:.1f}%")
    breakeven_bps = (rets.sum() / len(rets)) * 100
    print(f"損益兩平來回成本：約 {breakeven_bps:.1f} bps/筆")

    oos_trades = [t for wn in ("OOS(拉回)", "W3(盤整)", "W4") for t in results_by_window[wn]["trades"]]
    oos_days = sum(results_by_window[wn]["n_days"] for wn in ("OOS(拉回)", "W3(盤整)", "W4"))
    oos_rets = np.array([t["ret_pct"] for t in oos_trades])
    oos_gross = oos_rets.sum() / oos_days
    print(f"\n=== 排除IS，只看OOS/W3/W4（避免調參偏誤，{oos_days}天，{len(oos_rets)}筆）===")
    print(f"gross日均={oos_gross:.3f}%")
    for c in COST_BPS_SCENARIOS + [116]:
        net = (oos_rets - c / 100.0).sum() / oos_days
        print(f"  {c:>4}bps -> 淨日均={net:+.3f}%")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps({
        "windows": {wn: {"n_days": w["n_days"], "n_trades": len(w["trades"]),
                          "gross_day_mean_pct": round(sum(t["ret_pct"] for t in w["trades"]) / w["n_days"], 3)}
                    for wn, w in results_by_window.items()},
        "pooled_4window": {"n_days": all_days_n, "n_trades": len(rets),
                            "gross_day_mean_pct": round(gross, 3), "breakeven_bps": round(breakeven_bps, 1)},
        "pooled_oos_only": {"n_days": oos_days, "n_trades": len(oos_rets),
                             "gross_day_mean_pct": round(oos_gross, 3)},
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nWrote {OUT_PATH}")


if __name__ == "__main__":
    main()
