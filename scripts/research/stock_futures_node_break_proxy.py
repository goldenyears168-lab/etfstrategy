#!/usr/bin/env python3
"""個股期貨「穿過成交量堆積價位」的代理版破牆測試——用歷史逐筆，不必等五檔累積。

為什麼可以現在跑
----------------
牆本身（掛單量）只能 live 收，沒有歷史來源。但三個濾網裡有兩個半只需要逐筆：
① 量縮、② 事前盤整、③ 對台指期同方向，全部從成交資料算得出來。而
``~/goldenstocks-data/cache/momentum_rotation/taifex_tick_daily_broad/`` 已經有
**346 檔個股期貨 × 32 個交易日**（606 MB）。

代理的不完美之處要講在前面：**成交量堆積是回顧的、掛單牆是前瞻的**，兩者不等價
——2026-08-20 的 837 天研究已在指數期貨上證實「節點覆蓋率提升兩萬倍，方向資訊
增量卻是 IC 0.00038」。但那是「掛單在節點上」的用法，而且是指數期貨；
**「穿過節點之後會不會續行」在個股期貨上從沒驗過**。

這是一個便宜的否證測試：如果連代理版都沒有續行，真牆版不用做。反之則值得
等五檔累積後再驗一次。

方法
----
  節點  = 回看 10 分鐘，某價位累計成交量 ≥ 該窗口每價位均量 × NODE_RATIO
  穿過  = 價格自節點的一側，成交到節點的另一側
  續行  = 穿過後 30/60/300 秒的報酬（bps，方向與穿越方向同號為正）
  成本  = Roll 有效價差（Roll 1984，從逐筆自相關反推）+ 交易稅 4 bps 來回
  合約  = **ex-ante** 近月規則（第三個週三結算後轉倉）。刻意不用「當日成交量
          argmax」——那是 2026-08-20 在 tick 研究裡抓到的整日 look-ahead，
          值 bar +1,089 pt / tick +952 pt。

用法
----
    PYTHONPATH=src .venv/bin/python scripts/research/stock_futures_node_break_proxy.py \\
        --top 30 --json-out reports/research/channel_lab/sf_node_break_proxy.json
"""
from __future__ import annotations

import argparse
import bisect
import csv
import json
import math
import os
import statistics as st
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

TZ = timezone(timedelta(hours=8))
NODE_LOOKBACK_SEC = 600.0
NODE_RATIO = 3.0
HORIZONS = (30, 60, 300)
TAX_BPS_ROUNDTRIP = 4.0
MIN_TICKS_PER_DAY = 1000
TOUCH_MODE = False   # True＝碰到節點就進場；False＝等穿過才進場


def archive_dir() -> Path:
    base = Path(os.environ.get("GOLDENSTOCKS_DATA_DIR") or Path.home() / "goldenstocks-data")
    return base / "cache" / "momentum_rotation" / "taifex_tick_daily_broad"


def _third_wednesday(y: int, m: int) -> date:
    d = date(y, m, 1)
    d += timedelta(days=(2 - d.weekday()) % 7)      # 第一個週三
    return d + timedelta(days=14)


def front_contract(d: date) -> str:
    """ex-ante 近月：過了本月第三個週三就換下個月。不看任何未來資料。"""
    y, m = d.year, d.month
    if d > _third_wednesday(y, m):
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return f"{y}{m:02d}"


def load_product(code: str) -> dict[str, list[tuple[float, float, float]]]:
    """→ {day: [(ts, price, volume)]}，只留 ex-ante 近月。"""
    p = archive_dir() / f"{code}.csv"
    if not p.exists():
        return {}
    by_day: dict[str, list[tuple[float, float, float]]] = defaultdict(list)
    with p.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                dt = datetime.strptime(r["date"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ)
                px = float(r["price"])
                vol = float(r["volume"])
            except (KeyError, TypeError, ValueError):
                continue
            if px <= 0 or vol <= 0:
                continue
            # 夜盤 17:25 之後屬於「下一個交易日」的 session，但這裡以日曆日分組即可
            # （節點回看 10 分鐘，跨日的 session 邊界由下面的 gap 檢查切開）
            sd = dt.date()
            if str(r.get("contract_date") or "") != front_contract(sd):
                continue
            by_day[str(sd)].append((dt.timestamp(), px, vol))
    for k in by_day:
        by_day[k].sort()
    return {k: v for k, v in by_day.items() if len(v) >= MIN_TICKS_PER_DAY}


def roll_spread_bps(prices: list[float]) -> float | None:
    """Roll(1984) 有效價差：2·sqrt(-cov(Δp_t, Δp_{t-1}))，換算成 bps。
    自協方差為正時（趨勢主導）估計式無定義，回傳 None 而不是硬湊一個數字。"""
    if len(prices) < 200:
        return None
    dp = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
    a = dp[:-1]
    b = dp[1:]
    n = len(a)
    if n < 100:
        return None
    ma, mb = st.mean(a), st.mean(b)
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b)) / n
    if cov >= 0:
        return None
    mid = st.median(prices)
    return 2.0 * math.sqrt(-cov) / mid * 1e4 if mid else None


