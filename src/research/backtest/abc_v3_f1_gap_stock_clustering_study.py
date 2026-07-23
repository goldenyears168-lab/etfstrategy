"""ABC v3+F1 · gap_w20_w5 cross-stock clustering study (RP-3).

Background: the entry factor scan (H-ENTRY-NORM-1) found a sign flip on the
MV gap W20-W5 feature — `gap_w20_w5_raw` correlates POSITIVELY (+0.069
Spearman) with forward hold-days return across stocks, while the per-stock
self-normalized `gap_w20_w5_z` correlates NEGATIVELY (−0.097). This suggests
the "bigger absolute gap = better entry" effect may be a *which-kind-of-stock*
structural effect rather than a *when-to-enter* timing effect.

This module tests that hypothesis directly:

1. Build stock-level features from PRE-EVENT history only (no lookahead —
   everything is computed strictly before each stock's FIRST event date):
   avg/std of the stock's own daily EOD gap_w20_w5 series, average daily
   turnover, ATR%, plus the event count as a control variable.
2. Split the stock universe into terciles per feature (quantile groups, not
   k-means, for interpretability).
3. Re-run the factor correlation scan (scan_factor_correlations) inside each
   group to see where the raw-gap effect concentrates.
4. Decompose the overall raw-gap correlation into between-stock vs
   within-stock components, and re-check with one-event-per-stock sampling —
   this controls for a few heavily-sampled stocks dominating the pooled rho.

All Spearman rhos are reported with 95% CIs (Fisher z, Bonett–Wright SE
approximation for Spearman) so small-group estimates are not over-read.
"""

from __future__ import annotations

import math
import sqlite3
from datetime import datetime
from typing import Any

from project_config import DEFAULT_ETF_CODES
from research.backtest.abc_v3_f1_entry_factor_scan import (
    _spearman,
    attach_normalized_gate_features,
    build_stock_daily_gap_rv_series,
    scan_factor_correlations,
)
from research.backtest.abc_v3_f1_entry_gate_tp_sweep import collect_abc_v3_f1_raw_legs
from research.backtest.finpilot_local_backtest import load_price_panels

GAP_FACTORS: tuple[str, ...] = ("gap_w20_w5_raw", "gap_w20_w5_z", "gap_w20_w5_pct")

CLUSTER_FEATURES: tuple[str, ...] = (
    "avg_gap_w20_w5",
    "std_gap_w20_w5",
    "adv_turnover_60d",
    "atr_pct_20d",
)

TERCILE_LABELS: tuple[str, ...] = ("low", "mid", "high")
EVENT_COUNT_LABELS: tuple[str, ...] = ("1", "2-3", "4+")


# ---------------------------------------------------------------------------
# statistics helpers
# ---------------------------------------------------------------------------


def spearman_ci95(rho: float | None, n: int) -> tuple[float, float] | None:
    """95% CI for Spearman rho via Fisher z with the Bonett–Wright (2000)
    standard error sqrt((1 + rho^2/2) / (n - 3))."""
    if rho is None or n < 4:
        return None
    r = max(-0.999999, min(0.999999, float(rho)))
    z = math.atanh(r)
    se = math.sqrt((1.0 + r * r / 2.0) / (n - 3))
    lo = math.tanh(z - 1.96 * se)
    hi = math.tanh(z + 1.96 * se)
    return (round(lo, 4), round(hi, 4))


def _scan_with_ci(
    legs: list[dict[str, Any]],
    *,
    factor_cols: tuple[str, ...] = GAP_FACTORS,
    min_n: int = 20,
) -> list[dict[str, Any]]:
    rows = scan_factor_correlations(legs, factor_cols=factor_cols, min_n=min_n)
    for row in rows:
        if row.get("skipped"):
            continue
        ci = spearman_ci95(row.get("spearman_rho"), int(row.get("n") or 0))
        row["spearman_ci95"] = list(ci) if ci else None
    # keep a stable factor order instead of |rho| sort for group tables
    order = {c: i for i, c in enumerate(factor_cols)}
    rows.sort(key=lambda r: order.get(str(r.get("factor")), 99))
    return rows


