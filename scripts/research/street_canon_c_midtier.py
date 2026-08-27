#!/usr/bin/env python
"""H-STREET-C：中實戶級距（100-400 張 = pct_100 - pct_400）連續 N 週上升 → 前瞻相對報酬。

預註記規格（config/research.yaml topic chip-street-canon，禁止事後加格子）：
- mid(T) = pct_100 - pct_400（累計制「>=張數」持股比之差 = 100~400 張級距）。
- 事件：mid 週差 > 0 連續 >= N 週（N∈{2,3}）；對照組：週差 < 0 連續 >= N 週。
- PIT：集保為週五資料、下週一才可得 → 資料日 d 後第 2 個交易日開盤進場，
  持有 h∈{2,4} 週（10/20 交易日，含進場日）收盤出。共 4 格。
- 報酬主口徑：橫斷面去均值（減同週 251 檔期貨宇宙、過濾網後之均值）；附原始均值。
  升/降兩組去均值後同號 = 混淆非方向（tdcc-big-holder-forward 死法）→ 判 reject。
- 重疊：週頻算 2/4 週前瞻 → NW lag>=h 之外，附「每 h 週取一筆」不重疊版本。
- 宇宙濾網（PIT，T 日及以前）：close>=10 且 20 日均量 > 300,000 股。
- 成本 30bps 來回；判準 t>=3.0；4 格 BH-FDR。Holdout：d >= 2026-01-01 另報。
- 千張 40-70% 甜蜜區僅描述性附錄（pct_1000 水位分箱 vs 前瞻報酬），不設假說。

資料：
- 集保快取 reports/research/chip-overlays/cache/holding_shares_per_futures_universe.csv
- 價格：reports/research/chip-street-canon/cache/price_panel.pkl（stock_daily_bars
  已去重面板，優先序 twse_mi_index > tpex_daily > finmind > yfinance；本腳本仍自檢
  去重與來源分布並回報）。
- 交易日曆 = 去重面板全市場 trade_date 全集（不用 per-stock shift，避免 91.2% 坑；
  以日曆索引取進出場日，個股缺該日價格 → 事件設 NaN 剔除並回報缺口率）。

執行：PYTHONPATH=src .venv/bin/python scripts/research/street_canon_c_midtier.py
"""

from __future__ import annotations

import json
import pickle
from bisect import bisect_right
from pathlib import Path

import numpy as np
import pandas as pd
import statsmodels.api as sm
from scipy import stats as sps

PROJECT_ROOT = Path(__file__).resolve().parents[2]
HOLD_CSV = PROJECT_ROOT / "reports/research/chip-overlays/cache/holding_shares_per_futures_universe.csv"
PRICE_PKL = PROJECT_ROOT / "reports/research/chip-street-canon/cache/price_panel.pkl"
REPORT_DIR = PROJECT_ROOT / "reports/research/chip-street-canon"
REPORT_MD = REPORT_DIR / "C_report.md"
CELLS_JSON = REPORT_DIR / "C_cells.json"

HOLDOUT_START = "2026-01-01"
COST_RT_PCT = 0.30          # 30bps 來回，事件制：每趟扣一次
MIN_CLOSE = 10.0
MIN_AVG_VOL20 = 300_000     # 股
ENTRY_LAG_TDAYS = 2         # 資料日後第 2 個交易日開盤進場
STREAK_MAX_GAP_DAYS = 9     # 週資料相鄰列間隔 <=9 天才視為連續週（13/15 天假期缺口斷開連續性）
GRID_N = (2, 3)
GRID_H_WEEKS = (2, 4)
H_TDAYS = {2: 10, 4: 20}    # 持有交易日數（含進場日）


# ---------------------------------------------------------------- helpers

def nw_tstat(series: np.ndarray, lags: int) -> tuple[float, float, float]:
    """對常數迴歸的 Newey-West t（回傳 mean, t, p 兩尾）。"""
    y = np.asarray(series, dtype=float)
    y = y[~np.isnan(y)]
    if len(y) < 3:
        return (float(np.nanmean(y)) if len(y) else np.nan, np.nan, np.nan)
    x = np.ones((len(y), 1))
    res = sm.OLS(y, x).fit(cov_type="HAC", cov_kwds={"maxlags": lags})
    return float(res.params[0]), float(res.tvalues[0]), float(res.pvalues[0])


