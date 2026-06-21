#!/usr/bin/env python3
"""法人連買回測（ETF 成分聯集 · L1H5/H9/H14）。"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from research.backtest.copytrade_backtest import group_signals_by_date  # noqa: E402
from research.backtest.inst_flow_backtest import (  # noqa: E402
    DEFAULT_CAPITAL_CYCLE_MAX_H,
    SIGNAL_PROFILES,
    apply_etf_confluence,
    build_inst_flow_capital_cycle_rows,
    confluence_action_suffix,
    confluence_profile_id,
    format_inst_flow_report,
    format_inst_flow_round4_report,
    load_etf_add_index,
    run_inst_flow_matrix,
    scan_inst_flow_signals,
    _legs_per_day_stats,
)
from stock_db import (  # noqa: E402
    DEFAULT_DB_PATH,
    ETF_CODES_INTRADAY_DEFAULT,
    connect,
    load_etf_constituent_watchlist,
)


def _parse_confluence_etf(
    raw: str | None,
    *,
    round4: bool,
) -> tuple[str, ...]:
    if raw:
        return tuple(x.strip().upper() for x in raw.split(",") if x.strip())
    if round4:
        return ("00981A",)
    return ETF_CODES_INTRADAY_DEFAULT


def main() -> int:
    parser = argparse.ArgumentParser(description="法人連買 standalone 回測（ETF 成分聯集）")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--capital", type=float, default=10_000.0)
    parser.add_argument("--cost-bps", type=float, default=0.0)
    parser.add_argument("--window-start", default=None)
    parser.add_argument("--window-end", default=None)
    parser.add_argument(
        "--horizons",
        default="5,9,14",
        help="持有交易日，逗號分隔",
    )
    parser.add_argument(
        "--profiles",
        default="all",
        help="foreign5_pos,foreign5_top30,sync_buy3 或 all",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=None,
        metavar="K",
        help="每訊號日僅保留外資5日累計淨買最高的 K 檔",
    )
    parser.add_argument(
        "--rank-from",
        type=int,
        default=None,
        help="外資5日累計排名下限（1-based，與 --rank-to 合用）",
    )
    parser.add_argument(
        "--rank-to",
        type=int,
        default=None,
        help="外資5日累計排名上限（含，如 6–10）",
    )
    parser.add_argument(
        "--sync-buy-study",
        action="store_true",
        help="sync_buy2 vs sync_buy3 · L1 隔天 · Top-K",
    )
    parser.add_argument(
        "--confluence",
        action="store_true",
        help="另跑 inst∩ETF新进/加码（與 standalone 對照）",
    )
    parser.add_argument(
        "--confluence-etf",
        default=None,
        metavar="CODES",
        help="confluence 限定 ETF（逗號分隔，預設六檔聯集；round4 預設 00981A）",
    )
    parser.add_argument(
        "--round3",
        action="store_true",
        help="預設第三輪：profiles=sync_buy3,foreign5_pos · top-k=10 · confluence",
    )
    parser.add_argument(
        "--round4",
        action="store_true",
        help="第四輪：sync_buy3 · top-k=10 · 00981A confluence · H1–H20 資金輪動",
    )
    parser.add_argument(
        "--round5",
        action="store_true",
        help="第五輪：sync_buy3 × 00981A leg 重疊/互補 · H9/H12 · 雙槽",
    )
    parser.add_argument("--write-report", action="store_true")
    parser.add_argument(
        "--report",
        type=Path,
        default=None,
        help="預設 reports/YYYYMMDD_inst_flow_*.md",
    )
    args = parser.parse_args()

    if args.sync_buy_study:
        from research.backtest.inst_flow_backtest import (
            format_sync_buy_streak_report,
            run_sync_buy_streak_study,
        )

        horizons = tuple(int(x.strip()) for x in args.horizons.split(",") if x.strip())
        batch_id = f"inst-flow-syncbuy-{date.today().strftime('%Y%m%d')}"
        conn = connect(args.db)
        try:
            payload = run_sync_buy_streak_study(
                conn,
                capital_ntd=args.capital,
                cost_bps=args.cost_bps,
                horizons=horizons,
                top_k=args.top_k or 10,
                window_start=args.window_start,
                window_end=args.window_end,
                batch_id=batch_id,
            )
            print(f"batch={batch_id} sync_buy2 vs sync_buy3 · L1")
            for r in payload["results"]:
                wr = getattr(r, "win_rate_vs_bench_pct", r.win_rate_pct)
                print(
                    f"  {r.strategy_id}: days={r.n_complete_days} "
                    f"α={r.total_alpha_ntd:+,.0f} win_bench={wr}% p={r.p_value_wilcoxon}"
                )
            if args.write_report:
                report_path = args.report or (
                    ROOT
                    / "reports"
                    / f"{date.today().strftime('%Y%m%d')}_inst_flow_sync_buy2_vs_3.md"
                )
                report_path.parent.mkdir(parents=True, exist_ok=True)
                report_path.write_text(
                    format_sync_buy_streak_report(payload),
                    encoding="utf-8",
                )
                print(f"report → {report_path}")
        finally:
            conn.close()
        return 0

    if args.entry_lag_study:
        from research.backtest.inst_flow_backtest import (
            format_sync_buy3_entry_lag_report,
            run_sync_buy3_entry_lag_study,
        )

        horizons = tuple(int(x.strip()) for x in args.horizons.split(",") if x.strip())
        batch_id = f"inst-flow-entrylag-{date.today().strftime('%Y%m%d')}"
        conn = connect(args.db)
        try:
            payload = run_sync_buy3_entry_lag_study(
                conn,
                capital_ntd=args.capital,
                cost_bps=args.cost_bps,
                horizons=horizons,
                top_k=args.top_k or 10,
                window_start=args.window_start,
                window_end=args.window_end,
                batch_id=batch_id,
            )
            print(f"batch={batch_id} entry-lag study (L1=隔天 T+1 開盤)")
            for r in payload["results"]:
                wr = getattr(r, "win_rate_vs_bench_pct", r.win_rate_pct)
                print(
                    f"  {r.strategy_id}: days={r.n_complete_days} "
                    f"α={r.total_alpha_ntd:+,.0f} win_bench={wr}% p={r.p_value_wilcoxon}"
                )
            if args.write_report:
                report_path = args.report or (
                    ROOT
                    / "reports"
                    / f"{date.today().strftime('%Y%m%d')}_inst_flow_sync_buy3_entry_lag.md"
                )
                report_path.parent.mkdir(parents=True, exist_ok=True)
                report_path.write_text(
                    format_sync_buy3_entry_lag_report(payload),
                    encoding="utf-8",
                )
                print(f"report → {report_path}")
        finally:
            conn.close()
        return 0

    if args.rank_study:
        from research.backtest.inst_flow_backtest import (
            format_sync_buy3_rank_band_report,
            run_sync_buy3_rank_band_study,
        )

        if args.profiles == "all":
            args.profiles = "sync_buy3"
        horizons = tuple(int(x.strip()) for x in args.horizons.split(",") if x.strip())
        batch_id = f"inst-flow-rankband-{date.today().strftime('%Y%m%d')}"
        conn = connect(args.db)
        try:
            payload = run_sync_buy3_rank_band_study(
                conn,
                capital_ntd=args.capital,
                cost_bps=args.cost_bps,
                horizons=horizons,
                window_start=args.window_start,
                window_end=args.window_end,
                batch_id=batch_id,
            )
            print(f"batch={batch_id} rank-band study")
            for r in payload["results"]:
                wr = getattr(r, "win_rate_vs_bench_pct", r.win_rate_pct)
                print(
                    f"  {r.strategy_id}: days={r.n_complete_days} "
                    f"α={r.total_alpha_ntd:+,.0f} win_bench={wr}% p={r.p_value_wilcoxon}"
                )
            if args.write_report:
                report_path = args.report or (
                    ROOT
                    / "reports"
                    / f"{date.today().strftime('%Y%m%d')}_inst_flow_sync_buy3_rank_band.md"
                )
                report_path.parent.mkdir(parents=True, exist_ok=True)
                report_path.write_text(
                    format_sync_buy3_rank_band_report(payload),
                    encoding="utf-8",
                )
                print(f"report → {report_path}")
        finally:
            conn.close()
        return 0

    if args.round5:
        from research.backtest.inst_flow_981a_overlap import (
            format_inst_flow_round5_report,
            run_inst_flow_round5,
        )

        batch_id = f"inst-flow-r5-{date.today().strftime('%Y%m%d')}"
        conn = connect(args.db)
        try:
            payload = run_inst_flow_round5(
                conn,
                capital_ntd=args.capital,
                cost_bps=args.cost_bps,
                top_k=args.top_k or 10,
                window_start=args.window_start,
                window_end=args.window_end,
                batch_id=batch_id,
            )
            print(f"batch={batch_id} round5 overlap")
            overlap = payload["overlap_stats"]
            print(
                f"  legs: inst={overlap['inst_legs']} etf={overlap['etf_legs']} "
                f"both={overlap['both_legs']} jaccard={overlap['leg_jaccard_pct']}%"
            )
            for r in payload["bucket_results"]:
                wr = getattr(r, "win_rate_vs_bench_pct", r.win_rate_pct)
                print(
                    f"  {r.strategy_id}: days={r.n_complete_days} "
                    f"α={r.total_alpha_ntd:+,.0f} win_bench={wr}% "
                    f"p={r.p_value_wilcoxon}"
                )
            if args.write_report:
                report_path = args.report or (
                    ROOT
                    / "reports"
                    / f"{date.today().strftime('%Y%m%d')}_inst_flow_r5_981a_overlap.md"
                )
                report_path.parent.mkdir(parents=True, exist_ok=True)
                report_path.write_text(
                    format_inst_flow_round5_report(payload),
                    encoding="utf-8",
                )
                print(f"report → {report_path}")
        finally:
            conn.close()
        return 0

    if args.round4:
        args.confluence = True
        if args.profiles == "all":
            args.profiles = "sync_buy3"
        if args.top_k is None:
            args.top_k = 10
        if args.horizons == "5,9,14":
            args.horizons = ",".join(str(h) for h in range(1, DEFAULT_CAPITAL_CYCLE_MAX_H + 1))

    if args.round3:
        args.confluence = True
        if args.profiles == "all":
            args.profiles = "sync_buy3,foreign5_pos"
        if args.top_k is None:
            args.top_k = 10

    confluence_etf_codes = _parse_confluence_etf(
        args.confluence_etf,
        round4=args.round4,
    )

    horizons = tuple(int(x.strip()) for x in args.horizons.split(",") if x.strip())
    if args.profiles.lower() == "all":
        profiles = SIGNAL_PROFILES
    else:
        wanted = {x.strip() for x in args.profiles.split(",") if x.strip()}
        profiles = tuple(p for p in SIGNAL_PROFILES if p.profile_id in wanted)
        if not profiles:
            print("無有效 profile", file=sys.stderr)
            return 1

    batch_id = f"inst-flow-l1-{date.today().strftime('%Y%m%d')}"
    if args.round4:
        batch_id = f"inst-flow-r4-{date.today().strftime('%Y%m%d')}"
    elif args.confluence:
        batch_id = f"inst-flow-r3-{date.today().strftime('%Y%m%d')}"
    if args.top_k:
        batch_id = batch_id.replace("l1-", f"top{args.top_k}-").replace("r3-", f"r3-top{args.top_k}-")
        batch_id = batch_id.replace("r4-", f"r4-top{args.top_k}-")

    conn = connect(args.db)
    try:
        watchlist = load_etf_constituent_watchlist(conn, ETF_CODES_INTRADAY_DEFAULT)
        if not watchlist:
            print("ETF 成分聯集為空", file=sys.stderr)
            return 1

        stock_ids = [w["stock_id"] for w in watchlist]
        name_by_id = {w["stock_id"]: w.get("stock_name") or w["stock_id"] for w in watchlist}

        results = run_inst_flow_matrix(
            conn,
            profiles=profiles,
            horizons=horizons,
            capital_ntd=args.capital,
            cost_bps=args.cost_bps,
            window_start=args.window_start,
            window_end=args.window_end,
            batch_id=batch_id,
            top_k=args.top_k,
            rank_from=args.rank_from,
            rank_to=args.rank_to,
            confluence=args.confluence,
            confluence_etf_codes=confluence_etf_codes,
        )

        w_start = results[0].window_start if results else args.window_start
        w_end = results[0].window_end if results else args.window_end

        etf_add_index = (
            load_etf_add_index(conn, confluence_etf_codes) if args.confluence else None
        )
        conf_suffix = confluence_action_suffix(confluence_etf_codes)
        leg_stats: dict[str, dict[str, float | int]] = {}
        for p in profiles:
            signals = scan_inst_flow_signals(
                conn,
                profile=p,
                stock_ids=stock_ids,
                name_by_id=name_by_id,
                window_start=w_start,
                window_end=w_end,
                top_k=args.top_k,
                rank_from=args.rank_from,
                rank_to=args.rank_to,
            )
            leg_stats[p.profile_id] = _legs_per_day_stats(group_signals_by_date(signals))
            if args.confluence and etf_add_index is not None:
                cid = confluence_profile_id(p.profile_id, confluence_etf_codes)
                conf = apply_etf_confluence(
                    signals,
                    etf_add_index,
                    action_suffix=conf_suffix,
                )
                leg_stats[cid] = _legs_per_day_stats(group_signals_by_date(conf))

        print(
            f"batch={batch_id} universe={len(stock_ids)} profiles={len(profiles)} "
            f"top_k={args.top_k} confluence={args.confluence} "
            f"confluence_etf={','.join(confluence_etf_codes) if args.confluence else '—'}"
        )
        for r in results:
            wr = getattr(r, "win_rate_vs_bench_pct", r.win_rate_pct)
            print(
                f"  {r.strategy_id}: days={r.n_complete_days} "
                f"α={r.total_alpha_ntd:+,.0f} win_bench={wr}% "
                f"p={r.p_value_wilcoxon}"
            )

        if args.write_report:
            if args.round4:
                suffix = "_r4_981a_capital_cycle"
                report_path = args.report or (
                    ROOT / "reports" / f"{date.today().strftime('%Y%m%d')}_inst_flow{suffix}.md"
                )
                cycle_rows: dict[str, list[dict]] = {}
                for p in profiles:
                    cycle_rows[p.profile_id] = build_inst_flow_capital_cycle_rows(
                        conn,
                        results,
                        p.profile_id,
                        capital_ntd=args.capital,
                    )
                    cid = confluence_profile_id(p.profile_id, confluence_etf_codes)
                    cycle_rows[cid] = build_inst_flow_capital_cycle_rows(
                        conn,
                        results,
                        cid,
                        capital_ntd=args.capital,
                    )
                report_path.parent.mkdir(parents=True, exist_ok=True)
                report_path.write_text(
                    format_inst_flow_round4_report(
                        results,
                        cycle_rows,
                        profiles=profiles,
                        horizons=horizons,
                        capital_ntd=args.capital,
                        batch_id=batch_id,
                        universe_n=len(stock_ids),
                        confluence_etf_codes=confluence_etf_codes,
                        leg_stats_by_profile=leg_stats,
                        top_k=args.top_k,
                    ),
                    encoding="utf-8",
                )
            else:
                if args.confluence:
                    suffix = "_r3_confluence"
                    if args.top_k:
                        suffix = f"_r3_top{args.top_k}_confluence"
                else:
                    suffix = f"_top{args.top_k}" if args.top_k else ""
                report_path = args.report or (
                    ROOT / "reports" / f"{date.today().strftime('%Y%m%d')}_inst_flow{suffix}_l1_decay.md"
                )
                report_path.parent.mkdir(parents=True, exist_ok=True)
                report_path.write_text(
                    format_inst_flow_report(
                        results,
                        profiles=profiles,
                        horizons=horizons,
                        capital_ntd=args.capital,
                        cost_bps=args.cost_bps,
                        batch_id=batch_id,
                        universe_n=len(stock_ids),
                        leg_stats_by_profile=leg_stats,
                        top_k=args.top_k,
                        confluence=args.confluence,
                        confluence_etf_codes=confluence_etf_codes,
                        base_profile_ids=tuple(p.profile_id for p in profiles),
                    ),
                    encoding="utf-8",
                )
            print(f"report → {report_path}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
