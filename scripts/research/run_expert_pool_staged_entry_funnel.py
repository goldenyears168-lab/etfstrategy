#!/usr/bin/env python3
"""Staged entry funnel · open / 5m / 25m avoid rules (expert-pool knowledge).

Research only. Stages:
  S0 open  — gap vs T close (不建議追的開盤位置)
  S1 09:05 — first 5 minutes
  S2 09:25 — first 25 minutes (「再想想看」確認窗)

For each rule: pooled Δmed + permutation p; per focus sid; say 找不到 if n.s.

  PYTHONPATH=src .venv/bin/python scripts/research/run_expert_pool_staged_entry_funnel.py
"""
from __future__ import annotations

import json
import math
import random
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402

TRADES = (
    ROOT
    / "reports/research/branch-footprint-screen/expert_pool/knowledge/trades"
)
OUT = (
    ROOT
    / "reports/research/branch-footprint-screen/expert_pool/hypotheses"
    / "avoid_morphology"
)
FOCUS = ("2303", "2327", "6515", "2344", "8046")
NAMES = {
    "2303": "聯電",
    "2327": "國巨",
    "6515": "穎崴",
    "2344": "華邦電",
    "8046": "南電",
}
SOURCE = "finmind"
N_PERM = 2500
ALPHA = 0.10
RNG = random.Random(42)


def median(xs: list[float]) -> float | None:
    return float(statistics.median(xs)) if xs else None


def load_trades() -> list[dict]:
    rows: list[dict] = []
    for p in sorted(TRADES.glob("*.json")):
        data = json.loads(p.read_text(encoding="utf-8"))
        trades = data if isinstance(data, list) else data.get("trades") or []
        for t in trades:
            rows.append(
                {
                    "sid": p.stem,
                    "signal_date": str(t["signal_date"])[:10],
                    "entry_date": str(t["entry_date"])[:10],
                    "r_adj": float(t["r_adj_pct"]),
                }
            )
    return rows


def t_close(conn, sid: str, d: str) -> float | None:
    r = conn.execute(
        "SELECT close FROM stock_daily_bars WHERE stock_id=? AND trade_date=? AND source=?",
        (sid, d, SOURCE),
    ).fetchone()
    return float(r[0]) if r else None


