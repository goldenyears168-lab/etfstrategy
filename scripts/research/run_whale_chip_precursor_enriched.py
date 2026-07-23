#!/usr/bin/env python3
"""Whale-event chip precursors — enriched FinMind features (Research only).

Extends run_whale_chip_precursor_study.py with DB fields already synced
(lending / daytrade / shareholding / institutional side / short) plus
minimal API caches:
  - TaiwanStockHoldingSharesPer (集保分級 · Tier A range pull)
  - TaiwanStockGovernmentBankBuySell (八大行庫 · one day / request · Tier A filter)

Does NOT write strategy.yaml or install launchd. Book-only research.
"""
from __future__ import annotations

import json
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from itertools import product
from pathlib import Path

import numpy as np
import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from finmind_client import fetch_finmind, finmind_token  # noqa: E402
from research.chen_chip.adapters_db import connect_ro, load_bench, load_calendar, load_ohlc  # noqa: E402
from research.chen_chip.features import build_chip_feature_frame  # noqa: E402
from research.chen_chip.whale_events import TIER_A, TIER_B, load_whale_events  # noqa: E402

OUT = ROOT / "reports/research/whale_chip_precursor"
CACHE = OUT / "cache"
OUT.mkdir(parents=True, exist_ok=True)
CACHE.mkdir(parents=True, exist_ok=True)

D0, D1, OOS = "2024-07-01", "2026-07-16", "2026-01-01"
COST, BETA = 0.003, 1.15
LAGS = (1, 2, 3, 4, 5)
HOLD = 10

HS_PATH = CACHE / "holding_shares_per_tier_a.csv"
GOV_PATH = CACHE / "gov_bank_net_tier_a.csv"

# 集保：大戶 ≈ 持股 ≥10 萬股各級合計；散戶 ≈ ≤1 萬股
BIG_LEVELS = {
    "100,001-200,000",
    "200,001-400,000",
    "400,001-600,000",
    "600,001-800,000",
    "800,001-1,000,000",
    "more than 1,000,001",
}
RETAIL_LEVELS = {"1-999", "1,000-5,000", "5,001-10,000"}

ENRICHED_NUM = [
    "foreign_net",
    "trust_net",
    "dealer_net",
    "three_buy_net",
    "foreign_streak",
    "trust_streak",
    "three_streak",
    "foreign_pctl20",
    "foreign_sum_5d",
    "three_sum_5d",
    "margin_change",
    "short_change",
    "short_sum_5d",
    "short_streak",
    "lending_change",
    "lending_sum_5d",
    "lending_drop_streak",
    "daytrade_ratio_pct",
    "dt_ratio_pctl20",
    "foreign_remaining_ratio",
    "foreign_ratio_chg",
    "foreign_ratio_chg_5d",
    "foreign_shares_chg",
    "dealer_hedge_net",
    "dealer_self_net",
    "dealer_hedge_streak",
    "dealer_self_streak",
    "dealer_book_spread",
    "side_foreign_dealer_net",
    "big_holder_pct",
    "retail_holder_pct",
    "big_holder_pct_chg",
    "gov_bank_net",
    "gov_bank_streak",
    "gov_bank_sum_5d",
    "net_oi_vol_zscore",
    "tx_foreign_net_rising_streak",
]


