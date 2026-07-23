"""Plain-language order fill / submit email for Order layer (C18acc · ABC)."""

from __future__ import annotations

import os
from typing import Any

_NOTIFY_STATUSES = frozenset(
    {
        "submitted",
        "reconciled",
        "working",
        "filled",
        "partial",
        "ambiguous",
        "failed",
    }
)

_STATUS_ZH = {
    "filled": "已成交",
    "partial": "部成",
    "submitted": "已送出（待成交）",
    "reconciled": "已對上券商委託",
    "working": "委託中",
    "ambiguous": "狀態不明（請查 App）",
    "failed": "送單失敗",
    "dry_run": "模擬（未進券商）",
    "skipped": "略過",
}


def _env_flag(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default).strip().lower() not in ("0", "false", "no", "off")


def _lifecycle(row: dict[str, Any]) -> str:
    return str(row.get("lifecycle_status") or row.get("status") or "").strip().lower()


def _side_zh(side: str) -> str:
    s = (side or "").lower()
    if s == "buy":
        return "買進"
    if s == "sell":
        return "賣出"
    return side or "—"


def _kind_zh(kind: str) -> str:
    k = (kind or "").lower()
    return {
        "entry": "新開倉",
        "swap": "換倉",
        "exit": "出場",
        "pyramid_add": "加碼",
        "pyramid_add_buy": "加碼",
        "champion_tp_sell": "Champion TP 出場",
        "hold_cap_sell": "持有到期出場",
    }.get(k, kind or "委託")


def _fmt_px(val: float | None) -> str:
    if val is None:
        return "—"
    if abs(val) >= 100:
        return f"{val:.1f}"
    return f"{val:.2f}"


def _fmt_pct(val: float | None) -> str:
    if val is None:
        return "—"
    return f"{val:+.2f}%"


def _fmt_money(val: float | int | None) -> str:
    if val is None:
        return "—"
    try:
        return f"{float(val):,.0f}"
    except (TypeError, ValueError):
        return str(val)


def row_should_notify(row: dict[str, Any]) -> bool:
    if not isinstance(row, dict):
        return False
    if row.get("notify_submit") is False:
        return False
    status = _lifecycle(row)
    if status in _NOTIFY_STATUSES:
        return True
    return bool(row.get("notify_submit"))


def format_row_block(row: dict[str, Any], *, sleeve: str) -> list[str]:
    status = _lifecycle(row)
    status_zh = _STATUS_ZH.get(status, status or "—")
    side = str(row.get("side") or "buy")
    kind = str(row.get("action_kind") or row.get("order_kind") or row.get("kind") or "")
    sym = str(row.get("symbol") or "?")
    name = str(row.get("stock_name") or "").strip()
    name_bit = f" {name}" if name else ""
    qty = int(row.get("quantity_shares") or row.get("qty") or 0)
    filled = int(row.get("filled_qty") or 0)
    ask = row.get("ask_price") or row.get("price") or row.get("estimated_ask")
    avg = row.get("avg_fill_price")
    notional = row.get("notional_twd")
    cid = str(row.get("client_intent_id") or "")
    order_no = row.get("order_no")
    session = row.get("session_date") or ""
    poll = row.get("poll_minute") or ""
    note = str(row.get("note") or row.get("reason") or row.get("reentry") or "").strip()
    counter = row.get("counterparty_id")
    counter_name = row.get("counterparty_name")

    head = f"{status_zh} · {sleeve} · {_kind_zh(kind)} · {_side_zh(side)} {sym}{name_bit}"
    lines = [
        "【執行摘要】",
        head,
    ]
    if filled > 0 and qty > 0:
        lines.append(f"成交 {filled}/{qty} 股" + (f" · 均價 {avg}" if avg else ""))
    elif qty > 0:
        lines.append(f"委託 {qty} 股" + (f" · 參考價 {ask}" if ask else ""))
    if avg is not None and filled > 0:
        lines.append(f"成交均價：{avg}")
    elif ask is not None:
        lines.append(f"委託／參考價：{ask}")
    if notional is not None:
        try:
            lines.append(f"金額：約 NT$ {float(notional):,.0f}")
        except (TypeError, ValueError):
            lines.append(f"金額：{notional}")
    if session or poll:
        lines.append(f"時間：{session} {poll}".strip())
    if counter:
        cn = f" {counter_name}" if counter_name else ""
        lines.append(f"對腿：{counter}{cn}")

    lines.extend(["", "【委託明細】"])
    if order_no:
        lines.append(f"券商委託書號：{order_no}")
    if cid:
        lines.append(f"client_intent_id：{cid}")
    lines.append(f"lifecycle：{status or '—'}")
    if note:
        lines.append(f"理由／備註：{note}")

    lines.extend(
        [
            "",
            "【審計】",
            "Order layer（下單層）· 請以富邦 App 成交回報為準",
            "本信為系統自動通知 · 非研究結論",
        ]
    )
    return lines


