"""ABC v3+F1 · prior-5d range × day-up entry filter (inductive → deductive).

Inductive (explore, no pre-commitment):
  - range5% = (max high − min low) / close(T−1) over sessions T−5..T−1
  - range5_atr = range5% / ATR14%(ending T−1)
  - day_up% = entry px / close(T−1) − 1
  - bins, 2×2, correlations, gap/open variants

Deductive (seg1 discovers thresholds; seg2+seg3 OOS):
  - H-ENTRY-RANGE-DAYUP-1: high prior range ∧ day_up lifts champion TP-only
    hold_5d mean ≥ +2pp vs baseline (n≥30 full; positive Δ in ≥2/3 segments
    with n≥10; OOS mean Δ > 0 after dropping top-3 winners optional check)
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any, Callable

from research.backtest.abc_v3_f1_entry_multifactor_tp_sweep import CHAMPION_COMBO
from research.backtest.archive.abc_v3_f1_kinematic_tp_sl_study import evaluate_leg_tp_sl
from research.backtest.finpilot_local_backtest import load_price_panels

SCHEMA = "abc_v3_f1_range_dayup-v1"

SEGMENTS: tuple[tuple[str, str, str], ...] = (
    ("seg1", "2024-01-02", "2024-10-31"),
    ("seg2", "2024-11-01", "2025-08-31"),
    ("seg3", "2025-09-01", "2026-07-02"),
)

# Inductive bin edges for raw range5%.
RANGE5_EDGES: tuple[float, ...] = (0.0, 5.0, 8.0, 10.0, 12.0, 15.0, 20.0, 25.0, 100.0)

# Deductive discovery grid (seg1 only).
RANGE_THR_GRID: tuple[float, ...] = (8.0, 10.0, 12.0, 15.0)
RANGE_ATR_THR_GRID: tuple[float, ...] = (2.0, 2.5, 3.0, 3.5, 4.0)
DAY_UP_THR_GRID: tuple[float, ...] = (0.0, 0.5, 1.0)


def _mean(xs: list[float]) -> float | None:
    return round(sum(xs) / len(xs), 3) if xs else None


def _median(xs: list[float]) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    n = len(s)
    mid = n // 2
    if n % 2:
        return round(s[mid], 3)
    return round((s[mid - 1] + s[mid]) / 2.0, 3)


def _win_rate(xs: list[float]) -> float | None:
    if not xs:
        return None
    return round(100.0 * sum(1 for x in xs if x > 0) / len(xs), 1)


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 2:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx < 1e-12 or vy < 1e-12:
        return None
    return round(cov / (vx * vy) ** 0.5, 4)


def _summarize(rets: list[float], *, baseline: float | None) -> dict[str, Any]:
    mean = _mean(rets)
    trimmed = sorted(rets)[:-3] if len(rets) > 3 else list(rets)
    return {
        "n": len(rets),
        "mean_pct": mean,
        "median_pct": _median(rets),
        "win_rate_pct": _win_rate(rets),
        "delta_vs_baseline_pp": (
            round(mean - baseline, 3) if mean is not None and baseline is not None else None
        ),
        "mean_drop_top3_pct": _mean(trimmed) if len(rets) > 3 else mean,
        "delta_drop_top3_pp": (
            round(_mean(trimmed) - baseline, 3)
            if len(rets) > 3 and baseline is not None and _mean(trimmed) is not None
            else None
        ),
    }


def _load_ohlc_lookup(conn: sqlite3.Connection) -> tuple[dict[tuple[str, str], float], ...]:
    rows = conn.execute(
        """
        SELECT trade_date, stock_id, open, high, low, source
        FROM stock_daily_bars
        ORDER BY trade_date, stock_id,
          CASE source WHEN 'finmind' THEN 0 WHEN 'yfinance' THEN 1 ELSE 2 END
        """
    ).fetchall()
    open_m: dict[tuple[str, str], float] = {}
    high_m: dict[tuple[str, str], float] = {}
    low_m: dict[tuple[str, str], float] = {}
    seen: set[tuple[str, str]] = set()
    for trade_date, stock_id, opn, high, low, _src in rows:
        key = (str(stock_id), str(trade_date)[:10])
        if key in seen:
            continue
        seen.add(key)
        if opn is not None:
            open_m[key] = float(opn)
        if high is not None:
            high_m[key] = float(high)
        if low is not None:
            low_m[key] = float(low)
    return open_m, high_m, low_m


def _atr14_pct(
    *,
    sid: str,
    dates: list[str],
    ei: int,
    close,
    high_m: dict[tuple[str, str], float],
    low_m: dict[tuple[str, str], float],
    period: int = 14,
) -> float | None:
    """ATR14% ending at T−1 (PIT). Needs period+1 closes for true range."""
    if ei < period + 1:
        return None
    # windows ending at ei-1
    trs: list[float] = []
    for j in range(ei - period, ei):
        d = dates[j]
        prev = dates[j - 1]
        try:
            h = high_m[(sid, d)]
            l = low_m[(sid, d)]
            c_prev = float(close.at[prev, sid])
            c = float(close.at[d, sid])
        except Exception:
            return None
        if any(x != x or x is None for x in (h, l, c_prev, c)) or c_prev <= 0 or c <= 0:
            return None
        trs.append(max(h - l, abs(h - c_prev), abs(l - c_prev)))
    if len(trs) < period:
        return None
    c_end = float(close.at[dates[ei - 1], sid])
    if c_end != c_end or c_end <= 0:
        return None
    return (sum(trs) / period) / c_end * 100.0


def annotate_legs_range_dayup(
    conn: sqlite3.Connection,
    legs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    close, _opn, _vol = load_price_panels(conn)
    close = close.copy()
    close.index = close.index.astype(str)
    open_m, high_m, low_m = _load_ohlc_lookup(conn)
    dates = list(close.index)
    di = {d: i for i, d in enumerate(dates)}
    out: list[dict[str, Any]] = []
    for leg in legs:
        sid = str(leg["stock_id"])
        entry = str(leg["entry_date"])
        if sid not in close.columns or entry not in di:
            continue
        ei = di[entry]
        if ei < 5:
            continue
        win = dates[ei - 5 : ei]
        try:
            hs = [high_m[(sid, d)] for d in win]
            ls = [low_m[(sid, d)] for d in win]
            c_prev = float(close.at[dates[ei - 1], sid])
            o_t = open_m[(sid, entry)]
        except KeyError:
            continue
        if c_prev != c_prev or c_prev <= 0 or o_t != o_t or o_t <= 0:
            continue
        hi5, lo5 = max(hs), min(ls)
        range5 = (hi5 - lo5) / c_prev * 100.0
        pos = None if hi5 == lo5 else (c_prev - lo5) / (hi5 - lo5) * 100.0
        atr = _atr14_pct(
            sid=sid, dates=dates, ei=ei, close=close, high_m=high_m, low_m=low_m
        )
        range5_atr = (range5 / atr) if atr is not None and atr > 1e-9 else None
        ret5 = None
        if ei >= 6:
            c6 = float(close.at[dates[ei - 6], sid])
            if c6 == c6 and c6 > 0:
                ret5 = (c_prev / c6 - 1.0) * 100.0
        t0 = leg.get("entry_t0") or {}
        px = t0.get("px")
        if px is None:
            continue
        px = float(px)
        day_up = (px / c_prev - 1.0) * 100.0
        from_open = (px / o_t - 1.0) * 100.0
        gap_open = (o_t / c_prev - 1.0) * 100.0
        tp = float(evaluate_leg_tp_sl(leg, CHAMPION_COMBO)["exit_ret_pct"])
        out.append(
            {
                "stock_id": sid,
                "entry_date": entry,
                "entry_minute": t0.get("minute"),
                "tp_only_ret_pct": tp,
                "range5_pct": round(range5, 4),
                "range5_atr": None if range5_atr is None else round(range5_atr, 4),
                "atr14_pct": None if atr is None else round(atr, 4),
                "pos_in_range_pct": None if pos is None else round(pos, 2),
                "day_up_pct": round(day_up, 4),
                "from_open_pct": round(from_open, 4),
                "gap_open_pct": round(gap_open, 4),
                "ret5_pct": None if ret5 is None else round(ret5, 4),
                "spread_dist": t0.get("spread_dist"),
                "gap_mv_w20_w5": t0.get("gap_mv_w20_w5"),
            }
        )
    return out


def _bin_rows(
    rows: list[dict[str, Any]],
    *,
    key: str,
    edges: tuple[float, ...],
    baseline: float | None,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i in range(len(edges) - 1):
        lo, hi = edges[i], edges[i + 1]
        rets = [
            float(r["tp_only_ret_pct"])
            for r in rows
            if r.get(key) is not None and lo <= float(r[key]) < hi
        ]
        if not rets:
            continue
        out.append({"bin": f"[{lo},{hi})", "lo": lo, "hi": hi, **_summarize(rets, baseline=baseline)})
    return out


def _two_by_two(
    rows: list[dict[str, Any]],
    *,
    vol_key: str,
    up_key: str = "day_up_pct",
    baseline: float | None,
) -> dict[str, Any]:
    usable = [r for r in rows if r.get(vol_key) is not None and r.get(up_key) is not None]
    if len(usable) < 20:
        return {"n": len(usable), "cells": []}
    med = float(_median([float(r[vol_key]) for r in usable]) or 0.0)
    cells: list[dict[str, Any]] = []
    for hi_vol in (True, False):
        for up in (True, False):
            rets = [
                float(r["tp_only_ret_pct"])
                for r in usable
                if (float(r[vol_key]) >= med) == hi_vol and (float(r[up_key]) > 0) == up
            ]
            cells.append(
                {
                    "vol": "HI" if hi_vol else "LO",
                    "day_up": "Y" if up else "N",
                    "vol_split": med,
                    **_summarize(rets, baseline=baseline),
                }
            )
    return {"n": len(usable), "vol_median_split": med, "cells": cells}


def _variant_eval(
    rows: list[dict[str, Any]],
    *,
    vid: str,
    label: str,
    pred: Callable[[dict[str, Any]], bool],
    baseline: float | None,
    min_n: int,
    lift_pp: float,
) -> dict[str, Any]:
    sub = [float(r["tp_only_ret_pct"]) for r in rows if pred(r)]
    mean = _mean(sub)
    summary = _summarize(sub, baseline=baseline)
    return {
        "id": vid,
        "label": label,
        **summary,
        "passes_gate": bool(
            len(sub) >= min_n
            and mean is not None
            and baseline is not None
            and (mean - baseline) >= lift_pp
        ),
    }


def _segment_stability(
    rows: list[dict[str, Any]],
    *,
    vid: str,
    pred: Callable[[dict[str, Any]], bool],
    min_seg_n: int = 10,
) -> dict[str, Any]:
    hits = 0
    detail: list[dict[str, Any]] = []
    for seg_id, d0, d1 in SEGMENTS:
        pool = [r for r in rows if d0 <= str(r["entry_date"]) <= d1]
        base = _mean([float(r["tp_only_ret_pct"]) for r in pool])
        sub = [float(r["tp_only_ret_pct"]) for r in pool if pred(r)]
        m = _mean(sub)
        dlt = round(m - base, 3) if m is not None and base is not None else None
        ok = len(sub) >= min_seg_n and dlt is not None and dlt > 0
        if ok:
            hits += 1
        detail.append(
            {
                "seg": seg_id,
                "n": len(sub),
                "pool_n": len(pool),
                "mean_pct": m,
                "baseline_pct": base,
                "delta_pp": dlt,
                "positive": ok,
            }
        )
    return {
        "variant_id": vid,
        "positive_segments": hits,
        "segments": detail,
        "stable": hits >= 2,
    }


def _discover_on_seg1(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Rank combo thresholds by seg1 Δ (inductive → candidate shortlist)."""
    seg1 = [r for r in rows if SEGMENTS[0][1] <= str(r["entry_date"]) <= SEGMENTS[0][2]]
    base = _mean([float(r["tp_only_ret_pct"]) for r in seg1])
    cands: list[dict[str, Any]] = []

    for rmin in RANGE_THR_GRID:
        for dup in DAY_UP_THR_GRID:
            pred = lambda r, rr=rmin, dd=dup: (
                r.get("range5_pct") is not None
                and float(r["range5_pct"]) >= rr
                and float(r["day_up_pct"]) > dd
            )
            sub = [float(r["tp_only_ret_pct"]) for r in seg1 if pred(r)]
            if len(sub) < 5:
                continue
            m = _mean(sub)
            cands.append(
                {
                    "family": "range5",
                    "range_thr": rmin,
                    "day_up_thr": dup,
                    "id": f"r>={rmin:g}&up>{dup:g}",
                    "n_seg1": len(sub),
                    "mean_seg1": m,
                    "delta_seg1_pp": round(m - base, 3) if m is not None and base is not None else None,
                }
            )

    for amin in RANGE_ATR_THR_GRID:
        for dup in DAY_UP_THR_GRID:
            pred = lambda r, aa=amin, dd=dup: (
                r.get("range5_atr") is not None
                and float(r["range5_atr"]) >= aa
                and float(r["day_up_pct"]) > dd
            )
            sub = [float(r["tp_only_ret_pct"]) for r in seg1 if pred(r)]
            if len(sub) < 5:
                continue
            m = _mean(sub)
            cands.append(
                {
                    "family": "range5_atr",
                    "range_atr_thr": amin,
                    "day_up_thr": dup,
                    "id": f"atr>={amin:g}&up>{dup:g}",
                    "n_seg1": len(sub),
                    "mean_seg1": m,
                    "delta_seg1_pp": round(m - base, 3) if m is not None and base is not None else None,
                }
            )

    cands.sort(
        key=lambda c: (
            c.get("delta_seg1_pp") is not None,
            c.get("delta_seg1_pp") or -999,
            int(c.get("n_seg1") or 0),
        ),
        reverse=True,
    )
    return cands


