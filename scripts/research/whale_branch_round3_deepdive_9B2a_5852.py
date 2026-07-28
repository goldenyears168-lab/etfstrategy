"""Deep-dive on two H14/H20-only candidates: 9B2a and 5852.

Pulls branch name, daily trading footprint (buy/sell balance, trade frequency,
concentration by stock) from stock_broker_branch_daily on the mini DB.
Run this ON MINI via SSH (per project protocol).
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402

BRANCHES = ["9B2a", "5852"]


def main() -> None:
    conn = connect(DEFAULT_DB_PATH)
    cur = conn.cursor()

    print("=" * 80)
    print("1. BRANCH NAME LOOKUP")
    print("=" * 80)
    for b in BRANCHES:
        cur.execute(
            "SELECT DISTINCT securities_trader_id, securities_trader "
            "FROM stock_broker_branch_daily WHERE securities_trader_id = ? LIMIT 5",
            (b,),
        )
        rows = cur.fetchall()
        print(b, "->", rows)

    for b in BRANCHES:
        print()
        print("=" * 80)
        print(f"2. FOOTPRINT SUMMARY FOR {b}")
        print("=" * 80)

        cur.execute(
            """
            SELECT MIN(trade_date), MAX(trade_date), COUNT(DISTINCT trade_date),
                   COUNT(DISTINCT stock_id)
            FROM stock_broker_branch_daily
            WHERE securities_trader_id = ? AND (buy > 0 OR sell > 0) AND stock_id != '__EMPTY__'
            """,
            (b,),
        )
        min_d, max_d, n_days, n_stocks = cur.fetchone()
        print(f"active date range (buy>0 or sell>0): {min_d} ~ {max_d}, distinct trade_dates={n_days}, distinct stocks={n_stocks}")

        # total calendar trading days in range for activity ratio
        cur.execute(
            "SELECT COUNT(DISTINCT trade_date) FROM stock_broker_branch_daily WHERE trade_date BETWEEN ? AND ?",
            (min_d, max_d),
        )
        total_mkt_days = cur.fetchone()[0]
        print(f"market trading days in that window: {total_mkt_days}  -> activity ratio = {n_days/total_mkt_days:.2%}")

        # buy/sell balance overall
        cur.execute(
            """
            SELECT SUM(buy), SUM(sell), COUNT(*) as n_rows
            FROM stock_broker_branch_daily
            WHERE securities_trader_id = ? AND stock_id != '__EMPTY__'
            """,
            (b,),
        )
        buy_amt, sell_amt, n_rows = cur.fetchone()
        buy_amt = buy_amt or 0
        sell_amt = sell_amt or 0
        print(f"n_rows(stock-day records)={n_rows}")
        print(f"total buy={buy_amt:,.0f}  total sell={sell_amt:,.0f}")
        if buy_amt + sell_amt > 0:
            print(f"buy ratio = {buy_amt/(buy_amt+sell_amt):.3f}  (0.5 = perfectly balanced -> market-making signature)")
        net = buy_amt - sell_amt
        print(f"net amount = {net:,.0f}")

        # per stock-day buy/sell balance distribution (how many days is it also selling same stock)
        cur.execute(
            """
            SELECT stock_id, trade_date, buy, sell
            FROM stock_broker_branch_daily
            WHERE securities_trader_id = ? AND stock_id != '__EMPTY__'
            ORDER BY trade_date
            """,
            (b,),
        )
        rows = cur.fetchall()
        both_sides = sum(1 for r in rows if r[2] > 0 and r[3] > 0)
        buy_only = sum(1 for r in rows if r[2] > 0 and r[3] == 0)
        sell_only = sum(1 for r in rows if r[2] == 0 and r[3] > 0)
        print(f"stock-days with BOTH buy&sell (same day, same stock) = {both_sides} ({both_sides/n_rows:.1%})")
        print(f"stock-days BUY-only = {buy_only} ({buy_only/n_rows:.1%})")
        print(f"stock-days SELL-only = {sell_only} ({sell_only/n_rows:.1%})")

        # concentration: top 10 stocks by total buy amount
        cur.execute(
            """
            SELECT stock_id, SUM(buy) as tb, SUM(sell) as ts, COUNT(*) as n
            FROM stock_broker_branch_daily
            WHERE securities_trader_id = ? AND stock_id != '__EMPTY__'
            GROUP BY stock_id
            ORDER BY tb DESC
            LIMIT 15
            """,
            (b,),
        )
        print("\ntop 15 stocks by total buy (stock_id, buy_amt, sell_amt, n_days):")
        for r in cur.fetchall():
            print("  ", r)

        # how many distinct stocks account for 50%/80% of buy volume
        cur.execute(
            """
            SELECT stock_id, SUM(buy) as tb
            FROM stock_broker_branch_daily
            WHERE securities_trader_id = ? AND stock_id != '__EMPTY__'
            GROUP BY stock_id
            ORDER BY tb DESC
            """,
            (b,),
        )
        all_stocks = cur.fetchall()
        total_buy = sum(r[1] for r in all_stocks)
        cum = 0
        n50 = n80 = None
        for i, (sid, tb) in enumerate(all_stocks, start=1):
            cum += tb
            if n50 is None and cum >= 0.5 * total_buy:
                n50 = i
            if n80 is None and cum >= 0.8 * total_buy:
                n80 = i
        print(f"\n# distinct stocks needed for 50% of buy volume: {n50} / {len(all_stocks)} total stocks")
        print(f"# distinct stocks needed for 80% of buy volume: {n80} / {len(all_stocks)} total stocks")

    conn.close()


if __name__ == "__main__":
    main()
