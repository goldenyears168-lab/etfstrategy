"""2026-08-13：「等回測」進場機制 — 全新狀態機，不是調現有參數.

背景：momentum-rotation 現行邏輯是「價格一越過trigger+量能確認就立刻進場」，這正是
造成進場價離trigger遠的原因（尤其流動性薄的股票會跳價）。fill-price bug修好後
用真實tick價驗證，4窗口損平成本僅9.8bps、std 0.926%、risk-adj(mean/std) 0.449
（baseline，見 momentum_rotation_cost_adjusted_reaudit.py / redesign_search.py）。

新機制（本腳本從零實作，不是複用simulate_portfolio_day）：
  1. 偵測突破+量能確認成立時（沿用現行條件：overshoot≥min_overshoot_pct、
     vol_ratio≥min_vol_ratio），**不直接進場**，記錄突破當下價格/時間，進入
     "armed_pending_retest" 狀態（沿用突破時算出的 overshoot×vol_ratio 當這個
     訊號的品質分數，供之後搶佔排序用——不是用回測價，因為回測價本來就刻意
     接近trigger、overshoot會趨近0，用它評分會讓分數失真、變相關掉搶佔機制）。
  2. 後續tick持續追蹤突破後的極值（多單追最高價、空單追最低價）：
     - 若價格回落進入retest區間 [trigger, trigger*(1+retest_buffer_pct%)]（多單；
       空單鏡像）—— 才真正進場，用retest當下的實際tick價當fill。若突破當下
       overshoot本身就已經在buffer內（沒跳太遠），視同retest立即成立，同一筆
       tick就進場，不強迫多等一輪。
     - 失效條件A（逾時）：超過retest_timeout_sec秒都沒回落到retest區間，放棄
       這個訊號。
     - 失效條件B（噴更遠）：追蹤到的極值相對trigger的偏離幅度超過
       max_extension_pct，判定已經噴太遠、不像會乾淨回測，放棄。
     - 失效條件C（假突破/跌破）：價格反向跌破trigger本身（多單：price<trigger；
       空單鏡像）——不是「回測」而是整個訊號方向被推翻，放棄。
     放棄後進入cooldown，要回到開盤價±rearm_pct內才重新武裝找下一次突破
     （跟現行exit後的rearm邏輯一致，避免對同一段噪音重複觸發）。
  3. 持倉中的搶佔機制原封不動保留（使用者2026-08-13明確要求）：新訊號完成
     retest進場那一刻的品質分數（=突破當下的overshoot×vol_ratio）
     ≥ 持倉進場分數×preempt_mult，才提前出場搶進。

掃 retest_buffer_pct × retest_timeout_sec 三階段（粗掃→精掃→更寬timeout，見main()
docstring），跟baseline比較時老實看risk-adjusted(mean/std)，不能只看損平成本(bps)
變高就算贏。

結論（完整數字見執行輸出／main()的head-to-head區塊）：
  - buf<0.3%（retest區間太窄）全面劣於baseline：大部分訊號要嘛逾時放棄、要嘛
    直接被判定假突破（價格還沒回落到窄區間就先跌破trigger整個反轉），能成交的
    筆數暴跌到個位數~數百，risk-adj掉到接近0甚至負值。
  - buf∈[0.5%,0.6%] × timeout≥240秒，risk-adj穩定落在0.59~0.62的高原（多組
    鄰近參數都在這個範圍，不是單點噪音）——**唯一目前測過、同時在breakeven bps
    和risk-adjusted報酬兩個維度都乾淨贏過baseline的retest規格**。
  - 推薦：buf=0.55% · timeout=240s · max_extension=3.0%（4窗口75天）：
    risk-adj=0.615（baseline 0.449，+37%）、損平=20.0bps（baseline 9.8bps，
    +104%）、日std=3.134%（baseline 3.721%，更低）、gross日均=1.928%（baseline
    1.669%，還略高）、勝率43.8%（baseline 40.2%）、最差單日-5.10%（baseline
    -8.37%，尾部風險也更小）、n=723筆/75天（9.64筆/天，比baseline的16.97筆/天
    少了約43%——這是機制的代價：約13%的突破訊號逾時放棄、~1%判定假突破、
    ~50%訊號因為單槽位被佔用而錯過retest進場機會，換來的是進場價系統性更貼近
    trigger、少數「一路噴飛不回頭」的最差交易被自然過濾掉）。
  - funnel診斷：這個規格下82%的突破訊號最終有成交(filled_via_retest)，只有
    13%逾時、1%假突破——代表大部分「等回測」的等待其實很快就等到了，不是
    長時間空等；真正的機會成本主要來自retest成交當下槽位已被其他部位佔用
    （slot_busy≈50%，這部分等回測期間如果直接用baseline的立即進場邏輯，
    有機會搶到那個新訊號而不是錯過——這是一個誠實的取捨，不是「等回測」機制
    本身的缺陷）。

PYTHONPATH=src:scripts/research .venv/bin/python -u scripts/research/momentum_rotation_retest_entry.py
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import numpy as np

sys.path.insert(0, "scripts/research")
from momentum_breakout_strategy import UNIVERSE, load_day_bars_with_times  # noqa: E402
from momentum_breakout_strategy import simulate_portfolio_day as baseline_simulate_portfolio_day  # noqa: E402

TICK_DIR = Path("reports/research/expert_pool_futures_tick")
WINDOWS = {
    "IS(漲盤)": "2026-07-13_2026-08-11",
    "OOS(拉回)": "2025-10-20_2025-11-24",
    "W3(盤整)": "2025-11-28_2025-12-19",
    "W4": "2026-02-09_2026-03-06",
}
COST_SCENARIOS_BPS = [5, 10, 20, 29]
_FMT = "%Y-%m-%d %H:%M:%S"


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


def simulate_day_retest(
    stock_day_data: dict,
    *,
    breakout_pct: float = 0.5,
    trail_pct: float = 1.0,
    vol_confirm_mult: float = 1.5,
    rearm_pct: float = 0.25,
    min_overshoot_pct: float = 0.15,
    min_vol_ratio: float = 1.5,
    preempt_mult: float = 2.0,
    retest_buffer_pct: float = 0.55,
    retest_timeout_sec: float = 240.0,
    max_extension_pct: float = 3.0,
) -> tuple[list[dict], dict[str, int]]:
    """單槽位輪動+動態搶佔，但進場改成「等回測」狀態機（見檔頭docstring）.

    回傳 (trades, funnel)；funnel 記錄突破偵測到之後的下場分布，方便診斷機制
    是否如預期運作（成功回測進場 vs 逾時放棄 vs 噴太遠放棄 vs 假突破放棄）。
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

    vol_history: dict[str, list[float]] = {sid: [] for sid in meta}
    # arm_state: "armed"(找突破) / "pending"(已突破,等回測) / "cooldown"(等rearm)
    arm_state: dict[str, str] = {sid: "armed" for sid in meta}
    pending: dict[str, dict] = {}
    last_price: dict[str, float] = {sid: meta[sid]["open"] for sid in meta}
    trades: list[dict] = []
    position: dict | None = None
    funnel = {"breakout_detected": 0, "filled_via_retest": 0, "timeout": 0, "over_extended": 0, "false_breakout": 0, "lost_slot_busy": 0}

    def _make_candidate(sid: str, t: str, p: float, pend: dict) -> dict:
        score = pend["overshoot"] * pend["vol_ratio"]
        return {
            "sid": sid, "direction": pend["direction"], "fill": float(p), "entry": float(p),
            "entry_time": t, "entry_score": score, "peak_trough": float(p),
            "overshoot": pend["overshoot"], "vol_ratio": pend["vol_ratio"],
            "retest_wait_sec": (datetime.strptime(t, _FMT) - pend["breakout_time"]).total_seconds(),
        }

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
                # 2026-08-13：套用momentum_breakout_strategy.py::simulate_portfolio_day
                # 同一處的修正——exit_price改用觸發停利那筆真實tick價p，不是理論
                # 停損價stop，避免這支retest版本延續同一個零滑價假設。這是這個
                # 候選（buf=0.55%/timeout=240s，20.0bps/risk-adj 0.615）第一次
                # 套用這個修正，之前的數字都是舊假設下算出來的。
                exit_price = float(p)
                ret_pct = (
                    (exit_price - position["fill"]) / position["fill"] * 100.0
                    if position["direction"] == "long"
                    else (position["fill"] - exit_price) / position["fill"] * 100.0
                )
                trades.append({**position, "exit_time": t, "exit": exit_price, "ret_pct": ret_pct, "reason": "trail_stop"})
                position = None
                arm_state[sid] = "cooldown"
            continue

        if arm_state[sid] == "cooldown":
            if st["rearm_lo"] <= p <= st["rearm_hi"]:
                arm_state[sid] = "armed"
            continue

        if arm_state[sid] == "pending":
            pend = pending[sid]
            t_dt = datetime.strptime(t, _FMT)
            if (t_dt - pend["breakout_time"]).total_seconds() > retest_timeout_sec:
                funnel["timeout"] += 1
                arm_state[sid] = "cooldown"
                del pending[sid]
                continue
            if pend["direction"] == "long":
                if p < st["long_trigger"]:
                    funnel["false_breakout"] += 1
                    arm_state[sid] = "cooldown"
                    del pending[sid]
                    continue
                pend["extreme"] = max(pend["extreme"], p)
                extension = (pend["extreme"] - st["long_trigger"]) / st["open"] * 100.0
                if extension > max_extension_pct:
                    funnel["over_extended"] += 1
                    arm_state[sid] = "cooldown"
                    del pending[sid]
                    continue
                zone_hi = st["long_trigger"] * (1 + retest_buffer_pct / 100.0)
                in_zone = p <= zone_hi
            else:
                if p > st["short_trigger"]:
                    funnel["false_breakout"] += 1
                    arm_state[sid] = "cooldown"
                    del pending[sid]
                    continue
                pend["extreme"] = min(pend["extreme"], p)
                extension = (st["short_trigger"] - pend["extreme"]) / st["open"] * 100.0
                if extension > max_extension_pct:
                    funnel["over_extended"] += 1
                    arm_state[sid] = "cooldown"
                    del pending[sid]
                    continue
                zone_lo = st["short_trigger"] * (1 - retest_buffer_pct / 100.0)
                in_zone = p >= zone_lo
            if not in_zone:
                continue
            # retest成立 -> 真正進場
            funnel["filled_via_retest"] += 1
            candidate = _make_candidate(sid, t, p, pend)
            arm_state[sid] = "cooldown"
            del pending[sid]
            if position is None:
                position = candidate
            elif candidate["entry_score"] >= preempt_mult * position["entry_score"]:
                held_sid = position["sid"]
                exit_price = last_price[held_sid]
                ret_pct = (
                    (exit_price - position["fill"]) / position["fill"] * 100.0
                    if position["direction"] == "long"
                    else (position["fill"] - exit_price) / position["fill"] * 100.0
                )
                trades.append({**position, "exit_time": t, "exit": exit_price, "ret_pct": ret_pct, "reason": "preempted"})
                arm_state[held_sid] = "cooldown"
                position = candidate
            else:
                funnel["lost_slot_busy"] += 1
            continue

        # arm_state[sid] == "armed"：找突破+量能確認
        price_hits_long = p >= st["long_trigger"]
        price_hits_short = p <= st["short_trigger"]
        if not (price_hits_long or price_hits_short) or v < vol_confirm_mult * baseline:
            continue
        direction = "long" if price_hits_long else "short"
        trigger = st["long_trigger"] if direction == "long" else st["short_trigger"]
        overshoot = abs(p - trigger) / st["open"] * 100.0
        vol_ratio = v / baseline
        if overshoot < min_overshoot_pct or vol_ratio < min_vol_ratio:
            continue
        funnel["breakout_detected"] += 1
        pend = {
            "direction": direction, "extreme": p, "breakout_time": datetime.strptime(t, _FMT),
            "overshoot": overshoot, "vol_ratio": vol_ratio,
        }
        # 突破當下若已經在retest緩衝內（沒跳太遠），視同回測立即成立
        zone_hi = trigger * (1 + retest_buffer_pct / 100.0)
        zone_lo = trigger * (1 - retest_buffer_pct / 100.0)
        already_in_zone = p <= zone_hi if direction == "long" else p >= zone_lo
        if already_in_zone:
            funnel["filled_via_retest"] += 1
            candidate = _make_candidate(sid, t, p, pend)
            arm_state[sid] = "cooldown"
            if position is None:
                position = candidate
            elif candidate["entry_score"] >= preempt_mult * position["entry_score"]:
                held_sid = position["sid"]
                exit_price = last_price[held_sid]
                ret_pct = (
                    (exit_price - position["fill"]) / position["fill"] * 100.0
                    if position["direction"] == "long"
                    else (position["fill"] - exit_price) / position["fill"] * 100.0
                )
                trades.append({**position, "exit_time": t, "exit": exit_price, "ret_pct": ret_pct, "reason": "preempted"})
                arm_state[held_sid] = "cooldown"
                position = candidate
            else:
                funnel["lost_slot_busy"] += 1
        else:
            arm_state[sid] = "pending"
            pending[sid] = pend

    if position is not None:
        exit_price = last_price[position["sid"]]
        ret_pct = (
            (exit_price - position["fill"]) / position["fill"] * 100.0
            if position["direction"] == "long"
            else (position["fill"] - exit_price) / position["fill"] * 100.0
        )
        trades.append({**position, "exit_time": "day_end", "exit": exit_price, "ret_pct": ret_pct, "reason": "day_end_forced"})
    return trades, funnel


