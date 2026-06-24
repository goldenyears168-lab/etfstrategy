#!/usr/bin/env python3
"""C18acc funnel ablation · buy logic · pool · mono_up · entry · max_swaps."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from market_benchmark import load_benchmark_close
from market_breadth_ma import build_breadth_panel
from project_config import DEFAULT_ETF_CODES
from research.backtest.finpilot_local_backtest import load_price_panels, summarize_periods
from research.backtest.rrg_mono_backtest import build_fresh_mono_calendar
from research.backtest.rrg_mono_intraday_ab import CVariantConfig, DEFAULT_C_SWEEP
from research.backtest.rrg_mono_score_swap_c import (
    CHAMPION_SCORE_SWAP_C_VARIANT_ID,
    ScoreSwapCConfig,
    champion_score_swap_c_config,
    simulate_score_swap_c,
)
from research.backtest.rrg_mono_swap_exit_b import build_mono_tier2_calendar
from report_paths import RESEARCH_RRG
from rrg_mono_daily_brief import LOOKBACK, ScanRow, _feat, _mono_tier2, _tier2_gate
from rrg_rotation import compute_rrg_panel
from stock_db import DEFAULT_DB_PATH, connect, load_etf_constituent_watchlist

DATE_START = "2024-01-01"
DATE_END = "2026-06-22"


def _load_context(conn, date_start: str, date_end: str) -> dict[str, Any]:
    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index)
    rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench, length=20)
    full_dates = close.index.astype(str).tolist()
    trade_dates = [d for d in full_dates if date_start <= d <= date_end]
    fresh_by_date = build_fresh_mono_calendar(conn, trade_dates)
    mono_by_date = build_mono_tier2_calendar(conn, trade_dates, close=close, bench=bench)
    panel = build_breadth_panel(conn, date_start=date_start, date_end=date_end)
    zone_by_date = {str(r.trade_date): str(r.zone_200) for r in panel.itertuples()}
    return {
        "close": close,
        "bench": bench,
        "rs_ratio": rs_ratio,
        "rs_mom": rs_mom,
        "full_dates": full_dates,
        "trade_dates": trade_dates,
        "fresh_by_date": fresh_by_date,
        "mono_by_date": mono_by_date,
        "zone_by_date": zone_by_date,
    }


def _fresh_tier2_no_mono_up(
    rs_ratio,
    rs_mom,
    full_dates: list[str],
    si: int,
    sid: str,
    *,
    lb: int = LOOKBACK,
) -> bool:
    f = _feat(rs_ratio, rs_mom, full_dates, si, sid, lb=lb)
    if not _tier2_gate(f):
        return False
    prev = _feat(rs_ratio, rs_mom, full_dates, si - 1, sid, lb=lb)
    return not _tier2_gate(prev)


def build_tier2_fresh_no_mono_up_calendar(
    conn,
    trade_dates: list[str],
    *,
    close=None,
    bench=None,
) -> dict[str, list[ScanRow]]:
    """tier2 fresh transition · 不要求 mono_up（B1 ablation）。"""
    if close is None or bench is None:
        close, _, _ = load_price_panels(conn)
        bench = load_benchmark_close(conn).reindex(close.index)
    rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench, length=20)
    daily_pct = close.pct_change(fill_method=None) * 100.0
    full_dates = close.index.astype(str).tolist()
    watch = load_etf_constituent_watchlist(conn, DEFAULT_ETF_CODES)
    name_map = {w["stock_id"]: w.get("stock_name", "") for w in watch}
    universe = [w["stock_id"] for w in watch]
    date_set = set(trade_dates)
    lb = LOOKBACK

    out: dict[str, list[ScanRow]] = {d: [] for d in trade_dates}
    for si, as_of in enumerate(full_dates):
        if as_of not in date_set or si < lb:
            continue
        pool: list[ScanRow] = []
        for sid in universe:
            if not _fresh_tier2_no_mono_up(rs_ratio, rs_mom, full_dates, si, sid, lb=lb):
                continue
            f = _feat(rs_ratio, rs_mom, full_dates, si, sid, lb=lb)
            if f is None:
                continue
            pct = float(daily_pct.at[as_of, sid]) if sid in daily_pct.columns else None
            if pct != pct:
                pct = None
            pool.append(
                ScanRow(
                    stock_id=sid,
                    stock_name=name_map.get(sid, ""),
                    fresh=True,
                    mono=bool(f.get("mono_up")),
                    seg_last=float(f["seg_last"]),
                    disp=float(f["disp"]),
                    segs=[float(x) for x in f["segs"]],
                    quadrants=[q or "?" for q in f["quadrants"]],
                    rs_ratio=float(f["rs_ratio"]),
                    rs_momentum=float(f["rs_momentum"]),
                    daily_pct=pct,
                )
            )
        pool.sort(key=lambda r: (-r.seg_last, r.stock_id))
        out[as_of] = pool
    return out


def _mono_up_redundancy_stats(conn, ctx: dict[str, Any]) -> dict[str, Any]:
    rs_ratio = ctx["rs_ratio"]
    rs_mom = ctx["rs_mom"]
    full_dates = ctx["full_dates"]
    trade_dates = ctx["trade_dates"]
    fresh_by_date = ctx["fresh_by_date"]
    watch = load_etf_constituent_watchlist(conn, DEFAULT_ETF_CODES)
    universe = [w["stock_id"] for w in watch]
    lb = LOOKBACK

    tier2_n: list[int] = []
    mono_tier2_n: list[int] = []
    tier2_not_mono_n: list[int] = []
    fresh_n: list[int] = []
    tier2_fresh_no_mono_n: list[int] = []

    for as_of in trade_dates:
        si = full_dates.index(as_of)
        if si < lb:
            continue
        t2 = mt2 = t2nm = 0
        for sid in universe:
            f = _feat(rs_ratio, rs_mom, full_dates, si, sid, lb=lb)
            if _tier2_gate(f):
                t2 += 1
                if not (f and f.get("mono_up")):
                    t2nm += 1
            if _mono_tier2(f):
                mt2 += 1
        fresh_n.append(len(fresh_by_date.get(as_of, [])))
        tier2_n.append(t2)
        mono_tier2_n.append(mt2)
        tier2_not_mono_n.append(t2nm)
        tier2_fresh_no_mono_n.append(
            sum(
                1
                for sid in universe
                if _fresh_tier2_no_mono_up(rs_ratio, rs_mom, full_dates, si, sid, lb=lb)
            )
        )

    n = len(trade_dates) or 1
    return {
        "n_trade_days": len(trade_dates),
        "mean_tier2_per_day": round(sum(tier2_n) / n, 2),
        "mean_mono_tier2_per_day": round(sum(mono_tier2_n) / n, 2),
        "mean_tier2_not_mono_up_per_day": round(sum(tier2_not_mono_n) / n, 2),
        "mean_fresh_mono_per_day": round(sum(fresh_n) / n, 2),
        "mean_tier2_fresh_no_mono_up_per_day": round(sum(tier2_fresh_no_mono_n) / n, 2),
        "pct_days_tier2_not_mono_gt_fresh": round(
            100.0 * sum(1 for i, f in enumerate(fresh_n) if tier2_not_mono_n[i] > f) / n,
            2,
        ),
    }


def _swap_legs(periods: list[dict[str, Any]]) -> list[tuple[str, str, str]]:
    legs = []
    for p in periods:
        if p.get("exit_reason") != "score_swap":
            continue
        legs.append(
            (
                str(p["stock_id"]),
                str(p.get("challenger_id") or ""),
                str(p.get("signal_date") or p.get("entry_date") or ""),
            )
        )
    return legs


def _compare_swap_legs(
    base: list[tuple[str, str, str]], other: list[tuple[str, str, str]]
) -> dict[str, Any]:
    return {
        "n_base": len(base),
        "n_other": len(other),
        "all_match": base == other,
        "n_matching_prefix": sum(1 for a, b in zip(base, other) if a == b),
    }


def _summarize(periods: list[dict[str, Any]], summary: dict[str, Any]) -> dict[str, Any]:
    s = dict(summary)
    n = len(periods)
    if n:
        s["mean_excess_pct"] = round(sum(p["excess_pct"] for p in periods) / n, 4)
    s["n_periods"] = n
    return s


def _pool_size_bucket(signal_date: str, fresh_by_date: dict[str, list]) -> str:
    n = len(fresh_by_date.get(signal_date, []))
    if n == 0:
        return "pool_0"
    if n == 1:
        return "pool_1"
    return "pool_ge2"


def _split_by_pool_size(
    periods: list[dict[str, Any]], fresh_by_date: dict[str, list]
) -> dict[str, dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = {"pool_0": [], "pool_1": [], "pool_ge2": []}
    for p in periods:
        sig = str(p.get("signal_date") or p.get("entry_date") or "")
        buckets[_pool_size_bucket(sig, fresh_by_date)].append(p)
    out: dict[str, dict[str, Any]] = {}
    for key, legs in buckets.items():
        s = summarize_periods(legs)
        n = len(legs)
        s["n_periods"] = n
        s["mean_excess_pct"] = round(sum(x["excess_pct"] for x in legs) / n, 4) if n else None
        out[key] = s
    return out


def _run_variant(
    conn,
    ctx: dict[str, Any],
    cfg: ScoreSwapCConfig,
    kbar_cache: dict,
    *,
    fresh_by_date: dict[str, list[ScanRow]] | None = None,
    entry_c_config: CVariantConfig | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    periods, summary = simulate_score_swap_c(
        conn,
        trade_dates=ctx["trade_dates"],
        full_dates=ctx["full_dates"],
        close=ctx["close"],
        bench=ctx["bench"],
        fresh_by_date=fresh_by_date or ctx["fresh_by_date"],
        zone_by_date=ctx["zone_by_date"],
        config=cfg,
        mono_by_date=ctx["mono_by_date"],
        kbar_cache=kbar_cache,
        rs_mom=ctx["rs_mom"],
        rs_ratio=ctx["rs_ratio"],
        entry_c_config=entry_c_config,
    )
    return periods, _summarize(periods, summary)


def _champion_cfg(**overrides: Any) -> ScoreSwapCConfig:
    base = champion_score_swap_c_config()
    if not overrides:
        return base
    d = base.to_dict()
    d.update(overrides)
    return ScoreSwapCConfig(**{k: d[k] for k in ScoreSwapCConfig.__dataclass_fields__})


def _delta_vs_champion(champ_ex: float | None, ex: float | None) -> float | None:
    if champ_ex is None or ex is None:
        return None
    return round(ex - champ_ex, 4)


def run_ablation(
    conn,
    *,
    date_start: str = DATE_START,
    date_end: str = DATE_END,
) -> dict[str, Any]:
    ctx = _load_context(conn, date_start, date_end)
    kbar_cache: dict[tuple[str, str], tuple[tuple[str, float], ...]] = {}

    champion = champion_score_swap_c_config()
    champ_periods, champ_summary = _run_variant(conn, ctx, champion, kbar_cache)
    champ_ex = champ_summary.get("mean_excess_pct")
    champ_swaps = champ_summary.get("swaps_total")
    champ_legs = _swap_legs(champ_periods)

    variants: list[dict[str, Any]] = []

    def _record(
        variant_id: str,
        hypothesis: str,
        section: str,
        periods: list[dict[str, Any]],
        summary: dict[str, Any],
        *,
        notes: str = "",
        swap_compare_base: bool = True,
        pool_split: bool = False,
        extra: dict[str, Any] | None = None,
    ) -> None:
        legs = _swap_legs(periods)
        row: dict[str, Any] = {
            "variant_id": variant_id,
            "section": section,
            "hypothesis": hypothesis,
            "mean_excess_pct": summary.get("mean_excess_pct"),
            "swaps_total": summary.get("swaps_total"),
            "n_periods": summary.get("n_periods"),
            "delta_vs_champion_pp": _delta_vs_champion(champ_ex, summary.get("mean_excess_pct")),
            "notes": notes,
        }
        if swap_compare_base:
            cmp = _compare_swap_legs(champ_legs, legs)
            row["swap_leg_match"] = cmp
            if cmp["all_match"]:
                row["notes"] = (notes + " · swap legs identical to champion").strip(" ·")
        if pool_split:
            row["by_signal_pool_size"] = _split_by_pool_size(periods, ctx["fresh_by_date"])
        if extra:
            row.update(extra)
        variants.append(row)

    # ── 1. A1+B3: buy logic ──
    _record(
        CHAMPION_SCORE_SWAP_C_VARIANT_ID,
        "Baseline · seg_last margin + buy max avg_accel",
        "A1+B3",
        champ_periods,
        champ_summary,
        notes="champion",
        swap_compare_base=False,
    )

    cfg_a_none = _champion_cfg(buy_sort_key=None, variant_id="C18acc-buy-none")
    p_a, s_a = _run_variant(conn, ctx, cfg_a_none, kbar_cache)
    _record(
        "C18acc-buy-none",
        "buy_sort_key=None · pick by sort_key (avg_accel) among margin beats",
        "A1+B3",
        p_a,
        s_a,
    )

    cfg_a_seg = _champion_cfg(buy_sort_key="seg_last", variant_id="C18acc-buy-seg")
    p_as, s_as = _run_variant(conn, ctx, cfg_a_seg, kbar_cache)
    _record(
        "C18acc-buy-seg",
        "buy_sort_key=seg_last · no accel buy rank",
        "A1+B3",
        p_as,
        s_as,
    )

    # Variant B = champion (merged single rule should be equivalent)
    _record(
        "C18acc-buy-merged",
        "Merged rule · max avg_accel among seg_last>threshold (same as champion)",
        "A1+B3",
        champ_periods,
        champ_summary,
        notes="duplicate of champion config",
    )

    # ── 2. C4: fresh vs mono_tier2 ──
    cfg_mono = _champion_cfg(
        candidate_pool="mono_tier2",
        variant_id="C18acc-mono",
        label="mono_tier2 pool · champion accel",
    )
    p_c4, s_c4 = _run_variant(conn, ctx, cfg_mono, kbar_cache)
    _record(
        "C18acc-mono",
        "candidate_pool=mono_tier2 · else champion",
        "C4",
        p_c4,
        s_c4,
        notes=f"prior research ~5.09%; observed {s_c4.get('mean_excess_pct')}%",
    )

    # ── 3. B1: mono_up redundancy ──
    b1_stats = _mono_up_redundancy_stats(conn, ctx)
    tier2_fresh_cal = build_tier2_fresh_no_mono_up_calendar(
        conn, ctx["trade_dates"], close=ctx["close"], bench=ctx["bench"]
    )
    cfg_b1 = _champion_cfg(
        variant_id="C18acc-tier2fresh-nomono",
        label="tier2 fresh · no mono_up · champion accel",
    )
    p_b1, s_b1 = _run_variant(
        conn, ctx, cfg_b1, kbar_cache, fresh_by_date=tier2_fresh_cal
    )
    _record(
        "C18acc-tier2fresh-nomono",
        "tier2 fresh transition without mono_up requirement",
        "B1",
        p_b1,
        s_b1,
        extra={"mono_up_stats": b1_stats},
    )

    # ── 4. A2+A3: confirm_bars & entry ranking ──
    c0_base = next(c for c in DEFAULT_C_SWEEP if c.variant_id == "C0")
    entry_grid: list[tuple[str, str, CVariantConfig]] = [
        ("C18acc-entry-c0", "confirm_bars=1 · scale (baseline)", c0_base),
        (
            "C18acc-entry-cfm0",
            "confirm_bars=0 · scale",
            replace(c0_base, variant_id="cfm0", confirm_bars=0),
        ),
        (
            "C18acc-entry-cfm2",
            "confirm_bars=2 · scale",
            next(c for c in DEFAULT_C_SWEEP if c.variant_id == "C3"),
        ),
        (
            "C18acc-entry-sigseg",
            "signal-day seg_last order · no intraday scale",
            replace(c0_base, variant_id="sigseg", score_mode="signal_seg_last"),
        ),
    ]
    for vid, hyp, ecfg in entry_grid:
        p_e, s_e = _run_variant(
            conn, ctx, champion, kbar_cache, entry_c_config=ecfg
        )
        _record(
            vid,
            hyp,
            "A2+A3",
            p_e,
            s_e,
            pool_split=True,
            notes=f"entry_c: confirm={ecfg.confirm_bars} score_mode={ecfg.score_mode}",
        )

    # ── 5. C3: max_swaps_per_day ──
    cfg_sw2 = _champion_cfg(max_swaps_per_day=2, variant_id="C18acc-sw2")
    p_sw2, s_sw2 = _run_variant(conn, ctx, cfg_sw2, kbar_cache)
    _record(
        "C18acc-sw2",
        "max_swaps_per_day=2 · expect identical to max=1",
        "C3",
        p_sw2,
        s_sw2,
        notes="identical" if s_sw2.get("swaps_total") == champ_swaps else "DIFFERS",
    )

    return {
        "date_start": date_start,
        "date_end": date_end,
        "champion": {
            "variant_id": CHAMPION_SCORE_SWAP_C_VARIANT_ID,
            "mean_excess_pct": champ_ex,
            "swaps_total": champ_swaps,
            "n_periods": champ_summary.get("n_periods"),
            "n_swap_legs": len(champ_legs),
        },
        "b1_mono_up_stats": b1_stats,
        "variants": variants,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="C18acc funnel ablation sweep")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--date-start", default=DATE_START)
    parser.add_argument("--date-end", default=DATE_END)
    parser.add_argument("--out-json", type=Path, default=None)
    args = parser.parse_args(argv)

    conn = connect(args.db)
    try:
        payload = run_ablation(conn, date_start=args.date_start, date_end=args.date_end)
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    out = args.out_json or RESEARCH_RRG / f"{stamp}_c18acc_funnel_ablation.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {out}")
    print(
        f"Champion: {payload['champion']['mean_excess_pct']}% · "
        f"swaps={payload['champion']['swaps_total']} · "
        f"n={payload['champion']['n_periods']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
