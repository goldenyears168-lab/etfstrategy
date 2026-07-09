#!/usr/bin/env python3
"""00981A 持股變動 × RRG 象限/軌跡統計檢定（2025-05 起）。"""

from __future__ import annotations

import argparse
import csv
import json
import random
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from stock_db import (
    DEFAULT_DB_PATH,
    PROJECT_ROOT,
    compute_etf_holdings_changes,
    connect,
    list_etf_snapshot_dates,
    load_etf_constituent_watchlist,
)

from report_paths import RESEARCH_COPYTRADE_00981A

REPORTS = RESEARCH_COPYTRADE_00981A
DATA_ARTIFACTS = REPORTS / "_holdings_rrg_audit"
QUADRANTS = ("leading", "weakening", "lagging", "improving")
QUAD_ZH = {
    "leading": "Leading",
    "weakening": "Weakening",
    "lagging": "Lagging",
    "improving": "Improving",
}


@dataclass
class ChangeEvent:
    change_date: str
    prev_date: str
    stock_id: str
    stock_name: str
    action: str
    weight_delta: float | None


def _collect_events(conn, etf_code: str, start_date: str) -> list[ChangeEvent]:
    dates = [d for d in list_etf_snapshot_dates(conn, etf_code) if d >= start_date]
    dates.sort()
    events: list[ChangeEvent] = []
    for i in range(1, len(dates)):
        prev_d, curr_d = dates[i - 1], dates[i]
        for row in compute_etf_holdings_changes(conn, etf_code, curr_d, prev_d):
            if row["action"] == "不变":
                continue
            wd = row["weight_delta"]
            events.append(
                ChangeEvent(
                    change_date=curr_d,
                    prev_date=prev_d,
                    stock_id=row["stock_id"],
                    stock_name=row["stock_name"] or "",
                    action=row["action"],
                    weight_delta=float(wd) if wd == wd else None,
                )
            )
    return events


def _load_rrg_lookup(conn, etf_codes: tuple[str, ...], length: int) -> tuple[dict, dict]:
    from research.backtest.finpilot_local_backtest import load_price_panels
    from market_benchmark import load_benchmark_close
    from rrg_rotation import classify_quadrant, compute_rrg_panel

    watch = load_etf_constituent_watchlist(conn, etf_codes)
    universe_ids = [w["stock_id"] for w in watch]
    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index)
    rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench, length=length)
    trading_days = list(rs_ratio.index)

    def day_index(d: str) -> int:
        return trading_days.index(d)

    quad_at: dict[tuple[str, str], str] = {}
    rs_at: dict[tuple[str, str], float] = {}
    mom_at: dict[tuple[str, str], float] = {}
    universe_by_date: dict[str, list[str]] = {}

    for d in trading_days:
        ids_today = []
        for sid in universe_ids:
            if sid not in rs_ratio.columns:
                continue
            rv, mv = rs_ratio.at[d, sid], rs_mom.at[d, sid]
            if rv != rv or mv != mv:
                continue
            rv_f, mv_f = float(rv), float(mv)
            quad_at[(sid, d)] = classify_quadrant(rv_f, mv_f)
            rs_at[(sid, d)] = rv_f
            mom_at[(sid, d)] = mv_f
            ids_today.append(sid)
        universe_by_date[d] = ids_today

    meta = {"trading_days": trading_days, "day_index": day_index}
    lookup = {"quad_at": quad_at, "rs_at": rs_at, "mom_at": mom_at, "universe_by_date": universe_by_date}
    return lookup, meta


def _universe_quad_counts(lookup: dict, d: str) -> Counter:
    c: Counter = Counter()
    for sid in lookup["universe_by_date"].get(d, []):
        q = lookup["quad_at"].get((sid, d))
        if q:
            c[q] += 1
    return c


def _chi2_quadrant(events: list[ChangeEvent], lookup: dict) -> dict:
    from scipy.stats import chi2

    observed = Counter()
    expected = Counter({q: 0.0 for q in QUADRANTS})
    used = 0
    for e in events:
        q = lookup["quad_at"].get((e.stock_id, e.change_date))
        if not q:
            continue
        used += 1
        observed[q] += 1
        uni = _universe_quad_counts(lookup, e.change_date)
        n = sum(uni.values())
        if n == 0:
            continue
        for qq in QUADRANTS:
            expected[qq] += uni[qq] / n

    obs = [observed[q] for q in QUADRANTS]
    exp = [expected[q] for q in QUADRANTS]
    chi2_stat = sum((o - e) ** 2 / e for o, e in zip(obs, exp) if e > 0)
    df = sum(1 for e in exp if e > 0) - 1
    p = float(chi2.sf(chi2_stat, df)) if df > 0 else float("nan")
    return {
        "n": used,
        "observed": dict(observed),
        "expected": {q: round(expected[q], 1) for q in QUADRANTS},
        "chi2": chi2_stat,
        "df": df,
        "p_value": p,
    }


