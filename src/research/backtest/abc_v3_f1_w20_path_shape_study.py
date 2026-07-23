"""ABC v3+F1 · W20 T-2→T-1→T0 path shape observation (MV / RV separate).

Hypothesis: a genuine pullback leaves a two-step WMA20 trail —
  d1 = X(T-1 EOD) − X(T-2 EOD) < 0   (loosened into the prior close)
  d0 = X(T0 entry) − X(T-1 EOD) ≥ 0  (stabilized / lifted at entry)
— and that shape predicts higher forward return than other day-over-day paths.

X ∈ {W20 MV, W20 RV}; studies are reported separately.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import datetime
from typing import Any, Literal

from market_benchmark import previous_trading_date
from research.backtest.abc_v3_f1_entry_multifactor_tp_sweep import CHAMPION_COMBO
from research.backtest.archive.abc_v3_f1_kinematic_tp_sl_study import evaluate_leg_tp_sl
from research.backtest.archive.c18acc_triple_wma_extreme_profile import _frame_idx_for_minute
from research.backtest.dual_wma_signal_backtest import _build_stock_frame_maps
from research.backtest.triple_wma_pullback_sweep import _mv, _rv
from rrg_universe_intraday_panel import (
    WMA_MICRO_LENGTH,
    WMA_ULTRA_LENGTH,
    build_dual_wma_intraday_trajectories,
)

SCHEMA = "abc_v3_f1_w20_path_shape-v1"
Metric = Literal["mv", "rv"]

SHAPE_ORDER: tuple[str, ...] = (
    "pullback_then_recover",
    "continued_pullback",
    "no_pullback",
    "sharp_bounce",
)

SHAPE_LABELS: dict[str, str] = {
    "pullback_then_recover": "真回踩後回升（d1<0 · d0≥0）",
    "continued_pullback": "持續回踩（d1<0 · d0<0）",
    "no_pullback": "無回踩（d1≥0）",
    "sharp_bounce": "急殺後反彈（d1≤−0.3 · d0≥0.3）",
}


def classify_w20_path_shape(
    d1: float,
    d0: float,
    *,
    sharp_d1_max: float = -0.3,
    sharp_d0_min: float = 0.3,
) -> str:
    """Primary shape for (d1, d0). ``sharp_bounce`` is a subset tag preferred when hit."""
    if d1 <= sharp_d1_max and d0 >= sharp_d0_min:
        return "sharp_bounce"
    if d1 < 0 and d0 >= 0:
        return "pullback_then_recover"
    if d1 < 0 and d0 < 0:
        return "continued_pullback"
    return "no_pullback"


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


def _summarize_rets(rets: list[float], *, baseline_mean: float | None) -> dict[str, Any]:
    mean = _mean(rets)
    out: dict[str, Any] = {
        "n": len(rets),
        "mean_pct": mean,
        "median_pct": _median(rets),
        "win_rate_pct": _win_rate(rets),
        "delta_vs_baseline_pp": (
            round(mean - baseline_mean, 3) if mean is not None and baseline_mean is not None else None
        ),
    }
    return out


def _prior_trading_dates(conn: sqlite3.Connection, entry_date: str) -> tuple[str | None, str | None]:
    """Return (T-1, T-2) trading dates strictly before entry_date."""
    t1 = previous_trading_date(conn, entry_date)
    if not t1:
        return None, None
    t2 = previous_trading_date(conn, t1)
    return t1, t2


def fetch_w20_eod_cache(
    conn: sqlite3.Connection,
    *,
    date_to_stocks: dict[str, set[str]],
    chunk_days: int = 25,
) -> dict[tuple[str, str], dict[str, float]]:
    """``(stock_id, date) → {w20_mv, w20_rv}`` at 13:30 EOD.

    Builds trajectories in date chunks (not one date at a time) to keep I/O
    amortized; stocks are the union needed inside each chunk.
    """
    cache: dict[tuple[str, str], dict[str, float]] = {}
    dates = sorted(d for d, sids in date_to_stocks.items() if sids)
    if not dates:
        return cache
    n_chunks = (len(dates) + chunk_days - 1) // chunk_days
    for ci in range(n_chunks):
        chunk = dates[ci * chunk_days : (ci + 1) * chunk_days]
        sids = sorted({sid for d in chunk for sid in date_to_stocks.get(d, ())})
        print(
            f"  eod chunk {ci + 1}/{n_chunks}: dates={len(chunk)} stocks={len(sids)}",
            flush=True,
        )
        frames, trajectories, _ = build_dual_wma_intraday_trajectories(
            conn,
            dates=chunk,
            stock_ids=sids,
            micro_lengths=(WMA_MICRO_LENGTH, WMA_ULTRA_LENGTH),
        )
        stock_maps = _build_stock_frame_maps(trajectories, frames)
        for d in chunk:
            idx = _frame_idx_for_minute(frames, d, "13:30")
            if idx is None:
                continue
            for sid in date_to_stocks.get(d, ()):
                points = stock_maps.get(sid)
                if not points:
                    continue
                p = points[idx]
                if not p:
                    continue
                mv, rv = _mv(p, "w20"), _rv(p, "w20")
                if mv is None or rv is None:
                    continue
                cache[(sid, d)] = {"w20_mv": float(mv), "w20_rv": float(rv)}
    return cache


def annotate_legs_with_w20_path(
    conn: sqlite3.Connection,
    legs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Attach T-2/T-1 EOD W20 snaps + path deltas/shapes; keep champion TP-only ret."""
    date_to_stocks: dict[str, set[str]] = defaultdict(set)
    meta: list[tuple[dict[str, Any], str, str]] = []
    for leg in legs:
        sid = str(leg.get("stock_id") or "")
        entry = str(leg.get("entry_date") or "")
        t0 = leg.get("entry_t0") or {}
        if not sid or not entry:
            continue
        if t0.get("w20_mv") is None or t0.get("w20_rv") is None:
            continue
        t1, t2 = _prior_trading_dates(conn, entry)
        if not t1 or not t2:
            continue
        date_to_stocks[t1].add(sid)
        date_to_stocks[t2].add(sid)
        meta.append((leg, t1, t2))

    eod = fetch_w20_eod_cache(conn, date_to_stocks=date_to_stocks)
    out: list[dict[str, Any]] = []
    for leg, t1, t2 in meta:
        sid = str(leg["stock_id"])
        snap1 = eod.get((sid, t1))
        snap2 = eod.get((sid, t2))
        if not snap1 or not snap2:
            continue
        t0 = leg["entry_t0"]
        tp = evaluate_leg_tp_sl(leg, CHAMPION_COMBO)
        row = dict(leg)
        row["t_minus_1"] = t1
        row["t_minus_2"] = t2
        row["t2_w20_mv"] = snap2["w20_mv"]
        row["t2_w20_rv"] = snap2["w20_rv"]
        row["t1_w20_mv"] = snap1["w20_mv"]
        row["t1_w20_rv"] = snap1["w20_rv"]
        row["t0_w20_mv"] = float(t0["w20_mv"])
        row["t0_w20_rv"] = float(t0["w20_rv"])
        row["d1_mv"] = round(row["t1_w20_mv"] - row["t2_w20_mv"], 4)
        row["d0_mv"] = round(row["t0_w20_mv"] - row["t1_w20_mv"], 4)
        row["d1_rv"] = round(row["t1_w20_rv"] - row["t2_w20_rv"], 4)
        row["d0_rv"] = round(row["t0_w20_rv"] - row["t1_w20_rv"], 4)
        row["shape_mv"] = classify_w20_path_shape(row["d1_mv"], row["d0_mv"])
        row["shape_rv"] = classify_w20_path_shape(row["d1_rv"], row["d0_rv"])
        row["tp_only_ret_pct"] = float(tp["exit_ret_pct"])
        row["hold_ret_pct"] = float(leg.get("return_pct") or 0)
        out.append(row)
    return out


