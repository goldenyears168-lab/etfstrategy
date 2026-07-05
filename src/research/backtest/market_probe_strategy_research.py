"""Market probe × strategy research · 探針穩定性 · 日型分類 · 策略相似/互補。

T-1 探針 → T 日策略日超額 · 全 track_adapters + 市集缺漏軌。
"""

from __future__ import annotations

import json
import math
import sqlite3
import statistics
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any

from backtest_standard_config import BacktestStandardConfig, load_backtest_standard_config
from flow_returns import trading_dates_after
from market_benchmark import load_benchmark_close
from research.backtest.finmind_marketplace_comparison import MARKETPLACE_STRATEGIES
from research.backtest.finmind_marketplace_complementarity import (
    StrategyDailyPanel,
    load_strategy_daily_panel,
    pairwise_metrics,
)
from research.backtest.market_probe_radar import (
    AxisScore,
    ProbeRadarConfig,
    apply_meta_router,
    axis_z_to_quartile,
    compute_ix0001_vol_state,
    resolve_universe,
    score_axis_on_date,
    _zscore,
)
from research.backtest.unified_nav_adapter import build_strategy_nav
from stock_db import DEFAULT_DB_PATH, PROJECT_ROOT, connect

DEFAULT_OUT_DIR = PROJECT_ROOT / "reports" / "research" / "market-probe"
DEFAULT_DATE_START = "2025-01-01"
DEFAULT_WF_TRAIN_END = "2025-06-30"
STABILITY_PCT_TARGET = 80.0

STRATEGY_LABELS: dict[str, str] = {
    "00981a-l1h9": "Copytrade 00981A",
    "rrg-mono-hold7": "RRG hold7",
    "rrg-mono-swap-accel": "RRG swap-accel",
    "vcp-pivot-gate": "VCP pivot",
    "vcp-coil-close": "VCP coil",
    "finmind-low-ps-growth": "FM 低P/S",
    "finmind-tx-foreign-net-long-3d": "FM TX外資",
    "finmind-ma20-breakout-volume": "FM MA20突破",
    "finmind-low-rebound-foreign-it-sync": "FM 低檔回升",
    "finmind-it-cold-stove": "FM 投信冷灶",
}

EXTRA_FINMIND_IDS = (
    "finmind-low-rebound-foreign-it-sync",
    "finmind-it-cold-stove",
)


@dataclass
class ProbeDayRecord:
    probe_date: str
    signal_date: str | None
    axis_z: dict[str, float | None]
    axis_raw: dict[str, float | None]
    axis_n: dict[str, int]
    axis_sufficient: dict[str, bool]
    vol_decile: int | None
    vol_regime: str
    market_type_id: str
    market_type_label: str
    dominant_axis: str | None
    axis_quartile: dict[str, int | None]


