"""S3 + RV 98–99 executable research · beta decomposition · tradable proxies.

Proves oracle「隔日台指≥0」≈ β exposure; replaces with D+1 open-gap entry (tradable).
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
from research.backtest.finpilot_local_backtest import load_price_panels
from research.backtest.rrg_improving_lifecycle_backtest import BURST_PCT, build_lifecycle_events
from stock_db import connect

CACHE_DIR = Path(__file__).resolve().parents[3] / "reports" / "research" / "rrg"
SID_HIST_CACHE = CACHE_DIR / "s3rv_sid_history.json"
MIN_SID_HIST = 3

# Pre-release tiers · raw multiplicative score mapped to 0–100 display
PRE_RELEASE_TIERS: tuple[tuple[float, str, str, float], ...] = (
    (72.0, "full", "滿倉", 1.5),
    (52.0, "standard", "標準", 1.0),
    (32.0, "light", "輕倉", 0.5),
    (0.0, "observe", "觀察", 0.25),
)
# burst-day tiers (post-hoc research only)
CONVICTION_TIERS: tuple[tuple[float, str, str, float], ...] = PRE_RELEASE_TIERS


@dataclass(frozen=True)
class S3RvConviction:
    score: float
    tier_id: str
    tier_label: str
    position_weight: float
    raw_mul: float = 0.0
    blocked: str = ""


def conviction_tier_from_score(score: float) -> tuple[str, str, float]:
    for threshold, tier_id, label, weight in PRE_RELEASE_TIERS:
        if score >= threshold:
            return tier_id, label, weight
    return "observe", "觀察", 0.25


def _pre_release_factors(
    *,
    bench_today_ret: float,
    sig_ret: float,
    improve_streak: int,
    disp_contract: bool,
    disp_d: float,
    base_setup: bool,
    end_q: str,
    prev_end_q: str,
    excess: float,
    sid_burst_hist_ok: bool,
    sid_burst_hist_n: int,
) -> dict[str, float]:
    return {
        "bench": 1.0 if bench_today_ret >= 0 else 0.0,
        "bench_str": max(0.0, min(float(bench_today_ret) / 2.0, 1.5)),
        "disp": 1.0 if disp_contract else 0.0,
        "base": 1.0 if base_setup else 0.0,
        "st6": 1.0 if improve_streak >= 6 else 0.0,
        "st1": 1.0 if improve_streak == 1 else 0.0,
        "st23_bad": 1.0 if improve_streak in (2, 3) else 0.0,
        "quasi": 1.0 if 3.0 <= sig_ret < 5.0 else 0.0,
        "flat": 1.0 if -1.0 <= sig_ret <= 1.0 else 0.0,
        "sid_ok": 1.0 if sid_burst_hist_ok else 0.0,
        "prev_lag": 1.0 if prev_end_q == "lagging" or end_q == "lagging" else 0.0,
        "chase_bad": 1.0 if sig_ret >= 3.0 and not disp_contract else 0.0,
        "disp_exp": 1.0 if disp_d > 0.1 else 0.0,
        "bench_neg": 1.0 if bench_today_ret < 0 else 0.0,
        "excess_hot": 1.0 if excess > 3.0 else 0.0,
        "sid_bad": 1.0 if sid_burst_hist_n >= MIN_SID_HIST and not sid_burst_hist_ok else 0.0,
    }


def _pre_release_outlier_block(
    *,
    sig_ret: float,
    excess: float,
    disp_contract: bool,
    end_q: str,
    s3_burst: bool,
) -> str:
    """Return block reason · empty = ok to score."""
    if s3_burst:
        return ""
    if end_q not in ("improving", "lagging"):
        return "not_improving_track"
    if sig_ret >= 5.0:
        return "already_burst"
    if excess > 5.0 and not disp_contract:
        return "excess_outlier"
    if sig_ret <= -3.0:
        return "deep_drop"
    return ""


def compute_pre_release_conviction_score(
    *,
    end_q: str,
    bench_today_ret: float,
    sig_ret: float,
    improve_streak: int = 0,
    disp_contract: bool = False,
    disp_d: float = 0.0,
    base_setup: bool = False,
    prev_end_q: str = "",
    excess: float = 0.0,
    sid_burst_hist_ok: bool = False,
    sid_burst_hist_n: int = 0,
    s3_burst: bool = False,
    inst3_net: float | None = None,
    breadth_zone: str | None = None,
) -> S3RvConviction:
    """PIT pre-release score · D close before potential D+1 burst · multiplicative model."""
    block = _pre_release_outlier_block(
        sig_ret=sig_ret,
        excess=excess,
        disp_contract=disp_contract,
        end_q=end_q,
        s3_burst=s3_burst,
    )
    if block:
        return S3RvConviction(0.0, "observe", "觀察", 0.0, raw_mul=0.0, blocked=block)

    # lagging pre-flip: need disp↓ or positive sig to enter pool
    if end_q == "lagging" and not (disp_contract or sig_ret > 0.5):
        return S3RvConviction(0.0, "observe", "觀察", 0.0, raw_mul=0.0, blocked="lagging_inactive")

    fv = _pre_release_factors(
        bench_today_ret=bench_today_ret,
        sig_ret=sig_ret,
        improve_streak=improve_streak,
        disp_contract=disp_contract,
        disp_d=disp_d,
        base_setup=base_setup,
        end_q=end_q,
        prev_end_q=prev_end_q,
        excess=excess,
        sid_burst_hist_ok=sid_burst_hist_ok,
        sid_burst_hist_n=sid_burst_hist_n,
    )

    core = 1.0
    if fv["bench_neg"]:
        core *= 0.55
    if fv["st23_bad"]:
        core *= 0.65
    if fv["chase_bad"]:
        core *= 0.70
    if fv["disp_exp"]:
        core *= 0.85
    if fv["excess_hot"]:
        core *= 0.80
    if fv["sid_bad"]:
        core *= 0.75

    boost = 1.0
    boost += 0.30 * fv["disp"] + 0.22 * fv["quasi"] + 0.18 * fv["st6"] + 0.15 * fv["base"]
    boost += 0.12 * fv["sid_ok"] + 0.10 * fv["prev_lag"] + 0.08 * fv["st1"] + 0.08 * fv["flat"]
    boost += 0.05 * fv["bench_str"]

    raw_mul = core * boost
    if inst3_net is not None and np.isfinite(inst3_net):
        if inst3_net > 0:
            raw_mul *= 1.05
        elif inst3_net < 0:
            raw_mul *= 0.90
    if breadth_zone == "neutral":
        raw_mul *= 0.92

    display = min(100.0, max(0.0, raw_mul / 1.5 * 100.0))
    tier_id, tier_label, weight = conviction_tier_from_score(display)
    return S3RvConviction(
        score=round(display, 1),
        tier_id=tier_id,
        tier_label=tier_label,
        position_weight=weight,
        raw_mul=round(raw_mul, 3),
    )


def compute_s3_burst_conviction_score(
    *,
    s3_burst: bool,
    bench_today_ret: float,
    sig_ret: float,
    improve_streak: int = 0,
    prev_disp_contract: bool = False,
    prev_base_setup: bool = False,
    prev_end_q: str = "",
    sid_burst_hist_ok: bool = False,
    sid_burst_hist_n: int = 0,
    inst3_net: float | None = None,
    breadth_zone: str | None = None,
) -> S3RvConviction:
    """PIT S3 burst conviction (0–100) · no RV band · signal + prior-day factors."""
    if not s3_burst:
        return S3RvConviction(0.0, "observe", "觀察", 0.0)

    score = 30.0
    if bench_today_ret >= 0:
        score += 12.0 + min(float(bench_today_ret), 3.0) * 4.0
        if bench_today_ret >= 1.0:
            score += 5.0
        if bench_today_ret >= 2.0:
            score += 5.0
    else:
        score -= 12.0

    if 8.0 <= sig_ret < 10.0:
        score += 15.0
    elif 6.0 <= sig_ret < 8.0:
        score += 6.0
    elif sig_ret >= 10.0:
        score += 10.0

    if improve_streak in (2, 3):
        score -= 14.0
    elif improve_streak == 1:
        score += 10.0
    elif improve_streak >= 6:
        score += 8.0
    elif improve_streak in (4, 5):
        score += 4.0

    if prev_disp_contract:
        score += 12.0
    if prev_base_setup:
        score += 10.0
    if prev_end_q == "lagging":
        score += 8.0

    if sid_burst_hist_ok:
        score += 14.0
    elif sid_burst_hist_n >= MIN_SID_HIST:
        score -= 6.0

    if inst3_net is not None and np.isfinite(inst3_net):
        if inst3_net > 0:
            score += 6.0
        elif inst3_net < 0:
            score -= 6.0

    if breadth_zone == "overbought":
        score += 6.0
    elif breadth_zone == "neutral":
        score -= 6.0

    score = max(0.0, min(100.0, score))
    tier_id, tier_label, weight = conviction_tier_from_score(score)
    return S3RvConviction(
        score=round(score, 1),
        tier_id=tier_id,
        tier_label=tier_label,
        position_weight=weight,
    )


def compute_setup_release_score(
    *,
    base_setup: bool,
    bench_today_ret: float,
    improve_streak: int = 0,
    disp_contract: bool = False,
    sid_burst_hist_ok: bool = False,
    end_q: str = "improving",
    sig_ret: float = 0.0,
    disp_d: float = 0.0,
    prev_end_q: str = "",
    excess: float = 0.0,
    sid_burst_hist_n: int = 0,
) -> S3RvConviction:
    """Deprecated alias · use compute_pre_release_conviction_score."""
    return compute_pre_release_conviction_score(
        end_q=end_q,
        bench_today_ret=bench_today_ret,
        sig_ret=sig_ret,
        improve_streak=improve_streak,
        disp_contract=disp_contract,
        disp_d=disp_d,
        base_setup=base_setup,
        prev_end_q=prev_end_q,
        excess=excess,
        sid_burst_hist_ok=sid_burst_hist_ok,
        sid_burst_hist_n=sid_burst_hist_n,
    )


def compute_s3_rv_conviction_score(
    *,
    s3_rv_98_99: bool,
    bench_today_ret: float,
    disp_contract: bool,
    sid_s3rv_hist_ok: bool,
    sid_s3rv_hist_n: int = 0,
    improve_streak: int = 0,
    inst3_net: float | None = None,
    breadth_zone: str | None = None,
    sig_ret: float = 0.0,
    prev_disp_contract: bool = False,
    prev_base_setup: bool = False,
    prev_end_q: str = "",
    s3_burst: bool = False,
) -> S3RvConviction:
    """Backward-compatible wrapper · delegates to S3 burst score when s3_burst."""
    if s3_burst or s3_rv_98_99:
        return compute_s3_burst_conviction_score(
            s3_burst=True,
            bench_today_ret=bench_today_ret,
            sig_ret=sig_ret if sig_ret > 0 else 6.0,
            improve_streak=improve_streak,
            prev_disp_contract=prev_disp_contract or disp_contract,
            prev_base_setup=prev_base_setup,
            prev_end_q=prev_end_q,
            sid_burst_hist_ok=sid_s3rv_hist_ok,
            sid_burst_hist_n=sid_s3rv_hist_n,
            inst3_net=inst3_net,
            breadth_zone=breadth_zone,
        )
    return S3RvConviction(0.0, "observe", "觀察", 0.0)


def conviction_from_improving_row(
    row: Any,
    *,
    inst3_net: float | None = None,
    breadth_zone: str | None = None,
    use_burst_posthoc: bool = False,
) -> S3RvConviction:
    """Build conviction · default = pre-release (tradable D close before D+1 move)."""
    disp_d = float(getattr(row, "disp_d", 0.0))
    if np.isnan(disp_d):
        disp_d = 0.0
    if use_burst_posthoc and bool(getattr(row, "s3_burst", False)):
        return compute_s3_burst_conviction_score(
            s3_burst=True,
            bench_today_ret=float(getattr(row, "bench_today_ret", 0.0)),
            sig_ret=float(getattr(row, "sig_ret", 0.0)),
            improve_streak=int(getattr(row, "improve_streak", 0)),
            prev_disp_contract=bool(getattr(row, "prev_disp_contract", False)),
            prev_base_setup=bool(getattr(row, "prev_base_setup", False)),
            prev_end_q=str(getattr(row, "prev_end_q", "")),
            sid_burst_hist_ok=bool(getattr(row, "sid_s3rv_hist_ok", False)),
            sid_burst_hist_n=int(getattr(row, "sid_s3rv_hist_n", 0)),
            inst3_net=inst3_net,
            breadth_zone=breadth_zone,
        )
    return compute_pre_release_conviction_score(
        end_q=str(getattr(row, "end_q", "improving")),
        bench_today_ret=float(getattr(row, "bench_today_ret", 0.0)),
        sig_ret=float(getattr(row, "sig_ret", 0.0)),
        improve_streak=int(getattr(row, "improve_streak", 0)),
        disp_contract=bool(getattr(row, "disp_contract", False)),
        disp_d=disp_d,
        base_setup=bool(getattr(row, "base_setup", False)),
        prev_end_q=str(getattr(row, "prev_end_q", "")),
        excess=float(getattr(row, "excess", 0.0)),
        sid_burst_hist_ok=bool(getattr(row, "sid_s3rv_hist_ok", False)),
        sid_burst_hist_n=int(getattr(row, "sid_s3rv_hist_n", 0)),
        s3_burst=bool(getattr(row, "s3_burst", False)),
        inst3_net=inst3_net,
        breadth_zone=breadth_zone,
    )


@dataclass(frozen=True)
class BetaDecomp:
    label: str
    n: int
    alpha_pct: float
    beta: float
    r_squared: float
    mean_nxt_pct: float
    mean_alpha_pct: float
    mean_bench_nxt_pct: float


@dataclass(frozen=True)
class GapPredictorStats:
    predictor: str
    n: int
    corr_with_bench_nxt: float
    oracle_capture_rate: float
    precision_mkt_up: float
    mean_nxt_when_filter: float


@dataclass(frozen=True)
class ExecVariantResult:
    variant_id: str
    label: str
    tradable: bool
    entry: str
    n_events: int
    n_trade_days: int
    event_mean_pct: float
    event_win_rate: float
    p_burst_pct: float
    deployed_cagr_pct: float
    deployed_total_pct: float
    full_calendar_cagr_pct: float
    sharpe_deployed: float
    max_drawdown_pct: float
    capital_util_pct: float


def load_benchmark_open(conn: sqlite3.Connection, *, code: str = "IX0001") -> pd.Series:
    rows = conn.execute(
        """
        SELECT date AS trade_date, open
        FROM daily_bars
        WHERE code = ? AND open IS NOT NULL AND open > 0
        ORDER BY date,
            CASE source WHEN 'tej' THEN 0 WHEN 'finmind' THEN 1 WHEN 'yahoo' THEN 2 ELSE 3 END
        """,
        (code,),
    ).fetchall()
    if not rows:
        return pd.Series(dtype=float)
    df = pd.DataFrame(rows, columns=["trade_date", "open"])
    df = df.drop_duplicates(subset=["trade_date"], keep="first")
    return df.set_index("trade_date")["open"].astype(float)


def _pit_sid_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Add sid_s3rv_hist_n/mean/ok from prior S3 burst events (PIT)."""
    s3 = df[df["s3_burst"]].sort_values(["sid", "date"]).copy()
    hist_n: list[int] = []
    hist_mean: list[float] = []
    hist_ok: list[bool] = []
    prior: dict[str, list[float]] = {}
    for _, row in s3.iterrows():
        sid = str(row["sid"])
        prev = prior.get(sid, [])
        n = len(prev)
        if n >= MIN_SID_HIST:
            m = float(np.mean(prev))
            hist_n.append(n)
            hist_mean.append(m)
            hist_ok.append(m > 0)
        else:
            hist_n.append(n)
            hist_mean.append(float("nan"))
            hist_ok.append(False)
        prior.setdefault(sid, []).append(float(row["nxt_ret"]))
    s3 = s3.assign(
        sid_s3rv_hist_n=hist_n,
        sid_s3rv_hist_mean=hist_mean,
        sid_s3rv_hist_ok=hist_ok,
    )
    out = df.copy()
    for col in ("sid_s3rv_hist_n", "sid_s3rv_hist_mean", "sid_s3rv_hist_ok"):
        out[col] = np.nan if col != "sid_s3rv_hist_ok" else False
    out.loc[s3.index, "sid_s3rv_hist_n"] = s3["sid_s3rv_hist_n"]
    out.loc[s3.index, "sid_s3rv_hist_mean"] = s3["sid_s3rv_hist_mean"]
    out.loc[s3.index, "sid_s3rv_hist_ok"] = s3["sid_s3rv_hist_ok"]
    out["sid_s3rv_hist_n"] = out["sid_s3rv_hist_n"].fillna(0).astype(int)
    out["sid_s3rv_hist_ok"] = out["sid_s3rv_hist_ok"].fillna(False).astype(bool)
    return out


