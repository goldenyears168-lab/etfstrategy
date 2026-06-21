"""RRG mono + seg_last 每日掃描（3 槽 hold7；D4 收盤進場 / D11 收盤出場）。"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

from research.backtest.finpilot_local_backtest import load_price_panels
from flow_returns import trading_dates_after
from market_benchmark import load_benchmark_close
from project_config import DEFAULT_ETF_CODES
from rrg_rotation import classify_quadrant, compute_rrg_panel
from report_paths import REPORTS_DIR
from stock_db import (
    DEFAULT_DB_PATH,
    PROJECT_ROOT,
    connect,
    load_etf_constituent_watchlist,
)

REPORTS = REPORTS_DIR
STATE_PATH = PROJECT_ROOT / "data" / "rrg_mono_slots.json"

LENGTH = 20
LOOKBACK = 4
HOLD_DAYS = 7
MAX_SLOTS = 3
TOP_N = 10
EXECUTION_RULE_ZH = "D4 收盤進場 / D11 收盤出場"
EXECUTION_DETAIL_ZH = (
    "訊號：4 日軌跡最後一日（D4）收盤判定 mono fresh，以 D4 close 進場；"
    "出場：訊號日後第 7 個交易日收盤（D11 close，hold7）。"
)


@dataclass
class SlotPosition:
    slot: int
    stock_id: str
    stock_name: str
    entry_date: str
    exit_date: str
    seg_last: float
    disp: float


@dataclass
class ScanRow:
    stock_id: str
    stock_name: str
    fresh: bool
    mono: bool
    seg_last: float
    disp: float
    segs: list[float]
    quadrants: list[str]
    rs_ratio: float
    rs_momentum: float
    daily_pct: float | None


def _feat(
    rs_ratio,
    rs_mom,
    full_dates: list[str],
    si: int,
    sid: str,
    *,
    lb: int = LOOKBACK,
) -> dict[str, Any] | None:
    if si < lb - 1 or sid not in rs_ratio.columns:
        return None
    vals: list[tuple[float, float, str | None]] = []
    for j in range(lb):
        d = full_dates[si - lb + 1 + j]
        rv = float(rs_ratio.at[d, sid])
        mv = float(rs_mom.at[d, sid])
        if rv != rv or mv != mv:
            return None
        vals.append((rv, mv, classify_quadrant(rv, mv)))
    dr = vals[-1][0] - vals[0][0]
    dm = vals[-1][1] - vals[0][1]
    segs = [
        math.hypot(vals[k][0] - vals[k - 1][0], vals[k][1] - vals[k - 1][1])
        for k in range(1, lb)
    ]
    if dr > 0 and dm > 0:
        trend = "up_right"
    elif dr > 0:
        trend = "down_right"
    elif dm > 0:
        trend = "up_left"
    else:
        trend = "down_left"
    return {
        "quadrants": [v[2] for v in vals],
        "end_q": vals[-1][2],
        "trend": trend,
        "disp": math.hypot(dr, dm),
        "segs": segs,
        "mono_up": all(segs[i] > segs[i - 1] for i in range(1, len(segs)))
        if len(segs) >= 2
        else False,
        "seg_last": segs[-1] if segs else 0.0,
        "rs_ratio": vals[-1][0],
        "rs_momentum": vals[-1][1],
    }


def _tier2(f: dict[str, Any] | None) -> bool:
    return bool(
        f
        and f["trend"] == "up_right"
        and f["end_q"] == "leading"
        and 1 <= f["disp"] < 2
    )


def _mono_tier2(f: dict[str, Any] | None) -> bool:
    return _tier2(f) and bool(f and f["mono_up"])


def _fresh_mono(
    rs_ratio,
    rs_mom,
    full_dates: list[str],
    si: int,
    sid: str,
) -> bool:
    f = _feat(rs_ratio, rs_mom, full_dates, si, sid)
    if not _mono_tier2(f):
        return False
    prev = _feat(rs_ratio, rs_mom, full_dates, si - 1, sid)
    return not _mono_tier2(prev)


def _latest_trading_date(conn: sqlite3.Connection, *, on_or_before: str | None = None) -> str:
    if on_or_before:
        row = conn.execute(
            """
            SELECT MAX(trade_date) AS d FROM stock_daily_bars
            WHERE source = 'finmind' AND trade_date <= ?
            """,
            (on_or_before,),
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT MAX(trade_date) AS d FROM stock_daily_bars
            WHERE source = 'finmind'
            """
        ).fetchone()
    if row is None or row["d"] is None:
        raise RuntimeError("stock_daily_bars 無資料")
    return str(row["d"])


