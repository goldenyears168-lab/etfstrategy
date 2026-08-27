#!/usr/bin/env python3
"""Out-of-sample validation (2026-08-09) of the day|normal 30-min
efficiency-ratio (ER) split found on the 22-day window (2026-07-08..08-07):
top ER quartile (>=0.313) net +926.0 (+9.08/trade), bottom ER quartile
(<=0.073) net -2053.0 (-19.93/trade) -- the bottom quartile alone was 56%
of day|normal's total loss.

That threshold was DISCOVERED on those 22 days -- applying it back to the
same days would be circular. This script applies the SAME FIXED thresholds
(0.073 / 0.313, not re-derived) to 2026-04-01..07-07 (66 trading days,
genuinely unused by any prior analysis tonight, same cache source so no
rebuild needed) to see whether the effect generalizes or was a same-sample
artifact.

Descriptive only -- does NOT touch src/order/, config/order.yaml, .env,
launchd/, scripts/order/, and does not modify session_pv_book.
"""
from __future__ import annotations

import json
import statistics as st
import sys
from copy import deepcopy

sys.path.insert(0, "src")

from order.tmf_channel_config import PAPER_RECIPE  # noqa: E402
from tmf_channel.cache_store import list_days, load_day  # noqa: E402
from tmf_channel.engine import load_vixtwn_delta, simulate  # noqa: E402

SOURCE = "tx_1m_fullnight_cache_full.json"
IN_SAMPLE_CUTOFF = "2026-07-08"  # exclude anything >= this (already used)

LOOKBACK = 30
ER_LO = 0.073   # bottom-quartile cutoff, fixed from the in-sample discovery
ER_HI = 0.313   # top-quartile cutoff, fixed from the in-sample discovery


def efficiency_ratio(C: list[float], idx: int, lookback: int = LOOKBACK) -> float | None:
    if idx < lookback:
        return None
    net = abs(C[idx] - C[idx - lookback])
    path = sum(abs(C[k] - C[k - 1]) for k in range(idx - lookback + 1, idx + 1))
    if path <= 0:
        return None
    return net / path


def main():
    vix = load_vixtwn_delta() or {}
    recipe = deepcopy(PAPER_RECIPE)
    recipe.setdefault("hang_anchor", "O")

    oos_days = [d for d in list_days(source=SOURCE) if d < IN_SAMPLE_CUTOFF]
    print(f"OOS window: {len(oos_days)} days, {oos_days[0]}..{oos_days[-1]}")

    entries = []  # (day, er, pnl)
    for day in oos_days:
        rows = load_day(day, source=SOURCE)
        if not rows:
            continue
        O = [float(r["o"]) for r in rows]
        H = [float(r["h"]) for r in rows]
        L = [float(r["l"]) for r in rows]
        C = [float(r["c"]) for r in rows]
        V = [float(r.get("v") or 0) for r in rows]
        T = [f"{day}T{r.get('t')}:00.000+08:00" for r in rows]
        t_index = {ts: i for i, ts in enumerate(T)}

        trades, *_ = simulate(O, H, L, C, V, T, recipe, vix_delta=vix)
        for tr in trades:
            if str(tr.get("regime_e") or "") != "normal":
                continue
            hm = str(tr.get("et") or "")[11:16]
            if not ("08:45" <= hm < "13:45"):
                continue
            et = str(tr.get("et") or "")
            idx = t_index.get(et)
            if idx is None:
                continue
            er = efficiency_ratio(C, idx)
            if er is None:
                continue
            entries.append((day, er, float(tr["pnl"])))

    if not entries:
        print("no day|normal entries found in OOS window")
        return

    def stats(vals):
        n = len(vals)
        if n == 0:
            return dict(n=0)
        return dict(
            n=n, net=round(sum(vals), 1), mean=round(st.mean(vals), 2),
            std=round(st.stdev(vals), 2) if n > 1 else 0.0,
            win_rate=round(100.0 * sum(1 for v in vals if v > 0) / n, 1),
        )

    all_pnls = [pnl for _, _, pnl in entries]
    top = [pnl for _, er, pnl in entries if er >= ER_HI]
    bot = [pnl for _, er, pnl in entries if er <= ER_LO]
    mid = [pnl for _, er, pnl in entries if ER_LO < er < ER_HI]

    print(f"\ntotal OOS day|normal (day-session) entries: {len(entries)}")
    print("ALL entries:              ", stats(all_pnls))
    print(f"ER >= {ER_HI} (top, fixed thr):  ", stats(top))
    print(f"ER <= {ER_LO} (bottom, fixed thr):", stats(bot))
    print(f"{ER_LO} < ER < {ER_HI} (middle):    ", stats(mid))

    # single-day sensitivity: exclude 2026-06-05 (known contaminated day
    # from other research tonight) from the bottom-quartile bucket.
    bot_excl = [pnl for d, er, pnl in entries if er <= ER_LO and d != "2026-06-05"]
    print(f"\nbottom quartile excl 2026-06-05:", stats(bot_excl))

    out_path = "reports/research/channel_lab/tmf_daynormal_30m_context_oos_validate_result.json"
    with open(out_path, "w") as f:
        json.dump(
            dict(
                oos_days=len(oos_days), n_entries=len(entries),
                all=stats(all_pnls), top=stats(top), bottom=stats(bot), middle=stats(mid),
                bottom_excl_20260605=stats(bot_excl),
            ),
            f, indent=2, ensure_ascii=False,
        )
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
