"""Fair multi-branch Copytrade compare · frozen L/H/slots + IS/OOS stats."""

from __future__ import annotations

import json
import math
import sqlite3
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from statistics import mean, median, pstdev

from copytrade.branch_signals import iter_branch_amount_buy_signals
from copytrade.signals import group_signals_by_date
from research.branch_exclusion import check_trader_ids, exclusion_index, format_warning
from research.backtest.copytrade_backtest import (
    DEFAULT_SIGNAL_CAPITAL_NTD,
    count_hold_trading_days,
    run_copytrade_backtest,
    select_executed_signal_days,
)
from research.backtest.yuanta_songjiang_copytrade_sweep import day_results_to_dicts
from report_paths import REPORTS_RESEARCH

DEFAULT_WINDOW_START = "2024-07-01"
DEFAULT_OOS_CUT = "2025-07-01"
DEFAULT_ENTRY_LAG = 0  # L1
DEFAULT_HOLD = 12
DEFAULT_SLOTS = 3
DEFAULT_TOP_N = 1
DEFAULT_MIN_NET_AMOUNTS_M = (
    10.0,
    20.0,
    30.0,
    40.0,
    50.0,
    70.0,
    80.0,
    100.0,
    200.0,
    500.0,
    700.0,
    1000.0,
    1500.0,
    2000.0,
)
TARGET_IS_SIGNALS = 80  # density-match target on IS window


@dataclass(frozen=True)
class BranchSpec:
    slug: str
    label: str
    trader_id: str
    style: str  # community attribute, not a measured fact


DEFAULT_BRANCHES: tuple[BranchSpec, ...] = (
    BranchSpec("fubon-xindian", "富邦-新店", "9661", "community_wave"),
    BranchSpec("kgi-chengzhong", "凱基-城中", "9227", "community_wave"),
    BranchSpec("yuanta-songjiang", "元大-松江", "9801", "community_wave_id_renamed"),
    BranchSpec("kgi-taipei", "凱基-台北", "9268", "community_daytrade"),
    BranchSpec("guopiao-anhe", "國票-安和", "779Z", "community_winner"),
    BranchSpec("jpmorgan", "摩根大通", "8440", "foreign"),
    # Expansion batch (20260718)
    BranchSpec("fubon-jianguo", "富邦-建國", "9658", "community_daytrade"),
    BranchSpec("yuanfu-chiayi", "元富-嘉義", "592I", "community_daytrade"),
    BranchSpec("kgi-songshan", "凱基-松山", "9217", "community_branch"),
    BranchSpec("yuanta-ximen", "元大-西門", "9873", "community_branch"),
    BranchSpec("uni-tucheng", "統一-土城", "585Y", "community_branch"),
    BranchSpec("kgi-huwei", "凱基-虎尾", "9236", "community_branch"),
    BranchSpec("fubon-taoyuan", "富邦-桃園", "9665", "community_branch"),
    BranchSpec("goldman", "美商高盛", "1480", "foreign"),
    BranchSpec("ms-taiwan", "台灣摩根士丹利", "1470", "foreign"),
    BranchSpec("nomura", "港商野村", "1560", "foreign"),
    BranchSpec("ubs-sg", "新加坡商瑞銀", "1650", "foreign"),
    # 1520 瑞士信貸 已移除（20260817）：CS 併入 UBS，tape 只剩 2026-05-20 起 40 天
    # 且 net 全為 0，任何 window 都跑不出結果，留著只會每次被 min-days 濾掉一次。
    # 該席的實際流量現已併入 1650 新加坡商瑞銀。
    # Foreign-branch completion batch (20260817): remaining 外資分點 on tape
    BranchSpec("macquarie", "港商麥格理", "1360", "foreign"),
    BranchSpec("merrill", "美林", "1440", "foreign"),
    BranchSpec("citigroup", "花旗環球", "1590", "foreign"),
    BranchSpec("daiwa-cathay", "大和國泰", "8890", "foreign"),
    BranchSpec("hsbc", "上海匯豐", "8960", "foreign"),
)


def _sharpe_like(xs: list[float]) -> float | None:
    if len(xs) < 2:
        return None
    sd = pstdev(xs)
    if sd <= 1e-12:
        return None
    return mean(xs) / sd


