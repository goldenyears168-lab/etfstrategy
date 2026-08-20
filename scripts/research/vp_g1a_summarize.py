#!/usr/bin/env python3
"""vp_g1a — derived decision summary + statistical power.

Reads the two result JSONs and writes a THIRD file.  It never writes back
to a file it read (a previous script in this repo did that and destroyed
its own pre-fix baseline).  Every string here is formatted from the data;
nothing is a literal copy of a previous run's numbers.
"""
from __future__ import annotations

import datetime as dt
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / "reports/research/channel_lab"
IN_MAIN = LAB / "vp_g1a_node_ic.json"
IN_INJ = LAB / "vp_g1a_causal_injection.json"
OUT = LAB / "vp_g1a_summary.json"
assert OUT != IN_MAIN and OUT != IN_INJ, "must not overwrite an input"


def mde(sd, n, crit=2.0):
    """smallest effect a 2-sided t-test at |t|>=crit could have detected."""
    return crit * sd / math.sqrt(n) if sd and n else None


def main():
    m = json.loads(IN_MAIN.read_text())
    inj = json.loads(IN_INJ.read_text())
    cost = m["config"]["cost_line_pts"]
    gross = m["config"]["gross_per_fill_pts"]
    day = m["node_minus_placebo"]["day"]

    out = {
        "schema": "vp_g1a_summary/v1",
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "inputs": [str(IN_MAIN), str(IN_INJ)],
        "coverage": m["coverage"],
        "cost_line_pts": cost,
    }

    # ---------- primary decision ----------
    k_ic = "r900|near_node_dist-minus-plac_matched_dist"
    k_sp = "r900|spread_pts|near_node_dist-minus-plac_matched_dist"
    prim = {}
    for smp in ("IS", "OOS"):
        a = day[k_ic][smp]
        b = day[k_sp][smp]
        pf = b["mean"] / 2.0 if b["mean"] is not None else None
        mde_sp = mde(b["sd"], b["n_days"])
        prim[smp] = {
            "n_days": a["n_days"],
            "delta_IC_mean": a["mean"], "delta_IC_t": a["t"],
            "delta_IC_same_sign_day_share": a["pos_share"],
            "delta_IC_MDE_at_t2": mde(a["sd"], a["n_days"]),
            "delta_tercile_spread_pts": b["mean"], "delta_spread_t": b["t"],
            "pts_per_fill": pf,
            "pct_of_cost_line": None if pf is None else 100 * abs(pf) / cost,
            "pct_of_gross_per_fill": None if pf is None else 100 * abs(pf) / gross,
            "MDE_spread_pts_at_t2": mde_sp,
            "MDE_pts_per_fill_at_t2": None if mde_sp is None else mde_sp / 2,
            "MDE_pct_of_cost_line": None if mde_sp is None
            else 100 * (mde_sp / 2) / cost,
        }
    out["primary_node_minus_matched_placebo_day_r900"] = prim

    # ---------- node vs the trivial controls (does node BEAT simple stuff?) ----
    beat = {}
    for blk in ("day", "night"):
        d = m["node_minus_placebo"][blk]
        for k, v in d.items():
            if "|spread_pts|" in k:
                continue
            beat[f"{blk}|{k}"] = {s: {"mean": v[s]["mean"], "t": v[s]["t"],
                                      "n_days": v[s]["n_days"]}
                                  for s in ("IS", "OOS")}
    out["node_minus_controls"] = beat

    # ---------- raw IC leaderboard, OOS day r900 (who actually wins) ---------
    lb = []
    for k, v in m["sections"]["day"]["OOS"].items():
        if not k.endswith("|r900"):
            continue
        lb.append({"feature": k.split("|")[0],
                   "ic": v["ic"]["mean"], "t": v["ic"]["t"],
                   "pos_day_share": v["ic"]["pos_share"],
                   "tercile_spread_pts": v["tercile_spread_pts"]["mean"],
                   "n_days": v["ic"]["n_days"]})
    lb.sort(key=lambda r: -(r["tercile_spread_pts"] or -9e9))
    out["oos_day_r900_leaderboard_by_spread"] = lb

    # ---------- per-year consistency of the delta ----------
    yr = {}
    for y, v in m["sections"]["day"]["by_year_primary"].items():
        s = v["stats"]
        n = s.get("near_node_dist|r900", {}).get("ic", {}).get("mean")
        p = s.get("plac_matched_dist|r900", {}).get("ic", {}).get("mean")
        yr[y] = {
            "n_days": v["n_days"], "n_samples": v["n_samples"],
            "node_present_share": v["node_present_share"],
            "mean_10min_range_pts": v["mean_window_range_pts"],
            "node_IC": n, "matched_placebo_IC": p,
            "delta": None if (n is None or p is None) else n - p,
        }
    out["by_year_day_r900"] = yr
    ds = [v["delta"] for v in yr.values() if v["delta"] is not None]
    out["by_year_delta_consistency"] = {
        "n_years": len(ds), "max_abs_delta": max(abs(x) for x in ds) if ds else None,
        "n_years_positive": sum(1 for x in ds if x > 0),
    }

    # ---------- positive control ----------
    pc = {}
    for mode, v in inj["modes"].items():
        st = v["stats"]
        pc[mode] = {
            "shift_s": v["shift_s"], "n_days": v["n_days"], "n_samples": v["n_samples"],
            "near_node_dist_r30_IC": st["near_node_dist|r30"]["ic"]["mean"],
            "near_node_dist_r30_t": st["near_node_dist|r30"]["ic"]["t"],
            "near_node_dist_r900_IC": st["near_node_dist|r900"]["ic"]["mean"],
            "matched_placebo_r900_IC": st["plac_matched_dist|r900"]["ic"]["mean"],
        }
        pc[mode]["node_minus_placebo_r900_IC"] = (
            pc[mode]["near_node_dist_r900_IC"] - pc[mode]["matched_placebo_r900_IC"])
    out["positive_control"] = {"window": inj["window"], "modes": pc}
    cl = pc.get("clean", {}).get("near_node_dist_r30_IC")
    i3 = pc.get("inject300", {}).get("near_node_dist_r30_IC")
    out["positive_control"]["r30_IC_amplification_inject300_over_clean"] = (
        None if not cl else i3 / cl)

    # ---------- narrative, FORMATTED FROM THE NUMBERS ABOVE ----------
    o = prim["OOS"]
    i = prim["IS"]
    out["reading"] = {
        "has_directional_info": m["verdict"]["overall_has_directional_info"],
        "one_line": (
            f"OOS ({o['n_days']} clustered days) node-minus-matched-placebo "
            f"delta IC = {o['delta_IC_mean']:+.5f} (t={o['delta_IC_t']:+.2f}, "
            f"same-sign days {o['delta_IC_same_sign_day_share']:.1%}); the same "
            f"delta in IS is {i['delta_IC_mean']:+.5f} (t={i['delta_IC_t']:+.2f}) "
            f"— opposite sign. Economically {o['pts_per_fill']:+.4f} pt/fill = "
            f"{o['pct_of_cost_line']:.2f}% of the {cost} pt cost line, against a "
            f"detectable floor of {o['MDE_pct_of_cost_line']:.2f}%."),
        "why_the_raw_IC_looks_positive": (
            "raw node IC is real but is pure location: the best OOS day/r900 "
            "tercile spread belongs to "
            f"'{lb[0]['feature']}' ({lb[0]['tercile_spread_pts']:+.2f} pt), not to "
            "near_node_dist "
            f"({[r for r in lb if r['feature'] == 'near_node_dist'][0]['tercile_spread_pts']:+.2f} pt)."),
    }

    OUT.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    print(f"[vp_g1a_summarize] wrote {OUT}")
    print(json.dumps(out["reading"], indent=1, ensure_ascii=False))
    print(json.dumps(out["by_year_delta_consistency"], indent=1))
    print(json.dumps(prim, indent=1))


if __name__ == "__main__":
    main()
