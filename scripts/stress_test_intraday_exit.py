#!/usr/bin/env python3
"""Stress-test intraday exit heuristics · 9 challenge questions with stats."""

from __future__ import annotations

import argparse
import math
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from statistics import mean, median, pstdev

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from stock_db import DEFAULT_DB_PATH

HOLDINGS = ("2337", "3264", "2327", "2449", "3008", "3211", "5347")
MIN_BARS = 200
CHECK_MINUTE = "09:05:00"
MARKET_PROXY = "2330"  # 台積電 1m 作盤中大盤代理


@dataclass
class DayRow:
    trade_date: str
    n_full: int
    n_down_2_at_check: int
    n_end_down_2: int
    n_end_down_3: int
    sync_end: bool  # ≥4 names max dd from prev close ≤ -3%
    portfolio_ret_close: float | None
    market_ret_close: float | None
    market_ret_at_check: float | None
    avg_liquidity: float | None


def load_day_bars(conn: sqlite3.Connection, stock_id: str, trade_date: str) -> list[dict]:
    best: list[dict] = []
    for src in ("finmind", "yahoo"):
        rows = conn.execute(
            """
            SELECT minute, open, high, low, close, volume
            FROM stock_kbar_1m
            WHERE stock_id = ? AND trade_date = ? AND source = ?
            ORDER BY minute
            """,
            (stock_id, trade_date, src),
        ).fetchall()
        if len(rows) > len(best):
            best = [dict(r) for r in rows]
    return best


def prev_close(conn: sqlite3.Connection, stock_id: str, trade_date: str) -> float | None:
    row = conn.execute(
        """
        SELECT close FROM stock_daily_bars
        WHERE stock_id = ? AND trade_date < ?
        ORDER BY trade_date DESC LIMIT 1
        """,
        (stock_id, trade_date),
    ).fetchone()
    return float(row[0]) if row and row[0] else None


def index_daily(conn: sqlite3.Connection, trade_date: str) -> tuple[float | None, float | None]:
    prev_row = conn.execute(
        "SELECT close FROM daily_bars WHERE code='IX0001' AND date < ? ORDER BY date DESC LIMIT 1",
        (trade_date,),
    ).fetchone()
    today_row = conn.execute(
        "SELECT close FROM daily_bars WHERE code='IX0001' AND date = ?",
        (trade_date,),
    ).fetchone()
    if not prev_row or not today_row:
        return None, None
    return float(prev_row[0]), float(today_row[0])


def px_at_or_before(bars: list[dict], minute: str) -> float | None:
    last = None
    for b in bars:
        if b["minute"] <= minute:
            last = float(b["close"])
        else:
            break
    return last


def max_dd_from_prev(bars: list[dict], prev: float) -> float:
    return (min(float(b["low"]) for b in bars) / prev - 1) * 100


def first_hit_minute(bars: list[dict], level: float) -> str | None:
    for b in bars:
        if float(b["low"]) <= level:
            return str(b["minute"])
    return None


def fill_after_lag(bars: list[dict], trigger_minute: str, lag_min: int) -> float:
    t0 = (int(trigger_minute[:2]) - 9) * 60 + int(trigger_minute[3:5])
    target = t0 + lag_min
    for b in bars:
        tm = (int(b["minute"][:2]) - 9) * 60 + int(b["minute"][3:5])
        if tm >= target:
            return float(b["close"])
    return float(bars[-1]["close"])


def trading_dates(conn: sqlite3.Connection, start: str | None, end: str | None) -> list[str]:
    q = "SELECT DISTINCT trade_date FROM stock_daily_bars WHERE stock_id='2337'"
    args: list = []
    if start:
        q += " AND trade_date >= ?"
        args.append(start)
    if end:
        q += " AND trade_date <= ?"
        args.append(end)
    q += " ORDER BY trade_date"
    return [str(r[0]) for r in conn.execute(q, args).fetchall()]


