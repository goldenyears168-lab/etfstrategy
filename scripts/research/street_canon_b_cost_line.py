#!/usr/bin/env python
"""H-STREET-B 主力成本線檢定（chip-street-canon B 線）.

規格（config/research.yaml topic "chip-street-canon" 預註記，不得改）：
- cost20(T) = Σ_{t∈過去20日}(top15_net_t × vwap_t) ÷ Σ top15_net_t，
  僅在 Σ top15_net > 0 且 Σ top15_net >= 0.5% × 過去20日總成交量（張）時有定義。
- 前提濾網：5 日平均家數差 (n_buy_houses - n_sell_houses) < 0。
- 事件：close 上穿 / 下穿 cost20（昨收 vs 昨 cost、今收 vs 今 cost）。
- 進出場：open(T+1) 進、close(T+h) 出，h∈{5,10}；共 4 格。
- 窗 2024-07-01~2026-08-26；tape regime 限制 → 主檢定 T ≤ 2026-07-16；
  holdout 2026-01-01 之後（同樣截斷於 2026-07-16，之後為退化 tape 只另行標註）。
- 主口徑橫斷面去均值（減同日事件外全宇宙均值）、附無條件原始均值、
  Newey-West lag>=h（日聚合 calendar-time）、BH-FDR(4格)、扣 30bps 來回成本。
- 描述性附錄：(close-cost20)/cost20 十分位的隔日與 5 日去均值報酬單調性。

執行：PYTHONPATH=src .venv/bin/python scripts/research/street_canon_b_cost_line.py
輸出：reports/research/chip-street-canon/B_report.md（含所有自檢數據）
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CACHE_DIR = PROJECT_ROOT / "reports/research/chip-street-canon/cache"
REPORT_PATH = PROJECT_ROOT / "reports/research/chip-street-canon/B_report.md"

WINDOW_START = "2024-07-01"          # 預註記窗起點（tape 主段起點）
WINDOW_END = "2026-08-26"            # 預註記窗終點
TAPE_VALID_END = "2026-07-16"        # 分點 tape 退化前最後一日（cache README）
HOLDOUT_START = "2026-01-01"
HOLDS = (5, 10)
COST_RT_PCT = 0.30                   # 30bps 來回，事件制整段扣一次
UNIV_MIN_CLOSE = 10.0
UNIV_MIN_AVGVOL = 300_000            # 股
COST20_LOOKBACK = 20
DENOM_FLOOR_FRAC = 0.005             # Σtop15_net < 0.5% × 20日總量(張) → cost20 未定義
HOUSE_DIFF_WINDOW = 5

sanity: dict[str, object] = {}


def nw_tstat(series: np.ndarray, lag: int) -> tuple[float, float, int]:
    """Newey-West t-stat of the mean of a (time-ordered) series, lag L.

    回傳 (mean, t, n)。變異數 = (γ0 + 2Σ_{l=1..L} w_l γ_l) / n，Bartlett 權重。
    """
    x = np.asarray(series, dtype=float)
    x = x[~np.isnan(x)]
    n = len(x)
    if n < 3:
        return (float("nan"), float("nan"), n)
    m = x.mean()
    e = x - m
    gamma0 = float(e @ e) / n
    var = gamma0
    for l in range(1, min(lag, n - 1) + 1):
        w = 1.0 - l / (lag + 1.0)
        var += 2.0 * w * float(e[:-l] @ e[l:]) / n
    var = max(var, 1e-18)
    t = m / np.sqrt(var / n)
    return (m, float(t), n)


def two_sided_p(t: float) -> float:
    from math import erf, sqrt
    if np.isnan(t):
        return float("nan")
    return 2.0 * (1.0 - 0.5 * (1.0 + erf(abs(t) / sqrt(2.0))))


def bh_fdr(pvals: list[float]) -> list[float]:
    """Benjamini-Hochberg q-values（保持原順序）。"""
    p = np.asarray(pvals, dtype=float)
    n = len(p)
    order = np.argsort(p)
    q = np.full(n, np.nan)
    prev = 1.0
    for rank_from_end, idx in enumerate(order[::-1]):
        rank = n - rank_from_end
        val = p[idx] * n / rank
        prev = min(prev, val)
        q[idx] = prev
    return q.tolist()


def main() -> None:
    # ---------- 載入快取 ----------
    top = pd.read_pickle(CACHE_DIR / "top15_daily.pkl")
    px = pd.read_pickle(CACHE_DIR / "price_panel.pkl")
    top["trade_date"] = pd.to_datetime(top["trade_date"])
    px["trade_date"] = pd.to_datetime(px["trade_date"])
    sanity["top15_rows"] = len(top)
    sanity["price_rows"] = len(px)

    # 去重自檢（快取聲稱已去重，這裡再驗一次）
    dup_top = int(top.duplicated(["stock_id", "trade_date"]).sum())
    dup_px = int(px.duplicated(["stock_id", "trade_date"]).sum())
    sanity["dup_top15"] = dup_top
    sanity["dup_price"] = dup_px
    assert dup_top == 0 and dup_px == 0, "cache 內 stock-day 重複，中止"

    # volume 單位自檢：vwap 應與 close 同量級（volume=股）
    chk = px.dropna(subset=["vwap", "close"])
    chk = chk[chk["close"] > 0]
    ratio = (chk["vwap"] / chk["close"]).clip(0, 10)
    sanity["vwap_close_ratio_median"] = round(float(ratio.median()), 4)
    assert 0.9 < ratio.median() < 1.1, "vwap/close 量級異常，volume 單位存疑"

    # ---------- 交易日曆（全市場 trade_date 全集）----------
    cal = pd.DatetimeIndex(sorted(px["trade_date"].unique()))
    sanity["n_calendar_days"] = len(cal)
    next_cal = dict(zip(cal[:-1], cal[1:]))

    # 91.2% 坑自檢：以「長面板 groupby-shift 的下一列」對照全市場曆的次一交易日。
    # 本檢定的前瞻報酬全部改在「以全曆 reindex 的寬矩陣」上做（不依賴下一列假設），
    # 此數字純為回報資料稀疏程度。
    px_sorted = px.sort_values(["stock_id", "trade_date"])
    nxt_row = px_sorted.groupby("stock_id")["trade_date"].shift(-1)
    expected = px_sorted["trade_date"].map(next_cal)
    valid_mask = nxt_row.notna() & expected.notna()
    match_rate = float((nxt_row[valid_mask] == expected[valid_mask]).mean())
    sanity["next_row_is_next_tradeday_pct"] = round(100 * match_rate, 2)

    # ---------- join 自檢（分點 stock-day 有無價格列）----------
    merged_probe = top.merge(
        px[["stock_id", "trade_date", "close"]],
        on=["stock_id", "trade_date"], how="left",
    )
    sanity["join_top15_rows_before"] = len(top)
    sanity["join_top15_rows_with_price"] = int(merged_probe["close"].notna().sum())
    sanity["join_coverage_pct"] = round(
        100 * merged_probe["close"].notna().mean(), 2
    )

    # ---------- 寬矩陣（index=全曆, columns=stock）----------
    def pivot(df: pd.DataFrame, col: str) -> pd.DataFrame:
        return (
            df.pivot(index="trade_date", columns="stock_id", values=col)
            .reindex(cal)
        )

    C = pivot(px, "close")
    O = pivot(px, "open")
    V = pivot(px, "volume")
    VW = pivot(px, "vwap")

    branch_stocks = sorted(top["stock_id"].unique())
    N = pivot(top, "top15_net").reindex(columns=branch_stocks)      # 張
    HB = pivot(top, "n_buy_houses").reindex(columns=branch_stocks)
    HS = pivot(top, "n_sell_houses").reindex(columns=branch_stocks)

    # 分點缺列（該日無分點資料）視為淨額 0（家數差不補，缺值即濾網不通過）
    N0 = N.fillna(0.0)

    # ---------- 宇宙濾網（PIT：T 及以前）----------
    avg_vol20 = V.rolling(20, min_periods=20).mean()
    UNIV = (C >= UNIV_MIN_CLOSE) & (avg_vol20 > UNIV_MIN_AVGVOL)
    sanity["universe_stockdays"] = int(UNIV.sum().sum())
    sanity["universe_daily_median"] = int(UNIV.sum(axis=1).median())

    # ---------- cost20 ----------
    VWb = VW.reindex(columns=branch_stocks)
    contrib = N0 * VWb
    # net==0 的日子不需要 vwap；net!=0 但 vwap NaN → 讓 NaN 傳染（保守：整窗 cost20 未定義）
    contrib = contrib.where(~((N0 == 0.0) & contrib.isna()).fillna(False), 0.0)
    num20 = contrib.rolling(COST20_LOOKBACK, min_periods=COST20_LOOKBACK).sum()
    den20 = N0.rolling(COST20_LOOKBACK, min_periods=COST20_LOOKBACK).sum()   # 張
    vol20_lots = (V.fillna(0.0) / 1000.0).reindex(columns=branch_stocks) \
        .rolling(COST20_LOOKBACK, min_periods=COST20_LOOKBACK).sum()

    defined = (den20 > 0) & (den20 >= DENOM_FLOOR_FRAC * vol20_lots)
    # tape 護欄：20 日回看窗必須整段落在有效 tape（>=2024-07-01）
    first_valid_pos = cal.searchsorted(pd.Timestamp(WINDOW_START))
    first_cost_date = cal[first_valid_pos + COST20_LOOKBACK - 1]
    defined = defined & (defined.index.to_series() >= first_cost_date).values[:, None]
    cost20 = (num20 / den20).where(defined)
    sanity["first_cost20_date"] = str(first_cost_date.date())
    sanity["cost20_defined_stockdays"] = int(cost20.notna().sum().sum())

    # ---------- 前提濾網：5 日均家數差 < 0 ----------
    house_diff = (HB - HS).rolling(
        HOUSE_DIFF_WINDOW, min_periods=HOUSE_DIFF_WINDOW
    ).mean()
    FILT = house_diff < 0

    # ---------- 事件 ----------
    Cb = C.reindex(columns=branch_stocks)
    above = Cb > cost20
    below = Cb < cost20
    both_defined = cost20.notna() & cost20.shift(1).notna() \
        & Cb.notna() & Cb.shift(1).notna()
    cross_up = below.shift(1).fillna(False) & above & both_defined
    cross_dn = above.shift(1).fillna(False) & below & both_defined

    UNIVb = UNIV.reindex(columns=branch_stocks).fillna(False)
    ev_up = cross_up & FILT & UNIVb
    ev_dn = cross_dn & FILT & UNIVb

    in_window = (cal >= pd.Timestamp(WINDOW_START)) & (cal <= pd.Timestamp(WINDOW_END))
    in_valid_tape = in_window & (cal <= pd.Timestamp(TAPE_VALID_END))
    sanity["events_up_raw"] = int(ev_up.values[in_window].sum())
    sanity["events_dn_raw"] = int(ev_dn.values[in_window].sum())
    sanity["events_up_degraded_tape_excluded"] = int(
        ev_up.values[in_window & ~in_valid_tape].sum()
    )
    sanity["events_dn_degraded_tape_excluded"] = int(
        ev_dn.values[in_window & ~in_valid_tape].sum()
    )
    ev_up = ev_up & pd.Series(in_valid_tape, index=cal).values[:, None]
    ev_dn = ev_dn & pd.Series(in_valid_tape, index=cal).values[:, None]

    # ---------- 前瞻報酬（全曆位置制，不依賴下一列假設）----------
    O_pos = O.where(O > 0)     # open<=0 視為缺值（避免除零 → inf 污染同日均值）
    C_pos = C.where(C > 0)
    sanity["open_nonpositive_cells"] = int(((O <= 0) & O.notna()).sum().sum())
    entry_open = O_pos.shift(-1)  # open(T+1)：全曆的次一交易日；停牌 → NaN 自然剔除
    results = {}
    ev_any = (ev_up | ev_dn)

    # 事件股在 T+1 是否真的有開盤價（停牌/下市風險自檢）
    eob = entry_open.reindex(columns=branch_stocks)
    n_ev = int(ev_any.sum().sum())
    n_ev_no_entry = int((ev_any & eob.isna()).sum().sum())
    sanity["events_total_valid_tape"] = n_ev
    sanity["events_missing_entry_open"] = n_ev_no_entry

    decile_rows = []
    for h in HOLDS:
        exit_close = C_pos.shift(-h)
        ret = (exit_close / entry_open - 1.0) * 100.0          # 全宇宙，%

        # 同日全宇宙均值（排除當日事件股）
        ev_any_full = ev_any.reindex(columns=C.columns).fillna(False)
        univ_ret = ret.where(UNIV & ~ev_any_full)
        day_mean = univ_ret.mean(axis=1)
        excess = ret.sub(day_mean, axis=0)

        retb = ret.reindex(columns=branch_stocks)
        excb = excess.reindex(columns=branch_stocks)
        for direction, ev in (("up", ev_up), ("dn", ev_dn)):
            mask = ev.values
            dates_idx, stock_idx = np.where(mask)
            df = pd.DataFrame({
                "date": cal[dates_idx],
                "stock": np.array(branch_stocks)[stock_idx],
                "raw": retb.values[mask],
                "excess": excb.values[mask],
                "univ_mean": day_mean.reindex(cal).values[dates_idx],
            }).dropna(subset=["raw", "excess"])
            results[(direction, h)] = df

        # ---------- 描述性附錄：rel 十分位（同條件母體，含前提濾網＋宇宙）----------
        rel = ((Cb - cost20) / cost20).where(
            FILT & UNIVb & pd.Series(in_valid_tape, index=cal).values[:, None]
        )
        if h == 5:
            for hh, r_mat in (("1", (C_pos.shift(-1) / entry_open - 1.0) * 100.0),
                              ("5", ret)):
                r_ex = r_mat.sub(
                    r_mat.where(UNIV).mean(axis=1), axis=0
                ).reindex(columns=branch_stocks)
                rel_long = rel.stack().rename("rel").reset_index()
                rel_long.columns = ["date", "stock", "rel"]
                rex_long = r_ex.stack().rename("rex").reset_index()
                rex_long.columns = ["date", "stock", "rex"]
                dd = rel_long.merge(rex_long, on=["date", "stock"], how="inner")
                # 每日橫斷面十分位（當日樣本 <30 檔捨棄）
                def _q(g):
                    if len(g) < 30:
                        return pd.Series(np.nan, index=g.index)
                    return pd.qcut(g, 10, labels=False, duplicates="drop")
                dd["decile"] = dd.groupby("date")["rel"].transform(_q)
                dec = dd.dropna(subset=["decile"]).groupby("decile")["rex"] \
                    .agg(["mean", "count"])
                for d, row in dec.iterrows():
                    decile_rows.append({
                        "h": hh, "decile": int(d),
                        "mean_excess_pct": round(float(row["mean"]), 4),
                        "n": int(row["count"]),
                    })

    # ---------- 統計 ----------
    cells = []
    pvals = []
    cell_order = []
    for direction in ("up", "dn"):
        for h in HOLDS:
            df = results[(direction, h)]
            cell_id = f"{direction}_h{h}"
            n_events = len(df)
            mean_raw = float(df["raw"].mean()) if n_events else float("nan")
            mean_exc = float(df["excess"].mean()) if n_events else float("nan")
            baseline = float(df["univ_mean"].mean()) if n_events else float("nan")
            # calendar-time：日聚合均值 → NW lag=h
            daily = df.groupby("date")["excess"].mean().sort_index()
            m_daily, t_nw, n_days = nw_tstat(daily.values, lag=h)
            p = two_sided_p(t_nw)
            # 不重疊子抽樣（每檔股票事件間隔 >= h 個交易日）附註
            sub = []
            for _, g in df.sort_values("date").groupby("stock"):
                last_pos = -10**9
                for _, r in g.iterrows():
                    pos = cal.searchsorted(r["date"])
                    if pos - last_pos >= h:
                        sub.append(r["excess"])
                        last_pos = pos
            sub = np.asarray(sub, dtype=float)
            t_sub = (
                float(sub.mean() / (sub.std(ddof=1) / np.sqrt(len(sub))))
                if len(sub) > 2 and sub.std(ddof=1) > 0 else float("nan")
            )
            # holdout
            hold = df[df["date"] >= pd.Timestamp(HOLDOUT_START)]
            hold_daily = hold.groupby("date")["excess"].mean().sort_index()
            hm, ht, hn = nw_tstat(hold_daily.values, lag=h)
            hold_mean = float(hold["excess"].mean()) if len(hold) else float("nan")
            cells.append({
                "cell_id": cell_id,
                "n_events": n_events,
                "n_event_days": int(n_days),
                "mean_raw_pct": round(mean_raw, 4),
                "baseline_raw_pct": round(baseline, 4),
                "mean_excess_pct": round(mean_exc, 4),
                "mean_excess_daily_pct": round(float(m_daily), 4),
                "t_stat": round(float(t_nw), 3),
                "p": p,
                "t_nonoverlap_sub": round(t_sub, 3) if not np.isnan(t_sub) else None,
                "n_nonoverlap": int(len(sub)),
                "net_after_cost_pct": round(mean_exc - COST_RT_PCT, 4),
                "holdout_n": int(len(hold)),
                "holdout_mean_pct": round(hold_mean, 4) if len(hold) else None,
                "holdout_t": round(float(ht), 3) if not np.isnan(ht) else None,
            })
            pvals.append(p if not np.isnan(p) else 1.0)
            cell_order.append(cell_id)

    qvals = bh_fdr(pvals)
    for c, q in zip(cells, qvals):
        c["q_bh"] = round(float(q), 4)
        c["passes"] = bool(
            abs(c["t_stat"]) >= 3.0 and q < 0.05
            and (
                (c["cell_id"].startswith("up") and c["mean_excess_pct"] > 0
                 and c["net_after_cost_pct"] > 0)
                or (c["cell_id"].startswith("dn") and c["mean_excess_pct"] < 0)
            )
        )

    # 混淆檢查：上穿與下穿去均值後同號 → 混淆非方向
    sign_up5 = np.sign(next(c for c in cells if c["cell_id"] == "up_h5")["mean_excess_pct"])
    sign_dn5 = np.sign(next(c for c in cells if c["cell_id"] == "dn_h5")["mean_excess_pct"])
    sign_up10 = np.sign(next(c for c in cells if c["cell_id"] == "up_h10")["mean_excess_pct"])
    sign_dn10 = np.sign(next(c for c in cells if c["cell_id"] == "dn_h10")["mean_excess_pct"])
    sanity["same_sign_confound_h5"] = bool(sign_up5 == sign_dn5)
    sanity["same_sign_confound_h10"] = bool(sign_up10 == sign_dn10)

    out = {
        "cells": cells,
        "deciles": decile_rows,
        "sanity": sanity,
    }
    print(json.dumps(out, indent=2, default=str))
    (CACHE_DIR.parent / "B_results.json").write_text(
        json.dumps(out, indent=2, default=str)
    )


if __name__ == "__main__":
    main()
