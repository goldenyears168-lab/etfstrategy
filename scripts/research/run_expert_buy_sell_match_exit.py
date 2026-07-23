#!/usr/bin/env python3
"""H_EXPERT_BUY_SELL_MATCH: match green buy amounts vs later expert sells.

Research only · mode=ro. After champion green → L1 open, track the same
core seats that fired the buy (net≥floor on signal window) and accumulate
their subsequent net-sells. Ask: does 「賣出≥買入」 (full unwind) beat
holding to H7, vs partial (50%)?

Product intent: buy email stores seat amounts; sell email shows
cum_sell / buy_book — partial vs full.

  PYTHONPATH=src .venv/bin/python scripts/research/run_expert_buy_sell_match_exit.py
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
    / "H_EXPERT_BUY_SELL_MATCH.md"
)
OUT_JSON = OUT_MD.with_suffix(".json")
DB = ROOT / "data" / "stocks.db"
SOURCE = "finmind"
START, END, OOS = "2024-07-01", "2026-07-17", "2026-01-01"
COST, BETA, DEDUP = 0.003, 1.15, 5
H7, H10 = 7, 10

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
        "epw_bsm", ROOT / "scripts/research/run_expert_pool_watch.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["epw_bsm"] = mod
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


def blk(xs: list[float]) -> dict:
    if not xs:
        return {"n": 0, "med": None, "mean": None, "win": None}
    return {
        "n": len(xs),
        "med": median(xs),
        "mean": mean(xs),
        "win": sum(1 for x in xs if x > 0) / len(xs),
    }


def summarize(trades: list[dict]) -> dict:
    allb = blk([t["radj"] for t in trades])
    oos = blk([t["radj"] for t in trades if t["oos"]])
    early_n = sum(1 for t in trades if t.get("exit_reason") != "time")
    return {
        "n": allb["n"],
        "med": allb["med"],
        "mean": allb["mean"],
        "win": allb["win"],
        "oos_n": oos["n"],
        "oos_med": oos["med"],
        "oos_win": oos["win"],
        "early_frac": (early_n / allb["n"]) if allb["n"] else None,
    }


def pct(x: float | None) -> str:
    return "—" if x is None else f"{x * 100:+.2f}%"


def win_pct(x: float | None) -> str:
    return "—" if x is None else f"{x * 100:.0f}%"


def yi(x: float) -> str:
    a = abs(x)
    if a >= 1e8:
        return f"{x / 1e8:+.2f}億"
    return f"{x / 1e4:+.0f}萬"


def main() -> None:
    mod = _load_watch_module()
    cores, floors, modes, names = load_champions(mod)

    con = sqlite3.connect(f"file:{DB.resolve()}?mode=ro", uri=True, timeout=120)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
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
    con.close()

    path_cals = {sid: sorted(bars[sid]) for sid in UNIVERSE}
    path_idx = {sid: {d: i for i, d in enumerate(path_cals[sid])} for sid in UNIVERSE}
    cals = {
        sid: sorted(d for d in bars[sid] if START <= d <= END) for sid in UNIVERSE
    }

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

    def buy_book(sid: str, sig: str) -> dict[str, float]:
        """Hit seats on green window: max(0, net) ≥ floor, sum per tid across T (and T-1 if lookback)."""
        floor = floors[sid]
        mode = modes[sid]
        pi = path_idx[sid].get(sig)
        if pi is None:
            return {}
        days = [sig]
        if "同日" not in mode and "OR" not in mode and pi > 0:
            days = [path_cals[sid][pi - 1], sig]
        elif "OR" in mode:
            days = [sig]
        book: dict[str, float] = defaultdict(float)
        for d in days:
            for tid, a in day_core_amts(sid, d).items():
                if a >= floor:
                    book[tid] += a
        return dict(book)

    def bench_ret(ed: str, xd: str, *, exit_px: str = "close") -> float | None:
        j = next((k for k, d in enumerate(ix_dates) if d >= ed), None)
        xj = next((k for k, d in enumerate(ix_dates) if d >= xd), None)
        if j is None or xj is None:
            return None
        bo = ix[ix_dates[j]][0]
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
        meta: dict | None = None,
    ) -> dict | None:
        if ep <= 0 or xp <= 0:
            return None
        ret = xp / ep - 1 - COST
        bret = bench_ret(ed, xd, exit_px="open" if reason != "time" else "close")
        if bret is None:
            return None
        radj = ret - BETA * bret
        row = {
            "sid": sid,
            "sig": sig,
            "entry": ed,
            "exit": xd,
            "ret": ret,
            "radj": radj,
            "oos": sig >= OOS,
            "exit_reason": reason,
        }
        if meta:
            row.update(meta)
        return row

    def time_exit(sid: str, entry_i: int, hold: int) -> dict | None:
        cal = path_cals[sid]
        if entry_i < 0 or entry_i + hold - 1 >= len(cal):
            return None
        ed = cal[entry_i]
        xd = cal[entry_i + hold - 1]
        ep, _ = bars[sid][ed]
        _, xp = bars[sid][xd]
        # sig passed via closure — filled by caller
        return None  # placeholder replaced below

    # Collect signals
    signals: list[tuple[str, str]] = []
    last: dict[str, str] = {}
    for sid in UNIVERSE:
        for d in cals[sid]:
            if not expert_green(sid, d):
                continue
            if not dedupe_ok(last, sid, d):
                continue
            last[sid] = d
            signals.append((sid, d))
    print(f"green signals: {len(signals)}")

    def trade_pack(
        sid: str,
        sig: str,
        entry_i: int,
        exit_i: int,
        reason: str,
        *,
        exit_open: bool,
        meta: dict,
    ) -> dict | None:
        cal = path_cals[sid]
        ed = cal[entry_i]
        xd = cal[exit_i]
        ep = bars[sid][ed][0]
        xp = bars[sid][xd][0] if exit_open else bars[sid][xd][1]
        return pack(sid, sig, ep, ed, xp, xd, reason, meta=meta)

    arms: dict[str, list[dict]] = {
        "L1H7": [],
        "L1H10": [],
        "MATCH50_NEXT_OPEN": [],  # cum_sell/buy ≥ 0.5
        "MATCH80_NEXT_OPEN": [],
        "MATCH100_NEXT_OPEN": [],  # full unwind
        "MATCH120_NEXT_OPEN": [],  # oversell
        "MATCH150_NEXT_OPEN": [],
        "MATCH170_NEXT_OPEN": [],
        "MATCH200_NEXT_OPEN": [],
        "SEATS_ALL_COVERED": [],  # every buy seat sell≥buy
        "SEATS_ALL_COVERED_AND_100": [],
    }
    path_rows: list[dict] = []  # diagnostics per trade through H7

    for sid, sig in signals:
        pi = path_idx[sid].get(sig)
        if pi is None or pi + 1 >= len(path_cals[sid]):
            continue
        entry_i = pi + 1  # T+1 open
        book = buy_book(sid, sig)
        buy_total = sum(book.values())
        if buy_total <= 0 or not book:
            continue

        # Baselines
        for hold, key in ((H7, "L1H7"), (H10, "L1H10")):
            if entry_i + hold - 1 >= len(path_cals[sid]):
                continue
            t = trade_pack(
                sid,
                sig,
                entry_i,
                entry_i + hold - 1,
                "time",
                exit_open=False,
                meta={
                    "buy_total": buy_total,
                    "n_buy_seats": len(book),
                    "hold_day": hold - 1,
                },
            )
            if t:
                arms[key].append(t)

        # Scan hold for match ratios
        last_i = min(entry_i + H7 - 1, len(path_cals[sid]) - 1)
        sell_cum_by: dict[str, float] = defaultdict(float)
        sell_cum = 0.0
        fired: dict[str, tuple[int, float, float, int]] = {}
        # name -> (exit_i, ratio, seats_covered_frac, hold_day)

        for j in range(entry_i, last_i + 1):
            d = path_cals[sid][j]
            amts = day_core_amts(sid, d)
            for tid in book:
                a = amts.get(tid, 0.0)
                if a < 0:
                    sell_cum_by[tid] += -a
                    sell_cum += -a
            ratio = sell_cum / buy_total
            seats_cov = sum(
                1 for tid, b in book.items() if sell_cum_by[tid] >= b - 1e-6
            )
            seats_frac = seats_cov / len(book)
            hold_day = j - entry_i

            for thr, arm in (
                (0.5, "MATCH50_NEXT_OPEN"),
                (0.8, "MATCH80_NEXT_OPEN"),
                (1.0, "MATCH100_NEXT_OPEN"),
                (1.2, "MATCH120_NEXT_OPEN"),
                (1.5, "MATCH150_NEXT_OPEN"),
                (1.7, "MATCH170_NEXT_OPEN"),
                (2.0, "MATCH200_NEXT_OPEN"),
            ):
                if arm not in fired and ratio >= thr:
                    # exit next open if possible
                    if j + 1 < len(path_cals[sid]):
                        fired[arm] = (j + 1, ratio, seats_frac, hold_day)
                    else:
                        fired[arm] = (j, ratio, seats_frac, hold_day)

            if "SEATS_ALL_COVERED" not in fired and seats_frac >= 1.0 - 1e-9:
                if j + 1 < len(path_cals[sid]):
                    fired["SEATS_ALL_COVERED"] = (j + 1, ratio, seats_frac, hold_day)
                else:
                    fired["SEATS_ALL_COVERED"] = (j, ratio, seats_frac, hold_day)

            if (
                "SEATS_ALL_COVERED_AND_100" not in fired
                and seats_frac >= 1.0 - 1e-9
                and ratio >= 1.0
            ):
                if j + 1 < len(path_cals[sid]):
                    fired["SEATS_ALL_COVERED_AND_100"] = (
                        j + 1,
                        ratio,
                        seats_frac,
                        hold_day,
                    )
                else:
                    fired["SEATS_ALL_COVERED_AND_100"] = (
                        j,
                        ratio,
                        seats_frac,
                        hold_day,
                    )

        # Path snapshot at H7 end
        path_rows.append(
            {
                "sid": sid,
                "sig": sig,
                "buy_total": buy_total,
                "n_buy_seats": len(book),
                "ratio_at_h7": sell_cum / buy_total,
                "seats_frac_at_h7": (
                    sum(1 for tid, b in book.items() if sell_cum_by[tid] >= b - 1e-6)
                    / len(book)
                ),
                "ever_ratio_ge_1": sell_cum / buy_total >= 1.0,
                "ever_all_seats": all(
                    sell_cum_by[tid] >= b - 1e-6 for tid, b in book.items()
                ),
                "oos": sig >= OOS,
            }
        )

        for arm in (
            "MATCH50_NEXT_OPEN",
            "MATCH80_NEXT_OPEN",
            "MATCH100_NEXT_OPEN",
            "MATCH120_NEXT_OPEN",
            "MATCH150_NEXT_OPEN",
            "MATCH170_NEXT_OPEN",
            "MATCH200_NEXT_OPEN",
            "SEATS_ALL_COVERED",
            "SEATS_ALL_COVERED_AND_100",
        ):
            if arm in fired:
                exit_i, ratio, seats_frac, hold_day = fired[arm]
                t = trade_pack(
                    sid,
                    sig,
                    entry_i,
                    exit_i,
                    arm,
                    exit_open=True,
                    meta={
                        "buy_total": buy_total,
                        "n_buy_seats": len(book),
                        "ratio_at_exit": ratio,
                        "seats_frac": seats_frac,
                        "hold_day": hold_day,
                    },
                )
            else:
                # fall through to H7 close
                if entry_i + H7 - 1 >= len(path_cals[sid]):
                    continue
                t = trade_pack(
                    sid,
                    sig,
                    entry_i,
                    entry_i + H7 - 1,
                    "time",
                    exit_open=False,
                    meta={
                        "buy_total": buy_total,
                        "n_buy_seats": len(book),
                        "ratio_at_exit": sell_cum / buy_total,
                        "seats_frac": path_rows[-1]["seats_frac_at_h7"],
                        "hold_day": H7 - 1,
                    },
                )
            if t:
                arms[arm].append(t)

    # Summaries
    summaries = {k: summarize(v) for k, v in arms.items()}
    h7 = summaries["L1H7"]

    def delta(arm: str) -> tuple[float | None, float | None]:
        s = summaries[arm]
        d_all = (
            None
            if s["med"] is None or h7["med"] is None
            else s["med"] - h7["med"]
        )
        d_oos = (
            None
            if s["oos_med"] is None or h7["oos_med"] is None
            else s["oos_med"] - h7["oos_med"]
        )
        return d_all, d_oos

    # Path distribution
    ratios = [p["ratio_at_h7"] for p in path_rows]
    frac_full = sum(1 for p in path_rows if p["ever_ratio_ge_1"]) / max(len(path_rows), 1)
    frac_seats = sum(1 for p in path_rows if p["ever_all_seats"]) / max(
        len(path_rows), 1
    )
    med_ratio_h7 = median(ratios) if ratios else None
    # among those who hit ratio≥1, paired early vs H7
    full_early = [t for t in arms["MATCH100_NEXT_OPEN"] if t["exit_reason"] != "time"]
    paired_deltas: list[float] = []
    h7_by = {(t["sid"], t["sig"]): t for t in arms["L1H7"]}
    for t in full_early:
        base = h7_by.get((t["sid"], t["sig"]))
        if base:
            paired_deltas.append(t["radj"] - base["radj"])

    print("\n=== arms ===")
    for k, s in summaries.items():
        d_all, d_oos = delta(k)
        print(
            f"{k}: n={s['n']} med={pct(s['med'])} oos={pct(s['oos_med'])} "
            f"early={s['early_frac']} Δall={pct(d_all)} Δoos={pct(d_oos)}"
        )
    print(
        f"path: n={len(path_rows)} med_ratio@H7={med_ratio_h7} "
        f"ever≥100%={frac_full:.0%} ever_all_seats={frac_seats:.0%}"
    )
    if paired_deltas:
        print(
            f"MATCH100 fired paired Δ vs H7: n={len(paired_deltas)} "
            f"med={pct(median(paired_deltas))}"
        )

    # Verdict + report
    def fp(x: float | None) -> str:
        return "—" if x is None else f"{x * 100:.0f}%"

    d100_all, d100_oos = delta("MATCH100_NEXT_OPEN")
    beats = (
        d100_oos is not None
        and d100_oos >= 0.005
        and (d100_all is None or d100_all >= -0.01)
    )
    verdict = (
        "「賣≥買」全出清提早出 **不贏** L1H7 — 維持預警；買訊金額應對仍值得在郵件顯示。"
        if not beats
        else "MATCH100 有 OOS 優勢 — 候選凍結（需人工複核）。"
    )

    buckets = [(0, 0.5, "<50%"), (0.5, 1.0, "50–99%"), (1.0, 9.0, "≥100%")]
    bucket_lines = []
    for lo, hi, lab in buckets:
        ids = {
            (p["sid"], p["sig"])
            for p in path_rows
            if lo <= p["ratio_at_h7"] < hi
        }
        ts = [h7_by[k]["radj"] for k in ids if k in h7_by]
        bucket_lines.append(
            f"| {lab} | {len(ts)} | {pct(median(ts) if ts else None)} |"
        )

    arm_order = (
        "L1H7",
        "L1H10",
        "MATCH50_NEXT_OPEN",
        "MATCH80_NEXT_OPEN",
        "MATCH100_NEXT_OPEN",
        "MATCH120_NEXT_OPEN",
        "MATCH150_NEXT_OPEN",
        "MATCH170_NEXT_OPEN",
        "MATCH200_NEXT_OPEN",
        "SEATS_ALL_COVERED",
        "SEATS_ALL_COVERED_AND_100",
    )
    arm_lines = []
    for arm in arm_order:
        s = summaries[arm]
        d_all, d_oos = delta(arm)
        arm_lines.append(
            f"| {arm} | {s['n']} | {fp(s['early_frac'])} | "
            f"{pct(s['med'])} | {win_pct(s['win'])} | {s['oos_n']} | "
            f"{pct(s['oos_med'])} | {pct(d_all)} | {pct(d_oos)} |"
        )

    ratio_h7_s = (
        f"{(med_ratio_h7 or 0) * 100:.0f}%" if med_ratio_h7 is not None else "—"
    )
    final = [
        "# H_EXPERT_BUY_SELL_MATCH · 買訊金額 vs 後續專家淨賣",
        "",
        f"- 生成：{datetime.now().isoformat(timespec='seconds')}",
        "- Research only · sqlite `mode=ro` · **未採納** Strategy／Order",
        "- Runner：`scripts/research/run_expert_buy_sell_match_exit.py`",
        "",
        "## Verdict",
        "",
        f"**{verdict}**",
        "",
        "| 指標 | 值 |",
        "|------|-----|",
        f"| L1H7 med / OOS | {pct(h7['med'])} / {pct(h7['oos_med'])} |",
        f"| MATCH100 med / OOS / ΔOOS | {pct(summaries['MATCH100_NEXT_OPEN']['med'])} / "
        f"{pct(summaries['MATCH100_NEXT_OPEN']['oos_med'])} / {pct(d100_oos)} |",
        f"| MATCH100 early% | {fp(summaries['MATCH100_NEXT_OPEN']['early_frac'])} |",
        f"| @H7 中位 賣／買 | {ratio_h7_s} |",
        f"| 窗內曾達賣≥買 (100%) | {frac_full:.0%} |",
        f"| 窗內每席都賣滿 | {frac_seats:.0%} |",
        "",
    ]
    if paired_deltas:
        final += [
            f"MATCH100 觸發配對：早出−H7 med Δ **{pct(median(paired_deltas))}** "
            f"(n={len(paired_deltas)})。",
            "",
        ]
    final += [
        "## 假說與設計",
        "",
        "買訊（綠燈）記錄觸發席位（core·net≥floor，回看含 T−1）淨買＝`buy_book`；",
        "持有期只累計**同一批席位**淨賣。`ratio=cum_sell/buy_total`；",
        "`seats_all_covered`＝每席各自賣≥該席買。",
        "",
        "**產品**：買信記數字、賣信對照 ratio（部分了結 vs 疑似出清）· 非自動賣。",
        "",
        "## Frozen protocol",
        "",
        "| 鍵 | 值 |",
        "|----|-----|",
        f"| window | `{START}`..`{END}` |",
        f"| universe | thick-14 |",
        "| green / entry | champion · T+1 open |",
        f"| cost / β / OOS / dedupe | {COST} / {BETA} / ≥`{OOS}` / {DEDUP}d |",
        "| exit | 達檻 D → D+1 open；否則 H7 close |",
        "",
        "## Arms",
        "",
        "| arm | n | early% | ALL med | win | OOS n | OOS med | ΔALL vs H7 | ΔOOS |",
        "|-----|--:|-------:|--------:|----:|------:|--------:|-----------:|------:|",
        *arm_lines,
        "",
        "## Path（抱滿 H7 時賣／買走到哪）",
        "",
        f"- 樣本：{len(path_rows)} · @H7 中位 ratio **{ratio_h7_s}**",
        f"- 窗內曾 ratio≥100%：**{frac_full:.0%}** · 每席賣滿：**{frac_seats:.0%}**",
        "",
        "### @H7 ratio 分桶 vs 該筆 L1H7 r_adj",
        "",
        "| @H7 ratio | n | med r_adj |",
        "|-----------|--:|----------:|",
        *bucket_lines,
        "",
        "## Reading",
        "",
        "1. @H7 中位 ratio 常 **≥100%** → 持有週內帳面常已「賣完買訊量」，但價格超額多在之後才完整實現。",
        "2. MATCH100 早出仍輸 H7（配對亦負）→ **出清買訊量 ≠ 該砍倉**。",
        "3. 愈早達標（MATCH50）愈傷；愈嚴（120／每席滿）愈接近 H7，仍非優勝。",
        "4. 郵件對照數字有資訊價值；出場 SSOT 仍 L1H7。",
        "",
        "## Freeze",
        "",
        "- Hold SSOT：**L1H7**",
        "- 買／賣金額對照：可接 20:00／20:10 註記 · **非**出場規則",
        "- MATCH*：**不凍結**",
        "",
        "## Reproduce",
        "",
        "```bash",
        "PYTHONPATH=src .venv/bin/python scripts/research/run_expert_buy_sell_match_exit.py",
        "```",
        "",
    ]
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.write_text("\n".join(final) + "\n", encoding="utf-8")
    payload = {
        "verdict": verdict,
        "summaries": {
            k: {
                kk: (float(vv) if isinstance(vv, float) else vv)
                for kk, vv in s.items()
            }
            for k, s in summaries.items()
        },
        "path": {
            "n": len(path_rows),
            "med_ratio_at_h7": med_ratio_h7,
            "frac_ever_ratio_ge_1": frac_full,
            "frac_ever_all_seats": frac_seats,
            "paired_match100_delta_med": (
                median(paired_deltas) if paired_deltas else None
            ),
            "paired_n": len(paired_deltas),
        },
        "generated": datetime.now().isoformat(timespec="seconds"),
    }
    OUT_JSON.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"wrote {OUT_MD}")
    print(f"wrote {OUT_JSON}")


if __name__ == "__main__":
    main()
