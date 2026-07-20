#!/usr/bin/env python3
"""Generate readdy-490731/src/lib/uiCopy.generated.ts from Python copy SSOT modules."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
OUT = ROOT / "readdy-490731" / "src" / "lib" / "uiCopy.generated.ts"

sys.path.insert(0, str(SRC))

import daily_ui_copy as daily  # noqa: E402
import home_ui_copy as home  # noqa: E402
import lens_ui_copy as lens  # noqa: E402


def _ts_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _ts_value(value: Any, indent: int = 0) -> str:
    pad = "  " * indent
    if isinstance(value, str):
        return _ts_string(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        if not value:
            return "[]"
        if all(isinstance(x, dict) for x in value):
            items = ",\n".join(
                f"{pad}  {{{', '.join(f'{k}: {_ts_value(v, indent + 2)}' for k, v in row.items())}}}"
                for row in value
            )
            return f"[\n{items},\n{pad}]"
        inner = ", ".join(_ts_value(v, indent + 1) for v in value)
        return f"[{inner}]"
    if isinstance(value, dict):
        if not value:
            return "{}"
        lines = [
            f"{pad}  {json.dumps(str(k), ensure_ascii=False)}: {_ts_value(v, indent + 1)}"
            for k, v in value.items()
        ]
        return "{\n" + ",\n".join(lines) + f",\n{pad}}}"
    return _ts_string(str(value))


def _export_module(mod: Any, names: list[str]) -> list[str]:
    lines: list[str] = []
    for name in names:
        value = getattr(mod, name)
        lines.append(f"export const {name} = {_ts_value(value)};")
    return lines


def main() -> int:
    header = [
        "/** AUTO-GENERATED — do not edit. Run: scripts/generate_readdy_ui_copy.py */",
        "",
    ]

    home_names = [
        n
        for n in dir(home)
        if n.isupper() and not n.startswith("_")
    ]
    daily_names = [
        n
        for n in dir(daily)
        if n.isupper() and not n.startswith("_")
    ]

    lens_names = sorted(
        n
        for n in dir(lens)
        if n.isupper() and not n.startswith("_")
        and n not in ("BADGE_PLAIN_ZH", "RRG_QUADRANT_CHANGE_PLAIN_ZH")
    )
    lens_name_set = set(lens_names)
    home_names_filtered = sorted(
        n for n in home_names if n not in lens_name_set
    )
    daily_names_filtered = sorted(
        n for n in daily_names if n not in lens_name_set
    )

    body = _export_module(home, home_names_filtered)
    body.append("")
    body.extend(_export_module(daily, daily_names_filtered))
    body.append("")
    body.extend(_export_module(lens, lens_names))
    body.extend(
        [
            "",
            "export function formatRrgRankZh(rank: number | null | undefined, total: number | null | undefined): string | null {",
            "  if (total == null || total <= 0) return null;",
            "  if (rank == null || rank <= 0) return `—/${total}`;",
            "  return `${rank}/${total}`;",
            "}",
            "",
            "export function formatWatchlistCountZh(count: number): string {",
            f"  return `${{_ts_string(lens.CHIP_WATCHLIST_ZH).strip(chr(34))}} ${{count}} 檔`;",
            "}",
            "",
            "export function formatRrgEmptyZh(isIntraday: boolean): string {",
            f"  return isIntraday ? {_ts_string(daily.format_rrg_empty_zh(True))} : {_ts_string(daily.format_rrg_empty_zh(False))};",
            "}",
            "",
            "export function formatRrgSignalCountZh(n: number, isIntraday: boolean): string {",
            "  if (isIntraday) {",
            f"    return `盤中預估顯示 ${{n}} 檔符合「新鮮軌跡」條件，適合作為今天盤中觀察重點之一。`;",
            "  }",
            f"  return `今天共有 ${{n}} 檔符合單軌條件，可進一步查看軌跡路徑與位置變化。`;",
            "}",
            "",
            "export function plainVcpHeader(header: string): string {",
            "  const key = header.trim().toLowerCase();",
            f"  const map = {_ts_value(daily.BRIEF_VCP_HEADER_PLAIN)} as Record<string, string>;",
            "  return map[key] ?? map[header] ?? header;",
            "}",
            "",
            "export function plainVcpState(state: string): string {",
            f"  const entries: Array<[string, string]> = {json.dumps(list(daily.BRIEF_VCP_STATE_PLAIN.items()), ensure_ascii=False)};",
            "  for (const [pattern, plain] of entries) {",
            "    if (state.includes(pattern)) return plain;",
            "  }",
            "  return state;",
            "}",
            "",
        ]
    )

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(header + body) + "\n", encoding="utf-8")
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
