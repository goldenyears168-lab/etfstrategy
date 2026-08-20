#!/usr/bin/env python3
"""tickrev_shard_2 — append analysis + sharding-fidelity warning blocks to
reports/research/channel_lab/tickrev_shard_2.json (in place, additive only:
every key written by scripts/research/tickrev_t3_runner.py is left untouched).

The control is T3's 837-day full-sample run restricted to the same 168 dates:
there each day's rolling half-width history comes from the *immediately
preceding* usable days (causally what a live system would have). In this shard
the history comes from every-5th day, i.e. a ~200-calendar-day window instead of
40 -- a different channel-width baseline, so the shard is NOT a subset of the
full-sample result and shards must NOT be summed to rebuild it.
Usage: PYTHONPATH=src .venv/bin/python scripts/research/tickrev_shard_2_augment.py
"""
import json, statistics as st
from collections import defaultdict
from pathlib import Path

LAB = Path("/Users/jackm4/goldenstocks/reports/research/channel_lab")
p = LAB / "tickrev_shard_2.json"
S = json.loads(p.read_text())
F = json.loads((LAB / "tickrev_t3_full_sample.json").read_text())
C = json.loads((LAB / "tickrev_t3_coverage.json").read_text())

days = S["days"]["requested"]; dset = set(days)
sb, stk = S["by_day"]["bar"], S["by_day"]["tick"]
fb, ft = F["by_day"]["bar"], F["by_day"]["tick"]
trad = sorted(set(sb) | set(stk))


def perday(bd, td, ds):
    return [dict(day=d, bar_n=bd.get(d, {}).get("n", 0), bar_net=bd.get(d, {}).get("net_pts", 0.0),
                 tick_n=td.get(d, {}).get("n", 0), tick_net=td.get(d, {}).get("net_pts", 0.0),
                 delta=round(td.get(d, {}).get("net_pts", 0.0) - bd.get(d, {}).get("net_pts", 0.0), 1))
            for d in ds]


rows = perday(sb, stk, trad)
frows = perday(fb, ft, trad)


def dayblock(rr):
    dl = [r["delta"] for r in rr]
    w = sum(1 for x in dl if x > 0); l = sum(1 for x in dl if x < 0)
    ex = sorted(rr, key=lambda r: -abs(r["delta"]))
    tot = round(sum(dl), 1)
    return dict(n_days_with_trades=len(rr), tick_better_days=w, bar_better_days=l,
                tie_days=len(rr) - w - l,
                tick_better_share_pct=round(100.0 * w / max(1, w + l), 1),
                delta_median_per_day=st.median(dl), delta_mean_per_day=round(sum(dl) / len(dl), 2),
                delta_total=tot,
                delta_total_ex_topN_abs_days={f"ex_top{k}": round(tot - sum(r["delta"] for r in ex[:k]), 1)
                                              for k in (1, 3, 5, 10)},
                biggest_abs_delta_days=[(r["day"], r["delta"]) for r in ex[:10]])


vb = {d: C["days"][d].get("vol_bucket") for d in days if d in C["days"]}
def decomp(keyfn):
    agg = defaultdict(lambda: [0, 0.0, 0.0])
    for r in rows:
        a = agg[keyfn(r["day"])]; a[0] += 1; a[1] += r["bar_net"]; a[2] += r["tick_net"]
    return {k: dict(days=v[0], bar_net=round(v[1], 1), tick_net=round(v[2], 1),
                    delta=round(v[2] - v[1], 1)) for k, v in sorted(agg.items())}


fb_net = round(sum(fb.get(d, {}).get("net_pts", 0.0) for d in dset), 1)
ft_net = round(sum(ft.get(d, {}).get("net_pts", 0.0) for d in dset), 1)
pert = [(d, round(sb.get(d, {}).get("net_pts", 0.0) - fb.get(d, {}).get("net_pts", 0.0), 1),
         round(stk.get(d, {}).get("net_pts", 0.0) - ft.get(d, {}).get("net_pts", 0.0), 1)) for d in trad]
