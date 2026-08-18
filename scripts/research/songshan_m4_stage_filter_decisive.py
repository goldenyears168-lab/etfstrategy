#!/usr/bin/env python3
"""M4-Step3：補齊真實價格歷史後，決定性重測技術面階段濾網（H-D2）＋去重規則.

Step2 證明：本地 DB 對 13 檔事件標的的價格歷史是 2024-06-03 / 2025-06-09 / 2026-04-20
三批 backfill 的殘缺結果（6449 鈺邦只有 39 根 K 線），所以
`vectorized_minervini_criteria_count` 與 `stage_series_daily` 對它們機械性回傳
crit≈0 / stage=0。原始 F1（Minervini 7/7）與 F3（Weinstein 階段）的強分層有一大半
是「DB 有沒有這檔的歷史」的代理變數，不是技術面資訊。

本腳本從 FinMind API **唯讀取數到記憶體**（不寫 DB、不建表），補齊這些標的的真實
日線，重算訊號日 T 的 PIT 技術面階段，再做決定性檢定：
  F1''  Minervini 7 條全過（真實歷史）
  F3''  Weinstein 日更階段 == 2（真實歷史；stage 0=資料不足另計）
  F4''  延伸度 extension_pct（離 30W MA）
並重跑產業/相關族群去重規則與組合濾網。

DB 唯讀。FinMind 只讀不寫。用法：
  PYTHONPATH=src .venv/bin/python scripts/research/songshan_m4_stage_filter_decisive.py
"""

from __future__ import annotations

import importlib.util
import json
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np
import pandas as pd

from stock_db import DEFAULT_DB_PATH
from stock_db.connection import connect_ro
from stage_analysis import stage_series_daily, vectorized_minervini_criteria_count
from research.branch_signal_validation import build_l1h7_signal_dict, permutation_test
from project_dotenv import load_project_dotenv

OUT_DIR = ROOT / "reports" / "research" / "branch-footprint-screen"
SCRIPTS = ROOT / "scripts" / "research"
FEAT = OUT_DIR / "songshan_m4_event_features_with_coverage.csv"
CACHE = OUT_DIR / "songshan_m4_finmind_price_cache.parquet"
SOURCE = "finmind"
FETCH_START = "2021-06-01"
STUDY_END = "2026-08-17"
N_PERM = 20_000
SEED = 20260817

ELEC_BUCKET = {
    "半導體業", "電子零組件業", "電子工業", "光電業", "電腦及週邊設備業",
    "通信網路業", "其他電子業", "電子通路業", "資訊服務業",
}


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MGEN = _load_module("mgen", SCRIPTS / "study_whale_branch_5d_net95_live_signal_validation.py")


def section(t: str) -> None:
    print(f"\n{'=' * 96}\n{t}\n{'=' * 96}")


def layer_stats(v: np.ndarray) -> dict:
    if len(v) == 0:
        return {"n": 0}
    return {"n": int(len(v)), "mean_pct": round(float(np.mean(v)), 3),
            "median_pct": round(float(np.median(v)), 3),
            "win_rate_pct": round(float((v > 0).mean() * 100), 1)}


def perm_diff(vals: np.ndarray, mask: np.ndarray, n_perm: int = N_PERM) -> dict:
    hi, lo = vals[mask], vals[~mask]
    if len(hi) < 2 or len(lo) < 2:
        return {"note": "layer too small"}
    om, omd = float(np.mean(hi) - np.mean(lo)), float(np.median(hi) - np.median(lo))
    rng = np.random.default_rng(SEED)
    cm = cmd = 0
    for _ in range(n_perm):
        idx = rng.permutation(len(vals))
        a, b = vals[idx[:len(hi)]], vals[idx[len(hi):]]
        cm += abs(np.mean(a) - np.mean(b)) >= abs(om) - 1e-12
        cmd += abs(np.median(a) - np.median(b)) >= abs(omd) - 1e-12
    return {"diff_mean_pp": round(om, 3), "diff_median_pp": round(omd, 3),
            "p_mean_two_sided": round((cm + 1) / (n_perm + 1), 5),
            "p_median_two_sided": round((cmd + 1) / (n_perm + 1), 5)}


def bh_fdr(pairs: list[tuple[str, float]]) -> dict[str, float]:
    items = sorted([(k, p) for k, p in pairs if p is not None], key=lambda x: x[1])
    n = len(items)
    out: dict[str, float] = {}
    run = float("inf")
    for k, q in reversed([(k, p * n / (i + 1)) for i, (k, p) in enumerate(items)]):
        run = min(run, q)
        out[k] = round(min(run, 1.0), 5)
    return out