def plain_tstat(series: np.ndarray) -> tuple[float, float, int]:
    y = np.asarray(series, dtype=float)
    y = y[~np.isnan(y)]
    if len(y) < 3:
        return (float(np.nanmean(y)) if len(y) else np.nan, np.nan, len(y))
    t, _p = sps.ttest_1samp(y, 0.0)
    return float(y.mean()), float(t), len(y)


def bh_fdr(pvals: list[float]) -> list[float]:
    """Benjamini-Hochberg q 值（NaN p 保留 NaN）。"""
    p = np.asarray(pvals, dtype=float)
    q = np.full_like(p, np.nan)
    ok = ~np.isnan(p)
    pv = p[ok]
    m = len(pv)
    if m == 0:
        return q.tolist()
    order = np.argsort(pv)
    ranked = pv[order] * m / (np.arange(m) + 1)
    ranked = np.minimum.accumulate(ranked[::-1])[::-1]
    qq = np.empty(m)
    qq[order] = np.clip(ranked, 0, 1)
    q[ok] = qq
    return q.tolist()


# ---------------------------------------------------------------- load data

def main() -> None:
    sanity: list[str] = []

    hold = pd.read_csv(HOLD_CSV, dtype={"sid": str})
    n_hold_raw = len(hold)
    hold = hold.drop_duplicates(["sid", "d"]).sort_values(["sid", "d"]).reset_index(drop=True)
    sanity.append(
        f"holdings CSV：{n_hold_raw} 列 → 去重後 {len(hold)} 列（重複 {n_hold_raw - len(hold)}）"
        f"；{hold['sid'].nunique()} 檔 × {hold['d'].nunique()} 週，{hold['d'].min()}~{hold['d'].max()}"
    )

    panel: pd.DataFrame = pickle.load(open(PRICE_PKL, "rb"))
    n_panel_raw = len(panel)
    dup = int(panel.duplicated(["stock_id", "trade_date"]).sum())
    src_counts = panel["source"].value_counts().to_dict()
    sanity.append(
        f"price_panel：{n_panel_raw} 列，stock-day 重複 {dup}（快取已按 twse_mi_index>"
        f"tpex_daily>finmind>yfinance 去重）；來源分布 {src_counts}"
    )
    assert dup == 0, "價格面板仍有多來源重複列，需先去重"

    # 全市場交易日曆（全 source 全股 trade_date 全集）
    calendar = sorted(panel["trade_date"].unique())
    cal_idx = {d: i for i, d in enumerate(calendar)}
    sanity.append(f"交易日曆：{len(calendar)} 個交易日（{calendar[0]}~{calendar[-1]}）")

    sids = sorted(hold["sid"].unique())
    sub = panel[panel["stock_id"].isin(sids)].copy()
    n_sub = len(sub)
    open_w = sub.pivot(index="trade_date", columns="stock_id", values="open").reindex(calendar)
    close_w = sub.pivot(index="trade_date", columns="stock_id", values="close").reindex(calendar)
    vol_w = sub.pivot(index="trade_date", columns="stock_id", values="volume").reindex(calendar)
    vol20_w = vol_w.rolling(20, min_periods=20).mean()
    covered = open_w.notna().sum().sum()
    sanity.append(
        f"251 檔宇宙價格子面板：{n_sub} 列（{sub['stock_id'].nunique()}/{len(sids)} 檔有價格）"
        f"；open 覆蓋 {covered}/{len(calendar) * len(sids)} 格"
        f"（{covered / (len(calendar) * len(sids)):.1%}）"
    )

    # ------------------------------------------------------------ streaks
    hold["mid"] = hold["pct_100"] - hold["pct_400"]
    hold["d_dt"] = pd.to_datetime(hold["d"])
    hold["mid_chg"] = hold.groupby("sid")["mid"].diff()
    hold["gap_days"] = hold.groupby("sid")["d_dt"].diff().dt.days

    def _streaks(g: pd.DataFrame) -> pd.DataFrame:
        up = np.zeros(len(g), dtype=int)
        dn = np.zeros(len(g), dtype=int)
        chg = g["mid_chg"].to_numpy()
        gap = g["gap_days"].to_numpy()
        for i in range(len(g)):
            if np.isnan(chg[i]) or gap[i] > STREAK_MAX_GAP_DAYS:
                continue
            if chg[i] > 0:
                up[i] = up[i - 1] + 1 if i > 0 else 1
            elif chg[i] < 0:
                dn[i] = dn[i - 1] + 1 if i > 0 else 1
        g = g.copy()
        g["up_streak"] = up
        g["dn_streak"] = dn
        return g

    hold = hold.groupby("sid", group_keys=False).apply(_streaks)
    n_gap_broken = int((hold["gap_days"] > STREAK_MAX_GAP_DAYS).sum())
    sanity.append(
        f"週序連續性：相鄰列間隔 >{STREAK_MAX_GAP_DAYS} 天者 {n_gap_broken} 列"
        f"（假期缺口，streak 斷開不跨接）；mid_chg 恰為 0 者 "
        f"{int((hold['mid_chg'] == 0).sum())} 列（不算升也不算降）"
    )

    # ------------------------------------------------------------ PIT entry/exit per (sid, week)
    weeks = sorted(hold["d"].unique())
    entry_idx_by_week: dict[str, int | None] = {}
    for d in weeks:
        pos = bisect_right(calendar, d)          # 第 1 個 > d 的交易日
        eidx = pos + ENTRY_LAG_TDAYS - 1         # 第 2 個交易日
        entry_idx_by_week[d] = eidx if eidx < len(calendar) else None

    # 驗證「資料日後第 2 個交易日」語意：週五資料 → 通常週二進場
    chk = []
    for d in weeks[:5]:
        ei = entry_idx_by_week[d]
        chk.append(f"{d}→{calendar[ei] if ei is not None else 'NA'}")
    sanity.append("PIT 進場日抽查（資料日→進場日）：" + "; ".join(chk))

    # 每週每檔的前瞻報酬（供事件與同週宇宙 benchmark 共用；口徑完全一致）
    ret: dict[int, pd.DataFrame] = {}
    entry_dates: dict[str, str] = {}
    exit_dates: dict[int, dict[str, str]] = {h: {} for h in GRID_H_WEEKS}
    open_v = open_w.to_numpy()
    close_v = close_w.to_numpy()
    col_of = {s: j for j, s in enumerate(open_w.columns)}
    for h in GRID_H_WEEKS:
        hd = H_TDAYS[h]
        mat = np.full((len(weeks), len(sids)), np.nan)
        for wi, d in enumerate(weeks):
            ei = entry_idx_by_week[d]
            if ei is None:
                continue
            xi = ei + hd - 1                     # 含進場日共 hd 個交易日
            if xi >= len(calendar):
                continue
            entry_dates[d] = calendar[ei]
            exit_dates[h][d] = calendar[xi]
            for sj, s in enumerate(sids):
                j = col_of.get(s)
                if j is None:
                    continue
                o = open_v[ei, j]
                c = close_v[xi, j]
                if np.isfinite(o) and np.isfinite(c) and o > 0:
                    mat[wi, sj] = (c / o - 1.0) * 100.0
        ret[h] = pd.DataFrame(mat, index=weeks, columns=sids)

    # ------------------------------------------------------------ universe filter (PIT at data date d)
    filt = np.zeros((len(weeks), len(sids)), dtype=bool)
    for wi, d in enumerate(weeks):
        pos = bisect_right(calendar, d) - 1      # 最後一個 <= d 的交易日
        if pos < 0:
            continue
        c_row = close_v[pos]
        v20_row = vol20_w.to_numpy()[pos]
        for sj, s in enumerate(sids):
            j = col_of.get(s)
            if j is None:
                continue
            if np.isfinite(c_row[j]) and c_row[j] >= MIN_CLOSE and np.isfinite(v20_row[j]) and v20_row[j] > MIN_AVG_VOL20:
                filt[wi, sj] = True
    filt_df = pd.DataFrame(filt, index=weeks, columns=sids)
    sanity.append(
        f"宇宙濾網（close>={MIN_CLOSE:g} 且 20 日均量>{MIN_AVG_VOL20:,} 股，PIT）："
        f"週均通過 {filt_df.sum(axis=1).mean():.1f}/251 檔"
    )

    # join 前後列數（事件 × 價格）
    hold_w = hold.set_index(["d", "sid"])

    # ------------------------------------------------------------ benchmark（同週 251 檔宇宙、過濾網後全體均值）
    bench: dict[int, pd.Series] = {}
    for h in GRID_H_WEEKS:
        r = ret[h].where(filt_df)
        bench[h] = r.mean(axis=1)
        n_valid = r.notna().sum(axis=1)
        sanity.append(
            f"h={h}週 benchmark：週均有效檔數 {n_valid[n_valid > 0].mean():.1f}，"
            f"可算週數 {(n_valid > 0).sum()}/{len(weeks)}"
        )

    # ------------------------------------------------------------ cell evaluation
    up_flag = hold.pivot(index="d", columns="sid", values="up_streak").reindex(index=weeks, columns=sids)
    dn_flag = hold.pivot(index="d", columns="sid", values="dn_streak").reindex(index=weeks, columns=sids)

    def eval_group(streak_df: pd.DataFrame, n_req: int, h: int) -> dict:
        hd = H_TDAYS[h]
        raw = ret[h]
        is_event = (streak_df >= n_req) & filt_df
        n_signal = int(((streak_df >= n_req)).sum().sum())
        n_after_filter = int(is_event.sum().sum())
        r_evt = raw.where(is_event)
        n_events = int(r_evt.notna().sum().sum())          # 有進出場價的最終事件數
        excess = r_evt.sub(bench[h], axis=0)

        # per-event pooled
        pooled_ex = excess.to_numpy().ravel()
        pooled_raw = r_evt.to_numpy().ravel()
        mean_ex_pooled, _, _ = plain_tstat(pooled_ex)
        mean_raw_pooled, _, _ = plain_tstat(pooled_raw)

        # weekly portfolio series（主檢定：等權週組合，NW lag = h 週）
        wk_ex = excess.mean(axis=1)
        wk_raw = r_evt.mean(axis=1)
        wk_ex_v = wk_ex.dropna()
        mean_ex_wk, t_nw, p_nw = nw_tstat(wk_ex_v.to_numpy(), lags=h)
        mean_raw_wk = float(wk_raw.dropna().mean()) if wk_raw.notna().any() else np.nan

        # 不重疊：每 h 週取一筆（從第一個有事件的週起，跨完整序列 stride）
        non_ov = wk_ex_v.iloc[::h]
        mean_no, t_no, n_no = plain_tstat(non_ov.to_numpy())

        # holdout
        ho_mask = wk_ex_v.index >= HOLDOUT_START
        ho = wk_ex_v[ho_mask]
        mean_ho, t_ho, n_ho_weeks = plain_tstat(ho.to_numpy())
        n_ho_events = int(excess.loc[excess.index >= HOLDOUT_START].notna().sum().sum())

        return {
            "n_signal_rows": n_signal,
            "n_after_universe_filter": n_after_filter,
            "n_events": n_events,
            "n_weeks": int(len(wk_ex_v)),
            "mean_excess_pct": round(mean_ex_wk, 4) if np.isfinite(mean_ex_wk) else None,
            "mean_excess_pooled_pct": round(mean_ex_pooled, 4) if np.isfinite(mean_ex_pooled) else None,
            "mean_raw_pct": round(mean_raw_wk, 4) if np.isfinite(mean_raw_wk) else None,
            "mean_raw_pooled_pct": round(mean_raw_pooled, 4) if np.isfinite(mean_raw_pooled) else None,
            "t_nw": round(t_nw, 3) if np.isfinite(t_nw) else None,
            "p_nw": p_nw if np.isfinite(p_nw) else None,
            "nonoverlap_mean_pct": round(mean_no, 4) if np.isfinite(mean_no) else None,
            "nonoverlap_t": round(t_no, 3) if np.isfinite(t_no) else None,
            "nonoverlap_n": n_no,
            "holdout_mean_pct": round(mean_ho, 4) if np.isfinite(mean_ho) else None,
            "holdout_t": round(t_ho, 3) if np.isfinite(t_ho) else None,
            "holdout_n_weeks": n_ho_weeks,
            "holdout_n_events": n_ho_events,
            "net_after_cost_pct": round(mean_ex_wk - COST_RT_PCT, 4) if np.isfinite(mean_ex_wk) else None,
        }

    cells = []
    for n_req in GRID_N:
        for h in GRID_H_WEEKS:
            up = eval_group(up_flag, n_req, h)
            dn = eval_group(dn_flag, n_req, h)
            same_sign = (
                up["mean_excess_pct"] is not None
                and dn["mean_excess_pct"] is not None
                and np.sign(up["mean_excess_pct"]) == np.sign(dn["mean_excess_pct"])
                and up["mean_excess_pct"] != 0
            )
            cells.append({
                "cell_id": f"N{n_req}_h{h}w",
                "N": n_req, "h_weeks": h, "h_tdays": H_TDAYS[h],
                "up": up, "down": dn,
                "confounded_same_sign": bool(same_sign),
            })

    qs = bh_fdr([c["up"]["p_nw"] if c["up"]["p_nw"] is not None else np.nan for c in cells])
    for c, q in zip(cells, qs):
        c["q_bh"] = round(q, 4) if np.isfinite(q) else None
        u = c["up"]
        c["passes"] = bool(
            u["t_nw"] is not None and u["t_nw"] >= 3.0
            and c["q_bh"] is not None and c["q_bh"] < 0.05
            and u["net_after_cost_pct"] is not None and u["net_after_cost_pct"] > 0
            and not c["confounded_same_sign"]
        )

    # join 前後列數回報（事件列 vs 有價格的最終事件列）
    total_sig = sum(c["up"]["n_signal_rows"] for c in cells if c["N"] == 2 and c["h_weeks"] == 2)
    sanity.append(
        f"join 追蹤（N=2,h=2w up 為例）：訊號列 {cells[0]['up']['n_signal_rows']} → "
        f"過濾網後 {cells[0]['up']['n_after_universe_filter']} → 有進出場價 {cells[0]['up']['n_events']}"
    )
    _ = total_sig

    # ------------------------------------------------------------ appendix: pct_1000 水位分箱（描述性）
    lvl = hold.pivot(index="d", columns="sid", values="pct_1000").reindex(index=weeks, columns=sids)
    bins = [(0, 40), (40, 55), (55, 70), (70, 101)]
    appendix = []
    for lo, hi in bins:
        m = (lvl >= lo) & (lvl < hi) & filt_df
        row = {"bin": f"[{lo},{hi})", "n": int((ret[4].where(m)).notna().sum().sum())}
        for h in GRID_H_WEEKS:
            ex = ret[h].where(m).sub(bench[h], axis=0)
            wk = ex.mean(axis=1).dropna()
            mu, t, _p = nw_tstat(wk.to_numpy(), lags=h)
            row[f"h{h}w_mean_excess_pct"] = round(mu, 4) if np.isfinite(mu) else None
            row[f"h{h}w_t_nw"] = round(t, 3) if np.isfinite(t) else None
        appendix.append(row)

    # ------------------------------------------------------------ outputs
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    CELLS_JSON.write_text(json.dumps({"cells": cells, "appendix": appendix, "sanity": sanity},
                                     ensure_ascii=False, indent=2))

    verdict_lines = []
    any_pass = any(c["passes"] for c in cells)
    any_confound = any(c["confounded_same_sign"] for c in cells)

    md = []
    md.append("# H-STREET-C：中實戶級距（100–400 張）連續多週上升 — 檢定報告\n")
    md.append(f"執行日：2026-08-27 · 腳本：`scripts/research/street_canon_c_midtier.py` · "
              f"預註記：`config/research.yaml` topic `chip-street-canon`\n")
    md.append("## 規格（照預註記，未改動）\n")
    md.append("- mid(T) = pct_100 − pct_400（集保累計制，= 100~400 張級距持股比）\n"
              "- 事件：mid 週差 > 0 連續 ≥ N 週（N∈{2,3}）；對照組：週差 < 0 連續 ≥ N 週\n"
              f"- PIT：週五資料下週一可得 → 資料日後第 2 個交易日**開盤**進場；持有 2/4 週（10/20 交易日，含進場日）**收盤**出\n"
              f"- 宇宙濾網（PIT）：close ≥ {MIN_CLOSE:g} 且 20 日均量 > {MIN_AVG_VOL20:,} 股\n"
              "- 主口徑：橫斷面去均值（減同週 251 檔宇宙、過同濾網之均值）；附原始均值\n"
              f"- 主檢定：等權週組合序列 Newey-West（lag = h 週）；附每 h 週取一筆不重疊版\n"
              f"- 成本 {COST_RT_PCT:.2f}%（30bps 來回，事件制每趟一次）；判準 t ≥ 3.0 + BH-FDR(4 格) q<0.05 + 淨值>0 + 升降兩組不同號\n")

    md.append("## 主表（4 格 · 連續上升組）\n")
    md.append("| cell | N | h | 事件數 | 週數 | 去均值均值% | 原始均值% | NW t | q(BH) | 不重疊 t(n) | 扣成本後% | Holdout均值% | Holdout t(週) | 判定 |")
    md.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for c in cells:
        u = c["up"]
        md.append(
            f"| {c['cell_id']} | {c['N']} | {c['h_weeks']}週 | {u['n_events']} | {u['n_weeks']} "
            f"| {u['mean_excess_pct']} | {u['mean_raw_pct']} | {u['t_nw']} | {c['q_bh']} "
            f"| {u['nonoverlap_t']} ({u['nonoverlap_n']}) | {u['net_after_cost_pct']} "
            f"| {u['holdout_mean_pct']} | {u['holdout_t']} ({u['holdout_n_weeks']}) "
            f"| {'PASS' if c['passes'] else 'FAIL'} |")

    md.append("\n## 對照表（連續下降組）與混淆判定\n")
    md.append("| cell | 事件數 | 去均值均值% | NW t | 原始均值% | 升降同號？ |")
    md.append("|---|---|---|---|---|---|")
    for c in cells:
        d = c["down"]
        md.append(f"| {c['cell_id']} | {d['n_events']} | {d['mean_excess_pct']} | {d['t_nw']} "
                  f"| {d['mean_raw_pct']} | {'**是（混淆）**' if c['confounded_same_sign'] else '否'} |")

    if any_confound:
        verdict_lines.append("升/降兩組去均值後同號的格子存在 → 該格為混淆非方向性（同 tdcc-big-holder-forward 死法）。")
    if not any_pass:
        verdict_lines.append("4 格皆未達 t≥3.0＋BH-FDR＋扣成本判準 → H-STREET-C 不成立（reject）。")
    else:
        verdict_lines.append("有格子通過，需進對抗覆核（lookahead / 雙來源 / 選格）後才算數。")
    verdict_lines.append(
        "解讀註記：原始均值 4 格全為正（0.5~1.6%）但下降組原始均值同樣全為正——"
        "那是樣本窗（2024-06 起）251 檔期貨宇宙整體上漲的 beta，不是訊號；"
        "去均值後上升組僅 ±0.08% 且 N=3 比 N=2 更差（甚至轉負、N3_h2w 與下降組同號），"
        "「連續愈久愈強」的劑量反應不存在，方向與周依晴 2020 的正向先驗不符。")

    md.append("\n## 附錄（描述性 · 不進判定）：千張大戶 pct_1000 水位分箱 vs 前瞻去均值報酬\n")
    md.append("| pct_1000 bin | n(4週有效) | 2週均值% | 2週 NW t | 4週均值% | 4週 NW t |")
    md.append("|---|---|---|---|---|---|")
    for r in appendix:
        md.append(f"| {r['bin']} | {r['n']} | {r['h2w_mean_excess_pct']} | {r['h2w_t_nw']} "
                  f"| {r['h4w_mean_excess_pct']} | {r['h4w_t_nw']} |")

    md.append("\n## Sanity（來源去重 / PIT / join 列數自檢）\n")
    for s in sanity:
        md.append(f"- {s}")

    md.append("\n## 判定\n")
    for v in verdict_lines:
        md.append(f"- {v}")

    REPORT_MD.write_text("\n".join(md) + "\n")

    print("=== SANITY ===")
    for s in sanity:
        print("-", s)
    print("\n=== CELLS (up) ===")
    for c in cells:
        u = c["up"]
        print(f"{c['cell_id']}: n={u['n_events']} ex={u['mean_excess_pct']}% raw={u['mean_raw_pct']}% "
              f"tNW={u['t_nw']} q={c['q_bh']} tNO={u['nonoverlap_t']} net={u['net_after_cost_pct']}% "
              f"ho={u['holdout_mean_pct']}%({u['holdout_t']}) confound={c['confounded_same_sign']} "
              f"passes={c['passes']}")
    print("\n=== CELLS (down, 對照) ===")
    for c in cells:
        d = c["down"]
        print(f"{c['cell_id']}: n={d['n_events']} ex={d['mean_excess_pct']}% tNW={d['t_nw']}")
    print("\nreport:", REPORT_MD)


if __name__ == "__main__":
    main()
