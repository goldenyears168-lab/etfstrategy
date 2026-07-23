#!/usr/bin/env python3
"""C18acc unconstrained showcase · all POOL1 signals · no slot cap · no swap."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from project_config import DEFAULT_ETF_CODES, parse_etf_codes
from project_dotenv import load_project_dotenv
from render_c18acc_pool1_showcase_html import (
    build_unconstrained_showcase_payload,
    default_showcase_cache_path,
    load_showcase_cache,
    render_champion_showcase_html,
    save_showcase_cache,
)
from render_rrg_intro_champion_combo_html import render_rrg_intro_champion_combo_html
from render_rrg_universe_html import _load_rrg_trajectories, _load_trading_dates_range
from report_paths import research_html_path
from stock_db import DEFAULT_DB_PATH, connect

DEFAULT_DATE_FROM = "2025-01-02"
DEFAULT_OUTPUT = "20260712_rrg_intro_champion_unconstrained.html"
PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_LIVE_GATE_CACHE = (
    PROJECT_ROOT
    / "reports/research/rrg/20260711_c18acc_avoid_mixed_gate_live_poll5m_2025-01-02_2026-07-09.json"
)


def main(argv: list[str] | None = None) -> int:
    load_project_dotenv()
    ap = argparse.ArgumentParser(
        description="C18acc unconstrained HTML · all signals · no swap"
    )
    ap.add_argument("--date-from", default=DEFAULT_DATE_FROM)
    ap.add_argument("--date-to", default=None)
    ap.add_argument("--etf-codes", default=",".join(DEFAULT_ETF_CODES))
    ap.add_argument("--length", type=int, default=20)
    ap.add_argument("--capital-ntd", type=float, default=10_000.0)
    ap.add_argument("--confirm-bars", type=int, default=2)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--output", type=Path, default=None)
    ap.add_argument("--cache", type=Path, default=None)
    ap.add_argument("--render-only", action="store_true")
    ap.add_argument("--no-save-cache", action="store_true")
    ap.add_argument("--gate-cache", type=Path, default=DEFAULT_LIVE_GATE_CACHE)
    ap.add_argument("--no-avoid-mixed", action="store_true")
    args = ap.parse_args(argv)

    etf_codes = parse_etf_codes(args.etf_codes)
    t0 = time.perf_counter()

    conn = connect(args.db)
    try:
        dates = _load_trading_dates_range(
            conn, date_from=args.date_from, date_to=args.date_to
        )
        if len(dates) < 5:
            raise ValueError(f"need ≥5 trade dates, got {len(dates)}")

        universe_trajectories = _load_rrg_trajectories(
            conn,
            dates=dates,
            etf_codes=etf_codes,
            length=args.length,
            with_close=True,
        )

        if args.render_only:
            cache_path = args.cache or default_showcase_cache_path(
                date_from=dates[0],
                date_to=dates[-1],
            ).with_name(
                f"c18acc_showcase_{dates[0]}_{dates[-1]}_unconstrained_live_aligned.json.gz"
            )
            if not cache_path.exists():
                raise SystemExit(f"cache not found: {cache_path}")
            payload = load_showcase_cache(cache_path)
            mode = "render-only"
        else:
            payload = build_unconstrained_showcase_payload(
                conn,
                dates,
                etf_codes=etf_codes,
                length=args.length,
                capital_ntd=args.capital_ntd,
                avoid_spread_mixed=not args.no_avoid_mixed,
                avoid_mixed_gate_cache=args.gate_cache,
                confirm_bars=args.confirm_bars,
            )
            cache_path = args.cache or default_showcase_cache_path(
                date_from=dates[0],
                date_to=dates[-1],
            ).with_name(
                f"c18acc_showcase_{dates[0]}_{dates[-1]}_unconstrained_live_aligned.json.gz"
            )
            if not args.no_save_cache:
                save_showcase_cache(cache_path, payload)
            mode = "full"

        bundle = payload["bundle"]
        bench_closes = payload["bench_closes"]
        length = int(payload.get("length") or args.length)
    finally:
        conn.close()

    champion_html = render_champion_showcase_html(
        bundle=bundle,
        dates=payload["dates"],
        all_trajectories=universe_trajectories,
        universe_trajectories=universe_trajectories,
        bench_closes=bench_closes,
        length=length,
    )
    combo = render_rrg_intro_champion_combo_html(
        champion_html=champion_html,
        page_title=(
            f"RRG · C18acc 全訊號回測（無槽位 · 無換倉）· "
            f"{dates[0]} → {dates[-1]}"
        ),
    )
    out = args.output or research_html_path("rrg", DEFAULT_OUTPUT)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(combo, encoding="utf-8")

    champ = (bundle.get("tracks") or {}).get("champion") or {}
    legs = champ.get("legs") or []
    meta = champ.get("meta") or {}
    rets = [float(lg["return_pct"]) for lg in legs if lg.get("return_pct") is not None]
    mean_ret = sum(rets) / len(rets) if rets else 0.0
    elapsed = time.perf_counter() - t0
    print(
        f"unconstrained [{mode}]: {dates[0]}→{dates[-1]} · "
        f"legs={len(legs)} · mean_ret={mean_ret:.2f}% · "
        f"mean_excess={meta.get('mean_excess_pct')} · {elapsed:.1f}s"
    )
    if not args.render_only and not args.no_save_cache:
        print(f"Cache: {cache_path}")
    print(f"Wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