@dataclass
class UnifiedStrategyPanel:
    strategy_id: str
    label: str
    dates: list[str]
    daily_return_pct: list[float]
    daily_excess_pct: list[float]
    in_market_flag: list[int]
    signal_flag: list[int]


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) != len(ys) or len(xs) < 5:
        return None
    mx = statistics.mean(xs)
    my = statistics.mean(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den_x = math.sqrt(sum((x - mx) ** 2 for x in xs))
    den_y = math.sqrt(sum((y - my) ** 2 for y in ys))
    if den_x == 0 or den_y == 0:
        return None
    return round(num / (den_x * den_y), 4)


def _lag1_autocorr(values: list[float]) -> float | None:
    if len(values) < 6:
        return None
    xs = values[:-1]
    ys = values[1:]
    return _pearson(xs, ys)


def _mean(xs: list[float]) -> float | None:
    return round(statistics.mean(xs), 4) if xs else None


def _dates_with_kbar(
    conn: sqlite3.Connection,
    date_start: str,
    date_end: str,
) -> list[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT trade_date AS d
        FROM stock_kbar_1m
        WHERE trade_date BETWEEN ? AND ?
          AND source IN ('finmind', 'yahoo')
        GROUP BY trade_date
        HAVING COUNT(DISTINCT stock_id) >= 20
        ORDER BY d
        """,
        (date_start, date_end),
    ).fetchall()
    return [str(r["d"]) for r in rows]


def build_probe_daily_panel(
    conn: sqlite3.Connection,
    *,
    probe_cfg: ProbeRadarConfig,
    meta_router: dict[str, Any],
    probe_dates: list[str],
    universe: list[str],
) -> list[ProbeDayRecord]:
    axes = probe_cfg.axes
    min_n = probe_cfg.min_n_signals
    min_z_n = probe_cfg.min_n_for_zscore
    lookback = probe_cfg.zscore_lookback_days
    raw_history: list[list[dict[str, Any]]] = []
    z_history: dict[str, list[float]] = {ax.axis_id: [] for ax in axes}
    records: list[ProbeDayRecord] = []

    for i, d in enumerate(probe_dates):
        vol = compute_ix0001_vol_state(
            conn,
            d,
            rank_lookback_days=int(probe_cfg.vol_state.get("rank_lookback_days") or 252),
            n_deciles=int(probe_cfg.vol_state.get("n_deciles") or 10),
            high_decile_min=int(probe_cfg.vol_state.get("high_decile_min") or 7),
            low_decile_max=int(probe_cfg.vol_state.get("low_decile_max") or 3),
            benchmark=probe_cfg.benchmark,
        )
        day_rows = [
            score_axis_on_date(
                conn,
                ax,
                d,
                universe,
                min_bars_per_day=probe_cfg.min_bars_per_day,
                use_stop=probe_cfg.probe_use_stop,
                vol_state=vol,
            )
            for ax in axes
        ]
        raw_history.append(day_rows)

        axis_z: dict[str, float | None] = {}
        axis_raw: dict[str, float | None] = {}
        axis_n: dict[str, int] = {}
        axis_sufficient: dict[str, bool] = {}
        axis_scores: list[AxisScore] = []

        vol_decile = vol.get("vol_decile")
        for j, ax_spec in enumerate(axes):
            row = day_rows[j]
            n_sig = int(row.get("n_signals") or 0)
            sufficient = ax_spec.source == "vol_state" or n_sig >= min_n
            hist_vals: list[float] = []
            for k in range(max(0, i - lookback), i):
                prev = raw_history[k][j]
                if prev.get("raw_score") is None:
                    continue
                if ax_spec.source == "vol_state" or int(prev.get("n_signals") or 0) >= min_z_n:
                    hist_vals.append(float(prev["raw_score"]))
            z = None
            if sufficient and (ax_spec.source == "vol_state" or n_sig >= min_z_n):
                raw_v = row.get("raw_score")
                z = _zscore(float(raw_v) if raw_v is not None else None, hist_vals)
            if ax_spec.axis_id == "volatility" and row.get("vol_decile") is not None:
                vol_decile = int(row["vol_decile"])

            axis_z[ax_spec.axis_id] = z
            axis_raw[ax_spec.axis_id] = row.get("raw_score")
            axis_n[ax_spec.axis_id] = n_sig
            axis_sufficient[ax_spec.axis_id] = sufficient
            axis_scores.append(
                AxisScore(
                    axis_id=ax_spec.axis_id,
                    label=ax_spec.label,
                    raw_score=row.get("raw_score"),
                    n_signals=n_sig,
                    hit_rate=row.get("hit_rate"),
                    z_score=z,
                    probe_modes=ax_spec.probe_modes,
                    sample_sufficient=sufficient,
                    vol_decile=row.get("vol_decile"),
                )
            )

        vol_regime = str(vol.get("vol_regime") or "unknown")
        if vol_decile is not None and vol_regime == "unknown":
            high_min = int(probe_cfg.vol_state.get("high_decile_min") or 7)
            low_max = int(probe_cfg.vol_state.get("low_decile_max") or 3)
            if vol_decile >= high_min:
                vol_regime = "high_vol"
            elif vol_decile <= low_max:
                vol_regime = "low_vol"
            else:
                vol_regime = "normal"

        axis_quartile: dict[str, int | None] = {}
        for ax_spec in axes:
            if ax_spec.source == "vol_state":
                continue
            z = axis_z.get(ax_spec.axis_id)
            axis_quartile[ax_spec.axis_id] = axis_z_to_quartile(
                z_history.get(ax_spec.axis_id, []), z
            )
            if z is not None:
                z_history[ax_spec.axis_id].append(z)
                if len(z_history[ax_spec.axis_id]) > lookback * 2:
                    z_history[ax_spec.axis_id] = z_history[ax_spec.axis_id][-lookback * 2 :]

        routing = apply_meta_router(
            meta_router=meta_router,
            axis_scores=axis_scores,
            vol_decile=vol_decile,
            axis_quartiles={k: v for k, v in axis_quartile.items() if v is not None},
        )
        next_days = trading_dates_after(conn, d, count=1, inclusive_anchor=False)
        signal_for = next_days[0] if next_days else None

        routable = [
            (ax.axis_id, axis_z[ax.axis_id])
            for ax in axes
            if ax.axis_id != "volatility" and axis_sufficient.get(ax.axis_id) and axis_z.get(ax.axis_id) is not None
        ]
        dominant = max(routable, key=lambda x: x[1])[0] if routable else None

        records.append(
            ProbeDayRecord(
                probe_date=d,
                signal_date=signal_for,
                axis_z=axis_z,
                axis_raw=axis_raw,
                axis_n=axis_n,
                axis_sufficient=axis_sufficient,
                vol_decile=vol_decile,
                vol_regime=vol_regime,
                market_type_id=routing["market_type_id"],
                market_type_label=routing["market_type_label"],
                dominant_axis=dominant,
                axis_quartile=axis_quartile,
            )
        )
    return records


def _nav_to_panel(strategy_id: str, nav_rows: list[dict[str, Any]]) -> UnifiedStrategyPanel:
    dates: list[str] = []
    daily_return: list[float] = []
    daily_excess: list[float] = []
    in_market: list[int] = []
    prev_eq: float | None = None
    prev_bench: float | None = None
    for row in nav_rows:
        d = str(row["date"])
        eq = float(row["equity_ntd"])
        bench = row.get("benchmark_equity_ntd")
        bench_f = float(bench) if bench is not None else None
        ret = 0.0
        exc = 0.0
        if prev_eq is not None and prev_eq > 0:
            ret = (eq / prev_eq - 1.0) * 100.0
        if prev_bench is not None and bench_f is not None and prev_bench > 0:
            bench_ret = (bench_f / prev_bench - 1.0) * 100.0
            exc = ret - bench_ret
        dates.append(d)
        daily_return.append(round(ret, 4))
        daily_excess.append(round(exc, 4))
        in_market.append(int(row.get("in_market_flag") or 0))
        prev_eq = eq
        prev_bench = bench_f if bench_f is not None else prev_bench

    return UnifiedStrategyPanel(
        strategy_id=strategy_id,
        label=STRATEGY_LABELS.get(strategy_id, strategy_id),
        dates=dates,
        daily_return_pct=daily_return,
        daily_excess_pct=daily_excess,
        in_market_flag=in_market,
        signal_flag=in_market[:],
    )


def load_all_strategy_panels(
    conn: sqlite3.Connection,
    *,
    cfg: BacktestStandardConfig,
    date_start: str,
    date_end: str,
) -> list[UnifiedStrategyPanel]:
    cost = cfg.cost_model if cfg.cost_model.get("enabled") else None
    panels: list[UnifiedStrategyPanel] = []

    for sid, adapter in cfg.track_adapters.items():
        start = date_start
        if adapter.earliest_data and start < adapter.earliest_data:
            start = adapter.earliest_data
        if start > date_end:
            continue
        result = build_strategy_nav(
            conn,
            sid,
            date_start=start,
            date_end=date_end,
            adapter=adapter,
            comparison_notional_ntd=cfg.comparison_notional_ntd,
            cost_model=cost,
        )
        merged = result.get("daily_nav_merged") or []
        if merged:
            panels.append(_nav_to_panel(sid, merged))

    adapter_ids = set(cfg.track_adapters)
    fm_by_id = {s.topic_id: s for s in MARKETPLACE_STRATEGIES}
    for fid in EXTRA_FINMIND_IDS:
        if fid in adapter_ids:
            continue
        spec = fm_by_id.get(fid)
        if spec is None:
            continue
        fmp = load_strategy_daily_panel(conn, spec, date_start=date_start, date_end=date_end)
        bench = load_benchmark_close(conn).reindex(fmp.dates).astype(float)
        prev_b = None
        excess: list[float] = []
        for i, d in enumerate(fmp.dates):
            ret = fmp.daily_return_pct[i]
            b = float(bench.loc[d]) if d in bench.index else None
            exc = 0.0
            if prev_b is not None and b is not None and prev_b > 0:
                exc = ret - (b / prev_b - 1.0) * 100.0
            excess.append(round(exc, 4))
            prev_b = b if b is not None else prev_b
        panels.append(
            UnifiedStrategyPanel(
                strategy_id=fid,
                label=STRATEGY_LABELS.get(fid, fmp.label),
                dates=fmp.dates,
                daily_return_pct=fmp.daily_return_pct,
                daily_excess_pct=excess,
                in_market_flag=fmp.in_market_flag,
                signal_flag=fmp.signal_flag,
            )
        )
    return panels


def _panel_by_date(panel: UnifiedStrategyPanel) -> dict[str, int]:
    return {d: i for i, d in enumerate(panel.dates)}


def probe_strategy_correlations(
    probes: list[ProbeDayRecord],
    strategies: list[UnifiedStrategyPanel],
    *,
    axis_ids: list[str],
) -> list[dict[str, Any]]:
    strat_idx = {p.strategy_id: _panel_by_date(p) for p in strategies}
    rows: list[dict[str, Any]] = []

    for axis_id in axis_ids:
        for sp in strategies:
            xs: list[float] = []
            ys: list[float] = []
            ys_active: list[float] = []
            xs_active: list[float] = []
            for rec in probes:
                if rec.signal_date is None:
                    continue
                z = rec.axis_z.get(axis_id)
                if z is None:
                    continue
                idx = strat_idx[sp.strategy_id].get(rec.signal_date)
                if idx is None:
                    continue
                exc = sp.daily_excess_pct[idx]
                xs.append(z)
                ys.append(exc)
                if sp.in_market_flag[idx]:
                    xs_active.append(z)
                    ys_active.append(exc)
            corr = _pearson(xs, ys)
            corr_active = _pearson(xs_active, ys_active)
            med = statistics.median(xs) if xs else 0.0
            high_exc = [ys[i] for i in range(len(xs)) if xs[i] >= med]
            low_exc = [ys[i] for i in range(len(xs)) if xs[i] < med]
            rows.append(
                {
                    "probe_axis": axis_id,
                    "strategy_id": sp.strategy_id,
                    "strategy_label": sp.label,
                    "n_aligned": len(xs),
                    "corr_z_vs_next_day_excess": corr,
                    "corr_active_days": corr_active,
                    "mean_excess_high_z": _mean(high_exc),
                    "mean_excess_low_z": _mean(low_exc),
                    "spread_high_minus_low": round(
                        (_mean(high_exc) or 0) - (_mean(low_exc) or 0), 4
                    )
                    if high_exc and low_exc
                    else None,
                }
            )
    return rows


def stratify_by_market_type(
    probes: list[ProbeDayRecord],
    strategies: list[UnifiedStrategyPanel],
) -> list[dict[str, Any]]:
    strat_idx = {p.strategy_id: _panel_by_date(p) for p in strategies}
    by_type: dict[str, list[ProbeDayRecord]] = {}
    for rec in probes:
        by_type.setdefault(rec.market_type_id, []).append(rec)

    rows: list[dict[str, Any]] = []
    for type_id, recs in sorted(by_type.items(), key=lambda x: -len(x[1])):
        label = recs[0].market_type_label if recs else type_id
        for sp in strategies:
            excesses: list[float] = []
            active_excess: list[float] = []
            for rec in recs:
                if not rec.signal_date:
                    continue
                idx = strat_idx[sp.strategy_id].get(rec.signal_date)
                if idx is None:
                    continue
                e = sp.daily_excess_pct[idx]
                excesses.append(e)
                if sp.in_market_flag[idx]:
                    active_excess.append(e)
            rows.append(
                {
                    "market_type_id": type_id,
                    "market_type_label": label,
                    "n_days": len(recs),
                    "strategy_id": sp.strategy_id,
                    "strategy_label": sp.label,
                    "mean_daily_excess_pct": _mean(excesses),
                    "mean_excess_in_market": _mean(active_excess),
                    "n_in_market_days": len(active_excess),
                }
            )
    return rows


def stratify_by_vol_regime(
    probes: list[ProbeDayRecord],
    strategies: list[UnifiedStrategyPanel],
) -> list[dict[str, Any]]:
    strat_idx = {p.strategy_id: _panel_by_date(p) for p in strategies}
    by_vol: dict[str, list[ProbeDayRecord]] = {}
    for rec in probes:
        by_vol.setdefault(rec.vol_regime, []).append(rec)
    rows: list[dict[str, Any]] = []
    for regime, recs in sorted(by_vol.items(), key=lambda x: -len(x[1])):
        for sp in strategies:
            excesses = []
            for rec in recs:
                if not rec.signal_date:
                    continue
                idx = strat_idx[sp.strategy_id].get(rec.signal_date)
                if idx is not None:
                    excesses.append(sp.daily_excess_pct[idx])
            rows.append(
                {
                    "vol_regime": regime,
                    "n_days": len(recs),
                    "strategy_id": sp.strategy_id,
                    "strategy_label": sp.label,
                    "mean_daily_excess_pct": _mean(excesses),
                }
            )
    return rows


def probe_stability_metrics(
    probes: list[ProbeDayRecord],
    *,
    axis_ids: list[str],
    min_n: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for axis_id in axis_ids:
        zs = [rec.axis_z[axis_id] for rec in probes if rec.axis_z.get(axis_id) is not None]
        ns = [rec.axis_n.get(axis_id, 0) for rec in probes]
        sufficient_days = sum(1 for rec in probes if rec.axis_sufficient.get(axis_id))
        rows.append(
            {
                "axis_id": axis_id,
                "n_probe_days": len(probes),
                "n_z_available": len(zs),
                "pct_sufficient": round(sufficient_days / len(probes) * 100, 1) if probes else 0,
                "mean_n_signals": round(statistics.mean(ns), 1) if ns else 0,
                "median_n_signals": round(statistics.median(ns), 1) if ns else 0,
                "lag1_autocorr_z": _lag1_autocorr(zs),
                "z_std": round(statistics.pstdev(zs), 4) if len(zs) >= 2 else None,
            }
        )
    market_types: dict[str, int] = {}
    for rec in probes:
        market_types[rec.market_type_id] = market_types.get(rec.market_type_id, 0) + 1
    return rows


def strategy_pairwise_all(panels: list[UnifiedStrategyPanel]) -> list[dict[str, Any]]:
    """Reuse complementarity pairwise on UnifiedStrategyPanel → StrategyDailyPanel shim."""
    shims: list[StrategyDailyPanel] = []
    for p in panels:
        shims.append(
            StrategyDailyPanel(
                topic_id=p.strategy_id,
                label=p.label,
                short_id=p.strategy_id[:12],
                cluster="mixed",
                engine="slot",
                dates=p.dates,
                daily_return_pct=p.daily_return_pct,
                signal_flag=p.signal_flag,
                in_market_flag=p.in_market_flag,
                signal_dates=set(),
                in_market_dates=set(),
                n_periods=0,
                signal_days=sum(p.signal_flag),
                exposure_pct=0.0,
            )
        )
    pairs: list[dict[str, Any]] = []
    for i in range(len(shims)):
        for j in range(i + 1, len(shims)):
            m = pairwise_metrics(shims[i], shims[j])
            m["a_id"] = panels[i].strategy_id
            m["b_id"] = panels[j].strategy_id
            pairs.append(m)
    return pairs


def best_strategy_per_market_type(
    stratified: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_type: dict[str, list[dict[str, Any]]] = {}
    for row in stratified:
        if row.get("n_in_market_days", 0) < 3:
            continue
        key = row["market_type_id"]
        by_type.setdefault(key, []).append(row)
    out: list[dict[str, Any]] = []
    for type_id, rows in by_type.items():
        ranked = sorted(
            rows,
            key=lambda r: (r.get("mean_excess_in_market") is None, -(r.get("mean_excess_in_market") or -999)),
        )
        if not ranked:
            continue
        best = ranked[0]
        out.append(
            {
                "market_type_id": type_id,
                "market_type_label": best["market_type_label"],
                "n_days": best["n_days"],
                "best_strategy_id": best["strategy_id"],
                "best_strategy_label": best["strategy_label"],
                "mean_excess_in_market": best["mean_excess_in_market"],
                "runner_up": ranked[1]["strategy_id"] if len(ranked) > 1 else None,
            }
        )
    out.sort(key=lambda r: -r["n_days"])
    return out


def day_type_taxonomy(probes: list[ProbeDayRecord]) -> dict[str, Any]:
    """日型分類統計 · meta_router + vol_regime + dominant_axis + quartile."""
    combo: dict[str, int] = {}
    quartile_combo: dict[str, int] = {}
    for rec in probes:
        dom = rec.dominant_axis or "none"
        key = f"{rec.vol_regime}|{rec.market_type_id}|dom={dom}"
        combo[key] = combo.get(key, 0) + 1
        qparts = [
            f"{ax}Q{rec.axis_quartile.get(ax)}"
            for ax in sorted(rec.axis_quartile)
            if rec.axis_quartile.get(ax) is not None
        ]
        if qparts:
            qlabel = "+".join(qparts[:2])
            quartile_combo[qlabel] = quartile_combo.get(qlabel, 0) + 1
    market_type = {}
    vol_regime = {}
    dominant = {}
    for rec in probes:
        market_type[rec.market_type_id] = market_type.get(rec.market_type_id, 0) + 1
        vol_regime[rec.vol_regime] = vol_regime.get(rec.vol_regime, 0) + 1
        if rec.dominant_axis:
            dominant[rec.dominant_axis] = dominant.get(rec.dominant_axis, 0) + 1
    return {
        "market_type_counts": market_type,
        "vol_regime_counts": vol_regime,
        "dominant_axis_counts": dominant,
        "combined_top10": sorted(
            [{"label": k, "n_days": v} for k, v in combo.items()],
            key=lambda x: -x["n_days"],
        )[:10],
        "quartile_combo_top10": sorted(
            [{"label": k, "n_days": v} for k, v in quartile_combo.items()],
            key=lambda x: -x["n_days"],
        )[:10],
    }


def walk_forward_day_type_validation(
    probes: list[ProbeDayRecord],
    strategies: list[UnifiedStrategyPanel],
    *,
    train_end: str = DEFAULT_WF_TRAIN_END,
    min_in_market_days: int = 3,
) -> dict[str, Any]:
    """2025 H1 定規則 → H2 驗證日型→策略映射。"""
    train = [p for p in probes if p.probe_date <= train_end]
    test = [p for p in probes if p.probe_date > train_end]
    strat_idx = {p.strategy_id: _panel_by_date(p) for p in strategies}
    sid_order = [p.strategy_id for p in strategies]

    train_stratified = stratify_by_market_type(train, strategies)
    train_rules = best_strategy_per_market_type(train_stratified)
    rule_map = {r["market_type_id"]: r for r in train_rules}

    chosen_excess: list[float] = []
    baseline_excess: list[float] = []
    swap_excess: list[float] = []
    hits = 0
    n_routed = 0
    per_type_oos: list[dict[str, Any]] = []

    for rec in test:
        if not rec.signal_date:
            continue
        rule = rule_map.get(rec.market_type_id)
        if not rule:
            continue
        best_sid = rule["best_strategy_id"]
        excess_today: list[tuple[str, float]] = []
        for sp in strategies:
            idx = strat_idx[sp.strategy_id].get(rec.signal_date)
            if idx is not None:
                excess_today.append((sp.strategy_id, sp.daily_excess_pct[idx]))
        if not excess_today:
            continue
        chosen_idx = strat_idx[best_sid].get(rec.signal_date)
        if chosen_idx is None:
            continue
        chosen_e = strategies[sid_order.index(best_sid)].daily_excess_pct[chosen_idx]
        median_e = statistics.median([e for _, e in excess_today])
        swap_idx = strat_idx["rrg-mono-swap-accel"].get(rec.signal_date)
        swap_e = (
            strategies[sid_order.index("rrg-mono-swap-accel")].daily_excess_pct[swap_idx]
            if swap_idx is not None and "rrg-mono-swap-accel" in sid_order
            else None
        )
        chosen_excess.append(chosen_e)
        baseline_excess.append(median_e)
        if swap_e is not None:
            swap_excess.append(swap_e)
        if chosen_e >= median_e:
            hits += 1
        n_routed += 1

    for type_id, rule in rule_map.items():
        type_test = [p for p in test if p.market_type_id == type_id]
        if len(type_test) < 2:
            continue
        best_sid = rule["best_strategy_id"]
        oos_excess = []
        train_excess = []
        for rec in type_test:
            if not rec.signal_date:
                continue
            idx = strat_idx[best_sid].get(rec.signal_date)
            if idx is not None:
                oos_excess.append(strategies[sid_order.index(best_sid)].daily_excess_pct[idx])
        for rec in train:
            if rec.market_type_id != type_id or not rec.signal_date:
                continue
            idx = strat_idx[best_sid].get(rec.signal_date)
            if idx is not None:
                train_excess.append(strategies[sid_order.index(best_sid)].daily_excess_pct[idx])
        per_type_oos.append(
            {
                "market_type_id": type_id,
                "market_type_label": rule["market_type_label"],
                "train_n_days": sum(1 for p in train if p.market_type_id == type_id),
                "test_n_days": len(type_test),
                "train_mean_excess": _mean(train_excess),
                "test_mean_excess": _mean(oos_excess),
                "best_strategy_id": best_sid,
                "oos_holds": (
                    _mean(oos_excess) is not None
                    and _mean(train_excess) is not None
                    and (_mean(oos_excess) or 0) >= (_mean(train_excess) or 0) - 0.15
                ),
            }
        )

    mean_chosen = _mean(chosen_excess)
    mean_baseline = _mean(baseline_excess)
    mean_swap = _mean(swap_excess)
    hit_rate = round(hits / n_routed, 4) if n_routed else None
    passed = (
        n_routed >= 15
        and mean_chosen is not None
        and mean_baseline is not None
        and mean_chosen > mean_baseline
        and (hit_rate or 0) >= 0.5
    )

    return {
        "train_end": train_end,
        "train_days": len(train),
        "test_days": len(test),
        "n_routed_test_days": n_routed,
        "hit_rate_vs_median": hit_rate,
        "mean_chosen_excess_pct": mean_chosen,
        "mean_median_excess_pct": mean_baseline,
        "mean_swap_accel_excess_pct": mean_swap,
        "spread_chosen_minus_median": round((mean_chosen or 0) - (mean_baseline or 0), 4)
        if mean_chosen is not None and mean_baseline is not None
        else None,
        "train_rules": train_rules,
        "per_market_type_oos": per_type_oos,
        "walk_forward_passed": passed,
        "verdict": (
            "H1 規則在 H2 仍優於中位策略 · 可進入 Phase 2 meta-labeling"
            if passed
            else "H2 未確認 H1 日型→策略映射 · 僅作研究參考"
        ),
    }


def run_probe_strategy_research(
    *,
    date_start: str = DEFAULT_DATE_START,
    date_end: str | None = None,
    db_path: Path | str | None = None,
    out_dir: Path | None = None,
    cfg: BacktestStandardConfig | None = None,
) -> dict[str, Any]:
    cfg = cfg or load_backtest_standard_config()
    probe_cfg = cfg.probe_radar
    if probe_cfg is None:
        raise ValueError("probe_radar missing in config")
    meta_router = cfg.meta_router or {}
    end = date_end or date.today().isoformat()

    conn = connect(Path(db_path) if db_path else DEFAULT_DB_PATH)
    try:
        probe_dates = _dates_with_kbar(conn, date_start, end)
        if len(probe_dates) < probe_cfg.zscore_lookback_days + 5:
            raise ValueError(f"insufficient kbar probe dates: {len(probe_dates)}")

        universe = resolve_universe(conn, probe_cfg, probe_dates[-1])
        probes = build_probe_daily_panel(
            conn,
            probe_cfg=probe_cfg,
            meta_router=meta_router,
            probe_dates=probe_dates,
            universe=universe,
        )
        strategies = load_all_strategy_panels(
            conn, cfg=cfg, date_start=date_start, date_end=end
        )

        axis_ids = [ax.axis_id for ax in probe_cfg.axes if ax.source != "vol_state"]
        axis_ids.append("volatility")

        stability = probe_stability_metrics(
            probes, axis_ids=axis_ids, min_n=probe_cfg.min_n_signals
        )
        taxonomy = day_type_taxonomy(probes)
        probe_strat_corr = probe_strategy_correlations(probes, strategies, axis_ids=axis_ids)
        stratified = stratify_by_market_type(probes, strategies)
        vol_stratified = stratify_by_vol_regime(probes, strategies)
        pairwise = strategy_pairwise_all(strategies)
        recommendations = best_strategy_per_market_type(stratified)

        walk_forward = walk_forward_day_type_validation(
            probes,
            strategies,
            train_end=str(meta_router.get("walk_forward_train_end") or DEFAULT_WF_TRAIN_END),
        )

        strong_links = [
            r
            for r in probe_strat_corr
            if r["n_aligned"] >= 30
            and r.get("corr_z_vs_next_day_excess") is not None
            and abs(r["corr_z_vs_next_day_excess"]) >= 0.15
        ]
        strong_links.sort(
            key=lambda r: abs(r["corr_z_vs_next_day_excess"] or 0), reverse=True
        )

        payload: dict[str, Any] = {
            "schema_version": "market-probe-strategy-research-v2",
            "date_start": date_start,
            "date_end": end,
            "n_probe_days": len(probes),
            "n_strategies": len(strategies),
            "strategy_ids": [p.strategy_id for p in strategies],
            "probe_config": {
                "min_n_signals": probe_cfg.min_n_signals,
                "min_bars_per_day": probe_cfg.min_bars_per_day,
                "zscore_lookback_days": probe_cfg.zscore_lookback_days,
            },
            "meta_router_version": meta_router.get("version"),
            "stability": stability,
            "stability_target_pct": STABILITY_PCT_TARGET,
            "day_type_taxonomy": taxonomy,
            "probe_strategy_correlation": probe_strat_corr,
            "strong_probe_strategy_links": strong_links[:20],
            "stratify_market_type": stratified,
            "stratify_vol_regime": vol_stratified,
            "strategy_pairwise": pairwise,
            "recommendations_per_market_type": recommendations,
            "walk_forward": walk_forward,
            "quality_verdict": _quality_verdict(
                stability, probes, probe_cfg.min_n_signals, target_pct=STABILITY_PCT_TARGET
            ),
        }
    finally:
        conn.close()

    out = out_dir or DEFAULT_OUT_DIR
    out.mkdir(parents=True, exist_ok=True)
    tag = end.replace("-", "")
    json_path = out / f"probe_strategy_research_{tag}.json"
    md_path = out / f"probe_strategy_research_{tag}.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(_render_markdown(payload), encoding="utf-8")

    return {"json_path": str(json_path), "md_path": str(md_path), "payload": payload}


def _quality_verdict(
    stability: list[dict[str, Any]],
    probes: list[ProbeDayRecord],
    min_n: int,
    *,
    target_pct: float = STABILITY_PCT_TARGET,
) -> dict[str, Any]:
    weather_axes = [s for s in stability if s["axis_id"] != "volatility"]
    avg_pct = statistics.mean([s["pct_sufficient"] for s in weather_axes]) if weather_axes else 0
    min_pct = min((s["pct_sufficient"] for s in weather_axes), default=0)
    avg_n = statistics.mean([s["mean_n_signals"] for s in weather_axes]) if weather_axes else 0
    autocorrs = [s["lag1_autocorr_z"] for s in weather_axes if s.get("lag1_autocorr_z") is not None]
    mean_ac = statistics.mean(autocorrs) if autocorrs else None
    stable = (
        min_pct >= target_pct
        and avg_pct >= target_pct - 5
        and avg_n >= min_n
        and (mean_ac is None or abs(mean_ac) < 0.85)
    )
    return {
        "stable_enough_for_routing": stable,
        "target_pct_sufficient": target_pct,
        "min_axis_pct_sufficient": round(min_pct, 1),
        "avg_pct_axis_sufficient": round(avg_pct, 1),
        "avg_mean_n_signals": round(avg_n, 1),
        "mean_lag1_autocorr_z": round(mean_ac, 4) if mean_ac is not None else None,
        "n_classified_days": len(probes),
        "notes": (
            f"各天氣軸 ≥{target_pct}% 達標 · 探針可作日型路由"
            if stable
            else f"至少一天氣軸 <{target_pct}% 達標 · 請先 backfill_probe_kbar"
        ),
    }


def _render_markdown(data: dict[str, Any]) -> str:
    q = data["quality_verdict"]
    lines = [
        "# Market probe × strategy research",
        "",
        f"- Window: {data['date_start']} → {data['date_end']}",
        f"- Probe days: {data['n_probe_days']} · Strategies: {data['n_strategies']}",
        f"- **Stable for routing?** {'✓' if q['stable_enough_for_routing'] else '✗'} — {q['notes']}",
        "",
        "## 1. Probe stability（探針品質）",
        "",
        "| Axis | % sufficient | mean n | lag1 ρ(z) | z std |",
        "|------|-------------|--------|-----------|-------|",
    ]
    for s in data["stability"]:
        lines.append(
            f"| {s['axis_id']} | {s['pct_sufficient']}% | {s['mean_n_signals']} | "
            f"{s.get('lag1_autocorr_z', '—')} | {s.get('z_std', '—')} |"
        )

    tax = data["day_type_taxonomy"]
    lines.extend(["", "## 2. Day types（日型分類）", ""])
    lines.append("### meta_router market types")
    for k, v in sorted(tax["market_type_counts"].items(), key=lambda x: -x[1]):
        lines.append(f"- `{k}`: {v} days")
    lines.append("")
    lines.append("### vol_regime")
    for k, v in sorted(tax["vol_regime_counts"].items(), key=lambda x: -x[1]):
        lines.append(f"- `{k}`: {v} days")
    lines.append("")
    lines.append("### Top combined labels")
    for row in tax["combined_top10"][:5]:
        lines.append(f"- {row['label']}: {row['n_days']} days")
    if tax.get("quartile_combo_top10"):
        lines.append("")
        lines.append("### Quartile combos")
        for row in tax["quartile_combo_top10"][:5]:
            lines.append(f"- {row['label']}: {row['n_days']} days")

    lines.extend(["", "## 3. Strategy fit by market type（日型 → 策略超額）", ""])
    lines.append("| Day type | n | Best strategy | mean excess (in-market) |")
    lines.append("|----------|---|---------------|-------------------------|")
    for r in data["recommendations_per_market_type"]:
        lines.append(
            f"| {r['market_type_label']} | {r['n_days']} | {r['best_strategy_label']} | "
            f"{r.get('mean_excess_in_market', '—')}% |"
        )

    lines.extend(["", "## 4. Probe → strategy correlation（T-1 z vs T excess）", ""])
    lines.append("| Axis | Strategy | n | ρ | high_z − low_z |")
    lines.append("|------|----------|---|---|----------------|")
    for r in data["strong_probe_strategy_links"][:15]:
        lines.append(
            f"| {r['probe_axis']} | {r['strategy_label']} | {r['n_aligned']} | "
            f"{r['corr_z_vs_next_day_excess']} | {r.get('spread_high_minus_low', '—')} |"
        )

    lines.extend(["", "## 5. Strategy similarity / complement", ""])
    lines.append("| A | B | daily ρ | complement | pattern |")
    lines.append("|---|---|---------|------------|---------|")
    top_pairs = sorted(
        data["strategy_pairwise"],
        key=lambda p: p.get("complement_score") or 0,
        reverse=True,
    )[:10]
    for p in top_pairs:
        lines.append(
            f"| {p.get('a_id', p['a'])} | {p.get('b_id', p['b'])} | "
            f"{p.get('daily_return_corr')} | {p.get('complement_score')} | {p.get('pattern')} |"
        )

    lines.extend(
        [
            "",
            "## 7. Walk-forward（H1 定規則 → H2 驗證）",
            "",
        ]
    )
    wf = data.get("walk_forward") or {}
    lines.append(f"- Train through: **{wf.get('train_end', '—')}** · test days: {wf.get('n_routed_test_days', '—')}")
    lines.append(
        f"- **Passed?** {'✓' if wf.get('walk_forward_passed') else '✗'} — {wf.get('verdict', '')}"
    )
    lines.append(
        f"- Hit rate vs median: {wf.get('hit_rate_vs_median')} · "
        f"chosen−median: {wf.get('spread_chosen_minus_median')}%"
    )
    if wf.get("per_market_type_oos"):
        lines.append("")
        lines.append("| Day type | test n | train excess | test excess | holds |")
        lines.append("|----------|--------|--------------|-------------|-------|")
        for r in wf["per_market_type_oos"]:
            lines.append(
                f"| {r['market_type_label']} | {r['test_n_days']} | "
                f"{r.get('train_mean_excess', '—')} | {r.get('test_mean_excess', '—')} | "
                f"{'✓' if r.get('oos_holds') else '✗'} |"
            )

    lines.extend(
        [
            "",
            "## 8. Classification summary",
            "",
            "探針分類層級：",
            "1. **vol_regime** — Holmberg IX0001 |return| decile（high / normal / low）",
            "2. **market_type** — meta_router v2（規則 + Q4 四分位日型）",
            "3. **dominant_axis** — 當日 z 最高的探針軸",
            "4. **combined** — vol + market_type + dominant",
            "",
            "_Research · T-1 probe → T strategy excess · walk-forward H1/H2._",
        ]
    )
    return "\n".join(lines) + "\n"
