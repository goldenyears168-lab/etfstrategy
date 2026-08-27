#!/usr/bin/env python3
"""READ-ONLY audit: close the CELL_TUNE_V3 w25-equivalent validation gap and
apply BUG-7 (exit_reason decomposition) to the already-cited w83/holdout
evidence.

Does NOT edit src/order/tmf_channel_pv16_book.py or any live config. Pure
research-harness re-run + trade-level decomposition, reusing the live engine
and the live PAPER_RECIPE / CELL_TUNE_V2_PATCHES / CELL_TUNE_V3_PATCHES
constants verbatim via import (no signal-logic reimplementation).

Part A — w83 bar-level + tick-rebuilt dual-window check (mirrors
  scripts/research/tx_channel_v3_tick_rebuild_holdouts.py, but for the w83
  window instead of the 3 OOS holdouts) — reproduces the w83 numbers cited
  in config/strategy.yaml.

Part B — BUG-7 exit_reason decomposition on the *blocked* cells
  (day|normal, day|div_hh_weak_vol): what would those trades' exit_reason
  mix look like if V3 did NOT block them, across w83+w25+3 holdouts pooled?
  If their negative PnL is concentrated in a few forced-close/EOD-style
  exits rather than broadly distributed across ordinary exit reasons
  (stop/struct_break/max_hold/trail/vix_flat), that would undermine the
  "confirmed structural loss center" framing used to justify blocking them.

Part C — BUG-7 on the *surviving* book's improvement: pool all non-blocked
  trades across w83, before vs after V3, decompose the delta by exit_reason
  to confirm the improvement isn't itself concentrated in a handful of
  EOD/session-flatten exits (which would be a different BUG-7 flavor: V3's
  apparent edge being a session-end/forced-close artifact rather than a
  genuine removal of a structural loser).

Run:
  PYTHONPATH=src .venv/bin/python \
    scripts/research/audit_cell_tune_v3_w25_gap_and_bug7.py
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from copy import deepcopy
from pathlib import Path
from statistics import mean, stdev

sys.path.insert(0, "src")

from tmf_channel.cache_store import list_days, load_day  # noqa: E402
from tmf_channel.engine import load_vixtwn_delta, simulate, summarize  # noqa: E402
from order.tmf_channel_config import PAPER_RECIPE  # noqa: E402
from order.tmf_channel_pv16_book import (  # noqa: E402
    RECIPE_VERSION,
    freeze_cell_book,
    SPECIALIZED_PATCHES,
    CELL_TUNE_V2_PATCHES,
    CELL_TUNE_V3_PATCHES,
    session_from_hhmm,
)

sys.path.insert(0, "scripts/research")
from tx_channel_tick_validation import load_front_month_ticks, resample_to_1min  # noqa: E402

OUT = Path("reports/research/channel_lab/audit_cell_tune_v3_w25_gap_and_bug7.json")

WINDOWS = {
    "w83": "tx_1m_fullnight_cache_full.json",
    "julsep25": "tx_1m_julsep_holdout_cache.json",
    "octdec25": "tx_1m_octdec_holdout_cache.json",
    "janmar26": "tx_1m_janmar_holdout_cache.json",
}
BLOCKED_CELLS = {("day", "normal"), ("day", "div_hh_weak_vol")}


def v2_only_book():
    book = freeze_cell_book()
    for sess, reg, upd in SPECIALIZED_PATCHES:
        book[sess][reg].update(upd)
    for sess, reg, upd in CELL_TUNE_V2_PATCHES:
        book[sess][reg].update(deepcopy(upd))
    return book


def v2_plus_v3_book():
    book = v2_only_book()
    for sess, reg, upd in CELL_TUNE_V3_PATCHES:
        book[sess][reg].update(deepcopy(upd))
    return book


def _arrays_from_rows(day: str, rows):
    O = [float(r["o"]) for r in rows]
    H = [float(r["h"]) for r in rows]
    L = [float(r["l"]) for r in rows]
    C = [float(r["c"]) for r in rows]
    V = [float(r.get("v") or 0) for r in rows]
    T = [f"{day}T{r.get('t')}:00.000+08:00" for r in rows]
    return O, H, L, C, V, T


def sim(day, rows, recipe, vix):
    O, H, L, C, V, T = _arrays_from_rows(day, rows)
    trades, *_ = simulate(O, H, L, C, V, T, deepcopy(recipe), vix_delta=vix)
    return trades or []


def build_hybrid_rows(day: str, cache_rows: list[dict]) -> tuple[list[dict], dict]:
    ticks = load_front_month_ticks(day)
    stats = dict(day_rows=0, replaced=0, fallback=0, had_ticks=ticks is not None)
    if ticks is None or ticks.empty:
        return cache_rows, stats
    tb = resample_to_1min(ticks)
    if tb.empty:
        return cache_rows, stats
    tb_by_hhmm = {row["Datetime"].strftime("%H:%M"): row for _, row in tb.iterrows()}
    out = []
    for r in cache_rows:
        if r["sess"] != "day":
            out.append(r)
            continue
        stats["day_rows"] += 1
        tr = tb_by_hhmm.get(r["t"])
        if tr is None:
            stats["fallback"] += 1
            out.append(r)
            continue
        stats["replaced"] += 1
        out.append(dict(t=r["t"], o=float(tr["Open"]), h=float(tr["High"]),
                         l=float(tr["Low"]), c=float(tr["Close"]),
                         v=float(tr["Volume"]), sess="day"))
    return out, stats


def day_clustered_t(deltas: list[float]) -> tuple[float, float]:
    n = len(deltas)
    if n < 2:
        return float("nan"), float("nan")
    dmean = mean(deltas)
    dsd = stdev(deltas)
    se = dsd / (n ** 0.5)
    t = dmean / se if se > 0 else float("inf")
    try:
        from scipy import stats
        p = 2 * (1 - stats.t.cdf(abs(t), df=n - 1))
    except ImportError:
        p = float("nan")
    return t, p


def _hhmm(ts: str) -> str:
    s = str(ts or "")
    if "T" in s:
        return s.split("T", 1)[1][:5]
    return s.split()[-1][:5] if s else ""


def trade_session(t: dict) -> str:
    return session_from_hhmm(_hhmm(t.get("et") or ""))


def exit_reason_bucket(why: str) -> str:
    """Coarsen the free-form `why` string (which often embeds a numeric
    suffix like 'stop|123') into a stable category for BUG-7 grouping."""
    w = why or ""
    if w == "EOD":
        return "EOD_force_close"
    if w.startswith("session_flat") or w.startswith("vix_") and "flat" in w:
        return "session_vix_flatten"
    if w.startswith("stop|"):
        return "stop"
    if w.startswith("struct_break|"):
        return "struct_break"
    if w.startswith("max_hold|"):
        return "max_hold"
    if w.startswith("trail|"):
        return "trail"
    return f"other:{w.split('|')[0]}" if w else "unknown"


def main() -> None:
    vix = load_vixtwn_delta() or {}
    before = v2_only_book()
    after = v2_plus_v3_book()
    recipe_before = deepcopy(PAPER_RECIPE)
    recipe_before["session_pv_book"] = before
    recipe_after = deepcopy(PAPER_RECIPE)
    recipe_after["session_pv_book"] = after
    assert recipe_after["session_pv_book"] == PAPER_RECIPE["session_pv_book"], (
        "v2_plus_v3_book() must reconstruct the exact live book"
    )

    out: dict = dict(
        title="CELL_TUNE_V3 w25-gap closure + BUG-7 exit_reason decomposition",
        recipe_version=RECIPE_VERSION,
        windows={},
        blocked_cell_bug7={},
        surviving_book_bug7_w83={},
    )

    # ---------------- Part A: dual-window bar + tick rebuild per window ----
    pooled_blocked_before_trades = []  # trades that WOULD have fired in the two blocked cells
    survivors_before_w83: list[dict] = []
    survivors_after_w83: list[dict] = []

    for wname, cache in WINDOWS.items():
        days = list_days(cache)
        bar_deltas, tick_deltas = [], []
        n_tick_days = 0
        total_day_rows = total_replaced = total_fallback = 0
        for d in days:
            rows = load_day(d, source=cache)
            if not rows:
                continue
            tr_before = sim(d, rows, recipe_before, vix)
            tr_after = sim(d, rows, recipe_after, vix)
            nb_before = sum(float(t["pnl"]) for t in tr_before)
            nb_after = sum(float(t["pnl"]) for t in tr_after)
            bar_deltas.append(nb_after - nb_before)

            for t in tr_before:
                if (trade_session(t), t.get("regime_e")) in BLOCKED_CELLS:
                    t2 = dict(t)
                    t2["window"] = wname
                    t2["day"] = d
                    pooled_blocked_before_trades.append(t2)

            if wname == "w83":
                for t in tr_before:
                    if (trade_session(t), t.get("regime_e")) not in BLOCKED_CELLS:
                        t2 = dict(t)
                        t2["day"] = d
                        survivors_before_w83.append(t2)
                for t in tr_after:
                    t2 = dict(t)
                    t2["day"] = d
                    survivors_after_w83.append(t2)

            hybrid, stats = build_hybrid_rows(d, rows)
            if stats["had_ticks"] and stats["replaced"] > 0:
                n_tick_days += 1
                total_day_rows += stats["day_rows"]
                total_replaced += stats["replaced"]
                total_fallback += stats["fallback"]
                nt_before = sum(float(t["pnl"]) for t in sim(d, hybrid, recipe_before, vix))
                nt_after = sum(float(t["pnl"]) for t in sim(d, hybrid, recipe_after, vix))
                tick_deltas.append(nt_after - nt_before)

        bt, bp = day_clustered_t(bar_deltas)
        row = dict(
            n_days=len(bar_deltas),
            bar_sum_delta=round(sum(bar_deltas), 1),
            bar_mean_delta=round(mean(bar_deltas), 2) if bar_deltas else None,
            bar_t=round(bt, 3) if bar_deltas else None,
            bar_p=round(bp, 4) if bar_deltas and bar_deltas.__len__() > 1 else None,
        )
        if tick_deltas:
            tt, tp = day_clustered_t(tick_deltas)
            cov = 100.0 * total_replaced / total_day_rows if total_day_rows else 0.0
            row.update(
                tick_n_days=n_tick_days,
                tick_sum_delta=round(sum(tick_deltas), 1),
                tick_mean_delta=round(mean(tick_deltas), 2),
                tick_t=round(tt, 3),
                tick_p=round(tp, 4),
                tick_coverage_pct=round(cov, 1),
                tick_fallback=total_fallback,
                tick_day_rows=total_day_rows,
            )
        out["windows"][wname] = row
        print(f"{wname}: {row}", flush=True)

    # w25 = last 25 days of the SAME w83 cache (v2 graduation convention)
    w83_days = list_days("tx_1m_fullnight_cache_full.json")
    w25_days = w83_days[-25:]
    w25_bar_deltas = []
    for d in w25_days:
        rows = load_day(d, source="tx_1m_fullnight_cache_full.json")
        if not rows:
            continue
        nb_before = sum(float(t["pnl"]) for t in sim(d, rows, recipe_before, vix))
        nb_after = sum(float(t["pnl"]) for t in sim(d, rows, recipe_after, vix))
        w25_bar_deltas.append(nb_after - nb_before)
    wt, wp = day_clustered_t(w25_bar_deltas)
    out["windows"]["w25_IS"] = dict(
        n_days=len(w25_bar_deltas),
        first_day=w25_days[0],
        last_day=w25_days[-1],
        bar_sum_delta=round(sum(w25_bar_deltas), 1),
        bar_mean_delta=round(mean(w25_bar_deltas), 2),
        bar_t=round(wt, 3),
        bar_p=round(wp, 4),
    )
    print(f"w25_IS: {out['windows']['w25_IS']}", flush=True)

    # dual-window accept criterion (same as v2 graduation): w83 book mean
    # up AND w25 book mean not down.
    w83_row = out["windows"]["w83"]
    w25_row = out["windows"]["w25_IS"]
    out["dual_window_accept"] = dict(
        w83_delta_up=w83_row["bar_sum_delta"] > 0,
        w25_delta_not_down=w25_row["bar_sum_delta"] >= 0,
        accept=(w83_row["bar_sum_delta"] > 0 and w25_row["bar_sum_delta"] >= 0),
    )

    # ---------------- Part B: BUG-7 on the blocked-cell trades -------------
    by_reason: dict[str, list[float]] = defaultdict(list)
    for t in pooled_blocked_before_trades:
        by_reason[exit_reason_bucket(t.get("why"))].append(float(t["pnl"]))
    total_pnl = sum(float(t["pnl"]) for t in pooled_blocked_before_trades)
    breakdown = {
        k: dict(n=len(v), net=round(sum(v), 1), mean=round(mean(v), 2))
        for k, v in sorted(by_reason.items(), key=lambda kv: sum(kv[1]))
    }
    forced_reasons = {"EOD_force_close", "session_vix_flatten"}
    forced_net = sum(sum(v) for k, v in by_reason.items() if k in forced_reasons)
    non_forced_net = total_pnl - forced_net
    out["blocked_cell_bug7"] = dict(
        note="Trades that WOULD have fired in day|normal / day|div_hh_weak_vol "
        "under the v2-only (pre-V3-block) book, pooled across w83+w25+3 "
        "holdouts, decomposed by exit_reason.",
        n_trades=len(pooled_blocked_before_trades),
        total_pnl=round(total_pnl, 1),
        by_exit_reason=breakdown,
        forced_close_reasons=sorted(forced_reasons),
        forced_close_net=round(forced_net, 1),
        non_forced_net=round(non_forced_net, 1),
        structural_loss_survives_excl_forced_close=non_forced_net < 0,
    )

    # ---------------- Part C: BUG-7 on the w83 surviving-book improvement --
    def bucket_pnl(trades):
        by = defaultdict(list)
        for t in trades:
            by[exit_reason_bucket(t.get("why"))].append(float(t["pnl"]))
        return by

    by_before = bucket_pnl(survivors_before_w83)
    by_after = bucket_pnl(survivors_after_w83)
    all_reasons = sorted(set(by_before) | set(by_after))
    delta_by_reason = {}
    for r in all_reasons:
        b = sum(by_before.get(r, []))
        a = sum(by_after.get(r, []))
        delta_by_reason[r] = dict(
            before_net=round(b, 1), after_net=round(a, 1), delta=round(a - b, 1),
            before_n=len(by_before.get(r, [])), after_n=len(by_after.get(r, [])),
        )
    total_delta = sum(v["delta"] for v in delta_by_reason.values())
    forced_delta = sum(v["delta"] for k, v in delta_by_reason.items() if k in forced_reasons)
    non_forced_delta = total_delta - forced_delta
    out["surviving_book_bug7_w83"] = dict(
        note="w83 book-level delta (v2+V3 minus v2-only) restricted to the "
        "NOT-blocked cells only ('before' side) vs the full v2+V3 live book "
        "('after' side includes day|normal/day|div_hh_weak_vol as blocked, "
        "i.e. zero trades there) — decomposed by exit_reason to check "
        "whether the net improvement is broadly distributed or concentrated "
        "in EOD/session-flatten exits.",
        total_delta=round(total_delta, 1),
        forced_close_delta=round(forced_delta, 1),
        non_forced_delta=round(non_forced_delta, 1),
        improvement_survives_excl_forced_close=non_forced_delta > 0,
        by_exit_reason=delta_by_reason,
    )

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    print(f"\nWrote {OUT}", flush=True)
    print(json.dumps(out["dual_window_accept"], indent=2), flush=True)
    print(json.dumps(out["blocked_cell_bug7"], indent=2)[:2000], flush=True)
    print(json.dumps(out["surviving_book_bug7_w83"], indent=2)[:2000], flush=True)


if __name__ == "__main__":
    main()