def _metric_block(rows: list[dict[str, Any]], metric: Metric) -> dict[str, Any]:
    d1_key = f"d1_{metric}"
    d0_key = f"d0_{metric}"
    shape_key = f"shape_{metric}"
    rets = [float(r["tp_only_ret_pct"]) for r in rows]
    baseline = _mean(rets)

    shape_rows: list[dict[str, Any]] = []
    for sid in SHAPE_ORDER:
        sub = [float(r["tp_only_ret_pct"]) for r in rows if r.get(shape_key) == sid]
        shape_rows.append(
            {
                "shape": sid,
                "label": SHAPE_LABELS[sid],
                **_summarize_rets(sub, baseline_mean=baseline),
            }
        )

    # Sign buckets for each leg of the path (not mutually exclusive with shapes).
    sign_buckets = [
        ("d1_neg_d0_nonneg", "d1<0 · d0≥0（=真回踩主桶）", lambda r: r[d1_key] < 0 and r[d0_key] >= 0),
        ("d1_neg_d0_neg", "d1<0 · d0<0", lambda r: r[d1_key] < 0 and r[d0_key] < 0),
        ("d1_nonneg", "d1≥0", lambda r: r[d1_key] >= 0),
        ("d0_nonneg", "d0≥0（進場日不降）", lambda r: r[d0_key] >= 0),
        ("d0_neg", "d0<0（進場日續降）", lambda r: r[d0_key] < 0),
    ]
    bucket_rows: list[dict[str, Any]] = []
    for bid, label, pred in sign_buckets:
        sub = [float(r["tp_only_ret_pct"]) for r in rows if pred(r)]
        bucket_rows.append({"id": bid, "label": label, **_summarize_rets(sub, baseline_mean=baseline)})

    xs_d1 = [float(r[d1_key]) for r in rows]
    xs_d0 = [float(r[d0_key]) for r in rows]
    return {
        "metric": metric,
        "n": len(rows),
        "baseline_mean_tp_pct": baseline,
        "corr_d1_vs_ret": _pearson(xs_d1, rets),
        "corr_d0_vs_ret": _pearson(xs_d0, rets),
        "shapes": shape_rows,
        "sign_buckets": bucket_rows,
    }


