"""TMF channel · PV16 specialized session×PV8 recipe book (Order SSOT).

16 cells = day|night × PV8. Observe H2H vs Final v1.1.3 PASS (causal O-anchor).
Hard lock: night climax_up block L+S. US VIX / nq_calib stay global (off by default).

v1.3.0 cell-tune v2 layer (@ COST=5.8 pe=5 STRICT_BAR): per-cell tuned patches
on top of SPECIALIZED_PATCHES. 12 patches kept; day|div_hh_weak_vol /
night|climax_up / night|climax_dn reverted after julsep-2025 OOS attribution.
Validation: 5/5 periods beat v1.2.0 (3 OOS seasons + w83 + w25) —
reports/research/channel_lab/r_cost_aware_cell_tune_wf.json.

Do not import from research scripts into Order reverse direction —
research may re-export from here later.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

RECIPE_VERSION = "final_v1_3_0_pv16_celltune_v2"
FROZEN_ID = "final_v1_3_0_pv16_celltune_v2"

PV8 = (
    "climax_up",
    "climax_dn",
    "expand_up",
    "expand_dn",
    "contract",
    "dry",
    "normal",
    "div_hh_weak_vol",
)

DAY_BLOCKS: dict[str, list[str]] = {
    "expand_up": ["L", "S"],
    "climax_up": ["L", "S"],
    "expand_dn": ["L"],
}
NIGHT_BLOCKS: dict[str, list[str]] = {
    "climax_up": ["L", "S"],
    "climax_dn": ["L"],
}

# Single-factor dual-window accepts (25d∩83d STRICT_TICK)
SPECIALIZED_PATCHES: tuple[tuple[str, str, dict[str, Any]], ...] = (
    ("day", "contract", {"hang_lo": 13.0, "hang_hi": 28.0}),
    ("night", "expand_dn", {"hang_lo": 16.0, "hang_hi": 30.0, "max_hold_bars": 16}),
    ("night", "normal", {"early_fill_gamma": 9.0}),
    ("night", "contract", {"hang_lo": 20.0, "hang_hi": 34.0}),
    ("day", "normal", {"early_fill_gamma": 10.0}),
)

# Cell-tune v2 (2026-08-06, applied AFTER SPECIALIZED_PATCHES; later wins on
# overlapping keys). Source: reports/research/channel_lab/cell_tune/*.json,
# combined-book WF validation r_cost_aware_cell_tune_wf.json.
CELL_TUNE_V2_PATCHES: tuple[tuple[str, str, dict[str, Any]], ...] = (
    ("day", "climax_up", {"block": [], "hang_lo": 27.0, "hang_hi": 42.0, "early_fill_gamma": 0}),
    ("day", "climax_dn", {"hang_lo": 12.0, "hang_hi": 27.0, "early_fill_gamma": 11.0, "max_hold_bars": 38}),
    ("day", "expand_up", {"hang_lo": 18.0, "hang_hi": 33.0}),
    ("day", "contract", {"hang_lo": 10.0, "hang_hi": 25.0, "early_fill_gamma": 11.0, "max_hold_bars": 38}),
    ("day", "dry", {"hang_lo": 13.0, "hang_hi": 28.0}),
    ("day", "normal", {"hang_lo": 12.0, "hang_hi": 27.0, "early_fill_gamma": 13.0, "max_hold_bars": 38}),
    ("night", "expand_dn", {"block": ["L", "S"]}),
    ("night", "contract", {"hang_lo": 16.0, "hang_hi": 30.0}),
    ("night", "dry", {"hang_lo": 14.0, "hang_hi": 28.0}),
    ("night", "normal", {"block": ["L", "S"]}),
    ("night", "expand_up", {"hang_lo": 15.0, "hang_hi": 29.0, "early_fill_gamma": 8.0, "max_hold_bars": 28}),
    ("night", "div_hh_weak_vol", {"block": ["L", "S"]}),
)


def freeze_cell_book() -> dict[str, dict[str, dict[str, Any]]]:
    """16-cell freeze clone (pre-specialization)."""
    day_base: dict[str, Any] = dict(
        hang_lo=15.0,
        hang_hi=30.0,
        early_fill_gamma=8.0,
        max_hold_bars=30,
        block=[],
        skip_quiet_mode="dry",
        bias=True,
        vixtwn_calib="blend",
        vixtwn_calib_gamma=5.0,
    )
    night_base: dict[str, Any] = dict(
        hang_lo=18.0,
        hang_hi=32.0,
        early_fill_gamma=5.0,
        max_hold_bars=20,
        block=[],
        skip_quiet_mode="dry",
        bias=True,
        vixtwn_calib="none",
    )
    book: dict[str, dict[str, dict[str, Any]]] = {"day": {}, "night": {}}
    for reg in PV8:
        d = dict(day_base)
        d["block"] = list(DAY_BLOCKS.get(reg) or [])
        book["day"][reg] = d
        n = dict(night_base)
        n["block"] = list(NIGHT_BLOCKS.get(reg) or [])
        book["night"][reg] = n
    return book


def specialized_cell_book() -> dict[str, dict[str, dict[str, Any]]]:
    book = freeze_cell_book()
    for sess, reg, upd in SPECIALIZED_PATCHES:
        book[sess][reg].update(upd)
    for sess, reg, upd in CELL_TUNE_V2_PATCHES:
        book[sess][reg].update(deepcopy(upd))
    return book


def session_from_hhmm(hm: str) -> str:
    return "night" if (hm >= "15:00" or hm < "05:00") else "day"


def hhmm_from_bar_t(t: str | None) -> str:
    """Extract HH:MM from a bar timestamp.

    Live bars use full ISO (``2026-08-06T11:12:00.000+08:00``). Taking ``[:5]``
    yields ``2026-``, which string-compares as night forever (``>= "15:00"``).
    """
    s = str(t or "").strip()
    if "T" in s:
        return s.split("T", 1)[1][:5]
    if len(s) >= 5 and s[2] == ":":
        return s[:5]
    return s[:5]


def cell_recipe(
    book: dict[str, dict[str, dict[str, Any]]] | None,
    *,
    session: str,
    pv: str,
) -> dict[str, Any]:
    if not book:
        return {}
    return dict((book.get(session) or {}).get(pv) or {})


def book_summary(
    book: dict[str, dict[str, dict[str, Any]]] | None,
) -> list[dict[str, Any]]:
    """Flat rows for dashboard / broadcast."""
    if not book:
        return []
    rows = []
    for sess in ("day", "night"):
        for pv in PV8:
            c = cell_recipe(book, session=sess, pv=pv)
            if not c:
                continue
            rows.append(
                {
                    "cell": f"{sess}|{pv}",
                    "session": sess,
                    "pv": pv,
                    "hang_lo": c.get("hang_lo"),
                    "hang_hi": c.get("hang_hi"),
                    "early_fill_gamma": c.get("early_fill_gamma"),
                    "max_hold_bars": c.get("max_hold_bars"),
                    "block": list(c.get("block") or []),
                    "skip_quiet_mode": c.get("skip_quiet_mode"),
                    "bias": bool(c.get("bias")),
                }
            )
    return rows


def active_cell_payload(
    *,
    session: str,
    pv: str,
    book: dict[str, dict[str, dict[str, Any]]] | None,
    nq_gate: str | None = None,
) -> dict[str, Any]:
    cell = cell_recipe(book, session=session, pv=pv)
    return {
        "cell": f"{session}|{pv}",
        "session": session,
        "pv": pv,
        "recipe": cell,
        "nq_gate": nq_gate,
        "recipe_version": RECIPE_VERSION,
    }


def paper_recipe_overlay() -> dict[str, Any]:
    """Keys merged into PAPER_RECIPE for Final v1.2.0."""
    return dict(
        hang_anchor="O",
        tick_native=False,  # live poll = 1m bars (STRICT_BAR floor)
        hang_lo=15.0,  # fallback when cell missing
        hang_hi=30.0,
        night_hang_lo=18.0,
        night_hang_hi=32.0,
        early_fill_gamma=5.0,
        max_hold_bars=25,
        open_greed_bypass_quiet=False,  # never bypass quiet=dry on live paper book
        skip_quiet_regime=False,
        skip_quiet_mode="dry",
        trend_hang_dampen="regime",
        session_pv_book=specialized_cell_book(),
        vixtwn_calib="blend",
        vixtwn_calib_gamma=5.0,
        us_vix_calib="none",
        nq_calib="none",
        recipe_version=RECIPE_VERSION,
    )
