#!/usr/bin/env python
"""Fetch MARKET-LEVEL short_daytrade signals by aggregating a fixed large-cap
universe (~50, 0050-style) from FinMind per-stock datasets, per-date summed.

Signals built (all market-aggregate over the universe):
  - daytrade_ratio = sum(daytrade sell amount) / sum(trading_money)   [當沖比重]
  - sbl_util       = sum(SBLShortSalesCurrentDayBalance) / sum(SBLShortSalesQuota)  [借券賣出使用率]
  - sbl_bal        = sum(SBLShortSalesCurrentDayBalance)              [借券賣出餘額]
  - margin_util    = sum(MarginPurchaseTodayBalance) / sum(MarginPurchaseLimit)     [融資使用率]

Honest coverage: this is a top-~50 large-cap proxy, NOT the whole market.
Written to data/research/dashboard/short_daytrade_mkt.parquet
"""
import os, time, sys, io
import requests
import pandas as pd

ROOT = "/Users/jackm4/Documents/ETF/股票研究"
OUT = f"{ROOT}/data/research/dashboard/short_daytrade_mkt.parquet"
BASE = "https://api.finmindtrade.com/api/v4/data"
START = "2021-01-01"
END = "2026-07-29"

def get_token():
    for line in open(f"{ROOT}/.env"):
        if line.startswith("FINMIND_TOKEN"):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError("no token")

TOK = get_token()

# fixed large-cap universe (0050-style), where daytrade/SBL activity concentrates
UNIV = ["2330","2317","2454","2308","2382","2412","2891","2881","2882","2303",
        "3711","2886","2884","1216","2357","2892","2885","3034","2890","2327",
        "2345","5880","2880","3231","2379","2603","2887","1303","1301","2883",
        "4938","2002","1101","2207","2301","3008","2408","2409","2395","3037",
        "2474","5871","2888","9910","2912","1326","2801","2809","6669","3661"]

def fetch(dataset, sid):
    for attempt in range(4):
        try:
            r = requests.get(BASE, params={"dataset": dataset, "data_id": sid,
                             "start_date": START, "end_date": END, "token": TOK}, timeout=40)
            j = r.json()
            if j.get("status") == 200:
                return pd.DataFrame(j.get("data", []))
            if "402" in str(j.get("status")) or "quota" in str(j.get("msg", "")).lower():
                print(f"  QUOTA hit at {sid} {dataset}: {j.get('msg')}"); return None
            print(f"  {sid} {dataset} status={j.get('status')} msg={j.get('msg')}")
            return pd.DataFrame()
        except Exception as e:
            print(f"  retry {sid} {dataset}: {e}"); time.sleep(2)
    return pd.DataFrame()

frames = {"day": [], "sbl": [], "mar": [], "px": []}
specs = [("day", "TaiwanStockDayTrading"), ("sbl", "TaiwanDailyShortSaleBalances"),
         ("mar", "TaiwanStockMarginPurchaseShortSale"), ("px", "TaiwanStockPrice")]

quota_hit = False
for i, sid in enumerate(UNIV):
    if quota_hit: break
    for key, ds in specs:
        df = fetch(ds, sid)
        if df is None:
            quota_hit = True; break
        if df is not None and len(df):
            frames[key].append(df)
        time.sleep(0.25)
    if (i + 1) % 10 == 0:
        print(f"  ...{i+1}/{len(UNIV)} stocks fetched")

def cat(key):
    return pd.concat(frames[key], ignore_index=True) if frames[key] else pd.DataFrame()

day = cat("day"); sbl = cat("sbl"); mar = cat("mar"); px = cat("px")
print("rows:", {k: len(cat(k)) for k in frames})

# --- aggregate per date ---
out = pd.DataFrame()

if len(px):
    px["date"] = pd.to_datetime(px["date"])
    g_money = px.groupby("date")["Trading_money"].sum().rename("univ_money")
    out = g_money.to_frame()

if len(day):
    day["date"] = pd.to_datetime(day["date"])
    # SellAmount ~ daytrade turnover (sell leg); use it as daytrade value
    g_dt = day.groupby("date")["SellAmount"].sum().rename("dt_val")
    out = out.join(g_dt, how="outer")

if len(sbl):
    sbl["date"] = pd.to_datetime(sbl["date"])
    g_sbl_bal = sbl.groupby("date")["SBLShortSalesCurrentDayBalance"].sum().rename("sbl_bal")
    g_sbl_q = sbl.groupby("date")["SBLShortSalesQuota"].sum().rename("sbl_quota")
    out = out.join(g_sbl_bal, how="outer").join(g_sbl_q, how="outer")

if len(mar):
    mar["date"] = pd.to_datetime(mar["date"])
    g_mb = mar.groupby("date")["MarginPurchaseTodayBalance"].sum().rename("margin_bal")
    g_ml = mar.groupby("date")["MarginPurchaseLimit"].sum().rename("margin_limit")
    out = out.join(g_mb, how="outer").join(g_ml, how="outer")

out = out.sort_index().reset_index()
# derived ratios
if "dt_val" in out and "univ_money" in out:
    out["daytrade_ratio"] = out["dt_val"] / out["univ_money"]
if "sbl_bal" in out and "sbl_quota" in out:
    out["sbl_util"] = out["sbl_bal"] / out["sbl_quota"]
if "margin_bal" in out and "margin_limit" in out:
    out["margin_util"] = out["margin_bal"] / out["margin_limit"]

out.to_parquet(OUT, index=False)
print("QUOTA_HIT" if quota_hit else "COMPLETE")
print("saved", OUT, "shape", out.shape, "range", out["date"].min(), out["date"].max())
print(out[[c for c in ["date","daytrade_ratio","sbl_util","sbl_bal","margin_util"] if c in out]].tail(3).to_string())
print("non-null:", out.notna().sum().to_dict())