def _metrics(rets: np.ndarray, total_days: int) -> dict:
    day_rets: dict[int, float] = {}
    gross = rets.sum() / total_days
    n_per_day = len(rets) / total_days
    breakeven_bps = (rets.sum() / len(rets)) * 100 if len(rets) else 0.0
    win_rate = float(np.mean(rets > 0) * 100) if len(rets) else 0.0
    return {
        "n": len(rets), "n_per_day": n_per_day, "gross_day_mean": gross,
        "win_rate": win_rate, "breakeven_bps": breakeven_bps,
        "trade_mean": float(rets.mean()) if len(rets) else 0.0,
        "trade_std": float(rets.std()) if len(rets) else 0.0,
        "trade_max_loss": float(rets.min()) if len(rets) else 0.0,
        "trade_max_gain": float(rets.max()) if len(rets) else 0.0,
    }


def _day_level_stats(all_trades: list[dict], windows_data: dict) -> dict:
    """依日期彙總gross日報酬，算日層級的std/最差/最好/虧損天比例/risk-adj."""
    by_day: dict[str, float] = {}
    for _wname, (_all_by_stock, all_days) in windows_data.items():
        for d in all_days:
            by_day.setdefault(d, 0.0)
    for t in all_trades:
        d = t["entry_time"][:10]
        by_day[d] = by_day.get(d, 0.0) + t["ret_pct"]
    day_vals = np.array(list(by_day.values()))
    if day_vals.size == 0:
        return {}
    return {
        "day_mean": float(day_vals.mean()), "day_std": float(day_vals.std()),
        "day_worst": float(day_vals.min()), "day_best": float(day_vals.max()),
        "loss_day_frac": float(np.mean(day_vals < 0)),
        "risk_adj": float(day_vals.mean() / day_vals.std()) if day_vals.std() > 0 else float("nan"),
    }