def _pred_from_cand(c: dict[str, Any]) -> Callable[[dict[str, Any]], bool]:
    if c["family"] == "range5":
        rr = float(c["range_thr"])
        dd = float(c["day_up_thr"])
        return lambda r: (
            r.get("range5_pct") is not None
            and float(r["range5_pct"]) >= rr
            and float(r["day_up_pct"]) > dd
        )
    aa = float(c["range_atr_thr"])
    dd = float(c["day_up_thr"])
    return lambda r: (
        r.get("range5_atr") is not None
        and float(r["range5_atr"]) >= aa
        and float(r["day_up_pct"]) > dd
    )


def run_range_dayup_study(
    conn: sqlite3.Connection,
    legs: list[dict[str, Any]],
    *,
    legs_source: str = "",
    min_bucket_n: int = 30,
    lift_pp_gate: float = 2.0,
    annotated: list[dict[str, Any]] | None = None,
    top_k_deductive: int = 6,
) -> dict[str, Any]:
    rows = annotated if annotated is not None else annotate_legs_range_dayup(conn, legs)
    rets = [float(r["tp_only_ret_pct"]) for r in rows]
    baseline = _mean(rets)

    # --- Inductive ---
    corr_range = _pearson(
        [float(r["range5_pct"]) for r in rows],
        [float(r["tp_only_ret_pct"]) for r in rows],
    )
    atr_rows = [r for r in rows if r.get("range5_atr") is not None]
    corr_atr = _pearson(
        [float(r["range5_atr"]) for r in atr_rows],
        [float(r["tp_only_ret_pct"]) for r in atr_rows],
    )
    up_rows = [r for r in rows if float(r["day_up_pct"]) > 0]
    corr_range_given_up = _pearson(
        [float(r["range5_pct"]) for r in up_rows],
        [float(r["tp_only_ret_pct"]) for r in up_rows],
    )

    inductive = {
        "corr_range5_vs_ret": corr_range,
        "corr_range5_atr_vs_ret": corr_atr,
        "corr_range5_vs_ret_given_day_up": corr_range_given_up,
        "range5_bins": _bin_rows(rows, key="range5_pct", edges=RANGE5_EDGES, baseline=baseline),
        "range5_atr_terciles": _terciles(rows, key="range5_atr", baseline=baseline),
        "day_up_alone": [
            _variant_eval(
                rows,
                vid=vid,
                label=label,
                pred=pred,
                baseline=baseline,
                min_n=min_bucket_n,
                lift_pp=lift_pp_gate,
            )
            for vid, label, pred in (
                ("day_up>0", "進場價>T−1收", lambda r: float(r["day_up_pct"]) > 0),
                ("day_up>0.5", "進場價>T−1收 +0.5%", lambda r: float(r["day_up_pct"]) > 0.5),
                ("day_up>1", "進場價>T−1收 +1%", lambda r: float(r["day_up_pct"]) > 1.0),
                ("day_up<=0", "進場價≤T−1收", lambda r: float(r["day_up_pct"]) <= 0),
                ("from_open>0", "進場價>開盤", lambda r: float(r["from_open_pct"]) > 0),
            )
        ],
        "two_by_two_range5": _two_by_two(rows, vol_key="range5_pct", baseline=baseline),
        "two_by_two_range5_atr": _two_by_two(rows, vol_key="range5_atr", baseline=baseline),
        "pattern_notes": [
            "高振幅 × 當日相對昨收上漲 是 2×2 中唯一明顯偏強的格子",
            "低振幅即使當日上漲亦偏弱 → 不是單純 momentum day",
            "raw range5 與 ATR 正規化都要進演繹；seg1 定標、後段 OOS",
        ],
    }

    # --- Deductive ---
    discovery = _discover_on_seg1(rows)
    shortlist = discovery[:top_k_deductive]

    # Always include prior exploratory favorite as named control.
    controls = [
        {
            "family": "range5",
            "range_thr": 12.0,
            "day_up_thr": 0.0,
            "id": "r>=12&up>0",
            "label": "exploratory control (pre-study)",
        },
        {
            "family": "range5",
            "range_thr": 10.0,
            "day_up_thr": 0.5,
            "id": "r>=10&up>0.5",
            "label": "milder range + stronger day_up",
        },
    ]

    deductive_variants: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for c in controls + shortlist:
        vid = str(c["id"])
        if vid in seen_ids:
            continue
        seen_ids.add(vid)
        pred = _pred_from_cand(c)
        label = c.get("label") or f"seg1-discovered {vid}"
        full = _variant_eval(
            rows,
            vid=vid,
            label=label,
            pred=pred,
            baseline=baseline,
            min_n=min_bucket_n,
            lift_pp=lift_pp_gate,
        )
        stab = _segment_stability(rows, vid=vid, pred=pred)
        oos = [r for r in rows if str(r["entry_date"]) >= SEGMENTS[1][1]]
        oos_base = _mean([float(r["tp_only_ret_pct"]) for r in oos])
        oos_eval = _variant_eval(
            oos,
            vid=vid + "_oos",
            label="OOS seg2+seg3",
            pred=pred,
            baseline=oos_base,
            min_n=15,
            lift_pp=0.0,  # OOS: require positive Δ only for soft pass
        )
        full_proto = bool(
            full.get("passes_gate")
            and stab.get("stable")
            and (oos_eval.get("delta_vs_baseline_pp") or -999) > 0
            and int(oos_eval.get("n") or 0) >= 15
        )
        # Robustness: mean uplift must survive dropping the top-3 winners.
        robust = bool(
            full_proto
            and full.get("delta_drop_top3_pp") is not None
            and float(full["delta_drop_top3_pp"]) >= 1.0
        )
        deductive_variants.append(
            {
                **full,
                "discovery": {
                    k: c.get(k)
                    for k in (
                        "family",
                        "range_thr",
                        "range_atr_thr",
                        "day_up_thr",
                        "delta_seg1_pp",
                        "n_seg1",
                    )
                },
                "stability": stab,
                "oos_seg23": oos_eval,
                "passes_full_protocol": full_proto,
                "passes_robust_drop_top3": robust,
            }
        )

    any_robust = any(bool(v.get("passes_robust_drop_top3")) for v in deductive_variants)
    any_pass = any(bool(v.get("passes_full_protocol")) for v in deductive_variants)
    soft_pass = any(
        bool(v.get("passes_gate")) and bool(v.get("stability", {}).get("stable"))
        for v in deductive_variants
    )
    best = max(
        deductive_variants,
        key=lambda v: (
            bool(v.get("passes_robust_drop_top3")),
            bool(v.get("passes_full_protocol")),
            bool(v.get("passes_gate")),
            v.get("delta_vs_baseline_pp") is not None,
            v.get("delta_vs_baseline_pp") or -999,
            int(v.get("n") or 0),
        ),
        default=None,
    )

    if any_robust:
        verdict = "passed_candidate"
    elif any_pass or soft_pass:
        # Mechanical gate/OOS ok but fat-tail fragile (drop-top3 < +1pp).
        verdict = "mixed"
    else:
        verdict = "rejected"

    dates = sorted({str(r["entry_date"]) for r in rows})
    return {
        "schema": SCHEMA,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "legs_source": legs_source,
        "n_input_legs": len(legs),
        "n_annotated": len(rows),
        "n_stocks": len({str(r["stock_id"]) for r in rows}),
        "date_start": dates[0] if dates else None,
        "date_end": dates[-1] if dates else None,
        "exit": "champion TP-only · NEVER_SL · hold_5d cap",
        "baseline_mean_tp_pct": baseline,
        "min_bucket_n": min_bucket_n,
        "lift_pp_gate": lift_pp_gate,
        "method": {
            "inductive": "bins / 2×2 / correlations / day_up alone (no threshold commit)",
            "deductive": "seg1 discovers thr grid → full + 3-seg stability + OOS seg2+seg3",
            "range5_def": "(max high − min low) / close(T−1) over T−5..T−1",
            "range5_atr_def": "range5% / ATR14%(ending T−1)",
            "day_up_def": "entry_t0.px / close(T−1) − 1",
        },
        "inductive": inductive,
        "deductive": {
            "seg1_discovery_ranked": discovery[:20],
            "variants": deductive_variants,
            "best_variant": best,
        },
        "any_variant_passes_full_protocol": any_pass,
        "any_variant_passes_robust_drop_top3": any_robust,
        "verdict": verdict,
        "hypothesis_id": "H-ENTRY-RANGE-DAYUP-1",
    }


