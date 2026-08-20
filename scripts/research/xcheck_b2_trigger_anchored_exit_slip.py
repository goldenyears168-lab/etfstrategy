#!/usr/bin/env python3
"""對抗性複核 B2：以「觸發那一筆成交價」為錨的市價出場增量成本。

B2 用 mid 當錨（隨機時刻半價差 1.6/1.4 點）。但 causal_engine.close_side 把出場
記在觸發那一筆 tick 的價格上，所以 backtest 少算的是 (p − bid)／(ask − p)，
而且要條件在**觸發方向**上（trail／struct_break 78% 都是逆向觸發）。
本腳本用 live book 對齊（含延遲 Δ）重量一次。
"""
from __future__ import annotations
import bisect, json, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
TZ = timezone(timedelta(hours=8))
CACHE = Path.home() / "goldenstocks-data" / "cache"
MAXAGE = 5.0


def books(day):
    out = []
    p = CACHE / "tmf_books" / f"tmf_books_{day}.jsonl"
    for line in p.open():
        r = json.loads(line)
        b, a = r.get("bids"), r.get("asks")
        if not b or not a:
            continue
        wall = datetime.fromisoformat(r["ts"]).timestamp()
        bt = r["book_time"] / 1e6
        stale = bool(r["stale"]) if "stale" in r else (wall - bt > MAXAGE)
        if stale or a[0]["price"] <= b[0]["price"]:
            continue
        out.append((bt, float(b[0]["price"]), float(a[0]["price"])))
    out.sort()
    return out


def trades(day):
    out = []
    p = CACHE / "tmf_trades" / f"tmf_trades_{day}.jsonl"
    for line in p.open():
        r = json.loads(line)
        wall = datetime.fromisoformat(r["ts"]).timestamp()
        tt = r["trade_time"] / 1e6
        if wall - tt > MAXAGE:
            continue
        out.append((tt, float(r["price"])))
    out.sort()
    return out


def sess_of(ts):
    hm = datetime.fromtimestamp(ts, tz=TZ).strftime("%H:%M")
    return "day" if "08:45" <= hm <= "13:45" else "night"


def main():
    days = sys.argv[1:] or ["2026-08-17", "2026-08-18"]
    res = {}
    for day in days:
        bk = books(day)
        tr = trades(day)
        bt = [x[0] for x in bk]
        for lag in (0.0, 1.0, 5.0, 20.0):
            acc = {}
            prev = None
            for tt, px in tr:
                d = None if prev is None else (1 if px > prev else (-1 if px < prev else 0))
                prev = px
                if not d:
                    continue
                i = bisect.bisect_right(bt, tt + lag) - 1
                if i < 0 or (tt + lag) - bt[i] > MAXAGE:
                    continue
                _, B, A = bk[i]
                s = sess_of(tt)
                # 逆向觸發：downtick → 多單被停損 → 市價賣拿 bid，增量 = px − B
                #           uptick   → 空單被停損 → 市價買付 ask，增量 = A − px
                cost = (px - B) if d < 0 else (A - px)
                side = "long_stop_sell" if d < 0 else "short_stop_buy"
                acc.setdefault((s, side), []).append(cost)
                acc.setdefault((s, "spread"), []).append(A - B)
            for (s, side), v in sorted(acc.items()):
                res[f"{day}|lag{lag:g}s|{s}|{side}"] = (len(v), round(sum(v) / len(v), 3))
    for k, (n, m) in res.items():
        print(f"{k:<50} n={n:>7,}  mean={m:+.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
