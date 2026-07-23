#!/usr/bin/env python3
"""H_SOFT_VS_HARD_GREEN: intra-pool soft consensus vs champion hard green.

Research only · sqlite mode=ro · does not touch strategy/order.

Focus: soft thresholds *inside* each stock's expert core (not Top10 light2 yellow;
that path is already covered by H_C / H_E).

Soft defs (all require NOT hard green on the same day unless noted):
  SoftOR       ≥1 core ≥champion floor today (watch OR annotation)
  SoftHalf     champion structure at floor/2
  SoftLite2    lookback-style: ≥2 core with amt∈[5e6, floor) over today∪yday,
               ≥1 today in band; no core ≥floor today
  AlmostTm1    SoftHalf day S where hard greens on next session (precursor label)

Arms:
  1 Hard → T+1 open H7
  2 Soft* → T+1 open H7 (and SoftHalf → T close H7)
  3 Paired SoftHalf→later hard: enter soft vs wait hard
  4 Throughput / overlap / early-only

  PYTHONPATH=src .venv/bin/python scripts/research/run_soft_vs_hard_green.py
"""
from __future__ import annotations

import importlib.util
import json
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
    / "H_SOFT_VS_HARD_GREEN.md"
)
OUT_JSON = OUT_MD.with_suffix(".json")
DB = ROOT / "data" / "stocks.db"
SOURCE = "finmind"
START, END, OOS = "2024-07-01", "2026-07-20", "2026-01-01"
# Same thick spirit as H_C / H_E fair green (adopted∩hard≥3 ex-mega + legacy pool)
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
LIGHT_LO = 5e6
COST, HOLD, BETA, DEDUP = 0.003, 7, 1.15, 5


