#!/usr/bin/env python3
"""Verifier-only diagnostic (2026-08-11): reproduce w25_IS per-day deltas from
gt5_r2_audit_cell_tune_v2_continuous_gate.py's exact methodology and check
top-day concentration (does the SIGNIFICANT POSITIVE result depend on 1-2
outlier days?). Read-only. Does not touch any protected file.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / "reports" / "research" / "channel_lab"
sys.path.insert(0, str(LAB))
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts" / "research"))

import r_cost_aware_cell_tune as ht  # noqa: E402
from order.tmf_channel_pv16_book import (  # noqa: E402
    CELL_TUNE_V2_PATCHES,
    SPECIALIZED_PATCHES,
    freeze_cell_book,
)
from tmf_order_layer_aware_replay import patch_nq_gate_for_backfill  # noqa: E402
from tmf_channel import nq_signal  # noqa: E402
import tmf_channel.nq_gate as nq_gate_mod  # noqa: E402

ov = ht.ov
eng = ht.eng
_TZ = ZoneInfo("Asia/Taipei")


def build_v1_2_0():
    book = freeze_cell_book()
    for sess, reg, upd in SPECIALIZED_PATCHES:
        book[sess][reg].update(upd)
    return book


def build_v1_3_0_v2only():
    from copy import deepcopy

    book = build_v1_2_0()
    for sess, reg, upd in CELL_TUNE_V2_PATCHES:
        book[sess][reg].update(deepcopy(upd))
    return book


def build_continuous_gate(days, tx):
    bundle = nq_gate_mod.get_cached(
        "nq_futures_1h", 1800.0, nq_gate_mod._load_futures_bundle
    )
    nq_1h, es_1h, nq_d, es_d, us_dates = bundle
    gate = {}
    for day in days:
        bars = tx.get(day) or []
        for b in bars:
            t = f"{day}T{b['t']}:00.000+08:00"
            dt_tw = datetime.fromisoformat(t).astimezone(_TZ)
            snap = nq_signal.futures_overnight_at(
                dt_tw, nq_1h=nq_1h, es_1h=es_1h, nq_d=nq_d, es_d=es_d,
                us_dates=us_dates,
            )
            nq = None if snap is None else snap.get("nq_overnight_pct")
            side = nq_signal.bias_side(nq)
            gate[t] = {"up": "L", "down": "S"}.get(side, "none")
    return gate


def day_nets_for_book(days, tx, vix, gate, v1m, book):
    ht.set_cost(ht.LIVE)
    cfg = ov.base_recipe(
        hang_anchor="O", tick_native=False, gap_fill_improve=False,
        session_side_gate=gate, hang_lo=15.0, hang_hi=30.0,
        night_hang_lo=18.0, night_hang_hi=32.0, early_fill_gamma=5.0,
        max_hold_bars=25, skip_quiet_mode="dry", place_every=ht.PLACE_EVERY,
        vixtwn_1m=v1m, vixtwn_calib="blend", vixtwn_calib_gamma=5.0,
        us_vix_calib="none", nq_calib="none", session_pv_book=book,
    )
    out = []
    for day in days:
        tr, *_ = eng.simulate(*ov.day_arrays(day, tx[day]), cfg, vix_delta=vix, tick_index=None)
        out.append(sum(float(t["pnl"]) for t in tr))
    return out


def main():
    patch_nq_gate_for_backfill(lookback_days=440)
    ht.set_cost(ht.LIVE)
    vix = ht.load_vixtwn_delta()
    v1m = eng.load_vixtwn_1m()
    v1_2_0 = build_v1_2_0()
    v1_3_0 = build_v1_3_0_v2only()

    tx_main = json.loads(ov.TX_PATH.read_text())
    days_main = sorted(tx_main)
    for pname, days in [("w25_IS", days_main[-25:]), ("w83_IS", days_main[-83:])]:
        tx = tx_main
        gate = build_continuous_gate(days, tx)
        base_nets = day_nets_for_book(days, tx, vix, gate, v1m, v1_2_0)
        v2_nets = day_nets_for_book(days, tx, vix, gate, v1m, v1_3_0)
        delta = [b - a for a, b in zip(base_nets, v2_nets)]
        total = sum(delta)
        sorted_abs = sorted(zip(days, delta), key=lambda kv: -abs(kv[1]))
        top1 = sorted_abs[0]
        top2_sum = sum(v for _, v in sorted_abs[:2])
        print(f"\n=== {pname} n={len(days)} sum={total:.1f} mean={total/len(days):.2f}")
        print(f"top1 day={top1[0]} delta={top1[1]:.1f} ({100*top1[1]/total:.1f}% of total)")
        print(f"top2 sum={top2_sum:.1f} ({100*top2_sum/total:.1f}% of total)")
        print("top5:", [(d, round(v, 1)) for d, v in sorted_abs[:5]])
        n_pos = sum(1 for v in delta if v > 0)
        print(f"days with delta>0: {n_pos}/{len(delta)}")


if __name__ == "__main__":
    main()