def build_days(conn: sqlite3.Connection, dates: list[str]) -> list[DayRow]:
    out: list[DayRow] = []
    for d in dates:
        n_down_check = 0
        n_end_down_2 = 0
        n_end_down_3 = 0
        n_full = 0
        rets = []
        liq = []
        sync_members = 0
        for sid in HOLDINGS:
            bars = load_day_bars(conn, sid, d)
            if len(bars) < MIN_BARS:
                continue
            pc = prev_close(conn, sid, d)
            if not pc:
                continue
            n_full += 1
            px_check = px_at_or_before(bars, CHECK_MINUTE)
            close_px = float(bars[-1]["close"])
            if px_check and px_check <= pc * 0.98:
                n_down_check += 1
            if close_px <= pc * 0.98:
                n_end_down_2 += 1
            dd = max_dd_from_prev(bars, pc)
            if dd <= -3:
                sync_members += 1
            if close_px <= pc * 0.97:
                n_end_down_3 += 1
            rets.append((close_px / pc - 1) * 100)
            vols = [float(b["volume"] or 0) for b in bars]
            if vols:
                liq.append(mean(vols))
        m_prev, m_close = index_daily(conn, d)
        m_ret = ((m_close / m_prev) - 1) * 100 if m_prev and m_close else None
        m_bars = load_day_bars(conn, MARKET_PROXY, d)
        m_pc = prev_close(conn, MARKET_PROXY, d)
        m_at = None
        if m_bars and m_pc:
            px = px_at_or_before(m_bars, CHECK_MINUTE)
            if px:
                m_at = (px / m_pc - 1) * 100
        out.append(
            DayRow(
                trade_date=d,
                n_full=n_full,
                n_down_2_at_check=n_down_check,
                n_end_down_2=n_end_down_2,
                n_end_down_3=n_end_down_3,
                sync_end=sync_members >= 4,
                portfolio_ret_close=mean(rets) if rets else None,
                market_ret_close=m_ret,
                market_ret_at_check=m_at,
                avg_liquidity=mean(liq) if liq else None,
            )
        )
    return out


def confusion(days: list[DayRow], *, k: int, pred: str, label: str) -> dict:
    """pred/label: 'sync_end' | 'end_down_3' | 'end_down_2'."""
    def get_val(dr: DayRow, key: str) -> bool:
        if key == "sync_end":
            return dr.sync_end
        if key == "end_down_3":
            return dr.n_end_down_3 >= 4
        if key == "end_down_2":
            return dr.n_end_down_2 >= 4
        if key == "check_k":
            return dr.n_down_2_at_check >= k
        raise ValueError(key)

    tp = fp = fn = tn = 0
    for dr in days:
        if dr.n_full < 5:
            continue
        p = get_val(dr, pred) if pred != "check_k" else dr.n_down_2_at_check >= k
        y = get_val(dr, label)
        if p and y:
            tp += 1
        elif p and not y:
            fp += 1
        elif not p and y:
            fn += 1
        else:
            tn += 1
    prec = tp / (tp + fp) if tp + fp else 0.0
    rec = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn, "precision": prec, "recall": rec, "f1": f1}


def e1_outcomes(
    conn: sqlite3.Connection,
    dates: list[str],
    threshold_pct: float,
    lag: int = 0,
    slip_pct: float = 0.0,
):
    """Per stock-day with full bars. saved = (close - fill)/pc*100; negative fill better."""
    saves = []
    wins = 0
    total = 0
    for d in dates:
        for sid in HOLDINGS:
            bars = load_day_bars(conn, sid, d)
            if len(bars) < MIN_BARS:
                continue
            pc = prev_close(conn, sid, d)
            if not pc:
                continue
            level = pc * (1 - threshold_pct / 100)
            tm = first_hit_minute(bars, level)
            if not tm:
                continue
            fill = fill_after_lag(bars, tm, lag) * (1 - slip_pct / 100)
            close_px = float(bars[-1]["close"])
            save = (close_px - fill) / pc * 100
            saves.append(save)
            total += 1
            if save < 0:
                wins += 1
    return {
        "n": total,
        "win_rate": wins / total if total else 0.0,
        "median_save": median(saves) if saves else None,
        "mean_save": mean(saves) if saves else None,
        "saves": saves,
    }


