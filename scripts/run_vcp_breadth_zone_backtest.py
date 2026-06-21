#!/usr/bin/env python3
"""VCP Pivot Gate vs Coil Close × 200MA breadth zones · 2025 / 2026."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from research.backtest.chunge_funnel_backtest import (  # noqa: E402
    VCP_COIL_CLOSE,
    VCP_PIVOT_GATE,
    render_vcp_breadth_dual_markdown,
    run_vcp_breadth_zone_comparison,
)
from research.backtest.finpilot_local_backtest import load_price_panels  # noqa: E402
from research.backtest.rrg_mono_backtest import run_breadth_zone_comparison  # noqa: E402
from research.backtest.slot_portfolio_metrics import (  # noqa: E402
    portfolio_metrics_for_periods,
    render_portfolio_breadth_markdown,
    render_portfolio_zone_summary,
    render_statistical_support_md,
    render_zone_centric_portfolio_markdown,
)
from market_breadth_ma import BREADTH_ZONE_ZH, BREADTH_ZONES_ORDER, build_breadth_panel  # noqa: E402
from report_paths import RESEARCH_VCP  # noqa: E402
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402

TOTAL_CAPITAL_NTD = 50_000.0
STRATEGY_SLOTS = {
    "pivot_gate": VCP_PIVOT_GATE["n_slots"],
    "coil_close": VCP_COIL_CLOSE["n_slots"],
    "rrg": 3,
}
STRATEGY_LABELS = {
    "pivot_gate": "Pivot Gate",
    "coil_close": "Coil Close",
    "rrg": "RRG hold7",
}

YEAR_WINDOWS: dict[str, tuple[str, str]] = {
    "2023": ("2023-01-01", "2023-12-31"),
    "2024": ("2024-01-01", "2024-12-31"),
    "2025": ("2025-01-01", "2025-12-31"),
    "2026": ("2026-01-01", "2026-12-31"),
}

POOLED_WINDOWS: dict[str, tuple[str, str]] = {
    "2023-2026": ("2023-01-01", "2026-12-31"),
    "2024-2026": ("2024-01-01", "2026-12-31"),
    "breadth_valid": ("2024-11-01", "2026-12-31"),
}

BREADTH_VALID_FROM = "2024-11-01"
MIN_N_ZONE_CREDIBLE = 10
MIN_N_ALL_CREDIBLE = 30


def _portfolio_for_results(
    conn,
    close,
    trade_dates: list[str],
    *,
    pg: dict,
    cc: dict,
    rrg: dict | None,
) -> dict[str, dict[str, dict]]:
    out: dict[str, dict[str, dict]] = {
        "pivot_gate": {},
        "coil_close": {},
    }
    zone_keys = list(BREADTH_ZONES_ORDER) + ["ALL"]
    for zone in zone_keys:
        if zone == "ALL":
            pg_p = pg["pooled_all"]["periods"]
            cc_p = cc["pooled_all"]["periods"]
            rr_p = rrg["pooled_all"]["periods"] if rrg else []
        else:
            pg_p = pg["by_zone"][zone]["periods"]
            cc_p = cc["by_zone"][zone]["periods"]
            rr_p = rrg["by_zone"][zone]["periods"] if rrg else []
        out["pivot_gate"][zone] = portfolio_metrics_for_periods(
            conn, pg_p, trade_dates,
            total_capital=TOTAL_CAPITAL_NTD,
            n_slots=STRATEGY_SLOTS["pivot_gate"],
            close=close,
        )
        out["coil_close"][zone] = portfolio_metrics_for_periods(
            conn, cc_p, trade_dates,
            total_capital=TOTAL_CAPITAL_NTD,
            n_slots=STRATEGY_SLOTS["coil_close"],
            close=close,
        )
        if rrg:
            out.setdefault("rrg", {})[zone] = portfolio_metrics_for_periods(
                conn, rr_p, trade_dates,
                total_capital=TOTAL_CAPITAL_NTD,
                n_slots=STRATEGY_SLOTS["rrg"],
                close=close,
            )
    return out


def _breadth_data_notes(conn, *, years: list[str]) -> str:
    lines = [
        "## 資料與廣度可用性",
        "",
        "- **廣度 zone** 需 universe ≥40 檔具 MA200；目前面板約 **135 檔**，",
        f"  可靠區間自 **{BREADTH_VALID_FROM}** 起（約 129 檔有效）。",
        "- **2023**：可跑策略回測，但 **無可靠 breadth zone 分類**（早期 universe 過小）。",
        "- **2024-01～10**：策略可跑；breadth zone 僅 **11–12 月起** 可信。",
        "- **IX0001（TEJ）**：自 2024-01-02 起；2023 篩選用 FinMind 個股、基準可能缺 TEJ。",
        "",
    ]
    panel = build_breadth_panel(conn, date_start="2023-01-01", date_end="2026-12-31")
    if not panel.empty:
        lines.append(f"- 合併廣度面板：{panel['trade_date'].min()} → {panel['trade_date'].max()}（{len(panel)} 日）")
    lines.append("")
    for year in years:
        if year not in YEAR_WINDOWS:
            continue
        ds, de = YEAR_WINDOWS[year]
        p = build_breadth_panel(conn, date_start=ds, date_end=de)
        lines.append(f"- **{year}** breadth 有效日：{len(p)}")
    lines.append("")
    return "\n".join(lines)


def _run_window(
    conn,
    close,
    label: str,
    date_start: str,
    date_end: str,
    *,
    with_rrg: bool,
) -> tuple[dict, dict, dict | None, dict, list[str], dict[str, int]]:
    trade_dates = [
        d for d in close.index.astype(str).tolist() if date_start <= d <= date_end
    ]
    print(f"Running {label} ({date_start}..{date_end})...")
    pg = run_vcp_breadth_zone_comparison(
        conn, spec=VCP_PIVOT_GATE, date_start=date_start, date_end=date_end
    )
    cc = run_vcp_breadth_zone_comparison(
        conn, spec=VCP_COIL_CLOSE, date_start=date_start, date_end=date_end
    )
    rrg = (
        run_breadth_zone_comparison(conn, date_start=date_start, date_end=date_end)
        if with_rrg
        else None
    )
    portfolio = _portfolio_for_results(conn, close, trade_dates, pg=pg, cc=cc, rrg=rrg)
    return pg, cc, rrg, portfolio, trade_dates, pg["zone_day_counts"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Pivot Gate vs Coil Close × 200MA breadth zone · by year"
    )
    parser.add_argument(
        "--years",
        default="2023,2024,2025,2026",
        help="Comma-separated years (default 2023,2024,2025,2026)",
    )
    parser.add_argument(
        "--pooled",
        default="2023-2026,breadth_valid",
        help="Comma-separated pooled windows: 2023-2026, 2024-2026, breadth_valid",
    )
    parser.add_argument("--with-rrg", action="store_true", help="Include RRG mono hold7 column")
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args(argv)

    years = [y.strip() for y in args.years.split(",") if y.strip()]
    pooled_labels = [p.strip() for p in args.pooled.split(",") if p.strip()]
    sections: list[str] = [
        "# VCP Pivot Gate vs Coil Close × 200MA Breadth",
        "",
        f"組合本金：**{TOTAL_CAPITAL_NTD:,.0f} NTD**（三策略同本金 · 按槽數等額部署）",
        "",
    ]
    payload: dict = {"total_capital_ntd": TOTAL_CAPITAL_NTD, "years": {}, "pooled": {}}
    portfolio_by_year: dict[str, dict[str, dict[str, dict]]] = {}
    zone_days_by_year: dict[str, dict[str, int]] = {}
    portfolio_pooled: dict[str, dict[str, dict[str, dict]]] = {}
    zone_days_pooled: dict[str, dict[str, int]] = {}

    conn = connect(DEFAULT_DB_PATH)
    close, _, _ = load_price_panels(conn)
    sections.append(_breadth_data_notes(conn, years=years))

    strategy_keys = tuple(k for k in STRATEGY_LABELS if k != "rrg" or args.with_rrg)

    for pool_label in pooled_labels:
        if pool_label not in POOLED_WINDOWS:
            print(
                f"Unknown pooled window {pool_label}; supported: {list(POOLED_WINDOWS)}",
                file=sys.stderr,
            )
            return 1
        ds, de = POOLED_WINDOWS[pool_label]
        pg, cc, rrg, portfolio, _, zone_days = _run_window(
            conn, close, f"pooled {pool_label}", ds, de, with_rrg=args.with_rrg
        )
        portfolio_pooled[pool_label] = portfolio
        zone_days_pooled[pool_label] = zone_days
        payload["pooled"][pool_label] = {
            "date_start": ds,
            "date_end": de,
            "pivot_gate": pg["pooled_all"]["summary"],
            "coil_close": cc["pooled_all"]["summary"],
            "portfolio": portfolio,
            "zone_day_counts": zone_days,
        }
        if rrg:
            payload["pooled"][pool_label]["rrg_mono_hold7"] = rrg["pooled_all"]["summary"]

    if portfolio_pooled:
        sections.append(
            render_statistical_support_md(
                portfolio_by_label=portfolio_pooled,
                zone_days_by_label=zone_days_pooled,
                labels=pooled_labels,
                strategy_keys=strategy_keys,
                strategy_labels=STRATEGY_LABELS,
                min_n_zone=MIN_N_ZONE_CREDIBLE,
                min_n_all=MIN_N_ALL_CREDIBLE,
            )
        )
        primary = pooled_labels[0]
        ds, de = POOLED_WINDOWS[primary]
        sections.append(
            f"## 合併區間 `{primary}`（{ds}～{de}）· 組合摘要\n\n"
            + render_portfolio_zone_summary(
                year_label=primary,
                total_capital=TOTAL_CAPITAL_NTD,
                zones=list(BREADTH_ZONES_ORDER) + ["ALL"],
                zone_zh={**BREADTH_ZONE_ZH, "ALL": "全樣本"},
                metrics_by_strategy=portfolio_pooled[primary],
                strategy_labels=STRATEGY_LABELS,
            )
        )

    for year in years:
        if year not in YEAR_WINDOWS:
            print(f"Unknown year {year}; supported: {list(YEAR_WINDOWS)}", file=sys.stderr)
            return 1
        ds, de = YEAR_WINDOWS[year]
        pg, cc, rrg, portfolio, trade_dates, zone_days = _run_window(
            conn, close, year, ds, de, with_rrg=args.with_rrg
        )
        portfolio_by_year[year] = portfolio
        zone_days_by_year[year] = zone_days
        sections.append(
            render_vcp_breadth_dual_markdown(
                pivot_results=pg,
                coil_results=cc,
                rrg_results=rrg,
                year_label=year,
            )
        )
        zone_keys = list(BREADTH_ZONES_ORDER) + ["ALL"]
        sections.append(
            render_portfolio_zone_summary(
                year_label=year,
                total_capital=TOTAL_CAPITAL_NTD,
                zones=zone_keys,
                zone_zh={**BREADTH_ZONE_ZH, "ALL": "全樣本"},
                metrics_by_strategy=portfolio,
                strategy_labels=STRATEGY_LABELS,
            )
        )
        sections.append(
            render_portfolio_breadth_markdown(
                year_label=year,
                total_capital=TOTAL_CAPITAL_NTD,
                zones=zone_keys,
                zone_zh={**BREADTH_ZONE_ZH, "ALL": "全樣本"},
                metrics_by_strategy=portfolio,
                strategy_labels=STRATEGY_LABELS,
            )
        )
        payload["years"][year] = {
            "pivot_gate": {
                "pooled_all": pg["pooled_all"]["summary"],
                "pooled_by_entry_zone": pg["pooled_by_entry_zone"],
                "zone_day_counts": pg["zone_day_counts"],
                "pct_above_200_mean": pg["pct_above_200_mean"],
            },
            "coil_close": {
                "pooled_all": cc["pooled_all"]["summary"],
                "pooled_by_entry_zone": cc["pooled_by_entry_zone"],
            },
        }
        if rrg:
            payload["years"][year]["rrg_mono_hold7"] = {
                "pooled_all": rrg["pooled_all"]["summary"],
                "pooled_by_entry_zone": rrg["pooled_by_entry_zone"],
            }
        payload["years"][year]["portfolio"] = portfolio

    sections.append(
        render_zone_centric_portfolio_markdown(
            portfolio_by_year=portfolio_by_year,
            zone_day_counts_by_year=zone_days_by_year,
            years=years,
            total_capital=TOTAL_CAPITAL_NTD,
            strategy_keys=strategy_keys,
            strategy_labels=STRATEGY_LABELS,
            zone_zh=BREADTH_ZONE_ZH,
        )
    )

    conn.close()

    stamp = date.today().strftime("%Y%m%d")
    md = "\n".join(sections)
    out = args.output or RESEARCH_VCP / f"{stamp}_vcp_pivot_gate_coil_close_breadth_zones.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    print(f"Wrote {out}")
    print()
    print(md)

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Wrote {args.json}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
