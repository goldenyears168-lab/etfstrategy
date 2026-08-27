#!/usr/bin/env python3
"""序列式「一次一筆、一天可多次」vs 現行「一天只進場一次」.

使用者問：現行 dayflip_short_order.py 是 covered 之後直接 noop_terminal
（不會再進場），使用者確認這是設計選擇，不是08:45-09:05窗口造成的結構性
限制，問改成「同時最多1口曝險、停利後立刻換下一個候選、一天可循環多次」
會不會更好。

方法：沿用 dayflip_short_post_dump_long_intraday_capital_scheduler.py 已驗證
的 basis 重建技術（個股1分K × 當日期貨/現貨 close-close basis比例，
相關係數0.987-0.994）——只是那支腳本是為長邊做「跨日」資金排程，這裡改成
同一天內「候選#1停利後接著換候選#2」的序列式重建。

限制（誠實揭露，不是可以忽略的細節）：
  1) 期貨08:45開盤、股票09:00才開盤，08:45-09:00這15分鐘沒有分鐘級資料可
     重建——這段視為「持倉中、不检查停利」，可能低估真實停利速度。
  2) basis比例用當日收盤/收盤定值近似，不是逐分鐘真實basis，跟原本驗證
     過的方法論一致但仍是近似值不是真實成交價。
  3) 每多一腿就多收一次5bps成本，序列式的樣本天數比現行少（很多天候選池
     裡只有1檔合格候選，序列式在那些天跟現行完全相同，只有『同一天有≥2檔
     合格候選』的天數才可能出現差異，需要單獨報這個子樣本的天數多寡）。

PYTHONPATH=src:scripts/research .venv/bin/python scripts/research/dayflip_short_sequential_multi_entry.py
"""

from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

import numpy as np

import stock_db

ROOT = Path(__file__).resolve().parents[2]
SHORT_TRADELOG_CSV = ROOT / "reports/research/branch-footprint-screen/dayflip_gapup_short/single_pick_tradelog.csv"
FUT_CACHE_PATH = ROOT / "reports/research/branch-footprint-screen/dayflip_gapup_short/futures_daily_cache.json"
DAY_POOL_CACHE = ROOT / "reports/research/dayflip_fgap_calibration/day_pool_full_74d.json"
COVER_TARGET_PCT = 0.02
ROUND_TRIP_COST_PCT = 0.05
FGAP_FLOOR = 7.0
FORCE_CLOSE_HHMM = "13:40"
N_BOOTSTRAP = 3000


def load_stock_minute_closes(con: sqlite3.Connection, stock_id: str, trade_date: str) -> dict[str, float]:
    from stock_db.kbar import load_kbar_day_bars
    raw = load_kbar_day_bars(con, stock_id, trade_date)
    return {
        b.minute[:5]: b.close
        for b in raw
        if "09:00" <= b.minute[:5] <= FORCE_CLOSE_HHMM and b.close and b.close > 0
    }


def stock_close(con: sqlite3.Connection, stock_id: str, trade_date: str) -> float | None:
    row = con.execute(
        "SELECT close FROM stock_daily_bars WHERE source='finmind' AND stock_id=? AND trade_date=? AND close>0",
        (stock_id, trade_date),
    ).fetchone()
    return float(row[0]) if row else None


def t01_of(fut_cache: dict, stock_id: str, t0: str) -> str | None:
    m = fut_cache.get(stock_id) or {}
    dates = sorted(m)
    if t0 not in dates:
        return None
    i = dates.index(t0)
    return dates[i + 1] if i + 1 < len(dates) else None


