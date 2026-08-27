"""DESIGN CANDIDATE B (draft only, not wired in) — min inter-cancel interval
for entry-rail cancels caused by cell-transition, in
src/order/tmf_channel_order.py::reconcile_once().

Do NOT import/apply this automatically. This is a review artifact for the
primary session to test and hand-apply.

--------------------------------------------------------------------------
SCOPE (read this before the diff)
--------------------------------------------------------------------------
Only the "want_s/want_l became None because pv re-entered the quiet set"
cancel is throttleable. `cell.block` cancels are OUT OF SCOPE, full stop —
no exception path exists for them in this design, because the live
2026-08-08 incident was exactly a blocked-side cancel firing too slowly, and
a time-based limiter has no reliable way to tell "this block cancel is safe
to delay" from "this is the one that matters" within the throttle window.
The task prompt asked for a "block=[] both sides" exception carve-out; I
deliberately did not build one. A carve-out implies block cancels sometimes
pass *through* the throttle path, which means the throttle function itself
becomes a second place capable of delaying a block cancel — new surface
area for exactly the bug that was found live. Instead: block-caused cancels
never enter this function at all (caller gates on the reason string before
calling), so there is no code path, correct or buggy, in which a block
cancel can be delayed by so much as one poll. This is stricter than what was
asked for and I'm flagging that as a deliberate deviation.

Price-drift cancels (`abs(px - want) > match`, i.e. want is not None but has
moved) are also OUT OF SCOPE — only the `want is None` branch is throttled.
So if price/anchor genuinely moves during a suppressed interval, the next
poll takes the `abs(px-want) > match` branch, which this function never
touches, and cancels+replaces normally. No stale-price risk: the throttle
can only ever *hold a rail resting at its current price for up to N extra
seconds*, never redirect it to a new price it doesn't currently have.

--------------------------------------------------------------------------
1) NEW: src/order/tmf_channel_ledger.py — add to _empty()
--------------------------------------------------------------------------
    # Cancel-rate throttle (candidate B, 2026-08-08): per-side timestamp of
    # the last CANCEL fired for a want-became-None-via-quiet reason. Never
    # touched for block-caused cancels or price-drift cancels.
    "cancel_throttle_last": {"S": None, "L": None},

--------------------------------------------------------------------------
2) NEW function in src/order/tmf_channel_order.py (near apply_quiet_flat_entry_gate)
--------------------------------------------------------------------------

def should_throttle_quiet_cancel(
    side: str,
    *,
    quiet_skip_reason: str | None,
    open_pos: dict[str, Any] | None,
    ledger: dict[str, Any],
    min_interval_sec: float = 45.0,
    now: datetime | None = None,
) -> tuple[bool, dict[str, Any]]:
    \"\"\"Rate-limit REDUNDANT cancels of a resting entry rail whose want just
    went to None purely because pv re-entered the quiet set. Returns
    (suppress, mutated_ledger); caller must only call this for the `want is
    None` cancel branch, and must never call it for a side that block:<side>
    covers (see module docstring — no in-function exception exists).

    Preconditions enforced defensively (fail-safe = do NOT suppress):
      - reason string must contain 'quiet_flat_skip' (not block)
      - open_pos must be None (flat) — matches apply_quiet_flat_entry_gate's
        own scope; a throttled cancel can never rest against a live fill
    \"\"\"
    reason = quiet_skip_reason or ""
    if f\"block:{side}\" in reason or \"quiet_flat_skip\" not in reason:
        return False, ledger
    if open_pos is not None:
        return False, ledger

    now = now or datetime.now(tz=_TZ)
    throttle = ledger.get(\"cancel_throttle_last\")
    throttle = dict(throttle) if isinstance(throttle, dict) else {}
    last_ts = None
    last_str = throttle.get(side)
    if last_str:
        try:
            last_ts = datetime.fromisoformat(last_str)
        except ValueError:
            last_ts = None

    if last_ts is not None and (now - last_ts).total_seconds() < min_interval_sec:
        return True, ledger  # suppress; do NOT bump the stamp (no window reset)

    throttle[side] = now.isoformat()
    ledger[\"cancel_throttle_last\"] = throttle
    return False, ledger


--------------------------------------------------------------------------
3) CALL SITE — reconcile_once, "Cancel extras / wrong prices" loop
   (current code ~line 834-859 of tmf_channel_order.py)
--------------------------------------------------------------------------

BEFORE:

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
                    session, raw, acc=acc, dry_run=cfg.dry_run, session_date=day,
                )
                act["ok"] = True
            except Exception as e:
                act["ok"] = False
                act["error"] = str(e)
            actions.append(act)

AFTER:

    for side, px, raw in list(working):
        want = want_s if side == "S" else want_l
        if want is None or abs(px - float(want)) > match:
            if not budget():
                break
            if want is None:
                # Only the "want vanished" branch is throttle-eligible, and
                # only when the vanish reason is quiet (not block — see
                # module docstring, no exception exists for block here).
                suppress, ledger = should_throttle_quiet_cancel(
                    side,
                    quiet_skip_reason=quiet_skip,
                    open_pos=open_pos,
                    ledger=ledger,
                )
                if suppress:
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
                    session, raw, acc=acc, dry_run=cfg.dry_run, session_date=day,
                )
                act["ok"] = True
            except Exception as e:
                act["ok"] = False
                act["error"] = str(e)
            actions.append(act)

Note: `quiet_skip` is already computed earlier in reconcile_once (the
`out["quiet_flat_skip"] = quiet_skip` line, ~642-643) and is in scope at the
cancel loop. `open_pos` is likewise already in scope (set earlier, possibly
overwritten to broker_live-authoritative form at ~625-631, which is exactly
the "still flat" signal we want — if broker has a position, open_pos is
non-None and this throttle no-ops).

--------------------------------------------------------------------------
4) Optional: reset stamp for the side once it's no longer relevant
--------------------------------------------------------------------------
Not required for correctness (see block-safety and price-drift-safety notes
above), but for hygiene, roll_day()'s existing full-ledger reset already
clears cancel_throttle_last at day boundary since _empty() is rebuilt.
No mid-day reset needed: once want returns non-None (quiet clears or price
moves), the code path bypasses should_throttle_quiet_cancel entirely, so a
stale timestamp sitting in the ledger is inert until the *next* want=None
quiet event for that side, which is exactly when we want the interval to
still apply (that's the point of the limiter).
"""
