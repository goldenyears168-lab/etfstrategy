#!/usr/bin/env python3
"""PCB/載板/軟板同產業對照組 vs 9661(富邦-新店/陳族元) 在 8046/4958 的訊號.

目的：排除產業共同因子混淆。8046（南電/PCB載板）、4958（臻鼎-KY/軟板）在 2024-2026
經歷主升段，若「追9661大單」的 edge 只是「追這段期間PCB/載板類股本身就賺」的產業
共同因子，那麼同期同板塊、但9661沒有重倉的對照股，用同樣的訊號日期/同樣的策略
也應該表現類似。

方法（兩種互補測試，誠實比較）：
  (A) 「移植訊號日」測試：把 9661 在 8046 / 4958 的實際 campaign 去重疊訊號日
      （campaign 起始日）原封不動搬到對照股上，跑 L1H7 回測。
      這控制了「同一段時間」，只換掉「哪一檔股票」。
      如果對照股用同樣的日期表現跟 8046/4958 差不多甚至更好，代表 edge 主要來自
      「那段時間 buy PCB 供應鏈」的產業/期間共同因子，不是 9661 選股本身。
  (B) 「該股自己最大漲幅窗口」測試：對每檔對照股，找出它在 2024-01-01~2026-07-27
      期間累積漲幅最大的一段窗口（用 rolling N 日報酬找 peak），並在該窗口內任何
      一個交易日進場都跑 L1H7，看看「隨便買在這波breakout」是否普遍有效。

對照股選取（PCB載板/軟板供應鏈，同期亦有主升段，且非9661重倉股，先用
DB 查詢確認 9661 在這些股票的交易量遠低於 8046/4958 才正式採用）：
  3037 欣興（IC載板）、2383 台光電（CCL覆銅箔）、3044 健鼎（PCB）、
  6274 台燿（CCL）、6213 聯茂（CCL）

用法：
  PYTHONPATH=src .venv/bin/python scripts/research/study_pcb_sector_control_group_vs_9661_8046_4958.py
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
WHALE_STOCKS = ["8046", "4958"]
CONTROL_STOCKS = ["3044", "2367", "2316"]
CONTROL_NAMES = {"3044": "健鼎", "2367": "燿華電子", "2316": "楠梓電子"}
# 注意：原本擬用 3037(欣興)/2383(台光電)/6274(台燿)/2313(華通) 作對照組，但預查發現
# 9661 在這幾檔的買進金額(9661 buy_amt: 3037=104億, 2313=83億, 2383=64億, 6274=52億)
# 竟然跟/甚至超過他在 8046(84億)/4958(59億) 的曝險量級——代表這個分點對整個PCB/CCL
# 供應鏈都重押，不是只挑8046/4958。這件事本身就是重要發現（見報告），但也代表這四檔
# 不能拿來當「他沒重倉」的乾淨對照組，因此排除。6269/6141/8213 則是 stock_daily_bars
# 幾乎沒有價格資料（跟8358/1815一樣的資料缺口問題，INNER JOIN會全部靜默丟棄），
# 也排除。最終對照組為 3044(健鼎)/2367(燿華電子)/2316(楠梓電子)——
# 這三檔 9661 買進金額僅為 8046/4958 的 16-37%，相對曝險明顯較低，且有完整價格歷史。
HOLD_DAYS = 7
COST_RT = 0.003
BETA = 1.15
CAMPAIGN_GAP = 10  # 專案慣例預設值
THRESHOLD_PCTL = 0.85  # 前15%，落在建議「前10-20%」門檻區間中段
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


def load_9661_exposure(conn, stock_id: str) -> dict:
    q = """
        SELECT COUNT(*) n_buy_days, COALESCE(SUM(buy),0) buy_shares, MIN(trade_date) mn, MAX(trade_date) mx
        FROM stock_broker_branch_daily
        WHERE securities_trader_id = ? AND stock_id = ? AND source = 'finmind' AND buy > 0
    """
    r = pd.read_sql_query(q, conn, params=(TRADER_ID, stock_id)).iloc[0]
    return r.to_dict()


def merge_campaigns(dates: list[str], all_dates: list[str], gap: int) -> list[str]:
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
        rows.append({"signal_date": sig, "excess_pct": (net_ret - BETA * bench_ret) * 100})
    return pd.DataFrame(rows)


def summarize(df: pd.DataFrame, label: str, extra: dict | None = None) -> dict:
    base = {"label": label}
    if extra:
        base.update(extra)
    vals = df["excess_pct"].dropna() / 100.0 if not df.empty else pd.Series(dtype=float)
    n = len(vals)
    base["n"] = n
    if n < 2 or vals.std() == 0:
        base.update({"mean_excess_pct": None, "median_excess_pct": None, "win_rate_pct": None, "t": None, "p": None})
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


def find_best_rally_window(bars: pd.DataFrame, window: int = 60) -> tuple[str, str, float]:
    """找出 rolling `window` 交易日報酬率最大的窗口，回傳 (start_date, end_date, return_pct)."""
    close = bars["close"]
    fwd_ret = close.shift(-window) / close - 1.0
    if fwd_ret.dropna().empty:
        return None, None, None
    best_start_idx = fwd_ret.idxmax()
    ret = fwd_ret.loc[best_start_idx]
    all_dates = bars.index.tolist()
    si = all_dates.index(best_start_idx)
    ei = min(si + window, len(all_dates) - 1)
    return all_dates[si], all_dates[ei], ret * 100


def main() -> int:
    conn = connect(DEFAULT_DB_PATH)
    bench = load_benchmark_close(conn, code="IX0001")

    # ---- Step 0: 確認對照股確實不是 9661 重倉股（交易量遠低於 8046/4958） ----
    print(f"{'='*80}\n[Step 0] 9661 在對照股 vs 重倉股 的交易曝險比較（確認對照組真的沒被他重押）\n{'='*80}")
    exposure_rows = []
    for sid in WHALE_STOCKS + CONTROL_STOCKS:
        exp = load_9661_exposure(conn, sid)
        exp["stock_id"] = sid
        exp["group"] = "重倉股(whale)" if sid in WHALE_STOCKS else "對照股(control)"
        exp["name"] = CONTROL_NAMES.get(sid, "")
        exposure_rows.append(exp)
    exp_df = pd.DataFrame(exposure_rows)[["group", "stock_id", "name", "n_buy_days", "buy_shares", "mn", "mx"]]
    print(exp_df.to_string(index=False))
    exp_df.to_csv(OUT_DIR / "whale_9661_pcb_control_exposure_check.csv", index=False)

    # ---- Step 1: 取得 8046/4958 的 campaign 去重疊訊號日（前15%門檻, gap=10）----
    print(f"\n{'='*80}\n[Step 1] 9661 在 8046/4958 的 campaign 去重疊訊號日（前15%門檻, gap={CAMPAIGN_GAP}）\n{'='*80}")
    whale_campaign_dates: dict[str, list[str]] = {}
    whale_bars: dict[str, pd.DataFrame] = {}
    for sid in WHALE_STOCKS:
        buy_days = load_buy_days(conn, sid)
        bars = load_bars(conn, sid)
        whale_bars[sid] = bars
        floor = buy_days["buy_amt"].quantile(THRESHOLD_PCTL)
        sel = buy_days[buy_days["buy_amt"] >= floor]
        all_dates = bars.index.tolist()
        camp = merge_campaigns(sel["trade_date"].tolist(), all_dates, gap=CAMPAIGN_GAP)
        whale_campaign_dates[sid] = camp
        print(f"{sid}: 前15%門檻(>={floor/1e6:.2f}百萬) 原始 {len(sel)} 天 -> 去重疊後 {len(camp)} 個 campaign")
        print(f"   campaign 起始日: {camp}")

        # 這檔股票自己的去重疊回測結果，作為對照基準
        bt_self = l1h7_backtest(camp, bars, bench)
        s_self = summarize(bt_self, f"{sid}自己(campaign去重疊)", {"stock_id": sid})
        print(f"   -> L1H7 excess: {s_self}")

    # ---- Step 2 (A): 把這些訊號日「移植」到對照股，看是否也有效 ----
    print(f"\n{'='*80}\n[Step 2A] 移植訊號日測試：同樣的日期，換成對照股，L1H7 是否也有效？\n{'='*80}")
    transplant_results = []
    for whale_sid, camp_dates in whale_campaign_dates.items():
        bt_whale = l1h7_backtest(camp_dates, whale_bars[whale_sid], bench)
        s_whale = summarize(bt_whale, f"原股{whale_sid}(基準)", {"signal_source": whale_sid, "applied_to": whale_sid, "group": "whale(原股)"})
        transplant_results.append(s_whale)
        for ctrl_sid in CONTROL_STOCKS:
            ctrl_bars = load_bars(conn, ctrl_sid)
            bt_ctrl = l1h7_backtest(camp_dates, ctrl_bars, bench)
            s_ctrl = summarize(bt_ctrl, f"移植到{ctrl_sid}({CONTROL_NAMES[ctrl_sid]})",
                                {"signal_source": whale_sid, "applied_to": ctrl_sid, "group": "control(移植)"})
            transplant_results.append(s_ctrl)

    transplant_df = pd.DataFrame(transplant_results)
    print(transplant_df.to_string(index=False))
    transplant_df.to_csv(OUT_DIR / "whale_9661_pcb_control_transplant_signal_test.csv", index=False)

    # ---- Step 2 (B): 對照股自己歷史最大漲幅窗口，任意一天買進 L1H7 ----
    print(f"\n{'='*80}\n[Step 2B] 對照股自己最大漲幅窗口（60交易日）內任意一天買進，L1H7 是否普遍有效？\n{'='*80}")
    rally_results = []
    for sid in WHALE_STOCKS + CONTROL_STOCKS:
        bars = whale_bars[sid] if sid in whale_bars else load_bars(conn, sid)
        # 只看 2024-01-01 以後的資料，避免抓到太舊的窗口
        recent_bars = bars[bars.index >= "2024-01-01"]
        if recent_bars.empty or len(recent_bars) < 65:
            print(f"{sid}: 2024年後資料不足，略過")
            continue
        start_d, end_d, ret_pct = find_best_rally_window(recent_bars, window=60)
        if start_d is None:
            continue
        window_dates = [d for d in recent_bars.index.tolist() if start_d <= d <= end_d]
        bt = l1h7_backtest(window_dates, bars, bench)
        s = summarize(bt, f"{sid}最大60日漲幅窗口內任一天買進", {
            "stock_id": sid, "group": "whale" if sid in WHALE_STOCKS else "control",
            "name": CONTROL_NAMES.get(sid, ""),
            "window_start": start_d, "window_end": end_d, "window_raw_return_pct": round(ret_pct, 1),
        })
        rally_results.append(s)
        print(f"{sid}({CONTROL_NAMES.get(sid,'')}): 最大60日窗口 {start_d}~{end_d} 原始報酬 {ret_pct:+.1f}% "
              f"-> 窗口內任一天買L1H7: {s}")

    rally_df = pd.DataFrame(rally_results)
    rally_df.to_csv(OUT_DIR / "whale_9661_pcb_control_own_rally_window_test.csv", index=False)

    conn.close()

    # ---- 誠實摘要 ----
    print(f"\n{'='*80}\n=== 誠實摘要 ===\n{'='*80}")
    for whale_sid in WHALE_STOCKS:
        sub = transplant_df[transplant_df["signal_source"] == whale_sid]
        whale_row = sub[sub["applied_to"] == whale_sid].iloc[0]
        ctrl_rows = sub[sub["applied_to"] != whale_sid]
        n_ctrl_positive = (ctrl_rows["mean_excess_pct"].astype(float) > 0).sum()
        n_ctrl_sig = (ctrl_rows["p"].astype(float) < 0.05).sum()
        print(f"\n{whale_sid} 訊號日移植測試：")
        print(f"  原股 {whale_sid} mean_excess={whale_row['mean_excess_pct']}%, p={whale_row['p']}, n={whale_row['n']}")
        print(f"  {len(ctrl_rows)} 檔對照股中，{n_ctrl_positive} 檔 mean_excess>0，{n_ctrl_sig} 檔 p<0.05")
        print(f"  對照股平均 mean_excess: {ctrl_rows['mean_excess_pct'].astype(float).mean():.3f}%")

    print(f"\n[OK] 全部結果已寫入 {OUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
