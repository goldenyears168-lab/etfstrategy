#!/usr/bin/env python3
"""AB58 × MEGA fair copytrade compare · L1H7 / L1H14 · β=1.15×IX0001.

Same event floors for all branches (NOT density-matched).
Protocol: reports/research/branch-footprint-screen/ab58_xMega_copytrade/PROTOCOL.md

  PYTHONPATH=src:scripts/research .venv/bin/python \\
    scripts/research/run_ab58_xmega_l1h_compare.py
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path
from statistics import mean, median

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "scripts" / "research")]

import run_abc_rolling_top10_l1h7_mvp as mvp  # noqa: E402

OUT = ROOT / "reports/research/branch-footprint-screen/ab58_xMega_copytrade"
UNIVERSE_JSON = OUT / "ab58_universe.json"
MEGA_JSON = OUT / "mega_blacklist_v1.json"
RANK_CSV = (
    ROOT / "reports/research/branch-footprint-screen/taiwan_branch_footprint_rank40d.csv"
)
PRE_PROFILES = OUT / "ab58_pre_profiles.csv"

START = "2024-07-01"
OOS_CUT = "2026-01-01"
COST = 0.003
BETA = 1.15
HOLDS = (7, 14)
EPISODE_CAL_DAYS = 5
THIN_OOS = 8

RULES: dict[str, tuple[float, float]] = {
    "R_song": (2e8, 0.25),
    "R_mid": (1e8, 0.20),
    "R_loose": (0.5e8, 0.15),
    "R_xin": (5e8, 0.40),
}

RETAIL_SEATS = {"營業分點"}  # PROTOCOL retail_branch_only


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def load_mega() -> set[str]:
    raw = json.loads(MEGA_JSON.read_text(encoding="utf-8"))
    return {str(x) for x in raw["symbols"]}


def load_branches() -> list[dict]:
    raw = json.loads(UNIVERSE_JSON.read_text(encoding="utf-8"))
    branches = list(raw["branches"])
    if len(branches) != 58:
        raise RuntimeError(f"expected 58 branches, got {len(branches)}")
    if PRE_PROFILES.exists():
        pref = pd.read_csv(PRE_PROFILES)
        pmap = {
            str(r.id): r
            for _, r in pref.iterrows()
        }
        for b in branches:
            pr = pmap.get(str(b["id"]))
            if pr is not None:
                b["research_priority"] = int(pr.research_priority) if pd.notna(pr.research_priority) else None
                b["taxonomy"] = str(pr.taxonomy) if pd.notna(pr.taxonomy) else ""
                b["trust"] = str(pr.trust) if pd.notna(pr.trust) else ""
    return branches


def pick_db(path: Path | None) -> Path:
    candidates = []
    if path is not None:
        candidates.append(path)
    candidates.extend(
        [
            ROOT / "data/stocks.db",
            ROOT / "data/scratch/rolling_mvp_snapshot.db",
        ]
    )
    for p in candidates:
        if p.exists():
            return p
    raise FileNotFoundError("no stocks.db / snapshot found")


def episode_dedupe(events: list[mvp.TopBuy]) -> list[mvp.TopBuy]:
    """Keep first (branch, stock) within 5 calendar days."""
    events = sorted(events, key=lambda e: (e.trade_date, e.stock_id))
    last: dict[str, date] = {}
    out: list[mvp.TopBuy] = []
    for e in events:
        d = date.fromisoformat(e.trade_date)
        prev = last.get(e.stock_id)
        # Keep first only when gap ≤ 5 calendar days (expert-funnel / protocol).
        if prev is not None and (d - prev).days <= EPISODE_CAL_DAYS:
            continue
        last[e.stock_id] = d
        out.append(e)
    return out


def outcome_hold(
    event: mvp.TopBuy,
    calendar: list[str],
    date_index: dict[str, int],
    ohlc: dict[tuple[str, str], tuple[float, float]],
    benchmark: dict[str, tuple[float, float]],
    hold: int,
) -> dict | None:
    si = date_index.get(event.trade_date)
    if si is None:
        return None
    ei, xi = si + 1, si + hold
    if xi >= len(calendar):
        return None
    ed, xd = calendar[ei], calendar[xi]
    er, xr = ohlc.get((event.stock_id, ed)), ohlc.get((event.stock_id, xd))
    be, bx = benchmark.get(ed), benchmark.get(xd)
    if not er or not xr or not be or not bx:
        return None
    stock_r = xr[1] / er[0] - 1.0 - COST
    ix_r = bx[1] / be[0] - 1.0
    radj = stock_r - BETA * ix_r
    bare = stock_r - ix_r
    return {
        "signal_date": event.trade_date,
        "entry_date": ed,
        "exit_date": xd,
        "stock_id": event.stock_id,
        "buy_amt": event.buy_amt,
        "buy_share": event.buy_share,
        "stock_pct": stock_r * 100.0,
        "ix_pct": ix_r * 100.0,
        "radj_pct": radj * 100.0,
        "bare_excess_pct": bare * 100.0,
    }


def summarize(legs: list[dict], oos_cut: str) -> dict:
    def pack(xs: list[dict], prefix: str) -> dict:
        if not xs:
            return {
                f"n_{prefix}": 0,
                f"med_radj_{prefix}": float("nan"),
                f"mean_radj_{prefix}": float("nan"),
                f"win_radj_{prefix}": float("nan"),
                f"med_stock_{prefix}": float("nan"),
            }
        radj = [x["radj_pct"] for x in xs]
        stock = [x["stock_pct"] for x in xs]
        return {
            f"n_{prefix}": len(xs),
            f"med_radj_{prefix}": float(median(radj)),
            f"mean_radj_{prefix}": float(mean(radj)),
            f"win_radj_{prefix}": float(sum(1 for v in radj if v > 0) / len(radj)),
            f"med_stock_{prefix}": float(median(stock)),
        }

    is_legs = [x for x in legs if x["signal_date"] < oos_cut]
    oos_legs = [x for x in legs if x["signal_date"] >= oos_cut]
    out = {}
    out.update(pack(legs, "all"))
    out.update(pack(is_legs, "is"))
    out.update(pack(oos_legs, "oos"))
    ctr = Counter(x["stock_id"] for x in legs)
    top3 = "|".join(f"{s}:{n}" for s, n in ctr.most_common(3))
    out["top3_stocks"] = top3
    out["uniq_stocks"] = len(ctr)
    out["flag_thin_oos"] = int(out["n_oos"] < THIN_OOS)
    return out


def render_main_md(df: pd.DataFrame, path: Path) -> None:
    lines = [
        "# AB58 · R_song × xMega · L1H7 / L1H14 leaderboard",
        "",
        f"> Generated {datetime.now().isoformat(timespec='seconds')} · Research only · β={BETA}×IX0001 · 30bps",
        "",
        "Primary sort: OOS median `r_adj`. Rows with `n_oos < 8` marked thin（僅觀察）.",
        "",
    ]
    for hold in HOLDS:
        sub = df[(df.rule == "R_song") & (df.mega_mode == "xMega") & (df.hold == hold)].copy()
        sub = sub.sort_values(["flag_thin_oos", "med_radj_oos"], ascending=[True, False])
        lines.append(f"## L1H{hold} · full58")
        lines.append("")
        lines.append(
            "| rank | id | name | seat | tier | n_oos | med_radj_oos% | win_oos | med_stock_oos% | thin | top3 |"
        )
        lines.append("|---:|---|---|---|---|---:|---:|---:|---:|:---:|---|")
        for i, r in enumerate(sub.itertuples(index=False), 1):
            thin = "Y" if r.flag_thin_oos else ""
            med = "" if pd.isna(r.med_radj_oos) else f"{r.med_radj_oos:+.2f}"
            win = "" if pd.isna(r.win_radj_oos) else f"{r.win_radj_oos:.0%}"
            mstk = "" if pd.isna(r.med_stock_oos) else f"{r.med_stock_oos:+.2f}"
            lines.append(
                f"| {i} | `{r.id}` | {r.name} | {r.seat_type} | {str(r.tier)[:1]} | "
                f"{int(r.n_oos)} | {med} | {win} | {mstk} | {thin} | {r.top3_stocks} |"
            )
        lines.append("")

        retail = sub[sub.seat_type.isin(RETAIL_SEATS)].copy()
        retail = retail.sort_values(["flag_thin_oos", "med_radj_oos"], ascending=[True, False])
        lines.append(f"## L1H{hold} · 營業分點-only (n={len(retail)})")
        lines.append("")
        lines.append(
            "| rank | id | name | tier | n_oos | med_radj_oos% | win_oos | med_stock_oos% | thin | top3 |"
        )
        lines.append("|---:|---|---|---|---:|---:|---:|---:|:---:|---|")
        for i, r in enumerate(retail.itertuples(index=False), 1):
            thin = "Y" if r.flag_thin_oos else ""
            med = "" if pd.isna(r.med_radj_oos) else f"{r.med_radj_oos:+.2f}"
            win = "" if pd.isna(r.win_radj_oos) else f"{r.win_radj_oos:.0%}"
            mstk = "" if pd.isna(r.med_stock_oos) else f"{r.med_stock_oos:+.2f}"
            lines.append(
                f"| {i} | `{r.id}` | {r.name} | {str(r.tier)[:1]} | "
                f"{int(r.n_oos)} | {med} | {win} | {mstk} | {thin} | {r.top3_stocks} |"
            )
        lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def render_ablation(df: pd.DataFrame, path: Path) -> None:
    lines = [
        "# MEGA ablation · R_song · L1H7",
        "",
        "Compare `keepMega` vs `xMega` OOS median r_adj (same floors).",
        "",
        "| id | name | seat | n_oos keep | med keep | n_oos xMega | med xMega | Δmed | note |",
        "|---|---|---|---:|---:|---:|---:|---:|---|",
    ]
    k = df[(df.rule == "R_song") & (df.hold == 7) & (df.mega_mode == "keepMega")].set_index("id")
    x = df[(df.rule == "R_song") & (df.hold == 7) & (df.mega_mode == "xMega")].set_index("id")
    rows = []
    for bid in k.index.intersection(x.index):
        rk, rx = k.loc[bid], x.loc[bid]
        mk = rk.med_radj_oos
        mx = rx.med_radj_oos
        delta = (mx - mk) if pd.notna(mk) and pd.notna(mx) else float("nan")
        note = ""
        if rk.n_oos >= THIN_OOS and rx.n_oos < THIN_OOS:
            note = "collapse→thin after xMega"
        elif pd.notna(delta) and delta <= -3:
            note = "α drops ≥3pp without MEGA"
        elif pd.notna(mk) and (not pd.notna(mx) or rx.n_oos == 0):
            note = "no xMega sample"
        rows.append((bid, rk, rx, delta, note))
    rows.sort(
        key=lambda t: (
            0 if "collapse" in t[4] else 1,
            -(abs(t[3]) if pd.notna(t[3]) else -1),
        )
    )
    for bid, rk, rx, delta, note in rows:
        mk = "" if pd.isna(rk.med_radj_oos) else f"{rk.med_radj_oos:+.2f}"
        mx = "" if pd.isna(rx.med_radj_oos) else f"{rx.med_radj_oos:+.2f}"
        dlt = "" if pd.isna(delta) else f"{delta:+.2f}"
        lines.append(
            f"| `{bid}` | {rk['name']} | {rk.seat_type} | {int(rk.n_oos)} | {mk} | "
            f"{int(rx.n_oos)} | {mx} | {dlt} | {note} |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_findings(df: pd.DataFrame, path: Path) -> None:
    song7 = df[(df.rule == "R_song") & (df.mega_mode == "xMega") & (df.hold == 7)]
    retail = song7[song7.seat_type.isin(RETAIL_SEATS) & (song7.flag_thin_oos == 0)].sort_values(
        "med_radj_oos", ascending=False
    )
    thin_n = int((song7.flag_thin_oos == 1).sum())
    lines = [
        "# FINDINGS_DRAFT · AB58 xMega L1H",
        "",
        f"- Generated: {datetime.now().isoformat(timespec='seconds')}",
        f"- Protocol: `PROTOCOL.md` · β={BETA} · cost 30bps · OOS≥{OOS_CUT}",
        f"- Leaderboard rows: {len(df)} (= 58 × {len(RULES)} rules × {len(HOLDS)} holds × 2 mega modes)",
        f"- R_song×xMega×L1H7 thin (n_oos<{THIN_OOS}): {thin_n}/58",
        "",
        "## Top 營業分點 · R_song × xMega · L1H7 (n_oos≥8)",
        "",
    ]
    if retail.empty:
        lines.append("_No retail branch with n_oos≥8 under R_song×xMega×L1H7._")
    else:
        for i, r in enumerate(retail.head(10).itertuples(index=False), 1):
            lines.append(
                f"{i}. **{r.name}** (`{r.id}`) med_oos={r.med_radj_oos:+.2f}% "
                f"n={int(r.n_oos)} win={r.win_radj_oos:.0%} top={r.top3_stocks}"
            )
    song14 = df[(df.rule == "R_song") & (df.mega_mode == "xMega") & (df.hold == 14)]
    retail14 = song14[
        song14.seat_type.isin(RETAIL_SEATS) & (song14.flag_thin_oos == 0)
    ].sort_values("med_radj_oos", ascending=False)
    lines += ["", "## Top 營業分點 · R_song × xMega · L1H14 (n_oos≥8)", ""]
    if retail14.empty:
        lines.append("_No retail branch with n_oos≥8 under R_song×xMega×L1H14._")
    else:
        for i, r in enumerate(retail14.head(10).itertuples(index=False), 1):
            lines.append(
                f"{i}. **{r.name}** (`{r.id}`) med_oos={r.med_radj_oos:+.2f}% "
                f"n={int(r.n_oos)} win={r.win_radj_oos:.0%} top={r.top3_stocks}"
            )

    # H7 vs H14 delta for overlapping retail
    lines += ["", "## H14 − H7 (營業分點, n_oos≥8 both)", ""]
    m7 = retail.set_index("id")["med_radj_oos"]
    m14 = retail14.set_index("id")["med_radj_oos"]
    common = m7.index.intersection(m14.index)
    deltas = (m14.loc[common] - m7.loc[common]).sort_values(ascending=False)
    for bid, d in deltas.head(5).items():
        lines.append(f"- `{bid}` {song7.set_index('id').loc[bid, 'name']}: Δ={d:+.2f}pp (偏波段)")
    for bid, d in deltas.tail(5).items():
        lines.append(f"- `{bid}` {song7.set_index('id').loc[bid, 'name']}: Δ={d:+.2f}pp (偏短打)")

    lines += [
        "",
        "## Caveats",
        "",
        "- 富邦新店 `9661` 是 C，不在 AB58；需另開對照組。",
        "- R_song 在去 MEGA 後樣本可能偏薄；看 R_mid / R_loose 敏感度。",
        "- 排名是單席事件中位，不是單槽複利。",
        "- 指紋強度（A/B）≠ 跟單 alpha。",
        "",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=None)
    ap.add_argument("--end", default=None)
    args = ap.parse_args()

    t0 = time.time()
    OUT.mkdir(parents=True, exist_ok=True)
    branches = load_branches()
    mega = load_mega()
    meta = {str(b["id"]): b for b in branches}
    ids = [str(b["id"]) for b in branches]
    log(f"universe n={len(ids)} mega={len(mega)}")

    db = pick_db(args.db)
    log(f"db={db}")
    conn = mvp.connect_readonly(db)
    try:
        end = args.end
        if not end:
            row = mvp.query_with_retry(
                conn,
                """
                SELECT MAX(trade_date) FROM stock_daily_bars
                WHERE stock_id='2330' AND source=? AND open>0 AND close>0
                """,
                (mvp.SOURCE,),
            )[0]
            end = str(row[0])
        log(f"window {START}..{end} oos_cut={OOS_CUT}")

        calendar = mvp.load_calendar(conn, START, end)
        # load_calendar requires long window for MVP constants; ok for our range
        date_index = {d: i for i, d in enumerate(calendar)}
        benchmark = mvp.load_benchmark(conn, calendar[0], calendar[-1])
        log(f"calendar={len(calendar)} bench={len(benchmark)}")
        log("loading OHLC (may take a while)...")
        ohlc = mvp.load_stock_ohlc(conn, calendar[0], calendar[-1])
        log(f"ohlc keys={len(ohlc)}")
        log("loading Top1 buys for 58 branches...")
        top_buys = mvp.load_top_buys(conn, ids, START, end)
        log(f"branches with tape={len(top_buys)}")
    finally:
        conn.close()

    # event sample stats
    sample_rows = []
    for bid in ids:
        evs = list(top_buys.get(bid, {}).values())
        row = {
            "id": bid,
            "name": meta[bid]["name"],
            "seat_type": meta[bid]["seat_type"],
            "n_top1_days": len(evs),
        }
        for mode in ("keepMega", "xMega"):
            base = [
                e
                for e in evs
                if not (mode == "xMega" and e.stock_id in mega)
            ]
            for rule, (abs_f, sh_f) in RULES.items():
                n = sum(1 for e in base if e.buy_amt >= abs_f and e.buy_share >= sh_f)
                row[f"n_{rule}_{mode}"] = n
        sample_rows.append(row)
    sample_df = pd.DataFrame(sample_rows)
    sample_df.to_csv(OUT / "events_sample_stats.csv", index=False)
    log("wrote events_sample_stats.csv")

    # main loop
    board_rows = []
    legs_dir = OUT / "legs"
    legs_dir.mkdir(exist_ok=True)
    top_legs_cache: dict[str, list[dict]] = {}

    for bid in ids:
        bmeta = meta[bid]
        raw_events = list(top_buys.get(bid, {}).values())
        for mega_mode in ("keepMega", "xMega"):
            filtered = [
                e
                for e in raw_events
                if not (mega_mode == "xMega" and e.stock_id in mega)
            ]
            for rule, (abs_f, sh_f) in RULES.items():
                passed = [
                    e
                    for e in filtered
                    if e.buy_amt >= abs_f and e.buy_share >= sh_f
                ]
                passed = episode_dedupe(passed)
                for hold in HOLDS:
                    legs = []
                    for e in passed:
                        oc = outcome_hold(
                            e, calendar, date_index, ohlc, benchmark, hold
                        )
                        if oc is not None:
                            legs.append(oc)
                    stats = summarize(legs, OOS_CUT)
                    row = {
                        "id": bid,
                        "name": bmeta["name"],
                        "tier": bmeta["tier"],
                        "seat_type": bmeta["seat_type"],
                        "region": bmeta.get("region", ""),
                        "rule": rule,
                        "hold": hold,
                        "mega_mode": mega_mode,
                        **stats,
                    }
                    board_rows.append(row)
                    if (
                        rule == "R_song"
                        and mega_mode == "xMega"
                        and hold == 7
                        and bmeta["seat_type"] in RETAIL_SEATS
                    ):
                        top_legs_cache[bid] = legs

    board = pd.DataFrame(board_rows)
    board.to_csv(OUT / "leaderboard_all.csv", index=False)
    log(f"wrote leaderboard_all.csv rows={len(board)}")

    render_main_md(board, OUT / "leaderboard_main_R_song_xMega.md")
    render_ablation(board, OUT / "mega_ablation_R_song.md")
    write_findings(board, OUT / "FINDINGS_DRAFT.md")
    log("wrote markdown artifacts")

    # save legs for top10 retail R_song xMega L1H7 with n_oos>=8
    song7 = board[
        (board.rule == "R_song")
        & (board.mega_mode == "xMega")
        & (board.hold == 7)
        & (board.seat_type.isin(RETAIL_SEATS))
        & (board.flag_thin_oos == 0)
    ].sort_values("med_radj_oos", ascending=False)
    for _, r in song7.head(10).iterrows():
        legs = top_legs_cache.get(str(r.id), [])
        if legs:
            pd.DataFrame(legs).to_csv(legs_dir / f"legs_{r.id}_R_song_xMega_H7.csv", index=False)

    # console summary
    log("=== Top 營業分點 R_song xMega L1H7 (n_oos≥8) ===")
    for _, r in song7.head(10).iterrows():
        log(
            f"  {r['name']:14} med_oos={r.med_radj_oos:+.2f}% n={int(r.n_oos)} "
            f"win={r.win_radj_oos:.0%} {r.top3_stocks}"
        )
    song14 = board[
        (board.rule == "R_song")
        & (board.mega_mode == "xMega")
        & (board.hold == 14)
        & (board.seat_type.isin(RETAIL_SEATS))
        & (board.flag_thin_oos == 0)
    ].sort_values("med_radj_oos", ascending=False)
    log("=== Top 營業分點 R_song xMega L1H14 (n_oos≥8) ===")
    for _, r in song14.head(10).iterrows():
        log(
            f"  {r['name']:14} med_oos={r.med_radj_oos:+.2f}% n={int(r.n_oos)} "
            f"win={r.win_radj_oos:.0%} {r.top3_stocks}"
        )
    log(f"done in {time.time()-t0:.1f}s → {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
