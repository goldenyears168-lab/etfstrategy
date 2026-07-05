#!/usr/bin/env python3
"""Launchd-safe entry for daily_sync.sh.

macOS TCC blocks launchd from executing ``.sh`` files under Documents
(``Operation not permitted`` · exit 126). Pipe the script to ``bash -s``
so the file is read, not executed as a Documents inode.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    root = Path(__file__).resolve().parent.parent
    script = root / "scripts" / "daily_sync.sh"
    if not script.is_file():
        print(f"✗ missing daily_sync.sh: {script}", file=sys.stderr)
        return 2

    env = os.environ.copy()
    env["ROOT"] = str(root)
    env.setdefault("PYTHONPATH", str(root / "src"))
    for key in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        env.pop(key, None)

    with script.open(encoding="utf-8") as stdin:
        proc = subprocess.run(
            ["/bin/bash", "-s", "--", *args],
            stdin=stdin,
            cwd=root,
            env=env,
        )
    return int(proc.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
