#!/usr/bin/env python3
"""dayflip-short post-dump 做多——PROACTIVE(事前) 資金上限規則，取代 margin call 事後強砍.

背景（見同目錄兩支既有腳本）：
  - `dayflip_short_post_dump_long_capital_simulation.py`：逐日資金/保證金排程模擬
    baseline（無任何風控），曾得出「約2,000,000 NTD是甜蜜點（Sharpe~2.6）」的初步結論——
    但那一輪用 0050 當市場基準的 proxy 訊號，且完全沒有風控機制。
  - `dayflip_short_margin_call_capital_simulation.py`：加上 REACTIVE(事後) margin call——
    NAV 回檔超過門檻(15/20/25/30%)才強制砍最爛部位。結果是回馬槍：強砍會在移動停利
    5%這個均值回歸機制本來要生效之前，先把浮虧鎖死成實現虧損；而且砍倉騰出的保證金，
    讓排程器在同一段壞行情裡吃進更多相關風險的新單，某些情境下最大回檔反而比不做
    margin call更差（例：2M NTD/15%門檻：-35.6% vs baseline -29.9%）。

本輪任務：改用 PROACTIVE（事前限制曝險，不靠事後砍倉）規則：
  (a) 同時在場口數硬上限（5/10/15/20/30口）——不管保證金理論上還夠不夠，直接擋新單。
  (b) 資金保留緩衝——只把總資金的一部分(50/60/70/80%)當可動用保證金池，其餘完全不碰、
      不進新單也不被強砍，純粹留在帳上撐 NAV。

進場訊號改用真台指期(TX)1分K基準（不再用0050 proxy）：滾動15分鐘窗、rolling_lag
(個股報酬-TX報酬)最負且低於-0.3%門檻的那個時間點，之後10分鐘內回升到超過該負值一半
即為訊號分鐘，用該分鐘個股收盤價進場——沿用
`dayflip_short_tx_real_rolling_dip_signal_v2.py` 已驗證過的訊號重建邏輯（15分鐘/0.3%/
10分鐘為任務固定死的參數，這裡不重掃）。出場沿用移動停利5%/最長10日，
`futures_daily_cache.json` 日頻期貨收盤。

逐日資金/保證金排程模擬的主迴圈結構沿用
`dayflip_short_post_dump_long_capital_simulation.py`（出場釋放保證金→進場搶保證金
→逐日mark-to-market算NAV→從NAV序列算max drawdown/Sharpe/總報酬，不是簡化cumsum）。

跑法：
  1) baseline（無任何風控，等同資金模擬那支的邏輯，但改用TX-real訊號）：1M/2M/3M/5M NTD。
  2) REACTIVE margin call 復刻（用TX-real訊號重跑，門檻15/20%）：跟(1)同資金規模，
     拿來跟本輪的PROACTIVE規則做真正蘋果對蘋果比較（原本兩支參照腳本是0050 proxy訊號，
     這裡統一換成TX-real，數字本身會跟背景描述的舊結論有落差，屬預期中的訊號差異）。
  3) PROACTIVE (a) 同時在場口數上限：cap∈{5,10,15,20,30}，資金規模分別在2M（貼近甜蜜點）
     與5M（資金夠寬鬆，讓cap而非保證金變成唯一綁住的約束，驗證「不管資金多少都有效」）
     兩個規模下各跑一輪。
  4) PROACTIVE (b) 資金保留緩衝：fraction∈{50%,60%,70%,80%}使用率 × 資金規模∈{1M,2M,3M}。
  5) 綜合：(a)+(b) 疊加的候選規則，直接對照baseline/reactive margin call。

PYTHONPATH=src .venv/bin/python scripts/research/dayflip_short_proactive_capital_sizing.py
"""

from __future__ import annotations

import csv
import json
import os
import sqlite3
from pathlib import Path

import numpy as np

import stock_db
from stock_db.kbar import load_kbar_day_bars
from trial_registry import append_trial

ROOT = Path(__file__).resolve().parents[2]
TRADES_CSV = ROOT / "reports/research/branch-footprint-screen/dayflip_gapup_short/all_trades.csv"
FUT_CACHE_PATH = ROOT / "reports/research/branch-footprint-screen/dayflip_gapup_short/futures_daily_cache.json"
DATA_DIR = Path(os.environ.get("GOLDENSTOCKS_DATA_DIR", str(Path.home() / "goldenstocks-data")))
TX_BARS_DB = DATA_DIR / "cache" / "tmf_channel" / "bars.sqlite"
TX_SOURCE = "tx_1m_tick_built_582d"

