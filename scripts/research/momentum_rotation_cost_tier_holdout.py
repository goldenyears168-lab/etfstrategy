"""2026-08-20：把「扣成本」從全體共用一個 bps，改成每檔用自己的真實成本.

背景：momentum_rotation_broad_universe_pnl_test.py 已驗證 micro VCP（量能收縮 coil
＋3 分鐘趨勢延續）方向是對的——同一份 holdout 上把損平從 baseline 的 −8.5bps 拉到
+10.9bps，關掉濾網則崩成 −17.8bps。但那份分析對**所有標的套用同一個 bps**，而
2026-08-20 的全市場微結構篩選顯示成本跨度極大：小型緯穎 8.4bps ↔ 環球晶 48.7bps
（台股跳動單位在 ≥1000 元封頂 5 元，價格越高相對成本越低）。用單一 bps 等於假裝
一檔 49 元的群創跟一檔 5920 元的小型緯穎付一樣的過路費。

本腳本做三件事：
  1. 每筆交易扣**該檔自己的** tick 成本（跳動單位/價格）。tick 成本是交易所級距表
     決定的**結構性**量，不是從報酬擬合出來的，所以拿它當篩選條件沒有 look-ahead。
  2. 依成本分層，檢查 net 報酬是否隨成本單調改善——這是「成本是真因」的檢定，
     比單看白名單子集的絕對數字更難造假。
  3. 白名單子集自己跑一次（自己的輪動/搶佔動態，不是從全體結果篩出來）。

Roll(1984) 有效價差同時報出來當對照，但**不拿來當篩選**：Roll 是從同一段資料估的，
拿它篩選會有選擇偏誤；tick 成本沒有這個問題。
"""
from __future__ import annotations

import csv
import json
import math
import random
import statistics
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, "scripts/research")
from momentum_rotation_vcp_coil_trend_holdout_cv import simulate_day_coil_trend  # noqa: E402

RANDOM_SEED = 42
MIN_TOTAL_ROWS = 1500
# 刻意**不**沿用 momentum_rotation_broad_universe_coil_trend_test.load_broad_universe：
# 那支有 MAX_TOTAL_ROWS=80_000 上限，註解自承是「避免單一標的拖慢整體 sweep」的效能
# 防護，但它按成交筆數排除，等於靜默砍掉最密集的標的——本次白名單裡成本最低的 DQF
# （群創，16098 tick/日）與 LUF（臻鼎）都因此被擋掉。本腳本只跑一組參數、不做 sweep，
# 不需要這個上限，移除它才不會讓「效能防護」變成隱形的經濟篩選。
ARCHIVE = Path.home() / "goldenstocks-data/cache/momentum_rotation/taifex_tick_daily_broad"
EXCLUDE_CODES = {"TMF"}


def load_broad_universe() -> dict[str, dict[str, tuple[list, np.ndarray, np.ndarray]]]:
    """{code: {date: (times, prices, volumes)}}——注意巢狀順序是 code→date。"""
    universe: dict[str, dict] = {}
    for path in sorted(ARCHIVE.glob("*.csv")):
        code = path.stem
        if code in EXCLUDE_CODES:
            continue
        by_day: dict[str, tuple[list, list, list]] = defaultdict(lambda: ([], [], []))
        total = 0
        with path.open() as f:
            for row in csv.DictReader(f):
                d = (row.get("date") or "")[:10]
                if not d or "/" in (row.get("contract_date") or ""):
                    continue
                try:
                    px = float(row["price"]); vol = float(row["volume"])
                except (KeyError, ValueError, TypeError):
                    continue
                if px <= 0:
                    continue
                t, p, v = by_day[d]
                t.append(row["date"]); p.append(px); v.append(vol)
                total += 1
        if total < MIN_TOTAL_ROWS:
            continue
        srt = {}
        for d, (t, p, v) in by_day.items():
            order = sorted(range(len(t)), key=lambda i: t[i])
            srt[d] = ([t[i] for i in order],
                      np.array([p[i] for i in order], dtype=float),
                      np.array([v[i] for i in order], dtype=float))
        universe[code] = srt
    return universe

BEST_PARAMS = dict(
    trail_pct=1.0, preempt_mult=2.0, window_sec=1.0, cooldown_sec=10.0,
    move_thresh_pct=0.15, vol_mult=2.5, hold_sec=8.0,
    require_coil=True, min_coil_sec=3.0, contraction_ratio=0.4,
    require_trend_align=True, trend_lookback_min=3.0,
)
WHITELIST = ["PWF", "SFF", "DQF", "CKF", "LUF", "RWF", "IRF"]


def tw_tick_size(p: float) -> float:
    if p < 10: return 0.01
    if p < 50: return 0.05
    if p < 100: return 0.1
    if p < 500: return 0.5
    if p < 1000: return 1.0
    return 5.0


def median_price(day_data: dict) -> dict[str, float]:
    acc: dict[str, list[float]] = defaultdict(list)
    for code, by_day in day_data.items():          # universe 是 code -> date -> payload
        for _d, (_t, prices, _v) in by_day.items():
            acc[code].extend(float(p) for p in prices)
    return {c: statistics.median(v) for c, v in acc.items() if v}


