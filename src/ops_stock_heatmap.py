"""Payload builder for the ops-console ``stock_heatmap`` kind — individual-stock
Weinstein 30W stage heatmap + RS / 綜合順勢 lights for the copy-trade watchlist.

Mirrors scripts/research/chip_macro/stock_tracker.py but emits a compact JSON payload
(daily stage cells as digit-strings) for the haoshi-quant-ops frontend to render.
Framing: trend/risk filter, NOT stock-picking alpha (research showed 分點 follow-edge
is unreliable). Research/observe only — not an Order signal.
"""
from __future__ import annotations

import csv
import sqlite3
from pathlib import Path
from typing import Any

from stock_db import PROJECT_ROOT

ROOT = PROJECT_ROOT
CHIP_MACRO_HISTORY = ROOT / "reports/research/chip-macro/signal_history.csv"
PANEL_PARQUET = ROOT / "data/research/chip_macro/panel.parquet"
PANEL_CSV = ROOT / "data/research/chip_macro/panel.csv"
WATCH = ["3189", "6147", "2492", "3653", "6505"]
NAMES = {"3189": "景碩", "6147": "頎邦", "2492": "華新科", "3653": "健策", "6505": "台塑化"}
STAGE_LABEL = {1: "S1 打底", 2: "S2 多頭", 3: "S3 走緩", 4: "S4 下跌"}
HEAT_DAYS = 252


def _candidate_dbs() -> list[Path]:
    from stock_db import DEFAULT_DB_PATH
    return [Path(DEFAULT_DB_PATH), ROOT / "data/replica/stocks.db"]


def _load_prices(sid: str, pd):
    """Pick the candidate DB with the FRESHEST last trade_date for this symbol
    (Book's data/stocks.db can lag data/replica; the mini's default is fresh)."""
    best = None
    best_last = ""
    for dbp in _candidate_dbs():
        if not dbp.is_file():
            continue
        try:
            con = sqlite3.connect(f"file:{dbp}?mode=ro", uri=True)
            df = pd.read_sql_query(
                "select trade_date as date, open, high, low, close from stock_daily_bars "
                "where stock_id=? and close>0 order by trade_date", con, params=(sid,))
            con.close()
        except Exception:
            continue
        if len(df) > 200:
            last = str(df["date"].iloc[-1])
            if last > best_last:
                best_last, best = last, df
    return best


def build_stock_heatmap_payload() -> dict[str, Any]:
    try:
        import pandas as pd
        import sys as _sys
        _sys.path.insert(0, str(ROOT / "src"))
        from stage_analysis import stage_series_daily
    except Exception as e:  # noqa: BLE001
        return {"schema": "ops-stock-heatmap-v1", "title": "個股趨勢熱力圖",
                "present": False, "note": f"依賴缺失：{type(e).__name__}"}

    # market regime from signal_history.csv (produced by daily_tracker.py)
    mkt = {"stage": None, "armed": False, "z60": None}
    if CHIP_MACRO_HISTORY.is_file():
        hrows = list(csv.DictReader(CHIP_MACRO_HISTORY.read_text(encoding="utf-8").splitlines()))
        if hrows:
            last = hrows[-1]
            try:
                z = round(float(last.get("z60")), 2)
            except (TypeError, ValueError):
                z = None
            mkt = {"stage": last.get("stage"),
                   "armed": str(last.get("armed", "")).strip().lower() in ("true", "1"),
                   "z60": z}

    tx = None
    for loader, path in ((pd.read_parquet, PANEL_PARQUET), (pd.read_csv, PANEL_CSV)):
        if path.is_file():
            try:
                p = loader(path)
                tx = pd.Series(p["ix_close"].values, index=p["date"].astype(str))
                break
            except Exception:
                continue

    def inten(sl, ex) -> int:
        t = 0.0
        if pd.notna(sl):
            t = max(t, max(0.0, float(sl)) / 25.0)
        if pd.notna(ex):
            t = max(t, max(0.0, float(ex)) / 20.0)
        return min(9, int(t * 9))

    rows_out, heat, dates, asof = [], [], None, None
    for sid in WATCH:
        df = _load_prices(sid, pd)
        if df is None:
            continue
        df = df.dropna(subset=["close"]).drop_duplicates("date", keep="last")
        for c in ("open", "high", "low"):
            df[c] = df[c].fillna(df["close"])
        if len(df) < 180:
            continue
        sf = stage_series_daily(df, ma_weeks=30, confirm_days=2).tail(HEAT_DAYS)
        cur = sf.iloc[-1]
        stg = int(cur["stage"])
        slope = None if pd.isna(cur["ma_slope_pct"]) else round(float(cur["ma_slope_pct"]), 1)
        ext = None if pd.isna(cur["extension_pct"]) else round(float(cur["extension_pct"]), 1)
        asof = str(sf["date"].iloc[-1])[:10]
        rs_up = None
        if tx is not None:
            rsm = df.set_index("date")["close"].to_frame("c").join(tx.rename("tx")).dropna()
            if len(rsm) >= 50:
                rs = rsm["c"] / rsm["tx"]
                rs_up = bool(rs.iloc[-1] > rs.rolling(50, min_periods=50).mean().iloc[-1])
        if stg == 2 and rs_up and mkt["armed"]:
            comb = "順勢"
        elif stg == 2 and rs_up:
            comb = "個股順勢·大盤risk-off"
        elif stg in (1, 4):
            comb = "逆勢"
        else:
            comb = "中性"
        rows_out.append({"sid": sid, "name": NAMES.get(sid, sid),
                         "price": round(float(df["close"].iloc[-1]), 1), "stage": stg,
                         "stage_label": STAGE_LABEL.get(stg, "—"),
                         "slope_pct": slope, "extension_pct": ext,
                         "rs_up": rs_up, "comb": comb})
        stages_str = "".join(str(int(s)) for s in sf["stage"])
        intens_str = "".join(str(inten(r.ma_slope_pct, r.extension_pct)) for r in sf.itertuples(index=False))
        if dates is None:
            dates = [str(d)[:10] for d in sf["date"]]
        heat.append({"sid": sid, "name": NAMES.get(sid, sid), "stages": stages_str,
                     "intens": intens_str, "pinned": sid in ("3189", "6147")})

    if not rows_out:
        return {"schema": "ops-stock-heatmap-v1", "title": "個股趨勢熱力圖",
                "present": False, "note": "無個股價格資料（stock_daily_bars 缺該標的）"}
    return {
        "schema": "ops-stock-heatmap-v1",
        "title": "個股趨勢熱力圖 · 30W",
        "present": True,
        "asof": asof,
        "market": mkt,
        "rows": rows_out,
        "dates": dates,
        "heat": heat,
        "note_zh": ("Weinstein 30W 階段 + RS(相對大盤) + 綜合順勢燈，由大盤 regime 把關。"
                    "綠=S2多頭(越亮越強)/藍灰=S1/黃=S3/紅=S4。趨勢/風控濾網，非選股 alpha；"
                    "研究證分點跟單 edge 不可靠。研究觀察用，非個股買賣依據。"),
    }
