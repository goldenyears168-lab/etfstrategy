"""Fetch REAL market-level daily series for short_daytrade dimension.

Datasets (all support date-only all-stocks fetch -> cheap market aggregation):
  * TaiwanDailyShortSaleBalances  -> directional SBL 機構做空餘額 (correct caliber)
  * TaiwanStockDayTrading         -> 當沖成交值 (market ratio via panel turnover)
  * TaiwanStockSecuritiesLending  -> 借券成交量 (direction-neutral, honesty proxy)
  * TaiwanStockTotalMarginPurchaseShortSale -> 市場融資/融券餘額 (util proxy, no limit)

Aggregates to a single market-level daily series; saves parquet for reuse.
Quota-aware: one call per year-chunk per dataset, sleep 0.4s between.
"""
from __future__ import annotations
import re, time, sys
from pathlib import Path
import requests
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "data" / "research" / "dashboard" / "short_daytrade_data.parquet"
PANEL = ROOT / "data" / "research" / "chip_macro" / "panel.parquet"
OUT.parent.mkdir(parents=True, exist_ok=True)
BASE = "https://api.finmindtrade.com/api/v4/data"
TOK = re.search(r"FINMIND_TOKEN\s*=\s*([^\s#]+)", (ROOT/".env").read_text()).group(1)
H = {"Authorization": f"Bearer {TOK}"}
START, END = "2018-06-01", "2026-07-29"
# per-stock datasets cap to 1 day w/o data_id -> must loop trading days.
# Quota-limit those to a representative recent window (task guidance).
DAILY_START = "2023-07-01"


def fetch(ds: str, start: str, end: str, data_id: str | None = None) -> pd.DataFrame:
    p = {"dataset": ds, "start_date": start, "end_date": end}
    if data_id:
        p["data_id"] = data_id
    r = requests.get(BASE, params=p, headers=H, timeout=120)
    r.raise_for_status()
    j = r.json()
    if j.get("msg") != "success":
        print(f"  ! {ds} {start}: {j.get('msg')}")
    return pd.DataFrame(j.get("data", []))


def trading_days():
    d = pd.read_parquet(PANEL)["date"].astype(str)
    return sorted(d[(d >= DAILY_START) & (d <= END)].tolist())


def agg_daily_loop(dataset: str, agg_fn, label: str):
    """Loop per trading day (per-stock datasets cap to 1 day w/o data_id)."""
    rows = []
    days = trading_days()
    print(f"  {label}: looping {len(days)} trading days {days[0]}..{days[-1]}")
    for i, d in enumerate(days):
        try:
            df = fetch(dataset, d, d)
        except Exception as e:
            print(f"  ! {label} {d}: {e}")
            time.sleep(1.0)
            continue
        if len(df):
            rows.append(agg_fn(df, d))
        if i % 60 == 0:
            print(f"    {label} {d} ({i}/{len(days)})")
        time.sleep(0.3)
    return pd.DataFrame(rows)


def agg_sbl():
    def f(df, d):
        return {
            "date": d,
            "sbl_short_bal": pd.to_numeric(df["SBLShortSalesCurrentDayBalance"], errors="coerce").sum(),
            "sbl_quota": pd.to_numeric(df["SBLShortSalesQuota"], errors="coerce").sum(),
            "margin_short_bal": pd.to_numeric(df["MarginShortSalesCurrentDayBalance"], errors="coerce").sum(),
        }
    return agg_daily_loop("TaiwanDailyShortSaleBalances", f, "SBL")


def agg_daytrade():
    def f(df, d):
        b = pd.to_numeric(df["BuyAmount"], errors="coerce").fillna(0)
        s = pd.to_numeric(df["SellAmount"], errors="coerce").fillna(0)
        return {"date": d, "daytrade_val": float((b + s).sum())}
    return agg_daily_loop("TaiwanStockDayTrading", f, "DAY")


def agg_total_margin():
    df = fetch("TaiwanStockTotalMarginPurchaseShortSale", START, END)
    if not len(df):
        return pd.DataFrame()
    df["TodayBalance"] = pd.to_numeric(df["TodayBalance"], errors="coerce")
    mp = df[df["name"] == "MarginPurchase"][["date", "TodayBalance"]].rename(
        columns={"TodayBalance": "tot_margin_bal"})
    ss = df[df["name"] == "ShortSale"][["date", "TodayBalance"]].rename(
        columns={"TodayBalance": "tot_short_bal"})
    g = mp.merge(ss, on="date", how="outer")
    print(f"  TOTMARGIN: {len(g)} days")
    return g


def main():
    print("Fetching market-level series...")
    tot = agg_total_margin()          # full 8y, 1 call
    sbl = agg_sbl()                   # daily loop ~3y
    day = agg_daytrade()              # daily loop ~3y
    out = None
    for f in (sbl, day, tot):
        if len(f):
            out = f if out is None else out.merge(f, on="date", how="outer")
    out = out.sort_values("date").reset_index(drop=True)
    out.to_parquet(OUT, index=False)
    print(f"\nSaved {len(out)} days -> {OUT}")
    print("cols", list(out.columns))
    print("range", out['date'].min(), out['date'].max())
    print(out.tail(3).to_string())


if __name__ == "__main__":
    main()