def format_fill_subject(rows: list[dict[str, Any]], *, sleeve: str) -> str:
    notify_rows = [r for r in rows if row_should_notify(r)]
    if not notify_rows:
        return f"[ORDER] {sleeve} · 無成交列"
    row = notify_rows[0]
    status = _lifecycle(row).upper() or "ORDER"
    side = str(row.get("side") or "buy").upper()
    sym = str(row.get("symbol") or "?")
    qty = int(row.get("filled_qty") or row.get("quantity_shares") or 0)
    px = row.get("avg_fill_price") or row.get("ask_price") or row.get("price") or ""
    extra = f" · +{len(notify_rows) - 1}" if len(notify_rows) > 1 else ""
    return f"[ORDER|{status}] {sleeve} {side} {sym} · {qty}股 @ {px}{extra}"


def holdings_status_rows_from_pulse(pulse: Any) -> list[dict[str, Any]]:
    """Serialize HoldingsPulse rows for email（含成本／市值／漲跌／損益）。"""
    rows: list[dict[str, Any]] = []
    for r in getattr(pulse, "holdings", ()) or ():
        base = r.base
        cost = base.cost_price
        shares = int(base.shares or 0)
        cost_basis = (cost * shares) if cost is not None and shares else None
        rows.append(
            {
                "stock_id": base.stock_id,
                "stock_name": base.stock_name or "",
                "shares": shares,
                "current_price": r.current_price,
                "prev_close": base.prev_close,
                "daily_pct": r.daily_pct,
                "cost_price": cost,
                "cost_basis": cost_basis,
                "market_value": r.market_value,
                "unrealized_pnl": base.unrealized_pnl,
                "pnl_pct": r.pnl_pct,
                "price_source": r.price_source,
            }
        )
    return rows


def format_holdings_status_section(
    rows: list[dict[str, Any]],
    *,
    cash_available: int | None = None,
    total_market_value: float | None = None,
    total_unrealized_pnl: int | None = None,
    error: str | None = None,
) -> str:
    lines = ["【持倉狀態】"]
    if error:
        lines.append(f"⚠ 持倉讀取：{error}")
    summary_bits: list[str] = []
    if cash_available is not None:
        summary_bits.append(f"可用現金 NT$ {_fmt_money(cash_available)}")
    if total_market_value is not None:
        summary_bits.append(f"持倉市值 NT$ {_fmt_money(total_market_value)}")
    if total_unrealized_pnl is not None:
        summary_bits.append(f"未實現 {_fmt_money(total_unrealized_pnl)}")
    if summary_bits:
        lines.append(" · ".join(summary_bits))
    if not rows:
        lines.append("（目前無持股資料）")
        return "\n".join(lines)

    lines.append("")
    for r in rows:
        sid = str(r.get("stock_id") or "?")
        name = str(r.get("stock_name") or "—")
        lines.append(f"{sid} {name}")
        lines.append(
            f"  持股 {int(r.get('shares') or 0)} 股 · 現價 {_fmt_px(r.get('current_price'))}"
            f" · 今日漲跌 {_fmt_pct(r.get('daily_pct'))}"
        )
        lines.append(
            f"  成本均價 {_fmt_px(r.get('cost_price'))} · 投資成本 NT$ {_fmt_money(r.get('cost_basis'))}"
            f" · 市值 NT$ {_fmt_money(r.get('market_value'))}"
        )
        lines.append(
            f"  損益 NT$ {_fmt_money(r.get('unrealized_pnl'))} · 損益率 {_fmt_pct(r.get('pnl_pct'))}"
            f" · 昨收 {_fmt_px(r.get('prev_close') if isinstance(r.get('prev_close'), (int, float)) else None)}"
            f" · 報價來源 {r.get('price_source') or '—'}"
        )
        lines.append("")
    return "\n".join(lines).rstrip()


