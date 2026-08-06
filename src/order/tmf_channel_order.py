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
from order.tmf_channel_ledger import (
    load_ledger,
    record_actions,
    roll_day,
    save_ledger,
    trading_day_str,
)
from order.tmf_channel_marketdata import (
    bars_to_arrays,
    fetch_1m_bars,
    in_tmf_trade_window,
    resolve_front_symbol,
    session_hhmm_now,
)

_TZ = ZoneInfo("Asia/Taipei")


def apply_quiet_flat_entry_gate(
    want_s: float | None,
    want_l: float | None,
    *,
    broker_live: dict[str, Any] | None,
    desired: dict[str, Any],
    recipe: dict[str, Any] | None = None,
) -> tuple[float | None, float | None, str | None]:
    """When flat + cell quiet=dry (or both), strip entry rails.

    Defense in depth for live: sim may still emit leftover wants if a prior
    bar placed hangs before quiet, or greed bypass. Protect rails while in a
    broker position are untouched (caller only invokes when flat).
    """
    if broker_live and broker_live.get("s") and int(broker_live.get("n") or 0) > 0:
        return want_s, want_l, None
    ac = desired.get("active_cell") if isinstance(desired, dict) else None
    ac = ac if isinstance(ac, dict) else {}
    cr = ac.get("recipe") if isinstance(ac.get("recipe"), dict) else {}
    recipe = recipe or {}
    sq = cr.get("skip_quiet_mode")
    if sq is None:
        sq = recipe.get("skip_quiet_mode")
    if sq is None:
        sq = "both" if recipe.get("skip_quiet_regime") else "none"
    sq = str(sq or "none")
    if sq == "none":
        return want_s, want_l, None
    pv = str(ac.get("pv") or desired.get("regime") or "")
    quiet = ("contract", "dry") if sq == "both" else (("dry",) if sq == "dry" else ())
    if pv not in quiet:
        return want_s, want_l, None
    if want_s is None and want_l is None:
        return want_s, want_l, None
    return None, None, f"quiet_flat_skip:{sq}|{pv}"


def block_same_side_scale_wants(
    want_s: float | None,
    want_l: float | None,
    *,
    open_pos: dict[str, Any] | None,
    max_lots: int,
) -> tuple[float | None, float | None, str | None]:
    """Drop same-side hang when already at max_lots (keep opposite protect).

    Returns (want_s, want_l, reason_or_None).
    """
    if not open_pos or not open_pos.get("s"):
        return want_s, want_l, None
    side = str(open_pos["s"])
    n = int(open_pos.get("n") or 0)
    if n < max(1, int(max_lots)):
        return want_s, want_l, None
    if side == "S":
        return None, want_l, f"at_max_lots={max_lots} side=S n={n}"
    if side == "L":
        return want_s, None, f"at_max_lots={max_lots} side=L n={n}"
    return want_s, want_l, None


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
    """Causal O-anchor desired rails via ``tmf_channel.engine`` (no sys.path lab)."""
    from order.tmf_channel_pv16_book import (
        active_cell_payload,
        hhmm_from_bar_t,
        session_from_hhmm,
    )
    from tmf_channel.aux_cache import load_vixtwn_1m_cached, load_vixtwn_delta_cached
    from tmf_channel.desired_cache import (
        fingerprint_bars,
        get_cached_desired,
        store_desired,
    )
    from tmf_channel.engine import classify_pv, rvol_series, simulate
    from tmf_channel.nq_gate import nq_side_for_day

    if len(bars) < 20:
        return dict(ok=False, reason="bars_lt_20")

    fp = fingerprint_bars(bars)
    cached = get_cached_desired(fp)
    if cached is not None:
        return cached

    O, H, L, C, V, T = bars_to_arrays(day, bars)
    run_recipe = dict(recipe)
    run_recipe["hang_anchor"] = "O"
    run_recipe["eod_flatten"] = False
    try:
        run_recipe["vixtwn_1m"] = load_vixtwn_1m_cached()
    except Exception:
        run_recipe.setdefault("vixtwn_calib", "none")
    nq_side = nq_side_for_day(day, hm=hhmm_from_bar_t(bars[-1].get("t")))
    if nq_side is not None:
        run_recipe["session_side_gate"] = {day: nq_side}

    vix = load_vixtwn_delta_cached()
    trades, events, ws, wl, rvol, regime, open_pos = simulate(
        O, H, L, C, V, T, run_recipe, vix_delta=vix or {}
    )
    last_i = len(C) - 1
    hm = hhmm_from_bar_t(bars[-1].get("t"))
    sess = session_from_hhmm(hm)
    pv = "na"
    try:
        rv = rvol_series(V)
        pv, _ = classify_pv(C, O, rv, last_i)
    except Exception:
        if regime:
            pv = str(regime[-1] or "na")
    book = run_recipe.get("session_pv_book")
    cell = active_cell_payload(
        session=sess,
        pv=str(pv),
        book=book if isinstance(book, dict) else None,
        nq_gate=nq_side,
    )
    out = dict(
        ok=True,
        want_s=ws[-1] if ws else None,
        want_l=wl[-1] if wl else None,
        open_pos=open_pos,
        trades=trades,
        events=events,
        spot=float(C[-1]),
        last_t=bars[-1]["t"],
        regime=regime[-1] if regime else None,
        active_cell=cell,
        nq_gate=nq_side,
        recipe_version=str(run_recipe.get("recipe_version") or ""),
    )
    store_desired(fp, out, bars=bars)
    return out


