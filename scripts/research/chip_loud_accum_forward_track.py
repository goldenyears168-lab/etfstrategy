#!/usr/bin/env python3
"""高調吸貨前瞻紀錄（topic ``chip-loud-accum-forward`` · 凍結規格 2026-08-28）.

每晚做三件事：
1. 抓當日全宇宙分點日報（FinMind by-stock，≈950 request），算家數差與集中度
2. 記錄買/賣兩側各 30 檔到 DB 表 ``chip_loud_accum_forward``（證據進 DB，不留在
   被 gitignore 的 reports/——教訓見 commit 869557f）
3. 回填已到期事件的 outcome（T+1 跳空/開收、T+1 收→T+10 收與宇宙均值）

凍結規格（不得改動；改動＝前瞻重新起算）：
  宇宙 close>=10 且 20 日均量>300k 股、四碼普通股；brdiff=(nb-ns)/n；
  假說格 A1=買側∩當日漲>=3%、A2=A1∩跳空>=事件中位、B=A1剔除 top1>P75。
  進出場 T+1 收→T+10 收；判準 60 訊號日 NW t>=3。

用法::

    PYTHONPATH=src .venv/bin/python scripts/research/chip_loud_accum_forward_track.py
    PYTHONPATH=src .venv/bin/python scripts/research/chip_loud_accum_forward_track.py --date 2026-08-27
"""
from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from finmind_client import fetch_taiwan_stock_trading_daily_report  # noqa: E402
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402

TOP_N = 30
REQUEST_DELAY = 0.25
WORKERS = 3
FIRST_SIGNAL_DATE = "2026-08-27"  # 前瞻起算日（8/27 名單已於當日盤後產出）

DDL = """CREATE TABLE IF NOT EXISTS chip_loud_accum_forward (
  signal_date TEXT NOT NULL,
  stock_id    TEXT NOT NULL,
  side        TEXT NOT NULL,          -- buy / sell
  rank        INTEGER,
  n_branches  INTEGER, nb INTEGER, ns INTEGER,
  brdiff      REAL,
  top1_pct    REAL, top2_5_pct REAL,
  chg_t       REAL,                   -- 訊號日當日漲跌 %
  close_t     REAL,
  gap_t1      REAL, oc_t1 REAL,      -- 回填：T+1 跳空 / 開收 %
  entry_close REAL, exit_close REAL, -- 回填：T+1 收、T+10 收
  ret10       REAL, uni_ret10 REAL,  -- 回填：事件與宇宙的 T+1收→T+10收 %
  outcome_filled INTEGER DEFAULT 0,
  synced_at   TEXT,
  PRIMARY KEY (signal_date, stock_id, side)
)"""


def _bars(conn, start: str, end: str) -> pd.DataFrame:
    """去重日線（官方來源優先）。"""
    return pd.read_sql_query(
        """SELECT stock_id, trade_date, open, close, volume FROM (
             SELECT *, ROW_NUMBER() OVER (PARTITION BY stock_id, trade_date
               ORDER BY CASE source WHEN 'twse_mi_index' THEN 0
                 WHEN 'tpex_daily' THEN 1 WHEN 'finmind' THEN 2 ELSE 3 END) rn
             FROM stock_daily_bars WHERE trade_date>=? AND trade_date<=?)
           WHERE rn=1 AND open>0 AND close>0""",
        conn, params=(start, end))


def universe_for(conn, d: str) -> pd.DataFrame:
    """凍結宇宙：close>=10、20 日均量>300k、四碼普通股。回傳含 close/前收。"""
    b = _bars(conn, (pd.Timestamp(d) - pd.Timedelta(days=45)).date().isoformat(), d)
    days = sorted(b.trade_date.unique())
    if d not in days:
        return pd.DataFrame()
    win = days[-21:-1] if len(days) > 21 else days[:-1]
    av = b[b.trade_date.isin(win)].groupby("stock_id").volume.mean()
    cur = b[b.trade_date == d].set_index("stock_id")
    prev_day = days[days.index(d) - 1] if days.index(d) > 0 else None
    pc = (b[b.trade_date == prev_day].set_index("stock_id").close
          if prev_day else pd.Series(dtype=float))
    u = cur.join(av.rename("av20")).join(pc.rename("prev_close"))
    u = u[(u.close >= 10) & (u.av20 > 300_000)]
    u = u[u.index.str.fullmatch(r"\d{4}")]
    u["chg_t"] = (u.close / u.prev_close - 1) * 100
    return u


def fetch_branches(sids: list[str], d: str) -> dict[str, list[dict]]:
    def one(sid):
        for att in range(3):
            try:
                rows = fetch_taiwan_stock_trading_daily_report(trade_date=d, data_id=sid)
                time.sleep(REQUEST_DELAY)
                return sid, rows
            except Exception as e:  # noqa: BLE001
                if "402" in str(e) or "upper limit" in str(e).lower():
                    raise
                time.sleep(2 * (att + 1))
        return sid, []
    out: dict[str, list[dict]] = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for k, (sid, rows) in enumerate(ex.map(one, sids)):
            out[sid] = rows
            if k % 200 == 0:
                print(f"  fetch {k}/{len(sids)}", flush=True)
    return out


