#!/usr/bin/env python3
"""Item AD (100-item creative-combo plan, wave 6): does the underlying stock's
large-holder ownership concentration LEVEL (not change-rate) predict
dayflip-futures-short (FROZEN_SPEC_V1) gap-fade outcome?

Distinct from two prior tests in this line:
  - item R (asquith_dayflip_crosscheck): institutional securities-lending /
    retail margin-short LEVEL on this same dayflip-short universe -> POSITIVE
    (si_lend_pct tercile gradient, p=0.021/0.005).
  - second_disp_concentration_changerate: large-holder concentration
    CHANGE-RATE on a *different* universe (second-disposition stocks) -> null.

This test: large-holder concentration LEVEL (千張大戶比 = FinMind
TaiwanStockHoldingSharesPer, level="more than 1,000,001", i.e. >1,000 張),
static snapshot as-of signal date, on dayflip-short's OWN 190-trade candidate
universe (reused from item B / item R, NOT re-derived).

Two competing hypotheses (test both, let data decide):
  (a) high concentration = branch buying IS / is connected to a large holder
      = more "real" conviction = gap less likely to fade = WORSE short
  (b) high concentration = thin float = gap mechanically easy to produce and
      reverse = BETTER short candidate (more fragile move)

PIT convention: identical to dashboard-completeness/holder_concentration.md
and item R. TDCC 集保分級表 is published weekly (Fri close ~ Sat), so we use
publish_lag_days=5 (same constant as
scripts/research/run_second_disp_concentration_changerate.py and the
dashboard-completeness study) subtracted from signal_date as the cutoff, and
take the latest disclosed big_pct row at/before that cutoff. No look-ahead.

Trade set: reports/research/dayflip_revenue_momentum_filter/trades_with_revyoy.csv
(190 trades, 38 stocks, signal_date/trade_date/stock/pnl_pct/...) -- same
dataset items B/R/Y all used, not re-derived from events.json.

Read-only DB (this script does not touch DB at all, only FinMind HTTP + CSV).
Does NOT touch config/order.yaml, config/strategy.yaml,
src/order/dayflip_short_*.py, or launchd.

Output: reports/research/dayflip_teq_concentration_level/{trades_with_concentration.csv,summary.json}

Run:
  PYTHONPATH=src .venv/bin/python scripts/research/run_dayflip_concentration_level.py
"""
from __future__ import annotations

import csv
import json
import random
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import mean, median, pstdev

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from finmind_client import fetch_finmind  # noqa: E402

SRC_CSV = ROOT / "reports/research/dayflip_revenue_momentum_filter/trades_with_revyoy.csv"
DEST = ROOT / "reports/research/dayflip_teq_concentration_level"
LEVEL_BIG = "more than 1,000,001"  # strict >1,000 張 (千張大戶), same cutoff as dashboard study
PUBLISH_LAG_DAYS = 5
FETCH_START = date(2024, 6, 1)
FETCH_END = date(2026, 8, 8)
DELAY_SEC = 0.35
N_PERM = 20000
RNG_SEED = 42


def log(msg: str) -> None:
    print(f"[dayflip-concentration-level] {msg}", flush=True)


