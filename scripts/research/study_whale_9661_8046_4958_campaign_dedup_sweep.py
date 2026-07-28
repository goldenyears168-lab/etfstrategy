#!/usr/bin/env python3
"""9661 (富邦-新店/陳族元) 在 8046 南電 / 4958 臻鼎-KY 的門檻 sweep + campaign 去重疊重新驗證.

背景：上一輪門檻 sweep（study_whale_9217_6147_threshold_sweep.py --trader-id 9661）
在「原始每日訊號」下，8046/4958 前10-20% 門檻或 ≥1000萬元 門檻都出現 p<0.05 的顯著結果，
被標記為 worth_tracking。但 9661 在重倉股上交易極頻繁，同一波建倉可能連續數十天觸發訊號，
7天報酬窗口高度重疊，嚴重違反統計獨立性假設，可能虛增樣本數、灌水顯著性。

本腳本：
  1. 對 8046、4958，用門檻建議（前10%/15%/20%、≥1000萬）做 campaign 去重疊
     （同分點同股票 <=gap 交易日內的訊號合併為一筆 campaign，訊號日=campaign 起始日）
  2. 用多個 gap 值 (1,3,5,7,10,15,20) 做敏感度測試，誠實揭露多重比較
  3. 輸出「原始（未去重疊）」vs「campaign 去重疊」的對照表，讓結論可以直接比較

用法：
  PYTHONPATH=src .venv/bin/python scripts/research/study_whale_9661_8046_4958_campaign_dedup_sweep.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np
import pandas as pd
from scipy import stats

from market_benchmark import load_benchmark_close
from stock_db import DEFAULT_DB_PATH, connect

TRADER_ID = "9661"
STOCKS = ["8046", "4958"]
HOLD_DAYS = 7
COST_RT = 0.003
BETA = 1.15
GAP_SWEEP = [1, 3, 5, 7, 10, 15, 20]
OUT_DIR = ROOT / "reports/research/branch-footprint-screen"


def load_buy_days(conn, stock_id: str) -> pd.DataFrame:
    q = """
        SELECT b.trade_date, b.buy, p.close
        FROM stock_broker_branch_daily b
        JOIN stock_daily_bars p
          ON p.stock_id = b.stock_id AND p.trade_date = b.trade_date AND p.source = 'finmind'
        WHERE b.securities_trader_id = ? AND b.stock_id = ? AND b.source = 'finmind' AND b.buy > 0
        ORDER BY b.trade_date
    """
    df = pd.read_sql_query(q, conn, params=(TRADER_ID, stock_id))
    df["buy_amt"] = df["buy"] * df["close"]
    return df


def load_bars(conn, stock_id: str) -> pd.DataFrame:
    q = """
        SELECT trade_date, open, close FROM stock_daily_bars
        WHERE stock_id = ? AND source = 'finmind' ORDER BY trade_date
    """
    return pd.read_sql_query(q, conn, params=(stock_id,)).set_index("trade_date")


def merge_campaigns(dates: list[str], all_dates: list[str], gap: int) -> list[str]:
    """把 <=gap 交易日間隔內的訊號合併為一筆 campaign，回傳每個 campaign 的起始日."""
    idx = {d: i for i, d in enumerate(all_dates)}
    dates = sorted(set(dates))
    if not dates:
        return []
    campaigns = []
    cur_start = dates[0]
    last_idx = idx.get(dates[0])
    for d in dates[1:]:
        di = idx.get(d)
        if last_idx is not None and di is not None and di - last_idx <= gap:
            last_idx = di
            continue
        campaigns.append(cur_start)
        cur_start = d
        last_idx = di
    campaigns.append(cur_start)
    return campaigns


def l1h7_backtest(signal_dates: list[str], bars: pd.DataFrame, bench: pd.Series) -> pd.DataFrame:
    all_dates = bars.index.tolist()
    idx = {d: i for i, d in enumerate(all_dates)}
    bench_dates = bench.index.astype(str).tolist()
    bidx = {d: i for i, d in enumerate(bench_dates)}
    rows = []
    for sig in signal_dates:
        if sig not in idx:
            continue
        i_entry = idx[sig] + 1
        i_exit = i_entry + HOLD_DAYS - 1
        if i_entry >= len(all_dates) or i_exit >= len(all_dates):
            continue
        entry_date, exit_date = all_dates[i_entry], all_dates[i_exit]
        entry_px, exit_px = bars["open"].iloc[i_entry], bars["close"].iloc[i_exit]
        if pd.isna(entry_px) or pd.isna(exit_px) or entry_px <= 0:
            continue
        if entry_date not in bidx or exit_date not in bidx:
            continue
        b0, b1 = bench.iloc[bidx[entry_date]], bench.iloc[bidx[exit_date]]
        if pd.isna(b0) or pd.isna(b1) or b0 <= 0:
            continue
        net_ret = (exit_px / entry_px - 1.0) - COST_RT
        bench_ret = b1 / b0 - 1.0
        rows.append({"signal_date": sig, "entry_date": entry_date, "exit_date": exit_date,
                     "excess_pct": (net_ret - BETA * bench_ret) * 100})
    return pd.DataFrame(rows)


def summarize(df: pd.DataFrame, label: str, extra: dict | None = None) -> dict:
    base = {"label": label}
    if extra:
        base.update(extra)
    vals = df["excess_pct"].dropna() / 100.0 if not df.empty else pd.Series(dtype=float)
    n = len(vals)
    base["n"] = n
    if n < 2 or vals.std() == 0:
        base.update({"mean_excess_pct": None, "median_excess_pct": None,
                     "win_rate_pct": None, "t": None, "p": None})
        return base
    t_stat, p_val = stats.ttest_1samp(vals, 0)
    base.update({
        "mean_excess_pct": round(vals.mean() * 100, 3),
        "median_excess_pct": round(vals.median() * 100, 3),
        "win_rate_pct": round((vals > 0).mean() * 100, 1),
        "t": round(float(t_stat), 3),
        "p": round(float(p_val), 4),
    })
    return base


def main() -> int:
    conn = connect(DEFAULT_DB_PATH)
    bench = load_benchmark_close(conn, code="IX0001")

    all_results = []

    for stock_id in STOCKS:
        buy_days = load_buy_days(conn, stock_id)
        bars = load_bars(conn, stock_id)
        all_dates = bars.index.tolist()

        print(f"\n{'='*70}\n{stock_id}: 總買進交易日 = {len(buy_days)} / 全部交易日 = {len(all_dates)} "
              f"（佔比 {len(buy_days)/len(all_dates)*100:.1f}%）\n{'='*70}")

        # 門檻定義：無門檻(全部)、前10/15/20%百分位、絕對金額>=1000萬
        thresholds: dict[str, pd.DataFrame] = {}
        thresholds["無門檻(全部買進日)"] = buy_days
        for pct_label, q in (("前20%", 0.80), ("前15%", 0.85), ("前10%", 0.90)):
            floor = buy_days["buy_amt"].quantile(q)
            thresholds[f"{pct_label}(>={floor/1e6:.2f}百萬)"] = buy_days[buy_days["buy_amt"] >= floor]
        thresholds[">=1000萬元"] = buy_days[buy_days["buy_amt"] >= 10e6]

        for th_label, sub in thresholds.items():
            raw_dates = sorted(sub["trade_date"].tolist())
            if not raw_dates:
                continue
            # 原始（未去重疊）版本
            bt_raw = l1h7_backtest(raw_dates, bars, bench)
            s_raw = summarize(bt_raw, "原始(未去重疊)", {
                "stock_id": stock_id, "threshold": th_label, "gap": None, "n_raw_signal_days": len(raw_dates),
            })
            all_results.append(s_raw)

            # campaign 去重疊，多個 gap 值敏感度測試
            for gap in GAP_SWEEP:
                camp_dates = merge_campaigns(raw_dates, all_dates, gap=gap)
                bt_camp = l1h7_backtest(camp_dates, bars, bench)
                s_camp = summarize(bt_camp, f"campaign去重疊(gap={gap})", {
                    "stock_id": stock_id, "threshold": th_label, "gap": gap,
                    "n_raw_signal_days": len(raw_dates), "n_campaigns": len(camp_dates),
                })
                all_results.append(s_camp)

    conn.close()

    out = pd.DataFrame(all_results)
    cols = ["stock_id", "threshold", "label", "gap", "n_raw_signal_days", "n_campaigns", "n",
            "mean_excess_pct", "median_excess_pct", "win_rate_pct", "t", "p"]
    out = out[[c for c in cols if c in out.columns]]

    print(f"\n\n{'='*90}\n=== 8046 / 4958 campaign 去重疊 sweep 完整結果 ===\n{'='*90}")
    pd.set_option("display.width", 200)
    pd.set_option("display.max_rows", 200)
    print(out.to_string(index=False))

    out_path = OUT_DIR / "whale_9661_8046_4958_campaign_dedup_sweep.csv"
    out.to_csv(out_path, index=False)
    print(f"\n[OK] 完整結果寫入 {out_path}")

    # 誠實摘要：對每個門檻，比較「原始」vs「campaign去重疊」在各 gap 值下 p 值是否仍 < 0.05
    print(f"\n{'='*90}\n=== 誠實摘要：門檻 x gap 敏感度 —— p<0.05 存活情況 ===\n{'='*90}")
    for stock_id in STOCKS:
        sub = out[out["stock_id"] == stock_id]
        for th in sub["threshold"].unique():
            th_sub = sub[sub["threshold"] == th]
            raw_row = th_sub[th_sub["gap"].isna()]
            raw_p = raw_row["p"].iloc[0] if not raw_row.empty else None
            raw_n = raw_row["n"].iloc[0] if not raw_row.empty else None
            camp_rows = th_sub[th_sub["gap"].notna()].sort_values("gap")
            surv = camp_rows[camp_rows["p"] < 0.05]
            print(f"{stock_id} / {th}: 原始 n={raw_n}, p={raw_p} -> "
                  f"去重疊後 {len(surv)}/{len(camp_rows)} 個 gap 值仍 p<0.05 "
                  f"(gap值列表及p: {list(zip(camp_rows['gap'], camp_rows['p']))})")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
