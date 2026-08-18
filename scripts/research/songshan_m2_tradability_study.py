#!/usr/bin/env python3
"""songshan_m2 · 9217 跟單訊號的「執行載具」與「盤前可交易性」檢定（純研究 · DB 唯讀）.

動機（真實事故）：data/order/songshan_copytrade_ledger.json 只有 4 筆、全是 2492，
全部被券商以「全額處置股請至(預收款券)補足預收款」退件。此袖從未成交過一次，
而下單層至今沒有任何盤前可交易性過濾。

H-B1：個股期貨作為執行載具，能否繞開現股的處置／全額交割限制？
H-B2：盤前可交易性過濾能否事先剔除「吃不到的訊號」？

輸入：
  reports/research/branch-footprint-screen/songshan_m2/mother_set_trades.csv
  reports/research/branch-footprint-screen/songshan_m2/futures_daily_cache.json
  reports/research/branch-footprint-screen/songshan_m2/disposition_history.json
  reports/research/branch-footprint-screen/dayflip_gapup_short/stock_futures_universe.json

輸出：
  reports/research/branch-footprint-screen/songshan_m2/tradability_report.json
  reports/research/branch-footprint-screen/songshan_m2/tradability_events.csv

用法：
  PYTHONPATH=src .venv/bin/python scripts/research/songshan_m2_tradability_study.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from scipy import stats  # noqa: E402

from stock_db import DEFAULT_DB_PATH  # noqa: E402
from stock_db.connection import connect_ro  # noqa: E402

BASE = ROOT / "reports" / "research" / "branch-footprint-screen"
OUT_DIR = BASE / "songshan_m2"
SOURCE = "finmind"

# L1H7 協議
COST_EQ = 0.003      # 現股 30bps
COST_FUT = 0.0005    # 期貨 5bps 來回（FROZEN_SPEC_V1 execution.cost_bps_round_trip）
HOLD = 7
BETA = 1.15
BENCH = "IX0001"

# 期貨流動性門檻（FROZEN_SPEC_V1 universe.liquidity_filter）
FUT_ADV_WINDOW = 20
FUT_MIN_LOTS = 800

# 下單層現況（config/order.yaml songshan-copytrade）
BUDGET_TWD = 100_000

# 現股流動性門檻候選（T0 收盤前 20 日均成交金額，NTD）
ADV_FLOORS = (30_000_000, 50_000_000, 100_000_000)

LIMIT_UP_PCT = 0.095  # 台股漲停 +10%，留 tick 捨入餘裕


# ---------------------------------------------------------------- helpers
def full_stats(vals_pct: pd.Series, label: str) -> dict:
    vals = pd.Series(vals_pct).dropna().to_numpy() / 100.0
    n = len(vals)
    out = {"label": label, "n": n}
    if n == 0:
        return out
    out.update(
        {
            "mean_pct": round(float(np.mean(vals)) * 100, 3),
            "median_pct": round(float(np.median(vals)) * 100, 3),
            "win_rate_pct": round(float((vals > 0).mean()) * 100, 1),
        }
    )
    if n >= 2 and np.std(vals) > 0:
        t, p = stats.ttest_1samp(vals, 0)
        out["t_stat"] = round(float(t), 3)
        out["t_p"] = round(float(p), 4)
        try:
            w, wp = stats.wilcoxon(vals)
            out["wilcoxon_p"] = round(float(wp), 4)
        except ValueError:
            out["wilcoxon_p"] = None
    if n < 15:
        out["caveat"] = "樣本不足(n<15)"
    return out


def load_bars(conn, sid: str) -> pd.DataFrame:
    df = pd.read_sql_query(
        """
        SELECT trade_date, open, high, low, close, volume, amount
        FROM stock_daily_bars
        WHERE stock_id=? AND source=? AND trade_date BETWEEN '2024-01-01' AND '2026-08-31'
          AND close>0
        ORDER BY trade_date
        """,
        conn,
        params=(sid, SOURCE),
    )
    if df.empty:
        return df
    df["turnover"] = df["amount"].fillna(df["volume"] * df["close"])
    df["prev_close"] = df["close"].shift(1)
    df["adv20"] = df["turnover"].rolling(20, min_periods=10).mean()
    return df


def load_ix(conn) -> pd.DataFrame:
    df = pd.read_sql_query(
        """
        SELECT date AS trade_date, open, close, source FROM daily_bars
        WHERE code=? AND date BETWEEN '2024-01-01' AND '2026-08-31' AND open>0 AND close>0
        ORDER BY date,
          CASE source WHEN 'yahoo' THEN 0 WHEN 'tej' THEN 1 WHEN 'finmind' THEN 2 ELSE 3 END
        """,
        conn,
        params=(BENCH,),
    )
    return df.drop_duplicates("trade_date").reset_index(drop=True)


def disposition_index(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))["episodes"]


def disp_state(eps: list[dict], sid: str, day: str) -> dict:
    """該股在 day 當下生效中的處置狀態（取最嚴格者）。"""
    active = [e for e in eps if e["sid"] == sid and e["p_start"] <= day <= e["p_end"]]
    if not active:
        return {"active": False, "prefund": "none", "types": [], "market": None}
    rank = {"prefund_blanket": 2, "prefund_ge10lots": 1, "none": 0}
    worst = max(active, key=lambda e: rank.get(e["prefund"], 0))
    return {
        "active": True,
        "prefund": worst["prefund"],
        "types": sorted({e["disp_type"] for e in active}),
        "market": worst["market"],
        "windows": [[e["p_start"], e["p_end"], e["disp_type"], e["prefund"]] for e in active],
    }


def fut_path_return(fut: dict, entry_date: str, hold: int = HOLD) -> dict | None:
    """期貨 L1H7：T+1 開盤進、第 hold 個交易日收盤出。近月序列串接，換月以當日 open→close 接。"""
    days = sorted(fut)
    idx = {d: i for i, d in enumerate(days)}
    if entry_date not in idx:
        return None
    i0 = idx[entry_date]
    path = days[i0 : i0 + hold]
    if len(path) < hold:
        return None
    mult = 1.0
    rolls = 0
    d0 = path[0]
    r0 = fut[d0]
    if r0["o"] <= 0 or r0["c"] <= 0:
        return None
    mult *= r0["c"] / r0["o"]
    prev = r0
    for d in path[1:]:
        cur = fut[d]
        if cur["c"] <= 0:
            return None
        if cur["cd"] == prev["cd"] and prev["c"] > 0:
            mult *= cur["c"] / prev["c"]
        else:
            rolls += 1
            if cur["o"] <= 0:
                return None
            mult *= cur["c"] / cur["o"]
        prev = cur
    return {
        "entry_date": d0,
        "entry_open": r0["o"],
        "exit_date": path[-1],
        "exit_close": prev["c"],
        "gross_pct": round((mult - 1) * 100, 3),
        "n_rolls": rolls,
    }


def fut_adv(fut: dict, asof: str, window: int = FUT_ADV_WINDOW, key: str = "v_front2") -> float | None:
    """T0 收盤（含當日）往前 window 日的期貨均量（口）。PIT：不看未來。"""
    days = [d for d in sorted(fut) if d <= asof]
    if len(days) < window:
        return None
    return float(np.mean([fut[d][key] for d in days[-window:]]))


# ---------------------------------------------------------------- main
def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    trades = pd.read_csv(OUT_DIR / "mother_set_trades.csv", dtype={"stock_id": str})
    futmap = json.loads((BASE / "dayflip_gapup_short" / "stock_futures_universe.json").read_text())
    fut_universe = futmap["map"]
    fut_cache = json.loads((OUT_DIR / "futures_daily_cache.json").read_text())
    eps = disposition_index(OUT_DIR / "disposition_history.json")

    conn = connect_ro(DEFAULT_DB_PATH)
    ix = load_ix(conn)
    ix_idx = {r.trade_date: (r.open, r.close) for r in ix.itertuples()}
    ix_days = list(ix["trade_date"])

    bars_cache: dict[str, pd.DataFrame] = {}
    rows: list[dict] = []

    for t in trades.itertuples(index=False):
        sid = t.stock_id
        if sid not in bars_cache:
            bars_cache[sid] = load_bars(conn, sid)
        b = bars_cache[sid]
        bidx = {d: i for i, d in enumerate(b["trade_date"])}
        sig, ed, xd = t.signal_date, t.entry_date, t.exit_date
        ei = bidx.get(ed)
        si = bidx.get(sig)
        er = b.iloc[ei]

        # --- 盤前可交易性（只用 <= T0 收盤 + T+1 開盤當下可觀察的資訊）---
        d_t0 = disp_state(eps, sid, sig)
        d_t1 = disp_state(eps, sid, ed)
        adv20_t0 = float(b.iloc[si]["adv20"]) if si is not None and not pd.isna(b.iloc[si]["adv20"]) else None

        prev_close = float(er["prev_close"]) if not pd.isna(er["prev_close"]) else None
        o, h, lo, c = float(er["open"]), float(er["high"]), float(er["low"]), float(er["close"])
        gap_pct = (o / prev_close - 1) * 100 if prev_close else None
        # 漲停鎖死：開盤即漲停 且 全日未跌破開盤價（low==open）
        open_at_limit = bool(prev_close and o >= prev_close * (1 + LIMIT_UP_PCT))
        locked_all_day = bool(open_at_limit and lo >= o - 1e-9)
        # 較寬鬆：開盤漲停但盤中有跌破 → 仍難用開盤價成交但不至於完全買不到
        open_limit_touchable = bool(open_at_limit and not locked_all_day)

        shares = int(BUDGET_TWD / o) if o > 0 else 0
        is_odd_lot = shares < 1000
        lot_cost = o * 1000

        # --- 期貨側 ---
        in_fut_universe = sid in fut_universe
        fut = fut_cache.get(sid) or {}
        fut_days = sorted(fut)
        fut_exists_at_t0 = bool(fut_days and fut_days[0] <= sig)
        adv_lots = fut_adv(fut, sig) if fut_exists_at_t0 else None
        fut_liquid = bool(adv_lots is not None and adv_lots >= FUT_MIN_LOTS)
        fr = fut_path_return(fut, ed) if fut_exists_at_t0 else None

        # 基差（期貨近月收盤 vs 現股收盤）
        def basis(day: str) -> float | None:
            if day not in fut or day not in bidx:
                return None
            sc = float(b.iloc[bidx[day]]["close"])
            fc = fut[day]["c"]
            return (fc / sc - 1) * 100 if sc > 0 else None

        b_entry = basis(ed)
        b_exit = basis(xd)

        # 期貨 beta 調整報酬
        r_fut_adj = None
        r_fut_net = None
        if fr:
            bo, bc = None, None
            if fr["entry_date"] in ix_idx:
                bo = ix_idx[fr["entry_date"]][0]
            if fr["exit_date"] in ix_idx:
                bc = ix_idx[fr["exit_date"]][1]
            if bo and bc:
                r_fut_net = fr["gross_pct"] / 100 - COST_FUT
                r_ix = bc / bo - 1
                r_fut_adj = round((r_fut_net - BETA * r_ix) * 100, 3)
                r_fut_net = round(r_fut_net * 100, 3)

        rows.append(
            {
                "stock_id": sid,
                "signal_date": sig,
                "entry_date": ed,
                "exit_date": xd,
                "entry_open": o,
                "r_pct": t.r_pct,
                "r_adj_pct": t.r_adj_pct,
                # 可交易性
                "disp_t0_active": d_t0["active"],
                "disp_t0_prefund": d_t0["prefund"],
                "disp_t1_active": d_t1["active"],
                "disp_t1_prefund": d_t1["prefund"],
                "disp_t1_types": "|".join(d_t1["types"]),
                "disp_market": d_t1["market"],
                "gap_open_pct": round(gap_pct, 2) if gap_pct is not None else None,
                "open_at_limit_up": open_at_limit,
                "locked_limit_up_all_day": locked_all_day,
                "open_limit_touchable": open_limit_touchable,
                "adv20_t0_ntd": round(adv20_t0, 0) if adv20_t0 else None,
                "budget_shares": shares,
                "is_odd_lot": is_odd_lot,
                "one_lot_cost_ntd": round(lot_cost, 0),
                "notional_pct_of_adv": round(BUDGET_TWD / adv20_t0 * 100, 4) if adv20_t0 else None,
                # 期貨
                "in_fut_universe": in_fut_universe,
                "fut_exists_at_t0": fut_exists_at_t0,
                "fut_adv20_lots_front2": round(adv_lots, 0) if adv_lots else None,
                "fut_liquid_ge800": fut_liquid,
                "fut_r_net_pct": r_fut_net,
                "fut_r_adj_pct": r_fut_adj,
                "fut_n_rolls": fr["n_rolls"] if fr else None,
                "basis_entry_pct": round(b_entry, 3) if b_entry is not None else None,
                "basis_exit_pct": round(b_exit, 3) if b_exit is not None else None,
                "basis_drag_pct": round(b_exit - b_entry, 3)
                if (b_entry is not None and b_exit is not None)
                else None,
            }
        )

    df = pd.DataFrame(rows)
    df.to_csv(OUT_DIR / "tradability_events.csv", index=False)

    rep: dict = {
        "generated_for": "songshan_m2 · 執行載具與可交易性",
        "db": str(DEFAULT_DB_PATH),
        "n_events": int(len(df)),
        "protocol": f"L1H7 · 現股cost {COST_EQ*1e4:.0f}bps · 期貨cost {COST_FUT*1e4:.0f}bps · beta {BETA} · bench {BENCH}",
        "baseline_stock": full_stats(df["r_adj_pct"], "baseline_all"),
    }

    # ---------------- H-B1 ----------------
    hb1: dict = {}
    hb1["universe_coverage"] = {
        "n_events": int(len(df)),
        "n_events_in_fut_universe_20260708": int(df["in_fut_universe"].sum()),
        "pct_in_fut_universe": round(df["in_fut_universe"].mean() * 100, 1),
        "n_events_fut_existed_at_signal_PIT": int(df["fut_exists_at_t0"].sum()),
        "pct_fut_existed_PIT": round(df["fut_exists_at_t0"].mean() * 100, 1),
        "n_events_pass_liquidity_ge800lots": int(df["fut_liquid_ge800"].sum()),
        "pct_pass_liquidity": round(df["fut_liquid_ge800"].mean() * 100, 1),
        "n_distinct_stocks": int(df["stock_id"].nunique()),
        "n_stocks_in_fut_universe": int(df[df["in_fut_universe"]]["stock_id"].nunique()),
        "stocks_without_futures": sorted(df[~df["in_fut_universe"]]["stock_id"].unique().tolist()),
    }

    # 反向檢查：有無期貨兩組的現股 L1H7
    has = df[df["in_fut_universe"]]
    non = df[~df["in_fut_universe"]]
    mw = stats.mannwhitneyu(has["r_adj_pct"], non["r_adj_pct"], alternative="two-sided") if len(has) and len(non) else None
    hb1["reverse_check_has_vs_no_futures"] = {
        "stock_leg_has_futures": full_stats(has["r_adj_pct"], "has_futures"),
        "stock_leg_no_futures": full_stats(non["r_adj_pct"], "no_futures"),
        "mannwhitney_p": round(float(mw.pvalue), 4) if mw else None,
        "entry_price_median_has": round(float(has["entry_open"].median()), 1) if len(has) else None,
        "entry_price_median_no": round(float(non["entry_open"].median()), 1) if len(non) else None,
        "adv20_median_has_ntd": round(float(has["adv20_t0_ntd"].median()), 0) if len(has) else None,
        "adv20_median_no_ntd": round(float(non["adv20_t0_ntd"].median()), 0) if len(non) else None,
    }

    # 同一批可交易事件下：期貨腿 vs 現股腿
    pair = df[df["fut_r_adj_pct"].notna()]
    pair_liq = pair[pair["fut_liquid_ge800"]]
    hb1["futures_vs_stock_same_events"] = {
        "n_paired": int(len(pair)),
        "stock_leg": full_stats(pair["r_adj_pct"], "paired_stock"),
        "futures_leg": full_stats(pair["fut_r_adj_pct"], "paired_futures"),
        "mean_diff_fut_minus_stock_pct": round(
            float((pair["fut_r_adj_pct"] - pair["r_adj_pct"]).mean()), 3
        )
        if len(pair)
        else None,
        "median_diff_pct": round(
            float((pair["fut_r_adj_pct"] - pair["r_adj_pct"]).median()), 3
        )
        if len(pair)
        else None,
        "paired_ttest_p": round(
            float(stats.ttest_rel(pair["fut_r_adj_pct"], pair["r_adj_pct"]).pvalue), 4
        )
        if len(pair) >= 2
        else None,
        "n_holds_crossing_roll": int(pair["fut_n_rolls"].gt(0).sum()) if len(pair) else 0,
        "liquid_only": {
            "n": int(len(pair_liq)),
            "stock_leg": full_stats(pair_liq["r_adj_pct"], "liquid_stock"),
            "futures_leg": full_stats(pair_liq["fut_r_adj_pct"], "liquid_futures"),
        },
    }

    bs = pair["basis_drag_pct"].dropna()
    hb1["basis"] = {
        "n": int(len(bs)),
        "entry_basis_median_pct": round(float(pair["basis_entry_pct"].median()), 3) if len(pair) else None,
        "entry_basis_mean_pct": round(float(pair["basis_entry_pct"].mean()), 3) if len(pair) else None,
        "exit_basis_median_pct": round(float(pair["basis_exit_pct"].median()), 3) if len(pair) else None,
        "drag_mean_pct": round(float(bs.mean()), 3) if len(bs) else None,
        "drag_median_pct": round(float(bs.median()), 3) if len(bs) else None,
        "drag_std_pct": round(float(bs.std()), 3) if len(bs) else None,
        "note": "basis_drag = (期貨/現股−1)@出場 − (期貨/現股−1)@進場；正=期貨腿相對現股多賺",
    }

    # 期貨是否繞開處置：處置中的事件有多少有期貨
    disp_ev = df[df["disp_t1_prefund"] == "prefund_blanket"]
    hb1["blocked_events_futures_rescue"] = {
        "n_blanket_prefund_blocked": int(len(disp_ev)),
        "n_of_those_with_liquid_futures": int(disp_ev["fut_liquid_ge800"].sum()),
        "detail": disp_ev[
            ["stock_id", "signal_date", "entry_date", "in_fut_universe",
             "fut_adv20_lots_front2", "fut_liquid_ge800", "r_adj_pct", "fut_r_adj_pct"]
        ].to_dict("records"),
    }
    rep["H_B1_futures_vehicle"] = hb1

    # ---------------- H-B2 ----------------
    hb2: dict = {}
    blocked_disp = df["disp_t1_prefund"] == "prefund_blanket"
    degraded_disp = df["disp_t1_prefund"] == "prefund_ge10lots"
    blocked_limit = df["locked_limit_up_all_day"]

    hb2["filter_incidence"] = {
        "F1_disposition_blanket_prefund_at_T1": int(blocked_disp.sum()),
        "F1b_disposition_threshold_prefund_at_T1": int(degraded_disp.sum()),
        "F1_any_disposition_active_at_T1": int(df["disp_t1_active"].sum()),
        "F2_locked_limit_up_all_day_at_T1": int(blocked_limit.sum()),
        "F2b_open_at_limit_but_touchable": int(df["open_limit_touchable"].sum()),
        "F3_adv20_below_floor": {
            f"{f/1e8:.1f}yi": int((df["adv20_t0_ntd"] < f).sum()) for f in ADV_FLOORS
        },
        "F4_odd_lot_required_budget_100k": int(df["is_odd_lot"].sum()),
        "F4b_one_lot_cost_gt_budget": int((df["one_lot_cost_ntd"] > BUDGET_TWD).sum()),
        "max_notional_pct_of_adv": round(float(df["notional_pct_of_adv"].max()), 4),
    }

    hard_block = blocked_disp | blocked_limit
    hb2["untradable_events"] = df[hard_block][
        [
            "stock_id", "signal_date", "entry_date", "entry_open", "disp_t1_prefund",
            "disp_t1_types", "disp_market", "gap_open_pct", "locked_limit_up_all_day",
            "r_adj_pct", "in_fut_universe", "fut_liquid_ge800", "fut_r_adj_pct",
        ]
    ].to_dict("records")

    variants = {
        "baseline_all": df,
        "F1_only_drop_blanket_prefund": df[~blocked_disp],
        "F2_only_drop_locked_limit_up": df[~blocked_limit],
        "F1+F2_hard_block": df[~hard_block],
        "F1+F2+drop_any_disposition": df[~(df["disp_t1_active"] | blocked_limit)],
    }
    for f in ADV_FLOORS:
        variants[f"F1+F2+adv_ge_{f/1e8:.1f}yi"] = df[~hard_block & (df["adv20_t0_ntd"] >= f)]
    hb2["stats_by_filter"] = {
        k: full_stats(v["r_adj_pct"], k) for k, v in variants.items()
    }

    # 反面：被剔除的那些筆本身是賺是賠（倖存者偏誤反向版）
    hb2["removed_slice_returns"] = {
        "blanket_prefund_removed": full_stats(df[blocked_disp]["r_adj_pct"], "removed_disp_blanket"),
        "locked_limit_up_removed": full_stats(df[blocked_limit]["r_adj_pct"], "removed_limitup"),
        "any_disposition_removed": full_stats(df[df["disp_t1_active"]]["r_adj_pct"], "removed_any_disp"),
        "note": "若被剔除者報酬顯著高於留下者，代表過濾在砍掉真實 alpha（紙上報酬不可實現）",
    }

    # 零股維度
    odd = df[df["is_odd_lot"]]
    whole = df[~df["is_odd_lot"]]
    hb2["odd_lot_dimension"] = {
        "budget_twd": BUDGET_TWD,
        "n_odd_lot_required": int(len(odd)),
        "pct_odd_lot": round(df["is_odd_lot"].mean() * 100, 1),
        "entry_price_deciles": {
            str(q): round(float(df["entry_open"].quantile(q)), 1)
            for q in (0.1, 0.25, 0.5, 0.75, 0.9, 1.0)
        },
        "median_budget_shares": int(df["budget_shares"].median()),
        "min_budget_shares": int(df["budget_shares"].min()),
        "n_shares_lt_500": int((df["budget_shares"] < 500).sum()),
        "odd_lot_leg": full_stats(odd["r_adj_pct"], "odd_lot"),
        "whole_lot_leg": full_stats(whole["r_adj_pct"], "whole_lot"),
        "data_gap": "DB 無盤中零股成交量/委託簿資料，零股實際可成交性無法量化（見限制清單）",
    }

    # 6449 專章（協調者指定）
    c6449 = df[df["stock_id"] == "6449"]
    hb2["case_6449"] = {
        "n_events": int(len(c6449)),
        "in_fut_universe": bool(c6449["in_fut_universe"].iloc[0]) if len(c6449) else None,
        "rows": c6449[
            ["signal_date", "entry_date", "entry_open", "r_adj_pct", "disp_t1_active",
             "disp_t1_prefund", "disp_t1_types", "locked_limit_up_all_day",
             "adv20_t0_ntd", "budget_shares", "is_odd_lot", "in_fut_universe"]
        ].to_dict("records"),
        "blocked_by_hard_filters": int(hard_block[df["stock_id"] == "6449"].sum()),
    }
    rep["H_B2_pretrade_filters"] = hb2

    (OUT_DIR / "tradability_report.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print(json.dumps(rep, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