def load_trades() -> list[dict]:
    with open(SRC_CSV, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def fetch_big_pct_series(stock_id: str) -> list[tuple[str, float]]:
    rows = fetch_finmind("TaiwanStockHoldingSharesPer", stock_id, FETCH_START, FETCH_END, timeout=90)
    out = []
    for r in rows:
        if r.get("HoldingSharesLevel") != LEVEL_BIG:
            continue
        d = str(r.get("date") or "")[:10]
        pct = r.get("percent")
        if d and pct is not None:
            out.append((d, float(pct)))
    out.sort(key=lambda x: x[0])
    return out


def concentration_at(series: list[tuple[str, float]], signal_date: str) -> dict | None:
    sig = datetime.strptime(signal_date, "%Y-%m-%d").date()
    cutoff = sig - timedelta(days=PUBLISH_LAG_DAYS)
    usable = [(d, v) for d, v in series if datetime.strptime(d, "%Y-%m-%d").date() <= cutoff]
    if not usable:
        return None
    latest_date, latest_val = usable[-1]
    return {
        "concentration_asof_date": latest_date,
        "concentration_cutoff": cutoff.isoformat(),
        "big_pct": latest_val,
    }


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
    """Two-sample permutation test on mean(a) - mean(b). Returns (obs_diff, two_sided_p)."""
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
        diff = mean(pa) - mean(pb)
        if abs(diff) >= abs(obs) - 1e-12:
            count += 1
    p = count / n_perm
    return obs, p


def permutation_test_diff_winrate(a: list[float], b: list[float], n_perm: int, seed: int) -> tuple[float, float]:
    """a, b are lists of 0/1 win indicators. Permutation test on winrate(a) - winrate(b)."""
    obs = mean(a) - mean(b)
    pooled = a + b
    na = len(a)
    rng = random.Random(seed + 1)
    count = 0
    idx = list(range(len(pooled)))
    for _ in range(n_perm):
        rng.shuffle(idx)
        pa = [pooled[i] for i in idx[:na]]
        pb = [pooled[i] for i in idx[na:]]
        diff = mean(pa) - mean(pb)
        if abs(diff) >= abs(obs) - 1e-12:
            count += 1
    p = count / n_perm
    return obs, p


def jackknife_by_stock(rows: list[dict]) -> dict:
    """Leave-one-stock-out spearman(big_pct, pnl_pct) stability check."""
    stocks = sorted({r["stock"] for r in rows})
    full_x = [r["big_pct"] for r in rows]
    full_y = [r["pnl_pct"] for r in rows]
    full_rho = spearman(full_x, full_y)
    fold_rhos = []
    for sid in stocks:
        sub = [r for r in rows if r["stock"] != sid]
        if len(sub) < 5:
            continue
        rho = spearman([r["big_pct"] for r in sub], [r["pnl_pct"] for r in sub])
        fold_rhos.append(rho)
    same_sign = sum(1 for r in fold_rhos if (r > 0) == (full_rho > 0)) if fold_rhos else 0
    return {
        "full_spearman": full_rho,
        "n_stocks": len(stocks),
        "n_folds": len(fold_rhos),
        "fold_same_sign_as_full": same_sign,
        "fold_rho_min": min(fold_rhos) if fold_rhos else None,
        "fold_rho_max": max(fold_rhos) if fold_rhos else None,
    }


def main() -> None:
    DEST.mkdir(parents=True, exist_ok=True)
    trades = load_trades()
    log(f"loaded {len(trades)} dayflip-short trades")
    sids = sorted({t["stock"] for t in trades})
    log(f"{len(sids)} unique stock_ids -> fetching FinMind TaiwanStockHoldingSharesPer (level={LEVEL_BIG})")

    series_cache: dict[str, list[tuple[str, float]]] = {}
    fetch_failed: list[str] = []
    for i, sid in enumerate(sids):
        try:
            series_cache[sid] = fetch_big_pct_series(sid)
            log(f"  [{i + 1}/{len(sids)}] {sid}: {len(series_cache[sid])} weekly rows")
        except Exception as exc:  # noqa: BLE001
            log(f"  [{i + 1}/{len(sids)}] {sid}: FETCH FAILED {exc}")
            series_cache[sid] = []
            fetch_failed.append(sid)
        time.sleep(DELAY_SEC)

    matched: list[dict] = []
    skipped_no_history = 0
    for t in trades:
        sid = t["stock"]
        series = series_cache.get(sid) or []
        conc = concentration_at(series, t["signal_date"])
        if conc is None:
            skipped_no_history += 1
            continue
        row = dict(t)
        row["pnl_pct"] = float(t["pnl_pct"])
        row.update(conc)
        matched.append(row)

    log(f"matched {len(matched)}/{len(trades)} trades with PIT-safe concentration level "
        f"(skipped_no_history={skipped_no_history}, fetch_failed={fetch_failed})")

    # Write matched CSV
    out_csv = DEST / "trades_with_concentration.csv"
    fieldnames = list(matched[0].keys()) if matched else []
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in matched:
            w.writerow(r)

    summary: dict = {
        "n_trades_total": len(trades),
        "n_stocks_total": len(sids),
        "n_matched": len(matched),
        "n_skipped_no_history": skipped_no_history,
        "fetch_failed_stocks": fetch_failed,
        "publish_lag_days": PUBLISH_LAG_DAYS,
        "level": LEVEL_BIG,
    }

    if len(matched) < 20:
        summary["verdict"] = "INSUFFICIENT_DATA"
        (DEST / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False))
        log("insufficient matched trades, aborting analysis")
        return

    # Tercile split by big_pct level
    matched_sorted = sorted(matched, key=lambda r: r["big_pct"])
    n = len(matched_sorted)
    t1_end = n // 3
    t2_end = 2 * n // 3
    low = matched_sorted[:t1_end]
    mid = matched_sorted[t1_end:t2_end]
    high = matched_sorted[t2_end:]

    def bucket_stats(bucket: list[dict]) -> dict:
        pnls = [r["pnl_pct"] for r in bucket]
        wins = [1.0 if p > 0 else 0.0 for p in pnls]
        stocks = sorted({r["stock"] for r in bucket})
        return {
            "n": len(bucket),
            "n_stocks": len(stocks),
            "mean_pnl_pct": mean(pnls),
            "median_pnl_pct": median(pnls),
            "std_pnl_pct": pstdev(pnls) if len(pnls) > 1 else 0.0,
            "win_rate": mean(wins),
            "big_pct_range": [bucket[0]["big_pct"], bucket[-1]["big_pct"]],
        }

    low_stats = bucket_stats(low)
    mid_stats = bucket_stats(mid)
    high_stats = bucket_stats(high)

    low_pnls = [r["pnl_pct"] for r in low]
    high_pnls = [r["pnl_pct"] for r in high]
    low_wins = [1.0 if p > 0 else 0.0 for p in low_pnls]
    high_wins = [1.0 if p > 0 else 0.0 for p in high_pnls]

    pnl_obs, pnl_p = permutation_test_diff_means(high_pnls, low_pnls, N_PERM, RNG_SEED)
    win_obs, win_p = permutation_test_diff_means(high_wins, low_wins, N_PERM, RNG_SEED)

    full_rho = spearman([r["big_pct"] for r in matched], [r["pnl_pct"] for r in matched])
    jk = jackknife_by_stock(matched)

    # Outlier-robust recheck: drop single worst-pnl trade
    worst = min(matched, key=lambda r: r["pnl_pct"])
    matched_ex_worst = [r for r in matched if r is not worst]
    ms2 = sorted(matched_ex_worst, key=lambda r: r["big_pct"])
    n2 = len(ms2)
    low2 = ms2[: n2 // 3]
    high2 = ms2[2 * n2 // 3:]
    pnl_obs2, pnl_p2 = permutation_test_diff_means(
        [r["pnl_pct"] for r in high2], [r["pnl_pct"] for r in low2], N_PERM, RNG_SEED
    )
    win_obs2, win_p2 = permutation_test_diff_means(
        [1.0 if r["pnl_pct"] > 0 else 0.0 for r in high2],
        [1.0 if r["pnl_pct"] > 0 else 0.0 for r in low2],
        N_PERM,
        RNG_SEED,
    )

    summary.update({
        "tercile_low": low_stats,
        "tercile_mid": mid_stats,
        "tercile_high": high_stats,
        "high_vs_low_pnl_diff_pct": pnl_obs,
        "high_vs_low_pnl_perm_p": pnl_p,
        "high_vs_low_winrate_diff": win_obs,
        "high_vs_low_winrate_perm_p": win_p,
        "full_sample_spearman_bigpct_pnl": full_rho,
        "jackknife_leave_one_stock_out": jk,
        "outlier_robust_recheck": {
            "dropped_trade_stock": worst["stock"],
            "dropped_trade_pnl_pct": worst["pnl_pct"],
            "high_vs_low_pnl_diff_pct": pnl_obs2,
            "high_vs_low_pnl_perm_p": pnl_p2,
            "high_vs_low_winrate_diff": win_obs2,
            "high_vs_low_winrate_perm_p": win_p2,
        },
    })

    verdict = "NULL"
    if pnl_p < 0.05 or win_p < 0.05:
        verdict = "POSITIVE_CANDIDATE"
    summary["verdict"] = verdict

    (DEST / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False, default=str))
    log(f"wrote {out_csv}")
    log(f"wrote {DEST / 'summary.json'}")
    log(f"verdict={verdict} pnl_p={pnl_p:.4f} win_p={win_p:.4f} spearman={full_rho:.3f}")


if __name__ == "__main__":
    main()
