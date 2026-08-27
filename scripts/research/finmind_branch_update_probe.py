#!/usr/bin/env python3
"""One-time probe (2026-08-10): detect when FinMind's TaiwanStockTradingDailyReport
publishes same-day data, email the detected time, then self-cleanup (plist + script).

Not part of config/job_registry.yaml — this is a transient, self-deleting job.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from project_dotenv import load_project_dotenv  # noqa: E402

load_project_dotenv(override=True)

from finmind_client import FINMIND_TRADING_DAILY_REPORT_URL, fetch_finmind_json  # noqa: E402
from notify_email import send_alert  # noqa: E402

PROBE_DATE = "2026-08-10"
STOCK_ID = "2330"
CUTOFF_HOUR, CUTOFF_MIN = 19, 0
TAIPEI = timezone(timedelta(hours=8))

DATA_DIR = Path(os.environ.get("GOLDENSTOCKS_DATA_DIR", str(REPO_ROOT)))
STATE_PATH = DATA_DIR / "scratch" / "finmind_branch_probe_state.json"

PLIST_LABEL = "com.jackm4.goldenstocks.finmind-branch-probe-onetime"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{PLIST_LABEL}.plist"
SCRIPT_PATH = Path(__file__).resolve()


def _now_taipei() -> datetime:
    return datetime.now(tz=TAIPEI)


def _cleanup() -> None:
    gui_domain = f"gui/{os.getuid()}"
    subprocess.run(
        ["launchctl", "bootout", f"{gui_domain}/{PLIST_LABEL}"],
        capture_output=True,
    )
    subprocess.run(["launchctl", "unload", str(PLIST_PATH)], capture_output=True)
    PLIST_PATH.unlink(missing_ok=True)
    STATE_PATH.unlink(missing_ok=True)
    SCRIPT_PATH.unlink(missing_ok=True)


def main() -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if STATE_PATH.is_file():
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if state.get("resolved"):
            _cleanup()
            return
    else:
        state = {"resolved": False, "first_probe": None}

    now = _now_taipei()
    if state["first_probe"] is None:
        state["first_probe"] = now.isoformat()

    rows: list = []
    try:
        payload = fetch_finmind_json(
            {"data_id": STOCK_ID, "date": PROBE_DATE},
            url=FINMIND_TRADING_DAILY_REPORT_URL,
        )
        rows = payload.get("data", [])
    except Exception as exc:  # FinMind transient errors shouldn't kill the poll loop
        print(f"[{now.isoformat()}] fetch error: {exc}")

    if rows:
        subject = f"[FinMind探測] {PROBE_DATE} 分點資料已更新 · {now.strftime('%H:%M')}"
        body = (
            f"FinMind TaiwanStockTradingDailyReport（{STOCK_ID}）\n"
            f"探測日期：{PROBE_DATE}\n"
            f"偵測到資料出現時間：{now.strftime('%Y-%m-%d %H:%M:%S')}（Asia/Taipei）\n"
            f"開始探測時間：{state['first_probe']}\n"
            f"回傳筆數：{len(rows)}\n"
        )
        send_alert(subject, body)
        print(body)
        state.update(resolved=True, result="found", found_at=now.isoformat())
        STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        _cleanup()
        return

    cutoff = now.replace(hour=CUTOFF_HOUR, minute=CUTOFF_MIN, second=0, microsecond=0)
    if now >= cutoff:
        subject = f"[FinMind探測] {PROBE_DATE} 到 {CUTOFF_HOUR}:{CUTOFF_MIN:02d} 分點資料仍未更新"
        body = (
            f"FinMind TaiwanStockTradingDailyReport（{STOCK_ID}）\n"
            f"探測日期：{PROBE_DATE}\n"
            f"從 {state['first_probe']} 開始每 5 分鐘探測一次，"
            f"到截止時間 {CUTOFF_HOUR}:{CUTOFF_MIN:02d} 仍未偵測到資料，已停止輪詢。\n"
        )
        send_alert(subject, body)
        print(body)
        state.update(resolved=True, result="cutoff_no_data")
        STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        _cleanup()
        return

    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[{now.isoformat()}] 尚未更新，繼續等待下一輪")


if __name__ == "__main__":
    main()
