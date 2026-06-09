#!/usr/bin/env python3
"""收盤執行上下文：籌碼共振、量確認、技術位、手動 R:R。"""

from __future__ import annotations

import sqlite3

from market_labels import ENTRY_WAIT
from research_universe import ResearchUniverseResult, build_research_universe
from entry_signal import classify_entry_context_batch
from stock_context import (
    build_chip_resonance,
    compute_technical,
    load_latest_institutional,
)
from signal_engine import build_aligned_signals
from stock_db import count_stock_market_rows
from trade_levels import levels_for_stocks, levels_path_hint


def _warn_if_no_market_data(conn: sqlite3.Connection) -> None:
    bar_n, inst_n, bar_max, inst_max = count_stock_market_rows(conn)
    if bar_n == 0 and inst_n == 0:
        print(
            "  ⚠ 尚無成分股日線/法人（請 RUN_STOCK_MARKET_SYNC=1 並完成收盤同步）"
        )
        return
    print(
        f"  資料覆蓋：K線 {bar_n} 筆（最新 {bar_max or '—'}）· "
        f"法人 {inst_n} 筆（最新 {inst_max or '—'}）"
    )


def print_chip_resonance_section(
    conn: sqlite3.Connection,
    etf_codes: tuple[str, ...],
    universe: ResearchUniverseResult,
) -> None:
    print("")
    print("=== 籌碼共振（ETF 流向 × 三大法人 · 最新交易日）===")
    _warn_if_no_market_data(conn)
    name_by_id = {e.stock_id: e.stock_name for e in universe.entries}
    rows = build_chip_resonance(
        conn,
        etf_codes,
        universe.stock_ids,
        name_by_id,
    )
    print(
        f"  {'代號':>6} {'名稱':<8} {'ETF':<8} {'外資':<4} {'投信':<4} {'自營':<4} "
        f"{'標籤':<8} 說明"
    )
    for r in rows:
        print(
            f"  {r.stock_id:>6} {r.stock_name:<8} {r.etf_flow:<8} "
            f"{r.foreign_label:<4} {r.trust_label:<4} {r.dealer_label:<4} "
            f"{r.tag:<8} {r.note}"
        )


def print_technical_volume_section(
    conn: sqlite3.Connection,
    universe: ResearchUniverseResult,
    *,
    etf_codes: tuple[str, ...] | None = None,
) -> None:
    sig_map: dict[str, str | None] = {}
    if etf_codes:
        aligned = build_aligned_signals(conn, etf_codes)
        if aligned is not None:
            sig_map = {s.stock_id: s.net_side for s in aligned.signals}
    print("")
    print("=== 技術位 & 量確認（Research Universe · 個股）===")
    print(
        "  乖離=距 MA20/MA60%；52週位=區間百分位(0=低 100=高)；"
        "量倍=當日量/近5日均量"
    )
    print(
        f"  {'代號':>6} {'名稱':<8} {'價位型態':<12} {'MA20%':>7} {'MA60%':>7} "
        f"{'52週位':>6} {'距高%':>7} {'量倍':>5} {'量':<8}"
    )
    batch_items: list = []
    tech_by_id: dict = {}
    for e in universe.entries:
        tech = compute_technical(conn, e.stock_id)
        tech_by_id[e.stock_id] = tech
        batch_items.append(
            (e.stock_id, tech, sig_map.get(e.stock_id), None, None)
        )
    ctx_map = classify_entry_context_batch(batch_items)
    for e in universe.entries:
        tech = tech_by_id.get(e.stock_id)
        if tech is None:
            print(f"  {e.stock_id:>6} {e.stock_name:<8}  —（無 K 線）")
            continue
        ctx = ctx_map.get(e.stock_id)
        entry_sig = ctx.display if ctx else ENTRY_WAIT
        ma20 = f"{tech.dist_ma20_pct:+.1f}" if tech.dist_ma20_pct is not None else "—"
        ma60 = f"{tech.dist_ma60_pct:+.1f}" if tech.dist_ma60_pct is not None else "—"
        pos = f"{tech.position_52w_pct:.0f}" if tech.position_52w_pct is not None else "—"
        dhi = (
            f"{tech.dist_from_52w_high_pct:+.1f}"
            if tech.dist_from_52w_high_pct is not None
            else "—"
        )
        vr = f"{tech.vol_ratio_5d:.2f}" if tech.vol_ratio_5d is not None else "—"
        chase = ""
        if tech.dist_ma20_pct is not None and tech.dist_ma20_pct >= 18:
            chase = " [乖離大]"
        elif tech.dist_ma60_pct is not None and tech.dist_ma60_pct >= 18:
            chase = " [乖離大]"
        print(
            f"  {e.stock_id:>6} {e.stock_name:<8} {entry_sig:<12} {ma20:>7} {ma60:>7} "
            f"{pos:>6} {dhi:>7} {vr:>5} {tech.vol_label:<8}{chase}"
        )


def print_trade_levels_section(
    conn: sqlite3.Connection,
    universe: ResearchUniverseResult,
) -> None:
    levels = levels_for_stocks(universe.stock_ids)
    print("")
    print("=== 風險報酬（手動價位 · 不產 AI 目標價）===")
    if not levels:
        print(f"  無價位（可編輯 {levels_path_hint()}）")
        return
    name_by_id = {e.stock_id: e.stock_name for e in universe.entries}
    print(
        f"  {'代號':>6} {'名稱':<8} {'進場':>8} {'停損':>8} {'目標':>8} "
        f"{'風險%':>6} {'獲利%':>6} {'R:R':>5} 備註"
    )
    for lv in levels:
        if not lv.valid:
            print(f"  {lv.stock_id:>6}  價位無效（需 停損<進場<目標）")
            continue
        print(
            f"  {lv.stock_id:>6} {name_by_id.get(lv.stock_id, ''):<8} "
            f"{lv.entry:>8.2f} {lv.stop:>8.2f} {lv.target:>8.2f} "
            f"{lv.risk_pct:>5.1f}% {lv.reward_pct:>5.1f}% {lv.risk_reward:>5.2f} "
            f"{lv.note}"
        )


def print_execution_context_report(
    conn: sqlite3.Connection,
    etf_codes: tuple[str, ...],
    *,
    universe: ResearchUniverseResult | None = None,
) -> None:
    uni = universe or build_research_universe(conn, etf_codes)
    if uni is None or not uni.entries:
        print("")
        print("=== 執行上下文（籌碼 · 量 · 技術 · R:R）===")
        print("  略過：無 Research Universe")
        return
    print_chip_resonance_section(conn, etf_codes, uni)
    print_technical_volume_section(conn, uni, etf_codes=etf_codes)
    print_trade_levels_section(conn, uni)
