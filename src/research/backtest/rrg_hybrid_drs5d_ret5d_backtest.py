"""Hybrid dual-WMA research · WMA(20) drs_5d + WMA(5) extension · separate ret_5d track.

Design:
  - Pool / structural outcome: WMA(20) · drs_5d
  - Ranking bonus: WMA(5) spread_rs > 0 (extension at daily close)
  - Price outcome: ret_5d modeled separately (WMA5 momentum + bench + fundamentals)
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from market_benchmark import load_benchmark_close
from research.backtest.dual_wma_drs3d_intraday_backtest import _matches_imp_extension_strict
from research.backtest.finpilot_local_backtest import load_fundamental_snapshot, load_price_panels
from research.backtest.rrg_drs5d_score_backtest import (
    HOLD_DAYS,
    TUNED_KINEMATIC_WEIGHTS_5D,
    WF_TEST_YEARS,
    WF_TRAIN_YEARS,
    _DRS_COL,
    _RET_COL,
    build_drs5d_panel,
    build_year_walk_forward_folds,
    compute_drs5d_score_kinematic,
    daily_spearman_ic_series,
    evaluate_score_deciles,
    pooled_spearman_ic,
)
from research.backtest.tw100_walk_forward import _ic_stats
from project_config import DEFAULT_ETF_CODES
from rrg_universe_intraday_panel import build_dual_wma_intraday_trajectories
from rrg_rotation import compute_rrg_panel
from rrg_universe_intraday_panel import WMA_LONG_LENGTH, WMA_SHORT_LENGTH, classify_wma_spread
from stock_db import PROJECT_ROOT

W_EXTENSION_BONUS = 0.25
W_INTRADAY_EXTENSION_BONUS = 0.15
INTRADAY_SAMPLE_FROM = "2024-01-01"
INTRADAY_POLL_MINUTES = ("13:30",)


def attach_daily_dual_wma_spread(panel: pd.DataFrame, conn: Any) -> pd.DataFrame:
    """Daily close · WMA(5) vs WMA(20) spread_rs / spread_class."""
    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn)
    rs5, mom5, _ = compute_rrg_panel(close, bench, length=WMA_SHORT_LENGTH)
    rs20, mom20, _ = compute_rrg_panel(close, bench, length=WMA_LONG_LENGTH)

    spread_rs_l: list[float] = []
    spread_mom_l: list[float] = []
    spread_dist_l: list[float] = []
    spread_class_l: list[str] = []
    w5_rsr_l: list[float] = []
    w5_dr1_l: list[float] = []

    full_dates = close.index.astype(str).tolist()
    date_idx = {d: i for i, d in enumerate(full_dates)}

    for _, row in panel.iterrows():
        d, sid = str(row["date"]), str(row["sid"])
        if sid not in rs5.columns or d not in rs5.index:
            spread_rs_l.append(float("nan"))
            spread_mom_l.append(float("nan"))
            spread_dist_l.append(float("nan"))
            spread_class_l.append("")
            w5_rsr_l.append(float("nan"))
            w5_dr1_l.append(float("nan"))
            continue
        rv5 = float(rs5.at[d, sid])
        mv5 = float(mom5.at[d, sid])
        rv20 = float(rs20.at[d, sid])
        mv20 = float(mom20.at[d, sid])
        sc, dist, srs, sms = classify_wma_spread(rv5, mv5, rv20, mv20)
        spread_rs_l.append(srs)
        spread_mom_l.append(sms)
        spread_dist_l.append(dist)
        spread_class_l.append(sc)
        w5_rsr_l.append(rv5)
        si = date_idx.get(d)
        dr1 = float("nan")
        if si is not None and si >= 1:
            dp = full_dates[si - 1]
            rv_p = float(rs5.at[dp, sid])
            if np.isfinite(rv_p) and np.isfinite(rv5):
                dr1 = rv5 - rv_p
        w5_dr1_l.append(dr1)

    out = panel.copy()
    out["spread_rs"] = spread_rs_l
    out["spread_mom"] = spread_mom_l
    out["spread_dist"] = spread_dist_l
    out["spread_class"] = spread_class_l
    out["w5_rsr"] = w5_rsr_l
    out["w5_drs_rsr_1d"] = w5_dr1_l
    out["w5_extension"] = (out["spread_rs"] > 0) & (out["spread_class"] == "extension")
    return out


def attach_pit_fundamentals(panel: pd.DataFrame, conn: Any) -> pd.DataFrame:
    """PIT merge roe / revenue_yoy · merge_asof backward on as_of_date ≤ signal date."""
    fund = load_fundamental_snapshot(conn)
    out = panel.copy()
    out["roe_latest_q"] = float("nan")
    out["revenue_yoy_pct"] = float("nan")
    if fund.empty or panel.empty:
        return out

    fund = fund.copy()
    fund["sid"] = fund["stock_id"].astype(str)
    fund["_pit_date"] = pd.to_datetime(fund["as_of_date"].astype(str), errors="coerce")
    fund = fund.dropna(subset=["_pit_date"]).sort_values(["sid", "_pit_date"])
    if fund.empty:
        return out

    p = out.copy()
    p["sid"] = p["sid"].astype(str)
    p["_pit_date"] = pd.to_datetime(p["date"].astype(str), errors="coerce")
    chunks: list[pd.DataFrame] = []
    fcols = ["_pit_date", "roe_latest_q", "revenue_yoy_pct"]
    for sid, grp in p.groupby("sid", sort=False):
        g = grp.dropna(subset=["_pit_date"]).sort_values("_pit_date")
        g = g.drop(columns=["roe_latest_q", "revenue_yoy_pct"], errors="ignore")
        if g.empty:
            chunks.append(grp)
            continue
        fsub = fund.loc[fund["sid"] == sid, fcols]
        if fsub.empty:
            chunks.append(grp)
            continue
        merged = pd.merge_asof(g, fsub, on="_pit_date", direction="backward")
        chunks.append(merged)

    merged_all = pd.concat(chunks, axis=0)
    merged_all = merged_all.groupby(level=0, sort=False).last()
    out.update(merged_all[["roe_latest_q", "revenue_yoy_pct"]])
    return out


def _bench_tier(bench_ret: float) -> str:
    if not np.isfinite(bench_ret):
        return "unknown"
    if bench_ret >= 2.0:
        return "b2"
    if bench_ret >= 1.0:
        return "b1"
    if bench_ret >= 0.0:
        return "b0"
    return "neg"


def stratify_bench_gating(kin: pd.DataFrame) -> list[dict[str, Any]]:
    """b0 / b1 / b2 / neg · mean outcomes · ret_5d score IC per tier."""
    if "bench_today_ret" not in kin.columns:
        return []
    sub = kin.copy()
    sub["bench_tier"] = sub["bench_today_ret"].map(lambda x: _bench_tier(float(x)))
    rows: list[dict[str, Any]] = []
    for tier in ("b2", "b1", "b0", "neg"):
        g = sub[sub["bench_tier"] == tier]
        if len(g) < 30:
            continue
        ic_ret = _ic_pair(g, "score_ret5d", _RET_COL)
        ic_drs = _ic_pair(g, "score_hybrid", _DRS_COL)
        rows.append(
            {
                "tier": tier,
                "bench_rule": {"b2": "≥+2%", "b1": "≥+1%", "b0": "≥0%", "neg": "<0%"}[tier],
                "n": len(g),
                "mean_ret_5d": round(float(g[_RET_COL].mean()), 4),
                "mean_drs_5d": round(float(g[_DRS_COL].mean()), 4),
                "ic_ret5d_vs_ret": ic_ret["ic"],
                "ic_hybrid_vs_drs": ic_drs["ic"],
            }
        )
    return rows


def attach_intraday_extension_flags(
    panel: pd.DataFrame,
    conn: Any,
    *,
    sample_from: str = INTRADAY_SAMPLE_FROM,
    etf_codes: tuple[str, ...] = DEFAULT_ETF_CODES,
) -> pd.DataFrame:
    """盤中 imp_extension strict · sample dates ≥ sample_from (1m K 覆蓋區間)."""
    out = panel.copy()
    out["intraday_extension"] = False
    out["intraday_spread_rs"] = float("nan")
    dates = sorted(
        d for d in out["date"].astype(str).unique().tolist() if str(d) >= sample_from
    )
    if not dates:
        out["intraday_extension"] = False
        return out

    sids = sorted(out["sid"].astype(str).unique().tolist())
    frames, trajectories, meta = build_dual_wma_intraday_trajectories(
        conn,
        dates=dates,
        etf_codes=etf_codes,
        stock_ids=sids,
        poll_minutes=list(INTRADAY_POLL_MINUTES),
    )
    frame_date = {f["id"]: f["date"] for f in frames}
    flags: dict[tuple[str, str], tuple[bool, float]] = {}
    for t in trajectories:
        sid = str(t["stock_id"])
        for p in t["points"]:
            fid = str(p.get("frame_id", ""))
            d = frame_date.get(fid) or (fid.split("T")[0] if "T" in fid else "")
            if not d:
                continue
            ext = _matches_imp_extension_strict(p)
            srs = float(p.get("spread_rs") or 0)
            key = (d, sid)
            prev = flags.get(key)
            if prev is None or (ext and not prev[0]):
                flags[key] = (ext, srs)
            elif ext and prev[0]:
                flags[key] = (True, max(prev[1], srs))

    intraday_ext, intraday_srs = [], []
    for _, row in out.iterrows():
        key = (str(row["date"]), str(row["sid"]))
        if key in flags:
            intraday_ext.append(flags[key][0])
            intraday_srs.append(flags[key][1])
        else:
            intraday_ext.append(False)
            intraday_srs.append(float("nan"))
    out["intraday_extension"] = intraday_ext
    out["intraday_spread_rs"] = intraday_srs
    out.attrs["intraday_meta"] = {
        "n_dates": len(dates),
        "n_frames": len(frames),
        "sample_from": sample_from,
        **{k: meta.get(k) for k in ("n_frames", "avg_priced_per_frame") if k in meta},
    }
    return out


def compute_hybrid_intraday_score(row: pd.Series | dict[str, Any]) -> float:
    """Daily hybrid + intraday extension confirmation bonus."""
    if isinstance(row, pd.Series):
        row = row.to_dict()
    base = compute_hybrid_structural_score(row)
    if row.get("intraday_extension"):
        base += W_INTRADAY_EXTENSION_BONUS
    return round(base, 6)


def run_walk_forward_ret5d(
    kin: pd.DataFrame,
    *,
    train_years: int = WF_TRAIN_YEARS,
    test_years: int = WF_TEST_YEARS,
) -> dict[str, Any]:
    """OOS validation · score_ret5d vs ret_5d (fixed formula · no IS weight fit)."""
    sub = kin.dropna(subset=[_RET_COL, "score_ret5d"]).copy()
    if sub.empty:
        return {"folds": [], "summary": {}, "passed": False}

    dates = sorted(sub["date"].astype(str).unique().tolist())
    folds = build_year_walk_forward_folds(dates, train_years=train_years, test_years=test_years)
    fold_rows: list[dict[str, Any]] = []
    oos_ics: list[float] = []

    for fold in folds:
        test = sub[sub["date"].astype(str).between(fold["test_start"], fold["test_end"])]
        if len(test) < 30:
            continue
        ic, p, n = pooled_spearman_ic(test, score_col="score_ret5d", label_col=_RET_COL)
        daily = daily_spearman_ic_series(test, score_col="score_ret5d", label_col=_RET_COL)
        dstats = _ic_stats([v for _, v in daily])
        pool_ret = float(test[_RET_COL].mean())
        top = test.nlargest(max(len(test) // 10, 1), "score_ret5d")
        top_ret = float(top[_RET_COL].mean())
        oos_ics.append(ic)
        fold_rows.append(
            {
                "fold_id": fold["fold_id"],
                "test_years": list(fold["test_years"]),
                "test_n": n,
                "oos_ic_ret5d": ic,
                "oos_p": p,
                "pool_mean_ret_5d": round(pool_ret, 4),
                "top_decile_mean_ret_5d": round(top_ret, 4),
                "daily_ic_mean": dstats.get("mean"),
            }
        )

    n_folds = len(fold_rows)
    n_pos = sum(1 for x in oos_ics if x > 0)
    mean_ic = float(np.mean(oos_ics)) if oos_ics else 0.0
    passed = n_folds >= 4 and n_pos >= max(3, n_folds // 2) and mean_ic > 0.01
    return {
        "folds": fold_rows,
        "summary": {
            "n_folds": n_folds,
            "n_oos_ic_positive": n_pos,
            "mean_oos_ic": round(mean_ic, 4),
            "train_years": train_years,
            "test_years": test_years,
        },
        "passed": passed,
        "verdict": "pass" if passed else ("marginal" if mean_ic > 0 else "fail"),
    }


def _fundamental_columns(panel: pd.DataFrame, conn: Any) -> pd.DataFrame:
    return attach_pit_fundamentals(panel, conn)


def compute_hybrid_structural_score(row: pd.Series | dict[str, Any]) -> float:
    """WMA(20) kinematic score + WMA(5) extension bonus."""
    if isinstance(row, pd.Series):
        row = row.to_dict()
    base = compute_drs5d_score_kinematic(row, weights=TUNED_KINEMATIC_WEIGHTS_5D)
    bonus = 0.0
    if float(row.get("spread_rs") or 0) > 0:
        bonus += W_EXTENSION_BONUS
    if row.get("spread_class") == "extension":
        bonus += W_EXTENSION_BONUS * 0.5
    return round(base + bonus, 6)


def compute_ret5d_score(row: pd.Series | dict[str, Any]) -> float:
    """Separate price track · NOT structural score."""
    if isinstance(row, pd.Series):
        row = row.to_dict()

    w5_v = float(row.get("w5_drs_rsr_1d", float("nan")))
    spread_rs = float(row.get("spread_rs") or 0)
    bench = float(row.get("bench_today_ret", 0.0))
    roe = float(row.get("roe_latest_q", float("nan")))
    rev_yoy = float(row.get("revenue_yoy_pct", float("nan")))

    raw = 0.0
    if np.isfinite(w5_v):
        raw += min(max(w5_v, 0.0) / 1.5, 1.0) * 0.45
    if spread_rs > 0:
        raw += min(spread_rs / 2.0, 1.0) * 0.25
    if bench >= 0:
        raw += 0.15 + min(bench / 2.0, 1.0) * 0.15
    if np.isfinite(roe) and roe > 0:
        raw += min(roe / 20.0, 1.0) * 0.1
    if np.isfinite(rev_yoy) and rev_yoy > 0:
        raw += min(rev_yoy / 30.0, 1.0) * 0.1
    sig_ret = float(row.get("sig_ret", 0.0))
    if sig_ret >= 3.0:
        raw -= 0.2
    return round(max(raw, 0.0), 6)


def _ic_pair(
    df: pd.DataFrame, score_col: str, label_col: str
) -> dict[str, float | int]:
    sub = df[[score_col, label_col]].dropna()
    if len(sub) < 10:
        return {"ic": 0.0, "p": 1.0, "n": len(sub)}
    ic, p, n = pooled_spearman_ic(sub, score_col=score_col, label_col=label_col)
    return {"ic": ic, "p": p, "n": n}


def run_hybrid_ret5d_research(
    conn: Any | None = None,
    *,
    skip_intraday: bool = False,
) -> dict[str, Any]:
    conn = conn or __import__("stock_db", fromlist=["connect"]).connect()
    panel, meta = build_drs5d_panel(conn)
    panel = attach_daily_dual_wma_spread(panel, conn)
    panel = _fundamental_columns(panel, conn)

    kin = panel[panel["in_pool_omega_kinematic"]].copy()
    kin["score_struct"] = kin["drs5d_score_kin"]
    kin["score_hybrid"] = kin.apply(compute_hybrid_structural_score, axis=1)
    kin["score_ret5d"] = kin.apply(compute_ret5d_score, axis=1)

    fund_cov = {
        "roe_nonnull_pct": round(
            float(kin["roe_latest_q"].notna().mean() * 100), 1
        )
        if "roe_latest_q" in kin.columns
        else 0.0,
        "revenue_yoy_nonnull_pct": round(
            float(kin["revenue_yoy_pct"].notna().mean() * 100), 1
        )
        if "revenue_yoy_pct" in kin.columns
        else 0.0,
    }

    intraday_meta: dict[str, Any] = {}
    if not skip_intraday:
        kin = attach_intraday_extension_flags(kin, conn)
        intraday_meta = dict(kin.attrs.get("intraday_meta", {}))
        kin["score_hybrid_intraday"] = kin.apply(compute_hybrid_intraday_score, axis=1)
    else:
        kin["intraday_extension"] = False
        kin["score_hybrid_intraday"] = kin["score_hybrid"]

    kw = {
        "drs_col": _DRS_COL,
        "ret_col": _RET_COL,
        "flip_col": "flip_leading_5d",
        "cross_col": "cross_100_5d",
    }

    ic_matrix = {
        "struct_vs_drs5d": _ic_pair(kin, "score_struct", _DRS_COL),
        "struct_vs_ret5d": _ic_pair(kin, "score_struct", _RET_COL),
        "hybrid_vs_drs5d": _ic_pair(kin, "score_hybrid", _DRS_COL),
        "hybrid_vs_ret5d": _ic_pair(kin, "score_hybrid", _RET_COL),
        "hybrid_intraday_vs_drs5d": _ic_pair(kin, "score_hybrid_intraday", _DRS_COL),
        "hybrid_intraday_vs_ret5d": _ic_pair(kin, "score_hybrid_intraday", _RET_COL),
        "ret5d_vs_ret5d": _ic_pair(kin, "score_ret5d", _RET_COL),
        "ret5d_vs_drs5d": _ic_pair(kin, "score_ret5d", _DRS_COL),
        "w5_vel_vs_ret5d": _ic_pair(kin.dropna(subset=["w5_drs_rsr_1d"]), "w5_drs_rsr_1d", _RET_COL),
        "spread_rs_vs_ret5d": _ic_pair(kin, "spread_rs", _RET_COL),
        "roe_vs_ret5d": _ic_pair(kin.dropna(subset=["roe_latest_q"]), "roe_latest_q", _RET_COL),
        "revenue_yoy_vs_ret5d": _ic_pair(
            kin.dropna(subset=["revenue_yoy_pct"]), "revenue_yoy_pct", _RET_COL
        ),
    }

    ext = kin[kin["spread_rs"] > 0]
    no_ext = kin[kin["spread_rs"] <= 0]
    extension_lift = {
        "n_extension": len(ext),
        "n_no_extension": len(no_ext),
        "mean_drs5d_ext": round(float(ext[_DRS_COL].mean()), 4) if len(ext) else 0.0,
        "mean_drs5d_no_ext": round(float(no_ext[_DRS_COL].mean()), 4) if len(no_ext) else 0.0,
        "mean_ret5d_ext": round(float(ext[_RET_COL].mean()), 4) if len(ext) else 0.0,
        "mean_ret5d_no_ext": round(float(no_ext[_RET_COL].mean()), 4) if len(no_ext) else 0.0,
    }

    id_ext = kin[kin["intraday_extension"] == True]  # noqa: E712
    id_no = kin[kin["intraday_extension"] != True]  # noqa: E712
    intraday_lift = {
        "n_intraday_ext": len(id_ext),
        "n_no_intraday_ext": len(id_no),
        "mean_drs5d_id_ext": round(float(id_ext[_DRS_COL].mean()), 4) if len(id_ext) else 0.0,
        "mean_drs5d_no_id": round(float(id_no[_DRS_COL].mean()), 4) if len(id_no) else 0.0,
        "mean_ret5d_id_ext": round(float(id_ext[_RET_COL].mean()), 4) if len(id_ext) else 0.0,
        "mean_ret5d_no_id": round(float(id_no[_RET_COL].mean()), 4) if len(id_no) else 0.0,
        "sample_from": intraday_meta.get("sample_from", INTRADAY_SAMPLE_FROM),
        "n_dates_sampled": intraday_meta.get("n_dates", 0),
    }

    ev_struct = evaluate_score_deciles(
        kin, subset_col=None, score_col="score_struct", **kw
    )
    ev_hybrid = evaluate_score_deciles(kin, subset_col=None, score_col="score_hybrid", **kw)
    ev_hybrid_id = evaluate_score_deciles(
        kin, subset_col=None, score_col="score_hybrid_intraday", **kw
    )
    ev_ret = evaluate_score_deciles(kin, subset_col=None, score_col="score_ret5d", **kw)

    bench_strata = stratify_bench_gating(kin)
    wfa_ret5d = run_walk_forward_ret5d(kin)

    bench_pos = kin[kin["bench_today_ret"] >= 0] if "bench_today_ret" in kin.columns else kin
    ic_ret_bench = _ic_pair(bench_pos, "score_ret5d", _RET_COL)

    return {
        "meta": {
            **meta,
            "generated_on": date.today().isoformat(),
            "omega_kinematic_n": len(kin),
            "hold_days": HOLD_DAYS,
            "design": "WMA20 drs_5d + WMA5 extension + PIT fundamentals + bench gating + intraday联合",
            "fundamental_coverage": fund_cov,
            "intraday": intraday_meta,
        },
        "ic_matrix": ic_matrix,
        "extension_lift": extension_lift,
        "intraday_lift": intraday_lift,
        "bench_strata": bench_strata,
        "walk_forward_ret5d": wfa_ret5d,
        "evaluation": {
            "structural": asdict(ev_struct),
            "hybrid": asdict(ev_hybrid),
            "hybrid_intraday": asdict(ev_hybrid_id),
            "ret5d": asdict(ev_ret),
        },
        "ret5d_bench_gated_ic": ic_ret_bench,
        "verdict": _verdict(ic_matrix, ev_hybrid, ev_ret, wfa_ret5d),
    }


def _verdict(
    ic: dict[str, dict[str, float | int]],
    ev_hybrid: Any,
    ev_ret: Any,
    wfa_ret5d: dict[str, Any] | None = None,
) -> dict[str, str]:
    struct_drs = ic["struct_vs_drs5d"]["ic"]
    hybrid_drs = ic["hybrid_vs_drs5d"]["ic"]
    ret_ret = ic["ret5d_vs_ret5d"]["ic"]
    ret_drs = ic["ret5d_vs_drs5d"]["ic"]

    lines = []
    if hybrid_drs > struct_drs:
        lines.append("extension_bonus_improves_drs5d_ic")
    else:
        lines.append("extension_bonus_flat_or_worse_drs5d")
    if ret_ret > 0.03 and ic["ret5d_vs_ret5d"]["p"] < 0.05:
        lines.append("ret5d_track_has_price_signal")
    elif ret_ret > ic["struct_vs_ret5d"]["ic"]:
        lines.append("ret5d_track_better_than_struct_for_price")
    else:
        lines.append("ret5d_track_still_weak")
    if abs(ret_drs) < abs(struct_drs) * 0.5:
        lines.append("tracks_decoupled_ok")
    if wfa_ret5d:
        if wfa_ret5d.get("passed"):
            lines.append("ret5d_wfa_pass")
        elif wfa_ret5d.get("verdict") == "marginal":
            lines.append("ret5d_wfa_marginal")
        else:
            lines.append("ret5d_wfa_fail")
    return {
        "summary": " · ".join(lines),
        "struct_ic_drs5d": f"{struct_drs:+.4f}",
        "hybrid_ic_drs5d": f"{hybrid_drs:+.4f}",
        "ret5d_ic_ret": f"{ret_ret:+.4f}",
        "hybrid_top_ret5d": f"{ev_hybrid.deciles[-1].mean_ret_3d if ev_hybrid.deciles else 0:+.2f}%",
        "ret5d_top_ret5d": f"{ev_ret.deciles[-1].mean_ret_3d if ev_ret.deciles else 0:+.2f}%",
    }


def render_hybrid_ret5d_markdown(result: dict[str, Any]) -> str:
    meta = result["meta"]
    ic = result["ic_matrix"]
    ext = result["extension_lift"]
    id_lift = result.get("intraday_lift", {})
    bench = result.get("bench_strata", [])
    wfa = result.get("walk_forward_ret5d", {})
    fund = meta.get("fundamental_coverage", {})
    v = result["verdict"]
    ev = result["evaluation"]

    lines = [
        "# Hybrid dual-WMA · drs_5d + ret_5d research",
        "",
        "> WMA(20) pool/outcome · WMA(5) spread_rs extension · PIT fundamentals · bench b0/b1/b2 · intraday联合",
        "",
        f"- Ω′ n: **{meta.get('omega_kinematic_n', '—')}** · hold **{meta.get('hold_days', 5)}** days",
        f"- PIT fundamentals: ROE {fund.get('roe_nonnull_pct', 0)}% · revenue YoY {fund.get('revenue_yoy_nonnull_pct', 0)}%",
        f"- Intraday sample: ≥{id_lift.get('sample_from', INTRADAY_SAMPLE_FROM)} · {id_lift.get('n_dates_sampled', 0)} dates",
        f"- Generated: {meta.get('generated_on', '')}",
        "",
        "## A. IC matrix (Ω′)",
        "",
        "| Model | IC vs drs_5d | p | IC vs ret_5d | p |",
        "|-------|--------------|---|--------------|---|",
    ]
    for name, dk, rk in [
        ("Structural WMA20", "struct_vs_drs5d", "struct_vs_ret5d"),
        ("Hybrid + extension", "hybrid_vs_drs5d", "hybrid_vs_ret5d"),
        ("Hybrid + intraday", "hybrid_intraday_vs_drs5d", "hybrid_intraday_vs_ret5d"),
        ("ret_5d track", "ret5d_vs_drs5d", "ret5d_vs_ret5d"),
        ("W5 drs_rsr_1d alone", "w5_vel_vs_ret5d", "w5_vel_vs_ret5d"),
        ("ROE (raw)", "roe_vs_ret5d", "roe_vs_ret5d"),
        ("Revenue YoY (raw)", "revenue_yoy_vs_ret5d", "revenue_yoy_vs_ret5d"),
    ]:
        d, r = ic.get(dk, {}), ic.get(rk, {})
        lines.append(
            f"| {name} | {d.get('ic', 0):+.4f} | {d.get('p', 1):.4f} | "
            f"{r.get('ic', 0):+.4f} | {r.get('p', 1):.4f} |"
        )

    lines.extend(
        [
            "",
            "## B. WMA(5) daily extension split",
            "",
            f"| | n | mean drs_5d | mean ret_5d |",
            f"|---|---|-------------|-------------|",
            f"| spread_rs > 0 | {ext['n_extension']} | {ext['mean_drs5d_ext']:+.3f} | {ext['mean_ret5d_ext']:+.2f}% |",
            f"| spread_rs ≤ 0 | {ext['n_no_extension']} | {ext['mean_drs5d_no_ext']:+.3f} | {ext['mean_ret5d_no_ext']:+.2f}% |",
            "",
            "## C. Intraday extension (dual_wma_drs3d_intraday strict)",
            "",
            f"| | n | mean drs_5d | mean ret_5d |",
            f"|---|---|-------------|-------------|",
            f"| intraday ext | {id_lift.get('n_intraday_ext', 0)} | {id_lift.get('mean_drs5d_id_ext', 0):+.3f} | {id_lift.get('mean_ret5d_id_ext', 0):+.2f}% |",
            f"| no intraday ext | {id_lift.get('n_no_intraday_ext', 0)} | {id_lift.get('mean_drs5d_no_id', 0):+.3f} | {id_lift.get('mean_ret5d_no_id', 0):+.2f}% |",
            "",
            "## D. 台指 bench gating (b0/b1/b2)",
            "",
            "| tier | rule | n | mean ret_5d | mean drs_5d | IC ret5d→ret | IC hybrid→drs |",
            "|------|------|---|-------------|-------------|--------------|---------------|",
        ]
    )
    for row in bench:
        lines.append(
            f"| {row['tier']} | {row['bench_rule']} | {row['n']} | {row['mean_ret_5d']:+.2f}% | "
            f"{row['mean_drs_5d']:+.3f} | {row['ic_ret5d_vs_ret']:+.4f} | {row['ic_hybrid_vs_drs']:+.4f} |"
        )
    if not bench:
        lines.append("| — | — | — | — | — | — | — |")

    wfa_sum = wfa.get("summary", {})
    lines.extend(
        [
            "",
            "## E. Walk-forward · score_ret5d vs ret_5d",
            "",
            f"- Folds: **{wfa_sum.get('n_folds', 0)}** · OOS IC>0: **{wfa_sum.get('n_oos_ic_positive', 0)}** · "
            f"mean OOS IC: **{wfa_sum.get('mean_oos_ic', 0):+.4f}** · verdict: **{wfa.get('verdict', '—')}**",
            "",
            "| fold | test years | n | OOS IC | pool ret_5d | top dec ret_5d |",
            "|------|------------|---|--------|-------------|----------------|",
        ]
    )
    for f in wfa.get("folds", []):
        lines.append(
            f"| {f.get('fold_id')} | {','.join(str(y) for y in f.get('test_years', []))} | "
            f"{f.get('test_n')} | {f.get('oos_ic_ret5d', 0):+.4f} | "
            f"{f.get('pool_mean_ret_5d', 0):+.2f}% | {f.get('top_decile_mean_ret_5d', 0):+.2f}% |"
        )

    lines.extend(
        [
            "",
            "## F. Top decile outcomes",
            "",
            f"| Model | IC drs_5d | top drs_5d lift | top mean ret_5d |",
            f"|-------|-----------|-----------------|-----------------|",
        ]
    )
    for label, key in [
        ("Structural", "structural"),
        ("Hybrid daily", "hybrid"),
        ("Hybrid + intraday", "hybrid_intraday"),
        ("ret_5d track", "ret5d"),
    ]:
        block = ev.get(key, {})
        if block.get("deciles"):
            lines.append(
                f"| {label} | {block['spearman_ic']:+.4f} | {block['top_decile_lift']:+.3f} | "
                f"{block['deciles'][-1]['mean_ret_3d']:+.2f}% |"
            )
        else:
            lines.append(f"| {label} | — | — | — |")

    lines.extend(
        [
            "",
            "## G. Verdict",
            "",
            f"- {v.get('summary', '')}",
            f"- struct IC drs_5d: {v.get('struct_ic_drs5d')} → hybrid: {v.get('hybrid_ic_drs5d')}",
            f"- ret_5d model IC: {v.get('ret5d_ic_ret')} · top decile ret: hybrid {v.get('hybrid_top_ret5d')} · ret track {v.get('ret5d_top_ret5d')}",
        ]
    )
    return "\n".join(lines)


def write_hybrid_ret5d_report(
    result: dict[str, Any],
    out_dir: Path | None = None,
    *,
    stamp: str | None = None,
) -> tuple[Path, Path]:
    out_dir = out_dir or (PROJECT_ROOT / "reports" / "research" / "rrg")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = stamp or date.today().strftime("%Y%m%d")
    json_path = out_dir / f"hybrid_drs5d_ret5d_{stamp}.json"
    md_path = out_dir / f"hybrid_drs5d_ret5d_{stamp}.md"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_hybrid_ret5d_markdown(result), encoding="utf-8")
    return json_path, md_path
