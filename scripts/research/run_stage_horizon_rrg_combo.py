#!/usr/bin/env python3
"""30W × 15W × RRG(L=20) 搭配回測（Research only · 未採納）.

接續 `run_stage_fast_combo_backtest.py`（10W 無增益）：
  - 中線：weinstein stage ma_period=15（stage_mid）
  - 相對強度：JdK RRG length=20（rs_ratio / rs_momentum · 四象限）

協議同 multiwave：HARD/OR 分池 · L1H7 · 8 檔 · 2024-07-01..2026-07-17 · B=2000
不覆寫 weinstein_stage(30W)。

Out: reports/research/chip-overlays/2327_ta_adaptive/stage_horizon_rrg/
"""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from rrg_rotation import classify_quadrant, rs_ratio_momentum  # noqa: E402
from stage_analysis import classify_weinstein_stage  # noqa: E402

UNIVERSE = ["2327", "2303", "2408", "6669", "2383", "3037", "3189", "2337"]
SOURCE = "finmind"
START, END = "2024-07-01", "2026-07-17"
COST, BETA = 0.003, 1.15
FLOOR = 5e7
BOOT = 2000
RRG_LEN = int(os.environ.get("RRG_LENGTH", "20"))
RNG = np.random.default_rng(3015)
DB = ROOT / "data" / "stocks.db"
OUT_NAME = "stage_horizon_rrg" if RRG_LEN == 20 else f"stage_horizon_rrg_wma{RRG_LEN}"
OUT = ROOT / f"reports/research/chip-overlays/2327_ta_adaptive/{OUT_NAME}"


def load_mod(rel_path: str, name: str):
    path = ROOT / rel_path
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    assert spec.loader
    spec.loader.exec_module(mod)
    return mod


def connect_ro() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{DB.resolve()}?mode=ro", uri=True, timeout=120)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=120000")
    return conn


def load_price_full(conn: sqlite3.Connection, sid: str) -> pd.DataFrame:
    rows = conn.execute(
        """
        SELECT trade_date, open, high, low, close, volume
        FROM stock_daily_bars
        WHERE stock_id=? AND source=? AND close>0 AND trade_date<=?
        ORDER BY trade_date
        """,
        (sid, SOURCE, END),
    ).fetchall()
    df = pd.DataFrame(
        [tuple(r) for r in rows],
        columns=["date", "open", "high", "low", "close", "volume"],
    )
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["open"] = df["open"].fillna(df["close"])
    df["high"] = df["high"].fillna(df["close"])
    df["low"] = df["low"].fillna(df["close"])
    return df


def load_ix_ohlc_full(conn: sqlite3.Connection) -> pd.DataFrame:
    rows = conn.execute(
        """
        SELECT date, open, high, low, close, volume, source
        FROM daily_bars
        WHERE code='IX0001' AND close>0 AND date<=?
        ORDER BY date,
          CASE source WHEN 'yahoo' THEN 0 WHEN 'tej' THEN 1 WHEN 'finmind' THEN 2 ELSE 3 END
        """,
        (END,),
    ).fetchall()
    best: dict[str, tuple] = {}
    for d, o, h, low, c, v, _src in rows:
        if d not in best:
            best[d] = (o, h, low, c, v)
    dates = sorted(best)
    df = pd.DataFrame(
        [(d, *best[d]) for d in dates],
        columns=["date", "open", "high", "low", "close", "volume"],
    )
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["open"] = df["open"].fillna(df["close"])
    df["high"] = df["high"].fillna(df["close"])
    df["low"] = df["low"].fillna(df["close"])
    return df


def weinstein_at(df: pd.DataFrame, sig_date: str, *, ma_period: int) -> int:
    hist = df[df["date"] <= sig_date].copy()
    if len(hist) < max(80, ma_period * 6):
        return 0
    wf = hist.rename(
        columns={
            "date": "Date",
            "open": "Open",
            "high": "High",
            "low": "Low",
            "close": "Close",
            "volume": "Volume",
        }
    )
    wf["Date"] = pd.to_datetime(wf["Date"])
    wf = wf.set_index("Date")
    try:
        r = classify_weinstein_stage(wf, ma_period=ma_period)
        return int(r.get("stage") or 0)
    except Exception:
        return 0


def build_rrg_series(
    stock_ohlc: pd.DataFrame, ix_ohlc: pd.DataFrame
) -> tuple[pd.Series, pd.Series]:
    a = stock_ohlc.set_index(pd.to_datetime(stock_ohlc["date"]))["close"].astype(float)
    b = ix_ohlc.set_index(pd.to_datetime(ix_ohlc["date"]))["close"].astype(float)
    return rs_ratio_momentum(a, b, length=RRG_LEN)


