#!/usr/bin/env python3
"""9661（富邦-新店）在 2634（漢翔） — artifact check + 條件式正式回測.

背景：item O 的空白股掃描發現 9661 在 2634 的平均每日淨買超 +8,717股（508 session
累計 +4.43M股），比其他檢查過的分點x標的組合大7-40倍。因為 2634 是台灣50成分股，
懷疑是ETF/指數資金流偽影，而非真正的分點信念交易。

任務：
  (1) Artifact check：
      a. 9661 是否幾乎每天都交易 2634（smooth/persistent flow 的特徵）？
      b. 9661 在 2634 的日淨買超是否集中在特定事件窗口，還是均勻分布？
      c. 高淨買日是否伴隨「很多其他分點同時買」（item N 的 red flag：ETF申購/大盤買盤）？
      d. 是否集中在0050季度調整月份（3/6/9/12月）附近？
  (2) 若通過 artifact check（事件驅動、非平滑）：套用比照 9217 study 的
      scan_5d_net95 + L1H7 + permutation test 方法論做正式回測。

用法：
  PYTHONPATH=src .venv/bin/python scripts/research/study_whale_9661_2634_artifact_and_backtest.py
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

from stock_db import DEFAULT_DB_PATH, connect
from research.branch_signal_validation import (
    build_l1h7_signal_dict,
    permutation_test,
)

TRADER_ID = "9661"
STOCK_ID = "2634"
SOURCE = "finmind"
STUDY_START = "2024-07-01"
STUDY_END = "2026-08-07"
OUT_DIR = ROOT / "reports" / "research" / "branch_9661_2634_formal_backtest"
OUT_DIR.mkdir(parents=True, exist_ok=True)

COST, HOLD, BETA = 0.003, 7, 1.15
BENCH_CODE = "IX0001"
BUY_FLOOR = 50_000_000.0
NET_MIN = 0.95
N_PERM = 5000
SEED = 20260808


def section(title: str) -> None:
    print(f"\n{'='*88}\n{title}\n{'='*88}")


# ---------------------------------------------------------------------------
# artifact check
# ---------------------------------------------------------------------------

def load_9661_2634(conn) -> pd.DataFrame:
    q = """
        SELECT b.trade_date, b.buy, b.sell, b.net, p.close
        FROM stock_broker_branch_daily b
        JOIN stock_daily_bars p
          ON p.stock_id = b.stock_id AND p.trade_date = b.trade_date AND p.source = 'finmind'
        WHERE b.securities_trader_id = ? AND b.stock_id = ? AND b.source = 'finmind'
          AND b.trade_date BETWEEN ? AND ?
        ORDER BY b.trade_date
    """
    df = pd.read_sql_query(q, conn, params=(TRADER_ID, STOCK_ID, STUDY_START, STUDY_END))
    df["net_amt"] = df["net"] * df["close"]
    return df


def load_calendar(conn, start: str, end: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT trade_date FROM stock_daily_bars
        WHERE stock_id='2330' AND source=? AND trade_date BETWEEN ? AND ? AND close>0
        ORDER BY trade_date
        """,
        (SOURCE, start, end),
    ).fetchall()
    return [str(r[0]) for r in rows]


def load_market_breadth(conn) -> pd.DataFrame:
    """每天 2634 有多少不同分點淨買 vs 淨賣，及市場淨買總股數（用來看是否『全市場齊買』）。"""
    q = """
        SELECT trade_date,
               SUM(CASE WHEN net > 0 THEN 1 ELSE 0 END) AS n_branches_net_buy,
               SUM(CASE WHEN net < 0 THEN 1 ELSE 0 END) AS n_branches_net_sell,
               COUNT(*) AS n_branches_active,
               SUM(net) AS market_net_shares,
               SUM(buy) AS market_buy_shares
        FROM stock_broker_branch_daily
        WHERE stock_id = ? AND source = 'finmind' AND trade_date BETWEEN ? AND ?
        GROUP BY trade_date
        ORDER BY trade_date
    """
    return pd.read_sql_query(q, conn, params=(STOCK_ID, STUDY_START, STUDY_END))