def tape_coverage(
    conn: sqlite3.Connection,
    trader_id: str,
    *,
    window_start: str,
    window_end: str,
) -> dict:
    row = conn.execute(
        """
        SELECT COUNT(*) AS n_rows,
               COUNT(DISTINCT trade_date) AS n_days,
               MIN(trade_date) AS mn,
               MAX(trade_date) AS mx
        FROM stock_broker_branch_daily
        WHERE securities_trader_id = ?
          AND source = 'finmind'
          AND trade_date >= ? AND trade_date <= ?
        """,
        (trader_id, window_start, window_end),
    ).fetchone()
    return {
        "n_rows": int(row["n_rows"] or 0),
        "n_days": int(row["n_days"] or 0),
        "min_date": row["mn"],
        "max_date": row["mx"],
    }


def summarize_cycles(
    conn: sqlite3.Connection,
    day_dicts: list[dict],
    *,
    capital_ntd: float,
    n_slots: int,
) -> dict:
    executed, meta = select_executed_signal_days(day_dicts, n_slots=n_slots)
    rets = [float(d["return_pct"]) for d in executed if d.get("return_pct") is not None]
    alphas = [float(d["alpha_ntd"]) for d in executed if d.get("alpha_ntd") is not None]
    pnls = [float(d["pnl_ntd"]) for d in executed if d.get("pnl_ntd") is not None]
    n = len(executed)
    total_capital = capital_ntd * n_slots
    total_pnl = sum(pnls)
    total_alpha = sum(alphas)
    locked = sum(
        count_hold_trading_days(conn, str(d["entry_date"]), str(d["exit_date"]))
        for d in executed
    )
    wins = sum(1 for r in rets if r > 0)
    alpha_wins = sum(1 for a in alphas if a > 0)

    by_month: dict[str, list[float]] = {}
    for d in executed:
        ym = str(d.get("signal_date") or "")[:7]
        if not ym or d.get("return_pct") is None:
            continue
        by_month.setdefault(ym, []).append(float(d["return_pct"]))
    month_hit = sum(1 for xs in by_month.values() if sum(xs) > 0)

    return {
        "n_signals": int(meta.get("n_signals") or 0),
        "n_cycles": n,
        "n_skipped": int(meta.get("n_skipped") or 0),
        "signal_capture_pct": meta.get("signal_capture_pct"),
        "period_return_pct": (
            round(100.0 * total_pnl / total_capital, 4) if total_capital else None
        ),
        "total_pnl_ntd": round(total_pnl, 2),
        "total_alpha_ntd": round(total_alpha, 2),
        "alpha_per_cycle": round(total_alpha / n, 2) if n else None,
        "alpha_per_locked_day": (
            round(total_alpha / locked, 4) if locked else None
        ),
        "locked_days": locked,
        "win_rate_pct": round(100.0 * wins / n, 2) if n else None,
        "alpha_win_rate_pct": round(100.0 * alpha_wins / n, 2) if n else None,
        "mean_return_pct": round(mean(rets), 4) if rets else None,
        "median_return_pct": round(median(rets), 4) if rets else None,
        "std_return_pct": round(pstdev(rets), 4) if len(rets) >= 2 else None,
        "sharpe_like": (
            round(_sharpe_like(rets), 4) if _sharpe_like(rets) is not None else None
        ),
        "mean_alpha_ntd": round(mean(alphas), 2) if alphas else None,
        "n_months": len(by_month),
        "positive_month_pct": (
            round(100.0 * month_hit / len(by_month), 2) if by_month else None
        ),
        "sum_cycle_pnl_ntd": round(sum(pnls), 2) if pnls else 0.0,
    }