def rrg_at(rs_ratio: pd.Series, rs_mom: pd.Series, sig_date: str) -> dict:
    ts = pd.Timestamp(sig_date)
    # PIT: last available ≤ sig_date
    rr = rs_ratio.loc[:ts]
    mm = rs_mom.loc[:ts]
    if rr.empty or mm.empty or not np.isfinite(rr.iloc[-1]) or not np.isfinite(mm.iloc[-1]):
        return {"rs_ratio": None, "rs_mom": None, "quadrant": None}
    r, m = float(rr.iloc[-1]), float(mm.iloc[-1])
    return {"rs_ratio": r, "rs_mom": m, "quadrant": classify_quadrant(r, m)}


def stats(vals: list[float]) -> dict:
    a = np.array([v for v in vals if v is not None and np.isfinite(v)], dtype=float)
    if a.size == 0:
        return {"n": 0, "med": None, "mean": None, "win": None}
    return {
        "n": int(a.size),
        "med": float(np.median(a)),
        "mean": float(a.mean()),
        "win": float((a > 0).mean()),
    }


def two_sample_boot(kept: list[float], base: list[float], boot: int = BOOT) -> dict:
    k = np.array([v for v in kept if v is not None and np.isfinite(v)], float)
    b = np.array([v for v in base if v is not None and np.isfinite(v)], float)
    if k.size < 3 or b.size < 3:
        return {"delta_med": None, "ci": None, "p_pos": None}
    deltas = np.empty(boot)
    for i in range(boot):
        deltas[i] = np.median(k[RNG.integers(0, k.size, k.size)]) - np.median(
            b[RNG.integers(0, b.size, b.size)]
        )
    return {
        "delta_med": float(np.median(k) - np.median(b)),
        "ci": [float(np.quantile(deltas, 0.025)), float(np.quantile(deltas, 0.975))],
        "p_pos": float((deltas > 0).mean()),
    }


def _s(x) -> str:
    return f"{x:+.2f}" if isinstance(x, (int, float)) and np.isfinite(x) else "nan"


def _ci(b: dict) -> str:
    ci = b.get("ci")
    if not ci:
        return "n/a"
    return f"[{ci[0]:+.2f},{ci[1]:+.2f}]"


GATES = {
    "base": ("全綠燈", lambda r: True),
    "s30": ("weinstein 30W==2", lambda r: r["s30"] == 2),
    "s15": ("weinstein 15W==2 (mid)", lambda r: r["s15"] == 2),
    "both15": ("30W==2 ∧ 15W==2", lambda r: r["s30"] == 2 and r["s15"] == 2),
    "s30_not15_4": (
        "30W==2 ∧ 15W≠4",
        lambda r: r["s30"] == 2 and r["s15"] != 4,
    ),
    "rrg_rs100": ("RRG rs_ratio>100", lambda r: (r["rs_ratio"] or 0) > 100),
    "rrg_leading": ("RRG leading", lambda r: r["quadrant"] == "leading"),
    "rrg_lead_imp": (
        "RRG leading∨improving",
        lambda r: r["quadrant"] in ("leading", "improving"),
    ),
    "s30_rrg100": (
        "30W==2 ∧ rs_ratio>100",
        lambda r: r["s30"] == 2 and (r["rs_ratio"] or 0) > 100,
    ),
    "s30_leading": (
        "30W==2 ∧ leading",
        lambda r: r["s30"] == 2 and r["quadrant"] == "leading",
    ),
    "s30_lead_imp": (
        "30W==2 ∧ (lead∨imp)",
        lambda r: r["s30"] == 2 and r["quadrant"] in ("leading", "improving"),
    ),
    "s30_ix": ("30W==2 ∧ IX30==2", lambda r: r["s30"] == 2 and r["ix30"] == 2),
    "s30_ix_rrg100": (
        "30W==2 ∧ IX30==2 ∧ rs>100",
        lambda r: r["s30"] == 2
        and r["ix30"] == 2
        and (r["rs_ratio"] or 0) > 100,
    ),
    "both15_rrg100": (
        "30∧15 ∧ rs>100",
        lambda r: r["s30"] == 2
        and r["s15"] == 2
        and (r["rs_ratio"] or 0) > 100,
    ),
}


