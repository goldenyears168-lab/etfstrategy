#!/usr/bin/env python3
"""H_EXPERT_SELL_LOOKBACK_WINDOW: W0 same-day vs W1/W3 consensus sell windows.

Research only · sqlite mode=ro · thick-14 · β=1.15 · dedupe 5d · OOS≥2026-01-01.
Observation quality for email annotation — NOT an auto-exit claim.

Windows (require ≥1 hard seat on T to avoid stale spam):
  W0  ≥2 distinct core with net_sell≥hard on T
  W1  present_ids = hard-sell seats on T∪T-1; |present|≥2 ∧ ≥1 on T
  W3  present_ids = hard-sell seats on T∪T-1∪T-2; |present|≥2 ∧ ≥1 on T
Soft variants use soft floor = max(hard×0.5, 2500萬) with same topology.

Lenses
  1) Standalone wind: signal → L1 open → H3/H5/H7 close · vs non-signal baseline
  2) Path-exit overlay: after green, first W* → D+1 open else H7

  PYTHONPATH=src .venv/bin/python scripts/research/run_expert_sell_lookback_window.py
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import os
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from statistics import mean, median

ROOT = Path(__file__).resolve().parents[2]
OUT_MD = (
    ROOT
    / "reports/research/branch-footprint-screen/expert_pool/hypotheses"
    / "H_EXPERT_SELL_LOOKBACK_WINDOW.md"
)
OUT_JSON = OUT_MD.with_suffix(".json")
DB = (Path(os.environ.get("GOLDENSTOCKS_DATA_DIR") or ROOT) / "data" / "stocks.db")
SOURCE = "finmind"
START, END, OOS = "2024-07-01", "2026-07-20", "2026-01-01"
COST, BETA, DEDUP = 0.003, 1.15, 5
SOFT_FLOOR_MIN = 2.5e7
HOLD_HS = (3, 5, 7)
PRIMARY_H = 5

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

# (label, lookback_sessions including T, floor_kind)
WINDOWS = (
    ("W0_hard", 1, "hard"),
    ("W1_hard", 2, "hard"),
    ("W3_hard", 3, "hard"),
    ("W0_soft", 1, "soft"),
    ("W1_soft", 2, "soft"),
    ("W3_soft", 3, "soft"),
)


def _load_watch_module():
    spec = importlib.util.spec_from_file_location(
        "epw_lbw", ROOT / "scripts/research/run_expert_pool_watch.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["epw_lbw"] = mod
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


def main() -> None:
    mod = _load_watch_module()
    cores, floors, modes, names = load_champions(mod)
    print("universe", len(UNIVERSE), UNIVERSE)
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
              AND trade_date BETWEEN date(?, '-10 days') AND date(?, '+25 days')
              AND close>0""",
        (SOURCE, *UNIVERSE, START, END),
    ):
        o = float(r["open"] or r["close"])
        c = float(r["close"])
        if o <= 0:
            continue
        bars[r["stock_id"]][r["trade_date"]] = (o, c)

    ix: dict[str, tuple[float, float]] = {}
    for r in con.execute(
        """SELECT date, open, close FROM daily_bars
           WHERE code='IX0001'
             AND date BETWEEN date(?, '-10 days') AND date(?, '+25 days')
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
                  AND trade_date BETWEEN date(?, '-10 days') AND date(?, '+20 days')
                  AND net != 0""",
            (SOURCE, *UNIVERSE, *core_tids, START, END),
        ):
            oc = bars[r["stock_id"]].get(r["trade_date"])
            if oc:
                core_by[r["stock_id"]][r["trade_date"]][
                    r["securities_trader_id"]
                ] = float(r["net"]) * oc[1]
    con.close()
    print("loaded bars/ix/core nets")

    cals = {
        sid: sorted(d for d in bars[sid] if START <= d <= END) for sid in UNIVERSE
    }
    path_cals = {sid: sorted(bars[sid]) for sid in UNIVERSE}
    path_idx = {sid: {d: i for i, d in enumerate(path_cals[sid])} for sid in UNIVERSE}

    years = (
        datetime.strptime(END, "%Y-%m-%d") - datetime.strptime(START, "%Y-%m-%d")
    ).days / 365.25
    n_stock_days = sum(len(cals[sid]) for sid in UNIVERSE)

    def expert_green(sid: str, d: str) -> bool:
        core = cores[sid]
        if not core:
            return False
        floor = floors[sid]
        mode = modes[sid]
        pi = path_idx[sid].get(d)
        if pi is None:
            return False
        today = {
            t
            for t, a in core_by[sid].get(d, {}).items()
            if t in core and a >= floor
        }
        yday: set[str] = set()
        if pi > 0:
            yd = path_cals[sid][pi - 1]
            yday = {
                t
                for t, a in core_by[sid].get(yd, {}).items()
                if t in core and a >= floor
            }
        if "OR" in mode:
            return len(today) >= 1
        if "同日" in mode:
            return len(today) >= 2
        return len(today) >= 1 and len(today | yday) >= 2

    def hard_seats_on(sid: str, d: str, thr: float) -> set[str]:
        core = cores[sid]
        return {
            t
            for t, a in core_by[sid].get(d, {}).items()
            if t in core and a <= -thr
        }

    def window_fire(sid: str, d: str, lookback: int, floor_kind: str) -> bool:
        """lookback=1 → W0 (T only); 2→W1; 3→W3. Always need ≥1 seat on T."""
        core = cores[sid]
        if not core:
            return False
        soft, hard = soft_hard(floors[sid])
        thr = hard if floor_kind == "hard" else soft
        pi = path_idx[sid].get(d)
        if pi is None:
            return False
        today = hard_seats_on(sid, d, thr)
        if not today:
            return False
        present = set(today)
        for k in range(1, lookback):
            if pi - k < 0:
                break
            present |= hard_seats_on(sid, path_cals[sid][pi - k], thr)
        return len(present) >= 2

    def bench_ret(ed: str, xd: str) -> float | None:
        j = next((k for k, dd in enumerate(ix_dates) if dd >= ed), None)
        xj = next((k for k, dd in enumerate(ix_dates) if dd >= xd), None)
        if j is None or xj is None:
            return None
        bo = ix[ix_dates[j]][0]
        bc = ix[ix_dates[xj]][1]
        if bo <= 0:
            return None
        return bc / bo - 1

    def pack_l1h(
        sid: str, sig: str, hold: int
    ) -> dict | None:
        """Signal day T → entry T+1 open → hold-th session close (H=hold)."""
        pi = path_idx[sid].get(sig)
        if pi is None or pi + 1 >= len(path_cals[sid]):
            return None
        entry_i = pi + 1
        if entry_i + hold - 1 >= len(path_cals[sid]):
            return None
        ed = path_cals[sid][entry_i]
        xd = path_cals[sid][entry_i + hold - 1]
        ep = bars[sid][ed][0]
        xp = bars[sid][xd][1]
        if ep <= 0 or xp <= 0:
            return None
        ret = xp / ep - 1 - COST
        bret = bench_ret(ed, xd)
        if bret is None:
            return None
        return {
            "sid": sid,
            "sig": sig,
            "entry": ed,
            "exit": xd,
            "hold": hold,
            "ret": ret,
            "radj": ret - BETA * bret,
            "oos": sig >= OOS,
        }

    # --- Precompute fire flags per (sid,d,window) ---
    fire: dict[str, dict[str, set[str]]] = {
        label: defaultdict(set) for label, _, _ in WINDOWS
    }
    for label, lb, kind in WINDOWS:
        for sid in UNIVERSE:
            for d in cals[sid]:
                if window_fire(sid, d, lb, kind):
                    fire[label][sid].add(d)
        n_raw = sum(len(v) for v in fire[label].values())
        print(f"  raw fires {label}: {n_raw}")

    # --- Standalone wind (deduped signals) ---
    wind: dict[str, dict] = {}
    for label, lb, kind in WINDOWS:
        last: dict[str, str] = {}
        trades_by_h: dict[int, list[dict]] = {h: [] for h in HOLD_HS}
        sig_days: list[tuple[str, str]] = []
        for sid in UNIVERSE:
            for d in cals[sid]:
                if d not in fire[label][sid]:
                    continue
                if not dedupe_ok(last, sid, d):
                    continue
                # need enough path for H7
                pi = path_idx[sid].get(d)
                if pi is None or pi + 1 + PRIMARY_H - 1 >= len(path_cals[sid]):
                    continue
                ok_all = True
                packs = {}
                for h in HOLD_HS:
                    p = pack_l1h(sid, d, h)
                    if p is None:
                        ok_all = False
                        break
                    packs[h] = p
                if not ok_all:
                    continue
                for h in HOLD_HS:
                    trades_by_h[h].append(packs[h])
                sig_days.append((sid, d))
                last[sid] = d

        # Baseline: non-signal stock-days with same L1H* path available
        base_by_h: dict[int, list[float]] = {h: [] for h in HOLD_HS}
        base_oos_by_h: dict[int, list[float]] = {h: [] for h in HOLD_HS}
        for sid in UNIVERSE:
            for d in cals[sid]:
                if d in fire[label][sid]:
                    continue
                pi = path_idx[sid].get(d)
                if pi is None or pi + 1 + PRIMARY_H - 1 >= len(path_cals[sid]):
                    continue
                for h in HOLD_HS:
                    p = pack_l1h(sid, d, h)
                    if p is None:
                        continue
                    base_by_h[h].append(p["radj"])
                    if d >= OOS:
                        base_oos_by_h[h].append(p["radj"])

        n = len(sig_days)
        fires_per_yr = n / years if years > 0 else None
        day_fire_pct = (100.0 * n / n_stock_days) if n_stock_days else None
        # fire%/yr ≈ expected % of stock-days/year that are (deduped) signals
        # = fires_per_yr / (n_stock_days/years) * 100
        stock_days_per_yr = n_stock_days / years if years > 0 else None
        fire_pct_yr = (
            100.0 * fires_per_yr / stock_days_per_yr
            if fires_per_yr is not None and stock_days_per_yr
            else None
        )

        h_stats = {}
        for h in HOLD_HS:
            ts = trades_by_h[h]
            s = blk([t["radj"] for t in ts])
            oos = blk([t["radj"] for t in ts if t["oos"]])
            base = blk(base_by_h[h])
            base_oos = blk(base_oos_by_h[h])
            delta = (
                s["med"] - base["med"]
                if s["med"] is not None and base["med"] is not None
                else None
            )
            delta_oos = (
                oos["med"] - base_oos["med"]
                if oos["med"] is not None and base_oos["med"] is not None
                else None
            )
            h_stats[h] = {
                "sig": s,
                "oos": oos,
                "base": base,
                "base_oos": base_oos,
                "delta_med": delta,
                "delta_oos_med": delta_oos,
            }

        wind[label] = {
            "lookback": lb,
            "floor_kind": kind,
            "n": n,
            "oos_n": h_stats[PRIMARY_H]["oos"]["n"],
            "fires_per_yr": fires_per_yr,
            "day_fire_pct": day_fire_pct,
            "fire_pct_yr": fire_pct_yr,
            "h": h_stats,
            "sig_sample": sig_days[:8],
        }
        h5 = h_stats[PRIMARY_H]
        print(
            f"  wind {label}: n={n} fire%/yr={fire_pct_yr:.3f}% "
            f"H5 med={pct(h5['sig']['med'])} Δ={pct(h5['delta_med'])} "
            f"OOS med={pct(h5['oos']['med'])} ΔOOS={pct(h5['delta_oos_med'])}"
            if fire_pct_yr is not None
            else f"  wind {label}: n={n}"
        )

    # --- Path-exit overlay after green ---
    green_sigs: list[tuple[str, int, str]] = []
    last_g: dict[str, str] = {}
    for sid in UNIVERSE:
        for d in cals[sid]:
            if not expert_green(sid, d):
                continue
            if not dedupe_ok(last_g, sid, d):
                continue
            pi = path_idx[sid].get(d)
            if pi is None or pi + 1 >= len(path_cals[sid]):
                continue
            if pi + 1 + 7 - 1 >= len(path_cals[sid]):
                continue
            green_sigs.append((sid, pi, d))
            last_g[sid] = d
    print(f"green signals (deduped): {len(green_sigs)}")

    def path_exit_overlay(sid: str, entry_i: int, win_label: str) -> dict | None:
        cal = path_cals[sid]
        if entry_i < 0 or entry_i + 6 >= len(cal):
            return None
        ed = cal[entry_i]
        ep = bars[sid][ed][0]
        last_i = entry_i + 6  # H7 sessions
        fired = False
        exit_reason = "time"
        xd = cal[last_i]
        xp = bars[sid][xd][1]
        for j in range(entry_i, last_i + 1):
            d = cal[j]
            if d in fire[win_label][sid]:
                if j + 1 < len(cal):
                    xd = cal[j + 1]
                    xp = bars[sid][xd][0]
                    exit_reason = win_label
                    fired = True
                    break
        if ep <= 0 or xp <= 0:
            return None
        # exit px mode: open if early, close if time
        exit_is_open = fired
        ret = xp / ep - 1 - COST
        # bench: open→close for time; open→open for early
        j = next((k for k, dd in enumerate(ix_dates) if dd >= ed), None)
        xj = next((k for k, dd in enumerate(ix_dates) if dd >= xd), None)
        if j is None or xj is None:
            return None
        bo = ix[ix_dates[j]][0]
        bc = ix[ix_dates[xj]][0 if exit_is_open else 1]
        if bo <= 0:
            return None
        bret = bc / bo - 1
        # paired H7 time for FA
        h7d = cal[last_i]
        h7c = bars[sid][h7d][1]
        h7_ret = h7c / ep - 1 - COST
        h7_b = bench_ret(ed, h7d)
        if h7_b is None:
            return None
        return {
            "sid": sid,
            "entry": ed,
            "exit": xd,
            "exit_reason": exit_reason,
            "fired": fired,
            "ret": ret,
            "radj": ret - BETA * bret,
            "h7_radj": h7_ret - BETA * h7_b,
            "oos": False,  # filled by caller via green sig date
        }

    overlay: dict[str, dict] = {}
    # L1H7 baseline on green
    l1h7_trades: list[dict] = []
    for sid, pi, sig in green_sigs:
        p = pack_l1h(sid, sig, 7)
        if p is None:
            continue
        # pack_l1h uses sig as green day; entry is T+1 — correct
        l1h7_trades.append(p)
    l1h7 = {
        "all": blk([t["radj"] for t in l1h7_trades]),
        "oos": blk([t["radj"] for t in l1h7_trades if t["oos"]]),
    }

    for label, _, _ in WINDOWS:
        trades: list[dict] = []
        for sid, pi, sig in green_sigs:
            r = path_exit_overlay(sid, pi + 1, label)
            if r is None:
                continue
            r["sig"] = sig
            r["oos"] = sig >= OOS
            trades.append(r)
        fired = [t for t in trades if t["fired"]]
        fa = [t for t in fired if (t.get("h7_radj") or 0) > 0]
        allb = blk([t["radj"] for t in trades])
        oos = blk([t["radj"] for t in trades if t["oos"]])
        delta_all = (
            allb["med"] - l1h7["all"]["med"]
            if allb["med"] is not None and l1h7["all"]["med"] is not None
            else None
        )
        delta_oos = (
            oos["med"] - l1h7["oos"]["med"]
            if oos["med"] is not None and l1h7["oos"]["med"] is not None
            else None
        )
        overlay[label] = {
            "n": allb["n"],
            "med": allb["med"],
            "oos_n": oos["n"],
            "oos_med": oos["med"],
            "delta_vs_l1h7": delta_all,
            "delta_oos_vs_l1h7": delta_oos,
            "trigger_n": len(fired),
            "trigger_frac": (len(fired) / allb["n"]) if allb["n"] else None,
            "fa_n": len(fa),
            "fa_rate": (len(fa) / len(fired)) if fired else None,
            "oos_trigger_n": sum(1 for t in fired if t["oos"]),
            "oos_fa_rate": (
                (
                    sum(1 for t in fa if t["oos"])
                    / sum(1 for t in fired if t["oos"])
                )
                if any(t["oos"] for t in fired)
                else None
            ),
        }
        o = overlay[label]
        print(
            f"  overlay {label}: med={pct(o['med'])} "
            f"ΔOOS={pct(o['delta_oos_vs_l1h7'])} "
            f"trig={frac_str(o['trigger_frac'])} FA={frac_str(o['fa_rate'])}"
        )

    # --- Rank hard windows by wind separation (H5 Δ, prefer more negative) ---
    hard_labels = [l for l, _, k in WINDOWS if k == "hard"]
    ranked = sorted(
        hard_labels,
        key=lambda lab: (
            wind[lab]["h"][PRIMARY_H]["delta_med"] is not None,
            -(wind[lab]["h"][PRIMARY_H]["delta_med"] or 0),  # more neg better
            wind[lab]["n"],
        ),
        reverse=True,
    )

    # Recommendation heuristic for observation annotation:
    # maximize |ΔH5| vs baseline among hard, with fire%/yr not insane (>~5% stock-days)
    # and OOS Δ also ≤0 (same direction). Prefer tighter window if Δ similar.
    def score_obs(lab: str) -> tuple:
        w = wind[lab]
        h5 = w["h"][PRIMARY_H]
        d = h5["delta_med"]
        do = h5["delta_oos_med"]
        fire = w["fire_pct_yr"] or 99
        # reject insane fire
        insane = fire > 5.0
        # want negative Δ
        good_dir = d is not None and d < 0
        good_oos = do is not None and do <= 0.002  # allow tiny noise
        return (
            not insane,
            good_dir,
            good_oos,
            -(d if d is not None else 0),
            -fire,  # prefer lower fire when Δ similar
            -(w["lookback"]),  # prefer shorter lookback
        )

    best = max(hard_labels, key=score_obs)
    # Explicit freeze pick with commentary keys
    freeze = best
    w0, w1, w3 = wind["W0_hard"], wind["W1_hard"], wind["W3_hard"]
    d0 = w0["h"][5]["delta_med"]
    d1 = w1["h"][5]["delta_med"]
    d3 = w3["h"][5]["delta_med"]
    # Is W3 justified? only if Δ clearly better than W0 by ≥0.3pp and fire not wild
    w3_edge = (
        (d3 - d0)
        if d3 is not None and d0 is not None
        else None
    )  # more neg d3 → negative edge number means W3 better
    w3_justified = (
        w3_edge is not None
        and w3_edge <= -0.003
        and (w3["fire_pct_yr"] or 99) <= 5.0
        and (w3["h"][5]["delta_oos_med"] or 1) <= 0.002
    )

    # --- Write MD ---
    lines: list[str] = []
    lines.append("# H_EXPERT_SELL_LOOKBACK_WINDOW · same-day vs 1d/3d consensus sell")
    lines.append("")
    lines.append(
        "> Research only · **observation / email annotation** · "
        "**not** an auto-exit / Order-layer claim."
    )
    lines.append("")
    lines.append("## Verdict")
    lines.append("")
    lines.append(
        f"**Freeze for email annotation: `{freeze}`** "
        f"(hard floor · lookback={wind[freeze]['lookback']} session"
        f"{'s' if wind[freeze]['lookback'] > 1 else ''})."
    )
    lines.append("")
    h5f = wind[freeze]["h"][5]
    lines.append(
        f"- Wind H5 med {pct(h5f['sig']['med'])} · "
        f"Δ vs non-signal baseline {pct(h5f['delta_med'])} · "
        f"OOS Δ {pct(h5f['delta_oos_med'])} · "
        f"n={wind[freeze]['n']} · fire%/yr={wind[freeze]['fire_pct_yr']:.3f}%"
    )
    if freeze == "W0_hard":
        lines.append(
            "- **W0 (same-day ≥2 hard)** is the cleanest observation tag: "
            "tightest contemporaneous consensus, lowest stale risk, and "
            "competitive / best separation vs baseline among hard windows."
        )
    elif freeze == "W1_hard":
        lines.append(
            "- **W1 (T∪T-1 present≥2 with ≥1 on T)** adds modest lookback without "
            "the 3-day spam surface; freeze only if Δ meaningfully beats W0."
        )
    else:
        lines.append(
            "- **W3** wins on this sample's Δ; still treat as annotation, not exit."
        )
    if w3_justified:
        lines.append(
            f"- 3-day lookback **is justified on wind Δ** "
            f"(W3−W0 H5 Δ edge = {pct(w3_edge)}): keep W3 as optional secondary tag."
        )
    else:
        lines.append(
            f"- **3-day is not justified as primary annotation**: "
            f"W3−W0 H5 Δ edge = {pct(w3_edge)} "
            f"(need ≤−0.30pp with sane fire + OOS Δ≤0). "
            f"W3 also raises fire%/yr "
            f"({w0['fire_pct_yr']:.3f}% → {w3['fire_pct_yr']:.3f}%) "
            "and mixes stale seats even with the T-hard gate."
        )
    lines.append(
        "- Soft-floor variants are **diagnostic only** (marked soft below); "
        "do not freeze soft for primary email tags."
    )
    lines.append("")
    lines.append("## Protocol")
    lines.append("")
    lines.append("| key | value |")
    lines.append("|-----|-------|")
    lines.append(f"| universe | thick-14 · {', '.join(UNIVERSE)} |")
    lines.append(f"| window | `{START}`..`{END}` · years≈{years:.2f} |")
    lines.append(f"| OOS | ≥`{OOS}` |")
    lines.append(f"| β / cost / dedupe | {BETA} / {COST*1e4:.0f}bps / {DEDUP}d |")
    lines.append("| tape | `stock_broker_branch_daily` finmind · core×floor from watch_spec/POOLS |")
    lines.append(
        "| W0 | ≥2 distinct core net_sell≥hard **on T** |"
    )
    lines.append(
        "| W1 | present=hard seats on T∪T-1; \\|present\\|≥2 **and** ≥1 on T |"
    )
    lines.append(
        "| W3 | present=hard seats on T..T-2; \\|present\\|≥2 **and** ≥1 on T |"
    )
    lines.append(
        "| soft | same topology · thr=max(hard×0.5, 2500萬) |"
    )
    lines.append(
        "| wind | signal T → L1 open → Hk close · "
        "baseline = all non-signal stock-days with same path |"
    )
    lines.append(
        "| fire%/yr | 100 × (deduped n / years) / (stock_days / years) "
        "= % of stock-days that are signals |"
    )
    lines.append(
        "| overlay | after champion green → T+1 open; "
        "first W* in hold → next open else H7 close |"
    )
    lines.append("")
    lines.append("## 1) Standalone wind — tradeoff table")
    lines.append("")
    lines.append(
        "| window | kind | n | fire%/yr | H5 med | Δ vs base | "
        "OOS n | OOS H5 | ΔOOS | H3 Δ | H7 Δ |"
    )
    lines.append(
        "|--------|------|--:|--------:|-------:|----------:|"
        "-----:|-------:|-----:|-----:|-----:|"
    )
    for label, _, kind in WINDOWS:
        w = wind[label]
        h5 = w["h"][5]
        h3 = w["h"][3]
        h7 = w["h"][7]
        lines.append(
            f"| {label} | {kind} | {w['n']} | "
            f"{w['fire_pct_yr']:.3f}% | {pct(h5['sig']['med'])} | "
            f"{pct(h5['delta_med'])} | {w['oos_n']} | "
            f"{pct(h5['oos']['med'])} | {pct(h5['delta_oos_med'])} | "
            f"{pct(h3['delta_med'])} | {pct(h7['delta_med'])} |"
        )
    lines.append("")
    lines.append(
        f"Baseline (non-signal) H5 med ≈ "
        f"{pct(wind['W0_hard']['h'][5]['base']['med'])} "
        f"(OOS {pct(wind['W0_hard']['h'][5]['base_oos']['med'])}); "
        f"stock-days={n_stock_days:,} · fires/yr shown via fire%/yr."
    )
    lines.append("")
    lines.append("### Hard-window detail (H3 / H5 / H7)")
    lines.append("")
    lines.append(
        "| window | H3 med | H3 Δ | H5 med | H5 Δ | H7 med | H7 Δ | fires/yr |"
    )
    lines.append(
        "|--------|-------:|-----:|-------:|-----:|-------:|-----:|--------:|"
    )
    for label in hard_labels:
        w = wind[label]
        row = f"| {label} |"
        for h in HOLD_HS:
            hs = w["h"][h]
            row += f" {pct(hs['sig']['med'])} | {pct(hs['delta_med'])} |"
        row += f" {w['fires_per_yr']:.1f} |"
        lines.append(row)
    lines.append("")
    lines.append("## 2) Path-exit overlay after green (diagnostic)")
    lines.append("")
    lines.append(
        f"Green n={len(green_sigs)} · L1H7 med {pct(l1h7['all']['med'])} · "
        f"OOS med {pct(l1h7['oos']['med'])}."
    )
    lines.append("")
    lines.append(
        "| window | med | Δ vs L1H7 | OOS med | ΔOOS | trigger% | FA% | OOS FA% |"
    )
    lines.append(
        "|--------|----:|----------:|--------:|-----:|---------:|----:|--------:|"
    )
    for label, _, kind in WINDOWS:
        o = overlay[label]
        lines.append(
            f"| {label} | {pct(o['med'])} | {pct(o['delta_vs_l1h7'])} | "
            f"{pct(o['oos_med'])} | {pct(o['delta_oos_vs_l1h7'])} | "
            f"{frac_str(o['trigger_frac'])} | {frac_str(o['fa_rate'])} | "
            f"{frac_str(o['oos_fa_rate'])} |"
        )
    lines.append("")
    lines.append(
        "Expect ΔOOS ≤ 0 vs L1H7 for sell-as-exit (often fails). "
        "High trigger% + high FA% ⇒ noisy exit, not observation quality."
    )
    lines.append("")
    lines.append("## Recommendation (freeze)")
    lines.append("")
    lines.append(f"1. **Primary email annotation: `{freeze}`**")
    lines.append(
        "2. Optional secondary: "
        + (
            "`W3_hard` as wider scan tag (Δ edge qualifies)."
            if w3_justified
            else "skip W3 as primary; at most footnote «3d present» without promoting."
        )
    )
    lines.append(
        "3. Soft windows: do **not** freeze for the main tag "
        "(higher fire, weaker / noisier separation)."
    )
    lines.append(
        "4. **No auto-exit**: overlay Δ vs L1H7 is reported only; "
        "does not graduate to Order / launchd sell."
    )
    lines.append("")
    lines.append("## Ranked hard windows (by H5 Δ separation)")
    lines.append("")
    for i, lab in enumerate(ranked, 1):
        w = wind[lab]
        lines.append(
            f"{i}. `{lab}` · H5 Δ {pct(w['h'][5]['delta_med'])} · "
            f"fire%/yr {w['fire_pct_yr']:.3f}% · n={w['n']}"
        )
    lines.append("")
    lines.append(f"_Generated {datetime.now().isoformat(timespec='seconds')}_")

    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")

    payload = {
        "protocol": {
            "universe": UNIVERSE,
            "start": START,
            "end": END,
            "oos": OOS,
            "beta": BETA,
            "cost": COST,
            "dedupe_days": DEDUP,
            "years": years,
            "n_stock_days": n_stock_days,
            "claim": "observation_annotation_only",
        },
        "cores": {s: list(cores[s]) for s in UNIVERSE},
        "floors": floors,
        "modes": modes,
        "names": names,
        "wind": {
            k: {
                **{kk: vv for kk, vv in v.items() if kk != "h"},
                "h": {
                    str(h): {
                        "sig": hs["sig"],
                        "oos": hs["oos"],
                        "base": hs["base"],
                        "base_oos": hs["base_oos"],
                        "delta_med": hs["delta_med"],
                        "delta_oos_med": hs["delta_oos_med"],
                    }
                    for h, hs in v["h"].items()
                },
            }
            for k, v in wind.items()
        },
        "overlay": overlay,
        "l1h7_green": l1h7,
        "green_n": len(green_sigs),
        "ranked_hard": ranked,
        "freeze": freeze,
        "w3_justified": w3_justified,
        "w3_minus_w0_h5_delta": w3_edge,
    }
    OUT_JSON.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"wrote {OUT_MD}")
    print(f"wrote {OUT_JSON}")
    print(f"FREEZE={freeze} w3_justified={w3_justified}")


def frac_str(x: float | None) -> str:
    if x is None:
        return "—"
    return f"{x * 100:.0f}%"


if __name__ == "__main__":
    main()