def evaluate_branch_window(
    conn: sqlite3.Connection,
    *,
    trader_id: str,
    etf_proxy: str,
    label_prefix: str,
    min_net_amount_ntd: float,
    top_n: int,
    window_start: str,
    window_end: str,
    entry_lag_days: int = DEFAULT_ENTRY_LAG,
    hold_trading_days: int = DEFAULT_HOLD,
    n_slots: int = DEFAULT_SLOTS,
    capital_ntd: float = DEFAULT_SIGNAL_CAPITAL_NTD,
    min_avg_volume_shares: float | None = 200_000.0,
    cost_bps: float = 0.0,
) -> dict:
    signals = iter_branch_amount_buy_signals(
        conn,
        trader_id,
        min_net_amount_ntd=min_net_amount_ntd,
        top_n=top_n,
        window_start=window_start,
        window_end=window_end,
        min_avg_volume_shares=min_avg_volume_shares,
    )
    grouped = group_signals_by_date(signals)
    if not grouped:
        return {
            "empty": True,
            "n_raw_signals": 0,
            **summarize_cycles(
                conn,
                [],
                capital_ntd=capital_ntd,
                n_slots=n_slots,
            ),
        }

    sid = f"L{entry_lag_days + 1}H{hold_trading_days}"
    result = run_copytrade_backtest(
        conn,
        etf_proxy,
        strategy_id=sid,
        strategy_label=(
            f"{label_prefix} {sid} amount≥{min_net_amount_ntd:.0f} top{top_n}"
        ),
        entry_lag_days=entry_lag_days,
        hold_trading_days=hold_trading_days,
        entry_price_mode="open",
        capital_ntd=capital_ntd,
        cost_bps=cost_bps,
        window_start=window_start,
        window_end=window_end,
        grouped=grouped,
    )
    day_dicts = day_results_to_dicts(result)
    summary = summarize_cycles(
        conn, day_dicts, capital_ntd=capital_ntd, n_slots=n_slots
    )
    return {
        "empty": False,
        "n_raw_signals": len(signals),
        "n_signal_days": len(grouped),
        **summary,
    }


def pick_density_matched_amount(
    conn: sqlite3.Connection,
    *,
    trader_id: str,
    etf_proxy: str,
    label_prefix: str,
    window_start: str,
    window_end: str,
    candidate_amounts_m: tuple[float, ...] = DEFAULT_MIN_NET_AMOUNTS_M,
    target_signals: int = TARGET_IS_SIGNALS,
    top_n: int = DEFAULT_TOP_N,
) -> tuple[float, dict]:
    """Pick amount threshold whose IS n_signals is closest to target."""
    best_amount_m = candidate_amounts_m[0]
    best_row: dict | None = None
    best_dist = math.inf
    grid: list[dict] = []
    for amount_m in candidate_amounts_m:
        row = evaluate_branch_window(
            conn,
            trader_id=trader_id,
            etf_proxy=etf_proxy,
            label_prefix=label_prefix,
            min_net_amount_ntd=amount_m * 1_000_000.0,
            top_n=top_n,
            window_start=window_start,
            window_end=window_end,
        )
        n_sig = int(row.get("n_signals") or 0)
        dist = abs(n_sig - target_signals)
        grid.append(
            {"min_net_amount_m": amount_m, "n_signals": n_sig, "dist": dist}
        )
        if dist < best_dist or (
            dist == best_dist and best_row is not None and n_sig > int(best_row.get("n_signals") or 0)
        ):
            best_dist = dist
            best_amount_m = amount_m
            best_row = row
    assert best_row is not None
    return best_amount_m, {
        "grid": grid,
        "picked_amount_m": best_amount_m,
        "is_eval": best_row,
    }


def previous_trading_date(conn: sqlite3.Connection, oos_start: str) -> str:
    row = conn.execute(
        """
        SELECT MAX(trade_date)
        FROM stock_daily_bars
        WHERE source = 'finmind' AND trade_date < ?
        """,
        (oos_start,),
    ).fetchone()
    if row is None or row[0] is None:
        raise ValueError(f"no trading date before OOS start {oos_start}")
    return str(row[0])


