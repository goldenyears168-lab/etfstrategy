"""FinMind strategy marketplace · cross-strategy comparison (unified contract)."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable

from analytics.bench import bench_close, bench_open
from backtest_standard_config import load_backtest_standard_config
from flow_returns import return_pct
from market_benchmark import load_benchmark_close
from research.backtest.finmind_it_cold_stove_backtest import run_it_cold_stove_backtest
from research.backtest.finmind_low_rebound_backtest import run_low_rebound_backtest
from research.backtest.finmind_ma20_breakout_backtest import run_ma20_breakout_backtest
from research.backtest.finmind_ps_growth_backtest import run_ps_growth_backtest
from research.backtest.finmind_tx_foreign_backtest import run_tx_foreign_backtest
from research.backtest.slot_backtest_summary import metrics_from_summary_payload
from stock_db import DEFAULT_DB_PATH, connect

CMP_UNIVERSE = "tw100"
CMP_N_SLOTS_SLOT = 5
OOS_START = "2024-01-01"
G2_MIN_TRADES = 30

# Longest window all five strategies can run (PS growth needs market_value from 2020-12).
WINDOW_FULL_COMMON = ("cmp_finmind_full_common", "2020-12-01", None)
WINDOW_OOS_2024 = ("cmp_finmind_oos_2024", OOS_START, None)


@dataclass(frozen=True)
class MarketplaceStrategySpec:
    rank: int
    topic_id: str
    label: str
    spec_fidelity: str
    engine: str
    hold_label: str
    runner: Callable[..., dict[str, Any]]
    runner_kwargs: dict[str, Any]


MARKETPLACE_STRATEGIES: tuple[MarketplaceStrategySpec, ...] = (
    MarketplaceStrategySpec(
        1,
        "finmind-low-rebound-foreign-it-sync",
        "低檔回升·外資投信同步買超",
        "faithful",
        "slot",
        "hold 20d",
        run_low_rebound_backtest,
        {"hold_days": 20, "n_slots": CMP_N_SLOTS_SLOT},
    ),
    MarketplaceStrategySpec(
        2,
        "finmind-low-ps-growth",
        "低市銷率成長股",
        "approx",
        "slot",
        "hold 60d",
        run_ps_growth_backtest,
        {"hold_days": 60, "n_slots": CMP_N_SLOTS_SLOT},
    ),
    MarketplaceStrategySpec(
        3,
        "finmind-tx-foreign-net-long-3d",
        "TX 外資淨多 3 日",
        "approx",
        "timing",
        "MA5 exit · max 60d",
        run_tx_foreign_backtest,
        {},
    ),
    MarketplaceStrategySpec(
        4,
        "finmind-ma20-breakout-volume",
        "突破月線長紅放量",
        "approx",
        "slot",
        "hold 10d",
        run_ma20_breakout_backtest,
        {"hold_days": 10, "n_slots": CMP_N_SLOTS_SLOT},
    ),
    MarketplaceStrategySpec(
        5,
        "finmind-it-cold-stove",
        "投信燒冷灶（簡化）",
        "simplified",
        "slot",
        "SL10/TP15 · max 40d",
        run_it_cold_stove_backtest,
        {"n_slots": CMP_N_SLOTS_SLOT},
    ),
)


def bench_interval_return_pct(
    conn: sqlite3.Connection,
    date_start: str,
    date_end: str,
) -> float | None:
    bench = load_benchmark_close(conn)
    dates = [d for d in bench.index.astype(str).tolist() if date_start <= d <= date_end]
    if len(dates) < 2:
        return None
    b0 = bench_open(conn, dates[0])
    b1 = bench_close(conn, dates[-1])
    if b0 is None or b1 is None or b0 <= 0:
        return None
    return round(return_pct(b0, b1), 4)


def run_strategy_on_window(
    spec: MarketplaceStrategySpec,
    *,
    date_start: str,
    date_end: str | None,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    end = date_end or date.today().isoformat()
    kwargs: dict[str, Any] = {
        "db_path": db_path,
        "date_start": date_start,
        "date_end": end,
        "include_validation": False,
        **spec.runner_kwargs,
    }
    if spec.engine == "slot":
        kwargs["universe"] = CMP_UNIVERSE
    payload = spec.runner(**kwargs)
    metrics = metrics_from_summary_payload(payload)
    summary = payload.get("summary") or {}
    pm = payload.get("portfolio_metrics") or {}

    conn = connect(Path(db_path) if db_path else DEFAULT_DB_PATH)
    try:
        bench_ret = bench_interval_return_pct(conn, date_start, end)
    finally:
        conn.close()

    total_ret = metrics.get("total_return_pct") or summary.get("total_return_pct")
    interval_excess = None
    if total_ret is not None and bench_ret is not None:
        interval_excess = round(float(total_ret) - float(bench_ret), 4)

    oos = payload.get("oos_from_2024") or {}
    n_periods = int(summary.get("n_periods") or metrics.get("n_periods") or 0)

    return {
        "rank_seed": spec.rank,
        "topic_id": spec.topic_id,
        "label": spec.label,
        "spec_fidelity": spec.spec_fidelity,
        "engine": spec.engine,
        "hold_label": spec.hold_label,
        "universe": CMP_UNIVERSE,
        "date_start": date_start,
        "date_end": end,
        "n_slots": payload.get("n_slots"),
        "total_capital_ntd": payload.get("total_capital_ntd"),
        "n_periods": n_periods,
        "win_rate_vs_bench_pct": summary.get("win_rate_vs_bench_pct"),
        "mean_excess_pct": summary.get("mean_excess_pct"),
        "total_return_pct": total_ret,
        "bench_interval_return_pct": bench_ret,
        "interval_excess_pct": interval_excess,
        "sharpe_ratio": pm.get("sharpe_ratio") or summary.get("sharpe_ratio"),
        "max_drawdown_pct": pm.get("max_drawdown_pct") or summary.get("max_drawdown_pct"),
        "oos_n_periods": oos.get("n_periods"),
        "oos_mean_excess_pct": oos.get("mean_excess_pct"),
        "g2_pass": (
            date_start >= OOS_START
            and n_periods >= G2_MIN_TRADES
            and interval_excess is not None
            and interval_excess > 0
        ),
    }


def build_comparison_table(
    *,
    window_id: str,
    date_start: str,
    date_end: str | None = None,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    std = load_backtest_standard_config()
    rows: list[dict[str, Any]] = []
    for spec in MARKETPLACE_STRATEGIES:
        rows.append(
            run_strategy_on_window(
                spec,
                date_start=date_start,
                date_end=date_end,
                db_path=db_path,
            )
        )

    rows.sort(
        key=lambda r: (
            r.get("interval_excess_pct") is None,
            -(r.get("interval_excess_pct") or -999),
        ),
    )
    for i, row in enumerate(rows, start=1):
        row["rank"] = i

    end = date_end or date.today().isoformat()
    return {
        "schema_version": "finmind_marketplace_comparison-v1",
        "window_id": window_id,
        "date_start": date_start,
        "date_end": end,
        "universe": CMP_UNIVERSE,
        "comparison_contract": {
            "notional_ssot": "config/backtest_standard.yaml#comparison_notional_ntd",
            "total_capital_ntd": std.comparison_notional_ntd,
            "benchmark": std.benchmark,
            "entry_price_mode": "open",
            "cost_model": std.cost_model,
            "ranking_metric": "interval_excess_pct",
            "diagnostic_metrics": ["mean_excess_pct", "win_rate_vs_bench_pct", "n_periods"],
            "g2_gate": f"OOS window · n>={G2_MIN_TRADES} · interval_excess_pct>0",
        },
        "rows": rows,
        "generated_at": date.today().isoformat(),
    }


def render_comparison_markdown(tables: list[dict[str, Any]]) -> str:
    std_note = tables[0]["comparison_contract"] if tables else {}
    lines = [
        "# FinMind 市集策略 · 橫向對比（統一合約）",
        "",
        "## 比較合約（全表一致）",
        "",
        f"- **universe**: `{CMP_UNIVERSE}`",
        f"- **本金 SSOT**: {std_note.get('notional_ssot', 'config/backtest_standard.yaml')}"
        f" · **{std_note.get('total_capital_ntd', 100000):,.0f} NTD**",
        f"- **基準**: `{std_note.get('benchmark', 'IX0001')}` · 區間 buy-and-hold",
        f"- **進場**: T+1 open · **槽位**: slot 策略 5 槽 · timing 策略 1 倉",
        f"- **成本**: buy/sell fee {std_note.get('cost_model', {}).get('buy_fee_pct', '—')}%"
        f" · sell tax {std_note.get('cost_model', {}).get('sell_tax_pct', '—')}%",
        f"- **排名主指標**: `{std_note.get('ranking_metric', 'interval_excess_pct')}`"
        f"（策略區間總報酬 − 基準區間總報酬）",
        f"- **診斷欄**（不排名）: 每筆均超額 · 勝率 vs 台指",
        "",
        "> 各策略**持有/出場規則**依 preregistered spec 不同（見 hold 欄）· 僅在相同窗口與資金假設下比較。",
        "",
    ]

    for table in tables:
        wid = table["window_id"]
        lines.extend(
            [
                f"## 窗口 · `{wid}`",
                "",
                f"**{table['date_start']}** ～ **{table['date_end']}**",
                "",
                "| # | 策略 | fidelity | 引擎 | 出場 | n | 區間總報酬% | 基準% | **區間超額%** | Sharpe | MaxDD% | G2 |",
                "|---|------|----------|------|------|---|-------------|-------|---------------|--------|--------|-----|",
            ]
        )
        for row in table["rows"]:
            g2 = "✓" if row.get("g2_pass") else "—"
            lines.append(
                f"| {row['rank']} | {row['label']} | {row['spec_fidelity']} | {row['engine']} | "
                f"{row['hold_label']} | {row.get('n_periods', 0)} | "
                f"{row.get('total_return_pct', '—')} | {row.get('bench_interval_return_pct', '—')} | "
                f"**{row.get('interval_excess_pct', '—')}** | "
                f"{row.get('sharpe_ratio', '—')} | {row.get('max_drawdown_pct', '—')} | {g2} |"
            )
        lines.extend(
            [
                "",
                "### 診斷（逐筆均超額 · 不可跨策略排名）",
                "",
                "| 策略 | 每筆均超額% | 勝率 vs 台指% |",
                "|------|-------------|---------------|",
            ]
        )
        for row in table["rows"]:
            lines.append(
                f"| {row['label']} | {row.get('mean_excess_pct', '—')} | "
                f"{row.get('win_rate_vs_bench_pct', '—')} |"
            )
        lines.append("")

    lines.extend(
        [
            "## 解讀備註",
            "",
            "- **#2 低 P/S** 資料自 2020-12 起 · 全窗對比以此為起點。",
            "- **#3 期貨淨多** 為 single-position timing · IX0001 proxy · 與槽位策略引擎不同。",
            "- **#5 投信燒冷灶** 為 simplified（缺 E2 持股濾網）。",
            "- G2 僅在 OOS 窗口（2024+）且 n≥30 時標記。",
            "",
        ]
    )
    return "\n".join(lines)


def run_marketplace_comparison(
    *,
    db_path: str | Path | None = None,
    out_dir: Path | None = None,
) -> dict[str, Any]:
    out = out_dir or Path("reports/research/finmind-marketplace")
    out.mkdir(parents=True, exist_ok=True)

    tables = [
        build_comparison_table(
            window_id=WINDOW_FULL_COMMON[0],
            date_start=WINDOW_FULL_COMMON[1],
            date_end=WINDOW_FULL_COMMON[2],
            db_path=db_path,
        ),
        build_comparison_table(
            window_id=WINDOW_OOS_2024[0],
            date_start=WINDOW_OOS_2024[1],
            date_end=WINDOW_OOS_2024[2],
            db_path=db_path,
        ),
    ]

    stamp = date.today().strftime("%Y%m%d")
    json_path = out / f"finmind_marketplace_comparison_{stamp}.json"
    md_path = out / f"finmind_marketplace_comparison_{stamp}.md"
    payload = {
        "schema_version": "finmind_marketplace_comparison-v1",
        "windows": tables,
        "generated_at": date.today().isoformat(),
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_comparison_markdown(tables), encoding="utf-8")

    return {"json_path": json_path, "md_path": md_path, "windows": tables}
