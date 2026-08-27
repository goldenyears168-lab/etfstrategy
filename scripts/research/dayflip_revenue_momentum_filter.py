#!/usr/bin/env python3
"""Item B (creative-combo plan): does filtering dayflip-futures-short candidates
by underlying-stock revenue momentum (rev_yoy_3m) improve expected value?

Read-only research script. Does NOT touch config/order.yaml, config/strategy.yaml,
src/order/dayflip_short_*.py, or launchd. DB access is read-only (mode=ro).

Steps:
  1. Reconstruct the FROZEN_SPEC_V1 qualifying candidate set (seat filters:
     accumulation exclusion, manual pair exclusion, high-flip requirement)
     from events.json, cross-checked against the frozen spec's reported
     165 in-sample trades.
  2. Join qualifying (stock, signal_date) pairs against all_trades.csv (which
     already has tick-replayed entry/exit/pnl_pct for the superset of
     fgap>=6% candidates) + the forward_test JSON (2026-07-09..08-06).
  3. Reconstruct rev_yoy_3m PIT-correct per trade: monthly revenue YoY,
     3-month trailing mean, winsorized [-90,300]%, gated on a revenue
     "known as of" date = 10th of the month following the revenue period
     (statutory TW monthly-revenue disclosure deadline), using stock_id
     revenue from stock_financial_history (DB) or FinMind fallback fetch
     for stocks missing DB coverage.
  4. Tercile-split trades by rev_yoy_3m, compare P&L / hit-rate / fgap.

Output: reports/research/dayflip_revenue_momentum_filter/{trades_with_revyoy.csv, summary.json}
"""
from __future__ import annotations

import csv
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from statistics import mean, median, pstdev

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
import stock_db  # noqa: E402
from finmind_client import fetch_finmind  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / "reports/research/branch-footprint-screen"
OUT_DIR = BASE / "dayflip_gapup_short"
DEST = ROOT / "reports/research/dayflip_revenue_momentum_filter"
DEST.mkdir(parents=True, exist_ok=True)

SPEC = json.loads((OUT_DIR / "FROZEN_SPEC_V1.json").read_text())
FLIP_TABLE = SPEC["seat_flip_table_frozen"]["values"]
HIGH_FLIP = 0.40
MANUAL = {tuple(x) for x in SPEC["signal"]["step2_seat_filters"]["manual_pair_exclusion"]}
MANUAL |= {("9217", "2308"), ("9217", "3653")}  # dayflip_short_signal.py EXTRA_MANUAL_PAIRS
ACC_THR = 0.30
ACC_WINDOW = 60
ACC_MIN_BUY = 100_000_000
ADV_MIN = 800
FGAP_MIN = 0.06
YOY_CLIP_LO, YOY_CLIP_HI = -90.0, 300.0
N_MONTHS = 3
ANNOUNCE_LAG_DAY = 10  # statutory: due by 10th of month following the revenue period


def log(m: str) -> None:
    print(f"[revyoy-filter] {m}", flush=True)


