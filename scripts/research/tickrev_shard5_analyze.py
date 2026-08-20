#!/usr/bin/env python3
"""tickrev_shard_5 — post-run analysis of the interleaved (i mod 5 == 4) shard.

Reads reports/research/channel_lab/tickrev_shard_5.json (produced by
scripts/research/tickrev_t3_runner.py) and emits:
  * per-day net for bar / tick + tick-beats-bar day fraction
  * concentration (ex-topN / ex-worstN) on the shard's own trades
  * fidelity check vs the 837-day full-sample run's per-day numbers
    (interleaving changes the rolling half-width history, so per-day results
     are NOT guaranteed to match the full-sample run -- this quantifies it)
Read-only; writes only the enriched sidecar it is told to.
"""
import json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / "reports/research/channel_lab"

sh = json.loads((LAB / "tickrev_shard_5.json").read_text())
full = json.loads((LAB / "tickrev_t3_full_sample.json").read_text())

req = sh["days"]["requested"]
bd_bar, bd_tick = sh["by_day"]["bar"], sh["by_day"]["tick"]
fb_bar, fb_tick = full["by_day"]["bar"], full["by_day"]["tick"]

days_with = sorted(set(bd_bar) | set(bd_tick))
rows = []
for d in days_with:
    b = bd_bar.get(d, {"n": 0, "net_pts": 0.0})
    t = bd_tick.get(d, {"n": 0, "net_pts": 0.0})
    fbv = fb_bar.get(d, {"n": 0, "net_pts": 0.0})
    ftv = fb_tick.get(d, {"n": 0, "net_pts": 0.0})
    rows.append(dict(day=d, bar_n=b["n"], bar_net=b["net_pts"], tick_n=t["n"], tick_net=t["net_pts"],
                     delta=round(t["net_pts"] - b["net_pts"], 1),
                     full_bar_net=fbv["net_pts"], full_tick_net=ftv["net_pts"],
                     full_bar_n=fbv["n"], full_tick_n=ftv["n"]))

deltas = [r["delta"] for r in rows]
tick_win_days = sum(1 for x in deltas if x > 0)
bar_win_days = sum(1 for x in deltas if x < 0)
tie_days = sum(1 for x in deltas if x == 0)
sd = sorted(deltas)
med = sd[len(sd)//2] if sd else None
tot_delta = round(sum(deltas), 1)
by_abs = sorted(rows, key=lambda r: -abs(r["delta"]))
def ex_top_days(k):
    return round(tot_delta - sum(r["delta"] for r in by_abs[:k]), 1)

# fidelity vs full sample (only days present in the full-sample by_day)
cmp_days = [r for r in rows if r["day"] in fb_bar or r["day"] in fb_tick]
bar_same = sum(1 for r in cmp_days if r["bar_net"] == r["full_bar_net"] and r["bar_n"] == r["full_bar_n"])
tick_same = sum(1 for r in cmp_days if r["tick_net"] == r["full_tick_net"] and r["tick_n"] == r["full_tick_n"])
shard_bar_on_cmp = round(sum(r["bar_net"] for r in cmp_days), 1)
full_bar_on_cmp = round(sum(r["full_bar_net"] for r in cmp_days), 1)
shard_tick_on_cmp = round(sum(r["tick_net"] for r in cmp_days), 1)
full_tick_on_cmp = round(sum(r["full_tick_net"] for r in cmp_days), 1)

out = dict(
    shard="tickrev_shard_5 (interleaved i mod 5 == 4 of sorted(usable_days), n=837)",
    n_days_requested=len(req), n_days_with_trades=len(days_with),
    day_level=dict(
        tick_beats_bar_days=tick_win_days, bar_beats_tick_days=bar_win_days, tie_days=tie_days,
        tick_beats_bar_pct=round(100.0*tick_win_days/len(rows), 1) if rows else None,
        median_daily_delta=med,
        mean_daily_delta=round(sum(deltas)/len(deltas), 2) if deltas else None,
        total_delta=tot_delta,
        total_delta_ex_top1_abs_days=ex_top_days(1),
        total_delta_ex_top3_abs_days=ex_top_days(3),
        total_delta_ex_top5_abs_days=ex_top_days(5),
        total_delta_ex_top10_abs_days=ex_top_days(10),
        largest_abs_delta_days=[dict(day=r["day"], delta=r["delta"]) for r in by_abs[:10]],
    ),
    fidelity_vs_full_sample=dict(
        note=("interleaved sharding rebuilds the rolling half-width history from the shard's own "
              "days only; this is NOT expected to reproduce the full-sample per-day numbers. "
              "Numbers below quantify the drift so the merge step can account for it."),
        n_days_compared=len(cmp_days),
        bar_days_identical=bar_same, tick_days_identical=tick_same,
        shard_bar_net_on_compared_days=shard_bar_on_cmp, full_bar_net_on_same_days=full_bar_on_cmp,
        shard_tick_net_on_compared_days=shard_tick_on_cmp, full_tick_net_on_same_days=full_tick_on_cmp,
        bar_net_drift=round(shard_bar_on_cmp - full_bar_on_cmp, 1),
        tick_net_drift=round(shard_tick_on_cmp - full_tick_on_cmp, 1),
    ),
    per_day=rows,
)
p = LAB / "tickrev_shard_5_dayview.json"
p.write_text(json.dumps(out, indent=1, ensure_ascii=False))
print(json.dumps({k: v for k, v in out.items() if k != "per_day"}, indent=1, ensure_ascii=False))
print("->", p)
