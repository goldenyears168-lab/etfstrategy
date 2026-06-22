#!/usr/bin/env python3
"""One-off / maintenance: normalize user-facing Traditional Chinese prose."""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SKIP_DIRS = {
    ".git",
    ".venv",
    "vendor",
    "node_modules",
    "CAFubon",
    "__pycache__",
    "log",
}

SKIP_GLOBS = ("reports/**",)

# Order matters: longer / more specific first.
REPLACEMENTS: tuple[tuple[str, str], ...] = (
    ("四軸市場體制診斷", "四軸市場環境"),
    ("四軸市場體制", "四軸市場環境"),
    ("四軸體制診斷", "四軸市場環境"),
    ("四軸體制", "四軸市場環境"),
    ("市場體制診斷層", "環境層"),
    ("體制診斷層", "環境層"),
    ("體制診斷軸", "市場環境軸"),
    ("環境診斷", "市場環境"),
    ("環境層", "環境層"),
    ("市場體制快照", "市場環境快照"),
    ("今天市場體制", "今天市場環境"),
    ("市場體制、", "市場環境、"),
    ("市場體制 +", "市場環境 +"),
    ("市場體制**", "市場環境**"),
    ("市場體制？", "市場環境？"),
    ("Strategy Hub · 多策略研究入口", "策略入口 · 多軌並行"),
    ("Strategy Hub · Parallel Alpha Tracks", "策略入口 · 多軌並行"),
    ("最新 Hub", "最新日報"),
    ("日報 Hub", "日報首頁"),
    ("H1 訊號日腿數研究", "H1 訊號日異動檔數研究"),
    ("H1 腿數假说验证", "H1 異動檔數假說驗證"),
    ("H1 腿數假說驗證", "H1 異動檔數假說驗證"),
    ("僅 H1 訊號日腿數研究", "僅 H1 訊號日異動檔數研究"),
    ("H1（訊號日腿數）", "H1（訊號日異動檔數）"),
    ("跳過 5–10 腿訊號日", "跳過單日 5–10 檔異動的訊號日"),
    ("跳過 5-10 腿", "跳過單日 5–10 檔異動"),
    ("跳過 5–10 腿", "跳過單日 5–10 檔異動"),
    ("僅 5–10 腿（反向）", "僅 5–10 檔異動（反向）"),
    ("5–10 腿 rebalance", "5–10 檔異動 rebalance"),
    ("5-10 腿", "5–10 檔異動"),
    ("5–10 腿", "5–10 檔異動"),
    ("only_5_10 腿", "only_5_10 檔異動"),
    ("only_11plus 腿", "only_11plus 檔異動"),
    ("only_1_4 腿", "only_1_4 檔異動"),
    ("only_2_4 腿", "only_2_4 檔異動"),
    ("only_1 腿", "only_1 檔異動"),
    ("≤4 腿", "≤4 檔異動"),
    ("2–4 腿", "2–4 檔異動"),
    ("≥11 腿", "≥11 檔異動"),
    ("多腿日", "多檔異動日"),
    ("单腿 vs 多腿日", "單檔 vs 多檔異動日"),
    ("整腿 skip", "整檔 skip"),
    ("整腿 skip", "整檔 skip"),
    ("跟不跟哪幾腿", "跟不跟哪幾檔"),
    ("當日最賺腿", "當日最賺的一檔"),
    ("因子判差」腿", "因子判差」的一檔"),
    ("1,872 腿", "1,872 筆持股異動"),
    ("腿數", "異動檔數"),
    ("{len(legs)} 腿", "{len(legs)} 檔"),
    ("n_legs}}腿", "n_legs}}檔"),
    ("一腿持倉", "一檔持倉"),
    ("的一腿", "的一檔"),
    ("一腿；", "一檔；"),
    ("Leg 層", "成分股層"),
    ("leg 層", "成分股層"),
    ("Leg 通過", "成分股通過"),
    ("eligible leg", "eligible 成分股"),
    ("triple leg", "triple 成分股"),
    ("过熱 leg", "過熱成分股"),
    ("skip leg", "skip 成分股"),
    ("单池回收 α", "單池實現超額"),
    ("單池回收 α", "單池實現超額"),
    ("單池回收α", "單池實現超額"),
    ("单池回收α", "單池實現超額"),
    ("rotation 回收α", "rotation 實現超額"),
    ("9 槽回收 α", "9 槽實現超額"),
    ("9 槽回收α", "9 槽實現超額"),
    ("回收 α", "實現超額"),
    ("回收α", "實現超額"),
    ("總回收 α", "總實現超額"),
    ("总回收 α", "總實現超額"),
    ("總回收", "總實現超額"),
    ("总回收", "總實現超額"),
    ("边际回收 α", "邊際實現超額"),
    ("邊際回收 α", "邊際實現超額"),
    ("Δ回收", "Δ實現超額"),
    ("Δ回收α", "Δ實現超額"),
    ("成交輪數", "成交筆數"),
    ("执行輪數", "成交筆數"),
    ("實際成交輪數", "實際成交筆數"),
    ("可執行輪數", "可執行筆數"),
    ("RRG 對標子 sweep", "RRG 對照基準子 sweep"),
    ("RRG 對標）", "RRG 對照基準）"),
    ("RRG 對標尺", "RRG 對照基準"),
    ("RRG 對標", "RRG 對照基準"),
    ("對標 TradingView", "對照 TradingView"),
    ("對標門檻", "對照門檻"),
    ("對標分", "對照分"),
    ("對標加分", "對照加分"),
    ("對標子", "對照基準子"),
    ("FinPilot 原版用 FinLab 全市場 + 月頻持有至下月，此處改 H9 對標。", "FinPilot 原版用 FinLab 全市場 + 月頻持有至下月，此處改 H9 對照。"),
    ("§11 L1-P1～P3：分桶持有政策 × 單池回收 α", "§11 L1-P1～P3：分桶持有政策 × 單池實現超額"),
    ("分桶持有政策 × 單池回收 α", "分桶持有政策 × 單池實現超額"),
    ("新鮮訊號 + 段落末排序", "fresh 訊號 + 依軌跡排序"),
    ("新鮮 + 二階", "fresh + 二階"),
    ("新鮮訊號", "fresh 訊號"),
    ("**新鮮**", "**fresh**"),
    ("段落末排序", "依軌跡排序"),
    ("段落末", "軌跡末段"),
    ("篩選檔", "篩選條件"),
    ("米涅爾維尼趨勢模板", "Minervini Trend Template"),
    ("米涅爾維尼", "Minervini"),
    ("米涅維尼", "Minervini"),
    ("每輪均", "每筆均"),
    ("／輪", "／筆"),
    ("| 轮数 |", "| 成交筆數 |"),
    ("| 轮数 |", "| 成交筆數 |"),
    ("| 輪數 |", "| 成交筆數 |"),
    ("轮数", "成交筆數"),
    ("捕获%", "捕獲%"),
    ("捕获", "捕獲"),
    ("基准", "基準"),
    ("建议", "建議"),
    ("无约束", "無約束"),
    ("总回收", "總實現超額"),
    ("| legs |", "| 異動檔數 |"),
)