def analyse(code: str, node_ratio: float) -> dict | None:
    days = load_product(code)
    if not days:
        return None
    events: list[dict] = []
    all_px: list[float] = []
    for day, rows in days.items():
        T = [r[0] for r in rows]
        P = [r[1] for r in rows]
        V = [r[2] for r in rows]
        all_px.extend(P)
        i = 0
        last_break_ts = -1e18
        while i < len(T):
            i += 1
            if i >= len(T):
                break
            if T[i] - T[i - 1] > 1800:          # session 邊界，重新起算
                continue
            j0 = bisect.bisect_left(T, T[i] - NODE_LOOKBACK_SEC)
            if i - j0 < 200:
                continue
            if T[i] - last_break_ts < 60:       # 去重：同一波穿越只算一次
                continue
            vol_at: dict[float, float] = defaultdict(float)
            for m in range(j0, i):
                vol_at[P[m]] += V[m]
            if len(vol_at) < 5:
                continue
            avg = sum(vol_at.values()) / len(vol_at)
            cur = P[i - 1]
            nodes = [px for px, v in vol_at.items() if v >= node_ratio * avg]
            if not nodes:
                continue
            if TOUCH_MODE:
                # 「碰到節點就進場」：成交價**正好等於**某個節點，不等它穿過。
                # 進場價＝該筆成交價＝節點價，所以先前那個「節點→穿越價」的落差
                # 不再是抓不到的東西，而是你本來就站在起點。方向＝逼近方向。
                if P[i] not in vol_at or vol_at[P[i]] < node_ratio * avg:
                    continue
                node = P[i]
                d = 1.0 if P[i] > cur else (-1.0 if P[i] < cur else 0.0)
                if d == 0.0:
                    continue
            else:
                crossed = [px for px in nodes
                           if (cur <= px < P[i]) or (P[i] < px <= cur)]
                if not crossed:
                    continue
                node = max(crossed) if P[i] > cur else min(crossed)
                d = 1.0 if P[i] > node else -1.0
            last_break_ts = T[i]
            a3 = bisect.bisect_left(T, T[i] - 3)
            a60 = bisect.bisect_left(T, T[i] - 60)
            v3 = sum(V[m] for m in range(a3, i))
            v60 = sum(V[m] for m in range(a60, i))
            seg = P[a60:i] or [cur]
            rec = {"day": day, "dir": d, "node": node,
                   "vol_ratio": (v3 / (v60 / 20.0)) if v60 > 0 else None,
                   "range60_bps": (max(seg) - min(seg)) / node * 1e4,
                   "node_strength": vol_at[node] / avg}
            # 【進場價】必須用穿越那一筆的成交價 P[i]，不是節點價。
            # 第一版寫成 (P[k] − node)/node，等於假設你能在節點價成交——但你是
            # **看到價格穿過節點才知道它穿了**，那一跳（node → P[i]，至少一個跳動單位、
            # 對 Roll 價差 ~11 bps 的標的可能就是 5–10 bps）抓不到。
            # 不修的話那一跳會被整包算進「續行」，而它正好與 d 同號，系統性灌水。
            entry = P[i]
            rec["entry_gap_bps"] = (entry - node) / node * 1e4 * d
            for hz in HORIZONS:
                k = bisect.bisect_left(T, T[i] + hz)
                rec[f"run{hz}"] = ((P[k] - entry) / entry * 1e4 * d) if k < len(T) else None
            events.append(rec)
    ok = [e for e in events if e.get("run30") is not None and e.get("vol_ratio") is not None]
    if len(ok) < 30:
        return {"code": code, "n_days": len(days), "n_events": len(events),
                "n_scored": len(ok), "note": "事件太少"}
    spread = roll_spread_bps(all_px)
    return {"code": code, "n_days": len(days), "n_events": len(events), "n_scored": len(ok),
            "ticks_per_day": round(sum(len(v) for v in days.values()) / len(days)),
            "roll_spread_bps": round(spread, 2) if spread else None,
            "cost_bps": round(spread + TAX_BPS_ROUNDTRIP, 2) if spread else None,
            "events": ok}