# ---------------------------------------------------------------- step 1/2 --
def build_qualifying_set() -> tuple[set[tuple[str, str]], dict]:
    ev = json.loads((OUT_DIR / "events.json").read_text())
    cf = {k: v for k, v in json.loads((OUT_DIR / "futures_daily_cache.json").read_text()).items() if v}
    mega = set(json.loads((BASE / "ab58_xMega_copytrade/mega_blacklist_v1.json").read_text())["symbols"])
    futmap = json.loads((OUT_DIR / "stock_futures_universe.json").read_text())["map"]

    grp: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for e in ev:
        if e["sid"] in mega or e["sid"].startswith("00") or e["sid"] not in futmap or e["sid"] not in cf:
            continue
        grp[(e["sid"], e["date"])].append(e)
    log(f"raw (sid,date) groups after mega/00/futmap/cf filter: {len(grp)}")

    con = sqlite3.connect(f"file:{stock_db.DEFAULT_DB_PATH}?mode=ro", uri=True)
    lo = "2024-01-01"
    branch: dict[str, dict[str, dict[str, tuple]]] = defaultdict(lambda: defaultdict(dict))
    px: dict[tuple[str, str], float] = {}
    seats_needed = {e["tid"] for es in grp.values() for e in es}
    for tid in seats_needed:
        for d, sid, b, s in con.execute(
            "SELECT trade_date, stock_id, buy, sell FROM stock_broker_branch_daily "
            "WHERE securities_trader_id=? AND trade_date BETWEEN ? AND ?",
            (tid, lo, "2026-08-07"),
        ):
            branch[tid][str(sid)][str(d)] = (float(b or 0), float(s or 0))
    for sid, d, c in con.execute(
        "SELECT stock_id, trade_date, close FROM stock_daily_bars "
        "WHERE source='finmind' AND trade_date BETWEEN ? AND ? AND close>0",
        (lo, "2026-08-07"),
    ):
        px[(str(sid), str(d))] = float(c)
    con.close()
    cal = sorted({d for (_, d) in px})
    ci = {d: i for i, d in enumerate(cal)}

    def net_ratio(tid: str, sid: str, d0: str) -> float | None:
        i = ci.get(d0)
        if i is None or i < ACC_WINDOW:
            return None
        tb = ts = 0.0
        for d in cal[i - ACC_WINDOW:i]:
            b, s = (branch[tid].get(sid) or {}).get(d, (0.0, 0.0))
            p = px.get((sid, d))
            if p is None:
                continue
            tb += b * p
            ts += s * p
        return None if tb < ACC_MIN_BUY else (tb - ts) / tb

    qualifying: set[tuple[str, str]] = set()
    for (sid, d0), es in grp.items():
        keep = []
        for e in es:
            if (e["tid"], sid) in MANUAL:
                continue
            nr = net_ratio(e["tid"], sid, d0)
            if nr is not None and nr >= ACC_THR:
                continue
            keep.append(e)
        if not keep or not any(FLIP_TABLE.get(e["tid"], 0) >= HIGH_FLIP for e in keep):
            continue
        # liquidity + fgap: apply from futures cache (same as spec step1/3)
        m = cf.get(sid) or {}
        ds = sorted(m)
        if d0 not in ds:
            continue
        i = ds.index(d0)
        if i < 20 or i + 1 >= len(ds):
            continue
        adv = mean(m[x][4] for x in ds[i - 20:i])
        if adv < ADV_MIN:
            continue
        d1 = ds[i + 1]
        fo, pf = m[d1][0], m[d0][1]
        if fo <= 0 or pf <= 0:
            continue
        fgap = fo / pf - 1
        if fgap < FGAP_MIN:
            continue
        qualifying.add((sid, d0))

    n_is = sum(1 for (sid, d0) in qualifying if "2024-07-01" <= d0 <= "2026-07-08")
    log(f"qualifying (sid,date) total={len(qualifying)}  IS-window(<=2026-07-08)={n_is}  "
        f"(spec reports 165 IS trades — some day may have >1 trade so groups != trades)")
    return qualifying, {"n_qualifying_groups": len(qualifying), "n_is_groups": n_is}


# --------------------------------------------------------------------- step 2b --
def load_trade_outcomes() -> tuple[list[dict], list[dict]]:
    """All_trades.csv (superset from events.json, tick-replayed — needs qualifying-set
    filter applied by caller) + forward-test JSON trades (already spec-filtered by
    run_dayflip_forward_test.py; events.json doesn't cover 2026-07-09+ so these can't
    be re-derived from the qualifying-set path and are trusted as-is)."""
    rows = list(csv.DictReader(open(OUT_DIR / "all_trades.csv")))
    csv_trades = [dict(signal_date=r["signal_date"], trade_date=r["trade_date"], stock=r["stock"],
                        pnl_pct=float(r["pnl_pct"]), how=r["how"], fgap=float(r["fgap"]),
                        n_seats=int(r["n_seats"])) for r in rows]
    fwd = json.loads((OUT_DIR / "forward_test_2026-07-09_2026-08-06.json").read_text())
    fwd_trades = [dict(signal_date=t["signal_date"], trade_date=t["trade_date"], stock=t["stock"],
                        pnl_pct=float(t["pnl_pct"]), how=t["how"], fgap=float(t["fgap_pct"]),
                        n_seats=int(t["n_seats"])) for t in fwd["trades"]]
    return csv_trades, fwd_trades


