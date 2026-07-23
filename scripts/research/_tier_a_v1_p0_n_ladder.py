#!/usr/bin/env python3
"""Thin P0 n-ladder note for Tier A v1 funnel (local floor window).

Research only · 未採納 · MacBook only.

    PYTHONPATH=src .venv/bin/python scripts/research/_tier_a_v1_p0_n_ladder.py --sid 3037
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from research.chen_chip.adapters_db import (  # noqa: E402
    connect_ro,
    load_bench,
    load_calendar,
    load_ohlc,
)
from research.chen_chip.features import build_chip_feature_frame  # noqa: E402
from research.chen_chip.whale_events import load_whale_events  # noqa: E402
from stock_db import DEFAULT_DB_PATH  # noqa: E402

OUT = ROOT / "reports/research/whale_chip_precursor"
CACHE = OUT / "cache"
TRADES_DIR = (
    ROOT
    / "reports/research/branch-footprint-screen/expert_pool/knowledge/trades"
)
D0, D1, OOS = "2024-07-01", "2026-07-16", "2026-01-01"
COST, BETA, H = 0.003, 1.15, 7
FLOOR_0P5, FLOOR_1 = 5e7, 1e8
FOREIGN_KW = (
    "美商",
    "摩根",
    "瑞銀",
    "美林",
    "港商",
    "港麥",
    "麥格理",
    "野村",
    "瑞士",
    "花旗",
    "渣打",
    "香港",
    "新加坡",
    "大和",
    "法銀",
    "德意志",
    "巴克萊",
    "高盛",
    "美銀",
)


def is_foreign(name: str) -> bool:
    return any(k in (name or "") for k in FOREIGN_KW)


def excess_l1(
    sid: str,
    signal_d: str,
    hold: int,
    cal: list[str],
    di: dict[str, int],
    ohlc_px: dict[tuple[str, str], tuple[float, float]],
    bench: dict[str, float],
) -> float | None:
    i = di.get(signal_d)
    if i is None or i + 1 >= len(cal) or i + hold >= len(cal):
        return None
    ed, xd = cal[i + 1], cal[i + hold]
    er, xr = ohlc_px.get((sid, ed)), ohlc_px.get((sid, xd))
    be, bx = bench.get(ed), bench.get(xd)
    if not er or not xr or not be or not bx:
        return None
    eo, _ = er
    _, xp = xr
    if eo <= 0 or xp <= 0 or be <= 0 or bx <= 0:
        return None
    sr = xp / eo - 1.0 - COST
    br = bx / be - 1.0
    return (sr - BETA * br) * 100.0


def nonoverlap_n(
    fires: set[str],
    sid: str,
    cal: list[str],
    di: dict[str, int],
    ohlc_px: dict[tuple[str, str], tuple[float, float]],
    bench: dict[str, float],
) -> int:
    ordered = sorted(d for d in fires if d in di)
    n = 0
    next_ok_i = -1
    for d in ordered:
        i = di[d]
        if i < next_ok_i:
            continue
        if excess_l1(sid, d, H, cal, di, ohlc_px, bench) is None:
            continue
        n += 1
        next_ok_i = i + H
    return n


def atom_ok(row: pd.Series, atom_id: str) -> bool:
    if atom_id in ("ts1", "ts2", "ts3"):
        return float(row.get("three_streak", 0) or 0) >= int(atom_id[-1])
    if atom_id == "bhchg":
        v = row.get("big_holder_pct_chg", np.nan)
        return bool(pd.notna(v) and float(v) > 0)
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sid", required=True)
    ap.add_argument("--d1", default=D1)
    args = ap.parse_args()
    sid = str(args.sid)
    d1 = str(args.d1)

    doc = json.loads((TRADES_DIR / f"{sid}.json").read_text(encoding="utf-8"))
    core = {str(k): str(v) for k, v in (doc.get("core") or {}).items()}
    if not core:
        raise SystemExit(f"missing core seats in trades/{sid}.json")
    name = str(doc.get("stock_name") or sid)
    champ = set(core.keys())

    conn = connect_ro(DEFAULT_DB_PATH)
    whale = load_whale_events([sid])
    whale = whale[(whale["signal_date"] >= D0) & (whale["signal_date"] <= d1)].copy()
    greens = sorted(whale["signal_date"].astype(str).tolist())

    cal = [d for d in load_calendar(conn, D0, d1) if D0 <= d <= d1]
    cal_ext = load_calendar(conn, D0, "2026-08-31")
    di = {d: i for i, d in enumerate(cal_ext)}
    bench = load_bench(conn, D0, d1)
    ohlc = load_ohlc(conn, [sid], D0, d1)
    ohlc_px = {
        (str(r.sid), str(r.d)): (float(r.open), float(r.close))
        for r in ohlc.itertuples()
    }
    px_close = {(str(r.sid), str(r.d)): float(r.close) for r in ohlc.itertuples()}

    hs = CACHE / "holding_shares_per_tier_a.csv"
    gov = CACHE / "gov_bank_net_tier_a.csv"
    feat = build_chip_feature_frame(
        conn,
        [sid],
        D0,
        d1,
        holding_shares_path=hs if hs.exists() else None,
        gov_bank_path=gov if gov.exists() else None,
    )
    feat = feat.sort_values(["sid", "d"]).copy()
    for col_src, col_out, w in (
        ("foreign_net", "foreign_sum_3d", 3),
        ("three_buy_net", "three_sum_3d", 3),
    ):
        if col_src in feat.columns:
            feat[col_out] = feat.groupby("sid")[col_src].transform(
                lambda s: s.rolling(w, min_periods=1).sum()
            )

    parts: list[pd.DataFrame] = []
    chunk = 40
    for i in range(0, len(cal), chunk):
        batch = cal[i : i + chunk]
        ph = ",".join("?" * len(batch))
        df = pd.read_sql_query(
            f"""
            SELECT trade_date AS d, stock_id AS sid,
                   securities_trader_id AS tid, securities_trader AS tname,
                   buy, sell, net
            FROM stock_broker_branch_daily
            WHERE source=? AND stock_id=? AND trade_date IN ({ph})
            """,
            conn,
            params=["finmind", sid, *batch],
        )
        if not df.empty:
            parts.append(df)
    conn.close()
    if not parts:
        raise SystemExit(f"empty branch tape for {sid} in {D0}..{d1}")
    tape = pd.concat(parts, ignore_index=True)
    tape["d"] = tape["d"].astype(str)
    tape["tid"] = tape["tid"].astype(str)
    tape["sid"] = tape["sid"].astype(str)
    tape["net"] = pd.to_numeric(tape["net"], errors="coerce").fillna(0.0)
    tape["close"] = [
        px_close.get((str(r.sid), str(r.d)), np.nan) for r in tape.itertuples()
    ]
    tape["amt"] = tape["net"] * tape["close"]
    tape["is_foreign"] = tape["tname"].map(is_foreign)
    dom = tape[~tape["is_foreign"]].copy()

    by_day = dom.groupby("d")["amt"].sum()
    any_net = set(by_day[by_day > 0].index.astype(str))
    any_0p5 = set(by_day[by_day >= FLOOR_0P5].index.astype(str))
    any_1 = set(by_day[by_day >= FLOOR_1].index.astype(str))

    champ_day = dom[dom["tid"].isin(champ)].groupby("d")["amt"].sum()
    champ_net = set(champ_day[champ_day > 0].index.astype(str))
    champ_0p5 = set(champ_day[champ_day >= FLOOR_0P5].index.astype(str))
    champ_1 = set(champ_day[champ_day >= FLOOR_1].index.astype(str))

    feat_idx = feat.set_index(["sid", "d"])
    chip_ts1_bh: set[str] = set()
    chip_ts2: set[str] = set()
    for d in cal:
        try:
            row = feat_idx.loc[(sid, d)]
        except KeyError:
            continue
        if isinstance(row, pd.DataFrame):
            row = row.iloc[0]
        if atom_ok(row, "ts1") and atom_ok(row, "bhchg"):
            chip_ts1_bh.add(d)
        if atom_ok(row, "ts2"):
            chip_ts2.add(d)

    chip_and_champ = chip_ts1_bh & champ_0p5
    chip_only_n = nonoverlap_n(chip_ts1_bh, sid, cal_ext, di, ohlc_px, bench)
    chip_champ_n = nonoverlap_n(chip_and_champ, sid, cal_ext, di, ohlc_px, bench)

    hard_stop = None
    if len(greens) < 5:
        hard_stop = "cannot_proceed vs Whale (greens n<5)"
    elif chip_champ_n < 8 and chip_only_n < 8:
        hard_stop = "sample_too_thin (standalone nonoverlap H7 n<8 for chip / chip∧champ)"

    status = "proceed" if hard_stop is None else "hard_stop"

    rows = [
        ("calendar trading days", len(cal), None),
        ("domestic branch 任一日 net>0", len(any_net), None),
        ("domestic branch 任一日 ≥0.5億", len(any_0p5), None),
        ("domestic branch 任一日 ≥1億", len(any_1), None),
        (f"champion {len(champ)}席 net>0", len(champ_net), None),
        (f"champion {len(champ)}席 ≥0.5億", len(champ_0p5), None),
        (f"champion {len(champ)}席 ≥1億", len(champ_1), None),
        ("chip fire ts1+bhchg（日曆）", len(chip_ts1_bh), chip_only_n),
        ("chip fire ts2（日曆）", len(chip_ts2), nonoverlap_n(chip_ts2, sid, cal_ext, di, ohlc_px, bench)),
        ("chip ∧ champ≥0.5億 同日", len(chip_and_champ), chip_champ_n),
        ("Whale greens（external only）", len(greens), len(greens)),
        ("standalone chip-only → D+1（非重疊 H7）", chip_only_n, chip_only_n),
        ("standalone chip∧champ≥0.5 → D+1（非重疊 H7）", chip_champ_n, chip_champ_n),
    ]

    core_line = " · ".join(f"{k} {v}" for k, v in core.items())
    md = [
        f"# {sid} {name} · P0 n-ladder（v1 薄漏斗）",
        "",
        f"- 生成：{datetime.now():%Y-%m-%d %H:%M}",
        f"- 窗：`{D0}..{d1}` · OOS=`{OOS}` · H={H} · cost={COST} · β={BETA}",
        "- 資料窗：本機 branch floor（**無** FinMind 擴窗）",
        "- Research only · **未採納** · MacBook only",
        "",
        "## 可行性（漏斗聲明）",
        "",
        "```text",
        "Doable as a repeatable research funnel per sid.",
        "NOT doable as a promise that every Tier A beats Whale T+1 —",
        "sample sizes vary (2327 greens≈12; some Tier A thinner).",
        "Encode hard stop / \"cannot beat\" outcomes like the 2327 study.",
        "```",
        "",
        "## 一句話",
        "",
        (
            f"**P0 {status}** · Whale greens n={len(greens)} · "
            f"chip∧champ 非重疊 H7≈{chip_champ_n} · "
            f"chip-only 非重疊 H7≈{chip_only_n}"
            + (f" · **{hard_stop}**" if hard_stop else " · 可進 P2/P3")
        ),
        "",
        "## Core seats",
        "",
        core_line,
        "",
        "## 樣本階梯",
        "",
        "| definition | n_calendar | n_nonoverlap_H7 |",
        "|------------|----------:|----------------:|",
    ]
    for a, b, c in rows:
        md.append(f"| {a} | **{b}** | {'' if c is None else c} |")

    md += [
        "",
        "## P0 硬停檢查",
        "",
        f"| 檢查 | 值 | 門檻 | 結果 |",
        f"|------|---:|-----:|------|",
        f"| Whale greens | {len(greens)} | ≥5 | "
        f"{'OK' if len(greens) >= 5 else 'STOP'} |",
        f"| chip∧champ 非重疊 H7 | {chip_champ_n} | ≥8（或 chip-only≥8） | "
        f"{'OK' if (chip_champ_n >= 8 or chip_only_n >= 8) else 'STOP'} |",
        f"| trades core | {len(champ)} | 非空 | OK |",
        "",
        f"**Verdict:** `{status}`"
        + (f" — {hard_stop}" if hard_stop else " — proceed to P2 sequence → P3 standalone"),
        "",
        f"- Whale_T+1 = 外部基準臂 only（n={len(greens)}）；禁止用綠燈子集當挖礦母體",
        "",
    ]

    path = OUT / f"{sid}_N_DIAGNOSTIC.md"
    OUT.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(md), encoding="utf-8")
    print(json.dumps({
        "sid": sid,
        "name": name,
        "status": status,
        "hard_stop": hard_stop,
        "greens": len(greens),
        "chip_only_H7": chip_only_n,
        "chip_champ_H7": chip_champ_n,
        "path": str(path),
    }, ensure_ascii=False))
    return 0 if status == "proceed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
