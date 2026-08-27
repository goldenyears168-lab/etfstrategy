#!/usr/bin/env python3
"""隔日沖開高做空 v2：相對台指跳空 · 個股期貨標的 · 排除權值股（research only）.

三項修正（使用者 2026-08-07 指示）：
  1. 觸發價改為「相對台指」而非相對 T0 收盤 → rel_gap = (1+個股跳空)/(1+指數跳空)-1
  2. 標的限縮為期交所個股期貨標的（成本低、可快速脫手、可空可留倉）
  3. 排除大型權值股（mega_blacklist_v1.json）

  PYTHONPATH=src .venv/bin/python scripts/research/run_dayflip_relgap_futures_short.py
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from statistics import mean, median, pstdev

import stock_db

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / "reports/research/branch-footprint-screen"
OUT = BASE / "dayflip_gapup_short"
EVENTS = OUT / "events.json"
FUT = OUT / "stock_futures_universe.json"
MEGA = BASE / "ab58_xMega_copytrade/mega_blacklist_v1.json"

BETA = 1.5
COST_FUT = 0.0005   # 個股期貨來回：期交稅 0.002%x2 + 手續費 ≈ 2~3bps，取 5bps 保守
COST_EQ = 0.003     # 現股當沖對照


def log(m: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


def daystats(rows: list[tuple[str, float]]) -> dict:
    if len(rows) < 30:
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
    ev = json.loads(EVENTS.read_text())
    futmap = json.loads(FUT.read_text())["map"]
    mega = set(json.loads(MEGA.read_text())["symbols"])
    log(f"個股期貨標的 {len(futmap)} 檔 · mega 排除 {len(mega)} 檔")

    con = sqlite3.connect(f"file:{stock_db.DEFAULT_DB_PATH}?mode=ro", uri=True)
    ix: dict[str, tuple[float, float]] = {}
    best: dict[str, int] = {}
    rank = {"yahoo": 0, "tej": 1, "finmind": 2}
    for d, o, c, src in con.execute(
        "SELECT date, open, close, source FROM daily_bars "
        "WHERE code='IX0001' AND date BETWEEN '2024-05-01' AND '2026-07-20' "
        "AND open>0 AND close>0"
    ):
        r = rank.get(str(src), 9)
        if str(d) not in best or r < best[str(d)]:
            best[str(d)] = r
            ix[str(d)] = (float(o), float(c))
    con.close()

    ixd = sorted(ix)
    ixi = {d: i for i, d in enumerate(ixd)}
    # 指數跳空 = 當日 open / 前一日 close - 1
    ix_gap = {ixd[i]: ix[ixd[i]][0] / ix[ixd[i - 1]][1] - 1 for i in range(1, len(ixd))}

    pool = []
    for e in ev:
        if e["d1"] not in ix_gap:
            continue
        g = ix_gap[e["d1"]]
        e["ix_gap"] = g
        e["rel_gap"] = (1 + e["gap"]) / (1 + g) - 1
        e["has_fut"] = e["sid"] in futmap
        e["is_mega"] = e["sid"] in mega
        pool.append(e)

    def leg(e, cost, beta=BETA):
        rs, ri = e["rets"]["1d"]
        return -rs + beta * ri - cost

    def stock_leg(e, cost):
        return -e["rets"]["1d"][0] - cost

    res: dict = {}

    # ---- 1. 宇宙過濾的影響（門檻固定用 rel_gap>=7%）----
    log("宇宙過濾影響 ...")
    universes = [
        ("全部", lambda e: True),
        ("排除 mega", lambda e: not e["is_mega"]),
        ("僅個股期貨標的", lambda e: e["has_fut"]),
        ("個股期貨 且 排除 mega", lambda e: e["has_fut"] and not e["is_mega"]),
    ]
    res["universe"] = []
    for nm, f in universes:
        sub = [e for e in pool if f(e) and e["rel_gap"] >= 0.07]
        res["universe"].append(dict(
            universe=nm,
            **daystats([(e["d1"], leg(e, COST_FUT)) for e in sub]),
            stocks=len({e["sid"] for e in sub}),
        ))

    # ---- 2. 相對台指 vs 相對前收：同一宇宙下的門檻掃描 ----
    log("門檻掃描：rel_gap vs raw gap ...")
    U = lambda e: e["has_fut"] and not e["is_mega"]  # noqa: E731
    res["threshold"] = []
    for x in (0.03, 0.05, 0.06, 0.07, 0.08, 0.09):
        a = [e for e in pool if U(e) and e["rel_gap"] >= x]
        b = [e for e in pool if U(e) and e["gap"] >= x]
        res["threshold"].append(dict(
            x=f"{x:.0%}",
            rel=daystats([(e["d1"], leg(e, COST_FUT)) for e in a]),
            raw=daystats([(e["d1"], leg(e, COST_FUT)) for e in b]),
            rel_stock=daystats([(e["d1"], stock_leg(e, COST_FUT)) for e in a]),
            raw_stock=daystats([(e["d1"], stock_leg(e, COST_FUT)) for e in b]),
        ))

    # ---- 3. 成本口徑：期貨 vs 現股 ----
    log("成本口徑 ...")
    res["cost"] = []
    for x in (0.05, 0.07):
        sub = [e for e in pool if U(e) and e["rel_gap"] >= x]
        for cnm, c in (("個股期貨 5bps", COST_FUT), ("現股當沖 30bps", COST_EQ)):
            res["cost"].append(dict(
                rule=f"rel_gap>={x:.0%} · {cnm}",
                **daystats([(e["d1"], leg(e, c)) for e in sub]),
            ))

    # ---- 4. IS / OOS ----
    log("IS/OOS ...")
    res["is_oos"] = []
    for x in (0.05, 0.07):
        sub = [e for e in pool if U(e) and e["rel_gap"] >= x]
        res["is_oos"].append(dict(
            rule=f"rel_gap>={x:.0%}",
            IS=daystats([(e["d1"], leg(e, COST_FUT)) for e in sub if e["date"] < "2026-01-01"]),
            OOS=daystats([(e["d1"], leg(e, COST_FUT)) for e in sub if e["date"] >= "2026-01-01"]),
        ))

    # ---- 5. 指數跳空自身的方向（rel 定義是否真的排除了大盤影響）----
    log("指數跳空分層 ...")
    res["by_ixgap"] = []
    for lo, hi, nm in ((-9, -0.003, "大盤開低<-0.3%"), (-0.003, 0.003, "大盤平盤"),
                       (0.003, 0.01, "大盤開高0.3~1%"), (0.01, 9, "大盤開高>1%")):
        for tag, key in (("rel", "rel_gap"), ("raw", "gap")):
            sub = [e for e in pool if U(e) and lo <= e["ix_gap"] < hi and e[key] >= 0.07]
            res["by_ixgap"].append(dict(
                bucket=f"{nm} · {tag}>=7%",
                **daystats([(e["d1"], leg(e, COST_FUT)) for e in sub]),
            ))

    # ---- 6. 依分點買量微調門檻 ----
    log("分點買量微調 ...")
    res["adaptive"] = []
    for base, k in ((0.07, 0.0), (0.07, 0.2), (0.06, 0.2), (0.05, 0.2), (0.07, -0.2)):
        sub = [e for e in pool if U(e)
               and e["rel_gap"] >= max(0.02, min(0.12, base + k * min(e["amt_share"], 0.3)))]
        res["adaptive"].append(dict(
            rule=f"rel_gap >= {base:.0%} + {k} × 佔ADV",
            **daystats([(e["d1"], leg(e, COST_FUT)) for e in sub]),
        ))

    (OUT / "relgap_futures.json").write_text(
        json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    for sec, rows in res.items():
        print(f"\n=== {sec} ===")
        for r in rows if isinstance(rows, list) else []:
            print("  " + json.dumps(r, ensure_ascii=False))
    log(f"→ {OUT/'relgap_futures.json'}")


if __name__ == "__main__":
    main()
