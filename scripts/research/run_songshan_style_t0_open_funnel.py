#!/usr/bin/env python3
"""T0 signal-day + T+1 open funnel on Songshan-style adaptive legs.

Prior adaptive Top30 screen did NOT exclude T0 morphology or open chase.
This study asks: do bad T0 / open shapes share features, and does filtering
change median r_adj meaningfully?

Universe: robust seats from adaptive leaderboard (n_all≥15 ∩ n_oos≥8)
plus fixed 凱基松山 9217 @ 0.5億.

  PYTHONPATH=src .venv/bin/python \\
    scripts/research/run_songshan_style_t0_open_funnel.py \\
    --db data/replica/stocks.db
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import sys
import time
from datetime import datetime
from pathlib import Path
from statistics import median

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

OUT = (
    ROOT
    / "reports/research/branch-footprint-screen"
    / "songshan_style_adaptive_top30"
)
BOARD = OUT / "adaptive_l1h7_leaderboard.csv"
MEGA_JSON = (
    ROOT
    / "reports/research/branch-footprint-screen"
    / "ab58_xMega_copytrade"
    / "mega_blacklist_v1.json"
)

START = "2024-07-01"
OOS_CUT = "2026-01-01"
COST = 0.003
BETA = 1.15
HOLD = 7
ANCHOR = "9217"
ANCHOR_FLOOR = 0.5e8


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def load_screen_mod():
    path = ROOT / "scripts/research/run_songshan_style_adaptive_branch_screen.py"
    spec = importlib.util.spec_from_file_location("ss_screen", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def fmt(x: float | None) -> str:
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return ""
    return f"{x:+.2f}"


def pack(legs: list[dict]) -> dict:
    if not legs:
        return {
            "n": 0,
            "med": float("nan"),
            "mean": float("nan"),
            "win": float("nan"),
            "n_oos": 0,
            "med_oos": float("nan"),
        }
    xs = [x["radj_pct"] for x in legs]
    oos = [x for x in legs if x["signal_date"] >= OOS_CUT]
    ox = [x["radj_pct"] for x in oos]
    return {
        "n": len(legs),
        "med": float(median(xs)),
        "mean": float(np.mean(xs)),
        "win": float(np.mean([v > 0 for v in xs])),
        "n_oos": len(oos),
        "med_oos": float(median(ox)) if ox else float("nan"),
    }


def enrich_t0_open(
    legs: list[dict],
    *,
    ohlc_full: dict[tuple[str, str], tuple[float, float, float, float]],
    calendar: list[str],
    date_index: dict[str, int],
) -> list[dict]:
    """Attach T0 daily morphology + T+1 open chase."""
    out: list[dict] = []
    for lg in legs:
        sid = lg["stock_id"]
        sig = lg["signal_date"]
        si = date_index.get(sig)
        if si is None or si < 1:
            continue
        prev_d = calendar[si - 1]
        t0 = ohlc_full.get((sid, sig))
        prev = ohlc_full.get((sid, prev_d))
        t1 = ohlc_full.get((sid, lg["entry_date"]))
        if not t0 or not prev or not t1:
            continue
        o0, h0, l0, c0 = t0
        _op, _hp, _lp, c_prev = prev
        o1 = t1[0]
        if min(o0, h0, l0, c0, c_prev, o1) <= 0:
            continue
        body_top = max(o0, c0)
        body_bot = min(o0, c0)
        rng = h0 - l0
        row = dict(lg)
        row.update(
            {
                "t0_open": o0,
                "t0_high": h0,
                "t0_low": l0,
                "t0_close": c0,
                "t0_ret": c0 / c_prev - 1.0,
                "t0_gap": o0 / c_prev - 1.0,
                "t0_body": c0 / o0 - 1.0,
                "t0_upper": (h0 - body_top) / o0,
                "t0_lower": (body_bot - l0) / o0,
                "t0_range": (h0 - l0) / o0,
                "t0_close_loc": ((c0 - l0) / rng) if rng > 1e-12 else 0.5,
                "t0_green": int(c0 >= o0),
                "chase": o1 / c0 - 1.0,
            }
        )
        out.append(row)
    return out


def load_ohlc_hl(
    conn, start: str, end: str
) -> dict[tuple[str, str], tuple[float, float, float, float]]:
    rows = conn.execute(
        """
        SELECT stock_id, trade_date, open, high, low, close
        FROM stock_daily_bars
        WHERE source='finmind' AND trade_date>=? AND trade_date<=?
          AND length(stock_id)=4
          AND stock_id GLOB '[0-9][0-9][0-9][0-9]'
          AND open>0 AND high>0 AND low>0 AND close>0
        """,
        (start, end),
    ).fetchall()
    return {
        (str(r[0]), str(r[1])): (
            float(r[2]),
            float(r[3]),
            float(r[4]),
            float(r[5]),
        )
        for r in rows
    }


def tercile_table(legs: list[dict], key: str) -> pd.DataFrame:
    xs = [x for x in legs if x.get(key) is not None and not math.isnan(x[key])]
    if len(xs) < 12:
        return pd.DataFrame()
    vals = np.array([x[key] for x in xs], float)
    q1, q2 = np.quantile(vals, [1 / 3, 2 / 3])
    rows = []
    for label, mask in (
        ("low", vals <= q1),
        ("mid", (vals > q1) & (vals <= q2)),
        ("high", vals > q2),
    ):
        sub = [xs[i] for i, m in enumerate(mask) if m]
        st = pack(sub)
        rows.append(
            {
                "feature": key,
                "bucket": label,
                "lo": float(vals[mask].min()) if mask.any() else float("nan"),
                "hi": float(vals[mask].max()) if mask.any() else float("nan"),
                **st,
            }
        )
    return pd.DataFrame(rows)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=ROOT / "data/replica/stocks.db")
    ap.add_argument("--end", default=None)
    ap.add_argument("--min-n", type=int, default=15)
    ap.add_argument("--min-oos", type=int, default=8)
    ap.add_argument("--max-branches", type=int, default=20)
    args = ap.parse_args()

    t0 = time.time()
    m = load_screen_mod()
    board = pd.read_csv(BOARD)
    robust = board[
        (board["n_all"] >= args.min_n) & (board["n_oos"] >= args.min_oos)
    ].sort_values("med_radj_all", ascending=False)
    robust = robust.head(args.max_branches).copy()
    # Ensure Songshan present with fixed floor.
    if ANCHOR not in set(robust["id"].astype(str)):
        row = board[board["id"].astype(str) == ANCHOR]
        if len(row):
            robust = pd.concat([robust, row], ignore_index=True)

    seats = []
    for r in robust.itertuples(index=False):
        floor = ANCHOR_FLOOR if str(r.id) == ANCHOR else float(r.floor)
        seats.append(
            {
                "id": str(r.id),
                "name": str(r.name),
                "floor": floor,
            }
        )
    log(f"seats n={len(seats)}: " + ", ".join(f"{s['id']}:{s['name']}" for s in seats[:8]) + " …")

    conn = m.connect_ro(args.db)
    try:
        end = args.end
        if end is None:
            end = str(
                conn.execute(
                    "SELECT MAX(trade_date) FROM stock_daily_bars "
                    "WHERE source='finmind' AND stock_id='2330'"
                ).fetchone()[0]
            )
        mega = m.load_mega()
        cal = m.load_calendar(
            conn,
            START,
            (pd.Timestamp(end) + pd.Timedelta(days=40)).strftime("%Y-%m-%d"),
        )
        date_index = {d: i for i, d in enumerate(cal)}
        bench = m.load_benchmark(conn, cal[0], cal[-1])
        ohlc_oc = m.load_ohlc(conn, cal[0], cal[-1])
        log("loading OHLC HL …")
        ohlc_hl = load_ohlc_hl(conn, cal[0], cal[-1])
        log(f"ohlc_hl={len(ohlc_hl):,}")

        ids = [s["id"] for s in seats]
        floor_map = {s["id"]: s["floor"] for s in seats}
        name_map = {s["id"]: s["name"] for s in seats}

        # Load tape in one batch (≤20 seats).
        log("loading branch day amounts …")
        day_amt = m.load_branch_day_amts(conn, ids, start=START, end=end)
        warm = START
        if START in date_index and date_index[START] >= 4:
            warm = cal[date_index[START] - 4]
        cal_sig = [d for d in cal if warm <= d <= end]
        log("building 5d panels …")
        panel = m.build_5d_panel(day_amt, cal_sig)
        panel = panel[panel["d"] >= START]

        all_legs: list[dict] = []
        for bid, sub in panel.groupby("branch_id"):
            bid = str(bid)
            if bid not in floor_map:
                continue
            ev = m.filter_signals(sub, floor=floor_map[bid], mega=mega)
            legs = m.outcome_legs(
                ev,
                calendar=cal,
                date_index=date_index,
                ohlc=ohlc_oc,
                benchmark=bench,
            )
            for lg in legs:
                lg["branch_name"] = name_map.get(bid, bid)
                lg["floor"] = floor_map[bid]
            all_legs.extend(legs)
            log(f"  {bid} {name_map.get(bid)} n={len(legs)}")

        enriched = enrich_t0_open(
            all_legs,
            ohlc_full=ohlc_hl,
            calendar=cal,
            date_index=date_index,
        )
        log(f"enriched legs={len(enriched)} / raw={len(all_legs)}")

        # Songshan-only and pooled.
        song = [x for x in enriched if x["branch_id"] == ANCHOR]
        pool = enriched

        # ---- Feature terciles ----
        feat_keys = [
            "t0_ret",
            "t0_gap",
            "t0_body",
            "t0_upper",
            "t0_lower",
            "t0_close_loc",
            "chase",
        ]
        terc_rows = []
        for scope, legs in (("songshan", song), ("pool_robust", pool)):
            for k in feat_keys:
                tdf = tercile_table(legs, k)
                if tdf.empty:
                    continue
                tdf.insert(0, "scope", scope)
                terc_rows.append(tdf)
            # binary green
            for label, pred in (
                ("green", lambda x: x["t0_green"] == 1),
                ("red", lambda x: x["t0_green"] == 0),
            ):
                sub = [x for x in legs if pred(x)]
                st = pack(sub)
                terc_rows.append(
                    pd.DataFrame(
                        [
                            {
                                "scope": scope,
                                "feature": "t0_color",
                                "bucket": label,
                                "lo": float("nan"),
                                "hi": float("nan"),
                                **st,
                            }
                        ]
                    )
                )
        terc = pd.concat(terc_rows, ignore_index=True) if terc_rows else pd.DataFrame()

        # ---- Filter funnel ----
        def apply_filter(legs: list[dict], name: str, pred) -> dict:
            kept = [x for x in legs if pred(x)]
            dropped = [x for x in legs if not pred(x)]
            sk, sd = pack(kept), pack(dropped)
            return {
                "filter": name,
                "n_keep": sk["n"],
                "med_keep": sk["med"],
                "win_keep": sk["win"],
                "n_drop": sd["n"],
                "med_drop": sd["med"],
                "win_drop": sd["win"],
                "delta_med": (
                    sk["med"] - pack(legs)["med"]
                    if sk["n"] and legs
                    else float("nan")
                ),
                "keep_rate": sk["n"] / len(legs) if legs else float("nan"),
                "n_oos_keep": sk["n_oos"],
                "med_oos_keep": sk["med_oos"],
            }

        filters = [
            ("baseline", lambda x: True),
            ("chase≤3%", lambda x: x["chase"] <= 0.03),
            ("chase≤5%", lambda x: x["chase"] <= 0.05),
            ("t0_ret≤5%", lambda x: x["t0_ret"] <= 0.05),
            ("t0_ret≤7%", lambda x: x["t0_ret"] <= 0.07),
            ("t0_ret≤9%", lambda x: x["t0_ret"] <= 0.09),
            ("t0_ret>-3%", lambda x: x["t0_ret"] > -0.03),
            ("t0_green", lambda x: x["t0_green"] == 1),
            ("t0_close_loc≥0.5", lambda x: x["t0_close_loc"] >= 0.5),
            ("t0_upper≤3%", lambda x: x["t0_upper"] <= 0.03),
            ("t0_upper≤5%", lambda x: x["t0_upper"] <= 0.05),
            (
                "combo: chase≤3% ∩ t0_ret≤7%",
                lambda x: x["chase"] <= 0.03 and x["t0_ret"] <= 0.07,
            ),
            (
                "combo: chase≤3% ∩ t0_green",
                lambda x: x["chase"] <= 0.03 and x["t0_green"] == 1,
            ),
            (
                "exclude_bad: ¬(t0_ret>7% ∪ chase>3%)",
                lambda x: not (x["t0_ret"] > 0.07 or x["chase"] > 0.03),
            ),
            (
                "exclude_exhaust: ¬(t0_ret>7% ∪ t0_upper>5% ∪ chase>5%)",
                lambda x: not (
                    x["t0_ret"] > 0.07 or x["t0_upper"] > 0.05 or x["chase"] > 0.05
                ),
            ),
        ]

        funnel_rows = []
        for scope, legs in (("songshan", song), ("pool_robust", pool)):
            base = pack(legs)
            for name, pred in filters:
                row = apply_filter(legs, name, pred)
                row["scope"] = scope
                row["base_med"] = base["med"]
                row["base_n"] = base["n"]
                funnel_rows.append(row)
        funnel = pd.DataFrame(funnel_rows)

        # Persist
        OUT.mkdir(parents=True, exist_ok=True)
        legs_df = pd.DataFrame(enriched)
        legs_path = OUT / "t0_open_legs.csv"
        terc_path = OUT / "t0_open_terciles.csv"
        funnel_path = OUT / "t0_open_funnel.csv"
        md_path = OUT / "T0_OPEN_FUNNEL.md"
        legs_df.to_csv(legs_path, index=False)
        terc.to_csv(terc_path, index=False)
        funnel.to_csv(funnel_path, index=False)

        # Markdown report
        lines = [
            "# Songshan-style · T0 圖形 × T+1 開盤漏斗",
            "",
            f"> Generated {datetime.now().isoformat(timespec='seconds')} · Research only",
            "",
            "## 結論（先講）",
            "",
            "- 先前 adaptive Top30 **沒有**排除 T0 圖形，也 **沒有**開盤／追價篩選（純 5d-net95 ∩ !mega → L1H7）。",
            "- 本漏斗在 **松山單席** 與 **穩健前段池** 上重算 T0 日K 形態 + T+1 open chase。",
            "",
        ]

        def add_scope_summary(scope: str, legs: list[dict]) -> None:
            st = pack(legs)
            lines.append(f"### {scope} · baseline")
            lines.append("")
            lines.append(
                f"- n={st['n']} · med r_adj={fmt(st['med'])}% · win={st['win']:.0%} · "
                f"oos_n={st['n_oos']} · oos_med={fmt(st['med_oos'])}%"
            )
            if legs:
                ch = np.array([x["chase"] for x in legs])
                tr = np.array([x["t0_ret"] for x in legs])
                lines.append(
                    f"- chase 中位={fmt(float(np.median(ch))*100)}% · "
                    f"chase>3% 占比={(ch>0.03).mean():.0%} · "
                    f"t0_ret 中位={fmt(float(np.median(tr))*100)}% · "
                    f"t0_ret>7% 占比={(tr>0.07).mean():.0%} · "
                    f"綠K占比={np.mean([x['t0_green'] for x in legs]):.0%}"
                )
            lines.append("")

        add_scope_summary("songshan 9217", song)
        add_scope_summary("pool_robust", pool)

        # Bad vs good contrast on pool
        lines += ["## 差筆共同特徵（pool · 依 r_adj 分位）", ""]
        if len(pool) >= 20:
            radj = np.array([x["radj_pct"] for x in pool])
            lo_cut, hi_cut = np.quantile(radj, [0.25, 0.75])
            bad = [x for x in pool if x["radj_pct"] <= lo_cut]
            good = [x for x in pool if x["radj_pct"] >= hi_cut]

            def avg(xs, k):
                return float(np.mean([x[k] for x in xs]))

            lines.append(
                f"Q1（差）n={len(bad)} r_adj≤{lo_cut:+.2f}% · "
                f"Q4（好）n={len(good)} r_adj≥{hi_cut:+.2f}%"
            )
            lines.append("")
            lines.append("| feature | 差筆(Q1) | 好筆(Q4) | Δ |")
            lines.append("|---|---:|---:|---:|")
            for k, scale in (
                ("t0_ret", 100),
                ("t0_gap", 100),
                ("t0_body", 100),
                ("t0_upper", 100),
                ("t0_close_loc", 1),
                ("chase", 100),
                ("t0_green", 1),
            ):
                a, b = avg(bad, k), avg(good, k)
                if scale == 100:
                    lines.append(
                        f"| {k} | {a*100:+.2f}% | {b*100:+.2f}% | {(a-b)*100:+.2f}pp |"
                    )
                else:
                    lines.append(f"| {k} | {a:.3f} | {b:.3f} | {a-b:+.3f} |")
            lines.append("")

        lines += ["## 三分位（feature → med r_adj）", ""]
        for scope in ("songshan", "pool_robust"):
            sub = terc[terc.scope == scope] if len(terc) else pd.DataFrame()
            if sub.empty:
                continue
            lines.append(f"### {scope}")
            lines.append("")
            lines.append("| feature | bucket | n | med% | win |")
            lines.append("|---|---|---:|---:|---:|")
            for r in sub.itertuples(index=False):
                lines.append(
                    f"| {r.feature} | {r.bucket} | {int(r.n)} | "
                    f"{fmt(r.med)} | {'' if pd.isna(r.win) else f'{r.win:.0%}'} |"
                )
            lines.append("")

        lines += ["## 漏斗：排除後 Δmed（相對 baseline）", ""]
        for scope in ("songshan", "pool_robust"):
            sub = funnel[funnel.scope == scope]
            lines.append(f"### {scope}")
            lines.append("")
            lines.append(
                "| filter | keep_n | keep% | med_keep | Δmed | drop_n | med_drop | oos_med_keep |"
            )
            lines.append("|---|---:|---:|---:|---:|---:|---:|---:|")
            for r in sub.itertuples(index=False):
                lines.append(
                    f"| {r.filter} | {int(r.n_keep)} | "
                    f"{'' if pd.isna(r.keep_rate) else f'{r.keep_rate:.0%}'} | "
                    f"{fmt(r.med_keep)} | {fmt(r.delta_med)} | "
                    f"{int(r.n_drop)} | {fmt(r.med_drop)} | {fmt(r.med_oos_keep)} |"
                )
            lines.append("")

        # Verdict bullets from numbers
        def pick(scope: str, name: str) -> dict | None:
            hit = funnel[(funnel.scope == scope) & (funnel["filter"] == name)]
            return None if hit.empty else hit.iloc[0].to_dict()

        lines += ["## 判決", ""]
        for scope in ("songshan", "pool_robust"):
            lines.append(f"**{scope}**")
            for name in (
                "chase≤3%",
                "t0_ret≤7%",
                "t0_green",
                "combo: chase≤3% ∩ t0_ret≤7%",
                "exclude_exhaust: ¬(t0_ret>7% ∪ t0_upper>5% ∪ chase>5%)",
            ):
                r = pick(scope, name)
                if not r:
                    continue
                drop = pick(scope, name)
                lines.append(
                    f"- `{name}`：keep {int(r['n_keep'])}/{int(r['base_n'])} "
                    f"({r['keep_rate']:.0%}) · med {fmt(r['med_keep'])}% "
                    f"(Δ {fmt(r['delta_med'])}pp) · "
                    f"dropped med {fmt(r['med_drop'])}%"
                )
            lines.append("")

        lines += [
            "## 檔案",
            "",
            f"- `{legs_path.relative_to(ROOT)}`",
            f"- `{terc_path.relative_to(ROOT)}`",
            f"- `{funnel_path.relative_to(ROOT)}`",
            "",
            f"elapsed {time.time()-t0:.1f}s",
            "",
        ]
        md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        log(f"wrote {md_path}")
        print("\n=== songshan funnel (key) ===")
        for name in (
            "baseline",
            "chase≤3%",
            "t0_ret≤7%",
            "combo: chase≤3% ∩ t0_ret≤7%",
            "exclude_exhaust: ¬(t0_ret>7% ∪ t0_upper>5% ∪ chase>5%)",
        ):
            r = pick("songshan", name)
            if r:
                print(
                    f"{name}: n={int(r['n_keep'])} med={fmt(r['med_keep'])} "
                    f"Δ={fmt(r['delta_med'])} drop_med={fmt(r['med_drop'])}"
                )
        print("\n=== pool_robust funnel (key) ===")
        for name in (
            "baseline",
            "chase≤3%",
            "t0_ret≤7%",
            "combo: chase≤3% ∩ t0_ret≤7%",
            "exclude_exhaust: ¬(t0_ret>7% ∪ t0_upper>5% ∪ chase>5%)",
        ):
            r = pick("pool_robust", name)
            if r:
                print(
                    f"{name}: n={int(r['n_keep'])} med={fmt(r['med_keep'])} "
                    f"Δ={fmt(r['delta_med'])} drop_med={fmt(r['med_drop'])}"
                )
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
