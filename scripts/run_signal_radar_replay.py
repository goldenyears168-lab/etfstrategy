#!/usr/bin/env python3
"""Replay buy/sell signal radar for one session · writes sample launchd-style logs."""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from project_dotenv import load_project_dotenv
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


def _setup_replay_fast() -> None:
    os.environ["SIGNAL_RADAR_REPLAY_FAST"] = "1"
    from strategy_signal_radar import clear_signal_radar_replay_caches

    clear_signal_radar_replay_caches()


def _poll_times(start_h: int, start_m: int, end_h: int, end_m: int, interval: int = 5) -> list[str]:
    out: list[str] = []
    total = start_h * 60 + start_m
    end_total = end_h * 60 + end_m
    while total <= end_total:
        hh, mm = divmod(total, 60)
        out.append(f"{hh:02d}:{mm:02d}")
        total += interval
    return out


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Replay signal radar for one trading day")
    p.add_argument("--date", default="2026-06-26", metavar="YYYY-MM-DD")
    p.add_argument("--out", default="", help="Output log path (default: logs/replay/signal_radar_<date>.log)")
    return p.parse_args()


def main() -> int:
    load_project_dotenv()
    args = _parse_args()
    session = args.date
    stamp = session.replace("-", "")

    out_path = Path(args.out) if args.out else REPLAY_DIR / f"signal_radar_{stamp}.log"
    dedup_path = REPLAY_DIR / f"signal_radar_dedup_{stamp}.json"
    buy_state_path = REPLAY_DIR / f"signal_radar_buy_state_{stamp}.json"

    REPLAY_DIR.mkdir(parents=True, exist_ok=True)

    os.environ["C18ACC_KBAR_SYNC"] = "0"
    _setup_replay_fast()

    os.environ["RUN_BUY_SIGNAL_RADAR"] = "1"

    import strategy_signal_radar as radar_mod

    radar_mod.DEDUP_PATH = dedup_path
    radar_mod.BUY_STATE_PATH = buy_state_path

    buy_times = _poll_times(9, 0, 13, 20)
    sell_times = _poll_times(9, 6, 13, 20)

    lines: list[str] = [
        f"# Signal radar replay · {session}",
        f"# 買方 {buy_times[0]}–{buy_times[-1]} · 賣方 {sell_times[0]}–{sell_times[-1]} · 每 5 分",
        f"# 賣方：宇宙 extension 純訊號（無持倉 overlay）",
        "",
    ]

    conn = connect(DEFAULT_DB_PATH)
    try:
        for t in buy_times:
            now = datetime(
                int(session[:4]), int(session[5:7]), int(session[8:10]),
                int(t[:2]), int(t[3:5]), tzinfo=_TZ,
            )
            result = run_buy_signal_radar(conn, session_date=session, now=now, mark_dedup=True)
            lines.append(format_buy_radar_log(result))
            lines.append(f"BUY_SIGNAL_RADAR={'1' if result.new_signals else '0'}")
            if result.new_signals:
                path = write_radar_report(
                    "buy", result.new_signals, session_date=session, poll_minute=result.poll_minute
                )
                lines.append(f"報告：{path}")
            lines.append("")

        os.environ["RUN_SELL_SIGNAL_RADAR"] = "1"
        os.environ["RUN_C18ACC_EXTENSION_OVERLAY"] = "1"
        for t in sell_times:
            now = datetime(
                int(session[:4]), int(session[5:7]), int(session[8:10]),
                int(t[:2]), int(t[3:5]), tzinfo=_TZ,
            )
            result = run_sell_signal_radar(conn, session_date=session, now=now, mark_dedup=True)
            lines.append(format_sell_radar_log(result))
            lines.append(f"SELL_SIGNAL_RADAR={'1' if result.new_signals else '0'}")
            if result.new_signals:
                path = write_radar_report(
                    "sell", result.new_signals, session_date=session, poll_minute=result.poll_minute
                )
                lines.append(f"報告：{path}")
            lines.append("")
    finally:
        conn.close()

    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {out_path} ({len(lines)} lines)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