def eval_gate(rows: list[dict], gate_fn, hold: int = 7) -> dict:
    base_vals = [r["path"][hold] for r in rows]
    kept = [r["path"][hold] for r in rows if gate_fn(r)]
    bs, ks = stats(base_vals), stats(kept)
    boot = two_sample_boot(kept, base_vals)
    return {
        "n_base": bs["n"],
        "n_kept": ks["n"],
        "coverage": round(ks["n"] / bs["n"], 3) if bs["n"] else None,
        "med_base": bs["med"],
        "med_kept": ks["med"],
        "win_kept": ks["win"],
        "delta_med": (
            round(ks["med"] - bs["med"], 3)
            if ks["med"] is not None and bs["med"] is not None
            else None
        ),
        "boot": boot,
    }


def leave_one_stock(stocks: dict[str, dict], pool: str, gate_key: str) -> dict:
    _, gate_fn = GATES[gate_key]
    per = []
    for holdout in UNIVERSE:
        test = stocks[holdout]["rows"][pool] if holdout in stocks else []
        if len(test) < 3:
            per.append({"holdout": holdout, "verdict": "insufficient", "n_test": len(test)})
            continue
        m = eval_gate(test, gate_fn)
        delta = m["delta_med"]
        verdict = (
            "PASS" if delta is not None and delta > 0 else ("FAIL" if delta is not None else "n/a")
        )
        per.append(
            {
                "holdout": holdout,
                "verdict": verdict,
                "n_test": m["n_base"],
                "n_kept": m["n_kept"],
                "delta_med": delta,
                "med_kept": m["med_kept"],
            }
        )
    n_pass = sum(1 for x in per if x["verdict"] == "PASS")
    n_fail = sum(1 for x in per if x["verdict"] == "FAIL")
    return {
        "gate": gate_key,
        "n_pass": n_pass,
        "n_fail": n_fail,
        "verdict": (
            "PASS" if n_pass >= 6 else ("MIXED" if n_pass >= 4 else ("FAIL" if n_fail else "n/a"))
        ),
        "per_stock": per,
    }


def quad_table(rows: list[dict], hold: int = 7) -> list[dict]:
    buckets: dict[str, list[float]] = {}
    for r in rows:
        q = r.get("quadrant") or "unknown"
        key = f"s30={r['s30']}|{q}"
        buckets.setdefault(key, []).append(r["path"][hold])
    out = []
    for k, vals in sorted(buckets.items()):
        st = stats(vals)
        out.append({"cell": k, **st})
    return out


def build_stock(conn, ta_mod, epk_mod, wmod, ix_bars, ix_df, ix_ohlc, sid: str):
    spec = wmod.POOLS.get(sid)
    if spec is None:
        return None
    core = dict(spec.core)
    bars = epk_mod.load_stock(conn, sid)
    if len(bars) < 60:
        return None
    ohlc = load_price_full(conn, sid)
    if len(ohlc) < 250:
        return None
    amts, by_date = ta_mod.branch_amts(conn, sid, core)
    cal = [d for d, *_ in bars if START <= d <= END]
    if not cal:
        return None
    sigs_hard = epk_mod.dedupe(
        epk_mod.build_signals("回看1日共識", FLOOR, by_date, cal, amts)
    )
    sigs_or = epk_mod.dedupe(epk_mod.build_signals("OR", FLOOR, by_date, cal, amts))
    ta = ta_mod.build_ta(ohlc, ix_df)
    rows_hard = ta_mod.make_rows(sigs_hard, bars, ix_bars, epk_mod, ta)
    rows_or = ta_mod.make_rows(sigs_or, bars, ix_bars, epk_mod, ta)

    rs_ratio, rs_mom = build_rrg_series(ohlc, ix_ohlc)
    dates = sorted({r["signal_date"] for r in rows_hard + rows_or})
    cache30, cache15, ix30, rrg = {}, {}, {}, {}
    for d in dates:
        cache30[d] = weinstein_at(ohlc, d, ma_period=30)
        cache15[d] = weinstein_at(ohlc, d, ma_period=15)
        ix30[d] = weinstein_at(ix_ohlc, d, ma_period=30)
        rrg[d] = rrg_at(rs_ratio, rs_mom, d)

    for rows in (rows_hard, rows_or):
        for r in rows:
            d = r["signal_date"]
            r["sid"] = sid
            r["name"] = spec.stock_name
            r["s30"] = cache30[d]
            r["s15"] = cache15[d]
            r["ix30"] = ix30[d]
            rg = rrg[d]
            r["rs_ratio"] = rg["rs_ratio"]
            r["rs_mom"] = rg["rs_mom"]
            r["quadrant"] = rg["quadrant"]

    return {"sid": sid, "name": spec.stock_name, "rows": {"HARD": rows_hard, "OR": rows_or}}


