"""持股調倉研究（僅用 DB／官網可得的資料，不依 FinMind）。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from stock_db import compute_etf_holdings_changes, list_etf_snapshot_dates

ADD_ACTIONS = frozenset({"新进", "加码"})
REDUCE_ACTIONS = frozenset({"减码", "出清"})
TW_SPOT_CODE = "IX0001"


def resolve_change_dates(
    conn: sqlite3.Connection,
    etf_code: str,
    curr_date: str | None = None,
    prev_date: str | None = None,
) -> tuple[str, str] | None:
    dates = list_etf_snapshot_dates(conn, etf_code)
    if not dates:
        return None
    curr = curr_date or dates[0]
    prev = prev_date
    if prev is None:
        if len(dates) < 2:
            return None
        prev = dates[1] if dates[0] == curr else dates[0]
    return curr, prev


@dataclass(frozen=True)
class EtfChangeWindow:
    etf_code: str
    latest_snapshot: str | None
    curr: str | None
    prev: str | None


def latest_tej_ix_date(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        """
        SELECT MAX(date) FROM daily_bars
        WHERE code = ? AND source IN ('tej', 'yahoo')
        """,
        (TW_SPOT_CODE,),
    ).fetchone()
    return str(row[0]) if row and row[0] else None


@dataclass(frozen=True)
class AlignedCohort:
    """同一 prev→curr 窗口的 ETF 子集（跨檔對齊）。"""

    prev_date: str
    curr_date: str
    etf_codes: tuple[str, ...]


def resolve_aligned_cohort(
    conn: sqlite3.Connection,
    etf_codes: tuple[str, ...],
    *,
    min_etfs: int = 2,
) -> AlignedCohort | None:
    """取人數最多的對齊窗口；至少 min_etfs 檔才回傳。"""
    buckets: dict[tuple[str, str], list[str]] = {}
    for etf_code in etf_codes:
        pair = resolve_change_dates(conn, etf_code)
        if not pair:
            continue
        buckets.setdefault(pair, []).append(etf_code)
    if not buckets:
        return None
    (curr, prev), members = max(buckets.items(), key=lambda item: len(item[1]))
    if len(members) < min_etfs:
        return None
    return AlignedCohort(prev_date=prev, curr_date=curr, etf_codes=tuple(members))


def resolve_aligned_change_window(
    conn: sqlite3.Connection,
    etf_codes: tuple[str, ...],
) -> tuple[str, str] | None:
    """全部 ETF 同一窗口時回傳 (prev, curr)；否則 None。"""
    cohort = resolve_aligned_cohort(conn, etf_codes, min_etfs=len(etf_codes))
    if cohort is None or len(cohort.etf_codes) != len(etf_codes):
        return None
    return cohort.prev_date, cohort.curr_date


def gather_etf_change_windows(
    conn: sqlite3.Connection,
    etf_codes: tuple[str, ...],
) -> list[EtfChangeWindow]:
    windows: list[EtfChangeWindow] = []
    for etf_code in etf_codes:
        dates = list_etf_snapshot_dates(conn, etf_code)
        latest = dates[0] if dates else None
        pair = resolve_change_dates(conn, etf_code)
        if pair:
            curr, prev = pair
        else:
            curr, prev = None, None
        windows.append(
            EtfChangeWindow(
                etf_code=etf_code,
                latest_snapshot=latest,
                curr=curr,
                prev=prev,
            )
        )
    return windows


def _fmt_pct_short(val: float | None) -> str:
    return f"{val:+.2f}%" if val is not None else "—"


def _tech_risk_latest_line(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        """
        SELECT session_date, us_trade_date, tsm_daily_return_pct,
               COALESCE(sox_daily_return_pct, smh_daily_return_pct) AS semi_ret,
               semi_benchmark, tx_gap_pct, te_overnight_pct
        FROM tech_risk_daily_snapshot
        ORDER BY session_date DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        return None
    semi_label = row["semi_benchmark"] or "SOX"
    return (
        f"台股 {row['session_date']} | 美股 {row['us_trade_date']} | "
        f"TSM {_fmt_pct_short(row['tsm_daily_return_pct'])} | "
        f"{semi_label} {_fmt_pct_short(row['semi_ret'])} | "
        f"TX gap {_fmt_pct_short(row['tx_gap_pct'])} | "
        f"TE o/n {_fmt_pct_short(row['te_overnight_pct'])}"
    )


