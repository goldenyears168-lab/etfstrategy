#!/usr/bin/env python3
"""tickrev_shard_2 — post-run analysis of the interleaved (i%5==1) shard.

Reads reports/research/channel_lab/tickrev_shard_2.json (produced verbatim by
scripts/research/tickrev_t3_runner.py, engine unmodified) and reports:
  * bar/tick summary, per-day net, tick-beats-bar day share
  * right-tail (ex-topN / ex-worstN) concentration
  * shard-vs-full-sample per-day fidelity: how much the interleaved sharding
    itself perturbs the rolling half-width history (NOT a subset of full sample)
  * volatility-stratum and yearly delta decomposition
Usage: PYTHONPATH=src .venv/bin/python scripts/research/tickrev_shard_2_analyze.py
"""
import json, statistics as st
from collections import Counter, defaultdict
from pathlib import Path

LAB = Path("/Users/jackm4/goldenstocks/reports/research/channel_lab")
S = json.loads((LAB / "tickrev_shard_2.json").read_text())
F = json.loads((LAB / "tickrev_t3_full_sample.json").read_text())
C = json.loads((LAB / "tickrev_t3_coverage.json").read_text())

days = S["days"]["requested"]
dayset = set(days)
vb = {d: C["days"][d].get("vol_bucket") for d in days if d in C["days"]}

print("== shard 2 (interleaved i%5==1 of 837 usable days) ==")
print("n_requested", S["days"]["n_requested"], "warmup", S["days"]["n_warmup"],
      "days_with_bars", S["days"]["n_days_with_trades_or_bars"])
print("missing_or_empty", S["days"]["missing_or_empty"])
print("no_session", S["days"]["no_session"])
print("lock_bar_signals", S["causal_lock_check"])
for k in ("bar_level", "tick_level"):
    b = S[k]
    print(f"\n[{k}] n={b['n_trades']} net={b['net_pts']} avg={b['avg_pts']} "
          f"wr={b['win_rate_pct']}% day={b['day_net']} night={b['night_net']}")
    print("  by_exit_reason:", json.dumps(b["by_exit_reason"], ensure_ascii=False))
    print("  touch_resolution:", json.dumps(b["touch_resolution"], ensure_ascii=False))
    print("  accounting:", json.dumps(b["accounting"], ensure_ascii=False))
print("\ndelta:", json.dumps(S["delta"], ensure_ascii=False))
print("right_tail bar:", json.dumps(S["right_tail"]["bar"], ensure_ascii=False))
print("right_tail tick:", json.dumps(S["right_tail"]["tick"], ensure_ascii=False))
print("entry lag:", json.dumps(S["tick_level"]["entry_signal_to_fill_lag_sec"], ensure_ascii=False))
print("secs saved:", json.dumps(S["tick_level"]["entry_seconds_saved_vs_bar_next_open"], ensure_ascii=False))

bd, td = S["by_day"]["bar"], S["by_day"]["tick"]
alld = sorted(set(bd) | set(td))
rows = []
for d in alld:
    bn = bd.get(d, {}).get("net_pts", 0.0); tn = td.get(d, {}).get("net_pts", 0.0)
    rows.append((d, bd.get(d, {}).get("n", 0), bn, td.get(d, {}).get("n", 0), tn, round(tn - bn, 1)))
print(f"\n== per-day ({len(rows)} days with trades of {len(days)} requested) ==")
wins = sum(1 for r in rows if r[5] > 0); losses = sum(1 for r in rows if r[5] < 0)
ties = len(rows) - wins - losses
deltas = [r[5] for r in rows]
print(f"tick>bar days {wins}  bar>tick {losses}  tie {ties}  -> tick win share "
      f"{100*wins/len(rows):.1f}%  (excl ties {100*wins/max(1,wins+losses):.1f}%)")
print(f"delta per day: median {st.median(deltas)} mean {round(sum(deltas)/len(deltas),2)}")
ex = sorted(rows, key=lambda r: abs(r[5]), reverse=True)
for k in (1, 3, 5, 10):
    print(f"  total delta ex top-{k} |delta| days: {round(sum(deltas)-sum(r[5] for r in ex[:k]),1)}")
print("  biggest |delta| days:", [(r[0], r[5]) for r in ex[:10]])

# fidelity vs full sample on the SAME dates
fb, ft = F["by_day"]["bar"], F["by_day"]["tick"]
diff_b = [d for d in alld if round(bd.get(d, {}).get("net_pts", 0.0), 1) != round(fb.get(d, {}).get("net_pts", 0.0), 1)]
diff_t = [d for d in alld if round(td.get(d, {}).get("net_pts", 0.0), 1) != round(ft.get(d, {}).get("net_pts", 0.0), 1)]
fbn = round(sum(fb.get(d, {}).get("net_pts", 0.0) for d in dayset), 1)
ftn = round(sum(ft.get(d, {}).get("net_pts", 0.0) for d in dayset), 1)
print(f"\n== fidelity vs full-sample restricted to the same {len(dayset)} dates ==")
print(f"  full-sample subset: bar net {fbn} / tick net {ftn} / delta {round(ftn-fbn,1)}")
print(f"  this shard        : bar net {S['bar_level']['net_pts']} / tick net {S['tick_level']['net_pts']} / delta {S['delta']['net_pts_delta']}")
print(f"  days differing: bar {len(diff_b)}/{len(alld)}  tick {len(diff_t)}/{len(alld)}")

# strata / year decomposition
for name, keyfn in (("vol_bucket", lambda d: vb.get(d, "?")), ("year", lambda d: d[:4])):
    agg = defaultdict(lambda: [0, 0.0, 0.0])
    for d, bn_, bnet, tn_, tnet, dl in rows:
        a = agg[keyfn(d)]; a[0] += 1; a[1] += bnet; a[2] += tnet
    print(f"\n== by {name} ==")
    for k in sorted(agg):
        n, b_, t_ = agg[k]
        print(f"  {k:12s} days={n:3d} bar={b_:9.1f} tick={t_:9.1f} delta={t_-b_:9.1f}")