def morning_bars(conn, sid: str, d: str, end: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT minute, open, high, low, close, source
        FROM stock_kbar_1m
        WHERE stock_id=? AND trade_date=? AND minute>='09:00:00' AND minute<=?
        ORDER BY minute
        """,
        (sid, d, end),
    ).fetchall()
    if not rows:
        return []
    by: dict[str, list] = {}
    for r in rows:
        by.setdefault(str(r[5]), []).append(
            {
                "minute": str(r[0]),
                "open": float(r[1]),
                "high": float(r[2]),
                "low": float(r[3]),
                "close": float(r[4]),
            }
        )
    if "finmind" in by and len(by["finmind"]) >= 3:
        return by["finmind"]
    # densest source
    return max(by.values(), key=len)


def agg(bars: list[dict]) -> dict | None:
    if not bars:
        return None
    o = bars[0]["open"]
    if o <= 0:
        return None
    h = max(b["high"] for b in bars)
    l = min(b["low"] for b in bars)
    c = bars[-1]["close"]
    return {
        "open": o,
        "high": h,
        "low": l,
        "close": c,
        "ret": c / o - 1,
        "dd": l / o - 1,
        "up": h / o - 1,
        "n": len(bars),
    }


def enrich(conn, rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        tc = t_close(conn, r["sid"], r["signal_date"])
        b5 = morning_bars(conn, r["sid"], r["entry_date"], "09:05:00")
        b25 = morning_bars(conn, r["sid"], r["entry_date"], "09:25:00")
        a5 = agg(b5)
        a25 = agg(b25)
        gap = None
        if tc and a5 and a5["open"] > 0:
            gap = a5["open"] / tc - 1
        elif tc and a25 and a25["open"] > 0:
            gap = a25["open"] / tc - 1
        fail25 = False
        if a25:
            fail25 = a25["high"] > a25["open"] * 1.01 and a25["close"] < a25["open"]
        weak_vs_t25 = False
        if a25 and tc:
            weak_vs_t25 = a25["close"] < tc and a25["ret"] < 0
        out.append(
            {
                **r,
                "t_close": tc,
                "gap": gap,
                "a5": a5,
                "a25": a25,
                "fail25": fail25,
                "weak_vs_t25": weak_vs_t25,
                "has5": a5 is not None and a5["n"] >= 3,
                "has25": a25 is not None and a25["n"] >= 10,
            }
        )
    return out


def eval_rule(
    rows: list[dict], pred
) -> dict:
    hit = [r["r_adj"] for r in rows if pred(r) is True]
    miss = [r["r_adj"] for r in rows if pred(r) is False]
    na = sum(1 for r in rows if pred(r) is None)
    if not hit or not miss:
        return {
            "n_hit": len(hit),
            "n_miss": len(miss),
            "n_na": na,
            "med_hit": median(hit),
            "med_miss": median(miss),
            "delta": None,
            "perm_p": None,
            "label": "找不到" if not hit else "樣本不足",
        }
    d = median(hit) - median(miss)  # type: ignore
    # perm: H1 hit med lower
    all_r = hit + miss
    n_h = len(hit)
    ge = 0
    for _ in range(N_PERM):
        RNG.shuffle(all_r)
        mh = statistics.median(all_r[:n_h])
        mm = statistics.median(all_r[n_h:])
        if (mh - mm) <= d + 1e-12:
            ge += 1
    p = (ge + 1) / (N_PERM + 1)
    label = "顯著" if p < ALPHA and d < 0 else (
        "方向對未顯著" if d < 0 else ("反向" if d > 0 else "無差異")
    )
    if p >= ALPHA and d < 0:
        label = "方向對未顯著"
    if len(hit) < 8 and label == "顯著":
        label = "顯著但n薄"
    return {
        "n_hit": len(hit),
        "n_miss": len(miss),
        "n_na": na,
        "med_hit": median(hit),
        "med_miss": median(miss),
        "delta": d,
        "perm_p": p,
        "label": label if len(hit) >= 5 else "找不到（n_hit太小）",
    }


def rules() -> list[tuple[str, str, callable]]:
    def g(th):
        def _p(r):
            if r["gap"] is None:
                return None
            return r["gap"] >= th

        return _p

    def open5_weak(th_ret=-0.01, th_dd=-0.02):
        def _p(r):
            if not r["has5"]:
                return None
            a = r["a5"]
            return a["ret"] < th_ret and a["dd"] < th_dd

        return _p

    def ret25(th):
        def _p(r):
            if not r["has25"]:
                return None
            return r["a25"]["ret"] <= th

        return _p

    def perfect5():
        """很吻合：小跳空或低開 + 5分不弱."""

        def _p(r):
            if r["gap"] is None or not r["has5"]:
                return None
            # "perfect" = NOT avoid → we score avoid as inverse for table;
            # instead define AVOID_NOT_PERFECT
            gap_ok = r["gap"] <= 0.03
            a = r["a5"]
            strong = a["ret"] >= -0.005 and a["dd"] > -0.02
            return not (gap_ok and strong)  # True = problematic / not perfect

        return _p

    return [
        ("S0", "GAP>=2%", g(0.02)),
        ("S0", "GAP>=3%", g(0.03)),
        ("S0", "GAP>=5%", g(0.05)),
        ("S1", "OPEN5_WEAK", open5_weak()),
        ("S1", "OPEN5_RET<=-1%", lambda r: None if not r["has5"] else r["a5"]["ret"] <= -0.01),
        ("S1", "GAP>=3%&OPEN5_WEAK", lambda r: None if r["gap"] is None or not r["has5"] else (r["gap"] >= 0.03 and r["a5"]["ret"] < -0.01 and r["a5"]["dd"] < -0.02)),
        ("S1", "NOT_PERFECT_5M", perfect5()),
        ("S2", "RET25<=0", ret25(0.0)),
        ("S2", "RET25<=-1%", ret25(-0.01)),
        ("S2", "RET25<=-2%", ret25(-0.02)),
        ("S2", "FAIL_BREAK25", lambda r: None if not r["has25"] else r["fail25"]),
        ("S2", "WEAK_VS_T25", lambda r: None if not r["has25"] or r["t_close"] is None else r["weak_vs_t25"]),
        ("S2", "OPEN5_WEAK&RET25<=0", lambda r: None if not r["has5"] or not r["has25"] else (r["a5"]["ret"] < -0.01 and r["a5"]["dd"] < -0.02 and r["a25"]["ret"] <= 0)),
    ]


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    conn = connect(DEFAULT_DB_PATH)
    raw = load_trades()
    rows = enrich(conn, raw)
    conn.close()
    cov5 = sum(1 for r in rows if r["has5"])
    cov25 = sum(1 for r in rows if r["has25"])
    pooled = []
    per_stock = {sid: [] for sid in FOCUS}
    for stage, name, pred in rules():
        res = eval_rule(rows, pred)
        pooled.append({"stage": stage, "rule": name, **res})
        for sid in FOCUS:
            sub = [r for r in rows if r["sid"] == sid]
            per_stock[sid].append({"stage": stage, "rule": name, **eval_rule(sub, pred)})

    payload = {
        "n": len(rows),
        "cov5": cov5,
        "cov25": cov25,
        "alpha": ALPHA,
        "n_perm": N_PERM,
        "pooled": pooled,
        "per_stock": per_stock,
        "names": NAMES,
    }
    (OUT / "H_STAGED_ENTRY_FUNNEL.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    def fmt(row: dict) -> str:
        d = row["delta"]
        ds = f"{d:+.2f}" if d is not None else "—"
        p = row["perm_p"]
        ps = f"{p:.3f}" if p is not None else "—"
        mh = f"{row['med_hit']:.2f}" if row["med_hit"] is not None else "—"
        mm = f"{row['med_miss']:.2f}" if row["med_miss"] is not None else "—"
        return (
            f"| {row['stage']} | {row['rule']} | {row['n_hit']} | {mh} | {mm} | "
            f"{ds} | {ps} | {row['label']} |"
        )

    md = [
        "# 專家池 · 分階段進場漏斗（開盤／5分／25分）",
        "",
        f"- n={len(rows)} · 5分覆蓋 {cov5}/{len(rows)} · 25分覆蓋 {cov25}/{len(rows)}",
        f"- Δmed = med_hit − med_miss（命中=應避；越負越好）· perm 單尾 · α={ALPHA}",
        "- Research only · 未採納 Order",
        "",
        "## 全池",
        "",
        "| stage | rule | n_hit | med_hit | med_miss | Δmed | p | 判定 |",
        "|-------|------|------:|--------:|---------:|-----:|--:|------|",
    ]
    for row in pooled:
        md.append(fmt(row))
    md += ["", "## 重點五檔", ""]
    for sid in FOCUS:
        md += [f"### {sid} {NAMES[sid]}", "", "| stage | rule | n_hit | med_hit | med_miss | Δmed | p | 判定 |", "|-------|------|------:|--------:|---------:|-----:|--:|------|"]
        for row in per_stock[sid]:
            md.append(fmt(row))
        md.append("")
    # funnel narrative
    good_pool = [r for r in pooled if r["label"] in ("顯著", "顯著但n薄") and (r["delta"] or 0) < 0]
    md += [
        "## 漏斗解讀（研究）",
        "",
        "1. **開盤（S0）**：若 GAP 規則顯著 → 高開超過該閾值則「不建議市價追」改限價。",
        "2. **09:05（S1）**：若很吻合（非 NOT_PERFECT）→ 可考慮 5 分後跟；若 OPEN5_WEAK → 暫緩看 25 分。",
        "3. **09:25（S2）**：RET25／FAIL_BREAK／WEAK_VS_T 若顯著 → 仍弱則 skip 本次。",
        "",
        "### 全池站得住的規則",
        "",
    ]
    if not good_pool:
        md.append("- （無）全池在 α=0.10 下找不到穩定應避規則")
    else:
        for r in sorted(good_pool, key=lambda x: x["delta"] or 0):
            md.append(
                f"- **{r['stage']} {r['rule']}** · Δ={r['delta']:+.2f}pp · "
                f"p={r['perm_p']:.3f} · n_hit={r['n_hit']} · {r['label']}"
            )
    md.append("")
    (OUT / "H_STAGED_ENTRY_FUNNEL.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print(f"wrote {OUT / 'H_STAGED_ENTRY_FUNNEL.md'}")
    print(f"cov5={cov5} cov25={cov25}")
    for r in good_pool:
        print("OK", r["stage"], r["rule"], r["delta"], r["perm_p"], r["label"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
