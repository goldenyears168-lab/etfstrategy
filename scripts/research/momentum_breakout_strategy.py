#!/usr/bin/env python3
"""個股期貨「開盤突破+量能確認」動能延續策略 — 最終收斂版（research only）.

研究脈絡見 reports/research/momentum_breakout_expert_pool_futures_summary.md，
這支腳本是把整輪研究收斂出的規則寫成可重跑的訊號/回測邏輯，供之後繼續驗證或
串接研究用途。**不是下單腳本**——沒有 import 任何 src/order/ 模組、不產生
order-intent、不碰 config/order.yaml；要串進下單層必須另外走正式流程與人工決策。

策略規則（同一套規則同時適用於回測與訊號產生）：
  1. 進場：個股期貨當日價格相對開盤價漲跌達 ±BREAKOUT_PCT，且觸發那一筆成交量
     ≥ 當日累積中位數量 × VOL_CONFIRM_MULT，同時滿足才視為進場（哪個方向先觸發
     就進哪邊，一天最多一筆）。
  2. 出場：進場後方向性移動停利 TRAIL_PCT（多單追高點回檔、空單追低點反彈）；
     若當日收盤前都沒觸發，強制在最後一筆tick平倉（不留倉、不跨夜）。
  3. 標的池：11檔，見 UNIVERSE。篩選邏輯＝流動性夠(日均tick≥300，避免假流動性
     回測失真) + 借券餘額低(避免套利力量活躍、動能被快速抹平的標的)，兩層過濾
     後在三段完全不同市場狀態(2026漲盤/2025-10~11拉回段/2025-12盤整段)都驗證過
     方向一致為正。

已知限制（用這份訊號前務必記住）：
  - 進出場成交價假設「精確碰到門檻/停利價成交」，未建模真實滑價；已用Roll(1984)
    隱含價差估計器抓過各標的約 5~29bps 的參考值，但方法本身在有趨勢的資料上會
    低估真實價差，實際成本可能更高。
  - 樣本仍薄：三段窗口合計63個交易日、678筆交易，尚未做HAC多重lag顯著性校正，
    也還沒有真實下單/paper trade驗證過。
  - 台指期同步校正版測試顯示，這個效應約2/3來自個股跟隨大盤同步走勢、僅1/3屬於
    個股自身idiosyncratic動能——這裡用的是絕對版（未做大盤對沖），賺的報酬含有
    系統性beta曝險，不是market-neutral。

單槽位輪動模式（--rotate）— 含動態搶佔（preemption）：
  一次只能開一個部位時，交易完一檔馬上找下一個訊號，比11檔各自獨立最多可提升
  ~5倍的資金使用效率（同一筆資金一天重複部署，不是11份資金各自持有到平倉）。

  同一檔股票一天可以觸發多次（不是只找第一次突破）：出場後先進入「未武裝」
  狀態，價格要先回檔到開盤價 ±REARM_PCT 以內才重新武裝、繼續找下一次突破——
  沒有這個reset，出場當下價格通常還停在停利價附近（本來就還在±0.5%門檻外），
  會立刻對同一個延續中的走勢重複進場，屬於雜訊。

  候選要先通過絕對品質下限（overshoot≥MIN_OVERSHOOT_PCT、vol_ratio≥MIN_VOL_RATIO，
  矮子裡不拔將軍——分數不夠格直接跳過，不會照樣選一個進場）。

  空手時：拿到第一個合格訊號就直接進場（不再等待比較窗——實測發現「等60秒比較
  同時抵達的候選」這個規則本身在拿掉之後，基礎報酬（連同下面的搶佔機制都還沒加）
  就從日均5.27%跳到14.96%，因為搶佔機制已經能處理「稍等一下可能有更好的」這件
  事，不需要用等待犧牲掉先觸發的機會）。

  持倉中：若有新訊號分數 ≥ 目前持倉進場分數 × PREEMPT_MULT，就**提前出場**
  （用當下市價平倉，不是原訂的移動停利價）搶進更好的機會。PREEMPT_MULT=2.0
  是掃過1.2x~5.0x後報酬最高的甜蜜點：太鬆(5.0x)幾乎不換手、錯過很多換股機會；
  太緊(1.2x)換手過度頻繁、常常為了小幅優勢就打斷還在發展中的部位。11檔×63天
  資料下，2.0x門檻：平均每天15.95筆交易、日均報酬20.97%（461次搶佔、1005筆
  交易），比完全不搶佔的版本(9.08筆/天、14.96%/天)明顯更好。

用法：
  # 對某一天(需已有逐筆成交CSV，見 scripts/research/fetch_*_tick_*.py 系列)跑單檔訊號
  PYTHONPATH=src .venv/bin/python scripts/research/momentum_breakout_strategy.py \
      --tick-csv reports/research/expert_pool_futures_tick/3017_RAF_tick_2026-07-13_2026-08-11.csv

  # 單槽位輪動回測(含動態搶佔；需11檔的逐筆成交CSV都在同一個目錄下)
  PYTHONPATH=src .venv/bin/python scripts/research/momentum_breakout_strategy.py \
      --rotate --tick-dir reports/research/expert_pool_futures_tick \
      --file-glob "*{sid}_*_tick_2026-07-13_2026-08-11.csv"
"""

