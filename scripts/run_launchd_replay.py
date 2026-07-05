#!/usr/bin/env python3
"""Replay intraday launchd jobs for one session · separate logs per job.

預設只 replay buy-signal-radar + sell-signal-radar（純訊號 · 無槽位）。
rrg-c18acc-poll 槽位策略需明確 --only c18acc。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from project_dotenv import load_project_dotenv
from rrg_mono_swap_accel_screen import run_screen, ScreenResult
from stock_db import DEFAULT_DB_PATH, connect
from strategy_signal_radar import (
    BUY_STATE_PATH,
    DEDUP_PATH,
    format_buy_radar_log,
    format_sell_radar_log,
    run_buy_signal_radar,
    run_sell_signal_radar,
    write_radar_report,
)

_TZ = ZoneInfo("Asia/Taipei")
REPLAY_DIR = ROOT / "logs" / "replay"

DEFAULT_C18ACC_SLOTS_20260626 = {
    "schema_version": 2,
    "session_date": "2026-06-26",
    "slots": [
        {
            "stock_id": "6488",
            "stock_name": "環球晶",
            "entry_date": "2026-06-25",
            "signal_date": "2026-06-25",
            "quantity_shares": 100,
        },
        {
            "stock_id": "3711",
            "stock_name": "日月光投控",
            "entry_date": "2026-06-25",
            "signal_date": "2026-06-25",
            "quantity_shares": 100,
        },
        {
            "stock_id": "2344",
            "stock_name": "華邦電子",
            "entry_date": "2026-06-25",
            "signal_date": "2026-06-25",
            "quantity_shares": 1000,
        },
    ],
    "swaps_today": 0,
    "entries_today": [],
    "history": [],
}


def _poll_times(start_h: int, start_m: int, end_h: int, end_m: int, interval: int = 5) -> list[str]:
    out: list[str] = []
    total = start_h * 60 + start_m
    end_total = end_h * 60 + end_m
    while total <= end_total:
        hh, mm = divmod(total, 60)
        out.append(f"{hh:02d}:{mm:02d}")
        total += interval
    return out


def _dt(session: str, hhmm: str) -> datetime:
    return datetime(
        int(session[:4]), int(session[5:7]), int(session[8:10]),
        int(hhmm[:2]), int(hhmm[3:5]), tzinfo=_TZ,
    )


def _format_c18acc_tick(result: ScreenResult, tick_label: str) -> str:
    signal = 1 if result.actions else 0
    lines = [
        "",
        f"=== launchd rrg-c18acc-poll tick {tick_label} ===",
        (
            f"C18acc screen: report=(replay) pool_as_of={result.pool_as_of or '—'} "
            f"fresh_pool_n={len(result.mono_top10)} minute={result.poll_minute} "
            f"kbar={result.kbar_sync_n} actions={len(result.actions)} "
            f"ext_alerts={len(result.extension_alerts)} dry_run={result.dry_run} "
            f"skip={result.skip_reason or '—'} tick_log=(replay)"
        ),
    ]
    for a in result.extension_alerts:
        lines.append(
            f"EXT_OVERLAY {a.stock_id} heat={a.heat_score} @ {a.minute} px={a.price:.2f} ({a.note})"
        )
    for act in result.actions:
        lines.append(
            f"C18acc intent: {act.kind} {act.side} {act.stock_id} {act.quantity_shares} @ {act.price:.2f} · {act.note}"
        )
    lines.append(f"C18ACC_SIGNAL={signal} actions={len(result.actions)}")
    lines.append(f"=== launchd rrg-c18acc-poll end exit=0 {tick_label} ===")
    return "\n".join(lines)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Replay launchd intraday jobs for one session")
    p.add_argument("--date", default="2026-06-26", metavar="YYYY-MM-DD")
    p.add_argument("--holdings", default="", help="（已廢棄 · 賣方不再使用持倉 overlay）")
    p.add_argument(
        "--only",
        choices=("all", "c18acc", "buy", "sell"),
        default="all",
        help="只跑指定 job（預設 all = buy+sell · 不含 rrg-c18acc-poll）",
    )
    return p.parse_args()


def _setup_replay_fast() -> None:
    os.environ["SIGNAL_RADAR_REPLAY_FAST"] = "1"
    from strategy_signal_radar import clear_signal_radar_replay_caches

    clear_signal_radar_replay_caches()


def _setup_buy_replay(stamp: str) -> None:
    os.environ["RUN_BUY_SIGNAL_RADAR"] = "1"
    os.environ["C18ACC_KBAR_SYNC"] = "0"
    _setup_replay_fast()
    import strategy_signal_radar as radar_mod

    radar_mod.DEDUP_PATH = REPLAY_DIR / f"signal_radar_dedup_{stamp}.json"
    radar_mod.BUY_STATE_PATH = REPLAY_DIR / f"signal_radar_buy_state_{stamp}.json"


def _setup_sell_replay(stamp: str) -> None:
    os.environ["RUN_SELL_SIGNAL_RADAR"] = "1"
    os.environ["RUN_C18ACC_EXTENSION_OVERLAY"] = "1"
    os.environ["C18ACC_KBAR_SYNC"] = "0"
    _setup_replay_fast()
    import strategy_signal_radar as radar_mod

    radar_mod.DEDUP_PATH = REPLAY_DIR / f"signal_radar_dedup_{stamp}.json"


def _setup_c18acc_replay(slots_path: Path) -> None:
    os.environ["RUN_RRG_C18ACC_SCREEN"] = "1"
    os.environ["ORDER_C18ACC_DRY_RUN"] = "1"
    os.environ["ORDER_C18ACC_APPLY_STATE"] = "0"
    os.environ["RUN_RRG_C18ACC_EMAIL"] = "0"
    os.environ["RUN_C18ACC_EXTENSION_OVERLAY"] = "0"
    os.environ["C18ACC_KBAR_SYNC"] = "0"

    import rrg_mono_swap_accel_screen as c18

    c18.STATE_PATH = slots_path


def replay_buy(conn, session: str, stamp: str) -> Path:
    out = REPLAY_DIR / f"launchd_buy-signal-radar_{stamp}.log"
    times = _poll_times(9, 0, 13, 20)
    _setup_buy_replay(stamp)

    lines = [
        f"# Replay · buy-signal-radar · {session}",
        f"# 週一～五 09:00–13:20 · 每 5 分 · 對應 logs/intraday/launchd_buy-signal-radar.log",
        "",
    ]
    for t in times:
        now = _dt(session, t)
        tick = now.strftime("%Y-%m-%d %H:%M:%S")
        lines.append("")
        result = run_buy_signal_radar(conn, session_date=session, now=now, mark_dedup=True)
        lines.append(format_buy_radar_log(result))
        lines.append(f"BUY_SIGNAL_RADAR={'1' if result.new_signals else '0'}")
        if result.new_signals:
            path = write_radar_report("buy", result.new_signals, session_date=session, poll_minute=result.poll_minute)
            lines.append(f"報告：{path}")

    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def replay_sell(conn, session: str, stamp: str) -> Path:
    out = REPLAY_DIR / f"launchd_sell-signal-radar_{stamp}.log"
    times = _poll_times(9, 6, 13, 20)
    _setup_sell_replay(stamp)

    lines = [
        f"# Replay · sell-signal-radar · {session}",
        f"# 週一～五 09:06–13:20 · 每 5 分 · 宇宙 extension 純訊號（無持倉 overlay）",
        f"# 對應 logs/intraday/launchd_sell-signal-radar.log",
        "",
    ]
    for t in times:
        now = _dt(session, t)
        result = run_sell_signal_radar(conn, session_date=session, now=now, mark_dedup=True)
        lines.append("")
        lines.append(format_sell_radar_log(result))
        lines.append(f"SELL_SIGNAL_RADAR={'1' if result.new_signals else '0'}")
        if result.new_signals:
            path = write_radar_report("sell", result.new_signals, session_date=session, poll_minute=result.poll_minute)
            lines.append(f"報告：{path}")

    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def replay_c18acc(conn, session: str, stamp: str) -> Path:
    out = REPLAY_DIR / f"launchd_rrg-c18acc-poll_{stamp}.log"
    tick_out = REPLAY_DIR / f"rrg_c18acc_poll_tick_{stamp}.log"
    slots_path = REPLAY_DIR / f"rrg_c18acc_slots_{stamp}.json"
    slots_path.write_text(json.dumps(DEFAULT_C18ACC_SLOTS_20260626, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _setup_c18acc_replay(slots_path)

    times = _poll_times(9, 0, 13, 30)
    lines = [
        f"# Replay · rrg-c18acc-poll · {session}",
        f"# 週一～五 09:00–13:30 · 每 5 分 · dry-run · 不更新生產槽位",
        f"# 對應 logs/intraday/launchd_rrg-c18acc-poll.log",
        "",
    ]
    tick_lines: list[str] = [f"# tick replay · {session}", ""]

    for t in times:
        now = _dt(session, t)
        tick_label = now.strftime("%Y-%m-%d %H:%M:%S")
        result = run_screen(
            conn,
            session_date=session,
            now=now,
            dry_run=True,
            apply_state=False,
        )
        block = _format_c18acc_tick(result, tick_label)
        lines.append(block)
        signal = 1 if result.actions else 0
        tick_lines.append(
            f"{result.polled_at} | session={session} | signal={signal} | "
            f"minute={result.poll_minute} | actions={len(result.actions)} | skip={result.skip_reason or '—'}"
        )

    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tick_out.write_text("\n".join(tick_lines) + "\n", encoding="utf-8")
    return out


def main() -> int:
    load_project_dotenv()
    args = _parse_args()
    session = args.date
    stamp = session.replace("-", "")

    REPLAY_DIR.mkdir(parents=True, exist_ok=True)

    conn = connect(DEFAULT_DB_PATH)
    written: list[Path] = []
    try:
        if args.only == "c18acc":
            written.append(replay_c18acc(conn, session, stamp))
        if args.only in ("all", "buy"):
            written.append(replay_buy(conn, session, stamp))
        if args.only in ("all", "sell"):
            written.append(replay_sell(conn, session, stamp))
    finally:
        conn.close()

    for p in written:
        n = sum(1 for _ in p.open(encoding="utf-8"))
        print(f"Wrote {p} ({n} lines)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
