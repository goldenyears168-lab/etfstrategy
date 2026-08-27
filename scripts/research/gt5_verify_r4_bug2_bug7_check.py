#!/usr/bin/env python3
"""Independent verifier for the R4-a2-gamma-retest claim
(gt5_r4_nq_early_gamma_day_normal_test.py / _result.json).

Checks NOT present anywhere in the reviewed artifacts:
  (BUG-7) top1/top2 tail-dominance decomposition on the octdec25
          SIGNIFICANT NEGATIVE result and on the pooled-OOS k=0.3
          SIGNIFICANT NEGATIVE grid point -- the claim cites specific
          top1=20%/top2=40% (octdec25) and top1=9%/top2=18% (pooled k=0.3)
          numbers, but no script in the repo computes them for THIS test.
  (BUG-2) same-root/self-loop look-ahead perturbation test on the exact
          production path this R4 script drives (nq_signal.futures_overnight_at
          via patch_nq_gate_for_backfill's wide bundle) -- the claim's own
          integrity_checks list does not claim to have done this at all for
          this specific test (a separate R5 script did a generic version on
          a different bundle).

Read-only verification. Does not touch config/strategy*.yaml, src/order/*,
src/tmf_channel/*, launchd/, .env, or any existing gt5_ file. Reuses the
exact functions from gt5_r4_nq_early_gamma_day_normal_test.py by import
(no duplication, no modification of that file).

Writes reports/research/channel_lab/gt5_verify_r4_bug2_bug7_check_result.json
"""
from __future__ import annotations

import json
import sys
from copy import deepcopy
from datetime import timedelta
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / "reports" / "research" / "channel_lab"
sys.path.insert(0, str(LAB))
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts" / "research"))

import gt5_r4_nq_early_gamma_day_normal_test as r4  # noqa: E402
from us_futures_overnight import price_at_or_before  # noqa: E402

OUT = LAB / "gt5_verify_r4_bug2_bug7_check_result.json"