def collect_holdings_status_snapshot() -> dict[str, Any]:
    """Fetch holdings+quotes for email. Uses .venv-fubon subprocess when host is 3.14+."""
    subprocess_err: str | None = None
    try:
        from order.fubon_subprocess import host_needs_fubon_venv, run_fubon_order_worker

        if host_needs_fubon_venv():
            out = run_fubon_order_worker(
                {"kind": "holdings_status", "dry_run": True},
                timeout_sec=90.0,
            )
            if isinstance(out, dict):
                return out
            return {"rows": [], "error": "holdings_status_bad_result"}
    except Exception as exc:  # noqa: BLE001 — email path must not crash fill notify
        subprocess_err = str(exc)

    try:
        from stock_db import DEFAULT_DB_PATH, connect
        from order.holdings_pulse import build_holdings_pulse

        conn = connect(DEFAULT_DB_PATH)
        try:
            pulse = build_holdings_pulse(
                conn,
                sync_rrg_intraday=False,
                refresh_morning_risk=False,
            )
        finally:
            conn.close()
        return {
            "rows": holdings_status_rows_from_pulse(pulse),
            "cash_available": pulse.cash_available,
            "total_market_value": pulse.total_market_value,
            "total_unrealized_pnl": pulse.total_unrealized_pnl,
            "error": pulse.fubon_error or subprocess_err,
        }
    except Exception as exc:  # noqa: BLE001
        err = str(exc)
        if subprocess_err:
            err = f"{subprocess_err}; in_process:{err}"
        return {"rows": [], "error": err}


def format_fill_body(
    rows: list[dict[str, Any]],
    *,
    sleeve: str,
    header: str = "",
    include_holdings: bool = True,
) -> str:
    notify_rows = [r for r in rows if row_should_notify(r)]
    parts: list[str] = []
    if header:
        parts.append(header.strip())
        parts.append("")
    if not notify_rows:
        parts.append(f"{sleeve}：本輪無須通知的委託列。")
    else:
        for i, row in enumerate(notify_rows):
            if i:
                parts.append("")
                parts.append("────────")
                parts.append("")
            parts.extend(format_row_block(row, sleeve=sleeve))

    if include_holdings and _env_flag("ORDER_FILL_EMAIL_HOLDINGS", "1"):
        snap = collect_holdings_status_snapshot()
        parts.append("")
        parts.append(
            format_holdings_status_section(
                list(snap.get("rows") or []),
                cash_available=snap.get("cash_available"),
                total_market_value=snap.get("total_market_value"),
                total_unrealized_pnl=snap.get("total_unrealized_pnl"),
                error=snap.get("error"),
            )
        )
    return "\n".join(parts).strip()


def maybe_send_fill_report(
    rows: list[dict[str, Any]],
    *,
    sleeve: str,
    env_flag: str,
    header: str = "",
) -> bool:
    """Send plain fill email if enabled. Returns True when sent."""
    notify_rows = [r for r in rows if row_should_notify(r)]
    if not notify_rows:
        return False
    if not _env_flag(env_flag, "1"):
        return False
    from notify_email import send_alert

    subject = format_fill_subject(notify_rows, sleeve=sleeve)
    body = format_fill_body(notify_rows, sleeve=sleeve, header=header, include_holdings=True)
    send_alert(subject, body)
    print(f"ORDER_FILL_REPORT_SENT=1 sleeve={sleeve} n={len(notify_rows)}")
    return True


_SPREAD_ZH = {
    "mixed": "mixed（W5/W20 方向不一致 · 略過）",
    "pullback": "pullback（回撤 · 較佳）",
    "aligned": "aligned（雙腿貼齊）",
    "extension": "extension（延伸）",
}


