#!/usr/bin/env python3
"""RP-2 structural pyramid · chunked collector/driver for 45s-capped sandbox.

The sandbox kills every process when a bash call ends, so the expensive raw
leg collection (``collect_abc_v3_f1_raw_legs``) is done in date chunks with a
pickle checkpoint; the study itself runs in ``--mode finalize``.

  # repeat until REMAINING=0
  PYTHONPATH=src timeout 43 python3 scripts/research/rp2_pyramid_chunked_collect.py --mode collect
  # then
  PYTHONPATH=src python3 scripts/research/rp2_pyramid_chunked_collect.py --mode finalize
"""

from __future__ import annotations

import argparse
import json
import pickle
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
assert (ROOT / "src").exists(), f"unexpected ROOT resolution: {ROOT}"

from report_paths import RESEARCH_RRG  # noqa: E402
from research.backtest.abc_v3_f1_entry_gate_tp_sweep import (  # noqa: E402
    collect_abc_v3_f1_raw_legs,
)
from research.backtest.abc_v3_f1_structural_pyramid_study import (  # noqa: E402
    render_structural_pyramid_md,
    run_structural_pyramid_study,
)
from research.backtest.finpilot_local_backtest import load_price_panels  # noqa: E402
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402

CKPT = ROOT / "logs" / "rp2_pyramid_legs_ckpt.pkl"
SLOT_CACHE = ROOT / "reports" / "research" / "rrg" / "20260709_abc_f1_legs_5d_full.json"


def _load_ckpt(date_start: str, date_end: str) -> dict:
    if CKPT.exists():
        ck = pickle.loads(CKPT.read_bytes())
        if ck.get("window") == [date_start, date_end]:
            return ck
        print(f"ckpt window {ck.get('window')} != requested, starting fresh", flush=True)
    return {"window": [date_start, date_end], "done": [], "legs": []}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="RP-2 chunked raw-leg collector / study driver")
    ap.add_argument("--mode", choices=["collect", "finalize", "status"], default="collect")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--date-start", default="2026-01-02")
    ap.add_argument("--date-end", default="2026-07-01")
    ap.add_argument("--hold-days", type=int, default=5)
    ap.add_argument("--budget-seconds", type=float, default=18.0)
    ap.add_argument("--chunk-dates", type=int, default=2)
    ap.add_argument("--min-trigger-n", type=int, default=30)
    ap.add_argument("--min-oos-trigger-n", type=int, default=15)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)
    t0 = time.time()

    ckpt = _load_ckpt(args.date_start, args.date_end)

    if args.mode == "status":
        print(f"done={len(ckpt['done'])} legs={len(ckpt['legs'])} window={ckpt['window']}")
        return 0

    conn = connect(args.db)
    try:
        close, _, _ = load_price_panels(conn)
        full_dates = close.index.astype(str).tolist()
        sample_dates = [d for d in full_dates if args.date_start <= d <= args.date_end]
        if not sample_dates:
            print("BLOCKER: no trading dates in window", file=sys.stderr)
            return 1

        if args.mode == "collect":
            done = set(ckpt["done"])
            remaining = [d for d in sample_dates if d not in done]
            while remaining and (time.time() - t0) < args.budget_seconds:
                chunk = remaining[: args.chunk_dates]
                legs = collect_abc_v3_f1_raw_legs(conn, dates=chunk, hold_days=args.hold_days)
                ckpt["legs"].extend(legs)
                ckpt["done"].extend(chunk)
                CKPT.parent.mkdir(parents=True, exist_ok=True)
                CKPT.write_bytes(pickle.dumps(ckpt))
                remaining = remaining[len(chunk):]
                print(
                    f"chunk {chunk[0]}..{chunk[-1]} +{len(legs)} legs "
                    f"(total {len(ckpt['legs'])}) elapsed {time.time()-t0:.0f}s",
                    flush=True,
                )
            print(f"REMAINING={len(remaining)} LEGS={len(ckpt['legs'])}")
            return 0

        # finalize
        legs = ckpt["legs"]
        done = set(ckpt["done"])
        missing = [d for d in sample_dates if d not in done]
        if missing:
            print(f"BLOCKER: {len(missing)} dates not collected yet (e.g. {missing[:3]})", file=sys.stderr)
            return 1
        if not legs:
            print("BLOCKER: no legs in checkpoint", file=sys.stderr)
            return 1
        print(f"finalize: {len(legs)} raw legs · running primary study …", flush=True)
        payload = run_structural_pyramid_study(
            conn,
            legs,
            date_start=sample_dates[0],
            date_end=sample_dates[-1],
            hold_days=args.hold_days,
            min_trigger_n=args.min_trigger_n,
            min_oos_trigger_n=args.min_oos_trigger_n,
        )
        payload["sample_note"] = (
            "primary sample = raw ABC-V3-F1 first-trigger legs (unconstrained universe), "
            f"window {sample_dates[0]}..{sample_dates[-1]} (6 個月；RP-2 建議 2024 起全窗口，"
            "因沙箱 45s/call 運算限制縮短，於報告註明); secondary robustness sample = "
            "slot-ledger legs 2024-01-03..2026-07-02 (20260709_abc_f1_legs_5d_full.json)"
        )

        # secondary robustness: slot-ledger legs over the full 2024-2026 window
        if SLOT_CACHE.exists():
            slot_legs = json.loads(SLOT_CACHE.read_text())["legs"]
            slot_dates = sorted({str(l["entry_date"]) for l in slot_legs})
            print(f"finalize: secondary slot-ledger study · {len(slot_legs)} legs …", flush=True)
            sec = run_structural_pyramid_study(
                conn,
                slot_legs,
                date_start=slot_dates[0],
                date_end=slot_dates[-1],
                hold_days=args.hold_days,
                min_trigger_n=args.min_trigger_n,
                min_oos_trigger_n=args.min_oos_trigger_n,
            )
            payload["secondary_slot_ledger_full_window"] = sec

        stamp = date.today().strftime("%Y%m%d")
        out_json = args.out or RESEARCH_RRG / f"{stamp}_abc_v3_f1_structural_pyramid.json"
        out_md = out_json.with_suffix(".md")
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

        md = render_structural_pyramid_md(payload)
        sec = payload.get("secondary_slot_ledger_full_window")
        if sec:
            extra = [
                "",
                "## 穩健性：slot-ledger 全窗口樣本（2024-01-03 .. 2026-07-02，404 legs）",
                "",
                f"- 樣本：既有槽位限制 ledger legs（`20260709_abc_f1_legs_5d_full.json`），"
                f"非 raw first-trigger；verdict（同一套 IS→OOS 準則）：**{sec.get('verdict')}** · "
                f"winner `{sec.get('winner_id')}`",
            ]
            for c in sec.get("criteria") or []:
                extra.append(f"- {'✅' if c.get('passed') else '❌'} {c.get('criterion')} — {c.get('detail')}")
            md += "\n".join(extra) + "\n"
        note = payload.get("sample_note")
        if note:
            md += f"\n> 樣本註記：{note}\n"
        out_md.write_text(md, encoding="utf-8")

        print(f"verdict={payload.get('verdict')} winner={payload.get('winner_id')}")
        for c in payload.get("criteria") or []:
            print(f"  [{'PASS' if c.get('passed') else 'FAIL'}] {c.get('criterion')} — {c.get('detail')}")
        print(f"Wrote {out_json}\nWrote {out_md}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
