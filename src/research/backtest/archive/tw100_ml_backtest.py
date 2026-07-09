"""TW100 ML research backtest skeleton · Research layer only.

Baseline: cross-sectional momentum rank IC + 2-day forward label (qlib-tw-trader label shape).
Future: swap signal_fn for Alpha158 / LightGBM scores from qlib export.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from rank_stats import icir, spearman_correlation
from stock_universe import TW100_UNIVERSE_ID, load_universe_constituents

DEFAULT_SNAPSHOT_DATE = "2026-06-26"
DEFAULT_PANEL_START = "2015-01-01"
DEFAULT_MOM_WINDOW = 20
DEFAULT_LABEL_HORIZON = 2  # Ref(close,-3)/Ref(close,-1)-1 uses T+1..T+3 closes
MIN_CROSS_SECTION = 30
MIN_WARMUP_DAYS = 60


@dataclass(frozen=True)
class Tw100MlBacktestConfig:
    snapshot_date: str = DEFAULT_SNAPSHOT_DATE
    start_date: str = DEFAULT_PANEL_START
    end_date: str | None = None
    mom_window: int = DEFAULT_MOM_WINDOW
    label_horizon: int = DEFAULT_LABEL_HORIZON
    is_ratio: float = 0.7
    min_cross_section: int = MIN_CROSS_SECTION


def load_close_panel(
    conn: sqlite3.Connection,
    stock_ids: list[str],
    *,
    start: str,
    end: str | None,
) -> pd.DataFrame:
    if not stock_ids:
        return pd.DataFrame()
    placeholders = ",".join("?" * len(stock_ids))
    sql = f"""
        SELECT stock_id, trade_date, close
        FROM stock_daily_bars
        WHERE stock_id IN ({placeholders})
          AND trade_date >= ?
          AND source = 'finmind'
    """
    params: list[Any] = [*stock_ids, start]
    if end:
        sql += " AND trade_date <= ?"
        params.append(end)
    sql += " ORDER BY trade_date, stock_id"
    rows = conn.execute(sql, params).fetchall()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=["stock_id", "trade_date", "close"])
    df["close"] = df["close"].astype(float)
    panel = df.pivot(index="trade_date", columns="stock_id", values="close").sort_index()
    return panel


def momentum_signal(panel: pd.DataFrame, window: int) -> pd.DataFrame:
    """Simple baseline signal: past N-day return (placeholder for ML score)."""
    return panel / panel.shift(window) - 1.0


def forward_label(panel: pd.DataFrame, horizon: int = DEFAULT_LABEL_HORIZON) -> pd.DataFrame:
    """2-day forward return aligned at T: close(T+3)/close(T+1)-1 (PIT at T close)."""
    if horizon != 2:
        raise ValueError("skeleton only implements horizon=2 (qlib-tw-trader label)")
    entry = panel.shift(-1)
    exit_ = panel.shift(-(horizon + 1))
    return exit_ / entry - 1.0


def daily_rank_ic_series(
    signal: pd.DataFrame,
    label: pd.DataFrame,
    *,
    min_cross_section: int = MIN_CROSS_SECTION,
) -> list[tuple[str, float]]:
    out: list[tuple[str, float]] = []
    common_idx = signal.index.intersection(label.index)
    for dt in common_idx:
        sig = signal.loc[dt]
        lab = label.loc[dt]
        mask = sig.notna() & lab.notna()
        if mask.sum() < min_cross_section:
            continue
        ic = spearman_correlation(sig[mask].tolist(), lab[mask].tolist())
        if ic is not None:
            out.append((str(dt), ic))
    return out


def split_ic_series(
    ic_series: list[tuple[str, float]],
    *,
    is_ratio: float,
) -> tuple[list[float], list[float], str, str]:
    if not ic_series:
        return [], [], "", ""
    ic_series = sorted(ic_series, key=lambda x: x[0])
    cut = max(1, int(len(ic_series) * is_ratio))
    is_part = [v for _, v in ic_series[:cut]]
    oos_part = [v for _, v in ic_series[cut:]]
    is_range = f"{ic_series[0][0]}..{ic_series[cut - 1][0]}"
    oos_range = f"{ic_series[cut][0]}..{ic_series[-1][0]}" if oos_part else ""
    return is_part, oos_part, is_range, oos_range


def run_tw100_baseline_backtest(
    conn: sqlite3.Connection,
    cfg: Tw100MlBacktestConfig,
    *,
    signal_fn: Callable[[pd.DataFrame], pd.DataFrame] | None = None,
) -> dict[str, Any]:
    watch = load_universe_constituents(conn, TW100_UNIVERSE_ID, cfg.snapshot_date)
    if not watch:
        raise RuntimeError(
            f"TW100 universe empty @ {cfg.snapshot_date}; run sync_tw100_market --refresh-universe"
        )
    stock_ids = [w["stock_id"] for w in watch]
    end = cfg.end_date
    if not end:
        row = conn.execute(
            "SELECT MAX(trade_date) FROM stock_daily_bars WHERE stock_id IN ({})".format(
                ",".join("?" * len(stock_ids))
            ),
            stock_ids,
        ).fetchone()
        end = str(row[0]) if row and row[0] else cfg.start_date

    panel = load_close_panel(conn, stock_ids, start=cfg.start_date, end=end)
    if panel.empty:
        raise RuntimeError("no close panel in range")

    sig_fn = signal_fn or (lambda p: momentum_signal(p, cfg.mom_window))
    signal = sig_fn(panel)
    label = forward_label(panel, cfg.label_horizon)
    ic_series = daily_rank_ic_series(
        signal,
        label,
        min_cross_section=cfg.min_cross_section,
    )
    is_ic, oos_ic, is_range, oos_range = split_ic_series(ic_series, is_ratio=cfg.is_ratio)
    all_ic = [v for _, v in ic_series]

    bar_counts = {
        sid: int(panel[sid].notna().sum()) for sid in panel.columns if sid in stock_ids
    }
    short_history = [sid for sid, n in bar_counts.items() if n < 504]

    return {
        "schema": "tw100_ml_backtest-v1",
        "layer": "research",
        "universe_id": TW100_UNIVERSE_ID,
        "snapshot_date": cfg.snapshot_date,
        "start_date": cfg.start_date,
        "end_date": end,
        "n_constituents": len(stock_ids),
        "n_trading_days_panel": int(panel.shape[0]),
        "signal": "momentum_baseline",
        "mom_window": cfg.mom_window,
        "label": "forward_2d_return_t1_t3",
        "label_horizon": cfg.label_horizon,
        "is_ratio": cfg.is_ratio,
        "is_date_range": is_range,
        "oos_date_range": oos_range,
        "ic": {
            "n_days": len(all_ic),
            "mean": round(sum(all_ic) / len(all_ic), 6) if all_ic else None,
            "icir": round(icir(all_ic), 6) if icir(all_ic) is not None else None,
            "is_mean": round(sum(is_ic) / len(is_ic), 6) if is_ic else None,
            "is_icir": round(icir(is_ic), 6) if icir(is_ic) is not None else None,
            "oos_mean": round(sum(oos_ic) / len(oos_ic), 6) if oos_ic else None,
            "oos_icir": round(icir(oos_ic), 6) if icir(oos_ic) is not None else None,
            "oos_n_days": len(oos_ic),
        },
        "data_quality": {
            "stocks_with_bars_lt_504": short_history,
            "n_stocks_with_bars_lt_504": len(short_history),
            "min_bars_in_window": min(bar_counts.values()) if bar_counts else 0,
        },
        "notes": [
            "Research-layer skeleton; not strategy SSOT.",
            "Replace momentum_baseline with Alpha158/LightGBM when qlib path is wired.",
        ],
    }


def write_tw100_backtest_artifact(payload: dict[str, Any], out_path: Path) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return out_path
