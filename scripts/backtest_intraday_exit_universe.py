#!/usr/bin/env python3
"""Backtest intraday-exit playbook across stock universe · consistency check."""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from statistics import mean, median
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from stock_db import DEFAULT_DB_PATH, connect

MIN_BARS = 200
CHECK = "09:05:00"
NO_TRADE_UNTIL = "09:05:00"
MARKET = "2330"
SLIP_PCT = 0.5
FILL_LAG_MIN = 1
GATE_STOCKS_MIN = 2
GATE_FRAC = 0.98
MARKET_FRAC = 0.995
WEAK_EXIT_FRAC = 0.97
NEUTRAL_EXIT_FRAC = 0.98
EXCLUDE_NAIVE_2PCT = frozenset({"2327", "3264", "5347"})
HOLDINGS = frozenset({"2337", "3264", "2327", "2449", "3008", "3211", "5347"})
RULE_PRIORITY = (
    "S1_vcp_stop",
    "S1_rrg_weak",
    "S2_weak_3pct",
    "S3_neutral_2pct",
    "S4_weak_2pct",
)
BOOTSTRAP_N = 5000
BOOTSTRAP_SEED = 42
DISABLE_WIN_MAX = 0.40
CAUTION_WIN_MAX = 0.50


@dataclass
class DayContext:
    trade_date: str
    mode_on: bool
    n_gate: int
    mkt_ret_check: float | None


@dataclass
class ExitResult:
    trade_date: str
    stock_id: str
    tier: str
    mode_on: bool
    rule: str
    save_pct: float
    triggered: bool


def load_bars(conn: sqlite3.Connection, sid: str, d: str) -> list[dict]:
    best: list[dict] = []
    for src in ("finmind", "yahoo"):
        rows = conn.execute(
            """
            SELECT minute, open, high, low, close FROM stock_kbar_1m
            WHERE stock_id=? AND trade_date=? AND source=? ORDER BY minute
            """,
            (sid, d, src),
        ).fetchall()
        if len(rows) > len(best):
            best = [dict(r) for r in rows]
    return best


def prev_close(conn: sqlite3.Connection, sid: str, d: str) -> float | None:
    row = conn.execute(
        """
        SELECT close FROM stock_daily_bars
        WHERE stock_id=? AND trade_date<? ORDER BY trade_date DESC LIMIT 1
        """,
        (sid, d),
    ).fetchone()
    return float(row[0]) if row and row[0] else None


def px_at(bars: list[dict], minute: str) -> float | None:
    last = None
    for b in bars:
        if b["minute"] <= minute:
            last = float(b["close"])
        else:
            break
    return last


def fill_after(bars: list[dict], tm: str, lag: int = FILL_LAG_MIN) -> float:
    t0 = (int(tm[:2]) - 9) * 60 + int(tm[3:5]) + lag
    for b in bars:
        t = (int(b["minute"][:2]) - 9) * 60 + int(b["minute"][3:5])
        if t >= t0:
            return float(b["close"]) * (1 - SLIP_PCT / 100)
    return float(bars[-1]["close"]) * (1 - SLIP_PCT / 100)


def first_hit_after(bars: list[dict], level: float, not_before: str = NO_TRADE_UNTIL) -> str | None:
    for b in bars:
        if b["minute"] < not_before:
            continue
        if float(b["low"]) <= level:
            return str(b["minute"])
    return None


def rrg_quadrant(conn: sqlite3.Connection, sid: str, d: str) -> str | None:
    row = conn.execute(
        """
        SELECT quadrant FROM rrg_universe_scores
        WHERE stock_id=? AND session_date<=?
        ORDER BY session_date DESC LIMIT 1
        """,
        (sid, d),
    ).fetchone()
    return str(row[0]) if row and row[0] else None


