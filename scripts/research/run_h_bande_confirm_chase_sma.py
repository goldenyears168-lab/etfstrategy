#!/usr/bin/env python3
"""H_B∧E: yellow light2 → confirm≥0.5億 ≤2d ∧ chase≤3% ∧ SMA5≤+8% → L1H7.

Research only · sqlite mode=ro · frozen protocol = H_E (14 thick hard≥3 ex-mega).

  PYTHONPATH=src .venv/bin/python scripts/research/run_h_bande_confirm_chase_sma.py
"""
from __future__ import annotations

import json
import sqlite3
import sys
import importlib.util
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from statistics import mean, median

ROOT = Path(__file__).resolve().parents[2]
OUT_MD = (
    ROOT
    / "reports/research/branch-footprint-screen/expert_pool/hypotheses"
    / "H_BandE_confirm_chase_sma.md"
)
DB = ROOT / "data" / "stocks.db"
SOURCE = "finmind"
START, END, OOS = "2024-07-01", "2026-07-17", "2026-01-01"
TOP10 = ["5850", "779Z", "779c", "9216", "9A81", "8840", "5854", "9268", "9227", "1260"]
LIGHT_LO, LIGHT_HI, HEAVY, SEMI = 5e6, 5e7, 1e8, 5e7
COST, HOLD, BETA, DEDUP = 0.003, 7, 1.15, 5
CHASE_MAX, SMA_EXT = 0.03, 0.08
CONFIRM_WAIT = 2
LIMIT_MULT = 1.01
LIMIT_DAYS = 3

UNIVERSE = [
    "2327", "2337", "2344", "2383", "2408", "3037", "3189",
    "3443", "3481", "3653", "3665", "6223", "8046", "8358",
]
NAMES = {
    "2327": "國巨", "2337": "旺宏", "2344": "華邦電", "2383": "台光電", "2408": "南亞科",
    "3037": "欣興", "3189": "景碩", "3443": "創意", "3481": "群創", "3653": "健策",
    "3665": "貿聯-KY", "6223": "旺矽", "8046": "南電", "8358": "金居",
}


def connect_ro():
    c = sqlite3.connect(f"file:{DB.resolve()}?mode=ro", uri=True, timeout=120)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=120000")
    return c


