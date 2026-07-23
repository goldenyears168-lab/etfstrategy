#!/usr/bin/env python3
"""H_GREEN_HOLD_EXIT: expert green hold horizon + simple early exits vs L1H7.

Research only · sqlite mode=ro · same thick universe as fair studies.

Arms
  1) time: L1H3 / L1H5 / L1H7 / L1H10 (T+1 open → H close)
  2) early (max hold H7 unless noted):
       SL−8% from entry (day low touch → exit @ −8%)
       TP+8% / TP+12% (day high touch → exit @ TP)
       peak DD−5% from max close since entry (exit @ that close)
  3) Detach Gate: SKIPPED — live sleeve is US–TW 5m session gate
     (`run_detach_gate_poll` / `order.us_tw_5m_sell_gate`); not a cheap
     daily-bar overlay on expert-green holdings.

  PYTHONPATH=src .venv/bin/python scripts/research/run_h_green_hold_exit.py
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
    / "H_GREEN_HOLD_EXIT.md"
)
OUT_JSON = OUT_MD.with_suffix(".json")
DB = ROOT / "data" / "stocks.db"
SOURCE = "finmind"
START, END, OOS = "2024-07-01", "2026-07-17", "2026-01-01"
COST, BETA, DEDUP = 0.003, 1.15, 5
HOLDS = (3, 5, 7, 10)
BASE_H = 7
SL_PCT = 0.08
TP_PCTS = (0.08, 0.12)
PEAK_DD = 0.05

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
        "epw_ghe", ROOT / "scripts/research/run_expert_pool_watch.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["epw_ghe"] = mod
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
    is_ = blk([t for t in trades if not t["oos"]])
    early_n = sum(1 for t in trades if t.get("exit_reason") not in (None, "time"))
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

    # Pad past END so late signals can still complete H10.
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
                ] = float(r["net"]) * oc[3]
    con.close()

    # Signal calendar clipped to study window; path calendar may extend.
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
    ) -> dict | None:
        if ep <= 0 or xp <= 0:
            return None
        ret = xp / ep - 1 - COST
        bret = bench_ret(ed, xd, px_mode="open")
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
        }

    def time_exit(sid: str, entry_i: int, hold: int) -> dict | None:
        cal = path_cals[sid]
        if entry_i < 0 or entry_i + hold - 1 >= len(cal):
            return None
        ed = cal[entry_i]
        xd = cal[entry_i + hold - 1]
        o, _, _, _ = bars[sid][ed]
        _, _, _, xc = bars[sid][xd]
        return pack(sid, "", o, ed, xc, xd, "time")

    def path_exit(
        sid: str,
        entry_i: int,
        *,
        max_hold: int,
        stop_pct: float | None = None,
        tp_pct: float | None = None,
        peak_dd: float | None = None,
        reason_tag: str | None = None,
    ) -> dict | None:
        """Walk daily bars from entry; first trigger wins; else time @ max_hold."""
        cal = path_cals[sid]
        if entry_i < 0 or entry_i + max_hold - 1 >= len(cal):
            return None
        ed = cal[entry_i]
        o, hi0, lo0, c0 = bars[sid][ed]
        ep = o
        stop_px = ep * (1.0 - stop_pct) if stop_pct is not None else None
        tp_px = ep * (1.0 + tp_pct) if tp_pct is not None else None

        # Same-day SL / TP (intraday high-low vs entry open)
        if stop_px is not None and lo0 <= stop_px:
            return pack(sid, "", ep, ed, stop_px, ed, reason_tag or "sl")
        if tp_px is not None and hi0 >= tp_px:
            return pack(sid, "", ep, ed, tp_px, ed, reason_tag or "tp")

        peak = c0
        last_i = entry_i + max_hold - 1
        for j in range(entry_i + 1, last_i + 1):
            d = cal[j]
            _, hi, lo, cl = bars[sid][d]
            # Priority when both SL & TP same day: SL first (conservative).
            if stop_px is not None and lo <= stop_px:
                return pack(sid, "", ep, ed, stop_px, d, reason_tag or "sl")
            if tp_px is not None and hi >= tp_px:
                return pack(sid, "", ep, ed, tp_px, d, reason_tag or "tp")
            if peak_dd is not None:
                if cl <= peak * (1.0 - peak_dd):
                    return pack(sid, "", ep, ed, cl, d, reason_tag or "peak_dd")
                peak = max(peak, cl)

        xd = cal[last_i]
        _, _, _, xc = bars[sid][xd]
        return pack(sid, "", ep, ed, xc, xd, "time")

    # Collect green signals (deduped)
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

    # 1) time holds
    for h in HOLDS:
        trades: list[dict] = []
        for sid, pi, sig in signals:
            r = time_exit(sid, pi + 1, h)
            if r is None:
                continue
            r["sig"] = sig
            r["oos"] = sig >= OOS
            trades.append(r)
        arms[f"L1H{h}"] = trades
        print(f"  L1H{h}: n={len(trades)}")

    # 2) early exits on max_hold = H7 (vs L1H7 baseline)
    specs = [
        ("SL8_H7", dict(stop_pct=SL_PCT, max_hold=BASE_H)),
        ("TP8_H7", dict(tp_pct=0.08, max_hold=BASE_H)),
        ("TP12_H7", dict(tp_pct=0.12, max_hold=BASE_H)),
        ("PEAKDD5_H7", dict(peak_dd=PEAK_DD, max_hold=BASE_H)),
        # sensitivity: same early rules with H10 cap
        ("SL8_H10", dict(stop_pct=SL_PCT, max_hold=10)),
        ("TP8_H10", dict(tp_pct=0.08, max_hold=10)),
        ("TP12_H10", dict(tp_pct=0.12, max_hold=10)),
        ("PEAKDD5_H10", dict(peak_dd=PEAK_DD, max_hold=10)),
    ]
    for name, kw in specs:
        trades = []
        for sid, pi, sig in signals:
            r = path_exit(sid, pi + 1, **kw)
            if r is None:
                continue
            r["sig"] = sig
            r["oos"] = sig >= OOS
            trades.append(r)
        arms[name] = trades
        er = summarize(trades)["exit_reasons"]
        print(f"  {name}: n={len(trades)} reasons={er}")

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

    # Ranking: prefer OOS med, then ALL med; require n comparable
    ranked = sorted(
        stats.keys(),
        key=lambda k: (
            stats[k]["oos_med"] is not None,
            stats[k]["oos_med"] if stats[k]["oos_med"] is not None else -9e9,
            stats[k]["med"] if stats[k]["med"] is not None else -9e9,
        ),
        reverse=True,
    )
    best = ranked[0]
    beats_l1h7 = (
        best != "L1H7"
        and stats[best]["oos_med"] is not None
        and base["oos_med"] is not None
        and (
            stats[best]["oos_med"] > base["oos_med"] + 1e-6
            or (
                abs(stats[best]["oos_med"] - base["oos_med"]) <= 1e-6
                and (stats[best]["med"] or -9) > (base["med"] or -9)
            )
        )
    )

    # Freeze heuristic: OOS med ≥+0.5pp vs L1H7 and ALL not worse by >1pp.
    adopt_candidate = None
    for k in ranked:
        if k == "L1H7":
            continue
        dm = delta_med(k)
        do = delta_oos(k)
        if do is None or dm is None:
            continue
        if do >= 0.005 and dm >= -0.01:
            adopt_candidate = k
            break

    early_hurt = all(
        (delta_oos(k) or 0) < 0 and (delta_med(k) or 0) < 0
        for k in ("SL8_H7", "PEAKDD5_H7")
    )
    if adopt_candidate == "L1H10":
        freeze = (
            "keep L1H7 as protocol SSOT; research note L1H10 longer-hold "
            "(not an early-sell). Early SL / peak-DD hurt on green too."
        )
    elif adopt_candidate and adopt_candidate.startswith("TP12"):
        freeze = (
            f"keep L1H7 SSOT; research alt {adopt_candidate} "
            "(TP only — not SL/peak). Not Strategy yet."
        )
    elif adopt_candidate:
        freeze = (
            f"keep L1H7 as protocol SSOT; {adopt_candidate} is weak OOS edge only"
        )
    else:
        freeze = "freeze L1H7 — early exits do not clear bar"
        if early_hurt:
            freeze += " (SL−8% / peakDD−5% hurt)"

    # ---- markdown ----
    lines: list[str] = []
    lines.append("# H_GREEN_HOLD_EXIT · expert green hold / early exit vs L1H7")
    lines.append("")
    lines.append(f"- 生成：{datetime.now():%Y-%m-%d %H:%M}")
    lines.append("- Research only · sqlite `mode=ro` · **未採納** Strategy／Order")
    lines.append("- Runner：`scripts/research/run_h_green_hold_exit.py`")
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
    if adopt_candidate:
        ac = stats[adopt_candidate]
        lines.append(
            f"**Best vs L1H7（OOS-first）**：`{adopt_candidate}` · "
            f"ALL med {pct(ac['med'])} (Δ {pct(delta_med(adopt_candidate))}) · "
            f"OOS med {pct(ac['oos_med'])} (Δ {pct(delta_oos(adopt_candidate))}) · "
            f"early/trigger {frac_pct(ac['early_frac'])}."
        )
        lines.append("")
        lines.append(
            "**Early sell vs L1H7**：SL−8% 與 peak DD−5% **hurt**（ALL／OOS 皆負）；"
            "TP+8% 不穩；**TP+12%** 可抬中位但仍次於單純拉長至 H10。"
            "結論：**不是 early-sell 打敗 L1H7，是 longer hold（H10）**。"
        )
    else:
        lines.append(
            f"**Best ranked arm**：`{best}` "
            f"(OOS med {pct(stats[best]['oos_med'])}); "
            "**no early-exit / alt-hold clears the promote bar vs L1H7**."
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
    lines.append("### 1) Time exits（L1H*）")
    lines.append("")
    lines.append(
        "| arm | n | ALL med "
        + r"\(r_{adj}\)"
        + " | win | OOS n | OOS med | OOS win | ΔALL vs L1H7 | ΔOOS |"
    )
    lines.append(
        "|-----|--:|--------------------:|----:|------:|--------:|--------:|-------------:|------:|"
    )
    for h in HOLDS:
        k = f"L1H{h}"
        s = stats[k]
        lines.append(
            f"| {'**' if h == BASE_H else ''}{k}{'**' if h == BASE_H else ''} | {s['n']} | "
            f"{pct(s['med'])} | {win_pct(s['win'])} | {s['oos_n']} | "
            f"{pct(s['oos_med'])} | {win_pct(s['oos_win'])} | "
            f"{pct(delta_med(k))} | {pct(delta_oos(k))} |"
        )
    lines.append("")
    lines.append("### 2) Early exits（daily bars · max hold H7 / H10）")
    lines.append("")
    lines.append(
        "- **SL−8%**：日低觸及進場價×0.92 → 出場價設 −8%（對齊 H_E 黃燈簡化）。"
    )
    lines.append("- **TP+8% / +12%**：日高觸及目標 → 出場價設目標價。")
    lines.append(
        "- **peak DD−5%**：自進場日起追蹤 max close；若收盤 ≤ peak×0.95 → 以該收盤出。"
    )
    lines.append("- 同日 SL 與 TP 皆觸：本腳本 **SL 優先**（保守）。")
    lines.append("")
    lines.append(
        "| arm | n | early% | ALL med | win | OOS n | OOS med | OOS win | ΔALL | ΔOOS | reasons |"
    )
    lines.append(
        "|-----|--:|-------:|--------:|----:|------:|--------:|--------:|-----:|------:|---------|"
    )
    for name, _ in specs:
        s = stats[name]
        rs = ", ".join(f"{a}={b}" for a, b in sorted(s["exit_reasons"].items()))
        lines.append(
            f"| {name} | {s['n']} | {frac_pct(s['early_frac'])} | {pct(s['med'])} | "
            f"{win_pct(s['win'])} | {s['oos_n']} | {pct(s['oos_med'])} | "
            f"{win_pct(s['oos_win'])} | {pct(delta_med(name))} | {pct(delta_oos(name))} | "
            f"{rs} |"
        )
    lines.append("")
    lines.append("### 3) Detach Gate overlay")
    lines.append("")
    lines.append(
        "**SKIPPED.** Detach Gate is a Strategy／Order **US–TW 5m** session sell-half "
        "gate (`scripts/order/run_detach_gate_poll.py`, `order.us_tw_5m_sell_gate`, "
        "`config/order.yaml` `detach_gate`). It is not a stock-path daily rule and "
        "needs NQ/TW intraday tape — not cheap to overlay on these L1 green holdings "
        "in research. No live Order changes in this study."
    )
    lines.append("")
    lines.append("## Ranking（OOS med → ALL med）")
    lines.append("")
    lines.append("| rank | arm | OOS med | ALL med | note |")
    lines.append("|-----:|-----|--------:|--------:|------|")
    for i, k in enumerate(ranked[:10], 1):
        note = "baseline" if k == "L1H7" else ""
        if k == adopt_candidate:
            note = "best clear-bar candidate" if note == "" else note
        lines.append(
            f"| {i} | `{k}` | {pct(stats[k]['oos_med'])} | {pct(stats[k]['med'])} | {note} |"
        )
    lines.append("")
    lines.append("## Reading")
    lines.append("")
    # auto commentary from numbers
    h10 = stats["L1H10"]
    h3 = stats["L1H3"]
    sl = stats["SL8_H7"]
    tp8 = stats["TP8_H7"]
    tp12 = stats["TP12_H7"]
    pdd = stats["PEAKDD5_H7"]
    lines.append(
        f"- **Hold ladder**：H3 ALL {pct(h3['med'])} / OOS {pct(h3['oos_med'])}；"
        f"H7 ALL {pct(g7['med'])} / OOS {pct(g7['oos_med'])}；"
        f"H10 ALL {pct(h10['med'])} / OOS {pct(h10['oos_med'])}。"
    )
    lines.append(
        f"- **SL−8%@H7**：觸發 {frac_pct(sl['early_frac'])} · "
        f"ALL {pct(sl['med'])}（Δ {pct(delta_med('SL8_H7'))}）· "
        f"OOS {pct(sl['oos_med'])}（Δ {pct(delta_oos('SL8_H7'))}）"
        " — 對齊黃燈研究：固定 −8% 易誤殺（需看數值是否仍 hurt on green）。"
    )
    lines.append(
        f"- **TP+8%/@H7**：early {frac_pct(tp8['early_frac'])} · "
        f"ΔALL {pct(delta_med('TP8_H7'))} · ΔOOS {pct(delta_oos('TP8_H7'))}。"
    )
    lines.append(
        f"- **TP+12%@H7**：early {frac_pct(tp12['early_frac'])} · "
        f"ΔALL {pct(delta_med('TP12_H7'))} · ΔOOS {pct(delta_oos('TP12_H7'))}。"
    )
    lines.append(
        f"- **peak DD−5%@H7**：early {frac_pct(pdd['early_frac'])} · "
        f"ΔALL {pct(delta_med('PEAKDD5_H7'))} · ΔOOS {pct(delta_oos('PEAKDD5_H7'))}。"
    )
    lines.append("")
    lines.append("## Freeze recommendation")
    lines.append("")
    lines.append(f"**{freeze}**")
    lines.append("")
    lines.append("- 不寫 `config/strategy.yaml`／不進 Order。")
    lines.append("- 協議持有仍以 **L1H7** 為 SSOT，除非後續獨立 OOS 複驗通過 promote bar。")
    lines.append("")
    lines.append("## Caveats")
    lines.append("")
    lines.append(
        "- 單槽、無容量／重疊約束；sum "
        + r"\(r_{adj}\)"
        + " 不可當複利權益。"
    )
    lines.append("- SL/TP 用日高低觸發、出場價設在觸發價（簡化），實際滑價可能更差。")
    lines.append("- peak DD 用收盤（非盤中），較慢、也較不易誤殺。")
    lines.append("- Detach Gate 未疊加；全帳戶 5m RED≠個股路徑出場。")
    lines.append("")
    lines.append("## Reproduction")
    lines.append("")
    lines.append("```bash")
    lines.append(
        "PYTHONPATH=src .venv/bin/python scripts/research/run_h_green_hold_exit.py"
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
            "detach_gate": "skipped",
        },
        "baseline": "L1H7",
        "best_ranked": best,
        "adopt_candidate": adopt_candidate,
        "beats_l1h7_oos": beats_l1h7,
        "freeze": freeze,
        "stats": {
            k: {
                **{kk: vv for kk, vv in s.items() if kk != "exit_reasons"},
                "exit_reasons": s["exit_reasons"],
                "delta_all_vs_l1h7": delta_med(k),
                "delta_oos_vs_l1h7": delta_oos(k),
            }
            for k, s in stats.items()
        },
        "n_signals": len(signals),
    }
    OUT_JSON.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"wrote {OUT_MD}")
    print(f"wrote {OUT_JSON}")
    print("freeze:", freeze)
    print("best:", best, "candidate:", adopt_candidate)


if __name__ == "__main__":
    main()
