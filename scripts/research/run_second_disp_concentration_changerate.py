#!/usr/bin/env python3
"""第二次處置股 x 大戶集中度「變化率」風控分格研究.

Question: dashboard-completeness/holder_concentration.md 測的是「水位」(level),
在全市場尺度上證明只是動能偽裝/無效。第二次處置股是結構不同、更小更波動的宇宙,
且這裡測的是「變化率」(concentration currently rising vs falling),不是水位 ——
兩者皆與原研究不同,值得單獨檢定,不能直接外推 null 結論。

Universe / event log:
  既有「處置股專家池跟單」研究(scripts/research/run_second_disp_top30_l1h7.py 系列)
  在 reports/research/branch-footprint-screen/second_disp_top30_l1h7/shard_*.json
  留下 235 個唯一 (stock_id, signal_date) L1H7 事件(2025-12-16→2026-07-08,30 檔股票),
  每筆已有 PIT-correct 的 excess_pct(股 − 1.15×IX0001, T+1 open → H7 close,30bps 成本)。
  本研究直接借用這組事件當 forward-outcome ground truth,不重新回測進出場。

Concentration change-rate:
  FinMind TaiwanStockHoldingSharesPer,level = "more than 1,000,001"(千張大戶比),週頻。
  PIT cutoff = signal_date - publish_lag(5 曆日)(集保表週五盤後~週六公布,同
  dashboard-completeness 方法論)。change_rate = 最近一筆 big_pct(cutoff 前)
  − 前 N 週(預設 4 週)前一筆 big_pct。要求至少 2 筆可比對的歷史點,否則跳過。

Output: reports/research/second_disp_concentration_changerate/
  events.csv, summary.md

  PYTHONPATH=src .venv/bin/python \\
    scripts/research/run_second_disp_concentration_changerate.py
"""

from __future__ import annotations

import glob
import json
import statistics
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from finmind_client import fetch_finmind  # noqa: E402

SECOND_DISP_DIR = ROOT / "reports/research/branch-footprint-screen/second_disp_top30_l1h7"
OUT = ROOT / "reports/research/second_disp_concentration_changerate"
LEVEL_BIG = "more than 1,000,001"
PUBLISH_LAG_DAYS = 5
CHANGE_WINDOW_DAYS = 28  # ~4 weeks
FETCH_START = date(2024, 1, 1)
FETCH_END = date(2026, 7, 31)
DELAY_SEC = 0.35


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def load_events() -> list[dict]:
    files = glob.glob(str(SECOND_DISP_DIR / "shard_*.json"))
    uniq: dict[tuple, dict] = {}
    for f in files:
        d = json.loads(Path(f).read_text(encoding="utf-8"))
        legs = d.get("legs") if isinstance(d, dict) else d
        if not legs:
            continue
        for leg in legs:
            key = (leg["stock_id"], leg["signal_date"])
            if key not in uniq:
                uniq[key] = leg
    return list(uniq.values())


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


