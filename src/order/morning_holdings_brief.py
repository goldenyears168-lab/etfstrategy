"""開盤前持倉 brief · 富邦 snapshot + 昨收結構 + 台指 gap。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from stock_db import DEFAULT_DB_PATH, PROJECT_ROOT, connect
from stock_db.order_holdings import OrderHoldingSnapshotRow, upsert_order_holdings_snapshot

_TZ = ZoneInfo("Asia/Taipei")
CORE4 = frozenset({"2327", "2449", "3264", "3211"})
SPECIAL_NO_2PCT = frozenset({"2327", "3264", "5347"})
_WEAK_QUADS = frozenset({"weakening", "lagging"})
_STRONG_QUADS = frozenset({"leading", "improving"})


@dataclass(frozen=True)
class HoldingBriefRow:
    stock_id: str
    stock_name: str
    shares: int
    cost_price: float | None
    unrealized_pnl: int
    prev_close: float | None
    bar_date: str | None
    rrg_quadrant: str | None
    rrg_seg: float | None
    rrg_session: str | None
    structure_tier: str
    gate_2pct: float | None
    trigger_s1b: float | None
    extension_spike: float | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class MorningBrief:
    generated_at: str
    session_date: str
    account_label: str
    cash_available: int | None
    bar_as_of: str | None
    rrg_as_of: str | None
    tx_gap_pct: float | None
    tx_price: float | None
    tw_spot_prev_close: float | None
    te_gap_pct: float | None
    morning_risk_captured_at: str | None
    morning_warnings: tuple[str, ...]
    gate_2330_995: float | None
    core4_gate_rows: tuple[HoldingBriefRow, ...]
    holdings: tuple[HoldingBriefRow, ...]
    fubon_error: str | None = None
    simulate: bool = False
    simulate_note: str | None = None
    morning_risk_note: str | None = None
    holdings_source: str = "none"
    morning_risk_source: str = "none"


def structure_tier(quadrant: str | None, *, vcp_state: str | None = None) -> str:
    overext = vcp_state and "Overextended" in str(vcp_state)
    if quadrant in _WEAK_QUADS or overext:
        return "weak"
    if quadrant in _STRONG_QUADS and not overext:
        return "strong"
    return "neutral"


def _stock_name(conn: sqlite3.Connection, stock_id: str) -> str:
    row = conn.execute(
        """
        SELECT stock_name FROM rrg_universe_scores
        WHERE stock_id = ? AND stock_name IS NOT NULL AND TRIM(stock_name) != ''
        ORDER BY session_date DESC LIMIT 1
        """,
        (stock_id,),
    ).fetchone()
    if row and row[0]:
        return str(row[0])
    row = conn.execute(
        """
        SELECT stock_name FROM etf_holdings
        WHERE stock_id = ? AND stock_name IS NOT NULL AND TRIM(stock_name) != ''
        ORDER BY snapshot_date DESC LIMIT 1
        """,
        (stock_id,),
    ).fetchone()
    return str(row[0]) if row and row[0] else ""


def _pit_bar_date(conn: sqlite3.Connection, as_of_session: str) -> str | None:
    row = conn.execute(
        """
        SELECT MAX(trade_date) FROM stock_daily_bars
        WHERE trade_date < ?
        """,
        (as_of_session,),
    ).fetchone()
    return str(row[0]) if row and row[0] else None


def _pit_rrg_session(conn: sqlite3.Connection, as_of_session: str) -> str | None:
    row = conn.execute(
        """
        SELECT MAX(session_date) FROM rrg_universe_scores
        WHERE screen_kind = 'close' AND session_date < ?
        """,
        (as_of_session,),
    ).fetchone()
    return str(row[0]) if row and row[0] else None


def _prev_close(
    conn: sqlite3.Connection,
    stock_id: str,
    *,
    as_of_session: str,
) -> tuple[float | None, str | None]:
    row = conn.execute(
        """
        SELECT close, trade_date FROM stock_daily_bars
        WHERE stock_id = ? AND trade_date < ?
        ORDER BY trade_date DESC LIMIT 1
        """,
        (stock_id, as_of_session),
    ).fetchone()
    if not row or row[0] is None:
        return None, None
    return float(row[0]), str(row[1])


def _rrg_row(
    conn: sqlite3.Connection,
    stock_id: str,
    *,
    as_of_session: str,
) -> tuple[str | None, float | None, str | None]:
    row = conn.execute(
        """
        SELECT quadrant, seg_last, session_date FROM rrg_universe_scores
        WHERE stock_id = ? AND screen_kind = 'close' AND session_date < ?
        ORDER BY session_date DESC LIMIT 1
        """,
        (stock_id, as_of_session),
    ).fetchone()
    if not row:
        return None, None, None
    seg = float(row[1]) if row[1] is not None else None
    return str(row[0]) if row[0] else None, seg, str(row[2]) if row[2] else None


def _holding_notes(
    stock_id: str,
    *,
    tier: str,
    unrealized_pnl: int,
    rrg_seg: float | None,
) -> tuple[str, ...]:
    notes: list[str] = []
    if stock_id in CORE4:
        notes.append("CORE4")
    if stock_id in SPECIAL_NO_2PCT:
        notes.append("禁-2%機械")
    if stock_id == "3008":
        notes.append("零股流動性低")
    if tier == "weak":
        notes.append("結構弱")
    if unrealized_pnl < -1500:
        notes.append("帳面大虧")
    elif unrealized_pnl > 1000:
        notes.append("有獲利可守")
    if rrg_seg is not None and rrg_seg < 0.3:
        notes.append("RRG seg低")
    return tuple(notes)


def _held_level_qty(node: dict[str, Any]) -> int:
    """單層（整股或零股）持有量 · 優先 today_qty，缺漏或 0 時退回 tradable_qty。

    tradable_qty 只計「當下可賣」數量；今日新買尚未交割者為 0，會被漏算，
    導致持倉與未實現損益（整倉）不一致。以 today_qty（整股 + 零股）為準。
    """
    today = int(node.get("today_qty", 0) or 0)
    if today > 0:
        return today
    return int(node.get("tradable_qty", 0) or 0)


def _shares_from_holding(row: dict[str, Any]) -> int:
    """持有股數（含今日買進、尚未交割）= 整股 today_qty + 零股 today_qty。"""
    whole = _held_level_qty(row)
    odd = row.get("odd") or {}
    odd_qty = _held_level_qty(odd) if isinstance(odd, dict) else 0
    return whole + odd_qty


def _pnl_by_symbol(unrealized: list[dict[str, Any]]) -> dict[str, tuple[int, float | None]]:
    out: dict[str, tuple[int, float | None]] = {}
    for item in unrealized:
        sym = str(item.get("stock_no", "") or "")
        if not sym:
            continue
        profit = int(item.get("unrealized_profit", 0) or 0)
        loss = int(item.get("unrealized_loss", 0) or 0)
        cost = item.get("cost_price")
        cost_f = float(cost) if cost is not None else None
        out[sym] = (profit - loss, cost_f)
    return out


def enrich_holdings(
    conn: sqlite3.Connection,
    holdings: list[dict[str, Any]],
    *,
    as_of_session: str,
    pnl_by_symbol: dict[str, tuple[int, float | None]] | None = None,
) -> list[HoldingBriefRow]:
    pnl_map = pnl_by_symbol or {}
    rows: list[HoldingBriefRow] = []
    for h in holdings:
        sym = str(h.get("stock_no", "") or "")
        if not sym:
            continue
        shares = _shares_from_holding(h)
        if shares <= 0:
            continue
        prev_close, bar_date = _prev_close(conn, sym, as_of_session=as_of_session)
        quad, seg, rrg_sess = _rrg_row(conn, sym, as_of_session=as_of_session)
        tier = structure_tier(quad)
        pnl, cost = pnl_map.get(sym, (0, None))
        if cost is None and sym in pnl_map:
            _, cost = pnl_map[sym]
        notes = _holding_notes(sym, tier=tier, unrealized_pnl=pnl, rrg_seg=seg)
        gate_2 = round(prev_close * 0.98, 2) if prev_close else None
        s1b = round(prev_close * 0.97, 2) if prev_close and tier == "weak" else None
        ext_spike = round(prev_close * 1.04, 2) if prev_close else None
        rows.append(
            HoldingBriefRow(
                stock_id=sym,
                stock_name=_stock_name(conn, sym),
                shares=shares,
                cost_price=cost,
                unrealized_pnl=pnl,
                prev_close=prev_close,
                bar_date=bar_date,
                rrg_quadrant=quad,
                rrg_seg=seg,
                rrg_session=rrg_sess,
                structure_tier=tier,
                gate_2pct=gate_2,
                trigger_s1b=s1b,
                extension_spike=ext_spike,
                notes=notes,
            )
        )
    rows.sort(key=lambda r: (r.structure_tier != "weak", r.unrealized_pnl, r.stock_id))
    return rows


def _gate_2330(conn: sqlite3.Connection, *, as_of_session: str) -> float | None:
    row = conn.execute(
        """
        SELECT close FROM stock_daily_bars
        WHERE stock_id = '2330' AND trade_date < ?
        ORDER BY trade_date DESC LIMIT 1
        """,
        (as_of_session,),
    ).fetchone()
    if not row or row[0] is None:
        return None
    return round(float(row[0]) * 0.995, 2)


def _load_morning_risk(
    conn: sqlite3.Connection,
    trade_date: str,
    *,
    allow_fallback: bool = True,
) -> tuple[dict[str, Any] | None, str | None]:
    row = conn.execute(
        """
        SELECT trade_date, captured_at, tx_gap_live_pct, tx_price, te_gap_live_pct,
               tw_spot_prev_close
        FROM morning_risk_snapshot
        WHERE trade_date = ?
        ORDER BY captured_at DESC LIMIT 1
        """,
        (trade_date,),
    ).fetchone()
    if row:
        return dict(row), None
    if not allow_fallback:
        return None, None
    row = conn.execute(
        """
        SELECT trade_date, captured_at, tx_gap_live_pct, tx_price, te_gap_live_pct,
               tw_spot_prev_close
        FROM morning_risk_snapshot
        WHERE trade_date <= ?
        ORDER BY trade_date DESC, captured_at DESC LIMIT 1
        """,
        (trade_date,),
    ).fetchone()
    if not row:
        return None, None
    d = dict(row)
    if str(d.get("trade_date")) != trade_date:
        return d, f"無 {trade_date} morning_risk；沿用最近 {d.get('trade_date')} 快照"
    return d, None


def fetch_fubon_snapshot() -> tuple[dict[str, Any] | None, str | None]:
    try:
        from order.fubon_account import account_snapshot
        from order.fubon_session import account_label, connect_fubon

        session = connect_fubon(realtime=False)
        snap = account_snapshot(session)
        acc = snap.get("account") or {}
        label = account_label(session.primary) if session.accounts else "—"
        if acc:
            label = f"{acc.get('name', '')} · {acc.get('branch_no', '')}-{acc.get('account', '')}"
        snap["_account_label"] = label.strip(" ·") or label
        return snap, None
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)


def try_refresh_morning_risk(
    conn: sqlite3.Connection,
    *,
    trade_date: str,
    sync: bool = False,
    dry_run: bool = True,
) -> tuple[dict[str, Any] | None, str | None]:
    if not sync and not dry_run:
        row, note = _load_morning_risk(conn, trade_date)
        return row, note
    try:
        from sync_morning_futures import build_morning_risk_row, fetch_futures_snapshots, morning_radar_warnings
        from sync_morning_futures import _snapshot_ids, format_morning_risk_line, upsert_morning_risk_snapshot

        tx_id, te_id = _snapshot_ids()
        rows, err = fetch_futures_snapshots([tx_id, te_id])
        if err:
            row, note = _load_morning_risk(conn, trade_date)
            return row, err if row is None else note
        captured_at = datetime.now(_TZ).replace(microsecond=0).isoformat()
        payload = build_morning_risk_row(
            conn,
            trade_date=trade_date,
            captured_at=captured_at,
            snapshot_rows=rows or [],
            tx_id=tx_id,
            te_id=te_id,
        )
        if payload is None:
            row, note = _load_morning_risk(conn, trade_date)
            return row, note or "無法組裝 morning_risk（缺 IX0001 昨收或期貨價）"
        if sync and not dry_run:
            upsert_morning_risk_snapshot(conn, payload)
        warnings = tuple(morning_radar_warnings(payload))
        payload["_warnings"] = warnings
        payload["_line"] = format_morning_risk_line(payload)
        return payload, None
    except Exception as exc:  # noqa: BLE001
        row, note = _load_morning_risk(conn, trade_date)
        return row, note or str(exc)


def build_morning_brief(
    conn: sqlite3.Connection,
    *,
    session_date: str | None = None,
    use_fubon: bool = True,
    sync_futures: bool = False,
    refresh_futures: bool = True,
    simulate: bool = False,
) -> MorningBrief:
    today = session_date or datetime.now(_TZ).date().isoformat()
    if simulate:
        generated_at = f"{today}T08:50:00+08:00"
    else:
        generated_at = datetime.now(_TZ).replace(microsecond=0).isoformat()
    simulate_note = None
    if simulate:
        simulate_note = (
            f"PIT 復盤：結構／昨收截至 {today} 開盤前；"
            "持倉／未實現為執行當下富邦 snapshot（非歷史還原）"
        )

    fubon_snap: dict[str, Any] | None = None
    fubon_error: str | None = None
    if use_fubon:
        fubon_snap, fubon_error = fetch_fubon_snapshot()

    holdings_raw: list[dict[str, Any]] = []
    cash_available: int | None = None
    account_label = "—"
    pnl_map: dict[str, tuple[int, float | None]] = {}
    holdings_source = "none"
    if fubon_snap:
        account_label = str(fubon_snap.get("_account_label") or "—")
        cash = fubon_snap.get("cash") or {}
        cash_available = int(cash.get("available_balance", 0) or 0)
        holdings_raw = list(fubon_snap.get("holdings") or [])
        pnl_map = _pnl_by_symbol(list(fubon_snap.get("unrealized_pnl") or []))
        holdings_source = "fubon"
    elif not use_fubon:
        holdings_source = "skipped"

    holdings = enrich_holdings(
        conn,
        holdings_raw,
        as_of_session=today,
        pnl_by_symbol=pnl_map,
    )
    core4_rows = tuple(r for r in holdings if r.stock_id in CORE4)

    morning_row: dict[str, Any] | None = None
    morning_warnings: tuple[str, ...] = ()
    morning_risk_note: str | None = None
    morning_risk_source = "none"
    if simulate:
        morning_row, morning_risk_note = _load_morning_risk(
            conn, today, allow_fallback=True
        )
        if morning_row:
            morning_risk_source = "db_fallback" if morning_risk_note else "db"
    elif refresh_futures or sync_futures:
        morning_row, morning_risk_note = try_refresh_morning_risk(
            conn,
            trade_date=today,
            sync=sync_futures,
            dry_run=not sync_futures,
        )
        morning_risk_source = "live_sync" if sync_futures else "live"
        if morning_row and morning_row.get("_warnings"):
            morning_warnings = tuple(morning_row["_warnings"])
    else:
        morning_row, morning_risk_note = _load_morning_risk(conn, today)
        if morning_row:
            morning_risk_source = "db_fallback" if morning_risk_note else "db"

    if morning_row is None:
        morning_row, morning_risk_note = _load_morning_risk(conn, today)
        if morning_row:
            morning_risk_source = "db_fallback" if morning_risk_note else "db"

    if morning_row and morning_row.get("tx_gap_live_pct") is not None and not morning_warnings:
        try:
            from sync_morning_futures import morning_radar_warnings

            morning_warnings = tuple(morning_radar_warnings(morning_row))
        except Exception:  # noqa: BLE001
            morning_warnings = ()

    return MorningBrief(
        generated_at=generated_at,
        session_date=today,
        account_label=account_label,
        cash_available=cash_available,
        bar_as_of=_pit_bar_date(conn, today),
        rrg_as_of=_pit_rrg_session(conn, today),
        tx_gap_pct=float(morning_row["tx_gap_live_pct"])
        if morning_row and morning_row.get("tx_gap_live_pct") is not None
        else None,
        tx_price=float(morning_row["tx_price"])
        if morning_row and morning_row.get("tx_price") is not None
        else None,
        tw_spot_prev_close=float(morning_row["tw_spot_prev_close"])
        if morning_row and morning_row.get("tw_spot_prev_close") is not None
        else None,
        te_gap_pct=float(morning_row["te_gap_live_pct"])
        if morning_row and morning_row.get("te_gap_live_pct") is not None
        else None,
        morning_risk_captured_at=str(morning_row["captured_at"])
        if morning_row and morning_row.get("captured_at")
        else None,
        morning_warnings=morning_warnings,
        gate_2330_995=_gate_2330(conn, as_of_session=today),
        core4_gate_rows=core4_rows,
        holdings=tuple(holdings),
        fubon_error=fubon_error,
        simulate=simulate,
        simulate_note=simulate_note,
        morning_risk_note=morning_risk_note,
        holdings_source=holdings_source,
        morning_risk_source=morning_risk_source,
    )


def format_morning_brief(brief: MorningBrief) -> str:
    lines: list[str] = [
        f"# 開盤前持倉 brief · {brief.session_date}",
        "",
        f"產生時間：{brief.generated_at}",
        f"帳戶：{brief.account_label}",
    ]
    if brief.cash_available is not None:
        lines.append(f"可用餘額：NT$ {brief.cash_available:,}")
    if brief.fubon_error:
        lines.append(f"富邦：⚠ {brief.fubon_error}")
    if brief.simulate_note:
        lines.append(f"模式：**PIT 復盤** · {brief.simulate_note}")
    lines.extend(
        [
            "",
            "## 資料來源",
            f"- 持倉／損益：**{brief.holdings_source}**（live 應為 fubon）",
            f"- 台指 gap：**{brief.morning_risk_source}**",
            f"- 結構／昨收：PIT · bar {brief.bar_as_of or '—'} · RRG {brief.rrg_as_of or '—'}",
            "",
        ]
    )

    lines.append("## 隔夜／台指 gap")
    if brief.morning_risk_note:
        lines.append(f"- _{brief.morning_risk_note}_")
    if brief.tx_gap_pct is not None:
        stale = ""
        if brief.morning_risk_captured_at:
            stale = f"（更新 {brief.morning_risk_captured_at}）"
        lines.append(
            f"- TX gap **{brief.tx_gap_pct:+.2f}%** @ {brief.tx_price or '—'}"
            f" · 前日加權 {brief.tw_spot_prev_close or '—'}{stale}"
        )
        if brief.te_gap_pct is not None:
            lines.append(f"- TE gap **{brief.te_gap_pct:+.2f}%**")
    else:
        lines.append("- 無 morning_risk 資料（可加上 `--sync-futures` 重拉）")
    for w in brief.morning_warnings:
        lines.append(f"- {w}")
    lines.append("")

    lines.append("## 09:05 組合閘門（intraday-exit-playbook v2）")
    lines.append(f"- 2330 09:05 ≤ **{brief.gate_2330_995 or '—'}**（昨收×0.995）")
    if brief.core4_gate_rows:
        for r in brief.core4_gate_rows:
            name = f" {r.stock_name}" if r.stock_name else ""
            lines.append(
                f"- {r.stock_id}{name} 09:05 ≤ **{r.gate_2pct or '—'}**（昨收×0.98）"
            )
        lines.append("- → CORE4 ≥2 檔觸發 A **且** 2330 觸發 B → portfolio_exit_mode=ON")
    else:
        lines.append("- 持倉中無 CORE4（2327/2449/3264/3211）")
    lines.append("")

    lines.append(f"## 持倉明細（昨收 {brief.bar_as_of or '—'} · RRG {brief.rrg_as_of or '—'}）")
    lines.append("")
    lines.append("| 代號 | 名稱 | 股數 | 昨收 | RRG | tier | 未實現 | 弱-3%參考 | ext≥4% | 備註 |")
    lines.append("|------|------|-----:|-----:|-----|------|-------:|-------:|-------:|------|")
    for r in brief.holdings:
        pc = f"{r.prev_close:.2f}" if r.prev_close else "—"
        quad = r.rrg_quadrant or "—"
        trig = f"≤{r.trigger_s1b:.2f}" if r.trigger_s1b else "—"
        ext = f"≥{r.extension_spike:.2f}" if r.extension_spike else "—"
        pnl = f"{r.unrealized_pnl:+,}"
        note = " · ".join(r.notes) if r.notes else "—"
        name = r.stock_name or "—"
        lines.append(
            f"| {r.stock_id} | {name} | {r.shares} | {pc} | {quad} | {r.structure_tier} "
            f"| {pnl} | {trig} | {ext} | {note} |"
        )
    lines.append("")

    if brief.holdings:
        lines.append("## 開盤情境（09:05 後才有效）")
        for r in brief.holdings:
            bits = [f"**{r.stock_id}** {r.stock_name or '—'}"]
            if r.trigger_s1b:
                bits.append(f"≤{r.trigger_s1b:.2f}（弱-3%參考）")
            if r.extension_spike:
                bits.append(f"≥{r.extension_spike:.2f} 後回落 1% → extension")
            if r.stock_id == "3008":
                bits.append("零股限價")
            lines.append(f"- {' · '.join(bits)}")
        lines.append("")

    weak = [r for r in brief.holdings if r.structure_tier == "weak"]
    big_loss = sorted(
        [r for r in brief.holdings if r.unrealized_pnl < -1500],
        key=lambda r: r.unrealized_pnl,
    )
    lines.append("## 開盤前優先盯")
    if weak:
        ids = "、".join(f"{r.stock_id}({r.stock_name or '—'})" for r in weak)
        lines.append(f"- **結構弱**：{ids}")
    if big_loss:
        ids = "、".join(
            f"{r.stock_id} {r.unrealized_pnl:+,}" for r in big_loss
        )
        lines.append(f"- **帳面大虧**：{ids}")
    if not weak and not big_loss:
        lines.append("- 無結構弱檔；留意 extension combo_spike（09:06 起 sell-signal-radar）")
    lines.append("")
    lines.append("## 紀律")
    lines.append("- 09:00–09:04 只觀察、不賣")
    lines.append("- 09:05 判斷組合閘門；09:06 起 sell-signal-radar（持倉交集才寄信）")
    lines.append("- 系統 advisory · 人工確認後下單")
    return "\n".join(lines)


def brief_to_snapshot_rows(brief: MorningBrief) -> list[OrderHoldingSnapshotRow]:
    source = "fubon" if brief.holdings_source == "fubon" else brief.holdings_source
    return [
        OrderHoldingSnapshotRow(
            snapshot_date=brief.session_date,
            stock_id=r.stock_id,
            stock_name=r.stock_name,
            shares=r.shares,
            avg_cost=r.cost_price,
            unrealized_pnl=r.unrealized_pnl,
            prev_close=r.prev_close,
            bar_date=r.bar_date,
            rrg_quadrant=r.rrg_quadrant,
            rrg_session=r.rrg_session,
            structure_tier=r.structure_tier,
            gate_2pct=r.gate_2pct,
            trigger_s1b=r.trigger_s1b,
            extension_spike=r.extension_spike,
            notes=r.notes,
            source=source,
            synced_at=brief.generated_at,
        )
        for r in brief.holdings
        if r.shares > 0
    ]


def sync_morning_brief_to_db(conn: sqlite3.Connection, brief: MorningBrief) -> int:
    rows = brief_to_snapshot_rows(brief)
    return upsert_order_holdings_snapshot(conn, rows)


def default_report_path(session_date: str) -> Path:
    stamp = session_date.replace("-", "")
    return PROJECT_ROOT / "reports" / "order" / "snapshots" / f"morning_brief_{stamp}.md"


def write_morning_brief(brief: MorningBrief, path: Path | None = None) -> Path:
    out = path or default_report_path(brief.session_date)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(format_morning_brief(brief), encoding="utf-8")
    return out
