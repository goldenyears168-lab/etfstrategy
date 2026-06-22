#!/usr/bin/env python3
"""Refresh AUTO-GENERATED research tables in supabase/site/research/*.md."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from report_paths import RESEARCH_RRG, RESEARCH_VCP  # noqa: E402
from stock_db import DEFAULT_DB_PATH  # noqa: E402

RESEARCH_DIR = ROOT / "supabase" / "site" / "research"
PY = ROOT / ".venv" / "bin" / "python"


def _patch(path: Path, marker: str, body: str) -> None:
    text = path.read_text(encoding="utf-8")
    pattern = (
        rf"(<!-- AUTO:{marker}:start -->\n)"
        rf"(.*?)"
        rf"(\n<!-- AUTO:{marker}:end -->)"
    )
    repl = rf"\1{body.rstrip()}\3"
    new_text, n = re.subn(pattern, repl, text, count=1, flags=re.DOTALL)
    if n != 1:
        raise ValueError(f"{path.name}: marker AUTO:{marker} not found")
    path.write_text(new_text, encoding="utf-8")


def _run(cmd: list[str]) -> None:
    env = {"PYTHONPATH": str(ROOT / "src")}
    subprocess.run(cmd, cwd=ROOT, env={**os.environ, **env}, check=True)


def _latest_report(pattern: str, *, under: Path) -> Path:
    matches = sorted(under.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    if not matches:
        raise FileNotFoundError(f"no report matching {pattern} under {under}")
    return matches[0]


def _strip_leading_blockquote(body: str) -> str:
    return re.sub(r"^(?:> .+\n)+\n?", "", body, count=1)


def _normalize_rrg_breadth_body(body: str) -> str:
    """Replace internal English strategy shorthand in AUTO block intro."""
    body = re.sub(
        r"^策略：\*\*mono 濾網 \+ fresh 訊號 \+ seg_last 排序 \+ 3 槽 \+ hold7\*\*"
        r"（D4 收盤進場 / D11 收盤出場）",
        "策略：**單軌濾網 + fresh 訊號 + 依軌跡排序 + 3 槽 + 持有 7 日**"
        "（第 4 日收盤進場 / 第 11 日收盤出場）",
        body,
        count=1,
    )
    body = body.replace(
        "策略：**mono 濾網 + fresh 訊號 + seg_last 排序 + 3 槽 + hold7**"
        "（D4 收盤進場 / D11 收盤出場）",
        "策略：**單軌濾網 + fresh 訊號 + 依軌跡排序 + 3 槽 + 持有 7 日**"
        "（第 4 日收盤進場 / 第 11 日收盤出場）",
    )
    return body


_VCP_PROFILE = {
    "near_pivot": "近樞紐",
    "pre_forming": "成形前",
    "breakout_zone": "突破區",
    "section_a": "區段A",
}
_VCP_ENTRY = {
    "close": "訊號收盤",
    "breakout_close": "突破收盤確認",
    "pivot_stop": "樞紐停損",
}


def _translate_vcp_cell_values(body: str) -> str:
    for en, zh in {**_VCP_PROFILE, **_VCP_ENTRY}.items():
        body = re.sub(rf"\| {re.escape(en)} \|", f"| {zh} |", body)
        body = re.sub(rf"\| \*\*{re.escape(en)}\*\* \|", f"| **{zh}** |", body)
        body = body.replace(en, zh)
    return body


def _normalize_vcp_sweep_body(body: str) -> str:
    body = body.replace("## Top combinations", "## 前 25 名組合")
    body = body.replace(
        "| # | profile | entry | slots | hold | min | wait | n | mean α | total α | win% | score |",
        "| 排名 | 篩選條件 | 進場 | 槽位 | 持有 | 最低分 | 等待 | 樣本 | 均超額% | 總超額% | 勝率% | 評分 |",
    )
    return _translate_vcp_cell_values(body)


def _normalize_lxh_matrix_body(body: str) -> str:
    body = body.replace(
        "訊號日 **T** = 持股公布日；**L1–L3** = T+1～T+3 開盤進場；**H1–H20** = 持有 1～20 交易日（收盤出）。",
        "訊號日 = 持股公布日；列 = 訊號日開盤／收盤，或隔日起第 1～3 日開盤；欄 = 持有 1～20 交易日後收盤出場。",
    )
    for h in range(20, 0, -1):
        body = body.replace(f"| H{h} |", f"| 持{h}日 |")
    body = body.replace("**L0O**", "**訊號日開盤**")
    body = body.replace("**L0C**", "**訊號日收盤**")
    body = body.replace("**L1**", "**隔日開盤**")
    body = body.replace("**L2**", "**T+2開盤**")
    body = body.replace("**L3**", "**T+3開盤**")
    body = body.replace("## 顯著性 Decay · L1 列（完整）", "## 顯著性衰減 · 隔日開盤列")
    body = body.replace("| H | n | 累計α | 日均超額% | p(W) |", "| 持有 | 樣本 | 累計超額 | 日均超額% | p值 |")
    for h in range(20, 0, -1):
        body = body.replace(f"| H{h}* ", f"| 持{h}日* ")
        body = body.replace(f"| H{h} ", f"| 持{h}日 ")
    return body


def refresh_lxh_matrix(*, date_end: str | None) -> str:
    cmd = [
        str(PY),
        "scripts/run_00981a_copytrade_backtest.py",
        "--matrix",
        "--max-hold",
        "20",
        "--etf-code",
        "00981A",
        "--write-report",
    ]
    if date_end:
        cmd.extend(["--window-end", date_end])
    _run(cmd)
    report = _latest_report("*_00981a_copytrade_h20_alpha.md", under=ROOT / "reports" / "research")
    body = report.read_text(encoding="utf-8")
    # Drop top title; page already has section heading.
    body = re.sub(r"^# .+\n\n", "", body, count=1)
    return _normalize_lxh_matrix_body(_strip_leading_blockquote(body.strip()))(*, date_start: str, date_end: str) -> str:
    out_md = RESEARCH_VCP / f"{date.today():%Y%m%d}_chunge_funnel_minervini_sweep.md"
    _run(
        [
            str(PY),
            "scripts/run_chunge_funnel_sweep.py",
            "--date-start",
            date_start,
            "--date-end",
            date_end,
            "--top",
            "25",
            "--write-report",
        ]
    )
    report = out_md if out_md.is_file() else _latest_report(
        "*_chunge_funnel_minervini_sweep.md", under=RESEARCH_VCP
    )
    text = report.read_text(encoding="utf-8")
    m = re.search(r"(## Top combinations.*?)(?:\n## Best pick|\Z)", text, re.DOTALL)
    if not m:
        raise ValueError("sweep report missing Top combinations section")
    return _normalize_vcp_sweep_body(m.group(1).strip())


def refresh_rrg_breadth(*, date_start: str, date_end: str) -> str:
    _run(
        [
            str(PY),
            "scripts/run_rrg_mono_breadth_backtest.py",
            "--date-start",
            date_start,
            "--date-end",
            date_end,
        ]
    )
    report = _latest_report("*_rrg_mono_breadth_zones.md", under=RESEARCH_RRG)
    body = report.read_text(encoding="utf-8")
    body = re.sub(r"^# .+\n\n", "", body, count=1)
    return _normalize_rrg_breadth_body(_strip_leading_blockquote(body.strip()))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Refresh research site AUTO sections from backtests")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--date-start", default="2026-01-01")
    parser.add_argument("--date-end", default="2026-06-18")
    parser.add_argument("--only", choices=("lxh", "vcp", "rrg", "all"), default="all")
    args = parser.parse_args(argv)

    if not args.db.is_file():
        print(f"ERROR: DB missing: {args.db}", file=sys.stderr)
        return 1

    if args.only in ("lxh", "all"):
        body = refresh_lxh_matrix(date_end=args.date_end)
        _patch(RESEARCH_DIR / "research_case_copytrade.md", "lxh-matrix", body)
        print("patched research_case_copytrade.md · lxh-matrix")

    if args.only in ("vcp", "all"):
        body = refresh_vcp_sweep_top25(date_start=args.date_start, date_end=args.date_end)
        _patch(RESEARCH_DIR / "research_case_vcp_funnel.md", "vcp-sweep-top25", body)
        print("patched research_case_vcp_funnel.md · vcp-sweep-top25")

    if args.only in ("rrg", "all"):
        body = refresh_rrg_breadth(date_start=args.date_start, date_end=args.date_end)
        _patch(RESEARCH_DIR / "research_case_rrg_mono.md", "rrg-breadth", body)
        print("patched research_case_rrg_mono.md · rrg-breadth")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
