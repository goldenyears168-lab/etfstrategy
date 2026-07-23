#!/usr/bin/env python3
"""陳族元 style overnight study · Research-only.

Waves:
  A) Dual archetypes on 富邦新店 (9661) signals
     - shuanghong: trend-confirm add (above SMA20/60, ret60>0, abs∩share)
     - nanya: dip-theme accumulate (below SMA60, ret60<-15%, then hold longer)
     - baseline: plain abs∩share Top1
  B) Fair hold sweep H7/H10/H14/H20 + exclude weights
  C) Walk-forward frozen (train 60 / OOS fold)
  D) Secondary seats 嘉義/南屯 co-signal lift
  E) Macro overlay ablation (Chen SOP filter)

Writes under reports/research/branch-footprint-screen/chen_overnight_*/
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from stock_db.util import DEFAULT_DB_PATH  # noqa: E402

OUT = ROOT / "reports/research/branch-footprint-screen/chen_overnight_20260720"
OUT.mkdir(parents=True, exist_ok=True)
BRANCH = "9661"
SECONDARIES = [("9692", "富邦嘉義"), ("9666", "富邦南屯")]
WEIGHT = {"2330", "2317", "2454", "2308", "2881", "2412", "3045", "2303", "2382", "2882", "2891", "2886"}
COST = 0.003
BETA = 1.15
# Local branch tape for 9661 starts ~2024-07 (earlier years empty).
D0, D1 = "2024-07-01", "2026-07-16"
SIG_CUT = "2026-07-03"
SOURCE = "finmind"


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def connect_ro() -> sqlite3.Connection:
    """Read-only URI — skip schema migrate; tolerate concurrent funnel readers."""
    uri = f"file:{DEFAULT_DB_PATH.resolve()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=120.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA cache_size=-64000")  # ~64MB
    return conn


def year_windows(d0: str, d1: str) -> list[tuple[str, str]]:
    y0, y1 = int(d0[:4]), int(d1[:4])
    out: list[tuple[str, str]] = []
    for y in range(y0, y1 + 1):
        a = max(d0, f"{y}-01-01")
        b = min(d1, f"{y}-12-31")
        if a <= b:
            out.append((a, b))
    return out


def load_branch_top1(conn: sqlite3.Connection, branch: str) -> pd.DataFrame:
    """Year-chunked Top1 by buy_amt — avoids loading full multi-year tape+prices."""
    parts: list[pd.DataFrame] = []
    for a, b in year_windows(D0, D1):
        log(f"  top1 chunk {a}..{b}")
        buys = pd.read_sql_query(
            """
            SELECT trade_date AS d, stock_id AS sid, buy
            FROM stock_broker_branch_daily
            WHERE securities_trader_id=? AND source=?
              AND trade_date>=? AND trade_date<=?
              AND stock_id != '__EMPTY__'
              AND length(stock_id)=4
              AND stock_id GLOB '[0-9][0-9][0-9][0-9]'
              AND buy>0
            """,
            conn,
            params=[branch, SOURCE, a, b],
        )
        if buys.empty:
            continue
        buys["d"] = buys["d"].astype(str)
        buys["sid"] = buys["sid"].astype(str)
        sids = sorted(buys["sid"].unique())
        ph = ",".join("?" * len(sids))
        px = pd.read_sql_query(
            f"""
            SELECT stock_id AS sid, trade_date AS d, close
            FROM stock_daily_bars
            WHERE source=? AND stock_id IN ({ph})
              AND trade_date>=? AND trade_date<=?
              AND close>0
            """,
            conn,
            params=[SOURCE, *sids, a, b],
        )
        if px.empty:
            del buys
            continue
        px["d"] = px["d"].astype(str)
        px["sid"] = px["sid"].astype(str)
        raw = buys.merge(px, on=["sid", "d"], how="inner")
        del buys, px
        raw["buy_amt"] = raw["buy"] * raw["close"]
        day_buy = raw.groupby("d")["buy_amt"].transform("sum")
        raw["buy_share"] = raw["buy_amt"] / day_buy.replace(0, np.nan)
        raw["rk"] = raw.groupby("d")["buy_amt"].rank(method="first", ascending=False)
        parts.append(raw.loc[raw["rk"] == 1, ["d", "sid", "buy_amt", "buy_share", "close"]].copy())
        del raw
        log(f"    kept top1 days={len(parts[-1])}")
    if not parts:
        return pd.DataFrame(columns=["d", "sid", "buy_amt", "buy_share", "close"])
    return pd.concat(parts, ignore_index=True)


def load_sec_events_for_pairs(
    conn: sqlite3.Connection, bid: str, pairs: set[tuple[str, str]]
) -> set[tuple[str, str]]:
    """Only check co-presence on given (date, stock) pairs — avoids full secondary tape."""
    if not pairs:
        return set()
    by_day: dict[str, set[str]] = {}
    for d, sid in pairs:
        by_day.setdefault(d, set()).add(sid)
    hit: set[tuple[str, str]] = set()
    # Process by year to keep SQL params small
    days = sorted(by_day)
    for a, b in year_windows(D0, D1):
        chunk_days = [d for d in days if a <= d <= b]
        if not chunk_days:
            continue
        want_sids = sorted({s for d in chunk_days for s in by_day[d]})
        ph_d = ",".join("?" * len(chunk_days))
        ph_s = ",".join("?" * len(want_sids))
        df = pd.read_sql_query(
            f"""
            SELECT trade_date AS d, stock_id AS sid, buy
            FROM stock_broker_branch_daily
            WHERE securities_trader_id=? AND source=?
              AND trade_date IN ({ph_d})
              AND stock_id IN ({ph_s})
              AND buy>0
            """,
            conn,
            params=[bid, SOURCE, *chunk_days, *want_sids],
        )
        if df.empty:
            continue
        df["d"] = df["d"].astype(str)
        df["sid"] = df["sid"].astype(str)
        # significant: buy shares * close later; use buy shares proxy then price
        px = pd.read_sql_query(
            f"""
            SELECT stock_id AS sid, trade_date AS d, close
            FROM stock_daily_bars
            WHERE source=? AND stock_id IN ({ph_s})
              AND trade_date IN ({ph_d}) AND close>0
            """,
            conn,
            params=[SOURCE, *want_sids, *chunk_days],
        )
        if px.empty:
            continue
        px["d"] = px["d"].astype(str)
        px["sid"] = px["sid"].astype(str)
        m = df.merge(px, on=["sid", "d"], how="inner")
        m["buy_amt"] = m["buy"] * m["close"]
        for r in m.itertuples():
            if r.buy_amt >= 0.5e8 and r.sid in by_day.get(r.d, set()):
                hit.add((r.d, r.sid))
        del df, px, m
    return hit


def summarize(legs: list[dict], min_n: int = 8) -> dict:
    if len(legs) < min_n:
        return {
            "n": len(legs),
            "ok": False,
            "excess_med": np.nan,
            "excess_mean": np.nan,
            "stock_med": np.nan,
            "win": np.nan,
            "max1": np.nan,
            "slot_n": 0,
            "slot_med": np.nan,
            "slot_dd": np.nan,
        }
    xs = np.array([x["excess_pct"] for x in legs])
    ss = np.array([x["stock_pct"] for x in legs])
    ctr = Counter(x["stock_id"] for x in legs)
    taken = []
    busy = None
    for lg in sorted(legs, key=lambda z: z["entry_date"]):
        if busy is not None and lg["entry_date"] <= busy:
            continue
        taken.append(lg)
        busy = lg["exit_date"]
    txs = np.array([x["excess_pct"] for x in taken]) if taken else np.array([])
    trs = np.array([x["stock_pct"] for x in taken]) / 100 if taken else np.array([])
    if len(trs):
        eq = np.cumprod(1 + trs)
        dd = float((eq / np.maximum.accumulate(eq) - 1).min() * 100)
        slot_med = float(np.median(txs))
    else:
        dd, slot_med = np.nan, np.nan
    return {
        "n": len(legs),
        "ok": True,
        "excess_med": float(np.median(xs)),
        "excess_mean": float(np.mean(xs)),
        "stock_med": float(np.median(ss)),
        "win": float((xs > 0).mean() * 100),
        "max1": max(ctr.values()) / len(legs),
        "slot_n": len(taken),
        "slot_med": slot_med,
        "slot_dd": dd,
    }


def main() -> int:
    t0 = time.time()
    status = {"phase": "init", "at": datetime.now().isoformat(timespec="seconds")}
    (OUT / "status.json").write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")

    conn = connect_ro()
    log("load calendar + benchmark")
    cal = [
        str(r[0])
        for r in conn.execute(
            """
            SELECT DISTINCT trade_date FROM stock_daily_bars
            WHERE source=? AND trade_date>=? AND trade_date<=date(?, '+40 day')
            ORDER BY trade_date
            """,
            (SOURCE, D0, D1),
        )
    ]
    di = {d: i for i, d in enumerate(cal)}
    bench_rows = conn.execute(
        """
        SELECT date, close FROM daily_bars
        WHERE code='IX0001' AND date>=? AND date<=date(?, '+40 day')
        ORDER BY date
        """,
        (D0, D1),
    ).fetchall()
    if not bench_rows:
        bench_rows = conn.execute(
            """
            SELECT trade_date, close FROM stock_daily_bars
            WHERE stock_id='IX0001' AND source=? AND trade_date>=?
            ORDER BY trade_date
            """,
            (SOURCE, D0),
        ).fetchall()
    bench = {str(a): float(b) for a, b in bench_rows if b}

    status = {"phase": "load_top1", "at": datetime.now().isoformat(timespec="seconds")}
    (OUT / "status.json").write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    log("load 新店 Top1 (year-chunked)")
    top1 = load_branch_top1(conn, BRANCH)
    log(f"top1 days={len(top1)}")

    # stock panels for SMA/ret60 — only stocks appearing in top1
    sids = sorted(top1["sid"].unique())
    log(f"load ohlc for {len(sids)} stocks")
    ph = ",".join("?" * len(sids))
    ohlc_df = pd.read_sql_query(
        f"""
        SELECT stock_id AS sid, trade_date AS d, open, close, volume
        FROM stock_daily_bars
        WHERE source=? AND stock_id IN ({ph})
          AND trade_date>=date(?, '-120 day') AND trade_date<=date(?, '+40 day')
          AND open>0 AND close>0
        ORDER BY stock_id, trade_date
        """,
        conn,
        params=[SOURCE, *sids, D0, D1],
    )
    ohlc_df["sid"] = ohlc_df["sid"].astype(str)
    ohlc_df["d"] = ohlc_df["d"].astype(str)
    feat_rows = []
    for sid, g in ohlc_df.groupby("sid"):
        g = g.sort_values("d").copy()
        g["sma20"] = g["close"].rolling(20).mean()
        g["sma60"] = g["close"].rolling(60).mean()
        g["ret60"] = g["close"].pct_change(60)
        g["ret20"] = g["close"].pct_change(20)
        feat_rows.append(g)
    feat = pd.concat(feat_rows, ignore_index=True)
    feat_map = {(r.sid, r.d): r for r in feat.itertuples()}
    ohlc_px = {(r.sid, r.d): (float(r.open), float(r.close)) for r in ohlc_df.itertuples()}
    del feat, feat_rows, ohlc_df

    # secondary branch same-day presence (only on 新店 top1 pairs)
    status = {"phase": "load_secondary", "at": datetime.now().isoformat(timespec="seconds")}
    (OUT / "status.json").write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    log("load secondary seats (top1-pair check only)")
    top1_pairs = set(zip(top1["d"].astype(str), top1["sid"].astype(str)))
    sec_sets: dict[str, set[tuple[str, str]]] = {}
    for bid, _name in SECONDARIES:
        sec_sets[bid] = load_sec_events_for_pairs(conn, bid, top1_pairs)
        log(f"  {bid} co-hits={len(sec_sets[bid])}")

    # try macro via yfinance (optional)
    macro = None
    try:
        import yfinance as yf

        dxy = yf.download("DX-Y.NYB", start="2019-01-01", end="2026-07-17", progress=False, auto_adjust=True)
        tnx = yf.download("^TNX", start="2019-01-01", end="2026-07-17", progress=False, auto_adjust=True)
        if isinstance(dxy.columns, pd.MultiIndex):
            dxy.columns = dxy.columns.get_level_values(0)
        if isinstance(tnx.columns, pd.MultiIndex):
            tnx.columns = tnx.columns.get_level_values(0)
        macro = pd.DataFrame({"dxy": dxy["Close"], "ust10": tnx["Close"]}).dropna(how="all")
        macro.index = pd.to_datetime(macro.index).tz_localize(None)
        log(f"macro n={len(macro)}")
    except Exception as exc:  # noqa: BLE001
        log(f"macro unavailable: {exc}")

    def macro_bull(d: str) -> int | None:
        if macro is None:
            return None
        ts = pd.Timestamp(d)
        m = macro.loc[:ts].tail(65)
        if len(m) < 30:
            return None
        dxy_chg = float(m.dxy.iloc[-1] / m.dxy.iloc[-60] - 1)
        ust = float(m.ust10.iloc[-1])
        ust_chg = float(m.ust10.iloc[-1] - m.ust10.iloc[-60])
        return int(dxy_chg < 0) + int(ust_chg < 0) + int(ust < 3.0)

    def make_leg(sid: str, sig: str, hold: int) -> dict | None:
        i = di.get(sig)
        if i is None:
            return None
        ei, xi = i + 1, i + hold
        if xi >= len(cal):
            return None
        ed, xd = cal[ei], cal[xi]
        er = ohlc_px.get((sid, ed))
        xr = ohlc_px.get((sid, xd))
        be, bx = bench.get(ed), bench.get(xd)
        if not er or not xr or not be or not bx:
            return None
        entry_px, _ = er
        _, exit_px = xr
        if entry_px <= 0 or exit_px <= 0 or be <= 0 or bx <= 0:
            return None
        sr = exit_px / entry_px - 1.0 - COST
        br = bx / be - 1.0
        return {
            "signal_date": sig,
            "entry_date": ed,
            "exit_date": xd,
            "stock_id": sid,
            "stock_pct": sr * 100,
            "excess_pct": (sr - BETA * br) * 100,
        }

    # annotate top1 with features
    ann = []
    for r in top1.itertuples():
        f = feat_map.get((r.sid, r.d))
        if f is None or pd.isna(getattr(f, "sma60", np.nan)):
            continue
        bull = macro_bull(r.d)
        row = {
            "d": r.d,
            "sid": r.sid,
            "buy_amt": float(r.buy_amt),
            "buy_share": float(r.buy_share) if pd.notna(r.buy_share) else np.nan,
            "close": float(f.close),
            "above20": bool(f.close > f.sma20) if pd.notna(f.sma20) else False,
            "above60": bool(f.close > f.sma60) if pd.notna(f.sma60) else False,
            "ret60": float(f.ret60) if pd.notna(f.ret60) else np.nan,
            "ret20": float(f.ret20) if pd.notna(f.ret20) else np.nan,
            "macro_bull": bull,
            "with_jiayi": (r.d, r.sid) in sec_sets.get("9692", set()),
            "with_nantun": (r.d, r.sid) in sec_sets.get("9666", set()),
        }
        ann.append(row)
    adf = pd.DataFrame(ann)
    adf = adf[adf["d"] <= SIG_CUT].copy()
    log(f"annotated signals={len(adf)}")
    adf.to_csv(OUT / "xindian_top1_annotated.csv", index=False)

    def filter_arch(df: pd.DataFrame, arch: str, abs_amt: float, share: float) -> pd.DataFrame:
        m = (df["buy_amt"] >= abs_amt) & (df["buy_share"] >= share) & (~df["sid"].isin(WEIGHT))
        sub = df.loc[m].copy()
        if arch == "baseline":
            return sub
        if arch == "shuanghong":
            # 雙鴻型：趨勢確認
            return sub[sub["above20"] & sub["above60"] & (sub["ret60"] > 0)]
        if arch == "shuanghong_macro":
            return sub[
                sub["above20"]
                & sub["above60"]
                & (sub["ret60"] > 0)
                & (sub["macro_bull"].fillna(0) >= 1)
            ]
        if arch == "nanya":
            # 南亞科型：深跌主題（短線訊號日在弱勢）
            return sub[(~sub["above60"]) & (sub["ret60"] < -0.15)]
        if arch == "nanya_soft":
            return sub[(~sub["above60"]) & (sub["ret60"] < -0.05)]
        if arch == "chase":
            # 新店爆量常見：強趨勢 + 大金額
            return sub[sub["above20"] & sub["above60"] & (sub["ret20"] > 0.05)]
        if arch == "dual_seat":
            return sub[sub["with_jiayi"] | sub["with_nantun"]]
        if arch == "anti_chen_sop":
            # 故意違反公開 SOP：高利率年代可交易？
            return sub[(sub["macro_bull"].fillna(9) == 0) & sub["above60"]]
        return sub

    grids = [
        ("baseline", 2e8, 0.25),
        ("baseline", 2e8, 0.40),
        ("baseline", 5e8, 0.40),
        ("shuanghong", 2e8, 0.25),
        ("shuanghong", 2e8, 0.40),
        ("shuanghong", 5e8, 0.40),
        ("shuanghong_macro", 2e8, 0.40),
        ("nanya", 1e8, 0.15),
        ("nanya", 2e8, 0.25),
        ("nanya_soft", 2e8, 0.25),
        ("chase", 2e8, 0.40),
        ("chase", 5e8, 0.40),
        ("dual_seat", 2e8, 0.25),
        ("anti_chen_sop", 2e8, 0.40),
    ]
    holds = (7, 10, 14, 20)

    status = {"phase": "grid", "at": datetime.now().isoformat(timespec="seconds")}
    (OUT / "status.json").write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")

    rows = []
    all_legs = []
    for arch, a, s in grids:
        sub = filter_arch(adf, arch, a, s)
        for hold in holds:
            legs = []
            for r in sub.itertuples():
                lg = make_leg(r.sid, r.d, hold)
                if lg:
                    lg2 = {
                        **lg,
                        "arch": arch,
                        "abs": a,
                        "share": s,
                        "hold": hold,
                        "buy_amt": r.buy_amt,
                        "buy_share": r.buy_share,
                        "macro_bull": r.macro_bull,
                        "ret60": r.ret60,
                    }
                    legs.append(lg2)
                    all_legs.append(lg2)
            st = summarize(legs, min_n=8)
            rows.append(
                {
                    "arch": arch,
                    "abs億": a / 1e8,
                    "share": s,
                    "hold": hold,
                    **st,
                }
            )
            log(
                f"grid {arch} ≥{a/1e8:.0f}e∩{s:.0%} H{hold}: "
                f"n={st['n']} med={st['excess_med']:+.2f} win={st.get('win', float('nan')):.0f}"
                if st["ok"]
                else f"grid {arch} ≥{a/1e8:.0f}e∩{s:.0%} H{hold}: n={st['n']} (thin)"
            )

    rdf = pd.DataFrame(rows).sort_values(["excess_med", "n"], ascending=[False, False])
    rdf.to_csv(OUT / "archetype_hold_grid.csv", index=False)
    pd.DataFrame(all_legs).to_csv(OUT / "all_legs.csv", index=False)

    # Walk-forward on best candidates per arch
    status = {"phase": "walkforward", "at": datetime.now().isoformat(timespec="seconds")}
    (OUT / "status.json").write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    log("walk-forward frozen")
    wf_rows = []
    candidates = [
        ("baseline", 2e8, 0.40, 14),
        ("baseline", 5e8, 0.40, 14),
        ("shuanghong", 2e8, 0.40, 14),
        ("shuanghong", 5e8, 0.40, 14),
        ("nanya", 2e8, 0.25, 20),
        ("nanya_soft", 2e8, 0.25, 20),
        ("chase", 5e8, 0.40, 14),
        ("dual_seat", 2e8, 0.25, 14),
        ("anti_chen_sop", 2e8, 0.40, 14),
    ]
    for arch, a, s, hold in candidates:
        sub = filter_arch(adf, arch, a, s).sort_values("d")
        dates = sub["d"].tolist()
        for train in (60, 80):
            oos_legs = []
            i = train
            while i < len(dates):
                te = set(dates[i : min(i + train, len(dates))])
                for r in sub.itertuples():
                    if r.d not in te:
                        continue
                    lg = make_leg(r.sid, r.d, hold)
                    if lg:
                        oos_legs.append(lg)
                i += train
            st = summarize(oos_legs, min_n=8)
            wf_rows.append(
                {
                    "arch": arch,
                    "abs億": a / 1e8,
                    "share": s,
                    "hold": hold,
                    "train": train,
                    **st,
                }
            )
            log(
                f"WF {arch} H{hold} tr{train}: n={st['n']} med={st['excess_med']:+.2f}"
                if st["ok"]
                else f"WF {arch} H{hold} tr{train}: n={st['n']} thin"
            )
    wdf = pd.DataFrame(wf_rows).sort_values(["excess_med", "n"], ascending=[False, False])
    wdf.to_csv(OUT / "walkforward.csv", index=False)

    # IS/OOS split at 2025-01-01
    log("IS/OOS 2025 cut")
    cut = "2025-01-01"
    io_rows = []
    for arch, a, s, hold in candidates:
        sub = filter_arch(adf, arch, a, s)
        for label, part in [("IS", sub[sub["d"] < cut]), ("OOS", sub[sub["d"] >= cut])]:
            legs = []
            for r in part.itertuples():
                lg = make_leg(r.sid, r.d, hold)
                if lg:
                    legs.append(lg)
            st = summarize(legs, min_n=5)
            io_rows.append({"arch": arch, "abs億": a / 1e8, "share": s, "hold": hold, "split": label, **st})
    iodf = pd.DataFrame(io_rows)
    iodf.to_csv(OUT / "is_oos_2025.csv", index=False)

    # Briefing
    log("write briefing")
    top_grid = rdf[rdf["ok"]].head(15)
    top_wf = wdf[wdf["ok"]].head(12)
    lines = [
        "# 陳族元風格 · Overnight 研究簡報\n\n",
        f"- 生成：{datetime.now().isoformat(timespec='seconds')}\n",
        f"- 窗：`{D0}`..`{D1}` · 訊號截止 `{SIG_CUT}`\n",
        f"- 分點：富邦新店 `9661` · 超額 = 股 − {BETA}×IX0001 · 成本 30bps · L1\n",
        f"- 耗時：{(time.time()-t0)/60:.1f} 分\n\n",
        "## 原型定義\n\n",
        "| arch | 意義 |\n|------|------|\n",
        "| baseline | 新店 Top1 ≥abs∩share，剔權值 |\n",
        "| shuanghong | 雙鴻型：>SMA20∧>SMA60∧ret60>0 |\n",
        "| shuanghong_macro | 雙鴻型 + macro_bull≥1（公開 SOP） |\n",
        "| nanya / nanya_soft | 南亞科型：弱勢日訊號（均線下 + 深跌） |\n",
        "| chase | 強勢追價（>均線 + ret20>5%）≈新店爆量段 |\n",
        "| dual_seat | 同日嘉義或南屯共現 |\n",
        "| anti_chen_sop | macro_bull=0 但仍 >SMA60 |\n\n",
        "## Grid Top（excess 中位）\n\n",
        "| arch | abs億 | share | H | n | excess中位 | win% | slot中位 | slotDD% |\n",
        "|------|------:|------:|--:|--:|----------:|-----:|---------:|--------:|\n",
    ]
    for r in top_grid.itertuples():
        lines.append(
            f"| {r.arch} | {r.abs億:.0f} | {r.share:.0%} | {r.hold} | {r.n} | "
            f"{r.excess_med:+.2f} | {r.win:.0f} | {r.slot_med:+.2f} | {r.slot_dd:.1f} |\n"
        )
    lines += [
        "\n## Walk-forward Top\n\n",
        "| arch | abs億 | share | H | train | n | excess中位 | win% |\n",
        "|------|------:|------:|--:|------:|--:|----------:|-----:|\n",
    ]
    for r in top_wf.itertuples():
        lines.append(
            f"| {r.arch} | {r.abs億:.0f} | {r.share:.0%} | {r.hold} | {r.train} | {r.n} | "
            f"{r.excess_med:+.2f} | {r.win:.0f} |\n"
        )

    # key contrasts
    def pick(arch: str, hold: int = 14):
        sub = rdf[(rdf.arch == arch) & (rdf.hold == hold) & (rdf.ok)]
        if sub.empty:
            return None
        return sub.sort_values("excess_med", ascending=False).iloc[0]

    lines.append("\n## 關鍵對照（同窗 Grid）\n\n")
    for arch in ["baseline", "shuanghong", "shuanghong_macro", "nanya", "chase", "dual_seat", "anti_chen_sop"]:
        r = pick(arch, 14)
        if r is None:
            r = pick(arch, 20)
        if r is None:
            r = pick(arch, 7)
        if r is None:
            lines.append(f"- **{arch}**：樣本不足\n")
        else:
            lines.append(
                f"- **{arch}**：H{int(r.hold)} ≥{r.abs億:.0f}億∩{r.share:.0%} · "
                f"n={int(r.n)} · excess中位 **{r.excess_med:+.2f}%** · win {r.win:.0f}%\n"
            )

    lines += [
        "\n## 初步判讀（機器寫，醒來覆核）\n\n",
        "1. 若 shuanghong ≫ baseline：雙鴻型趨勢確認對新店有加值。\n",
        "2. 若 shuanghong_macro ≤ shuanghong：公開宏觀 SOP 不宜當短線 gate（延續日盤發現）。\n",
        "3. 若 nanya 在 H20 優於 H7：深跌主題需要長持有，短線跟單會扭曲。\n",
        "4. 若 chase 強：新店 alpha 偏「漲勢確認後爆量」，與鄉民想像的抄底不同。\n",
        "5. 若 dual_seat 無提升：嘉義／南屯不像同戶拆席。\n\n",
        "## 產物\n\n",
        "- `archetype_hold_grid.csv`\n",
        "- `walkforward.csv`\n",
        "- `is_oos_2025.csv`\n",
        "- `xindian_top1_annotated.csv`\n",
        "- `all_legs.csv`\n",
    ]
    (OUT / "OVERNIGHT_BRIEFING.md").write_text("".join(lines), encoding="utf-8")

    status = {
        "phase": "done",
        "at": datetime.now().isoformat(timespec="seconds"),
        "elapsed_min": round((time.time() - t0) / 60, 1),
        "grid_rows": len(rdf),
        "wf_rows": len(wdf),
    }
    (OUT / "status.json").write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"DONE elapsed={status['elapsed_min']}m → {OUT}")
    conn.close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        import traceback

        log(f"FATAL: {exc}")
        traceback.print_exc()
        status = {
            "phase": "fatal",
            "at": datetime.now().isoformat(timespec="seconds"),
            "error": str(exc),
        }
        (OUT / "status.json").write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
        raise
