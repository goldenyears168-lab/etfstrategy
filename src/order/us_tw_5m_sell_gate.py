"""TW↔NQ 5m sell-gate live evaluator (human SOP · sell-only · no rebuy).

Research / ops observation — does NOT submit orders.
Frozen rule: frozen_sell_gate_spec() in us_tw_sell_only_live_readiness_study.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from research.backtest.us_tw_30m_rolling_risk_gate_study import enrich_poll_30m
from research.backtest.us_tw_rolling_5m_detach_study import (
    build_poll_frame_5m,
    fetch_yahoo_5m_panel,
)
from research.backtest.us_tw_sell_only_live_readiness_study import (
    SellSpec,
    _detach_mask,
    frozen_sell_gate_spec,
)

_TZ = ZoneInfo("Asia/Taipei")
_NQ_LOCAL_ID = "NQ=F"
YELLOW = SellSpec(
    strategy_id="yellow_w-0.6_c2",
    panel="5m30",
    win_thr=-0.6,
    confirm_polls=2,
)


def local_5m_max_trade_date(stock_id: str = _NQ_LOCAL_ID) -> str | None:
    """Latest ``trade_date`` in ``stock_kbar_5m`` for ``stock_id`` (or None)."""
    from stock_db import DEFAULT_DB_PATH, connect

    conn = connect(DEFAULT_DB_PATH)
    try:
        row = conn.execute(
            "SELECT MAX(trade_date) AS mx FROM stock_kbar_5m WHERE stock_id = ?",
            (stock_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    mx = row["mx"] if hasattr(row, "keys") else row[0]
    return str(mx) if mx else None


def ensure_nq_5m_for_session(
    session_date: str | date | None = None,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Upsert Yahoo ``NQ=F`` 5m when local coverage stops before ``session_date``.

    No-op when ``max(trade_date) >= session_date`` unless ``force``.
    Uses the same Yahoo→DB path as ``scripts/tools/backfill_ix0001_finmind_5m.py --seed-nq``.
    """
    day = (
        session_date.isoformat()
        if isinstance(session_date, date)
        else (session_date or date.today().isoformat())
    )
    mx = local_5m_max_trade_date(_NQ_LOCAL_ID)
    if not force and mx is not None and mx >= day:
        return {
            "status": "fresh",
            "stock_id": _NQ_LOCAL_ID,
            "session_date": day,
            "max_trade_date": mx,
            "bars_upserted": 0,
        }

    from stock_db import DEFAULT_DB_PATH, connect
    from stock_db.kbar import upsert_yahoo_5m_rows
    from yahoo_chart_sync import fetch_yahoo_intraday_df, yahoo_intraday_rows_to_db

    end = date.fromisoformat(day)
    start = end - timedelta(days=55)
    try:
        df = fetch_yahoo_intraday_df(_NQ_LOCAL_ID, start, end, interval="5m")
    except Exception as exc:  # noqa: BLE001 — network / yfinance; keep poll alive
        return {
            "status": "error",
            "stock_id": _NQ_LOCAL_ID,
            "session_date": day,
            "max_trade_date": mx,
            "bars_upserted": 0,
            "error": str(exc),
        }

    rows = yahoo_intraday_rows_to_db(_NQ_LOCAL_ID, df, source="yahoo")
    conn = connect(DEFAULT_DB_PATH)
    try:
        n = upsert_yahoo_5m_rows(conn, _NQ_LOCAL_ID, rows) if rows else 0
        conn.commit()
    finally:
        conn.close()

    mx_after = local_5m_max_trade_date(_NQ_LOCAL_ID)
    return {
        "status": "refreshed" if n else "empty",
        "stock_id": _NQ_LOCAL_ID,
        "session_date": day,
        "max_trade_date_before": mx,
        "max_trade_date": mx_after,
        "bars_upserted": int(n),
        "yahoo_rows": len(rows),
    }


