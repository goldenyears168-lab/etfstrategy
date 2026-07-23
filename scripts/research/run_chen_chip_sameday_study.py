#!/usr/bin/env python3
"""Chen-style chip × expert-pool whale same-day (L0) alignment study.

Research only. Vendors: tw-institutional-stocker (Apache-2.0), cftc-cot (MIT).
"""
from __future__ import annotations

import json
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from research.chen_chip.adapters_db import (  # noqa: E402
    connect_ro,
    load_bench,
    load_calendar,
    load_ohlc,
)
from research.chen_chip.features import build_chip_feature_frame  # noqa: E402
from research.chen_chip.signals import (  # noqa: E402
    ChipRule,
    apply_chip_rule,
    default_chen_rule,
    sweep_grid,
)
from research.chen_chip.whale_events import TIER_A, TIER_B, load_whale_events  # noqa: E402

OUT = ROOT / "reports/research/chen_chip_sameday"
OUT.mkdir(parents=True, exist_ok=True)
D0, D1 = "2024-07-01", "2026-07-16"
OOS_CUT = "2026-01-01"
COST = 0.003
BETA = 1.15
HOLDS = (7, 10, 14)


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def summarize(legs: list[dict], min_n: int = 5) -> dict:
    if len(legs) < min_n:
        return {
            "n": len(legs),
            "ok": False,
            "excess_med": np.nan,
            "excess_mean": np.nan,
            "win": np.nan,
            "slot_n": 0,
            "slot_med": np.nan,
        }
    xs = np.array([x["excess_pct"] for x in legs])
    taken, busy = [], None
    for lg in sorted(legs, key=lambda z: z["entry_date"]):
        if busy is not None and lg["entry_date"] <= busy:
            continue
        taken.append(lg)
        busy = lg["exit_date"]
    txs = np.array([x["excess_pct"] for x in taken]) if taken else np.array([])
    return {
        "n": len(legs),
        "ok": True,
        "excess_med": float(np.median(xs)),
        "excess_mean": float(np.mean(xs)),
        "win": float((xs > 0).mean() * 100),
        "slot_n": len(taken),
        "slot_med": float(np.median(txs)) if len(txs) else np.nan,
    }


def prf(chip_days: set[tuple[str, str]], whale_days: set[tuple[str, str]]) -> dict:
    if not chip_days and not whale_days:
        return {"tp": 0, "fp": 0, "fn": 0, "precision": np.nan, "recall": np.nan, "f1": np.nan}
    tp = len(chip_days & whale_days)
    fp = len(chip_days - whale_days)
    fn = len(whale_days - chip_days)
    prec = tp / (tp + fp) if (tp + fp) else 0.0
    rec = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
    return {"tp": tp, "fp": fp, "fn": fn, "precision": prec, "recall": rec, "f1": f1}


def lag_stats(chip_days: set[tuple[str, str]], whale: pd.DataFrame, cal: list[str], di: dict[str, int]) -> dict:
    """Share of whale events with chip fire on T-1 / T / T+1 (same sid)."""
    early = same = late = miss = 0
    for r in whale.itertuples():
        i = di.get(r.signal_date)
        if i is None:
            miss += 1
            continue
        keys = []
        if i > 0:
            keys.append(("early", (r.sid, cal[i - 1])))
        keys.append(("same", (r.sid, r.signal_date)))
        if i + 1 < len(cal):
            keys.append(("late", (r.sid, cal[i + 1])))
        hit = None
        for label, k in keys:
            if k in chip_days:
                hit = label
                break
        if hit == "early":
            early += 1
        elif hit == "same":
            same += 1
        elif hit == "late":
            late += 1
        else:
            miss += 1
    n = max(1, len(whale))
    return {
        "n_whale": len(whale),
        "pct_early": 100 * early / n,
        "pct_same": 100 * same / n,
        "pct_late": 100 * late / n,
        "pct_miss": 100 * miss / n,
    }


