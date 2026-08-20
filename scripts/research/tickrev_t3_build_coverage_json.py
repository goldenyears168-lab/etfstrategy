#!/usr/bin/env python3
"""tickrev_t3 step 4 — assemble reports/research/channel_lab/tickrev_t3_coverage.json:
the corpus inventory + the authoritative `usable_days` array + the runner CLI
contract that downstream shard agents copy verbatim.

Inputs: tickrev_t3_inventory_raw.json, tickrev_t3_coverage_days.json,
        tickrev_t3_vol_raw.json  (produced by steps 1-3)

Run:
    PYTHONPATH=src .venv/bin/python scripts/research/tickrev_t3_build_coverage_json.py
"""
from __future__ import annotations

import datetime as dt
import json
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / "reports/research/channel_lab"
COV = LAB / "tickrev_t3_coverage_days.json"
VOL = LAB / "tickrev_t3_vol_raw.json"
OUT = LAB / "tickrev_t3_coverage.json"

RUNNER = "scripts/research/tickrev_t3_runner.py"


def q(vals, ps=(0, 5, 25, 50, 75, 95, 100)):
    v = sorted(x for x in vals if x is not None)
    if not v:
        return {}
    return {f"p{p}": round(v[min(len(v) - 1, int(p / 100 * len(v)))], 2) for p in ps}


