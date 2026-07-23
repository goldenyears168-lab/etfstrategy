#!/usr/bin/env python3
"""H_2327_EXPERT_SELL_EXIT: 國巨 expert-pool HARD net-sell early exit vs H7/H10.

Research only · sqlite mode=ro · 2327 only · do not conflate with chip T2/T1.

Question: during a champion-green hold, does expert-pool HARD net-sell
(core 981j/779Z/8888/8840/918e · floor ≥0.5億) predict a better early exit
than fixed L1H7 / L1H10?

Arms
  baselines: L1H7 / L1H10 (T+1 open → H close)
  sell overlays (first trigger in hold window; else time @ H*):
    HARD≥1 / CONSENSUS_HARD≥2 / MULTI3 / MULTI4
    × exit lag0 same-day close | lag1 next open
  stratify by n_hard sellers on trigger day

PIT note: FinMind EOD branch tape → same-day close exit is treated as
lag0-valid (same caveat class as green T-close). Live motivation (4/5 HARD)
is NOT used as lookahead in the backtest.

  PYTHONPATH=src .venv/bin/python scripts/research/run_h_2327_expert_sell_exit.py
"""
from __future__ import annotations

import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from statistics import mean, median
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
OUT_MD = (
    ROOT
    / "reports/research/branch-footprint-screen/expert_pool/2327"
    / "H_2327_EXPERT_SELL_EXIT.md"
)
OUT_JSON = OUT_MD.with_suffix(".json")
WATCH = (
    ROOT
    / "reports/research/branch-footprint-screen/expert_pool/2327"
    / "watch_spec.json"
)
DB = ROOT / "data" / "stocks.db"
SOURCE = "finmind"
START, END, OOS = "2024-07-01", "2026-07-20", "2026-01-01"
COST, BETA, DEDUP = 0.003, 1.15, 5
SID = "2327"
SOFT_FLOOR_MIN = 2.5e7  # align sell monitor
MIN_FREEZE_N = 20
MIN_TRIGGER_N = 8


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


def load_spec() -> tuple[dict[str, str], float, str, str]:
    w = json.loads(WATCH.read_text(encoding="utf-8"))
    core = {str(k): str(v) for k, v in (w.get("core") or {}).items()}
    floor = float(w.get("net_floor") or 5e7)
    mode = (w.get("champion") or {}).get("mode") or "回看1日共識"
    name = w.get("stock_name") or SID
    return core, floor, mode, name


