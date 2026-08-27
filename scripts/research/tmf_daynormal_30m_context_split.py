#!/usr/bin/env python3
"""Quick validation (2026-08-09): within the day|normal cell (biggest-volume,
biggest-loss cell in the 22-day breakdown), does a 30-minute-scale trend
context distinguish a meaningfully-better sub-population from a worse one?

For every day|normal trade (current LIVE recipe, raw simulate(), same
22-day corrected full-day window), compute Kaufman's Efficiency Ratio over
the preceding 30 one-minute bars ending at entry:
    ER = |C[t] - C[t-30]| / sum(|C[k]-C[k-1]| for k in t-29..t)
ER near 1 = a clean directional 30-min move (trend); near 0 = choppy/range,
scale-invariant so no arbitrary point-threshold is needed. Split at the
median ER across all day|normal entries (balanced comparison, not a
cherry-picked cutoff) into "30m-trend" vs "30m-range" halves and compare.

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
from tmf_channel.cache_store import load_day  # noqa: E402
from tmf_channel.engine import load_vixtwn_delta, simulate  # noqa: E402

JULY_DAYS = [
    "2026-07-08", "2026-07-09", "2026-07-13", "2026-07-14", "2026-07-15",
    "2026-07-16", "2026-07-17", "2026-07-20", "2026-07-21", "2026-07-22",
    "2026-07-23", "2026-07-24", "2026-07-27", "2026-07-28", "2026-07-29",
    "2026-07-30", "2026-07-31",
]
AUG_DAYS = ["2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07"]
SOURCE_FOR_DAY = {d: "tx_1m_fullnight_cache_full.json" for d in JULY_DAYS}
SOURCE_FOR_DAY.update({d: "tx_1m_tick_built_fullnight_aug" for d in AUG_DAYS})

LOOKBACK = 30


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

    entries = []  # (er, pnl)
    for day in JULY_DAYS + AUG_DAYS:
        source = SOURCE_FOR_DAY[day]
        rows = load_day(day, source=source)
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
            if not ("08:45" <= hm < "13:45"):  # day session only
                continue
            et = str(tr.get("et") or "")
            idx = t_index.get(et)
            if idx is None:
                continue
            er = efficiency_ratio(C, idx)
            if er is None:
                continue
            entries.append((er, float(tr["pnl"])))

    if not entries:
        print("no day|normal entries found")
        return

    ers = sorted(e[0] for e in entries)
    median_er = ers[len(ers) // 2]
    trend = [pnl for er, pnl in entries if er >= median_er]
    rng = [pnl for er, pnl in entries if er < median_er]

    def stats(vals):
        n = len(vals)
        if n == 0:
            return dict(n=0)
        return dict(
            n=n, net=round(sum(vals), 1), mean=round(st.mean(vals), 2),
            std=round(st.stdev(vals), 2) if n > 1 else 0.0,
            win_rate=round(100.0 * sum(1 for v in vals if v > 0) / n, 1),
        )

    print(f"total day|normal (day-session) entries: {len(entries)}, median ER={median_er:.3f}")
    print("30m-TREND half (ER >= median):", stats(trend))
    print("30m-RANGE half (ER <  median):", stats(rng))

    # also report top/bottom ER quartile for a sharper contrast
    q = len(ers) // 4
    er_p25 = ers[q]
    er_p75 = ers[-q] if q > 0 else ers[-1]
    top_q = [pnl for er, pnl in entries if er >= er_p75]
    bot_q = [pnl for er, pnl in entries if er <= er_p25]
    print(f"\ntop ER quartile (>= {er_p75:.3f}):", stats(top_q))
    print(f"bottom ER quartile (<= {er_p25:.3f}):", stats(bot_q))

    out_path = "reports/research/channel_lab/tmf_daynormal_30m_context_split_result.json"
    with open(out_path, "w") as f:
        json.dump(
            dict(median_er=median_er, trend=stats(trend), range=stats(rng),
                 top_quartile=stats(top_q), bottom_quartile=stats(bot_q)),
            f, indent=2, ensure_ascii=False,
        )
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
