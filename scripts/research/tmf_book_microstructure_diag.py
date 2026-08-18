#!/usr/bin/env python3
"""TMF 五檔（five-tier book）微觀結構診斷 — 為什麼五檔沒發揮效果。

資料：$GOLDENSTOCKS_DATA_DIR/cache/tmf_books/tmf_books_YYYY-MM-DD.jsonl
（Fubon websocket ``books`` channel，日盤＋夜盤同時訂閱、寫進同一個檔）。

必讀的資料陷阱（本腳本一律先套用，別繞過）：
  * 兩個 session 的 book 交錯寫進同一檔。收盤後那一邊會凍結在最後一筆，每次
    session 續約（約 58 分）就被原樣重送一次：2026-08-17 00:10~03:06 的列全是
    08-14 13:45 與 08-15 05:00 的收盤簿在鬼打牆。不濾掉的話「五檔沒用」會是
    量測假象，不是市場事實。
    **2026-08-18 起收集器已在寫檔當下標記** ``session`` / ``book_age_sec`` /
    ``stale`` 三個欄位，本腳本優先採用；只有那天以前的舊資料才回退成自己用
    ``book_time``（微秒 epoch）對照 wall-clock ``ts`` 反推。
  * price/size 都是整數口數，TMF 一點 = NT$10。

方法（對應文獻）：
  M1  報價價差分布                      Demsetz(1968) / Roll(1984)
  M2  各檔深度分布                      Harris(2003) ch.13
  M3  盤口失衡 OBI 分布                 Cont-Kukanov-Stoikov(2014)
  M4  OBI → 未來 mid 變動之預測力        同上（OFI 的靜態近親）
  M5  micro-price 對 mid 的領先性        Stoikov(2018) "The Micro-Price"
  M6  盤口存續期（quote lifetime）        Hasbrouck(2007)
  M7  價差相對於策略掛單距離的量級        Glosten-Milgrom(1985) 的補償缺口
"""

from __future__ import annotations

import argparse
import json
import statistics as st
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

TZ = timezone(timedelta(hours=8))
MAX_BOOK_AGE_SEC = 5.0  # book_time 落後 wall-clock 超過這個秒數就是殭屍


def books_dir() -> Path:
    try:
        import stock_db

        return Path(stock_db.DATA_DIR).parent / "cache" / "tmf_books"
    except Exception:  # noqa: BLE001
        return Path.home() / "goldenstocks-data" / "cache" / "tmf_books"


