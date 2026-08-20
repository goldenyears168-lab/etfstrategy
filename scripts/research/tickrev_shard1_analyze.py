#!/usr/bin/env python3
"""tickrev_shard_1 — post-analysis of the interleaved (i%5==0) shard.

Reads reports/research/channel_lab/tickrev_shard_1.json (produced by
tickrev_t3_runner.py on the interleaved day list) and enriches it in place with
  * per-day net for bar/tick + "tick beats bar" day fraction,
  * right-tail (ex-topN) already present -- re-checked,
  * a fidelity comparison against the same days inside the existing 837-day
    full-sample run (tickrev_t3_full_sample.json), which quantifies how much
    the INTERLEAVED sampling distorts per-day results (half_hist differs),
  * volatility-quintile mix of the shard vs the full corpus.
Nothing in config/, src/order/ or src/tmf_channel/ is touched.
"""
from __future__ import annotations
import json, statistics as st
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / "reports/research/channel_lab"
SH = LAB / "tickrev_shard_1.json"
FULL = LAB / "tickrev_t3_full_sample.json"
COV = LAB / "tickrev_t3_coverage.json"

sh = json.loads(SH.read_text())
cov = json.loads(COV.read_text())
days = sh["days"]["requested"]
dayset = set(days)

# ---- per-day + win-day fraction ------------------------------------------
bd_bar = sh["by_day"]["bar"]
bd_tick = sh["by_day"]["tick"]
alld = sorted(set(bd_bar) | set(bd_tick))
rows = []
for d in alld:
    b = bd_bar.get(d, {"n": 0, "net_pts": 0.0})
    t = bd_tick.get(d, {"n": 0, "net_pts": 0.0})
    rows.append(dict(day=d, bar_n=b["n"], bar_net=b["net_pts"],
                     tick_n=t["n"], tick_net=t["net_pts"],
                     delta=round(t["net_pts"] - b["net_pts"], 1)))
deltas = [r["delta"] for r in rows]
tick_better = sum(1 for x in deltas if x > 0)
bar_better = sum(1 for x in deltas if x < 0)
ties = sum(1 for x in deltas if x == 0)
srt = sorted(deltas, key=abs, reverse=True)
tot_delta = round(sum(deltas), 1)
ex10 = round(tot_delta - sum(srt[:10]), 1)
ex5 = round(tot_delta - sum(srt[:5]), 1)

daily = dict(
    n_days_requested=len(days),
    n_days_with_any_trade=len(alld),
    n_days_zero_trades=len(days) - len(alld),
    tick_beats_bar_days=tick_better,
    bar_beats_tick_days=bar_better,
    tie_days=ties,
    tick_beats_bar_day_pct=round(100.0 * tick_better / max(1, len(alld)), 1),
    delta_day_median=round(st.median(deltas), 1) if deltas else None,
    delta_day_mean=round(sum(deltas) / len(deltas), 2) if deltas else None,
    delta_total=tot_delta,
    delta_total_ex_abs_top5_days=ex5,
    delta_total_ex_abs_top10_days=ex10,
    biggest_abs_delta_days=[r for r in sorted(rows, key=lambda r: abs(r["delta"]), reverse=True)[:10]],
    per_day=rows,
)

# ---- fidelity vs full-sample run on the SAME days -------------------------
fid = {"note": "same days, but half_hist history differs: shard sees only every 5th day"}
if FULL.exists():
    fu = json.loads(FULL.read_text())
    fb, ft = fu["by_day"]["bar"], fu["by_day"]["tick"]
    fbar_n = sum(v["n"] for d, v in fb.items() if d in dayset)
    fbar_net = round(sum(v["net_pts"] for d, v in fb.items() if d in dayset), 1)
    ftick_n = sum(v["n"] for d, v in ft.items() if d in dayset)
    ftick_net = round(sum(v["net_pts"] for d, v in ft.items() if d in dayset), 1)
    diffdays = sorted({d for d in dayset
                       if round(fb.get(d, {"net_pts": 0.0})["net_pts"], 1) != round(bd_bar.get(d, {"net_pts": 0.0})["net_pts"], 1)
                       or round(ft.get(d, {"net_pts": 0.0})["net_pts"], 1) != round(bd_tick.get(d, {"net_pts": 0.0})["net_pts"], 1)})
    fid.update(
        full_sample_same_days=dict(bar_n=fbar_n, bar_net=fbar_net, tick_n=ftick_n,
                                   tick_net=ftick_net,
                                   delta=round(ftick_net - fbar_net, 1)),
        shard_interleaved=dict(bar_n=sh["bar_level"]["n_trades"], bar_net=sh["bar_level"]["net_pts"],
                               tick_n=sh["tick_level"]["n_trades"], tick_net=sh["tick_level"]["net_pts"],
                               delta=sh["delta"]["net_pts_delta"]),
        n_days_differing=len(diffdays),
        pct_days_differing=round(100.0 * len(diffdays) / len(dayset), 1),
        differing_days=diffdays,
        verdict=("interleaved sharding is NOT loss-free: the rolling half-width history "
                 "is built from every-5th-day legs, so per-day results drift from the "
                 "full-sample truth. Contiguous+warmup120 was shown loss-free; this is not."),
    )

# ---- volatility mix -------------------------------------------------------
cd = cov["days"]
from collections import Counter
mix_shard = Counter(cd[d].get("vol_bucket") for d in days if d in cd)
mix_full = Counter(cd[d].get("vol_bucket") for d in cov["usable_days"] if d in cd)
vol = dict(shard=dict(sorted(mix_shard.items())), corpus=dict(sorted(mix_full.items())),
           note="interleaved sampling keeps the corpus volatility mix (unlike the 13-day sample, 54% q5)")

sh["shard_definition"] = dict(
    rule="sorted(usable_days)[i] for i % 5 == 0 (interleaved, NOT contiguous)",
    n_days=len(days), first=days[0], last=days[-1],
    warmup_days=0,
    warmup_note=("first shard day 2021-12-01 is the corpus' first usable day, so no "
                 "warm-up prefix exists; --warmup-days 0 used."),
)
sh["daily_pnl_and_win_days"] = daily
sh["fidelity_vs_full_sample"] = fid
sh["volatility_mix"] = vol
SH.write_text(json.dumps(sh, indent=1, ensure_ascii=False))
print(json.dumps({k: v for k, v in daily.items() if k not in ("per_day", "biggest_abs_delta_days")}, indent=1))
print(json.dumps({k: v for k, v in fid.items() if k != "differing_days"}, indent=1, ensure_ascii=False))
print(json.dumps(vol, indent=1, ensure_ascii=False))
