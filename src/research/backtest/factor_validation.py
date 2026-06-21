#!/usr/bin/env python3
"""
Alphalens-style factor validation layer (Phase 2 / 2b).

Validates cross-sectional factors vs forward close-to-close returns:
Rank IC, ICIR, quantile spread / monotonicity, IC decay (train vs valid split).
Tear sheets: native HTML + optional alphalens-reloaded `create_full_tear_sheet` PNG.

用法：
  PYTHONPATH=src python src/factor_validation.py --write-reports
"""

from __future__ import annotations

import argparse
import html
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from statistics import mean

import yaml

from project_config import SCORE_VERSION
from rank_stats import icir, spearman_correlation
from report_paths import REPORTS_RESEARCH
from stock_db import PROJECT_ROOT, connect

REPORTS_DIR = REPORTS_RESEARCH / "factor_validation"
TEARSHEETS_DIR = REPORTS_DIR / "tearsheets"
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "factor_validation.yaml"
BENCHMARK_CODE = "IX0001"


@dataclass(frozen=True)
class ICDecayConfig:
    train_pct: float = 0.7
    min_split_days: int = 6
    stable_delta_min: float = -0.05
    moderate_delta_min: float = -0.15


@dataclass(frozen=True)
class TearsheetConfig:
    primary_horizon_days: int = 1
    write_html: bool = True
    alphalens_png: bool = True


@dataclass(frozen=True)
class ICDecayMetrics:
    train_ic_mean: float | None
    valid_ic_mean: float | None
    train_n_days: int
    valid_n_days: int
    decay_delta: float | None
    decay_ratio: float | None
    verdict: str
    split_date: str | None = None


@dataclass(frozen=True)
class TrackFactorConfig:
    track_id: str
    title: str
    source: str
    factors: tuple[str, ...]
    score_version: str | None = None
    model_id: str | None = None
    etf_code: str | None = None
    cohort: str | None = None


@dataclass(frozen=True)
class FactorValidationConfig:
    version: str
    lookback_trading_days: int
    forward_horizons_days: tuple[int, ...]
    min_names_per_day: int
    quantile_buckets: int
    ic_decay: ICDecayConfig
    tearsheet: TearsheetConfig
    tracks: tuple[TrackFactorConfig, ...]


@dataclass(frozen=True)
class FactorDaySlice:
    as_of_date: str
    stock_ids: tuple[str, ...]
    factor_values: tuple[float, ...]
    forward_returns_pct: tuple[float, ...]


@dataclass
class HorizonMetrics:
    horizon_days: int
    ic_mean: float | None = None
    icir_value: float | None = None
    ic_n_days: int = 0
    quantile_means_pct: list[float] = field(default_factory=list)
    quantile_spread_pct: float | None = None
    monotonicity: str = "樣本不足"
    engine: str = "native"
    ic_decay: ICDecayMetrics | None = None
    ic_by_date: list[tuple[str, float]] = field(default_factory=list)


@dataclass
class FactorValidationResult:
    track_id: str
    track_title: str
    factor: str
    status: str
    horizons: list[HorizonMetrics] = field(default_factory=list)
    n_signal_days: int = 0
    window_start: str | None = None
    window_end: str | None = None
    message: str = ""


def load_factor_validation_config(path: Path | None = None) -> FactorValidationConfig:
    p = path or DEFAULT_CONFIG
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    tracks: list[TrackFactorConfig] = []
    for tid, body in (raw.get("tracks") or {}).items():
        if not isinstance(body, dict):
            continue
        factors = body.get("factors") or []
        tracks.append(
            TrackFactorConfig(
                track_id=str(tid),
                title=str(body.get("title") or tid),
                source=str(body.get("source") or ""),
                factors=tuple(str(f) for f in factors),
                score_version=body.get("score_version"),
                model_id=body.get("model_id"),
                etf_code=body.get("etf_code"),
                cohort=body.get("cohort"),
            )
        )
    horizons = raw.get("forward_horizons_days") or [1, 5, 10]
    decay_raw = raw.get("ic_decay") or {}
    ts_raw = raw.get("tearsheet") or {}
    return FactorValidationConfig(
        version=str(raw.get("version") or "factor-validation-v1"),
        lookback_trading_days=int(raw.get("lookback_trading_days") or 90),
        forward_horizons_days=tuple(int(h) for h in horizons),
        min_names_per_day=int(raw.get("min_names_per_day") or 8),
        quantile_buckets=int(raw.get("quantile_buckets") or 5),
        ic_decay=ICDecayConfig(
            train_pct=float(decay_raw.get("train_pct", 0.7)),
            min_split_days=int(decay_raw.get("min_split_days", 6)),
            stable_delta_min=float(decay_raw.get("stable_delta_min", -0.05)),
            moderate_delta_min=float(decay_raw.get("moderate_delta_min", -0.15)),
        ),
        tearsheet=TearsheetConfig(
            primary_horizon_days=int(ts_raw.get("primary_horizon_days", 1)),
            write_html=bool(ts_raw.get("write_html", True)),
            alphalens_png=bool(ts_raw.get("alphalens_png", True)),
        ),
        tracks=tuple(tracks),
    )