def print_sync_baseline_header(
    conn: sqlite3.Connection,
    etf_codes: tuple[str, ...],
) -> None:
    """持股 changes / 共識輸出前：標示各資料源基準日與是否一致。"""
    tw_latest = latest_tej_ix_date(conn)
    windows = gather_etf_change_windows(conn, etf_codes)

    print("")
    print("=== 研究基準日（本次輸出）===")
    print(
        "  資料來源: SQLite 快照（TEJ/FinMind/官網已於 daily_sync 寫入；"
        "本報告不即時打 API）"
    )
    print(f"  台股日線最新（{TW_SPOT_CODE}）: {tw_latest or '—'}")

    tech_line = _tech_risk_latest_line(conn)
    if tech_line:
        print(f"  科技風險（最新一列）: {tech_line}")
    else:
        print("  科技風險: —（尚無 tech_risk_daily_snapshot）")

    comparable = [w for w in windows if w.curr and w.prev]
    if not comparable:
        print("  持股比較區間: —（每檔需 ≥2 個 snapshot 日）")
    else:
        curr_set = {w.curr for w in comparable}
        prev_set = {w.prev for w in comparable}
        if len(curr_set) == 1 and len(prev_set) == 1:
            curr = next(iter(curr_set))
            prev = next(iter(prev_set))
            print(
                f"  持股比較區間: {prev} → {curr} "
                f"（{len(comparable)}/{len(etf_codes)} 檔可比較）"
            )
        else:
            print(
                f"  持股比較區間: 各檔不同步（{len(comparable)}/{len(etf_codes)} 檔可比較）"
            )
            print_holdings_date_warnings(windows, only_mismatch=True)


def print_holdings_date_warnings(
    windows: list[EtfChangeWindow],
    *,
    only_mismatch: bool = False,
) -> None:
    """列出各 ETF 最新 snapshot 與 changes 用的 prev→curr。"""
    comparable = [w for w in windows if w.curr and w.prev]
    if not comparable:
        return

    curr_set = {w.curr for w in comparable}
    prev_set = {w.prev for w in comparable}
    aligned = len(curr_set) == 1 and len(prev_set) == 1
    if aligned and only_mismatch:
        return

    if not aligned or not only_mismatch:
        print("")
        print("  ⚠ 持股 snapshot 日不一致（跨 ETF 共識請以各檔 changes 日期為準）")

    for w in windows:
        if w.latest_snapshot is None:
            print(f"    {w.etf_code}: 無 snapshot")
            continue
        if w.curr and w.prev:
            print(
                f"    {w.etf_code}: 最新 {w.latest_snapshot}  |  "
                f"比較 {w.prev} → {w.curr}"
            )
        else:
            print(
                f"    {w.etf_code}: 最新 {w.latest_snapshot}  |  "
                "不足 2 日無法比較"
            )


def holding_growth_pct(
    shares_prev: float | None,
    shares_curr: float | None,
    action: str,
) -> float | None:
    if action == "新进":
        return None
    prev = shares_prev or 0
    curr = shares_curr or 0
    if prev <= 0:
        return None
    return (curr / prev - 1) * 100


def implied_close_from_holdings(
    conn: sqlite3.Connection,
    stock_id: str,
    *snapshot_dates: str,
) -> float | None:
    """以持股 amount/shares 推算單價（EZMoney 有 amount；凱基多為 NULL）。"""
    for snap in snapshot_dates:
        if not snap:
            continue
        rows = conn.execute(
            """
            SELECT amount, shares FROM etf_holdings
            WHERE stock_id = ? AND snapshot_date = ?
              AND shares > 0 AND amount IS NOT NULL AND amount > 0
            """,
            (stock_id, snap),
        ).fetchall()
        prices = [
            float(amount) / float(shares)
            for amount, shares in rows
            if shares and float(shares) > 0
        ]
        if prices:
            return sum(prices) / len(prices)
    return None


def load_implied_closes(
    conn: sqlite3.Connection,
    stock_ids: list[str],
    prev_date: str,
    curr_date: str | None = None,
) -> dict[str, float | None]:
    return {
        sid: implied_close_from_holdings(conn, sid, prev_date, curr_date or "")
        for sid in stock_ids
    }


def implied_flow_ntd(share_delta: float, close: float | None) -> float | None:
    if close is None or close <= 0:
        return None
    return share_delta * close


def fmt_growth_pct(value: float | None, action: str) -> str:
    if action == "新进":
        return "NEW"
    if value is None:
        return "—"
    return f"{value:+.1f}%"


def fmt_ntd_short(value: float | None) -> str | None:
    if value is None:
        return None
    sign = "+" if value >= 0 else "-"
    av = abs(value)
    if av >= 1e8:
        return f"{sign}{av / 1e8:.2f}億"
    if av >= 1e4:
        return f"{sign}{av / 1e4:.0f}萬"
    return f"{sign}{av:,.0f}"


@dataclass
class ConsensusStock:
    stock_id: str
    stock_name: str = ""
    etf_add: int = 0
    etf_reduce: int = 0
    etf_held: int = 0
    share_delta_total: float = 0.0
    flow_ntd: float | None = None
    etf_add_list: list[str] = field(default_factory=list)
    etf_reduce_list: list[str] = field(default_factory=list)
    growth_pct: float | None = None


