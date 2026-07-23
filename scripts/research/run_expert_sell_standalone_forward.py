#!/usr/bin/env python3
"""H_EXPERT_SELL_STANDALONE_WIND: core net-sell day → forward excess (no green).

Research only · sqlite mode=ro · thick-14 · does NOT touch Strategy/Order.

Event = expert-pool core net-sell day (standalone; green NOT required).
Ask: does it predict weaker subsequent r_adj (風向)?

  PYTHONPATH=src .venv/bin/python scripts/research/run_expert_sell_standalone_forward.py
"""
from __future__ import annotations

import importlib.util
import json
import random
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from statistics import mean, median

ROOT = Path(__file__).resolve().parents[2]
OUT_MD = (
    ROOT
    / "reports/research/branch-footprint-screen/expert_pool/hypotheses"
    / "H_EXPERT_SELL_STANDALONE_WIND.md"
)
OUT_JSON = OUT_MD.with_suffix(".json")
DB = ROOT / "data" / "stocks.db"
SOURCE = "finmind"
START, END, OOS = "2024-07-01", "2026-07-20", "2026-01-01"
COST, BETA, DEDUP = 0.003, 1.15, 5
SOFT_FLOOR_MIN = 2.5e7
HORIZONS = (1, 3, 5, 7)
RNG_SEED = 42
# Matched non-event: same sid, same month, not within DEDUP of any event of that type
MATCH_PER_EVENT = 3

UNIVERSE = [
    "2327",
    "2337",
    "2344",
    "2383",
    "2408",
    "3037",
    "3189",
    "3443",
    "3481",
    "3653",
    "3665",
    "6223",
    "8046",
    "8358",
]

# A/B/C/D per brief; D skipped per-sid when core_n < 3
EVENT_DEFS = (
    ("1HARD", "n_hard", 1),
    ("2SOFT+", "n_soft", 2),
    ("2HARD", "n_hard", 2),
    ("3HARD+", "n_hard", 3),
)


