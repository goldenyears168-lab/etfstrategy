#!/usr/bin/env python3
"""Gap #5 (completeness-critic follow-up): does day-trading turnover ratio
(stock_daytrade_daily.daytrade_ratio_pct) at dayflip-futures-short signal
time predict trade outcome?

dayflip-futures-short (FROZEN_SPEC_V1) shorts individual-stock futures on a
T+1 gap-up after big broker-branch buying on T0. High day-trading ratio at
T0 plausibly proxies short-term speculative crowding: either (a) heavy
day-trading = hot-money churn that fades cleanly next day (BETTER dayflip-
short candidate), or (b) heavy day-trading = crowd already fighting for the
squeeze, adds noise/whipsaw risk that fights the fade (WORSE candidate).

Trade set: reused directly from item B's already-reconstructed 190-trade
dataset (reports/research/dayflip_revenue_momentum_filter/trades_with_revyoy.csv).

PIT note: stock_daytrade_daily.trade_date = T (the day the ratio describes),
TWSE 當日沖銷 stats are published same-day after close, same PIT logic as
item R's short-interest check -- use ratio AS OF signal_date (T0) directly,
asof-backward with a short lookback tolerance.

Read-only DB. Does NOT touch config/order.yaml, config/strategy.yaml,
src/order/dayflip_short_*.py, launchd, or any tx_channel/tmf files.

Output: reports/research/dayflip_daytrade_ratio_filter/{trades_with_dtr.csv,summary.json}
"""
from __future__ import annotations

import csv
import json
import random
import sqlite3
import sys
from pathlib import Path
from statistics import mean, median, pstdev

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
import stock_db  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
SRC_CSV = ROOT / "reports/research/dayflip_revenue_momentum_filter/trades_with_revyoy.csv"
DEST = ROOT / "reports/research/dayflip_daytrade_ratio_filter"
DEST.mkdir(parents=True, exist_ok=True)

N_PERM = 20000
RNG_SEED = 42


def log(m: str) -> None:
    print(f"[dayflip-daytrade-ratio] {m}", flush=True)


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


