"""Market probe radar · Holmberg vol state + intraday probe axes → meta routing.

T-1 收盤後：以當日 1m 探針評分各軸，結合 IX0001 波動分位，輸出 T 日策略優先序建議。
Phase 1：規則 meta_router（非 ML meta-labeling）。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import mean, pstdev
from typing import Any

import math

from analytics.bench import bench_return_entry_to_exit
from backtest_standard_config import (
    BacktestStandardConfig,
    ProbeAxisSpec,
    ProbeRadarConfig,
    load_backtest_standard_config,
)
from flow_returns import return_pct, trading_dates_after
from market_benchmark import latest_trading_date, load_benchmark_close
from research.backtest.probe_weather_detectors import detect_probe_weather_entry
from stock_db import PROJECT_ROOT
from stock_db.etf import load_etf_constituent_watchlist
from stock_db.kbar import load_kbar_day_bars

DEFAULT_OUT_DIR = PROJECT_ROOT / "reports" / "daily" / "regime"


@dataclass(frozen=True)
class AxisScore:
    axis_id: str
    label: str
    raw_score: float | None
    n_signals: int
    hit_rate: float | None
    z_score: float | None
    probe_modes: tuple[str, ...]
    sample_sufficient: bool
    vol_decile: int | None = None


@dataclass(frozen=True)
class ProbeRadarResult:
    asof_date: str
    signal_for_date: str | None
    benchmark: str
    vol_decile: int | None
    vol_regime: str
    axes: tuple[AxisScore, ...]
    market_type_id: str
    market_type_label: str
    suggested_team_id: str
    priority_boost: dict[str, int]
    slot_cap_scale: float


def abs_return_decile(
    historical_abs_returns: list[float],
    asof_abs_return: float,
    *,
    n_deciles: int = 10,
) -> int | None:
    """Holmberg-style decile · 1=最低波動 · n_deciles=最高。"""
    if not historical_abs_returns or asof_abs_return < 0:
        return None
    pool = sorted(historical_abs_returns)
    n = len(pool)
    rank = sum(1 for v in pool if v <= asof_abs_return)
    pct = rank / n
    decile = int(pct * n_deciles)
    if decile < 1:
        decile = 1
    if decile > n_deciles:
        decile = n_deciles
    return decile


def compute_ix0001_vol_state(
    conn: sqlite3.Connection,
    asof_date: str,
    *,
    rank_lookback_days: int = 252,
    n_deciles: int = 10,
    high_decile_min: int = 7,
    low_decile_max: int = 3,
    benchmark: str = "IX0001",
) -> dict[str, Any]:
    bench = load_benchmark_close(conn, code=benchmark)
    if bench.empty or asof_date not in bench.index:
        return {
            "vol_decile": None,
            "vol_regime": "unknown",
            "asof_abs_return_pct": None,
        }
    rets = bench.pct_change().abs() * 100.0
    dates = [d for d in bench.index.astype(str).tolist() if d <= asof_date]
    if len(dates) < 2:
        return {
            "vol_decile": None,
            "vol_regime": "unknown",
            "asof_abs_return_pct": None,
        }
    asof_abs = float(rets.loc[asof_date]) if asof_date in rets.index else None
    if asof_abs is not None and math.isnan(asof_abs):
        asof_abs = None
    hist_dates = dates[-(rank_lookback_days + 1) : -1]
    hist_abs = [float(rets.loc[d]) for d in hist_dates if d in rets.index and rets.loc[d] == rets.loc[d]]
    decile = None
    if asof_abs is not None and hist_abs:
        decile = abs_return_decile(hist_abs, asof_abs, n_deciles=n_deciles)
    regime = "normal"
    if decile is not None:
        if decile >= high_decile_min:
            regime = "high_vol"
        elif decile <= low_decile_max:
            regime = "low_vol"
    return {
        "vol_decile": decile,
        "vol_regime": regime,
        "asof_abs_return_pct": round(asof_abs, 4) if asof_abs is not None else None,
    }


def _probe_period_same_day(
    conn: sqlite3.Connection,
    *,
    trade_date: str,
    entry_px: float,
    bars: tuple,
    use_stop: bool = False,
    entry_minute: str = "",
    stop_px: float = 0.0,
) -> dict[str, Any] | None:
    if entry_px <= 0:
        return None
    exit_px = float(bars[-1].close)
    exit_reason = "close"
    if use_stop and stop_px > 0 and entry_minute:
        from research.backtest.expert_intraday_standalone import _stop_hit_intraday

        if _stop_hit_intraday(bars, entry_minute, stop_px):
            exit_reason = "stop"
            exit_px = stop_px
    ret = return_pct(entry_px, exit_px)
    bench = bench_return_entry_to_exit(
        conn, trade_date, trade_date, entry_price_mode="open"
    )
    excess = ret - bench if bench is not None else None
    return {
        "return_pct": ret,
        "excess_pct": excess,
        "beat_bench": excess > 0 if excess is not None else None,
        "exit_reason": exit_reason,
    }


def _kbar_universe_for_day(
    conn: sqlite3.Connection,
    stock_ids: list[str],
    trade_date: str,
    *,
    min_bars: int = 30,
) -> list[str]:
    out: list[str] = []
    for sid in stock_ids:
        row = conn.execute(
            """
            SELECT COUNT(*) AS n FROM stock_kbar_1m
            WHERE stock_id = ? AND trade_date = ? AND source IN ('finmind', 'yahoo')
            """,
            (sid, trade_date),
        ).fetchone()
        if row and int(row[0] or 0) >= min_bars:
            out.append(sid)
    return out


def score_probe_mode_on_date(
    conn: sqlite3.Connection,
    *,
    mode: str,
    trade_date: str,
    stock_ids: list[str],
    min_bars_per_day: int = 30,
    use_stop: bool = False,
) -> dict[str, Any]:
    day_stocks = _kbar_universe_for_day(
        conn, stock_ids, trade_date, min_bars=min_bars_per_day
    )
    trades: list[dict[str, Any]] = []
    for stock_id in day_stocks:
        bars = load_kbar_day_bars(conn, stock_id, trade_date)
        if not bars or len(bars) < min_bars_per_day:
            continue
        trig = detect_probe_weather_entry(mode, bars)
        if trig is None:
            continue
        period = _probe_period_same_day(
            conn,
            trade_date=trade_date,
            entry_px=float(trig.entry_px),
            entry_minute=trig.entry_minute,
            stop_px=float(trig.stop_px),
            bars=bars,
            use_stop=use_stop,
        )
        if period is not None:
            trades.append(period)
    if not trades:
        return {
            "mean_excess_pct": None,
            "mean_return_pct": None,
            "n_signals": 0,
            "hit_rate": None,
        }
    excesses = [t["excess_pct"] for t in trades if t["excess_pct"] is not None]
    returns = [t["return_pct"] for t in trades]
    hits = sum(1 for t in trades if t.get("beat_bench") is True)
    return {
        "mean_excess_pct": round(mean(excesses), 4) if excesses else None,
        "mean_return_pct": round(mean(returns), 4),
        "n_signals": len(trades),
        "hit_rate": round(hits / len(trades), 4) if trades else None,
    }


def score_axis_on_date(
    conn: sqlite3.Connection,
    axis: ProbeAxisSpec,
    trade_date: str,
    stock_ids: list[str],
    *,
    min_bars_per_day: int,
    use_stop: bool = False,
    vol_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if axis.source == "vol_state":
        decile = vol_state.get("vol_decile") if vol_state else None
        raw = float(decile) if decile is not None else None
        return {
            "axis_id": axis.axis_id,
            "label": axis.label,
            "raw_score": raw,
            "n_signals": 0,
            "hit_rate": None,
            "probe_modes": axis.probe_modes,
            "vol_decile": decile,
        }
    mode_scores: list[float] = []
    total_n = 0
    hit_rates: list[float] = []
    for mode in axis.probe_modes:
        s = score_probe_mode_on_date(
            conn,
            mode=mode,
            trade_date=trade_date,
            stock_ids=stock_ids,
            min_bars_per_day=min_bars_per_day,
            use_stop=use_stop,
        )
        if s["mean_excess_pct"] is not None:
            mode_scores.append(float(s["mean_excess_pct"]))
        total_n += int(s["n_signals"])
        if s["hit_rate"] is not None:
            hit_rates.append(float(s["hit_rate"]))
    raw = round(mean(mode_scores), 4) if mode_scores else None
    return {
        "axis_id": axis.axis_id,
        "label": axis.label,
        "raw_score": raw,
        "n_signals": total_n,
        "hit_rate": round(mean(hit_rates), 4) if hit_rates else None,
        "probe_modes": axis.probe_modes,
        "vol_decile": None,
    }


def _zscore(current: float | None, history: list[float]) -> float | None:
    if current is None or len(history) < 3:
        return None
    mu = mean(history)
    sigma = pstdev(history)
    if sigma <= 1e-9:
        return 0.0
    return round((current - mu) / sigma, 4)


def build_axis_history(
    conn: sqlite3.Connection,
    probe_cfg: ProbeRadarConfig,
    stock_ids: list[str],
    end_date: str,
    lookback_days: int,
) -> list[tuple[str, list[dict[str, Any]]]]:
    all_dates = [
        d
        for d in load_benchmark_close(conn, code=probe_cfg.benchmark).index.astype(str).tolist()
        if d <= end_date
    ]
    window = all_dates[-lookback_days:] if len(all_dates) >= lookback_days else all_dates
    out: list[tuple[str, list[dict[str, Any]]]] = []
    for d in window:
        vol = compute_ix0001_vol_state(
            conn,
            d,
            rank_lookback_days=int(probe_cfg.vol_state.get("rank_lookback_days") or 252),
            n_deciles=int(probe_cfg.vol_state.get("n_deciles") or 10),
            high_decile_min=int(probe_cfg.vol_state.get("high_decile_min") or 7),
            low_decile_max=int(probe_cfg.vol_state.get("low_decile_max") or 3),
            benchmark=probe_cfg.benchmark,
        )
        axis_rows = [
            score_axis_on_date(
                conn,
                ax,
                d,
                stock_ids,
                min_bars_per_day=probe_cfg.min_bars_per_day,
                use_stop=probe_cfg.probe_use_stop,
                vol_state=vol,
            )
            for ax in probe_cfg.axes
        ]
        out.append((d, axis_rows))
    return out


def axis_z_to_quartile(history_z: list[float], current: float | None) -> int | None:
    """Cross-sectional quartile 1–4 within trailing z history · None if insufficient."""
    if current is None or len(history_z) < 8:
        return None
    pool = sorted(history_z)
    rank = sum(1 for v in pool if v <= current)
    pct = rank / len(pool)
    q = int(pct * 4)
    if q < 1:
        q = 1
    if q > 4:
        q = 4
    return q


def apply_meta_router(
    *,
    meta_router: dict[str, Any],
    axis_scores: list[AxisScore],
    vol_decile: int | None,
    axis_quartiles: dict[str, int] | None = None,
) -> dict[str, Any]:
    default_team = str(meta_router.get("default_team_id") or "team_native_super")
    zmap = {a.axis_id: a.z_score for a in axis_scores}
    qmap = axis_quartiles or {}
    market_types = meta_router.get("market_types") or []

    def _routable_axes() -> list[AxisScore]:
        return [
            a
            for a in axis_scores
            if a.axis_id != "volatility" and a.sample_sufficient and a.z_score is not None
        ]

    def _rule_match(rules: dict[str, Any]) -> bool:
        if not rules:
            return True
        for key, val in rules.items():
            if key == "all_axes_z_max":
                probe_axes = _routable_axes()
                if not probe_axes:
                    return False
                thr = float(val)
                if any(a.z_score is None or a.z_score > thr for a in probe_axes):
                    return False
            elif key.endswith("_quartile_min"):
                axis = key[: -len("_quartile_min")]
                q = qmap.get(axis)
                if q is None or q < int(val):
                    return False
            elif key.endswith("_quartile_max"):
                axis = key[: -len("_quartile_max")]
                q = qmap.get(axis)
                if q is None or q > int(val):
                    return False
            elif key.endswith("_z_min"):
                axis = key[: -len("_z_min")]
                z = zmap.get(axis)
                if z is None:
                    return False
                if z < float(val):
                    return False
            elif key.endswith("_z_max"):
                axis = key[: -len("_z_max")]
                z = zmap.get(axis)
                if z is None:
                    return False
                if z > float(val):
                    return False
            elif key == "vol_decile_min":
                if vol_decile is None or vol_decile < int(val):
                    return False
            elif key == "vol_decile_max":
                if vol_decile is None or vol_decile > int(val):
                    return False
        return True

    for mt in market_types:
        if not isinstance(mt, dict):
            continue
        rules = mt.get("rules") or {}
        if _rule_match(rules):
            return {
                "market_type_id": str(mt.get("type_id") or "unknown"),
                "market_type_label": str(mt.get("label") or ""),
                "suggested_team_id": default_team,
                "priority_boost": dict(mt.get("priority_boost") or {}),
                "slot_cap_scale": float(mt.get("slot_cap_scale") or 1.0),
            }

    return {
        "market_type_id": "neutral",
        "market_type_label": "中性",
        "suggested_team_id": default_team,
        "priority_boost": {},
        "slot_cap_scale": 1.0,
    }


def _radar_bar(z: float | None, width: int = 12) -> str:
    if z is None:
        return "·" * width
    filled = int(round((z + 2) / 4 * width))
    filled = max(0, min(width, filled))
    return "█" * filled + "·" * (width - filled)


def render_probe_radar_markdown(result: ProbeRadarResult) -> str:
    lines = [
        f"# Market probe radar · {result.signal_for_date or result.asof_date}",
        "",
        f"- **Probe date（探針日）**: {result.asof_date}",
        f"- **Signal for（訊號適用日）**: {result.signal_for_date or '—'}",
        f"- **Benchmark**: {result.benchmark}",
        f"- **Vol decile**: {result.vol_decile or '—'} ({result.vol_regime})",
        f"- **Market type**: {result.market_type_label} (`{result.market_type_id}`)",
        f"- **Suggested team**: `{result.suggested_team_id}`",
        "",
        "## Axis scores",
        "",
        "| Axis | z | raw | n | hit% | radar |",
        "|------|---|-----|---|------|-------|",
    ]
    for ax in result.axes:
        z_disp = ax.z_score if ax.z_score is not None else ("insufficient" if not ax.sample_sufficient else "—")
        lines.append(
            f"| {ax.label} | {z_disp} "
            f"| {ax.raw_score if ax.raw_score is not None else '—'} "
            f"| {ax.n_signals} | "
            f"{round(ax.hit_rate * 100, 1) if ax.hit_rate is not None else '—'} "
            f"| `{_radar_bar(ax.z_score)}` |"
        )
    if result.priority_boost:
        lines.extend(["", "## Priority boost", ""])
        for sid, pri in sorted(result.priority_boost.items(), key=lambda x: x[1]):
            lines.append(f"- `{sid}` → priority {pri}")
    if result.slot_cap_scale != 1.0:
        lines.append(f"\n- **Slot cap scale**: {result.slot_cap_scale}")
    lines.extend(["", "_Phase 1 rule router · not walk-forward validated._"])
    return "\n".join(lines) + "\n"


def resolve_universe(
    conn: sqlite3.Connection,
    probe_cfg: ProbeRadarConfig,
    asof_date: str,
) -> list[str]:
    watch = [str(r["stock_id"]) for r in load_etf_constituent_watchlist(conn)]
    if probe_cfg.universe == "etf_constituent_watchlist_intraday":
        return watch
    if probe_cfg.universe == "etf_constituent_watchlist":
        from research.backtest.expert_intraday_standalone import stocks_with_kbar_coverage

        bench_dates = [
            d
            for d in load_benchmark_close(conn, code=probe_cfg.benchmark).index.astype(str).tolist()
            if d <= asof_date
        ]
        span = probe_cfg.zscore_lookback_days + 30
        date_start = (
            bench_dates[-span] if len(bench_dates) >= span else (bench_dates[0] if bench_dates else asof_date)
        )
        return stocks_with_kbar_coverage(
            conn,
            date_start=date_start,
            date_end=asof_date,
            min_stock_days=min(10, probe_cfg.zscore_lookback_days),
        )
    return watch


def run_probe_radar(
    conn: sqlite3.Connection,
    *,
    asof_date: str | None = None,
    cfg: BacktestStandardConfig | None = None,
    out_dir: Path | None = None,
) -> dict[str, Any]:
    cfg = cfg or load_backtest_standard_config()
    probe_cfg = cfg.probe_radar
    if probe_cfg is None:
        raise ValueError("probe_radar section missing in config/backtest_standard.yaml")
    meta_router = cfg.meta_router or {}

    asof = asof_date or latest_trading_date(conn)
    if asof is None:
        raise ValueError("no trading date available")
    next_days = trading_dates_after(conn, asof, count=1, inclusive_anchor=False)
    signal_for = next_days[0] if next_days else None

    lookback = probe_cfg.zscore_lookback_days
    universe = resolve_universe(conn, probe_cfg, asof)
    history = build_axis_history(conn, probe_cfg, universe, asof, lookback)

    vol = compute_ix0001_vol_state(
        conn,
        asof,
        rank_lookback_days=int(probe_cfg.vol_state.get("rank_lookback_days") or 252),
        n_deciles=int(probe_cfg.vol_state.get("n_deciles") or 10),
        high_decile_min=int(probe_cfg.vol_state.get("high_decile_min") or 7),
        low_decile_max=int(probe_cfg.vol_state.get("low_decile_max") or 3),
        benchmark=probe_cfg.benchmark,
    )

    today_rows = history[-1][1] if history else []
    if not today_rows:
        today_rows = [
            score_axis_on_date(
                conn,
                ax,
                asof,
                universe,
                min_bars_per_day=probe_cfg.min_bars_per_day,
                use_stop=probe_cfg.probe_use_stop,
                vol_state=vol,
            )
            for ax in probe_cfg.axes
        ]

    axis_scores: list[AxisScore] = []
    min_n = probe_cfg.min_n_signals
    min_z_n = probe_cfg.min_n_for_zscore
    for i, ax_spec in enumerate(probe_cfg.axes):
        row = today_rows[i] if i < len(today_rows) else {}
        n_sig = int(row.get("n_signals") or 0)
        sufficient = ax_spec.source == "vol_state" or n_sig >= min_n
        hist_vals = []
        for _d, rows in history[:-1]:
            if i < len(rows) and rows[i].get("raw_score") is not None:
                if ax_spec.source == "vol_state" or int(rows[i].get("n_signals") or 0) >= min_z_n:
                    hist_vals.append(float(rows[i]["raw_score"]))
        z = None
        if sufficient and n_sig >= min_z_n:
            z = _zscore(
                float(row["raw_score"]) if row.get("raw_score") is not None else None,
                hist_vals,
            )
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

    axis_quartiles: dict[str, int] = {}
    for j, ax_spec in enumerate(probe_cfg.axes):
        if ax_spec.source == "vol_state":
            continue
        hist_raw: list[float] = []
        for _d, rows in history[:-1]:
            if j < len(rows) and rows[j].get("raw_score") is not None:
                hist_raw.append(float(rows[j]["raw_score"]))
        current = axis_scores[j].raw_score if j < len(axis_scores) else None
        if current is not None:
            q = axis_z_to_quartile(hist_raw, float(current))
            if q is not None:
                axis_quartiles[ax_spec.axis_id] = q

    routing = apply_meta_router(
        meta_router=meta_router,
        axis_scores=axis_scores,
        vol_decile=vol.get("vol_decile"),
        axis_quartiles=axis_quartiles,
    )
    vol_decile = vol.get("vol_decile")
    if vol_decile is None:
        for ax in axis_scores:
            if ax.axis_id == "volatility" and ax.vol_decile is not None:
                vol_decile = ax.vol_decile
                break
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

    result = ProbeRadarResult(
        asof_date=asof,
        signal_for_date=signal_for,
        benchmark=probe_cfg.benchmark,
        vol_decile=vol_decile,
        vol_regime=vol_regime,
        axes=tuple(axis_scores),
        market_type_id=routing["market_type_id"],
        market_type_label=routing["market_type_label"],
        suggested_team_id=routing["suggested_team_id"],
        priority_boost=routing["priority_boost"],
        slot_cap_scale=float(routing["slot_cap_scale"]),
    )

    out_base = out_dir or Path(cfg.output.get("probe_radar_dir") or DEFAULT_OUT_DIR)
    out_base.mkdir(parents=True, exist_ok=True)
    tag = signal_for or asof
    json_path = out_base / f"probe_radar_{tag}.json"
    md_path = out_base / f"probe_radar_{tag}.md"

    payload = {
        "asof_date": result.asof_date,
        "signal_for_date": result.signal_for_date,
        "benchmark": result.benchmark,
        "vol_decile": result.vol_decile,
        "vol_regime": result.vol_regime,
        "vol_detail": vol,
        "axes": [asdict(a) for a in result.axes],
        "market_type_id": result.market_type_id,
        "market_type_label": result.market_type_label,
        "suggested_team_id": result.suggested_team_id,
        "priority_boost": result.priority_boost,
        "slot_cap_scale": result.slot_cap_scale,
        "universe_size": len(universe),
        "universe_with_kbar": len(
            _kbar_universe_for_day(conn, universe, asof, min_bars=probe_cfg.min_bars_per_day)
        ),
        "min_n_signals": min_n,
        "probe_use_stop": probe_cfg.probe_use_stop,
        "meta_router_version": meta_router.get("version"),
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_probe_radar_markdown(result), encoding="utf-8")

    return {
        "json_path": str(json_path),
        "md_path": str(md_path),
        "result": result,
        "payload": payload,
    }