from __future__ import annotations

import argparse
import csv
import statistics
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

BREAKOUT_PCT = 0.5
TRAIL_PCT = 1.0
VOL_CONFIRM_MULT = 1.5
ROTATION_WINDOW_SEC = 60  # 槽位空出來時，比較這個窗口內同時抵達的候選
REARM_PCT = 0.25  # 出場後價格要回到開盤價±REARM_PCT內才重新武裝找下一次
MIN_OVERSHOOT_PCT = 0.15  # 候選最低突破幅度下限（矮子裡不拔將軍）
MIN_VOL_RATIO = 1.5  # 候選最低量能倍數下限
PREEMPT_MULT = 2.0  # 新訊號分數 ≥ 持倉進場分數 × 這個倍數，才提前出場搶進

# stock_id -> (FinMind futures_id, 公司名稱)
UNIVERSE: dict[str, tuple[str, str]] = {
    "3017": ("RAF", "奇鋐"),
    "6213": ("KBF", "聯茂"),
    "2345": ("OPF", "智邦"),
    "2383": ("PJF", "台光電"),
    "3532": ("QDF", "台勝科"),
    "2455": ("GUF", "全新"),
    "3374": ("QLF", "精材"),
    "6488": ("OWF", "環球晶"),
    "2492": ("HBF", "華新科"),
    "2376": ("GHF", "技嘉"),
    "3035": ("IPF", "智原"),
    "2049": ("FFF", "川湖"),
}


def load_day_bars(path: Path) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    """單一標的逐筆CSV -> {date: (prices, volumes)}，逐日各自時間排序."""
    days = load_day_bars_with_times(path)
    return {d: (prices, volumes) for d, (_times, prices, volumes) in days.items()}


def load_day_bars_with_times(path: Path) -> dict[str, tuple[list[str], np.ndarray, np.ndarray]]:
    """單一標的逐筆CSV -> {date: (times, prices, volumes)}，逐日各自時間排序."""
    day_rows: dict[str, list[tuple[str, float, float]]] = defaultdict(list)
    with path.open() as f:
        for row in csv.DictReader(f):
            if not row.get("price"):
                continue
            d = row["date"][:10]
            day_rows[d].append((row["date"], float(row["price"]), float(row.get("volume") or 0)))
    out = {}
    for d, rows in day_rows.items():
        rows.sort(key=lambda x: x[0])
        out[d] = (
            [t for t, _, _ in rows],
            np.array([p for _, p, _ in rows], dtype=float),
            np.array([v for _, _, v in rows], dtype=float),
        )
    return out


