"""
Item AQ (100-item creative-combo plan, wave 8): is ETF-constituent membership
(0050/0056) itself a confound/predictor for dayflip-short pnl, independent of
item R's si_lend_pct finding -- given item AK found lending-data coverage is a
deterministic function of ETF-watchlist membership (sync_stock_chip_daily.py
only pulls lending for load_etf_constituent_watchlist(conn)).

Read-only DB. Reuses the 190-trade / 38-stock dayflip-short reconstruction from
item B (reports/research/dayflip_revenue_momentum_filter/trades_with_revyoy.csv)
and item R's lending crosscheck (reports/research/asquith_dayflip_crosscheck/
trades_with_si.csv).
"""
from __future__ import annotations

import csv
import json
import random
import sqlite3
import sys
from pathlib import Path
from statistics import mean, median

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from stock_db import DEFAULT_DB_PATH  # noqa: E402
from stock_db.etf import load_etf_constituent_watchlist  # noqa: E402

OUT_DIR = Path("reports/research/dayflip_etf_membership_confound")
OUT_DIR.mkdir(parents=True, exist_ok=True)

TRADES_CSV = Path("reports/research/dayflip_revenue_momentum_filter/trades_with_revyoy.csv")
LENDING_CSV = Path("reports/research/asquith_dayflip_crosscheck/trades_with_si.csv")

random.seed(20260808)
N_PERM = 20000


def load_trades():
    with TRADES_CSV.open() as f:
        return list(csv.DictReader(f))


def load_lending():
    with LENDING_CSV.open() as f:
        rows = list(csv.DictReader(f))
    by_key = {}
    for r in rows:
        key = (r["signal_date"], r["stock"])
        by_key[key] = r
    return by_key


def benchmark_snapshot_dates(conn, code):
    return [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT snapshot_date FROM benchmark_constituents_meta "
            "WHERE benchmark_code = ? ORDER BY snapshot_date",
            (code,),
        )
    ]


def pit_snapshot_for(dates, as_of):
    """Latest snapshot_date <= as_of; falls back to earliest if none (shouldn't happen here)."""
    candidates = [d for d in dates if d <= as_of]
    if candidates:
        return candidates[-1]
    return None


def load_constituents(conn, code, snapshot_date):
    return {
        r[0]
        for r in conn.execute(
            "SELECT stock_id FROM benchmark_constituents WHERE benchmark_code = ? AND snapshot_date = ?",
            (code, snapshot_date),
        )
    }


def permutation_test_meandiff(a, b, n=N_PERM):
    """Two-sided permutation test on difference of means (a - b)."""
    obs = mean(a) - mean(b)
    pooled = a + b
    na = len(a)
    count = 0
    for _ in range(n):
        random.shuffle(pooled)
        pa = pooled[:na]
        pb = pooled[na:]
        d = mean(pa) - mean(pb)
        if abs(d) >= abs(obs):
            count += 1
    return obs, (count + 1) / (n + 1)


def winrate(pnls):
    return sum(1 for p in pnls if p > 0) / len(pnls)


def permutation_test_winrate(a, b, n=N_PERM):
    obs = winrate(a) - winrate(b)
    pooled = a + b
    na = len(a)
    count = 0
    for _ in range(n):
        random.shuffle(pooled)
        pa = pooled[:na]
        pb = pooled[na:]
        d = winrate(pa) - winrate(pb)
        if abs(d) >= abs(obs):
            count += 1
    return obs, (count + 1) / (n + 1)


def summarize(label, pnls):
    return {
        "label": label,
        "n": len(pnls),
        "mean_pnl_pct": round(mean(pnls), 4),
        "median_pnl_pct": round(median(pnls), 4),
        "win_rate": round(winrate(pnls), 4),
    }