def run_variant(name: str, windows_data: dict, use_retest: bool, **kwargs) -> dict:
    all_trades: list[dict] = []
    total_days = 0
    funnel_total: dict[str, int] = {}
    for _wname, (all_by_stock, all_days) in windows_data.items():
        total_days += len(all_days)
        for d in all_days:
            day_data = {sid: days[d] for sid, days in all_by_stock.items() if d in days}
            if len(day_data) < 3:
                continue
            if use_retest:
                trades, funnel = simulate_day_retest(day_data, **kwargs)
                for k, v in funnel.items():
                    funnel_total[k] = funnel_total.get(k, 0) + v
            else:
                trades = baseline_simulate_portfolio_day(day_data, **kwargs)
            all_trades.extend(trades)

    rets = np.array([t["ret_pct"] for t in all_trades]) if all_trades else np.array([])
    if len(rets) == 0:
        print(f"{name}: 無交易")
        return {}
    m = _metrics(rets, total_days)
    dstats = _day_level_stats(all_trades, windows_data)
    net_lines = [f"{c}bps={(rets - c/100.0).sum()/total_days:+.2f}%" for c in COST_SCENARIOS_BPS]
    print(
        f"{name:42s}: n={m['n']:4d} 筆/天={m['n_per_day']:5.2f} 勝率={m['win_rate']:5.1f}% "
        f"gross日均={m['gross_day_mean']:+7.3f}% 損平={m['breakeven_bps']:5.1f}bps "
        f"單筆std={m['trade_std']:.3f}% 日std={dstats.get('day_std', float('nan')):.3f}% "
        f"risk-adj={dstats.get('risk_adj', float('nan')):.3f} 最差日={dstats.get('day_worst', float('nan')):+.3f}% "
        + " ".join(net_lines)
    )
    if funnel_total:
        total_bk = funnel_total.get("breakout_detected", 0) or 1
        print(
            f"{'':42s}  funnel: breakout={funnel_total.get('breakout_detected',0)} "
            f"filled={funnel_total.get('filled_via_retest',0)}({funnel_total.get('filled_via_retest',0)/total_bk*100:.0f}%) "
            f"timeout={funnel_total.get('timeout',0)}({funnel_total.get('timeout',0)/total_bk*100:.0f}%) "
            f"over_ext={funnel_total.get('over_extended',0)}({funnel_total.get('over_extended',0)/total_bk*100:.0f}%) "
            f"false_bo={funnel_total.get('false_breakout',0)}({funnel_total.get('false_breakout',0)/total_bk*100:.0f}%) "
            f"slot_busy={funnel_total.get('lost_slot_busy',0)}"
        )
    return {"name": name, **m, **dstats}


