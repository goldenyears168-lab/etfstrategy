"""global_macro GAP-FILL — Fed 政策路徑 (NY Fed EFFR) + 在地 VIXTWN (TAIFEX) —— 補既有兩個待補錨.

WHAT THIS IS
  既有 global_macro_study.py 的 STRAND A/B 已把 SOX/ADR + VIX/DXY/US10Y/USDTWD 用真資料跑完,
  留兩個「待補」錨: (1) Fed 政策路徑 (FRED FEDFUNDS/FOMC),(2) 在地 VIXTWN (TAIFEX 台指選擇權隱波)。
  本腳本補這兩者,並回答任務核心問題: **在地 VIXTWN(同日、無美盤時差)是否優於美盤 ^VIX(lag+1 台股日)?**

DATA SOURCES (真資料,誠實記錄可得性)
  · Fed  : FRED CSV/API 在本環境 timeout;改用 **NY Fed markets API (權威原始源)** EFFR
           https://markets.newyorkfed.org/api/rates/unsecured/effr/search.json
           回傳每日 effective rate + **FOMC 目標區間 (targetRateFrom/To)** → 可精準抽政策變動事件。
           2018-01→2026-07 共 ~2150 日。美盤序列 → lag +1 TW 日防前視。
  · VIXTWN: TAIFEX 每月檔 https://www.taifex.com.tw/file/taifex/Dailydownload/vix/log2data/YYYYMMnew.txt
           **僅最近 ~3 個月免費** (更早在 edatashop 付費 NT$3000/半年)。本輪抓到 2026-05/06/07 ~60 日。
           全歷史 falsification 研究 → **待補 (paywall)**;本輪只做 3 個月 descriptive 同日 vs ^VIX 比較。

METHODOLOGY  (完全對齊既有 evaluate_anchor / chip-macro 方法論)
  IS/OOS 70/30 · 同曝險 permutation · Deflated-Sharpe · regime(>MA200) · ⟂(foreign+champion) · 對 champion 共線。
  Fed 為極低頻 regime 變數 → 日頻連續因子近乎 by-construction 無訊號;真正的檢定是 **降息 regime gate**。

RUN
  cd /Users/jackm4/Documents/ETF/股票研究 && .venv/bin/python scripts/research/dashboard/global_macro_fed_vixtwn_study.py
  （--refetch 重抓;預設讀已存 parquet）

Not investment advice. Research falsification only. 非投資建議,僅供研究證偽。
"""
from __future__ import annotations

import argparse
import io
from pathlib import Path

import numpy as np
import pandas as pd
import requests

# reuse the exact same evaluator/primitives as the main study (no re-implementation)
import sys
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
from global_macro_study import (  # noqa: E402
    ROOT, PANEL, OUT, rz, sharpe, lf, evaluate_anchor, orthogonalize,
)

HDR = {"User-Agent": "Mozilla/5.0"}
DATA = ROOT / "data" / "research" / "dashboard"
FED_PARQUET = DATA / "global_macro_fed_data.parquet"
VIXTWN_PARQUET = DATA / "global_macro_vixtwn_data.parquet"
RNG = np.random.default_rng(7)
ANN = np.sqrt(252)


