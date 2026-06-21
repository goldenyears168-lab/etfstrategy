#!/usr/bin/env python3
"""
VCP 盤中 watchlist：讀最近 vcp_screen_scores_v2 + FinMind tick，產報告並通知。

用法：
  PYTHONPATH=src python src/vcp_intraday_watch.py
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from finmind_client import fetch_tick_snapshots
from project_dotenv import load_project_dotenv
from report_paths import REPORTS_DIR
from stock_db import (
    DATA_DIR,
    PROJECT_ROOT,
    load_latest_vcp_screen_v2_date,
    load_vcp_screen_v2_for_date,
)

ALERT_STATE_PATH = DATA_DIR / "vcp_intraday_alerted.json"

DEFAULT_MODEL_IDS = ("vcp-tm", "chunge-funnel")
DEFAULT_EXECUTION_STATES = ("Pre-breakout", "Breakout")
DEFAULT_MIN_SCORE = 50.0
DEFAULT_ALERT_ON = ("BREAKOUT_HOLD", "BREAKOUT_FADE", "NEAR")
BREAKOUT_BUFFER = 1.005
NEAR_BUFFER = 0.97

# 自 intraday high 回落 ≥ 此值 → 突破回落（假突破風險）
DEFAULT_FADE_PULLBACK_PCT = 3.0
# 回落 ≤ 此值 → 突破守穩（價仍在高點附近）
DEFAULT_HOLD_PULLBACK_PCT = 2.0
# 距 pivot 已拉過遠
DEFAULT_EXTENDED_PCT = 8.0

STATUS_LABELS = {
    "BREAKOUT_HOLD": "突破·守穩",
    "BREAKOUT_FADE": "突破·回落",
    "BREAKOUT_EXTENDED": "突破·過遠",
    "NEAR": "接近",
    "SETUP": "整理",
    "UNKNOWN": "—",
}

ACTION_HINTS = {
    "BREAKOUT_HOLD": "突破有效，可評估進場（停損 pivot 下）",
    "BREAKOUT_FADE": "曾突破但自高點回落，慎追／假突破風險",
    "BREAKOUT_EXTENDED": "已離 pivot 過遠，不宜標準 VCP 追價",
    "NEAR": "接近 pivot，等放量突破或掛停損",
    "SETUP": "距 pivot 尚遠，僅觀察",
    "UNKNOWN": "無即時價",
}


@dataclass(frozen=True)
class WatchRow:
    stock_id: str
    stock_name: str
    vcp_score: float
    execution_state: str
    pivot_price: float | None
    model_id: str
    price: float | None
    day_open: float | None
    day_high: float | None
    dist_pivot_pct: float | None
    pullback_from_high_pct: float | None
    intraday_status: str
    action_hint: str
    entry_ready: bool = False


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    return float(raw)


def _env_csv(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    return tuple(x.strip() for x in raw.split(",") if x.strip())


def last_vcp_as_of(conn: sqlite3.Connection) -> str | None:
    return load_latest_vcp_screen_v2_date(conn)


def load_merged_watchlist(
    conn: sqlite3.Connection,
    as_of_date: str,
    *,
    model_ids: tuple[str, ...],
    min_score: float,
    execution_states: tuple[str, ...] | None = None,
) -> list[dict]:
    states = execution_states or DEFAULT_EXECUTION_STATES
    merged: dict[str, dict] = {}
    for model_id in model_ids:
        rows = load_vcp_screen_v2_for_date(
            conn,
            as_of_date,
            model_id=model_id,
            min_score=min_score,
            execution_states=states,
        )
        for row in rows:
            sid = str(row["stock_id"])
            score = float(row["composite_score"] or 0)
            prev = merged.get(sid)
            if prev is None or score > float(prev["vcp_score"]):
                merged[sid] = {
                    "stock_id": sid,
                    "stock_name": row["stock_name"] or "",
                    "vcp_score": score,
                    "execution_state": str(row["execution_state"] or ""),
                    "entry_ready": bool(row["entry_ready"]),
                    "pivot_price": row["pivot_price"],
                    "distance_from_pivot_pct": row["distance_from_pivot_pct"],
                    "model_id": model_id,
                }
    out = list(merged.values())
    out.sort(key=lambda x: (-float(x["vcp_score"]), x["stock_id"]))
    return out


def _tick_float(row: dict, *keys: str) -> float | None:
    for key in keys:
        val = row.get(key)
        if val is not None:
            try:
                return float(val)
            except (TypeError, ValueError):
                continue
    return None


def _tick_stock_id(row: dict) -> str:
    for key in ("stock_id", "StockID", "data_id"):
        val = row.get(key)
        if val:
            return str(val).strip()
    return ""


def _pullback_from_high(day_high: float | None, price: float | None) -> float | None:
    if day_high is None or price is None or day_high <= 0:
        return None
    return round((day_high - price) / day_high * 100.0, 2)


def classify_intraday(
    pivot: float | None,
    price: float | None,
    *,
    day_high: float | None = None,
    fade_pullback_pct: float = DEFAULT_FADE_PULLBACK_PCT,
    hold_pullback_pct: float = DEFAULT_HOLD_PULLBACK_PCT,
    extended_pct: float = DEFAULT_EXTENDED_PCT,
) -> tuple[str, float | None, float | None]:
    """
    回傳 (intraday_status, dist_pivot_pct, pullback_from_high_pct)。
    """
    pullback = _pullback_from_high(day_high, price)
    if pivot is None or pivot <= 0 or price is None or price <= 0:
        return "UNKNOWN", None, pullback

    dist_pct = round((price - pivot) / pivot * 100.0, 2)

    if price < pivot * NEAR_BUFFER:
        return "SETUP", dist_pct, pullback

    if price < pivot * BREAKOUT_BUFFER:
        return "NEAR", dist_pct, pullback

    # 已站上 pivot（BREAKOUT 家族）
    if dist_pct >= extended_pct:
        return "BREAKOUT_EXTENDED", dist_pct, pullback

    if pullback is not None and pullback >= fade_pullback_pct:
        return "BREAKOUT_FADE", dist_pct, pullback

    if pullback is not None and pullback <= hold_pullback_pct:
        return "BREAKOUT_HOLD", dist_pct, pullback

    # 介於 hold 與 fade 之間
    return "BREAKOUT_FADE", dist_pct, pullback


def status_label(status: str) -> str:
    return STATUS_LABELS.get(status, status)


def action_hint(status: str) -> str:
    return ACTION_HINTS.get(status, "")


def build_watch_rows(
    watchlist: list[dict],
    tick_rows: list[dict],
    *,
    fade_pullback_pct: float = DEFAULT_FADE_PULLBACK_PCT,
    hold_pullback_pct: float = DEFAULT_HOLD_PULLBACK_PCT,
    extended_pct: float = DEFAULT_EXTENDED_PCT,
) -> list[WatchRow]:
    tick_by_id: dict[str, dict] = {}
    for tick in tick_rows:
        sid = _tick_stock_id(tick)
        if sid:
            tick_by_id[sid] = tick

    rows: list[WatchRow] = []
    for item in watchlist:
        sid = item["stock_id"]
        pivot = item.get("pivot_price")
        pivot_f = float(pivot) if pivot is not None else None
        tick = tick_by_id.get(sid, {})
        price = _tick_float(tick, "close", "Close", "deal_price", "price")
        day_open = _tick_float(tick, "open", "Open")
        day_high = _tick_float(tick, "high", "High")
        status, dist, pullback = classify_intraday(
            pivot_f,
            price,
            day_high=day_high,
            fade_pullback_pct=fade_pullback_pct,
            hold_pullback_pct=hold_pullback_pct,
            extended_pct=extended_pct,
        )
        rows.append(
            WatchRow(
                stock_id=sid,
                stock_name=item.get("stock_name") or "",
                vcp_score=float(item["vcp_score"]),
                execution_state=str(item.get("execution_state") or ""),
                pivot_price=pivot_f,
                model_id=str(item.get("model_id") or ""),
                price=price,
                day_open=day_open,
                day_high=day_high,
                dist_pivot_pct=dist,
                pullback_from_high_pct=pullback,
                intraday_status=status,
                action_hint=action_hint(status),
                entry_ready=bool(item.get("entry_ready")),
            )
        )
    return rows


def build_markdown(
    *,
    session_date: str,
    as_of_date: str,
    rows: list[WatchRow],
    tick_error: str | None,
    model_ids: tuple[str, ...],
) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        f"# VCP 盤中觀察 · {session_date}",
        "",
        f"> 產出 {now} · VCP as_of **{as_of_date}** · models `{','.join(model_ids)}` · states Pre-breakout/Breakout · **非交易主檔**",
        "",
        "**型態**：`突破·守穩` = 過 pivot 且距今日高點近 · `突破·回落` = 曾衝高但回吐 · `突破·過遠` = 離 pivot 已 >8%",
        "",
    ]
    if tick_error:
        lines.extend([f"⚠ FinMind tick：`{tick_error}`", ""])
    if not rows:
        lines.append("_無 VCP watchlist（請先跑 16:30 VCP screen）_")
        return "\n".join(lines)

    lines.extend(
        [
            "| 代號 | 名稱 | pivot | 現價 | 今日高 | 距pivot% | 自高點回落% | 型態 | execution | pivot來源 |",
            "|------|------|-------|------|--------|----------|-------------|------|-----------|-----------|",
        ]
    )
    for r in rows:
        pivot_s = f"{r.pivot_price:.0f}" if r.pivot_price else "—"
        price_s = f"{r.price:.0f}" if r.price is not None else "—"
        high_s = f"{r.day_high:.0f}" if r.day_high is not None else "—"
        dist_s = f"{r.dist_pivot_pct:+.1f}%" if r.dist_pivot_pct is not None else "—"
        pb_s = f"{r.pullback_from_high_pct:.1f}%" if r.pullback_from_high_pct is not None else "—"
        label = status_label(r.intraday_status)
        flag = f"**{label}**" if r.intraday_status.startswith("BREAKOUT") or r.intraday_status == "NEAR" else label
        lines.append(
            f"| {r.stock_id} | {r.stock_name} | {pivot_s} | {price_s} | {high_s} "
            f"| {dist_s} | {pb_s} | {flag} | {r.execution_state} | `{r.model_id}` |"
        )

    alerts = [r for r in rows if r.intraday_status in DEFAULT_ALERT_ON or r.intraday_status.startswith("BREAKOUT")]
    actionable = [r for r in rows if r.intraday_status in ("BREAKOUT_HOLD", "BREAKOUT_FADE", "BREAKOUT_EXTENDED", "NEAR")]

    lines.extend(["", "## 決策摘要", ""])
    if actionable:
        for r in actionable:
            pivot_s = f"{r.pivot_price:.0f}" if r.pivot_price else "—"
            price_s = f"{r.price:.0f}" if r.price is not None else "—"
            high_s = f"{r.day_high:.0f}" if r.day_high is not None else "—"
            pb = f"，自高點回落 {r.pullback_from_high_pct:.1f}%" if r.pullback_from_high_pct is not None else ""
            lines.append(
                f"- **{status_label(r.intraday_status)}** {r.stock_id} {r.stock_name} "
                f"pivot {pivot_s} 現 {price_s} 高 {high_s}{pb} — {r.action_hint}"
            )
    else:
        lines.append("_無需特別關注的接近/突破標的_")

    lines.extend(
        [
            "",
            "---",
            "",
            "決策：守穩 > 接近 > 整理；**回落** 需防假突破；**過遠** 不追。詳見 evening VCP brief。",
            "",
        ]
    )
    return "\n".join(lines)


def _load_alert_state(path: Path = ALERT_STATE_PATH) -> dict[str, list[str]]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): list(v) for k, v in data.items() if isinstance(v, list)}


def _save_alert_state(state: dict[str, list[str]], path: Path = ALERT_STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def filter_new_alerts(
    rows: list[WatchRow],
    *,
    session_date: str,
    alert_on: tuple[str, ...],
) -> list[WatchRow]:
    state = _load_alert_state()
    sent = set(state.get(session_date, []))
    fresh: list[WatchRow] = []
    for row in rows:
        if row.intraday_status not in alert_on:
            continue
        key = f"{row.stock_id}-{row.intraday_status}"
        if key in sent:
            continue
        fresh.append(row)
        sent.add(key)
    state[session_date] = sorted(sent)
    keep_dates = sorted(state.keys())[-14:]
    state = {d: state[d] for d in keep_dates}
    _save_alert_state(state)
    return fresh


def notify_mac(title: str, body: str) -> None:
    safe_title = title.replace('"', "'").replace("\\", "")
    safe_body = body.replace('"', "'").replace("\\", "")[:200]
    subprocess.run(
        [
            "osascript",
            "-e",
            f'display notification "{safe_body}" with title "{safe_title}" sound name "Glass"',
        ],
        check=False,
        capture_output=True,
    )


def send_watch_alerts(alerts: list[WatchRow], *, session_date: str) -> None:
    if not alerts:
        return
    lines = []
    for r in alerts:
        pivot_s = f"{r.pivot_price:.0f}" if r.pivot_price else "—"
        price_s = f"{r.price:.0f}" if r.price is not None else "—"
        high_s = f"{r.day_high:.0f}" if r.day_high is not None else "—"
        pb = f" 回落{r.pullback_from_high_pct:.1f}%" if r.pullback_from_high_pct is not None else ""
        lines.append(
            f"{status_label(r.intraday_status)} {r.stock_id} {r.stock_name} "
            f"pivot{pivot_s} 現{price_s} 高{high_s}{pb}"
        )
    body = "\n".join(lines)
    subject = f"[ETF研究] VCP 盤中 · {session_date} · {len(alerts)} 檔"
    notify_mac("VCP 盤中", body.split("\n")[0])
    if os.environ.get("RUN_VCP_INTRADAY_EMAIL", "1").strip() not in ("0", "false", "False"):
        try:
            from notify_email import send_alert

            send_alert(subject, body)
        except Exception as exc:
            print(f"VCP intraday: email notify failed: {exc}", flush=True)


def write_intraday_report(
    md: str,
    *,
    session_date: str,
    reports_dir: Path = REPORTS_DIR,
) -> Path:
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = session_date.replace("-", "")
    dated = reports_dir / f"{stamp}_vcp_intraday_watch.md"
    latest = reports_dir / "vcp_intraday_watch.md"
    dated.write_text(md, encoding="utf-8")
    latest.write_text(md, encoding="utf-8")
    return dated


def run_intraday_watch(
    conn: sqlite3.Connection,
    *,
    session_date: str | None = None,
    model_ids: tuple[str, ...] | None = None,
    min_score: float | None = None,
    alert_on: tuple[str, ...] | None = None,
    send_notifications: bool = True,
) -> tuple[Path | None, list[WatchRow], str | None]:
    if os.environ.get("RUN_VCP_INTRADAY_WATCH", "1").strip() in ("0", "false", "False"):
        return None, [], "RUN_VCP_INTRADAY_WATCH=0"

    session = session_date or date.today().isoformat()
    models = model_ids or _env_csv("VCP_INTRADAY_MODEL_IDS", DEFAULT_MODEL_IDS)
    exec_states = _env_csv("VCP_INTRADAY_EXECUTION_STATES", DEFAULT_EXECUTION_STATES)
    score_min = min_score if min_score is not None else _env_float("VCP_INTRADAY_MIN_SCORE", DEFAULT_MIN_SCORE)
    alerts_on = alert_on or _env_csv("VCP_INTRADAY_ALERT_ON", DEFAULT_ALERT_ON)
    fade_pct = _env_float("VCP_INTRADAY_FADE_PULLBACK_PCT", DEFAULT_FADE_PULLBACK_PCT)
    hold_pct = _env_float("VCP_INTRADAY_HOLD_PULLBACK_PCT", DEFAULT_HOLD_PULLBACK_PCT)
    extended_pct = _env_float("VCP_INTRADAY_EXTENDED_PCT", DEFAULT_EXTENDED_PCT)

    as_of = last_vcp_as_of(conn)
    if not as_of:
        md = build_markdown(
            session_date=session,
            as_of_date="—",
            rows=[],
            tick_error="vcp_screen_scores_v2 無資料",
            model_ids=models,
        )
        path = write_intraday_report(md, session_date=session)
        return path, [], "vcp_screen_scores_v2 無資料"

    watchlist = load_merged_watchlist(
        conn, as_of, model_ids=models, min_score=score_min, execution_states=exec_states
    )
    stock_ids = [w["stock_id"] for w in watchlist]
    tick_rows: list[dict] = []
    tick_error: str | None = None
    if stock_ids:
        tick_rows, tick_error = fetch_tick_snapshots(stock_ids)

    rows = build_watch_rows(
        watchlist,
        tick_rows,
        fade_pullback_pct=fade_pct,
        hold_pullback_pct=hold_pct,
        extended_pct=extended_pct,
    )
    md = build_markdown(
        session_date=session,
        as_of_date=as_of,
        rows=rows,
        tick_error=tick_error,
        model_ids=models,
    )
    path = write_intraday_report(md, session_date=session)

    if send_notifications:
        new_alerts = filter_new_alerts(rows, session_date=session, alert_on=alerts_on)
        send_watch_alerts(new_alerts, session_date=session)

    return path, rows, tick_error


def main() -> int:
    load_project_dotenv()
    from stock_db import connect

    conn = connect()
    try:
        path, rows, err = run_intraday_watch(conn)
    finally:
        conn.close()
    if path is None:
        print("VCP intraday: skipped (RUN_VCP_INTRADAY_WATCH=0)")
        return 0
    if err:
        print(f"VCP intraday: report={path} tick_warn={err} rows={len(rows)}")
    else:
        print(f"VCP intraday: report={path} rows={len(rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
