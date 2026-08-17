#!/usr/bin/env python3
"""第十輪附掛：scan_5d_net95 的 INNER JOIN 缺價靜默丟失 → PIT-legal last-known-close 敏感度檢查.

背景（資料品質缺口，2026-08-17 由平行 agent 指出並由本輪獨立量化）：
  `scan_5d_net95()` 用 `stock_broker_branch_daily INNER JOIN stock_daily_bars`（同日 close）
  換算金額。9217 的 tape 在 2024-07-01~2026-08-14 有 **52.3%** 的 (stock, day) 列
  在 `stock_daily_bars` 找不到當日價格列，被 INNER JOIN 靜默丟棄
  （月度缺失率 2024-07 60.8% → 2026-08 46.3%）。成因是
  `run_songshan_follow_watch.py::refresh_missing_ohlc()` 每輪硬上限只補 80 檔，補不完就積著。
  → 第六~八輪的 n=36 母體、以及第十輪主腳本的 n=48 母體，都可能一直有事件被吃掉。

本腳本做一次**唯讀、記憶體內**的敏感度檢查：把「同日 close」換成
「trade_date <= 該日的最新一筆 close」（PIT 合法：只用當日或更早的資訊），
重算事件母體與全部統計量，看 n 與結論變動多少。**不寫 DB、不補資料。**

用法：
  PYTHONPATH=src .venv/bin/python scripts/research/study_whale_9217_round10_price_fallback_sensitivity.py

輸出：
  reports/research/branch-footprint-screen/whale_9217_round10_price_fallback_events.csv
  reports/research/branch-footprint-screen/whale_9217_round10_price_fallback_trades.csv
  reports/research/branch-footprint-screen/whale_9217_round10_price_fallback_summary.json
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import pandas as pd

from stock_db import DEFAULT_DB_PATH
from stock_db.connection import connect_ro
from research.branch_signal_validation import build_l1h7_signal_dict, permutation_test

STUDY_START = "2024-07-01"
STUDY_END = "2026-08-14"
PRICE_HISTORY_START = "2023-01-01"  # fallback 需要往前找最近一筆已知收盤
N_PERM = 20_000
PERM_SEEDS = (20260728, 20260817, 424242)
CAMPAIGN_HEADLINE_GAP = 10
FAMILY = ("9217", "9661", "9227", "9801")
OUT_DIR = ROOT / "reports" / "research" / "branch-footprint-screen"
SCRIPTS = ROOT / "scripts" / "research"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MGEN = _load_module("mgen_fb", SCRIPTS / "study_whale_branch_5d_net95_live_signal_validation.py")
R10 = _load_module("r10", SCRIPTS / "study_whale_9217_round10_extended_window.py")
MGEN.STUDY_START = STUDY_START
MGEN.STUDY_END = STUDY_END


def section(t: str) -> None:
    print(f"\n{'=' * 92}\n{t}\n{'=' * 92}")


# ---------------------------------------------------------------------------
# PIT-legal fallback price panel
# ---------------------------------------------------------------------------

_PRICE_PANEL: pd.DataFrame | None = None


def load_price_panel(conn) -> pd.DataFrame:
    global _PRICE_PANEL
    if _PRICE_PANEL is None:
        df = pd.read_sql_query(
            """
            SELECT stock_id, trade_date, close FROM stock_daily_bars
            WHERE source='finmind' AND close>0 AND trade_date BETWEEN ? AND ?
              AND length(stock_id)=4 AND stock_id GLOB '[0-9][0-9][0-9][0-9]'
              AND stock_id NOT GLOB '00*'
            """,
            conn,
            params=(PRICE_HISTORY_START, STUDY_END),
        )
        df["dt"] = pd.to_datetime(df["trade_date"])
        _PRICE_PANEL = df.sort_values(["dt", "stock_id"]).reset_index(drop=True)
        print(f"[INFO] 價格 panel：{len(_PRICE_PANEL)} 列，{_PRICE_PANEL['stock_id'].nunique()} 檔")
    return _PRICE_PANEL


def load_raw_activity_fallback(conn, trader_id: str, start: str, end: str) -> pd.DataFrame:
    """同 scan_5d_net95 的宇宙篩選，但價格改用 PIT-legal last-known close（asof backward）。"""
    raw = pd.read_sql_query(
        """
        SELECT stock_id, trade_date, buy, sell FROM stock_broker_branch_daily
        WHERE source='finmind' AND securities_trader_id=? AND trade_date BETWEEN ? AND ?
          AND length(stock_id)=4 AND stock_id GLOB '[0-9][0-9][0-9][0-9]'
          AND stock_id NOT GLOB '00*'
        """,
        conn,
        params=(trader_id, start, end),
    )
    panel = load_price_panel(conn)
    raw["dt"] = pd.to_datetime(raw["trade_date"])
    raw = raw.sort_values(["dt", "stock_id"]).reset_index(drop=True)
    merged = pd.merge_asof(
        raw,
        panel.rename(columns={"trade_date": "price_date"})[["dt", "stock_id", "price_date", "close"]],
        on="dt",
        by="stock_id",
        direction="backward",
    )
    n_total = len(merged)
    n_no_price = int(merged["close"].isna().sum())
    n_same_day = int((merged["price_date"] == merged["trade_date"]).sum())
    n_stale = n_total - n_same_day - n_no_price
    stale_days = (
        (pd.to_datetime(merged["dt"]) - pd.to_datetime(merged["price_date"])).dt.days.dropna()
    )
    print(
        f"[INFO] {trader_id} tape 列數={n_total}；同日有價={n_same_day} ({n_same_day/n_total*100:.1f}%)、"
        f"用舊價救回={n_stale} ({n_stale/n_total*100:.1f}%)、完全無價仍丟棄={n_no_price} "
        f"({n_no_price/n_total*100:.1f}%)"
    )
    if len(stale_days):
        print(
            f"[INFO] fallback 價格陳舊度（日曆天）：median={stale_days.median():.0f}, "
            f"p90={stale_days.quantile(0.9):.0f}, max={stale_days.max():.0f}"
        )
    merged = merged.dropna(subset=["close"])
    merged["buy_amt"] = merged["buy"] * merged["close"]
    merged["sell_amt"] = merged["sell"] * merged["close"]
    stats = {
        "n_tape_rows": n_total,
        "n_same_day_price": n_same_day,
        "n_rescued_by_stale_price": n_stale,
        "n_still_no_price": n_no_price,
        "same_day_price_pct": round(n_same_day / n_total * 100, 2),
        "rescued_pct": round(n_stale / n_total * 100, 2),
        "stale_days_median": float(stale_days.median()) if len(stale_days) else None,
        "stale_days_p90": float(stale_days.quantile(0.9)) if len(stale_days) else None,
        "stale_days_max": float(stale_days.max()) if len(stale_days) else None,
    }
    return merged[["stock_id", "trade_date", "buy_amt", "sell_amt"]].reset_index(drop=True), stats


def main() -> int:
    print(f"[INFO] DB(read-only) = {DEFAULT_DB_PATH}")
    conn = connect_ro(DEFAULT_DB_PATH)
    mega = MGEN.load_mega(MGEN.MEGA_PATH)
    calendar = MGEN.load_calendar(conn, STUDY_START, STUDY_END)
    ix_dict = build_l1h7_signal_dict(MGEN.load_ix(conn))
    bars_cache: dict[str, dict] = {}

    join_stats: dict[str, dict] = {}

    def patched(c, tid, s, e):
        df, st = load_raw_activity_fallback(c, tid, s, e)
        join_stats[tid] = st
        return df

    MGEN.load_raw_activity = patched

    results: dict[str, dict] = {}
    for tid in FAMILY:
        section(f"fallback-price 重算：{tid}")
        events = MGEN.build_5d_net95_events(conn, tid, mega, 50_000_000.0, 0.95)
        trades, drop_stats = MGEN.build_trades(conn, events)
        print(json.dumps(drop_stats, ensure_ascii=False))
        full = MGEN.full_stats(trades["r_adj_pct"], f"{tid}_fallback_n{len(trades)}")
        print(json.dumps(full, ensure_ascii=False, indent=2))
        d = R10.build_dicts(conn, trades, ix_dict, bars_cache)
        perm = R10.multi_seed_permutation(trades, d, ix_dict)
        entry = {
            "n_events_rising_edge": len(events),
            "drop_stats": drop_stats,
            "full_sample": full,
            "permutation": perm,
            "join_coverage": join_stats.get(tid),
        }
        if tid == "9217":
            events.to_csv(OUT_DIR / "whale_9217_round10_price_fallback_events.csv", index=False)
            trades.to_csv(OUT_DIR / "whale_9217_round10_price_fallback_trades.csv", index=False)
            entry["outlier_trim_sensitivity"] = MGEN.outlier_trim_sensitivity(trades)
            entry["yearly_breakdown"] = MGEN.yearly_breakdown(trades)
            # campaign 去重
            ev_c = R10.campaign_dedup_events(events, calendar, CAMPAIGN_HEADLINE_GAP)
            keep = set(zip(ev_c["stock_id"], ev_c["signal_date"]))
            tr_c = trades[[(r.stock_id, r.signal_date) in keep for r in trades.itertuples(index=False)]]
            st_c = MGEN.full_stats(tr_c["r_adj_pct"], f"fallback_campaign_gap10_n{len(tr_c)}")
            print("[campaign gap<=10]", json.dumps(st_c, ensure_ascii=False, indent=2))
            d_c = R10.build_dicts(conn, tr_c, ix_dict, bars_cache)
            entry["campaign_dedup_gap10"] = {
                "n_events_after_dedup": len(ev_c),
                "stats": st_c,
                "permutation": R10.multi_seed_permutation(tr_c, d_c, ix_dict),
            }
            # 前瞻 OOS 切片
            entry["prospective_oos"] = {
                "ge_2026-07-28": {
                    "n_events": int((events["signal_date"] >= "2026-07-28").sum()),
                    "n_trades": int((trades["signal_date"] >= "2026-07-28").sum()),
                },
                "gt_2026-06-22": trades[trades["signal_date"] > "2026-06-22"][
                    ["signal_date", "stock_id", "r_adj_pct"]
                ].to_dict("records"),
                "last_event": {
                    "stock_id": events.iloc[-1]["stock_id"],
                    "signal_date": events.iloc[-1]["signal_date"],
                }
                if len(events)
                else None,
            }
            print(json.dumps(entry["prospective_oos"], ensure_ascii=False, indent=2, default=str))
        results[tid] = entry
    conn.close()

    section("fallback 版 BH-FDR（N=4）")
    fdr = {}
    for stat in ("mean", "median"):
        for i, seed in enumerate(PERM_SEEDS):
            pv = {t: results[t]["permutation"]["per_seed"][i][f"p_value_{stat}_onesided"] for t in FAMILY}
            fdr[f"{stat}_seed{seed}"] = {"p_values": pv, "bh": R10.bh_fdr(pv)}
    for k, v in fdr.items():
        print(k, [(r["key"], round(r["q_bh"], 5), r["pass"]) for r in v["bh"]["ranked"]])

    payload = {
        "round": 10,
        "variant": "PIT-legal last-known-close fallback (取代同日 close INNER JOIN)",
        "study_window": f"{STUDY_START}..{STUDY_END}",
        "n_perm": N_PERM,
        "seeds": list(PERM_SEEDS),
        "db_access": "read-only (connect_ro), 記憶體內重算，未寫入任何資料",
        "results": results,
        "bh_fdr": fdr,
    }
    p = OUT_DIR / "whale_9217_round10_price_fallback_summary.json"
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n[OK] 寫入 {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
