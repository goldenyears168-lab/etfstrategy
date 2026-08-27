#!/usr/bin/env python3
"""驗證做多(dayflip-post-dump-long)是否真的需要fgap>=7%前提，還是這是繼承自
all_trades.csv（本身已用fgap≥6/7%篩過）的隱性假設，從沒被獨立驗證過.

背景：使用者問「做多不一定要7%才對，有相對跌就算」——查過現行下單層
（dayflip_post_dump_long_order.py._check_entries()）發現它在呼叫
rolling_relative_dip訊號偵測*之前*，就已經要求 fgap>=FGAP_MIN（跟空單共用
同一個門檻）；再查過全部rolling_relative_dip相關研究腳本
（dayflip_short_rolling_relative_dip_signal.py、
dayflip_short_tx_real_rolling_dip_signal_v1/v2.py），全部都讀
all_trades.csv當候選池——而all_trades.csv本身是run_dayflip_single_pick_
tradelog.py用--fgap-min(預設0.06)篩過的產物。也就是說，rolling_relative_dip
訊號從來沒有在「沒有跳空前提」的候選池上驗證過，這個7%前提是透過共用資料管線
繼承來的，不是獨立測試證實必要。

方法：重用day_pool_full_74d.json（今天稍早為短邊研究建的候選池快取，*不含*
跳空篩選，74個訊號日的完整分點候選+fgap+期貨開高低收），對每一檔候選：
  1) 用該候選T0+1的個股現貨1分K + 0050現貨1分K，跑跟現行部署一致的
     rolling_relative_dip訊號（window=15分, threshold=0.3%, confirm=10分)
  2) 訊號觸發則模擬進場，5%移動停利/最長10個交易日出場（期貨快取價格路徑，
     entry_frac_of_close換算，跟既有研究同一套方法）
  3) 依fgap分成 >=7%（現行部署允許的）vs <7%（現行部署會直接拒絕的）兩組比較

PYTHONPATH=src:scripts/research .venv/bin/python scripts/research/dayflip_post_dump_long_no_gap_precondition_test.py
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import numpy as np

import stock_db
from stock_db.kbar import load_kbar_day_bars

ROOT = Path(__file__).resolve().parents[2]
DAY_POOL_CACHE = ROOT / "reports/research/dayflip_fgap_calibration/day_pool_full_74d.json"
FUT_CACHE_PATH = ROOT / "reports/research/branch-footprint-screen/dayflip_gapup_short/futures_daily_cache.json"
RESULTS_CACHE = ROOT / "reports/research/dayflip_fgap_calibration/post_dump_long_rolling_dip_results.json"

ROUND_TRIP_COST_PCT = 0.05
TRAIL_PCT = 5.0
MAX_HOLD_DAYS = 10
BENCH = "0050"
ROLLING_WINDOW_MIN = 15
LAG_THRESHOLD_PCT = 0.3
CONFIRM_MINUTES = 10
FGAP_GATE = 7.0
N_BOOTSTRAP = 3000


def load_minute_closes(con: sqlite3.Connection, stock_id: str, t01: str) -> dict[str, float]:
    raw = load_kbar_day_bars(con, stock_id, t01)
    return {
        b.minute[:5]: b.close
        for b in raw
        if "09:00" <= b.minute[:5] <= "13:30" and b.close and b.close > 0
    }


def find_rolling_dip_signal(stock_closes: dict, bench_closes: dict) -> tuple[str, float] | None:
    minutes = sorted(set(stock_closes) & set(bench_closes))
    if len(minutes) < 50:
        return None
    rolling_lag = {}
    for i, m in enumerate(minutes):
        if i < ROLLING_WINDOW_MIN:
            continue
        m0 = minutes[i - ROLLING_WINDOW_MIN]
        stock_ret = (stock_closes[m] / stock_closes[m0] - 1) * 100
        bench_ret = (bench_closes[m] / bench_closes[m0] - 1) * 100
        rolling_lag[m] = stock_ret - bench_ret
    lag_minutes = sorted(rolling_lag)
    if not lag_minutes:
        return None
    worst_idx, worst_val = None, 0.0
    for i, m in enumerate(lag_minutes):
        if rolling_lag[m] < -LAG_THRESHOLD_PCT and rolling_lag[m] < worst_val:
            worst_val, worst_idx = rolling_lag[m], i
    if worst_idx is None:
        return None
    worst_minute = lag_minutes[worst_idx]
    for i in range(worst_idx + 1, len(lag_minutes)):
        m = lag_minutes[i]
        if (i - worst_idx) >= CONFIRM_MINUTES and rolling_lag[m] > rolling_lag[worst_minute] * 0.5:
            return m, stock_closes[m]
    return None


def t01_of(fut_cache: dict, stock_id: str, t0: str) -> str | None:
    m = fut_cache.get(stock_id) or {}
    dates = sorted(m)
    if t0 not in dates:
        return None
    i = dates.index(t0)
    return dates[i + 1] if i + 1 < len(dates) else None


def simulate_trailing(fut_cache: dict, stock_id: str, t01: str, entry_frac_of_close: float) -> dict | None:
    m = fut_cache.get(stock_id) or {}
    dates = sorted(m)
    if t01 not in dates:
        return None
    i0 = dates.index(t01)
    if i0 + MAX_HOLD_DAYS >= len(dates):
        return None
    fut_close_t01 = float(m[t01][1])
    if fut_close_t01 <= 0:
        return None
    fut_entry = fut_close_t01 * entry_frac_of_close
    if fut_entry <= 0:
        return None
    peak = fut_entry
    for h in range(1, MAX_HOLD_DAYS + 1):
        d = dates[i0 + h]
        px = float(m[d][1])
        if px <= 0:
            return None
        peak = max(peak, px)
        pullback = (peak - px) / peak * 100
        if pullback >= TRAIL_PCT or h == MAX_HOLD_DAYS:
            ret = (px / fut_entry - 1) * 100 - ROUND_TRIP_COST_PCT
            return {"ret": ret, "hold_days": h, "exit_day": d, "entry_px": fut_entry}
    return None


def sharpe_like(rets: list[float]) -> float:
    arr = np.array(rets)
    if len(arr) < 2 or arr.std() == 0:
        return float("nan")
    return float(arr.mean() / arr.std())


def main() -> None:
    if RESULTS_CACHE.exists():
        print(f"讀取快取 {RESULTS_CACHE}（不重跑分K查詢；要強制重算請先刪除這個檔案）")
        results = json.loads(RESULTS_CACHE.read_text(encoding="utf-8"))
        report_only(results)
        return

    day_pool = json.loads(DAY_POOL_CACHE.read_text(encoding="utf-8"))
    fut_cache = json.loads(FUT_CACHE_PATH.read_text(encoding="utf-8"))
    con = sqlite3.connect(f"file:{stock_db.DEFAULT_DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    all_candidates = []
    for t0, pool in day_pool.items():
        for r in pool:
            all_candidates.append({"t0": t0, "stock_id": r["stock_id"], "fgap": r["fgap"]})
    print(f"全部候選(不分fgap): {len(all_candidates)}筆，來自day_pool_full_74d.json（74個訊號日）\n")

    results = []
    n_no_minute_data = 0
    for i, c in enumerate(all_candidates):
        t01 = t01_of(fut_cache, c["stock_id"], c["t0"])
        if t01 is None:
            continue
        stock_closes = load_minute_closes(con, c["stock_id"], t01)
        bench_closes = load_minute_closes(con, BENCH, t01)
        if len(stock_closes) < 50 or len(bench_closes) < 50:
            n_no_minute_data += 1
            continue
        day_close_row = con.execute(
            "SELECT close FROM stock_daily_bars WHERE stock_id=? AND trade_date=? AND source='finmind' AND close>0",
            (c["stock_id"], t01),
        ).fetchone()
        if not day_close_row:
            continue
        day_close = float(day_close_row[0])
        sig = find_rolling_dip_signal(stock_closes, bench_closes)
        if sig is None:
            continue
        sig_minute, sig_price = sig
        entry_frac = sig_price / day_close
        sim = simulate_trailing(fut_cache, c["stock_id"], t01, entry_frac)
        if sim is None:
            continue
        results.append({
            "t0": c["t0"], "stock_id": c["stock_id"], "fgap": c["fgap"],
            "entry_day": t01, "entry_minute": sig_minute, "ret": sim["ret"],
            "hold_days": sim["hold_days"], "exit_day": sim["exit_day"], "entry_px": sim["entry_px"],
        })
        if (i + 1) % 100 == 0:
            print(f"  進度 {i + 1}/{len(all_candidates)}")
    con.close()

    print(f"\n完成。{len(all_candidates)}筆候選中，{n_no_minute_data}筆缺分K資料，"
          f"{len(results)}筆有完整訊號+出場結果可分析\n")

    RESULTS_CACHE.parent.mkdir(parents=True, exist_ok=True)
    RESULTS_CACHE.write_text(json.dumps(results, ensure_ascii=False), encoding="utf-8")
    print(f"已存快取到 {RESULTS_CACHE}，之後測其他門檻不用重跑分K查詢\n")

    report_only(results)


def report(name, rets):
    if not rets:
        print(f"{name}: 無樣本")
        return
    arr = np.array(rets)
    win = float(np.mean(arr > 0)) * 100
    print(f"{name}: n={len(rets)} 均報酬={arr.mean():+.3f}% 中位數={np.median(arr):+.3f}% "
          f"勝率={win:.1f}% sharpe_like={sharpe_like(rets):.3f}")


def bootstrap_compare(name_a, rets_a, name_b, rets_b):
    if len(rets_a) < 5 or len(rets_b) < 5:
        print(f"⚠️ 樣本數不足做bootstrap比較({name_a}: {len(rets_a)}筆, {name_b}: {len(rets_b)}筆)")
        return
    rng = np.random.default_rng(20260811)
    a_arr, b_arr = np.array(rets_a), np.array(rets_b)
    diffs, wins = [], 0
    for _ in range(N_BOOTSTRAP):
        s1 = rng.choice(a_arr, size=len(a_arr), replace=True)
        s2 = rng.choice(b_arr, size=len(b_arr), replace=True)
        m1, m2 = s1.mean(), s2.mean()
        diffs.append(m2 - m1)
        if m2 > m1:
            wins += 1
    diffs = np.array(diffs)
    print(f"『{name_b}』均報酬贏過『{name_a}』比例: {wins/len(diffs)*100:.1f}% "
          f"diff mean={diffs.mean():+.3f} 5th={np.percentile(diffs,5):+.3f} 95th={np.percentile(diffs,95):+.3f}")


def report_only(results: list[dict]) -> None:
    ge7 = [r["ret"] for r in results if r["fgap"] >= 7.0]
    lt7 = [r["ret"] for r in results if r["fgap"] < 7.0]
    ge4 = [r["ret"] for r in results if r["fgap"] >= 4.0]
    lt4 = [r["ret"] for r in results if r["fgap"] < 4.0]
    mid_4_7 = [r["ret"] for r in results if 4.0 <= r["fgap"] < 7.0]
    all_r = [r["ret"] for r in results]

    print("=== 分組比較 ===")
    report("fgap<4%", lt4)
    report("fgap在[4%,7%)區間（4%門檻會多放行的部分）", mid_4_7)
    report("fgap>=4%（使用者問的新門檻）", ge4)
    report("fgap>=7%（現行部署）", ge7)
    report("fgap<7%（現行部署會拒絕）", lt7)
    report("全部（不分fgap）", all_r)

    print("\n=== Block bootstrap：4%門檻 vs 現行7%門檻，重抽3000次 ===")
    bootstrap_compare("fgap>=7%(現行)", ge7, "fgap>=4%(新提案)", ge4)
    print()
    bootstrap_compare("fgap<7%(現行會拒絕的)", lt7, "[4%,7%)區間(4%門檻會多放行的部分)", mid_4_7)


if __name__ == "__main__":
    main()
