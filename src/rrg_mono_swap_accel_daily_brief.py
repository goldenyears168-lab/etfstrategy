"""RRG mono swap-accel（C18acc）· 16:30 收盤診斷 brief（Scheme A · 不下單）。

PIT as_of = 當日收盤；鎖定隔日盤中候選池（fresh mono 全池 · 依 seg_last 排序）。
"""

from __future__ import annotations

import argparse
import sqlite3
from dataclasses import dataclass
from typing import Any

from market_benchmark import load_benchmark_close
from project_config import DEFAULT_ETF_CODES
from report_paths import REPORTS_DIR
from research.backtest.finpilot_local_backtest import load_price_panels
from research.backtest.rrg_mono_score_swap_c import (
    RRG_MONO_SWAP_ACCEL_SHORT,
    RRG_MONO_SWAP_ACCEL_SLUG,
    ScoreSwapCConfig,
    _pick_swap_pair,
    _buy_threshold_score,
    _trading_days_between,
    candidate_shortlist_is_passthrough,
    champion_score_swap_c_config,
)
from rrg_mono_daily_brief import (
    TOP_N,
    ScanRow,
    _latest_trading_date,
    scan_rows_from_panels,
)
from rrg_mono_swap_accel_screen import (
    _build_accel_maps,
    _signal_rrg_panels,
    load_slot_state,
)

REPORTS = REPORTS_DIR
BRIEF_KIND = "post_close_diagnostic"
EXECUTION_NOTE_ZH = (
    "本 brief 為收盤後診斷（Scheme A）· 不產下單 intent · "
    "隔日盤中執行見 C0 scale + 5m poll live screen。"
)


@dataclass
class SwapProximityRow:
    stock_id: str
    stock_name: str
    hold_days: int
    seg_last: float
    avg_accel: float | None
    sell_eligible: bool
    best_challenger_id: str | None
    best_challenger_seg: float | None
    margin_required: float
    margin_gap: float | None


def _breadth_zone_for(conn: sqlite3.Connection, as_of: str) -> tuple[str | None, str | None]:
    try:
        from market_breadth_ma import BREADTH_ZONE_ZH, build_breadth_panel

        panel = build_breadth_panel(conn)
        zone_by_date = {str(r.trade_date): str(r.zone_200) for r in panel.itertuples()}
        zone = zone_by_date.get(as_of)
        if not zone:
            return None, None
        return zone, BREADTH_ZONE_ZH.get(zone, zone)  # type: ignore[arg-type]
    except Exception:
        return None, None


def _tomorrow_pool(
    conn: sqlite3.Connection,
    as_of: str,
    close,
    bench,
    *,
    etf_codes: tuple[str, ...] = DEFAULT_ETF_CODES,
) -> tuple[list[ScanRow], int]:
    _all_mono, fresh_mono = scan_rows_from_panels(
        conn, as_of, close, bench, etf_codes=etf_codes
    )
    ranked = sorted(fresh_mono, key=lambda r: (-r.seg_last, r.stock_id))
    return ranked, len(fresh_mono)


def _swap_proximity_rows(
    *,
    config: ScoreSwapCConfig,
    pool: list[ScanRow],
    slots: list[dict[str, Any]],
    held_today: dict[str, float],
    challenger_avg_accel: dict[str, float],
    session_dates: list[str],
    as_of: str,
) -> list[SwapProximityRow]:
    held_ids = {str(p["stock_id"]) for p in slots}
    if candidate_shortlist_is_passthrough(config):
        eligible = [r for r in pool if r.stock_id not in held_ids]
    else:
        top_n = max(1, int(config.candidate_top_n))
        eligible = [r for r in pool[:top_n] if r.stock_id not in held_ids]
    margin = config.effective_margin
    rows: list[SwapProximityRow] = []

    for pos in sorted(slots, key=lambda p: int(p.get("slot", 0))):
        sid = str(pos["stock_id"])
        entry = str(pos.get("entry_date") or pos.get("signal_date") or as_of)
        hold_days = _trading_days_between(session_dates, entry, as_of)
        seg = float(pos.get("seg_last") or 0.0)
        accel = held_today.get(sid)
        sell_eligible = True
        if config.accel_sell_negative_only:
            sell_eligible = accel is not None and accel < 0
        sell_eligible = sell_eligible and hold_days >= config.min_hold_days

        threshold = _buy_threshold_score(pos, config.sort_key) + margin
        beats = [r for r in eligible if float(r.seg_last) > threshold]
        best: ScanRow | None = None
        if beats and config.buy_sort_key == "avg_accel_decel":
            scored = [
                (r, challenger_avg_accel[r.stock_id])
                for r in beats
                if r.stock_id in challenger_avg_accel
            ]
            best = max(scored, key=lambda x: x[1])[0] if scored else None
        elif beats:
            best = max(beats, key=lambda r: float(r.seg_last))

        gap: float | None = None
        if best is not None:
            gap = float(best.seg_last) - threshold

        rows.append(
            SwapProximityRow(
                stock_id=sid,
                stock_name=str(pos.get("stock_name") or ""),
                hold_days=hold_days,
                seg_last=seg,
                avg_accel=accel,
                sell_eligible=sell_eligible,
                best_challenger_id=best.stock_id if best else None,
                best_challenger_seg=float(best.seg_last) if best else None,
                margin_required=margin,
                margin_gap=gap,
            )
        )
    return rows