def main() -> None:
    cov = json.loads(COV.read_text())
    vol = json.loads(VOL.read_text())
    dates = sorted(cov)

    # ---- attach volatility profile + buckets ----
    rv_all = []
    for d in dates:
        v = vol.get(d) or {}
        rec = cov[d]
        day = v.get("day") or {}
        nh = v.get("night_head") or {}
        # night tail lives in D+1's file
        d1 = (dt.date.fromisoformat(d) + dt.timedelta(days=1)).isoformat()
        nt = (vol.get(d1) or {}).get("night_tail") or {}
        # only meaningful if D+1's dominant contract == D's (else tail is a different series)
        if cov.get(d1, {}).get("dominant_contract") != rec.get("dominant_contract"):
            nt = {}
        night_rv = (nh.get("rv") or 0.0) + (nt.get("rv") or 0.0)
        night_his = [x for x in (nh.get("hi"), nt.get("hi")) if x is not None]
        night_los = [x for x in (nh.get("lo"), nt.get("lo")) if x is not None]
        rec["vol"] = dict(
            day_range_pt=day.get("rng"), day_rv_pt=day.get("rv"),
            night_range_pt=(round(max(night_his) - min(night_los), 1) if night_his and night_los else None),
            night_rv_pt=round(night_rv, 1) if night_rv else None,
            session_close=day.get("last"),
        )
        tot = (day.get("rv") or 0.0) + night_rv
        rec["vol"]["total_rv_pt"] = round(tot, 1) if tot else None
        if rec.get("usable") and tot:
            rv_all.append((tot, d))

    rv_all.sort()
    n = len(rv_all)
    bucket_names = ["q1_calmest", "q2", "q3", "q4", "q5_wildest"]
    edges = []
    for i, (_, d) in enumerate(rv_all):
        b = min(4, int(5 * i / n))
        cov[d]["vol_bucket"] = bucket_names[b]
    for k in range(1, 5):
        edges.append(round(rv_all[int(k * n / 5)][0], 1))

    usable = [d for d in dates if cov[d].get("usable")]
    roll_ambiguous = [d for d in usable if (cov[d].get("dominant_share_of_single_month") or 1) < 0.80]
    night_partial = [d for d in usable
                     if cov[d].get("night_session_ok") and cov[d].get("night_bar_coverage_pct", 100) < 90]
    day_only = [d for d in usable if cov[d]["day_session_ok"] and not cov[d]["night_session_ok"]]
    night_only = [d for d in usable if cov[d]["night_session_ok"] and not cov[d]["day_session_ok"]]

    by_month = defaultdict(int)
    for d in usable:
        by_month[d[:7]] += 1

    # contiguous file blocks
    blocks, start, prev = [], None, None
    for d in dates:
        dd = dt.date.fromisoformat(d)
        if prev is None or (dd - prev).days > 5:
            if start:
                blocks.append([start.isoformat(), prev.isoformat()])
            start = dd
        prev = dd
    blocks.append([start.isoformat(), prev.isoformat()])

    # ---- stratified fallback sample: 5 vol buckets x N, spread over months ----
    strata = defaultdict(list)
    for d in usable:
        strata[cov[d]["vol_bucket"]].append(d)
    strat_sample = []
    for b in bucket_names:
        days_b = strata[b]
        step = max(1, len(days_b) // 24)
        strat_sample += days_b[::step][:24]
    strat_sample = sorted(set(strat_sample))

    out = {
        "task": "tickrev_t3 — tick corpus coverage inventory + shardable full-sample runner",
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "corpus": {
            "dir": str(LAB / "finmind_tx_tick_by_day"),
            "mirror": "~/goldenstocks-data/cache/tmf_channel/finmind_tx_tick_by_day (same files)",
            "n_files": len(dates),
            "total_bytes": sum(cov[d].get("bytes", 0) for d in dates),
            "date_min": dates[0], "date_max": dates[-1],
            "contiguous_file_blocks": blocks,
            "status_counts": dict(Counter(cov[d]["status"] for d in dates)),
        },
        "usability_rule": (
            "usable == build_sessions() 用 _dominant_outright_contract() 過濾後，至少有一個 session "
            "同時滿足 build_bundle 的 >=60 ticks 與 simulate_block_tick 的 >=60 bars。"
            "Saturday 檔只含前一夜 00:00-05:00 尾巴、本身沒有 session，因此不是 usable day，"
            "但仍是必要輸入（週五夜盤的尾段從它讀出來）。"
        ),
        "n_usable_days": len(usable),
        "usable_days": usable,
        "coverage_summary": {
            "n_files": len(dates),
            "n_usable_days": len(usable),
            "n_empty_files": sum(1 for d in dates if cov[d]["status"] == "empty_file"),
            "n_ok_but_unusable": sum(1 for d in dates if cov[d]["status"] == "ok" and not cov[d]["usable"]),
            "n_saturday_tail_only_files": sum(
                1 for d in dates if cov[d]["status"] == "ok" and not cov[d]["usable"]
                and dt.date.fromisoformat(d).strftime("%a") == "Sat"),
            "n_day_session_usable": sum(1 for d in dates if cov[d].get("day_session_ok")),
            "n_night_session_usable": sum(1 for d in dates if cov[d].get("night_session_ok")),
            "n_both_sessions": len(usable) - len(day_only) - len(night_only),
            "day_only_days": day_only, "night_only_days": night_only,
            "usable_days_by_month": dict(sorted(by_month.items())),
            "n_months_covered": len(by_month),
            "ticks_per_usable_day": q([cov[d]["n_rows"] for d in usable]),
            "day_session_ticks": q([cov[d]["day_ticks"] for d in usable]),
            "night_session_ticks": q([cov[d]["night_ticks"] for d in usable]),
            "day_bar_coverage_pct": q([cov[d]["day_bar_coverage_pct"] for d in usable]),
            "night_bar_coverage_pct": q([cov[d]["night_bar_coverage_pct"] for d in usable]),
        },
        "contamination_flags": {
            "note": (
                "_dominant_outright_contract() 只挑『當日出現次數最多的單一月份』，所以永遠只有一個純"
                "價格序列、不會把價差報價或兩個月份混在一起。真正需要標記的是 roll 前後兩天：那兩天"
                "近月與次月成交量接近，dominant 可能被判給次月（價格水準不同、但仍是完整乾淨的序列）。"
            ),
            "n_roll_ambiguous_days(dominant_share<0.80)": len(roll_ambiguous),
            "roll_ambiguous_days": roll_ambiguous,
            "n_days_dominant_is_deferred_month": sum(
                1 for d in usable if cov[d]["dominant_contract"] > d[:4] + d[5:7]),
            "n_night_partial_days(<90% bar coverage)": len(night_partial),
            "night_partial_days": night_partial,
            "n_usable_days_missing_next_file(night tail truncated)": sum(
                1 for d in usable if not cov[d]["next_file_present"]),
            "spread_quote_rows_total": sum(cov[d].get("spread_rows", 0) for d in dates if cov[d]["status"] == "ok"),
        },
        "volatility_strata": {
            "metric": "total_rv_pt = sum |1-minute close-to-close| over day session + stitched night session, dominant contract only",
            "quintile_edges_pt": edges,
            "counts": dict(Counter(cov[d].get("vol_bucket") for d in usable)),
            "note": "每個 usable day 的 vol_bucket 存在 days[<date>].vol_bucket。",
        },
        "runner": {
            "path": RUNNER,
            "cli_usage": (
                f"PYTHONPATH=src .venv/bin/python {RUNNER} "
                "--days-file reports/research/channel_lab/tickrev_t3_coverage.json "
                "--days-file-key usable_days --shard <i>/<n> --warmup-days 120 "
                "--out reports/research/channel_lab/tickrev_t3_shard_<i>.json"
            ),
            "cli_usage_full_sample_single_process": (
                f"PYTHONPATH=src .venv/bin/python {RUNNER} "
                "--days-file reports/research/channel_lab/tickrev_t3_coverage.json "
                "--days-file-key usable_days "
                "--out reports/research/channel_lab/tickrev_t3_full_sample.json"
            ),
            "cli_usage_reproduction_check": (
                f"PYTHONPATH=src .venv/bin/python {RUNNER} --sample13 "
                "--out reports/research/channel_lab/tickrev_t3_repro13.json"
            ),
            "cli_usage_explicit_days": (
                f"PYTHONPATH=src .venv/bin/python {RUNNER} "
                "--days 2025-07-15,2025-08-19 --out /tmp/x.json"
            ),
            "flags": {
                "--days": "comma-separated YYYY-MM-DD",
                "--days-file": "JSON array / JSON object(+--days-file-key) / newline text",
                "--days-file-key": "default usable_days",
                "--sample13": "the engine's own hard-coded 13-day SAMPLE_DAYS",
                "--shard": "i/n, CONTIGUOUS block i of n (not round-robin)",
                "--warmup-days": "prime the rolling half-width history with N usable days before the shard; their trades are excluded",
                "--no-trades": "omit per-trade arrays",
                "--deterministic-out": "omit wall-clock/timing fields (for byte-diffing two runs)",
                "--tag": "free-form label copied into output",
            },
            "output_contract": [
                "bar_level{n_trades,net_pts,avg_pts,win_rate_pct,day_net,night_net,touch_resolution,by_exit_reason,accounting}",
                "tick_level{... , same_bar_check, entry_signal_to_fill_lag_sec, entry_seconds_saved_vs_bar_next_open}",
                "causal_lock_check.n_signal_on_lock_bar  (asserted == 0 inside summarize_tick)",
                "delta{net_pts_delta,net_pts_delta_pct_of_bar,n_trades_delta}",
                "right_tail{bar,tick}: net_ex_top1/3/5/10 and net_ex_worst1/3/5/10",
                "by_day{bar,tick}: per-day n + net_pts",
                "trades{bar,tick}: per-trade detail (unless --no-trades)",
                "cost{wall_sec_total,wall_sec_per_day,peak_rss_gb,per_day[]}",
            ],
        },
        "cost_and_sharding": {
            "measured_on": "Mac mini (10 cores, 16GB), single process, .venv/bin/python 3.13.14",
            "wall_sec_per_day": 3.39,
            "note_per_day": (
                "2.3-3.4 s/day 端看機器是否同時跑別的 shard；成本幾乎全在 json.loads 一整天的 tick 檔"
                "(~9MB) 與逐 tick 掃描，不是回測邏輯本身。"
            ),
            "full_sample_837_days": {
                "wall_sec": 2835.9, "wall_min": 47.3, "peak_rss_gb": 0.832,
                "verdict": "全樣本單一 process 就跑得完（<50 分鐘、<1GB RAM），不需要抽樣。",
            },
            "sharded_8_way_with_warmup120": {
                "per_shard_days": "105 report + 120 warm-up = 225",
                "measured_wall_sec_one_shard": 843.8, "measured_peak_rss_gb": 0.683,
                "total_cpu_multiplier_vs_full": 2.15,
                "wall_min_if_8_run_in_parallel": "≈14-16",
            },
            "WARM_UP_IS_MANDATORY": (
                "half_hist（rolling ZigZag leg half-width，window=40 legs）跨日累積且不重置。"
                "leg 產生速率極慢：實測 30 個交易日只累積 12 個 day-session leg / 55 個 night leg，"
                "所以 day session 要 ~100 個交易日才填滿 40-leg 視窗。"
                "實測驗證（shard 4/8，105 天）：--warmup-days 120 與 837 天全樣本逐日完全一致"
                "（bar 1240 筆/-3908pt、tick 1293 筆/-2534pt、52 個有交易日 0 天不同）；"
                "--warmup-days 0 則有 9-10/52 天不同，tick net 誤報成 -1894（真值 -2534，差 640pt/34%）。"
                "結論：分片一律帶 --warmup-days 120，否則分片結果不是全樣本的分解。"
            ),
        },
        "full_sample_reference_run": {
            "path": "reports/research/channel_lab/tickrev_t3_full_sample.json",
            "cmd": (
                f"PYTHONPATH=src .venv/bin/python {RUNNER} "
                "--days-file reports/research/channel_lab/tickrev_t3_coverage.json "
                "--days-file-key usable_days --out reports/research/channel_lab/tickrev_t3_full_sample.json"
            ),
            "n_days": 837, "n_days_with_trades": 343,
            "bar": {"n": 10325, "net_pts": -9222.0, "avg_pts": -0.893, "win_rate_pct": 26.93},
            "tick": {"n": 11836, "net_pts": -10453.0, "avg_pts": -0.883, "win_rate_pct": 14.27},
            "delta_net_pts": -1231.0,
            "causal_lock_check_n_signal_on_lock_bar": 0,
            "note": (
                "13 天樣本的 +1363pt(+220.2%) 在 837 天全樣本上反轉為 -1231pt(-13.3%)，"
                "且兩個引擎在全樣本都是大幅淨虧。逐日 delta 中位數 +4.0pt、177 天 tick 較優 vs "
                "165 天 bar 較優（近乎擲硬幣）；扣掉 |delta| 最大的 10 天後總 delta 變成 +2351pt —— "
                "也就是總和的正負完全由 <10 天決定。分年 delta：2024 +1704 / 2025 +620 / 2026 -3665。"
            ),
        },
        "stratified_fallback_sample": {
            "note": (
                "全樣本其實跑得完（見 cost），這份只是『若必須抽樣』的替代方案：先用 total_rv_pt "
                "把 837 天切五等分（波動五分位），每個分位沿時間軸等距取 ~24 天，因此同時涵蓋"
                "『安靜 tape』與『暴力 tape』兩端，而不是每月抽一天（那種抽法會系統性避開連續高波動群集）。"
            ),
            "n_days": len(strat_sample),
            "days": strat_sample,
            "by_bucket": dict(Counter(cov[d]["vol_bucket"] for d in strat_sample)),
        },
        "days": {d: cov[d] for d in dates},
    }
    OUT.write_text(json.dumps(out, indent=1, ensure_ascii=False))
    print(f"usable={len(usable)} files={len(dates)} -> {OUT}")
    print("quintile edges", edges)
    print("strat sample", len(strat_sample))


if __name__ == "__main__":
    main()