def load_champions():
    spec = importlib.util.spec_from_file_location(
        "epw", ROOT / "scripts/research/run_expert_pool_watch.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["epw"] = mod
    assert spec.loader is not None
    spec.loader.exec_module(mod)

    cores: dict[str, dict[str, str]] = {}
    floors: dict[str, float] = {}
    modes: dict[str, str] = {}
    for sid in UNIVERSE:
        mode, floor, core = "回看1日共識", 5e7, {}
        ws = ROOT / "reports/research/branch-footprint-screen/expert_pool" / sid / "watch_spec.json"
        if ws.exists():
            w = json.loads(ws.read_text(encoding="utf-8"))
            core = {str(k): str(v) for k, v in (w.get("core") or {}).items()}
            floor = float(w.get("net_floor") or 5e7)
            fl = w.get("floor_label") or ""
            if "1億" in fl:
                floor = float(w.get("net_floor") or 1e8)
            mode = (w.get("champion") or {}).get("mode") or mode
        if sid in mod.POOLS:
            p = mod.POOLS[sid]
            if not core:
                core = {str(k): str(v) for k, v in p.core.items()}
            floor = float(p.net_floor)
        if sid == "2344":
            mode = "同日共識"
        cores[sid] = core
        floors[sid] = floor
        modes[sid] = mode
    return cores, floors, modes


def pct(x: float | None) -> str:
    if x is None:
        return "—"
    return f"{x * 100:+.2f}%"


def summarize(trades: list[dict]) -> dict:
    if not trades:
        return dict(
            n=0, med=None, mean=None, win=None, med_r=None,
            oos_n=0, oos_med=None, oos_win=None, is_n=0, is_med=None, is_win=None,
        )
    xs = [t["radj"] for t in trades]
    rs = [t["ret"] for t in trades]
    oos = [t for t in trades if t["oos"]]
    is_ = [t for t in trades if not t["oos"]]

    def blk(ts):
        if not ts:
            return 0, None, None
        a = [t["radj"] for t in ts]
        return len(ts), median(a), sum(1 for x in a if x > 0) / len(a)

    oos_n, oos_med, oos_win = blk(oos)
    is_n, is_med, is_win = blk(is_)
    return dict(
        n=len(trades),
        med=median(xs),
        mean=mean(xs),
        win=sum(1 for x in xs if x > 0) / len(xs),
        med_r=median(rs),
        oos_n=oos_n,
        oos_med=oos_med,
        oos_win=oos_win,
        is_n=is_n,
        is_med=is_med,
        is_win=is_win,
    )


def main() -> None:
    cores, floors, modes = load_champions()
    con = connect_ro()
    ph = ",".join("?" * len(UNIVERSE))

    # OHLC (need pad for SMA5)
    bars: dict[str, dict[str, tuple[float, float, float, float]]] = defaultdict(dict)
    for r in con.execute(
        f"""SELECT stock_id, trade_date, open, high, low, close FROM stock_daily_bars
            WHERE source=? AND stock_id IN ({ph})
              AND trade_date BETWEEN date(?, '-40 days') AND date(?, '+40 days')
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
           WHERE code='IX0001' AND date BETWEEN date(?, '-40 days') AND date(?, '+40 days')
             AND open>0 AND close>0
           ORDER BY date,
             CASE source WHEN 'yahoo' THEN 0 WHEN 'tej' THEN 1 WHEN 'finmind' THEN 2 ELSE 3 END""",
        (START, END),
    ):
        ix.setdefault(r["date"], (float(r["open"]), float(r["close"])))
    ix_dates = sorted(ix)

    # Top10 tape (amount = net * close)
    by: dict[str, dict[str, dict[str, float]]] = defaultdict(lambda: defaultdict(dict))
    ph_t = ",".join("?" * len(TOP10))
    for r in con.execute(
        f"""SELECT stock_id, trade_date, securities_trader_id, net
            FROM stock_broker_branch_daily
            WHERE source=? AND stock_id IN ({ph}) AND securities_trader_id IN ({ph_t})
              AND trade_date BETWEEN ? AND ? AND net!=0""",
        (SOURCE, *UNIVERSE, *TOP10, START, END),
    ):
        oc = bars[r["stock_id"]].get(r["trade_date"])
        if oc:
            by[r["stock_id"]][r["trade_date"]][r["securities_trader_id"]] = (
                float(r["net"]) * oc[3]
            )

    # Core tape for green
    core_tids = set().union(*(set(c) for c in cores.values()))
    core_by: dict[str, dict[str, dict[str, float]]] = defaultdict(lambda: defaultdict(dict))
    if core_tids:
        ph_c = ",".join("?" * len(core_tids))
        for r in con.execute(
            f"""SELECT stock_id, trade_date, securities_trader_id, net
                FROM stock_broker_branch_daily
                WHERE source=? AND stock_id IN ({ph}) AND securities_trader_id IN ({ph_c})
                  AND trade_date BETWEEN ? AND ? AND net>0""",
            (SOURCE, *UNIVERSE, *core_tids, START, END),
        ):
            oc = bars[r["stock_id"]].get(r["trade_date"])
            if oc and r["securities_trader_id"] in cores[r["stock_id"]]:
                core_by[r["stock_id"]][r["trade_date"]][r["securities_trader_id"]] = (
                    float(r["net"]) * oc[3]
                )
    con.close()

    cals = {sid: sorted(d for d in bars[sid] if START <= d <= END) for sid in UNIVERSE}
    full_cals = {sid: sorted(bars[sid]) for sid in UNIVERSE}
    idx = {sid: {d: i for i, d in enumerate(cals[sid])} for sid in UNIVERSE}

    def day_stats(sid: str, d: str) -> dict:
        m = by[sid].get(d, {})
        light = {t for t, a in m.items() if LIGHT_LO <= a < LIGHT_HI}
        n_heavy = sum(1 for a in m.values() if a >= HEAVY)
        n_semi = sum(1 for a in m.values() if a >= SEMI)
        return dict(n_light=len(light), n_heavy=n_heavy, n_semi=n_semi)

    def yellow_light2(sid: str, i: int) -> bool:
        if i < 1:
            return False
        cal = cals[sid]
        s1, s2 = day_stats(sid, cal[i - 1]), day_stats(sid, cal[i])
        if s1["n_heavy"] or s2["n_heavy"]:
            return False
        return s1["n_light"] >= 2 and s2["n_light"] >= 2

    def expert_green(sid: str, d: str) -> bool:
        core = cores[sid]
        if not core:
            return False
        floor = floors[sid]
        mode = modes[sid]
        i = idx[sid].get(d)
        if i is None:
            return False
        today = {t for t, a in core_by[sid].get(d, {}).items() if a >= floor}
        yday: set[str] = set()
        if i > 0:
            yd = cals[sid][i - 1]
            yday = {t for t, a in core_by[sid].get(yd, {}).items() if a >= floor}
        if "OR" in mode:
            return len(today) >= 1
        if "同日" in mode:
            return len(today) >= 2
        return len(today) >= 1 and len(today | yday) >= 2

    def sma5_through(sid: str, d: str) -> float | None:
        cal = [x for x in full_cals[sid] if x <= d]
        if len(cal) < 5:
            return None
        closes = [bars[sid][x][3] for x in cal[-5:]]
        return sum(closes) / 5.0

    def dedupe_ok(last: dict[str, str], sid: str, d: str) -> bool:
        if sid not in last:
            return True
        return (
            datetime.strptime(d, "%Y-%m-%d") - datetime.strptime(last[sid], "%Y-%m-%d")
        ).days > DEDUP

    def find_ix_idx(d: str) -> int | None:
        for k, dd in enumerate(ix_dates):
            if dd >= d:
                return k
        return None

    def trade_from_entry(sid: str, entry_d: str, entry_px: float) -> dict | None:
        cal = cals[sid]
        if entry_d not in idx[sid]:
            # entry might be just after END pad — use full cal
            fcal = full_cals[sid]
            if entry_d not in fcal:
                return None
            ei = fcal.index(entry_d)
            if ei + HOLD - 1 >= len(fcal):
                return None
            xd = fcal[ei + HOLD - 1]
        else:
            ei = idx[sid][entry_d]
            if ei + HOLD - 1 >= len(cal):
                return None
            xd = cal[ei + HOLD - 1]
        if xd not in bars[sid] or entry_px <= 0:
            return None
        xc = bars[sid][xd][3]
        ret = xc / entry_px - 1 - COST
        ji = find_ix_idx(entry_d)
        xj = find_ix_idx(xd)
        if ji is None or xj is None:
            return None
        bo = ix[ix_dates[ji]][0]
        bc = ix[ix_dates[xj]][1]
        if bo <= 0:
            return None
        radj = ret - BETA * (bc / bo - 1)
        return dict(ret=ret, radj=radj, entry=entry_d, exit=xd, entry_px=entry_px)

    def next_open_day(sid: str, sig_i: int) -> str | None:
        cal = cals[sid]
        if sig_i + 1 >= len(cal):
            return None
        return cal[sig_i + 1]

    def gates_at_open(sid: str, ref_close_d: str, entry_d: str) -> dict:
        """chase = open/ref_close−1; SMA5 through ref_close_d; ext = open/SMA5−1 (H_B)."""
        eo = bars[sid][entry_d][0]
        ref_c = bars[sid][ref_close_d][3]
        sma = sma5_through(sid, ref_close_d)
        chase = eo / ref_c - 1 if ref_c > 0 else None
        ext = (eo / sma - 1) if sma and sma > 0 else None
        pass_chase = chase is not None and chase <= CHASE_MAX
        pass_sma = ext is not None and ext <= SMA_EXT
        return dict(
            chase=chase,
            ext=ext,
            sma5=sma,
            pass_chase=pass_chase,
            pass_sma=pass_sma,
            pass_and=pass_chase and pass_sma,
            open=eo,
            ref_close=ref_c,
        )

    def try_limit_fill(sid: str, conf_i: int, limit_px: float) -> tuple[str, float] | None:
        """Mirror live soft-exception: hang limit confirm_close×1.01 ≤3 sessions from T+1."""
        cal = cals[sid]
        # start from confirm+1
        for k in range(1, LIMIT_DAYS + 1):
            ei = conf_i + k
            if ei >= len(cal):
                break
            d = cal[ei]
            o, hi, lo, _c = bars[sid][d]
            # fill if open already ≤ limit, else if low touches limit
            if o <= limit_px:
                return d, o
            if lo <= limit_px <= hi:
                return d, limit_px
        return None

    # --- yellow events ---
    yellow_events: list[tuple[str, int, str]] = []
    for sid in UNIVERSE:
        cal = cals[sid]
        for i in range(1, len(cal)):
            if yellow_light2(sid, i):
                yellow_events.append((sid, i, cal[i]))

    def find_confirm(sid: str, i: int, wait: int = CONFIRM_WAIT) -> int | None:
        cal = cals[sid]
        for j in range(1, wait + 1):
            if i + j >= len(cal):
                break
            if day_stats(sid, cal[i + j])["n_semi"] >= 1:
                return i + j
        return None

    # ========== Arms ==========
    arms: dict[str, list[dict]] = {}

    # 1) Naked yellow → T+1 open H7
    trades = []
    last: dict[str, str] = {}
    for sid, i, d in yellow_events:
        if not dedupe_ok(last, sid, d):
            continue
        ed = next_open_day(sid, i)
        if not ed:
            continue
        tr = trade_from_entry(sid, ed, bars[sid][ed][0])
        if not tr:
            continue
        g = gates_at_open(sid, d, ed)
        trades.append(dict(sid=sid, sig=d, oos=d >= OOS, arm="Y", **tr, **g))
        last[sid] = d
    arms["1_Y_naked"] = trades

    # 2) E only: confirm≤2d → next open H7
    trades = []
    last = {}
    for sid, i, d in yellow_events:
        if not dedupe_ok(last, sid, d):
            continue
        conf_i = find_confirm(sid, i)
        if conf_i is None:
            continue
        ed = next_open_day(sid, conf_i)
        if not ed:
            continue
        conf_d = cals[sid][conf_i]
        tr = trade_from_entry(sid, ed, bars[sid][ed][0])
        if not tr:
            continue
        g = gates_at_open(sid, conf_d, ed)  # gates vs confirm close (diagnostic)
        trades.append(
            dict(
                sid=sid, sig=d, conf=conf_d, lag=conf_i - i,
                oos=d >= OOS, arm="E", **tr, **g,
            )
        )
        last[sid] = d
    arms["2_E_confirm"] = trades

    # 3) B only on yellow: chase∧SMA at T+1 open
    trades = []
    last = {}
    for sid, i, d in yellow_events:
        if not dedupe_ok(last, sid, d):
            continue
        ed = next_open_day(sid, i)
        if not ed:
            continue
        g = gates_at_open(sid, d, ed)
        if not g["pass_and"]:
            continue
        tr = trade_from_entry(sid, ed, bars[sid][ed][0])
        if not tr:
            continue
        trades.append(dict(sid=sid, sig=d, oos=d >= OOS, arm="B", **tr, **g))
        last[sid] = d
    arms["3_B_yellow"] = trades

    # 4a) B∧E hard AND at confirm+1 open (PRIMARY)
    trades = []
    last = {}
    n_conf = n_gate_fail = 0
    for sid, i, d in yellow_events:
        if not dedupe_ok(last, sid, d):
            continue
        conf_i = find_confirm(sid, i)
        if conf_i is None:
            continue
        n_conf += 1
        conf_d = cals[sid][conf_i]
        ed = next_open_day(sid, conf_i)
        if not ed:
            continue
        g = gates_at_open(sid, conf_d, ed)
        if not g["pass_and"]:
            n_gate_fail += 1
            continue
        tr = trade_from_entry(sid, ed, bars[sid][ed][0])
        if not tr:
            continue
        trades.append(
            dict(
                sid=sid, sig=d, conf=conf_d, lag=conf_i - i,
                oos=d >= OOS, arm="BandE_hard", fill="open_and", **tr, **g,
            )
        )
        last[sid] = d
    arms["4_BandE_hard"] = trades
    bande_gate_stats = dict(n_conf_after_dedupe=n_conf, n_gate_fail=n_gate_fail, n_pass=len(trades))

    # 4b) B∧E soft-exception mirror: AND → open; else if SMA ok → open; else limit×1.01 ≤3d
    trades = []
    last = {}
    fill_counts = defaultdict(int)
    for sid, i, d in yellow_events:
        if not dedupe_ok(last, sid, d):
            continue
        conf_i = find_confirm(sid, i)
        if conf_i is None:
            continue
        conf_d = cals[sid][conf_i]
        conf_c = bars[sid][conf_d][3]
        ed = next_open_day(sid, conf_i)
        if not ed:
            continue
        g = gates_at_open(sid, conf_d, ed)
        entry_d = None
        entry_px = None
        fill = None
        if g["pass_and"]:
            entry_d, entry_px, fill = ed, bars[sid][ed][0], "open_and"
        elif g["pass_sma"]:
            # soft exception: open already within SMA5≤+8%
            entry_d, entry_px, fill = ed, bars[sid][ed][0], "open_sma_soft"
        else:
            limit_px = conf_c * LIMIT_MULT
            filled = try_limit_fill(sid, conf_i, limit_px)
            if filled:
                entry_d, entry_px = filled
                fill = "limit_1pct_le3d"
            else:
                fill_counts["abandon"] += 1
                continue
        tr = trade_from_entry(sid, entry_d, entry_px)
        if not tr:
            continue
        fill_counts[fill] += 1
        trades.append(
            dict(
                sid=sid, sig=d, conf=conf_d, lag=conf_i - i,
                oos=d >= OOS, arm="BandE_soft", fill=fill, **tr, **g,
            )
        )
        last[sid] = d
    arms["4b_BandE_soft"] = trades

    # 5) Green only → T+1 open L1H7
    trades = []
    last = {}
    green_events: list[tuple[str, str]] = []
    for sid in UNIVERSE:
        for d in cals[sid]:
            if expert_green(sid, d):
                green_events.append((sid, d))
    for sid, d in green_events:
        if not dedupe_ok(last, sid, d):
            continue
        i = idx[sid][d]
        ed = next_open_day(sid, i)
        if not ed:
            continue
        tr = trade_from_entry(sid, ed, bars[sid][ed][0])
        if not tr:
            continue
        trades.append(dict(sid=sid, sig=d, oos=d >= OOS, arm="G", **tr))
        last[sid] = d
    arms["5_GREEN"] = trades

    # 6) Overlap / capacity: B∧E hard vs green
    # early = BandE entry date strictly before any green signal date in [yellow, BandE entry]
    # extra = BandE trades whose yellow→entry window has no green yet at BandE signal (confirm) day
    bande = arms["4_BandE_hard"]
    green_by_sid: dict[str, set[str]] = defaultdict(set)
    for sid, d in green_events:
        green_by_sid[sid].add(d)

    n_early = 0
    n_overlap_same_week = 0
    early_trades = []
    for t in bande:
        sid, conf = t["sid"], t["conf"]
        # green on or before confirm day in window after yellow?
        greens_before_entry = [
            g for g in green_by_sid[sid]
            if t["sig"] < g <= t["entry"]
        ]
        if not greens_before_entry:
            n_early += 1
            early_trades.append(t)
        # overlap if any green within 5 calendar days of confirm
        conf_dt = datetime.strptime(conf, "%Y-%m-%d")
        for g in green_by_sid[sid]:
            gd = datetime.strptime(g, "%Y-%m-%d")
            if abs((gd - conf_dt).days) <= 5:
                n_overlap_same_week += 1
                break

    early_sum = summarize(early_trades)

    # Also: BandE that never get green within 10 sessions after yellow
    never_green = []
    for t in bande:
        sid, i = t["sid"], idx[t["sid"]][t["sig"]]
        cal = cals[sid]
        hit = False
        for j in range(1, 11):
            if i + j >= len(cal):
                break
            if expert_green(sid, cal[i + j]):
                hit = True
                break
        if not hit:
            never_green.append(t)

    # stats table
    labels = {
        "1_Y_naked": "1 Naked yellow → T+1 open H7",
        "2_E_confirm": "2 E only: confirm≤2d → next open H7",
        "3_B_yellow": "3 B only: yellow ∧ chase∧SMA → T+1 H7",
        "4_BandE_hard": "4 B∧E hard AND @ confirm+1 open",
        "4b_BandE_soft": "4b B∧E soft-exception (SMA open / limit≤3d)",
        "5_GREEN": "5 GREEN → T+1 open L1H7",
    }
    stats = {k: summarize(v) for k, v in arms.items()}

    # gap to green
    g_med = stats["5_GREEN"]["med"]
    g_oos = stats["5_GREEN"]["oos_med"]

    def gap(arm_key: str) -> tuple[float | None, float | None]:
        s = stats[arm_key]
        dg = None if s["med"] is None or g_med is None else s["med"] - g_med
        do = None if s["oos_med"] is None or g_oos is None else s["oos_med"] - g_oos
        return dg, do

    # conversion rates
    n_yellow_raw = len(yellow_events)
    n_y_dedup = stats["1_Y_naked"]["n"]
    # recompute confirm conversion on same dedupe as E
    n_yellow_considered = 0
    n_got_confirm = 0
    last = {}
    for sid, i, d in yellow_events:
        if not dedupe_ok(last, sid, d):
            continue
        n_yellow_considered += 1
        if find_confirm(sid, i) is not None:
            n_got_confirm += 1
        last[sid] = d

    # write report
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    lines = []
    lines.append("# H_B∧E · Yellow confirm × chase∧SMA5 vs Green")
    lines.append("")
    lines.append(f"- 生成：{now}")
    lines.append("- Research only · sqlite `mode=ro` · **未採納** Strategy／Order")
    lines.append(
        f"- 協議＝H_E：窗 `{START}`..`{END}` · OOS≥`{OOS}` · L1H7 · 30bps · "
        r"$r_{\mathrm{adj}}=r-1.15\times IX0001$ · 去重 5 曆日"
    )
    lines.append(
        f"- 宇宙 thick hard≥3 ex-mega n=14：{', '.join(UNIVERSE)}"
    )
    lines.append(f"- Top10：{','.join(TOP10)}")
    lines.append(
        "- 黃燈 light2：連續 2 日各 ≥2 家 light（淨買金額 ∈[500萬, 0.5億)），兩日皆無 ≥1億"
    )
    lines.append(
        "- 確認 E：黃燈後 **1–2** 個交易日內任一 Top10 淨買 ≥0.5億 → **確認日次日開**"
    )
    lines.append("")
    lines.append("## Formulas（對齊 H_B）")
    lines.append("")
    lines.append("| 鍵 | 公式 |")
    lines.append("|----|------|")
    lines.append("| chase | `entry_open / ref_close − 1` · 門檻 ≤ **+3%** |")
    lines.append("| SMA5 | PIT：`ref_close` 日含以前共 5 根收盤均 |")
    lines.append("| SMA5≤+8% | `entry_open / SMA5 − 1` ≤ **+8%**（≡ open ≤ SMA5×1.08） |")
    lines.append("| B on yellow | `ref_close` = 黃燈訊號日收 |")
    lines.append("| B∧E | `ref_close` = **確認日收**（進場決策日） |")
    lines.append("| B∧E hard（主臂） | 確認後次日開 **僅當 chase∧SMA 皆過**；否則 **skip** |")
    lines.append(
        "| B∧E soft（對照） | AND→開；僅 SMA 過→開（軟例外）；否則限價 "
        f"確認收×{LIMIT_MULT:.2f} 掛 ≤{LIMIT_DAYS} 日，未成則放棄 |"
    )
    lines.append("")

    # Verdict
    be = stats["4_BandE_hard"]
    e = stats["2_E_confirm"]
    g = stats["5_GREEN"]
    b = stats["3_B_yellow"]
    y = stats["1_Y_naked"]
    soft = stats["4b_BandE_soft"]

    lines.append("## Verdict")
    lines.append("")
    lines.append("**Does not close green gap · B gate redundant on H_E yellow.**")
    lines.append(
        f"B∧E hard 全期 med **{pct(be['med'])}** ≈ E alone **{pct(e['med'])}**，"
        f"OOS 反而略差（**{pct(be['oos_med'])}** vs E **{pct(e['oos_med'])}**）；"
        f"綠燈仍是 **{pct(g['med'])} / OOS {pct(g['oos_med'])}**。"
    )
    lines.append(
        "對齊 H_B 敏感度：在「light2 ∩ 兩日無≥1億」宇宙，追價∧SMA **幾乎無增益**（甚至微傷 OOS）。"
    )
    lines.append("")
    lines.append("| 問題 | 答案 |")
    lines.append("|------|------|")
    dg_all, dg_oos = gap("4_BandE_hard")
    lines.append(
        f"| 能否逼近綠燈中位？ | **否**（全期差 {pct(dg_all)}；OOS 差 {pct(dg_oos)}） |"
    )
    early_pct = (100 * n_early / be["n"]) if be["n"] else 0
    lines.append(
        f"| 能否提早一段且仍有綠燈級報酬？ | **否**：{early_pct:.0f}% early med "
        f"**{pct(early_sum['med'])}**／OOS **{pct(early_sum['oos_med'])}**；"
        f"永不轉綠子集 med **{pct(summarize(never_green)['med'])}** |"
    )
    lines.append(
        "| 實務凍結？ | 保留 **E（confirm≤2d→次日開）**；**不要**在 H_E 黃燈上再疊 B∧；"
        "B 只當 live 註記（防死追），不當研究 alpha 濾鏡 |"
    )
    lines.append("")
    if be["n"]:
        lines.append(
            f"早期段：B∧E hard 中 **{n_early}/{be['n']}**（{early_pct:.0f}%）"
            f" 在進場前尚未出現綠燈；該子集 med {pct(early_sum['med'])} · "
            f"OOS n={early_sum['oos_n']} med {pct(early_sum['oos_med'])} → 提早量多、質弱。"
        )
    lines.append("")
    lines.append("## Main table")
    lines.append("")
    lines.append(
        "| arm | n | med $r_{adj}$ | mean | win | med r | IS n/med | OOS n/med/win | Δmed vs green |"
    )
    lines.append("|-----|--:|---:|---:|---:|---:|---|---|---:|")
    for k, lab in labels.items():
        s = stats[k]
        dg, _ = gap(k)
        lines.append(
            f"| {lab} | {s['n']} | {pct(s['med'])} | {pct(s['mean'])} | "
            f"{(s['win'] or 0)*100:.0f}% | {pct(s['med_r'])} | "
            f"{s['is_n']}/{pct(s['is_med'])} | "
            f"{s['oos_n']}/{pct(s['oos_med'])}/{(s['oos_win'] or 0)*100:.0f}% | "
            f"{pct(dg)} |"
        )
    lines.append("")

    lines.append("## Throughput vs quality")
    lines.append("")
    lines.append(f"- 黃燈 raw fires：`{n_yellow_raw}`")
    lines.append(
        f"- 去重後可交易黃燈（arm1）：`{n_y_dedup}` · "
        f"其中 confirm≤2d：`{n_got_confirm}/{n_yellow_considered}` "
        f"（{100 * n_got_confirm / max(n_yellow_considered, 1):.0f}%）"
    )
    lines.append(
        f"- B∧E hard：confirm 後 gate fail skip "
        f"`{bande_gate_stats['n_gate_fail']}` / "
        f"pass `{bande_gate_stats['n_pass']}` "
        f"（保留率 vs confirm "
        f"{100 * bande_gate_stats['n_pass'] / max(bande_gate_stats['n_conf_after_dedupe'], 1):.0f}%）"
    )
    lines.append(
        f"- B∧E soft fill mix：{dict(fill_counts)} · n={soft['n']} · "
        f"med {pct(soft['med'])} · OOS {pct(soft['oos_med'])}"
    )
    lines.append(
        f"- 綠燈 dedupe 後 n=`{g['n']}` · med {pct(g['med'])} · OOS {pct(g['oos_med'])}"
    )
    lines.append("")
    lines.append("| 對照 | 解讀 |")
    lines.append("|------|------|")
    lines.append(
        f"| E vs Y | confirm 濾誤報：n {y['n']}→{e['n']}；"
        f"med {pct(y['med'])}→{pct(e['med'])}；OOS {pct(y['oos_med'])}→{pct(e['oos_med'])} |"
    )
    lines.append(
        f"| B vs Y | 延伸門檻：n {y['n']}→{b['n']}；"
        f"med {pct(y['med'])}→{pct(b['med'])}；OOS {pct(y['oos_med'])}→{pct(b['oos_med'])} |"
    )
    lines.append(
        f"| B∧E vs E | 再砍追價／離均：n {e['n']}→{be['n']}；"
        f"med {pct(e['med'])}→{pct(be['med'])}；OOS {pct(e['oos_med'])}→{pct(be['oos_med'])} |"
    )
    lines.append(
        f"| B∧E vs Green | 全期 Δ {pct(gap('4_BandE_hard')[0])} · "
        f"OOS Δ {pct(gap('4_BandE_hard')[1])} · "
        f"throughput {be['n']}/{g['n']} |"
    )
    lines.append("")

    lines.append("## Early segment vs green (capacity)")
    lines.append("")
    lines.append(
        f"- B∧E hard 進場前尚無綠燈（extra early）：**{n_early}** / {be['n']} · "
        f"med {pct(early_sum['med'])} · OOS n={early_sum['oos_n']} med {pct(early_sum['oos_med'])}"
    )
    lines.append(
        f"- confirm 日 ±5 曆日內有綠燈重疊：`{n_overlap_same_week}` / {be['n']}"
    )
    lines.append(
        f"- B∧E 黃燈後 10 日內**永不**轉綠：`{len(never_green)}` · "
        f"med {pct(summarize(never_green)['med'])}"
    )
    lines.append("")
    lines.append(
        "讀法：若 early 子集中位仍接近綠燈，則 B∧E 有「提早一段」價值；"
        "若 early 偏弱而重疊段才接近綠燈，則增益多半是綠燈選擇效應。"
    )
    lines.append("")

    # per-stock BandE vs green brief
    lines.append("## Per-stock（B∧E hard vs Green med）")
    lines.append("")
    lines.append("| sid | name | B∧E n/med | Green n/med |")
    lines.append("|-----|------|----------:|------------:|")
    for sid in UNIVERSE:
        bt = [t for t in bande if t["sid"] == sid]
        gt = [t for t in arms["5_GREEN"] if t["sid"] == sid]
        bm = median([t["radj"] for t in bt]) if bt else None
        gm = median([t["radj"] for t in gt]) if gt else None
        lines.append(
            f"| {sid} | {NAMES.get(sid, sid)} | {len(bt)}/{pct(bm)} | {len(gt)}/{pct(gm)} |"
        )
    lines.append("")

    lines.append("## Caveats")
    lines.append("")
    lines.append("- 單槽、無組合容量約束；sum 不可當複利。")
    lines.append("- Confirm 條件於黃燈先觸發（半神諭路徑）。")
    lines.append("- OOS 樣本：E / B∧E 約數十筆，幅度勿過度解讀。")
    lines.append("- Soft 臂限價以日低觸價簡化，實盤成交可能更差。")
    lines.append("- 綠燈用各股 champion core×mode（與 H_C 一致）；6223=OR。")
    lines.append("")
    lines.append("## Reproduction")
    lines.append("")
    lines.append("```bash")
    lines.append(
        "PYTHONPATH=src .venv/bin/python scripts/research/run_h_bande_confirm_chase_sma.py"
    )
    lines.append("```")
    lines.append("")

    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # console summary
    print("=== H_B∧E summary ===")
    for k, lab in labels.items():
        s = stats[k]
        print(
            f"{lab}: n={s['n']} med={pct(s['med'])} "
            f"OOS n={s['oos_n']} med={pct(s['oos_med'])} win={(s['oos_win'] or 0)*100:.0f}%"
        )
    print(
        f"early extra (no green before entry): {n_early}/{be['n']} "
        f"med={pct(early_sum['med'])} OOS={pct(early_sum['oos_med'])}"
    )
    print(f"wrote {OUT_MD}")


if __name__ == "__main__":
    main()
