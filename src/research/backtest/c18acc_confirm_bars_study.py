"""C18acc champion · confirm_bars 1 vs 2 · Order layer adoption evidence."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from typing import Any

from market_benchmark import load_benchmark_close
from market_breadth_ma import build_breadth_panel
from research.backtest.finpilot_local_backtest import load_price_panels, summarize_periods
from research.backtest.rrg_mono_backtest import build_fresh_mono_calendar
from research.backtest.rrg_mono_intraday_ab import CVariantConfig, DEFAULT_C_SWEEP
from research.backtest.rrg_mono_score_swap_c import (
    _poll5m_needs_spread_rs_panel,
    build_daily_spread_rs_panel,
    champion_score_swap_c_config,
    simulate_score_swap_c,
)
from research.backtest.rrg_mono_swap_exit_b import build_mono_tier2_calendar
from rrg_mono_daily_brief import LENGTH
from rrg_rotation import compute_rrg_panel


def _entry_c_config(confirm_bars: int) -> CVariantConfig:
    base = next(c for c in DEFAULT_C_SWEEP if c.variant_id == ("C0" if confirm_bars <= 1 else "C3"))
    if int(base.confirm_bars) == int(confirm_bars):
        return base
    return replace(base, confirm_bars=int(confirm_bars))


def _slice_summary(periods: list[dict[str, Any]], *, start: str, end: str) -> dict[str, Any]:
    sub = [
        p
        for p in periods
        if start <= str(p.get("entry_date") or p.get("signal_date") or "") <= end
    ]
    s = summarize_periods(sub)
    s["n_periods"] = len(sub)
    s["date_start"] = start
    s["date_end"] = end
    entries = sum(1 for p in sub if str(p.get("action") or "") in ("entry", "swap_buy", "buy"))
    s["n_entries"] = entries or len(sub)
    mr, mb = s.get("mean_return_pct"), s.get("mean_bench_pct")
    if mr is not None and mb is not None:
        s["mean_excess_pct"] = round(float(mr) - float(mb), 4)
    else:
        s["mean_excess_pct"] = None
    return s


def run_confirm_bars_study(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2024-01-02",
    date_end: str | None = None,
    is_end: str = "2025-12-31",
    confirm_variants: tuple[int, ...] = (1, 2),
) -> dict[str, Any]:
    """Champion POOL1 sim · vary only entry confirm_bars."""
    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index)
    rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench, length=LENGTH)
    full_dates = close.index.astype(str).tolist()
    if date_end is None:
        date_end = full_dates[-1]
    trade_dates = [d for d in full_dates if date_start <= d <= date_end]
    if len(trade_dates) < 20:
        raise ValueError(f"need ≥20 trade dates, got {len(trade_dates)}")

    fresh_by_date = build_fresh_mono_calendar(conn, trade_dates)
    mono_by_date = build_mono_tier2_calendar(conn, trade_dates, close=close, bench=bench)
    panel = build_breadth_panel(conn, date_start=trade_dates[0], date_end=trade_dates[-1])
    zone_by_date = {str(r.trade_date): str(r.zone_200) for r in panel.itertuples()}
    cfg = champion_score_swap_c_config()
    spread_rs_panel = (
        build_daily_spread_rs_panel(close, bench)
        if _poll5m_needs_spread_rs_panel(cfg)
        else None
    )
    kbar_cache: dict[tuple[str, str], tuple[tuple[str, float], ...]] = {}

    oos_start = next((d for d in trade_dates if d > is_end), None)
    variants: list[dict[str, Any]] = []
    baseline_confirm = int(confirm_variants[0])

    for bars in confirm_variants:
        entry_cfg = _entry_c_config(bars)
        print(f"C18acc confirm_bars={bars} · champion sim ...", flush=True)
        periods, summary = simulate_score_swap_c(
            conn,
            trade_dates=trade_dates,
            full_dates=full_dates,
            close=close,
            bench=bench,
            fresh_by_date=fresh_by_date,
            zone_by_date=zone_by_date,
            config=cfg,
            mono_by_date=mono_by_date,
            kbar_cache=kbar_cache,
            rs_mom=rs_mom,
            rs_ratio=rs_ratio,
            spread_rs_panel=spread_rs_panel,
            entry_c_config=entry_cfg,
        )
        full = dict(summary)
        full["confirm_bars"] = bars
        full["entry_variant_id"] = entry_cfg.variant_id
        full["slices"] = {
            "full": _slice_summary(periods, start=date_start, end=date_end),
            "is": _slice_summary(periods, start=date_start, end=is_end),
        }
        if oos_start:
            full["slices"]["oos"] = _slice_summary(periods, start=oos_start, end=date_end)
        else:
            full["slices"]["oos"] = {"n_periods": 0, "mean_excess_pct": None}
        if bars == baseline_confirm:
            full["delta_vs_baseline_pp"] = 0.0
        variants.append(full)
        print(
            f"  done confirm={bars}: n={summary.get('n_periods')} "
            f"mean_excess={summary.get('mean_excess_pct')}% "
            f"entries={summary.get('total_entries')}",
            flush=True,
        )

    base_ex = variants[0].get("mean_excess_pct")
    for v in variants[1:]:
        be = base_ex if base_ex is not None else 0.0
        ve = v.get("mean_excess_pct")
        v["delta_vs_baseline_pp"] = round(float(ve or 0) - float(be), 4) if ve is not None else None

    ranked = sorted(
        variants,
        key=lambda s: (-(s.get("mean_excess_pct") or -999.0), -(s.get("n_periods") or 0)),
    )
    alpha_winner = ranked[0]
    c1 = next(v for v in variants if int(v.get("confirm_bars") or 0) == 1)
    c2 = next(v for v in variants if int(v.get("confirm_bars") or 0) == 2)
    delta_pp = float(c2.get("delta_vs_baseline_pp") or 0)
    same_n = int(c1.get("n_periods") or 0) == int(c2.get("n_periods") or 0)
    live_confirm = 2 if same_n and abs(delta_pp) <= 0.2 else int(alpha_winner.get("confirm_bars") or 1)
    verdict = {
        "alpha_winner_confirm_bars": alpha_winner.get("confirm_bars"),
        "live_confirm_bars": live_confirm,
        "same_trade_count": same_n,
        "delta_confirm2_vs_1_pp": delta_pp,
        "recommend_live": live_confirm >= 2,
        "summary": (
            f"alpha winner confirm={alpha_winner.get('confirm_bars')} "
            f"mean_excess={alpha_winner.get('mean_excess_pct')}% · "
            f"confirm=2 vs 1: Δ{delta_pp}pp · same n={same_n} · "
            f"live adopt confirm={live_confirm} "
            f"({'operational anti-flicker' if live_confirm == 2 and alpha_winner.get('confirm_bars') == 1 else 'alpha'})"
        ),
    }
    return {
        "date_start": date_start,
        "date_end": date_end,
        "is_end": is_end,
        "oos_start": oos_start,
        "champion_variant_id": cfg.variant_id,
        "baseline_confirm_bars": baseline_confirm,
        "variants": variants,
        "winner": alpha_winner,
        "verdict": verdict,
    }


def render_confirm_bars_md(payload: dict[str, Any]) -> str:
    lines = [
        "# C18acc champion · confirm_bars adoption study",
        "",
        f"窗口：**{payload['date_start']}** .. **{payload['date_end']}** · "
        f"champion `{payload.get('champion_variant_id')}` · "
        f"baseline confirm={payload.get('baseline_confirm_bars')}",
        "",
        f"OOS 起點：{payload.get('oos_start') or '—'}（IS 截止 {payload.get('is_end')}）",
        "",
        "## 全窗口",
        "",
        "| confirm | entry_id | n legs | mean_excess% | total_entries | swaps | vs baseline |",
        "|--------:|----------|-------:|-------------:|--------------:|------:|------------:|",
    ]
    for v in payload.get("variants") or []:
        lines.append(
            f"| {v.get('confirm_bars')} | {v.get('entry_variant_id')} "
            f"| {v.get('n_periods')} | {v.get('mean_excess_pct')} "
            f"| {v.get('total_entries')} | {v.get('swaps_total')} "
            f"| {v.get('delta_vs_baseline_pp')}pp |"
        )
    lines += ["", "## 分段", ""]
    for label, key in (("IS", "is"), ("OOS", "oos")):
        lines.append(f"### {label}")
        lines.append("")
        lines.append("| confirm | n | mean_excess% |")
        lines.append("|--------:|--:|-------------:|")
        for v in payload.get("variants") or []:
            sl = (v.get("slices") or {}).get(key) or {}
            lines.append(
                f"| {v.get('confirm_bars')} | {sl.get('n_periods')} | {sl.get('mean_excess_pct')} |"
            )
        lines.append("")
    verdict = payload.get("verdict") or {}
    lines += [
        "## 採納建議",
        "",
        f"- **Alpha 冠軍**：confirm=`{verdict.get('alpha_winner_confirm_bars')}`",
        f"- **Live `C18ACC_CONFIRM_BARS`**：`{verdict.get('live_confirm_bars')}` "
        f"（confirm=2 vs 1：Δ{verdict.get('delta_confirm2_vs_1_pp')}pp · "
        f"same n={verdict.get('same_trade_count')}）",
        f"- {verdict.get('summary', '')}",
        "",
        "模組：`src/research/backtest/c18acc_confirm_bars_study.py`",
    ]
    return "\n".join(lines) + "\n"
