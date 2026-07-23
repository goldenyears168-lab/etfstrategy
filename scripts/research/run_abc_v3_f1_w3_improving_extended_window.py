#!/usr/bin/env python3
"""RP-1 · W3 Improving overlay · 延伸歷史窗口驗證（2024-01-02 ～ 2026-07-01）.

重用 run_entry_multifactor_tp_sweep（只跑 overlay=(none, w3_improving) 兩格），
再以同一批 raw legs 做時間三切段驗證 + bootstrap / t 信賴區間，
依 RP-1 第 5 節成功標準判定 passed / rejected / mixed。

  cd <project root> && PYTHONPATH=src python3 \
    scripts/research/run_abc_v3_f1_w3_improving_extended_window.py \
    --date-start 2024-01-02 --date-end 2026-07-01

計劃書：reports/research/rrg/20260710_rp1_w3_improving_extended_window.md
"""

from __future__ import annotations

import argparse
import json
import math
import random
import statistics
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
assert (ROOT / "src").exists(), f"unexpected ROOT resolution: {ROOT}"

from project_config import DEFAULT_ETF_CODES  # noqa: E402
from report_paths import RESEARCH_RRG  # noqa: E402

import research.backtest.abc_v3_f1_entry_multifactor_tp_sweep as mf  # noqa: E402
from research.backtest.abc_v3_f1_entry_multifactor_tp_sweep import (  # noqa: E402
    CHAMPION_COMBO,
    MultifactorOverlay,
    _leg_matches_overlay,
    _matches_base_gap_gate,
)
from research.backtest.archive.abc_v3_f1_kinematic_tp_sl_study import (  # noqa: E402
    evaluate_leg_tp_sl,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402

# ---------------------------------------------------------------------------
# Leg cache：run_entry_multifactor_tp_sweep 內部會呼叫 collect_abc_v3_f1_raw_legs
# （最貴的一步）。這裡以行程內 monkeypatch 快取住結果，讓切段/CI 分析重用同一批
# legs，不必重收集，也不修改任何既有模組檔案。
# ---------------------------------------------------------------------------
_LEG_CACHE: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
_ORIG_COLLECT = mf.collect_abc_v3_f1_raw_legs

# 沙箱單次 bash 呼叫上限 45s，全窗口一次收集跑不完 → 支援磁碟分段快取：
# --collect-chunk 模式把一小段日期的 legs 存成 JSON；--analyze 模式載入全部
# 分段直接分析（monkeypatch collect 回傳預載 legs）。
_PRELOADED_LEGS: list[dict[str, Any]] | None = None


def _caching_collect(conn, *, dates, hold_days=5, etf_codes=DEFAULT_ETF_CODES):
    key = (dates[0], dates[-1], len(dates), hold_days, tuple(etf_codes))
    if key not in _LEG_CACHE:
        if _PRELOADED_LEGS is not None:
            _LEG_CACHE[key] = [
                leg
                for leg in _PRELOADED_LEGS
                if dates[0] <= str(leg.get("entry_date")) <= dates[-1]
            ]
        else:
            _LEG_CACHE[key] = _ORIG_COLLECT(
                conn, dates=dates, hold_days=hold_days, etf_codes=etf_codes
            )
    return _LEG_CACHE[key]


mf.collect_abc_v3_f1_raw_legs = _caching_collect


def collect_chunk(conn, *, chunk_start: str, chunk_end: str, hold_days: int, cache_dir: Path) -> Path:
    """收集 [chunk_start, chunk_end] 的 raw legs 並寫入 JSON 分段快取（可續跑）。"""
    from research.backtest.finpilot_local_backtest import load_price_panels

    out = cache_dir / f"legs_{chunk_start}_{chunk_end}_h{hold_days}.json"
    if out.exists():
        print(f"chunk exists, skip: {out}")
        return out
    close, _, _ = load_price_panels(conn)
    dates = [d for d in close.index.astype(str) if chunk_start <= d <= chunk_end]
    legs = (
        _ORIG_COLLECT(conn, dates=dates, hold_days=hold_days) if dates else []
    )
    cache_dir.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(legs, ensure_ascii=False), encoding="utf-8")
    tmp.rename(out)
    print(f"chunk {chunk_start}..{chunk_end}: dates={len(dates)} legs={len(legs)} -> {out}")
    return out