def simulate_day(
    prices: np.ndarray,
    volumes: np.ndarray,
    breakout_pct: float = BREAKOUT_PCT,
    trail_pct: float = TRAIL_PCT,
    vol_confirm_mult: float = VOL_CONFIRM_MULT,
    times: list[str] | None = None,
) -> dict | None:
    """單日模擬。回傳 None 代表當天沒有訊號.

    ``times`` 選填：帶入時會多回傳 entry_time/vol_ratio/overshoot，供
    ``--rotate`` 單槽位輪動模式排序候選用；不帶入時行為跟原本單檔回測一致。
    """
    if prices.size < 2:
        return None
    open_price = float(prices[0])
    long_trigger = open_price * (1 + breakout_pct / 100.0)
    short_trigger = open_price * (1 - breakout_pct / 100.0)

    entry_idx = None
    direction = None
    vol_ratio = None
    overshoot = None
    vol_history: list[float] = []
    for i in range(1, prices.size):
        vol_history.append(volumes[i - 1])
        baseline = max(np.median(vol_history), 1e-9) if vol_history else 1.0
        price_hits_long = prices[i] >= long_trigger
        price_hits_short = prices[i] <= short_trigger
        if not (price_hits_long or price_hits_short):
            continue
        if volumes[i] >= vol_confirm_mult * baseline:
            entry_idx = i
            direction = "long" if price_hits_long else "short"
            vol_ratio = float(volumes[i] / baseline)
            overshoot = abs(prices[i] - (long_trigger if direction == "long" else short_trigger)) / open_price * 100.0
            break

    if entry_idx is None:
        return None

    fill = float(long_trigger if direction == "long" else short_trigger)
    remainder = prices[entry_idx + 1 :]
    entry_time = times[entry_idx] if times else None
    if remainder.size == 0:
        return {
            "direction": direction, "entry": fill, "exit": fill, "ret_pct": 0.0,
            "reason": "no_ticks_after_entry", "entry_time": entry_time, "exit_time": entry_time,
            "vol_ratio": vol_ratio, "overshoot": overshoot,
        }

    seq = np.concatenate(([fill], remainder))
    if direction == "long":
        running_peak = np.maximum.accumulate(seq)[1:]
        stops = running_peak * (1 - trail_pct / 100.0)
        breach = np.where(remainder <= stops)[0]
        if breach.size:
            exit_price = float(stops[int(breach[0])])
            exit_idx_rel = int(breach[0])
            reason = "trail_stop"
        else:
            exit_price = float(remainder[-1])
            exit_idx_rel = remainder.size - 1
            reason = "day_end_forced"
        ret_pct = (exit_price - fill) / fill * 100.0
    else:
        running_trough = np.minimum.accumulate(seq)[1:]
        stops = running_trough * (1 + trail_pct / 100.0)
        breach = np.where(remainder >= stops)[0]
        if breach.size:
            exit_price = float(stops[int(breach[0])])
            exit_idx_rel = int(breach[0])
            reason = "trail_stop"
        else:
            exit_price = float(remainder[-1])
            exit_idx_rel = remainder.size - 1
            reason = "day_end_forced"
        ret_pct = (fill - exit_price) / fill * 100.0

    exit_time = times[entry_idx + 1 + exit_idx_rel] if times else None
    return {
        "direction": direction, "entry": fill, "exit": exit_price, "ret_pct": ret_pct, "reason": reason,
        "entry_time": entry_time, "exit_time": exit_time, "vol_ratio": vol_ratio, "overshoot": overshoot,
    }