def artifact_check(df: pd.DataFrame, calendar: list[str], breadth: pd.DataFrame) -> dict:
    section("(1a) 交易頻率：9661 是否幾乎每天交易 2634")
    n_total_cal = len(calendar)
    n_traded = len(df)
    pct_traded = 100.0 * n_traded / n_total_cal
    print(f"研究窗口交易日總數={n_total_cal}, 9661 有掛單(buy或sell非零)天數={n_traded} "
          f"({pct_traded:.1f}%)")

    section("(1b) 平滑度：日淨買超的自相關 / 變異係數 / 零日比例")
    net = df["net"].to_numpy(dtype=float)
    nonzero_frac = float((net != 0).mean())
    n_pos = int((net > 0).sum())
    n_neg = int((net < 0).sum())
    n_zero = int((net == 0).sum())
    mean_net = float(net.mean())
    std_net = float(net.std())
    cv = std_net / abs(mean_net) if mean_net else float("nan")
    ac1 = float(pd.Series(net).autocorr(lag=1))
    ac5 = float(pd.Series(net).autocorr(lag=5))
    print(f"非零淨買賣天數比例={nonzero_frac:.3f}, 正/負/零={n_pos}/{n_neg}/{n_zero}")
    print(f"日淨買超(股) mean={mean_net:,.0f}, std={std_net:,.0f}, CV={cv:.2f}")
    print(f"自相關 lag1={ac1:.3f}, lag5={ac5:.3f}")

    section("(1c) 高淨買日的『分點齊買』紅旗檢查（比照item N）")
    merged = df.merge(breadth, on="trade_date", how="left")
    # 用9661當日net_amt排序，取前10%大單日，比較那些日子跟其他日子的breadth
    merged = merged.sort_values("trade_date").reset_index(drop=True)
    top_decile_cut = merged["net_amt"].quantile(0.9)
    top_days = merged[merged["net_amt"] >= top_decile_cut]
    other_days = merged[merged["net_amt"] < top_decile_cut]
    print(f"9661大單日(前10%, net_amt>={top_decile_cut:,.0f}) n={len(top_days)}: "
          f"平均全市場淨買分點數={top_days['n_branches_net_buy'].mean():.1f}, "
          f"平均活躍分點數={top_days['n_branches_active'].mean():.1f}")
    print(f"其餘日 n={len(other_days)}: "
          f"平均全市場淨買分點數={other_days['n_branches_net_buy'].mean():.1f}, "
          f"平均活躍分點數={other_days['n_branches_active'].mean():.1f}")
    # 9661佔全市場淨買股數的比重
    merged["trader_share_of_market_net"] = np.where(
        merged["market_net_shares"] != 0, merged["net"] / merged["market_net_shares"], np.nan
    )
    print(f"9661淨買股數 / 全市場淨買股數，中位數比重={merged['trader_share_of_market_net'].median():.3f}")

    section("(1d) 是否集中在0050季度調整月份(3/6/9/12月)附近")
    merged["month"] = merged["trade_date"].str[5:7]
    merged["is_rebalance_month"] = merged["month"].isin(["03", "06", "09", "12"])
    rb = merged[merged["is_rebalance_month"]]
    non_rb = merged[~merged["is_rebalance_month"]]
    print(f"調整月(3/6/9/12) n={len(rb)}: 平均日淨買超={rb['net'].mean():,.0f}股, "
          f"中位數={rb['net'].median():,.0f}")
    print(f"非調整月 n={len(non_rb)}: 平均日淨買超={non_rb['net'].mean():,.0f}股, "
          f"中位數={non_rb['net'].median():,.0f}")
    # t-test 兩組是否有差異
    if len(rb) > 1 and len(non_rb) > 1:
        t_stat, t_p = stats.ttest_ind(rb["net"], non_rb["net"], equal_var=False)
    else:
        t_stat, t_p = float("nan"), float("nan")
    print(f"調整月 vs 非調整月 淨買超 Welch t-test: t={t_stat:.3f}, p={t_p:.4f}")

    monthly = merged.groupby("month")["net"].agg(["mean", "median", "count"])
    print("\n各月平均日淨買超(股):")
    print(monthly.to_string())

    return {
        "n_calendar_days": n_total_cal,
        "n_traded_days": n_traded,
        "pct_traded_days": round(pct_traded, 1),
        "nonzero_frac": round(nonzero_frac, 3),
        "n_pos_days": n_pos,
        "n_neg_days": n_neg,
        "n_zero_days": n_zero,
        "mean_net_shares": round(mean_net, 0),
        "std_net_shares": round(std_net, 0),
        "cv": round(cv, 3) if cv == cv else None,
        "autocorr_lag1": round(ac1, 3),
        "autocorr_lag5": round(ac5, 3),
        "top_decile_days_avg_branches_net_buy": round(float(top_days["n_branches_net_buy"].mean()), 1),
        "other_days_avg_branches_net_buy": round(float(other_days["n_branches_net_buy"].mean()), 1),
        "top_decile_days_avg_branches_active": round(float(top_days["n_branches_active"].mean()), 1),
        "other_days_avg_branches_active": round(float(other_days["n_branches_active"].mean()), 1),
        "trader_share_of_market_net_median": round(float(merged["trader_share_of_market_net"].median()), 3)
        if merged["trader_share_of_market_net"].notna().any()
        else None,
        "rebalance_month_mean_net": round(float(rb["net"].mean()), 0) if len(rb) else None,
        "non_rebalance_month_mean_net": round(float(non_rb["net"].mean()), 0) if len(non_rb) else None,
        "rebalance_vs_nonrebalance_ttest_p": round(float(t_p), 4) if t_p == t_p else None,
        "monthly_breakdown": {
            m: {"mean": round(float(r["mean"]), 0), "median": round(float(r["median"]), 0), "n": int(r["count"])}
            for m, r in monthly.iterrows()
        },
    }