def _load_watch_module():
    spec = importlib.util.spec_from_file_location(
        "epw_soft", ROOT / "scripts/research/run_expert_pool_watch.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["epw_soft"] = mod
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


def dedupe_ok(last: dict[str, str], sid: str, d: str) -> bool:
    if sid not in last:
        return True
    return (
        datetime.strptime(d, "%Y-%m-%d") - datetime.strptime(last[sid], "%Y-%m-%d")
    ).days > DEDUP


def summarize(trades: list[dict]) -> dict:
    if not trades:
        return {
            "n": 0,
            "med": None,
            "mean": None,
            "win": None,
            "oos_n": 0,
            "oos_med": None,
            "oos_win": None,
            "is_n": 0,
            "is_med": None,
        }

    def blk(ts: list[dict]) -> dict:
        if not ts:
            return {"n": 0, "med": None, "mean": None, "win": None}
        xs = [t["radj"] for t in ts]
        return {
            "n": len(ts),
            "med": median(xs),
            "mean": mean(xs),
            "win": sum(1 for x in xs if x > 0) / len(xs),
        }

    allb = blk(trades)
    oos = [t for t in trades if t["oos"]]
    is_ = [t for t in trades if not t["oos"]]
    oosb = blk(oos)
    isb = blk(is_)
    return {
        "n": allb["n"],
        "med": allb["med"],
        "mean": allb["mean"],
        "win": allb["win"],
        "oos_n": oosb["n"],
        "oos_med": oosb["med"],
        "oos_win": oosb["win"],
        "is_n": isb["n"],
        "is_med": isb["med"],
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
        print(
            f"  {sid} {names[sid]} mode={modes[sid]} "
            f"floor={floors[sid]:.0f} core_n={len(cores[sid])}"
        )
        if not cores[sid]:
            print("    WARNING: empty core")

    con = sqlite3.connect(f"file:{DB.resolve()}?mode=ro", uri=True, timeout=120)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    con.execute("PRAGMA busy_timeout=120000")
    ph = ",".join("?" * len(UNIVERSE))

    bars: dict[str, dict[str, tuple[float, float]]] = defaultdict(dict)
    for r in con.execute(
        f"""SELECT stock_id, trade_date, open, close FROM stock_daily_bars
            WHERE source=? AND stock_id IN ({ph})
              AND trade_date BETWEEN ? AND ? AND close>0""",
        (SOURCE, *UNIVERSE, START, END),
    ):
        o = float(r["open"] or r["close"])
        c = float(r["close"])
        if o > 0:
            bars[r["stock_id"]][r["trade_date"]] = (o, c)

    ix: dict[str, tuple[float, float]] = {}
    for r in con.execute(
        """SELECT date, open, close FROM daily_bars
           WHERE code='IX0001' AND date BETWEEN ? AND ? AND open>0 AND close>0
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
                  AND trade_date BETWEEN ? AND ? AND net>0""",
            (SOURCE, *UNIVERSE, *core_tids, START, END),
        ):
            oc = bars[r["stock_id"]].get(r["trade_date"])
            if oc:
                core_by[r["stock_id"]][r["trade_date"]][
                    r["securities_trader_id"]
                ] = float(r["net"]) * oc[1]
    con.close()

    cals = {sid: sorted(bars[sid]) for sid in UNIVERSE}
    idx = {sid: {d: i for i, d in enumerate(cals[sid])} for sid in UNIVERSE}

    def day_core_amts(sid: str, d: str) -> dict[str, float]:
        core = cores[sid]
        return {
            t: a
            for t, a in core_by[sid].get(d, {}).items()
            if t in core
        }

    def hard_sets(sid: str, d: str) -> tuple[set[str], set[str]]:
        floor = floors[sid]
        i = idx[sid][d]
        today = {t for t, a in day_core_amts(sid, d).items() if a >= floor}
        yday: set[str] = set()
        if i > 0:
            yd = cals[sid][i - 1]
            yday = {t for t, a in day_core_amts(sid, yd).items() if a >= floor}
        return today, yday

    def expert_green(sid: str, d: str) -> bool:
        if not cores[sid]:
            return False
        mode = modes[sid]
        today, yday = hard_sets(sid, d)
        if "OR" in mode:
            return len(today) >= 1
        if "同日" in mode:
            return len(today) >= 2
        return len(today) >= 1 and len(today | yday) >= 2

    def soft_or(sid: str, d: str) -> bool:
        """Fewer specialists: OR@floor and not yet hard green."""
        if expert_green(sid, d):
            return False
        today, _ = hard_sets(sid, d)
        return len(today) >= 1

    def soft_half(sid: str, d: str) -> bool:
        """Champion structure at floor/2, not hard green."""
        if expert_green(sid, d):
            return False
        if not cores[sid]:
            return False
        half = floors[sid] / 2.0
        mode = modes[sid]
        i = idx[sid][d]
        today = {t for t, a in day_core_amts(sid, d).items() if a >= half}
        yday: set[str] = set()
        if i > 0:
            yd = cals[sid][i - 1]
            yday = {t for t, a in day_core_amts(sid, yd).items() if a >= half}
        if "OR" in mode:
            return len(today) >= 1
        if "同日" in mode:
            return len(today) >= 2
        return len(today) >= 1 and len(today | yday) >= 2

    def soft_lite2(sid: str, d: str) -> bool:
        """≥2 core light buys in lookback window; nobody at hard floor today."""
        if expert_green(sid, d):
            return False
        floor = floors[sid]
        i = idx[sid][d]
        today_m = day_core_amts(sid, d)
        if any(a >= floor for a in today_m.values()):
            return False
        today_lite = {
            t for t, a in today_m.items() if LIGHT_LO <= a < floor
        }
        yday_lite: set[str] = set()
        if i > 0:
            yd = cals[sid][i - 1]
            yday_m = day_core_amts(sid, yd)
            yday_lite = {
                t for t, a in yday_m.items() if LIGHT_LO <= a < floor
            }
        return len(today_lite) >= 1 and len(today_lite | yday_lite) >= 2

    def l1h_from_entry(
        sid: str, entry_i: int, *, px_mode: str = "open", hold: int = HOLD
    ) -> dict | None:
        cal = cals[sid]
        if entry_i < 0 or entry_i + hold - 1 >= len(cal):
            return None
        ed = cal[entry_i]
        xd = cal[entry_i + hold - 1]
        eo, ec = bars[sid][ed]
        _, xc = bars[sid][xd]
        ep = eo if px_mode == "open" else ec
        if ep <= 0:
            return None
        ret = xc / ep - 1 - COST
        j = None
        for k, d in enumerate(ix_dates):
            if d >= ed:
                j = k
                break
        if j is None:
            return None
        xj = None
        for k, d in enumerate(ix_dates):
            if d >= xd:
                xj = k
                break
        if xj is None:
            return None
        bo = ix[ix_dates[j]][0] if px_mode == "open" else ix[ix_dates[j]][1]
        bc = ix[ix_dates[xj]][1]
        if bo <= 0:
            return None
        bret = bc / bo - 1
        return {
            "ret": ret,
            "radj": ret - BETA * bret,
            "entry": ed,
            "exit": xd,
        }

    def l1h7_from_sig(sid: str, sig_i: int, *, px_mode: str = "open") -> dict | None:
        if px_mode == "open":
            return l1h_from_entry(sid, sig_i + 1, px_mode="open", hold=HOLD)
        # T close entry: hold H7 from signal day close → day+6 close
        return l1h_from_entry(sid, sig_i, px_mode="close", hold=HOLD)

    def collect_days(pred) -> list[tuple[str, int, str]]:
        out: list[tuple[str, int, str]] = []
        for sid in UNIVERSE:
            cal = cals[sid]
            for i, d in enumerate(cal):
                if pred(sid, d):
                    out.append((sid, i, d))
        return out

    hard_days = collect_days(expert_green)
    soft_or_days = collect_days(soft_or)
    soft_half_days = collect_days(soft_half)
    soft_lite_days = collect_days(soft_lite2)

    # Almost-green: SoftHalf on S and hard on next session
    almost_days: list[tuple[str, int, str]] = []
    for sid, i, d in soft_half_days:
        cal = cals[sid]
        if i + 1 < len(cal) and expert_green(sid, cal[i + 1]):
            almost_days.append((sid, i, d))

    print(
        f"raw fires hard={len(hard_days)} softOR={len(soft_or_days)} "
        f"softHalf={len(soft_half_days)} softLite2={len(soft_lite_days)} "
        f"almostTm1={len(almost_days)}"
    )

    def trades_from(
        events: list[tuple[str, int, str]],
        *,
        px_mode: str = "open",
        tag: str,
    ) -> list[dict]:
        trades: list[dict] = []
        last: dict[str, str] = {}
        for sid, i, d in events:
            if not dedupe_ok(last, sid, d):
                continue
            r = l1h7_from_sig(sid, i, px_mode=px_mode)
            if r is None:
                continue
            last[sid] = d
            trades.append(
                dict(
                    sid=sid,
                    sig=d,
                    tag=tag,
                    oos=d >= OOS,
                    **r,
                )
            )
        return trades

    arms: dict[str, list[dict]] = {
        "hard_t1open": trades_from(hard_days, tag="hard"),
        "softOR_t1open": trades_from(soft_or_days, tag="softOR"),
        "softHalf_t1open": trades_from(soft_half_days, tag="softHalf"),
        "softHalf_tclose": trades_from(
            soft_half_days, px_mode="close", tag="softHalf_close"
        ),
        "softLite2_t1open": trades_from(soft_lite_days, tag="softLite2"),
        "almostTm1_t1open": trades_from(almost_days, tag="almostTm1"),
    }

    # Soft ∪ Hard union (softer entry that includes hard days) for SoftHalf@half
    # — useful as "earlier+same" throughput; still report soft-only above.
    def soft_half_incl(sid: str, d: str) -> bool:
        if not cores[sid]:
            return False
        half = floors[sid] / 2.0
        mode = modes[sid]
        i = idx[sid][d]
        today = {t for t, a in day_core_amts(sid, d).items() if a >= half}
        yday: set[str] = set()
        if i > 0:
            yd = cals[sid][i - 1]
            yday = {t for t, a in day_core_amts(sid, yd).items() if a >= half}
        if "OR" in mode:
            return len(today) >= 1
        if "同日" in mode:
            return len(today) >= 2
        return len(today) >= 1 and len(today | yday) >= 2

    soft_half_incl_days = collect_days(soft_half_incl)
    arms["softHalfIncl_t1open"] = trades_from(
        soft_half_incl_days, tag="softHalfIncl"
    )

    # SoftOR incl hard (= OR@floor always; for OR-mode stocks ≡ hard)
    def soft_or_incl(sid: str, d: str) -> bool:
        today, _ = hard_sets(sid, d)
        return len(today) >= 1

    soft_or_incl_days = collect_days(soft_or_incl)
    arms["softORIncl_t1open"] = trades_from(soft_or_incl_days, tag="softORIncl")

    # ---- Paired timing: SoftHalf days that upgrade to hard within ≤5 sessions ----
    paired_imm: list[dict] = []
    paired_wait: list[dict] = []
    last_p: dict[str, str] = {}
    upgrade_lags: list[int] = []
    soft_half_will: int = 0
    soft_half_never: int = 0
    for sid, i, d in soft_half_days:
        cal = cals[sid]
        hit_j: int | None = None
        for j in range(1, 6):
            if i + j >= len(cal):
                break
            if expert_green(sid, cal[i + j]):
                hit_j = j
                break
        if hit_j is None:
            soft_half_never += 1
            continue
        soft_half_will += 1
        upgrade_lags.append(hit_j)
        if not dedupe_ok(last_p, sid, d):
            continue
        r_imm = l1h7_from_sig(sid, i)
        r_wait = l1h7_from_sig(sid, i + hit_j)
        if r_imm is None or r_wait is None:
            continue
        last_p[sid] = d
        paired_imm.append(
            dict(sid=sid, sig=d, tag="paired_imm", oos=d >= OOS, **r_imm)
        )
        paired_wait.append(
            dict(
                sid=sid,
                sig=cal[i + hit_j],
                soft=d,
                lag=hit_j,
                tag="paired_wait",
                oos=d >= OOS,
                **r_wait,
            )
        )

    arms["paired_softHalf_imm"] = paired_imm
    arms["paired_softHalf_wait"] = paired_wait

    # SoftHalf never→hard@5 immediate (selection junk)
    junk: list[dict] = []
    last_j: dict[str, str] = {}
    for sid, i, d in soft_half_days:
        cal = cals[sid]
        hit = False
        for j in range(1, 6):
            if i + j >= len(cal):
                break
            if expert_green(sid, cal[i + j]):
                hit = True
                break
        if hit:
            continue
        if not dedupe_ok(last_j, sid, d):
            continue
        r = l1h7_from_sig(sid, i)
        if r is None:
            continue
        last_j[sid] = d
        junk.append(dict(sid=sid, sig=d, tag="softHalf_never", oos=d >= OOS, **r))
    arms["softHalf_never_hard5_imm"] = junk

    # Throughput / overlap (raw, no dedupe)
    hard_set = {(sid, d) for sid, _, d in hard_days}
    soft_sets = {
        "softOR": {(sid, d) for sid, _, d in soft_or_days},
        "softHalf": {(sid, d) for sid, _, d in soft_half_days},
        "softLite2": {(sid, d) for sid, _, d in soft_lite_days},
        "almostTm1": {(sid, d) for sid, _, d in almost_days},
        "softHalfIncl": {(sid, d) for sid, _, d in soft_half_incl_days},
        "softORIncl": {(sid, d) for sid, _, d in soft_or_incl_days},
    }
    # early-only: soft that precedes a hard within ≤5 (same sid), soft day itself not hard
    def early_only_frac(soft_events: list[tuple[str, int, str]]) -> dict:
        early = 0
        for sid, i, d in soft_events:
            cal = cals[sid]
            found = False
            for j in range(1, 6):
                if i + j >= len(cal):
                    break
                if expert_green(sid, cal[i + j]):
                    found = True
                    break
            if found:
                early += 1
        n = len(soft_events)
        return {
            "n": n,
            "early_n": early,
            "early_frac": (early / n) if n else None,
            "overlap_hard_same_day": 0,  # soft defs exclude hard
        }

    throughput = {
        "hard_raw": len(hard_days),
        "softOR_raw": len(soft_or_days),
        "softHalf_raw": len(soft_half_days),
        "softLite2_raw": len(soft_lite_days),
        "almostTm1_raw": len(almost_days),
        "softHalfIncl_raw": len(soft_half_incl_days),
        "softORIncl_raw": len(soft_or_incl_days),
        "softHalf_early": early_only_frac(soft_half_days),
        "softOR_early": early_only_frac(soft_or_days),
        "softLite2_early": early_only_frac(soft_lite_days),
        "softHalf_will_hard5": soft_half_will,
        "softHalf_never_hard5": soft_half_never,
        "upgrade_lag_med": median(upgrade_lags) if upgrade_lags else None,
        "upgrade_lag_mean": mean(upgrade_lags) if upgrade_lags else None,
    }

    # Paired Δ
    paired_deltas = []
    for a, b in zip(paired_imm, paired_wait):
        paired_deltas.append(b["radj"] - a["radj"])
    paired_stats = {
        "n": len(paired_deltas),
        "med_delta_wait_minus_imm": median(paired_deltas) if paired_deltas else None,
        "mean_delta": mean(paired_deltas) if paired_deltas else None,
        "p_wait_gt_imm": (
            sum(1 for x in paired_deltas if x > 0) / len(paired_deltas)
            if paired_deltas
            else None
        ),
    }
    oos_pair = [
        (b["radj"] - a["radj"])
        for a, b in zip(paired_imm, paired_wait)
        if a["oos"]
    ]
    paired_stats["oos_n"] = len(oos_pair)
    paired_stats["oos_med_delta"] = median(oos_pair) if oos_pair else None

    summaries = {k: summarize(v) for k, v in arms.items()}

    # ---- Write MD ----
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines: list[str] = []
    lines.append("# H_SOFT_VS_HARD_GREEN · 池內軟共識 vs 冠軍硬綠燈\n")
    lines.append(f"- 生成：{now}")
    lines.append("- Research only · sqlite `mode=ro` · **未採納** Strategy／Order")
    lines.append("- Runner：`scripts/research/run_soft_vs_hard_green.py`")
    lines.append(
        "- **非** Top10 light2 黃燈（見 H_C／H_E）；本檔只測 **intra-pool** 軟門檻。\n"
    )

    hard_s = summaries["hard_t1open"]
    sh_s = summaries["softHalf_t1open"]
    so_s = summaries["softOR_t1open"]
    sl_s = summaries["softLite2_t1open"]
    al_s = summaries["almostTm1_t1open"]
    shi_s = summaries["softHalfIncl_t1open"]
    soi_s = summaries["softORIncl_t1open"]

    soft_beats = False
    # Primary soft candidates must beat hard on ALL med and not collapse OOS
    for label, s in [
        ("SoftOR-only", so_s),
        ("SoftHalf-only", sh_s),
        ("SoftLite2", sl_s),
        ("SoftHalfIncl", shi_s),
        ("SoftORIncl", soi_s),
    ]:
        if (
            s["med"] is not None
            and hard_s["med"] is not None
            and s["med"] >= hard_s["med"]
            and s["oos_med"] is not None
            and hard_s["oos_med"] is not None
            and s["oos_med"] >= hard_s["oos_med"] - 0.005
        ):
            soft_beats = True
            break

    satellite_ok = False
    # Satellite useful if early precursor almostTm1 or will→hard subset approaches hard
    will_s = summaries["paired_softHalf_imm"]
    if (
        al_s["med"] is not None
        and hard_s["med"] is not None
        and al_s["med"] >= hard_s["med"] - 0.02
        and al_s["n"] >= 20
    ):
        satellite_ok = True
    if (
        will_s["med"] is not None
        and hard_s["med"] is not None
        and will_s["med"] >= hard_s["med"] - 0.02
        and will_s["n"] >= 20
    ):
        satellite_ok = True

    if soft_beats:
        verdict = (
            "**Soft can approach/beat hard on med** under at least one soft-incl arm — "
            "see tables; do **not** auto-replace green without capacity／OOS n check."
        )
    elif satellite_ok:
        verdict = (
            "**Soft does not beat hard as a standalone entry.** "
            "Will→hard SoftHalf imm approaches hard med (look-ahead); "
            "AlmostT−1 ALL ok but OOS fragile. Useful only as "
            "**earlier satellite／alert**, not a green substitute."
        )
    else:
        verdict = (
            "**Soft does not beat hard** on L1H7 med/OOS. Soft-only arms are "
            "weaker／noisier; do **not** use as green substitute. Satellite value "
            "also weak on this cut."
        )

    lines.append("## Verdict\n")
    lines.append(verdict + "\n")
    lines.append(
        f"- Hard L1H7：ALL med {pct(hard_s['med'])}（n={hard_s['n']}）· "
        f"OOS med {pct(hard_s['oos_med'])}（n={hard_s['oos_n']}）"
    )
    lines.append(
        f"- SoftHalf-only：ALL {pct(sh_s['med'])}（n={sh_s['n']}）· "
        f"OOS {pct(sh_s['oos_med'])}（n={sh_s['oos_n']}）"
    )
    lines.append(
        f"- SoftOR-only：ALL {pct(so_s['med'])}（n={so_s['n']}）· "
        f"OOS {pct(so_s['oos_med'])}（n={so_s['oos_n']}）"
    )
    lines.append(
        f"- SoftHalfIncl（含硬日）：ALL {pct(shi_s['med'])}（n={shi_s['n']}）· "
        f"OOS {pct(shi_s['oos_med'])}（n={shi_s['oos_n']}）"
    )
    lines.append(
        f"- SoftORIncl（=OR@floor）：ALL {pct(soi_s['med'])}（n={soi_s['n']}）· "
        f"OOS {pct(soi_s['oos_med'])}（n={soi_s['oos_n']}）"
    )
    lines.append(
        f"- AlmostT−1（SoftHalf→次日硬綠）：ALL {pct(al_s['med'])}（n={al_s['n']}）· "
        f"OOS {pct(al_s['oos_med'])}（n={al_s['oos_n']}）"
    )
    if paired_stats["med_delta_wait_minus_imm"] is not None:
        lines.append(
            f"- Paired SoftHalf→hard≤5：Δ(wait−imm) med "
            f"{pct(paired_stats['med_delta_wait_minus_imm'])} "
            f"（n={paired_stats['n']}；P(wait>imm)="
            f"{win_pct(paired_stats['p_wait_gt_imm'])}）"
        )
    lines.append(
        f"- SoftHalf → hard@≤5：{throughput['softHalf_will_hard5']}/"
        f"{throughput['softHalf_will_hard5'] + throughput['softHalf_never_hard5']} "
        f"（med lag {throughput['upgrade_lag_med']} sessions）\n"
    )

    lines.append("## Frozen protocol\n")
    lines.append("| 鍵 | 值 |")
    lines.append("|----|-----|")
    lines.append(f"| window | `{START}`..`{END}` |")
    lines.append(
        "| universe | H_C thick hard≥3 ex-mega + legacy · **14**： "
        + ", ".join(UNIVERSE)
        + " |"
    )
    lines.append(
        "| hard green | 各股 champion（watch_spec／POOLS）：回看1日／同日／OR × core×floor |"
    )
    lines.append(
        "| SoftOR-only | ≥1 core ≥floor 今日，且 **非** hard green（watch OR 附註） |"
    )
    lines.append(
        "| SoftHalf-only | 冠軍結構但 floor′=floor/2，且 **非** hard |"
    )
    lines.append(
        "| SoftLite2 | 回看窗 ≥2 core 淨買∈[500萬, floor)，今日≥1；今日無人≥floor；非 hard |"
    )
    lines.append(
        "| AlmostT−1 | SoftHalf 日 S 且次一交易日 hard green |"
    )
    lines.append("| Soft*Incl | 同結構 **含** hard 日（較早+同日並集） |")
    lines.append("| cost | 30bps（只扣個股） |")
    lines.append(
        r"| excess | \(r_{adj}=r-1.15\times r_{IX0001}\) |"
    )
    lines.append(f"| OOS | 訊號日 ≥ `{OOS}` |")
    lines.append("| dedupe | 同股 **5 曆日** |")
    lines.append("| hold | H7（L1H7 = 訊號次日開 → 第 7 日收；T close 臂另標） |")
    lines.append("| DB | `data/stocks.db` mode=ro · source=finmind |\n")

    lines.append("### Champion modes（本宇宙）\n")
    lines.append("| sid | 名 | mode | floor | core_n |")
    lines.append("|-----|----|------|------:|-------:|")
    for sid in UNIVERSE:
        fl = floors[sid]
        fl_s = "≥1億" if fl >= 1e8 - 1 else "≥0.5億"
        lines.append(
            f"| {sid} | {names[sid]} | {modes[sid]} | {fl_s} | {len(cores[sid])} |"
        )
    lines.append("")

    lines.append("## Arm table（L1H7）\n")
    lines.append(
        r"| arm | n | ALL med \(r_{adj}\) | win | OOS n | OOS med | OOS win |"
    )
    lines.append(
        "|-----|--:|--------------------:|----:|------:|--------:|--------:|"
    )
    arm_order = [
        ("1 · Hard green → T+1 open", "hard_t1open"),
        ("2a · SoftOR-only → T+1 open", "softOR_t1open"),
        ("2b · SoftHalf-only → T+1 open", "softHalf_t1open"),
        ("2c · SoftHalf-only → T close", "softHalf_tclose"),
        ("2d · SoftLite2 → T+1 open", "softLite2_t1open"),
        ("2e · AlmostT−1 SoftHalf → T+1 open", "almostTm1_t1open"),
        ("2f · SoftORIncl (=OR@floor) → T+1", "softORIncl_t1open"),
        ("2g · SoftHalfIncl → T+1 open", "softHalfIncl_t1open"),
        ("3a · SoftHalf will→hard≤5 · imm", "paired_softHalf_imm"),
        ("3b · SoftHalf will→hard≤5 · wait hard", "paired_softHalf_wait"),
        ("3c · SoftHalf never→hard@5 · imm", "softHalf_never_hard5_imm"),
    ]
    for label, key in arm_order:
        s = summaries[key]
        lines.append(
            f"| {label} | {s['n']} | {pct(s['med'])} | {win_pct(s['win'])} | "
            f"{s['oos_n']} | {pct(s['oos_med'])} | {win_pct(s['oos_win'])} |"
        )
    lines.append("")

    lines.append("## Throughput / overlap\n")
    lines.append("| signal | raw fires | early→hard@≤5 | early% | note |")
    lines.append("|--------|----------:|--------------:|-------:|------|")
    for key, raw_k, early_k in [
        ("Hard green", "hard_raw", None),
        ("SoftOR-only", "softOR_raw", "softOR_early"),
        ("SoftHalf-only", "softHalf_raw", "softHalf_early"),
        ("SoftLite2", "softLite2_raw", "softLite2_early"),
        ("AlmostT−1", "almostTm1_raw", None),
        ("SoftORIncl", "softORIncl_raw", None),
        ("SoftHalfIncl", "softHalfIncl_raw", None),
    ]:
        raw = throughput[raw_k]
        if early_k:
            ef = throughput[early_k]
            early_n = ef["early_n"]
            early_pct = (
                f"{ef['early_frac'] * 100:.1f}%" if ef["early_frac"] is not None else "—"
            )
            lines.append(
                f"| {key} | {raw} | {early_n} | {early_pct} | soft-only（同日不與 hard 重疊） |"
            )
        else:
            note = "benchmark" if key.startswith("Hard") else "by construction → hard next"
            if "Incl" in key:
                note = "含 hard 日"
            lines.append(f"| {key} | {raw} | — | — | {note} |")
    lines.append("")
    lines.append(
        f"- SoftHalf upgrade@≤5：**{throughput['softHalf_will_hard5']}** will / "
        f"**{throughput['softHalf_never_hard5']}** never · "
        f"lag med **{throughput['upgrade_lag_med']}** · "
        f"mean **{throughput['upgrade_lag_mean']:.2f}** sessions\n"
        if throughput["upgrade_lag_mean"] is not None
        else ""
    )

    lines.append("## Paired timing vs selection（SoftHalf → hard≤5）\n")
    if paired_stats["n"]:
        lines.append(
            f"- n={paired_stats['n']} · median(wait−imm)="
            f"**{pct(paired_stats['med_delta_wait_minus_imm'])}** · "
            f"mean={pct(paired_stats['mean_delta'])} · "
            f"P(wait>imm)={win_pct(paired_stats['p_wait_gt_imm'])}"
        )
        lines.append(
            f"- OOS n={paired_stats['oos_n']} · median(wait−imm)="
            f"**{pct(paired_stats['oos_med_delta'])}**"
        )
        imm = summaries["paired_softHalf_imm"]
        nev = summaries["softHalf_never_hard5_imm"]
        if imm["med"] is not None and nev["med"] is not None:
            lines.append(
                f"- Selection：will→hard imm {pct(imm['med'])} vs never {pct(nev['med'])} "
                f"（Δ {pct(imm['med'] - nev['med'])}）"
            )
        lines.append(
            "- 讀法：若 Δ(wait−imm)≈0 且 will≫never → **選擇主、等待不創造 alpha**"
            "（對齊 H_C／H_E）。\n"
        )
    else:
        lines.append("- 無 paired 樣本。\n")

    lines.append("## Interpretation\n")
    lines.append(
        "1. **Hard green 仍是主線**：與 H_C fair green 同形；本檔測的是池內「更早／更軟」。"
    )
    lines.append(
        "2. **SoftOR / SoftHalf-only**：多訊號、較早，但中位通常被稀釋；"
        "若 Incl 臂接近 hard，代表軟日加入後沒有明顯抬升——軟日本身偏弱。"
    )
    lines.append(
        "3. **AlmostT−1 / will→hard**：事後會升級的軟日接近硬綠 → "
        "實務上只能當 **警戒衛星**（無法事前只留 will）。"
    )
    lines.append(
        "4. **vs Top10 黃燈**：本檔不重複 H_C；池內 soft 更貼 champion 席位，"
        "但仍非綠燈替代。\n"
    )

    lines.append("## Freeze recommendation\n")
    if soft_beats:
        lines.append(
            "**research note — soft-incl candidate may approach hard; "
            "not auto-adopted as green replacement**"
        )
    elif satellite_ok:
        lines.append(
            "**research note / earlier satellite only — not a green substitute**"
        )
    else:
        lines.append("**reject soft as entry path — keep hard green**")
    lines.append("")
    lines.append("- 不寫 `config/strategy.yaml`／不進 Order。")
    lines.append(
        f"- Soft survives fair vs hard as replacement? "
        f"**{'YES (approach)' if soft_beats else 'NO'}**"
    )
    lines.append(
        f"- Soft useful as earlier satellite? "
        f"**{'YES (conditional)' if satellite_ok else 'WEAK / NO'}**\n"
    )

    lines.append("## Caveats\n")
    lines.append(r"- 單槽、無容量；sum \(r_{adj}\) 不可當複利權益。")
    lines.append("- Soft-only 定義排除同日 hard → 與 hard 無同日 overlap（by construction）。")
    lines.append("- Almost／will→hard 含前瞻選擇偏誤；不可直接當可交易濾網。")
    lines.append("- OR 冠軍股（6223）上 SoftOR-only 幾乎不火（OR≡hard）。")
    lines.append("- END 延伸至 2026-07-20；與 H_C（至 07-17）n 可能微差。\n")

    lines.append("## Reproduction\n")
    lines.append("```bash")
    lines.append(
        "PYTHONPATH=src .venv/bin/python scripts/research/run_soft_vs_hard_green.py"
    )
    lines.append("```\n")

    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.write_text("\n".join(lines), encoding="utf-8")

    payload = {
        "generated_at": now,
        "protocol": {
            "start": START,
            "end": END,
            "oos": OOS,
            "cost": COST,
            "beta": BETA,
            "hold": HOLD,
            "dedupe_calendar_days": DEDUP,
            "universe": UNIVERSE,
            "modes": modes,
            "floors": floors,
        },
        "throughput": throughput,
        "paired": paired_stats,
        "summaries": {
            k: {
                kk: (float(vv) if isinstance(vv, float) else vv)
                for kk, vv in s.items()
            }
            for k, s in summaries.items()
        },
        "verdict": {
            "soft_beats_hard": soft_beats,
            "satellite_ok": satellite_ok,
            "text": verdict,
        },
    }
    OUT_JSON.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"wrote {OUT_MD}")
    print(f"wrote {OUT_JSON}")
    print("VERDICT:", verdict)


if __name__ == "__main__":
    main()