def vcp_state(conn: sqlite3.Connection, sid: str, d: str) -> tuple[str | None, float | None]:
    row = conn.execute(
        """
        SELECT execution_state, stop_loss FROM vcp_screen_scores_v2
        WHERE stock_id=? AND as_of_date<=?
        ORDER BY as_of_date DESC LIMIT 1
        """,
        (sid, d),
    ).fetchone()
    if not row:
        return None, None
    stop = float(row[1]) if row[1] else None
    return str(row[0] or ""), stop


def structure_tier(quadrant: str | None, vcp_exec: str | None) -> str:
    if vcp_exec == "Overextended":
        return "weak"
    if quadrant in ("weakening", "lagging"):
        return "weak"
    if quadrant in ("leading", "improving"):
        return "strong"
    return "neutral"


def universe_stocks(conn: sqlite3.Connection, min_full_days: int) -> list[str]:
    rows = conn.execute(
        """
        SELECT stock_id FROM (
          SELECT stock_id, COUNT(*) AS days FROM (
            SELECT stock_id, trade_date, MAX(n) mx FROM (
              SELECT stock_id, trade_date, source, COUNT(*) n FROM stock_kbar_1m
              GROUP BY stock_id, trade_date, source
            ) GROUP BY stock_id, trade_date HAVING MAX(n)>=?
          ) GROUP BY stock_id
        ) WHERE days >= ?
        ORDER BY stock_id
        """,
        (MIN_BARS, min_full_days),
    ).fetchall()
    return [str(r[0]) for r in rows]