# ---------------------------------------------------------------------------
# stock-level features (pre-event history only)
# ---------------------------------------------------------------------------


def _stock_bar_features(
    conn: sqlite3.Connection,
    stock_id: str,
    before_date: str,
    *,
    adv_window: int = 60,
    atr_window: int = 20,
) -> tuple[float | None, float | None]:
    """(avg daily turnover, ATR%) from stock_daily_bars strictly before
    ``before_date``. Turnover uses the amount column when present, else
    volume*close. ATR% = mean(TrueRange / close) * 100 over atr_window."""
    rows = conn.execute(
        "SELECT trade_date, high, low, close, volume, amount FROM stock_daily_bars "
        "WHERE stock_id = ? AND trade_date < ? ORDER BY trade_date DESC LIMIT ?",
        (str(stock_id), str(before_date), max(adv_window, atr_window + 1)),
    ).fetchall()
    if not rows:
        return None, None
    rows = list(reversed(rows))  # chronological

    turnovers: list[float] = []
    for _, _, _, c, v, amt in rows[-adv_window:]:
        if amt is not None and float(amt) > 0:
            turnovers.append(float(amt))
        elif v is not None and c is not None:
            turnovers.append(float(v) * float(c))
    adv = round(sum(turnovers) / len(turnovers), 1) if turnovers else None

    trs: list[float] = []
    for i in range(1, len(rows)):
        _, h, low, c, _, _ = rows[i]
        prev_c = rows[i - 1][3]
        if h is None or low is None or c is None or prev_c is None or float(c) <= 0:
            continue
        tr = max(
            float(h) - float(low),
            abs(float(h) - float(prev_c)),
            abs(float(low) - float(prev_c)),
        )
        trs.append(tr / float(c) * 100.0)
    trs = trs[-atr_window:]
    atr_pct = round(sum(trs) / len(trs), 4) if len(trs) >= max(5, atr_window // 2) else None
    return adv, atr_pct


def build_stock_level_features(
    conn: sqlite3.Connection,
    legs: list[dict[str, Any]],
    baseline: dict[str, list[dict[str, Any]]],
    *,
    gap_obs_window: int = 60,
    min_gap_obs: int = 10,
    adv_window: int = 60,
    atr_window: int = 20,
) -> dict[str, dict[str, Any]]:
    """One feature row per stock. To rule out lookahead for EVERY event of a
    stock, all features use only data strictly before that stock's FIRST
    event date in the sample."""
    by_sid: dict[str, list[dict[str, Any]]] = {}
    for leg in legs:
        by_sid.setdefault(str(leg["stock_id"]), []).append(leg)

    feats: dict[str, dict[str, Any]] = {}
    for sid in sorted(by_sid):
        sid_legs = by_sid[sid]
        first_event = min(str(leg["entry_date"]) for leg in sid_legs)
        series = baseline.get(sid) or []
        gaps = [
            float(r["gap_w20_w5_eod"])
            for r in series
            if r["date"] < first_event and r.get("gap_w20_w5_eod") is not None
        ]
        gaps = gaps[-gap_obs_window:]
        avg_gap: float | None = None
        std_gap: float | None = None
        if len(gaps) >= min_gap_obs:
            mean = sum(gaps) / len(gaps)
            avg_gap = round(mean, 4)
            std_gap = round((sum((g - mean) ** 2 for g in gaps) / len(gaps)) ** 0.5, 4)
        adv, atr_pct = _stock_bar_features(
            conn, sid, first_event, adv_window=adv_window, atr_window=atr_window
        )
        feats[sid] = {
            "stock_id": sid,
            "first_event_date": first_event,
            "n_events": len(sid_legs),
            "n_gap_obs": len(gaps),
            "avg_gap_w20_w5": avg_gap,
            "std_gap_w20_w5": std_gap,
            "adv_turnover_60d": adv,
            "atr_pct_20d": atr_pct,
        }
    return feats


# ---------------------------------------------------------------------------
# grouping
# ---------------------------------------------------------------------------


def assign_quantile_groups(
    feats: dict[str, dict[str, Any]],
    key: str,
    *,
    labels: tuple[str, ...] = TERCILE_LABELS,
    min_stocks: int = 9,
) -> tuple[dict[str, str], list[float] | None]:
    """Tercile assignment (by feature value) across stocks with a non-null
    feature. Ties at a cutpoint fall into the lower group."""
    items = [(sid, float(f[key])) for sid, f in feats.items() if f.get(key) is not None]
    if len(items) < min_stocks:
        return {}, None
    vals = sorted(v for _, v in items)
    n = len(vals)
    q1, q2 = vals[n // 3], vals[(2 * n) // 3]
    groups: dict[str, str] = {}
    for sid, v in items:
        if v <= q1:
            groups[sid] = labels[0]
        elif v <= q2:
            groups[sid] = labels[1]
        else:
            groups[sid] = labels[2]
    return groups, [round(q1, 4), round(q2, 4)]


def assign_event_count_buckets(
    feats: dict[str, dict[str, Any]],
) -> tuple[dict[str, str], list[float] | None]:
    """n_events is heavily tied (many single-event stocks), so use fixed,
    interpretable buckets instead of quantile cuts."""
    groups: dict[str, str] = {}
    for sid, f in feats.items():
        ne = int(f["n_events"])
        groups[sid] = "1" if ne == 1 else ("2-3" if ne <= 3 else "4+")
    return groups, None


def scan_groups(
    legs: list[dict[str, Any]],
    groups: dict[str, str],
    *,
    label_order: tuple[str, ...],
    factor_cols: tuple[str, ...] = GAP_FACTORS,
    min_n: int = 20,
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for label in label_order:
        sids = {sid for sid, lab in groups.items() if lab == label}
        subset = [leg for leg in legs if str(leg["stock_id"]) in sids]
        rets = [float(leg["return_pct"]) for leg in subset]
        out[label] = {
            "n_stocks": len(sids),
            "n_legs": len(subset),
            "mean_return_pct": round(sum(rets) / len(rets), 3) if rets else None,
            "win_rate_pct": (
                round(100.0 * sum(1 for r in rets if r > 0) / len(rets), 1) if rets else None
            ),
            "scan": _scan_with_ci(subset, factor_cols=factor_cols, min_n=min_n),
        }
    return out


# ---------------------------------------------------------------------------
# between-stock vs within-stock decomposition
# ---------------------------------------------------------------------------


def between_within_decomposition(
    legs: list[dict[str, Any]],
    *,
    factor: str = "gap_w20_w5_raw",
    target: str = "return_pct",
) -> dict[str, Any]:
    by_sid: dict[str, list[tuple[str, float, float]]] = {}
    for leg in legs:
        if leg.get(factor) is None:
            continue
        by_sid.setdefault(str(leg["stock_id"]), []).append(
            (str(leg["entry_date"]), float(leg[factor]), float(leg[target]))
        )

    def _entry(rho: float | None, n: int, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        ci = spearman_ci95(rho, n)
        d = {"spearman_rho": rho, "n": n, "spearman_ci95": list(ci) if ci else None}
        if extra:
            d.update(extra)
        return d

    # between-stock: one point per stock (means)
    bx = [sum(x for _, x, _ in v) / len(v) for v in by_sid.values()]
    by = [sum(y for _, _, y in v) / len(v) for v in by_sid.values()]
    between = _entry(_spearman(bx, by), len(bx))

    # within-stock: demeaned per stock, stocks with >= 2 events only
    wx: list[float] = []
    wy: list[float] = []
    n_multi = 0
    for v in by_sid.values():
        if len(v) < 2:
            continue
        n_multi += 1
        mx = sum(x for _, x, _ in v) / len(v)
        my = sum(y for _, _, y in v) / len(v)
        for _, x, y in v:
            wx.append(x - mx)
            wy.append(y - my)
    within = _entry(_spearman(wx, wy), len(wx), {"n_stocks_multi_event": n_multi})

    # first-event-only: strict one-leg-per-stock resample (controls repeat sampling)
    fx: list[float] = []
    fy: list[float] = []
    for v in by_sid.values():
        first = min(v, key=lambda t: t[0])
        fx.append(first[1])
        fy.append(first[2])
    first_only = _entry(_spearman(fx, fy), len(fx))

    return {
        "factor": factor,
        "between_stock": between,
        "within_stock": within,
        "first_event_only": first_only,
    }


# ---------------------------------------------------------------------------
# success-criteria judgement
# ---------------------------------------------------------------------------


def judge_group_scans(
    group_scans: dict[str, dict[str, Any]],
    *,
    focus_factor: str = "gap_w20_w5_raw",
    min_events_per_group: int = 30,
    min_rho_spread: float = 0.15,
) -> list[dict[str, Any]]:
    """Per clustering feature: does the raw-gap effect concentrate in one
    tercile? 'ci_separated' is the strict criterion (best group's CI entirely
    above worst group's CI); 'spread_notable' is the soft one."""
    findings: list[dict[str, Any]] = []
    for feature, spec in group_scans.items():
        rows: dict[str, dict[str, Any]] = {}
        for label, g in (spec.get("groups") or {}).items():
            for r in g.get("scan") or []:
                if r.get("factor") == focus_factor and not r.get("skipped"):
                    rows[label] = {**r, "n_legs": g["n_legs"], "n_stocks": g["n_stocks"]}
        if len(rows) < 2:
            findings.append({"feature": feature, "usable": False, "reason": "too few valid groups"})
            continue
        rhos = {lab: float(r.get("spearman_rho") or 0.0) for lab, r in rows.items()}
        best = max(rhos, key=lambda k: rhos[k])
        worst = min(rhos, key=lambda k: rhos[k])
        spread = round(rhos[best] - rhos[worst], 4)
        best_ci = rows[best].get("spearman_ci95")
        worst_ci = rows[worst].get("spearman_ci95")
        ci_separated = bool(best_ci and worst_ci and best_ci[0] > worst_ci[1])
        groups_meet_min_n = all(
            (g.get("n_legs") or 0) >= min_events_per_group
            for g in (spec.get("groups") or {}).values()
        )
        best_ci_excludes_zero = bool(best_ci and (best_ci[0] > 0 or best_ci[1] < 0))
        findings.append(
            {
                "feature": feature,
                "usable": True,
                "focus_factor": focus_factor,
                "rho_by_group": {k: round(v, 4) for k, v in rhos.items()},
                "best_group": best,
                "worst_group": worst,
                "rho_spread": spread,
                "spread_notable": spread >= min_rho_spread,
                "ci_separated": ci_separated,
                "best_group_ci_excludes_zero": best_ci_excludes_zero,
                "groups_meet_min_n": groups_meet_min_n,
            }
        )
    return findings


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------


def run_gap_stock_clustering_study(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2025-09-01",
    date_end: str = "2026-07-01",
    hold_days: int = 5,
    baseline_window: int = 60,
    min_trailing_days: int = 10,
    group_min_n: int = 20,
    min_events_per_group: int = 30,
    etf_codes: tuple[str, ...] = DEFAULT_ETF_CODES,
) -> dict[str, Any]:
    close, _, _ = load_price_panels(conn)
    full_dates = close.index.astype(str).tolist()
    sample_dates = [d for d in full_dates if date_start <= d <= date_end]
    if not sample_dates:
        raise ValueError(f"no trading dates in [{date_start}, {date_end}]")

    legs = collect_abc_v3_f1_raw_legs(conn, dates=sample_dates, hold_days=hold_days, etf_codes=etf_codes)
    if not legs:
        raise ValueError("no raw ABC-V3-F1 legs in window")

    stock_ids = sorted({str(leg["stock_id"]) for leg in legs})
    earliest_entry = min(str(leg["entry_date"]) for leg in legs)
    try:
        entry_idx = full_dates.index(earliest_entry)
    except ValueError:
        entry_idx = 0
    lookback_start = max(0, entry_idx - baseline_window - 5)
    end_idx = full_dates.index(date_end) if date_end in full_dates else len(full_dates) - 1
    baseline_dates = full_dates[lookback_start : end_idx + 1]

    baseline = build_stock_daily_gap_rv_series(
        conn, stock_ids=stock_ids, dates=baseline_dates, etf_codes=etf_codes
    )
    legs = attach_normalized_gate_features(
        legs, baseline, window=baseline_window, min_trailing=min_trailing_days
    )

    feats = build_stock_level_features(
        conn, legs, baseline, gap_obs_window=baseline_window, min_gap_obs=min_trailing_days
    )

    overall = _scan_with_ci(legs, factor_cols=GAP_FACTORS, min_n=group_min_n)
    decomposition = between_within_decomposition(legs, factor="gap_w20_w5_raw")
    decomposition_z = between_within_decomposition(legs, factor="gap_w20_w5_z")

    group_scans: dict[str, dict[str, Any]] = {}
    for feature in CLUSTER_FEATURES:
        groups, cutpoints = assign_quantile_groups(feats, feature)
        if not groups:
            group_scans[feature] = {"cutpoints": None, "groups": {}, "skipped": True}
            continue
        group_scans[feature] = {
            "cutpoints": cutpoints,
            "skipped": False,
            "groups": scan_groups(legs, groups, label_order=TERCILE_LABELS, min_n=group_min_n),
        }

    ev_groups, _ = assign_event_count_buckets(feats)
    group_scans["n_events"] = {
        "cutpoints": None,
        "skipped": False,
        "groups": scan_groups(legs, ev_groups, label_order=EVENT_COUNT_LABELS, min_n=group_min_n),
    }

    findings = judge_group_scans(
        group_scans, min_events_per_group=min_events_per_group
    )
    # n_events is a sampling-control variable, not a structural stock feature —
    # exclude it from both success criteria (its findings are still reported).
    strict_hits = [
        f
        for f in findings
        if f.get("usable") and f["ci_separated"] and f["groups_meet_min_n"] and f["feature"] != "n_events"
    ]
    soft_hits = [
        f
        for f in findings
        if f.get("usable") and f["spread_notable"] and f["groups_meet_min_n"] and f["feature"] != "n_events"
    ]

    return {
        "schema": "abc_v3_f1_gap_stock_clustering-v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "date_start": date_start,
        "date_end": date_end,
        "hold_days": hold_days,
        "baseline_window": baseline_window,
        "min_trailing_days": min_trailing_days,
        "group_min_n": group_min_n,
        "min_events_per_group": min_events_per_group,
        "n_legs": len(legs),
        "n_stocks": len(stock_ids),
        "overall_scan": overall,
        "decomposition_raw": decomposition,
        "decomposition_z": decomposition_z,
        "stock_features": sorted(feats.values(), key=lambda f: str(f["stock_id"])),
        "group_scans": group_scans,
        "findings": findings,
        "success": {
            "strict_hit_features": [f["feature"] for f in strict_hits],
            "soft_hit_features": [f["feature"] for f in soft_hits],
            "strict_success": bool(strict_hits),
            "soft_success": bool(soft_hits),
        },
    }


# ---------------------------------------------------------------------------
# markdown rendering
# ---------------------------------------------------------------------------

_FEATURE_LABELS = {
    "avg_gap_w20_w5": "avg_gap_w20_w5（該股 60d EOD gap 均值，事件前）",
    "std_gap_w20_w5": "std_gap_w20_w5（該股 60d EOD gap 標準差＝振幅，事件前）",
    "adv_turnover_60d": "adv_turnover_60d（60d 日均成交金額，事件前）",
    "atr_pct_20d": "atr_pct_20d（20d ATR%，事件前）",
    "n_events": "n_events（該股在樣本中的事件次數，控制變數）",
}


def _fmt_ci(ci: Any) -> str:
    if not ci:
        return "—"
    return f"[{ci[0]:+.3f}, {ci[1]:+.3f}]"


def _fmt_rho(v: Any) -> str:
    return "—" if v is None else f"{float(v):+.4f}"


def render_gap_stock_clustering_md(payload: dict[str, Any]) -> str:
    lines: list[str] = [
        "# ABC v3+F1 · gap_w20_w5 跨股票族群深挖（RP-3）",
        "",
        f"**{payload.get('n_legs')}** legs · **{payload.get('n_stocks')}** 檔股票 · "
        f"窗口 **{payload.get('date_start')}** .. **{payload.get('date_end')}** · "
        f"hold **{payload.get('hold_days')}d** · baseline window **{payload.get('baseline_window')}** 天",
        "",
        "研究問題：`gap_w20_w5_raw`（跨股票，+ρ）與 `gap_w20_w5_z`（自我標準化，−ρ）方向翻轉，"
        "是否來自可觀察的股票結構特徵？分群特徵一律只用該股票**第一筆事件之前**的歷史計算（無 lookahead）。"
        "DB 無產業對照表（`flow_event_legs.sector` 為空），依計劃書退而求其次使用量化特徵分群。",
        "",
        "## 1. 全樣本基準（Spearman ρ 附 95% CI）",
        "",
        "| Factor | n | Spearman ρ | 95% CI | Tercile lift(pp) |",
        "|--------|--:|-----------:|--------|------------------:|",
    ]
    for r in payload.get("overall_scan") or []:
        if r.get("skipped"):
            continue
        lines.append(
            f"| {r['factor']} | {r['n']} | {_fmt_rho(r.get('spearman_rho'))} | "
            f"{_fmt_ci(r.get('spearman_ci95'))} | {r.get('tercile_lift')} |"
        )

    lines.extend(
        [
            "",
            "## 2. Between-stock vs within-stock 分解",
            "",
            "把 pooled 相關拆成「股票之間」（每檔股票取事件均值，一檔一點）與「股票之內」"
            "（各股自身 demean 後合併）兩個成分；另附「每檔只取第一筆事件」的重抽樣，"
            "控制重複抽樣股票對 pooled ρ 的主導。",
            "",
            "| 成分 | factor | n | Spearman ρ | 95% CI |",
            "|------|--------|--:|-----------:|--------|",
        ]
    )
    for key, deco in (("raw", payload.get("decomposition_raw")), ("z", payload.get("decomposition_z"))):
        if not deco:
            continue
        for comp, label in (
            ("between_stock", "between-stock（股票均值）"),
            ("within_stock", "within-stock（demeaned）"),
            ("first_event_only", "first-event-only（一股一筆）"),
        ):
            d = deco.get(comp) or {}
            lines.append(
                f"| {label} | {deco.get('factor')} | {d.get('n')} | "
                f"{_fmt_rho(d.get('spearman_rho'))} | {_fmt_ci(d.get('spearman_ci95'))} |"
            )

    lines.extend(["", "## 3. 分群組內掃描（三分位，特徵皆為事件前歷史）", ""])
    group_scans = payload.get("group_scans") or {}
    for feature, spec in group_scans.items():
        label = _FEATURE_LABELS.get(feature, feature)
        lines.append(f"### {label}")
        lines.append("")
        if spec.get("skipped"):
            lines.extend(["（可用股票數不足，略過）", ""])
            continue
        cp = spec.get("cutpoints")
        if cp:
            lines.append(f"三分位切點：q1={cp[0]}，q2={cp[1]}")
            lines.append("")
        lines.extend(
            [
                "| 組 | 股票數 | 事件數 | 組均報酬% | 勝率% | Factor | ρ | 95% CI | lift(pp) |",
                "|----|-------:|-------:|----------:|------:|--------|--:|--------|---------:|",
            ]
        )
        for glabel, g in (spec.get("groups") or {}).items():
            first = True
            rows = [r for r in g.get("scan") or []]
            if not rows:
                lines.append(
                    f"| {glabel} | {g['n_stocks']} | {g['n_legs']} | {g.get('mean_return_pct')} | "
                    f"{g.get('win_rate_pct')} | — | — | — | — |"
                )
                continue
            for r in rows:
                head = (
                    f"| {glabel} | {g['n_stocks']} | {g['n_legs']} | {g.get('mean_return_pct')} | {g.get('win_rate_pct')} | "
                    if first
                    else "|  |  |  |  |  | "
                )
                if r.get("skipped"):
                    lines.append(head + f"{r['factor']} | n={r['n']}<min | — | — |")
                else:
                    lines.append(
                        head
                        + f"{r['factor']} | {_fmt_rho(r.get('spearman_rho'))} | "
                        f"{_fmt_ci(r.get('spearman_ci95'))} | {r.get('tercile_lift')} |"
                    )
                first = False
        lines.append("")

    lines.extend(["## 4. 成功標準判定", ""])
    findings = payload.get("findings") or []
    for f in findings:
        if not f.get("usable"):
            lines.append(f"- `{f['feature']}`：{f.get('reason')}")
            continue
        rho_txt = "，".join(f"{k}={v:+.3f}" for k, v in (f.get("rho_by_group") or {}).items())
        lines.append(
            f"- `{f['feature']}`（focus={f['focus_factor']}）：組內 ρ {{{rho_txt}}}，"
            f"spread={f['rho_spread']:+.3f}（最佳 `{f['best_group']}` vs 最差 `{f['worst_group']}`）"
            f"｜每組事件數 ≥{payload.get('min_events_per_group')}：{'✔' if f['groups_meet_min_n'] else '✘'}"
            f"｜CI 分離：{'✔' if f['ci_separated'] else '✘'}"
            f"｜最佳組 CI 排除 0：{'✔' if f['best_group_ci_excludes_zero'] else '✘'}"
        )
    success = payload.get("success") or {}
    lines.extend(
        [
            "",
            f"- **嚴格標準**（存在一個特徵，最佳組與最差組的 95% CI 完全分離，且各組事件數達標）："
            f"{'達成 — ' + ', '.join(success.get('strict_hit_features') or []) if success.get('strict_success') else '未達成'}",
            f"- **寬鬆標準**（ρ spread ≥ 0.15 且各組事件數達標，排除 n_events 控制變數）："
            f"{'達成 — ' + ', '.join(success.get('soft_hit_features') or []) if success.get('soft_success') else '未達成'}",
            "",
            "## 5. 對 RP-5 的建議",
            "",
        ]
    )

    deco = payload.get("decomposition_raw") or {}
    between = (deco.get("between_stock") or {}).get("spearman_rho")
    within = (deco.get("within_stock") or {}).get("spearman_rho")
    if success.get("strict_success"):
        lines.append(
            "- 找到 CI 分離的分群特徵，建議 RP-5 改用**分群條件版** raw gap（僅在效應集中的族群啟用），"
            "而非全域保留 `gap_w20_w5_raw`。"
        )
    elif success.get("soft_success"):
        lines.append(
            "- 僅達寬鬆標準（點估計 spread 明顯但 CI 重疊）：`gap_w20_w5_raw` 可**暫時保留**為 RP-5 候選 shape，"
            "但優先序應下調，且建議以分群條件版與全域版並列進入 RP-5 比較，不可單獨依賴。"
        )
    else:
        lines.append(
            "- 未找到任何可將 raw-gap 效應顯著隔離的股票結構特徵：效應更可能是市場整體 regime/抽樣噪音，"
            "建議 RP-5 **不保留** `gap_w20_w5_raw` 為獨立候選 shape（或僅以最低優先序陪跑），"
            "以自我標準化的 `gap_w20_w5_z`（方向穩定、|ρ| 較大）為主。"
        )
    if between is not None and within is not None:
        lines.append(
            f"- 分解結果佐證：between-stock ρ={between:+.3f} vs within-stock ρ={within:+.3f}——"
            "raw 的正相關主要由「哪一種股票」貢獻、股票之內反而不成立（或反向），"
            "與 z-score 版本翻轉為負一致。"
        )
    lines.extend(
        [
            "",
            "## 6. 注意事項",
            "",
            f"- 分群後每組事件數約 {payload.get('n_legs')}/3，Spearman CI 半寬約 ±0.2，"
            "小群組的組內 ρ 點估計不可過度解讀，一律以 CI 為準。",
            "- 分群特徵僅使用各股第一筆事件之前的資料（gap 序列、成交金額、ATR% 皆同），無未來資訊。",
            "- 三分位切點取自 113 檔股票的橫斷面分布（研究用途；若上線需改用滾動歷史分布定切點）。",
            "",
            "模組：`src/research/backtest/abc_v3_f1_gap_stock_clustering_study.py` · "
            "runner：`scripts/research/run_abc_v3_f1_gap_stock_clustering.py`",
        ]
    )
    return "\n".join(lines)