def random_prune_pvalue(vals: np.ndarray, n_keep: int, obs_mean: float,
                        n_perm: int = N_PERM) -> float:
    rng = np.random.default_rng(SEED)
    cnt = 0
    for _ in range(n_perm):
        s = rng.choice(len(vals), size=n_keep, replace=False)
        cnt += np.mean(vals[s]) >= obs_mean - 1e-12
    return round((cnt + 1) / (n_perm + 1), 5)


# ---------------------------------------------------------------------------
def fetch_prices(stock_ids: list[str]) -> pd.DataFrame:
    """FinMind TaiwanStockPrice → 記憶體 DataFrame（唯讀 API，不寫 DB）。"""
    if CACHE.exists():
        df = pd.read_parquet(CACHE)
        have = set(df["stock_id"].unique())
        missing = [s for s in stock_ids if s not in have]
        if not missing:
            print(f"[CACHE] 命中 {CACHE.name}（{df['stock_id'].nunique()} 檔）")
            return df
    else:
        df = pd.DataFrame()
        missing = list(stock_ids)

    load_project_dotenv()
    from finmind_client import fetch_finmind

    frames = [df] if len(df) else []
    for i, sid in enumerate(missing, 1):
        try:
            rows = fetch_finmind("TaiwanStockPrice", sid,
                                 date.fromisoformat(FETCH_START), date.fromisoformat(STUDY_END))
        except Exception as exc:  # noqa: BLE001
            print(f"  [{i}/{len(missing)}] {sid} FAILED: {exc}")
            continue
        sub = pd.DataFrame(rows)
        if sub.empty:
            print(f"  [{i}/{len(missing)}] {sid} empty")
            continue
        sub = sub.rename(columns={"date": "trade_date", "max": "high", "min": "low"})
        sub["stock_id"] = sid
        frames.append(sub[["stock_id", "trade_date", "open", "high", "low", "close"]])
        print(f"  [{i}/{len(missing)}] {sid} n={len(sub)} {sub['trade_date'].min()}~{sub['trade_date'].max()}")
        time.sleep(0.6)
    out = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if len(out):
        out = out[out["close"] > 0].drop_duplicates(["stock_id", "trade_date"])
        out.to_parquet(CACHE, index=False)
    return out