def _try_nq_gate_for_day(day: str) -> str | None:
    """Compat wrapper for tests — prefer ``tmf_channel.nq_gate``."""
    from tmf_channel.nq_gate import nq_side_for_day

    return nq_side_for_day(day)


def _trade_exit_trading_day(trade: dict[str, Any]) -> str | None:
    """Map a simulate() fill to ledger trading_day_str (session-aware)."""
    raw = str(trade.get("xt") or trade.get("et") or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_TZ)
    else:
        dt = dt.astimezone(_TZ)
    return trading_day_str(dt)


def day_pnl_from_sim_trades(trades: list[dict[str, Any]] | None, day: str) -> float:
    """Sum simulate PnL for the current trading day only.

    ``fetch_1m_bars`` spans prior night + today; summing the whole window
    (or switching engines mid-session) can trip a false day-loss kill while
    broker realized is still small.
    """
    total = 0.0
    for t in trades or []:
        if _trade_exit_trading_day(t) != day:
            continue
        try:
            total += float(t.get("pnl") or 0)
        except (TypeError, ValueError):
            continue
    return round(total, 1)


def trip_day_pnl_kill(*, dry_run: bool, day_pnl_pts: float, kill_day_loss_pts: float) -> bool:
    """Whether sim day PnL should trip the kill switch.

    Live submit path must not kill on simulate() replay PnL — it diverges from
    broker fills (2026-08-06: sim −1230 vs broker day ≈ +12). Dry-run keeps
    the sim breaker for paper observation.
    """
    if not dry_run:
        return False
    return float(day_pnl_pts) <= -abs(float(kill_day_loss_pts))


def reconcile_once(
    cfg: TmfChannelOrderConfig | None = None,
    *,
    force: bool = False,
    use_session_pool: bool = False,
    session: Any | None = None,
) -> dict[str, Any]:
    """One poll tick. Returns summary dict (always JSON-serializable).

    ``use_session_pool=True`` (worker path) reuses a long-lived Fubon login.
    """
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

    def _acquire_session():
        nonlocal session
        if session is not None:
            return session
        if use_session_pool:
            from tmf_channel.session_pool import get_fubon_session

            session = get_fubon_session(realtime=True)
            return session
        session = connect_fubon(realtime=True)
        return session

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
            session = _acquire_session()
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

    session = _acquire_session()
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

    # Quiet flat hard gate (defense in depth vs leftover hangs / greed bypass).
    want_s, want_l, quiet_skip = apply_quiet_flat_entry_gate(
        want_s,
        want_l,
        broker_live=broker_live,
        desired=desired,
        recipe=cfg.recipe,
    )
    if quiet_skip:
        out["quiet_flat_skip"] = quiet_skip

    # Hard size lock: at max_lots, never rest a same-side scale/entry rail.
    # 2026-08-05 live: sim in_pos_hang=both still emitted same-side want while
    # already 1 short → resting S limit filled → broker n=2 → broker_over_max.
    # Opposite-side protect (opp_cover) remains allowed.
    want_s, want_l, scale_block = block_same_side_scale_wants(
        want_s, want_l, open_pos=open_pos, max_lots=int(cfg.max_lots)
    )
    if scale_block:
        out["scale_blocked"] = scale_block

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
            "active_cell": desired.get("active_cell"),
            "nq_gate": desired.get("nq_gate"),
            "recipe_version": desired.get("recipe_version"),
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
            active_cell=desired.get("active_cell"),
            nq_gate=desired.get("nq_gate"),
            recipe_version=desired.get("recipe_version"),
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
        # While flat: dual hang. While in pos at max_lots: same-side want already
        # nulled above — only opposite protect can place.
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

    # Recomputed from simulate fills on the current trading day only.
    # Live submit: do NOT write sim PnL into ledger day_pnl (diverges from
    # Fubon blotter; UI uses close_position_record). Dry-run keeps sim breaker.
    day_pnl_pts = day_pnl_from_sim_trades(desired.get("trades"), day)
    if cfg.dry_run:
        ledger["day_pnl_pts"] = day_pnl_pts
    else:
        ledger["sim_day_pnl_pts"] = day_pnl_pts
        # Do not publish sim replay as day_pnl on live (8770 uses Fubon blotter).
        ledger["day_pnl_pts"] = None
    kill_triggers = []
    if trip_day_pnl_kill(
        dry_run=cfg.dry_run,
        day_pnl_pts=day_pnl_pts,
        kill_day_loss_pts=cfg.kill_day_loss_pts,
    ):
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
    elif broker_live and broker_live.get("s") and spot is not None:
        # Live ghost-flat path may null sim open_pos; still kill on broker float.
        try:
            ep = float(broker_live.get("ep"))
            n = int(broker_live.get("n") or 1)
            side = str(broker_live["s"])
            u = round((spot - ep) * n if side == "L" else (ep - spot) * n, 1)
            if u <= -abs(cfg.kill_day_loss_pts):
                kill_triggers.append(f"broker_u_pnl={u}<=-{cfg.kill_day_loss_pts}")
        except (TypeError, ValueError):
            pass

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
        "active_cell": desired.get("active_cell"),
        "nq_gate": desired.get("nq_gate"),
        "recipe_version": desired.get("recipe_version"),
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
        active_cell=desired.get("active_cell"),
        nq_gate=desired.get("nq_gate"),
        recipe_version=desired.get("recipe_version"),
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
