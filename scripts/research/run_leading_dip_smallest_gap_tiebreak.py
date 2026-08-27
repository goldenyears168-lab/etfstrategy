#!/usr/bin/env python3
"""Transplant test: does dayflip-gapup-short's "smallest qualifying gap wins"
tie-break rule generalize to Leading Dip's multi-candidate day selection?

Leading Dip (research-frozen ``leading_dip`` spec) already has a same-day
multi-candidate rule: ``rank_deepest_excess`` — on days with >1 qualifying
dip candidate, it currently picks the DEEPEST excess (most negative ex0,
i.e. the analog of "largest gap"). This script asks: would picking the
SHALLOWEST qualifying excess (closest to the −4pp threshold, i.e. the
analog of "smallest qualifying gap") have done better on the days where
the two rules disagree on the day's #1 pick?

Read-only DB. Reuses ``collect_leading_dip_events`` (frozen spec, T2
required, structure filters on) rather than re-deriving the signal.

    PYTHONPATH=src .venv/bin/python \
        scripts/research/run_leading_dip_smallest_gap_tiebreak.py
"""

from __future__ import annotations

import json
from pathlib import Path
from statistics import mean, median, pstdev

import pandas as pd

from research.backtest.leading_dip_sleeve_validate import (
    OOS_CUT_DEFAULT,
    START_DEFAULT,
    collect_leading_dip_events,
    rank_deepest_excess,
)
from stock_db import DEFAULT_DB_PATH, connect_ro

OUT_DIR = Path(__file__).resolve().parents[2] / "reports/research/smallest_gap_rule_transplant"


def rank_shallowest_excess(rows):
    """Sort by depth descending (least negative first = closest to threshold)."""
    return sorted(
        rows,
        key=lambda r: (-float(r["ex0"]), str(r.get("sid", "")), str(r.get("minute", ""))),
    )


def _stats(xs: list[float]) -> dict:
    if not xs:
        return dict(n=0, mean=None, med=None, win=None, sd=None)
    sd = pstdev(xs) if len(xs) > 1 else 0.0
    return dict(
        n=len(xs),
        mean=round(mean(xs), 3),
        med=round(median(xs), 3),
        win=round(100 * sum(1 for x in xs if x > 0) / len(xs), 1),
        sd=round(sd, 3),
    )


def main() -> None:
    con = connect_ro(DEFAULT_DB_PATH)
    events, trading = collect_leading_dip_events(
        con, start=START_DEFAULT, oos_cut=OOS_CUT_DEFAULT, apply_structure=True
    )
    print(f"structure events: {len(events)} rows / {events['date'].nunique()} days")

    t2 = events[events["T2"].astype(bool)].copy()
    print(f"T2 events (frozen quality track pool): {len(t2)} rows / {t2['date'].nunique()} days")

    multi_days = []
    current_picks = []
    alt_picks = []
    disagree_rows = []

    for d, g in t2.groupby("date", sort=True):
        rows = g.to_dict("records")
        if len(rows) < 2:
            continue
        multi_days.append(d)
        cur = rank_deepest_excess(rows)[0]
        alt = rank_shallowest_excess(rows)[0]
        current_picks.append(cur)
        alt_picks.append(alt)
        if cur["sid"] != alt["sid"]:
            disagree_rows.append(
                dict(
                    date=d,
                    n_candidates=len(rows),
                    current_sid=cur["sid"],
                    current_ex0=round(cur["ex0"], 3),
                    current_ex3=round(cur["ex3"], 3) if cur["ex3"] == cur["ex3"] else None,
                    alt_sid=alt["sid"],
                    alt_ex0=round(alt["ex0"], 3),
                    alt_ex3=round(alt["ex3"], 3) if alt["ex3"] == alt["ex3"] else None,
                    all_sids=",".join(sorted({r["sid"] for r in rows})),
                    half=cur.get("half"),
                )
            )

    print(f"\nmulti-candidate days (n_candidates>=2, T2 pool): {len(multi_days)}")
    print(f"days where current(deepest) vs alt(shallowest) top-1 pick DIFFERS: {len(disagree_rows)}")

    # Full-sample comparison across all multi-candidate days (same-pick days cancel out)
    cur_all_ex3 = [r["ex3"] for r in current_picks if r["ex3"] == r["ex3"]]
    alt_all_ex3 = [r["ex3"] for r in alt_picks if r["ex3"] == r["ex3"]]

    # Disagreement-only comparison (the actual transplant test)
    dis_df = pd.DataFrame(disagree_rows)
    cur_dis = [r for r in dis_df["current_ex3"].tolist() if r is not None] if len(dis_df) else []
    alt_dis = [r for r in dis_df["alt_ex3"].tolist() if r is not None] if len(dis_df) else []

    # Paired diff (alt - current) on disagreement days only, dropping any NaN pair
    paired = []
    if len(dis_df):
        for _, r in dis_df.iterrows():
            if r["current_ex3"] is not None and r["alt_ex3"] is not None:
                paired.append(r["alt_ex3"] - r["current_ex3"])

    # IS/OOS split on disagreement days
    is_cur = dis_df[dis_df["half"] == "IS"]["current_ex3"].dropna().tolist() if len(dis_df) else []
    is_alt = dis_df[dis_df["half"] == "IS"]["alt_ex3"].dropna().tolist() if len(dis_df) else []
    oos_cur = dis_df[dis_df["half"] == "OOS"]["current_ex3"].dropna().tolist() if len(dis_df) else []
    oos_alt = dis_df[dis_df["half"] == "OOS"]["alt_ex3"].dropna().tolist() if len(dis_df) else []

    report = dict(
        frozen_spec="leading_dip (T2 required · gap3<=-0.80 cap excess<=-4 · structure filters)",
        start=START_DEFAULT,
        oos_cut=OOS_CUT_DEFAULT,
        n_multi_candidate_days=len(multi_days),
        n_disagreement_days=len(disagree_rows),
        current_rule_full_sample=_stats(cur_all_ex3),
        alt_rule_full_sample=_stats(alt_all_ex3),
        current_rule_disagreement_only=_stats(cur_dis),
        alt_rule_disagreement_only=_stats(alt_dis),
        paired_diff_alt_minus_current=_stats(paired),
        is_current=_stats(is_cur),
        is_alt=_stats(is_alt),
        oos_current=_stats(oos_cur),
        oos_alt=_stats(oos_alt),
    )

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "leading_dip_smallest_gap_tiebreak.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if len(dis_df):
        dis_df.to_csv(OUT_DIR / "disagreement_days.csv", index=False)

    print("\n=== Full-sample (every multi-candidate day, both rules score every day) ===")
    print("current (deepest excess):", report["current_rule_full_sample"])
    print("alt     (shallowest excess):", report["alt_rule_full_sample"])

    print("\n=== Disagreement-only (the actual tie-break test) ===")
    print(f"n disagreement days = {len(disagree_rows)}")
    print("current pick ex3:", report["current_rule_disagreement_only"])
    print("alt pick ex3:    ", report["alt_rule_disagreement_only"])
    print("paired diff (alt-current):", report["paired_diff_alt_minus_current"])

    print("\n=== IS/OOS split (disagreement days) ===")
    print("IS  current:", report["is_current"], " IS  alt:", report["is_alt"])
    print("OOS current:", report["oos_current"], " OOS alt:", report["oos_alt"])

    print(f"\n-> {OUT_DIR/'leading_dip_smallest_gap_tiebreak.json'}")


if __name__ == "__main__":
    main()
