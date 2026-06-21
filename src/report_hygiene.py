#!/usr/bin/env python3
"""移除 reports/ 已淘汰的 E0 / ensemble 產物。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from report_paths import REPORTS_DAILY, REPORTS_ROOT

# 清理 daily 目錄與根目錄 legacy（遷移前殘留）。
LEGACY_GLOBS: tuple[str, ...] = (
    "ensemble_digest.md",
    "*_ensemble_digest.md",
    "*_research_scoreboard.md",
    "*_execution_eval.md",
    "*_execution_eval_*.md",
    "*_order_intents.md",
    "*_order_intents.json",
    "*_order_intents_preview.md",
    "*_order_intents_preview.json",
)


def legacy_report_paths(reports_dir: Path) -> list[Path]:
    found: list[Path] = []
    seen: set[Path] = set()
    for pattern in LEGACY_GLOBS:
        for path in sorted(reports_dir.glob(pattern)):
            if not path.is_file():
                continue
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            found.append(path)
    return found


def prune_legacy_reports(
    reports_root: Path | None = None,
    *,
    dry_run: bool = False,
) -> list[str]:
    """刪除 legacy 檔；回傳已刪除（或 dry-run 將刪除）的相對路徑。"""
    removed: list[str] = []
    if reports_root is not None:
        bases = [reports_root]
        root_for_rel = reports_root
    else:
        bases = [p for p in (REPORTS_ROOT, REPORTS_DAILY) if p.is_dir()]
        root_for_rel = REPORTS_ROOT
    for base in bases:
        for path in legacy_report_paths(base):
            try:
                rel = str(path.relative_to(root_for_rel))
            except ValueError:
                rel = str(path)
            if dry_run:
                removed.append(rel)
                continue
            path.unlink(missing_ok=True)
            removed.append(rel)
    return removed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="prune legacy report artifacts")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    removed = prune_legacy_reports(dry_run=args.dry_run)
    if not removed:
        print("No legacy reports to prune.")
        return 0
    prefix = "Would remove" if args.dry_run else "Removed"
    for rel in removed:
        print(f"  {prefix}: {rel}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
