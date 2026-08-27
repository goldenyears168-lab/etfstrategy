"""DESIGN ONLY — final proposal, post-review, for order-layer cancel/place
churn throttling in src/order/tmf_channel_order.py::reconcile_once().

NOT wired up. NOT imported by src/order/*. src/order/ is untouched by this
file. This is the artifact the primary session should read, test, and
hand-apply (or reject) piece by piece.

==============================================================================
BACKGROUND / WHY THIS FILE EXISTS
==============================================================================
Tonight's regime-commitment-hysteresis research confirmed PV8 genuinely
flickers near block/non-block boundaries from thin night-session volume vs.
tightly-packed classifier thresholds, and that smoothing the classification
itself is not worth the safety cost (resting orders sitting in a technically
-blocked cell). The chosen alternative: throttle the ORDER LAYER's redundant
cancel+place API churn, WITHOUT ever changing what gets blocked or when. The
non-negotiable invariant across everything below:

    A genuinely-needed CANCEL of a now-blocked side's resting rail is NEVER
    delayed or skipped, by even one poll, under any code path in this file.

Two independent candidate designs were drafted and critically reviewed
against the real reconcile_once() (src/order/tmf_channel_order.py, cancel
loop ~L834-859, place loop ~L866-900) and apply_quiet_flat_entry_gate()
(~L49-172). Review verdicts:

  - Design B (cancel-rate throttle, quiet-vanish cancels only): SAFE-AS-
    DESIGNED. One soft finding (fragile reliance on a formatted reason
    string) — addressed below via a pinned regression test rather than a
    signature change to apply_quiet_flat_entry_gate, to keep blast radius on
    live order-submission code minimal (see "REVIEWER CONCERNS ADDRESSED").
  - Design A (rail-place debounce, cancel+replace at identical price):
    NEEDS-REVISION. The recording step was scoped to the whole
    `want is None or abs(px-want) > match` condition instead of just the
    `want is None` sub-case the design targets, so a genuine price-drift
    cancel could poison rail_debounce and, if budget() ran out before that
    same poll's place loop, incorrectly suppress a *legitimate* later
    re-entry at a new price within DEBOUNCE_SEC. FIXED below.

RECOMMENDATION (unchanged from review): ship Design B alone first — smaller
diff, touches only the cancel loop, every interaction checked (block,
scale-lock, ghost-sim override, restart, day-roll) was verifiably clean.
Design A is included below FIXED and ready to review, but marked RESERVE —
hold it until B's churn reduction alone proves insufficient in live logs.

==============================================================================
REVIEWER CONCERNS ADDRESSED (map from review → change)
==============================================================================
1. [Design A, NEEDS-REVISION] "recording triggers on the whole cancel
   condition, not just want is None" →
   FIXED in `record_rail_debounce_after_cancel()` below: the function now
   hard-requires `want is None` as a precondition (see its docstring and the
   explicit `if ... or want is not None: return ledger` guard). A
   price-drift cancel (want is not None but moved) never reaches the
   ledger at all now, so it can never poison a later legitimate re-entry.

2. [Design B, soft finding] "depends on parsing another function's formatted
   reason string; fragile if that format is ever refactored" →
   Decision: do NOT change apply_quiet_flat_entry_gate's return signature
   (from `str | None` reason to structured per-side booleans) as part of
   this throttle change. That function is already used elsewhere in
   reconcile_once (out["quiet_flat_skip"] = quiet_skip) and by
   QuietFlatEntryGateTest; widening its blast radius to accommodate a
   separate throttle feature contradicts the "keep blast radius small on
   live order-submission code" principle from the review's own
   recommendation. Instead: added a pinned regression test (see
   `test_mixed_block_and_quiet_reason_string_isolates_side` in the Unit
   Tests section below) that locks in the exact mixed-reason string shape
   (`"block:S|quiet_flat_skip:both|dry|3.2min"`) `should_throttle_quiet_cancel`
   depends on. If apply_quiet_flat_entry_gate's reason format ever changes,
   that test breaks loudly instead of this throttle silently mis-parsing.

3. [Design A] scope-guard on open_pos is None, block-cancel immediacy,
   restart/day-roll safety, cross-poll state requirement — all confirmed
   correct by the review; carried over unchanged from candidateA.

4. [Design B] block-cancel exception carve-out deliberately NOT built
   (candidate B's own documented deviation from the task prompt) — carried
   over unchanged; the review explicitly agreed this is the safer choice.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

_TZ = ZoneInfo("Asia/Taipei")


# ==============================================================================
# PART 1 — SHIP FIRST: Design B, cancel-rate throttle (quiet-vanish only)
# ==============================================================================
#
# Ledger addition (src/order/tmf_channel_ledger.py::_empty()):
#
#     # Cancel-rate throttle (2026-08-08): per-side timestamp of the last
#     # CANCEL fired for a want-became-None-via-quiet reason. Never touched
#     # for block-caused cancels or price-drift cancels (see
#     # should_throttle_quiet_cancel() in tmf_channel_order.py).
#     "cancel_throttle_last": {"S": None, "L": None},
#
# (Not strictly required for correctness — ledger.get() with a default
# works without it — but matches the existing hygiene pattern used for
# quiet_pv_since/quiet_pv_value/quiet_not_quiet_since, and documents the
# key's purpose at the schema's single source of truth.)


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

    Caller contract (enforced twice — once by the caller's own gating, once
    defensively inside this function):
      - Only call this for the `want is None` cancel branch. Never call it
        for the `abs(px-want) > match` (price-drift) branch — a genuinely
        new want price must always cancel+replace immediately.
      - Never call it for a side that block:<side> covers. There is no
        exception path for block anywhere in this function, by design (see
        module docstring point 4) — the live 2026-08-08 incident was
        exactly a blocked-side cancel arriving too slowly, and a rate
        limiter has no reliable way to tell "safe to delay" from "the one
        that matters" within its own window.

    Fail-safe precondition checks (return "don't suppress" if violated,
    even though the caller is expected to already gate on these):
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


# ------------------------------------------------------------------------
# Call site — reconcile_once, "Cancel extras / wrong prices" loop
# (current code: src/order/tmf_channel_order.py ~L834-859)
# ------------------------------------------------------------------------
#
# BEFORE:
#
#     for side, px, raw in list(working):
#         want = want_s if side == "S" else want_l
#         if want is None or abs(px - float(want)) > match:
#             if not budget():
#                 break
#             act = {
#                 "kind": "cancel", "side": side, "price": px,
#                 "why": "reconcile_cancel", "counts_api": True,
#             }
#             try:
#                 cancel_futopt_order(session, raw, acc=acc, dry_run=cfg.dry_run, session_date=day)
#                 act["ok"] = True
#             except Exception as e:
#                 act["ok"] = False
#                 act["error"] = str(e)
#             actions.append(act)
#
# AFTER:
#
#     for side, px, raw in list(working):
#         want = want_s if side == "S" else want_l
#         if want is None or abs(px - float(want)) > match:
#             if not budget():
#                 break
#             if want is None:
#                 # Only the "want vanished" branch is throttle-eligible, and
#                 # only for a quiet reason (never block — see module docstring).
#                 suppress, ledger = should_throttle_quiet_cancel(
#                     side,
#                     quiet_skip_reason=quiet_skip,
#                     open_pos=open_pos,
#                     ledger=ledger,
#                 )
#                 if suppress:
#                     out.setdefault("throttled_cancels", []).append({"side": side, "price": px})
#                     continue
#             act = {
#                 "kind": "cancel", "side": side, "price": px,
#                 "why": "reconcile_cancel", "counts_api": True,
#             }
#             try:
#                 cancel_futopt_order(session, raw, acc=acc, dry_run=cfg.dry_run, session_date=day)
#                 act["ok"] = True
#             except Exception as e:
#                 act["ok"] = False
#                 act["error"] = str(e)
#             actions.append(act)
#
# `quiet_skip` is already computed earlier in reconcile_once
# (out["quiet_flat_skip"] = quiet_skip, ~L642-643) and in scope here.
# `open_pos` is likewise already in scope, possibly overwritten to
# broker_live-authoritative form at ~L625-631 — exactly the "still flat"
# signal wanted; if broker has a position, open_pos is non-None and this
# throttle is a guaranteed no-op.
#
# No mid-day reset needed beyond roll_day()'s existing full-ledger rebuild
# at the trading-day boundary (src/order/tmf_channel_ledger.py::roll_day),
# which clears cancel_throttle_last along with everything else in _empty().


# ==============================================================================
# PART 2 — RESERVE, NOT FOR IMMEDIATE SHIPMENT: Design A, fixed per review
# ==============================================================================
#
# Only apply this if live logs show Design B's churn reduction alone is
# insufficient. Ledger addition (src/order/tmf_channel_ledger.py::_empty()),
# only needed if/when this part ships:
#
#     "rail_debounce": {},  # {"S": {"price": float, "ts": iso8601}, "L": {...}}


def record_rail_debounce_after_cancel(
    ledger: dict[str, Any],
    side: str,
    px: float,
    *,
    want: float | None,
    open_pos: dict[str, Any] | None,
    quiet_skip_reason: str | None,
    cancel_ok: bool,
    now: datetime | None = None,
) -> dict[str, Any]:
    """FIXED 2026-08-08 per review (candidate A was NEEDS-REVISION here).

    Records that side's just-cancelled price so a same-poll-or-later
    redundant re-place at the identical price can be skipped by
    should_skip_redundant_place() below.

    Original bug: candidate A recorded on the *entire* cancel condition
    (`want is None or abs(px-want) > match`), which also covers a genuine
    price-drift cancel (want moved to a *different*, non-None price). That
    almost always self-clears in the same poll's place loop (the new want
    differs from the just-cancelled price by construction), but if
    budget() was exhausted before the place loop ran, the stale entry
    could survive and incorrectly suppress a *legitimate* later re-entry
    if price whipsawed back near the old level within debounce_sec.

    Fix: hard-require `want is None` (the pv-flicker/quiet-vanish case this
    debounce actually targets) as a precondition. A price-drift cancel
    (want is not None) now never reaches the ledger via this function at
    all — it is out of scope for this debounce, full stop, matching Design
    B's identical scoping decision for the same reason.

    Also unchanged from candidate A (both confirmed correct by review):
      - open_pos must be None (flat only — this can never touch the
        opposite-side protect/trailing-stop rail while in a position,
        since that rail's want is never None while open_pos is set).
      - block-caused cancels never get recorded (checked via the reason
        string tag "block:<side>"), so a block cancel is never followed
        by a suppressed re-place — matches "block is permanent, not a
        wobble" from apply_quiet_flat_entry_gate's own docstring.
    """
    if not cancel_ok or open_pos is not None or want is not None:
        return ledger
    reason = quiet_skip_reason or ""
    if f"block:{side}" in reason:
        return ledger
    ledger = dict(ledger)
    rd = dict(ledger.get("rail_debounce") or {})
    rd[side] = {"price": float(px), "ts": (now or datetime.now(tz=_TZ)).isoformat()}
    ledger["rail_debounce"] = rd
    return ledger


def should_skip_redundant_place(
    ledger: dict[str, Any],
    side: str,
    want: float,
    *,
    open_pos: dict[str, Any] | None,
    match: float,
    debounce_sec: float = 25.0,
    now: datetime | None = None,
) -> tuple[bool, dict[str, Any]]:
    """Returns (skip, mutated_ledger). Call only when about to place (i.e.
    the caller's rail_ok(side, want) has already returned False this poll —
    see the AFTER call-site sketch below).

    Skips a redundant re-place at the identical price this side was just
    cancelled at, within debounce_sec, while flat. Clears (consumes or
    stale-clears) the ledger entry whenever it does NOT match closely
    enough or has aged out, so a stale entry can never leak into a later,
    unrelated debounce decision — this is the mechanism that fixes the
    review's stale-entry concern in combination with the recording-side
    fix above.
    """
    if open_pos is not None:
        return False, ledger
    rd = ledger.get("rail_debounce")
    entry = rd.get(side) if isinstance(rd, dict) else None
    if not entry:
        return False, ledger
    now = now or datetime.now(tz=_TZ)
    try:
        price_close = abs(float(entry["price"]) - float(want)) <= match
        ts = datetime.fromisoformat(str(entry["ts"]))
        age = (now - ts).total_seconds()
    except (KeyError, TypeError, ValueError):
        # Malformed entry -- fail safe: place normally, and drop the entry
        # so it doesn't keep tripping this branch every poll.
        ledger = dict(ledger)
        rd2 = dict(ledger.get("rail_debounce") or {})
        rd2.pop(side, None)
        ledger["rail_debounce"] = rd2
        return False, ledger

    if price_close and age <= debounce_sec:
        return True, ledger  # suppress; leave entry in place (still "live")

    # Different price, or aged out -- consumed/stale, clear so it can't
    # leak into a later, unrelated debounce decision.
    ledger = dict(ledger)
    rd3 = dict(ledger.get("rail_debounce") or {})
    rd3.pop(side, None)
    ledger["rail_debounce"] = rd3
    return False, ledger


# ------------------------------------------------------------------------
# Call sites — reconcile_once (RESERVE, only if Part 1 alone proves
# insufficient in live logs)
# ------------------------------------------------------------------------
#
# 1) "Cancel extras / wrong prices" loop (~L834-859), inside the existing
#    try/except, right after `actions.append(act)`:
#
#         actions.append(act)
#         ledger = record_rail_debounce_after_cancel(
#             ledger, side, px,
#             want=want, open_pos=open_pos,
#             quiet_skip_reason=quiet_skip, cancel_ok=act.get("ok", False),
#         )
#
# 2) "Place missing rails" loop (~L866-900):
#
#     for side, want in (("S", want_s), ("L", want_l)):
#         if want is None:
#             continue
#         if rail_ok(side, float(want)):
#             rd = dict(ledger.get("rail_debounce") or {})
#             if rd.pop(side, None) is not None:      # NEW: stale-clear
#                 ledger = dict(ledger); ledger["rail_debounce"] = rd
#             continue
#         skip, ledger = should_skip_redundant_place(          # NEW
#             ledger, side, float(want), open_pos=open_pos, match=match,
#         )
#         if skip:
#             out.setdefault("debounced_places", []).append({"side": side, "price": want})
#             continue
#         if not budget():
#             break
#         # ... existing place_futopt_order() body unchanged ...
#
# Honest caveat (unchanged from candidate A, still true after the fix):
# this only removes the redundant PLACE leg, not the original CANCEL leg —
# by the time we know want will bounce back, the cancel has already fired.
# Net effect per flicker cycle: one broker round trip removed (was
# cancel+place+place..., becomes cancel+[skip-or-place]), not zero. A
# stricter zero-round-trip fix would require delaying the cancel itself,
# which reintroduces the exact "blocked-side order sits too long" risk
# this whole effort exists to avoid — so cancel always stays immediate,
# only the mirror-image place is ever throttled.


# ==============================================================================
# UNIT TESTS NEEDED (description-level; match style/rigor of
# QuietFlatEntryGateTest / DropFormingLastBarTest in
# tests/test_tmf_channel_order.py — small `_TZ`-aware datetime fixtures,
# ledger passed in as a plain dict, assert both the return values and the
# resulting ledger keys)
# ==============================================================================
#
# class QuietCancelThrottleTest(unittest.TestCase)   [Part 1 — should ship]
# ------------------------------------------------------------------------
# - test_first_quiet_cancel_not_suppressed_and_stamps_ledger:
#     should_throttle_quiet_cancel("S", quiet_skip_reason="quiet_flat_skip:dry|dry|2.1min",
#     open_pos=None, ledger={}, now=t0) -> (False, ledger) and
#     ledger["cancel_throttle_last"]["S"] == t0.isoformat().
# - test_second_quiet_cancel_within_window_is_suppressed_and_stamp_not_refreshed:
#     call once at t0 (records), call again at t0+timedelta(seconds=20) with
#     min_interval_sec=45 -> (True, ledger); assert
#     ledger["cancel_throttle_last"]["S"] still == t0.isoformat() (not bumped
#     to the second call's time -- pins "no sliding window" from the
#     function's own comment).
# - test_cancel_after_window_elapsed_is_not_suppressed_and_restamps:
#     same as above but second call at t0+timedelta(seconds=46) -> (False,
#     ledger) and the stamp is now the second call's time.
# - test_block_reason_never_suppressed_even_with_a_fresh_stamp:
#     seed ledger["cancel_throttle_last"]["S"] = t0.isoformat() (as if a
#     quiet cancel just fired), then call at t0+timedelta(seconds=1) with
#     quiet_skip_reason="block:S" -> (False, ledger) always, regardless of
#     how fresh the stamp is. This is the test that pins the
#     non-negotiable invariant: block cancels are NEVER suppressed by this
#     function under any ledger state.
# - test_mixed_block_and_quiet_reason_string_isolates_side [regression
#   test added per review's soft finding -- pins the exact string shape
#   should_throttle_quiet_cancel depends on from
#   apply_quiet_flat_entry_gate]:
#     reason = "block:S|quiet_flat_skip:both|dry|3.2min" (S hard-blocked,
#     both sides flagged quiet-mature). Assert side="S" -> not suppressed
#     via the throttle path is irrelevant/unreachable (caller wouldn't call
#     this for a blocked side) but DIRECTLY assert
#     should_throttle_quiet_cancel("S", quiet_skip_reason=reason, ...)
#     returns (False, ledger) [defensive fail-safe fires because
#     "block:S" in reason], and separately
#     should_throttle_quiet_cancel("L", quiet_skip_reason=reason, ...)
#     DOES apply normal throttle logic [since "block:L" not in reason and
#     "quiet_flat_skip" in reason] -- proving the substring check isolates
#     S from L within one combined reason string. If
#     apply_quiet_flat_entry_gate's joined-reason format is ever refactored
#     this test should break loudly.
# - test_open_pos_not_none_never_suppressed:
#     even with a fresh in-window stamp, open_pos={"s": "S", "n": 1,
#     "ep": ...} -> (False, ledger) always (defensive no-op check, matches
#     apply_quiet_flat_entry_gate's own scope).
# - test_malformed_stamp_in_ledger_fails_safe:
#     ledger={"cancel_throttle_last": {"S": "not-a-timestamp"}} -> caught
#     ValueError path -> treated as no prior stamp -> (False, ledger) and
#     the stamp gets correctly overwritten with a valid iso string.
# - test_reason_without_quiet_flat_skip_substring_is_not_suppressed:
#     quiet_skip_reason=None and quiet_skip_reason="" both -> (False,
#     ledger), i.e. calling this with a reason that isn't a quiet-skip
#     reason at all is inert (extra defense beyond the caller's own gating).
#
# Integration-level (one test, exercises the real call site once wired):
# - test_reconcile_once_throttles_repeated_quiet_cancel_but_never_a_block_cancel:
#     fake session/broker fixtures (reuse whatever fixture
#     KillSwitchFlattenStopgapTest / OneLotScaleBlockTest already use for
#     get_futopt_order_results / cancel_futopt_order / place_futopt_order),
#     drive two consecutive polls where pv flickers dry->contract->dry with
#     an already-resting S rail and no block; assert the second poll's cancel
#     count for that flicker is 0 (throttled) while the FIRST poll's cancel
#     (if pv genuinely enters quiet-mature) still fires. Then run a third
#     scenario where the active cell shows block=["S"] instead -- assert the
#     cancel for S fires on every single poll regardless of any throttle
#     stamp already in the ledger (this is the test that would have caught
#     a regression reintroducing the live 2026-08-08 bug).
#
# class RailDebounceRecordingTest(unittest.TestCase)   [Part 2 — reserve]
# ------------------------------------------------------------------------
# - test_records_only_when_want_is_none_not_on_price_drift [pins the
#   review's NEEDS-REVISION fix -- the single most important test in this
#   class]:
#     record_rail_debounce_after_cancel(ledger={}, side="S", px=44900.0,
#     want=44950.0 [non-None, i.e. a price-drift cancel], open_pos=None,
#     quiet_skip_reason=None, cancel_ok=True) -> returned ledger has NO
#     "rail_debounce" key at all / side not present. Contrast with the same
#     call but want=None -> "rail_debounce"]["S"]["price"] == 44900.0.
# - test_records_nothing_when_cancel_failed (cancel_ok=False).
# - test_records_nothing_when_in_position (open_pos not None).
# - test_records_nothing_for_block_reason ("block:S" in quiet_skip_reason).
# - test_records_independently_per_side (recording S doesn't touch L's
#   existing/absent entry -- dict merge, not dict replace).
#
# class RailDebouncePlaceTest(unittest.TestCase)   [Part 2 — reserve]
# ------------------------------------------------------------------------
# - test_skips_place_within_window_at_matching_price:
#     ledger={"rail_debounce": {"S": {"price": 44900.0, "ts": t0.isoformat()}}},
#     call at t0+timedelta(seconds=10) with want=44900.5, match=1.0,
#     debounce_sec=25 -> (True, ledger), entry still present (not consumed
#     -- a suppressed poll doesn't equal "handled").
# - test_does_not_skip_after_window_elapsed_and_clears_stale_entry:
#     same setup, call at t0+timedelta(seconds=26) -> (False, ledger), and
#     ledger["rail_debounce"] no longer has "S" (stale-cleared).
# - test_does_not_skip_when_price_differs_beyond_match_and_clears_entry:
#     entry at 44900.0, call with want=44930.0 (outside match=1.0) even
#     within the time window -> (False, ledger), entry cleared (this is a
#     genuinely different want, not a redundant repeat -- must place).
# - test_open_pos_not_none_bypasses_entirely:
#     even with a matching fresh entry, open_pos not None -> (False,
#     ledger), entry left untouched (defensive no-op, never mutates state
#     for a path that structurally can't apply).
# - test_malformed_ledger_entry_fails_safe_and_self_heals:
#     entry missing "ts" key or containing a non-ISO string -> caught
#     exception path -> (False, ledger), entry removed so the poll after
#     doesn't keep re-hitting the same malformed data.
# - test_no_entry_present_is_a_pure_noop: ledger={} -> (False, ledger),
#     identity-equal or at least equal-by-value, no new keys added.