def change_rate_at(series: list[tuple[str, float]], signal_date: str) -> dict | None:
    sig = datetime.strptime(signal_date, "%Y-%m-%d").date()
    cutoff = sig - timedelta(days=PUBLISH_LAG_DAYS)
    # latest point at/before cutoff
    usable = [(d, v) for d, v in series if datetime.strptime(d, "%Y-%m-%d").date() <= cutoff]
    if len(usable) < 2:
        return None
    latest_date, latest_val = usable[-1]
    latest_dt = datetime.strptime(latest_date, "%Y-%m-%d").date()
    target_prior = latest_dt - timedelta(days=CHANGE_WINDOW_DAYS)
    # nearest point <= target_prior (allow +/- 10 day slack toward earlier side)
    prior_candidates = [
        (d, v) for d, v in usable if datetime.strptime(d, "%Y-%m-%d").date() <= target_prior
    ]
    if not prior_candidates:
        return None
    prior_date, prior_val = prior_candidates[-1]
    prior_dt = datetime.strptime(prior_date, "%Y-%m-%d").date()
    span_days = (latest_dt - prior_dt).days
    if span_days < 14 or span_days > 60:
        return None
    return {
        "cutoff": cutoff.isoformat(),
        "latest_date": latest_date,
        "latest_big_pct": latest_val,
        "prior_date": prior_date,
        "prior_big_pct": prior_val,
        "span_days": span_days,
        "change_rate_pp": latest_val - prior_val,
    }


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    events = load_events()
    log(f"loaded {len(events)} unique (stock_id, signal_date) events")
    sids = sorted({e["stock_id"] for e in events})
    log(f"{len(sids)} unique stock_ids -> fetching FinMind TaiwanStockHoldingSharesPer")

    series_cache: dict[str, list[tuple[str, float]]] = {}
    for i, sid in enumerate(sids):
        try:
            series_cache[sid] = fetch_big_pct_series(sid)
            log(f"  [{i+1}/{len(sids)}] {sid}: {len(series_cache[sid])} weekly rows")
        except Exception as exc:  # noqa: BLE001
            log(f"  [{i+1}/{len(sids)}] {sid}: FETCH FAILED {exc}")
            series_cache[sid] = []
        time.sleep(DELAY_SEC)

    rows = []
    skipped_no_history = 0
    for e in events:
        sid = e["stock_id"]
        series = series_cache.get(sid) or []
        cr = change_rate_at(series, e["signal_date"])
        if cr is None:
            skipped_no_history += 1
            continue
        rows.append({**e, **cr})

    log(f"events with usable change-rate: {len(rows)} (skipped {skipped_no_history} no-history)")

    # write events.csv
    import csv

    cols = [
        "stock_id", "signal_date", "entry_date", "exit_date", "excess_pct",
        "stock_pct", "bench_pct", "branch_id", "branch_name", "disp_announce0",
        "cutoff", "latest_date", "latest_big_pct", "prior_date", "prior_big_pct",
        "span_days", "change_rate_pp",
    ]
    with open(OUT / "events.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in cols})

    if not rows:
        (OUT / "summary.md").write_text("no usable events\n", encoding="utf-8")
        return

    # dedupe again at stock-episode level is already done (stock_id, signal_date) unique.
    # Bucket by sign of change_rate_pp.
    rising = [r for r in rows if r["change_rate_pp"] > 0]
    falling = [r for r in rows if r["change_rate_pp"] < 0]
    flat = [r for r in rows if r["change_rate_pp"] == 0]

    def stats(bucket: list[dict]) -> dict:
        if not bucket:
            return {"n": 0}
        ex = [b["excess_pct"] for b in bucket]
        wins = sum(1 for x in ex if x > 0)
        return {
            "n": len(bucket),
            "mean_excess": statistics.mean(ex),
            "median_excess": statistics.median(ex),
            "win_rate": wins / len(bucket),
            "stdev": statistics.pstdev(ex) if len(ex) > 1 else 0.0,
        }

    s_rising = stats(rising)
    s_falling = stats(falling)
    s_flat = stats(flat)
    s_all = stats(rows)

    # tercile split too
    sorted_rows = sorted(rows, key=lambda r: r["change_rate_pp"])
    n = len(sorted_rows)
    t = n // 3
    low = sorted_rows[:t] if t > 0 else []
    mid = sorted_rows[t: n - t] if t > 0 else sorted_rows
    high = sorted_rows[n - t:] if t > 0 else []
    s_low, s_mid, s_high = stats(low), stats(mid), stats(high)

    # simple significance checks: Welch t-test rising vs falling (manual, no scipy dependency assumed)
    def welch_t(a: list[float], b: list[float]) -> tuple[float, float] | None:
        if len(a) < 2 or len(b) < 2:
            return None
        ma, mb = statistics.mean(a), statistics.mean(b)
        va, vb = statistics.variance(a), statistics.variance(b)
        na, nb = len(a), len(b)
        se = (va / na + vb / nb) ** 0.5
        if se == 0:
            return None
        tstat = (ma - mb) / se
        # Welch-Satterthwaite df
        df = (va / na + vb / nb) ** 2 / (
            (va / na) ** 2 / (na - 1) + (vb / nb) ** 2 / (nb - 1)
        )
        return tstat, df

    tt = welch_t([r["excess_pct"] for r in rising], [r["excess_pct"] for r in falling])

    # Spearman-ish rank correlation (manual, no scipy)
    def rank(vals: list[float]) -> list[float]:
        order = sorted(range(len(vals)), key=lambda i: vals[i])
        ranks = [0.0] * len(vals)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and vals[order[j + 1]] == vals[order[i]]:
                j += 1
            avg_rank = (i + j) / 2 + 1
            for k in range(i, j + 1):
                ranks[order[k]] = avg_rank
            i = j + 1
        return ranks

    xs = [r["change_rate_pp"] for r in rows]
    ys = [r["excess_pct"] for r in rows]
    rx, ry = rank(xs), rank(ys)
    n_ = len(rows)
    mx, my = statistics.mean(rx), statistics.mean(ry)
    cov = sum((rx[i] - mx) * (ry[i] - my) for i in range(n_)) / n_
    sx = (sum((v - mx) ** 2 for v in rx) / n_) ** 0.5
    sy = (sum((v - my) ** 2 for v in ry) / n_) ** 0.5
    spearman = cov / (sx * sy) if sx > 0 and sy > 0 else float("nan")

    lines = []
    lines.append("# 第二次處置股 x 大戶集中度變化率風控分格\n")
    lines.append(f"events with usable change-rate: n={len(rows)} / {len(events)} unique episodes "
                 f"(skipped {skipped_no_history} lacking 2+ weekly holding points before PIT cutoff)\n")
    lines.append(f"unique stock_ids: {len(sids)}\n")
    lines.append("\n## Bucket by sign\n")
    lines.append("| bucket | n | mean excess_pct | median excess_pct | win_rate | stdev |")
    lines.append("|---|---:|---:|---:|---:|---:|")
    for name, s in [("rising (Δbig_pct>0)", s_rising), ("falling (Δbig_pct<0)", s_falling), ("flat (=0)", s_flat), ("all", s_all)]:
        if s["n"] == 0:
            lines.append(f"| {name} | 0 | - | - | - | - |")
        else:
            lines.append(f"| {name} | {s['n']} | {s['mean_excess']:.2f} | {s['median_excess']:.2f} | {s['win_rate']:.1%} | {s['stdev']:.2f} |")
    lines.append("\n## Tercile split (by change_rate_pp)\n")
    lines.append("| tercile | n | mean excess_pct | median excess_pct | win_rate |")
    lines.append("|---|---:|---:|---:|---:|")
    for name, s in [("low (falling most)", s_low), ("mid", s_mid), ("high (rising most)", s_high)]:
        if s["n"] == 0:
            lines.append(f"| {name} | 0 | - | - | - |")
        else:
            lines.append(f"| {name} | {s['n']} | {s['mean_excess']:.2f} | {s['median_excess']:.2f} | {s['win_rate']:.1%} |")
    lines.append("\n## Significance\n")
    if tt:
        lines.append(f"- Welch t-test (rising vs falling excess_pct): t={tt[0]:.3f}, df={tt[1]:.1f}\n")
    else:
        lines.append("- Welch t-test: insufficient n in one bucket\n")
    lines.append(f"- Spearman rank corr(change_rate_pp, excess_pct), n={n_}: rho={spearman:.3f}\n")
    (OUT / "summary.md").write_text("\n".join(lines), encoding="utf-8")
    log("wrote events.csv + summary.md")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