def _stock_close(conn: sqlite3.Connection, stock_id: str, trade_date: str) -> float | None:
    row = conn.execute(
        """
        SELECT close FROM stock_daily_bars
        WHERE stock_id = ? AND trade_date = ? AND source = 'finmind'
        """,
        (stock_id, trade_date),
    ).fetchone()
    if row is None:
        return None
    return float(row["close"])


def _bench_close(conn: sqlite3.Connection, trade_date: str) -> float | None:
    row = conn.execute(
        """
        SELECT close FROM daily_bars
        WHERE code = ? AND date = ?
        ORDER BY CASE source WHEN 'tej' THEN 0 WHEN 'yahoo' THEN 1 ELSE 2 END
        LIMIT 1
        """,
        (BENCHMARK_CODE, trade_date),
    ).fetchone()
    if row is None:
        return None
    return float(row["close"])


def _outcome_date_after_k(
    conn: sqlite3.Connection,
    signal_date: str,
    k: int,
) -> str | None:
    if k < 1:
        return None
    rows = conn.execute(
        """
        SELECT DISTINCT trade_date AS d
        FROM stock_daily_bars
        WHERE trade_date > ? AND source = 'finmind'
        ORDER BY d ASC
        LIMIT ?
        """,
        (signal_date, k),
    ).fetchall()
    if len(rows) < k:
        return None
    outcome = str(rows[k - 1]["d"])
    if _bench_close(conn, signal_date) is None or _bench_close(conn, outcome) is None:
        return None
    return outcome


def stock_forward_return_pct(
    conn: sqlite3.Connection,
    stock_id: str,
    from_date: str,
    horizon_days: int,
) -> float | None:
    c0 = _stock_close(conn, stock_id, from_date)
    out_date = _outcome_date_after_k(conn, from_date, horizon_days)
    if c0 is None or out_date is None or c0 <= 0:
        return None
    c1 = _stock_close(conn, stock_id, out_date)
    if c1 is None:
        return None
    return (c1 / c0 - 1.0) * 100.0


def _list_pm_watchlist_dates(
    conn: sqlite3.Connection,
    *,
    score_version: str,
    as_of: str,
    lookback: int,
) -> list[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT as_of_date AS d
        FROM pm_watchlist
        WHERE score_version = ? AND as_of_date <= ?
        ORDER BY d DESC
        LIMIT ?
        """,
        (score_version, as_of, lookback),
    ).fetchall()
    return sorted(str(r["d"]) for r in rows)


def _list_vcp_dates(
    conn: sqlite3.Connection,
    *,
    model_id: str,
    as_of: str,
    lookback: int,
) -> list[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT as_of_date AS d
        FROM vcp_screen_scores_v2
        WHERE model_id = ? AND as_of_date <= ?
        ORDER BY d DESC
        LIMIT ?
        """,
        (model_id, as_of, lookback),
    ).fetchall()
    return sorted(str(r["d"]) for r in rows)


def _load_pm_factor_day(
    conn: sqlite3.Connection,
    *,
    as_of_date: str,
    score_version: str,
    factor: str,
) -> list[tuple[str, float]]:
    if factor not in {
        "investment_score",
        "flow_score",
        "chip_score",
        "tech_score",
        "catalyst_score",
        "fundamental_score",
    }:
        return []
    rows = conn.execute(
        f"""
        SELECT stock_id, {factor} AS fv
        FROM pm_watchlist
        WHERE as_of_date = ? AND score_version = ? AND {factor} IS NOT NULL
        """,
        (as_of_date, score_version),
    ).fetchall()
    out: list[tuple[str, float]] = []
    for r in rows:
        try:
            out.append((str(r["stock_id"]), float(r["fv"])))
        except (TypeError, ValueError):
            continue
    return out


def _load_vcp_factor_day(
    conn: sqlite3.Connection,
    *,
    as_of_date: str,
    model_id: str,
    factor: str,
) -> list[tuple[str, float]]:
    if factor != "composite_score":
        return []
    rows = conn.execute(
        """
        SELECT stock_id, composite_score AS fv
        FROM vcp_screen_scores_v2
        WHERE as_of_date = ? AND model_id = ?
        """,
        (as_of_date, model_id),
    ).fetchall()
    return [(str(r["stock_id"]), float(r["fv"])) for r in rows]


