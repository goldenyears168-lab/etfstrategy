#!/usr/bin/env python3
"""00981A ETF 持股變化跟單回測 → copytrade_* tables + markdown 報告。"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from research.backtest.copytrade_backtest import (  # noqa: E402
    backfill_copytrade_overnight_gaps,
    format_fixed_capital_horizon_markdown,
    format_rotation_capital_markdown,
    resolve_strategy_specs,
    run_allocation_comparison,
    run_capital_cycle_analysis,
    run_fixed_slots_analysis,
    run_strategies,
    summarize_decay_insights,
    summarize_capital_cycle_insights,
    build_horizon_decay_rows,
    write_copytrade_report,
)
from stock_db import (  # noqa: E402
    DEFAULT_DB_PATH,
    connect,
    load_copytrade_capital_cycle,
    load_copytrade_capital_slots,
    load_copytrade_research_conclusions,
    load_copytrade_runs,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="00981A 跟單回測（已停損 filter 研究見 docs/00981a-retired-research.md）"
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--etf-code", default="00981A")
    parser.add_argument("--capital", type=float, default=10_000.0)
    parser.add_argument("--cost-bps", type=float, default=0.0, help="來回成本 bps")
    parser.add_argument(
        "--strategy",
        default="all",
        help="L1H3,L0O-H1,S0 或 all（S0→L1H1 等舊代號仍可用）",
    )
    parser.add_argument(
        "--matrix",
        action="store_true",
        help="跑完整 L×H 矩陣",
    )
    parser.add_argument(
        "--max-hold",
        type=int,
        default=20,
        help="最大持有交易日 H（矩陣預設 20）",
    )
    parser.add_argument(
        "--no-l0",
        action="store_true",
        help="矩陣略過 L0O/L0C",
    )
    parser.add_argument("--window-start", default=None)
    parser.add_argument("--window-end", default=None)
    parser.add_argument("--write-db", action="store_true", help="寫入 copytrade_*")
    parser.add_argument("--write-report", action="store_true", help="寫入 reports/")
    parser.add_argument(
        "--backfill-overnight-gaps",
        action="store_true",
        help="僅補齊 copytrade_leg_overnight_gaps",
    )
    parser.add_argument(
        "--compare-982a-day",
        action="store_true",
        help="982A 重疊調倉日 filter（方向 A · 重疊日跟全 basket · 預設 L1H9）",
    )
    parser.add_argument(
        "--consensus-etf",
        default="00982A",
        help="跨 ETF 共識 peer（預設 00982A）",
    )
    parser.add_argument(
        "--compare-allocation",
        action="store_true",
        help="等權 vs 按 weight_pct 配置對照（預設 L1H9）",
    )
    parser.add_argument(
        "--compare-hypothesis",
        action="store_true",
        help="H1 異動檔數假說驗證（預設 L1H9）",
    )
    parser.add_argument(
        "--compare-leg-count",
        action="store_true",
        help="僅 H1 訊號日異動檔數研究",
    )
    parser.add_argument(
        "--strategy-id",
        default="L1H9",
        help="配置對照使用的策略（如 L1H9）",
    )
    parser.add_argument(
        "--analyze-capital-cycle",
        action="store_true",
        help="僅跑資金週轉分析（需 --batch-id 或最近 matrix batch）",
    )
    parser.add_argument(
        "--analyze-fixed-slots",
        action="store_true",
        help="固定本金槽位 H 研究（需 matrix batch；搭配 --capital / --slots）",
    )
    parser.add_argument(
        "--analyze-rotation-capital",
        action="store_true",
        help="固定总本金 H 日轮动（每日 capital/H）；例 --capital 100000",
    )
    parser.add_argument(
        "--analyze-regime-horizon",
        action="store_true",
        help="Track A: Trend posture stratification · hold horizon (needs L1H matrix batch)",
    )
    parser.add_argument(
        "--analyze-leg-bucket-horizon",
        action="store_true",
        help="§11 L1-F1：leg 桶（1/2-4/5-10/11+）× H 矩陣",
    )
    parser.add_argument(
        "--analyze-l1-h3",
        action="store_true",
        help="§11 L1-H3：leg 桶 × H 交互檢定（H20−H9 配對 Δα）",
    )
    parser.add_argument(
        "--analyze-l1-policy",
        action="store_true",
        help="§11 L1-P1～P3：分桶持有政策 × 單池實現超額",
    )
    parser.add_argument(
        "--analyze-leg-decay",
        action="store_true",
        help="轨 B：Leg 级 forward α 衰减曲线（L1 · 每 leg 固定部署）",
    )
    parser.add_argument(
        "--analyze-event-exit",
        action="store_true",
        help="轨 C：事件驱动提前出场（H20 基准 + rotation 对照）",
    )
    parser.add_argument(
        "--analyze-leg-attribution",
        action="store_true",
        help="gap×p5d 动量归因（冠军/垫底日 · H-G1～G5）",
    )
    parser.add_argument(
        "--analyze-etf-compare",
        action="store_true",
        help="§4.4 跟單 vs 直接買 ETF（配對檢定 · rotation 对照）",
    )
    parser.add_argument(
        "--etf-slots-mode",
        default="rotation",
        choices=("rotation", "unconstrained"),
        help="ETF 对照资金模型（默认 rotation · 100k/H）",
    )
    parser.add_argument(
        "--baseline-h",
        type=int,
        default=20,
        help="轨 C 固定持有基准 H（默认 20）",
    )
    parser.add_argument(
        "--slots",
        type=int,
        default=None,
        help="固定槽位數（預設 floor(capital/per_signal)）",
    )
    parser.add_argument(
        "--per-signal",
        type=float,
        default=10_000.0,
        dest="per_signal_ntd",
        help="每訊號部署金額 NTD",
    )
    parser.add_argument(
        "--batch-id",
        default=None,
        help="指定 batch（資金週期分析 / 報告再生）",
    )
    parser.add_argument("--list-runs", action="store_true", help="列出 DB 既有 runs")
    args = parser.parse_args()

    conn = connect(args.db)
    try:
        if args.list_runs:
            for row in load_copytrade_runs(conn, etf_code=args.etf_code):
                print(
                    f"{row['run_id']}  {row['strategy_id']}  "
                    f"pnl={row['total_pnl_ntd']}  alpha={row['total_alpha_ntd']}  "
                    f"days={row['n_complete_days']}"
                )
            return 0

        if args.backfill_overnight_gaps:
            stats = backfill_copytrade_overnight_gaps(
                conn,
                args.etf_code,
                entry_lag_days=0,
                window_start=args.window_start,
                window_end=args.window_end,
            )
            print(
                f"overnight_gaps: rows={stats['n_rows']} complete={stats['n_complete']} "
                f"missing={stats['n_missing']}"
            )
            return 0

        if args.compare_hypothesis or args.compare_leg_count:
            from research.backtest.copytrade_hypothesis import (  # noqa: E402
                format_hypothesis_combined_markdown,
                format_leg_count_markdown,
                run_hypothesis_studies,
                run_leg_count_filter_study,
            )

            if args.compare_hypothesis:
                out = run_hypothesis_studies(
                    conn,
                    args.etf_code,
                    strategy_id=args.strategy_id,
                    capital_ntd=args.capital,
                    cost_bps=args.cost_bps,
                    window_start=args.window_start,
                    window_end=args.window_end,
                    persist=args.write_db,
                )
                r = out["H1"]
                print(f"H1 batch={r['batch_id']}")
                print(r["conclusion_zh"])
                print(f"  採納: {'是' if r['adopted_primary'] else '否'}")
                if args.write_report:
                    report = format_hypothesis_combined_markdown(out)
                    out_path = (
                        ROOT
                        / "reports"
                        / f"{date.today().strftime('%Y%m%d')}_00981a_hypothesis_{args.strategy_id.lower()}.md"
                    )
                    out_path.parent.mkdir(parents=True, exist_ok=True)
                    out_path.write_text(report, encoding="utf-8")
                    print(f"  report → {out_path}")
                return 0

            out = run_leg_count_filter_study(
                conn,
                args.etf_code,
                strategy_id=args.strategy_id,
                capital_ntd=args.capital,
                cost_bps=args.cost_bps,
                window_start=args.window_start,
                window_end=args.window_end,
                persist=args.write_db,
            )
            print(f"H1 batch={out['batch_id']}")
            print(out["conclusion_zh"])
            if args.write_report:
                out_path = (
                    ROOT
                    / "reports"
                    / f"{date.today().strftime('%Y%m%d')}_00981a_h1_legcount_{args.strategy_id.lower()}.md"
                )
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(format_leg_count_markdown(out), encoding="utf-8")
                print(f"  report → {out_path}")
            return 0

        if args.compare_982a_day:
            from research.backtest.copytrade_982a_day_filter import (  # noqa: E402
                format_982a_day_filter_markdown,
                run_982a_day_filter_study,
            )

            out = run_982a_day_filter_study(
                conn,
                args.etf_code,
                strategy_id=args.strategy_id,
                consensus_etf=args.consensus_etf,
                capital_ntd=args.capital,
                cost_bps=args.cost_bps,
                window_start=args.window_start,
                window_end=args.window_end,
                persist=args.write_db,
            )
            print(f"982a_day_filter batch={out['batch_id']}")
            print(out["conclusion_zh"])
            wr = out["win_rate_delta_pp"]
            print(
                f"  重疊日 {out['n_overlap_days']}（{out['capture_pct']}%）· "
                f"Δ勝率 {wr.get('day_982a_overlap'):+} pp · "
                f"Δ累計 α {out['primary_alpha_delta']:+,.0f} · "
                f"Δ单池 α {out['secondary_alpha_delta']:+,.0f} · "
                f"判決 {out['verdict']}"
            )
            if args.write_report:
                report = format_982a_day_filter_markdown(out)
                out_path = (
                    ROOT
                    / "reports"
                    / f"{date.today().strftime('%Y%m%d')}_00981a_982a_day_filter_{args.strategy_id.lower()}.md"
                )
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(report, encoding="utf-8")
                print(f"  report → {out_path}")
            return 0

        if args.compare_allocation:
            out = run_allocation_comparison(
                conn,
                args.etf_code,
                strategy_id=args.strategy_id,
                capital_ntd=args.capital,
                cost_bps=args.cost_bps,
                window_start=args.window_start,
                window_end=args.window_end,
                persist=args.write_db,
            )
            print(f"allocation_compare batch={out['batch_id']}")
            print(out["conclusion_zh"])
            pr = out["paired_return"]
            pa = out["paired_alpha_ntd"]
            print(
                f"  paired return% diff mean={pr.get('mean_diff')} p(W)={pr.get('p_value_wilcoxon')}"
            )
            print(
                f"  paired alpha_ntd diff mean={pa.get('mean_diff')} p(W)={pa.get('p_value_wilcoxon')}"
            )
            return 0

        if args.analyze_leg_bucket_horizon:
            from research.backtest.copytrade_leg_bucket_horizon import (
                format_leg_bucket_horizon_markdown,
                run_leg_bucket_horizon_study,
            )

            out = run_leg_bucket_horizon_study(
                conn,
                etf_code=args.etf_code,
                batch_id=args.batch_id,
                persist=True,
            )
            study_batch = out["batch_id"]
            l1_f1 = out["l1_f1"]
            print(f"leg_bucket_horizon batch={study_batch}")
            print(
                f"  L1-F1 5-10: H9 α={l1_f1['cum_alpha_baseline']:+,.0f} "
                f"win={l1_f1['win_rate_baseline_pct']}% → H20 "
                f"α={l1_f1['cum_alpha_candidate']:+,.0f} "
                f"win={l1_f1['win_rate_candidate_pct']}% "
                f"verdict={l1_f1['verdict']}"
            )
            for s in out["sweet_spots"]:
                print(
                    f"  bucket {s['bucket_value']}: sweet H{s['sweet_spot_h']}  "
                    f"alpha={s['sweet_spot_total_alpha_ntd']:+,.0f}"
                )
            if args.write_report:
                report = format_leg_bucket_horizon_markdown(
                    etf_code=args.etf_code,
                    batch_id=study_batch,
                    summary_rows=out["summary_rows"],
                    sweet_spots=out["sweet_spots"],
                    l1_f1=l1_f1,
                )
                out_path = (
                    ROOT
                    / "reports"
                    / f"{date.today().strftime('%Y%m%d')}_00981a_l1f1_leg_bucket_horizon.md"
                )
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(report, encoding="utf-8")
                print(f"  report → {out_path}")
            return 0

        if args.analyze_l1_h3:
            from research.backtest.copytrade_leg_bucket_horizon import (
                format_l1_h3_markdown,
                run_l1_h3_study,
            )

            out = run_l1_h3_study(
                conn,
                etf_code=args.etf_code,
                batch_id=args.batch_id,
                persist=True,
            )
            study_batch = out["batch_id"]
            h3 = out["l1_h3"]
            print(f"l1_h3 batch={study_batch}")
            print(f"  verdict={h3['verdict']}")
            print(f"  KW(Δα) p={h3['kruskal_wallis_alpha_p']}")
            for row in h3["bucket_rows"]:
                print(
                    f"  bucket {row['bucket']}: n={row['n_paired']} "
                    f"cum Δα={row['cum_alpha_delta']:+,.0f} "
                    f"mean Δα={row['mean_alpha_delta_ntd']}"
                )
            if args.write_report:
                report = format_l1_h3_markdown(h3)
                out_path = (
                    ROOT
                    / "reports"
                    / f"{date.today().strftime('%Y%m%d')}_00981a_l1h3_interaction.md"
                )
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(report, encoding="utf-8")
                print(f"  report → {out_path}")
            return 0

        if args.analyze_l1_policy:
            from research.backtest.copytrade_leg_bucket_horizon import (
                format_l1_policy_markdown,
                run_l1_policy_study,
            )

            out = run_l1_policy_study(
                conn,
                etf_code=args.etf_code,
                batch_id=args.batch_id,
                persist=True,
            )
            study_batch = out["batch_id"]
            single = out["single_pool"]
            print(f"l1_policy batch={study_batch}")
            print(f"  verdict={single['verdict']}")
            print(f"  best_recycled={single['best_recycled_policy_id']}")
            for r in single["policies"]:
                print(
                    f"  {r['policy_id']}: recycled={r['recycled_total_alpha_ntd']:+,.0f} "
                    f"total={r['total_alpha_ntd']:+,.0f} capture={r.get('signal_capture_pct')}%"
                )
            if args.write_report:
                report = format_l1_policy_markdown(single, slots_result=out["slots_9"])
                out_path = (
                    ROOT
                    / "reports"
                    / f"{date.today().strftime('%Y%m%d')}_00981a_l1policy.md"
                )
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(report, encoding="utf-8")
                print(f"  report → {out_path}")
            return 0

        if args.analyze_regime_horizon:
            from research.backtest.copytrade_regime_horizon import (
                format_regime_horizon_markdown,
                run_regime_horizon_analysis,
            )

            batch_id = args.batch_id
            if not batch_id:
                row = conn.execute(
                    """
                    SELECT batch_id FROM copytrade_runs
                    WHERE etf_code = ? AND batch_id IS NOT NULL
                    ORDER BY synced_at DESC LIMIT 1
                    """,
                    (args.etf_code,),
                ).fetchone()
                if not row:
                    print("ERROR: 無 batch_id", file=sys.stderr)
                    return 1
                batch_id = row["batch_id"]
            max_h = conn.execute(
                """
                SELECT MAX(hold_trading_days) AS mh FROM copytrade_runs
                WHERE batch_id = ? AND strategy_id LIKE 'L1H%'
                """,
                (batch_id,),
            ).fetchone()["mh"]
            max_horizon = int(max_h or args.max_hold)

            out = run_regime_horizon_analysis(
                conn,
                batch_id=batch_id,
                etf_code=args.etf_code,
                max_horizon=max_horizon,
                persist=True,
            )
            print(f"regime_horizon batch={batch_id} max_h={max_horizon}")
            for s in out["sweet_regime"]:
                print(
                    f"  regime {s['bucket_value']}: sweet H{s['sweet_spot_h']}  "
                    f"alpha={s['sweet_spot_total_alpha_ntd']:+,.0f}  n={s['n_signal_days_at_sweet']}"
                )
            for s in out["sweet_exposure"]:
                print(
                    f"  exposure {s['bucket_value']}: sweet H{s['sweet_spot_h']}  "
                    f"alpha={s['sweet_spot_total_alpha_ntd']:+,.0f}"
                )
            if args.write_report:
                report = format_regime_horizon_markdown(
                    etf_code=args.etf_code,
                    batch_id=batch_id,
                    summary_rows=out["summary_rows"],
                    label_rows=out["label_rows"],
                    sweet_spots=out["sweet_regime"] + out["sweet_exposure"],
                    max_horizon=max_horizon,
                )
                out_path = (
                    ROOT
                    / "reports"
                    / f"{date.today().strftime('%Y%m%d')}_00981a_regime_horizon_l1.md"
                )
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(report, encoding="utf-8")
                print(f"  report → {out_path}")
            return 0

        if args.analyze_leg_decay:
            from research.backtest.copytrade_leg_decay import (
                format_leg_decay_markdown,
                run_leg_decay_analysis,
            )

            batch_id = args.batch_id or (
                f"{args.etf_code.lower()}-leg-decay-{date.today().strftime('%Y%m%d')}"
            )
            out = run_leg_decay_analysis(
                conn,
                etf_code=args.etf_code,
                batch_id=batch_id,
                max_horizon=args.max_hold,
                leg_capital_ntd=args.per_signal_ntd,
                window_start=args.window_start,
                window_end=args.window_end,
                persist=True,
            )
            print(
                f"leg_decay batch={batch_id} legs={out['n_unique_legs']} "
                f"obs={len(out['observations'])} max_h={args.max_hold}"
            )
            for k in out["knees"]:
                if k["bucket_field"] == "all":
                    print(
                        f"  all: peak H{k['peak_mean_excess_h']} "
                        f"({k['peak_mean_excess_pct']:.3f}%) knee H{k['knee_h']} "
                        f"efficiency H{k.get('efficiency_h')}"
                    )
                elif k["bucket_field"] == "action":
                    print(
                        f"  {k['bucket_value']}: peak H{k['peak_mean_excess_h']} "
                        f"knee H{k['knee_h']}"
                    )
            if args.write_report:
                report = format_leg_decay_markdown(
                    etf_code=args.etf_code,
                    batch_id=batch_id,
                    curve_rows=out["curve_rows"],
                    knees=out["knees"],
                    max_horizon=args.max_hold,
                    leg_capital_ntd=args.per_signal_ntd,
                    n_obs=len(out["observations"]),
                )
                out_path = (
                    ROOT
                    / "reports"
                    / f"{date.today().strftime('%Y%m%d')}_00981a_leg_horizon_decay.md"
                )
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(report, encoding="utf-8")
                print(f"  report → {out_path}")
            return 0

        if args.analyze_event_exit:
            from research.backtest.copytrade_event_exit import (
                format_event_exit_markdown,
                run_event_exit_analysis,
            )

            batch_id = args.batch_id or (
                f"{args.etf_code.lower()}-event-exit-{date.today().strftime('%Y%m%d')}"
            )
            rotation_cap = args.capital if args.capital != 10_000.0 else 100_000.0
            out = run_event_exit_analysis(
                conn,
                etf_code=args.etf_code,
                batch_id=batch_id,
                baseline_h=args.baseline_h,
                leg_capital_ntd=args.per_signal_ntd,
                rotation_capital_ntd=rotation_cap,
                window_start=args.window_start,
                window_end=args.window_end,
                persist=True,
            )
            print(
                f"event_exit batch={batch_id} baseline_h={args.baseline_h} "
                f"rotation={rotation_cap:,.0f}"
            )
            for s in out["summaries"]:
                delta = s.get("vs_baseline_alpha_delta")
                rot = s.get("rotation_recycled_alpha_ntd")
                d_s = (
                    "—"
                    if s["policy_id"] == "baseline_h20"
                    else f"{delta:+,.0f}" if delta is not None else "—"
                )
                rot_s = f"{rot:+,.0f}" if rot is not None else "—"
                print(
                    f"  {s['policy_id']}: triggered={s['n_triggered']} "
                    f"total_α={s['total_alpha_ntd']:+,.0f} Δ={d_s} rot={rot_s}"
                )
            if args.write_report:
                report = format_event_exit_markdown(
                    etf_code=args.etf_code,
                    batch_id=batch_id,
                    summaries=out["summaries"],
                    baseline_h=args.baseline_h,
                    rotation_capital_ntd=rotation_cap,
                )
                out_path = (
                    ROOT
                    / "reports"
                    / f"{date.today().strftime('%Y%m%d')}_00981a_event_exit_l1h{args.baseline_h}.md"
                )
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(report, encoding="utf-8")
                print(f"  report → {out_path}")
            return 0

        if args.analyze_etf_compare:
            from research.backtest.copytrade_etf_compare import (
                format_etf_compare_markdown,
                run_etf_compare_analysis,
            )

            rotation_cap = args.capital if args.capital != 10_000.0 else 100_000.0
            batch_id = args.batch_id or (
                f"{args.etf_code.lower()}-etf-compare-"
                f"{args.strategy_id.lower()}-{date.today().strftime('%Y%m%d')}"
            )
            out = run_etf_compare_analysis(
                conn,
                etf_code=args.etf_code,
                strategy_id=args.strategy_id,
                batch_id=batch_id,
                capital_ntd=rotation_cap,
                slots_mode=args.etf_slots_mode,
                persist=True,
            )
            primary = out.get("rotation_executed") or out["all_signals"]
            print(
                f"etf_compare batch={batch_id} strategy={args.strategy_id} "
                f"verdict={out['verdict']} win%={primary.win_rate_pct} "
                f"W p={primary.p_value_wilcoxon} diff_gross={primary.diff_gross_ntd:+,.0f}"
            )
            if args.write_report:
                report = format_etf_compare_markdown(
                    etf_code=args.etf_code,
                    strategy_id=args.strategy_id,
                    batch_id=batch_id,
                    summary=out,
                )
                sid = args.strategy_id.lower()
                out_path = (
                    ROOT
                    / "reports"
                    / f"{date.today().strftime('%Y%m%d')}_00981a_etf_compare_{sid}.md"
                )
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(report, encoding="utf-8")
                print(f"  report → {out_path}")
            return 0

        if args.analyze_leg_attribution:
            from research.backtest.copytrade_leg_attribution import (
                format_leg_attribution_markdown,
                run_leg_attribution_analysis,
            )

            batch_id = args.batch_id or (
                f"{args.etf_code.lower()}-leg-attrib-"
                f"{args.strategy_id.lower()}-{date.today().strftime('%Y%m%d')}"
            )
            out = run_leg_attribution_analysis(
                conn,
                etf_code=args.etf_code,
                strategy_id=args.strategy_id,
                batch_id=batch_id,
                persist=True,
            )
            print(
                f"leg_attribution batch={batch_id} strategy={args.strategy_id} "
                f"legs={out['n_obs']} with_features={out['n_with_features']}"
            )
            for h in out["hypotheses"]:
                print(
                    f"  {h['hypothesis_id']}: {h['verdict']} · {h.get('summary_zh', '')}"
                )
            if args.write_report:
                report = format_leg_attribution_markdown(
                    etf_code=args.etf_code,
                    strategy_id=args.strategy_id,
                    batch_id=batch_id,
                    bucket_rows=out["bucket_rows"],
                    hypotheses=out["hypotheses"],
                    correlations=out["correlations"],
                    case_rows=out["case_rows"],
                    n_obs=int(out["n_obs"]),
                    n_with_features=int(out["n_with_features"]),
                )
                sid = args.strategy_id.lower()
                out_path = (
                    ROOT
                    / "reports"
                    / f"{date.today().strftime('%Y%m%d')}_00981a_leg_attribution_{sid}.md"
                )
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(report, encoding="utf-8")
                print(f"  report → {out_path}")
            return 0

        if args.analyze_rotation_capital:
            batch_id = args.batch_id
            if not batch_id:
                row = conn.execute(
                    """
                    SELECT batch_id FROM copytrade_runs
                    WHERE etf_code = ? AND batch_id IS NOT NULL
                    ORDER BY synced_at DESC LIMIT 1
                    """,
                    (args.etf_code,),
                ).fetchone()
                if not row:
                    print("ERROR: 無 batch_id（需先跑 --matrix）", file=sys.stderr)
                    return 1
                batch_id = row["batch_id"]

            rows = run_fixed_slots_analysis(
                conn,
                batch_id=batch_id,
                etf_code=args.etf_code,
                slots_mode="rotation",
                total_capital_ntd=args.capital,
                entry_rows=("L1",),
                persist=True,
            )
            ins = summarize_capital_cycle_insights(rows, "L1")
            print(f"rotation_capital batch={batch_id} capital={args.capital:,.0f}")
            if ins:
                sweet = int(ins["sweet_spot_h"])
                sr = next((r for r in rows if int(r["horizon"]) == sweet), None)
                print(
                    f"  L1 sweet H{sweet}  recycled_alpha="
                    f"{ins['sweet_spot_recycled_alpha_ntd']:+,.0f}  "
                    f"per_day={sr['per_signal_ntd']:,.0f}"
                    if sr
                    else f"  L1 sweet H{sweet}"
                )
                eff_h = int(ins.get("best_efficiency_h") or sweet)
                er = next((r for r in rows if int(r["horizon"]) == eff_h), None)
                if er:
                    print(
                        f"  best efficiency H{eff_h}  "
                        f"alpha/locked_day={er['alpha_per_locked_day']:.2f}"
                    )
            if args.write_report:
                report = format_rotation_capital_markdown(
                    rows,
                    etf_code=args.etf_code,
                    batch_id=batch_id,
                    total_capital_ntd=args.capital,
                )
                cap_k = int(args.capital // 1000)
                out_path = (
                    ROOT
                    / "reports"
                    / f"{date.today().strftime('%Y%m%d')}_00981a_rotation_{cap_k}k.md"
                )
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(report, encoding="utf-8")
                print(f"  report → {out_path}")
            return 0

        if args.analyze_fixed_slots:
            batch_id = args.batch_id
            if not batch_id:
                row = conn.execute(
                    """
                    SELECT batch_id FROM copytrade_runs
                    WHERE etf_code = ? AND batch_id IS NOT NULL
                    ORDER BY synced_at DESC LIMIT 1
                    """,
                    (args.etf_code,),
                ).fetchone()
                if not row:
                    print("ERROR: 無 batch_id（需先跑 --matrix）", file=sys.stderr)
                    return 1
                batch_id = row["batch_id"]

            n_slots = args.slots
            if n_slots is None:
                n_slots = max(1, int(args.capital // args.per_signal_ntd))

            single_rows, _ = run_capital_cycle_analysis(
                conn,
                batch_id=batch_id,
                etf_code=args.etf_code,
                capital_ntd=args.per_signal_ntd,
                persist=False,
            )
            fixed_rows = run_fixed_slots_analysis(
                conn,
                batch_id=batch_id,
                etf_code=args.etf_code,
                per_signal_ntd=args.per_signal_ntd,
                n_slots=n_slots,
                slots_mode="fixed",
                entry_rows=("L1",),
                persist=True,
            )
            match_rows = run_fixed_slots_analysis(
                conn,
                batch_id=batch_id,
                etf_code=args.etf_code,
                per_signal_ntd=args.per_signal_ntd,
                slots_mode="match_horizon",
                entry_rows=("L1",),
                persist=True,
            )

            ins_single = summarize_capital_cycle_insights(single_rows, "L1")
            ins_fixed = summarize_capital_cycle_insights(fixed_rows, "L1")
            ins_match = summarize_capital_cycle_insights(match_rows, "L1")

            print(f"fixed_capital_horizon batch={batch_id}")
            print(f"  slots={n_slots} per_signal={args.per_signal_ntd:,.0f}")
            if ins_single:
                print(
                    f"  A single-pool H{ins_single['sweet_spot_h']}  "
                    f"α={ins_single['sweet_spot_recycled_alpha_ntd']:+,.0f}"
                )
            if ins_fixed:
                print(
                    f"  B fixed-slots H{ins_fixed['sweet_spot_h']}  "
                    f"α={ins_fixed['sweet_spot_recycled_alpha_ntd']:+,.0f}"
                )
            if ins_match:
                print(
                    f"  C match-horizon H{ins_match['sweet_spot_h']}  "
                    f"α={ins_match['sweet_spot_recycled_alpha_ntd']:+,.0f}"
                )

            if args.write_report:
                report = format_fixed_capital_horizon_markdown(
                    etf_code=args.etf_code,
                    batch_id=batch_id,
                    per_signal_ntd=args.per_signal_ntd,
                    single_pool_rows=single_rows,
                    fixed_slot_rows=fixed_rows,
                    match_horizon_rows=match_rows,
                    n_slots=n_slots,
                )
                out_path = (
                    ROOT
                    / "reports"
                    / f"{date.today().strftime('%Y%m%d')}_00981a_horizon_fixed_capital.md"
                )
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(report, encoding="utf-8")
                print(f"  report → {out_path}")
            return 0

        if args.analyze_capital_cycle:
            batch_id = args.batch_id
            if not batch_id:
                row = conn.execute(
                    """
                    SELECT batch_id FROM copytrade_runs
                    WHERE etf_code = ? AND batch_id IS NOT NULL
                    ORDER BY synced_at DESC LIMIT 1
                    """,
                    (args.etf_code,),
                ).fetchone()
                if not row:
                    print("ERROR: 無 batch_id", file=sys.stderr)
                    return 1
                batch_id = row["batch_id"]
            max_hold = conn.execute(
                """
                SELECT MAX(hold_trading_days) AS mh FROM copytrade_runs
                WHERE batch_id = ?
                """,
                (batch_id,),
            ).fetchone()["mh"]
            cycle_rows, conclusions = run_capital_cycle_analysis(
                conn,
                batch_id=batch_id,
                etf_code=args.etf_code,
                capital_ntd=args.capital,
                max_hold=int(max_hold or 20),
                persist=True,
            )
            ins = summarize_capital_cycle_insights(cycle_rows, "L1")
            print(f"capital_cycle batch={batch_id} rows={len(cycle_rows)}")
            if ins:
                print(
                    f"  L1 sweet spot H{ins['sweet_spot_h']}  "
                    f"recycled_alpha={ins['sweet_spot_recycled_alpha_ntd']:+,.0f}"
                )
            for c in conclusions:
                if c.get("metric_key") in ("sweet_spot", "actionable"):
                    print(f"  [{c['analysis_type']}] {c['conclusion_zh'][:120]}...")
            if args.write_report:
                runs = conn.execute(
                    "SELECT * FROM copytrade_runs WHERE batch_id = ? ORDER BY strategy_id",
                    (batch_id,),
                ).fetchall()
                from research.backtest.copytrade_backtest import CopytradeRunResult

                results = [
                    CopytradeRunResult(
                        run_id=r["run_id"],
                        etf_code=r["etf_code"],
                        strategy_id=r["strategy_id"],
                        strategy_label=r["strategy_label"] or "",
                        capital_ntd=float(r["capital_ntd"]),
                        entry_lag_days=int(r["entry_lag_days"]),
                        hold_trading_days=int(r["hold_trading_days"]),
                        entry_price_mode=r["entry_price_mode"] or "open",
                        cost_bps=float(r["cost_bps"] or 0),
                        window_start=r["window_start"],
                        window_end=r["window_end"],
                        signal_days=[],
                        n_signal_days=int(r["n_signal_days"]),
                        n_complete_days=int(r["n_complete_days"]),
                        total_deployed_ntd=float(r["total_deployed_ntd"] or 0),
                        total_pnl_ntd=float(r["total_pnl_ntd"] or 0),
                        total_return_pct=r["total_return_pct"],
                        avg_day_return_pct=r["avg_day_return_pct"],
                        win_rate_pct=r["win_rate_pct"],
                        max_drawdown_pct=r["max_drawdown_pct"],
                        total_bench_return_pct=r["total_bench_return_pct"],
                        total_alpha_ntd=float(r["total_alpha_ntd"] or 0),
                        total_capm_alpha_ntd=float(r["total_capm_alpha_ntd"] or 0)
                        if r["total_capm_alpha_ntd"] is not None
                        else 0,
                        mean_excess_pct=r["mean_excess_pct"],
                        p_value_ttest=r["p_value_ttest"],
                        p_value_wilcoxon=r["p_value_wilcoxon"],
                        t_stat=r["t_stat"],
                        batch_id=batch_id,
                    )
                    for r in runs
                ]
                path = write_copytrade_report(
                    results,
                    etf_code=args.etf_code,
                    capital_ntd=args.capital,
                    cost_bps=args.cost_bps,
                    matrix=True,
                    max_hold=int(max_hold or 20),
                    batch_id=batch_id,
                    cycle_rows=cycle_rows,
                    conclusions=conclusions,
                )
                print(f"report → {path}")
            return 0

        include_l0 = not args.no_l0
        max_hold = args.max_hold if args.matrix else min(args.max_hold, 5)
        specs = resolve_strategy_specs(
            args.strategy,
            matrix=args.matrix,
            include_l0=include_l0,
            max_hold=max_hold,
        )
        if not specs:
            print("ERROR: 無有效 strategy", file=sys.stderr)
            return 1

        batch_id = None
        run_suffix = None
        if args.matrix:
            batch_id = (
                f"{args.etf_code.lower()}-copytrade-h{max_hold}-"
                f"{date.today().strftime('%Y%m%d')}"
            )
            run_suffix = f"h{max_hold}-{date.today().strftime('%Y%m%d')}"

        results = run_strategies(
            conn,
            args.etf_code,
            capital_ntd=args.capital,
            cost_bps=args.cost_bps,
            strategies=specs,
            window_start=args.window_start,
            window_end=args.window_end,
            persist=args.write_db,
            run_suffix=run_suffix,
            batch_id=batch_id,
        )

        print(
            f"strategies={len(results)}  max_hold={max_hold}  "
            f"batch={batch_id or '—'}"
        )
        for r in results[:5]:
            print(
                f"  {r.strategy_id}: gross={r.total_pnl_ntd:+,.0f}  "
                f"alpha={r.total_alpha_ntd:+,.0f}  "
                f"p(W)={r.p_value_wilcoxon}  n={r.n_complete_days}"
            )
        if len(results) > 5:
            print(f"  ... +{len(results) - 5} more")

        decay = build_horizon_decay_rows(results, args.etf_code)
        for row in ("L1", "L2", "L3"):
            ins = summarize_decay_insights(decay, row)
            if ins:
                fs = ins.get("first_significant_h")
                print(
                    f"  {row} decay: peak H{ins.get('peak_h')}  "
                    f"first_sig H{fs or '—'}"
                )

        if args.write_report:
            cycle_rows = None
            conclusions = None
            if batch_id:
                cycle_rows = [
                    dict(r)
                    for r in load_copytrade_capital_cycle(conn, batch_id)
                ]
                conclusions = [
                    dict(r)
                    for r in load_copytrade_research_conclusions(conn, batch_id)
                ]
            path = write_copytrade_report(
                results,
                etf_code=args.etf_code,
                capital_ntd=args.capital,
                cost_bps=args.cost_bps,
                matrix=args.matrix or len(specs) >= 15,
                max_hold=max_hold,
                batch_id=batch_id,
                cycle_rows=cycle_rows,
                conclusions=conclusions,
            )
            print(f"report → {path}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
