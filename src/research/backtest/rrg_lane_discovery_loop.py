"""RRG pre-release lane discovery loop · case scan · miss-list · preregistered sweep.

Topic SSOT: config/research.yaml → rrg-lane-discovery-loop
Outcome: ret_3d (3-day hold) · PIT signal-day features only.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml

from research.backtest.finpilot_local_backtest import load_price_panels
from research.backtest.rrg_improving_lifecycle_backtest import BURST_PCT, build_lifecycle_events
from research.backtest.rrg_improving_s3rv_executable import enrich_lifecycle_events
from research.backtest.rrg_pre_release_funnel_lanes import lane_mask
from research.backtest.rrg_pre_release_funnel_lanes_3d import (
    HOLD_DAYS,
    LOSS5_PCT,
    LOSS8_PCT,
    _find_pre_release_signal,
    _years_span,
    build_pre_release_funnel_panel_3d,
    enrich_events_3d_outcomes,
    jaccard,
    calendar_day_set,
    load_funnel_lanes_3d_config,
)
from research.backtest.rrg_lane_signal_cooccurrence import (
    apply_exclusion_mask,
    run_cooccurrence_analysis,
)
from research_config import load_research_config
from stock_db import PROJECT_ROOT

TOPIC_ID = "rrg-lane-discovery-loop"
DEFAULT_REPORT_DIR = PROJECT_ROOT / "reports" / "research" / "rrg"


def _json_safe(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_json_safe(x) for x in obj]
    if isinstance(obj, float) and np.isnan(obj):
        return None
    if isinstance(obj, (np.floating, np.integer)):
        return obj.item()
    return obj


def load_discovery_topic() -> dict[str, Any]:
    cfg = load_research_config()
    topic = cfg.get(TOPIC_ID)
    if topic is None:
        raise KeyError(f"topic {TOPIC_ID!r} not in config/research.yaml")
    rd = topic.report_dir or "reports/research/rrg"
    report_dir = Path(rd) if Path(rd).is_absolute() else PROJECT_ROOT / rd
    return {
        "topic_id": TOPIC_ID,
        "title": topic.title,
        "report_dir": report_dir,
        "sweep": topic.sweep or {},
        "hypotheses": [dict(h) for h in topic.hypotheses],
    }


def _load_lane_defs(cfg_path: Path | None = None, *, tiers: tuple[str, ...] = ("strict", "coverage")) -> list[dict[str, Any]]:
    cfg = load_funnel_lanes_3d_config(cfg_path)
    lanes = cfg.get("lanes") or {}
    out: list[dict[str, Any]] = []
    for tier in tiers:
        for ld in lanes.get(tier) or []:
            if isinstance(ld, dict) and ld.get("id"):
                out.append(ld)
    return out


def _signal_features(row: pd.Series, *, signal_date: str | None = None) -> dict[str, Any]:
    def _get(key: str, default: Any = 0) -> Any:
        if key in row.index:
            return row[key]
        if key in row:
            return row[key]
        return default

    d = signal_date or str(_get("date", ""))
    return {
        "date": d,
        "sid": str(_get("sid", "")),
        "sig_ret": round(float(_get("sig_ret", 0)), 3),
        "excess": round(float(_get("excess", 0)), 3),
        "end_q": str(_get("end_q", "")),
        "prev_end_q": str(_get("prev_end_q") or ""),
        "improve_streak": int(_get("improve_streak", 0) or 0),
        "rv": round(float(_get("rv", 0) or 0), 2),
        "bench_today_ret": round(float(_get("bench_today_ret", 0) or 0), 3),
        "disp_contract": bool(_get("disp_contract", False)),
        "session_gap_days": int(_get("session_gap_days", 1) or 1),
    }


def _passes_loss_gate(
    ret_3d: float,
    min_ret_3d: float | None,
    *,
    max_loss5_rate: float,
    max_loss8_rate: float,
    exclude_signal: bool = False,
) -> bool:
    """Exclude paths that finished or dipped too deep during hold."""
    if exclude_signal:
        return False
    if ret_3d <= LOSS5_PCT:
        return False
    if min_ret_3d is not None and min_ret_3d <= LOSS8_PCT:
        return False
    if ret_3d <= LOSS8_PCT:
        return False
    return True


def scan_three_day_streak_outcomes(
    conn: Any | None = None,
    *,
    min_total_pct: float = 15.0,
    year_start: int = 2015,
    year_end: int | None = None,
    min_ret_3d: float = 0.0,
    exclude_loss5: bool = True,
    exclude_loss8_path: bool = True,
) -> pd.DataFrame:
    """Scan watchlist for 3 consecutive up closes · total gain D0→D3 ≥ min_total_pct."""
    conn = conn or __import__("stock_db", fromlist=["connect"]).connect()
    close, _, _ = load_price_panels(conn)
    full_dates = close.index.astype(str).tolist()
    year_end = year_end or date.today().year

    base, _ = enrich_lifecycle_events(conn)
    full, _ = enrich_events_3d_outcomes(conn, events=base)
    full = full.sort_values(["sid", "date"]).copy()
    full["prev_end_q"] = full.groupby("sid")["end_q"].shift(1).fillna("")
    feat_idx = full.set_index(["sid", "date"])

    rows: list[dict[str, Any]] = []
    for sid in close.columns:
        for i in range(3, len(full_dates)):
            d0, d1, d2, d3 = full_dates[i - 3], full_dates[i - 2], full_dates[i - 1], full_dates[i]
            yr = int(d3[:4])
            if yr < year_start or yr > year_end:
                continue
            try:
                c0 = float(close.at[d0, sid])
                c1 = float(close.at[d1, sid])
                c2 = float(close.at[d2, sid])
                c3 = float(close.at[d3, sid])
            except (KeyError, TypeError, ValueError):
                continue
            if min(c0, c1, c2, c3) <= 0:
                continue
            if not (c1 > c0 and c2 > c1 and c3 > c2):
                continue
            total_pct = (c3 / c0 - 1) * 100
            if total_pct < min_total_pct:
                continue
            rows.append(
                {
                    "sid": str(sid),
                    "d0": d0,
                    "streak_start": d1,
                    "streak_end": d3,
                    "streak_total_pct": round(total_pct, 2),
                }
            )

    if not rows:
        return pd.DataFrame()

    streaks = pd.DataFrame(rows)
    panel, _ = build_pre_release_funnel_panel_3d(conn=conn, events=full)
    panel_idx = panel.set_index(["sid", "date"], drop=False)

    enriched: list[dict[str, Any]] = []
    for _, st in streaks.iterrows():
        sid = st["sid"]
        signal_d, pr_row = _find_pre_release_signal(
            panel, full, sid, st["streak_start"], lookback_sessions=5
        )
        entry: dict[str, Any] = {
            **st.to_dict(),
            "signal_date": signal_d,
            "in_pre_release_pool": pr_row is not None,
        }
        if signal_d and (sid, signal_d) in feat_idx.index:
            fr = feat_idx.loc[(sid, signal_d)]
            if isinstance(fr, pd.DataFrame):
                fr = fr.iloc[-1]
            entry.update(_signal_features(fr, signal_date=str(signal_d)))
        elif signal_d and (sid, signal_d) in panel_idx.index:
            pr = panel_idx.loc[(sid, signal_d)]
            if isinstance(pr, pd.DataFrame):
                pr = pr.iloc[-1]
            entry.update(_signal_features(pr, signal_date=str(signal_d)))
        if pr_row is not None:
            entry["ret_3d"] = round(float(pr_row["ret_3d"]), 2)
            entry["ret_1d"] = round(float(pr_row["ret_1d"]), 2)
            entry["min_ret_3d"] = round(float(pr_row.get("min_ret_3d", pr_row["ret_3d"])), 2)
            entry["win_3d"] = bool(pr_row["ret_3d"] > 0)
        else:
            entry["ret_3d"] = None
            entry["min_ret_3d"] = None
            entry["win_3d"] = None

        if entry.get("ret_3d") is not None:
            if entry["ret_3d"] < min_ret_3d:
                continue
            if exclude_loss5 and entry["ret_3d"] <= LOSS5_PCT:
                continue
            if exclude_loss8_path and entry.get("min_ret_3d") is not None:
                if entry["min_ret_3d"] <= LOSS8_PCT:
                    continue
        enriched.append(entry)

    return pd.DataFrame(enriched)


def _panel_row_positions(panel: pd.DataFrame, sid: str, signal_date: str) -> list[int]:
    """Integer positions in panel for (sid, signal_date) — lane_mask uses RangeIndex."""
    m = (panel["sid"].astype(str) == str(sid)) & (panel["date"].astype(str) == str(signal_date))
    return [int(i) for i in panel.index[m].tolist()]


def _lane_seed_hits_at_positions(
    positions: list[int],
    lane_masks: dict[str, pd.Series],
    seed_masks: list[tuple[str, pd.Series]] | None,
) -> tuple[list[str], list[str]]:
    if not positions:
        return [], []
    matched_lanes: list[str] = []
    for lid, m in lane_masks.items():
        if any(bool(m.loc[pos]) for pos in positions):
            matched_lanes.append(lid)
    seed_hits: list[str] = []
    for name, m in seed_masks or []:
        if any(bool(m.loc[pos]) for pos in positions):
            seed_hits.append(name)
    return matched_lanes, seed_hits


def build_miss_list(
    cases: pd.DataFrame,
    lane_defs: list[dict[str, Any]],
    panel: pd.DataFrame,
    *,
    min_ret_3d: float = 5.0,
    seed_masks: list[tuple[str, pd.Series]] | None = None,
) -> pd.DataFrame:
    """Cases with strong ret_3d but zero registered lane match at signal-day."""
    if cases.empty:
        return pd.DataFrame()

    check_lanes = bool(lane_defs)
    check_seeds = bool(seed_masks)
    if not check_lanes and not check_seeds:
        return pd.DataFrame()

    lane_masks = (
        {str(ld["id"]): lane_mask(panel, ld["rule_atoms"]) for ld in lane_defs} if check_lanes else {}
    )

    misses: list[dict[str, Any]] = []
    for _, row in cases.iterrows():
        r3 = row.get("ret_3d")
        if r3 is None or (isinstance(r3, float) and np.isnan(r3)) or float(r3) < min_ret_3d:
            continue
        sd, sid = row.get("signal_date"), str(row["sid"])
        if not sd or pd.isna(sd):
            continue
        positions = _panel_row_positions(panel, sid, str(sd))
        if not positions:
            continue
        matched, seed_hits = _lane_seed_hits_at_positions(positions, lane_masks, seed_masks)
        if matched or seed_hits:
            continue
        misses.append(
            {
                **row.to_dict(),
                "matched_lanes": matched,
                "matched_seeds": seed_hits,
                "miss_source": row.get("miss_source", "streak_scan"),
            }
        )
    return pd.DataFrame(misses)


def build_june2026_miss_supplement(
    panel: pd.DataFrame,
    lane_defs: list[dict[str, Any]],
    *,
    min_ret_3d: float = 5.0,
    seed_masks: list[tuple[str, pd.Series]] | None = None,
    conn: Any | None = None,
) -> pd.DataFrame:
    """Miss rows from June 2026 Improving→Leading streak case study (P0 backfill)."""
    from research.backtest.rrg_pre_release_funnel_lanes_3d import (
        analyze_june2026_leading_streak_cases,
    )

    study = analyze_june2026_leading_streak_cases(panel, lane_defs, conn=conn)
    rows: list[dict[str, Any]] = []
    for entry in study.get("june2026_leading_streak_cases") or []:
        if not entry.get("in_pre_release_pool"):
            continue
        r3 = entry.get("ret_3d")
        if r3 is None or float(r3) < min_ret_3d:
            continue
        if entry.get("matched_lanes"):
            continue
        sd = str(entry.get("signal_date") or "")
        sid = str(entry.get("sid") or "")
        positions = _panel_row_positions(panel, sid, sd)
        matched, seed_hits = _lane_seed_hits_at_positions(positions, {}, seed_masks)
        if matched or seed_hits:
            continue
        rows.append(
            {
                "sid": sid,
                "signal_date": sd,
                "streak_start": entry.get("streak_start"),
                "streak_end": entry.get("streak_end"),
                "streak_total_pct": entry.get("streak_total_pct"),
                "ret_3d": r3,
                "ret_1d": entry.get("ret_1d"),
                "sig_ret": entry.get("sig_ret"),
                "end_q": entry.get("end_q"),
                "improve_streak": entry.get("improve_streak"),
                "in_pre_release_pool": True,
                "matched_lanes": [],
                "matched_seeds": seed_hits,
                "miss_source": "june2026_lead3",
                "d3_leading": entry.get("d3_leading"),
                "d3_rs_ratio": entry.get("d3_rs_ratio"),
            }
        )
    return pd.DataFrame(rows)


def merge_miss_frames(*frames: pd.DataFrame) -> pd.DataFrame:
    """Dedupe miss-list by (sid, signal_date) · keep highest ret_3d."""
    parts = [f for f in frames if f is not None and len(f)]
    if not parts:
        return pd.DataFrame()
    merged = pd.concat(parts, ignore_index=True)
    if "signal_date" not in merged.columns:
        return merged
    merged["_ret_sort"] = pd.to_numeric(merged.get("ret_3d"), errors="coerce").fillna(-999)
    merged = merged.sort_values("_ret_sort", ascending=False)
    merged = merged.drop_duplicates(subset=["sid", "signal_date"], keep="first")
    return merged.drop(columns=["_ret_sort"], errors="ignore")


def mask_from_seed(panel: pd.DataFrame, params: dict[str, Any]) -> pd.Series:
    """Build PIT mask from preregistered seed params (H1c / H3 / custom)."""
    m = pd.Series(True, index=panel.index)
    seed = str(params.get("seed_id") or params.get("seed") or "")

    def _num(key: str) -> float | None:
        v = params.get(key)
        if v is None or (isinstance(v, float) and np.isnan(v)):
            return None
        return float(v)

    def _int(key: str) -> int | None:
        v = _num(key)
        return None if v is None else int(v)

    bench_min = _num("bench_min")
    if bench_min is not None:
        m &= panel["bench_today_ret"] >= bench_min

    sig_lo, sig_hi = _num("sig_lo"), _num("sig_hi")
    if sig_lo is not None:
        m &= panel["sig_ret"] >= sig_lo
    if sig_hi is not None:
        m &= panel["sig_ret"] < sig_hi

    rv_lo, rv_hi = _num("rv_lo"), _num("rv_hi")
    if rv_lo is not None:
        m &= panel["rv"] >= rv_lo
    if rv_hi is not None:
        m &= panel["rv"] < rv_hi

    streak_lo, streak_hi = _int("streak_lo"), _int("streak_hi")
    if streak_lo is not None:
        m &= panel["improve_streak"] >= streak_lo
    if streak_hi is not None:
        m &= panel["improve_streak"] <= streak_hi

    end_q = params.get("end_q")
    if end_q and str(end_q) != "nan":
        m &= panel["end_q"] == str(end_q)
    prev_q = params.get("prev_end_q")
    if prev_q and str(prev_q) != "nan":
        m &= panel["prev_end_q"] == str(prev_q)

    if params.get("require_elag_plag"):
        m &= (panel["end_q"] == "lagging") & (panel["prev_end_q"] == "lagging")
    if params.get("require_excess_band"):
        ex_lo = float(_num("ex_lo") or -2)
        ex_hi = float(_num("ex_hi") or -0.3)
        m &= (panel["excess"] >= ex_lo) & (panel["excess"] <= ex_hi)
    if params.get("require_gap1"):
        m &= panel["session_gap_days"] == 1
    if params.get("require_disp"):
        m &= panel["disp_contract"]
    if params.get("require_hist"):
        m &= panel["sid_hist_ok"]
    bench_max = _num("bench_max")
    if bench_max is not None:
        m &= panel["bench_today_ret"] < bench_max

    if seed.startswith("H1c") and not (end_q and str(end_q) != "nan"):
        m &= (panel["end_q"] == "lagging") & (panel["prev_end_q"] == "lagging")
    if seed.startswith("H3") and not (end_q and str(end_q) != "nan"):
        m &= panel["end_q"] == "improving"
        if streak_lo is None:
            m &= panel["improve_streak"] >= 7

    return m


def evaluate_seed(
    panel: pd.DataFrame,
    params: dict[str, Any],
    *,
    gates: dict[str, Any] | None = None,
    exclusion_tags: list[str] | None = None,
) -> dict[str, Any]:
    gates = gates or {}
    years = _years_span(panel["date"])
    m = mask_from_seed(panel, params)
    if exclusion_tags:
        m = apply_exclusion_mask(m, panel, exclusion_tags)
    n = int(m.sum())
    if n == 0:
        return {
            **params,
            "n": 0,
            "ev_yr": 0.0,
            "win_3d": 0.0,
            "mean_3d": 0.0,
            "loss5_rate": 0.0,
            "loss8_rate": 0.0,
            "pass_mean": False,
            "pass_loss": False,
            "pass_ev": False,
            "pass_win": False,
            "pass_all": False,
        }

    rets = panel.loc[m, "ret_3d"].to_numpy(dtype=float)
    min_rets = panel.loc[m, "min_ret_3d"].to_numpy(dtype=float) if "min_ret_3d" in panel.columns else rets
    win = float((rets > 0).mean())
    mean = float(rets.mean())
    loss5 = float((rets <= LOSS5_PCT).mean())
    loss8 = float((rets <= LOSS8_PCT).mean())
    loss8_path = float((min_rets <= LOSS8_PCT).mean()) if len(min_rets) else loss8
    ev_yr = n / years

    mean_min = float(gates.get("mean_3d_min", 3.0))
    win_min = float(gates.get("win_3d_min", 0.58))
    loss5_max = float(gates.get("loss5_rate_max", 0.15))
    loss8_max = float(gates.get("loss8_rate_max", 0.08))
    ev_min = float(gates.get("ev_yr_min", 0.0))
    ev_max = float(gates.get("ev_yr_max", 30.0))
    n_min = int(gates.get("n_min", 8))

    pass_mean = mean >= mean_min
    pass_win = win >= win_min
    pass_loss = loss5 <= loss5_max and loss8_path <= loss8_max
    pass_ev = ev_min <= ev_yr <= ev_max and n >= n_min

    return {
        **params,
        "n": n,
        "ev_yr": round(ev_yr, 2),
        "win_3d": round(win, 4),
        "mean_3d": round(mean, 2),
        "loss5_rate": round(loss5, 4),
        "loss8_rate": round(loss8, 4),
        "loss8_path_rate": round(loss8_path, 4),
        "pass_mean": pass_mean,
        "pass_win": pass_win,
        "pass_loss": pass_loss,
        "pass_ev": pass_ev,
        "pass_all": pass_mean and pass_win and pass_loss and pass_ev,
    }


def run_cell(params: dict[str, Any]) -> dict[str, Any]:
    """Sweep runner entry · loads panel once per cell (research sweep_runner)."""
    topic = load_discovery_topic()
    gates = (topic["sweep"].get("gates") or {}) if topic.get("sweep") else {}
    panel, _ = build_pre_release_funnel_panel_3d()
    result = evaluate_seed(panel, params, gates=gates)
    return {
        "n": result["n"],
        "ev_yr": result["ev_yr"],
        "win_3d": result["win_3d"],
        "mean_3d": result["mean_3d"],
        "loss5_rate": result["loss5_rate"],
        "loss8_path_rate": result.get("loss8_path_rate", 0),
        "pass_all": result["pass_all"],
    }


def _expand_seed_grid(sweep: dict[str, Any]) -> list[dict[str, Any]]:
    seeds = sweep.get("seeds") or {}
    grid: list[dict[str, Any]] = []
    for seed_id, spec in seeds.items():
        if not isinstance(spec, dict):
            continue
        base = {"seed_id": seed_id, **{k: v for k, v in spec.items() if k != "grid"}}
        sub = spec.get("grid")
        if not isinstance(sub, dict) or not sub:
            grid.append(base)
            continue
        import itertools

        keys = list(sub.keys())
        vals = [sub[k] if isinstance(sub[k], list) else [sub[k]] for k in keys]
        for combo in itertools.product(*vals):
            grid.append({**base, **dict(zip(keys, combo))})
    return grid


def _profile_cluster(df: pd.DataFrame, label: str) -> dict[str, Any]:
    if df.empty:
        return {"label": label, "n": 0}
    num_cols = ["sig_ret", "excess", "rv", "bench_today_ret", "improve_streak", "ret_3d"]
    prof: dict[str, Any] = {
        "label": label,
        "n": len(df),
        "end_q_top": df["end_q"].value_counts().head(3).to_dict() if "end_q" in df else {},
        "prev_end_q_top": df["prev_end_q"].value_counts().head(3).to_dict() if "prev_end_q" in df else {},
    }
    for col in num_cols:
        if col in df.columns and df[col].notna().any():
            prof[f"{col}_mean"] = round(float(df[col].mean()), 3)
            prof[f"{col}_median"] = round(float(df[col].median()), 3)
    if "ret_3d" in df.columns:
        prof["win_3d"] = round(float((df["ret_3d"] > 0).mean()), 3)
    return prof


def run_discovery_loop(
    *,
    report_dir: Path | None = None,
    min_streak_pct: float = 15.0,
    min_ret_3d_case: float = 0.0,
    miss_min_ret_3d: float = 5.0,
    run_sweep: bool = True,
    cooccurrence_only: bool = False,
    conn: Any | None = None,
) -> dict[str, Any]:
    topic = load_discovery_topic()
    out_dir = report_dir or topic["report_dir"]
    out_dir.mkdir(parents=True, exist_ok=True)
    sweep = topic["sweep"]
    gates = sweep.get("gates") or {}

    conn = conn or __import__("stock_db", fromlist=["connect"]).connect()
    panel, meta = build_pre_release_funnel_panel_3d(conn=conn)
    miss_cfg = sweep.get("miss_list") or {}
    cooc_cfg = sweep.get("cooccurrence") or {}

    cooc_result = run_cooccurrence_analysis(panel, cooc_cfg) if cooc_cfg.get("enabled", True) else {}
    exclusion_tags = cooc_result.get("active_exclusion_tags") or []
    pattern_seeds = cooc_result.get("pattern_seeds") or []

    miss_tiers = tuple(miss_cfg.get("lane_tiers") or ("strict", "coverage"))
    lane_defs = _load_lane_defs(tiers=miss_tiers)
    miss_min_ret = float(miss_cfg.get("min_ret_3d", miss_min_ret_3d))

    cases = pd.DataFrame()
    in_pool = pd.DataFrame()
    miss_from_streak = pd.DataFrame()
    miss_june = pd.DataFrame()
    miss = pd.DataFrame()
    profiles: list[dict[str, Any]] = []

    if not cooccurrence_only:
        cohort_loss = sweep.get("cohort_scan") or {}
        cases = scan_three_day_streak_outcomes(
            conn,
            min_total_pct=float(cohort_loss.get("min_streak_total_pct", min_streak_pct)),
            year_start=int(cohort_loss.get("year_start", 2015)),
            year_end=int(cohort_loss.get("year_end", date.today().year)),
            min_ret_3d=float(cohort_loss.get("min_ret_3d", min_ret_3d_case)),
            exclude_loss5=bool(cohort_loss.get("exclude_loss5", True)),
            exclude_loss8_path=bool(cohort_loss.get("exclude_loss8_path", True)),
        )

    sweep_results: list[dict[str, Any]] = []
    all_seed_params = _expand_seed_grid(sweep) + pattern_seeds
    if run_sweep:
        for params in all_seed_params:
            sweep_results.append(
                evaluate_seed(panel, params, gates=gates, exclusion_tags=exclusion_tags)
            )
    sweep_df = pd.DataFrame(sweep_results)
    passed = sweep_df[sweep_df["pass_all"]] if len(sweep_df) else sweep_df

    # Compare with/without exclusion on top patterns
    exclusion_ab: list[dict[str, Any]] = []
    for ps in pattern_seeds[:5]:
        without = evaluate_seed(panel, ps, gates=gates, exclusion_tags=[])
        with_ex = evaluate_seed(panel, ps, gates=gates, exclusion_tags=exclusion_tags)
        exclusion_ab.append(
            {
                "seed_id": ps.get("seed_id"),
                "pattern": ps.get("pattern"),
                "mean_without_excl": without.get("mean_3d"),
                "mean_with_excl": with_ex.get("mean_3d"),
                "loss5_without": without.get("loss5_rate"),
                "loss5_with_excl": with_ex.get("loss5_rate"),
                "n_without": without.get("n"),
                "n_with_excl": with_ex.get("n"),
            }
        )

    seed_masks: list[tuple[str, pd.Series]] = []
    if not cooccurrence_only and bool(miss_cfg.get("include_passed_seeds", True)) and len(passed):
        seed_keys = {
            "seed_id", "bench_min", "bench_max", "sig_lo", "sig_hi", "rv_lo", "rv_hi",
            "streak_lo", "streak_hi", "end_q", "prev_end_q", "require_elag_plag",
            "require_excess_band", "require_gap1", "require_disp", "require_hist",
        }
        for _, row in passed.iterrows():
            params = {k: row[k] for k in seed_keys if k in row.index and pd.notna(row[k])}
            label = f"{row.get('seed_id')}|{params.get('bench_min')}|{params.get('sig_lo')}"
            seed_masks.append((label, mask_from_seed(panel, params)))

    if not cooccurrence_only:
        in_pool = cases[cases["in_pre_release_pool"]].copy() if len(cases) else cases
        if len(in_pool):
            in_pool["miss_source"] = "streak_scan"

        miss_from_streak = (
            build_miss_list(
                in_pool, lane_defs, panel, min_ret_3d=miss_min_ret, seed_masks=seed_masks or None
            )
            if bool(miss_cfg.get("lane_tiers", True))
            else build_miss_list(in_pool, [], panel, min_ret_3d=miss_min_ret, seed_masks=seed_masks)
        )

        miss_june = pd.DataFrame()
        if bool(miss_cfg.get("include_june2026_lead3", True)):
            miss_june = build_june2026_miss_supplement(
                panel,
                lane_defs,
                min_ret_3d=miss_min_ret,
                seed_masks=seed_masks or None,
                conn=conn,
            )
        miss = merge_miss_frames(miss_from_streak, miss_june)

        if len(in_pool):
            lagv = in_pool[
                (in_pool["end_q"] == "lagging")
                & (in_pool["prev_end_q"] == "lagging")
                & (in_pool["sig_ret"] < 0)
            ]
            imp_long = in_pool[
                (in_pool["end_q"] == "improving") & (in_pool["improve_streak"] >= 7)
            ]
            imp_pb = in_pool[
                (in_pool["end_q"] == "improving")
                & (in_pool["improve_streak"].between(4, 6))
                & (in_pool["sig_ret"] <= -2)
            ]
        else:
            lagv = imp_long = imp_pb = in_pool

        profiles = [
            _profile_cluster(in_pool, "all_in_pool"),
            _profile_cluster(lagv, "type_lagv"),
            _profile_cluster(imp_long, "type_h3_long_streak"),
            _profile_cluster(imp_pb, "type_imppb"),
            _profile_cluster(miss, "miss_list_strong"),
        ]

    # Jaccard vs greedy pick among passing seeds
    greedy: list[dict[str, Any]] = []
    day_sets: list[set[str]] = []
    jaccard_max = float(sweep.get("jaccard_max", 0.32))
    if len(passed):
        seed_keys = {
            "seed_id", "bench_min", "bench_max", "sig_lo", "sig_hi", "rv_lo", "rv_hi",
            "streak_lo", "streak_hi", "end_q", "prev_end_q", "ex_lo", "ex_hi",
            "require_elag_plag", "require_excess_band", "require_gap1", "require_disp", "require_hist",
        }
        for _, row in passed.sort_values("mean_3d", ascending=False).iterrows():
            params = {k: row[k] for k in seed_keys if k in row.index and pd.notna(row[k])}
            m = mask_from_seed(panel, params)
            if exclusion_tags:
                m = apply_exclusion_mask(m, panel, exclusion_tags)
            ds = calendar_day_set(panel[m])
            if day_sets and max(jaccard(ds, o) for o in day_sets) > jaccard_max:
                continue
            greedy.append(row.to_dict())
            day_sets.append(ds)
            if len(greedy) >= 8:
                break

    today = date.today().isoformat()
    miss_path = out_dir / f"lane_discovery_miss_list_{today.replace('-', '')}.json"
    case_path = out_dir / f"lane_discovery_case_facts_{today.replace('-', '')}.json"
    cooc_path = out_dir / f"lane_discovery_cooccurrence_{today.replace('-', '')}.json"
    sweep_path = out_dir / f"lane_discovery_sweep_{today.replace('-', '')}.json"
    md_path = out_dir / f"lane_discovery_loop_{today.replace('-', '')}.md"

    miss_path.write_text(
        json.dumps(_json_safe(miss.to_dict(orient="records")), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    case_path.write_text(
        json.dumps(_json_safe(cases.to_dict(orient="records")), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    payload = {
        "meta": {
            "topic_id": TOPIC_ID,
            "generated_on": today,
            "pool_n": len(panel),
            "years_span": _years_span(panel["date"]),
            "lane_count": len(lane_defs),
        },
        "cohort_scan": {
            "n_streaks": len(cases),
            "n_in_pool": len(in_pool),
            "n_miss_strong": len(miss),
            "n_miss_streak_scan": len(miss_from_streak),
            "n_miss_june2026_lead3": len(miss_june),
            "profiles": profiles,
        },
        "cooccurrence": {
            **cooc_result,
            "exclusion_ablation": exclusion_ab,
        },
        "sweep": {
            "n_cells": len(sweep_results),
            "n_pattern_seeds": len(pattern_seeds),
            "n_pass_all": int(len(passed)),
            "greedy_selected": greedy,
            "top_by_mean": sweep_df.sort_values("mean_3d", ascending=False).head(20).to_dict(orient="records")
            if len(sweep_df)
            else [],
        },
        "artifacts": {
            "miss_list": str(miss_path.relative_to(PROJECT_ROOT)),
            "case_facts": str(case_path.relative_to(PROJECT_ROOT)),
        },
    }
    cooc_path.write_text(
        json.dumps(_json_safe(cooc_result), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    sweep_path.write_text(
        json.dumps(_json_safe(payload), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    md_path.write_text(_render_md(payload, sweep_df, miss, cooc_result, exclusion_ab), encoding="utf-8")
    payload["artifacts"]["cooccurrence_json"] = str(cooc_path.relative_to(PROJECT_ROOT))
    payload["artifacts"]["report_md"] = str(md_path.relative_to(PROJECT_ROOT))
    payload["artifacts"]["sweep_json"] = str(sweep_path.relative_to(PROJECT_ROOT))
    return payload


def _render_md(
    payload: dict[str, Any],
    sweep_df: pd.DataFrame,
    miss: pd.DataFrame,
    cooc_result: dict[str, Any] | None = None,
    exclusion_ab: list[dict[str, Any]] | None = None,
) -> str:
    meta = payload["meta"]
    cs = payload["cohort_scan"]
    sw = payload["sweep"]
    lines = [
        "# RRG Lane Discovery Loop",
        "",
        f"- **Topic**: `{TOPIC_ID}` · **Generated**: {meta['generated_on']}",
        f"- **Pool**: {meta['pool_n']} stock-days · **Registered lanes**: {meta['lane_count']}",
        f"- **Outcome**: `ret_3d` · **Loss gate**: ret_3d > {LOSS5_PCT}% · path min > {LOSS8_PCT}%",
        "",
        "## 1. Outcome cohort（3 連漲 · 排除大賠）",
        "",
        f"- 掃描命中 **{cs['n_streaks']}** 段 streak · pre-release 池內 **{cs['n_in_pool']}**",
        f"- 強 ret_3d 但未命中任何 lane：**{cs['n_miss_strong']}** → miss-list",
        f"  - streak_scan：**{cs.get('n_miss_streak_scan', 0)}** · june2026_lead3：**{cs.get('n_miss_june2026_lead3', 0)}**",
        "",
        "### 子族群 profile",
        "",
        "| 族群 | n | ret_3d mean | sig_ret mean | bench mean | end_q 主要 |",
        "|------|---|-------------|--------------|------------|------------|",
    ]
    for p in cs["profiles"]:
        lines.append(
            f"| {p['label']} | {p['n']} | {p.get('ret_3d_mean', '—')} | "
            f"{p.get('sig_ret_mean', '—')} | {p.get('bench_today_ret_mean', '—')} | "
            f"{p.get('end_q_top', {})} |"
        )

    lines.extend(["", "## 2. 全月份 signal-day 共現（success · ret_3d≥3% · 排除極端）", ""])
    if cooc_result:
        cs = cooc_result.get("cohort_summary") or {}
        lines.append(
            f"- 母池 signal-day **{cs.get('n_total', '—')}** · success **{cs.get('n_success', '—')}** · "
            f"loss **{cs.get('n_loss', '—')}** · 跨 **{cs.get('n_months', '—')}** 個月"
        )
        excl = cooc_result.get("active_exclusion_tags") or []
        lines.append(f"- **Exclusion gate tags**：`{', '.join(excl) if excl else '—'}`")
        lines.extend(["", "### 月度 success 摘要（有 success 的月份）", ""])
        lines.append("| month | n_total | n_success | n_loss | top_tags |")
        lines.append("|-------|---------|-----------|--------|----------|")
        for mp in cooc_result.get("monthly_profiles") or []:
            if not mp.get("n_success"):
                continue
            tops = ", ".join(mp.get("top_tags_success") or [])[:60]
            lines.append(
                f"| {mp['month']} | {mp['n_total']} | {mp['n_success']} | {mp['n_loss']} | {tops} |"
            )
        lines.extend(["", "### 共現 pattern（≥2 次 · lift≥門檻 · 跨維）", ""])
        lines.append("| pattern | n_success | lift | n_months | months |")
        lines.append("|---------|-----------|------|----------|--------|")
        for p in (cooc_result.get("cooccurrence_pairs") or [])[:20]:
            months = ",".join((p.get("months") or [])[:4])
            if len(p.get("months") or []) > 4:
                months += "…"
            lines.append(
                f"| {p['pattern']} | {p['n_success']} | {p['lift']} | {p['n_months']} | {months} |"
            )
        if exclusion_ab:
            lines.extend(["", "### Exclusion ablation（pattern seed · 有/無 exclusion）", ""])
            lines.append("| seed | pattern | mean w/o | mean w/ | loss5 w/o | loss5 w/ |")
            lines.append("|------|---------|----------|---------|-----------|----------|")
            for row in exclusion_ab:
                lines.append(
                    f"| {row.get('seed_id')} | {row.get('pattern')} | "
                    f"{row.get('mean_without_excl'):+.2f}% | {row.get('mean_with_excl'):+.2f}% | "
                    f"{row.get('loss5_without', 0):.1%} | {row.get('loss5_with_excl', 0):.1%} |"
                )
    else:
        lines.append("（未執行 cooccurrence）")

    lines.extend(["", "## 3. Preregistered + co-occurrence sweep", ""])
    if len(sweep_df):
        top = sweep_df.sort_values("mean_3d", ascending=False).head(15)
        lines.append("| seed | n | ev/yr | win_3d | mean_3d | loss5 | pass |")
        lines.append("|------|---|-------|--------|---------|-------|------|")
        for _, r in top.iterrows():
            lines.append(
                f"| {r.get('seed_id', '?')} | {int(r['n'])} | {r['ev_yr']:.1f} | "
                f"{r['win_3d']:.1%} | {r['mean_3d']:+.2f}% | {r['loss5_rate']:.1%} | "
                f"{'✅' if r.get('pass_all') else '—'} |"
            )
    else:
        lines.append("（未執行 sweep）")

    lines.extend(["", "## 4. Greedy 低 Jaccard 選取", ""])
    for i, g in enumerate(sw.get("greedy_selected") or [], 1):
        lines.append(
            f"- **G{i:02d}** `{g.get('seed_id')}` · mean={g.get('mean_3d'):+.2f}% · "
            f"win={g.get('win_3d', 0):.1%} · ev/yr={g.get('ev_yr')} · loss5={g.get('loss5_rate', 0):.1%}"
        )

    if len(miss):
        lines.extend(["", "## 5. Miss-list 摘要（top ret_3d）", ""])
        lines.append("| sid | signal_date | source | streak_total% | ret_3d | end_q | sig_ret | streak |")
        lines.append("|-----|-------------|--------|---------------|--------|-------|---------|--------|")
        for _, r in miss.sort_values("ret_3d", ascending=False).head(20).iterrows():
            lines.append(
                f"| {r['sid']} | {r.get('signal_date', '—')} | {r.get('miss_source', '—')} | "
                f"{r.get('streak_total_pct', '—')} | "
                f"{r.get('ret_3d', '—')} | {r.get('end_q', '—')} | {r.get('sig_ret', '—')} | "
                f"{r.get('improve_streak', '—')} |"
            )

    lines.extend(
        [
            "",
            "## 6. 下一輪假說",
            "",
            "- **Co-occurrence seeds（CO01+）**：共現 ≥2 次 · 跨維 tag pair → 自動 sweep",
            "- **Exclusion gate**：loss lift 高 tag 從 candidate 剔除，不事後刪樣本",
            "- **Miss-driven**：miss-list 新 end_q × bench 組合",
            "",
        ]
    )
    return "\n".join(lines)