# Per-file extra: path suffix -> replacements
FILE_EXTRA: dict[str, tuple[tuple[str, str], ...]] = {
    "render_rrg_universe_html.py": (
        (
            "leg=單一標的一腿持倉（本策略 1 訊號 = 1 檔 = 1 leg）",
            "leg=單一標的一檔持倉（本策略 1 訊號 = 1 檔 = 1 leg）",
        ),
        (
            "leg=籃子內每檔股票一腿",
            "leg=籃子內每檔股票一檔持倉",
        ),
        (
            "批內單一股票的一腿持倉（例：一批 5 檔 = 5 leg）",
            "批內單一股票的一檔持倉（例：一批 5 檔 = 5 leg）",
        ),
        (
            "整籃 {cap} NTD 等權拆成多 leg",
            "整籃 {cap} NTD 等權拆成多檔",
        ),
    ),
    "rrg_mono_backtest.py": (
        (
            "策略：**mono 濾網 + fresh 訊號 + seg_last 排序 + 3 槽 + hold7**",
            "策略：**單軌濾網 + fresh 訊號 + 依軌跡排序 + 3 槽 + 持有 7 日**",
        ),
    ),
    "rrg_mono_daily_brief.py": (
        (
            "策略：**mono 濾網 + seg_last 排序 + 3 槽 + hold7**",
            "策略：**單軌濾網 + fresh 訊號 + 依軌跡排序 + 3 槽 + 持有 7 日**",
        ),
    ),
}


def _should_skip(path: Path) -> bool:
    parts = set(path.parts)
    if parts & SKIP_DIRS:
        return True
    rel = path.relative_to(ROOT).as_posix()
    if rel.startswith("reports/"):
        return True
    return False


def _iter_files() -> list[Path]:
    out: list[Path] = []
    for ext in (".md", ".py", ".yaml", ".yml", ".txt", ".html", ".sh", ".command", ".mdc"):
        for path in ROOT.rglob(f"*{ext}"):
            if path.is_file() and not _should_skip(path):
                out.append(path)
    return sorted(out)


def normalize_text(text: str, *, extras: tuple[tuple[str, str], ...] = ()) -> str:
    for old, new in (*REPLACEMENTS, *extras):
        text = text.replace(old, new)
    # Remaining isolated 輪 in trade-count context (e.g. "89 輪")
    text = re.sub(r"(\d+) 輪(?=[\s|·，。）)])", r"\1 筆", text)
    return text


def patch_file(path: Path, *, dry_run: bool) -> bool:
    rel = path.relative_to(ROOT).as_posix()
    extras = FILE_EXTRA.get(path.name, ())
    text = path.read_text(encoding="utf-8")
    new_text = normalize_text(text, extras=extras)
    if new_text == text:
        return False
    if not dry_run:
        path.write_text(new_text, encoding="utf-8")
    print(rel)
    return True


def main(argv: list[str] | None = None) -> int:
    dry_run = "--dry-run" in (argv or sys.argv[1:])
    n = sum(1 for p in _iter_files() if patch_file(p, dry_run=dry_run))
    print(f"{'would patch' if dry_run else 'patched'} {n} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