def _load_watch_module():
    spec = importlib.util.spec_from_file_location(
        "epw_standalone_sell", ROOT / "scripts/research/run_expert_pool_watch.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["epw_standalone_sell"] = mod
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def load_champions(mod) -> tuple[dict, dict, dict, dict]:
    cores: dict[str, dict[str, str]] = {}
    floors: dict[str, float] = {}
    modes: dict[str, str] = {}
    names: dict[str, str] = {}
    for sid in UNIVERSE:
        mode, floor, core, name = "回看1日共識", 5e7, {}, sid
        ws = (
            ROOT
            / "reports/research/branch-footprint-screen/expert_pool"
            / sid
            / "watch_spec.json"
        )
        if ws.exists():
            w = json.loads(ws.read_text(encoding="utf-8"))
            core = {str(k): str(v) for k, v in (w.get("core") or {}).items()}
            floor = float(w.get("net_floor") or 5e7)
            fl = w.get("floor_label") or ""
            if "1億" in fl:
                floor = float(w.get("net_floor") or 1e8)
            mode = (w.get("champion") or {}).get("mode") or mode
            name = w.get("stock_name") or name
        if sid in mod.POOLS:
            p = mod.POOLS[sid]
            if not core:
                core = {str(k): str(v) for k, v in p.core.items()}
            floor = float(p.net_floor)
            name = p.stock_name or name
        if sid == "2344" and not ws.exists():
            mode = "同日共識"
        cores[sid] = core
        floors[sid] = floor
        modes[sid] = mode
        names[sid] = name
    return cores, floors, modes, names


def soft_hard(floor: float) -> tuple[float, float]:
    hard = float(floor)
    soft = max(hard * 0.5, SOFT_FLOOR_MIN)
    if soft > hard:
        soft = hard
    return soft, hard


def dedupe_ok(last: dict[str, str], sid: str, d: str) -> bool:
    if sid not in last:
        return True
    return (
        datetime.strptime(d, "%Y-%m-%d") - datetime.strptime(last[sid], "%Y-%m-%d")
    ).days > DEDUP


def blk(xs: list[float]) -> dict:
    if not xs:
        return {"n": 0, "med": None, "mean": None, "win": None}
    return {
        "n": len(xs),
        "med": median(xs),
        "mean": mean(xs),
        "win": sum(1 for x in xs if x > 0) / len(xs),
    }


def pct(x: float | None, digits: int = 2) -> str:
    if x is None:
        return "—"
    return f"{x * 100:+.{digits}f}%"


def win_pct(x: float | None) -> str:
    if x is None:
        return "—"
    return f"{x * 100:.0f}%"


def delta(a: float | None, b: float | None) -> float | None:
    if a is None or b is None:
        return None
    return a - b


def main() -> None:
    mod = _load_watch_module()
    cores, floors, modes, names = load_champions(mod)
    print("universe", len(UNIVERSE))
    for sid in UNIVERSE:
        soft, hard = soft_hard(floors[sid])
        print(
            f"  {sid} {names[sid]} mode={modes[sid]} "
            f"hard={hard:.0f} soft={soft:.0f} core_n={len(cores[sid])}"
        )

    con = sqlite3.connect(f"file:{DB.resolve()}?mode=ro", uri=True, timeout=120)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    con.execute("PRAGMA busy_timeout=120000")
    ph = ",".join("?" * len(UNIVERSE))

    bars: dict[str, dict[str, tuple[float, float]]] = defaultdict(dict)
    for r in con.execute(
        f"""SELECT stock_id, trade_date, open, close FROM stock_daily_bars
            WHERE source=? AND stock_id IN ({ph})
              AND trade_date BETWEEN date(?, '-5 days') AND date(?, '+25 days')
              AND close>0""",
        (SOURCE, *UNIVERSE, START, END),
    ):
        o = float(r["open"] or r["close"])
        c = float(r["close"])
        if o > 0:
            bars[r["stock_id"]][r["trade_date"]] = (o, c)

    ix: dict[str, tuple[float, float]] = {}
    for r in con.execute(
        """SELECT date, open, close FROM daily_bars
           WHERE code='IX0001'
             AND date BETWEEN date(?, '-5 days') AND date(?, '+25 days')
             AND open>0 AND close>0
           ORDER BY date,
             CASE source WHEN 'yahoo' THEN 0 WHEN 'tej' THEN 1
                  WHEN 'finmind' THEN 2 ELSE 3 END""",
        (START, END),
    ):
        ix.setdefault(r["date"], (float(r["open"]), float(r["close"])))
    ix_dates = sorted(ix)

    core_tids = set().union(*(set(c) for c in cores.values()))
    core_by: dict[str, dict[str, dict[str, float]]] = defaultdict(
        lambda: defaultdict(dict)
    )
    if core_tids:
        ph_c = ",".join("?" * len(core_tids))
        for r in con.execute(
            f"""SELECT stock_id, trade_date, securities_trader_id, net
                FROM stock_broker_branch_daily
                WHERE source=? AND stock_id IN ({ph})
                  AND securities_trader_id IN ({ph_c})
                  AND trade_date BETWEEN ? AND date(?, '+25 days')
                  AND net != 0""",
            (SOURCE, *UNIVERSE, *core_tids, START, END),
        ):
            oc = bars[r["stock_id"]].get(r["trade_date"])
            if oc:
                core_by[r["stock_id"]][r["trade_date"]][
                    r["securities_trader_id"]
                ] = float(r["net"]) * oc[1]
    # latest bar date for report
    max_bar = max(
        (d for sid in UNIVERSE for d in bars[sid] if d <= END),
        default=END,
    )
    con.close()

    path_cals = {sid: sorted(bars[sid]) for sid in UNIVERSE}
    path_idx = {sid: {d: i for i, d in enumerate(path_cals[sid])} for sid in UNIVERSE}
    cals = {
        sid: sorted(d for d in bars[sid] if START <= d <= END) for sid in UNIVERSE
    }

    def day_core_amts(sid: str, d: str) -> dict[str, float]:
        core = cores[sid]
        return {t: a for t, a in core_by[sid].get(d, {}).items() if t in core}

    def sell_counts(sid: str, d: str) -> dict:
        soft, hard = soft_hard(floors[sid])
        amts = day_core_amts(sid, d)
        sell_amts = [-a for a in amts.values() if a < 0]
        return {
            "n_soft": sum(1 for s in sell_amts if s >= soft),
            "n_hard": sum(1 for s in sell_amts if s >= hard),
            "soft": soft,
            "hard": hard,
            "core_n": len(cores[sid]),
        }

    def bench_ret(ed: str, xd: str) -> float | None:
        """L1 open → Hx close on IX0001 (yahoo→tej→finmind prefer via load)."""
        j = next((k for k, d in enumerate(ix_dates) if d >= ed), None)
        xj = next((k for k, d in enumerate(ix_dates) if d >= xd), None)
        if j is None or xj is None:
            return None
        bo = ix[ix_dates[j]][0]
        bc = ix[ix_dates[xj]][1]
        if bo <= 0:
            return None
        return bc / bo - 1

    def forward_radj(sid: str, sig: str, h: int) -> dict | None:
        """From next session open (L1) after signal day T → H-session close.

        Hold H sessions: entry = cal[i+1] open, exit = cal[i+H] close
        (H=1 → same as L1 close; H=7 → L1..L7 with exit on L7 close).
        """
        cal = path_cals[sid]
        pi = path_idx[sid].get(sig)
        if pi is None:
            return None
        entry_i = pi + 1
        exit_i = entry_i + h - 1
        if exit_i >= len(cal):
            return None
        ed = cal[entry_i]
        xd = cal[exit_i]
        ep = bars[sid][ed][0]
        xp = bars[sid][xd][1]
        if ep <= 0 or xp <= 0:
            return None
        ret_raw = xp / ep - 1
        bret = bench_ret(ed, xd)
        if bret is None:
            return None
        radj_raw = ret_raw - BETA * bret
        # cost-adjusted: simulate short/avoid entry friction (30bps once)
        radj_cost = (ret_raw - COST) - BETA * bret
        return {
            "sid": sid,
            "sig": sig,
            "entry": ed,
            "exit": xd,
            "h": h,
            "radj": radj_raw,
            "radj_cost": radj_cost,
            "oos": sig >= OOS,
        }

    # --- collect events per type (deduped) ---
    events: dict[str, list[tuple[str, str]]] = {k: [] for k, _, _ in EVENT_DEFS}
    last_ev: dict[str, dict[str, str]] = {k: {} for k, _, _ in EVENT_DEFS}
    skipped_3hard = [sid for sid in UNIVERSE if len(cores[sid]) < 3]

    for sid in UNIVERSE:
        for d in cals[sid]:
            sc = sell_counts(sid, d)
            for ev_name, key, thr in EVENT_DEFS:
                if ev_name == "3HARD+" and sc["core_n"] < 3:
                    continue
                if sc[key] < thr:
                    continue
                if not dedupe_ok(last_ev[ev_name], sid, d):
                    continue
                # need forward room for H7
                if forward_radj(sid, d, 7) is None:
                    continue
                events[ev_name].append((sid, d))
                last_ev[ev_name][sid] = d

    rng = random.Random(RNG_SEED)

    def month_key(d: str) -> str:
        return d[:7]

    def event_set(ev_name: str) -> set[tuple[str, str]]:
        return set(events[ev_name])

    def near_event(sid: str, d: str, ev_dates: dict[str, list[str]]) -> bool:
        for ed in ev_dates.get(sid, []):
            if abs(
                (
                    datetime.strptime(d, "%Y-%m-%d")
                    - datetime.strptime(ed, "%Y-%m-%d")
                ).days
            ) <= DEDUP:
                return True
        return False

    def build_baseline_pool(ev_name: str) -> list[tuple[str, str]]:
        """Same-stock non-event days outside ±DEDUP of any event of this type."""
        ev_dates: dict[str, list[str]] = defaultdict(list)
        for sid, d in events[ev_name]:
            ev_dates[sid].append(d)
        pool: list[tuple[str, str]] = []
        for sid in UNIVERSE:
            for d in cals[sid]:
                if near_event(sid, d, ev_dates):
                    continue
                if forward_radj(sid, d, 7) is None:
                    continue
                pool.append((sid, d))
        return pool

    def matched_controls(ev_name: str) -> list[tuple[str, str]]:
        """Matched: same sid + same calendar month; up to MATCH_PER_EVENT each."""
        pool = build_baseline_pool(ev_name)
        by_sid_m: dict[tuple[str, str], list[str]] = defaultdict(list)
        for sid, d in pool:
            by_sid_m[(sid, month_key(d))].append(d)
        out: list[tuple[str, str]] = []
        for sid, d in events[ev_name]:
            cands = list(by_sid_m.get((sid, month_key(d)), []))
            rng.shuffle(cands)
            for cd in cands[:MATCH_PER_EVENT]:
                out.append((sid, cd))
        return out

    def all_days_sample(ev_name: str, n_target: int) -> list[tuple[str, str]]:
        pool = build_baseline_pool(ev_name)
        if not pool:
            return []
        if len(pool) <= n_target:
            return list(pool)
        return rng.sample(pool, n_target)

    def summarize_days(days: list[tuple[str, str]], h: int, *, key: str = "radj") -> dict:
        xs_all: list[float] = []
        xs_oos: list[float] = []
        for sid, d in days:
            row = forward_radj(sid, d, h)
            if row is None:
                continue
            xs_all.append(row[key])
            if row["oos"]:
                xs_oos.append(row[key])
        return {"all": blk(xs_all), "oos": blk(xs_oos)}

    results: dict = {}
    for ev_name, _, _ in EVENT_DEFS:
        matched = matched_controls(ev_name)
        # all-days baseline sized ~ events×MATCH for stability
        n_ev = len(events[ev_name])
        all_base = all_days_sample(ev_name, max(n_ev * MATCH_PER_EVENT, n_ev))
        results[ev_name] = {
            "n_events": n_ev,
            "n_oos_events": sum(1 for _, d in events[ev_name] if d >= OOS),
            "n_matched": len(matched),
            "n_all_base": len(all_base),
            "horizons": {},
        }
        for h in HORIZONS:
            ev_s = summarize_days(events[ev_name], h, key="radj")
            ev_c = summarize_days(events[ev_name], h, key="radj_cost")
            m_s = summarize_days(matched, h, key="radj")
            a_s = summarize_days(all_base, h, key="radj")
            results[ev_name]["horizons"][str(h)] = {
                "event_raw": ev_s,
                "event_cost": ev_c,
                "matched_raw": m_s,
                "all_days_raw": a_s,
                "delta_med_vs_matched_all": delta(
                    ev_s["all"]["med"], m_s["all"]["med"]
                ),
                "delta_med_vs_matched_oos": delta(
                    ev_s["oos"]["med"], m_s["oos"]["med"]
                ),
                "delta_med_vs_alldays_all": delta(
                    ev_s["all"]["med"], a_s["all"]["med"]
                ),
                "delta_med_vs_alldays_oos": delta(
                    ev_s["oos"]["med"], a_s["oos"]["med"]
                ),
            }
        print(
            f"{ev_name}: n={n_ev} oos={results[ev_name]['n_oos_events']} "
            f"matched={len(matched)}"
        )

    # Verdict (observation/wind only — not exit).
    # Require BOTH absolute weakness tendency AND relative vs matched/all-days.
    # Sells that cluster in hot months can lose to matched peers yet still
    # post +r_adj — that is relative softness, not a clean 風向 avoid signal.
    focus = []
    for ev in ("2HARD", "2SOFT+", "1HARD", "3HARD+"):
        if ev not in results or results[ev]["n_events"] == 0:
            continue
        for h in (5, 7, 3, 1):
            cell = results[ev]["horizons"][str(h)]
            n_oos = cell["event_raw"]["oos"]["n"]
            if n_oos < 8:
                continue
            focus.append(
                {
                    "event": ev,
                    "h": h,
                    "med_oos": cell["event_raw"]["oos"]["med"],
                    "delta_matched_oos": cell["delta_med_vs_matched_oos"],
                    "delta_alldays_oos": cell["delta_med_vs_alldays_oos"],
                    "n_oos": n_oos,
                }
            )

    abs_weak = [
        f
        for f in focus
        if f["med_oos"] is not None and f["med_oos"] < -0.002 and f["h"] in (5, 7)
    ]
    rel_matched_weak = [
        f
        for f in focus
        if f["delta_matched_oos"] is not None
        and f["delta_matched_oos"] < -0.005
        and f["h"] in (5, 7)
    ]
    rel_alldays_weak = [
        f
        for f in focus
        if f["delta_alldays_oos"] is not None
        and f["delta_alldays_oos"] < -0.002
        and f["h"] in (5, 7)
    ]
    # Opposite of wind thesis: absolute strength after sell
    abs_strong = [
        f
        for f in focus
        if f["med_oos"] is not None and f["med_oos"] > 0.01 and f["h"] in (5, 7)
    ]

    if not focus:
        wind_label = "noise"
        wind_note = "Insufficient OOS n for primary arms; treat as noise."
    elif (
        abs_weak
        and (rel_matched_weak or rel_alldays_weak)
        and len(abs_strong) == 0
    ):
        wind_label = "useful"
        wind_note = (
            "OOS absolute med r_adj negative and below baseline on H5/H7 — "
            "soft wind/avoid annotation candidate only."
        )
    elif rel_matched_weak and (abs_strong or len(abs_weak) < len(rel_matched_weak)):
        wind_label = "weak"
        wind_note = (
            "Relative underperformance vs same-month matched is real on some "
            "H5/H7 cells (esp. 2HARD/2SOFT+), but absolute OOS excess is often "
            "still positive (and vs all-days often stronger — sells cluster in "
            "hot months). Not a clean absolute 風向 avoid; soft annotation only."
        )
    elif abs_strong and not abs_weak:
        wind_label = "noise"
        wind_note = (
            "Standalone sell does not predict weaker absolute forward excess "
            "OOS; medians often positive. Do not use as wind radar SSOT."
        )
    else:
        wind_label = "noise"
        wind_note = (
            "OOS pattern mixed / inconsistent across baselines; standalone sell "
            "is not a reliable weaker-forward-excess predictor."
        )

    payload = {
        "hypothesis": "H_EXPERT_SELL_STANDALONE_WIND",
        "protocol": {
            "universe": UNIVERSE,
            "window": [START, END],
            "max_bar_in_window": max_bar,
            "oos": OOS,
            "beta": BETA,
            "bench": "IX0001",
            "cost_bps": COST * 10000,
            "dedup_calendar_days": DEDUP,
            "forward": "L1 open → Hh close (signal T evening → next open)",
            "baseline": (
                f"matched same-sid same-month non-event (×{MATCH_PER_EVENT}); "
                "also all-days non-event sample"
            ),
            "tape": "stock_broker_branch_daily source=finmind; amt=net*close",
            "soft": f"max(floor*0.5, {SOFT_FLOOR_MIN})",
            "hard": "net_floor",
            "rng_seed": RNG_SEED,
        },
        "champions": {
            sid: {
                "name": names[sid],
                "mode": modes[sid],
                "net_floor": floors[sid],
                "soft": soft_hard(floors[sid])[0],
                "hard": soft_hard(floors[sid])[1],
                "core_n": len(cores[sid]),
            }
            for sid in UNIVERSE
        },
        "skipped_3hard_sids": skipped_3hard,
        "results": results,
        "verdict": {
            "wind_radar": wind_label,
            "note": wind_note,
            "exit_adoption": False,
            "focus_cells": focus,
            "counts": {
                "abs_weak_h57": len(abs_weak),
                "rel_matched_weak_h57": len(rel_matched_weak),
                "rel_alldays_weak_h57": len(rel_alldays_weak),
                "abs_strong_h57": len(abs_strong),
            },
        },
    }

    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    lines: list[str] = []
    lines.append("# H_EXPERT_SELL_STANDALONE_WIND")
    lines.append("")
    lines.append(
        "> Research only · standalone core net-sell → forward excess（無 green 條件）· "
        "**非出場採納**。"
    )
    lines.append("")
    lines.append("## Verdict")
    lines.append("")
    lines.append(
        f"**Wind radar（觀測／風向）**: `{wind_label}` — {wind_note}"
    )
    lines.append("")
    lines.append("- **Exit adoption**: **No**（本假設不主張出場規則）。")
    lines.append(
        "- Forward path: signal day **T**（EOD tape）→ **L1 open → Hh close**；"
        f"r_adj = r − {BETA}×r_IX0001（raw；另報 −{COST*10000:.0f}bps cost）。"
    )
    lines.append(
        f"- Baseline: same-sid same-month **matched non-event**（×{MATCH_PER_EVENT}，"
        f"排除事件 ±{DEDUP}d）；對照 all-days non-event sample。"
    )
    lines.append(f"- Dedupe: same-sid events >{DEDUP} calendar days apart.")
    lines.append(
        f"- Window `{START}`..`{max_bar}` · OOS signal ≥ `{OOS}` · thick-14."
    )
    if skipped_3hard:
        lines.append(
            f"- 3HARD+: skipped sids with core_n<3: `{', '.join(skipped_3hard)}` "
            "(none expected on thick-14)."
        )
    lines.append("")
    lines.append("## Protocol")
    lines.append("")
    lines.append("| item | value |")
    lines.append("|---|---|")
    lines.append(
        "| universe | "
        + ", ".join(UNIVERSE)
        + " |"
    )
    lines.append(f"| excess | \\(r_{{\\mathrm{{adj}}}}=r-{BETA}\\times r_{{IX0001}}\\) |")
    lines.append("| DB | `data/stocks.db` mode=ro · source=finmind |")
    lines.append("| hard | `net_floor` |")
    lines.append(f"| soft | `max(net_floor×0.5, {SOFT_FLOOR_MIN:.1e})` |")
    lines.append("| A 1HARD | ≥1 core net_sell_amt ≥ hard |")
    lines.append("| B 2SOFT+ | ≥2 core ≥ soft |")
    lines.append("| C 2HARD | ≥2 core ≥ hard |")
    lines.append("| D 3HARD+ | ≥3 core ≥ hard |")
    lines.append("")
    lines.append("## Champions")
    lines.append("")
    lines.append("| sid | 名 | mode | hard | soft | core_n |")
    lines.append("|---|---|---|---|---|---|")
    for sid in UNIVERSE:
        soft, hard = soft_hard(floors[sid])
        lines.append(
            f"| {sid} | {names[sid]} | {modes[sid]} | "
            f"≥{hard/1e8:.2f}億 | ≥{soft/1e8:.2f}億 | {len(cores[sid])} |"
        )
    lines.append("")
    lines.append("## Event counts")
    lines.append("")
    lines.append("| event | n (deduped) | n OOS | matched n |")
    lines.append("|---|---:|---:|---:|")
    for ev_name, _, _ in EVENT_DEFS:
        r = results[ev_name]
        lines.append(
            f"| {ev_name} | {r['n_events']} | {r['n_oos_events']} | {r['n_matched']} |"
        )
    lines.append("")
    lines.append("## Forward table（raw r_adj · vs matched）")
    lines.append("")
    lines.append(
        "| event | H | n | med | win% | OOS n | OOS med | OOS win% | "
        "Δmed ALL vs matched | Δmed OOS vs matched |"
    )
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for ev_name, _, _ in EVENT_DEFS:
        for h in HORIZONS:
            cell = results[ev_name]["horizons"][str(h)]
            er = cell["event_raw"]
            lines.append(
                f"| {ev_name} | H{h} | {er['all']['n']} | {pct(er['all']['med'])} | "
                f"{win_pct(er['all']['win'])} | {er['oos']['n']} | "
                f"{pct(er['oos']['med'])} | {win_pct(er['oos']['win'])} | "
                f"{pct(cell['delta_med_vs_matched_all'])} | "
                f"{pct(cell['delta_med_vs_matched_oos'])} |"
            )
    lines.append("")
    lines.append("## Cost-adjusted（−30bps · event only）")
    lines.append("")
    lines.append("| event | H | ALL med_cost | OOS med_cost |")
    lines.append("|---|---:|---:|---:|")
    for ev_name, _, _ in EVENT_DEFS:
        for h in HORIZONS:
            cell = results[ev_name]["horizons"][str(h)]
            ec = cell["event_cost"]
            lines.append(
                f"| {ev_name} | H{h} | {pct(ec['all']['med'])} | {pct(ec['oos']['med'])} |"
            )
    lines.append("")
    lines.append("## vs all-days baseline（Δmed）")
    lines.append("")
    lines.append("| event | H | ΔALL | ΔOOS |")
    lines.append("|---|---:|---:|---:|")
    for ev_name, _, _ in EVENT_DEFS:
        for h in HORIZONS:
            cell = results[ev_name]["horizons"][str(h)]
            lines.append(
                f"| {ev_name} | H{h} | {pct(cell['delta_med_vs_alldays_all'])} | "
                f"{pct(cell['delta_med_vs_alldays_oos'])} |"
            )
    lines.append("")
    lines.append("## Recommendation（observation / wind radar）")
    lines.append("")
    lines.append(
        f"- Label: **{wind_label}**. {wind_note}"
    )
    if wind_label == "weak":
        lines.append(
            "- Optional: annotate digest on **2HARD** (and maybe **2SOFT+**) as "
            "*relative* softness vs recent same-month peers — not an absolute avoid."
        )
    elif wind_label == "useful":
        lines.append(
            "- Prefer **2HARD / 2SOFT+** as soft avoid / risk posture cue only."
        )
    else:
        lines.append("- Do not elevate standalone sell to wind radar SSOT.")
    lines.append("- Do **not** claim exit adoption from this study.")
    lines.append("")
    lines.append(f"_Generated by `scripts/research/run_expert_sell_standalone_forward.py`_")
    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("wrote", OUT_MD)
    print("verdict", wind_label)


if __name__ == "__main__":
    main()
