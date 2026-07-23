#!/usr/bin/env python3
"""Entry timing funnel · OPEN vs 09:05 vs 09:25 vs skip policies.

Research only. Rebuild r_adj(entry_px); exit_close / r_ix from ledger.

  PYTHONPATH=src .venv/bin/python scripts/research/run_expert_pool_entry_timing_funnel.py
"""
from __future__ import annotations

import json
import math
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

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
NAMES = {
    "2303": "聯電",
    "2327": "國巨",
    "6515": "穎崴",
    "2344": "華邦電",
    "8046": "南電",
}


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
        fail25 = False
        if a25:
            fail25 = a25["high"] > a25["open"] * 1.01 and a25["close"] < a25["open"]
        perfect = False
        if gap is not None and a5:
            perfect = gap <= 0.03 and a5["ret"] >= -0.005 and a5["dd"] > -0.02
        chase_weak = False
        if gap is not None and a5:
            chase_weak = gap >= 0.03 and a5["ret"] < -0.01 and a5["dd"] < -0.02
        out.append(
            {
                **r,
                "gap": gap,
                "open_px": open_px,
                "px05": px05,
                "px25": px25,
                "a5": a5,
                "a25": a25,
                "fail25": fail25,
                "perfect": perfect,
                "chase_weak": chase_weak,
                "has_intra": a5 is not None and a25 is not None,
                "has_05": a5 is not None,
                "has_25": a25 is not None,
            }
        )
    return out


def rebuild_radj(row: dict, entry_px: float) -> float | None:
    if entry_px is None or entry_px <= 0:
        return None
    r_s = row["exit_close"] / entry_px - 1 - COST
    r_ix = row["r_ix_pct"] / 100.0
    return (r_s - BETA * r_ix) * 100.0


@dataclass
class Decision:
    action: str  # SKIP | OPEN | E05 | E25
    reason: str


PolicyFn = Callable[[dict], Decision]


def metrics(rows: list[dict], decisions: list[Decision]) -> dict:
    kept_adj: list[float] = []
    skipped_base: list[float] = []
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
        "med": median(kept_adj),
        "mean": mean(kept_adj),
        "p10": pctl(kept_adj, 0.10),
        "win": sum(1 for x in kept_adj if x > 0) / len(kept_adj) if kept_adj else None,
        "naive_med": median(naive),
        "delta_med_vs_naive": (
            (median(kept_adj) - median(naive)) if kept_adj and naive else None
        ),
        "skip_med_naive": median(skipped_base),
    }


# --- policies A–F ---


def pol_a_all_open(_r: dict) -> Decision:
    return Decision("OPEN", "A_all_open")


def pol_b_all_e05(r: dict) -> Decision:
    if not r["has_05"] or r["px05"] is None:
        return Decision("SKIP", "B_no_05_k")
    return Decision("E05", "B_all_e05")


def pol_c_all_e25(r: dict) -> Decision:
    if not r["has_25"] or r["px25"] is None:
        return Decision("SKIP", "C_no_25_k")
    return Decision("E25", "C_all_e25")


def pol_d_user_tree(r: dict) -> Decision:
    """User tree from optimize.py pol_user_tree."""
    if not r["has_intra"] or r["gap"] is None:
        return Decision("OPEN", "no_intra_fallback_open")
    g = r["gap"]
    if g >= 0.05:
        if r["perfect"]:
            return Decision("E05", "gap5_but_perfect05")
        if r["a25"] and r["a25"]["ret"] > 0 and not r["fail25"]:
            return Decision("E25", "gap5_wait25_stable")
        return Decision("SKIP", "gap5_still_weak25")
    if g >= 0.02:
        if r["perfect"]:
            return Decision("E05", "gap2_5_perfect")
        if r["chase_weak"]:
            if r["a25"] and r["a25"]["ret"] > 0 and not r["fail25"]:
                return Decision("E25", "gap2_5_chaseweak_then25ok")
            return Decision("SKIP", "gap2_5_chaseweak_skip")
        if r["a25"] and r["a25"]["ret"] > 0 and not r["fail25"]:
            return Decision("E25", "gap2_5_ambiguous_25ok")
        return Decision("SKIP", "gap2_5_ambiguous_skip")
    if r["perfect"]:
        return Decision("E05", "lowgap_perfect")
    if r["chase_weak"]:
        if r["a25"] and r["a25"]["ret"] > 0 and not r["fail25"]:
            return Decision("E25", "lowgap_chaseweak_25ok")
        return Decision("SKIP", "lowgap_chaseweak_skip")
    if r["a25"] and r["a25"]["ret"] > 0 and not r["fail25"]:
        return Decision("E25", "lowgap_amb_25ok")
    return Decision("SKIP", "lowgap_amb_skip")


