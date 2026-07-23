#!/usr/bin/env python3
"""Serial expert-funnel scale batch (one process; sqlite ro)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "reports/research/branch-footprint-screen/expert_pool"
QUEUE = json.loads((OUT / "SCALE_QUEUE_20260720.json").read_text(encoding="utf-8"))["queue"]
NOTES = OUT / "SCALE_RUN_NOTES_20260720.md"
STATE = OUT / "SCALE_RUN_STATE_20260720.json"
LOCK = OUT / "SCALE_RUN_BATCH.lock"
# Original clean batch start; 12h wall clock (user extension 2026-07-20)
ORIGIN_START = datetime.fromisoformat("2026-07-20T18:42:06")
DEADLINE_DEFAULT = ORIGIN_START + timedelta(hours=12)  # → 2026-07-21T06:42:06
MAX_EVALS = 200
TARGET_ADOPTED = 80


def read_deadline() -> datetime:
    """Prefer STATE.deadline so extensions apply without code edits mid-run."""
    if STATE.exists():
        try:
            st = json.loads(STATE.read_text(encoding="utf-8"))
            if st.get("deadline"):
                return datetime.fromisoformat(str(st["deadline"]))
        except (json.JSONDecodeError, ValueError, TypeError):
            pass
    return DEADLINE_DEFAULT



def acquire_lock():
    import fcntl

    LOCK.parent.mkdir(parents=True, exist_ok=True)
    fh = open(LOCK, "w", encoding="utf-8")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        print("Another scale batch holds the lock; exiting.", flush=True)
        raise SystemExit(0)
    fh.write(f"pid={os.getpid()}\nstarted={datetime.now().isoformat(timespec='seconds')}\n")
    fh.flush()
    return fh



def load_reg():
    return json.loads((OUT / "EVAL_REGISTRY.json").read_text(encoding="utf-8"))


def adopted_n(reg):
    return sum(1 for r in reg["stocks"] if r.get("status") == "adopted")


def append_note(line: str) -> None:
    NOTES.write_text(NOTES.read_text(encoding="utf-8") + line, encoding="utf-8")


def save_state(d) -> None:
    STATE.write_text(json.dumps(d, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    lock_fh = acquire_lock()
    reg = load_reg()
    done = {str(r["stock_id"]) for r in reg["stocks"]}
    deadline = read_deadline()
    batch = {
        "started_at": ORIGIN_START.isoformat(timespec="seconds"),
        "runner_started_at": datetime.now().isoformat(timespec="seconds"),
        "deadline": deadline.isoformat(timespec="seconds"),
        "wall_hours": 12,
        "evaluated": [],
        "errors": [],
        "next_index": 0,
    }
    # Preserve prior evaluated log if present (registry remains skip SSOT).
    if STATE.exists():
        try:
            prev = json.loads(STATE.read_text(encoding="utf-8"))
            if prev.get("evaluated"):
                batch["prior_evaluated_n"] = len(prev["evaluated"])
        except json.JSONDecodeError:
            pass
    save_state(batch)
    append_note(
        f"\n## Batch runner start `{batch['runner_started_at']}` "
        f"deadline `{batch['deadline']}` (12h from `{ORIGIN_START.isoformat()}`) "
        f"pid={os.getpid()}\n"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = "src"

    idx = 0
    while idx < len(QUEUE):
        deadline = read_deadline()  # pick up live extensions
        batch["deadline"] = deadline.isoformat(timespec="seconds")
        if datetime.now() >= deadline:
            nxt = QUEUE[idx]["stock_id"] if idx < len(QUEUE) else None
            append_note(
                f"\n- STOP deadline at {datetime.now().isoformat(timespec='seconds')} next={nxt}\n"
            )
            break
        reg = load_reg()
        if adopted_n(reg) >= TARGET_ADOPTED:
            append_note(f"\n- STOP adopted={adopted_n(reg)} target hit\n")
            break
        if len(batch["evaluated"]) >= MAX_EVALS:
            append_note(f"\n- STOP max evals {MAX_EVALS}\n")
            break

        item = QUEUE[idx]
        sid = item["stock_id"]
        idx += 1
        batch["next_index"] = idx
        save_state(batch)

        reg_ids = {str(r["stock_id"]) for r in load_reg()["stocks"]}
        if sid in done or sid in reg_ids:
            print(f"skip {sid} already in registry", flush=True)
            continue

        t0 = time.time()
        print(
            f"=== RUN {sid} {item.get('name')} tier={item.get('tier')} "
            f"remaining_q={len(QUEUE) - idx} ===",
            flush=True,
        )
        try:
            r = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/research/run_stock_expert_funnel.py"),
                    "--stock",
                    sid,
                    "--end",
                    "2026-07-17",
                ],
                cwd=str(ROOT),
                env=env,
                capture_output=True,
                text=True,
                timeout=900,
            )
            elapsed = round(time.time() - t0, 1)
            summary: dict = {}
            summary_path = OUT / sid / "funnel_summary.json"
            if summary_path.exists():
                summary = json.loads(summary_path.read_text(encoding="utf-8"))
            else:
                for line in reversed(r.stdout.splitlines()):
                    line = line.strip()
                    if line.startswith("{"):
                        try:
                            summary = json.loads(line)
                            break
                        except json.JSONDecodeError:
                            pass
            row = {
                "sid": sid,
                "name": item.get("name"),
                "tier": item.get("tier"),
                "elapsed_s": elapsed,
                "rc": r.returncode,
                "hard_n": summary.get("hard_n"),
                "action": summary.get("action"),
                "p7_pass": summary.get("p7_pass"),
                "med_radj": (summary.get("p7_radj") or {}).get("med_radj_pct"),
                "ts": datetime.now().isoformat(timespec="seconds"),
            }
            if r.returncode != 0:
                row["stderr_tail"] = (r.stderr or "")[-500:]
                batch["errors"].append(row)
                append_note(
                    f"\n- `{row['ts']}` · **{sid}** ERROR rc={r.returncode} ({elapsed}s) · "
                    f"{(r.stderr or '')[:200]}\n"
                )
            else:
                batch["evaluated"].append(row)
                append_note(
                    f"\n- `{row['ts']}` · **{sid}** {row['name']} [{row['tier']}] · "
                    f"hard={row['hard_n']} · action={row['action']} · "
                    f"radj={row['med_radj']} · {elapsed}s\n"
                )
            done.add(sid)
            save_state(batch)
            print(json.dumps(row, ensure_ascii=False), flush=True)
        except Exception as e:  # noqa: BLE001 — continue batch
            elapsed = round(time.time() - t0, 1)
            err = {
                "sid": sid,
                "error": str(e),
                "elapsed_s": elapsed,
                "ts": datetime.now().isoformat(timespec="seconds"),
            }
            batch["errors"].append(err)
            append_note(f"\n- `{err['ts']}` · **{sid}** EXCEPTION {e} ({elapsed}s)\n")
            save_state(batch)
            print("EXC", err, flush=True)

    reg = load_reg()
    batch["finished_at"] = datetime.now().isoformat(timespec="seconds")
    batch["final_counts"] = reg.get("counts")
    batch["adopted"] = adopted_n(reg)
    batch["resume_next"] = QUEUE[idx]["stock_id"] if idx < len(QUEUE) else None
    save_state(batch)
    append_note(
        f"\n## Batch end `{batch['finished_at']}`\n"
        f"- evaluated_this_run={len(batch['evaluated'])} errors={len(batch['errors'])}\n"
        f"- registry counts={reg.get('counts')}\n"
        f"- resume_next={batch['resume_next']}\n"
    )
    print(
        "DONE",
        json.dumps(
            {k: batch[k] for k in ("finished_at", "adopted", "resume_next", "final_counts")},
            ensure_ascii=False,
        ),
        flush=True,
    )
    try:
        lock_fh.close()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
