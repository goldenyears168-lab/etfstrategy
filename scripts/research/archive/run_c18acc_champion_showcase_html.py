#!/usr/bin/env python3
"""Adopted champion-only showcase · poll_5m · cache + --render-only."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from project_config import DEFAULT_ETF_CODES, parse_etf_codes
from project_dotenv import load_project_dotenv
from render_c18acc_pool1_showcase_html import (
    build_triple_showcase_payload,
    champion_trajectories_from_payload,
    default_showcase_cache_path,
    load_showcase_cache,
    render_champion_showcase_html,
    resolve_showcase_cache_path,
    save_showcase_cache,
)
from render_rrg_universe_html import _load_trading_dates_range
from report_paths import research_html_path
from stock_db import DEFAULT_DB_PATH, connect

DEFAULT_DATE_FROM = "2025-01-02"
DEFAULT_OUTPUT = "20260706_c18acc_champion_showcase_2025.html"


def main(argv: list[str] | None = None) -> int:
    load_project_dotenv()
    ap = argparse.ArgumentParser(
        description="C18acc adopted champion showcase HTML (single track)"
    )
    ap.add_argument("--date-from", default=DEFAULT_DATE_FROM)
    ap.add_argument("--date-to", default=None, help="default: DB latest")
    ap.add_argument("--etf-codes", default=",".join(DEFAULT_ETF_CODES))
    ap.add_argument("--length", type=int, default=20)
    ap.add_argument("--n-slots", type=int, default=3)
    ap.add_argument("--capital-ntd", type=float, default=10_000.0)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--output", type=Path, default=None)
    ap.add_argument(
        "--cache",
        type=Path,
        default=None,
        help="cache file path (default: reports/research/rrg/cache/...)",
    )
    ap.add_argument(
        "--render-only",
        action="store_true",
        help="skip backtest; render HTML from --cache only",
    )
    ap.add_argument(
        "--no-save-cache",
        action="store_true",
        help="on full run, do not write cache file",
    )
    args = ap.parse_args(argv)

    etf_codes = parse_etf_codes(args.etf_codes)
    t0 = time.perf_counter()

    if args.render_only:
        cache_path = resolve_showcase_cache_path(
            date_from=args.date_from,
            date_to=args.date_to,
            explicit=args.cache,
        )
        if not cache_path.exists():
            raise SystemExit(f"cache not found: {cache_path}")
        payload = load_showcase_cache(cache_path)
        dates = payload["dates"]
        bundle = payload["bundle"]
        trajectories = champion_trajectories_from_payload(payload)
        bench_closes = payload["bench_closes"]
        length = int(payload.get("length") or args.length)
        mode = "render-only"
    else:
        conn = connect(args.db)
        try:
            dates = _load_trading_dates_range(
                conn, date_from=args.date_from, date_to=args.date_to
            )
            if len(dates) < 5:
                raise ValueError(f"need ≥5 trade dates, got {len(dates)}")

            payload = build_triple_showcase_payload(
                conn,
                dates,
                etf_codes=etf_codes,
                length=args.length,
                n_slots=args.n_slots,
                capital_ntd=args.capital_ntd,
            )
        finally:
            conn.close()

        cache_path = args.cache or default_showcase_cache_path(
            date_from=dates[0],
            date_to=dates[-1],
        )
        if not args.no_save_cache:
            save_showcase_cache(cache_path, payload)
        bundle = payload["bundle"]
        trajectories = champion_trajectories_from_payload(payload)
        bench_closes = payload["bench_closes"]
        length = args.length
        mode = "full"

    html = render_champion_showcase_html(
        bundle=bundle,
        dates=dates,
        all_trajectories=trajectories,
        bench_closes=bench_closes,
        length=length,
    )
    out = args.output or research_html_path("rrg", DEFAULT_OUTPUT)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    elapsed = time.perf_counter() - t0

    champ = (bundle.get("tracks") or {}).get("champion") or {}
    meta = champ.get("meta") or {}
    print(
        f"champion showcase [{mode}]: {dates[0]} → {dates[-1]} · {len(dates)} TD · "
        f"excess={meta.get('mean_excess_pct')}% · legs={len(champ.get('legs') or [])} · "
        f"{elapsed:.1f}s"
    )
    if not args.render_only and not args.no_save_cache:
        print(f"Cache: {cache_path}")
    print(f"Wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
