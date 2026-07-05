"""H-A1 · Dual WMA intraday spread score vs drs outcomes (rest-of-day & 3-day hold).

Hypothesis: intraday extension + W20∈Improving + spread_rs>0 predicts same-day
remaining ΔRSR (WMA20) better than prior-close daily drs3d score.

Entry signals: imp_extension · imp_early_long · lead_pullback_buy (comparison).
Primary outcomes: drs_rest_d · drs_3d (WMA20 RS-Ratio) · ret_3d (price, net of cost).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from market_benchmark import load_benchmark_close
from rank_stats import spearman_correlation
from research.backtest.dual_wma_signal_backtest import (
    DEFAULT_POLL_END,
    ROUND_TRIP_COST_PCT,
    EntryParams,
    _build_stock_frame_maps,
    _collect_events,
    _exit_frame_idx,
    _price_at,
)
from research.backtest.finpilot_local_backtest import load_price_panels
from research.backtest.rrg_drs3d_score_backtest import (
    _rsr_proximity_proxy,
    _spearman_pvalue,
    compute_drs3d_score,
)
from rrg_mono_daily_brief import _feat
from rrg_rotation import DEFAULT_LENGTH, compute_rrg_panel
from rrg_universe_intraday_panel import build_dual_wma_intraday_trajectories
from stock_db import PROJECT_ROOT

HOLD_DAYS = 3
DEFAULT_EXIT_RULE = "hold_3d"

ENTRY_SIGNALS: tuple[str, ...] = (
    "imp_extension",
    "imp_early_long",
    "lead_pullback_buy",
)

# H-A1 strict imp_extension: W20 improving · extension · spread_rs>0
IMP_EXTENSION_STRICT = EntryParams(signal="imp_extension", first_per_day=True)


@dataclass(frozen=True)
class SignalICResult:
    signal: str
    n: int
    ic_intraday_vs_drs_rest: float | None
    ic_daily_vs_drs_rest: float | None
    ic_intraday_vs_drs_3d: float | None
    ic_daily_vs_drs_3d: float | None
    p_intraday_rest: float
    p_daily_rest: float
    mean_drs_rest_d: float
    mean_drs_3d: float
    mean_ret_3d_net: float
    win_rate_3d: float
    flip_leading_3d_rate: float


def _matches_imp_extension_strict(p: dict[str, Any]) -> bool:
    """H-A1: W20∈Improving · extension · spread_rs>0."""
    if not p.get("fresh"):
        return False
    w20 = p.get("w20") or {}
    if w20.get("quadrant") != "improving":
        return False
    if p.get("spread_class") != "extension":
        return False
    return float(p.get("spread_rs") or 0) > 0


def compute_intraday_spread_score(point: dict[str, Any]) -> float:
    """PIT intraday score from true spread_rs / spread_mom / spread_class."""
    w20 = point.get("w20") or {}
    rsr = float(w20.get("rs_ratio") or float("nan"))
    srs = float(point.get("spread_rs") or 0)
    sms = float(point.get("spread_mom") or 0)
    dist = float(point.get("spread_dist") or 0)
    sc = point.get("spread_class")

    prox = _rsr_proximity_proxy(rsr)
    ext_bonus = 0.0
    if sc == "extension" and srs > 0:
        ext_bonus = 0.45
    elif sc == "aligned":
        ext_bonus = 0.15

    raw = (
        prox * 1.0
        + min(max(srs, 0) / 2.0, 1.0) * 0.55
        + min(max(sms, 0) / 2.0, 1.0) * 0.35
        + ext_bonus
        + min(dist / 4.0, 1.0) * 0.15
    )
    return round(max(raw, 0.0), 6)


def _eod_frame_idx(entry_idx: int, frames: list[dict[str, str]]) -> int:
    entry_date = frames[entry_idx]["date"]
    last = entry_idx
    for j in range(entry_idx + 1, len(frames)):
        if frames[j]["date"] == entry_date:
            last = j
        else:
            break
    return last


def _w20_rsr(point: dict[str, Any] | None) -> float:
    if not point:
        return float("nan")
    w20 = point.get("w20") or {}
    v = float(w20.get("rs_ratio") or float("nan"))
    return v


def _w20_quadrant(point: dict[str, Any] | None) -> str | None:
    if not point:
        return None
    w20 = point.get("w20") or {}
    return w20.get("quadrant")


def _daily_score_at_entry(
    sid: str,
    entry_date: str,
    full_dates: list[str],
    date_idx: dict[str, int],
    rs_ratio: pd.DataFrame,
    rs_mom: pd.DataFrame,
    cache: dict[tuple[str, str], float],
) -> float:
    """Prior-close PIT daily score for intraday entry on entry_date."""
    key = (sid, entry_date)
    if key in cache:
        return cache[key]
    si = date_idx.get(entry_date)
    if si is None or si < 4 or sid not in rs_ratio.columns:
        cache[key] = float("nan")
        return float("nan")
    feat_si = si - 1
    f = _feat(rs_ratio, rs_mom, full_dates, feat_si, sid)
    if not f:
        cache[key] = float("nan")
        return float("nan")
    feat_d = full_dates[feat_si]
    row: dict[str, Any] = {
        **f,
        "rsr_d": f["rs_ratio"],
        "rv": f["rs_ratio"],
        "drs_rsr_1d": float("nan"),
        "drs_rsr_4d": float("nan"),
        "sig_ret": 0.0,
        "disp_contract": False,
    }
    if feat_si >= 1:
        rv_prev = float(rs_ratio.at[full_dates[feat_si - 1], sid])
        rv_cur = float(rs_ratio.at[feat_d, sid])
        if np.isfinite(rv_prev) and np.isfinite(rv_cur):
            row["drs_rsr_1d"] = rv_cur - rv_prev
    dr4_start = feat_si - 3
    if dr4_start >= 0:
        rv0 = float(rs_ratio.at[full_dates[dr4_start], sid])
        rv1 = float(rs_ratio.at[feat_d, sid])
        if np.isfinite(rv0) and np.isfinite(rv1):
            row["drs_rsr_4d"] = rv1 - rv0
    score = compute_drs3d_score(row)
    cache[key] = score
    return score


def _collect_signal_events(
    trajectories: list[dict],
    frames: list[dict[str, str]],
    signal: str,
    *,
    strict_extension: bool = False,
) -> list[tuple[str, int]]:
    if signal == "imp_extension" and strict_extension:
        frame_ids = [f["id"] for f in frames]
        events: list[tuple[str, int]] = []
        seen: set[tuple[str, str]] = set()
        for t in trajectories:
            sid = t["stock_id"]
            by_id = {p["frame_id"]: p for p in t["points"]}
            for i, fid in enumerate(frame_ids):
                p = by_id.get(fid)
                if not p or not _matches_imp_extension_strict(p):
                    continue
                d = frames[i]["date"]
                key = (sid, d)
                if key in seen:
                    continue
                seen.add(key)
                events.append((sid, i))
        return events
    ep = EntryParams(signal=signal, first_per_day=True)
    return _collect_events(trajectories, frames, ep)


def build_intraday_event_panel(
    conn: sqlite3.Connection,
    *,
    dates: list[str],
    etf_codes: tuple[str, ...],
    signal: str = "imp_extension",
    exit_rule: str = DEFAULT_EXIT_RULE,
    strict_extension: bool = False,
    _cache: dict[str, Any] | None = None,
) -> pd.DataFrame:
    if _cache is not None:
        frames = _cache["frames"]
        trajectories = _cache["trajectories"]
        bench = _cache["bench"]
        stock_maps = _cache["stock_maps"]
        daily_ctx = _cache["daily_ctx"]
    else:
        frames, trajectories, _meta = build_dual_wma_intraday_trajectories(
            conn, dates=dates, etf_codes=etf_codes
        )
        close, _, _ = load_price_panels(conn)
        bench = load_benchmark_close(conn).reindex(close.index).astype(float)
        rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench, length=DEFAULT_LENGTH)
        full_dates = close.index.astype(str).tolist()
        date_idx = {d: i for i, d in enumerate(full_dates)}
        stock_maps = _build_stock_frame_maps(trajectories, frames)
        daily_ctx = {
            "full_dates": full_dates,
            "date_idx": date_idx,
            "rs_ratio": rs_ratio,
            "rs_mom": rs_mom,
            "score_cache": {},
        }
        _cache = {
            "frames": frames,
            "trajectories": trajectories,
            "bench": bench,
            "stock_maps": stock_maps,
            "daily_ctx": daily_ctx,
        }

    events = _collect_signal_events(
        trajectories, frames, signal, strict_extension=strict_extension
    )
    rows: list[dict[str, Any]] = []

    for sid, ei in events:
        pts = stock_maps.get(sid)
        if not pts:
            continue
        p_entry = pts[ei]
        if not p_entry:
            continue
        eod_idx = _eod_frame_idx(ei, frames)
        exit_idx = _exit_frame_idx(exit_rule, ei, frames, pts)
        p_eod = pts[eod_idx]
        p_exit = pts[exit_idx]

        rsr_entry = _w20_rsr(p_entry)
        rsr_eod = _w20_rsr(p_eod)
        rsr_exit = _w20_rsr(p_exit)
        if not all(np.isfinite(v) for v in (rsr_entry, rsr_eod, rsr_exit)):
            continue

        ef, xf = frames[ei], frames[exit_idx]
        entry_px, entry_b = _price_at(conn, bench, sid, ef["date"], ef["minute"])
        exit_px, exit_b = _price_at(conn, bench, sid, xf["date"], xf["minute"])
        if not entry_px or not exit_px or entry_px <= 0 or exit_px <= 0:
            continue

        gross = (exit_px / entry_px - 1) * 100
        net = gross - ROUND_TRIP_COST_PCT
        bench_ret = (exit_b / entry_b - 1) * 100 if entry_b and exit_b else 0.0

        rows.append(
            {
                "stock_id": sid,
                "signal": signal,
                "entry_date": ef["date"],
                "entry_minute": ef["minute"],
                "exit_date": xf["date"],
                "exit_minute": xf["minute"],
                "spread_class": p_entry.get("spread_class"),
                "spread_rs": float(p_entry.get("spread_rs") or 0),
                "spread_mom": float(p_entry.get("spread_mom") or 0),
                "spread_dist": float(p_entry.get("spread_dist") or 0),
                "w20_quadrant": _w20_quadrant(p_entry),
                "w20_rsr": round(rsr_entry, 3),
                "intraday_score": compute_intraday_spread_score(p_entry),
                "daily_score": _daily_score_at_entry(
                    sid,
                    ef["date"],
                    daily_ctx["full_dates"],
                    daily_ctx["date_idx"],
                    daily_ctx["rs_ratio"],
                    daily_ctx["rs_mom"],
                    daily_ctx["score_cache"],
                ),
                "drs_rest_d": round(rsr_eod - rsr_entry, 4),
                "drs_3d": round(rsr_exit - rsr_entry, 4),
                "ret_3d_gross": round(gross, 4),
                "ret_3d_net": round(net, 4),
                "excess_3d_net": round(net - bench_ret, 4),
                "flip_leading_3d": _w20_quadrant(p_exit) == "leading",
                "cross_100_3d": rsr_entry < 100.0 <= rsr_exit,
                "hold_polls": exit_idx - ei,
                "eod_polls": eod_idx - ei,
            }
        )

    return pd.DataFrame(rows)


def evaluate_signal_ic(panel: pd.DataFrame, *, signal: str) -> SignalICResult:
    sub = panel[panel["signal"] == signal] if "signal" in panel.columns else panel
    sub = sub.dropna(subset=["intraday_score", "daily_score", "drs_rest_d", "drs_3d"])
    n = len(sub)
    if n < 5:
        return SignalICResult(
            signal=signal,
            n=n,
            ic_intraday_vs_drs_rest=None,
            ic_daily_vs_drs_rest=None,
            ic_intraday_vs_drs_3d=None,
            ic_daily_vs_drs_3d=None,
            p_intraday_rest=1.0,
            p_daily_rest=1.0,
            mean_drs_rest_d=0.0,
            mean_drs_3d=0.0,
            mean_ret_3d_net=0.0,
            win_rate_3d=0.0,
            flip_leading_3d_rate=0.0,
        )

    ic_ir = spearman_correlation(
        sub["intraday_score"].tolist(), sub["drs_rest_d"].tolist()
    )
    ic_dr = spearman_correlation(
        sub["daily_score"].tolist(), sub["drs_rest_d"].tolist()
    )
    ic_i3 = spearman_correlation(
        sub["intraday_score"].tolist(), sub["drs_3d"].tolist()
    )
    ic_d3 = spearman_correlation(
        sub["daily_score"].tolist(), sub["drs_3d"].tolist()
    )

    return SignalICResult(
        signal=signal,
        n=n,
        ic_intraday_vs_drs_rest=round(ic_ir, 4) if ic_ir is not None else None,
        ic_daily_vs_drs_rest=round(ic_dr, 4) if ic_dr is not None else None,
        ic_intraday_vs_drs_3d=round(ic_i3, 4) if ic_i3 is not None else None,
        ic_daily_vs_drs_3d=round(ic_d3, 4) if ic_d3 is not None else None,
        p_intraday_rest=_spearman_pvalue(ic_ir or 0.0, n),
        p_daily_rest=_spearman_pvalue(ic_dr or 0.0, n),
        mean_drs_rest_d=round(float(sub["drs_rest_d"].mean()), 4),
        mean_drs_3d=round(float(sub["drs_3d"].mean()), 4),
        mean_ret_3d_net=round(float(sub["ret_3d_net"].mean()), 4),
        win_rate_3d=round(float((sub["ret_3d_net"] > 0).mean()), 4),
        flip_leading_3d_rate=round(float(sub["flip_leading_3d"].mean()), 4),
    )


def run_h_a1_backtest(
    conn: sqlite3.Connection,
    *,
    dates: list[str],
    etf_codes: tuple[str, ...],
    signals: tuple[str, ...] = ENTRY_SIGNALS,
    strict_extension: bool = True,
) -> dict[str, Any]:
    frames, trajectories, meta = build_dual_wma_intraday_trajectories(
        conn, dates=dates, etf_codes=etf_codes
    )
    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index).astype(float)
    rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench, length=DEFAULT_LENGTH)
    full_dates = close.index.astype(str).tolist()
    date_idx = {d: i for i, d in enumerate(full_dates)}
    cache = {
        "frames": frames,
        "trajectories": trajectories,
        "bench": bench,
        "stock_maps": _build_stock_frame_maps(trajectories, frames),
        "daily_ctx": {
            "full_dates": full_dates,
            "date_idx": date_idx,
            "rs_ratio": rs_ratio,
            "rs_mom": rs_mom,
            "score_cache": {},
        },
    }

    parts: list[pd.DataFrame] = []
    ic_rows: list[SignalICResult] = []
    for sig in signals:
        use_strict = strict_extension and sig == "imp_extension"
        df = build_intraday_event_panel(
            conn,
            dates=dates,
            etf_codes=etf_codes,
            signal=sig,
            strict_extension=use_strict,
            _cache=cache,
        )
        if not df.empty:
            parts.append(df)
        ic_rows.append(evaluate_signal_ic(df, signal=sig))

    panel = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    ext_panel = panel[panel["signal"] == "imp_extension"] if len(panel) else panel

    deciles: list[dict[str, Any]] = []
    if len(ext_panel) >= 10:
        ext = ext_panel.copy()
        ext["decile"] = pd.qcut(
            ext["intraday_score"].rank(method="first"),
            10,
            labels=False,
            duplicates="drop",
        ) + 1
        for d, g in ext.groupby("decile", observed=True):
            deciles.append(
                {
                    "decile": int(d),
                    "n": len(g),
                    "mean_intraday_score": round(float(g["intraday_score"].mean()), 4),
                    "mean_drs_rest_d": round(float(g["drs_rest_d"].mean()), 4),
                    "mean_drs_3d": round(float(g["drs_3d"].mean()), 4),
                    "mean_ret_3d_net": round(float(g["ret_3d_net"].mean()), 4),
                    "flip_rate": round(float(g["flip_leading_3d"].mean()), 4),
                }
            )

    ext_ic = next((r for r in ic_rows if r.signal == "imp_extension"), None)
    h_a1_pass = (
        ext_ic is not None
        and ext_ic.ic_intraday_vs_drs_rest is not None
        and ext_ic.ic_daily_vs_drs_rest is not None
        and ext_ic.ic_intraday_vs_drs_rest > ext_ic.ic_daily_vs_drs_rest
    )

    return {
        "meta": {
            **meta,
            "date_start": dates[0] if dates else "",
            "date_end": dates[-1] if dates else "",
            "n_trading_days": len(dates),
            "generated_on": date.today().isoformat(),
            "hold_days": HOLD_DAYS,
            "exit_rule": DEFAULT_EXIT_RULE,
            "strict_imp_extension": strict_extension,
        },
        "ic_by_signal": [asdict(r) for r in ic_rows],
        "h_a1_intraday_beats_daily_rest": h_a1_pass,
        "deciles_imp_extension": deciles,
        "events": panel.to_dict(orient="records") if len(panel) else [],
        "summary": {
            "n_events_total": len(panel),
            "n_imp_extension": int((panel["signal"] == "imp_extension").sum()) if len(panel) else 0,
            "n_imp_early_long": int((panel["signal"] == "imp_early_long").sum()) if len(panel) else 0,
            "n_lead_pullback": int((panel["signal"] == "lead_pullback_buy").sum()) if len(panel) else 0,
        },
    }


def render_h_a1_markdown(result: dict[str, Any]) -> str:
    meta = result.get("meta", {})
    lines = [
        "# H-A1 · Dual WMA intraday spread score vs drs outcomes",
        "",
        f"> **Hypothesis**: intraday extension + W20∈Improving + spread_rs>0 predicts "
        f"same-day remaining ΔRSR better than prior-close daily score.",
        "",
        f"- Period: **{meta.get('date_start')} → {meta.get('date_end')}** "
        f"({meta.get('n_trading_days')} trading days)",
        f"- Hold: **{meta.get('hold_days')}d** · exit `{meta.get('exit_rule')}` · "
        f"strict imp_extension: **{meta.get('strict_imp_extension')}**",
        f"- Events: **{result.get('summary', {}).get('n_events_total', 0)}**",
        "",
        "## A. IC comparison (intraday spread score vs prior-close daily score)",
        "",
        "| Signal | n | IC→drs_rest | IC→drs_3d | daily IC→rest | daily IC→3d | "
        "H-A1 rest | mean drs_rest | mean drs_3d | mean ret_3d net | win% | P(flip) |",
        "|--------|---|-------------|-----------|---------------|-------------|"
        "-----------|---------------|-------------|-----------------|------|---------|",
    ]
    for r in result.get("ic_by_signal", []):
        beat = "✓" if (
            r.get("ic_intraday_vs_drs_rest") is not None
            and r.get("ic_daily_vs_drs_rest") is not None
            and r["ic_intraday_vs_drs_rest"] > r["ic_daily_vs_drs_rest"]
        ) else "—"
        lines.append(
            f"| {r['signal']} | {r['n']} | "
            f"{r.get('ic_intraday_vs_drs_rest', '—')} | {r.get('ic_intraday_vs_drs_3d', '—')} | "
            f"{r.get('ic_daily_vs_drs_rest', '—')} | {r.get('ic_daily_vs_drs_3d', '—')} | "
            f"{beat} | {r.get('mean_drs_rest_d', 0):+.3f} | {r.get('mean_drs_3d', 0):+.3f} | "
            f"{r.get('mean_ret_3d_net', 0):+.2f}% | {r.get('win_rate_3d', 0):.1%} | "
            f"{r.get('flip_leading_3d_rate', 0):.1%} |"
        )

    ha1 = "✅" if result.get("h_a1_intraday_beats_daily_rest") else "❌"
    lines.extend(
        [
            "",
            f"**H-A1 (imp_extension · rest-of-day)**: intraday IC > daily IC → {ha1}",
            "",
            "## B. imp_extension deciles (intraday_score)",
            "",
        ]
    )
    dec = result.get("deciles_imp_extension") or []
    if dec:
        lines.extend(
            [
                "| D | n | score | drs_rest | drs_3d | ret_3d net | P(flip) |",
                "|---|---|-------|----------|--------|------------|---------|",
            ]
        )
        for d in dec:
            lines.append(
                f"| {d['decile']} | {d['n']} | {d['mean_intraday_score']:.3f} | "
                f"{d['mean_drs_rest_d']:+.3f} | {d['mean_drs_3d']:+.3f} | "
                f"{d['mean_ret_3d_net']:+.2f}% | {d['flip_rate']:.1%} |"
            )
    else:
        lines.append("_Insufficient imp_extension events for deciles._")

    lines.extend(
        [
            "",
            "## C. Notes",
            "",
            "- `drs_rest_d` = WMA(20) RSR from entry frame → same-day 13:30.",
            "- `drs_3d` / `ret_3d_net` = entry frame → D+3 13:30 (hold_3d).",
            "- `daily_score` = prior-close PIT `compute_drs3d_score` (fair vs intraday entry).",
            "- Cost round-trip **0.585%** deducted from ret_3d_net.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_h_a1_report(
    result: dict[str, Any],
    out_dir: Path | None = None,
    *,
    stamp: str | None = None,
) -> tuple[Path, Path]:
    out_dir = out_dir or (PROJECT_ROOT / "reports" / "research" / "rrg")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = stamp or date.today().strftime("%Y%m%d")
    json_path = out_dir / f"dual_wma_drs3d_intraday_{stamp}.json"
    md_path = out_dir / f"dual_wma_drs3d_intraday_{stamp}.md"
    payload = {**result, "events": result.get("events", [])[:500]}
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_h_a1_markdown(result), encoding="utf-8")
    return json_path, md_path