# ---------------------------------------------------------------------------
# conditional event study (single stock version of scan_5d_net95 + L1H7)
# ---------------------------------------------------------------------------

def build_5d_net95_events_single_stock(df: pd.DataFrame, calendar: list[str]) -> pd.DataFrame:
    cal_df = pd.DataFrame({"trade_date": calendar})
    df2 = df.copy()
    df2["buy_amt"] = df2["buy"] * df2["close"]
    df2["sell_amt"] = df2["sell"] * df2["close"]
    merged = cal_df.merge(df2[["trade_date", "buy_amt", "sell_amt"]], on="trade_date", how="left")
    merged["buy_amt"] = merged["buy_amt"].fillna(0.0)
    merged["sell_amt"] = merged["sell_amt"].fillna(0.0)
    merged = merged.sort_values("trade_date").reset_index(drop=True)
    merged["buy_5d"] = merged["buy_amt"].rolling(5, min_periods=5).sum()
    merged["sell_5d"] = merged["sell_amt"].rolling(5, min_periods=5).sum()
    valid = merged["buy_5d"] > 0
    merged["net_ratio"] = np.where(
        valid, (merged["buy_5d"] - merged["sell_5d"]) / merged["buy_5d"].replace(0, np.nan), np.nan
    )
    triggered = (merged["buy_5d"] >= BUY_FLOOR) & (merged["net_ratio"] >= NET_MIN)
    merged["triggered"] = triggered
    prev_trig = merged["triggered"].shift(1).fillna(False)
    merged["is_event"] = merged["triggered"] & (~prev_trig)
    events = merged[merged["is_event"]].copy().rename(columns={"trade_date": "signal_date"})
    events = events[["signal_date", "buy_5d", "sell_5d", "net_ratio"]].reset_index(drop=True)
    events["stock_id"] = STOCK_ID
    return events, merged