def format_c18acc_pool_digest(
    *,
    session_date: str,
    poll_minute: str,
    pool_as_of: str,
    pool: list[dict[str, Any]],
    slots: list[dict[str, Any]],
    lead: str,
    lead_drift: str,
    entry_gate: str,
    blocker: str,
    spread_gate_snapshot: dict[str, Any] | None,
    entry_confirm: dict[str, Any] | None = None,
    actions_n: int = 0,
    header: str = "",
) -> tuple[str, str]:
    """Return (subject, body) for E@NTB candidate-pool digest (default 13:00)."""
    n = len(pool)
    subject = f"[C18acc|POOL] {session_date} {poll_minute} · 候選 {n} 檔"
    if blocker and blocker != "—":
        subject += " · 有擋單"
    lines: list[str] = []
    if header:
        lines.append(header.strip())
        lines.append("")
    lines.extend(
        [
            f"【C18acc 候選池】{session_date} · poll {poll_minute}",
            f"pool_as_of（信號日）：{pool_as_of or '—'}",
            f"進場狀態：{entry_gate or '—'}",
            f"領先者：{lead or '—'}",
            f"領先變化：{lead_drift or '—'}",
            f"擋單原因：{blocker or '—'}",
            f"本輪動作數：{actions_n}",
            "",
            "【候選名單】",
        ]
    )
    if not pool:
        lines.append("（空池）")
    else:
        confirm = entry_confirm or {}
        snap = {
            str(k): v
            for k, v in (spread_gate_snapshot or {}).items()
            if not str(k).startswith("_")
        }
        for i, row in enumerate(pool, 1):
            sid = str(row.get("stock_id") or "?")
            name = str(row.get("stock_name") or "")
            seg = row.get("seg_last")
            disp = row.get("disp")
            sc = snap.get(sid)
            sc_zh = _SPREAD_ZH.get(str(sc), str(sc) if sc else "（尚無 spread）")
            conf = confirm.get(sid, confirm.get(str(sid), "—"))
            lines.append(
                f"{i}. {sid} {name} · seg={seg} · disp={disp} · confirm={conf}"
            )
            lines.append(f"   spread：{sc_zh}")
            if sc == "mixed":
                lines.append(
                    "   → 擋下：avoid mixed 閘門（W5 與 W20 相對強弱／動量不同向）"
                )
    lines.extend(["", "【目前槽位】"])
    if not slots:
        lines.append("（空倉）")
    else:
        for s in slots:
            lines.append(
                f"- slot{s.get('slot')}: {s.get('stock_id')} {s.get('stock_name') or ''}"
                f" · 進場 {s.get('entry_date')} @ {s.get('entry_px')}"
            )
    lines.extend(
        [
            "",
            "【白話說明 · mixed】",
            "spread_class 看盤中 WMA5 與 WMA20 的相對位置：",
            "· pullback：短腿相對長腿回撤（較常允許進場）",
            "· aligned：兩腿貼近",
            "· extension：短腿同向延伸",
            "· mixed：強弱／動量一正一負（結構雜訊）→ C18acc 預設略過",
            "研究證據：avoid mixed 採納後 OOS 超額約 +1.4pp。",
            "",
            "【審計】Order layer · 本信為候選／擋單摘要 · 非成交回報",
        ]
    )
    return subject, "\n".join(lines)


def maybe_send_c18acc_pool_digest(
    *,
    session_date: str,
    poll_minute: str,
    pool_as_of: str,
    pool: list[dict[str, Any]],
    slots: list[dict[str, Any]],
    lead: str = "",
    lead_drift: str = "",
    entry_gate: str = "",
    blocker: str = "",
    spread_gate_snapshot: dict[str, Any] | None = None,
    entry_confirm: dict[str, Any] | None = None,
    actions_n: int = 0,
    header: str = "",
    env_flag: str = "RUN_C18ACC_POOL_DIGEST_EMAIL",
    only_at_minute: str | None = "13:00",
) -> bool:
    """Email E@NTB pool + blockers. Default only at 13:00 (open) poll."""
    if only_at_minute and poll_minute != only_at_minute:
        return False
    if not _env_flag(env_flag, "1"):
        return False
    if not pool and not blocker:
        return False
    from notify_email import send_alert

    subject, body = format_c18acc_pool_digest(
        session_date=session_date,
        poll_minute=poll_minute,
        pool_as_of=pool_as_of,
        pool=pool,
        slots=slots,
        lead=lead,
        lead_drift=lead_drift,
        entry_gate=entry_gate,
        blocker=blocker,
        spread_gate_snapshot=spread_gate_snapshot,
        entry_confirm=entry_confirm,
        actions_n=actions_n,
        header=header,
    )
    send_alert(subject, body)
    print(f"C18ACC_POOL_DIGEST_SENT=1 minute={poll_minute} pool_n={len(pool)}")
    return True
