"""2026-08-13：使用者提出的新支線——今天早上4筆真金交易全賠、且已知搶佔進場
的overshoot(1.617%)遠高於新鮮進場(0.942%)，代表這套訊號常常是「追價追到頭」
才成交。與其一直在「原方向、調參數」裡打轉（今天已經搜過quality threshold/
preempt_mult/trail_pct/overshoot範圍等組合，最佳候選損平17.1bps仍低於29bps
成本上限），這裡測試反方向假說：訊號本身（突破+量能確認）當成「過熱/追價過頭」
的偵測器，實際下單方向整個反過來——原本要long的地方改short（賭噴出去的會拉
回)，原本要short的地方改long（賭殺出去的會反彈）。

刻意只做「方向反轉」這一個變數，其餘（overshoot/vol_ratio評分、rearm、
preempt_mult、trailing stop機制）完全比照
momentum_rotation_redesign_search.py的simulate_day原封不動搬過來（direct
import其load_window/run_variant/WINDOWS/COST_SCENARIOS_BPS，只重寫
simulate_day本身）——這是最乾淨的「改一個變數看效果」對照實驗，之後才視
結果決定要不要進一步改出場邏輯（例如「反彈回到trigger價位就出場」這種真正
針對均值回歸設計的停利，而非沿用趨勢跟隨的移動停利）。

跟原本一樣：對4個完全獨立窗口跑、套用5~29bps文件記錄的合理成本區間評分。
"""

from __future__ import annotations

import sys

import numpy as np

sys.path.insert(0, "scripts/research")
from momentum_rotation_redesign_search import (  # noqa: E402
    COST_SCENARIOS_BPS,
    WINDOWS,
    load_window,
)


def simulate_day_fade(
    stock_day_data: dict,
    *,
    breakout_pct: float,
    trail_pct: float,
    vol_confirm_mult: float,
    rearm_pct: float,
    min_overshoot_pct: float,
    max_overshoot_pct: float,
    min_vol_ratio: float,
    preempt_mult: float,
    use_preemption: bool,
) -> list[dict]:
    """跟simulate_day一模一樣，唯一差異：candidate建立時direction整個反過來
    ——price_hits_long（衝過長邊trigger）改進short（賭拉回），price_hits_short
    改進long（賭反彈）。overshoot/vol_ratio的定義（相對於「被突破的那個trigger」
    量測衝過頭的幅度）維持不變，分數越高代表原始訊號越極端、fade的邏輯上也是
    「越極端越該賭均值回歸」，跟momentum版「overshoot越大代表追價越嚴重」是
    同一個數字、兩種相反的解讀。
    """
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
                # 2026-08-13：同步套用多agent研究workflow抓到的第二個fill-price
                # bug修正（見momentum_breakout_strategy.py::simulate_portfolio_day
                # 同一處的修正說明）——exit_price改用觸發停利那筆真實tick價p，
                # 不是理論停損價stop，避免這支fade版本重蹈同一種零滑價假設。
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

        if position is not None and not use_preemption:
            continue

        if not armed[sid]:
            if st["rearm_lo"] <= p <= st["rearm_hi"]:
                armed[sid] = True
            continue

        price_hits_long = p >= st["long_trigger"]
        price_hits_short = p <= st["short_trigger"]
        if not (price_hits_long or price_hits_short) or v < vol_confirm_mult * baseline:
            continue
        breakout_dir = "long" if price_hits_long else "short"
        direction = "short" if breakout_dir == "long" else "long"  # <-- 唯一差異：反方向
        trigger = st["long_trigger"] if breakout_dir == "long" else st["short_trigger"]
        overshoot = abs(p - trigger) / st["open"] * 100.0
        vol_ratio = v / baseline
        if overshoot < min_overshoot_pct or overshoot > max_overshoot_pct or vol_ratio < min_vol_ratio:
            continue
        score = overshoot * vol_ratio
        fill = float(p)
        candidate = {
            "sid": sid, "direction": direction, "fill": fill, "entry": fill,
            "entry_time": t, "entry_score": score, "peak_trough": fill,
            "overshoot": overshoot, "vol_ratio": vol_ratio,
        }

        if position is None:
            position = candidate
            armed[sid] = False
        elif use_preemption and score >= preempt_mult * position["entry_score"]:
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
    return trades


