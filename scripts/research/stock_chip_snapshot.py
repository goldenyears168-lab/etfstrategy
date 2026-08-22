#!/usr/bin/env python3
"""單檔個股的當日籌碼全貌 —— 交易所端點即時取數 + 15 年歷史分位。

盤後各表發布時間不同（實測：T86 三大法人與 t13sa710 借券成交約 17:00 前後、
TWT93U 借券賣出餘額與 MI_MARGN 融資融券約 21:00），本工具對尚未發布的欄位
顯示「未發布」而不是靜默留空或誤用前一日數值。

**評分口徑**：等權**五**訊號，正分＝偏空——
S1 Δ借券賣出餘額／S2 佔股本近一年分位／S3 券源使用率變化／
S5 借券費率 vs 近 20 筆中位／**S6 分點買賣家數差**。
已剔除「融券餘額變化」——它在 2005-2012 是強反指標（t +2.4~+7.3）但之後
衰減，15 年 t=+1.75 不顯著、逐年正負反覆（見 config/research.yaml 的
chip-signal-daily-horizon）。

**S6 分點**（``(買超家數−賣超家數)÷分點家數`` 的當日橫斷面五分位，最高分位判
偏空）是唯一通過 walk-forward 的增量：OOS 1,001 日 / 17 期，價差
0.1024%→0.1159%/日、t 8.02→8.75，增量逐日差 t=+2.37。與四訊號相關僅 0.096。
方向反直覺——買超**家數**多代表散戶分散進場，隔日偏弱；所有「主力買超金額」
類特徵反而全部不顯著（t 0.24~1.64）。

⚠️ **評分只描述部位結構，不預測隔日方向。** 15 年 190 萬個 stock-day 實測：
看空帶隔日超額 −0.04%、命中率 56.05%（無條件基準 55.38%，優勢 0.7 個百分點），
而個股單日離散 σ≈3%。不要拿它當進場訊號。

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


def history(sid: str) -> pd.DataFrame:
    c = connect_ro()
    return pd.read_sql_query("""
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

    h = history(sid)
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

    # 評分（缺欄位就標明無法計分）
    if s.get("sbl") and len(h):
        b = s["sbl"]
        shares = (b["short_lim"] or 0) * 4 / K
        bal, lim = b["bal"] / K, b["lim"] / K
        util = bal / (bal + lim) if (bal + lim) else np.nan
        prev_util = h.bal.iloc[-1] / (h.bal.iloc[-1] + h.lim.iloc[-1])
        pr = ((h.bal / h.shares).tail(243) < (bal / shares)).mean()
        med = h.fee.dropna().tail(20).median()
        S1 = 1 if bal > h.bal.iloc[-1] else -1
        S2 = 1 if pr >= 0.8 else (-1 if pr <= 0.2 else 0)
        S3 = 1 if util > prev_util else -1
        S5 = (1 if (s.get("fee") and med == med and s["fee"]["vw"] > med) else -1)
        s6v = int(s6) if s6 is not None else 0
        net = S1 + S2 + S3 + S5 + s6v
        band = "看空帶" if net >= 2 else ("看多帶" if net <= -2 else "中性帶")
        s6txt = f" · S6 {s6v:+d}" if s6 is not None else " · S6 無資料"
        print(f"\n評分  S1 {S1:+d} · S2 {S2:+d} · S3 {S3:+d} · S5 {S5:+d}{s6txt} "
              f"→ 淨 {net:+d} / ±{5 if s6 is not None else 4} → **{band}**")
        print("      ⚠️ 只描述部位結構。15 年實測看空帶隔日超額 −0.04%、"
              "命中率優勢 0.7pp，個股離散 σ≈3%")
    else:
        print("\n評分  借券資料未發布，無法計分")
    return 0


if __name__ == "__main__":
    sys.exit(main())
