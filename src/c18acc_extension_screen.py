#!/usr/bin/env python3
"""C18acc extension overlay · 獨立盤中 screen（combo_spike · dry-run intent）。"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from c18acc_extension_overlay import (
    ExtensionAlert,
    append_overlay_log,
    overlay_enabled,
    poll_interval_for_now,
    scan_held_extension_alerts,
    write_overlay_snapshot,
)
from project_dotenv import load_project_dotenv
from report_paths import REPORTS_DIR
from research.backtest.finpilot_local_backtest import load_price_panels
from research.backtest.rrg_mono_score_swap_c import champion_score_swap_c_config
from rrg_mono_swap_accel_screen import (
    ALIGN_MODE,
    INTENTS_DIR,
    load_slot_state,
    sync_watchlist_kbar,
    _lot_shares,
    _env_flag,
    _now_local,
    _poll_window_ok,
)
from stock_db import DEFAULT_DB_PATH, connect

STRATEGY_ID = "rrg-mono-swap-accel-extension"


@dataclass
class ExtensionScreenResult:
    session_date: str
    polled_at: str
    poll_minute: str
    slots: list[dict[str, Any]] = field(default_factory=list)
    alerts: list[ExtensionAlert] = field(default_factory=list)
    skip_reason: str | None = None
    dry_run: bool = True
    kbar_sync_n: int = 0


def _limit_price(px: float) -> str:
    return f"{px:.2f}"


def _intent_market_type(quantity_shares: int) -> str:
    return "common" if quantity_shares >= 1000 else "odd"


def render_markdown(result: ExtensionScreenResult) -> str:
    mode = "dry-run" if result.dry_run else "live-metadata"
    lines = [
        f"# C18acc Extension overlay · {result.session_date}",
        "",
        f"> strategy `{STRATEGY_ID}` · **{mode}** · parent `rrg-mono-swap-accel` · combo_spike",
        "",
        f"- poll `{result.poll_minute}` · 持倉 **{len(result.slots)}** 槽 · kbar sync **{result.kbar_sync_n}** bars",
    ]
    if result.skip_reason:
        lines.append(f"- **SKIP**：{result.skip_reason}")
    lines.append("")
    if result.alerts:
        lines.append("## Extension 出場訊號")
        lines.append("")
        for a in result.alerts:
            lines.append(
                f"- **{a.stock_id}** {a.stock_name} · {a.exit_mode} @ {a.minute} "
                f"px={a.price:.2f} ext={a.ext_prev_pct:.1f}% · {a.note}"
            )
    else:
        lines.append("_本輪無 extension 出場訊號_")
    lines.extend(
        [
            "",
            "## 執行",
            "",
            "- 盤程：**獨立 launchd** `c18acc-extension-overlay` · 09:06–13:20",
            "- 下單：僅 intent · **人工確認後** `submit_intents.py`",
            "",
        ]
    )
    return "\n".join(lines)


def build_intent_batch(result: ExtensionScreenResult, *, budget: float, board_lot: bool) -> dict[str, Any] | None:
    if not result.alerts:
        return None
    intents: list[dict[str, Any]] = []
    for alert in result.alerts:
        qty = _lot_shares(alert.price, budget, board_lot=board_lot)
        if qty <= 0:
            continue
        intents.append(
            {
                "symbol": alert.stock_id,
                "side": "sell",
                "quantity_shares": qty,
                "price": _limit_price(alert.price),
                "price_type": "limit",
                "market_type": _intent_market_type(qty),
                "time_in_force": "rod",
                "order_type": "stock",
                "user_def": f"EXT:{alert.exit_mode}",
                "note": alert.note,
            }
        )
    if not intents:
        return None
    return {
        "schema_version": "order-intent-v1",
        "strategy_id": STRATEGY_ID,
        "as_of": result.session_date,
        "metadata": {
            "layer": "strategy",
            "parent_strategy": "rrg-mono-swap-accel",
            "align_mode": ALIGN_MODE,
            "poll_minute": result.poll_minute,
            "dry_run": result.dry_run,
            "alerts": [a.to_dict() for a in result.alerts],
        },
        "intents": intents,
    }


def write_outputs(result: ExtensionScreenResult, *, budget: float, board_lot: bool) -> tuple[Path, Path | None]:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = result.session_date.replace("-", "")
    md_path = REPORTS_DIR / f"{stamp}_c18acc_extension_daily.md"
    latest = REPORTS_DIR / "c18acc_extension_daily.md"
    md_path.write_text(render_markdown(result), encoding="utf-8")
    latest.write_text(md_path.read_text(encoding="utf-8"), encoding="utf-8")

    intent_path: Path | None = None
    batch = build_intent_batch(result, budget=budget, board_lot=board_lot)
    if batch is not None:
        INTENTS_DIR.mkdir(parents=True, exist_ok=True)
        intent_path = INTENTS_DIR / f"{STRATEGY_ID}_{result.session_date}.json"
        intent_path.write_text(json.dumps(batch, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return md_path, intent_path


def run_extension_screen(
    conn,
    *,
    session: str | None = None,
    dry_run: bool = True,
) -> ExtensionScreenResult:
    now = _now_local()
    session_date = session or now.date().isoformat()
    poll_minute = now.strftime("%H:%M")
    polled_at = now.isoformat(timespec="seconds")

    result = ExtensionScreenResult(
        session_date=session_date,
        polled_at=polled_at,
        poll_minute=poll_minute,
        dry_run=dry_run,
    )

    if not overlay_enabled():
        result.skip_reason = "RUN_C18ACC_EXTENSION_OVERLAY=0"
        return result
    if not _poll_window_ok(now):
        result.skip_reason = "outside poll window (Mon–Fri 09:00–13:20)"
        state = load_slot_state()
        result.slots = list(state.get("slots") or [])
        return result

    state = load_slot_state()
    slots = list(state.get("slots") or [])
    result.slots = slots
    if not slots:
        result.skip_reason = "no C18acc slots (empty portfolio)"
        return result

    close, _, _ = load_price_panels(conn)
    watch_ids = [str(p["stock_id"]) for p in slots]
    result.kbar_sync_n = sync_watchlist_kbar(conn, watch_ids, session_date)

    cfg = champion_score_swap_c_config()
    ext_interval = poll_interval_for_now(now)
    session_dates = close.index.astype(str).tolist()
    alerts = scan_held_extension_alerts(
        conn,
        slots=slots,
        session=session_date,
        poll_minute=poll_minute,
        full_dates=session_dates,
        min_hold_days=cfg.min_hold_days,
        poll_interval_min=ext_interval,
    )
    result.alerts = alerts
    append_overlay_log(session=session_date, poll_minute=poll_minute, alerts=alerts)
    write_overlay_snapshot(session=session_date, poll_minute=poll_minute, alerts=alerts)
    return result


def main(argv: list[str] | None = None) -> int:
    load_project_dotenv()
    parser = argparse.ArgumentParser(description="C18acc extension overlay · standalone screen")
    parser.add_argument("--date", help="session YYYY-MM-DD")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--live-intent", action="store_true")
    args = parser.parse_args(argv)

    budget = float(os.environ.get("C18ACC_BUDGET_TWD_PER_SLOT", "20000"))
    board_lot = _env_flag("C18ACC_BOARD_LOT", "0")
    dry_run = not args.live_intent

    conn = connect(args.db)
    try:
        result = run_extension_screen(conn, session=args.date, dry_run=dry_run)
    finally:
        conn.close()

    md_path, intent_path = write_outputs(result, budget=budget, board_lot=board_lot)
    print(f"Wrote {md_path}")
    if intent_path:
        print(f"Wrote {intent_path}")
    if result.alerts:
        print("C18ACC_EXTENSION_SIGNAL=1")
        for a in result.alerts:
            print(f"  alert {a.stock_id} {a.exit_mode} @ {a.minute}")
    if result.skip_reason:
        print(f"skip: {result.skip_reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
