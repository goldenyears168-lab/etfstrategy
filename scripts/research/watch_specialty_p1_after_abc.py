#!/usr/bin/env python3
"""Wait for ABC 2y backfill to finish, then deep-fill specialty P1 queue.

Does NOT dual-run FinMind with the active abc2y job.
Quota / weekday blackout reuse watch_backfill460_quota.py.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "reports/research/branch-footprint-screen"


def _load(path: Path) -> dict | list:
    return json.loads(path.read_text(encoding="utf-8"))


def abc_done(abc_universe: Path, abc_progress: Path) -> tuple[bool, str]:
    if not abc_universe.exists() or not abc_progress.exists():
        return False, "missing abc files"
    uni = _load(abc_universe)
    ids = {str(x["id"]) for x in uni} if isinstance(uni, list) else set()
    prog = _load(abc_progress)
    assert isinstance(prog, dict)
    done = set(str(x) for x in prog.get("completed_ids", []))
    i = int(prog.get("i") or 0)
    n = int(prog.get("n") or 0)
    # Only trust full universe coverage — progress i/n is a per-wave residual counter.
    if ids and ids <= done:
        return True, f"all {len(ids)} abc ids completed"
    missing = sorted(ids - done)
    return False, f"abc progress done={len(done)}/{len(ids)} i={i}/{n} missing={len(missing)}"


def backfill_running() -> bool:
    try:
        out = subprocess.check_output(
            ["pgrep", "-f", "backfill_broker_branch_tape.py"],
            text=True,
        )
        return bool(out.strip())
    except subprocess.CalledProcessError:
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--poll-sec", type=int, default=120)
    ap.add_argument(
        "--abc-universe",
        type=Path,
        default=OUT / "_abc_2y_universe.json",
    )
    ap.add_argument(
        "--abc-progress",
        type=Path,
        default=OUT / "_abc_2y_progress.json",
    )
    ap.add_argument(
        "--universe-json",
        type=Path,
        default=OUT / "_specialty_p1_2y_universe.json",
    )
    ap.add_argument(
        "--progress",
        type=Path,
        default=OUT / "_specialty_p1_2y_progress.json",
    )
    ap.add_argument(
        "--status",
        type=Path,
        default=OUT / "_specialty_p1_2y_watch_status.json",
    )
    ap.add_argument("--start", default="2024-07-01")
    ap.add_argument("--end", default="2026-07-16")
    ap.add_argument("--resume-below", type=int, default=800)
    ap.add_argument("--delay", type=float, default=0.55)
    args = ap.parse_args()

    if not args.universe_json.exists():
        raise SystemExit(f"missing universe: {args.universe_json}")

    status_path = args.status
    print(
        f"specialty P1 waiter: wait ABC → then backfill "
        f"{args.universe_json.name} ({len(_load(args.universe_json))} branches)",
        flush=True,
    )

    while True:
        ok, msg = abc_done(args.abc_universe, args.abc_progress)
        running = backfill_running()
        payload = {
            "state": "waiting_abc" if not ok else ("waiting_idle" if running else "ready"),
            "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "abc": msg,
            "backfill_running": running,
        }
        status_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"[{payload['at']}] {payload['state']} · {msg} · running={running}", flush=True)
        if ok and not running:
            break
        time.sleep(args.poll_sec)

    # Hand off to existing quota watcher (single-stage backfill).
    cmd = [
        str(ROOT / ".venv/bin/python"),
        str(ROOT / "scripts/research/watch_backfill460_quota.py"),
        "--universe-json",
        str(args.universe_json),
        "--progress",
        str(args.progress),
        "--status",
        str(args.status),
        "--start",
        args.start,
        "--end",
        args.end,
        "--resume-below",
        str(args.resume_below),
        "--poll-sec",
        str(args.poll_sec),
        "--delay",
        str(args.delay),
    ]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    env["PYTHONUNBUFFERED"] = "1"
    log = ROOT / "logs" / f"backfill_specialty_p1_watch_{time.strftime('%Y%m%d_%H%M%S')}.log"
    log.parent.mkdir(exist_ok=True)
    print(f"hand-off watch_backfill460_quota → log={log}", flush=True)
    status_path.write_text(
        json.dumps(
            {
                "state": "handing_off",
                "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "log": str(log),
                "cmd": cmd,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    with log.open("w", encoding="utf-8") as fh:
        proc = subprocess.run(cmd, cwd=str(ROOT), env=env, stdout=fh, stderr=fh)
    return int(proc.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
