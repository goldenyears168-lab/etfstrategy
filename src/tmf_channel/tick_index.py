"""Real TAIFEX tick index for the engine's ``tick_native`` replay path.

``causal_engine.simulate()`` has accepted a ``tick_index`` since the
H-TICK-NATIVE work, but nothing in the repo has built one for a long time:
every current research script (``gt5_r5_official_4window_backtest.py``,
``audit_cell_tune_v2_*.py``, ``gt5_r*``…) passes ``tick_native=False``, so the
whole evidence base behind the live PV16 recipe rests on the 1-minute-bar
fill convention — *a resting limit fills whenever the bar's H/L reaches its
price*. That convention silently assumes the strategy is always at the front
of the queue, which for a purely passive strategy is the single strongest
assumption in the entire backtest (see the CCF second-level study: 82% of
passive limit orders never reached the front of their queue).

This module rebuilds the index from FinMind TX trade prints
(``$GOLDENSTOCKS_DATA_DIR/cache/tmf_channel/finmind_tx_tick_by_day/*.json``:
one row per print, ``price`` + ``volume``), and additionally carries per-print
volume — which the original index did not — so a queue-position model can be
evaluated on top of it (see ``causal_engine`` ``fill_model``).

Proxy caveat, stated once here so downstream reports can cite it: these are
**TX (large) prints, not TMF (micro)**. The *price path* is the same index and
is exactly right; the *depth* is not — TMF's book is far thinner, so any
volume-based queue estimate computed from TX volume is optimistic. The
``through`` fill model deliberately uses no volume at all and is therefore
free of this caveat.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

_TICK_DIR_NAME = "finmind_tx_tick_by_day"


@dataclass
class TickIndex:
    """Shape expected by ``causal_engine._bar_tick_range`` / ``_tick_walk_bar``.

    ``T`` mirrors the bar array's timestamps; ``minute_start_idx`` maps a bar
    timestamp to the first tick belonging to that minute; ticks are stored flat
    and in time order so a bar's slice is ``[start, end)``.
    """

    T: list[str]
    minute_start_idx: dict[str, int]
    n_tk: int
    tk_px: list[float]
    tk_vol: list[float] = field(default_factory=list)
    #: seconds-of-day per print — lets callers build trailing-N-second
    #: order-flow features, which minute boundaries alone cannot express.
    tk_sec: list[int] = field(default_factory=list)
    n_bars_with_ticks: int = 0


def tick_dir() -> Path:
    try:
        import stock_db

        root = Path(stock_db.DATA_DIR).parent
    except Exception:  # noqa: BLE001
        root = Path.home() / "goldenstocks-data"
    return root / "cache" / "tmf_channel" / _TICK_DIR_NAME


@lru_cache(maxsize=8)
def _load_raw(day: str, product: str = "TX") -> tuple[tuple[str, float, float, int], ...]:
    """(minute_key 'YYYY-mm-dd HH:MM', price, volume, sec_of_day) for the FRONT-MONTH OUTRIGHT.

    The FinMind tick file is the whole TX tape, not one contract: it mixes
    every listed month *and* every calendar-spread combo. Spread rows carry a
    ``contract_date`` like ``"202608/202609"`` and their ``price`` is the
    spread itself — 36 to 103 points on 2026-08-06, against an index near
    44,000. Feeding those into the replay lets a rail "fill" at 36 and exit at
    44,000; the first run of this replay before the filter existed reported
    +1.25 million points/day, which is how the bug announced itself. Anything
    that walks this tape must filter first.

    Front month = the highest-volume outright (no "/") that day, which rolls
    automatically — the same heuristic the taifex-intraday-snapshot job uses.
    """
    path = tick_dir() / f"{day}.json"
    if not path.exists():
        return ()
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ()

    vol_by_contract: dict[str, float] = {}
    for r in rows:
        if str(r.get("futures_id") or "") != product:
            continue
        cd = str(r.get("contract_date") or "")
        if not cd or "/" in cd:
            continue  # calendar spread, not an outright
        try:
            vol_by_contract[cd] = vol_by_contract.get(cd, 0.0) + float(r.get("volume") or 0)
        except (TypeError, ValueError):
            continue
    if not vol_by_contract:
        return ()
    front = max(vol_by_contract, key=lambda k: vol_by_contract[k])

    out: list[tuple[str, float, float, int]] = []
    for r in rows:
        if str(r.get("futures_id") or "") != product:
            continue
        if str(r.get("contract_date") or "") != front:
            continue
        ts = str(r.get("date") or "")
        if len(ts) < 19:
            continue
        try:
            sec = int(ts[11:13]) * 3600 + int(ts[14:16]) * 60 + int(ts[17:19])
            out.append((ts[:16], float(r["price"]), float(r.get("volume") or 0), sec))
        except (KeyError, TypeError, ValueError):
            continue
    return tuple(out)


def available_days() -> list[str]:
    return sorted(p.stem for p in tick_dir().glob("*.json"))


def build_tick_index(T: list[str]) -> TickIndex | None:
    """Build an index covering bar timestamps ``T`` (full ISO, Asia/Taipei).

    ``T`` may span two calendar dates (a night session runs 15:00 → 05:00), so
    every distinct date in ``T`` is loaded, not just the session's anchor day.
    Returns None when no tick file covers the range — callers must then fall
    back to bar-level replay rather than silently reporting a tick result.
    """
    if not T:
        return None
    days = sorted({str(t)[:10] for t in T})
    by_minute: dict[str, list[tuple[float, float, int]]] = {}
    for day in days:
        for minute_key, px, vol, sec in _load_raw(day):
            by_minute.setdefault(minute_key, []).append((px, vol, sec))
    if not by_minute:
        return None

    tk_px: list[float] = []
    tk_vol: list[float] = []
    tk_sec: list[int] = []
    minute_start_idx: dict[str, int] = {}
    covered = 0
    for bar_t in T:
        # EVERY bar gets a start offset, including tickless minutes (start==end
        # → empty slice). This is load-bearing, not tidiness:
        # causal_engine._bar_tick_range() derives bar t's end from bar t+1's
        # start and falls back to ``n_tk`` when t+1 is absent from this dict —
        # so a single missing minute would hand bar t *every remaining tick of
        # the session* and turn the replay into wholesale look-ahead. Quiet
        # night minutes with zero prints are common, so that is not a corner
        # case. Recording all bars makes the fallback unreachable.
        minute_start_idx[bar_t] = len(tk_px)
        # bar_t is "YYYY-mm-ddTHH:MM:SS+08:00"; tick keys are "YYYY-mm-dd HH:MM"
        key = f"{str(bar_t)[:10]} {str(bar_t)[11:16]}"
        prints = by_minute.get(key)
        if not prints:
            continue
        for px, vol, sec in prints:
            tk_px.append(px)
            tk_vol.append(vol)
            tk_sec.append(sec)
        covered += 1
    if not tk_px:
        return None
    return TickIndex(
        T=list(T),
        minute_start_idx=minute_start_idx,
        n_tk=len(tk_px),
        tk_px=tk_px,
        tk_vol=tk_vol,
        tk_sec=tk_sec,
        n_bars_with_ticks=covered,
    )


def coverage(T: list[str], idx: TickIndex | None) -> float:
    """Fraction of bars that actually have ticks — report it, never assume 1.0."""
    if not T or idx is None:
        return 0.0
    return round(idx.n_bars_with_ticks / len(T), 4)
