#!/usr/bin/env python3
"""華邦電（2344）專家池共識跟單 · 可重跑研究腳本.

Preregistered expert pool (same-stock funnel, 2024-07 chat):
  統一 (5850) · 統一-城中 (5854) · 國票安和 (779Z)

Champion application rules (T+1 open, L1H7, 30bps cost, vs 0050 excess):
  - OR·≥1億        — full-sample ceiling; OOS fragile
  - 共識·≥1億      — ≥2 core branches same day; best OOS balance
  - 共識·share≥5%  — secondary robust arm

Controls (report only): 凱基台北 9268 · 富邦新店 9661 · 凱基松山 9217

  PYTHONPATH=src python3 scripts/research/run_winbond_expert_pool_consensus.py
  PYTHONPATH=src python3 scripts/research/run_winbond_expert_pool_consensus.py \\
      --start 2024-07-01 --end 2026-07-16 --hold 10

Outputs (default):
  reports/research/branch-footprint-screen/winbond_expert_pool/
    rule_grid.csv · hold_grid.csv · is_oos.csv · overlay_grid.csv
    champion_signals.csv · run_summary.md · run_config.json
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import mean, median
from typing import Callable

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from report_paths import REPORTS_RESEARCH  # noqa: E402
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402

SOURCE = "finmind"
STOCK_ID = "2344"
STOCK_NAME = "華邦電"
BENCHMARK_ID = "0050"
DEFAULT_START = "2024-07-01"
DEFAULT_OOS_CUT = "2026-01-01"
DEFAULT_HOLD = 7
DEFAULT_COST = 0.003
DEDUPE_DAYS = 5
OUT_ROOT = REPORTS_RESEARCH / "branch-footprint-screen" / "winbond_expert_pool"

# Preregistered specialist core + controls
CORE_POOL: dict[str, str] = {
    "5850": "統一",
    "5854": "統一-城中",
    "779Z": "國票安和",
}
CONTROL_POOL: dict[str, str] = {
    "9268": "凱基台北",
    "9661": "富邦新店",
    "9217": "凱基松山",
}
ALL_POOL = {**CORE_POOL, **CONTROL_POOL}
CORE_IDS = list(CORE_POOL.keys())

RulePred = Callable[[dict], bool]


@dataclass(frozen=True)
class RuleSpec:
    name: str
    pred: RulePred


@dataclass
class Stats:
    label: str
    n: int
    med: float
    mean: float
    win: float
    med_ex: float | None
    p05: float
    p_m05: float

    def as_row(self, **extra) -> dict:
        row = {
            "label": self.label,
            "n": self.n,
            "med_pct": round(self.med * 100, 2),
            "mean_pct": round(self.mean * 100, 2),
            "win_pct": round(self.win * 100, 1),
            "med_excess_pct": (
                round(self.med_ex * 100, 2) if self.med_ex is not None else None
            ),
            "big_win_pct": round(self.p05 * 100, 1),
            "big_loss_pct": round(self.p_m05 * 100, 1),
        }
        row.update(extra)
        return row


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="華邦電專家池共識跟單研究")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--start", default=DEFAULT_START)
    ap.add_argument("--end", default=date.today().isoformat())
    ap.add_argument("--oos-cut", default=DEFAULT_OOS_CUT, help="IS/OOS split date")
    ap.add_argument("--hold", type=int, default=DEFAULT_HOLD, help="Primary hold days")
    ap.add_argument(
        "--out-dir",
        type=Path,
        default=OUT_ROOT,
        help="Output directory (default: fixed under branch-footprint-screen)",
    )
    ap.add_argument(
        "--timestamped",
        action="store_true",
        help="Append YYYYMMDD_HHMMSS subdir under --out-dir",
    )
    ap.add_argument(
        "--quick",
        action="store_true",
        help="Smaller hold grid and skip overlay section",
    )
    return ap.parse_args()


def load_prices(conn, start: str, end: str) -> tuple[dict, dict[str, list[str]]]:
    px: dict[tuple[str, str], tuple[float, float]] = {}
    sid_dates: dict[str, list[str]] = defaultdict(list)
    pad_start = (
        datetime.strptime(start, "%Y-%m-%d") - timedelta(days=45)
    ).strftime("%Y-%m-%d")
    pad_end = (
        datetime.strptime(end, "%Y-%m-%d") + timedelta(days=45)
    ).strftime("%Y-%m-%d")
    stock_ids = (STOCK_ID, BENCHMARK_ID)
    for row in conn.execute(
        """
        SELECT stock_id, trade_date, open, close
        FROM stock_daily_bars
        WHERE source = ? AND trade_date BETWEEN ? AND ? AND close > 0
          AND stock_id IN (?, ?)
        """,
        (SOURCE, pad_start, pad_end, *stock_ids),
    ):
        op = float(row["open"]) if row["open"] else float(row["close"])
        cl = float(row["close"])
        px[(row["stock_id"], row["trade_date"])] = (op, cl)
        sid_dates[row["stock_id"]].append(row["trade_date"])
    for sid in sid_dates:
        sid_dates[sid].sort()
    return px, sid_dates


def load_highs(conn, start: str, end: str) -> dict[str, tuple[float | None, float | None, float | None]]:
    pad_start = (
        datetime.strptime(start, "%Y-%m-%d") - timedelta(days=45)
    ).strftime("%Y-%m-%d")
    out: dict[str, tuple[float | None, float | None, float | None]] = {}
    for row in conn.execute(
        """
        SELECT trade_date, high, low, volume
        FROM stock_daily_bars
        WHERE stock_id = ? AND source = ? AND trade_date >= ?
        """,
        (STOCK_ID, SOURCE, pad_start),
    ):
        hi = float(row["high"]) if row["high"] else None
        lo = float(row["low"]) if row["low"] else None
        vol = float(row["volume"]) if row["volume"] else None
        out[row["trade_date"]] = (hi, lo, vol)
    return out


def fwd_ret(
    sig_date: str,
    px: dict,
    sid_dates: dict[str, list[str]],
    *,
    hold: int,
    cost: float = DEFAULT_COST,
) -> tuple[float | None, float | None]:
    ds = [d for d in sid_dates[STOCK_ID] if d > sig_date]
    if len(ds) < hold:
        return None, None
    entry = px[(STOCK_ID, ds[0])][0]
    exit_px = px[(STOCK_ID, ds[hold - 1])][1]
    ret = exit_px / entry - 1 - cost
    md = [d for d in sid_dates.get(BENCHMARK_ID, []) if d > sig_date]
    if len(md) < hold:
        return ret, None
    mret = px[(BENCHMARK_ID, md[hold - 1])][1] / px[(BENCHMARK_ID, md[0])][0] - 1
    return ret, ret - mret


def dedupe_events(events: list[dict], days: int = DEDUPE_DAYS) -> list[dict]:
    events = sorted(events, key=lambda x: x["date"])
    out: list[dict] = []
    last: str | None = None
    for e in events:
        if last is not None:
            gap = (
                datetime.strptime(e["date"], "%Y-%m-%d")
                - datetime.strptime(last, "%Y-%m-%d")
            ).days
            if gap < days:
                continue
        out.append(e)
        last = e["date"]
    return out


def compute_stats(events: list[dict], label: str) -> Stats | None:
    if not events:
        return None
    rets = [e["ret"] for e in events]
    exs = [e["excess"] for e in events if e["excess"] is not None]
    return Stats(
        label=label,
        n=len(events),
        med=median(rets),
        mean=mean(rets),
        win=sum(1 for r in rets if r > 0) / len(rets),
        med_ex=median(exs) if exs else None,
        p05=sum(1 for r in rets if r >= 0.05) / len(rets),
        p_m05=sum(1 for r in rets if r <= -0.05) / len(rets),
    )


def attach_fwd(
    events: list[dict],
    px: dict,
    sid_dates: dict[str, list[str]],
    *,
    hold: int,
) -> list[dict]:
    out: list[dict] = []
    for e in events:
        ret, excess = fwd_ret(e["date"], px, sid_dates, hold=hold)
        if ret is None:
            continue
        out.append({**e, "ret": ret, "excess": excess, "hold": hold})
    return out


def build_branch_events(
    conn,
    *,
    start: str,
    end: str,
    px: dict,
    trader_ids: list[str],
) -> dict[str, list[dict]]:
    placeholders = ",".join("?" * len(trader_ids))
    raw = conn.execute(
        f"""
        SELECT trade_date, securities_trader_id, securities_trader, net, buy, sell
        FROM stock_broker_branch_daily
        WHERE stock_id = ? AND source = ? AND trade_date BETWEEN ? AND ?
          AND securities_trader_id IN ({placeholders})
        """,
        (STOCK_ID, SOURCE, start, end, *trader_ids),
    ).fetchall()

    focus_dates = sorted({row["trade_date"] for row in raw})
    if focus_dates:
        date_ph = ",".join("?" * len(focus_dates))
        all_branch = conn.execute(
            f"""
            SELECT b.trade_date, b.securities_trader_id, b.stock_id, b.net, p.close
            FROM stock_broker_branch_daily b
            JOIN stock_daily_bars p
              ON p.stock_id = b.stock_id AND p.trade_date = b.trade_date AND p.source = ?
            WHERE b.securities_trader_id IN ({placeholders})
              AND b.source = ? AND b.net > 0
              AND b.trade_date IN ({date_ph}) AND p.close > 0
            """,
            (SOURCE, *trader_ids, SOURCE, *focus_dates),
        ).fetchall()
    else:
        all_branch = []

    rank_share: dict[tuple[str, str], tuple[int | None, float | None, float]] = {}
    day_pos: dict[tuple[str, str], list[tuple[str, float]]] = defaultdict(list)
    for row in all_branch:
        amt = float(row["net"]) * float(row["close"])
        day_pos[(row["securities_trader_id"], row["trade_date"])].append(
            (row["stock_id"], amt)
        )
    for key, lst in day_pos.items():
        tot = sum(a for _, a in lst)
        for i, (sid, amt) in enumerate(sorted(lst, key=lambda x: -x[1]), 1):
            if sid == STOCK_ID:
                rank_share[key] = (i, amt / tot if tot else 0.0, amt)

    events_by: dict[str, list[dict]] = defaultdict(list)
    for row in raw:
        tid = row["securities_trader_id"]
        d = row["trade_date"]
        if (STOCK_ID, d) not in px:
            continue
        net = float(row["net"])
        if net <= 0:
            continue
        close = px[(STOCK_ID, d)][1]
        net_amt = net * close
        rs = rank_share.get((tid, d), (None, None, net_amt))
        events_by[tid].append(
            {
                "date": d,
                "tid": tid,
                "name": ALL_POOL.get(tid, row["securities_trader"] or tid),
                "net_amt": net_amt,
                "net": net,
                "rank": rs[0],
                "share": rs[1],
            }
        )
    return events_by


def pool_signals(
    events_by: dict[str, list[dict]],
    tids: list[str],
    pred: RulePred,
    *,
    dedupe_days: int = DEDUPE_DAYS,
) -> tuple[list[dict], list[dict]]:
    by_date: dict[str, list[dict]] = defaultdict(list)
    for tid in tids:
        for e in events_by.get(tid, []):
            if pred(e):
                by_date[e["date"]].append(e)
    singles: list[dict] = []
    consensus: list[dict] = []
    for d, lst in sorted(by_date.items()):
        branches = {e["tid"] for e in lst}
        best = max(lst, key=lambda x: x["net_amt"])
        payload = {
            **best,
            "n_branches": len(branches),
            "branches": sorted(branches),
        }
        singles.append(payload)
        if len(branches) >= 2:
            consensus.append(payload)
    return dedupe_events(singles, dedupe_days), dedupe_events(consensus, dedupe_days)


def rule_specs() -> list[RuleSpec]:
    return [
        RuleSpec("≥0.5億", lambda e: e["net_amt"] >= 5e7),
        RuleSpec("≥1億", lambda e: e["net_amt"] >= 1e8),
        RuleSpec("≥3億", lambda e: e["net_amt"] >= 3e8),
        RuleSpec(
            "Top5",
            lambda e: e["rank"] is not None and e["rank"] <= 5 and e["net_amt"] >= 5e7,
        ),
        RuleSpec(
            "Top3",
            lambda e: e["rank"] is not None and e["rank"] <= 3 and e["net_amt"] >= 5e7,
        ),
        RuleSpec(
            "share≥5%",
            lambda e: e["share"] is not None and e["share"] >= 0.05,
        ),
        RuleSpec(
            "share≥3%",
            lambda e: e["share"] is not None and e["share"] >= 0.03,
        ),
        RuleSpec(
            "Top5∩≥1億",
            lambda e: e["rank"] is not None
            and e["rank"] <= 5
            and e["net_amt"] >= 1e8,
        ),
        RuleSpec(
            "share≥5%∩≥1億",
            lambda e: e["share"] is not None
            and e["share"] >= 0.05
            and e["net_amt"] >= 1e8,
        ),
    ]


def enrich_overlay(
    events: list[dict],
    events_by: dict[str, list[dict]],
    px: dict,
    sid_dates: dict[str, list[str]],
    highs: dict,
    *,
    core_ids: list[str],
) -> list[dict]:
    enriched: list[dict] = []
    for e in events:
        d = e["date"]
        ds = [x for x in sid_dates[STOCK_ID] if x <= d]
        ret5 = dist_hi = volr = None
        if len(ds) >= 6:
            ret5 = px[(STOCK_ID, d)][1] / px[(STOCK_ID, ds[-6])][1] - 1
        if len(ds) >= 20 and all(highs.get(x, (None,))[0] for x in ds[-20:]):
            hi = max(highs[x][0] for x in ds[-20:] if highs[x][0] is not None)
            dist_hi = px[(STOCK_ID, d)][1] / hi - 1
        if len(ds) >= 20:
            vols = [highs[x][2] for x in ds[-20:] if highs.get(x) and highs[x][2]]
            if vols and highs.get(d) and highs[d][2]:
                volr = highs[d][2] / mean(vols)
        addon = False
        for tid in core_ids:
            for pe in events_by.get(tid, []):
                if pe["date"] >= d:
                    continue
                gap = (
                    datetime.strptime(d, "%Y-%m-%d")
                    - datetime.strptime(pe["date"], "%Y-%m-%d")
                ).days
                if gap <= 20 and pe["net_amt"] >= 5e7:
                    addon = True
                    break
            if addon:
                break
        enriched.append(
            {**e, "ret5": ret5, "dist_hi": dist_hi, "volr": volr, "addon": addon}
        )
    return enriched


def overlay_specs() -> list[tuple[str, RulePred]]:
    return [
        ("無濾", lambda e: True),
        ("回檔距高≥8%", lambda e: e["dist_hi"] is not None and e["dist_hi"] <= -0.08),
        ("近高≤3%", lambda e: e["dist_hi"] is not None and e["dist_hi"] >= -0.03),
        ("前5日跌≥5%", lambda e: e["ret5"] is not None and e["ret5"] <= -0.05),
        ("前5日漲≥8%", lambda e: e["ret5"] is not None and e["ret5"] >= 0.08),
        ("放量≥1.5x", lambda e: e["volr"] is not None and e["volr"] >= 1.5),
        ("池內加碼", lambda e: e["addon"]),
        ("池內首擊", lambda e: not e["addon"]),
        (
            "回檔+加碼",
            lambda e: e["addon"]
            and e["dist_hi"] is not None
            and e["dist_hi"] <= -0.08,
        ),
    ]


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    headers = list(rows[0].keys())
    lines = [",".join(headers)]
    for row in rows:
        vals = []
        for h in headers:
            v = row.get(h)
            if v is None:
                vals.append("")
            elif isinstance(v, float):
                vals.append(f"{v:.4f}" if abs(v) < 1 else f"{v:.2f}")
            else:
                vals.append(str(v))
        lines.append(",".join(vals))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def fmt_pct(x: float | None) -> str:
    if x is None:
        return "n/a"
    return f"{x * 100:+.1f}%"


def build_summary_md(
    *,
    args: argparse.Namespace,
    out_dir: Path,
    single_rows: list[dict],
    rule_rows: list[dict],
    hold_rows: list[dict],
    is_oos_rows: list[dict],
    overlay_rows: list[dict],
    champion_rows: list[dict],
    half_rows: list[dict],
) -> str:
    lines = [
        f"# 華邦電專家池共識跟單 · {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        "> Research layer · 未採納 · T+1 open · L1H7 · 30bps · excess vs 0050",
        "",
        "## 設定",
        "",
        f"- 標的：`{STOCK_ID}` {STOCK_NAME}",
        f"- 窗口：`{args.start}` .. `{args.end}`",
        f"- OOS 切：`{args.oos_cut}`",
        f"- 主持有：H{args.hold}",
        f"- 專家核心：{', '.join(f'{n} ({i})' for i, n in CORE_POOL.items())}",
        "",
        "## 單分點基準（≥0.5億）",
        "",
        "| 分點 | n | 中位 | 超額 | 勝率 | 大賺 | 大虧 |",
        "|------|---|------|------|------|------|------|",
    ]
    for r in single_rows:
        ex = r.get("med_excess_pct")
        ex_s = f"{ex:+.1f}%" if ex is not None else "n/a"
        lines.append(
            f"| {r['label']} | {r['n']} | {r['med_pct']:+.1f}% | "
            f"{ex_s} | {r['win_pct']:.0f}% | {r['big_win_pct']:.0f}% | "
            f"{r['big_loss_pct']:.0f}% |"
        )

    lines += [
        "",
        "## 規則網格（核心池 · H"
        + str(args.hold)
        + "）",
        "",
        "| 模式 | 規則 | n | 中位 | 超額 | 勝率 | 大賺 | 大虧 |",
        "|------|------|---|------|------|------|------|------|",
    ]
    for r in rule_rows:
        ex = r["med_excess_pct"]
        ex_s = f"{ex:+.1f}%" if ex is not None else "n/a"
        lines.append(
            f"| {r['mode']} | {r['rule']} | {r['n']} | {r['med_pct']:+.1f}% | "
            f"{ex_s} | {r['win_pct']:.0f}% | {r['big_win_pct']:.0f}% | "
            f"{r['big_loss_pct']:.0f}% |"
        )

    lines += [
        "",
        "## IS / OOS（H" + str(args.hold) + "）",
        "",
        "| 模式 | 規則 | IS n/中位/勝 | OOS n/中位/勝 |",
        "|------|------|--------------|---------------|",
    ]
    for r in is_oos_rows:
        lines.append(
            f"| {r['mode']} | {r['rule']} | "
            f"{r['is_n']}/{r['is_med_pct']:+.1f}%/{r['is_win_pct']:.0f}% | "
            f"{r['oos_n']}/{r['oos_med_pct']:+.1f}%/{r['oos_win_pct']:.0f}% |"
        )

    if hold_rows:
        lines += [
            "",
            "## 持有天數網格（冠軍候選）",
            "",
            "| 模式 | 規則 | H | n | 中位 | 勝率 |",
            "|------|------|---|---|------|------|",
        ]
        for r in hold_rows:
            lines.append(
                f"| {r['mode']} | {r['rule']} | H{r['hold']} | {r['n']} | "
                f"{r['med_pct']:+.1f}% | {r['win_pct']:.0f}% |"
            )

    if overlay_rows:
        lines += [
            "",
            "## 疊加濾網（OR·≥1億）",
            "",
            "| 濾網 | n | 中位 | 勝率 | 大賺 | 大虧 |",
            "|------|---|------|------|------|------|",
        ]
        for r in overlay_rows:
            lines.append(
                f"| {r['overlay']} | {r['n']} | {r['med_pct']:+.1f}% | "
                f"{r['win_pct']:.0f}% | {r['big_win_pct']:.0f}% | "
                f"{r['big_loss_pct']:.0f}% |"
            )

    lines += [
        "",
        "## 冠軍規則（預先登錄）",
        "",
        "| 用途 | 規則 | 備註 |",
        "|------|------|------|",
        "| 全樣本天花板 | OR·≥1億 | 中位最高；2026 OOS 易轉弱 |",
        "| **實務首選** | **共識·≥1億** | ≥2 核心分點同日；OOS 較穩 |",
        "| 次選 | 共識·share≥5% | 樣本略多、OOS 亦正 |",
        "",
        "## 半年穩定度（共識·≥1億）",
        "",
        "| 半年 | n | 中位 |",
        "|------|---|------|",
    ]
    for r in half_rows:
        med_s = f"{r['med_pct']:+.1f}%" if r["med_pct"] is not None else "n/a"
        lines.append(f"| {r['half']} | {r['n']} | {med_s} |")

    lines += [
        "",
        "## 產物",
        "",
        f"- `{out_dir.name}/rule_grid.csv`",
        f"- `{out_dir.name}/hold_grid.csv`",
        f"- `{out_dir.name}/is_oos.csv`",
        f"- `{out_dir.name}/overlay_grid.csv`",
        f"- `{out_dir.name}/champion_signals.csv`",
        f"- `{out_dir.name}/run_config.json`",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    out_dir = args.out_dir
    if args.timestamped:
        out_dir = out_dir / datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)

    conn = connect(args.db)
    try:
        px, sid_dates = load_prices(conn, args.start, args.end)
        if STOCK_ID not in sid_dates or not sid_dates[STOCK_ID]:
            print(f"ERROR: no daily bars for {STOCK_ID}", file=sys.stderr)
            return 2
        highs = {} if args.quick else load_highs(conn, args.start, args.end)
        events_by = build_branch_events(
            conn,
            start=args.start,
            end=args.end,
            px=px,
            trader_ids=list(ALL_POOL.keys()),
        )

        tape_counts = {
            tid: len(events_by.get(tid, []))
            for tid in ALL_POOL
        }
        print(
            f"window={args.start}..{args.end} hold=H{args.hold} "
            f"tape_pos_rows={sum(tape_counts.values())} out={out_dir}"
        )

        # --- single branch baselines ---
        single_rows: list[dict] = []
        kgi_baseline: Stats | None = None
        for tid, name in ALL_POOL.items():
            ev = [e for e in events_by.get(tid, []) if e["net_amt"] >= 5e7]
            ep = attach_fwd(dedupe_events(ev), px, sid_dates, hold=args.hold)
            st = compute_stats(ep, name)
            if st:
                single_rows.append(st.as_row(tid=tid, pool="all"))
            if tid == "9268":
                kgi_baseline = st

        # --- rule grid ---
        rules = rule_specs()
        rule_rows: list[dict] = []
        or_store: dict[str, list[dict]] = {}
        cons_store: dict[str, list[dict]] = {}
        for spec in rules:
            singles, consensus = pool_signals(events_by, CORE_IDS, spec.pred)
            or_ep = attach_fwd(singles, px, sid_dates, hold=args.hold)
            cons_ep = attach_fwd(consensus, px, sid_dates, hold=args.hold)
            or_store[spec.name] = singles
            cons_store[spec.name] = consensus
            for mode, ep in (("OR", or_ep), ("共識", cons_ep)):
                st = compute_stats(ep, f"{mode}·{spec.name}")
                if st:
                    row = st.as_row(mode=mode, rule=spec.name, hold=args.hold)
                    if kgi_baseline and kgi_baseline.n >= 5:
                        row["vs_kgi_med_pp"] = round(
                            (st.med - kgi_baseline.med) * 100, 1
                        )
                    rule_rows.append(row)

        # --- hold grid (champion candidates) ---
        hold_rows: list[dict] = []
        champ_rules = ["≥0.5億", "≥1億", "Top5", "share≥5%", "Top5∩≥1億"]
        hold_grid = [5, 7, 10, 14] if not args.quick else [args.hold, 10]
        for spec in rules:
            if spec.name not in champ_rules:
                continue
            singles, consensus = pool_signals(events_by, CORE_IDS, spec.pred)
            for mode, base in (("OR", singles), ("共識", consensus)):
                for h in hold_grid:
                    ep = attach_fwd(base, px, sid_dates, hold=h)
                    st = compute_stats(ep, f"{mode}·{spec.name}")
                    if st:
                        hold_rows.append(st.as_row(mode=mode, rule=spec.name, hold=h))

        # --- IS/OOS ---
        is_oos_rows: list[dict] = []
        for spec in rules:
            for mode, store in (("OR", or_store), ("共識", cons_store)):
                ep = attach_fwd(store[spec.name], px, sid_dates, hold=args.hold)
                is_ = [e for e in ep if e["date"] < args.oos_cut]
                oos = [e for e in ep if e["date"] >= args.oos_cut]
                si, so = compute_stats(is_, "IS"), compute_stats(oos, "OOS")
                if si and so:
                    is_oos_rows.append(
                        {
                            "mode": mode,
                            "rule": spec.name,
                            "hold": args.hold,
                            "is_n": si.n,
                            "is_med_pct": round(si.med * 100, 1),
                            "is_win_pct": round(si.win * 100, 0),
                            "oos_n": so.n,
                            "oos_med_pct": round(so.med * 100, 1),
                            "oos_win_pct": round(so.win * 100, 0),
                        }
                    )

        # KGI control split
        kgi_ev = dedupe_events(
            [e for e in events_by.get("9268", []) if e["net_amt"] >= 5e7]
        )
        kgi_ep = attach_fwd(kgi_ev, px, sid_dates, hold=args.hold)
        kgi_is = [e for e in kgi_ep if e["date"] < args.oos_cut]
        kgi_oos = [e for e in kgi_ep if e["date"] >= args.oos_cut]
        si, so = compute_stats(kgi_is, "IS"), compute_stats(kgi_oos, "OOS")
        if si and so:
            is_oos_rows.append(
                {
                    "mode": "control",
                    "rule": "凱北≥0.5億",
                    "hold": args.hold,
                    "is_n": si.n,
                    "is_med_pct": round(si.med * 100, 1),
                    "is_win_pct": round(si.win * 100, 0),
                    "oos_n": so.n,
                    "oos_med_pct": round(so.med * 100, 1),
                    "oos_win_pct": round(so.win * 100, 0),
                }
            )

        # --- overlays on OR≥1億 ---
        overlay_rows: list[dict] = []
        if not args.quick:
            base_or = or_store["≥1億"]
            enriched = enrich_overlay(
                base_or, events_by, px, sid_dates, highs, core_ids=CORE_IDS
            )
            for name, pred in overlay_specs():
                sub = [e for e in enriched if pred(e)]
                ep = attach_fwd(sub, px, sid_dates, hold=args.hold)
                st = compute_stats(ep, name)
                if st:
                    overlay_rows.append(st.as_row(overlay=name, base="OR·≥1億"))

        # --- champion signal ledger ---
        champion_specs = [
            ("OR·≥1億", or_store["≥1億"]),
            ("共識·≥1億", cons_store["≥1億"]),
            ("共識·share≥5%", cons_store["share≥5%"]),
        ]
        champion_rows: list[dict] = []
        for label, base in champion_specs:
            ep = attach_fwd(base, px, sid_dates, hold=args.hold)
            for e in ep:
                champion_rows.append(
                    {
                        "strategy": label,
                        "signal_date": e["date"],
                        "branch_id": e["tid"],
                        "branch_name": e["name"],
                        "net_amt_m": round(e["net_amt"] / 1e6, 1),
                        "rank": e["rank"],
                        "share_pct": (
                            round(e["share"] * 100, 1) if e["share"] is not None else None
                        ),
                        "n_branches": e.get("n_branches", 1),
                        "branches": "|".join(e.get("branches", [e["tid"]])),
                        "hold": args.hold,
                        "ret_pct": round(e["ret"] * 100, 2),
                        "excess_pct": (
                            round(e["excess"] * 100, 2)
                            if e["excess"] is not None
                            else None
                        ),
                        "oos": e["date"] >= args.oos_cut,
                    }
                )

        # --- half-year stability for 共識·≥1億 ---
        folds = [
            ("2024H2", "2024-07-01", "2024-12-31"),
            ("2025H1", "2025-01-01", "2025-06-30"),
            ("2025H2", "2025-07-01", "2025-12-31"),
            ("2026H1", "2026-01-01", "2026-06-30"),
        ]
        half_rows: list[dict] = []
        cons_ep = attach_fwd(cons_store["≥1億"], px, sid_dates, hold=args.hold)
        for fl, a, b in folds:
            sub = [e for e in cons_ep if a <= e["date"] <= b]
            if len(sub) < 2:
                half_rows.append({"half": fl, "n": len(sub), "med_pct": None})
            else:
                half_rows.append(
                    {
                        "half": fl,
                        "n": len(sub),
                        "med_pct": round(median([e["ret"] for e in sub]) * 100, 1),
                    }
                )

        # --- write artifacts ---
        write_csv(out_dir / "single_branch_baselines.csv", single_rows)
        write_csv(out_dir / "rule_grid.csv", rule_rows)
        write_csv(out_dir / "hold_grid.csv", hold_rows)
        write_csv(out_dir / "is_oos.csv", is_oos_rows)
        write_csv(out_dir / "overlay_grid.csv", overlay_rows)
        write_csv(out_dir / "champion_signals.csv", champion_rows)

        config = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "stock_id": STOCK_ID,
            "stock_name": STOCK_NAME,
            "start": args.start,
            "end": args.end,
            "oos_cut": args.oos_cut,
            "hold": args.hold,
            "cost_bps": int(DEFAULT_COST * 10000),
            "benchmark": BENCHMARK_ID,
            "dedupe_days": DEDUPE_DAYS,
            "core_pool": CORE_POOL,
            "control_pool": CONTROL_POOL,
            "champion_rules": [
                "OR·≥1億",
                "共識·≥1億",
                "共識·share≥5%",
            ],
            "tape_pos_rows": tape_counts,
        }
        (out_dir / "run_config.json").write_text(
            json.dumps(config, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        summary = build_summary_md(
            args=args,
            out_dir=out_dir,
            single_rows=single_rows,
            rule_rows=rule_rows,
            hold_rows=hold_rows,
            is_oos_rows=is_oos_rows,
            overlay_rows=overlay_rows,
            champion_rows=champion_rows,
            half_rows=half_rows,
        )
        (out_dir / "run_summary.md").write_text(summary, encoding="utf-8")

        # console headline
        or1 = next((r for r in rule_rows if r["mode"] == "OR" and r["rule"] == "≥1億"), None)
        cons1 = next(
            (r for r in rule_rows if r["mode"] == "共識" and r["rule"] == "≥1億"), None
        )
        print(f"wrote {out_dir}")
        if or1:
            print(
                f"  OR·≥1億: n={or1['n']} med={or1['med_pct']:+.1f}% "
                f"win={or1['win_pct']:.0f}%"
            )
        if cons1:
            print(
                f"  共識·≥1億: n={cons1['n']} med={cons1['med_pct']:+.1f}% "
                f"win={cons1['win_pct']:.0f}%"
            )
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
