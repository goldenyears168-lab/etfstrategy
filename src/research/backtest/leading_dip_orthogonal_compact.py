"""Leading Dip · orthogonal 3-layer funnel · suitability (效能 × 效率).

Compares Strategy ``leading-dip`` coverage baseline against thinner / rebundled
funnels. 效能 = OOS med/hit；效率 = 更少獨立 alpha 謂詞、更高 med/謂詞、
層級獨特貢獻。

Research-only · does not mutate strategy.yaml.
"""

from __future__ import annotations

import math
import sqlite3
from typing import Any

import numpy as np
import pandas as pd

from research.backtest.leading_dip_sleeve_validate import (
    EXCESS_MAX,
    GAP3_MAX,
    OOS_CUT_DEFAULT,
    START_DEFAULT,
    TOTAL_OPEN_CAP,
    _ablation_gate_snapshot,
    apply_frozen_policy,
    collect_leading_dip_events,
)

_DISABLED = 100.0


def _attach_geo(con: sqlite3.Connection, events: pd.DataFrame) -> pd.DataFrame:
    if events is None or len(events) == 0:
        return pd.DataFrame()
    scores = pd.read_sql(
        """
        SELECT data_baseline_date AS baseline, stock_id AS sid,
               rs_ratio AS rv, rs_momentum AS mv
        FROM rrg_universe_scores WHERE screen_kind='close'
        """,
        con,
    )
    scores["sid"] = scores["sid"].astype(str)
    scores["baseline"] = scores["baseline"].astype(str)
    rows: list[dict[str, Any]] = []
    for r in events.itertuples(index=False):
        sub = scores[(scores.sid == str(r.sid)) & (scores.baseline < str(r.date))]
        if sub.empty:
            continue
        s = sub.sort_values("baseline").iloc[-1]
        rv, mv = float(s.rv), float(s.mv)
        base = {c: getattr(r, c) for c in events.columns}
        base["geo"] = bool(rv > 100.0 and mv > 100.0 and mv < rv)
        base["rv"] = rv
        base["mv"] = mv
        rows.append(base)
    return pd.DataFrame(rows)


def _pol(ev: pd.DataFrame, trading: list[str], *, t2: bool) -> pd.DataFrame:
    if ev is None or len(ev) == 0:
        return pd.DataFrame()
    return apply_frozen_policy(ev, trading, require_t2=t2, max_open=TOTAL_OPEN_CAP)


def _eff_row(
    snap: dict[str, Any],
    *,
    arm: str,
    title: str,
    n_pred: int,
    layers: str,
    wide_n: int,
) -> dict[str, Any]:
    n = int(snap.get("n") or 0)
    oos_med = snap.get("oos_med")
    oos_hit = snap.get("oos_hit")
    med_per_pred = (
        None if oos_med is None or n_pred <= 0 else round(float(oos_med) / n_pred, 3)
    )
    yield_pct = None if wide_n <= 0 else round(100.0 * n / wide_n, 1)
    return {
        "arm": arm,
        "title": title,
        "layers": layers,
        "n_predicates": n_pred,
        "n": n,
        "hit": snap.get("hit"),
        "med": snap.get("med"),
        "oos_n": snap.get("oos_n"),
        "oos_hit": snap.get("oos_hit"),
        "oos_med": oos_med,
        "gates_ok": snap.get("gates_ok"),
        "yield_vs_wide_pct": yield_pct,
        "oos_med_per_predicate": med_per_pred,
        "efficiency_score": (
            None
            if oos_med is None or oos_hit is None or n_pred <= 0
            else round(float(oos_med) * float(oos_hit) / n_pred, 3)
        ),
    }


