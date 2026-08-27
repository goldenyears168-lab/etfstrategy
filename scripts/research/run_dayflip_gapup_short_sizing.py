#!/usr/bin/env python3
"""隔日沖倒貨 · 開高做空 · 進場門檻與部位大小研究（research only）.

命題：分點於 T0 大額買進（隔日沖傾向席）→ T+1 開高 → 該席在盤中倒貨。
      在 T+1 開盤放空，於當日收盤（或 T+2/T+3）回補。

PIT：T0 分點資料於 T0 盤後公布 → T+1 開盤決策合法。
     席位 flip 傾向用「事件日之前」的擴張窗中位數（不看未來）。

超額（做空口徑）：short_exc = -(r_stock) + BETA * (r_index) - COST
  → 即持有「空個股 + 多 BETA 倍指數」的對沖組合。BETA 預設 1.5（使用者指定）。

  PYTHONPATH=src .venv/bin/python scripts/research/run_dayflip_gapup_short_sizing.py
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from statistics import median, mean, pstdev

import stock_db

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "reports/research/branch-footprint-screen/dayflip_gapup_short"
SOURCE = "finmind"

SEATS = {
    "920M": "凱基-宜蘭", "7008": "兆豐-三重", "989g": "元大-嘉義", "981j": "元大-士林",
    "5851": "統一-高雄", "9217": "凱基松山", "980h": "元大-台北", "918e": "群益金鼎-大安",
    "989X": "元大-民生三民", "779Z": "國票安和", "913R": "群益金鼎-北高雄", "9661": "富邦新店",
    "585Y": "統一土城", "9875": "元大-土城永寧", "918X": "群益金鼎-台北", "5383": "第一金-高雄",
    "1360": "港麥格理", "9A9R": "永豐金信義", "9325": "華南永昌-忠孝", "9A81": "永豐金-匯立",
    "779n": "國票南京", "9227": "凱基城中", "9216": "凱基-信義", "920F": "凱基-站前",
}

OOS_START = "2026-01-01"


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def q(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    i = max(0, min(len(s) - 1, int(p * (len(s) - 1))))
    return s[i]


def build(args) -> list[dict]:
    conn = sqlite3.connect(f"file:{stock_db.DEFAULT_DB_PATH}?mode=ro", uri=True)
    lo, hi = "2024-05-01", "2026-07-20"

    ix: dict[str, tuple[float, float]] = {}
    best: dict[str, int] = {}
    rank = {"yahoo": 0, "tej": 1, "finmind": 2}
    for d, o, c, src in conn.execute(
        "SELECT date, open, close, source FROM daily_bars "
        "WHERE code='IX0001' AND date BETWEEN ? AND ? AND open>0 AND close>0",
        (lo, hi),
    ):
        r = rank.get(str(src), 9)
        if str(d) not in best or r < best[str(d)]:
            best[str(d)] = r
            ix[str(d)] = (float(o), float(c))

    log("載入分點交易 ...")
    seat_rows: dict[tuple[str, str, str], tuple[float, float]] = {}
    for tid in SEATS:
        for d, sid, b, s in conn.execute(
            "SELECT trade_date, stock_id, buy, sell FROM stock_broker_branch_daily "
            "WHERE securities_trader_id=? AND trade_date BETWEEN ? AND ?",
            (tid, "2024-05-01", hi),
        ):
            seat_rows[(tid, str(sid), str(d))] = (float(b or 0), float(s or 0))

    sids = sorted({k[1] for k in seat_rows})
    log(f"載入 {len(sids)} 檔股價 ...")
    bars: dict[str, dict[str, tuple]] = defaultdict(dict)
    for i in range(0, len(sids), 400):
        ch = sids[i : i + 400]
        ph = ",".join("?" * len(ch))
        for sid, d, o, h, lw, c, v, amt in conn.execute(
            f"SELECT stock_id, trade_date, open, high, low, close, volume, amount "
            f"FROM stock_daily_bars WHERE source=? AND stock_id IN ({ph}) "
            f"AND trade_date BETWEEN ? AND ? AND close>0",
            (SOURCE, *ch, lo, hi),
        ):
            bars[str(sid)][str(d)] = (
                float(o or 0), float(h or 0), float(lw or 0), float(c),
                float(v or 0), float(amt or 0),
            )
    conn.close()

    dates_of = {s: sorted(v) for s, v in bars.items()}
    idx_of = {s: {d: i for i, d in enumerate(ds)} for s, ds in dates_of.items()}

    # 先組原始事件（未加 PIT flip 傾向），再依時間排序做擴張窗
    raw: list[dict] = []
    thr = args.min_amt_yi * 1e8
    for (tid, sid, d), (b, s) in seat_rows.items():
        if b <= 0:
            continue
        ds = dates_of.get(sid)
        if not ds:
            continue
        i = idx_of[sid].get(d)
        if i is None or i < 21 or i + 4 >= len(ds):
            continue
        o0, h0, l0, c0, v0, amt0 = bars[sid][d]
        amt_buy = b * c0
        if amt_buy < thr:
            continue
        d1 = ds[i + 1]
        _, ns = seat_rows.get((tid, sid, d1), (0.0, 0.0))
        flip = ns / b

        o1, h1, l1, c1, v1, _ = bars[sid][d1]
        if o1 <= 0 or d1 not in ix:
            continue
        gap = o1 / c0 - 1
        prev_c = bars[sid][ds[i - 1]][3]
        vols = [bars[sid][x][4] for x in ds[i - 20 : i]]
        avg_v = mean(vols) if vols else 0.0
        amts = [bars[sid][x][5] for x in ds[i - 20 : i]]
        adv = mean(amts) if amts else 0.0

        ix_o1, ix_c1 = ix[d1]
        rets = {}
        for hnm, k in (("1d", 1), ("2d", 2), ("3d", 3)):
            dk = ds[i + k]
            if dk not in ix:
                rets = {}
                break
            rets[hnm] = (
                bars[sid][dk][3] / o1 - 1,
                ix[dk][1] / ix_o1 - 1,
            )
        if not rets:
            continue

        raw.append(
            dict(
                tid=tid, sid=sid, date=d, d1=d1, flip=flip,
                amt_buy_yi=amt_buy / 1e8,
                buy_share=(b / v0 if v0 > 0 else 0.0),
                amt_share=(amt_buy / adv if adv > 0 else 0.0),
                adv_yi=adv / 1e8, gap=gap,
                ret0=(c0 / prev_c - 1 if prev_c else 0.0),
                limit_up=(c0 / prev_c - 1) >= 0.095 if prev_c else False,
                ret5=c0 / bars[sid][ds[i - 5]][3] - 1,
                rvol=(v0 / avg_v if avg_v > 0 else 0.0),
                buy_shares=b,
                mae=(h1 / o1 - 1),          # 空單當日最大不利（T+1 高點）
                mfe=(l1 / o1 - 1),          # 空單當日最大有利（T+1 低點）
                v1=v1,
                rets=rets,
            )
        )

    # PIT 擴張窗：席位歷史中位 flip（僅用事件日之前的樣本）
    raw.sort(key=lambda e: e["date"])
    hist: dict[str, list[float]] = defaultdict(list)
    out: list[dict] = []
    for e in raw:
        h = hist[e["tid"]]
        e["pit_seat_flip"] = median(h) if len(h) >= 30 else None
        h.append(e["flip"])
        if e["date"] >= args.start:
            out.append(e)
    log(f"事件 n={len(out):,}（PIT 席位傾向可用 {sum(1 for e in out if e['pit_seat_flip'] is not None):,}）")
    return out


def short_ret(e: dict, horizon: str, beta: float, cost: float) -> float:
    rs, ri = e["rets"][horizon]
    return -rs + beta * ri - cost


def stats(rows: list[dict], horizon: str, beta: float, cost: float) -> dict:
    if not rows:
        return dict(n=0)
    xs = [short_ret(e, horizon, beta, cost) for e in rows]
    m = mean(xs)
    sd = pstdev(xs) or 1e-9
    return dict(
        n=len(rows),
        med=round(100 * median(xs), 2),
        mean=round(100 * m, 2),
        sd=round(100 * sd, 2),
        win=round(100 * mean([x > 0 for x in xs]), 1),
        p10=round(100 * q(xs, 0.10), 2),
        p90=round(100 * q(xs, 0.90), 2),
        sharpe_per_trade=round(m / sd, 3),
        kelly=round(m / (sd * sd), 2),
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--beta", type=float, default=1.5)
    ap.add_argument("--cost", type=float, default=0.003)
    ap.add_argument("--start", default="2024-07-01")
    ap.add_argument("--min-amt-yi", type=float, default=0.3)
    args = ap.parse_args()

    ev = build(args)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "events.json").write_text(
        json.dumps(ev, ensure_ascii=False), encoding="utf-8"
    )

    B, C = args.beta, args.cost
    res: dict = dict(beta=B, cost=C, n=len(ev))

    # 1) 跳空門檻 × 持有天期
    gaps = [(-9, 0.0, "<0%"), (0.0, 0.01, "0~1%"), (0.01, 0.02, "1~2%"),
            (0.02, 0.03, "2~3%"), (0.03, 0.05, "3~5%"), (0.05, 0.07, "5~7%"),
            (0.07, 9, ">=7%")]
    tbl = []
    for lo_, hi_, nm in gaps:
        sub = [e for e in ev if lo_ <= e["gap"] < hi_]
        row = dict(gap=nm)
        for h in ("1d", "2d", "3d"):
            row[h] = stats(sub, h, B, C)
        tbl.append(row)
    res["by_gap"] = tbl

    # 2) IS / OOS 時間切
    is_ = [e for e in ev if e["date"] < OOS_START]
    oos = [e for e in ev if e["date"] >= OOS_START]
    res["is_oos"] = []
    for lo_, hi_, nm in gaps:
        res["is_oos"].append(dict(
            gap=nm,
            IS=stats([e for e in is_ if lo_ <= e["gap"] < hi_], "1d", B, C),
            OOS=stats([e for e in oos if lo_ <= e["gap"] < hi_], "1d", B, C),
        ))

    # 3) 在 gap>=3% 內，用 T0 買進金額 / 佔比 再切
    base = [e for e in ev if e["gap"] >= 0.03]
    res["gap3_by_amt"] = [
        dict(bucket=nm, **stats([e for e in base if lo_ <= e["amt_buy_yi"] < hi_], "1d", B, C))
        for lo_, hi_, nm in [(0.3, 0.5, "0.3~0.5億"), (0.5, 1.0, "0.5~1億"),
                             (1.0, 2.0, "1~2億"), (2.0, 99, ">=2億")]
    ]
    res["gap3_by_share"] = [
        dict(bucket=nm, **stats([e for e in base if lo_ <= e["buy_share"] < hi_], "1d", B, C))
        for lo_, hi_, nm in [(0, 0.01, "<1%"), (0.01, 0.03, "1~3%"),
                             (0.03, 0.08, "3~8%"), (0.08, 9, ">=8%")]
    ]
    res["gap3_by_amt_share"] = [
        dict(bucket=nm, **stats([e for e in base if lo_ <= e["amt_share"] < hi_], "1d", B, C))
        for lo_, hi_, nm in [(0, 0.02, "<2%ADV"), (0.02, 0.05, "2~5%"),
                             (0.05, 0.12, "5~12%"), (0.12, 9, ">=12%")]
    ]
    res["gap3_by_pitflip"] = [
        dict(bucket=nm, **stats([e for e in base
                                 if e["pit_seat_flip"] is not None
                                 and lo_ <= e["pit_seat_flip"] < hi_], "1d", B, C))
        for lo_, hi_, nm in [(0, 0.2, "席位傾向<0.2"), (0.2, 0.4, "0.2~0.4"),
                             (0.4, 9, ">=0.4")]
    ]

    # 4) 停損：以 T+1 日內高點估算觸價率與去尾後報酬
    stop_tbl = []
    for stop in (0.01, 0.02, 0.03, 0.05, 99.0):
        rows = []
        for e in base:
            r = short_ret(e, "1d", B, C)
            if stop < 90 and e["mae"] >= stop:
                r = -stop - C  # 觸價停損（保守：不計指數對沖當日收益）
            rows.append(r)
        m, sd = mean(rows), pstdev(rows) or 1e-9
        stop_tbl.append(dict(
            stop=("無" if stop > 90 else f"{stop:.0%}"),
            hit=round(100 * mean([e["mae"] >= stop for e in base]), 1) if stop < 90 else 0.0,
            mean=round(100 * m, 2), med=round(100 * median(rows), 2),
            sd=round(100 * sd, 2), win=round(100 * mean([x > 0 for x in rows]), 1),
            sharpe_per_trade=round(m / sd, 3),
        ))
    res["stops_gap3"] = stop_tbl

    # 5) 容量：空單相對 T+1 成交量／預估倒貨供給
    res["capacity"] = dict(
        med_v1_shares=round(median([e["v1"] for e in base])),
        med_buy_shares=round(median([e["buy_shares"] for e in base])),
        med_buy_share_of_v1=round(100 * median([e["buy_shares"] / e["v1"]
                                                for e in base if e["v1"] > 0]), 2),
        med_adv_yi=round(median([e["adv_yi"] for e in base]), 1),
    )

    (OUT / f"short_sizing_beta{B}.json").write_text(
        json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(json.dumps(res, ensure_ascii=False, indent=1))
    log(f"→ {OUT}")


if __name__ == "__main__":
    main()