def classify_rebound(conn: sqlite3.Connection, dates: list[str], threshold_pct: float = 2.0):
    """After first hit -threshold%, classify close vs levels."""
    rebound = whipsaw = sustained = 0
    total = 0
    for d in dates:
        for sid in HOLDINGS:
            bars = load_day_bars(conn, sid, d)
            if len(bars) < MIN_BARS:
                continue
            pc = prev_close(conn, sid, d)
            if not pc:
                continue
            level = pc * (1 - threshold_pct / 100)
            tm = first_hit_minute(bars, level)
            if not tm:
                continue
            total += 1
            close_px = float(bars[-1]["close"])
            close_ret = (close_px / pc - 1) * 100
            # rebound: hit -2% intraday but close ABOVE -2%
            if close_ret > -threshold_pct:
                rebound += 1
            elif close_ret <= -threshold_pct - 1.0:
                sustained += 1
            else:
                whipsaw += 1
    return {
        "n": total,
        "rebound_pct": rebound / total * 100 if total else 0,
        "whipsaw_pct": whipsaw / total * 100 if total else 0,
        "sustained_pct": sustained / total * 100 if total else 0,
    }


def opportunity_cost(conn: sqlite3.Connection, dates: list[str], threshold_pct: float = 2.0):
    """After E1 sell, return from check minute holding cash vs switching to 2330 to close."""
    cash_better = 0
    market_better = 0
    n = 0
    for d in dates:
        for sid in HOLDINGS:
            bars = load_day_bars(conn, sid, d)
            if len(bars) < MIN_BARS:
                continue
            pc = prev_close(conn, sid, d)
            if not pc:
                continue
            level = pc * (1 - threshold_pct / 100)
            tm = first_hit_minute(bars, level)
            if not tm:
                continue
            fill = fill_after_lag(bars, tm, 1)
            hold_ret = (float(bars[-1]["close"]) / pc - 1) * 100
            exit_ret = (fill / pc - 1) * 100
            m_bars = load_day_bars(conn, MARKET_PROXY, d)
            m_pc = prev_close(conn, MARKET_PROXY, d)
            if m_bars and m_pc:
                m_fill = fill_after_lag(m_bars, tm, 1)
                m_close = float(m_bars[-1]["close"])
                m_switch_ret = (m_close / m_fill - 1) * 100
                n += 1
                if exit_ret > hold_ret:
                    cash_better += 1
                if m_switch_ret > hold_ret - exit_ret:
                    market_better += 1
    return {"n": n, "cash_exit_better_rate": cash_better / n if n else 0, "market_switch_better_rate": market_better / n if n else 0}