def make_leg_l0(
    sid: str,
    sig: str,
    hold: int,
    cal: list[str],
    di: dict[str, int],
    ohlc_px: dict[tuple[str, str], tuple[float, float]],
    bench: dict[str, float],
) -> dict | None:
    i = di.get(sig)
    if i is None:
        return None
    xi = i + hold
    if xi >= len(cal):
        return None
    ed, xd = cal[i], cal[xi]  # L0: enter signal close
    er = ohlc_px.get((sid, ed))
    xr = ohlc_px.get((sid, xd))
    be, bx = bench.get(ed), bench.get(xd)
    if not er or not xr or not be or not bx:
        return None
    _, entry_px = er
    _, exit_px = xr
    if entry_px <= 0 or exit_px <= 0:
        return None
    sr = exit_px / entry_px - 1.0 - COST
    br = bx / be - 1.0
    return {
        "signal_date": sig,
        "entry_date": ed,
        "exit_date": xd,
        "stock_id": sid,
        "stock_pct": sr * 100,
        "excess_pct": (sr - BETA * br) * 100,
        "lag": "L0",
        "hold": hold,
    }


def make_leg_l1(
    sid: str,
    sig: str,
    hold: int,
    cal: list[str],
    di: dict[str, int],
    ohlc_px: dict[tuple[str, str], tuple[float, float]],
    bench: dict[str, float],
) -> dict | None:
    i = di.get(sig)
    if i is None:
        return None
    ei, xi = i + 1, i + hold
    if xi >= len(cal):
        return None
    ed, xd = cal[ei], cal[xi]
    er = ohlc_px.get((sid, ed))
    xr = ohlc_px.get((sid, xd))
    be, bx = bench.get(ed), bench.get(xd)
    if not er or not xr or not be or not bx:
        return None
    entry_px, _ = er
    _, exit_px = xr
    if entry_px <= 0 or exit_px <= 0:
        return None
    sr = exit_px / entry_px - 1.0 - COST
    br = bx / be - 1.0
    return {
        "signal_date": sig,
        "entry_date": ed,
        "exit_date": xd,
        "stock_id": sid,
        "stock_pct": sr * 100,
        "excess_pct": (sr - BETA * br) * 100,
        "lag": "L1",
        "hold": hold,
    }


def score_rule_is(
    rule: ChipRule,
    feat: pd.DataFrame,
    whale: pd.DataFrame,
    cal: list[str],
    di: dict[str, int],
    ohlc_px,
    bench,
) -> dict:
    sub = feat[feat["d"] < OOS_CUT].copy()
    wh = whale[whale["signal_date"] < OOS_CUT]
    fire = apply_chip_rule(sub, rule)
    chip_days = set(zip(sub.loc[fire, "sid"], sub.loc[fire, "d"]))
    whale_days = set(zip(wh["sid"], wh["signal_date"]))
    m = prf(chip_days, whale_days)
    lag = lag_stats(chip_days, wh, cal, di)
    # L0 excess on intersection
    hold = 10
    legs = []
    for sid, d in chip_days & whale_days:
        lg = make_leg_l0(sid, d, hold, cal, di, ohlc_px, bench)
        if lg:
            legs.append(lg)
    st = summarize(legs, min_n=3)
    # whale baseline L0 on all whale IS days
    wlegs = []
    for r in wh.itertuples():
        lg = make_leg_l0(r.sid, r.signal_date, hold, cal, di, ohlc_px, bench)
        if lg:
            wlegs.append(lg)
    wst = summarize(wlegs, min_n=3)
    return {
        "rule": rule.name,
        **rule.to_dict(),
        **{f"align_{k}": v for k, v in m.items()},
        **{f"lag_{k}": v for k, v in lag.items()},
        "hit_n": st["n"],
        "hit_excess_med": st["excess_med"],
        "whale_n": wst["n"],
        "whale_excess_med": wst["excess_med"],
        "delta_vs_whale_pp": (
            st["excess_med"] - wst["excess_med"]
            if st["ok"] and wst["ok"]
            else np.nan
        ),
        "chip_fire_n": int(fire.sum()),
        "score": _objective(m, st, wst, wh),
    }


