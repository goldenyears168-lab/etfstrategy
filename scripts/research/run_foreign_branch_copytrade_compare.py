#!/usr/bin/env python3
"""外資分點 Copytrade 潛力比較（parallel wrapper over the fair-compare protocol）。

同一份凍結協議（L1 open · H12 · 3 slots · Top-1 · IS 密度配對），但只跑 style=foreign
的分點，並用多行程平行化（單分點約 7 分鐘，序列跑 10 支要 75 分鐘）。

  PYTHONPATH=src .venv/bin/python scripts/research/run_foreign_branch_copytrade_compare.py \\
      --end 2026-08-14 --oos-cut 2025-07-01 --workers 5
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from concurrent.futures import ProcessPoolExecutor
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from research.backtest.branch_copytrade_fair_compare import (  # noqa: E402
    DEFAULT_BRANCHES,
    render_markdown,
    run_fair_compare,
)
from report_paths import REPORTS_RESEARCH  # noqa: E402
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402

FOREIGN = tuple(b for b in DEFAULT_BRANCHES if b.style == "foreign")


def _t_stat(mean_pct, std_pct, n) -> float | None:
    if mean_pct is None or std_pct in (None, 0) or not n or n < 2:
        return None
    return round(float(mean_pct) / (float(std_pct) / math.sqrt(int(n))), 3)


def _one(job: tuple[str, str, str, str, str]) -> dict | None:
    slug, db_path, start, end, oos_cut = job
    spec = next(b for b in DEFAULT_BRANCHES if b.slug == slug)
    conn = connect(Path(db_path))
    try:
        row = conn.execute(
            """
            SELECT COUNT(DISTINCT trade_date) AS d
            FROM stock_broker_branch_daily
            WHERE securities_trader_id = ? AND source = 'finmind'
              AND trade_date >= ? AND trade_date <= ?
            """,
            (spec.trader_id, start, end),
        ).fetchone()
        if int(row["d"] or 0) < 60:
            print(f"SKIP {spec.label} ({spec.trader_id}): {row['d']} tape days")
            return None
        payload = run_fair_compare(
            conn,
            branches=(spec,),
            window_start=start,
            window_end=end,
            oos_cut=oos_cut,
        )
        print(f"done {spec.label}")
        return payload
    finally:
        conn.close()


def _rank_rows(branch_rows: list[dict]) -> list[dict]:
    out = []
    for row in branch_rows:
        dm = row["density_matched"]
        oos, full, is_ = dm["oos"], dm["full"], dm["is"]
        out.append(
            {
                "slug": row["slug"],
                "label": row["label"],
                "style": row["style"],
                "trader_id": row["trader_id"],
                "min_net_amount_m": dm["min_net_amount_m"],
                "is_n_signals": is_.get("n_signals"),
                "is_period_return_pct": is_.get("period_return_pct"),
                "oos_n_cycles": oos.get("n_cycles"),
                "oos_period_return_pct": oos.get("period_return_pct"),
                "oos_mean_return_pct": oos.get("mean_return_pct"),
                "oos_median_return_pct": oos.get("median_return_pct"),
                "oos_std_return_pct": oos.get("std_return_pct"),
                "oos_t_stat": _t_stat(
                    oos.get("mean_return_pct"),
                    oos.get("std_return_pct"),
                    oos.get("n_cycles"),
                ),
                "oos_win_rate_pct": oos.get("win_rate_pct"),
                "oos_alpha_win_rate_pct": oos.get("alpha_win_rate_pct"),
                "oos_alpha_per_cycle": oos.get("alpha_per_cycle"),
                "oos_total_alpha_ntd": oos.get("total_alpha_ntd"),
                "oos_sharpe_like": oos.get("sharpe_like"),
                "oos_positive_month_pct": oos.get("positive_month_pct"),
                "full_n_cycles": full.get("n_cycles"),
                "full_period_return_pct": full.get("period_return_pct"),
                "full_mean_return_pct": full.get("mean_return_pct"),
                "full_alpha_per_cycle": full.get("alpha_per_cycle"),
                "full_t_stat": _t_stat(
                    full.get("mean_return_pct"),
                    full.get("std_return_pct"),
                    full.get("n_cycles"),
                ),
            }
        )
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Foreign branch copytrade compare")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--start", default="2024-07-01")
    ap.add_argument("--end", default=date.today().isoformat())
    ap.add_argument("--oos-cut", default="2025-07-01")
    ap.add_argument("--workers", type=int, default=5)
    ap.add_argument(
        "--branches",
        default=",".join(b.slug for b in FOREIGN),
        help="Comma slugs (default: all style=foreign)",
    )
    args = ap.parse_args()

    wanted = [s.strip() for s in str(args.branches).split(",") if s.strip()]
    jobs = [
        (slug, str(args.db), args.start, args.end, args.oos_cut) for slug in wanted
    ]
    print(f"foreign compare n={len(jobs)} workers={args.workers} "
          f"window={args.start}..{args.end} oos_cut={args.oos_cut}")

    payloads: list[dict] = []
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        for res in ex.map(_one, jobs):
            if res:
                payloads.append(res)
    if not payloads:
        print("ERROR: no branch produced results", file=sys.stderr)
        return 1

    branch_rows = [b for p in payloads for b in p["branches"]]
    ranked = _rank_rows(branch_rows)
    dens_rank = sorted(
        ranked,
        key=lambda r: (
            0 if (r["oos_n_cycles"] or 0) >= 10 else 1,
            -(r["oos_period_return_pct"] if r["oos_period_return_pct"] is not None else -1e18),
        ),
    )
    full_rank = sorted(
        ranked,
        key=lambda r: (
            0 if (r["full_n_cycles"] or 0) >= 10 else 1,
            -(r["full_period_return_pct"] if r["full_period_return_pct"] is not None else -1e18),
        ),
    )
    payload = {
        "protocol": payloads[0]["protocol"],
        "branches": branch_rows,
        "rank_density_matched_oos": dens_rank,
        "rank_density_matched_full": full_rank,
        "generated_on": date.today().isoformat(),
    }

    out = REPORTS_RESEARCH / "foreign-branch-copytrade"
    out.mkdir(parents=True, exist_ok=True)
    stamp = date.today().strftime("%Y%m%d")
    (out / f"{stamp}_foreign_branch_copytrade_compare.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out / f"{stamp}_foreign_branch_copytrade_compare.md").write_text(
        render_markdown(payload), encoding="utf-8"
    )
    print("wrote", out / f"{stamp}_foreign_branch_copytrade_compare.md")

    print("\n=== Density-matched OOS ===")
    for i, r in enumerate(dens_rank, 1):
        print(
            f"{i}. {r['label']}({r['trader_id']}) own={r['min_net_amount_m']:g}M "
            f"is_n={r['is_n_signals']} oos_n={r['oos_n_cycles']} "
            f"oos_ret%={r['oos_period_return_pct']} mean%={r['oos_mean_return_pct']} "
            f"t={r['oos_t_stat']} win%={r['oos_win_rate_pct']} "
            f"alpha/cyc={r['oos_alpha_per_cycle']}"
        )
    print("\n=== Density-matched Full ===")
    for i, r in enumerate(full_rank, 1):
        print(
            f"{i}. {r['label']}({r['trader_id']}) full_n={r['full_n_cycles']} "
            f"full_ret%={r['full_period_return_pct']} t={r['full_t_stat']} "
            f"alpha/cyc={r['full_alpha_per_cycle']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