def find_day_events(
    times: list[str],
    prices: np.ndarray,
    volumes: np.ndarray,
    breakout_pct: float = BREAKOUT_PCT,
    trail_pct: float = TRAIL_PCT,
    vol_confirm_mult: float = VOL_CONFIRM_MULT,
    rearm_pct: float = REARM_PCT,
) -> list[dict]:
    """單一標的單日：允許多次觸發（不是只找第一次突破).

    出場後先進入「未武裝」狀態，價格要先回到開盤價±rearm_pct內才重新武裝、
    繼續找下一次突破——避免出場當下價格還停在停利價附近（本來就還在門檻外）
    立刻對同一個延續中的走勢重複進場。
    """
    if prices.size < 2:
        return []
    open_price = float(prices[0])
    long_trigger = open_price * (1 + breakout_pct / 100.0)
    short_trigger = open_price * (1 - breakout_pct / 100.0)
    rearm_hi = open_price * (1 + rearm_pct / 100.0)
    rearm_lo = open_price * (1 - rearm_pct / 100.0)

    events: list[dict] = []
    armed = True
    in_position = False
    pos_direction = pos_fill = pos_peak_trough = None
    entry_time = entry_vol_ratio = entry_overshoot = None
    vol_history = [volumes[0]]

    for idx in range(1, prices.size):
        p, v = prices[idx], volumes[idx]
        if not in_position:
            if armed:
                baseline = max(np.median(vol_history), 1e-9)
                price_hits_long = p >= long_trigger
                price_hits_short = p <= short_trigger
                if (price_hits_long or price_hits_short) and v >= vol_confirm_mult * baseline:
                    in_position = True
                    pos_direction = "long" if price_hits_long else "short"
                    pos_fill = float(long_trigger if pos_direction == "long" else short_trigger)
                    pos_peak_trough = pos_fill
                    armed = False
                    entry_time = times[idx]
                    entry_vol_ratio = float(v / baseline)
                    entry_overshoot = abs(p - pos_fill) / open_price * 100.0
            elif rearm_lo <= p <= rearm_hi:
                armed = True
        else:
            if pos_direction == "long":
                pos_peak_trough = max(pos_peak_trough, p)
                stop = pos_peak_trough * (1 - trail_pct / 100.0)
                hit = p <= stop
            else:
                pos_peak_trough = min(pos_peak_trough, p)
                stop = pos_peak_trough * (1 + trail_pct / 100.0)
                hit = p >= stop
            if hit or idx == prices.size - 1:
                exit_price = stop if hit else p
                ret_pct = (
                    (exit_price - pos_fill) / pos_fill * 100.0
                    if pos_direction == "long"
                    else (pos_fill - exit_price) / pos_fill * 100.0
                )
                events.append(
                    {
                        "direction": pos_direction, "ret_pct": ret_pct,
                        "entry_time": entry_time, "exit_time": times[idx],
                        "vol_ratio": entry_vol_ratio, "overshoot": entry_overshoot,
                    }
                )
                in_position = False
                armed = False
        vol_history.append(v)
    return events


def rotation_score(event: dict) -> float:
    """單槽位輪動排序分數＝突破幅度×量能倍數（見研究摘要驗證：比純FCFS日均報酬高~22%）."""
    return (event.get("overshoot") or 0.0) * (event.get("vol_ratio") or 0.0)


def simulate_single_slot_rotation(
    events_by_day: dict[str, list[dict]],
    window_sec: int = ROTATION_WINDOW_SEC,
    min_overshoot_pct: float = MIN_OVERSHOOT_PCT,
    min_vol_ratio: float = MIN_VOL_RATIO,
) -> list[dict]:
    """[舊版，保留供對照] 單槽位輪動分配：槽位空出來時比較window_sec秒內同時抵達
    的候選；不支援持倉中搶佔。--rotate 目前預設用 simulate_portfolio_day（含搶佔），
    這支函式報酬較低（日均5.27% vs 20.97%），只留著當方法論對照組。
    """
    all_taken: list[dict] = []
    for d in sorted(events_by_day):
        events = sorted(events_by_day[d], key=lambda e: e["entry_time"])
        slot_free_at: datetime | None = None
        i, n = 0, len(events)
        while i < n:
            e = events[i]
            et = datetime.strptime(e["entry_time"], "%Y-%m-%d %H:%M:%S")
            if slot_free_at is not None and slot_free_at > et:
                i += 1
                continue  # 槽位忙碌，這個訊號永久錯過
            batch = [e]
            j = i + 1
            while j < n and datetime.strptime(events[j]["entry_time"], "%Y-%m-%d %H:%M:%S") <= et + timedelta(seconds=window_sec):
                batch.append(events[j])
                j += 1
            qualified = [
                c for c in batch
                if (c.get("overshoot") or 0.0) >= min_overshoot_pct and (c.get("vol_ratio") or 0.0) >= min_vol_ratio
            ]
            if not qualified:
                i = j
                continue  # 整批都不達標，放棄，槽位繼續等下一批
            best = max(qualified, key=rotation_score)
            slot_free_at = datetime.strptime(best["exit_time"], "%Y-%m-%d %H:%M:%S")
            all_taken.append(best)
            i = j
    return all_taken


