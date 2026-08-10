"""TMF micro-channel Order sleeve · desired-state reconciler.

Poll loop (not full event replay):
  candles → Final v1.1.1 simulate → want rails/position → diff vs broker → place/cancel.

Safety: dry_run default; ORDER_MASTER_ENABLED; per-sleeve flags; day API + PnL kill.
Order layer must not be imported by strategy scripts; this module pulls the lab
engine via path (same pattern as other research→order sleeves).
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
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
    ledger: dict[str, Any] | None = None,
    quiet_hysteresis_min: float = 2.0,
    quiet_exit_debounce_min: float = 1.0,
    now: datetime | None = None,
) -> tuple[float | None, float | None, str | None, dict[str, Any] | None]:
    """When flat, enforce cell.block immediately and cell quiet=dry (or both)
    with symmetric hysteresis. Returns (want_s, want_l, reason_or_None,
    mutated_ledger).

    Two independent defenses, found live 2026-08-08:

    1. ``cell.block`` (a permanent CELL_TUNE policy — e.g. night|normal and
       night|div_hh_weak_vol are hard-blocked) was never checked here at all,
       only ``skip_quiet_mode`` was. A real Buy order placed live while the
       active cell showed ``block=['L','S']`` traced back to this gap — the
       order-layer's own defense-in-depth had no code path for it. Stripped
       immediately below, no hysteresis: block is a deliberate, permanent
       decision, not a transient classification wobble.
    2. ``skip_quiet_mode`` (e.g. "dry") is a live *reclassification* that can
       genuinely flip every ~20-60s near a boundary (contract<->dry) even
       after ``_drop_forming_last_bar`` removed the mid-bar flicker source —
       PV8 itself can still legitimately alternate bar-to-bar. Stripping the
       want the instant pv enters the quiet set made the reconciler
       cancel+replace the same resting order every time it flickered back in
       (confirmed live: ~28 place + ~26 cancel/hour, same price). First fix
       (entry-side only) required pv to stay in the quiet set continuously
       for ``quiet_hysteresis_min`` before cancelling — but the streak clock
       reset on *any* single non-quiet poll, and since the underlying want
       price itself doesn't move, a market that keeps drifting briefly in
       and out of "dry" still produced a cancel+replace at the *identical*
       price every ~2-3 min (confirmed live: 5 cycles at 44941 between
       03:52-04:02, every ~1-3 min, well under the naive fix's intent).
       Real fix: symmetric hysteresis — the quiet streak only resets after
       pv has stayed *outside* the quiet set continuously for
       ``quiet_exit_debounce_min``; a brief single-poll drop-out bridges
       across (the maturing streak keeps its original start time) instead of
       restarting the clock from zero. New entries during quiet are
       separately blocked inside ``causal_engine.py`` itself (unaffected by
       this hysteresis).
    """
    ledger = ledger if isinstance(ledger, dict) else {}
    if broker_live and broker_live.get("s") and int(broker_live.get("n") or 0) > 0:
        return want_s, want_l, None, ledger

    ac = desired.get("active_cell") if isinstance(desired, dict) else None
    ac = ac if isinstance(ac, dict) else {}
    cr = ac.get("recipe") if isinstance(ac.get("recipe"), dict) else {}
    recipe = recipe or {}

    reasons: list[str] = []
    block_sides = set(cr.get("block") or [])
    if "S" in block_sides and want_s is not None:
        want_s = None
        reasons.append("block:S")
    if "L" in block_sides and want_l is not None:
        want_l = None
        reasons.append("block:L")

    sq = cr.get("skip_quiet_mode")
    if sq is None:
        sq = recipe.get("skip_quiet_mode")
    if sq is None:
        sq = "both" if recipe.get("skip_quiet_regime") else "none"
    sq = str(sq or "none")
    pv = str(ac.get("pv") or desired.get("regime") or "")
    quiet = ("contract", "dry") if sq == "both" else (("dry",) if sq == "dry" else ())

    if sq == "none":
        ledger["quiet_pv_since"] = None
        ledger["quiet_pv_value"] = None
        ledger["quiet_not_quiet_since"] = None
        return want_s, want_l, ("|".join(reasons) or None), ledger

    now = now or datetime.now(tz=_TZ)

    if pv not in quiet:
        # Not quiet this poll -- only clear an in-progress streak once
        # non-quiet has itself persisted (debounce the exit side too).
        nq_since_str = ledger.get("quiet_not_quiet_since")
        nq_since = None
        if nq_since_str:
            try:
                nq_since = datetime.fromisoformat(nq_since_str)
            except ValueError:
                nq_since = None
        if nq_since is None:
            ledger["quiet_not_quiet_since"] = now.isoformat()
            nq_since = now
        not_quiet_min = (now - nq_since).total_seconds() / 60.0
        if not_quiet_min >= quiet_exit_debounce_min:
            ledger["quiet_pv_since"] = None
            ledger["quiet_pv_value"] = None
        return want_s, want_l, ("|".join(reasons) or None), ledger

    # Currently quiet -- clear any in-progress "exiting quiet" tracking and
    # resume (not restart) the quiet-maturity clock.
    ledger["quiet_not_quiet_since"] = None
    since_ts = None
    if ledger.get("quiet_pv_value") == pv and ledger.get("quiet_pv_since"):
        try:
            since_ts = datetime.fromisoformat(ledger["quiet_pv_since"])
        except ValueError:
            since_ts = None
    if since_ts is None:
        ledger["quiet_pv_since"] = now.isoformat()
        ledger["quiet_pv_value"] = pv
        since_ts = now
    quiet_min = (now - since_ts).total_seconds() / 60.0

    if want_s is None and want_l is None:
        return want_s, want_l, ("|".join(reasons) or None), ledger
    if quiet_min < quiet_hysteresis_min:
        # Streak hasn't matured yet — leave an already-resting rail alone.
        return want_s, want_l, ("|".join(reasons) or None), ledger

    reasons.append(f"quiet_flat_skip:{sq}|{pv}|{quiet_min:.1f}min")
    return None, None, "|".join(reasons), ledger


def check_max_hold_safety_net(
    ledger: dict[str, Any],
    *,
    broker_live: dict[str, Any] | None,
    max_hold_safety_min: float,
    now: datetime | None = None,
    query_failed: bool = False,
) -> tuple[dict[str, Any], float | None, str | None]:
    """Independent, sim-state-free max-hold tracking (2026-08-07 backstop).

    Tracks wall-clock position-open time in the ledger (survives restarts),
    keyed off the real broker position — not simulate()'s internal
    lots[0]['eb'], which stops governing once broker_live becomes
    authoritative for open_pos. Returns (mutated_ledger, elapsed_min_or_None,
    flatten_why_or_None). Caller ORs flatten_why into its own flatten_why.

    query_failed=True means broker_live is None because query_tmf_broker_net
    raised, not because it confirmed flat — the tracked open_ts/sig must
    survive the outage untouched, or a transient query error silently resets
    this backstop's clock and lets a real position ride naked well past
    max_hold_safety_min. (2026-08-10: a ~4min "call id" outage did exactly
    this live — a position opened 15:09 should have safety-net-flattened
    ~16:39 but the clock restarted at ~16:17 instead.)
    """
    now = now or datetime.now(tz=_TZ)
    if query_failed:
        return ledger, None, None
    if not (broker_live and broker_live.get("s") and int(broker_live.get("n") or 0) > 0):
        ledger["position_open_ts"] = None
        ledger["position_open_sig"] = None
        return ledger, None, None
    sig = str(broker_live["s"])
    prev_sig = ledger.get("position_open_sig")
    prev_ts_str = ledger.get("position_open_ts")
    prev_ts = None
    if prev_sig == sig and prev_ts_str:
        try:
            prev_ts = datetime.fromisoformat(prev_ts_str)
        except ValueError:
            prev_ts = None
    if prev_ts is None:
        ledger["position_open_ts"] = now.isoformat()
        ledger["position_open_sig"] = sig
        prev_ts = now
    elapsed_min = (now - prev_ts).total_seconds() / 60.0
    why = None
    if elapsed_min > float(max_hold_safety_min):
        why = f"max_hold_safety_net elapsed={elapsed_min:.0f}min>cap={max_hold_safety_min:.0f}min"
    return ledger, elapsed_min, why


def synthesize_lost_tracking_protect_rail(
    want_s: float | None,
    want_l: float | None,
    *,
    broker_live: dict[str, Any] | None,
    active_cell_recipe: dict[str, Any] | None,
    fallback_recipe: dict[str, Any],
) -> tuple[float | None, float | None, bool]:
    """Rebuild a protective rail off the REAL broker position when simulate()'s
    own bar-replay has already closed its internal copy of this fill (its own
    max_hold_bars elapsed) and stopped producing any want for it.

    Without this, a held position rides with no resting close order at all
    from the moment simulate()'s internal max_hold_bars elapses until the
    independent time-based max_hold_safety_min backstop force-flattens it at
    market. Confirmed live 2026-08-10: the dashboard kept describing the
    cell's theoretical hang band ("S 掛帶 O+16~30") the whole time, but no
    order was actually resting there, so touching that price did nothing.

    Anchors off the real broker entry price using the CURRENT active cell's
    own hang_hi (the same number already shown on the dashboard), falling
    back to the base recipe's if the cell payload doesn't carry one. Only
    fires when BOTH sides are None -- a real, still-tracked in-position want
    (e.g. one side nulled by the max-lots same-side lock) is left alone.
    """
    if want_s is not None or want_l is not None:
        return want_s, want_l, False
    if not (broker_live and broker_live.get("s") and int(broker_live.get("n") or 0) > 0):
        return want_s, want_l, False
    try:
        ep = float(broker_live.get("ep"))
    except (TypeError, ValueError):
        return want_s, want_l, False
    recipe = active_cell_recipe if isinstance(active_cell_recipe, dict) else {}
    try:
        hi = float(recipe.get("hang_hi", fallback_recipe.get("hang_hi", 60.0)))
    except (TypeError, ValueError):
        hi = 60.0
    side = str(broker_live["s"])
    if side == "L":
        return round(ep + hi, 1), None, True
    if side == "S":
        return None, round(ep - hi, 1), True
    return want_s, want_l, False


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


def should_throttle_quiet_cancel(
    side: str,
    *,
    quiet_skip_reason: str | None,
    open_pos: dict[str, Any] | None,
    ledger: dict[str, Any],
    min_interval_sec: float = 45.0,
    now: datetime | None = None,
) -> tuple[bool, dict[str, Any]]:
    """Rate-limit REDUNDANT cancels of a resting entry rail whose want just
    went to None purely because pv re-entered the quiet set. Returns
    (suppress, mutated_ledger).

    Found live 2026-08-08: PV8 classification genuinely flickers near
    block/non-block boundaries from thin night-session volume vs tightly-
    packed classifier thresholds (confirmed via true re-simulation on pure
    historical data — inherent to classify_pv, not a live-only artifact).
    Smoothing the classifier itself (regime-commitment hysteresis) was tested
    and rejected: it cuts churn but produces a P&L-losing, statistically
    significant number of bar-events where a resting order would sit in a
    technically-blocked cell — the exact class of bug already fixed once
    tonight (a real order surviving a block=['L','S'] cell). This throttle
    instead targets the ORDER LAYER's redundant cancel/place round trips
    directly, without ever changing what gets blocked or when:

        A genuinely-needed CANCEL of a now-blocked side's resting rail is
        NEVER delayed or skipped, by even one poll, under any code path here.

    Caller contract (enforced twice — once by the caller's own gating, once
    defensively inside this function):
      - Only call this for the `want is None` cancel branch. Never call it
        for the `abs(px-want) > match` (price-drift) branch — a genuinely
        new want price must always cancel+replace immediately.
      - Never call it for a side that block:<side> covers. There is no
        exception path for block anywhere in this function, by design — a
        rate limiter has no reliable way to tell "safe to delay" from "the
        one that matters" within its own window.

    Fail-safe precondition checks (return "don't suppress" if violated, even
    though the caller is expected to already gate on these):
      - reason string must contain 'quiet_flat_skip' for this side's
        situation, and must NOT contain 'block:<side>'.
      - open_pos must be None (flat) — matches apply_quiet_flat_entry_gate's
        own scope; a throttled cancel can never rest against a live fill.
    """
    reason = quiet_skip_reason or ""
    if f"block:{side}" in reason or "quiet_flat_skip" not in reason:
        return False, ledger
    if open_pos is not None:
        return False, ledger

    now = now or datetime.now(tz=_TZ)
    throttle = ledger.get("cancel_throttle_last")
    throttle = dict(throttle) if isinstance(throttle, dict) else {}
    last_ts = None
    last_str = throttle.get(side)
    if last_str:
        try:
            last_ts = datetime.fromisoformat(last_str)
        except ValueError:
            last_ts = None

    if last_ts is not None and (now - last_ts).total_seconds() < min_interval_sec:
        return True, ledger  # suppress; do NOT bump the stamp (no sliding window)

    ledger = dict(ledger)
    throttle[side] = now.isoformat()
    ledger["cancel_throttle_last"] = throttle
    return False, ledger


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


def _drop_forming_last_bar(bars: list[dict]) -> list[dict]:
    """Exclude the currently-forming (not yet closed) last 1m bar.

    Found live 2026-08-08: Fubon's candle feed returns a live-updating bar
    for the in-progress minute — its H/L/C/V (and therefore PV8 regime
    classification) keep changing across successive ~20s polls of the same
    minute. ``simulate()``'s regime classifier was never validated against
    this: every backtest replays only fully-closed historical bars (one
    classification per bar). Reclassifying against a partial bar made PV8
    flip between adjacent states (e.g. contract<->normal) several times
    inside a single minute, and since some of those states are cell-blocked
    and others aren't, the reconciler correctly-but-uselessly cancelled and
    replaced the same resting order in lockstep — confirmed live: ~28 place
    + ~26 cancel/hour, repeatedly at the identical price. Dropping the
    forming bar restores backtest parity (one classification per closed
    bar); the cost is at most one ~20s poll cycle of latency, which does not
    matter for resting limit orders (they wait to be touched regardless of
    when they were (re)placed).
    """
    if not bars:
        return bars
    last_t = str(bars[-1].get("t") or "")
    if not last_t:
        return bars
    try:
        last_start = datetime.fromisoformat(last_t)
    except ValueError:
        return bars
    tz = last_start.tzinfo or _TZ
    if datetime.now(tz=tz) < last_start + timedelta(minutes=1):
        return bars[:-1]
    return bars


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
    from tmf_channel.nq_gate import last_nq_load_error, nq_side_for_day

    bars = _drop_forming_last_bar(bars)
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
    nq_gate_error = last_nq_load_error()
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
        nq_gate_error=nq_gate_error,
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
    query_failed = False
    try:
        broker_live = query_tmf_broker_net(session, acc=acc, front_symbol=sym)
    except Exception as e:
        out["broker_query_error"] = str(e)[:200]
        query_failed = True
        # Can't confirm real broker state -- treat entry wants as unknown
        # rather than trusting the raw sim signal, so a query outage never
        # sprays fresh "place" orders while we're blind to an existing
        # position/resting order.
        # 2026-08-10: a ~4min "call id" outage produced 5 duplicate L 45131
        # places under night|expand_up/contract before self-correcting via
        # dedupe_extra_rail once the query recovered -- this closes that gap.
        want_s = None
        want_l = None

    # Ghost sim position: bar engine still "in pos" after we flattened broker
    # (or external close). Order layer must trust broker, else wants go None and
    # we cancel the flat dual hangs that were correct for a flat book.
    # query_failed guard (2026-08-10): broker_live is also None when the query
    # merely raised, not just on a confirmed flat -- without this, the block
    # below would treat "we don't know" as "broker confirms flat", null
    # open_pos, and recompute a fresh want that cancels a real, still-
    # legitimate resting rail as a stale price the moment a query blips.
    ghost_sim_pos = None
    if open_pos and not broker_live and not query_failed:
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

    # Independent max-hold safety net (2026-08-07): simulate()'s own max_hold_bars
    # (16-38 bars depending on cell) only fires off its internal lots[0]['eb'],
    # which stops governing decisions the moment broker_live becomes authoritative
    # for open_pos below — bars are re-fetched fresh from Fubon each poll (not one
    # continuously-growing array), so the bar-replay's own lot tracking can lose
    # sync with the real broker position. Confirmed live 2026-08-06: a position
    # opened 13:41 sat untouched through 21:40 (8+ hours) with zero max_hold
    # enforcement, against an 83-day backtest where max hold ever seen was 38min.
    ledger, elapsed_hold_min, safety_flatten_why = check_max_hold_safety_net(
        ledger,
        broker_live=broker_live,
        max_hold_safety_min=cfg.max_hold_safety_min,
        query_failed=query_failed,
    )
    flatten_why = flatten_why or safety_flatten_why
    out["position_hold_min"] = round(elapsed_hold_min, 1) if elapsed_hold_min is not None else None

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
    want_s, want_l, quiet_skip, ledger = apply_quiet_flat_entry_gate(
        want_s,
        want_l,
        broker_live=broker_live,
        desired=desired,
        recipe=cfg.recipe,
        ledger=ledger,
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

    # Rebuild a protective rail off the real broker position when nothing
    # upstream (sim's own tracking, pvBlock, max-lots same-side lock) left a
    # want standing for it. Must run AFTER block_same_side_scale_wants above
    # — a same-side want that leaked past a fully-blocked cell (e.g.
    # block=["L","S"]) still reads non-None at this point in the pipeline
    # until the max-lots lock strips it, which would make this fire too
    # early and never actually synthesize anything (found live 2026-08-10).
    want_s, want_l, protect_synthesized = synthesize_lost_tracking_protect_rail(
        want_s,
        want_l,
        broker_live=broker_live,
        active_cell_recipe=(desired.get("active_cell") or {}).get("recipe"),
        fallback_recipe=cfg.recipe,
    )
    if protect_synthesized:
        out["protect_rail_synthesized"] = True

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
            if want is None and query_failed:
                # want went None because the broker query itself failed, not
                # because we confirmed nothing is wanted -- cancelling here
                # would strip a real, still-legitimate resting rail (e.g. the
                # synthesized protect above) for the entire outage, the exact
                # opposite of what the query-failure guard above intends.
                # 2026-08-10: found this the same night as that guard, before
                # it ever fired live -- leave resting orders untouched until
                # the query recovers and we can actually reconcile again.
                out.setdefault("query_outage_cancel_skipped", []).append(
                    {"side": side, "price": px}
                )
                continue
            if want is None:
                # Only the "want vanished" branch is throttle-eligible, and
                # only for a quiet reason — never for a block reason (see
                # should_throttle_quiet_cancel docstring) and never for a
                # price-drift cancel (that branch has want is not None and
                # never reaches here).
                suppress, ledger = should_throttle_quiet_cancel(
                    side,
                    quiet_skip_reason=quiet_skip,
                    open_pos=open_pos,
                    ledger=ledger,
                )
                if suppress:
                    out.setdefault("throttled_cancels", []).append(
                        {"side": side, "price": px}
                    )
                    continue
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
    # Fold this poll's actions into the ledger (incl. consecutive_order_failures)
    # BEFORE checking kill_triggers below, so a just-failed action counts
    # immediately rather than one poll later.
    api_n = sum(1 for a in actions if a.get("counts_api") and a.get("ok"))
    ledger = record_actions(ledger, actions, api_n=api_n)

    kill_triggers = []
    if trip_day_pnl_kill(
        dry_run=cfg.dry_run,
        day_pnl_pts=day_pnl_pts,
        kill_day_loss_pts=cfg.kill_day_loss_pts,
    ):
        kill_triggers.append(f"day_pnl_pts={day_pnl_pts}<=-{cfg.kill_day_loss_pts}")

    consecutive_failures = int(ledger.get("consecutive_order_failures") or 0)
    if consecutive_failures >= cfg.kill_consecutive_failures:
        kill_triggers.append(
            f"consecutive_order_failures={consecutive_failures}>={cfg.kill_consecutive_failures}"
        )

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
