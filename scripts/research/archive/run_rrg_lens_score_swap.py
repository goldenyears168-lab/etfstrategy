#!/usr/bin/env python3
"""RRG lens score-swap · Phase sweep（日線 Lens + 盤中 RRG 加權 · Top-N 升級換倉）。

用法：
  # Phase 1 · 單一 gate 全格掃描（平行 job 用）
  python scripts/run_rrg_lens_score_swap.py --gate mono_tier2 --sweep --out reports/research/rrg/swap_gate_mono_tier2.json

  # 單組參數
  python scripts/run_rrg_lens_score_swap.py --alpha 0.8 --max-slots 3 --gate lens_only

  # Phase 2/3 · 覆寫 fixed 維度
  python scripts/run_rrg_lens_score_swap.py --phase 2 --gate mono_tier2 --sweep
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import yaml  # noqa: E402

from research.backtest.rrg_lens_score_swap import SwapConfig, run_config_grid  # noqa: E402
from research.backtest.rrg_mono_backtest import run_breadth_zone_comparison  # noqa: E402
from report_paths import RESEARCH_RRG  # noqa: E402
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def _load_topic_config() -> dict:
    path = ROOT / "config" / "research.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return raw["topics"]["rrg-lens-score-swap"]


def _phase1_configs(topic: dict, *, gate: str | None) -> list[SwapConfig]:
    sweep = topic["sweep"]
    fixed = topic["fixed"]
    gates = [gate] if gate else list(sweep["candidate_gate"])
    configs: list[SwapConfig] = []
    for g, alpha, slots in itertools.product(gates, sweep["alpha"], sweep["max_slots"]):
        configs.append(
            SwapConfig(
                alpha=float(alpha),
                max_slots=int(slots),
                candidate_gate=str(g),
                rrg_length=int(fixed.get("rrg_length", 20)),
                rebalance_interval_min=int(fixed.get("rebalance_interval_min", 15)),
                confirm_bars=int(fixed.get("confirm_bars", 2)),
                swap_trigger=str(fixed.get("swap_trigger", "beat_held_best")),
                sell_leg=str(fixed.get("sell_leg", "held_worst")),
                exit_quadrants=tuple(fixed.get("exit_quadrants", ["weakening", "lagging"])),
                min_hold_days=int(fixed.get("min_hold_days", 1)),
                max_hold_days=int(fixed.get("max_hold_days", 7)),
                max_swaps_per_day=int(fixed.get("max_swaps_per_day", 1)),
                watchlist_pit=str(fixed.get("watchlist_pit", "prior_close_lens")),
                no_swap_before=str(fixed.get("no_swap_before", "09:30")),
            )
        )
    return configs


def _phase2_configs(
    topic: dict,
    *,
    gate: str,
    best: SwapConfig,
) -> list[SwapConfig]:
    min_hold = topic.get("sweep_phase2", {}).get("min_hold_days", [1, 2, 3, 7])
    swaps = topic.get("sweep_phase2", {}).get("max_swaps_per_day", [0, 1, 2])
    intervals = topic.get("sweep_phase2", {}).get("rebalance_interval_min", [5, 15, 30])
    configs: list[SwapConfig] = []
    for mh, ms, iv in itertools.product(min_hold, swaps, intervals):
        cfg = SwapConfig(**best.to_dict())
        cfg.candidate_gate = gate
        cfg.min_hold_days = int(mh)
        cfg.max_swaps_per_day = int(ms)
        cfg.rebalance_interval_min = int(iv)
        configs.append(cfg)
    return configs


def _phase3_configs(
    topic: dict,
    *,
    gate: str,
    best: SwapConfig,
) -> list[SwapConfig]:
    triggers = topic.get("sweep_phase3", {}).get(
        "swap_trigger", ["beat_held_best", "beat_held_median", "beat_held_worst"]
    )
    sell_legs = topic.get("sweep_phase3", {}).get("sell_leg", ["held_worst", "held_fastest_decay"])
    margins = topic.get("sweep_phase3", {}).get("score_margin", [0, 5, 10, 20])
    configs: list[SwapConfig] = []
    for trig, leg, margin in itertools.product(triggers, sell_legs, margins):
        cfg = SwapConfig(**best.to_dict())
        cfg.candidate_gate = gate
        cfg.swap_trigger = str(trig)
        cfg.sell_leg = str(leg)
        cfg.score_margin = float(margin)
        configs.append(cfg)
    return configs


def _config_from_best_json(best_raw: dict) -> SwapConfig:
    cfg_dict = (best_raw.get("best") or {}).get("config") or best_raw.get("config") or best_raw
    allowed = {f.name for f in SwapConfig.__dataclass_fields__.values()}
    cfg_kwargs = {k: v for k, v in cfg_dict.items() if k in allowed and k != "exit_quadrants"}
    best = SwapConfig(**cfg_kwargs)
    if "exit_quadrants" in cfg_dict:
        best.exit_quadrants = tuple(cfg_dict["exit_quadrants"])
    return best


def _apply_dual_gate(cfg: SwapConfig, topic: dict, *, gate: str) -> SwapConfig:
    diag = topic.get("sweep_swap_diagnostic", {})
    if diag.get("entry_gate") or diag.get("swap_gate"):
        cfg.entry_gate = str(diag.get("entry_gate") or gate)
        cfg.swap_gate = str(diag.get("swap_gate") or "lens_only")
    return cfg


def _swap_diagnostic_configs(topic: dict, *, gate: str, best: SwapConfig) -> list[SwapConfig]:
    diag = topic.get("sweep_swap_diagnostic", {})
    triggers = diag.get("swap_trigger", ["beat_held_worst", "beat_held_median", "beat_held_best"])
    confirm = diag.get("confirm_bars", [1, 2])
    swaps_day = diag.get("max_swaps_per_day", [1, 2, 99])
    margins = diag.get("score_margin", [0])
    configs: list[SwapConfig] = []
    baseline = SwapConfig(**best.to_dict())
    baseline.max_swaps_per_day = 0
    _apply_dual_gate(baseline, topic, gate=gate)
    configs.append(baseline)
    for trig, cb, ms, margin in itertools.product(triggers, confirm, swaps_day, margins):
        cfg = SwapConfig(**best.to_dict())
        cfg.candidate_gate = gate
        cfg.swap_trigger = str(trig)
        cfg.confirm_bars = int(cb)
        cfg.max_swaps_per_day = int(ms)
        cfg.score_margin = float(margin)
        cfg.min_hold_days = int(diag.get("min_hold_days", 1))
        _apply_dual_gate(cfg, topic, gate=gate)
        configs.append(cfg)
    return configs


def _pick_best(summaries: list[dict]) -> dict:
    ranked = sorted(
        summaries,
        key=lambda s: (
            -(s.get("mean_excess_pct") or -999.0),
            -(s.get("win_rate_vs_bench_pct") or 0.0),
            -(s.get("n_periods") or 0),
        ),
    )
    return ranked[0] if ranked else {}


def _pick_best_swap_diagnostic(summaries: list[dict]) -> dict:
    with_swaps = [s for s in summaries if int(s.get("swaps_total") or 0) > 0]
    if with_swaps:
        return sorted(
            with_swaps,
            key=lambda s: (-(s.get("mean_excess_pct") or -999.0), -(s.get("swaps_total") or 0)),
        )[0]
    return _pick_best(summaries)


def _render_markdown(
    *,
    date_start: str,
    date_end: str,
    baseline: dict | None,
    summaries: list[dict],
    phase: int,
    gate: str | None,
    swap_diagnostic: bool = False,
) -> str:
    lines = [
        f"# RRG lens score-swap · Phase {phase}",
        "",
        f"區間：{date_start} .. {date_end}",
        f"gate filter：{gate or 'all'}",
        "",
    ]
    if baseline:
        b = baseline.get("pooled_all", {}).get("summary", {})
        lines += [
            "## 對照基準 · rrg-mono-hold7",
            "",
            f"- 成交筆數：{b.get('n_periods')}",
            f"- 均超額%：{b.get('mean_excess_pct')}",
            f"- 勝率%：{b.get('win_rate_vs_bench_pct')}",
            "",
        ]
    lines += [
        "## Sweep 排名（依均超額）",
        "",
    ]
    if swap_diagnostic:
        lines += [
            "| rank | entry | swap | trigger | confirm | swaps/d | margin | n | 均超額% | swaps | full_chk | kbar% |",
            "|------|-------|------|---------|---------|---------|--------|---|---------|-------|----------|-------|",
        ]
        ranked = sorted(
            summaries,
            key=lambda s: (
                -(int(s.get("swaps_total") or 0) > 0),
                -(s.get("mean_excess_pct") or -999.0),
                -(s.get("swaps_total") or 0),
            ),
        )
        for i, s in enumerate(ranked[:30], start=1):
            cfg = s.get("config") or {}
            entry_g = cfg.get("entry_gate") or cfg.get("candidate_gate")
            swap_g = cfg.get("swap_gate") or cfg.get("candidate_gate")
            lines.append(
                f"| {i} | {entry_g} | {swap_g} | {cfg.get('swap_trigger')} | {cfg.get('confirm_bars')} "
                f"| {cfg.get('max_swaps_per_day')} | {cfg.get('score_margin')} "
                f"| {s.get('n_periods')} | {s.get('mean_excess_pct')} | {s.get('swaps_total')} "
                f"| {s.get('full_slot_checkpoints')} | {s.get('kbar_coverage_pct')} |"
            )
    else:
        lines += [
            "| rank | alpha | slots | gate | n | 均超額% | 勝率% | swaps | kbar% |",
            "|------|-------|-------|------|---|---------|-------|-------|-------|",
        ]
        ranked = sorted(
            summaries,
            key=lambda s: (-(s.get("mean_excess_pct") or -999.0), -(s.get("n_periods") or 0)),
        )
        for i, s in enumerate(ranked[:25], start=1):
            cfg = s.get("config") or {}
            lines.append(
                f"| {i} | {cfg.get('alpha')} | {cfg.get('max_slots')} | {cfg.get('candidate_gate')} "
                f"| {s.get('n_periods')} | {s.get('mean_excess_pct')} | {s.get('win_rate_vs_bench_pct')} "
                f"| {s.get('swaps_total')} | {s.get('kbar_coverage_pct')} |"
            )
    best = _pick_best_swap_diagnostic(summaries) if swap_diagnostic else (ranked[0] if ranked else {})
    if best:
        lines += [
            "",
            "## Phase 冠軍",
            "",
            f"```json\n{json.dumps(best, ensure_ascii=False, indent=2)}\n```",
        ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    topic = _load_topic_config()
    parser = argparse.ArgumentParser(description="RRG lens score-swap sweep")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--date-start", default="2026-01-01")
    parser.add_argument("--date-end", default="2026-06-22")
    parser.add_argument("--gate", default=None, help="只跑單一 candidate_gate（平行 job）")
    parser.add_argument("--alpha", type=float, default=None)
    parser.add_argument("--max-slots", type=int, default=None)
    parser.add_argument("--phase", type=int, default=1, choices=(1, 2, 3, 4))
    parser.add_argument("--sweep", action="store_true", help="跑 phase 全格")
    parser.add_argument("--best-config", type=Path, help="Phase 2/3 起點（JSON 冠軍 config）")
    parser.add_argument("--baseline", action="store_true", help="一併跑 rrg-mono-hold7 基準")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--md", type=Path, default=None)
    args = parser.parse_args(argv)

    conn = connect(args.db)
    try:
        baseline = None
        if args.baseline:
            baseline = run_breadth_zone_comparison(
                conn,
                date_start=args.date_start,
                date_end=args.date_end,
            )

        if args.sweep:
            if args.phase == 1:
                configs = _phase1_configs(topic, gate=args.gate)
            elif args.phase == 4:
                if not args.best_config or not args.gate:
                    parser.error("Phase 4（swap diagnostic）需要 --gate 與 --best-config")
                best = _config_from_best_json(
                    json.loads(args.best_config.read_text(encoding="utf-8"))
                )
                configs = _swap_diagnostic_configs(topic, gate=args.gate, best=best)
            elif args.phase in (2, 3):
                if not args.best_config or not args.gate:
                    parser.error("Phase 2/3 需要 --gate 與 --best-config")
                best = _config_from_best_json(
                    json.loads(args.best_config.read_text(encoding="utf-8"))
                )
                if args.phase == 2:
                    configs = _phase2_configs(topic, gate=args.gate, best=best)
                else:
                    configs = _phase3_configs(topic, gate=args.gate, best=best)
            else:
                configs = []
            summaries = run_config_grid(
                conn,
                date_start=args.date_start,
                date_end=args.date_end,
                configs=configs,
            )
        else:
            fixed = topic["fixed"]
            cfg = SwapConfig(
                alpha=args.alpha if args.alpha is not None else 0.75,
                max_slots=args.max_slots if args.max_slots is not None else 3,
                candidate_gate=args.gate or "mono_tier2",
                rrg_length=int(fixed.get("rrg_length", 20)),
                rebalance_interval_min=int(fixed.get("rebalance_interval_min", 15)),
                confirm_bars=int(fixed.get("confirm_bars", 2)),
                swap_trigger=str(fixed.get("swap_trigger", "beat_held_best")),
                sell_leg=str(fixed.get("sell_leg", "held_worst")),
                exit_quadrants=tuple(fixed.get("exit_quadrants", ["weakening", "lagging"])),
                min_hold_days=int(fixed.get("min_hold_days", 1)),
                max_hold_days=int(fixed.get("max_hold_days", 7)),
                max_swaps_per_day=int(fixed.get("max_swaps_per_day", 1)),
            )
            summaries = run_config_grid(
                conn,
                date_start=args.date_start,
                date_end=args.date_end,
                configs=[cfg],
            )

        stamp = date.today().strftime("%Y%m%d")
        gate_slug = args.gate or "all"
        out = args.out or RESEARCH_RRG / f"{stamp}_lens_score_swap_p{args.phase}_{gate_slug}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        pick = _pick_best_swap_diagnostic if args.phase == 4 else _pick_best
        payload = {
            "track_id": "rrg-lens-score-swap",
            "phase": args.phase,
            "date_start": args.date_start,
            "date_end": args.date_end,
            "gate": args.gate,
            "baseline_hold7": baseline.get("pooled_all", {}).get("summary") if baseline else None,
            "summaries": summaries,
            "best": pick(summaries),
            "best_with_swaps": _pick_best_swap_diagnostic(summaries) if args.phase == 4 else None,
            "n_configs_with_swaps": sum(1 for s in summaries if int(s.get("swaps_total") or 0) > 0),
        }
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Wrote {out}")

        md_path = args.md or out.with_suffix(".md")
        md_path.write_text(
            _render_markdown(
                date_start=args.date_start,
                date_end=args.date_end,
                baseline=baseline,
                summaries=summaries,
                phase=args.phase,
                gate=args.gate,
                swap_diagnostic=args.phase == 4,
            ),
            encoding="utf-8",
        )
        print(f"Wrote {md_path}")

        best = payload["best"]
        if best:
            cfg = best.get("config") or {}
            print(
                f"Best: gate={cfg.get('candidate_gate')} "
                f"alpha={cfg.get('alpha')} slots={cfg.get('max_slots')} "
                f"trigger={cfg.get('swap_trigger')} swaps/d={cfg.get('max_swaps_per_day')} "
                f"mean_excess={best.get('mean_excess_pct')} swaps={best.get('swaps_total')} "
                f"full_chk={best.get('full_slot_checkpoints')} n={best.get('n_periods')}"
            )
        if args.phase == 4:
            print(f"Configs with swaps>0: {payload['n_configs_with_swaps']}/{len(summaries)}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
