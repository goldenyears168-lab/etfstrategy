#!/usr/bin/env python3
"""VCP funnel slot backtest — hold7 · entry_ready hold20 · Pivot Gate · Coil Close."""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from research.backtest.chunge_funnel_backtest import (  # noqa: E402
    ENTRY_READY_EXECUTION_STATES,
    ENTRY_READY_HOLD20_DEFAULTS,
    ENTRY_READY_PIVOT_STOP_DEFAULTS,
    VCP_COIL_CLOSE,
    VCP_PIVOT_GATE,
    render_chunge_backtest_markdown,
    run_chunge_slot_backtest,
)
from report_paths import RESEARCH_VCP  # noqa: E402
from research.backtest.slot_backtest_summary import (  # noqa: E402
    SlotBacktestConfig,
    build_summary_payload,
    write_slot_backtest_summary,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def _year_tag(date_start: str, date_end: str) -> str:
    if date_start[:4] == date_end[:4]:
        return date_start[:4]
    return f"{date_start[:4]}_{date_end[:4]}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="VCP funnel slot backtest")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--date-start", default="2026-01-01")
    parser.add_argument("--date-end", default="2026-12-31")
    parser.add_argument("--n-slots", type=int, default=None)
    parser.add_argument("--hold-days", type=int, default=None)
    parser.add_argument("--min-composite", type=float, default=45.0)
    parser.add_argument(
        "--entry-ready",
        action="store_true",
        help="VCP-faithful：entry_ready=1 · Pre/Breakout · 預設 5 槽 hold20",
    )
    parser.add_argument(
        "--pivot-gate",
        "--calibrated",
        action="store_true",
        dest="pivot_gate",
        help="VCP Pivot Gate（原 #7）：near pivot · breakout_close · 5槽 hold20",
    )
    parser.add_argument(
        "--coil-close",
        action="store_true",
        help="VCP Coil Close（原 sweep #1）：near pivot · 訊號日 close · 5槽 hold20",
    )
    parser.add_argument(
        "--pivot-stop",
        action="store_true",
        help="需搭配 --entry-ready：pivot 突破進場 · 停損或 hold 時間出場",
    )
    parser.add_argument("--max-entry-wait", type=int, default=None, help="pivot 掛單最長等待交易日")
    parser.add_argument("--output-md", type=Path, default=None)
    parser.add_argument("--output-json", type=Path, default=None)
    parser.add_argument("--write-timeline", action="store_true")
    parser.add_argument("--timeline-output", type=Path, default=None)
    args = parser.parse_args(argv)

    if args.pivot_stop and not args.entry_ready and not args.pivot_gate and not args.coil_close:
        parser.error("--pivot-stop 需搭配 --entry-ready")

    year = _year_tag(args.date_start, args.date_end)

    if args.coil_close:
        spec = VCP_COIL_CLOSE
        n_slots = args.n_slots if args.n_slots is not None else spec["n_slots"]
        hold_days = args.hold_days if args.hold_days is not None else spec["hold_days"]
        states = spec["execution_states"]
        variant = spec["variant"]
        entry_mode = spec["entry_price_mode"]
        max_wait = spec["max_entry_wait_days"]
        stop_lb = spec["stop_lookback_days"]
        min_comp = args.min_composite if args.min_composite != 45.0 else spec["min_composite"]
        entry_ready = spec["entry_ready_only"]
        require_pivot = spec["require_pivot"]
        min_dist = spec["min_dist_pivot_pct"]
        max_dist = spec["max_dist_pivot_pct"]
        default_json = RESEARCH_VCP / f"vcp_coil_close_slot_backtest_{year}.json"
        default_md_suffix = "coil_close"
    elif args.pivot_gate:
        gate = VCP_PIVOT_GATE
        n_slots = args.n_slots if args.n_slots is not None else gate["n_slots"]
        hold_days = args.hold_days if args.hold_days is not None else gate["hold_days"]
        states = gate["execution_states"]
        variant = gate["variant"]
        entry_mode = gate["entry_price_mode"]
        max_wait = gate["max_entry_wait_days"]
        stop_lb = gate["stop_lookback_days"]
        min_comp = args.min_composite if args.min_composite != 45.0 else gate["min_composite"]
        entry_ready = gate["entry_ready_only"]
        require_pivot = gate["require_pivot"]
        min_dist = gate["min_dist_pivot_pct"]
        max_dist = gate["max_dist_pivot_pct"]
        default_json = RESEARCH_VCP / f"vcp_pivot_gate_slot_backtest_{year}.json"
        default_md_suffix = "pivot_gate"
    elif args.pivot_stop:
        n_slots = args.n_slots if args.n_slots is not None else ENTRY_READY_PIVOT_STOP_DEFAULTS["n_slots"]
        hold_days = args.hold_days if args.hold_days is not None else ENTRY_READY_PIVOT_STOP_DEFAULTS["hold_days"]
        states = ENTRY_READY_EXECUTION_STATES
        variant = "entry_ready_pivot_stop"
        entry_mode = "pivot_stop"
        max_wait = (
            args.max_entry_wait
            if args.max_entry_wait is not None
            else ENTRY_READY_PIVOT_STOP_DEFAULTS["max_entry_wait_days"]
        )
        stop_lb = ENTRY_READY_PIVOT_STOP_DEFAULTS["stop_lookback_days"]
        default_json = RESEARCH_VCP / "chunge_funnel_entry_ready_pivot_stop_slot_backtest_2026.json"
        default_md_suffix = "entry_ready_pivot_stop"
        min_comp = args.min_composite
        entry_ready = True
        require_pivot = False
        min_dist = None
        max_dist = None
    elif args.entry_ready:
        n_slots = args.n_slots if args.n_slots is not None else ENTRY_READY_HOLD20_DEFAULTS["n_slots"]
        hold_days = args.hold_days if args.hold_days is not None else ENTRY_READY_HOLD20_DEFAULTS["hold_days"]
        states = ENTRY_READY_EXECUTION_STATES
        variant = "entry_ready_hold20"
        entry_mode = "close"
        max_wait = 10
        stop_lb = 20
        default_json = RESEARCH_VCP / "chunge_funnel_entry_ready_hold20_slot_backtest_2026.json"
        default_md_suffix = "entry_ready_hold20"
        min_comp = args.min_composite
        entry_ready = True
        require_pivot = False
        min_dist = None
        max_dist = None
    else:
        n_slots = args.n_slots if args.n_slots is not None else 3
        hold_days = args.hold_days if args.hold_days is not None else 7
        states = (
            "Pre-breakout",
            "Breakout",
            "Overextended",
            "Extended",
        )
        variant = "hold7"
        entry_mode = "close"
        max_wait = 10
        stop_lb = 20
        default_json = RESEARCH_VCP / "chunge_funnel_hold7_slot_backtest_2026.json"
        default_md_suffix = "hold7"
        min_comp = args.min_composite
        entry_ready = False
        require_pivot = False
        min_dist = None
        max_dist = None

    cfg = SlotBacktestConfig(
        date_start=args.date_start,
        date_end=args.date_end,
        n_slots=n_slots,
        hold_days=hold_days,
        min_composite=min_comp,
        model_id="vcp-funnel",
        execution_states=states,
        entry_ready_only=entry_ready,
        require_pivot=require_pivot,
        min_dist_pivot_pct=min_dist,
        max_dist_pivot_pct=max_dist,
        variant=variant,
        entry_price_mode=entry_mode,
        max_entry_wait_days=max_wait,
        stop_lookback_days=stop_lb,
        source_summary=str(args.output_json or default_json),
    )

    conn = connect(args.db)
    try:
        if args.coil_close:
            label = f"VCP Coil Close hold{hold_days}"
        elif args.pivot_gate:
            label = f"VCP Pivot Gate hold{hold_days}"
        elif args.pivot_stop:
            label = f"entry_ready pivot/stop hold{hold_days}"
        elif args.entry_ready:
            label = f"entry_ready hold{hold_days}"
        else:
            label = f"hold{hold_days}"
        print(
            f"Running vcp-funnel × {cfg.n_slots}-slot {label} "
            f"({cfg.date_start}..{cfg.date_end})..."
        )
        result = run_chunge_slot_backtest(conn, config=cfg)
    finally:
        conn.close()

    md = render_chunge_backtest_markdown(result)
    stamp = date.today().strftime("%Y%m%d")
    md_path = args.output_md or RESEARCH_VCP / f"{stamp}_vcp_funnel_{default_md_suffix}_backtest.md"
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(md, encoding="utf-8")
    print(f"Wrote {md_path}")
    print()
    print(md)

    json_path = Path(cfg.source_summary or default_json)
    payload = build_summary_payload(
        track_id="vcp-pivot-gate",
        config=cfg,
        summary=result["summary"],
        source_module="chunge_funnel_backtest",
        extra={
            "model_id": result.get("model_id"),
            "execution_states": result.get("execution_states"),
            "entry_ready_only": result.get("entry_ready_only"),
            "variant": result.get("variant"),
            "max_entry_wait_days": cfg.max_entry_wait_days,
        },
    )
    write_slot_backtest_summary(json_path, payload)
    print(f"Wrote {json_path}")

    if args.write_timeline:
        if args.pivot_stop and not args.pivot_gate:
            print("Note: pivot/stop timeline 尚未實作，略過 --write-timeline", file=sys.stderr)
        else:
            cmd = [
                sys.executable,
                str(ROOT / "scripts/render_rrg_universe_html.py"),
                "--chunge-funnel-slots-timeline",
                "--date-from",
                cfg.date_start,
                "--date-to",
                cfg.date_end,
                "--db",
                str(args.db),
                "--n-slots",
                str(cfg.n_slots),
                "--hold-days",
                str(cfg.hold_days),
            ]
            if args.pivot_gate:
                cmd.append("--vcp-pivot-gate")
            elif args.coil_close:
                cmd.append("--vcp-coil-close")
            elif args.entry_ready:
                cmd.append("--chunge-entry-ready")
            if args.timeline_output:
                cmd.extend(["--output", str(args.timeline_output)])
            subprocess.run(cmd, check=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
