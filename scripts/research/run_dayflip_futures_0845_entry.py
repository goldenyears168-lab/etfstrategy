#!/usr/bin/env python3
"""個股期貨 08:45 進場 vs 現貨 09:00 進場（research only）.

期交所股票期貨日盤 08:45–13:45，比現貨 09:00 早 15 分鐘（DB 內 TX1! 分鐘資料
最早棒為 08:45:00，佐證此時段）。因此：

  · 訊號若定義在「現貨 09:00 跳空」，就 **不能** 在期貨 08:45 成交 → look-ahead。
  · 純期貨口徑：訊號＝期貨 08:45 開盤相對 T0 收盤漲幅，進場即在 08:45，PIT 合法。

  PYTHONPATH=src .venv/bin/python scripts/research/run_dayflip_futures_0845_entry.py
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path
from statistics import mean, median, pstdev

import stock_db
from finmind_client import fetch_finmind

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / "reports/research/branch-footprint-screen"
OUT = BASE / "dayflip_gapup_short"
CACHE = OUT / "futures_daily_cache.json"
TOP_N = 22
COST_FUT = 0.0005
COST_EQ = 0.003
BETA = 1.5


def log(m: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


def load_futures(sids: list[str], futmap: dict) -> dict:
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    for sid in sids:
        if sid in cache:
            continue
        code = futmap[sid]
        fid = code + "F" if len(code) == 2 else code
        try:
            rows = fetch_finmind("TaiwanFuturesDaily", fid,
                                 date(2024, 7, 1), date(2026, 7, 20), timeout=180)
        except Exception as ex:  # noqa: BLE001
            log(f"  {sid} {fid} ERR {str(ex)[:60]}")
            cache[sid] = {}
            continue
        byd: dict[str, list] = defaultdict(list)
        for r in rows:
            cd = str(r.get("contract_date", ""))
            if "/" in cd or r.get("trading_session") != "position":
                continue
            if float(r.get("open") or 0) <= 0:
                continue
            byd[str(r["date"])].append(r)
        out = {}
        for d, rs in byd.items():
            r = max(rs, key=lambda x: float(x.get("volume") or 0))
            out[d] = [float(r["open"]), float(r["close"]),
                      float(r.get("max") or 0), float(r.get("min") or 0),
                      float(r.get("volume") or 0)]
        cache[sid] = out
        log(f"  {sid} {fid}: {len(out)} 日")
        time.sleep(0.3)
    CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    return cache


def daystats(rows: list[tuple[str, float]], min_n: int = 30) -> dict:
    if len(rows) < min_n:
        return dict(n=len(rows), thin=True)
    byd = defaultdict(list)
    for d, r in rows:
        byd[d].append(r)
    dm = [mean(v) for v in byd.values()]
    sd = pstdev(dm) or 1e-9
    return dict(
        n=len(rows), days=len(dm),
        day_mean=round(100 * mean(dm), 3),
        day_med=round(100 * median(dm), 3),
        day_win=round(100 * mean([x > 0 for x in dm]), 1),
        t=round(mean(dm) / (sd / len(dm) ** 0.5), 2),
    )


def main() -> None:
    ev = json.loads((OUT / "events.json").read_text())
    futmap = json.loads((OUT / "stock_futures_universe.json").read_text())["map"]
    mega = set(json.loads((BASE / "ab58_xMega_copytrade/mega_blacklist_v1.json")
                          .read_text())["symbols"])

    # 事件宇宙：個股期貨標的、非權值股
    pool = [e for e in ev if e["sid"] in futmap and e["sid"] not in mega]
    cnt = Counter(e["sid"] for e in pool if e["gap"] >= 0.05)
    top = [s for s, _ in cnt.most_common(TOP_N)]
    log(f"取前 {len(top)} 檔（gap>=5% 訊號數排序）")
    fut = load_futures(top, futmap)
    good = [s for s in top if len(fut.get(s) or {}) >= 400]
    log(f"期貨資料覆蓋 >=400 日：{len(good)} 檔 {good}")

    con = sqlite3.connect(f"file:{stock_db.DEFAULT_DB_PATH}?mode=ro", uri=True)
    ph = ",".join("?" * len(good))
    cash: dict[tuple[str, str], tuple] = {}
    for sid, d, o, c in con.execute(
        f"SELECT stock_id,trade_date,open,close FROM stock_daily_bars "
        f"WHERE source='finmind' AND stock_id IN ({ph}) "
        f"AND trade_date BETWEEN '2024-06-01' AND '2026-07-20' AND close>0", good
    ):
        cash[(str(sid), str(d))] = (float(o), float(c))
    ixrow: dict[str, tuple[float, float]] = {}
    best: dict[str, int] = {}
    rank = {"yahoo": 0, "tej": 1, "finmind": 2}
    for d, o, c, src in con.execute(
        "SELECT date,open,close,source FROM daily_bars WHERE code='IX0001' "
        "AND date BETWEEN '2024-05-01' AND '2026-07-20' AND open>0 AND close>0"
    ):
        r = rank.get(str(src), 9)
        if str(d) not in best or r < best[str(d)]:
            best[str(d)] = r
            ixrow[str(d)] = (float(o), float(c))
    con.close()

    # 每檔股票的交易日序列（用來取 T0 的前一交易日期貨收盤）
    days_of = {s: sorted(v) for s, v in
               defaultdict(dict, {s: {k: 1 for k in fut[s]} for s in good}).items()}
    idx_of = {s: {d: i for i, d in enumerate(ds)} for s, ds in days_of.items()}

    recs = []
    for e in pool:
        sid, d0, d1 = e["sid"], e["date"], e["d1"]
        if sid not in good:
            continue
        f1 = (fut[sid] or {}).get(d1)
        f0 = (fut[sid] or {}).get(d0)
        c1 = cash.get((sid, d1))
        c0 = cash.get((sid, d0))
        if not f1 or not f0 or not c1 or not c0 or d1 not in ixrow:
            continue
        fo, fc, fh, fl, fv = f1
        if fv < 50:
            continue
        ix_o, ix_c = ixrow[d1]
        recs.append(dict(
            sid=sid, date=d0, d1=d1, fv=fv,
            fut_gap_vs_fut=fo / f0[1] - 1,      # 期貨08:45 vs 前一日期貨收盤
            fut_gap_vs_cash=fo / c0[1] - 1,     # 期貨08:45 vs 前一日現貨收盤
            cash_gap=e["gap"],                  # 現貨09:00 vs 前一日現貨收盤
            short_fut=-(fc / fo - 1),           # 空期貨 08:45 → 13:45
            short_cash=-(c1[1] / c1[0] - 1),    # 空現貨 09:00 → 13:30
            ix=ix_c / ix_o - 1,
            amt_share=e["amt_share"],
        ))
    log(f"配對 {len(recs)} 筆 / {len({r['sid'] for r in recs})} 檔")

    res: dict = {}

    # 1. 期貨 08:45 跳空 與 現貨 09:00 跳空 的關係
    both = [r for r in recs]
    res["gap_relation"] = dict(
        n=len(both),
        fut_gap_med=round(100 * median([r["fut_gap_vs_cash"] for r in both]), 3),
        cash_gap_med=round(100 * median([r["cash_gap"] for r in both]), 3),
        diff_med=round(100 * median([r["cash_gap"] - r["fut_gap_vs_cash"] for r in both]), 3),
        pct_fut_ge7_when_cash_ge7=round(100 * mean(
            [r["fut_gap_vs_cash"] >= 0.07 for r in both if r["cash_gap"] >= 0.07]), 1),
        pct_cash_ge7_when_fut_ge7=round(100 * mean(
            [r["cash_gap"] >= 0.07 for r in both if r["fut_gap_vs_cash"] >= 0.07]), 1),
    )

    # 2. 純期貨口徑（PIT 合法）：期貨 08:45 跳空門檻掃描
    res["fut_pit"] = []
    for x in (0.03, 0.05, 0.07, 0.09):
        for key, nm in (("fut_gap_vs_cash", "vs前現收"), ("fut_gap_vs_fut", "vs前期收")):
            sub = [r for r in recs if r[key] >= x]
            res["fut_pit"].append(dict(
                rule=f"期貨08:45跳空>={x:.0%} ({nm}) · 空期貨",
                stock_leg=daystats([(r["d1"], r["short_fut"] - COST_FUT) for r in sub]),
                hedged=daystats([(r["d1"], r["short_fut"] + BETA * r["ix"] - COST_FUT)
                                 for r in sub]),
            ))

    # 3. 對照：現貨 09:00 訊號 → 空現貨（PIT 合法）
    res["cash_pit"] = []
    for x in (0.05, 0.07):
        sub = [r for r in recs if r["cash_gap"] >= x]
        res["cash_pit"].append(dict(
            rule=f"現貨09:00跳空>={x:.0%} · 空現貨",
            stock_leg=daystats([(r["d1"], r["short_cash"] - COST_EQ) for r in sub]),
            hedged=daystats([(r["d1"], r["short_cash"] + BETA * r["ix"] - COST_EQ)
                             for r in sub]),
        ))

    # 4. 前一輪的 look-ahead 版本（現貨09:00訊號 → 期貨08:45成交），僅供對照
    res["lookahead"] = []
    for x in (0.05, 0.07):
        sub = [r for r in recs if r["cash_gap"] >= x]
        res["lookahead"].append(dict(
            rule=f"⚠️look-ahead 現貨09:00訊號>={x:.0%} → 期貨08:45成交",
            stock_leg=daystats([(r["d1"], r["short_fut"] - COST_FUT) for r in sub]),
        ))

    # 5. 期貨口徑下依成交量分層
    res["by_vol"] = []
    sub7 = [r for r in recs if r["fut_gap_vs_cash"] >= 0.07]
    for lo, hi, nm in ((50, 2000, "50~2000口"), (2000, 10000, "2000~1萬口"),
                       (10000, 9e9, ">1萬口")):
        s = [r for r in sub7 if lo <= r["fv"] < hi]
        res["by_vol"].append(dict(
            bucket=nm,
            stock_leg=daystats([(r["d1"], r["short_fut"] - COST_FUT) for r in s], 20),
        ))

    # 6. IS/OOS
    res["is_oos"] = []
    for x in (0.05, 0.07):
        sub = [r for r in recs if r["fut_gap_vs_cash"] >= x]
        res["is_oos"].append(dict(
            rule=f"期貨08:45跳空>={x:.0%}",
            IS=daystats([(r["d1"], r["short_fut"] + BETA * r["ix"] - COST_FUT)
                         for r in sub if r["date"] < "2026-01-01"], 20),
            OOS=daystats([(r["d1"], r["short_fut"] + BETA * r["ix"] - COST_FUT)
                          for r in sub if r["date"] >= "2026-01-01"], 20),
        ))

    (OUT / "futures_0845.json").write_text(
        json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(res, ensure_ascii=False, indent=1))
    log(f"→ {OUT/'futures_0845.json'}")


if __name__ == "__main__":
    main()
