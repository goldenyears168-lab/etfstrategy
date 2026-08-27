#!/usr/bin/env python3
"""使用者問：4%門檻n更多，能不能全收（不互斥），資金衝突時才排序——這是資金池
層級問題，不是單筆平均報酬能回答的（前一支腳本已證實單筆平均7%門檻更好，但
如果資金本來就沒用滿，多接一些較低品質但為正期望值的訊號，整體組合報酬可能
仍然更好）。

方法：沿用capital_simulation.py同一套邏輯（固定總資金、單股上限、逐日mark-to-
market），比較「只收fgap>=7%」vs「fgap>=4%全收，資金/單股上限衝突時用n_seats
排優先序」兩種資金配置下的組合層級表現（總報酬/Sharpe/最大回撤/資金使用率），
不是只看單筆平均。

用post_dump_long_rolling_dip_results.json快取（已含entry_day/exit_day/
hold_days/fgap/ret），不重跑分K查詢。

PYTHONPATH=src:scripts/research .venv/bin/python scripts/research/dayflip_post_dump_long_capital_portfolio_4v7.py
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
RESULTS_CACHE = ROOT / "reports/research/dayflip_fgap_calibration/post_dump_long_rolling_dip_results.json"

TOTAL_CAPITAL_NTD = 300_000.0
SINGLE_NAME_CAP_PCT = 15.0
MARGIN_RATE = 0.135
LOT_SHARES = 2000


def estimate_margin_ntd(price: float) -> float:
    return price * LOT_SHARES * MARGIN_RATE


def run_portfolio(trades: list[dict], *, fgap_floor: float, single_name_cap_pct: float = SINGLE_NAME_CAP_PCT) -> dict:
    """逐日模擬：candidates依entry_day排序處理，每天先出場(釋放保證金)再進場
    （進場用n_seats排序當優先序，資金不夠就跳過）。"""
    qualifying = [t for t in trades if t["fgap"] >= fgap_floor]
    if not qualifying:
        return {"n_entered": 0, "n_skipped_capital": len(qualifying)}

    calendar = sorted({t["entry_day"] for t in qualifying} | {t["exit_day"] for t in qualifying})
    by_entry_day: dict[str, list[dict]] = {}
    for t in qualifying:
        by_entry_day.setdefault(t["entry_day"], []).append(t)

    open_positions: list[dict] = []  # {"exit_day", "margin", "ret", "entry_px"}
    margin_used = 0.0
    cap_per_name = TOTAL_CAPITAL_NTD * single_name_cap_pct / 100
    daily_nav: list[float] = []
    realized_pnl = 0.0
    n_entered = 0
    n_skipped_capital = 0

    for day in calendar:
        # 1) 出場：釋放保證金、記已實現損益
        still_open = []
        for p in open_positions:
            if p["exit_day"] == day:
                pnl = p["margin"] * (p["ret"] / 100) / (MARGIN_RATE)  # notional*ret%，margin/MARGIN_RATE=notional
                realized_pnl += pnl
                margin_used -= p["margin"]
            else:
                still_open.append(p)
        open_positions = still_open

        # 2) 進場：當天訊號依n_seats(這裡沒存，改用fgap當代理排序，越大優先——
        #    跟pick_signal()精神一致：訊號越強越優先)由強到弱嘗試進場
        todays = sorted(by_entry_day.get(day, []), key=lambda t: -t["fgap"])
        for t in todays:
            margin = estimate_margin_ntd(t["entry_px"])
            if margin > cap_per_name:
                n_skipped_capital += 1
                continue
            if margin_used + margin > TOTAL_CAPITAL_NTD:
                n_skipped_capital += 1
                continue
            margin_used += margin
            open_positions.append({"exit_day": t["exit_day"], "margin": margin, "ret": t["ret"], "entry_px": t["entry_px"]})
            n_entered += 1

        nav = TOTAL_CAPITAL_NTD + realized_pnl
        daily_nav.append(nav)

    nav_arr = np.array(daily_nav)
    total_ret_pct = (nav_arr[-1] / TOTAL_CAPITAL_NTD - 1) * 100 if len(nav_arr) else 0.0
    running_max = np.maximum.accumulate(nav_arr) if len(nav_arr) else np.array([TOTAL_CAPITAL_NTD])
    drawdown = (nav_arr - running_max) / running_max * 100 if len(nav_arr) else np.array([0.0])
    max_dd = float(drawdown.min()) if len(drawdown) else 0.0
    daily_rets = np.diff(nav_arr) / nav_arr[:-1] * 100 if len(nav_arr) > 1 else np.array([0.0])
    sharpe_like = float(daily_rets.mean() / daily_rets.std()) if daily_rets.std() > 0 else float("nan")

    return {
        "n_qualifying": len(qualifying), "n_entered": n_entered, "n_skipped_capital": n_skipped_capital,
        "total_ret_pct": total_ret_pct, "max_dd_pct": max_dd, "sharpe_like_daily": sharpe_like,
        "final_nav": float(nav_arr[-1]) if len(nav_arr) else TOTAL_CAPITAL_NTD,
    }


def main() -> None:
    trades = json.loads(RESULTS_CACHE.read_text(encoding="utf-8"))
    print(f"共{len(trades)}筆已知訊號可供資金排程模擬\n")
    print("⚠️ margin用estimate_margin_ntd()粗估公式(價格×2000×13.5%)，不是真實查詢——"
          "今天實測6274真實保證金645,165 vs 粗估413,100，粗估會系統性低估，這裡的"
          "n_entered/總報酬可能因此偏樂觀，僅供相對比較參考，不是精確預測\n")

    scenarios = [
        ("A) fgap>=7% + 單股上限15%(舊)", 7.0, 15.0),
        ("B) fgap>=4% + 單股上限15%(舊cap+新floor)", 4.0, 15.0),
        ("C) fgap>=4% + 單股上限100%(現行live設定)", 4.0, 100.0),
    ]
    results = {}
    for label, floor, cap_pct in scenarios:
        r = run_portfolio(trades, fgap_floor=floor, single_name_cap_pct=cap_pct)
        results[label] = r
        print(f"=== {label} ===")
        print(f"  合格訊號數: {r['n_qualifying']}  實際進場: {r['n_entered']}  "
              f"因資金/單股上限跳過: {r['n_skipped_capital']}")
        print(f"  總報酬: {r['total_ret_pct']:+.1f}%  最大回撤: {r['max_dd_pct']:.1f}%  "
              f"日Sharpe_like: {r['sharpe_like_daily']:.3f}  期末NAV: {r['final_nav']:,.0f}")
        print()

    b, c = results["B) fgap>=4% + 單股上限15%(舊cap+新floor)"], results["C) fgap>=4% + 單股上限100%(現行live設定)"]
    print("=== 單股上限拿掉的邊際效果（B→C，同樣fgap>=4%，只變cap）===")
    print(f"  進場筆數: {b['n_entered']} → {c['n_entered']} ({c['n_entered']-b['n_entered']:+d})")
    print(f"  總報酬: {b['total_ret_pct']:+.1f}% → {c['total_ret_pct']:+.1f}% "
          f"({c['total_ret_pct']-b['total_ret_pct']:+.1f}pp)")
    print(f"  最大回撤: {b['max_dd_pct']:.1f}% → {c['max_dd_pct']:.1f}% "
          f"({c['max_dd_pct']-b['max_dd_pct']:+.1f}pp)")
    print(f"  Sharpe_like: {b['sharpe_like_daily']:.3f} → {c['sharpe_like_daily']:.3f}")


if __name__ == "__main__":
    main()
