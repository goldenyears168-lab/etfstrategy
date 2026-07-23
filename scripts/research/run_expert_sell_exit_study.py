#!/usr/bin/env python3
"""H_EXPERT_SELL_EXIT: expert-pool sell-monitor early exit vs L1H7 / L1H10.

Research only · sqlite mode=ro · same thick universe as H_GREEN_HOLD_EXIT /
H_E_vs_GREEN_fair. Does not touch Strategy / Order / mini.

Question: after champion HARD green → T+1 open, does holdings-branch-sell
monitor logic (consensus HARD / single HARD / consensus soft+) beat holding
to H7 close (and H10)?

Sell rules (match scripts/order/run_holdings_branch_sell_monitor.py):
  hard = pool net_floor
  soft = max(hard × 0.5, 2.5e7)
  HARD day       = ≥1 core with sell_amt ≥ hard
  CONSENSUS soft = ≥2 core with sell_amt ≥ soft
  CONSENSUS HARD = CONSENSUS soft ∧ ≥1 HARD

Signal observability: FinMind EOD / 20:10 → exit **next session open**.
Hold scan: entry day 0 .. H7−1 (H7 sessions); no fire → H7 close.

  PYTHONPATH=src .venv/bin/python scripts/research/run_expert_sell_exit_study.py
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
    / "H_EXPERT_SELL_EXIT.md"
)
OUT_JSON = OUT_MD.with_suffix(".json")
DB = ROOT / "data" / "stocks.db"
SOURCE = "finmind"
START, END, OOS = "2024-07-01", "2026-07-17", "2026-01-01"
COST, BETA, DEDUP = 0.003, 1.15, 5
H7, H10 = 7, 10
SOFT_FLOOR_MIN = 2.5e7  # match run_holdings_branch_sell_monitor
# Freeze as early-exit only if OOS med ≥ H7 + 0.5pp and ALL not worse by >1pp.
FREEZE_OOS_EDGE = 0.005
FREEZE_ALL_FLOOR = -0.01
MIN_OOS_N = 12

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
        "epw_ese", ROOT / "scripts/research/run_expert_pool_watch.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["epw_ese"] = mod
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


def summarize(trades: list[dict], *, key: str = "radj") -> dict:
    def _b(ts: list[dict]) -> dict:
        return blk([t[key] for t in ts])

    allb = _b(trades)
    oos = _b([t for t in trades if t["oos"]])
    is_ = _b([t for t in trades if not t["oos"]])
    early_n = sum(1 for t in trades if t.get("exit_reason") != "time")
    reasons: dict[str, int] = defaultdict(int)
    for t in trades:
        reasons[str(t.get("exit_reason") or "time")] += 1
    return {
        "n": allb["n"],
        "med": allb["med"],
        "mean": allb["mean"],
        "win": allb["win"],
        "oos_n": oos["n"],
        "oos_med": oos["med"],
        "oos_win": oos["win"],
        "is_n": is_["n"],
        "is_med": is_["med"],
        "early_frac": (early_n / allb["n"]) if allb["n"] else None,
        "exit_reasons": dict(reasons),
    }


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

    # Buy-side nets (for green) and sell-side nets (for exit) on core seats.
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
    con.close()

    cals = {
        sid: sorted(d for d in bars[sid] if START <= d <= END) for sid in UNIVERSE
    }
    path_cals = {sid: sorted(bars[sid]) for sid in UNIVERSE}
    path_idx = {sid: {d: i for i, d in enumerate(path_cals[sid])} for sid in UNIVERSE}

    def day_core_amts(sid: str, d: str) -> dict[str, float]:
        core = cores[sid]
        return {t: a for t, a in core_by[sid].get(d, {}).items() if t in core}

    def expert_green(sid: str, d: str) -> bool:
        core = cores[sid]
        if not core:
            return False
        floor = floors[sid]
        mode = modes[sid]
        pi = path_idx[sid].get(d)
        if pi is None:
            return False
        amts = day_core_amts(sid, d)
        today = {t for t, a in amts.items() if a >= floor}
        yday: set[str] = set()
        if pi > 0:
            yd = path_cals[sid][pi - 1]
            yday = {t for t, a in day_core_amts(sid, yd).items() if a >= floor}
        if "OR" in mode:
            return len(today) >= 1
        if "同日" in mode:
            return len(today) >= 2
        return len(today) >= 1 and len(today | yday) >= 2

    def sell_flags(sid: str, d: str) -> dict:
        soft, hard = soft_hard(floors[sid])
        amts = day_core_amts(sid, d)
        # sell_amt = abs(net_amt) when net_amt < 0
        sell_amts = [-a for a in amts.values() if a < 0]
        n_soft = sum(1 for s in sell_amts if s >= soft)
        n_hard = sum(1 for s in sell_amts if s >= hard)
        return {
            "n_soft": n_soft,
            "n_hard": n_hard,
            "single_hard": n_hard >= 1,
            "cons_soft": n_soft >= 2,
            "cons_hard": n_soft >= 2 and n_hard >= 1,
            "cons_hard2": n_hard >= 2,  # sensitivity: ≥2 HARD seats
            "soft": soft,
            "hard": hard,
        }

    def bench_ret(ed: str, xd: str, *, entry_px: str = "open", exit_px: str = "close") -> float | None:
        j = next((k for k, d in enumerate(ix_dates) if d >= ed), None)
        xj = next((k for k, d in enumerate(ix_dates) if d >= xd), None)
        if j is None or xj is None:
            return None
        bo = ix[ix_dates[j]][0] if entry_px == "open" else ix[ix_dates[j]][1]
        bc = ix[ix_dates[xj]][1] if exit_px == "close" else ix[ix_dates[xj]][0]
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
        exit_px_mode: str,
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
            "exit_reason": reason,
            "ret": ret,
            "radj": ret - BETA * bret,
            "oos": sig >= OOS,
            "exit_px_mode": exit_px_mode,
        }

    def time_exit(sid: str, entry_i: int, hold: int) -> dict | None:
        cal = path_cals[sid]
        if entry_i < 0 or entry_i + hold - 1 >= len(cal):
            return None
        ed = cal[entry_i]
        xd = cal[entry_i + hold - 1]
        ep = bars[sid][ed][0]
        xc = bars[sid][xd][1]
        return pack(sid, "", ep, ed, xc, xd, "time", exit_px_mode="close")

    def sell_exit(
        sid: str,
        entry_i: int,
        *,
        flag_key: str,
        max_hold: int = H7,
        reason_tag: str,
    ) -> dict | None:
        """Scan hold days 0..max_hold-1; first flag → next open; else H close."""
        cal = path_cals[sid]
        last_i = entry_i + max_hold - 1
        if entry_i < 0 or last_i >= len(cal):
            return None
        ed = cal[entry_i]
        ep = bars[sid][ed][0]
        # Signal on session D evening → exit D+1 open. Need D+1 in calendar.
        for j in range(entry_i, last_i + 1):
            d = cal[j]
            fl = sell_flags(sid, d)
            if not fl[flag_key]:
                continue
            if j + 1 >= len(cal):
                break
            xd = cal[j + 1]
            xo = bars[sid][xd][0]
            row = pack(
                sid, "", ep, ed, xo, xd, reason_tag, exit_px_mode="open"
            )
            if row is None:
                continue
            row["signal_day"] = d
            row["hold_day"] = j - entry_i  # 0=entry .. 6=H7
            row["n_soft"] = fl["n_soft"]
            row["n_hard"] = fl["n_hard"]
            # Counterfactual: force H7 close anyway
            h7 = time_exit(sid, entry_i, max_hold)
            row["radj_h7_cf"] = h7["radj"] if h7 else None
            return row
        # no fire → time exit at H close
        row = time_exit(sid, entry_i, max_hold)
        if row is None:
            return None
        row["signal_day"] = None
        row["hold_day"] = None
        row["n_soft"] = 0
        row["n_hard"] = 0
        row["radj_h7_cf"] = row["radj"]
        return row

    # Green signals (deduped) — same as fair green
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

    arms: dict[str, list[dict]] = {}

    # 1–2) time baselines
    for h, name in ((H7, "L1H7"), (H10, "L1H10")):
        trades = []
        for sid, pi, sig in signals:
            r = time_exit(sid, pi + 1, h)
            if r is None:
                continue
            r["sig"] = sig
            r["oos"] = sig >= OOS
            trades.append(r)
        arms[name] = trades
        print(f"  {name}: n={len(trades)}")

    # 3–5) sell-monitor early exits (max hold H7)
    sell_specs = [
        ("CONS_HARD_NEXT_OPEN", "cons_hard", "cons_hard"),
        ("SINGLE_HARD_NEXT_OPEN", "single_hard", "single_hard"),
        ("CONS_SOFT_NEXT_OPEN", "cons_soft", "cons_soft"),
        # sensitivity
        ("CONS_HARD2_NEXT_OPEN", "cons_hard2", "cons_hard2"),
    ]
    for name, flag_key, tag in sell_specs:
        trades = []
        for sid, pi, sig in signals:
            r = sell_exit(sid, pi + 1, flag_key=flag_key, max_hold=H7, reason_tag=tag)
            if r is None:
                continue
            r["sig"] = sig
            r["oos"] = sig >= OOS
            trades.append(r)
        arms[name] = trades
        s = summarize(trades)
        print(
            f"  {name}: n={len(trades)} early={frac_pct(s['early_frac'])} "
            f"reasons={s['exit_reasons']}"
        )

    stats = {k: summarize(v) for k, v in arms.items()}
    base = stats["L1H7"]

    def d_all(arm: str) -> float | None:
        a, b = stats[arm]["med"], base["med"]
        if a is None or b is None:
            return None
        return a - b

    def d_oos(arm: str) -> float | None:
        a, b = stats[arm]["oos_med"], base["oos_med"]
        if a is None or b is None:
            return None
        return a - b

    # Path analysis on CONS_HARD
    ch = arms["CONS_HARD_NEXT_OPEN"]
    fired = [t for t in ch if t.get("exit_reason") != "time"]
    never = [t for t in ch if t.get("exit_reason") == "time"]
    path = {
        "cons_hard_fired_n": len(fired),
        "cons_hard_never_n": len(never),
        "fired_early": summarize(fired),
        "never_h7": summarize(never),
        "fired_cf_h7": summarize(
            [
                {
                    **t,
                    "radj": t["radj_h7_cf"],
                }
                for t in fired
                if t.get("radj_h7_cf") is not None
            ]
        ),
        "fired_delta_early_vs_cf": None,
        "hold_day_med": median([t["hold_day"] for t in fired if t.get("hold_day") is not None])
        if fired
        else None,
    }
    fe, fc = path["fired_early"]["med"], path["fired_cf_h7"]["med"]
    if fe is not None and fc is not None:
        path["fired_delta_early_vs_cf"] = fe - fc

    # Also path for single HARD / cons soft
    path_extra = {}
    for arm_name, label in (
        ("SINGLE_HARD_NEXT_OPEN", "single_hard"),
        ("CONS_SOFT_NEXT_OPEN", "cons_soft"),
    ):
        ts = arms[arm_name]
        f = [t for t in ts if t.get("exit_reason") != "time"]
        n = [t for t in ts if t.get("exit_reason") == "time"]
        path_extra[label] = {
            "fired_n": len(f),
            "never_n": len(n),
            "fired_early": summarize(f),
            "never_h7": summarize(n),
            "fired_cf_h7": summarize(
                [{**t, "radj": t["radj_h7_cf"]} for t in f if t.get("radj_h7_cf") is not None]
            ),
        }
        fe2 = path_extra[label]["fired_early"]["med"]
        fc2 = path_extra[label]["fired_cf_h7"]["med"]
        path_extra[label]["delta_early_vs_cf"] = (
            fe2 - fc2 if fe2 is not None and fc2 is not None else None
        )

    # Freeze decision: CONS_HARD primary
    primary = "CONS_HARD_NEXT_OPEN"
    do = d_oos(primary)
    da = d_all(primary)
    oos_n = stats[primary]["oos_n"]
    freeze = False
    freeze_note = "warning-only"
    if (
        do is not None
        and da is not None
        and oos_n >= MIN_OOS_N
        and do >= FREEZE_OOS_EDGE
        and da >= FREEZE_ALL_FLOOR
    ):
        freeze = True
        freeze_note = "freeze as early-exit overlay (research→strategy bar)"
    elif do is not None and do > 0 and (da is None or da >= FREEZE_ALL_FLOOR):
        freeze_note = "weak positive · warning-only (below freeze bar)"
    elif do is not None and do < 0:
        freeze_note = "hurts vs L1H7 · warning-only"
    else:
        freeze_note = "inconclusive / thin · warning-only"

    # Best sell arm by OOS
    sell_arms = [k for k in arms if k.endswith("NEXT_OPEN")]
    ranked_sell = sorted(
        sell_arms,
        key=lambda k: (
            stats[k]["oos_med"] is not None,
            stats[k]["oos_med"] if stats[k]["oos_med"] is not None else -9e9,
            stats[k]["med"] if stats[k]["med"] is not None else -9e9,
        ),
        reverse=True,
    )

    now = datetime.now().isoformat(timespec="seconds")
    lines: list[str] = []
    lines.append("# H_EXPERT_SELL_EXIT · consensus HARD sell vs L1H7")
    lines.append("")
    lines.append(f"- 生成：{now}")
    lines.append("- Research only · sqlite `mode=ro` · **未採納** Strategy／Order")
    lines.append("- Runner：`scripts/research/run_expert_sell_exit_study.py`")
    lines.append("- Align：`H_GREEN_HOLD_EXIT` / `H_E_vs_GREEN_fair` universe／cost／β／OOS")
    lines.append(
        "- Sell SSOT：`scripts/order/run_holdings_branch_sell_monitor.py` soft/hard/CONSENSUS"
    )
    lines.append("")
    lines.append("## Verdict")
    lines.append("")
    lines.append(
        f"**Baseline L1H7**：n={base['n']} · ALL med {pct(base['med'])} · "
        f"win {win_pct(base['win'])} · OOS n={base['oos_n']} med {pct(base['oos_med'])} "
        f"win {win_pct(base['oos_win'])}."
    )
    lines.append("")
    chs = stats[primary]
    lines.append(
        f"**CONSENSUS HARD → next open**：n={chs['n']} · early {frac_pct(chs['early_frac'])} · "
        f"ALL med {pct(chs['med'])} (Δ {pct(d_all(primary))}) · "
        f"OOS med {pct(chs['oos_med'])} (Δ {pct(d_oos(primary))})."
    )
    lines.append("")
    h10 = stats["L1H10"]
    lines.append(
        f"**L1H10**：ALL med {pct(h10['med'])} (Δ {pct(d_all('L1H10'))}) · "
        f"OOS med {pct(h10['oos_med'])} (Δ {pct(d_oos('L1H10'))})."
    )
    lines.append("")
    if freeze:
        lines.append(
            f"**Does consensus HARD beat H7?** YES on freeze bar "
            f"(OOS Δ≥{FREEZE_OOS_EDGE*100:.1f}pp, ALL Δ≥{FREEZE_ALL_FLOOR*100:.0f}pp)."
        )
    else:
        beat = do is not None and do > 0
        lines.append(
            f"**Does consensus HARD beat H7?** "
            + (
                "No — does not clear freeze bar."
                if not beat
                else "Marginally on OOS med, but **not** freeze-grade."
            )
        )
    lines.append("")
    lines.append(
        f"**Freeze recommendation**：{'freeze early-exit' if freeze else '**warning-only**'} "
        f"— {freeze_note}. Keep L1H7 as hold SSOT; 20:10 monitor stays observe／email."
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
    lines.append("| green | 各股 champion（watch_spec／POOLS）：回看1日／同日／OR × core×floor |")
    lines.append("| entry | 綠燈訊號 **T+1 open**（L1） |")
    lines.append("| cost | 30bps（只扣個股） |")
    lines.append("| excess | \\(r_{\\mathrm{adj}}=r-1.15\\times r_{IX0001}\\) |")
    lines.append(f"| OOS | 訊號日 ≥ `{OOS}` |")
    lines.append("| dedupe | 同股 **5 曆日** |")
    lines.append("| DB | `data/stocks.db` mode=ro · source=finmind |")
    lines.append(
        f"| soft | `max(net_floor×0.5, {SOFT_FLOOR_MIN:.1e})` · hard = `net_floor` |"
    )
    lines.append(
        "| CONSENSUS | ≥2 core sell_amt ≥ soft（對齊 holdings-branch-sell-monitor） |"
    )
    lines.append("| CONSENSUS HARD | CONSENSUS ∧ ≥1 HARD |")
    lines.append("| sell exit | 訊號日 D（EOD／20:10）→ **D+1 open**；無訊號 → H7 close |")
    lines.append("| hold scan | entry day 0 .. H7−1（含） |")
    lines.append("")
    lines.append("### Champion modes")
    lines.append("")
    lines.append("| sid | 名 | mode | hard | soft | core_n |")
    lines.append("|-----|----|------|------:|------:|-------:|")
    for sid in UNIVERSE:
        soft, hard = soft_hard(floors[sid])
        lines.append(
            f"| {sid} | {names[sid]} | {modes[sid]} | "
            f"≥{hard/1e8:.1f}億 | ≥{soft/1e8:.2f}億 | {len(cores[sid])} |"
        )
    lines.append("")
    lines.append("## Arms")
    lines.append("")
    lines.append("| arm | n | early% | ALL med | win | OOS n | OOS med | OOS win | ΔALL vs L1H7 | ΔOOS | reasons |")
    lines.append("|-----|--:|-------:|--------:|----:|------:|--------:|--------:|-------------:|------:|---------|")
    arm_order = [
        "L1H7",
        "L1H10",
        "CONS_HARD_NEXT_OPEN",
        "SINGLE_HARD_NEXT_OPEN",
        "CONS_SOFT_NEXT_OPEN",
        "CONS_HARD2_NEXT_OPEN",
    ]
    for k in arm_order:
        s = stats[k]
        reasons = ", ".join(f"{a}={b}" for a, b in sorted(s["exit_reasons"].items()))
        lines.append(
            f"| {k} | {s['n']} | {frac_pct(s['early_frac'])} | {pct(s['med'])} | "
            f"{win_pct(s['win'])} | {s['oos_n']} | {pct(s['oos_med'])} | "
            f"{win_pct(s['oos_win'])} | {pct(d_all(k))} | {pct(d_oos(k))} | {reasons} |"
        )
    lines.append("")
    lines.append("## Path analysis")
    lines.append("")
    lines.append("### CONSENSUS HARD fires vs never")
    lines.append("")
    lines.append(
        f"- Fired early：n={path['cons_hard_fired_n']} · "
        f"med {pct(path['fired_early']['med'])} · "
        f"win {win_pct(path['fired_early']['win'])} · "
        f"med hold_day {path['hold_day_med']}"
    )
    lines.append(
        f"- Never fire（→ H7）：n={path['cons_hard_never_n']} · "
        f"med {pct(path['never_h7']['med'])} · "
        f"win {win_pct(path['never_h7']['win'])}"
    )
    lines.append(
        f"- **Counterfactual**：same fired trades forced to H7 close · "
        f"med {pct(path['fired_cf_h7']['med'])} · "
        f"Δ(early − cf_H7) {pct(path['fired_delta_early_vs_cf'])}"
    )
    lines.append("")
    lines.append("### Other sell arms（fired only）")
    lines.append("")
    lines.append("| arm | fired n | early med | cf H7 med | Δ early−cf | never n | never med |")
    lines.append("|-----|--------:|----------:|----------:|-----------:|--------:|----------:|")
    for label, arm_name in (
        ("single_hard", "SINGLE_HARD_NEXT_OPEN"),
        ("cons_soft", "CONS_SOFT_NEXT_OPEN"),
    ):
        p = path_extra[label]
        lines.append(
            f"| {arm_name} | {p['fired_n']} | {pct(p['fired_early']['med'])} | "
            f"{pct(p['fired_cf_h7']['med'])} | {pct(p['delta_early_vs_cf'])} | "
            f"{p['never_n']} | {pct(p['never_h7']['med'])} |"
        )
    lines.append("")
    lines.append("## Ranking（sell arms · OOS med）")
    lines.append("")
    lines.append("| rank | arm | OOS med | ALL med | ΔOOS vs H7 |")
    lines.append("|-----:|-----|--------:|--------:|-----------:|")
    for i, k in enumerate(ranked_sell, 1):
        lines.append(
            f"| {i} | `{k}` | {pct(stats[k]['oos_med'])} | {pct(stats[k]['med'])} | "
            f"{pct(d_oos(k))} |"
        )
    lines.append("")
    lines.append("## Reading")
    lines.append("")
    lines.append(
        f"- **CONSENSUS HARD** early% {frac_pct(chs['early_frac'])}："
        f"portfolio-level ΔALL {pct(d_all(primary))} · ΔOOS {pct(d_oos(primary))}。"
    )
    if path["fired_delta_early_vs_cf"] is not None:
        if path["fired_delta_early_vs_cf"] < 0:
            lines.append(
                f"- 觸發後早出 **劣於** 同筆撐到 H7（Δ {pct(path['fired_delta_early_vs_cf'])}）"
                "→ 訊號日後續仍有超額，早出是 mistime。"
            )
        else:
            lines.append(
                f"- 觸發後早出 **優於** 同筆撐到 H7（Δ {pct(path['fired_delta_early_vs_cf'])}）"
                "→ 路徑上有避險價值，但仍需看組合層是否抬中位。"
            )
    lines.append(
        f"- Unconditional **L1H10** 仍是時間軸對照（ΔOOS {pct(d_oos('L1H10'))}）；"
        "對齊 H_GREEN_HOLD_EXIT：longer hold ≠ early sell。"
    )
    lines.append("")
    lines.append("## Freeze recommendation")
    lines.append("")
    if freeze:
        lines.append(
            f"**{freeze_note}** — CONSENSUS HARD next-open clears bar "
            f"(OOS Δ≥+{FREEZE_OOS_EDGE*100:.1f}pp · ALL Δ≥{FREEZE_ALL_FLOOR*100:.0f}pp · "
            f"OOS n≥{MIN_OOS_N})."
        )
    else:
        lines.append(
            f"**warning-only** — {freeze_note}. "
            "Do **not** auto-exit on 20:10 CONSENSUS HARD; keep holdings-branch-sell-monitor "
            "as observe／email. Hold SSOT remains **L1H7**."
        )
    lines.append("")
    lines.append("- 不寫 `config/strategy.yaml`／不進 Order。")
    lines.append("")
    lines.append("## Caveats")
    lines.append("")
    lines.append("- 單槽、無容量／重疊約束；sum \\(r_{adj}\\) 不可當複利權益。")
    lines.append("- 賣方訊號用收盤價×淨股數（與 monitor 一致）；實際 20:10 後才可觀測。")
    lines.append("- Next-open 出場簡化（無滑價）；若訊號落在 H7 當日，出場可能晚於 H7 close。")
    lines.append("- `CONS_HARD2`（≥2 HARD seats）為敏感度，非 monitor subject 定義。")
    lines.append("")
    lines.append("## Reproduction")
    lines.append("")
    lines.append("```bash")
    lines.append(
        "PYTHONPATH=src .venv/bin/python scripts/research/run_expert_sell_exit_study.py"
    )
    lines.append("```")
    lines.append("")

    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")

    payload = {
        "generated": now,
        "protocol": {
            "start": START,
            "end": END,
            "oos": OOS,
            "cost": COST,
            "beta": BETA,
            "dedupe": DEDUP,
            "universe": UNIVERSE,
            "soft_floor_min": SOFT_FLOOR_MIN,
            "exit": "next_open_after_sell_else_H7_close",
            "cons_hard_def": "n_soft>=2 and n_hard>=1",
        },
        "champions": {
            sid: {
                "name": names[sid],
                "mode": modes[sid],
                "floor": floors[sid],
                "soft": soft_hard(floors[sid])[0],
                "core_n": len(cores[sid]),
            }
            for sid in UNIVERSE
        },
        "stats": stats,
        "deltas_vs_l1h7": {
            k: {"all": d_all(k), "oos": d_oos(k)} for k in arm_order
        },
        "path_cons_hard": {
            "fired_n": path["cons_hard_fired_n"],
            "never_n": path["cons_hard_never_n"],
            "fired_early": path["fired_early"],
            "never_h7": path["never_h7"],
            "fired_cf_h7": path["fired_cf_h7"],
            "delta_early_vs_cf": path["fired_delta_early_vs_cf"],
            "hold_day_med": path["hold_day_med"],
        },
        "path_extra": path_extra,
        "freeze": freeze,
        "freeze_note": freeze_note,
        "primary_arm": primary,
    }
    OUT_JSON.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {OUT_MD}")
    print(f"wrote {OUT_JSON}")
    print(f"freeze={freeze} · {freeze_note}")
    print(
        f"CONS_HARD ΔALL={pct(d_all(primary))} ΔOOS={pct(d_oos(primary))} "
        f"fired_Δ(early−cf)={pct(path['fired_delta_early_vs_cf'])}"
    )


if __name__ == "__main__":
    main()
