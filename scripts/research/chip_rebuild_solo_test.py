#!/usr/bin/env python3
"""chip-orthogonal-rebuild 步驟 2（A 方）：八因子單獨檢定。

輸入：reports/research/chip-orthogonal-rebuild/panel.pkl（步驟 1 共用面板）
輸出：reports/research/chip-orthogonal-rebuild/solo_test_A.json

每因子計算：
(a) 未控五分位價差：逐日在宇宙內按因子分五分位（rank 五等分），
    spread = mean(r_oc | Q5) - mean(r_oc | Q1)，%/日；t = 日 spread 序列 Newey-West(lag 5)。
(b) FM 風險中性：逐日橫斷面 OLS
      r_oc ~ 1 + frank + z(vol60) + z(gap) + z(turnover)
    其中 frank = 當日橫斷面 rank/(n-1) - 0.5（係數即「因子由最低到最高」的全距報酬），
    t = 日係數序列 Newey-West(lag 5)。
存活判準：中性後 |t| >= 3（config/research.yaml chip-orthogonal-rebuild 預註記）。

規格備忘：
- 宇宙旗標 in_universe（close>=10 且 vol20>300k）已在面板內。
- gap 欄位＝報酬日（T+1）的 open/prev_close-1（面板步驟 1 已對齊）。
- F5 z6 截斷至 2026-07-16；其餘至 2026-08-26。
- 面板存 raw z（未 fillna(0)/clip）；本檢定用 rank，clip 與否不影響。
- 每日最低有效檔數 MIN_N=30，不足整日剔除。
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = PROJECT_ROOT / "reports/research/chip-orthogonal-rebuild"
PANEL_PATH = OUT_DIR / "panel.pkl"
OUT_PATH = OUT_DIR / "solo_test_A.json"

F5_CUTOFF = "2026-07-16"
MIN_N = 30
NW_LAG = 5

FACTORS = [
    ("F1", "z1", "Δ借券賣出餘額 60日自身z", None),
    ("F2", "zp", "借券佔股本 243日分位", None),
    ("F3", "zu", "Δ券源使用率 60日自身z", None),
    ("F4", "zf", "借券費率 60筆分位", None),
    ("F5", "z6", "分點買賣家數差 當日橫斷面分位", F5_CUTOFF),
    ("F6", "retail", "集保<50張持股比水位（週頻 PIT）", None),
    ("F7", "margin", "Δ融資餘額/股本", None),
    ("F8", "inst", "三大法人合計買超/成交量", None),
]


def nw_tstat(series: np.ndarray, lag: int = NW_LAG) -> tuple[float, float]:
    """Newey-West t 檢定 H0: mean=0。回傳 (mean, t)。"""
    x = series[~np.isnan(series)]
    n = len(x)
    if n < 20:
        return float("nan"), float("nan")
    mu = x.mean()
    e = x - mu
    gamma0 = float(e @ e) / n
    lrv = gamma0
    for j in range(1, lag + 1):
        gamma_j = float(e[j:] @ e[:-j]) / n
        lrv += 2.0 * (1.0 - j / (lag + 1)) * gamma_j
    se = np.sqrt(lrv / n)
    return float(mu), float(mu / se) if se > 0 else float("nan")


def zscore(v: np.ndarray) -> np.ndarray:
    sd = np.nanstd(v)
    if not np.isfinite(sd) or sd == 0:
        return np.zeros_like(v)
    return (v - np.nanmean(v)) / sd


def daily_quintile_spread(g: pd.DataFrame, col: str) -> float:
    """當日 Q5-Q1 平均 r_oc（比例）。rank 五等分，抗重值。"""
    r = g[col].rank(method="first")
    q = np.ceil(r / (len(g) / 5.0)).clip(1, 5)
    m = g.groupby(q)["r_oc"].mean()
    if 1 not in m.index or 5 not in m.index:
        return np.nan
    return float(m.loc[5] - m.loc[1])


def daily_fm_slope(g: pd.DataFrame, col: str) -> float:
    """當日橫斷面 OLS r_oc ~ 1 + frank + z(vol60)+z(gap)+z(turnover) 的 frank 係數。"""
    n = len(g)
    frank = g[col].rank(method="average").to_numpy()
    frank = (frank - 1) / (n - 1) - 0.5
    X = np.column_stack([
        np.ones(n),
        frank,
        zscore(g["vol60"].to_numpy(dtype=float)),
        zscore(g["gap"].to_numpy(dtype=float)),
        zscore(g["turnover"].to_numpy(dtype=float)),
    ])
    y = g["r_oc"].to_numpy(dtype=float)
    try:
        beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    except np.linalg.LinAlgError:
        return np.nan
    return float(beta[1])


def main() -> None:
    panel = pd.read_pickle(PANEL_PATH)
    results = []
    for fid, col, label, cutoff in FACTORS:
        df = panel[panel["in_universe"]].copy()
        if cutoff:
            df = df[df["trade_date"] <= cutoff]
        need = [col, "r_oc", "vol60", "gap", "turnover"]
        df = df.dropna(subset=need)
        # 逐日
        spreads, slopes, dates, counts = [], [], [], []
        for d, g in df.groupby("trade_date", sort=True):
            if len(g) < MIN_N:
                continue
            spreads.append(daily_quintile_spread(g, col))
            slopes.append(daily_fm_slope(g, col))
            dates.append(d)
            counts.append(len(g))
        spreads = np.array(spreads, dtype=float)
        slopes = np.array(slopes, dtype=float)
        raw_mean, raw_t = nw_tstat(spreads)
        neu_mean, neu_t = nw_tstat(slopes)
        survives = bool(np.isfinite(neu_t) and abs(neu_t) >= 3.0)
        results.append({
            "id": fid,
            "column": col,
            "label": label,
            "n_days": len(dates),
            "date_range": [dates[0], dates[-1]] if dates else None,
            "n_stocks_avg": float(np.mean(counts)) if counts else float("nan"),
            "raw_spread_pct": raw_mean * 100.0,
            "raw_t": raw_t,
            "neutral_slope_pct": neu_mean * 100.0,
            "neutral_t": neu_t,
            "survives": survives,
        })
        print(f"{fid} {col:8s} days={len(dates):4d} n={np.mean(counts):7.1f} "
              f"raw={raw_mean*100:+.4f}%/d (t={raw_t:+.2f}) "
              f"neutral t={neu_t:+.2f} {'SURVIVES' if survives else ''}")

    # 附加診斷（影響解讀，寫進輸出）
    notes = {
        "F4": "面板 zf 依 snapshot pipeline SSOT 定義含無借券成交日（宇宙內非NaN列 66.7% 為 0）；"
              "若改成剔除無成交日（B 方選擇）raw_t 會從 -5.6 掉到約 -1.8——定義差異非統計 bug",
        "F5": "中性後由正轉負完全來自 gap 控制（單控 gap: t=-2.30；單控 vol60/turnover 仍 +2.8~+2.9）"
              "——z6 與報酬日跳空同向，控跳空後反轉且不過 |t|>=3",
        "F7": "存活 t=+8.52 主要由 2026-06-01 前 finmind 子集時代（463日·~121檔/日·選樣偏誤）貢獻"
              "（該段 t=+8.03）；twse_mi_margn 全市場時代僅 60 日 t=+2.77 未達 3——"
              "存活判定成立於混合樣本，全市場代表性未證明",
    }
    for r in results:
        if r["id"] in notes:
            r["note"] = notes[r["id"]]

    out = {
        "impl": "A（共用面板 panel.pkl；步驟 1 ETL 已與 stock_chip_snapshot 逐股比對 max|diff|<=4e-12）",
        "spec": "config/research.yaml chip-orthogonal-rebuild（2026-08-27 預註記）",
        "panel": str(PANEL_PATH),
        "method": {
            "raw": "宇宙內逐日因子 rank 五等分，Q5-Q1 平均 r_oc；t=NW(lag5) on 日 spread 序列",
            "neutral": "FM 逐日 OLS r_oc ~ 1 + (rank/(n-1)-0.5) + z(vol60)+z(gap報酬日)+z(turnover)；t=NW(lag5) on 日係數序列",
            "min_n_per_day": MIN_N,
            "survival": "|neutral_t| >= 3",
            "f5_cutoff": F5_CUTOFF,
            "listwise_note": "逐因子 dropna(factor, r_oc, vol60, gap, turnover)；raw 與 neutral 用同一樣本以利對照",
        },
        "factors": results,
    }
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    print(f"written: {OUT_PATH}")


if __name__ == "__main__":
    main()
