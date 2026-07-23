#!/usr/bin/env python3
"""Expert-pool T+1 open 5m/30m avoid-rule grid backtest (research only).

  PYTHONPATH=src .venv/bin/python scripts/research/run_expert_pool_t1_intraday_avoid_grid.py

Reads: expert_pool/knowledge/trades/*.json · data/stocks.db stock_kbar_1m
Writes: hypotheses/avoid_morphology/H_T1_INTRADAY_GRID.{json,md}
"""
from __future__ import annotations

import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from itertools import product
from pathlib import Path
from statistics import median

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from stock_db import DEFAULT_DB_PATH  # noqa: E402

# Reuse loaders / enrichment from cross scan SSOT
sys.path.insert(0, str(ROOT / "scripts/research"))
import run_expert_pool_cross_avoid_morphology_scan as cross  # noqa: E402

OUT_DIR = cross.OUT_DIR
OOS_CUT = "2026-01-01"
SOURCE = cross.SOURCE

GAP_THRESH = [0.02, 0.03, 0.05]
OPEN5_THRESH = [-0.005, -0.01, -0.02]
MDD5_THRESH = [-0.01, -0.02, -0.03]
RET30_THRESH = [0.0, -0.01, -0.02]
FAIL_BREAK_OPTS = [False, True]
WEAK_VS_T_OPTS = [False, True]