def pol_e_perfect_e05_else_open(r: dict) -> Decision:
    if r["perfect"] and r["has_05"] and r["px05"] is not None:
        return Decision("E05", "E_perfect05")
    return Decision("OPEN", "E_else_open")


def pol_f_perfect_skip_chase_e25(r: dict) -> Decision:
    if r["perfect"] and r["has_05"] and r["px05"] is not None:
        return Decision("E05", "F_perfect05")
    if r["chase_weak"]:
        return Decision("SKIP", "F_chase_weak_skip")
    if r["a25"] and r["a25"]["ret"] > 0 and not r["fail25"]:
        return Decision("E25", "F_e25_ret_ok")
    return Decision("SKIP", "F_e25_weak_skip")


POLICIES: dict[str, tuple[str, PolicyFn]] = {
    "A_all_open": ("A · 全數 OPEN", pol_a_all_open),
    "B_all_e05": ("B · 全數 E05（有K才）", pol_b_all_e05),
    "C_all_e25": ("C · 全數 E25", pol_c_all_e25),
    "D_user_tree": ("D · 使用者樹", pol_d_user_tree),
    "E_perfect05_else_open": ("E · perfect→E05 else OPEN", pol_e_perfect_e05_else_open),
    "F_perfect_skip_chase_e25": (
        "F · perfect→E05；chase_weak→SKIP；else E25",
        pol_f_perfect_skip_chase_e25,
    ),
}


def eval_all(rows: list[dict]) -> dict:
    intra = [r for r in rows if r["has_intra"] and r["gap"] is not None]
    results = {
        "protocol": {
            "cost_bps": 30,
            "beta": BETA,
            "bench": "IX0001",
            "hold": "L1H7",
            "exit": "ledger exit_close",
            "r_ix": "ledger r_ix_pct",
            "entry_px": "rebuild r_adj(entry_px)",
        },
        "n_all": len(rows),
        "n_intra": len(intra),
        "focus_sids": list(FOCUS),
        "pooled": {},
        "focus": {},
    }
    for key, (_label, fn) in POLICIES.items():
        dec = [fn(r) for r in intra]
        results["pooled"][key] = metrics(intra, dec)
        focus_m = {}
        for sid in FOCUS:
            sub = [r for r in intra if r["sid"] == sid]
            if not sub:
                continue
            focus_m[sid] = metrics(sub, [fn(r) for r in sub])
        results["focus"][key] = focus_m
    return results


def fmt_pct(x: float | None) -> str:
    if x is None:
        return "—"
    return f"{x:+.2f}"


def fmt_win(x: float | None) -> str:
    if x is None:
        return "—"
    return f"{x * 100:.1f}%"


def row_cells(m: dict) -> str:
    return (
        f"{m['n_kept']} | {fmt_pct(m['med'])} | {fmt_pct(m['mean'])} | "
        f"{fmt_pct(m['p10'])} | {fmt_win(m['win'])} | {m['skip_rate']:.1%}"
    )