def build_cross_etf_consensus(
    conn: sqlite3.Connection,
    etf_codes: tuple[str, ...],
) -> list[ConsensusStock]:
    by_stock: dict[str, ConsensusStock] = {}
    anchor_by_stock: dict[str, str] = {}

    for etf_code in etf_codes:
        pair = resolve_change_dates(conn, etf_code)
        if not pair:
            continue
        curr, prev = pair
        rows = compute_etf_holdings_changes(conn, etf_code, curr, prev)
        for row in rows:
            sid = row["stock_id"]
            entry = by_stock.setdefault(
                sid,
                ConsensusStock(stock_id=sid, stock_name=row["stock_name"] or ""),
            )
            if row["stock_name"] and not entry.stock_name:
                entry.stock_name = row["stock_name"]
            anchor_by_stock.setdefault(sid, prev)
            action = row["action"]
            delta = float(row["share_delta"] or 0)
            if row["shares_curr"] and float(row["shares_curr"]) > 0:
                entry.etf_held += 1
            if action in ADD_ACTIONS and delta > 0:
                entry.etf_add += 1
                entry.etf_add_list.append(etf_code)
                entry.share_delta_total += delta
            elif action in REDUCE_ACTIONS and delta < 0:
                entry.etf_reduce += 1
                entry.etf_reduce_list.append(etf_code)
                entry.share_delta_total += delta

    if not by_stock:
        return []

    for sid, entry in by_stock.items():
        prev = anchor_by_stock.get(sid, "")
        pair_dates = [
            resolve_change_dates(conn, etf)[0]
            for etf in entry.etf_add_list or entry.etf_reduce_list
            if resolve_change_dates(conn, etf)
        ]
        curr_fallback = pair_dates[0] if pair_dates else None
        close = implied_close_from_holdings(conn, sid, prev, curr_fallback or "")
        entry.flow_ntd = implied_flow_ntd(entry.share_delta_total, close)
        if entry.etf_add == 1 and entry.etf_add_list:
            etf = entry.etf_add_list[0]
            pair = resolve_change_dates(conn, etf)
            if pair:
                for r in compute_etf_holdings_changes(conn, etf, pair[0], pair[1]):
                    if r["stock_id"] == sid:
                        entry.growth_pct = holding_growth_pct(
                            r["shares_prev"],
                            r["shares_curr"],
                            r["action"],
                        )
                        break

    result = [s for s in by_stock.values() if s.etf_add > 0 or s.etf_reduce > 0]
    result.sort(
        key=lambda s: (s.etf_add, abs(s.flow_ntd or 0)),
        reverse=True,
    )
    return result


def format_research_suffix(
    row: sqlite3.Row,
    close: float | None,
) -> str:
    action = row["action"]
    growth = holding_growth_pct(row["shares_prev"], row["shares_curr"], action)
    flow = implied_flow_ntd(float(row["share_delta"] or 0), close)
    parts = [f"grow={fmt_growth_pct(growth, action)}"]
    flow_s = fmt_ntd_short(flow)
    if flow_s is not None:
        parts.append(f"flow={flow_s}")
    return " " + " ".join(parts)


