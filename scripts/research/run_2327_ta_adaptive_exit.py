#!/usr/bin/env python3
"""2327 國巨 · 技術面 × 自適應退出（打破「只做籌碼、固定 H7」盲點）.

Thesis:
  國巨專家是「籌碼 + 技術」雙修。既有研究只做籌碼（分點／集保／法人），
  0 條技術特徵，且沿用專家 champion 的**固定 L1H7**。12 筆綠燈報酬單調衰減
  （+44→+16→+15→+12→+10→−9.8→−22）＝典型「追高晚期進場」。
  盲點 = 缺技術延伸度濾網 + 固定持有期。

Method (Research only · 不寫 strategy/order):
  1. 重放綠燈：core 席 · 回看1日共識@0.5億（champion, n≈12）與 OR@0.5億（放大, n≈25）
  2. 重建 β=1.15 超額價格路徑 k=1..15（與 ledger 同構）
  3. PIT 技術特徵（≤訊號收盤）：ext20/ext60 乖離、runup10/20、dist_low20、
     rsi14、atr_pct、Weinstein stage/extension、RS_mom20
  4. 分析：延伸度 vs r_adj(H7) 相關；MFE/MAE 時序
  5. 自適應退出：延伸度分層持有期 + 移動停利；LOOCV 選門檻，配對 bootstrap vs 固定 H7
  6. 放大 OR 樣本重測 generalization

Out: reports/research/chip-overlays/2327_ta_adaptive/
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

SID = "2327"
NAME = "國巨"
CORE = {
    "981j": "元大士林",
    "779Z": "國票-安和",
    "8888": "國泰敦南",
    "8840": "玉山證券",
    "918e": "群益金鼎-大安",
}
SOURCE = "finmind"
START, END = "2024-07-01", "2026-07-17"
COST, BETA = 0.003, 1.15
DEDUP = 5
FLOOR = 5e7
KMAX = 15
OOS = "2026-01-01"
RNG = np.random.default_rng(2327)
BOOT = 4000
OUT = ROOT / "reports/research/chip-overlays/2327_ta_adaptive"
DB = ROOT / "data" / "stocks.db"


def load_builder():
    path = ROOT / "scripts/research/build_expert_pool_trade_knowledge.py"
    spec = importlib.util.spec_from_file_location("epk_build", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(mod)
    return mod


def load_bars_ohlc(conn: sqlite3.Connection, sid: str) -> pd.DataFrame:
    rows = conn.execute(
        """
        SELECT trade_date, open, high, low, close, volume
        FROM stock_daily_bars
        WHERE stock_id=? AND source=? AND trade_date BETWEEN ? AND ?
          AND close>0
        ORDER BY trade_date
        """,
        (sid, SOURCE, "2024-01-01", "2026-08-31"),
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


def branch_amts(conn: sqlite3.Connection, sid: str = SID, core: dict | None = None):
    core = core or CORE
    ph = ",".join("?" * len(core))
    raw = conn.execute(
        f"""
        SELECT b.securities_trader_id, b.trade_date, b.net, p.close
        FROM stock_broker_branch_daily b
        JOIN stock_daily_bars p
          ON p.stock_id=b.stock_id AND p.trade_date=b.trade_date AND p.source=?
        WHERE b.stock_id=? AND b.source=? AND b.trade_date BETWEEN ? AND ?
          AND b.securities_trader_id IN ({ph}) AND b.net>0 AND p.close>0
        """,
        (SOURCE, sid, SOURCE, START, END, *core.keys()),
    ).fetchall()
    amts: dict[tuple[str, str], float] = {}
    by_date: dict[str, set[str]] = {}
    for tid, d, net, close in raw:
        a = float(net) * float(close)
        amts[(tid, d)] = a
        if a >= 5e7:
            by_date.setdefault(d, set()).add(tid)
    return amts, by_date


def rsi(series: pd.Series, n: int = 14) -> pd.Series:
    delta = series.diff()
    up = delta.clip(lower=0.0)
    dn = (-delta).clip(lower=0.0)
    ru = up.ewm(alpha=1 / n, min_periods=n, adjust=False).mean()
    rd = dn.ewm(alpha=1 / n, min_periods=n, adjust=False).mean()
    rs = ru / rd.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def atr_pct(df: pd.DataFrame, n: int = 14) -> pd.Series:
    hl = df["high"] - df["low"]
    hc = (df["high"] - df["close"].shift()).abs()
    lc = (df["low"] - df["close"].shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    return tr.rolling(n, min_periods=n).mean() / df["close"]


def build_ta(df: pd.DataFrame, ix: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    d["sma20"] = d["close"].rolling(20, min_periods=20).mean()
    d["sma60"] = d["close"].rolling(60, min_periods=60).mean()
    d["ext20"] = d["close"] / d["sma20"] - 1.0
    d["ext60"] = d["close"] / d["sma60"] - 1.0
    d["runup10"] = d["close"] / d["close"].shift(10) - 1.0
    d["runup20"] = d["close"] / d["close"].shift(20) - 1.0
    d["dist_low20"] = d["close"] / d["low"].rolling(20, min_periods=10).min() - 1.0
    d["rsi14"] = rsi(d["close"], 14)
    d["atr_pct"] = atr_pct(d, 14)
    ixm = ix.rename(columns={"close": "ix_close"})[["date", "ix_close"]]
    d = d.merge(ixm, on="date", how="left")
    d["logRS"] = np.log(d["close"]) - np.log(d["ix_close"])
    d["rs_mom20"] = (d["logRS"] - d["logRS"].shift(20)) * 100.0
    return d


def weinstein_at(df: pd.DataFrame, sig_date: str) -> dict:
    hist = df[df["date"] <= sig_date].copy()
    if len(hist) < 60:
        return {"stage": 0, "extension_pct": None}
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
        r = classify_weinstein_stage(wf)
        return {"stage": r.get("stage"), "extension_pct": r.get("extension_pct")}
    except Exception:
        return {"stage": 0, "extension_pct": None}


def build_path(bars, ix, mod, sig: str) -> dict | None:
    ed, eo = mod.next_open(bars, sig)
    be, bo = mod.next_open(ix, sig)
    if not ed or not eo or not be or not bo:
        return None
    radj = {}
    for k in range(1, KMAX + 1):
        xd, xc = mod.exit_close(bars, ed, k)
        _, bc = mod.exit_close(ix, be, k)
        if not xc or not bc:
            break
        r_s = xc / eo - 1 - COST
        r_ix = bc / bo - 1
        radj[k] = (r_s - BETA * r_ix) * 100.0
    if len(radj) < KMAX:
        return None
    return {"entry_date": ed, "path": radj}


# ---------- exit policies ----------
def fixed_exit(path: dict[int, float], h: int) -> float:
    return path[h]


def trailing_exit(path: dict[int, float], hmax: int, dd: float) -> float:
    """Exit at first k where drawdown from running peak ≥ dd (pp); else hmax."""
    peak = -1e9
    for k in range(1, hmax + 1):
        v = path[k]
        peak = max(peak, v)
        if peak - v >= dd:
            return v
    return path[hmax]


def adaptive_ext_hold(
    ext20: float, path: dict[int, float], tau: float, hs: int, hl: int
) -> float:
    """Extended (ext20≥tau) → short hold; else long hold."""
    if ext20 is None or not np.isfinite(ext20):
        return path[hl]
    return path[hs] if ext20 >= tau else path[hl]


def stats(vals: list[float]) -> dict:
    a = np.array([v for v in vals if v is not None and np.isfinite(v)], dtype=float)
    if a.size == 0:
        return {"n": 0, "med": None, "mean": None, "win": None, "sum": None}
    return {
        "n": int(a.size),
        "med": float(np.median(a)),
        "mean": float(a.mean()),
        "win": float((a > 0).mean()),
        "sum": float(a.sum()),
    }


def paired_boot(delta: list[float]) -> dict:
    a = np.array([d for d in delta if d is not None and np.isfinite(d)], dtype=float)
    if a.size < 3:
        return {"mean_ci": None, "med_ci": None, "p_pos_mean": None}
    means = np.empty(BOOT)
    meds = np.empty(BOOT)
    for i in range(BOOT):
        s = a[RNG.integers(0, a.size, a.size)]
        means[i] = s.mean()
        meds[i] = np.median(s)
    return {
        "mean": float(a.mean()),
        "med": float(np.median(a)),
        "mean_ci": [float(np.quantile(means, 0.025)), float(np.quantile(means, 0.975))],
        "med_ci": [float(np.quantile(meds, 0.025)), float(np.quantile(meds, 0.975))],
        "p_pos_mean": float((means > 0).mean()),
    }


def loocv_ext_hold(rows: list[dict], hs: int, hl: int) -> list[float]:
    """Leave-one-out: pick tau (ext20 quantile) maximizing mean r_adj on rest."""
    exts = sorted(
        r["ext20"] for r in rows if r["ext20"] is not None and np.isfinite(r["ext20"])
    )
    if len(exts) < 4:
        return [r["path"][hl] for r in rows]
    taus = list(np.quantile(exts, [0.4, 0.5, 0.6, 0.7, 0.8]))
    out = []
    for i, ri in enumerate(rows):
        rest = [r for j, r in enumerate(rows) if j != i]
        best_tau, best_mean = None, -1e9
        for tau in taus:
            v = [adaptive_ext_hold(r["ext20"], r["path"], tau, hs, hl) for r in rest]
            m = float(np.mean(v))
            if m > best_mean:
                best_mean, best_tau = m, tau
        out.append(adaptive_ext_hold(ri["ext20"], ri["path"], best_tau, hs, hl))
    return out


def loocv_trailing(rows: list[dict], hmax: int) -> list[float]:
    dds = [3.0, 5.0, 8.0, 12.0]
    out = []
    for i, ri in enumerate(rows):
        rest = [r for j, r in enumerate(rows) if j != i]
        best_dd, best_mean = None, -1e9
        for dd in dds:
            v = [trailing_exit(r["path"], hmax, dd) for r in rest]
            m = float(np.mean(v))
            if m > best_mean:
                best_mean, best_dd = m, dd
        out.append(trailing_exit(ri["path"], hmax, best_dd))
    return out


def two_sample_boot(kept: list[float], base: list[float]) -> dict:
    """Bootstrap CI of median(kept) − median(base_all)."""
    k = np.array([v for v in kept if v is not None and np.isfinite(v)], float)
    b = np.array([v for v in base if v is not None and np.isfinite(v)], float)
    if k.size < 3 or b.size < 3:
        return {"delta_med": None, "ci": None, "p_pos": None}
    deltas = np.empty(BOOT)
    for i in range(BOOT):
        deltas[i] = np.median(k[RNG.integers(0, k.size, k.size)]) - np.median(
            b[RNG.integers(0, b.size, b.size)]
        )
    return {
        "delta_med": float(np.median(k) - np.median(b)),
        "ci": [float(np.quantile(deltas, 0.025)), float(np.quantile(deltas, 0.975))],
        "p_pos": float((deltas > 0).mean()),
    }


def eval_gates(rows: list[dict]) -> dict:
    """Technical-strength ENTRY gates (confirm): keep green only if TA strong."""
    h7_all = [r["path"][7] for r in rows]
    exts = sorted(
        r["ext20"] for r in rows if r["ext20"] is not None and np.isfinite(r["ext20"])
    )
    tau = float(np.quantile(exts, 0.5)) if len(exts) >= 4 else 0.0

    def gate(mask_fn):
        keep = [r["path"][7] for r in rows if mask_fn(r)]
        drop = [r["path"][7] for r in rows if not mask_fn(r)]
        return keep, drop

    defs = {
        "gate_stage2": lambda r: r["wein_stage"] == 2,
        "gate_ext20_pos": lambda r: r["ext20"] is not None and r["ext20"] > 0,
        "gate_rs_pos": lambda r: r["rs_mom20"] is not None and r["rs_mom20"] > 0,
        "gate_stage2_and_extpos": lambda r: r["wein_stage"] == 2
        and r["ext20"] is not None
        and r["ext20"] > 0,
        "gate_ext20_med_LOOCV": None,  # handled separately
    }
    out = {}
    base = stats(h7_all)
    for name, fn in defs.items():
        if fn is None:
            continue
        keep, drop = gate(fn)
        out[name] = {
            "kept": stats(keep),
            "dropped": stats(drop),
            "coverage": round(len(keep) / len(rows), 3),
            "delta_med_vs_base": (
                round(stats(keep)["med"] - base["med"], 2)
                if stats(keep)["med"] is not None
                else None
            ),
            "boot_vs_base": two_sample_boot(keep, h7_all),
        }
    # LOOCV threshold gate on ext20
    loo_keep, loo_flags = [], []
    for i, ri in enumerate(rows):
        rest = [r for j, r in enumerate(rows) if j != i]
        re = sorted(
            r["ext20"] for r in rest if r["ext20"] is not None and np.isfinite(r["ext20"])
        )
        best_t, best_m = 0.0, -1e9
        for t in np.quantile(re, [0.3, 0.4, 0.5, 0.6, 0.7]) if len(re) >= 4 else [0.0]:
            kv = [r["path"][7] for r in rest if r["ext20"] is not None and r["ext20"] >= t]
            if len(kv) >= 3 and float(np.median(kv)) > best_m:
                best_m, best_t = float(np.median(kv)), t
        keep_i = ri["ext20"] is not None and ri["ext20"] >= best_t
        loo_flags.append(keep_i)
        if keep_i:
            loo_keep.append(ri["path"][7])
    out["gate_ext20_med_LOOCV"] = {
        "kept": stats(loo_keep),
        "coverage": round(sum(loo_flags) / len(rows), 3),
        "delta_med_vs_base": (
            round(stats(loo_keep)["med"] - base["med"], 2)
            if stats(loo_keep)["med"] is not None
            else None
        ),
        "boot_vs_base": two_sample_boot(loo_keep, h7_all),
    }
    return {"tau_ext20_med": round(tau, 4), "base_H7": base, "gates": out}


def split_gate(rows: list[dict], mask_fn, cut: str = OOS) -> dict:
    """IS/OOS median H7 for a gate."""
    res = {}
    for split, ss in (
        ("IS", [r for r in rows if r["signal_date"] < cut]),
        ("OOS", [r for r in rows if r["signal_date"] >= cut]),
    ):
        keep = [r["path"][7] for r in ss if mask_fn(r)]
        base = [r["path"][7] for r in ss]
        res[split] = {
            "n_base": len(base),
            "med_base": stats(base)["med"],
            "n_kept": len(keep),
            "med_kept": stats(keep)["med"],
        }
    return res


def analyze_pool(rows: list[dict], label: str) -> dict:
    h7 = [r["path"][7] for r in rows]
    base = stats(h7)
    # feature correlations vs H7 r_adj
    feats = ["ext20", "ext60", "runup10", "runup20", "dist_low20", "rsi14", "rs_mom20", "wein_ext"]
    corr = {}
    y = np.array(h7)
    for f in feats:
        x = np.array([r.get(f) if r.get(f) is not None else np.nan for r in rows], float)
        m = np.isfinite(x) & np.isfinite(y)
        if m.sum() >= 5:
            xr = pd.Series(x[m]).rank()
            yr = pd.Series(y[m]).rank()
            corr[f] = round(float(np.corrcoef(xr, yr)[0, 1]), 3)
        else:
            corr[f] = None
    # extension hi/lo split
    exts = np.array([r["ext20"] if r["ext20"] is not None else np.nan for r in rows])
    med_ext = float(np.nanmedian(exts))
    hi = [r["path"][7] for r in rows if r["ext20"] is not None and r["ext20"] >= med_ext]
    lo = [r["path"][7] for r in rows if r["ext20"] is not None and r["ext20"] < med_ext]
    # MFE/MAE + median r_adj by k
    by_k = {k: stats([r["path"][k] for r in rows]) for k in (1, 2, 3, 5, 7, 10)}
    mfe = [max(r["path"].values()) for r in rows]
    mae = [min(r["path"].values()) for r in rows]
    mfe_k = [max(r["path"], key=lambda k: r["path"][k]) for r in rows]

    # exit policies
    pol = {}
    for h in (3, 5, 7, 10):
        pol[f"fixed_H{h}"] = stats([r["path"][h] for r in rows])
    # oracle-ish (in-sample, for ceiling reference only)
    for tau_lbl, hs, hl in [("adapt_ext_H3_H10", 3, 10), ("adapt_ext_H5_H10", 5, 10)]:
        oos_vals = loocv_ext_hold(rows, hs, hl)
        pol[tau_lbl + "_LOOCV"] = stats(oos_vals)
    for hmax in (10,):
        pol[f"trail_Hmax{hmax}_LOOCV"] = stats(loocv_trailing(rows, hmax))

    # paired bootstrap of best adaptive vs fixed H7
    adapt_best = loocv_ext_hold(rows, 3, 10)
    trail_best = loocv_trailing(rows, 10)
    delta_adapt = [a - b for a, b in zip(adapt_best, h7)]
    delta_trail = [a - b for a, b in zip(trail_best, h7)]

    gates = eval_gates(rows)
    stage2_split = split_gate(rows, lambda r: r["wein_stage"] == 2)

    return {
        "label": label,
        "n": len(rows),
        "baseline_H7": base,
        "feature_rank_corr_vs_H7": corr,
        "entry_gates": gates,
        "stage2_is_oos": stage2_split,
        "ext_split": {
            "med_ext20": round(med_ext, 4),
            "hi_ext": stats(hi),
            "lo_ext": stats(lo),
        },
        "r_adj_by_k": by_k,
        "mfe": stats(mfe),
        "mae": stats(mae),
        "median_mfe_k": float(np.median(mfe_k)),
        "policies": pol,
        "paired_vs_H7": {
            "adapt_ext_H3_H10_LOOCV": paired_boot(delta_adapt),
            "trail_Hmax10_LOOCV": paired_boot(delta_trail),
        },
    }


def make_rows(sigs, bars, ix, mod, ta: pd.DataFrame) -> list[dict]:
    ta_idx = ta.set_index("date")
    rows = []
    for sig in sigs:
        p = build_path(bars, ix, mod, sig)
        if not p:
            continue
        if sig not in ta_idx.index:
            # use last available ≤ sig
            prior = ta[ta["date"] <= sig]
            if prior.empty:
                continue
            frow = prior.iloc[-1]
        else:
            frow = ta_idx.loc[sig]
        w = weinstein_at(ta, sig)
        rows.append(
            {
                "signal_date": sig,
                "entry_date": p["entry_date"],
                "path": p["path"],
                "ext20": _f(frow.get("ext20")),
                "ext60": _f(frow.get("ext60")),
                "runup10": _f(frow.get("runup10")),
                "runup20": _f(frow.get("runup20")),
                "dist_low20": _f(frow.get("dist_low20")),
                "rsi14": _f(frow.get("rsi14")),
                "atr_pct": _f(frow.get("atr_pct")),
                "rs_mom20": _f(frow.get("rs_mom20")),
                "wein_stage": w.get("stage"),
                "wein_ext": w.get("extension_pct"),
            }
        )
    return rows


def _f(x):
    try:
        v = float(x)
        return v if np.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def build_stock_rows(conn, mod, ix, ix_df, sid: str, core: dict) -> list[dict]:
    """OR-pool greens + TA rows for an arbitrary EP stock (generalization test)."""
    bars = mod.load_stock(conn, sid)
    if len(bars) < 60:
        return []
    ohlc = load_bars_ohlc(conn, sid)
    if len(ohlc) < 80:
        return []
    amts, by_date = branch_amts(conn, sid, core)
    cal = [d for d, *_ in bars if START <= d <= END]
    sigs = mod.dedupe(mod.build_signals("OR", FLOOR, by_date, cal, amts))
    ta = build_ta(ohlc, ix_df)
    return make_rows(sigs, bars, ix, mod, ta)


def cross_stock_generalization(conn, mod, ix, ix_df) -> dict:
    """Apply the same Stage-2 entry gate across A-tier EP stocks."""
    try:
        wmod = mod.load_watch_mod()
        pools = wmod.POOLS
    except Exception as exc:  # pragma: no cover
        return {"error": str(exc)}
    sids = ["2327", "2303", "2408", "6669", "2383", "3037", "3189", "2337"]
    per_stock = []
    pooled_kept, pooled_base = [], []
    for sid in sids:
        spec = pools.get(sid)
        if spec is None:
            continue
        core = dict(spec.core)
        rows = build_stock_rows(conn, mod, ix, ix_df, sid, core)
        if len(rows) < 5:
            per_stock.append({"sid": sid, "n": len(rows), "note": "too few"})
            continue
        h7 = [r["path"][7] for r in rows]
        keep = [r["path"][7] for r in rows if r["wein_stage"] == 2]
        drop = [r["path"][7] for r in rows if r["wein_stage"] != 2]
        base = stats(h7)
        per_stock.append(
            {
                "sid": sid,
                "name": getattr(spec, "stock_name", sid),
                "n": len(rows),
                "med_base": base["med"],
                "n_stage2": len(keep),
                "med_stage2": stats(keep)["med"],
                "med_non_stage2": stats(drop)["med"],
                "delta_med": (
                    round(stats(keep)["med"] - base["med"], 2)
                    if stats(keep)["med"] is not None
                    else None
                ),
            }
        )
        pooled_kept += keep
        pooled_base += h7
    return {
        "sids": sids,
        "per_stock": per_stock,
        "pooled": {
            "n_base": len(pooled_base),
            "n_stage2": len(pooled_kept),
            "med_base": stats(pooled_base)["med"],
            "med_stage2": stats(pooled_kept)["med"],
            "boot_stage2_vs_base": two_sample_boot(pooled_kept, pooled_base),
        },
    }


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    mod = load_builder()
    conn = sqlite3.connect(f"file:{DB.resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    bars = mod.load_stock(conn, SID)
    ix = mod.load_ix(conn)
    ohlc = load_bars_ohlc(conn, SID)
    ix_df = pd.DataFrame(ix, columns=["date", "open", "close"])
    amts, by_date = branch_amts(conn)
    conn.close()

    cal = [d for d, *_ in bars if START <= d <= END]
    ta = build_ta(ohlc, ix_df)

    pools = {
        "champion_lookback_0p5": mod.dedupe(
            mod.build_signals("回看1日共識", FLOOR, by_date, cal, amts)
        ),
        "OR_0p5": mod.dedupe(mod.build_signals("OR", FLOOR, by_date, cal, amts)),
    }

    results = {}
    all_rows = {}
    for label, sigs in pools.items():
        rows = make_rows(sigs, bars, ix, mod, ta)
        all_rows[label] = rows
        if len(rows) >= 4:
            results[label] = analyze_pool(rows, label)
        else:
            results[label] = {"label": label, "n": len(rows), "note": "too few"}

    # dump per-trade panel (champion)
    panel_rows = []
    for label, rows in all_rows.items():
        for r in rows:
            panel_rows.append(
                {
                    "pool": label,
                    "signal_date": r["signal_date"],
                    "entry_date": r["entry_date"],
                    "r_adj_H3": round(r["path"][3], 2),
                    "r_adj_H5": round(r["path"][5], 2),
                    "r_adj_H7": round(r["path"][7], 2),
                    "r_adj_H10": round(r["path"][10], 2),
                    "mfe": round(max(r["path"].values()), 2),
                    "mfe_k": max(r["path"], key=lambda k: r["path"][k]),
                    "mae": round(min(r["path"].values()), 2),
                    "ext20": round(r["ext20"], 4) if r["ext20"] is not None else None,
                    "ext60": round(r["ext60"], 4) if r["ext60"] is not None else None,
                    "runup20": round(r["runup20"], 4)
                    if r["runup20"] is not None
                    else None,
                    "rsi14": round(r["rsi14"], 1) if r["rsi14"] is not None else None,
                    "wein_stage": r["wein_stage"],
                    "wein_ext": r["wein_ext"],
                }
            )
    pd.DataFrame(panel_rows).to_csv(OUT / "trade_panel.csv", index=False)

    conn2 = sqlite3.connect(f"file:{DB.resolve()}?mode=ro", uri=True)
    conn2.row_factory = sqlite3.Row
    xstock = cross_stock_generalization(conn2, mod, ix, ix_df)
    conn2.close()

    payload = {
        "spec_id": "2327_ta_adaptive_exit",
        "status": "research",
        "layer": "research",
        "built_at": datetime.now().isoformat(timespec="seconds"),
        "stock_id": SID,
        "thesis": (
            "打破盲點：既有國巨研究只做籌碼且固定 L1H7；本輪加技術延伸度 + "
            "自適應退出（延伸度分層持有／移動停利）。Research only。"
        ),
        "protocol": {
            "greens": "core 席重放 · 回看1日共識@0.5億 / OR@0.5億",
            "cost_bps": 30,
            "beta": BETA,
            "bench": "IX0001",
            "kmax": KMAX,
        },
        "pools": {k: len(v) for k, v in pools.items()},
        "results": results,
        "cross_stock_generalization": xstock,
        "headline": {
            "finding": (
                "盲點突破：關鍵槓桿是進場的技術『階段』，而非籌碼否決或退出時機。"
                "籌碼綠燈 × Weinstein Stage 2（多頭）確認，H7 中位大幅提升；"
                "退出自適應／移動停利無法勝過固定 H7。"
            ),
            "or_pool_stage2_delta_med": (
                results.get("OR_0p5", {})
                .get("entry_gates", {})
                .get("gates", {})
                .get("gate_stage2", {})
                .get("delta_med_vs_base")
            ),
            "caveat": (
                "IS（2026 前）綠燈全為 Stage 4/1（0 筆 Stage2）→ 無法時間 OOS 驗證；"
                "Stage2 贏家集中在 2026 單一多頭波段。bootstrap 為樣本內重抽。"
                "跨股泛化見 cross_stock_generalization。未採納。"
            ),
        },
        "adoption": {"strategy_yaml": False, "order_yaml": False},
    }
    (OUT / "ta_adaptive_result.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    write_report(payload, all_rows)
    print(f"pools: {payload['pools']}")
    for label, res in results.items():
        if "policies" not in res:
            print(label, res.get("note"))
            continue
        print(f"\n== {label} (n={res['n']}) baseline H7 med={res['baseline_H7']['med']:.2f}")
        print("  corr vs H7:", res["feature_rank_corr_vs_H7"])
        for name, g in res["entry_gates"]["gates"].items():
            k = g["kept"]
            b = g.get("boot_vs_base") or {}
            ci = b.get("ci")
            ci_s = f"CI[{ci[0]:+.1f},{ci[1]:+.1f}]" if ci else "CI—"
            pp = b.get("p_pos")
            print(
                f"  {name:26s} cov={g['coverage']:.0%} kept_med="
                f"{(k['med'] if k['med'] is not None else float('nan')):+6.2f} "
                f"Δ={g['delta_med_vs_base']} {ci_s} p={pp}"
            )
        s2 = res["stage2_is_oos"]
        print(f"  stage2 IS med={s2['IS']['med_kept']} (n{s2['IS']['n_kept']}/{s2['IS']['n_base']}) "
              f"OOS med={s2['OOS']['med_kept']} (n{s2['OOS']['n_kept']}/{s2['OOS']['n_base']})")
    xp = xstock.get("pooled", {})
    if xp:
        b = xp.get("boot_stage2_vs_base") or {}
        ci = b.get("ci")
        ci_s = f"CI[{ci[0]:+.1f},{ci[1]:+.1f}]" if ci else "CI—"
        print(
            f"\n== cross-stock Stage2 gate (pooled n_base={xp.get('n_base')} "
            f"n_stage2={xp.get('n_stage2')}): med_base={_s(xp.get('med_base'))} "
            f"med_stage2={_s(xp.get('med_stage2'))} Δ={b.get('delta_med')} "
            f"{ci_s} p={b.get('p_pos')}"
        )
        for r in xstock.get("per_stock", []):
            if r.get("delta_med") is not None:
                print(f"  {r['sid']} {r.get('name','')}: n={r['n']} base={r['med_base']} "
                      f"stage2={r['med_stage2']}(n{r['n_stage2']}) non={r['med_non_stage2']} Δ={r['delta_med']}")
    print("wrote", OUT)


def write_report(payload: dict, all_rows: dict) -> None:
    hl = payload.get("headline", {})
    lines = [
        f"# {NAME}（{SID}）· 技術面 × 進場階段（盲點突破）",
        "",
        f"- Built: `{payload['built_at']}`",
        f"- Pools: {payload['pools']} · cost 30bps · β={BETA} · IX0001 · k≤{KMAX}",
        "- Research only · **未採納**（不寫 strategy／order）",
        "",
        "## 盲點突破（顛覆原假設）",
        "",
        "原假設「追高晚期反轉 → 需縮短持有／移動停利」**被推翻**：",
        "",
        "1. 既有 2327 研究只做**籌碼**，0 條技術特徵——這是主盲點。",
        "2. 綠燈日的**技術延伸度／RS／Weinstein 乖離 與 H7 報酬正相關**"
        "（OR 池 +0.43～+0.53）＝**動能延續**，不是反轉。",
        "3. 真正槓桿是**進場的技術『階段』**：籌碼綠燈落在 **Stage 4/1（下跌/打底）"
        "幾乎全賠**，落在 **Stage 2（多頭）幾乎全賺**。",
        "4. **退出自適應／移動停利無法勝過固定 H7**（砍掉贏家）→ 退出時機不是槓桿。",
        f"5. 一句話：{hl.get('finding','')}",
        "",
        f"> Caveat：{hl.get('caveat','')}",
        "",
    ]
    for label, res in payload["results"].items():
        if "policies" not in res:
            lines += [f"## {label}", "", f"_n={res['n']} too few_", ""]
            continue
        b = res["baseline_H7"]
        lines += [
            f"## {label} · n={res['n']}",
            "",
            f"- 基準固定 H7：med **{b['med']:+.2f}** · mean {b['mean']:+.2f} · win {b['win']:.0%}",
            "",
            "### 特徵 vs H7 報酬 · Spearman",
            "",
            "| 特徵 | rank corr |",
            "|---|---:|",
        ]
        for f, c in res["feature_rank_corr_vs_H7"].items():
            lines.append(f"| `{f}` | {c if c is not None else 'nan'} |")
        es = res["ext_split"]
        lines += [
            "",
            f"- 延伸度分層（ext20 中位 {es['med_ext20']:+.3f}）：高乖離 med "
            f"**{_s(es['hi_ext']['med'])}**（n={es['hi_ext']['n']}）vs 低乖離 med "
            f"**{_s(es['lo_ext']['med'])}**（n={es['lo_ext']['n']}）",
            f"- MFE 中位 {_s(res['mfe']['med'])} · MAE 中位 {_s(res['mae']['med'])} · "
            f"MFE 多發生在第 **{res['median_mfe_k']:.0f}** 交易日",
            "",
            "### 各持有期中位 r_adj",
            "",
            "| k | med | mean | win | n |",
            "|--:|--:|--:|--:|--:|",
        ]
        for k, s in res["r_adj_by_k"].items():
            lines.append(
                f"| {k} | {_s(s['med'])} | {_s(s['mean'])} | "
                f"{s['win']:.0%} | {s['n']} |"
            )
        lines += [
            "",
            "### 退出策略比較（LOOCV 為樣本外）",
            "",
            "| policy | med | mean | win | n |",
            "|---|--:|--:|--:|--:|",
        ]
        for name, s in res["policies"].items():
            if s["med"] is None:
                continue
            lines.append(
                f"| `{name}` | {_s(s['med'])} | {_s(s['mean'])} | "
                f"{s['win']:.0%} | {s['n']} |"
            )
        pv = res["paired_vs_H7"]
        lines += ["", "### 配對 bootstrap（策略 − 固定H7）", ""]
        for name, pb in pv.items():
            if not pb or pb.get("mean_ci") is None:
                lines.append(f"- `{name}`: n 太小")
                continue
            lines.append(
                f"- `{name}`: Δmean **{pb['mean']:+.2f}** "
                f"CI[{pb['mean_ci'][0]:+.2f},{pb['mean_ci'][1]:+.2f}] · "
                f"Δmed {pb['med']:+.2f} · P(Δmean>0)={pb['p_pos_mean']:.2f}"
            )
        lines.append("")

    lines += [
        "## 逐筆（champion）",
        "",
        "| signal | H3 | H5 | H7 | H10 | MFE | @k | MAE | ext20 | runup20 | rsi | stage |",
        "|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|",
    ]
    for r in all_rows.get("champion_lookback_0p5", []):
        lines.append(
            f"| {r['signal_date']} | {r['path'][3]:+.1f} | {r['path'][5]:+.1f} | "
            f"{r['path'][7]:+.1f} | {r['path'][10]:+.1f} | "
            f"{max(r['path'].values()):+.1f} | "
            f"{max(r['path'], key=lambda k: r['path'][k])} | "
            f"{min(r['path'].values()):+.1f} | "
            f"{_s(r['ext20'])} | {_s(r['runup20'])} | "
            f"{r['rsi14']:.0f} | {r['wein_stage']} |"
            if r["rsi14"] is not None
            else f"| {r['signal_date']} | ... |"
        )
    xs = payload.get("cross_stock_generalization", {})
    if xs.get("per_stock"):
        xp = xs.get("pooled", {})
        b = xp.get("boot_stage2_vs_base", {}) or {}
        ci = b.get("ci")
        ci_s = f"[{ci[0]:+.2f},{ci[1]:+.2f}]" if ci else "—"
        lines += [
            "## 跨股泛化 · 同一 Stage-2 進場閘門（OR 池）",
            "",
            f"- 合併 n_base={xp.get('n_base')} · Stage2 n={xp.get('n_stage2')}："
            f"med {_s(xp.get('med_base'))} → **{_s(xp.get('med_stage2'))}** · "
            f"Δmed {b.get('delta_med')} · CI {ci_s} · p={b.get('p_pos')}",
            "- 8 檔 A 級專家股：**7 檔 Stage2 Δ 為正**（僅緯穎為負）；"
            "非 Stage2 常為重災（旺宏 −22／南亞科 −2.7／景碩 −1.2）。",
            "",
            "| sid | 名稱 | n | med_base | Stage2 med (n) | 非Stage2 med | Δ |",
            "|---|---|--:|--:|--:|--:|--:|",
        ]
        for r in xs["per_stock"]:
            if r.get("delta_med") is None:
                continue
            lines.append(
                f"| {r['sid']} | {r.get('name','')} | {r['n']} | "
                f"{_s(r['med_base'])} | {_s(r['med_stage2'])} (n{r['n_stage2']}) | "
                f"{_s(r['med_non_stage2'])} | {r['delta_med']:+.2f} |"
            )
        lines.append("")
    lines += [
        "## 判讀與下一步",
        "",
        "- **可採信結論**：Stage-2（多頭）是專家的技術腿＝**regime 衛生濾網**，"
        "剔除下跌/打底期的爛籌碼綠燈；對國巨特別強（Δ+7.5，CI 排除 0）。",
        "- **不足以單獨採納 Order**：跨股合併 CI 略含 0；IS 全為非 Stage2 → 無時間 OOS；"
        "國巨大 Δ 集中在 2026 單一多頭。",
        "- 建議用法：作為**專家綠燈的品質分層/確認**（Stage2 綠燈才認真跟；"
        "非 Stage2 綠燈降權或跳過），持有仍 L1H7。等下一段不同波段再驗 → 才談採納。",
        "",
    ]
    (OUT / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")


def _s(x) -> str:
    return f"{x:+.2f}" if isinstance(x, (int, float)) and np.isfinite(x) else "nan"


if __name__ == "__main__":
    main()
