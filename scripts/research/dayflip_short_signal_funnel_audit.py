#!/usr/bin/env python3
"""dayflip-futures-short 訊號漏斗稽核 —— 逐層量「哪一個閘門把候選殺光」。

動機（2026-08-19）：sleeve 自 2026-08-07 上線至今 **0 筆成交**，log 4,171 行全是
noop。0 筆有兩種完全相反的解讀，處置也完全相反：

  (a) 閘門太窄 → 設計問題（但放寬＝拿同一份資料重新最佳化，會摧毀 DSR）
  (b) 這段期間市場真的沒有合格標的 → 市場狀態，等就好

分辨兩者不需要新研究，只要把 build_candidates() 的每一層各自留下多少候選印出來。

**覆蓋率警告（本稽核最重要的設計）**：`stock_broker_branch_daily` 的全市場 by-trader
tape 是一次性 backfill，2026-07-16 之後只剩 branch-tape-prewarm 的縮減視角——24 席合計
每日覆蓋的個股數從 ~1,850 掉到 ~1,050（−43%）。而 live 期間（08-07 起）**整段落在殘缺
覆蓋裡**。若不分期統計，就會重演松山 [[songshan-copytrade-current-status]] 那次
「在覆蓋 40% 的世界裡挑冠軍」的覆蓋偏誤（Fung & Hsieh 2000）。因此本腳本一律把
FULL_COVERAGE_END 之前與之後分開報，禁止合併成一個數字。

門檻常數一律 import 自 `order.dayflip_short_signal` / `order.dayflip_short_order`，
不在本檔複製，避免與 live 規格漂移。

用法：
    PYTHONPATH=src .venv/bin/python scripts/research/dayflip_short_signal_funnel_audit.py \
        --start 2025-06-01 --end 2026-08-18
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from statistics import mean

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import stock_db  # noqa: E402
from order.dayflip_short_order import FGAP_MAX  # noqa: E402
from order.dayflip_short_signal import (  # noqa: E402
    ACC_MIN_WINDOW_BUY_NTD,
    ACC_NET_RATIO_MAX,
    ACC_WINDOW_DAYS,
    ADV_MIN_LOTS,
    EXTRA_MANUAL_PAIRS,
    FGAP_MIN,
    HIGH_FLIP_MIN,
    MIN_BUY_NTD,
    _load_mega,
    _load_spec,
    _load_universe,
    estimate_margin_ntd,
)

# 全市場 by-trader tape 一次性 backfill 的最後一天（見模組 docstring）
FULL_COVERAGE_END = "2026-07-16"
# live 上線日
LIVE_START = "2026-08-07"
MARGIN_CAP_NTD = 170_000.0

BASE = (
    stock_db.PROJECT_ROOT
    / "reports/research/branch-footprint-screen/dayflip_gapup_short"
)

STAGES = [
    ("s0_seat_buy_events", "24 席當日買超事件（buy>0 且查得到收盤價）"),
    ("s1_amt_ge_min", f"買超金額 ≥ {MIN_BUY_NTD/1e6:.0f}M"),
    ("s2_tradable_universe", "排除權值股/00開頭ETF/不在個股期宇宙"),
    ("s3_manual_pair", "排除人工 (席位,個股) 黑名單"),
    ("s4_accum_ratio", f"排除 60 日建倉（net_ratio ≥ {ACC_NET_RATIO_MAX}）"),
    ("s5_flip_gate", f"至少一席 flip ≥ {HIGH_FLIP_MIN}"),
    ("s6_adv", f"個股期 ADV20 ≥ {ADV_MIN_LOTS:.0f} 口"),
    ("s7_fgap_min", f"T+1 開盤 fgap ≥ {FGAP_MIN:.0%}"),
    ("s8_fgap_max", f"fgap < {FGAP_MAX:.0%}（2026-08-10 才加的漲停風控上限）"),
    ("s9_margin_cap", f"保證金 ≤ {MARGIN_CAP_NTD/1e4:.0f} 萬"),
]


def _flip_table(pit: bool) -> dict[str, float]:
    """回傳 flip 門檻用的席位表。

    pit=True 讀 pit_seat_flip_latest.json（live 實際用的、PIT-safe，但只對接近
    computed_at 的日期有效）；pit=False 讀 FROZEN_SPEC_V1 的 frozen 表——spec 自己
    的 caveat 承認含輕微 look-ahead，**只能拿來做歷史頻率估計，不能當作 live 依據**。
    歷史回溯一律用 frozen，否則 _load_pit_flip 的 fail-closed 會讓每一天都是 0 候選，
    那是量測假象不是事實。
    """
    if pit:
        payload = json.loads((BASE / "pit_seat_flip_latest.json").read_text())
        vals = payload.get("values") or {}
        return {k: float(v) for k, v in vals.items() if isinstance(v, (int, float))}
    return {k: float(v) for k, v in _load_spec()["seat_flip_table_frozen"]["values"].items()}


def run(start: str, end: str) -> dict:
    spec = _load_spec()
    seats = list(spec["seat_flip_table_frozen"]["values"])
    manual = {tuple(x) for x in spec["signal"]["step2_seat_filters"]["manual_pair_exclusion"]}
    manual |= EXTRA_MANUAL_PAIRS
    mega = _load_mega()
    futmap = _load_universe()
    flip = _flip_table(pit=False)
    fut_cache = json.loads((BASE / "futures_daily_cache.json").read_text())

    con = sqlite3.connect(f"file:{stock_db.DEFAULT_DB_PATH}?mode=ro", uri=True)
    ph = ",".join("?" * len(seats))

    # --- 價格面板（收盤 + 開盤，開盤用來算 T+1 fgap）
    px_close: dict[tuple[str, str], float] = {}
    px_open: dict[tuple[str, str], float] = {}
    for sid, d, o, c in con.execute(
        "SELECT stock_id,trade_date,open,close FROM stock_daily_bars "
        "WHERE source='finmind' AND trade_date BETWEEN ? AND ? AND close>0",
        (start, end),
    ):
        px_close[(str(sid), str(d))] = float(c)
        if o:
            px_open[(str(sid), str(d))] = float(o)

    cal = sorted({d for (_, d) in px_close})
    nxt = {d: cal[i + 1] for i, d in enumerate(cal[:-1])}

    # --- s0/s1：把金額門檻推進 SQL，避免把 290 萬列全拉進記憶體
    stage_rows: dict[str, Counter] = defaultdict(Counter)  # date -> stage -> n
    events: dict[str, list[dict]] = defaultdict(list)
    n_s0 = Counter()
    for d, tid, sid, buy, close in con.execute(
        "SELECT b.trade_date,b.securities_trader_id,b.stock_id,b.buy,p.close "
        "FROM stock_broker_branch_daily b "
        "JOIN stock_daily_bars p ON p.stock_id=b.stock_id AND p.trade_date=b.trade_date "
        "  AND p.source='finmind' AND p.close>0 "
        f"WHERE b.securities_trader_id IN ({ph}) AND b.trade_date BETWEEN ? AND ? AND b.buy>0",
        (*seats, start, end),
    ):
        d, tid, sid = str(d), str(tid), str(sid)
        n_s0[d] += 1
        if float(buy) * float(close) >= MIN_BUY_NTD:
            events[d].append({"tid": tid, "sid": sid, "amt": float(buy) * float(close)})

    # --- net_ratio 的 60 日窗資料
    #
    # 效能註記（2026-08-19 重寫）：第一版對每個 (席位, 個股, 日期) 三元組各發一次
    # SQL，跑 1 小時還沒完。用 EXPLAIN QUERY PLAN 查出 SQLite 會選
    # `idx_branch_daily_stock_date (stock_id, trade_date)`，於是每次呼叫都先撈出
    # 「這檔股票這 60 天的全部 ~800 個分點」再過濾成 1 個——掃 48,000 列只為了拿
    # 60 列，而這種呼叫有上萬次。
    #
    # 改成：先算出「真的需要 net_ratio 的 (席位, 個股) 配對」（那只由 s0~s3 決定，
    # 不依賴 net_ratio 自己），再**每席發一次**查詢走
    # `idx_branch_daily_trader_date (securities_trader_id, trade_date)`，整段歷史
    # 一次load進記憶體。24 次索引查詢取代上萬次掃描。
    needed_sids: set[str] = set()
    needed_pairs: set[tuple[str, str]] = set()
    for d, evs in events.items():
        for e in evs:
            sid = e["sid"]
            if sid in mega or sid.startswith("00") or sid not in futmap:
                continue
            if (e["tid"], sid) in manual:
                continue
            needed_sids.add(sid)
            needed_pairs.add((e["tid"], sid))
    print(f"  需要 net_ratio 的配對：{len(needed_pairs)}（涉及 {len(needed_sids)} 檔）",
          file=sys.stderr)

    br: dict[tuple[str, str], dict[str, tuple[float, float]]] = {}
    for n, tid in enumerate(seats, 1):
        rows = con.execute(
            "SELECT trade_date,stock_id,buy,sell FROM stock_broker_branch_daily "
            "WHERE securities_trader_id=? AND trade_date BETWEEN ? AND ?",
            (tid, start, end),
        ).fetchall()
        kept = 0
        for dd, sid, b, s in rows:
            sid = str(sid)
            if sid not in needed_sids:
                continue
            br.setdefault((tid, sid), {})[str(dd)] = (float(b or 0), float(s or 0))
            kept += 1
        print(f"  [{n}/{len(seats)}] 席位 {tid}: {len(rows):,} 列 → 留 {kept:,}",
              file=sys.stderr)

    ci = {d: i for i, d in enumerate(cal)}
    ratio_cache: dict[tuple[str, str, str], float | None] = {}

    def net_ratio(tid: str, sid: str, as_of: str) -> float | None:
        key = (tid, sid, as_of)
        if key in ratio_cache:
            return ratio_cache[key]
        i = ci.get(as_of, -1)
        if i < ACC_WINDOW_DAYS:
            ratio_cache[key] = None
            return None
        hist = br.get((tid, sid)) or {}
        tb = ts = 0.0
        for dd in cal[i - ACC_WINDOW_DAYS:i]:
            b, s = hist.get(dd, (0.0, 0.0))
            if not b and not s:
                continue
            p = px_close.get((sid, dd))
            if p is None:
                continue
            tb += b * p
            ts += s * p
        val = None if tb < ACC_MIN_WINDOW_BUY_NTD else (tb - ts) / tb
        ratio_cache[key] = val
        return val

    picks: list[dict] = []
    for d in cal:
        st = stage_rows[d]
        st["s0_seat_buy_events"] = n_s0.get(d, 0)
        evs = events.get(d, [])
        st["s1_amt_ge_min"] = len(evs)

        by_sid: dict[str, list[dict]] = defaultdict(list)
        for e in evs:
            sid = e["sid"]
            if sid in mega or sid.startswith("00") or sid not in futmap:
                continue
            by_sid[sid].append(e)
        st["s2_tradable_universe"] = sum(len(v) for v in by_sid.values())

        surv_manual: dict[str, list[dict]] = {}
        for sid, es in by_sid.items():
            keep = [e for e in es if (e["tid"], sid) not in manual]
            if keep:
                surv_manual[sid] = keep
        st["s3_manual_pair"] = sum(len(v) for v in surv_manual.values())

        surv_acc: dict[str, list[dict]] = {}
        for sid, es in surv_manual.items():
            keep = []
            for e in es:
                nr = net_ratio(e["tid"], sid, d)
                if nr is not None and nr >= ACC_NET_RATIO_MAX:
                    continue
                keep.append(e)
            if keep:
                surv_acc[sid] = keep
        st["s4_accum_ratio"] = sum(len(v) for v in surv_acc.values())

        # s5 起改用「個股」為單位（一檔股票 = 一個候選）
        surv_flip = {
            sid: es for sid, es in surv_acc.items()
            if any(flip.get(e["tid"], 0) >= HIGH_FLIP_MIN for e in es)
        }
        st["s5_flip_gate"] = len(surv_flip)

        surv_adv = []
        for sid in surv_flip:
            m = fut_cache.get(sid) or {}
            ds = sorted(m)
            if d not in ds:
                continue
            i = ds.index(d)
            if i < 20:
                continue
            adv = mean([m[x][4] for x in ds[i - 20:i]])
            if adv >= ADV_MIN_LOTS:
                surv_adv.append(sid)
        st["s6_adv"] = len(surv_adv)

        # s7~s9 用 T+1 開盤
        d1 = nxt.get(d)
        gapped = []
        for sid in surv_adv:
            if d1 is None:
                continue
            op = px_open.get((sid, d1))
            t0c = px_close.get((sid, d))
            if op is None or not t0c:
                continue
            fgap = op / t0c - 1
            gapped.append((sid, fgap, op))
        st["s7_fgap_min"] = sum(1 for _, f, _ in gapped if f >= FGAP_MIN)
        under_cap = [(s, f, o) for s, f, o in gapped if FGAP_MIN <= f < FGAP_MAX]
        st["s8_fgap_max"] = len(under_cap)
        final = [(s, f, o) for s, f, o in under_cap
                 if estimate_margin_ntd(o) <= MARGIN_CAP_NTD]
        st["s9_margin_cap"] = len(final)
        for s, f, o in final:
            picks.append({
                "t0": d, "t1": d1, "stock_id": s, "fgap": round(f, 6),
                "open": o, "margin": round(estimate_margin_ntd(o)),
                # n_seats 給下游回測重現 live 的單日選股規則用
                # （pick_signal：0.75×fgap升冪rank + 0.25×席數降冪rank，取最小）
                "n_seats": len({e["tid"] for e in surv_acc.get(s, [])}),
                "t0_close": px_close.get((s, d)),
            })

    con.close()
    return {"stage_rows": stage_rows, "picks": picks, "cal": cal}


def report(res: dict, start: str, end: str) -> None:
    stage_rows, picks, cal = res["stage_rows"], res["picks"], res["cal"]

    def agg(days: list[str]) -> dict[str, int]:
        out = {k: 0 for k, _ in STAGES}
        for d in days:
            for k, _ in STAGES:
                out[k] += stage_rows[d].get(k, 0)
        return out

    full = [d for d in cal if d <= FULL_COVERAGE_END]
    degraded = [d for d in cal if d > FULL_COVERAGE_END]
    live = [d for d in cal if d >= LIVE_START]

    print("=" * 78)
    print(f"dayflip-short 訊號漏斗稽核  {start} ~ {end}")
    print("=" * 78)
    for label, days in (("完整覆蓋期", full), ("殘缺覆蓋期", degraded), ("live 期間", live)):
        if not days:
            continue
        a = agg(days)
        n = len(days)
        print(f"\n【{label}】{days[0]} ~ {days[-1]}  共 {n} 個交易日")
        prev = None
        for k, desc in STAGES:
            v = a[k]
            kill = "" if prev is None or prev == 0 else f"  (砍掉 {100*(1-v/prev):5.1f}%)"
            print(f"  {k:22s} {v:8d}{kill}   {desc}")
            prev = v
        got = [p for p in picks if p["t0"] in set(days)]
        print(f"  → 合格訊號 {len(got)} 筆／{n} 日 = 每 {n/len(got):.1f} 日 1 筆"
              if got else f"  → 合格訊號 0 筆／{n} 日")

    print("\n【FGAP 上限 (9%) 單獨影響】")
    for label, days in (("完整覆蓋期", full), ("殘缺覆蓋期", degraded)):
        if not days:
            continue
        a = agg(days)
        lost = a["s7_fgap_min"] - a["s8_fgap_max"]
        print(f"  {label}: fgap≥7% 有 {a['s7_fgap_min']} 筆，被 9% 上限砍掉 {lost} 筆"
              f"（{100*lost/a['s7_fgap_min']:.1f}%）" if a["s7_fgap_min"] else
              f"  {label}: fgap≥7% 0 筆")

    if picks:
        print(f"\n【最近 15 筆合格訊號】（共 {len(picks)} 筆）")
        for p in picks[-15:]:
            print(f"  T0={p['t0']} T+1={p['t1']} {p['stock_id']}  "
                  f"fgap={p['fgap']:+.2%} open={p['open']:.2f} margin={p['margin']:,}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2025-06-01")
    ap.add_argument("--end", default=date.today().isoformat())
    ap.add_argument("--json-out", default=None)
    args = ap.parse_args()
    res = run(args.start, args.end)
    report(res, args.start, args.end)
    if args.json_out:
        Path(args.json_out).write_text(json.dumps({
            "stage_rows": {d: dict(c) for d, c in res["stage_rows"].items()},
            "picks": res["picks"],
        }, ensure_ascii=False, indent=1))
        print(f"\n寫出 {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
