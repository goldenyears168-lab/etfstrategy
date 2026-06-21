#!/usr/bin/env python3
"""tw_stocker v8.5 動量 vs 00981A L1H9：勝台指率對照（IX0001）。"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TW_STOCKER = ROOT / "vendor" / "tw_stocker"
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(TW_STOCKER))

from research.backtest.copytrade_backtest import (  # noqa: E402
    bench_return_entry_to_exit,
    compute_win_rate_stats,
    resolve_strategy_specs,
    run_strategies,
)
from stock_db import connect, DEFAULT_DB_PATH  # noqa: E402


def _run_tw_stocker_momentum(
    *, days: int, start: str | None, end: str | None, hold_days: int = 9
):
    from ai_report import (  # type: ignore[import-not-found]
        EXTENDED_TICKERS,
        build_liquid_universe,
        engineer_features,
        fetch_panel_data,
    )
    from strategy.benchmark import fetch_benchmark  # type: ignore[import-not-found]
    from strategy.event_backtest import EventDrivenBacktester  # type: ignore[import-not-found]

    close_df, open_df, high_df, low_df, vol_df = fetch_panel_data(
        EXTENDED_TICKERS,
        days=days,
        start_date=start,
        end_date=end,
    )
    universe_mask = build_liquid_universe(close_df, vol_df, top_n=60)
    bench_raw = fetch_benchmark("0050", days=days, start_date=start, end_date=end)
    market_close = bench_raw * bench_raw.iloc[0] if len(bench_raw) else None
    total_score, ma_60, atr_df, _short_ma = engineer_features(
        close_df, vol_df, universe_mask, market_close=market_close
    )
    backtester = EventDrivenBacktester(
        tp_sl_mode="atr",
        tp_atr_mult=4.0,
        sl_atr_mult=3.0,
        max_hold_days=hold_days,
        initial_capital=200_000.0,
        position_size=0.10,
        regime_filter=True,
        gap_filter_atr=1.5,
        buy_cost=0.00143,
        sell_cost=0.00443,
        slippage=0.001,
    )
    trades_df, equity_df = backtester.run(
        total_score,
        close_df,
        open_df,
        high_df,
        low_df,
        ma_60,
        top_k=7,
        threshold=2.0,
        atr_df=atr_df,
        market_close=market_close,
        vol_df=vol_df,
        universe_mask=universe_mask,
    )
    return trades_df, equity_df


def _trade_vs_bench_stats(
    conn,
    trades_df,
    *,
    window_start: str | None = None,
    window_end: str | None = None,
) -> dict:
    if trades_df is None or trades_df.empty:
        return {
            "n_trades": 0,
            "win_rate_gross_pct": None,
            "win_rate_vs_bench_pct": None,
            "window_start": None,
            "window_end": None,
        }

    rows = []
    for _, row in trades_df.iterrows():
        entry = str(row["Entry_Date"])[:10]
        exit_d = str(row["Exit_Date"])[:10]
        if window_start and entry < window_start:
            continue
        if window_end and entry > window_end:
            continue
        trade_ret = float(row["Return_Pct"]) * 100.0
        bench_ret = bench_return_entry_to_exit(
            conn, entry, exit_d, entry_price_mode="open"
        )
        if bench_ret is None:
            continue
        rows.append(
            {
                "entry_date": entry,
                "exit_date": exit_d,
                "trade_ret_pct": trade_ret,
                "bench_ret_pct": bench_ret,
                "beat_bench": trade_ret > bench_ret,
                "gross_win": trade_ret > 0,
            }
        )

    if not rows:
        return {
            "n_trades": 0,
            "win_rate_gross_pct": None,
            "win_rate_vs_bench_pct": None,
            "window_start": window_start,
            "window_end": window_end,
        }

    n = len(rows)
    gross = sum(1 for r in rows if r["gross_win"])
    vs_bench = sum(1 for r in rows if r["beat_bench"])
    return {
        "n_trades": n,
        "win_rate_gross_pct": round(gross / n * 100.0, 2),
        "win_rate_vs_bench_pct": round(vs_bench / n * 100.0, 2),
        "mean_trade_ret_pct": round(sum(r["trade_ret_pct"] for r in rows) / n, 4),
        "mean_bench_ret_pct": round(sum(r["bench_ret_pct"] for r in rows) / n, 4),
        "window_start": min(r["entry_date"] for r in rows),
        "window_end": max(r["entry_date"] for r in rows),
        "rows": rows,
    }


def _l1h9_stats(conn, *, window_start: str | None, window_end: str | None) -> dict:
    specs = resolve_strategy_specs("L1H9", matrix=False, include_l0=False, max_hold=9)
    results = run_strategies(
        conn,
        "00981A",
        capital_ntd=10_000.0,
        strategies=specs,
        window_start=window_start,
        window_end=window_end,
        persist=False,
    )
    r = results[0]
    wr = compute_win_rate_stats(r.signal_days)
    complete = [d for d in r.signal_days if d.status == "complete"]
    return {
        "strategy_id": r.strategy_id,
        "n_signal_days": len(complete),
        "win_rate_gross_pct": wr["win_rate_gross_pct"],
        "win_rate_vs_bench_pct": wr["win_rate_vs_bench_pct"],
        "total_return_pct": r.total_return_pct,
        "total_bench_return_pct": r.total_bench_return_pct,
        "window_start": min(d.signal_date for d in complete) if complete else None,
        "window_end": max(d.signal_date for d in complete) if complete else None,
    }


def format_report(l1: dict, tw_full: dict, tw_overlap: dict) -> str:
    lines = [
        "# tw_stocker v8.5 動量 (H9) vs 00981A L1H9 · 勝台指率對照",
        "",
        "> 基準：IX0001（加權指數）· 同期間報酬比較",
        "> tw_stocker：最長持有 **9 交易日**（對齊 L1H9），ATR TP/SL 仍可能提前出場",
        "> L1H9：每訊號日 T+1 開盤買、持有 9 交易日收盤賣",
        "",
        "## 全樣本",
        "",
        "| 策略 | n | 勝率（毛） | **勝台指%** | 樣本期間 |",
        "|------|---|-----------|------------|----------|",
        f"| **L1H9** 跟單 | {l1['n_signal_days']} | "
        f"{l1['win_rate_gross_pct']}% | **{l1['win_rate_vs_bench_pct']}%** | "
        f"{l1['window_start']} → {l1['window_end']} |",
        f"| **tw_stocker** v8.5 動量 (H{tw_full.get('hold_days', 9)}) | {tw_full['n_trades']} | "
        f"{tw_full['win_rate_gross_pct']}% | **{tw_full['win_rate_vs_bench_pct']}%** | "
        f"{tw_full['window_start']} → {tw_full['window_end']} |",
        "",
        "## L1 重疊區間（公平對照）",
        "",
        f"區間：{l1['window_start']} → {l1['window_end']}（00981A 有訊號的可回測期）",
        "",
        "| 策略 | n | 勝台指% | 均報酬% | 均台指% |",
        "|------|---|---------|---------|---------|",
        f"| L1H9 | {l1['n_signal_days']} | **{l1['win_rate_vs_bench_pct']}%** | — | — |",
        f"| tw_stocker | {tw_overlap['n_trades']} | "
        f"**{tw_overlap['win_rate_vs_bench_pct']}%** | "
        f"{tw_overlap.get('mean_trade_ret_pct', '—')} | "
        f"{tw_overlap.get('mean_bench_ret_pct', '—')} |",
        "",
        "## 解讀",
        "",
    ]
    l1_wr = float(l1["win_rate_vs_bench_pct"] or 0)
    tw_wr = float(tw_overlap["win_rate_vs_bench_pct"] or 0)
    delta = round(tw_wr - l1_wr, 2)
    if tw_wr > l1_wr:
        winner = "tw_stocker v8.5 動量在重疊區間勝台指率較高"
    elif tw_wr < l1_wr:
        winner = "L1H9 跟單在重疊區間勝台指率較高"
    else:
        winner = "兩策略勝台指率相同"
    lines.append(f"- **結論（重疊區）**：{winner}（Δ {delta:+.2f} pp）。")
    lines.append(
        "- tw_stocker 為全市場動量選股（Top-60 流動性池、Top-7 持股）；"
        "L1 為 00981A 持股變化事件跟單，標的與頻率不同，僅供參考。"
    )
    lines.append(
        "- tw_stocker 內建 gross 勝率（交易賺錢%）≠ 勝台指%；本報告統一用 IX0001 同期比較。"
    )
    lines.append("")
    lines.append("```bash")
    lines.append("PYTHONPATH=src .venv/bin/python scripts/run_tw_stocker_vs_l1_compare.py --write-report")
    lines.append("```")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="tw_stocker vs L1H9 勝台指率對照")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--days", type=int, default=1200)
    parser.add_argument("--hold-days", type=int, default=9, help="tw_stocker 最長持有天數（預設 9 對齊 L1H9）")
    parser.add_argument("--write-report", action="store_true")
    args = parser.parse_args()

    if not TW_STOCKER.is_dir():
        print(f"缺少 {TW_STOCKER}，請先下載 voidful/tw_stocker 至 vendor/")
        return 1

    conn = connect(args.db)
    l1 = _l1h9_stats(conn, window_start=None, window_end=None)
    print(f"執行 tw_stocker v8.5 動量回測（H{args.hold_days}）…")
    trades_df, _equity = _run_tw_stocker_momentum(
        days=args.days, start=None, end=None, hold_days=args.hold_days
    )
    tw_full = _trade_vs_bench_stats(conn, trades_df)
    tw_full["hold_days"] = args.hold_days
    tw_overlap = _trade_vs_bench_stats(
        conn,
        trades_df,
        window_start=l1["window_start"],
        window_end=l1["window_end"],
    )
    conn.close()

    report = format_report(l1, tw_full, tw_overlap)
    print(report)

    if args.write_report:
        out = ROOT / "reports" / f"{date.today().strftime('%Y%m%d')}_tw_stocker_vs_l1h9.md"
        out.write_text(report, encoding="utf-8")
        print(f"已寫入 {out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
