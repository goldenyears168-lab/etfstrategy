"""NQ overnight side gate for cell.bias (frozen in-package signal).

Uses ``tmf_channel.nq_signal`` (verbatim port of the channel_lab research
helpers) — no runtime import of research code outside the frozen package.
Fail-safe: any load or eval problem returns ``None`` (no bias), never raises
into the order layer.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from tmf_channel import nq_signal
from tmf_channel.aux_cache import get_cached

_TZ = ZoneInfo("Asia/Taipei")
_LAST_ERR: str | None = None


def _load_futures_bundle():
    return nq_signal.load_futures_1h()


def nq_side_for_day(day: str, *, hm: str | None = None) -> str | None:
    """Return ``L`` / ``S`` / ``none`` or ``None`` if unavailable.

    Cached futures bundle for 30 minutes; side eval is cheap per day.
    """
    global _LAST_ERR
    try:
        bundle = get_cached("nq_futures_1h", 1800.0, _load_futures_bundle)
        if bundle is None:
            return None
        nq_1h, es_1h, nq_d, es_d, us_dates = bundle
        if hm is None:
            hm = datetime.now(tz=_TZ).strftime("%H:%M")
        if hm >= "15:00" or hm < "05:00":
            dt = datetime.fromisoformat(f"{day}T15:00:00").replace(tzinfo=_TZ)
        else:
            dt = datetime.fromisoformat(f"{day}T08:45:00").replace(tzinfo=_TZ)
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
