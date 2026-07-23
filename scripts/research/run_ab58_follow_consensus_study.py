#!/usr/bin/env python3
"""AB58 follow / side-box / consensus study (research layer only).

Same PROTOCOL as ab58_xMega_copytrade:
  R_song / R_mid / R_1p5 · xMega · L1H7/L1H14 · β=1.15 · 30bps · OOS≥2026-01-01

  PYTHONPATH=src:scripts/research .venv/bin/python \\
    scripts/research/run_ab58_follow_consensus_study.py
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
import run_ab58_xmega_l1h_compare as ab58  # noqa: E402

OUT = ROOT / "reports/research/branch-footprint-screen/ab58_xMega_copytrade"
LEGS = OUT / "legs"

START = ab58.START
OOS_CUT = ab58.OOS_CUT
COST = ab58.COST
BETA = ab58.BETA
EPISODE_CAL_DAYS = ab58.EPISODE_CAL_DAYS
THIN_OOS = ab58.THIN_OOS

# Side-box external control (C seat, not in AB58)
XINDIAN_ID = "9661"
XINDIAN_NAME = "富邦-新店"

# Champion seats for how-to-follow
CHAMPIONS = [
    ("9217", "凱基-松山"),
    ("700b", "兆豐-中壢"),
    ("9801", "元大-松江"),
    ("9227", "凱基-城中"),
    (XINDIAN_ID, XINDIAN_NAME),
]

# Consensus candidate pool (positive/stable 營業分點; no 虎尾/自營/外資)
POOL_CORE = ["9217", "700b", "9801", "918X"]
POOL_PLUS = ["9217", "700b", "9801", "918X", "9227"]

RULES: dict[str, tuple[float, float]] = {
    "R_song": (2e8, 0.25),
    "R_mid": (1e8, 0.20),
    "R_1p5": (1.5e8, 0.25),  # overnight continuity sensitivity
}

HOLDS = (7, 14)
NAME_MAP = {
    "9217": "凱基-松山",
    "700b": "兆豐-中壢",
    "9801": "元大-松江",
    "918X": "群益金鼎-台北",
    "9227": "凱基-城中",
    XINDIAN_ID: XINDIAN_NAME,
}


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def episode_dedupe_stock(events: list[mvp.TopBuy]) -> list[mvp.TopBuy]:
    """Global stock dedupe: keep first event per stock within 5 calendar days."""
    events = sorted(events, key=lambda e: (e.trade_date, -e.buy_amt, e.branch_id))
    last: dict[str, date] = {}
    out: list[mvp.TopBuy] = []
    for e in events:
        d = date.fromisoformat(e.trade_date)
        prev = last.get(e.stock_id)
        if prev is not None and (d - prev).days <= EPISODE_CAL_DAYS:
            continue
        last[e.stock_id] = d
        out.append(e)
    return out


def filter_events(
    raw: list[mvp.TopBuy],
    mega: set[str],
    abs_f: float,
    sh_f: float,
    *,
    xmega: bool = True,
) -> list[mvp.TopBuy]:
    out = []
    for e in raw:
        if xmega and e.stock_id in mega:
            continue
        if e.buy_amt >= abs_f and e.buy_share >= sh_f:
            out.append(e)
    return out


def run_seat(
    raw: list[mvp.TopBuy],
    mega: set[str],
    rule: str,
    hold: int,
    calendar: list[str],
    date_index: dict[str, int],
    ohlc: dict,
    benchmark: dict,
) -> tuple[list[dict], dict]:
    abs_f, sh_f = RULES[rule]
    passed = filter_events(raw, mega, abs_f, sh_f)
    passed = ab58.episode_dedupe(passed)  # per-branch stock dedupe
    legs = []
    for e in passed:
        oc = ab58.outcome_hold(e, calendar, date_index, ohlc, benchmark, hold)
        if oc is not None:
            legs.append(oc)
    return legs, ab58.summarize(legs, OOS_CUT)


def enrich_chase(
    legs: list[dict],
    ohlc: dict[tuple[str, str], tuple[float, float]],
) -> list[dict]:
    """Add chase = T+1 open / signal close − 1 (research soft-entry note)."""
    out = []
    for leg in legs:
        sig = leg["signal_date"]
        sid = leg["stock_id"]
        entry = leg["entry_date"]
        sig_bar = ohlc.get((sid, sig))
        ent_bar = ohlc.get((sid, entry))
        chase = None
        if sig_bar and ent_bar and sig_bar[1] > 0:
            chase = ent_bar[0] / sig_bar[1] - 1.0
        row = dict(leg)
        row["chase_pct"] = None if chase is None else chase * 100.0
        row["chase_ok_3pct"] = None if chase is None else int(chase <= 0.03)
        out.append(row)
    return out


def seat_deep_dive(
    bid: str,
    name: str,
    legs7: list[dict],
    legs14: list[dict],
    ohlc: dict,
) -> dict:
    e7 = enrich_chase(legs7, ohlc)
    e14 = enrich_chase(legs14, ohlc)
    s7 = ab58.summarize(legs7, OOS_CUT)
    s14 = ab58.summarize(legs14, OOS_CUT)

    oos7 = [x for x in e7 if x["signal_date"] >= OOS_CUT]
    oos14 = [x for x in e14 if x["signal_date"] >= OOS_CUT]
    wins = [x for x in oos7 if x["radj_pct"] > 0]
    losses = [x for x in oos7 if x["radj_pct"] <= 0]
    chases = [x["chase_pct"] for x in oos7 if x.get("chase_pct") is not None]
    chase_ok = [x for x in oos7 if x.get("chase_ok_3pct") == 1]
    chase_bad = [x for x in oos7 if x.get("chase_ok_3pct") == 0]

    def med_or_nan(xs: list[float]) -> float:
        return float(median(xs)) if xs else float("nan")

    # Prefer hold: H7 if med_oos higher or H14 thin/worse
    prefer = "H7"
    if s14["n_oos"] >= THIN_OOS and s7["n_oos"] >= THIN_OOS:
        if s14["med_radj_oos"] > s7["med_radj_oos"] + 0.5:
            prefer = "H14"
        elif s7["med_radj_oos"] > s14["med_radj_oos"] + 0.5:
            prefer = "H7"
        else:
            prefer = "H7≈H14"
    elif s14["n_oos"] >= THIN_OOS and s7["n_oos"] < THIN_OOS:
        prefer = "H14"
    elif s7["n_oos"] >= THIN_OOS:
        prefer = "H7"

    ctr = Counter(x["stock_id"] for x in oos7)
    return {
        "id": bid,
        "name": name,
        "n_oos_h7": s7["n_oos"],
        "med_oos_h7": s7["med_radj_oos"],
        "mean_oos_h7": s7["mean_radj_oos"],
        "win_oos_h7": s7["win_radj_oos"],
        "n_oos_h14": s14["n_oos"],
        "med_oos_h14": s14["med_radj_oos"],
        "win_oos_h14": s14["win_radj_oos"],
        "prefer_hold": prefer,
        "top3_oos": "|".join(f"{s}:{n}" for s, n in ctr.most_common(3)),
        "uniq_oos": len(ctr),
        "win_n": len(wins),
        "loss_n": len(losses),
        "win_med": med_or_nan([x["radj_pct"] for x in wins]),
        "loss_med": med_or_nan([x["radj_pct"] for x in losses]),
        "chase_med_oos": med_or_nan(chases),
        "chase_ok3_n": len(chase_ok),
        "chase_ok3_med": med_or_nan([x["radj_pct"] for x in chase_ok]),
        "chase_gt3_n": len(chase_bad),
        "chase_gt3_med": med_or_nan([x["radj_pct"] for x in chase_bad]),
        "legs7_enriched": e7,
        "legs14_enriched": e14,
    }


def build_branch_events(
    top_buys: dict,
    branch_ids: list[str],
    mega: set[str],
    abs_f: float,
    sh_f: float,
) -> dict[str, list[mvp.TopBuy]]:
    """Per-branch filtered+deduped events."""
    out: dict[str, list[mvp.TopBuy]] = {}
    for bid in branch_ids:
        raw = list(top_buys.get(bid, {}).values())
        passed = filter_events(raw, mega, abs_f, sh_f)
        out[bid] = ab58.episode_dedupe(passed)
    return out


def consensus_designs(
    by_branch: dict[str, list[mvp.TopBuy]],
    pool: list[str],
    calendar: list[str],
    date_index: dict[str, int],
    ohlc: dict,
    benchmark: dict,
    hold: int,
) -> list[tuple[str, list[dict], dict]]:
    """Return list of (design_id, legs, stats).

    Dedupe choice (documented):
      - solo: per-branch (branch, stock) 5d
      - OR_union: per-branch first, then union, then **global stock** 5d dedupe
        (keeps highest buy_amt on collision via sort)
      - same_day_ge2 / lookback1d_ge2: consensus events then global stock 5d
      - intensity: among same-day consensus multi-stock, pick max buy_amt
    """
    # Index: date -> stock -> list of (branch, event)
    day_stock: dict[str, dict[str, list[mvp.TopBuy]]] = defaultdict(lambda: defaultdict(list))
    for bid in pool:
        for e in by_branch.get(bid, []):
            day_stock[e.trade_date][e.stock_id].append(e)

    cal_set = set(calendar)
    cal_list = [d for d in calendar if d >= START]

    def outcome_list(
        events: list[mvp.TopBuy],
        n_br: dict[tuple[str, str], int] | None = None,
    ) -> list[dict]:
        legs = []
        for e in events:
            oc = ab58.outcome_hold(e, calendar, date_index, ohlc, benchmark, hold)
            if oc is not None:
                row = dict(oc)
                row["branch_id"] = e.branch_id
                row["n_branches"] = (n_br or {}).get((e.trade_date, e.stock_id), 1)
                legs.append(row)
        return legs

    results: list[tuple[str, list[dict], dict]] = []

    # 1) Solo best = 松山 alone
    solo_id = "9217"
    solo_ev = list(by_branch.get(solo_id, []))
    solo_legs = outcome_list(solo_ev)
    results.append(("solo_songshan", solo_legs, ab58.summarize(solo_legs, OOS_CUT)))

    # 2) OR any of pool
    or_raw: list[mvp.TopBuy] = []
    for bid in pool:
        or_raw.extend(by_branch.get(bid, []))
    or_ev = episode_dedupe_stock(or_raw)
    or_legs = outcome_list(or_ev)
    results.append(("or_union", or_legs, ab58.summarize(or_legs, OOS_CUT)))

    # 3) Same-day consensus >=2 (TopBuy frozen — n_branches in side map)
    same_day: list[mvp.TopBuy] = []
    same_n: dict[tuple[str, str], int] = {}
    for d, stocks in day_stock.items():
        for sid, evs in stocks.items():
            branches = {e.branch_id for e in evs}
            if len(branches) >= 2:
                best = max(evs, key=lambda e: e.buy_amt)
                same_day.append(best)
                same_n[(best.trade_date, best.stock_id)] = len(branches)
    same_day = episode_dedupe_stock(same_day)
    sd_legs = outcome_list(same_day, same_n)
    results.append(("same_day_ge2", sd_legs, ab58.summarize(sd_legs, OOS_CUT)))

    # 4) Lookback-1d consensus >=2 on {T-1, T}, require >=1 on T
    lookback: list[mvp.TopBuy] = []
    lb_n: dict[tuple[str, str], int] = {}
    presence: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    amt_map: dict[tuple[str, str, str], mvp.TopBuy] = {}
    for bid in pool:
        for e in by_branch.get(bid, []):
            presence[e.trade_date][e.stock_id].add(bid)
            amt_map[(bid, e.trade_date, e.stock_id)] = e

    for i, d in enumerate(cal_list):
        if d not in cal_set:
            continue
        yday = cal_list[i - 1] if i > 0 else None
        today_stocks = set(presence.get(d, {}).keys())
        for sid in today_stocks:
            today_br = presence[d][sid]
            yday_br = presence.get(yday, {}).get(sid, set()) if yday else set()
            union = today_br | yday_br
            if len(union) >= 2 and len(today_br) >= 1:
                cands = [amt_map[(b, d, sid)] for b in today_br if (b, d, sid) in amt_map]
                if not cands:
                    continue
                best = max(cands, key=lambda e: e.buy_amt)
                lookback.append(best)
                lb_n[(best.trade_date, best.stock_id)] = len(union)
    lookback = episode_dedupe_stock(lookback)
    lb_legs = outcome_list(lookback, lb_n)
    results.append(("lookback1d_ge2", lb_legs, ab58.summarize(lb_legs, OOS_CUT)))

    # 5) Intensity: among same-day consensus multi-stock days, pick max buy_amt
    by_day: dict[str, list[mvp.TopBuy]] = defaultdict(list)
    for e in same_day:
        by_day[e.trade_date].append(e)
    intensity: list[mvp.TopBuy] = []
    for _d, evs in sorted(by_day.items()):
        intensity.append(max(evs, key=lambda e: e.buy_amt))
    intensity = episode_dedupe_stock(intensity)
    int_legs = outcome_list(intensity, same_n)
    results.append(("intensity_same_day", int_legs, ab58.summarize(int_legs, OOS_CUT)))

    return results


def compound_slot(legs: list[dict], oos_only: bool = True) -> float | None:
    """Optional single-slot compound on OOS (appendix). Non-overlapping by exit."""
    xs = [x for x in legs if (not oos_only or x["signal_date"] >= OOS_CUT)]
    if not xs:
        return None
    xs = sorted(xs, key=lambda x: x["signal_date"])
    nav = 1.0
    last_exit = None
    n = 0
    for x in xs:
        if last_exit is not None and x["entry_date"] <= last_exit:
            continue
        nav *= 1.0 + x["radj_pct"] / 100.0
        last_exit = x["exit_date"]
        n += 1
    return None if n == 0 else (nav - 1.0) * 100.0


def fmt_pct(v: float | None, digits: int = 2) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    return f"{v:+.{digits}f}"


def write_xindian_sidebox(
    rows: list[dict],
    songshan: dict,
    path: Path,
) -> None:
    lines = [
        "# 9661 富邦新店 · Side-box（同 PROTOCOL 外控）",
        "",
        f"> Generated {datetime.now().isoformat(timespec='seconds')} · Research only · 非 AB58 席（tier C）",
        "",
        "尺規與主賽相同：β=1.15 · 30bps · xMega · OOS≥2026-01-01 · episode 5d。",
        "",
        "## 結果表",
        "",
        "| seat | rule | hold | n_is | n_oos | med_oos% | mean_oos% | win_oos | thin | top3 |",
        "|---|---|---:|---:|---:|---:|---:|---:|:---:|---|",
    ]
    for r in rows:
        thin = "Y" if r["flag_thin_oos"] else ""
        lines.append(
            f"| {r['name']} (`{r['id']}`) | {r['rule']} | H{r['hold']} | "
            f"{r['n_is']} | {r['n_oos']} | {fmt_pct(r['med_radj_oos'])} | "
            f"{fmt_pct(r['mean_radj_oos'])} | "
            f"{('—' if pd.isna(r['win_radj_oos']) else f'{r['win_radj_oos']:.0%}')} | "
            f"{thin} | {r['top3_stocks']} |"
        )
    lines += [
        "",
        "## vs 松山（主賽錨）",
        "",
        "| Compare | 松山 R_song H7 | 新店 R_song H7 | Δmed |",
        "|---|---:|---:|---:|",
    ]
    xin_song_h7 = next(
        (r for r in rows if r["id"] == XINDIAN_ID and r["rule"] == "R_song" and r["hold"] == 7),
        None,
    )
    if xin_song_h7 and songshan:
        dlt = xin_song_h7["med_radj_oos"] - songshan["med_radj_oos"]
        lines.append(
            f"| OOS med r_adj | {fmt_pct(songshan['med_radj_oos'])}% "
            f"(n={songshan['n_oos']}) | {fmt_pct(xin_song_h7['med_radj_oos'])}% "
            f"(n={xin_song_h7['n_oos']}) | {fmt_pct(dlt)}pp |"
        )
    lines += [
        "",
        "## R_1p5 連續敏感度（overnight 1.5億∩25%）",
        "",
        "比較 R_song(2e8) vs R_1p5(1.5e8) 對 9217 / 9661 的 n 與 OOS med。",
        "",
    ]
    for bid, label in [("9217", "松山"), (XINDIAN_ID, "新店")]:
        for hold in (7, 14):
            a = next((r for r in rows if r["id"] == bid and r["rule"] == "R_song" and r["hold"] == hold), None)
            b = next((r for r in rows if r["id"] == bid and r["rule"] == "R_1p5" and r["hold"] == hold), None)
            if a and b:
                lines.append(
                    f"- **{label} H{hold}**: R_song med={fmt_pct(a['med_radj_oos'])}% n={a['n_oos']} → "
                    f"R_1p5 med={fmt_pct(b['med_radj_oos'])}% n={b['n_oos']}"
                )
    lines += [
        "",
        "## 解讀（研究層）",
        "",
        "- 新店是 **C 外控**，不能用 AB58 主榜宣稱「贏過／輸給新店」。",
        "- 若新店同尺下明顯弱於松山 → 支持「跟 AB58 頭部營業分點，而非經典敘事錨」。",
        "- 若新店接近／優於松山 → 主賽排除 C 席有選樣偏誤，跟單名單應另開 C 對照。",
        "",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_consensus_md(board: pd.DataFrame, path: Path) -> None:
    lines = [
        "# Consensus compare · AB58 follow pool",
        "",
        f"> Generated {datetime.now().isoformat(timespec='seconds')} · Research only",
        "",
        "**Pool core:** 9217 松山 · 700b 中壢 · 9801 松江 · 918X 台北",
        "**Pool+:** core + 9227 城中（H14-oriented）",
        "",
        "Base cell: **R_song × xMega × β=1.15 × 30bps**；R_mid = sensitivity。",
        "",
        "### Dedupe choice",
        "",
        "- Solo: per-branch `(stock)` 5 calendar-day first-only（同 PROTOCOL）。",
        "- OR / consensus: after collecting signals, **global stock** 5d first-only "
        "（同日多席取最高 `buy_amt`）。",
        "",
        "## Leaderboard (primary = OOS med r_adj；n_oos&lt;8 → thin)",
        "",
        "| design | pool | rule | hold | n_is | n_oos | med_oos% | mean_oos% | win_oos | compound_oos% | thin |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|:---:|",
    ]
    view = board.sort_values(
        ["rule", "hold", "pool", "flag_thin_oos", "med_radj_oos"],
        ascending=[True, True, True, True, False],
    )
    for r in view.itertuples(index=False):
        thin = "Y" if r.flag_thin_oos else ""
        win = "—" if pd.isna(r.win_radj_oos) else f"{r.win_radj_oos:.0%}"
        lines.append(
            f"| `{r.design}` | {r.pool} | {r.rule} | H{r.hold} | "
            f"{int(r.n_is)} | {int(r.n_oos)} | {fmt_pct(r.med_radj_oos)} | "
            f"{fmt_pct(r.mean_radj_oos)} | {win} | {fmt_pct(r.compound_oos)} | {thin} |"
        )

    # Headline pick under R_song H7 core
    sub = board[(board.rule == "R_song") & (board.hold == 7) & (board.pool == "core")]
    sub = sub[sub.flag_thin_oos == 0].sort_values("med_radj_oos", ascending=False)
    lines += ["", "## Headline · R_song × H7 × pool=core (non-thin)", ""]
    if sub.empty:
        lines.append("_No non-thin design._")
    else:
        for i, r in enumerate(sub.itertuples(index=False), 1):
            lines.append(
                f"{i}. **{r.design}** med_oos={r.med_radj_oos:+.2f}% "
                f"n={int(r.n_oos)} win={r.win_radj_oos:.0%} "
                f"compound={fmt_pct(r.compound_oos)}%"
            )
    lines += [
        "",
        "## Reading guide",
        "",
        "- 若 **solo_songshan** 仍是 med 冠軍且 n 夠 → 共識 **不需要**（可選加分僅當 OR 不傷 med）。",
        "- 若 **lookback1d_ge2** / **same_day_ge2** 明顯抬高 med 且 n_oos≥8 → 共識 **可選加分** 或 **必須**。",
        "- **or_union** 若 med 崩塌 → 多席聯集稀釋，勿當跟單預設。",
        "",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_follow_protocol(
    deep: list[dict],
    consensus_df: pd.DataFrame,
    xin_rows: list[dict],
    song_ablation: dict | None,
    path: Path,
) -> None:
    # Best consensus under R_song H7 core
    sub = consensus_df[
        (consensus_df.rule == "R_song")
        & (consensus_df.hold == 7)
        & (consensus_df.pool == "core")
    ].copy()
    solo = sub[sub.design == "solo_songshan"]
    solo_med = float(solo.iloc[0].med_radj_oos) if len(solo) else float("nan")
    non_thin = sub[sub.flag_thin_oos == 0].sort_values("med_radj_oos", ascending=False)
    best = non_thin.iloc[0] if len(non_thin) else None

    cons_verdict = "不需要"
    cons_mode = "—"
    if best is not None:
        cons_mode = str(best.design)
        if best.design == "solo_songshan":
            cons_verdict = "不需要"
        elif best.med_radj_oos >= solo_med + 1.0 and best.n_oos >= THIN_OOS:
            cons_verdict = "可選加分"
            # if consensus beats solo by a lot AND OR is worse → soft must
            or_row = sub[sub.design == "or_union"]
            if len(or_row) and or_row.iloc[0].med_radj_oos < solo_med - 1:
                cons_verdict = "可選加分"  # filter not union
        elif best.design != "solo_songshan" and best.med_radj_oos > solo_med:
            cons_verdict = "可選加分"
        else:
            cons_verdict = "不需要"

    # Override logic with clearer evidence rules (filled after we see numbers in caller)
    # This function receives already-computed verdict hints via consensus_df columns if present.
    if "verdict_hint" in consensus_df.columns:
        hints = consensus_df["verdict_hint"].dropna()
        if len(hints):
            cons_verdict = str(hints.iloc[0])

    song = next((d for d in deep if d["id"] == "9217"), None)
    zhongli = next((d for d in deep if d["id"] == "700b"), None)
    songjiang = next((d for d in deep if d["id"] == "9801"), None)
    chengzhong = next((d for d in deep if d["id"] == "9227"), None)
    xin = next(
        (r for r in xin_rows if r["id"] == XINDIAN_ID and r["rule"] == "R_song" and r["hold"] == 7),
        None,
    )

    lines = [
        "# FOLLOW_PROTOCOL_DRAFT · AB58 × MEGA Copytrade",
        "",
        f"> Frozen research recommendation · {datetime.now().date().isoformat()}  ",
        "> **Research layer only** — 非採納策略；不得寫入 `config/strategy.yaml` / Order。",
        "",
        "尺規：`PROTOCOL.md` · β=1.15 · 30bps · MEGA v1 exclude · OOS≥2026-01-01。",
        "",
        "---",
        "",
        "## 1. 跟誰（Primary follow list · 最多 1–3 席）",
        "",
        "| Priority | Seat | Default hold | OOS med (R_song×xMega) | Why |",
        "|:---:|---|---|---:|---|",
    ]
    if song:
        lines.append(
            f"| **1** | **9217 凱基-松山** | **{song['prefer_hold']}** | "
            f"H7 {fmt_pct(song['med_oos_h7'])}% (n={song['n_oos_h7']}) · "
            f"H14 {fmt_pct(song['med_oos_h14'])}% | 主賽 H7 冠軍；MEGA 剔除後翻正 |"
        )
    if zhongli:
        lines.append(
            f"| **2** | **700b 兆豐-中壢** | **{zhongli['prefer_hold']}** | "
            f"H7 {fmt_pct(zhongli['med_oos_h7'])}% (n={zhongli['n_oos_h7']}) | "
            f"唯二穩定正樣本；勝率偏高 |"
        )
    if songjiang:
        lines.append(
            f"| 3（觀察） | 9801 元大-松江 | {songjiang['prefer_hold']} | "
            f"H7 {fmt_pct(songjiang['med_oos_h7'])}% (n={songjiang['n_oos_h7']}) | "
            f"樣本密但 edge 弱於松山/中壢；適合共識池而非 solo 主跟 |"
        )
    if chengzhong:
        lines.append(
            f"| H14 專席 | 9227 凱基-城中 | **H14** | "
            f"H7 {fmt_pct(chengzhong['med_oos_h7'])}% · "
            f"H14 {fmt_pct(chengzhong['med_oos_h14'])}% "
            f"(n={chengzhong['n_oos_h14']}) | 僅在放長持有時進入正區；勿用 H7 跟 |"
        )

    lines += [
        "",
        "### Anti-picks（明確不跟）",
        "",
        "| Seat | Reason |",
        "|---|---|",
        "| 虎尾 trio（9A9b / 9236 / 700F） | 強指紋弱 alpha；×MEGA 後仍負或 thin |",
        "| 權值通道席（9186 新竹、5383 高雄等） | ×MEGA 後 n 崩塌，無法評分 |",
        "| 活躍負樣本（9875 土城永寧、9216 信義） | n 很大但 OOS med 負 — 忙碌≠edge |",
        "| 自營 / 外資（980T/960T/8890…） | 非營業分點跟單對象；機構行為污染 |",
        "",
    ]
    if xin:
        thin = "（thin）" if xin["flag_thin_oos"] else ""
        lines.append(
            f"- **9661 富邦新店（C 外控）**: R_song×H7 med={fmt_pct(xin['med_radj_oos'])}% "
            f"n={xin['n_oos']}{thin} — 見 `xindian_sidebox.md`；**不進 Primary list**"
            "（經典敘事錨 ≠ 本協議優勝席）。"
        )

    lines += [
        "",
        "---",
        "",
        "## 2. 怎麼跟（Default rule string）",
        "",
        "```",
        "R_song ∩ xMega ∩ L1H7",
        "  abs ≥ 2e8 ∩ buy_share ≥ 0.25",
        "  exclude mega_blacklist_v1",
        "  entry = T+1 open · hold 7 trading days · exit close",
        "  cost 30bps RT · r_adj = r − 1.15 × r_IX0001",
        "  episode dedupe: (branch, stock) 5 calendar days · keep first",
        "```",
        "",
        "| Knob | Frozen | Note |",
        "|---|---|---|",
        "| Event | daily Top1 `buy×close` | 分點買超金額第一 |",
        "| MEGA | **mandatory exclude** | 松山 keepMega −3.44% → xMega +5.78%（Δ+9.2pp） |",
        "| Cost | 30 bps | 已扣 |",
        "| Hold default | **H7**（松山/中壢） | 城中改 **H14** |",
        "| Soft entry（研究備註） | 追價≤3%（訊號收→T+1開）；限價可設收×1.01 | "
        "**非** Order 自動化；見 legs chase 欄 |",
        "",
        "### Per-seat hold",
        "",
        "| Seat | Prefer | Evidence |",
        "|---|---|---|",
    ]
    for d in deep:
        if d["id"] == XINDIAN_ID:
            continue
        lines.append(
            f"| {d['name']} (`{d['id']}`) | **{d['prefer_hold']}** | "
            f"H7 med={fmt_pct(d['med_oos_h7'])}% n={d['n_oos_h7']} · "
            f"H14 med={fmt_pct(d['med_oos_h14'])}% n={d['n_oos_h14']} |"
        )

    lines += [
        "",
        "### Soft entry notes（對齊 expert-pool 研究，非下單層）",
        "",
    ]
    for d in deep:
        if d["id"] in ("9217", "700b", "9801", "9227"):
            lines.append(
                f"- **{d['name']}**: OOS chase 中位 {fmt_pct(d['chase_med_oos'])}%；"
                f"追價≤3% 子集 n={d['chase_ok3_n']} med={fmt_pct(d['chase_ok3_med'])}%；"
                f"追價>3% n={d['chase_gt3_n']} med={fmt_pct(d['chase_gt3_med'])}%"
            )
    lines += [
        "",
        "> 操作備註：若隔日開盤相對訊號收追價 >3%，研究傾向 **跳過或減碼**；"
        "限價試算可用訊號收 ×1.01（與 expert-pool watch 一致）。**不**寫入 order.yaml。",
        "",
        "---",
        "",
        "## 3. 需要共識嗎？",
        "",
        f"### 判決：**{cons_verdict}**",
        "",
    ]
    if best is not None:
        lines += [
            f"- Solo 松山 OOS med = **{fmt_pct(solo_med)}%**",
            f"- 同池最佳設計 = **`{best.design}`** "
            f"med={fmt_pct(best.med_radj_oos)}% n={int(best.n_oos)}",
            f"- 證據表：`consensus_compare.md` / `consensus_leaderboard.csv`",
            "",
        ]
    lines += [
        "| 判決 | 含義 |",
        "|---|---|",
        "| **不需要** | 預設只跟松山（+可選中壢 solo）；共識不改善或傷害 med |",
        "| **可選加分** | 主跟仍 solo；共識模式可當濾網／衛星，非必須 |",
        "| **必須** | solo 不穩且共識明顯抬升 — **本輪未達此門檻則不選** |",
        "",
        "---",
        "",
        "## 4. 怎麼共識最好（若啟用）",
        "",
    ]
    if cons_verdict == "不需要":
        lines += [
            f"- **不啟用共識為預設**；若實驗性加分，優先試 **`{cons_mode}`**"
            "（見 leaderboard），**不要用 OR union 當預設**（稀釋風險）。",
            "- 共識池鎖定：`{9217, 700b, 9801, 918X}`；可選加 `9227` 僅配 H14。",
            "- **禁止**納入虎尾／自營／外資。",
        ]
    else:
        lines += [
            f"- **最佳模式：`{cons_mode}`**（pool=core · R_song · H7）",
            "- 共識池：`9217, 700b, 9801, 918X`（勿加虎尾/自營/外資）",
            "- OR union **不是**最佳（除非 leaderboard 反證）",
            "- 城中僅在 H14 設計中可選加入 pool+",
        ]

    if song_ablation:
        lines += [
            "",
            "---",
            "",
            "## 5. MEGA 紀律（ops 研究結論）",
            "",
            f"- 松山 keepMega med={fmt_pct(song_ablation.get('keep'))}% → "
            f"xMega {fmt_pct(song_ablation.get('x'))}% "
            f"（Δ {fmt_pct(song_ablation.get('delta'))}pp）",
            "- **結論：MEGA exclude 對松山型跟單是必要條件，不是可選美化。**",
        ]

    lines += [
        "",
        "---",
        "",
        "## 6. Non-goals",
        "",
        "- 非 strategy 採納；非 launchd / Order。",
        "- 非 stock-first expert funnel（P4–P7）替代品。",
        "- Primary metric = OOS median event `r_adj`，≠ 單槽複利 NAV。",
        "",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def decide_consensus_verdict(df: pd.DataFrame) -> tuple[str, str, str]:
    """Return (verdict, best_design, rationale)."""
    sub = df[(df.rule == "R_song") & (df.hold == 7) & (df.pool == "core")].copy()
    if sub.empty:
        return "不需要", "solo_songshan", "no rows"
    solo = sub[sub.design == "solo_songshan"]
    if solo.empty:
        return "不需要", "solo_songshan", "missing solo"
    solo_med = float(solo.iloc[0].med_radj_oos)
    solo_n = int(solo.iloc[0].n_oos)

    candidates = sub[sub.flag_thin_oos == 0].sort_values("med_radj_oos", ascending=False)
    if candidates.empty:
        return "不需要", "solo_songshan", "all thin"

    best = candidates.iloc[0]
    best_d = str(best.design)
    best_med = float(best.med_radj_oos)

    cons_rows = sub[
        sub.design.isin(["same_day_ge2", "lookback1d_ge2", "intensity_same_day"])
        & (sub.flag_thin_oos == 0)
    ].sort_values("med_radj_oos", ascending=False)

    or_row = sub[sub.design == "or_union"]
    or_med = float(or_row.iloc[0].med_radj_oos) if len(or_row) else float("nan")

    if best_d == "solo_songshan":
        # Check if any consensus is close (+ within 0.5pp) with cleaner win
        if len(cons_rows):
            c = cons_rows.iloc[0]
            if c.med_radj_oos >= solo_med - 0.5 and c.n_oos >= THIN_OOS:
                return (
                    "可選加分",
                    str(c.design),
                    f"solo still best/tied; {c.design} near-par for optional filter",
                )
        return "不需要", "solo_songshan", "solo wins outright"

    # Consensus/OR beats solo
    if best_d == "or_union":
        if best_med >= solo_med + 1.0:
            return "可選加分", "or_union", "OR beats solo but dilution risk — optional only"
        return "不需要", "solo_songshan", "OR not meaningfully better"

    # true consensus modes
    if best_med >= solo_med + 2.0 and best.n_oos >= THIN_OOS:
        # "必須" only if solo is weak/negative while consensus strong
        if solo_med < 1.0 and best_med >= 3.0:
            return "必須", best_d, "solo weak; consensus lifts hard"
        return "可選加分", best_d, f"{best_d} beats solo by ≥2pp"
    if best_med > solo_med:
        return "可選加分", best_d, f"{best_d} edges solo"
    return "不需要", "solo_songshan", "no consensus advantage"


def append_pass4(
    deep: list[dict],
    consensus_df: pd.DataFrame,
    xin_rows: list[dict],
    verdict: str,
    best_mode: str,
    rationale: str,
) -> None:
    path = OUT / "DISCUSSION_NOTES.md"
    xin7 = next(
        (r for r in xin_rows if r["id"] == XINDIAN_ID and r["rule"] == "R_song" and r["hold"] == 7),
        None,
    )
    song = next((d for d in deep if d["id"] == "9217"), None)
    lines = [
        "",
        "---",
        "",
        f"## Pass 4 · {datetime.now().strftime('%Y-%m-%d %H:%M')} · follow / side-box / consensus",
        "",
        "**New artifacts:** `run_ab58_follow_consensus_study.py`, `FOLLOW_PROTOCOL_DRAFT.md`, "
        "`consensus_leaderboard.csv`, `consensus_compare.md`, `xindian_sidebox.md`, "
        "champion legs (+H14 / chase), Pass-4 notes.",
        "",
        "### Side-box 9661",
        "",
    ]
    if xin7 and song:
        lines.append(
            f"- 新店 R_song×xMega×H7: med={fmt_pct(xin7['med_radj_oos'])}% n={xin7['n_oos']} "
            f"thin={'Y' if xin7['flag_thin_oos'] else 'N'}"
        )
        lines.append(
            f"- 松山同尺: med={fmt_pct(song['med_oos_h7'])}% n={song['n_oos_h7']}"
        )
        dlt = xin7["med_radj_oos"] - song["med_oos_h7"]
        lines.append(f"- Δ(新店−松山)={fmt_pct(dlt)}pp")
    lines += [
        "",
        "### Consensus verdict",
        "",
        f"- **{verdict}** · best_mode=`{best_mode}` · {rationale}",
        "",
        "### Surprises (Pass 4)",
        "",
    ]
    # Auto-collect surprises from data
    sub = consensus_df[
        (consensus_df.rule == "R_song") & (consensus_df.hold == 7) & (consensus_df.pool == "core")
    ]
    for r in sub.itertuples(index=False):
        lines.append(
            f"- `{r.design}`: med_oos={fmt_pct(r.med_radj_oos)}% n={int(r.n_oos)} "
            f"win={('—' if pd.isna(r.win_radj_oos) else f'{r.win_radj_oos:.0%}')} "
            f"thin={'Y' if r.flag_thin_oos else 'N'}"
        )
    for d in deep:
        if d["id"] in ("9217", "700b", "9227", XINDIAN_ID):
            lines.append(
                f"- {d['name']} prefer **{d['prefer_hold']}**; "
                f"chase_med={fmt_pct(d['chase_med_oos'])}%; "
                f"top3_oos={d['top3_oos']}"
            )
    lines += [
        "",
        "### Conflicts sharpened",
        "",
        "- 經典錨新店 vs 松山：以 side-box 數字為準（見上），主賽排除 C 的代價可量化。",
        "- Overnight「要共識」vs branch-first solo：本輪共識判決見上 — 勿與 stock-first P6 混談。",
        "- 城中仍是 H14 型；強行 H7 共識池可能拖累。",
        "",
        "### Still open",
        "",
        "- R_1p5 是否值得把松山預設從 2億改回 1.5億（連續性 vs 主賽凍結）。",
        "- Soft entry 追價≤3% 是否在 OOS 穩定加分（樣本子集可能偏薄）。",
        "",
        "**Note-taker:** Pass 4 complete.",
        "",
    ]
    with path.open("a", encoding="utf-8") as f:
        f.write("\n".join(lines))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=None)
    ap.add_argument("--end", default=None)
    args = ap.parse_args()

    t0 = time.time()
    OUT.mkdir(parents=True, exist_ok=True)
    LEGS.mkdir(exist_ok=True)
    mega = ab58.load_mega()

    # Branches: AB58 + 9661
    branches = ab58.load_branches()
    meta = {str(b["id"]): b for b in branches}
    ids = [str(b["id"]) for b in branches]
    need_ids = sorted(set(ids) | {XINDIAN_ID} | set(POOL_PLUS))

    db = ab58.pick_db(args.db)
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
        date_index = {d: i for i, d in enumerate(calendar)}
        benchmark = mvp.load_benchmark(conn, calendar[0], calendar[-1])
        log("loading OHLC...")
        ohlc = mvp.load_stock_ohlc(conn, calendar[0], calendar[-1])
        log(f"ohlc keys={len(ohlc)}")
        log(f"loading Top1 for {len(need_ids)} branches (AB58+9661)...")
        top_buys = mvp.load_top_buys(conn, need_ids, START, end)
        log(f"branches with tape={len(top_buys)}")
    finally:
        conn.close()

    # ----- A. Side-box 9661 + R_1p5 sensitivity for 9217 & 9661 -----
    log("=== A. Side-box + R_1p5 ===")
    side_rows: list[dict] = []
    for bid in ("9217", XINDIAN_ID):
        name = NAME_MAP[bid]
        raw = list(top_buys.get(bid, {}).values())
        for rule in RULES:
            for hold in HOLDS:
                legs, stats = run_seat(
                    raw, mega, rule, hold, calendar, date_index, ohlc, benchmark
                )
                side_rows.append(
                    {
                        "id": bid,
                        "name": name,
                        "rule": rule,
                        "hold": hold,
                        "mega_mode": "xMega",
                        **stats,
                    }
                )
                # save legs for champions
                if rule == "R_song" and bid in {c[0] for c in CHAMPIONS}:
                    pd.DataFrame(legs).to_csv(
                        LEGS / f"legs_{bid}_R_song_xMega_H{hold}.csv", index=False
                    )

    songshan_h7 = next(
        r for r in side_rows if r["id"] == "9217" and r["rule"] == "R_song" and r["hold"] == 7
    )
    write_xindian_sidebox(side_rows, songshan_h7, OUT / "xindian_sidebox.md")
    pd.DataFrame(side_rows).to_csv(OUT / "xindian_sidebox_table.csv", index=False)
    log("wrote xindian_sidebox.md")

    # ----- B. Champion deep-dive -----
    log("=== B. Champion deep-dive ===")
    deep: list[dict] = []
    for bid, name in CHAMPIONS:
        raw = list(top_buys.get(bid, {}).values())
        legs7, _ = run_seat(raw, mega, "R_song", 7, calendar, date_index, ohlc, benchmark)
        legs14, _ = run_seat(raw, mega, "R_song", 14, calendar, date_index, ohlc, benchmark)
        dive = seat_deep_dive(bid, name, legs7, legs14, ohlc)
        deep.append(dive)
        # enriched legs
        pd.DataFrame(dive["legs7_enriched"]).to_csv(
            LEGS / f"legs_{bid}_R_song_xMega_H7_chase.csv", index=False
        )
        pd.DataFrame(dive["legs14_enriched"]).to_csv(
            LEGS / f"legs_{bid}_R_song_xMega_H14_chase.csv", index=False
        )
        log(
            f"  {name}: prefer={dive['prefer_hold']} "
            f"H7={fmt_pct(dive['med_oos_h7'])}% n={dive['n_oos_h7']} "
            f"H14={fmt_pct(dive['med_oos_h14'])}% "
            f"chase_med={fmt_pct(dive['chase_med_oos'])}%"
        )

    # MEGA ablation for 松山 from existing leaderboard if present
    song_ablation = None
    lb_path = OUT / "leaderboard_all.csv"
    if lb_path.exists():
        lb = pd.read_csv(lb_path)
        k = lb[(lb.id == "9217") & (lb.rule == "R_song") & (lb.hold == 7) & (lb.mega_mode == "keepMega")]
        x = lb[(lb.id == "9217") & (lb.rule == "R_song") & (lb.hold == 7) & (lb.mega_mode == "xMega")]
        if len(k) and len(x):
            song_ablation = {
                "keep": float(k.iloc[0].med_radj_oos),
                "x": float(x.iloc[0].med_radj_oos),
                "delta": float(x.iloc[0].med_radj_oos) - float(k.iloc[0].med_radj_oos),
            }

    # ----- C. Consensus -----
    log("=== C. Consensus study ===")
    board_rows = []
    for pool_name, pool in (("core", POOL_CORE), ("plus", POOL_PLUS)):
        for rule in ("R_song", "R_mid"):
            abs_f, sh_f = RULES[rule]
            by_branch = build_branch_events(top_buys, pool, mega, abs_f, sh_f)
            for hold in HOLDS:
                designs = consensus_designs(
                    by_branch, pool, calendar, date_index, ohlc, benchmark, hold
                )
                for design, legs, stats in designs:
                    board_rows.append(
                        {
                            "design": design,
                            "pool": pool_name,
                            "pool_members": "|".join(pool),
                            "rule": rule,
                            "hold": hold,
                            "mega_mode": "xMega",
                            "compound_oos": compound_slot(legs, oos_only=True),
                            **stats,
                        }
                    )
                    # save key legs
                    if rule == "R_song" and hold == 7 and pool_name == "core":
                        if legs:
                            pd.DataFrame(legs).to_csv(
                                LEGS / f"consensus_{design}_core_R_song_H7.csv",
                                index=False,
                            )

    consensus_df = pd.DataFrame(board_rows)
    consensus_df.to_csv(OUT / "consensus_leaderboard.csv", index=False)
    write_consensus_md(consensus_df, OUT / "consensus_compare.md")
    log("wrote consensus artifacts")

    verdict, best_mode, rationale = decide_consensus_verdict(consensus_df)
    log(f"consensus verdict={verdict} best={best_mode} ({rationale})")

    # Inject verdict into follow protocol via temporary column
    consensus_df = consensus_df.copy()
    consensus_df["verdict_hint"] = None
    consensus_df.loc[0, "verdict_hint"] = verdict  # hack for writer — we'll pass explicitly

    # Rewrite FOLLOW with explicit verdict injection
    write_follow_protocol_explicit(
        deep, consensus_df, side_rows, song_ablation, verdict, best_mode, rationale,
        OUT / "FOLLOW_PROTOCOL_DRAFT.md",
    )

    # Update FINDINGS with Pass 4 pointer
    findings = OUT / "FINDINGS_DRAFT.md"
    if findings.exists():
        extra = [
            "",
            "## Pass 4 pointer (follow / consensus)",
            "",
            f"- See `FOLLOW_PROTOCOL_DRAFT.md` · consensus **{verdict}** (`{best_mode}`).",
            "- Side-box: `xindian_sidebox.md`.",
            "- Consensus table: `consensus_compare.md`.",
            "",
        ]
        text = findings.read_text(encoding="utf-8")
        if "Pass 4 pointer" not in text:
            findings.write_text(text.rstrip() + "\n" + "\n".join(extra), encoding="utf-8")

    append_pass4(deep, consensus_df, side_rows, verdict, best_mode, rationale)
    log(f"done in {time.time()-t0:.1f}s → {OUT}")
    return 0


def write_follow_protocol_explicit(
    deep: list[dict],
    consensus_df: pd.DataFrame,
    xin_rows: list[dict],
    song_ablation: dict | None,
    verdict: str,
    best_mode: str,
    rationale: str,
    path: Path,
) -> None:
    """FOLLOW_PROTOCOL with explicit consensus verdict (overrides heuristic)."""
    sub = consensus_df[
        (consensus_df.rule == "R_song")
        & (consensus_df.hold == 7)
        & (consensus_df.pool == "core")
    ].copy()
    solo = sub[sub.design == "solo_songshan"]
    solo_med = float(solo.iloc[0].med_radj_oos) if len(solo) else float("nan")
    solo_n = int(solo.iloc[0].n_oos) if len(solo) else 0
    best_row = sub[sub.design == best_mode]
    best_med = float(best_row.iloc[0].med_radj_oos) if len(best_row) else float("nan")
    best_n = int(best_row.iloc[0].n_oos) if len(best_row) else 0

    song = next((d for d in deep if d["id"] == "9217"), None)
    zhongli = next((d for d in deep if d["id"] == "700b"), None)
    songjiang = next((d for d in deep if d["id"] == "9801"), None)
    taipei = next((d for d in deep if d["id"] == "918X"), None)
    # 918X may not be in CHAMPIONS deep — handle
    chengzhong = next((d for d in deep if d["id"] == "9227"), None)
    xin = next(
        (r for r in xin_rows if r["id"] == XINDIAN_ID and r["rule"] == "R_song" and r["hold"] == 7),
        None,
    )

    # Ensure 918X stats from consensus solo isn't available — pull from leaderboard
    lines = [
        "# FOLLOW_PROTOCOL_DRAFT · AB58 × MEGA Copytrade",
        "",
        f"> Frozen research recommendation · {datetime.now().date().isoformat()}  ",
        "> **Research layer only** — 非採納策略；不得寫入 `config/strategy.yaml` / Order。",
        "",
        "尺規：`PROTOCOL.md` · β=1.15 · 30bps · MEGA v1 exclude · OOS≥2026-01-01。",
        "",
        "---",
        "",
        "## 1. 跟誰（Primary follow list · ≤3 席）",
        "",
        "| Priority | Seat | Default hold | OOS med (R_song×xMega) | Why |",
        "|:---:|---|---|---:|---|",
    ]
    if song:
        lines.append(
            f"| **1** | **9217 凱基-松山** | **{song['prefer_hold']}** | "
            f"H7 {fmt_pct(song['med_oos_h7'])}% (n={song['n_oos_h7']}) · "
            f"H14 {fmt_pct(song['med_oos_h14'])}% | 主賽 H7 冠軍；MEGA 剔除後翻正 |"
        )
    if zhongli:
        lines.append(
            f"| **2** | **700b 兆豐-中壢** | **{zhongli['prefer_hold']}** | "
            f"H7 {fmt_pct(zhongli['med_oos_h7'])}% (n={zhongli['n_oos_h7']}) | "
            f"穩定正樣本；勝率偏高 |"
        )
    if songjiang and songjiang["med_oos_h7"] > 0 and songjiang["n_oos_h7"] >= THIN_OOS:
        lines.append(
            f"| 3（衛星） | 9801 元大-松江 | {songjiang['prefer_hold']} | "
            f"H7 {fmt_pct(songjiang['med_oos_h7'])}% (n={songjiang['n_oos_h7']}) | "
            f"樣本密、edge 次於松山/中壢；優先進共識池而非 solo 主跟 |"
        )
    if chengzhong:
        lines.append(
            f"| H14 專席 | 9227 凱基-城中 | **H14** | "
            f"H7 {fmt_pct(chengzhong['med_oos_h7'])}% · "
            f"H14 {fmt_pct(chengzhong['med_oos_h14'])}% "
            f"(n={chengzhong['n_oos_h14']}) | 僅放長持有；勿用 H7 跟 |"
        )

    lines += [
        "",
        "### Anti-picks（明確不跟）",
        "",
        "| Seat / class | Reason |",
        "|---|---|",
        "| 虎尾（9A9b / 9236 / 700F） | 強指紋弱 alpha；×MEGA 後仍負或 thin |",
        "| 權值通道席（9186 新竹、5383 高等） | ×MEGA 後 n 崩塌 |",
        "| 活躍負樣本（9875 土城永寧、9216 信義） | n 大但 OOS med 負 |",
        "| 自營 / 外資 | 非營業分點跟單對象 |",
        "| **9661 富邦新店** | C 外控；見 side-box — **不進 Primary** |",
        "",
    ]
    if xin and song:
        lines.append(
            f"**新店 vs 松山（同尺 R_song×H7）:** 新店 med={fmt_pct(xin['med_radj_oos'])}% "
            f"n={xin['n_oos']} vs 松山 {fmt_pct(song['med_oos_h7'])}% n={song['n_oos_h7']} "
            f"（Δ {fmt_pct(xin['med_radj_oos'] - song['med_oos_h7'])}pp）。詳 `xindian_sidebox.md`。"
        )

    lines += [
        "",
        "---",
        "",
        "## 2. 怎麼跟（Default rule string）",
        "",
        "```",
        "R_song ∩ xMega ∩ L1H7",
        "  abs ≥ 2e8 ∩ buy_share ≥ 0.25",
        "  exclude mega_blacklist_v1",
        "  entry = T+1 open · hold 7 trading days · exit close",
        "  cost 30bps RT · r_adj = r − 1.15 × r_IX0001",
        "  episode dedupe: (branch, stock) 5 calendar days · keep first",
        "```",
        "",
        "| Knob | Frozen | Note |",
        "|---|---|---|",
        "| Event | daily Top1 `buy×close` | 分點買超金額第一 |",
        "| **MEGA** | **mandatory exclude** | 松山 keepMega→xMega 翻正（見 ablation） |",
        "| Cost | 30 bps | 已扣於 r_adj |",
        "| Hold default | **H7**（松山/中壢） | 城中改 **H14** |",
        "| Soft entry | 追價≤3%；限價試算收×1.01 | **研究備註 only**，非 Order |",
        "",
        "### Per-seat hold",
        "",
        "| Seat | Prefer | H7 med / n | H14 med / n |",
        "|---|---|---:|---:|",
    ]
    for d in deep:
        if d["id"] == XINDIAN_ID:
            continue
        lines.append(
            f"| {d['name']} (`{d['id']}`) | **{d['prefer_hold']}** | "
            f"{fmt_pct(d['med_oos_h7'])}% / {d['n_oos_h7']} | "
            f"{fmt_pct(d['med_oos_h14'])}% / {d['n_oos_h14']} |"
        )

    lines += [
        "",
        "### Soft entry（legs chase）",
        "",
    ]
    for d in deep:
        if d["id"] in ("9217", "700b", "9801", "9227"):
            lines.append(
                f"- **{d['name']}**: chase 中位 {fmt_pct(d['chase_med_oos'])}%；"
                f"≤3% → n={d['chase_ok3_n']} med={fmt_pct(d['chase_ok3_med'])}%；"
                f">3% → n={d['chase_gt3_n']} med={fmt_pct(d['chase_gt3_med'])}%"
            )
    lines += [
        "",
        "> 隔日開盤相對訊號收追價 >3% → 研究傾向跳過/減碼；限價可設訊號收×1.01。",
        "",
        "---",
        "",
        "## 3. 需要共識嗎？",
        "",
        f"### 判決：**{verdict}**",
        "",
        f"- Solo 松山：med **{fmt_pct(solo_med)}%** (n={solo_n})",
        f"- 參照最佳模式：`{best_mode}` med **{fmt_pct(best_med)}%** (n={best_n})",
        f"- Rationale: {rationale}",
        f"- 全表：`consensus_compare.md`",
        "",
        "---",
        "",
        "## 4. 怎麼共識最好",
        "",
    ]
    if verdict == "不需要":
        lines += [
            f"- **預設不開共識**；主跟 = 松山 solo（+可選中壢）。",
            f"- 若要做實驗性濾網，可試 `{best_mode}`（見 leaderboard），",
            "  **不要用 `or_union` 當預設**（稀釋風險高）。",
            "- 池子若啟用：`{9217, 700b, 9801, 918X}`；禁虎尾/自營/外資。",
        ]
    elif verdict == "可選加分":
        lines += [
            f"- **最佳模式：`{best_mode}`**（pool=core · R_song · H7）",
            "- 主路徑仍可 solo 松山；共識當加分濾網／衛星。",
            "- 池：`9217, 700b, 9801, 918X`；`9227` 僅配 H14 的 pool+。",
            "- **禁止 OR-any 當預設**（除非表上 OR 顯著優於共識 — 本輪以表為準）。",
        ]
    else:  # 必須
        lines += [
            f"- **必須使用：`{best_mode}`**（pool=core · R_song · H7）",
            "- Solo 不足；共識為預設進場條件。",
            "- 池：`9217, 700b, 9801, 918X`",
        ]

    if song_ablation:
        lines += [
            "",
            "---",
            "",
            "## 5. MEGA 紀律",
            "",
            f"- 松山：keepMega {fmt_pct(song_ablation['keep'])}% → "
            f"xMega {fmt_pct(song_ablation['x'])}% "
            f"（Δ {fmt_pct(song_ablation['delta'])}pp）",
            "- **MEGA exclude = 必要條件（非可選）。**",
        ]

    lines += [
        "",
        "---",
        "",
        "## 6. Non-goals",
        "",
        "- 非 strategy 採納；非 Order / launchd。",
        "- 非 stock-first expert funnel 替代。",
        "- Primary = OOS median event `r_adj` ≠ 單槽複利。",
        "",
    ]
    # silence unused
    _ = (taipei,)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