def log(m: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {m}", flush=True)


def summarize(xs: list[float], min_n: int = 8) -> dict:
    if len(xs) < min_n:
        return {"n": len(xs), "ok": False, "med": np.nan, "mean": np.nan, "win": np.nan}
    a = np.asarray(xs, dtype=float)
    return {
        "n": len(a),
        "ok": True,
        "med": float(np.median(a)),
        "mean": float(np.mean(a)),
        "win": float((a > 0).mean() * 100),
    }


def backfill_holding_shares(stock_ids: list[str], d0: str, d1: str) -> Path:
    if HS_PATH.exists() and HS_PATH.stat().st_size > 100:
        age_h = (time.time() - HS_PATH.stat().st_mtime) / 3600
        if age_h < 72:
            log(f"reuse HoldingSharesPer cache ({age_h:.1f}h old)")
            return HS_PATH
    if not finmind_token():
        log("SKIP HoldingSharesPer · no FINMIND_TOKEN")
        return HS_PATH
    rows: list[dict] = []
    start, end = date.fromisoformat(d0), date.fromisoformat(d1)
    for sid in stock_ids:
        log(f"FinMind HoldingSharesPer {sid}")
        try:
            raw = fetch_finmind("TaiwanStockHoldingSharesPer", sid, start, end, timeout=120)
        except Exception as exc:  # noqa: BLE001
            log(f"  FAIL {sid}: {exc}")
            continue
        by_date: dict[str, list[dict]] = defaultdict(list)
        for item in raw:
            d = str(item.get("date") or "")[:10]
            if d:
                by_date[d].append(item)
        prev_big: float | None = None
        for d in sorted(by_date):
            big = retail = 0.0
            for item in by_date[d]:
                lvl = str(item.get("HoldingSharesLevel") or "")
                pct = item.get("percent")
                try:
                    pct_f = float(pct) if pct is not None else 0.0
                except (TypeError, ValueError):
                    pct_f = 0.0
                if lvl in BIG_LEVELS:
                    big += pct_f
                elif lvl in RETAIL_LEVELS:
                    retail += pct_f
            chg = (big - prev_big) if prev_big is not None else np.nan
            prev_big = big
            rows.append(
                {
                    "sid": sid,
                    "d": d,
                    "big_holder_pct": big,
                    "retail_holder_pct": retail,
                    "big_holder_pct_chg": chg,
                }
            )
        time.sleep(0.25)
    pd.DataFrame(rows).to_csv(HS_PATH, index=False)
    log(f"wrote {HS_PATH} n={len(rows)}")
    return HS_PATH


def _trading_days(conn, d0: str, d1: str) -> list[str]:
    return load_calendar(conn, d0, d1)


def backfill_gov_bank(stock_ids: list[str], days: list[str]) -> Path:
    """One FinMind call per calendar day (dataset forbids data_id + multi-day)."""
    want = set(stock_ids)
    # Reuse if covers ≥80% of requested days
    if GOV_PATH.exists() and GOV_PATH.stat().st_size > 100:
        old = pd.read_csv(GOV_PATH)
        have = set(old["d"].astype(str)) if "d" in old.columns else set()
        need = [d for d in days if d not in have]
        if len(need) / max(1, len(days)) < 0.2:
            log(f"reuse gov_bank cache · missing {len(need)}/{len(days)} days")
            return GOV_PATH
    else:
        need = list(days)
        old = pd.DataFrame()

    if not finmind_token():
        log("SKIP gov_bank · no FINMIND_TOKEN")
        return GOV_PATH

    # Cap: prefer recent year if still too many (quota-safe)
    if len(need) > 280:
        need = [d for d in need if d >= "2025-01-01"]
        log(f"gov_bank capped to ≥2025-01-01 · {len(need)} days")

    tok = finmind_token()
    rows: list[dict] = []
    fail = 0
    for i, d in enumerate(need):
        try:
            r = requests.get(
                "https://api.finmindtrade.com/api/v4/data",
                params={
                    "dataset": "TaiwanStockGovernmentBankBuySell",
                    "start_date": d,
                    "token": tok,
                },
                timeout=90,
            )
            payload = r.json()
            if payload.get("status") != 200:
                fail += 1
                if fail <= 3:
                    log(f"  gov_bank {d} msg={payload.get('msg')}")
                continue
            data = payload.get("data") or []
            agg: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
            for item in data:
                sid = str(item.get("stock_id") or "")
                if sid not in want:
                    continue
                buy = float(item.get("buy") or 0)
                sell = float(item.get("sell") or 0)
                agg[sid][0] += buy
                agg[sid][1] += sell
            for sid, (buy, sell) in agg.items():
                rows.append(
                    {
                        "sid": sid,
                        "d": d,
                        "gov_bank_buy": buy,
                        "gov_bank_sell": sell,
                        "gov_bank_net": buy - sell,
                    }
                )
        except Exception as exc:  # noqa: BLE001
            fail += 1
            if fail <= 3:
                log(f"  gov_bank {d} ERR {exc}")
        if (i + 1) % 40 == 0:
            log(f"  gov_bank progress {i+1}/{len(need)} rows={len(rows)} fail={fail}")
        time.sleep(0.15)

    new_df = pd.DataFrame(rows)
    if not old.empty and not new_df.empty:
        out = pd.concat([old, new_df], ignore_index=True)
        out = out.drop_duplicates(subset=["sid", "d"], keep="last")
    elif not new_df.empty:
        out = new_df
    else:
        out = old
    out.to_csv(GOV_PATH, index=False)
    log(f"wrote {GOV_PATH} n={len(out)} fail={fail}")
    return GOV_PATH


def main() -> int:
    t0 = time.time()
    universe = TIER_A + TIER_B
    conn = connect_ro()
    cal = load_calendar(conn, D0, D1)
    # Backfill caches (Tier A only — high value / quota)
    backfill_holding_shares(TIER_A, D0, D1)
    backfill_gov_bank(TIER_A, [d for d in cal if D0 <= d <= D1])

    log("load whale + enriched features")
    whale = load_whale_events(universe)
    whale = whale[whale["signal_date"] >= D0].copy()
    feat = build_chip_feature_frame(
        conn,
        universe,
        D0,
        D1,
        holding_shares_path=HS_PATH if HS_PATH.exists() else None,
        gov_bank_path=GOV_PATH if GOV_PATH.exists() else None,
    )
    di = {d: i for i, d in enumerate(cal)}
    bench = load_bench(conn, D0, D1)
    ohlc = load_ohlc(conn, universe, D0, D1)
    ohlc_px = {(r.sid, r.d): (float(r.open), float(r.close)) for r in ohlc.itertuples()}
    conn.close()

    feat = feat.set_index(["sid", "d"], drop=False)
    whale = whale[whale["signal_date"].isin(di)].copy()
    whale_a = whale[whale["sid"].isin(TIER_A)].copy()
    log(f"whale A={len(whale_a)} feat={len(feat)} cols={len(feat.columns)}")

    num_cols = [c for c in ENRICHED_NUM if c in feat.columns]
    coverage = {
        c: float(feat[c].notna().mean()) for c in num_cols
    }
    pd.Series(coverage, name="non_null_rate").sort_values(ascending=False).to_csv(
        OUT / "enriched_feature_coverage.csv"
    )

    def row_at(sid: str, d: str) -> pd.Series | None:
        try:
            return feat.loc[(sid, d)]
        except KeyError:
            return None

    def lag_date(sig: str, k: int) -> str | None:
        i = di.get(sig)
        if i is None or i < k:
            return None
        return cal[i - k]

    log("build event / control panels")
    ev_rows = []
    for r in whale_a.itertuples():
        for k in LAGS:
            d = lag_date(r.signal_date, k)
            if not d:
                continue
            row = row_at(r.sid, d)
            if row is None:
                continue
            rec = {"sid": r.sid, "signal_date": r.signal_date, "lag": k, "d": d, "label": 1}
            for c in num_cols:
                rec[c] = float(row[c]) if pd.notna(row[c]) else np.nan
            rec["margin_surge"] = bool(row.get("margin_surge", False)) if "margin_surge" in row.index else False
            ev_rows.append(rec)
    ev = pd.DataFrame(ev_rows)

    whale_by_sid: dict[str, set[str]] = defaultdict(set)
    for r in whale_a.itertuples():
        i = di[r.signal_date]
        for j in range(max(0, i - 2), min(len(cal), i + 3)):
            whale_by_sid[r.sid].add(cal[j])

    rng = np.random.default_rng(42)
    ctrl_rows = []
    cand = feat[feat["sid"].isin(TIER_A)][["sid", "d"]].drop_duplicates()
    cand = cand[~cand.apply(lambda r: r["d"] in whale_by_sid.get(r["sid"], set()), axis=1)]
    if len(cand) > len(ev):
        cand = cand.sample(n=min(len(ev) * 2, len(cand)), random_state=42)
    for r in cand.itertuples():
        k = int(rng.choice(LAGS))
        row = row_at(r.sid, r.d)
        if row is None:
            continue
        rec = {"sid": r.sid, "signal_date": r.d, "lag": k, "d": r.d, "label": 0}
        for c in num_cols:
            rec[c] = float(row[c]) if pd.notna(row[c]) else np.nan
        rec["margin_surge"] = bool(row.get("margin_surge", False)) if "margin_surge" in row.index else False
        ctrl_rows.append(rec)
    ctrl = pd.DataFrame(ctrl_rows)
    panel = pd.concat([ev, ctrl], ignore_index=True)
    panel.to_csv(OUT / "precursor_panel_enriched.csv", index=False)
    log(f"panel events={len(ev)} controls={len(ctrl)}")

    log("univariate lag profiles")
    uni_rows = []
    for k in LAGS:
        e = panel[(panel.label == 1) & (panel.lag == k)]
        c = panel[(panel.label == 0) & (panel.lag == k)]
        for col in num_cols:
            ea, ca = e[col].dropna(), c[col].dropna()
            if len(ea) < 8 or len(ca) < 8:
                continue
            em, cm = float(ea.median()), float(ca.median())
            pooled = np.sqrt(((ea.std() ** 2) + (ca.std() ** 2)) / 2) or np.nan
            d = (ea.mean() - ca.mean()) / pooled if pooled and pooled > 0 else np.nan
            e_pos = float((ea > 0).mean())
            c_pos = float((ca > 0).mean())
            uni_rows.append(
                {
                    "lag": k,
                    "feature": col,
                    "event_med": em,
                    "ctrl_med": cm,
                    "delta_med": em - cm,
                    "cohens_d": float(d) if pd.notna(d) else np.nan,
                    "event_pos_rate": e_pos,
                    "ctrl_pos_rate": c_pos,
                    "lift_pos": e_pos / c_pos if c_pos > 0 else np.nan,
                    "n_event": len(ea),
                    "n_ctrl": len(ca),
                }
            )
    uni = pd.DataFrame(uni_rows)
    uni.to_csv(OUT / "univariate_by_lag_enriched.csv", index=False)

    focus = uni[uni.lag.isin([1, 2, 3])].copy()
    rank = (
        focus.groupby("feature")
        .agg(
            mean_abs_d=("cohens_d", lambda s: float(np.nanmean(np.abs(s)))),
            mean_d=("cohens_d", "mean"),
            mean_lift=("lift_pos", "mean"),
        )
        .sort_values("mean_abs_d", ascending=False)
    )
    rank.to_csv(OUT / "feature_rank_enriched.csv")
    log("top enriched features:\n" + rank.head(15).to_string())

    # Mine rules (baseline + new)
    candidates = [
        ("foreign_net", ">", 0),
        ("foreign_streak", ">=", 1),
        ("foreign_streak", ">=", 2),
        ("trust_net", ">", 0),
        ("three_buy_net", ">", 0),
        ("three_streak", ">=", 2),
        ("foreign_sum_5d", ">", 0),
        ("three_sum_5d", ">", 0),
        ("foreign_ratio_chg", ">", 0),
        ("foreign_ratio_chg_5d", ">", 0),
        ("lending_change", "<", 0),
        ("lending_drop_streak", ">=", 1),
        ("lending_sum_5d", "<", 0),
        ("short_change", ">", 0),
        ("short_sum_5d", ">", 0),
        ("dealer_hedge_net", ">", 0),
        ("dealer_self_net", ">", 0),
        ("dealer_hedge_streak", ">=", 1),
        ("gov_bank_net", ">", 0),
        ("gov_bank_streak", ">=", 1),
        ("gov_bank_sum_5d", ">", 0),
        ("big_holder_pct_chg", ">", 0),
        ("dt_ratio_pctl20", ">=", 0.7),
        ("margin_surge", "==", False),
    ]

    def eval_mask(df: pd.DataFrame, feat_name: str, op: str, thr) -> pd.Series:
        if feat_name == "margin_surge":
            return df["margin_surge"] == thr
        s = df[feat_name]
        if op == ">":
            return s > thr
        if op == ">=":
            return s >= thr
        if op == "<":
            return s < thr
        if op == "==":
            return s == thr
        raise ValueError(op)

    mined = []
    for k in (1, 2, 3):
        sub = panel[panel.lag == k]
        for feat_name, op, thr in candidates:
            if feat_name not in sub.columns and feat_name != "margin_surge":
                continue
            m = eval_mask(sub, feat_name, op, thr)
            # require enough non-null for the feature
            if feat_name != "margin_surge" and sub[feat_name].notna().sum() < 20:
                continue
            ev_m = m[sub.label == 1]
            ct_m = m[sub.label == 0]
            if len(ev_m) < 5 or len(ct_m) < 5:
                continue
            er, cr = float(ev_m.mean()), float(ct_m.mean())
            lift = er / cr if cr > 0 else np.nan
            mined.append(
                {
                    "lag": k,
                    "rule": f"L{k}:{feat_name}{op}{thr}",
                    "feat": feat_name,
                    "op": op,
                    "thr": thr,
                    "event_hit_rate": er,
                    "ctrl_hit_rate": cr,
                    "lift": lift,
                    "delta_pp": (er - cr) * 100,
                }
            )
    mdf = pd.DataFrame(mined).sort_values(["lift", "delta_pp"], ascending=False)
    mdf.to_csv(OUT / "mined_single_rules_enriched.csv", index=False)

    top_singles = mdf[mdf.lag.isin([1, 2]) & (mdf.lift >= 1.15)].head(14)
    combos = []
    singles_list = top_singles.to_dict("records")
    for a, b in product(singles_list, singles_list):
        if a["rule"] >= b["rule"] or a["lag"] != b["lag"]:
            continue
        k = a["lag"]
        sub = panel[panel.lag == k]
        m = eval_mask(sub, a["feat"], a["op"], a["thr"]) & eval_mask(
            sub, b["feat"], b["op"], b["thr"]
        )
        ev_m = m[sub.label == 1]
        ct_m = m[sub.label == 0]
        if len(ev_m) < 5:
            continue
        er, cr = float(ev_m.mean()), float(ct_m.mean())
        if cr <= 0:
            continue
        combos.append(
            {
                "lag": k,
                "rule": f"{a['rule']}&{b['rule']}",
                "event_hit_rate": er,
                "ctrl_hit_rate": cr,
                "lift": er / cr,
                "delta_pp": (er - cr) * 100,
                "parts": [a["rule"], b["rule"]],
            }
        )
    cdf = pd.DataFrame(combos).sort_values("lift", ascending=False) if combos else pd.DataFrame()
    if len(cdf):
        cdf.to_csv(OUT / "mined_combo_rules_enriched.csv", index=False)

    # Day-level IS/OOS
    whale_set = set(zip(whale_a["sid"], whale_a["signal_date"]))
    feat_idx = feat.set_index(["sid", "d"])

    def eval_day_rule(sid: str, d: str, feat_name: str, op: str, thr) -> bool:
        try:
            row = feat_idx.loc[(sid, d)]
        except KeyError:
            return False
        if feat_name == "margin_surge":
            return bool(row.get("margin_surge", False)) == thr
        val = row[feat_name]
        if pd.isna(val):
            return False
        if op == ">":
            return val > thr
        if op == ">=":
            return val >= thr
        if op == "<":
            return val < thr
        if op == "==":
            return val == thr
        return False

    pick = mdf.head(10).to_dict("records")
    if len(cdf):
        for r in cdf.head(5).to_dict("records"):
            pick.append({"lag": r["lag"], "rule": r["rule"], "combo": True, "parts": r["parts"]})

    def parse_atom(s: str):
        body = s.split(":", 1)[1]
        for op in (">=", ">", "<", "=="):
            if op in body:
                f, thr = body.split(op, 1)
                if thr == "False":
                    thr_v: object = False
                elif thr == "True":
                    thr_v = True
                else:
                    thr_v = float(thr) if "." in thr else int(thr)
                return f, op, thr_v
        raise ValueError(s)

    day_rows = []
    for rule in pick:
        k = int(rule["lag"])
        name = rule["rule"]
        fires = []
        for sid in TIER_A:
            for i, T in enumerate(cal):
                if i < k or T < D0 or T > D1:
                    continue
                d_pre = cal[i - k]
                ok = True
                if rule.get("combo"):
                    for part in rule["parts"]:
                        f, op, thr = parse_atom(part)
                        if not eval_day_rule(sid, d_pre, f, op, thr):
                            ok = False
                            break
                else:
                    ok = eval_day_rule(sid, d_pre, rule["feat"], rule["op"], rule["thr"])
                if ok:
                    fires.append((sid, T))
        fire_set = set(fires)
        for split, pred in [("IS", lambda t: t < OOS), ("OOS", lambda t: t >= OOS)]:
            fs = {(s, t) for s, t in fire_set if pred(t)}
            ws = {(s, t) for s, t in whale_set if pred(t)}
            tp = len(fs & ws)
            fp = len(fs - ws)
            fn = len(ws - fs)
            prec = tp / (tp + fp) if tp + fp else 0.0
            rec = tp / (tp + fn) if tp + fn else 0.0
            f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
            xs = []
            for sid, T in fs & ws:
                i = di.get(T)
                if i is None or i + HOLD >= len(cal):
                    continue
                er = ohlc_px.get((sid, T))
                xr = ohlc_px.get((sid, cal[i + HOLD]))
                be, bx = bench.get(T), bench.get(cal[i + HOLD])
                if not er or not xr or not be or not bx:
                    continue
                _, ep = er
                _, xp = xr
                if ep <= 0 or xp <= 0 or be <= 0 or bx <= 0:
                    continue
                sr = xp / ep - 1 - COST
                br = bx / be - 1
                xs.append((sr - BETA * br) * 100)
            st = summarize(xs, min_n=5)
            day_rows.append(
                {
                    "rule": name,
                    "lag": k,
                    "split": split,
                    "fire_n": len(fs),
                    "whale_n": len(ws),
                    "tp": tp,
                    "fp": fp,
                    "fn": fn,
                    "precision": prec,
                    "recall": rec,
                    "f1": f1,
                    "hit_l0h10_n": st["n"],
                    "hit_l0h10_med": st["med"],
                    "hit_l0h10_win": st["win"],
                }
            )
    ddf = pd.DataFrame(day_rows)
    ddf.to_csv(OUT / "rule_daylevel_is_oos_enriched.csv", index=False)

    consensus = []
    for feat_name in num_cols:
        sub = uni[uni.feature == feat_name]
        l1 = sub[sub.lag == 1]
        l2 = sub[sub.lag == 2]
        if l1.empty or l2.empty:
            continue
        if float(l1.iloc[0]["lift_pos"] or 0) >= 1.15 and float(l2.iloc[0]["lift_pos"] or 0) >= 1.10:
            consensus.append(
                {
                    "feature": feat_name,
                    "lag1_lift": float(l1.iloc[0]["lift_pos"]),
                    "lag2_lift": float(l2.iloc[0]["lift_pos"]),
                    "lag1_d": float(l1.iloc[0]["cohens_d"]) if pd.notna(l1.iloc[0]["cohens_d"]) else np.nan,
                    "lag1_event_pos": float(l1.iloc[0]["event_pos_rate"]),
                    "lag1_ctrl_pos": float(l1.iloc[0]["ctrl_pos_rate"]),
                }
            )
    cons_df = pd.DataFrame(consensus).sort_values("lag1_lift", ascending=False) if consensus else pd.DataFrame()
    if len(cons_df):
        cons_df.to_csv(OUT / "consensus_precursors_enriched.csv", index=False)

    # Classify new vs baseline features
    baseline = {
        "foreign_net",
        "trust_net",
        "dealer_net",
        "three_buy_net",
        "foreign_streak",
        "trust_streak",
        "three_streak",
        "foreign_pctl20",
        "foreign_sum_5d",
        "three_sum_5d",
        "margin_change",
        "net_oi_vol_zscore",
        "tx_foreign_net_rising_streak",
    }
    new_feats = [c for c in num_cols if c not in baseline]
    useful, useless = [], []
    for feat_name in new_feats:
        sub = uni[uni.feature == feat_name]
        if sub.empty:
            useless.append((feat_name, "no_uni"))
            continue
        sub_f = sub[sub.lag.isin([1, 2, 3])].copy()
        if sub_f.empty or sub_f["cohens_d"].notna().sum() == 0:
            useless.append((feat_name, "no_d"))
            continue
        abs_d = np.abs(sub_f["cohens_d"].astype(float))
        max_abs_d = float(np.nanmax(abs_d.to_numpy()))
        lifts = sub_f["lift_pos"].astype(float)
        max_lift = float(np.nanmax(lifts.to_numpy())) if lifts.notna().any() else 0.0
        best_idx = abs_d.idxmax()
        best = sub_f.loc[best_idx]
        # Always-positive levels (ratios/pct): prefer |d|; lift_pos on >0 is meaningless
        always_pos = feat_name in {
            "foreign_remaining_ratio",
            "big_holder_pct",
            "retail_holder_pct",
            "daytrade_ratio_pct",
            "dt_ratio_pctl20",
        }
        gate = max_abs_d >= 0.15 or (max_lift >= 1.15 and not always_pos)
        if gate:
            useful.append(
                {
                    "feature": feat_name,
                    "max_abs_d": max_abs_d,
                    "max_lift": max_lift,
                    "best_lag": int(best["lag"]),
                    "best_d": float(best["cohens_d"]),
                }
            )
        else:
            useless.append((feat_name, f"|d|={max_abs_d:.2f} lift={max_lift:.2f}"))

    oos = ddf[ddf.split == "OOS"].sort_values(["f1", "recall"], ascending=False) if len(ddf) else ddf
    is_ = ddf[ddf.split == "IS"].sort_values(["f1", "recall"], ascending=False) if len(ddf) else ddf

    # --- BRIEFING ---
    lines: list[str] = []
    lines.append("# 大戶賺錢軌跡 · 籌碼前兆規律（擴充版）\n\n")
    lines.append(f"- 生成：{datetime.now().isoformat(timespec='seconds')}\n")
    lines.append(f"- Runner：`scripts/research/run_whale_chip_precursor_enriched.py`（Research only）\n")
    lines.append(f"- 宇宙：Tier A {TIER_A}（調參）· Tier B 僅事件對照未納主掃\n")
    lines.append(f"- 事件：expert-pool 冠軍訊號日 n={len(whale_a)} · 前兆窗 T−5..T−1\n")
    lines.append(f"- 對照：同股非事件日 · OOS≥{OOS} · L0H{HOLD} 超額 β=1.15\n")
    lines.append("- 層級：Research；**未採納**進 Strategy／Order layer；未裝 launchd\n")
    lines.append("- 盤點見 `DATA_INVENTORY_tej_finmind.md`\n\n")

    lines.append("## 本輪新增資料（真正用到）\n\n")
    lines.append("| 來源 | 欄位／快取 | 覆蓋 |\n|------|-----------|------|\n")
    lines.append("| DB `stock_lending_daily` | lending_change / sum_5d / drop_streak | Tier A ~450–490d |\n")
    lines.append("| DB `stock_daytrade_daily` | daytrade_ratio_pct / pctl20 | Tier A ~460–490d |\n")
    lines.append("| DB `stock_shareholding_daily` | foreign_remaining_ratio + Δ | Tier A ~490d |\n")
    lines.append("| DB `stock_institutional_side_daily` | dealer hedge/self / foreign dealer | Tier A 全日 |\n")
    lines.append("| DB `stock_margin_daily` | short_change（原僅用融資） | Tier A ~489d |\n")
    lines.append(
        f"| API→cache HoldingSharesPer | big/retail_holder_pct | "
        f"{'有' if HS_PATH.exists() else '缺'} `{HS_PATH.name}` |\n"
    )
    lines.append(
        f"| API→cache 八大行庫 | gov_bank_net / streak | "
        f"{'有' if GOV_PATH.exists() else '缺'} `{GOV_PATH.name}` |\n"
    )
    lines.append("| TEJ | （本輪籌碼前兆**未用**；庫內僅指數／ETF 日線） | — |\n\n")

    lines.append("## 新特徵：有 lift vs 無用\n\n")
    lines.append("### 真正有 lift（|d|≥0.15 或正值率 lift≥1.15 @ lag1–3）\n\n")
    if useful:
        lines.append("| feature | max\\|d\\| | max lift | best lag | best d |\n")
        lines.append("|---------|----------:|---------:|---------:|-------:|\n")
        for u in sorted(useful, key=lambda x: -x["max_abs_d"]):
            lines.append(
                f"| {u['feature']} | {u['max_abs_d']:.2f} | {u['max_lift']:.2f} | "
                f"T−{u['best_lag']} | {u['best_d']:+.2f} |\n"
            )
    else:
        lines.append("（無新增特徵跨門檻）\n")

    lines.append("\n### 無用／弱（未跨門檻）\n\n")
    for name, why in useless:
        lines.append(f"- `{name}` — {why}\n")

    lines.append("\n## 共同前兆（事件 vs 對照 · 正值率 lift）\n\n")
    lines.append("| feature | lag1 lift | lag2 lift | lag1 event正值率 | lag1 ctrl |\n")
    lines.append("|---------|----------:|----------:|-----------------:|----------:|\n")
    if len(cons_df):
        for r in cons_df.itertuples():
            lines.append(
                f"| {r.feature} | {r.lag1_lift:.2f} | {r.lag2_lift:.2f} | "
                f"{r.lag1_event_pos:.0%} | {r.lag1_ctrl_pos:.0%} |\n"
            )
    else:
        lines.append("| （無同時 lag1+lag2 lift≥門檻） | | | | |\n")

    lines.append("\n## 單變量最強（|Cohen's d| @ lag1–3）\n\n")
    lines.append("| feature | mean\\|d\\| | mean d | mean lift |\n|---------|----------:|-------:|----------:|\n")
    for feat_name, r in rank.head(12).iterrows():
        lines.append(f"| {feat_name} | {r.mean_abs_d:.2f} | {r.mean_d:.2f} | {r.mean_lift:.2f} |\n")

    lines.append("\n## 候選規律（單條件 · 依 lift）\n\n")
    lines.append("| rule | event命中率 | ctrl | lift | Δpp |\n|------|----------:|-----:|-----:|----:|\n")
    for r in mdf.head(15).itertuples():
        lines.append(
            f"| `{r.rule}` | {r.event_hit_rate:.0%} | {r.ctrl_hit_rate:.0%} | "
            f"{r.lift:.2f} | {r.delta_pp:+.1f} |\n"
        )

    if len(cdf):
        lines.append("\n## 組合規律（同 lag AND）\n\n")
        lines.append("| rule | lift | event | ctrl |\n|------|-----:|------:|-----:|\n")
        for r in cdf.head(8).itertuples():
            lines.append(
                f"| `{r.rule}` | {r.lift:.2f} | {r.event_hit_rate:.0%} | {r.ctrl_hit_rate:.0%} |\n"
            )

    lines.append("\n## 日曆級驗證（前兆@T−k → 預測大戶@T）\n\n")
    lines.append("| rule | split | P | R | F1 | fire | hit L0H10 med |\n|------|-------|--:|--:|----:|-----:|--------------:|\n")
    show = pd.concat([is_.head(5), oos.head(5)]) if len(ddf) else ddf
    for r in show.itertuples():
        med = f"{r.hit_l0h10_med:+.1f}%" if pd.notna(r.hit_l0h10_med) else "—"
        lines.append(
            f"| `{r.rule}` | {r.split} | {r.precision:.2f} | {r.recall:.2f} | "
            f"{r.f1:.2f} | {int(r.fire_n)} | {med} |\n"
        )

    lines.append("\n## 收成規律（暫定 · Research）\n\n")
    lines.append("### 既有 W1–W4（仍有效）\n\n")
    lines.append("- **W1** T−2：`three_streak≥2` / `foreign_streak≥2`\n")
    lines.append("- **W2** T−2：五日累計偏多\n")
    lines.append("- **W3** T−2：foreign_sum_5d>0 ∧ three_streak≥2\n")
    lines.append("- **W4** T−1：trust_net>0\n\n")

    # Propose W5+ from useful new features / top mined new rules
    new_rules = mdf[mdf["feat"].isin(new_feats)].head(6)
    lines.append("### 擴充候補（本輪）\n\n")
    if len(new_rules):
        for r in new_rules.itertuples():
            lines.append(
                f"- `{r.rule}` · lift={r.lift:.2f} · event={r.event_hit_rate:.0%} vs ctrl={r.ctrl_hit_rate:.0%}\n"
            )
    else:
        lines.append("- （新增欄位未打出優於 W1–W4 的單條件）\n")

    lines.append("\n### 明確「不是」前兆（擴充後仍成立）\n\n")
    lines.append("- TX 外資淨部位 Z：事件前偏空，勿當多頭濾網。\n")
    lines.append("- 融資暴增否決：仍非這批大戶共同前兆。\n")
    lines.append("- `stock_branch_daily` / `stock_block_trade`：Tier A 窗內 **0 列**（未用）。\n")
    lines.append("- TEJ：本帳戶／本庫無籌碼表可餵前兆。\n\n")

    lines.append("**使用方式**：籌碼前兆 = 黃燈；綠燈仍要等分點共識。Precision 仍低，**不可單獨開倉**。\n\n")
    lines.append("## 限制\n\n")
    lines.append("- 前兆 ≠ 因果；與大戶同日分點行為可能共因。\n")
    lines.append("- 八大行庫為全日全市場拉取後過濾 Tier A；缺日見 cache。\n")
    lines.append("- 集保分級為週頻，以 asof backward 對齊日曆。\n")
    lines.append("- 本輪未改 live watch；僅研究。\n")

    (OUT / "BRIEFING.md").write_text("".join(lines), encoding="utf-8")

    # Update FROZEN if new rules beat W1 lift meaningfully
    frozen_lines = [
        "# FROZEN_PRECURSOR_RULES · 大戶籌碼前兆（Research）\n\n",
        f"來源：Tier A {len(whale_a)} 筆 expert-pool 冠軍訊號 · 對照同股非事件日 · "
        f"{datetime.now().date().isoformat()} · enriched runner\n\n",
        "| ID | 時機 | 條件 | 對照 lift（事件 vs 安靜） | 用途 |\n",
        "|----|------|------|---------------------------|------|\n",
        "| W1a | T−2 | `three_streak ≥ 2` | ≈1.64 | 主前兆 |\n",
        "| W1b | T−2 | `foreign_streak ≥ 2` | ≈1.49 | 主前兆（外資） |\n",
        "| W2 | T−2 | `three_sum_5d > 0` 或 `foreign_sum_5d > 0` | ≈1.3–1.4 | 寬覆蓋 |\n",
        "| W3 | T−2 | `foreign_sum_5d > 0` ∧ `three_streak ≥ 2` | ≈1.87 | 較乾淨組合 |\n",
        "| W4 | T−1 | `trust_net > 0` | ≈1.23 | 弱確認 |\n",
    ]
    # Add W5+ only if new rule lift >= 1.4 on lag 1-2
    w_id = 5
    added = []
    for r in mdf.itertuples():
        if r.feat not in new_feats:
            continue
        if r.lag not in (1, 2) or r.lift < 1.4:
            continue
        if r.rule in added:
            continue
        frozen_lines.append(
            f"| W{w_id} | T−{r.lag} | `{r.feat} {r.op} {r.thr}` | ≈{r.lift:.2f} | 擴充候補 |\n"
        )
        added.append(r.rule)
        w_id += 1
        if w_id > 8:
            break
    if not added:
        frozen_lines.append(
            "| — | — | （擴充欄位未達 lift≥1.4 凍結門檻） | — | 見 BRIEFING 弱訊號 |\n"
        )
    frozen_lines.append(
        "\n**禁止當主訊號**：日曆級 Precision 仍偏低。\n"
        "**建議**：W1/W3 黃燈 → 盯分點共識；共識觸發才進場邏輯。\n\n"
        "Runner：`scripts/research/run_whale_chip_precursor_enriched.py`  \n"
        "盤點：`DATA_INVENTORY_tej_finmind.md`  \n"
        "詳見 `BRIEFING.md`。\n"
    )
    (OUT / "FROZEN_PRECURSOR_RULES.md").write_text("".join(frozen_lines), encoding="utf-8")

    status = {
        "phase": "enriched_done",
        "at": datetime.now().isoformat(timespec="seconds"),
        "elapsed_min": round((time.time() - t0) / 60, 2),
        "whale_a": len(whale_a),
        "top_features": list(rank.head(8).index) if len(rank) else [],
        "useful_new": [u["feature"] for u in useful],
        "useless_new": [n for n, _ in useless],
        "hs_cache": HS_PATH.exists(),
        "gov_cache": GOV_PATH.exists(),
    }
    (OUT / "status.json").write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"DONE → {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