def load_slot_state() -> dict[str, Any]:
    if not STATE_PATH.is_file():
        return {"slots": [], "history": []}
    return json.loads(STATE_PATH.read_text(encoding="utf-8"))


def save_slot_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _expire_slots(state: dict[str, Any], as_of: str) -> list[dict[str, Any]]:
    """移除 exit_date < as_of 的持倉，回傳剛到期清單。"""
    kept: list[dict[str, Any]] = []
    expired: list[dict[str, Any]] = []
    for pos in state.get("slots", []):
        exit_d = str(pos.get("exit_date", ""))
        if not exit_d or exit_d > as_of:
            kept.append(pos)
        else:
            expired.append(pos)
            state.setdefault("history", []).append({**pos, "closed_on": as_of})
    state["slots"] = kept
    return expired


def _backfill_exit_dates(
    conn: sqlite3.Connection,
    state: dict[str, Any],
    full_dates: list[str],
) -> None:
    for pos in state.get("slots", []):
        if pos.get("exit_date") and not pos.get("exit_pending"):
            continue
        entry = str(pos.get("entry_date", ""))
        exit_d = _exit_date_from_entry(conn, full_dates, entry, HOLD_DAYS)
        if exit_d:
            pos["exit_date"] = exit_d
            pos.pop("exit_pending", None)


def scan_rows_from_panels(
    conn: sqlite3.Connection,
    as_of: str,
    close: pd.DataFrame,
    bench: pd.Series,
    *,
    etf_codes: tuple[str, ...] = DEFAULT_ETF_CODES,
) -> tuple[list[ScanRow], list[ScanRow]]:
    """以給定 close / bench panel 掃描 mono（盤中預估價亦可）。"""
    bench = bench.reindex(close.index)
    rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench, length=LENGTH)
    daily_pct = close.pct_change(fill_method=None) * 100.0
    full_dates = close.index.astype(str).tolist()
    if as_of not in full_dates:
        raise RuntimeError(f"{as_of} 不在收盤價 panel")
    si = full_dates.index(as_of)

    watch = load_etf_constituent_watchlist(conn, etf_codes)
    name_map = {w["stock_id"]: w.get("stock_name", "") for w in watch}
    universe = [w["stock_id"] for w in watch]

    all_mono: list[ScanRow] = []
    fresh_mono: list[ScanRow] = []
    for sid in universe:
        f = _feat(rs_ratio, rs_mom, full_dates, si, sid)
        if not _mono_tier2(f):
            continue
        is_fresh = _fresh_mono(rs_ratio, rs_mom, full_dates, si, sid)
        pct = float(daily_pct.at[as_of, sid]) if sid in daily_pct.columns else None
        if pct != pct:
            pct = None
        row = ScanRow(
            stock_id=sid,
            stock_name=name_map.get(sid, ""),
            fresh=is_fresh,
            mono=True,
            seg_last=float(f["seg_last"]),
            disp=float(f["disp"]),
            segs=[float(x) for x in f["segs"]],
            quadrants=[q or "?" for q in f["quadrants"]],
            rs_ratio=float(f["rs_ratio"]),
            rs_momentum=float(f["rs_momentum"]),
            daily_pct=pct,
        )
        all_mono.append(row)
        if is_fresh:
            fresh_mono.append(row)

    key = lambda r: (-r.seg_last, r.stock_id)
    all_mono.sort(key=key)
    fresh_mono.sort(key=key)
    return all_mono, fresh_mono


def _scan_rows(
    conn: sqlite3.Connection,
    as_of: str,
    *,
    etf_codes: tuple[str, ...] = DEFAULT_ETF_CODES,
) -> tuple[list[ScanRow], list[ScanRow]]:
    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index)
    return scan_rows_from_panels(conn, as_of, close, bench, etf_codes=etf_codes)