# ------------------------------------------------------------------------ step 3 --
def load_revenue_series(stock_ids: set[str]) -> dict[str, dict[str, float]]:
    """stock_id -> {period_date('YYYY-MM-01'): revenue}."""
    con = sqlite3.connect(f"file:{stock_db.DEFAULT_DB_PATH}?mode=ro", uri=True)
    have: dict[str, dict[str, float]] = defaultdict(dict)
    for sid, pd_, v in con.execute(
        "SELECT stock_id, period_date, value FROM stock_financial_history "
        "WHERE metric='revenue' AND period_type='month' AND stock_id IN (%s)"
        % ",".join("?" * len(stock_ids)),
        tuple(stock_ids),
    ):
        have[str(sid)][str(pd_)] = float(v)
    con.close()

    missing = sorted(s for s in stock_ids if s not in have or len(have[s]) < 20)
    log(f"revenue: {len(have)} stocks from DB (>=20mo history); fetching {len(missing)} via FinMind: {missing}")
    for sid in missing:
        try:
            rows = fetch_finmind("TaiwanStockMonthRevenue", sid, date(2022, 1, 1), date(2026, 8, 1), timeout=60)
        except Exception as ex:  # noqa: BLE001
            log(f"  {sid} FETCH ERR {ex}")
            continue
        for r in rows:
            ry, rm = r.get("revenue_year"), r.get("revenue_month")
            rev = r.get("revenue")
            if not ry or not rm or not rev:
                continue
            pd_key = f"{int(ry):04d}-{int(rm):02d}-01"
            have[sid][pd_key] = float(rev)
    return have


def rev_yoy_3m_asof(series: dict[str, float], asof: str) -> tuple[float | None, str | None]:
    """Mirrors rev_yoy_monthly_screen.screen(): last N_MONTHS YoY values with
    known_date <= asof, winsorized, averaged. known_date(period M) = 10th of
    month M+1 (statutory disclosure deadline; FinMind's 'date' field lands here)."""
    items = []  # (period_date, yoy_pct, known_date)
    for pd_str, rev in series.items():
        py, pm = int(pd_str[:4]), int(pd_str[5:7])
        py1, pm1 = (py - 1, pm) if True else (py, pm)
        prior_key = f"{py-1:04d}-{pm:02d}-01"
        prior = series.get(prior_key)
        if not prior or prior <= 0:
            continue
        yoy = (rev / prior - 1) * 100.0
        yoy = max(YOY_CLIP_LO, min(YOY_CLIP_HI, yoy))
        # known date = 10th of the month after the revenue period
        ky, km = (py, pm + 1) if pm < 12 else (py + 1, 1)
        known = f"{ky:04d}-{km:02d}-{ANNOUNCE_LAG_DAY:02d}"
        items.append((pd_str, yoy, known))
    items.sort()
    usable = [x for x in items if x[2] <= asof]
    if len(usable) < N_MONTHS:
        return None, None
    tail = usable[-N_MONTHS:]
    return mean(x[1] for x in tail), tail[-1][0]


