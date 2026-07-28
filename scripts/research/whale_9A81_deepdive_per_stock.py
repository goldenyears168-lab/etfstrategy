#!/usr/bin/env python3
"""H-9A81-DEEPDIVE · 對 9A81（永豐金-匯立）7 檔候選股票逐一細節分析。

跑在 mini 上（分點資料完整）：
  ssh mac-mini-lan 'cd .../股票研究 && PYTHONPATH=src .venv/bin/python scripts/research/whale_9A81_deepdive_per_stock.py'

輸出：
  reports/research/branch-footprint-screen/whale_9A81_deepdive_raw.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np
import pandas as pd

from stock_db import DEFAULT_DB_PATH, connect

SOURCE = "finmind"
TRADER = "9A81"
STOCKS = ["2379", "6531", "2049", "3034", "3406", "3702", "2451"]

NAMES = {
    "2379": "瑞昱 (Realtek) - IC設計(網通/音訊/多媒體晶片)",
    "6531": "愛普* (Aspeed) - IC設計(BMC伺服器管理晶片)",
    "2049": "上銀 (Hiwin) - 精密機械/自動化(線性滑軌/滾珠螺桿/機器人)",
    "3034": "聯詠 (Novatek) - IC設計(顯示驅動IC/SoC)",
    "3406": "玉晶光 (Genius Electronic Optical) - 光學鏡頭(蘋果供應鏈)",
    "3702": "大聯大 (WPG Holdings) - 電子零組件通路商(IC代理)",
    "2451": "創見 (Transcend) - 記憶體/儲存模組",
}

OUT_DIR = ROOT / "reports/research/branch-footprint-screen"


def trend_r2(y: np.ndarray) -> float:
    n = len(y)
    if n < 3 or np.all(y == y[0]):
        return 0.0
    x = np.arange(n)
    slope, intercept = np.polyfit(x, y, 1)
    y_hat = slope * x + intercept
    ss_res = np.sum((y - y_hat) ** 2)
    ss_tot = np.sum((y - y.mean()) ** 2)
    return 1.0 - ss_res / ss_tot if ss_tot else 0.0


def load_branch_daily(conn, stock_id: str) -> pd.DataFrame:
    q = """
        SELECT b.trade_date, b.buy, b.sell, b.net, p.close
        FROM stock_broker_branch_daily b
        JOIN stock_daily_bars p
          ON p.stock_id = b.stock_id AND p.trade_date = b.trade_date AND p.source = ?
        WHERE b.source = ? AND b.securities_trader_id = ? AND b.stock_id = ?
          AND (b.buy > 0 OR b.sell > 0)
        ORDER BY b.trade_date
    """
    return pd.read_sql_query(q, conn, params=(SOURCE, SOURCE, TRADER, stock_id))


def load_full_price_series(conn, stock_id: str, start: str, end: str) -> pd.DataFrame:
    q = """
        SELECT trade_date, close
        FROM stock_daily_bars
        WHERE source = ? AND stock_id = ? AND trade_date BETWEEN ? AND ?
        ORDER BY trade_date
    """
    return pd.read_sql_query(q, conn, params=(SOURCE, stock_id, start, end))


def load_benchmark(conn, start: str, end: str) -> pd.DataFrame:
    q = """
        SELECT date as trade_date, close
        FROM daily_bars
        WHERE code = 'IX0001' AND date BETWEEN ? AND ?
        ORDER BY date
    """
    return pd.read_sql_query(q, conn, params=(start, end))


def price_on_or_after(price_df: pd.DataFrame, target_date: str) -> tuple[str | None, float | None]:
    """回傳 target_date 當天或之後第一個有交易的收盤價。"""
    sub = price_df[price_df["trade_date"] >= target_date]
    if sub.empty:
        return None, None
    row = sub.iloc[0]
    return row["trade_date"], float(row["close"])


def offset_calendar_days(date_str: str, days: int) -> str:
    d = pd.Timestamp(date_str) + pd.Timedelta(days=days)
    return d.strftime("%Y-%m-%d")


def analyze_one(conn, stock_id: str) -> dict:
    branch = load_branch_daily(conn, stock_id)
    branch = branch.sort_values("trade_date").reset_index(drop=True)
    net = branch["net"].to_numpy(dtype=float)
    cum = np.cumsum(net)
    buy_amt = (branch["buy"] * branch["close"]).to_numpy()

    first_date = branch["trade_date"].iloc[0]
    last_date = branch["trade_date"].iloc[-1]
    n_active_days = len(branch)
    span_calendar_days = (pd.Timestamp(last_date) - pd.Timestamp(first_date)).days

    peak_idx = int(np.argmax(cum))
    peak_date = branch["trade_date"].iloc[peak_idx]
    peak_cum = float(cum[peak_idx])
    final_cum = float(cum[-1])

    total_buy_shares = branch.loc[branch["buy"] > 0, "buy"].sum()
    total_buy_amt = buy_amt[branch["buy"] > 0].sum() if (branch["buy"] > 0).any() else 0.0
    weighted_avg_cost = total_buy_amt / total_buy_shares if total_buy_shares > 0 else np.nan

    # 建倉波次分析：把累積部位序列切成「上升段」與「持平/下降段」，用簡單斜率變號法
    # 用月度分桶看每月淨買超金額，抓「主要建倉的月份」
    branch["ym"] = branch["trade_date"].str.slice(0, 7)
    monthly_net_amt = (
        branch.assign(net_amt=branch["net"] * branch["close"])
        .groupby("ym")["net_amt"].sum()
        .round(0)
    )
    monthly_net_shares = branch.groupby("ym")["net"].sum().round(0)

    # 波次偵測：找累積部位序列中，連續同方向（上升 vs 走平/下降）超過 5 個交易日的區段
    waves = []
    if len(cum) >= 2:
        diffs = np.diff(cum)
        state = "up" if diffs[0] > 0 else "flat_or_down"
        seg_start = 0
        for i in range(1, len(diffs)):
            cur_state = "up" if diffs[i] > 0 else "flat_or_down"
            if cur_state != state:
                waves.append((state, branch["trade_date"].iloc[seg_start], branch["trade_date"].iloc[i], i - seg_start + 1))
                seg_start = i
                state = cur_state
        waves.append((state, branch["trade_date"].iloc[seg_start], branch["trade_date"].iloc[len(diffs)], len(diffs) - seg_start + 1))
    up_waves = [w for w in waves if w[0] == "up" and w[3] >= 5]

    # 價格脈絡：抓建倉期起點往前推 60 個交易日 ~ 至今的完整股價序列，及對應大盤
    lookback_start = offset_calendar_days(first_date, -120)
    today_end = "2026-07-28"
    price_series = load_full_price_series(conn, stock_id, lookback_start, today_end)
    bench_series = load_benchmark(conn, lookback_start, today_end)

    def price_at_or_before(df, date_str):
        sub = df[df["trade_date"] <= date_str]
        if sub.empty:
            return None, None
        row = sub.iloc[-1]
        return row["trade_date"], float(row["close"])

    pre_build_date, pre_build_price = price_at_or_before(price_series, offset_calendar_days(first_date, -1))
    build_start_date, build_start_price = price_on_or_after(price_series, first_date)
    build_end_date, build_end_price = price_on_or_after(price_series, last_date)
    latest_date, latest_price = price_series.iloc[-1]["trade_date"], float(price_series.iloc[-1]["close"])

    pre_build_bench_date, pre_build_bench = price_at_or_before(bench_series, offset_calendar_days(first_date, -1))
    build_start_bench_date, build_start_bench = price_on_or_after(bench_series, first_date)
    build_end_bench_date, build_end_bench = price_on_or_after(bench_series, last_date)
    if build_end_bench is None:
        # benchmark table 有時比個股價落後幾天，退而求其次抓 last_date 之前最近的一筆
        build_end_bench_date, build_end_bench = price_at_or_before(bench_series, last_date)
    latest_bench_date, latest_bench = bench_series.iloc[-1]["trade_date"], float(bench_series.iloc[-1]["close"])

    def pct(a, b):
        if a is None or b is None or a == 0:
            return None
        return round((b / a - 1.0) * 100, 2)

    stock_ret_pre_to_buildstart = pct(pre_build_price, build_start_price)
    stock_ret_buildstart_to_buildend = pct(build_start_price, build_end_price)
    bench_ret_pre_to_buildstart = pct(pre_build_bench, build_start_bench)
    bench_ret_buildstart_to_buildend = pct(build_start_bench, build_end_bench)

    # 三種「已實現報酬代理」版本：建倉結束(last_date) +1個月 / +3個月 / 至今最新
    d_plus1m = offset_calendar_days(last_date, 30)
    d_plus3m = offset_calendar_days(last_date, 90)
    date_1m, price_1m = price_on_or_after(price_series, d_plus1m)
    date_3m, price_3m = price_on_or_after(price_series, d_plus3m)

    def realized_pct(px):
        if px is None or not weighted_avg_cost or np.isnan(weighted_avg_cost):
            return None
        return round((px / weighted_avg_cost - 1.0) * 100, 2)

    realized_plus1m = realized_pct(price_1m)
    realized_plus3m = realized_pct(price_3m)
    realized_latest = realized_pct(latest_price)
    realized_at_peak_close_orig = realized_pct(branch["close"].iloc[peak_idx])

    return {
        "stock_id": stock_id,
        "name": NAMES.get(stock_id, "?"),
        "first_active_date": first_date,
        "last_active_date": last_date,
        "n_active_days": int(n_active_days),
        "span_calendar_days": int(span_calendar_days),
        "span_months_approx": round(span_calendar_days / 30.4, 1),
        "peak_date": peak_date,
        "peak_cum_shares": peak_cum,
        "final_cum_shares": final_cum,
        "weighted_avg_cost": round(float(weighted_avg_cost), 3) if not np.isnan(weighted_avg_cost) else None,
        "n_up_waves_ge5days": len(up_waves),
        "up_waves": [
            {"start": w[1], "end": w[2], "n_days": int(w[3])} for w in up_waves
        ],
        "monthly_net_amt": {k: float(v) for k, v in monthly_net_amt.items()},
        "monthly_net_shares": {k: float(v) for k, v in monthly_net_shares.items()},
        "price_context": {
            "pre_build_date": pre_build_date, "pre_build_price": pre_build_price,
            "build_start_date": build_start_date, "build_start_price": build_start_price,
            "build_end_date": build_end_date, "build_end_price": build_end_price,
            "latest_date": latest_date, "latest_price": latest_price,
            "stock_ret_pre_to_buildstart_pct": stock_ret_pre_to_buildstart,
            "stock_ret_during_build_pct": stock_ret_buildstart_to_buildend,
            "bench_pre_build_date": pre_build_bench_date, "bench_pre_build": pre_build_bench,
            "bench_build_start_date": build_start_bench_date, "bench_build_start": build_start_bench,
            "bench_build_end_date": build_end_bench_date, "bench_build_end": build_end_bench,
            "bench_latest_date": latest_bench_date, "bench_latest": latest_bench,
            "bench_ret_pre_to_buildstart_pct": bench_ret_pre_to_buildstart,
            "bench_ret_during_build_pct": bench_ret_buildstart_to_buildend,
        },
        "realized_proxy_variants": {
            "at_peak_close_original_method": realized_at_peak_close_orig,
            "buildend_plus_1m": {"date": date_1m, "price": price_1m, "pct": realized_plus1m},
            "buildend_plus_3m": {"date": date_3m, "price": price_3m, "pct": realized_plus3m},
            "latest_available": {"date": latest_date, "price": latest_price, "pct": realized_latest},
        },
    }


def main() -> int:
    conn = connect(DEFAULT_DB_PATH)
    results = []
    for sid in STOCKS:
        print(f"[INFO] analyzing {sid} ...")
        try:
            r = analyze_one(conn, sid)
            results.append(r)
        except Exception as e:
            print(f"[ERROR] {sid}: {e}")
    conn.close()

    out_path = OUT_DIR / "whale_9A81_deepdive_raw.json"
    out_path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[INFO] wrote {out_path}")

    for r in results:
        print(f"\n=== {r['stock_id']} {r['name']} ===")
        print(f"  建倉期: {r['first_active_date']} ~ {r['last_active_date']} ({r['span_months_approx']}個月, {r['n_active_days']}個出手交易日)")
        print(f"  波次數(>=5連續交易日上升): {r['n_up_waves_ge5days']}")
        print(f"  加權均價: {r['weighted_avg_cost']}")
        pc = r["price_context"]
        print(f"  建倉前->建倉起點 股價變化: {pc['stock_ret_pre_to_buildstart_pct']}% | 大盤: {pc['bench_ret_pre_to_buildstart_pct']}%")
        print(f"  建倉期間 股價變化: {pc['stock_ret_during_build_pct']}% | 大盤: {pc['bench_ret_during_build_pct']}%")
        rv = r["realized_proxy_variants"]
        print(f"  已實現報酬(peak原法): {rv['at_peak_close_original_method']}% | +1m: {rv['buildend_plus_1m']['pct']}% | +3m: {rv['buildend_plus_3m']['pct']}% | 至今: {rv['latest_available']['pct']}%")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
