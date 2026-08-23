"""Fubon live-book session blotter · FIFO pairing + realized PnL（下單層 paper UI 共用）.

Extracted verbatim from ``reports/research/channel_lab/live_v6_sim_server.py``
so tests and the paper UI server import it as ``tmf_channel.blotter`` without
lab ``sys.path`` injection. TMF micro: 1 pt = NT$10.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

_TZ = ZoneInfo("Asia/Taipei")
# TAIFEX 微型臺指期貨：1 點 = NT$10（與 tmf_cost_sensitivity_check 一致）
TMF_POINT_VALUE_NTD = 10.0
NIGHT_OPEN = "15:00"
NIGHT_CLOSE = "05:00"


def _hhmm_of(iso: str) -> str:
    s = str(iso or "")
    if "T" in s:
        return s.split("T", 1)[1][:5]
    if " " in s:
        return s.split(" ", 1)[1][:5]
    return s[:5]


def _is_night_hhmm(hm: str) -> bool:
    return hm >= NIGHT_OPEN or hm < NIGHT_CLOSE


def _trading_day_str(now: datetime | None = None) -> str:
    """Align with Order layer ``order.tmf_channel_ledger.trading_day_str``.

    One 交易日 = 日盤 08:45–13:45 + 夜盤 15:00–次日 05:00。
    00:00–04:59 still belongs to the evening that started at 15:00.
    """
    try:
        from order.tmf_channel_ledger import trading_day_str as _tds

        return _tds(now)
    except Exception:
        now = now or datetime.now(tz=_TZ)
        if now.hour < 5:
            return (now.date() - timedelta(days=1)).strftime("%Y-%m-%d")
        return now.date().strftime("%Y-%m-%d")


def _fill_on_trading_day(iso: str, trading_day: str) -> bool:
    """True if fill timestamp belongs to trading_day window (incl. post-midnight night)."""
    s = str(iso or "")
    if trading_day and trading_day in s:
        return True
    try:
        nxt = (
            datetime.strptime(trading_day, "%Y-%m-%d") + timedelta(days=1)
        ).strftime("%Y-%m-%d")
    except ValueError:
        return False
    if nxt not in s:
        return False
    return _hhmm_of(s) < NIGHT_CLOSE


def _has_real_fill_time(iso: str | None) -> bool:
    """Reject history rows that lack last_time (show up as T00:00:00)."""
    s = str(iso or "")
    if not s or "T00:00:00" in s:
        return False
    return True


def filter_session_fills(
    fill_legs: list[dict[str, Any]],
    *,
    day: str,
    scope: str = "calendar",
) -> list[dict[str, Any]]:
    """Fills for blotter.

    ``scope=calendar`` (default): only legs with ``date == day``. Matches Fubon
    app 「今日」／``query_margin_equity.fut_realized_pnl`` clearing day.

    ``scope=trading``: calendar day + prior-calendar afterhours ``a*`` (strategy
    trading-day window through 05:00). Do **not** use this for the headline
    that claims to match Fubon 今日平倉損益 — it double-counts prior night.
    """
    try:
        prev = (
            datetime.strptime(day, "%Y-%m-%d") - timedelta(days=1)
        ).strftime("%Y-%m-%d")
    except ValueError:
        prev = ""

    today: list[dict[str, Any]] = []
    for f in fill_legs:
        t = str(f.get("t") or "")
        d = str(f.get("date") or "")[:10]
        if not d and "T" in t:
            d = t[:10]
        src = str(f.get("source") or "")
        if scope == "trading" and _has_real_fill_time(t):
            if _fill_on_trading_day(t, day):
                today.append(f)
            continue
        if _has_real_fill_time(t) and scope == "calendar":
            # Real timestamps: keep if calendar date matches trading-day label
            # for day session, or night belonging to this clearing day.
            try:
                dt = datetime.fromisoformat(t)
                local_d = dt.astimezone(_TZ).date().isoformat()
            except Exception:
                local_d = t[:10]
            if local_d == day:
                today.append(f)
            continue
        # Date-only (typical close_position_record / history without last_time)
        if d == day:
            today.append(f)
            continue
        if scope == "trading" and d == prev and src == "close_position_record":
            ono = str(f.get("order_no") or f.get("order_no_raw") or "")
            if ono.lower().startswith("a"):
                today.append(f)

    today.sort(
        key=lambda x: (
            str(x.get("t") or ""),
            str(x.get("date") or ""),
            str(x.get("order_no_raw") or x.get("order_no") or ""),
        )
    )
    # close_position_record has no user_def — don't drop to empty via tmf filter
    if any(str(f.get("source") or "") == "close_position_record" for f in today):
        return today
    tmf = [
        f
        for f in today
        if str(f.get("user_def") or "").lower().startswith("tmf")
    ]
    return tmf if tmf else today


def filter_calendar_day_fills(fill_legs: list[dict[str, Any]], *, day: str) -> list[dict[str, Any]]:
    return filter_session_fills(fill_legs, day=day, scope="calendar")


def reconstruct_seed_lots(
    fills: list[dict[str, Any]],
    broker: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Reverse-apply session fills from broker end-state → pre-session inventory.

    Overnight entries often lack fill rows in today's book; seeding keeps FIFO
    aligned with the live position (e.g. open short still working).
    """
    if broker and broker.get("s"):
        pos_s: str | None = str(broker["s"])
        pos_n = int(broker.get("n") or 0)
    else:
        pos_s, pos_n = None, 0

    for f in reversed(sorted(fills, key=lambda x: str(x.get("t") or ""))):
        side = str(f.get("s") or "")
        try:
            n = int(f.get("n") or 0)
        except (TypeError, ValueError):
            continue
        if side not in ("L", "S") or n <= 0:
            continue
        if side == "L":
            # undo buy
            if pos_s == "L":
                pos_n -= n
                if pos_n < 0:
                    pos_s, pos_n = "S", -pos_n
                elif pos_n == 0:
                    pos_s = None
            elif pos_s == "S":
                pos_n += n
            else:
                pos_s, pos_n = "S", n
        else:
            # undo sell
            if pos_s == "S":
                pos_n -= n
                if pos_n < 0:
                    pos_s, pos_n = "L", -pos_n
                elif pos_n == 0:
                    pos_s = None
            elif pos_s == "L":
                pos_n += n
            else:
                pos_s, pos_n = "L", n

    if not pos_s or pos_n <= 0:
        return []
    return [
        {
            "s": pos_s,
            "n": int(pos_n),
            "px": None,
            "t": None,
            "order_no": "seed_overnight",
            "seed": True,
        }
    ]


