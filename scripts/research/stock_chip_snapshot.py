#!/usr/bin/env python3
"""單檔個股的當日籌碼全貌 —— 交易所端點即時取數 + 15 年歷史分位。

盤後各表發布時間不同（實測：T86 三大法人與 t13sa710 借券成交約 17:00 前後、
TWT93U 借券賣出餘額與 MI_MARGN 融資融券約 21:00），本工具對尚未發布的欄位
顯示「未發布」而不是靜默留空或誤用前一日數值。

**評分口徑（v4 連續版）**：五個訊號先轉成 z 型變數（±2.5），做硬收縮
``s = z if |z|>=0.1 else 0``，相加後取**當日全市場分位**切三帶（最低/最高 20%）。

| | 訊號 | 尺度基準 |
|---|---|---|
| z1 | Δ借券賣出餘額 相對自身近 60 日的 z 值 | 個股自適應 |
| zp | 佔股本近 243 日分位 →(分位−0.5)×4 | 個股自適應 |
| zu | Δ券源使用率 相對自身近 60 日的 z 值 | 個股自適應 |
| zf | 借券費率 自身近 60 筆分位；**當日無成交 → 0** | 個股自適應 |
| z6 | 分點(買超家數−賣超家數)÷家數 的**當日橫斷面**分位 | 橫斷面 |

三個設計依據（皆實測）：保留連續值不壓成 ±1（t 8.02→10.48）；hard 收縮優於
soft（soft 會把極端值也減掉）；死區只用 0.1（k∈[0,0.2] 平坦、k>0.3 變差）。

**已剔除**「融券餘額變化」——2005-2012 是強反指標但之後衰減，15 年 t=+1.75。

⚠️ **效果量與時間結構**：OOS 1,001 日（2022-07~2026-08）多空價差 0.1670%/日、
t=10.48。但**優勢集中在近兩年**（分年逐日差 t：2022 0.53／2023 1.26／2024 1.54／
2025 2.68／2026 5.87）。本工具採用「近期習慣為主」的解釋，以 2025-2026 的
水準為準。**否證檢定**：2026-09 之後若差 t 掉回 1.5 以下，該解釋即被推翻。
橫斷面已驗：隨機切半 t=3.84/4.29、市值三層 t=4.74/3.40/2.42 全部成立。

⚠️ **仍不可當進場訊號**：即使用 2026 的 0.392%/日，換手成本 1.17%/日 仍是 3 倍、
個股單日離散 σ≈3% 是 8 倍，看空帶隔日仍約 43% 上漲。

用法::

    PYTHONPATH=src .venv/bin/python scripts/research/stock_chip_snapshot.py 9914
    PYTHONPATH=src .venv/bin/python scripts/research/stock_chip_snapshot.py 2492 --date 2026-08-20
"""
from __future__ import annotations

import argparse
import http.client
import json
import ssl
import sys
import urllib.error
import urllib.request
from datetime import date, datetime

import numpy as np
import pandas as pd

from stock_db import connect_ro

UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.twse.com.tw/"}
_CTX = ssl.create_default_context()
_CTX.verify_flags &= ~ssl.VERIFY_X509_STRICT
_RETRY = (urllib.error.URLError, TimeoutError, json.JSONDecodeError,
          http.client.IncompleteRead, http.client.HTTPException, ConnectionError, OSError)


def _get(url: str):
    for i in range(3):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=45, context=_CTX) as r:
                raw = r.read()
            return json.loads(raw) if raw.strip() else None
        except _RETRY:
            if i == 2:
                return None
    return None


def _num(x):
    t = str(x or "").replace(",", "").strip()
    try:
        return float(t)
    except ValueError:
        return None


def _find(payload, sid: str, key: str, col0: int = 0):
    """在 payload（頂層或 tables）裡找出代號等於 sid 的那一列。"""
    if not payload:
        return None, None
    tabs = (payload.get("tables") or []) or [payload]
    for t in tabs:
        f = [str(x).strip() for x in (t.get("fields") or [])]
        if key and not any(key in x for x in f):
            continue
        for r in t.get("data") or []:
            if str(r[col0]).strip() == sid:
                return r, f
    return None, None


