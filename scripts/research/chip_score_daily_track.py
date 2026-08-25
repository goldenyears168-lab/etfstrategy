#!/usr/bin/env python3
"""v4 籌碼評分的每日前瞻紀錄 —— 累積真正的樣本外證據。

**為什麼需要這支**：本研究線的乾淨 hold-out 已在合併測試（OOS 涵蓋至 2026-08）
時燒掉，之後任何在既有資料上的比較都無法區分「機制真的變了」與「在同一份
資料上調參」。唯一的解法是**往前累積**。

設計凍結於 **2026-08-23**。凍結後的每一天都是真正的樣本外，紀錄裡以
``regime='forward'`` 標記；凍結前補記的以 ``'backfill'`` 標記，兩者不可混算。

**否證條件**（已登錄於 config/research.yaml 的 H-CHIP-BRANCH-INCREMENT）：
累積 60~120 個 forward 交易日後重測，若多空價差的 t 掉回 1.5 以下，
「近期習慣為主」的解釋即被推翻。

⚠️ **不要用單日結果調整看法**。單日多空價差的歷史標準差是 0.456%（收→收），
平均只有 +0.115%——**60.5% 的日子為正**。單日落在 80 百分位跟丟銅板連對兩次
差不多。這支工具存在的意義就是避免那種誤判。

用法::

    # 記錄某一天（訊號取前一交易日、報酬取當日）
    PYTHONPATH=src .venv/bin/python scripts/research/chip_score_daily_track.py --date 2026-08-24

    # 只看累積結果
    PYTHONPATH=src .venv/bin/python scripts/research/chip_score_daily_track.py --summary
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from stock_db import connect, connect_ro

FREEZE_DATE = "2026-08-23"
# SSOT 存 DB（reports/ 在 .gitignore 裡，紀錄不能只活在被忽略的檔案）；
# CSV 只是方便人眼檢視的匯出。
OUT = Path("reports/research/chip-signal-daily-horizon/daily_track.csv")
MIN_VOL_LOTS = 500
MIN_CLOSE = 10.0


def _brief_module():
    from importlib.machinery import SourceFileLoader
    here = Path(__file__).resolve().parent
    return SourceFileLoader("brief", str(here / "run_chip_daily_brief.py")).load_module()


def _score_module():
    # 絕對路徑：launchd 的 cwd 不在 repo，相對路徑會 FileNotFoundError。
    from importlib.machinery import SourceFileLoader
    here = Path(__file__).resolve().parent
    return SourceFileLoader("snap", str(here / "stock_chip_snapshot.py")).load_module()


def prev_trading_date(d: str) -> str | None:
    c = connect_ro()
    r = c.execute("SELECT MAX(trade_date) FROM stock_short_interest_daily WHERE trade_date < ?",
                  (d,)).fetchone()
    return r[0] if r and r[0] else None


def _risk_neutral(m: pd.DataFrame) -> pd.Series:
    """對「波動／跳空／市值」五分位虛擬變數 ＋ 波動×跳空交互項迴歸取殘差。

    為什麼不用線性控制：波動效應強烈非線性（v4 的價差在低波動層 t=+6.17、
    高波動層 t=−3.42，直接變號），線性殘差只移除一半。
    為什麼不用分格法：三維會退化到只剩 1 格，那種 t 值沒有意義。
    """
    g = m.dropna(subset=["oc", "vol60", "gap", "mcap"]).copy()
    if len(g) < 120:
        return pd.Series(np.nan, index=m.index)
    for c, src in (("vq", "vol60"), ("gq", "gap"), ("mq", "mcap")):
        g[c] = pd.qcut(g[src].rank(method="first"), 5, labels=False, duplicates="drop")
    g = g.dropna(subset=["vq", "gq", "mq"])
    P = [np.ones((len(g), 1))]
    for c in ("vq", "gq", "mq"):
        P.append(pd.get_dummies(g[c].astype(int), drop_first=True).to_numpy(float))
    P.append(pd.get_dummies(g.vq.astype(int) * 5 + g.gq.astype(int),
                            drop_first=True).to_numpy(float))
    X = np.column_stack(P)
    y = g.oc.to_numpy()
    try:
        b, *_ = np.linalg.lstsq(X, y, rcond=None)
    except np.linalg.LinAlgError:
        return pd.Series(np.nan, index=m.index)
    out = pd.Series(np.nan, index=m.index)
    out.loc[g.index] = y - X @ b
    return out


def _spread(df: pd.DataFrame, by: str, col: str, frac: float = 0.058,
            ascending: bool = True) -> float:
    d2 = df.dropna(subset=[by, col])
    if len(d2) < 60:
        return float("nan")
    n = max(3, int(round(len(d2) * frac)))
    s = d2.sort_values(by, ascending=ascending)
    return round((s[col].head(n).mean() - s[col].tail(n).mean()) * 100, 4)


def record(d: str) -> dict | None:
    """訊號日 = d 的前一交易日；報酬日 = d。"""
    sig_d = prev_trading_date(d)
    if not sig_d:
        return None
    mk = _score_module().market_scores(sig_d)
    if mk is None or mk.empty:
        return None
    c = connect_ro()
    px = pd.read_sql_query(
        """SELECT stock_id, close, open, volume/1000.0 vol FROM stock_daily_bars
            WHERE trade_date=? AND source='finmind'""", c, params=(d,))
    p0 = pd.read_sql_query(
        """SELECT stock_id, close AS c0, volume/1000.0 AS v0 FROM stock_daily_bars
            WHERE trade_date=? AND source='finmind'""", c, params=(sig_d,))
    m = mk[["stock_id", "score"]].merge(p0, on="stock_id").merge(px, on="stock_id")
    m = m[(m.v0 >= MIN_VOL_LOTS) & (m.c0 >= MIN_CLOSE) & m.open.notna()]
    if len(m) < 100:
        return None
    m["cc"] = m.close / m.c0 - 1
    m["oc"] = m.close / m.open - 1
    m["gap"] = m.open / m.c0 - 1
    for col in ("cc", "oc", "gap"):
        m[f"{col}x"] = m[col] - m[col].mean()
    m["q"] = pd.qcut(m.score.rank(method="first"), 5, labels=False, duplicates="drop")
    lo, hi = m[m.q == 0], m[m.q == 4]
    m["gq"] = pd.qcut(m.gapx.rank(method="first"), 5, labels=False, duplicates="drop")

    # ---- 風險中性後的版本（2026-08-26 起） ----
    snap = _score_module()
    brief = _brief_module()
    vol = pd.read_sql_query(
        """SELECT stock_id, trade_date, source, close FROM stock_daily_bars
            WHERE trade_date <= ? AND trade_date >= date(?, '-140 day')
              AND close IS NOT NULL""", c, params=(sig_d, sig_d))
    vol["rk"] = vol.source.map({"finmind": 0, "twse_mi_index": 1,
                                "tpex_daily": 2}).fillna(9)
    vol = (vol.sort_values("rk").drop_duplicates(["stock_id", "trade_date"])
              .sort_values(["stock_id", "trade_date"]))
    vol["ret"] = vol.groupby("stock_id", group_keys=False).close.pct_change()
    vol = vol[vol.ret.abs() < 0.5]                    # 擋未還原的股票分割
    v60 = (vol.groupby("stock_id").ret.apply(lambda s: s.tail(60).std())
              .rename("vol60").reset_index())
    m = m.merge(v60, on="stock_id", how="left")
    m["gap"] = m.open / m.c0 - 1
    sh = pd.read_sql_query(
        "SELECT stock_id, short_limit*4 AS shares FROM stock_short_interest_daily"
        " WHERE trade_date = ?", c, params=(sig_d,))
    m = m.merge(sh, on="stock_id", how="left")
    m["mcap"] = m.c0 * m.shares.replace(0, np.nan)
    m["oc_n"] = _risk_neutral(m)
    hold = brief.holding_structure(sig_d)
    m = m.merge(hold, on="stock_id", how="left")
    extra = {
        "v4_oc_n": _spread(m, "score", "oc_n"),
        "retail_sp_oc": _spread(m, "ret_pct", "oc"),
        "retail_sp_oc_n": _spread(m, "ret_pct", "oc_n"),
        "hold_asof": (m.as_of.dropna().max() if "as_of" in m else None),
    }
    ml = m.dropna(subset=["ret_pct", "oc_n"])
    if len(ml) >= 60:
        k = max(3, int(round(len(ml) * 0.058)))
        extra["retail_long_n"] = round(
            ml.sort_values("ret_pct").oc_n.head(k).mean() * 100, 4)
    else:
        extra["retail_long_n"] = None
    return {**extra,
        "signal_date": sig_d, "return_date": d,
        "regime": "forward" if d > FREEZE_DATE else "backfill",
        "n": len(m), "mkt_cc": round(m.cc.mean() * 100, 4),
        "spread_cc": round((lo.ccx.mean() - hi.ccx.mean()) * 100, 4),
        "spread_oc": round((lo.ocx.mean() - hi.ocx.mean()) * 100, 4),
        "q1_cc": round(lo.ccx.mean() * 100, 4), "q5_cc": round(hi.ccx.mean() * 100, 4),
        # 跳空回歸對照：低開組 − 高開組的開→收超額（歷史 +0.49%）
        "gap_rev": round((m[m.gq == 0].ocx.mean() - m[m.gq == 4].ocx.mean()) * 100, 4),
    }


def upsert(row: dict) -> None:
    from stock_db.util import utc_now_iso
    conn = connect()
    conn.execute(
        """INSERT INTO chip_score_forward_track (
               return_date, signal_date, regime, n, mkt_cc, spread_cc, spread_oc,
               q1_cc, q5_cc, gap_rev, v4_oc_n, retail_sp_oc, retail_sp_oc_n,
               retail_long_n, hold_asof, synced_at
           ) VALUES (
               :return_date, :signal_date, :regime, :n, :mkt_cc, :spread_cc,
               :spread_oc, :q1_cc, :q5_cc, :gap_rev, :v4_oc_n, :retail_sp_oc,
               :retail_sp_oc_n, :retail_long_n, :hold_asof, :synced_at)
           ON CONFLICT(return_date) DO UPDATE SET
               signal_date=excluded.signal_date, regime=excluded.regime, n=excluded.n,
               mkt_cc=excluded.mkt_cc, spread_cc=excluded.spread_cc,
               spread_oc=excluded.spread_oc, q1_cc=excluded.q1_cc, q5_cc=excluded.q5_cc,
               gap_rev=excluded.gap_rev, v4_oc_n=excluded.v4_oc_n,
               retail_sp_oc=excluded.retail_sp_oc,
               retail_sp_oc_n=excluded.retail_sp_oc_n,
               retail_long_n=excluded.retail_long_n, hold_asof=excluded.hold_asof,
               synced_at=excluded.synced_at""",
        {**row, "synced_at": utc_now_iso()})
    conn.commit()
    conn.close()


def load_track() -> pd.DataFrame:
    # connect() 才會跑 DDL；connect_ro() 不會。開場先觸發一次確保表存在，
    # 否則首次執行會死在「no such table」。
    connect().close()
    return pd.read_sql_query(
        "SELECT * FROM chip_score_forward_track ORDER BY return_date", connect_ro())


def summary(df: pd.DataFrame) -> None:
    pd.set_option("display.width", 200)
    for regime in ("forward", "backfill"):
        s = df[df.regime == regime]
        if s.empty:
            continue
        print(f"\n=== {regime}（{len(s)} 日 · {s.return_date.min()}~{s.return_date.max()}）===")
        for col, lab, exp in (("spread_cc", "多空價差 收→收", 0.115),
                              ("spread_oc", "多空價差 開→收", 0.051),
                              ("v4_oc_n", "★v4 開→收 中性後", 0.000),
                              ("retail_sp_oc", "散戶持股 多空 原始", 0.219),
                              ("retail_sp_oc_n", "★散戶持股 多空 中性後", 0.099),
                              ("retail_long_n", "★散戶最低腿 中性後", 0.058),
                              ("gap_rev", "跳空回歸（低−高）", 0.493)):
            v = s[col].dropna()
            # n 很小時 t 值毫無意義：兩個相近的值會讓標準誤趨近 0，
            # 曾印出 t=−17.14（n=2）這種數字。門檻設 10 天。
            if len(v) < 10:
                print(f"  {lab:<20} {v.mean():+.4f}%（n={len(v)}，"
                      f"樣本不足，不報 t）")
                continue
            t = v.mean() / (v.std(ddof=1) / np.sqrt(len(v)))
            print(f"  {lab:<20} {v.mean():+.4f}%/日 · t={t:+.2f} · 為正 "
                  f"{(v > 0).mean() * 100:.0f}% · 歷史期望 {exp:+.3f}%")
        if regime == "forward":
            n = len(s)
            need = max(0, 60 - n)
            print(f"  → 距最低判斷樣本（60 日）還差 {need} 日"
                  if need else "  → 樣本已達 60 日，可做初步判斷")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", default=None, help="報酬日（預設：DB 最新有價格的交易日）")
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--summary", action="store_true", help="只印累積結果，不記錄新日期")
    ap.add_argument("--force", action="store_true",
                    help="已記錄過的日期也重算覆蓋（資料補全後重錄會改變 n，"
                         "屬於覆蓋率變化而非挑選，但要留意 n 欄的跳動）")
    args = ap.parse_args()

    df = load_track()
    if not args.summary:
        d = args.date or connect_ro().execute(
            "SELECT MAX(trade_date) FROM stock_daily_bars WHERE source='finmind'").fetchone()[0]
        if not df.empty and d in set(df.return_date) and not args.force:
            print(f"{d} 已記錄過，略過（要重算加 --force）")
        else:
            row = record(d)
            if row is None:
                print(f"{d} 資料不足，未記錄（籌碼或價格尚未進 DB？）")
            else:
                upsert(row)
                df = load_track()
                args.out.parent.mkdir(parents=True, exist_ok=True)
                df.to_csv(args.out, index=False)     # 人眼檢視用的匯出
                print(f"已記錄 {d}（{row['regime']}）：多空價差 收→收 {row['spread_cc']:+.4f}% · "
                      f"開→收 {row['spread_oc']:+.4f}% · 標的 {row['n']} 檔")
    if not df.empty:
        summary(df)
    return 0


if __name__ == "__main__":
    sys.exit(main())
