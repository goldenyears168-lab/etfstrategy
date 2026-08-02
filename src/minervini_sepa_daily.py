#!/usr/bin/env python3
"""Minervini SEPA basket · 月末等權調倉 screen（dry-run intent · 人工確認後送單）。"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from project_dotenv import load_project_dotenv
from report_paths import REPORTS_DIR
from research.backtest.broad_momentum_tv_backtest import params_from_config
from price_panels import load_price_panels
from research.backtest.finpilot_local_backtest import month_end_trading_dates
from stage_analysis import MINERVINI_CRITERIA_TOTAL, vectorized_minervini_criteria_count
from stock_db import DATA_DIR, DEFAULT_DB_PATH, PROJECT_ROOT, connect

STRATEGY_ID = "minervini-sepa-basket"
STATE_PATH = DATA_DIR / "minervini_sepa_basket_state.json"
INTENTS_DIR = PROJECT_ROOT / "reports" / "order" / "intents"


@dataclass(frozen=True)
class ScreenResult:
    session_date: str
    is_rebalance_day: bool
    month_end: str | None
    picks: list[str]
    pick_names: dict[str, str]
    prev_picks: list[str]
    changed: bool
    dry_run: bool


def _env_flag(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


def load_state() -> dict[str, Any]:
    if not STATE_PATH.is_file():
        return {"picks": [], "as_of_month_end": ""}
    return json.loads(STATE_PATH.read_text(encoding="utf-8"))


def save_state(month_end: str, picks: list[str]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "as_of_month_end": month_end,
        "picks": picks,
        "updated": datetime.now().isoformat(timespec="seconds"),
    }
    STATE_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _stock_names(conn: sqlite3.Connection, stock_ids: list[str]) -> dict[str, str]:
    if not stock_ids:
        return {}
    placeholders = ",".join("?" * len(stock_ids))
    rows = conn.execute(
        f"""
        SELECT stock_id, stock_name
        FROM etf_holdings
        WHERE stock_id IN ({placeholders})
        GROUP BY stock_id
        """,
        stock_ids,
    ).fetchall()
    out = {str(r[0]): str(r[1] or "") for r in rows}
    for sid in stock_ids:
        out.setdefault(sid, "")
    return out


def compute_picks(conn: sqlite3.Connection, session: str) -> list[str]:
    params = params_from_config()
    close, _, _ = load_price_panels(conn)
    if session not in close.index.astype(str):
        return []
    crit = vectorized_minervini_criteria_count(close)
    min_pass = min(params.minervini_pass, MINERVINI_CRITERIA_TOTAL - 1)
    row = crit.loc[session]
    return sorted([str(s) for s in row.index if float(row[s]) >= min_pass])


def run_screen(
    conn: sqlite3.Connection,
    *,
    session: str | None = None,
    dry_run: bool = True,
    apply_state: bool = True,
) -> ScreenResult:
    close, _, _ = load_price_panels(conn)
    dates = sorted(close.index.astype(str).tolist())
    if not dates:
        raise RuntimeError("stock_daily_bars 無交易日")
    session_date = session or dates[-1]
    if session_date not in dates:
        session_date = dates[-1]

    month_ends = month_end_trading_dates(dates)
    is_rebalance = session_date in month_ends
    prev_state = load_state()
    prev_picks = list(prev_state.get("picks") or [])

    picks: list[str] = []
    if is_rebalance:
        picks = compute_picks(conn, session_date)
    else:
        picks = list(prev_picks)

    names = _stock_names(conn, picks)
    changed = is_rebalance and picks != prev_picks

    if is_rebalance and apply_state and changed:
        save_state(session_date, picks)

    return ScreenResult(
        session_date=session_date,
        is_rebalance_day=is_rebalance,
        month_end=session_date if is_rebalance else None,
        picks=picks,
        pick_names=names,
        prev_picks=prev_picks,
        changed=changed,
        dry_run=dry_run,
    )


def render_markdown(result: ScreenResult) -> str:
    mode = "dry-run" if result.dry_run else "live-metadata"
    lines = [
        f"# Minervini SEPA basket · {result.session_date}",
        "",
        f"> strategy `{STRATEGY_ID}` · **{mode}** · 月末等權 · Trend Template 7/7（bulk 不含 RS）",
        "",
        f"- 今日是否月末調倉日：**{'是' if result.is_rebalance_day else '否'}**",
        f"- 候選檔數：**{len(result.picks)}** · 上次月末：**{load_state().get('as_of_month_end') or '—'}**",
    ]
    if result.is_rebalance_day and result.changed:
        lines.append("- **組合變動**：是 → 請人工確認 intent 後送單")
    elif result.is_rebalance_day:
        lines.append("- 組合變動：否（與上次相同）")
    lines.append("")
    if result.picks:
        lines.append("## 月末 basket")
        lines.append("")
        lines.append("| 代碼 | 名稱 |")
        lines.append("|------|------|")
        for sid in result.picks:
            lines.append(f"| {sid} | {result.pick_names.get(sid, '')} |")
    else:
        lines.append("_本月無 7/7 合格標 → 空倉（現金）_")
    lines.extend(
        [
            "",
            "## 執行",
            "",
            "- 盤程：**獨立 launchd** `minervini-sepa-basket` · 16:35 檢查月末",
            "- 下單：僅產 `reports/order/intents/` · **不自動送單**",
            "- 確認：`.venv-fubon/bin/python scripts/order/submit_intents.py <intent.json> --dry-run`",
            "",
        ]
    )
    return "\n".join(lines)


def build_intent_batch(result: ScreenResult, *, capital_ntd: float) -> dict[str, Any] | None:
    if not result.is_rebalance_day or not result.changed or not result.picks:
        return None
    per_name = capital_ntd / len(result.picks)
    intents: list[dict[str, Any]] = []
    for sid in result.picks:
        intents.append(
            {
                "symbol": sid,
                "side": "buy",
                "quantity_shares": 0,
                "price": "0",
                "price_type": "limit",
                "market_type": "common",
                "time_in_force": "rod",
                "order_type": "stock",
                "user_def": f"SEPA:rebalance",
                "note": f"月末等權 · budget≈{per_name:.0f} NTD · 請依市價填 quantity",
            }
        )
    return {
        "schema_version": "order-intent-v1",
        "strategy_id": STRATEGY_ID,
        "as_of": result.session_date,
        "metadata": {
            "layer": "strategy",
            "rebalance": True,
            "dry_run": result.dry_run,
            "picks": result.picks,
            "prev_picks": result.prev_picks,
            "capital_ntd": capital_ntd,
            "per_name_ntd": per_name,
        },
        "intents": intents,
    }


def write_outputs(result: ScreenResult, *, capital_ntd: float) -> tuple[Path, Path | None]:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = result.session_date.replace("-", "")
    md_path = REPORTS_DIR / f"{stamp}_minervini_sepa_basket_daily.md"
    latest = REPORTS_DIR / "minervini_sepa_basket_daily.md"
    md_path.write_text(render_markdown(result), encoding="utf-8")
    latest.write_text(md_path.read_text(encoding="utf-8"), encoding="utf-8")

    intent_path: Path | None = None
    batch = build_intent_batch(result, capital_ntd=capital_ntd)
    if batch is not None:
        INTENTS_DIR.mkdir(parents=True, exist_ok=True)
        intent_path = INTENTS_DIR / f"{STRATEGY_ID}_{result.session_date}.json"
        intent_path.write_text(json.dumps(batch, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return md_path, intent_path


def main(argv: list[str] | None = None) -> int:
    load_project_dotenv()
    parser = argparse.ArgumentParser(description="Minervini SEPA basket · 月末 screen")
    parser.add_argument("--date", help="session YYYY-MM-DD（預設 DB 最新交易日）")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--no-apply-state", action="store_true")
    parser.add_argument("--live-intent", action="store_true", help="intent metadata dry_run=false")
    args = parser.parse_args(argv)

    capital = float(os.environ.get("MINERVINI_SEPA_CAPITAL_NTD", "100000"))
    dry_run = not args.live_intent

    conn = connect(args.db)
    try:
        result = run_screen(
            conn,
            session=args.date,
            dry_run=dry_run,
            apply_state=not args.no_apply_state,
        )
    finally:
        conn.close()

    md_path, intent_path = write_outputs(result, capital_ntd=capital)
    print(f"Wrote {md_path}")
    if intent_path:
        print(f"Wrote {intent_path}")
        print("MINERVINI_SEPA_SIGNAL=1")

    if result.is_rebalance_day and result.changed:
        print(f"rebalance: {len(result.prev_picks)} → {len(result.picks)} picks")
    elif result.is_rebalance_day:
        print("rebalance: no pick change")
    else:
        print("skip: not month-end trading day")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
