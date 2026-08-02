"""自算台指VIX(VIXTWN, source='computed')回填 market_vix_daily。

背景
----
FinMind 的 ``TaiwanOptionVix``(官方已算好的 VIX)只從 2026-03 起,無法回測多年。
本腳本改用 FinMind ``TaiwanOptionDaily``(TXO 原始選擇權,含每個履約價的官方日結算價
``settlement_price``,回溯到 2011 年)自行以 CBOE/TAIFEX 公式重建台指VIX。

驗證(2026-03~07 與官方 ``symbol='VIXTWN',source='finmind'`` 重疊 105 天):
相關 0.989、平均差 -0.16 點、樣本外校準後相關 0.994、逐日誤差 ~0.3 點。
關鍵:用官方 ``settlement_price``(每個履約價都有,等同買賣中點)而非最後成交價,
並納入週選(W=第n週三 / F=第n週五)挑最貼 30 天的近/次期。

寫入 ``market_vix_daily(symbol='VIXTWN', source='computed')``,與官方 finmind 那份並存區分。

方法
----
每個評價日 D:
1. 取當日 position 盤所有 TXO 合約,以 settlement_price 為價格。
2. 依代碼推到期日(YYYYMM=第3週三月選;W n=第n週三;F n=第n週五),
   選近期(距到期 7~30 天最大者)與次期(>30 天最小者)。
3. 各期以變異數複製公式算 σ²=(2/T)Σ(ΔK/K²)e^{RT}Q(K) − (1/T)(F/K0−1)²,
   F 由買賣權平價(|C−P|最小履約價)推得,K0=F 下方最近履約價,Q=OTM 結算價。
4. 線性插補近/次期到 30 天:VIX=100√(插補變異數×365/30)。

用法
----
    PYTHONPATH=src python src/sync_vixtwn_computed.py --start 2011-01 --end 2026-07   # 回填
    PYTHONPATH=src python src/sync_vixtwn_computed.py --start 2026-07 --end 2026-07 --dry-run
"""
from __future__ import annotations
import argparse, math, datetime as dt, sqlite3, os, json, time, sys
from collections import defaultdict

from finmind_client import fetch_finmind_json
from stock_db import connect
from stock_db.util import utc_now_iso

R = 0.015
CACHE = os.path.expanduser("~/goldenstocks-data/data/_txo_cache")
_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS market_vix_daily(
  symbol TEXT NOT NULL, date TEXT NOT NULL,
  open REAL, high REAL, low REAL, close REAL NOT NULL,
  n_ticks INTEGER, source TEXT NOT NULL DEFAULT 'finmind',
  synced_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY(symbol,date,source));
"""
_UPSERT = """
INSERT INTO market_vix_daily (symbol,date,open,high,low,close,n_ticks,source,synced_at)
VALUES (:symbol,:date,:open,:high,:low,:close,:n_ticks,:source,:synced_at)
ON CONFLICT(symbol,date,source) DO UPDATE SET
  close=excluded.close, synced_at=excluded.synced_at
