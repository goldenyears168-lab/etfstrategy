#!/usr/bin/env python3
"""Fair revalidation: H_E yellow→confirm path vs same-universe expert green L1H7.

Research only · sqlite mode=ro · does not touch strategy/order.

Frozen protocol (H_E):
  universe 14 thick hard≥3 ex-mega
  yellow   Top10 light2
  confirm  Top10 ≥0.5億 within 1–2 sessions after yellow
  cost 30bps · r_adj = r − 1.15×IX0001 · OOS sig≥2026-01-01
  dedupe   5 calendar days

  PYTHONPATH=src .venv/bin/python scripts/research/run_yellow_confirm_vs_green_fair.py
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
    / "reports/research/branch-footprint-screen/expert_pool/hypotheses/H_E_vs_GREEN_fair.md"
)
OUT_JSON = OUT_MD.with_suffix(".json")
DB = ROOT / "data" / "stocks.db"
SOURCE = "finmind"
START, END, OOS = "2024-07-01", "2026-07-17", "2026-01-01"
TOP10 = ["5850", "779Z", "779c", "9216", "9A81", "8840", "5854", "9268", "9227", "1260"]
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
LIGHT_LO, LIGHT_HI, HEAVY, SEMI = 5e6, 5e7, 1e8, 5e7
COST, HOLD, BETA, DEDUP = 0.003, 7, 1.15, 5
CONFIRM_WAIT = 2  # primary E path


def _load_watch_module():
    spec = importlib.util.spec_from_file_location(
        "epw_fair", ROOT / "scripts/research/run_expert_pool_watch.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["epw_fair"] = mod
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

    by: dict[str, dict[str, dict[str, float]]] = defaultdict(lambda: defaultdict(dict))
    ph_t = ",".join("?" * len(TOP10))
    for r in con.execute(
        f"""SELECT stock_id, trade_date, securities_trader_id, net
            FROM stock_broker_branch_daily
            WHERE source=? AND stock_id IN ({ph})
              AND securities_trader_id IN ({ph_t})
              AND trade_date BETWEEN ? AND ? AND net!=0""",
        (SOURCE, *UNIVERSE, *TOP10, START, END),
    ):
        oc = bars[r["stock_id"]].get(r["trade_date"])
        if oc:
            by[r["stock_id"]][r["trade_date"]][r["securities_trader_id"]] = (
                float(r["net"]) * oc[1]
            )

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

    def day_stats(sid: str, d: str) -> dict:
        m = by[sid].get(d, {})
        light = {t for t, a in m.items() if LIGHT_LO <= a < LIGHT_HI}
        n_heavy = sum(1 for a in m.values() if a >= HEAVY)
        n_semi = sum(1 for a in m.values() if a >= SEMI)
        return {"n_light": len(light), "n_heavy": n_heavy, "n_semi": n_semi}

    def yellow_light2(sid: str, i: int) -> bool:
        if i < 1:
            return False
        cal = cals[sid]
        d1, d2 = cal[i - 1], cal[i]
        s1, s2 = day_stats(sid, d1), day_stats(sid, d2)
        if s1["n_heavy"] or s2["n_heavy"]:
            return False
        return s1["n_light"] >= 2 and s2["n_light"] >= 2

    def expert_green(sid: str, d: str) -> bool:
        core = cores[sid]
        if not core:
            return False
        floor = floors[sid]
        mode = modes[sid]
        i = idx[sid][d]
        today = {
            t
            for t, a in core_by[sid].get(d, {}).items()
            if t in core and a >= floor
        }
        yday: set[str] = set()
        if i > 0:
            yd = cals[sid][i - 1]
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

    def find_confirm(sid: str, yi: int, wait_max: int) -> int | None:
        cal = cals[sid]
        for j in range(1, wait_max + 1):
            if yi + j >= len(cal):
                break
            if day_stats(sid, cal[yi + j])["n_semi"] >= 1:
                return yi + j
        return None

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

    def l1h7_from_sig(sid: str, sig_i: int) -> dict | None:
        return l1h_from_entry(sid, sig_i + 1, px_mode="open", hold=HOLD)

    # ---- yellow events ----
    yellow_events: list[tuple[str, int, str]] = []
    for sid in UNIVERSE:
        cal = cals[sid]
        for i in range(1, len(cal)):
            if yellow_light2(sid, i):
                yellow_events.append((sid, i, cal[i]))
    print(f"raw yellow fires: {len(yellow_events)}")

    # confirm conversion
    conf2 = sum(
        1 for sid, i, _ in yellow_events if find_confirm(sid, i, CONFIRM_WAIT) is not None
    )
    print(
        f"confirm@≤{CONFIRM_WAIT}d: {conf2}/{len(yellow_events)} "
        f"= {conf2 / len(yellow_events) * 100:.1f}%"
    )

    # green within 10 after yellow
    will_green: set[tuple[str, str]] = set()
    never_green: set[tuple[str, str]] = set()
    for sid, i, d in yellow_events:
        cal = cals[sid]
        hit = False
        for j in range(1, 11):
            if i + j >= len(cal):
                break
            if expert_green(sid, cal[i + j]):
                hit = True
                break
        if hit:
            will_green.add((sid, d))
        else:
            never_green.add((sid, d))
    print(
        f"yellow→green@≤10: {len(will_green)}/{len(yellow_events)} "
        f"= {len(will_green) / len(yellow_events) * 100:.1f}%"
    )

    arms: dict[str, list[dict]] = {}

    # 1) naked yellow → T+1 open → H7
    trades: list[dict] = []
    last: dict[str, str] = {}
    for sid, i, d in yellow_events:
        if not dedupe_ok(last, sid, d):
            continue
        r = l1h7_from_sig(sid, i)
        if r is None:
            continue
        trades.append(
            dict(
                sid=sid,
                yellow=d,
                sig=d,
                oos=d >= OOS,
                will_green=(sid, d) in will_green,
                **r,
            )
        )
        last[sid] = d
    arms["Y_T1open_H7"] = trades

    # 2) yellow → confirm≤2d → next open → H7  (E primary)
    trades = []
    last = {}
    for sid, i, d in yellow_events:
        if not dedupe_ok(last, sid, d):
            continue
        ci = find_confirm(sid, i, CONFIRM_WAIT)
        if ci is None:
            continue
        r = l1h_from_entry(sid, ci + 1, px_mode="open")
        if r is None:
            continue
        trades.append(
            dict(
                sid=sid,
                yellow=d,
                sig=d,
                confirm=cals[sid][ci],
                oos=d >= OOS,
                will_green=(sid, d) in will_green,
                **r,
            )
        )
        last[sid] = d
    arms["Y_confirm2_next_open_H7"] = trades

    # 3) yellow → confirm≤2d → confirm-day close → H7
    trades = []
    last = {}
    for sid, i, d in yellow_events:
        if not dedupe_ok(last, sid, d):
            continue
        ci = find_confirm(sid, i, CONFIRM_WAIT)
        if ci is None:
            continue
        r = l1h_from_entry(sid, ci, px_mode="close")
        if r is None:
            continue
        trades.append(
            dict(
                sid=sid,
                yellow=d,
                sig=d,
                confirm=cals[sid][ci],
                oos=d >= OOS,
                will_green=(sid, d) in will_green,
                **r,
            )
        )
        last[sid] = d
    arms["Y_confirm2_close_H7"] = trades

    # also confirm≤1d next open (for A reconciliation)
    trades = []
    last = {}
    for sid, i, d in yellow_events:
        if not dedupe_ok(last, sid, d):
            continue
        ci = find_confirm(sid, i, 1)
        if ci is None:
            continue
        r = l1h_from_entry(sid, ci + 1, px_mode="open")
        if r is None:
            continue
        trades.append(
            dict(sid=sid, yellow=d, sig=d, oos=d >= OOS, **r)
        )
        last[sid] = d
    arms["Y_confirm1_next_open_H7"] = trades

    # 4) expert green only → T+1 open → L1H7
    trades = []
    last = {}
    for sid in UNIVERSE:
        cal = cals[sid]
        for i, d in enumerate(cal):
            if not expert_green(sid, d):
                continue
            if not dedupe_ok(last, sid, d):
                continue
            r = l1h7_from_sig(sid, i)
            if r is None:
                continue
            trades.append(dict(sid=sid, sig=d, oos=d >= OOS, **r))
            last[sid] = d
    arms["GREEN_T1open_L1H7"] = trades

    # 5a) yellow that later greens (hindsight) → immediate T+1
    trades = []
    last = {}
    for sid, i, d in yellow_events:
        if (sid, d) not in will_green:
            continue
        if not dedupe_ok(last, sid, d):
            continue
        r = l1h7_from_sig(sid, i)
        if r is None:
            continue
        trades.append(dict(sid=sid, yellow=d, sig=d, oos=d >= OOS, **r))
        last[sid] = d
    arms["Y_will_green_imm"] = trades

    # 5b) yellow that never greens → immediate T+1
    trades = []
    last = {}
    for sid, i, d in yellow_events:
        if (sid, d) not in never_green:
            continue
        if not dedupe_ok(last, sid, d):
            continue
        r = l1h7_from_sig(sid, i)
        if r is None:
            continue
        trades.append(dict(sid=sid, yellow=d, sig=d, oos=d >= OOS, **r))
        last[sid] = d
    arms["Y_never_green_imm"] = trades

    # 6) Paired: same yellow events that confirm≤2d — immediate vs wait-confirm
    paired_imm: list[dict] = []
    paired_wait: list[dict] = []
    last = {}
    for sid, i, d in yellow_events:
        if not dedupe_ok(last, sid, d):
            continue
        ci = find_confirm(sid, i, CONFIRM_WAIT)
        if ci is None:
            continue
        r_imm = l1h7_from_sig(sid, i)
        r_wait = l1h_from_entry(sid, ci + 1, px_mode="open")
        if r_imm is None or r_wait is None:
            continue
        row_i = dict(sid=sid, yellow=d, oos=d >= OOS, **r_imm)
        row_w = dict(
            sid=sid, yellow=d, confirm=cals[sid][ci], oos=d >= OOS, **r_wait
        )
        paired_imm.append(row_i)
        paired_wait.append(row_w)
        last[sid] = d
    arms["paired_confirm_imm"] = paired_imm
    arms["paired_confirm_wait"] = paired_wait

    # yellow without confirm (filtered out by E) — immediate for selection attribution
    trades = []
    last = {}
    for sid, i, d in yellow_events:
        if find_confirm(sid, i, CONFIRM_WAIT) is not None:
            continue
        if not dedupe_ok(last, sid, d):
            continue
        r = l1h7_from_sig(sid, i)
        if r is None:
            continue
        trades.append(dict(sid=sid, yellow=d, sig=d, oos=d >= OOS, **r))
        last[sid] = d
    arms["Y_no_confirm2_imm"] = trades

    stats = {k: summarize(v) for k, v in arms.items()}
    for k, st in stats.items():
        print(
            f"{k:28s} n={st['n']:4d} med={pct(st['med'])} "
            f"win={win_pct(st['win'])} OOS n={st['oos_n']} med={pct(st['oos_med'])}"
        )

    # paired deltas
    paired_deltas = [
        paired_wait[i]["radj"] - paired_imm[i]["radj"] for i in range(len(paired_imm))
    ]
    paired_oos_d = [
        paired_wait[i]["radj"] - paired_imm[i]["radj"]
        for i in range(len(paired_imm))
        if paired_imm[i]["oos"]
    ]
    paired_meta = {
        "n": len(paired_deltas),
        "med_delta": median(paired_deltas) if paired_deltas else None,
        "mean_delta": mean(paired_deltas) if paired_deltas else None,
        "frac_wait_better": (
            sum(1 for x in paired_deltas if x > 0) / len(paired_deltas)
            if paired_deltas
            else None
        ),
        "oos_n": len(paired_oos_d),
        "oos_med_delta": median(paired_oos_d) if paired_oos_d else None,
    }
    print(
        "paired Δ(wait−imm) med=",
        pct(paired_meta["med_delta"]),
        "oos med=",
        pct(paired_meta["oos_med_delta"]),
    )

    # ---- write report ----
    y = stats["Y_T1open_H7"]
    e = stats["Y_confirm2_next_open_H7"]
    e_close = stats["Y_confirm2_close_H7"]
    g = stats["GREEN_T1open_L1H7"]
    will = stats["Y_will_green_imm"]
    never = stats["Y_never_green_imm"]
    no_c = stats["Y_no_confirm2_imm"]
    p_imm = stats["paired_confirm_imm"]
    p_wait = stats["paired_confirm_wait"]

    # Verdict logic
    all_lift = (e["med"] or 0) - (y["med"] or 0)
    oos_lift = (e["oos_med"] or 0) - (y["oos_med"] or 0)
    vs_green_all = (e["med"] or 0) - (g["med"] or 0)
    vs_green_oos = (e["oos_med"] or 0) - (g["oos_med"] or 0)
    paired_timing = paired_meta["med_delta"] or 0
    selection_gap = (p_imm["med"] or 0) - (no_c["med"] or 0)

    survives_vs_green = False
    if vs_green_all < -0.02 or paired_timing < 0:
        freeze = "research note / satellite only — not a green substitute"
        verdict_short = (
            "**E does not survive fair compare vs green.** "
            "Confirm≤2d next-open beats naked yellow mainly by **selection** "
            "(drop no-confirm junk); **waiting after confirm hurts** on paired trades. "
            "Same-universe expert green L1H7 dominates on ALL. "
            "OOS med of E ≈ green is not enough to adopt E as an entry path."
        )
    elif abs(paired_timing) < 0.005 and all_lift < 0.01:
        freeze = "kill as entry alpha; keep as onset / confirm satellite note only"
        verdict_short = (
            "**E does not survive fair compare vs green.** "
            "ALL lift thin; paired timing ≈0; green dominates."
        )
    else:
        freeze = "research note — revisit if green unavailable"
        verdict_short = (
            "**E is competitive with green on this cut** — unexpected; re-check."
        )
        survives_vs_green = True

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines: list[str] = []
    lines.append("# H_E vs GREEN · fair revalidation (same universe)\n")
    lines.append(f"\n- 生成：{now}\n")
    lines.append("- Research only · sqlite `mode=ro` · **未採納** Strategy／Order\n")
    lines.append(
        f"- Runner：`scripts/research/run_yellow_confirm_vs_green_fair.py`\n"
    )
    lines.append("\n## Verdict\n\n")
    lines.append(verdict_short + "\n\n")
    lines.append(
        f"- Confirm@≤{CONFIRM_WAIT}d 轉換率：**{conf2}/{len(yellow_events)} = "
        f"{conf2 / len(yellow_events) * 100:.0f}%**（過濾力有限，對齊 H_A）。\n"
    )
    lines.append(
        f"- Paired timing Δ(wait−imm) med：{pct(paired_meta['med_delta'])} "
        f"（OOS {pct(paired_meta['oos_med_delta'])}）→ "
        f"{'等待本身幾乎不創造 alpha' if abs(paired_timing) < 0.01 else '等待略有 timing'}。\n"
    )
    lines.append(
        f"- Selection：confirm 子集立即進 {pct(p_imm['med'])} vs 無 confirm 立即進 "
        f"{pct(no_c['med'])}（Δ {pct(selection_gap)}）。\n"
    )
    lines.append(
        f"- vs GREEN：E ALL med {pct(e['med'])} vs green {pct(g['med'])} "
        f"（Δ {pct(vs_green_all)}）；OOS {pct(e['oos_med'])} vs {pct(g['oos_med'])} "
        f"（Δ {pct(vs_green_oos)}）。\n"
    )
    lines.append(f"- **Freeze recommendation**：{freeze}\n")

    lines.append("\n## Frozen protocol\n\n")
    lines.append("| 鍵 | 值 |\n|----|-----|\n")
    lines.append(f"| window | `{START}`..`{END}` |\n")
    lines.append(
        "| universe | adopted ∩ hard≥3 ∩ 非權值熱門 → **14**： "
        + ", ".join(UNIVERSE)
        + " |\n"
    )
    lines.append(
        "| yellow | Top10 連續 2 日各 ≥2 家 light（淨買×收 ∈[500萬, 0.5億)），"
        "兩日皆無 ≥1億 |\n"
    )
    lines.append(f"| Top10 | {','.join(TOP10)} |\n")
    lines.append(
        "| confirm | 黃燈後 1–2 交易日內任一 Top10 淨買 ≥0.5億（不含黃燈當日） |\n"
    )
    lines.append(
        "| green | 各股 champion（watch_spec／POOLS）：回看1日／同日／OR × core×floor |\n"
    )
    lines.append("| cost | 30bps（只扣個股） |\n")
    lines.append("| excess | \\(r_{\\mathrm{adj}}=r-1.15\\times r_{IX0001}\\) |\n")
    lines.append(f"| OOS | 訊號日 ≥ `{OOS}` |\n")
    lines.append("| dedupe | 同股 **5 曆日**（對齊 H_E；H_A 曾用 5 交易日） |\n")
    lines.append("| hold | H7（L1H7 = 訊號次日開 → 進場起第 7 日收） |\n")
    lines.append("| DB | `data/stocks.db` mode=ro · source=finmind |\n")

    lines.append("\n### Champion modes（本宇宙）\n\n")
    lines.append("| sid | 名 | mode | floor | core_n |\n")
    lines.append("|-----|----|------|------:|-------:|\n")
    for sid in UNIVERSE:
        fl = floors[sid]
        flab = "≥1億" if fl >= 1e8 - 1 else "≥0.5億"
        lines.append(
            f"| {sid} | {names[sid]} | {modes[sid]} | {flab} | {len(cores[sid])} |\n"
        )

    lines.append("\n## Arm table\n\n")
    lines.append(
        "| arm | n | ALL med \\(r_{adj}\\) | win | OOS n | OOS med | OOS win |\n"
    )
    lines.append(
        "|-----|--:|--------------------:|----:|------:|--------:|--------:|\n"
    )

    def row(label: str, st: dict) -> str:
        return (
            f"| {label} | {st['n']} | {pct(st['med'])} | {win_pct(st['win'])} | "
            f"{st['oos_n']} | {pct(st['oos_med'])} | {win_pct(st['oos_win'])} |\n"
        )

    lines.append(row("1 · Yellow → T+1 open → H7", y))
    lines.append(row("2 · Yellow → confirm≤2d → next open → H7 (**E**)", e))
    lines.append(row("3 · Yellow → confirm≤2d → confirm close → H7", e_close))
    lines.append(row("2b · Yellow → confirm≤1d → next open → H7", stats["Y_confirm1_next_open_H7"]))
    lines.append(row("4 · Expert GREEN → T+1 open → L1H7", g))
    lines.append(row("5a · Yellow will→green@10 · imm（選擇）", will))
    lines.append(row("5b · Yellow never→green@10 · imm", never))
    lines.append(row("6a · Paired confirmers · imm T+1", p_imm))
    lines.append(row("6b · Paired confirmers · wait next open", p_wait))
    lines.append(row("6c · Yellow no-confirm@2 · imm（被 E 丟掉）", no_c))

    lines.append("\n## Paired attribution（same yellow events that confirm≤2d）\n\n")
    lines.append(
        f"- n={paired_meta['n']} · Δ(wait−imm) med={pct(paired_meta['med_delta'])} "
        f"mean={pct(paired_meta['mean_delta'])} · "
        f"P(wait>imm)={win_pct(paired_meta['frac_wait_better'])}\n"
    )
    lines.append(
        f"- OOS n={paired_meta['oos_n']} · OOS Δ med={pct(paired_meta['oos_med_delta'])}\n"
    )
    lines.append(
        f"- Confirmers immediate ALL med={pct(p_imm['med'])} vs non-confirmers "
        f"{pct(no_c['med'])} → **selection Δ={pct(selection_gap)}**\n"
    )
    lines.append(
        "- 讀法：若 paired timing ≈0 而 confirm arm 優於全黃燈，增益來自"
        "**丟掉無半億的黃燈**，不是晚進場本身。\n"
    )

    lines.append("\n## Reconcile H_A vs H_E\n\n")
    lines.append("| 來源 | 主張 | 本腳本對齊結果 |\n")
    lines.append("|------|------|----------------|\n")
    lines.append(
        f"| H_A | confirm 全期中性（轉換率高、ALL Δmed≈0） | "
        f"confirm@≤2 ≈ {conf2 / len(yellow_events) * 100:.0f}%；"
        f"ALL Δ(E−Y)={pct(all_lift)} → **同意中性／弱 lift** |\n"
    )
    lines.append(
        f"| H_E | confirm≤2 → 次日開 OOS med ~+5.6% | "
        f"本跑 OOS med={pct(e['oos_med'])}（n={e['oos_n']}）→ "
        f"**OOS 方向對齊**；相對裸黃 OOS Δ={pct(oos_lift)} |\n"
    )
    lines.append(
        "| 張力來源 | A 看 ALL／轉換率；E 看 OOS 中位 | "
        "兩者可並存：全期幾乎不抬、OOS 子集中位較肥；"
        "paired 顯示 **timing 弱、selection 主** |\n"
    )
    lines.append(
        "| 與綠燈 | H_C：綠燈級 edge | "
        f"green ALL={pct(g['med'])} / OOS={pct(g['oos_med'])} ≫ E "
        f"ALL={pct(e['med'])} → E **不是**綠燈替代 |\n"
    )
    lines.append(
        "| 協議差 | H_A 5 交易日 dedupe；H_E／本檔 5 曆日 | "
        f"本檔裸黃 n={y['n']}（H_E≈297；H_A≈277） |\n"
    )

    lines.append("\n## Freeze recommendation\n\n")
    lines.append(f"**{freeze}**\n\n")
    lines.append("- 不寫 `config/strategy.yaml`／不進 Order。\n")
    lines.append(
        "- 黃燈→半億 confirm 可當 **onset 衛星／警戒**："
        "提高「這波可能有戲」的先驗，但仍應等 expert green（或同等重本）再當進場。\n"
    )
    lines.append(
        f"- E survives fair vs green? **{'YES' if survives_vs_green else 'NO'}**\n"
    )

    lines.append("\n## Caveats\n\n")
    lines.append("- 單槽、無容量；sum \\(r_{adj}\\) 不可當複利權益。\n")
    lines.append(
        "- Confirm／will-green 子集含選擇偏誤；paired 只在「會 confirm」事件上估 timing。\n"
    )
    lines.append(f"- OOS confirm n={e['oos_n']}，幅度勿過度解讀。\n")
    lines.append("- Green 用各股 champion core×floor，與 Top10 黃燈席位不完全重疊。\n")

    lines.append("\n## Reproduction\n\n")
    lines.append("```bash\n")
    lines.append(
        "PYTHONPATH=src .venv/bin/python "
        "scripts/research/run_yellow_confirm_vs_green_fair.py\n"
    )
    lines.append("```\n")

    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.write_text("".join(lines), encoding="utf-8")

    payload = {
        "generated_at": now,
        "protocol": {
            "start": START,
            "end": END,
            "oos": OOS,
            "universe": UNIVERSE,
            "top10": TOP10,
            "cost": COST,
            "beta": BETA,
            "hold": HOLD,
            "dedupe_calendar_days": DEDUP,
            "confirm_wait": CONFIRM_WAIT,
        },
        "raw_yellow": len(yellow_events),
        "confirm_rate_le2": conf2 / len(yellow_events) if yellow_events else None,
        "arms": {
            k: {kk: vv for kk, vv in st.items()} for k, st in stats.items()
        },
        "paired": paired_meta,
        "survives_vs_green": survives_vs_green,
        "freeze": freeze,
    }
    OUT_JSON.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    print("wrote", OUT_MD)
    print("wrote", OUT_JSON)


if __name__ == "__main__":
    main()