def run_w20_path_shape_study(
    conn: sqlite3.Connection,
    legs: list[dict[str, Any]],
    *,
    legs_source: str = "",
) -> dict[str, Any]:
    annotated = annotate_legs_with_w20_path(conn, legs)
    dates = sorted({str(r["entry_date"]) for r in annotated})
    return {
        "schema": SCHEMA,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "legs_source": legs_source,
        "n_input_legs": len(legs),
        "n_annotated": len(annotated),
        "n_stocks": len({str(r["stock_id"]) for r in annotated}),
        "date_start": dates[0] if dates else None,
        "date_end": dates[-1] if dates else None,
        "exit": "champion TP-only · NEVER_SL · hold_5d cap",
        "definitions": {
            "d1": "X(T-1 13:30 EOD) − X(T-2 13:30 EOD)",
            "d0": "X(T0 entry poll) − X(T-1 13:30 EOD)",
            "X": "W20 MV or W20 RV",
            "shapes": SHAPE_LABELS,
        },
        "mv_study": _metric_block(annotated, "mv"),
        "rv_study": _metric_block(annotated, "rv"),
    }


def render_w20_path_shape_md(payload: dict[str, Any]) -> str:
    lines = [
        "# ABC v3+F1 · W20 T−2→T−1→T0 路徑形狀觀察（MV / RV）",
        "",
        f"> legs **{payload.get('n_annotated')}** / input **{payload.get('n_input_legs')}** · "
        f"股票 **{payload.get('n_stocks')}** · "
        f"窗口 **{payload.get('date_start')}** .. **{payload.get('date_end')}**",
        f"> 出場：{payload.get('exit')} · 來源：`{payload.get('legs_source')}`",
        "",
        "## 定義",
        "",
        "- `d1` = X(T−1 13:30) − X(T−2 13:30)",
        "- `d0` = X(T0 進場 poll) − X(T−1 13:30)",
        "- **真回踩後回升**：`d1<0` 且 `d0≥0`",
        "- **持續回踩**：`d1<0` 且 `d0<0`",
        "- **無回踩**：`d1≥0`",
        "- **急殺後反彈**：`d1≤−0.3` 且 `d0≥0.3`（優先於真回踩標籤）",
        "",
    ]

    for key, title in (("mv_study", "MV 差額研究（W20 rs_momentum）"), ("rv_study", "RV 差額研究（W20 rs_ratio）")):
        block = payload.get(key) or {}
        lines.extend(
            [
                f"## {title}",
                "",
                f"- n={block.get('n')} · baseline TP-only 均 **{block.get('baseline_mean_tp_pct')}%**",
                f"- Pearson(d1, ret)={block.get('corr_d1_vs_ret')} · Pearson(d0, ret)={block.get('corr_d0_vs_ret')}",
                "",
                "### 形狀桶",
                "",
                "| 形狀 | n | 均% | 中位% | 勝率% | Δ vs baseline (pp) |",
                "|------|--:|----:|------:|------:|-------------------:|",
            ]
        )
        for row in block.get("shapes") or []:
            lines.append(
                f"| {row.get('label')} | {row.get('n')} | {row.get('mean_pct')} | "
                f"{row.get('median_pct')} | {row.get('win_rate_pct')} | {row.get('delta_vs_baseline_pp')} |"
            )
        lines.extend(
            [
                "",
                "### 符號桶",
                "",
                "| 條件 | n | 均% | Δ vs baseline (pp) |",
                "|------|--:|----:|-------------------:|",
            ]
        )
        for row in block.get("sign_buckets") or []:
            lines.append(
                f"| {row.get('label')} | {row.get('n')} | {row.get('mean_pct')} | {row.get('delta_vs_baseline_pp')} |"
            )
        lines.append("")

    lines.extend(
        [
            "## 解讀門檻（觀察級）",
            "",
            "- 真回踩桶相對 baseline lift ≥ **+2pp** 且 n≥30 → 值得升級成正式假說",
            "- |Pearson| < 0.05 且各桶 lift 雜亂 → 維持觀察、不進 frozen overlay",
            "",
            "模組：`src/research/backtest/abc_v3_f1_w20_path_shape_study.py`",
            "",
        ]
    )
    return "\n".join(lines)