def main() -> int:
    conn = connect_ro(DEFAULT_DB_PATH)
    df = pd.read_csv(FEAT, dtype={"stock_id": str})
    stocks = sorted(df["stock_id"].unique())

    section("(1) 補齊真實價格歷史（FinMind，記憶體內；不寫 DB）")
    px = fetch_prices(stocks)
    print(f"[INFO] 補齊後 panel：{px['stock_id'].nunique()} 檔 / {len(px)} 列")
    cov = px.groupby("stock_id")["trade_date"].agg(["min", "max", "count"])
    short = cov[cov["count"] < 400]
    if len(short):
        print("[WARN] FinMind 也拿不到長歷史的標的：")
        print(short.to_string())

    close = px.pivot_table(index="trade_date", columns="stock_id", values="close", aggfunc="last").sort_index()
    crit = vectorized_minervini_criteria_count(close)
    ma200 = close.rolling(200, min_periods=200).mean()
    crit = crit.where(close.notna() & ma200.notna())  # 歷史不足 → NaN，不再壓成 0

    stage_map: dict[str, pd.DataFrame] = {}
    for sid in stocks:
        sub = px[px["stock_id"] == sid][["trade_date", "open", "high", "low", "close"]]
        sub = sub.rename(columns={"trade_date": "date"}).sort_values("date")
        s = stage_series_daily(sub)
        s["date"] = pd.to_datetime(s["date"]).dt.strftime("%Y-%m-%d")
        stage_map[sid] = s.set_index("date")

    rows = []
    for r in df.itertuples(index=False):
        sid, d = r.stock_id, r.signal_date
        s = stage_map.get(sid)
        srow = s.loc[d] if (s is not None and d in s.index) else None
        rows.append({
            "stock_id": sid, "signal_date": d,
            "crit_true": float(crit.loc[d, sid]) if (d in crit.index and sid in crit.columns
                                                     and pd.notna(crit.loc[d, sid])) else np.nan,
            "wstage_true": float(srow["stage"]) if srow is not None else np.nan,
            "ext_true": float(srow["extension_pct"]) if srow is not None else np.nan,
            "slope_true": float(srow["ma_slope_pct"]) if srow is not None else np.nan,
            "bars_true": int((px[(px["stock_id"] == sid) & (px["trade_date"] <= d)]).shape[0]),
        })
    tf = pd.DataFrame(rows)
    df = df.merge(tf, on=["stock_id", "signal_date"], how="left")
    df.to_csv(OUT_DIR / "songshan_m4_event_features_true_history.csv", index=False)

    print("\n[補齊前 vs 補齊後：原本歷史不足的 14 筆]")
    print(df[~df["hist_ok"]][["signal_date", "stock_id", "stock_name", "bars_before",
                              "bars_true", "minervini_crit", "crit_true", "wstage",
                              "wstage_true", "ext_true", "r_adj_pct"]].to_string(index=False))
    n_still_na = int(df["crit_true"].isna().sum())
    print(f"\n[INFO] 補齊後仍無法計算 Minervini 的事件數 = {n_still_na}")

    vals = df["r_adj_pct"].to_numpy()
    out: dict = {"n_base": int(len(df)),
                 "base": layer_stats(vals),
                 "n_still_missing_history": n_still_na}

    section("(2) H-D2 決定性重測（真實歷史）")
    tests = []
    ok = df["crit_true"].notna()
    sub = df[ok].reset_index(drop=True)
    sv = sub["r_adj_pct"].to_numpy()

    m = (sub["crit_true"] >= 7).to_numpy()
    tests.append(("F1'' Minervini 7/7（真實歷史）", {
        "n_evaluable": int(len(sub)), "layer_pass": layer_stats(sv[m]),
        "layer_fail": layer_stats(sv[~m]), **perm_diff(sv, m)}))

    m = (sub["crit_true"] >= 6).to_numpy()
    tests.append(("F1b Minervini >=6/7", {
        "n_evaluable": int(len(sub)), "layer_pass": layer_stats(sv[m]),
        "layer_fail": layer_stats(sv[~m]), **perm_diff(sv, m)}))

    okw = df["wstage_true"].notna() & (df["wstage_true"] > 0)
    subw = df[okw].reset_index(drop=True)
    swv = subw["r_adj_pct"].to_numpy()
    mw = (subw["wstage_true"] == 2).to_numpy()
    tests.append(("F3'' Weinstein 階段==2（真實歷史 · 排除 stage0）", {
        "n_evaluable": int(len(subw)), "layer_stage2": layer_stats(swv[mw]),
        "layer_other": layer_stats(swv[~mw]), **perm_diff(swv, mw)}))

    oke = df["ext_true"].notna()
    sube = df[oke].reset_index(drop=True)
    sev = sube["r_adj_pct"].to_numpy()
    me = (sube["ext_true"] > sube["ext_true"].median()).to_numpy()
    tests.append(("F4'' 延伸度（離 30W MA · 中位數分層）", {
        "n_evaluable": int(len(sube)), "threshold": round(float(sube["ext_true"].median()), 2),
        "layer_ext_hi": layer_stats(sev[me]), "layer_ext_lo": layer_stats(sev[~me]),
        **perm_diff(sev, me)}))

    mp = (df["close_px"] <= df["close_px"].median()).to_numpy()
    tests.append(("F6 訊號日股價（中位數分層）", {
        "n_evaluable": int(len(df)), "threshold": round(float(df["close_px"].median()), 1),
        "layer_px_lo": layer_stats(vals[mp]), "layer_px_hi": layer_stats(vals[~mp]),
        **perm_diff(vals, mp)}))

    ma = df["adv20_amt"].notna()
    suba = df[ma].reset_index(drop=True)
    sav = suba["r_adj_pct"].to_numpy()
    maa = (suba["adv20_amt"] > suba["adv20_amt"].median()).to_numpy()
    tests.append(("F5 20 日均額 ADV（中位數分層）", {
        "n_evaluable": int(len(suba)), "layer_adv_hi": layer_stats(sav[maa]),
        "layer_adv_lo": layer_stats(sav[~maa]), **perm_diff(sav, maa)}))

    q = bh_fdr([(k, v.get("p_mean_two_sided")) for k, v in tests])
    qmd = bh_fdr([(k, v.get("p_median_two_sided")) for k, v in tests])
    for k, v in tests:
        v["q_bh_mean"] = q.get(k)
        v["q_bh_median"] = qmd.get(k)
        print(f"\n[{k}]")
        print(json.dumps(v, ensure_ascii=False))
    out["hd2_decisive"] = {k: v for k, v in tests}

    section("(3) random-timing permutation（真實歷史子群）")
    ix_dict = build_l1h7_signal_dict(MGEN.load_ix(conn))
    cache: dict[str, dict] = {}

    def dicts_for(frame):
        d = {}
        for sid in sorted(frame["stock_id"].unique()):
            if sid not in cache:
                cache[sid] = build_l1h7_signal_dict(MGEN.load_stock_bars(conn, sid))
            d[sid] = cache[sid]
        return d

    subsets = {
        "base_all": df,
        "crit_true>=7": df[df["crit_true"] >= 7],
        "crit_true>=6": df[df["crit_true"] >= 6],
        "wstage_true==2": df[df["wstage_true"] == 2],
        "px<=median": df[df["close_px"] <= df["close_px"].median()],
        "crit>=7 & px<=median": df[(df["crit_true"] >= 7) & (df["close_px"] <= df["close_px"].median())],
    }
    perm_out = {}
    for name, frame in subsets.items():
        if len(frame) < 5:
            perm_out[name] = {"n": int(len(frame)), "note": "too small"}
            continue
        res = permutation_test(frame[["stock_id", "signal_date"]], dicts_for(frame),
                               ix_dict, n_perm=N_PERM, seed=SEED)
        perm_out[name] = {"n": res["n_events"],
                          "obs_mean_pct": round(res["observed_mean_pct"], 3),
                          "obs_median_pct": round(res["observed_median_pct"], 3),
                          "p_mean_onesided": res["p_value_mean_onesided"],
                          "p_median_onesided": res["p_value_median_onesided"]}
        print(name, json.dumps(perm_out[name], ensure_ascii=False))
    out["random_timing_permutation"] = perm_out

    section("(4) 濾網對 6449／2337／6271／2492 的處置")
    focus = df[df["stock_id"].isin(["6449", "2337", "6271", "2492"])][
        ["signal_date", "stock_id", "stock_name", "r_adj_pct", "close_px", "bars_true",
         "crit_true", "wstage_true", "ext_true"]]
    focus = focus.assign(
        pass_F1=lambda d: d["crit_true"] >= 7,
        pass_F3=lambda d: d["wstage_true"] == 2,
        pass_px150=lambda d: d["close_px"] <= 150,
        pass_px200=lambda d: d["close_px"] <= 200,
    )
    print(focus.to_string(index=False))
    out["focus_cases"] = json.loads(focus.to_json(orient="records"))

    section("(5) 組合濾網（真實歷史）")
    combos = {
        "baseline(mega only)": pd.Series(True, index=df.index),
        "F1 crit>=7": df["crit_true"] >= 7,
        "F1b crit>=6": df["crit_true"] >= 6,
        "F3 wstage==2": df["wstage_true"] == 2,
        "F6 px<=150": df["close_px"] <= 150,
        "F6 px<=200": df["close_px"] <= 200,
        "F1 & px<=200": (df["crit_true"] >= 7) & (df["close_px"] <= 200),
        "F1 & px<=150": (df["crit_true"] >= 7) & (df["close_px"] <= 150),
        "F1 & F3": (df["crit_true"] >= 7) & (df["wstage_true"] == 2),
        "F1 & ext<=median": (df["crit_true"] >= 7) & (df["ext_true"] <= df["ext_true"].median()),
    }
    rows = []
    for name, m in combos.items():
        m = m.fillna(False)
        v = df.loc[m, "r_adj_pct"].to_numpy()
        rec = {"filter": name, **layer_stats(v),
               "kept_pct": round(float(m.mean()) * 100, 1),
               "n_6449_kept": int(((df["stock_id"] == "6449") & m).sum()),
               "worst4_kept": int((df["stock_id"].isin(["6449", "2337", "6271", "2492"])
                                   & m & (df["r_adj_pct"] < -15)).sum())}
        if 0 < len(v) < len(df):
            rec["p_vs_random_prune"] = random_prune_pvalue(vals, len(v), float(np.mean(v)))
        rows.append(rec)
    cdf = pd.DataFrame(rows)
    print(cdf.to_string(index=False))
    cdf.to_csv(OUT_DIR / "songshan_m4_combo_filters_true_history.csv", index=False)
    out["combo_filters_true_history"] = rows

    (OUT_DIR / "songshan_m4_stage_filter_decisive.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n[OK] → {OUT_DIR / 'songshan_m4_stage_filter_decisive.json'}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