def _exit_date_from_entry(
    conn: sqlite3.Connection,
    full_dates: list[str],
    entry_date: str,
    hold_days: int,
) -> str | None:
    """D4 收盤進場（entry_date=訊號日 si）→ D11 收盤出場（exit_si=si+hold_days）。"""
    if entry_date in full_dates:
        si = full_dates.index(entry_date)
        exit_si = si + hold_days
        if exit_si < len(full_dates):
            return full_dates[exit_si]
    dates = trading_dates_after(conn, entry_date, count=hold_days)
    if len(dates) >= hold_days:
        return dates[hold_days - 1]
    return None


def _apply_entries(
    conn: sqlite3.Connection,
    state: dict[str, Any],
    as_of: str,
    fresh_mono: list[ScanRow],
    *,
    full_dates: list[str] | None = None,
) -> list[dict[str, Any]]:
    """空槽依 seg_last 填入 fresh 訊號，回傳新進場清單。"""
    held = {p["stock_id"] for p in state.get("slots", [])}
    used_slots = {int(p["slot"]) for p in state.get("slots", [])}
    free_slots = [i for i in range(MAX_SLOTS) if i not in used_slots]
    added: list[dict[str, Any]] = []

    for row in fresh_mono[:TOP_N]:
        if not free_slots:
            break
        if row.stock_id in held:
            continue
        exit_d = _exit_date_from_entry(conn, full_dates or [], as_of, HOLD_DAYS)
        slot = free_slots.pop(0)
        pos = {
            "slot": slot,
            "stock_id": row.stock_id,
            "stock_name": row.stock_name,
            "entry_date": as_of,
            "exit_date": exit_d or "",
            "seg_last": round(row.seg_last, 4),
            "disp": round(row.disp, 4),
        }
        if exit_d is None:
            pos["exit_pending"] = True
        state.setdefault("slots", []).append(pos)
        held.add(row.stock_id)
        added.append(pos)
    return added


def _fmt_pct(v: float | None) -> str:
    if v is None or v != v:
        return "—"
    return f"{v:+.1f}%"


def render_markdown(
    *,
    as_of: str,
    all_mono: list[ScanRow],
    fresh_mono: list[ScanRow],
    slots: list[dict[str, Any]],
    expired: list[dict[str, Any]],
    added: list[dict[str, Any]],
) -> str:
    lines = [
        f"# RRG mono 每日掃描 · {as_of}",
        "",
        f"策略：**mono 濾網 + seg_last 排序 + 3 槽 + hold7**（{EXECUTION_RULE_ZH}）",
        "",
        "## 槽位狀態",
        "",
    ]
    if not slots:
        lines.append("- （目前無持倉，3 槽皆空）")
    else:
        lines.append("| 槽 | 代號 | 名稱 | 進場(D4) | 出場(D11) | seg_last |")
        lines.append("|---|------|------|------|----------|----------|")
        for p in sorted(slots, key=lambda x: int(x["slot"])):
            lines.append(
                f"| {int(p['slot']) + 1} | {p['stock_id']} | {p['stock_name']} | "
                f"{p['entry_date']} | {p.get('exit_date') or '待補庫'} | {p.get('seg_last', '—')} |"
            )
    free = MAX_SLOTS - len(slots)
    lines.extend(["", f"**空槽：{free} / {MAX_SLOTS}**", ""])

    if expired:
        lines.extend(["## 今日到期出場", ""])
        for p in expired:
            lines.append(
                f"- 槽{int(p['slot']) + 1} **{p['stock_id']} {p['stock_name']}** "
                f"（{p['entry_date']} → {p['exit_date']}）"
            )
        lines.append("")

    if added:
        lines.extend(["## 今日新進場（已寫入槽位狀態）", ""])
        for p in added:
            lines.append(
                f"- 槽{int(p['slot']) + 1} **{p['stock_id']} {p['stock_name']}** "
                f"seg_last={p['seg_last']:.3f}，持有至 {p.get('exit_date') or '（待補庫）'}"
            )
        lines.append("")

    lines.extend(["## mono fresh 訊號（seg_last 排序）", ""])
    if not fresh_mono:
        lines.append("_今日無 mono fresh 新訊號。_")
    else:
        lines.append("| # | 代號 | 名稱 | seg_last | 位移 | 三段 | 當日 | RV | MV |")
        lines.append("|---|------|------|----------|------|------|------|----|----|")
        for i, r in enumerate(fresh_mono[:TOP_N], 1):
            segs = " / ".join(f"{s:.2f}" for s in r.segs)
            lines.append(
                f"| {i} | {r.stock_id} | {r.stock_name} | {r.seg_last:.3f} | "
                f"{r.disp:.2f} | {segs} | {_fmt_pct(r.daily_pct)} | "
                f"{r.rs_ratio:.1f} | {r.rs_momentum:.1f} |"
            )
    lines.append("")

    lines.extend(["## 所有 mono（含非 fresh）", ""])
    if not all_mono:
        lines.append("_今日無 mono 標的。_")
    else:
        lines.append("| # | 代號 | 名稱 | fr | seg_last | 位移 | 當日 | 象限路徑 |")
        lines.append("|---|------|------|----|----------|------|------|----------|")
        for i, r in enumerate(all_mono, 1):
            fr = "★" if r.fresh else ""
            qp = " → ".join(r.quadrants)
            lines.append(
                f"| {i} | {r.stock_id} | {r.stock_name} | {fr} | {r.seg_last:.3f} | "
                f"{r.disp:.2f} | {_fmt_pct(r.daily_pct)} | {qp} |"
            )
    lines.extend(
        [
            "",
            "---",
            f"狀態檔：`{STATE_PATH.relative_to(PROJECT_ROOT)}`",
            EXECUTION_DETAIL_ZH,
        ]
    )
    return "\n".join(lines) + "\n"


