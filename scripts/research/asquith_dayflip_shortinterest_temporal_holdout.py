#!/usr/bin/env python3
"""Gap #1 Part 2 (follow-up to item R, 100-item creative-combo plan).

Item R (institutional securities-lending level predicts dayflip-futures-short
trade quality) has no out-of-time holdout: all robustness checks
(leave-one-out by stock, outlier removal, ETF-membership confound check) were
run within the same single pooled 2025-08~2026-07 window
(reports/research/asquith_dayflip_crosscheck/trades_with_si.csv, 190 trades,
139 with usable si_lend_pct).

This script splits those 139 trades into an EARLY half and a LATE half by
signal_date (median split, not calendar-midpoint split, so both halves have
similar n), then re-runs item R's exact tercile + two-sided permutation-test
methodology (scripts/research/asquith_dayflip_shortinterest_crosscheck.py,
same permutation_test_diff_means/spearman code, copied verbatim) on si_lend_pct
independently within each half.

Note: item P's 2022-2024 backward-holdout cache
(reports/research/branch_backward_holdout_reverify/) is NOT si_lend_pct
joinable -- it covers a different signal entirely (whale branch 9227/9661
5d-net-ratio echo trades), not dayflip-futures-short trades. Confirmed by
inspection; not used here.

Read-only. Does not touch config/order.yaml, config/strategy.yaml,
src/order/, or the branch/channel-lab paths reserved for other sessions.

Output: reports/research/asquith_dayflip_si_temporal_holdout/{early_trades.csv,late_trades.csv,summary.json}
"""
from __future__ import annotations

import csv
import json
import random
import sys
from pathlib import Path
from statistics import mean, median, pstdev

ROOT = Path(__file__).resolve().parents[2]
SRC_CSV = ROOT / "reports/research/asquith_dayflip_crosscheck/trades_with_si.csv"
DEST = ROOT / "reports/research/asquith_dayflip_si_temporal_holdout"
DEST.mkdir(parents=True, exist_ok=True)

N_PERM = 20000
RNG_SEED = 42
METRIC = "si_lend_pct"


def log(m: str) -> None:
    print(f"[dayflip-si-temporal-holdout] {m}", flush=True)