def fetch_day(sid: str, d: str) -> dict:
    ymd = d.replace("-", "")
    slash = d.replace("-", "/")
    out: dict = {}

    r, f = _find(_get(f"https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date={ymd}&type=ALLBUT0999&response=json"), sid, "收盤價")
    if r:
        i = {n: k for k, n in enumerate(f)}
        out["price"] = {k: _num(r[i[v]]) for k, v in
                        (("open", "開盤價"), ("high", "最高價"), ("low", "最低價"),
                         ("close", "收盤價"), ("volume", "成交股數"))}
    if "price" not in out:
        r, f = _find(_get(f"https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes?date={slash}&type=EW&response=json"), sid, "收盤")
        if r:
            i = {n: k for k, n in enumerate(f)}
            out["price"] = {"open": _num(r[i["開盤"]]), "high": _num(r[i["最高"]]),
                            "low": _num(r[i["最低"]]), "close": _num(r[i["收盤"]]),
                            "volume": _num(r[i["成交股數"]])}
            out["otc"] = True

    r, _ = _find(_get(f"https://www.twse.com.tw/rwd/zh/marginTrading/TWT93U?date={ymd}&response=json"), sid, "")
    if r and len(r) >= 14:
        out["sbl"] = {"bal": _num(r[12]) or 0, "sell": _num(r[9]) or 0,
                      "ret": _num(r[10]) or 0, "lim": _num(r[13]) or 0,
                      "short_bal": _num(r[6]) or 0, "short_lim": _num(r[7]) or 0}

    r, _ = _find(_get(f"https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN?date={ymd}&selectType=ALL&response=json"), sid, "次一營業日限額")
    if r and len(r) >= 14:
        out["margin"] = {"bal": _num(r[6]), "prev": _num(r[5]),
                         "short": _num(r[12]), "short_prev": _num(r[11])}

    p = _get(f"https://www.twse.com.tw/rwd/zh/fund/T86?date={ymd}&selectType=ALL&response=json")
    r, f = _find(p, sid, "三大法人買賣超股數")
    if r:
        i = {n: k for k, n in enumerate(f)}
        out["inst"] = {
            "foreign": (_num(r[i["外陸資買賣超股數(不含外資自營商)"]]) or 0)
                       + (_num(r[i["外資自營商買賣超股數"]]) or 0),
            "trust": _num(r[i["投信買賣超股數"]]),
            "dealer": _num(r[i["自營商買賣超股數"]]),
            "total": _num(r[i["三大法人買賣超股數"]])}

    p = _get(f"https://www.twse.com.tw/exchangeReport/TWTB4U?response=json&date={ymd}&selectType=All")
    r, _ = _find(p, sid, "當日沖銷交易成交股數")
    if r:
        out["daytrade"] = _num(r[3])

    p = _get(f"https://www.twse.com.tw/rwd/zh/lending/t13sa710?startDate={ymd}&endDate={ymd}&response=json")
    rows = [x for x in ((p or {}).get("data") or []) if str(x[1]).split()[0] == sid]
    if rows:
        q = sum(_num(x[3]) or 0 for x in rows)
        w = sum((_num(x[3]) or 0) * (_num(x[4]) or 0) for x in rows)
        out["fee"] = {"n": len(rows), "qty": q, "vw": (w / q) if q else None,
                      "lo": min(_num(x[4]) or 0 for x in rows),
                      "hi": max(_num(x[4]) or 0 for x in rows)}
    return out