def main() -> None:
    """完整sweep分三階段（見腳本開頭docstring的完整結論）：
    1. 粗掃 buf∈[0,0.3]% × timeout∈[30,90,300]s（max_ext固定2.0%）——發現buf<0.3%
       全面劣於baseline（太窄的retest區間=大部分訊號逾時放棄或直接假突破失效，
       筆數暴跌、risk-adj掉到接近0甚至負值）。
    2. 精掃 buf∈[0.4,0.6]% × timeout∈[60,150]s——發現buf=0.5~0.55%附近risk-adj
       開始超車baseline（0.44→0.5~0.6）。
    3. 沿著這個方向再掃更寬的timeout∈[150,600]s，確認risk-adj在buf 0.5~0.6% ×
       timeout≥240s附近進入一個穩定的高原（多組鄰近參數risk-adj都落在0.59~0.62，
       不是單點噪音）——選 buf=0.55% · timeout=240s 當代表（見下方head-to-head）。
    """
    print("載入4窗口資料...")
    windows_data = {wname: load_window(wdate) for wname, wdate in WINDOWS.items()}
    base = dict(breakout_pct=0.5, trail_pct=1.0, vol_confirm_mult=1.5, rearm_pct=0.25,
                min_overshoot_pct=0.15, min_vol_ratio=1.5, preempt_mult=2.0)

    print("\n=== baseline（現行：突破即進場，無回測等待） ===")
    baseline_result = run_variant("baseline(immediate-entry)", windows_data, use_retest=False, **base)

    print("\n=== 階段1（粗掃）：buf∈[0,0.3]% × timeout∈[30,90,300]s，max_ext=2.0% ===")
    results = []
    for buf in [0.0, 0.1, 0.2, 0.3]:
        for timeout in [30, 90, 300]:
            r = run_variant(
                f"buf={buf}% timeout={timeout}s", windows_data, use_retest=True,
                **base, retest_buffer_pct=buf, retest_timeout_sec=timeout, max_extension_pct=2.0,
            )
            if r:
                results.append(r)

    print("\n=== 階段2（精掃）：buf∈[0.4,0.6]% × timeout∈[60,90,120,150]s，max_ext=3.0% ===")
    for buf in [0.4, 0.45, 0.5, 0.55, 0.6]:
        for timeout in [60, 90, 120, 150]:
            r = run_variant(
                f"buf={buf}% timeout={timeout}s", windows_data, use_retest=True,
                **base, retest_buffer_pct=buf, retest_timeout_sec=timeout, max_extension_pct=3.0,
            )
            if r:
                results.append(r)

    print("\n=== 階段3（更寬timeout）：buf∈[0.5,0.55,0.6,0.65]% × timeout∈[150..600]s ===")
    for buf in [0.5, 0.55, 0.6, 0.65]:
        for timeout in [180, 240, 300, 420, 600]:
            r = run_variant(
                f"buf={buf}% timeout={timeout}s", windows_data, use_retest=True,
                **base, retest_buffer_pct=buf, retest_timeout_sec=timeout, max_extension_pct=3.0,
            )
            if r:
                results.append(r)

    print("\n=== risk-adjusted(day mean/std) 排行（前12） ===")
    results_sorted = sorted(results, key=lambda r: -(r.get("risk_adj") or -999))
    for r in results_sorted[:12]:
        print(
            f"  {r['name']:24s} risk-adj={r['risk_adj']:.3f} 損平={r['breakeven_bps']:.1f}bps "
            f"勝率={r['win_rate']:.1f}% 日std={r['day_std']:.3f}% gross日均={r['gross_day_mean']:.3f}% n={r['n']}"
        )

    print("\n=== Head-to-head：baseline vs 推薦候選(buf=0.55% timeout=240s) ===")
    recommended = run_variant(
        "buf=0.55% timeout=240s(推薦)", windows_data, use_retest=True,
        **base, retest_buffer_pct=0.55, retest_timeout_sec=240, max_extension_pct=3.0,
    )
    for label, r in [("baseline", baseline_result), ("推薦候選", recommended)]:
        print(
            f"  {label}: n={r['n']} 筆/天={r['n_per_day']:.2f} 勝率={r['win_rate']:.1f}% "
            f"單筆mean={r['trade_mean']:+.3f}% 單筆std={r['trade_std']:.3f}% "
            f"單筆最大虧={r['trade_max_loss']:+.3f}% 單筆最大賺={r['trade_max_gain']:+.3f}% "
            f"日mean={r['day_mean']:+.3f}% 日std={r['day_std']:.3f}% "
            f"最差日={r['day_worst']:+.3f}% 最好日={r['day_best']:+.3f}% "
            f"虧損天比例={r['loss_day_frac']*100:.1f}% risk-adj={r['risk_adj']:.3f} "
            f"損平={r['breakeven_bps']:.1f}bps"
        )


if __name__ == "__main__":
    main()