nz = [x for x in pert if x[1] or x[2]]

S["sharding_scheme"] = dict(
    shard_id=2, rule="sorted(usable_days)[i] for i % 5 == 1 (interleaved, 1-of-5)",
    n_days=len(days), source="reports/research/channel_lab/tickrev_t3_coverage.json:usable_days (n=837)",
    day_list_file="reports/research/channel_lab/tickrev_shard_2_days.json",
    warmup_days_requested=120, warmup_days_actually_available=S["days"]["n_warmup"],
    warmup_note=("first shard day 2021-12-02 is the 2nd usable day of the whole corpus, so only "
                 "1 warm-up day exists; the full-sample run also starts cold there, so warm-up is "
                 "NOT the source of the divergence below"),
    engine_modified=False,
    vol_bucket_counts={k: sum(1 for d in days if vb.get(d) == k)
                       for k in ("q1_calmest", "q2", "q3", "q4", "q5_wildest")},
    contamination_in_shard=dict(
        roll_ambiguous_days=len([d for d in C["contamination_flags"]["roll_ambiguous_days"] if d in dset]),
        night_partial_days=len([d for d in C["contamination_flags"]["night_partial_days"] if d in dset])),
)

S["sharding_fidelity_warning"] = dict(
    severity="high",
    claim=("interleaved (1-of-5) sharding changes the answer's SIGN; this shard's totals are an "
           "artifact of the sharding scheme, not a property of the 168 dates"),
    mechanism=("the engine's channel half-width baseline is a rolling percentile over the last "
               "WINDOW=40 *processed* days. Feeding every 5th day makes that window span ~200 "
               "calendar days, so the same date gets a different width threshold, different touches "
               "and different fills than under consecutive-day history."),
    control=("T3 full-sample run (837 consecutive usable days) restricted to these same 168 dates"),
    control_full_sample_subset=dict(bar_net=fb_net, tick_net=ft_net, delta=round(ft_net - fb_net, 1),
                                    bar_n=sum(fb.get(d, {}).get("n", 0) for d in dset),
                                    tick_n=sum(ft.get(d, {}).get("n", 0) for d in dset),
                                    per_day=dayblock(frows)),
    this_shard=dict(bar_net=S["bar_level"]["net_pts"], tick_net=S["tick_level"]["net_pts"],
                    delta=S["delta"]["net_pts_delta"], bar_n=S["bar_level"]["n_trades"],
                    tick_n=S["tick_level"]["n_trades"]),
    days_with_trades_identical=len(trad) == len([d for d in dset if d in fb or d in ft]),
    n_days_perturbed=len(nz), n_days_with_trades=len(trad),
    median_abs_perturbation_pt=dict(bar=st.median([abs(x[1]) for x in nz]),
                                    tick=st.median([abs(x[2]) for x in nz])),
    do_not=("do NOT sum shard_0..shard_4 to reconstruct the 837-day answer; use "
            "reports/research/channel_lab/tickrev_t3_full_sample.json (47 min single process) or "
            "contiguous shards with --warmup-days 120, which T3 proved bit-exact."),
)

S["shard_analysis"] = dict(
    per_day=rows, per_day_stats=dayblock(rows),
    by_vol_bucket=decomp(lambda d: vb.get(d, "?")), by_year=decomp(lambda d: d[:4]),
    days_requested_without_any_trade=sorted(dset - set(trad)),
    n_days_requested_without_any_trade=len(dset - set(trad)),
    skipped_days=dict(missing_or_empty=S["days"]["missing_or_empty"], no_session=S["days"]["no_session"],
                      note="0 days skipped: all 168 requested days were loaded and simulated"),
)
p.write_text(json.dumps(S, indent=1, ensure_ascii=False, sort_keys=False))
print("augmented", p, p.stat().st_size)
print(json.dumps(S["sharding_fidelity_warning"]["control_full_sample_subset"]["per_day"], ensure_ascii=False)[:400])
print(json.dumps(S["shard_analysis"]["per_day_stats"], ensure_ascii=False)[:400])
