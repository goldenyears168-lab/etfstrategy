"""轨 B：Leg 级 forward α 衰减曲线（L1 进场 · 每 leg 固定部署）。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date

from .copytrade_backtest import (
    ADD_ACTIONS,
    bench_return_entry_to_exit,
    group_signals_by_date,
    iter_copytrade_signals,
    resolve_entry_date,
)
from flow_returns import exit_close_date_from_entry, return_pct, stock_close, stock_open

DEFAULT_LEG_CAPITAL_NTD = 10_000.0
ENTRY_LAG_DAYS = 0  # L1
ENTRY_PRICE_MODE = "open"
INITIATION_ACTION = "新进"
REPEAT_ADD_ACTION = "加码"


@dataclass(frozen=True)
class LegHorizonObs:
    signal_date: str
    stock_id: str
    action: str
    entry_date: str
    exit_date: str
    horizon: int
    allocated_ntd: float
    return_pct: float
    bench_return_pct: float
    excess_pct: float
    alpha_ntd: float
    multi_leg_day: bool


def _entry_px(conn: sqlite3.Connection, stock_id: str, entry_date: str) -> float | None:
    return stock_open(conn, stock_id, entry_date)


def collect_leg_horizon_observations(
    conn: sqlite3.Connection,
    etf_code: str,
    *,
    max_horizon: int = 45,
    leg_capital_ntd: float = DEFAULT_LEG_CAPITAL_NTD,
    entry_lag_days: int = ENTRY_LAG_DAYS,
    window_start: str | None = None,
    window_end: str | None = None,
) -> list[LegHorizonObs]:
    """每个 complete leg 在 H=1..max_horizon 的 forward α（相对台指同进出场）。"""
    signals = iter_copytrade_signals(
        conn,
        etf_code,
        window_start=window_start,
        window_end=window_end,
        actions=ADD_ACTIONS,
    )
    grouped = group_signals_by_date(signals)
    out: list[LegHorizonObs] = []

    for signal_date, legs in grouped.items():
        entry_date = resolve_entry_date(conn, signal_date, entry_lag_days)
        if entry_date is None:
            continue
        multi = len(legs) > 1
        for sig in legs:
            entry_px = _entry_px(conn, sig.stock_id, entry_date)
            if entry_px is None or entry_px <= 0:
                continue
            for h in range(1, max_horizon + 1):
                exit_date = exit_close_date_from_entry(conn, entry_date, h)
                if exit_date is None:
                    break
                exit_px = stock_close(conn, sig.stock_id, exit_date)
                if exit_px is None:
                    break
                bench = bench_return_entry_to_exit(
                    conn,
                    entry_date,
                    exit_date,
                    entry_price_mode=ENTRY_PRICE_MODE,
                )
                if bench is None:
                    break
                leg_ret = return_pct(entry_px, exit_px)
                excess = leg_ret - bench
                alpha = leg_capital_ntd * excess / 100.0
                out.append(
                    LegHorizonObs(
                        signal_date=signal_date,
                        stock_id=sig.stock_id,
                        action=sig.action,
                        entry_date=entry_date,
                        exit_date=exit_date,
                        horizon=h,
                        allocated_ntd=leg_capital_ntd,
                        return_pct=leg_ret,
                        bench_return_pct=bench,
                        excess_pct=excess,
                        alpha_ntd=alpha,
                        multi_leg_day=multi,
                    )
                )
    return out


def _bucket_value(obs: LegHorizonObs, bucket_field: str) -> str:
    if bucket_field == "all":
        return "all"
    if bucket_field == "action":
        return obs.action
    if bucket_field == "leg_day_size":
        return "multi_leg" if obs.multi_leg_day else "single_leg"
    raise ValueError(f"unknown bucket_field: {bucket_field}")


def _wilcoxon_vs_zero(values: list[float]) -> float | None:
    if len(values) < 20:
        return None
    try:
        from scipy.stats import wilcoxon

        nz = [v for v in values if abs(v) > 1e-12]
        if len(nz) < 15:
            return None
        _, p = wilcoxon(nz)
        return float(p)
    except Exception:
        return None


def aggregate_leg_decay_curves(
    observations: list[LegHorizonObs],
    *,
    etf_code: str,
    bucket_fields: tuple[str, ...] = ("all", "action", "leg_day_size"),
    max_horizon: int = 45,
) -> list[dict]:
    rows: list[dict] = []
    for bf in bucket_fields:
        buckets: dict[str, dict[int, list[LegHorizonObs]]] = {}
        for obs in observations:
            if obs.horizon > max_horizon:
                continue
            b = _bucket_value(obs, bf)
            buckets.setdefault(b, {}).setdefault(obs.horizon, []).append(obs)

        for bucket, by_h in sorted(buckets.items()):
            prev_mean_excess = 0.0
            prev_sum_alpha = 0.0
            for h in sorted(by_h):
                obs_list = by_h[h]
                excess = [o.excess_pct for o in obs_list]
                alphas = [o.alpha_ntd for o in obs_list]
                n = len(obs_list)
                mean_excess = sum(excess) / n if n else None
                mean_alpha = sum(alphas) / n if n else None
                sum_alpha = sum(alphas)
                marg_excess = (
                    (mean_excess - prev_mean_excess)
                    if mean_excess is not None and h > 1
                    else mean_excess
                )
                rows.append(
                    {
                        "etf_code": etf_code,
                        "entry_lag_days": ENTRY_LAG_DAYS,
                        "bucket_field": bf,
                        "bucket_value": bucket,
                        "horizon": h,
                        "n_legs": n,
                        "mean_excess_pct": round(mean_excess, 4) if mean_excess is not None else None,
                        "mean_alpha_ntd": round(mean_alpha, 2) if mean_alpha is not None else None,
                        "sum_alpha_ntd": round(sum_alpha, 2),
                        "marginal_mean_excess_pct": (
                            round(marg_excess, 4) if marg_excess is not None else None
                        ),
                        "marginal_sum_alpha_ntd": round(sum_alpha - prev_sum_alpha, 2),
                        "p_value_wilcoxon": _wilcoxon_vs_zero(excess),
                        "is_significant": int(
                            _wilcoxon_vs_zero(excess) is not None
                            and _wilcoxon_vs_zero(excess) < 0.05
                        ),
                    }
                )
                if mean_excess is not None:
                    prev_mean_excess = mean_excess
                prev_sum_alpha = sum_alpha
    return rows


def summarize_leg_decay_knees(
    curve_rows: list[dict],
    *,
    bucket_field: str = "all",
    marginal_window: tuple[int, int] = (5, 30),
) -> list[dict]:
    """实务膝点：邊際 sum_α 相对 H5–H30 峰值跌破 25%；另给 α/日效率峰。"""
    by_bucket: dict[str, list[dict]] = {}
    for r in curve_rows:
        if r["bucket_field"] != bucket_field:
            continue
        by_bucket.setdefault(str(r["bucket_value"]), []).append(r)

    out: list[dict] = []
    for bucket, rows in sorted(by_bucket.items()):
        sub = sorted(rows, key=lambda x: int(x["horizon"]))
        if not sub:
            continue
        peak = max(sub, key=lambda x: float(x["mean_excess_pct"] or 0))
        peak_h = int(peak["horizon"])
        peak_excess = float(peak["mean_excess_pct"] or 0)

        best_sum = max(sub, key=lambda x: float(x["sum_alpha_ntd"] or 0))
        best_sum_h = int(best_sum["horizon"])

        lo, hi = marginal_window
        marginals = [
            (int(r["horizon"]), float(r["marginal_sum_alpha_ntd"] or 0))
            for r in sub
            if lo <= int(r["horizon"]) <= hi
        ]
        max_marg = max((m for _, m in marginals), default=0.0)
        threshold = max(max_marg * 0.25, 5_000.0)

        marginal_knee_h = peak_h
        consecutive_below = 0
        for h, m in marginals:
            if h < 15:
                continue
            if m < threshold:
                consecutive_below += 1
                if consecutive_below >= 2:
                    marginal_knee_h = h - 2
                    break
            else:
                consecutive_below = 0

        efficiency = max(
            sub,
            key=lambda r: float(r["sum_alpha_ntd"] or 0) / max(int(r["horizon"]), 1),
        )
        efficiency_h = int(efficiency["horizon"])

        out.append(
            {
                "bucket_field": bucket_field,
                "bucket_value": bucket,
                "peak_mean_excess_h": peak_h,
                "peak_mean_excess_pct": peak_excess,
                "best_sum_alpha_h": best_sum_h,
                "best_sum_alpha_ntd": best_sum["sum_alpha_ntd"],
                "knee_h": marginal_knee_h,
                "marginal_knee_h": marginal_knee_h,
                "efficiency_h": efficiency_h,
                "efficiency_alpha_per_day": round(
                    float(efficiency["sum_alpha_ntd"] or 0) / efficiency_h, 2
                ),
                "n_legs_at_peak": peak["n_legs"],
            }
        )
    return out


def format_leg_decay_markdown(
    *,
    etf_code: str,
    batch_id: str,
    curve_rows: list[dict],
    knees: list[dict],
    max_horizon: int,
    leg_capital_ntd: float,
    n_obs: int,
) -> str:
    today = date.today().strftime("%Y%m%d")
    lines = [
        f"# {etf_code} Leg 级 α 衰减曲线（轨 B · L1）",
        "",
        f"> batch `{batch_id}` · 每 leg {leg_capital_ntd:,.0f} NTD · "
        f"H1–H{max_horizon} · 报告日 {today}",
        "",
        "## 方法",
        "",
        "- **单位**：每个新进/加码 **leg**（不是讯号日组合）。",
        "- **进场**：T+1 开盘（L1）；**出场**：进场后第 H 日收盘。",
        f"- **α**：leg 报酬 − 台指同规则报酬，× {leg_capital_ntd:,.0f} NTD。",
        "- **膝点 knee_h**：H5–H30 内邊際 **Δsum α** 相对峰值跌破 25% 的首个 H−1（牛市下 mean_excess 可单调升，不可单靠均值峰）。",
        f"- **观测点**：{n_obs:,}（leg×H complete 格）",
        "",
        "## 膝点摘要",
        "",
        "| 分层 | 桶 | 峰值 H | 峰值日均超额% | 累计α最大 H | knee_h | 效率峰 H | n |",
        "|------|----|--------|--------------|------------|--------|----------|---|",
    ]
    for k in knees:
        lines.append(
            f"| {k['bucket_field']} | {k['bucket_value']} | H{k['peak_mean_excess_h']} | "
            f"{k['peak_mean_excess_pct']:.3f} | H{k['best_sum_alpha_h']} | "
            f"H{k['knee_h']} | H{k.get('efficiency_h', k['knee_h'])} | {k['n_legs_at_peak']} |"
        )

    for bf in ("all", "action", "leg_day_size"):
        buckets = sorted(
            {str(r["bucket_value"]) for r in curve_rows if r["bucket_field"] == bf}
        )
        if not buckets:
            continue
        lines.extend(["", f"## 分层：{bf}", ""])
        for bucket in buckets:
            sub = sorted(
                [
                    r
                    for r in curve_rows
                    if r["bucket_field"] == bf and r["bucket_value"] == bucket
                ],
                key=lambda x: int(x["horizon"]),
            )
            knee = next(
                (
                    k
                    for k in knees
                    if k["bucket_field"] == bf and k["bucket_value"] == bucket
                ),
                None,
            )
            lines.extend(["", f"### {bucket}", ""])
            if knee:
                lines.append(
                    f"峰值 mean_excess **H{knee['peak_mean_excess_h']}** "
                    f"({knee['peak_mean_excess_pct']:.3f}%) · "
                    f"邊際膝点 **H{knee['knee_h']}** · "
                    f"α/日效率峰 **H{knee.get('efficiency_h', knee['knee_h'])}**"
                )
            lines.append("")
            lines.append(
                "| H | n | mean超额% | Δmean超额 | mean α | Δsum α | p(W) |"
            )
            lines.append("|---|-----|---------|---------|--------|--------|------|")
            for r in sub:
                if int(r["horizon"]) > min(max_horizon, 30) and int(r["horizon"]) % 5 != 0:
                    if int(r["horizon"]) != max_horizon:
                        continue
                mark = (
                    " **"
                    if knee
                    and int(r["horizon"]) in (knee["knee_h"], knee["peak_mean_excess_h"])
                    else ""
                )
                end = "**" if mark else ""
                p = r["p_value_wilcoxon"]
                p_s = f"{p:.4f}" if p is not None else "—"
                lines.append(
                    f"| {mark}H{r['horizon']}{end} | {r['n_legs']} | "
                    f"{r['mean_excess_pct'] or 0:.3f} | "
                    f"{r['marginal_mean_excess_pct'] or 0:+.3f} | "
                    f"{r['mean_alpha_ntd'] or 0:.0f} | "
                    f"{r['marginal_sum_alpha_ntd']:+,.0f} | {p_s} |"
                )

    lines.extend(
        [
            "",
            "## 解读",
            "",
            "- **mean_excess%** 随 H 上升 ≠ 应该持那么久；看 **Δmean超额** 何时趋近 0。",
            "- **新进 vs 加码** 若膝点不同，可支持条件持有期（轨 D 规则雏形）。",
            "- 本分析未含提前出场事件（经理减码等）→ 见轨 C。",
            "",
        ]
    )
    return "\n".join(lines)


def run_leg_decay_analysis(
    conn: sqlite3.Connection,
    *,
    etf_code: str,
    batch_id: str | None = None,
    max_horizon: int = 45,
    leg_capital_ntd: float = DEFAULT_LEG_CAPITAL_NTD,
    window_start: str | None = None,
    window_end: str | None = None,
    persist: bool = True,
) -> dict[str, object]:
    from stock_db import persist_copytrade_leg_decay

    bid = batch_id or f"{etf_code.lower()}-leg-decay-{date.today().strftime('%Y%m%d')}"
    observations = collect_leg_horizon_observations(
        conn,
        etf_code,
        max_horizon=max_horizon,
        leg_capital_ntd=leg_capital_ntd,
        window_start=window_start,
        window_end=window_end,
    )
    curve_rows = aggregate_leg_decay_curves(
        observations,
        etf_code=etf_code,
        max_horizon=max_horizon,
    )
    knees: list[dict] = []
    for bf in ("all", "action", "leg_day_size"):
        knees.extend(summarize_leg_decay_knees(curve_rows, bucket_field=bf))

    if persist:
        persist_copytrade_leg_decay(conn, bid, curve_rows, knees=knees)

    return {
        "batch_id": bid,
        "observations": observations,
        "curve_rows": curve_rows,
        "knees": knees,
        "n_unique_legs": len({(o.signal_date, o.stock_id) for o in observations}),
    }