def build_factor_slices(
    conn: sqlite3.Connection,
    track: TrackFactorConfig,
    factor: str,
    *,
    as_of: str,
    lookback: int,
    horizon_days: int,
    min_names: int,
) -> list[FactorDaySlice]:
    slices: list[FactorDaySlice] = []

    if track.source == "pm_watchlist":
        score_version = track.score_version or SCORE_VERSION
        dates = _list_pm_watchlist_dates(
            conn, score_version=score_version, as_of=as_of, lookback=lookback
        )
        for d in dates:
            pairs = _load_pm_factor_day(
                conn, as_of_date=d, score_version=score_version, factor=factor
            )
            _append_slice(conn, slices, d, pairs, horizon_days, min_names)

    elif track.source == "vcp_screen_scores_v2":
        model_id = track.model_id or ""
        dates = _list_vcp_dates(conn, model_id=model_id, as_of=as_of, lookback=lookback)
        for d in dates:
            pairs = _load_vcp_factor_day(
                conn, as_of_date=d, model_id=model_id, factor=factor
            )
            _append_slice(conn, slices, d, pairs, horizon_days, min_names)

    return slices


def _append_slice(
    conn: sqlite3.Connection,
    out: list[FactorDaySlice],
    as_of_date: str,
    pairs: list[tuple[str, float]],
    horizon_days: int,
    min_names: int,
) -> None:
    stock_ids: list[str] = []
    factor_values: list[float] = []
    fwd_returns: list[float] = []
    for sid, fv in pairs:
        ret = stock_forward_return_pct(conn, sid, as_of_date, horizon_days)
        if ret is None:
            continue
        stock_ids.append(sid)
        factor_values.append(fv)
        fwd_returns.append(ret)
    if len(stock_ids) < min_names:
        return
    out.append(
        FactorDaySlice(
            as_of_date=as_of_date,
            stock_ids=tuple(stock_ids),
            factor_values=tuple(factor_values),
            forward_returns_pct=tuple(fwd_returns),
        )
    )


def _quantile_means(
    factor_values: list[float],
    returns_pct: list[float],
    n_buckets: int,
) -> list[float]:
    if len(factor_values) < n_buckets:
        return []
    pairs = sorted(zip(factor_values, returns_pct), key=lambda x: x[0])
    n = len(pairs)
    bucket_size = n // n_buckets
    if bucket_size < 1:
        return []
    means: list[float] = []
    for i in range(n_buckets):
        start = i * bucket_size
        end = n if i == n_buckets - 1 else (i + 1) * bucket_size
        chunk = pairs[start:end]
        if not chunk:
            return []
        means.append(mean(r for _, r in chunk))
    return means


def _monotonicity_label(q_means: list[float]) -> str:
    if len(q_means) < 2:
        return "樣本不足"
    if all(q_means[i] <= q_means[i + 1] for i in range(len(q_means) - 1)):
        return "遞增（支持因子）"
    spread = q_means[-1] - q_means[0]
    if spread > 0:
        return f"Q高-Q低={spread:.2f}%（部分遞增）"
    return "不支持"


def _aggregate_quantiles(
    slices: list[FactorDaySlice],
    n_buckets: int,
) -> tuple[list[float], str]:
    per_day: list[list[float]] = []
    for sl in slices:
        qm = _quantile_means(
            list(sl.factor_values), list(sl.forward_returns_pct), n_buckets
        )
        if len(qm) == n_buckets:
            per_day.append(qm)
    if not per_day:
        return [], "樣本不足"
    agg = [mean(day[i] for day in per_day) for i in range(n_buckets)]
    return agg, _monotonicity_label(agg)


def ic_series_from_slices(slices: list[FactorDaySlice]) -> list[tuple[str, float]]:
    out: list[tuple[str, float]] = []
    for sl in sorted(slices, key=lambda s: s.as_of_date):
        ic = spearman_correlation(list(sl.factor_values), list(sl.forward_returns_pct))
        if ic is not None:
            out.append((sl.as_of_date, ic))
    return out


def compute_ic_decay(
    slices: list[FactorDaySlice],
    *,
    cfg: ICDecayConfig,
) -> ICDecayMetrics:
    series = ic_series_from_slices(slices)
    empty = ICDecayMetrics(
        train_ic_mean=None,
        valid_ic_mean=None,
        train_n_days=0,
        valid_n_days=0,
        decay_delta=None,
        decay_ratio=None,
        verdict="樣本不足",
    )
    if len(series) < cfg.min_split_days:
        return empty

    split_idx = max(1, int(len(series) * cfg.train_pct))
    if split_idx >= len(series):
        split_idx = len(series) - 1
    train = [ic for _, ic in series[:split_idx]]
    valid = [ic for _, ic in series[split_idx:]]
    if len(train) < 2 or len(valid) < 2:
        return empty

    train_mean = mean(train)
    valid_mean = mean(valid)
    delta = valid_mean - train_mean
    ratio = valid_mean / train_mean if train_mean != 0 else None
    if delta >= cfg.stable_delta_min:
        verdict = "stable"
    elif delta >= cfg.moderate_delta_min:
        verdict = "moderate_decay"
    else:
        verdict = "severe_decay"

    return ICDecayMetrics(
        train_ic_mean=round(train_mean, 4),
        valid_ic_mean=round(valid_mean, 4),
        train_n_days=len(train),
        valid_n_days=len(valid),
        decay_delta=round(delta, 4),
        decay_ratio=round(ratio, 4) if ratio is not None else None,
        verdict=verdict,
        split_date=series[split_idx][0],
    )