def _terciles(
    rows: list[dict[str, Any]],
    *,
    key: str,
    baseline: float | None,
) -> list[dict[str, Any]]:
    usable = [r for r in rows if r.get(key) is not None]
    if len(usable) < 9:
        return []
    ordered = sorted(usable, key=lambda r: float(r[key]))
    n = len(ordered)
    t = n // 3
    bands = (("bot", ordered[:t]), ("mid", ordered[t : n - t]), ("top", ordered[n - t :]))
    out: list[dict[str, Any]] = []
    for bid, chunk in bands:
        rets = [float(r["tp_only_ret_pct"]) for r in chunk]
        vals = [float(r[key]) for r in chunk]
        out.append(
            {
                "id": bid,
                f"{key}_min": round(min(vals), 4),
                f"{key}_max": round(max(vals), 4),
                **_summarize(rets, baseline=baseline),
            }
        )
    return out


# Pre-registered closeout variants (H-ENTRY-RANGE-DAYUP-2). No new thr search.
CLOSEOUT_SPECS: tuple[tuple[str, str, Callable[[dict[str, Any]], bool]], ...] = (
    ("r10_up0", "range≥10 & day_up>0", lambda r: float(r["range5_pct"]) >= 10 and float(r["day_up_pct"]) > 0),
    ("r12_up0", "range≥12 & day_up>0", lambda r: float(r["range5_pct"]) >= 12 and float(r["day_up_pct"]) > 0),
    ("r15_up0", "range≥15 & day_up>0", lambda r: float(r["range5_pct"]) >= 15 and float(r["day_up_pct"]) > 0),
    (
        "r12_up_band_3",
        "range≥12 & 0<day_up≤3（限幅追價）",
        lambda r: float(r["range5_pct"]) >= 12
        and 0 < float(r["day_up_pct"]) <= 3.0,
    ),
    (
        "r12_up_band_2",
        "range≥12 & 0<day_up≤2",
        lambda r: float(r["range5_pct"]) >= 12
        and 0 < float(r["day_up_pct"]) <= 2.0,
    ),
    (
        "r12_up0_range_cap20",
        "range∈[12,20) & day_up>0（去掉極端振幅）",
        lambda r: 12 <= float(r["range5_pct"]) < 20 and float(r["day_up_pct"]) > 0,
    ),
    (
        "r12_up0_gap_le2",
        "range≥12 & day_up>0 & gap_open≤2%",
        lambda r: float(r["range5_pct"]) >= 12
        and float(r["day_up_pct"]) > 0
        and float(r.get("gap_open_pct") or 99) <= 2.0,
    ),
    (
        "r12_up0_from_open",
        "range≥12 & day_up>0 & from_open>0",
        lambda r: float(r["range5_pct"]) >= 12
        and float(r["day_up_pct"]) > 0
        and float(r["from_open_pct"]) > 0,
    ),
)


