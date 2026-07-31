#!/usr/bin/env python3
"""Track A.3：把 9217 專用的 scan_5d_net95 live訊號決定性驗證泛化成 --trader-id 通用版.

沿用 study_whale_9217_5d_net95_live_signal_validation.py 完全相同的協議（滾動5日
buy_5d>=buy_floor 且 net_ratio>=net_min、rising-edge去重、L1H7: T+1開盤進場/H7收盤
出場/30bps成本/β=1.15、walk-forward IS(60%)/OOS(40%)、年度分解、去極值敏感度、
permutation/placebo顯著性檢定），只是把 TRADER_ID/BUY_FLOOR/NET_MIN 參數化。

預設門檻沿用9217（songshan-copytrade, buy_floor=0.5億, net_min=0.95）——9661沒有
自己的copytrade job/門檻設定，此為合理預設（同協議才能跟9217結果直接比較），如果
使用者對特定分點有不同門檻想法，用 --buy-floor/--net-min 覆寫即可。

用法（必須在 mini 上跑）：
    PYTHONPATH=src .venv/bin/python scripts/research/study_whale_branch_5d_net95_live_signal_validation.py --trader-id 9661

輸出（檔名依 trader_id 命名）：
    reports/research/branch-footprint-screen/whale_{trader_id}_5dnet95_events.csv
    reports/research/branch-footprint-screen/whale_{trader_id}_5dnet95_trades.csv
    reports/research/branch-footprint-screen/whale_{trader_id}_5dnet95_summary.json
    reports/research/branch-footprint-screen/whale_{trader_id}_permutation_test_result.json
"""

from __future__ import annotations

import argparse
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
    outlier_trim_sensitivity as perm_outlier_trim_sensitivity,
)

SOURCE = "finmind"
BENCH_CODE = "IX0001"
STUDY_START = "2024-07-01"
STUDY_END = "2026-07-24"
COST, HOLD, BETA = 0.003, 7, 1.15  # L1H7 SSOT
MEGA_PATH = (
    ROOT / "reports" / "research" / "branch-footprint-screen" / "ab58_xMega_copytrade" / "mega_blacklist_v1.json"
)
OLD_EVENTS_CSV = (
    ROOT / "reports" / "research" / "branch-footprint-screen" / "whale_branch_l1h7_universe_events.csv"
)
OUT_DIR = ROOT / "reports" / "research" / "branch-footprint-screen"
N_BOOTSTRAP = 10_000
N_PERM = 20_000
RNG_SEED = 20260728


def section(title: str) -> None:
    print(f"\n{'='*88}\n{title}\n{'='*88}")


def load_mega(path: Path) -> set[str]:
    data = json.loads(path.read_text())
    return set(str(s) for s in data.get("symbols", []))


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


def load_raw_activity(conn, trader_id: str, start: str, end: str) -> pd.DataFrame:
    q = """
        SELECT b.stock_id, b.trade_date,
               b.buy * p.close AS buy_amt,
               b.sell * p.close AS sell_amt
        FROM stock_broker_branch_daily b
        JOIN stock_daily_bars p
          ON p.stock_id = b.stock_id AND p.trade_date = b.trade_date AND p.source = ?
        WHERE b.source = ?
          AND b.securities_trader_id = ?
          AND b.trade_date BETWEEN ? AND ?
          AND p.close > 0
          AND length(b.stock_id) = 4
          AND b.stock_id GLOB '[0-9][0-9][0-9][0-9]'
          AND b.stock_id NOT GLOB '00*'
    """
    return pd.read_sql_query(q, conn, params=(SOURCE, SOURCE, trader_id, start, end))