def record_day(conn, d: str) -> int:
    """抓 d 日資料並寫入兩側名單。回傳寫入列數（0=無資料/休市）。"""
    u = universe_for(conn, d)
    if u.empty:
        print(f"{d}: 非交易日或日線未到，跳過")
        return 0
    # 先探一檔確認 FinMind 已有當日分點
    probe = fetch_taiwan_stock_trading_daily_report(trade_date=d, data_id="2330")
    if not probe:
        print(f"{d}: FinMind 分點尚未可得，留待下次")
        return 0
    raw = fetch_branches(list(u.index), d)
    recs = []
    for sid, rows in raw.items():
        if not rows:
            continue
        df = pd.DataFrame(rows)
        if df.empty or "buy" not in df:
            continue
        g = df.groupby("securities_trader_id")[["buy", "sell"]].sum()
        g["net"] = g.buy - g.sell
        g = g[g.net != 0]
        if len(g) < 40:            # 深度斷言：分點太少不進 rank
            continue
        nb, ns, n = int((g.net > 0).sum()), int((g.net < 0).sum()), len(g)
        pos = g[g.net > 0].net.sort_values(ascending=False)
        vol = float(u.loc[sid, "volume"])
        recs.append({
            "stock_id": sid, "n": n, "nb": nb, "ns": ns,
            "brdiff": (nb - ns) / n,
            "top1_pct": pos.iloc[0] / vol * 100 if len(pos) else 0.0,
            "top2_5_pct": pos.iloc[1:5].sum() / vol * 100,
            "chg_t": float(u.loc[sid, "chg_t"]) if pd.notna(u.loc[sid, "chg_t"]) else None,
            "close_t": float(u.loc[sid, "close"]),
        })
    r = pd.DataFrame(recs)
    if len(r) < 100:
        print(f"{d}: 有效股 {len(r)} 檔過少，資料可能不完整，跳過不記錄")
        return 0
    now = pd.Timestamp.now().isoformat(timespec="seconds")
    written = 0
    for side, sel in (("buy", r.nsmallest(TOP_N, "brdiff")),
                      ("sell", r.nlargest(TOP_N, "brdiff"))):
        for rank, (_, row) in enumerate(sel.iterrows(), 1):
            conn.execute(
                """INSERT OR REPLACE INTO chip_loud_accum_forward
                   (signal_date, stock_id, side, rank, n_branches, nb, ns, brdiff,
                    top1_pct, top2_5_pct, chg_t, close_t, outcome_filled, synced_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0,?)""",
                (d, row.stock_id, side, rank, row.n, row.nb, row.ns,
                 round(row.brdiff, 4), round(row.top1_pct, 3),
                 round(row.top2_5_pct, 3),
                 None if row.chg_t is None else round(row.chg_t, 3),
                 row.close_t, now))
            written += 1
    conn.commit()
    print(f"{d}: 記錄 {written} 列（宇宙 {len(r)} 檔）")
    return written


def backfill_outcomes(conn) -> int:
    """回填 T+1 跳空/開收與 T+1收→T+10收（含宇宙均值）。"""
    todo = pd.read_sql_query(
        "SELECT DISTINCT signal_date FROM chip_loud_accum_forward WHERE outcome_filled=0",
        conn)
    if todo.empty:
        return 0
    filled = 0
    for d in sorted(todo.signal_date):
        b = _bars(conn, d, date.today().isoformat())
        days = sorted(b.trade_date.unique())
        # 幽靈日防護：交易日=當日宇宙檔數>500
        counts = b.groupby("trade_date").size()
        days = [x for x in days if counts.get(x, 0) > 500]
        if d not in days or len(days) < days.index(d) + 11 + 1:
            continue                      # T+10 收盤還沒到
        i = days.index(d)
        e, end = days[i + 1], days[i + 10]
        px = b.pivot_table(index="trade_date", columns="stock_id", values="close")
        po = b.pivot_table(index="trade_date", columns="stock_id", values="open")
        uni = universe_for(conn, d)
        umask = [s for s in uni.index if s in px.columns]
        uni_ret = ((px.loc[end] / px.loc[e] - 1) * 100).reindex(umask).mean()
        rows = pd.read_sql_query(
            "SELECT stock_id, side, close_t FROM chip_loud_accum_forward "
            "WHERE signal_date=? AND outcome_filled=0", conn, params=(d,))
        for _, row in rows.iterrows():
            sid = row.stock_id
            try:
                o1, c1, c10 = po.loc[e, sid], px.loc[e, sid], px.loc[end, sid]
            except KeyError:
                continue
            if pd.isna(o1) or pd.isna(c1) or pd.isna(c10):
                continue
            conn.execute(
                """UPDATE chip_loud_accum_forward SET gap_t1=?, oc_t1=?,
                     entry_close=?, exit_close=?, ret10=?, uni_ret10=?, outcome_filled=1
                   WHERE signal_date=? AND stock_id=? AND side=?""",
                (round((o1 / row.close_t - 1) * 100, 3),
                 round((c1 / o1 - 1) * 100, 3),
                 c1, c10, round((c10 / c1 - 1) * 100, 3), round(uni_ret, 3),
                 d, sid, row.side))
            filled += 1
        conn.commit()
        print(f"{d}: outcome 回填 {filled} 列（宇宙均值 {uni_ret:+.3f}%）")
    return filled


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=Path(DEFAULT_DB_PATH))
    ap.add_argument("--date", default=None, help="只補指定訊號日（預設：自動補缺）")
    ap.add_argument("--skip-fetch", action="store_true", help="只回填 outcome")
    a = ap.parse_args()
    conn = connect(a.db)
    conn.execute("PRAGMA busy_timeout = 60000")
    conn.execute(DDL)
    if not a.skip_fetch:
        if a.date:
            record_day(conn, a.date)
        else:
            last = conn.execute(
                "SELECT MAX(signal_date) FROM chip_loud_accum_forward").fetchone()[0]
            start = last or FIRST_SIGNAL_DATE
            b = _bars(conn, start, date.today().isoformat())
            counts = b.groupby("trade_date").size()
            for d in sorted(x for x in counts.index if counts[x] > 500):
                if last and d <= last:
                    continue
                record_day(conn, d)
    backfill_outcomes(conn)
    return 0


if __name__ == "__main__":
    sys.exit(main())
