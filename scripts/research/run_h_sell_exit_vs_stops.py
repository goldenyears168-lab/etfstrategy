#!/usr/bin/env python3
"""H_SELL_EXIT_VS_STOPS: expert CONSENSUS HARD sell-exit vs SL/peak/H10.

Research only · same thick green universe / L1 / cost / β / OOS as
H_GREEN_HOLD_EXIT. Sell definition aligns with holdings branch sell monitor
HARD floor (= pool net_floor), but CONSENSUS HARD here means ≥2 *core HARD*
sells (stricter than live subject tag which uses ≥2 soft+).

Arms
  1) L1H7
  2) L1H10
  3) SL−8%@H7
  4) peak DD−5%@H7
  5) First CONSENSUS HARD → next open (cap H7 close if never)
  6) First CONSENSUS HARD → next open (else H10 close)
  7) CONSENSUS HARD OR peak DD−5% (cap H7)

  PYTHONPATH=src .venv/bin/python scripts/research/run_h_sell_exit_vs_stops.py
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
    / "H_SELL_EXIT_VS_STOPS.md"
)
OUT_JSON = OUT_MD.with_suffix(".json")
DB = ROOT / "data" / "stocks.db"
SOURCE = "finmind"
START, END, OOS = "2024-07-01", "2026-07-17", "2026-01-01"
COST, BETA, DEDUP = 0.003, 1.15, 5
BASE_H, LONG_H = 7, 10
SL_PCT = 0.08
PEAK_DD = 0.05
CONSENSUS_N = 2  # ≥2 core HARD sells

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


def _load_watch_module():
    spec = importlib.util.spec_from_file_location(
        "epw_ses", ROOT / "scripts/research/run_expert_pool_watch.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["epw_ses"] = mod
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
    oos = blk([t for t in trades if t["oos"]])
    early_n = sum(1 for t in trades if t.get("exit_reason") not in (None, "time"))
    hold_days = [t.get("hold_days") for t in trades if t.get("hold_days") is not None]
    return {
        "n": allb["n"],
        "med": allb["med"],
        "mean": allb["mean"],
        "win": allb["win"],
        "oos_n": oos["n"],
        "oos_med": oos["med"],
        "oos_win": oos["win"],
        "early_frac": (early_n / allb["n"]) if allb["n"] else None,
        "med_hold_days": median(hold_days) if hold_days else None,
        "exit_reasons": _reason_counts(trades),
    }


def _reason_counts(trades: list[dict]) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for t in trades:
        out[str(t.get("exit_reason") or "time")] += 1
    return dict(out)


def pct(x: float | None, digits: int = 2) -> str:
    if x is None:
        return "—"
    return f"{x * 100:+.{digits}f}%"


def win_pct(x: float | None) -> str:
    if x is None:
        return "—"
    return f"{x * 100:.0f}%"


def frac_pct(x: float | None) -> str:
    if x is None:
        return "—"
    return f"{x * 100:.0f}%"


def main() -> None:
    mod = _load_watch_module()
    cores, floors, modes, names = load_champions(mod)
    print("universe", len(UNIVERSE), UNIVERSE)

    con = sqlite3.connect(f"file:{DB.resolve()}?mode=ro", uri=True, timeout=120)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    con.execute("PRAGMA busy_timeout=120000")
    ph = ",".join("?" * len(UNIVERSE))

    bars: dict[str, dict[str, tuple[float, float, float, float]]] = defaultdict(dict)
    for r in con.execute(
        f"""SELECT stock_id, trade_date, open, high, low, close FROM stock_daily_bars
            WHERE source=? AND stock_id IN ({ph})
              AND trade_date BETWEEN date(?, '-5 days') AND date(?, '+25 days')
              AND close>0""",
        (SOURCE, *UNIVERSE, START, END),
    ):
        o = float(r["open"] or r["close"])
        c = float(r["close"])
        if o <= 0:
            continue
        hi = float(r["high"] or max(o, c))
        lo = float(r["low"] or min(o, c))
        bars[r["stock_id"]][r["trade_date"]] = (o, hi, lo, c)

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
    # Buy-side (green) + sell-side (HARD sell) nets for core branches.
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
                  AND trade_date BETWEEN ? AND date(?, '+20 days')
                  AND net != 0""",
            (SOURCE, *UNIVERSE, *core_tids, START, END),
        ):
            oc = bars[r["stock_id"]].get(r["trade_date"])
            if oc:
                core_by[r["stock_id"]][r["trade_date"]][
                    r["securities_trader_id"]
                ] = float(r["net"]) * oc[3]
    con.close()

    cals = {
        sid: sorted(d for d in bars[sid] if START <= d <= END) for sid in UNIVERSE
    }
    path_cals = {sid: sorted(bars[sid]) for sid in UNIVERSE}
    path_idx = {sid: {d: i for i, d in enumerate(path_cals[sid])} for sid in UNIVERSE}

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

    def consensus_hard_sell(sid: str, d: str) -> bool:
        """≥2 core branches with sell_amt ≥ hard floor (= net_floor)."""
        core = cores[sid]
        if not core:
            return False
        floor = floors[sid]
        hard_n = sum(
            1
            for t, a in core_by[sid].get(d, {}).items()
            if t in core and a <= -floor
        )
        return hard_n >= CONSENSUS_N

    def bench_ret(ed: str, xd: str, *, px_mode: str = "open") -> float | None:
        j = next((k for k, d in enumerate(ix_dates) if d >= ed), None)
        xj = next((k for k, d in enumerate(ix_dates) if d >= xd), None)
        if j is None or xj is None:
            return None
        bo = ix[ix_dates[j]][0] if px_mode == "open" else ix[ix_dates[j]][1]
        bc = ix[ix_dates[xj]][1]
        if bo <= 0:
            return None
        return bc / bo - 1

    def pack(
        sid: str,
        sig: str,
        ep: float,
        ed: str,
        xp: float,
        xd: str,
        reason: str,
        *,
        entry_i: int | None = None,
    ) -> dict | None:
        if ep <= 0 or xp <= 0:
            return None
        ret = xp / ep - 1 - COST
        bret = bench_ret(ed, xd, px_mode="open")
        if bret is None:
            return None
        hold_days = None
        if entry_i is not None:
            xi = path_idx[sid].get(xd)
            if xi is not None:
                hold_days = xi - entry_i + 1
        return {
            "sid": sid,
            "sig": sig,
            "entry": ed,
            "exit": xd,
            "exit_reason": reason,
            "hold_days": hold_days,
            "ret": ret,
            "radj": ret - BETA * bret,
            "oos": sig >= OOS,
        }

    def time_exit(sid: str, entry_i: int, hold: int) -> dict | None:
        cal = path_cals[sid]
        if entry_i < 0 or entry_i + hold - 1 >= len(cal):
            return None
        ed = cal[entry_i]
        xd = cal[entry_i + hold - 1]
        o, _, _, _ = bars[sid][ed]
        _, _, _, xc = bars[sid][xd]
        return pack(sid, "", o, ed, xc, xd, "time", entry_i=entry_i)

    def path_exit_price(
        sid: str,
        entry_i: int,
        *,
        max_hold: int,
        stop_pct: float | None = None,
        peak_dd: float | None = None,
    ) -> dict | None:
        cal = path_cals[sid]
        if entry_i < 0 or entry_i + max_hold - 1 >= len(cal):
            return None
        ed = cal[entry_i]
        o, hi0, lo0, c0 = bars[sid][ed]
        ep = o
        stop_px = ep * (1.0 - stop_pct) if stop_pct is not None else None

        if stop_px is not None and lo0 <= stop_px:
            return pack(
                sid, "", ep, ed, stop_px, ed, "sl", entry_i=entry_i
            )

        peak = c0
        last_i = entry_i + max_hold - 1
        for j in range(entry_i + 1, last_i + 1):
            d = cal[j]
            _, hi, lo, cl = bars[sid][d]
            if stop_px is not None and lo <= stop_px:
                return pack(
                    sid, "", ep, ed, stop_px, d, "sl", entry_i=entry_i
                )
            if peak_dd is not None:
                if cl <= peak * (1.0 - peak_dd):
                    return pack(
                        sid, "", ep, ed, cl, d, "peak_dd", entry_i=entry_i
                    )
                peak = max(peak, cl)

        xd = cal[last_i]
        _, _, _, xc = bars[sid][xd]
        return pack(sid, "", ep, ed, xc, xd, "time", entry_i=entry_i)

    def consensus_exit(
        sid: str,
        entry_i: int,
        *,
        max_hold: int,
        also_peak_dd: bool = False,
    ) -> dict | None:
        """First CONSENSUS HARD on day D → exit next open; else time @ max_hold.

        Sell tape is EOD (same observability as green). Window scanned:
        entry day .. entry+(max_hold-1). Next-open may land one day past the
        time-cap when sell fires on the last hold day.
        """
        cal = path_cals[sid]
        if entry_i < 0 or entry_i + max_hold - 1 >= len(cal):
            return None
        ed = cal[entry_i]
        o, _, _, c0 = bars[sid][ed]
        ep = o
        peak = c0
        last_i = entry_i + max_hold - 1

        # Entry-day EOD: CONSENSUS HARD → next open (peak cannot trip vs itself).
        if consensus_hard_sell(sid, ed):
            if entry_i + 1 < len(cal):
                xd = cal[entry_i + 1]
                xo = bars[sid][xd][0]
                return pack(
                    sid, "", ep, ed, xo, xd, "cons_hard", entry_i=entry_i
                )

        for j in range(entry_i + 1, last_i + 1):
            d = cal[j]
            _, _, _, cl = bars[sid][d]
            if also_peak_dd:
                if cl <= peak * (1.0 - PEAK_DD):
                    return pack(
                        sid, "", ep, ed, cl, d, "peak_dd", entry_i=entry_i
                    )
                peak = max(peak, cl)
            if consensus_hard_sell(sid, d):
                if j + 1 < len(cal):
                    xd = cal[j + 1]
                    xo = bars[sid][xd][0]
                    return pack(
                        sid, "", ep, ed, xo, xd, "cons_hard", entry_i=entry_i
                    )
                break

        xd = cal[last_i]
        _, _, _, xc = bars[sid][xd]
        return pack(sid, "", ep, ed, xc, xd, "time", entry_i=entry_i)

    # Collect green signals (deduped) — identical to H_GREEN_HOLD_EXIT
    signals: list[tuple[str, int, str]] = []
    last: dict[str, str] = {}
    for sid in UNIVERSE:
        for d in cals[sid]:
            if not expert_green(sid, d):
                continue
            if not dedupe_ok(last, sid, d):
                continue
            pi = path_idx[sid].get(d)
            if pi is None or pi + 1 >= len(path_cals[sid]):
                continue
            signals.append((sid, pi, d))
            last[sid] = d
    print(f"green signals (deduped): {len(signals)}")

    # How often does CONSENSUS HARD appear within H7 path?
    cons_within_h7 = 0
    for sid, pi, _sig in signals:
        entry_i = pi + 1
        cal = path_cals[sid]
        if entry_i + BASE_H - 1 >= len(cal):
            continue
        hit = False
        for j in range(entry_i, entry_i + BASE_H):
            if consensus_hard_sell(sid, cal[j]):
                hit = True
                break
        if hit:
            cons_within_h7 += 1
    print(
        f"CONSENSUS HARD within H7 window: "
        f"{cons_within_h7}/{len(signals)} "
        f"({100 * cons_within_h7 / len(signals):.0f}%)"
        if signals
        else "no signals"
    )

    arms: dict[str, list[dict]] = {}

    def run_arm(name: str, fn) -> None:
        trades: list[dict] = []
        for sid, pi, sig in signals:
            r = fn(sid, pi + 1)
            if r is None:
                continue
            r["sig"] = sig
            r["oos"] = sig >= OOS
            trades.append(r)
        arms[name] = trades
        s = summarize(trades)
        print(
            f"  {name}: n={s['n']} med={pct(s['med'])} "
            f"oos_med={pct(s['oos_med'])} reasons={s['exit_reasons']}"
        )

    run_arm("L1H7", lambda sid, ei: time_exit(sid, ei, BASE_H))
    run_arm("L1H10", lambda sid, ei: time_exit(sid, ei, LONG_H))
    run_arm(
        "SL8_H7",
        lambda sid, ei: path_exit_price(
            sid, ei, max_hold=BASE_H, stop_pct=SL_PCT
        ),
    )
    run_arm(
        "PEAKDD5_H7",
        lambda sid, ei: path_exit_price(
            sid, ei, max_hold=BASE_H, peak_dd=PEAK_DD
        ),
    )
    run_arm(
        "CONS_HARD_H7",
        lambda sid, ei: consensus_exit(sid, ei, max_hold=BASE_H),
    )
    run_arm(
        "CONS_HARD_H10",
        lambda sid, ei: consensus_exit(sid, ei, max_hold=LONG_H),
    )
    run_arm(
        "CONS_OR_PEAKDD_H7",
        lambda sid, ei: consensus_exit(
            sid, ei, max_hold=BASE_H, also_peak_dd=True
        ),
    )

    stats = {k: summarize(v) for k, v in arms.items()}
    base = stats["L1H7"]

    def delta_med(arm: str) -> float | None:
        a, b = stats[arm]["med"], base["med"]
        if a is None or b is None:
            return None
        return a - b

    def delta_oos(arm: str) -> float | None:
        a, b = stats[arm]["oos_med"], base["oos_med"]
        if a is None or b is None:
            return None
        return a - b

    ranked = sorted(
        stats.keys(),
        key=lambda k: (
            stats[k]["oos_med"] is not None,
            stats[k]["oos_med"] if stats[k]["oos_med"] is not None else -9e9,
            stats[k]["med"] if stats[k]["med"] is not None else -9e9,
        ),
        reverse=True,
    )

    # Promote bar (same as H_GREEN_HOLD_EXIT): OOS med ≥+0.5pp vs L1H7
    # and ALL not worse by >1pp.
    adopt_candidate = None
    for k in ranked:
        if k == "L1H7":
            continue
        dm, do = delta_med(k), delta_oos(k)
        if do is None or dm is None:
            continue
        if do >= 0.005 and dm >= -0.01:
            adopt_candidate = k
            break

    cons7 = stats["CONS_HARD_H7"]
    cons10 = stats["CONS_HARD_H10"]
    sl = stats["SL8_H7"]
    pdd = stats["PEAKDD5_H7"]
    h10 = stats["L1H10"]
    cons_or = stats["CONS_OR_PEAKDD_H7"]

    cons_rank = ranked.index("CONS_HARD_H7") + 1
    cons10_rank = ranked.index("CONS_HARD_H10") + 1

    # Classify expert sell relative to losers (SL/peak) vs useful (H10)
    def _oos(arm: str) -> float:
        return stats[arm]["oos_med"] if stats[arm]["oos_med"] is not None else -9e9

    losers_ceiling = max(_oos("SL8_H7"), _oos("PEAKDD5_H7"))
    cons_oos = _oos("CONS_HARD_H7")
    if cons_oos <= losers_ceiling + 1e-9:
        sell_bucket = "with the losers (SL / peak-DD)"
    elif cons_oos + 1e-9 >= _oos("L1H10"):
        sell_bucket = "with / above longer-hold H10 (useful)"
    elif cons_oos + 1e-9 >= _oos("L1H7"):
        sell_bucket = "near baseline L1H7 (neutral — not a clear upgrade)"
    else:
        sell_bucket = "between losers and L1H7 (partial hurt)"

    if adopt_candidate == "L1H10":
        freeze = (
            "keep L1H7 as protocol SSOT; research note L1H10 longer-hold. "
            "CONSENSUS HARD sell-exit does not clear promote bar — "
            "do not adopt as exit overlay."
        )
    elif adopt_candidate and adopt_candidate.startswith("CONS"):
        freeze = (
            f"keep L1H7 SSOT; {adopt_candidate} clears weak OOS bar only — "
            "research note, not Strategy／Order."
        )
    elif adopt_candidate:
        freeze = (
            f"keep L1H7 as protocol SSOT; {adopt_candidate} is weak OOS edge only"
        )
    else:
        freeze = (
            "freeze L1H7 — CONSENSUS HARD sell-exit does not clear promote bar"
        )

    # ---- markdown ----
    lines: list[str] = []
    lines.append(
        "# H_SELL_EXIT_VS_STOPS · expert CONSENSUS HARD sell vs SL / peak / H10"
    )
    lines.append("")
    lines.append(f"- 生成：{datetime.now():%Y-%m-%d %H:%M}")
    lines.append("- Research only · sqlite `mode=ro` · **未採納** Strategy／Order")
    lines.append("- Runner：`scripts/research/run_h_sell_exit_vs_stops.py`")
    lines.append("- Align：`H_GREEN_HOLD_EXIT` universe／cost／β／OOS／dedupe／L1")
    lines.append("")
    lines.append("## Verdict")
    lines.append("")
    g7 = stats["L1H7"]
    lines.append(
        f"**Baseline L1H7**：n={g7['n']} · ALL med {pct(g7['med'])} · "
        f"win {win_pct(g7['win'])} · OOS n={g7['oos_n']} med {pct(g7['oos_med'])} "
        f"win {win_pct(g7['oos_win'])}."
    )
    lines.append("")
    lines.append(
        f"**Expert sell bucket**：CONSENSUS HARD@H7 ranks **#{cons_rank}** by OOS med "
        f"({pct(cons7['oos_med'])}) — **{sell_bucket}**."
    )
    lines.append("")
    lines.append(
        f"- vs SL−8%@H7 OOS {pct(sl['oos_med'])}：Δ "
        f"{pct((cons7['oos_med'] or 0) - (sl['oos_med'] or 0))}"
    )
    lines.append(
        f"- vs peakDD−5%@H7 OOS {pct(pdd['oos_med'])}：Δ "
        f"{pct((cons7['oos_med'] or 0) - (pdd['oos_med'] or 0))}"
    )
    lines.append(
        f"- vs L1H7 OOS {pct(g7['oos_med'])}：Δ {pct(delta_oos('CONS_HARD_H7'))}"
    )
    lines.append(
        f"- vs L1H10 OOS {pct(h10['oos_med'])}：Δ "
        f"{pct((cons7['oos_med'] or 0) - (h10['oos_med'] or 0))}"
    )
    lines.append("")
    if adopt_candidate:
        ac = stats[adopt_candidate]
        lines.append(
            f"**Best clear-bar arm**：`{adopt_candidate}` · "
            f"ALL {pct(ac['med'])} (Δ {pct(delta_med(adopt_candidate))}) · "
            f"OOS {pct(ac['oos_med'])} (Δ {pct(delta_oos(adopt_candidate))})."
        )
    else:
        lines.append(
            f"**Best ranked**：`{ranked[0]}` · "
            f"OOS {pct(stats[ranked[0]]['oos_med'])} — "
            "no arm besides longer-hold typically clears promote vs L1H7."
        )
    lines.append("")
    lines.append(f"**Freeze recommendation**：{freeze}")
    lines.append("")
    lines.append("## Frozen protocol")
    lines.append("")
    lines.append("| 鍵 | 值 |")
    lines.append("|----|-----|")
    lines.append(f"| window | `{START}`..`{END}` |")
    lines.append(
        "| universe | adopted ∩ hard≥3 ∩ 非權值熱門 → **14**： "
        + ", ".join(UNIVERSE)
        + " |"
    )
    lines.append(
        "| green | 各股 champion（watch_spec／POOLS）：回看1日／同日／OR × core×floor |"
    )
    lines.append("| entry | 綠燈訊號 **T+1 open**（L1） |")
    lines.append("| cost | 30bps（只扣個股） |")
    lines.append(
        "| excess | "
        + r"\(r_{\mathrm{adj}}=r-1.15\times r_{IX0001}\)"
        + " |"
    )
    lines.append(f"| OOS | 訊號日 ≥ `{OOS}` |")
    lines.append("| dedupe | 同股 **5 曆日** |")
    lines.append(
        "| CONSENSUS HARD | 同日 ≥"
        + str(CONSENSUS_N)
        + " 家 **core** 淨賣金額 ≥ hard（= pool `net_floor`）→ **次日開**出場 |"
    )
    lines.append(
        "| note | 嚴於 live `holdings_branch_sell_monitor` 的 CONSENSUS（≥2 soft+） |"
    )
    lines.append("| DB | `data/stocks.db` mode=ro · source=finmind |")
    lines.append("")
    lines.append("### Champion modes")
    lines.append("")
    lines.append("| sid | 名 | mode | floor | core_n |")
    lines.append("|-----|----|------|------:|-------:|")
    for sid in UNIVERSE:
        fl = floors[sid]
        fl_s = "≥1億" if fl >= 1e8 - 1 else "≥0.5億"
        lines.append(
            f"| {sid} | {names[sid]} | {modes[sid]} | {fl_s} | {len(cores[sid])} |"
        )
    lines.append("")
    lines.append("## Arms")
    lines.append("")
    lines.append(
        f"Green signals（deduped）n={len(signals)} · "
        f"CONSENSUS HARD fires within H7 path："
        f"{cons_within_h7}（{frac_pct(cons_within_h7 / len(signals) if signals else None)}）。"
    )
    lines.append("")
    lines.append(
        "| arm | n | early% | med hold | ALL med | win | OOS n | OOS med | "
        "OOS win | ΔALL vs L1H7 | ΔOOS | reasons |"
    )
    lines.append(
        "|-----|--:|-------:|---------:|--------:|----:|------:|--------:|"
        "--------:|-------------:|------:|---------|"
    )
    arm_order = [
        "L1H7",
        "L1H10",
        "SL8_H7",
        "PEAKDD5_H7",
        "CONS_HARD_H7",
        "CONS_HARD_H10",
        "CONS_OR_PEAKDD_H7",
    ]
    for name in arm_order:
        s = stats[name]
        rs = ", ".join(f"{a}={b}" for a, b in sorted(s["exit_reasons"].items()))
        med_h = (
            f"{s['med_hold_days']:.0f}"
            if s["med_hold_days"] is not None
            else "—"
        )
        bold = "**" if name == "L1H7" else ""
        lines.append(
            f"| {bold}{name}{bold} | {s['n']} | {frac_pct(s['early_frac'])} | "
            f"{med_h} | {pct(s['med'])} | {win_pct(s['win'])} | {s['oos_n']} | "
            f"{pct(s['oos_med'])} | {win_pct(s['oos_win'])} | "
            f"{pct(delta_med(name))} | {pct(delta_oos(name))} | {rs} |"
        )
    lines.append("")
    lines.append("### Arm definitions")
    lines.append("")
    lines.append("| # | arm | rule |")
    lines.append("|--:|-----|------|")
    lines.append("| 1 | L1H7 | T+1 open → H7 close（協議 SSOT） |")
    lines.append("| 2 | L1H10 | T+1 open → H10 close（longer hold） |")
    lines.append("| 3 | SL8_H7 | 日低觸 −8% → 出；否則 H7 close |")
    lines.append("| 4 | PEAKDD5_H7 | 收盤 ≤ max-close×0.95 → 出；否則 H7 |")
    lines.append(
        "| 5 | CONS_HARD_H7 | 首個 CONSENSUS HARD → **次日開**；從未觸 → H7 close |"
    )
    lines.append(
        "| 6 | CONS_HARD_H10 | 同 #5，從未觸 → H10 close |"
    )
    lines.append(
        "| 7 | CONS_OR_PEAKDD_H7 | CONSENSUS HARD（次日開）**或** peak DD（收盤）先到；否則 H7 |"
    )
    lines.append("")
    lines.append("## Ranking（OOS med → ALL med）")
    lines.append("")
    lines.append("| rank | arm | OOS med | ALL med | note |")
    lines.append("|-----:|-----|--------:|--------:|------|")
    for i, k in enumerate(ranked, 1):
        note = ""
        if k == "L1H7":
            note = "baseline"
        elif k == adopt_candidate:
            note = "best clear-bar candidate"
        elif k.startswith("CONS"):
            note = f"expert sell · bucket: {sell_bucket}"
        lines.append(
            f"| {i} | `{k}` | {pct(stats[k]['oos_med'])} | "
            f"{pct(stats[k]['med'])} | {note} |"
        )
    lines.append("")
    lines.append("## Reading")
    lines.append("")
    lines.append(
        f"- **H10 vs H7**：對齊 H_GREEN_HOLD_EXIT — L1H10 OOS {pct(h10['oos_med'])} "
        f"vs L1H7 {pct(g7['oos_med'])}（Δ {pct(delta_oos('L1H10'))}）。"
    )
    lines.append(
        f"- **SL / peak still hurt**：SL OOS {pct(sl['oos_med'])}（Δ {pct(delta_oos('SL8_H7'))}）；"
        f"peak OOS {pct(pdd['oos_med'])}（Δ {pct(delta_oos('PEAKDD5_H7'))}）。"
    )
    lines.append(
        f"- **CONS_HARD_H7**：early {frac_pct(cons7['early_frac'])} · "
        f"med hold {cons7['med_hold_days']} · "
        f"ALL {pct(cons7['med'])}（Δ {pct(delta_med('CONS_HARD_H7'))}）· "
        f"OOS {pct(cons7['oos_med'])}（Δ {pct(delta_oos('CONS_HARD_H7'))}）· "
        f"rank #{cons_rank}/{len(ranked)}。"
    )
    lines.append(
        f"- **CONS_HARD_H10**：early {frac_pct(cons10['early_frac'])} · "
        f"OOS {pct(cons10['oos_med'])}（Δ {pct(delta_oos('CONS_HARD_H10'))}）· "
        f"rank #{cons10_rank} — "
        + (
            "若多數仍 time@H10，代表共識賣訊號稀疏，臂近似 L1H10。"
            if (cons10["early_frac"] or 0) < 0.35
            else "共識賣有一定觸發率；對照 reasons 欄。"
        )
    )
    lines.append(
        f"- **CONS∨peak**：OOS {pct(cons_or['oos_med'])}（Δ {pct(delta_oos('CONS_OR_PEAKDD_H7'))}）· "
        f"reasons {cons_or['exit_reasons']} — OR 通常被 peak 拖累。"
    )
    lines.append("")
    lines.append("## Freeze recommendation")
    lines.append("")
    lines.append(f"**{freeze}**")
    lines.append("")
    lines.append("- 不寫 `config/strategy.yaml`／不進 Order。")
    lines.append(
        "- 協議持有仍以 **L1H7** 為 SSOT；H10 僅研究註記；"
        "CONSENSUS HARD 賣方監控維持 **observe／email only**。"
    )
    lines.append("")
    lines.append("## Caveats")
    lines.append("")
    lines.append(
        "- 單槽、無容量／重疊；sum "
        + r"\(r_{adj}\)"
        + " 不可當複利權益。"
    )
    lines.append(
        "- CONSENSUS HARD 用 EOD tape → 次日開，與綠燈 L1 對稱；"
        "末持有日觸發時 next-open 可能略逾 H7／H10 時間帽。"
    )
    lines.append(
        "- 本臂「≥2 HARD」嚴於 live monitor 的 CONSENSUS（≥2 soft+）；"
        "若改 soft 共識，觸發率上升、誤殺風險亦升。"
    )
    lines.append("- SL／peak 定義與 `H_GREEN_HOLD_EXIT` 相同。")
    lines.append("")
    lines.append("## Reproduction")
    lines.append("")
    lines.append("```bash")
    lines.append(
        "PYTHONPATH=src .venv/bin/python scripts/research/run_h_sell_exit_vs_stops.py"
    )
    lines.append("```")
    lines.append("")

    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.write_text("\n".join(lines), encoding="utf-8")
    payload = {
        "generated": datetime.now().isoformat(timespec="seconds"),
        "protocol": {
            "start": START,
            "end": END,
            "oos": OOS,
            "cost": COST,
            "beta": BETA,
            "dedup_calendar_days": DEDUP,
            "universe": UNIVERSE,
            "consensus_hard_n": CONSENSUS_N,
            "align": "H_GREEN_HOLD_EXIT",
        },
        "baseline": "L1H7",
        "ranked": ranked,
        "cons_hard_h7_rank": cons_rank,
        "sell_bucket": sell_bucket,
        "adopt_candidate": adopt_candidate,
        "freeze": freeze,
        "n_signals": len(signals),
        "cons_hard_within_h7": cons_within_h7,
        "stats": {
            k: {
                **{kk: vv for kk, vv in s.items() if kk != "exit_reasons"},
                "exit_reasons": s["exit_reasons"],
                "delta_all_vs_l1h7": delta_med(k),
                "delta_oos_vs_l1h7": delta_oos(k),
            }
            for k, s in stats.items()
        },
    }
    OUT_JSON.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"wrote {OUT_MD}")
    print(f"wrote {OUT_JSON}")
    print("freeze:", freeze)
    print("sell_bucket:", sell_bucket)
    print("rank CONS_HARD_H7:", cons_rank, "candidate:", adopt_candidate)


if __name__ == "__main__":
    main()
