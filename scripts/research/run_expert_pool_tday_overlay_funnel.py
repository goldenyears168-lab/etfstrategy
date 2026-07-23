#!/usr/bin/env python3
"""T-day pattern/branch risk overlays on staged user funnel.

Research only. Compares O1–O5 vs P0 naive L1 open & P1 user_tree.

  PYTHONPATH=src .venv/bin/python scripts/research/run_expert_pool_tday_overlay_funnel.py

Writes:
  reports/.../avoid_morphology/H_FUNNEL_TDAY_OVERLAY.{json,md}
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts" / "research"))

import run_expert_pool_staged_funnel_optimize as opt  # noqa: E402

OUT = opt.OUT
FOCUS = opt.FOCUS
NAMES = {
    "2303": "聯電",
    "2327": "國巨",
    "6515": "穎崴",
    "2344": "華邦電",
    "8046": "南電",
}


def any_tday_flag(r: dict) -> bool:
    td = r["td"]
    return bool(
        td.get("hot5")
        or td.get("t_reject")
        or (td.get("max_today_yi") or 0) >= 5
    )


def flag_count(r: dict) -> int:
    td = r["td"]
    n = 0
    if td.get("hot5"):
        n += 1
    if td.get("t_reject"):
        n += 1
    if (td.get("max_today_yi") or 0) >= 5:
        n += 1
    return n


def flag_ids(r: dict) -> list[str]:
    td = r["td"]
    out: list[str] = []
    if td.get("hot5"):
        out.append("HOT5")
    if td.get("t_reject"):
        out.append("T_REJECT")
    if (td.get("max_today_yi") or 0) >= 5:
        out.append("MAX_TODAY_5YI")
    return out


def wait25_or_skip(r: dict, reason_ok: str, reason_fail: str) -> opt.Decision:
    if r.get("a25") and r["a25"]["ret"] > 0 and not r["fail25"]:
        return opt.Decision("E25", reason_ok)
    return opt.Decision("SKIP", reason_fail)


def downgrade_no_e05(r: dict, base: opt.Decision, tag: str) -> opt.Decision:
    """Any T-day flag: forbid E05 / OPEN → E25 or SKIP."""
    if base.action == "E05":
        return wait25_or_skip(r, f"{tag}_downgrade25", f"{tag}_skip")
    if base.action == "OPEN":
        return wait25_or_skip(r, f"{tag}_open_to25", f"{tag}_skip")
    return base


def pol_o1(r: dict) -> opt.Decision:
    """O1: any flag → no E05; max E25 / SKIP (align P4_tday_overlay)."""
    base = opt.pol_user_tree(r)
    if not any_tday_flag(r):
        return base
    return downgrade_no_e05(r, base, "o1")


def pol_o2(r: dict) -> opt.Decision:
    """O2: any flag → SKIP."""
    if any_tday_flag(r):
        return opt.Decision("SKIP", "o2_any_flag")
    return opt.pol_user_tree(r)


def pol_o3(r: dict) -> opt.Decision:
    """O3: T_REJECT → SKIP; HOT5 / MAX5 → force wait25."""
    td = r["td"]
    base = opt.pol_user_tree(r)
    if td.get("t_reject"):
        return opt.Decision("SKIP", "o3_treject_skip")
    if td.get("hot5") or (td.get("max_today_yi") or 0) >= 5:
        if base.action in ("E05", "OPEN"):
            return wait25_or_skip(r, "o3_force25", "o3_force25_fail")
        return base
    return base


def pol_o4(r: dict) -> opt.Decision:
    """O4: MAX≥5 → wait25; T_REJECT on 2303 only → SKIP."""
    td = r["td"]
    base = opt.pol_user_tree(r)
    if td.get("t_reject") and r["sid"] == "2303":
        return opt.Decision("SKIP", "o4_2303_treject")
    if (td.get("max_today_yi") or 0) >= 5:
        if base.action in ("E05", "OPEN"):
            return wait25_or_skip(r, "o4_max5_force25", "o4_max5_fail")
        return base
    return base


def pol_o5(r: dict) -> opt.Decision:
    """O5: flag count ≥2 → SKIP; else user tree."""
    if flag_count(r) >= 2:
        return opt.Decision("SKIP", "o5_multi_flag")
    return opt.pol_user_tree(r)


OVERLAYS: dict[str, opt.PolicyFn] = {
    "O1_any_no_e05": pol_o1,
    "O2_any_skip": pol_o2,
    "O3_treject_skip_hot_max25": pol_o3,
    "O4_max25_2303_treject": pol_o4,
    "O5_multi2_skip": pol_o5,
}

BASELINES = ("P0_naive_L1", "P1_user_tree")


def flag_inventory(rows: list[dict]) -> dict:
    inv = {
        "n_intra": len(rows),
        "any_flag": 0,
        "hot5": 0,
        "t_reject": 0,
        "max5": 0,
        "multi2": 0,
        "by_sid": {},
    }
    for r in rows:
        td = r["td"]
        flags = flag_ids(r)
        if flags:
            inv["any_flag"] += 1
        if td.get("hot5"):
            inv["hot5"] += 1
        if td.get("t_reject"):
            inv["t_reject"] += 1
        if (td.get("max_today_yi") or 0) >= 5:
            inv["max5"] += 1
        if flag_count(r) >= 2:
            inv["multi2"] += 1
        sid = r["sid"]
        bs = inv["by_sid"].setdefault(
            sid, {"n": 0, "any_flag": 0, "flags": {"HOT5": 0, "T_REJECT": 0, "MAX_TODAY_5YI": 0}}
        )
        bs["n"] += 1
        if flags:
            bs["any_flag"] += 1
        for fid in flags:
            bs["flags"][fid] = bs["flags"].get(fid, 0) + 1
    return inv


def eval_overlays(rows: list[dict]) -> dict:
    intra = [r for r in rows if r["has_intra"] and r["gap"] is not None]
    out: dict = {
        "n_all": len(rows),
        "n_intra": len(intra),
        "flag_inventory": flag_inventory(intra),
        "baselines": {},
        "overlays": {},
        "focus": {},
        "recommendation": {},
    }
    all_policies = {
        "P0_naive_L1": opt.pol_naive,
        "P1_user_tree": opt.pol_user_tree,
        **OVERLAYS,
    }
    for name, fn in all_policies.items():
        dec = [fn(r) for r in intra]
        m = opt.metrics(intra, dec)
        bucket = "baselines" if name in BASELINES else "overlays"
        out[bucket][name] = m
        focus_m = {}
        for sid in FOCUS:
            sub = [r for r in intra if r["sid"] == sid]
            if sub:
                focus_m[sid] = opt.metrics(sub, [fn(r) for r in sub])
        out["focus"][name] = focus_m

    p0 = out["baselines"]["P0_naive_L1"]
    p1 = out["baselines"]["P1_user_tree"]
    scored = []
    for name, m in out["overlays"].items():
        scored.append(
            {
                "name": name,
                "delta_p10": m.get("delta_p10_vs_naive"),
                "delta_med": m.get("delta_med_vs_naive"),
                "skip_rate": m.get("skip_rate"),
                "n_kept": m.get("n_kept"),
                "kept_p10": m.get("kept_p10"),
                "kept_med": m.get("kept_med"),
                "skip_med_naive": m.get("skip_med_naive"),
            }
        )

    # Best Δp10 among overlays with skip not excessive vs P1 (+15pp cap, max 55%)
    max_skip = min(0.55, (p1.get("skip_rate") or 0) + 0.15)
    eligible = [
        s
        for s in scored
        if s["delta_p10"] is not None and (s["skip_rate"] or 0) <= max_skip
    ]
    eligible.sort(key=lambda x: (-x["delta_p10"], -(x["delta_med"] or -999)))
    best = eligible[0] if eligible else None
    best_unrestricted = max(
        scored, key=lambda x: (x["delta_p10"] if x["delta_p10"] is not None else -999)
    )

    out["recommendation"] = {
        "p0_naive_p10": p0.get("naive_p10"),
        "p1_user_skip_rate": p1.get("skip_rate"),
        "max_skip_reasonable": max_skip,
        "best_delta_p10_eligible": best,
        "best_delta_p10_unrestricted": best_unrestricted,
        "vs_p1_user_tree": {
            name: {
                "delta_p10_vs_p1": (
                    (m.get("kept_p10") - p1.get("kept_p10"))
                    if m.get("kept_p10") is not None and p1.get("kept_p10") is not None
                    else None
                ),
                "delta_med_vs_p1": (
                    (m.get("kept_med") - p1.get("kept_med"))
                    if m.get("kept_med") is not None and p1.get("kept_med") is not None
                    else None
                ),
                "skip_delta_vs_p1": (m.get("skip_rate") or 0) - (p1.get("skip_rate") or 0),
            }
            for name, m in out["overlays"].items()
        },
    }
    return out


def fmt_pct(x: float | None, digits: int = 1) -> str:
    if x is None:
        return "—"
    return f"{x * 100:.{digits}f}%"


def fmt_pp(x: float | None) -> str:
    if x is None:
        return "—"
    return f"{x:+.2f}"


def render_md(res: dict) -> str:
    inv = res["flag_inventory"]
    p0 = res["baselines"]["P0_naive_L1"]
    p1 = res["baselines"]["P1_user_tree"]
    rec = res["recommendation"]
    lines = [
        "# T 日圖形／分點風險 × 三階段漏斗 Overlay",
        "",
        "Research only · 未採納 Order",
        "",
        "## 凍結旗標（T 日 · 20:00 可判）",
        "",
        "| 旗標 | 定義 |",
        "|------|------|",
        "| **HOT5** | 訊號前 5 交易日報酬 ≥+20% |",
        "| **T_REJECT** | 收黑 ∧ close_pos<0.33，或 body<−3% |",
        "| **MAX_TODAY_5YI** | 分點 ledger「今」席 amt_yi ≥5 億 |",
        "",
        f"- 樣本：全池 {res['n_all']} 筆 · 有完整 09:05+09:25 分K **{res['n_intra']}** 筆",
        f"- 任一旗標：**{inv['any_flag']}**（HOT5={inv['hot5']} · T_REJECT={inv['t_reject']} · "
        f"MAX≥5億={inv['max5']} · 旗標≥2={inv['multi2']}）",
        "- 報酬重算：進場=OPEN／09:05／09:25 收 · 出場仍 ledger exit_close · cost 30bps · "
        "r_ix 沿用 ledger",
        "",
        "## 基準",
        "",
        "| 政策 | n_kept | skip% | kept_med | Δmed vs P0 | kept_p10 | Δp10 vs P0 |",
        "|------|-------:|------:|---------:|-----------:|---------:|-----------:|",
    ]
    for name in BASELINES:
        m = res["baselines"][name]
        lines.append(
            f"| {name} | {m['n_kept']} | {fmt_pct(m['skip_rate'])} | "
            f"{fmt_pp(m['kept_med'])} | {fmt_pp(m['delta_med_vs_naive'])} | "
            f"{fmt_pp(m['kept_p10'])} | {fmt_pp(m['delta_p10_vs_naive'])} |"
        )
    lines += [
        "",
        f"P0 naive：med={fmt_pp(p0['naive_med'])} · p10={fmt_pp(p0['naive_p10'])} · "
        f"win={fmt_pct(p0['naive_win'])}",
        "",
        "## Overlay O1–O5 vs P0 / P1",
        "",
        "| overlay | 規則摘要 | n_kept | skip% | Δskip vs P1 | kept_med | Δmed vs P0 | "
        "kept_p10 | Δp10 vs P0 | Δp10 vs P1 | skip組原med |",
        "|---------|----------|-------:|------:|------------:|---------:|-----------:|"
        "---------:|-----------:|-----------:|------------:|",
    ]
    summaries = {
        "O1_any_no_e05": "任一旗標→禁 E05，最多 E25／SKIP（≈P4）",
        "O2_any_skip": "任一旗標→直接 SKIP",
        "O3_treject_skip_hot_max25": "T_REJECT→SKIP；HOT5／MAX5→強制等25",
        "O4_max25_2303_treject": "MAX≥5→等25；僅 2303 的 T_REJECT→SKIP",
        "O5_multi2_skip": "旗標≥2→SKIP，否則 user_tree",
    }
    for name, m in res["overlays"].items():
        vs_p1 = rec["vs_p1_user_tree"][name]
        lines.append(
            f"| {name} | {summaries[name]} | {m['n_kept']} | {fmt_pct(m['skip_rate'])} | "
            f"{fmt_pp(vs_p1['skip_delta_vs_p1'] * 100 if vs_p1['skip_delta_vs_p1'] is not None else None)}pp | "
            f"{fmt_pp(m['kept_med'])} | {fmt_pp(m['delta_med_vs_naive'])} | "
            f"{fmt_pp(m['kept_p10'])} | {fmt_pp(m['delta_p10_vs_naive'])} | "
            f"{fmt_pp(vs_p1['delta_p10_vs_p1'])} | "
            f"{fmt_pp(m['skip_med_naive'])} |"
        )

    best = rec.get("best_delta_p10_eligible")
    best_u = rec.get("best_delta_p10_unrestricted")
    lines += [
        "",
        "## 解讀",
        "",
        "- **Δp10 vs P0 >0**：左尾（最差約 10%）改善 → 風險↓",
        "- **Δmed vs P0 >0**：留存單中位超額↑ → 報酬↑",
        "- **skip組原med 越低**：被跳過的單在原 L1 下越差 → skip 越「對」",
        "",
        "## 五檔旗標覆蓋",
        "",
        "| sid | n | 任一旗標 | HOT5 | T_REJECT | MAX≥5億 |",
        "|-----|--:|--------:|-----:|---------:|--------:|",
    ]
    for sid in FOCUS:
        bs = inv["by_sid"].get(sid, {})
        fl = bs.get("flags") or {}
        lines.append(
            f"| {sid} {NAMES.get(sid, '')} | {bs.get('n', 0)} | {bs.get('any_flag', 0)} | "
            f"{fl.get('HOT5', 0)} | {fl.get('T_REJECT', 0)} | {fl.get('MAX_TODAY_5YI', 0)} |"
        )

    lines += ["", "## 推薦（Δp10 優先 · skip 不過分）", ""]
    if best:
        lines += [
            f"**首選（skip ≤ {fmt_pct(rec['max_skip_reasonable'])}）**：`{best['name']}`",
            "",
            f"- Δp10 vs P0：**{fmt_pp(best['delta_p10'])}** · Δmed vs P0：{fmt_pp(best['delta_med'])}",
            f"- skip **{fmt_pct(best['skip_rate'])}**（P1 user_tree={fmt_pct(p1['skip_rate'])}）",
            f"- kept_p10={fmt_pp(best['kept_p10'])} · skip組原med={fmt_pp(best['skip_med_naive'])}",
            "",
        ]
    else:
        lines.append("（無 overlay 同時滿足 skip 上限與 Δp10 改善）", "")

    if best_u and (not best or best_u["name"] != best["name"]):
        lines += [
            f"**Δp10 絕對最高（不限制 skip）**：`{best_u['name']}` · "
            f"Δp10={fmt_pp(best_u['delta_p10'])} · skip={fmt_pct(best_u['skip_rate'])}",
            "",
        ]

    lines += [
        "### 一句話",
        "",
    ]
    if best:
        lines.append(
            f"在 user_tree 之上疊 T 日旗標，**{best['name']}** 以有限 skip 換最大左尾改善；"
            f"若需最狠避險可看 {best_u['name']}（skip 較高）。"
        )
    else:
        lines.append(
            "T 日 overlay 對全池左尾改善有限；維持 **P1 user_tree** 或僅疊 **O4** 個股化較穩。"
        )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    conn = opt.connect(opt.DEFAULT_DB_PATH)
    rows = opt.enrich(conn, opt.load_trades())
    conn.close()
    res = eval_overlays(rows)
    jp = OUT / "H_FUNNEL_TDAY_OVERLAY.json"
    mp = OUT / "H_FUNNEL_TDAY_OVERLAY.md"
    jp.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    mp.write_text(render_md(res), encoding="utf-8")
    print(f"wrote {mp}")
    best = res["recommendation"].get("best_delta_p10_eligible")
    if best:
        print(
            "best_eligible",
            best["name"],
            "dP10",
            best["delta_p10"],
            "skip",
            f"{best['skip_rate']:.1%}",
        )
    for name, m in res["overlays"].items():
        print(
            name,
            "dP10",
            m["delta_p10_vs_naive"],
            "dMed",
            m["delta_med_vs_naive"],
            "skip",
            f"{m['skip_rate']:.1%}",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