def next_open(bars, sig):
    for d, o, _c in bars:
        if d > sig and o > 0:
            return d, o
    return None, None


def exit_close(bars, entry, hold=HOLD):
    ordered = [x for x in bars if x[0] >= entry]
    if len(ordered) < hold:
        return None, None
    return ordered[hold - 1][0], ordered[hold - 1][2]


def load_bars(conn, sid: str) -> list[tuple[str, float, float]]:
    rows = conn.execute(
        """
        SELECT trade_date, open, close FROM stock_daily_bars
        WHERE stock_id=? AND source=? AND trade_date BETWEEN '2024-05-01' AND '2026-08-31'
          AND close>0
        ORDER BY trade_date
        """,
        (sid, SOURCE),
    ).fetchall()
    out = []
    for r in rows:
        o = float(r[1]) if r[1] else float(r[2])
        out.append((r[0], o, float(r[2])))
    return out


def load_ix(conn) -> list[tuple[str, float, float]]:
    rows = conn.execute(
        """
        SELECT date, open, close FROM daily_bars
        WHERE code=? AND date BETWEEN '2024-05-01' AND '2026-08-31' AND open>0 AND close>0
        ORDER BY date,
          CASE source WHEN 'yahoo' THEN 0 WHEN 'tej' THEN 1 WHEN 'finmind' THEN 2 ELSE 3 END
        """,
        (BENCH_CODE,),
    ).fetchall()
    out: dict[str, tuple[float, float]] = {}
    for d, o, c in rows:
        out.setdefault(d, (float(o), float(c)))
    return [(d, o, c) for d, (o, c) in sorted(out.items())]


