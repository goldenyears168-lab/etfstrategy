#!/usr/bin/env python3
"""設計C：beta校正離均差(短期版) —— 用T0之前20個交易日(以及對照10個交易日)
個股日報酬對0050日報酬的OLS beta，把rolling_relative_dip的離均差
dev(t) = 個股滾動15分鐘報酬(t) - 大盤滾動15分鐘報酬(t) 除以 max(beta,0.3)
做正規化，再對正規化後序列在進場前15分鐘窗口做線性回歸取斜率，跟後續
交易報酬(ret)做Spearman IC + walk-forward(前70%/後30%) + permutation test。

PIT要點：beta用T0(含)以前的日報酬(嚴格早於entry_day，entry_day=T0+1)，
不會用到收盤後才公布的資料，日K本身T0收盤價在entry_day開盤前就已知，安全。

20天與10天兩個回看窗口都跑，不挑最好看的，verdict取兩者中較保守者。

PYTHONPATH=src .venv/bin/python scripts/research/dayflip_post_dump_long_beta_adj_dev_slope_short.py
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
from scipy.stats import spearmanr

import stock_db
from stock_db.kbar import load_kbar_day_bars

ROOT = Path(__file__).resolve().parents[2]
RESULTS_CACHE = ROOT / "reports/research/dayflip_fgap_calibration/post_dump_long_rolling_dip_results.json"
BENCH = "0050"
ROLLING_WINDOW_MIN = 15
SLOPE_WINDOW_MIN = 15
BETA_WINDOWS = [20, 10]
BETA_FLOOR = 0.3
N_PERM = 3000


def load_minute_closes(con: sqlite3.Connection, stock_id: str, day: str) -> dict[str, float]:
    raw = load_kbar_day_bars(con, stock_id, day)
    return {b.minute[:5]: b.close for b in raw if "09:00" <= b.minute[:5] <= "13:30" and b.close and b.close > 0}


def deviation_series(stock_closes: dict, bench_closes: dict) -> dict[str, float]:
    minutes = sorted(set(stock_closes) & set(bench_closes))
    out = {}
    for i, m in enumerate(minutes):
        if i < ROLLING_WINDOW_MIN:
            continue
        m0 = minutes[i - ROLLING_WINDOW_MIN]
        stock_ret = (stock_closes[m] / stock_closes[m0] - 1) * 100
        bench_ret = (bench_closes[m] / bench_closes[m0] - 1) * 100
        out[m] = stock_ret - bench_ret
    return out


def window_slope(series: dict[str, float], entry_minute: str) -> float | None:
    entry_dt = datetime.strptime(entry_minute, "%H:%M")
    start_dt = entry_dt - timedelta(minutes=SLOPE_WINDOW_MIN)
    window_minutes = sorted(m for m in series if start_dt.strftime("%H:%M") <= m <= entry_minute)
    if len(window_minutes) < 5:
        return None
    xs = np.arange(len(window_minutes))
    ys = np.array([series[m] for m in window_minutes])
    return float(np.polyfit(xs, ys, 1)[0])


def load_daily_closes(con: sqlite3.Connection, stock_id: str) -> list[tuple[str, float]]:
    cur = con.execute(
        "SELECT trade_date, close FROM stock_daily_bars WHERE stock_id=? AND source='finmind' "
        "AND close IS NOT NULL ORDER BY trade_date",
        (stock_id,),
    )
    return [(r["trade_date"], float(r["close"])) for r in cur.fetchall()]


def compute_beta(stock_daily: list[tuple[str, float]], bench_daily: list[tuple[str, float]],
                  t0: str, lookback: int) -> float | None:
    """用 <=t0 的日收盤算 daily return，取最近lookback個交易日的報酬序列，
    對0050做OLS回歸取斜率(beta)。t0本身收盤價可用(entry_day在t0+1開盤才交易，
    beta是t0收盤後就已知的特徵，不是收盤後才公布的統計)。"""
    s_map = {d: c for d, c in stock_daily if d <= t0}
    b_map = {d: c for d, c in bench_daily if d <= t0}
    common_days = sorted(set(s_map) & set(b_map))
    if len(common_days) < lookback + 1:
        return None
    recent_days = common_days[-(lookback + 1):]
    s_rets = []
    b_rets = []
    for i in range(1, len(recent_days)):
        d_prev, d_cur = recent_days[i - 1], recent_days[i]
        s_rets.append(s_map[d_cur] / s_map[d_prev] - 1)
        b_rets.append(b_map[d_cur] / b_map[d_prev] - 1)
    s_rets = np.array(s_rets)
    b_rets = np.array(b_rets)
    if np.std(b_rets) < 1e-10:
        return None
    beta = float(np.polyfit(b_rets, s_rets, 1)[0])
    return beta


def walk_forward_report(label: str, rows: list[dict], feature_key: str) -> dict:
    rows_sorted = sorted(rows, key=lambda t: (t["entry_day"], t["entry_minute"]))
    n_train = int(len(rows_sorted) * 0.7)
    train, test = rows_sorted[:n_train], rows_sorted[n_train:]
    out = {}
    for split_label, group in [("full", rows_sorted), ("train", train), ("test", test)]:
        xs = np.array([t[feature_key] for t in group])
        ys = np.array([t["ret"] for t in group])
        ic, pval = spearmanr(xs, ys)
        out[split_label] = {"n": len(group), "ic": float(ic), "p": float(pval)}
    xs_full = np.array([t[feature_key] for t in rows_sorted])
    ys_full = np.array([t["ret"] for t in rows_sorted])
    real_ic, _ = spearmanr(xs_full, ys_full)
    rng = np.random.default_rng(20260811)
    perm_ics = []
    for _ in range(N_PERM):
        shuffled = rng.permutation(ys_full)
        perm_ic, _ = spearmanr(xs_full, shuffled)
        perm_ics.append(abs(perm_ic))
    perm_p = float(np.mean(np.array(perm_ics) >= abs(real_ic)))
    out["perm_p"] = perm_p
    out["n"] = len(rows_sorted)
    print(f"\n=== {label} ===")
    for k in ("full", "train", "test"):
        v = out[k]
        print(f"  {k}: n={v['n']} IC={v['ic']:.3f} p={v['p']:.3f}")
    print(f"  permutation p={perm_p:.3f} (n_perm={N_PERM})")
    return out


def main() -> None:
    con = sqlite3.connect(f"file:{stock_db.DEFAULT_DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    trades = json.loads(RESULTS_CACHE.read_text(encoding="utf-8"))
    sub = [t for t in trades if t["fgap"] >= 4.0]

    bench_daily = load_daily_closes(con, BENCH)
    daily_cache: dict[str, list[tuple[str, float]]] = {}

    enriched = []
    n_missing_intraday = 0
    n_missing_beta = {w: 0 for w in BETA_WINDOWS}
    for t in sub:
        stock_closes = load_minute_closes(con, t["stock_id"], t["entry_day"])
        bench_closes = load_minute_closes(con, BENCH, t["entry_day"])
        if len(stock_closes) < 30 or len(bench_closes) < 30:
            n_missing_intraday += 1
            continue
        dev = deviation_series(stock_closes, bench_closes)
        raw_slope = window_slope(dev, t["entry_minute"])
        if raw_slope is None:
            n_missing_intraday += 1
            continue

        if t["stock_id"] not in daily_cache:
            daily_cache[t["stock_id"]] = load_daily_closes(con, t["stock_id"])
        stock_daily = daily_cache[t["stock_id"]]

        row = {**t, "raw_dev_slope": raw_slope}
        ok = True
        for w in BETA_WINDOWS:
            beta = compute_beta(stock_daily, bench_daily, t["t0"], w)
            if beta is None:
                n_missing_beta[w] += 1
                ok = False
                continue
            beta_used = max(beta, BETA_FLOOR)
            dev_adj_series = {m: v / beta_used for m, v in dev.items()}
            adj_slope = window_slope(dev_adj_series, t["entry_minute"])
            row[f"beta_{w}"] = beta
            row[f"dev_adj_slope_{w}"] = adj_slope
        enriched.append(row)
    con.close()

    print(f"意圖樣本: {len(sub)}筆 (fgap>=4%)")
    print(f"缺分鐘K線: {n_missing_intraday}筆")
    for w in BETA_WINDOWS:
        print(f"缺{w}日beta(日K不足): {n_missing_beta[w]}筆")
    print(f"可比對(有離均差斜率): {len(enriched)}筆")

    reports = {}
    for w in BETA_WINDOWS:
        usable = [r for r in enriched if r.get(f"dev_adj_slope_{w}") is not None]
        print(f"\n{'='*60}\n{w}日回看窗口 beta校正離均差斜率  (可用n={len(usable)})\n{'='*60}")
        beta_arr = np.array([r[f"beta_{w}"] for r in usable])
        print(f"beta分布: min={beta_arr.min():.2f} median={np.median(beta_arr):.2f} max={beta_arr.max():.2f} "
              f"(<{BETA_FLOOR}被floor的比例={np.mean(beta_arr < BETA_FLOOR):.1%})")
        rep = walk_forward_report(f"{w}日beta校正 dev_adj_slope vs ret", usable, f"dev_adj_slope_{w}")
        reports[w] = rep

    print(f"\n{'='*60}\n對照：原始(未校正)離均差斜率 raw_dev_slope vs ret (同一批可用樣本, 用20日版本的usable集合)\n{'='*60}")
    usable20 = [r for r in enriched if r.get("dev_adj_slope_20") is not None]
    raw_rep = walk_forward_report("原始離均差斜率(對照組)", usable20, "raw_dev_slope")

    print("\n=== 兩個回看窗口誠實對照 ===")
    for w in BETA_WINDOWS:
        r = reports[w]
        print(f"{w}日: train IC={r['train']['ic']:.3f}(p={r['train']['p']:.3f})  "
              f"test IC={r['test']['ic']:.3f}(p={r['test']['p']:.3f})  perm_p={r['perm_p']:.3f}")

    out_path = ROOT / "reports/research/dayflip_fgap_calibration/post_dump_long_beta_adj_dev_slope_short_result.json"
    out_path.write_text(json.dumps({
        "n_usable_by_window": {str(w): reports[w]["n"] for w in BETA_WINDOWS},
        "reports": reports,
        "raw_dev_slope_control": raw_rep,
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n寫入: {out_path}")


if __name__ == "__main__":
    main()