def _fisher_weakening(events: list[ChangeEvent], lookup: dict) -> dict:
    from scipy.stats import binomtest

    in_w, n = 0, 0
    p_rates: list[float] = []
    for e in events:
        q = lookup["quad_at"].get((e.stock_id, e.change_date))
        uni = _universe_quad_counts(lookup, e.change_date)
        n_uni = sum(uni.values())
        if not q or n_uni == 0:
            continue
        n += 1
        p_rates.append(uni["weakening"] / n_uni)
        if q == "weakening":
            in_w += 1

    p0 = sum(p_rates) / len(p_rates) if p_rates else 0.0
    bt = binomtest(in_w, n, p0, alternative="greater") if n else None
    return {
        "n": n,
        "weakening_count": in_w,
        "weakening_rate": in_w / n if n else 0.0,
        "expected_rate": p0,
        "p_value": float(bt.pvalue) if bt else float("nan"),
    }


def _delta_window(
    lookup: dict,
    meta: dict,
    sid: str,
    center: str,
    n: int,
    *,
    before: bool,
) -> tuple[float | None, float | None]:
    days = meta["trading_days"]
    idx = meta["day_index"](center)
    if before:
        i0, i1 = idx - n, idx
    else:
        i0, i1 = idx, idx + n
    if i0 < 0 or i1 >= len(days) or i1 <= i0:
        return None, None
    d0, d1 = days[i0], days[i1]
    rs0 = lookup["rs_at"].get((sid, d0))
    rs1 = lookup["rs_at"].get((sid, d1))
    m0 = lookup["mom_at"].get((sid, d0))
    m1 = lookup["mom_at"].get((sid, d1))
    if None in (rs0, rs1, m0, m1):
        return None, None
    return rs1 - rs0, m1 - m0


def _random_control_deltas(
    lookup: dict,
    meta: dict,
    event: ChangeEvent,
    n: int,
    *,
    before: bool,
    exclude_ids: set[str],
    rng: random.Random,
    pool: list[str] | None = None,
) -> tuple[float, float] | None:
    cands = pool or [
        sid
        for sid in lookup["universe_by_date"].get(event.change_date, [])
        if sid not in exclude_ids
    ]
    cands = [sid for sid in cands if _delta_window(lookup, meta, sid, event.change_date, n, before=before)[0] is not None]
    if not cands:
        return None
    sid = rng.choice(cands)
    return _delta_window(lookup, meta, sid, event.change_date, n, before=before)


def _compare_deltas(
    events: list[ChangeEvent],
    lookup: dict,
    meta: dict,
    *,
    n: int,
    exclude_ids: set[str],
    seed: int = 42,
) -> dict:
    from scipy.stats import mannwhitneyu, ttest_ind

    rng = random.Random(seed)
    changed_ids = {e.stock_id for e in events}
    exclude = exclude_ids | changed_ids

    for label, before in [("before", True), ("after", False)]:
        pass

    results = {}
    for window_name, before in [("pre", True), ("post", False)]:
        evt_rs, evt_mom = [], []
        ctl_rs, ctl_mom = [], []
        for e in events:
            d = _delta_window(lookup, meta, e.stock_id, e.change_date, n, before=before)
            if d[0] is None:
                continue
            evt_rs.append(d[0])
            evt_mom.append(d[1])
            for _ in range(5):
                cd = _random_control_deltas(lookup, meta, e, n, before=before, exclude_ids=exclude, rng=rng)
                if cd:
                    ctl_rs.append(cd[0])
                    ctl_mom.append(cd[1])
        if len(evt_rs) < 5:
            results[window_name] = {"n_event": len(evt_rs), "note": "樣本不足"}
            continue
        t_rs = ttest_ind(evt_rs, ctl_rs, equal_var=False)
        t_mom = ttest_ind(evt_mom, ctl_mom, equal_var=False)
        u_rs = mannwhitneyu(evt_rs, ctl_rs, alternative="two-sided")
        u_mom = mannwhitneyu(evt_mom, ctl_mom, alternative="two-sided")
        results[window_name] = {
            "n_event": len(evt_rs),
            "n_control": len(ctl_rs),
            "event_mean_dRS": sum(evt_rs) / len(evt_rs),
            "control_mean_dRS": sum(ctl_rs) / len(ctl_rs),
            "event_mean_dMom": sum(evt_mom) / len(evt_mom),
            "control_mean_dMom": sum(ctl_mom) / len(ctl_mom),
            "ttest_dRS_p": float(t_rs.pvalue),
            "ttest_dMom_p": float(t_mom.pvalue),
            "mwu_dRS_p": float(u_rs.pvalue),
            "mwu_dMom_p": float(u_mom.pvalue),
        }
    return results