ROUND_TRIP_COST_PCT = 0.05
TRAIL_PCT = 5.0
MAX_HOLD_DAYS = 10
MARGIN_RATE = 0.135
LOT_SHARES = 2000
ROLLING_WINDOW_MIN = 15
CONFIRM_MINUTES = 10
LAG_THRESHOLD_PCT = 0.3  # 任務固定死的參數，不重掃

CAPITAL_SCENARIOS_NTD = (1_000_000, 2_000_000, 3_000_000, 5_000_000)
MARGIN_CALL_THRESHOLDS_PCT = (15.0, 20.0)
CONCURRENT_CAPS = (5, 10, 15, 20, 30)
CONCURRENT_CAP_CAPITALS_NTD = (2_000_000, 5_000_000)
RESERVE_FRACTIONS = (0.5, 0.6, 0.7, 0.8)
RESERVE_CAPITALS_NTD = (1_000_000, 2_000_000, 3_000_000)
MARGIN_CALL_DANGER_ZONE_PCT = 15.0  # 真實券商追繳通常落在維持保證金跌破一定比例，這裡
# 用「NAV回檔逼近/超過此值」當代理，判斷某個規則自身的虧損連跌是否已經逼近追繳區。


# ---------------------------------------------------------------------------
# 訊號：真TX台指期1分K滾動相對弱勢（沿用 v2 已驗證邏輯，門檻固定0.3%）
# ---------------------------------------------------------------------------

def load_trades() -> list[dict]:
    with TRADES_CSV.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_stock_minute_closes(con: sqlite3.Connection, stock_id: str, t01: str) -> dict[str, float]:
    raw = load_kbar_day_bars(con, stock_id, t01)
    return {
        b.minute[:5]: b.close
        for b in raw
        if "09:00" <= b.minute[:5] <= "13:30" and b.close and b.close > 0
    }


def load_tx_minute_closes(tx_con: sqlite3.Connection, t01: str) -> dict[str, float]:
    rows = tx_con.execute(
        "SELECT t, c FROM bars WHERE source=? AND sess='day' AND day=? AND c IS NOT NULL AND c>0",
        (TX_SOURCE, t01),
    ).fetchall()
    return {t: float(c) for t, c in rows}


def _minute_to_int(hhmm: str) -> int:
    h, m = hhmm[:5].split(":")
    return int(h) * 60 + int(m)


def _int_to_minute(v: int) -> str:
    return f"{v // 60:02d}:{v % 60:02d}"


def find_rolling_dip_signal(stock_closes: dict[str, float], tx_closes: dict[str, float]) -> tuple[str, float] | None:
    common = sorted(set(stock_closes) & set(tx_closes), key=_minute_to_int)
    if len(common) < 50:
        return None
    common_set = set(common)

    rolling_lag: dict[str, float] = {}
    for m in common:
        m_int = _minute_to_int(m)
        anchor = _int_to_minute(m_int - ROLLING_WINDOW_MIN)
        if anchor not in common_set:
            continue
        stock_ret = (stock_closes[m] / stock_closes[anchor] - 1) * 100
        tx_ret = (tx_closes[m] / tx_closes[anchor] - 1) * 100
        rolling_lag[m] = stock_ret - tx_ret

    if not rolling_lag:
        return None
    lag_minutes = sorted(rolling_lag, key=_minute_to_int)

    worst_m, worst_val = None, 0.0
    for m in lag_minutes:
        if rolling_lag[m] < -LAG_THRESHOLD_PCT and rolling_lag[m] < worst_val:
            worst_val = rolling_lag[m]
            worst_m = m
    if worst_m is None:
        return None

    worst_int = _minute_to_int(worst_m)
    candidates = [m for m in lag_minutes if 0 < _minute_to_int(m) - worst_int <= CONFIRM_MINUTES]
    for m in candidates:
        if rolling_lag[m] > worst_val * 0.5:
            return m, stock_closes[m]
    return None