def run_variant(name: str, windows_data: dict, **kwargs) -> dict:
    all_trades = []
    total_days = 0
    for _wname, (all_by_stock, all_days) in windows_data.items():
        total_days += len(all_days)
        for d in all_days:
            day_data = {sid: days[d] for sid, days in all_by_stock.items() if d in days}
            if len(day_data) < 3:
                continue
            all_trades.extend(simulate_day_fade(day_data, **kwargs))
    rets = np.array([t["ret_pct"] for t in all_trades]) if all_trades else np.array([])
    if len(rets) == 0:
        print(f"{name:40s}: 無交易")
        return {"name": name, "n_trades": 0}
    gross = rets.sum() / total_days
    win = float(np.mean(rets > 0) * 100)
    net_lines = [f"{c}bps={(rets - c / 100.0).sum() / total_days:+.2f}%" for c in COST_SCENARIOS_BPS]
    breakeven = (rets.sum() / len(rets)) * 100
    print(f"{name:40s}: n={len(rets):4d} 筆/天={len(rets) / total_days:5.2f} "
          f"勝率={win:5.1f}% gross日均={gross:+7.3f}% 損平={breakeven:5.1f}bps std={rets.std():.3f}%  " + " ".join(net_lines))
    return {"name": name, "n_trades": len(rets), "win_rate": win, "gross_day_mean": gross,
            "breakeven_bps": breakeven, "std": float(rets.std())}


def main() -> None:
    print("載入4窗口資料...")
    windows_data = {wname: load_window(wdate) for wname, wdate in WINDOWS.items()}
    print()

    base = dict(breakout_pct=0.5, trail_pct=1.0, vol_confirm_mult=1.5, rearm_pct=0.25,
                min_overshoot_pct=0.15, max_overshoot_pct=999.0, min_vol_ratio=1.5,
                preempt_mult=2.0, use_preemption=True)

    print("=== 對照組：momentum原方向現行規格（今天已知數字，重印方便對照）===")
    print("  baseline(原方向)                      : 勝率≈40.2% gross日均≈+1.67% 損平≈9.8bps")
    print()

    print("=== 假說A：純反方向（跟原規格唯一差異=direction整個反過來）===")
    run_variant("fade_baseline(純反方向)", windows_data, **base)
    print()

    print("=== 假說B：反方向 + 拉高overshoot下限（越極端才賭回歸）===")
    for ov in [0.3, 0.5, 0.8, 1.0, 1.5]:
        run_variant(f"fade+min_overshoot>={ov}%", windows_data, **{**base, "min_overshoot_pct": ov})
    print()

    print("=== 假說C：反方向 + overshoot上限（太極端可能是真突破，不是過熱）===")
    for lo, hi in [(0.15, 1.0), (0.3, 1.0), (0.5, 1.5), (0.5, 2.0)]:
        run_variant(f"fade+overshoot[{lo}%,{hi}%]", windows_data,
                    **{**base, "min_overshoot_pct": lo, "max_overshoot_pct": hi})
    print()

    print("=== 假說D：反方向 + 縮小移動停利（回歸行情通常較快/較小）===")
    for tp in [0.3, 0.5, 0.75, 1.5]:
        run_variant(f"fade+trail_pct={tp}%", windows_data, **{**base, "trail_pct": tp})
    print()

    print("=== 假說E：反方向 + 關掉搶佔 ===")
    run_variant("fade+no_preemption", windows_data, **{**base, "use_preemption": False})
    print()

    print("=== 假說F：反方向組合拳（縮小停利+overshoot範圍）===")
    for ov, hi, tp in [(0.3, 1.0, 0.5), (0.5, 1.5, 0.5), (0.5, 1.0, 0.75), (0.8, 2.0, 0.5)]:
        run_variant(f"fade+ov[{ov},{hi}]+trail{tp}", windows_data,
                    **{**base, "min_overshoot_pct": ov, "max_overshoot_pct": hi, "trail_pct": tp})


if __name__ == "__main__":
    main()