def run_range_dayup_closeout(
    conn: sqlite3.Connection,
    legs: list[dict[str, Any]],
    *,
    legs_source: str = "",
    min_bucket_n: int = 30,
    lift_pp_gate: float = 2.0,
    robust_drop3_pp: float = 1.0,
    annotated: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Goal-aligned closeout: de-fat the HI-vol×day_up clue or reject the line.

    Product goal reminder: ABC entry overlays are frozen (H-ENTRY-STACK-1);
    this line only continues if a *robust* observe gate survives drop-top3.
    Otherwise close and pivot to Order-layer champion TP (O3-5).
    """
    rows = annotated if annotated is not None else annotate_legs_range_dayup(conn, legs)
    rets = [float(r["tp_only_ret_pct"]) for r in rows]
    baseline = _mean(rets)
    baseline_med = _median(rets)

    variants: list[dict[str, Any]] = []
    for vid, label, pred in CLOSEOUT_SPECS:
        full = _variant_eval(
            rows,
            vid=vid,
            label=label,
            pred=pred,
            baseline=baseline,
            min_n=min_bucket_n,
            lift_pp=lift_pp_gate,
        )
        stab = _segment_stability(rows, vid=vid, pred=pred)
        oos = [r for r in rows if str(r["entry_date"]) >= SEGMENTS[1][1]]
        oos_base = _mean([float(r["tp_only_ret_pct"]) for r in oos])
        oos_eval = _variant_eval(
            oos,
            vid=vid + "_oos",
            label="OOS seg2+seg3",
            pred=pred,
            baseline=oos_base,
            min_n=15,
            lift_pp=0.0,
        )
        med_ok = (
            full.get("median_pct") is not None
            and baseline_med is not None
            and float(full["median_pct"]) > float(baseline_med)
        )
        full_proto = bool(
            full.get("passes_gate")
            and stab.get("stable")
            and (oos_eval.get("delta_vs_baseline_pp") or -999) > 0
            and int(oos_eval.get("n") or 0) >= 15
        )
        robust = bool(
            full_proto
            and full.get("delta_drop_top3_pp") is not None
            and float(full["delta_drop_top3_pp"]) >= robust_drop3_pp
            and med_ok
        )
        variants.append(
            {
                **full,
                "label": label,
                "median_beats_baseline": med_ok,
                "stability": stab,
                "oos_seg23": oos_eval,
                "passes_full_protocol": full_proto,
                "passes_robust_closeout": robust,
            }
        )

    any_robust = any(bool(v.get("passes_robust_closeout")) for v in variants)
    any_full = any(bool(v.get("passes_full_protocol")) for v in variants)
    best = max(
        variants,
        key=lambda v: (
            bool(v.get("passes_robust_closeout")),
            bool(v.get("passes_full_protocol")),
            v.get("delta_drop_top3_pp") is not None,
            v.get("delta_drop_top3_pp") or -999,
            v.get("delta_vs_baseline_pp") or -999,
        ),
        default=None,
    )

    if any_robust:
        verdict = "passed_observe"
        next_step = (
            "Optional weak observe tag on live abc-v3-f1-pullback pool only; "
            "do not change frozen strategy stack. Primary next work remains "
            "Order-layer champion TP (O3-5)."
        )
    else:
        verdict = "rejected"
        next_step = (
            "Close H-ENTRY-RANGE-DAYUP line. No further entry thr grids. "
            "Next: Order-layer abc-v3-f1-champion-tp shadow (docs/order-layer-prd.md §17.2 / O3-5)."
        )

    dates = sorted({str(r["entry_date"]) for r in rows})
    return {
        "schema": "abc_v3_f1_range_dayup_closeout-v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "legs_source": legs_source,
        "n_annotated": len(rows),
        "date_start": dates[0] if dates else None,
        "date_end": dates[-1] if dates else None,
        "exit": "champion TP-only · NEVER_SL · hold_5d cap",
        "baseline_mean_tp_pct": baseline,
        "baseline_median_tp_pct": baseline_med,
        "min_bucket_n": min_bucket_n,
        "lift_pp_gate": lift_pp_gate,
        "robust_drop3_pp": robust_drop3_pp,
        "goal": (
            "Improve ABC deliverable quality without breaking H-ENTRY-STACK-1 "
            "(raw F1 entry + champion TP). Entry overlay must be robust or we pivot "
            "to Order-layer champion TP wiring."
        ),
        "hypothesis_id": "H-ENTRY-RANGE-DAYUP-2",
        "variants": variants,
        "best_variant": best,
        "any_passes_robust_closeout": any_robust,
        "any_passes_full_protocol": any_full,
        "verdict": verdict,
        "next_step": next_step,
    }


def render_range_dayup_closeout_md(payload: dict[str, Any]) -> str:
    lines = [
        "# ABC v3+F1 · range×day_up closeout（去肥尾 / 目標對齊）",
        "",
        f"> **Verdict: {payload.get('verdict')}** · legs **{payload.get('n_annotated')}** · "
        f"**{payload.get('date_start')}** .. **{payload.get('date_end')}**",
        f"> baseline mean **{payload.get('baseline_mean_tp_pct')}%** · "
        f"median **{payload.get('baseline_median_tp_pct')}%** · "
        f"假說 **{payload.get('hypothesis_id')}**",
        "",
        "## 目標與本步角色",
        "",
        f"{payload.get('goal')}",
        "",
        "本步只回答：探索表上的「波動大 + 當天還在漲」能否在 **drop-top3 Δ≥+1pp** "
        "且 **median > baseline** 後仍成立。失敗則關掉 entry 線。",
        "",
        "## 預註冊變體（不再搜 thr）",
        "",
        "| id | n | mean% | med% | Δpp | drop3 Δ | med>base | segs+ | OOS Δ | full | robust |",
        "|----|--:|------:|-----:|----:|--------:|:--------:|------:|------:|:----:|:------:|",
    ]
    for v in payload.get("variants") or []:
        stab = v.get("stability") or {}
        oos = v.get("oos_seg23") or {}
        lines.append(
            f"| {v.get('id')} | {v.get('n')} | {v.get('mean_pct')} | {v.get('median_pct')} | "
            f"{v.get('delta_vs_baseline_pp')} | {v.get('delta_drop_top3_pp')} | "
            f"{'Y' if v.get('median_beats_baseline') else 'N'} | "
            f"{stab.get('positive_segments')}/3 | {oos.get('delta_vs_baseline_pp')} | "
            f"{'Y' if v.get('passes_full_protocol') else 'N'} | "
            f"{'Y' if v.get('passes_robust_closeout') else 'N'} |"
        )
    best = payload.get("best_variant") or {}
    lines += [
        "",
        f"**Best by robust:** `{best.get('id')}` · drop3 Δ={best.get('delta_drop_top3_pp')} · "
        f"robust={'Y' if best.get('passes_robust_closeout') else 'N'}",
        "",
        "## Verdict / 下一步",
        "",
        f"- Verdict: **{payload.get('verdict')}**",
        f"- Next: {payload.get('next_step')}",
        "",
        f"Artifact schema: `{payload.get('schema')}`",
        "",
    ]
    return "\n".join(lines)


def render_range_dayup_md(payload: dict[str, Any]) -> str:
    ind = payload.get("inductive") or {}
    ded = payload.get("deductive") or {}
    lines = [
        "# ABC v3+F1 · 前五日振幅 × 當日上漲（歸納 → 演繹）",
        "",
        f"> **Verdict: {payload.get('verdict')}** · legs **{payload.get('n_annotated')}** · "
        f"窗口 **{payload.get('date_start')}** .. **{payload.get('date_end')}**",
        f"> baseline TP-only **{payload.get('baseline_mean_tp_pct')}%** · "
        f"出場：{payload.get('exit')}",
        f"> 假說 **{payload.get('hypothesis_id')}** · gate Δ≥**{payload.get('lift_pp_gate')}pp** · n≥**{payload.get('min_bucket_n')}**",
        "",
        "## 方法",
        "",
        "- **歸納**：range5 / range5÷ATR14 分箱、當日上漲單因子、2×2、相關（不定標）。",
        "- **演繹**：僅用 **seg1** 搜 thr → 全樣本 gate + 3 段穩定 + **OOS seg2+seg3**。",
        "- `range5%` = (maxH−minL)/close(T−1)，T−5..T−1；`day_up` = 進場價/close(T−1)−1。",
        "",
        "## 一、歸納結果",
        "",
        f"- corr(range5, ret) = **{ind.get('corr_range5_vs_ret')}**",
        f"- corr(range5_atr, ret) = **{ind.get('corr_range5_atr_vs_ret')}**",
        f"- corr(range5, ret | day_up>0) = **{ind.get('corr_range5_vs_ret_given_day_up')}**",
        "",
        "### range5% 分箱",
        "",
        "| bin | n | mean% | med% | win% | Δpp | Δ drop-top3 |",
        "|-----|--:|------:|-----:|-----:|----:|------------:|",
    ]
    for b in ind.get("range5_bins") or []:
        lines.append(
            f"| {b.get('bin')} | {b.get('n')} | {b.get('mean_pct')} | {b.get('median_pct')} | "
            f"{b.get('win_rate_pct')} | {b.get('delta_vs_baseline_pp')} | {b.get('delta_drop_top3_pp')} |"
        )
    lines += ["", "### 當日上漲單因子", "", "| id | n | mean% | Δpp | pass |", "|----|--:|------:|----:|:----:|"]
    for v in ind.get("day_up_alone") or []:
        lines.append(
            f"| {v.get('id')} | {v.get('n')} | {v.get('mean_pct')} | {v.get('delta_vs_baseline_pp')} | "
            f"{'Y' if v.get('passes_gate') else 'N'} |"
        )
    lines += ["", "### 2×2（range5 中位切）", ""]
    tw = ind.get("two_by_two_range5") or {}
    lines.append(f"split @ **{tw.get('vol_median_split')}%**")
    lines += ["", "| vol | up | n | mean% | Δpp |", "|-----|----|--:|------:|----:|"]
    for c in tw.get("cells") or []:
        lines.append(
            f"| {c.get('vol')} | {c.get('day_up')} | {c.get('n')} | {c.get('mean_pct')} | {c.get('delta_vs_baseline_pp')} |"
        )
    lines += ["", "### range5_atr 三分位", "", "| tercile | n | mean% | Δpp |", "|---------|--:|------:|----:|"]
    for t in ind.get("range5_atr_terciles") or []:
        lines.append(
            f"| {t.get('id')} | {t.get('n')} | {t.get('mean_pct')} | {t.get('delta_vs_baseline_pp')} |"
        )

    lines += [
        "",
        "## 二、演繹結果（seg1 定標 → 驗證）",
        "",
        "### seg1 discovery top",
        "",
        "| id | family | n_seg1 | Δ_seg1 |",
        "|----|--------|-------:|-------:|",
    ]
    for c in (ded.get("seg1_discovery_ranked") or [])[:10]:
        lines.append(
            f"| {c.get('id')} | {c.get('family')} | {c.get('n_seg1')} | {c.get('delta_seg1_pp')} |"
        )

    lines += [
        "",
        "### 候選驗證",
        "",
        "| id | n | mean% | Δpp | drop3 Δ | gate | segs+ | OOS Δ | full | robust |",
        "|----|--:|------:|----:|--------:|:----:|------:|------:|:----:|:------:|",
    ]
    for v in ded.get("variants") or []:
        stab = v.get("stability") or {}
        oos = v.get("oos_seg23") or {}
        lines.append(
            f"| {v.get('id')} | {v.get('n')} | {v.get('mean_pct')} | {v.get('delta_vs_baseline_pp')} | "
            f"{v.get('delta_drop_top3_pp')} | {'Y' if v.get('passes_gate') else 'N'} | "
            f"{stab.get('positive_segments')}/3 | {oos.get('delta_vs_baseline_pp')} | "
            f"{'Y' if v.get('passes_full_protocol') else 'N'} | "
            f"{'Y' if v.get('passes_robust_drop_top3') else 'N'} |"
        )

    best = ded.get("best_variant") or {}
    lines += [
        "",
        f"**Best:** `{best.get('id')}` · Δ={best.get('delta_vs_baseline_pp')}pp · "
        f"full={'Y' if best.get('passes_full_protocol') else 'N'} · "
        f"robust={'Y' if best.get('passes_robust_drop_top3') else 'N'}",
        "",
        "## 三、Verdict",
        "",
    ]
    if payload.get("verdict") == "passed_candidate":
        lines.append(
            "至少一組通過 full protocol 且 drop-top3 Δ≥+1pp。"
            "可進 observe；尚未採納進 strategy。"
        )
    elif payload.get("verdict") == "mixed":
        lines.append(
            "機械協議（+2pp / 穩定 / OOS）可能通過，但 **drop-top3 後 uplift 塌縮** "
            "或穩定度不齊 → fat-tail 依賴。observe-only，**不採納**。"
        )
    else:
        lines.append(
            "無候選通過演繹協議。高振幅×當日漲的歸納圖案存在，但不足以當 entry gate。"
        )
    lines += ["", f"Artifact schema: `{payload.get('schema')}`", ""]
    return "\n".join(lines)