# ----------------------------------------------------------------------------
# FETCH — Fed (NY Fed EFFR, authoritative) + VIXTWN (TAIFEX monthly, last ~3M free)
# ----------------------------------------------------------------------------
def fetch_effr(start="2018-01-01", end="2026-07-31") -> pd.DataFrame:
    """NY Fed Effective Federal Funds Rate + FOMC target range (daily)."""
    url = ("https://markets.newyorkfed.org/api/rates/unsecured/effr/search.json"
           f"?startDate={start}&endDate={end}")
    j = requests.get(url, headers=HDR, timeout=60).json()
    df = pd.DataFrame(j["refRates"])
    df = df.rename(columns={"effectiveDate": "date", "percentRate": "effr",
                            "targetRateTo": "tgt_hi", "targetRateFrom": "tgt_lo"})
    df["date"] = pd.to_datetime(df["date"])
    for c in ("effr", "tgt_hi", "tgt_lo"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df[["date", "effr", "tgt_hi", "tgt_lo"]].sort_values("date").reset_index(drop=True)


def fetch_vixtwn_recent() -> pd.DataFrame:
    """TAIFEX daily VIXTWN — only the last ~3 monthly files are served free."""
    months = pd.period_range("2026-05", "2026-07", freq="M")
    rows = []
    for m in months:
        ym = f"{m.year}{m.month:02d}"
        u = f"https://www.taifex.com.tw/file/taifex/Dailydownload/vix/log2data/{ym}new.txt"
        r = requests.get(u, headers=HDR, timeout=30)
        txt = r.content.decode("big5", "ignore")
        if txt.startswith("<HTML"):
            continue
        for ln in txt.splitlines()[2:]:
            parts = [p for p in ln.split("\t") if p.strip() != ""]
            if len(parts) >= 3 and parts[0].strip().isdigit():
                d, val = parts[0].strip(), parts[2].strip()
                try:
                    rows.append({"date": pd.to_datetime(d, format="%Y%m%d"),
                                 "vixtwn": float(val)})
                except ValueError:
                    pass
    return pd.DataFrame(rows).sort_values("date").reset_index(drop=True)


def load_or_fetch(refetch: bool):
    if refetch or not FED_PARQUET.exists():
        fed = fetch_effr()
        fed.to_parquet(FED_PARQUET, index=False)
    else:
        fed = pd.read_parquet(FED_PARQUET)
    if refetch or not VIXTWN_PARQUET.exists():
        vt = fetch_vixtwn_recent()
        vt.to_parquet(VIXTWN_PARQUET, index=False)
    else:
        vt = pd.read_parquet(VIXTWN_PARQUET)
    fed["date"] = pd.to_datetime(fed["date"]); vt["date"] = pd.to_datetime(vt["date"])
    return fed, vt


# ----------------------------------------------------------------------------
# STRAND C1 — Fed policy path → TAIEX
# ----------------------------------------------------------------------------
def run_fed(panel: pd.DataFrame, fed: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    m = panel.copy()
    # US series → TW date t uses last US session STRICTLY BEFORE t (no lookahead)
    m = pd.merge_asof(m.sort_values("date"), fed.sort_values("date"), on="date",
                      direction="backward", allow_exact_matches=False)
    m = m.reset_index(drop=True)
    m["fwd1"] = m["ix_close"].pct_change().shift(-1)
    m["fwd5"] = m["ix_close"].shift(-5) / m["ix_close"] - 1.0
    m["fwd20"] = m["ix_close"].shift(-20) / m["ix_close"] - 1.0
    m["champ"] = m["fut_foreign_oi"].diff(5)
    bull = (m["ix_close"] > m["ix_close"].rolling(200, min_periods=200).mean()).set_axis(m.index)
    controls = pd.DataFrame({"foreign": m["foreign"].values, "champ": m["champ"].values},
                            index=m.index)

    # a-priori FROZEN signals (direction +1 = easing/risk-on)
    # ~63 TW trading days ≈ 3 months
    easing_3m = (-(m["tgt_hi"] - m["tgt_hi"].shift(63)))     # cut → positive → long
    easing_6m = (-(m["tgt_hi"] - m["tgt_hi"].shift(126)))
    level_z = rz(m["tgt_hi"], 252)                            # high rate = headwind, dir -1
    n_trials = 12  # {easing3m,easing6m,level}x{fwd5,fwd20} + gate family ≈ conservative

    rows = []
    for sig, name, tgt, d in [
        (easing_3m, "Fed_easing3m", "fwd5", +1.0),
        (easing_3m, "Fed_easing3m", "fwd20", +1.0),
        (easing_6m, "Fed_easing6m", "fwd20", +1.0),
        (level_z,   "Fed_level_z",  "fwd5", -1.0),
        (level_z,   "Fed_level_z",  "fwd20", -1.0),
    ]:
        res = evaluate_anchor(sig.set_axis(m.index), m[tgt].set_axis(m.index), bull,
                              f"{name}[{tgt}]", n_trials, direction=d)
        res["target"] = tgt
        rows.append(res)
    # orthogonalize the strongest continuous factor vs foreign+champ + champion collinearity
    resid = orthogonalize(easing_6m.set_axis(m.index), controls)
    rr = evaluate_anchor(resid, m["fwd20"].set_axis(m.index), bull,
                         "Fed_easing6m ⟂(foreign+champ)[fwd20]", n_trials, direction=+1.0)
    dd = pd.concat([easing_6m.set_axis(m.index).rename("s"), m["champ"].rename("c"),
                    m["foreign"].rename("f")], axis=1).dropna()
    rr["corr_champ"] = dd["s"].corr(dd["c"]); rr["corr_foreign"] = dd["s"].corr(dd["f"])
    rows.append(rr)
    raw = pd.DataFrame(rows)

    # STRAND C1b — 降息 regime GATE (event-style, same-exposure permutation)
    # gate = 目前政策利率上緣 低於 6M 前 (= 進行中的降息循環)
    cutting = (m["tgt_hi"] < m["tgt_hi"].shift(126))
    hiking = (m["tgt_hi"] > m["tgt_hi"].shift(126))
    gate_rows = []
    for gname, gmask, tgt in (("降息循環(tgt<6M前)", cutting, "fwd20"),
                              ("升息循環(tgt>6M前)", hiking, "fwd20"),
                              ("降息循環(tgt<6M前)", cutting, "fwd5"),
                              ("FOMC降息事件當日→", (m["tgt_hi"].diff() < 0), "fwd5")):
        gm = gmask.fillna(False).values
        f = m[tgt].values
        fv = f[~np.isnan(f)]
        ok = gm & ~np.isnan(f)
        k = int(ok.sum())
        if k < 20:
            gate_rows.append({"gate": gname, "target": tgt, "n": k, "note": "n_lt_20"})
            continue
        obs = np.nanmean(f[gm])
        null = np.array([fv[RNG.choice(len(fv), k, replace=False)].mean() for _ in range(5000)])
        gate_rows.append({"gate": gname, "target": tgt, "n": k,
                          "mean_in_pct": obs * 100, "mean_out_pct": np.nanmean(f[~gm]) * 100,
                          "perm_p_high": float((null >= obs).mean()),
                          "perm_p_low": float((null <= obs).mean())})
    return raw, pd.DataFrame(gate_rows)


# ----------------------------------------------------------------------------
# STRAND C2 — VIXTWN (在地,同日) vs ^VIX (美盤,lag+1) — descriptive on 3M overlap
# ----------------------------------------------------------------------------
def run_vixtwn(panel: pd.DataFrame, vt: pd.DataFrame) -> dict:
    ext = pd.read_parquet(DATA / "global_macro_data.parquet")
    ext["date"] = pd.to_datetime(ext["date"])
    m = panel.copy()
    # ^VIX: US series, lag +1 TW day (backward, no exact) — the framework convention
    vix_us = ext[["date", "VIX"]].dropna().sort_values("date")
    m = pd.merge_asof(m.sort_values("date"), vix_us, on="date",
                      direction="backward", allow_exact_matches=False)
    # VIXTWN: local, SAME day (exact allowed) — no US timelag
    m = pd.merge_asof(m.sort_values("date"), vt.sort_values("date"), on="date",
                      direction="backward", allow_exact_matches=True)
    m = m.reset_index(drop=True)
    m["fwd1"] = m["ix_close"].pct_change().shift(-1)
    ov = m.dropna(subset=["VIX", "vixtwn"]).copy()
    n = len(ov)
    out = {"overlap_n": n,
           "date_min": str(ov["date"].min().date()) if n else None,
           "date_max": str(ov["date"].max().date()) if n else None}
    if n < 15:
        out["note"] = "overlap_too_small"
        return out
    out["corr_level_vix_vixtwn"] = ov["VIX"].corr(ov["vixtwn"])
    out["corr_chg_vix_vixtwn"] = ov["VIX"].diff().corr(ov["vixtwn"].diff())
    out["vixtwn_mean"] = ov["vixtwn"].mean()
    out["vix_us_mean"] = ov["VIX"].mean()
    # which better forecasts next-day TAIEX (higher vol → risk-off, dir -1)?
    # use daily change z within-window (small sample: report IC only, no perm claim)
    for lbl, col in (("vixtwn_sameday", "vixtwn"), ("vix_us_lag1", "VIX")):
        z = (ov[col] - ov[col].mean()) / ov[col].std()
        ic = (-z).corr(ov["fwd1"])                 # dir -1: high vol → low fwd ret
        out[f"IC_{lbl}_fwd1"] = ic
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refetch", action="store_true")
    args = ap.parse_args()
    pd.set_option("display.width", 200)
    pd.set_option("display.float_format", lambda x: f"{x:+.3f}" if isinstance(x, float) else str(x))

    panel = pd.read_parquet(PANEL); panel["date"] = pd.to_datetime(panel["date"])
    fed, vt = load_or_fetch(args.refetch)
    print(f"EFFR rows {len(fed)} ({fed['date'].min().date()}→{fed['date'].max().date()})  "
          f"目標上緣 {fed['tgt_hi'].iloc[0]:.2f}→{fed['tgt_hi'].iloc[-1]:.2f}")
    print(f"VIXTWN rows {len(vt)} ({vt['date'].min().date() if len(vt) else '-'}"
          f"→{vt['date'].max().date() if len(vt) else '-'})  [僅免費近3月]\n")

    print("=" * 78, "\nSTRAND C1 — Fed 政策路徑 → TAIEX (連續因子 + ⟂)\n", "=" * 78, sep="")
    fed_raw, fed_gate = run_fed(panel, fed)
    show = ["signal", "IC_IS", "IC_OOS", "IS_Sharpe", "OOS_Sharpe", "OOS_perm_p",
            "OOS_DSR", "OOS_Sharpe_bull", "OOS_Sharpe_bear"]
    print(fed_raw[show].to_string(index=False))
    if "corr_champ" in fed_raw:
        print("\n共線: ", fed_raw.dropna(subset=["corr_champ"])[["signal", "corr_champ", "corr_foreign"]].to_string(index=False))
    print("\n[STRAND C1b — 降息/升息 regime GATE, 同曝險 permutation 5000]")
    print(fed_gate.to_string(index=False))

    print("\n" + "=" * 78, "\nSTRAND C2 — VIXTWN(同日) vs ^VIX(lag+1) 同日比較 (3M overlap)\n", "=" * 78, sep="")
    vres = run_vixtwn(panel, vt)
    for k, v in vres.items():
        print(f"  {k}: {v}")

    fed_raw.to_csv(OUT / "global_macro_fed_metrics.csv", index=False)
    fed_gate.to_csv(OUT / "global_macro_fed_gate.csv", index=False)
    pd.DataFrame([vres]).to_csv(OUT / "global_macro_vixtwn_compare.csv", index=False)
    print(f"\n-> {OUT/'global_macro_fed_metrics.csv'}\n-> {OUT/'global_macro_fed_gate.csv'}"
          f"\n-> {OUT/'global_macro_vixtwn_compare.csv'}")


if __name__ == "__main__":
    main()
