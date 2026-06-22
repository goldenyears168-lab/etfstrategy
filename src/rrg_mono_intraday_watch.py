"""RRG mono 盤中收盤前預警（13:00）：FinMind tick 估算 D4 軌跡，不更新槽位。"""

from __future__ import annotations

import os
import sqlite3
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from finmind_client import fetch_tick_snapshots
from research.backtest.finpilot_local_backtest import load_price_panels
from market_benchmark import load_benchmark_close
from project_config import DEFAULT_ETF_CODES
from project_dotenv import load_project_dotenv
from rrg_mono_daily_brief import (
    EXECUTION_DETAIL_ZH,
    EXECUTION_RULE_ZH,
    MAX_SLOTS,
    ScanRow,
    TOP_N,
    _fmt_pct,
    load_slot_state,
    scan_rows_from_panels,
)
from report_paths import REPORTS_DIR
from stock_db import DEFAULT_DB_PATH, PROJECT_ROOT, connect, load_etf_constituent_watchlist

BENCH_TICK_IDS = ("TAIEX", "IX0001")


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


def _tick_map(rows: list[dict]) -> dict[str, float]:
    out: dict[str, float] = {}
    for row in rows:
        sid = _tick_stock_id(row)
        px = _tick_float(row, "close", "Close", "deal_price", "price")
        if sid and px is not None:
            out[sid] = px
    return out


def _session_date() -> str:
    return date.today().isoformat()


def _build_provisional_panels(
    close: pd.DataFrame,
    bench: pd.Series,
    session_date: str,
    stock_ticks: dict[str, float],
    bench_tick: float | None,
) -> tuple[pd.DataFrame, pd.Series, int, int]:
    """回傳 (close_panel, bench_series, tick_stock_n, universe_n)。"""
    out = close.copy()
    if session_date not in out.index:
        out.loc[session_date] = np.nan
    out = out.sort_index()

    tick_n = 0
    for sid, px in stock_ticks.items():
        if sid not in out.columns:
            continue
        out.at[session_date, sid] = px
        tick_n += 1

    bench_out = bench.reindex(out.index).astype(float).copy()
    if session_date not in bench_out.index:
        bench_out.loc[session_date] = np.nan
    if bench_tick is not None and bench_tick > 0:
        bench_out.at[session_date] = bench_tick
    elif session_date in bench_out.index and not np.isfinite(bench_out.at[session_date]):
        prev = bench_out.loc[:session_date].iloc[:-1].dropna()
        if not prev.empty:
            bench_out.at[session_date] = float(prev.iloc[-1])

    universe_n = len(out.columns)
    return out, bench_out.ffill(), tick_n, universe_n


def build_markdown(
    *,
    session_date: str,
    all_mono: list[ScanRow],
    fresh_mono: list[ScanRow],
    slots: list[dict],
    tick_error: str | None,
    tick_stock_n: int,
    universe_n: int,
    bench_tick_ok: bool,
) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    free = MAX_SLOTS - len(slots)
    lines = [
        f"# RRG mono 收盤前預警 · {session_date}",
        "",
        f"> 產出 {now} · **盤中預估** · 策略 mono + seg_last + 3 槽 + hold7（{EXECUTION_RULE_ZH}）",
        "> ⚠️ **候選名單，非最終訊號** — 13:30 收盤後數值可能翻轉；**16:40 收盤掃描**為準。",
        "",
    ]
    if tick_error:
        lines.extend([f"⚠ FinMind tick：`{tick_error}`", ""])
    lines.extend(
        [
            f"- tick 覆蓋：**{tick_stock_n} / {universe_n}** 檔 · 大盤 tick：{'有' if bench_tick_ok else '沿用昨收'}",
            f"- 目前空槽：**{free} / {MAX_SLOTS}**",
            "",
        ]
    )

    if fresh_mono:
        lines.extend(["## ★ mono fresh 候選（seg_last 排序）", ""])
        lines.append("| # | 代號 | 名稱 | seg_last | 位移 | 當日% | RV | MV |")
        lines.append("|---|------|------|----------|------|-------|----|----|")
        for i, r in enumerate(fresh_mono[:TOP_N], 1):
            lines.append(
                f"| {i} | {r.stock_id} | {r.stock_name} | {r.seg_last:.3f} | "
                f"{r.disp:.2f} | {_fmt_pct(r.daily_pct)} | "
                f"{r.rs_ratio:.1f} | {r.rs_momentum:.1f} |"
            )
        lines.append("")
        if free > 0:
            picks = fresh_mono[: min(free, TOP_N)]
            lines.append("### 收盤前可評估進場（若收盤仍成立）")
            for r in picks[:free]:
                lines.append(
                    f"- **{r.stock_id} {r.stock_name}** seg_last={r.seg_last:.3f} "
                    f"（D4 收盤進場 → D11 收盤出場）"
                )
            lines.append("")
    else:
        lines.extend(["## mono fresh 候選", "", "_目前盤中預估：無 mono fresh 候選。_", ""])

    lines.extend(["## 所有 mono 候選（含非 fresh）", ""])
    if not all_mono:
        lines.append("_目前盤中預估：無 mono 標的。_")
    else:
        lines.append("| # | 代號 | 名稱 | fr | seg_last | 位移 | 當日% |")
        lines.append("|---|------|------|----|----------|------|-------|")
        for i, r in enumerate(all_mono[:20], 1):
            fr = "★" if r.fresh else ""
            lines.append(
                f"| {i} | {r.stock_id} | {r.stock_name} | {fr} | {r.seg_last:.3f} | "
                f"{r.disp:.2f} | {_fmt_pct(r.daily_pct)} |"
            )
    lines.append("")

    if slots:
        lines.extend(["## 現有持倉（僅供對照，本 job 不變更）", ""])
        lines.append("| 槽 | 代號 | 名稱 | 進場(D4) | 出場(D11) |")
        lines.append("|---|------|------|----------|----------|")
        for p in sorted(slots, key=lambda x: int(x["slot"])):
            lines.append(
                f"| {int(p['slot']) + 1} | {p['stock_id']} | {p.get('stock_name', '')} | "
                f"{p.get('entry_date', '')} | {p.get('exit_date') or '—'} |"
            )
        lines.append("")

    lines.extend(["---", EXECUTION_DETAIL_ZH, ""])
    return "\n".join(lines)