def summarize(sub: list[dict], hz: int) -> dict:
    v = [e[f"run{hz}"] for e in sub if e.get(f"run{hz}") is not None]
    if len(v) < 20:
        return {"n": len(v)}
    m = st.mean(v)
    return {"n": len(v), "mean_bps": round(m, 2),
            "se_bps": round(st.stdev(v) / len(v) ** 0.5, 2),
            "pct_up": round(100 * sum(1 for x in v if x > 0) / len(v), 1)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--top", type=int, default=30)
    ap.add_argument("--node-ratio", type=float, default=NODE_RATIO)
    ap.add_argument("--touch", action="store_true",
                    help="碰到節點就進場（不等穿過）")
    ap.add_argument("--json-out", default=None)
    args = ap.parse_args()
    globals()["TOUCH_MODE"] = bool(args.touch)
    print("模式：" + ("碰到節點就進場（touch）" if args.touch else "等穿過才進場（break）"))

    sizes = sorted(((p.stat().st_size, p.stem) for p in archive_dir().glob("*.csv")),
                   reverse=True)
    codes = [c for _, c in sizes if c != "TMF"][:args.top]
    print(f"標的 {len(codes)} 檔（依檔案大小取前 {args.top}，排除 TMF 微台）")

    res = []
    for c in codes:
        try:
            r = analyse(c, args.node_ratio)
        except Exception as exc:  # noqa: BLE001
            print(f"  {c}: 失敗 {exc!r}")
            continue
        if not r or r.get("note"):
            print(f"  {c}: {r.get('note') if r else '無資料'}"
                  f"（事件 {r.get('n_scored', 0) if r else 0}）")
            continue
        res.append(r)
        b = summarize(r["events"], 30)
        net = (b["mean_bps"] - r["cost_bps"]) if (b.get("mean_bps") is not None
                                                 and r["cost_bps"]) else None
        print(f"  {c}: {r['n_days']}天 {r['ticks_per_day']:>6,}筆/日 · 事件 {r['n_scored']:>4} · "
              f"續行 {b.get('mean_bps')}±{b.get('se_bps')} bps · "
              f"Roll價差 {r['roll_spread_bps']} · 淨 {net if net is None else round(net,2)}")

    pooled = [e for r in res for e in r["events"]]
    print(f"\n=== 合併 {len(res)} 檔 · {len(pooled)} 個穿越事件 ===")
    print(f"{'':<24}{'n':>7}{'續行bps':>10}{'SE':>8}{'>0%':>8}")
    for hz in HORIZONS:
        s = summarize(pooled, hz)
        print(f"{'順勢 '+str(hz)+'秒':<24}{s.get('n',0):>7}{str(s.get('mean_bps','—')):>10}"
              f"{str(s.get('se_bps','—')):>8}{str(s.get('pct_up','—')):>8}")
    print("（逆勢＝順勢取負號；成本兩邊一樣，所以只要 |順勢| > 成本，其中一邊就有機會）")
    costs = [r["cost_bps"] for r in res if r["cost_bps"]]
    print(f"\n成本（Roll 價差＋稅）中位 = {st.median(costs):.1f} bps" if costs else "")

    def q(xs, p):
        s2 = sorted(xs)
        return s2[min(len(s2) - 1, int(p * (len(s2) - 1)))]
    vr = sorted(e["vol_ratio"] for e in pooled)
    r6 = sorted(e["range60_bps"] for e in pooled)
    f1 = [e for e in pooled if e["vol_ratio"] <= q(vr, 1 / 3)]
    f2 = [e for e in pooled if e["range60_bps"] <= q(r6, 1 / 3)]
    f12 = [e for e in pooled if e["vol_ratio"] <= q(vr, 1 / 3) and e["range60_bps"] <= q(r6, 1 / 3)]
    print(f"\n=== 濾網（30 秒視野）===")
    print(f"{'':<24}{'n':>7}{'續行bps':>10}{'SE':>8}{'>0%':>8}")
    for lbl, sub in (("①量縮", f1), ("②事前盤整", f2), ("①+②", f12)):
        s = summarize(sub, 30)
        print(f"{lbl:<24}{s.get('n',0):>7}{str(s.get('mean_bps','—')):>10}"
              f"{str(s.get('se_bps','—')):>8}{str(s.get('pct_up','—')):>8}")

    if args.json_out:
        p = Path(args.json_out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({
            "schema": "sf-node-break-proxy-v1", "node_ratio": args.node_ratio,
            "n_products": len(res), "n_events": len(pooled),
            "cost_bps_median": round(st.median(costs), 2) if costs else None,
            "pooled": {f"h{h}": summarize(pooled, h) for h in HORIZONS},
            "filters": {"f1_vol": summarize(f1, 30), "f2_consol": summarize(f2, 30),
                        "f12": summarize(f12, 30)},
            "per_product": [{k: v for k, v in r.items() if k != "events"} |
                            {"run30": summarize(r["events"], 30)} for r in res],
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nwrote {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