def build_payload(
    conn: sqlite3.Connection,
    *,
    as_of: str,
    config: ScoreSwapCConfig | None = None,
    etf_codes: tuple[str, ...] = DEFAULT_ETF_CODES,
) -> dict[str, Any]:
    cfg = config or champion_score_swap_c_config()
    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index)
    pool, fresh_n = _tomorrow_pool(conn, as_of, close, bench, etf_codes=etf_codes)
    state = load_slot_state()
    slots = list(state.get("slots") or [])

    _close_sig, _bench_sig, rs_ratio, rs_mom, signal_dates = _signal_rrg_panels(
        close, bench, as_of
    )
    session_dates = close.index.astype(str).tolist()
    held_today, _held_trend, _chall_trend, _chall_va, chall_avg = _build_accel_maps(
        config=cfg,
        pool=pool,
        slots=slots,
        rs_ratio=rs_ratio,
        rs_mom=rs_mom,
        full_dates=signal_dates,
        as_of=as_of,
    )
    proximity = _swap_proximity_rows(
        config=cfg,
        pool=pool,
        slots=slots,
        held_today=held_today,
        challenger_avg_accel=chall_avg,
        session_dates=session_dates,
        as_of=as_of,
    )
    sell, buy = _pick_swap_pair(
        slots,
        pool,
        held_ids={str(p["stock_id"]) for p in slots},
        config=cfg,
        held_today=held_today,
        challenger_avg_accel=chall_avg,
    )
    zone, zone_zh = _breadth_zone_for(conn, as_of)

    return {
        "as_of": as_of,
        "brief_kind": BRIEF_KIND,
        "strategy_id": RRG_MONO_SWAP_ACCEL_SLUG,
        "short_name": RRG_MONO_SWAP_ACCEL_SHORT,
        "config": cfg,
        "tomorrow_pool": pool,
        "pool_fresh_n": fresh_n,
        "slots": slots,
        "held_accel": held_today,
        "challenger_accel": chall_avg,
        "proximity": proximity,
        "hypothetical_swap_sell": sell,
        "hypothetical_swap_buy": buy,
        "breadth_zone": zone,
        "breadth_zone_zh": zone_zh,
        "session_dates": session_dates,
    }


def _fmt_accel(v: float | None) -> str:
    if v is None or v != v:
        return "—"
    return f"{v:+.3f}"


def _fmt_gap(v: float | None) -> str:
    if v is None or v != v:
        return "—"
    sign = "+" if v >= 0 else ""
    return f"{sign}{v:.3f}"