def _ic_decay_verdict_label(verdict: str) -> str:
    return {
        "stable": "穩定（valid ≈ train）",
        "moderate_decay": "中度衰減（valid < train）",
        "severe_decay": "嚴重衰減（過擬合風險）",
        "樣本不足": "樣本不足",
    }.get(verdict, verdict)


def _svg_ic_sparkline(points: list[tuple[str, float]], *, width: int = 640, height: int = 120) -> str:
    if len(points) < 2:
        return "<p>IC time series: insufficient data</p>"
    vals = [v for _, v in points]
    lo, hi = min(vals), max(vals)
    span = hi - lo or 1.0
    pad = 8
    coords: list[str] = []
    for i, (_, v) in enumerate(points):
        x = pad + i * (width - 2 * pad) / max(len(points) - 1, 1)
        y = height - pad - (v - lo) / span * (height - 2 * pad)
        coords.append(f"{x:.1f},{y:.1f}")
    zero_y = height - pad - (0 - lo) / span * (height - 2 * pad) if lo <= 0 <= hi else None
    zero_line = (
        f'<line x1="{pad}" y1="{zero_y:.1f}" x2="{width-pad}" y2="{zero_y:.1f}" '
        f'stroke="#999" stroke-dasharray="4"/>'
        if zero_y is not None
        else ""
    )
    return (
        f'<svg width="{width}" height="{height}" xmlns="http://www.w3.org/2000/svg">'
        f"{zero_line}"
        f'<polyline fill="none" stroke="#2563eb" stroke-width="2" points="{" ".join(coords)}"/>'
        "</svg>"
    )


def _quantile_bar_html(q_means: list[float]) -> str:
    if not q_means:
        return "<p>Quantile returns: —</p>"
    mx = max(abs(v) for v in q_means) or 1.0
    rows = []
    for i, v in enumerate(q_means, start=1):
        w = min(100, int(abs(v) / mx * 100))
        color = "#16a34a" if v >= 0 else "#dc2626"
        rows.append(
            f'<div class="qrow"><span class="qlab">Q{i}</span>'
            f'<div class="qbar" style="width:{w}%;background:{color}"></div>'
            f'<span class="qval">{v:.2f}%</span></div>'
        )
    return '<div class="quantiles">' + "".join(rows) + "</div>"


def write_native_tear_sheet_html(
    path: Path,
    *,
    track_id: str,
    track_title: str,
    factor: str,
    horizon: HorizonMetrics,
    generated_at: str,
    cfg: FactorValidationConfig,
) -> None:
    decay = horizon.ic_decay
    decay_row = "—"
    if decay and decay.train_ic_mean is not None:
        decay_row = (
            f"train={decay.train_ic_mean:.4f} · valid={decay.valid_ic_mean:.4f} · "
            f"Δ={decay.decay_delta:.4f} · {_ic_decay_verdict_label(decay.verdict)}"
        )

    body = f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8"/>
