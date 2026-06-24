#!/usr/bin/env python3
"""Phase-2 stress test: bootstrap CI · beta/alpha split · 6/23 real-path validation."""

from __future__ import annotations

import argparse
import math
import random
import sqlite3
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from statistics import mean, median

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from stock_db import DEFAULT_DB_PATH, connect

CORE4 = ("3264", "2327", "2449", "3211")
HOLDINGS_623 = ("2337", "3264", "2327", "2449", "3008", "3211", "5347")
MIN_BARS = 200
CHECK_MINUTE = "09:05:00"
MARKET_STOCK = "2330"
BOOTSTRAP_N = 10_000
SEED = 42


@dataclass
class TradeDay:
    trade_date: str
    sync_dd3: bool
    port_ret: float
    mkt_ret: float
    n_check: int


@dataclass
class E1Row:
    trade_date: str
    stock_id: str
    sync_dd3: bool
    save_pct: float  # (close-fill)/prev_close*100; negative => exit better
    exit_ret_pct: float
    hold_ret_pct: float
    mkt_exit_ret_pct: float | None
    mkt_hold_ret_pct: float | None


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


def trading_dates(conn: sqlite3.Connection, start: str, end: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT trade_date FROM stock_daily_bars
        WHERE stock_id = '2449' AND trade_date BETWEEN ? AND ?
        ORDER BY trade_date
        """,
        (start, end),
    ).fetchall()
    return [str(r[0]) for r in rows]


def px_at(bars: list[dict], minute: str) -> float | None:
    last = None
    for b in bars:
        if b["minute"] <= minute:
            last = float(b["close"])
        else:
            break
    return last


def max_dd_pct(bars: list[dict], prev: float) -> float:
    return (min(float(b["low"]) for b in bars) / prev - 1) * 100


def first_hit(bars: list[dict], level: float) -> tuple[str, float] | None:
    for b in bars:
        if float(b["low"]) <= level:
            return str(b["minute"]), max(float(b["close"]), level)
    return None


def fill_after_lag(bars: list[dict], trigger_minute: str, lag_min: int = 1) -> float:
    t0 = (int(trigger_minute[:2]) - 9) * 60 + int(trigger_minute[3:5]) + lag_min
    for b in bars:
        tm = (int(b["minute"][:2]) - 9) * 60 + int(b["minute"][3:5])
        if tm >= t0:
            return float(b["close"])
    return float(bars[-1]["close"])


def index_ret(conn: sqlite3.Connection, trade_date: str) -> float | None:
    prev = conn.execute(
        "SELECT close FROM daily_bars WHERE code='IX0001' AND date < ? ORDER BY date DESC LIMIT 1",
        (trade_date,),
    ).fetchone()
    today = conn.execute(
        "SELECT close FROM daily_bars WHERE code='IX0001' AND date = ?",
        (trade_date,),
    ).fetchone()
    if not prev or not today:
        return None
    return (float(today[0]) / float(prev[0]) - 1) * 100


def build_trade_days(conn: sqlite3.Connection, dates: list[str]) -> list[TradeDay]:
    out: list[TradeDay] = []
    for d in dates:
        n_check = n_dd3 = 0
        rets: list[float] = []
        ok = True
        for sid in CORE4:
            bars = load_day_bars(conn, sid, d)
            pc = prev_close(conn, sid, d)
            if len(bars) < MIN_BARS or not pc:
                ok = False
                break
            px = px_at(bars, CHECK_MINUTE)
            if px and px <= pc * 0.98:
                n_check += 1
            if max_dd_pct(bars, pc) <= -3:
                n_dd3 += 1
            rets.append((float(bars[-1]["close"]) / pc - 1) * 100)
        if not ok:
            continue
        mkt = index_ret(conn, d)
        if mkt is None:
            continue
        out.append(
            TradeDay(
                trade_date=d,
                sync_dd3=n_dd3 >= 3,
                port_ret=mean(rets),
                mkt_ret=mkt,
                n_check=n_check,
            )
        )
    return out


def build_e1_rows(
    conn: sqlite3.Connection,
    dates: list[str],
    *,
    threshold_pct: float = 2.0,
    lag_min: int = 1,
    slip_pct: float = 0.5,
) -> list[E1Row]:
    sync_dates = {t.trade_date for t in build_trade_days(conn, dates) if t.sync_dd3}
    rows: list[E1Row] = []
    for d in dates:
        m_bars = load_day_bars(conn, MARKET_STOCK, d)
        m_pc = prev_close(conn, MARKET_STOCK, d)
        for sid in CORE4:
            bars = load_day_bars(conn, sid, d)
            pc = prev_close(conn, sid, d)
            if len(bars) < MIN_BARS or not pc:
                continue
            hit = first_hit(bars, pc * (1 - threshold_pct / 100))
            if not hit:
                continue
            tm, _ = hit
            fill = fill_after_lag(bars, tm, lag_min) * (1 - slip_pct / 100)
            close_px = float(bars[-1]["close"])
            save_pct = (close_px - fill) / pc * 100
            exit_ret = (fill / pc - 1) * 100
            hold_ret = (close_px / pc - 1) * 100
            m_exit = m_hold = None
            if m_bars and m_pc and len(m_bars) >= MIN_BARS:
                m_fill = fill_after_lag(m_bars, tm, lag_min)
                m_close = float(m_bars[-1]["close"])
                m_exit = (m_fill / m_pc - 1) * 100
                m_hold = (m_close / m_pc - 1) * 100
            rows.append(
                E1Row(
                    trade_date=d,
                    stock_id=sid,
                    sync_dd3=d in sync_dates,
                    save_pct=save_pct,
                    exit_ret_pct=exit_ret,
                    hold_ret_pct=hold_ret,
                    mkt_exit_ret_pct=m_exit,
                    mkt_hold_ret_pct=m_hold,
                )
            )
    return rows


def ols_beta_alpha(xs: list[float], ys: list[float]) -> tuple[float, float]:
    if len(xs) < 3:
        return 0.0, 0.0
    mx, my = mean(xs), mean(ys)
    var_x = sum((x - mx) ** 2 for x in xs)
    if var_x <= 0:
        return 0.0, my
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    beta = cov / var_x
    alpha = my - beta * mx
    return beta, alpha


def percentile(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return float("nan")
    k = (len(sorted_vals) - 1) * p / 100
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return sorted_vals[int(k)]
    return sorted_vals[f] * (c - k) + sorted_vals[c] * (k - f)


def bootstrap_ci(values: list[float], n_boot: int = BOOTSTRAP_N, seed: int = SEED) -> dict[str, float]:
    rng = random.Random(seed)
    n = len(values)
    if n == 0:
        return {}
    meds = []
    means = []
    wins = []
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
        "obs_win_rate": sum(1 for v in values if v < 0) / n,
        "med_p2.5": percentile(meds, 2.5),
        "med_p50": percentile(meds, 50),
        "med_p97.5": percentile(meds, 97.5),
        "mean_p2.5": percentile(means, 2.5),
        "mean_p97.5": percentile(means, 97.5),
        "win_p2.5": percentile(wins, 2.5),
        "win_p97.5": percentile(wins, 97.5),
    }


def aggregate_tx_ticks_to_1m(conn: sqlite3.Connection, trade_date: str) -> list[dict]:
    """Use cached stock_kbar if exists; else skip (FinMind tick fetch is on-demand)."""
    rows = conn.execute(
        """
        SELECT minute, close FROM stock_kbar_1m
        WHERE stock_id = 'TX1!' AND trade_date = ? AND source = 'finmind_fut'
        ORDER BY minute
        """,
        (trade_date,),
    ).fetchall()
    return [dict(r) for r in rows]


def fetch_and_cache_tx_1m(conn: sqlite3.Connection, trade_date: str, *, quiet: bool = True) -> int:
    from collections import defaultdict

    from project_dotenv import load_project_dotenv
    from finmind_client import fetch_finmind
    from stock_db import upsert_stock_kbar_1m

    load_project_dotenv()
    d = date.fromisoformat(trade_date)
    try:
        ticks = fetch_finmind("TaiwanFuturesTick", "TX", d, d)
    except Exception as exc:
        if not quiet:
            print(f"  TX tick {trade_date}: {exc}")
        return 0
    if not ticks:
        return 0
    buckets: dict[str, list[float]] = defaultdict(list)
    for t in ticks:
        ts = str(t.get("date") or "")
        if " " not in ts:
            continue
        minute = ts.split(" ", 1)[1][:8]
        if not minute.startswith("0") and not minute.startswith("1"):
            continue
        if minute < "08:45:00" or minute > "13:45:00":
            continue
        px = t.get("price")
        if px is None:
            continue
        buckets[minute].append(float(px))
    db_rows = []
    for minute in sorted(buckets):
        prices = buckets[minute]
        o, h, l, c = prices[0], max(prices), min(prices), prices[-1]
        db_rows.append(
            {
                "stock_id": "TX1!",
                "trade_date": trade_date,
                "minute": minute,
                "open": o,
                "high": h,
                "low": l,
                "close": c,
                "volume": len(prices),
                "source": "finmind_fut",
            }
        )
    if db_rows:
        return upsert_stock_kbar_1m(conn, db_rows)
    return 0


def beta_alpha_report(conn: sqlite3.Connection, days: list[TradeDay], e1_rows: list[E1Row]) -> None:
    print("\n【Beta / Alpha 分離】")

    sync_days = [d for d in days if d.sync_dd3]
    nons = [d for d in days if not d.sync_dd3]

    for label, subset in [("全日", days), ("同步急跌日", sync_days), ("非同步", nons)]:
        xs = [d.mkt_ret for d in subset]
        ys = [d.port_ret for d in subset]
        if len(xs) < 5:
            continue
        beta, alpha = ols_beta_alpha(xs, ys)
        resid = [y - (alpha + beta * x) for x, y in zip(xs, ys)]
        print(
            f"  {label} n={len(subset)}: "
            f"port = {alpha:+.2f}% + {beta:.2f}×台指  (日線 OLS)"
        )
        if resid:
            print(f"    殘差(α)日均={mean(resid):+.2f}% · |残差|中位={median([abs(r) for r in resid]):.2f}%")

    # Intraday E1: decompose exit edge vs 2330 vs TX
    sync_e1 = [r for r in e1_rows if r.sync_dd3 and r.mkt_exit_ret_pct is not None]
    if len(sync_e1) >= 10:
        stock_saves = [r.save_pct for r in sync_e1]
        mkt_saves = [
            (r.mkt_hold_ret_pct or 0) - (r.mkt_exit_ret_pct or 0)
            for r in sync_e1
        ]
        # negative stock_save means exit beat hold
        idio = [s - m for s, m in zip(stock_saves, mkt_saves)]
        print(
            f"\n  E1 同步日股日 n={len(sync_e1)} (2330 分K對齊 · 滑價0.5%):"
        )
        print(
            f"    個股 save 中位={median(stock_saves):+.2f}% · "
            f"2330 同期 save 中位={median(mkt_saves):+.2f}% · "
            f"超額(idio) 中位={median(idio):+.2f}%"
        )
        print(
            f"    → 2330 同期 save / 個股 save 中位比 ≈ "
            f"{(median(mkt_saves) / median(stock_saves)) if median(stock_saves) else float('nan'):.2f}"
        )

    # TX futures 1m proxy on recent sync days (fetch ticks if missing)
    tx_dates = [d.trade_date for d in sync_days[-5:]]
    tx_pairs = []
    for d in tx_dates:
        n = conn.execute(
            "SELECT COUNT(*) FROM stock_kbar_1m WHERE stock_id='TX1!' AND trade_date=?",
            (d,),
        ).fetchone()[0]
        if n < 50:
            fetch_and_cache_tx_1m(conn, d)
        tx_rows = conn.execute(
            """
            SELECT minute, close FROM stock_kbar_1m
            WHERE stock_id='TX1!' AND trade_date=? AND source='finmind_fut'
            ORDER BY minute
            """,
            (d,),
        ).fetchall()
        if len(tx_rows) < 50:
            continue
        # use first CORE4 stock that triggered E1
        for sid in CORE4:
            bars = load_day_bars(conn, sid, d)
            pc = prev_close(conn, sid, d)
            if len(bars) < MIN_BARS or not pc:
                continue
            hit = first_hit(bars, pc * 0.98)
            if not hit:
                continue
            tm, fill_s = hit
            fill_s = fill_after_lag(bars, tm, 1) * 0.995
            close_s = float(bars[-1]["close"])
            save_s = (close_s - fill_s) / pc * 100
            # TX prev: use IX0001 prev as futures reference is messy; use first TX bar as open proxy
            tx_list = [dict(r) for r in tx_rows]
            tx_fill = fill_after_lag(
                [{"minute": r["minute"], "close": r["close"]} for r in tx_list],
                tm,
                1,
            )
            tx_close = float(tx_list[-1]["close"])
            tx_open = float(tx_list[0]["close"])
            save_tx = (tx_close - tx_fill) / tx_open * 100
            tx_pairs.append((save_s, save_tx))
            break
    if tx_pairs:
        corr = pearson([a for a, _ in tx_pairs], [b for _, b in tx_pairs])
        print(f"\n  台指期 TX 1m（近{len(tx_pairs)}個 sync 日抽樣）:")
        print(f"    個股 save vs TX save Pearson r={corr:.3f}")
        print(f"    中位 個股={median([a for a,_ in tx_pairs]):+.2f}% TX={median([b for _,b in tx_pairs]):+.2f}%")
    else:
        print("\n  台指期 TX 1m: 樣本不足（需 FinMind tick 聚合）")


def pearson(xs: list[float], ys: list[float]) -> float:
    if len(xs) < 2:
        return float("nan")
    mx, my = mean(xs), mean(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den = math.sqrt(sum((x - mx) ** 2 for x in xs) * sum((y - my) ** 2 for y in ys))
    return num / den if den else float("nan")


def validate_20260623(conn: sqlite3.Connection) -> None:
    target = "2026-06-23"
    print(f"\n【6/23 真實路徑驗證】trade_date={target}")
    any_data = False
    for sid in HOLDINGS_623:
        n = conn.execute(
            "SELECT COUNT(*) FROM stock_kbar_1m WHERE stock_id=? AND trade_date=?",
            (sid, target),
        ).fetchone()[0]
        if n:
            any_data = True
    if not any_data:
        print("  ⚠ DB 尚無 6/23 分K（FinMind/Yahoo 皆回傳空）。")
        print("  收盤後重跑: python scripts/backfill_rrg_lens_backtest_data.py \\")
        print("    --layers kbar --start 2026-06-23 --end 2026-06-23 --kbar-1m-only")
        print("  再跑: python scripts/stress_test_intraday_exit_phase2.py --validate-623")
        return

    sync_n = 0
    print(f"  {'代號':<6} {'觸發':>6} {'時間':>8} {'賣價':>8} {'收盤':>8} {'save':>7} {'09:05≤-2%':>10}")
    for sid in HOLDINGS_623:
        bars = load_day_bars(conn, sid, target)
        pc = prev_close(conn, sid, target)
        if not bars or not pc:
            print(f"  {sid:<6} 無資料")
            continue
        hit = first_hit(bars, pc * 0.98)
        px5 = px_at(bars, CHECK_MINUTE)
        flag = "Y" if px5 and px5 <= pc * 0.98 else "N"
        if hit:
            tm, _ = hit
            fill = fill_after_lag(bars, tm, 1) * 0.995
            close_px = float(bars[-1]["close"])
            save = (close_px - fill) / pc * 100
            print(
                f"  {sid:<6} {'E1':>6} {tm[:5]:>8} {fill:>8.1f} {close_px:>8.1f} "
                f"{save:>+6.2f}% {flag:>10}"
            )
        else:
            close_px = float(bars[-1]["close"])
            ret = (close_px / pc - 1) * 100
            print(f"  {sid:<6} {'—':>6} {'—':>8} {'—':>8} {close_px:>8.1f} {ret:>+6.2f}% {flag:>10}")
        if px5 and px5 <= pc * 0.98:
            sync_n += 1
    print(f"  09:05 ≤昨收-2% 檔數: {sync_n} / {len(HOLDINGS_623)}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Intraday exit stress test phase 2")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--start", default="2026-01-02")
    parser.add_argument("--end", default="2026-06-22")
    parser.add_argument("--bootstrap-n", type=int, default=BOOTSTRAP_N)
    parser.add_argument("--validate-623", action="store_true")
    parser.add_argument("--fetch-tx", action="store_true", help="Fetch TX tick→1m for sync days")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    dates = trading_dates(conn, args.start, args.end)
    days = build_trade_days(conn, dates)
    e1_all = build_e1_rows(conn, dates)
    e1_sync = [r for r in e1_all if r.sync_dd3]

    print("=" * 72)
    print(f"PHASE 2 · CORE4 · {args.start}..{args.end} · eval_days={len(days)} sync={sum(d.sync_dd3 for d in days)}")

    # 1 Bootstrap
    print("\n【1 Bootstrap 信賴區間】E1(-2%) · 滑價0.5% · +1分 · save%（負=賣比收盤好）")

    day_level_saves: list[float] = []
    by_day: dict[str, list[float]] = {}
    for r in e1_sync:
        by_day.setdefault(r.trade_date, []).append(r.save_pct)
    for d, saves in by_day.items():
        day_level_saves.append(mean(saves))

    for label, vals in [
        ("同步日·股日", [r.save_pct for r in e1_sync]),
        ("同步日·日均", day_level_saves),
        ("非同步·股日", [r.save_pct for r in e1_all if not r.sync_dd3]),
    ]:
        ci = bootstrap_ci(vals, n_boot=args.bootstrap_n)
        if not ci:
            continue
        print(f"\n  {label} n={ci['n']}")
        print(f"    觀測: median={ci['obs_median']:+.2f}% mean={ci['obs_mean']:+.2f}% win={ci['obs_win_rate']:.1%}")
        print(
            f"    median 95% CI: [{ci['med_p2.5']:+.2f}%, {ci['med_p97.5']:+.2f}%]"
        )
        print(
            f"    mean  95% CI: [{ci['mean_p2.5']:+.2f}%, {ci['mean_p97.5']:+.2f}%]"
        )
        print(
            f"    win_rate 95% CI: [{ci['win_p2.5']:.1%}, {ci['win_p97.5']:.1%}]"
        )
        # one-sided: P(true median save >= 0) i.e. exit not helpful
        if label.startswith("同步"):
            pass

    # Hypothesis test: is sync median save < 0?
    sync_saves = [r.save_pct for r in e1_sync]
    if sync_saves:
        boot_med = []
        rng = random.Random(SEED)
        n = len(sync_saves)
        for _ in range(args.bootstrap_n):
            s = [sync_saves[rng.randrange(n)] for _ in range(n)]
            boot_med.append(median(s))
        boot_med.sort()
        p_exit_helps = sum(1 for m in boot_med if m < 0) / args.bootstrap_n
        print(f"\n  H0: 同步日早賣無優勢 (median save ≥ 0)")
        print(f"    P(median save < 0 | data) ≈ {p_exit_helps:.1%}")

    # 2 Beta alpha
    beta_alpha_report(conn, days, e1_all)
    if args.fetch_tx:
        print("\n  (--fetch-tx: 已於 beta 報告中嘗試抓取近 5 個 sync 日 TX tick)")

    # 3 6/23
    if args.validate_623:
        validate_20260623(conn)
    else:
        validate_20260623(conn)

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
