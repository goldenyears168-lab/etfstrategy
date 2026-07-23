#!/usr/bin/env python3
"""Render 富邦-新店 Copytrade sweep JSON → self-contained HTML report.

Reuses the Yuanta Songjiang HTML renderer (branch_label from JSON).

  PYTHONPATH=src .venv/bin/python scripts/research/render_fubon_xindian_copytrade_html.py
  PYTHONPATH=src .venv/bin/python scripts/research/render_fubon_xindian_copytrade_html.py \\
      --json reports/research/fubon-xindian-copytrade/20260717_fubon_xindian_copytrade_sweep.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts" / "research"))

from report_paths import REPORTS_RESEARCH  # noqa: E402
from render_yuanta_songjiang_copytrade_html import (  # noqa: E402
    CHAMPION_KEYS,
    render_html,
)
from research.backtest.yuanta_songjiang_copytrade_sweep import (  # noqa: E402
    load_champion_trade_ledger,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402

OUT_DIR = REPORTS_RESEARCH / "fubon-xindian-copytrade"
DEFAULT_TRADER = "9661"


def _latest_json(dir_path: Path) -> Path | None:
    hits = sorted(
        dir_path.glob("*_fubon_xindian_copytrade_sweep.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return hits[0] if hits else None


def main() -> int:
    ap = argparse.ArgumentParser(description="Render 富邦-新店 copytrade sweep HTML")
    ap.add_argument("--json", type=Path, default=None)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--no-trades", action="store_true")
    ap.add_argument("--no-volume-filter", action="store_true")
    args = ap.parse_args()

    src = args.json or _latest_json(OUT_DIR)
    if src is None or not src.exists():
        print(
            f"ERROR: no sweep JSON under {OUT_DIR}. Run the sweep first.",
            file=sys.stderr,
        )
        return 2
    payload = json.loads(src.read_text(encoding="utf-8"))
    if not payload.get("branch_label"):
        payload["branch_label"] = "富邦-新店"
    out = args.out or src.with_suffix(".html")
    out.parent.mkdir(parents=True, exist_ok=True)

    ledgers: dict[str, dict] = {}
    if not args.no_trades:
        min_vol = None if args.no_volume_filter else 200_000.0
        conn = connect(args.db)
        try:
            champs = payload.get("champions") or {}
            etf_proxy = payload.get("etf_code_proxy") or "FUBON_XD"
            for key, _label in CHAMPION_KEYS:
                ch = champs.get(key)
                if not ch:
                    continue
                ch_replay = dict(ch)
                ch_replay["etf_code_proxy"] = etf_proxy
                print(f"replay trades: {key} {ch_replay.get('strategy_id')} …")
                ledgers[key] = load_champion_trade_ledger(
                    conn,
                    ch_replay,
                    trader_id=str(payload.get("trader_id") or DEFAULT_TRADER),
                    window_start=str(payload.get("window_start")),
                    window_end=str(payload.get("window_end")),
                    min_avg_volume_shares=min_vol,
                )
                print(
                    f"  legs={ledgers[key]['meta']['n_trade_legs']} "
                    f"cycles={ledgers[key]['meta']['n_cycles']}"
                )
        finally:
            conn.close()
        trades_path = out.with_name(out.stem + "_trades.json")
        trades_path.write_text(
            json.dumps(ledgers, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"wrote {trades_path}")

    html_doc = render_html(
        payload, source_name=src.name, champion_ledgers=ledgers
    )
    out.write_text(html_doc, encoding="utf-8")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
