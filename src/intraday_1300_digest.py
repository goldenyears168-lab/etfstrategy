"""13:00 盤中研究合併摘要（VCP funnel + RRG mono intraday + 交叉比對）。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from report_paths import REPORTS_DIR

_MD_TABLE_ROW = re.compile(r"^\|\s*(.+?)\s*\|$")
_BUY_LINE = re.compile(
    r"\*\*(\d{4,5})\*\*\s+(.+?)\s+·\s+\*\*buy\*\*\s+·\s+ref\s+\*\*([\d.]+)\*\*\s+·\s+(.+)$",
)
_SELL_LINE = re.compile(
    r"\*\*(\d{4,5})\*\*\s+(.+?)\s+·\s+\*\*sell\*\*\s+·\s+ref\s+\*\*([\d.]+)\*\*\s+·\s+(.+)$",
)
_META_TICK = re.compile(r"tick 覆蓋：\*\*(\d+)\s*/\s*(\d+)\*\*")
_META_SLOTS = re.compile(r"空槽：\*\*(\d+)\s*/\s*(\d+)\*\*")
_META_LAST_CLOSE = re.compile(r"上一收盤 K 線 \*\*(\d{4}-\d{2}-\d{2})\*\*")


@dataclass(frozen=True)
class VcpCandidate:
    stock_id: str
    stock_name: str
    composite: float
    state: str
    pivot: str
    dist_pct: str
    stop: str


@dataclass(frozen=True)
class RrgCandidate:
    stock_id: str
    stock_name: str
    seg_last: float
    daily_pct: str
    fresh: bool


@dataclass(frozen=True)
class RadarBuy:
    stock_id: str
    stock_name: str
    ref: str
    detail: str


@dataclass(frozen=True)
class RadarSell:
    stock_id: str
    stock_name: str
    ref: str
    detail: str


@dataclass
class DigestInput:
    session_date: str
    vcp: list[VcpCandidate] = field(default_factory=list)
    rrg_fresh: list[RrgCandidate] = field(default_factory=list)
    rrg_all: list[RrgCandidate] = field(default_factory=list)
    c18acc_held: dict[str, str] = field(default_factory=dict)
    c18acc_fresh: set[str] = field(default_factory=set)
    buy_radar: list[RadarBuy] = field(default_factory=list)
    sell_radar: list[RadarSell] = field(default_factory=list)
    held_ids: set[str] = field(default_factory=set)
    vcp_tick: str = "—"
    rrg_tick: str = "—"
    rrg_free_slots: str = "—"
    last_close_date: str = "—"


def _split_cells(line: str) -> list[str]:
    m = _MD_TABLE_ROW.match(line.strip())
    if not m:
        return []
    return [c.strip() for c in m.group(1).split("|")]


def _is_separator(line: str) -> bool:
    cells = _split_cells(line)
    return bool(cells) and all(set(c) <= {"-", ":"} for c in cells if c)


def parse_vcp_brief(text: str) -> tuple[list[VcpCandidate], str, str]:
    """Parse pivot_gate section of vcp_funnel_specs or standalone brief."""
    tick = "—"
    last_close = "—"
    m = _META_TICK.search(text)
    if m:
        tick = f"{m.group(1)}/{m.group(2)}"
    m = _META_LAST_CLOSE.search(text)
    if m:
        last_close = m.group(1)

    rows: list[VcpCandidate] = []
    in_table = False
    for line in text.splitlines():
        if line.startswith("## 候選 Top"):
            in_table = True
            continue
        cells = _split_cells(line)
        if not cells:
            continue
        if cells[0] == "代號":
            in_table = True
            continue
        if _is_separator(line):
            continue
        if not in_table:
            continue
        if len(cells) < 7:
            continue
        try:
            composite = float(cells[2])
        except ValueError:
            continue
        rows.append(
            VcpCandidate(
                stock_id=cells[0],
                stock_name=cells[1],
                composite=composite,
                state=cells[3],
                pivot=cells[4],
                dist_pct=cells[5],
                stop=cells[6],
            )
        )
    return rows, tick, last_close


def parse_rrg_intraday(text: str) -> tuple[list[RrgCandidate], list[RrgCandidate], str, str]:
    fresh: list[RrgCandidate] = []
    all_rows: list[RrgCandidate] = []
    tick = "—"
    free_slots = "—"
    m = _META_TICK.search(text)
    if m:
        tick = f"{m.group(1)}/{m.group(2)}"
    m = _META_SLOTS.search(text)
    if m:
        free_slots = f"{m.group(1)}/{m.group(2)}"

    section = "header"
    for line in text.splitlines():
        if line.startswith("## ★ mono fresh"):
            section = "fresh"
            continue
        if line.startswith("## 所有 mono 候選"):
            section = "all"
            continue
        if line.startswith("## ") and not line.startswith("## ★"):
            if section in ("fresh", "all"):
                section = "other"
            continue
        cells = _split_cells(line)
        if not cells or _is_separator(line) or cells[0] in ("#", "代號"):
            continue
        if section == "fresh" and len(cells) >= 7 and cells[0].isdigit():
            try:
                seg = float(cells[3])
            except ValueError:
                continue
            fresh.append(
                RrgCandidate(
                    stock_id=cells[1],
                    stock_name=cells[2],
                    seg_last=seg,
                    daily_pct=cells[5],
                    fresh=True,
                )
            )
        elif section == "all" and len(cells) >= 7 and cells[0].isdigit():
            try:
                seg = float(cells[4])
            except ValueError:
                continue
            all_rows.append(
                RrgCandidate(
                    stock_id=cells[1],
                    stock_name=cells[2],
                    seg_last=seg,
                    daily_pct=cells[6],
                    fresh=cells[3] == "★",
                )
            )
    return fresh, all_rows, tick, free_slots


def parse_c18acc_screen(text: str) -> tuple[dict[str, str], set[str]]:
    held: dict[str, str] = {}
    fresh_ids: set[str] = set()
    section = "header"
    for line in text.splitlines():
        if line.startswith("## fresh mono 全池") or line.startswith("## fresh∪accel"):
            section = "fresh"
            continue
        if line.startswith("## 持倉（C18acc state）"):
            section = "held"
            continue
        if line.startswith("## "):
            if section in ("fresh", "held"):
                section = "other"
            continue
        cells = _split_cells(line)
        if not cells or _is_separator(line):
            continue
        if section == "fresh" and len(cells) >= 3 and cells[0].isdigit():
            fresh_ids.add(cells[1])
        elif section == "held" and len(cells) >= 3 and cells[0].isdigit():
            held[cells[1]] = cells[2]
    return held, fresh_ids


def parse_buy_radar(text: str) -> list[RadarBuy]:
    out: list[RadarBuy] = []
    for line in text.splitlines():
        m = _BUY_LINE.search(line)
        if m:
            out.append(
                RadarBuy(
                    stock_id=m.group(1),
                    stock_name=m.group(2).strip(),
                    ref=m.group(3),
                    detail=m.group(4).strip(),
                )
            )
    return out


def parse_sell_radar(text: str) -> list[RadarSell]:
    out: list[RadarSell] = []
    for line in text.splitlines():
        m = _SELL_LINE.search(line)
        if m:
            out.append(
                RadarSell(
                    stock_id=m.group(1),
                    stock_name=m.group(2).strip(),
                    ref=m.group(3),
                    detail=m.group(4).strip(),
                )
            )
    return out


def parse_morning_held_ids(text: str) -> set[str]:
    held: set[str] = set()
    in_table = False
    for line in text.splitlines():
        if line.startswith("## 持倉明細"):
            in_table = False
            continue
        cells = _split_cells(line)
        if not cells:
            continue
        if cells[0] == "代號":
            in_table = True
            continue
        if in_table and re.fullmatch(r"\d{4,5}", cells[0]):
            held.add(cells[0])
    return held


def _read_if_exists(path: Path) -> str:
    if path.is_file():
        return path.read_text(encoding="utf-8")
    return ""


def load_digest_input(
    *,
    session_date: str | None = None,
    reports_dir: Path = REPORTS_DIR,
) -> DigestInput:
    session = session_date or date.today().isoformat()
    stamp = session.replace("-", "")

    inp = DigestInput(session_date=session)

    vcp_text = _read_if_exists(reports_dir / f"{stamp}_vcp_pivot_gate_daily_brief.md")
    if not vcp_text:
        vcp_text = _read_if_exists(reports_dir / "vcp_pivot_gate_daily_brief.md")
    if vcp_text:
        inp.vcp, inp.vcp_tick, inp.last_close_date = parse_vcp_brief(vcp_text)

    rrg_text = _read_if_exists(reports_dir / f"{stamp}_rrg_mono_intraday_watch.md")
    if not rrg_text:
        rrg_text = _read_if_exists(reports_dir / "rrg_mono_intraday_watch.md")
    if rrg_text:
        inp.rrg_fresh, inp.rrg_all, inp.rrg_tick, inp.rrg_free_slots = parse_rrg_intraday(rrg_text)

    c18_text = _read_if_exists(reports_dir / f"{stamp}_rrg_c18acc_screen.md")
    if not c18_text:
        c18_text = _read_if_exists(reports_dir / "rrg_c18acc_screen.md")
    if c18_text:
        inp.c18acc_held, inp.c18acc_fresh = parse_c18acc_screen(c18_text)

    buy_text = _read_if_exists(reports_dir / f"{stamp}_buy_signal_radar.md")
    if buy_text:
        inp.buy_radar = parse_buy_radar(buy_text)

    sell_text = _read_if_exists(reports_dir / f"{stamp}_sell_signal_radar.md")
    if sell_text:
        inp.sell_radar = parse_sell_radar(sell_text)

    morning = _read_if_exists(reports_dir.parent / "order" / "snapshots" / f"morning_brief_{stamp}.md")
    if morning:
        inp.held_ids = parse_morning_held_ids(morning)

    return inp


def _sources_for_symbol(inp: DigestInput, sid: str) -> list[str]:
    tags: list[str] = []
    if any(c.stock_id == sid for c in inp.vcp):
        v = next(c for c in inp.vcp if c.stock_id == sid)
        tags.append(f"VCP composite {v.composite:.1f}")
    if sid in {c.stock_id for c in inp.rrg_fresh}:
        r = next(c for c in inp.rrg_fresh if c.stock_id == sid)
        tags.append(f"RRG mono fresh seg={r.seg_last:.3f}")
    elif sid in {c.stock_id for c in inp.rrg_all if c.fresh}:
        tags.append("RRG mono fresh")
    if sid in inp.c18acc_fresh:
        tags.append("C18acc fresh pool")
    if sid in inp.c18acc_held:
        tags.append(f"C18acc 持倉（{inp.c18acc_held[sid]}）")
    for b in inp.buy_radar:
        if b.stock_id == sid:
            ref = f" @ {b.ref}" if b.ref else ""
            tags.append(f"buy radar{ref}")
            break
    return tags


def format_intraday_1300_digest(
    inp: DigestInput,
    *,
    holdings_digest: str | None = None,
    holdings_error: str | None = None,
) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        f"【13:00 盤中研究摘要 · {inp.session_date}】",
        f"產出 {now}",
        "",
        "⚠ 盤中預估 · 13:30 收盤後可能翻轉",
        "⚠ 16:30 VCP / 16:40 RRG 收盤 screen 為準",
        "",
        f"資料基準：上一收盤 K {inp.last_close_date} + 今日 tick",
        f"VCP tick {inp.vcp_tick} · RRG tick {inp.rrg_tick} · RRG 空槽 {inp.rrg_free_slots}",
        "",
    ]

    from intraday_holdings_digest import append_holdings_section

    append_holdings_section(lines, holdings_digest, error=holdings_error)

    vcp_ids = {c.stock_id for c in inp.vcp}
    rrg_fresh_ids = {c.stock_id for c in inp.rrg_fresh}
    buy_ids = {b.stock_id for b in inp.buy_radar}
    c18_ids = set(inp.c18acc_held) | inp.c18acc_fresh

    overlap_ids: set[str] = set()
    for sid in vcp_ids | rrg_fresh_ids | buy_ids | c18_ids:
        n = sum(
            [
                sid in vcp_ids,
                sid in rrg_fresh_ids,
                sid in buy_ids,
                sid in c18_ids,
            ]
        )
        if n >= 2:
            overlap_ids.add(sid)

    if overlap_ids:
        lines.append("■ 多軌重疊（最優先）")
        for sid in sorted(overlap_ids):
            name = _symbol_name(inp, sid)
            tags = _sources_for_symbol(inp, sid)
            lines.append(f"  {sid} {name} · {' · '.join(tags)}")
        lines.append("")

    rrg_only = [c for c in inp.rrg_fresh if c.stock_id not in overlap_ids]
    if rrg_only:
        lines.append("■ RRG mono fresh（13:30 收盤仍成立才進場）")
        for c in rrg_only:
            lines.append(
                f"  {c.stock_id} {c.stock_name} · seg_last {c.seg_last:.3f} · 當日 {c.daily_pct}"
            )
        lines.append("")

    vcp_only = [c for c in inp.vcp if c.stock_id not in overlap_ids]
    if vcp_only:
        lines.append("■ VCP near pivot（Pivot Gate / Coil Close 同池）")
        for c in vcp_only:
            lines.append(
                f"  {c.stock_id} {c.stock_name} · composite {c.composite:.1f} · "
                f"{c.state} · pivot {c.pivot} · dist {c.dist_pct}% · stop {c.stop}"
            )
        lines.append("")

    if not overlap_ids and not rrg_only and not vcp_only:
        lines.append("■ 13:00 候選")
        lines.append("  目前 VCP / RRG mono 皆無符合條件的候選。")
        lines.append("")

    held_sells = [s for s in inp.sell_radar if not inp.held_ids or s.stock_id in inp.held_ids]
    if held_sells:
        lines.append("■ 持倉監控（sell signal radar · advisory）")
        for s in held_sells:
            lines.append(f"  {s.stock_id} {s.stock_name} · sell · ref {s.ref} · {s.detail}")
        lines.append("")

    lines.extend(
        [
            "■ 建議盯盤順序（→ 13:30 收盤）",
        ]
    )
    priority = 1
    for sid in sorted(overlap_ids):
        name = _symbol_name(inp, sid)
        lines.append(f"  {priority}. {sid} {name}（多軌）")
        priority += 1
    for c in rrg_only:
        lines.append(f"  {priority}. {c.stock_id} {c.stock_name}（RRG fresh）")
        priority += 1
    for c in vcp_only:
        lines.append(f"  {priority}. {c.stock_id} {c.stock_name}（VCP）")
        priority += 1
    for s in held_sells:
        lines.append(f"  {priority}. {s.stock_id} {s.stock_name}（持倉賣出監控）")
        priority += 1
    if priority == 1:
        lines.append("  — 本輪無需優先盯盤標的")

    lines.extend(
        [
            "",
            "系統 advisory · 人工確認後下單 · 詳細表格見 reports/daily/",
        ]
    )
    return "\n".join(lines)


def _symbol_name(inp: DigestInput, sid: str) -> str:
    for c in inp.vcp:
        if c.stock_id == sid:
            return c.stock_name
    for c in inp.rrg_fresh + inp.rrg_all:
        if c.stock_id == sid:
            return c.stock_name
    if sid in inp.c18acc_held:
        return inp.c18acc_held[sid]
    for b in inp.buy_radar:
        if b.stock_id == sid:
            return b.stock_name
    for s in inp.sell_radar:
        if s.stock_id == sid:
            return s.stock_name
    return ""


def write_digest(text: str, *, session_date: str, reports_dir: Path = REPORTS_DIR) -> Path:
    stamp = session_date.replace("-", "")
    reports_dir.mkdir(parents=True, exist_ok=True)
    dated = reports_dir / f"{stamp}_intraday_1300_digest.md"
    latest = reports_dir / "intraday_1300_digest.md"
    dated.write_text(text, encoding="utf-8")
    latest.write_text(text, encoding="utf-8")
    return dated


def required_reports_ready(*, session_date: str, reports_dir: Path = REPORTS_DIR) -> tuple[bool, list[str]]:
    stamp = session_date.replace("-", "")
    missing: list[str] = []
    for name in (
        f"{stamp}_vcp_pivot_gate_daily_brief.md",
        f"{stamp}_rrg_mono_intraday_watch.md",
    ):
        if not (reports_dir / name).is_file():
            missing.append(name)
    return not missing, missing
