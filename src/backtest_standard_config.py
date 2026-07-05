"""Load config/backtest_standard.yaml (跨軌比較層 SSOT)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from stock_db import PROJECT_ROOT

DEFAULT_CONFIG = PROJECT_ROOT / "config" / "backtest_standard.yaml"
DEFAULT_COMPARISON_NOTIONAL_NTD = 100_000.0


def comparison_notional_ntd() -> float:
    """Single notional SSOT for backtest / summary JSON / comparison layer."""
    return load_backtest_standard_config().comparison_notional_ntd


@dataclass(frozen=True)
class TrackAdapterSpec:
    strategy_id: str
    source: str
    n_slots: int
    earliest_data: str | None = None

    def total_capital_ntd(self, comparison_notional_ntd: float) -> float:
        return comparison_notional_ntd


@dataclass(frozen=True)
class EnsembleMemberSpec:
    strategy_id: str
    priority: int


@dataclass(frozen=True)
class EnsembleTeamSpec:
    team_id: str
    label: str
    window_id: str
    n_slots: int
    pool_mode: str
    members: tuple[EnsembleMemberSpec, ...]


@dataclass(frozen=True)
class ProbeAxisSpec:
    axis_id: str
    label: str
    probe_modes: tuple[str, ...]
    source: str | None = None


@dataclass(frozen=True)
class ProbeRadarConfig:
    benchmark: str
    zscore_lookback_days: int
    universe: str
    min_bars_per_day: int
    min_n_signals: int
    min_n_for_zscore: int
    probe_exit: str
    probe_use_stop: bool
    vol_state: dict[str, Any]
    axes: tuple[ProbeAxisSpec, ...]


@dataclass(frozen=True)
class BacktestStandardConfig:
    version: str
    benchmark: str
    comparison_notional_ntd: float
    track_adapters: dict[str, TrackAdapterSpec]
    ensemble_teams: dict[str, EnsembleTeamSpec]
    probe_radar: ProbeRadarConfig | None
    meta_router: dict[str, Any] | None
    windows: dict[str, dict[str, Any]]
    min_trading_days_for_annualization: int
    cost_model: dict[str, Any]
    excess_definition: str
    significance: dict[str, Any]
    output: dict[str, str]


def load_backtest_standard_config(path: Path | None = None) -> BacktestStandardConfig:
    p = path or DEFAULT_CONFIG
    raw: dict[str, Any] = {}
    if p.is_file():
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    comparison_notional = float(
        raw.get("comparison_notional_ntd") or DEFAULT_COMPARISON_NOTIONAL_NTD
    )
    adapters_raw = raw.get("track_adapters") or {}
    adapters: dict[str, TrackAdapterSpec] = {}
    for sid, body in adapters_raw.items():
        if not isinstance(body, dict):
            continue
        adapters[sid] = TrackAdapterSpec(
            strategy_id=sid,
            source=str(body.get("source") or ""),
            n_slots=int(body.get("n_slots") or 3),
            earliest_data=body.get("earliest_data"),
        )

    teams_raw = raw.get("ensemble_teams") or {}
    ensemble_teams: dict[str, EnsembleTeamSpec] = {}
    for tid, body in teams_raw.items():
        if not isinstance(body, dict):
            continue
        members_raw = body.get("members") or []
        members: list[EnsembleMemberSpec] = []
        for m in members_raw:
            if not isinstance(m, dict):
                continue
            members.append(
                EnsembleMemberSpec(
                    strategy_id=str(m.get("strategy_id") or ""),
                    priority=int(m.get("priority") or 99),
                )
            )
        ensemble_teams[tid] = EnsembleTeamSpec(
            team_id=tid,
            label=str(body.get("label") or tid),
            window_id=str(body.get("window_id") or "cmp_max_common"),
            n_slots=int(body.get("n_slots") or 5),
            pool_mode=str(body.get("pool_mode") or "shared_pool_yield_priority"),
            members=tuple(members),
        )

    probe_radar_raw = raw.get("probe_radar")
    probe_radar: ProbeRadarConfig | None = None
    if isinstance(probe_radar_raw, dict):
        axes_list: list[ProbeAxisSpec] = []
        for ax in probe_radar_raw.get("axes") or []:
            if not isinstance(ax, dict):
                continue
            modes = ax.get("probe_modes") or ([ax["probe_mode"]] if ax.get("probe_mode") else [])
            axes_list.append(
                ProbeAxisSpec(
                    axis_id=str(ax.get("axis_id") or ""),
                    label=str(ax.get("label") or ""),
                    probe_modes=tuple(str(m) for m in modes),
                    source=ax.get("source"),
                )
            )
        probe_radar = ProbeRadarConfig(
            benchmark=str(probe_radar_raw.get("benchmark") or "IX0001"),
            zscore_lookback_days=int(probe_radar_raw.get("zscore_lookback_days") or 20),
            universe=str(probe_radar_raw.get("universe") or "etf_constituent_watchlist_intraday"),
            min_bars_per_day=int(probe_radar_raw.get("min_bars_per_day") or 30),
            min_n_signals=int(probe_radar_raw.get("min_n_signals") or 50),
            min_n_for_zscore=int(probe_radar_raw.get("min_n_for_zscore") or 30),
            probe_exit=str(probe_radar_raw.get("probe_exit") or "same_day_close"),
            probe_use_stop=bool(probe_radar_raw.get("probe_use_stop", False)),
            vol_state=dict(probe_radar_raw.get("vol_state") or {}),
            axes=tuple(axes_list),
        )

    meta_router = raw.get("meta_router")
    if not isinstance(meta_router, dict):
        meta_router = None

    return BacktestStandardConfig(
        version=str(raw.get("version") or "backtest-standard-v0"),
        benchmark=str(raw.get("benchmark") or "IX0001"),
        comparison_notional_ntd=comparison_notional,
        track_adapters=adapters,
        ensemble_teams=ensemble_teams,
        probe_radar=probe_radar,
        meta_router=meta_router,
        windows=dict(raw.get("windows") or {}),
        min_trading_days_for_annualization=int(
            raw.get("min_trading_days_for_annualization") or 120
        ),
        cost_model=dict(raw.get("cost_model") or {}),
        excess_definition=str(raw.get("excess_definition") or "interval_excess_pct"),
        significance=dict(raw.get("significance") or {}),
        output=dict(raw.get("output") or {}),
    )