def run_fair_compare(
    conn: sqlite3.Connection,
    *,
    branches: tuple[BranchSpec, ...] = DEFAULT_BRANCHES,
    window_start: str = DEFAULT_WINDOW_START,
    window_end: str | None = None,
    oos_cut: str = DEFAULT_OOS_CUT,
    min_net_amounts_m_grid: tuple[float, ...] = DEFAULT_MIN_NET_AMOUNTS_M,
    top_n: int = DEFAULT_TOP_N,
    entry_lag_days: int = DEFAULT_ENTRY_LAG,
    hold_trading_days: int = DEFAULT_HOLD,
    n_slots: int = DEFAULT_SLOTS,
    target_is_signals: int = TARGET_IS_SIGNALS,
) -> dict:
    w_end = window_end or date.today().isoformat()
    is_end = previous_trading_date(conn, oos_cut)
    protocol = {
        "window_start": window_start,
        "window_end": w_end,
        "oos_cut": oos_cut,
        "is_end": is_end,
        "entry": f"L{entry_lag_days + 1}_open",
        "hold_trading_days": hold_trading_days,
        "n_slots": n_slots,
        "top_n": top_n,
        "capital_ntd_per_slot": DEFAULT_SIGNAL_CAPITAL_NTD,
        "min_avg_volume_shares": 200_000.0,
        "cost_bps": 0.0,
        "metric": "net_amount_ntd",
        "amount_definition": "positive net shares × signal-day close",
        "min_net_amounts_m_grid": list(min_net_amounts_m_grid),
        "density_target_is_signals": target_is_signals,
        "primary_rank": (
            "oos_period_return_pct (density-matched per-branch amount), "
            "min_cycles>=10"
        ),
        "note": (
            "Each branch uses its own amount threshold so IS signal density "
            "≈ target; do not compare fixed 100M for finding branch skill."
        ),
    }

    # 排除清單是警示不是硬擋：重測既有成員合法，但必須是有意識的選擇。
    hits = check_trader_ids([b.trader_id for b in branches])
    if hits:
        print(format_warning(hits), file=sys.stderr)
    protocol["market_maker_exclusion_hits"] = [r["trader_id"] for r in hits]
    excl_idx = exclusion_index()

    branch_rows: list[dict] = []
    for b in branches:
        cov = tape_coverage(
            conn, b.trader_id, window_start=window_start, window_end=w_end
        )
        etf_proxy = f"BR_{b.slug.upper().replace('-', '_')}"
        frozen_grid: list[dict] = []
        for amount_m in min_net_amounts_m_grid:
            full = evaluate_branch_window(
                conn,
                trader_id=b.trader_id,
                etf_proxy=etf_proxy,
                label_prefix=b.label,
                min_net_amount_ntd=amount_m * 1_000_000.0,
                top_n=top_n,
                window_start=window_start,
                window_end=w_end,
                entry_lag_days=entry_lag_days,
                hold_trading_days=hold_trading_days,
                n_slots=n_slots,
            )
            is_part = evaluate_branch_window(
                conn,
                trader_id=b.trader_id,
                etf_proxy=etf_proxy,
                label_prefix=b.label,
                min_net_amount_ntd=amount_m * 1_000_000.0,
                top_n=top_n,
                window_start=window_start,
                window_end=is_end,
                entry_lag_days=entry_lag_days,
                hold_trading_days=hold_trading_days,
                n_slots=n_slots,
            )
            oos_part = evaluate_branch_window(
                conn,
                trader_id=b.trader_id,
                etf_proxy=etf_proxy,
                label_prefix=b.label,
                min_net_amount_ntd=amount_m * 1_000_000.0,
                top_n=top_n,
                window_start=oos_cut,
                window_end=w_end,
                entry_lag_days=entry_lag_days,
                hold_trading_days=hold_trading_days,
                n_slots=n_slots,
            )
            frozen_grid.append(
                {
                    "min_net_amount_m": amount_m,
                    "full": full,
                    "is": is_part,
                    "oos": oos_part,
                }
            )

        dens_amount_m, dens_meta = pick_density_matched_amount(
            conn,
            trader_id=b.trader_id,
            etf_proxy=etf_proxy,
            label_prefix=b.label,
            window_start=window_start,
            window_end=is_end,
            candidate_amounts_m=min_net_amounts_m_grid,
            target_signals=target_is_signals,
            top_n=top_n,
        )
        dens_full = evaluate_branch_window(
            conn,
            trader_id=b.trader_id,
            etf_proxy=etf_proxy,
            label_prefix=b.label,
            min_net_amount_ntd=dens_amount_m * 1_000_000.0,
            top_n=top_n,
            window_start=window_start,
            window_end=w_end,
            entry_lag_days=entry_lag_days,
            hold_trading_days=hold_trading_days,
            n_slots=n_slots,
        )
        dens_oos = evaluate_branch_window(
            conn,
            trader_id=b.trader_id,
            etf_proxy=etf_proxy,
            label_prefix=b.label,
            min_net_amount_ntd=dens_amount_m * 1_000_000.0,
            top_n=top_n,
            window_start=oos_cut,
            window_end=w_end,
            entry_lag_days=entry_lag_days,
            hold_trading_days=hold_trading_days,
            n_slots=n_slots,
        )

        branch_rows.append(
            {
                "slug": b.slug,
                "label": b.label,
                "trader_id": b.trader_id,
                "style": b.style,
                "market_maker_excluded": b.trader_id in excl_idx,
                "market_maker_exclusion_note": (
                    excl_idx.get(b.trader_id, {}).get("note")
                ),
                "coverage": cov,
                "frozen_grid": frozen_grid,
                "density_matched": {
                    "min_net_amount_m": dens_amount_m,
                    "is_pick_meta": dens_meta,
                    "full": dens_full,
                    "is": dens_meta["is_eval"],
                    "oos": dens_oos,
                },
            }
        )

    # Primary: density-matched with per-branch amount, ranked by period return.
    dens_rank = []
    for row in branch_rows:
        oos = row["density_matched"]["oos"]
        full = row["density_matched"]["full"]
        dens_rank.append(
            {
                "slug": row["slug"],
                "label": row["label"],
                "style": row["style"],
                "min_net_amount_m": row["density_matched"]["min_net_amount_m"],
                "is_n_signals": row["density_matched"]["is"].get("n_signals"),
                "oos_n_cycles": oos.get("n_cycles"),
                "oos_period_return_pct": oos.get("period_return_pct"),
                "oos_mean_return_pct": oos.get("mean_return_pct"),
                "oos_median_return_pct": oos.get("median_return_pct"),
                "oos_win_rate_pct": oos.get("win_rate_pct"),
                "oos_sharpe_like": oos.get("sharpe_like"),
                "oos_positive_month_pct": oos.get("positive_month_pct"),
                "oos_alpha_per_cycle": oos.get("alpha_per_cycle"),
                "full_period_return_pct": full.get("period_return_pct"),
                "full_n_cycles": full.get("n_cycles"),
                "full_mean_return_pct": full.get("mean_return_pct"),
            }
        )
    dens_rank.sort(
        key=lambda r: (
            0 if (r["oos_n_cycles"] or 0) >= 10 else 1,
            -(
                r["oos_period_return_pct"]
                if r["oos_period_return_pct"] is not None
                else -1e18
            ),
        )
    )

    full_rank = sorted(
        dens_rank,
        key=lambda r: (
            0 if (r["full_n_cycles"] or 0) >= 10 else 1,
            -(
                r["full_period_return_pct"]
                if r["full_period_return_pct"] is not None
                else -1e18
            ),
        ),
    )

    return {
        "protocol": protocol,
        "branches": branch_rows,
        "rank_density_matched_oos": dens_rank,
        "rank_density_matched_full": full_rank,
        "generated_on": date.today().isoformat(),
    }