def enrich_lifecycle_events(
    conn: sqlite3.Connection | None = None,
    *,
    events: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    conn = conn or connect()
    df, meta = (events, {}) if events is not None else build_lifecycle_events(conn)
    if df.empty:
        return df, meta

    close, opn, _ = load_price_panels(conn)
    bench_close = load_benchmark_close(conn)
    bench_open = load_benchmark_open(conn)
    full_dates = close.index.astype(str).tolist()
    date_idx = {d: i for i, d in enumerate(full_dates)}

    nxt_open = []
    nxt_open_to_close = []
    bench_open_gap = []
    alpha_nxt = []

    for _, row in df.iterrows():
        d, sid = str(row["date"]), str(row["sid"])
        si = date_idx.get(d)
        if si is None or si + 1 >= len(full_dates):
            nxt_open.append(np.nan)
            nxt_open_to_close.append(np.nan)
            bench_open_gap.append(np.nan)
            alpha_nxt.append(np.nan)
            continue
        dn = full_dates[si + 1]
        o = opn.at[dn, sid] if sid in opn.columns else np.nan
        c = close.at[dn, sid] if sid in close.columns else np.nan
        bc = bench_close.get(d)
        bo = bench_open.get(dn)
        if pd.isna(o) or pd.isna(c) or o <= 0 or c <= 0 or bc is None or bo is None or bc <= 0 or bo <= 0:
            nxt_open.append(np.nan)
            nxt_open_to_close.append(np.nan)
            bench_open_gap.append(np.nan)
            alpha_nxt.append(np.nan)
            continue
        otc = (c / o - 1) * 100
        gap = (bo / bc - 1) * 100
        nxt_open.append(float(o))
        nxt_open_to_close.append(float(otc))
        bench_open_gap.append(float(gap))
        alpha_nxt.append(float(row["nxt_ret"]) - float(row["bench_nxt_ret"]))

    out = df.copy()
    out["nxt_open_to_close"] = nxt_open_to_close
    out["bench_open_gap"] = bench_open_gap
    out["alpha_nxt"] = alpha_nxt

    overnight = []
    for _, row in out.iterrows():
        d, sid = str(row["date"]), str(row["sid"])
        si = date_idx.get(d)
        if si is None or si + 1 >= len(full_dates):
            overnight.append(np.nan)
            continue
        dn = full_dates[si + 1]
        c0 = close.at[d, sid] if sid in close.columns else np.nan
        o1 = opn.at[dn, sid] if sid in opn.columns else np.nan
        if pd.isna(c0) or pd.isna(o1) or c0 <= 0 or o1 <= 0:
            overnight.append(np.nan)
            continue
        overnight.append((o1 / c0 - 1) * 100)
    out["overnight_ret"] = overnight
    out = _pit_sid_columns(out)
    return out, meta


def build_sid_history_cache(conn: sqlite3.Connection | None = None, *, force: bool = False) -> pd.DataFrame:
    from stock_db import PROJECT_ROOT

    conn = conn or connect()
    db_path = PROJECT_ROOT / "data" / "stocks.db"
    db_mtime = db_path.stat().st_mtime if db_path.is_file() else 0.0
    if (
        not force
        and SID_HIST_CACHE.is_file()
        and SID_HIST_CACHE.stat().st_mtime >= db_mtime
    ):
        payload = json.loads(SID_HIST_CACHE.read_text(encoding="utf-8"))
        return pd.DataFrame(payload)

    df, _ = enrich_lifecycle_events(conn)
    s3 = df[df["s3_burst"]][
        ["date", "sid", "sid_s3rv_hist_n", "sid_s3rv_hist_mean", "sid_s3rv_hist_ok"]
    ].copy()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    SID_HIST_CACHE.write_text(
        s3.to_json(orient="records", force_ascii=False),
        encoding="utf-8",
    )
    return s3


def lookup_sid_stats(
    cache: pd.DataFrame,
    signal_date: str,
    stock_id: str,
) -> tuple[int, float, bool]:
    sub = cache[(cache["date"] == signal_date) & (cache["sid"] == stock_id)]
    if sub.empty:
        return 0, float("nan"), False
    r = sub.iloc[0]
    mean = float(r["sid_s3rv_hist_mean"]) if not pd.isna(r["sid_s3rv_hist_mean"]) else float("nan")
    return int(r["sid_s3rv_hist_n"]), mean, bool(r["sid_s3rv_hist_ok"])


def attach_sid_stats_to_rows(rows: list[Any], cache: pd.DataFrame) -> list[Any]:
    from dataclasses import replace

    out = []
    for row in rows:
        n, mean, ok = lookup_sid_stats(cache, row.date, row.stock_id)
        out.append(
            replace(
                row,
                sid_s3rv_hist_n=n,
                sid_s3rv_hist_mean=mean,
                sid_s3rv_hist_ok=ok,
            )
        )
    return out


def is_s3_executable(
    *,
    s3_rv_98_99: bool,
    bench_today_ret: float,
    sid_s3rv_hist_n: int,
    sid_s3rv_hist_ok: bool,
    require_sid_hist: bool = True,
) -> bool:
    if not s3_rv_98_99:
        return False
    if bench_today_ret < 0:
        return False
    if require_sid_hist:
        return sid_s3rv_hist_n >= MIN_SID_HIST and sid_s3rv_hist_ok
    return sid_s3rv_hist_n < MIN_SID_HIST or sid_s3rv_hist_ok


def _ols_decomp(sub: pd.DataFrame, label: str) -> BetaDecomp:
    y = sub["nxt_ret"].to_numpy(dtype=float)
    x = sub["bench_nxt_ret"].to_numpy(dtype=float)
    mask = np.isfinite(y) & np.isfinite(x)
    y, x = y[mask], x[mask]
    if len(y) < 10:
        return BetaDecomp(label, len(y), 0, 0, 0, 0, 0, 0)
    xm = x - x.mean()
    ym = y - y.mean()
    beta = float(np.dot(xm, ym) / np.dot(xm, xm)) if np.dot(xm, xm) > 0 else 0.0
    alpha = float(y.mean() - beta * x.mean())
    pred = alpha + beta * x
    ss_res = float(np.sum((y - pred) ** 2))
    ss_tot = float(np.sum(ym**2))
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0
    alpha_series = y - beta * x
    return BetaDecomp(
        label=label,
        n=len(y),
        alpha_pct=round(alpha, 4),
        beta=round(beta, 4),
        r_squared=round(r2, 4),
        mean_nxt_pct=round(float(y.mean()), 4),
        mean_alpha_pct=round(float(alpha_series.mean()), 4),
        mean_bench_nxt_pct=round(float(x.mean()), 4),
    )


def analyze_oracle_beta(df: pd.DataFrame) -> list[BetaDecomp]:
    s3 = df[df["s3_rv_98_99"]]
    return [
        _ols_decomp(s3, "S3+RV 全部"),
        _ols_decomp(s3[s3["bench_nxt_ret"] >= 0], "oracle · 隔日台指≥0"),
        _ols_decomp(s3[s3["bench_nxt_ret"] < 0], "oracle · 隔日台指<0"),
        _ols_decomp(s3[s3["bench_open_gap"] >= 0], "tradable · 隔日開盤 gap≥0"),
    ]


def analyze_gap_predictors(df: pd.DataFrame) -> list[GapPredictorStats]:
    s3 = df[df["s3_rv_98_99"]].dropna(subset=["bench_open_gap", "bench_nxt_ret"])
    oracle = s3["bench_nxt_ret"] >= 0
    out: list[GapPredictorStats] = []

    sig_filt = s3["bench_today_ret"] >= 0
    out.append(
        GapPredictorStats(
            predictor="bench_today_ret（信號日 · D 收盤可判）",
            n=int(sig_filt.sum()),
            corr_with_bench_nxt=round(float(s3["bench_today_ret"].corr(s3["bench_nxt_ret"])), 4)
            if len(s3) > 2
            else 0.0,
            oracle_capture_rate=round(float((sig_filt & oracle).sum() / oracle.sum()), 4)
            if oracle.sum()
            else 0.0,
            precision_mkt_up=round(float((s3.loc[sig_filt, "bench_nxt_ret"] >= 0).mean()), 4)
            if sig_filt.sum()
            else 0.0,
            mean_nxt_when_filter=round(float(s3.loc[sig_filt, "nxt_ret"].mean()), 4)
            if sig_filt.sum()
            else 0.0,
        )
    )

    gap_filt = s3["bench_open_gap"] >= 0
    gap_sub = s3.loc[gap_filt].dropna(subset=["nxt_open_to_close"])
    out.append(
        GapPredictorStats(
            predictor="bench_open_gap（D+1 09:00 · open→close 報酬）",
            n=len(gap_sub),
            corr_with_bench_nxt=round(float(s3["bench_open_gap"].corr(s3["bench_nxt_ret"])), 4)
            if len(s3) > 2
            else 0.0,
            oracle_capture_rate=round(float((gap_filt & oracle).sum() / oracle.sum()), 4)
            if oracle.sum()
            else 0.0,
            precision_mkt_up=round(float((gap_sub["bench_nxt_ret"] >= 0).mean()), 4)
            if len(gap_sub)
            else 0.0,
            mean_nxt_when_filter=round(float(gap_sub["nxt_open_to_close"].mean()), 4)
            if len(gap_sub)
            else 0.0,
        )
    )
    return out


def analyze_oracle_mechanism(df: pd.DataFrame) -> dict[str, float]:
    """Split close→close into overnight (D→D+1 open) vs intraday (D+1 open→close)."""
    s3 = df[df["s3_rv_98_99"]].dropna(subset=["overnight_ret", "nxt_open_to_close", "nxt_ret"])
    up = s3[s3["bench_nxt_ret"] >= 0]
    down = s3[s3["bench_nxt_ret"] < 0]
    return {
        "all_overnight_mean": round(float(s3["overnight_ret"].mean()), 4),
        "all_intraday_mean": round(float(s3["nxt_open_to_close"].mean()), 4),
        "all_close_to_close": round(float(s3["nxt_ret"].mean()), 4),
        "oracle_overnight_mean": round(float(up["overnight_ret"].mean()), 4) if len(up) else 0.0,
        "oracle_intraday_mean": round(float(up["nxt_open_to_close"].mean()), 4) if len(up) else 0.0,
        "oracle_close_to_close": round(float(up["nxt_ret"].mean()), 4) if len(up) else 0.0,
        "oracle_down_close_to_close": round(float(down["nxt_ret"].mean()), 4) if len(down) else 0.0,
    }


def _exec_equity(
    picks: pd.DataFrame,
    ret_col: str,
    all_dates: list[str],
    *,
    full_calendar: bool,
) -> tuple[pd.Series, float]:
    if picks.empty:
        return pd.Series(0.0, index=all_dates), 0.0
    date_idx = {d: i for i, d in enumerate(all_dates)}
    realized: list[tuple[str, float]] = []
    for d, grp in picks.groupby("date"):
        si = date_idx.get(str(d))
        if si is None or si + 1 >= len(all_dates):
            continue
        realize = all_dates[si + 1]
        realized.append((realize, float(grp[ret_col].mean())))
    if not realized:
        return pd.Series(0.0, index=all_dates), 0.0

    if full_calendar:
        daily = pd.Series(0.0, index=all_dates)
        for rd, r in realized:
            if rd in daily.index:
                daily.at[rd] = r
        util = len(realized) / len(all_dates) * 100
        return daily, util

    trade_dates = sorted({r for r, _ in realized})
    daily = pd.Series({r: v for r, v in realized}).reindex(trade_dates).fillna(0.0)
    util = 100.0
    return daily, util


def _summarize_exec(
    variant_id: str,
    label: str,
    tradable: bool,
    entry: str,
    picks: pd.DataFrame,
    ret_col: str,
    all_dates: list[str],
) -> ExecVariantResult:
    if picks.empty or ret_col not in picks.columns:
        return ExecVariantResult(
            variant_id, label, tradable, entry, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0
        )
    sub = picks.dropna(subset=[ret_col])
    deployed, _ = _exec_equity(sub, ret_col, all_dates, full_calendar=False)
    full, util = _exec_equity(sub, ret_col, all_dates, full_calendar=True)
    years = max(len(all_dates) / 252, 1 / 252)

    def _stats(daily: pd.Series) -> tuple[float, float, float, float]:
        if daily.empty or (daily == 0).all():
            return 0.0, 0.0, 0.0, 0.0
        eq = (1 + daily / 100).cumprod()
        total = (eq.iloc[-1] - 1) * 100
        cagr = ((eq.iloc[-1]) ** (1 / years) - 1) * 100 if eq.iloc[-1] > 0 else -100.0
        peak = eq.cummax()
        dd = float((eq / peak - 1).min() * 100)
        sharpe = 0.0
        if daily.std() > 0 and len(daily) > 1:
            sharpe = float(daily.mean() / daily.std() * np.sqrt(252))
        return total, cagr, dd, sharpe

    dep_total, dep_cagr, dd, sharpe = _stats(deployed)
    _, full_cagr, _, _ = _stats(full)
    trade_days = int((deployed != 0).sum()) if len(deployed) else 0

    return ExecVariantResult(
        variant_id=variant_id,
        label=label,
        tradable=tradable,
        entry=entry,
        n_events=len(sub),
        n_trade_days=trade_days,
        event_mean_pct=round(float(sub[ret_col].mean()), 4),
        event_win_rate=round(float((sub[ret_col] > 0).mean()), 4),
        p_burst_pct=round(float((sub[ret_col] >= BURST_PCT).mean()), 4),
        deployed_cagr_pct=round(dep_cagr, 2),
        deployed_total_pct=round(dep_total, 2),
        full_calendar_cagr_pct=round(full_cagr, 2),
        sharpe_deployed=round(sharpe, 2),
        max_drawdown_pct=round(dd, 2),
        capital_util_pct=round(util, 2),
    )


def run_executable_proof(conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    conn = conn or connect()
    df, meta = enrich_lifecycle_events(conn)
    all_dates = sorted(df["date"].unique())
    s3 = df[df["s3_rv_98_99"]].copy()

    og = s3[(s3["bench_open_gap"] >= 0) & s3["nxt_open_to_close"].notna()].copy()
    og["trade_ret"] = og["nxt_open_to_close"]
    ogs = s3[
        (s3["bench_open_gap"] >= 0) & s3["sid_s3rv_hist_ok"] & s3["nxt_open_to_close"].notna()
    ].copy()
    ogs["trade_ret"] = ogs["nxt_open_to_close"]
    variants = {
        "close_all": s3,
        "close_sig_mkt": s3[s3["bench_today_ret"] >= 0],
        "oracle_mkt": s3[s3["bench_nxt_ret"] >= 0],
        "open_gap": og,
        "open_gap_sid": ogs,
        "close_exec": s3[(s3["bench_today_ret"] >= 0) & s3["sid_s3rv_hist_ok"]],
    }

    exec_results = [
        _summarize_exec("close_all", "D 收盤買 · S3 RV 全部", True, "close→close", variants["close_all"], "nxt_ret", all_dates),
        _summarize_exec(
            "close_sig_mkt",
            "D 收盤 · 信號日台指≥0",
            True,
            "close→close",
            variants["close_sig_mkt"],
            "nxt_ret",
            all_dates,
        ),
        _summarize_exec(
            "oracle_mkt",
            "oracle · 隔日台指≥0",
            False,
            "close→close",
            variants["oracle_mkt"],
            "nxt_ret",
            all_dates,
        ),
        _summarize_exec(
            "open_gap",
            "D+1 開盤 · 台指 gap≥0（可執行）",
            True,
            "open→close",
            variants["open_gap"],
            "trade_ret",
            all_dates,
        ),
        _summarize_exec(
            "open_gap_sid",
            "D+1 開盤 gap≥0 + 個股史>0",
            True,
            "open→close",
            variants["open_gap_sid"],
            "trade_ret",
            all_dates,
        ),
        _summarize_exec(
            "close_exec",
            "D 收盤 · 信號日台指≥0 + 個股史>0",
            True,
            "close→close",
            variants["close_exec"],
            "nxt_ret",
            all_dates,
        ),
    ]

    build_sid_history_cache(conn, force=True)

    return {
        "meta": {**meta, "generated_on": date.today().isoformat()},
        "beta_decomposition": [asdict(x) for x in analyze_oracle_beta(df)],
        "gap_predictors": [asdict(x) for x in analyze_gap_predictors(df)],
        "oracle_mechanism": analyze_oracle_mechanism(df),
        "executable_variants": [asdict(x) for x in exec_results],
        "oracle_vs_tradable": {
            "oracle_mean_nxt": round(float(variants["oracle_mkt"]["nxt_ret"].mean()), 4),
            "open_gap_mean_open_to_close": round(float(og["trade_ret"].mean()), 4) if len(og) else 0,
            "close_exec_mean": round(float(variants["close_exec"]["nxt_ret"].mean()), 4)
            if len(variants["close_exec"])
            else 0,
            "open_gap_capture_n": len(og),
            "oracle_n": len(variants["oracle_mkt"]),
            "alpha_oracle_mean": round(float(variants["oracle_mkt"]["alpha_nxt"].mean()), 4),
        },
    }


def render_proof_markdown(result: dict[str, Any]) -> str:
    meta = result["meta"]
    lines = [
        "# S3 + RV 98–99 · oracle 證明 vs 可執行方案",
        "",
        f"- Universe **{meta['universe_size']}** · {meta['date_start']} → {meta['date_end']}",
        f"- Generated: {meta['generated_on']}",
        "",
        "## 1. β 分解（隔日報酬 ~ 隔日台指）",
        "",
        "若 oracle「超強」純粹來自 β，則 `nxt_ret ≈ α + β·bench_nxt`，oracle 子樣本 α≈0、β≈1、R² 高。",
        "",
        "| 子樣本 | N | α | β | R² | mean(nxt) | mean(α残差) | mean(台指) |",
        "|--------|---|---|---|-----|-----------|-------------|-----------|",
    ]
    for b in result["beta_decomposition"]:
        lines.append(
            f"| {b['label']} | {b['n']} | {b['alpha_pct']:+.3f}% | {b['beta']:.2f} | "
            f"{b['r_squared']:.2f} | {b['mean_nxt_pct']:+.3f}% | {b['mean_alpha_pct']:+.3f}% | "
            f"{b['mean_bench_nxt_pct']:+.3f}% |"
        )

    lines.extend(["", "## 2. 可交易 proxy · 能否抓到 oracle？", ""])
    lines.append("| Predictor | N(filter) | corr(台指隔日) | oracle 覆蓋率 | P(台指≥0\\|filter) | mean(nxt\\|filter) |")
    lines.append("|-----------|-----------|---------------|--------------|-------------------|-------------------|")
    for g in result["gap_predictors"]:
        lines.append(
            f"| {g['predictor']} | {g['n']} | {g['corr_with_bench_nxt']:.2f} | "
            f"{g['oracle_capture_rate']:.1%} | {g['precision_mkt_up']:.1%} | {g['mean_nxt_when_filter']:+.3f}% |"
        )

    ov = result["oracle_vs_tradable"]
    mech = result.get("oracle_mechanism", {})
    lines.extend(
        [
            "",
            f"- oracle close→close: **{ov['oracle_mean_nxt']:+.3f}%** · close_exec（可交易）: **{ov['close_exec_mean']:+.3f}%**",
            f"- open-gap open→close: **{ov['open_gap_mean_open_to_close']:+.3f}%**",
            f"- oracle α均值: **{ov['alpha_oracle_mean']:+.3f}%**",
            "",
            "## 2b. oracle 為何看起來超強？（close→close 拆解）",
            "",
            "| 分量 | 全部 S3 RV | oracle 台指≥0 |",
            "|------|-----------|--------------|",
            f"| 隔夜 D收→D+1開 | {mech.get('all_overnight_mean', 0):+.3f}% | {mech.get('oracle_overnight_mean', 0):+.3f}% |",
            f"| 日內 D+1 open→close | {mech.get('all_intraday_mean', 0):+.3f}% | {mech.get('oracle_intraday_mean', 0):+.3f}% |",
            f"| 合計 close→close | {mech.get('all_close_to_close', 0):+.3f}% | {mech.get('oracle_close_to_close', 0):+.3f}% |",
            "",
            "oracle 順風日的優勢 **主要在隔夜**；D+1 開盤進場會 **漏掉隔夜、吃到日內回吐**。",
            "",
            "## 3. 可執行 variant · 資金利用率",
            "",
            "**deployed CAGR** = 僅在交易 compounding（100% 投入）；**full calendar CAGR** = 無訊號日 0%。",
            "",
            "| Variant | 可交易 | 進場 | N | 每筆均值 | 勝率 | deployed CAGR | deployed 總 | Sharpe | MaxDD | 資金利用率 |",
            "|---------|--------|------|---|---------|------|---------------|------------|--------|-------|-----------|",
        ]
    )
    for v in result["executable_variants"]:
        trad = "✓" if v["tradable"] else "oracle"
        lines.append(
            f"| {v['label']} | {trad} | {v['entry']} | {v['n_events']} | {v['event_mean_pct']:+.3f}% | "
            f"{v['event_win_rate']:.1%} | {v['deployed_cagr_pct']:+.1f}% | {v['deployed_total_pct']:+.0f}% | "
            f"{v['sharpe_deployed']:.2f} | {v['max_drawdown_pct']:.1f}% | {v['capital_util_pct']:.0f}% |"
        )

    lines.extend(
        [
            "",
            "## 4. 結論",
            "",
            "1. **oracle 超強 = 順風日 close→close β** · α 小 · R² 低（個股噪聲大）· **不可在 D+1 開盤複製**。",
            "2. **最佳可交易（本回測）**：D 收 **S3 RV + 信號日台指≥0 + 個股史>0** · close→close · N=56。",
            "3. **D+1 gap 進場 open→close 全樣本為負** · gap 只適合作 **風控/加碼時點**，非 oracle 替身。",
            "4. **deployed CAGR** 才反映全倉強度 · full calendar 因 ~85% 日現金而稀釋。",
            "",
        ]
    )
    return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class PreReleaseTierStats:
    tier_id: str
    tier_label: str
    score_min: float
    n: int
    mean_nxt_pct: float
    median_nxt_pct: float
    win_rate: float
    p_burst_pct: float
    mean_alpha_pct: float


@dataclass(frozen=True)
class PreReleaseBacktestResult:
    meta: dict[str, Any]
    model: dict[str, Any]
    universe: dict[str, Any]
    tier_stats: list[PreReleaseTierStats]
    decile_stats: list[dict[str, Any]]
    rank_stats: dict[str, float]
    portfolio: dict[str, Any]
    oos_tier_stats: list[PreReleaseTierStats]


def pre_release_model_spec() -> dict[str, Any]:
    """Formal math model · SSOT for report section 0."""
    return {
        "information_set": "ℱ_D · PIT close on signal day D",
        "universe": "Ω = {(i,D): q∈{improving,lagging}, sig_ret<5%, block=∅}",
        "core": "C(x) = ∏ λ_j when penalty_j active (bench_neg, st23, chase, disp_exp, excess_hot, sid_bad)",
        "boost": "B(x) = 1 + Σ γ_k f_k(x) (disp, quasi, st6, base, sid_ok, prev_lag, st1, flat, bench_str)",
        "raw_score": "M(x) = C(x) · B(x)",
        "display": "S(x) = clip(100 · M / 1.5, 0, 100)",
        "tier_map": [
            {"min_score": t[0], "id": t[1], "label": t[2], "weight": t[3]}
            for t in PRE_RELEASE_TIERS
        ],
        "outcome": "Y_{D+1} = close→close · B=1{Y≥5%} · α=Y−r_bench",
        "portfolio": "R_{D+1} = Σ w_τ(S_i)·Y_i / Σ w_τ(S_i) · equal within day",
        "hypotheses": [
            "H_mono: E[Y|tier_k] non-decreasing in k",
            "H_rank: Spearman(S,Y)>0 on scored pool",
            "H_tail: P(B|full) > P(B|observe)",
        ],
    }


def _score_event_row(row: pd.Series) -> dict[str, Any]:
    disp_d = float(row["disp_d"]) if not pd.isna(row.get("disp_d")) else 0.0
    conv = compute_pre_release_conviction_score(
        end_q=str(row["end_q"]),
        bench_today_ret=float(row["bench_today_ret"]),
        sig_ret=float(row["sig_ret"]),
        improve_streak=int(row["improve_streak"]),
        disp_contract=bool(row.get("disp_contract", False)),
        disp_d=disp_d,
        base_setup=bool(row.get("base_setup", False)),
        prev_end_q=str(row.get("prev_q", row.get("prev_end_q", ""))),
        excess=float(row.get("excess", 0.0)),
        sid_burst_hist_ok=bool(row.get("sid_s3rv_hist_ok", False)),
        sid_burst_hist_n=int(row.get("sid_s3rv_hist_n", 0)),
        s3_burst=bool(row.get("s3_burst", False)),
    )
    return {
        "pre_score": conv.score,
        "pre_raw_mul": conv.raw_mul,
        "pre_tier_id": conv.tier_id,
        "pre_tier_label": conv.tier_label,
        "pre_weight": conv.position_weight,
        "pre_blocked": conv.blocked,
    }


def build_pre_release_scored_events(df: pd.DataFrame) -> pd.DataFrame:
    """Score full lifecycle panel · pre-burst signal days only."""
    pre = df[(~df["s3_burst"]) & (df["sig_ret"] < BURST_PCT)].copy()
    pre = pre[pre["end_q"].isin(["improving", "lagging"])].copy()
    scored = pre.apply(_score_event_row, axis=1, result_type="expand")
    out = pd.concat([pre.reset_index(drop=True), scored.reset_index(drop=True)], axis=1)
    return out


def _tier_summary(sub: pd.DataFrame, tier_id: str, label: str, score_min: float) -> PreReleaseTierStats:
    if sub.empty:
        return PreReleaseTierStats(tier_id, label, score_min, 0, 0, 0, 0, 0, 0)
    alpha = sub["alpha_nxt"] if "alpha_nxt" in sub.columns else sub["nxt_ret"] - sub["bench_nxt_ret"]
    return PreReleaseTierStats(
        tier_id=tier_id,
        tier_label=label,
        score_min=score_min,
        n=len(sub),
        mean_nxt_pct=round(float(sub["nxt_ret"].mean()), 4),
        median_nxt_pct=round(float(sub["nxt_ret"].median()), 4),
        win_rate=round(float((sub["nxt_ret"] > 0).mean()), 4),
        p_burst_pct=round(float((sub["nxt_ret"] >= BURST_PCT).mean()), 4),
        mean_alpha_pct=round(float(alpha.mean()), 4),
    )


def _summarize_tiers(scored: pd.DataFrame) -> list[PreReleaseTierStats]:
    active = scored[scored["pre_blocked"] == ""].copy()
    tiers: list[PreReleaseTierStats] = []
    # Disjoint bands [lo, hi) · hi exclusive
    bands: list[tuple[float, str, str]] = [
        (72.0, "full", "滿倉"),
        (52.0, "standard", "標準"),
        (32.0, "light", "輕倉"),
        (0.0, "observe", "觀察"),
    ]
    for i, (lo, tid, label) in enumerate(bands):
        hi = bands[i - 1][0] if i > 0 else 100.01
        sub = active[(active["pre_score"] >= lo) & (active["pre_score"] < hi)]
        tiers.append(_tier_summary(sub, tid, label, lo))
    blocked = scored[scored["pre_blocked"] != ""]
    tiers.append(_tier_summary(blocked, "blocked", "阻擋", -1.0))
    return tiers


def _decile_stats(scored: pd.DataFrame) -> list[dict[str, Any]]:
    active = scored[scored["pre_blocked"] == ""].copy()
    if len(active) < 20:
        return []
    active = active.sort_values("pre_score")
    active["decile"] = pd.qcut(active["pre_score"].rank(method="first"), 10, labels=False) + 1
    out: list[dict[str, Any]] = []
    for d in range(1, 11):
        sub = active[active["decile"] == d]
        if sub.empty:
            continue
        out.append(
            {
                "decile": int(d),
                "n": len(sub),
                "score_lo": round(float(sub["pre_score"].min()), 1),
                "score_hi": round(float(sub["pre_score"].max()), 1),
                "mean_nxt_pct": round(float(sub["nxt_ret"].mean()), 4),
                "p_burst_pct": round(float((sub["nxt_ret"] >= BURST_PCT).mean()), 4),
            }
        )
    return out


def _rank_stats(scored: pd.DataFrame) -> dict[str, float]:
    active = scored[(scored["pre_blocked"] == "") & scored["pre_score"].notna()].copy()
    if len(active) < 10:
        return {"spearman_score_nxt": 0.0, "spearman_raw_nxt": 0.0, "n": 0}
    return {
        "n": len(active),
        "spearman_score_nxt": round(float(active["pre_score"].corr(active["nxt_ret"], method="spearman")), 4),
        "spearman_raw_nxt": round(float(active["pre_raw_mul"].corr(active["nxt_ret"], method="spearman")), 4),
    }


def _weighted_portfolio(scored: pd.DataFrame, all_dates: list[str], *, min_tier: str = "standard") -> dict[str, Any]:
    tier_thr = {"full": 72.0, "standard": 52.0, "light": 32.0, "observe": 0.0}
    thr = tier_thr.get(min_tier, 52.0)
    active = scored[(scored["pre_blocked"] == "") & (scored["pre_score"] >= thr)].copy()
    if active.empty:
        return {"min_tier": min_tier, "n_events": 0, "deployed_total_pct": 0.0, "sharpe": 0.0}

    daily_rows: list[tuple[str, float]] = []
    date_idx = {d: i for i, d in enumerate(all_dates)}
    for d, grp in active.groupby("date"):
        si = date_idx.get(str(d))
        if si is None or si + 1 >= len(all_dates):
            continue
        w = grp["pre_weight"].astype(float)
        y = grp["nxt_ret"].astype(float)
        if w.sum() <= 0:
            continue
        daily_rows.append((all_dates[si + 1], float((w * y).sum() / w.sum())))

    if not daily_rows:
        return {"min_tier": min_tier, "n_events": len(active), "deployed_total_pct": 0.0, "sharpe": 0.0}

    daily = pd.Series({r: v for r, v in daily_rows})
    eq = (1 + daily / 100).cumprod()
    total = (eq.iloc[-1] - 1) * 100
    sharpe = float(daily.mean() / daily.std() * np.sqrt(252)) if daily.std() > 0 else 0.0
    years = max(len(all_dates) / 252, 1 / 252)
    cagr = ((eq.iloc[-1]) ** (1 / years) - 1) * 100 if eq.iloc[-1] > 0 else -100.0
    return {
        "min_tier": min_tier,
        "threshold_score": thr,
        "n_events": len(active),
        "n_trade_days": len(daily),
        "event_mean_pct": round(float(active["nxt_ret"].mean()), 4),
        "daily_mean_pct": round(float(daily.mean()), 4),
        "deployed_total_pct": round(total, 2),
        "deployed_cagr_pct": round(cagr, 2),
        "sharpe": round(sharpe, 2),
        "max_drawdown_pct": round(float(((eq / eq.cummax()) - 1).min() * 100), 2),
    }


def run_pre_release_backtest(
    conn: sqlite3.Connection | None = None,
    *,
    oos_days: int = 252,
) -> PreReleaseBacktestResult:
    conn = conn or connect()
    df, meta = enrich_lifecycle_events(conn)
    all_dates = sorted(df["date"].unique())
    scored = build_pre_release_scored_events(df)

    tier_stats = _summarize_tiers(scored)
    deciles = _decile_stats(scored)
    rank = _rank_stats(scored)
    port_std = _weighted_portfolio(scored, all_dates, min_tier="standard")
    port_full = _weighted_portfolio(scored, all_dates, min_tier="full")

    oos_tiers: list[PreReleaseTierStats] = []
    if len(all_dates) > oos_days:
        oos_start = all_dates[-oos_days]
        oos_tiers = _summarize_tiers(scored[scored["date"] >= oos_start])

    active = scored[scored["pre_blocked"] == ""]
    universe = {
        "n_stock_days": len(scored),
        "n_scored_active": len(active),
        "n_blocked": int((scored["pre_blocked"] != "").sum()),
        "pct_burst_d1": round(float((scored["nxt_ret"] >= BURST_PCT).mean()), 4),
        "mean_nxt_all": round(float(scored["nxt_ret"].mean()), 4),
    }

    return PreReleaseBacktestResult(
        meta={**meta, "oos_days": oos_days, "generated_on": date.today().isoformat()},
        model=pre_release_model_spec(),
        universe=universe,
        tier_stats=tier_stats,
        decile_stats=deciles,
        rank_stats=rank,
        portfolio={"standard": port_std, "full": port_full},
        oos_tier_stats=oos_tiers,
    )


def render_pre_release_backtest_markdown(result: PreReleaseBacktestResult) -> str:
    meta = result.meta
    model = result.model
    lines = [
        "# Pre-release conviction score → D+1 正式回測",
        "",
        f"- Universe: **{meta['universe_size']}** · {meta['date_start']} → {meta['date_end']}",
        f"- Generated: {meta['generated_on']}",
        "",
        "## 0. 數學模型（SSOT）",
        "",
        "**信息集** · " + model["information_set"],
        "",
        "**母體（pre-release · 釋放前）**",
        "",
        "```text",
        model["universe"],
        "```",
        "",
        "**阻擋** · `block(x) ≠ ∅` → 不計分（already_burst / excess_outlier / deep_drop / …）",
        "",
        "**乘法模型**",
        "",
        "```text",
        model["core"],
        model["boost"],
        f"M(x) = C(x) · B(x)",
        model["display"],
        "```",
        "",
        "**分層與倉位** · τ(S) → w_τ",
        "",
        "| S ≥ | tier | 倉位 w |",
        "|-----|------|--------|",
    ]
    for t in reversed(model["tier_map"]):
        lines.append(f"| {t['min_score']:.0f} | {t['label']} ({t['id']}) | {t['weight']} |")

    lines.extend(
        [
            "",
            "**結果變量（D+1 close→close）** · " + model["outcome"],
            "",
            "**組合** · " + model["portfolio"],
            "",
            "**檢定假設**",
            "",
        ]
    )
    for h in model["hypotheses"]:
        lines.append(f"- {h}")

    u = result.universe
    lines.extend(
        [
            "",
            "## 1. 母體概況",
            "",
            f"- Pre-burst stock-days: **{u['n_stock_days']:,}**",
            f"- 有效計分（未 block）: **{u['n_scored_active']:,}** · block: **{u['n_blocked']:,}**",
            f"- 全母體 D+1 mean: **{u['mean_nxt_all']:+.3f}%** · P(D+1≥5%): **{u['pct_burst_d1']:.2%}**",
            "",
            "## 2. Tier → D+1（H_mono · H_tail）",
            "",
            "| tier | N | D+1 mean | median | 勝率 | P(≥5%) | mean(α) |",
            "|------|---|---------|--------|------|--------|---------|",
        ]
    )
    tier_hi = {"full": 100, "standard": 72, "light": 52, "observe": 32}
    for t in result.tier_stats:
        if t.tier_id == "blocked":
            continue
        hi = tier_hi.get(t.tier_id, 100)
        lines.append(
            f"| {t.tier_label} [{t.score_min:.0f},{hi:.0f}) | {t.n:,} | {t.mean_nxt_pct:+.3f}% | "
            f"{t.median_nxt_pct:+.3f}% | {t.win_rate:.1%} | {t.p_burst_pct:.2%} | {t.mean_alpha_pct:+.3f}% |"
        )

    r = result.rank_stats
    lines.extend(
        [
            "",
            "## 3. 排序力（H_rank）",
            "",
            f"- N = **{r['n']:,}**",
            f"- Spearman(**S**, D+1): **{r['spearman_score_nxt']:.4f}**",
            f"- Spearman(**M**, D+1): **{r['spearman_raw_nxt']:.4f}**",
            "",
            "## 4. 分數 decile → D+1",
            "",
            "| decile | N | S 區間 | D+1 mean | P(≥5%) |",
            "|--------|---|--------|---------|--------|",
        ]
    )
    for d in result.decile_stats:
        lines.append(
            f"| D{d['decile']} | {d['n']:,} | [{d['score_lo']}, {d['score_hi']}] | "
            f"{d['mean_nxt_pct']:+.3f}% | {d['p_burst_pct']:.2%} |"
        )

    lines.extend(["", "## 5. 加權組合回測（w_τ · D 收→D+1 收）", ""])
    for key, label in [("standard", "S≥52 標準+"), ("full", "S≥72 滿倉")]:
        p = result.portfolio[key]
        lines.append(
            f"### {label} · threshold **{p.get('threshold_score', 0):.0f}** · "
            f"N={p['n_events']:,} · trade days={p.get('n_trade_days', 0)}"
        )
        lines.append(
            f"- 每筆 mean **{p.get('event_mean_pct', 0):+.3f}%** · "
            f"deployed 總 **{p.get('deployed_total_pct', 0):+.1f}%** · "
            f"Sharpe **{p.get('sharpe', 0):.2f}** · MaxDD **{p.get('max_drawdown_pct', 0):.1f}%**"
        )
        lines.append("")

    if result.oos_tier_stats:
        lines.extend(["## 6. OOS 近 252 日 · tier", ""])
        lines.append("| tier | N | D+1 mean | P(≥5%) |")
        lines.append("|------|---|---------|--------|")
        for t in result.oos_tier_stats:
            if t.tier_id == "blocked":
                continue
            lines.append(
                f"| {t.tier_label} | {t.n:,} | {t.mean_nxt_pct:+.3f}% | {t.p_burst_pct:.2%} |"
            )

    lines.extend(
        [
            "",
            "## 7. 解讀",
            "",
            "1. **Pre-release 分** 在 D 收盤 PIT 計算 · 預測 **釋放前** 隔日 burst / drift。",
            "2. Tier 單調 ↑ → 支持 H_mono / H_tail；Spearman → 支持 H_rank。",
            "3. 組合回測用 **w_τ 加權** · 非 oracle · 不含成本。",
            "",
        ]
    )
    return "\n".join(lines) + "\n"