def render_markdown(payload: dict[str, Any]) -> str:
    as_of = str(payload["as_of"])
    cfg: ScoreSwapCConfig = payload["config"]
    pool: list[ScanRow] = payload["tomorrow_pool"]
    slots: list[dict[str, Any]] = payload["slots"]
    proximity: list[SwapProximityRow] = payload["proximity"]
    sell = payload.get("hypothetical_swap_sell")
    buy = payload.get("hypothetical_swap_buy")
    zone_zh = payload.get("breadth_zone_zh")

    lines = [
        f"# RRG mono swap-accel（C18acc）每日診斷 · {as_of}",
        "",
        f"> **Scheme A** · 收盤後 PIT · 隔日候選池 · **不下單** · variant `{cfg.variant_id}`",
        "",
        "## 隔日候選池（fresh mono · 依軌跡排序 · 全池）",
        "",
        f"信號日 **{as_of}** · 池內 fresh **{payload['pool_fresh_n']}** 檔 · "
        f"隔日盤中 screen 鎖定下列全池（不裁 top10）。",
        "",
    ]
    if not pool:
        lines.append("_信號日無 fresh mono 候選。_")
    else:
        lines.append("| # | 代號 | 名稱 | seg_last | 位移 | 四日加速 |")
        lines.append("|---|------|------|----------|------|----------|")
        chall_accel: dict[str, float] = payload.get("challenger_accel") or {}
        for i, r in enumerate(pool[:TOP_N], 1):
            lines.append(
                f"| {i} | {r.stock_id} | {r.stock_name} | {r.seg_last:.3f} | "
                f"{r.disp:.2f} | {_fmt_accel(chall_accel.get(r.stock_id))} |"
            )
    lines.append("")

    lines.extend(["## 持倉（3 槽 state · 唯讀）", ""])
    if not slots:
        lines.append("_空槽。_")
    else:
        lines.append("| 槽 | 代號 | 名稱 | 進場 | hold | seg_last | 四日加速 |")
        lines.append("|---|------|------|------|------|----------|----------|")
        held_accel: dict[str, float] = payload.get("held_accel") or {}
        for p in sorted(slots, key=lambda x: int(x.get("slot", 0))):
            entry = str(p.get("entry_date") or p.get("signal_date") or "")
            hold = _trading_days_between(payload["session_dates"], entry, as_of) if entry else "—"
            sid = str(p["stock_id"])
            lines.append(
                f"| {int(p.get('slot', 0)) + 1} | {sid} | {p.get('stock_name', '')} | "
                f"{entry} | {hold} | {float(p.get('seg_last') or 0):.3f} | "
                f"{_fmt_accel(held_accel.get(sid))} |"
            )
    lines.append("")

    lines.extend(
        [
            "## 換倉門檻接近度",
            "",
            f"margin **{cfg.effective_margin:.2f}** · min_hold **{cfg.min_hold_days}** 日 · "
            f"max_hold **{cfg.max_hold_days}** 日 · 賣 **avg_accel<0** · 買 **avg_accel 最大**",
            "",
        ]
    )
    if not proximity:
        lines.append("_無持倉或無候選，略過門檻表。_")
    else:
        lines.append("| 代號 | hold | 四日加速 | 可賣 | 最佳 challenger | Δmargin |")
        lines.append("|------|------|----------|------|-----------------|--------|")
        for row in proximity:
            ok = "✓" if row.sell_eligible else "—"
            chall = row.best_challenger_id or "—"
            if row.best_challenger_seg is not None:
                chall = f"{chall} ({row.best_challenger_seg:.3f})"
            lines.append(
                f"| {row.stock_id} | {row.hold_days} | {_fmt_accel(row.avg_accel)} | "
                f"{ok} | {chall} | {_fmt_gap(row.margin_gap)} |"
            )
    lines.append("")

    lines.extend(["## 假設換倉（收盤 PIT · 非實單）", ""])
    if sell and buy:
        lines.append(
            f"- 賣 **{sell['stock_id']}** {sell.get('stock_name', '')} → "
            f"買 **{buy.stock_id}** {buy.stock_name}"
        )
    else:
        lines.append("_滿槽條件下暫無符合 margin / min_hold / 加速 gate 的換倉對。_")
    lines.append("")

    if zone_zh:
        lines.extend(["## Market breadth（市場廣度）", "", f"200MA 分區：**{zone_zh}**（{as_of}）", ""])
    else:
        lines.extend(["## Market breadth（市場廣度）", "", "_廣度分區資料不可用。_", ""])

    lines.extend(
        [
            "## 持有規則備註",
            "",
            f"- **min_hold** {cfg.min_hold_days} 交易日 · **max_hold** {cfg.max_hold_days} 交易日",
            f"- 盤中：**{cfg.timing_mode}** · poll {cfg.poll_interval_min}m · "
            f"no swap before {cfg.no_trade_before}",
            f"- 進場腿：**{cfg.entry_leg}** scale confirm",
            "",
            "---",
            EXECUTION_NOTE_ZH,
        ]
    )
    return "\n".join(lines) + "\n"


def build_brief(
    conn: sqlite3.Connection,
    *,
    as_of: str | None = None,
    etf_codes: tuple[str, ...] = DEFAULT_ETF_CODES,
) -> dict[str, Any]:
    trade_date = as_of or _latest_trading_date(conn)
    payload = build_payload(conn, as_of=trade_date, etf_codes=etf_codes)
    md = render_markdown(payload)
    stamp = trade_date.replace("-", "")
    out_dated = REPORTS / f"{stamp}_rrg_mono_swap_accel_daily.md"
    out_latest = REPORTS / "rrg_mono_swap_accel_daily.md"
    REPORTS.mkdir(parents=True, exist_ok=True)
    out_dated.write_text(md, encoding="utf-8")
    out_latest.write_text(md, encoding="utf-8")
    return {
        "as_of": trade_date,
        "pool_count": len(payload["tomorrow_pool"]),
        "pool_fresh_n": payload["pool_fresh_n"],
        "slots": payload["slots"],
        "report_dated": str(out_dated),
        "report_latest": str(out_latest),
        "markdown": md,
        "payload": payload,
    }


def main(argv: list[str] | None = None) -> int:
    from stock_db import DEFAULT_DB_PATH, connect

    parser = argparse.ArgumentParser(
        description="RRG mono swap-accel（C18acc）16:30 收盤診斷 brief（Scheme A）"
    )
    parser.add_argument("--date", default="", help="YYYY-MM-DD（預設最新交易日）")
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
            etf_codes=tuple(args.etf_codes),
        )
    finally:
        conn.close()

    print(result["report_latest"])
    print(
        f"C18acc daily: fresh_pool={result['pool_fresh_n']} "
        f"pool_n={result['pool_count']} slots={len(result['slots'])}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
