#!/usr/bin/env python3
"""第十輪：9217（凱基-松山）scan_5d_net95 live 訊號延長窗口正式重跑（2024-07-01 ~ 2026-08-14）.

背景：
  第六~八輪的樣本止於 signal_date 2026-06-22（scan 窗口 STUDY_END=2026-07-24，其中
  2026-07-03/2026-07-22 兩筆事件當時因 H7 未走完被右設限而未進 trades，n=36）。
  分點 tape 已累積到 2026-08-14，本輪把窗口延長並把「舊窗口之後」的區段當成一段
  真正的前瞻樣本外（prospective OOS）單獨報告。

本輪只做重跑與切片，**不改協議**：
  訊號 = scan_5d_net95（rolling 5 交易日 buy_5d>=0.5億 ∩ net_ratio>=0.95 ∩ !mega）
  協議 = L1H7（T+1 開盤進 / 第7個交易日收盤出 / COST=30bps / BETA=1.15 / bench=IX0001）
  去重 = rising-edge（per stock，狀態由 not-triggered 翻成 triggered 的第一天）

所有計算函式直接 import 既有腳本／模組，不重寫一套：
  scripts/research/study_whale_9217_5d_net95_live_signal_validation.py（9217 專用；回傳 full grid）
  scripts/research/study_whale_branch_5d_net95_live_signal_validation.py（--trader-id 通用版）
  src/research/branch_signal_validation.py（permutation/placebo）
  scripts/research/study_whale_9217_6147_2337_campaign_dedup.py 的 merge_campaigns() 定義

DB 一律唯讀（stock_db.connect_ro）。不寫 DB、不建索引、不 VACUUM。

用法：
  PYTHONPATH=src .venv/bin/python scripts/research/study_whale_9217_round10_extended_window.py

輸出（全部帶 round10，不覆蓋第六~八輪 artifact）：
  reports/research/branch-footprint-screen/whale_9217_round10_events.csv
  reports/research/branch-footprint-screen/whale_9217_round10_trades.csv
  reports/research/branch-footprint-screen/whale_9217_round10_campaign_gap_sweep.csv
  reports/research/branch-footprint-screen/whale_9217_round10_summary.json
  reports/research/branch-footprint-screen/whale_branch_family_round10_bhfdr.json
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np
import pandas as pd

from stock_db import DEFAULT_DB_PATH
from stock_db.connection import connect_ro
from research.branch_signal_validation import build_l1h7_signal_dict, permutation_test

# ---- 本輪參數 -------------------------------------------------------------
STUDY_START = "2024-07-01"
STUDY_END_R10 = "2026-08-14"          # 延長後
STUDY_END_PRIOR = "2026-07-24"        # 第六~八輪 scan 窗口尾端
LAST_EVALUABLE_PRIOR_SIGNAL = "2026-06-22"  # 第六~八輪 n=36 裡最後一筆可評估訊號
PROSPECTIVE_CUT = "2026-07-28"        # 使用者指定的前瞻 OOS 起點（含）

N_PERM = 20_000
PERM_SEEDS = (20260728, 20260817, 424242)
CAMPAIGN_GAPS = (1, 3, 5, 7, 10, 15, 20)
CAMPAIGN_HEADLINE_GAP = 10
FAMILY = ("9217", "9661", "9227", "9801")  # 第八輪定義的 N=4 同protocol家族
FDR_ALPHA = 0.05

OUT_DIR = ROOT / "reports" / "research" / "branch-footprint-screen"
SCRIPTS = ROOT / "scripts" / "research"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


M9217 = _load_module("m9217", SCRIPTS / "study_whale_9217_5d_net95_live_signal_validation.py")
MGEN = _load_module("mgen", SCRIPTS / "study_whale_branch_5d_net95_live_signal_validation.py")

# 把兩個模組的 scan 窗口尾端統一延長到本輪的 STUDY_END
M9217.STUDY_START = STUDY_START
M9217.STUDY_END = STUDY_END_R10
MGEN.STUDY_START = STUDY_START
MGEN.STUDY_END = STUDY_END_R10


def section(title: str) -> None:
    print(f"\n{'=' * 92}\n{title}\n{'=' * 92}")


# ---------------------------------------------------------------------------
# campaign 去重（比照 study_whale_9217_6147_2337_campaign_dedup.py::merge_campaigns）
# ---------------------------------------------------------------------------

def merge_campaigns(dates: list[str], all_dates: list[str], gap: int) -> list[str]:
    """同股票內，交易日距離 <= gap 的連續訊號合併為一波 campaign，只保留第一筆。"""
    idx = {d: i for i, d in enumerate(all_dates)}
    dates = sorted(d for d in dates if d in idx)
    if not dates:
        return []
    campaigns: list[str] = []
    cur_start = dates[0]
    last_idx = idx[dates[0]]
    for d in dates[1:]:
        di = idx[d]
        if di - last_idx <= gap:
            last_idx = di
            continue
        campaigns.append(cur_start)
        cur_start = d
        last_idx = di
    campaigns.append(cur_start)
    return campaigns


def campaign_dedup_events(events: pd.DataFrame, calendar: list[str], gap: int) -> pd.DataFrame:
    keep: list[tuple[str, str]] = []
    for sid, sub in events.groupby("stock_id"):
        reps = merge_campaigns(sub["signal_date"].tolist(), calendar, gap)
        keep.extend((sid, d) for d in reps)
    keep_set = set(keep)
    mask = [(r.stock_id, r.signal_date) in keep_set for r in events.itertuples(index=False)]
    return events[pd.Series(mask, index=events.index)].reset_index(drop=True)


# ---------------------------------------------------------------------------
# permutation：多 seed
# ---------------------------------------------------------------------------

def multi_seed_permutation(trades: pd.DataFrame, stock_dicts: dict, ix_dict: dict) -> dict:
    if trades.empty:
        return {"note": "no trades", "per_seed": [], "p_mean_seeds": [], "p_median_seeds": []}
    per_seed = []
    for seed in PERM_SEEDS:
        res = permutation_test(
            trades[["stock_id", "signal_date"]], stock_dicts, ix_dict, n_perm=N_PERM, seed=seed
        )
        res["seed"] = seed
        per_seed.append(res)
        print(
            f"    seed={seed}: n={res['n_events']} obs_mean={res['observed_mean_pct']:.3f}% "
            f"obs_median={res['observed_median_pct']:.3f}% "
            f"p_mean={res['p_value_mean_onesided']:.5f} p_median={res['p_value_median_onesided']:.5f}"
        )
    return {
        "n_perm": N_PERM,
        "seeds": list(PERM_SEEDS),
        "per_seed": per_seed,
        "p_mean_seeds": [r["p_value_mean_onesided"] for r in per_seed],
        "p_median_seeds": [r["p_value_median_onesided"] for r in per_seed],
        "p_mean_worst": max(r["p_value_mean_onesided"] for r in per_seed),
        "p_median_worst": max(r["p_value_median_onesided"] for r in per_seed),
    }


# ---------------------------------------------------------------------------
# BH-FDR
# ---------------------------------------------------------------------------

def bh_fdr(pvals: dict[str, float], alpha: float = FDR_ALPHA) -> dict:
    """Benjamini-Hochberg step-up。p=0 以 1/N_PERM 為下限（permutation 解析度）。"""
    floor = 1.0 / N_PERM
    items = sorted(((k, max(float(v), floor)) for k, v in pvals.items()), key=lambda x: x[1])
    n = len(items)
    raw_q = [(k, p * n / (i + 1)) for i, (k, p) in enumerate(items)]
    # step-up monotone：從最大 rank 往回取 min
    q_mono: list[tuple[str, float]] = []
    running = float("inf")
    for k, q in reversed(raw_q):
        running = min(running, q)
        q_mono.append((k, min(running, 1.0)))
    q_mono.reverse()
    return {
        "alpha": alpha,
        "n_hypotheses": n,
        "p_floor_used": floor,
        "ranked": [
            {"key": k, "p_used": p, "rank": i + 1, "q_bh": round(q, 6), "pass": q <= alpha}
            for i, ((k, p), (_, q)) in enumerate(zip(items, q_mono))
        ],
    }


# ---------------------------------------------------------------------------
# 一支分點的完整 pipeline（scan → trades → dicts）
# ---------------------------------------------------------------------------

def run_branch(conn, tid: str, mega: set[str]) -> dict:
    if tid == "9217":
        events, grid = M9217.build_5d_net95_events(conn, mega)
    else:
        events = MGEN.build_5d_net95_events(conn, tid, mega, 50_000_000.0, 0.95)
        grid = None
    trades, drop_stats = MGEN.build_trades(conn, events)
    return {"trader_id": tid, "events": events, "grid": grid, "trades": trades, "drop_stats": drop_stats}


def build_dicts(conn, trades: pd.DataFrame, ix_dict: dict, cache: dict) -> dict:
    out = {}
    for sid in sorted(trades["stock_id"].unique()):
        if sid not in cache:
            cache[sid] = build_l1h7_signal_dict(MGEN.load_stock_bars(conn, sid))
        out[sid] = cache[sid]
    return out


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    print(f"[INFO] DB(read-only) = {DEFAULT_DB_PATH}")
    conn = connect_ro(DEFAULT_DB_PATH)
    mega = MGEN.load_mega(MGEN.MEGA_PATH)
    print(f"[INFO] mega blacklist n={len(mega)}")
    calendar = MGEN.load_calendar(conn, STUDY_START, STUDY_END_R10)
    print(f"[INFO] 交易日曆 {len(calendar)} 天：{calendar[0]} ~ {calendar[-1]}")

    ix_bars = MGEN.load_ix(conn)
    ix_dict = build_l1h7_signal_dict(ix_bars)
    bars_cache: dict[str, dict] = {}

    # ---------------- (1) 9217 完整重跑 ----------------
    section("(1) 9217 全窗口重跑 2024-07-01 ~ 2026-08-14（scan_5d_net95 + L1H7）")
    r9217 = run_branch(conn, "9217", mega)
    events, grid, trades = r9217["events"], r9217["grid"], r9217["trades"]
    print(json.dumps(r9217["drop_stats"], ensure_ascii=False, indent=2))
    events.to_csv(OUT_DIR / "whale_9217_round10_events.csv", index=False)
    trades.to_csv(OUT_DIR / "whale_9217_round10_trades.csv", index=False)

    full = MGEN.full_stats(trades["r_adj_pct"], f"round10_full_n{len(trades)}")
    trim = MGEN.outlier_trim_sensitivity(trades)
    yearly = MGEN.yearly_breakdown(trades)
    print("\n[全樣本]", json.dumps(full, ensure_ascii=False, indent=2))
    print("[去極值]", json.dumps(trim, ensure_ascii=False, indent=2))
    print("[年度]", json.dumps(yearly, ensure_ascii=False, indent=2))

    # ---------------- (2) 乾涸獨立驗證 ----------------
    section("(2) 2026-07-22 之後的訊號乾涸獨立驗證")
    after = grid[grid["trade_date"] > "2026-07-22"].copy()
    trig_days = after[after["triggered"]]
    edge_after = after[after["is_event"]]
    near_miss = after[(after["buy_5d"] >= 50_000_000.0) & (~after["stock_id"].isin(mega))]
    near_miss_top = (
        near_miss.sort_values("net_ratio", ascending=False)
        .head(10)[["stock_id", "trade_date", "buy_5d", "net_ratio"]]
        .to_dict("records")
    )
    days_after = [d for d in calendar if d > "2026-07-22"]
    drought = {
        "trading_days_after_2026-07-22": len(days_after),
        "date_range": [days_after[0], days_after[-1]] if days_after else None,
        "n_triggered_state_rows": int(len(trig_days)),
        "n_rising_edge_events": int(len(edge_after)),
        "branch_still_active": {
            "n_stock_days_with_activity": int(
                len(
                    after[(after["buy_amt"] > 0) | (after["sell_amt"] > 0)]
                )
            ),
            "n_distinct_stocks": int(
                after[(after["buy_amt"] > 0) | (after["sell_amt"] > 0)]["stock_id"].nunique()
            ),
        },
        "n_stockdays_passing_buy_floor_only": int(len(near_miss)),
        "top10_net_ratio_among_buyfloor_passers": near_miss_top,
        "last_rising_edge_event_overall": {
            "stock_id": events.iloc[-1]["stock_id"],
            "signal_date": events.iloc[-1]["signal_date"],
        }
        if len(events)
        else None,
    }
    print(json.dumps(drought, ensure_ascii=False, indent=2, default=str))

    # ---------------- (3) 前瞻 OOS 切片 ----------------
    section("(3) 前瞻樣本外切片")
    # 右設限邊界：能完成 H7 的最後一個訊號日
    tradable_last = calendar[-(MGEN.HOLD + 1)] if len(calendar) > MGEN.HOLD else None
    slices = {}
    for label, cut in (
        (f"prospective_ge_{PROSPECTIVE_CUT}", PROSPECTIVE_CUT),
        (f"post_prior_scan_end_gt_{STUDY_END_PRIOR}", STUDY_END_PRIOR),
        (f"post_prior_evaluable_gt_{LAST_EVALUABLE_PRIOR_SIGNAL}", LAST_EVALUABLE_PRIOR_SIGNAL),
    ):
        ev = events[events["signal_date"] >= cut] if label.startswith("prospective") else events[
            events["signal_date"] > cut
        ]
        tr = trades[trades["signal_date"] >= cut] if label.startswith("prospective") else trades[
            trades["signal_date"] > cut
        ]
        slices[label] = {
            "cutoff": cut,
            "n_events": int(len(ev)),
            "n_trades_evaluable": int(len(tr)),
            "events": ev[["stock_id", "signal_date", "buy_5d", "net_ratio"]].to_dict("records"),
            "trades": tr.to_dict("records"),
            "stats": MGEN.full_stats(tr["r_adj_pct"], label) if len(tr) else {"label": label, "n": 0},
        }
        print(f"\n--- {label}: n_events={len(ev)}, n_trades={len(tr)}")
        if len(tr):
            print(tr[["signal_date", "stock_id", "r_adj_pct"]].to_string(index=False))
    slices["right_censoring_note"] = {
        "last_signal_date_that_can_complete_H7": tradable_last,
        "explain": "訊號日之後需要 1(進場) + 7(持有) 個交易日，晚於此日的觸發即使發生也無法評估",
    }
    print(json.dumps(slices["right_censoring_note"], ensure_ascii=False, indent=2))

    # ---------------- (4) campaign 去重 ----------------
    section("(4) campaign 去重 gap sweep（同股票 <=gap 交易日內的連續觸發合併為一波）")
    gap_rows = []
    camp_stats: dict[str, dict] = {}
    camp_trades_headline = None
    for gap in CAMPAIGN_GAPS:
        ev_c = campaign_dedup_events(events, calendar, gap)
        keep = set(zip(ev_c["stock_id"], ev_c["signal_date"]))
        tr_c = trades[[(r.stock_id, r.signal_date) in keep for r in trades.itertuples(index=False)]]
        st = MGEN.full_stats(tr_c["r_adj_pct"], f"campaign_gap{gap}_n{len(tr_c)}")
        tm = MGEN.outlier_trim_sensitivity(tr_c)
        camp_stats[f"gap_{gap}"] = {"n_events_after_dedup": int(len(ev_c)), "stats": st, "trim": tm}
        gap_rows.append(
            {
                "gap": gap,
                "n_events": len(ev_c),
                "n_trades": st["n"],
                "mean_pct": st.get("mean_pct"),
                "median_pct": st.get("median_pct"),
                "win_rate_pct": st.get("win_rate_pct"),
                "t_p": st.get("t_p"),
                "wilcoxon_p": st.get("wilcoxon_p"),
            }
        )
        if gap == CAMPAIGN_HEADLINE_GAP:
            camp_trades_headline = tr_c.copy()
    gap_df = pd.DataFrame(gap_rows)
    print(gap_df.to_string(index=False))
    gap_df.to_csv(OUT_DIR / "whale_9217_round10_campaign_gap_sweep.csv", index=False)

    # ---------------- (5) permutation ----------------
    section("(5) permutation/placebo 20000 次 × 3 seed")
    dicts_full = build_dicts(conn, trades, ix_dict, bars_cache)
    print("  [全樣本]")
    perm_full = multi_seed_permutation(trades, dicts_full, ix_dict)
    print(f"  [campaign 去重 gap<={CAMPAIGN_HEADLINE_GAP}]")
    dicts_camp = build_dicts(conn, camp_trades_headline, ix_dict, bars_cache)
    perm_camp = multi_seed_permutation(camp_trades_headline, dicts_camp, ix_dict)

    # ---------------- (6) 家族 BH-FDR ----------------
    section("(6) N=4 家族（9217/9661/9227/9801）延長窗口重跑 + BH-FDR")
    family: dict[str, dict] = {}
    for tid in FAMILY:
        if tid == "9217":
            fam_trades = trades
            fam_perm = perm_full
            fam_full = full
            n_events_tid = len(events)
        else:
            print(f"\n--- {tid} ---")
            rb = run_branch(conn, tid, mega)
            fam_trades = rb["trades"]
            n_events_tid = len(rb["events"])
            rb["events"].to_csv(OUT_DIR / f"whale_{tid}_round10_events.csv", index=False)
            fam_trades.to_csv(OUT_DIR / f"whale_{tid}_round10_trades.csv", index=False)
            fam_full = MGEN.full_stats(fam_trades["r_adj_pct"], f"{tid}_round10_n{len(fam_trades)}")
            print(json.dumps(fam_full, ensure_ascii=False, indent=2))
            d = build_dicts(conn, fam_trades, ix_dict, bars_cache)
            fam_perm = multi_seed_permutation(fam_trades, d, ix_dict)
        family[tid] = {
            "n_events_rising_edge": n_events_tid,
            "full_sample": fam_full,
            "permutation": fam_perm,
        }

    fdr = {}
    for stat in ("mean", "median"):
        for seed_i, seed in enumerate(PERM_SEEDS):
            pv = {
                tid: family[tid]["permutation"]["per_seed"][seed_i][f"p_value_{stat}_onesided"]
                for tid in FAMILY
            }
            fdr[f"{stat}_seed{seed}"] = {"p_values": pv, "bh": bh_fdr(pv)}
        # 用最保守（最大）seed p 值再做一次
        pv_worst = {tid: family[tid]["permutation"][f"p_{stat}_worst"] for tid in FAMILY}
        fdr[f"{stat}_worst_seed"] = {"p_values": pv_worst, "bh": bh_fdr(pv_worst)}
    print(json.dumps(fdr, ensure_ascii=False, indent=2))

    conn.close()

    # ---------------- 輸出 ----------------
    summary = {
        "round": 10,
        "generated_for": "9217 凱基-松山 scan_5d_net95 live 訊號 延長窗口正式重跑",
        "protocol": {
            "signal": "rolling 5-trading-day buy_5d>=50,000,000 & net_ratio>=0.95, mega excluded",
            "study_window": f"{STUDY_START}..{STUDY_END_R10}",
            "prior_round_window": f"{STUDY_START}..{STUDY_END_PRIOR}",
            "dedup": "rising-edge",
            "l1h7": {"cost": MGEN.COST, "hold": MGEN.HOLD, "beta": MGEN.BETA, "bench": MGEN.BENCH_CODE},
            "mega_excluded_n": len(mega),
            "db_access": "read-only (connect_ro)",
        },
        "n_events_rising_edge": len(events),
        "drop_stats": r9217["drop_stats"],
        "full_sample": full,
        "outlier_trim_sensitivity": trim,
        "yearly_breakdown": yearly,
        "drought_verification": drought,
        "prospective_oos_slices": slices,
        "campaign_dedup": {"gap_sweep": camp_stats, "headline_gap": CAMPAIGN_HEADLINE_GAP},
        "permutation_full_sample": perm_full,
        "permutation_campaign_dedup": perm_camp,
        "family_bh_fdr": fdr,
        "family_detail": {
            tid: {
                "n_events_rising_edge": family[tid]["n_events_rising_edge"],
                "full_sample": family[tid]["full_sample"],
                "p_mean_seeds": family[tid]["permutation"]["p_mean_seeds"],
                "p_median_seeds": family[tid]["permutation"]["p_median_seeds"],
            }
            for tid in FAMILY
        },
    }
    p1 = OUT_DIR / "whale_9217_round10_summary.json"
    p1.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    p2 = OUT_DIR / "whale_branch_family_round10_bhfdr.json"
    p2.write_text(
        json.dumps(
            {"family": FAMILY, "n_perm": N_PERM, "seeds": list(PERM_SEEDS), "bh_fdr": fdr,
             "family_detail": summary["family_detail"]},
            ensure_ascii=False, indent=2, default=str,
        ),
        encoding="utf-8",
    )
    print(f"\n[OK] 寫入 {p1}")
    print(f"[OK] 寫入 {p2}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