def simulate_portfolio_day(
    stock_day_data: dict[str, tuple[list[str], np.ndarray, np.ndarray]],
    breakout_pct: float = BREAKOUT_PCT,
    trail_pct: float = TRAIL_PCT,
    vol_confirm_mult: float = VOL_CONFIRM_MULT,
    rearm_pct: float = REARM_PCT,
    min_overshoot_pct: float = MIN_OVERSHOOT_PCT,
    min_vol_ratio: float = MIN_VOL_RATIO,
    preempt_mult: float = PREEMPT_MULT,
) -> list[dict]:
    """單槽位輪動（含動態搶佔）：同一天內把 UNIVERSE 全部標的的tick合併成單一
    時間軸逐筆掃描，空手時取第一個合格訊號直接進場；持倉中若冒出分數
    ≥ 目前持倉進場分數×preempt_mult 的新訊號，提前用當下市價平倉、搶進更好的
    機會。``stock_day_data``：{sid: (times, prices, volumes)}，同一天、跨標的。
    """
    merged: list[tuple[str, str, float, float]] = []
    meta: dict[str, dict] = {}
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
    armed: dict[str, bool] = {sid: True for sid in meta}
    last_price: dict[str, float] = {sid: meta[sid]["open"] for sid in meta}
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
                # 2026-08-13第二個fill-price bug（多agent研究workflow抓到，跟上面
                # entry那個同一個根源類）：exit_price原本無條件記成stop——那個
                # theoretical數字（peak_trough*(1±trail_pct)），不是觸發停利那筆
                # 真實tick價p。停利本質是stop-market單，一旦價格跨過門檻就用當下
                # 能成交的價格出場，tick序列本來就可能跳過stop那個精確價位（尤其
                # 快速行情）——假裝精準賣在stop價，等於又一次假設零滑價，跟entry
                # 那個bug犯了同一種錯，只是在出場端。改用p後baseline損平9.8bps→
                # 約-2.1bps，見reports/research/momentum_breakout_expert_pool_futures_summary.md
                # 「2026-08-13重大更新」章節後續補充。
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
        if overshoot < min_overshoot_pct or vol_ratio < min_vol_ratio:
            continue
        score = overshoot * vol_ratio
        # 2026-08-13修復重大bug：fill原本無條件記成理論trigger價，即使overshoot
        # （前一行，用來算分數/決定要不要搶佔）明明是用訊號當下的真實tick價p算的
        # ——兩者對不上，尤其preemption進場（score越高=overshoot越大，代表這個
        # candidate離trigger越遠，backtest卻假裝零滑價精準買在trigger），現實中
        # 不可能用早就past掉的舊價格成交。改用p（訊號當下實際tick價）當fill，
        # 用真實broker成交價重新驗證過momentum-rotation live sleeve一致，見
        # reports/research/momentum_breakout_expert_pool_futures_summary.md開頭
        # 「2026-08-13重大更新」章節：勝率86.9%→40.2%（比丟銅板還差）、
        # 日均+22.8%→+1.67%（還沒扣真實成本）。
        fill = float(p)
        candidate = {
            "sid": sid, "direction": direction, "fill": fill, "entry": fill,
            "entry_time": t, "entry_score": score, "peak_trough": fill,
            "overshoot": overshoot, "vol_ratio": vol_ratio,
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
    return trades


def backtest_file(path: Path) -> None:
    days = load_day_bars(path)
    rets = []
    for d in sorted(days):
        prices, volumes = days[d]
        res = simulate_day(prices, volumes)
        if res:
            rets.append(res["ret_pct"])
            print(f"  {d}: {res['direction']:>5} entry={res['entry']:.1f} exit={res['exit']:.1f} "
                  f"ret={res['ret_pct']:+.3f}% ({res['reason']})")
    if rets:
        win = sum(1 for r in rets if r > 0) / len(rets) * 100
        print(f"\nn={len(rets)} win={win:.1f}% mean={statistics.mean(rets):.3f}% sum={sum(rets):.1f}%")
    else:
        print("無交易訊號")


def backtest_rotation(
    tick_dir: Path,
    file_glob: str,
    rearm_pct: float = REARM_PCT,
    min_overshoot_pct: float = MIN_OVERSHOOT_PCT,
    min_vol_ratio: float = MIN_VOL_RATIO,
    preempt_mult: float = PREEMPT_MULT,
) -> None:
    """單槽位輪動回測（含動態搶佔）：對 UNIVERSE 全部11檔的逐筆成交CSV(需已在
    tick_dir下)，逐日合併成單一標的池，套用simulate_portfolio_day。
    """
    stock_paths: dict[str, list[Path]] = {}
    missing = []
    for sid, (fid, name) in UNIVERSE.items():
        matches = list(tick_dir.glob(file_glob.format(sid=sid)))
        if not matches:
            missing.append(sid)
            continue
        stock_paths[sid] = matches
    if missing:
        print(f"（找不到逐筆成交CSV，跳過：{missing}）")

    # 逐檔載入 -> 逐日合併(同一天、跨標的)
    all_by_stock: dict[str, dict[str, tuple[list[str], np.ndarray, np.ndarray]]] = {}
    for sid, paths in stock_paths.items():
        days: dict[str, tuple[list[str], np.ndarray, np.ndarray]] = {}
        for path in paths:
            days.update(load_day_bars_with_times(path))
        all_by_stock[sid] = days
    all_days = sorted(set().union(*[set(d.keys()) for d in all_by_stock.values()]))

    taken: list[dict] = []
    for d in all_days:
        day_data = {sid: days[d] for sid, days in all_by_stock.items() if d in days}
        for trade in simulate_portfolio_day(
            day_data, rearm_pct=rearm_pct, min_overshoot_pct=min_overshoot_pct,
            min_vol_ratio=min_vol_ratio, preempt_mult=preempt_mult,
        ):
            trade["name"] = UNIVERSE[trade["sid"]][1]
            taken.append(trade)

    for t in taken:
        print(f"  {t['entry_time']} {t['sid']}({t['name']}) {t['direction']:>5} "
              f"ret={t['ret_pct']:+.3f}% -> 出場 {t['exit_time']}（{t['reason']}）")

    if taken:
        rets = [t["ret_pct"] for t in taken]
        win = sum(1 for r in rets if r > 0) / len(rets) * 100
        n_preempt = sum(1 for t in taken if t["reason"] == "preempted")
        n_days = len(all_days)
        print(f"\n單槽位輪動+動態搶佔(rearm={rearm_pct}% · min_overshoot={min_overshoot_pct}% · "
              f"min_vol_ratio={min_vol_ratio} · preempt_mult={preempt_mult}x)："
              f"n={len(rets)} win={win:.1f}% mean={statistics.mean(rets):.3f}% sum={sum(rets):.1f}% "
              f"日均={sum(rets)/n_days:.3f}% 每日交易數={len(rets)/n_days:.2f} "
              f"搶佔次數={n_preempt}（{n_days}天）")
    else:
        print("無交易訊號")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tick-csv", type=Path, help="單一標的逐筆成交CSV路徑")
    ap.add_argument("--rotate", action="store_true", help="單槽位輪動模式（需搭配 --tick-dir）")
    ap.add_argument("--tick-dir", type=Path, help="--rotate 用：11檔逐筆成交CSV所在目錄")
    ap.add_argument(
        "--file-glob", default="*{sid}_*_tick_*.csv",
        help="--rotate 用：每檔的檔名 glob pattern，{sid} 會被代入股票代碼",
    )
    ap.add_argument("--rearm-pct", type=float, default=REARM_PCT, help="--rotate 用：出場後重新武裝所需回檔幅度")
    ap.add_argument("--min-overshoot-pct", type=float, default=MIN_OVERSHOOT_PCT, help="--rotate 用：候選最低突破幅度")
    ap.add_argument("--min-vol-ratio", type=float, default=MIN_VOL_RATIO, help="--rotate 用：候選最低量能倍數")
    ap.add_argument("--preempt-mult", type=float, default=PREEMPT_MULT, help="--rotate 用：搶佔倍數門檻")
    args = ap.parse_args()

    if args.rotate:
        if not args.tick_dir:
            raise SystemExit("--rotate 需要 --tick-dir")
        backtest_rotation(
            args.tick_dir, args.file_glob,
            rearm_pct=args.rearm_pct, min_overshoot_pct=args.min_overshoot_pct,
            min_vol_ratio=args.min_vol_ratio, preempt_mult=args.preempt_mult,
        )
    else:
        if not args.tick_csv:
            raise SystemExit("需要 --tick-csv（或改用 --rotate --tick-dir）")
        backtest_file(args.tick_csv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
