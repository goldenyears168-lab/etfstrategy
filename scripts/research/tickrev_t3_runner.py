#!/usr/bin/env python3
"""tickrev_t3 — shardable full-sample runner for the bar-vs-tick trigger engine.

Wraps `reports/research/channel_lab/slow_cell_tick_trigger_engine.py` WITHOUT
copying its logic: `build_bundle`, `simulate_block_tick`, `summarize_tick` and
the bar baseline `slow_cell_width_percentile_rolling.run_config/summarize` are
all *imported* and called. This file only adds
  * a day list from the CLI (any subset of the corpus, not the hard-coded 13),
  * streaming per-day bundle construction so a 100-day shard does not hold
    100 days of raw ticks in RAM at once,
  * an optional warm-up prefix (days that prime the rolling half-width history
    but whose trades are excluded from the shard's reported numbers),
  * per-trade detail + right-tail (ex-top-N) diagnostics in the output,
  * runtime / peak-RSS accounting so full-sample cost can be extrapolated.

Determinism: no randomness, no parallelism inside the simulation, days are
processed in sorted() order exactly like the upstream run_config/run_config_tick
loops. Running the same --days twice byte-identically reproduces the output
(bar `generated_at`/timing fields excluded -- use --deterministic-out to omit
them entirely for diffing).

CLI
---
  PYTHONPATH=src .venv/bin/python scripts/research/tickrev_t3_runner.py \
      --days 2025-07-15,2025-08-19 --out reports/research/channel_lab/tickrev_t3_shard_x.json
  PYTHONPATH=src .venv/bin/python scripts/research/tickrev_t3_runner.py \
      --days-file reports/research/channel_lab/tickrev_t3_coverage.json \
      --days-file-key usable_days --shard 0/8 \
      --out reports/research/channel_lab/tickrev_t3_shard_00.json
Optional:
  --warmup-days N     prime half_hist with the N usable days immediately before
                      the shard's first day (their trades are dropped)
  --no-trades         omit the per-trade detail arrays (smaller JSON)
  --deterministic-out drop wall-clock/timing fields so two runs diff to zero
  --tag NAME          free-form label copied into the output
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import resource
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / "reports/research/channel_lab"
COVERAGE_DAYS = LAB / "tickrev_t3_coverage_days.json"

CLI_USAGE = (
    "PYTHONPATH=src .venv/bin/python scripts/research/tickrev_t3_runner.py "
    "--days-file reports/research/channel_lab/tickrev_t3_coverage.json "
    "--days-file-key usable_days --shard <i>/<n> [--warmup-days 12] "
    "--out reports/research/channel_lab/tickrev_t3_shard_<i>.json"
)


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


E = _load_module("slow_cell_tick_trigger_engine", LAB / "slow_cell_tick_trigger_engine.py")
W = E.W   # bar engine (slow_cell_width_percentile_rolling)
TL = E.TL  # tick loader (slow_cell_tick_latency_lab)


# --------------------------------------------------------------------------- helpers

def _resolve_days(args) -> list[str]:
    days: list[str] = []
    if args.days:
        days += [d.strip() for d in args.days.split(",") if d.strip()]
    if args.days_file:
        p = Path(args.days_file)
        if not p.is_absolute():
            p = ROOT / p
        txt = p.read_text()
        try:
            obj = json.loads(txt)
        except json.JSONDecodeError:
            obj = [ln.strip() for ln in txt.splitlines() if ln.strip() and not ln.startswith("#")]
        if isinstance(obj, dict):
            obj = obj[args.days_file_key]
        days += [str(x) for x in obj]
    if args.sample13:
        days += list(E.SAMPLE_DAYS)
    days = sorted(set(days))
    if args.shard:
        i, n = (int(x) for x in args.shard.split("/"))
        if not (0 <= i < n):
            raise SystemExit(f"bad --shard {args.shard}")
        # contiguous blocks (NOT round-robin): keeps each shard's rolling
        # half-width history close to what the full-sample run would have seen.
        size = (len(days) + n - 1) // n
        days = days[i * size:(i + 1) * size]
    return days


def _warmup_for(first_day: str, k: int) -> list[str]:
    if k <= 0:
        return []
    cov = json.loads(COVERAGE_DAYS.read_text())
    usable = sorted(d for d, v in cov.items() if v.get("usable"))
    before = [d for d in usable if d < first_day]
    return before[-k:]


def _concentration(trades: list[dict], top_n: tuple[int, ...] = (1, 3, 5, 10)) -> dict:
    pnls = sorted((t["pnl"] for t in trades), reverse=True)
    if not pnls:
        return dict(n=0)
    tot = sum(pnls)
    out = dict(
        n=len(pnls),
        net=round(tot, 1),
        median_pnl=sorted(pnls)[len(pnls) // 2],
        mean_pnl=round(tot / len(pnls), 3),
        max_single_trade_pnl=pnls[0],
        min_single_trade_pnl=pnls[-1],
    )
    for k in top_n:
        top = sum(pnls[:k])
        out[f"top{k}_sum"] = round(top, 1)
        out[f"net_ex_top{k}"] = round(tot - top, 1)
        out[f"top{k}_pct_of_total_net"] = round(100.0 * top / tot, 1) if tot else None
    # symmetric: drop the k worst too, to show it is not a one-sided trim
    for k in top_n:
        out[f"net_ex_worst{k}"] = round(tot - sum(pnls[-k:]), 1)
    return out


def _by_day(trades: list[dict]) -> dict:
    acc: dict[str, float] = {}
    cnt: dict[str, int] = {}
    for tr in trades:
        acc[tr["day"]] = acc.get(tr["day"], 0.0) + tr["pnl"]
        cnt[tr["day"]] = cnt.get(tr["day"], 0) + 1
    return {d: dict(n=cnt[d], net_pts=round(acc[d], 1)) for d in sorted(acc)}


# --------------------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", help="comma-separated YYYY-MM-DD list")
    ap.add_argument("--days-file", help="JSON array / JSON object / newline text file of dates")
    ap.add_argument("--days-file-key", default="usable_days",
                    help="key to read when --days-file is a JSON object (default usable_days)")
    ap.add_argument("--sample13", action="store_true",
                    help="use the engine's own hard-coded 13-day SAMPLE_DAYS (reproduction check)")
    ap.add_argument("--shard", help="i/n -- take contiguous block i of n from the resolved day list")
    ap.add_argument("--warmup-days", type=int, default=0,
                    help="prime half_hist with the N usable days before the shard (trades dropped)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--no-trades", action="store_true")
    ap.add_argument("--deterministic-out", action="store_true",
                    help="omit wall-clock/timing fields so two identical runs diff to zero")
    ap.add_argument("--tag", default="")
    ap.add_argument("--progress", type=int, default=10)
    args = ap.parse_args()

    report_days = _resolve_days(args)
    if not report_days:
        raise SystemExit("no days resolved; pass --days/--days-file/--sample13")
    warm_days = _warmup_for(report_days[0], args.warmup_days)
    all_days = sorted(set(warm_days) | set(report_days))
    warm_set = set(warm_days) - set(report_days)

    print(f"days: report={len(report_days)} warmup={len(warm_set)} total={len(all_days)}", flush=True)

    # ---- streaming tick pass (bundles built and dropped one day at a time) ----
    half_hist = {"day": [], "night": []}
    tick_trades: list[dict] = []
    acct: Counter = Counter()
    acct_at_boundary: Counter | None = None
    trades_at_boundary = 0
    bar_cache: dict[str, list[dict]] = {}
    tick_n_days = 0
    per_day_cost: list[dict] = []
    lag_sec: list[float] = []
    saved_sec: list[float] = []
    missing_days: list[str] = []
    skipped_no_session: list[str] = []
    t_start = time.time()

    started_report = False
    for k, day in enumerate(all_days, 1):
        if day not in warm_set and not started_report:
            # everything from here on counts toward the shard's reported numbers
            acct_at_boundary = Counter(acct)
            trades_at_boundary = len(tick_trades)
            started_report = True
        t0 = time.time()
        try:
            bundle = E.build_bundle(day)
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            missing_days.append(f"{day}:{type(exc).__name__}")
            continue
        if not bundle:
            missing_days.append(f"{day}:no_file_or_no_ticks")
            continue
        t_build = time.time() - t0

        n_tick_before = len(tick_trades)
        used = False
        sess_bars = []
        for sess in ("day", "night"):
            b = bundle.get(sess)
            if b is None:
                continue
            E.simulate_block_tick(day, sess, b["bars"], b["ticks"], b["ranges"],
                                  half_hist[sess], tick_trades, acct, E.PCT, E.WINDOW)
            sess_bars.extend(b["bars"])
            used = True
        if not used:
            skipped_no_session.append(day)
            continue
        tick_n_days += 1
        if sess_bars:
            bar_cache[day] = sess_bars

        # entry-lag stats need the tick arrays -> compute now, before dropping them
        if day not in warm_set:
            for tr in tick_trades[n_tick_before:]:
                b = bundle[tr["session"]]
                ticks, ranges = b["ticks"], b["ranges"]
                sig_ts = ticks[tr["entry_sig_tick"]][0]
                fill_ts = ticks[tr["entry_fill_tick"]][0]
                lag_sec.append((fill_ts - sig_ts).total_seconds())
                tr["entry_fill_time"] = fill_ts.isoformat(sep=" ")
                tr["exit_fill_time"] = ticks[min(tr["exit_fill_tick"], len(ticks) - 1)][0].isoformat(sep=" ")
                nb = tr["entry_sig_bar"] + 1
                if nb < len(ranges):
                    saved_sec.append((ticks[ranges[nb][0]][0] - fill_ts).total_seconds())
        per_day_cost.append(dict(
            day=day, n_ticks=sum(len(bundle[s]["ticks"]) for s in bundle),
            n_bars=len(sess_bars), build_sec=round(t_build, 2),
            total_sec=round(time.time() - t0, 2), warmup=day in warm_set,
        ))
        del bundle
        if args.progress and k % args.progress == 0:
            print(f"  {k}/{len(all_days)} {day} elapsed={time.time()-t_start:.0f}s "
                  f"trades={len(tick_trades)}", flush=True)

    if acct_at_boundary is None:      # every day was warmup (degenerate)
        acct_at_boundary = Counter(acct)
        trades_at_boundary = len(tick_trades)

    tick_report_trades = tick_trades[trades_at_boundary:]
    acct_report = Counter()
    for key, v in acct.items():
        d = v - acct_at_boundary.get(key, 0)
        if d:
            acct_report[key] = d
    # summarize_tick asserts internally (balanced / no same-bar / lock-bar==0)
    tick_summary = E.summarize_tick(tick_report_trades, acct_report)

    # ---- bar baseline: verbatim upstream run_config/summarize on the SAME days ----
    bar_cache_report = {d: b for d, b in bar_cache.items() if d not in warm_set}
    bar_cache_full = bar_cache  # incl. warmup, so half_hist priming matches the tick side
    bar_trades_all, bar_acct_all, _ = W.run_config(bar_cache_full, E.MODE, E.TRIG, E.N_CAP, E.PCT, E.WINDOW)
    if warm_set:
        # recompute the warmup-only accounting so the reported window is clean
        _, bar_acct_warm, _ = W.run_config({d: b for d, b in bar_cache.items() if d in warm_set},
                                           E.MODE, E.TRIG, E.N_CAP, E.PCT, E.WINDOW)
        bar_trades = [t for t in bar_trades_all if t["day"] not in warm_set]
        bar_acct = Counter()
        for key, v in bar_acct_all.items():
            d = v - bar_acct_warm.get(key, 0)
            if d:
                bar_acct[key] = d
        bar_acct_note = ("warm-up days removed by subtracting an independent warmup-only run's "
                         "counters; trade list filtered by day. n_days_used counts report days only.")
    else:
        bar_trades, bar_acct = bar_trades_all, bar_acct_all
        bar_acct_note = "no warm-up: upstream run_config output used verbatim"
    bar_summary = W.summarize(bar_trades, bar_acct) if not warm_set else dict(
        **W.pnl_block(bar_trades),
        by_session={s: W.pnl_block([t for t in bar_trades if t["session"] == s]) for s in ("day", "night")},
        by_exit_reason={r: W.pnl_block([t for t in bar_trades if t["exit_reason"] == r])
                        for r in sorted({t["exit_reason"] for t in bar_trades})},
        touch_resolution=W.touch_resolution(bar_trades),
        accounting=dict(n_signals=bar_acct["signals"], n_fills=bar_acct["fills"],
                        n_closed=bar_acct["closed"],
                        n_skipped=sum(v for k, v in bar_acct.items() if k.startswith("skip:")),
                        skip_reasons={k.split(":", 1)[1]: v for k, v in bar_acct.items() if k.startswith("skip:")},
                        balanced=None),
    )

    n_report_days_used = len({d for d in bar_cache_report})
    elapsed = time.time() - t_start
    peak_rss_gb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 ** 3)

    def _stat(vals, name):
        if not vals:
            return dict(n=0)
        s = sorted(vals)
        return {"n": len(s), f"mean_{name}": round(sum(s) / len(s), 2),
                f"median_{name}": round(s[len(s) // 2], 2),
                f"p90_{name}": round(s[int(0.9 * (len(s) - 1))], 2),
                f"max_{name}": round(s[-1], 2), f"min_{name}": round(s[0], 2)}

    out = dict(
        runner=str(Path(__file__).resolve()),
        engine=str(LAB / "slow_cell_tick_trigger_engine.py"),
        cli_usage=CLI_USAGE,
        tag=args.tag,
        argv=sys.argv[1:],
        config=dict(percentile=E.PCT, window=E.WINDOW, mode=E.MODE,
                    flip_trigger=f"{E.TRIG[0]}:{E.TRIG[1]}", flip_cap_n=E.N_CAP,
                    cost_pt_per_roundtrip=E.COST, sw_th=E.SW_TH,
                    lock_k=E.LOCK_K, cooldown_bars=E.COOLDOWN_BARS),
        days=dict(
            n_requested=len(report_days), requested=report_days,
            n_warmup=len(warm_set), warmup=sorted(warm_set),
            n_days_with_trades_or_bars=n_report_days_used,
            missing_or_empty=missing_days, no_session=skipped_no_session,
        ),
        bar_level=dict(
            n_trades=bar_summary["n_trades"], net_pts=bar_summary["net_pts"],
            avg_pts=bar_summary["avg_pts"], win_rate_pct=bar_summary["win_rate_pct"],
            day_net=bar_summary["by_session"]["day"]["net_pts"],
            night_net=bar_summary["by_session"]["night"]["net_pts"],
            touch_resolution=bar_summary["touch_resolution"],
            by_exit_reason={r: dict(n=v["n_trades"], net_pts=v["net_pts"])
                            for r, v in bar_summary["by_exit_reason"].items()},
            accounting=bar_summary["accounting"], accounting_note=bar_acct_note,
        ),
        tick_level=dict(
            n_trades=tick_summary["n_trades"], net_pts=tick_summary["net_pts"],
            avg_pts=tick_summary["avg_pts"], win_rate_pct=tick_summary["win_rate_pct"],
            day_net=tick_summary["by_session"]["day"]["net_pts"],
            night_net=tick_summary["by_session"]["night"]["net_pts"],
            touch_resolution=tick_summary["touch_resolution"],
            by_exit_reason={r: dict(n=v["n_trades"], net_pts=v["net_pts"])
                            for r, v in tick_summary["by_exit_reason"].items()},
            accounting=tick_summary["accounting"],
            same_bar_check=tick_summary["same_bar_check"],
            entry_signal_to_fill_lag_sec=_stat(lag_sec, "sec"),
            entry_seconds_saved_vs_bar_next_open=_stat(saved_sec, "sec"),
        ),
        causal_lock_check=tick_summary["causal_lock_check"],
        delta=dict(
            net_pts_delta=round(tick_summary["net_pts"] - bar_summary["net_pts"], 1),
            net_pts_delta_pct_of_bar=(round(100.0 * (tick_summary["net_pts"] - bar_summary["net_pts"])
                                            / abs(bar_summary["net_pts"]), 1) if bar_summary["net_pts"] else None),
            n_trades_delta=tick_summary["n_trades"] - bar_summary["n_trades"],
        ),
        right_tail=dict(
            note="策略結構性右尾：ex-topN 才是穩健性判準，平均值會誤導。同時給 ex-worstN 以示這不是單邊修剪。",
            bar=_concentration(bar_trades), tick=_concentration(tick_report_trades),
        ),
        by_day=dict(bar=_by_day(bar_trades), tick=_by_day(tick_report_trades)),
    )
    if not args.no_trades:
        out["trades"] = dict(bar=bar_trades, tick=tick_report_trades)
    if not args.deterministic_out:
        out["cost"] = dict(
            wall_sec_total=round(elapsed, 1),
            wall_sec_per_day=round(elapsed / max(1, len(per_day_cost)), 2),
            peak_rss_gb=round(peak_rss_gb, 3),
            per_day=per_day_cost,
            python=sys.version.split()[0],
        )
        out["generated_at"] = dt.datetime.now().isoformat(timespec="seconds")

    outp = Path(args.out)
    if not outp.is_absolute():
        outp = ROOT / outp
    outp.parent.mkdir(parents=True, exist_ok=True)
    outp.write_text(json.dumps(out, indent=1, ensure_ascii=False, sort_keys=False))
    print(f"[bar]  n={bar_summary['n_trades']:5d} net={bar_summary['net_pts']:>10.1f} "
          f"wr={bar_summary['win_rate_pct']}")
    print(f"[tick] n={tick_summary['n_trades']:5d} net={tick_summary['net_pts']:>10.1f} "
          f"wr={tick_summary['win_rate_pct']}  lock_bar_signals="
          f"{tick_summary['causal_lock_check']['n_signal_on_lock_bar']}")
    print(f"elapsed={elapsed:.1f}s ({elapsed/max(1,len(per_day_cost)):.2f}s/day) "
          f"peak_rss={peak_rss_gb:.2f}GB -> {outp}")


if __name__ == "__main__":
    main()