def load_cached_legs(cache_dir: Path, *, date_start: str, date_end: str, hold_days: int) -> list[dict[str, Any]]:
    legs_by_id: dict[str, dict[str, Any]] = {}
    files = sorted(cache_dir.glob(f"legs_*_h{hold_days}.json"))
    if not files:
        raise SystemExit(f"BLOCKER: no leg cache files in {cache_dir}")
    for f in files:
        for leg in json.loads(f.read_text(encoding="utf-8")):
            d = str(leg.get("entry_date"))
            if date_start <= d <= date_end:
                legs_by_id[str(leg.get("leg_id"))] = leg
    legs = sorted(
        legs_by_id.values(), key=lambda l: (str(l.get("entry_date")), str(l.get("stock_id")))
    )
    print(f"loaded {len(legs)} legs from {len(files)} chunk files in {cache_dir}")
    return legs

OVERLAY_NONE = MultifactorOverlay(id="none", label="base only", filter_ids=())
OVERLAY_W3 = MultifactorOverlay(
    id="w3_improving", label="W3 MV>100", filter_ids=("w3_improving",)
)
TWO_CELL_GRID = (OVERLAY_NONE, OVERLAY_W3)

# 三切段（各約 10 個月）；第三段刻意對齊 H-ENTRY-MF-1 既有 10 個月窗口
# （2025-09-01..2026-07-01，n=8 · TP-only 10.06%）作一致性對照。
DEFAULT_SEGMENTS = (
    ("seg1", "2024-01-02", "2024-10-31"),
    ("seg2", "2024-11-01", "2025-08-31"),
    ("seg3", "2025-09-01", "2026-07-01"),
)


def _tp_only_ret(leg: dict[str, Any]) -> float:
    return float(evaluate_leg_tp_sl(leg, CHAMPION_COMBO)["exit_ret_pct"])


def _t_crit_975(df: int) -> float:
    """兩尾 95% t 臨界值（Cornish-Fisher 展開近似，無 scipy 環境用）。"""
    z = 1.959963985
    if df <= 0:
        return float("nan")
    return z + (z**3 + z) / (4 * df) + (5 * z**5 + 16 * z**3 + 3 * z) / (96 * df**2)


def _describe(rets: list[float], *, boot_iters: int = 10000, seed: int = 20260710) -> dict[str, Any]:
    n = len(rets)
    out: dict[str, Any] = {"n": n}
    if n == 0:
        return out
    mean = statistics.fmean(rets)
    out["mean_pct"] = round(mean, 3)
    out["median_pct"] = round(statistics.median(rets), 3)
    out["win_rate_pct"] = round(100.0 * sum(1 for r in rets if r > 0) / n, 1)
    out["pct_ge_8"] = round(100.0 * sum(1 for r in rets if r >= 8.0) / n, 1)
    if n < 2:
        return out
    sd = statistics.stdev(rets)
    out["std_pct"] = round(sd, 3)
    se = sd / math.sqrt(n)
    tcrit = _t_crit_975(n - 1)
    out["t_ci95"] = [round(mean - tcrit * se, 3), round(mean + tcrit * se, 3)]
    rng = random.Random(seed)
    boot_means = sorted(
        statistics.fmean(rng.choices(rets, k=n)) for _ in range(boot_iters)
    )
    lo = boot_means[int(0.025 * boot_iters)]
    hi = boot_means[min(int(0.975 * boot_iters), boot_iters - 1)]
    out["bootstrap_ci95"] = [round(lo, 3), round(hi, 3)]
    out["bootstrap_iters"] = boot_iters
    return out