def evaluate_session_poll(
    *,
    session_date: str | None = None,
    end: date | None = None,
    red_spec: SellSpec | None = None,
    refresh_nq: bool = True,
) -> dict[str, Any]:
    """Fetch Yahoo 5m, build today's poll frame, return YELLOW/RED state.

    When ``refresh_nq`` is true (default), stale local ``NQ=F`` is upserted from
    Yahoo before the panel load so live Detach polls do not fail on T-1 caches.
    """
    end_d = end or date.today()
    day = session_date or end_d.isoformat()
    red = red_spec or frozen_sell_gate_spec()

    nq_refresh: dict[str, Any] | None = None
    if refresh_nq:
        nq_refresh = ensure_nq_5m_for_session(day)

    tw = fetch_yahoo_5m_panel("^TWII", end_d)
    nq = fetch_yahoo_5m_panel("NQ=F", end_d)
    pf = build_poll_frame_5m(day, tw, nq, window_min=30)
    if pf is None or len(pf) < 7:
        return {
            "ok": False,
            "error": "insufficient_5m_bars",
            "session_date": day,
            "as_of": datetime.now(tz=_TZ).isoformat(timespec="seconds"),
            "strategy_id": red.strategy_id,
            "nq_refresh": nq_refresh,
            "tw_source": tw.attrs.get("panel_source") if tw is not None and len(tw) else None,
            "nq_source": nq.attrs.get("panel_source") if nq is not None and len(nq) else None,
        }

    poll = enrich_poll_30m(pf)
    red_m = _detach_mask(poll, red)
    yel_m = _detach_mask(poll, YELLOW)
    last = poll.iloc[-1]
    last_i = poll.index[-1]

    # first RED / YELLOW today
    red_idxs = list(poll.index[red_m])
    yel_idxs = list(poll.index[yel_m])
    first_red = str(poll.loc[red_idxs[0], "poll"]) if red_idxs else None
    first_yel = str(poll.loc[yel_idxs[0], "poll"]) if yel_idxs else None

    level = "GREEN"
    if bool(red_m.loc[last_i]):
        level = "RED"
    elif bool(yel_m.loc[last_i]):
        level = "YELLOW"
    elif red_idxs:
        level = "RED_LATCHED"  # already hit RED earlier; still elevated
    elif yel_idxs:
        level = "YELLOW_SEEN"

    return {
        "ok": True,
        "schema": "us-tw-5m-sell-gate-v1",
        "as_of": datetime.now(tz=_TZ).isoformat(timespec="seconds"),
        "session_date": day,
        "strategy_id": red.strategy_id,
        "level": level,
        "nq_refresh": nq_refresh,
        "tw_source": tw.attrs.get("panel_source") if tw is not None and len(tw) else None,
        "nq_source": nq.attrs.get("panel_source") if nq is not None and len(nq) else None,
        "action_hint": {
            "GREEN": "hold / no escalate",
            "YELLOW": "watch — detach building",
            "YELLOW_SEEN": "watched earlier; now quiet — stay alert",
            "RED": "HUMAN decide flatten (sell-only · no rebuy)",
            "RED_LATCHED": "RED already fired earlier today — review exposure",
        }.get(level, "review"),
        "auto_order": False,
        "rebuy": False,
        "latest": {
            "poll": str(last["poll"]),
            "tw_from_open": round(float(last["tw_from_open"]), 4),
            "nq_from_open": round(float(last["nq_from_open"]), 4),
            "spread_30m": round(float(last["spread_30m"]), 4),
            "gap_pulse": str(last.get("gap_pulse", "")),
            "sync_pulse": str(last.get("sync_pulse", "")),
            "red_now": bool(red_m.loc[last_i]),
            "yellow_now": bool(yel_m.loc[last_i]),
        },
        "first_yellow_poll": first_yel,
        "first_red_poll": first_red,
        "rule": {
            "red": (
                f"spread_30m≤{red.win_thr} ×{red.confirm_polls} · "
                f"tw_from_open≤{red.max_tw_from_open} · "
                f"arm {red.arm_after}–{red.arm_before}"
            ),
            "yellow": f"spread_30m≤{YELLOW.win_thr} ×{YELLOW.confirm_polls}",
        },
        "timeline_tail": [
            {
                "poll": str(r["poll"]),
                "tw_from_open": round(float(r["tw_from_open"]), 4),
                "nq_from_open": round(float(r["nq_from_open"]), 4),
                "spread_30m": round(float(r["spread_30m"]), 4),
                "red": bool(red_m.loc[i]),
                "yellow": bool(yel_m.loc[i]),
            }
            for i, r in poll.tail(12).iterrows()
        ],
    }


def write_latest_snapshot(payload: dict[str, Any], *, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    day = str(payload.get("session_date") or date.today().isoformat()).replace("-", "")
    path = out_dir / f"us_tw_5m_sell_gate_{day}.json"
    latest = out_dir / "us_tw_5m_sell_gate_latest.json"
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    path.write_text(text, encoding="utf-8")
    latest.write_text(text, encoding="utf-8")
    return path
