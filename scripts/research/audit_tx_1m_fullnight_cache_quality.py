#!/usr/bin/env python3
"""Audit data quality of `tx_1m_fullnight_cache_full.json` (TX 1-minute bar cache,
day 08:45-13:44 + night 15:00-23:59 + post-midnight tail 00:00-04:59) against raw
FinMind `TaiwanFuturesTick` per-day tick files.

Motivation: `docs/tmf-channel-research-handoff-20260811.md` §5a reported that the
00:00-04:59 tail of this cache deviates from real tick prices by ~1000pt on a few
dates (2026-04-02 00:36, 2026-06-26 01:11, 2026-05-07). This script re-derives that
comparison under TWO date-attribution schemes and scans every bar in the cache:

  * "cal"     — a post-midnight bar belongs to the NEXT calendar day; the cache
                stores that explicitly in each bar's `cal` field.
  * "session" — every bar is stamped with the session date (the cache's dict key).
                This is what `load_arrays()` in the downstream tick-validation
                scripts does (`T = f"{day}T{r['t']}:00.000+08:00"`), so it is the
                scheme under which §5a's numbers were produced.

For each bar the script computes, against the ticks of the same clock minute:
  d_hi  = bar.h - tick_max        d_lo  = bar.l - tick_min
  dev   = max(|d_hi|, |d_lo|)     endpoint deviation
  gap   = max(0, bar.l - tick_max, tick_min - bar.h)
          -> strictly positive only when the bar's range and the tick range are
             *disjoint*, i.e. real contamination rather than rounding/noise.

Contract handling (important — picking the wrong contract manufactures false
positives): the post-midnight bars carry an explicit `contract` label, which is
used directly. Day / pre-midnight bars carry no label, so the script reports both
(a) the deviation against the highest-volume ("front") contract of that minute and
(b) the minimum deviation over every non-spread contract present, together with
which contract achieved it.

Read-only. Modifies nothing: not the cache, not the tick files, not the DB.

Usage:
  PYTHONPATH=src .venv/bin/python scripts/research/audit_tx_1m_fullnight_cache_quality.py
  PYTHONPATH=src .venv/bin/python scripts/research/audit_tx_1m_fullnight_cache_quality.py --limit 5
  PYTHONPATH=src .venv/bin/python scripts/research/audit_tx_1m_fullnight_cache_quality.py \
      --attribution session --segments post_midnight
"""
from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CACHE_PATH = REPO / "reports/research/channel_lab/tx_1m_fullnight_cache_full.json"
TICK_DIR = REPO / "reports/research/channel_lab/finmind_tx_tick_by_day"
OUT_PATH = REPO / "reports/research/channel_lab/audit_tx_1m_fullnight_cache_quality.json"

SEVERE_PTS = 100.0
SUSPECT_PTS = 38.0  # repo's documented worst-case normal bar/tick noise

KNOWN_CASES = [
    # (session_date, hm) reported in handoff §5a
    ("2026-04-02", "00:36"),
    ("2026-06-26", "01:11"),
    ("2026-05-07", "00:00"),
]


# ---------------------------------------------------------------- tick loading
def load_tick_minutes(cal: str) -> dict[str, dict[str, dict]]:
    """cal date -> {contract: {hm: {mn,mx,first,last,n,vol}}}. Spread legs dropped."""
    path = TICK_DIR / f"{cal}.json"
    if not path.exists():
        return {}
    try:
        rows = json.loads(path.read_text())
    except (ValueError, OSError):
        return {}
    agg: dict[str, dict[str, dict]] = defaultdict(dict)
    for r in rows:
        cd = str(r.get("contract_date") or "")
        if "/" in cd or not cd.isdigit():
            continue  # calendar-spread quote, not an outright contract
        ts = str(r.get("date") or "")
        if len(ts) < 16 or " " not in ts:
            continue
        hm = ts[11:16]
        try:
            px = float(r["price"])
            vol = float(r.get("volume") or 0)
        except (TypeError, ValueError, KeyError):
            continue
        if px <= 0:
            continue
        slot = agg[cd].get(hm)
        if slot is None:
            agg[cd][hm] = {
                "mn": px, "mx": px, "first": px, "last": px, "n": 1, "vol": vol,
            }
        else:
            if px < slot["mn"]:
                slot["mn"] = px
            if px > slot["mx"]:
                slot["mx"] = px
            slot["last"] = px
            slot["n"] += 1
            slot["vol"] += vol
    return agg


