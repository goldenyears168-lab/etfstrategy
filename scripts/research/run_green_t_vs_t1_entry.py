#!/usr/bin/env python3
"""Fair compare: GREEN same-day (T) entry vs L1 (T+1 open).

Research only · sqlite mode=ro · does not touch strategy/order.

Universe / green / cost / β / OOS / dedupe aligned with
`run_yellow_confirm_vs_green_fair.py` (H_E_vs_GREEN_fair · green n~190).

Arms:
  1. L1 open (baseline): green T → enter T+1 open → H7
  2. T close: enter T close → H7  (research upper bound; tape usually EOD)
  3. T open: LOOKAHEAD-INVALID (EOD branch tape) — reported as upper bound only
  4. T close ∧ day-chase≤3% vs prior close; else skip / fallback L1
  5. Optional: limit@T_close×1.01 on T+1 (fill if open≤limit, else skip)

  PYTHONPATH=src .venv/bin/python scripts/research/run_green_t_vs_t1_entry.py
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
    / "reports/research/branch-footprint-screen/expert_pool/hypotheses/H_GREEN_T_vs_T1.md"
)
OUT_JSON = OUT_MD.with_suffix(".json")
DB = ROOT / "data" / "stocks.db"
SOURCE = "finmind"
START, END, OOS = "2024-07-01", "2026-07-17", "2026-01-01"
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
COST, HOLD, BETA, DEDUP = 0.003, 7, 1.15, 5
CHASE_MAX = 0.03  # ≤3%
LIMIT_MULT = 1.01


def _load_watch_module():
    spec = importlib.util.spec_from_file_location(
        "epw_green_t", ROOT / "scripts/research/run_expert_pool_watch.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["epw_green_t"] = mod
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


def paired_delta(a: list[dict], b: list[dict], key: str = "sig") -> dict:
    """Median Δ(a−b) on shared (sid, sig) keys."""
    mb = {(t["sid"], t[key]): t for t in b}
    diffs = []
    oos_diffs = []
    for t in a:
        k = (t["sid"], t[key])
        if k not in mb:
            continue
        d = t["radj"] - mb[k]["radj"]
        diffs.append(d)
        if t["oos"]:
            oos_diffs.append(d)
    return {
        "n": len(diffs),
        "med": median(diffs) if diffs else None,
        "mean": mean(diffs) if diffs else None,
        "p_pos": (sum(1 for x in diffs if x > 0) / len(diffs)) if diffs else None,
        "oos_n": len(oos_diffs),
        "oos_med": median(oos_diffs) if oos_diffs else None,
    }


def main() -> None:
    mod = _load_watch_module()
    cores, floors, modes, names = load_champions(mod)
    print("universe", len(UNIVERSE), UNIVERSE)
    for sid in UNIVERSE:
        print(
            f"  {sid} {names[sid]} mode={modes[sid]} "
            f"floor={floors[sid]:.0f} core_n={len(cores[sid])}"
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

    def hold_from_entry(
        sid: str,
        entry_i: int,
        *,
        px_mode: str = "open",
        entry_px: float | None = None,
        hold: int = HOLD,
    ) -> dict | None:
        """H7 inclusive from entry day → exit close of entry+(hold-1)."""
        cal = cals[sid]
        if entry_i < 0 or entry_i + hold - 1 >= len(cal):
            return None
        ed = cal[entry_i]
        xd = cal[entry_i + hold - 1]
        eo, ec = bars[sid][ed]
        _, xc = bars[sid][xd]
        if entry_px is not None:
            ep = entry_px
        else:
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
        if entry_px is not None:
            # bench: use open on entry day when synthetic fill on T+1;
            # if entry is T close, use close.
            bo = (
                ix[ix_dates[j]][0]
                if px_mode == "open"
                else ix[ix_dates[j]][1]
            )
        else:
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
            "entry_px": ep,
            "exit_px": xc,
        }

    # ---- collect green signals (dedupe later per arm) ----
    greens: list[tuple[str, int, str]] = []
    for sid in UNIVERSE:
        cal = cals[sid]
        for i, d in enumerate(cal):
            if expert_green(sid, d):
                greens.append((sid, i, d))
    print(f"raw green fires: {len(greens)}")

    arms: dict[str, list[dict]] = {}
    skip_stats: dict[str, int] = {}

    def run_arm(
        name: str,
        *,
        entry_fn,
    ) -> None:
        trades: list[dict] = []
        last: dict[str, str] = {}
        skipped = 0
        for sid, i, d in greens:
            if not dedupe_ok(last, sid, d):
                continue
            row = entry_fn(sid, i, d)
            if row is None:
                skipped += 1
                continue
            trades.append(row)
            last[sid] = d
        arms[name] = trades
        skip_stats[name] = skipped
        s = summarize(trades)
        print(
            f"  {name}: n={s['n']} med={pct(s['med'])} "
            f"oos_n={s['oos_n']} oos_med={pct(s['oos_med'])} skip={skipped}"
        )

    # 1) L1 open baseline
    def arm_l1(sid: str, i: int, d: str) -> dict | None:
        r = hold_from_entry(sid, i + 1, px_mode="open")
        if r is None:
            return None
        return dict(sid=sid, sig=d, oos=d >= OOS, arm="L1_open", **r)

    # 2) T close
    def arm_t_close(sid: str, i: int, d: str) -> dict | None:
        r = hold_from_entry(sid, i, px_mode="close")
        if r is None:
            return None
        return dict(sid=sid, sig=d, oos=d >= OOS, arm="T_close", **r)

    # 3) T open — lookahead invalid upper bound
    def arm_t_open(sid: str, i: int, d: str) -> dict | None:
        r = hold_from_entry(sid, i, px_mode="open")
        if r is None:
            return None
        return dict(
            sid=sid,
            sig=d,
            oos=d >= OOS,
            arm="T_open_LOOKAHEAD",
            **r,
        )

    # 4a) T close with day-chase≤3% else SKIP
    def arm_t_close_chase_skip(sid: str, i: int, d: str) -> dict | None:
        if i < 1:
            return None
        cal = cals[sid]
        prev_c = bars[sid][cal[i - 1]][1]
        tc = bars[sid][d][1]
        if prev_c <= 0:
            return None
        chase = tc / prev_c - 1
        if chase > CHASE_MAX:
            return None
        r = hold_from_entry(sid, i, px_mode="close")
        if r is None:
            return None
        return dict(
            sid=sid,
            sig=d,
            oos=d >= OOS,
            arm="T_close_chase3_skip",
            day_chase=chase,
            **r,
        )

    # 4b) T close chase≤3% else FALLBACK L1
    def arm_t_close_chase_fb(sid: str, i: int, d: str) -> dict | None:
        used_fb = False
        day_chase = None
        if i >= 1:
            cal = cals[sid]
            prev_c = bars[sid][cal[i - 1]][1]
            tc = bars[sid][d][1]
            if prev_c > 0:
                day_chase = tc / prev_c - 1
                if day_chase > CHASE_MAX:
                    used_fb = True
                    r = hold_from_entry(sid, i + 1, px_mode="open")
                else:
                    r = hold_from_entry(sid, i, px_mode="close")
            else:
                r = hold_from_entry(sid, i, px_mode="close")
        else:
            r = hold_from_entry(sid, i, px_mode="close")
        if r is None:
            return None
        return dict(
            sid=sid,
            sig=d,
            oos=d >= OOS,
            arm="T_close_chase3_fallback_L1",
            day_chase=day_chase,
            used_fallback=used_fb,
            **r,
        )

    # 5) limit@T_close×1.01 on T+1 — fill if open ≤ limit else skip
    def arm_limit_101(sid: str, i: int, d: str) -> dict | None:
        if i + 1 >= len(cals[sid]):
            return None
        tc = bars[sid][d][1]
        lim = tc * LIMIT_MULT
        o1, _ = bars[sid][cals[sid][i + 1]]
        if o1 > lim:
            return None
        # fill at open (≤ limit); hold H7 from T+1
        r = hold_from_entry(sid, i + 1, px_mode="open", entry_px=o1)
        if r is None:
            return None
        return dict(
            sid=sid,
            sig=d,
            oos=d >= OOS,
            arm="limit_Tclose_x101_T1",
            limit_px=lim,
            **r,
        )

    print("arms:")
    run_arm("1_L1_open", entry_fn=arm_l1)
    run_arm("2_T_close", entry_fn=arm_t_close)
    run_arm("3_T_open_LOOKAHEAD", entry_fn=arm_t_open)
    run_arm("4a_T_close_chase3_skip", entry_fn=arm_t_close_chase_skip)
    run_arm("4b_T_close_chase3_fb_L1", entry_fn=arm_t_close_chase_fb)
    run_arm("5_limit_x101_T1", entry_fn=arm_limit_101)

    # chase diagnostics on green set (deduped like L1)
    chase_ok = chase_bad = 0
    last = {}
    for sid, i, d in greens:
        if not dedupe_ok(last, sid, d):
            continue
        last[sid] = d
        if i < 1:
            continue
        prev_c = bars[sid][cals[sid][i - 1]][1]
        tc = bars[sid][d][1]
        if prev_c <= 0:
            continue
        if tc / prev_c - 1 <= CHASE_MAX:
            chase_ok += 1
        else:
            chase_bad += 1

    # live soft chase: T+1 open / T close − 1 (for context)
    live_ok = live_bad = 0
    last = {}
    for sid, i, d in greens:
        if not dedupe_ok(last, sid, d):
            continue
        last[sid] = d
        if i + 1 >= len(cals[sid]):
            continue
        tc = bars[sid][d][1]
        o1 = bars[sid][cals[sid][i + 1]][0]
        if tc <= 0:
            continue
        if o1 / tc - 1 <= CHASE_MAX:
            live_ok += 1
        else:
            live_bad += 1

    fb_n = sum(1 for t in arms["4b_T_close_chase3_fb_L1"] if t.get("used_fallback"))

    # paired: same signals in both L1 and T_close
    pd_tc = paired_delta(arms["2_T_close"], arms["1_L1_open"])
    pd_to = paired_delta(arms["3_T_open_LOOKAHEAD"], arms["1_L1_open"])
    pd_ch = paired_delta(arms["4a_T_close_chase3_skip"], arms["1_L1_open"])

    # exit-day shift note: T_close exit is 1 trading day earlier than L1 for same sig
    exit_shift_examples = []
    by_l1 = {(t["sid"], t["sig"]): t for t in arms["1_L1_open"]}
    for t in arms["2_T_close"][:5]:
        k = (t["sid"], t["sig"])
        if k in by_l1:
            exit_shift_examples.append(
                {
                    "sid": t["sid"],
                    "sig": t["sig"],
                    "T_close_entry": t["entry"],
                    "T_close_exit": t["exit"],
                    "L1_entry": by_l1[k]["entry"],
                    "L1_exit": by_l1[k]["exit"],
                }
            )

    summaries = {k: summarize(v) for k, v in arms.items()}
    gen_ts = datetime.now().strftime("%Y-%m-%d %H:%M")

    # ---- markdown ----
    lines: list[str] = []
    lines.append("# H_GREEN · T-day entry vs L1 (T+1 open)")
    lines.append("")
    lines.append(f"- 生成：{gen_ts}")
    lines.append(
        "- Research only · sqlite `mode=ro` · **未採納** Strategy／Order"
    )
    lines.append(
        "- Runner：`scripts/research/run_green_t_vs_t1_entry.py`"
    )
    lines.append(
        "- Align：`H_E_vs_GREEN_fair.md` universe／champion green／cost／β／OOS／dedupe"
    )
    lines.append("")
    lines.append("## Verdict")
    lines.append("")

    s1 = summaries["1_L1_open"]
    s2 = summaries["2_T_close"]
    s3 = summaries["3_T_open_LOOKAHEAD"]
    s4a = summaries["4a_T_close_chase3_skip"]
    s4b = summaries["4b_T_close_chase3_fb_L1"]
    s5 = summaries["5_limit_x101_T1"]

    beat_all = (
        s2["med"] is not None
        and s1["med"] is not None
        and s2["med"] > s1["med"]
    )
    beat_oos = (
        s2["oos_med"] is not None
        and s1["oos_med"] is not None
        and s2["oos_med"] > s1["oos_med"]
    )

    if beat_all and beat_oos:
        verdict_head = (
            "**T-close beats L1 on ALL and OOS medians** — but green is EOD-tape; "
            "T-close is a research upper bound, not live-valid without same-day tape."
        )
    elif beat_all and not beat_oos:
        verdict_head = (
            "**T-close beats L1 on ALL med but not OOS** — do not change L1 convention."
        )
    elif not beat_all and beat_oos:
        verdict_head = (
            "**T-close beats L1 on OOS med only** — weak / unstable; keep L1 as default."
        )
    else:
        verdict_head = (
            "**T-day does not beat L1.** Keep T+1 open (L1H7) as the expert-pool convention."
        )

    lines.append(verdict_head)
    lines.append("")
    lines.append(
        f"- L1 baseline：ALL med **{pct(s1['med'])}** (n={s1['n']}) · "
        f"OOS **{pct(s1['oos_med'])}** (n={s1['oos_n']})"
    )
    lines.append(
        f"- T close：ALL med **{pct(s2['med'])}** (n={s2['n']}) · "
        f"OOS **{pct(s2['oos_med'])}** (n={s2['oos_n']}) · "
        f"paired Δ(Tclose−L1) med **{pct(pd_tc['med'])}** "
        f"(n={pd_tc['n']}; OOS Δ med {pct(pd_tc['oos_med'])})"
    )
    lines.append(
        f"- T open（**lookahead-invalid**）：ALL **{pct(s3['med'])}** · "
        f"OOS **{pct(s3['oos_med'])}** — upper bound only"
    )
    lines.append(
        f"- T close ∧ day-chase≤3%（skip）：n={s4a['n']} ALL **{pct(s4a['med'])}** · "
        f"OOS **{pct(s4a['oos_med'])}** "
        f"(day-chase pass {chase_ok}/{chase_ok + chase_bad})"
    )
    lines.append(
        f"- T close chase else fallback L1：n={s4b['n']} ALL **{pct(s4b['med'])}** · "
        f"OOS **{pct(s4b['oos_med'])}** · fallback used {fb_n}"
    )
    lines.append(
        f"- Limit T_close×1.01 @T+1（open≤lim）：n={s5['n']} ALL **{pct(s5['med'])}** · "
        f"OOS **{pct(s5['oos_med'])}**"
    )
    lines.append(
        "- **Observability**：champion green uses FinMind `stock_broker_branch_daily` "
        "（EOD tape）→ green is knowable **after close / overnight** "
        "（`run_expert_pool_watch` 夜間觀測）. "
        "**Only valid live same-calendar arm would be T-close if tape arrived pre-close "
        "(it usually does not).** T-open is strictly lookahead-invalid. "
        "Live soft rules already target **T+1 open** with chase≤3% / limit×1.01."
    )
    lines.append(
        "- **Freeze recommendation**：keep L1；T-close／T-open not Order-eligible"
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
    lines.append("| cost | 30bps（只扣個股） |")
    lines.append("| excess | \\(r_{\\mathrm{adj}}=r-1.15\\times r_{IX0001}\\) |")
    lines.append(f"| OOS | 訊號日 ≥ `{OOS}` |")
    lines.append("| dedupe | 同股 **5 曆日** |")
    lines.append(
        "| hold | **H7 from entry**（進場日起含當日共 7 交易日收盤出場） |"
    )
    lines.append(
        "| exit shift | T-close 出場日較同訊號 L1 **早 1 個交易日**（進場早一日） |"
    )
    lines.append(
        "| day-chase | `T_close / prior_close − 1` ≤ **+3%**（同日漲幅門） |"
    )
    lines.append(
        "| live chase（對照） | `T+1_open / T_close − 1` ≤ +3% "
        f"（pass {live_ok}/{live_ok + live_bad}） |"
    )
    lines.append("| DB | `data/stocks.db` mode=ro · source=finmind |")
    lines.append("")
    lines.append("### Champion modes（本宇宙）")
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
    lines.append("## When is green observable?")
    lines.append("")
    lines.append("| Arm | Fill | Live-valid? | Why |")
    lines.append("|-----|------|-------------|-----|")
    lines.append(
        "| **1 · L1 open** | T+1 open | **Yes**（現行） | 夜間讀 EOD tape → 次日開 |"
    )
    lines.append(
        "| **2 · T close** | T close | **No / optimistic** | 需收盤前已知 green；"
        "FinMind 分點多為盤後 → research upper bound |"
    )
    lines.append(
        "| **3 · T open** | T open | **Lookahead-invalid** | green 用當日分點淨買；"
        "開盤不可能知 |"
    )
    lines.append(
        "| **4 · T close ∧ chase** | T close（或 fb L1） | 同 #2 | 軟規則同日漲≤3% |"
    )
    lines.append(
        "| **5 · limit×1.01** | T+1 @open if open≤T_close×1.01 | **Yes**（軟） | "
        "對齊 live「追價>3% 掛限價」的簡化成交模型 |"
    )
    lines.append("")
    lines.append(
        "**結論（可觀測性）**：若嚴格 PIT，**唯一合法同日臂不存在**——"
        "T-close 亦樂觀；**T-open 必剔除**。"
        "實務唯一合法同日候選僅在「分點 tape 於收盤前可得」時的 T-close。"
    )
    lines.append("")
    lines.append("## Arm table")
    lines.append("")
    lines.append(
        "| arm | n | ALL med \\(r_{adj}\\) | win | OOS n | OOS med | OOS win |"
    )
    lines.append(
        "|-----|--:|--------------------:|----:|------:|--------:|--------:|"
    )

    def row(label: str, key: str) -> str:
        s = summaries[key]
        return (
            f"| {label} | {s['n']} | {pct(s['med'])} | {win_pct(s['win'])} | "
            f"{s['oos_n']} | {pct(s['oos_med'])} | {win_pct(s['oos_win'])} |"
        )

    lines.append(row("1 · GREEN → T+1 open → H7 (**L1**)", "1_L1_open"))
    lines.append(row("2 · GREEN → T close → H7", "2_T_close"))
    lines.append(
        row("3 · GREEN → T open → H7 (**LOOKAHEAD**)", "3_T_open_LOOKAHEAD")
    )
    lines.append(
        row("4a · T close ∧ day-chase≤3%（else skip）", "4a_T_close_chase3_skip")
    )
    lines.append(
        row("4b · T close chase else fallback L1", "4b_T_close_chase3_fb_L1")
    )
    lines.append(
        row("5 · Limit T_close×1.01 @T+1（open≤lim）", "5_limit_x101_T1")
    )
    lines.append("")
    lines.append("## Paired timing（same green signals）")
    lines.append("")
    lines.append(
        f"- T_close − L1：n={pd_tc['n']} · med Δ **{pct(pd_tc['med'])}** · "
        f"P(Tclose>L1)={win_pct(pd_tc['p_pos'])} · "
        f"OOS n={pd_tc['oos_n']} med Δ **{pct(pd_tc['oos_med'])}**"
    )
    lines.append(
        f"- T_open − L1（lookahead）：n={pd_to['n']} · med Δ **{pct(pd_to['med'])}** · "
        f"OOS med Δ **{pct(pd_to['oos_med'])}**"
    )
    lines.append(
        f"- Chase-skip ∩ L1：n={pd_ch['n']} · med Δ **{pct(pd_ch['med'])}** · "
        f"OOS med Δ **{pct(pd_ch['oos_med'])}**"
    )
    lines.append("")
    lines.append("### Exit-day shift（H7 from entry）")
    lines.append("")
    lines.append(
        "Same signal T：L1 enters T+1 → exits at close of (T+1)+6；"
        "T-close enters T → exits at close of T+6 → **出場早 1 交易日**。"
    )
    if exit_shift_examples:
        lines.append("")
        lines.append("| sid | sig | T-close entry→exit | L1 entry→exit |")
        lines.append("|-----|-----|--------------------|---------------|")
        for e in exit_shift_examples:
            lines.append(
                f"| {e['sid']} | {e['sig']} | "
                f"{e['T_close_entry']}→{e['T_close_exit']} | "
                f"{e['L1_entry']}→{e['L1_exit']} |"
            )
    lines.append("")
    lines.append("## Freeze recommendation")
    lines.append("")
    lines.append("**Keep L1 (T+1 open · L1H7) as expert-pool entry convention.**")
    lines.append("")
    lines.append("- 不寫 `config/strategy.yaml`／不進 Order。")
    lines.append(
        "- T-close／T-open 僅 research；T-open = lookahead-invalid。"
    )
    lines.append(
        "- Live soft（追價≤3%／限價×1.01）仍掛在 **T+1**，與本表 arm 5 對齊方向。"
    )
    lines.append("")
    lines.append("## Caveats")
    lines.append("")
    lines.append("- 單槽、無容量；sum \\(r_{adj}\\) 不可當複利權益。")
    lines.append(
        "- T-close 假設收盤可知 green → 相對真實夜間 tape **樂觀**。"
    )
    lines.append(
        "- Limit 模型僅用日線 open≤limit；無分鐘 high/low → 成交率偏保守／偏簡。"
    )
    lines.append(
        f"- Day-chase pass rate（deduped greens）："
        f"{chase_ok}/{chase_ok + chase_bad}；"
        f"live T+1 chase pass：{live_ok}/{live_ok + live_bad}。"
    )
    lines.append("")
    lines.append("## Reproduction")
    lines.append("")
    lines.append("```bash")
    lines.append(
        "PYTHONPATH=src .venv/bin/python scripts/research/run_green_t_vs_t1_entry.py"
    )
    lines.append("```")
    lines.append("")

    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")

    payload = {
        "generated": gen_ts,
        "protocol": {
            "start": START,
            "end": END,
            "oos": OOS,
            "universe": UNIVERSE,
            "cost": COST,
            "hold": HOLD,
            "beta": BETA,
            "dedup_calendar_days": DEDUP,
            "chase_max": CHASE_MAX,
            "limit_mult": LIMIT_MULT,
        },
        "champions": {
            sid: {
                "name": names[sid],
                "mode": modes[sid],
                "floor": floors[sid],
                "core_n": len(cores[sid]),
            }
            for sid in UNIVERSE
        },
        "summaries": {
            k: {
                kk: (float(vv) if isinstance(vv, float) else vv)
                for kk, vv in s.items()
            }
            for k, s in summaries.items()
        },
        "paired": {"T_close_vs_L1": pd_tc, "T_open_vs_L1": pd_to, "chase_vs_L1": pd_ch},
        "chase_day_pass": {"ok": chase_ok, "bad": chase_bad},
        "chase_live_pass": {"ok": live_ok, "bad": live_bad},
        "fallback_used": fb_n,
        "skip_stats": skip_stats,
        "raw_green_fires": len(greens),
        "exit_shift_examples": exit_shift_examples,
        "trades": {k: v for k, v in arms.items()},
    }
    OUT_JSON.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {OUT_MD}")
    print(f"wrote {OUT_JSON}")


if __name__ == "__main__":
    main()
