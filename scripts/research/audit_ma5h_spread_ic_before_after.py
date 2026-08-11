#!/usr/bin/env python3
"""Before/after re-run of the TW-US MA5h deviation-spread IC (handoff §4b) under
the 2026-08-11 calendar-attribution fix.

The IC in tmf_tw_us_ma5h_deviation_spread_ic.py converts each TX bar timestamp
to ET and looks up the NQ 5-hour MA there. Those timestamps were built as
f"{day}T{t}:00.000+08:00", which for a "session"-convention source is 24h early
on every 00:00-04:59 bar — so the us_dev leg of the spread was mixing in NQ
state from the previous day for ~26% of the bars.

This script recomputes the identical IC twice, changing ONLY the timestamp
construction (and, separately, the bar order), so the effect is attributable:

  legacy_ts : T = f"{day}T{t}:00.000+08:00"      (what §4b was computed with)
  fixed_ts  : T = cache_store.bar_timestamps(...) (real instants)

Both legs reuse `us_ma5h_dev` / `spearman` imported unmodified from the original
IC script, so this is the same estimator, not a re-implementation.

Read-only. Hits Yahoo for the NQ/ES 1h bundle (cached in-process).

Usage:
  PYTHONPATH=src .venv/bin/python scripts/research/audit_ma5h_spread_ic_before_after.py
  PYTHONPATH=src .venv/bin/python scripts/research/audit_ma5h_spread_ic_before_after.py --window oos
"""
from __future__ import annotations

import argparse
import json
import statistics as st
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, "src")
sys.path.insert(0, "scripts/research")

from tmf_channel import nq_gate as nq_gate_mod  # noqa: E402
from tmf_channel import nq_signal  # noqa: E402
from tmf_channel.cache_store import bar_timestamps, list_days, load_day  # noqa: E402
from tmf_order_layer_aware_replay import patch_nq_gate_for_backfill  # noqa: E402
from tmf_tw_us_ma5h_deviation_spread_ic import (  # noqa: E402
    HORIZONS_MIN,
    IS_DAYS,
    SOURCE_FOR_DAY,
    TW_WINDOW_MIN,
    spearman,
    us_ma5h_dev,
)

_TZ = ZoneInfo("Asia/Taipei")
OUT = Path("reports/research/channel_lab/audit_ma5h_spread_ic_before_after.json")


def timestamps(day, rows, source, mode):
    if mode == "legacy":
        return [f"{day}T{r.get('t')}:00.000+08:00" for r in rows]
    return bar_timestamps(day, rows, source=source)


def ic_for(days, bundle, min_age, *, ts_mode, order_mode="fixed"):
    per_day_ic = {h: [] for h in HORIZONS_MIN}
    us_cache: dict[str, float | None] = {}
    total_bars = 0
    n_spreads = 0
    for d in days:
        source = SOURCE_FOR_DAY.get(d, "tx_1m_fullnight_cache_full.json")
        rows = load_day(d, source=source)
        if not rows:
            continue
        if order_mode == "legacy":
            rows = sorted(rows, key=lambda r: str(r.get("t") or ""))
        C = [float(r["c"]) for r in rows]
        T = timestamps(d, rows, source, ts_mode)
        n = len(C)
        spreads = [None] * n
        for i in range(TW_WINDOW_MIN, n):
            tw_ma = sum(C[i - TW_WINDOW_MIN:i]) / TW_WINDOW_MIN
            if tw_ma <= 0:
                continue
            tw_dev = (C[i] - tw_ma) / tw_ma * 100.0
            dt_et = datetime.fromisoformat(T[i]).astimezone(_TZ).astimezone(nq_signal.TZ_ET)
            key = dt_et.strftime("%Y-%m-%d %H")
            if key not in us_cache:
                us_cache[key] = us_ma5h_dev(bundle, dt_et, min_age)
            us_dev = us_cache[key]
            if us_dev is None:
                continue
            spreads[i] = tw_dev - us_dev
        n_spreads += sum(1 for s in spreads if s is not None)
        for horizon in HORIZONS_MIN:
            xs, ys = [], []
            for i in range(TW_WINDOW_MIN, n - horizon):
                if spreads[i] is None:
                    continue
                xs.append(spreads[i])
                ys.append((C[i + horizon] - C[i]) / C[i] * 100.0)
            if len(xs) >= 10:
                ic = spearman(xs, ys)
                if ic is not None:
                    per_day_ic[horizon].append(ic)
        total_bars += n

    out = {"total_bars": total_bars, "n_spreads": n_spreads, "horizons": {}}
    for horizon in HORIZONS_MIN:
        ics = per_day_ic[horizon]
        if len(ics) < 2:
            out["horizons"][horizon] = {"n_days": len(ics), "insufficient": True}
            continue
        mean_ic = st.fmean(ics)
        sd_ic = st.stdev(ics)
        t = mean_ic / (sd_ic / (len(ics) ** 0.5)) if sd_ic > 0 else 0.0
        try:
            from scipy import stats as sp

            p = float(2 * (1 - sp.t.cdf(abs(t), df=len(ics) - 1)))
        except Exception:
            p = None
        out["horizons"][horizon] = {
            "n_days": len(ics), "mean_daily_IC": round(mean_ic, 4),
            "std": round(sd_ic, 4), "t": round(t, 3), "p": p,
        }
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", choices=("is", "oos", "both"), default="both")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()

    patch_nq_gate_for_backfill(lookback_days=500)
    min_age = nq_signal.NQ_ES_1H_MIN_AGE
    bundle = nq_gate_mod.get_cached(
        "nq_futures_1h", 1800.0, nq_gate_mod._load_futures_bundle
    )

    windows = {}
    if args.window in ("is", "both"):
        windows["IS_22d"] = list(IS_DAYS)
    if args.window in ("oos", "both"):
        windows["OOS_66d"] = [
            d for d in list_days(source="tx_1m_fullnight_cache_full.json")
            if d < "2026-07-08"
        ]
    if args.limit:
        windows = {k: v[: args.limit] for k, v in windows.items()}

    # Three legs, so the two defects are attributable separately and the first
    # leg actually reproduces what §4b was computed with (scrambled bar order
    # from the pre-fix load_day AND session-dated timestamps).
    legs = [
        ("original", dict(order_mode="legacy", ts_mode="legacy")),
        ("ts_fixed_only", dict(order_mode="legacy", ts_mode="fixed")),
        ("fully_fixed", dict(order_mode="fixed", ts_mode="fixed")),
    ]
    report = {}
    for label, days in windows.items():
        print(f"\n===== {label} ({len(days)} days) =====")
        res = {name: ic_for(days, bundle, min_age, **kw) for name, kw in legs}
        report[label] = {"n_days": len(days), **res}
        header = f"{'horizon':>8} |"
        for name, _ in legs:
            header += f" {name+' IC':>18} {'t':>7} {'p':>10} |"
        print(header)
        for h in HORIZONS_MIN:
            line = f"{h:>8} |"
            for name, _ in legs:
                x = res[name]["horizons"][h]
                if x.get("insufficient"):
                    line += f" {'insufficient':>18} {'':>7} {'':>10} |"
                else:
                    line += (f" {x['mean_daily_IC']:>18.4f} {x['t']:>7.3f} "
                             f"{x['p']:>10.3g} |")
            print(line)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