def trading_dates(conn: sqlite3.Connection, start: str, end: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT trade_date FROM stock_daily_bars
        WHERE stock_id='2330' AND trade_date BETWEEN ? AND ?
        ORDER BY trade_date
        """,
        (start, end),
    ).fetchall()
    return [str(r[0]) for r in rows]


CORE4 = ("3264", "2327", "2449", "3211")


def build_day_contexts(
    conn: sqlite3.Connection,
    dates: list[str],
    universe: list[str],
) -> dict[str, DayContext]:
    """Gate = CORE4 playbook（與持倉無關的市場 regime）· 適用於全宇宙個股。"""
    out: dict[str, DayContext] = {}
    for d in dates:
        n_gate = 0
        for sid in CORE4:
            bars = load_bars(conn, sid, d)
            if len(bars) < MIN_BARS:
                continue
            pc = prev_close(conn, sid, d)
            if not pc:
                continue
            px = px_at(bars, CHECK)
            if px and px <= pc * GATE_FRAC:
                n_gate += 1
        m_bars = load_bars(conn, MARKET, d)
        m_pc = prev_close(conn, MARKET, d)
        m_ret = None
        if m_bars and m_pc:
            px = px_at(m_bars, CHECK)
            if px:
                m_ret = (px / m_pc - 1) * 100
        mode = (
            n_gate >= GATE_STOCKS_MIN
            and m_ret is not None
            and m_ret <= (MARKET_FRAC - 1) * 100 + 0.01
        )
        out[d] = DayContext(d, mode, n_gate, m_ret)
    return out


def simulate_playbook(
    conn: sqlite3.Connection,
    sid: str,
    d: str,
    ctx: DayContext,
) -> ExitResult | None:
    bars = load_bars(conn, sid, d)
    pc = prev_close(conn, sid, d)
    if len(bars) < MIN_BARS or not pc:
        return None
    close_px = float(bars[-1]["close"])
    quad = rrg_quadrant(conn, sid, d)
    vcp_exec, vcp_stop = vcp_state(conn, sid, d)
    tier = structure_tier(quad, vcp_exec)

    # S1: VCP stop
    if vcp_stop and vcp_stop > 0:
        tm = first_hit_after(bars, vcp_stop)
        if tm:
            fill = fill_after(bars, tm)
            return ExitResult(d, sid, tier, ctx.mode_on, "S1_vcp_stop", (close_px - fill) / pc * 100, True)

    # S1b: RRG weakening (prior day already weak - intraday -3% proxy for regime break)
    if quad == "weakening":
        tm = first_hit_after(bars, pc * WEAK_EXIT_FRAC)
        if tm:
            fill = fill_after(bars, tm)
            return ExitResult(d, sid, tier, ctx.mode_on, "S1_rrg_weak", (close_px - fill) / pc * 100, True)

    # S2 weak -3%
    if tier == "weak":
        tm = first_hit_after(bars, pc * WEAK_EXIT_FRAC)
        if tm:
            fill = fill_after(bars, tm)
            return ExitResult(d, sid, tier, ctx.mode_on, "S2_weak_3pct", (close_px - fill) / pc * 100, True)

    # S3/S4 mode-gated -2%
    if ctx.mode_on and sid not in EXCLUDE_NAIVE_2PCT:
        tm = first_hit_after(bars, pc * NEUTRAL_EXIT_FRAC)
        if tm:
            fill = fill_after(bars, tm)
            rule = "S4_weak_2pct" if tier == "weak" else "S3_neutral_2pct"
            if tier == "strong":
                return None
            return ExitResult(d, sid, tier, ctx.mode_on, rule, (close_px - fill) / pc * 100, True)

    return ExitResult(d, sid, tier, ctx.mode_on, "hold", (0.0), False)


def naive_e1(conn: sqlite3.Connection, sid: str, d: str) -> float | None:
    bars = load_bars(conn, sid, d)
    pc = prev_close(conn, sid, d)
    if len(bars) < MIN_BARS or not pc:
        return None
    tm = first_hit_after(bars, pc * NEUTRAL_EXIT_FRAC)
    if not tm:
        return None
    fill = fill_after(bars, tm)
    return (float(bars[-1]["close"]) - fill) / pc * 100


def win_rate(saves: list[float]) -> float:
    return sum(1 for s in saves if s < 0) / len(saves) if saves else float("nan")


def summarize(label: str, saves: list[float]) -> str:
    if not saves:
        return f"  {label}: n=0"
    return (
        f"  {label}: n={len(saves)} win={win_rate(saves):.1%} "
        f"med={median(saves):+.2f}% mean={mean(saves):+.2f}%"
    )


def save_pct(bars: list[dict], pc: float, fill: float) -> float:
    return (float(bars[-1]["close"]) - fill) / pc * 100


def sync_dd3_day(conn: sqlite3.Connection, d: str) -> bool:
    dds: list[float] = []
    for sid in CORE4:
        bars = load_bars(conn, sid, d)
        pc = prev_close(conn, sid, d)
        if len(bars) < MIN_BARS or not pc:
            continue
        lo = min(float(b["low"]) for b in bars)
        dds.append((lo / pc - 1) * 100)
    return len(dds) >= 3 and sum(1 for x in dds if x <= -3) >= 3


def rule_s1_vcp(conn: sqlite3.Connection, sid: str, d: str, ctx: DayContext) -> float | None:
    bars = load_bars(conn, sid, d)
    pc = prev_close(conn, sid, d)
    if len(bars) < MIN_BARS or not pc:
        return None
    _, vcp_stop = vcp_state(conn, sid, d)
    if not vcp_stop or vcp_stop <= 0:
        return None
    tm = first_hit_after(bars, vcp_stop)
    if not tm:
        return None
    return save_pct(bars, pc, fill_after(bars, tm))


def rule_s1_rrg(conn: sqlite3.Connection, sid: str, d: str, ctx: DayContext) -> float | None:
    bars = load_bars(conn, sid, d)
    pc = prev_close(conn, sid, d)
    if len(bars) < MIN_BARS or not pc:
        return None
    if rrg_quadrant(conn, sid, d) != "weakening":
        return None
    tm = first_hit_after(bars, pc * WEAK_EXIT_FRAC)
    if not tm:
        return None
    return save_pct(bars, pc, fill_after(bars, tm))


def rule_s2(conn: sqlite3.Connection, sid: str, d: str, ctx: DayContext) -> float | None:
    bars = load_bars(conn, sid, d)
    pc = prev_close(conn, sid, d)
    if len(bars) < MIN_BARS or not pc:
        return None
    quad = rrg_quadrant(conn, sid, d)
    vcp_exec, _ = vcp_state(conn, sid, d)
    if structure_tier(quad, vcp_exec) != "weak":
        return None
    tm = first_hit_after(bars, pc * WEAK_EXIT_FRAC)
    if not tm:
        return None
    return save_pct(bars, pc, fill_after(bars, tm))


def rule_s3(conn: sqlite3.Connection, sid: str, d: str, ctx: DayContext) -> float | None:
    if not ctx.mode_on or sid in EXCLUDE_NAIVE_2PCT:
        return None
    bars = load_bars(conn, sid, d)
    pc = prev_close(conn, sid, d)
    if len(bars) < MIN_BARS or not pc:
        return None
    quad = rrg_quadrant(conn, sid, d)
    vcp_exec, _ = vcp_state(conn, sid, d)
    if structure_tier(quad, vcp_exec) != "neutral":
        return None
    tm = first_hit_after(bars, pc * NEUTRAL_EXIT_FRAC)
    if not tm:
        return None
    return save_pct(bars, pc, fill_after(bars, tm))


def rule_s4(conn: sqlite3.Connection, sid: str, d: str, ctx: DayContext) -> float | None:
    if not ctx.mode_on or sid in EXCLUDE_NAIVE_2PCT:
        return None
    bars = load_bars(conn, sid, d)
    pc = prev_close(conn, sid, d)
    if len(bars) < MIN_BARS or not pc:
        return None
    quad = rrg_quadrant(conn, sid, d)
    vcp_exec, _ = vcp_state(conn, sid, d)
    if structure_tier(quad, vcp_exec) != "weak":
        return None
    tm = first_hit_after(bars, pc * NEUTRAL_EXIT_FRAC)
    if not tm:
        return None
    return save_pct(bars, pc, fill_after(bars, tm))


RULE_FNS: dict[str, Callable[[sqlite3.Connection, str, str, DayContext], float | None]] = {
    "S1_vcp_stop": rule_s1_vcp,
    "S1_rrg_weak": rule_s1_rrg,
    "S2_weak_3pct": rule_s2,
    "S3_neutral_2pct": rule_s3,
    "S4_weak_2pct": rule_s4,
}


def simulate_combo(
    conn: sqlite3.Connection,
    sid: str,
    d: str,
    ctx: DayContext,
    active: frozenset[str],
    *,
    sync_only: bool = False,
    stock_filter: frozenset[str] | None = None,
) -> tuple[str, float] | None:
    if stock_filter is not None and sid not in stock_filter:
        return None
    if sync_only and not sync_dd3_day(conn, d):
        return None
    for rule in RULE_PRIORITY:
        if rule not in active:
            continue
        save = RULE_FNS[rule](conn, sid, d, ctx)
        if save is not None:
            return rule, save
    return None


def collect_combo_saves(
    conn: sqlite3.Connection,
    dates: list[str],
    universe: list[str],
    contexts: dict[str, DayContext],
    active: frozenset[str],
    *,
    sync_only: bool = False,
    stock_filter: frozenset[str] | None = None,
) -> tuple[list[float], dict[str, list[float]]]:
    saves: list[float] = []
    by_rule: dict[str, list[float]] = defaultdict(list)
    for d in dates:
        ctx = contexts[d]
        for sid in universe:
            hit = simulate_combo(
                conn, sid, d, ctx, active, sync_only=sync_only, stock_filter=stock_filter
            )
            if hit:
                rule, save = hit
                saves.append(save)
                by_rule[rule].append(save)
    return saves, by_rule


def rule_disable_flag(saves: list[float]) -> str:
    if not saves:
        return "NO_DATA"
    wr = win_rate(saves)
    med = median(saves)
    if wr < DISABLE_WIN_MAX and med > 0:
        return "DISABLE"
    if wr < CAUTION_WIN_MAX:
        return "CAUTION"
    return "KEEP"


def percentile(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return float("nan")
    k = (len(sorted_vals) - 1) * p / 100
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    if f == c:
        return sorted_vals[f]
    return sorted_vals[f] * (c - k) + sorted_vals[c] * (k - f)


def bootstrap_ci(values: list[float], n_boot: int = BOOTSTRAP_N, seed: int = BOOTSTRAP_SEED) -> dict:
    rng = random.Random(seed)
    n = len(values)
    if n == 0:
        return {}
    meds: list[float] = []
    means: list[float] = []
    wins: list[float] = []
    for _ in range(n_boot):
        sample = [values[rng.randrange(n)] for _ in range(n)]
        meds.append(median(sample))
        means.append(mean(sample))
        wins.append(sum(1 for v in sample if v < 0) / n)
    meds.sort()
    means.sort()
    wins.sort()
    return {
        "n": n,
        "obs_median": median(values),
        "obs_mean": mean(values),
        "obs_win_rate": win_rate(values),
        "med_p2.5": percentile(meds, 2.5),
        "med_p97.5": percentile(meds, 97.5),
        "mean_p2.5": percentile(means, 2.5),
        "mean_p97.5": percentile(means, 97.5),
        "win_p2.5": percentile(wins, 2.5),
        "win_p97.5": percentile(wins, 97.5),
        "p_exit_helps": sum(1 for m in meds if m < 0) / n_boot,
    }


def print_bootstrap(label: str, saves: list[float], n_boot: int) -> None:
    ci = bootstrap_ci(saves, n_boot=n_boot)
    if not ci:
        print(f"  {label}: n=0")
        return
    print(f"\n  {label} n={ci['n']}")
    print(
        f"    觀測: median={ci['obs_median']:+.2f}% "
        f"mean={ci['obs_mean']:+.2f}% win={ci['obs_win_rate']:.1%}"
    )
    print(f"    median 95% CI: [{ci['med_p2.5']:+.2f}%, {ci['med_p97.5']:+.2f}%]")
    print(f"    win_rate 95% CI: [{ci['win_p2.5']:.1%}, {ci['win_p97.5']:.1%}]")
    print(f"    P(median save < 0) ≈ {ci['p_exit_helps']:.1%}  （>50% 才有統計優勢）")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--start", default="2026-01-02")
    parser.add_argument("--end", default="2026-06-22")
    parser.add_argument("--min-full-days", type=int, default=30)
    parser.add_argument("--bootstrap-n", type=int, default=BOOTSTRAP_N)
    parser.add_argument("--json", action="store_true", help="輸出 rule_flags JSON 至 stdout 末尾")
    args = parser.parse_args()

    conn = connect()
    conn.row_factory = sqlite3.Row
    universe = universe_stocks(conn, args.min_full_days)
    dates = trading_dates(conn, args.start, args.end)
    contexts = build_day_contexts(conn, dates, universe)
    sync_days = {d for d in dates if sync_dd3_day(conn, d)}

    mode_on_days = sum(1 for c in contexts.values() if c.mode_on)
    print("=" * 72)
    print(
        f"UNIVERSE BACKTEST · {args.start}..{args.end} · "
        f"stocks={len(universe)} · days={len(dates)} · "
        f"mode_on={mode_on_days} · sync_dd3={len(sync_days)}"
    )

    playbook_results: list[ExitResult] = []
    naive_saves: list[float] = []
    naive_by_mode: dict[bool, list[float]] = {True: [], False: []}

    for d in dates:
        ctx = contexts[d]
        for sid in universe:
            r = simulate_playbook(conn, sid, d, ctx)
            if r and r.triggered:
                playbook_results.append(r)
            ns = naive_e1(conn, sid, d)
            if ns is not None:
                naive_saves.append(ns)
                naive_by_mode[ctx.mode_on].append(ns)

    pb_saves = [r.save_pct for r in playbook_results]
    pb_mode = {True: [], False: []}
    pb_rule: dict[str, list[float]] = defaultdict(list)
    pb_tier: dict[str, list[float]] = defaultdict(list)
    for r in playbook_results:
        pb_mode[r.mode_on].append(r.save_pct)
        pb_rule[r.rule].append(r.save_pct)
        pb_tier[r.tier].append(r.save_pct)

    print("\n【A】Playbook 全宇宙")
    print(summarize("全部觸發", pb_saves))
    print(summarize("MODE=ON", pb_mode[True]))
    print(summarize("MODE=OFF", pb_mode[False]))
    for rule in sorted(pb_rule):
        print(summarize(f"  {rule}", pb_rule[rule]))

    print("\n【B】Naive E1(-2%) 無閘門 · 09:05後 · 對照組")
    print(summarize("全宇宙", naive_saves))
    print(summarize("MODE=ON 日", naive_by_mode[True]))
    print(summarize("MODE=OFF 日", naive_by_mode[False]))

    # Holdings subset
    print("\n【C】你的持倉子集")
    h_pb = [r.save_pct for r in playbook_results if r.stock_id in HOLDINGS]
    h_nv = []
    for d in dates:
        for sid in HOLDINGS:
            if sid not in universe:
                continue
            v = naive_e1(conn, sid, d)
            if v is not None:
                h_nv.append(v)
    print(summarize("Playbook", h_pb))
    print(summarize("Naive E1", h_nv))

    # CORE4
    core = {"3264", "2327", "2449", "3211"}
    c_pb = [r.save_pct for r in playbook_results if r.stock_id in core]
    print(summarize("CORE4 Playbook", c_pb))

    # By tier (playbook triggers only)
    print("\n【D】依 structure_tier")
    for t in ("weak", "neutral", "strong"):
        print(summarize(t, pb_tier.get(t, [])))

    # Consistency: MODE ON win rate playbook vs naive
    print("\n【E】一致性檢定")
    pb_on, pb_off = win_rate(pb_mode[True]), win_rate(pb_mode[False])
    nv_on, nv_off = win_rate(naive_by_mode[True]), win_rate(naive_by_mode[False])
    print(f"  Playbook  MODE=ON win={pb_on:.1%}  MODE=OFF win={pb_off:.1%}")
    print(f"  Naive E1  MODE=ON win={nv_on:.1%}  MODE=OFF win={nv_off:.1%}")
    gap_pb = pb_on - pb_off if pb_on == pb_on and pb_off == pb_off else 0
    gap_nv = nv_on - nv_off if nv_on == nv_on and nv_off == nv_off else 0
    print(f"  MODE=ON 優勢（win率差）Playbook {gap_pb:+.1%} · Naive {gap_nv:+.1%}")
    consistent = pb_on > pb_off and pb_on >= nv_on
    print(f"  結論: {'一致（Playbook MODE 分層有效）' if consistent else '不一致（需調參）'}")

    # Sample other stocks not in holdings
    print("\n【F】非持倉標的（universe \\ holdings）樣本")
    other = [s for s in universe if s not in HOLDINGS]
    o_pb = [r.save_pct for r in playbook_results if r.stock_id in other]
    o_nv = []
    for d in dates:
        for sid in other:
            v = naive_e1(conn, sid, d)
            if v is not None:
                o_nv.append(v)
    print(summarize(f"其他 {len(other)} 檔 Playbook", o_pb))
    print(summarize("其他 Naive E1", o_nv))

    # Per-rule isolated (no priority collision)
    print("\n【G】分規則獨立回測（各規則單獨計 · 不互相搶先）")
    isolated: dict[str, list[float]] = {}
    rule_flags: dict[str, dict] = {}
    for rule, fn in RULE_FNS.items():
        saves: list[float] = []
        for d in dates:
            ctx = contexts[d]
            for sid in universe:
                v = fn(conn, sid, d, ctx)
                if v is not None:
                    saves.append(v)
        isolated[rule] = saves
        flag = rule_disable_flag(saves)
        rule_flags[rule] = {
            "flag": flag,
            "n": len(saves),
            "win_rate": round(win_rate(saves), 4) if saves else None,
            "median_save_pct": round(median(saves), 4) if saves else None,
        }
        print(summarize(rule, saves) + f"  → {flag}")

    refined_active = frozenset(
        r for r, meta in rule_flags.items() if meta["flag"] != "DISABLE"
    )
    print(f"\n  建議停用（DISABLE）: {[r for r, m in rule_flags.items() if m['flag']=='DISABLE']}")
    print(f"  建議保留但謹慎（CAUTION）: {[r for r, m in rule_flags.items() if m['flag']=='CAUTION']}")

    # Combo variants
    print("\n【H】精簡組合（優先序模擬）")
    combos = {
        "full_playbook": frozenset(RULE_PRIORITY),
        "no_S1": frozenset({"S2_weak_3pct", "S3_neutral_2pct", "S4_weak_2pct"}),
        "no_S3_S4": frozenset({"S1_vcp_stop", "S1_rrg_weak", "S2_weak_3pct"}),
        "S2_only": frozenset({"S2_weak_3pct"}),
        "refined_auto": refined_active,
    }
    combo_stats: dict[str, dict] = {}
    for label, active in combos.items():
        saves, by_rule = collect_combo_saves(conn, dates, universe, contexts, active)
        combo_stats[label] = {"n": len(saves), "win_rate": win_rate(saves) if saves else None}
        print(summarize(label, saves))
        for rule in RULE_PRIORITY:
            if by_rule.get(rule):
                print(summarize(f"  └ {rule}", by_rule[rule]))

    print("\n  sync_dd3 環境過濾：")
    for label, active, filt in [
        ("sync · no_S1", frozenset({"S2_weak_3pct", "S3_neutral_2pct", "S4_weak_2pct"}), None),
        ("sync · S2 · CORE4", frozenset({"S2_weak_3pct"}), frozenset(CORE4)),
        ("sync · S2 · 持倉", frozenset({"S2_weak_3pct"}), HOLDINGS),
    ]:
        saves, _ = collect_combo_saves(
            conn, dates, universe, contexts, active, sync_only=True, stock_filter=filt
        )
        combo_stats[label] = {"n": len(saves), "win_rate": win_rate(saves) if saves else None}
        print(summarize(f"  {label}", saves))

    # Bootstrap CI on key subsets
    print("\n【I】Bootstrap 95% CI（精簡子集 · 0.5% slip 已含）")
    bootstrap_targets: list[tuple[str, list[float]]] = [
        ("full_playbook", pb_saves),
        ("no_S3_S4", collect_combo_saves(
            conn, dates, universe, contexts, frozenset({"S1_vcp_stop", "S1_rrg_weak", "S2_weak_3pct"})
        )[0]),
        ("S2_only", isolated["S2_weak_3pct"]),
        ("S1_rrg_weak", isolated["S1_rrg_weak"]),
    ]
    for label, active, filt in [
        ("sync_S2_CORE4", frozenset({"S2_weak_3pct"}), frozenset(CORE4)),
        ("sync_S2_holdings", frozenset({"S2_weak_3pct"}), HOLDINGS),
    ]:
        saves, _ = collect_combo_saves(
            conn, dates, universe, contexts, active, sync_only=True, stock_filter=filt
        )
        bootstrap_targets.append((label, saves))
    for label, saves in bootstrap_targets:
        print_bootstrap(label, saves, args.bootstrap_n)

    if args.json:
        payload = {
            "period": f"{args.start}..{args.end}",
            "universe_n": len(universe),
            "sync_dd3_days": len(sync_days),
            "rule_flags": rule_flags,
            "recommended_disable": [r for r, m in rule_flags.items() if m["flag"] == "DISABLE"],
            "refined_active": sorted(refined_active),
            "combo_stats": combo_stats,
        }
        print("\n--- JSON ---")
        print(json.dumps(payload, ensure_ascii=False, indent=2))

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