def _objective(m: dict, st: dict, wst: dict, wh: pd.DataFrame) -> float:
    """Higher better. Recall primary, then excess floor, then n coverage."""
    if not wh.shape[0]:
        return -1e9
    rec = float(m["recall"] or 0)
    prec = float(m["precision"] or 0)
    f1 = float(m["f1"] or 0)
    cov = st["n"] / max(1, len(wh))
    excess_ok = 0.0
    if st["ok"] and wst["ok"] and st["excess_med"] >= wst["excess_med"] - 1.0:
        excess_ok = 1.0
    # soft barriers
    if rec < 0.25:
        return rec * 10 + f1
    return 100 * rec + 40 * f1 + 20 * excess_ok + 15 * min(1.0, cov) + 10 * prec


def coverage_report(conn, sids: list[str]) -> pd.DataFrame:
    rows = []
    for sid in sids:
        inst_n = conn.execute(
            "SELECT COUNT(*), MIN(trade_date), MAX(trade_date) FROM stock_institutional_daily WHERE stock_id=? AND source='finmind' AND trade_date>=?",
            (sid, D0),
        ).fetchone()
        mar_n = conn.execute(
            "SELECT COUNT(*), MIN(trade_date), MAX(trade_date) FROM stock_margin_daily WHERE stock_id=? AND source='finmind' AND trade_date>=?",
            (sid, D0),
        ).fetchone()
        # Avoid full COUNT on 86M-row branch tape — existence probe only.
        br_hit = conn.execute(
            """
            SELECT 1 FROM stock_broker_branch_daily
            WHERE stock_id=? AND source='finmind' AND trade_date>=? LIMIT 1
            """,
            (sid, D0),
        ).fetchone()
        px_n = conn.execute(
            "SELECT COUNT(*), MIN(trade_date), MAX(trade_date) FROM stock_daily_bars WHERE stock_id=? AND source='finmind' AND trade_date>=?",
            (sid, D0),
        ).fetchone()
        rows.append(
            {
                "sid": sid,
                "inst_n": inst_n[0],
                "inst_min": inst_n[1],
                "inst_max": inst_n[2],
                "margin_n": mar_n[0],
                "branch_has_rows": bool(br_hit),
                "px_n": px_n[0],
                "px_max": px_n[2],
            }
        )
    return pd.DataFrame(rows)