def _t01_stock_close(con: sqlite3.Connection, stock_id: str, trade_date: str) -> float | None:
    row = con.execute(
        "SELECT close FROM stock_daily_bars WHERE stock_id=? AND trade_date=? AND source='finmind' AND close>0",
        (stock_id, trade_date),
    ).fetchone()
    return float(row[0]) if row else None


def estimate_margin_ntd(price: float) -> float:
    return price * LOT_SHARES * MARGIN_RATE


def build_calendar(con: sqlite3.Connection, start: str, end: str) -> list[str]:
    rows = con.execute(
        "SELECT DISTINCT trade_date FROM stock_daily_bars "
        "WHERE stock_id='0050' AND source='finmind' AND trade_date BETWEEN ? AND ? ORDER BY trade_date",
        (start, end),
    ).fetchall()
    return [str(r[0]) for r in rows]


# ---------------------------------------------------------------------------
# 逐日資金/保證金排程模擬：baseline / REACTIVE margin call / PROACTIVE 資金規則
# ---------------------------------------------------------------------------

def _position_unrealized_pct(p: dict, close: float) -> float:
    return (close / p["entry_price"] - 1) * 100


def run_simulation(
    signals: list[dict],
    fut_cache: dict,
    calendar: list[str],
    total_capital: float,
    *,
    margin_fraction: float = 1.0,
    max_concurrent: int | None = None,
    margin_call_threshold_pct: float | None = None,
) -> dict:
    """逐日資金/保證金排程模擬.

    - margin_fraction: 只把 total_capital * margin_fraction 當可動用保證金池
      （PROACTIVE規則(b)：資金保留緩衝）；其餘資金完全不進新單。NAV仍以
      total_capital(含未動用緩衝)計算，緩衝本身不因為虧損被強砍。
    - max_concurrent: 同時在場口數硬上限（PROACTIVE規則(a)），None=不限制。
    - margin_call_threshold_pct: 若非None，重現REACTIVE margin call（事後強砍
      未實現損益最負部位），只用來跟PROACTIVE規則做對照，不是本輪的建議方案。
    """
    margin_pool = total_capital * margin_fraction
    signals_by_date: dict[str, list[dict]] = {}
    for s in signals:
        signals_by_date.setdefault(s["trade_date"], []).append(s)
    for d in signals_by_date:
        signals_by_date[d].sort(key=lambda s: -s["n_seats"])

    open_positions: list[dict] = []
    realized_pnl = 0.0
    skipped_for_capital = 0
    skipped_for_concurrent_cap = 0
    taken = 0
    nav_series = []
    utilization_series = []
    max_concurrent_seen = 0
    running_peak = total_capital
    margin_call_days = 0
    forced_liquidations = 0

    for day_idx, day in enumerate(calendar):
        margin_used = sum(p["margin"] for p in open_positions)
        available_margin = margin_pool - margin_used
        utilization_series.append(margin_used / margin_pool * 100 if margin_pool > 0 else 0.0)

        # (1) 一般出場：移動停利 / 最長持有天數
        still_open = []
        for p in open_positions:
            m = fut_cache.get(p["stock"]) or {}
            px = m.get(day)
            if px is None:
                still_open.append(p)
                continue
            close = float(px[1])
            if close <= 0:
                still_open.append(p)
                continue
            p["peak"] = max(p["peak"], close)
            pullback = (p["peak"] - close) / p["peak"] * 100
            hold_days = day_idx - p["entry_day_idx"]
            if pullback >= TRAIL_PCT or hold_days >= MAX_HOLD_DAYS:
                raw_ret = (close / p["entry_price"] - 1) * 100
                net_ret = raw_ret - ROUND_TRIP_COST_PCT
                realized_pnl += p["margin"] / MARGIN_RATE * (net_ret / 100)
                available_margin += p["margin"]
            else:
                still_open.append(p)
        open_positions = still_open

        # (2) 進場：保證金池夠、且(若有設定)同時在場口數未達上限，才進場
        for s in signals_by_date.get(day, []):
            if max_concurrent is not None and len(open_positions) >= max_concurrent:
                skipped_for_concurrent_cap += 1
                continue
            m = fut_cache.get(s["stock"]) or {}
            if day not in m:
                continue
            fut_close = float(m[day][1])
            if fut_close <= 0:
                continue
            entry_price = fut_close * s["entry_frac"]
            margin = estimate_margin_ntd(entry_price)
            if margin <= available_margin:
                open_positions.append({
                    "stock": s["stock"], "entry_price": entry_price, "entry_day_idx": day_idx,
                    "margin": margin, "peak": entry_price,
                })
                available_margin -= margin
                taken += 1
                max_concurrent_seen = max(max_concurrent_seen, len(open_positions))
            else:
                skipped_for_capital += 1

        # (3) mark-to-market NAV（含未動用緩衝，緩衝本身不受風控機制影響）
        def _nav_now() -> float:
            unrealized = 0.0
            for p in open_positions:
                m = fut_cache.get(p["stock"]) or {}
                px = m.get(day)
                if px is None:
                    continue
                close = float(px[1])
                if close <= 0:
                    continue
                notional = p["margin"] / MARGIN_RATE
                unrealized += notional * (close / p["entry_price"] - 1)
            return total_capital + realized_pnl + unrealized

        nav = _nav_now()

        # (4) REACTIVE margin call（僅供對照，非本輪建議方案）
        if margin_call_threshold_pct is not None and running_peak > 0 and open_positions:
            dd_pct = (nav - running_peak) / running_peak * 100
            if dd_pct < -margin_call_threshold_pct:
                margin_call_days += 1
            while dd_pct < -margin_call_threshold_pct and open_positions:
                worst, worst_ret = None, None
                for p in open_positions:
                    m = fut_cache.get(p["stock"]) or {}
                    px = m.get(day)
                    if px is None or float(px[1]) <= 0:
                        continue
                    ret = _position_unrealized_pct(p, float(px[1]))
                    if worst_ret is None or ret < worst_ret:
                        worst_ret, worst = ret, p
                if worst is None:
                    break
                close = float((fut_cache.get(worst["stock"]) or {})[day][1])
                raw_ret = (close / worst["entry_price"] - 1) * 100
                net_ret = raw_ret - ROUND_TRIP_COST_PCT
                realized_pnl += worst["margin"] / MARGIN_RATE * (net_ret / 100)
                open_positions.remove(worst)
                forced_liquidations += 1
                nav = _nav_now()
                dd_pct = (nav - running_peak) / running_peak * 100

        running_peak = max(running_peak, nav)
        nav_series.append(nav)

    nav_arr = np.array(nav_series)
    daily_ret = np.diff(nav_arr) / nav_arr[:-1]
    daily_ret = daily_ret[np.isfinite(daily_ret)]
    peak = np.maximum.accumulate(nav_arr)
    dd = (nav_arr - peak) / peak * 100
    max_dd = float(dd.min()) if len(dd) else 0.0
    total_ret_pct = (nav_arr[-1] / total_capital - 1) * 100 if len(nav_arr) else 0.0
    sharpe_annualized = (
        float(daily_ret.mean() / daily_ret.std() * np.sqrt(252)) if len(daily_ret) > 1 and daily_ret.std() > 0
        else float("nan")
    )
    return {
        "taken": taken,
        "skipped_for_capital": skipped_for_capital,
        "skipped_for_concurrent_cap": skipped_for_concurrent_cap,
        "total_ret_pct": total_ret_pct,
        "max_drawdown_pct": max_dd,
        "sharpe_annualized": sharpe_annualized,
        "avg_capital_utilization_pct": float(np.mean(utilization_series)) if utilization_series else 0.0,
        "max_concurrent_seen": max_concurrent_seen,
        "margin_call_days": margin_call_days,
        "forced_liquidations": forced_liquidations,
        "near_margin_call_zone": max_dd <= -MARGIN_CALL_DANGER_ZONE_PCT,
    }