def jackknife_by_stock(usable: list[dict], metric: str) -> dict:
    """Leave-one-stock-out: recompute high-minus-low tercile perm-test diff each time a
    stock is dropped, report range/stability of observed diff."""
    stocks = sorted({r["stock"] for r in usable})
    if len(stocks) < 4:
        return {"note": "too few stocks for jackknife"}
    diffs = []
    for held_out in stocks:
        sub = [r for r in usable if r["stock"] != held_out]
        n = len(sub)
        if n < 12:
            continue
        vals = sorted(r[metric] for r in sub)
        q1, q2 = vals[n // 3], vals[2 * n // 3]
        low = [r["pnl_pct"] for r in sub if r[metric] <= q1]
        high = [r["pnl_pct"] for r in sub if r[metric] > q2]
        if len(low) >= 3 and len(high) >= 3:
            diffs.append(mean(high) - mean(low))
    if not diffs:
        return {"note": "no valid jackknife folds"}
    return dict(n_folds=len(diffs), mean_diff=round(mean(diffs), 4),
                min_diff=round(min(diffs), 4), max_diff=round(max(diffs), 4),
                std_diff=round(pstdev(diffs), 4) if len(diffs) > 1 else None,
                sign_stable=all(d > 0 for d in diffs) or all(d < 0 for d in diffs))


def load_trades() -> list[dict]:
    rows = list(csv.DictReader(open(SRC_CSV)))
    out = []
    for r in rows:
        out.append(dict(
            signal_date=r["signal_date"], trade_date=r["trade_date"], stock=r["stock"],
            pnl_pct=float(r["pnl_pct"]), how=r["how"], fgap=float(r["fgap"]), n_seats=int(r["n_seats"]),
        ))
    return out


def load_daytrade_ratio(stock_ids: set[str]) -> dict[str, list[tuple[str, float]]]:
    con = sqlite3.connect(f"file:{stock_db.DEFAULT_DB_PATH}?mode=ro", uri=True)
    ids = tuple(sorted(stock_ids))
    ph = ",".join("?" * len(ids))
    dtr: dict[str, list[tuple[str, float]]] = {sid: [] for sid in ids}
    for sid, d, r in con.execute(
        f"""SELECT stock_id, trade_date, daytrade_ratio_pct FROM stock_daytrade_daily
            WHERE stock_id IN ({ph}) AND daytrade_ratio_pct IS NOT NULL
            ORDER BY trade_date""",
        ids,
    ):
        dtr[str(sid)].append((str(d), float(r)))
    con.close()
    return dtr


def asof_backward(series: list[tuple[str, float]], asof: str, max_lookback_days: int = 10) -> tuple[str, float] | None:
    best = None
    for d, v in series:
        if d > asof:
            break
        best = (d, v)
    if best is None:
        return None
    from datetime import date as _date
    dd = _date.fromisoformat(best[0])
    ad = _date.fromisoformat(asof)
    if (ad - dd).days > max_lookback_days:
        return None
    return best


def trail_avg_20d(series: list[tuple[str, float]], asof: str) -> tuple[float | None, int]:
    """20-trading-day trailing average of ratio as of (<=) asof. Returns (avg, n_points_used)."""
    vals = [v for d, v in series if d <= asof]
    if not vals:
        return None, 0
    window = vals[-20:]
    return mean(window), len(window)


def main() -> None:
    trades = load_trades()
    log(f"loaded {len(trades)} trades from item-B reconstruction ({SRC_CSV.name})")
    stocks = {t["stock"] for t in trades}

    dtr = load_daytrade_ratio(stocks)
    have_any = {s for s in stocks if dtr.get(s)}
    log(f"stock_daytrade_daily.daytrade_ratio_pct coverage: {len(have_any)}/{len(stocks)} of trade-set stocks have ANY non-null row")
    all_dates_seen = sorted({d for series in dtr.values() for d, _ in series})
    if all_dates_seen:
        log(f"ratio_pct global date range across trade-set stocks: {all_dates_seen[0]} .. {all_dates_seen[-1]} ({len(all_dates_seen)} distinct dates)")
    else:
        log("ratio_pct: ZERO rows found for any trade-set stock")

    rows = []
    for t in trades:
        sid, asof = t["stock"], t["signal_date"]
        m = asof_backward(dtr.get(sid, []), asof, max_lookback_days=10)
        dtr_t0 = m[1] if m else None
        dtr_t0_date = m[0] if m else None
        avg20, n20 = trail_avg_20d(dtr.get(sid, []), asof)
        rows.append(dict(
            **t,
            dtr_t0=dtr_t0, dtr_t0_date=dtr_t0_date,
            dtr_avg20=avg20 if n20 >= 15 else None,  # require >=15/20 pts to trust the trailing avg
            dtr_avg20_npts=n20,
            hit=1 if t["how"] == "觸價回補" else 0,
        ))

    fieldnames = list(rows[0].keys())
    with (DEST / "trades_with_dtr.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)

    summary: dict = {"n_total_trades": len(rows),
                      "stocks_with_any_dtr_data": sorted(have_any),
                      "dtr_global_date_range": [all_dates_seen[0], all_dates_seen[-1]] if all_dates_seen else None,
                      "dtr_n_distinct_dates": len(all_dates_seen)}

    def analyze(metric: str, label: str) -> dict:
        usable = [r for r in rows if r[metric] is not None]
        n = len(usable)
        res: dict = {"metric": metric, "label": label, "n_usable": n,
                     "n_unique_stocks": len({r["stock"] for r in usable})}
        if n < 12:
            res["note"] = f"too few trades with usable metric for tercile split (n={n} < 12)"
            res["usable_trades"] = [dict(signal_date=r["signal_date"], stock=r["stock"], pnl_pct=r["pnl_pct"],
                                          value=r[metric]) for r in usable]
            return res
        vals = sorted(r[metric] for r in usable)
        q1, q2 = vals[n // 3], vals[2 * n // 3]
        low = [r for r in usable if r[metric] <= q1]
        mid = [r for r in usable if q1 < r[metric] <= q2]
        high = [r for r in usable if r[metric] > q2]

        def bucket_stats(b: list[dict]) -> dict:
            pnl = [r["pnl_pct"] for r in b]
            return dict(n=len(b), n_unique_stocks=len({r["stock"] for r in b}),
                        mean_pnl_pct=round(mean(pnl), 4) if pnl else None,
                        median_pnl_pct=round(median(pnl), 4) if pnl else None,
                        std_pnl_pct=round(pstdev(pnl), 4) if len(pnl) > 1 else None,
                        hit_rate_pct=round(100 * mean(r["hit"] for r in b), 2) if b else None,
                        metric_range=[round(min(r[metric] for r in b), 4), round(max(r[metric] for r in b), 4)])

        res["tercile_low"] = bucket_stats(low)
        res["tercile_mid"] = bucket_stats(mid)
        res["tercile_high"] = bucket_stats(high)

        if len(high) >= 3 and len(low) >= 3:
            hp = [r["pnl_pct"] for r in high]
            lp = [r["pnl_pct"] for r in low]
            obs, p = permutation_test_diff_means(hp, lp, N_PERM, RNG_SEED)
            res["perm_test_high_minus_low_pnl"] = dict(
                obs_diff_pct=round(obs, 4), n_perm=N_PERM, two_sided_p=round(p, 4))
            hh = [r["hit"] for r in high]
            hl = [r["hit"] for r in low]
            obs_h, p_h = permutation_test_diff_means([float(x) for x in hh], [float(x) for x in hl], N_PERM, RNG_SEED + 1)
            res["perm_test_high_minus_low_hitrate"] = dict(
                obs_diff=round(obs_h, 4), n_perm=N_PERM, two_sided_p=round(p_h, 4))

        res["spearman_trade_level"] = round(spearman([r[metric] for r in usable], [r["pnl_pct"] for r in usable]), 4)
        by_stock: dict[str, list[dict]] = {}
        for r in usable:
            by_stock.setdefault(r["stock"], []).append(r)
        sx, sy = [], []
        for sid, rs in by_stock.items():
            sx.append(mean(r[metric] for r in rs))
            sy.append(mean(r["pnl_pct"] for r in rs))
        res["spearman_stock_level"] = round(spearman(sx, sy), 4) if len(sx) >= 3 else None
        res["n_stocks_for_stock_level"] = len(sx)

        res["jackknife_leave_one_stock_out"] = jackknife_by_stock(usable, metric)

        # confound check vs fgap (own item) and si_lend_pct (item R) if we can load item R's csv
        res["spearman_vs_fgap"] = round(spearman([r[metric] for r in usable], [r["fgap"] for r in usable]), 4)
        return res

    summary["dtr_t0"] = analyze("dtr_t0", "day-trading ratio (%) as-of signal_date T0")
    summary["dtr_avg20"] = analyze("dtr_avg20", "20-trading-day trailing avg day-trading ratio (%) as-of T0")

    # confound check against item R's si_lend_pct where both exist
    r_csv = ROOT / "reports/research/asquith_dayflip_crosscheck/trades_with_si.csv"
    if r_csv.exists():
        r_rows = {(r["signal_date"], r["stock"]): r for r in csv.DictReader(open(r_csv))}
        joined = []
        for row in rows:
            key = (row["signal_date"], row["stock"])
            rr = r_rows.get(key)
            if rr and rr.get("si_lend_pct") not in (None, "") and row["dtr_t0"] is not None:
                joined.append((row["dtr_t0"], float(rr["si_lend_pct"])))
        summary["confound_check_vs_item_R_si_lend_pct"] = dict(
            n_joined=len(joined),
            spearman=round(spearman([a for a, b in joined], [b for a, b in joined]), 4) if len(joined) >= 3 else None,
        )
    else:
        summary["confound_check_vs_item_R_si_lend_pct"] = {"note": "item R csv not found"}

    (DEST / "summary.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False))
    log("wrote trades_with_dtr.csv and summary.json")
    for k in ("dtr_t0", "dtr_avg20"):
        s = summary[k]
        log(f"{k}: n_usable={s.get('n_usable')} stocks={s.get('n_unique_stocks')} note={s.get('note')}")
        if "tercile_low" in s:
            log(f"  low={s['tercile_low']} high={s['tercile_high']}")
            if "perm_test_high_minus_low_pnl" in s:
                log(f"  perm(pnl)={s['perm_test_high_minus_low_pnl']} perm(hit)={s['perm_test_high_minus_low_hitrate']}")
            log(f"  spearman trade={s['spearman_trade_level']} stock={s['spearman_stock_level']} (n_stocks={s['n_stocks_for_stock_level']})")
            log(f"  jackknife={s['jackknife_leave_one_stock_out']}")
    log(f"confound vs item-R si_lend_pct: {summary['confound_check_vs_item_R_si_lend_pct']}")


if __name__ == "__main__":
    main()