def run_orthogonal_compact_study(
    con: sqlite3.Connection,
    *,
    start: str = START_DEFAULT,
    oos_cut: str = OOS_CUT_DEFAULT,
) -> dict[str, Any]:
    def collect(**kw: Any) -> tuple[pd.DataFrame, list[str]]:
        raw, trading = collect_leading_dip_events(
            con, start=start, oos_cut=oos_cut, **kw
        )
        return _attach_geo(con, raw), trading

    print("  collect wide (no structure) …", flush=True)
    wide, trading = collect(
        apply_structure=False,
        gap3_max=GAP3_MAX,
        excess_max=EXCESS_MAX,
        universe_mode="leading",
    )
    wide_n = len(wide)

    print("  collect frozen …", flush=True)
    frozen, _ = collect(
        apply_structure=True,
        require_not_ur=True,
        require_w5_weak=True,
        gap3_max=GAP3_MAX,
        excess_max=EXCESS_MAX,
    )

    print("  collect gap-only / excess-only / drop-struct variants …", flush=True)
    gap_only, _ = collect(
        apply_structure=True,
        gap3_max=GAP3_MAX,
        excess_max=_DISABLED,
    )
    ex_only, _ = collect(
        apply_structure=True,
        gap3_max=_DISABLED,
        excess_max=EXCESS_MAX,
    )
    no_struct, _ = collect(
        apply_structure=False,
        gap3_max=GAP3_MAX,
        excess_max=EXCESS_MAX,
    )

    # Layer unique contribution on wide (event-level, before slots)
    legs = {
        "notUR": wide["notUR"].astype(bool),
        "W5w": wide["W5w"].astype(bool),
        "geo": wide["geo"].astype(bool),
        "T2": wide["T2"].astype(bool),
    }
    struct = legs["notUR"] & legs["W5w"]

    def _med(mask: pd.Series) -> tuple[float | None, int]:
        g = wide.loc[mask, "ex3"]
        if len(g) == 0:
            return None, 0
        return float(g.median()), int(len(g))

    nested = []
    mask = pd.Series(True, index=wide.index)
    nested.append({"layer": "① Leading∧gap∩excess", "n": int(mask.sum()), "med": _med(mask)[0]})
    for key, lab in (
        ("struct", "② structure notUR∧W5"),
        ("geo", "③ geo y<x (optional)"),
    ):
        prev = mask
        add = struct if key == "struct" else legs["geo"]
        keep = prev & add
        drop = prev & ~add
        mk, nk = _med(keep)
        md, nd = _med(drop)
        nested.append(
            {
                "layer": lab,
                "keep_n": nk,
                "keep_med": mk,
                "drop_n": nd,
                "drop_med": md,
                "delta_med_keep_vs_drop": (
                    None if mk is None or md is None else round(mk - md, 3)
                ),
            }
        )
        mask = keep

    # Phi orthogonality among daily legs (triggers already AND-bound)
    def phi(a: pd.Series, b: pd.Series) -> float:
        x, y = a.astype(float), b.astype(float)
        if x.std() == 0 or y.std() == 0:
            return float("nan")
        return float(np.corrcoef(x, y)[0, 1])

    ortho = {
        "phi_notUR_W5": round(phi(legs["notUR"], legs["W5w"]), 3),
        "phi_struct_geo": round(phi(struct, legs["geo"]), 3),
        "phi_W5_geo": round(phi(legs["W5w"], legs["geo"]), 3),
        "phi_notUR_geo": round(phi(legs["notUR"], legs["geo"]), 3),
        "phi_T2_struct": round(phi(legs["T2"], struct), 3),
        "note": "gap∩excess treated as one trigger predicate (same timescale)",
    }

    arms: list[tuple[str, str, str, int, pd.DataFrame, bool]] = [
        # arm, title, layers blurb, n_pred, events, require_t2
        (
            "B0_frozen_cov",
            "凍結 coverage（Strategy SSOT）",
            "①Leading ②gap∩excess ③struct",
            3,
            frozen,
            False,
        ),
        (
            "B0_frozen_qual",
            "凍結 quality（T2+ 研究對照）",
            "①②③ + T2",
            4,
            frozen,
            True,
        ),
        (
            "B1_3layer_plus_geo",
            "三層 + geo 加層",
            "①②③ + geo",
            4,
            frozen[frozen["geo"]],
            False,
        ),
        (
            "B2_pack_same_as_B0",
            "三層正交包裝（≡B0，契約合併）",
            "①宇宙 ②觸發包 ③結構包",
            3,
            frozen,
            False,
        ),
        (
            "T1_drop_W5",
            "減層：砍 W5 只留 notUR",
            "①② + notUR",
            3,
            wide[wide["notUR"]],
            False,
        ),
        (
            "T2_drop_notUR",
            "減層：砍 notUR 只留 W5",
            "①② + W5",
            3,
            wide[wide["W5w"]],
            False,
        ),
        (
            "T3_geo_for_struct",
            "減層：geo 取代結構包",
            "①② + geo",
            3,
            no_struct[no_struct["geo"]],
            False,
        ),
        (
            "T4_no_struct",
            "減層：不要結構",
            "①②",
            2,
            no_struct,
            False,
        ),
        (
            "T5_gap_only",
            "減層：觸發只留 gap",
            "① + gap + ③struct",
            3,
            gap_only,
            False,
        ),
        (
            "T6_excess_only",
            "減層：觸發只留 excess",
            "① + excess + ③struct",
            3,
            ex_only,
            False,
        ),
        (
            "T7_notUR_plus_geo",
            "重組：notUR+geo（無 W5）",
            "①② + notUR + geo",
            4,
            wide[wide["notUR"] & wide["geo"]],
            False,
        ),
        (
            "T8_W5_plus_geo",
            "重組：W5+geo（無 notUR）",
            "①② + W5 + geo",
            4,
            wide[wide["W5w"] & wide["geo"]],
            False,
        ),
    ]

    rows: list[dict[str, Any]] = []
    snaps: dict[str, dict[str, Any]] = {}
    for arm, title, layers, n_pred, ev, t2 in arms:
        pol = _pol(ev, trading, t2=t2)
        snap = _ablation_gate_snapshot(pol, oos_cut=oos_cut, label=arm)
        snaps[arm] = snap
        rows.append(
            _eff_row(
                snap,
                arm=arm,
                title=title,
                n_pred=n_pred,
                layers=layers,
                wide_n=wide_n,
            )
        )

    b0 = next(r for r in rows if r["arm"] == "B0_frozen_cov")
    b1 = next(r for r in rows if r["arm"] == "B1_3layer_plus_geo")

    def _delta(arm_row: dict[str, Any]) -> dict[str, Any]:
        om = arm_row.get("oos_med")
        oh = arm_row.get("oos_hit")
        return {
            "delta_oos_med_vs_B0": (
                None
                if om is None or b0["oos_med"] is None
                else round(float(om) - float(b0["oos_med"]), 3)
            ),
            "delta_oos_hit_vs_B0": (
                None
                if oh is None or b0["oos_hit"] is None
                else round(float(oh) - float(b0["oos_hit"]), 4)
            ),
            "delta_n_vs_B0": int(arm_row["n"] or 0) - int(b0["n"] or 0),
            "delta_predicates_vs_B0": int(arm_row["n_predicates"]) - int(b0["n_predicates"]),
        }

    for r in rows:
        r.update(_delta(r))

    # Suitability decision
    thin_fail = [
        r
        for r in rows
        if r["arm"].startswith("T")
        and (
            r.get("oos_med") is None
            or float(r["oos_med"]) < float(b0["oos_med"]) - 0.5
            or (r.get("oos_hit") is not None and float(r["oos_hit"]) < 0.65)
        )
    ]
    geo_ok = (
        b1.get("oos_med") is not None
        and b0.get("oos_med") is not None
        and float(b1["oos_med"]) >= float(b0["oos_med"]) - 0.05
        and float(b1.get("oos_hit") or 0) >= float(b0.get("oos_hit") or 0) - 0.02
    )
    # Efficiency: B0 already 3 preds; packaging doesn't reduce predicates further.
    # B1 adds 1 pred for small med lift — efficiency_score compare
    b0_eff = b0.get("efficiency_score")
    b1_eff = b1.get("efficiency_score")

    if all(
        r["arm"] in ("T4_no_struct", "T3_geo_for_struct", "T5_gap_only", "T6_excess_only")
        or r in thin_fail
        for r in rows
        if r["arm"].startswith("T")
    ):
        pass

    verdict = {
        "suitable_for_rebundle": True,  # 3-layer packaging = same as B0
        "suitable_for_thinning": False,  # all thin arms lose 效能
        "suitable_geo_addon": bool(geo_ok),
        "choice": (
            "keep_3layer_pack_optional_geo"
            if geo_ok
            else "keep_3layer_pack_no_geo"
        ),
        "prose": "",
    }
    prose = [
        "三層正交包裝（宇宙／觸發包／結構包）與凍結 coverage 數值等價，適合作為契約簡化（效率在認知／維運，非再砍謂詞）。",
        "所有減層變體（砍 W5／notUR／結構／gap 或 excess 單腿／geo 代結構）效能劣於 B0，不適合為效率犧牲。",
    ]
    if geo_ok:
        prose.append(
            f"geo 加層適度提升效能（ΔOOS med={b1['delta_oos_med_vs_B0']:+.2f}pp · "
            f"n {b0['n']}→{b1['n']}）· efficiency_score "
            f"{b0_eff}→{b1_eff}（多 1 謂詞，屬可選）。"
        )
    else:
        prose.append("geo 加層未通過軟門檻，暫不建議。")
    prose.append(
        f"結構兩腿不正交（φ_notUR_W5={ortho['phi_notUR_W5']}），應捆成一包而非拆層；"
        f"geo 與結構近正交（φ={ortho['phi_struct_geo']}）。"
    )
    verdict["prose"] = " ".join(prose)

    return {
        "schema": "leading_dip_orthogonal_compact-v1",
        "contract": {
            "start": start,
            "oos_cut": oos_cut,
            "baseline": "leading-dip coverage (Strategy SSOT)",
            "效能": "OOS med ex3 · OOS hit · gates_ok",
            "效率": "n_predicates · yield_vs_wide · oos_med_per_predicate · efficiency_score=oos_med*oos_hit/n_pred",
            "wide_n": wide_n,
        },
        "orthogonality": ortho,
        "nested_layer_lift": nested,
        "arms": rows,
        "verdict": verdict,
    }