def write_intraday_report(md: str, *, session_date: str) -> Path:
    stamp = session_date.replace("-", "")
    dated = REPORTS_DIR / f"{stamp}_rrg_mono_intraday_watch.md"
    latest = REPORTS_DIR / "rrg_mono_intraday_watch.md"
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    dated.write_text(md, encoding="utf-8")
    latest.write_text(md, encoding="utf-8")
    return dated


def run_intraday_watch(
    conn: sqlite3.Connection,
    *,
    session_date: str | None = None,
    etf_codes: tuple[str, ...] | None = None,
) -> tuple[Path | None, list[ScanRow], list[ScanRow], str | None]:
    if os.environ.get("RUN_RRG_MONO_INTRADAY_WATCH", "1").strip() in ("0", "false", "False"):
        return None, [], [], "RUN_RRG_MONO_INTRADAY_WATCH=0"

    session = session_date or _session_date()
    codes = etf_codes or _env_csv("RRG_MONO_ETF_CODES", DEFAULT_ETF_CODES)

    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn)
    universe = [w["stock_id"] for w in load_etf_constituent_watchlist(conn, codes)]

    tick_rows, tick_error = fetch_tick_snapshots(universe)
    stock_ticks = _tick_map(tick_rows)

    bench_rows, bench_err = fetch_tick_snapshots(list(BENCH_TICK_IDS))
    bench_ticks = _tick_map(bench_rows)
    bench_px = None
    for bid in BENCH_TICK_IDS:
        if bid in bench_ticks:
            bench_px = bench_ticks[bid]
            break
    if bench_px is None and bench_err and not tick_error:
        tick_error = bench_err

    close_prov, bench_prov, tick_n, uni_n = _build_provisional_panels(
        close, bench, session, stock_ticks, bench_px
    )

    if os.environ.get("RUN_RRG_UNIVERSE_SNAPSHOT", "1").strip() not in (
        "0",
        "false",
        "False",
    ):
        try:
            from rrg_universe_snapshot import persist_intraday_universe_from_panels
            from supabase_rrg_universe_sync import maybe_sync_rrg_universe_to_supabase

            n_rows = persist_intraday_universe_from_panels(
                conn,
                session_date=session,
                close_prov=close_prov,
                bench_prov=bench_prov,
                stock_ticks=stock_ticks,
                etf_codes=codes,
            )
            print(f"RRG universe intraday: session={session} rows={n_rows} tick={tick_n}")
            maybe_sync_rrg_universe_to_supabase(conn, session, "intraday")
        except Exception as exc:
            print(f"RRG universe intraday persist warn: {exc}")

    try:
        all_mono, fresh_mono = scan_rows_from_panels(
            conn, session, close_prov, bench_prov, etf_codes=codes
        )
    except RuntimeError as exc:
        md = build_markdown(
            session_date=session,
            all_mono=[],
            fresh_mono=[],
            slots=load_slot_state().get("slots", []),
            tick_error=str(exc),
            tick_stock_n=tick_n,
            universe_n=uni_n,
            bench_tick_ok=bench_px is not None,
        )
        path = write_intraday_report(md, session_date=session)
        return path, [], [], str(exc)

    slots = load_slot_state().get("slots", [])
    md = build_markdown(
        session_date=session,
        all_mono=all_mono,
        fresh_mono=fresh_mono,
        slots=slots,
        tick_error=tick_error,
        tick_stock_n=tick_n,
        universe_n=uni_n,
        bench_tick_ok=bench_px is not None,
    )
    path = write_intraday_report(md, session_date=session)
    return path, all_mono, fresh_mono, tick_error


def main() -> int:
    load_project_dotenv()
    conn = connect(DEFAULT_DB_PATH)
    try:
        path, _all_mono, fresh, err = run_intraday_watch(conn)
    finally:
        conn.close()

    if path is None:
        print("RRG mono intraday: skipped (RUN_RRG_MONO_INTRADAY_WATCH=0)")
        return 0

    print(f"RRG mono intraday: report={path} fresh={len(fresh)} tick_warn={err or '—'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
