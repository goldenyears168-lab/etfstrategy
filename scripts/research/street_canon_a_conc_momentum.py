"""H-STREET-A：集中度增幅・雙尺度共振（chip-street-canon A 線）。

規格（config/research.yaml topic chip-street-canon 預註記，不得改）：
- conc5(T) = 過去5日 top15_net 合計 ÷ 過去5日 volume 合計；conc20 同理 20 日。
- 事件：conc5 連續遞增 >= N 日（N∈{2,3}）且 conc20(T) > conc20(T-5)（雙尺度共振）。
- 進出場：open(T+1) 進、close(T+h) 出，h∈{2,5,10}。共 6 格。
- 窗 2024-07-01~2026-08-26；holdout 2026-01-01 之後另報。
  ⚠️ 分點 tape 2026-07-17 起退化為子集視角（cache/README.md），事件訊號日
  截斷於 2026-07-16；2026-07-17+ 段另行標註、不入檢定。
- 宇宙：close>=10 且 20 日均量 > 300,000 股（PIT，T 日及以前）。
- 主口徑：橫斷面去均值（減同日「事件宇宙外」的全宇宙均值）；附無條件原始均值
  與全宇宙基準；Newey-West lag>=h（calendar-time 日組合序列）；BH-FDR(6 格)；
  扣 30bps 來回攤入持有期。
- 對照：反向事件（連續遞減＋conc20 下行）同號檢查；描述性對照＝以 conc5 水位
  top 分位取代增幅。

用法::

    PYTHONPATH=src .venv/bin/python scripts/research/street_canon_a_conc_momentum.py
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import statsmodels.api as sm

ROOT = Path(__file__).resolve().parents[2]
CACHE = ROOT / "reports/research/chip-street-canon/cache"
REPORT_DIR = ROOT / "reports/research/chip-street-canon"

WINDOW_START = "2024-07-01"
WINDOW_END = "2026-08-26"
TAPE_VALID_END = "2026-07-16"   # 分點 tape 2026-07-17 起退化（cache/README.md）
HOLDOUT_START = "2026-01-01"
COST_PCT = 0.30                 # 30bps 來回，事件制攤入持有期
N_LIST = (2, 3)
H_LIST = (2, 5, 10)
LEVEL_Q = 0.90                  # 描述性對照：conc5 水位同日 top 10% 分位


def nw_t(series: np.ndarray, lag: int) -> tuple[float, float, float]:
    """Newey-West (HAC) mean / t / two-sided p of a 1-D series."""
    y = np.asarray(series, dtype=float)
    y = y[~np.isnan(y)]
    if len(y) < 8:
        return (float(np.mean(y)) if len(y) else np.nan, np.nan, np.nan)
    x = np.ones_like(y)
    res = sm.OLS(y, x).fit(cov_type="HAC", cov_kwds={"maxlags": lag})
    return float(res.params[0]), float(res.tvalues[0]), float(res.pvalues[0])


def bh_fdr(pvals: list[float]) -> list[float]:
    p = np.asarray(pvals, dtype=float)
    n = len(p)
    order = np.argsort(p)
    q = np.empty(n)
    prev = 1.0
    for rank_idx in range(n - 1, -1, -1):
        i = order[rank_idx]
        val = p[i] * n / (rank_idx + 1)
        prev = min(prev, val)
        q[i] = prev
    return q.tolist()


def consecutive_true(mask: pd.DataFrame) -> pd.DataFrame:
    """每格＝該欄目前連續 True 的長度（False 歸零）。"""
    cs = mask.cumsum()
    return (cs - cs.where(~mask).ffill().fillna(0)).astype(int)


def daily_portfolio(ret: pd.DataFrame, event: pd.DataFrame, univ_valid: pd.DataFrame):
    """回傳 (事件日均原始報酬, 事件外宇宙日均基準, 每日事件數) 之日序列。

    基準＝同日全宇宙（通過濾網且報酬有效）扣除事件股後的均值 —— 主口徑分母。
    """
    ev = event & univ_valid                       # 事件必在宇宙內且報酬有效
    r = ret.where(univ_valid)
    sum_u = r.sum(axis=1)
    cnt_u = univ_valid.sum(axis=1)
    sum_e = r.where(ev).sum(axis=1)
    cnt_e = ev.sum(axis=1)
    raw_mean = (sum_e / cnt_e).where(cnt_e > 0)
    base = ((sum_u - sum_e) / (cnt_u - cnt_e)).where((cnt_u - cnt_e) > 0)
    return raw_mean, base, cnt_e


def main() -> None:
    sanity: dict[str, object] = {}

    top = pd.read_pickle(CACHE / "top15_daily.pkl")
    px = pd.read_pickle(CACHE / "price_panel.pkl")
    sanity["top15_rows"] = len(top)
    sanity["price_rows"] = len(px)
    sanity["dup_top15"] = int(top.duplicated(["stock_id", "trade_date"]).sum())
    sanity["dup_price"] = int(px.duplicated(["stock_id", "trade_date"]).sum())
    assert sanity["dup_top15"] == 0 and sanity["dup_price"] == 0, "cache 有重複 stock-day"

    # ---- 交易日曆＝全市場 trade_date 全集；面板 reindex 到日曆，shift 即交易日對齊 ----
    cal = np.sort(px["trade_date"].unique())
    sanity["n_calendar_days"] = int(len(cal))

    # 91.2% 坑實測：原始（未上日曆）面板中「同股下一列＝次一交易日」比率
    nxt = dict(zip(cal[:-1], cal[1:]))
    px_sorted = px.sort_values(["stock_id", "trade_date"])
    same_stock = px_sorted["stock_id"].shift(-1) == px_sorted["stock_id"]
    next_ok = px_sorted["trade_date"].map(nxt) == px_sorted["trade_date"].shift(-1)
    frac = float((next_ok & same_stock).sum() / same_stock.sum())
    sanity["raw_nextrow_is_next_tradeday_pct"] = round(frac * 100, 2)

    # join 覆蓋（列數回報；INNER JOIN 靜默坑）
    merged = top.merge(px[["stock_id", "trade_date"]], on=["stock_id", "trade_date"], how="inner")
    sanity["branch_rows_with_price"] = len(merged)
    sanity["branch_price_join_pct"] = round(100 * len(merged) / len(top), 2)

    piv = lambda df, col: df.pivot(index="trade_date", columns="stock_id", values=col).reindex(cal)
    open_m = piv(px, "open")
    close_m = piv(px, "close")
    vol_lots = piv(px, "volume") / 1000.0           # 股→張，與 top15_net(張) 同單位
    vol_lots = vol_lots.where(vol_lots > 0)
    top15 = piv(top, "top15_net").reindex(columns=open_m.columns)

    # ---- conc5 / conc20（分子分母皆須 5/20 日齊備） ----
    num5 = top15.rolling(5, min_periods=5).sum()
    den5 = vol_lots.rolling(5, min_periods=5).sum()
    conc5 = (num5 / den5).where(den5 > 0)
    num20 = top15.rolling(20, min_periods=20).sum()
    den20 = vol_lots.rolling(20, min_periods=20).sum()
    conc20 = (num20 / den20).where(den20 > 0)

    # ---- 事件成分 ----
    d = conc5.diff()
    inc = (d > 0) & conc5.notna() & conc5.shift(1).notna()
    dec = (d < 0) & conc5.notna() & conc5.shift(1).notna()
    streak_up = consecutive_true(inc)
    streak_dn = consecutive_true(dec)
    reso_up = (conc20 - conc20.shift(5)) > 0
    reso_dn = (conc20 - conc20.shift(5)) < 0

    # 宇宙（PIT：只用 T 及以前）
    vol20 = vol_lots.fillna(0).rolling(20, min_periods=20).mean()
    univ = (close_m >= 10) & (vol20 > 300) & close_m.notna()
    sanity["universe_mean_stocks_per_day"] = int(
        univ.loc[(univ.index >= WINDOW_START) & (univ.index <= TAPE_VALID_END)].sum(axis=1).mean()
    )

    # 訊號日窗（tape 有效段）
    idx = conc5.index
    in_win = (idx >= WINDOW_START) & (idx <= TAPE_VALID_END)
    in_hold = (idx >= HOLDOUT_START) & (idx <= TAPE_VALID_END)
    in_degraded = idx > TAPE_VALID_END
    win_mask = pd.Series(in_win, index=idx)
    hold_mask = pd.Series(in_hold, index=idx)

    # 前瞻報酬：open(T+1) 進、close(T+h) 出（日曆網格上 shift＝交易日 shift；
    # T+1 停牌/缺列 → open NaN → 事件自動剔除並計數）
    open_next = open_m.shift(-1).where(open_m.shift(-1) > 0)
    ret_h = {h: (close_m.shift(-h) / open_next - 1.0) for h in H_LIST}

    # conc5 水位同日分位（宇宙內），供描述性對照
    conc5_u = conc5.where(univ)
    lvl_rank = conc5_u.rank(axis=1, pct=True)

    cells = []
    extra = {"reverse": [], "level": [], "degraded": [], "missing_ret": []}

    for N in N_LIST:
        ev_sig = (streak_up >= N) & reso_up & univ
        rv_sig = (streak_dn >= N) & reso_dn & univ
        lv_sig = (lvl_rank >= LEVEL_Q) & reso_up & univ  # 增幅→水位替換，其餘同
        for h in H_LIST:
            r = ret_h[h]
            valid = univ & r.notna()

            ev = ev_sig.mul(win_mask, axis=0)
            n_signal = int(ev.sum().sum())
            n_missing = int((ev & ~r.notna()).sum().sum())
            extra["missing_ret"].append({"cell": f"N{N}_h{h}", "signals": n_signal,
                                         "dropped_no_ret": n_missing})

            raw, base, cnt = daily_portfolio(r, ev, valid)
            exc = (raw - base).dropna()
            n_events = int(cnt.sum())
            mean_exc, t_exc, p_exc = nw_t(exc.values, lag=h)
            raw_days = raw.dropna()
            mean_raw = float(raw_days.mean()) if len(raw_days) else np.nan
            base_days = base[raw.notna()].dropna()
            mean_base = float(base_days.mean()) if len(base_days) else np.nan

            hold_exc = exc[hold_mask.reindex(exc.index).fillna(False)]
            h_mean, h_t, _ = nw_t(hold_exc.values, lag=h)

            # 事件層（事件加權）均值供對照
            ev_ret = r.where(ev & valid)
            ew_raw = float(ev_ret.stack().mean()) if n_events else np.nan

            cells.append({
                "cell_id": f"N{N}_h{h}",
                "N": N, "h": h,
                "n_events": n_events,
                "n_event_days": int(len(exc)),
                "mean_excess_pct": round(mean_exc * 100, 4),
                "t_stat": round(t_exc, 2) if np.isfinite(t_exc) else np.nan,
                "p_two_sided": p_exc,
                "mean_raw_pct": round(mean_raw * 100, 4),
                "eventweighted_raw_pct": round(ew_raw * 100, 4) if np.isfinite(ew_raw) else np.nan,
                "baseline_raw_pct": round(mean_base * 100, 4),
                "net_after_cost_pct": round(mean_exc * 100 - COST_PCT, 4),
                "holdout_mean_pct": round(h_mean * 100, 4) if np.isfinite(h_mean) else np.nan,
                "holdout_t": round(h_t, 2) if np.isfinite(h_t) else np.nan,
                "holdout_days": int(len(hold_exc)),
            })

            # 反向組（混淆檢查）
            rvm = rv_sig.mul(win_mask, axis=0)
            rraw, rbase, rcnt = daily_portfolio(r, rvm, valid)
            rexc = (rraw - rbase).dropna()
            r_mean, r_t, _ = nw_t(rexc.values, lag=h)
            extra["reverse"].append({
                "cell": f"N{N}_h{h}", "n_events": int(rcnt.sum()),
                "mean_excess_pct": round(r_mean * 100, 4) if np.isfinite(r_mean) else np.nan,
                "t": round(r_t, 2) if np.isfinite(r_t) else np.nan,
            })

            # 描述性：水位 top 分位替換增幅（N 對水位無意義 → 只在 N=2 迴圈算一次）
            if N == N_LIST[0]:
                lvm = lv_sig.mul(win_mask, axis=0)
                lraw, lbase, lcnt = daily_portfolio(r, lvm, valid)
                lexc = (lraw - lbase).dropna()
                l_mean, l_t, _ = nw_t(lexc.values, lag=h)
                both = int((lvm & ev_sig.mul(win_mask, axis=0)).sum().sum())
                extra["level"].append({
                    "cell": f"level_q{int(LEVEL_Q*100)}_h{h}", "n_events": int(lcnt.sum()),
                    "mean_excess_pct": round(l_mean * 100, 4) if np.isfinite(l_mean) else np.nan,
                    "t": round(l_t, 2) if np.isfinite(l_t) else np.nan,
                    "overlap_with_N2_events": both,
                })

            # 退化段（2026-07-17+）描述性標註，不入檢定
            dgm = ev_sig.mul(pd.Series(in_degraded, index=idx), axis=0)
            draw, dbase, dcnt = daily_portfolio(r, dgm, valid)
            dexc = (draw - dbase).dropna()
            extra["degraded"].append({
                "cell": f"N{N}_h{h}", "n_events": int(dcnt.sum()),
                "mean_excess_pct": round(float(dexc.mean()) * 100, 4) if len(dexc) else np.nan,
            })

    # BH-FDR（線內 6 格）
    qs = bh_fdr([c["p_two_sided"] for c in cells])
    for c, q in zip(cells, qs):
        c["q_bh"] = round(q, 4)
        c["passes"] = bool(c["t_stat"] >= 3.0 and q < 0.05 and c["mean_excess_pct"] > 0)

    out = {"cells": cells, "extra": extra, "sanity": sanity}
    (REPORT_DIR / "A_results.json").write_text(
        json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(json.dumps(out, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
