"""Payload builder for the ops-console ``holdings_stability`` kind.

Grades EACH current holding 🟢穩定 / 🟡中性偏弱 / 🔴不穩定 using the project's
validated stack, recomputed each Live-TA poll (盤中) + EOD:
  - Weinstein 30W stage (stage_series_daily) + RS vs TAIEX + realized vol + distance to MA150/MA50  (daily, local stock_daily_bars)
  - intraday last_print & 相對昨收%  (from ops_live_ta, updated by the Live-TA poll)
Framing: TREND-STABILITY classification, NOT buy/sell advice.

Sources: ops_holdings (holdings universe), ops_live_ta (intraday), local stock_daily_bars,
signal_history.csv (大盤 regime). Research/observe only.
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
STAGE_LABEL = {1: "S1 打底", 2: "S2 多頭", 3: "S3 走緩", 4: "S4 下跌"}


def _candidate_dbs() -> list[Path]:
    from stock_db import DEFAULT_DB_PATH
    return [Path(DEFAULT_DB_PATH), ROOT / "data/replica/stocks.db"]


def _load_prices(sid: str, pd):
    best, best_last = None, ""
    for dbp in _candidate_dbs():
        if not dbp.is_file():
            continue
        try:
            con = sqlite3.connect(f"file:{dbp}?mode=ro", uri=True)
            df = pd.read_sql_query(
                "select trade_date as date, close, source from stock_daily_bars "
                "where stock_id=? and close>0 order by trade_date, "
                "case source when 'finmind' then 0 when 'yfinance' then 1 else 2 end",
                con, params=(sid,))
            # finmind（原始價）/ yfinance（還原價）同日雙列語意不同，只留 finmind 那列
            df = df.drop_duplicates(subset=["date"], keep="first").drop(columns=["source"])
            con.close()
        except Exception:
            continue
        if len(df) > 180:
            last = str(df["date"].iloc[-1])
            if last > best_last:
                best_last, best = last, df
    return best


def _holdings_map(payload: dict) -> dict[str, int]:
    """stock_id -> qty from ops_holdings payload (by_symbol first, else holdings rows)."""
    out: dict[str, int] = {}
    by = payload.get("by_symbol")
    if isinstance(by, dict) and by:
        for k, v in by.items():
            try:
                out[str(k)] = int(v)
            except (TypeError, ValueError):
                out[str(k)] = 0
    for r in payload.get("holdings") or []:
        sid = str(r.get("stock_no") or r.get("stock_id") or "").strip()
        if not sid:
            continue
        odd = r.get("odd") or {}
        qty = int(r.get("lastday_qty") or 0) + int(r.get("today_qty") or 0) \
            + int(odd.get("lastday_qty") or 0) + int(odd.get("today_qty") or 0)
        out.setdefault(sid, qty)
    return out


def build_holdings_stability_payload() -> dict[str, Any]:
    try:
        import numpy as np
        import pandas as pd
        import sys as _sys
        _sys.path.insert(0, str(ROOT / "src"))
        from stage_analysis import stage_series_daily
        from ops_console_sync import fetch_latest_ops_holdings_payload, fetch_live_ta_rows
    except Exception as e:  # noqa: BLE001
        return {"schema": "ops-holdings-stability-v1", "title": "持倉穩定度分級",
                "present": False, "note": f"依賴缺失：{type(e).__name__}"}

    try:
        hp = fetch_latest_ops_holdings_payload()
    except Exception as e:  # noqa: BLE001
        return {"schema": "ops-holdings-stability-v1", "title": "持倉穩定度分級",
                "present": False, "note": f"讀 ops_holdings 失敗：{e}"}
    if not hp:
        return {"schema": "ops-holdings-stability-v1", "title": "持倉穩定度分級",
                "present": False, "note": "尚無 ops_holdings 快照"}
    qty_map = _holdings_map(hp)

    live = {}
    try:
        for r in fetch_live_ta_rows():
            sid = str(r.get("stock_id"))
            live[sid] = r
    except Exception:
        live = {}

    # market regime
    mkt = {"stage": None, "armed": False, "z60": None}
    if CHIP_MACRO_HISTORY.is_file():
        rows = list(csv.DictReader(CHIP_MACRO_HISTORY.read_text(encoding="utf-8").splitlines()))
        if rows:
            last = rows[-1]
            try:
                z = round(float(last.get("z60")), 2)
            except (TypeError, ValueError):
                z = None
            mkt = {"stage": last.get("stage"),
                   "armed": str(last.get("armed", "")).strip().lower() in ("true", "1"), "z60": z}

    tx = None
    for loader, path in ((pd.read_parquet, PANEL_PARQUET), (pd.read_csv, PANEL_CSV)):
        if path.is_file():
            try:
                p = loader(path)
                tx = pd.Series(p["ix_close"].values, index=p["date"].astype(str))
                break
            except Exception:
                continue

    rows_out = []
    for sid, qty in qty_map.items():
        if sid in ("IX0001",):
            continue
        df = _load_prices(sid, pd)
        if df is None:
            continue
        df = df.dropna(subset=["close"]).drop_duplicates("date", keep="last")
        if len(df) < 180:
            continue
        sf = stage_series_daily(df.assign(open=df["close"], high=df["close"], low=df["close"]),
                                ma_weeks=30, confirm_days=2)
        stg = int(sf.iloc[-1]["stage"])
        prev_close = float(df["close"].iloc[-1])
        ma150 = float(df["close"].rolling(150, min_periods=150).mean().iloc[-1])
        ma50 = float(df["close"].rolling(50, min_periods=50).mean().iloc[-1])
        rv10 = float(df["close"].pct_change().tail(10).std() * np.sqrt(252) * 100)
        rs_up = None
        if tx is not None:
            rsm = df.set_index("date")["close"].to_frame("c").join(tx.rename("t")).dropna()
            if len(rsm) >= 50:
                rs = rsm["c"] / rsm["t"]
                rs_up = bool(rs.iloc[-1] > rs.rolling(50, min_periods=50).mean().iloc[-1])
        # intraday
        lr = live.get(sid) or {}
        last_print = lr.get("last_print")
        try:
            last_print = float(last_print) if last_print is not None else None
        except (TypeError, ValueError):
            last_print = None
        px = last_print if last_print else prev_close
        # prefer Live-TA's broker-accurate 相對昨收 (local daily prev_close can be a day stale)
        chg = (px / prev_close - 1) * 100 if prev_close else 0.0
        note = str(lr.get("note_zh") or "")
        import re as _re
        mm = _re.search(r"相對昨收\s*([+-]?\d+\.?\d*)\s*%", note)
        if mm:
            try:
                chg = float(mm.group(1))
            except ValueError:
                pass
        d150 = (px / ma150 - 1) * 100
        d50 = (px / ma50 - 1) * 100
        # score
        sc = {2: 2, 3: 0, 1: -1, 4: -2}[stg]
        sc += 1 if rs_up else -1
        sc += 1 if d150 > 0 else -2
        sc += 0.5 if d50 > 0 else -0.5
        sc += -1 if rv10 > 45 else (0 if rv10 > 30 else 0.5)
        sc += -1.5 if chg <= -5 else (-1 if chg <= -3 else (-0.3 if chg < 0 else 0.5))
        grade = "穩定" if sc >= 2 else ("中性偏弱" if sc >= -0.5 else "不穩定")
        tone = "good" if grade == "穩定" else ("warn" if grade == "中性偏弱" else "bad")
        rows_out.append({
            "sid": sid, "name": (lr.get("stock_name") or sid), "qty": qty, "held": qty > 0,
            "stage": stg, "stage_label": STAGE_LABEL.get(stg, "—"),
            "rs_up": rs_up, "d150": round(d150, 1), "d50": round(d50, 1),
            "rv10": round(rv10, 0), "last": round(px, 2), "chg": round(chg, 2),
            "score": round(sc, 1), "grade": grade, "tone": tone,
            "phase": lr.get("phase"),
        })
    if not rows_out:
        return {"schema": "ops-holdings-stability-v1", "title": "持倉穩定度分級",
                "present": False, "note": "持倉無足夠日線可評分"}
    rows_out.sort(key=lambda r: (-int(r["held"]), -r["score"]))
    counts = {"穩定": 0, "中性偏弱": 0, "不穩定": 0}
    for r in rows_out:
        if r["held"]:
            counts[r["grade"]] += 1
    return {
        "schema": "ops-holdings-stability-v1",
        "title": "持倉穩定度分級",
        "present": True,
        "market": mkt,
        "counts_held": counts,
        "rows": rows_out,
        "note_zh": ("每檔用 Weinstein 30W 階段 + RS(相對大盤) + 距年線/季線 + 10日波動 + Live TA 盤中相對昨收 綜合評分。"
                    "🟢穩定/🟡中性偏弱/🔴不穩定。盤中每輪 Live TA poll 重算。"
                    "波動於崩盤週偏大且部分含資料調整雜訊，分級以階段+RS+距均線+盤中為主。趨勢穩定度分級，非買賣建議。"),
    }
