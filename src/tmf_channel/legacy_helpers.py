"""Minimal helpers formerly imported from jack_channel_v5.

Keeps the live engine free of ``reports/research/channel_lab`` on sys.path
for the hot import graph (v5 teacher file stays for historical labs only).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from stock_db import PROJECT_ROOT

COST = 3.0
_LAB = PROJECT_ROOT / "reports" / "research" / "channel_lab"

TEACHER = [
    dict(s="S", ep=43700, xp=43000, pts=700, open_t="2026-08-04 09:54", close_t="2026-08-04 12:10"),
    dict(s="S", ep=43038, xp=43000, pts=38, open_t="2026-08-04 ~09:xx", close_t="2026-08-04 11:14"),
    dict(s="S", ep=43300, xp=42770, pts=530, open_t="?", close_t="?"),
    dict(s="S", ep=42840, xp=42630, pts=210, open_t="?", close_t="?"),
    dict(s="S", ep=42650, xp=42400, pts=250, open_t="?", close_t="?"),
    dict(s="S", ep=42430, xp=42600, pts=-170, open_t="?", close_t="?"),
    dict(s="S", ep=43000, xp=42676, pts=324, open_t="?", close_t="?"),
    dict(s="L", ep=42360, xp=42388, pts=28, open_t="?", close_t="?"),
    dict(s="L", ep=42500, xp=42800, pts=300, open_t="?", close_t="?"),
]


def load_merged_tmf():
    """Day 8/3 from tmf_0803 + full night/day from live_0804 extract (lab files)."""
    day = json.loads((_LAB / "tmf_0803_bars.json").read_text())
    full = json.loads((_LAB / "tmf_full_night_0803_0804.json").read_text())
    d83 = [
        dict(
            date="2026-08-03",
            t=x["t"],
            o=x["o"],
            h=x["h"],
            l=x["l"],
            c=x["c"],
            v=x["v"],
            sess="day",
        )
        for x in day
        if x.get("sess") == "day" or ("08:45" <= x["t"] < "15:00")
    ]
    n83 = [
        x
        for x in full
        if (x["date"] == "2026-08-03" and x["t"] >= "15:00")
        or (x["date"] == "2026-08-04" and x["t"] < "08:00")
    ]
    d84 = [x for x in full if x["date"] == "2026-08-04" and x["t"] >= "08:45"]
    merged = d83 + n83 + d84
    seen: dict[tuple[str, str], dict] = {}
    for x in merged:
        seen[(x["date"], x["t"])] = x

    def key(x: dict) -> tuple[str, str]:
        return (x["date"], x["t"])

    return sorted(seen.values(), key=key)


def arrays(bars):
    return (
        [x["o"] for x in bars],
        [x["h"] for x in bars],
        [x["l"] for x in bars],
        [x["c"] for x in bars],
        [x["v"] for x in bars],
        [f"{x['date']} {x['t']}" for x in bars],
    )


def _ols(ys):
    m = len(ys)
    if m < 2:
        return 0.0
    xs = list(range(m))
    mx = (m - 1) / 2.0
    my = sum(ys) / m
    sxx = sum((x - mx) ** 2 for x in xs)
    if not sxx:
        return 0.0
    return sum((xs[i] - mx) * (ys[i] - my) for i in range(m)) / sxx


def slope_at(C, t, win=10):
    if t < win - 1:
        return None
    return _ols(C[t - win + 1 : t + 1])


def summarize(trades: list[dict[str, Any]] | None):
    if not trades:
        return dict(n=0, net=0.0, wr=0, nL=0, nS=0)
    net = sum(x["pnl"] for x in trades)
    w = sum(1 for x in trades if x["pnl"] > 0)
    return dict(
        n=len(trades),
        net=round(net, 1),
        wr=round(100 * w / len(trades)),
        nL=sum(1 for x in trades if x["s"] == "L"),
        nS=sum(1 for x in trades if x["s"] == "S"),
    )


def teacher_match(trades, tol=50):
    rows = []
    for jt in TEACHER:
        best = None
        for tr in trades:
            if tr["s"] != jt["s"]:
                continue
            d = abs(tr["ep"] - jt["ep"])
            if best is None or d < best[0]:
                best = (d, tr)
        rows.append(
            dict(
                ep=jt["ep"],
                s=jt["s"],
                pts=jt["pts"],
                dist=None if best is None else round(best[0], 1),
                ok=best is not None and best[0] <= tol,
                bot_ep=None if best is None else best[1]["ep"],
                bot_pnl=None if best is None else best[1]["pnl"],
            )
        )
    return rows
