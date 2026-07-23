#!/usr/bin/env python3
"""Whale-event chip precursors (Tier A/B expert-pool) — Research only.

Ignore Chen SOP. Find common chip patterns in T-5..T-1 before whale
champion signal days, vs matched non-event control days. Encode rules,
IS/OOS validate.
"""
from __future__ import annotations

import json
import sys
import time
from collections import defaultdict
from datetime import datetime
from itertools import product
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from research.chen_chip.adapters_db import connect_ro, load_bench, load_calendar, load_ohlc  # noqa: E402
from research.chen_chip.features import build_chip_feature_frame  # noqa: E402
from research.chen_chip.whale_events import TIER_A, TIER_B, load_whale_events  # noqa: E402

OUT = ROOT / "reports/research/whale_chip_precursor"
OUT.mkdir(parents=True, exist_ok=True)
D0, D1, OOS = "2024-07-01", "2026-07-16", "2026-01-01"
COST, BETA = 0.003, 1.15
LAGS = (1, 2, 3, 4, 5)  # lookback from signal_date
HOLD = 10


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


def main() -> int:
    t0 = time.time()
    conn = connect_ro()
    universe = TIER_A + TIER_B
    log("load whale + features")
    whale = load_whale_events(universe)
    whale = whale[whale["signal_date"] >= D0].copy()
    feat = build_chip_feature_frame(conn, universe, D0, D1)
    cal = load_calendar(conn, D0, D1)
    di = {d: i for i, d in enumerate(cal)}
    bench = load_bench(conn, D0, D1)
    ohlc = load_ohlc(conn, universe, D0, D1)
    ohlc_px = {(r.sid, r.d): (float(r.open), float(r.close)) for r in ohlc.itertuples()}
    conn.close()

    feat = feat.set_index(["sid", "d"], drop=False)
    whale = whale[whale["signal_date"].isin(di)].copy()
    whale_a = whale[whale["sid"].isin(TIER_A)].copy()
    log(f"whale A={len(whale_a)} all={len(whale)} feat={len(feat)}")

    # --- Feature columns to profile ---
    num_cols = [
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
    ]
    num_cols = [c for c in num_cols if c in feat.columns]

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

    # --- Event panel: each whale day × lag ---
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

    # Controls: same stock, dates not within ±2 days of any whale signal for that stock
    whale_by_sid = defaultdict(set)
    for r in whale_a.itertuples():
        i = di[r.signal_date]
        for j in range(max(0, i - 2), min(len(cal), i + 3)):
            whale_by_sid[r.sid].add(cal[j])

    rng = np.random.default_rng(42)
    ctrl_rows = []
    target_n = len(ev)  # roughly match
    # pool candidate (sid,d) with features
    cand = feat[feat["sid"].isin(TIER_A)][["sid", "d"]].drop_duplicates()
    cand = cand[~cand.apply(lambda r: r["d"] in whale_by_sid.get(r["sid"], set()), axis=1)]
    # sample
    if len(cand) > target_n:
        cand = cand.sample(n=min(target_n * 2, len(cand)), random_state=42)
    for r in cand.itertuples():
        # fake lag tag by random for stratified compare — better: assign synthetic signal = d+k
        # Use each control day as "as-of" with lag label matching distribution of events
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
    panel.to_csv(OUT / "precursor_panel.csv", index=False)
    log(f"panel events={len(ev)} controls={len(ctrl)}")

    # --- Univariate: event vs control by lag (effect size) ---
    log("univariate lag profiles")
    uni_rows = []
    for k in LAGS:
        e = panel[(panel.label == 1) & (panel.lag == k)]
        c = panel[(panel.label == 0) & (panel.lag == k)]
        for col in num_cols:
            em, cm = e[col].median(skipna=True), c[col].median(skipna=True)
            # Cohen's d approx
            ea, ca = e[col].dropna(), c[col].dropna()
            if len(ea) < 8 or len(ca) < 8:
                continue
            pooled = np.sqrt(((ea.std() ** 2) + (ca.std() ** 2)) / 2) or np.nan
            d = (ea.mean() - ca.mean()) / pooled if pooled and pooled > 0 else np.nan
            # binary lift for positive
            e_pos = (ea > 0).mean()
            c_pos = (ca > 0).mean()
            uni_rows.append(
                {
                    "lag": k,
                    "feature": col,
                    "event_med": float(em) if pd.notna(em) else np.nan,
                    "ctrl_med": float(cm) if pd.notna(cm) else np.nan,
                    "delta_med": float(em - cm) if pd.notna(em) and pd.notna(cm) else np.nan,
                    "cohens_d": float(d) if pd.notna(d) else np.nan,
                    "event_pos_rate": float(e_pos),
                    "ctrl_pos_rate": float(c_pos),
                    "lift_pos": float(e_pos / c_pos) if c_pos > 0 else np.nan,
                    "n_event": len(ea),
                    "n_ctrl": len(ca),
                }
            )
        # margin surge rate
        uni_rows.append(
            {
                "lag": k,
                "feature": "margin_surge_rate",
                "event_med": float(e["margin_surge"].mean()),
                "ctrl_med": float(c["margin_surge"].mean()),
                "delta_med": float(e["margin_surge"].mean() - c["margin_surge"].mean()),
                "cohens_d": np.nan,
                "event_pos_rate": float(e["margin_surge"].mean()),
                "ctrl_pos_rate": float(c["margin_surge"].mean()),
                "lift_pos": np.nan,
                "n_event": len(e),
                "n_ctrl": len(c),
            }
        )
    uni = pd.DataFrame(uni_rows)
    uni.to_csv(OUT / "univariate_by_lag.csv", index=False)

    # Rank strongest precursors (mean |d| across lags 1-3)
    focus = uni[uni.lag.isin([1, 2, 3]) & (uni.feature != "margin_surge_rate")].copy()
    rank = (
        focus.groupby("feature")
        .agg(mean_abs_d=("cohens_d", lambda s: float(np.nanmean(np.abs(s)))), mean_d=("cohens_d", "mean"), mean_lift=("lift_pos", "mean"))
        .sort_values("mean_abs_d", ascending=False)
    )
    rank.to_csv(OUT / "feature_rank.csv")
    log("top features:\n" + rank.head(12).to_string())

    # --- Mine simple binary rules on lag-1 (strongest actionable day) ---
    # Rule form: feature threshold at T-k
    log("mine precursor rules @ lag1..3")
    rule_defs = []
    # from vendor / data-driven candidates
    candidates = [
        ("foreign_net", ">", 0),
        ("foreign_streak", ">=", 1),
        ("foreign_streak", ">=", 2),
        ("trust_net", ">", 0),
        ("trust_streak", ">=", 1),
        ("three_buy_net", ">", 0),
        ("three_streak", ">=", 2),
        ("foreign_pctl20", ">=", 0.5),
        ("foreign_pctl20", ">=", 0.7),
        ("foreign_sum_5d", ">", 0),
        ("three_sum_5d", ">", 0),
        ("tx_foreign_net_rising_streak", ">=", 1),
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
            ev_m = m[sub.label == 1]
            ct_m = m[sub.label == 0]
            if len(ev_m) < 5 or len(ct_m) < 5:
                continue
            er = float(ev_m.mean())
            cr = float(ct_m.mean())
            lift = er / cr if cr > 0 else np.nan
            # precision-like if we fire on all days: need day-level later
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
    mdf.to_csv(OUT / "mined_single_rules.csv", index=False)

    # Combinations of top singles at lag 1-2 (AND)
    top_singles = mdf[mdf.lag.isin([1, 2]) & (mdf.lift >= 1.15)].head(12)
    combos = []
    singles_list = top_singles.to_dict("records")
    for a, b in product(singles_list, singles_list):
        if a["rule"] >= b["rule"]:
            continue
        if a["lag"] != b["lag"]:
            continue  # same lag only for simplicity
        k = a["lag"]
        sub = panel[panel.lag == k]
        m1 = eval_mask(sub, a["feat"], a["op"], a["thr"])
        m2 = eval_mask(sub, b["feat"], b["op"], b["thr"])
        m = m1 & m2
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
        cdf.to_csv(OUT / "mined_combo_rules.csv", index=False)

    # --- Day-level fire: rule on T-k predicts whale on T (IS/OOS) ---
    log("day-level IS/OOS for precursor → whale@T")
    # Build map of whale days
    whale_set = set(zip(whale_a["sid"], whale_a["signal_date"]))

    def fire_days(rule_row, feat_df: pd.DataFrame) -> set[tuple[str, str]]:
        """Return set of (sid, signal_candidate_date=T) where precursor at T-k holds."""
        k = int(rule_row["lag"])
        out = set()
        # iterate feature rows; if rule holds on d, then T = d+k is candidate
        for r in feat_df.itertuples():
            # evaluate single feature rules only for day-level (parse from mined)
            pass
        return out

    # Simpler: for each Tier A stock calendar day as potential T, check precursor at T-k
    feat_a = feat[feat["sid"].isin(TIER_A)].reset_index(drop=True)
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
        if op == "==":
            return val == thr
        return False

    # Pick top rules for full day-level validation
    pick = mdf.head(8).to_dict("records")
    if len(cdf):
        # expand top 5 combos into structured form for eval
        for r in cdf.head(5).to_dict("records"):
            pick.append({"lag": r["lag"], "rule": r["rule"], "combo": True, "parts": r["parts"]})

    def parse_atom(s: str):
        # L1:foreign_net>0
        body = s.split(":", 1)[1]
        for op in (">=", ">", "=="):
            if op in body:
                f, thr = body.split(op, 1)
                thr_v: object
                if thr == "False":
                    thr_v = False
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
        # For each possible T in calendar for each sid
        fires = []  # (sid, T)
        for sid in TIER_A:
            for i, T in enumerate(cal):
                if i < k:
                    continue
                if T < D0 or T > D1:
                    continue
                d_pre = cal[i - k]
                ok = True
                if rule.get("combo"):
                    for part in rule["parts"]:
                        # part like L1:foreign_net>0
                        f, op, thr = parse_atom(part)
                        if not eval_day_rule(sid, d_pre, f, op, thr):
                            ok = False
                            break
                else:
                    f, op, thr = rule["feat"], rule["op"], rule["thr"]
                    ok = eval_day_rule(sid, d_pre, f, op, thr)
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
            # L0 excess on tp
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
    ddf.to_csv(OUT / "rule_daylevel_is_oos.csv", index=False)

    # Consensus precursor pattern: features with lift>1.2 at lag1 AND lag2
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
        cons_df.to_csv(OUT / "consensus_precursors.csv", index=False)

    # Best day-level rule by OOS f1 then recall
    oos = ddf[ddf.split == "OOS"].sort_values(["f1", "recall"], ascending=False)
    is_ = ddf[ddf.split == "IS"].sort_values(["f1", "recall"], ascending=False)

    # Briefing
    lines = []
    lines.append("# 大戶賺錢軌跡 · 籌碼前兆規律\n\n")
    lines.append(f"- 生成：{datetime.now().isoformat(timespec='seconds')}\n")
    lines.append(f"- 宇宙：Tier A {TIER_A}（調參）· Tier B 僅事件對照未納主掃\n")
    lines.append(f"- 事件：expert-pool 冠軍訊號日 n={len(whale_a)} · 前兆窗 T−5..T−1\n")
    lines.append(f"- 對照：同股非事件日 · OOS≥{OOS} · L0H{HOLD} 超額 β=1.15\n")
    lines.append("- **不採納陳式 SOP**；只看大戶軌跡前後的共同籌碼型態\n\n")

    lines.append("## 共同前兆（事件日相對對照日 · 正值率 lift）\n\n")
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
    for feat_name, r in rank.head(10).iterrows():
        lines.append(f"| {feat_name} | {r.mean_abs_d:.2f} | {r.mean_d:.2f} | {r.mean_lift:.2f} |\n")

    lines.append("\n## 候選規律（單條件 · 依 lift）\n\n")
    lines.append("| rule | event命中率 | ctrl | lift | Δpp |\n|------|----------:|-----:|-----:|----:|\n")
    for r in mdf.head(12).itertuples():
        lines.append(
            f"| `{r.rule}` | {r.event_hit_rate:.0%} | {r.ctrl_hit_rate:.0%} | "
            f"{r.lift:.2f} | {r.delta_pp:+.1f} |\n"
        )

    if len(cdf):
        lines.append("\n## 組合規律（同 lag AND）\n\n")
        lines.append("| rule | lift | event | ctrl |\n|------|-----:|------:|-----:|\n")
        for r in cdf.head(8).itertuples():
            lines.append(f"| `{r.rule}` | {r.lift:.2f} | {r.event_hit_rate:.0%} | {r.ctrl_hit_rate:.0%} |\n")

    lines.append("\n## 日曆級驗證（前兆@T−k → 預測大戶@T）\n\n")
    lines.append("| rule | split | P | R | F1 | fire | hit L0H10 med |\n|------|-------|--:|--:|----:|-----:|--------------:|\n")
    show = pd.concat([is_.head(6), oos.head(6)]) if len(ddf) else ddf
    for r in show.itertuples():
        med = f"{r.hit_l0h10_med:+.1f}%" if pd.notna(r.hit_l0h10_med) else "—"
        lines.append(
            f"| `{r.rule}` | {r.split} | {r.precision:.2f} | {r.recall:.2f} | "
            f"{r.f1:.2f} | {int(r.fire_n)} | {med} |\n"
        )

    lines.append("\n## 收成規律（暫定 · Research）\n\n")
    lines.append("在 Tier A 大戶訊號出現前 1–2 日，相對同股安靜日，較常出現：\n\n")
    if len(cons_df):
        for r in cons_df.head(5).itertuples():
            lines.append(f"1. **{r.feature}** 偏多／偏強（lag1 lift {r.lag1_lift:.2f}）\n")
    else:
        # fallback from rank
        for feat_name, r in rank.head(5).iterrows():
            direction = "偏高" if r.mean_d > 0 else "偏低"
            lines.append(f"1. **{feat_name}** {direction}（mean d={r.mean_d:.2f}）\n")

    lines.append("\n**可操作草案（需 OOS 複核）**：\n\n")
    lines.append("```\n")
    lines.append("IF 外資淨買連續 ≥1～2 日 且 foreign_pctl20 ≥ 0.5（T−1 或 T−2）\n")
    lines.append("AND 投信淨買 ≥0（不強烈賣超）\n")
    lines.append("AND 非融資暴增\n")
    lines.append("THEN 標記「大戶可能臨近」— 仍須分點共識確認，不單獨開倉\n")
    lines.append("```\n\n")
    lines.append("## 限制\n\n")
    lines.append("- 前兆 ≠ 因果；與大戶同日分點行為可能共因（題材／漲勢）。\n")
    lines.append("- Precision 仍可能偏低：籌碼前兆會在許多非共識日出現。\n")
    lines.append("- 本輪未改 live watch；僅研究。\n")

    (OUT / "BRIEFING.md").write_text("".join(lines), encoding="utf-8")
    status = {
        "phase": "done",
        "at": datetime.now().isoformat(timespec="seconds"),
        "elapsed_min": round((time.time() - t0) / 60, 2),
        "whale_a": len(whale_a),
        "top_features": list(rank.head(5).index) if len(rank) else [],
    }
    (OUT / "status.json").write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"DONE → {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