def print_cross_etf_consensus(
    conn: sqlite3.Connection,
    etf_codes: tuple[str, ...],
) -> None:
    windows = gather_etf_change_windows(conn, etf_codes)
    comparable = [w for w in windows if w.curr and w.prev]
    curr_set = {w.curr for w in comparable}
    prev_set = {w.prev for w in comparable}
    aligned = len(comparable) > 0 and len(curr_set) == 1 and len(prev_set) == 1

    rows = build_cross_etf_consensus(conn, etf_codes)
    if not rows:
        print("跨 ETF 共識：尚無足夠 snapshot 可比較")
        return

    adds = [r for r in rows if r.etf_add > 0]
    reduces = [r for r in rows if r.etf_reduce > 0]

    def _flow_cell(flow: float | None) -> str:
        s = fmt_ntd_short(flow)
        return f"{s:>8}" if s else f"{'':>8}"

    header = (
        f"  {'代號':>6} {'名稱':<8} {'加碼':>2} {'流入':>8} "
        f"{'Δ股數':>12} {'grow':>7}  ETF"
    )

    def _print_add_rows(title: str, subset: list[ConsensusStock]) -> None:
        if not subset:
            return
        print("")
        print(title)
        print(header)
        for r in subset:
            grow_s = fmt_growth_pct(r.growth_pct, "加码") if r.etf_add == 1 else "—"
            print(
                f"  {r.stock_id:>6} {r.stock_name:<8} {r.etf_add:>2} "
                f"{_flow_cell(r.flow_ntd)} {r.share_delta_total:>+12,.0f} "
                f"{grow_s:>7}  {','.join(r.etf_add_list)}"
            )

    print("")
    if aligned:
        curr = next(iter(curr_set))
        prev = next(iter(prev_set))
        print(
            f"=== 跨 ETF 共識（{len(etf_codes)} 檔；區間 {prev} → {curr}；"
            "流入=Δ股×持股推算單價，僅 EZMoney 有 amount）==="
        )
    else:
        print(
            f"=== 跨 ETF 共識（{len(etf_codes)} 檔；⚠ 各檔日期不同步，共識僅供參考；"
            "見上方基準日明細；流入=Δ股×持股推算單價，僅 EZMoney 有 amount）==="
        )
    if adds:
        multi = [r for r in adds if r.etf_add >= 2]
        single = [r for r in adds if r.etf_add == 1]
        single.sort(key=lambda s: abs(s.flow_ntd or 0), reverse=True)
        _print_add_rows("--- 共識 ≥2 檔 ETF 同步加碼 ---", multi)
        _print_add_rows("--- 單檔加碼（依流入排序前 15）---", single[:15])
    else:
        print("  （本日無加碼）")

    hard = [r for r in reduces if r.etf_reduce >= 2 and r.etf_add == 0]
    hard.sort(key=lambda s: abs(s.flow_ntd or 0), reverse=True)
    soft = [r for r in reduces if r.etf_reduce == 1 and r.etf_add == 0]
    soft.sort(key=lambda s: abs(s.flow_ntd or 0), reverse=True)
    if hard:
        print("")
        print("--- 跨 ETF 減碼 ≥2 檔 ---")
        for r in hard:
            flow_s = fmt_ntd_short(r.flow_ntd)
            flow_part = f" flow={flow_s}" if flow_s else ""
            print(
                f"  {r.stock_id:>6} {r.stock_name:<8} 減碼{r.etf_reduce}檔"
                f"{flow_part} {','.join(r.etf_reduce_list)}"
            )
    if soft:
        print("")
        print("--- 單檔減碼（前 10）---")
        for r in soft[:10]:
            flow_s = fmt_ntd_short(r.flow_ntd)
            flow_part = f" flow={flow_s}" if flow_s else ""
            print(
                f"  {r.stock_id:>6} {r.stock_name:<8} 減碼1檔"
                f"{flow_part} {','.join(r.etf_reduce_list)}"
            )


def print_position_intent_report(
    conn: sqlite3.Connection,
    etf_codes: tuple[str, ...],
    *,
    debug: bool = False,
) -> None:
    """跨 ETF 對齊日：L2 共識 + L3 輪動 + L4 conviction + L5 角色 + 意圖註解。"""
    from comment_engine import compose_comment, format_signal_debug
    from signal_engine import build_aligned_signals

    result = build_aligned_signals(conn, etf_codes)
    print("")
    if result is None:
        print("=== 部位意圖（L2–L6）===")
        print("  略過：少於 2 檔 ETF 共用同一 prev→curr（無法做跨檔橫截面）")
        return

    prev_date = result.prev_date
    curr_date = result.curr_date
    signals = result.signals
    active = result.etf_codes
    skipped = [c for c in etf_codes if c not in active]
    movers = [s for s in signals if s.net_side in ("add", "reduce", "mixed")]
    cohort_note = f"{len(active)}/{len(etf_codes)} 檔對齊"
    if skipped:
        cohort_note += f"（未納入：{','.join(skipped)}）"
    print(
        f"=== 部位意圖 L2–L6（{cohort_note}；{prev_date} → {curr_date}；"
        f"{len(movers)} 檔變動）==="
    )
    if not movers:
        print("  （區間內無加減碼）")
        return

    adds = [s for s in movers if s.net_side == "add"]
    reduces = [s for s in movers if s.net_side == "reduce"]
    mixed = [s for s in movers if s.net_side == "mixed"]

    def _print_block(title: str, block: list) -> None:
        if not block:
            return
        print("")
        print(title)
        for sig in block:
            print(f"  {sig.stock_id:>6} {compose_comment(sig)}")
            if debug:
                print(f"         {format_signal_debug(sig)}")

    _print_block("--- 加碼／新進（依 conviction 排序）---", adds)
    _print_block("--- 減碼／出清 ---", reduces)
    _print_block("--- 分歧調倉 ---", mixed)

    pairs_seen: set[str] = set()
    for sig in signals:
        for attr in ("rotation_in", "rotation_out"):
            val = getattr(sig, attr)
            if val:
                pairs_seen.add(val)
    if pairs_seen:
        from investment_themes import theme_label

        print("")
        print("--- 主題資金流（L3 矩陣摘要）---")
        for p in sorted(pairs_seen):
            a, b = p.split("→", 1)
            print(f"  {theme_label(a)} → {theme_label(b)}")
