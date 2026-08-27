#!/usr/bin/env python3
"""被隔日沖的「回落幅度」由什麼決定 + 盤中回補參數配置（research only）.

進場：T+1 08:45 空個股期貨（訊號＝期貨開盤/前一日期貨收盤-1 >= 門檻）
出場：盤中限價回補（達 −X% 即成交），未達則 13:45 收盤平倉

目標變數：
  MFE = (期貨開盤 − 當日最低) / 期貨開盤   ← 空單盤中最大有利幅度＝回補上限
  MAE = (當日最高 − 期貨開盤) / 期貨開盤   ← 空單盤中最大不利

解釋變數（同一 (股票,日) 內把所有隔日沖席加總）：
  · 前一日增加：T0 現貨漲幅 ret0 / 近5日 ret5 / 期貨跳空 fgap
  · 分點買進量：合計買進金額（億）
  · 佔股比重：合計買進股數 / T0 成交量、合計買進金額 / ADV20

  PYTHONPATH=src .venv/bin/python scripts/research/run_dayflip_futures_intraday_cover.py
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from statistics import mean, median, pstdev

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / "reports/research/branch-footprint-screen"
OUT = BASE / "dayflip_gapup_short"
COST = 0.0005
ADV_MIN = 2000
FGAP_MIN = 0.06


def log(m: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


def q(xs: list[float], p: float) -> float:
    s = sorted(xs)
    return s[max(0, min(len(s) - 1, int(p * (len(s) - 1))))]


def corr(xs: list[float], ys: list[float]) -> float:
    mx, my = mean(xs), mean(ys)
    num = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
    den = (sum((a - mx) ** 2 for a in xs) * sum((b - my) ** 2 for b in ys)) ** 0.5
    return num / den if den else 0.0


def spearman(xs: list[float], ys: list[float]) -> float:
    def ranks(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        for pos, i in enumerate(order):
            r[i] = pos
        return r
    return corr(ranks(xs), ranks(ys))


def main() -> None:
    ev = json.loads((OUT / "events.json").read_text())
    cache = {k: v for k, v in
             json.loads((OUT / "futures_daily_cache.json").read_text()).items() if v}
    mega = set(json.loads((BASE / "ab58_xMega_copytrade/mega_blacklist_v1.json")
                          .read_text())["symbols"])

    roll: dict[tuple[str, str], float] = {}
    for sid, m in cache.items():
        ds = sorted(m)
        for i, d in enumerate(ds):
            if i >= 20:
                roll[(sid, d)] = mean([m[x][4] for x in ds[i - 20:i]])

    # 同一 (股票, 訊號日) 把所有隔日沖席加總
    grp: dict[tuple[str, str], list] = defaultdict(list)
    for e in ev:
        if e["sid"] in mega or e["sid"] not in cache:
            continue
        grp[(e["sid"], e["date"])].append(e)

    recs = []
    for (sid, d0), es in grp.items():
        d1 = es[0]["d1"]
        m = cache[sid]
        if d0 not in m or d1 not in m:
            continue
        fo, fc, fh, fl, fv = m[d1]
        pf = m[d0][1]
        adv = roll.get((sid, d0))
        if fo <= 0 or pf <= 0 or fl <= 0 or adv is None or adv < ADV_MIN:
            continue
        fgap = fo / pf - 1
        if fgap < FGAP_MIN:
            continue
        e0 = es[0]
        recs.append(dict(
            sid=sid, date=d0, d1=d1, n_seats=len(es),
            fgap=fgap,
            mfe=(fo - fl) / fo,          # 空單最大有利
            mae=(fh - fo) / fo,          # 空單最大不利
            ret_close=-(fc / fo - 1),    # 收盤平倉
            # 解釋變數
            ret0=e0["ret0"], ret5=e0["ret5"], rvol=e0["rvol"], adv_yi=e0["adv_yi"],
            amt=sum(x["amt_buy_yi"] for x in es),
            share=sum(x["buy_share"] for x in es),
            advshare=sum(x["amt_share"] for x in es),
            fut_adv=adv,
        ))
    log(f"訊號 {len(recs)} 筆（股票×日，已合併同日多席）· "
        f"{len({r['sid'] for r in recs})} 檔 · {len({r['d1'] for r in recs})} 天")

    res: dict = {}

    # 1. MFE / MAE 分佈
    res["excursion"] = {
        k: dict(med=round(100 * median([r[k] for r in recs]), 2),
                mean=round(100 * mean([r[k] for r in recs]), 2),
                p25=round(100 * q([r[k] for r in recs], 0.25), 2),
                p75=round(100 * q([r[k] for r in recs], 0.75), 2),
                p90=round(100 * q([r[k] for r in recs], 0.90), 2))
        for k in ("mfe", "mae", "ret_close")}

    # 2. 相關性：回落幅度 ~ 各解釋變數
    feats = [("fgap", "期貨跳空"), ("ret0", "T0現貨漲幅"), ("ret5", "近5日漲幅"),
             ("rvol", "量能倍數"), ("amt", "分點買進金額(億)"),
             ("share", "佔T0成交量"), ("advshare", "佔ADV20"),
             ("n_seats", "同日買進席數"), ("adv_yi", "個股ADV(億)"),
             ("fut_adv", "期貨20日均量")]
    res["corr"] = []
    for k, nm in feats:
        xs = [r[k] for r in recs]
        res["corr"].append(dict(
            feature=nm, key=k,
            spearman_mfe=round(spearman(xs, [r["mfe"] for r in recs]), 3),
            spearman_close=round(spearman(xs, [r["ret_close"] for r in recs]), 3),
            spearman_mae=round(spearman(xs, [r["mae"] for r in recs]), 3),
        ))

    # 3. 分位分層：每個特徵切五分位看 MFE / 收盤報酬
    res["quintiles"] = {}
    for k, nm in feats:
        xs = sorted(r[k] for r in recs)
        cuts = [q(xs, p) for p in (0.2, 0.4, 0.6, 0.8)]
        rows = []
        for i, (lo, hi) in enumerate(zip([-9e9] + cuts, cuts + [9e9])):
            s = [r for r in recs if lo <= r[k] < hi]
            if len(s) < 15:
                rows.append(dict(bucket=f"Q{i+1}", n=len(s), thin=True))
                continue
            rows.append(dict(
                bucket=f"Q{i+1}", n=len(s),
                lo=round(lo, 4) if lo > -9e8 else None,
                mfe_med=round(100 * median([r["mfe"] for r in s]), 2),
                close_med=round(100 * median([r["ret_close"] for r in s]), 2),
                close_mean=round(100 * mean([r["ret_close"] for r in s]), 2),
                mae_med=round(100 * median([r["mae"] for r in s]), 2),
            ))
        res["quintiles"][nm] = rows

    # 4. 盤中限價回補目標掃描
    def sim(target: float | None, subset=None) -> dict:
        rows = []
        for r in (subset or recs):
            if target is not None and r["mfe"] >= target:
                pnl = target - COST
            else:
                pnl = r["ret_close"] - COST
            rows.append((r["d1"], pnl, r))
        byd = defaultdict(list)
        for d, p, _ in rows:
            byd[d].append(p)
        dm = [mean(v) for v in byd.values()]
        sd = pstdev(dm) or 1e-9
        hit = (mean([r["mfe"] >= target for r in (subset or recs)])
               if target is not None else 0.0)
        return dict(
            target=("收盤平倉" if target is None else f"{target:.0%}"),
            hit=round(100 * hit, 1), n=len(rows), days=len(dm),
            day_mean=round(100 * mean(dm), 3),
            day_med=round(100 * median(dm), 3),
            day_win=round(100 * mean([x > 0 for x in dm]), 1),
            t=round(mean(dm) / (sd / len(dm) ** 0.5), 2),
            ev_med=round(100 * median([p for _, p, _ in rows]), 2),
            ev_mean=round(100 * mean([p for _, p, _ in rows]), 2),
        )

    res["cover_target"] = [sim(t) for t in (0.01, 0.02, 0.03, 0.04, 0.05, 0.07, None)]

    # 5. 目標隨特徵縮放：target = a + b × 特徵
    res["scaled_target"] = []
    for k, nm, b in (("fgap", "期貨跳空", 0.3), ("fgap", "期貨跳空", 0.5),
                     ("advshare", "佔ADV20", 0.1), ("share", "佔T0成交量", 0.3),
                     ("ret0", "T0漲幅", 0.3)):
        rows = []
        for r in recs:
            tgt = max(0.01, min(0.08, 0.02 + b * r[k]))
            pnl = (tgt - COST) if r["mfe"] >= tgt else (r["ret_close"] - COST)
            rows.append((r["d1"], pnl))
        byd = defaultdict(list)
        for d, p in rows:
            byd[d].append(p)
        dm = [mean(v) for v in byd.values()]
        sd = pstdev(dm) or 1e-9
        res["scaled_target"].append(dict(
            rule=f"target = 2% + {b} × {nm}",
            day_mean=round(100 * mean(dm), 3),
            day_med=round(100 * median(dm), 3),
            day_win=round(100 * mean([x > 0 for x in dm]), 1),
            t=round(mean(dm) / (sd / len(dm) ** 0.5), 2),
            avg_target=round(100 * mean([max(0.01, min(0.08, 0.02 + b * r[k]))
                                         for r in recs]), 2),
        ))

    # 6. 最佳固定目標的 IS/OOS
    res["is_oos"] = []
    for t in (0.02, 0.03, 0.04, None):
        res["is_oos"].append(dict(
            target=("收盤" if t is None else f"{t:.0%}"),
            IS=sim(t, [r for r in recs if r["date"] < "2026-01-01"]),
            OOS=sim(t, [r for r in recs if r["date"] >= "2026-01-01"]),
        ))

    (OUT / "intraday_cover.json").write_text(
        json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(res, ensure_ascii=False, indent=1))
    log(f"→ {OUT/'intraday_cover.json'}")


if __name__ == "__main__":
    main()
