"""VCP Pivot Gate / Coil Close — 盤中 daily brief（收盤 DB 或 FinMind tick 盤中重算）。"""

from __future__ import annotations

import os
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Any

from finmind_client import fetch_tick_snapshots
from project_config import DEFAULT_ETF_CODES
from project_dotenv import load_project_dotenv
from research.backtest.chunge_funnel_backtest import (
    VCP_COIL_CLOSE,
    VCP_PIVOT_GATE,
    VCP_COIL_CLOSE_VARIANT,
    VCP_PIVOT_GATE_VARIANT,
    ChungeCandidate,
    build_chunge_candidates_calendar,
)
from vcp_funnel_screen import FUNNEL_MODEL_IDS, MODEL_ID, VcpFunnelEval, run_vcp_funnel_screen
from market_benchmark import latest_trading_date
from report_paths import REPORTS_DIR
from stock_db import connect, load_etf_constituent_watchlist, load_vcp_screen_v2_for_date

BENCH_TICK_IDS = ("TAIEX", "IX0001")

SPEC_REGISTRY: dict[str, dict[str, Any]] = {
    "pivot_gate": VCP_PIVOT_GATE,
    "coil_close": VCP_COIL_CLOSE,
}
SPEC_TITLES: dict[str, str] = {
    "pivot_gate": "VCP Pivot Gate",
    "coil_close": "VCP Coil Close",
}
SPEC_VARIANTS: dict[str, str] = {
    "pivot_gate": VCP_PIVOT_GATE_VARIANT,
    "coil_close": VCP_COIL_CLOSE_VARIANT,
}
ENTRY_HINTS: dict[str, str] = {
    "pivot_gate": "breakout_close · close≥pivot · 最長等 10 交易日",
    "coil_close": "訊號日 close · 可低於 pivot",
}