def connect_ro(db_path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    return cross.connect_ro(db_path)


def load_backfill_meta() -> dict | None:
    for name in ("backfill_summary.json", "backfill_gaps.json"):
        p = OUT_DIR / name
        if p.is_file():
            raw = json.loads(p.read_text(encoding="utf-8"))
            if name == "backfill_gaps.json":
                return {"file": name, "n_gaps": len(raw), "gaps": raw}
            return {"file": name, "summary": raw}
    return None


def raw_intraday_values(
    bars: list[dict] | None, entry_open: float, t_close: float
) -> dict | None:
    if not bars or entry_open <= 0 or t_close <= 0:
        return None
    o = entry_open
    bars5 = [b for b in bars if b["minute"] <= "09:05:00"]
    bars30 = [b for b in bars if b["minute"] <= "09:30:00"]
    if not bars5 or not bars30:
        return None
    last_min = max(b["minute"] for b in bars30)
    if last_min < "09:30:00":
        return None
    lo5 = min(b["low"] for b in bars5)
    c5 = bars5[-1]["close"]
    ret5 = c5 / o - 1.0
    lo5_ret = lo5 / o - 1.0
    c30 = bars30[-1]["close"]
    ret30 = c30 / o - 1.0
    hi30 = max(b["high"] for b in bars30)
    gap = o / t_close - 1.0
    return {
        "gap": gap,
        "open5_ret": ret5,
        "mdd5": lo5_ret,
        "ret30": ret30,
        "fail_break": bool(hi30 > o * 1.01 and c30 < o),
        "weak_vs_t": bool(c30 < t_close and ret30 < 0),
    }


def build_intraday_rows(conn: sqlite3.Connection, trades: list[dict]) -> tuple[list[dict], dict]:
    sids = {t["stock_id"] for t in trades}
    daily_pairs: set[tuple[str, str]] = set()
    entry_pairs: set[tuple[str, str]] = set()
    for t in trades:
        daily_pairs.add((t["stock_id"], t["signal_date"]))
        entry_pairs.add((t["stock_id"], t["entry_date"]))
    daily = cross.fetch_daily_bars(conn, daily_pairs)
    kb1m = cross.fetch_1m_morning(conn, entry_pairs)

    rows: list[dict] = []
    missing = 0
    for t in trades:
        sid = t["stock_id"]
        T = t["signal_date"]
        bar_t = daily.get((sid, T))
        bars = kb1m.get((sid, t["entry_date"]))
        raw = raw_intraday_values(bars, t["entry_open"], bar_t["close"] if bar_t else 0.0)
        if raw is None:
            missing += 1
            continue
        rows.append(
            {
                "stock_id": sid,
                "signal_date": T,
                "entry_date": t["entry_date"],
                "year": T[:4],
                "is_oos": T >= OOS_CUT,
                "r_adj_pct": t["r_adj_pct"],
                **raw,
            }
        )
    cov = {
        "n_trades_total": len(trades),
        "n_with_1m": len(rows),
        "n_missing_1m": missing,
        "coverage_pct": round(len(rows) / len(trades) * 100, 1) if trades else 0.0,
    }
    return rows, cov


def rule_hit(row: dict, spec: dict) -> bool:
    if spec.get("gap_chase") is not None and row["gap"] < spec["gap_chase"]:
        return False
    if spec.get("open5_ret") is not None and row["open5_ret"] > spec["open5_ret"]:
        return False
    if spec.get("mdd5") is not None and row["mdd5"] > spec["mdd5"]:
        return False
    if spec.get("ret30") is not None and row["ret30"] > spec["ret30"]:
        return False
    if spec.get("fail_break") and not row["fail_break"]:
        return False
    if spec.get("weak_vs_t") and not row["weak_vs_t"]:
        return False
    return True


def fmt_thresh(x: float | None, *, pct: bool = True) -> str:
    if x is None:
        return "—"
    if pct:
        return f"{x * 100:+.1f}%".replace("+0.0%", "0%")
    return str(x)


def rule_label(spec: dict) -> str:
    parts: list[str] = []
    if spec.get("gap_chase") is not None:
        parts.append(f"gap≥{spec['gap_chase'] * 100:.0f}%")
    if spec.get("open5_ret") is not None:
        parts.append(f"open5≤{spec['open5_ret'] * 100:.1f}%")
    if spec.get("mdd5") is not None:
        parts.append(f"mdd5≤{spec['mdd5'] * 100:.1f}%")
    if spec.get("ret30") is not None:
        parts.append(f"ret30≤{spec['ret30'] * 100:.1f}%")
    if spec.get("fail_break"):
        parts.append("fail_break")
    if spec.get("weak_vs_t"):
        parts.append("vs_T_weak")
    return " ∧ ".join(parts) if parts else "EMPTY"


def summarize_split(rows: list[dict], spec: dict) -> dict:
    skip_r, keep_r = [], []
    for row in rows:
        if rule_hit(row, spec):
            skip_r.append(row["r_adj_pct"])
        else:
            keep_r.append(row["r_adj_pct"])

    def grp(xs: list[float]) -> dict:
        if not xs:
            return {"n": 0, "med_r_adj": None, "win_rate": None}
        return {
            "n": len(xs),
            "med_r_adj": round(median(xs), 2),
            "win_rate": round(sum(1 for x in xs if x > 0) / len(xs) * 100, 1),
        }

    sk = grp(skip_r)
    kp = grp(keep_r)
    delta = None
    if sk["med_r_adj"] is not None and kp["med_r_adj"] is not None:
        delta = round(kp["med_r_adj"] - sk["med_r_adj"], 2)
    n = len(rows)
    return {
        "n_total": n,
        "n_skip": sk["n"],
        "n_keep": kp["n"],
        "skip": sk,
        "keep": kp,
        "delta_med_keep_minus_skip": delta,
        "skip_rate_pct": round(sk["n"] / n * 100, 1) if n else 0.0,
    }


def eval_rule(rows: list[dict], spec: dict, *, kind: str) -> dict:
    pooled = summarize_split(rows, spec)
    is_rows = [r for r in rows if not r["is_oos"]]
    oos_rows = [r for r in rows if r["is_oos"]]
    is_s = summarize_split(is_rows, spec)
    oos_s = summarize_split(oos_rows, spec)
    same_dir = None
    if is_s["delta_med_keep_minus_skip"] is not None and oos_s["delta_med_keep_minus_skip"] is not None:
        same_dir = (is_s["delta_med_keep_minus_skip"] > 0) == (oos_s["delta_med_keep_minus_skip"] > 0)
    yr_map: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        yr_map[r["year"]].append(r)
    by_year: dict[str, dict] = {}
    for yr, grp in sorted(yr_map.items()):
        by_year[yr] = summarize_split(grp, spec)
    return {
        "rule_id": rule_label(spec),
        "kind": kind,
        "spec": spec,
        "pooled": pooled,
        "is_pre_2026": is_s,
        "oos_from_2026": oos_s,
        "is_oos_same_direction": same_dir,
        "by_year": by_year,
    }


def build_grid_specs() -> list[tuple[str, dict]]:
    specs: list[tuple[str, dict]] = []

    for g in GAP_THRESH:
        specs.append(("single", {"gap_chase": g}))
    for o5 in OPEN5_THRESH:
        specs.append(("single", {"open5_ret": o5}))
    for m in MDD5_THRESH:
        specs.append(("single", {"mdd5": m}))
    for r30 in RET30_THRESH:
        specs.append(("single", {"ret30": r30}))
    specs.append(("single", {"fail_break": True}))
    specs.append(("single", {"weak_vs_t": True}))

    combo_pairs = [
        ("combo2", {"gap_chase": 0.03, "open5_ret": -0.01}),
        ("combo2", {"gap_chase": 0.03, "open5_ret": -0.005}),
        ("combo2", {"gap_chase": 0.05, "open5_ret": -0.01}),
        ("combo2", {"open5_ret": -0.01, "ret30": 0.0}),
        ("combo2", {"open5_ret": -0.01, "mdd5": -0.02}),
        ("combo2", {"ret30": 0.0, "fail_break": True}),
        ("combo2", {"ret30": -0.01, "weak_vs_t": True}),
        ("combo2", {"gap_chase": 0.03, "ret30": 0.0}),
        ("combo2", {"fail_break": True, "weak_vs_t": True}),
    ]
    specs.extend(combo_pairs)

    combo3 = [
        ("combo3", {"gap_chase": 0.03, "open5_ret": -0.01, "ret30": 0.0}),
        ("combo3", {"gap_chase": 0.03, "open5_ret": -0.01, "mdd5": -0.02}),
        ("combo3", {"gap_chase": 0.05, "open5_ret": -0.01, "ret30": 0.0}),
        ("combo3", {"open5_ret": -0.01, "mdd5": -0.02, "ret30": 0.0}),
        ("combo3", {"gap_chase": 0.03, "ret30": 0.0, "fail_break": True}),
        ("combo3", {"open5_ret": -0.01, "ret30": 0.0, "weak_vs_t": True}),
    ]
    specs.extend(combo3)

    # Full threshold grid for canonical 2-factor gap + open5
    for g, o5 in product([0.03, 0.05], [-0.01, -0.02]):
        specs.append(("combo2_grid", {"gap_chase": g, "open5_ret": o5}))

    return specs


def pick_stable(rules: list[dict]) -> dict:
    stable = []
    for r in rules:
        p = r["pooled"]
        if p["n_skip"] < 15:
            continue
        d = p["delta_med_keep_minus_skip"]
        if d is None or d < 2.0:
            continue
        if not r.get("is_oos_same_direction"):
            continue
        oos = r["oos_from_2026"]
        if oos["n_skip"] < 5:
            continue
        stable.append(r)
    stable.sort(
        key=lambda x: (
            -(x["pooled"]["delta_med_keep_minus_skip"] or 0),
            -x["pooled"]["n_skip"],
        )
    )
    return {
        "criteria": "n_skip≥15 · Δ≥+2pp · IS/OOS 同向 · OOS n_skip≥5",
        "top": stable[:8],
    }


def render_md(payload: dict) -> str:
    cov = payload["coverage"]
    proto = payload["protocol"]
    lines = [
        "# Expert-pool · T+1 開盤 5 分／30 分避險規則網格",
        "",
        f"- 生成：{payload['generated']}",
        f"- 樣本：{cov['n_trades_total']} 筆 ledger · **有 1m 覆蓋 {cov['n_with_1m']} 筆 "
        f"({cov['coverage_pct']}%)** · 缺 K 剔除",
        f"- 標籤：`r_adj_pct` · β={proto['beta']}×{proto['bench']} · {proto['cost_bps']}bps · L1H7",
        f"- 盤中：`stock_kbar_1m` {proto['intraday_window']} · source 優先 {SOURCE}",
        f"- OOS 切點：signal_date ≥ {OOS_CUT}",
        "",
    ]
    bf = payload.get("backfill")
    if bf:
        lines.append(f"- Backfill 狀態：`{bf['file']}`" + (
            f" · n_gaps={bf.get('n_gaps')}" if bf.get("n_gaps") is not None else ""
        ))
        lines.append("")

    lines += [
        "## 1. 穩定規則（IS/OOS 同向 · Δ≥+2pp · n_skip≥15）",
        "",
    ]
    stable = payload["stable"]["top"]
    if not stable:
        lines.append("_無規則同時滿足穩定門檻；見下方全網格排序。_")
    else:
        lines.append(
            "| 規則 | n_skip | skip med | keep med | Δ | skip率 | IS Δ | OOS Δ | 同向 |"
        )
        lines.append("|------|-------:|---------:|---------:|--:|------:|-----:|------:|:----:|")
        for r in stable:
            p, is_s, oos_s = r["pooled"], r["is_pre_2026"], r["oos_from_2026"]
            lines.append(
                f"| {r['rule_id']} | {p['n_skip']} | {p['skip']['med_r_adj']}% | "
                f"{p['keep']['med_r_adj']}% | {p['delta_med_keep_minus_skip']} | "
                f"{p['skip_rate_pct']}% | {is_s['delta_med_keep_minus_skip']} | "
                f"{oos_s['delta_med_keep_minus_skip']} | "
                f"{'Y' if r['is_oos_same_direction'] else 'N'} |"
            )

    lines += [
        "",
        "## 2. 單因子 Top（依 pooled Δ 排序 · n_skip≥10）",
        "",
        "| 規則 | n_skip | skip med | keep med | Δ | skip win | keep win | IS Δ | OOS Δ | 同向 |",
        "|------|-------:|---------:|---------:|--:|---------:|---------:|-----:|------:|:----:|",
    ]
    singles = [
        r for r in payload["rules"]
        if r["kind"] == "single" and r["pooled"]["n_skip"] >= 10
    ]
    singles.sort(key=lambda x: -(x["pooled"]["delta_med_keep_minus_skip"] or -999))
    for r in singles[:12]:
        p, is_s, oos_s = r["pooled"], r["is_pre_2026"], r["oos_from_2026"]
        lines.append(
            f"| {r['rule_id']} | {p['n_skip']} | {p['skip']['med_r_adj']}% | "
            f"{p['keep']['med_r_adj']}% | {p['delta_med_keep_minus_skip']} | "
            f"{p['skip']['win_rate']}% | {p['keep']['win_rate']}% | "
            f"{is_s['delta_med_keep_minus_skip']} | {oos_s['delta_med_keep_minus_skip']} | "
            f"{'Y' if r['is_oos_same_direction'] else 'N'} |"
        )

    lines += [
        "",
        "## 3. 組合規則 Top（2–3 因子 AND · n_skip≥8）",
        "",
        "| 規則 | kind | n_skip | Δ | skip率 | IS Δ | OOS Δ | 同向 |",
        "|------|------|-------:|--:|------:|-----:|------:|:----:|",
    ]
    combos = [
        r for r in payload["rules"]
        if r["kind"].startswith("combo") and r["pooled"]["n_skip"] >= 8
    ]
    combos.sort(key=lambda x: -(x["pooled"]["delta_med_keep_minus_skip"] or -999))
    for r in combos[:15]:
        p, is_s, oos_s = r["pooled"], r["is_pre_2026"], r["oos_from_2026"]
        lines.append(
            f"| {r['rule_id']} | {r['kind']} | {p['n_skip']} | "
            f"{p['delta_med_keep_minus_skip']} | {p['skip_rate_pct']}% | "
            f"{is_s['delta_med_keep_minus_skip']} | {oos_s['delta_med_keep_minus_skip']} | "
            f"{'Y' if r['is_oos_same_direction'] else 'N'} |"
        )

    lines += [
        "",
        "## 4. 分年 pooled Δ（Top 3 穩定規則）",
        "",
    ]
    for r in stable[:3]:
        lines.append(f"### {r['rule_id']}")
        lines.append("| 年 | n_skip | Δ |")
        lines.append("|----|-------:|--:|")
        for yr, ys in sorted(r["by_year"].items()):
            lines.append(
                f"| {yr} | {ys['n_skip']} | {ys['delta_med_keep_minus_skip']} |"
            )
        lines.append("")

    lines += [
        "## 5. 過擬合與限制",
        "",
        "- **多重檢定**：本網格含數十至上百有效組合，未校正 p 值；Top 規則需 freeze + per-sid 再驗。",
        "- **OOS 樣本薄**：2026 起僅部分訊號；OOS 同向必要但不充分。",
        "- **缺 K 偏差**：僅覆蓋有 1m 的子樣本；backfill 後覆蓋率見上，仍可能偏向較新／較大票。",
        "- **Δ 解讀**：Δ = keep med − skip med；正值代表 skip 組更差，規則有避險方向性。",
        "- **倖存者**：樣本來自已採納 expert-pool champion，正超額先驗偏高。",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    trades = cross.load_all_trades()
    conn = connect_ro()
    try:
        rows, cov = build_intraday_rows(conn, trades)
    finally:
        conn.close()

    specs = build_grid_specs()
    rules = [eval_rule(rows, spec, kind=kind) for kind, spec in specs]
    rules.sort(key=lambda x: -(x["pooled"]["delta_med_keep_minus_skip"] or -999))

    payload = {
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "protocol": {
            "outcome": "r_adj_pct",
            "beta": 1.15,
            "bench": "IX0001",
            "cost_bps": 30,
            "hold": "L1H7",
            "daily_source": SOURCE,
            "intraday_window": "09:00-09:30",
            "oos_cut_signal_date": OOS_CUT,
            "grid": {
                "gap_chase": GAP_THRESH,
                "open5_ret": OPEN5_THRESH,
                "mdd5": MDD5_THRESH,
                "ret30": RET30_THRESH,
                "fail_break": FAIL_BREAK_OPTS,
                "vs_T_close_weak": WEAK_VS_T_OPTS,
            },
            "feature_defs": {
                "gap_chase": "entry_open / T_close - 1 ≥ threshold → skip",
                "open5_ret": "09:05 close vs open return ≤ threshold → skip",
                "mdd5": "09:00-09:05 min low vs open ≤ threshold → skip",
                "ret30": "09:30 close vs open return ≤ threshold → skip",
                "fail_break": "30m high>open×1.01 ∧ 30m close<open → skip",
                "vs_T_close_weak": "09:30 close<T close ∧ ret30<0 → skip",
            },
        },
        "backfill": load_backfill_meta(),
        "coverage": cov,
        "n_rules": len(rules),
        "rules": rules,
        "stable": pick_stable(rules),
    }

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    json_path = OUT_DIR / "H_T1_INTRADAY_GRID.json"
    md_path = OUT_DIR / "H_T1_INTRADAY_GRID.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(render_md(payload), encoding="utf-8")

    print(f"wrote {json_path}")
    print(f"wrote {md_path}")
    print(f"rows={len(rows)} rules={len(rules)} stable={len(payload['stable']['top'])}")
    for r in payload["stable"]["top"][:5]:
        p = r["pooled"]
        print(
            f"  {r['rule_id']:40s} n_skip={p['n_skip']:3d} "
            f"Δ={p['delta_med_keep_minus_skip']} same_oos={r['is_oos_same_direction']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