def render_markdown(payload: dict) -> str:
    p = payload["protocol"]
    lines = [
        f"# 券商分點 Copytrade 金額公平統計比較 · {payload['generated_on']}",
        "",
        "## Protocol（凍結）",
        "",
        f"- Window: `{p['window_start']}` → `{p['window_end']}` · IS end `{p['is_end']}` · OOS start `{p['oos_cut']}`",
        f"- Execution: {p['entry']} · H={p['hold_trading_days']} · slots={p['n_slots']} · Top-{p['top_n']}",
        f"- Liquidity: 20d avg vol ≥ {p['min_avg_volume_shares']:.0f} shares · cost_bps={p['cost_bps']}",
        f"- Amount: {p['amount_definition']} (PIT)",
        f"- Density match: each branch picks its own amount so IS n_signals ≈ {p['density_target_is_signals']} (grid {p['min_net_amounts_m_grid']} M)",
        f"- Primary rank: {p['primary_rank']}",
        f"- Note: {p.get('note')}",
        "",
        "## Density-matched · OOS period return%（找隱藏高手主表）",
        "",
        "| Rank | Branch | Own amount M | IS sig | OOS n | OOS ret% | Mean ret%/cycle | Med ret% | Win% | Sharpe* | +Month% |",
        "|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for i, r in enumerate(payload["rank_density_matched_oos"], 1):
        lines.append(
            "| {rank} | {label} | {amount:g} | {is_n} | {n} | {ret} | {mean} | {med} | {wr} | {sh} | {pm} |".format(
                rank=i,
                label=r["label"],
                amount=r["min_net_amount_m"],
                is_n=r["is_n_signals"],
                n=r["oos_n_cycles"],
                ret=r["oos_period_return_pct"],
                mean=r.get("oos_mean_return_pct"),
                med=r.get("oos_median_return_pct"),
                wr=r["oos_win_rate_pct"],
                sh=r["oos_sharpe_like"],
                pm=r["oos_positive_month_pct"],
            )
        )
    lines += [
        "",
        "## Density-matched · Full-window period return%",
        "",
        "| Rank | Branch | Own amount M | Full n | Full ret% | Mean ret%/cycle | OOS ret% |",
        "|---:|---|---:|---:|---:|---:|---:|",
    ]
    for i, r in enumerate(payload.get("rank_density_matched_full") or [], 1):
        lines.append(
            "| {rank} | {label} | {amount:g} | {n} | {fr} | {mean} | {oos} |".format(
                rank=i,
                label=r["label"],
                amount=r["min_net_amount_m"],
                n=r.get("full_n_cycles"),
                fr=r["full_period_return_pct"],
                mean=r.get("full_mean_return_pct"),
                oos=r["oos_period_return_pct"],
            )
        )
    lines += [
        "",
        "## Coverage",
        "",
        "| Branch | ID | Days | Rows | Min | Max | Style | 造市排除清單 |",
        "|---|---|---:|---:|---|---|---|---|",
    ]
    for b in payload["branches"]:
        c = b["coverage"]
        excl = "**已列入**" if b.get("market_maker_excluded") else "—"
        lines.append(
            f"| {b['label']} | {b['trader_id']} | {c['n_days']} | {c['n_rows']} | "
            f"{c['min_date']} | {c['max_date']} | {b['style']} | {excl} |"
        )
    hits = payload["protocol"].get("market_maker_exclusion_hits") or []
    if hits:
        lines += [
            "",
            f"> ⚠️ 本次 {len(hits)} 支分點（{', '.join(hits)}）已在造市／綜合流量排除清單內"
            "（`reports/research/branch-footprint-screen/market_maker_branch_exclusion_v1.json`）。"
            "清單語意是警示不是硬擋；若這次就是要重測既有成員，請在 notes 寫明理由。",
        ]
    lines += [
        "",
        "## Notes",
        "",
        "- Each branch uses its own density-matched amount threshold; fixed 100M is not used for ranking.",
        "- Primary metric: period_return% on fixed 3-slot capital (10k NTD/slot).",
        "- Sharpe* = mean(cycle return%) / pstdev(cycle return%); not annualized.",
        "- OOS n < 10 rows are demoted in ranking (insufficient sample).",
        "",
    ]
    return "\n".join(lines)


def write_artifacts(payload: dict, out_dir: Path | None = None) -> dict[str, Path]:
    stamp = date.today().strftime("%Y%m%d")
    out = out_dir or (REPORTS_RESEARCH / "branch-copytrade-fair-compare")
    out.mkdir(parents=True, exist_ok=True)
    stem = f"{stamp}_branch_copytrade_amount_fair_compare"
    json_path = out / f"{stem}.json"
    md_path = out / f"{stem}.md"
    latest_json = out / "branch_copytrade_amount_fair_compare.json"
    latest_md = out / "branch_copytrade_amount_fair_compare.md"

    # Drop heavy nested grids from latest? Keep full for research.
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    latest_json.write_text(json_path.read_text(encoding="utf-8"), encoding="utf-8")
    md = render_markdown(payload)
    md_path.write_text(md, encoding="utf-8")
    latest_md.write_text(md, encoding="utf-8")
    return {"json": json_path, "md": md_path, "latest_json": latest_json, "latest_md": latest_md}
