#!/usr/bin/env python3
"""tickrev_shard_3 — post-run diagnostics + fidelity check vs the 837-day full-sample run.

Read-only. Adds nothing to the engine; only re-reads the shard JSON and the
existing tickrev_t3_full_sample.json, and writes the extra blocks back into
reports/research/channel_lab/tickrev_shard_3.json (fields: shard_selection,
day_level, fidelity_vs_full_sample, subsample_robustness).
"""
from __future__ import annotations
import json, statistics as st
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / "reports/research/channel_lab"
SH = LAB / "tickrev_shard_3.json"
FULL = LAB / "tickrev_t3_full_sample.json"
COV = LAB / "tickrev_t3_coverage.json"


def pct(x, n):
    return round(100.0 * x / n, 2) if n else None


def main():
    sh = json.loads(SH.read_text())
    full = json.loads(FULL.read_text())
    cov = json.loads(COV.read_text())
    days = sh["days"]["requested"]
    S = set(days)

    bd_bar, bd_tick = sh["by_day"]["bar"], sh["by_day"]["tick"]
    active = sorted(set(bd_bar) | set(bd_tick))
    per_day = []
    for d in active:
        b = bd_bar.get(d, {"n": 0, "net_pts": 0.0})
        t = bd_tick.get(d, {"n": 0, "net_pts": 0.0})
        per_day.append(dict(day=d, vol_bucket=cov["days"][d].get("vol_bucket"),
                            bar_n=b["n"], bar_net=b["net_pts"],
                            tick_n=t["n"], tick_net=t["net_pts"],
                            delta=round(t["net_pts"] - b["net_pts"], 1)))
    deltas = [r["delta"] for r in per_day]
    win = sum(1 for x in deltas if x > 0)
    loss = sum(1 for x in deltas if x < 0)
    tie = sum(1 for x in deltas if x == 0)
    tot = sum(deltas)
    by_abs = sorted(per_day, key=lambda r: -abs(r["delta"]))
    trim = {f"total_delta_ex_top{k}_abs_days": round(tot - sum(r["delta"] for r in by_abs[:k]), 1)
            for k in (1, 3, 5, 10)}

    # per-vol-bucket
    vb = {}
    for r in per_day:
        v = vb.setdefault(r["vol_bucket"], dict(n_days=0, bar_net=0.0, tick_net=0.0, delta=0.0))
        v["n_days"] += 1; v["bar_net"] += r["bar_net"]; v["tick_net"] += r["tick_net"]; v["delta"] += r["delta"]
    vb = {k: {kk: (round(vv, 1) if isinstance(vv, float) else vv) for kk, vv in v.items()}
          for k, v in sorted(vb.items())}

    # per-year
    yr = {}
    for r in per_day:
        y = r["day"][:4]
        v = yr.setdefault(y, dict(n_days=0, bar_net=0.0, tick_net=0.0, delta=0.0))
        v["n_days"] += 1; v["bar_net"] += r["bar_net"]; v["tick_net"] += r["tick_net"]; v["delta"] += r["delta"]
    yr = {k: {kk: (round(vv, 1) if isinstance(vv, float) else vv) for kk, vv in v.items()}
          for k, v in sorted(yr.items())}

    # ---- fidelity: same days pulled out of the 837-day full-sample run ----
    fb, ft = full["by_day"]["bar"], full["by_day"]["tick"]
    f_bar_net = round(sum(v["net_pts"] for d, v in fb.items() if d in S), 1)
    f_bar_n = sum(v["n"] for d, v in fb.items() if d in S)
    f_tick_net = round(sum(v["net_pts"] for d, v in ft.items() if d in S), 1)
    f_tick_n = sum(v["n"] for d, v in ft.items() if d in S)
    diff_days = []
    for d in sorted(S):
        a = (bd_bar.get(d, {}).get("net_pts", 0.0), bd_tick.get(d, {}).get("net_pts", 0.0),
             bd_bar.get(d, {}).get("n", 0), bd_tick.get(d, {}).get("n", 0))
        b = (fb.get(d, {}).get("net_pts", 0.0), ft.get(d, {}).get("net_pts", 0.0),
             fb.get(d, {}).get("n", 0), ft.get(d, {}).get("n", 0))
        if a != b:
            diff_days.append(dict(day=d, shard=dict(bar_net=a[0], tick_net=a[1], bar_n=a[2], tick_n=a[3]),
                                  full_sample=dict(bar_net=b[0], tick_net=b[1], bar_n=b[2], tick_n=b[3])))
    fid = dict(
        note=("交錯抽樣（i%5==2）讓 rolling half-width 歷史只由每 5 天的一天餵養，"
              "與 837 天連續狀態不同 → 本片的逐日結果不等於全樣本切片。這裡直接量化差多少，"
              "供合併時決定要用哪一份。"),
        shard_run=dict(bar_n=sh["bar_level"]["n_trades"], bar_net=sh["bar_level"]["net_pts"],
                       tick_n=sh["tick_level"]["n_trades"], tick_net=sh["tick_level"]["net_pts"],
                       delta=sh["delta"]["net_pts_delta"]),
        same_days_sliced_from_full_sample=dict(bar_n=f_bar_n, bar_net=f_bar_net,
                                               tick_n=f_tick_n, tick_net=f_tick_net,
                                               delta=round(f_tick_net - f_bar_net, 1)),
        n_days_differing=len(diff_days), n_days_compared=len(S),
        differing_days=diff_days,
    )

    sh["shard_selection"] = dict(
        rule="sorted(usable_days)[i] for i % 5 == 2  (interleaved, NOT contiguous)",
        source=str(COV), source_key="usable_days", n_source_days=len(cov["usable_days"]),
        n_shard_days=len(days), shard_label="tickrev_shard_3 (3rd of 5, 0-indexed offset 2)",
        vol_bucket_counts={k: sum(1 for d in days if cov["days"][d].get("vol_bucket") == k)
                           for k in sorted({cov["days"][d].get("vol_bucket") for d in days})},
        roll_ambiguous_days_in_shard=[d for d in cov["contamination_flags"]["roll_ambiguous_days"] if d in S],
        night_partial_days_in_shard=[d for d in cov["contamination_flags"]["night_partial_days"] if d in S],
        days_with_no_trades_either_engine=[d for d in days if d not in set(active)],
    )
    sh["day_level"] = dict(
        n_days_requested=len(days), n_days_with_trades=len(active),
        tick_beats_bar_days=win, bar_beats_tick_days=loss, tie_days=tie,
        tick_beats_bar_pct_of_active_days=pct(win, len(active)),
        delta_median_pt=round(st.median(deltas), 1) if deltas else None,
        delta_mean_pt=round(sum(deltas) / len(deltas), 2) if deltas else None,
        delta_total_pt=round(tot, 1),
        delta_trimmed_by_largest_abs_days=trim,
        by_vol_bucket=vb, by_year=yr,
        per_day=per_day,
    )
    SH.write_text(json.dumps(sh, indent=1, ensure_ascii=False))

    print(f"days requested={len(days)} active={len(active)}")
    print(f"bar  n={sh['bar_level']['n_trades']} net={sh['bar_level']['net_pts']} "
          f"avg={sh['bar_level']['avg_pts']} wr={sh['bar_level']['win_rate_pct']}")
    print(f"tick n={sh['tick_level']['n_trades']} net={sh['tick_level']['net_pts']} "
          f"avg={sh['tick_level']['avg_pts']} wr={sh['tick_level']['win_rate_pct']}")
    print(f"delta={sh['delta']['net_pts_delta']} ({sh['delta']['net_pts_delta_pct_of_bar']}% of bar)")
    print(f"tick beats bar on {win}/{len(active)} days ({pct(win,len(active))}%), median delta="
          f"{sh['day_level']['delta_median_pt']}, trimmed={trim}")
    print(f"lock_bar_signals={sh['causal_lock_check']['n_signal_on_lock_bar']}")
    print(f"fidelity: shard delta={fid['shard_run']['delta']} vs full-sample-slice "
          f"{fid['same_days_sliced_from_full_sample']['delta']}, differing days="
          f"{len(diff_days)}/{len(S)}")
    sh["fidelity_vs_full_sample"] = fid
    SH.write_text(json.dumps(sh, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
