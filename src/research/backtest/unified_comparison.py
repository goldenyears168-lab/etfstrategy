"""Cross-track comparison metrics and league table (comparison layer only)."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from backtest_standard_config import BacktestStandardConfig, load_backtest_standard_config
from research.backtest.finpilot_local_backtest import load_price_panels
from research.backtest.unified_nav_adapter import build_all_nav, resolve_window_trade_range
from stock_db import DEFAULT_DB_PATH, connect

_TPE = ZoneInfo("Asia/Taipei")


def _interval_metrics(
    nav_rows: list[dict[str, Any]],
    *,
    total_capital: float,
    min_days_for_cagr: int,
) -> dict[str, Any]:
    if not nav_rows:
        return {}
    eq1 = float(nav_rows[-1]["equity_ntd"])
    b0 = nav_rows[0].get("benchmark_equity_ntd")
    b1 = nav_rows[-1].get("benchmark_equity_ntd")
    n_days = len(nav_rows)
    total_return_pct = (eq1 / total_capital - 1.0) * 100.0 if total_capital > 0 else 0.0
    bench_return_pct = None
    interval_excess_pct = None
    if b0 and b1 and float(b0) > 0:
        bench_return_pct = (float(b1) / float(b0) - 1.0) * 100.0
        interval_excess_pct = total_return_pct - bench_return_pct

    daily_rets: list[float] = []
    for i in range(1, len(nav_rows)):
        prev = float(nav_rows[i - 1]["equity_ntd"])
        cur = float(nav_rows[i]["equity_ntd"])
        if prev > 0:
            daily_rets.append((cur / prev - 1.0) * 100.0)

    import math

    sharpe = None
    ann_vol = None
    if len(daily_rets) > 1:
        mean_dr = sum(daily_rets) / len(daily_rets)
        var = sum((x - mean_dr) ** 2 for x in daily_rets) / (len(daily_rets) - 1)
        std_dr = math.sqrt(var)
        ann_vol = std_dr * math.sqrt(252.0)
        sharpe = (mean_dr / std_dr * math.sqrt(252.0)) if std_dr > 0 else None

    cagr_pct = None
    if n_days >= min_days_for_cagr and total_capital > 0 and eq1 > 0:
        years = n_days / 252.0
        cagr_pct = ((eq1 / total_capital) ** (1.0 / years) - 1.0) * 100.0

    peak = total_capital
    max_dd = 0.0
    for row in nav_rows:
        eq = float(row["equity_ntd"])
        peak = max(peak, eq)
        if peak > 0:
            max_dd = max(max_dd, (peak - eq) / peak)

    calmar = None
    if cagr_pct is not None and max_dd > 0:
        calmar = cagr_pct / (max_dd * 100.0)

    monthly_wins = 0
    monthly_n = 0
    by_month: dict[str, list[float]] = {}
    bench_by_month: dict[str, list[float]] = {}
    for i in range(1, len(nav_rows)):
        d = str(nav_rows[i]["date"])
        ym = d[:7]
        prev = float(nav_rows[i - 1]["equity_ntd"])
        cur = float(nav_rows[i]["equity_ntd"])
        if prev <= 0:
            continue
        by_month.setdefault(ym, []).append((cur / prev - 1.0) * 100.0)
        bp = nav_rows[i - 1].get("benchmark_equity_ntd")
        bc = nav_rows[i].get("benchmark_equity_ntd")
        if bp and bc and float(bp) > 0:
            bench_by_month.setdefault(ym, []).append((float(bc) / float(bp) - 1.0) * 100.0)

    for ym, rets in by_month.items():
        if not rets:
            continue
        strat_m = (1.0 + sum(rets) / 100.0) - 1.0
        bench_rets = bench_by_month.get(ym) or []
        if not bench_rets:
            continue
        bench_m = (1.0 + sum(bench_rets) / 100.0) - 1.0
        monthly_n += 1
        if strat_m > bench_m:
            monthly_wins += 1

    win_rate_monthly = (
        round(monthly_wins / monthly_n * 100.0, 2) if monthly_n > 0 else None
    )

    return {
        "total_return_pct": round(total_return_pct, 4),
        "benchmark_return_pct": round(bench_return_pct, 4) if bench_return_pct is not None else None,
        "interval_excess_pct": round(interval_excess_pct, 4) if interval_excess_pct is not None else None,
        "cagr_pct": round(cagr_pct, 4) if cagr_pct is not None else None,
        "ann_vol_pct": round(ann_vol, 4) if ann_vol is not None else None,
        "sharpe_ratio": round(sharpe, 4) if sharpe is not None else None,
        "max_drawdown_pct": round(max_dd * 100.0, 4),
        "calmar_ratio": round(calmar, 4) if calmar is not None else None,
        "win_rate_vs_bench_monthly_pct": win_rate_monthly,
        "n_trading_days": n_days,
        "sample_sufficient_for_cagr": n_days >= min_days_for_cagr,
    }


def build_league_table(
    nav_by_strategy: dict[str, dict[str, Any]],
    *,
    window_id: str,
    window_label: str,
    cfg: BacktestStandardConfig,
) -> dict[str, Any]:
    min_cagr = cfg.min_trading_days_for_annualization
    notional = cfg.comparison_notional_ntd
    rows: list[dict[str, Any]] = []
    for sid, payload in nav_by_strategy.items():
        merged = payload.get("daily_nav_merged") or []
        adapter = cfg.track_adapters[sid]
        m = _interval_metrics(
            merged,
            total_capital=notional,
            min_days_for_cagr=min_cagr,
        )
        m["strategy_id"] = sid
        m["comparison_notional_ntd"] = notional
        m["capital_per_slot_ntd"] = round(notional / max(1, adapter.n_slots), 2)
        m["n_slots"] = adapter.n_slots
        m["n_trades"] = payload.get("n_trades")
        m["diagnostic_note"] = "interval_excess_pct 為排名欄 · 契約版 mean_excess 見各軌 JSON"
        rows.append(m)

    rows.sort(
        key=lambda r: (r.get("interval_excess_pct") is not None, r.get("interval_excess_pct") or -999),
        reverse=True,
    )
    for i, row in enumerate(rows, start=1):
        row["rank"] = i

    return {
        "window_id": window_id,
        "window_label": window_label,
        "excess_definition": cfg.excess_definition,
        "cost_model_enabled": bool(cfg.cost_model.get("enabled")),
        "comparison_notional_ntd": cfg.comparison_notional_ntd,
        "computed_at": datetime.now(_TPE).isoformat(timespec="seconds"),
        "disclaimer": "比較限定版 · no-compound slot M2M · 非操作建議 · 契約版見 strategy.yaml",
        "rows": rows,
    }


def render_league_markdown(table: dict[str, Any]) -> str:
    lines = [
        f"# 標準化比較 · {table['window_label']}",
        "",
        f"- window_id: `{table['window_id']}`",
        f"- excess: `{table['excess_definition']}`",
        f"- cost_model: `{table['cost_model_enabled']}`",
        f"- comparison_notional: `{table.get('comparison_notional_ntd', 0):,.0f}` NTD（全軌一致 · 排名看 %）",
        f"- computed_at: {table['computed_at']}",
        "",
        f"> {table['disclaimer']}",
        "",
        "| rank | strategy | interval_excess% | total_return% | sharpe | maxDD% | n_days | cagr% |",
        "|------|----------|------------------|---------------|--------|--------|--------|-------|",
    ]
    for row in table["rows"]:
        cagr = row.get("cagr_pct")
        cagr_s = f"{cagr:.2f}" if cagr is not None else "—"
        lines.append(
            f"| {row['rank']} | `{row['strategy_id']}` | "
            f"{row.get('interval_excess_pct', '—')} | {row.get('total_return_pct', '—')} | "
            f"{row.get('sharpe_ratio', '—')} | {row.get('max_drawdown_pct', '—')} | "
            f"{row.get('n_trading_days', '—')} | {cagr_s} |"
        )
    lines.append("")
    return "\n".join(lines)


def run_unified_comparison(
    *,
    window_id: str = "cmp_max_common",
    cfg: BacktestStandardConfig | None = None,
) -> dict[str, Any]:
    cfg = cfg or load_backtest_standard_config()
    win = cfg.windows.get(window_id) or {}
    nav_by_strategy = build_all_nav(cfg=cfg, window_id=window_id)
    table = build_league_table(
        nav_by_strategy,
        window_id=window_id,
        window_label=str(win.get("label") or window_id),
        cfg=cfg,
    )
    out_dir = Path(cfg.output.get("nav_csv_dir") or "reports/research/_unified/")
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"league_table_{window_id}.json"
    md_path = out_dir / f"league_table_{window_id}.md"
    json_path.write_text(json.dumps(table, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(render_league_markdown(table), encoding="utf-8")
    return {"table": table, "json_path": str(json_path), "md_path": str(md_path)}