def _fmt_row(label: str, r: dict) -> str:
    return (
        f"{label:>34} {r['taken']:>6} {r['skipped_for_capital']:>8} {r['skipped_for_concurrent_cap']:>8} "
        f"{r['total_ret_pct']:>10.1f} {r['sharpe_annualized']:>10.3f} {r['max_drawdown_pct']:>10.1f} "
        f"{r['max_concurrent_seen']:>6} {('是' if r['near_margin_call_zone'] else '否'):>6}"
    )


def main() -> None:
    trades = load_trades()
    con = sqlite3.connect(f"file:{stock_db.DEFAULT_DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    tx_con = sqlite3.connect(f"file:{TX_BARS_DB}?mode=ro", uri=True)
    fut_cache = json.loads(FUT_CACHE_PATH.read_text(encoding="utf-8"))

    signals = []
    skipped_no_tx = skipped_no_stock = skipped_no_signal = skipped_no_close = 0
    for t in trades:
        sid, t01 = t["stock"], t["trade_date"]
        stock_closes = load_stock_minute_closes(con, sid, t01)
        tx_closes = load_tx_minute_closes(tx_con, t01)
        if len(tx_closes) < 50:
            skipped_no_tx += 1
            continue
        if len(stock_closes) < 50:
            skipped_no_stock += 1
            continue
        sig = find_rolling_dip_signal(stock_closes, tx_closes)
        if sig is None:
            skipped_no_signal += 1
            continue
        _, sig_price = sig
        day_close = _t01_stock_close(con, sid, t01)
        if day_close is None or day_close <= 0:
            skipped_no_close += 1
            continue
        signals.append({
            "stock": sid, "trade_date": t01, "entry_frac": sig_price / day_close,
            "n_seats": int(t["n_seats"]),
        })

    print("=== PROACTIVE 資金上限規則 vs baseline / REACTIVE margin call（TX-real訊號）===")
    print(
        f"訊號: {len(signals)}/{len(trades)} 筆有效（TX不足跳過={skipped_no_tx}、個股分K不足跳過="
        f"{skipped_no_stock}、無訊號跳過={skipped_no_signal}、無日收盤跳過={skipped_no_close}）\n"
    )
    if not signals:
        raise SystemExit("no signals produced — abort before writing a misleading trial record")

    calendar = build_calendar(con, min(s["trade_date"] for s in signals), "2026-08-09")
    print(f"交易日曆: {calendar[0]} ~ {calendar[-1]}（{len(calendar)}天）")
    n_dates = len({s["trade_date"] for s in signals})
    print(f"訊號涵蓋 {n_dates} 個相異T0+1日期\n")

    header = (
        f"{'情境':>34} {'成交':>6} {'因保證金跳過':>8} {'因口數上限跳過':>8} "
        f"{'總報酬%':>10} {'年化Sharpe':>10} {'最大回檔%':>10} {'最大同時在場口':>6} {'逼近追繳區':>6}"
    )

    # --- (1) baseline：無任何風控 ---
    print("--- (1) Baseline：無任何資金風控機制 ---")
    print(header)
    baseline_results = {}
    for cap in CAPITAL_SCENARIOS_NTD:
        r = run_simulation(signals, fut_cache, calendar, cap)
        baseline_results[cap] = r
        print(_fmt_row(f"baseline {cap:,}", r))
    print()

    # --- (2) REACTIVE margin call 復刻（用TX-real訊號重跑，供對照，非建議方案） ---
    print("--- (2) REACTIVE margin call 復刻（TX-real訊號，供對照，非本輪建議方案）---")
    print(header)
    reactive_results = {}
    for cap in CAPITAL_SCENARIOS_NTD:
        for th in MARGIN_CALL_THRESHOLDS_PCT:
            r = run_simulation(signals, fut_cache, calendar, cap, margin_call_threshold_pct=th)
            reactive_results[(cap, th)] = r
            print(_fmt_row(f"reactive-mc {cap:,}/{th:.0f}%", r))
    print()

    # --- (3) PROACTIVE (a) 同時在場口數硬上限 ---
    print("--- (3) PROACTIVE (a) 同時在場口數硬上限 ---")
    print(header)
    cap_a_results = {}
    for cap in CONCURRENT_CAP_CAPITALS_NTD:
        for n in CONCURRENT_CAPS:
            r = run_simulation(signals, fut_cache, calendar, cap, max_concurrent=n)
            cap_a_results[(cap, n)] = r
            print(_fmt_row(f"cap(a) {cap:,}/{n}口", r))
    print()

    # --- (4) PROACTIVE (b) 資金保留緩衝 ---
    print("--- (4) PROACTIVE (b) 資金保留緩衝（只用X%資金當可動用保證金池）---")
    print(header)
    reserve_b_results = {}
    for cap in RESERVE_CAPITALS_NTD:
        for frac in RESERVE_FRACTIONS:
            r = run_simulation(signals, fut_cache, calendar, cap, margin_fraction=frac)
            reserve_b_results[(cap, frac)] = r
            print(_fmt_row(f"reserve(b) {cap:,}/{frac*100:.0f}%", r))
    print()

    # --- (5) 綜合：(a)+(b) 疊加候選 ---
    print("--- (5) PROACTIVE (a)+(b) 疊加候選規則 ---")
    print(header)
    combo_candidates = [
        (2_000_000, 0.7, 10),
        (2_000_000, 0.7, 15),
        (2_000_000, 0.6, 10),
        (3_000_000, 0.7, 15),
        (3_000_000, 0.6, 10),
    ]
    combo_results = {}
    for cap, frac, n in combo_candidates:
        r = run_simulation(signals, fut_cache, calendar, cap, margin_fraction=frac, max_concurrent=n)
        combo_results[(cap, frac, n)] = r
        print(_fmt_row(f"combo {cap:,}/{frac*100:.0f}%/{n}口", r))
    print()

    # --- 找出「不需要強砍、且回檔沒有逼近15-20%追繳區」的最佳PROACTIVE候選 ---
    all_proactive = []
    for (cap, n), r in cap_a_results.items():
        all_proactive.append((f"cap(a) {cap:,}/{n}口", cap, r))
    for (cap, frac), r in reserve_b_results.items():
        all_proactive.append((f"reserve(b) {cap:,}/{frac*100:.0f}%", cap, r))
    for (cap, frac, n), r in combo_results.items():
        all_proactive.append((f"combo {cap:,}/{frac*100:.0f}%/{n}口", cap, r))

    safe_candidates = [
        (label, cap, r) for label, cap, r in all_proactive
        if not r["near_margin_call_zone"] and np.isfinite(r["sharpe_annualized"]) and r["sharpe_annualized"] > 0
    ]
    safe_candidates.sort(key=lambda x: -x[2]["sharpe_annualized"])

    print("=== 結論 ===")
    if safe_candidates:
        best_label, best_cap, best_r = safe_candidates[0]
        print(
            f"找到不需強砍、且最大回檔未逼近{MARGIN_CALL_DANGER_ZONE_PCT:.0f}%追繳區的候選規則，"
            f"依Sharpe排序最佳者：\n"
            f"  {best_label} → 總報酬{best_r['total_ret_pct']:+.1f}% Sharpe{best_r['sharpe_annualized']:.3f} "
            f"最大回檔{best_r['max_drawdown_pct']:.1f}% 因保證金跳過{best_r['skipped_for_capital']}筆 "
            f"因口數上限跳過{best_r['skipped_for_concurrent_cap']}筆\n"
            f"  前5名安全候選（依Sharpe排序）："
        )
        for label, cap, r in safe_candidates[:5]:
            print(
                f"    {label}: 總報酬{r['total_ret_pct']:+.1f}% Sharpe{r['sharpe_annualized']:.3f} "
                f"最大回檔{r['max_drawdown_pct']:.1f}%"
            )
        recommendation = f"{best_cap:,} NTD 總資金 · {best_label}"
    else:
        best_label, best_cap, best_r = None, None, None
        print(
            f"⚠️ 本輪測試的PROACTIVE候選中，沒有任何一個同時滿足「Sharpe>0 且最大回檔未逼近"
            f"{MARGIN_CALL_DANGER_ZONE_PCT:.0f}%追繳區」——誠實回報：沒有找到乾淨解法，"
            f"見下方完整表格自行判斷取捨（例如接受較低Sharpe換回檔空間，或反之）。"
        )
        recommendation = "none — 無候選同時滿足安全回檔且正Sharpe，見報告誠實說明"

    print(
        "\n⚠️ 限制：\n"
        "  1) 保證金用13.5%概估（跟src/order/dayflip_short_signal.py同一套），非官方逐檔試算表。\n"
        "  2) 多訊號搶資金用n_seats(觸發分點數)排優先序，未另外驗證是否優於FIFO/隨機。\n"
        "  3) 訊號改用真TX台指期1分K基準（tx_1m_tick_built_582d），15分鐘窗/0.3%門檻/10分鐘\n"
        "     確認為任務固定死的參數，未重新sweep；跟兩支參照腳本(0050 proxy或未fix門檻)的\n"
        "     訊號集合不完全相同，數字不能跟舊結論逐字比對，只能看『同一套訊號下不同資金規則'\n"
        "     的相對排序。\n"
        "  4) PROACTIVE規則(a)/(b)都是「事前」限制曝險，完全沒有强制平倉邏輯——因此\n"
        "     max drawdown是唯一能反映「這個規則下帳戶實際會虧到多深」的指標；『逼近追繳區』\n"
        "     用max_drawdown_pct <= -15%當代理，不是真實券商維持保證金率機制。\n"
        "  5) 沒算融資利息/機會成本；保留緩衝(reserve buffer)閒置資金假設不生息也不虧損。\n"
        "  6) 樣本仍是同一組221筆歷史事件、~74個相異T0+1日期，訊號互相在時間上高度重疊\n"
        "     （同一波後could有多檔訊號集中在少數幾天），這是資金規則會「因口數上限/保證金\n"
        "     不足跳過」訊號的根本原因，也是為什麼PROACTIVE規則有意義（相關性風險本來就\n"
        "     集中在少數幾天）。"
    )

    ts = "2026-08-09"
    status = "kept" if safe_candidates else "rejected"
    metric_value = best_r["sharpe_annualized"] if best_r else float("nan")
    n_obs = sum(r["taken"] for _, _, r in all_proactive) if all_proactive else 0
    append_trial(
        "dayflip_short_gapup_short",
        topic_id="proactive-capital-sizing-rule",
        ts=ts,
        params={
            "approach_a_concurrent_caps": list(CONCURRENT_CAPS),
            "approach_a_capitals_ntd": list(CONCURRENT_CAP_CAPITALS_NTD),
            "approach_b_reserve_fractions": list(RESERVE_FRACTIONS),
            "approach_b_capitals_ntd": list(RESERVE_CAPITALS_NTD),
            "combo_candidates": combo_candidates,
            "entry_signal": "rolling_relative_dip_tx_real_0.3pct_15min_10min",
            "trail_pct": TRAIL_PCT,
            "margin_rate": MARGIN_RATE,
            "margin_call_danger_zone_pct": MARGIN_CALL_DANGER_ZONE_PCT,
        },
        n_observations=n_obs,
        metric_name="best_safe_candidate_sharpe" if safe_candidates else "no_safe_candidate_found",
        metric_value=float(metric_value) if np.isfinite(metric_value) else 0.0,
        status=status,
        source=__file__,
        notes=(
            f"用真TX台指期1分K重建的滾動相對弱勢訊號(15min/0.3%/10min)，測試PROACTIVE(事前)"
            f"資金上限規則：(a)同時在場口數硬上限{list(CONCURRENT_CAPS)}於資金"
            f"{list(CONCURRENT_CAP_CAPITALS_NTD)}NTD；(b)資金保留緩衝(可動用比例"
            f"{list(RESERVE_FRACTIONS)})於資金{list(RESERVE_CAPITALS_NTD)}NTD；及(a)+(b)疊加"
            f"候選。跟REACTIVE margin call(事後強砍，已知在此策略上會回馬槍：鎖死均值回歸\n前的浮虧、"
            f"且騰出保證金讓排程器在壞行情追加相關風險)以及無風控baseline三方比較，皆用同一套"
            f"逐日mark-to-market資金/保證金排程模擬(真實max drawdown，非簡化cumsum)。"
            f"{'找到' + str(len(safe_candidates)) + '個滿足Sharpe>0且最大回檔未逼近15%追繳區的PROACTIVE候選，最佳者=' + best_label + f'（總資金{best_cap:,}NTD）' if safe_candidates else '沒有任何PROACTIVE候選同時滿足正Sharpe與回檔安全兩條件——誠實回報未解決margin-call回馬槍問題，需要更保守的規則或接受較低報酬。'}"
            f" 建議規則：{recommendation}。"
        ),
        tags=["dayflip-short", "post-dump", "long-side", "capital-simulation", "proactive-risk-management",
              "tx-real-futures"],
    )
    print("\n(已記入 reports/research/_trial_registry/dayflip_short_gapup_short.jsonl, topic_id=proactive-capital-sizing-rule)")

    con.close()
    tx_con.close()


if __name__ == "__main__":
    main()