def market_scores(d: str, hist_days: int = 320) -> pd.DataFrame | None:
    """算出當日**全市場**的 v4 連續分數（需要全市場才能取分位切帶）。

    從 DB 取近 ``hist_days`` 個交易日的借券／費率，算個股自適應的滾動 z；
    分點則直接查當日全市場。當日資料若尚未進 DB，由呼叫端先補上。
    """
    c = connect_ro()
    hist = pd.read_sql_query(
        """SELECT s.stock_id, s.trade_date, s.sbl_balance, s.sbl_next_limit,
                  s.short_limit, f.fee_rate_vw
             FROM stock_short_interest_daily s
             LEFT JOIN (SELECT stock_id, trade_date, fee_rate_vw FROM stock_sbl_fee_daily
                         WHERE deal_type='ALL') f
               ON f.stock_id=s.stock_id AND f.trade_date=s.trade_date
            WHERE s.trade_date <= ? AND s.trade_date >= date(?, '-500 day')""",
        c, params=(d, d))
    if hist.empty:
        return None
    h = hist.sort_values(["stock_id", "trade_date"]).copy()
    h["shares"] = (h.short_limit * 4).replace(0, np.nan)
    h["sbl_pct"] = h.sbl_balance / h.shares
    h["util"] = h.sbl_balance / (h.sbl_balance + h.sbl_next_limit)
    g = h.groupby("stock_id", group_keys=False)
    h["d_sbl"] = g.sbl_balance.diff()
    h["d_util"] = g.util.diff()

    def zself(col, win=60):
        mu = g[col].transform(lambda x: x.rolling(win, min_periods=30).mean())
        sd = g[col].transform(lambda x: x.rolling(win, min_periods=30).std())
        return (h[col] - mu) / sd.replace(0, np.nan)

    h["z1"] = zself("d_sbl")
    h["zu"] = zself("d_util")
    h["zp"] = ((g.sbl_pct.transform(lambda x: x.rolling(243, min_periods=60).rank(pct=True))
                - 0.5) * 4)
    h["zf"] = ((g.fee_rate_vw.transform(lambda x: x.rolling(60, min_periods=10).rank(pct=True))
                - 0.5) * 4)
    cur = h[h.trade_date == d].copy()
    if cur.empty:
        return None

    br = pd.read_sql_query(
        """SELECT stock_id,
                  SUM(CASE WHEN net>0 THEN 1 ELSE 0 END) nb,
                  SUM(CASE WHEN net<0 THEN 1 ELSE 0 END) ns, COUNT(*) n
             FROM stock_broker_branch_daily
            WHERE trade_date=? AND net IS NOT NULL AND net<>0
            GROUP BY stock_id""", c, params=(d,))
    if not br.empty:
        br["brdiff"] = (br.nb - br.ns) / br.n
        br["z6"] = (br.brdiff.rank(pct=True) - 0.5) * 4
        cur = cur.merge(br[["stock_id", "z6", "nb", "ns", "n"]], on="stock_id", how="left")
    else:
        cur["z6"] = np.nan

    for z in ("z1", "zp", "zu", "zf", "z6"):
        cur[z] = cur[z].fillna(0).clip(-2.5, 2.5)
        cur[f"s_{z}"] = np.where(cur[z].abs() >= 0.1, cur[z], 0.0)
    cur["score"] = cur[[f"s_{z}" for z in ("z1", "zp", "zu", "zf", "z6")]].sum(axis=1)
    cur["pctile"] = cur.score.rank(pct=True) * 100
    return cur


def branch_score(sid: str, d: str) -> tuple[float, dict] | tuple[None, None]:
    """當日分點買賣家數差 → 橫斷面五分位 → S6（正=偏空）。需全市場才能排序。"""
    c = connect_ro()
    q = """SELECT stock_id,
                  SUM(CASE WHEN net>0 THEN 1 ELSE 0 END) nb,
                  SUM(CASE WHEN net<0 THEN 1 ELSE 0 END) ns,
                  COUNT(*) n
             FROM stock_broker_branch_daily
            WHERE trade_date=? AND net IS NOT NULL AND net<>0
            GROUP BY stock_id"""
    df = pd.read_sql_query(q, c, params=(d,))
    if df.empty or sid not in set(df.stock_id):
        return None, None
    df["brdiff"] = (df.nb - df.ns) / df.n
    df["q"] = pd.qcut(df.brdiff.rank(method="first"), 5, labels=False, duplicates="drop")
    row = df[df.stock_id == sid].iloc[0]
    s6 = {0: -1, 4: 1}.get(int(row.q), 0)
    return s6, {"買超家數": int(row.nb), "賣超家數": int(row.ns),
                "分點家數": int(row.n), "家數差比": round(row.brdiff, 3),
                "橫斷面分位": f"Q{int(row.q)+1}/5"}


