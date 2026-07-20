#!/usr/bin/env python3
"""華邦電專家池共識 · 夜間觀測（達標才寄信）.

Research / observe only · 不下單。

  1. 可選：FinMind 補最近 N 個交易日 2344 分點 tape
  2. 檢查實務冠軍規則是否在 as-of 日觸發
     - 共識·≥1億（≥2 核心分點同日淨買 ≥1億）← 首選
     - OR·≥1億（任一核心分點 ≥1億）
  3. 僅在「新」訊號寄信（state 去重）；首次空 state 只 bootstrap 不洗信

  PYTHONPATH=src python3 scripts/research/run_winbond_expert_pool_watch.py
  PYTHONPATH=src python3 scripts/research/run_winbond_expert_pool_watch.py --no-refresh
  PYTHONPATH=src python3 scripts/research/run_winbond_expert_pool_watch.py --force-email
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from finmind_client import fetch_taiwan_stock_trading_daily_report  # noqa: E402
from notify_email import send_alert  # noqa: E402
from project_dotenv import load_project_dotenv  # noqa: E402
from stock_db import DEFAULT_DB_PATH, connect, upsert_stock_broker_branch_daily  # noqa: E402

SOURCE = "finmind"
STOCK_ID = "2344"
STOCK_NAME = "華邦電"
CORE_POOL: dict[str, str] = {
    "5850": "統一",
    "5854": "統一-城中",
    "779Z": "國票安和",
}
CORE_IDS = set(CORE_POOL.keys())
NET_FLOOR = 1e8  # 1 億
STATE_PATH = ROOT / "data" / "scratch" / "winbond_expert_pool_watch_state.json"
REPORT_DIR = (
    ROOT
    / "reports"
    / "research"
    / "branch-footprint-screen"
    / "winbond_expert_pool"
)
ENV_FLAG = "RUN_WINBOND_EXPERT_POOL_EMAIL"


def _email_enabled() -> bool:
    return os.environ.get(ENV_FLAG, "1").strip() not in ("0", "false", "False")


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="華邦電專家池共識夜間觀測")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument(
        "--asof",
        default="",
        help="Signal date YYYY-MM-DD（預設：DB 最新有 2344 tape 的交易日）",
    )
    ap.add_argument(
        "--refresh-days",
        type=int,
        default=5,
        help="FinMind 補最近 N 個交易日 2344 分點（0=不補）",
    )
    ap.add_argument("--no-refresh", action="store_true", help="等同 --refresh-days 0")
    ap.add_argument(
        "--state",
        type=Path,
        default=STATE_PATH,
        help="已通知去重 state JSON",
    )
    ap.add_argument(
        "--force-email",
        action="store_true",
        help="忽略去重，若當日達標仍寄信（測試用）",
    )
    ap.add_argument(
        "--bootstrap-only",
        action="store_true",
        help="只寫入現況到 state、不寄信",
    )
    return ap.parse_args()


def _float(v: object) -> float:
    if v is None or v == "":
        return 0.0
    return float(v)


def aggregate_stock_day_rows(raw: list[dict], *, trade_date: str) -> list[dict]:
    """Stock-centric FinMind report → per (trader, stock) buy/sell."""
    by_tid: dict[str, dict[str, float | str]] = {}
    for item in raw:
        tid = str(item.get("securities_trader_id") or "").strip()
        sid = str(item.get("stock_id") or item.get("data_id") or "").strip()
        if not tid:
            continue
        if sid and sid != STOCK_ID:
            continue
        buy = _float(item.get("buy") or item.get("Buy"))
        sell = _float(item.get("sell") or item.get("Sell"))
        name = str(item.get("securities_trader") or "")
        slot = by_tid.get(tid)
        if slot is None:
            by_tid[tid] = {
                "buy": buy,
                "sell": sell,
                "securities_trader": name,
            }
        else:
            slot["buy"] = float(slot["buy"]) + buy
            slot["sell"] = float(slot["sell"]) + sell
            if name and not slot.get("securities_trader"):
                slot["securities_trader"] = name
    out: list[dict] = []
    for tid, agg in by_tid.items():
        buy = float(agg["buy"])
        sell = float(agg["sell"])
        out.append(
            {
                "trade_date": trade_date,
                "securities_trader_id": tid,
                "securities_trader": str(agg.get("securities_trader") or tid),
                "stock_id": STOCK_ID,
                "buy": buy,
                "sell": sell,
                "net": buy - sell,
                "source": SOURCE,
            }
        )
    return out


def list_recent_trading_days(conn, *, n: int, end: str | None = None) -> list[str]:
    end = end or date.today().isoformat()
    rows = conn.execute(
        """
        SELECT DISTINCT trade_date FROM stock_daily_bars
        WHERE source = ? AND stock_id = ? AND trade_date <= ? AND close > 0
        ORDER BY trade_date DESC
        LIMIT ?
        """,
        (SOURCE, STOCK_ID, end, max(1, n)),
    ).fetchall()
    return [str(r[0]) for r in rows]


def refresh_stock_tape(conn, days: list[str], *, delay: float = 0.5) -> dict[str, int]:
    """Fetch FinMind stock-centric report for 2344; upsert branch tape."""
    counts: dict[str, int] = {}
    for day in days:
        try:
            raw = fetch_taiwan_stock_trading_daily_report(
                trade_date=day,
                data_id=STOCK_ID,
            )
        except Exception as exc:  # noqa: BLE001 — soft skip missing/API
            print(f"  refresh {day}: FAIL {exc}")
            counts[day] = -1
            time.sleep(delay)
            continue
        rows = aggregate_stock_day_rows(raw, trade_date=day)
        n = upsert_stock_broker_branch_daily(conn, rows)
        print(f"  refresh {day}: rows={n} traders={len(rows)}")
        counts[day] = n
        time.sleep(delay)
    return counts


def close_on(conn, trade_date: str) -> float | None:
    row = conn.execute(
        """
        SELECT close FROM stock_daily_bars
        WHERE stock_id = ? AND trade_date = ? AND source = ? AND close > 0
        """,
        (STOCK_ID, trade_date, SOURCE),
    ).fetchone()
    return float(row[0]) if row else None


def core_nets_on(conn, trade_date: str) -> list[dict]:
    close = close_on(conn, trade_date)
    if close is None:
        return []
    placeholders = ",".join("?" * len(CORE_POOL))
    rows = conn.execute(
        f"""
        SELECT securities_trader_id, securities_trader, net, buy, sell
        FROM stock_broker_branch_daily
        WHERE stock_id = ? AND trade_date = ? AND source = ?
          AND securities_trader_id IN ({placeholders})
          AND net > 0
        """,
        (STOCK_ID, trade_date, SOURCE, *CORE_POOL.keys()),
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        net = float(r["net"])
        net_amt = net * close
        tid = str(r["securities_trader_id"])
        out.append(
            {
                "tid": tid,
                "name": CORE_POOL.get(tid, str(r["securities_trader"] or tid)),
                "net": net,
                "net_amt": net_amt,
                "close": close,
            }
        )
    return sorted(out, key=lambda x: -x["net_amt"])


def evaluate_rules(events: list[dict]) -> list[dict]:
    """Return fired champion rules for one day."""
    big = [e for e in events if e["net_amt"] >= NET_FLOOR]
    fired: list[dict] = []
    if big:
        fired.append(
            {
                "rule": "OR·≥1億",
                "priority": 2,
                "n_branches": len(big),
                "branches": big,
                "note": "任一核心分點淨買≥1億（全樣本天花板；OOS 較脆）",
            }
        )
    if len(big) >= 2:
        fired.append(
            {
                "rule": "共識·≥1億",
                "priority": 1,
                "n_branches": len(big),
                "branches": big,
                "note": "≥2 核心分點同日淨買≥1億（實務首選）",
            }
        )
    return sorted(fired, key=lambda x: x["priority"])


def load_state(path: Path) -> dict:
    if not path.is_file():
        return {"notified": {}, "updated_at": None}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"notified": {}, "updated_at": None}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = datetime.now().isoformat(timespec="seconds")
    path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def signal_key(rule: str, asof: str) -> str:
    return f"{rule}|{asof}"


def format_alert_body(*, asof: str, fired: list[dict], tape_note: str) -> str:
    lines = [
        f"華邦電（{STOCK_ID}）專家池共識觀測 · {asof}",
        "",
        "專家核心：統一 / 統一-城中 / 國票安和",
        "動作：觀測通知（Research · 未採納 · 不下單）",
        f"Tape：{tape_note}",
        "",
    ]
    for hit in fired:
        lines.append(f"【{hit['rule']}】{hit['note']}")
        lines.append(f"  觸發分點數：{hit['n_branches']}")
        for b in hit["branches"]:
            lines.append(
                f"  · {b['name']} ({b['tid']}) 淨買 "
                f"{b['net_amt'] / 1e8:.2f} 億（{b['net']:,.0f} 股 @ {b['close']:.2f}）"
            )
        lines.append("")
    lines += [
        "建議持有：H7–H10（T+1 開盤跟）",
        "腳本：scripts/research/run_winbond_expert_pool_watch.py",
        "回測：scripts/research/run_winbond_expert_pool_consensus.py",
    ]
    return "\n".join(lines)


def write_daily_report(*, asof: str, fired: list[dict], body: str) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / f"watch_{asof.replace('-', '')}.md"
    status = "TRIGGERED" if fired else "quiet"
    md = [
        f"# 華邦電專家池觀測 · {asof}",
        "",
        f"- status: `{status}`",
        f"- generated: `{datetime.now().isoformat(timespec='seconds')}`",
        "",
        "```",
        body if fired else f"{asof} · 未達標（共識·≥1億 / OR·≥1億）",
        "```",
        "",
    ]
    path.write_text("\n".join(md), encoding="utf-8")
    return path


def resolve_asof(conn, explicit: str) -> str | None:
    if explicit:
        return explicit[:10]
    row = conn.execute(
        """
        SELECT MAX(trade_date) FROM stock_broker_branch_daily
        WHERE stock_id = ? AND source = ?
          AND securities_trader_id IN (?, ?, ?)
        """,
        (STOCK_ID, SOURCE, *CORE_POOL.keys()),
    ).fetchone()
    return str(row[0]) if row and row[0] else None


def main() -> int:
    load_project_dotenv()
    args = parse_args()
    refresh_n = 0 if args.no_refresh else max(0, int(args.refresh_days))

    conn = connect(args.db)
    try:
        tape_note = "db-only"
        if refresh_n > 0:
            days = list_recent_trading_days(conn, n=refresh_n)
            days = list(reversed(days))  # oldest first
            print(f"refresh tape days={days}")
            counts = refresh_stock_tape(conn, days)
            ok = sum(1 for v in counts.values() if v >= 0)
            tape_note = f"finmind refresh {ok}/{len(days)} days"

        asof = resolve_asof(conn, args.asof)
        if not asof:
            print("no 2344 core-branch tape in DB; nothing to watch")
            return 0

        events = core_nets_on(conn, asof)
        fired = evaluate_rules(events)
        print(
            f"asof={asof} core_pos={len(events)} "
            f"fired={[f['rule'] for f in fired] or ['(none)']}"
        )
        for e in events:
            mark = "*" if e["net_amt"] >= NET_FLOOR else " "
            print(
                f"  {mark} {e['name']:8s} {e['net_amt'] / 1e8:6.2f}億"
            )

        state = load_state(args.state)
        notified: dict = state.setdefault("notified", {})
        is_bootstrap = not notified and not args.force_email

        new_hits: list[dict] = []
        for hit in fired:
            key = signal_key(hit["rule"], asof)
            if args.force_email or key not in notified:
                new_hits.append(hit)

        body = format_alert_body(asof=asof, fired=fired, tape_note=tape_note)
        report = write_daily_report(
            asof=asof,
            fired=fired,
            body=body if fired else f"{asof} quiet",
        )
        print(f"report={report}")

        if args.bootstrap_only or is_bootstrap:
            for hit in fired:
                notified[signal_key(hit["rule"], asof)] = {
                    "at": datetime.now().isoformat(timespec="seconds"),
                    "bootstrap": True,
                }
            # also seed quiet marker so next run isn't treated as bootstrap
            state["bootstrapped_asof"] = asof
            save_state(args.state, state)
            print(
                "bootstrap state written"
                + (" (no email)" if fired else " (quiet day)")
            )
            return 0

        if not new_hits:
            print("no new champion fires (already notified or quiet)")
            return 0

        if not _email_enabled():
            print(f"email skipped: {ENV_FLAG}=0")
            for hit in new_hits:
                notified[signal_key(hit["rule"], asof)] = {
                    "at": datetime.now().isoformat(timespec="seconds"),
                    "skipped_email": True,
                }
            save_state(args.state, state)
            return 0

        primary = any(h["rule"] == "共識·≥1億" for h in new_hits)
        subject = (
            f"[ETF研究] 華邦電專家池共識觸發 · {asof}"
            if primary
            else f"[ETF研究] 華邦電專家池 OR≥1億 · {asof}"
        )
        alert_body = format_alert_body(
            asof=asof, fired=new_hits, tape_note=tape_note
        )
        send_alert(subject, alert_body)
        print(f"email sent: {subject}")
        for hit in new_hits:
            notified[signal_key(hit["rule"], asof)] = {
                "at": datetime.now().isoformat(timespec="seconds"),
            }
        save_state(args.state, state)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
