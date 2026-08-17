#!/usr/bin/env python3
"""Round10 · 「乾涸是不是 INNER JOIN 缺價假象」定案審計（DB 唯讀 · 不下單）.

背景：run_songshan_follow_watch.scan_5d_net95() 用
    stock_broker_branch_daily b JOIN stock_daily_bars p ON 同日同股
缺當日 close 的分點列會被 INNER JOIN 靜默丟掉；而 refresh_missing_ohlc() 每輪
只補 80 檔（missing[:80]），9217 tape 每日缺價 200~350 檔。

本腳本用 PIT 合法的「最近一筆已知收盤價」（trade_date <= 該日的最新 close）
取代同日 INNER JOIN，重算全期事件並與原版逐筆比對。

用法：
    PYTHONPATH=src .venv/bin/python \
        scripts/research/round10_drought_priced_join_audit.py
輸出：reports/research/branch-footprint-screen/round10_drought_joinaudit_*.csv|json
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np
import pandas as pd

from stock_db import DEFAULT_DB_PATH

SOURCE = "finmind"
TRADER_ID = "9217"
STUDY_START = "2024-07-01"
STUDY_END = "2026-08-14"
PRICE_LOOKBACK_START = "2023-06-01"  # for ffill seeding
BASE_FLOOR, BASE_NET = 0.5e8, 0.95
MEGA_PATH = (
    ROOT / "reports" / "research" / "branch-footprint-screen"
    / "ab58_xMega_copytrade" / "mega_blacklist_v1.json"
)
OUT_DIR = ROOT / "reports" / "research" / "branch-footprint-screen"
PREFIX = "round10_drought_joinaudit"
DROUGHT_FROM = "2026-07-23"


def ro_connect() -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{DEFAULT_DB_PATH}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def section(t: str) -> None:
    print(f"\n{'=' * 92}\n{t}\n{'=' * 92}")


def load_mega() -> set[str]:
    return {str(s) for s in json.loads(MEGA_PATH.read_text())["symbols"]}


def load_calendar(conn) -> list[str]:
    return [
        str(r[0])
        for r in conn.execute(
            """SELECT trade_date FROM stock_daily_bars
               WHERE stock_id='2330' AND source=? AND trade_date BETWEEN ? AND ? AND close>0
               ORDER BY trade_date""",
            (SOURCE, STUDY_START, STUDY_END),
        )
    ]


def load_tape_shares(conn) -> pd.DataFrame:
    """9217 tape in SHARES — no price join at all (this is the honest denominator)."""
    return pd.read_sql_query(
        """
        SELECT stock_id, trade_date, buy AS buy_sh, sell AS sell_sh
        FROM stock_broker_branch_daily
        WHERE source=? AND securities_trader_id=?
          AND trade_date BETWEEN ? AND ?
          AND length(stock_id)=4
          AND stock_id GLOB '[0-9][0-9][0-9][0-9]'
          AND stock_id NOT GLOB '00*'
        """,
        conn,
        params=(SOURCE, TRADER_ID, STUDY_START, STUDY_END),
    )


def load_bars(conn, stock_ids: list[str]) -> pd.DataFrame:
    frames = []
    for i in range(0, len(stock_ids), 900):
        chunk = stock_ids[i : i + 900]
        ph = ",".join("?" * len(chunk))
        frames.append(
            pd.read_sql_query(
                f"""SELECT stock_id, trade_date, close FROM stock_daily_bars
                    WHERE source=? AND close>0 AND trade_date BETWEEN ? AND ?
                      AND stock_id IN ({ph})""",
                conn,
                params=(SOURCE, PRICE_LOOKBACK_START, STUDY_END, *chunk),
            )
        )
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def rising_edge(df: pd.DataFrame, trig_col: str) -> pd.DataFrame:
    prev = df.groupby("stock_id", sort=False)[trig_col].shift(1).fillna(False)
    ev = df[df[trig_col] & (~prev)].copy()
    return (
        ev.rename(columns={"trade_date": "signal_date"})
        .sort_values(["signal_date", "stock_id"])
        .reset_index(drop=True)
    )


def main() -> int:
    conn = ro_connect()
    mega = load_mega()
    cal = load_calendar(conn)
    tape = load_tape_shares(conn)
    stocks = sorted(tape["stock_id"].unique())
    print(f"[INFO] calendar={len(cal)} tape_rows={len(tape)} stocks={len(stocks)}")

    bars = load_bars(conn, stocks)
    print(f"[INFO] bars rows={len(bars)} stocks_with_any_bar={bars['stock_id'].nunique()}")

    # ---- PIT last-known-close panel: calendar x stock, ffill (only past info)
    px = bars.pivot_table(index="trade_date", columns="stock_id", values="close", aggfunc="last")
    full_idx = sorted(set(px.index) | set(cal))
    px = px.reindex(full_idx).sort_index()
    px_ff = px.ffill()
    # age of the ffilled price in calendar rows (0 = same-day real close)
    age = px.notna().cumsum()
    age = age.where(px.notna()).ffill()
    row_no = pd.Series(np.arange(len(px)), index=px.index)
    last_real = pd.DataFrame(
        np.where(px.notna(), row_no.to_numpy()[:, None], np.nan),
        index=px.index, columns=px.columns,
    ).ffill()
    price_age = row_no.to_numpy()[:, None] - last_real.to_numpy()

    px_ff = px_ff.loc[cal]
    px_same = px.loc[cal]
    price_age_df = pd.DataFrame(price_age, index=px.index, columns=px.columns).loc[cal]

    # ---- coverage diagnostics on the tape
    t = tape.copy()
    same = px_same.stack(dropna=True).rename("close_same")
    ffl = px_ff.stack(dropna=True).rename("close_ff")
    aged = price_age_df.stack(dropna=True).rename("price_age")
    t = t.merge(same.reset_index().rename(columns={"level_0": "trade_date"}),
                on=["trade_date", "stock_id"], how="left")
    t = t.merge(ffl.reset_index().rename(columns={"level_0": "trade_date"}),
                on=["trade_date", "stock_id"], how="left")
    t = t.merge(aged.reset_index().rename(columns={"level_0": "trade_date"}),
                on=["trade_date", "stock_id"], how="left")
    t["buy_amt_same"] = t["buy_sh"] * t["close_same"]
    t["buy_amt_ff"] = t["buy_sh"] * t["close_ff"]

    section("(0) 缺價覆蓋率（scan 過濾後宇宙）")
    t["ym"] = t["trade_date"].str[:7]
    cov = t.groupby("ym").agg(
        tape_rows=("stock_id", "size"),
        priced_same=("close_same", "count"),
        priced_ff=("close_ff", "count"),
    )
    cov["missing_same_pct"] = (100 * (1 - cov.priced_same / cov.tape_rows)).round(1)
    cov["still_missing_ff_pct"] = (100 * (1 - cov.priced_ff / cov.tape_rows)).round(1)
    # how much BUY VALUE was silently dropped (valued at ffill price)
    dropped = t[t["close_same"].isna()]
    cov = cov.join(
        t.groupby("ym")["buy_amt_ff"].sum().rename("buy_value_ff_total")
    ).join(dropped.groupby("ym")["buy_amt_ff"].sum().rename("buy_value_dropped_ff"))
    cov["dropped_buy_value_pct"] = (
        100 * cov.buy_value_dropped_ff.fillna(0) / cov.buy_value_ff_total
    ).round(2)
    cov["total_yi"] = (cov.buy_value_ff_total / 1e8).round(1)
    cov["dropped_yi"] = (cov.buy_value_dropped_ff.fillna(0) / 1e8).round(2)
    show_cov = cov[["tape_rows", "missing_same_pct", "still_missing_ff_pct",
                    "total_yi", "dropped_yi", "dropped_buy_value_pct"]]
    print(show_cov.to_string())
    show_cov.to_csv(OUT_DIR / f"{PREFIX}_coverage_monthly.csv")

    # ---- build both panels on the full grid
    grid = pd.MultiIndex.from_product([stocks, cal], names=["stock_id", "trade_date"]).to_frame(
        index=False
    )
    m = grid.merge(
        t[["stock_id", "trade_date", "buy_sh", "sell_sh", "close_same", "close_ff", "price_age"]],
        on=["stock_id", "trade_date"], how="left",
    )
    for variant, pxcol in (("same", "close_same"), ("ff", "close_ff")):
        m[f"buy_{variant}"] = (m["buy_sh"] * m[pxcol]).fillna(0.0)
        m[f"sell_{variant}"] = (m["sell_sh"] * m[pxcol]).fillna(0.0)
    m = m.sort_values(["stock_id", "trade_date"]).reset_index(drop=True)
    g = m.groupby("stock_id", sort=False)
    for v in ("same", "ff"):
        m[f"buy5_{v}"] = g[f"buy_{v}"].transform(lambda s: s.rolling(5, min_periods=5).sum())
        m[f"sell5_{v}"] = g[f"sell_{v}"].transform(lambda s: s.rolling(5, min_periods=5).sum())
        m[f"net_{v}"] = np.where(
            m[f"buy5_{v}"] > 0,
            (m[f"buy5_{v}"] - m[f"sell5_{v}"]) / m[f"buy5_{v}"].replace(0, np.nan),
            np.nan,
        )
        m[f"trig_{v}"] = (
            (m[f"buy5_{v}"] >= BASE_FLOOR)
            & (m[f"net_{v}"] >= BASE_NET)
            & (~m["stock_id"].isin(mega))
        ).fillna(False)

    ev_same = rising_edge(m, "trig_same")
    ev_ff = rising_edge(m, "trig_ff")
    section("(1) 事件數比對")
    key_same = set(zip(ev_same.stock_id, ev_same.signal_date))
    key_ff = set(zip(ev_ff.stock_id, ev_ff.signal_date))
    only_ff = sorted(key_ff - key_same)
    only_same = sorted(key_same - key_ff)
    res = {
        "n_events_same_day_join_LIVE": len(ev_same),
        "n_events_pit_ffill": len(ev_ff),
        "delta": len(ev_ff) - len(ev_same),
        "n_only_in_ffill": len(only_ff),
        "n_only_in_same_day": len(only_same),
        "last_event_same": ev_same["signal_date"].max(),
        "last_event_ffill": ev_ff["signal_date"].max(),
    }
    print(json.dumps(res, ensure_ascii=False, indent=2))

    section("(2) 差異事件逐筆")
    rows = []
    mi = m.set_index(["stock_id", "trade_date"])
    for sid, d in only_ff + only_same:
        r = mi.loc[(sid, d)]
        rows.append({
            "kind": "only_ffill" if (sid, d) in key_ff and (sid, d) not in key_same else "only_same_day",
            "stock_id": sid, "signal_date": d,
            "buy5d_same_yi": round(float(r["buy5_same"] or 0) / 1e8, 3),
            "net_same": round(float(r["net_same"]), 4) if pd.notna(r["net_same"]) else None,
            "buy5d_ff_yi": round(float(r["buy5_ff"] or 0) / 1e8, 3),
            "net_ff": round(float(r["net_ff"]), 4) if pd.notna(r["net_ff"]) else None,
            "price_age_rows": None if pd.isna(r["price_age"]) else int(r["price_age"]),
            "in_drought_window": d >= DROUGHT_FROM,
        })
    diff = pd.DataFrame(rows)
    if len(diff):
        print(diff.to_string(index=False))
        diff.to_csv(OUT_DIR / f"{PREFIX}_diff_events.csv", index=False)
    else:
        print("（無差異事件）")
    res["diff_events"] = rows

    section("(3) 乾涸窗（2026-07-23 ~ 2026-08-14）ffill 版有無觸發")
    dr = m[(m["trade_date"] >= DROUGHT_FROM) & m["trig_ff"]]
    print(f"ffill 版乾涸窗內原始觸發 (stock,day) = {len(dr)}")
    if len(dr):
        print(dr[["stock_id", "trade_date", "buy5_ff", "net_ff"]].to_string(index=False))
    dr_ev = ev_ff[ev_ff["signal_date"] >= DROUGHT_FROM]
    print(f"ffill 版乾涸窗內 rising-edge 新事件 = {len(dr_ev)}")
    res["drought_window_ffill_raw_trigger_stockdays"] = len(dr)
    res["drought_window_ffill_new_events"] = len(dr_ev)

    section("(3b) fallback 版重算 Q1 漏斗：候選/日 與 net>=0.95 通過率")
    mm = m.copy()
    mm["ym"] = mm["trade_date"].str[:7]
    mm["is_mega"] = mm["stock_id"].isin(mega)
    funnel_rows = []
    for ym, sub in mm.groupby("ym"):
        n_days = sub["trade_date"].nunique()
        out = {"ym": ym, "n_days": n_days}
        for v in ("same", "ff"):
            c = sub[(~sub["is_mega"]) & (sub[f"buy5_{v}"] >= BASE_FLOOR)]
            hit = c[c[f"net_{v}"] >= BASE_NET]
            out[f"cand_per_day_{v}"] = round(len(c) / max(n_days, 1), 1)
            out[f"pass95_pct_{v}"] = round(100 * len(hit) / len(c), 1) if len(c) else None
            out[f"netp90_{v}"] = round(float(c[f"net_{v}"].quantile(0.9)), 3) if len(c) else None
        funnel_rows.append(out)
    fun = pd.DataFrame(funnel_rows)
    print(fun.to_string(index=False))
    fun.to_csv(OUT_DIR / f"{PREFIX}_funnel_same_vs_ff.csv", index=False)
    res["funnel_same_vs_ff"] = funnel_rows

    section("(3c) 11 日冷區間 2026-07-29 ~ 2026-08-12 · fallback 版每日最高候選")
    cold = mm[
        (mm["trade_date"] >= "2026-07-29")
        & (mm["trade_date"] <= "2026-08-12")
        & (~mm["is_mega"])
        & (mm["buy5_ff"] >= BASE_FLOOR)
    ].copy()
    cold["b5_ff_yi"] = (cold["buy5_ff"] / 1e8).round(2)
    cold["b5_same_yi"] = (cold["buy5_same"] / 1e8).round(2)
    cold["net_ff"] = cold["net_ff"].round(3)
    topc = cold.sort_values(["trade_date", "net_ff"], ascending=[True, False]).groupby(
        "trade_date"
    ).head(2)[["trade_date", "stock_id", "b5_same_yi", "b5_ff_yi", "net_ff"]]
    print(topc.to_string(index=False))
    topc.to_csv(OUT_DIR / f"{PREFIX}_cold_window_ff_top.csv", index=False)
    res["cold_window_ff_max_net"] = (
        round(float(cold["net_ff"].max()), 4) if len(cold) else None
    )
    res["cold_window_ff_cand_per_day"] = round(len(cold) / 11, 1)

    section("(4) 歷史事件母體是否被系統性吃掉：既有事件的 buy_5d 低估幅度")
    hist = ev_same.copy()
    vals = []
    for r in hist.itertuples(index=False):
        row = mi.loc[(r.stock_id, r.signal_date)]
        vals.append({
            "stock_id": r.stock_id, "signal_date": r.signal_date,
            "buy5d_same_yi": round(float(row["buy5_same"]) / 1e8, 3),
            "buy5d_ff_yi": round(float(row["buy5_ff"]) / 1e8, 3),
            "uplift_pct": round(100 * (row["buy5_ff"] / row["buy5_same"] - 1), 2)
            if row["buy5_same"] else None,
            "net_same": round(float(row["net_same"]), 4),
            "net_ff": round(float(row["net_ff"]), 4) if pd.notna(row["net_ff"]) else None,
        })
    hv = pd.DataFrame(vals)
    hv.to_csv(OUT_DIR / f"{PREFIX}_existing_events_uplift.csv", index=False)
    print(hv["uplift_pct"].describe().round(2).to_string())
    print(f"uplift>1% 的事件數 = {(hv['uplift_pct'] > 1).sum()} / {len(hv)}")
    res["existing_events_uplift"] = {
        "n": len(hv),
        "median_uplift_pct": round(float(hv["uplift_pct"].median()), 3),
        "max_uplift_pct": round(float(hv["uplift_pct"].max()), 3),
        "n_uplift_gt_1pct": int((hv["uplift_pct"] > 1).sum()),
        "n_net_flip_below_095": int((hv["net_ff"] < BASE_NET).sum()),
    }

    section("(5) 缺價標的到底有多大：缺價列的買進金額分布（以 ffill 價估）")
    dsum = dropped.copy()
    dsum["buy_yi"] = dsum["buy_amt_ff"] / 1e8
    q = dsum["buy_yi"].describe(percentiles=[0.5, 0.9, 0.99, 0.999]).round(4)
    print(q.to_string())
    top = (
        dsum.groupby("stock_id")["buy_amt_ff"].sum().sort_values(ascending=False).head(15) / 1e8
    ).round(3)
    print("\n缺價標的累積買進金額 Top15（億）:")
    print(top.to_string())
    # per-stock max rolling 5d under ffill, restricted to stocks that are NEVER priced same-day
    never = set(t.loc[t["close_same"].isna(), "stock_id"]) - set(
        t.loc[t["close_same"].notna(), "stock_id"]
    )
    res["dropped_rows_buy_yi_p99"] = float(q.get("99%", float("nan")))
    res["dropped_rows_buy_yi_max"] = float(q.get("max", float("nan")))
    res["n_stocks_never_priced_same_day"] = len(never)
    mn = m[m["stock_id"].isin(never)]
    res["max_buy5d_ff_among_never_priced_yi"] = (
        round(float(mn["buy5_ff"].max() or 0) / 1e8, 3) if len(mn) else 0.0
    )
    print(f"\n完全從未有同日價的標的數 = {len(never)}，"
          f"其 ffill 版 buy_5d 最大值 = {res['max_buy5d_ff_among_never_priced_yi']} 億"
          f"（門檻 {BASE_FLOOR/1e8} 億）")

    ev_ff.to_csv(OUT_DIR / f"{PREFIX}_events_ffill.csv", index=False)
    (OUT_DIR / f"{PREFIX}_summary.json").write_text(
        json.dumps(res, ensure_ascii=False, indent=2, default=str)
    )
    print(f"\n[OK] wrote {OUT_DIR}/{PREFIX}_*")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
