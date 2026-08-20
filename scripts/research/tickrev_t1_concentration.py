#!/usr/bin/env python3
"""T1 — 從原始交易明細重算集中度，判定 net_ex_top5 到底是 -445 還是 +548。

不信任 slow_cell_tick_trigger_engine.json 裡任何彙總數字：直接 import 該引擎模組
（不修改它），重跑 bar 版與 tick 版，輸出逐筆交易明細，然後自己算：
  * net_ex_topN (N = 1/3/5/10/20) 與 net_ex_top{1,2,5,10}%
  * median / mean / std / skew of per-trade pnl
  * 以日為單位的淨值序列，tick 勝過 bar 的天數
  * 對日淨值差做 sign test / bootstrap

Run:
    PYTHONPATH=src .venv/bin/python scripts/research/tickrev_t1_concentration.py
"""
from __future__ import annotations

import csv
import importlib.util
import json
import math
import random
from pathlib import Path

PROJ = Path(__file__).resolve().parents[2]
LAB = PROJ / "reports" / "research" / "channel_lab"
OUT_JSON = LAB / "tickrev_t1_concentration.json"
OUT_TRADES_BAR = LAB / "tickrev_t1_trades_bar.csv"
OUT_TRADES_TICK = LAB / "tickrev_t1_trades_tick.csv"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


E = _load_module("slow_cell_tick_trigger_engine", LAB / "slow_cell_tick_trigger_engine.py")
W = E.W


# --------------------------------------------------------------------------
# stats helpers (no scipy dependency)
# --------------------------------------------------------------------------
def _median(xs):
    s = sorted(xs)
    n = len(s)
    if n == 0:
        return None
    return s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2])


def _std(xs):
    n = len(xs)
    if n < 2:
        return None
    m = sum(xs) / n
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))


def _skew(xs):
    n = len(xs)
    if n < 3:
        return None
    m = sum(xs) / n
    s = _std(xs)
    if not s:
        return None
    g1 = sum(((x - m) / s) ** 3 for x in xs) / n
    return g1 * math.sqrt(n * (n - 1)) / (n - 2)  # sample-adjusted (Fisher-Pearson)


def _binom_two_sided(k, n, p=0.5):
    """two-sided exact binomial p-value."""
    if n == 0:
        return None
    def pmf(i):
        return math.comb(n, i) * p ** i * (1 - p) ** (n - i)
    obs = pmf(k)
    tol = 1e-12
    return min(1.0, sum(pmf(i) for i in range(n + 1) if pmf(i) <= obs + tol))


def concentration_curve(trades):
    pnls = sorted((t["pnl"] for t in trades), reverse=True)
    n = len(pnls)
    tot = round(sum(pnls), 2)
    out = {
        "n": n,
        "net_pts": tot,
        "mean_pnl": round(tot / n, 4) if n else None,
        "median_pnl": _median(pnls),
        "std_pnl": round(_std(pnls), 3) if n > 1 else None,
        "skew_pnl": round(_skew(pnls), 3) if n > 2 else None,
        "max_single_trade_pnl": pnls[0] if n else None,
        "min_single_trade_pnl": pnls[-1] if n else None,
        "n_win": sum(1 for p in pnls if p > 0),
        "win_rate_pct": round(100.0 * sum(1 for p in pnls if p > 0) / n, 2) if n else None,
    }
    drop_top = {}
    for k in (1, 3, 5, 10, 20):
        if k > n:
            continue
        drop_top[f"drop_top{k}"] = {
            "topk_sum": round(sum(pnls[:k]), 2),
            "topk_pct_of_total_net": round(100.0 * sum(pnls[:k]) / tot, 1) if tot else None,
            "net_ex_topk": round(tot - sum(pnls[:k]), 2),
            "n_remaining": n - k,
        }
    out["drop_top_n"] = drop_top

    drop_pct = {}
    for q in (1, 2, 5, 10):
        k = max(1, int(round(n * q / 100.0)))
        drop_pct[f"drop_top{q}pct"] = {
            "k_dropped": k,
            "net_ex": round(tot - sum(pnls[:k]), 2),
        }
    out["drop_top_pct"] = drop_pct

    trim = {}
    for k in (1, 3, 5, 10, 20):
        if 2 * k >= n:
            continue
        trim[f"trim{k}_each_tail"] = round(sum(pnls[k:n - k]), 2)
    out["two_sided_trimmed_net"] = trim

    # winsorise the top tail at the p-th percentile of the pnl distribution
    wins = {}
    asc = sorted(pnls)
    for q in (95, 99):
        idx = min(n - 1, int(math.ceil(q / 100.0 * n)) - 1)
        cap = asc[idx]
        wins[f"winsor_top_at_p{q}"] = {
            "cap_pt": cap,
            "net": round(sum(min(p, cap) for p in pnls), 2),
        }
    out["winsorised_net"] = wins
    return out