def _event_base_row(e: ChangeEvent) -> dict:
    return {
        "change_date": e.change_date,
        "prev_date": e.prev_date,
        "stock_id": e.stock_id,
        "stock_name": e.stock_name,
        "action": e.action,
        "weight_delta": e.weight_delta,
    }


def _enrich_jia_ma_rows(
    events: list[ChangeEvent],
    lookup: dict,
    meta: dict,
    *,
    n: int,
    exclude_ids: set[str],
) -> list[dict]:
    rows: list[dict] = []
    for e in events:
        row = _event_base_row(e)
        q = lookup["quad_at"].get((e.stock_id, e.change_date))
        rs = lookup["rs_at"].get((e.stock_id, e.change_date))
        mom = lookup["mom_at"].get((e.stock_id, e.change_date))
        uni = _universe_quad_counts(lookup, e.change_date)
        n_uni = sum(uni.values())
        pre = _delta_window(lookup, meta, e.stock_id, e.change_date, n, before=True)
        post = _delta_window(lookup, meta, e.stock_id, e.change_date, n, before=False)
        row.update(
            {
                "quadrant": q or "",
                "rs_ratio": rs,
                "rs_momentum": mom,
                "universe_n": n_uni,
                "universe_weakening_pct": (uni["weakening"] / n_uni * 100) if n_uni else None,
                "pre_dRS": pre[0],
                "pre_dMom": pre[1],
                "post_dRS": post[0],
                "post_dMom": post[1],
                "in_exclude_list": e.stock_id in exclude_ids,
            }
        )
        rows.append(row)
    return rows


def _write_csv(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _save_artifacts(
    *,
    data_dir: Path,
    db_path: Path,
    params: dict,
    all_events: list[ChangeEvent],
    jia_ma_enriched: list[dict],
    chi2_all: dict,
    chi2_ex2327: dict,
    fisher_all: dict,
    fisher_ex2327: dict,
    deltas: dict,
    deltas_ex: dict,
) -> dict[str, str]:
    data_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, str] = {}

    all_rows = [_event_base_row(e) for e in all_events]
    all_csv = data_dir / "events_all.csv"
    _write_csv(
        all_csv,
        all_rows,
        ["change_date", "prev_date", "stock_id", "stock_name", "action", "weight_delta"],
    )
    paths["events_all_csv"] = str(all_csv.relative_to(PROJECT_ROOT))

    jia_csv = data_dir / "events_jia_ma_enriched.csv"
    jia_fields = [
        "change_date",
        "prev_date",
        "stock_id",
        "stock_name",
        "action",
        "weight_delta",
        "quadrant",
        "rs_ratio",
        "rs_momentum",
        "universe_n",
        "universe_weakening_pct",
        "pre_dRS",
        "pre_dMom",
        "post_dRS",
        "post_dMom",
        "in_exclude_list",
    ]
    _write_csv(jia_csv, jia_ma_enriched, jia_fields)
    paths["events_jia_ma_enriched_csv"] = str(jia_csv.relative_to(PROJECT_ROOT))

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "raw_data": {
            "db_path": str(db_path),
            "etf_holdings_table": "etf_holdings",
            "daily_bars_table": "daily_bars (via load_price_panels)",
        },
        "params": params,
        "counts": {
            "all_events": len(all_events),
            "jia_ma_events": len(jia_ma_enriched),
            "jia_ma_with_quadrant": sum(1 for r in jia_ma_enriched if r.get("quadrant")),
        },
        "chi2_all": chi2_all,
        "chi2_excluded": chi2_ex2327,
        "weakening_binomial_all": fisher_all,
        "weakening_binomial_excluded": fisher_ex2327,
        "delta_tests_all": deltas,
        "delta_tests_excluded": deltas_ex,
        "artifacts": paths,
    }
    summary_path = data_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    paths["summary_json"] = str(summary_path.relative_to(PROJECT_ROOT))
    summary["artifacts"] = paths
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return paths


