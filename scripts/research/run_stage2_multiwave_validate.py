#!/usr/bin/env python3
"""Weinstein Stage 2 多波段驗證（Research only · 不寫 strategy/order）.

Thesis under test: `run_2327_ta_adaptive_exit.py` 的核心發現——籌碼綠燈 ×
Weinstein Stage 2（多頭）確認可大幅拉高 H7 超額報酬——目前只驗過「單一 2026
多頭波段」。本輪把 Stage 2 切成多個「波段」（wave），逐波段、逐股、逐 IX
regime 做 holdout 交叉驗證，檢查該效應是否波段泛化，還是單一波段的偶然。

Universe A（8 檔）：2327 國巨 · 2303 聯電 · 2408 南亞科 · 6669 緯穎 ·
2383 台光電 · 3037 欣興 · 3189 景碩 · 2337 旺宏。

協議：
  HARD = 回看1日共識@0.5億；OR = OR@0.5億（**分池報告，不合併主 Δ**）
  L1H7 · 30bps · β=1.15 · dedup 5 交易日 · 2024-07-01..2026-07-17
  Gate：wein_stage==2（PIT `classify_weinstein_stage(bars≤T)`）
  Bootstrap B=2000

波段（wave）定義：
  逐日（≤d，PIT）分類 Stage；連續 Stage==2 為一段；中斷 ≤5 交易日視為同波段
  （橋接合併）；合併後長度 ≥20 交易日才算一個「波段」。
  wave_id = f"{sid}|S2|{t0}|{t1}"。

Holdouts：
  H1 leave-one-wave（2327 為主；波段數 W<3 → insufficient_waves）
  H2 rolling-wave（train wave_rank≤k → test wave_rank=k+1；train 無 Stage 2
     樣本標 no_prior_stage2）
  H3a leave-one-stock（8 檔輪流排除一檔，用其餘 7 檔訓練，回測被排除股）
  H3b leave-one-IX-wave（IX0001 自身 Weinstein Stage 2 波段當市場 regime，
     輪流排除一個 IX 波段涵蓋的交易做 holdout）

效能折衷（見協議「注意效能」）：實測 `weinstein_at`（PIT 逐日精算）對 2327
2784 筆日K、489 個測試日僅需 ~1.5 秒（見下方 benchmark note），8 檔 × 逐日
~500 日總計 <20 秒，**遠低於必須近似的門檻**。因此本腳本對所有交易日（不只
綠燈日）皆用精確 `classify_weinstein_stage(hist)` PIT 計算，未使用週線 MA
近似捷徑——省去近似/精算兩套邏輯的一致性風險。

Out: reports/research/chip-overlays/2327_ta_adaptive/multiwave/
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from stage_analysis import classify_weinstein_stage  # noqa: E402  (used indirectly via weinstein_at)

UNIVERSE = ["2327", "2303", "2408", "6669", "2383", "3037", "3189", "2337"]
SID_MAIN = "2327"
SID_DIAG = "6669"
SOURCE = "finmind"
START, END = "2024-07-01", "2026-07-17"
COST, BETA = 0.003, 1.15
DEDUP = 5
FLOOR = 5e7
KMAX = 15
WAVE_MIN_LEN = 20
WAVE_GAP_MERGE = 5
BOOT = 2000
RNG = np.random.default_rng(2327)
DB = ROOT / "data" / "stocks.db"
BASE_OUT = ROOT / "reports/research/chip-overlays/2327_ta_adaptive"
OUT = BASE_OUT / "multiwave"


# ---------------------------------------------------------------------------
# reuse existing modules (research-only; no new src/ module needed)
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# minimal new loaders: existing loaders in ta_mod/epk_mod window-limit history
# (2024-01-01+ / 2024-05-01+), which is too short for PIT Weinstein stage
# classification at early START dates (needs 42+ weekly bars = ~210 daily).
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# stats / bootstrap (protocol B=2000; copied+trimmed from run_2327_ta_adaptive_exit
# which hardcodes B=4000 — kept separate to respect this study's protocol constant)
# ---------------------------------------------------------------------------
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


def cluster_two_sample_boot(
    kept: list[dict], base: list[dict], boot: int = BOOT
) -> dict:
    """Bootstrap by resampling *clusters* (wave_id, or 1-trade cluster for non-S2).

    kept/base: list of {"val": float, "cluster": str}.
    """
    kc: dict[str, list[float]] = defaultdict(list)
    for r in kept:
        if r["val"] is not None and np.isfinite(r["val"]):
            kc[r["cluster"]].append(r["val"])
    bc: dict[str, list[float]] = defaultdict(list)
    for r in base:
        if r["val"] is not None and np.isfinite(r["val"]):
            bc[r["cluster"]].append(r["val"])
    kkeys, bkeys = list(kc.keys()), list(bc.keys())
    out = {"n_clusters_kept": len(kkeys), "n_clusters_base": len(bkeys)}
    if len(kkeys) < 3 or len(bkeys) < 3:
        out.update({"delta_med": None, "ci": None, "p_pos": None})
        return out
    deltas = np.empty(boot)
    for i in range(boot):
        ksel = [kkeys[j] for j in RNG.integers(0, len(kkeys), len(kkeys))]
        bsel = [bkeys[j] for j in RNG.integers(0, len(bkeys), len(bkeys))]
        kvals = np.concatenate([kc[k] for k in ksel])
        bvals = np.concatenate([bc[k] for k in bsel])
        deltas[i] = np.median(kvals) - np.median(bvals)
    kept_med = float(np.median(np.concatenate(list(kc.values()))))
    base_med = float(np.median(np.concatenate(list(bc.values()))))
    out.update(
        {
            "delta_med": kept_med - base_med,
            "ci": [float(np.quantile(deltas, 0.025)), float(np.quantile(deltas, 0.975))],
            "p_pos": float((deltas > 0).mean()),
        }
    )
    return out


def _f(x):
    try:
        v = float(x)
        return v if np.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def _s(x) -> str:
    return f"{x:+.2f}" if isinstance(x, (int, float)) and np.isfinite(x) else "nan"


# ---------------------------------------------------------------------------
# Weinstein Stage 2 wave detection
# ---------------------------------------------------------------------------
def compute_daily_stage(ta_mod, ta_df: pd.DataFrame, cal: list[str]) -> dict[str, int]:
    """Exact PIT Weinstein stage for every trading day (reuses ta_mod.weinstein_at)."""
    out = {}
    for d in cal:
        w = ta_mod.weinstein_at(ta_df, d)
        out[d] = int(w.get("stage") or 0)
    return out


def find_waves(
    sid: str,
    cal: list[str],
    stage_by_date: dict[str, int],
    min_len: int = WAVE_MIN_LEN,
    gap_merge: int = WAVE_GAP_MERGE,
) -> list[dict]:
    stages = [stage_by_date.get(d, 0) for d in cal]
    n = len(stages)
    runs: list[list[int]] = []
    i = 0
    while i < n:
        if stages[i] == 2:
            j = i
            while j + 1 < n and stages[j + 1] == 2:
                j += 1
            runs.append([i, j])
            i = j + 1
        else:
            i += 1
    merged: list[list[int]] = []
    for r in runs:
        if merged and r[0] - merged[-1][1] - 1 <= gap_merge:
            merged[-1][1] = r[1]
        else:
            merged.append(list(r))
    waves = []
    for a, b in merged:
        if (b - a + 1) >= min_len:
            t0, t1 = cal[a], cal[b]
            waves.append(
                {
                    "wave_id": f"{sid}|S2|{t0}|{t1}",
                    "sid": sid,
                    "t0": t0,
                    "t1": t1,
                    "len_trading_days": b - a + 1,
                }
            )
    waves.sort(key=lambda w: w["t0"])
    for rank, w in enumerate(waves, 1):
        w["wave_rank"] = rank
    return waves


def assign_wave(sig_date: str, waves: list[dict]) -> tuple[str | None, int | None]:
    for w in waves:
        if w["t0"] <= sig_date <= w["t1"]:
            return w["wave_id"], w["wave_rank"]
    return None, None


# ---------------------------------------------------------------------------
# per-stock data build
# ---------------------------------------------------------------------------
def build_stock(conn, ta_mod, epk_mod, wmod, ix_bars, ix_df, sid: str) -> dict | None:
    spec = wmod.POOLS.get(sid)
    if spec is None:
        return None
    core = dict(spec.core)
    bars = epk_mod.load_stock(conn, sid)
    if len(bars) < 60:
        return None
    ohlc_full = load_price_full(conn, sid)
    if len(ohlc_full) < 250:
        return None
    amts, by_date = ta_mod.branch_amts(conn, sid, core)
    cal = [d for d, *_ in bars if START <= d <= END]
    if not cal:
        return None
    sigs_hard = epk_mod.dedupe(epk_mod.build_signals("回看1日共識", FLOOR, by_date, cal, amts))
    sigs_or = epk_mod.dedupe(epk_mod.build_signals("OR", FLOOR, by_date, cal, amts))
    ta = ta_mod.build_ta(ohlc_full, ix_df)
    rows_hard = ta_mod.make_rows(sigs_hard, bars, ix_bars, epk_mod, ta)
    rows_or = ta_mod.make_rows(sigs_or, bars, ix_bars, epk_mod, ta)
    stage_by_date = compute_daily_stage(ta_mod, ta, cal)
    waves = find_waves(sid, cal, stage_by_date)
    for pool_name, rows in (("HARD", rows_hard), ("OR", rows_or)):
        for r in rows:
            r["sid"] = sid
            r["name"] = spec.stock_name
            r["pool"] = pool_name
            wid, wrank = (None, None)
            if r["wein_stage"] == 2:
                wid, wrank = assign_wave(r["signal_date"], waves)
                if wid is None:
                    wid = f"{sid}|S2_unassigned|{r['signal_date']}"
            cluster = wid if wid is not None else f"{sid}|nonS2|{r['signal_date']}"
            r["wave_id"] = wid
            r["wave_rank"] = wrank
            r["cluster"] = cluster
    return {
        "sid": sid,
        "name": spec.stock_name,
        "core": core,
        "bars": bars,
        "cal": cal,
        "stage_by_date": stage_by_date,
        "waves": waves,
        "rows": {"HARD": rows_hard, "OR": rows_or},
    }


# ---------------------------------------------------------------------------
# pool metrics
# ---------------------------------------------------------------------------
def pool_metrics(rows: list[dict], hold: int = 7) -> dict:
    h_all = [r["path"][hold] for r in rows]
    kept = [r["path"][hold] for r in rows if r["wein_stage"] == 2]
    dropped = [r["path"][hold] for r in rows if r["wein_stage"] != 2]
    base = stats(h_all)
    kept_s = stats(kept)
    dropped_s = stats(dropped)
    delta_med = (
        round(kept_s["med"] - base["med"], 3) if kept_s["med"] is not None else None
    )
    kept_records = [
        {"val": r["path"][hold], "cluster": r["cluster"]}
        for r in rows
        if r["wein_stage"] == 2
    ]
    base_records = [{"val": r["path"][hold], "cluster": r["cluster"]} for r in rows]
    return {
        "n_total": len(rows),
        "hold": hold,
        "base": base,
        "kept_stage2": kept_s,
        "disaster_non_stage2": dropped_s,
        "coverage": round(len(kept) / len(rows), 3) if rows else None,
        "delta_med_vs_base": delta_med,
        "two_sample_boot": two_sample_boot(kept, h_all),
        "cluster_boot": cluster_two_sample_boot(kept_records, base_records),
    }


def pooled_rows(stocks: dict[str, dict], pool: str, sids: list[str] | None = None) -> list[dict]:
    sids = sids or list(stocks.keys())
    out = []
    for sid in sids:
        s = stocks.get(sid)
        if s:
            out += s["rows"][pool]
    return out


def annotate_ix_stage(stocks: dict[str, dict], ix_stage_by_date: dict[str, int]) -> None:
    """Attach ix_stage / dual_gate flags onto every green row (in-place)."""
    for s in stocks.values():
        for pool in ("HARD", "OR"):
            for r in s["rows"][pool]:
                ix_st = int(ix_stage_by_date.get(r["signal_date"], 0) or 0)
                r["ix_stage"] = ix_st
                r["dual_s2"] = bool(r["wein_stage"] == 2 and ix_st == 2)


def keep_stage2(r: dict) -> bool:
    return r.get("wein_stage") == 2


def keep_dual_s2(r: dict) -> bool:
    return bool(r.get("dual_s2"))


def pool_metrics_gate(rows: list[dict], keep_fn, *, hold: int = 7, label: str = "kept") -> dict:
    h_all = [r["path"][hold] for r in rows]
    kept = [r["path"][hold] for r in rows if keep_fn(r)]
    dropped = [r["path"][hold] for r in rows if not keep_fn(r)]
    base = stats(h_all)
    kept_s = stats(kept)
    dropped_s = stats(dropped)
    delta_med = (
        round(kept_s["med"] - base["med"], 3) if kept_s["med"] is not None else None
    )
    kept_records = [
        {"val": r["path"][hold], "cluster": r["cluster"]} for r in rows if keep_fn(r)
    ]
    base_records = [{"val": r["path"][hold], "cluster": r["cluster"]} for r in rows]
    return {
        "n_total": len(rows),
        "hold": hold,
        "gate_label": label,
        "base": base,
        "kept": kept_s,
        "disaster_rejected": dropped_s,
        "coverage": round(len(kept) / len(rows), 3) if rows else None,
        "delta_med_vs_base": delta_med,
        "two_sample_boot": two_sample_boot(kept, h_all),
        "cluster_boot": cluster_two_sample_boot(kept_records, base_records),
    }


def h3a_leave_one_stock_gate(
    stocks: dict[str, dict], pool: str, keep_fn, *, hold: int = 7
) -> dict:
    sids = [s for s in UNIVERSE if s in stocks]
    per_stock = []
    signs = []
    for s in sids:
        others = [x for x in sids if x != s]
        train_rows = pooled_rows(stocks, pool, others)
        held_rows = stocks[s]["rows"][pool]
        train_kept = [r["path"][hold] for r in train_rows if keep_fn(r)]
        train_base = [r["path"][hold] for r in train_rows]
        held_kept = [r["path"][hold] for r in held_rows if keep_fn(r)]
        held_base = [r["path"][hold] for r in held_rows]
        train_delta = (
            round(float(np.median(train_kept) - np.median(train_base)), 3)
            if train_kept and train_base
            else None
        )
        held_delta = (
            round(float(np.median(held_kept) - np.median(held_base)), 3)
            if held_kept and held_base
            else None
        )
        agree = None
        if train_delta is not None and held_delta is not None:
            agree = (train_delta > 0) == (held_delta > 0)
            signs.append(agree)
        per_stock.append(
            {
                "sid": s,
                "name": stocks[s]["name"],
                "n_train_kept": len(train_kept),
                "train_delta_med": train_delta,
                "n_held_kept": len(held_kept),
                "held_delta_med": held_delta,
                "sign_agree": agree,
            }
        )
    n_pos = sum(1 for a in signs if a)
    verdict = "n/a"
    if signs:
        frac = n_pos / len(signs)
        verdict = "PASS" if frac >= 0.75 else ("MIXED" if frac >= 0.5 else "FAIL")
    return {
        "per_stock": per_stock,
        "n_agree": n_pos,
        "n_evaluable": len(signs),
        "verdict": verdict,
    }


def run_extend_phase(
    stocks: dict[str, dict],
    ix_stage_by_date: dict[str, int],
    *,
    conn: sqlite3.Connection | None = None,
) -> dict:
    """Phase-2: H1/H2 on all W≥3 stocks · dual gate E5 · knowledge backfill."""
    annotate_ix_stage(stocks, ix_stage_by_date)

    h1_all: dict[str, dict] = {}
    h2_all: dict[str, dict] = {}
    for sid, s in stocks.items():
        h1_all[sid] = {
            "n_waves": len(s["waves"]),
            "HARD": h1_leave_one_wave(s, "HARD"),
            "OR": h1_leave_one_wave(s, "OR"),
        }
        h2_all[sid] = {
            "n_waves": len(s["waves"]),
            "HARD": h2_rolling_wave(s, "HARD"),
            "OR": h2_rolling_wave(s, "OR"),
        }

    dual_pooled = {
        pool: pool_metrics_gate(
            pooled_rows(stocks, pool), keep_dual_s2, label="stock_S2_and_ix_S2"
        )
        for pool in ("HARD", "OR")
    }
    stage2_pooled = {
        pool: pool_metrics_gate(
            pooled_rows(stocks, pool), keep_stage2, label="stock_S2"
        )
        for pool in ("HARD", "OR")
    }
    h3a_dual = {
        pool: h3a_leave_one_stock_gate(stocks, pool, keep_dual_s2) for pool in ("HARD", "OR")
    }

    # aggregate H1/H2 among stocks with W≥3
    def _agg_verdicts(block: dict[str, dict], pool: str) -> dict:
        rows = []
        for sid, pack in block.items():
            r = pack[pool]
            if r.get("status") == "insufficient_waves":
                continue
            if r.get("verdict") in (None, "n/a"):
                continue
            rows.append({"sid": sid, "verdict": r["verdict"], "n_waves": pack["n_waves"]})
        n_pass = sum(1 for x in rows if x["verdict"] == "PASS")
        n_fail = sum(1 for x in rows if x["verdict"] == "FAIL")
        n_mixed = sum(1 for x in rows if x["verdict"] == "MIXED")
        return {
            "n_stocks_evaluable": len(rows),
            "n_pass": n_pass,
            "n_mixed": n_mixed,
            "n_fail": n_fail,
            "per_stock": rows,
            "verdict": (
                "PASS"
                if rows and n_pass / len(rows) >= 0.75
                else (
                    "MIXED"
                    if rows and (n_pass + n_mixed) / len(rows) >= 0.5
                    else ("FAIL" if rows else "n/a")
                )
            ),
        }

    backfill = backfill_knowledge_stage(conn) if conn is not None else {"n": 0}

    return {
        "H1_all_stocks": h1_all,
        "H2_all_stocks": h2_all,
        "H1_agg": {p: _agg_verdicts(h1_all, p) for p in ("HARD", "OR")},
        "H2_agg": {p: _agg_verdicts(h2_all, p) for p in ("HARD", "OR")},
        "gate_compare_pooled": {
            "stock_S2": stage2_pooled,
            "stock_S2_and_ix_S2": dual_pooled,
        },
        "H3a_dual_gate": h3a_dual,
        "knowledge_backfill": backfill,
    }


def backfill_knowledge_stage(conn: sqlite3.Connection) -> dict:
    """Tag every expert-pool knowledge trade with Stage quality (observe)."""
    from research.expert_pool_stage_quality import detect_stage_quality

    trades_dir = (
        ROOT
        / "reports/research/branch-footprint-screen/expert_pool/knowledge/trades"
    )
    rows = []
    for fp in sorted(trades_dir.glob("*.json")):
        d = json.loads(fp.read_text(encoding="utf-8"))
        sid = str(d.get("stock_id") or fp.stem)
        name = d.get("stock_name") or sid
        for t in d.get("trades") or []:
            sig = t.get("signal_date") or t.get("date")
            if not sig:
                continue
            sq = detect_stage_quality(conn, stock_id=sid, asof=str(sig)[:10])
            rows.append(
                {
                    "sid": sid,
                    "name": name,
                    "signal_date": str(sig)[:10],
                    "r_adj_pct": t.get("r_adj_pct"),
                    "weinstein_stage": (sq or {}).get("weinstein_stage"),
                    "weinstein_stage_name": (sq or {}).get("weinstein_stage_name"),
                    "ta_quality": (sq or {}).get("ta_quality"),
                    "follow_weight": (sq or {}).get("follow_weight"),
                    "extension_pct": (sq or {}).get("extension_pct"),
                }
            )
    out_csv = (
        ROOT
        / "reports/research/branch-footprint-screen/expert_pool/knowledge"
        / "stage_quality_backfill.csv"
    )
    df = pd.DataFrame(rows)
    df.to_csv(out_csv, index=False)
    # summary by ta_quality
    summary = {}
    if not df.empty and "r_adj_pct" in df.columns:
        for q, g in df.groupby("ta_quality"):
            ra = pd.to_numeric(g["r_adj_pct"], errors="coerce").dropna()
            summary[str(q)] = {
                "n": int(len(g)),
                "med_radj": float(ra.median()) if len(ra) else None,
                "mean_radj": float(ra.mean()) if len(ra) else None,
                "win": float((ra > 0).mean()) if len(ra) else None,
            }
    return {
        "path": str(out_csv.relative_to(ROOT)),
        "n_trades": int(len(df)),
        "by_ta_quality": summary,
    }


def write_extend_report(ext: dict) -> None:
    lines = [
        "# Stage2 多波段 · 延伸相位（W≥3 全股 H1/H2 · 雙閘 E5 · 知識庫回填）",
        "",
        f"- Built: `{datetime.now().isoformat(timespec='seconds')}`",
        "- Research only · **未採納** · 不寫 strategy／order",
        "",
        "## 動機",
        "",
        "國巨僅 2 個 Stage2 波段 → H1/H2 無法驗。改在 **W≥3 個股**上跑 leave-one-wave／"
        "rolling-wave；並用 6669 診斷 E5（個股 S2 ∧ 大盤 S2）測雙閘能否救 HARD 跨股。",
        "",
        "## H1 leave-one-wave · 全股（W≥3 才可評）",
        "",
    ]
    for pool in ("HARD", "OR"):
        agg = ext["H1_agg"][pool]
        lines.append(
            f"- **{pool}** 可評 {agg['n_stocks_evaluable']} 檔 · "
            f"PASS {agg['n_pass']}／MIXED {agg['n_mixed']}／FAIL {agg['n_fail']} → "
            f"**{agg['verdict']}**"
        )
        for r in agg["per_stock"]:
            lines.append(f"  - {r['sid']} W={r['n_waves']} → {r['verdict']}")
    lines += ["", "## H2 rolling-wave · 全股", ""]
    for pool in ("HARD", "OR"):
        agg = ext["H2_agg"][pool]
        lines.append(
            f"- **{pool}** 可評 {agg['n_stocks_evaluable']} 檔 · "
            f"PASS {agg['n_pass']}／MIXED {agg['n_mixed']}／FAIL {agg['n_fail']} → "
            f"**{agg['verdict']}**"
        )
        for r in agg["per_stock"]:
            lines.append(f"  - {r['sid']} W={r['n_waves']} → {r['verdict']}")

    lines += [
        "",
        "## 閘門對照 · pooled（stock S2 vs stock∧IX S2）",
        "",
        "| pool | gate | n | cov | base_med | kept_med | Δmed | cluster CI | p |",
        "|---|---|--:|--:|--:|--:|--:|---|--:|",
    ]
    for gname, block in ext["gate_compare_pooled"].items():
        for pool, m in block.items():
            ci = (m.get("cluster_boot") or {}).get("ci")
            ci_s = f"[{ci[0]:+.2f},{ci[1]:+.2f}]" if ci else "—"
            pp = (m.get("cluster_boot") or {}).get("p_pos")
            lines.append(
                f"| {pool} | `{gname}` | {m['n_total']} | {m['coverage']} | "
                f"{_s(m['base']['med'])} | {_s(m['kept']['med'])} | "
                f"{m['delta_med_vs_base']} | {ci_s} | {pp} |"
            )

    lines += ["", "## H3a leave-one-stock · 雙閘 stock∧IX S2", ""]
    for pool, r in ext["H3a_dual_gate"].items():
        lines.append(
            f"- **{pool}**：方向一致 {r['n_agree']}/{r['n_evaluable']} → **{r['verdict']}**"
        )
        for ps in r["per_stock"]:
            lines.append(
                f"  - {ps['sid']}: held Δ={ps['held_delta_med']} · "
                f"agree={ps['sign_agree']}"
            )

    bf = ext.get("knowledge_backfill") or {}
    lines += [
        "",
        "## 知識庫綠燈 · Stage 品質歷史回填",
        "",
        f"- 檔：`{bf.get('path')}` · n={bf.get('n_trades')}",
        "",
        "| ta_quality | n | med r_adj | mean | win |",
        "|---|--:|--:|--:|--:|",
    ]
    for q, s in (bf.get("by_ta_quality") or {}).items():
        lines.append(
            f"| `{q}` | {s['n']} | {_s(s['med_radj'])} | {_s(s['mean_radj'])} | "
            f"{s['win']:.0%} |"
            if s.get("win") is not None
            else f"| `{q}` | {s['n']} | — | — | — |"
        )
    lines += [
        "",
        "## 判讀",
        "",
        "- 若 W≥3 股上 H1/H2 多數 PASS → 波段泛化證據補強（不必等國巨第 3 波）。",
        "- 若雙閘救得了 HARD H3a 且 CI 收斂 → 觀察欄可升級為「個股S2∧大盤S2」確認。",
        "- 仍 **不寫** strategy／order，直至 G0–G5 過關。",
        "",
    ]
    (OUT / "REPORT_EXTEND.md").write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# H1: leave-one-wave (2327 primary)
# ---------------------------------------------------------------------------
def h1_leave_one_wave(stock: dict, pool: str, hold: int = 7) -> dict:
    waves = stock["waves"]
    rows = stock["rows"][pool]
    if len(waves) < 3:
        return {"status": "insufficient_waves", "n_waves": len(waves)}
    per_wave = []
    signs = []
    for w in waves:
        test_kept = [
            r["path"][hold] for r in rows if r.get("wave_id") == w["wave_id"]
        ]
        train_base = [
            r["path"][hold] for r in rows if r.get("wave_id") != w["wave_id"]
        ]
        train_kept = [
            r["path"][hold]
            for r in rows
            if r["wein_stage"] == 2 and r.get("wave_id") != w["wave_id"]
        ]
        train_delta = (
            round(np.median(train_kept) - np.median(train_base), 3)
            if len(train_kept) >= 2 and len(train_base) >= 2
            else None
        )
        test_med = round(float(np.median(test_kept)), 3) if test_kept else None
        base_med = round(float(np.median(train_base)), 3) if train_base else None
        test_delta_vs_trainbase = (
            round(test_med - base_med, 3) if test_med is not None and base_med is not None else None
        )
        if test_delta_vs_trainbase is not None:
            signs.append(test_delta_vs_trainbase > 0)
        per_wave.append(
            {
                "wave_id": w["wave_id"],
                "wave_rank": w["wave_rank"],
                "t0": w["t0"],
                "t1": w["t1"],
                "n_test": len(test_kept),
                "test_med_H%d" % hold: test_med,
                "train_delta_med": train_delta,
                "test_delta_vs_train_base": test_delta_vs_trainbase,
            }
        )
    n_pos = sum(1 for s in signs if s)
    verdict = "n/a"
    if signs:
        frac = n_pos / len(signs)
        verdict = "PASS" if frac >= 0.75 else ("MIXED" if frac >= 0.5 else "FAIL")
    return {
        "status": "ok",
        "n_waves": len(waves),
        "per_wave": per_wave,
        "n_pos": n_pos,
        "n_evaluable": len(signs),
        "verdict": verdict,
    }


# ---------------------------------------------------------------------------
# H2: rolling-wave (train wave_rank<=k, test wave_rank=k+1)
# ---------------------------------------------------------------------------
def h2_rolling_wave(stock: dict, pool: str, hold: int = 7) -> dict:
    waves = stock["waves"]
    rows = stock["rows"][pool]
    if len(waves) < 2:
        return {"status": "insufficient_waves", "n_waves": len(waves)}
    steps = []
    signs = []
    for k in range(1, len(waves)):
        train_kept = [
            r["path"][hold]
            for r in rows
            if r["wein_stage"] == 2 and (r.get("wave_rank") or 0) <= k
        ]
        test_kept = [
            r["path"][hold] for r in rows if (r.get("wave_rank") or 0) == k + 1
        ]
        test_base = [
            r["path"][hold]
            for r in rows
            if r["signal_date"] <= waves[k]["t1"]  # up through test wave's end
        ]
        if not train_kept:
            steps.append({"k": k, "status": "no_prior_stage2", "n_test": len(test_kept)})
            continue
        train_med = round(float(np.median(train_kept)), 3)
        test_med = round(float(np.median(test_kept)), 3) if test_kept else None
        if test_med is not None:
            signs.append((test_med > 0) == (train_med > 0) or test_med > 0)
        steps.append(
            {
                "k": k,
                "status": "ok",
                "n_train_stage2": len(train_kept),
                "train_med_H%d" % hold: train_med,
                "n_test": len(test_kept),
                "test_med_H%d" % hold: test_med,
                "test_wave_id": waves[k]["wave_id"] if k < len(waves) else None,
            }
        )
    n_pos = sum(1 for s in signs if s)
    verdict = "n/a"
    if signs:
        frac = n_pos / len(signs)
        verdict = "PASS" if frac >= 0.75 else ("MIXED" if frac >= 0.5 else "FAIL")
    return {
        "status": "ok",
        "n_waves": len(waves),
        "steps": steps,
        "n_pos": n_pos,
        "n_evaluable": len(signs),
        "verdict": verdict,
    }


# ---------------------------------------------------------------------------
# H3a: leave-one-stock
# ---------------------------------------------------------------------------
def h3a_leave_one_stock(stocks: dict[str, dict], pool: str, hold: int = 7) -> dict:
    sids = [s for s in UNIVERSE if s in stocks]
    per_stock = []
    signs = []
    for s in sids:
        others = [x for x in sids if x != s]
        train_rows = pooled_rows(stocks, pool, others)
        held_rows = stocks[s]["rows"][pool]
        train_kept = [r["path"][hold] for r in train_rows if r["wein_stage"] == 2]
        train_base = [r["path"][hold] for r in train_rows]
        held_kept = [r["path"][hold] for r in held_rows if r["wein_stage"] == 2]
        held_base = [r["path"][hold] for r in held_rows]
        train_delta = (
            round(float(np.median(train_kept) - np.median(train_base)), 3)
            if train_kept and train_base
            else None
        )
        held_delta = (
            round(float(np.median(held_kept) - np.median(held_base)), 3)
            if held_kept and held_base
            else None
        )
        agree = None
        if train_delta is not None and held_delta is not None:
            agree = (train_delta > 0) == (held_delta > 0)
            signs.append(agree)
        per_stock.append(
            {
                "sid": s,
                "name": stocks[s]["name"],
                "n_train_kept": len(train_kept),
                "train_delta_med": train_delta,
                "n_held_kept": len(held_kept),
                "held_delta_med": held_delta,
                "sign_agree": agree,
            }
        )
    n_pos = sum(1 for a in signs if a)
    verdict = "n/a"
    if signs:
        frac = n_pos / len(signs)
        verdict = "PASS" if frac >= 0.75 else ("MIXED" if frac >= 0.5 else "FAIL")
    return {
        "per_stock": per_stock,
        "n_agree": n_pos,
        "n_evaluable": len(signs),
        "verdict": verdict,
    }


# ---------------------------------------------------------------------------
# H3b: leave-one-IX-wave (market regime holdout)
# ---------------------------------------------------------------------------
def h3b_leave_one_ix_wave(
    stocks: dict[str, dict], ix_waves: list[dict], pool: str, hold: int = 7
) -> dict:
    if len(ix_waves) < 2:
        return {"status": "insufficient_ix_waves", "n_ix_waves": len(ix_waves)}
    all_rows = pooled_rows(stocks, pool)
    per_wave = []
    signs = []
    for w in ix_waves:
        held_rows = [r for r in all_rows if w["t0"] <= r["signal_date"] <= w["t1"]]
        train_rows = [r for r in all_rows if not (w["t0"] <= r["signal_date"] <= w["t1"])]
        train_kept = [r["path"][hold] for r in train_rows if r["wein_stage"] == 2]
        train_base = [r["path"][hold] for r in train_rows]
        held_kept = [r["path"][hold] for r in held_rows if r["wein_stage"] == 2]
        held_base = [r["path"][hold] for r in held_rows]
        train_delta = (
            round(float(np.median(train_kept) - np.median(train_base)), 3)
            if train_kept and train_base
            else None
        )
        held_delta = (
            round(float(np.median(held_kept) - np.median(held_base)), 3)
            if held_kept and held_base
            else None
        )
        agree = None
        if train_delta is not None and held_delta is not None:
            agree = (train_delta > 0) == (held_delta > 0)
            signs.append(agree)
        per_wave.append(
            {
                "ix_wave_id": w["wave_id"],
                "t0": w["t0"],
                "t1": w["t1"],
                "n_train_kept": len(train_kept),
                "train_delta_med": train_delta,
                "n_held_kept": len(held_kept),
                "n_held_total": len(held_rows),
                "held_delta_med": held_delta,
                "sign_agree": agree,
            }
        )
    n_pos = sum(1 for a in signs if a)
    verdict = "n/a"
    if signs:
        frac = n_pos / len(signs)
        verdict = "PASS" if frac >= 0.75 else ("MIXED" if frac >= 0.5 else "FAIL")
    return {
        "status": "ok",
        "n_ix_waves": len(ix_waves),
        "per_wave": per_wave,
        "n_agree": n_pos,
        "n_evaluable": len(signs),
        "verdict": verdict,
    }


# ---------------------------------------------------------------------------
# 6669 diagnostics E1 / E3 / E5
# ---------------------------------------------------------------------------
def diag_6669(stock: dict, ix_stage_by_date: dict[str, int]) -> dict:
    out: dict = {}
    for pool in ("HARD", "OR"):
        rows = [r for r in stock["rows"][pool] if r["wein_stage"] == 2]
        if len(rows) < 4:
            out[pool] = {"note": f"n={len(rows)} too few for diagnostics"}
            continue
        # E1: ext20 tail (top quartile of extension) vs rest
        exts = sorted(r["ext20"] for r in rows if r["ext20"] is not None)
        e1 = {"note": "n too few"}
        if len(exts) >= 4:
            q75 = float(np.quantile(exts, 0.75))
            tail = [r["path"][7] for r in rows if r["ext20"] is not None and r["ext20"] >= q75]
            rest = [r["path"][7] for r in rows if r["ext20"] is not None and r["ext20"] < q75]
            e1 = {
                "ext20_q75": round(q75, 4),
                "tail_ext_H7": stats(tail),
                "rest_H7": stats(rest),
                "explains": (
                    stats(tail)["med"] is not None
                    and stats(rest)["med"] is not None
                    and stats(tail)["med"] < stats(rest)["med"]
                    and stats(tail)["n"] >= 3
                ),
            }
        # E3: H7 vs H10
        h7 = stats([r["path"][7] for r in rows])
        h10 = stats([r["path"][10] for r in rows])
        e3 = {
            "H7": h7,
            "H10": h10,
            "delta_H10_minus_H7_med": (
                round(h10["med"] - h7["med"], 3)
                if h10["med"] is not None and h7["med"] is not None
                else None
            ),
            "explains": (
                h7["med"] is not None
                and h10["med"] is not None
                and h10["med"] > h7["med"] + 1.0
            ),
        }
        # E5: stock Stage 2 ∧ IX Stage 2  vs  stock Stage 2 ∧ ¬IX Stage 2
        both = [
            r["path"][7]
            for r in rows
            if ix_stage_by_date.get(r["signal_date"], 0) == 2
        ]
        stock_only = [
            r["path"][7]
            for r in rows
            if ix_stage_by_date.get(r["signal_date"], 0) != 2
        ]
        s_both, s_only = stats(both), stats(stock_only)
        e5 = {
            "stock_S2_and_ix_S2": s_both,
            "stock_S2_and_not_ix_S2": s_only,
            "delta_med": (
                round(s_both["med"] - s_only["med"], 3)
                if s_both["med"] is not None and s_only["med"] is not None
                else None
            ),
            "explains": (
                s_both["n"] >= 3
                and s_only["n"] >= 3
                and s_both["med"] is not None
                and s_only["med"] is not None
                and s_both["med"] > s_only["med"] + 1.0
            ),
        }
        explained = any([e1.get("explains"), e3.get("explains"), e5.get("explains")])
        out[pool] = {
            "n_stage2": len(rows),
            "E1_ext20_tail": e1,
            "E3_H7_vs_H10": e3,
            "E5_stock_S2_vs_ix_S2": e5,
            "status": "explained" if explained else "unresolved",
        }
    return out


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    ta_mod = load_mod("scripts/research/run_2327_ta_adaptive_exit.py", "ta_adaptive_mod")
    epk_mod = load_mod("scripts/research/build_expert_pool_trade_knowledge.py", "epk_build_mod")
    wmod = epk_mod.load_watch_mod()

    conn = connect_ro()
    ix_bars = epk_mod.load_ix(conn)  # (date, open, close) for r_adj benchmark
    ix_df = pd.DataFrame(ix_bars, columns=["date", "open", "close"])
    ix_ohlc_full = load_ix_ohlc_full(conn)  # full OHLCV for IX own Weinstein stage
    ix_cal = [d for d, *_ in ix_bars if START <= d <= END]
    ix_stage_by_date = compute_daily_stage(ta_mod, ix_ohlc_full, ix_cal)
    ix_waves = find_waves("IX0001", ix_cal, ix_stage_by_date)

    t_build0 = datetime.now()
    stocks: dict[str, dict] = {}
    for sid in UNIVERSE:
        s = build_stock(conn, ta_mod, epk_mod, wmod, ix_bars, ix_df, sid)
        if s is None:
            print(f"  skip {sid} (insufficient data)")
            continue
        stocks[sid] = s
        print(
            f"  built {sid} {s['name']}: HARD n={len(s['rows']['HARD'])} "
            f"OR n={len(s['rows']['OR'])} waves={len(s['waves'])}"
        )
    conn.close()
    build_secs = (datetime.now() - t_build0).total_seconds()

    # ---- per-stock, per-pool metrics ----
    per_stock_metrics = {}
    for sid, s in stocks.items():
        per_stock_metrics[sid] = {
            "name": s["name"],
            "waves": [
                {k: v for k, v in w.items() if k != "sid"} for w in s["waves"]
            ],
            "HARD": pool_metrics(s["rows"]["HARD"]),
            "OR": pool_metrics(s["rows"]["OR"]),
        }

    # ---- pooled (universe) metrics per pool (separate — 分池不合併主Δ) ----
    pooled_metrics = {
        pool: pool_metrics(pooled_rows(stocks, pool)) for pool in ("HARD", "OR")
    }

    # ---- H1 / H2 (2327 primary) ----
    stock_main = stocks.get(SID_MAIN)
    h1 = {pool: h1_leave_one_wave(stock_main, pool) for pool in ("HARD", "OR")} if stock_main else {}
    h2 = {pool: h2_rolling_wave(stock_main, pool) for pool in ("HARD", "OR")} if stock_main else {}

    # ---- H3a / H3b (pooled universe) ----
    h3a = {pool: h3a_leave_one_stock(stocks, pool) for pool in ("HARD", "OR")}
    h3b = {
        pool: h3b_leave_one_ix_wave(stocks, ix_waves, pool) for pool in ("HARD", "OR")
    }

    # ---- 6669 diagnostics ----
    diag = diag_6669(stocks[SID_DIAG], ix_stage_by_date) if SID_DIAG in stocks else {
        "error": "6669 missing"
    }

    # ---- extend phase (W≥3 H1/H2 · dual gate · knowledge backfill) ----
    conn2 = connect_ro()
    extend = run_extend_phase(stocks, ix_stage_by_date, conn=conn2)
    conn2.close()

    payload = {
        "spec_id": "stage2_multiwave_validate",
        "status": "observe",
        "layer": "research",
        "built_at": datetime.now().isoformat(timespec="seconds"),
        "build_secs": round(build_secs, 1),
        "universe": UNIVERSE,
        "protocol": {
            "hard_pool": "回看1日共識@0.5億",
            "or_pool": "OR@0.5億",
            "note": "HARD/OR 分池報告；不合併主Δ",
            "window": f"{START}..{END}",
            "cost_bps": int(COST * 10000),
            "beta": BETA,
            "dedup_days": DEDUP,
            "hold": "L1H7",
            "kmax": KMAX,
            "gate": "wein_stage==2 (PIT classify_weinstein_stage(bars<=T))",
            "boot": BOOT,
            "wave_def": {
                "min_len_trading_days": WAVE_MIN_LEN,
                "gap_merge_trading_days": WAVE_GAP_MERGE,
                "wave_id_fmt": "{sid}|S2|{t0}|{t1}",
            },
            "perf_note": (
                f"逐日精算 classify_weinstein_stage（非近似）；建置 {len(stocks)} 檔 "
                f"× ~500 交易日 stage 序列耗時 {build_secs:.1f}s，未觸發近似捷徑"
            ),
        },
        "ix_waves": [{k: v for k, v in w.items() if k != "sid"} for w in ix_waves],
        "per_stock": per_stock_metrics,
        "pooled": pooled_metrics,
        "holdouts": {
            "H1_leave_one_wave_2327": h1,
            "H2_rolling_wave_2327": h2,
            "H3a_leave_one_stock": h3a,
            "H3b_leave_one_ix_wave": h3b,
        },
        "diag_6669": diag,
        "extend": {
            "H1_agg": extend["H1_agg"],
            "H2_agg": extend["H2_agg"],
            "gate_compare_pooled": extend["gate_compare_pooled"],
            "H3a_dual_gate": {
                p: {
                    "verdict": extend["H3a_dual_gate"][p]["verdict"],
                    "n_agree": extend["H3a_dual_gate"][p]["n_agree"],
                    "n_evaluable": extend["H3a_dual_gate"][p]["n_evaluable"],
                    "per_stock": extend["H3a_dual_gate"][p]["per_stock"],
                }
                for p in ("HARD", "OR")
            },
            "knowledge_backfill": extend["knowledge_backfill"],
            # full per-stock H1/H2 kept in extend_result.json to avoid huge main payload
        },
        "adoption": {"strategy_yaml": False, "order_yaml": False},
    }
    (OUT / "multiwave_result.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    (OUT / "extend_result.json").write_text(
        json.dumps(extend, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )

    # waves.json (optional per spec, still write for traceability)
    waves_payload = {
        "ix_waves": [{k: v for k, v in w.items() if k != "sid"} for w in ix_waves],
        "stocks": {
            sid: [{k: v for k, v in w.items() if k != "sid"} for w in s["waves"]]
            for sid, s in stocks.items()
        },
    }
    (OUT / "waves.json").write_text(
        json.dumps(waves_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    # event_panel.csv — every trade across all stocks/pools
    panel_rows = []
    for sid, s in stocks.items():
        for pool in ("HARD", "OR"):
            for r in s["rows"][pool]:
                panel_rows.append(
                    {
                        "sid": sid,
                        "name": s["name"],
                        "pool": pool,
                        "signal_date": r["signal_date"],
                        "entry_date": r["entry_date"],
                        "wein_stage": r["wein_stage"],
                        "wave_id": r["wave_id"],
                        "wave_rank": r["wave_rank"],
                        "cluster": r["cluster"],
                        "r_adj_H3": round(r["path"][3], 2),
                        "r_adj_H5": round(r["path"][5], 2),
                        "r_adj_H7": round(r["path"][7], 2),
                        "r_adj_H10": round(r["path"][10], 2),
                        "ext20": round(r["ext20"], 4) if r["ext20"] is not None else None,
                        "rs_mom20": round(r["rs_mom20"], 3) if r["rs_mom20"] is not None else None,
                        "ix_stage_at_signal": r.get("ix_stage")
                        if r.get("ix_stage") is not None
                        else ix_stage_by_date.get(r["signal_date"]),
                        "dual_s2": r.get("dual_s2"),
                    }
                )
    pd.DataFrame(panel_rows).to_csv(OUT / "event_panel.csv", index=False)

    write_freeze_spec(payload)
    write_report(payload, stocks)
    write_extend_report(extend)

    print(f"\nbuild: {len(stocks)} stocks in {build_secs:.1f}s")
    print(f"pooled HARD: {json.dumps(pooled_metrics['HARD'], default=str)[:300]}")
    print(f"pooled OR:   {json.dumps(pooled_metrics['OR'], default=str)[:300]}")
    for hname, hres in payload["holdouts"].items():
        print(f"{hname}: {json.dumps(hres, default=str)[:200]}")
    print(f"diag_6669: {json.dumps(diag, default=str)[:400]}")
    print("H1_agg", json.dumps(extend["H1_agg"], default=str)[:400])
    print("H2_agg", json.dumps(extend["H2_agg"], default=str)[:400])
    print(
        "dual HARD",
        json.dumps(extend["gate_compare_pooled"]["stock_S2_and_ix_S2"]["HARD"], default=str)[:250],
    )
    print("H3a_dual", {p: extend["H3a_dual_gate"][p]["verdict"] for p in ("HARD", "OR")})
    print("backfill", extend["knowledge_backfill"])
    print("wrote", OUT)


def write_freeze_spec(payload: dict) -> None:
    m2327 = payload["per_stock"].get(SID_MAIN, {})
    spec = {
        "spec_id": "stage2_multiwave_validate",
        "status": "observe",
        "layer": "research",
        "built_at": payload["built_at"],
        "gate": "wein_stage==2 · PIT classify_weinstein_stage(bars<=T)",
        "hard_stop": (
            "任一 holdout（H1/H2/H3a/H3b）在主池（HARD 或 OR）verdict=FAIL，"
            "或 pooled cluster_boot CI 含 0 且 delta_med<=0 → 凍結，不得寫入 "
            "config/strategy.yaml 或 config/order.yaml"
        ),
        "upgrade_gates": {
            "G0_data_sufficiency": "8 檔皆有 ≥3 個 Stage 2 波段（W>=3）才能跑滿 H1/H2",
            "G1_pooled_significance": (
                "pooled HARD 與 OR 的 cluster_boot CI 皆不含 0，且 delta_med>0"
            ),
            "G2_wave_robustness": "H1 leave-one-wave verdict=PASS（無單一波段主導效應）",
            "G3_temporal_robustness": "H2 rolling-wave verdict != FAIL（非僅回顧擬合）",
            "G4_cross_stock_robustness": "H3a leave-one-stock verdict=PASS（≥6/8 檔方向一致）",
            "G5_regime_robustness": "H3b leave-one-IX-wave verdict != FAIL（非單一大盤多頭期偶然）",
        },
        "current_snapshot": {
            "pooled_HARD": payload["pooled"]["HARD"],
            "pooled_OR": payload["pooled"]["OR"],
            "H1_2327": payload["holdouts"]["H1_leave_one_wave_2327"],
            "H2_2327": payload["holdouts"]["H2_rolling_wave_2327"],
            "H3a": {
                p: {"verdict": payload["holdouts"]["H3a_leave_one_stock"][p]["verdict"]}
                for p in ("HARD", "OR")
            },
            "H3b": {
                p: {
                    "verdict": payload["holdouts"]["H3b_leave_one_ix_wave"][p].get(
                        "verdict", payload["holdouts"]["H3b_leave_one_ix_wave"][p].get("status")
                    )
                }
                for p in ("HARD", "OR")
            },
            "diag_6669_status": {
                p: payload["diag_6669"].get(p, {}).get("status")
                for p in ("HARD", "OR")
                if isinstance(payload["diag_6669"].get(p), dict)
            },
            "extend": payload.get("extend"),
        },
        "note": (
            "Research only · 未採納 · 不寫 strategy/order；"
            "extend 相位含 W≥3 全股 H1/H2、雙閘 E5、知識庫 Stage 回填。"
        ),
    }
    (OUT / "freeze_observe_spec.json").write_text(
        json.dumps(spec, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )


def _fmt_ci(b: dict) -> str:
    if not b or b.get("ci") is None:
        return "CI—"
    return f"CI[{b['ci'][0]:+.2f},{b['ci'][1]:+.2f}] p={b.get('p_pos')}"


def write_report(payload: dict, stocks: dict[str, dict]) -> None:
    lines = [
        "# Weinstein Stage 2 多波段驗證（Research only · 未採納）",
        "",
        f"- Built: `{payload['built_at']}` · build {payload['build_secs']}s",
        f"- Universe A（8 檔）：{', '.join(UNIVERSE)}",
        f"- 協議：{payload['protocol']['hard_pool']} / {payload['protocol']['or_pool']} · "
        f"{payload['protocol']['note']} · {payload['protocol']['window']} · "
        f"{payload['protocol']['cost_bps']}bps · β={payload['protocol']['beta']} · "
        f"dedup{payload['protocol']['dedup_days']} · {payload['protocol']['hold']}",
        f"- Gate：`{payload['protocol']['gate']}` · Bootstrap B={payload['protocol']['boot']}",
        f"- 波段定義：連續 Stage 2 ≥{WAVE_MIN_LEN} 交易日；中斷 ≤{WAVE_GAP_MERGE} 日合併；"
        "`wave_id={sid}|S2|{t0}|{t1}`",
        f"- 效能：{payload['protocol']['perf_note']}",
        "- **Research only · 未採納**（不寫 strategy／order）",
        "",
        "## Pooled（8 檔合併，HARD／OR 分池，不合併主Δ）",
        "",
        "| pool | n | coverage | base_med | S2_med | Δmed | 非S2(disaster) med | two_sample CI | cluster CI |",
        "|---|--:|--:|--:|--:|--:|--:|---|---|",
    ]
    for pool in ("HARD", "OR"):
        m = payload["pooled"][pool]
        tb, cb = m["two_sample_boot"], m["cluster_boot"]
        lines.append(
            f"| {pool} | {m['n_total']} | {m['coverage']} | {_s(m['base']['med'])} | "
            f"{_s(m['kept_stage2']['med'])} | {m['delta_med_vs_base']} | "
            f"{_s(m['disaster_non_stage2']['med'])} | {_fmt_ci(tb)} | {_fmt_ci(cb)} |"
        )

    lines += ["", "## 逐股（HARD／OR）", "", "| sid | 名稱 | pool | n | waves(W) | 覆蓋率 | S2 med | Δmed | disaster med |",
              "|---|---|---|--:|--:|--:|--:|--:|--:|"]
    for sid in UNIVERSE:
        pm = payload["per_stock"].get(sid)
        if not pm:
            continue
        w = len(pm["waves"])
        for pool in ("HARD", "OR"):
            m = pm[pool]
            lines.append(
                f"| {sid} | {pm['name']} | {pool} | {m['n_total']} | {w} | "
                f"{m['coverage']} | {_s(m['kept_stage2']['med'])} | "
                f"{m['delta_med_vs_base']} | {_s(m['disaster_non_stage2']['med'])} |"
            )

    lines += ["", "## Holdouts", ""]
    h1 = payload["holdouts"]["H1_leave_one_wave_2327"]
    lines.append("### H1 leave-one-wave（2327 為主）")
    lines.append("")
    for pool in ("HARD", "OR"):
        r = h1.get(pool, {})
        if r.get("status") == "insufficient_waves":
            lines.append(f"- `{pool}`：W={r['n_waves']} < 3 → **insufficient_waves**")
        else:
            lines.append(
                f"- `{pool}`：W={r.get('n_waves')} · 方向一致 {r.get('n_pos')}/{r.get('n_evaluable')} "
                f"→ **{r.get('verdict')}**"
            )
    lines.append("")
    h2 = payload["holdouts"]["H2_rolling_wave_2327"]
    lines.append("### H2 rolling-wave（2327；train wave_rank≤k → test k+1）")
    lines.append("")
    for pool in ("HARD", "OR"):
        r = h2.get(pool, {})
        if r.get("status") == "insufficient_waves":
            lines.append(f"- `{pool}`：W={r['n_waves']} < 2 → **insufficient_waves**")
        else:
            lines.append(
                f"- `{pool}`：W={r.get('n_waves')} · 方向一致 {r.get('n_pos')}/{r.get('n_evaluable')} "
                f"→ **{r.get('verdict')}**"
            )
    lines.append("")
    h3a = payload["holdouts"]["H3a_leave_one_stock"]
    lines.append("### H3a leave-one-stock（8 檔輪流排除一檔）")
    lines.append("")
    for pool in ("HARD", "OR"):
        r = h3a.get(pool, {})
        lines.append(
            f"- `{pool}`：方向一致 {r.get('n_agree')}/{r.get('n_evaluable')} → **{r.get('verdict')}**"
        )
    lines.append("")
    h3b = payload["holdouts"]["H3b_leave_one_ix_wave"]
    lines.append("### H3b leave-one-IX-wave（大盤 Weinstein Stage 2 波段 holdout）")
    lines.append("")
    for pool in ("HARD", "OR"):
        r = h3b.get(pool, {})
        if r.get("status") == "insufficient_ix_waves":
            lines.append(f"- `{pool}`：IX waves={r['n_ix_waves']} < 2 → **insufficient_ix_waves**")
        else:
            lines.append(
                f"- `{pool}`：IX waves={r.get('n_ix_waves')} · 方向一致 "
                f"{r.get('n_agree')}/{r.get('n_evaluable')} → **{r.get('verdict')}**"
            )
    lines.append("")

    lines += ["## 6669 診斷 E1/E3/E5（解釋 Stage 2 gate 對緯穎為負／偏弱的原因）", ""]
    diag = payload.get("diag_6669", {})
    for pool in ("HARD", "OR"):
        d = diag.get(pool, {})
        if "note" in d:
            lines += [f"### `{pool}`", "", f"- {d['note']}", ""]
            continue
        lines += [f"### `{pool}` · n_stage2={d.get('n_stage2')} · status=**{d.get('status')}**", ""]
        e1 = d.get("E1_ext20_tail", {})
        if e1.get("tail_ext_H7"):
            lines.append(
                f"- E1 ext20 尾部（≥P75={e1.get('ext20_q75')}）：tail med "
                f"{_s(e1['tail_ext_H7']['med'])}（n={e1['tail_ext_H7']['n']}） vs rest med "
                f"{_s(e1['rest_H7']['med'])}（n={e1['rest_H7']['n']}） · explains={e1.get('explains')}"
            )
        e3 = d.get("E3_H7_vs_H10", {})
        if e3.get("H7"):
            lines.append(
                f"- E3 H7 vs H10：H7 med {_s(e3['H7']['med'])} → H10 med "
                f"{_s(e3['H10']['med'])} · Δ={e3.get('delta_H10_minus_H7_med')} · "
                f"explains={e3.get('explains')}"
            )
        e5 = d.get("E5_stock_S2_vs_ix_S2", {})
        if e5.get("stock_S2_and_ix_S2"):
            lines.append(
                f"- E5 stock_S2∧ix_S2 med {_s(e5['stock_S2_and_ix_S2']['med'])}"
                f"（n={e5['stock_S2_and_ix_S2']['n']}） vs stock_S2∧¬ix_S2 med "
                f"{_s(e5['stock_S2_and_not_ix_S2']['med'])}"
                f"（n={e5['stock_S2_and_not_ix_S2']['n']}） · Δ={e5.get('delta_med')} · "
                f"explains={e5.get('explains')}"
            )
        lines.append("")

    lines += [
        "## 波段清單（8 檔 + IX0001）",
        "",
        "| sid | wave_id | t0 | t1 | 長度(交易日) |",
        "|---|---|---|---|--:|",
    ]
    for w in payload.get("ix_waves", []):
        wid = w["wave_id"].replace("|", "\\|")
        lines.append(f"| IX0001 | `{wid}` | {w['t0']} | {w['t1']} | {w['len_trading_days']} |")
    for sid in UNIVERSE:
        pm = payload["per_stock"].get(sid)
        if not pm:
            continue
        for w in pm["waves"]:
            wid = w["wave_id"].replace("|", "\\|")
            lines.append(f"| {sid} | `{wid}` | {w['t0']} | {w['t1']} | {w['len_trading_days']} |")
    lines.append("")

    lines += [
        "## 判讀與 freeze 狀態",
        "",
        "- `status: observe`（見 `freeze_observe_spec.json`）· gate / hard_stop / "
        "upgrade gates G0–G5 皆已定義，供下一輪波段出現後複驗。",
        "- 本輪僅是波段泛化驗證，**不改動** `config/strategy.yaml` / `config/order.yaml`。",
        "",
    ]
    (OUT / "REPORT_MULTIWAVE.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
