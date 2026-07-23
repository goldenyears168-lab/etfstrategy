#!/usr/bin/env python3
"""Staged funnel policy optimizer · rebuild entry @ open/05/25 · risk/return.

Research only. Baseline = naive L1 open (ledger r_adj).
Policies decide: SKIP | ENTER_OPEN | ENTER_05 | ENTER_25.

  PYTHONPATH=src .venv/bin/python scripts/research/run_expert_pool_staged_funnel_optimize.py
"""
from __future__ import annotations

import json
import math
import statistics
import sys
from dataclasses import asdict, dataclass
from itertools import product
from pathlib import Path
from typing import Callable, Iterable

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
COST = 0.003
BETA = 1.15
SOURCE = "finmind"
FOCUS = ("2303", "2327", "6515", "2344", "8046")


def median(xs: list[float]) -> float | None:
    return float(statistics.median(xs)) if xs else None


def mean(xs: list[float]) -> float | None:
    return float(statistics.mean(xs)) if xs else None


def pctl(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    ys = sorted(xs)
    i = min(len(ys) - 1, max(0, int(math.floor(q * (len(ys) - 1)))))
    return float(ys[i])


def load_trades() -> list[dict]:
    rows = []
    for p in sorted(TRADES.glob("*.json")):
        data = json.loads(p.read_text(encoding="utf-8"))
        trades = data if isinstance(data, list) else data.get("trades") or []
        for t in trades:
            rows.append(
                {
                    "sid": p.stem,
                    "signal_date": str(t["signal_date"])[:10],
                    "entry_date": str(t["entry_date"])[:10],
                    "exit_date": str(t["exit_date"])[:10],
                    "entry_open": float(t["entry_open"]),
                    "exit_close": float(t["exit_close"]),
                    "r_pct": float(t["r_pct"]),
                    "r_ix_pct": float(t["r_ix_pct"]),
                    "r_adj": float(t["r_adj_pct"]),
                    "branches": t.get("branches") or [],
                }
            )
    return rows


def morning(conn, sid: str, d: str, end: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT minute, open, high, low, close, source FROM stock_kbar_1m
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
    return max(by.values(), key=len) if by else []


def agg(bars: list[dict]) -> dict | None:
    if len(bars) < 2:
        return None
    o = bars[0]["open"]
    if o <= 0:
        return None
    return {
        "open": o,
        "high": max(b["high"] for b in bars),
        "low": min(b["low"] for b in bars),
        "close": bars[-1]["close"],
        "ret": bars[-1]["close"] / o - 1,
        "dd": min(b["low"] for b in bars) / o - 1,
        "n": len(bars),
    }


def t_day_feats(conn, sid: str, sig: str) -> dict:
    rows = conn.execute(
        """
        SELECT trade_date, open, high, low, close FROM stock_daily_bars
        WHERE stock_id=? AND source=? AND trade_date<=?
        ORDER BY trade_date DESC LIMIT 8
        """,
        (sid, SOURCE, sig),
    ).fetchall()
    bars = [
        {
            "d": str(r[0]),
            "o": float(r[1]),
            "h": float(r[2]),
            "l": float(r[3]),
            "c": float(r[4]),
        }
        for r in reversed(rows)
    ]
    out = {
        "hot5": False,
        "t_reject": False,
        "max_today_yi": 0.0,
        "ok": False,
    }
    if not bars or bars[-1]["d"] != sig:
        return out
    t = bars[-1]
    prev5 = bars[-6:-1] if len(bars) >= 6 else []
    rng = t["h"] - t["l"]
    close_pos = (t["c"] - t["l"]) / rng if rng > 0 else 0.5
    body = t["c"] / t["o"] - 1 if t["o"] else 0.0
    black = t["c"] < t["o"]
    ret5 = None
    if len(prev5) == 5 and prev5[0]["c"] > 0:
        ret5 = prev5[-1]["c"] / prev5[0]["c"] - 1
    out["hot5"] = bool(ret5 is not None and ret5 >= 0.20)
    out["t_reject"] = (black and close_pos < 0.33) or body < -0.03
    out["ok"] = True
    return out


def classify_s1(row: dict, p: "FunnelParams") -> str:
    """S1 @09:05: perfect | chase_weak | ambiguous."""
    gap = row.get("gap")
    a5 = row.get("a5")
    if gap is None or not a5:
        return "ambiguous"
    if gap <= p.g_star and a5["ret"] >= p.r5_star and a5["dd"] > p.d5_star:
        return "perfect"
    if gap >= p.gc_star and a5["ret"] < p.rw_star and a5["dd"] < p.dw_star:
        return "chase_weak"
    return "ambiguous"


def stable_s2(row: dict, p: "FunnelParams") -> bool:
    """S2 @09:25 stable if ret25>s* and not fail_break."""
    a25 = row.get("a25")
    if not a25:
        return False
    return a25["ret"] > p.s_star and not row.get("fail25", False)


@dataclass
class Decision:
    action: str  # SKIP | OPEN | E05 | E25
    reason: str


@dataclass(frozen=True)
class FunnelParams:
    g_star: float  # perfect: gap <= g*
    gc_star: float  # chase_weak: gap >= gc*
    gap5_hard: float  # S0 hard gap gate
    s_star: float  # S2 skip if ret25 <= s*
    r5_star: float = -0.005  # perfect: ret5 >= r5*
    d5_star: float = -0.02  # perfect: dd5 > d5*
    rw_star: float = -0.01  # chase_weak: ret5 < rw*
    dw_star: float = -0.02  # chase_weak: dd5 < dw*
    soft_gap: float = 0.02  # S0 soft zone lower bound (fixed)


DEFAULT_FUNNEL = FunnelParams(
    g_star=0.03,
    gc_star=0.03,
    gap5_hard=0.05,
    s_star=0.0,
)


def enrich(conn, rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        b5 = morning(conn, r["sid"], r["entry_date"], "09:05:00")
        b25 = morning(conn, r["sid"], r["entry_date"], "09:25:00")
        a5 = agg(b5)
        a25 = agg(b25)
        open_px = a5["open"] if a5 else (a25["open"] if a25 else r["entry_open"])
        px05 = a5["close"] if a5 else None
        px25 = a25["close"] if a25 else None
        t_close = conn.execute(
            "SELECT close FROM stock_daily_bars WHERE stock_id=? AND trade_date=? AND source=?",
            (r["sid"], r["signal_date"], SOURCE),
        ).fetchone()
        tc = float(t_close[0]) if t_close else None
        gap = (open_px / tc - 1) if tc and open_px else None
        td = t_day_feats(conn, r["sid"], r["signal_date"])
        # max today branch yi from ledger snapshot
        max_yi = 0.0
        for b in r["branches"]:
            if "今" in str(b.get("role") or ""):
                max_yi = max(max_yi, float(b.get("amt_yi") or 0.0))
        td["max_today_yi"] = max_yi
        fail25 = False
        if a25:
            fail25 = a25["high"] > a25["open"] * 1.01 and a25["close"] < a25["open"]
        row_base = {
            **r,
            "gap": gap,
            "open_px": open_px,
            "px05": px05,
            "px25": px25,
            "a5": a5,
            "a25": a25,
            "fail25": fail25,
            "td": td,
            "has_intra": a5 is not None and a25 is not None,
        }
        s1 = classify_s1(row_base, DEFAULT_FUNNEL)
        out.append(
            {
                **row_base,
                "perfect": s1 == "perfect",
                "chase_weak": s1 == "chase_weak",
            }
        )
    return out


def rebuild_radj(row: dict, entry_px: float) -> float | None:
    if entry_px is None or entry_px <= 0:
        return None
    r_s = row["exit_close"] / entry_px - 1 - COST
    r_ix = row["r_ix_pct"] / 100.0
    return (r_s - BETA * r_ix) * 100.0


PolicyFn = Callable[[dict], Decision]


def metrics(rows: list[dict], decisions: list[Decision]) -> dict:
    kept_adj = []
    skipped_base = []
    naive = [r["r_adj"] for r in rows]
    actions = {"SKIP": 0, "OPEN": 0, "E05": 0, "E25": 0}
    for r, d in zip(rows, decisions):
        actions[d.action] = actions.get(d.action, 0) + 1
        if d.action == "SKIP":
            skipped_base.append(r["r_adj"])
            continue
        px = {
            "OPEN": r["open_px"],
            "E05": r["px05"] or r["open_px"],
            "E25": r["px25"] or r["px05"] or r["open_px"],
        }[d.action]
        radj = rebuild_radj(r, px)
        if radj is not None:
            kept_adj.append(radj)
    return {
        "n": len(rows),
        "n_kept": len(kept_adj),
        "n_skip": actions.get("SKIP", 0),
        "skip_rate": actions.get("SKIP", 0) / len(rows) if rows else 0,
        "actions": actions,
        "naive_med": median(naive),
        "naive_mean": mean(naive),
        "naive_win": sum(1 for x in naive if x > 0) / len(naive) if naive else None,
        "naive_p10": pctl(naive, 0.10),
        "kept_med": median(kept_adj),
        "kept_mean": mean(kept_adj),
        "kept_win": sum(1 for x in kept_adj if x > 0) / len(kept_adj) if kept_adj else None,
        "kept_p10": pctl(kept_adj, 0.10),
        "skip_med_naive": median(skipped_base),
        "delta_med_vs_naive": (
            (median(kept_adj) - median(naive)) if kept_adj and naive else None
        ),
        "delta_p10_vs_naive": (
            (pctl(kept_adj, 0.10) - pctl(naive, 0.10))
            if kept_adj and naive
            else None
        ),
    }


# --- policies ---


def pol_naive(r: dict) -> Decision:
    return Decision("OPEN", "naive_L1")


def pol_parametric_tree(r: dict, p: FunnelParams) -> Decision:
    """Frozen S0→S1→S2 tree with tunable thresholds."""
    if not r["has_intra"] or r["gap"] is None:
        return Decision("OPEN", "no_intra_fallback_open")
    s1 = classify_s1(r, p)
    ok25 = stable_s2(r, p)
    g = r["gap"]
    if g >= p.gap5_hard:
        if s1 == "perfect":
            return Decision("E05", "gap5_perfect05")
        if ok25:
            return Decision("E25", "gap5_stable25")
        return Decision("SKIP", "gap5_skip")
    if g >= p.soft_gap:
        if s1 == "perfect":
            return Decision("E05", "soft_perfect05")
        if s1 == "chase_weak":
            if ok25:
                return Decision("E25", "soft_cw_stable25")
            return Decision("SKIP", "soft_cw_skip")
        if ok25:
            return Decision("E25", "soft_amb_stable25")
        return Decision("SKIP", "soft_amb_skip")
    if s1 == "perfect":
        return Decision("E05", "lowgap_perfect05")
    if s1 == "chase_weak":
        if ok25:
            return Decision("E25", "lowgap_cw_stable25")
        return Decision("SKIP", "lowgap_cw_skip")
    if ok25:
        return Decision("E25", "lowgap_amb_stable25")
    return Decision("SKIP", "lowgap_amb_skip")


def pol_user_tree(r: dict) -> Decision:
    """User's tree: gap gates → perfect@05 → else wait25 → skip if weak."""
    return pol_parametric_tree(r, DEFAULT_FUNNEL)


def pol_user_strict25(r: dict) -> Decision:
    """Like user tree but S2 skip if ret25<=-2% only (looser keep)."""
    d = pol_user_tree(r)
    if d.action != "SKIP":
        return d
    # re-evaluate: if skipped only for ret25<=0, allow if ret25>-2%
    if not r["has_intra"] or r["gap"] is None:
        return d
    if r["a25"] and r["a25"]["ret"] > -0.02 and not r["fail25"]:
        if r["perfect"]:
            return Decision("E05", "strict25_relax_to05")
        return Decision("E25", "strict25_relax_keep")
    return d


def pol_aggressive05(r: dict) -> Decision:
    """Enter@05 unless chase_weak or gap>=5%; those go to 25/skip."""
    if not r["has_intra"] or r["gap"] is None:
        return Decision("OPEN", "fallback")
    if r["gap"] >= 0.05 or r["chase_weak"]:
        if r["a25"] and r["a25"]["ret"] > 0 and not r["fail25"]:
            return Decision("E25", "risk_wait25")
        return Decision("SKIP", "risk_skip")
    return Decision("E05", "aggr05")


def pol_tday_overlay(r: dict) -> Decision:
    """User tree + T-day risk forces wait25 or skip."""
    base = pol_user_tree(r)
    td = r["td"]
    risk = td.get("hot5") or td.get("t_reject") or (td.get("max_today_yi") or 0) >= 5
    if not risk:
        return base
    if base.action == "E05":
        # downgrade
        if r["a25"] and r["a25"]["ret"] > 0 and not r["fail25"]:
            return Decision("E25", "tday_risk_downgrade25")
        return Decision("SKIP", "tday_risk_skip")
    if base.action == "OPEN":
        return Decision("E25", "tday_risk_no_open") if r.get("px25") else Decision("SKIP", "tday_risk")
    return base


def pol_ret25_m2_only(r: dict) -> Decision:
    """Minimal: open unless ret25<=-2% or fail25 → skip (enter open)."""
    if not r["has_intra"]:
        return Decision("OPEN", "fallback")
    if (r["a25"] and r["a25"]["ret"] <= -0.02) or r["fail25"]:
        return Decision("SKIP", "ret25_m2_or_fail")
    return Decision("OPEN", "ok_open")


def pol_gap5_skip(r: dict) -> Decision:
    if r["gap"] is not None and r["gap"] >= 0.05:
        return Decision("SKIP", "gap5_skip")
    return Decision("OPEN", "ok")


POLICIES: dict[str, PolicyFn] = {
    "P0_naive_L1": pol_naive,
    "P1_user_tree": pol_user_tree,
    "P2_user_strict25_m2": pol_user_strict25,
    "P3_aggressive05": pol_aggressive05,
    "P4_tday_overlay": pol_tday_overlay,
    "P5_ret25_m2_only": pol_ret25_m2_only,
    "P6_gap5_skip_only": pol_gap5_skip,
}


def eval_all(rows: list[dict]) -> dict:
    # eligible with intraday
    intra = [r for r in rows if r["has_intra"] and r["gap"] is not None]
    results = {"n_all": len(rows), "n_intra": len(intra), "pooled": {}, "focus": {}}
    for name, fn in POLICIES.items():
        dec = [fn(r) for r in intra]
        results["pooled"][name] = metrics(intra, dec)
        focus_m = {}
        for sid in FOCUS:
            sub = [r for r in intra if r["sid"] == sid]
            if not sub:
                continue
            focus_m[sid] = metrics(sub, [fn(r) for r in sub])
        results["focus"][name] = focus_m
    return results


def eval_policy(rows: list[dict], p: FunnelParams) -> dict:
    fn = lambda r: pol_parametric_tree(r, p)
    dec = [fn(r) for r in rows]
    m = metrics(rows, dec)
    m["params"] = asdict(p)
    return m


def pareto_front(candidates: list[dict]) -> list[dict]:
    """Non-dominated on (delta_med, delta_p10); higher is better."""
    valid = [
        c
        for c in candidates
        if c.get("delta_med_vs_naive") is not None
        and c.get("delta_p10_vs_naive") is not None
    ]
    front: list[dict] = []
    for c in valid:
        dm = c["delta_med_vs_naive"]
        dp = c["delta_p10_vs_naive"]
        dominated = False
        for o in valid:
            if o is c:
                continue
            om = o["delta_med_vs_naive"]
            op = o["delta_p10_vs_naive"]
            if om >= dm and op >= dp and (om > dm or op > dp):
                dominated = True
                break
        if not dominated:
            front.append(c)
    front.sort(
        key=lambda x: (
            -(x["delta_med_vs_naive"] or 0),
            -(x["delta_p10_vs_naive"] or 0),
        )
    )
    return front


def practical_score(m: dict) -> float:
    """Balance Δmed, Δp10, moderate skip — prefer Pareto-like tradeoffs."""
    dm = m.get("delta_med_vs_naive") or 0.0
    dp = m.get("delta_p10_vs_naive") or 0.0
    sr = m.get("skip_rate") or 0.0
    skip_pen = max(0.0, sr - 0.30) * 6.0 + max(0.0, 0.12 - sr) * 2.0
    med_pen = max(0.0, -dm - 0.25) * 4.0
    return dm * 1.0 + dp * 0.55 - skip_pen - med_pen


def pick_practical(scanned: list[dict], front: list[dict]) -> dict | None:
    """Pick one balanced point from Pareto front (fallback: full scan)."""
    pool = front or scanned
    if not pool:
        return None
    return max(pool, key=practical_score)


def grid_combos() -> Iterable[FunnelParams]:
    g_stars = (0.02, 0.03, 0.04)
    gc_stars = (0.02, 0.03, 0.05)
    gap5_hards = (0.04, 0.05, 0.06)
    s_stars = (0.0, -0.01, -0.02)
    perfect_presets = (
        (-0.005, -0.02),
        (0.0, -0.015),
        (-0.01, -0.025),
    )
    for g, gc, gh, s, (r5, d5) in product(
        g_stars, gc_stars, gap5_hards, s_stars, perfect_presets
    ):
        yield FunnelParams(
            g_star=g,
            gc_star=gc,
            gap5_hard=gh,
            s_star=s,
            r5_star=r5,
            d5_star=d5,
        )


def run_pareto_thresholds(rows: list[dict]) -> dict:
    intra = [r for r in rows if r["has_intra"] and r["gap"] is not None]
    naive_dec = [pol_naive(r) for r in intra]
    naive_m = metrics(intra, naive_dec)

    scanned: list[dict] = []
    for p in grid_combos():
        m = eval_policy(intra, p)
        if m["skip_rate"] > 0.60:
            continue
        scanned.append(m)

    front = pareto_front(scanned)
    top10 = front[:10]
    practical = pick_practical(scanned, front)

    oos_cut = "2026-01-01"
    oos_rows = [r for r in intra if r["signal_date"] >= oos_cut]
    oos_naive = metrics(oos_rows, [pol_naive(r) for r in oos_rows])
    oos_eval: dict = {"n": len(oos_rows), "naive": oos_naive, "top10": [], "practical": None}
    for item in top10:
        p = FunnelParams(**{k: item["params"][k] for k in asdict(DEFAULT_FUNNEL)})
        om = eval_policy(oos_rows, p)
        oos_eval["top10"].append(
            {
                "params": item["params"],
                "delta_med_vs_naive": om["delta_med_vs_naive"],
                "delta_p10_vs_naive": om["delta_p10_vs_naive"],
                "skip_rate": om["skip_rate"],
                "kept_med": om["kept_med"],
                "kept_p10": om["kept_p10"],
            }
        )
    if practical:
        pp = FunnelParams(**{k: practical["params"][k] for k in asdict(DEFAULT_FUNNEL)})
        oos_eval["practical"] = eval_policy(oos_rows, pp)

    return {
        "n_all": len(rows),
        "n_intra": len(intra),
        "grid": {
            "g_star": [0.02, 0.03, 0.04],
            "gc_star": [0.02, 0.03, 0.05],
            "gap5_hard": [0.04, 0.05, 0.06],
            "s_star": [0.0, -0.01, -0.02],
            "perfect_presets": [
                {"r5_star": -0.005, "d5_star": -0.02},
                {"r5_star": 0.0, "d5_star": -0.015},
                {"r5_star": -0.01, "d5_star": -0.025},
            ],
            "n_combos": 3 * 3 * 3 * 3 * 3,
            "n_scanned_ok": len(scanned),
            "skip_cap": 0.60,
        },
        "baseline_naive_L1": naive_m,
        "default_user_tree": eval_policy(intra, DEFAULT_FUNNEL),
        "pareto_top10": top10,
        "practical_pick": practical,
        "oos_from": oos_cut,
        "oos": oos_eval,
    }


def fmt_params(p: dict) -> str:
    return (
        f"g*≤{p['g_star']:.0%} gc*≥{p['gc_star']:.0%} gap5≥{p['gap5_hard']:.0%} "
        f"s*={p['s_star']:+.0%} r5*≥{p['r5_star']:+.1%} d5*>{p['d5_star']:+.1%}"
    )


def user_chant(p: dict) -> str:
    """One-line mnemonic vs default user thresholds."""
    lines = [
        "【S0】硬缺口≥{gh:.0%}等25；2–{gh:.0%}不開盤追；<{sg:.0%}可05".format(
            gh=p["gap5_hard"], sg=p.get("soft_gap", 0.02)
        ),
        "【S1·perfect】缺口≤{g:.0%} 且 05 ret≥{r5:+.1%} 且 dd>{d5:+.1%}".format(
            g=p["g_star"], r5=p["r5_star"], d5=p["d5_star"]
        ),
        "【S1·追弱】缺口≥{gc:.0%} 且 05 ret<−1% 且 dd<−2%".format(gc=p["gc_star"]),
        "【S2】25分 ret>{s:+.0%} 且非 fail_break 才留；否則 skip".format(s=p["s_star"]),
    ]
    return " · ".join(lines)


def write_pareto_report(res: dict) -> None:
    naive = res["baseline_naive_L1"]
    practical = res.get("practical_pick")
    lines = [
        "# 漏斗閾值 Pareto 優化（H_FUNNEL_PARETO_THRESHOLDS）",
        "",
        f"- 樣本：全池 {res['n_all']} · 有完整 5+25 分K **{res['n_intra']}**",
        f"- 網格：{res['grid']['n_combos']} 組 · 通過 skip≤60%：**{res['grid']['n_scanned_ok']}**",
        "- 基準：**naive L1 開盤** · cost 30bps · β=1.15",
        "- **Research only** · 未採納",
        "",
        "## 凍結骨架",
        "",
        "S0 gap → S1 09:05 perfect/chase_weak/ambiguous → S2 09:25 stable/skip",
        "",
        "- perfect ≈ gap≤g* ∧ ret5≥r5* ∧ dd5>d5*",
        "- chase_weak ≈ gap≥gc* ∧ ret5<rw* ∧ dd5<dw*（rw*/dw* 固定 -1%/-2%）",
        "- S2 skip if ret25≤s* OR fail_break",
        "",
        f"Naive L1：med={naive['naive_med']:.2f} · p10={naive['naive_p10']:.2f} · "
        f"win={naive['naive_win']*100:.1f}%",
        "",
        "## Pareto 前 10（全池 · vs naive L1）",
        "",
        "| # | 閾值 | skip% | kept_med | Δmed | kept_p10 | Δp10 | skip組med |",
        "|--:|------|------:|---------:|-----:|---------:|-----:|----------:|",
    ]
    for i, m in enumerate(res["pareto_top10"], 1):
        lines.append(
            "| {i} | {params} | {sr:.1%} | {km:.2f} | {dm:+.2f} | {kp:.2f} | {dp:+.2f} | {sm} |".format(
                i=i,
                params=fmt_params(m["params"]),
                sr=m["skip_rate"],
                km=m["kept_med"] or 0,
                dm=m["delta_med_vs_naive"] or 0,
                kp=m["kept_p10"] or 0,
                dp=m["delta_p10_vs_naive"] or 0,
                sm=(
                    f"{m['skip_med_naive']:.2f}"
                    if m["skip_med_naive"] is not None
                    else "—"
                ),
            )
        )

    if practical:
        lines += [
            "",
            "## 實務推薦（平衡 Δmed / Δp10 / skip%）",
            "",
            f"- 閾值：**{fmt_params(practical['params'])}**",
            f"- skip **{practical['skip_rate']:.1%}** · kept_med **{practical['kept_med']:.2f}** "
            f"(Δmed {practical['delta_med_vs_naive']:+.2f}) · kept_p10 **{practical['kept_p10']:.2f}** "
            f"(Δp10 {practical['delta_p10_vs_naive']:+.2f})",
            f"- 口訣（實務版）：{user_chant(practical['params'])}",
        ]

    lines += [
        "",
        f"## OOS · signal_date ≥ {res['oos_from']}",
        "",
        f"- n={res['oos']['n']}",
        "",
        "| 來源 | Δmed OOS | Δp10 OOS | skip% OOS | 同向？ |",
        "|------|---------:|---------:|----------:|:------|",
    ]
    for i, item in enumerate(res["oos"]["top10"], 1):
        dm = item["delta_med_vs_naive"]
        dp = item["delta_p10_vs_naive"]
        same = (dm or 0) > 0 and (dp or 0) > 0
        lines.append(
            f"| Pareto#{i} | {dm:+.2f} | {dp:+.2f} | {item['skip_rate']:.1%} | "
            f"{'✓' if same else '△'} |"
        )
    if res["oos"].get("practical"):
        op = res["oos"]["practical"]
        dm = op["delta_med_vs_naive"]
        dp = op["delta_p10_vs_naive"]
        same = (dm or 0) > 0 and (dp or 0) > 0
        lines.append(
            f"| 實務推薦 | {dm:+.2f} | {dp:+.2f} | {op['skip_rate']:.1%} | "
            f"{'✓' if same else '△'} |"
        )
    lines.append("")
    (OUT / "H_FUNNEL_PARETO_THRESHOLDS.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )


def main_pareto() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    conn = connect(DEFAULT_DB_PATH)
    rows = enrich(conn, load_trades())
    conn.close()
    res = run_pareto_thresholds(rows)
    (OUT / "H_FUNNEL_PARETO_THRESHOLDS.json").write_text(
        json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    write_pareto_report(res)
    print(f"wrote {OUT / 'H_FUNNEL_PARETO_THRESHOLDS.md'}")
    if res.get("practical_pick"):
        p = res["practical_pick"]
        print(
            "practical",
            fmt_params(p["params"]),
            "dMed",
            p["delta_med_vs_naive"],
            "dP10",
            p["delta_p10_vs_naive"],
            "skip",
            f"{p['skip_rate']:.1%}",
        )
        print("chant:", user_chant(p["params"]))
    return 0


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    conn = connect(DEFAULT_DB_PATH)
    rows = enrich(conn, load_trades())
    conn.close()
    res = eval_all(rows)
    (OUT / "H_FUNNEL_POLICY_OPT.json").write_text(
        json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    lines = [
        "# 專家池分階段漏斗 · 政策優化（風險↓／報酬↑）",
        "",
        f"- 樣本：全池 {res['n_all']} · 有完整 5+25 分K {res['n_intra']}",
        "- 報酬重算：進場價=OPEN/09:05/09:25 收 · 出場仍 ledger exit_close · cost 30bps · "
        "r_ix 沿用 ledger（延遲進場近似）",
        "- Research only",
        "",
        "## 全池政策比較",
        "",
        "| policy | n_kept | skip% | kept_med | Δmed vs naive | kept_p10 | Δp10 | kept_win | skip組原med |",
        "|--------|-------:|------:|---------:|--------------:|---------:|-----:|---------:|------------:|",
    ]
    naive = res["pooled"]["P0_naive_L1"]
    for name, m in res["pooled"].items():
        lines.append(
            "| {name} | {nk} | {sr:.1%} | {km} | {dm} | {kp10} | {dp10} | {kw} | {sm} |".format(
                name=name,
                nk=m["n_kept"],
                sr=m["skip_rate"],
                km=f"{m['kept_med']:.2f}" if m["kept_med"] is not None else "—",
                dm=(
                    f"{m['delta_med_vs_naive']:+.2f}"
                    if m["delta_med_vs_naive"] is not None
                    else "—"
                ),
                kp10=f"{m['kept_p10']:.2f}" if m["kept_p10"] is not None else "—",
                dp10=(
                    f"{m['delta_p10_vs_naive']:+.2f}"
                    if m["delta_p10_vs_naive"] is not None
                    else "—"
                ),
                kw=f"{m['kept_win']*100:.1f}%" if m["kept_win"] is not None else "—",
                sm=(
                    f"{m['skip_med_naive']:.2f}"
                    if m["skip_med_naive"] is not None
                    else "—"
                ),
            )
        )
    lines += [
        "",
        f"Naive L1 med={naive['naive_med']:.2f} · p10={naive['naive_p10']:.2f} · "
        f"win={naive['naive_win']*100:.1f}%",
        "",
        "## 解讀鍵",
        "- **Δmed vs naive >0**：留下的單中位超額更高（報酬↑）",
        "- **Δp10 >0**：左尾（最差約10%）變好（風險↓）",
        "- **skip組原med**：被跳過的單在原 L1 下中位；越低表示 skip 抓到差單",
        "",
    ]
    (OUT / "H_FUNNEL_POLICY_OPT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {OUT / 'H_FUNNEL_POLICY_OPT.md'}")
    for name, m in res["pooled"].items():
        print(
            name,
            "kept_med",
            m["kept_med"],
            "dMed",
            m["delta_med_vs_naive"],
            "dP10",
            m["delta_p10_vs_naive"],
            "skip",
            f"{m['skip_rate']:.1%}",
        )
    return 0


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--pareto-thresholds",
        action="store_true",
        help="Grid-search funnel thresholds · Pareto vs naive L1",
    )
    args = ap.parse_args()
    if args.pareto_thresholds:
        raise SystemExit(main_pareto())
    raise SystemExit(main())
