#!/usr/bin/env python3
"""Dayflip-short T0 WMA20 bounce-confirm predictive check（Research · Book-only）。

Item AU (100項創意組合計畫 wave 10). Question: 對 dayflip-futures-short 的 190 筆
already-reconstructed 交易 (`reports/research/dayflip_revenue_momentum_filter/
trades_with_revyoy.csv`)，訊號日(T0, 分點買超日/隔日跳空放空的判斷基準日)當天股價
是否出現 WMA20 反彈確認(and_persist_and_buffer, edge-triggered, 5分K)型態，
是否能預測隔日(trade_date)放空的 pnl_pct？

完全重用 `run_2327_wma20_bounce_confirm_study.py` 的 `load_close_bars` /
`build_indicators`（不重新實作訊號邏輯），只是把「進場後前向報酬」換成
「當天(T0)是否出現過 edge=True」這個 0/1 旗標，再去對照隔日 dayflip-short 的
已知 pnl_pct。

Entry-only 型探索性研究、read-only DB，不寫回 config/order.yaml 或 strategy 層。
"""

from __future__ import annotations

import json
import random
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts" / "research"))

from report_paths import REPORTS_RESEARCH  # noqa: E402
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402

from run_2327_wma20_bounce_confirm_study import build_indicators, load_close_bars  # noqa: E402

TRADES_CSV = ROOT / "reports/research/dayflip_revenue_momentum_filter/trades_with_revyoy.csv"
OUT_DIR = REPORTS_RESEARCH / "dayflip_wma20_shortside_filter"
SIGNAL_COL = "and_persist_and_buffer"
LOOKBACK_CALENDAR_DAYS = 90  # WMA20(5分K) warmup 遠小於此，多留餘裕跨假期
N_PERM = 5000
SEED = 20260808


def load_trades() -> pd.DataFrame:
    df = pd.read_csv(TRADES_CSV, dtype={"stock": str})
    df["signal_date"] = df["signal_date"].astype(str)
    df["trade_date"] = df["trade_date"].astype(str)
    return df


def t0_confirm_flags(conn, stock_ids: list[str], signal_dates_by_stock: dict[str, list[str]]) -> dict[tuple[str, str], str]:
    """回傳 {(stock_id, signal_date): 'confirm'|'no_confirm'|'no_5m_data'}。"""
    flags: dict[tuple[str, str], str] = {}
    for stock_id in stock_ids:
        dates = signal_dates_by_stock[stock_id]
        start = (pd.Timestamp(min(dates)) - pd.Timedelta(days=LOOKBACK_CALENDAR_DAYS)).strftime("%Y-%m-%d")
        end = max(dates)
        bars = load_close_bars(conn, stock_id, start=start, end=end)
        if bars.empty:
            for d in dates:
                flags[(stock_id, d)] = "no_5m_data"
            continue
        ind = build_indicators(bars)
        confirm_by_date = ind.groupby("trade_date")[SIGNAL_COL].any()
        covered_dates = set(ind["trade_date"].unique())
        for d in dates:
            if d not in covered_dates:
                flags[(stock_id, d)] = "no_5m_data"
            else:
                flags[(stock_id, d)] = "confirm" if bool(confirm_by_date.loc[d]) else "no_confirm"
    return flags


def perm_test_mean_diff(a: list[float], b: list[float], *, n_perm: int, seed: int) -> tuple[float, float]:
    """Two-sided permutation test on difference of means (a - b). Returns (obs_diff, p_value)."""
    obs = (sum(a) / len(a)) - (sum(b) / len(b))
    pooled = a + b
    na = len(a)
    rng = random.Random(seed)
    count = 0
    for _ in range(n_perm):
        rng.shuffle(pooled)
        pa = pooled[:na]
        pb = pooled[na:]
        diff = (sum(pa) / len(pa)) - (sum(pb) / len(pb))
        if abs(diff) >= abs(obs):
            count += 1
    p = (count + 1) / (n_perm + 1)
    return obs, p


def perm_test_winrate_diff(a: list[float], b: list[float], *, n_perm: int, seed: int) -> tuple[float, float]:
    wa = sum(1 for v in a if v > 0) / len(a)
    wb = sum(1 for v in b if v > 0) / len(b)
    obs = wa - wb
    labels_a = [1 if v > 0 else 0 for v in a]
    labels_b = [1 if v > 0 else 0 for v in b]
    pooled = labels_a + labels_b
    na = len(a)
    rng = random.Random(seed + 1)
    count = 0
    for _ in range(n_perm):
        rng.shuffle(pooled)
        pa = pooled[:na]
        pb = pooled[na:]
        diff = (sum(pa) / len(pa)) - (sum(pb) / len(pb))
        if abs(diff) >= abs(obs):
            count += 1
    p = (count + 1) / (n_perm + 1)
    return obs, p


def median(vals: list[float]) -> float:
    s = sorted(vals)
    n = len(s)
    mid = n // 2
    if n % 2 == 1:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2.0


