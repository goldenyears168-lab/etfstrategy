"""DESIGN ONLY — candidate A: debounce redundant entry-rail cancel+replace.

NOT wired up, NOT imported by src/order/*. Proposed edits to
src/order/tmf_channel_order.py::reconcile_once() for the primary session to
review, test, and apply by hand.

------------------------------------------------------------------------
PROBLEM
------------------------------------------------------------------------
apply_quiet_flat_entry_gate() nulls want_s/want_l on hard `block` (immediate,
correct, must stay immediate) and on quiet-skip after hysteresis matures.
When a *non-block* null (or an ordinary reprice) causes the "Cancel extras /
wrong prices" loop (~line 835) to cancel a resting entry rail, and a few
polls later want returns to the *same* price, "Place missing rails"
(~line 866) re-places at that identical price — a pure round-trip through
the broker with zero economic content. This never happens *within* one
reconcile_once() call (want_s/want_l are computed once per call, and the
place loop only fires for a side whose want is non-None, while the cancel
loop for that side only fires when want is None or mismatched — so a
same-poll cancel+place at an identical price is structurally impossible
today). The redundant round trip is therefore always CROSS-poll, so state
must persist in the ledger (JSON-serializable, already round-tripped every
poll via load_ledger/save_ledger).

------------------------------------------------------------------------
SCOPE GUARD (answers the max_hold/stop/exit question)
------------------------------------------------------------------------
Debounce applies ONLY when `open_pos is None` (flat). While `open_pos` is
set, the same-side want is already nulled by block_same_side_scale_wants()
and the only resting rail is the OPPOSITE-side protect/trailing-stop rail —
never touch that path. Also: flatten_why / kill-switch / max-hold-safety-net
flattens all `return _finish(...)` *before* reaching the reprice loop (see
lines 604-813), and exit_market sends are unconditional IOC market orders,
never part of `working` (only resting LIMIT rails are). So this design can
only ever touch a same-side, flat, dual-hang ENTRY rail cancel+replace —
never an exit/protect cancel. Guarding on `open_pos is None` makes that
explicit and enforced in code, not just by convention.

------------------------------------------------------------------------
STATE
------------------------------------------------------------------------
ledger["rail_debounce"] = {"S": {"price": float, "ts": iso8601}, "L": {...}}
Written only when a cancel fires from the reprice loop AND that side was
NOT hard-blocked this poll (checked via the existing `quiet_skip` reason
string already returned by apply_quiet_flat_entry_gate — it already tags
block cancels as "block:S"/"block:L"). This keeps block cancels (and any
re-place right after a block clears) fully immediate/un-debounced, matching
"block is a deliberate, permanent decision, not a transient wobble" from
the existing docstring.

------------------------------------------------------------------------
PROPOSED EDIT 1 — inside "Cancel extras / wrong prices" loop (~line 834-859)
------------------------------------------------------------------------
    DEBOUNCE_SEC = 25.0  # < 2 poll cycles at ~20s cadence; tune from live logs

    for side, px, raw in list(working):
        want = want_s if side == "S" else want_l
        if want is None or abs(px - float(want)) > match:
            if not budget():
                break
            act = {"kind": "cancel", "side": side, "price": px,
                   "why": "reconcile_cancel", "counts_api": True}
            try:
                cancel_futopt_order(session, raw, acc=acc, dry_run=cfg.dry_run, session_date=day)
                act["ok"] = True
            except Exception as e:
                act["ok"] = False
                act["error"] = str(e)
            actions.append(act)
            # NEW: only record debounce state for non-block cancels while flat.
            is_block = bool(quiet_skip) and f"block:{side}" in quiet_skip
            if act["ok"] and open_pos is None and not is_block:
                rd = ledger.setdefault("rail_debounce", {})
                rd[side] = {"price": px, "ts": datetime.now(tz=_TZ).isoformat()}

------------------------------------------------------------------------
PROPOSED EDIT 2 — inside "Place missing rails" loop (~line 866-900)
------------------------------------------------------------------------
    for side, want in (("S", want_s), ("L", want_l)):
        if want is None:
            continue
        if rail_ok(side, float(want)):
            ledger.get("rail_debounce", {}).pop(side, None)  # NEW: stale-clear
            continue
        # NEW: skip a redundant re-place at the identical price we JUST
        # cancelled moments ago (flat only — see scope guard above).
        if open_pos is None:
            dbg = (ledger.get("rail_debounce") or {}).get(side)
            if dbg and abs(float(dbg["price"]) - float(want)) <= match:
                try:
                    age = (datetime.now(tz=_TZ)
                           - datetime.fromisoformat(dbg["ts"])).total_seconds()
                except Exception:
                    age = None
                if age is not None and age <= DEBOUNCE_SEC:
                    out.setdefault("debounced_places", []).append(
                        {"side": side, "price": want, "age_s": round(age, 1)}
                    )
                    continue
        if not budget():
            break
        ledger.get("rail_debounce", {}).pop(side, None)  # NEW: consumed
        # ... existing place_futopt_order() body unchanged ...

------------------------------------------------------------------------
CAVEAT (honest limitation)
------------------------------------------------------------------------
This only removes the redundant PLACE leg, not the original CANCEL leg —
by the time we know want will return to the same price, the cancel has
already been sent (we cannot see the future at cancel time). Net effect:
one broker round trip removed per flicker cycle (was cancel+place+place...,
becomes cancel+[skip]+place-or-skip), not zero. A stricter zero-round-trip
fix would require delaying the cancel itself by DEBOUNCE_SEC, which
re-introduces exactly the resting-order-in-a-blocked-cell risk this
candidate was designed to avoid — hence cancel stays immediate always,
only the mirror-image place is throttled.
"""
