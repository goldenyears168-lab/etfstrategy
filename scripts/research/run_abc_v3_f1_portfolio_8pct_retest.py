#!/usr/bin/env python3
"""RP-4 · Portfolio 8% retest across windows (gate + multifactor + stock pool).

  PYTHONPATH=src python3 scripts/research/run_abc_v3_f1_portfolio_8pct_retest.py
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from report_paths import RESEARCH_RRG  # noqa: E402
from research.backtest.abc_v3_f1_entry_multifactor_tp_sweep import (  # noqa: E402
    run_entry_multifactor_tp_sweep,
)
from research.backtest.abc_v3_f1_entry_stock_pool_sweep import (  # noqa: E402
    run_entry_stock_pool_sweep,
)
from research.backtest.finpilot_local_backtest import load_price_panels  # noqa: E402
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def _default_windows(full_dates: list[str], hold_days: int) -> list[tuple[str, str, str]]:
    end = full_dates[max(0, len(full_dates) - hold_days - 1)]
    windows: list[tuple[str, str, str]] = [
        ("3m", "2026-04-01", "2026-07-07"),
        ("10m", "2025-09-01", "2026-07-01"),
    ]
    try:
        two_y_start_idx = max(0, len(full_dates) - 504 - hold_days)
        end = full_dates[max(0, len(full_dates) - hold_days - 1)]
        windows.append(("2y", full_dates[two_y_start_idx], end))
    except IndexError:
        pass
    return windows


def _summarize_hits(label: str, payload: dict, *, min_n: int) -> list[str]:
    lines: list[str] = []
    hits = payload.get("cells_hitting_target") or []
    best = payload.get("best_cell") or {}
    if hits:
        lines.append(f"### {label} — **可達** ({len(hits)} cells)")
        for c in hits[:5]:
            lines.append(
                f"- n={c.get('n')} · TP-only **{c.get('tp_only_mean_pct')}%** · "
                f"`{c.get('overlay_label', c.get('pool_label'))}`"
            )
    elif best and not best.get("skipped"):
        lines.append(
            f"### {label} — **未達** · best n={best.get('n')} · "
            f"TP-only **{best.get('tp_only_mean_pct')}%** · "
            f"`{best.get('overlay_label', '')}` `{best.get('pool_label', '')}`"
        )
    else:
        lines.append(f"### {label} — 無可評估 cell")
    return lines


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="RP-4 portfolio 8% retest")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--target", type=float, default=8.0)
    ap.add_argument("--min-leg-n", type=int, default=50)
    ap.add_argument("--hold-days", type=int, default=5)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)

    conn = connect(args.db)
    try:
        close, _, _ = load_price_panels(conn)
        full_dates = close.index.astype(str).tolist()
        windows = _default_windows(full_dates, args.hold_days)

        report: dict = {
            "schema": "abc_v3_f1_portfolio_8pct_retest-v1",
            "generated_at": date.today().isoformat(),
            "target_pct": args.target,
            "min_leg_n": args.min_leg_n,
            "hold_days": args.hold_days,
            "windows": [],
        }
        md_sections: list[str] = [
            "# RP-4 · Portfolio 均報酬 8% 重新檢測",
            "",
            f"目標：**≥{args.target:g}%** TP-only portfolio 均報酬 · n **≥{args.min_leg_n}** · hold **{args.hold_days}d**",
            "",
        ]

        any_hit = False
        for win_id, d0, d1 in windows:
            if d0 > d1:
                continue
            mf = run_entry_multifactor_tp_sweep(
                conn,
                date_start=d0,
                date_end=d1,
                hold_days=args.hold_days,
                target_pct=args.target,
                min_leg_n=args.min_leg_n,
            )
            sp = run_entry_stock_pool_sweep(
                conn,
                date_start=d0,
                date_end=d1,
                hold_days=args.hold_days,
                target_pct=args.target,
                min_leg_n=args.min_leg_n,
            )
            win_payload = {
                "id": win_id,
                "date_start": d0,
                "date_end": d1,
                "multifactor": mf,
                "stock_pool": sp,
            }
            report["windows"].append(win_payload)
            if (mf.get("cells_hitting_target") or []) or (sp.get("cells_hitting_target") or []):
                any_hit = True

            md_sections.append(f"## 窗口 `{win_id}` · {d0} .. {d1}")
            md_sections.append("")
            md_sections.append(
                f"- raw legs **{mf.get('n_raw_legs')}** · base gate **{mf.get('n_base_gate_legs')}**"
            )
            md_sections.extend(_summarize_hits("Multifactor overlay", mf, min_n=args.min_leg_n))
            md_sections.append("")
            md_sections.extend(_summarize_hits("Stock pool × overlay", sp, min_n=args.min_leg_n))
            md_sections.append("")

        md_sections.extend(
            [
                "## 總結",
                "",
                (
                    f"**{'有' if any_hit else '無'}** cell 同時滿足 ≥{args.target:g}% 且 n≥{args.min_leg_n}。"
                ),
                "",
                "引擎：multifactor + stock pool（gap 分解導出之 exclude high gap_w5_w3_eod）",
            ]
        )
        report["any_hit"] = any_hit

    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    out_json = args.out or RESEARCH_RRG / f"{stamp}_abc_v3_f1_portfolio_8pct_retest.json"
    out_md = out_json.with_suffix(".md")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text("\n".join(md_sections), encoding="utf-8")
    print(f"any_hit={any_hit}")
    print(f"Wrote {out_json}\nWrote {out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
