"""Fetch holdings pulse digest text for intraday email digests."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from stock_db import DEFAULT_DB_PATH, PROJECT_ROOT


def fetch_holdings_pulse_digest(
    *,
    session_date: str,
    project_root: Path = PROJECT_ROOT,
    sync_rrg_intraday: bool = True,
    write_snapshot: bool = True,
) -> tuple[str | None, str | None]:
    """Run holdings_pulse via .venv-fubon; return (digest_text, error)."""
    if os.environ.get("RUN_HOLDINGS_PULSE_IN_DIGEST", "1").strip() in ("0", "false", "False"):
        return None, "RUN_HOLDINGS_PULSE_IN_DIGEST=0"

    fubon_py = project_root / ".venv-fubon" / "bin" / "python"
    script = project_root / "scripts" / "order" / "holdings_pulse.py"
    if not fubon_py.is_file():
        return None, f"missing {fubon_py}"
    if not script.is_file():
        return None, f"missing {script}"

    cmd = [
        str(fubon_py),
        str(script),
        "--digest-only",
        "--date",
        session_date,
        "--db",
        str(DEFAULT_DB_PATH),
    ]
    if sync_rrg_intraday:
        cmd.append("--sync-rrg-intraday")
    if write_snapshot:
        cmd.append("--write")

    proc = subprocess.run(
        cmd,
        cwd=project_root,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(project_root / "src"), "ROOT": str(project_root)},
        check=False,
    )
    if proc.returncode not in (0, 1):
        err = (proc.stderr or proc.stdout or "").strip() or f"exit {proc.returncode}"
        return None, err

    text = proc.stdout.strip()
    if not text:
        return None, (proc.stderr or "").strip() or "empty holdings pulse output"
    return text, None


def append_holdings_section(
    lines: list[str],
    holdings_digest: str | None,
    *,
    error: str | None = None,
) -> None:
    if holdings_digest:
        lines.extend(["", holdings_digest, ""])
        return
    if error:
        lines.extend(["", "■ 持倉即時脈動", "", f"  ⚠ 略過：{error}", ""])
