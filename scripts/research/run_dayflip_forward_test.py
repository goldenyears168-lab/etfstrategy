#!/usr/bin/env python3
"""dayflip-futures-short v1 前推驗證 runner（research only）.

完全依 FROZEN_SPEC_V1.json 執行，**不接受任何門檻參數**——這是刻意的，
凍結後任何門檻變更都必須改 spec 檔並重啟前推期。

  PYTHONPATH=src:scripts/research .venv/bin/python \
      scripts/research/run_dayflip_forward_test.py --start 2026-07-09 --end 2026-08-06
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path
from statistics import mean, median, pstdev

import stock_db
from finmind_client import fetch_finmind
from run_xd_50m_stock_futures_timing import fetch_futures_ticks

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / "reports/research/branch-footprint-screen"
OUT = BASE / "dayflip_gapup_short"
SPEC = json.loads((OUT / "FROZEN_SPEC_V1.json").read_text())

MIN_BUY = SPEC["signal"]["step1_branch"]["min_buy_amount_ntd"]
ADV_MIN = SPEC["universe"]["liquidity_filter"]["min_lots"]
FGAP_MIN = 0.06
TARGET = 0.02
COST = SPEC["execution"]["cost_bps_round_trip"] / 10000
ACC_THR = 0.30
ACC_WINDOW = 60
ACC_MIN_BUY = 100_000_000
HIGH_THR = SPEC["signal"]["step2_seat_filters"]["high_flip_requirement"]["rule"]
FLIP_TABLE = SPEC["seat_flip_table_frozen"]["values"]
HIGH_FLIP = 0.40
MANUAL = {tuple(x) for x in SPEC["signal"]["step2_seat_filters"]["manual_pair_exclusion"]}
SEATS = list(FLIP_TABLE)
MARKS = list(range(0, 305, 5))


def log(m: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=SPEC["forward_test_protocol"]["start"])
    ap.add_argument("--end", default="2026-08-06")
    args = ap.parse_args()
    log(f"前推期間 {args.start} ~ {args.end} · spec {SPEC['spec_id']} "
        f"(frozen {SPEC['frozen_at']})")

    futmap = json.loads((OUT / "stock_futures_universe.json").read_text())["map"]
    mega = set(json.loads((BASE / "ab58_xMega_copytrade/mega_blacklist_v1.json")
                          .read_text())["symbols"])

    con = sqlite3.connect(f"file:{stock_db.DEFAULT_DB_PATH}?mode=ro", uri=True)
    lo = "2025-10-01"   # 供 60 日建倉回看
    branch: dict[str, dict[str, dict[str, tuple]]] = defaultdict(lambda: defaultdict(dict))
    for tid in SEATS:
        for d, sid, b, s in con.execute(
            "SELECT trade_date, stock_id, buy, sell FROM stock_broker_branch_daily "
            "WHERE securities_trader_id=? AND trade_date BETWEEN ? AND ?",
            (tid, lo, args.end),
        ):
            branch[tid][str(sid)][str(d)] = (float(b or 0), float(s or 0))
    px: dict[tuple[str, str], float] = {}
    for sid, d, c in con.execute(
        "SELECT stock_id, trade_date, close FROM stock_daily_bars "
        "WHERE source='finmind' AND trade_date BETWEEN ? AND ? AND close>0",
        (lo, args.end),
    ):
        px[(str(sid), str(d))] = float(c)
    con.close()
    cal = sorted({d for (_, d) in px})
    ci = {d: i for i, d in enumerate(cal)}
    log(f"分點/價格載入完成 · 交易日 {len(cal)}")

    # 候選事件：T0 在前推窗內、買進 >=0.3億、非權值股、有期貨
    cands = defaultdict(list)
    for tid in SEATS:
        for sid, m in branch[tid].items():
            if sid in mega or sid.startswith("00") or sid not in futmap:
                continue
            for d, (b, s) in m.items():
                if d < args.start or d > args.end or b <= 0:
                    continue
                p = px.get((sid, d))
                if p is None or b * p < MIN_BUY:
                    continue
                cands[(sid, d)].append(dict(tid=tid, buy=b, amt=b * p))
    log(f"T0 候選 (股票×日) {len(cands)} 組")

    def net_ratio(tid, sid, d0):
        i = ci.get(d0)
        if i is None or i < ACC_WINDOW:
            return None
        tb = ts = 0.0
        for d in cal[i - ACC_WINDOW:i]:
            b, s = (branch[tid].get(sid) or {}).get(d, (0.0, 0.0))
            p = px.get((sid, d))
            if p is None:
                continue
            tb += b * p
            ts += s * p
        if tb < ACC_MIN_BUY:
            return None
        return (tb - ts) / tb

    # 套用 spec 的席位過濾
    kept = {}
    for (sid, d0), es in cands.items():
        keep = []
        for e in es:
            if (e["tid"], sid) in MANUAL:
                continue
            nr = net_ratio(e["tid"], sid, d0)
            if nr is not None and nr >= ACC_THR:
                continue
            keep.append(e)
        if not keep:
            continue
        if not any(FLIP_TABLE.get(e["tid"], 0) >= HIGH_FLIP for e in keep):
            continue
        kept[(sid, d0)] = keep
    log(f"通過席位過濾 {len(kept)} 組")

    # 取期貨日線（近月）與 20 日均量
    need = sorted({sid for (sid, _) in kept})
    log(f"抓 {len(need)} 檔期貨日線 ...")
    fut: dict[str, dict[str, list]] = {}
    for sid in need:
        c = futmap[sid]
        fid = c + "F" if len(c) == 2 else c
        try:
            rws = fetch_finmind("TaiwanFuturesDaily", fid,
                                date.fromisoformat(lo), date.fromisoformat(args.end),
                                timeout=180)
        except Exception as ex:  # noqa: BLE001
            log(f"  {sid} ERR {str(ex)[:50]}")
            continue
        byd = defaultdict(list)
        for r in rws:
            cd = str(r.get("contract_date", ""))
            if "/" in cd or r.get("trading_session") != "position":
                continue
            if float(r.get("open") or 0) <= 0:
                continue
            byd[str(r["date"])].append(r)
        out = {}
        for d, rs in byd.items():
            near = max(rs, key=lambda x: float(x.get("volume") or 0))
            out[d] = [float(near["open"]), float(near["close"]),
                      sum(float(x.get("volume") or 0) for x in rs)]
        fut[sid] = out
        time.sleep(0.25)

    trades = []
    for (sid, d0), es in sorted(kept.items()):
        m = fut.get(sid) or {}
        ds = sorted(m)
        if d0 not in m:
            continue
        idx = ds.index(d0)
        if idx + 1 >= len(ds) or idx < 20:
            continue
        d1 = ds[idx + 1]
        adv = mean([m[x][2] for x in ds[idx - 20:idx]])
        if adv < ADV_MIN:
            continue
        fo, prev_c = m[d1][0], m[d0][1]
        if fo <= 0 or prev_c <= 0:
            continue
        fgap = fo / prev_c - 1
        if fgap < FGAP_MIN:
            continue
        c = futmap[sid]
        fid = c + "F" if len(c) == 2 else c
        try:
            ticks = fetch_futures_ticks(fid, d1)
        except Exception as ex:  # noqa: BLE001
            log(f"  tick {sid} {d1} ERR {str(ex)[:40]}")
            continue
        if not ticks:
            continue
        vol = Counter()
        for t in ticks:
            vol[t.contract] += max(t.volume, 1)
        near = vol.most_common(1)[0][0]
        ts = [t for t in ticks if t.contract == near]
        if not ts:
            continue
        base = ts[0].ts.replace(hour=8, minute=45, second=0, microsecond=0)
        entry = ts[0].price
        hit_min = None
        for t in ts:
            if t.price <= entry * (1 - TARGET):
                hit_min = (t.ts - base).total_seconds() / 60.0
                break
        if hit_min is not None:
            pnl, how, ex_t = TARGET - COST, "觸價回補", hit_min
        else:
            pnl, how, ex_t = -(ts[-1].price / entry - 1) - COST, "收盤平倉", None
        trades.append(dict(
            signal_date=d0, trade_date=d1, stock=sid, entry=entry,
            fgap_pct=round(100 * fgap, 2), fut_adv=round(adv),
            n_seats=len(es), seats=",".join(sorted(e["tid"] for e in es)),
            max_seat_flip=round(max(FLIP_TABLE.get(e["tid"], 0) for e in es), 3),
            amt_yi=round(sum(e["amt"] for e in es) / 1e8, 2),
            how=how, exit_min=(None if ex_t is None else round(ex_t, 1)),
            pnl_pct=round(100 * pnl, 2)))
        time.sleep(0.2)

    log(f"前推交易 {len(trades)} 筆")
    if not trades:
        print("無訊號")
        return
    byd = defaultdict(list)
    for t in trades:
        byd[t["trade_date"]].append(t["pnl_pct"] / 100)
    dm = [mean(v) for v in byd.values()]
    sd = pstdev(dm) or 1e-9
    res = dict(
        spec=SPEC["spec_id"], frozen_at=SPEC["frozen_at"],
        window=[args.start, args.end],
        signal_days=len(dm), n_trades=len(trades),
        day_mean_pct=round(100 * mean(dm), 3),
        day_median_pct=round(100 * median(dm), 3),
        day_win_pct=round(100 * mean([x > 0 for x in dm]), 1),
        t=round(mean(dm) / (sd / len(dm) ** 0.5), 2),
        hit_rate_pct=round(100 * mean([t["how"] == "觸價回補" for t in trades]), 1),
        med_exit_min=median([t["exit_min"] for t in trades if t["exit_min"] is not None])
        if any(t["exit_min"] is not None for t in trades) else None,
        trades=trades,
    )
    crit = SPEC["forward_test_protocol"]["pass_criteria"]
    res["verdict"] = dict(
        min_signal_days=f"{len(dm)} / 需 {crit['min_signal_days']}",
        day_median=f"{res['day_median_pct']} / 需 > 0",
        day_win=f"{res['day_win_pct']} / 需 >= {crit['day_win_rate_pct']}",
        day_mean=f"{res['day_mean_pct']} / 需 > 0.5",
        note="訊號日不足者為『尚無結論』，不算 fail",
    )
    (OUT / f"forward_test_{args.start}_{args.end}.json").write_text(
        json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: v for k, v in res.items() if k != "trades"},
                     ensure_ascii=False, indent=1))
    print(f"\n{'訊號日':<11}{'交易日':<11}{'股票':<7}{'進場':>9}{'跳空%':>7}"
          f"{'席':>3}{'最高沖分':>8}{'出場':>10}{'分鐘':>6}{'損益%':>8}")
    for t in trades:
        print(f"{t['signal_date']:<11}{t['trade_date']:<11}{t['stock']:<7}{t['entry']:>9.1f}"
              f"{t['fgap_pct']:>7.2f}{t['n_seats']:>3}{t['max_seat_flip']:>8.3f}"
              f"{t['how']:>10}{(t['exit_min'] if t['exit_min'] is not None else '-'):>6}"
              f"{t['pnl_pct']:>8.2f}")
    log(f"→ {OUT}/forward_test_{args.start}_{args.end}.json")


if __name__ == "__main__":
    main()
