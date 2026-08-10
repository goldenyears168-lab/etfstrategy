"""NQ overnight side gate for cell.bias (frozen in-package signal).

Uses ``tmf_channel.nq_signal`` (verbatim port of the channel_lab research
helpers) — no runtime import of research code outside the frozen package.
Fail-safe: any load or eval problem returns ``None`` (no bias), never raises
into the order layer.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from tmf_channel import nq_signal
from tmf_channel.aux_cache import get_cached

_TZ = ZoneInfo("Asia/Taipei")
_LAST_ERR: str | None = None
_STALE_DAYS_MAX = 2


def _load_futures_bundle():
    """Fetch ES/NQ 1h + daily closes straight from Yahoo Finance.

    Replaces the old ``nq_signal.load_futures_1h()`` (reads a static
    ``nikkei_us_intraday_1h_cache.json`` research snapshot with no refresh
    job anywhere in the repo — the root cause of the 2026-08-08 stale-data
    incident). ``get_cached(..., 1800.0, ...)`` around this call already
    rate-limits to one real fetch per 30 minutes, so this needs no separate
    scheduled sync job — every cache refresh pulls genuinely current data.
    On any fetch failure this raises, and the caller's fail-safe except
    already turns that into "gate unavailable" (None), never a live block.
    """
    from us_futures_overnight import ES_YAHOO, NQ_YAHOO, fetch_yahoo_daily_closes, fetch_yahoo_intraday_closes

    end = datetime.now(tz=_TZ).date() + timedelta(days=1)
    start = end - timedelta(days=10)
    nq_1h = fetch_yahoo_intraday_closes(NQ_YAHOO, start, end, interval="1h")
    es_1h = fetch_yahoo_intraday_closes(ES_YAHOO, start, end, interval="1h")
    nq_d = fetch_yahoo_daily_closes(NQ_YAHOO, start - timedelta(days=30), end)
    es_d = fetch_yahoo_daily_closes(ES_YAHOO, start - timedelta(days=30), end)
    us_dates = sorted(set(nq_d.index.astype(str)) | set(es_d.index.astype(str)))
    return nq_1h, es_1h, nq_d, es_d, us_dates


def nq_side_for_day(day: str, *, hm: str | None = None) -> str | None:
    """Return ``L`` / ``S`` / ``none`` or ``None`` if unavailable.

    Cached futures bundle for 30 minutes; side eval is cheap per day.

    Found live 2026-08-08: ``nikkei_us_intraday_1h_cache.json`` has no
    scheduled refresh job anywhere in the repo and had been frozen since
    2026-08-05. ``price_at_or_before``/``prior_us_rth_close_date`` both
    silently fall back to the last available (stale) row, so a frozen feed
    compares the same frozen price against itself and reports a fabricated
    0.0% "flat" ("none" side) every single day going forward — never an
    error, never caught by the existing try/except. Combined with cell.bias
    now True on all 16 cells (2026-08-06 refactor), a "none" side hard-blocks
    both L and S for every new entry (see causal_engine.py's ssg_none
    branch), silently killing nearly all live entries. Guard here: if the
    freshest date in the cache is more than ``_STALE_DAYS_MAX`` calendar days
    behind today, treat the feed as unavailable (None) rather than "flat" —
    None already means "no gate opinion, don't block" per this function's
    existing contract, so this only restores normal (gate-inactive) cell
    behavior; it does not add any new blocking path.

    Second bug found same night: the ``hm < "05:00"`` branch anchored to
    *today's own* 15:00 open, a session that has not started yet, instead of
    the night session actually in progress (which opened at 15:00
    *yesterday*) — so every call during the 00:00-05:00 tail of a night
    session was silently biased by a same-day-future reference date, same
    "which trading day does 00:00-05:00 belong to" mistake already handled
    correctly elsewhere in this package (``trading_day_str()``,
    ``_in_night_window`` in ``tmf_channel_marketdata.py``).

    Third change (2026-08-10): anchor is now ``day`` + the *current* ``hm``,
    recomputed fresh every call, instead of a value frozen at that session's
    open (08:45 day / 15:00 night) for the session's whole duration. NQ/ES
    futures keep trading through the TX day session, so the frozen anchor
    only ever reflected pre-market movement and never any US-market action
    that happened WHILE the TX session was live. True re-simulation across
    22 in-sample + 66 out-of-sample days (causal_engine.py's
    session_side_gate lookup now supports a per-bar-timestamp key —
    backward compatible, existing day-keyed callers unaffected — see
    scripts/research/tmf_continuous_gate_vs_frozen_anchor.py) showed the
    continuous anchor beating the frozen one on both windows, OOS
    significantly (p=0.0037, n=66). ``day``+``hm`` together always describe
    a real, valid timestamp (both come from the same "now" snapshot in the
    live caller, or from the same historical (day, hm) pair in research
    callers) — no separate anchor-day-shift branch is needed for the
    00:00-05:00 tail; ``f"{day}T{hm}:00"`` already resolves to the correct
    real moment either way.
    """
    global _LAST_ERR
    try:
        bundle = get_cached("nq_futures_1h", 1800.0, _load_futures_bundle)
        if bundle is None:
            return None
        nq_1h, es_1h, nq_d, es_d, us_dates = bundle
        if not us_dates:
            _LAST_ERR = "nq_es_1h_cache: no us_dates in bundle"
            return None
        latest = max(us_dates)
        stale_days = (date.fromisoformat(day) - date.fromisoformat(latest)).days
        if stale_days > _STALE_DAYS_MAX:
            _LAST_ERR = (
                f"nq_es_1h_cache stale: latest={latest} ({stale_days}d old, "
                f"max={_STALE_DAYS_MAX}) — gate unavailable, not blocking"
            )
            return None
        if hm is None:
            hm = datetime.now(tz=_TZ).strftime("%H:%M")
        dt = datetime.fromisoformat(f"{day}T{hm}:00").replace(tzinfo=_TZ)
        snap = nq_signal.futures_overnight_at(
            dt, nq_1h=nq_1h, es_1h=es_1h, nq_d=nq_d, es_d=es_d, us_dates=us_dates
        )
        nq = None if snap is None else snap.get("nq_overnight_pct")
        side = nq_signal.bias_side(nq)
        if side == "up":
            return "L"
        if side == "down":
            return "S"
        return "none"
    except Exception as exc:  # noqa: BLE001 — gate is best-effort
        _LAST_ERR = str(exc)[:200]
        return None


def last_nq_load_error() -> str | None:
    return _LAST_ERR
