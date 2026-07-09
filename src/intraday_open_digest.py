"""09:08 開盤解讀 · 合併 morning brief + gate + C18acc + VCP/RRG + sell-radar。"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from intraday_1300_digest import (
    DigestInput,
    RadarSell,
    VcpCandidate,
    _read_if_exists,
    _sources_for_symbol,
    _symbol_name,
    load_digest_input,
    parse_buy_radar,
    parse_c18acc_screen,
    parse_morning_held_ids,
    parse_sell_radar,
)
from report_paths import REPORTS_DIR

_GATE_KBAR = re.compile(r"(?:1m|5m) K sync：\*\*(\d+)\*\* bars")
_GATE_SKIP = re.compile(r"略過：\*\*(.+?)\*\*")
_GATE_MODE = re.compile(r"\*\*portfolio_exit_mode=(.+?)\*\*")
_GATE_CORE4 = re.compile(r"CORE4 觸發 A：(\d+) 檔")
_GATE_B = re.compile(r"2330 觸發 B：(是|否)")

_C18_KBAR = re.compile(r"kbar sync：\*\*(\d+)\*\* bars")
_C18_SLOTS = re.compile(r"slots：\*\*(\d+)\s*/\s*(\d+)\*\*")
_C18_POOL_ASOF = re.compile(r"pool_as_of \*\*([^*]+)\*\*")
_C18_FRESH_ROW = re.compile(
    r"^\|\s*\d+\s*\|\s*(\d{4,5})\s*\|\s*([^|]+?)\s*\|\s*★\s*\|\s*([\d.]+)\s*\|"
)

_MORNING_TX_GAP = re.compile(
    r"TX gap \*\*([+-][\d.]+%)\*\* @ ([\d.]+)"
)
_MORNING_BAR = re.compile(r"PIT · bar (\d{4}-\d{2}-\d{2})")
_MORNING_WEAK = re.compile(r"\*\*結構弱\*\*：(.+)$")
_MORNING_BIG_LOSS = re.compile(r"\*\*帳面大虧\*\*：(.+)$")
_MORNING_WARN = re.compile(r"^-\s+⚠\s+(.+)$")

_SELL_BLOCK = re.compile(
    r"【賣出觀測】(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})(.*?)(?=\n════|$)",
    re.DOTALL,
)
_SELL_UNIVERSE = re.compile(r"宇宙：監測 (\d+) 檔 · 當日有 (?:1m|5m) K (\d+) 檔")
_SELL_STRESS = re.compile(r"holdings_stress=(ON|OFF)")
_SELL_STRESS_N = re.compile(r"holdings_stress=ON（(\d+)檔")
_SELL_STRUCT = re.compile(
    r"· (\d{4,5}) (.+?) ×\d+（(\w+)） @ ([\d.]+) · (.+)$"
)


@dataclass(frozen=True)
class C18FreshRow:
    stock_id: str
    stock_name: str
    seg_last: float


@dataclass
class GateSnapshot:
    kbar_sync_n: int | None = None
    skip_reason: str | None = None
    portfolio_exit_mode: str = "—"
    gate_a_count: int | None = None
    gate_b: str | None = None
    report_exists: bool = False


@dataclass
class MorningSnapshot:
    exists: bool = False
    tx_gap_pct: str | None = None
    tx_price: str | None = None
    bar_as_of: str | None = None
    weak_line: str | None = None
    big_loss_line: str | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass
class C18accSnapshot:
    kbar_sync_n: int | None = None
    slots_used: int | None = None
    slots_total: int | None = None
    pool_as_of: str | None = None
    fresh_rows: list[C18FreshRow] = field(default_factory=list)
    held: dict[str, str] = field(default_factory=dict)
    report_exists: bool = False


@dataclass
class SellStressRow:
    stock_id: str
    stock_name: str
    mode: str
    price: str
    reason: str


@dataclass
class SellObservation:
    poll_minute: str | None = None
    universe_n: int | None = None
    universe_kbar_n: int | None = None
    holdings_stress: bool = False
    holdings_stress_n: int = 0
    gate_label: str = "—"
    structural: list[SellStressRow] = field(default_factory=list)
    watch: list[SellStressRow] = field(default_factory=list)
    skip_reason: str | None = None
    log_found: bool = False


@dataclass
class OpenDigestInput:
    session_date: str
    core: DigestInput | None = None
    gate: GateSnapshot = field(default_factory=GateSnapshot)
    morning: MorningSnapshot = field(default_factory=MorningSnapshot)
    c18acc: C18accSnapshot = field(default_factory=C18accSnapshot)
    sell: SellObservation = field(default_factory=SellObservation)
    vcp_source: str = "—"


def parse_gate_report(text: str) -> GateSnapshot:
    snap = GateSnapshot(report_exists=bool(text.strip()))
    m = _GATE_KBAR.search(text)
    if m:
        snap.kbar_sync_n = int(m.group(1))
    m = _GATE_SKIP.search(text)
    if m:
        snap.skip_reason = m.group(1).strip()
    m = _GATE_MODE.search(text)
    if m:
        snap.portfolio_exit_mode = m.group(1).strip()
    m = _GATE_CORE4.search(text)
    if m:
        snap.gate_a_count = int(m.group(1))
    m = _GATE_B.search(text)
    if m:
        snap.gate_b = m.group(1)
    return snap


def parse_morning_brief(text: str) -> MorningSnapshot:
    snap = MorningSnapshot(exists=bool(text.strip()))
    m = _MORNING_TX_GAP.search(text)
    if m:
        snap.tx_gap_pct = m.group(1)
        snap.tx_price = m.group(2)
    m = _MORNING_BAR.search(text)
    if m:
        snap.bar_as_of = m.group(1)
    for line in text.splitlines():
        m = _MORNING_WEAK.search(line.strip())
        if m:
            snap.weak_line = m.group(1).strip()
        m = _MORNING_BIG_LOSS.search(line.strip())
        if m:
            snap.big_loss_line = m.group(1).strip()
        m = _MORNING_WARN.match(line.strip())
        if m:
            snap.warnings.append(m.group(1).strip())
    return snap


def parse_c18acc_extended(text: str) -> C18accSnapshot:
    held, fresh_ids = parse_c18acc_screen(text)
    snap = C18accSnapshot(
        held=held,
        report_exists=bool(text.strip()),
    )
    m = _C18_KBAR.search(text)
    if m:
        snap.kbar_sync_n = int(m.group(1))
    m = _C18_SLOTS.search(text)
    if m:
        snap.slots_used = int(m.group(1))
        snap.slots_total = int(m.group(2))
    m = _C18_POOL_ASOF.search(text)
    if m:
        snap.pool_as_of = m.group(1).strip()
    for line in text.splitlines():
        m = _C18_FRESH_ROW.match(line.strip())
        if m:
            snap.fresh_rows.append(
                C18FreshRow(
                    stock_id=m.group(1),
                    stock_name=m.group(2).strip(),
                    seg_last=float(m.group(3)),
                )
            )
    if not snap.fresh_rows and fresh_ids:
        for sid in sorted(fresh_ids):
            snap.fresh_rows.append(C18FreshRow(stock_id=sid, stock_name=held.get(sid, sid), seg_last=0.0))
    return snap


def parse_sell_log_for_session(
    text: str,
    session_date: str,
    *,
    max_poll_minute: str | None = "09:12",
) -> SellObservation:
    blocks = list(_SELL_BLOCK.finditer(text))
    obs = SellObservation()
    target = None
    for m in blocks:
        if m.group(1) != session_date:
            continue
        minute = m.group(2)
        if max_poll_minute is not None and minute > max_poll_minute:
            continue
        target = m
    if target is None:
        return obs

    obs.log_found = True
    obs.poll_minute = target.group(2)
    body = target.group(3)

    if "狀態：略過" in body:
        skip_m = re.search(r"狀態：略過 · (.+)", body)
        if skip_m:
            obs.skip_reason = skip_m.group(1).strip()
        return obs

    m = _SELL_UNIVERSE.search(body)
    if m:
        obs.universe_n = int(m.group(1))
        obs.universe_kbar_n = int(m.group(2))

    stress_m = _SELL_STRESS.search(body)
    if stress_m:
        obs.holdings_stress = stress_m.group(1) == "ON"
    stress_n = _SELL_STRESS_N.search(body)
    if stress_n:
        obs.holdings_stress_n = int(stress_n.group(1))

    gate_m = re.search(r"portfolio_exit_mode=([^·]+)", body)
    if gate_m:
        obs.gate_label = gate_m.group(1).strip()

    section = ""
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("→ 結構停損"):
            section = "structural"
            continue
        if line.startswith("→ 持倉觀測"):
            section = "watch"
            continue
        if line.startswith("→ "):
            section = ""
            continue
        if not line.startswith("·"):
            continue
        sm = _SELL_STRUCT.search(line)
        if not sm:
            continue
        row = SellStressRow(
            stock_id=sm.group(1),
            stock_name=sm.group(2).strip(),
            mode=sm.group(3),
            price=sm.group(4),
            reason=sm.group(5).strip(),
        )
        if section == "structural":
            obs.structural.append(row)
        elif section == "watch":
            obs.watch.append(row)
    return obs


def load_vcp_from_db(
    conn: sqlite3.Connection,
    session_date: str,
    *,
    min_composite: float = 45.0,
    limit: int = 5,
) -> tuple[list[VcpCandidate], str, str]:
    row = conn.execute(
        """
        SELECT MAX(as_of_date) FROM vcp_screen_scores_v2
        WHERE model_id = 'vcp-funnel' AND as_of_date <= ?
        """,
        (session_date,),
    ).fetchone()
    if not row or not row[0]:
        return [], "—", "—"
    as_of = str(row[0])
    rows = conn.execute(
        """
        SELECT stock_id, stock_name, composite_score, execution_state,
               pivot_price, distance_from_pivot_pct, stop_loss
        FROM vcp_screen_scores_v2
        WHERE model_id = 'vcp-funnel' AND as_of_date = ?
          AND composite_score >= ?
        ORDER BY composite_score DESC
        LIMIT ?
        """,
        (as_of, min_composite, limit),
    ).fetchall()
    out: list[VcpCandidate] = []
    for r in rows:
        pivot = f"{r[4]:.2f}" if r[4] is not None else "—"
        dist = f"{r[5]:.2f}" if r[5] is not None else "—"
        stop = f"{r[6]:.2f}" if r[6] is not None else "—"
        out.append(
            VcpCandidate(
                stock_id=str(r[0]),
                stock_name=str(r[1] or r[0]),
                composite=float(r[2]),
                state=str(r[3] or "—"),
                pivot=pivot,
                dist_pct=dist,
                stop=stop,
            )
        )
    return out, as_of, f"DB close · {as_of}"


def load_open_digest_input(
    *,
    session_date: str,
    reports_dir: Path = REPORTS_DIR,
    sell_log_path: Path | None = None,
    db_conn: sqlite3.Connection | None = None,
) -> OpenDigestInput:
    stamp = session_date.replace("-", "")
    order_dir = reports_dir.parent / "order" / "snapshots"

    inp = OpenDigestInput(session_date=session_date)
    inp.core = load_digest_input(session_date=session_date, reports_dir=reports_dir)

    gate_text = _read_if_exists(order_dir / f"intraday_gate_{stamp}.md")
    inp.gate = parse_gate_report(gate_text)

    morning_text = _read_if_exists(order_dir / f"morning_brief_{stamp}.md")
    inp.morning = parse_morning_brief(morning_text)
    if morning_text and not inp.core.held_ids:
        inp.core.held_ids = parse_morning_held_ids(morning_text)

    c18_text = _read_if_exists(reports_dir / f"{stamp}_rrg_c18acc_screen.md")
    if not c18_text:
        c18_text = _read_if_exists(reports_dir / "rrg_c18acc_screen.md")
    inp.c18acc = parse_c18acc_extended(c18_text)
    if c18_text and not inp.core.c18acc_held:
        inp.core.c18acc_held = inp.c18acc.held
        inp.core.c18acc_fresh = {r.stock_id for r in inp.c18acc.fresh_rows}

    if not inp.core.vcp and db_conn is not None:
        vcp_rows, bar_as_of, source = load_vcp_from_db(db_conn, session_date)
        if vcp_rows:
            inp.core.vcp = vcp_rows
            inp.vcp_source = source
            if inp.core.last_close_date == "—":
                inp.core.last_close_date = bar_as_of
    elif inp.core.vcp:
        inp.vcp_source = "VCP brief" if inp.core.vcp_tick != "—" else "VCP brief/DB"

    sell_log = sell_log_path
    if sell_log_path is not None and sell_log_path.is_file():
        inp.sell = parse_sell_log_for_session(
            sell_log_path.read_text(encoding="utf-8", errors="replace"), session_date
        )

    if not inp.sell.log_found:
        logs_dir = reports_dir.parent.parent / "logs"
        candidates = [
            logs_dir / "launchd_sell-signal-radar.log",
            logs_dir / "replay" / f"launchd_sell-signal-radar_{stamp}.log",
        ]
        for path in candidates:
            if not path.is_file():
                continue
            obs = parse_sell_log_for_session(
                path.read_text(encoding="utf-8", errors="replace"), session_date
            )
            if obs.log_found:
                inp.sell = obs
                break

    sell_md = _read_if_exists(reports_dir / f"{stamp}_sell_signal_radar.md")
    if sell_md and not inp.core.sell_radar:
        inp.core.sell_radar = parse_sell_radar(sell_md)

    return inp


def _pipeline_section(inp: OpenDigestInput) -> list[str]:
    lines = ["■ 今日開盤資料狀態", ""]
    kbar = inp.gate.kbar_sync_n if inp.gate.kbar_sync_n is not None else inp.c18acc.kbar_sync_n
    kbar_s = str(kbar) if kbar is not None else "—"
    lines.append(f"  5m K 同步（gate）：{kbar_s} bars")

    if inp.gate.skip_reason:
        lines.append(f"  09:05 組合閘門：略過 — {inp.gate.skip_reason}")
    elif inp.gate.report_exists:
        lines.append(f"  09:05 組合閘門：portfolio_exit_mode={inp.gate.portfolio_exit_mode}")
    else:
        lines.append("  09:05 組合閘門：尚未產出")

    morning_ok = "已產出" if inp.morning.exists else "尚未產出（最新見 reports/order/snapshots/）"
    lines.append(f"  今日 morning brief：{morning_ok}")

    if inp.sell.log_found:
        lines.append(f"  sell-radar：{inp.sell.poll_minute} 已掃描 · 宇宙 {inp.sell.universe_n} 檔 · 5m K {inp.sell.universe_kbar_n} 檔")
    else:
        lines.append("  sell-radar：09:06 輪次 log 尚未寫入")

    if kbar is not None and kbar == 0:
        lines.append("")
        lines.append("  ⚠ 初段 5m K 可能尚未就位 · gate / sell-radar 結論請保守解讀 · 09:15 後較可靠")
    lines.append("")
    return lines


def _morning_section(inp: OpenDigestInput) -> list[str]:
    if not inp.morning.exists:
        return []
    lines = ["■ 隔夜／台指 gap（08:50 morning brief）", ""]
    if inp.morning.tx_gap_pct:
        gap = inp.morning.tx_gap_pct
        if not gap.endswith("%"):
            gap = f"{gap}%"
        lines.append(f"  TX gap {gap} @ {inp.morning.tx_price or '—'}")
    else:
        lines.append("  TX gap：無 morning_risk 資料")
    for w in inp.morning.warnings:
        lines.append(f"  ⚠ {w}")
    if inp.morning.weak_line:
        lines.append(f"  結構弱：{inp.morning.weak_line}")
    if inp.morning.big_loss_line:
        lines.append(f"  帳面大虧：{inp.morning.big_loss_line}")
    lines.append("")
    return lines


def _universe_section(inp: OpenDigestInput) -> list[str]:
    core = inp.core
    if core is None:
        return []
    pit = inp.c18acc.pool_as_of or core.last_close_date or inp.morning.bar_as_of or "—"
    uni_n = inp.sell.universe_n or "—"
    lines = [
        f"■ 監測宇宙（約 {uni_n} 檔 · PIT {pit}）",
        "",
    ]

    if inp.c18acc.fresh_rows:
        if len(inp.c18acc.fresh_rows) == 1:
            r = inp.c18acc.fresh_rows[0]
            seg = f" · seg_last {r.seg_last:.3f}" if r.seg_last else ""
            lines.append(f"  RRG fresh mono 池：僅 {r.stock_id} {r.stock_name}{seg}")
        else:
            ids = "、".join(f"{r.stock_id}" for r in inp.c18acc.fresh_rows[:5])
            lines.append(f"  RRG fresh mono 池：{len(inp.c18acc.fresh_rows)} 檔（{ids}）")
    else:
        lines.append("  RRG fresh mono 池：目前無候選")

    slots = "—"
    if inp.c18acc.slots_used is not None and inp.c18acc.slots_total is not None:
        slots = f"{inp.c18acc.slots_used}/{inp.c18acc.slots_total}"
    lines.append(f"  C18acc 槽位：{slots}")
    if inp.c18acc.held:
        held_s = "、".join(f"{sid} {name}" for sid, name in inp.c18acc.held.items())
        lines.append(f"  C18acc 持倉：{held_s}")
    lines.append("")

    vcp_ids = {c.stock_id for c in core.vcp}
    rrg_fresh_ids = {c.stock_id for c in core.rrg_fresh}
    buy_ids = {b.stock_id for b in core.buy_radar}
    c18_ids = set(core.c18acc_held) | core.c18acc_fresh

    overlap_ids: set[str] = set()
    for sid in vcp_ids | rrg_fresh_ids | buy_ids | c18_ids:
        if sum([sid in vcp_ids, sid in rrg_fresh_ids, sid in buy_ids, sid in c18_ids]) >= 2:
            overlap_ids.add(sid)

    if overlap_ids:
        lines.append("  多軌重疊（宇宙最強交集）")
        for sid in sorted(overlap_ids):
            name = _symbol_name(core, sid)
            tags = _sources_for_symbol(core, sid)
            lines.append(f"    {sid} {name} · {' · '.join(tags)}")
        lines.append("")

    vcp_only = [c for c in core.vcp if c.stock_id not in overlap_ids][:3]
    if vcp_only:
        lines.append("  VCP near pivot（次要候選 · " + inp.vcp_source + "）")
        for c in vcp_only:
            lines.append(
                f"    {c.stock_id} {c.stock_name} · composite {c.composite:.1f} · "
                f"pivot {c.pivot} · dist {c.dist_pct}%"
            )
        lines.append("")

    return lines


def _holdings_stress_section(inp: OpenDigestInput) -> list[str]:
    core = inp.core
    rows: list[str] = []
    seen: set[str] = set()

    for s in inp.sell.structural:
        key = s.stock_id
        if key in seen:
            continue
        seen.add(key)
        rows.append(f"  {s.stock_id} {s.stock_name} · {s.mode} @ {s.price}")

    for s in inp.sell.watch:
        key = s.stock_id
        if key in seen:
            continue
        seen.add(key)
        rows.append(f"  {s.stock_id} {s.stock_name} · {s.mode} @ {s.price}")

    for s in (core.sell_radar if core else []):
        if s.stock_id in seen:
            continue
        if core and core.held_ids and s.stock_id not in core.held_ids:
            continue
        seen.add(s.stock_id)
        rows.append(f"  {s.stock_id} {s.stock_name} · sell ref {s.ref} · {s.detail[:60]}")

    if not rows:
        return []

    stress_hdr = ""
    if inp.sell.holdings_stress:
        stress_hdr = f"（holdings_stress=ON · {inp.sell.holdings_stress_n} 檔≤-2%）"
    lines = [f"■ 持倉壓力帶 {stress_hdr}", ""]
    lines.extend(rows)
    lines.append("")
    return lines


def _priority_section(inp: OpenDigestInput) -> list[str]:
    core = inp.core
    if core is None:
        return ["■ 今日開盤優先關注", "", "  — 資料尚未齊全", ""]
    lines = ["■ 今日開盤優先關注", ""]

    vcp_ids = {c.stock_id for c in core.vcp}
    rrg_fresh_ids = {c.stock_id for c in core.rrg_fresh}
    buy_ids = {b.stock_id for b in core.buy_radar}
    c18_ids = set(core.c18acc_held) | core.c18acc_fresh
    overlap_ids: set[str] = set()
    for sid in vcp_ids | rrg_fresh_ids | buy_ids | c18_ids:
        if sum([sid in vcp_ids, sid in rrg_fresh_ids, sid in buy_ids, sid in c18_ids]) >= 2:
            overlap_ids.add(sid)

    priority = 1
    for sid in sorted(overlap_ids):
        name = _symbol_name(core, sid)
        tag = "多軌"
        if sid in core.c18acc_held:
            tag += " · C18acc 持倉"
        lines.append(f"  {priority}. {sid} {name}（{tag}）")
        priority += 1

    rrg_only = [c for c in core.rrg_fresh if c.stock_id not in overlap_ids]
    for c in rrg_only[:3]:
        lines.append(f"  {priority}. {c.stock_id} {c.stock_name}（RRG mono fresh）")
        priority += 1

    stress_ids = [s.stock_id for s in inp.sell.structural + inp.sell.watch]
    for sid in stress_ids:
        if sid in overlap_ids:
            continue
        name = next(
            (s.stock_name for s in inp.sell.structural + inp.sell.watch if s.stock_id == sid),
            sid,
        )
        lines.append(f"  {priority}. {sid} {name}（持倉壓力 / sell-radar）")
        priority += 1

    vcp_only = [c for c in core.vcp if c.stock_id not in overlap_ids][:2]
    for c in vcp_only:
        lines.append(f"  {priority}. {c.stock_id} {c.stock_name}（VCP 候選 · 無持倉）")
        priority += 1

    if priority == 1:
        lines.append("  — 本輪無特別優先標的 · 09:15 後 sell-radar 更新再盯")
    lines.append("")
    return lines


def _discipline_section() -> list[str]:
    return [
        "■ 實務建議（開盤時段）",
        "",
        "  · 09:00–09:04 只觀察、不賣",
        "  · 09:15–09:30 確認 5m K 恢復 → sell-radar 較可靠",
        "  · 09:25–10:25 buy-radar C0 進場窗口",
        "  · C18acc 換倉：09:30 前不 swap",
        "  · 11:00 盤中持倉脈動 · 13:02 盤中研究摘要 · 16:30 VCP 收盤 screen 覆寫",
        "",
        "系統 advisory · 人工確認後下單 · 詳細表格見 reports/daily/ 與 reports/order/snapshots/",
    ]


def format_intraday_open_digest(
    inp: OpenDigestInput,
    *,
    holdings_digest: str | None = None,
    holdings_error: str | None = None,
) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    pit = inp.c18acc.pool_as_of or inp.core.last_close_date or inp.morning.bar_as_of or "—"
    lines = [
        f"【開盤解讀 · {inp.session_date}】",
        f"產出 {now}",
        "",
        f"資料基準：PIT {pit} · VCP 來源 {inp.vcp_source}",
        "",
    ]
    lines.extend(_pipeline_section(inp))
    from intraday_holdings_digest import append_holdings_section

    append_holdings_section(lines, holdings_digest, error=holdings_error)
    lines.extend(_morning_section(inp))
    lines.extend(_universe_section(inp))
    lines.extend(_holdings_stress_section(inp))
    lines.extend(_priority_section(inp))
    lines.extend(_discipline_section())
    return "\n".join(lines)


def write_open_digest(text: str, *, session_date: str, reports_dir: Path = REPORTS_DIR) -> Path:
    stamp = session_date.replace("-", "")
    reports_dir.mkdir(parents=True, exist_ok=True)
    dated = reports_dir / f"{stamp}_intraday_open_digest.md"
    latest = reports_dir / "intraday_open_digest.md"
    dated.write_text(text, encoding="utf-8")
    latest.write_text(text, encoding="utf-8")
    return dated