def _env_csv(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    return tuple(x.strip() for x in raw.split(",") if x.strip())


def _tick_float(row: dict, *keys: str) -> float | None:
    for key in keys:
        val = row.get(key)
        if val is not None:
            try:
                f = float(val)
                if f > 0:
                    return f
            except (TypeError, ValueError):
                continue
    return None


def _tick_stock_id(row: dict) -> str:
    for key in ("stock_id", "StockID", "data_id"):
        val = row.get(key)
        if val:
            return str(val).strip()
    return ""


def _tick_map(rows: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for row in rows:
        sid = _tick_stock_id(row)
        if sid and _tick_float(row, "close", "Close", "deal_price", "price") is not None:
            out[sid] = row
    return out


def _filter_evals_to_candidates(
    evals: list[VcpFunnelEval],
    spec: dict[str, Any],
) -> list[ChungeCandidate]:
    candidates: list[ChungeCandidate] = []
    for e in evals:
        if e.layers_passed < 7:
            continue
        score = float(e.funnel_score or 0.0)
        if score < float(spec["min_composite"]):
            continue
        ready = bool(e.extras.get("entry_ready"))
        if spec.get("entry_ready_only") and not ready:
            continue
        state = str(e.extras.get("execution_state") or "")
        if state not in tuple(spec["execution_states"]):
            continue
        pivot = e.pivot_price
        if spec.get("require_pivot") and (pivot is None or pivot <= 0):
            continue
        dist = e.dist_pivot_pct
        min_dist = spec.get("min_dist_pivot_pct")
        max_dist = spec.get("max_dist_pivot_pct")
        if min_dist is not None and dist is not None and dist < min_dist:
            continue
        if max_dist is not None and dist is not None and dist > max_dist:
            continue
        candidates.append(
            ChungeCandidate(
                stock_id=e.stock_id,
                stock_name=e.stock_name,
                composite_score=score,
                execution_state=state,
                entry_ready=ready,
                pivot_price=pivot,
                stop_loss=e.stop_loss,
                distance_from_pivot_pct=dist,
            )
        )
    candidates.sort(key=lambda c: (-c.composite_score, c.stock_id))
    return candidates


def fetch_session_ticks(
    conn: sqlite3.Connection,
    *,
    session_date: str,
    etf_codes: tuple[str, ...] | None = None,
) -> tuple[dict[str, dict], float | None, int, int, str | None, str | None]:
    codes = etf_codes or _env_csv("VCP_FUNNEL_ETF_CODES", DEFAULT_ETF_CODES)
    universe = [w["stock_id"] for w in load_etf_constituent_watchlist(conn, codes)]
    tick_rows, tick_error = fetch_tick_snapshots(universe)
    session_ticks = _tick_map(tick_rows)

    bench_rows, bench_err = fetch_tick_snapshots(list(BENCH_TICK_IDS))
    bench_ticks = _tick_map(bench_rows)
    bench_px: float | None = None
    for bid in BENCH_TICK_IDS:
        row = bench_ticks.get(bid)
        if row is not None:
            bench_px = _tick_float(row, "close", "Close", "deal_price", "price")
            if bench_px is not None:
                break

    last_close = latest_trading_date(conn, on_or_before=session_date)
    err = tick_error or (bench_err if bench_px is None and bench_err else None)
    return session_ticks, bench_px, len(session_ticks), len(universe), last_close, err


def run_intraday_funnel_screen(
    conn: sqlite3.Connection,
    session_date: str,
    *,
    etf_codes: tuple[str, ...] | None = None,
    persist: bool = False,
) -> tuple[list[VcpFunnelEval], dict[str, Any]]:
    session_ticks, bench_px, tick_n, uni_n, last_close, tick_error = fetch_session_ticks(
        conn, session_date=session_date, etf_codes=etf_codes
    )
    codes = etf_codes or _env_csv("VCP_FUNNEL_ETF_CODES", DEFAULT_ETF_CODES)
    _as_of, evals, layers, _cfg = run_vcp_funnel_screen(
        conn,
        etf_codes=codes,
        as_of_date=session_date,
        persist=persist,
        session_ticks=session_ticks,
        bench_session_price=bench_px,
    )
    meta = {
        "intraday": True,
        "tick_stock_n": tick_n,
        "universe_n": uni_n,
        "last_close_date": last_close,
        "tick_error": tick_error,
        "bench_tick_ok": bench_px is not None,
        "screen_as_of": session_date,
        "l7_count": layers.get("L7", 0),
        "persisted": persist,
    }
    return evals, meta


def _close_screen_ready(conn: sqlite3.Connection, as_of: str, *, min_bars: int = 50) -> bool:
    row = conn.execute(
        """
        SELECT COUNT(DISTINCT stock_id) AS n
        FROM stock_daily_bars
        WHERE source = 'finmind' AND trade_date = ?
        """,
        (as_of,),
    ).fetchone()
    return bool(row and int(row["n"] or 0) >= min_bars)


def run_close_funnel_screen(
    conn: sqlite3.Connection,
    *,
    as_of_date: str | None = None,
) -> tuple[str | None, dict[str, int]]:
    ref = as_of_date or latest_trading_date(conn) or date.today().isoformat()
    if not _close_screen_ready(conn, ref):
        return None, {}
    as_of, _evals, layer_counts, _cfg = run_vcp_funnel_screen(
        conn,
        as_of_date=ref,
        persist=True,
        replace_day=True,
    )
    return as_of or None, layer_counts


def run_intraday_cycle(
    conn: sqlite3.Connection,
    *,
    as_of_date: str | None = None,
    persist: bool = True,
) -> tuple[list[Path], dict[str, Any]]:
    ref = as_of_date or date.today().isoformat()
    evals, meta = run_intraday_funnel_screen(conn, ref, persist=persist)
    paths = write_spec_briefs(
        conn,
        as_of_date=ref,
        intraday=True,
        intraday_evals=evals,
        intraday_meta=meta,
    )
    return paths, meta


def run_close_cycle(
    conn: sqlite3.Connection,
    *,
    as_of_date: str | None = None,
) -> tuple[list[Path], str | None]:
    ref = as_of_date or latest_trading_date(conn) or date.today().isoformat()
    as_of, layer_counts = run_close_funnel_screen(conn, as_of_date=ref)
    if not as_of:
        return [], None
    paths = write_spec_briefs(conn, as_of_date=as_of, intraday=False)
    print(
        f"VCP close: screen_as_of={as_of} L1={layer_counts.get('L1', 0)} "
        f"L7={layer_counts.get('L7', 0)}"
    )
    return paths, as_of


def resolve_spec_key(spec_key: str) -> str:
    key = spec_key.strip().lower().replace("-", "_")
    aliases = {
        "vcp_pivot_gate": "pivot_gate",
        "vcp_coil_close": "coil_close",
        VCP_PIVOT_GATE_VARIANT.replace("-", "_"): "pivot_gate",
        VCP_COIL_CLOSE_VARIANT.replace("-", "_"): "coil_close",
    }
    return aliases.get(key, key)


def latest_screen_date(conn: sqlite3.Connection, *, on_or_before: str) -> str | None:
    placeholders = ",".join("?" * len(FUNNEL_MODEL_IDS))
    row = conn.execute(
        f"""
        SELECT MAX(as_of_date) AS d
        FROM vcp_screen_scores_v2
        WHERE model_id IN ({placeholders}) AND as_of_date <= ?
        """,
        (*FUNNEL_MODEL_IDS, on_or_before),
    ).fetchone()
    if not row or not row["d"]:
        return None
    return str(row["d"])


def load_spec_candidates(
    conn: sqlite3.Connection,
    signal_day: str,
    spec_key: str,
    *,
    top_n: int = 15,
    intraday: bool = False,
    etf_codes: tuple[str, ...] | None = None,
    intraday_evals: list[VcpFunnelEval] | None = None,
    intraday_meta: dict[str, Any] | None = None,
) -> tuple[str | None, list[ChungeCandidate], dict[str, Any] | None]:
    key = resolve_spec_key(spec_key)
    spec = SPEC_REGISTRY.get(key)
    if spec is None:
        raise KeyError(f"unknown funnel spec: {spec_key}")
    meta: dict[str, Any] | None = None
    if intraday:
        evals = intraday_evals
        meta = intraday_meta
        if evals is None or meta is None:
            evals, meta = run_intraday_funnel_screen(conn, signal_day, etf_codes=etf_codes)
        cands = _filter_evals_to_candidates(evals, spec)[:top_n]
        last_close = meta.get("last_close_date") if meta else None
        return last_close, cands, meta

    screen_day = latest_screen_date(conn, on_or_before=signal_day) or signal_day
    by_date = build_chunge_candidates_calendar(
        conn,
        [screen_day],
        model_id=MODEL_ID,
        min_composite=float(spec["min_composite"]),
        execution_states=tuple(spec["execution_states"]),
        entry_ready_only=bool(spec["entry_ready_only"]),
        require_pivot=bool(spec["require_pivot"]),
        min_dist_pivot_pct=spec.get("min_dist_pivot_pct"),
        max_dist_pivot_pct=spec.get("max_dist_pivot_pct"),
    )
    cands = by_date.get(screen_day, [])
    cands.sort(key=lambda c: (-c.composite_score, c.stock_id))
    return screen_day, cands[:top_n], meta


def build_spec_gate_summary(
    conn: sqlite3.Connection,
    signal_day: str,
    spec_key: str,
) -> tuple[tuple[tuple[str, str, int], ...], tuple[str, ...]]:
    key = resolve_spec_key(spec_key)
    spec = SPEC_REGISTRY[key]
    screen_day = latest_screen_date(conn, on_or_before=signal_day)
    if not screen_day:
        return (("screen", "vcp-funnel DB", 0),), ("尚無 vcp_screen_scores_v2（vcp-funnel）",)
    all_rows = load_vcp_screen_v2_for_date(
        conn, screen_day, model_id=MODEL_ID, min_score=0.0
    )
    scored = [r for r in all_rows if float(r["composite_score"] or 0) >= spec["min_composite"]]
    _, filtered, _ = load_spec_candidates(conn, signal_day, key, top_n=999)
    layers = (
        ("screen", f"as_of {screen_day}", len(all_rows)),
        (f"score≥{spec['min_composite']:.0f}", "composite", len(scored)),
        ("spec", SPEC_TITLES[key], len(filtered)),
    )
    notes = (f"variant `{SPEC_VARIANTS[key]}` · {ENTRY_HINTS[key]}",)
    return layers, notes


def build_spec_brief_markdown(
    conn: sqlite3.Connection,
    *,
    spec_key: str,
    as_of_date: str | None = None,
    top_n: int = 15,
    intraday: bool = False,
    etf_codes: tuple[str, ...] | None = None,
    intraday_evals: list[VcpFunnelEval] | None = None,
    intraday_meta: dict[str, Any] | None = None,
) -> str:
    key = resolve_spec_key(spec_key)
    ref = as_of_date or date.today().isoformat()
    title = SPEC_TITLES[key]
    variant = SPEC_VARIANTS[key]
    screen_day, cands, meta = load_spec_candidates(
        conn,
        ref,
        key,
        top_n=top_n,
        intraday=intraday,
        etf_codes=etf_codes,
        intraday_evals=intraday_evals,
        intraday_meta=intraday_meta,
    )
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        f"# {title} · daily brief · {ref}",
        "",
    ]
    if intraday and meta:
        bench_note = "有" if meta.get("bench_tick_ok") else "沿用昨收"
        tick_warn = meta.get("tick_error")
        warn_line = f" · tick 警告：{tick_warn}" if tick_warn else ""
        lines.extend(
            [
                line
                for line in (
                    f"> 產出 {now} · **盤中預估** · FinMind tick · model `{MODEL_ID}` · variant `{variant}` · **非交易主檔**",
                    f"> 上一收盤 K 線 **{meta.get('last_close_date') or screen_day or '—'}** + 今日 tick 合成 provisional close",
                    f"> tick 覆蓋：**{meta.get('tick_stock_n', 0)} / {meta.get('universe_n', 0)}** · 大盤 tick：{bench_note}{warn_line}",
                    (
                        f"> 盤中 screen 已寫入 DB · as_of **{meta.get('screen_as_of') or ref}** · L7={meta.get('l7_count', 0)}"
                        if meta.get("persisted")
                        else None
                    ),
                    "> ⚠️ **候選名單，非最終訊號** — 13:30 收盤後可能翻轉；**16:30 收盤 screen** 覆寫 DB 並更新 brief。",
                    f"> 進場規則：**{ENTRY_HINTS[key]}**",
                    "",
                )
                if line is not None
            ]
        )
    else:
        lines.extend(
            [
                f"> 產出 {now} · **收盤確認** · model `{MODEL_ID}` · variant `{variant}` · **非交易主檔**",
                f"> screen as_of **{screen_day or '—'}**（收盤 K 線 screen 已寫入 DB）",
                f"> 進場規則：**{ENTRY_HINTS[key]}**",
                "",
            ]
        )
    lines.extend(
        [
            f"## 候選 Top {top_n}（near pivot −8%～+5% · Pre/Breakout/Early · composite≥45）",
            "",
        ]
    )
    if not cands:
        empty = (
            "_盤中預估：目前無符合 spec 的候選。_"
            if intraday
            else "_今日無符合 spec 的候選（需先跑 vcp_funnel_screen --run 寫入 DB）_"
        )
        lines.append(empty)
    else:
        lines.extend(
            [
                "| 代號 | 名稱 | composite | state | pivot | dist% | stop |",
                "|------|------|-----------|-------|-------|-------|------|",
            ]
        )
        for c in cands:
            pivot_s = f"{c.pivot_price:.2f}" if c.pivot_price else "—"
            dist_s = f"{c.distance_from_pivot_pct:.1f}" if c.distance_from_pivot_pct is not None else "—"
            stop_s = f"{c.stop_loss:.2f}" if c.stop_loss else "—"
            lines.append(
                f"| {c.stock_id} | {c.stock_name} | {c.composite_score:.1f} | "
                f"{c.execution_state} | {pivot_s} | {dist_s} | {stop_s} |"
            )
    lines.extend(["", "---", f"模組：`vcp_funnel_specs_daily.py` · backtest variant `{variant}`", ""])
    return "\n".join(lines)


def write_spec_briefs(
    conn: sqlite3.Connection,
    *,
    as_of_date: str | None = None,
    reports_dir: Path = REPORTS_DIR,
    spec_keys: tuple[str, ...] = ("pivot_gate", "coil_close"),
    intraday: bool = False,
    etf_codes: tuple[str, ...] | None = None,
) -> list[Path]:
    ref = as_of_date or date.today().isoformat()
    stamp = ref.replace("-", "")
    reports_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    sections: list[str] = []
    slug_map = {"pivot_gate": "vcp_pivot_gate", "coil_close": "vcp_coil_close"}
    intraday_evals: list[VcpFunnelEval] | None = None
    intraday_meta: dict[str, Any] | None = None
    if intraday:
        intraday_evals, intraday_meta = run_intraday_funnel_screen(
            conn, ref, etf_codes=etf_codes, persist=False
        )
    for spec_key in spec_keys:
        md = build_spec_brief_markdown(
            conn,
            spec_key=spec_key,
            as_of_date=ref,
            intraday=intraday,
            etf_codes=etf_codes,
            intraday_evals=intraday_evals,
            intraday_meta=intraday_meta,
        )
        slug = slug_map.get(spec_key, spec_key)
        dated = reports_dir / f"{stamp}_{slug}_daily_brief.md"
        latest = reports_dir / f"{slug}_daily_brief.md"
        dated.write_text(md, encoding="utf-8")
        latest.write_text(md, encoding="utf-8")
        written.extend([dated, latest])
        sections.append(md)
    combined = "\n\n---\n\n".join(sections)
    combo_dated = reports_dir / f"{stamp}_vcp_funnel_specs_daily_brief.md"
    combo_latest = reports_dir / "vcp_funnel_specs_daily_brief.md"
    combo_dated.write_text(combined, encoding="utf-8")
    combo_latest.write_text(combined, encoding="utf-8")
    written.extend([combo_dated, combo_latest])
    return written


def main(argv: list[str] | None = None) -> int:
    import argparse

    load_project_dotenv()
    parser = argparse.ArgumentParser(description="VCP Pivot Gate / Coil Close daily brief")
    parser.add_argument("--as-of", default=None, help="session / 參考日 YYYY-MM-DD（預設今日）")
    parser.add_argument(
        "--intraday",
        action="store_true",
        help="FinMind tick 盤中重算（上一收盤 K + 今日 tick provisional close）",
    )
    parser.add_argument(
        "--close",
        action="store_true",
        help="收盤 screen 寫 DB + 收盤確認 brief（需當日 stock_daily_bars）",
    )
    parser.add_argument(
        "--persist-intraday",
        action="store_true",
        help="與 --intraday 併用：盤中 screen 寫入 vcp_screen_scores_v2",
    )
    parser.add_argument("--db", type=Path, default=None)
    args = parser.parse_args(argv)

    conn = connect(args.db) if args.db else connect()
    try:
        if args.close:
            paths, as_of = run_close_cycle(conn, as_of_date=args.as_of)
            if not paths:
                print(f"VCP close: skipped（as_of={args.as_of or 'auto'} 無足夠當日 K 線）")
                return 1
        elif args.intraday and args.persist_intraday:
            paths, meta = run_intraday_cycle(conn, as_of_date=args.as_of, persist=True)
            print(
                f"VCP intraday: screen_as_of={meta.get('screen_as_of')} "
                f"L7={meta.get('l7_count')} tick={meta.get('tick_stock_n')}/{meta.get('universe_n')}"
            )
        else:
            paths = write_spec_briefs(
                conn,
                as_of_date=args.as_of,
                intraday=args.intraday,
            )
    finally:
        conn.close()
    for p in paths:
        print(f"Wrote {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