def main() -> None:
    with SHORT_TRADELOG_CSV.open(encoding="utf-8") as f:
        signal_dates = sorted({row["signal_date"] for row in csv.DictReader(f)})
    fut_cache = json.loads(FUT_CACHE_PATH.read_text(encoding="utf-8"))
    day_pool = json.loads(DAY_POOL_CACHE.read_text(encoding="utf-8"))

    con = sqlite3.connect(f"file:{stock_db.DEFAULT_DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    print("=== 逐日序列式重建（basis法）===")
    day_results: dict[str, dict] = {}
    n_multi_candidate_days = 0
    for i, t0 in enumerate(signal_dates):
        qual = sorted([r for r in day_pool.get(t0, []) if r["fgap"] >= FGAP_FLOOR], key=lambda r: r["fgap"])
        if not qual:
            day_results[t0] = {"legs": [], "n_qualifying": 0}
            continue
        if len(qual) >= 2:
            n_multi_candidate_days += 1

        t01_by_stock = {r["stock_id"]: t01_of(fut_cache, r["stock_id"], t0) for r in qual}
        basis_by_stock = {}
        minute_px_by_stock = {}
        for r in qual:
            sid = r["stock_id"]
            t01 = t01_by_stock[sid]
            if t01 is None:
                continue
            sclose = stock_close(con, sid, t01)
            if sclose is None or sclose <= 0:
                continue
            basis_by_stock[sid] = r["close_px"] / sclose
            minute_px_by_stock[sid] = load_stock_minute_closes(con, sid, t01)

        legs = []
        current_minute = "09:00"
        for r in qual:
            sid = r["stock_id"]
            if sid not in basis_by_stock:
                continue
            if current_minute >= FORCE_CLOSE_HHMM:
                break
            basis = basis_by_stock[sid]
            mins = minute_px_by_stock[sid]
            if not legs:
                entry_px = r["open_px"]
                entry_minute = "08:45"
            else:
                after = sorted(mm for mm in mins if mm >= current_minute)
                if not after:
                    continue
                entry_minute = after[0]
                entry_px = mins[entry_minute] * basis
            if entry_px <= 0:
                continue
            target = entry_px * (1 - COVER_TARGET_PCT)
            cover_minute, cover_px = None, None
            for mm in sorted(mm for mm in mins if mm >= entry_minute):
                px = mins[mm] * basis
                if px <= target:
                    cover_minute, cover_px = mm, px
                    break
            if cover_minute is None:
                cover_minute, cover_px = FORCE_CLOSE_HHMM, r["close_px"]
            net_ret = (entry_px - cover_px) / entry_px * 100 - ROUND_TRIP_COST_PCT
            legs.append({"stock_id": sid, "entry_minute": entry_minute, "cover_minute": cover_minute,
                         "net_ret_pct": net_ret})
            current_minute = cover_minute
            if cover_minute >= FORCE_CLOSE_HHMM:
                break
        day_results[t0] = {"legs": legs, "n_qualifying": len(qual)}
        if (i + 1) % 20 == 0:
            print(f"  進度 {i + 1}/{len(signal_dates)}")
    con.close()
    print(f"完成。{len(signal_dates)}天中有{n_multi_candidate_days}天同時有≥2檔合格候選"
          f"（只有這些天序列式才可能跟現行不同）\n")

    def current_single_ret(t0: str) -> float | None:
        legs = day_results[t0]["legs"]
        return legs[0]["net_ret_pct"] if legs else None

    def sequential_total_ret(t0: str) -> float | None:
        legs = day_results[t0]["legs"]
        if not legs:
            return None
        return float(sum(leg["net_ret_pct"] for leg in legs))

    def sharpe_like(rets: list[float]) -> float:
        arr = np.array(rets)
        if len(arr) < 2 or arr.std() == 0:
            return float("nan")
        return float(arr.mean() / arr.std())

    r_single = [x for t0 in signal_dates if (x := current_single_ret(t0)) is not None]
    r_seq = [x for t0 in signal_dates if (x := sequential_total_ret(t0)) is not None]
    n_legs = [len(day_results[t0]["legs"]) for t0 in signal_dates if day_results[t0]["legs"]]

    print("=== 全樣本比較 ===")
    print(f"現行(一天一筆):   n={len(r_single)} sharpe={sharpe_like(r_single):.3f} 均pnl={np.mean(r_single):+.3f}%")
    print(f"序列式(一天可多筆): n={len(r_seq)} sharpe={sharpe_like(r_seq):.3f} 均pnl={np.mean(r_seq):+.3f}% "
          f"平均每天{np.mean(n_legs):.2f}腿（最多{max(n_legs)}腿）")

    only_multi_days = [t0 for t0 in signal_dates if day_results[t0]["n_qualifying"] >= 2]
    r_single_m = [x for t0 in only_multi_days if (x := current_single_ret(t0)) is not None]
    r_seq_m = [x for t0 in only_multi_days if (x := sequential_total_ret(t0)) is not None]
    print(f"\n=== 只看『同天≥2檔合格候選』的{len(only_multi_days)}天(唯一可能有差異的子樣本) ===")
    print(f"現行:   n={len(r_single_m)} sharpe={sharpe_like(r_single_m):.3f} 均pnl={np.mean(r_single_m):+.3f}%")
    print(f"序列式: n={len(r_seq_m)} sharpe={sharpe_like(r_seq_m):.3f} 均pnl={np.mean(r_seq_m):+.3f}%")

    print(f"\n=== Block bootstrap（對{len(signal_dates)}個訊號日重抽{N_BOOTSTRAP}次） ===")
    rng = np.random.default_rng(20260810)
    diffs, wins = [], 0
    for _ in range(N_BOOTSTRAP):
        sample = rng.choice(signal_dates, size=len(signal_dates), replace=True)
        rs = [x for t0 in sample if (x := current_single_ret(t0)) is not None]
        rq = [x for t0 in sample if (x := sequential_total_ret(t0)) is not None]
        if len(rs) < 5 or len(rq) < 5:
            continue
        ss, sq = sharpe_like(rs), sharpe_like(rq)
        if np.isnan(ss) or np.isnan(sq):
            continue
        diffs.append(sq - ss)
        if sq > ss:
            wins += 1
    diffs = np.array(diffs)
    print(f"序列式贏現行比例: {wins/len(diffs)*100:.1f}% diff mean={diffs.mean():+.3f} "
          f"5th={np.percentile(diffs,5):+.3f} 95th={np.percentile(diffs,95):+.3f}")


if __name__ == "__main__":
    main()