def build_trades(bars, ix, events: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    trades = []
    n_no_entry = n_no_exit = n_no_bench = 0
    for row in events.itertuples(index=False):
        sig = row.signal_date
        ed, eo = next_open(bars, sig)
        if not ed or not eo:
            n_no_entry += 1
            continue
        xd, xc = exit_close(bars, ed, HOLD)
        if not xd or not xc:
            n_no_exit += 1
            continue
        be, bo = next_open(ix, sig)
        if not be or not bo:
            n_no_bench += 1
            continue
        _, bc = exit_close(ix, be, HOLD)
        if not bc:
            n_no_bench += 1
            continue
        r_s = xc / eo - 1 - COST
        r_ix = bc / bo - 1
        r_adj = r_s - BETA * r_ix
        trades.append(
            {
                "signal_date": sig,
                "stock_id": STOCK_ID,
                "buy_5d": round(float(row.buy_5d), 0),
                "net_ratio": round(float(row.net_ratio), 4),
                "entry_date": ed,
                "entry_open": round(eo, 4),
                "exit_date": xd,
                "exit_close": round(xc, 4),
                "r_pct": round(r_s * 100, 3),
                "r_ix_pct": round(r_ix * 100, 3),
                "r_adj_pct": round(r_adj * 100, 3),
            }
        )
    drop_stats = {
        "n_events": len(events),
        "n_trades": len(trades),
        "dropped_no_entry_open": n_no_entry,
        "dropped_no_exit_close_right_censored": n_no_exit,
        "dropped_no_bench_data": n_no_bench,
    }
    return pd.DataFrame(trades), drop_stats


def full_stats(vals_pct: pd.Series, label: str) -> dict:
    vals = vals_pct.dropna().to_numpy() / 100.0
    n = len(vals)
    out = {"label": label, "n": n}
    if n < 2 or np.std(vals) == 0:
        out.update(
            {
                "mean_pct": round(float(np.mean(vals)) * 100, 3) if n else None,
                "median_pct": round(float(np.median(vals)) * 100, 3) if n else None,
                "win_rate_pct": round(float((vals > 0).mean()) * 100, 1) if n else None,
                "t_stat": None,
                "t_p": None,
            }
        )
        return out
    t_stat, t_p = stats.ttest_1samp(vals, 0)
    out.update(
        {
            "mean_pct": round(float(np.mean(vals)) * 100, 3),
            "median_pct": round(float(np.median(vals)) * 100, 3),
            "win_rate_pct": round(float((vals > 0).mean()) * 100, 1),
            "t_stat": round(float(t_stat), 3),
            "t_p": round(float(t_p), 4),
        }
    )
    return out


def main() -> int:
    conn = connect(DEFAULT_DB_PATH)
    df = load_9661_2634(conn)
    calendar = load_calendar(conn, STUDY_START, STUDY_END)
    breadth = load_market_breadth(conn)
    print(f"[INFO] 9661x2634 有掛單天數={len(df)}, 研究窗口交易日={len(calendar)}")

    ac_result = artifact_check(df, calendar, breadth)

    section("(2) Artifact-check 結論摘要")
    verdict_smooth = (
        ac_result["pct_traded_days"] >= 90.0
        and (ac_result["cv"] is None or ac_result["cv"] < 3.0)
    )
    print(json.dumps(ac_result, ensure_ascii=False, indent=2))
    print(f"\n[VERDICT-HINT] 交易天數比例={ac_result['pct_traded_days']}% "
          f"(>=90% 視為近乎每日persistent flow)")

    (OUT_DIR / "artifact_check_result.json").write_text(
        json.dumps(ac_result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    breadth_merged = df.merge(breadth, on="trade_date", how="left")
    breadth_merged.to_csv(OUT_DIR / "whale_9661_2634_daily_with_breadth.csv", index=False)

    section("(3) 條件式正式回測：scan_5d_net95 + L1H7 + permutation test（單股版，比照9217方法論）")
    events, merged_grid = build_5d_net95_events_single_stock(df, calendar)
    events.to_csv(OUT_DIR / "whale_9661_2634_5dnet95_events.csv", index=False)
    print(f"[INFO] rising-edge 觸發事件數 n={len(events)}")
    print(events.to_string())

    bars = load_bars(conn, STOCK_ID)
    ix = load_ix(conn)
    trades, drop_stats = build_trades(bars, ix, events)
    conn.close()
    trades.to_csv(OUT_DIR / "whale_9661_2634_5dnet95_trades.csv", index=False)
    print(json.dumps(drop_stats, ensure_ascii=False, indent=2))

    full = full_stats(trades["r_adj_pct"], f"full_sample_n{len(trades)}") if len(trades) else {"n": 0}
    print(json.dumps(full, ensure_ascii=False, indent=2))

    perm_result = None
    if len(trades) >= 3:
        stock_dict = build_l1h7_signal_dict(bars)
        ix_dict = build_l1h7_signal_dict(ix)
        perm_result = permutation_test(
            trades[["stock_id", "signal_date"]],
            {STOCK_ID: stock_dict},
            ix_dict,
            n_perm=N_PERM,
            seed=SEED,
        )
        print(json.dumps(perm_result, ensure_ascii=False, indent=2, default=str))

    summary = {
        "trader_id": TRADER_ID,
        "stock_id": STOCK_ID,
        "artifact_check": ac_result,
        "protocol": {
            "signal": "rolling 5-trading-day buy_5d>=5000萬 & net_ratio>=0.95 (single-stock version of scan_5d_net95)",
            "study_window": f"{STUDY_START}..{STUDY_END}",
            "l1h7": {"cost": COST, "hold": HOLD, "beta": BETA, "bench": BENCH_CODE},
        },
        "n_events": len(events),
        "drop_stats": drop_stats,
        "full_sample_stats": full,
        "permutation_test": perm_result,
    }
    (OUT_DIR / "whale_9661_2634_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print(f"\n[OK] 寫入 {OUT_DIR / 'whale_9661_2634_summary.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
