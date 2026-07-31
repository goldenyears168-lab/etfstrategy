"""Build the market-level chip (籌碼) research panel.

Merges three FinMind-sourced data families into one daily panel keyed by date:
  1. Market-wide institutional net buy/sell (cash market), 億 NTD
       dataset: TaiwanStockTotalInstitutionalInvestors
       -> foreign / trust / dealer_self / dealer_hedge / total  (net = buy - sell)
  2. TAIEX-futures (TX) institutional open-interest net position (口)
       dataset: TaiwanFuturesInstitutionalInvestors (data_id=TX)
       -> fut_foreign_oi / fut_trust_oi / fut_dealer_oi (long_oi - short_oi)
         plus day-over-day change columns *_doi
  3. TAIEX index OHLC (IX0001, source=finmind) from data/stocks.db
       -> ix_open / ix_high / ix_low / ix_close

Output: data/research/chip_macro/panel.parquet  (+ panel.csv for inspection)

Signal timing convention: every chip column on row T is information KNOWN AFTER
the close of day T (institutional stats publish post-close). The earliest
tradable point is the OPEN of day T+1. Forward-return columns are therefore
computed elsewhere from ix_open shifted by one day.
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from finmind_client import fetch_finmind, fetch_finmind_dataset  # noqa: E402

DB_PATH = ROOT / "data" / "stocks.db"
OUT_DIR = ROOT / "data" / "research" / "chip_macro"
START = date(2018, 6, 1)
END = date.today()  # dynamic: always pull up to today (daily tracker re-runs this)

OKU = 1e8  # 100 million (億)


def fetch_market_totals(start: date, end: date) -> pd.DataFrame:
    """Market-wide institutional net (億), fetched year-by-year to bound response size."""
    frames = []
    y0, y1 = start.year, end.year
    for yr in range(y0, y1 + 1):
        s = max(start, date(yr, 1, 1))
        e = min(end, date(yr, 12, 31))
        raw = fetch_finmind_dataset(
            "TaiwanStockTotalInstitutionalInvestors", start=s, end=e, timeout=120
        )
        print(f"  totals {yr}: {len(raw)} rows")
        for r in raw:
            b = float(r.get("buy") or 0)
            sv = float(r.get("sell") or 0)
            frames.append(
                {"date": str(r["date"])[:10], "name": r.get("name"), "net": (b - sv) / OKU}
            )
    df = pd.DataFrame(frames)
    piv = df.pivot_table(index="date", columns="name", values="net", aggfunc="sum")
    rename = {
        "Foreign_Investor": "foreign",
        "Investment_Trust": "trust",
        "Dealer_self": "dealer_self",
        "Dealer_Hedging": "dealer_hedge",
        "total": "inst_total",
    }
    piv = piv.rename(columns=rename)
    for c in rename.values():
        if c not in piv.columns:
            piv[c] = 0.0
    piv["dealer"] = piv["dealer_self"] + piv["dealer_hedge"]
    return piv[["foreign", "trust", "dealer_self", "dealer_hedge", "dealer", "inst_total"]].reset_index()


def fetch_futures_oi(start: date, end: date) -> pd.DataFrame:
    """TX futures institutional net OI (口), year-by-year."""
    frames = []
    for yr in range(start.year, end.year + 1):
        s = max(start, date(yr, 1, 1))
        e = min(end, date(yr, 12, 31))
        raw = fetch_finmind("TaiwanFuturesInstitutionalInvestors", "TX", s, e, timeout=120)
        print(f"  futures {yr}: {len(raw)} rows")
        for r in raw:
            net = (r.get("long_open_interest_balance_volume") or 0) - (
                r.get("short_open_interest_balance_volume") or 0
            )
            frames.append(
                {
                    "date": str(r["date"])[:10],
                    "inst": r.get("institutional_investors"),
                    "net_oi": net,
                }
            )
    df = pd.DataFrame(frames)
    piv = df.pivot_table(index="date", columns="inst", values="net_oi", aggfunc="sum")
    rename = {"外資": "fut_foreign_oi", "投信": "fut_trust_oi", "自營商": "fut_dealer_oi"}
    piv = piv.rename(columns=rename)
    keep = [c for c in rename.values() if c in piv.columns]
    return piv[keep].reset_index()


def fetch_margin(start: date, end: date) -> pd.DataFrame:
    """Market-wide 融資餘額(張)/融券餘額(張) via TaiwanStockTotalMarginPurchaseShortSale."""
    frames = []
    for yr in range(start.year, end.year + 1):
        s = max(start, date(yr, 1, 1))
        e = min(end, date(yr, 12, 31))
        raw = fetch_finmind_dataset(
            "TaiwanStockTotalMarginPurchaseShortSale", start=s, end=e, timeout=120
        )
        print(f"  margin {yr}: {len(raw)} rows")
        for r in raw:
            nm = r.get("name")
            if nm in ("MarginPurchase", "ShortSale"):
                frames.append(
                    {"date": str(r["date"])[:10], "name": nm, "bal": r.get("TodayBalance")}
                )
    df = pd.DataFrame(frames)
    piv = df.pivot_table(index="date", columns="name", values="bal", aggfunc="last")
    piv = piv.rename(columns={"MarginPurchase": "margin_bal", "ShortSale": "short_bal"})
    return piv.reset_index()


def load_taiex(db: Path, start: date, end: date) -> pd.DataFrame:
    """Continuous TAIEX daily OHLC via FinMind TaiwanStockPrice/TAIEX (DB IX0001 sync is stale)."""
    frames = []
    for yr in range(start.year, end.year + 1):
        s = max(start, date(yr, 1, 1))
        e = min(end, date(yr, 12, 31))
        raw = fetch_finmind("TaiwanStockPrice", "TAIEX", s, e, timeout=120)
        print(f"  taiex {yr}: {len(raw)} rows")
        for r in raw:
            frames.append(
                {
                    "date": str(r["date"])[:10],
                    "ix_open": r.get("open"),
                    "ix_high": r.get("max"),
                    "ix_low": r.get("min"),
                    "ix_close": r.get("close"),
                    "ix_money": r.get("Trading_money"),
                }
            )
    return pd.DataFrame(frames).sort_values("date").reset_index(drop=True)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print("Fetching market institutional totals...")
    totals = fetch_market_totals(START, END)
    print("Fetching TX futures OI...")
    fut = fetch_futures_oi(START, END)
    print("Fetching margin (融資融券)...")
    margin = fetch_margin(START, END)
    print("Loading TAIEX...")
    ix = load_taiex(DB_PATH, START, END)

    panel = (
        ix.merge(totals, on="date", how="left")
        .merge(fut, on="date", how="left")
        .merge(margin, on="date", how="left")
    )
    panel = panel.sort_values("date").reset_index(drop=True)

    # day-over-day futures OI change (加空/回補)
    for c in ["fut_foreign_oi", "fut_trust_oi", "fut_dealer_oi"]:
        if c in panel.columns:
            panel[c.replace("_oi", "_doi")] = panel[c].diff()

    panel.to_csv(OUT_DIR / "panel.csv", index=False)  # canonical (no pyarrow dep)
    try:
        panel.to_parquet(OUT_DIR / "panel.parquet", index=False)
    except Exception as e:  # pyarrow/fastparquet not installed (e.g. mini) -> CSV is enough
        print(f"  (parquet skipped: {type(e).__name__})")
    print(f"\nPanel: {len(panel)} rows  {panel['date'].min()}..{panel['date'].max()}")
    print("cols:", list(panel.columns))
    miss = panel[["foreign", "fut_foreign_oi", "ix_close"]].isna().sum()
    print("null counts (foreign/fut_foreign_oi/ix_close):", dict(miss))
    print(panel.tail(3).to_string())


if __name__ == "__main__":
    main()