def pearson(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 3:
        return None
    mx, my = mean(xs), mean(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den = math.sqrt(sum((x - mx) ** 2 for x in xs) * sum((y - my) ** 2 for y in ys))
    return num / den if den else None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--start", default="2026-01-02")
    parser.add_argument("--end", default="2026-06-22")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    dates = trading_dates(conn, args.start, args.end)
    days = build_days(conn, dates)
    valid = [d for d in days if d.n_full >= 5]

    print("=" * 72)
    print(f"STRESS TEST · {args.start} .. {args.end} · {len(valid)} eval days (≥5 holdings w/ 1m)")
    print("=" * 72)

    # Q1 hindsight
    print("\n【Q1 事後貼標？】09:05 ≥k 檔≤昨收-2% 能否預測「終局同步急跌」(≥4檔盤中最大回撤≤-3%)")
    for k in (2, 3, 4, 5):
        c = confusion(valid, k=k, pred="check_k", label="sync_end")
        print(
            f"  k={k}: precision={c['precision']:.1%} recall={c['recall']:.1%} "
            f"F1={c['f1']:.2f} (TP={c['tp']} FP={c['fp']} FN={c['fn']} TN={c['tn']})"
        )
    base_rate = sum(1 for d in valid if d.sync_end) / len(valid) if valid else 0
    print(f"  基準率 P(同步急跌日)={base_rate:.1%}（{sum(1 for d in valid if d.sync_end)}/{len(valid)} 日）")

    # Q2 sample size windows
    print("\n【Q2 樣本數】不同視窗下同步急跌日天數")
    for label, s, e in [
        ("近31日", "2026-05-22", "2026-06-22"),
        ("近60日", "2026-03-24", "2026-06-22"),
        ("全庫存YTD", "2026-01-02", "2026-06-22"),
    ]:
        ds = [d for d in build_days(conn, trading_dates(conn, s, e)) if d.n_full >= 5]
        n_sync = sum(1 for d in ds if d.sync_end)
        print(f"  {label}: {len(ds)} eval days · 同步急跌 {n_sync} 天 ({n_sync/len(ds):.1%})" if ds else f"  {label}: —")

    # Q3 cherry picking - full E1 distribution
    print("\n【Q3 只挑成功案例？】E1(-2%) 全部觸發股日 · save=(收盤-賣價)/昨收 · 負值=賣比收盤好")
    e1 = e1_outcomes(conn, dates, 2.0)
    if e1["saves"]:
        neg = sum(1 for s in e1["saves"] if s < 0)
        pos = sum(1 for s in e1["saves"] if s >= 0)
        print(f"  n={e1['n']} · 賣優於抱收盤 {neg} ({neg/e1['n']:.1%}) · 賣劣於抱收盤 {pos} ({pos/e1['n']:.1%})")
        print(f"  median save={e1['median_save']:+.2f}% mean={e1['mean_save']:+.2f}%")
        print(f"  解讀: save<0 佔 {neg/e1['n']:.0%} → 早賣在過半數情況其實不如抱著")

    # Q4 opportunity cost
    print("\n【Q4 機會成本？】觸發 E1 後：賣出 vs 抱收盤；若改買2330至收盤")
    oc = opportunity_cost(conn, dates, 2.0)
    print(f"  n={oc['n']} · 賣出優於抱股 {oc['cash_exit_better_rate']:.1%}")
    print(f"  觸發後改買2330至收盤優於繼續抱原股 {oc['market_switch_better_rate']:.1%}")

    # Q5 threshold sweep
    print("\n【Q5 data-snooping？】閾值敏感度（盤中觸及 threshold% · 1分延遲）")
    print(f"  {'thr%':>5} {'n':>5} {'win%':>7} {'med_save':>9} {'mean_save':>9}")
    for thr in [1.5, 1.8, 2.0, 2.2, 2.5, 3.0, 3.5, 4.0, 5.0]:
        r = e1_outcomes(conn, dates, thr, lag=1)
        if r["n"]:
            print(
                f"  {thr:>5.1f} {r['n']:>5} {r['win_rate']:>6.1%} "
                f"{r['median_save']:>+8.2f}% {r['mean_save']:>+8.2f}%"
            )

    # Q6 k sweep at 09:05
    print("\n【Q6 ≥3檔過擬合？】09:05 檔數門檻 k vs 終局同步急跌")
    print(f"  {'k':>3} {'prec':>7} {'recall':>7} {'F1':>6} {'FP率':>7}")
    for k in range(1, 7):
        c = confusion(valid, k=k, pred="check_k", label="sync_end")
        fp_rate = c["fp"] / (c["fp"] + c["tn"]) if c["fp"] + c["tn"] else 0
        print(f"  {k:>3} {c['precision']:>6.1%} {c['recall']:>6.1%} {c['f1']:>6.2f} {fp_rate:>6.1%}")

    # Q7 beta
    print("\n【Q7 只是大盤？】同步急跌日 vs 非同步 · 台指日報酬 & 2330@09:05")
    sync = [d for d in valid if d.sync_end]
    nons = [d for d in valid if not d.sync_end]
    for name, arr in [("同步急跌", sync), ("非同步", nons)]:
        m = [d.market_ret_close for d in arr if d.market_ret_close is not None]
        a = [d.market_ret_at_check for d in arr if d.market_ret_at_check is not None]
        p = [d.portfolio_ret_close for d in arr if d.portfolio_ret_close is not None]
        if m:
            print(
                f"  {name} n={len(arr)}: 台指日均 {mean(m):+.2f}% · "
                f"2330@09:05均 {mean(a):+.2f}% · 組合日均 {mean(p):+.2f}%"
            )
    xs = [d.market_ret_close for d in valid if d.market_ret_close is not None and d.portfolio_ret_close is not None]
    ys = [d.portfolio_ret_close for d in valid if d.market_ret_close is not None and d.portfolio_ret_close is not None]
    r = pearson(xs, ys)
    print(f"  組合日報酬 vs 台指日報酬 Pearson r={r:.3f}" if r else "  r=—")
    xs2 = [d.market_ret_at_check for d in valid if d.market_ret_at_check is not None and d.n_down_2_at_check]
    ys2 = [d.n_down_2_at_check for d in valid if d.market_ret_at_check is not None]
    r2 = pearson(xs2, ys2)
    print(f"  09:05 2330跌幅 vs 幾檔≤-2% Pearson r={r2:.3f}" if r2 else "")

    # Q8 slippage + liquidity
    print("\n【Q8 成交理想化？】E1(-2%) +1分延遲 + 滑價敏感度")
    for slip in (0.0, 0.5, 1.0, 1.5, 2.0):
        r = e1_outcomes(conn, dates, 2.0, lag=1, slip_pct=slip)
        print(f"  滑價{slip:.1f}%: win_rate={r['win_rate']:.1%} median_save={r['median_save']:+.2f}% n={r['n']}")
    print("  各股平均1分K成交量（高=較不易滑價）:")
    for sid in HOLDINGS:
        vols = []
        for d in dates:
            b = load_day_bars(conn, sid, d)
            if len(b) >= MIN_BARS:
                vols.append(mean([float(x["volume"] or 0) for x in b]))
        if vols:
            print(f"    {sid}: {mean(vols):,.0f}")

    # Q9 rebound frequency
    print("\n【Q9 反彈型比例？】觸及盤中-thr% 後收盤分類")
    for thr in (2.0, 3.0):
        c = classify_rebound(conn, dates, thr)
        print(
            f"  thr={thr}%: n={c['n']} · 反彈(收>-thr%) {c['rebound_pct']:.1f}% · "
            f"溫和 {c['whipsaw_pct']:.1f}% · 續跌(收≤-thr-1%) {c['sustained_pct']:.1f}%"
        )

    # Q10 implied: conditional on sync days only
    print("\n【加總】僅在「終局同步急跌日」內 E1 表現（條件樣本）")
    sync_dates = {d.trade_date for d in valid if d.sync_end}
    r_sync = e1_outcomes(conn, [d for d in dates if d in sync_dates], 2.0, lag=1)
    r_nons = e1_outcomes(conn, [d for d in dates if d not in sync_dates], 2.0, lag=1)
    print(
        f"  同步日 n={r_sync['n']} win={r_sync['win_rate']:.1%} med={r_sync['median_save']:+.2f}% · "
        f"非同步 n={r_nons['n']} win={r_nons['win_rate']:.1%} med={r_nons['median_save']:+.2f}%"
    )

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