def load_stock_bars(conn, sid: str) -> list[tuple[str, float, float]]:
    rows = conn.execute(
        """
        SELECT trade_date, open, close FROM stock_daily_bars
        WHERE stock_id=? AND source=? AND trade_date BETWEEN ? AND ? AND close>0
        ORDER BY trade_date
        """,
        (sid, SOURCE, "2024-05-01", "2026-08-31"),
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
        WHERE code=? AND date BETWEEN ? AND ? AND open>0 AND close>0
        ORDER BY date,
          CASE source WHEN 'yahoo' THEN 0 WHEN 'tej' THEN 1 WHEN 'finmind' THEN 2 ELSE 3 END
        """,
        (BENCH_CODE, "2024-05-01", "2026-08-31"),
    ).fetchall()
    out: dict[str, tuple[float, float]] = {}
    for d, o, c in rows:
        out.setdefault(d, (float(o), float(c)))
    return [(d, o, c) for d, (o, c) in sorted(out.items())]


def build_5d_net95_events(
    conn, trader_id: str, mega: set[str], buy_floor: float, net_min: float
) -> pd.DataFrame:
    calendar = load_calendar(conn, STUDY_START, STUDY_END)
    print(f"[INFO] 市場交易日曆（2330）：{len(calendar)} 天，{calendar[0]} ~ {calendar[-1]}")

    raw = load_raw_activity(conn, trader_id, STUDY_START, STUDY_END)
    print(f"[INFO] {trader_id} 全市場(4碼普通股,排除00*) 有活動的 (stock,day) 筆數 = {len(raw)}, "
          f"涵蓋 {raw['stock_id'].nunique()} 檔股票")
    if raw.empty:
        return pd.DataFrame(columns=["stock_id", "signal_date", "buy_5d", "sell_5d", "net_ratio"])

    stocks = sorted(raw["stock_id"].unique())
    grid = pd.MultiIndex.from_product([stocks, calendar], names=["stock_id", "trade_date"]).to_frame(
        index=False
    )
    merged = grid.merge(raw, on=["stock_id", "trade_date"], how="left")
    merged["buy_amt"] = merged["buy_amt"].fillna(0.0)
    merged["sell_amt"] = merged["sell_amt"].fillna(0.0)
    merged = merged.sort_values(["stock_id", "trade_date"]).reset_index(drop=True)

    g = merged.groupby("stock_id", sort=False)
    merged["buy_5d"] = g["buy_amt"].transform(lambda s: s.rolling(5, min_periods=5).sum())
    merged["sell_5d"] = g["sell_amt"].transform(lambda s: s.rolling(5, min_periods=5).sum())

    valid = merged["buy_5d"] > 0
    merged["net_ratio"] = np.where(
        valid, (merged["buy_5d"] - merged["sell_5d"]) / merged["buy_5d"].replace(0, np.nan), np.nan
    )

    triggered = (
        (merged["buy_5d"] >= buy_floor) & (merged["net_ratio"] >= net_min) & (~merged["stock_id"].isin(mega))
    )
    merged["triggered"] = triggered
    prev_trig = merged.groupby("stock_id", sort=False)["triggered"].shift(1).fillna(False)
    merged["is_event"] = merged["triggered"] & (~prev_trig)

    events = merged[merged["is_event"]].copy()
    events = events.rename(columns={"trade_date": "signal_date"})
    events = events[["stock_id", "signal_date", "buy_5d", "sell_5d", "net_ratio"]].sort_values(
        "signal_date"
    ).reset_index(drop=True)
    print(f"[INFO] 滾動5日觸發事件（rising-edge 去重後） n = {len(events)}, "
          f"涵蓋 {events['stock_id'].nunique()} 檔股票")
    return events


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


def build_trades(conn, events: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    ix = load_ix(conn)
    bars_cache: dict[str, list] = {}
    trades = []
    n_no_stock_bars = n_no_entry = n_no_exit = n_no_bench = 0

    for row in events.itertuples(index=False):
        sid = row.stock_id
        sig = row.signal_date
        if sid not in bars_cache:
            bars_cache[sid] = load_stock_bars(conn, sid)
        bars = bars_cache[sid]
        if len(bars) < 10:
            n_no_stock_bars += 1
            continue
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
                "stock_id": sid,
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
        "dropped_insufficient_stock_bars": n_no_stock_bars,
        "dropped_no_entry_open": n_no_entry,
        "dropped_no_exit_close_right_censored": n_no_exit,
        "dropped_no_bench_data": n_no_bench,
    }
    return pd.DataFrame(trades), drop_stats


def bootstrap_ci(vals: np.ndarray, stat_fn, n_boot=N_BOOTSTRAP, seed=RNG_SEED) -> tuple[float, float]:
    if len(vals) < 2:
        return (float("nan"), float("nan"))
    rng = np.random.default_rng(seed)
    n = len(vals)
    boots = np.empty(n_boot)
    for i in range(n_boot):
        sample = vals[rng.integers(0, n, n)]
        boots[i] = stat_fn(sample)
    lo, hi = np.percentile(boots, [2.5, 97.5])
    return float(lo), float(hi)


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
                "t_stat": None, "t_p": None, "wilcoxon_stat": None, "wilcoxon_p": None,
                "mean_ci95_pct": None, "median_ci95_pct": None,
            }
        )
        return out
    t_stat, t_p = stats.ttest_1samp(vals, 0)
    try:
        w_stat, w_p = stats.wilcoxon(vals)
    except ValueError:
        w_stat, w_p = None, None
    mean_lo, mean_hi = bootstrap_ci(vals, np.mean)
    med_lo, med_hi = bootstrap_ci(vals, np.median)
    out.update(
        {
            "mean_pct": round(float(np.mean(vals)) * 100, 3),
            "median_pct": round(float(np.median(vals)) * 100, 3),
            "win_rate_pct": round(float((vals > 0).mean()) * 100, 1),
            "t_stat": round(float(t_stat), 3),
            "t_p": round(float(t_p), 4),
            "wilcoxon_stat": round(float(w_stat), 3) if w_stat is not None else None,
            "wilcoxon_p": round(float(w_p), 4) if w_p is not None else None,
            "mean_ci95_pct": [round(mean_lo * 100, 3), round(mean_hi * 100, 3)],
            "median_ci95_pct": [round(med_lo * 100, 3), round(med_hi * 100, 3)],
        }
    )
    return out


def yearly_breakdown(trades: pd.DataFrame) -> list[dict]:
    if trades.empty:
        return []
    df = trades.copy()
    df["year"] = df["signal_date"].str[:4]
    out = []
    for yr, sub in df.groupby("year"):
        vals = sub["r_adj_pct"].to_numpy() / 100.0
        out.append(
            {
                "year": yr, "n": len(sub),
                "mean_pct": round(float(np.mean(vals)) * 100, 3),
                "median_pct": round(float(np.median(vals)) * 100, 3),
                "win_rate_pct": round(float((vals > 0).mean()) * 100, 1),
            }
        )
    return out


def outlier_trim_sensitivity(trades: pd.DataFrame) -> dict:
    out = {}
    for k in (3, 5):
        if len(trades) <= k:
            out[f"drop_top{k}_extreme"] = {"note": "n too small to trim"}
            continue
        df = trades.copy()
        df["abs_r"] = df["r_adj_pct"].abs()
        trimmed = df.sort_values("abs_r", ascending=False).iloc[k:]
        out[f"drop_top{k}_extreme"] = full_stats(trimmed["r_adj_pct"], f"trim_top{k}")
    return out


def old_method_comparison(trader_id: str) -> dict:
    if not OLD_EVENTS_CSV.exists():
        return {"note": f"{OLD_EVENTS_CSV} not found"}
    df = pd.read_csv(OLD_EVENTS_CSV, dtype={"securities_trader_id": str})
    sub = df[df["securities_trader_id"] == trader_id]
    return full_stats(sub["r_adj_pct"], f"old_event_level_{trader_id}_n{len(sub)}")


def run_permutation(conn, trader_id: str, trades: pd.DataFrame) -> dict:
    if trades.empty:
        return {"note": "no trades to test"}
    ix_bars = load_ix(conn)
    ix_dict = build_l1h7_signal_dict(ix_bars)
    stock_dicts = {}
    for sid in sorted(trades["stock_id"].unique()):
        stock_dicts[sid] = build_l1h7_signal_dict(load_stock_bars(conn, sid))
    result = permutation_test(
        trades[["stock_id", "signal_date"]], stock_dicts, ix_dict, n_perm=N_PERM, seed=RNG_SEED
    )
    trim = perm_outlier_trim_sensitivity(trades["r_adj_pct"].to_numpy(), trim_ns=(3, 5))
    return {
        "n_perm": N_PERM,
        "seed": RNG_SEED,
        "permutation_result": result,
        "outlier_trim_sensitivity_r_adj_pct": trim,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trader-id", required=True)
    ap.add_argument("--buy-floor", type=float, default=50_000_000.0)
    ap.add_argument("--net-min", type=float, default=0.95)
    args = ap.parse_args()
    tid = args.trader_id

    conn = connect(DEFAULT_DB_PATH)
    mega = load_mega(MEGA_PATH)
    print(f"[INFO] trader_id={tid}, buy_floor={args.buy_floor:,.0f}, net_min={args.net_min}")
    print(f"[INFO] Mega 排除清單 n={len(mega)}")

    section(f"(1) 建構 {tid} 滾動5日 buy>={args.buy_floor:,.0f} 且 net_ratio>={args.net_min} 觸發事件")
    events = build_5d_net95_events(conn, tid, mega, args.buy_floor, args.net_min)
    events_path = OUT_DIR / f"whale_{tid}_5dnet95_events.csv"
    events.to_csv(events_path, index=False)
    print(f"[OK] 寫入 {events_path}")

    section("(2) L1H7 forward return")
    trades, drop_stats = build_trades(conn, events)
    print(json.dumps(drop_stats, ensure_ascii=False, indent=2))
    trades_path = OUT_DIR / f"whale_{tid}_5dnet95_trades.csv"
    trades.to_csv(trades_path, index=False)
    print(f"[OK] 寫入 {trades_path}")

    if trades.empty:
        print(f"[WARN] {tid} 沒有任何完整trades，結束")
        summary = {"trader_id": tid, "note": "no complete trades", "n_events": len(events)}
        (OUT_DIR / f"whale_{tid}_5dnet95_summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2)
        )
        conn.close()
        return 0

    section("(3) 全樣本統計")
    full = full_stats(trades["r_adj_pct"], f"full_sample_n{len(trades)}")
    print(json.dumps(full, ensure_ascii=False, indent=2))

    section("(4) Walk-forward IS(60%)/OOS(40%)")
    start_ts = pd.Timestamp(STUDY_START)
    end_ts = pd.Timestamp(STUDY_END)
    cutoff = (start_ts + pd.Timedelta(days=round((end_ts - start_ts).days * 0.6))).strftime("%Y-%m-%d")
    is_trades = trades[trades["signal_date"] <= cutoff]
    oos_trades = trades[trades["signal_date"] > cutoff]
    is_stats = full_stats(is_trades["r_adj_pct"], f"IS_n{len(is_trades)}_<= {cutoff}")
    oos_stats = full_stats(oos_trades["r_adj_pct"], f"OOS_n{len(oos_trades)}_> {cutoff}")
    print("IS:", json.dumps(is_stats, ensure_ascii=False, indent=2))
    print("OOS:", json.dumps(oos_stats, ensure_ascii=False, indent=2))

    section("(5) 年度分解")
    yearly = yearly_breakdown(trades)
    print(json.dumps(yearly, ensure_ascii=False, indent=2))

    section("(6) 去極值敏感度")
    trim = outlier_trim_sensitivity(trades)
    print(json.dumps(trim, ensure_ascii=False, indent=2))

    section("(7) 對照組：舊版 event-level")
    old_stats = old_method_comparison(tid)
    print("舊版:", json.dumps(old_stats, ensure_ascii=False, indent=2))

    section("(8) Permutation/placebo 顯著性檢定")
    perm = run_permutation(conn, tid, trades)
    print(json.dumps(perm, ensure_ascii=False, indent=2))
    conn.close()

    summary = {
        "trader_id": tid,
        "protocol": {
            "signal": f"rolling 5-trading-day buy_5d>={args.buy_floor:,.0f} & net_ratio>={args.net_min}",
            "study_window": f"{STUDY_START}..{STUDY_END}",
            "dedup": "rising-edge",
            "l1h7": {"cost": COST, "hold": HOLD, "beta": BETA, "bench": BENCH_CODE},
            "mega_excluded_n": len(mega),
        },
        "n_raw_events_rising_edge": len(events),
        "drop_stats": drop_stats,
        "full_sample": full,
        "walk_forward": {"cutoff_date": cutoff, "in_sample": is_stats, "out_of_sample": oos_stats},
        "yearly_breakdown": yearly,
        "outlier_trim_sensitivity": trim,
        "comparison_old_method": old_stats,
        "permutation_test": perm,
    }
    summary_path = OUT_DIR / f"whale_{tid}_5dnet95_summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"\n[OK] 寫入 {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