def main():
    conn = sqlite3.connect(f"file:{DEFAULT_DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    trades = load_trades()
    lending = load_lending()
    stocks = sorted({t["stock"] for t in trades})
    print(f"loaded {len(trades)} trades, {len(stocks)} stocks")

    # --- PIT 0050/0056 membership ---
    dates_0050 = benchmark_snapshot_dates(conn, "0050")
    dates_0056 = benchmark_snapshot_dates(conn, "0056")
    snap_cache = {}

    def is_member_pit(stock_id, as_of):
        for code, dates in (("0050", dates_0050), ("0056", dates_0056)):
            snap = pit_snapshot_for(dates, as_of)
            if snap is None:
                continue
            key = (code, snap)
            if key not in snap_cache:
                snap_cache[key] = load_constituents(conn, code, snap)
            if stock_id in snap_cache[key]:
                return True
        return False

    # current (most-recent-snapshot) 0050/0056 membership, for comparison
    latest_0050 = dates_0050[-1]
    latest_0056 = dates_0056[-1]
    members_0050_now = load_constituents(conn, "0050", latest_0050)
    members_0056_now = load_constituents(conn, "0056", latest_0056)
    current_0050_0056 = members_0050_now | members_0056_now

    # broad chip-sync watchlist (current only -- function only supports latest snapshot)
    broad_watchlist = {row["stock_id"] for row in load_etf_constituent_watchlist(conn)}

    per_stock_membership = {}
    for s in stocks:
        # PIT flag: member on ANY of that stock's trade signal_dates (should be
        # stable within our ~1yr window since 0050/0056 rebalance quarterly and
        # stocks in this dataset rarely flip in/out mid-window, but compute per-trade)
        per_stock_membership[s] = {
            "current_0050_0056": s in current_0050_0056,
            "broad_watchlist_current": s in broad_watchlist,
        }

    for t in trades:
        t["pnl_pct"] = float(t["pnl_pct"])
        t["etf_0050_0056_pit"] = is_member_pit(t["stock"], t["signal_date"])
        t["etf_0050_0056_current"] = t["stock"] in current_0050_0056
        t["broad_watchlist_current"] = t["stock"] in broad_watchlist

    # sanity: PIT vs current flip count
    flips = sum(1 for t in trades if t["etf_0050_0056_pit"] != t["etf_0050_0056_current"])
    print(f"PIT vs current 0050/0056 flag disagreement: {flips}/{len(trades)} trades")

    # --- split 1: 0050/0056 PIT membership, all 190 trades ---
    member_pnls = [t["pnl_pct"] for t in trades if t["etf_0050_0056_pit"]]
    nonmember_pnls = [t["pnl_pct"] for t in trades if not t["etf_0050_0056_pit"]]
    member_stocks = sorted({t["stock"] for t in trades if t["etf_0050_0056_pit"]})
    nonmember_stocks = sorted({t["stock"] for t in trades if not t["etf_0050_0056_pit"]})

    diff_mean, p_mean = permutation_test_meandiff(list(member_pnls), list(nonmember_pnls))
    diff_wr, p_wr = permutation_test_winrate(list(member_pnls), list(nonmember_pnls))

    split_0050_0056 = {
        "member": summarize("0050/0056 constituent (PIT)", member_pnls),
        "nonmember": summarize("not 0050/0056 constituent (PIT)", nonmember_pnls),
        "member_stock_count": len(member_stocks),
        "nonmember_stock_count": len(nonmember_stocks),
        "member_stocks": member_stocks,
        "nonmember_stocks": nonmember_stocks,
        "mean_pnl_diff_member_minus_nonmember": round(diff_mean, 4),
        "perm_p_mean": round(p_mean, 4),
        "winrate_diff_member_minus_nonmember": round(diff_wr, 4),
        "perm_p_winrate": round(p_wr, 4),
    }

    # stock-level split (independence-robust): mean pnl per stock, then compare groups
    stock_mean_pnl = {}
    for t in trades:
        stock_mean_pnl.setdefault(t["stock"], []).append(t["pnl_pct"])
    stock_mean_pnl = {s: mean(v) for s, v in stock_mean_pnl.items()}
    member_stock_means = [stock_mean_pnl[s] for s in member_stocks]
    nonmember_stock_means = [stock_mean_pnl[s] for s in nonmember_stocks]
    diff_stockmean, p_stockmean = permutation_test_meandiff(
        list(member_stock_means), list(nonmember_stock_means)
    )
    split_0050_0056["stock_level"] = {
        "member_stock_mean_of_means": round(mean(member_stock_means), 4) if member_stock_means else None,
        "nonmember_stock_mean_of_means": round(mean(nonmember_stock_means), 4) if nonmember_stock_means else None,
        "diff": round(diff_stockmean, 4),
        "perm_p": round(p_stockmean, 4),
    }

    # --- split 2: broad chip-sync watchlist membership (current snapshot only) ---
    bw_member_pnls = [t["pnl_pct"] for t in trades if t["broad_watchlist_current"]]
    bw_nonmember_pnls = [t["pnl_pct"] for t in trades if not t["broad_watchlist_current"]]
    diff_bw, p_bw = permutation_test_meandiff(list(bw_member_pnls), list(bw_nonmember_pnls))
    diff_bw_wr, p_bw_wr = permutation_test_winrate(list(bw_member_pnls), list(bw_nonmember_pnls))
    split_broad = {
        "member": summarize("broad chip-sync watchlist member", bw_member_pnls),
        "nonmember": summarize("not in broad chip-sync watchlist", bw_nonmember_pnls),
        "mean_pnl_diff": round(diff_bw, 4),
        "perm_p_mean": round(p_bw, 4),
        "winrate_diff": round(diff_bw_wr, 4),
        "perm_p_winrate": round(p_bw_wr, 4),
    }

    # --- collinearity check: within item R's 24-stock/139-trade lending-covered
    # subsample, recompute si_lend_pct terciles and crosstab against 0050/0056
    # PIT membership ---
    covered = []
    for t in trades:
        key = (t["signal_date"], t["stock"])
        lr = lending.get(key)
        if lr is None:
            continue
        si = lr.get("si_lend_pct", "")
        if si in ("", "nan", None):
            continue
        covered.append(
            {
                "stock": t["stock"],
                "signal_date": t["signal_date"],
                "pnl_pct": t["pnl_pct"],
                "si_lend_pct": float(si),
                "etf_0050_0056_pit": t["etf_0050_0056_pit"],
            }
        )
    print(f"lending-covered trades matched: {len(covered)} (expect 139)")

    covered_sorted = sorted(covered, key=lambda r: r["si_lend_pct"])
    n = len(covered_sorted)
    t1 = n // 3
    t2 = 2 * n // 3
    for i, r in enumerate(covered_sorted):
        if i < t1:
            r["lend_tercile"] = "low"
        elif i < t2:
            r["lend_tercile"] = "mid"
        else:
            r["lend_tercile"] = "high"

    crosstab = {}
    for tercile in ("low", "mid", "high"):
        rows = [r for r in covered_sorted if r["lend_tercile"] == tercile]
        n_member = sum(1 for r in rows if r["etf_0050_0056_pit"])
        n_total = len(rows)
        stocks_in_tercile = sorted({r["stock"] for r in rows})
        member_stocks_in_tercile = sorted({r["stock"] for r in rows if r["etf_0050_0056_pit"]})
        crosstab[tercile] = {
            "n_trades": n_total,
            "n_trades_0050_0056_member": n_member,
            "pct_trades_0050_0056_member": round(n_member / n_total, 4) if n_total else None,
            "n_stocks": len(stocks_in_tercile),
            "n_stocks_0050_0056_member": len(member_stocks_in_tercile),
            "stocks": stocks_in_tercile,
            "member_stocks": member_stocks_in_tercile,
        }

    # overall covered-subsample: all 24 stocks are broad-watchlist members by
    # construction (sync only fetches lending for that watchlist) -- verify
    covered_stock_ids = sorted({r["stock"] for r in covered})
    broad_check = {s: (s in broad_watchlist) for s in covered_stock_ids}
    narrow_check = {s: (s in current_0050_0056) for s in covered_stock_ids}

    out = {
        "n_trades_total": len(trades),
        "n_stocks_total": len(stocks),
        "pit_vs_current_flag_disagreement": flips,
        "split_0050_0056_pit_membership": split_0050_0056,
        "split_broad_watchlist_membership": split_broad,
        "lending_covered_subsample": {
            "n_trades": len(covered),
            "n_stocks": len(covered_stock_ids),
            "all_covered_stocks_in_broad_watchlist": all(broad_check.values()),
            "broad_watchlist_check_per_stock": broad_check,
            "n_covered_stocks_that_are_0050_0056": sum(narrow_check.values()),
            "narrow_0050_0056_check_per_stock": narrow_check,
            "lend_tercile_x_0050_0056_crosstab": crosstab,
        },
    }

    (OUT_DIR / "summary.json").write_text(json.dumps(out, ensure_ascii=False, indent=2))

    fieldnames = [
        "signal_date", "trade_date", "stock", "pnl_pct", "how", "fgap", "n_seats",
        "etf_0050_0056_pit", "etf_0050_0056_current", "broad_watchlist_current",
    ]
    with (OUT_DIR / "trades_with_etf_membership.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for t in trades:
            w.writerow({k: t[k] for k in fieldnames})

    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