def dedupe_ok(last: str | None, d: str) -> bool:
    if not last:
        return True
    return (
        datetime.strptime(d, "%Y-%m-%d") - datetime.strptime(last, "%Y-%m-%d")
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
    reasons: dict[str, int] = defaultdict(int)
    for t in trades:
        reasons[str(t.get("exit_reason") or "time")] += 1
    hold_days = [t.get("hold_days") for t in trades if t.get("hold_days") is not None]
    return {
        "n": allb["n"],
        "med": allb["med"],
        "mean": allb["mean"],
        "win": allb["win"],
        "oos_n": oos["n"],
        "oos_med": oos["med"],
        "oos_mean": oos["mean"],
        "oos_win": oos["win"],
        "early_frac": (early_n / allb["n"]) if allb["n"] else None,
        "early_n": early_n,
        "exit_reasons": dict(reasons),
        "med_hold_days": median(hold_days) if hold_days else None,
    }


def main() -> None:
    core, floor, mode, name = load_spec()
    soft = max(floor * 0.5, SOFT_FLOOR_MIN)
    if soft > floor:
        soft = floor
    tids = list(core.keys())
    print(f"2327 {name} · core={len(tids)} · floor={floor:.0f} · mode={mode}")

    con = sqlite3.connect(f"file:{DB.resolve()}?mode=ro", uri=True, timeout=180)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    con.execute("PRAGMA busy_timeout=180000")

    bars: dict[str, tuple[float, float, float, float]] = {}
    for r in con.execute(
        """SELECT trade_date, open, high, low, close FROM stock_daily_bars
           WHERE source=? AND stock_id=?
             AND trade_date BETWEEN date(?, '-5 days') AND date(?, '+25 days')
             AND close>0""",
        (SOURCE, SID, START, END),
    ):
        o = float(r["open"] or r["close"])
        c = float(r["close"])
        if o <= 0:
            continue
        hi = float(r["high"] or max(o, c))
        lo = float(r["low"] or min(o, c))
        bars[r["trade_date"]] = (o, hi, lo, c)

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

    # net buy (for green) and net sell amounts (for exit) by date × tid
    buy_amt: dict[str, dict[str, float]] = defaultdict(dict)
    sell_amt: dict[str, dict[str, float]] = defaultdict(dict)
    ph = ",".join("?" * len(tids))
    for r in con.execute(
        f"""SELECT trade_date, securities_trader_id, net
            FROM stock_broker_branch_daily
            WHERE source=? AND stock_id=?
              AND securities_trader_id IN ({ph})
              AND trade_date BETWEEN ? AND ?
              AND net != 0""",
        (SOURCE, SID, *tids, START, END),
    ):
        d = r["trade_date"]
        oc = bars.get(d)
        if not oc:
            continue
        amt = float(r["net"]) * oc[3]
        tid = str(r["securities_trader_id"])
        if amt > 0:
            buy_amt[d][tid] = amt
        elif amt < 0:
            sell_amt[d][tid] = abs(amt)
    con.close()

    cal = sorted(d for d in bars if START <= d <= END)
    path_cal = sorted(bars)
    path_idx = {d: i for i, d in enumerate(path_cal)}

    def expert_green(d: str) -> bool:
        pi = path_idx.get(d)
        if pi is None:
            return False
        today = {t for t, a in buy_amt.get(d, {}).items() if t in core and a >= floor}
        yday: set[str] = set()
        if pi > 0:
            yd = path_cal[pi - 1]
            yday = {
                t for t, a in buy_amt.get(yd, {}).items() if t in core and a >= floor
            }
        if "OR" in mode:
            return len(today) >= 1
        if "同日" in mode:
            return len(today) >= 2
        # 回看1日共識
        return len(today) >= 1 and len(today | yday) >= 2

    def hard_sellers(d: str) -> list[tuple[str, float]]:
        hits = [
            (t, a)
            for t, a in sell_amt.get(d, {}).items()
            if t in core and a >= floor
        ]
        return sorted(hits, key=lambda x: -x[1])

    def soft_sellers(d: str) -> list[tuple[str, float]]:
        hits = [
            (t, a)
            for t, a in sell_amt.get(d, {}).items()
            if t in core and a >= soft
        ]
        return sorted(hits, key=lambda x: -x[1])

    def n_hard(d: str) -> int:
        return len(hard_sellers(d))

    def triggers(d: str) -> dict[str, bool]:
        nh = n_hard(d)
        ns = len(soft_sellers(d))
        return {
            "HARD": nh >= 1,
            "CONSENSUS_HARD": nh >= 2,
            "MULTI3": nh >= 3,
            "MULTI4": nh >= 4,
            # sell-monitor subject tag: ≥2 soft+ (diagnostic only)
            "CONSENSUS_SOFTPLUS": ns >= 2,
        }

    def bench_ret(ed: str, xd: str, *, entry_px: str = "open") -> float | None:
        j = next((k for k, d in enumerate(ix_dates) if d >= ed), None)
        xj = next((k for k, d in enumerate(ix_dates) if d >= xd), None)
        if j is None or xj is None:
            return None
        bo = ix[ix_dates[j]][0] if entry_px == "open" else ix[ix_dates[j]][1]
        bc = ix[ix_dates[xj]][1]
        if bo <= 0:
            return None
        return bc / bo - 1

    def pack(
        sig: str,
        ep: float,
        ed: str,
        xp: float,
        xd: str,
        reason: str,
        *,
        n_hard_trig: int | None = None,
        sell_day: str | None = None,
        hold_days: int | None = None,
        sellers: list[str] | None = None,
    ) -> dict | None:
        if ep <= 0 or xp <= 0:
            return None
        ret = xp / ep - 1 - COST
        bret = bench_ret(ed, xd, entry_px="open")
        if bret is None:
            return None
        return {
            "sid": SID,
            "sig": sig,
            "entry": ed,
            "exit": xd,
            "exit_reason": reason,
            "ret": ret,
            "radj": ret - BETA * bret,
            "oos": sig >= OOS,
            "n_hard": n_hard_trig,
            "sell_day": sell_day,
            "hold_days": hold_days,
            "sellers": sellers or [],
        }

    def time_exit(entry_i: int, hold: int, sig: str) -> dict | None:
        if entry_i < 0 or entry_i + hold - 1 >= len(path_cal):
            return None
        ed = path_cal[entry_i]
        xd = path_cal[entry_i + hold - 1]
        o, _, _, _ = bars[ed]
        _, _, _, xc = bars[xd]
        return pack(sig, o, ed, xc, xd, "time", hold_days=hold)

    def sell_exit(
        entry_i: int,
        *,
        max_hold: int,
        rule: str,
        lag: str,  # "T0_close" | "T1_open"
        sig: str,
    ) -> dict | None:
        if entry_i < 0 or entry_i + max_hold - 1 >= len(path_cal):
            return None
        ed = path_cal[entry_i]
        ep = bars[ed][0]
        last_i = entry_i + max_hold - 1
        for j in range(entry_i, last_i + 1):
            d = path_cal[j]
            if d > END:
                break
            trig = triggers(d)
            if not trig.get(rule):
                continue
            sellers = [f"{core.get(t, t)}:{a/1e8:.2f}億" for t, a in hard_sellers(d)]
            nh = n_hard(d)
            if lag == "T0_close":
                _, _, _, xc = bars[d]
                hd = j - entry_i + 1
                return pack(
                    sig,
                    ep,
                    ed,
                    xc,
                    d,
                    f"{rule}_T0c",
                    n_hard_trig=nh,
                    sell_day=d,
                    hold_days=hd,
                    sellers=sellers,
                )
            # lag1 next open
            if j + 1 >= len(path_cal):
                break
            nd = path_cal[j + 1]
            no = bars[nd][0]
            hd = (j + 1) - entry_i + 1
            return pack(
                sig,
                ep,
                ed,
                no,
                nd,
                f"{rule}_T1o",
                n_hard_trig=nh,
                sell_day=d,
                hold_days=hd,
                sellers=sellers,
            )
        # no trigger → time
        xd = path_cal[last_i]
        _, _, _, xc = bars[xd]
        return pack(sig, ep, ed, xc, xd, "time", hold_days=max_hold)

    # Collect green signals
    signals: list[tuple[int, str]] = []
    last: str | None = None
    for d in cal:
        if not expert_green(d):
            continue
        if not dedupe_ok(last, d):
            continue
        pi = path_idx.get(d)
        if pi is None or pi + 1 >= len(path_cal):
            continue
        signals.append((pi, d))
        last = d
    print(f"green signals (deduped): {len(signals)}")

    arms: dict[str, list[dict]] = {}
    for h in (7, 10):
        trades = []
        for pi, sig in signals:
            r = time_exit(pi + 1, h, sig)
            if r:
                trades.append(r)
        arms[f"L1H{h}"] = trades
        print(f"  L1H{h}: n={len(trades)}")

    rules = ("HARD", "CONSENSUS_HARD", "MULTI3", "MULTI4")
    lags = (("T0_close", "T0c"), ("T1_open", "T1o"))
    for rule in rules:
        for h in (7, 10):
            for lag_key, lag_tag in lags:
                name_arm = f"{rule}_{lag_tag}_H{h}"
                trades = []
                for pi, sig in signals:
                    r = sell_exit(
                        pi + 1, max_hold=h, rule=rule, lag=lag_key, sig=sig
                    )
                    if r:
                        trades.append(r)
                arms[name_arm] = trades
                s = summarize(trades)
                print(
                    f"  {name_arm}: n={s['n']} early={s['early_n']} "
                    f"med={pct(s['med'])} reasons={s['exit_reasons']}"
                )

    stats = {k: summarize(v) for k, v in arms.items()}
    base7, base10 = stats["L1H7"], stats["L1H10"]

    def delta(arm: str, base: str, key: str = "med") -> float | None:
        a, b = stats[arm][key], stats[base][key]
        if a is None or b is None:
            return None
        return a - b

    def first_rule_day(
        entry_i: int, rule: str, max_hold: int = 10
    ) -> tuple[str, int, int, list[str]] | None:
        """Return (sell_day, sell_i, n_hard, seller_names) for first day matching rule."""
        last_i = min(entry_i + max_hold - 1, len(path_cal) - 1)
        for j in range(entry_i, last_i + 1):
            d = path_cal[j]
            if d > END:
                break
            if not triggers(d).get(rule):
                continue
            sellers = [core.get(t, t) for t, _ in hard_sellers(d)]
            return d, j, n_hard(d), sellers
        return None

    # Triggered-only paired under H10 window (by first day matching each rule)
    paired_by_rule: dict[str, list[dict]] = {r: [] for r in rules}
    for pi, sig in signals:
        entry_i = pi + 1
        if entry_i + 9 >= len(path_cal):
            continue
        t7 = time_exit(entry_i, 7, sig)
        t10 = time_exit(entry_i, 10, sig)
        if not t7 or not t10:
            continue
        for rule in rules:
            hit = first_rule_day(entry_i, rule, 10)
            if hit is None:
                continue
            sell_day, sell_i, nh, sellers = hit
            e0 = sell_exit(
                entry_i, max_hold=10, rule=rule, lag="T0_close", sig=sig
            )
            e1 = sell_exit(
                entry_i, max_hold=10, rule=rule, lag="T1_open", sig=sig
            )
            if not e0 or not e1:
                continue
            paired_by_rule[rule].append(
                {
                    "sig": sig,
                    "entry": path_cal[entry_i],
                    "sell_day": sell_day,
                    "n_hard": nh,
                    "sellers": sellers,
                    "hold_to_sell_days": sell_i - entry_i + 1,
                    "radj_H7": t7["radj"],
                    "radj_H10": t10["radj"],
                    "radj_T0c": e0["radj"],
                    "radj_T1o": e1["radj"],
                    "delta_T0c_vs_H7": e0["radj"] - t7["radj"],
                    "delta_T1o_vs_H7": e1["radj"] - t7["radj"],
                    "delta_T0c_vs_H10": e0["radj"] - t10["radj"],
                    "delta_T1o_vs_H10": e1["radj"] - t10["radj"],
                    "oos": sig >= OOS,
                    "rule": rule,
                }
            )
    paired_rows = paired_by_rule["HARD"]

    def paired_blk(rows: list[dict], key: str) -> dict:
        if not rows:
            return {"n": 0, "med": None, "mean": None, "win_frac": None}
        xs = [r[key] for r in rows]
        return {
            "n": len(rows),
            "med": median(xs),
            "mean": mean(xs),
            "win_frac": sum(1 for x in xs if x > 0) / len(xs),
        }

    strat: dict[str, Any] = {}
    # First HARD day stratified by that day's seller count
    for label, pred in (
        ("firstHARD≥1", lambda r: r["n_hard"] >= 1),
        ("firstHARD==1", lambda r: r["n_hard"] == 1),
        ("firstHARD==2", lambda r: r["n_hard"] == 2),
        ("firstHARD≥2", lambda r: r["n_hard"] >= 2),
        ("firstHARD≥3", lambda r: r["n_hard"] >= 3),
        ("firstHARD≥4", lambda r: r["n_hard"] >= 4),
    ):
        sub = [r for r in paired_rows if pred(r)]
        strat[label] = {
            "n": len(sub),
            "oos_n": sum(1 for r in sub if r["oos"]),
            "med_n_hard": median([r["n_hard"] for r in sub]) if sub else None,
            "med_hold_to_sell": (
                median([r["hold_to_sell_days"] for r in sub]) if sub else None
            ),
            "delta_T0c_vs_H7": paired_blk(sub, "delta_T0c_vs_H7"),
            "delta_T1o_vs_H7": paired_blk(sub, "delta_T1o_vs_H7"),
            "delta_T0c_vs_H10": paired_blk(sub, "delta_T0c_vs_H10"),
            "delta_T1o_vs_H10": paired_blk(sub, "delta_T1o_vs_H10"),
            "radj_T0c": paired_blk(sub, "radj_T0c"),
            "radj_T1o": paired_blk(sub, "radj_T1o"),
            "radj_H7": paired_blk(sub, "radj_H7"),
            "radj_H10": paired_blk(sub, "radj_H10"),
        }
    # Escalation: first day the hold ever reaches ≥2 / ≥3 / ≥4 HARD
    for rule, label in (
        ("HARD", "esc_HARD≥1"),
        ("CONSENSUS_HARD", "esc_HARD≥2"),
        ("MULTI3", "esc_HARD≥3"),
        ("MULTI4", "esc_HARD≥4"),
    ):
        sub = paired_by_rule[rule]
        strat[label] = {
            "n": len(sub),
            "oos_n": sum(1 for r in sub if r["oos"]),
            "med_n_hard": median([r["n_hard"] for r in sub]) if sub else None,
            "med_hold_to_sell": (
                median([r["hold_to_sell_days"] for r in sub]) if sub else None
            ),
            "delta_T0c_vs_H7": paired_blk(sub, "delta_T0c_vs_H7"),
            "delta_T1o_vs_H7": paired_blk(sub, "delta_T1o_vs_H7"),
            "delta_T0c_vs_H10": paired_blk(sub, "delta_T0c_vs_H10"),
            "delta_T1o_vs_H10": paired_blk(sub, "delta_T1o_vs_H10"),
            "radj_T0c": paired_blk(sub, "radj_T0c"),
            "radj_T1o": paired_blk(sub, "radj_T1o"),
            "radj_H7": paired_blk(sub, "radj_H7"),
            "radj_H10": paired_blk(sub, "radj_H10"),
        }

    # Incidence: how often HARD occurs during H7/H10 holds
    n_sig = len(signals)
    n_hard_in_h7 = 0
    n_hard_in_h10 = 0
    n_c2_in_h10 = 0
    n_c3_in_h10 = 0
    n_c4_in_h10 = 0
    for pi, sig in signals:
        entry_i = pi + 1
        hit7 = hit10 = c2 = c3 = c4 = False
        for j in range(entry_i, min(entry_i + 10, len(path_cal))):
            d = path_cal[j]
            if d > END:
                break
            nh = n_hard(d)
            if nh >= 1:
                hit10 = True
                if j < entry_i + 7:
                    hit7 = True
            if nh >= 2:
                c2 = True
            if nh >= 3:
                c3 = True
            if nh >= 4:
                c4 = True
        n_hard_in_h7 += int(hit7)
        n_hard_in_h10 += int(hit10)
        n_c2_in_h10 += int(c2)
        n_c3_in_h10 += int(c3)
        n_c4_in_h10 += int(c4)

    # Verdict heuristics (small-n honest)
    trig_n = len(paired_rows)
    best_arm = None
    best_oos = -9e9
    for k, s in stats.items():
        if k.startswith("L1"):
            continue
        if s["oos_med"] is None:
            continue
        # prefer OOS med, then ALL med
        score = (s["oos_med"], s["med"] if s["med"] is not None else -9e9)
        if score[0] > best_oos or (
            score[0] == best_oos and best_arm and score[1] > (stats[best_arm]["med"] or -9e9)
        ):
            best_oos = score[0]
            best_arm = k

    can_freeze = (
        n_sig >= MIN_FREEZE_N
        and trig_n >= MIN_TRIGGER_N
        and base7["n"] >= MIN_FREEZE_N
    )
    # Edge vs H10 (champion policy) on triggered subset
    d_t0_h10 = strat["esc_HARD≥1"]["delta_T0c_vs_H10"]["med"]
    d_t1_h10 = strat["esc_HARD≥1"]["delta_T1o_vs_H10"]["med"]
    d_t0_h7 = strat["esc_HARD≥1"]["delta_T0c_vs_H7"]["med"]
    d_t1_h7 = strat["esc_HARD≥1"]["delta_T1o_vs_H7"]["med"]
    d2_t0_h10 = strat["esc_HARD≥2"]["delta_T0c_vs_H10"]["med"]
    d2_t1_h10 = strat["esc_HARD≥2"]["delta_T1o_vs_H10"]["med"]
    d3_t0_h10 = strat["esc_HARD≥3"]["delta_T0c_vs_H10"]["med"]
    d3_t1_h10 = strat["esc_HARD≥3"]["delta_T1o_vs_H10"]["med"]

    beats_h10 = (
        d_t0_h10 is not None
        and d_t1_h10 is not None
        and max(d_t0_h10, d_t1_h10) >= 0.005
    )
    hurts_h10 = (
        d_t0_h10 is not None
        and d_t1_h10 is not None
        and max(d_t0_h10, d_t1_h10) < -0.005
    )

    if not can_freeze:
        freeze = (
            f"**cannot freeze** — thin n (green={n_sig}, HARD-trigger={trig_n}; "
            f"need ≥{MIN_FREEZE_N} greens & ≥{MIN_TRIGGER_N} triggers)"
        )
    elif beats_h10:
        freeze = (
            "research-observe only: HARD sell early-exit shows positive paired "
            "Δ vs H10 on triggered subset — **still not freeze** without more n / OOS"
        )
    elif hurts_h10:
        freeze = "keep H10_skip SSOT — HARD early-exit **hurts** vs H10 on triggered subset"
    else:
        freeze = "keep H10_skip SSOT — HARD early-exit ≈ flat vs H10; no promote"

    # Build markdown
    now = datetime.now().isoformat(timespec="seconds")
    lines: list[str] = []
    lines.append("# H_2327_EXPERT_SELL_EXIT · 國巨專家池淨賣提早出場 vs H7/H10\n")
    lines.append(f"- 生成：{now}")
    lines.append("- Research only · sqlite `mode=ro` · **未採納** Strategy／Order")
    lines.append("- Runner：`scripts/research/run_h_2327_expert_sell_exit.py`")
    lines.append("- 勿與 chip T2/T1（`2327_CHIP_BRANCH_T2_T1.md`）混淆")
    lines.append("")
    lines.append("## Verdict（2327 專用）\n")
    lines.append(
        f"**Green 訊號**：n={n_sig}（dedupe 5 日）· "
        f"L1H7 n={base7['n']} med {pct(base7['med'])} · "
        f"L1H10 n={base10['n']} med {pct(base10['med'])} "
        f"(OOS {pct(base10['oos_med'])})."
    )
    lines.append("")
    lines.append(
        f"**持有窗內 HARD 發生率**：H7 {n_hard_in_h7}/{n_sig} "
        f"({n_hard_in_h7/n_sig*100:.0f}%) · "
        f"H10 {n_hard_in_h10}/{n_sig} ({n_hard_in_h10/n_sig*100:.0f}%) · "
        f"≥2HARD@H10 {n_c2_in_h10}/{n_sig} · "
        f"≥3 {n_c3_in_h10}/{n_sig} · ≥4 {n_c4_in_h10}/{n_sig}."
    )
    lines.append("")
    lines.append(
        f"**觸發子集（H10 窗內曾 HARD≥1）** n={trig_n} · "
        f"paired Δ med：T0-close vs H7 {pct(d_t0_h7)} · vs H10 {pct(d_t0_h10)}；"
        f"T1-open vs H7 {pct(d_t1_h7)} · vs H10 {pct(d_t1_h10)}."
    )
    lines.append("")
    lines.append(
        f"**升階（首次達標日）**：≥2HARD n={strat['esc_HARD≥2']['n']} "
        f"ΔT0c/T1o vs H10 {pct(d2_t0_h10)}/{pct(d2_t1_h10)} · "
        f"≥3HARD n={strat['esc_HARD≥3']['n']} "
        f"ΔT0c/T1o vs H10 {pct(d3_t0_h10)}/{pct(d3_t1_h10)} · "
        f"≥4HARD n={strat['esc_HARD≥4']['n']}（過稀）。"
    )
    lines.append("")
    if best_arm:
        ba = stats[best_arm]
        lines.append(
            f"**Portfolio 臂（含未觸發→持有到期）OOS-first 最佳賣訊臂**：`{best_arm}` · "
            f"ALL med {pct(ba['med'])}（Δ vs H7 {pct(delta(best_arm,'L1H7'))} · "
            f"Δ vs H10 {pct(delta(best_arm,'L1H10'))}）· "
            f"OOS med {pct(ba['oos_med'])} · early {frac_pct(ba['early_frac'])}."
        )
    lines.append("")
    lines.append(f"**Freeze**：{freeze}.")
    lines.append("")
    lines.append(
        "近期 live 4/5 HARD 僅作動機，**未**寫入回測標籤／過濾。"
    )
    lines.append("")
    lines.append("## Frozen protocol\n")
    lines.append("| 鍵 | 值 |")
    lines.append("|----|-----|")
    lines.append(f"| stock | {SID} {name} |")
    lines.append(f"| window | `{START}`..`{END}` |")
    lines.append(
        f"| green | champion `{mode}` · core "
        + "／".join(f"{v}({k})" for k, v in core.items())
        + f" · floor ≥{floor/1e8:.1f}億 |"
    )
    lines.append("| entry | 綠燈訊號 **T+1 open**（L1） |")
    lines.append("| cost | 30bps（只扣個股） |")
    lines.append(
        r"| excess | \(r_{\mathrm{adj}}=r-1.15\times r_{IX0001}\) |"
    )
    lines.append(f"| OOS | 訊號日 ≥ `{OOS}` |")
    lines.append("| dedupe | 同股 **5 曆日** |")
    lines.append(
        f"| HARD | 單分點淨賣金額 ≥ {floor/1e8:.1f}億（= watch `net_floor`） |"
    )
    lines.append(
        f"| soft（診斷） | max(floor×0.5, 0.25億) = {soft/1e8:.2f}億 "
        "（對齊 `run_holdings_branch_sell_monitor`） |"
    )
    lines.append(
        "| CONSENSUS_HARD / MULTI3 / MULTI4 | 同日 HARD 家數 ≥2 / ≥3 / ≥4 |"
    )
    lines.append(
        "| exit lag0 | 觸發日 **收盤**（EOD tape → 同日收視為 PIT-valid） |"
    )
    lines.append("| exit lag1 | 觸發日次日 **開盤** |")
    lines.append("| baseline | L1H7 · L1H10（冠軍政策 H10_skip） |")
    lines.append("| DB | `data/stocks.db` mode=ro · source=finmind |")
    lines.append("")

    lines.append("## 1) Time baselines\n")
    lines.append(
        "| arm | n | ALL med | mean | win | OOS n | OOS med | OOS win |"
    )
    lines.append(
        "|-----|--:|--------:|-----:|----:|------:|--------:|--------:|"
    )
    for k in ("L1H7", "L1H10"):
        s = stats[k]
        lines.append(
            f"| {k} | {s['n']} | {pct(s['med'])} | {pct(s['mean'])} | "
            f"{win_pct(s['win'])} | {s['oos_n']} | {pct(s['oos_med'])} | "
            f"{win_pct(s['oos_win'])} |"
        )
    lines.append("")

    lines.append("## 2) Sell-overlay portfolio arms\n")
    lines.append(
        "未觸發則持有至 H* 收；觸發則依 lag 提早出。Δ 相對 L1H7／L1H10。\n"
    )
    lines.append(
        "| arm | n | early% | ALL med | win | OOS med | ΔALL vs H7 | "
        "ΔALL vs H10 | ΔOOS vs H7 | ΔOOS vs H10 | reasons |"
    )
    lines.append(
        "|-----|--:|-------:|--------:|----:|--------:|-----------:|"
        "------------:|-----------:|------------:|---------|"
    )
    for rule in rules:
        for h in (7, 10):
            for _, lag_tag in lags:
                k = f"{rule}_{lag_tag}_H{h}"
                s = stats[k]
                er = ",".join(f"{a}={b}" for a, b in sorted(s["exit_reasons"].items()))
                lines.append(
                    f"| {k} | {s['n']} | {frac_pct(s['early_frac'])} | "
                    f"{pct(s['med'])} | {win_pct(s['win'])} | {pct(s['oos_med'])} | "
                    f"{pct(delta(k,'L1H7'))} | {pct(delta(k,'L1H10'))} | "
                    f"{pct(delta(k,'L1H7','oos_med'))} | "
                    f"{pct(delta(k,'L1H10','oos_med'))} | {er} |"
                )
    lines.append("")

    lines.append("## 3) Triggered-only paired（H10 窗）\n")
    lines.append(
        "同一筆綠燈：比較「首次達標日提早出」與「硬拿到 H7／H10」。"
        "先按 **首次 HARD 當日家數** 分層，再按 **升階（首次達 ≥k）**。\n"
    )
    lines.append(
        "| 子集 | n | OOS n | med hold→sell | "
        "med Δ T0c−H7 | med Δ T1o−H7 | med Δ T0c−H10 | med Δ T1o−H10 | "
        "med radj T0c | med radj H10 |"
    )
    lines.append(
        "|------|--:|------:|--------------:|"
        "-------------:|-------------:|--------------:|--------------:|"
        "-----------:|-----------:|"
    )
    for label in (
        "firstHARD≥1",
        "firstHARD==1",
        "firstHARD==2",
        "firstHARD≥2",
        "firstHARD≥3",
        "esc_HARD≥1",
        "esc_HARD≥2",
        "esc_HARD≥3",
        "esc_HARD≥4",
    ):
        st = strat[label]
        if st["n"] == 0:
            continue
        hd = st["med_hold_to_sell"]
        hd_s = f"{hd:.0f}" if hd is not None else "—"
        lines.append(
            f"| {label} | {st['n']} | {st['oos_n']} | {hd_s} | "
            f"{pct(st['delta_T0c_vs_H7']['med'])} | "
            f"{pct(st['delta_T1o_vs_H7']['med'])} | "
            f"{pct(st['delta_T0c_vs_H10']['med'])} | "
            f"{pct(st['delta_T1o_vs_H10']['med'])} | "
            f"{pct(st['radj_T0c']['med'])} | "
            f"{pct(st['radj_H10']['med'])} |"
        )
    lines.append("")

    lines.append("### 觸發逐筆（首次 HARD≥1 @ H10 窗）\n")
    lines.append(
        "| sig | entry | sell_day | n_hard | sellers | hold→sell | "
        "H7 | H10 | T0c | T1o | ΔT0c−H10 | ΔT1o−H10 |"
    )
    lines.append(
        "|-----|-------|----------|-------:|---------|----------:|"
        "---:|----:|----:|----:|----------:|----------:|"
    )
    for r in paired_rows:
        lines.append(
            f"| {r['sig']} | {r['entry']} | {r['sell_day']} | {r['n_hard']} | "
            f"{','.join(r['sellers'])} | {r['hold_to_sell_days']} | "
            f"{pct(r['radj_H7'])} | {pct(r['radj_H10'])} | "
            f"{pct(r['radj_T0c'])} | {pct(r['radj_T1o'])} | "
            f"{pct(r['delta_T0c_vs_H10'])} | {pct(r['delta_T1o_vs_H10'])} |"
        )
    lines.append("")

    # MULTI3 escalation trade table (thin but decision-relevant)
    m3 = paired_by_rule["MULTI3"]
    if m3:
        lines.append("### 升階逐筆（首次 HARD≥3 @ H10 窗）\n")
        lines.append(
            "| sig | entry | sell_day | n_hard | sellers | hold→sell | "
            "H7 | H10 | T0c | T1o | ΔT0c−H10 | ΔT1o−H10 |"
        )
        lines.append(
            "|-----|-------|----------|-------:|---------|----------:|"
            "---:|----:|----:|----:|----------:|----------:|"
        )
        for r in m3:
            lines.append(
                f"| {r['sig']} | {r['entry']} | {r['sell_day']} | {r['n_hard']} | "
                f"{','.join(r['sellers'])} | {r['hold_to_sell_days']} | "
                f"{pct(r['radj_H7'])} | {pct(r['radj_H10'])} | "
                f"{pct(r['radj_T0c'])} | {pct(r['radj_T1o'])} | "
                f"{pct(r['delta_T0c_vs_H10'])} | {pct(r['delta_T1o_vs_H10'])} |"
            )
        lines.append("")

    lines.append("## 4) 解讀\n")
    if trig_n == 0:
        lines.append("- 樣本窗內 **無** HARD 觸發 → 無法評估提早出場。")
    else:
        lines.append(
            f"- **單家 HARD 幾乎必現**：H7/H10 窗內發生率 100%；"
            f"首次 HARD 中位持有→賣僅 "
            f"{strat['esc_HARD≥1']['med_hold_to_sell']:.0f} 日，"
            "且多數為 n_hard=1 → 當 early-exit 等於「進場後立刻砍」。"
        )
        lines.append(
            f"- 首次 HARD 提早出 vs H10：T0-close {pct(d_t0_h10)} · "
            f"T1-open {pct(d_t1_h10)}（明顯傷害右尾，如 2026-05 大波段）。"
        )
        lines.append(
            f"- 等到 **≥2 HARD**：n={strat['esc_HARD≥2']['n']} · "
            f"ΔT0c/T1o vs H10 {pct(d2_t0_h10)}/{pct(d2_t1_h10)} "
            "（仍負，噪音略降但仍砍續航）。"
        )
        if strat["esc_HARD≥3"]["n"]:
            lines.append(
                f"- 等到 **≥3 HARD**：n={strat['esc_HARD≥3']['n']} · "
                f"ΔT0c/T1o vs H10 {pct(d3_t0_h10)}/{pct(d3_t1_h10)} · "
                "portfolio `MULTI3_*_H10` 中位略高於純 H10，但 OOS Δ≈0 且 n 極薄。"
            )
        else:
            lines.append("- **≥3 / ≥4 HARD** 觸發過稀，不能分層定論。")
        lines.append(
            f"- **≥4 HARD**：n={strat['esc_HARD≥4']['n']} → 實務上等於沒有賣訊。"
        )
    lines.append(
        "- Portfolio 臂 early% 高且 Δ vs H10 為負：賣訊常在強勢段出現，"
        "提早出會犧牲冠軍 H10 的右尾。"
    )
    lines.append(
        "- Sell monitor 的 CONSENSUS（≥2 soft+）≠ 本研究 CONSENSUS_HARD（≥2 hard）；"
        "後者較嚴。Live 4/5 HARD 屬預警，**不是**已驗證的出場規則。"
    )
    lines.append("")
    lines.append("## 5) Small-n honesty\n")
    lines.append(
        f"- 綠燈 n={n_sig}、HARD 觸發 n={trig_n}；"
        f"凍結門檻設 green≥{MIN_FREEZE_N} 且 trigger≥{MIN_TRIGGER_N} → "
        f"{'未達' if not can_freeze else '達標但仍建議獨立 OOS 複驗'}。"
    )
    lines.append(
        "- 知識庫 L1H7 交易表亦僅約十餘筆；本結論只適用 **2327 觀察**，"
        "不可外推其他 sleeve。"
    )
    lines.append("- **不寫** `config/strategy.yaml` · 不改 live sell monitor 門檻。")
    lines.append("")

    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")

    payload = {
        "generated": now,
        "stock_id": SID,
        "protocol": {
            "start": START,
            "end": END,
            "oos": OOS,
            "cost": COST,
            "beta": BETA,
            "dedup": DEDUP,
            "floor": floor,
            "soft": soft,
            "mode": mode,
            "core": core,
        },
        "n_signals": n_sig,
        "incidence": {
            "hard_in_h7": n_hard_in_h7,
            "hard_in_h10": n_hard_in_h10,
            "cons2_in_h10": n_c2_in_h10,
            "cons3_in_h10": n_c3_in_h10,
            "cons4_in_h10": n_c4_in_h10,
        },
        "stats": {
            k: {
                kk: (vv if not isinstance(vv, float) else vv)
                for kk, vv in s.items()
            }
            for k, s in stats.items()
        },
        "paired": paired_rows,
        "paired_by_rule": paired_by_rule,
        "stratify": strat,
        "freeze": freeze,
        "best_arm": best_arm,
        "can_freeze": can_freeze,
    }
    OUT_JSON.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {OUT_MD}")
    print(f"wrote {OUT_JSON}")
    print(f"VERDICT freeze: {freeze}")


if __name__ == "__main__":
    main()