def build_brief(
    conn: sqlite3.Connection,
    *,
    as_of: str | None = None,
    apply_slots: bool = True,
    etf_codes: tuple[str, ...] = DEFAULT_ETF_CODES,
) -> dict[str, Any]:
    trade_date = as_of or _latest_trading_date(conn)
    state = load_slot_state()
    expired = _expire_slots(state, trade_date)
    close, _, _ = load_price_panels(conn)
    full_dates = close.index.astype(str).tolist()
    _backfill_exit_dates(conn, state, full_dates)
    all_mono, fresh_mono = _scan_rows(conn, trade_date, etf_codes=etf_codes)
    added: list[dict[str, Any]] = []
    if apply_slots:
        added = _apply_entries(
            conn, state, trade_date, fresh_mono, full_dates=full_dates
        )
    state["updated"] = trade_date
    save_slot_state(state)
    md = render_markdown(
        as_of=trade_date,
        all_mono=all_mono,
        fresh_mono=fresh_mono,
        slots=state.get("slots", []),
        expired=expired,
        added=added,
    )
    stamp = trade_date.replace("-", "")
    out_dated = REPORTS / f"{stamp}_rrg_mono_daily.md"
    out_latest = REPORTS / "rrg_mono_daily.md"
    REPORTS.mkdir(parents=True, exist_ok=True)
    out_dated.write_text(md, encoding="utf-8")
    out_latest.write_text(md, encoding="utf-8")
    return {
        "as_of": trade_date,
        "fresh_count": len(fresh_mono),
        "mono_count": len(all_mono),
        "slots": state.get("slots", []),
        "expired": expired,
        "added": added,
        "report_dated": str(out_dated),
        "report_latest": str(out_latest),
        "markdown": md,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=f"RRG mono 每日掃描 brief（{EXECUTION_RULE_ZH}）"
    )
    parser.add_argument("--date", default="", help="YYYY-MM-DD（預設最新交易日）")
    parser.add_argument(
        "--no-apply",
        action="store_true",
        help="只產報告，不更新 3 槽狀態",
    )
    parser.add_argument(
        "--etf-codes",
        nargs="*",
        default=list(DEFAULT_ETF_CODES),
        help="ETF 成分宇宙",
    )
    args = parser.parse_args(argv)

    conn = connect(DEFAULT_DB_PATH)
    try:
        result = build_brief(
            conn,
            as_of=args.date or None,
            apply_slots=not args.no_apply,
            etf_codes=tuple(args.etf_codes),
        )
    finally:
        conn.close()

    print(result["report_latest"])
    print(
        f"mono fresh={result['fresh_count']} mono_all={result['mono_count']} "
        f"slots={len(result['slots'])} added={len(result['added'])}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