def spearman(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    if n < 3:
        return float("nan")

    def rank(vals: list[float]) -> list[float]:
        order = sorted(range(len(vals)), key=lambda i: vals[i])
        ranks = [0.0] * len(vals)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and vals[order[j + 1]] == vals[order[i]]:
                j += 1
            avg_rank = (i + j) / 2.0 + 1
            for k in range(i, j + 1):
                ranks[order[k]] = avg_rank
            i = j + 1
        return ranks

    rx, ry = rank(xs), rank(ys)
    mx, my = mean(rx), mean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    denx = sum((a - mx) ** 2 for a in rx) ** 0.5
    deny = sum((b - my) ** 2 for b in ry) ** 0.5
    if denx == 0 or deny == 0:
        return float("nan")
    return num / (denx * deny)


def permutation_test_diff_means(a: list[float], b: list[float], n_perm: int, seed: int) -> tuple[float, float]:
    obs = mean(a) - mean(b)
    pooled = a + b
    na = len(a)
    rng = random.Random(seed)
    count = 0
    idx = list(range(len(pooled)))
    for _ in range(n_perm):
        rng.shuffle(idx)
        pa = [pooled[i] for i in idx[:na]]
        pb = [pooled[i] for i in idx[na:]]
        d = mean(pa) - mean(pb)
        if abs(d) >= abs(obs):
            count += 1
    p = (count + 1) / (n_perm + 1)
    return obs, p


def load_usable_rows() -> list[dict]:
    rows = list(csv.DictReader(open(SRC_CSV, encoding="utf-8")))
    out = []
    for r in rows:
        if r.get(METRIC) in ("", None):
            continue
        out.append(dict(
            signal_date=r["signal_date"], stock=r["stock"],
            pnl_pct=float(r["pnl_pct"]), hit=1 if r["how"] == "觸價回補" else 0,
            si_lend_pct=float(r[METRIC]),
        ))
    return out


def analyze(rows: list[dict], label: str) -> dict:
    n = len(rows)
    res: dict = {"label": label, "n_usable": n, "n_unique_stocks": len({r["stock"] for r in rows})}
    if n < 12:
        res["note"] = "too few trades for tercile split (matches original script's threshold)"
        return res
    vals = sorted(r[METRIC] for r in rows)
    q1, q2 = vals[n // 3], vals[2 * n // 3]
    low = [r for r in rows if r[METRIC] <= q1]
    mid = [r for r in rows if q1 < r[METRIC] <= q2]
    high = [r for r in rows if r[METRIC] > q2]

    def bucket_stats(b: list[dict]) -> dict:
        pnl = [r["pnl_pct"] for r in b]
        return dict(n=len(b), n_unique_stocks=len({r["stock"] for r in b}),
                    mean_pnl_pct=round(mean(pnl), 4) if pnl else None,
                    median_pnl_pct=round(median(pnl), 4) if pnl else None,
                    std_pnl_pct=round(pstdev(pnl), 4) if len(pnl) > 1 else None,
                    hit_rate_pct=round(100 * mean(r["hit"] for r in b), 2) if b else None,
                    metric_range=[round(min(r[METRIC] for r in b), 6), round(max(r[METRIC] for r in b), 6)])

    res["tercile_low"] = bucket_stats(low)
    res["tercile_mid"] = bucket_stats(mid)
    res["tercile_high"] = bucket_stats(high)

    if len(high) >= 3 and len(low) >= 3:
        hp = [r["pnl_pct"] for r in high]
        lp = [r["pnl_pct"] for r in low]
        obs, p = permutation_test_diff_means(hp, lp, N_PERM, RNG_SEED)
        res["perm_test_high_minus_low_pnl"] = dict(obs_diff_pct=round(obs, 4), n_perm=N_PERM, two_sided_p=round(p, 4))
        hh = [float(r["hit"]) for r in high]
        hl = [float(r["hit"]) for r in low]
        obs_h, p_h = permutation_test_diff_means(hh, hl, N_PERM, RNG_SEED + 1)
        res["perm_test_high_minus_low_hitrate"] = dict(obs_diff=round(obs_h, 4), n_perm=N_PERM, two_sided_p=round(p_h, 4))

    res["spearman_trade_level"] = round(spearman([r[METRIC] for r in rows], [r["pnl_pct"] for r in rows]), 4)
    by_stock: dict[str, list[dict]] = {}
    for r in rows:
        by_stock.setdefault(r["stock"], []).append(r)
    sx, sy = [], []
    for sid, rs in by_stock.items():
        sx.append(mean(r[METRIC] for r in rs))
        sy.append(mean(r["pnl_pct"] for r in rs))
    res["spearman_stock_level"] = round(spearman(sx, sy), 4) if len(sx) >= 3 else None
    res["n_stocks_for_stock_level"] = len(sx)
    return res


def main() -> None:
    usable = load_usable_rows()
    usable.sort(key=lambda r: r["signal_date"])
    n = len(usable)
    log(f"loaded {n} usable ({METRIC} non-null) trades out of 190 total in {SRC_CSV.name}")

    mid = n // 2
    early = usable[:mid]
    late = usable[mid:]
    log(f"median-split by signal_date: early n={len(early)} [{early[0]['signal_date']}..{early[-1]['signal_date']}], "
        f"late n={len(late)} [{late[0]['signal_date']}..{late[-1]['signal_date']}]")

    for name, rows in (("early_trades", early), ("late_trades", late)):
        with (DEST / f"{name}.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)

    pooled_res = analyze(usable, "pooled (original item R window, for reference)")
    early_res = analyze(early, "early half")
    late_res = analyze(late, "late half")

    summary = dict(
        metric=METRIC,
        n_total_usable=n,
        split_date_boundary=dict(early_end=early[-1]["signal_date"], late_start=late[0]["signal_date"]),
        pooled=pooled_res,
        early_half=early_res,
        late_half=late_res,
    )

    def holds(res: dict) -> dict:
        pt = res.get("perm_test_high_minus_low_pnl")
        if not pt:
            return dict(evaluable=False)
        low_mean = res["tercile_low"]["mean_pnl_pct"]
        high_mean = res["tercile_high"]["mean_pnl_pct"]
        direction_low_gt_high = low_mean > high_mean
        p_sig = pt["two_sided_p"] < 0.05
        return dict(evaluable=True, low_tercile_mean_pnl=low_mean, high_tercile_mean_pnl=high_mean,
                     direction_matches_original_low_gt_high=direction_low_gt_high,
                     perm_p_pnl=pt["two_sided_p"], perm_p_under_0_05=p_sig,
                     both_direction_and_significance=direction_low_gt_high and p_sig)

    summary["verdict"] = dict(early=holds(early_res), late=holds(late_res))
    (DEST / "summary.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False))
    log("wrote early_trades.csv, late_trades.csv, summary.json")
    log(f"EARLY verdict: {summary['verdict']['early']}")
    log(f"LATE  verdict: {summary['verdict']['late']}")


if __name__ == "__main__":
    main()
