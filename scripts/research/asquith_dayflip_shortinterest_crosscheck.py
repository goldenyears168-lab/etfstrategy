#!/usr/bin/env python3
"""Item R (100-item creative-combo plan, wave 3): does the underlying stock's
existing short-interest / margin-short-balance level (Asquith panel data
source: stock_margin_daily = 融券 retail margin-short, stock_lending_daily =
借券 institutional securities lending) at dayflip-futures-short signal time
predict trade outcome?

dayflip-futures-short (FROZEN_SPEC_V1) shorts individual-stock futures on a
T+1 gap-up after big broker-branch buying on T0 -- it uses ZERO short-interest
data today. This checks two competing hypotheses without assuming a direction:
  (a) high existing short interest = crowded short = squeeze risk fights the
      fade = WORSE dayflip-short candidate
  (b) high existing short interest = market already skeptical = confirms the
      gap-up is unsustainable = BETTER dayflip-short candidate

Trade set: reused directly from item B's already-reconstructed 190-trade
dataset (reports/research/dayflip_revenue_momentum_filter/trades_with_revyoy.csv),
which was built from events.json + FROZEN_SPEC_V1 seat filters and cross-checked
against the frozen spec's reported IS trade count. Re-deriving it here would be
pure duplication.

PIT note (checked against build_panel.py / asquith line): stock_margin_daily
and stock_lending_daily store trade_date = T (the day the balance describes).
TWSE officially discloses 融資融券餘額 and 借券餘額 same-day after market close
(~15:00), i.e. strictly BEFORE the T0 ~21:00 broker-branch data that
FROZEN_SPEC_V1 itself already relies on for the T0->T+1 signal. Our own
ingest's `synced_at` column lags one calendar day (batch-pulled the next
morning) -- that is an artifact of OUR pull schedule, not of TWSE's publish
time, so it is NOT used as the PIT cutoff. We use short-interest AS OF
signal_date (T0) directly. This is at least as conservative as, and time-
consistent with, the branch-data PIT already baked into the frozen spec.

Read-only DB. Does NOT touch config/order.yaml, config/strategy.yaml,
src/order/dayflip_short_*.py, or launchd.

Output: reports/research/asquith_dayflip_crosscheck/{trades_with_si.csv,summary.json}
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
DEST = ROOT / "reports/research/asquith_dayflip_crosscheck"
DEST.mkdir(parents=True, exist_ok=True)

N_PERM = 20000
RNG_SEED = 42


def log(m: str) -> None:
    print(f"[asquith-dayflip-si] {m}", flush=True)


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
        d = mean(pa) - mean(pb)
        if abs(d) >= abs(obs):
            count += 1
    p = (count + 1) / (n_perm + 1)
    return obs, p


def load_trades() -> list[dict]:
    rows = list(csv.DictReader(open(SRC_CSV)))
    out = []
    for r in rows:
        out.append(dict(
            signal_date=r["signal_date"], trade_date=r["trade_date"], stock=r["stock"],
            pnl_pct=float(r["pnl_pct"]), how=r["how"], fgap=float(r["fgap"]), n_seats=int(r["n_seats"]),
        ))
    return out


def load_short_interest(stock_ids: set[str]) -> tuple[dict, dict, dict]:
    """Returns (margin[sid] -> sorted list[(date,short_balance)],
                lending[sid] -> sorted list[(date,lending_balance)],
                shares[sid] -> sorted list[(date,shares_issued)])."""
    con = sqlite3.connect(f"file:{stock_db.DEFAULT_DB_PATH}?mode=ro", uri=True)
    ids = tuple(sorted(stock_ids))
    ph = ",".join("?" * len(ids))
    margin: dict[str, list[tuple[str, float]]] = {sid: [] for sid in ids}
    for sid, d, sb in con.execute(
        f"SELECT stock_id, trade_date, short_balance FROM stock_margin_daily WHERE stock_id IN ({ph}) ORDER BY trade_date",
        ids,
    ):
        if sb is not None:
            margin[str(sid)].append((str(d), float(sb)))
    lending: dict[str, list[tuple[str, float]]] = {sid: [] for sid in ids}
    for sid, d, lb in con.execute(
        f"SELECT stock_id, trade_date, lending_balance FROM stock_lending_daily WHERE stock_id IN ({ph}) ORDER BY trade_date",
        ids,
    ):
        if lb is not None:
            lending[str(sid)].append((str(d), float(lb)))
    shares: dict[str, list[tuple[str, float]]] = {sid: [] for sid in ids}
    for sid, d, si in con.execute(
        f"SELECT stock_id, trade_date, shares_issued FROM stock_shareholding_daily WHERE stock_id IN ({ph}) ORDER BY trade_date",
        ids,
    ):
        if si is not None and si > 0:
            shares[str(sid)].append((str(d), float(si)))
    vol: dict[str, list[tuple[str, float]]] = {sid: [] for sid in ids}
    for sid, d, v in con.execute(
        f"SELECT stock_id, trade_date, volume FROM stock_daily_bars WHERE stock_id IN ({ph}) AND source='finmind' ORDER BY trade_date",
        ids,
    ):
        if v is not None and v > 0:
            vol[str(sid)].append((str(d), float(v)))
    con.close()
    return margin, lending, shares, vol  # type: ignore[return-value]


def asof_backward(series: list[tuple[str, float]], asof: str, max_lookback_days: int = 10) -> tuple[str, float] | None:
    """Most recent (date,value) with date <= asof. series must be sorted ascending by date."""
    best = None
    for d, v in series:
        if d > asof:
            break
        best = (d, v)
    if best is None:
        return None
    # sanity: don't allow arbitrarily stale data (calendar-day proxy for trading-day gap)
    from datetime import date as _date
    dd = _date.fromisoformat(best[0])
    ad = _date.fromisoformat(asof)
    if (ad - dd).days > max_lookback_days:
        return None
    return best


def avg_vol_20d(series: list[tuple[str, float]], asof: str) -> float | None:
    vals = [v for d, v in series if d <= asof]
    if len(vals) < 20:
        return None
    return mean(vals[-20:])


def main() -> None:
    trades = load_trades()
    log(f"loaded {len(trades)} trades from item-B reconstruction ({SRC_CSV.name})")
    stocks = {t["stock"] for t in trades}
    margin, lending, shares, vol = load_short_interest(stocks)

    have_margin = {s for s in stocks if margin.get(s)}
    have_lending = {s for s in stocks if lending.get(s)}
    log(f"stocks with any margin data: {len(have_margin)}/{len(stocks)}; lending: {len(have_lending)}/{len(stocks)}")

    rows = []
    for t in trades:
        sid, asof = t["stock"], t["signal_date"]
        m = asof_backward(margin.get(sid, []), asof)
        l = asof_backward(lending.get(sid, []), asof)
        sh = asof_backward(shares.get(sid, []), asof)
        v20 = avg_vol_20d(vol.get(sid, []), asof)
        si_margin_th = m[1] if m else None  # thousand shares
        si_lend_th = l[1] if l else None
        si_total_th = None
        if si_margin_th is not None or si_lend_th is not None:
            si_total_th = (si_margin_th or 0.0) + (si_lend_th or 0.0)
        shares_out = sh[1] if sh else None
        si_margin_pct = (si_margin_th * 1000 / shares_out) if (si_margin_th is not None and shares_out) else None
        si_lend_pct = (si_lend_th * 1000 / shares_out) if (si_lend_th is not None and shares_out) else None
        si_total_pct = (si_total_th * 1000 / shares_out) if (si_total_th is not None and shares_out) else None
        dtc_margin = (si_margin_th * 1000 / v20) if (si_margin_th is not None and v20) else None
        dtc_total = (si_total_th * 1000 / v20) if (si_total_th is not None and v20) else None
        rows.append(dict(
            **t,
            si_margin_th=si_margin_th, si_lend_th=si_lend_th, si_total_th=si_total_th,
            si_margin_date=m[0] if m else None, si_lend_date=l[0] if l else None,
            shares_out=shares_out, si_margin_pct=si_margin_pct, si_lend_pct=si_lend_pct, si_total_pct=si_total_pct,
            avg_vol20=v20, days_to_cover_margin=dtc_margin, days_to_cover_total=dtc_total,
            hit=1 if t["how"] == "觸價回補" else 0,
        ))

    fieldnames = list(rows[0].keys())
    with (DEST / "trades_with_si.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)

    summary: dict = {"n_total_trades": len(rows)}

    def analyze(metric: str, label: str) -> dict:
        usable = [r for r in rows if r[metric] is not None]
        n = len(usable)
        res: dict = {"metric": metric, "label": label, "n_usable": n,
                     "n_unique_stocks": len({r["stock"] for r in usable})}
        if n < 12:
            res["note"] = "too few trades with usable metric for bucket split"
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
            # hit-rate permutation (binary outcome)
            hh = [r["hit"] for r in high]
            hl = [r["hit"] for r in low]
            obs_h, p_h = permutation_test_diff_means([float(x) for x in hh], [float(x) for x in hl], N_PERM, RNG_SEED + 1)
            res["perm_test_high_minus_low_hitrate"] = dict(
                obs_diff=round(obs_h, 4), n_perm=N_PERM, two_sided_p=round(p_h, 4))

        # trade-level and stock-level spearman vs pnl
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
        return res

    summary["si_total_pct"] = analyze("si_total_pct", "combined short interest (margin+lending) / shares outstanding")
    summary["si_margin_pct"] = analyze("si_margin_pct", "retail margin-short (融券) / shares outstanding")
    summary["si_lend_pct"] = analyze("si_lend_pct", "institutional securities lending (借券) / shares outstanding")
    summary["days_to_cover_total"] = analyze("days_to_cover_total", "combined short balance / 20d avg volume")
    summary["days_to_cover_margin"] = analyze("days_to_cover_margin", "retail margin-short balance / 20d avg volume")

    (DEST / "summary.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False))
    log("wrote trades_with_si.csv and summary.json")
    for k in ("si_total_pct", "si_margin_pct", "si_lend_pct", "days_to_cover_total", "days_to_cover_margin"):
        s = summary[k]
        log(f"{k}: n_usable={s.get('n_usable')} stocks={s.get('n_unique_stocks')}")
        if "tercile_low" in s:
            log(f"  low={s['tercile_low']} high={s['tercile_high']}")
            if "perm_test_high_minus_low_pnl" in s:
                log(f"  perm(pnl)={s['perm_test_high_minus_low_pnl']} perm(hit)={s['perm_test_high_minus_low_hitrate']}")
            log(f"  spearman trade={s['spearman_trade_level']} stock={s['spearman_stock_level']} (n_stocks={s['n_stocks_for_stock_level']})")


if __name__ == "__main__":
    main()