def main() -> int:
    conn = connect(DEFAULT_DB_PATH)
    try:
        trades = load_trades()
        stock_ids = sorted(trades["stock"].unique())
        signal_dates_by_stock: dict[str, list[str]] = {
            sid: sorted(trades.loc[trades["stock"] == sid, "signal_date"].unique().tolist())
            for sid in stock_ids
        }
        flags = t0_confirm_flags(conn, stock_ids, signal_dates_by_stock)
    finally:
        conn.close()

    trades["t0_wma20_flag"] = [
        flags[(row.stock, row.signal_date)] for row in trades.itertuples()
    ]

    n_total = len(trades)
    n_missing = int((trades["t0_wma20_flag"] == "no_5m_data").sum())
    usable = trades[trades["t0_wma20_flag"] != "no_5m_data"].copy()

    confirm = usable[usable["t0_wma20_flag"] == "confirm"]
    no_confirm = usable[usable["t0_wma20_flag"] == "no_confirm"]

    a = confirm["pnl_pct"].tolist()
    b = no_confirm["pnl_pct"].tolist()

    result: dict = {
        "schema": "dayflip_wma20_t0_shortside_filter-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "signal_reused_from": "scripts/research/run_2327_wma20_bounce_confirm_study.py::build_indicators (and_persist_and_buffer, edge-triggered)",
        "trades_source": str(TRADES_CSV.relative_to(ROOT)),
        "n_trades_total": n_total,
        "n_stocks": len(stock_ids),
        "n_missing_5m_data": n_missing,
        "n_usable": len(usable),
        "buckets": {
            "t0_confirm": {
                "n": len(a),
                "mean_pnl_pct": round(sum(a) / len(a), 4) if a else None,
                "median_pnl_pct": round(median(a), 4) if a else None,
                "win_rate_pct": round(100.0 * sum(1 for v in a if v > 0) / len(a), 2) if a else None,
                "n_stocks": int(confirm["stock"].nunique()) if len(a) else 0,
            },
            "t0_no_confirm": {
                "n": len(b),
                "mean_pnl_pct": round(sum(b) / len(b), 4) if b else None,
                "median_pnl_pct": round(median(b), 4) if b else None,
                "win_rate_pct": round(100.0 * sum(1 for v in b if v > 0) / len(b), 2) if b else None,
                "n_stocks": int(no_confirm["stock"].nunique()) if len(b) else 0,
            },
        },
    }

    if a and b:
        mean_diff_obs, mean_diff_p = perm_test_mean_diff(a[:], b[:], n_perm=N_PERM, seed=SEED)
        wr_diff_obs, wr_diff_p = perm_test_winrate_diff(a[:], b[:], n_perm=N_PERM, seed=SEED)
        result["permutation_test"] = {
            "n_perm": N_PERM,
            "seed": SEED,
            "mean_pnl_diff_confirm_minus_noconfirm": round(mean_diff_obs, 4),
            "mean_pnl_diff_p_value": round(mean_diff_p, 4),
            "win_rate_diff_pp_confirm_minus_noconfirm": round(wr_diff_obs * 100.0, 2),
            "win_rate_diff_p_value": round(wr_diff_p, 4),
        }

    # Stock-level robustness: per-stock mean pnl for confirm vs no_confirm, only
    # for stocks that have >=1 trade in EACH bucket (paired-ish view).
    stock_level_rows = []
    for sid, grp in usable.groupby("stock"):
        ga = grp.loc[grp["t0_wma20_flag"] == "confirm", "pnl_pct"]
        gb = grp.loc[grp["t0_wma20_flag"] == "no_confirm", "pnl_pct"]
        if len(ga) and len(gb):
            stock_level_rows.append(
                {
                    "stock": sid,
                    "n_confirm": int(len(ga)),
                    "n_no_confirm": int(len(gb)),
                    "mean_confirm": round(float(ga.mean()), 4),
                    "mean_no_confirm": round(float(gb.mean()), 4),
                    "diff": round(float(ga.mean() - gb.mean()), 4),
                }
            )
    result["stock_level_paired"] = {
        "n_stocks_with_both_buckets": len(stock_level_rows),
        "rows": stock_level_rows,
    }
    if stock_level_rows:
        diffs = [r["diff"] for r in stock_level_rows]
        n_pos = sum(1 for d in diffs if d > 0)
        result["stock_level_paired"]["mean_diff"] = round(sum(diffs) / len(diffs), 4)
        result["stock_level_paired"]["n_positive_diff"] = n_pos
        result["stock_level_paired"]["n_negative_diff"] = len(diffs) - n_pos

    # Flag distribution / confirm rate
    result["flag_distribution"] = trades["t0_wma20_flag"].value_counts().to_dict()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d")
    out_json = OUT_DIR / f"{stamp}_dayflip_wma20_t0_shortside_filter.json"
    out_json.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    usable.to_csv(OUT_DIR / f"{stamp}_trades_with_t0_flag.csv", index=False)

    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"\nwrote {out_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
