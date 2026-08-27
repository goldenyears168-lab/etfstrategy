#!/usr/bin/env python3
"""擴大訊號偵測窗口：T0候選只看T0當天分點買超觸發，改成T0或T0-1任一天觸發就
納入候選——使用者觀察「今天很多漲停板」，想知道被FGAP_MAX=9%排除的極端跳空
候選，如果隔一天再進場會不會比較能交易（不是延後進場日，是放寬『哪天算觸發』
的偵測窗口，進場日仍固定T0+1、fgap仍用T0收盤算）。

方法：重寫一份 build_candidates() 的研究版，today_events 查詢從『只查 as_of』
改成『查 as_of 或 as_of的前一個交易日』，任一天有合格買超事件就納入——其餘
（60日建倉排除、高沖席門檻、ADV流動性、期貨代碼驗證）完全比照正式版，決策
基準點固定在 as_of（T0），不因為觸發日是T0-1而改變60日窗口或進場日。

跟現行(僅T0觸發)比較：候選數是否增加、增加的候選(僅T0-1觸發)最終走
floor7%/cap9%/blend排序後表現如何、walk-forward+bootstrap。

PYTHONPATH=src:scripts/research .venv/bin/python scripts/research/dayflip_short_2day_signal_window.py
"""

from __future__ import annotations

import csv
import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from statistics import mean

import numpy as np

import stock_db

ROOT = Path(__file__).resolve().parents[2]
SHORT_TRADELOG_CSV = ROOT / "reports/research/branch-footprint-screen/dayflip_gapup_short/single_pick_tradelog.csv"
FUT_CACHE_PATH = ROOT / "reports/research/branch-footprint-screen/dayflip_gapup_short/futures_daily_cache.json"
SPEC_PATH = ROOT / "reports/research/branch-footprint-screen/dayflip_gapup_short/FROZEN_SPEC_V1.json"
UNIVERSE_PATH = ROOT / "reports/research/branch-footprint-screen/dayflip_gapup_short/stock_futures_universe.json"
MEGA_PATH = ROOT / "reports/research/branch-footprint-screen/ab58_xMega_copytrade/mega_blacklist_v1.json"

MIN_BUY_NTD = 30_000_000.0
ADV_MIN_LOTS = 800.0
ACC_WINDOW_DAYS = 60
ACC_NET_RATIO_MAX = 0.30
ACC_MIN_WINDOW_BUY_NTD = 100_000_000.0
HIGH_FLIP_MIN = 0.40
EXTRA_MANUAL_PAIRS = {("9217", "2308"), ("9217", "3653")}

FGAP_FLOOR = 7.0
FGAP_CAP = 9.0
GAP_RANK_WEIGHT = 0.75
COVER_TARGET_PCT = 0.02
ROUND_TRIP_COST_PCT = 0.05
N_BOOTSTRAP = 3000


@dataclass(frozen=True)
class Candidate2:
    stock_id: str
    n_seats: int
    trigger_days: tuple[str, ...]  # 哪些天觸發了買超條件("T0"和/或"T0-1")