# ------------------------------------------------------------------------------ main --
def main() -> None:
    qualifying, qual_meta = build_qualifying_set()
    csv_outcomes, fwd_outcomes = load_trade_outcomes()
    matched = [t for t in csv_outcomes if (t["stock"], t["signal_date"]) in qualifying]
    matched += fwd_outcomes  # already spec-filtered by run_dayflip_forward_test.py
    log(f"trade outcomes: csv-matched={len(matched)-len(fwd_outcomes)}  "
        f"forward-test(pre-filtered)={len(fwd_outcomes)}  total matched={len(matched)}")

    stocks = {t["stock"] for t in matched}
    revenue = load_revenue_series(stocks)

    rows = []
    for t in matched:
        series = revenue.get(t["stock"], {})
        ryoy, latest_period = rev_yoy_3m_asof(series, t["signal_date"])
        rows.append(dict(**t, rev_yoy_3m=ryoy, rev_latest_period=latest_period))

    with_signal = [r for r in rows if r["rev_yoy_3m"] is not None]
    log(f"trades with usable rev_yoy_3m: {len(with_signal)} / {len(rows)} "
        f"({len(rows) - len(with_signal)} missing = insufficient revenue history)")

    with (DEST / "trades_with_revyoy.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else [])
        if rows:
            w.writeheader()
            w.writerows(rows)

    if len(with_signal) < 6:
        log("too few trades with signal for tercile split — aborting summary")
        (DEST / "summary.json").write_text(json.dumps(
            {"error": "insufficient n", "n_matched": len(matched), "n_with_signal": len(with_signal)},
            indent=1))
        return

    srt = sorted(with_signal, key=lambda r: r["rev_yoy_3m"])
    n = len(srt)
    k = n // 3
    low = srt[:k]
    mid = srt[k:n - k]
    high = srt[n - k:]

    def bucket_stats(b: list[dict]) -> dict:
        pnl = [r["pnl_pct"] for r in b]
        sd = pstdev(pnl) if len(pnl) > 1 else 0.0
        return dict(
            n=len(b),
            n_unique_stocks=len({r["stock"] for r in b}),
            mean_pnl_pct=round(mean(pnl), 3),
            median_pnl_pct=round(median(pnl), 3),
            std_pnl_pct=round(sd, 3),
            hit_target_rate_pct=round(100 * mean(r["how"] == "觸價回補" for r in b), 1),
            mean_fgap_pct=round(mean(r["fgap"] for r in b), 2),
            rev_yoy_3m_range=[round(b[0]["rev_yoy_3m"], 1), round(b[-1]["rev_yoy_3m"], 1)],
            stocks=sorted({r["stock"] for r in b}),
        )

    summary = dict(
        n_qualifying_groups=qual_meta["n_qualifying_groups"],
        n_is_groups=qual_meta["n_is_groups"],
        n_matched_to_outcomes=len(matched),
        n_with_usable_rev_yoy=len(with_signal),
        low_tercile=bucket_stats(low),
        mid_tercile=bucket_stats(mid),
        high_tercile=bucket_stats(high),
    )

    # simple two-sample comparisons: high vs low (Mann-Whitney via ranks, no scipy dependency assumed)
    def mannwhitney_u(a: list[float], b: list[float]) -> tuple[float, float]:
        try:
            from scipy.stats import mannwhitneyu
            u, p = mannwhitneyu(a, b, alternative="two-sided")
            return float(u), float(p)
        except Exception:
            return float("nan"), float("nan")

    pnl_low = [r["pnl_pct"] for r in low]
    pnl_high = [r["pnl_pct"] for r in high]
    u, p = mannwhitney_u(pnl_high, pnl_low)
    summary["high_vs_low_mannwhitney"] = {"U": u, "p_value": p}
    summary["high_minus_low_mean_pnl_pct"] = round(mean(pnl_high) - mean(pnl_low), 3)
    summary["high_minus_low_median_pnl_pct"] = round(median(pnl_high) - median(pnl_low), 3)

    (DEST / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1))
    log(f"wrote {DEST/'summary.json'} and {DEST/'trades_with_revyoy.csv'}")
    print(json.dumps(summary, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
