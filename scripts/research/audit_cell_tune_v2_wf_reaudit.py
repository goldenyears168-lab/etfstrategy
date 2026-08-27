#!/usr/bin/env python3
"""Read-only re-audit of the CELL_TUNE_V2_PATCHES "5/5 periods beat v1.2.0" claim.

Reconstructs v1.2.0 (freeze_cell_book + SPECIALIZED_PATCHES only, NO
CELL_TUNE_V2_PATCHES, NO CELL_TUNE_V3_PATCHES) and v1.3.0 (+ CELL_TUNE_V2_PATCHES
only, still no V3) directly from the tuples in src/order/tmf_channel_pv16_book.py,
because specialized_cell_book() itself now bakes in v2 AND v3 (moving target —
re-running the original r_cost_aware_cell_tune_wf.py verbatim today no longer
measures v1.2.0 vs v2, it measures v1.4.0-ish vs v1.4.0-ish).

Also adds BUG-7 exit_reason decomposition per period per book.

Read-only: does not edit src/order/, src/tmf_channel/, config/.
Writes: reports/research/channel_lab/audit_cell_tune_v2_reaudit.json
"""
from __future__ import annotations

import json
import sys
import time
from copy import deepcopy
from pathlib import Path
from collections import Counter
from statistics import mean, median

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / "reports" / "research" / "channel_lab"
sys.path.insert(0, str(LAB))
sys.path.insert(0, str(ROOT / "src"))

import r_cost_aware_cell_tune as ht  # noqa: E402
import r_cost_aware_cell_tune_synthesis as syn  # noqa: E402
from order.tmf_channel_pv16_book import (  # noqa: E402
    freeze_cell_book,
    SPECIALIZED_PATCHES,
    CELL_TUNE_V2_PATCHES,
    RECIPE_VERSION,
)

ov = ht.ov
eng = ht.eng
OUT = LAB / "audit_cell_tune_v2_reaudit.json"


def build_v1_2_0() -> dict:
    book = freeze_cell_book()
    for sess, reg, upd in SPECIALIZED_PATCHES:
        book[sess][reg].update(upd)
    return book


def build_v1_3_0_v2only() -> dict:
    book = build_v1_2_0()
    for sess, reg, upd in CELL_TUNE_V2_PATCHES:
        book[sess][reg].update(deepcopy(upd))
    return book


def run_full_with_exit_reasons(days, tx, vix, gate, v1m, book):
    """Like syn.run_full but also captures exit_reason (t['why']) buckets."""
    ht.set_cost(ht.LIVE)
    cfg = ov.base_recipe(
        hang_anchor="O",
        tick_native=False,
        gap_fill_improve=False,
        session_side_gate=gate,
        hang_lo=15.0,
        hang_hi=30.0,
        night_hang_lo=18.0,
        night_hang_hi=32.0,
        early_fill_gamma=5.0,
        max_hold_bars=25,
        skip_quiet_mode="dry",
        place_every=ht.PLACE_EVERY,
        vixtwn_1m=v1m,
        vixtwn_calib="blend",
        vixtwn_calib_gamma=5.0,
        us_vix_calib="none",
        nq_calib="none",
        session_pv_book=book,
    )
    day_nets = []
    n_trades = 0
    reason_net: dict[str, float] = {}
    reason_n: dict[str, int] = {}
    all_trades = []
    for day in days:
        tr, *_ = eng.simulate(
            *ov.day_arrays(day, tx[day]), cfg, vix_delta=vix, tick_index=None
        )
        day_nets.append(sum(float(t["pnl"]) for t in tr))
        n_trades += len(tr)
        for t in tr:
            why = str(t.get("why") or "")
            # bucket to coarse exit-reason family (strip numeric suffix after |)
            fam = why.split("|", 1)[0] if why else "unknown"
            pnl = float(t["pnl"])
            reason_net[fam] = reason_net.get(fam, 0.0) + pnl
            reason_n[fam] = reason_n.get(fam, 0) + 1
            all_trades.append(dict(day=day, pnl=pnl, why=why, fam=fam, et=t.get("et")))
    book_stats = ht.st(day_nets)
    net_total = book_stats.get("net", 0.0)
    eod_net = reason_net.get("EOD", 0.0)
    eod_n = reason_n.get("EOD", 0)
    net_ex_eod = round(net_total - eod_net, 1)
    return dict(
        book=book_stats,
        n_trades=n_trades,
        exit_reasons={
            fam: dict(n=reason_n[fam], net=round(reason_net[fam], 1))
            for fam in sorted(reason_net)
        },
        eod_n=eod_n,
        eod_net=eod_net,
        net_ex_eod=net_ex_eod,
        headline_is_eod_driven=bool(net_total > 0 and net_ex_eod <= 0),
    )