def build_candidates_window(as_of: str, con: sqlite3.Connection, *, two_day: bool) -> list[Candidate2]:
    spec = json.loads(SPEC_PATH.read_text())
    static_flip: dict[str, float] = dict(spec["seat_flip_table_frozen"]["values"])
    manual = {tuple(x) for x in spec["signal"]["step2_seat_filters"]["manual_pair_exclusion"]}
    manual |= EXTRA_MANUAL_PAIRS
    mega = set(json.loads(MEGA_PATH.read_text())["symbols"])
    futmap = json.loads(UNIVERSE_PATH.read_text())["map"]

    lo = (date.fromisoformat(as_of) - timedelta(days=260)).isoformat()
    px: dict[tuple[str, str], float] = {}
    for sid, d, c in con.execute(
        "SELECT stock_id,trade_date,close FROM stock_daily_bars "
        "WHERE source='finmind' AND trade_date BETWEEN ? AND ? AND close>0",
        (lo, as_of),
    ):
        px[(str(sid), str(d))] = float(c)

    cal = sorted({d for (_, d) in px if lo <= d <= as_of})
    if as_of not in cal:
        return []
    ai = cal.index(as_of)
    check_dates = [as_of] if not two_day else ([cal[ai - 1], as_of] if ai >= 1 else [as_of])

    events_by_stock: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for tid in static_flip:
        placeholders = ",".join(["?"] * len(check_dates))
        for d, sid, b, s in con.execute(
            f"SELECT trade_date,stock_id,buy,sell FROM stock_broker_branch_daily "
            f"WHERE securities_trader_id=? AND trade_date IN ({placeholders})",
            (tid, *check_dates),
        ):
            p = px.get((str(sid), str(d)))
            if p is None or not b or float(b) <= 0:
                continue
            amt = float(b) * p
            if amt >= MIN_BUY_NTD:
                events_by_stock[str(sid)][str(d)].append({"tid": tid, "amt": amt})

    br: dict[str, dict[str, dict[str, tuple]]] = defaultdict(lambda: defaultdict(dict))
    for tid in static_flip:
        for d, sid, b, s in con.execute(
            "SELECT trade_date,stock_id,buy,sell FROM stock_broker_branch_daily "
            "WHERE securities_trader_id=? AND trade_date BETWEEN ? AND ?",
            (tid, lo, as_of),
        ):
            br[tid][str(sid)][str(d)] = (float(b or 0), float(s or 0))
    ci = {d: i for i, d in enumerate(cal)}

    def net_ratio(tid: str, sid: str) -> float | None:
        i = ci.get(as_of)
        if i is None or i < ACC_WINDOW_DAYS:
            return None
        tb = ts = 0.0
        for d in cal[i - ACC_WINDOW_DAYS:i]:
            b, s = (br[tid].get(sid) or {}).get(d, (0.0, 0.0))
            p = px.get((sid, d))
            if p is None:
                continue
            tb += b * p
            ts += s * p
        return None if tb < ACC_MIN_WINDOW_BUY_NTD else (tb - ts) / tb

    fut_cache = json.loads(FUT_CACHE_PATH.read_text()) if FUT_CACHE_PATH.exists() else {}

    out: list[Candidate2] = []
    for sid, by_day in events_by_stock.items():
        if sid in mega or sid.startswith("00") or sid not in futmap:
            continue
        all_events = [e for es in by_day.values() for e in es]
        keep = []
        for e in all_events:
            if (e["tid"], sid) in manual:
                continue
            nr = net_ratio(e["tid"], sid)
            if nr is not None and nr >= ACC_NET_RATIO_MAX:
                continue
            keep.append(e)
        if not keep or not any(static_flip.get(e["tid"], 0) >= HIGH_FLIP_MIN for e in keep):
            continue
        m = fut_cache.get(sid) or {}
        ds = sorted(m)
        adv = None
        if as_of in ds:
            i = ds.index(as_of)
            if i >= 20:
                adv = mean([m[x][4] for x in ds[i - 20:i]])
        if adv is None or adv < ADV_MIN_LOTS:
            continue
        trigger_days = tuple(sorted(by_day.keys(), key=lambda d: check_dates.index(d) if d in check_dates else 99))
        out.append(Candidate2(stock_id=sid, n_seats=len({e["tid"] for e in keep}), trigger_days=trigger_days))
    return out


def net_ret_for_entry(entry_px: float, low_px: float, close_px: float) -> float:
    target = entry_px * (1 - COVER_TARGET_PCT)
    exit_px = target if low_px <= target else close_px
    return (entry_px - exit_px) / entry_px * 100 - ROUND_TRIP_COST_PCT


def sharpe_like(rets: list[float]) -> float:
    arr = np.array(rets)
    if len(arr) < 2 or arr.std() == 0:
        return float("nan")
    return float(arr.mean() / arr.std())


def pick_blend(qual: list[dict]) -> dict:
    by_gap = sorted(qual, key=lambda r: r["fgap"])
    gap_rank = {id(r): i + 1 for i, r in enumerate(by_gap)}
    by_seats = sorted(qual, key=lambda r: -r["n_seats"])
    seat_rank = {id(r): i + 1 for i, r in enumerate(by_seats)}
    return min(qual, key=lambda r: GAP_RANK_WEIGHT * gap_rank[id(r)] + (1 - GAP_RANK_WEIGHT) * seat_rank[id(r)])