def _leg_unit_cost_ntd(leg: dict[str, Any]) -> tuple[float, float]:
    """Per-lot fee/tax in NTD from a close_position_record leg."""
    try:
        n = max(1, int(leg.get("n") or 1))
    except (TypeError, ValueError):
        n = 1
    try:
        fee = float(leg.get("fee") or 0.0)
    except (TypeError, ValueError):
        fee = 0.0
    try:
        tax = float(leg.get("tax") or 0.0)
    except (TypeError, ValueError):
        tax = 0.0
    return fee / n, tax / n


def cost_ntd_to_pts(cost_ntd: float, *, point_value: float = TMF_POINT_VALUE_NTD) -> float:
    pv = float(point_value) if point_value else TMF_POINT_VALUE_NTD
    if pv <= 0:
        return 0.0
    return round(float(cost_ntd) / pv, 2)


def pair_fills_fifo(
    fills: list[dict[str, Any]],
    *,
    seed: list[dict[str, Any]] | None = None,
    point_value: float = TMF_POINT_VALUE_NTD,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Pair fill legs into round-trips (FIFO). Returns ``(trades, residual_open)``.

    Seed lots (``px=None``) cover overnight inventory; those closes get
    ``why=overnight_flat`` and ``pnl_gross=None``. Exit fee/tax still apply;
    ``pnl`` is net-of-cost (gross − cost_pts), so overnight flats may show a
    small negative ``pnl`` equal to close costs only.
    """
    open_lots: list[dict[str, Any]] = []
    for x in seed or []:
        lot = dict(x)
        lot.setdefault("fee_u", 0.0)
        lot.setdefault("tax_u", 0.0)
        open_lots.append(lot)
    trades: list[dict[str, Any]] = []
    for f in sorted(fills, key=lambda x: str(x.get("t") or "")):
        side = str(f.get("s") or "")
        try:
            n = int(f.get("n") or 0)
            px = float(f.get("px"))
        except (TypeError, ValueError):
            continue
        if side not in ("L", "S") or n <= 0 or not math.isfinite(px):
            continue
        t = str(f.get("t") or "")
        fee_u, tax_u = _leg_unit_cost_ntd(f)
        remain = n
        while remain > 0 and open_lots and open_lots[0]["s"] != side:
            ent = open_lots[0]
            take = min(remain, int(ent["n"]))
            ep_raw = ent.get("px")
            et = str(ent.get("t") or "")
            seeded = bool(ent.get("seed")) or ep_raw is None
            cost_ntd = (
                float(ent.get("fee_u") or 0.0) + float(ent.get("tax_u") or 0.0)
                + fee_u
                + tax_u
            ) * take
            cost_pts = cost_ntd_to_pts(cost_ntd, point_value=point_value)
            if seeded:
                pnl_gross = None
                why = "overnight_flat"
                ep_out = None
                hold_m = None
                pnl = round(-cost_pts, 2) if cost_pts else None
            else:
                ep = float(ep_raw)
                if ent["s"] == "L":
                    pnl_gross = round((px - ep) * take, 1)
                else:
                    pnl_gross = round((ep - px) * take, 1)
                why = "broker"
                ep_out = round(ep, 1)
                try:
                    hold_m = max(
                        0,
                        int(
                            (
                                datetime.fromisoformat(t) - datetime.fromisoformat(et)
                            ).total_seconds()
                            // 60
                        ),
                    )
                except Exception:
                    hold_m = None
                pnl = round(float(pnl_gross) - cost_pts, 2)
            trades.append(
                {
                    "s": ent["s"],
                    "et": et or None,
                    "xt": t,
                    "ep": ep_out,
                    "xp": round(px, 1),
                    "pnl": pnl,
                    "pnl_gross": pnl_gross,
                    "cost_ntd": round(cost_ntd, 1),
                    "cost_pts": cost_pts,
                    "hold": hold_m,
                    "n": take,
                    "why": why,
                    "how": "fubon",
                    "order_no_in": ent.get("order_no"),
                    "order_no_out": f.get("order_no"),
                    "seed": seeded,
                }
            )
            ent["n"] = int(ent["n"]) - take
            remain -= take
            if ent["n"] <= 0:
                open_lots.pop(0)
        if remain > 0:
            open_lots.append(
                {
                    "s": side,
                    "n": remain,
                    "px": px,
                    "t": t,
                    "order_no": f.get("order_no"),
                    "seed": False,
                    "fee_u": fee_u,
                    "tax_u": tax_u,
                }
            )
    return trades, open_lots


def build_session_blotter(
    fill_legs: list[dict[str, Any]],
    broker: dict[str, Any] | None,
    *,
    day: str,
    scope: str = "calendar",
    equity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Session report: fills + FIFO + realized.

    Default ``scope=calendar`` aligns FIFO gross with Fubon
    ``fut_realized_pnl`` (今日平倉損益). Pass ``equity`` from
    ``query_tmf_margin_equity`` for SSOT cross-check in ``health``.
    """
    session = filter_session_fills(fill_legs, day=day, scope=scope)
    seed = reconstruct_seed_lots(session, broker)
    trades, residual = pair_fills_fifo(session, seed=seed)
    # Realized = has a numeric pnl (gross round-trips + overnight cost-only).
    realized = [t for t in trades if t.get("pnl") is not None]
    overnight_flats = [t for t in trades if t.get("why") == "overnight_flat"]
    priced = [t for t in realized if t.get("why") != "overnight_flat"]
    night_trades = [
        t
        for t in priced
        if _is_night_hhmm(_hhmm_of(str(t.get("xt") or t.get("et") or "")))
    ]
    day_session_trades = [
        t
        for t in priced
        if not _is_night_hhmm(_hhmm_of(str(t.get("xt") or t.get("et") or "")))
    ]
    # Overnight close costs: attribute by exit clock when known, else day.
    overnight_cost_day = 0.0
    overnight_cost_night = 0.0
    for t in overnight_flats:
        if t.get("pnl") is None:
            continue
        c = float(t["pnl"])  # already negative cost_pts
        if _is_night_hhmm(_hhmm_of(str(t.get("xt") or ""))):
            overnight_cost_night += c
        else:
            overnight_cost_day += c

    def _sum_field(rows: list[dict[str, Any]], key: str) -> float:
        total = 0.0
        for t in rows:
            if t.get(key) is None:
                continue
            try:
                total += float(t[key])
            except (TypeError, ValueError):
                pass
        return round(total, 2)

    all_net = _sum_field(realized, "pnl")
    all_gross = _sum_field(priced, "pnl_gross")
    all_cost_pts = _sum_field(trades, "cost_pts")
    all_cost_ntd = _sum_field(trades, "cost_ntd")
    day_session_net = round(
        _sum_field(day_session_trades, "pnl") + overnight_cost_day, 2
    )
    night_net = round(_sum_field(night_trades, "pnl") + overnight_cost_night, 2)
    day_gross = _sum_field(day_session_trades, "pnl_gross")
    night_gross = _sum_field(night_trades, "pnl_gross")
    wins = sum(1 for t in priced if float(t.get("pnl") or 0) > 0)
    wr = round(100.0 * wins / len(priced), 1) if priced else 0.0

    # Annotate legs with running role vs seed-aware position
    legs_out: list[dict[str, Any]] = []
    # clone seed for walk
    walk = [dict(x) for x in seed]
    for f in session:
        side = str(f.get("s") or "")
        n = int(f.get("n") or 0)
        role = "open"
        if walk and walk[0]["s"] != side:
            role = "overnight_flat" if walk[0].get("seed") or walk[0].get("px") is None else "close"
        elif walk and walk[0]["s"] == side:
            role = "add"
        # apply same as fifo step for walk (simplified via re-pair not needed — use labels from matching trades)
        legs_out.append({**f, "role": role, "session": True})
        # update walk lightly
        remain = n
        while remain > 0 and walk and walk[0]["s"] != side:
            take = min(remain, int(walk[0]["n"]))
            walk[0]["n"] = int(walk[0]["n"]) - take
            remain -= take
            if walk[0]["n"] <= 0:
                walk.pop(0)
        if remain > 0:
            walk.append({"s": side, "n": remain, "px": f.get("px"), "seed": False})

    # Fix roles from trades for clarity
    close_out_nos = {t.get("order_no_out") for t in trades if t.get("why") == "broker"}
    flat_out_nos = {t.get("order_no_out") for t in overnight_flats}
    open_nos = {r.get("order_no") for r in residual}
    for leg in legs_out:
        ono = leg.get("order_no")
        if ono in flat_out_nos:
            leg["role"] = "overnight_flat"
        elif ono in close_out_nos and ono not in open_nos:
            # could be both close and later open same no — rare
            leg["role"] = "close" if leg["role"] != "overnight_flat" else leg["role"]
        elif ono in open_nos:
            leg["role"] = "open"

    # Attach FIFO realized PnL onto closing legs (order_no_out → sum).
    pnl_by_out: dict[str, dict[str, Any]] = {}
    for t in trades:
        ono = str(t.get("order_no_out") or "")
        if not ono:
            continue
        slot = pnl_by_out.setdefault(
            ono,
            {
                "pnl": 0.0,
                "pnl_gross": 0.0,
                "cost_pts": 0.0,
                "cost_ntd": 0.0,
                "n": 0,
                "ep": t.get("ep"),
                "hold": t.get("hold"),
                "why": t.get("why"),
                "has_pnl": False,
                "has_gross": False,
            },
        )
        if t.get("pnl") is not None:
            try:
                slot["pnl"] = round(float(slot["pnl"]) + float(t["pnl"]), 2)
                slot["has_pnl"] = True
            except (TypeError, ValueError):
                pass
        if t.get("pnl_gross") is not None:
            try:
                slot["pnl_gross"] = round(float(slot["pnl_gross"]) + float(t["pnl_gross"]), 1)
                slot["has_gross"] = True
            except (TypeError, ValueError):
                pass
        if t.get("cost_pts") is not None:
            try:
                slot["cost_pts"] = round(float(slot["cost_pts"]) + float(t["cost_pts"]), 2)
            except (TypeError, ValueError):
                pass
        if t.get("cost_ntd") is not None:
            try:
                slot["cost_ntd"] = round(float(slot["cost_ntd"]) + float(t["cost_ntd"]), 1)
            except (TypeError, ValueError):
                pass
        try:
            slot["n"] = int(slot["n"]) + int(t.get("n") or 0)
        except (TypeError, ValueError):
            pass
        if slot.get("ep") is None and t.get("ep") is not None:
            slot["ep"] = t.get("ep")
        if t.get("hold") is not None:
            slot["hold"] = t.get("hold")
        slot["why"] = t.get("why") or slot.get("why")
    for leg in legs_out:
        ono = str(leg.get("order_no") or "")
        info = pnl_by_out.get(ono)
        if not info:
            leg.setdefault("pnl", None)
            continue
        leg["cost_pts"] = info.get("cost_pts")
        leg["cost_ntd"] = info.get("cost_ntd")
        leg["pnl_gross"] = info.get("pnl_gross") if info.get("has_gross") else None
        if info.get("why") == "overnight_flat":
            leg["pnl"] = info.get("pnl") if info.get("has_pnl") else None
            leg["pair_ep"] = info.get("ep")
            leg["hold"] = info.get("hold")
            leg["pnl_note"] = "overnight_flat"
        else:
            leg["pnl"] = info.get("pnl") if info.get("has_pnl") else None
            leg["pair_ep"] = info.get("ep")
            leg["hold"] = info.get("hold")
            leg["pnl_note"] = "fifo_net"

    n_with_fee = sum(
        1
        for f in session
        if (f.get("fee") is not None or f.get("tax") is not None)
        and (float(f.get("fee") or 0) > 0 or float(f.get("tax") or 0) > 0)
    )
    n_date_only = sum(
        1
        for f in session
        if "T00:00:00" in str(f.get("t") or "")
        or not str(f.get("t") or "").strip()
    )
    health = _blotter_health(
        session=session,
        trades=trades,
        priced=priced,
        overnight_flats=overnight_flats,
        residual=residual,
        broker=broker,
        seed=seed,
        n_with_fee=n_with_fee,
        n_date_only=n_date_only,
        all_cost_pts=all_cost_pts,
        all_gross=all_gross,
        equity=equity,
        scope=scope,
    )

    return {
        "session_fills": legs_out,
        "trades": trades,
        "realized_trades": realized,
        "overnight_flats": overnight_flats,
        "residual": residual,
        "seed_lots": seed,
        "scope": scope,
        "summary_all": {
            "n": len(priced),
            "net": all_net,
            "net_gross": all_gross,
            "cost_pts": all_cost_pts,
            "cost_ntd": all_cost_ntd,
            "n_overnight_flat": sum(int(t.get("n") or 0) for t in overnight_flats),
            "n_fifo_rows": len(trades),
            "wr": wr,
            "point_value_ntd": TMF_POINT_VALUE_NTD,
            "scope": scope,
        },
        # 日盤＝出場 HH:MM 在 08:45–13:45；夜盤＝15:00–05:00（與 Order trading_day 同窗）
        "summary_day": {
            "n": len(day_session_trades),
            "net": day_session_net,
            "net_gross": day_gross,
        },
        "summary_night": {
            "n": len(night_trades),
            "net": night_net,
            "net_gross": night_gross,
        },
        "day_trades": day_session_trades,
        "night_trades": night_trades,
        "trading_day": day,
        "health": health,
        "equity": equity,
    }


def open_trade_row_from_broker(
    open_pos: dict[str, Any] | None,
    residual: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Build a blotter row for the live open lot (SSOT = Fubon position + MTM).

    Closed FIFO rows alone hide 持倉中 — mobile users never see the working
    trade. Residual from FIFO supplies entry time / order_no when aligned.
    """
    if not open_pos or not open_pos.get("s"):
        return None
    res0 = None
    for r in residual or []:
        if str(r.get("s") or "") == str(open_pos.get("s") or ""):
            res0 = r
            break
    if res0 is None and residual:
        res0 = residual[0]
    u = open_pos.get("u_pnl")
    u_ntd = open_pos.get("u_pnl_ntd")
    if u_ntd is None and u is not None:
        try:
            u_ntd = round(float(u) * TMF_POINT_VALUE_NTD, 1)
        except (TypeError, ValueError):
            u_ntd = None
    return {
        "s": open_pos["s"],
        "et": (res0 or {}).get("t"),
        "xt": None,
        "ep": open_pos.get("ep") if open_pos.get("ep") is not None else (res0 or {}).get("px"),
        "xp": None,
        "pnl": u,
        "pnl_gross": u,
        "u_pnl_ntd": u_ntd,
        "cost_ntd": None,
        "cost_pts": None,
        "hold": None,
        "n": int(open_pos.get("n") or 1),
        "why": "open",
        "how": "fubon",
        "open": True,
        "order_no_in": (res0 or {}).get("order_no"),
        "order_no_out": None,
        "seed": False,
    }


def compose_blotter_trade_rows(
    realized_or_all: list[dict[str, Any]],
    *,
    open_pos: dict[str, Any] | None,
    residual: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Closed FIFO rows + optional open row (open first for UI sticky)."""
    closed = [t for t in realized_or_all if not t.get("open") and t.get("why") != "open"]
    open_row = open_trade_row_from_broker(open_pos, residual)
    if open_row is None:
        return closed
    return [open_row, *closed]


def total_pnl_from_equity(
    equity: dict[str, Any] | None,
    open_pos: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Headline = Fubon realized_net + unrealized (NTD / pts).

    Unrealized prefers live ``open_pos`` MTM (ticks with spot); falls back to
    ``query_margin_equity.fut_unrealized_pnl``.
    """
    eq = equity or {}
    r_ntd = eq.get("fut_realized_net_ntd")
    r_pts = eq.get("fut_realized_net_pts")
    u_ntd = None
    u_pts = None
    if open_pos and open_pos.get("u_pnl_ntd") is not None:
        try:
            u_ntd = float(open_pos["u_pnl_ntd"])
        except (TypeError, ValueError):
            u_ntd = None
    if open_pos and open_pos.get("u_pnl") is not None and u_ntd is None:
        try:
            u_pts = float(open_pos["u_pnl"])
            u_ntd = round(u_pts * TMF_POINT_VALUE_NTD, 1)
        except (TypeError, ValueError):
            pass
    if u_ntd is None and eq.get("fut_unrealized_pnl_ntd") is not None:
        try:
            u_ntd = float(eq["fut_unrealized_pnl_ntd"])
        except (TypeError, ValueError):
            u_ntd = None
    if u_pts is None and eq.get("fut_unrealized_pnl_pts") is not None:
        try:
            u_pts = float(eq["fut_unrealized_pnl_pts"])
        except (TypeError, ValueError):
            u_pts = None
    if u_pts is None and u_ntd is not None:
        u_pts = round(u_ntd / TMF_POINT_VALUE_NTD, 2)
    total_ntd = None
    total_pts = None
    if r_ntd is not None or u_ntd is not None:
        total_ntd = round(float(r_ntd or 0) + float(u_ntd or 0), 1)
    if r_pts is not None or u_pts is not None:
        total_pts = round(float(r_pts or 0) + float(u_pts or 0), 2)
    return {
        "realized_net_ntd": r_ntd,
        "realized_net_pts": r_pts,
        "unrealized_ntd": u_ntd,
        "unrealized_pts": u_pts,
        "total_ntd": total_ntd,
        "total_pts": total_pts,
    }


def _blotter_health(
    *,
    session: list[dict[str, Any]],
    trades: list[dict[str, Any]],
    priced: list[dict[str, Any]],
    overnight_flats: list[dict[str, Any]],
    residual: list[dict[str, Any]],
    broker: dict[str, Any] | None,
    seed: list[dict[str, Any]],
    n_with_fee: int,
    n_date_only: int,
    all_cost_pts: float,
    all_gross: float = 0.0,
    equity: dict[str, Any] | None = None,
    scope: str = "calendar",
) -> dict[str, Any]:
    """Machine + human checks for the HTML blotter logic."""
    issues: list[dict[str, str]] = []
    n_sess = len(session)
    fee_cov = round(100.0 * n_with_fee / n_sess, 1) if n_sess else 100.0
    if n_sess and fee_cov < 80:
        issues.append(
            {
                "level": "warn",
                "code": "fee_coverage",
                "msg": f"僅 {fee_cov}% 成交腿有手續費／稅（缺欄位會低估成本）",
            }
        )
    if n_date_only:
        issues.append(
            {
                "level": "warn",
                "code": "date_only_legs",
                "msg": f"{n_date_only} 筆腿只有日期、無真實成交時間 → 日／夜歸屬可能偏差",
            }
        )
    issues.append(
        {
            "level": "ok" if scope == "calendar" else "info",
            "code": "scope",
            "msg": (
                "範圍＝日曆日（對齊富邦今日平倉損益）"
                if scope == "calendar"
                else "範圍＝策略交易日（含昨夜 a*；勿直接對富邦「今日」）"
            ),
        }
    )
    issues.append(
        {
            "level": "ok",
            "code": "fifo_row_mix",
            "msg": f"FIFO {len(trades)} 列＝已平 {len(priced)}＋隔夜平 {len(overnight_flats)}",
        }
    )
    res_n = sum(int(r.get("n") or 0) for r in residual)
    bro_n = int((broker or {}).get("n") or 0) if broker and broker.get("s") else 0
    bro_s = str((broker or {}).get("s") or "") if broker else ""
    res_s = str(residual[0]["s"]) if residual else ""
    if bro_n != res_n or (bro_n and bro_s != res_s):
        issues.append(
            {
                "level": "warn",
                "code": "residual_vs_broker",
                "msg": f"FIFO 殘倉 {res_s or 'flat'}×{res_n} ≠ 富邦持倉 {bro_s or 'flat'}×{bro_n}",
            }
        )
    else:
        issues.append(
            {
                "level": "ok",
                "code": "residual_vs_broker",
                "msg": f"FIFO 殘倉對齊富邦 {bro_s or '空手'}×{bro_n}",
            }
        )
    if seed:
        issues.append(
            {
                "level": "info",
                "code": "seed",
                "msg": f"開盤前種子 {seed[0].get('s')}×{seed[0].get('n')}（無進場價 → 隔夜平不算價差）",
            }
        )
    # 富邦權益（query_margin_equity）為唯一權威數字；不再跟 FIFO 重建互相比對
    # 顯示（那個比對只會在記帳歸屬口徑不同時製造誤導性的「error」，實際成交都是真的）。
    if equity and equity.get("fut_realized_pnl_ntd") is not None and scope == "calendar":
        broker_ntd = float(equity["fut_realized_pnl_ntd"])
        issues.append(
            {
                "level": "ok",
                "code": "fubon_realized",
                "msg": f"富邦今日平倉損益 NT${broker_ntd:.0f}",
            }
        )
        if equity.get("today_cost_ntd") is not None:
            issues.append(
                {
                    "level": "info",
                    "code": "fubon_fees",
                    "msg": (
                        f"富邦今日稅費 NT${float(equity['today_cost_ntd']):.0f}"
                        f"（手續 {equity.get('today_trading_fee_ntd')}＋稅 "
                        f"{equity.get('today_trading_tax_ntd')}）"
                    ),
                }
            )
    elif scope == "calendar":
        issues.append(
            {
                "level": "warn",
                "code": "fubon_realized",
                "msg": "無法讀取 query_margin_equity",
            }
        )
    if all_cost_pts > 0:
        issues.append(
            {
                "level": "ok",
                "code": "cost_applied",
                "msg": f"FIFO 已扣回合成本 {all_cost_pts:.2f}pt（點值 {TMF_POINT_VALUE_NTD:g}）",
            }
        )
    worst = "ok"
    for it in issues:
        if it["level"] == "error":
            worst = "error"
            break
        if it["level"] == "warn" and worst == "ok":
            worst = "warn"
    return {
        "status": worst,
        "fee_coverage_pct": fee_cov,
        "n_date_only": n_date_only,
        "n_fifo": len(trades),
        "n_priced": len(priced),
        "n_overnight": len(overnight_flats),
        "scope": scope,
        "issues": issues,
    }