def main() -> int:
    t0 = time.time()
    status = {"phase": "init", "at": datetime.now().isoformat(timespec="seconds")}
    (OUT / "status.json").write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")

    conn = connect_ro()
    universe = TIER_A + TIER_B
    log("E0 coverage")
    cov = coverage_report(conn, universe)
    cov.to_csv(OUT / "coverage_e0.csv", index=False)

    log("E1 whale calendar")
    whale_a = load_whale_events(TIER_A)
    whale_b = load_whale_events(TIER_B)
    whale_a.to_csv(OUT / "whale_events_tierA.csv", index=False)
    whale_b.to_csv(OUT / "whale_events_tierB.csv", index=False)
    whale = pd.concat([whale_a, whale_b], ignore_index=True)
    log(f"  TierA events={len(whale_a)} TierB={len(whale_b)}")

    log("E2 chip features")
    feat = build_chip_feature_frame(conn, universe, D0, D1)
    feat.to_csv(OUT / "chip_features.parquet".replace(".parquet", ".csv"), index=False)
    log(f"  feature rows={len(feat):,}")

    cal = load_calendar(conn, D0, D1)
    di = {d: i for i, d in enumerate(cal)}
    bench = load_bench(conn, D0, D1)
    ohlc = load_ohlc(conn, universe, D0, D1)
    ohlc_px = {(r.sid, r.d): (float(r.open), float(r.close)) for r in ohlc.itertuples()}
    conn.close()

    # Restrict feat/whale to dates with calendar
    whale = whale[whale["signal_date"].isin(di)].copy()
    whale_a = whale[whale["sid"].isin(TIER_A)].copy()

    log("E3 baseline chen alignment (Tier A)")
    base = default_chen_rule()
    fire = apply_chip_rule(feat, base)
    chip_days = set(zip(feat.loc[fire, "sid"], feat.loc[fire, "d"]))
    whale_days_a = set(zip(whale_a["sid"], whale_a["signal_date"]))
    # restrict chip to Tier A stocks for fair PR
    chip_days_a = {k for k in chip_days if k[0] in TIER_A}
    align_base = prf(chip_days_a, whale_days_a)
    lag_base = lag_stats(chip_days_a, whale_a, cal, di)
    (OUT / "alignment_chen_baseline.json").write_text(
        json.dumps({"rule": base.to_dict(), "align": align_base, "lag": lag_base}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    log(f"  baseline F1={align_base['f1']:.3f} P={align_base['precision']:.3f} R={align_base['recall']:.3f}")

    log("E4 IS sweep (Tier A features + whale)")
    feat_a = feat[feat["sid"].isin(TIER_A)].copy()
    sweep_rows = []
    rules = sweep_grid()
    log(f"  rules={len(rules)}")
    for i, rule in enumerate(rules):
        row = score_rule_is(rule, feat_a, whale_a, cal, di, ohlc_px, bench)
        sweep_rows.append(row)
        if (i + 1) % 40 == 0:
            log(f"  sweep {i+1}/{len(rules)}")
    sdf = pd.DataFrame(sweep_rows).sort_values("score", ascending=False)
    sdf.to_csv(OUT / "sweep_is.csv", index=False)
    best = sdf.iloc[0]
    best_rule = ChipRule(
        name=str(best["rule"]),
        foreign_streak_min=int(best["foreign_streak_min"]),
        trust_streak_min=int(best["trust_streak_min"]),
        three_streak_min=int(best.get("three_streak_min", 0) or 0),
        foreign_pctl20_min=float(best["foreign_pctl20_min"]),
        require_foreign_positive=bool(best["require_foreign_positive"]),
        require_trust_positive=bool(best["require_trust_positive"]),
        max_margin_surge=bool(best["max_margin_surge"]),
        tx_pos_streak_min=int(best.get("tx_pos_streak_min", 0) or 0),
        tx_rising_streak_min=int(best.get("tx_rising_streak_min", 0) or 0),
    )
    # Secondary freeze: best F1 among recall>=0.4
    f1_pick = sdf[sdf["align_recall"] >= 0.4].sort_values("align_f1", ascending=False)
    alt_rule = best_rule
    if len(f1_pick):
        alt = f1_pick.iloc[0]
        alt_rule = ChipRule(
            name=str(alt["rule"]),
            foreign_streak_min=int(alt["foreign_streak_min"]),
            trust_streak_min=int(alt["trust_streak_min"]),
            three_streak_min=int(alt.get("three_streak_min", 0) or 0),
            foreign_pctl20_min=float(alt["foreign_pctl20_min"]),
            require_foreign_positive=bool(alt["require_foreign_positive"]),
            require_trust_positive=bool(alt["require_trust_positive"]),
            max_margin_surge=bool(alt["max_margin_surge"]),
            tx_pos_streak_min=int(alt.get("tx_pos_streak_min", 0) or 0),
            tx_rising_streak_min=int(alt.get("tx_rising_streak_min", 0) or 0),
        )
    log(
        f"  BEST {best_rule.name} score={best['score']:.1f} "
        f"R={best['align_recall']:.2f} P={best['align_precision']:.2f} "
        f"hit_med={best['hit_excess_med']}"
    )
    log(f"  ALT_F1 {alt_rule.name}")
    (OUT / "frozen_rule.json").write_text(
        json.dumps(
            {
                "selected_on": "IS<" + OOS_CUT,
                "rule": best_rule.to_dict(),
                "alt_f1_rule": alt_rule.to_dict(),
                "is_metrics": best.to_dict(),
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )

    log("E5 L0 vs L1 excess grids (whale days + chip∩whale)")
    fire_frozen_all = apply_chip_rule(feat_a, best_rule)
    frozen_days_a = set(zip(feat_a.loc[fire_frozen_all, "sid"], feat_a.loc[fire_frozen_all, "d"]))
    grid_rows = []
    for label, days in [
        ("whale_all", whale_days_a),
        ("chen_baseline_hit", chip_days_a & whale_days_a),
        ("frozen_hit", frozen_days_a & whale_days_a),
    ]:
        for hold in HOLDS:
            for lag_name, maker in [("L0", make_leg_l0), ("L1", make_leg_l1)]:
                legs = []
                for sid, d in days:
                    lg = maker(sid, d, hold, cal, di, ohlc_px, bench)
                    if lg:
                        legs.append(lg)
                st = summarize(legs, min_n=5)
                grid_rows.append({"set": label, "lag": lag_name, "hold": hold, **st})
    gdf = pd.DataFrame(grid_rows)
    gdf.to_csv(OUT / "l0_l1_grid.csv", index=False)

    log("E6 OOS replay frozen rule")
    feat_oos = feat_a[feat_a["d"] >= OOS_CUT]
    wh_oos = whale_a[whale_a["signal_date"] >= OOS_CUT]
    fire_oos = apply_chip_rule(feat_oos, best_rule)
    chip_oos = set(zip(feat_oos.loc[fire_oos, "sid"], feat_oos.loc[fire_oos, "d"]))
    whale_oos = set(zip(wh_oos["sid"], wh_oos["signal_date"]))
    align_oos = prf(chip_oos, whale_oos)
    lag_oos = lag_stats(chip_oos, wh_oos, cal, di)
    oos_legs = []
    for sid, d in chip_oos & whale_oos:
        lg = make_leg_l0(sid, d, 10, cal, di, ohlc_px, bench)
        if lg:
            oos_legs.append(lg)
    oos_st = summarize(oos_legs, min_n=3)
    # also baseline OOS
    fire_b = apply_chip_rule(feat_oos, base)
    chip_b = set(zip(feat_oos.loc[fire_b, "sid"], feat_oos.loc[fire_b, "d"]))
    align_b_oos = prf(chip_b, whale_oos)
    oos_payload = {
        "oos_from": OOS_CUT,
        "frozen": {"align": align_oos, "lag": lag_oos, "hit_l0h10": oos_st},
        "chen_baseline": {"align": align_b_oos},
        "whale_oos_n": len(wh_oos),
    }
    (OUT / "oos_replay.json").write_text(json.dumps(oos_payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    pd.DataFrame(
        [
            {"rule": "frozen", **align_oos, **{f"lag_{k}": v for k, v in lag_oos.items()}, **{f"hit_{k}": v for k, v in oos_st.items()}},
            {"rule": "chen_baseline", **align_b_oos},
        ]
    ).to_csv(OUT / "oos_replay.csv", index=False)

    # Tier B diagnostic with frozen rule
    feat_b = feat[feat["sid"].isin(TIER_B)]
    fire_b2 = apply_chip_rule(feat_b, best_rule)
    chip_b2 = set(zip(feat_b.loc[fire_b2, "sid"], feat_b.loc[fire_b2, "d"]))
    whale_b_days = set(zip(whale_b["sid"], whale_b["signal_date"]))
    align_b = prf(chip_b2, whale_b_days)
    (OUT / "alignment_tierB_frozen.json").write_text(
        json.dumps({"align": align_b, "rule": best_rule.to_dict()}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    elapsed = (time.time() - t0) / 60
    status = {
        "phase": "done",
        "at": datetime.now().isoformat(timespec="seconds"),
        "elapsed_min": round(elapsed, 2),
        "frozen_rule": best_rule.name,
        "is_f1": float(best["align_f1"]),
        "oos_f1": float(align_oos["f1"]),
    }
    (OUT / "status.json").write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"DONE {elapsed:.1f}m → {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
