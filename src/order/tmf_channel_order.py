"""TMF micro-channel Order sleeve · desired-state reconciler.

Poll loop (not full event replay):
  candles → Final v1.1.1 simulate → want rails/position → diff vs broker → place/cancel.

Safety: dry_run default; ORDER_MASTER_ENABLED; per-sleeve flags; day API + PnL kill.
Order layer must not be imported by strategy scripts; this module pulls the lab
engine via path (same pattern as other research→order sleeves).
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from stock_db import PROJECT_ROOT

from order.fubon_futopt_orders import (
    FutOptResolvedOrder,
    cancel_futopt_order,
    get_futopt_order_results,
    is_tmf_acct_symbol,
    market_type_for_hhmm,
    pick_futopt_account,
    place_futopt_order,
    query_tmf_broker_net,
)
from order.fubon_session import connect_fubon
from order.tmf_channel_config import TmfChannelOrderConfig, load_tmf_channel_order_config
from order.tmf_channel_ledger import load_ledger, record_actions, roll_day, save_ledger
from order.tmf_channel_marketdata import (
    bars_to_arrays,
    fetch_1m_bars,
    in_tmf_trade_window,
    resolve_front_symbol,
    session_hhmm_now,
)

_TZ = ZoneInfo("Asia/Taipei")
_LAB = PROJECT_ROOT / "reports" / "research" / "channel_lab"


def _ensure_lab_import() -> None:
    p = str(_LAB)
    if p not in sys.path:
        sys.path.insert(0, p)


def _side_to_bs(side: str) -> str:
    # strategy S = short = Sell; L = long = Buy
    return "Sell" if side == "S" else "Buy"


def _parse_order_side(item: Any) -> str | None:
    bs = getattr(item, "buy_sell", None)
    name = str(getattr(bs, "name", bs) or "").lower()
    if "buy" in name:
        return "L"
    if "sell" in name:
        return "S"
    return None


def _parse_order_px(item: Any) -> float | None:
    for k in ("price", "order_price", "ord_price"):
        v = getattr(item, k, None)
        if v is None:
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return None


def _is_working(item: Any) -> bool:
    st = getattr(item, "status", None)
    try:
        si = int(st)
    except (TypeError, ValueError):
        name = str(getattr(st, "name", st) or "").lower()
        return any(x in name for x in ("open", "work", "new", "partial", "0", "10"))
    # Align with stock: 0/10 often working
    return si in (0, 10)


def desired_from_simulate(
    bars: list[dict],
    *,
    day: str,
    recipe: dict[str, Any],
) -> dict[str, Any]:
    _ensure_lab_import()
    from jack_channel_v6_pv import load_vixtwn_delta, simulate  # noqa: WPS433

    if len(bars) < 20:
        return dict(ok=False, reason="bars_lt_20")
    O, H, L, C, V, T = bars_to_arrays(day, bars)
    vix = load_vixtwn_delta()
    trades, events, ws, wl, rvol, regime, open_pos = simulate(
        O, H, L, C, V, T, recipe, vix_delta=vix or {}
    )
    return dict(
        ok=True,
        want_s=ws[-1] if ws else None,
        want_l=wl[-1] if wl else None,
        open_pos=open_pos,
        trades=trades,
        events=events,
        spot=float(C[-1]),
        last_t=bars[-1]["t"],
        regime=regime[-1] if regime else None,
    )


def reconcile_once(
    cfg: TmfChannelOrderConfig | None = None,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """One poll tick. Returns summary dict (always JSON-serializable)."""
    cfg = cfg or load_tmf_channel_order_config()
    hm = session_hhmm_now()
    day = datetime.now(tz=_TZ).strftime("%Y-%m-%d")
    out: dict[str, Any] = {
        "ok": False,
        "strategy_id": cfg.strategy_id,
        "dry_run": cfg.dry_run,
        "order_enabled": cfg.order_enabled,
        "auto_submit": cfg.auto_submit,
        "hhmm": hm,
        "day": day,
        "actions": [],
    }

    def _finish(payload: dict[str, Any], ledger_obj: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            from order.tmf_channel_broadcast import emit_from_summary

            payload["broadcast"] = emit_from_summary(payload, cfg=cfg, ledger=ledger_obj)
        except Exception as exc:  # never block order path on UI snapshot
            payload["broadcast_error"] = str(exc)[:160]
        return payload

    if not force and not in_tmf_trade_window(hm):
        out["reason"] = "outside_session"
        return _finish(out)
    if not cfg.order_enabled and not cfg.dry_run:
        out["reason"] = "ORDER_TMF_CHANNEL_ENABLED=0"
        return _finish(out)

    ledger = roll_day(load_ledger(cfg.ledger_path))
    if ledger.get("killed"):
        out["reason"] = f"killed:{ledger.get('kill_reason')}"
        out["ledger"] = {k: ledger.get(k) for k in ("day", "api_calls_day", "day_pnl_pts", "killed")}
        # Stopgap (2026-08-05): TMF has no broker-side stop orders — every exit
        # (trail/structure/stop_pts) only fires because reconcile_once runs.
        # Freezing entirely while killed leaves any open position fully
        # unprotected until the next trading day's roll_day resets `killed`
        # (~05:00) — a naked, unbounded exposure. Flatten once here (idempotent:
        # re-attempted every poll while still killed, in case this attempt
        # fails) rather than the fuller "block new entries only" redesign,
        # which still needs simulate()-level review and orphan-path coverage.
        try:
            session = connect_fubon(realtime=True)
            acc = pick_futopt_account(session)
            sym, _name, _end = resolve_front_symbol(session, product=cfg.product)
            broker_live = query_tmf_broker_net(session, acc=acc, front_symbol=sym)
            if broker_live and int(broker_live.get("n") or 0) > 0:
                side = str(broker_live["s"])
                lot = int(broker_live["n"])
                mt_name = (
                    "future_night" if "Night" in str(market_type_for_hhmm(hm)) else "future"
                )
                resolved = FutOptResolvedOrder(
                    symbol=sym,
                    buy_sell=_side_to_bs("S" if side == "L" else "L"),
                    lot=lot,
                    price=None,
                    price_type="market",
                    time_in_force="ioc",
                    order_type="close",
                    market_type=mt_name,
                    user_def=cfg.user_def,
                    session_date=day,
                )
                act = {
                    "kind": "exit_market",
                    "side": side,
                    "lot": lot,
                    "why": "kill_switch_flatten",
                    "counts_api": True,
                }
                try:
                    place_futopt_order(session, resolved, acc=acc, dry_run=cfg.dry_run)
                    act["ok"] = True
                except Exception as e:
                    act["ok"] = False
                    act["error"] = str(e)[:200]
                ledger = record_actions(ledger, [act], api_n=1 if act.get("ok") else 0)
                save_ledger(cfg.ledger_path, ledger)
                out["kill_flatten_action"] = act
                out["ledger"]["api_calls_day"] = ledger.get("api_calls_day")
        except Exception as e:
            out["kill_flatten_error"] = str(e)[:200]
        return _finish(out, ledger)
    if cfg.max_api_per_day > 0 and int(ledger.get("api_calls_day") or 0) >= cfg.max_api_per_day:
        ledger["killed"] = True
        ledger["kill_reason"] = f"api_day>={cfg.max_api_per_day}"
        save_ledger(cfg.ledger_path, ledger)
        out["reason"] = ledger["kill_reason"]
        return _finish(out, ledger)

    session = connect_fubon(realtime=True)
    acc = pick_futopt_account(session)
    sym, name, end = resolve_front_symbol(session, product=cfg.product)
    bars = fetch_1m_bars(session, sym)
    desired = desired_from_simulate(bars, day=day, recipe=cfg.recipe)
    if not desired.get("ok"):
        out["reason"] = desired.get("reason") or "simulate_failed"
        return _finish(out, ledger)

    want_s = desired.get("want_s")
    want_l = desired.get("want_l")
    open_pos = desired.get("open_pos")
    spot = float(desired["spot"])
    mt = market_type_for_hhmm(hm)
    mt_name = "future_night" if "Night" in str(mt) else "future"

    broker_live = None
    try:
        broker_live = query_tmf_broker_net(session, acc=acc, front_symbol=sym)
    except Exception as e:
        out["broker_query_error"] = str(e)[:200]

    # Ghost sim position: bar engine still "in pos" after we flattened broker
    # (or external close). Order layer must trust broker, else wants go None and
    # we cancel the flat dual hangs that were correct for a flat book.
    ghost_sim_pos = None
    if open_pos and not broker_live:
        ghost_sim_pos = dict(open_pos)
        hang_lo = float(cfg.recipe.get("hang_lo", 30.0))
        hang_hi = float(cfg.recipe.get("hang_hi", 60.0))
        hang_mid = 0.5 * (hang_lo + hang_hi)
        # Prefer last non-null hang trail from desired if present as protect;
        # otherwise place symmetric entry band around spot (flat intent).
        if want_s is None and want_l is None:
            want_s = round(spot + hang_mid)
            want_l = round(spot - hang_mid)
        open_pos = None
        out["ghost_sim_pos"] = ghost_sim_pos
        out["ghost_override"] = "broker_flat_authoritative"

    # Orphan / oversize broker book: ONLY flatten when over max_lots.
    # Do NOT flatten on sim↔broker side/size mismatch — bar sim lags fills and
    # was wiping good LIVE positions (e.g. short fill while sim still long).
    flatten_why = None
    if broker_live and int(broker_live.get("n") or 0) > int(cfg.max_lots):
        flatten_why = (
            f"broker_over_max n={int(broker_live['n'])}>max={cfg.max_lots}"
        )

    # When broker has a position, it is authoritative for open_pos used below
    # (ledger / kill / protect). Wants still come from sim unless ghost-flat.
    if broker_live and broker_live.get("s"):
        open_pos = {
            "s": str(broker_live["s"]),
            "n": int(broker_live.get("n") or 1),
            "ep": broker_live.get("ep"),
        }
        out["broker_authoritative_pos"] = True

    # Broker working orders for this symbol
    try:
        results = get_futopt_order_results(session, acc=acc, market_type=mt)
    except Exception:
        # Some SDK builds want market_type=None
        results = get_futopt_order_results(session, acc=acc, market_type=None)

    working: list[tuple[str, float, Any]] = []  # side, px, raw
    for item in results:
        # Accounting / order book often uses FITM while marketdata uses TMFH6
        if not is_tmf_acct_symbol(
            str(getattr(item, "symbol", "") or ""), front_symbol=sym
        ):
            continue
        if not _is_working(item):
            continue
        side = _parse_order_side(item)
        px = _parse_order_px(item)
        if side is None or px is None:
            continue
        working.append((side, px, item))

    actions: list[dict] = []
    api_budget = cfg.max_api_per_poll

    def budget() -> bool:
        if api_budget <= 0:
            return True
        return len([a for a in actions if a.get("counts_api")]) < api_budget

    # Dedupe: keep ≤1 working rail per side (nearest to want). FITM/TMFH6 mismatch
    # previously made every poll re-place, stacking duplicate L/S limits.
    kept: list[tuple[str, float, Any]] = []
    for side in ("S", "L"):
        same = [(px, raw) for s, px, raw in working if s == side]
        if not same:
            continue
        want = want_s if side == "S" else want_l
        if want is not None:
            same.sort(key=lambda x: abs(float(x[0]) - float(want)))
        keep_px, keep_raw = same[0]
        kept.append((side, keep_px, keep_raw))
        for px, raw in same[1:]:
            if not budget():
                break
            act = {
                "kind": "cancel",
                "side": side,
                "price": px,
                "why": "dedupe_extra_rail",
                "counts_api": True,
            }
            try:
                cancel_futopt_order(
                    session, raw, acc=acc, dry_run=cfg.dry_run, session_date=day
                )
                act["ok"] = True
            except Exception as e:
                act["ok"] = False
                act["error"] = str(e)
            actions.append(act)
    working = kept

    # Flatten orphan/oversize first — cancel resting then market-close full broker n.
    if flatten_why and broker_live:
        for side, px, raw in list(working):
            if not budget():
                break
            act = {
                "kind": "cancel",
                "side": side,
                "price": px,
                "why": "pre_flatten_cancel",
                "counts_api": True,
            }
            try:
                cancel_futopt_order(
                    session,
                    raw,
                    acc=acc,
                    dry_run=cfg.dry_run,
                    session_date=day,
                )
                act["ok"] = True
            except Exception as e:
                act["ok"] = False
                act["error"] = str(e)
            actions.append(act)
        if budget():
            bs = str(broker_live["s"])
            lot = int(broker_live["n"])
            resolved = FutOptResolvedOrder(
                symbol=sym,
                buy_sell=_side_to_bs("S" if bs == "L" else "L"),
                lot=lot,
                price=None,
                price_type="market",
                time_in_force="ioc",
                order_type="close",
                market_type=mt_name,
                user_def=cfg.user_def,
                session_date=day,
            )
            act = {
                "kind": "exit_market",
                "side": bs,
                "lot": lot,
                "why": flatten_why,
                "counts_api": True,
            }
            try:
                place_futopt_order(session, resolved, acc=acc, dry_run=cfg.dry_run)
                act["ok"] = True
                ledger["broker_pos"] = None
            except Exception as e:
                act["ok"] = False
                act["error"] = str(e)
            actions.append(act)

        api_n = sum(1 for a in actions if a.get("counts_api") and a.get("ok"))
        ledger["last_symbol"] = sym
        ledger["last_desired"] = {
            "want_s": want_s,
            "want_l": want_l,
            "open_pos": open_pos,
            "broker_live": broker_live,
            "flatten_why": flatten_why,
            "spot": spot,
            "t": desired.get("last_t"),
            "regime": desired.get("regime"),
            "endDate": end,
            "name": name,
        }
        ledger = record_actions(ledger, actions, api_n=api_n)
        save_ledger(cfg.ledger_path, ledger)
        out.update(
            ok=True,
            symbol=sym,
            symbol_name=name,
            endDate=end,
            spot=spot,
            want_s=want_s,
            want_l=want_l,
            open_pos=open_pos,
            broker_live=broker_live,
            flatten_why=flatten_why,
            actions=actions,
            api_calls_this_poll=api_n,
            api_calls_day=ledger.get("api_calls_day"),
            reason="flatten_first",
            dry_run=cfg.dry_run,
        )
        return _finish(out, ledger)

    # 1) Exit if sim flat but we still think we're in a local broker pos from ledger
    #    (broker OI query varies by SDK — use open_pos as authority for v1)
    # If sim wants flat and we have resting entry rails only — ok
    # If sim has open_pos opposite to rails — rails handled below

    # Active flatten: if last trade was exit-like and open_pos is None but
    # ledger says broker_pos — send close IOC. Keep v1 simple: trust open_pos;
    # when open_pos set, cancel opposite entry and ensure protect.

    match = cfg.rail_match_pts

    def rail_ok(side: str, want: float | None) -> bool:
        if want is None:
            return not any(s == side for s, _, _ in working)
        for s, px, _ in working:
            if s == side and abs(px - float(want)) <= match:
                return True
        return False

    # Cancel extras / wrong prices
    for side, px, raw in list(working):
        want = want_s if side == "S" else want_l
        if want is None or abs(px - float(want)) > match:
            if not budget():
                break
            act = {
                "kind": "cancel",
                "side": side,
                "price": px,
                "why": "reconcile_cancel",
                "counts_api": True,
            }
            try:
                cancel_futopt_order(
                    session,
                    raw,
                    acc=acc,
                    dry_run=cfg.dry_run,
                    session_date=day,
                )
                act["ok"] = True
            except Exception as e:
                act["ok"] = False
                act["error"] = str(e)
            actions.append(act)

    # Refresh working after cancels (shadow): remove cancelled sides from local list
    cancelled_sides = {a["side"] for a in actions if a.get("kind") == "cancel" and a.get("ok")}
    working = [(s, p, r) for s, p, r in working if s not in cancelled_sides or rail_ok(s, want_s if s == "S" else want_l)]

    # Place missing rails (flat dual hang or protect while in pos)
    for side, want in (("S", want_s), ("L", want_l)):
        if want is None:
            continue
        if rail_ok(side, float(want)):
            continue
        if not budget():
            break
        # While in position, same-side may be scale; opposite is protect
        resolved = FutOptResolvedOrder(
            symbol=sym,
            buy_sell=_side_to_bs(side),
            lot=1,
            price=float(want),
            price_type="limit",
            time_in_force="rod",
            order_type="auto",
            market_type=mt_name,
            user_def=cfg.user_def,
            session_date=day,
        )
        act = {
            "kind": "place",
            "side": side,
            "price": float(want),
            "why": "reconcile_place",
            "counts_api": True,
        }
        try:
            place_futopt_order(session, resolved, acc=acc, dry_run=cfg.dry_run)
            act["ok"] = True
        except Exception as e:
            act["ok"] = False
            act["error"] = str(e)
        actions.append(act)

    # Flatten signal: only if broker still has a position (never trust ledger alone —
    # ghost sim clears open_pos while broker is already flat → would 8481301).
    if open_pos is None and broker_live and broker_live.get("s") and budget():
        side = str(broker_live["s"])
        lot = int(broker_live.get("n") or 1)
        resolved = FutOptResolvedOrder(
            symbol=sym,
            buy_sell=_side_to_bs("S" if side == "L" else "L"),  # close opposite
            lot=lot,
            price=None,
            price_type="market",
            time_in_force="ioc",
            order_type="close",
            market_type=mt_name,
            user_def=cfg.user_def,
            session_date=day,
        )
        act = {"kind": "exit_market", "side": side, "lot": lot, "why": "broker_flat_sim", "counts_api": True}
        try:
            place_futopt_order(session, resolved, acc=acc, dry_run=cfg.dry_run)
            act["ok"] = True
            ledger["broker_pos"] = None
        except Exception as e:
            act["ok"] = False
            act["error"] = str(e)
        actions.append(act)
    elif open_pos is None and not broker_live:
        ledger["broker_pos"] = None

    # Recomputed fresh each poll (not incremented) from the full realized-trade
    # list simulate() returns for the current bar window — so a string of small
    # realized losses trips the breaker even with no single position ever
    # breaching the threshold on its own (previously only open_pos.u_pnl was
    # checked, so closed round-trips never counted toward the day-loss kill).
    realized_trades = desired.get("trades") or []
    day_pnl_pts = round(sum(float(t.get("pnl") or 0) for t in realized_trades), 1)
    ledger["day_pnl_pts"] = day_pnl_pts
    kill_triggers = []
    if day_pnl_pts <= -abs(cfg.kill_day_loss_pts):
        kill_triggers.append(f"day_pnl_pts={day_pnl_pts}<=-{cfg.kill_day_loss_pts}")

    if open_pos:
        ledger["broker_pos"] = {
            "s": open_pos.get("s"),
            "n": open_pos.get("n"),
            "ep": open_pos.get("ep"),
        }
        u = float(open_pos.get("u_pnl") or 0)
        if u <= -abs(cfg.kill_day_loss_pts):
            kill_triggers.append(f"u_pnl={u}<=-{cfg.kill_day_loss_pts}")

    if kill_triggers:
        ledger["killed"] = True
        ledger["kill_reason"] = " & ".join(kill_triggers)

    api_n = sum(1 for a in actions if a.get("counts_api") and a.get("ok"))
    ledger["last_symbol"] = sym
    ledger["last_desired"] = {
        "want_s": want_s,
        "want_l": want_l,
        "open_pos": open_pos,
        "broker_live": broker_live,
        "spot": spot,
        "t": desired.get("last_t"),
        "regime": desired.get("regime"),
        "endDate": end,
        "name": name,
    }
    ledger = record_actions(ledger, actions, api_n=api_n)
    save_ledger(cfg.ledger_path, ledger)

    out.update(
        ok=True,
        symbol=sym,
        symbol_name=name,
        endDate=end,
        spot=spot,
        want_s=want_s,
        want_l=want_l,
        open_pos=open_pos,
        broker_live=broker_live,
        actions=actions,
        api_calls_this_poll=api_n,
        api_calls_day=ledger.get("api_calls_day"),
        dry_run=cfg.dry_run,
        reason="reconciled",
    )
    return _finish(out, ledger)


def main(argv: list[str] | None = None) -> int:
    import argparse
    import json
    import os

    ap = argparse.ArgumentParser(description="TMF channel Order poll (desired-state)")
    ap.add_argument(
        "--force",
        action="store_true",
        help="ignore session window (requires ORDER_TMF_CHANNEL_FORCE_OK=1)",
    )
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    # Hard gate: empty-window --force can recompute day_pnl on stale bars and
    # trip kill into the production ledger / broadcast (2026-08-05 incident).
    # Tests call reconcile_once(force=...) directly and are unaffected.
    if args.force and os.environ.get("ORDER_TMF_CHANNEL_FORCE_OK", "").strip() != "1":
        msg = (
            "refusing --force without ORDER_TMF_CHANNEL_FORCE_OK=1 "
            "(avoids dual-path / false kill on production ledger; "
            "use launchd tmf-channel-poll inside the session window instead)"
        )
        if args.json:
            print(json.dumps({"ok": False, "reason": "force_refused", "error": msg}))
        else:
            print(f"tmf-channel ERROR: {msg}", file=sys.stderr)
        return 2
    summary = reconcile_once(force=args.force)
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    else:
        print(
            f"tmf-channel {summary.get('reason')} dry={summary.get('dry_run')} "
            f"sym={summary.get('symbol')} S={summary.get('want_s')} L={summary.get('want_l')} "
            f"pos={summary.get('open_pos')} actions={len(summary.get('actions') or [])} "
            f"api_day={summary.get('api_calls_day')}"
        )
        for a in summary.get("actions") or []:
            print(" ", a)
    return 0 if summary.get("ok") or summary.get("reason") in (
        "outside_session",
        "ORDER_TMF_CHANNEL_ENABLED=0",
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