def _leg_rows(legs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for leg in legs:
        rows.append(
            {
                "stock_id": leg.get("stock_id"),
                "entry_date": leg.get("entry_date"),
                "hold_ret_pct": round(float(leg.get("return_pct") or 0), 3),
                "tp_only_ret_pct": round(_tp_only_ret(leg), 3),
            }
        )
    rows.sort(key=lambda r: (r["entry_date"], str(r["stock_id"])))
    return rows


def run_extended_window_study(
    conn,
    *,
    date_start: str,
    date_end: str,
    hold_days: int = 5,
    target_pct: float = 8.0,
    min_leg_n: int = 30,
    segments: tuple[tuple[str, str, str], ...] = DEFAULT_SEGMENTS,
) -> dict[str, Any]:
    # 1) 全窗口：重用既有 sweep（只跑 none / w3_improving 兩格）
    sweep = mf.run_entry_multifactor_tp_sweep(
        conn,
        date_start=date_start,
        date_end=date_end,
        hold_days=hold_days,
        target_pct=target_pct,
        min_leg_n=min_leg_n,
        overlay_grid=TWO_CELL_GRID,
    )

    assert len(_LEG_CACHE) == 1, "expected exactly one cached leg collection"
    all_legs = next(iter(_LEG_CACHE.values()))
    base_legs = [leg for leg in all_legs if _matches_base_gap_gate(leg)]
    ovl_legs = [leg for leg in base_legs if _leg_matches_overlay(leg, OVERLAY_W3)]

    base_rets = [_tp_only_ret(leg) for leg in base_legs]
    ovl_rets = [_tp_only_ret(leg) for leg in ovl_legs]
    base_stats = _describe(base_rets)
    ovl_stats = _describe(ovl_rets)
    base_mean = float(base_stats.get("mean_pct") or 0.0)

    # overlay 對 base 補集（base 中不含 overlay 的腿）作額外對照
    rest_rets = [
        _tp_only_ret(leg)
        for leg in base_legs
        if not _leg_matches_overlay(leg, OVERLAY_W3)
    ]
    rest_stats = _describe(rest_rets, seed=20260711)

    # 2) 時間三切段
    seg_rows: list[dict[str, Any]] = []
    for seg_id, s, e in segments:
        seg_base = [leg for leg in base_legs if s <= str(leg.get("entry_date")) <= e]
        seg_ovl = [leg for leg in seg_base if _leg_matches_overlay(leg, OVERLAY_W3)]
        sb = _describe([_tp_only_ret(leg) for leg in seg_base])
        so = _describe([_tp_only_ret(leg) for leg in seg_ovl])
        seg_base_mean = sb.get("mean_pct")
        seg_ovl_mean = so.get("mean_pct")
        positive = bool(
            so["n"] > 0
            and seg_ovl_mean is not None
            and float(seg_ovl_mean) > 0
            and (seg_base_mean is None or float(seg_ovl_mean) > float(seg_base_mean))
        )
        seg_rows.append(
            {
                "segment": seg_id,
                "date_start": s,
                "date_end": e,
                "base": sb,
                "overlay": so,
                "overlay_minus_base_pp": (
                    round(float(seg_ovl_mean) - float(seg_base_mean), 3)
                    if seg_ovl_mean is not None and seg_base_mean is not None
                    else None
                ),
                "positive_effect": positive,
            }
        )

    # 3) 成功標準逐條判定（RP-1 §5）
    ovl_n = int(ovl_stats.get("n") or 0)
    ovl_mean = float(ovl_stats.get("mean_pct") or 0.0)
    ci = ovl_stats.get("bootstrap_ci95") or ovl_stats.get("t_ci95")
    ci_lower = float(ci[0]) if ci else None

    c1 = ovl_n >= min_leg_n
    c2_mean = ovl_mean >= target_pct
    c2_ci = bool(ci_lower is not None and ci_lower > base_mean)
    c2 = c2_mean and c2_ci
    n_positive_segments = sum(1 for r in seg_rows if r["positive_effect"])
    c3 = n_positive_segments >= 2

    if c1 and c2 and c3:
        verdict = "passed"
    elif (not c1) or (c1 and not c2_mean and not c2_ci):
        # n 依然過小（已無更早日內資料可延伸），或樣本夠大但效果回落至
        # base-only 水準且不顯著 → 過擬合判定
        verdict = "rejected"
    else:
        verdict = "mixed"

    criteria = [
        {
            "id": "C1",
            "desc": f"全歷史窗口 overlay n ≥ {min_leg_n}",
            "observed": f"n={ovl_n}",
            "passed": c1,
        },
        {
            "id": "C2",
            "desc": (
                f"overlay 均報酬 ≥ {target_pct:g}% 且 95% CI 下界 > base-only 均報酬"
                f"（{round(base_mean, 3)}%）"
            ),
            "observed": f"mean={round(ovl_mean, 3)}% · CI95={ci} · CI下界>{round(base_mean, 3)}%: {c2_ci}",
            "passed": c2,
        },
        {
            "id": "C3",
            "desc": "至少 2 個獨立時間切段觀察到正向效果（overlay 均>0 且 > 該段 base-only 均）",
            "observed": f"{n_positive_segments}/3 段正向",
            "passed": c3,
        },
    ]

    return {
        "schema": "abc_v3_f1_w3_improving_extended_window-v1",
        "plan": "RP-1 (reports/research/rrg/20260710_rp1_w3_improving_extended_window.md)",
        "hypothesis": "H-ENTRY-IMP-1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "date_start": date_start,
        "date_end": date_end,
        "hold_days": hold_days,
        "target_pct": target_pct,
        "min_leg_n": min_leg_n,
        "base_gate": sweep.get("base_gate"),
        "champion_tp": sweep.get("champion_tp"),
        "n_raw_legs": sweep.get("n_raw_legs"),
        "n_base_gate_legs": sweep.get("n_base_gate_legs"),
        "full_window": {
            "base_only": base_stats,
            "w3_improving": ovl_stats,
            "base_minus_overlay_complement": rest_stats,
            "overlay_minus_base_pp": round(ovl_mean - base_mean, 3),
        },
        "segments": seg_rows,
        "criteria": criteria,
        "verdict": verdict,
        "sweep_payload": sweep,
        "overlay_legs": _leg_rows(ovl_legs),
    }


def _fmt_stats(s: dict[str, Any]) -> str:
    if not s or not s.get("n"):
        return "n=0"
    parts = [f"n={s['n']}", f"均 **{s.get('mean_pct')}%**"]
    if s.get("median_pct") is not None:
        parts.append(f"中位 {s.get('median_pct')}%")
    if s.get("win_rate_pct") is not None:
        parts.append(f"勝率 {s.get('win_rate_pct')}%")
    if s.get("bootstrap_ci95"):
        parts.append(f"bootstrap CI95 [{s['bootstrap_ci95'][0]}, {s['bootstrap_ci95'][1]}]")
    if s.get("t_ci95"):
        parts.append(f"t CI95 [{s['t_ci95'][0]}, {s['t_ci95'][1]}]")
    return " · ".join(parts)


def render_md(p: dict[str, Any]) -> str:
    fw = p["full_window"]
    base = fw["base_only"]
    ovl = fw["w3_improving"]
    rest = fw["base_minus_overlay_complement"]
    bg = p.get("base_gate") or {}
    lines = [
        "# RP-1 · W3 Improving overlay · 延伸歷史窗口驗證（hold 5d）",
        "",
        f"假說 `H-ENTRY-MF-1` · 窗口 **{p['date_start']} .. {p['date_end']}** · "
        f"raw legs **{p['n_raw_legs']}** · base gate **{p['n_base_gate_legs']}** · "
        f"目標 均 ≥{p['target_pct']:g}% · n ≥{p['min_leg_n']}",
        "",
        f"## 最終判定：**{p['verdict'].upper()}**",
        "",
        "| # | 成功標準 | 觀察值 | 判定 |",
        "|---|----------|--------|:----:|",
    ]
    for c in p["criteria"]:
        lines.append(
            f"| {c['id']} | {c['desc']} | {c['observed']} | {'✓' if c['passed'] else '✗'} |"
        )
    lines.extend(
        [
            "",
            "## 全窗口（TP-only，champion exit）",
            "",
            f"- Base gate：`{bg.get('gap_band')}` · poll≥{bg.get('min_poll_idx')} · 無 rebound",
            f"- Exit：`{p.get('champion_tp')}` · NEVER_SL",
            f"- **base only**：{_fmt_stats(base)}",
            f"- **W3 Improving overlay**：{_fmt_stats(ovl)}",
            f"- base 補集（排除 overlay 腿）：{_fmt_stats(rest)}",
            f"- overlay − base 均差：**{fw['overlay_minus_base_pp']} pp**",
            "",
            "## 時間三切段（各約 10 個月）",
            "",
            "| 段 | 窗口 | base n | base 均% | overlay n | overlay 均% | Δ pp | 正向 |",
            "|----|------|-------:|---------:|----------:|------------:|-----:|:----:|",
        ]
    )
    for r in p["segments"]:
        b, o = r["base"], r["overlay"]
        lines.append(
            f"| {r['segment']} | {r['date_start']}..{r['date_end']} | {b.get('n', 0)} | "
            f"{b.get('mean_pct', '—')} | {o.get('n', 0)} | {o.get('mean_pct', '—')} | "
            f"{r.get('overlay_minus_base_pp', '—')} | {'✓' if r['positive_effect'] else '✗'} |"
        )
    lines.extend(
        [
            "",
            "（seg3 = H-ENTRY-MF-1 原 10 個月窗口 2025-09-01..2026-07-01，作一致性對照）",
            "",
            "## Overlay 全部樣本（entry_date · stock · hold / TP-only 報酬）",
            "",
            "| entry_date | stock | hold% | TP-only% |",
            "|------------|-------|------:|---------:|",
        ]
    )
    for row in p["overlay_legs"]:
        lines.append(
            f"| {row['entry_date']} | {row['stock_id']} | {row['hold_ret_pct']} | "
            f"{row['tp_only_ret_pct']} |"
        )
    lines.extend(
        [
            "",
            "## 設計說明",
            "",
            "- 樣本為原始 ABC-V3-F1 first-trigger stock-day（無槽位/資金限制），"
            "重用 `run_entry_multifactor_tp_sweep`（只跑 none / w3_improving 兩格）",
            "- 日內 1m kbar 自 2024 年起才完整，故窗口起點 = 2024-01-02（使用者裁定，不再往前）",
            "- CI：bootstrap 10000 次重抽（均值百分位法）為主，t 分布近似為輔（無 scipy，"
            "t 臨界值用 Cornish-Fisher 展開）",
            "- 切段正向效果定義：該段 overlay n>0、TP-only 均>0 且 > 該段 base-only 均",
            "- 計劃書：`20260710_rp1_w3_improving_extended_window.md` · "
            "對照報告：`20260710_abc_v3_f1_entry_multifactor_tp_hold5d_10m.md`",
        ]
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="RP-1: W3 Improving overlay extended-window validation"
    )
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--date-start", default="2024-01-02")
    ap.add_argument("--date-end", default="2026-07-01")
    ap.add_argument("--hold-days", type=int, default=5)
    ap.add_argument("--target", type=float, default=8.0)
    ap.add_argument("--min-leg-n", type=int, default=30)
    ap.add_argument(
        "--out",
        type=Path,
        default=RESEARCH_RRG / "20260710_abc_v3_f1_w3_improving_extended_window.json",
    )
    ap.add_argument(
        "--collect-chunk",
        nargs=2,
        metavar=("CHUNK_START", "CHUNK_END"),
        help="只收集該日期段的 legs 寫入快取後結束（沙箱 45s 上限的分段模式）",
    )
    ap.add_argument(
        "--analyze",
        action="store_true",
        help="從快取載入全部 legs 直接分析（需先跑完所有 --collect-chunk）",
    )
    ap.add_argument(
        "--cache-dir",
        type=Path,
        default=ROOT / "logs" / "rp1_leg_cache",
    )
    args = ap.parse_args(argv)

    if not args.db.exists():
        print(f"BLOCKER: database not found: {args.db}", file=sys.stderr)
        return 1

    conn = connect(args.db)
    try:
        if args.collect_chunk:
            collect_chunk(
                conn,
                chunk_start=args.collect_chunk[0],
                chunk_end=args.collect_chunk[1],
                hold_days=args.hold_days,
                cache_dir=args.cache_dir,
            )
            return 0
        if args.analyze:
            global _PRELOADED_LEGS
            _PRELOADED_LEGS = load_cached_legs(
                args.cache_dir,
                date_start=args.date_start,
                date_end=args.date_end,
                hold_days=args.hold_days,
            )
        payload = run_extended_window_study(
            conn,
            date_start=args.date_start,
            date_end=args.date_end,
            hold_days=args.hold_days,
            target_pct=args.target,
            min_leg_n=args.min_leg_n,
        )
    finally:
        conn.close()

    out_json = args.out
    out_md = out_json.with_suffix(".md")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_md(payload), encoding="utf-8")

    fw = payload["full_window"]
    print(f"verdict={payload['verdict']}")
    print(f"raw={payload['n_raw_legs']} base_gate={payload['n_base_gate_legs']}")
    print(f"base_only: {fw['base_only']}")
    print(f"w3_improving: {fw['w3_improving']}")
    for r in payload["segments"]:
        print(
            f"{r['segment']} {r['date_start']}..{r['date_end']}: "
            f"base n={r['base'].get('n')} mean={r['base'].get('mean_pct')} | "
            f"overlay n={r['overlay'].get('n')} mean={r['overlay'].get('mean_pct')} "
            f"positive={r['positive_effect']}"
        )
    for c in payload["criteria"]:
        print(f"{c['id']} passed={c['passed']} :: {c['observed']}")
    print(f"\nWrote {out_json}\nWrote {out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