# ------------------------------------------------------------------- metrics
def compare(bar: dict, tk: dict) -> dict:
    d_hi = float(bar["h"]) - tk["mx"]
    d_lo = float(bar["l"]) - tk["mn"]
    dev = max(abs(d_hi), abs(d_lo))
    gap = max(0.0, float(bar["l"]) - tk["mx"], tk["mn"] - float(bar["h"]))
    return {
        "d_hi": round(d_hi, 1),
        "d_lo": round(d_lo, 1),
        "d_close": round(float(bar["c"]) - tk["last"], 1),
        "dev": round(dev, 1),
        "gap": round(gap, 1),
        "tick_min": tk["mn"],
        "tick_max": tk["mx"],
        "tick_n": tk["n"],
    }


def segment_of(bar: dict) -> str:
    t = bar["t"]
    if t < "05:00":
        return "post_midnight"
    if t < "14:00":
        return "day"
    return "night_pre_midnight"


def front_contract(day_agg: dict[str, dict[str, dict]], hm: str) -> str | None:
    best, best_n = None, -1
    for cd, minutes in day_agg.items():
        slot = minutes.get(hm)
        if slot and slot["n"] > best_n:
            best, best_n = cd, slot["n"]
    return best


# ---------------------------------------------------------------------- main
def run(args) -> dict:
    cache = json.loads(CACHE_PATH.read_text())
    sessions = sorted(cache)
    if args.sessions:
        wanted = set(args.sessions.split(","))
        sessions = [s for s in sessions if s in wanted]
    if args.limit:
        sessions = sessions[: args.limit]

    want_segments = set(args.segments.split(",")) if args.segments else None

    # Group every bar by the calendar date it should be compared against.
    # attribution="cal": post-midnight bar -> its `cal` field (the true calendar day)
    # attribution="session": every bar -> the session key (what load_arrays() assumes)
    by_cal: dict[str, list[tuple[str, dict]]] = defaultdict(list)
    n_bars_total = 0
    for sess in sessions:
        for bar in cache[sess]:
            seg = segment_of(bar)
            if want_segments and seg not in want_segments:
                continue
            n_bars_total += 1
            if args.attribution == "cal":
                cal = bar.get("cal") or sess
            else:
                cal = sess
            by_cal[cal].append((sess, bar))

    findings: list[dict] = []
    no_tick_minutes: list[dict] = []
    missing_tick_files: list[str] = []
    devs_by_seg: dict[str, list[float]] = defaultdict(list)
    gaps_by_seg: dict[str, list[float]] = defaultdict(list)
    n_compared_by_seg: Counter = Counter()
    contract_match: Counter = Counter()

    for cal in sorted(by_cal):
        day_agg = load_tick_minutes(cal)
        if not day_agg:
            missing_tick_files.append(cal)
            for sess, bar in by_cal[cal]:
                no_tick_minutes.append(
                    {"session": sess, "cal": cal, "t": bar["t"],
                     "segment": segment_of(bar), "reason": "no_tick_file"}
                )
            continue
        for sess, bar in by_cal[cal]:
            seg = segment_of(bar)
            hm = bar["t"]
            labeled = bar.get("contract")

            if labeled:
                tk = day_agg.get(labeled, {}).get(hm)
                used_contract, how = labeled, "labeled"
            else:
                fc = front_contract(day_agg, hm)
                tk = day_agg.get(fc, {}).get(hm) if fc else None
                used_contract, how = fc, "front_volume"

            if tk is None:
                no_tick_minutes.append(
                    {"session": sess, "cal": cal, "t": hm, "segment": seg,
                     "contract": used_contract, "reason": "no_ticks_this_minute"}
                )
                continue

            res = compare(bar, tk)
            n_compared_by_seg[seg] += 1
            devs_by_seg[seg].append(res["dev"])
            gaps_by_seg[seg].append(res["gap"])

            # Would another contract have matched better? Distinguishes a genuine
            # cache error from a contract-selection artifact.
            alt_best, alt_dev = used_contract, res["dev"]
            if res["dev"] > SUSPECT_PTS:
                for cd, minutes in day_agg.items():
                    slot = minutes.get(hm)
                    if not slot:
                        continue
                    cand = compare(bar, slot)["dev"]
                    if cand < alt_dev:
                        alt_best, alt_dev = cd, cand
                contract_match[
                    "alt_contract_fixes" if alt_dev <= SUSPECT_PTS else "no_contract_fixes"
                ] += 1

            if res["dev"] > SUSPECT_PTS:
                findings.append({
                    "session": sess,
                    "cal": cal,
                    "t": hm,
                    "segment": seg,
                    "grade": "severe" if res["dev"] > SEVERE_PTS else "suspect",
                    "bar_o": bar["o"], "bar_h": bar["h"],
                    "bar_l": bar["l"], "bar_c": bar["c"], "bar_v": bar.get("v"),
                    "contract_used": used_contract,
                    "contract_source": how,
                    "best_alt_contract": alt_best,
                    "best_alt_dev": round(alt_dev, 1),
                    **res,
                })

    def dist(xs: list[float]) -> dict:
        if not xs:
            return {"n": 0}
        s = sorted(xs)
        return {
            "n": len(s),
            "mean": round(statistics.fmean(s), 3),
            "p50": s[len(s) // 2],
            "p95": s[int(len(s) * 0.95)],
            "p99": s[int(len(s) * 0.99)],
            "max": s[-1],
        }

    findings.sort(key=lambda r: -r["dev"])
    bad_sessions = Counter(f["session"] for f in findings)
    bad_hours = Counter(f["t"][:2] for f in findings)
    bad_segments = Counter(f["segment"] for f in findings)

    n_compared = sum(n_compared_by_seg.values())
    report = {
        "cache": str(CACHE_PATH),
        "tick_dir": str(TICK_DIR),
        "attribution": args.attribution,
        "thresholds": {"severe_pts": SEVERE_PTS, "suspect_pts": SUSPECT_PTS},
        "n_sessions": len(sessions),
        "sessions_range": [sessions[0], sessions[-1]] if sessions else [],
        "n_bars_scanned": n_bars_total,
        "n_bars_compared": n_compared,
        "n_bars_no_tick": len(no_tick_minutes),
        "dev_distribution_by_segment": {k: dist(v) for k, v in devs_by_seg.items()},
        "gap_distribution_by_segment": {k: dist(v) for k, v in gaps_by_seg.items()},
        "n_findings": len(findings),
        "pct_contaminated": round(100.0 * len(findings) / n_compared, 4) if n_compared else None,
        "n_severe": sum(1 for f in findings if f["grade"] == "severe"),
        "n_suspect": sum(1 for f in findings if f["grade"] == "suspect"),
        "findings_by_segment": dict(bad_segments),
        "findings_by_hour": dict(sorted(bad_hours.items())),
        "findings_by_session_top20": bad_sessions.most_common(20),
        "contract_alt_check": dict(contract_match),
        "missing_tick_files": missing_tick_files,
        "no_tick_minutes_sample": no_tick_minutes[:50],
        "n_no_tick_minutes": len(no_tick_minutes),
        "findings": findings if args.full_findings else findings[:500],
        "findings_truncated": (not args.full_findings) and len(findings) > 500,
    }
    return report


def structural_check(sessions: list[str], cache: dict) -> dict:
    """Checks that do NOT depend on the tick files, so a corrupt *source* tick file
    (which would still yield dev=0 in the bar-vs-tick scan) can be caught:
      - is each session's post-midnight `cal` exactly session_date + 1 day?
        (`build_fullnight()` falls back to +2/+3 mornings when the first one has
        <200 bars — that would silently staple the WRONG night onto a session)
      - seam continuity 23:59 close -> 00:00 open
      - largest 1-minute close-to-close jump inside the tail
      - how many of the 300 tail minutes are present
    """
    from datetime import date

    rows = []
    for sess in sessions:
        bars = cache[sess]
        early = [b for b in bars if b.get("cal")]
        pre = [b for b in bars if not b.get("cal") and b["t"] >= "14:00"]
        if not early:
            rows.append({"session": sess, "n_early": 0, "flag": "no_post_midnight"})
            continue
        cals = sorted({b["cal"] for b in early})
        y, m, d = (int(x) for x in sess.split("-"))
        y2, m2, d2 = (int(x) for x in cals[0].split("-"))
        offset = (date(y2, m2, d2) - date(y, m, d)).days
        jumps = [
            (round(abs(early[i]["c"] - early[i - 1]["c"]), 1), early[i]["t"])
            for i in range(1, len(early))
        ]
        mx_jump, mx_at = max(jumps) if jumps else (0.0, "")
        seam = round(early[0]["o"] - pre[-1]["c"], 1) if pre else None
        flags = []
        if offset != 1:
            flags.append(f"cal_offset={offset}d")
        if len(cals) != 1:
            flags.append(f"multi_cal={cals}")
        if seam is not None and abs(seam) > SUSPECT_PTS:
            flags.append(f"seam_gap={seam}")
        rows.append({
            "session": sess,
            "cal": cals[0],
            "cal_offset_days": offset,
            "n_cal_dates": len(cals),
            "n_tail_minutes": len(early),
            "n_tail_minutes_missing_of_300": 300 - len(early),
            "seam_2359c_to_0000o": seam,
            "tail_net_pts": round(early[-1]["c"] - early[0]["o"], 1),
            "max_1m_close_jump": mx_jump,
            "max_1m_close_jump_at": mx_at,
            "flags": flags,
        })
    return {
        "n_sessions": len(rows),
        "n_flagged": sum(1 for r in rows if r.get("flags")),
        "flagged": [r for r in rows if r.get("flags")],
        "cal_offset_histogram": dict(Counter(r.get("cal_offset_days") for r in rows)),
        "max_seam_gap_abs": max(
            (abs(r["seam_2359c_to_0000o"]) for r in rows
             if r.get("seam_2359c_to_0000o") is not None), default=None),
        "per_session": rows,
    }


def reproduce_known_cases() -> list[dict]:
    """Re-run handoff §5a's three cases under BOTH attributions, side by side."""
    cache = json.loads(CACHE_PATH.read_text())
    out = []
    for sess, hm in KNOWN_CASES:
        bars = {b["t"]: b for b in cache.get(sess, []) if b.get("cal")}
        bar = bars.get(hm)
        if bar is None:
            out.append({"session": sess, "t": hm, "error": "bar_not_found"})
            continue
        row = {
            "session": sess, "t": hm, "cal": bar.get("cal"),
            "bar": {k: bar.get(k) for k in ("o", "h", "l", "c", "v", "contract")},
        }
        for label, cal in (("session_date_attribution", sess),
                           ("cal_date_attribution", bar["cal"])):
            agg = load_tick_minutes(cal)
            tk = agg.get(str(bar.get("contract")), {}).get(hm)
            row[label] = {
                "compared_against": cal,
                "contract": bar.get("contract"),
                **({"no_ticks": True} if tk is None else compare(bar, tk)),
            }
        out.append(row)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=0, help="only the first N sessions")
    ap.add_argument("--sessions", default="", help="comma-separated session dates")
    ap.add_argument("--segments", default="",
                    help="comma-separated: day,night_pre_midnight,post_midnight")
    ap.add_argument("--attribution", choices=("cal", "session"), default="cal",
                    help="how to map a post-midnight bar to a calendar date")
    ap.add_argument("--full-findings", action="store_true",
                    help="write every finding instead of the first 500")
    ap.add_argument("--out", default=str(OUT_PATH))
    ap.add_argument("--skip-known-cases", action="store_true")
    args = ap.parse_args()

    report = {}
    if not args.skip_known_cases:
        report["known_cases_handoff_5a"] = reproduce_known_cases()
        print("=== handoff §5a known cases (bar vs ticks of that minute) ===")
        for r in report["known_cases_handoff_5a"]:
            if "error" in r:
                print(f"  {r['session']} {r['t']}: {r['error']}")
                continue
            b, s, c = r["bar"], r["session_date_attribution"], r["cal_date_attribution"]
            print(f"  session={r['session']} t={r['t']} cal={r['cal']} "
                  f"contract={b['contract']} bar_l/h={b['l']}/{b['h']}")
            print(f"    vs ticks on {s['compared_against']} (session-date): "
                  f"{s.get('tick_min')}-{s.get('tick_max')} dev={s.get('dev')} gap={s.get('gap')}")
            print(f"    vs ticks on {c['compared_against']} (cal-date)    : "
                  f"{c.get('tick_min')}-{c.get('tick_max')} dev={c.get('dev')} gap={c.get('gap')}")

    scan = run(args)
    report["full_scan"] = scan

    cache = json.loads(CACHE_PATH.read_text())
    sess_list = sorted(cache)
    if args.sessions:
        wanted = set(args.sessions.split(","))
        sess_list = [s for s in sess_list if s in wanted]
    if args.limit:
        sess_list = sess_list[: args.limit]
    struct = structural_check(sess_list, cache)
    report["structural_check"] = struct
    print(f"\n=== structural check (tick-independent) ===")
    print(f"cal offset histogram (days after session date): {struct['cal_offset_histogram']}")
    print(f"max |23:59c -> 00:00o| seam gap: {struct['max_seam_gap_abs']}pt")
    print(f"flagged sessions: {struct['n_flagged']}")
    for r in struct["flagged"][:20]:
        print(f"  {r['session']} {r['flags']}")

    print(f"\n=== full scan (attribution={scan['attribution']}) ===")
    print(f"sessions={scan['n_sessions']} {scan['sessions_range']} "
          f"bars_scanned={scan['n_bars_scanned']} compared={scan['n_bars_compared']} "
          f"no_tick={scan['n_bars_no_tick']}")
    for seg, d in sorted(scan["dev_distribution_by_segment"].items()):
        g = scan["gap_distribution_by_segment"].get(seg, {})
        print(f"  [{seg}] n={d['n']} dev mean={d['mean']} p95={d['p95']} p99={d['p99']} "
              f"max={d['max']} | gap mean={g.get('mean')} max={g.get('max')}")
    print(f"findings(dev>{SUSPECT_PTS}): {scan['n_findings']} "
          f"(severe>{SEVERE_PTS}: {scan['n_severe']}, suspect: {scan['n_suspect']}) "
          f"= {scan['pct_contaminated']}% of compared bars")
    print(f"  by segment: {scan['findings_by_segment']}")
    print(f"  by hour:    {scan['findings_by_hour']}")
    print(f"  contract alt check: {scan['contract_alt_check']}")
    for f in scan["findings"][:20]:
        print(f"  {f['grade']:7s} sess={f['session']} cal={f['cal']} {f['t']} "
              f"seg={f['segment']} bar_l/h={f['bar_l']}/{f['bar_h']} "
              f"tick={f['tick_min']}-{f['tick_max']} dev={f['dev']} gap={f['gap']} "
              f"cd={f['contract_used']}({f['contract_source']}) "
              f"alt={f['best_alt_contract']}/{f['best_alt_dev']}")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