def roll_bps(day_data: dict) -> dict[str, float]:
    per: dict[str, list[float]] = defaultdict(list)
    for code, by_day in day_data.items():
        for _d, (_t, prices, _v) in by_day.items():
            ps = [float(p) for p in prices]
            if len(ps) < 30:
                continue
            dp = np.diff(ps)
            if len(dp) < 20:
                continue
            cov = float(np.cov(dp[:-1], dp[1:])[0, 1])
            if cov < 0:
                per[code].append(2 * math.sqrt(-cov))
    med = median_price(day_data)
    return {c: statistics.median(v) / med[c] * 1e4 for c, v in per.items() if c in med}


def run(universe: dict) -> list[dict]:
    by_date: dict[str, dict] = defaultdict(dict)
    for code, days in universe.items():
        for d, payload in days.items():
            by_date[d][code] = payload
    out: list[dict] = []
    for d in sorted(by_date):
        for tr in simulate_day_coil_trend(by_date[d], **BEST_PARAMS):
            tr["date"] = d
            out.append(tr)
    return out


def summarize(name: str, trades: list[dict], cost_bps: dict[str, float], n_days: int) -> dict:
    if not trades:
        print(f"{name}: 無交易")
        return {}
    gross = [t["ret_pct"] for t in trades]
    net = [t["ret_pct"] - cost_bps[t["sid"]] / 100.0 for t in trades]
    per_day_g: dict[str, float] = defaultdict(float)
    per_day_n: dict[str, float] = defaultdict(float)
    for t, g, nv in zip(trades, gross, net):
        per_day_g[t["date"]] += g
        per_day_n[t["date"]] += nv
    dg = [per_day_g.get(d, 0.0) for d in sorted({t["date"] for t in trades})]
    dn = [per_day_n.get(d, 0.0) for d in sorted({t["date"] for t in trades})]
    std = statistics.pstdev(dn) if len(dn) > 1 else float("nan")
    print(f"{name}")
    print(f"  n={len(trades)}  交易日={len(dg)}  筆/天={len(trades)/max(n_days,1):.2f}  "
          f"勝率(gross)={sum(1 for g in gross if g>0)/len(gross)*100:.1f}%  "
          f"勝率(net)={sum(1 for v in net if v>0)/len(net)*100:.1f}%")
    print(f"  每筆 gross={statistics.mean(gross):+.4f}%  每筆 net={statistics.mean(net):+.4f}%  "
          f"平均扣掉成本={statistics.mean([cost_bps[t['sid']] for t in trades]):.1f}bps")
    print(f"  日均 gross={statistics.mean(dg):+.4f}%  日均 net={statistics.mean(dn):+.4f}%  "
          f"日std={std:.3f}%  risk-adj(net)={statistics.mean(dn)/std if std==std and std>0 else float('nan'):+.3f}")
    return dict(n=len(trades), net_trade=statistics.mean(net), net_day=statistics.mean(dn))


def main() -> None:
    print("載入TAIFEX全市場個股期貨archive...")
    universe = load_broad_universe()
    print(f"  {len(universe)}檔通過流動性門檻")
    med = median_price(universe)
    tick_bps = {sid: tw_tick_size(p) / p * 1e4 for sid, p in med.items()}
    rbps = roll_bps(universe)

    codes = sorted(universe)
    rng = random.Random(RANDOM_SEED)
    sh = codes[:]
    rng.shuffle(sh)
    holdout = {c: universe[c] for c in sh[len(sh) // 2:]}
    n_days = len({d for days in universe.values() for d in days})
    print(f"  holdout組{len(holdout)}檔（種子{RANDOM_SEED}，與前次同一切分）· 共{n_days}個交易日\n")

    print("=" * 88)
    print("=== 1. holdout 全體：全體共用 bps  vs  每檔自己的 tick 成本 ===")
    tr = run(holdout)
    for flat in (0.0, 10.9, 20.0):
        summarize(f"[全體·全部套用 {flat:.1f}bps]", tr, {c: flat for c in tick_bps}, n_days)
    summarize("[全體·每檔自己的 tick 成本]", tr, tick_bps, n_days)

    print("\n" + "=" * 88)
    print("=== 2. 成本分層（同一批交易，只是按該檔 tick 成本分組）===")
    print("    如果『成本是真因』成立，net 應該隨成本層級單調變差")
    tiers = [(0, 11), (11, 15), (15, 25), (25, 999)]
    for lo, hi in tiers:
        sub = [t for t in tr if lo <= tick_bps[t["sid"]] < hi]
        n_codes = len({t["sid"] for t in sub})
        summarize(f"[tick成本 {lo}~{hi}bps · {n_codes}檔]", sub, tick_bps, n_days)

    print("\n" + "=" * 88)
    print("=== 3. 白名單子集自己跑（自己的輪動/搶佔動態）===")
    wl = {c: universe[c] for c in WHITELIST if c in universe}
    print(f"    白名單在 archive 內的有 {len(wl)}/{len(WHITELIST)} 檔: {sorted(wl)}")
    for c in sorted(wl):
        print(f"      {c}: 中位價 {med.get(c, float('nan')):.0f}  tick成本 {tick_bps.get(c, float('nan')):.1f}bps  "
              f"Roll {rbps.get(c, float('nan')):.1f}bps")
    tr_wl = run(wl)
    summarize("[白名單·每檔自己的 tick 成本]", tr_wl, tick_bps, n_days)
    summarize("[白名單·改用 Roll 有效價差]", tr_wl, rbps, n_days)

    json.dump({"tick_bps": tick_bps, "roll_bps": rbps},
              open("reports/research/momentum_rotation_cost_tier_holdout.json", "w"))
    print("\n成本表已存 reports/research/momentum_rotation_cost_tier_holdout.json")


if __name__ == "__main__":
    main()
