#!/usr/bin/env python3
"""9699（富邦-經紀，待確認分點全名）× 7769（展宏）深度盡職調查.

背景：round3「個股優先」切角在20檔候選股票池找到 9699×7769 是唯一交叉驗證後仍站得住
腳的新線索（n=9去重事件、H20 median +13.75%、勝率88.9%、單樣本t p=0.024）。9699 在整個
分點宇宙（2024-07~2026-07）只交易過5檔股票，結構上很像9217/松山模式。此腳本比照
round3 對 9216/9B2a/5852/9268/9800 的深挖規格，做完整盡職調查：

  (1) entity-level 造市簽名檢查（最便宜的第一道過濾器）：交易日全勤率、買賣金額平衡度、
      觸及股票數/活躍天數
  (2) 完整交易歷史（不套用rn<=3/去重門檻的原始 stock_broker_branch_daily 記錄）
  (3) 建倉型態（cum_net_shares 時序，7769 + 6696）
  (4) 統計穩健性交叉驗證（t-test + Wilcoxon + bootstrap CI，mean/median都做，
      leave-1/2-out 敏感度分析，H7弱/H14-H20強時的leg分解）
  (5) 時序切分（前半段 vs 後半段 / 近期OOS）
  (6) 對手方/crossing查證（7769事件日的賣方分點）
  (7) 6696 campaign去重疊重新檢視
  (8) 9699 其餘標的（6831/7610/7734）快速掃描

***必須在 mini 上跑***（本地 SQLite 複本是舊的）：
  ssh mac-mini-lan "cd '.../股票研究' && PYTHONPATH=src .venv/bin/python scripts/research/study_whale_9699_7769_deepdive.py"
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np
import pandas as pd
from scipy import stats

from market_benchmark import load_benchmark_close
from stock_db import DEFAULT_DB_PATH, connect

TRADER_ID = "9699"
FOCUS_STOCK = "7769"
SECONDARY_STOCK = "6696"
ALL_KNOWN_STOCKS = ["7769", "6696", "6831", "7610", "7734"]
STOCK_NAMES = {"7769": "展宏", "6696": "汎銓", "6831": "動聯", "7610": "太雅", "7734": "晶心科"}

HOLD_DAYS_LIST = [7, 14, 20]
COST_RT = 0.003
BETA = 1.15
DEDUP_DAYS = 5
CAMPAIGN_GAP_SWEEP = [1, 3, 5, 7, 10, 15, 20]
MIN_AMT = 5_000_000.0
OUT_DIR = ROOT / "reports/research/branch-footprint-screen"
PREFIX = "whale_9699_7769_"

N_BOOTSTRAP = 20000
RNG_SEED = 42


def section(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


# ---------------------------------------------------------------------------
# 資料載入
# ---------------------------------------------------------------------------
def load_raw_all_trades(conn, trader_id: str) -> pd.DataFrame:
    """該分點的全部原始交易紀錄，不限標的、不套用任何門檻."""
    q = """
        SELECT trade_date, stock_id, buy, sell, net
        FROM stock_broker_branch_daily
        WHERE securities_trader_id = ? AND source = 'finmind'
        ORDER BY trade_date
    """
    df = pd.read_sql_query(q, conn, params=(trader_id,))
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    return df


def load_bars(conn, stock_id: str) -> pd.DataFrame:
    q = """
        SELECT trade_date, open, high, low, close, volume FROM stock_daily_bars
        WHERE stock_id = ? AND source = 'finmind' ORDER BY trade_date
    """
    df = pd.read_sql_query(q, conn, params=(stock_id,))
    return df


def load_all_branches_for_stock(conn, stock_id: str, date_start: str, date_end: str) -> pd.DataFrame:
    """該股票在時間窗內全部分點的買賣紀錄（給 crossing check 用）."""
    q = """
        SELECT trade_date, securities_trader_id, buy, sell, net
        FROM stock_broker_branch_daily
        WHERE stock_id = ? AND source = 'finmind' AND trade_date >= ? AND trade_date <= ?
        ORDER BY trade_date
    """
    return pd.read_sql_query(q, conn, params=(stock_id, date_start, date_end))


# ---------------------------------------------------------------------------
# (1) entity-level 造市簽名檢查
# ---------------------------------------------------------------------------
def part1_entity_signature(conn, raw_all: pd.DataFrame) -> dict:
    section("(1) entity-level 造市簽名檢查")
    stocks_touched = sorted(raw_all["stock_id"].unique().tolist())
    date_min, date_max = raw_all["trade_date"].min(), raw_all["trade_date"].max()
    n_active_days_total = raw_all["trade_date"].nunique()
    print(f"9699 全樣本：觸及 {len(stocks_touched)} 檔股票 {stocks_touched}")
    print(f"活躍日期範圍：{date_min.date()} ~ {date_max.date()}，共 {n_active_days_total} 個不同交易日出手")

    rows = []
    for sid in stocks_touched:
        g = raw_all[raw_all["stock_id"] == sid].copy()
        bars = load_bars(conn, sid)
        bars["trade_date"] = pd.to_datetime(bars["trade_date"])
        # 該股票在9699活躍窗口內的總交易日數
        win_start, win_end = g["trade_date"].min(), g["trade_date"].max()
        stock_total_days = bars[(bars["trade_date"] >= win_start) & (bars["trade_date"] <= win_end)][
            "trade_date"
        ].nunique()
        n_9699_days = g["trade_date"].nunique()
        attendance_pct = n_9699_days / stock_total_days * 100 if stock_total_days else np.nan

        px = bars.set_index("trade_date")["close"]
        g = g.set_index("trade_date")
        g["close"] = px.reindex(g.index).ffill()
        g["buy_amt"] = g["buy"] * g["close"]
        g["sell_amt"] = g["sell"] * g["close"]
        total_buy_amt = g["buy_amt"].sum()
        total_sell_amt = g["sell_amt"].sum()
        buy_share_pct = total_buy_amt / (total_buy_amt + total_sell_amt) * 100 if (total_buy_amt + total_sell_amt) else np.nan

        dual = g[(g["buy"] > 0) & (g["sell"] > 0)]
        pct_dual_side_days = len(dual) / len(g) * 100 if len(g) else np.nan

        rows.append({
            "stock_id": sid,
            "stock_name": STOCK_NAMES.get(sid, ""),
            "window": f"{win_start.date()}~{win_end.date()}",
            "n_active_days_9699": n_9699_days,
            "stock_total_trading_days_in_window": stock_total_days,
            "attendance_pct": round(attendance_pct, 1) if not np.isnan(attendance_pct) else None,
            "total_buy_amt_ntd": round(total_buy_amt, 0),
            "total_sell_amt_ntd": round(total_sell_amt, 0),
            "buy_share_of_gross_pct": round(buy_share_pct, 1) if not np.isnan(buy_share_pct) else None,
            "pct_dual_side_days": round(pct_dual_side_days, 1),
        })
        print(f"  {sid} {STOCK_NAMES.get(sid,''):6s}: 活躍{n_9699_days}天/該股窗口內共{stock_total_days}天 "
              f"(全勤率{attendance_pct:.1f}%), 買方佔比{buy_share_pct:.1f}%, 雙邊同日{pct_dual_side_days:.1f}%")

    out = pd.DataFrame(rows)
    out.to_csv(OUT_DIR / f"{PREFIX}1_entity_signature.csv", index=False)

    # 造市簽名判斷：全勤率是否接近100%、買賣比例是否接近50/50、觸及股票數是否近乎全市場
    mm_flags = {
        "n_stocks_touched_total": len(stocks_touched),
        "stocks_touched": stocks_touched,
        "high_attendance_any_gt80pct": bool((out["attendance_pct"].fillna(0) > 80).any()),
        "balanced_5050_any_stock_45to55pct": bool(
            out["buy_share_of_gross_pct"].between(45, 55).any()
        ),
        "verdict": None,
    }
    if mm_flags["n_stocks_touched_total"] <= 10 and not mm_flags["high_attendance_any_gt80pct"] and not mm_flags["balanced_5050_any_stock_45to55pct"]:
        mm_flags["verdict"] = "PASS (directional signature, not market-making)"
    else:
        mm_flags["verdict"] = "FLAG (needs closer look)"
    print(f"\n  造市簽名判斷: {mm_flags['verdict']}")
    with open(OUT_DIR / f"{PREFIX}1_marketmaking_flags.json", "w", encoding="utf-8") as f:
        json.dump(mm_flags, f, ensure_ascii=False, indent=2, default=str)

    return {"per_stock": out, "flags": mm_flags}


# ---------------------------------------------------------------------------
# (2)+(3) 完整交易歷史 + 建倉型態
# ---------------------------------------------------------------------------
def part2_3_full_history_and_position(conn, raw_all: pd.DataFrame) -> None:
    section("(2)+(3) 完整交易歷史 + 建倉型態 (7769 / 6696)")
    for sid in [FOCUS_STOCK, SECONDARY_STOCK]:
        g = raw_all[raw_all["stock_id"] == sid].sort_values("trade_date").copy()
        bars = load_bars(conn, sid)
        bars["trade_date"] = pd.to_datetime(bars["trade_date"])
        px = bars.set_index("trade_date")["close"]
        g["close"] = px.reindex(g["trade_date"].values).values
        g["cum_net_shares"] = g["net"].cumsum()
        g["buy_amt"] = g["buy"] * g["close"]
        g["sell_amt"] = g["sell"] * g["close"]
        g.to_csv(OUT_DIR / f"{PREFIX}2_raw_history_{sid}.csv", index=False)
        print(f"\n  {sid} {STOCK_NAMES.get(sid,'')}: 全部原始交易紀錄 n={len(g)} 筆 "
              f"({g['trade_date'].min().date()} ~ {g['trade_date'].max().date()})")
        print(f"    買進日: {(g['buy']>0).sum()}, 賣出日: {(g['sell']>0).sum()}, "
              f"雙邊日: {((g['buy']>0)&(g['sell']>0)).sum()}")
        print(f"    最終累積淨部位: {g['cum_net_shares'].iloc[-1]:,.0f} 股, "
              f"峰值: {g['cum_net_shares'].max():,.0f} 股, 谷值: {g['cum_net_shares'].min():,.0f} 股")
        peak = g['cum_net_shares'].max()
        final = g['cum_net_shares'].iloc[-1]
        drawdown_from_peak_pct = (peak - final) / peak * 100 if peak > 0 else np.nan
        print(f"    峰值後回落比例: {drawdown_from_peak_pct:.1f}% "
              f"({'單向建倉持有' if drawdown_from_peak_pct < 20 else '進出頻繁/大幅減碼'})")
        # 每月累積表
        g["ym"] = g["trade_date"].dt.strftime("%Y-%m")
        monthly = g.groupby("ym").agg(
            n_days=("net", "size"), net_shares=("net", "sum"),
            buy_amt=("buy_amt", "sum"), sell_amt=("sell_amt", "sum"),
        ).reset_index()
        monthly["cum_net_shares_eom"] = monthly["net_shares"].cumsum()
        monthly.to_csv(OUT_DIR / f"{PREFIX}3_monthly_position_{sid}.csv", index=False)
        print(f"    月度分布 ({len(monthly)} 個月有交易):")
        for _, r in monthly.iterrows():
            print(f"      {r['ym']}: {int(r['n_days'])}天, 淨買超{r['net_shares']:,.0f}股, "
                  f"月末累積{r['cum_net_shares_eom']:,.0f}股")


# ---------------------------------------------------------------------------
# L1H7 event builder (rn<=3 dedup, 沿用 SSOT 協議)
# ---------------------------------------------------------------------------
def build_events(conn, raw_all: pd.DataFrame, stock_id: str, bench: pd.Series) -> pd.DataFrame:
    g = raw_all[raw_all["stock_id"] == stock_id].copy()
    bars = load_bars(conn, stock_id)
    bars["trade_date"] = pd.to_datetime(bars["trade_date"])
    bars = bars.sort_values("trade_date").reset_index(drop=True)
    px_close = bars.set_index("trade_date")["close"]
    g["close"] = px_close.reindex(g["trade_date"]).ffill().values
    g["buy_amt"] = g["buy"] * g["close"]
    g = g[g["buy_amt"] >= MIN_AMT].copy()
    if g.empty:
        return pd.DataFrame()

    dates = bars["trade_date"].tolist()
    opens = bars["open"].tolist()
    closes = bars["close"].tolist()
    idx = {d: i for i, d in enumerate(dates)}
    bench_dates = bench.index.tolist()
    bidx = {d: i for i, d in enumerate(bench_dates)}
    max_h = max(HOLD_DAYS_LIST)

    rows = []
    for _, r in g.sort_values("trade_date").iterrows():
        sig = r["trade_date"]
        if sig not in idx:
            continue
        i_sig = idx[sig]
        i_entry = i_sig + 1
        if i_entry + max_h - 1 >= len(dates):
            continue
        entry_date = dates[i_entry]
        entry_open = opens[i_entry]
        if pd.isna(entry_open) or entry_open <= 0:
            continue
        if entry_date not in bidx:
            continue
        rec = {"signal_date": sig, "stock_id": stock_id, "buy_amt": r["buy_amt"],
               "entry_date": entry_date, "entry_open": entry_open}
        b0 = bench.loc[entry_date]
        ok = True
        for h in HOLD_DAYS_LIST:
            i_exit = i_entry + h - 1
            exit_date = dates[i_exit]
            exit_close = closes[i_exit]
            if exit_date not in bidx or pd.isna(exit_close):
                ok = False
                break
            b1 = bench.loc[exit_date]
            r_s = exit_close / entry_open - 1 - COST_RT
            r_ix = b1 / b0 - 1
            rec[f"exit_date_h{h}"] = exit_date
            rec[f"r_adj_h{h}_pct"] = (r_s - BETA * r_ix) * 100
        if not ok:
            continue
        rows.append(rec)
    events = pd.DataFrame(rows)
    if events.empty:
        return events
    events = events.sort_values("signal_date").reset_index(drop=True)
    keep_rows = []
    last_kept = None
    for _, r in events.iterrows():
        if last_kept is None or (r["signal_date"] - last_kept).days > DEDUP_DAYS:
            keep_rows.append(r)
            last_kept = r["signal_date"]
    return pd.DataFrame(keep_rows).reset_index(drop=True)


def build_events_campaign(conn, raw_all: pd.DataFrame, stock_id: str, bench: pd.Series, gap_days: int) -> pd.DataFrame:
    """跟 build_events 相同，但用交易日 gap（非曆日）合併 campaign."""
    g = raw_all[raw_all["stock_id"] == stock_id].copy()
    bars = load_bars(conn, stock_id)
    bars["trade_date"] = pd.to_datetime(bars["trade_date"])
    bars = bars.sort_values("trade_date").reset_index(drop=True)
    px_close = bars.set_index("trade_date")["close"]
    g["close"] = px_close.reindex(g["trade_date"]).ffill().values
    g["buy_amt"] = g["buy"] * g["close"]
    g = g[g["buy_amt"] >= MIN_AMT].copy()
    if g.empty:
        return pd.DataFrame()

    dates = bars["trade_date"].tolist()
    idx = {d: i for i, d in enumerate(dates)}
    sig_dates = sorted(g["trade_date"].unique().tolist())
    # merge into campaigns by trading-day gap
    campaign_starts = []
    cur_start = sig_dates[0]
    last_i = idx.get(sig_dates[0])
    for d in sig_dates[1:]:
        di = idx.get(d)
        if last_i is not None and di is not None and di - last_i <= gap_days:
            last_i = di
            continue
        campaign_starts.append(cur_start)
        cur_start = d
        last_i = di
    campaign_starts.append(cur_start)

    opens = bars["open"].tolist()
    closes = bars["close"].tolist()
    bench_dates = bench.index.tolist()
    bidx = {d: i for i, d in enumerate(bench_dates)}
    max_h = max(HOLD_DAYS_LIST)
    rows = []
    for sig in campaign_starts:
        if sig not in idx:
            continue
        i_sig = idx[sig]
        i_entry = i_sig + 1
        if i_entry + max_h - 1 >= len(dates):
            continue
        entry_date = dates[i_entry]
        entry_open = opens[i_entry]
        if pd.isna(entry_open) or entry_open <= 0 or entry_date not in bidx:
            continue
        rec = {"signal_date": sig, "entry_date": entry_date, "entry_open": entry_open}
        b0 = bench.loc[entry_date]
        ok = True
        for h in HOLD_DAYS_LIST:
            i_exit = i_entry + h - 1
            exit_date = dates[i_exit]
            exit_close = closes[i_exit]
            if exit_date not in bidx or pd.isna(exit_close):
                ok = False
                break
            b1 = bench.loc[exit_date]
            r_s = exit_close / entry_open - 1 - COST_RT
            r_ix = b1 / b0 - 1
            rec[f"r_adj_h{h}_pct"] = (r_s - BETA * r_ix) * 100
        if not ok:
            continue
        rows.append(rec)
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 統計工具
# ---------------------------------------------------------------------------
def bootstrap_ci(vals: np.ndarray, stat_fn, n_boot: int = N_BOOTSTRAP, seed: int = RNG_SEED) -> tuple[float, float]:
    rng = np.random.default_rng(seed)
    n = len(vals)
    boots = np.empty(n_boot)
    for i in range(n_boot):
        sample = rng.choice(vals, size=n, replace=True)
        boots[i] = stat_fn(sample)
    return float(np.percentile(boots, 2.5)), float(np.percentile(boots, 97.5))


def full_stats(vals: pd.Series, label: str) -> dict:
    v = vals.dropna().to_numpy() / 100.0
    n = len(v)
    out = {"label": label, "n": n}
    if n < 2:
        return out
    t_stat, t_p = stats.ttest_1samp(v, 0)
    try:
        w_stat, w_p = stats.wilcoxon(v)
    except ValueError:
        w_stat, w_p = np.nan, np.nan
    mean_ci = bootstrap_ci(v, np.mean)
    median_ci = bootstrap_ci(v, np.median)
    out.update({
        "mean_pct": round(float(v.mean()) * 100, 3),
        "median_pct": round(float(np.median(v)) * 100, 3),
        "win_rate_pct": round(float((v > 0).mean()) * 100, 1),
        "t_stat": round(float(t_stat), 3),
        "t_p": round(float(t_p), 5),
        "wilcoxon_stat": round(float(w_stat), 3) if not np.isnan(w_stat) else None,
        "wilcoxon_p": round(float(w_p), 5) if not np.isnan(w_p) else None,
        "mean_bootstrap_ci95_pct": [round(mean_ci[0] * 100, 3), round(mean_ci[1] * 100, 3)],
        "median_bootstrap_ci95_pct": [round(median_ci[0] * 100, 3), round(median_ci[1] * 100, 3)],
    })
    return out


# ---------------------------------------------------------------------------
# (4) 統計穩健性
# ---------------------------------------------------------------------------
def part4_statistical_robustness(events: pd.DataFrame) -> list[dict]:
    section("(4) 統計穩健性交叉驗證 (7769, n=%d)" % len(events))
    results = []
    for h in HOLD_DAYS_LIST:
        col = f"r_adj_h{h}_pct"
        r = full_stats(events[col], f"H{h}_full")
        results.append(r)
        print(f"  H{h}: n={r['n']}, mean={r.get('mean_pct')}%, median={r.get('median_pct')}%, "
              f"win={r.get('win_rate_pct')}%, t_p={r.get('t_p')}, wilcoxon_p={r.get('wilcoxon_p')}, "
              f"mean_CI95={r.get('mean_bootstrap_ci95_pct')}, median_CI95={r.get('median_bootstrap_ci95_pct')}")

    # leave-1-out / leave-2-out 敏感度分析 (對 H20, 最大報酬天期最容易被極端值主導)
    print("\n  Leave-k-out 敏感度分析 (H20):")
    col = "r_adj_h20_pct"
    vals = events[col].dropna().to_numpy()
    sorted_idx = np.argsort(-np.abs(vals - np.median(vals)))  # 依離中位數最遠排序 (抓極端值)
    for k in [1, 2]:
        drop_idx = sorted_idx[:k]
        keep = np.delete(vals, drop_idx)
        if len(keep) >= 2:
            t_stat, t_p = stats.ttest_1samp(keep / 100.0, 0)
            try:
                _, w_p = stats.wilcoxon(keep / 100.0)
            except ValueError:
                w_p = np.nan
            print(f"    去除最極端{k}筆後: n={len(keep)}, mean={keep.mean():.2f}%, median={np.median(keep):.2f}%, "
                  f"t_p={t_p:.5f}, wilcoxon_p={w_p if np.isnan(w_p) else round(w_p,5)}")
            results.append({
                "label": f"H20_drop_{k}_extreme", "n": len(keep),
                "mean_pct": round(float(keep.mean()), 3), "median_pct": round(float(np.median(keep)), 3),
                "t_p": round(float(t_p), 5), "wilcoxon_p": round(float(w_p), 5) if not np.isnan(w_p) else None,
            })

    # leg 分解: H8-14 增量段, H15-20 增量段 (檢驗「H7弱、H14/20強」是否為統計假象)
    print("\n  Leg 分解 (是否為報酬std隨天期擴大的統計假象):")
    if "r_adj_h14_pct" in events.columns and "r_adj_h7_pct" in events.columns:
        leg_8_14 = events["r_adj_h14_pct"] - events["r_adj_h7_pct"]
        r = full_stats(leg_8_14, "leg_H8-14_incremental")
        results.append(r)
        print(f"    H8-14增量段: n={r['n']}, mean={r.get('mean_pct')}%, median={r.get('median_pct')}%, "
              f"t_p={r.get('t_p')}, wilcoxon_p={r.get('wilcoxon_p')}")
    if "r_adj_h20_pct" in events.columns and "r_adj_h14_pct" in events.columns:
        leg_15_20 = events["r_adj_h20_pct"] - events["r_adj_h14_pct"]
        r = full_stats(leg_15_20, "leg_H15-20_incremental")
        results.append(r)
        print(f"    H15-20增量段: n={r['n']}, mean={r.get('mean_pct')}%, median={r.get('median_pct')}%, "
              f"t_p={r.get('t_p')}, wilcoxon_p={r.get('wilcoxon_p')}")

    return results


# ---------------------------------------------------------------------------
# (5) 時序切分
# ---------------------------------------------------------------------------
def part5_time_split(events: pd.DataFrame) -> list[dict]:
    section("(5) 時序/樣本外切分 (7769)")
    events = events.sort_values("signal_date").reset_index(drop=True)
    n = len(events)
    results = []
    half = n // 2
    first_half = events.iloc[:half]
    second_half = events.iloc[half:]
    for label, g in [("first_half", first_half), ("second_half", second_half)]:
        r = full_stats(g["r_adj_h20_pct"], f"{label}_H20")
        r["date_range"] = f"{g['signal_date'].min().date()}~{g['signal_date'].max().date()}" if len(g) else None
        results.append(r)
        print(f"  {label} ({r.get('date_range')}): n={r['n']}, mean={r.get('mean_pct')}%, "
              f"median={r.get('median_pct')}%, win={r.get('win_rate_pct')}%")

    recent_k = min(3, n)
    recent = events.iloc[-recent_k:]
    r = full_stats(recent["r_adj_h20_pct"], f"most_recent_{recent_k}_H20")
    r["date_range"] = f"{recent['signal_date'].min().date()}~{recent['signal_date'].max().date()}"
    results.append(r)
    print(f"  最近{recent_k}筆 (OOS proxy, {r.get('date_range')}): n={r['n']}, mean={r.get('mean_pct')}%, "
          f"median={r.get('median_pct')}%, win={r.get('win_rate_pct')}%")

    print("\n  全部事件時間點列表:")
    for _, row in events.iterrows():
        print(f"    {row['signal_date'].date()}: H7={row.get('r_adj_h7_pct'):.2f}%, "
              f"H14={row.get('r_adj_h14_pct'):.2f}%, H20={row.get('r_adj_h20_pct'):.2f}%")

    return results


# ---------------------------------------------------------------------------
# (6) crossing check
# ---------------------------------------------------------------------------
def part6_crossing_check(conn, events: pd.DataFrame) -> pd.DataFrame:
    section("(6) 對手方/crossing 查證 (7769 事件日)")
    sig_dates = events["signal_date"].dt.strftime("%Y-%m-%d").tolist()
    date_min, date_max = min(sig_dates), max(sig_dates)
    all_branches = load_all_branches_for_stock(conn, FOCUS_STOCK, date_min, date_max)
    bars = load_bars(conn, FOCUS_STOCK)
    bars["trade_date"] = pd.to_datetime(bars["trade_date"]).dt.strftime("%Y-%m-%d")
    px = bars.set_index("trade_date")["close"]

    rows = []
    for sd in sig_dates:
        day = all_branches[all_branches["trade_date"] == sd].copy()
        if day.empty:
            continue
        close = px.get(sd, np.nan)
        day["buy_amt"] = day["buy"] * close
        day["sell_amt"] = day["sell"] * close
        top_sell = day.sort_values("sell_amt", ascending=False).iloc[0] if day["sell_amt"].max() > 0 else None
        total_sell_amt = day["sell_amt"].sum()
        rows.append({
            "signal_date": sd,
            "n_branches_active": len(day),
            "top_sell_trader_id": top_sell["securities_trader_id"] if top_sell is not None else None,
            "top_sell_amt": round(float(top_sell["sell_amt"]), 0) if top_sell is not None else None,
            "top_sell_share_of_day_sell_pct": round(float(top_sell["sell_amt"]) / total_sell_amt * 100, 1)
            if top_sell is not None and total_sell_amt > 0 else None,
        })
    out = pd.DataFrame(rows)
    out.to_csv(OUT_DIR / f"{PREFIX}6_crossing_check.csv", index=False)
    print(out.to_string(index=False))

    if not out.empty:
        top_sellers = out["top_sell_trader_id"].value_counts()
        print(f"\n  賣方分點重複出現次數: {top_sellers.to_dict()}")
        max_repeat = int(top_sellers.max()) if len(top_sellers) else 0
        n_events = len(out)
        print(f"  最高重複次數 {max_repeat} / {n_events} 筆事件 "
              f"({'疑似固定對手方，需留意對敲可能' if max_repeat >= max(3, n_events//2) else '賣方分散，無明顯固定對手方模式'})")
    return out


# ---------------------------------------------------------------------------
# (7) 6696 campaign dedup 重新檢視
# ---------------------------------------------------------------------------
def part7_6696_campaign_dedup(conn, raw_all: pd.DataFrame, bench: pd.Series) -> list[dict]:
    section("(7) 6696(汎銓) campaign 去重疊重新檢視")
    results = []
    for gap in CAMPAIGN_GAP_SWEEP:
        ev = build_events_campaign(conn, raw_all, SECONDARY_STOCK, bench, gap)
        if ev.empty:
            continue
        r = full_stats(ev["r_adj_h20_pct"], f"6696_gap{gap}_H20")
        results.append(r)
        print(f"  gap={gap}交易日: n={r['n']}, mean={r.get('mean_pct')}%, median={r.get('median_pct')}%, "
              f"win={r.get('win_rate_pct')}%, t_p={r.get('t_p')}, wilcoxon_p={r.get('wilcoxon_p')}")
    # 原始4筆訊號日期供參照
    raw_ev = build_events(conn, raw_all, SECONDARY_STOCK, bench)
    if not raw_ev.empty:
        print(f"\n  原始(5日dedup)訊號日期: {raw_ev['signal_date'].dt.date.tolist()}")
    return results


# ---------------------------------------------------------------------------
# (8) 9699 其餘標的快速掃描
# ---------------------------------------------------------------------------
def part8_other_stocks(conn, raw_all: pd.DataFrame, bench: pd.Series) -> list[dict]:
    section("(8) 9699 其餘標的快速掃描 (6831/7610/7734)")
    results = []
    for sid in ["6831", "7610", "7734"]:
        ev = build_events(conn, raw_all, sid, bench)
        if ev.empty:
            print(f"  {sid} {STOCK_NAMES.get(sid,'')}: 無可計算事件 (可能資料不足或未達門檻)")
            results.append({"label": f"{sid}_H20", "n": 0})
            continue
        r = full_stats(ev["r_adj_h20_pct"], f"{sid}_H20")
        results.append(r)
        print(f"  {sid} {STOCK_NAMES.get(sid,'')}: n={r['n']}, mean={r.get('mean_pct')}%, "
              f"median={r.get('median_pct')}%, win={r.get('win_rate_pct')}%, t_p={r.get('t_p')}")
        ev.to_csv(OUT_DIR / f"{PREFIX}8_events_{sid}.csv", index=False)
    return results


# ---------------------------------------------------------------------------
def main() -> int:
    conn = connect(DEFAULT_DB_PATH)
    bench = load_benchmark_close(conn)
    bench.index = pd.to_datetime(bench.index)

    raw_all = load_raw_all_trades(conn, TRADER_ID)
    print(f"[INFO] 9699 全部原始交易紀錄: {len(raw_all)} 筆, "
          f"{raw_all['trade_date'].min().date()} ~ {raw_all['trade_date'].max().date()}, "
          f"觸及股票: {sorted(raw_all['stock_id'].unique().tolist())}")
    raw_all.to_csv(OUT_DIR / f"{PREFIX}0_raw_all_trades.csv", index=False)

    entity_result = part1_entity_signature(conn, raw_all)
    part2_3_full_history_and_position(conn, raw_all)

    events_7769 = build_events(conn, raw_all, FOCUS_STOCK, bench)
    events_7769.to_csv(OUT_DIR / f"{PREFIX}events_7769_rebuilt.csv", index=False)
    print(f"\n[INFO] 7769 重建事件數: {len(events_7769)} (應對照舊值 n=9)")

    stat_results = part4_statistical_robustness(events_7769)
    time_split_results = part5_time_split(events_7769)
    crossing_result = part6_crossing_check(conn, events_7769)
    campaign_6696_results = part7_6696_campaign_dedup(conn, raw_all, bench)
    other_stock_results = part8_other_stocks(conn, raw_all, bench)

    summary = {
        "trader_id": TRADER_ID,
        "focus_stock": FOCUS_STOCK,
        "n_events_rebuilt": len(events_7769),
        "entity_marketmaking_verdict": entity_result["flags"]["verdict"],
        "stat_robustness": stat_results,
        "time_split": time_split_results,
        "campaign_dedup_6696": campaign_6696_results,
        "other_stocks": other_stock_results,
    }
    with open(OUT_DIR / f"{PREFIX}FULL_SUMMARY.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2, default=str)

    conn.close()
    section("DONE")
    print(f"輸出目錄: {OUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