SPREAD_DIST_SCHEMA = "abc_v3_f1_spread_dist_closeout-v1"


def _tercile_edges(vals: list[float]) -> tuple[float, float] | None:
    if len(vals) < 9:
        return None
    s = sorted(vals)
    n = len(s)
    return s[n // 3], s[(2 * n) // 3]


def run_spread_dist_depth_closeout(
    legs: list[dict[str, Any]],
    *,
    legs_source: str = "",
    min_bucket_n: int = 30,
    lift_pp_gate: float = 2.0,
) -> dict[str, Any]:
    """Final entry observation: T0 ``spread_dist`` depth vs champion TP-only.

    No DB / trajectory rebuild — uses ``entry_t0.spread_dist`` already on legs.
    """
    rows: list[dict[str, Any]] = []
    for leg in legs:
        t0 = leg.get("entry_t0") or {}
        dist = t0.get("spread_dist")
        if dist is None:
            continue
        tp = evaluate_leg_tp_sl(leg, CHAMPION_COMBO)
        rows.append(
            {
                "spread_dist": float(dist),
                "spread_class": t0.get("spread_class"),
                "tp_only_ret_pct": float(tp["exit_ret_pct"]),
                "entry_date": leg.get("entry_date"),
                "stock_id": leg.get("stock_id"),
            }
        )

    rets = [r["tp_only_ret_pct"] for r in rows]
    baseline = _mean(rets)
    dists = [r["spread_dist"] for r in rows]
    edges = _tercile_edges(dists)
    terciles: list[dict[str, Any]] = []
    if edges is not None:
        lo, hi = edges
        bands = (
            ("shallow", f"spread_dist ≤ {lo:g}（淺）", lambda d, lo=lo, hi=hi: d <= lo),
            ("mid", f"{lo:g} < spread_dist ≤ {hi:g}（中）", lambda d, lo=lo, hi=hi: lo < d <= hi),
            ("deep", f"spread_dist > {hi:g}（深）", lambda d, lo=lo, hi=hi: d > hi),
        )
        for bid, label, pred in bands:
            sub = [r["tp_only_ret_pct"] for r in rows if pred(r["spread_dist"])]
            terciles.append(
                {"id": bid, "label": label, **_summarize_rets(sub, baseline_mean=baseline)}
            )

    # Fixed absolute bands (lead_pullback spread_dist scale).
    fixed_bands = (
        ("lt_1_5", "spread_dist < 1.5", lambda d: d < 1.5),
        ("1_5_to_3", "1.5 ≤ spread_dist < 3", lambda d: 1.5 <= d < 3.0),
        ("ge_3", "spread_dist ≥ 3", lambda d: d >= 3.0),
    )
    absolute: list[dict[str, Any]] = []
    for bid, label, pred in fixed_bands:
        sub = [r["tp_only_ret_pct"] for r in rows if pred(r["spread_dist"])]
        absolute.append({"id": bid, "label": label, **_summarize_rets(sub, baseline_mean=baseline)})

    deep = next((t for t in terciles if t.get("id") == "deep"), None)
    shallow = next((t for t in terciles if t.get("id") == "shallow"), None)
    deep_lift = None
    if deep and deep.get("mean_pct") is not None and baseline is not None:
        deep_lift = round(float(deep["mean_pct"]) - float(baseline), 3)
    passes_overlay_gate = bool(
        deep
        and int(deep.get("n") or 0) >= min_bucket_n
        and deep_lift is not None
        and deep_lift >= lift_pp_gate
    )

    dates = sorted({str(r["entry_date"]) for r in rows if r.get("entry_date")})
    return {
        "schema": SPREAD_DIST_SCHEMA,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "legs_source": legs_source,
        "n_input_legs": len(legs),
        "n_annotated": len(rows),
        "n_stocks": len({str(r["stock_id"]) for r in rows}),
        "date_start": dates[0] if dates else None,
        "date_end": dates[-1] if dates else None,
        "exit": "champion TP-only · NEVER_SL · hold_5d cap",
        "baseline_mean_tp_pct": baseline,
        "corr_spread_dist_vs_ret": _pearson(dists, rets),
        "tercile_edges": {"lo": edges[0], "hi": edges[1]} if edges else None,
        "terciles": terciles,
        "absolute_bands": absolute,
        "closeout": {
            "hypothesis_id": "H-ENTRY-W20-PATH-1",
            "min_bucket_n": min_bucket_n,
            "lift_pp_gate": lift_pp_gate,
            "deep_lift_pp": deep_lift,
            "deep_n": (deep or {}).get("n"),
            "shallow_mean_pct": (shallow or {}).get("mean_pct"),
            "deep_mean_pct": (deep or {}).get("mean_pct"),
            "spread_dist_passes_overlay_gate": passes_overlay_gate,
            "verdict": "rejected",
            "verdict_reason": (
                "W20 path shape failed n≥30 / median gate; spread_dist depth is a last "
                "T0 structural-pullback quality check. Entry-side further filters closed; "
                "keep raw ABC v3+F1."
            ),
        },
    }


def render_spread_dist_closeout_md(payload: dict[str, Any]) -> str:
    co = payload.get("closeout") or {}
    lines = [
        "# ABC v3+F1 · 進場觀察結案 · spread_dist 深淺 + H-ENTRY-W20-PATH-1",
        "",
        f"> **Verdict: {co.get('verdict')}** · legs **{payload.get('n_annotated')}** / "
        f"input **{payload.get('n_input_legs')}** · 股票 **{payload.get('n_stocks')}** · "
        f"窗口 **{payload.get('date_start')}** .. **{payload.get('date_end')}**",
        f"> 出場：{payload.get('exit')} · 來源：`{payload.get('legs_source')}`",
        "",
        "## 研究問題（最後一次進場觀察）",
        "",
        "在 ABC 已是結構回踩（E=`lead_pullback_buy`）的前提下，T0 的 "
        "`spread_dist`（回踩深度）是否還能把 champion TP-only 均報酬抬高 ≥+2pp（n≥30）？",
        "",
        "並行結案：`H-ENTRY-W20-PATH-1`（W20 T−2→T−1→T0 路徑形狀）— 不論本輪結果均關閉。",
        "",
        "## 對照：W20 路徑形狀（已跑）",
        "",
        "- 報告：[`20260712_abc_v3_f1_w20_path_shape.md`](./20260712_abc_v3_f1_w20_path_shape.md)",
        "- MV 真回踩後回升：均 9.58% 但 **n=9 · 中位 −1.99%**（離群拉高）",
        "- RV 同形狀：n=6 · 均 0.96%（Δ−1.2pp，反向）",
        "- 結論：不過 n≥30 / 中位門檻 → **不升級 overlay**",
        "",
        "## spread_dist 深淺",
        "",
        f"- baseline TP-only 均 **{payload.get('baseline_mean_tp_pct')}%**",
        f"- Pearson(spread_dist, ret)={payload.get('corr_spread_dist_vs_ret')}",
        f"- tercile 切點：{payload.get('tercile_edges')}",
        "",
        "### 三分位",
        "",
        "| 桶 | n | 均% | 中位% | 勝率% | Δ vs baseline (pp) |",
        "|----|--:|----:|------:|------:|-------------------:|",
    ]
    for row in payload.get("terciles") or []:
        lines.append(
            f"| {row.get('label')} | {row.get('n')} | {row.get('mean_pct')} | "
            f"{row.get('median_pct')} | {row.get('win_rate_pct')} | {row.get('delta_vs_baseline_pp')} |"
        )
    lines.extend(
        [
            "",
            "### 固定深度帶",
            "",
            "| 桶 | n | 均% | 中位% | 勝率% | Δ vs baseline (pp) |",
            "|----|--:|----:|------:|------:|-------------------:|",
        ]
    )
    for row in payload.get("absolute_bands") or []:
        lines.append(
            f"| {row.get('label')} | {row.get('n')} | {row.get('mean_pct')} | "
            f"{row.get('median_pct')} | {row.get('win_rate_pct')} | {row.get('delta_vs_baseline_pp')} |"
        )
    lines.extend(
        [
            "",
            "## 結案",
            "",
            f"- 假說 **`{co.get('hypothesis_id')}`** → **`{co.get('verdict')}`**",
            f"- spread_dist 深桶是否過 overlay 門檻（n≥{co.get('min_bucket_n')} · "
            f"lift≥{co.get('lift_pp_gate')}pp）："
            f"**{co.get('spread_dist_passes_overlay_gate')}** "
            f"（deep n={co.get('deep_n')} · deep lift={co.get('deep_lift_pp')}pp）",
            f"- 理由：{co.get('verdict_reason')}",
            "",
            "**行動**：維持 raw ABC v3+F1 進場；不再開進場側「回踩品質」過濾假說。"
            "研究預算轉出場落地（champion TP）或正交新題。",
            "",
            "模組：`src/research/backtest/abc_v3_f1_w20_path_shape_study.py`",
            "",
        ]
    )
    return "\n".join(lines)