def _render_md(
    *,
    start_date: str,
    etf_code: str,
    all_events: list[ChangeEvent],
    jia_ma: list[ChangeEvent],
    chi2_all: dict,
    chi2_ex2327: dict,
    fisher_all: dict,
    fisher_ex2327: dict,
    deltas: dict,
    deltas_ex: dict,
    n_window: int,
    artifact_paths: dict[str, str] | None = None,
) -> str:
    lines = [
        f"# {etf_code} 持股變動 × RRG 統計檢定",
        "",
        f"- 期間：{start_date} 起 · 持股快照連續比對",
        f"- 全部變動事件：**{len(all_events)}** 筆",
        f"- 加碼事件：**{len(jia_ma)}** 筆",
        f"- 視窗 N = **{n_window}** 交易日（加碼日前/後 ΔRS、ΔMom）",
        "",
    ]
    if artifact_paths:
        lines.extend(
            [
                "## 資料產物",
                "",
                "- 原始持股：`data/stocks.db` → `etf_holdings`（00981A 快照）",
                "- 原始行情：同 DB → `daily_bars`（RRG 計算用）",
                f"- 全部變動事件：`{artifact_paths['events_all_csv']}`",
                f"- 加碼事件＋RRG/ΔRS/ΔMom：`{artifact_paths['events_jia_ma_enriched_csv']}`",
                f"- 統計摘要 JSON：`{artifact_paths['summary_json']}`",
                "",
            ]
        )
    lines.extend(
        [
        "## 1. 加碼日象限 vs Universe（χ² 適合度）",
        "",
        "期望比例 = 各加碼當日 Universe 象限占比加總。",
        "",
        ]
    )

    def _chi_block(title: str, c: dict) -> None:
        lines.extend(
            [
                f"### {title}（n={c['n']}）",
                "",
                "| 象限 | 觀測 | 期望 |",
                "|------|------|------|",
            ]
        )
        for q in QUADRANTS:
            lines.append(
                f"| {QUAD_ZH[q]} | {c['observed'].get(q, 0)} | {c['expected'].get(q, 0)} |"
            )
        lines.extend(
            [
                "",
                f"χ² = **{c['chi2']:.2f}**（df={c['df']}）· p = **{c['p_value']:.4f}**",
                "",
            ]
        )

    _chi_block("全樣本", chi2_all)
    _chi_block("排除 2327 後", chi2_ex2327)

    lines.extend(["## 2. Weakening 超配（二項檢定 · 單尾）", ""])
    for title, f in [("全樣本", fisher_all), ("排除 2327", fisher_ex2327)]:
        lines.extend(
            [
                f"### {title}（n={f['n']}）",
                "",
                f"- 加碼落在 Weakening：**{f['weakening_count']}/{f['n']}** "
                f"（{100 * f['weakening_rate']:.1f}%）",
                f"- Universe 期望占比：**{100 * f['expected_rate']:.1f}%**",
                f"- 二項檢定（Weakening 超配）p = **{f['p_value']:.4f}**",
                "",
            ]
        )

    lines.extend([f"## 3. 加碼前後 {n_window} 日 ΔRS / ΔMom vs 隨機 Universe 對照", ""])
    lines.append("對照組：同一加碼日從 Universe 隨機抽樣（每事件 5 次），排除當期所有變動標的。")
    lines.append("")

    def _delta_block(title: str, d: dict) -> None:
        lines.append(f"### {title}")
        lines.append("")
        for w in ("pre", "post"):
            r = d.get(w, {})
            if "note" in r:
                lines.append(f"- **{w}**：{r['note']}")
                continue
            wlabel = f"前 {n_window} 日" if w == "pre" else f"後 {n_window} 日"
            lines.append(f"**{wlabel}**（事件 n={r['n_event']}，對照 n={r['n_control']}）")
            lines.append(
                f"- ΔRS：事件 {r['event_mean_dRS']:+.2f} vs 對照 {r['control_mean_dRS']:+.2f} · "
                f"t-test p={r['ttest_dRS_p']:.3f} · MWU p={r['mwu_dRS_p']:.3f}"
            )
            lines.append(
                f"- ΔMom：事件 {r['event_mean_dMom']:+.2f} vs 對照 {r['control_mean_dMom']:+.2f} · "
                f"t-test p={r['ttest_dMom_p']:.3f} · MWU p={r['mwu_dMom_p']:.3f}"
            )
            lines.append("")

    _delta_block("全樣本加碼", deltas)
    _delta_block("排除 2327 加碼", deltas_ex)

    lines.extend(
        [
            "## 解讀備註",
            "",
            "- p < 0.05 僅代表「此樣本期間」統計偏離，非因果。",
            "- 加碼事件同日可能多檔，非完全獨立。",
            "- Universe 為 ETF 持股聯集，與 00981A 單一 basket 不完全相同。",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))
    from project_config import DEFAULT_ETF_CODES, parse_etf_codes

    parser = argparse.ArgumentParser(description="00981A holdings change × RRG audit")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--etf-code", default="00981A")
    parser.add_argument("--start", default="2025-05-01")
    parser.add_argument("--length", type=int, default=20)
    parser.add_argument("--window", type=int, default=3, help="前/後 N 交易日")
    parser.add_argument("--exclude", default="2327", help="排除標的（逗號分隔）")
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=DATA_ARTIFACTS,
        help="結構化資料輸出目錄（CSV/JSON）",
    )
    parser.add_argument("--no-save-data", action="store_true", help="僅輸出 Markdown")
    args = parser.parse_args()

    exclude_ids = {s.strip() for s in args.exclude.split(",") if s.strip()}
    etf_codes = parse_etf_codes(",".join(DEFAULT_ETF_CODES))

    conn = connect(args.db)
    try:
        all_events = _collect_events(conn, args.etf_code, args.start)
        lookup, meta = _load_rrg_lookup(conn, etf_codes, args.length)
    finally:
        conn.close()

    jia_ma = [e for e in all_events if e.action == "加码"]
    jia_ma_ex = [e for e in jia_ma if e.stock_id not in exclude_ids]

    chi2_all = _chi2_quadrant(jia_ma, lookup)
    chi2_ex2327 = _chi2_quadrant(jia_ma_ex, lookup)
    fisher_all = _fisher_weakening(jia_ma, lookup)
    fisher_ex2327 = _fisher_weakening(jia_ma_ex, lookup)
    deltas = _compare_deltas(jia_ma, lookup, meta, n=args.window, exclude_ids=set())
    deltas_ex = _compare_deltas(jia_ma_ex, lookup, meta, n=args.window, exclude_ids=exclude_ids)

    artifact_paths: dict[str, str] | None = None
    if not args.no_save_data:
        jia_ma_enriched = _enrich_jia_ma_rows(
            jia_ma, lookup, meta, n=args.window, exclude_ids=exclude_ids
        )
        artifact_paths = _save_artifacts(
            data_dir=args.data_dir,
            db_path=args.db,
            params={
                "etf_code": args.etf_code,
                "start_date": args.start,
                "rrg_length": args.length,
                "window_days": args.window,
                "exclude_ids": sorted(exclude_ids),
            },
            all_events=all_events,
            jia_ma_enriched=jia_ma_enriched,
            chi2_all=chi2_all,
            chi2_ex2327=chi2_ex2327,
            fisher_all=fisher_all,
            fisher_ex2327=fisher_ex2327,
            deltas=deltas,
            deltas_ex=deltas_ex,
        )

    md = _render_md(
        start_date=args.start,
        etf_code=args.etf_code,
        all_events=all_events,
        jia_ma=jia_ma,
        chi2_all=chi2_all,
        chi2_ex2327=chi2_ex2327,
        fisher_all=fisher_all,
        fisher_ex2327=fisher_ex2327,
        deltas=deltas,
        deltas_ex=deltas_ex,
        n_window=args.window,
        artifact_paths=artifact_paths,
    )

    stamp = date.today().strftime("%Y%m%d")
    out = args.output or REPORTS / f"{stamp}_{args.etf_code.lower()}_holdings_rrg_audit.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    print(md)
    print(f"Wrote {out}")
    if artifact_paths:
        print("Saved data artifacts:")
        for label, rel in artifact_paths.items():
            print(f"  {label}: {PROJECT_ROOT / rel}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