def history(sid: str, before: str | None = None) -> pd.DataFrame:
    """該股的歷史序列。``before`` 給定時**排除該日**——否則「Δ vs 前一日」會拿
    當天自己當前一日，算出恆為 0 的假變動（當日資料一旦進 DB 就會發生）。"""
    c = connect_ro()
    df = pd.read_sql_query("""
        SELECT s.trade_date, p.close, p.volume/1000.0 vol,
               s.sbl_balance/1000.0 bal, s.sbl_next_limit/1000.0 lim,
               s.short_limit/1000.0*4 shares, f.fee_rate_vw fee
          FROM stock_short_interest_daily s
          LEFT JOIN stock_daily_bars p ON p.stock_id=s.stock_id
               AND p.trade_date=s.trade_date AND p.source='finmind'
          LEFT JOIN (SELECT stock_id,trade_date,fee_rate_vw FROM stock_sbl_fee_daily
                      WHERE deal_type='ALL') f
               ON f.stock_id=s.stock_id AND f.trade_date=s.trade_date
         WHERE s.stock_id=? ORDER BY s.trade_date""", c, params=(sid,))
    return df[df.trade_date < before] if before else df


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("stock_id")
    ap.add_argument("--date", default=date.today().isoformat())
    args = ap.parse_args()
    sid, d = args.stock_id, args.date
    print(f"=== {sid} · {d} · 取數時間 {datetime.now():%H:%M} ===\n")

    s = fetch_day(sid, d)
    if not s.get("price"):
        print("當日無收盤資料（非交易日或尚未發布）")
        return 0
    p = s["price"]
    print(f"價格  開 {p['open']} 高 {p['high']} 低 {p['low']} 收 {p['close']} "
          f"· 量 {(p['volume'] or 0)/1000:,.0f} 張" + ("  [上櫃]" if s.get("otc") else ""))

    h = history(sid, before=d)
    K = 1000.0
    if s.get("sbl"):
        b = s["sbl"]
        shares = (b["short_lim"] or 0) * 4 / K
        bal, lim = b["bal"] / K, b["lim"] / K
        prev = h.bal.iloc[-1] if len(h) else np.nan
        util = bal / (bal + lim) * 100 if (bal + lim) else np.nan
        prev_util = (h.bal.iloc[-1] / (h.bal.iloc[-1] + h.lim.iloc[-1]) * 100) if len(h) else np.nan
        pct = ((h.bal / h.shares).tail(243) < (bal / shares)).mean() * 100 if shares else np.nan
        dtc = bal / h.vol.tail(5).mean() if len(h) else np.nan
        print(f"借券  餘額 {bal:,.0f} 張（Δ{bal-prev:+,.0f}）· 賣出 {b['sell']/K:,.0f} "
              f"／還券 {b['ret']/K:,.0f}")
        print(f"      佔股本 {bal/shares*100:.2f}%（近一年分位 {pct:.0f}%）· "
              f"券源使用率 {util:.1f}%（Δ{util-prev_util:+.1f}pp）· DTC {dtc:.1f} 天")
        print(f"融券  {b['short_bal']/K:,.0f} 張")
    else:
        print("借券  未發布（TWT93U 約 21:00）")

    if s.get("margin"):
        m = s["margin"]
        print(f"融資  {m['bal']:,.0f} 張（Δ{m['bal']-m['prev']:+,.0f}）")
    else:
        print("融資  未發布（MI_MARGN 約 21:00）")

    if s.get("inst"):
        i = s["inst"]
        print(f"法人  外資 {i['foreign']/K:+,.0f} · 投信 {(i['trust'] or 0)/K:+,.0f} · "
              f"自營 {(i['dealer'] or 0)/K:+,.0f} · 合計 {(i['total'] or 0)/K:+,.0f} 張")
    if s.get("daytrade") and p.get("volume"):
        print(f"當沖  {s['daytrade']/K:,.0f} 張 = {s['daytrade']/p['volume']*100:.1f}%")
    if s.get("fee"):
        f_ = s["fee"]
        med = h.fee.dropna().tail(20).median() if len(h) else np.nan
        print(f"費率  量加權 {f_['vw']:.3f}%（{f_['n']} 筆 / {f_['qty']:,.0f} 張 · "
              f"區間 {f_['lo']}~{f_['hi']}）· 近 20 筆中位 {med:.2f}%")
    else:
        print("費率  當日無借券成交")

    s6, binfo = branch_score(sid, d)
    if binfo:
        print(f"分點  買超 {binfo['買超家數']} 家 / 賣超 {binfo['賣超家數']} 家 "
              f"（共 {binfo['分點家數']} 家）· 家數差比 {binfo['家數差比']:+.3f} "
              f"· {binfo['橫斷面分位']}")
    else:
        print("分點  未發布或無資料（2021-06 起才有）")

    # ── v4 連續評分（需全市場才能取分位）──
    mk = market_scores(d)
    if mk is None or sid not in set(mk.stock_id):
        print("\n評分  當日借券資料未進 DB，無法計分（DB 通常隔日才有）")
        return 0
    r = mk[mk.stock_id == sid].iloc[0]
    band = ("看空帶" if r.pctile >= 80 else ("看多帶" if r.pctile <= 20 else "中性帶"))
    print(f"\n評分（v4 連續 · 全市場 {len(mk):,} 檔）")
    print(f"  z1 Δ借券 {r.s_z1:+.2f} · zp 佔股本 {r.s_zp:+.2f} · zu Δ使用率 {r.s_zu:+.2f} "
          f"· zf 費率 {r.s_zf:+.2f} · z6 分點 {r.s_z6:+.2f}")
    print(f"  總分 {r.score:+.2f} → 當日全市場分位 {r.pctile:.0f}% → **{band}**")
    print("  ⚠️ 只描述部位結構。看空帶隔日仍約 43% 上漲；2026 年價差 0.392%/日，"
          "而換手成本 1.17%/日 是它的 3 倍")
    return 0


if __name__ == "__main__":
    sys.exit(main())