def main():
    bundles = {}
    missing = []
    for day in E.SAMPLE_DAYS:
        b = E.build_bundle(day)
        if b:
            bundles[day] = b
        else:
            missing.append(day)
        print(f"  bundle {day}: {'ok' if b else 'EMPTY'} sessions={sorted(b) if b else []}", flush=True)
    print(f"built bundles for {len(bundles)}/{len(E.SAMPLE_DAYS)} days; missing={missing}", flush=True)

    # ---- bar baseline ----
    bar_trades, bar_summary, bar_n_days = E.run_bar_baseline(bundles, E.PCT, E.WINDOW)
    print(f"[bar]  n={bar_summary['n_trades']} net={bar_summary['net_pts']}", flush=True)

    # ---- tick engine ----
    tick_trades, tick_acct, tick_n_days = E.run_config_tick(bundles, E.PCT, E.WINDOW)
    tick_summary = E.summarize_tick(tick_trades, tick_acct)  # contains the causal_lock_check asserts
    print(f"[tick] n={tick_summary['n_trades']} net={tick_summary['net_pts']}", flush=True)

    lock_check = tick_summary["causal_lock_check"]["n_signal_on_lock_bar"]
    assert lock_check == 0, "look-ahead reintroduced!"

    # ---- resolve timestamps for the per-trade detail ----
    # bar engine works on a merged day+night bar list per day (E.run_bar_baseline's cache)
    bar_time_map = {}
    for day, sess_map in bundles.items():
        merged = []
        for sess in ("day", "night"):
            b = sess_map.get(sess)
            if b is not None:
                merged.extend(b["bars"])
        bar_time_map[day] = [bb["t"] for bb in merged]

    bar_rows = []
    for tr in bar_trades:
        tm = bar_time_map.get(tr["day"], [])
        bar_rows.append({
            "day": tr["day"], "session": tr["session"], "side": tr["side"], "kind": tr["kind"],
            "entry_sig_bar": tr["entry_sig"], "entry_fill_bar": tr["entry_fill"],
            "entry_time": tm[tr["entry_fill"]] if tr["entry_fill"] < len(tm) else "",
            "entry_px": tr["entry_px"],
            "exit_fill_bar": tr["exit_fill"],
            "exit_time": tm[tr["exit_fill"]] if tr["exit_fill"] < len(tm) else "",
            "exit_px": tr["exit_px"], "exit_reason": tr["exit_reason"],
            "hold_bars": tr["exit_fill"] - tr["entry_fill"],
            "pnl": tr["pnl"],
        })

    tick_rows = []
    for tr in tick_trades:
        bd = bundles[tr["day"]][tr["session"]]
        ticks = bd["ticks"]
        e_ts = ticks[tr["entry_fill_tick"]][0]
        x_ts = ticks[tr["exit_fill_tick"]][0]
        tick_rows.append({
            "day": tr["day"], "session": tr["session"], "side": tr["side"], "kind": tr["kind"],
            "entry_sig_bar": tr["entry_sig_bar"], "entry_fill_bar": tr["entry_fill_bar"],
            "entry_time": e_ts.strftime("%Y-%m-%d %H:%M:%S"), "entry_px": tr["entry_px"],
            "exit_fill_bar": tr["exit_fill_bar"],
            "exit_time": x_ts.strftime("%Y-%m-%d %H:%M:%S"),
            "exit_px": tr["exit_px"], "exit_reason": tr["exit_reason"],
            "hold_bars": tr["exit_fill_bar"] - tr["entry_fill_bar"],
            "hold_sec": (x_ts - e_ts).total_seconds(),
            "pnl": tr["pnl"],
        })

    for path, rows in ((OUT_TRADES_BAR, bar_rows), (OUT_TRADES_TICK, tick_rows)):
        with path.open("w", newline="") as fh:
            wcsv = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            wcsv.writeheader()
            wcsv.writerows(rows)
        print(f"trades -> {path} ({len(rows)} rows)", flush=True)

    # ---- concentration ----
    conc = {"bar": concentration_curve(bar_trades), "tick": concentration_curve(tick_trades)}

    # ---- per-day nets ----
    days = sorted(bundles)
    def day_net(trs):
        acc = {d: 0.0 for d in days}
        for t in trs:
            acc[t["day"]] = acc.get(t["day"], 0.0) + t["pnl"]
        return {d: round(v, 2) for d, v in acc.items()}

    def day_n(trs):
        acc = {d: 0 for d in days}
        for t in trs:
            acc[t["day"]] = acc.get(t["day"], 0) + 1
        return acc

    bar_day, tick_day = day_net(bar_trades), day_net(tick_trades)
    bar_dn, tick_dn = day_n(bar_trades), day_n(tick_trades)
    diffs = {d: round(tick_day[d] - bar_day[d], 2) for d in days}
    active = [d for d in days if bar_dn[d] > 0 or tick_dn[d] > 0]
    diffs_active = [diffs[d] for d in active]
    n_tick_wins = sum(1 for d in active if diffs[d] > 0)
    n_ties = sum(1 for d in active if diffs[d] == 0)

    # day-session level (finer unit)
    def sess_net(trs):
        acc = {}
        for t in trs:
            acc[f"{t['day']}|{t['session']}"] = acc.get(f"{t['day']}|{t['session']}", 0.0) + t["pnl"]
        return acc
    bs, ts = sess_net(bar_trades), sess_net(tick_trades)
    all_sess = sorted(set(bs) | set(ts))
    sess_diff = {k: round(ts.get(k, 0.0) - bs.get(k, 0.0), 2) for k in all_sess}
    n_sess_tick_wins = sum(1 for v in sess_diff.values() if v > 0)

    # sign test on active days
    k = n_tick_wins
    nn = len([d for d in active if diffs[d] != 0])
    sign_p = _binom_two_sided(k, nn) if nn else None

    # bootstrap the day-level mean diff
    random.seed(20260820)
    boots = []
    if diffs_active:
        for _ in range(20000):
            samp = [random.choice(diffs_active) for _ in diffs_active]
            boots.append(sum(samp) / len(samp))
        boots.sort()
    ci = (round(boots[int(0.025 * len(boots))], 2), round(boots[int(0.975 * len(boots))], 2)) if boots else None
    p_boot = (2 * min(sum(1 for b in boots if b <= 0), sum(1 for b in boots if b >= 0)) / len(boots)) if boots else None

    # ---- exit-reason decomposition: how much of the net is the session_end
    # mark-to-last-print bucket (a forced close, not a rule the strategy could
    # actually harvest), and does the improvement survive removing it? ----
    def exit_block(trs):
        acc = {}
        for t in trs:
            r = t["exit_reason"]
            d = acc.setdefault(r, {"n": 0, "net": 0.0})
            d["n"] += 1
            d["net"] += t["pnl"]
        return {r: {"n": d["n"], "net": round(d["net"], 2)} for r, d in sorted(acc.items())}

    def _sub(trs, keep):
        return [t for t in trs if keep(t)]

    bar_rule = _sub(bar_trades, lambda t: t["exit_reason"] != "session_end")
    tick_rule = _sub(tick_trades, lambda t: t["exit_reason"] != "session_end")

    def day_net_of(trs):
        acc = {d: 0.0 for d in days}
        for t in trs:
            acc[t["day"]] += t["pnl"]
        return {d: round(v, 2) for d, v in acc.items()}

    bd_r, td_r = day_net_of(bar_rule), day_net_of(tick_rule)
    diff_rule = {d: round(td_r[d] - bd_r[d], 2) for d in days}
    act_r = [d for d in days if bd_r[d] or td_r[d]]
    n_win_r = sum(1 for d in act_r if diff_rule[d] > 0)

    exit_decomp = {
        "note": ("session_end = 該 session 收盤強制平倉的 mark-to-last-print，"
                 "不是策略自己觸發的出場規則；兩個引擎的全部正淨值都在這個桶裡。"),
        "bar": exit_block(bar_trades),
        "tick": exit_block(tick_trades),
        "net_ex_session_end": {
            "bar": {"n": len(bar_rule), "net": round(sum(t["pnl"] for t in bar_rule), 2)},
            "tick": {"n": len(tick_rule), "net": round(sum(t["pnl"] for t in tick_rule), 2)},
            "delta_tick_minus_bar": round(sum(t["pnl"] for t in tick_rule)
                                          - sum(t["pnl"] for t in bar_rule), 2),
        },
        "ex_session_end_concentration": {
            "bar": concentration_curve(bar_rule) if bar_rule else None,
            "tick": concentration_curve(tick_rule) if tick_rule else None,
        },
        "ex_session_end_by_day": {
            "bar_net": bd_r, "tick_net": td_r, "diff_tick_minus_bar": diff_rule,
            "n_days_active": len(act_r), "n_days_tick_wins": n_win_r,
            "sign_test_two_sided_p": (round(_binom_two_sided(
                n_win_r, sum(1 for d in act_r if diff_rule[d] != 0)), 4) if act_r else None),
        },
    }

    # ---- permutation test on the per-trade pnl means (unpaired; trades are
    # not 1:1 matchable because the two engines fire different counts) ----
    obs_diff = (sum(t["pnl"] for t in tick_trades) / len(tick_trades)
                - sum(t["pnl"] for t in bar_trades) / len(bar_trades))
    pool = [t["pnl"] for t in tick_trades] + [t["pnl"] for t in bar_trades]
    nt = len(tick_trades)
    random.seed(777)
    hits = 0
    NPERM = 20000
    for _ in range(NPERM):
        random.shuffle(pool)
        d = sum(pool[:nt]) / nt - sum(pool[nt:]) / (len(pool) - nt)
        if abs(d) >= abs(obs_diff):
            hits += 1
    perm = {"obs_mean_pnl_diff": round(obs_diff, 3),
            "two_sided_p": round((hits + 1) / (NPERM + 1), 4),
            "note": "unpaired label-shuffle on per-trade pnl; ignores day clustering (optimistic)"}

    # ---- ex-top5 comparison, computed both "global top5" and "per-engine top5" ----
    def net_ex_topk(trs, kk):
        p = sorted((t["pnl"] for t in trs), reverse=True)
        return round(sum(p) - sum(p[:kk]), 2)

    verdict = {
        "json_field_net_ex_top5": {"bar": -2484.0, "tick": -445.0},
        "json_caveat_prose_net_ex_top5": {"bar": -2484.0, "tick": 548.0},
        "recomputed_net_ex_top5": {"bar": net_ex_topk(bar_trades, 5), "tick": net_ex_topk(tick_trades, 5)},
    }

    out = {
        "task": "T1 — recompute ex-top5 concentration from raw per-trade detail",
        "script": str(Path(__file__).resolve()),
        "engine_imported": str(LAB / "slow_cell_tick_trigger_engine.py"),
        "engine_unmodified": True,
        "config": {"percentile": E.PCT, "window": E.WINDOW, "mode": E.MODE,
                   "cost_pt_per_roundtrip": E.COST, "sw_th": E.SW_TH,
                   "lock_k": E.LOCK_K, "cooldown_bars": E.COOLDOWN_BARS},
        "days_requested": E.SAMPLE_DAYS,
        "days_with_data": days,
        "days_missing": missing,
        "causal_lock_check_n_signal_on_lock_bar": lock_check,
        "headline": {
            "bar": {"n": len(bar_trades), "net_pts": round(sum(t["pnl"] for t in bar_trades), 2)},
            "tick": {"n": len(tick_trades), "net_pts": round(sum(t["pnl"] for t in tick_trades), 2)},
        },
        "ex_top5_verdict": verdict,
        "concentration": conc,
        "exit_reason_decomposition": exit_decomp,
        "per_trade_permutation_test": perm,
        "by_day": {
            "bar_net": bar_day, "tick_net": tick_day,
            "bar_n_trades": bar_dn, "tick_n_trades": tick_dn,
            "diff_tick_minus_bar": diffs,
            "n_days_total": len(days),
            "n_days_active": len(active),
            "n_days_tick_wins": n_tick_wins,
            "n_days_tie": n_ties,
            "n_days_bar_wins": len(active) - n_tick_wins - n_ties,
            "sign_test_two_sided_p": round(sign_p, 4) if sign_p is not None else None,
            "mean_daily_diff": round(sum(diffs_active) / len(diffs_active), 2) if diffs_active else None,
            "median_daily_diff": _median(diffs_active),
            "bootstrap_ci95_mean_daily_diff": ci,
            "bootstrap_two_sided_p": round(p_boot, 4) if p_boot is not None else None,
        },
        "by_day_session": {
            "diff_tick_minus_bar": sess_diff,
            "n_sessions": len(all_sess),
            "n_sessions_tick_wins": n_sess_tick_wins,
            "sign_test_two_sided_p": (round(_binom_two_sided(
                n_sess_tick_wins, sum(1 for v in sess_diff.values() if v != 0)), 4)
                if any(v != 0 for v in sess_diff.values()) else None),
        },
        "trade_detail_csv": {"bar": str(OUT_TRADES_BAR), "tick": str(OUT_TRADES_TICK)},
    }
    OUT_JSON.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    print(f"\n-> {OUT_JSON}")
    print(json.dumps(out["ex_top5_verdict"], ensure_ascii=False, indent=2))
    print(json.dumps(out["by_day"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