def main() -> None:
    with SHORT_TRADELOG_CSV.open(encoding="utf-8") as f:
        base_signal_dates = sorted({row["signal_date"] for row in csv.DictReader(f)})
    fut_cache = json.loads(FUT_CACHE_PATH.read_text(encoding="utf-8"))
    con = sqlite3.connect(f"file:{stock_db.DEFAULT_DB_PATH}?mode=ro", uri=True)

    lo_date, hi_date = base_signal_dates[0], base_signal_dates[-1]
    all_dates = sorted({d for (d,) in con.execute(
        "SELECT DISTINCT trade_date FROM stock_daily_bars WHERE source='finmind' AND trade_date BETWEEN ? AND ?",
        (lo_date, hi_date))})
    print(f"掃描範圍 {lo_date} ~ {hi_date}，{len(all_dates)}個交易日（不只74個已知訊號日，"
          f"因為2日窗口可能在原本『T0無候選』的日子也產生候選）\n")

    def eval_day(t0: str, two_day: bool) -> tuple[float | None, list]:
        cands = build_candidates_window(t0, con, two_day=two_day)
        rows = []
        for c in cands:
            m = fut_cache.get(c.stock_id) or {}
            ds = sorted(m)
            if t0 not in ds:
                continue
            idx = ds.index(t0)
            if idx + 1 >= len(ds):
                continue
            t01 = ds[idx + 1]
            row = m.get(t01)
            t0_close_row = m.get(t0)
            if not row or not t0_close_row:
                continue
            t0_close = float(t0_close_row[1])
            if t0_close <= 0:
                continue
            open_px, close_px, _high, low_px = float(row[0]), float(row[1]), float(row[2]), float(row[3])
            if open_px <= 0:
                continue
            fgap = (open_px / t0_close - 1) * 100
            rows.append({"stock_id": c.stock_id, "fgap": fgap, "n_seats": c.n_seats,
                        "open_px": open_px, "close_px": close_px, "low_px": low_px,
                        "trigger_days": c.trigger_days})
        qual = [r for r in rows if FGAP_FLOOR <= r["fgap"] < FGAP_CAP]
        if not qual:
            return None, rows
        best = pick_blend(qual)
        return net_ret_for_entry(best["open_px"], best["low_px"], best["close_px"]), rows

    print("=== 建立候選(這會花幾分鐘，逐日查DB) ===")
    baseline_ret: dict[str, float | None] = {}
    twoday_ret: dict[str, float | None] = {}
    n_extra_signal_days = 0
    n_extra_candidates_from_t0_minus_1 = 0
    for i, t0 in enumerate(all_dates):
        r1, rows1 = eval_day(t0, two_day=False)
        r2, rows2 = eval_day(t0, two_day=True)
        baseline_ret[t0] = r1
        twoday_ret[t0] = r2
        if r1 is None and r2 is not None:
            n_extra_signal_days += 1
        only_t0_minus_1 = [r for r in rows2 if r["trigger_days"] and t0 not in r["trigger_days"]]
        n_extra_candidates_from_t0_minus_1 += len(only_t0_minus_1)
        if (i + 1) % 40 == 0:
            print(f"  進度 {i + 1}/{len(all_dates)}")
    con.close()
    print("完成\n")

    n1 = sum(1 for v in baseline_ret.values() if v is not None)
    n2 = sum(1 for v in twoday_ret.values() if v is not None)
    print(f"現行(僅T0觸發)成交天數: {n1}/{len(all_dates)}")
    print(f"擴大窗口(T0或T0-1觸發)成交天數: {n2}/{len(all_dates)}")
    print(f"因擴大窗口而『從完全沒訊號變有訊號』的天數: {n_extra_signal_days}")
    print(f"總共出現過『只有T0-1觸發、T0當天沒有』的候選-事件次數: {n_extra_candidates_from_t0_minus_1}\n")

    r1_all = [v for v in baseline_ret.values() if v is not None]
    r2_all = [v for v in twoday_ret.values() if v is not None]
    print(f"現行: n={len(r1_all)} sharpe={sharpe_like(r1_all):.3f} mean={np.mean(r1_all):+.3f}%")
    print(f"擴大窗口: n={len(r2_all)} sharpe={sharpe_like(r2_all):.3f} mean={np.mean(r2_all):+.3f}%")

    common_dates = sorted(all_dates)
    print(f"\n=== Block bootstrap（對{len(common_dates)}個交易日重抽{N_BOOTSTRAP}次） ===")
    rng = np.random.default_rng(20260810)
    diffs, wins = [], 0
    for _ in range(N_BOOTSTRAP):
        sample = rng.choice(common_dates, size=len(common_dates), replace=True)
        r1 = [baseline_ret[t0] for t0 in sample if baseline_ret.get(t0) is not None]
        r2 = [twoday_ret[t0] for t0 in sample if twoday_ret.get(t0) is not None]
        if len(r1) < 5 or len(r2) < 5:
            continue
        s1, s2 = sharpe_like(r1), sharpe_like(r2)
        if np.isnan(s1) or np.isnan(s2):
            continue
        diffs.append(s2 - s1)
        if s2 > s1:
            wins += 1
    diffs = np.array(diffs)
    print(f"擴大窗口贏現行比例: {wins/len(diffs)*100:.1f}% diff mean={diffs.mean():+.3f} "
          f"5th={np.percentile(diffs,5):+.3f} 95th={np.percentile(diffs,95):+.3f}")


if __name__ == "__main__":
    main()
