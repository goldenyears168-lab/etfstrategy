#!/usr/bin/env python3
"""30W weinstein_stage × 10W stage_fast 搭配回測（Research only · 未採納）.

協議對齊 `run_stage2_multiwave_validate.py`：
  HARD = 回看1日共識@0.5億；OR = OR@0.5億（分池，不合併主 Δ）
  L1H7 · 30bps · β=1.15 · dedup 5d · 2024-07-01..2026-07-17
  Universe A 8 檔（同 multiwave）
  Bootstrap B=2000

閘門（相對 base 全綠燈）：
  s30          weinstein_stage(30W)==2          ← SSOT 對照
  s10          stage_fast(10W)==2
  both         30W==2 ∧ 10W==2                 ← 雙週期確認
  either       30W==2 ∨ 10W==2
  s30_not4     30W==2 ∧ 10W≠4                  ← 避快週期下跌背離
  s30_fast23   30W==2 ∧ 10W∈{2,3}              ← 慢多 + 快多／走緩
  s30_ix       30W==2 ∧ IX30==2                ← 既有雙閘
  both_ix      both ∧ IX30==2

Out: reports/research/chip-overlays/2327_ta_adaptive/stage_fast_combo/
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from stage_analysis import classify_weinstein_stage  # noqa: E402

UNIVERSE = ["2327", "2303", "2408", "6669", "2383", "3037", "3189", "2337"]
SOURCE = "finmind"
START, END = "2024-07-01", "2026-07-17"
COST, BETA = 0.003, 1.15
FLOOR = 5e7
BOOT = 2000
RNG = np.random.default_rng(3010)
DB = ROOT / "data" / "stocks.db"
OUT = ROOT / "reports/research/chip-overlays/2327_ta_adaptive/stage_fast_combo"

STAGE_NAME = {0: "?", 1: "打底", 2: "多頭", 3: "走緩", 4: "下跌"}


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
    "base": ("全綠燈（無閘）", lambda r: True),
    "s30": ("weinstein_stage 30W==2", lambda r: r["s30"] == 2),
    "s10": ("stage_fast 10W==2", lambda r: r["s10"] == 2),
    "both": ("30W==2 ∧ 10W==2", lambda r: r["s30"] == 2 and r["s10"] == 2),
    "either": ("30W==2 ∨ 10W==2", lambda r: r["s30"] == 2 or r["s10"] == 2),
    "s30_not4": ("30W==2 ∧ 10W≠4", lambda r: r["s30"] == 2 and r["s10"] != 4),
    "s30_fast23": (
        "30W==2 ∧ 10W∈{2,3}",
        lambda r: r["s30"] == 2 and r["s10"] in (2, 3),
    ),
    "s30_ix": ("30W==2 ∧ IX30==2", lambda r: r["s30"] == 2 and r["ix30"] == 2),
    "both_ix": (
        "both ∧ IX30==2",
        lambda r: r["s30"] == 2 and r["s10"] == 2 and r["ix30"] == 2,
    ),
}


def eval_gate(rows: list[dict], gate_fn, hold: int = 7) -> dict:
    base_vals = [r["path"][hold] for r in rows]
    kept = [r["path"][hold] for r in rows if gate_fn(r)]
    drop = [r["path"][hold] for r in rows if not gate_fn(r)]
    bs = stats(base_vals)
    ks = stats(kept)
    ds = stats(drop)
    boot = two_sample_boot(kept, base_vals)
    return {
        "n_base": bs["n"],
        "n_kept": ks["n"],
        "coverage": round(ks["n"] / bs["n"], 3) if bs["n"] else None,
        "med_base": bs["med"],
        "med_kept": ks["med"],
        "med_drop": ds["med"],
        "win_kept": ks["win"],
        "delta_med": (
            round(ks["med"] - bs["med"], 3)
            if ks["med"] is not None and bs["med"] is not None
            else None
        ),
        "boot": boot,
    }


def cell_table(rows: list[dict], hold: int = 7) -> list[dict]:
    """4×4 contingency of (s30, s10) on H7."""
    buckets: dict[tuple[int, int], list[float]] = {}
    for r in rows:
        key = (int(r["s30"] or 0), int(r["s10"] or 0))
        buckets.setdefault(key, []).append(r["path"][hold])
    out = []
    for (a, b), vals in sorted(buckets.items()):
        st = stats(vals)
        out.append(
            {
                "s30": a,
                "s10": b,
                "s30_name": STAGE_NAME.get(a, "?"),
                "s10_name": STAGE_NAME.get(b, "?"),
                **st,
            }
        )
    return out


def leave_one_stock(stocks: dict[str, dict], pool: str, gate_key: str) -> dict:
    _, gate_fn = GATES[gate_key]
    per = []
    for holdout in UNIVERSE:
        train = [
            r
            for sid, s in stocks.items()
            if sid != holdout
            for r in s["rows"][pool]
        ]
        test = stocks[holdout]["rows"][pool] if holdout in stocks else []
        if len(test) < 3:
            per.append(
                {
                    "holdout": holdout,
                    "verdict": "insufficient",
                    "n_test": len(test),
                }
            )
            continue
        m = eval_gate(test, gate_fn)
        # train only for coverage context
        tm = eval_gate(train, gate_fn)
        delta = m["delta_med"]
        verdict = (
            "PASS"
            if delta is not None and delta > 0
            else ("FAIL" if delta is not None else "n/a")
        )
        per.append(
            {
                "holdout": holdout,
                "name": stocks[holdout]["name"],
                "verdict": verdict,
                "n_test": m["n_base"],
                "n_kept": m["n_kept"],
                "med_base": m["med_base"],
                "med_kept": m["med_kept"],
                "delta_med": delta,
                "train_delta": tm["delta_med"],
            }
        )
    n_pass = sum(1 for x in per if x["verdict"] == "PASS")
    n_fail = sum(1 for x in per if x["verdict"] == "FAIL")
    return {
        "gate": gate_key,
        "pool": pool,
        "n_pass": n_pass,
        "n_fail": n_fail,
        "verdict": (
            "PASS"
            if n_pass >= 6
            else ("MIXED" if n_pass >= 4 else ("FAIL" if n_fail else "n/a"))
        ),
        "per_stock": per,
    }


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

    # cache stages on unique signal dates
    dates = sorted({r["signal_date"] for r in rows_hard + rows_or})
    cache30: dict[str, int] = {}
    cache10: dict[str, int] = {}
    ix30: dict[str, int] = {}
    ix10: dict[str, int] = {}
    for d in dates:
        cache30[d] = weinstein_at(ohlc, d, ma_period=30)
        cache10[d] = weinstein_at(ohlc, d, ma_period=10)
        ix30[d] = weinstein_at(ix_ohlc, d, ma_period=30)
        ix10[d] = weinstein_at(ix_ohlc, d, ma_period=10)

    for rows in (rows_hard, rows_or):
        for r in rows:
            d = r["signal_date"]
            r["sid"] = sid
            r["name"] = spec.stock_name
            r["s30"] = cache30[d]
            r["s10"] = cache10[d]
            r["ix30"] = ix30[d]
            r["ix10"] = ix10[d]
            # sanity: make_rows wein_stage should match 30W
            r["wein_stage_check"] = r.get("wein_stage")

    return {
        "sid": sid,
        "name": spec.stock_name,
        "rows": {"HARD": rows_hard, "OR": rows_or},
    }


def write_report(payload: dict) -> str:
    lines = [
        "# 30W × 10W Stage 搭配回測（Research · 未採納）",
        "",
        f"- 建於：`{payload['built_at']}`",
        f"- 窗：`{START}` → `{END}` · L1H7 · cost {COST} · β={BETA}",
        f"- Universe：{', '.join(UNIVERSE)}",
        "- **weinstein_stage(30W) 未覆寫**；`stage_fast(10W)` 僅診斷閘門",
        "",
        "## 結論（短）",
        "",
    ]
    verdict = payload["verdict"]
    lines += [
        f"- **主評**：{verdict['summary']}",
        f"- 相對 SSOT `s30`：{verdict['vs_s30']}",
        f"- 建議用法：{verdict['usage']}",
        "",
        "## Pooled 閘門對照（Δmed vs base · bootstrap CI）",
        "",
    ]
    for pool in ("HARD", "OR"):
        lines += [
            f"### {pool}",
            "",
            "| gate | 定義 | n_kept | cov | med_base | med_kept | Δmed | CI | p(Δ>0) |",
            "|---|---|---:|---:|---:|---:|---:|---|---:|",
        ]
        for gkey, (gdef, _) in GATES.items():
            m = payload["pooled"][pool][gkey]
            b = m["boot"] or {}
            lines.append(
                f"| `{gkey}` | {gdef} | {m['n_kept']} | {m['coverage']} | "
                f"{_s(m['med_base'])} | {_s(m['med_kept'])} | {_s(m['delta_med'])} | "
                f"{_ci(b)} | {b.get('p_pos')} |"
            )
        lines.append("")

    lines += [
        "## 4×4 格子（s30 × s10 · H7 med）",
        "",
    ]
    for pool in ("HARD", "OR"):
        lines += [f"### {pool}", "", "| s30 \\ s10 | n | med | win |", "|---|---:|---:|---:|"]
        for c in payload["cells"][pool]:
            lines.append(
                f"| {c['s30']}{c['s30_name']}/{c['s10']}{c['s10_name']} | "
                f"{c['n']} | {_s(c['med'])} | "
                f"{(c['win'] if c['win'] is not None else float('nan')):.0%} |"
            )
        lines.append("")

    lines += ["## 2327 國巨單檔", ""]
    for pool in ("HARD", "OR"):
        lines += [
            f"### {pool}",
            "",
            "| gate | n_kept | med_kept | Δmed | CI |",
            "|---|---:|---:|---:|---|",
        ]
        for gkey in GATES:
            m = payload["by_stock"]["2327"][pool][gkey]
            lines.append(
                f"| `{gkey}` | {m['n_kept']} | {_s(m['med_kept'])} | "
                f"{_s(m['delta_med'])} | {_ci(m['boot'])} |"
            )
        lines.append("")

    lines += [
        "## Leave-one-stock（跨股 · Δmed>0 計 PASS）",
        "",
        "| pool | gate | PASS/FAIL | verdict |",
        "|---|---|---|---|",
    ]
    for pool in ("HARD", "OR"):
        for gkey in ("s30", "s10", "both", "s30_not4", "s30_fast23", "s30_ix", "both_ix"):
            h = payload["h3a"][pool][gkey]
            lines.append(
                f"| {pool} | `{gkey}` | {h['n_pass']}/{h['n_fail']} | **{h['verdict']}** |"
            )
    lines += [
        "",
        "## 詮釋備註",
        "",
        "- `both` 覆蓋率通常低於 `s30`：犧牲訊號量換品質。",
        "- `s30_not4`／`s30_fast23` 測「慢多頭但快週期已轉弱」是否為雷區。",
        "- CI 含 0 → 方向可觀察，**未達採納**；不寫 strategy／order。",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    ta_mod = load_mod("scripts/research/run_2327_ta_adaptive_exit.py", "ta_stage_combo")
    epk_mod = load_mod(
        "scripts/research/build_expert_pool_trade_knowledge.py", "epk_stage_combo"
    )
    wmod = epk_mod.load_watch_mod()

    conn = connect_ro()
    ix_ohlc = load_ix_ohlc_full(conn)
    ix_bars = epk_mod.load_ix(conn)  # (date, open, close) for r_adj
    ix_df = pd.DataFrame(ix_bars, columns=["date", "open", "close"])

    stocks: dict[str, dict] = {}
    for i, sid in enumerate(UNIVERSE, 1):
        print(f"[{i}/{len(UNIVERSE)}] {sid} …", flush=True)
        s = build_stock(conn, ta_mod, epk_mod, wmod, ix_bars, ix_df, ix_ohlc, sid)
        if s is None:
            print(f"  skip {sid}")
            continue
        # mismatch check
        mism = sum(
            1
            for pool in ("HARD", "OR")
            for r in s["rows"][pool]
            if r.get("wein_stage") not in (None, r["s30"]) and r["s30"] != 0
        )
        print(
            f"  HARD={len(s['rows']['HARD'])} OR={len(s['rows']['OR'])} "
            f"wein_stage≠s30 mismatches≈{mism}"
        )
        stocks[sid] = s
    conn.close()

    pooled_rows = {
        pool: [r for s in stocks.values() for r in s["rows"][pool]]
        for pool in ("HARD", "OR")
    }

    pooled = {
        pool: {
            gkey: eval_gate(pooled_rows[pool], gate_fn)
            for gkey, (_, gate_fn) in GATES.items()
        }
        for pool in ("HARD", "OR")
    }
    cells = {pool: cell_table(pooled_rows[pool]) for pool in ("HARD", "OR")}
    by_stock = {
        sid: {
            pool: {
                gkey: eval_gate(s["rows"][pool], gate_fn)
                for gkey, (_, gate_fn) in GATES.items()
            }
            for pool in ("HARD", "OR")
        }
        for sid, s in stocks.items()
    }
    h3a = {
        pool: {
            gkey: leave_one_stock(stocks, pool, gkey)
            for gkey in ("s30", "s10", "both", "s30_not4", "s30_fast23", "s30_ix", "both_ix")
        }
        for pool in ("HARD", "OR")
    }

    # ---- short verdict ----
    or_s30 = pooled["OR"]["s30"]
    or_both = pooled["OR"]["both"]
    or_s10 = pooled["OR"]["s10"]
    or_not4 = pooled["OR"]["s30_not4"]
    hard_both = pooled["HARD"]["both"]
    hard_s30 = pooled["HARD"]["s30"]

    def _better(a, b) -> str:
        if a["delta_med"] is None or b["delta_med"] is None:
            return "無法比"
        if a["delta_med"] > b["delta_med"] + 0.3:
            return "優於"
        if a["delta_med"] < b["delta_med"] - 0.3:
            return "劣於"
        return "接近"

    summary_parts = []
    if or_both["delta_med"] is not None and or_s30["delta_med"] is not None:
        summary_parts.append(
            f"OR both Δ{_s(or_both['delta_med'])} (n{or_both['n_kept']}) vs "
            f"s30 Δ{_s(or_s30['delta_med'])} (n{or_s30['n_kept']}) → both {_better(or_both, or_s30)} s30"
        )
    if or_s10["delta_med"] is not None:
        summary_parts.append(
            f"單用 s10 Δ{_s(or_s10['delta_med'])} cov={or_s10['coverage']}"
        )
    if or_not4["delta_med"] is not None:
        summary_parts.append(
            f"s30_not4 Δ{_s(or_not4['delta_med'])} cov={or_not4['coverage']}"
        )

    # incremental: among s30==2, does requiring s10==2 help?
    incr = {}
    for pool in ("HARD", "OR"):
        s30_rows = [r for r in pooled_rows[pool] if r["s30"] == 2]
        both_on_s30 = eval_gate(s30_rows, GATES["both"][1])
        # gate both on s30-only base → same as filtering s10==2 within s30
        incr[pool] = {
            "n_s30": len(s30_rows),
            "within_s30_require_s10": eval_gate(
                s30_rows, lambda r: r["s10"] == 2
            ),
            "within_s30_avoid_s10_4": eval_gate(
                s30_rows, lambda r: r["s10"] != 4
            ),
            "within_s30_s10_in_23": eval_gate(
                s30_rows, lambda r: r["s10"] in (2, 3)
            ),
        }

    vs = (
        f"both {_better(or_both, or_s30)} s30（覆蓋 "
        f"{or_both['coverage']} vs {or_s30['coverage']}）；"
        f"HARD both {_better(hard_both, hard_s30)} s30"
    )
    # usage rule of thumb
    both_ci = (or_both.get("boot") or {}).get("ci")
    both_clear = (
        both_ci
        and both_ci[0] is not None
        and both_ci[0] > 0
        and or_both["delta_med"] is not None
        and or_both["delta_med"] > (or_s30["delta_med"] or 0)
    )
    h3a_both = h3a["OR"]["both"]["verdict"]
    h3a_s30 = h3a["OR"]["s30"]["verdict"]
    usage = (
        "維持 s30 SSOT（可選 s30∧IX30 觀察）；10W 僅熱力圖診斷，勿 AND／單獨當閘"
        if (not both_clear)
        or h3a_both != "PASS"
        or (h3a_s30 == "PASS" and h3a_both != "PASS")
        else "both 可作觀察欄候選（仍 observe）"
    )

    verdict = {
        "summary": "；".join(summary_parts) if summary_parts else "樣本不足",
        "vs_s30": vs,
        "usage": usage,
        "h3a_or_both": h3a["OR"]["both"]["verdict"],
        "h3a_or_s30": h3a["OR"]["s30"]["verdict"],
    }

    payload = {
        "built_at": datetime.now().isoformat(timespec="seconds"),
        "protocol": {
            "start": START,
            "end": END,
            "hold": 7,
            "cost": COST,
            "beta": BETA,
            "floor": FLOOR,
            "boot": BOOT,
            "field_ssot": "weinstein_stage",
            "field_fast": "stage_fast",
            "ma_slow": 30,
            "ma_fast": 10,
        },
        "universe": UNIVERSE,
        "gates": {k: v[0] for k, v in GATES.items()},
        "pooled": pooled,
        "cells": cells,
        "by_stock": by_stock,
        "incremental_within_s30": incr,
        "h3a": h3a,
        "verdict": verdict,
    }

    (OUT / "stage_fast_combo_result.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    report = write_report(payload)
    (OUT / "REPORT.md").write_text(report, encoding="utf-8")

    print("\n=== VERDICT ===")
    print(verdict["summary"])
    print("vs_s30:", verdict["vs_s30"])
    print("usage:", verdict["usage"])
    print(f"wrote {OUT / 'REPORT.md'}")
    for pool in ("HARD", "OR"):
        print(f"\n{pool}:")
        for g in ("s30", "s10", "both", "s30_not4", "s30_fast23", "s30_ix"):
            m = pooled[pool][g]
            print(
                f"  {g:12s} n={m['n_kept']:3d} cov={m['coverage']} "
                f"med={_s(m['med_kept'])} Δ={_s(m['delta_med'])} ci={_ci(m['boot'])}"
            )


if __name__ == "__main__":
    main()