<title>Tear sheet · {html.escape(track_id)} · {html.escape(factor)} · T+{horizon.horizon_days}</title>
<style>
body {{ font-family: system-ui, sans-serif; margin: 2rem; color: #111; max-width: 900px; }}
h1 {{ font-size: 1.25rem; }}
.meta {{ color: #555; font-size: 0.9rem; }}
table {{ border-collapse: collapse; margin: 1rem 0; }}
th, td {{ border: 1px solid #ddd; padding: 0.4rem 0.75rem; text-align: left; }}
th {{ background: #f8fafc; }}
.quantiles {{ margin: 1rem 0; }}
.qrow {{ display: flex; align-items: center; gap: 0.5rem; margin: 0.25rem 0; }}
.qlab {{ width: 2rem; font-weight: 600; }}
.qbar {{ height: 1rem; min-width: 2px; }}
.qval {{ width: 4rem; text-align: right; font-variant-numeric: tabular-nums; }}
.footer {{ margin-top: 2rem; font-size: 0.85rem; color: #666; }}
</style>
</head>
<body>
<h1>create_full_tear_sheet · {html.escape(track_title)}</h1>
<p class="meta">Track <code>{html.escape(track_id)}</code> · factor <code>{html.escape(factor)}</code> ·
horizon T+{horizon.horizon_days} · generated {html.escape(generated_at)} · engine <code>{html.escape(horizon.engine)}</code></p>
<table>
<tr><th>Rank IC mean</th><td>{_fmt(horizon.ic_mean)}</td></tr>
<tr><th>ICIR</th><td>{_fmt(horizon.icir_value)}</td></tr>
<tr><th>IC signal-days</th><td>{horizon.ic_n_days}</td></tr>
<tr><th>Quantile spread</th><td>{_fmt(horizon.quantile_spread_pct, digits=2)}%</td></tr>
<tr><th>Monotonicity</th><td>{html.escape(horizon.monotonicity)}</td></tr>
<tr><th>IC decay (train→valid)</th><td>{html.escape(decay_row)}</td></tr>
</table>
<h2>IC time series</h2>
{_svg_ic_sparkline(horizon.ic_by_date)}
<h2>Mean quantile forward returns</h2>
{_quantile_bar_html(horizon.quantile_means_pct)}
<p class="footer">Native tear sheet (Phase 2b). Alphalens PNG when <code>alphalens-reloaded</code> is installed.
Config <code>{html.escape(cfg.version)}</code> · IC decay split train_pct={cfg.ic_decay.train_pct}.</p>
</body>
</html>
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _build_alphalens_factor_data(
    conn: sqlite3.Connection,
    slices: list[FactorDaySlice],
    horizon_days: int,
):
    import pandas as pd
    from alphalens.utils import get_clean_factor_and_forward_returns

    factor_records: list[tuple[str, str, float]] = []
    for sl in slices:
        for sid, fv in zip(sl.stock_ids, sl.factor_values, strict=True):
            factor_records.append((sl.as_of_date, sid, fv))
    if not factor_records:
        return None

    stock_ids = sorted({sid for _, sid, _ in factor_records})
    dates = sorted({d for d, _, _ in factor_records})
    min_d, max_d = dates[0], dates[-1]
    out_date = _outcome_date_after_k(conn, max_d, horizon_days)
    if out_date is None:
        return None

    placeholders = ",".join("?" for _ in stock_ids)
    rows = conn.execute(
        f"""
        SELECT stock_id, trade_date, close
        FROM stock_daily_bars
        WHERE stock_id IN ({placeholders})
          AND trade_date >= ? AND trade_date <= ?
          AND source = 'finmind'
        """,
        (*stock_ids, min_d, out_date),
    ).fetchall()
    if not rows:
        return None

    price_df = pd.DataFrame(
        [{"date": str(r["trade_date"]), "asset": str(r["stock_id"]), "close": float(r["close"])} for r in rows]
    )
    price_wide = price_df.pivot(index="date", columns="asset", values="close")
    price_wide.index = pd.to_datetime(price_wide.index)
    price_wide = price_wide.sort_index()

    factor_df = pd.DataFrame(
        [{"date": d, "asset": sid, "factor": fv} for d, sid, fv in factor_records]
    )
    factor_s = factor_df.set_index(["date", "asset"])["factor"]
    factor_s.index = pd.MultiIndex.from_tuples(
        [(pd.Timestamp(d), a) for d, a in factor_s.index],
        names=["date", "asset"],
    )
    return get_clean_factor_and_forward_returns(
        factor_s,
        price_wide,
        periods=(horizon_days,),
        max_loss=0.85,
    )


def write_alphalens_tear_sheet_png(
    path: Path,
    conn: sqlite3.Connection,
    slices: list[FactorDaySlice],
    horizon_days: int,
) -> bool:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from alphalens.tears import create_full_tear_sheet
    except ImportError:
        return False

    factor_data = _build_alphalens_factor_data(conn, slices, horizon_days)
    if factor_data is None:
        return False

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        plt.close("all")
        create_full_tear_sheet(
            factor_data,
            long_short=False,
            group_neutral=False,
            set_context=False,
        )
        fig = plt.gcf()
        fig.savefig(path, dpi=120, bbox_inches="tight")
        plt.close(fig)
        return True
    except Exception:
        plt.close("all")
        return False


def tear_sheet_basename(track_id: str, factor: str, horizon_days: int) -> str:
    safe_factor = factor.replace("/", "_")
    return f"{track_id}_{safe_factor}_T{horizon_days}"


def _try_alphalens_metrics(
    conn: sqlite3.Connection,
    slices: list[FactorDaySlice],
    horizon_days: int,
) -> HorizonMetrics | None:
    """Optional alphalens-reloaded enrichment when price history is sufficient."""
    try:
        import pandas as pd
        from alphalens.performance import mean_information_coefficient
        from alphalens.utils import get_clean_factor_and_forward_returns
    except ImportError:
        return None

    if not slices:
        return None

    factor_records: list[tuple[str, str, float]] = []
    for sl in slices:
        for sid, fv in zip(sl.stock_ids, sl.factor_values, strict=True):
            factor_records.append((sl.as_of_date, sid, fv))
    if not factor_records:
        return None

    stock_ids = sorted({sid for _, sid, _ in factor_records})
    dates = sorted({d for d, _, _ in factor_records})
    min_d, max_d = dates[0], dates[-1]
    out_date = _outcome_date_after_k(conn, max_d, horizon_days)
    if out_date is None:
        return None

    placeholders = ",".join("?" for _ in stock_ids)
    rows = conn.execute(
        f"""
        SELECT stock_id, trade_date, close
        FROM stock_daily_bars
        WHERE stock_id IN ({placeholders})
          AND trade_date >= ? AND trade_date <= ?
          AND source = 'finmind'
        """,
        (*stock_ids, min_d, out_date),
    ).fetchall()
    if not rows:
        return None

    price_df = pd.DataFrame(
        [{"date": str(r["trade_date"]), "asset": str(r["stock_id"]), "close": float(r["close"])} for r in rows]
    )
    price_wide = price_df.pivot(index="date", columns="asset", values="close")
    price_wide.index = pd.to_datetime(price_wide.index)
    price_wide = price_wide.sort_index()

    factor_df = pd.DataFrame(
        [{"date": d, "asset": sid, "factor": fv} for d, sid, fv in factor_records]
    )
    factor_s = factor_df.set_index(["date", "asset"])["factor"]
    factor_s.index = pd.MultiIndex.from_tuples(
        [(pd.Timestamp(d), a) for d, a in factor_s.index],
        names=["date", "asset"],
    )

    try:
        factor_data = get_clean_factor_and_forward_returns(
            factor_s,
            price_wide,
            periods=(horizon_days,),
            max_loss=0.85,
        )
        ic_frame = mean_information_coefficient(factor_data)
        ic_val = float(ic_frame.iloc[0]) if not ic_frame.empty else None
    except Exception:
        return None

    if ic_val is None:
        return None

    ic_series = [
        spearman_correlation(list(sl.factor_values), list(sl.forward_returns_pct))
        for sl in slices
    ]
    ic_series_clean = [x for x in ic_series if x is not None]
    return HorizonMetrics(
        horizon_days=horizon_days,
        ic_mean=ic_val,
        icir_value=icir(ic_series_clean),
        ic_n_days=len(ic_series_clean),
        engine="alphalens-reloaded",
    )


def compute_horizon_metrics(
    conn: sqlite3.Connection,
    slices: list[FactorDaySlice],
    *,
    horizon_days: int,
    quantile_buckets: int,
    ic_decay_cfg: ICDecayConfig,
) -> HorizonMetrics:
    ic_series = [
        spearman_correlation(list(sl.factor_values), list(sl.forward_returns_pct))
        for sl in slices
    ]
    ic_clean = [x for x in ic_series if x is not None]
    ic_by_date = ic_series_from_slices(slices)
    q_means, mono = _aggregate_quantiles(slices, quantile_buckets)
    spread = (q_means[-1] - q_means[0]) if len(q_means) >= 2 else None

    metrics = HorizonMetrics(
        horizon_days=horizon_days,
        ic_mean=mean(ic_clean) if ic_clean else None,
        icir_value=icir(ic_clean),
        ic_n_days=len(ic_clean),
        quantile_means_pct=[round(x, 3) for x in q_means],
        quantile_spread_pct=round(spread, 3) if spread is not None else None,
        monotonicity=mono,
        engine="native",
        ic_decay=compute_ic_decay(slices, cfg=ic_decay_cfg),
        ic_by_date=ic_by_date,
    )

    al = _try_alphalens_metrics(conn, slices, horizon_days)
    if al is not None and al.ic_mean is not None:
        metrics.ic_mean = al.ic_mean
        metrics.icir_value = al.icir_value
        metrics.ic_n_days = al.ic_n_days
        metrics.engine = al.engine
    return metrics


def validate_factor(
    conn: sqlite3.Connection,
    track: TrackFactorConfig,
    factor: str,
    *,
    cfg: FactorValidationConfig,
    as_of: str,
) -> tuple[FactorValidationResult, dict[int, list[FactorDaySlice]]]:
    horizons: list[HorizonMetrics] = []
    slices_by_horizon: dict[int, list[FactorDaySlice]] = {}
    max_days = 0
    window_start: str | None = None
    window_end: str | None = None

    for h in cfg.forward_horizons_days:
        slices = build_factor_slices(
            conn,
            track,
            factor,
            as_of=as_of,
            lookback=cfg.lookback_trading_days,
            horizon_days=h,
            min_names=cfg.min_names_per_day,
        )
        slices_by_horizon[h] = slices
        if slices:
            dates = [sl.as_of_date for sl in slices]
            window_start = min(dates) if window_start is None else min(window_start, min(dates))
            window_end = max(dates) if window_end is None else max(window_end, max(dates))
            max_days = max(max_days, len(slices))
        horizons.append(
            compute_horizon_metrics(
                conn,
                slices,
                horizon_days=h,
                quantile_buckets=cfg.quantile_buckets,
                ic_decay_cfg=cfg.ic_decay,
            )
        )

    if max_days == 0:
        return (
            FactorValidationResult(
                track_id=track.track_id,
                track_title=track.title,
                factor=factor,
                status="pending",
                horizons=horizons,
                message="樣本不足（請先跑 score engine / VCP screen / behavior panel）",
            ),
            slices_by_horizon,
        )

    return (
        FactorValidationResult(
            track_id=track.track_id,
            track_title=track.title,
            factor=factor,
            status="ok",
            horizons=horizons,
            n_signal_days=max_days,
            window_start=window_start,
            window_end=window_end,
        ),
        slices_by_horizon,
    )


def run_factor_validation(
    conn: sqlite3.Connection,
    *,
    cfg: FactorValidationConfig | None = None,
    as_of: str | None = None,
) -> tuple[list[FactorValidationResult], dict[tuple[str, str], dict[int, list[FactorDaySlice]]]]:
    cfg = cfg or load_factor_validation_config()
    ref = as_of or date.today().isoformat()
    results: list[FactorValidationResult] = []
    slice_map: dict[tuple[str, str], dict[int, list[FactorDaySlice]]] = {}
    for track in cfg.tracks:
        for factor in track.factors:
            result, slices_by_h = validate_factor(
                conn, track, factor, cfg=cfg, as_of=ref
            )
            results.append(result)
            slice_map[(track.track_id, factor)] = slices_by_h
    return results, slice_map


def _fmt(v: float | None, *, digits: int = 4) -> str:
    if v is None:
        return "—"
    return f"{v:.{digits}f}"


def format_factor_report_md(result: FactorValidationResult, *, cfg: FactorValidationConfig) -> str:
    lines = [
        f"# Factor validation · `{result.track_id}` · `{result.factor}`",
        "",
        f"> Track: {result.track_title} · status: **{result.status}**",
        "",
    ]
    if result.message:
        lines.extend([result.message, ""])
    if result.window_start and result.window_end:
        lines.append(
            f"Window: `{result.window_start}` → `{result.window_end}` · "
            f"signal-days (max horizon): {result.n_signal_days}"
        )
        lines.append("")
    lines.extend(
        [
            "## Horizons (`get_clean_factor_and_forward_returns` semantics)",
            "",
            "| Horizon | Rank IC mean | ICIR | IC days | Q spread % | Monotonicity | Engine |",
            "|---------|--------------|------|---------|------------|--------------|--------|",
        ]
    )
    for h in result.horizons:
        lines.append(
            f"| T+{h.horizon_days} | {_fmt(h.ic_mean)} | {_fmt(h.icir_value)} | "
            f"{h.ic_n_days} | {_fmt(h.quantile_spread_pct, digits=2)} | "
            f"{h.monotonicity} | `{h.engine}` |"
        )
    primary_h = cfg.tearsheet.primary_horizon_days
    ph = next((h for h in result.horizons if h.horizon_days == primary_h), None)
    if ph and ph.ic_decay and ph.ic_decay.verdict != "樣本不足":
        d = ph.ic_decay
        lines.extend(
            [
                "",
                f"## IC decay · T+{primary_h} (`train_pct={cfg.ic_decay.train_pct}`)",
                "",
                "| Split | IC mean | n days |",
                "|-------|---------|--------|",
                f"| train (in-sample) | {_fmt(d.train_ic_mean)} | {d.train_n_days} |",
                f"| valid (out-of-sample) | {_fmt(d.valid_ic_mean)} | {d.valid_n_days} |",
                "",
                f"**Δ valid−train** = {_fmt(d.decay_delta)} · "
                f"ratio = {_fmt(d.decay_ratio)} · "
                f"**{_ic_decay_verdict_label(d.verdict)}**"
                + (f" · split @ `{d.split_date}`" if d.split_date else ""),
            ]
        )
    ts_base = tear_sheet_basename(result.track_id, result.factor, primary_h)
    lines.extend(
        [
            "",
            "## Tear sheet (`create_full_tear_sheet`)",
            "",
            f"- HTML: [`tearsheets/{ts_base}.html`](tearsheets/{ts_base}.html)",
            f"- PNG (alphalens): `tearsheets/{ts_base}.png` (when installed)",
        ]
    )
    for h in result.horizons:
        if h.quantile_means_pct:
            qcols = " · ".join(
                f"Q{i+1}={v:.2f}%" for i, v in enumerate(h.quantile_means_pct)
            )
            lines.extend(["", f"### T+{h.horizon_days} quantile means", "", qcols])
    lines.extend(
        [
            "",
            "## Notes",
            "",
            f"- Config `{cfg.version}` · lookback {cfg.lookback_trading_days}d · "
            f"min names/day {cfg.min_names_per_day}",
            "- Forward return: close-to-close on FinMind `stock_daily_bars` (trading-day calendar).",
            "- Rank IC: Spearman(factor, forward return); ICIR = mean(IC)/stdev(IC).",
            "",
        ]
    )
    return "\n".join(lines)


def format_summary_md(
    results: list[FactorValidationResult],
    *,
    cfg: FactorValidationConfig,
    generated_at: str,
) -> str:
    lines = [
        f"# Factor Validation Summary · {generated_at}",
        "",
        f"> Config `{cfg.version}` · alphalens-style tear sheet rollup · "
        "[docs/terminology.md](../docs/terminology.md)",
        "",
        "| Track | Factor | Status | T+1 IC | T+1 ICIR | IC decay | T+5 spread |",
        "|-------|--------|--------|--------|----------|----------|------------|",
    ]
    for r in results:
        h1 = next((h for h in r.horizons if h.horizon_days == 1), None)
        h5 = next((h for h in r.horizons if h.horizon_days == 5), None)
        decay_cell = "—"
        if h1 and h1.ic_decay and h1.ic_decay.verdict != "樣本不足":
            decay_cell = _ic_decay_verdict_label(h1.ic_decay.verdict)
        lines.append(
            f"| {r.track_title} | `{r.factor}` | {r.status} | "
            f"{_fmt(h1.ic_mean if h1 else None)} | "
            f"{_fmt(h1.icir_value if h1 else None)} | "
            f"{decay_cell} | "
            f"{_fmt(h5.quantile_spread_pct if h5 else None, digits=2)} |"
        )
    lines.extend(["", "## Tear sheets", "", "Primary horizon HTML: `tearsheets/{track}_{factor}_T1.html`", ""])
    lines.extend(["", "## Per-track reports", ""])
    seen: set[str] = set()
    for r in results:
        if r.track_id in seen:
            continue
        seen.add(r.track_id)
        lines.append(f"- [`{r.track_id}.md`]({r.track_id}.md)")
    lines.append("")
    return "\n".join(lines)


def write_factor_validation_reports(
    conn: sqlite3.Connection,
    *,
    cfg: FactorValidationConfig | None = None,
    as_of: str | None = None,
    reports_dir: Path = REPORTS_DIR,
) -> Path:
    cfg = cfg or load_factor_validation_config()
    ref = as_of or date.today().isoformat()
    results, slice_map = run_factor_validation(conn, cfg=cfg, as_of=ref)
    reports_dir.mkdir(parents=True, exist_ok=True)
    tearsheets_dir = reports_dir / "tearsheets"

    by_track: dict[str, list[str]] = {}
    for r in results:
        by_track.setdefault(r.track_id, []).append(format_factor_report_md(r, cfg=cfg))

        if r.status != "ok":
            continue
        primary_h = cfg.tearsheet.primary_horizon_days
        horizon = next((h for h in r.horizons if h.horizon_days == primary_h), None)
        slices = slice_map.get((r.track_id, r.factor), {}).get(primary_h, [])
        if horizon is None:
            continue

        base = tear_sheet_basename(r.track_id, r.factor, primary_h)
        if cfg.tearsheet.write_html:
            write_native_tear_sheet_html(
                tearsheets_dir / f"{base}.html",
                track_id=r.track_id,
                track_title=r.track_title,
                factor=r.factor,
                horizon=horizon,
                generated_at=ref,
                cfg=cfg,
            )
        if cfg.tearsheet.alphalens_png and slices:
            write_alphalens_tear_sheet_png(
                tearsheets_dir / f"{base}.png",
                conn,
                slices,
                primary_h,
            )

    for track_id, parts in by_track.items():
        path = reports_dir / f"{track_id}.md"
        path.write_text("\n\n---\n\n".join(parts), encoding="utf-8")

    summary = format_summary_md(results, cfg=cfg, generated_at=ref)
    stamp = ref.replace("-", "")
    dated = reports_dir / f"{stamp}_summary.md"
    latest = reports_dir / "summary.md"
    dated.write_text(summary, encoding="utf-8")
    latest.write_text(summary, encoding="utf-8")
    return latest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Alphalens-style factor validation")
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--as-of", default=None)
    parser.add_argument("--write-reports", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    conn = connect(args.db) if args.db else connect()
    try:
        cfg = load_factor_validation_config(args.config)
        if args.write_reports:
            path = write_factor_validation_reports(
                conn, cfg=cfg, as_of=args.as_of
            )
            if not args.quiet:
                print(path.relative_to(PROJECT_ROOT))
            return 0
        results, _ = run_factor_validation(conn, cfg=cfg, as_of=args.as_of)
        if not args.quiet:
            print(format_summary_md(results, cfg=cfg, generated_at=args.as_of or date.today().isoformat()))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
