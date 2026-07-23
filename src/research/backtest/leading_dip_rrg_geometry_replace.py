"""Leading Dip · RRG geometry as screener · per-funnel-leg replace study.

Research-only. Tests whether T−1 close RRG linear KEEP
``x>100 ∧ y>100 ∧ y<x`` (equiv. Leading ∧ below y=x) can **replace** each
frozen funnel leg (notUR / W5 / structure / gap / excess / T2 / Leading
universe), versus mere drop and versus frozen baseline.

Track SSOT for comparison: coverage policy (drop T2 · slots · total-open≤6),
matching Strategy ``leading-dip``. Quality (T2+) reported as secondary.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from typing import Any, Callable

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

GEO_ID = "G_lin_below_yx"
# Disable a trigger leg inside first_hit (gap≤ thr / excess≤ thr).
_DISABLED_THR = 100.0
# Soft replace gates vs frozen (coverage track).
_MED_SLACK_PP = 0.25
_HIT_FLOOR = 0.70
_N_RATIO_MIN = 0.50


@dataclass(frozen=True)
class GeoScreen:
    """T−1 close RRG geometry screen."""

    id: str
    label: str

    def mask(self, df: pd.DataFrame) -> pd.Series:
        rv = df["rv"].astype(float)
        mv = df["mv"].astype(float)
        if self.id == "G_lin_below_yx":
            return (rv > 100.0) & (mv > 100.0) & (mv < rv)
        if self.id == "G_wedge_right":
            # Leading∪shallow-Weakening between y=x and y=-x
            return (rv > 100.0) & (mv > (200.0 - rv)) & (mv < rv)
        if self.id == "G_lin_dist":
            # below y=x ∧ outside soft radius (IS-calibrated later via column)
            r = float(df.attrs.get("geo_R", 7.0))
            dist2 = (rv - 100.0) ** 2 + (mv - 100.0) ** 2
            return (rv > 100.0) & (mv > 100.0) & (mv < rv) & (dist2 >= r * r)
        raise KeyError(self.id)


SCREENS = (
    GeoScreen("G_lin_below_yx", "Leading ∧ y<x（線性 KEEP）"),
    GeoScreen("G_wedge_right", "右側扇形 RV>100 ∧ |Y|<X"),
)


def attach_t1_rrg(con: sqlite3.Connection, events: pd.DataFrame) -> pd.DataFrame:
    """Join PIT T−1 close RRG onto events (baseline < trade date)."""
    if events is None or len(events) == 0:
        return events.copy() if events is not None else pd.DataFrame()
    scores = pd.read_sql(
        """
        SELECT data_baseline_date AS baseline, stock_id AS sid,
               rs_ratio AS rv, rs_momentum AS mv, quadrant
        FROM rrg_universe_scores
        WHERE screen_kind='close'
        """,
        con,
    )
    scores["sid"] = scores["sid"].astype(str)
    scores["baseline"] = scores["baseline"].astype(str)
    rows: list[dict[str, Any]] = []
    for r in events.itertuples(index=False):
        d = str(r.date)
        sid = str(r.sid)
        sub = scores[(scores.sid == sid) & (scores.baseline < d)]
        if sub.empty:
            continue
        s = sub.sort_values("baseline").iloc[-1]
        rv, mv = float(s.rv), float(s.mv)
        base = {c: getattr(r, c) for c in events.columns}
        base.update(
            {
                "rv": rv,
                "mv": mv,
                "quadrant": s.quadrant,
                "geo_below_yx": bool(rv > 100.0 and mv > 100.0 and mv < rv),
                "geo_wedge": bool(rv > 100.0 and mv > (200.0 - rv) and mv < rv),
                "geo_dist": float(math.hypot(rv - 100.0, mv - 100.0)),
            }
        )
        rows.append(base)
    out = pd.DataFrame(rows)
    if len(out):
        # IS calibrate soft R on frozen-like Leading∧below_yx distances
        is_keep = out[(out["half"] == "IS") & out["geo_below_yx"]]
        if len(is_keep) >= 10:
            out.attrs["geo_R"] = float(np.percentile(is_keep["geo_dist"], 33))
        else:
            out.attrs["geo_R"] = 7.0
    return out


def _snap(picks: pd.DataFrame, *, oos_cut: str, label: str) -> dict[str, Any]:
    return _ablation_gate_snapshot(picks, oos_cut=oos_cut, label=label)


def _apply_geo(df: pd.DataFrame, screen: GeoScreen) -> pd.DataFrame:
    if df is None or len(df) == 0:
        return df.copy() if df is not None else pd.DataFrame()
    if screen.id == "G_lin_below_yx":
        m = df["geo_below_yx"].astype(bool)
    elif screen.id == "G_wedge_right":
        m = df["geo_wedge"].astype(bool)
    elif screen.id == "G_lin_dist":
        r = float(df.attrs.get("geo_R", 7.0))
        m = df["geo_below_yx"].astype(bool) & (df["geo_dist"].astype(float) >= r)
    else:
        m = screen.mask(df)
    return df.loc[m].copy()


def _policy(
    events: pd.DataFrame,
    trading: list[str],
    *,
    require_t2: bool,
) -> pd.DataFrame:
    if events is None or len(events) == 0:
        return pd.DataFrame()
    return apply_frozen_policy(
        events, trading, require_t2=require_t2, max_open=TOTAL_OPEN_CAP
    )


def _verdict(
    frozen: dict[str, Any],
    drop: dict[str, Any] | None,
    replace: dict[str, Any],
) -> dict[str, Any]:
    """Classify whether geometry can replace the stage on coverage metrics."""
    f_med = frozen.get("oos_med")
    f_hit = frozen.get("oos_hit")
    f_n = int(frozen.get("n") or 0)
    r_med = replace.get("oos_med")
    r_hit = replace.get("oos_hit")
    r_n = int(replace.get("n") or 0)
    reasons: list[str] = []

    if r_n < 15 or replace.get("oos_n", 0) < 15:
        return {
            "label": "insufficient_n",
            "replace_ok": False,
            "reasons": ["replace OOS/n too small"],
        }
    if f_n > 0 and r_n / f_n < _N_RATIO_MIN:
        reasons.append(f"n collapse {r_n}/{f_n} < {_N_RATIO_MIN}")

    med_ok = (
        f_med is not None
        and r_med is not None
        and float(r_med) >= float(f_med) - _MED_SLACK_PP
    )
    hit_floor = max(_HIT_FLOOR, float(f_hit) - 0.02) if f_hit is not None else _HIT_FLOOR
    hit_ok = r_hit is not None and float(r_hit) >= hit_floor
    if not med_ok:
        reasons.append(
            f"oos_med {r_med} < frozen {f_med} − {_MED_SLACK_PP}"
        )
    if not hit_ok:
        reasons.append(f"oos_hit {r_hit} < floor {hit_floor:.2f}")

    beats_drop = True
    if drop is not None and drop.get("oos_med") is not None and r_med is not None:
        # Geometry should not lose to mere drop by >0.5pp med
        if float(r_med) + 0.5 < float(drop["oos_med"]):
            beats_drop = False
            reasons.append(
                f"replace med {r_med} worse than drop {drop.get('oos_med')}"
            )

    replace_ok = bool(med_ok and hit_ok and beats_drop and not any("collapse" in x for x in reasons))
    if replace_ok and f_med is not None and r_med is not None and float(r_med) > float(f_med) + 0.5:
        label = "geo_replaces_and_lifts"
    elif replace_ok:
        label = "geo_can_replace"
    elif med_ok and hit_ok and not beats_drop:
        label = "drop_enough_geo_redundant"
    else:
        label = "cannot_replace"

    return {
        "label": label,
        "replace_ok": replace_ok,
        "reasons": reasons,
        "delta_oos_med_vs_frozen": (
            None
            if f_med is None or r_med is None
            else round(float(r_med) - float(f_med), 3)
        ),
        "delta_oos_hit_vs_frozen": (
            None
            if f_hit is None or r_hit is None
            else round(float(r_hit) - float(f_hit), 4)
        ),
    }


def run_geometry_replace_study(
    con: sqlite3.Connection,
    *,
    start: str = START_DEFAULT,
    oos_cut: str = OOS_CUT_DEFAULT,
    screen_id: str = GEO_ID,
) -> dict[str, Any]:
    screen = next(s for s in SCREENS if s.id == screen_id)

    def collect(**kw: Any) -> tuple[pd.DataFrame, list[str]]:
        raw, trading = collect_leading_dip_events(con, start=start, oos_cut=oos_cut, **kw)
        return attach_t1_rrg(con, raw), trading

    print("  collect frozen …", flush=True)
    frozen_ev, trading = collect(
        apply_structure=True,
        require_not_ur=True,
        require_w5_weak=True,
        gap3_max=GAP3_MAX,
        excess_max=EXCESS_MAX,
        universe_mode="leading",
    )
    geo_R = float(frozen_ev.attrs.get("geo_R", 7.0)) if len(frozen_ev) else 7.0

    pools: dict[str, tuple[pd.DataFrame, list[str]]] = {"frozen": (frozen_ev, trading)}

    specs: list[tuple[str, dict[str, Any]]] = [
        (
            "drop_notUR",
            dict(
                apply_structure=True,
                require_not_ur=False,
                require_w5_weak=True,
                gap3_max=GAP3_MAX,
                excess_max=EXCESS_MAX,
                universe_mode="leading",
            ),
        ),
        (
            "drop_W5",
            dict(
                apply_structure=True,
                require_not_ur=True,
                require_w5_weak=False,
                gap3_max=GAP3_MAX,
                excess_max=EXCESS_MAX,
                universe_mode="leading",
            ),
        ),
        (
            "drop_structure",
            dict(
                apply_structure=False,
                gap3_max=GAP3_MAX,
                excess_max=EXCESS_MAX,
                universe_mode="leading",
            ),
        ),
        (
            "drop_gap",
            dict(
                apply_structure=True,
                require_not_ur=True,
                require_w5_weak=True,
                gap3_max=_DISABLED_THR,
                excess_max=EXCESS_MAX,
                universe_mode="leading",
            ),
        ),
        (
            "drop_excess",
            dict(
                apply_structure=True,
                require_not_ur=True,
                require_w5_weak=True,
                gap3_max=GAP3_MAX,
                excess_max=_DISABLED_THR,
                universe_mode="leading",
            ),
        ),
        (
            "universe_rv20",
            dict(
                apply_structure=True,
                require_not_ur=True,
                require_w5_weak=True,
                gap3_max=GAP3_MAX,
                excess_max=EXCESS_MAX,
                universe_mode="rv20_ge_100",
            ),
        ),
    ]

    for name, kw in specs:
        print(f"  collect {name} …", flush=True)
        pools[name] = collect(**kw)

    def cov(ev: pd.DataFrame, tr: list[str]) -> pd.DataFrame:
        return _policy(ev, tr, require_t2=False)

    def qual(ev: pd.DataFrame, tr: list[str]) -> pd.DataFrame:
        return _policy(ev, tr, require_t2=True)

    frozen_cov = cov(frozen_ev, trading)
    frozen_qual = qual(frozen_ev, trading)
    base_cov = _snap(frozen_cov, oos_cut=oos_cut, label="frozen_cov")
    base_qual = _snap(frozen_qual, oos_cut=oos_cut, label="frozen_qual")

    # Additive geo on frozen (not a replace)
    add_cov = _snap(
        cov(_apply_geo(frozen_ev, screen), trading),
        oos_cut=oos_cut,
        label="frozen_plus_geo_cov",
    )
    add_qual = _snap(
        qual(_apply_geo(frozen_ev, screen), trading),
        oos_cut=oos_cut,
        label="frozen_plus_geo_qual",
    )

    stages: list[dict[str, Any]] = []

    def stage(
        stage_id: str,
        title: str,
        *,
        drop_pool: str | None,
        replace_pool: str,
        geo: GeoScreen = screen,
        note: str = "",
        t2_special: bool = False,
    ) -> None:
        """Build drop / replace arms for one funnel leg."""
        if t2_special:
            # T2 is policy-only: drop = coverage; replace = all-day events ∩ geo → cov policy
            # (geo cannot encode clock; test whether geo ⊆ quality of T2 filter)
            drop_s = base_cov
            # all-day structure events + geo, then slots without T2 already in cov
            # Compare: quality frozen vs (all-day ∩ geo) under cov policy
            repl_ev = _apply_geo(frozen_ev, geo)
            repl_s = _snap(cov(repl_ev, trading), oos_cut=oos_cut, label=f"replace_{stage_id}")
            # "drop T2" is coverage; replace uses geo on full day then cov slots
            # Fairer secondary: quality vs (T2 events ∩ geo) 
            t2_geo = _snap(
                qual(_apply_geo(frozen_ev, geo), trading),
                oos_cut=oos_cut,
                label=f"qual_plus_geo_{stage_id}",
            )
            v = _verdict(base_qual, drop_s, repl_s)
            # For T2: also check if geo on all-day recovers quality
            v_alt = _verdict(base_qual, drop_s, t2_geo)
            # Relabel: geometry cannot encode clock; success = cov∩geo ≈ quality
            if v.get("replace_ok"):
                v = {
                    **v,
                    "label": "geo_recovers_quality_on_cov",
                    "replace_ok": False,  # not a true funnel-leg replace
                    "reasons": list(v.get("reasons") or [])
                    + ["T2 is clock; geo cleans cov toward qual — not a leg swap"],
                }
            stages.append(
                {
                    "stage_id": stage_id,
                    "title": title,
                    "note": note,
                    "track": "T2_special",
                    "frozen": base_qual,
                    "drop": drop_s,
                    "replace": repl_s,
                    "replace_qual_plus_geo": t2_geo,
                    "verdict": v,
                    "verdict_qual_plus_geo": v_alt,
                }
            )
            return

        d_ev, d_tr = pools[drop_pool] if drop_pool else (frozen_ev, trading)
        r_ev, r_tr = pools[replace_pool]
        drop_s = (
            _snap(cov(d_ev, d_tr), oos_cut=oos_cut, label=f"drop_{stage_id}")
            if drop_pool
            else None
        )
        repl_s = _snap(
            cov(_apply_geo(r_ev, geo), r_tr),
            oos_cut=oos_cut,
            label=f"replace_{stage_id}",
        )
        # For universe stage use wedge screen on rv20 pool
        v = _verdict(base_cov, drop_s, repl_s)
        stages.append(
            {
                "stage_id": stage_id,
                "title": title,
                "note": note,
                "track": "coverage",
                "frozen": base_cov,
                "drop": drop_s,
                "replace": repl_s,
                "verdict": v,
            }
        )

    stage(
        "notUR",
        "T−1 W20 trend ≠ up_right（notUR）",
        drop_pool="drop_notUR",
        replace_pool="drop_notUR",
        note="結構腿 · 與 W20 軌跡方向有關；幾何 y<x 可能部分重疊",
    )
    stage(
        "W5",
        "T−1 W5_MV < 100（W5w）",
        drop_pool="drop_W5",
        replace_pool="drop_W5",
        note="短窗動量弱 · 與 W20 平面幾何不同時標",
    )
    stage(
        "structure",
        "結構核 notUR ∧ W5w（整包）",
        drop_pool="drop_structure",
        replace_pool="drop_structure",
        note="兩結構腿一次拿掉，改由幾何接手",
    )
    stage(
        "gap3",
        "觸發 gap3 ≤ −0.80",
        drop_pool="drop_gap",
        replace_pool="drop_gap",
        note="盤中 RS/WMA3 深度 · 日頻 RRG 幾何通常無法替代",
    )
    stage(
        "excess",
        "觸發 excess ≤ −4",
        drop_pool="drop_excess",
        replace_pool="drop_excess",
        note="盤中相對大盤超跌 · 日頻幾何通常無法替代",
    )
    stage(
        "universe",
        "宇宙 Leading → RV≥100 + 右側扇形",
        drop_pool="universe_rv20",
        replace_pool="universe_rv20",
        geo=GeoScreen("G_wedge_right", "右側扇形"),
        note="放寬到 RV≥100 後用扇形收回；對照先前 Weakening 稀釋結論",
    )
    stage(
        "T2",
        "品質軌 T2（minute>09:30）",
        drop_pool=None,
        replace_pool="frozen",
        t2_special=True,
        note="時鐘濾網；幾何不能編碼時刻。測：全日∩geo 能否追上 T2+ 品質",
    )

    # Summary table rows (coverage primary)
    table: list[dict[str, Any]] = []
    for st in stages:
        if st["track"] == "T2_special":
            fr, dr, rp = st["frozen"], st["drop"], st["replace"]
            table.append(
                {
                    "stage": st["stage_id"],
                    "title": st["title"],
                    "arm": "frozen_qual",
                    **{k: fr.get(k) for k in ("n", "hit", "med", "oos_n", "oos_hit", "oos_med", "gates_ok")},
                }
            )
            table.append(
                {
                    "stage": st["stage_id"],
                    "title": st["title"],
                    "arm": "drop_T2(=cov)",
                    **{k: dr.get(k) for k in ("n", "hit", "med", "oos_n", "oos_hit", "oos_med", "gates_ok")},
                }
            )
            table.append(
                {
                    "stage": st["stage_id"],
                    "title": st["title"],
                    "arm": "allday∩geo→cov",
                    **{k: rp.get(k) for k in ("n", "hit", "med", "oos_n", "oos_hit", "oos_med", "gates_ok")},
                    "verdict": st["verdict"]["label"],
                }
            )
            continue
        for arm_name, snap in (
            ("frozen_cov", st["frozen"]),
            ("drop", st["drop"]),
            ("replace_geo", st["replace"]),
        ):
            if snap is None:
                continue
            row = {
                "stage": st["stage_id"],
                "title": st["title"],
                "arm": arm_name,
                **{k: snap.get(k) for k in ("n", "hit", "med", "oos_n", "oos_hit", "oos_med", "gates_ok")},
            }
            if arm_name == "replace_geo":
                row["verdict"] = st["verdict"]["label"]
                row["delta_oos_med"] = st["verdict"].get("delta_oos_med_vs_frozen")
            table.append(row)

    replaceable = [
        s["stage_id"]
        for s in stages
        if s.get("verdict", {}).get("replace_ok")
    ]

    return {
        "schema": "leading_dip_rrg_geometry_replace-v1",
        "contract": {
            "start": start,
            "oos_cut": oos_cut,
            "screen": screen.id,
            "screen_label": screen.label,
            "geo_R_is_p33": geo_R,
            "baseline_track": "coverage (leading-dip Strategy)",
            "geometry": "T-1 close · x=RV y=MV · KEEP x>100 ∧ y>100 ∧ y<x",
            "med_slack_pp": _MED_SLACK_PP,
            "hit_floor": _HIT_FLOOR,
        },
        "baseline": {"coverage": base_cov, "quality": base_qual},
        "additive_geo_on_frozen": {"coverage": add_cov, "quality": add_qual},
        "stages": stages,
        "table": table,
        "replaceable_stages": replaceable,
        "verdict": _study_verdict(stages, add_cov, base_cov),
    }


def _study_verdict(
    stages: list[dict[str, Any]],
    add_cov: dict[str, Any],
    base_cov: dict[str, Any],
) -> dict[str, Any]:
    ok = [s for s in stages if s.get("verdict", {}).get("replace_ok")]
    fail = [
        s
        for s in stages
        if not s.get("verdict", {}).get("replace_ok") and s["stage_id"] != "T2"
    ]
    t2 = next((s for s in stages if s["stage_id"] == "T2"), None)
    add_lift = None
    if add_cov.get("oos_med") is not None and base_cov.get("oos_med") is not None:
        add_lift = float(add_cov["oos_med"]) - float(base_cov["oos_med"])
    prose_parts = []
    if ok:
        prose_parts.append(
            "幾何可替代（coverage 軟門檻）：" + ", ".join(s["stage_id"] for s in ok)
        )
    else:
        prose_parts.append("無結構／觸發／宇宙腿被幾何完整替代")
    if add_lift is not None:
        prose_parts.append(
            f"凍結上「加」幾何 ΔOOS med={add_lift:+.2f}pp "
            f"(n {base_cov.get('n')}→{add_cov.get('n')})"
        )
    if t2 is not None and t2["verdict"].get("label") == "geo_recovers_quality_on_cov":
        prose_parts.append(
            "cov∩geo 可接近 quality(T2+) 的 OOS med（幾何當 coverage 清潔劑，非取代時鐘）"
        )
    trig = [s for s in stages if s["stage_id"] in ("gap3", "excess")]
    if all(not s["verdict"]["replace_ok"] for s in trig):
        prose_parts.append("gap／excess 盤中觸發不可被日頻 RRG 幾何取代")
    choice = (
        "geometry_as_addon_not_funnel_replace"
        if not ok and add_lift is not None and add_lift >= 0
        else ("partial_replace" if ok else "no_replace")
    )
    return {
        "replaceable": [s["stage_id"] for s in ok],
        "not_replaceable": [s["stage_id"] for s in fail],
        "additive_delta_oos_med": None if add_lift is None else round(add_lift, 3),
        "prose": " · ".join(prose_parts),
        "choice": choice,
    }


def render_md(result: dict[str, Any]) -> str:
    c = result["contract"]
    v = result["verdict"]
    lines = [
        "# Leading Dip · RRG 圖形篩選器替代漏斗腿",
        "",
        "日期：2026-07-16 · Research only · **未採納**",
        "",
        "## Verdict",
        "",
        f"**{v['choice']}** — {v['prose']}",
        "",
        f"- 幾何：`{c['screen']}` · {c['screen_label']}",
        f"- 對照軌：{c['baseline_track']}",
        f"- 軟門檻：OOS med ≥ frozen−{_MED_SLACK_PP}pp · hit≥max(70%, frozen−2pp) · n≥50% frozen",
        "",
        "## 漏斗腿 × 三臂（coverage）",
        "",
        "| stage | arm | n | hit | med | OOS n/hit/med | gates | verdict |",
        "|--|--|--:|--:|--:|--|--|--|",
    ]
    for row in result["table"]:
        if row["stage"] == "T2":
            continue
        oos = (
            f"{row.get('oos_n')}/"
            f"{_pct(row.get('oos_hit'))}/"
            f"{_pp(row.get('oos_med'))}"
        )
        lines.append(
            f"| {row['stage']} | {row['arm']} | {row.get('n')} | {_pct(row.get('hit'))} | "
            f"{_pp(row.get('med'))} | {oos} | {row.get('gates_ok')} | {row.get('verdict', '')} |"
        )

    lines += [
        "",
        "## T2 特例（品質軌）",
        "",
        "| arm | n | hit | med | OOS n/hit/med | verdict |",
        "|--|--:|--:|--:|--|--|",
    ]
    for row in result["table"]:
        if row["stage"] != "T2":
            continue
        oos = (
            f"{row.get('oos_n')}/{_pct(row.get('oos_hit'))}/{_pp(row.get('oos_med'))}"
        )
        lines.append(
            f"| {row['arm']} | {row.get('n')} | {_pct(row.get('hit'))} | "
            f"{_pp(row.get('med'))} | {oos} | {row.get('verdict', '')} |"
        )

    lines += [
        "",
        "## 凍結上加幾何（非替代）",
        "",
    ]
    add = result["additive_geo_on_frozen"]
    base = result["baseline"]
    lines.append(
        f"- coverage：frozen n={base['coverage'].get('n')} med={_pp(base['coverage'].get('med'))} "
        f"OOS med={_pp(base['coverage'].get('oos_med'))} → "
        f"+geo n={add['coverage'].get('n')} med={_pp(add['coverage'].get('med'))} "
        f"OOS med={_pp(add['coverage'].get('oos_med'))} "
        f"(ΔOOS={v.get('additive_delta_oos_med')})"
    )
    lines.append(
        f"- quality：frozen n={base['quality'].get('n')} OOS med={_pp(base['quality'].get('oos_med'))} → "
        f"+geo n={add['quality'].get('n')} OOS med={_pp(add['quality'].get('oos_med'))}"
    )

    lines += [
        "",
        "## 逐腿解讀",
        "",
    ]
    for st in result["stages"]:
        vd = st["verdict"]
        lines.append(f"### `{st['stage_id']}` · {st['title']}")
        lines.append("")
        lines.append(f"- 判定：**{vd['label']}** · replace_ok={vd['replace_ok']}")
        if st.get("note"):
            lines.append(f"- 註：{st['note']}")
        if vd.get("reasons"):
            lines.append(f"- 原因：{'; '.join(vd['reasons'])}")
        if vd.get("delta_oos_med_vs_frozen") is not None:
            lines.append(
                f"- ΔOOS med vs frozen：{vd['delta_oos_med_vs_frozen']:+.3f}pp · "
                f"ΔOOS hit：{vd.get('delta_oos_hit_vs_frozen')}"
            )
        lines.append("")

    lines += [
        "## 幾何定義（KEEP）",
        "",
        "```text",
        "x = RS-Ratio (RV),  y = RS-Momentum (MV)   # T−1 close PIT",
        "KEEP:  x > 100  ∧  y > 100  ∧  y < x",
        "EXCLUDE (within Leading):  y ≥ x",
        "```",
        "",
        "## Re-run",
        "",
        "```bash",
        "PYTHONPATH=src .venv/bin/python scripts/research/run_leading_dip_rrg_geometry_replace.py",
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