"""


def nth_weekday(y, m, weekday, n):
    d = dt.date(y, m, 1)
    d += dt.timedelta((weekday - d.weekday()) % 7)
    d += dt.timedelta(7 * (n - 1))
    return d if d.month == m else None


def exp_date(code: str):
    if len(code) < 6 or not code[:6].isdigit():
        return None
    y, m, rest = int(code[:4]), int(code[4:6]), code[6:]
    if rest == "":
        return nth_weekday(y, m, 2, 3)
    kind = rest[0]
    try:
        n = int(rest[1:])
    except ValueError:
        return None
    if kind == "W":
        return nth_weekday(y, m, 2, n)
    if kind == "F":
        return nth_weekday(y, m, 4, n)
    return None


def _term_sigma2(chain: dict, T: float):
    Ks = sorted(k for k, v in chain.items() if v.get("call") and v.get("put"))
    if len(Ks) < 5:
        return None
    kmin = min(Ks, key=lambda k: abs(chain[k]["call"] - chain[k]["put"]))
    F = kmin + math.exp(R * T) * (chain[kmin]["call"] - chain[kmin]["put"])
    below = [k for k in Ks if k <= F]
    if not below:
        return None
    K0 = max(below)

    def Q(k):
        if k < K0:
            return chain[k].get("put")
        if k > K0:
            return chain[k].get("call")
        return (chain[k]["call"] + chain[k]["put"]) / 2.0

    allK = sorted(k for k in Ks
                  if (k < K0 and chain[k].get("put")) or (k > K0 and chain[k].get("call")) or k == K0)
    if len(allK) < 5:
        return None
    s = 0.0
    for i, k in enumerate(allK):
        if i == 0:
            dK = allK[1] - allK[0]
        elif i == len(allK) - 1:
            dK = allK[-1] - allK[-2]
        else:
            dK = (allK[i + 1] - allK[i - 1]) / 2.0
        q = Q(k)
        if not q or q <= 0:
            continue
        s += (dK / (k * k)) * math.exp(R * T) * q
    sigma2 = (2.0 / T) * s - (1.0 / T) * ((F / K0 - 1.0) ** 2)
    return sigma2 if sigma2 > 0 else None


def compute_vix_for_day(rows: list[dict], date: str):
    """rows = 該日 position 盤 TXO 逐筆。回傳 VIX 或 None。"""
    data = [r for r in rows if r.get("trading_session") == "position"]
    if not data:
        data = rows
    d0 = dt.date.fromisoformat(date)
    exps = []
    for c in set(r["contract_date"] for r in data):
        ed = exp_date(c)
        if ed:
            n = (ed - d0).days
            if n >= 1:
                exps.append((c, n))
    exps.sort(key=lambda x: x[1])
    below = [(c, n) for c, n in exps if 7 <= n <= 30]
    near = below[-1] if below else next(((c, n) for c, n in exps if n >= 7), None)
    if near is None:
        return None
    nxt = next(((c, n) for c, n in exps if n > near[1]), None)
    if nxt is None:
        return None
    out = []
    for code, ndays in (near, nxt):
        T = ndays / 365.0
        chain: dict = {}
        for r in data:
            if r["contract_date"] != code:
                continue
            px = r.get("settlement_price") or r.get("close")
            if not px or px <= 0:
                continue
            chain.setdefault(r["strike_price"], {})[r["call_put"]] = px
        s2 = _term_sigma2(chain, T)
        if s2 is None:
            return None
        out.append((T, s2, ndays))
    (T1, s1, n1), (T2, s2, n2) = out
    w1 = (n2 - 30) / (n2 - n1)
    w2 = (30 - n1) / (n2 - n1)
    var = (T1 * s1 * w1 + T2 * s2 * w2) * (365 / 30)
    return round(100 * math.sqrt(var), 2) if var > 0 else None


def fetch_month(ym: str) -> list[dict]:
    """抓一整月 TXO(一次 API call),cache 到本地。ym='YYYY-MM'。"""
    os.makedirs(CACHE, exist_ok=True)
    f = os.path.join(CACHE, f"txo_{ym}.json")
    if os.path.exists(f):
        return json.load(open(f))
    y, m = int(ym[:4]), int(ym[5:7])
    start = f"{ym}-01"
    end = (dt.date(y, m, 28) + dt.timedelta(7)).replace(day=1) - dt.timedelta(1)
    js = fetch_finmind_json({"dataset": "TaiwanOptionDaily", "data_id": "TXO",
                             "start_date": start, "end_date": end.isoformat()})
    data = [r for r in js.get("data", []) if r.get("trading_session") == "position"]
    json.dump(data, open(f, "w"))
    time.sleep(0.4)
    return data


def month_range(start_ym: str, end_ym: str):
    y, m = int(start_ym[:4]), int(start_ym[5:7])
    ey, em = int(end_ym[:4]), int(end_ym[5:7])
    while (y, m) <= (ey, em):
        yield f"{y:04d}-{m:02d}"
        m += 1
        if m > 12:
            y += 1; m = 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2011-01")
    ap.add_argument("--end", default=dt.date.today().strftime("%Y-%m"))
    ap.add_argument("--dry-run", action="store_true")
    A = ap.parse_args()

    conn = connect() if not A.dry_run else None
    if conn:
        conn.executescript(_CREATE_SQL); conn.commit()
    synced = utc_now_iso()
    total = 0
    for ym in month_range(A.start, A.end):
        try:
            rows = fetch_month(ym)
        except Exception as e:
            print(f"{ym} FETCH_ERR {str(e)[:80]}", flush=True); continue
        by_day = defaultdict(list)
        for r in rows:
            by_day[r["date"]].append(r)
        payload = []
        for d in sorted(by_day):
            try:
                v = compute_vix_for_day(by_day[d], d)
            except Exception:
                v = None
            if v and 3 < v < 200:
                payload.append({"symbol": "VIXTWN", "date": d, "open": None, "high": None,
                                "low": None, "close": v, "n_ticks": None,
                                "source": "computed", "synced_at": synced})
        if conn and payload:
            conn.executemany(_UPSERT, payload); conn.commit()
        total += len(payload)
        print(f"{ym}: {len(payload)} 天  (累計 {total})", flush=True)
    print(f"DONE 共寫入 {total} 天 VIXTWN(computed)")


if __name__ == "__main__":
    main()