def main() -> None:
    out: dict = {}

    r4.patch_nq_gate_for_backfill(lookback_days=r4.NQ_LOOKBACK_DAYS)

    vix = r4.ht.load_vixtwn_delta()
    v1m = r4.eng.load_vixtwn_1m()
    book_isolated = r4.isolate_day_normal(r4.specialized_cell_book())

    # ---------- reproduce octdec25 control/center delta ----------
    src = LAB / "tx_1m_octdec_holdout_cache.json"
    tx = json.loads(src.read_text())
    days = sorted(tx)
    gate, nq_on_1m = r4.build_continuous_gate_and_nq_on_1m(days, tx)
    cfg_control = r4.make_cfg(gate, v1m, vix, book_isolated, nq_conf=None)
    control_nets = r4.day_nets(days, tx, vix, cfg_control)
    cfg_center = r4.make_cfg(
        gate, v1m, vix, book_isolated,
        nq_conf=dict(
            nq_conf_enable=True, nq_on_1m=nq_on_1m,
            nq_conf_early_k=r4.CENTER_K,
            nq_conf_early_div_bonus=r4.CENTER_DB,
            nq_conf_early_same_penalty=r4.CENTER_SP,
        ),
    )
    center_nets = r4.day_nets(days, tx, vix, cfg_center)
    delta_octdec = [round(t - c, 2) for c, t in zip(control_nets, center_nets)]

    # cross-check reported stats reproduce
    reported = json.loads(
        (LAB / "gt5_r4_nq_early_gamma_day_normal_test_result.json").read_text()
    )["periods"]["octdec25"]
    my_stats = r4.full_stats(delta_octdec)
    out["octdec25_reproduction"] = dict(
        reported_mean=reported["naive"]["mean"],
        my_mean=my_stats["naive"]["mean"],
        reported_hac_p_maxlag5=reported["hac_p_by_maxlags"]["5"],
        my_hac_p_maxlag5=my_stats["hac_p_by_maxlags"]["5"],
        match=abs(reported["naive"]["mean"] - my_stats["naive"]["mean"]) < 0.01,
    )

    # ---------- BUG-7: top1/top2 tail-dominance, octdec25 ----------
    neg_sum = sum(v for v in delta_octdec if v < 0)
    sorted_by_val = sorted(zip(days, delta_octdec), key=lambda kv: kv[1])  # most negative first
    top1_day, top1_v = sorted_by_val[0]
    top2_v = sorted_by_val[0][1] + sorted_by_val[1][1]
    top1_pct = 100.0 * top1_v / neg_sum if neg_sum else float("nan")
    top2_pct = 100.0 * top2_v / neg_sum if neg_sum else float("nan")
    out["octdec25_topday_check"] = dict(
        neg_sum=round(neg_sum, 2),
        top1_day=top1_day, top1_delta=top1_v, top1_pct_of_neg_sum=round(top1_pct, 1),
        top2_pct_of_neg_sum=round(top2_pct, 1),
        claimed_top1_pct=20, claimed_top2_pct=40,
        claim_matches=abs(top1_pct - 20) < 5 and abs(top2_pct - 40) < 5,
        all_deltas_sorted=sorted_by_val,
    )
    print(f"octdec25 top1={top1_pct:.1f}% top2={top2_pct:.1f}% "
          f"(claimed 20%/40%) match={out['octdec25_topday_check']['claim_matches']}")

    # ---------- BUG-7: top1/top2 tail-dominance, pooled OOS k=0.3 ----------
    pooled_k03 = []
    per_period_extra = {}
    for pname, cache_name in [
        ("julsep25", "tx_1m_julsep_holdout_cache.json"),
        ("octdec25", "tx_1m_octdec_holdout_cache.json"),
        ("janmar26", "tx_1m_janmar_holdout_cache.json"),
    ]:
        if pname == "octdec25":
            p_days, p_tx, p_gate, p_nq1m, p_control = days, tx, gate, nq_on_1m, control_nets
        else:
            p_src = LAB / cache_name
            p_tx = json.loads(p_src.read_text())
            p_days = sorted(p_tx)
            p_gate, p_nq1m = r4.build_continuous_gate_and_nq_on_1m(p_days, p_tx)
            p_cfg_control = r4.make_cfg(p_gate, v1m, vix, book_isolated, nq_conf=None)
            p_control = r4.day_nets(p_days, p_tx, vix, p_cfg_control)
        cfg_k03 = r4.make_cfg(
            p_gate, v1m, vix, book_isolated,
            nq_conf=dict(
                nq_conf_enable=True, nq_on_1m=p_nq1m,
                nq_conf_early_k=0.3,
                nq_conf_early_div_bonus=r4.CENTER_DB,
                nq_conf_early_same_penalty=r4.CENTER_SP,
            ),
        )
        k03_nets = r4.day_nets(p_days, p_tx, vix, cfg_k03)
        p_delta = [round(t - c, 2) for c, t in zip(p_control, k03_nets)]
        pooled_k03.extend((pname, d, v) for d, v in zip(p_days, p_delta))
        per_period_extra[pname] = p_delta

    neg_sum_pooled = sum(v for _, _, v in pooled_k03 if v < 0)
    sorted_pooled = sorted(pooled_k03, key=lambda kv: kv[2])
    top1p = sorted_pooled[0]
    top2p_v = sorted_pooled[0][2] + sorted_pooled[1][2]
    top1p_pct = 100.0 * top1p[2] / neg_sum_pooled if neg_sum_pooled else float("nan")
    top2p_pct = 100.0 * top2p_v / neg_sum_pooled if neg_sum_pooled else float("nan")
    out["pooled_k03_topday_check"] = dict(
        n=len(pooled_k03), neg_sum=round(neg_sum_pooled, 2),
        top1=[top1p[0], top1p[1], top1p[2]], top1_pct_of_neg_sum=round(top1p_pct, 1),
        top2_pct_of_neg_sum=round(top2p_pct, 1),
        claimed_top1_pct=9, claimed_top2_pct=18,
        claim_matches=abs(top1p_pct - 9) < 5 and abs(top2p_pct - 18) < 5,
        top5=[(d, dd, v) for d, dd, v in sorted_pooled[:5]],
    )
    print(f"pooled k=0.3 top1={top1p_pct:.1f}% top2={top2p_pct:.1f}% "
          f"(claimed 9%/18%) match={out['pooled_k03_topday_check']['claim_matches']}")

    # ---------- BUG-2: perturbation test on THIS test's own bundle ----------
    bundle = r4.nq_gate_mod.get_cached(
        "nq_futures_1h", 1800.0, r4.nq_gate_mod._load_futures_bundle
    )
    nq_1h, es_1h, nq_d, es_d, us_dates = bundle
    idx = nq_1h.index
    pick_pos = len(idx) // 2
    ts0 = idx[pick_pos]
    settle = ts0 + timedelta(hours=1)
    inside_query = ts0 + timedelta(minutes=30)  # strictly inside forming window
    after_query = settle + timedelta(minutes=5)  # strictly after settle

    before = dict(
        inside=float(price_at_or_before(nq_1h, inside_query, min_age=timedelta(hours=1))),
        after=float(price_at_or_before(nq_1h, after_query, min_age=timedelta(hours=1))),
    )
    nq_1h_perturbed = nq_1h.copy()
    nq_1h_perturbed.loc[ts0] = float(nq_1h_perturbed.loc[ts0]) + 987.65
    after_p = dict(
        inside=float(price_at_or_before(nq_1h_perturbed, inside_query, min_age=timedelta(hours=1))),
        after=float(price_at_or_before(nq_1h_perturbed, after_query, min_age=timedelta(hours=1))),
    )
    out["bug2_perturbation_check"] = dict(
        bar_ts=str(ts0), settle_ts=str(settle),
        inside_query=str(inside_query), after_query=str(after_query),
        pre_perturb=before, post_perturb=after_p,
        inside_unchanged=abs(before["inside"] - after_p["inside"]) < 1e-9,
        after_changed_by_delta=abs((after_p["after"] - before["after"]) - 987.65) < 1e-6,
    )
    print(f"BUG-2 perturbation: inside_unchanged={out['bug2_perturbation_check']['inside_unchanged']} "
          f"after_changed_correctly={out['bug2_perturbation_check']['after_changed_by_delta']}")

    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    print(f"\nWrote {OUT}")


if __name__ == "__main__":
    main()
