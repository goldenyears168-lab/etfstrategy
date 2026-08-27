#!/usr/bin/env python3
"""分點「隔日沖」事件的 T0 共同特徵剖析（research only）.

事件 = 已知 Top 分點於 T 日單股買進金額 >= 門檻。
flip_ratio = sell_shares(T+1) / buy_shares(T)（沿用 LEADERBOARD_no_dayflip 定義）。
超額 = stock - BETA x IX0001（BETA 由 --beta 指定；使用者要求 1.5，repo 既有協議為 1.15）。

  PYTHONPATH=src .venv/bin/python scripts/research/run_branch_dayflip_feature_profile.py
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from statistics import median, mean

import stock_db

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "reports/research/branch-footprint-screen/dayflip_feature_profile"
SOURCE = "finmind"
COST = 0.003

# 16 隔日沖偏好席（LEADERBOARD_no_dayflip）+ focus9 專家席
SEATS = {
    "920M": "凱基-宜蘭", "7008": "兆豐-三重", "989g": "元大-嘉義", "981j": "元大-士林",
    "5851": "統一-高雄", "9217": "凱基松山", "980h": "元大-台北", "918e": "群益金鼎-大安",
    "989X": "元大-民生三民", "779Z": "國票安和", "913R": "群益金鼎-北高雄", "9661": "富邦新店",
    "585Y": "統一土城", "9875": "元大-土城永寧", "918X": "群益金鼎-台北", "5383": "第一金-高雄",
    "1360": "港麥格理", "9A9R": "永豐金信義", "9325": "華南永昌-忠孝", "9A81": "永豐金-匯立",
    "779n": "國票南京", "9227": "凱基城中", "9216": "凱基-信義", "920F": "凱基-站前",
}
DAYFLIP_16 = {
    "920M", "7008", "989g", "981j", "5851", "9217", "980h", "918e",
    "989X", "779Z", "913R", "9661", "585Y", "9875", "918X", "5383",
}


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def connect() -> sqlite3.Connection:
    return sqlite3.connect(f"file:{stock_db.DEFAULT_DB_PATH}?mode=ro", uri=True)


def pct(x: float) -> float:
    return round(100.0 * x, 2)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--beta", type=float, default=1.5)
    ap.add_argument("--start", default="2024-07-01")
    ap.add_argument("--end", default="2026-07-08")
    ap.add_argument("--min-amt-yi", type=float, default=0.3)
    ap.add_argument("--hold", type=int, default=7)
    args = ap.parse_args()

    conn = connect()
    lo = "2024-05-01"  # 前置緩衝供 20 日特徵
    hi = "2026-07-20"

    log("載入 IX0001 ...")
    ix: dict[str, tuple[float, float]] = {}
    rank = {"yahoo": 0, "tej": 1, "finmind": 2}
    best: dict[str, int] = {}
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
            (tid, args.start, hi),
        ):
            seat_rows[(tid, str(sid), str(d))] = (float(b or 0), float(s or 0))
    log(f"  分點列 {len(seat_rows):,}")

    sids = sorted({k[1] for k in seat_rows})
    log(f"載入 {len(sids)} 檔股價 ...")
    bars: dict[str, dict[str, tuple]] = defaultdict(dict)
    CH = 400
    for i in range(0, len(sids), CH):
        chunk = sids[i : i + CH]
        q = ",".join("?" * len(chunk))
        for sid, d, o, h, lw, c, v, amt in conn.execute(
            f"SELECT stock_id, trade_date, open, high, low, close, volume, amount "
            f"FROM stock_daily_bars WHERE source=? AND stock_id IN ({q}) "
            f"AND trade_date BETWEEN ? AND ? AND close>0",
            (SOURCE, *chunk, lo, hi),
        ):
            bars[str(sid)][str(d)] = (
                float(o or 0), float(h or 0), float(lw or 0), float(c),
                float(v or 0), float(amt or 0),
            )
    conn.close()

    # 每檔股票的交易日序列
    dates_of: dict[str, list[str]] = {s: sorted(v) for s, v in bars.items()}
    idx_of: dict[str, dict[str, int]] = {
        s: {d: i for i, d in enumerate(ds)} for s, ds in dates_of.items()
    }
    ix_dates = sorted(ix)
    ix_idx = {d: i for i, d in enumerate(ix_dates)}

    events: list[dict] = []
    thr = args.min_amt_yi * 1e8
    for (tid, sid, d), (b, s) in seat_rows.items():
        if b <= 0 or d < args.start or d > args.end:
            continue
        ds = dates_of.get(sid)
        if not ds:
            continue
        i = idx_of[sid].get(d)
        if i is None or i < 21 or i + args.hold + 1 >= len(ds):
            continue
        o0, h0, l0, c0, v0, amt0 = bars[sid][d]
        amt_buy = b * c0
        if amt_buy < thr:
            continue
        d1 = ds[i + 1]
        nb, ns = seat_rows.get((tid, sid, d1), (0.0, 0.0))
        flip = ns / b if b > 0 else 0.0

        prev_c = bars[sid][ds[i - 1]][3]
        ret0 = c0 / prev_c - 1 if prev_c else 0.0
        ret3 = c0 / bars[sid][ds[i - 3]][3] - 1
        ret5 = c0 / bars[sid][ds[i - 5]][3] - 1
        ret20 = c0 / bars[sid][ds[i - 20]][3] - 1
        vols = [bars[sid][x][4] for x in ds[i - 20 : i]]
        avg_v = mean(vols) if vols else 0.0
        rvol = v0 / avg_v if avg_v > 0 else 0.0
        amts = [bars[sid][x][5] for x in ds[i - 20 : i]]
        adv = mean(amts) if amts else 0.0
        hi20 = max(bars[sid][x][1] for x in ds[i - 20 : i])
        rng = (h0 - l0) / prev_c if prev_c else 0.0
        pos = (c0 - l0) / (h0 - l0) if h0 > l0 else 0.5
        buy_share = b / v0 if v0 > 0 else 0.0
        net_ratio = (b - s) / (b + s) if (b + s) > 0 else 0.0

        # 出場：T+1 open -> T+hold close，超額 = stock - beta * IX
        o1 = bars[sid][d1][0]
        c_h = bars[sid][ds[i + args.hold]][3]
        if o1 <= 0:
            continue
        gap = o1 / c0 - 1
        r1d = bars[sid][d1][3] / o1 - 1
        rh = c_h / o1 - 1
        j = ix_idx.get(d1)
        k = ix_idx.get(ds[i + args.hold])
        if j is None or k is None:
            continue
        ix_o1 = ix[d1][0]
        ix_r1d = ix[d1][1] / ix_o1 - 1
        ix_rh = ix[ds[i + args.hold]][1] / ix_o1 - 1
        exc_h = rh - args.beta * ix_rh - COST
        exc_1d = r1d - args.beta * ix_r1d - COST

        events.append(
            dict(
                tid=tid, sid=sid, date=d, flip=flip, is_flip=flip >= 0.5,
                amt_buy_yi=amt_buy / 1e8, ret0=ret0, ret3=ret3, ret5=ret5, ret20=ret20,
                rvol=rvol, adv_yi=adv / 1e8, rng=rng, pos=pos, buy_share=buy_share,
                net_ratio=net_ratio, near_hi20=c0 / hi20 - 1, gap=gap,
                rh=rh, ix_rh=ix_rh, r1d=r1d, ix_r1d=ix_r1d,
                exc_h=exc_h, exc_1d=exc_1d, limit_up=ret0 >= 0.095, price=c0,
                seat_dayflip=tid in DAYFLIP_16,
            )
        )

    log(f"事件 n={len(events):,}")
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"events_beta{args.beta}.json").write_text(
        json.dumps(events, ensure_ascii=False), encoding="utf-8"
    )

    def summarize(rows: list[dict], label: str) -> dict:
        if not rows:
            return {}
        g = lambda k: [r[k] for r in rows]  # noqa: E731
        return dict(
            label=label, n=len(rows),
            flip=round(median(g("flip")), 3),
            ret0=pct(median(g("ret0"))), lim=pct(mean([r["limit_up"] for r in rows])),
            ret5=pct(median(g("ret5"))), ret20=pct(median(g("ret20"))),
            rvol=round(median(g("rvol")), 2), near_hi20=pct(median(g("near_hi20"))),
            rng=pct(median(g("rng"))), pos=round(median(g("pos")), 2),
            buy_share=pct(median(g("buy_share"))), net_ratio=round(median(g("net_ratio")), 2),
            amt=round(median(g("amt_buy_yi")), 2), adv=round(median(g("adv_yi")), 2),
            gap=pct(median(g("gap"))),
            exc1d=pct(median(g("exc_1d"))),
            exch=pct(median(g("exc_h"))),
            win=pct(mean([r["exc_h"] > 0 for r in rows])),
        )

    flips = [e for e in events if e["is_flip"]]
    holds = [e for e in events if e["flip"] < 0.25]
    mid = [e for e in events if 0.25 <= e["flip"] < 0.5]
    groups = [
        summarize(events, "全部事件"),
        summarize(flips, f"隔日沖 flip>=0.5"),
        summarize(mid, "中間 0.25-0.5"),
        summarize(holds, "抱股 flip<0.25"),
    ]
    print(json.dumps(groups, ensure_ascii=False, indent=1))

    # 特徵分層：在各特徵分位上 flip 發生率
    def bucket_rate(key: str, edges: list[float], names: list[str]) -> list[dict]:
        out = []
        for lo_, hi_, nm in zip([-9e9] + edges, edges + [9e9], names):
            sub = [e for e in events if lo_ <= e[key] < hi_]
            if len(sub) < 30:
                out.append(dict(bucket=nm, n=len(sub)))
                continue
            out.append(dict(
                bucket=nm, n=len(sub),
                flip_rate=pct(mean([e["is_flip"] for e in sub])),
                med_flip=round(median([e["flip"] for e in sub]), 3),
                exch=pct(median([e["exc_h"] for e in sub])),
                gap=pct(median([e["gap"] for e in sub])),
            ))
        return out

    tiers = {
        "ret0(T0日漲跌)": bucket_rate("ret0", [0.0, 0.03, 0.07, 0.095],
                                    ["<0%", "0~3%", "3~7%", "7~9.5%", ">=9.5%漲停"]),
        "rvol(量能倍數)": bucket_rate("rvol", [1.0, 2.0, 4.0],
                                    ["<1x", "1~2x", "2~4x", ">=4x"]),
        "ret5(近5日)": bucket_rate("ret5", [0.0, 0.05, 0.15],
                                  ["<0%", "0~5%", "5~15%", ">=15%"]),
        "buy_share(該席佔全日量)": bucket_rate("buy_share", [0.03, 0.08, 0.15],
                                          ["<3%", "3~8%", "8~15%", ">=15%"]),
        "adv(20日均額,億)": bucket_rate("adv_yi", [1.0, 3.0, 10.0],
                                     ["<1億", "1~3億", "3~10億", ">=10億"]),
        "pos(收盤位置)": bucket_rate("pos", [0.33, 0.67],
                                  ["下1/3", "中", "上1/3"]),
    }
    print(json.dumps(tiers, ensure_ascii=False, indent=1))
    (OUT / f"summary_beta{args.beta}.json").write_text(
        json.dumps(dict(groups=groups, tiers=tiers, beta=args.beta,
                        n=len(events), window=[args.start, args.end]),
                   ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    log(f"→ {OUT}")


if __name__ == "__main__":
    main()