def write_report(payload: dict) -> str:
    lines = [
        f"# 30W × 15W × RRG(L={RRG_LEN}) 搭配回測（Research · 未採納）",
        "",
        f"- 建於：`{payload['built_at']}`",
        f"- 窗：`{START}` → `{END}` · L1H7 · Universe A 8 檔",
        f"- 軸：長線=weinstein_stage(30W SSOT) · 中線=stage_mid(15W) · 相對強度=RRG WMA{RRG_LEN}",
        "- **不覆寫** `weinstein_stage`",
        "",
        "## 結論（短）",
        "",
        f"- {payload['verdict']['summary']}",
        f"- vs s30：{payload['verdict']['vs_s30']}",
        f"- 用法：{payload['verdict']['usage']}",
        "",
        "## Pooled 閘門",
        "",
    ]
    for pool in ("HARD", "OR"):
        lines += [
            f"### {pool}",
            "",
            "| gate | n | cov | med | Δmed | CI | p+|",
            "|---|---:|---:|---:|---:|---|---:|",
        ]
        for gkey, (gdef, _) in GATES.items():
            m = payload["pooled"][pool][gkey]
            b = m["boot"] or {}
            lines.append(
                f"| `{gkey}` {gdef} | {m['n_kept']} | {m['coverage']} | "
                f"{_s(m['med_kept'])} | {_s(m['delta_med'])} | {_ci(b)} | {b.get('p_pos')} |"
            )
        lines.append("")

    lines += ["## Leave-one-stock", "", "| pool | gate | P/F | verdict |", "|---|---|---|---|"]
    for pool in ("HARD", "OR"):
        for gkey in (
            "s30",
            "s15",
            "both15",
            "rrg_rs100",
            "rrg_leading",
            "s30_rrg100",
            "s30_leading",
            "s30_ix",
            "s30_ix_rrg100",
        ):
            h = payload["h3a"][pool][gkey]
            lines.append(
                f"| {pool} | `{gkey}` | {h['n_pass']}/{h['n_fail']} | **{h['verdict']}** |"
            )
    lines += ["", "## s30 × RRG 格子（OR）", "", "| cell | n | med | win |", "|---|---:|---:|---:|"]
    for c in payload["quad_cells"]["OR"]:
        if c["n"] < 5:
            continue
        lines.append(
            f"| {c['cell']} | {c['n']} | {_s(c['med'])} | "
            f"{(c['win'] if c['win'] is not None else float('nan')):.0%} |"
        )
    lines += [
        "",
        "## 詮釋",
        "",
        "- 長／中／短**可以**作診斷分類；本輪測的是「進場閘門」是否加分。",
        "- RRG 是相對強度 GPS（正交於 Weinstein 絕對趨勢階段），不是另一條 Weinstein MA。",
        "- 未採納 → 不寫 strategy／order。",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    ta_mod = load_mod("scripts/research/run_2327_ta_adaptive_exit.py", "ta_h15rrg")
    epk_mod = load_mod(
        "scripts/research/build_expert_pool_trade_knowledge.py", "epk_h15rrg"
    )
    wmod = epk_mod.load_watch_mod()

    conn = connect_ro()
    ix_ohlc = load_ix_ohlc_full(conn)
    ix_bars = epk_mod.load_ix(conn)
    ix_df = pd.DataFrame(ix_bars, columns=["date", "open", "close"])

    stocks: dict[str, dict] = {}
    for i, sid in enumerate(UNIVERSE, 1):
        print(f"[{i}/{len(UNIVERSE)}] {sid} …", flush=True)
        s = build_stock(conn, ta_mod, epk_mod, wmod, ix_bars, ix_df, ix_ohlc, sid)
        if s is None:
            print(f"  skip {sid}")
            continue
        print(f"  HARD={len(s['rows']['HARD'])} OR={len(s['rows']['OR'])}")
        stocks[sid] = s
    conn.close()

    pooled_rows = {
        pool: [r for s in stocks.values() for r in s["rows"][pool]]
        for pool in ("HARD", "OR")
    }
    pooled = {
        pool: {g: eval_gate(pooled_rows[pool], fn) for g, (_, fn) in GATES.items()}
        for pool in ("HARD", "OR")
    }
    h3a_keys = [
        "s30",
        "s15",
        "both15",
        "rrg_rs100",
        "rrg_leading",
        "s30_rrg100",
        "s30_leading",
        "s30_ix",
        "s30_ix_rrg100",
    ]
    h3a = {
        pool: {g: leave_one_stock(stocks, pool, g) for g in h3a_keys}
        for pool in ("HARD", "OR")
    }
    quad_cells = {pool: quad_table(pooled_rows[pool]) for pool in ("HARD", "OR")}

    or_ = pooled["OR"]
    # incremental within s30
    s30_rows = [r for r in pooled_rows["OR"] if r["s30"] == 2]
    incr = {
        "require_s15": eval_gate(s30_rows, lambda r: r["s15"] == 2),
        "require_rrg100": eval_gate(s30_rows, lambda r: (r["rs_ratio"] or 0) > 100),
        "require_leading": eval_gate(s30_rows, lambda r: r["quadrant"] == "leading"),
    }

    def better(a, b):
        if a["delta_med"] is None or b["delta_med"] is None:
            return "?"
        da, db = a["delta_med"], b["delta_med"]
        if da > db + 0.4:
            return "優於"
        if da < db - 0.4:
            return "劣於"
        return "接近"

    summary = (
        f"15W both15 Δ{_s(or_['both15']['delta_med'])} vs s30 {_s(or_['s30']['delta_med'])} "
        f"→ {better(or_['both15'], or_['s30'])}；"
        f"s30∧rs>100 Δ{_s(or_['s30_rrg100']['delta_med'])}；"
        f"s30∧leading Δ{_s(or_['s30_leading']['delta_med'])}；"
        f"s30∧IX 仍 {_s(or_['s30_ix']['delta_med'])}"
    )
    vs = (
        f"both15 {better(or_['both15'], or_['s30'])} s30；"
        f"s30_rrg100 {better(or_['s30_rrg100'], or_['s30'])} s30；"
        f"H3a OR s30={h3a['OR']['s30']['verdict']} "
        f"both15={h3a['OR']['both15']['verdict']} "
        f"s30_rrg100={h3a['OR']['s30_rrg100']['verdict']} "
        f"s30_ix={h3a['OR']['s30_ix']['verdict']}"
    )
    # usage
    rrg_helps = (or_["s30_rrg100"]["delta_med"] or 0) > (or_["s30"]["delta_med"] or 0) + 0.4
    mid_helps = (or_["both15"]["delta_med"] or 0) > (or_["s30"]["delta_med"] or 0) + 0.4
    hard_rrg_hurts = (pooled["HARD"]["s30_rrg100"]["delta_med"] or 0) < 0
    if RRG_LEN < 20 and hard_rrg_hurts:
        usage = (
            f"RRG WMA{RRG_LEN} 僅可作敏感度診斷；HARD 反向且 OR CI 含0，"
            "不取代較穩定的 WMA20"
        )
    elif rrg_helps and h3a["OR"]["s30_rrg100"]["verdict"] in ("PASS", "MIXED"):
        usage = "長線用 30W；相對強度可用 RRG rs>100／leading 作觀察加權（仍 observe）"
    elif mid_helps:
        usage = "中線 15W 可觀察；仍次於／並列 s30∧IX"
    else:
        usage = "診斷可分長中短＋RRG，但進場閘以 30W（±IX）為主；15W/RRG 未穩定勝出"

    verdict = {"summary": summary, "vs_s30": vs, "usage": usage, "incr_within_s30_OR": incr}

    payload = {
        "built_at": datetime.now().isoformat(timespec="seconds"),
        "protocol": {
            "start": START,
            "end": END,
            "rrg_length": RRG_LEN,
            "ma_slow": 30,
            "ma_mid": 15,
        },
        "universe": UNIVERSE,
        "gates": {k: v[0] for k, v in GATES.items()},
        "pooled": pooled,
        "h3a": h3a,
        "quad_cells": quad_cells,
        "verdict": verdict,
    }
    (OUT / "stage_horizon_rrg_result.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    (OUT / "REPORT.md").write_text(write_report(payload), encoding="utf-8")

    print("\n=== VERDICT ===")
    print(summary)
    print(vs)
    print(usage)
    print(f"wrote {OUT / 'REPORT.md'}")
    for pool in ("HARD", "OR"):
        print(f"\n{pool}:")
        for g in (
            "s30",
            "s15",
            "both15",
            "rrg_rs100",
            "rrg_leading",
            "s30_rrg100",
            "s30_leading",
            "s30_ix",
            "s30_ix_rrg100",
        ):
            m = pooled[pool][g]
            print(
                f"  {g:16s} n={m['n_kept']:3d} cov={m['coverage']} "
                f"med={_s(m['med_kept'])} Δ={_s(m['delta_med'])} {_ci(m['boot'])}"
            )


if __name__ == "__main__":
    main()
