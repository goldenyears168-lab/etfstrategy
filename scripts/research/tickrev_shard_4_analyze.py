#!/usr/bin/env python3
"""tickrev_shard_4 — post-run analysis of the interleaved shard (i mod 5 == 3).

Reads reports/research/channel_lab/tickrev_shard_4.json plus the 837-day
full-sample reference run, and reports:
  * shard summary (bar/tick) and right-tail (ex-topN) robustness
  * per-day net for both engines + the fraction of days tick beats bar
  * shard-vs-full-sample per-day fidelity (interleaving perturbs the rolling
    half-width history, so this is measured, not assumed)
Writes the extra blocks back into the shard JSON under `shard_analysis`.
"""
import json, statistics as st
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / "reports/research/channel_lab"
SH = json.loads((LAB / "tickrev_shard_4.json").read_text())
FULL = json.loads((LAB / "tickrev_t3_full_sample.json").read_text())

days = SH["days"]["requested"]
out = {}

# ---- per-day nets, union of days that traded on either engine ----
bd_b, bd_t = SH["by_day"]["bar"], SH["by_day"]["tick"]
alld = sorted(set(bd_b) | set(bd_t))
per_day = []
for d in alld:
    bn = bd_b.get(d, {}).get("net_pts", 0.0)
    tn = bd_t.get(d, {}).get("net_pts", 0.0)
    per_day.append(dict(day=d, bar_n=bd_b.get(d, {}).get("n", 0), bar_net=bn,
                        tick_n=bd_t.get(d, {}).get("n", 0), tick_net=tn,
                        delta=round(tn - bn, 1)))
deltas = [p["delta"] for p in per_day]
wins = sum(1 for x in deltas if x > 0); losses = sum(1 for x in deltas if x < 0)
ties = sum(1 for x in deltas if x == 0)
out["per_day"] = per_day
out["day_level_delta"] = dict(
    n_trading_days=len(per_day), n_requested_days=len(days),
    n_days_no_trades=len(days) - len(per_day),
    tick_better_days=wins, bar_better_days=losses, tie_days=ties,
    tick_win_rate_pct=round(100.0 * wins / max(1, wins + losses), 2),
    tick_win_rate_pct_incl_ties=round(100.0 * wins / max(1, len(per_day)), 2),
    median_delta=round(st.median(deltas), 1) if deltas else None,
    mean_delta=round(sum(deltas) / len(deltas), 2) if deltas else None,
    sum_delta=round(sum(deltas), 1),
)
# how much of the total delta is decided by a handful of days
srt = sorted(deltas, key=abs, reverse=True)
tot = sum(deltas)
out["day_level_delta"]["sum_delta_ex_top1_abs_day"] = round(tot - srt[0], 1) if srt else None
out["day_level_delta"]["sum_delta_ex_top5_abs_days"] = round(tot - sum(srt[:5]), 1)
out["day_level_delta"]["sum_delta_ex_top10_abs_days"] = round(tot - sum(srt[:10]), 1)

# ---- fidelity vs the 837-day full-sample run, on the same days ----
fb, ft = FULL["by_day"]["bar"], FULL["by_day"]["tick"]
diff_b = diff_t = 0
db_pts = dt_pts = 0.0
mism = []
for d in days:
    a = bd_b.get(d, {}); b = fb.get(d, {})
    c = bd_t.get(d, {}); e = ft.get(d, {})
    an, bn = a.get("net_pts", 0.0), b.get("net_pts", 0.0)
    cn, en = c.get("net_pts", 0.0), e.get("net_pts", 0.0)
    if an != bn:
        diff_b += 1; db_pts += an - bn
    if cn != en:
        diff_t += 1; dt_pts += cn - en
    if an != bn or cn != en:
        mism.append(dict(day=d, shard_bar=an, full_bar=bn, shard_tick=cn, full_tick=en))
sub_b = round(sum(fb.get(d, {}).get("net_pts", 0.0) for d in days), 1)
sub_t = round(sum(ft.get(d, {}).get("net_pts", 0.0) for d in days), 1)
out["fidelity_vs_full_sample"] = dict(
    note="interleaved sharding skips 4/5 of the rolling half-width history; "
         "this block measures the resulting drift instead of assuming none",
    full_sample_same_days_bar_net=sub_b, shard_bar_net=SH["bar_level"]["net_pts"],
    full_sample_same_days_tick_net=sub_t, shard_tick_net=SH["tick_level"]["net_pts"],
    bar_net_drift=round(SH["bar_level"]["net_pts"] - sub_b, 1),
    tick_net_drift=round(SH["tick_level"]["net_pts"] - sub_t, 1),
    n_days_bar_differs=diff_b, n_days_tick_differs=diff_t,
    n_days_compared=len(days), mismatches=mism[:400],
)
SH["shard_analysis"] = out
(LAB / "tickrev_shard_4.json").write_text(json.dumps(SH, indent=1, ensure_ascii=False))

print(json.dumps({k: v for k, v in out.items() if k != "per_day"}, ensure_ascii=False, indent=1)[:3000])
print("bar", {k: SH["bar_level"][k] for k in ("n_trades", "net_pts", "avg_pts", "win_rate_pct", "day_net", "night_net")})
print("tick", {k: SH["tick_level"][k] for k in ("n_trades", "net_pts", "avg_pts", "win_rate_pct", "day_net", "night_net")})
print("lock", SH["causal_lock_check"])
print("rt_bar", SH["right_tail"]["bar"])
print("rt_tick", SH["right_tail"]["tick"])
