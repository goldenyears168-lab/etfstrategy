#!/usr/bin/env python3
"""H_GREEN_H7_EXTEND: at L1H7 close, which PIT features predict H10 > H7?

Research only · same thick universe / cost / β as H_GREEN_HOLD_EXIT.
Among trades that complete H7, label = (radj_H10 > radj_H7). Features at
H7 evening (H7 close date, FinMind EOD — same observability as green).

  PYTHONPATH=src .venv/bin/python scripts/research/run_h_green_h7_extend.py
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
sys.path.insert(0, str(ROOT / "src"))

from research.chen_chip.features import build_chip_feature_frame  # noqa: E402

OUT_MD = (
    ROOT
    / "reports/research/branch-footprint-screen/expert_pool/hypotheses"
    / "H_GREEN_H7_EXTEND.md"
)
OUT_JSON = OUT_MD.with_suffix(".json")
DB = ROOT / "data" / "stocks.db"
CACHE = ROOT / "reports/research/whale_chip_precursor/cache"
HS_PATH = CACHE / "holding_shares_per_tier_a.csv"
GOV_PATH = CACHE / "gov_bank_net_tier_a.csv"
SOURCE = "finmind"
START, END, OOS = "2024-07-01", "2026-07-17", "2026-01-01"
COST, BETA, DEDUP = 0.003, 1.15, 5
H7, H10 = 7, 10
# Freeze bar: OOS med Δ(H10−H7) when flag ON ≥ +0.5pp vs OFF, and ALL not
# worse by >1pp; min OOS n on both sides; cover ≥25% of H7-complete trades.
FREEZE_DELTA_OOS = 0.005
FREEZE_ALL_FLOOR = -0.01
MIN_OOS_N = 12
MIN_COVER = 0.25

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
        "epw_h7e", ROOT / "scripts/research/run_expert_pool_watch.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["epw_h7e"] = mod
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


def blk(xs: list[float]) -> dict:
    if not xs:
        return {"n": 0, "med": None, "mean": None, "win": None}
    return {
        "n": len(xs),
        "med": median(xs),
        "mean": mean(xs),
        "win": sum(1 for x in xs if x > 0) / len(xs),
    }


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

    # Core nets: buy (>0) and sell (<0) for specialist heavy-sell
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
                  AND trade_date BETWEEN ? AND date(?, '+25 days')""",
            (SOURCE, *UNIVERSE, *core_tids, START, END),
        ):
            oc = bars[r["stock_id"]].get(r["trade_date"])
            if oc:
                core_by[r["stock_id"]][r["trade_date"]][
                    r["securities_trader_id"]
                ] = float(r["net"]) * oc[3]

    # Chip panel (three_streak) — PIT on H7 date
    hs = HS_PATH if HS_PATH.exists() else None
    gov = GOV_PATH if GOV_PATH.exists() else None
    print("building chip features…")
    feat = build_chip_feature_frame(
        con, UNIVERSE, START, END, holding_shares_path=hs, gov_bank_path=gov
    )
    chip: dict[str, dict[str, float]] = defaultdict(dict)
    if not feat.empty and "three_streak" in feat.columns:
        for _, row in feat.iterrows():
            sid = str(row["sid"])
            d = str(row["d"])
            v = row.get("three_streak")
            if v == v:  # not NaN
                chip[sid][d] = float(v)
    print(f"  chip three_streak days: {sum(len(v) for v in chip.values())}")
    con.close()

    cals = {
        sid: sorted(d for d in bars[sid] if START <= d <= END) for sid in UNIVERSE
    }
    path_cals = {sid: sorted(bars[sid]) for sid in UNIVERSE}
    path_idx = {sid: {d: i for i, d in enumerate(path_cals[sid])} for sid in UNIVERSE}

    def day_core_amts(sid: str, d: str) -> dict[str, float]:
        core = cores[sid]
        return {t: a for t, a in core_by[sid].get(d, {}).items() if t in core}

    def hard_sets(sid: str, d: str) -> tuple[set[str], set[str]]:
        floor = floors[sid]
        pi = path_idx[sid].get(d)
        if pi is None:
            return set(), set()
        today = {t for t, a in day_core_amts(sid, d).items() if a >= floor}
        yday: set[str] = set()
        if pi > 0:
            yd = path_cals[sid][pi - 1]
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

    def soft_or_incl(sid: str, d: str) -> bool:
        """≥1 core @floor today (may also be hard)."""
        today, _ = hard_sets(sid, d)
        return len(today) >= 1

    def soft_half_incl(sid: str, d: str) -> bool:
        """Champion structure at floor/2 (may also be hard)."""
        if not cores[sid]:
            return False
        half = floors[sid] / 2.0
        mode = modes[sid]
        pi = path_idx[sid].get(d)
        if pi is None:
            return False
        amts = day_core_amts(sid, d)
        today = {t for t, a in amts.items() if a >= half}
        yday: set[str] = set()
        if pi > 0:
            yd = path_cals[sid][pi - 1]
            yday = {
                t for t, a in day_core_amts(sid, yd).items() if a >= half
            }
        if "OR" in mode:
            return len(today) >= 1
        if "同日" in mode:
            return len(today) >= 2
        return len(today) >= 1 and len(today | yday) >= 2

    def specialist_heavy_sell(sid: str, d: str) -> bool:
        """Any core seat net sell amount ≥ floor (absolute)."""
        floor = floors[sid]
        for a in day_core_amts(sid, d).values():
            if a <= -floor:
                return True
        return False

    def sma5_at(sid: str, d: str) -> float | None:
        pi = path_idx[sid].get(d)
        if pi is None or pi < 4:
            return None
        closes = [bars[sid][path_cals[sid][pi - k]][3] for k in range(5)]
        return sum(closes) / 5.0

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

    def radj_path(sid: str, entry_i: int, hold: int) -> tuple[float, str, float] | None:
        cal = path_cals[sid]
        if entry_i < 0 or entry_i + hold - 1 >= len(cal):
            return None
        ed = cal[entry_i]
        xd = cal[entry_i + hold - 1]
        ep = bars[sid][ed][0]
        xc = bars[sid][xd][3]
        if ep <= 0 or xc <= 0:
            return None
        ret = xc / ep - 1 - COST
        bret = bench_ret(ed, xd)
        if bret is None:
            return None
        return ret - BETA * bret, xd, xc

    # Collect green → L1 trades that reach both H7 and H10
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

    rows: list[dict] = []
    for sid, pi, sig in signals:
        entry_i = pi + 1
        t7 = radj_path(sid, entry_i, H7)
        t10 = radj_path(sid, entry_i, H10)
        if t7 is None or t10 is None:
            continue
        radj7, h7d, h7c = t7
        radj10, h10d, _ = t10
        delta = radj10 - radj7
        extend_wins = delta > 0

        green7 = expert_green(sid, h7d)
        soft_or7 = soft_or_incl(sid, h7d)
        soft_half7 = soft_half_incl(sid, h7d)
        heavy_sell7 = specialist_heavy_sell(sid, h7d)
        no_heavy_sell = not heavy_sell7
        ts = chip[sid].get(h7d)
        ts2 = (ts is not None) and (ts >= 2)
        sma = sma5_at(sid, h7d)
        above_sma5 = (sma is not None) and (h7c > sma)
        # unrealized already positive at H7
        itm = radj7 > 0
        # strong unrealized (≥ median-ish +4%)
        itm_strong = radj7 >= 0.04
        # still green OR soft half
        softish = green7 or soft_half7

        rows.append(
            {
                "sid": sid,
                "sig": sig,
                "entry": path_cals[sid][entry_i],
                "h7": h7d,
                "h10": h10d,
                "radj7": radj7,
                "radj10": radj10,
                "delta": delta,
                "extend_wins": extend_wins,
                "oos": sig >= OOS,
                "feat_green7": green7,
                "feat_soft_or7": soft_or7,
                "feat_soft_half7": soft_half7,
                "feat_softish7": softish,
                "feat_no_heavy_sell7": no_heavy_sell,
                "feat_ts2": ts2,
                "feat_above_sma5": above_sma5,
                "feat_itm": itm,
                "feat_itm_strong": itm_strong,
                "feat_green7_and_itm": green7 and itm,
                "feat_green7_and_above_sma5": green7 and above_sma5,
                "feat_ts2_and_itm": ts2 and itm,
                "feat_no_sell_and_itm": no_heavy_sell and itm,
                "feat_softish_and_itm": softish and itm,
                "three_streak": ts,
            }
        )
    print(f"H7-complete with H10: n={len(rows)}")

    base_delta = [r["delta"] for r in rows]
    base_oos = [r["delta"] for r in rows if r["oos"]]
    base_win = sum(1 for r in rows if r["extend_wins"]) / len(rows) if rows else None
    print(
        f"unconditional Δ(H10−H7): ALL med {pct(median(base_delta) if base_delta else None)} "
        f"· P(extend>) {win_pct(base_win)} · OOS med "
        f"{pct(median(base_oos) if base_oos else None)}"
    )

    feat_names = [
        ("green7", "feat_green7", "H7 仍 hard 綠燈"),
        ("soft_or7", "feat_soft_or7", "H7 softOR（≥1 core@floor）"),
        ("soft_half7", "feat_soft_half7", "H7 softHalfIncl（冠軍結構@floor/2）"),
        ("softish7", "feat_softish7", "H7 green ∨ softHalf"),
        ("no_heavy_sell7", "feat_no_heavy_sell7", "H7 無專家重賣（net≤−floor）"),
        ("ts2", "feat_ts2", "H7 three_streak≥2"),
        ("above_sma5", "feat_above_sma5", "H7 close > SMA5"),
        ("itm", "feat_itm", "H7 unrealized r_adj > 0"),
        ("itm_strong", "feat_itm_strong", "H7 unrealized r_adj ≥ +4%"),
        ("green7∧itm", "feat_green7_and_itm", "綠燈 ∧ ITM"),
        ("green7∧>SMA5", "feat_green7_and_above_sma5", "綠燈 ∧ close>SMA5"),
        ("ts2∧itm", "feat_ts2_and_itm", "ts≥2 ∧ ITM"),
        ("noSell∧itm", "feat_no_sell_and_itm", "無重賣 ∧ ITM"),
        ("softish∧itm", "feat_softish_and_itm", "softish ∧ ITM"),
    ]

    def slice_stats(flag_key: str, on: bool) -> dict:
        sub = [r for r in rows if bool(r[flag_key]) is on]
        oos = [r for r in sub if r["oos"]]
        deltas = [r["delta"] for r in sub]
        oos_d = [r["delta"] for r in oos]
        b = blk(deltas)
        ob = blk(oos_d)
        return {
            "n": b["n"],
            "med_delta": b["med"],
            "mean_delta": b["mean"],
            "p_extend": b["win"],
            "oos_n": ob["n"],
            "oos_med_delta": ob["med"],
            "oos_p_extend": ob["win"],
            "cover": (b["n"] / len(rows)) if rows else None,
            # also absolute exit levels for reading
            "med_radj7": median([r["radj7"] for r in sub]) if sub else None,
            "med_radj10": median([r["radj10"] for r in sub]) if sub else None,
            "oos_med_radj7": median([r["radj7"] for r in oos]) if oos else None,
            "oos_med_radj10": median([r["radj10"] for r in oos]) if oos else None,
        }

    results: list[dict] = []
    for short, key, label in feat_names:
        on = slice_stats(key, True)
        off = slice_stats(key, False)
        d_all = None
        d_oos = None
        if on["med_delta"] is not None and off["med_delta"] is not None:
            d_all = on["med_delta"] - off["med_delta"]
        if on["oos_med_delta"] is not None and off["oos_med_delta"] is not None:
            d_oos = on["oos_med_delta"] - off["oos_med_delta"]
        clears = (
            on["oos_n"] >= MIN_OOS_N
            and off["oos_n"] >= MIN_OOS_N
            and (on["cover"] or 0) >= MIN_COVER
            and d_oos is not None
            and d_all is not None
            and d_oos >= FREEZE_DELTA_OOS
            and d_all >= FREEZE_ALL_FLOOR
        )
        results.append(
            {
                "id": short,
                "key": key,
                "label": label,
                "on": on,
                "off": off,
                "d_all": d_all,
                "d_oos": d_oos,
                "clears_freeze": clears,
            }
        )
        print(
            f"  {short}: ON n={on['n']} medΔ={pct(on['med_delta'])} "
            f"OOS={pct(on['oos_med_delta'])} | "
            f"OFF n={off['n']} medΔ={pct(off['med_delta'])} "
            f"OOS={pct(off['oos_med_delta'])} | "
            f"ΔOOS(on−off)={pct(d_oos)} freeze={clears}"
        )

    # Rank by OOS (on−off) then ALL
    ranked = sorted(
        results,
        key=lambda x: (
            x["d_oos"] is not None,
            x["d_oos"] if x["d_oos"] is not None else -9e9,
            x["d_all"] if x["d_all"] is not None else -9e9,
        ),
        reverse=True,
    )
    frozen = [r for r in ranked if r["clears_freeze"]]
    if frozen:
        freeze = (
            f"weak candidate `{frozen[0]['id']}` clears bar — still research-only; "
            "do NOT change L1H7 SSOT / Order."
        )
        verdict_line = (
            f"**Freeze candidate**：`{frozen[0]['id']}`（{frozen[0]['label']}）· "
            f"ON OOS med Δ {pct(frozen[0]['on']['oos_med_delta'])} vs OFF "
            f"{pct(frozen[0]['off']['oos_med_delta'])} "
            f"（on−off {pct(frozen[0]['d_oos'])}）."
        )
    else:
        freeze = (
            "no H7-evening feature clears freeze bar for extend-to-H10; "
            "keep L1H7 exit as default (H10 remains unconditional research note)."
        )
        verdict_line = (
            "**Nothing freezes.** No PIT feature at H7 evening reliably predicts "
            "that holding to H10 beats exiting at H7 under the promote bar."
        )

    # Unconditional H10 vs H7 (from hold-exit alignment)
    u_h7 = blk([r["radj7"] for r in rows])
    u_h10 = blk([r["radj10"] for r in rows])
    u_h7_oos = blk([r["radj7"] for r in rows if r["oos"]])
    u_h10_oos = blk([r["radj10"] for r in rows if r["oos"]])

    # ---- markdown ----
    lines: list[str] = []
    lines.append("# H_GREEN_H7_EXTEND · Day-7 signals to hold past L1H7")
    lines.append("")
    lines.append(f"- 生成：{datetime.now():%Y-%m-%d %H:%M}")
    lines.append("- Research only · sqlite `mode=ro` · **未採納** Strategy／Order")
    lines.append("- Runner：`scripts/research/run_h_green_h7_extend.py`")
    lines.append("- Align：`H_GREEN_HOLD_EXIT` universe／cost／β／OOS／dedupe")
    lines.append("")
    lines.append("## Verdict")
    lines.append("")
    lines.append(
        f"**Sample**：H7-complete ∩ H10-available n={len(rows)} · "
        f"OOS n={sum(1 for r in rows if r['oos'])}."
    )
    lines.append("")
    lines.append(
        f"**Unconditional Δ(H10−H7)**：ALL med {pct(blk(base_delta)['med'])} · "
        f"P(Δ>0) {win_pct(base_win)} · OOS med {pct(blk(base_oos)['med'])}."
    )
    lines.append("")
    lines.append(
        f"**Exit levels（same trades）**：L1H7 ALL med {pct(u_h7['med'])} → "
        f"L1H10 {pct(u_h10['med'])}；OOS {pct(u_h7_oos['med'])} → "
        f"{pct(u_h10_oos['med'])}."
    )
    lines.append("")
    lines.append(verdict_line)
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
        "| label | 同筆交易 "
        + r"\(\Delta = r_{\mathrm{adj}}^{\mathrm{H10}} - r_{\mathrm{adj}}^{\mathrm{H7}}\)"
        + "；extend wins iff Δ>0 |"
    )
    lines.append(
        "| features | **H7 收盤日晚**（FinMind EOD · 與綠燈同可觀測時點）|"
    )
    lines.append(
        f"| freeze bar | OOS medΔ(ON−OFF) ≥ +{FREEZE_DELTA_OOS*100:.1f}pp · "
        f"ALL ≥ {FREEZE_ALL_FLOOR*100:.0f}pp · "
        f"両側 OOS n≥{MIN_OOS_N} · ON cover≥{MIN_COVER*100:.0f}% |"
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
    lines.append("## Feature table（ON vs OFF · med Δ H10−H7）")
    lines.append("")
    lines.append(
        "| feature | ON n | ON medΔ | ON OOSΔ | OFF n | OFF medΔ | OFF OOSΔ | "
        "ΔALL(on−off) | ΔOOS | cover | freeze? |"
    )
    lines.append(
        "|---------|-----:|--------:|--------:|------:|---------:|---------:|"
        "-------------:|------:|------:|:--------:|"
    )
    for r in ranked:
        on, off = r["on"], r["off"]
        lines.append(
            f"| {r['id']} · {r['label']} | {on['n']} | {pct(on['med_delta'])} | "
            f"{pct(on['oos_med_delta'])} | {off['n']} | {pct(off['med_delta'])} | "
            f"{pct(off['oos_med_delta'])} | {pct(r['d_all'])} | {pct(r['d_oos'])} | "
            f"{frac_pct(on['cover'])} | {'Y' if r['clears_freeze'] else '—'} |"
        )
    lines.append("")
    lines.append("## Reading")
    lines.append("")
    lines.append(
        "- **Unconditional**：H10 仍略優於 H7（對齊 H_GREEN_HOLD_EXIT），但這是"
        "「一律拉長」，不是「有條件才延長」。"
    )
    lines.append(
        "- **條件延長**：若 ON 子集的 medΔ 不顯著高於 OFF，則該特徵**不能**當"
        "「H7 當天決定續抱」的理由。"
    )
    if not frozen:
        lines.append(
            "- **結論**：H7 晚可見特徵（續綠／軟共識／籌碼 streak／無重賣／均線／浮盈）"
            "**皆未過凍結門**；操作上仍依 L1H7 出場，H10 只作研究註記。"
        )
    else:
        top = frozen[0]
        lines.append(
            f"- **弱候選** `{top['id']}`：僅過研究門檻；勿寫 strategy／Order；"
            "需獨立 OOS 複驗後才討論 promote。"
        )
    lines.append("")
    lines.append("## Freeze recommendation")
    lines.append("")
    lines.append(f"**{freeze}**")
    lines.append("")
    lines.append("- 不寫 `config/strategy.yaml`／不進 Order。")
    lines.append("- 協議持有仍以 **L1H7** 為 SSOT。")
    lines.append("")
    lines.append("## Caveats")
    lines.append("")
    lines.append("- 單槽、無容量；Δ 中位不可當複利權益。")
    lines.append(
        "- H7 晚特徵用當日 EOD（分點／籌碼／收盤）；與 live 20:00 觀測對齊，"
        "但非盤中可交易訊號。"
    )
    lines.append(
        "- `no_heavy_sell` 定義＝無 core seat 淨賣金額 ≥floor；小額賣出不算。"
    )
    lines.append("- three_streak 缺值日視為 ts2=False。")
    lines.append("")
    lines.append("## Reproduction")
    lines.append("")
    lines.append("```bash")
    lines.append(
        "PYTHONPATH=src .venv/bin/python scripts/research/run_h_green_h7_extend.py"
    )
    lines.append("```")
    lines.append("")

    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.write_text("\n".join(lines), encoding="utf-8")
    payload = {
        "generated": datetime.now().isoformat(timespec="seconds"),
        "n": len(rows),
        "oos_n": sum(1 for r in rows if r["oos"]),
        "unconditional": {
            "med_delta": blk(base_delta)["med"],
            "p_extend": base_win,
            "oos_med_delta": blk(base_oos)["med"],
            "l1h7": u_h7,
            "l1h10": u_h10,
            "l1h7_oos": u_h7_oos,
            "l1h10_oos": u_h10_oos,
        },
        "freeze": freeze,
        "frozen_ids": [r["id"] for r in frozen],
        "features": results,
        "trades_sample": rows[:5],
    }
    OUT_JSON.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {OUT_MD}")
    print(f"wrote {OUT_JSON}")
    print(f"FREEZE: {freeze}")


if __name__ == "__main__":
    main()