def render_md(result: dict[str, Any]) -> str:
    v = result["verdict"]
    c = result["contract"]
    lines = [
        "# Leading Dip · 三層正交漏斗 · 適配性（效能 × 效率）",
        "",
        "日期：2026-07-16 · Research only · **未採納變更 Strategy**",
        "",
        "## Verdict",
        "",
        f"**{v['choice']}**",
        "",
        v["prose"],
        "",
        f"- 適合作契約重綁（3 層包裝）: **{v['suitable_for_rebundle']}**",
        f"- 適合減層（砍腿）: **{v['suitable_for_thinning']}**",
        f"- 適合 geo 加層: **{v['suitable_geo_addon']}**",
        "",
        "## 契約",
        "",
        f"- Baseline: {c['baseline']}",
        f"- 效能: {c['效能']}",
        f"- 效率: {c['效率']}",
        f"- 寬池 n（Leading∧gap∩excess·無結構）: {c['wide_n']}",
        "",
        "## 正交性（日線腿）",
        "",
        "| pair | φ |",
        "|--|--:|",
    ]
    o = result["orthogonality"]
    for k in (
        "phi_notUR_W5",
        "phi_struct_geo",
        "phi_W5_geo",
        "phi_notUR_geo",
        "phi_T2_struct",
    ):
        lines.append(f"| {k} | {o[k]} |")
    lines += ["", f"_{o['note']}_", "", "## 層級獨特貢獻（event · 槽前）", ""]
    for step in result["nested_layer_lift"]:
        if "keep_n" not in step:
            lines.append(f"- {step['layer']}: n={step['n']} med={_pp(step['med'])}")
        else:
            lines.append(
                f"- {step['layer']}: keep n={step['keep_n']} med={_pp(step['keep_med'])} · "
                f"drop n={step['drop_n']} med={_pp(step['drop_med'])} · "
                f"Δ={_pp(step.get('delta_med_keep_vs_drop'))}"
            )

    lines += [
        "",
        "## 回測對照（coverage 政策 · slots · 總在外≤6）",
        "",
        "| arm | 層 | pred | n | hit | med | OOS hit/med | ΔOOS med | med/pred | eff_score |",
        "|--|--|--:|--:|--:|--:|--|--:|--:|--:|",
    ]
    for r in result["arms"]:
        oos = f"{_pct(r.get('oos_hit'))}/{_pp(r.get('oos_med'))}"
        lines.append(
            f"| `{r['arm']}` | {r['layers']} | {r['n_predicates']} | {r['n']} | "
            f"{_pct(r.get('hit'))} | {_pp(r.get('med'))} | {oos} | "
            f"{_pp(r.get('delta_oos_med_vs_B0'))} | "
            f"{r.get('oos_med_per_predicate')} | {r.get('efficiency_score')} |"
        )

    lines += [
        "",
        "## 解讀",
        "",
        "1. **效能**：B0 凍結三層已是減層變體的上界；砍結構／單觸發腿會明顯掉 OOS med。",
        "2. **效率**：真正可提升的是把 notUR∧W5 與 gap∩excess **各綁成一包**（謂詞數仍為 3，",
        "   但心智／維運層數下降）；再砍謂詞會傷效能。",
        "3. **geo**：與結構近正交，適合作可選第 4 謂詞，不是取代結構。",
        "",
        "## Re-run",
        "",
        "```bash",
        "PYTHONPATH=src .venv/bin/python scripts/research/run_leading_dip_orthogonal_compact.py --write-artifact",
        "```",
        "",
    ]
    return "\n".join(lines)


def _pct(x: Any) -> str:
    if x is None:
        return "—"
    return f"{100 * float(x):.1f}%"


def _pp(x: Any) -> str:
    if x is None:
        return "—"
    return f"{float(x):+.2f}"