def load_live_books(day: str) -> tuple[list[dict[str, Any]], dict[str, int]]:
    path = books_dir() / f"tmf_books_{day}.jsonl"
    stats = Counter()
    out: list[dict[str, Any]] = []
    if not path.exists():
        return out, dict(stats)
    for line in path.open(encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        stats["rows"] += 1
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            stats["bad_json"] += 1
            continue
        bids, asks = r.get("bids") or [], r.get("asks") or []
        if not bids or not asks:
            stats["empty_side"] += 1
            continue
        try:
            wall = datetime.fromisoformat(str(r["ts"])).astimezone(TZ)
            book_ts = datetime.fromtimestamp(float(r["book_time"]) / 1e6, tz=TZ)
        except (KeyError, TypeError, ValueError):
            stats["bad_ts"] += 1
            continue
        # 收集器自 2026-08-18 起直接寫入 stale/session/book_age_sec（見
        # collect_ccf_books_websocket._classify_book）。有欄位就信欄位——那是
        # 收到訊息當下算的，比事後用寫檔時間回推準；沒有的舊資料才自己算。
        if "stale" in r:
            is_stale = bool(r["stale"])
            age = r.get("book_age_sec")
        else:
            age = (wall - book_ts).total_seconds()
            is_stale = age > MAX_BOOK_AGE_SEC
        if is_stale:
            stats["stale_zombie"] += 1
            continue
        stats["live"] += 1
        out.append(
            {
                "t": book_ts,
                "hm": book_ts.strftime("%H:%M"),
                "bp": [float(b["price"]) for b in bids],
                "bs": [float(b["size"]) for b in bids],
                "ap": [float(a["price"]) for a in asks],
                "asz": [float(a["size"]) for a in asks],
            }
        )
    out.sort(key=lambda r: r["t"])
    return out, dict(stats)


def pct(xs: list[float], q: float) -> float:
    if not xs:
        return float("nan")
    s = sorted(xs)
    return s[min(len(s) - 1, int(q * (len(s) - 1)))]


def describe(name: str, xs: list[float], unit: str = "") -> str:
    if not xs:
        return f"{name:<28} (no data)"
    return (
        f"{name:<28} n={len(xs):>7}  mean={st.mean(xs):>8.2f}  p10={pct(xs,.10):>7.2f}  "
        f"p50={pct(xs,.50):>7.2f}  p90={pct(xs,.90):>7.2f}  max={max(xs):>8.2f} {unit}"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", nargs="+", required=True)
    ap.add_argument("--horizons", nargs="+", type=int, default=[5, 15, 30, 60, 300])
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    all_rows: list[dict[str, Any]] = []
    for day in args.days:
        rows, stats = load_live_books(day)
        print(f"[{day}] {stats}")
        all_rows.extend(rows)
    if not all_rows:
        print("no live book rows — every row was a stale zombie or the file is missing")
        return 1
    all_rows.sort(key=lambda r: r["t"])
    print(f"\ntotal live book updates: {len(all_rows)}  "
          f"{all_rows[0]['t']:%F %T} → {all_rows[-1]['t']:%F %T}\n")

    spreads, depth1_b, depth1_a, depth5, obis, micro_dev = [], [], [], [], [], []
    for r in all_rows:
        b1, a1 = r["bp"][0], r["ap"][0]
        bs1, as1 = r["bs"][0], r["asz"][0]
        spreads.append(a1 - b1)
        depth1_b.append(bs1)
        depth1_a.append(as1)
        depth5.append(sum(r["bs"]) + sum(r["asz"]))
        tot = bs1 + as1
        imb = (bs1 - as1) / tot if tot else 0.0
        obis.append(imb)
        mid = 0.5 * (a1 + b1)
        micro = (as1 * b1 + bs1 * a1) / tot if tot else mid  # Stoikov weighted mid
        micro_dev.append(micro - mid)
        r["mid"], r["micro"], r["imb"], r["spread"] = mid, micro, imb, a1 - b1

    print("=== M1/M2 報價價差與深度 ===")
    print(describe("M1 quoted spread", spreads, "pts (1pt=NT$10)"))
    print(describe("M2 bid1 size", depth1_b, "lots"))
    print(describe("M2 ask1 size", depth1_a, "lots"))
    print(describe("M2 total 5-tier size", depth5, "lots"))
    tick_hist = Counter(int(round(s)) for s in spreads)
    print("   spread histogram (pts→share): "
          + "  ".join(f"{k}:{100.0*v/len(spreads):.1f}%" for k, v in sorted(tick_hist.items())[:8]))

    print("\n=== M3 盤口失衡 OBI 分布 ===")
    print(describe("M3 OBI (bid1-ask1)/(sum)", obis))
    print(describe("M5 micro-price − mid", micro_dev, "pts"))

    print("\n=== M4/M5 預測力：OBI 與 micro-price 對未來 mid 的資訊量 ===")
    print(f"{'horizon':<10}{'n':>8}{'corr(OBI,Δmid)':>18}{'corr(micro-mid,Δmid)':>24}"
          f"{'OBI>0 →Δmid>0 %':>18}{'mean|Δmid|':>12}")
    results: dict[str, Any] = {}
    for h in args.horizons:
        xs_o, xs_m, ys = [], [], []
        j = 0
        for i, r in enumerate(all_rows):
            target = r["t"] + timedelta(seconds=h)
            if j < i:
                j = i
            while j + 1 < len(all_rows) and all_rows[j + 1]["t"] <= target:
                j += 1
            if all_rows[j]["t"] < target:  # ran past the end of data
                continue
            d = all_rows[j]["mid"] - r["mid"]
            xs_o.append(r["imb"])
            xs_m.append(r["micro"] - r["mid"])
            ys.append(d)
        if len(ys) < 50:
            continue
        def corr(a: list[float], b: list[float]) -> float:
            try:
                return st.correlation(a, b)
            except (st.StatisticsError, ZeroDivisionError):
                return float("nan")
        sign_hit = sum(1 for o, d in zip(xs_o, ys) if (o > 0) == (d > 0) and d != 0)
        n_sign = sum(1 for d in ys if d != 0)
        row = {
            "n": len(ys),
            "corr_obi": round(corr(xs_o, ys), 4),
            "corr_micro": round(corr(xs_m, ys), 4),
            "obi_sign_acc_pct": round(100.0 * sign_hit / n_sign, 1) if n_sign else None,
            "mean_abs_dmid": round(st.mean(abs(d) for d in ys), 3),
        }
        results[f"h{h}s"] = row
        print(f"{h:<10}{row['n']:>8}{row['corr_obi']:>18}{row['corr_micro']:>24}"
              f"{str(row['obi_sign_acc_pct']):>18}{row['mean_abs_dmid']:>12}")

    print("\n=== M6 盤口存續期（top-of-book 不變的持續時間）===")
    lifetimes, prev = [], None
    for r in all_rows:
        key = (r["bp"][0], r["bs"][0], r["ap"][0], r["asz"][0])
        if prev is None:
            prev = (key, r["t"])
            continue
        if key != prev[0]:
            lifetimes.append((r["t"] - prev[1]).total_seconds())
            prev = (key, r["t"])
    print(describe("M6 top-of-book lifetime", lifetimes, "sec"))

    print("\n=== M7 價差 vs 策略掛單距離 ===")
    med_spread = pct(spreads, 0.5)
    for dist in (10.0, 15.0, 25.0, 30.0, 42.0):
        print(f"   hang {dist:>5.0f} pts = {dist/med_spread:>5.1f}× 中位價差"
              f" · 來回付掉價差佔比 {100.0*med_spread/(2*dist):>5.1f}%")
    print(f"   引擎 COST 常數 = 3.0 pts；實測中位價差 = {med_spread:.1f} pts")

    if args.out:
        payload = {
            "schema": "tmf-book-microstructure-v1",
            "days": args.days,
            "n_live_updates": len(all_rows),
            "spread": {"mean": round(st.mean(spreads), 3), "p50": med_spread,
                       "p90": pct(spreads, .9), "hist_pts": dict(sorted(tick_hist.items())[:10])},
            "depth": {"bid1_p50": pct(depth1_b, .5), "ask1_p50": pct(depth1_a, .5),
                      "five_tier_total_p50": pct(depth5, .5)},
            "obi_micro_predictive": results,
            "top_of_book_lifetime_sec": {"p50": pct(lifetimes, .5), "p90": pct(lifetimes, .9)}
            if lifetimes else None,
        }
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
