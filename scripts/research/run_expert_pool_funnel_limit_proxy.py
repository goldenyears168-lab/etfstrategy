#!/usr/bin/env python3
"""Funnel limit-order proxy · vs SKIP / naive OPEN when 不要市價追.

When gap≥5% or chase_weak, do not OPEN at market; try limit fill instead:
  L1 limit = signal_close × 1.01
  L2 limit = open (不追)
  L3 gap≥5% → L1 limit; gap 2–5% → user tree @05/25

Fill proxy (declared approximation):
  09:00–09:25 session low ≤ limit → filled at limit, hold to ledger exit
  else → SKIP (unfilled)

Research only.

  PYTHONPATH=src .venv/bin/python scripts/research/run_expert_pool_funnel_limit_proxy.py
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402

# reuse enrich / user tree from policy optimizer
sys.path.insert(0, str(ROOT / "scripts" / "research"))
from run_expert_pool_staged_funnel_optimize import (  # noqa: E402
    Decision,
    FOCUS,
    OUT,
    enrich,
    load_trades,
    mean,
    median,
    metrics,
    pctl,
    pol_gap5_skip,
    pol_naive,
    pol_user_tree,
    rebuild_radj,
)

PolicyFn = Callable[[dict], Decision]


def _signal_close(r: dict) -> float | None:
    tc = r.get("signal_close")
    if tc is not None and tc > 0:
        return float(tc)
    return None


def _morning_low_0925(r: dict) -> float | None:
    a25 = r.get("a25")
    if a25 and a25.get("low"):
        return float(a25["low"])
    return None


def _limit_fill(r: dict, limit_px: float) -> tuple[bool, float]:
    """Return (filled, entry_px)."""
    low = _morning_low_0925(r)
    if low is None or limit_px <= 0:
        return False, limit_px
    if low <= limit_px:
        return True, limit_px
    return False, limit_px


def _no_chase_trigger(r: dict) -> bool:
    g = r.get("gap")
    if g is not None and g >= 0.05:
        return True
    return bool(r.get("chase_weak"))


def _entry_from_decision(r: dict, d: Decision) -> float | None:
    if d.action == "SKIP":
        return None
    return {
        "OPEN": r["open_px"],
        "E05": r["px05"] or r["open_px"],
        "E25": r["px25"] or r["px05"] or r["open_px"],
    }.get(d.action)


@dataclass
class LimitOutcome:
    action: str  # SKIP | FILLED | (delegated OPEN/E05/E25)
    entry_px: float | None
    reason: str
    limit_px: float | None = None
    filled: bool | None = None


def eval_policy(
    rows: list[dict],
    decide: Callable[[dict], LimitOutcome],
) -> dict:
    kept_adj: list[float] = []
    skipped_base: list[float] = []
    naive = [r["r_adj"] for r in rows]
    n_limit_attempt = 0
    n_filled = 0
    actions: dict[str, int] = {}

    for r in rows:
        o = decide(r)
        actions[o.action] = actions.get(o.action, 0) + 1
        if o.limit_px is not None:
            n_limit_attempt += 1
            if o.filled:
                n_filled += 1

        if o.action == "SKIP" or o.entry_px is None:
            skipped_base.append(r["r_adj"])
            continue
        radj = rebuild_radj(r, o.entry_px)
        if radj is not None:
            kept_adj.append(radj)

    base = metrics(rows, [Decision("OPEN", "x")] * len(rows))  # naive placeholders
    base = {
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
            (pctl(kept_adj, 0.10) - pctl(naive, 0.10)) if kept_adj and naive else None
        ),
        "fill_rate": (n_filled / n_limit_attempt) if n_limit_attempt else None,
        "n_limit_attempt": n_limit_attempt,
        "n_filled": n_filled,
    }
    return base


def pol_l1_limit_tc101(r: dict) -> LimitOutcome:
    if not _no_chase_trigger(r):
        d = pol_user_tree(r)
        px = _entry_from_decision(r, d)
        return LimitOutcome(d.action, px, d.reason)
    tc = _signal_close(r)
    if tc is None:
        return LimitOutcome("SKIP", None, "no_signal_close")
    limit = tc * 1.01
    filled, px = _limit_fill(r, limit)
    if filled:
        return LimitOutcome("FILLED", px, "L1_limit_tc101", limit_px=limit, filled=True)
    return LimitOutcome("SKIP", None, "L1_unfilled", limit_px=limit, filled=False)


def pol_l2_limit_open(r: dict) -> LimitOutcome:
    if not _no_chase_trigger(r):
        d = pol_user_tree(r)
        px = _entry_from_decision(r, d)
        return LimitOutcome(d.action, px, d.reason)
    op = r.get("open_px")
    if op is None or op <= 0:
        return LimitOutcome("SKIP", None, "no_open")
    limit = float(op)
    filled, px = _limit_fill(r, limit)
    if filled:
        return LimitOutcome("FILLED", px, "L2_limit_open", limit_px=limit, filled=True)
    return LimitOutcome("SKIP", None, "L2_unfilled", limit_px=limit, filled=False)


def pol_l3_hybrid(r: dict) -> LimitOutcome:
    g = r.get("gap")
    if g is not None and g >= 0.05:
        tc = _signal_close(r)
        if tc is None:
            return LimitOutcome("SKIP", None, "L3_gap5_no_tc")
        limit = tc * 1.01
        filled, px = _limit_fill(r, limit)
        if filled:
            return LimitOutcome("FILLED", px, "L3_gap5_limit", limit_px=limit, filled=True)
        return LimitOutcome("SKIP", None, "L3_gap5_unfilled", limit_px=limit, filled=False)
    d = pol_user_tree(r)
    px = _entry_from_decision(r, d)
    return LimitOutcome(d.action, px, d.reason)


def pol_skip_gap5(r: dict) -> LimitOutcome:
    d = pol_gap5_skip(r)
    if d.action == "SKIP":
        return LimitOutcome("SKIP", None, d.reason)
    return LimitOutcome("OPEN", r["open_px"], d.reason)


def pol_naive_open(r: dict) -> LimitOutcome:
    d = pol_naive(r)
    return LimitOutcome("OPEN", r["open_px"], d.reason)


POLICIES: dict[str, Callable[[dict], LimitOutcome]] = {
    "B0_naive_OPEN": pol_naive_open,
    "B1_SKIP_gap5": pol_skip_gap5,
    "L1_limit_tc101": pol_l1_limit_tc101,
    "L2_limit_open": pol_l2_limit_open,
    "L3_hybrid_gap5limit": pol_l3_hybrid,
}


def _delta_vs_skip(m: dict, skip_m: dict) -> dict:
    return {
        "delta_med_vs_skip": (
            (m["kept_med"] - skip_m["kept_med"])
            if m["kept_med"] is not None and skip_m["kept_med"] is not None
            else None
        ),
        "delta_p10_vs_skip": (
            (m["kept_p10"] - skip_m["kept_p10"])
            if m["kept_p10"] is not None and skip_m["kept_p10"] is not None
            else None
        ),
        "delta_kept_n_vs_skip": m["n_kept"] - skip_m["n_kept"],
    }


def eval_all(rows: list[dict]) -> dict:
    intra = [r for r in rows if r["has_intra"] and r["gap"] is not None]
    # attach signal close explicitly
    for r in intra:
        tc = r.get("signal_close")
        if tc is None:
            conn_tc = r.get("_tc")
            if conn_tc:
                r["signal_close"] = conn_tc

    results = {
        "n_all": len(rows),
        "n_intra": len(intra),
        "fill_model": {
            "window": "09:00-09:25",
            "rule": "session_low <= limit → fill at limit; else SKIP",
            "signal_close": "stock_daily_bars.close @ signal_date (finmind)",
            "exit": "ledger exit_close unchanged",
            "cost_bps": 30,
            "beta_ix": 1.15,
            "caveats": [
                "限價成交順序未建模（低點觸及≠保證排隊成交）",
                "09:00–09:04 開盤噪訊：low 可能低估可成交性",
                "r_ix 沿用 ledger（延遲進場未重算 β 窗）",
                "L1/L2 非 chase 分支沿用 user_tree（非 naive OPEN）",
            ],
        },
        "pooled": {},
        "focus": {},
    }

    pooled: dict[str, dict] = {}
    for name, fn in POLICIES.items():
        pooled[name] = eval_policy(intra, fn)

    skip_m = pooled["B1_SKIP_gap5"]
    for name, m in pooled.items():
        m.update(_delta_vs_skip(m, skip_m))

    results["pooled"] = pooled

    for sid in FOCUS:
        sub = [r for r in intra if r["sid"] == sid]
        if not sub:
            continue
        fm: dict[str, dict] = {}
        for name, fn in POLICIES.items():
            fm[name] = eval_policy(sub, fn)
        skip_s = fm["B1_SKIP_gap5"]
        for name, m in fm.items():
            m.update(_delta_vs_skip(m, skip_s))
        results["focus"][sid] = fm

    return results


def _attach_signal_close(conn, rows: list[dict]) -> None:
    from run_expert_pool_staged_funnel_optimize import SOURCE

    for r in rows:
        t_close = conn.execute(
            "SELECT close FROM stock_daily_bars WHERE stock_id=? AND trade_date=? AND source=?",
            (r["sid"], r["signal_date"], SOURCE),
        ).fetchone()
        r["signal_close"] = float(t_close[0]) if t_close else None


def render_md(res: dict) -> str:
    lines = [
        "# 漏斗限價替代 · 填單代理（H_FUNNEL_LIMIT_PROXY）",
        "",
        f"- 樣本：全池 {res['n_all']} · 有完整 5+25 分K **{res['n_intra']}**",
        "- **Research only** · 未採納",
        "",
        "## 填單近似（聲明）",
        "",
        f"- 窗：`{res['fill_model']['window']}` · 規則：**low ≤ limit → 以 limit 成交**；否則 SKIP",
        f"- 訊號收：`{res['fill_model']['signal_close']}`",
        f"- 出場：仍用 ledger `exit_close` · cost 30bps · β=1.15×IX0001",
        "",
        "### 限制",
    ]
    for c in res["fill_model"]["caveats"]:
        lines.append(f"- {c}")
    lines += [
        "",
        "## 政策定義",
        "",
        "| 臂 | 規則 |",
        "|----|------|",
        "| **B0 naive OPEN** | 一律開盤進 |",
        "| **B1 SKIP gap≥5%** | gap≥5% 直接跳過；其餘開盤 |",
        "| **L1 limit T收×1.01** | gap≥5% 或 chase_weak → 限價=T收×1.01；否則 user_tree |",
        "| **L2 limit 開盤** | 同上觸發 → 限價=開盤（不追）；否則 user_tree |",
        "| **L3 hybrid** | gap≥5% → L1 限價；gap 2–5% → user_tree 等05/25 |",
        "",
        "## 全池比較（vs naive · vs SKIP）",
        "",
        "| policy | n_kept | skip% | fill率 | kept_med | Δmed vs naive | Δmed vs SKIP | kept_p10 | Δp10 vs naive | Δp10 vs SKIP |",
        "|--------|-------:|------:|------:|---------:|--------------:|-------------:|---------:|--------------:|-------------:|",
    ]
    for name, m in res["pooled"].items():
        fr = f"{m['fill_rate']*100:.1f}%" if m.get("fill_rate") is not None else "—"
        lines.append(
            "| {name} | {nk} | {sr:.1%} | {fr} | {km} | {dmn} | {dms} | {kp10} | {dpn} | {dps} |".format(
                name=name,
                nk=m["n_kept"],
                sr=m["skip_rate"],
                fr=fr,
                km=f"{m['kept_med']:.2f}" if m["kept_med"] is not None else "—",
                dmn=(
                    f"{m['delta_med_vs_naive']:+.2f}"
                    if m["delta_med_vs_naive"] is not None
                    else "—"
                ),
                dms=(
                    f"{m['delta_med_vs_skip']:+.2f}"
                    if m.get("delta_med_vs_skip") is not None
                    else "—"
                ),
                kp10=f"{m['kept_p10']:.2f}" if m["kept_p10"] is not None else "—",
                dpn=(
                    f"{m['delta_p10_vs_naive']:+.2f}"
                    if m["delta_p10_vs_naive"] is not None
                    else "—"
                ),
                dps=(
                    f"{m['delta_p10_vs_skip']:+.2f}"
                    if m.get("delta_p10_vs_skip") is not None
                    else "—"
                ),
            )
        )

    skip = res["pooled"]["B1_SKIP_gap5"]
    naive = res["pooled"]["B0_naive_OPEN"]
    lines += [
        "",
        "## 解讀",
        "",
        f"- SKIP 基準：kept_med={skip['kept_med']:.2f} · p10={skip['kept_p10']:.2f} · n_kept={skip['n_kept']}",
        f"- Naive：kept_med={naive['kept_med']:.2f} · p10={naive['kept_p10']:.2f}",
        "- **fill率** 僅對有掛限價的 chase 分支計數（L1/L2/L3 gap5 子集）",
        "- **Δmed vs SKIP >0**：限價替代比單純跳過保留更多中位超額",
        "- **Δp10 vs SKIP >0**：左尾改善（風險↓）",
        "",
    ]

    if res.get("focus"):
        lines += ["## 焦點標的", ""]
        for sid, fm in res["focus"].items():
            lines.append(f"### {sid}")
            lines.append("")
            lines.append("| policy | n_kept | fill率 | kept_med | Δmed vs SKIP | Δp10 vs SKIP |")
            lines.append("|--------|-------:|------:|---------:|-------------:|-------------:|")
            for name, m in fm.items():
                fr = f"{m['fill_rate']*100:.1f}%" if m.get("fill_rate") is not None else "—"
                lines.append(
                    "| {name} | {nk} | {fr} | {km} | {dms} | {dps} |".format(
                        name=name,
                        nk=m["n_kept"],
                        fr=fr,
                        km=f"{m['kept_med']:.2f}" if m["kept_med"] is not None else "—",
                        dms=(
                            f"{m['delta_med_vs_skip']:+.2f}"
                            if m.get("delta_med_vs_skip") is not None
                            else "—"
                        ),
                        dps=(
                            f"{m['delta_p10_vs_skip']:+.2f}"
                            if m.get("delta_p10_vs_skip") is not None
                            else "—"
                        ),
                    )
                )
            lines.append("")

    return "\n".join(lines) + "\n"


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    conn = connect(DEFAULT_DB_PATH)
    rows = enrich(conn, load_trades())
    _attach_signal_close(conn, rows)
    conn.close()
    res = eval_all(rows)
    (OUT / "H_FUNNEL_LIMIT_PROXY.json").write_text(
        json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    md = render_md(res)
    (OUT / "H_FUNNEL_LIMIT_PROXY.md").write_text(md, encoding="utf-8")
    print(f"wrote {OUT / 'H_FUNNEL_LIMIT_PROXY.md'}")
    for name, m in res["pooled"].items():
        print(
            name,
            "kept",
            m["n_kept"],
            "fill",
            m.get("fill_rate"),
            "med",
            m["kept_med"],
            "dMedSkip",
            m.get("delta_med_vs_skip"),
            "dP10Skip",
            m.get("delta_p10_vs_skip"),
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
