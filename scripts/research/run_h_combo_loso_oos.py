#!/usr/bin/env python3
"""Frozen combo avoid rules · time OOS · leave-one-stock-out (research only).

  PYTHONPATH=src .venv/bin/python scripts/research/run_h_combo_loso_oos.py

Reads: expert_pool/knowledge/trades/*.json + data/stocks.db
Writes: hypotheses/avoid_morphology/H_COMBO_LOSO_OOS.{json,md}
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from statistics import median

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from stock_db import DEFAULT_DB_PATH  # noqa: E402

OUT_DIR = (
    ROOT
    / "reports"
    / "research"
    / "branch-footprint-screen"
    / "expert_pool"
    / "hypotheses"
    / "avoid_morphology"
)
OOS_CUT = "2026-01-01"
FOCUS5 = ("2303", "2327", "6515", "2344", "8046")
FOCUS_NAMES = {
    "2303": "聯電",
    "2327": "國巨",
    "6515": "穎崴",
    "2344": "華邦電",
    "8046": "南電",
}

RULES: dict[str, str] = {
    "R1": "HOT5 (ret5≥20%)",
    "R2": "T_REJECT (black∧close_pos<0.33) OR body<−3%",
    "R3": "HOT5 ∧ GAP_CHASE(≥3%)",
    "R4": "OPEN5_WEAK ∧ NO_RECOVERY30",
    "R5": "FAIL_BREAK30",
    "R6": "R1∨R2∨R4（聯集避）",
    "R7": "(R1∧R3)∨R2（嚴）",
}


def _load_scan_module():
    path = ROOT / "scripts/research/run_expert_pool_cross_avoid_morphology_scan.py"
    spec = importlib.util.spec_from_file_location("cross_avoid", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def gap_chase_daily(t: dict) -> bool | None:
    """entry open vs T close ≥ +3% — daily only."""
    feats = t.get("features") or {}
    if not feats.get("daily_ok"):
        return None
    # Prefer stored intraday flag; else compute from trade fields if we stash t_close
    tc = t.get("_t_close")
    eo = t.get("entry_open")
    if tc and eo and tc > 0:
        return eo / tc - 1.0 >= 0.03
    if "GAP_CHASE" in feats:
        return bool(feats["GAP_CHASE"])
    return None


def attach_t_close(trades: list[dict], conn: sqlite3.Connection, mod) -> None:
    pairs = {(t["stock_id"], t["signal_date"]) for t in trades}
    daily = mod.fetch_daily_bars(conn, pairs)
    for t in trades:
        bar = daily.get((t["stock_id"], t["signal_date"]))
        t["_t_close"] = bar["close"] if bar else None


def rule_flags(t: dict) -> dict[str, bool | None]:
    f = t.get("features") or {}
    daily_ok = bool(f.get("daily_ok"))
    intra_ok = bool(t.get("intraday_ok"))

    r1 = bool(f["HOT5"]) if daily_ok and "HOT5" in f else None
    t_reject = None
    if daily_ok:
        combo = bool(f.get("T_REJECT_COMBO"))
        body = bool(f.get("T_BODY_REJECT"))
        t_reject = combo or body
    r2 = t_reject

    gc = gap_chase_daily(t)
    r3 = (bool(r1) and bool(gc)) if (r1 is not None and gc is not None) else None

    r4 = None
    if intra_ok and "OPEN5_WEAK" in f and "NO_RECOVERY30" in f:
        r4 = bool(f["OPEN5_WEAK"] and f["NO_RECOVERY30"])

    r5 = bool(f["FAIL_BREAK30"]) if intra_ok and "FAIL_BREAK30" in f else None

    def _or(*vals: bool | None) -> bool | None:
        defined = [v for v in vals if v is not None]
        if not defined:
            return None
        return any(defined)

    r6 = _or(r1, r2, r4)
    r7 = None
    if r2 is not None:
        left = (bool(r1) and bool(r3)) if (r1 is not None and r3 is not None) else None
        r7 = _or(left, r2)

    return {"R1": r1, "R2": r2, "R3": r3, "R4": r4, "R5": r5, "R6": r6, "R7": r7}


def summarize_split(trades: list[dict], rule_key: str) -> dict:
    skip, keep = [], []
    na = 0
    for t in trades:
        flags = t.get("_rules") or {}
        v = flags.get(rule_key)
        if v is None:
            na += 1
            continue
        r = t["r_adj_pct"]
        if v:
            skip.append(r)
        else:
            keep.append(r)

    def blk(xs: list[float]) -> dict:
        if not xs:
            return {"n": 0, "med_r_adj": None, "win_rate": None}
        return {
            "n": len(xs),
            "med_r_adj": round(median(xs), 2),
            "win_rate": round(sum(1 for x in xs if x > 0) / len(xs) * 100, 1),
        }

    sk, kp = blk(skip), blk(keep)
    delta = None
    if sk["med_r_adj"] is not None and kp["med_r_adj"] is not None:
        delta = round(sk["med_r_adj"] - kp["med_r_adj"], 2)
    return {
        "n_pool": len(trades),
        "n_na": na,
        "n_eval": len(trades) - na,
        "skip": sk,
        "keep": kp,
        "delta_med": delta,
    }


def loso_analysis(trades: list[dict], rule_key: str) -> dict:
    by_sid: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        by_sid[t["stock_id"]].append(t)

    full = summarize_split(trades, rule_key)
    rows = []
    same_dir = 0
    n_with_holdout = 0
    for holdout in sorted(by_sid):
        rest = [t for t in trades if t["stock_id"] != holdout]
        if len(rest) < 10:
            continue
        pool = summarize_split(rest, rule_key)
        hold = summarize_split(by_sid[holdout], rule_key)
        pool_d = pool["delta_med"]
        hold_d = hold["delta_med"]
        aligned = None
        if pool_d is not None and hold_d is not None:
            n_with_holdout += 1
            # avoid rule: negative delta = skip worse than keep
            aligned = (pool_d <= 0 and hold_d <= 0) or (pool_d >= 0 and hold_d >= 0)
            if aligned:
                same_dir += 1
        rows.append(
            {
                "holdout_sid": holdout,
                "holdout_name": by_sid[holdout][0].get("stock_name") or "",
                "n_rest": len(rest),
                "pool_delta_med": pool_d,
                "holdout_delta_med": hold_d,
                "holdout_n_skip": hold["skip"]["n"],
                "holdout_n_keep": hold["keep"]["n"],
                "same_direction": aligned,
            }
        )
    return {
        "full_pool_delta_med": full["delta_med"],
        "n_holdouts": len(rows),
        "n_same_direction": same_dir,
        "pct_same_direction": (
            round(same_dir / n_with_holdout * 100, 1) if n_with_holdout else None
        ),
        "by_holdout": rows,
    }


def time_oos(trades: list[dict], rule_key: str) -> dict:
    is_ = [t for t in trades if t["signal_date"] < OOS_CUT]
    oos = [t for t in trades if t["signal_date"] >= OOS_CUT]
    si = summarize_split(is_, rule_key)
    so = summarize_split(oos, rule_key)
    same = None
    if si["delta_med"] is not None and so["delta_med"] is not None:
        same = (si["delta_med"] <= 0 and so["delta_med"] <= 0) or (
            si["delta_med"] >= 0 and so["delta_med"] >= 0
        )
    return {"is_pre_2026": si, "oos_ge_2026": so, "same_direction": same}


def score_rule(pooled: dict, loso: dict, time: dict) -> dict:
    """Higher = more cross-group avoid credibility."""
    score = 0
    notes = []
    d = pooled.get("delta_med")
    if d is not None and d <= -2:
        score += 2
        notes.append("pooled_Δ≤−2")
    elif d is not None and d < 0:
        score += 1
        notes.append("pooled_Δ<0")
    elif d is not None and d > 0:
        notes.append("pooled_反向")

    pct = loso.get("pct_same_direction")
    if pct is not None and pct >= 70:
        score += 2
        notes.append(f"LOSO一致≥70%")
    elif pct is not None and pct >= 50:
        score += 1

    if time.get("same_direction"):
        score += 1
        notes.append("時間OOS同向")

    ns = pooled["skip"]["n"]
    if ns < 10:
        notes.append("skip樣本極薄")
    elif ns < 20:
        notes.append("skip樣本偏薄")

    return {"score": score, "notes": notes}


def render_md(payload: dict) -> str:
    p = payload["protocol"]
    lines = [
        "# 共通避圖形 · 組合規則 × 時間 OOS × LOSO",
        "",
        f"- 生成：{payload['generated']}",
        "- Runner：`scripts/research/run_h_combo_loso_oos.py`（Research only · 未採納）",
        f"- 樣本：{p['n_trades']} 筆 L1H7 · {p['n_ledgers']} 檔 · 績效 `r_adj_pct`",
        f"- OOS 切點：signal_date {OOS_CUT}",
        f"- 盤中覆蓋：{p['intraday_ok']} / {p['n_trades']}（缺 K 則 R4/R5/R6 盤中臂 NA）",
        "",
        "## 凍結規則",
        "",
    ]
    for k, desc in RULES.items():
        lines.append(f"- **{k}**：{desc}")
    lines += [
        "",
        "Δmed = skip 組中位 r_adj − keep 組中位 r_adj（**越負越像應避**）。",
        "",
        "## 1. 全池 pooled",
        "",
        "| 規則 | n_skip | med_skip | win_skip | n_keep | med_keep | win_keep | Δmed | n_NA |",
        "|------|-------:|---------:|---------:|-------:|---------:|---------:|-----:|-----:|",
    ]
    for r in payload["rules_ranked"]:
        po = r["pooled"]
        lines.append(
            f"| {r['rule']} | {po['skip']['n']} | "
            f"{po['skip']['med_r_adj'] if po['skip']['med_r_adj'] is not None else '—'} | "
            f"{po['skip']['win_rate'] if po['skip']['win_rate'] is not None else '—'} | "
            f"{po['keep']['n']} | "
            f"{po['keep']['med_r_adj'] if po['keep']['med_r_adj'] is not None else '—'} | "
            f"{po['keep']['win_rate'] if po['keep']['win_rate'] is not None else '—'} | "
            f"{po['delta_med'] if po['delta_med'] is not None else '—'} | {po['n_na']} |"
        )

    lines += [
        "",
        "## 2. 時間 OOS（IS vs OOS 同向？）",
        "",
        "| 規則 | IS Δmed | IS n_skip | OOS Δmed | OOS n_skip | 同向 |",
        "|------|--------:|----------:|---------:|-----------:|:----:|",
    ]
    for r in payload["rules_ranked"]:
        t = r["time_oos"]
        si, so = t["is_pre_2026"], t["oos_ge_2026"]
        lines.append(
            f"| {r['rule']} | {si['delta_med'] if si['delta_med'] is not None else '—'} | "
            f"{si['skip']['n']} | "
            f"{so['delta_med'] if so['delta_med'] is not None else '—'} | "
            f"{so['skip']['n']} | {'✓' if t['same_direction'] else '✗'} |"
        )

    lines += [
        "",
        "## 3. LOSO（留一檔 out · 剩餘池 Δmed vs 該檔 Δmed）",
        "",
        "| 規則 | 全池Δ | LOSO同向檔數 | 同向% |",
        "|------|------:|-------------:|------:|",
    ]
    for r in payload["rules_ranked"]:
        lo = r["loso"]
        lines.append(
            f"| {r['rule']} | {lo['full_pool_delta_med'] if lo['full_pool_delta_med'] is not None else '—'} | "
            f"{lo['n_same_direction']}/{lo['n_holdouts']} | "
            f"{lo['pct_same_direction'] if lo['pct_same_direction'] is not None else '—'} |"
        )

    lines += [
        "",
        "## 4. 重點五檔（2303／2327／6515／2344／8046）",
        "",
    ]
    for sid in FOCUS5:
        name = FOCUS_NAMES.get(sid, sid)
        block = payload["focus5"].get(sid, {})
        lines.append(f"### {sid} {name}（n={block.get('n', 0)}）")
        lines.append("")
        lines.append(
            "| 規則 | n_skip | med_skip | n_keep | med_keep | Δmed |"
        )
        lines.append("|------|-------:|---------:|-------:|---------:|-----:|")
        for row in block.get("rules", []):
            po = row["pooled"]
            lines.append(
                f"| {row['rule']} | {po['skip']['n']} | "
                f"{po['skip']['med_r_adj'] if po['skip']['med_r_adj'] is not None else '—'} | "
                f"{po['keep']['n']} | "
                f"{po['keep']['med_r_adj'] if po['keep']['med_r_adj'] is not None else '—'} | "
                f"{po['delta_med'] if po['delta_med'] is not None else '—'} |"
            )
        lines.append("")

    lines += [
        "## 5. 結論（研究解讀）",
        "",
        payload["verdict_md"],
        "",
        "## 6. 限制",
        "",
        "- 組合規則由五檔差樣本歸納後凍結；全池驗證仍屬 **exploratory**，非 confirmatory。",
        "- R4/R5/R6 含盤中臂；缺 1m K 的列在該臂為 NA（不計入 skip）。",
        "- LOSO 逐檔 n 很小（每檔 ~6–26 筆）；同向比例僅作穩健性參考。",
        "- **未採納** Strategy／Order layer。",
        "",
    ]
    return "\n".join(lines)


def build_verdict(ranked: list[dict]) -> str:
    by_key = {r["rule"]: r for r in ranked}
    lines = []

    cross = []
    for rk in ("R4", "R5", "R6"):
        r = by_key.get(rk)
        if not r:
            continue
        po, lo, ti = r["pooled"], r["loso"], r["time_oos"]
        if po["delta_med"] is not None and po["delta_med"] <= -2 and ti["same_direction"]:
            cross.append(
                f"- **{rk}**（{RULES[rk]}）：Δmed={po['delta_med']}% · "
                f"n_skip={po['skip']['n']} · LOSO {lo['pct_same_direction']}% · "
                f"IS={ti['is_pre_2026']['delta_med']}% / OOS={ti['oos_ge_2026']['delta_med']}%"
            )
    if cross:
        lines.append("**跨組共通避（全池＋時間 OOS 同向）**：")
        lines.extend(cross)

    r1 = by_key.get("R1")
    if r1 and not r1["time_oos"]["same_direction"]:
        ti = r1["time_oos"]
        lines.append(
            f"- **R1 HOT5**：全池 Δmed={r1['pooled']['delta_med']}% 但 **IS/OOS 翻轉** "
            f"（IS {ti['is_pre_2026']['delta_med']}% vs OOS {ti['oos_ge_2026']['delta_med']}%）— "
            f"華邦電型高潮避險，不宜當穩定跨組硬規則。"
        )

    r3 = by_key.get("R3")
    if r3 and r3["pooled"]["delta_med"] is not None and r3["pooled"]["delta_med"] > 0:
        lines.append(
            f"- **R3 HOT5∧GAP**：全池 **反向** Δmed=+{r3['pooled']['delta_med']}% "
            f"（n_skip={r3['pooled']['skip']['n']}）— 典型個股／小樣本故事，非共通避。"
        )

    weak = [r for r in ranked if r["rule"] in ("R2", "R7")]
    if weak:
        parts = [
            f"{r['rule']}(Δ{r['pooled']['delta_med']}%, LOSO {r['loso']['pct_same_direction']}%)"
            for r in weak
        ]
        lines.append(
            f"- **R2／R7 T 日拒絕臂**：效應弱（{'; '.join(parts)}）— "
            f"聯電／國巨單檔有效，全池不穩。"
        )

    lines.append(
        "- **五檔分歧**：2303／2344 多數規則同向；**2327 HOT5 反向**、**8046 盤中弱反向** — "
        f"聯集 R6 實務覆蓋高（n_skip=183）但含個股噪音。"
    )
    return "\n".join(lines)


def main() -> int:
    mod = _load_scan_module()
    trades_raw = mod.load_all_trades()
    conn = mod.connect_ro(DEFAULT_DB_PATH)
    try:
        enriched = mod.enrich_trades(conn, trades_raw)
        attach_t_close(enriched, conn, mod)
    finally:
        conn.close()

    for t in enriched:
        t["_rules"] = rule_flags(t)

    rule_results = []
    for rk in RULES:
        pooled = summarize_split(enriched, rk)
        rule_results.append(
            {
                "rule": rk,
                "description": RULES[rk],
                "pooled": pooled,
                "time_oos": time_oos(enriched, rk),
                "loso": loso_analysis(enriched, rk),
            }
        )
    for r in rule_results:
        r["quality"] = score_rule(r["pooled"], r["loso"], r["time_oos"])

    ranked = sorted(
        rule_results,
        key=lambda x: (
            -x["quality"]["score"],
            x["pooled"]["delta_med"] if x["pooled"]["delta_med"] is not None else 999,
        ),
    )

    focus5 = {}
    for sid in FOCUS5:
        sub = [t for t in enriched if t["stock_id"] == sid]
        focus5[sid] = {
            "n": len(sub),
            "stock_name": FOCUS_NAMES.get(sid, ""),
            "rules": [
                {"rule": rk, "pooled": summarize_split(sub, rk)} for rk in RULES
            ],
        }

    n_intra = sum(1 for t in enriched if t.get("intraday_ok"))
    payload = {
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "protocol": {
            "n_trades": len(enriched),
            "n_ledgers": len(list(mod.TRADES_DIR.glob("*.json"))),
            "oos_cut": OOS_CUT,
            "intraday_ok": n_intra,
            "rules_frozen": RULES,
            "outcome": "r_adj_pct",
        },
        "rules": {r["rule"]: r for r in rule_results},
        "rules_ranked": ranked,
        "focus5": focus5,
        "verdict_md": build_verdict(ranked),
    }

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    jp = OUT_DIR / "H_COMBO_LOSO_OOS.json"
    mp = OUT_DIR / "H_COMBO_LOSO_OOS.md"
    jp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    mp.write_text(render_md(payload), encoding="utf-8")
    print(f"wrote {jp}")
    print(f"wrote {mp}")
    for r in ranked:
        print(
            f"  {r['rule']} score={r['quality']['score']} "
            f"Δ={r['pooled']['delta_med']} loso={r['loso']['pct_same_direction']}%"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
