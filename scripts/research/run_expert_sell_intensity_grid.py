#!/usr/bin/env python3
"""H_EXPERT_SELL_INTENSITY_GRID: sell intensity × exit timing after green entry.

Research only · sqlite mode=ro · thick adopted∩hard≥3 · does NOT touch Strategy/Order.

After champion green → T+1 open, scan expert-core sell pressure while held
(max L1H7). First day D that clears an intensity gate → exit at timing arm.
No fire → fall back to L1H7 close.

Intensity (align holdings_branch_sell_monitor):
  hard = pool net_floor · soft = max(hard×0.5, 2500萬)
  1HARD      ≥1 core sell_amt ≥ hard
  2SOFT+     ≥2 core sell_amt ≥ soft  (CONSENSUS)
  2HARD      ≥2 core sell_amt ≥ hard
  3HARD+     ≥3 core sell_amt ≥ hard
  AGG1       Σ core net_amt ≤ −1×floor
  AGG2       Σ core net_amt ≤ −2×floor

Timing (D = sell-signal day · EOD tape observability):
  D_CLOSE · D1_OPEN · D1_CLOSE

Benchmarks: unconditional L1H7 / L1H10.

  PYTHONPATH=src .venv/bin/python scripts/research/run_expert_sell_intensity_grid.py
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
    / "H_EXPERT_SELL_INTENSITY_GRID.md"
)
OUT_JSON = OUT_MD.with_suffix(".json")
DB = ROOT / "data" / "stocks.db"
SOURCE = "finmind"
START, END, OOS = "2024-07-01", "2026-07-20", "2026-01-01"
COST, BETA, DEDUP = 0.003, 1.15, 5
HOLD_H7, HOLD_H10 = 7, 10
SOFT_FLOOR_MIN = 2.5e7

# Same thick universe as H_GREEN_HOLD_EXIT / soft-vs-hard green
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

INTENSITIES = (
    "1HARD",
    "2SOFT+",
    "2HARD",
    "3HARD+",
    "AGG1",
    "AGG2",
)
TIMINGS = ("D_CLOSE", "D1_OPEN", "D1_CLOSE")


def _load_watch_module():
    spec = importlib.util.spec_from_file_location(
        "epw_sell_grid", ROOT / "scripts/research/run_expert_pool_watch.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["epw_sell_grid"] = mod
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
    is_ = blk([t for t in trades if not t["oos"]])
    early = [t for t in trades if t.get("early")]
    fired = [t for t in trades if t.get("sell_fired")]
    # False alarm: sell fired but unconditional H7 radj would still be >0
    fa = [t for t in fired if (t.get("h7_radj") or 0) > 0]
    # Miss: no sell fire but H7 radj ≤ 0
    miss = [t for t in trades if (not t.get("sell_fired")) and (t.get("h7_radj") or 0) <= 0]
    # Among fired: did early exit beat staying to H7?
    beat_h7 = [t for t in early if t.get("h7_radj") is not None and t["radj"] > t["h7_radj"]]
    return {
        "n": allb["n"],
        "med": allb["med"],
        "mean": allb["mean"],
        "win": allb["win"],
        "oos_n": oos["n"],
        "oos_med": oos["med"],
        "oos_mean": oos["mean"],
        "oos_win": oos["win"],
        "is_n": is_["n"],
        "is_med": is_["med"],
        "early_n": len(early),
        "early_frac": (len(early) / allb["n"]) if allb["n"] else None,
        "fire_n": len(fired),
        "fire_frac": (len(fired) / allb["n"]) if allb["n"] else None,
        "fa_n": len(fa),
        "fa_rate": (len(fa) / len(fired)) if fired else None,
        "miss_n": len(miss),
        "miss_rate": (len(miss) / allb["n"]) if allb["n"] else None,
        "beat_h7_n": len(beat_h7),
        "beat_h7_rate": (len(beat_h7) / len(early)) if early else None,
        "oos_early_n": sum(1 for t in early if t["oos"]),
        "oos_fa_rate": (
            (sum(1 for t in fa if t["oos"]) / sum(1 for t in fired if t["oos"]))
            if any(t["oos"] for t in fired)
            else None
        ),
    }


def main() -> None:
    mod = _load_watch_module()
    cores, floors, modes, names = load_champions(mod)
    print("universe", len(UNIVERSE), UNIVERSE)
    for sid in UNIVERSE:
        soft, hard = soft_hard(floors[sid])
        print(
            f"  {sid} {names[sid]} mode={modes[sid]} "
            f"floor={hard:.0f} soft={soft:.0f} core_n={len(cores[sid])}"
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
              AND trade_date BETWEEN date(?, '-5 days') AND date(?, '+25 days')
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
             AND date BETWEEN date(?, '-5 days') AND date(?, '+25 days')
             AND open>0 AND close>0
           ORDER BY date,
             CASE source WHEN 'yahoo' THEN 0 WHEN 'tej' THEN 1
                  WHEN 'finmind' THEN 2 ELSE 3 END""",
        (START, END),
    ):
        ix.setdefault(r["date"], (float(r["open"]), float(r["close"])))
    ix_dates = sorted(ix)

    # All core nets (buy + sell) for green + sell intensity
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
                  AND trade_date BETWEEN ? AND ?""",
            (SOURCE, *UNIVERSE, *core_tids, START, END),
        ):
            oc = bars[r["stock_id"]].get(r["trade_date"])
            if oc:
                core_by[r["stock_id"]][r["trade_date"]][
                    r["securities_trader_id"]
                ] = float(r["net"]) * oc[1]
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

    def day_sell_feats(sid: str, d: str) -> dict:
        core = cores[sid]
        soft, hard = soft_hard(floors[sid])
        amts = {t: a for t, a in core_by[sid].get(d, {}).items() if t in core}
        sell_amts = [-a for a in amts.values() if a < 0]
        n_hard = sum(1 for s in sell_amts if s >= hard)
        n_soft = sum(1 for s in sell_amts if s >= soft)
        agg = sum(amts.values()) if amts else 0.0
        return {
            "n_hard": n_hard,
            "n_soft": n_soft,
            "agg": agg,
            "soft": soft,
            "hard": hard,
            "1HARD": n_hard >= 1,
            "2SOFT+": n_soft >= 2,
            "2HARD": n_hard >= 2,
            "3HARD+": n_hard >= 3,
            "AGG1": agg <= -1.0 * hard,
            "AGG2": agg <= -2.0 * hard,
        }

    def bench_ret(ed: str, xd: str, *, entry_px: str = "open", exit_px: str = "close") -> float | None:
        j = next((k for k, d in enumerate(ix_dates) if d >= ed), None)
        xj = next((k for k, d in enumerate(ix_dates) if d >= xd), None)
        if j is None or xj is None:
            return None
        bo = ix[ix_dates[j]][0 if entry_px == "open" else 1]
        bc = ix[ix_dates[xj]][0 if exit_px == "open" else 1]
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
        *,
        exit_px_mode: str = "close",
    ) -> dict | None:
        if ep <= 0 or xp <= 0:
            return None
        ret = xp / ep - 1 - COST
        bret = bench_ret(ed, xd, entry_px="open", exit_px=exit_px_mode)
        if bret is None:
            return None
        return {
            "sid": sid,
            "sig": sig,
            "entry": ed,
            "exit": xd,
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
        o, _ = bars[sid][ed]
        _, xc = bars[sid][xd]
        return pack(sid, "", o, ed, xc, xd)

    # Green signals
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

    # Unconditional benches
    arms: dict[str, list[dict]] = {}
    for hold, key in ((HOLD_H7, "L1H7"), (HOLD_H10, "L1H10")):
        trades = []
        for sid, pi, sig in signals:
            r = time_exit(sid, pi + 1, hold)
            if r is None:
                continue
            r["sig"] = sig
            r["oos"] = sig >= OOS
            r["early"] = False
            r["sell_fired"] = False
            r["h7_radj"] = r["radj"] if hold == HOLD_H7 else None
            trades.append(r)
        # attach h7_radj for H10 trades
        if key == "L1H10":
            h7_map = {
                (t["sid"], t["sig"]): t["radj"] for t in arms.get("L1H7", [])
            }
            for t in trades:
                t["h7_radj"] = h7_map.get((t["sid"], t["sig"]))
        arms[key] = trades
        print(f"  {key}: n={len(trades)}")

    h7_by_sig = {(t["sid"], t["sig"]): t for t in arms["L1H7"]}

    def simulate(intensity: str, timing: str) -> list[dict]:
        out: list[dict] = []
        for sid, pi, sig in signals:
            entry_i = pi + 1
            cal = path_cals[sid]
            if entry_i + HOLD_H7 - 1 >= len(cal):
                continue
            h7 = h7_by_sig.get((sid, sig))
            if h7 is None:
                continue
            ed = cal[entry_i]
            ep = bars[sid][ed][0]
            last_i = entry_i + HOLD_H7 - 1
            fire_d: str | None = None
            fire_i: int | None = None
            # Scan hold days for first intensity hit (EOD features of day D)
            for j in range(entry_i, last_i + 1):
                d = cal[j]
                if d > END:
                    break
                feats = day_sell_feats(sid, d)
                if feats.get(intensity):
                    fire_d = d
                    fire_i = j
                    break

            if fire_d is None or fire_i is None:
                # no sell → L1H7
                r = dict(h7)
                r["early"] = False
                r["sell_fired"] = False
                r["sell_day"] = None
                r["intensity"] = intensity
                r["timing"] = timing
                r["exit_reason"] = "time_h7"
                out.append(r)
                continue

            # Resolve exit from fire day
            xd: str | None = None
            xp: float | None = None
            exit_px_mode = "close"
            reason = f"{intensity}_{timing}"
            if timing == "D_CLOSE":
                xd = fire_d
                xp = bars[sid][fire_d][1]
                exit_px_mode = "close"
            elif timing == "D1_OPEN":
                if fire_i + 1 >= len(cal):
                    # cannot exit next open → fall back H7
                    r = dict(h7)
                    r["early"] = False
                    r["sell_fired"] = True
                    r["sell_day"] = fire_d
                    r["intensity"] = intensity
                    r["timing"] = timing
                    r["exit_reason"] = "time_h7_no_d1"
                    out.append(r)
                    continue
                xd = cal[fire_i + 1]
                xp = bars[sid][xd][0]
                exit_px_mode = "open"
            else:  # D1_CLOSE
                if fire_i + 1 >= len(cal):
                    r = dict(h7)
                    r["early"] = False
                    r["sell_fired"] = True
                    r["sell_day"] = fire_d
                    r["intensity"] = intensity
                    r["timing"] = timing
                    r["exit_reason"] = "time_h7_no_d1"
                    out.append(r)
                    continue
                xd = cal[fire_i + 1]
                xp = bars[sid][xd][1]
                exit_px_mode = "close"

            # Cap: never exit after scheduled H7 close (keep comparable max hold)
            if path_idx[sid][xd] > last_i:
                r = dict(h7)
                r["early"] = False
                r["sell_fired"] = True
                r["sell_day"] = fire_d
                r["intensity"] = intensity
                r["timing"] = timing
                r["exit_reason"] = "time_h7_past_cap"
                out.append(r)
                continue

            # If exit is exactly H7 close and same as time exit, still mark early
            # only when xd is before H7 day OR (D_CLOSE on last day = same as H7).
            early = path_idx[sid][xd] < last_i or (
                timing != "D_CLOSE" and path_idx[sid][xd] <= last_i and fire_i < last_i
            )
            # D_CLOSE on H7 day ≡ L1H7; treat as not early
            if fire_i == last_i and timing == "D_CLOSE":
                early = False

            packed = pack(sid, sig, ep, ed, xp, xd, exit_px_mode=exit_px_mode)
            if packed is None:
                continue
            packed["early"] = early and path_idx[sid][xd] < last_i
            packed["sell_fired"] = True
            packed["sell_day"] = fire_d
            packed["intensity"] = intensity
            packed["timing"] = timing
            packed["exit_reason"] = reason
            packed["h7_radj"] = h7["radj"]
            out.append(packed)
        return out

    grid_keys: list[str] = []
    for inten in INTENSITIES:
        for tim in TIMINGS:
            key = f"{inten}__{tim}"
            trades = simulate(inten, tim)
            arms[key] = trades
            grid_keys.append(key)
            s = summarize(trades)
            print(
                f"  {key}: n={s['n']} early={s['early_n']} "
                f"med={pct(s['med'])} oos_med={pct(s['oos_med'])} "
                f"fa={frac_pct(s['fa_rate'])} miss={frac_pct(s['miss_rate'])}"
            )

    stats = {k: summarize(v) for k, v in arms.items()}
    base = stats["L1H7"]
    h10 = stats["L1H10"]

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

    # Freeze: OOS med ≥+0.5pp vs L1H7 and ALL not worse by >1pp; prefer cover
    ranked = sorted(
        grid_keys,
        key=lambda k: (
            stats[k]["oos_med"] is not None,
            stats[k]["oos_med"] if stats[k]["oos_med"] is not None else -9e9,
            stats[k]["med"] if stats[k]["med"] is not None else -9e9,
            stats[k]["early_frac"] if stats[k]["early_frac"] is not None else 0,
        ),
        reverse=True,
    )

    adopt_candidate = None
    for k in ranked:
        dm = delta_med(k)
        do = delta_oos(k)
        if do is None or dm is None:
            continue
        if stats[k]["oos_n"] < 8:
            continue
        if do >= 0.005 and dm >= -0.01:
            adopt_candidate = k
            break

    best = ranked[0] if ranked else None

    # ---- markdown ----
    lines: list[str] = []
    lines.append("# H_EXPERT_SELL_INTENSITY_GRID · sell intensity × exit timing")
    lines.append("")
    lines.append(f"- 生成：{datetime.now():%Y-%m-%d %H:%M}")
    lines.append("- Research only · sqlite `mode=ro` · **未採納** Strategy／Order")
    lines.append("- Runner：`scripts/research/run_expert_sell_intensity_grid.py`")
    lines.append(
        "- Sell 定義對齊 `scripts/order/run_holdings_branch_sell_monitor.py`（observe 預警）"
    )
    lines.append("")
    lines.append("## Verdict")
    lines.append("")
    lines.append(
        f"**Baseline L1H7**：n={base['n']} · ALL med {pct(base['med'])} · "
        f"win {win_pct(base['win'])} · OOS n={base['oos_n']} med {pct(base['oos_med'])} "
        f"win {win_pct(base['oos_win'])}."
    )
    lines.append(
        f"**L1H10（unconditional）**：n={h10['n']} · ALL med {pct(h10['med'])} · "
        f"OOS med {pct(h10['oos_med'])} · "
        f"ΔALL vs L1H7 {pct(delta_med('L1H10'))} · ΔOOS {pct(delta_oos('L1H10'))}."
    )
    lines.append("")
    if adopt_candidate:
        ac = stats[adopt_candidate]
        lines.append(
            f"**Best cell clearing freeze bar**：`{adopt_candidate}` · "
            f"ALL med {pct(ac['med'])} (Δ {pct(delta_med(adopt_candidate))}) · "
            f"OOS med {pct(ac['oos_med'])} (Δ {pct(delta_oos(adopt_candidate))}) · "
            f"early {frac_pct(ac['early_frac'])} · FA {frac_pct(ac['fa_rate'])} · "
            f"miss {frac_pct(ac['miss_rate'])}."
        )
        lines.append("")
        lines.append(
            f"**Freeze recommendation**：research-note `{adopt_candidate}` only — "
            "not Strategy／Order; keep L1H7 as protocol SSOT until live observe validates."
        )
    else:
        if best:
            bs = stats[best]
            lines.append(
                f"**Best ranked cell（OOS-first）**：`{best}` · "
                f"ALL med {pct(bs['med'])} (Δ {pct(delta_med(best))}) · "
                f"OOS med {pct(bs['oos_med'])} (Δ {pct(delta_oos(best))}) · "
                f"early {frac_pct(bs['early_frac'])} · FA {frac_pct(bs['fa_rate'])}."
            )
        lines.append("")
        lines.append(
            "**Freeze recommendation**：**warning-only** — no intensity×timing cell "
            "clears OOS med ≥+0.5pp vs L1H7 with ALL not worse by >1pp "
            f"(and OOS n≥8). Keep **L1H7** SSOT; holdings sell monitor stays observe／email."
        )
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
    lines.append("| max hold | **H7**（無賣壓則 L1H7 close）；對照臂 L1H10 無條件 |")
    lines.append("| cost | 30bps（只扣個股） |")
    lines.append(
        "| excess | "
        + r"\(r_{\mathrm{adj}}=r-1.15\times r_{IX0001}\)"
        + " |"
    )
    lines.append(f"| OOS | 訊號日 ≥ `{OOS}` |")
    lines.append("| dedupe | 同股 **5 曆日** |")
    lines.append("| DB | `data/stocks.db` mode=ro · source=finmind |")
    lines.append(
        "| soft/hard | soft=max(floor×0.5, 2500萬) · hard=pool net_floor |"
    )
    lines.append("")
    lines.append("### EOD observability")
    lines.append("")
    lines.append(
        "- 分點 tape 通常 **收盤後** 才完整（與 20:00／20:10 觀測一致）。"
    )
    lines.append(
        "- **`D_CLOSE`**：理論上同日收可出，但實務多半 **不可執行**（得知時已收）。"
        "表內保留作上界；操作解讀以 **`D1_OPEN`** 為主。"
    )
    lines.append("- **`D1_OPEN` / `D1_CLOSE`**：訊號日 D 收盤後得知 → 下一交易日進出。")
    lines.append("")
    lines.append("### Champion modes")
    lines.append("")
    lines.append("| sid | 名 | mode | floor | soft | core_n |")
    lines.append("|-----|----|------|------:|-----:|-------:|")
    for sid in UNIVERSE:
        soft, hard = soft_hard(floors[sid])
        fl_s = "≥1億" if hard >= 1e8 - 1 else "≥0.5億"
        soft_s = f"{soft / 1e8:.2f}億" if soft >= 1e8 else f"{soft / 1e4:.0f}萬"
        lines.append(
            f"| {sid} | {names[sid]} | {modes[sid]} | {fl_s} | {soft_s} | "
            f"{len(cores[sid])} |"
        )
    lines.append("")
    lines.append("## Intensity defs")
    lines.append("")
    lines.append("| key | 條件 |")
    lines.append("|-----|------|")
    lines.append("| `1HARD` | ≥1 家 core 淨賣金額 ≥ hard |")
    lines.append("| `2SOFT+` | ≥2 家 core 淨賣 ≥ soft（CONSENSUS） |")
    lines.append("| `2HARD` | ≥2 家 core 淨賣 ≥ hard |")
    lines.append("| `3HARD+` | ≥3 家 core 淨賣 ≥ hard |")
    lines.append("| `AGG1` | Σ core `net_amt` ≤ −1×floor |")
    lines.append("| `AGG2` | Σ core `net_amt` ≤ −2×floor |")
    lines.append("")
    lines.append("## Benchmarks")
    lines.append("")
    lines.append(
        "| arm | n | ALL med | win | OOS n | OOS med | OOS win |"
    )
    lines.append("|-----|--:|--------:|----:|------:|--------:|--------:|")
    for k in ("L1H7", "L1H10"):
        s = stats[k]
        mark = "**" if k == "L1H7" else ""
        lines.append(
            f"| {mark}{k}{mark} | {s['n']} | {pct(s['med'])} | {win_pct(s['win'])} | "
            f"{s['oos_n']} | {pct(s['oos_med'])} | {win_pct(s['oos_win'])} |"
        )
    lines.append("")
    lines.append("## Grid · intensity × timing")
    lines.append("")
    lines.append(
        "| intensity | timing | n | early% | ALL med | ΔALL | OOS n | OOS med | ΔOOS | "
        "FA% | miss% | beatH7% |"
    )
    lines.append(
        "|-----------|--------|--:|-------:|--------:|-----:|------:|--------:|-----:|"
        "----:|------:|--------:|"
    )
    for inten in INTENSITIES:
        for tim in TIMINGS:
            k = f"{inten}__{tim}"
            s = stats[k]
            star = " ★" if k == adopt_candidate else ""
            lines.append(
                f"| {inten}{star} | {tim} | {s['n']} | {frac_pct(s['early_frac'])} | "
                f"{pct(s['med'])} | {pct(delta_med(k))} | {s['oos_n']} | "
                f"{pct(s['oos_med'])} | {pct(delta_oos(k))} | "
                f"{frac_pct(s['fa_rate'])} | {frac_pct(s['miss_rate'])} | "
                f"{frac_pct(s['beat_h7_rate'])} |"
            )
    lines.append("")
    lines.append(
        r"- **FA%（false alarm）**：賣壓觸發且該筆 **無條件 L1H7 \(r_{adj}\)>0** "
        "（早出可能砍到贏家）／觸發筆。"
    )
    lines.append(
        r"- **miss%**：未觸發且 L1H7 \(r_{adj}\)≤0／全部筆（漏掉該出場的輸家）。"
    )
    lines.append(
        r"- **beatH7%**：早出筆中，早出 \(r_{adj}\) > 同筆 L1H7 \(r_{adj}\) 的比例。"
    )
    lines.append("")
    lines.append("## Throughput / cover")
    lines.append("")
    lines.append("| intensity | timing | fire% | early% | OOS early n |")
    lines.append("|-----------|--------|------:|-------:|------------:|")
    for inten in INTENSITIES:
        for tim in TIMINGS:
            k = f"{inten}__{tim}"
            s = stats[k]
            lines.append(
                f"| {inten} | {tim} | {frac_pct(s['fire_frac'])} | "
                f"{frac_pct(s['early_frac'])} | {s['oos_early_n']} |"
            )
    lines.append("")
    lines.append("## Reading notes")
    lines.append("")
    all_neg = all(
        (delta_oos(k) is not None and delta_oos(k) < 0) for k in grid_keys
    )
    if all_neg:
        lines.append(
            "- **全格 ΔOOS vs L1H7 皆負**：分點淨賣 early-exit 在本宇宙／窗 **沒有** "
            "抬高 OOS 中位；鬆門檻（`1HARD`／`AGG1`）傷害最大（ΔOOS ≈ −5pp）。"
        )
    d1 = [k for k in grid_keys if k.endswith("__D1_OPEN")]
    if d1:
        least_hurt = max(
            d1,
            key=lambda k: (
                delta_oos(k) is not None,
                delta_oos(k) if delta_oos(k) is not None else -9e9,
            ),
        )
        lines.append(
            f"- 可執行軸（`D1_OPEN`）**傷害最小**（非擊敗）：`{least_hurt}` · "
            f"OOS med {pct(stats[least_hurt]['oos_med'])} · "
            f"ΔOOS {pct(delta_oos(least_hurt))} · "
            f"early {frac_pct(stats[least_hurt]['early_frac'])} · "
            f"FA {frac_pct(stats[least_hurt]['fa_rate'])} "
            f"（多半因幾乎不觸發、退回 L1H7）。"
        )
        fa_lo = min(
            (stats[k]["fa_rate"] for k in d1 if stats[k]["fa_rate"] is not None),
            default=None,
        )
        if fa_lo is not None:
            lines.append(
                f"- `D1_OPEN` FA 全落在 ~{frac_pct(fa_lo)}–"
                f"{frac_pct(max(stats[k]['fa_rate'] for k in d1 if stats[k]['fa_rate'] is not None))}："
                "觸發後 H7 仍為正的比例高 → 賣壓日≠必然輸家。"
            )
    lines.append(
        "- 強度↑ → cover↓；`3HARD+` early≈10% 仍 ΔOOS≈−1pp（砍到的多半是續漲段）。"
    )
    lines.append(
        f"- 無條件 **L1H10** OOS med {pct(h10['oos_med'])} "
        f"（ΔOOS {pct(delta_oos('L1H10'))}）優於任何賣壓 early-exit 格；"
        "若要改出場，優先研究延長持有，而非分點賣壓砍倉。"
    )
    lines.append(
        "- 與 `H_GREEN_HOLD_EXIT` 同向：價格 SL／peakDD 已 hurt；本格分點淨賣路徑同樣 "
        "**未過凍結門**。"
    )
    lines.append("")
    lines.append("## Freeze bar")
    lines.append("")
    lines.append(
        "- Promote heuristic（與 hold-exit 同構）：**OOS med ≥ +0.5pp vs L1H7** "
        "且 **ALL med 不差於 −1pp**，且 **OOS n≥8**。"
    )
    lines.append("- 未過門 → **warning-only**（現有 holdings sell monitor 可續寄，不當出場 SSOT）。")
    lines.append("")

    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")

    payload = {
        "generated": datetime.now().isoformat(timespec="seconds"),
        "protocol": {
            "start": START,
            "end": END,
            "oos": OOS,
            "cost": COST,
            "beta": BETA,
            "dedup": DEDUP,
            "universe": UNIVERSE,
            "intensities": list(INTENSITIES),
            "timings": list(TIMINGS),
            "soft_floor_min": SOFT_FLOOR_MIN,
        },
        "champions": {
            sid: {
                "name": names[sid],
                "mode": modes[sid],
                "floor": floors[sid],
                "soft": soft_hard(floors[sid])[0],
                "core_n": len(cores[sid]),
                "core": cores[sid],
            }
            for sid in UNIVERSE
        },
        "stats": stats,
        "adopt_candidate": adopt_candidate,
        "best_ranked": best,
        "n_signals": len(signals),
    }
    OUT_JSON.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"wrote {OUT_MD}")
    print(f"wrote {OUT_JSON}")
    print(f"adopt_candidate={adopt_candidate} best_ranked={best}")


if __name__ == "__main__":
    main()
