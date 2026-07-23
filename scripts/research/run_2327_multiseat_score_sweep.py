#!/usr/bin/env python3
"""2327 國巨 · multi-seat expert branch discrete score sweep.

Champion core seats from trades/2327.json (+ top-5 extra by activity).
Discrete score: net>0→+1 · ≥0.5億→+2 · ≥1億→+3.
Aggregates: score sum@D · sum(D+D−1) · count net>0@D (k∈{1,2,3}).
Sequence: best-seat accel · any-2-seats co-buy D∧D−1.
IS-freeze thresholds (n≥8) · H=7 · D+1 open · non-overlap.
vs Whale_T+1 · frozen 8840 accel≥IS-p90.

Research only · 未採納 · Book only · sid=2327 tape only.

    PYTHONPATH=src .venv/bin/python scripts/research/run_2327_multiseat_score_sweep.py
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from itertools import combinations
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
from research.chen_chip.whale_events import load_whale_events  # noqa: E402
from stock_db import DEFAULT_DB_PATH  # noqa: E402

SID = "2327"
BENCH_SEAT = "8840"
OUT = ROOT / "reports/research/whale_chip_precursor"
TRADES_DIR = (
    ROOT
    / "reports/research/branch-footprint-screen/expert_pool/knowledge/trades"
)
SOURCE = "finmind"
D0, D1, OOS = "2024-07-01", "2026-07-16", "2026-01-01"
COST, BETA = 0.003, 1.15
HOLD = 7
FLOOR_0P5 = 5e7
FLOOR_1 = 1e8
MIN_IS_N = 8
N_BOOT = 5000
RNG = np.random.default_rng(42)
FOREIGN_KW = (
    "美商", "摩根", "瑞銀", "美林", "港商", "港麥", "麥格理", "野村", "瑞士",
    "花旗", "渣打", "香港", "新加坡", "大和", "法銀", "德意志", "巴克萊", "高盛", "美銀",
)
SCORE_SUM_D_THRS = (3, 4, 5, 6, 7, 8, 9, 10, 11, 12)
SCORE_SUM_DD1_THRS = (4, 6, 8, 10, 12, 14, 16, 18)
COUNT_K = (1, 2, 3)
MAX_EXTRA = 5


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def is_foreign(name: str) -> bool:
    return any(k in (name or "") for k in FOREIGN_KW)


def seat_score(amt: float) -> int:
    if amt >= FLOOR_1:
        return 3
    if amt >= FLOOR_0P5:
        return 2
    if amt > 0:
        return 1
    return 0


def excess_l1(
    signal_d: str,
    hold: int,
    cal: list[str],
    di: dict[str, int],
    ohlc_px: dict[tuple[str, str], tuple[float, float]],
    bench: dict[str, float],
) -> tuple[float | None, str | None, str | None]:
    i = di.get(signal_d)
    if i is None or i + 1 >= len(cal) or i + hold >= len(cal):
        return None, None, None
    ed = cal[i + 1]
    xd = cal[i + hold]
    er = ohlc_px.get((SID, ed))
    xr = ohlc_px.get((SID, xd))
    be, bx = bench.get(ed), bench.get(xd)
    if not er or not xr or not be or not bx:
        return None, None, None
    eo, _ = er
    _, xp = xr
    if eo <= 0 or xp <= 0 or be <= 0 or bx <= 0:
        return None, None, None
    sr = xp / eo - 1.0 - COST
    br = bx / be - 1.0
    return (sr - BETA * br) * 100.0, ed, xd


def summarize(xs: list[float]) -> dict:
    a = np.asarray(xs, dtype=float)
    n = int(a.size)
    if n == 0:
        return {"n": 0, "hit_rate": np.nan, "med": np.nan, "mean": np.nan, "sum": np.nan}
    return {
        "n": n,
        "hit_rate": float((a > 0).mean()),
        "med": float(np.median(a)),
        "mean": float(a.mean()),
        "sum": float(a.sum()),
    }


def bootstrap_delta(a: list[float], b: list[float]) -> dict:
    aa = np.asarray(a, dtype=float)
    bb = np.asarray(b, dtype=float)
    if aa.size == 0 or bb.size == 0:
        return {"delta_mean": np.nan, "p_delta_le0": np.nan}
    d0 = float(aa.mean() - bb.mean())
    if aa.size < 5 or bb.size < 5:
        return {"delta_mean": d0, "p_delta_le0": np.nan}
    idx_a = RNG.integers(0, aa.size, size=(N_BOOT, aa.size))
    idx_b = RNG.integers(0, bb.size, size=(N_BOOT, bb.size))
    deltas = aa[idx_a].mean(axis=1) - bb[idx_b].mean(axis=1)
    return {"delta_mean": d0, "p_delta_le0": float((deltas <= 0).mean())}


def fetch_tape(conn, dates: list[str]) -> pd.DataFrame:
    if not dates:
        return pd.DataFrame(columns=["d", "tid", "tname", "net"])
    uniq = sorted(set(dates))
    parts: list[pd.DataFrame] = []
    chunk = 40
    for i in range(0, len(uniq), chunk):
        batch = uniq[i : i + chunk]
        ph = ",".join("?" * len(batch))
        df = pd.read_sql_query(
            f"""
            SELECT trade_date AS d, securities_trader_id AS tid,
                   securities_trader AS tname, net
            FROM stock_broker_branch_daily
            WHERE source=? AND stock_id=? AND trade_date IN ({ph})
            """,
            conn,
            params=[SOURCE, SID, *batch],
        )
        if not df.empty:
            parts.append(df)
    if not parts:
        return pd.DataFrame(columns=["d", "tid", "tname", "net"])
    out = pd.concat(parts, ignore_index=True)
    out["net"] = pd.to_numeric(out["net"], errors="coerce").fillna(0.0)
    out["tid"] = out["tid"].astype(str)
    out["d"] = out["d"].astype(str)
    return out


def standalone_trades(
    fire_days: set[str],
    cal: list[str],
    di: dict[str, int],
    ohlc_px: dict,
    bench: dict,
    rule: str,
    arm: str = "multiseat",
) -> list[dict]:
    rows: list[dict] = []
    next_free = -1
    for j, d in enumerate(cal):
        if d < D0 or d > D1 or d not in fire_days:
            continue
        if j < next_free:
            continue
        x, ed, xd = excess_l1(d, HOLD, cal, di, ohlc_px, bench)
        if x is None or ed is None or xd is None:
            continue
        exit_j = di.get(xd)
        if exit_j is None:
            continue
        next_free = exit_j + 1
        rows.append(
            {
                "signal_date": d,
                "entry_date": ed,
                "exit_date": xd,
                "arm": arm,
                "rule": rule,
                "excess_pct": x,
                "split": "IS" if d < OOS else "OOS",
            }
        )
    return rows


def metrics_for(trades: list[dict], whale_xs: list[float]) -> dict:
    flat: dict = {}
    for sp in ("full", "IS", "OOS"):
        if sp == "full":
            xs = [t["excess_pct"] for t in trades]
        else:
            xs = [t["excess_pct"] for t in trades if t["split"] == sp]
        sm = summarize(xs)
        boot = bootstrap_delta(xs, whale_xs) if xs and whale_xs else {}
        for k, v in {**sm, **boot}.items():
            flat[f"{sp}_{k}"] = v
    return flat


def fmt_pct(v: float | None, digits: int = 1) -> str:
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "—"
    return f"{v:+.{digits}f}"


def fmt_rate(v: float | None) -> str:
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "—"
    return f"{100.0 * v:.0f}%"


def md_table(headers: list[str], rows: list[list[str]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "|" + "|".join(["---"] * len(headers)) + "|",
    ]
    for r in rows:
        lines.append("| " + " | ".join(r) + " |")
    return "\n".join(lines)


def compare_verdict(a_mean: float, b_mean: float, tol: float = 1.0) -> str:
    if pd.isna(a_mean) or pd.isna(b_mean):
        return "—"
    d = a_mean - b_mean
    if d >= tol:
        return "更好"
    if d <= -tol:
        return "更差"
    return "差不多"


def build_panel_features(
    cal: list[str],
    cal_ext: list[str],
    di: dict[str, int],
    seats: list[str],
    branch_amt: dict[str, dict[str, float]],
) -> pd.DataFrame:
    """Compact day panel with aggregate + sequence features."""
    rows: list[dict] = []
    n_seats = len(seats)
    for d in cal:
        i = di.get(d)
        if i is None or i < 1:
            continue
        dm1 = cal[i - 1]
        amts_d = [float(branch_amt.get(d, {}).get(t, 0.0) or 0.0) for t in seats]
        amts_m = [float(branch_amt.get(dm1, {}).get(t, 0.0) or 0.0) for t in seats]
        scores_d = [seat_score(a) for a in amts_d]
        scores_m = [seat_score(a) for a in amts_m]
        sum_d = sum(scores_d)
        sum_dd1 = sum_d + sum(scores_m)
        count_net = sum(1 for a in amts_d if a > 0)

        # best-seat accel among seats with max score on D
        max_sc = max(scores_d) if scores_d else 0
        best_accel = False
        if max_sc > 0:
            for j, sc in enumerate(scores_d):
                if sc == max_sc and amts_m[j] > 0 and amts_d[j] > amts_m[j]:
                    best_accel = True
                    break

        # any 2 champion seats net>0 both D and D-1
        net_both = sum(1 for j in range(n_seats) if amts_d[j] > 0 and amts_m[j] > 0)
        cobuy2_both = net_both >= 2

        rows.append(
            {
                "d": d,
                "split": "IS" if d < OOS else "OOS",
                "sum_d": sum_d,
                "sum_dd1": sum_dd1,
                "count_net": count_net,
                "best_accel": best_accel,
                "cobuy2_both": cobuy2_both,
            }
        )
    return pd.DataFrame(rows)


def fire_days_from_mask(panel: pd.DataFrame, mask: pd.Series) -> set[str]:
    return set(panel.loc[mask, "d"].astype(str).tolist())


def sweep_threshold_rules(
    panel: pd.DataFrame,
    col: str,
    thrs: tuple[int, ...],
    op: str,
    prefix: str,
    cal_ext: list[str],
    di: dict[str, int],
    ohlc_px: dict,
    bench: dict,
    whale_full: list[float],
    seq_mask: pd.Series | None = None,
    seq_suffix: str = "",
) -> list[dict]:
    """IS-freeze threshold; return one row per rule variant."""
    results: list[dict] = []
    is_panel = panel[panel["split"] == "IS"]
    for thr in thrs:
        if op == "ge":
            base = panel[col] >= thr
        else:
            base = panel[col] == thr
        mask = base & seq_mask if seq_mask is not None else base
        is_mask = mask & (panel["split"] == "IS")
        is_days = fire_days_from_mask(panel, is_mask)
        is_tr = standalone_trades(is_days, cal_ext, di, ohlc_px, bench, rule="tmp")
        is_m = summarize([t["excess_pct"] for t in is_tr if t["split"] == "IS"])
        if is_m["n"] < MIN_IS_N or pd.isna(is_m["mean"]) or is_m["mean"] < 0:
            continue
        full_days = fire_days_from_mask(panel, mask)
        rule_id = f"{prefix}{thr}{seq_suffix}"
        label = f"{prefix}≥{thr}{seq_suffix}" if op == "ge" else f"{prefix}={thr}{seq_suffix}"
        full_tr = standalone_trades(full_days, cal_ext, di, ohlc_px, bench, rule=rule_id)
        m = metrics_for(full_tr, whale_full)
        results.append(
            {
                "rule_id": rule_id,
                "label": label,
                "family": prefix.rstrip("_"),
                "thr": thr,
                "seq": seq_suffix.lstrip("_") or "none",
                "is_n": is_m["n"],
                "is_mean": is_m["mean"],
                "fires_full": len(full_days),
                **m,
                "trades": full_tr,
            }
        )
    return results


def pick_best(results: list[dict], key: str = "full_mean") -> dict | None:
    valid = [r for r in results if r.get("is_n", 0) >= MIN_IS_N and r.get("is_mean", -999) >= 0]
    if not valid:
        return None
    return max(valid, key=lambda r: (r.get(key, -999), r.get("full_n", 0)))


def fire_8840_accel_p90(
    cal: list[str],
    di: dict[str, int],
    branch_amt: dict[str, dict[str, float]],
    seat: str | None = None,
) -> tuple[set[str], float]:
    amts_is: list[float] = []
    for d in cal:
        if d >= OOS:
            continue
        a0 = branch_amt.get(d, {}).get(seat or BENCH_SEAT, 0.0)
        if a0 > 0:
            amts_is.append(a0)
    thr90 = float(np.quantile(amts_is, 0.90)) if len(amts_is) >= 30 else FLOOR_1
    fires: set[str] = set()
    for d in cal:
        i = di.get(d)
        if i is None or i < 1:
            continue
        dm1 = cal[i - 1]
        a0 = branch_amt.get(d, {}).get(seat or BENCH_SEAT, 0.0)
        a1 = branch_amt.get(dm1, {}).get(seat or BENCH_SEAT, 0.0)
        if a1 > 0 and a0 > a1 and a0 >= thr90:
            fires.add(d)
    return fires, thr90


def main() -> int:
    global D0, OOS, SID, BENCH_SEAT
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sid", default="2327", help="stock id (default 2327)")
    ap.add_argument("--d0", default=D0, help="window start (IS begins here)")
    ap.add_argument("--d1", default=D1, help="window end")
    ap.add_argument("--oos", default=OOS, help="OOS cut (dates >= cut are OOS)")
    ap.add_argument(
        "--bench-only",
        action="store_true",
        help="skip score sweep; only Whale_T+1 + frozen 8840 accel≥p90",
    )
    args = ap.parse_args()
    SID = str(args.sid)
    D0 = str(args.d0)
    d1 = str(args.d1)
    OOS = str(args.oos)
    t0 = time.time()
    OUT.mkdir(parents=True, exist_ok=True)

    doc = json.loads((TRADES_DIR / f"{SID}.json").read_text(encoding="utf-8"))
    core = {str(k): str(v) for k, v in (doc.get("core") or {}).items()}
    if not core:
        raise SystemExit(f"empty core in trades/{SID}.json")
    champ_list = list(core.keys())
    name_map: dict[str, str] = dict(core)
    if SID == "2327" and "8840" in core:
        BENCH_SEAT = "8840"
    else:
        BENCH_SEAT = champ_list[0]

    log(
        f"{SID} multiseat score sweep · seat={BENCH_SEAT} · db={DEFAULT_DB_PATH} · "
        f"D0={D0} D1={d1} OOS={OOS} bench_only={args.bench_only}"
    )

    conn = connect_ro(DEFAULT_DB_PATH)
    whale = load_whale_events([SID])
    whale = whale[(whale["signal_date"] >= D0) & (whale["signal_date"] <= d1)].copy()
    greens = sorted(whale["signal_date"].astype(str).tolist())
    log(f"Whale greens n={len(greens)}")

    cal = [d for d in load_calendar(conn, D0, d1) if D0 <= d <= d1]
    cal_ext = load_calendar(conn, D0, "2026-08-31")
    di = {d: i for i, d in enumerate(cal_ext)}
    bench = load_bench(conn, D0, d1)
    ohlc = load_ohlc(conn, [SID], D0, d1)
    ohlc_px = {
        (str(r.sid), str(r.d)): (float(r.open), float(r.close))
        for r in ohlc.itertuples()
    }
    px_close = {(str(r.sid), str(r.d)): float(r.close) for r in ohlc.itertuples()}

    log(f"load sid={SID} branch tape (chunked)")
    # Local DB branch floor is 2024-07-01; skip empty pre-floor days for speed.
    BRANCH_FLOOR = "2024-07-01"
    tape_cal = [d for d in cal if d >= BRANCH_FLOOR]
    if tape_cal != cal:
        log(
            f"tape fetch clipped {cal[0]}.. → {tape_cal[0]}.. "
            f"(pre-floor empty; cal still {len(cal)} days)"
        )
    tape = fetch_tape(conn, tape_cal)
    conn.close()
    tape = tape[~tape["tname"].map(is_foreign)].copy()
    tape["amt"] = tape["net"] * tape["d"].map(lambda x: px_close.get((SID, str(x)), np.nan))

    branch_amt: dict[str, dict[str, float]] = {}
    seat_activity: dict[str, float] = {}
    for r in tape.itertuples():
        d, tid = str(r.d), str(r.tid)
        amt = float(r.amt) if pd.notna(r.amt) else 0.0
        branch_amt.setdefault(d, {})[tid] = amt
        if r.tname and tid not in name_map:
            name_map[tid] = str(r.tname)
        if amt > 0:
            seat_activity[tid] = seat_activity.get(tid, 0.0) + amt

    extra = [
        t for t, _ in sorted(seat_activity.items(), key=lambda kv: -kv[1])
        if t not in core
    ][:MAX_EXTRA]
    seats = list(dict.fromkeys([*champ_list, *extra]))
    log(f"seats n={len(seats)} core={champ_list} extra={extra}")

    # benchmarks
    whale_trades = standalone_trades(set(greens), cal_ext, di, ohlc_px, bench, "Whale_T+1", "Whale_T+1")
    whale_m = metrics_for(whale_trades, [])
    whale_full_xs = [t["excess_pct"] for t in whale_trades]

    def mget(m: dict, sp: str, k: str):
        return m.get(f"{sp}_{k}", np.nan)

    fire_8840, thr90 = fire_8840_accel_p90(cal, di, branch_amt)
    tr_8840 = standalone_trades(fire_8840, cal_ext, di, ohlc_px, bench, "8840_accel_p90", "8840_frozen")
    m_8840 = metrics_for(tr_8840, whale_full_xs)
    branch_active = sum(1 for d in cal if d in branch_amt)
    log(
        f"8840 fires={len(fire_8840)} thr90={thr90/1e8:.3f}億 · "
        f"cal_days={len(cal)} branch_active={branch_active}"
    )

    best: dict | None = None
    all_results: list[dict] = []
    if not args.bench_only:
        panel = build_panel_features(cal, cal_ext, di, seats, branch_amt)
        panel.to_csv(OUT / f"{SID}_multiseat_day_panel.csv", index=False)
        log(
            f"panel n={len(panel)} IS={int((panel.split=='IS').sum())} "
            f"OOS={int((panel.split=='OOS').sum())}"
        )

        # sweep
        seq_accel = panel["best_accel"]
        seq_cobuy = panel["cobuy2_both"]

        for col, thrs, prefix in (
            ("sum_d", SCORE_SUM_D_THRS, "score_sum_d_"),
            ("sum_dd1", SCORE_SUM_DD1_THRS, "score_sum_dd1_"),
        ):
            all_results.extend(
                sweep_threshold_rules(
                    panel, col, thrs, "ge", prefix, cal_ext, di, ohlc_px, bench, whale_full_xs
                )
            )
            all_results.extend(
                sweep_threshold_rules(
                    panel, col, thrs, "ge", prefix, cal_ext, di, ohlc_px, bench, whale_full_xs,
                    seq_mask=seq_accel, seq_suffix="_AND_best_accel",
                )
            )
            all_results.extend(
                sweep_threshold_rules(
                    panel, col, thrs, "ge", prefix, cal_ext, di, ohlc_px, bench, whale_full_xs,
                    seq_mask=seq_cobuy, seq_suffix="_AND_cobuy2",
                )
            )

        for k in COUNT_K:
            mask = panel["count_net"] >= k
            is_days = fire_days_from_mask(panel, mask & (panel["split"] == "IS"))
            is_tr = standalone_trades(is_days, cal_ext, di, ohlc_px, bench, "tmp")
            is_m = summarize([t["excess_pct"] for t in is_tr if t["split"] == "IS"])
            if is_m["n"] >= MIN_IS_N and pd.notna(is_m["mean"]) and is_m["mean"] >= 0:
                full_days = fire_days_from_mask(panel, mask)
                rule_id = f"count_net_ge{k}"
                full_tr = standalone_trades(full_days, cal_ext, di, ohlc_px, bench, rule_id)
                m = metrics_for(full_tr, whale_full_xs)
                all_results.append(
                    {
                        "rule_id": rule_id,
                        "label": f"count_net≥{k}@D",
                        "family": "count_net",
                        "thr": k,
                        "seq": "none",
                        "is_n": is_m["n"],
                        "is_mean": is_m["mean"],
                        "fires_full": len(full_days),
                        **m,
                        "trades": full_tr,
                    }
                )
            for seq_mask, suffix in ((seq_accel, "_AND_best_accel"), (seq_cobuy, "_AND_cobuy2")):
                comb = mask & seq_mask
                is_days = fire_days_from_mask(panel, comb & (panel["split"] == "IS"))
                is_tr = standalone_trades(is_days, cal_ext, di, ohlc_px, bench, "tmp")
                is_m = summarize([t["excess_pct"] for t in is_tr if t["split"] == "IS"])
                if is_m["n"] >= MIN_IS_N and pd.notna(is_m["mean"]) and is_m["mean"] >= 0:
                    full_days = fire_days_from_mask(panel, comb)
                    rule_id = f"count_net_ge{k}{suffix}"
                    full_tr = standalone_trades(full_days, cal_ext, di, ohlc_px, bench, rule_id)
                    m = metrics_for(full_tr, whale_full_xs)
                    all_results.append(
                        {
                            "rule_id": rule_id,
                            "label": f"count_net≥{k}{suffix.replace('_AND_', ' ∧ ')}",
                            "family": "count_net",
                            "thr": k,
                            "seq": suffix.lstrip("_"),
                            "is_n": is_m["n"],
                            "is_mean": is_m["mean"],
                            "fires_full": len(full_days),
                            **m,
                            "trades": full_tr,
                        }
                    )

        # sequence-only
        for seq_col, rule_id, label in (
            ("best_accel", "seq_best_accel", "best-seat accel @D"),
            ("cobuy2_both", "seq_cobuy2_both", "≥2 seats net>0 D∧D−1"),
        ):
            mask = panel[seq_col]
            is_days = fire_days_from_mask(panel, mask & (panel["split"] == "IS"))
            is_tr = standalone_trades(is_days, cal_ext, di, ohlc_px, bench, "tmp")
            is_m = summarize([t["excess_pct"] for t in is_tr if t["split"] == "IS"])
            if is_m["n"] >= MIN_IS_N and pd.notna(is_m["mean"]) and is_m["mean"] >= 0:
                full_days = fire_days_from_mask(panel, mask)
                full_tr = standalone_trades(full_days, cal_ext, di, ohlc_px, bench, rule_id)
                m = metrics_for(full_tr, whale_full_xs)
                all_results.append(
                    {
                        "rule_id": rule_id,
                        "label": label,
                        "family": "sequence",
                        "thr": np.nan,
                        "seq": seq_col,
                        "is_n": is_m["n"],
                        "is_mean": is_m["mean"],
                        "fires_full": len(full_days),
                        **m,
                        "trades": full_tr,
                    }
                )

        log(f"sweep candidates passing IS gate n={len(all_results)}")

        # CSV (no trades column)
        csv_rows = []
        for r in all_results:
            csv_rows.append({k: v for k, v in r.items() if k != "trades"})
        sweep_df = pd.DataFrame(csv_rows)
        if not sweep_df.empty and {"full_mean", "is_mean"}.issubset(sweep_df.columns):
            sweep_df = sweep_df.sort_values(["full_mean", "is_mean"], ascending=False)
        sweep_df.to_csv(OUT / f"{SID}_multiseat_score_sweep.csv", index=False)

        best = pick_best(all_results)
        if best is None:
            best = (
                max(all_results, key=lambda r: r.get("full_mean", -999))
                if all_results
                else None
            )

        if best is not None:
            frozen = {
                "sid": SID,
                "rule_id": best["rule_id"],
                "label": best["label"],
                "family": best["family"],
                "thr": best["thr"] if pd.notna(best.get("thr")) else None,
                "seq": best["seq"],
                "seats": seats,
                "core": core,
                "extra": extra,
                "score_tiers": {"net>0": 1, "≥0.5億": 2, "≥1億": 3},
                "protocol": {
                    "window": f"{D0}..{d1}",
                    "oos_cut": OOS,
                    "hold": HOLD,
                    "cost": COST,
                    "beta": BETA,
                    "enter": "D+1 open",
                    "overlap": "non-overlap",
                },
                "is_n": best["is_n"],
                "is_mean": best["is_mean"],
                "full_n": best.get("full_n"),
                "full_mean": best.get("full_mean"),
                "full_sum": best.get("full_sum"),
                "oos_n": best.get("OOS_n"),
                "oos_mean": best.get("OOS_mean"),
                "generated": datetime.now().isoformat(timespec="seconds"),
            }
            (OUT / f"{SID}_multiseat_frozen.json").write_text(
                json.dumps(frozen, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            pd.DataFrame(best["trades"]).to_csv(
                OUT / f"{SID}_multiseat_best_trades.csv", index=False
            )
    else:
        # Extended-window probe: write compact bench JSON (do not overwrite frozen).
        probe = {
            "sid": SID,
            "mode": "bench_only",
            "window": f"{D0}..{d1}",
            "oos_cut": OOS,
            "cal_days": len(cal),
            "branch_active_days": branch_active,
            "thr90": thr90,
            "fires_8840": sorted(fire_8840),
            "whale": {k: whale_m.get(k) for k in whale_m},
            "8840": {k: m_8840.get(k) for k in m_8840},
            "generated": datetime.now().isoformat(timespec="seconds"),
        }
        tag = f"{D0.replace('-', '')}_{d1.replace('-', '')}"
        (OUT / f"{SID}_extended_bench_{tag}.json").write_text(
            json.dumps(probe, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    whale_fm = mget(whale_m, "full", "mean")
    whale_om = mget(whale_m, "OOS", "mean")
    b_fm = best.get("full_mean") if best else np.nan
    b_om = best.get("OOS_mean") if best else np.nan
    g_fm = mget(m_8840, "full", "mean")
    g_om = mget(m_8840, "OOS", "mean")

    v_whale = compare_verdict(b_fm, whale_fm) if best else "—"
    v_8840 = compare_verdict(b_fm, g_fm) if best else "—"
    v_whale_oos = compare_verdict(b_om, whale_om) if best else "—"
    v_8840_oos = compare_verdict(b_om, g_om) if best else "—"

    seat_desc = ", ".join(f"{t}({name_map.get(t, t)})" for t in seats)
    md_name = (
        f"{SID}_EXTENDED_BENCH_{D0.replace('-', '')}.md"
        if args.bench_only
        else f"{SID}_MULTISEAT_SCORE.md"
    )
    lines = [
        (
            "# 2327 國巨 · Extended-window bench (Whale + 8840)"
            if args.bench_only
            else "# 2327 國巨 · Multi-seat expert branch score sweep"
        ),
        "",
        f"- 生成：{datetime.now():%Y-%m-%dT%H:%M:%S}",
        f"- Runner：`scripts/research/run_2327_multiseat_score_sweep.py`",
        f"- 窗：{D0}..{d1} · OOS≥{OOS} · H={HOLD} · COST={COST} · β={BETA}×IX0001",
        f"- cal_days={len(cal)} · branch_active={branch_active}",
        *(
            [f"- 席位（core + top-{MAX_EXTRA} extra）：{seat_desc}"]
            if not args.bench_only
            else ["- mode：`--bench-only`（略過 multi-seat sweep）"]
        ),
        *(
            []
            if args.bench_only
            else [
                "- 離散分數：net>0→+1 · ≥0.5億→+2 · ≥1億→+3",
                "- 聚合：score sum@D · sum(D+D−1) · count net>0@D（k∈{1,2,3}）",
                "- 序列：best-seat accel · ≥2 seats co-buy D∧D−1",
                "- IS 凍結門檻：n≥8 · IS mean≥0 · 非重疊 D+1 開",
            ]
        ),
        "- Research only · **未採納**",
        "",
        "## 結論（先讀）",
        "",
    ]

    if best:
        lines.extend(
            [
                f"**最佳規則**：`{best['rule_id']}` — {best['label']}",
                f"- IS：n={best['is_n']} · mean={fmt_pct(best['is_mean'])}",
                f"- Full：n={int(best.get('full_n', 0))} · hit={fmt_rate(best.get('full_hit_rate'))} · "
                f"mean={fmt_pct(best.get('full_mean'))} · sum={fmt_pct(best.get('full_sum'))}",
                f"- OOS：n={int(best.get('OOS_n', 0))} · mean={fmt_pct(best.get('OOS_mean'))}",
                "",
                "### vs 對照臂（H=7 · full mean）",
                "",
                f"| 臂 | n | hit | mean% | sum% | 相對最佳 |",
                f"|---|---|---|---|---|---|",
                f"| **Multi-seat best** | {int(best.get('full_n', 0))} | {fmt_rate(best.get('full_hit_rate'))} | "
                f"{fmt_pct(best.get('full_mean'))} | {fmt_pct(best.get('full_sum'))} | — |",
                f"| Whale_T+1 | {int(mget(whale_m,'full','n'))} | {fmt_rate(mget(whale_m,'full','hit_rate'))} | "
                f"{fmt_pct(whale_fm)} | {fmt_pct(mget(whale_m,'full','sum'))} | {v_whale} |",
                f"| 8840 accel≥p90 | {int(mget(m_8840,'full','n'))} | {fmt_rate(mget(m_8840,'full','hit_rate'))} | "
                f"{fmt_pct(g_fm)} | {fmt_pct(mget(m_8840,'full','sum'))} | {v_8840} |",
                "",
                f"**總評（full mean）**：vs Whale **{v_whale}** · vs 8840 frozen **{v_8840}**",
                f"**總評（OOS mean）**：vs Whale **{v_whale_oos}** · vs 8840 **{v_8840_oos}**",
                "",
            ]
        )
        if v_whale == "更好" and v_8840 in ("更好", "差不多"):
            lines.append("Multi-seat score **有機會**作為獨立進場信號，但仍需 OOS 穩定性審核。")
        elif v_whale in ("更差", "差不多") and v_8840 == "更差":
            lines.append(
                "Multi-seat score **未能**打贏 Whale_T+1 與凍結 8840；"
                "分數加總未帶來額外 edge，建議維持單席序列／8840 凍結臂。"
            )
        else:
            lines.append(
                f"Mixed：full mean vs Whale={v_whale}、vs 8840={v_8840}；"
                "分數聚合有成交增量但 mean edge 有限。"
            )
    elif args.bench_only:
        lines.extend(
            [
                f"**8840 vs Whale（擴窗 probe）**：branch_active={branch_active}/{len(cal)}；"
                "若 branch_active 未增加，結論應與基準窗一致。",
                f"- Whale_T+1 full：n={int(mget(whale_m,'full','n'))} · "
                f"hit={fmt_rate(mget(whale_m,'full','hit_rate'))} · mean={fmt_pct(whale_fm)}",
                f"- 8840 accel≥p90 full：n={int(mget(m_8840,'full','n'))} · "
                f"hit={fmt_rate(mget(m_8840,'full','hit_rate'))} · mean={fmt_pct(g_fm)}",
            ]
        )
    else:
        lines.append("**無規則通過 IS 門檻（n≥8 · mean≥0）** — cannot_beat / 樣本不足。")

    lines.extend(
        [
            "",
            "## 對照臂明細",
            "",
            md_table(
                ["arm", "split", "n", "hit", "mean%", "sum%", "ΔvsWhale", "P(Δ≤0)"],
                [
                    [
                        "Whale_T+1", sp,
                        str(int(mget(whale_m, sp, "n"))),
                        fmt_rate(mget(whale_m, sp, "hit_rate")),
                        fmt_pct(mget(whale_m, sp, "mean")),
                        fmt_pct(mget(whale_m, sp, "sum")),
                        "—" if sp == "full" else fmt_pct(mget(whale_m, sp, "delta_mean")),
                        "—" if sp == "full" else fmt_p(mget(whale_m, sp, "p_delta_le0")),
                    ]
                    for sp in ("full", "IS", "OOS")
                ],
            ),
            "",
            md_table(
                ["arm", "split", "n", "hit", "mean%", "sum%", "ΔvsWhale", "P(Δ≤0)"],
                [
                    [
                        f"8840 accel≥p90({thr90/1e8:.2f}億)", sp,
                        str(int(mget(m_8840, sp, "n"))),
                        fmt_rate(mget(m_8840, sp, "hit_rate")),
                        fmt_pct(mget(m_8840, sp, "mean")),
                        fmt_pct(mget(m_8840, sp, "sum")),
                        fmt_pct(mget(m_8840, sp, "delta_mean")),
                        fmt_p(mget(m_8840, sp, "p_delta_le0")),
                    ]
                    for sp in ("full", "IS", "OOS")
                ],
            ),
            "",
        ]
    )

    if not args.bench_only:
        lines.extend(
            [
                "## Sweep Top（IS n≥8 · IS mean≥0 · by full mean）",
                "",
            ]
        )
        sweep_df = (
            pd.DataFrame([{k: v for k, v in r.items() if k != "trades"} for r in all_results])
            .sort_values(["full_mean", "is_mean"], ascending=False)
            if all_results
            else pd.DataFrame()
        )
        top = sweep_df.head(20) if not sweep_df.empty else sweep_df
        if top.empty:
            lines.append("_（無候選）_")
        else:
            rows = []
            for _, r in top.iterrows():
                rows.append(
                    [
                        str(r["rule_id"]),
                        str(r.get("family", "")),
                        str(int(r.get("is_n", 0))),
                        fmt_pct(r.get("is_mean")),
                        str(int(r.get("full_n", 0))),
                        fmt_pct(r.get("full_mean")),
                        fmt_pct(r.get("full_sum")),
                        str(int(r.get("OOS_n", 0))),
                        fmt_pct(r.get("OOS_mean")),
                    ]
                )
            lines.append(
                md_table(
                    [
                        "rule",
                        "family",
                        "IS_n",
                        "IS_mean",
                        "full_n",
                        "full_mean",
                        "full_sum",
                        "OOS_n",
                        "OOS_mean",
                    ],
                    rows,
                )
            )

    lines.extend(
        [
            "",
            "## 方法備註",
            "",
            "- 僅載入 sid=2327 分點 tape（chunked SQL），不含 chip features。",
            f"- Core 來自 `trades/{SID}.json`；extra = 窗內 positive-amt top-{MAX_EXTRA}（非 core）。",
            "- best-seat accel：D 日 score 最高席若 D−1>0 且 D>D−1 則成立。",
            "- co-buy2：≥2 席 D 與 D−1 皆 net>0。",
            f"- 凍結 json：`{SID}_multiseat_frozen.json`（若有通過 IS 門檻之最佳規則）。",
            "- `--d0` / `--oos` / `--bench-only`：擴窗 probe 用；本機 branch floor=2024-07-01。",
            "",
            f"_elapsed {time.time()-t0:.1f}s_",
        ]
    )

    md_path = OUT / md_name
    md_path.write_text("\n".join(lines), encoding="utf-8")
    log(
        f"Wrote {md_path.name} · candidates={len(all_results)} · "
        f"best={best['rule_id'] if best else 'none'}"
    )
    log(f"done in {time.time()-t0:.1f}s")
    return 0


def fmt_p(v: float | None) -> str:
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "—"
    return f"{v:.2f}"


if __name__ == "__main__":
    raise SystemExit(main())
