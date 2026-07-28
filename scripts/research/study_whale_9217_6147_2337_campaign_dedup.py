#!/usr/bin/env python3
"""9217（凱基-松山/廖學邦）在 6147頎邦、2337旺宏 的門檻sweep結果 campaign去重疊重驗證.

背景：上一輪對 9661/3189、9661/2408 用「同分點同股票 <=10交易日內連續買進訊號合併為一筆
campaign」重新檢驗後，原本的顯著訊號全部消失。9217 在 6147、2337 的門檻sweep結果（例如
6147 前15-20%門檻 p=0.0041~0.0168）從未用同樣方法重新驗證過，這支腳本補上：

  1. 資料缺口檢查：6147、2337 在 stock_daily_bars 是否有像 8358/1815 那樣的整段缺失
  2. 原始每日訊號 sweep 重現（沿用 study_whale_9217_6147_threshold_sweep.py 邏輯）
  3. Campaign去重疊版 sweep：對每個(先前顯著的)門檻，用 gap in {1,3,5,7,10,15,20} 交易日
     做敏感度測試，回報 n/mean/median/win/t/p
  4. 累積淨部位時間軸 + 交易密度指標（比照3189的「買進日佔比」）

用法：
  PYTHONPATH=src .venv/bin/python scripts/research/study_whale_9217_6147_2337_campaign_dedup.py
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

TRADER_ID = "9217"
HOLD_DAYS = 7
COST_RT = 0.003
BETA = 1.15
GAP_SWEEP = [1, 3, 5, 7, 10, 15, 20]
OUT_DIR = ROOT / "reports" / "research" / "branch-footprint-screen"

# 上一輪報告的「顯著」門檻，這次要重點重驗（用百分位門檻，跟原腳本 label 對齊）
KEY_PERCENTILES = {
    "6147": [0.50, 0.70, 0.75, 0.80, 0.85, 0.90],
    "2337": [0.60, 0.70, 0.75, 0.80],
}


def section(title: str) -> None:
    print(f"\n{'='*78}\n{title}\n{'='*78}")


def load_buy_days_raw(conn, stock_id: str) -> pd.DataFrame:
    """不 join 價格，取得該分點在該股票的全部買進交易日（用來檢查資料缺口影響）。"""
    q = """
        SELECT trade_date, buy FROM stock_broker_branch_daily
        WHERE securities_trader_id = ? AND stock_id = ? AND source = 'finmind' AND buy > 0
        ORDER BY trade_date
    """
    return pd.read_sql_query(q, conn, params=(TRADER_ID, stock_id))


def load_buy_days_priced(conn, stock_id: str) -> pd.DataFrame:
    """跟原 sweep 腳本相同的 INNER JOIN 版本（有資料缺口就會靜默丟棄）。"""
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


def load_all_branch_days(conn, stock_id: str) -> pd.DataFrame:
    """該分點在該股票的全部買賣交易日（不限buy>0），用來做累積淨部位時間軸。"""
    q = """
        SELECT trade_date, buy, sell, net FROM stock_broker_branch_daily
        WHERE securities_trader_id = ? AND stock_id = ? AND source = 'finmind'
        ORDER BY trade_date
    """
    return pd.read_sql_query(q, conn, params=(TRADER_ID, stock_id))


def load_bars(conn, stock_id: str) -> pd.DataFrame:
    q = """
        SELECT trade_date, open, high, low, close FROM stock_daily_bars
        WHERE stock_id = ? AND source = 'finmind' ORDER BY trade_date
    """
    return pd.read_sql_query(q, conn, params=(stock_id,)).set_index("trade_date")


def check_price_gap(bars: pd.DataFrame, stock_id: str) -> None:
    section(f"(0) 資料缺口檢查 -- {stock_id} stock_daily_bars 連續性")
    dates = pd.to_datetime(pd.Series(bars.index))
    print(f"共 {len(dates)} 筆價格資料，範圍 {dates.min().date()} ~ {dates.max().date()}")
    gaps = dates.diff().dt.days
    big = gaps[gaps > 10]
    if big.empty:
        print("[OK] 沒有發現 >10 個日曆天的缺口。")
    else:
        print("[警告] 發現以下 >10 個日曆天的缺口：")
        for idx in big.index:
            print(f"  {dates.iloc[idx-1].date()} -> {dates.iloc[idx].date()} ({int(gaps[idx])} 天)")


def check_join_drop(conn, stock_id: str, bars: pd.DataFrame) -> pd.DataFrame:
    """量化 INNER JOIN 造成的買進訊號流失（依年份）。"""
    raw = load_buy_days_raw(conn, stock_id)
    price_dates = set(bars.index)
    raw["has_price"] = raw["trade_date"].isin(price_dates)
    raw["year"] = raw["trade_date"].str[:4]
    yearly = raw.groupby("year")["has_price"].agg(["sum", "count"])
    yearly["missing"] = yearly["count"] - yearly["sum"]
    total_missing = yearly["missing"].sum()
    total = yearly["count"].sum()
    print(f"\n[INFO] {stock_id}: 分點9217買進日總數(不join價格)={total}, "
          f"因價格資料缺失被 INNER JOIN 靜默丟棄={total_missing} "
          f"({total_missing/total*100:.1f}%)")
    if total_missing > 0:
        print("按年份分解（missing=該年買進日中無對應價格資料的筆數）：")
        print(yearly.to_string())
    return raw


def merge_campaigns(dates: list[str], all_dates: list[str], gap: int) -> list[str]:
    idx = {d: i for i, d in enumerate(all_dates)}
    dates = sorted(d for d in dates if d in idx)
    if not dates:
        return []
    campaigns = []
    cur_start = dates[0]
    last_idx = idx[dates[0]]
    for d in dates[1:]:
        di = idx[d]
        if di - last_idx <= gap:
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
        rows.append({"signal_date": sig, "excess_pct": (net_ret - BETA * bench_ret) * 100})
    return pd.DataFrame(rows)


def summarize(df: pd.DataFrame, label: str, n_raw_signals: int | None = None) -> dict:
    vals = df["excess_pct"].dropna() / 100.0 if not df.empty else pd.Series(dtype=float)
    n = len(vals)
    out = {"門檻": label, "n_raw_signals": n_raw_signals, "n_trades": n}
    if n < 2 or vals.std() == 0:
        out.update({"mean_excess_pct": None, "median_excess_pct": None,
                     "win_rate_pct": None, "t": None, "p": None})
        return out
    t_stat, p_val = stats.ttest_1samp(vals, 0)
    out.update({
        "mean_excess_pct": round(vals.mean() * 100, 3),
        "median_excess_pct": round(vals.median() * 100, 3),
        "win_rate_pct": round((vals > 0).mean() * 100, 1),
        "t": round(float(t_stat), 3),
        "p": round(float(p_val), 4),
    })
    return out


def run_raw_sweep(buy_days: pd.DataFrame, bars: pd.DataFrame, bench: pd.Series, stock_id: str) -> pd.DataFrame:
    section(f"(1) 原始每日訊號門檻 Sweep 重現 -- 9217/{stock_id}")
    results = []
    bt = l1h7_backtest(buy_days["trade_date"].tolist(), bars, bench)
    results.append(summarize(bt, "全部（無門檻）", len(buy_days)))
    for pctile in (0.50, 0.60, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95):
        floor = buy_days["buy_amt"].quantile(pctile)
        sel = buy_days[buy_days["buy_amt"] >= floor]
        bt = l1h7_backtest(sel["trade_date"].tolist(), bars, bench)
        label = f"前{round((1-pctile)*100)}%（≥{floor/1e6:.2f}百萬）"
        results.append(summarize(bt, label, len(sel)))
    out = pd.DataFrame(results)
    print(out.to_string(index=False))
    return out


def run_campaign_sensitivity(buy_days: pd.DataFrame, bars: pd.DataFrame, bench: pd.Series,
                              stock_id: str, key_pctiles: list[float]) -> pd.DataFrame:
    section(f"(2) Campaign去重疊敏感度測試（多個gap值） -- 9217/{stock_id}")
    all_dates = bars.index.tolist()
    rows = []
    for pctile in key_pctiles:
        floor = buy_days["buy_amt"].quantile(pctile)
        sel = buy_days[buy_days["buy_amt"] >= floor]
        raw_dates = sel["trade_date"].tolist()
        label = f"前{round((1-pctile)*100)}%（≥{floor/1e6:.2f}百萬）"

        bt_raw = l1h7_backtest(raw_dates, bars, bench)
        s_raw = summarize(bt_raw, f"{label} | 原始(未去重疊)", len(raw_dates))
        s_raw["gap"] = "N/A(原始)"
        rows.append(s_raw)

        for gap in GAP_SWEEP:
            camp_dates = merge_campaigns(raw_dates, all_dates, gap=gap)
            bt_camp = l1h7_backtest(camp_dates, bars, bench)
            s_camp = summarize(bt_camp, f"{label} | gap<={gap}", len(camp_dates))
            s_camp["gap"] = gap
            rows.append(s_camp)

    out = pd.DataFrame(rows)
    cols = ["門檻", "gap", "n_raw_signals", "n_trades", "mean_excess_pct",
            "median_excess_pct", "win_rate_pct", "t", "p"]
    out = out[cols]
    print(out.to_string(index=False))
    return out


def position_density(conn, stock_id: str, bars: pd.DataFrame) -> None:
    section(f"(3) 累積淨部位時間軸 + 交易密度指標 -- 9217/{stock_id}")
    df = load_all_branch_days(conn, stock_id)
    if df.empty:
        print("[WARN] 無交易紀錄")
        return
    df = df.sort_values("trade_date").copy()
    df["cum_net_shares"] = df["net"].cumsum()

    first_date, last_date = df["trade_date"].iloc[0], df["trade_date"].iloc[-1]
    n_active_days = len(df)  # 有買或賣的交易日數
    buy_days_n = (df["buy"] > 0).sum()

    all_dates = bars.index.tolist()
    idx = {d: i for i, d in enumerate(all_dates)}
    if first_date in idx and last_date in idx:
        n_calendar_trading_days = idx[last_date] - idx[first_date] + 1
    else:
        n_calendar_trading_days = None

    print(f"首筆交易日: {first_date}，最新交易日: {last_date}")
    print(f"有買或賣紀錄的交易日數: {n_active_days}")
    print(f"其中 buy>0 的交易日數: {buy_days_n}")
    if n_calendar_trading_days:
        density_active_window = buy_days_n / n_calendar_trading_days * 100
        print(f"活躍期間內總交易日數(含無紀錄日): {n_calendar_trading_days}")
        print(f"買進日密度（buy日數/活躍期間總交易日數，比照3189的92.3%指標）: "
              f"{density_active_window:.1f}%")
    density_vs_all_bars = buy_days_n / len(bars) * 100
    print(f"買進日密度（buy日數/該股全部歷史交易日數）: {density_vs_all_bars:.1f}%")

    max_shares = df["cum_net_shares"].max()
    min_shares = df["cum_net_shares"].min()
    final_shares = df["cum_net_shares"].iloc[-1]
    print(f"累積淨股數峰值: {max_shares:,.0f} 股（{max_shares/1e3:.0f} 張）")
    print(f"累積淨股數谷值: {min_shares:,.0f} 股（{min_shares/1e3:.0f} 張）")
    print(f"目前累積淨股數: {final_shares:,.0f} 股（{final_shares/1e3:.0f} 張）")

    df["year"] = df["trade_date"].str[:4]
    yearly = df.groupby("year").agg(
        n_days=("trade_date", "count"),
        n_buy_days=("buy", lambda x: (x > 0).sum()),
        net_shares_k=("net", lambda x: x.sum() / 1e3),
    )
    yearly["buy_day_pct"] = (yearly["n_buy_days"] / yearly["n_days"] * 100).round(1)
    print("\n分年度交易日數 / buy日數 / 淨股數（張）:")
    print(yearly.to_string())

    # 買進訊號間交易日距離分布（自相關/重疊程度）
    buy_dates_sorted = sorted(df[df["buy"] > 0]["trade_date"].tolist())
    dd = pd.Series(buy_dates_sorted).map(idx)
    gaps = dd.diff().dropna()
    if len(gaps):
        print(f"\n買進訊號間交易日距離：中位數={gaps.median():.0f}，"
              f"距離<7交易日(與下一筆L1H7窗口重疊)比例={(gaps < 7).mean()*100:.1f}%，"
              f"距離<=10交易日(campaign合併門檻)比例={(gaps <= 10).mean()*100:.1f}%")


def main() -> int:
    conn = connect(DEFAULT_DB_PATH)
    bench = load_benchmark_close(conn, code="IX0001")

    all_raw_out = []
    all_camp_out = []

    for stock_id in ("6147", "2337"):
        bars = load_bars(conn, stock_id)
        check_price_gap(bars, stock_id)
        check_join_drop(conn, stock_id, bars)

        buy_days = load_buy_days_priced(conn, stock_id)
        print(f"\n[INFO] 9217/{stock_id}: INNER JOIN 後可用於回測的買進交易日 n={len(buy_days)}")

        raw_sweep = run_raw_sweep(buy_days, bars, bench, stock_id)
        raw_sweep.insert(0, "stock_id", stock_id)
        all_raw_out.append(raw_sweep)

        camp_sweep = run_campaign_sensitivity(buy_days, bars, bench, stock_id, KEY_PERCENTILES[stock_id])
        camp_sweep.insert(0, "stock_id", stock_id)
        all_camp_out.append(camp_sweep)

        position_density(conn, stock_id, bars)

    conn.close()

    raw_df = pd.concat(all_raw_out, ignore_index=True)
    camp_df = pd.concat(all_camp_out, ignore_index=True)

    raw_path = OUT_DIR / "whale_9217_6147_2337_raw_sweep_reproduction.csv"
    camp_path = OUT_DIR / "whale_9217_6147_2337_campaign_dedup_sensitivity.csv"
    raw_df.to_csv(raw_path, index=False)
    camp_df.to_csv(camp_path, index=False)
    print(f"\n[OK] 寫入 {raw_path}")
    print(f"[OK] 寫入 {camp_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
