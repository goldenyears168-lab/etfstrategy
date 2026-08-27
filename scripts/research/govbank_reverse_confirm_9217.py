#!/usr/bin/env python3
"""Gov-bank / foreign-shareholding reverse-indicator confirmation test on 9217 (KGI-松山).

Context: whale_chip_precursor research (reports/research/whale_chip_precursor/
FROZEN_PRECURSOR_RULES.md line 18, univariate_by_lag_enriched.csv) found that
八大行庫 (government-bank funds) 5-day net buy and 外資持股比例 change are REVERSE
indicators ahead of Tier-A whale-buy precursor events — i.e. gov-bank tends to be
NET SELLING and foreign ratio tends to be DECLINING right before/during those events,
not confirming them.

This script tests whether that same reverse-flow pattern, if present ahead of the
9217 (凱基-松山) decisive branch-follow signal (reports/research/branch-footprint-screen/
whale_branch_deepdive_9217_existing_rule_campaigns.csv, "既有規則 scan_5d_net95",
n=35 fire-day campaigns, entry_date = fire day T, excess_h9 already computed vs IX0001),
adds any real confirmation value: does forward excess_h9 differ between
"gov-bank/foreign also net-buying ahead of signal" (same direction, would CONTRADICT
the reverse-indicator hypothesis if it doesn't hurt returns) vs
"gov-bank/foreign net-selling ahead of signal" (opposite direction, CONFIRMS hypothesis)?

Data sources (reused exactly from whale_chip_precursor):
  - Foreign shareholding ratio: DB table stock_shareholding_daily.foreign_remaining_ratio
    (already synced, full history) -> foreign_ratio_chg_5d = ratio[T-1] - ratio[T-6]
    (same convention as src/research/chen_chip/features.py: g["foreign_ratio_chg_5d"] =
    foreign_remaining_ratio.diff(5), read at lag=1 i.e. row T-1).
  - Gov-bank net flow: FinMind dataset TaiwanStockGovernmentBankBuySell (NOT in local DB;
    research-only cache, same as reports/research/whale_chip_precursor/cache/
    gov_bank_net_tier_a.csv) -> gov_bank_net_sum_5d = sum(net[T-5..T-1]) (5 trading days
    strictly BEFORE the fire day, i.e. does not double-count the signal day's own tape).
  - "During" checks: foreign_ratio single-day chg on T, and gov_bank_net on T itself.

Output:
  reports/research/govbank_reverse_confirm/9217_govbank_foreign_join.csv
  reports/research/govbank_reverse_confirm/9217_govbank_foreign_bucket_summary.csv

Read-only DB. New FinMind API calls only for gov-bank (dataset has no DB table).
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from finmind_client import fetch_finmind_json, finmind_token  # noqa: E402
from research.chen_chip.adapters_db import connect_ro, load_calendar  # noqa: E402

EVENTS_CSV = ROOT / "reports/research/branch-footprint-screen/whale_branch_deepdive_9217_existing_rule_campaigns.csv"
OUT_DIR = ROOT / "reports/research/govbank_reverse_confirm"
OUT_DIR.mkdir(parents=True, exist_ok=True)
OUT_JOIN_CSV = OUT_DIR / "9217_govbank_foreign_join.csv"
OUT_SUMMARY_CSV = OUT_DIR / "9217_govbank_foreign_bucket_summary.csv"
GOV_CACHE_CSV = OUT_DIR / "gov_bank_cache_9217_events.csv"

CAL_D0, CAL_D1 = "2024-06-01", "2026-07-20"


def log(m: str) -> None:
    print(f"[govbank9217] {m}", flush=True)


def load_events() -> pd.DataFrame:
    df = pd.read_csv(EVENTS_CSV, dtype={"stock_id": str})
    log(f"loaded {len(df)} existing-rule 9217 campaigns from {EVENTS_CSV.name}")
    return df


def build_window_dates(events: pd.DataFrame, calendar: list[str]) -> tuple[pd.DataFrame, list[str]]:
    idx = {d: i for i, d in enumerate(calendar)}
    rows = []
    all_dates: set[str] = set()
    for _, r in events.iterrows():
        entry = r["entry_date"]
        if entry not in idx:
            log(f"  SKIP {r['stock_id']} {entry}: not in calendar")
            continue
        i0 = idx[entry]
        if i0 - 6 < 0:
            log(f"  SKIP {r['stock_id']} {entry}: not enough history before signal")
            continue
        win = calendar[i0 - 6 : i0 + 1]  # T-6..T inclusive (7 dates)
        rows.append({"stock_id": r["stock_id"], "entry_date": entry, "excess_h9": r["excess_h9"],
                      "n_events_in_campaign": r["n_events_in_campaign"],
                      "t6": win[0], "t5": win[1], "t4": win[2], "t3": win[3],
                      "t2": win[4], "t1": win[5], "t0": win[6]})
        all_dates |= set(win)
    return pd.DataFrame(rows), sorted(all_dates)


def fetch_gov_bank_for_dates(dates: list[str], stock_ids: set[str]) -> pd.DataFrame:
    if GOV_CACHE_CSV.exists():
        cached = pd.read_csv(GOV_CACHE_CSV, dtype={"sid": str})
        have = set(cached["d"].astype(str))
        need = [d for d in dates if d not in have]
        log(f"gov_bank cache hit: reuse {len(have)} days, need {len(need)} new")
    else:
        cached = pd.DataFrame()
        need = list(dates)

    if not need:
        return cached

    if not finmind_token():
        raise RuntimeError("FINMIND_TOKEN not available — cannot fetch TaiwanStockGovernmentBankBuySell")

    rows: list[dict] = []
    fail = 0
    for i, d in enumerate(need):
        try:
            payload = fetch_finmind_json({"dataset": "TaiwanStockGovernmentBankBuySell", "start_date": d})
            if payload.get("status") != 200:
                fail += 1
                log(f"  {d}: status={payload.get('status')} msg={payload.get('msg')}")
                continue
            data = payload.get("data") or []
            for item in data:
                sid = str(item.get("stock_id") or "")
                if sid not in stock_ids:
                    continue
                buy = float(item.get("buy") or 0)
                sell = float(item.get("sell") or 0)
                rows.append({"sid": sid, "d": d, "gov_bank_buy": buy, "gov_bank_sell": sell,
                             "gov_bank_net": buy - sell})
        except Exception as exc:  # noqa: BLE001
            fail += 1
            log(f"  {d}: ERR {exc}")
        if (i + 1) % 20 == 0:
            log(f"  progress {i+1}/{len(need)} fail={fail}")
        time.sleep(0.2)

    new_df = pd.DataFrame(rows)
    log(f"fetched {len(new_df)} (sid,date) gov_bank rows from {len(need)} calendar-day calls, fail={fail}")
    out = pd.concat([cached, new_df], ignore_index=True) if not cached.empty else new_df
    out = out.drop_duplicates(subset=["sid", "d"], keep="last")
    out.to_csv(GOV_CACHE_CSV, index=False)
    return out


def load_shareholding(conn, stock_ids: list[str], d0: str, d1: str) -> pd.DataFrame:
    ph = ",".join("?" * len(stock_ids))
    q = f"""
        SELECT stock_id, trade_date, foreign_remaining_ratio
        FROM stock_shareholding_daily
        WHERE stock_id IN ({ph}) AND trade_date >= ? AND trade_date <= ?
        ORDER BY stock_id, trade_date
    """
    return pd.read_sql_query(q, conn, params=[*stock_ids, d0, d1])


def main() -> int:
    events = load_events()
    conn = connect_ro()
    calendar = load_calendar(conn, CAL_D0, CAL_D1)
    log(f"calendar: {len(calendar)} trading days {calendar[0]}..{calendar[-1]}")

    win_df, all_dates = build_window_dates(events, calendar)
    log(f"windows built for {len(win_df)}/{len(events)} events, {len(all_dates)} unique calendar dates needed")

    stock_ids = sorted(win_df["stock_id"].unique().tolist())
    gov = fetch_gov_bank_for_dates(all_dates, set(stock_ids))
    gov_map = {(r["sid"], r["d"]): r["gov_bank_net"] for _, r in gov.iterrows()}

    sh = load_shareholding(conn, stock_ids, min(all_dates), max(all_dates))
    sh_map = {(r["stock_id"], r["trade_date"]): r["foreign_remaining_ratio"] for _, r in sh.iterrows()}
    conn.close()

    def gov_net(sid: str, d: str) -> float:
        return gov_map.get((sid, d), 0.0)  # missing day for that stock = 0 net (matches enriched script fillna(0))

    def fr_ratio(sid: str, d: str) -> float:
        return sh_map.get((sid, d), np.nan)

    out_rows = []
    for _, r in win_df.iterrows():
        sid = r["stock_id"]
        gov_sum_5d_before = sum(gov_net(sid, r[c]) for c in ("t5", "t4", "t3", "t2", "t1"))
        gov_net_during_t0 = gov_net(sid, r["t0"])
        ratio_t6 = fr_ratio(sid, r["t6"])
        ratio_t1 = fr_ratio(sid, r["t1"])
        ratio_t0 = fr_ratio(sid, r["t0"])
        fr_chg_5d_before = ratio_t1 - ratio_t6 if pd.notna(ratio_t1) and pd.notna(ratio_t6) else np.nan
        fr_chg_during_t0 = ratio_t0 - ratio_t1 if pd.notna(ratio_t0) and pd.notna(ratio_t1) else np.nan
        out_rows.append({
            "stock_id": sid, "entry_date": r["entry_date"], "excess_h9": r["excess_h9"],
            "n_events_in_campaign": r["n_events_in_campaign"],
            "gov_bank_sum_5d_before": gov_sum_5d_before,
            "gov_bank_net_during_t0": gov_net_during_t0,
            "foreign_ratio_chg_5d_before": fr_chg_5d_before,
            "foreign_ratio_chg_during_t0": fr_chg_during_t0,
        })
    join_df = pd.DataFrame(out_rows)
    join_df.to_csv(OUT_JOIN_CSV, index=False)
    log(f"wrote {OUT_JOIN_CSV} n={len(join_df)}")

    # ---- bucket + summarize ----
    def summarize(sub: pd.Series) -> dict:
        v = sub.dropna()
        n = len(v)
        if n == 0:
            return {"n": 0, "mean_pct": None, "median_pct": None, "win_rate_pct": None}
        return {
            "n": n,
            "mean_pct": round(float(v.mean()) * 100, 3),
            "median_pct": round(float(v.median()) * 100, 3),
            "win_rate_pct": round(float((v > 0).mean()) * 100, 1),
        }

    def permutation_pvalue(vals: pd.Series, mask: pd.Series, n_perm: int = 5000, seed: int = 42) -> float | None:
        """Two-sided permutation test on the mean-difference between mask==True vs False groups."""
        v = vals[mask.notna() & vals.notna()]
        m = mask[mask.notna() & vals.notna()]
        if m.nunique() < 2 or len(v) < 4:
            return None
        obs = v[m].mean() - v[~m].mean()
        rng = np.random.default_rng(seed)
        arr = v.to_numpy()
        m_arr = m.to_numpy()
        n_true = int(m_arr.sum())
        idx_all = np.arange(len(arr))
        diffs = np.empty(n_perm)
        for i in range(n_perm):
            perm_idx = rng.permutation(idx_all)
            grp = perm_idx[:n_true]
            other = perm_idx[n_true:]
            diffs[i] = arr[grp].mean() - arr[other].mean()
        p = float((np.abs(diffs) >= abs(obs)).mean())
        return p

    summary_rows = []

    for label, sum5d_col, during_col in [
        ("gov_bank", "gov_bank_sum_5d_before", "gov_bank_net_during_t0"),
        ("foreign_ratio", "foreign_ratio_chg_5d_before", "foreign_ratio_chg_during_t0"),
    ]:
        vals = join_df["excess_h9"]
        before = join_df[sum5d_col]
        mask_buy = before > 0  # same direction as whale buy -> CONTRADICTS reverse-indicator hypothesis
        mask_sell = before < 0  # opposite direction -> CONFIRMS reverse-indicator hypothesis
        s_buy = summarize(vals[mask_buy])
        s_sell = summarize(vals[mask_sell])
        p = permutation_pvalue(vals, mask_buy)
        summary_rows.append({"feature": label, "window": "5d_before_signal", "bucket": "gov/foreign_also_buying(contradicts)",
                              **s_buy})
        summary_rows.append({"feature": label, "window": "5d_before_signal", "bucket": "gov/foreign_selling(confirms_reverse)",
                              **s_sell})
        summary_rows.append({"feature": label, "window": "5d_before_signal", "bucket": "permutation_p_value(diff_of_means)",
                              "n": None, "mean_pct": p, "median_pct": None, "win_rate_pct": None})

        during = join_df[during_col]
        mask_buy_d = during > 0
        mask_sell_d = during < 0
        s_buy_d = summarize(vals[mask_buy_d])
        s_sell_d = summarize(vals[mask_sell_d])
        p_d = permutation_pvalue(vals, mask_buy_d)
        summary_rows.append({"feature": label, "window": "during_signal_day_T", "bucket": "gov/foreign_also_buying(contradicts)",
                              **s_buy_d})
        summary_rows.append({"feature": label, "window": "during_signal_day_T", "bucket": "gov/foreign_selling(confirms_reverse)",
                              **s_sell_d})
        summary_rows.append({"feature": label, "window": "during_signal_day_T", "bucket": "permutation_p_value(diff_of_means)",
                              "n": None, "mean_pct": p_d, "median_pct": None, "win_rate_pct": None})

    # combined: both gov-bank AND foreign ratio agree pre-signal
    both_confirm = (join_df["gov_bank_sum_5d_before"] < 0) & (join_df["foreign_ratio_chg_5d_before"] < 0)
    both_contradict = (join_df["gov_bank_sum_5d_before"] > 0) & (join_df["foreign_ratio_chg_5d_before"] > 0)
    s_both_confirm = summarize(join_df.loc[both_confirm, "excess_h9"])
    s_both_contradict = summarize(join_df.loc[both_contradict, "excess_h9"])
    summary_rows.append({"feature": "both_gov_and_foreign", "window": "5d_before_signal",
                          "bucket": "both_confirm_reverse(gov_sell&fr_down)", **s_both_confirm})
    summary_rows.append({"feature": "both_gov_and_foreign", "window": "5d_before_signal",
                          "bucket": "both_contradict(gov_buy&fr_up)", **s_both_contradict})

    all_events = summarize(join_df["excess_h9"])
    summary_rows.append({"feature": "ALL_EVENTS_BASELINE", "window": "n/a", "bucket": "no_split", **all_events})

    summary_df = pd.DataFrame(summary_rows)
    summary_df.to_csv(OUT_SUMMARY_CSV, index=False)
    log(f"wrote {OUT_SUMMARY_CSV}")
    print(summary_df.to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
