"""TW-US 1-minute MA-deviation spread session-side gate (live).

spread(t) = tw_dev(t) - us_dev(t): tw_dev is TX's deviation from its own
trailing WINDOW_MIN 1m-bar MA, us_dev is NQ's deviation from its own
trailing WINDOW_MIN 1m-bar MA (real Yahoo 1m closes, not the coarser
settled-hourly-bar proxy ``nq_gate.nq_side_for_day`` uses). Mean-reversion
rule: spread >= +THRESHOLD -> expect TX to fall -> "S"; spread <=
-THRESHOLD -> expect TX to rise -> "L"; else "none".

2026-08-11: explicitly authorized by user as a live trial with real (user's
own words: "test-sized") capital while forward-validation data accumulates
-- this candidate has NOT cleared the IS(22d)/OOS(66d)/3-holdout bar every
other change to this recipe has cleared before going live (only ~3 days of
genuine TX+real-1m-NQ overlap exist right now; see
docs/tmf-channel-research-handoff-20260811.md and
scripts/research/nq_es_1m_daily_accumulate.py, which keeps building real
history so this can be re-validated properly once enough days exist).
WINDOW_MIN=100 and THRESHOLD=0.2 were the values actually backtested
end-to-end against the live continuous NQ gate on those 3 days
(scripts/research/tmf_spread_gate_backtest_2h.py's sibling ad-hoc run) --
not re-tuned here.

Fail-safe: any load/eval problem returns ``None`` (no bias, gate inactive),
never raises into the order layer -- identical contract to
``tmf_channel.nq_gate.nq_side_for_day``, which this replaces as the
``session_side_gate`` source in ``order.tmf_channel_order.desired_from_simulate``.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from tmf_channel.aux_cache import get_cached
from us_futures_overnight import TZ_ET

WINDOW_MIN = 100
THRESHOLD = 0.2
_NQ_1M_CACHE_TTL_SEC = 90.0
_LAST_ERR: str | None = None


def _load_nq_1m_recent():
    from us_futures_overnight import NQ_YAHOO, fetch_yahoo_intraday_closes

    end = datetime.now(tz=TZ_ET).date() + timedelta(days=1)
    start = end - timedelta(days=2)
    return fetch_yahoo_intraday_closes(NQ_YAHOO, start, end, interval="1m")


def _tw_dev(C: list[float]) -> float | None:
    """MA over the WINDOW_MIN bars strictly BEFORE the current one (matches
    the rolling-sum construction in the backtested rule -- current bar is
    excluded from its own reference average). Do not "simplify" this to
    include the current bar; that changes the definition from the one
    actually validated end-to-end against the live gate."""
    n = len(C)
    if n < WINDOW_MIN + 1:
        return None
    now_px = float(C[-1])
    window = C[-(WINDOW_MIN + 1):-1]
    ma = sum(window) / WINDOW_MIN
    if ma <= 0:
        return None
    return (now_px - ma) / ma * 100.0


def _us_dev(nq_1m, dt_et: datetime) -> float | None:
    sub_now = nq_1m[nq_1m.index <= dt_et]
    if sub_now.empty:
        return None
    now_px = float(sub_now.iloc[-1])
    sub = nq_1m[(nq_1m.index > dt_et - timedelta(minutes=WINDOW_MIN)) & (nq_1m.index <= dt_et)]
    if sub.empty:
        return None
    ma = float(sub.mean())
    if ma <= 0:
        return None
    return (now_px - ma) / ma * 100.0


def spread_side_for_day(day: str, *, hm: str, C: list[float], T: list[str]) -> str | None:
    """Return ``L`` / ``S`` / ``none`` or ``None`` if unavailable.

    Two distinct failure modes, matched to what the backtested rule actually
    did bar-by-bar (see module docstring) vs. what ``nq_gate.nq_side_for_day``
    established as this order layer's safety contract for an unavailable
    feed:

    - NQ 1m feed genuinely unreachable/empty (network failure, dead cache) ->
      ``None`` (fail-OPEN: gate inactive, cell.bias mechanism skipped
      entirely in causal_engine -- matches ``nq_gate.py``'s existing
      contract, which exists specifically to stop a frozen/broken feed from
      silently hard-blocking every entry indefinitely).
    - NQ 1m feed loaded fine but has no bar in the trailing WINDOW_MIN
      minutes at this instant (e.g. CME's daily maintenance window), or not
      enough TX history yet (fresh worker start) -> ``"none"`` (hard block
      for THIS instant only, self-resolves within WINDOW_MIN minutes --
      exactly what the backtested rule did for these same cases, see
      ``devs[i] is None -> "none"`` in the research script).
    """
    global _LAST_ERR
    try:
        nq_1m = get_cached("nq_1m_live", _NQ_1M_CACHE_TTL_SEC, _load_nq_1m_recent)
    except Exception as exc:  # noqa: BLE001 -- feed load is best-effort
        _LAST_ERR = str(exc)[:200]
        return None
    if nq_1m is None or nq_1m.empty:
        _LAST_ERR = "nq_1m_live: empty series"
        return None

    try:
        tw_dev = _tw_dev(C)
        if tw_dev is None:
            return "none"

        if T and T[-1]:
            dt = datetime.fromisoformat(T[-1])
        else:
            from zoneinfo import ZoneInfo

            dt = datetime.fromisoformat(f"{day}T{hm}:00").replace(tzinfo=ZoneInfo("Asia/Taipei"))
        dt_et = dt.astimezone(TZ_ET)
        us_dev = _us_dev(nq_1m, dt_et)
        if us_dev is None:
            return "none"

        spread = tw_dev - us_dev
        if spread >= THRESHOLD:
            return "S"
        if spread <= -THRESHOLD:
            return "L"
        return "none"
    except Exception as exc:  # noqa: BLE001 -- eval is best-effort
        _LAST_ERR = str(exc)[:200]
        return None


def last_spread_load_error() -> str | None:
    return _LAST_ERR