def main() -> None:
    ht.set_cost(ht.LIVE)
    vix = ht.load_vixtwn_delta()
    v1m = eng.load_vixtwn_1m()

    v1_2_0 = build_v1_2_0()
    v1_3_0 = build_v1_3_0_v2only()

    books = {"v1_2_0_baseline": v1_2_0, "v1_3_0_cell_tune_v2": v1_3_0}

    tx_main = json.loads(ov.TX_PATH.read_text())
    days_main = sorted(tx_main)
    periods = {
        "julsep25": ("cache", LAB / "tx_1m_julsep_holdout_cache.json"),
        "octdec25": ("cache", LAB / "tx_1m_octdec_holdout_cache.json"),
        "janmar26": ("cache", LAB / "tx_1m_janmar_holdout_cache.json"),
        "w83_IS": ("main", days_main[-83:]),
        "w25_IS": ("main", days_main[-25:]),
    }

    out = dict(
        title="Re-audit: CELL_TUNE_V2_PATCHES 5/5-beats-v1.2.0 claim (BUG-1..7 pass)",
        recipe_version_current_live=RECIPE_VERSION,
        cost=ht.LIVE,
        place_every=ht.PLACE_EVERY,
        mode=ht.MODE,
        note="v1_2_0_baseline = freeze_cell_book+SPECIALIZED_PATCHES only. "
        "v1_3_0_cell_tune_v2 = + CELL_TUNE_V2_PATCHES (12 patches), NO v3.",
        results={},
    )
    for pname, (kind, src) in periods.items():
        if kind == "cache":
            if not src.exists():
                print(f"skip {pname}: missing", flush=True)
                continue
            tx = json.loads(src.read_text())
            days = sorted(tx)
        else:
            tx = tx_main
            days = src
        gate = ov.build_nq_gate(days)
        print(f"=== {pname} {days[0]}->{days[-1]} n={len(days)} ===", flush=True)
        block = {}
        for name, book in books.items():
            t0 = time.time()
            r = run_full_with_exit_reasons(days, tx, vix, gate, v1m, book)
            block[name] = r
            print(
                f"  {name:22s} mean={r['book']['mean']:7.1f} net={r['book']['net']:9.1f} "
                f"net_ex_eod={r['net_ex_eod']:9.1f} eod_n={r['eod_n']:3d} eod_net={r['eod_net']:8.1f} "
                f"trades={r['n_trades']} ({round(time.time() - t0, 1)}s)",
                flush=True,
            )
        base_mean = block["v1_2_0_baseline"]["book"]["mean"]
        v2_mean = block["v1_3_0_cell_tune_v2"]["book"]["mean"]
        block["d_mean_v2_vs_v1_2_0"] = round(v2_mean - base_mean, 1)
        out["results"][pname] = block

    verdict = dict(
        periods_improved=sum(
            1 for p in out["results"].values() if p["d_mean_v2_vs_v1_2_0"] > 0
        ),
        n_periods=len(out["results"]),
        per_period_delta={
            p: out["results"][p]["d_mean_v2_vs_v1_2_0"] for p in out["results"]
        },
        per_period_v2_headline_eod_driven={
            p: out["results"][p]["v1_3_0_cell_tune_v2"]["headline_is_eod_driven"]
            for p in out["results"]
        },
    )
    out["verdict"] = verdict
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    print(f"Wrote {OUT}", flush=True)
    print("VERDICT:", json.dumps(verdict, indent=2), flush=True)


if __name__ == "__main__":
    main()