def build_conclusion(res: dict) -> list[str]:
    p = res["pooled"]
    a = p["A_all_open"]
    b = p["B_all_e05"]
    c = p["C_all_e25"]
    d = p["D_user_tree"]
    e = p["E_perfect05_else_open"]
    f = p["F_perfect_skip_chase_e25"]

    lines = ["## 中文結論", ""]
    # Delay vs open
    delay_better = (b["med"] or -999) > (a["med"] or -999)
    delay25_better = (c["med"] or -999) > (a["med"] or -999)
    lines.append("### 1. 延遲進場本身是賺是賠？")
    lines.append("")
    lines.append(
        f"- **A 全 OPEN**（基線重算）：med {fmt_pct(a['med'])} · p10 {fmt_pct(a['p10'])} · "
        f"n={a['n_kept']}"
    )
    lines.append(
        f"- **B 全 E05**：med {fmt_pct(b['med'])}（Δ vs A "
        f"{fmt_pct((b['med'] or 0) - (a['med'] or 0) if b['med'] is not None and a['med'] is not None else None)}）"
        f" · skip {b['skip_rate']:.1%}"
    )
    lines.append(
        f"- **C 全 E25**：med {fmt_pct(c['med'])}（Δ vs A "
        f"{fmt_pct((c['med'] or 0) - (a['med'] or 0) if c['med'] is not None and a['med'] is not None else None)}）"
        f" · skip {c['skip_rate']:.1%}"
    )
    if delay_better or delay25_better:
        which = []
        if delay_better:
            which.append("09:05")
        if delay25_better:
            which.append("09:25")
        lines.append(
            f"- **判斷**：單純**統一延遲**到 {'／'.join(which)} "
            f"中位超額{'略優於' if (b['med'] or 0) > (a['med'] or 0) + 0.3 or (c['med'] or 0) > (a['med'] or 0) + 0.3 else '未明顯優於'}"
            f"全 OPEN；代價是較高進價與部分缺 K skip。"
        )
    else:
        lines.append(
            "- **判斷**：**統一延遲**（B/C）中位超額**未優於**全 OPEN — "
            "延遲本身不是 alpha 來源，更多是避開開盤劣價的副作用。"
        )
    lines.append("")

    lines.append("### 2. Skip 過濾是否比單純延遲更重要？")
    lines.append("")
    best_skip = max(
        [("D", d), ("F", f)],
        key=lambda x: (x[1]["med"] if x[1]["med"] is not None else -999),
    )
    best_delay_only = max(
        [("B", b), ("C", c)],
        key=lambda x: (x[1]["med"] if x[1]["med"] is not None else -999),
    )
    lines.append(
        f"- **D 使用者樹**：med {fmt_pct(d['med'])} · skip {d['skip_rate']:.1%} · "
        f"skip組原med {fmt_pct(d['skip_med_naive'])}"
    )
    lines.append(
        f"- **E perfect→E05 else OPEN**（無 skip）：med {fmt_pct(e['med'])} · skip {e['skip_rate']:.1%}"
    )
    lines.append(
        f"- **F perfect／chase／E25**：med {fmt_pct(f['med'])} · skip {f['skip_rate']:.1%} · "
        f"skip組原med {fmt_pct(f['skip_med_naive'])}"
    )
    d_vs_e = (
        (d["med"] or 0) - (e["med"] or 0)
        if d["med"] is not None and e["med"] is not None
        else None
    )
    skip_beats_delay = (best_skip[1]["med"] or -999) > (best_delay_only[1]["med"] or -999) + 0.5
    if skip_beats_delay:
        lines.append(
            f"- **判斷**：**Skip／條件樹**（{best_skip[0]} med {fmt_pct(best_skip[1]['med'])}）"
            f"明顯優於純延遲（{best_delay_only[0]} med {fmt_pct(best_delay_only[1]['med'])}）。"
            f"被 skip 組原 L1 med {fmt_pct(best_skip[1]['skip_med_naive'])} "
            f"{'低於' if (best_skip[1]['skip_med_naive'] or 0) < (a['naive_med'] or 0) else '未明顯低於'}"
            f"全樣本 — **過濾弱單比單純等 5/25 分更重要**。"
        )
    else:
        lines.append(
            "- **判斷**：Skip 樹與純延遲差距有限；"
            f"D−E Δmed {fmt_pct(d_vs_e)}。"
            "若 skip 組原 med 仍接近全樣本，則 skip 邊際有限。"
        )
    lines.append("")
    lines.append("### 3. 操作啟示（Research only）")
    lines.append("")
    lines.append(
        "- 無條件等 09:25 **不能**取代開盤；條件樹（gap／perfect／chase_weak／ret25）才有訊息。"
    )
    lines.append(
        "- **E** 驗證：僅 perfect 時改 E05、其餘仍 OPEN — 若 med 接近 A，表示 perfect 標籤 alone 不足。"
    )
    lines.append(
        "- **F** 是較精簡的可執行樹；與 **D** 對照可看 gap 分層是否必要。"
    )
    lines.append("")
    return lines


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    conn = connect(DEFAULT_DB_PATH)
    rows = enrich(conn, load_trades())
    conn.close()
    res = eval_all(rows)

    (OUT / "H_FUNNEL_ENTRY_TIMING.json").write_text(
        json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    lines = [
        "# H_FUNNEL_ENTRY_TIMING · 進場時點 vs Skip 政策",
        "",
        f"- 樣本：全池 {res['n_all']} 筆 · 有完整 09:05+09:25 分K **{res['n_intra']}** 筆",
        "- 協議：trades JSON + stock_kbar_1m · exit_close / r_ix 沿用 ledger · "
        f"cost={COST*10000:.0f}bps · β={BETA}×IX0001 · L1H7 · 重算 r_adj(entry_px)",
        "- Research only · 未採納",
        "",
        "## 政策",
        "",
        "| 代號 | 規則 |",
        "|------|------|",
        "| A | 全數 OPEN |",
        "| B | 全數 E05（有 5 分 K 才，否 SKIP） |",
        "| C | 全數 E25（有 25 分 K 才，否 SKIP） |",
        "| D | 使用者樹（optimize pol_user_tree） |",
        "| E | perfect → E05；else OPEN（不等 25） |",
        "| F | perfect → E05；chase_weak → SKIP；else E25 if ret25>0 & ¬fail25 else SKIP |",
        "",
        "## 全池比較",
        "",
        "| policy | n_kept | med | mean | p10 | win | skip% | Δmed vs naive |",
        "|--------|-------:|----:|----:|----:|----:|------:|--------------:|",
    ]
    naive_med = res["pooled"]["A_all_open"]["naive_med"]
    for key, (label, _fn) in POLICIES.items():
        m = res["pooled"][key]
        lines.append(
            f"| {label} | {m['n_kept']} | {fmt_pct(m['med'])} | {fmt_pct(m['mean'])} | "
            f"{fmt_pct(m['p10'])} | {fmt_win(m['win'])} | {m['skip_rate']:.1%} | "
            f"{fmt_pct(m['delta_med_vs_naive'])} |"
        )
    lines += [
        "",
        f"Ledger naive L1 med（未重算進場價）= {fmt_pct(naive_med)}",
        "",
        "## 五檔 focus",
        "",
    ]
    for sid in FOCUS:
        name = NAMES.get(sid, sid)
        lines.append(f"### {sid} {name}")
        lines.append("")
        lines.append("| policy | n_kept | med | mean | p10 | win | skip% |")
        lines.append("|--------|-------:|----:|----:|----:|----:|------:|")
        for key, (label, _fn) in POLICIES.items():
            fm = res["focus"][key].get(sid)
            if not fm:
                lines.append(f"| {label} | — | — | — | — | — | — |")
                continue
            lines.append(
                f"| {label} | {fm['n_kept']} | {fmt_pct(fm['med'])} | {fmt_pct(fm['mean'])} | "
                f"{fmt_pct(fm['p10'])} | {fmt_win(fm['win'])} | {fm['skip_rate']:.1%} |"
            )
        lines.append("")

    lines.extend(build_conclusion(res))

    (OUT / "H_FUNNEL_ENTRY_TIMING.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {OUT / 'H_FUNNEL_ENTRY_TIMING.md'}")
    for key in POLICIES:
        m = res["pooled"][key]
        print(key, "med", m["med"], "n", m["n_kept"], "skip", f"{m['skip_rate']:.1%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
