"""11:00 盤中持倉脈動 digest（持倉即時脈動 + sell radar 持倉交集）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from intraday_1300_digest import DigestInput, RadarSell, load_digest_input, parse_sell_radar
from intraday_holdings_digest import append_holdings_section
from report_paths import REPORTS_DIR


@dataclass
class MiddayDigestInput:
    session_date: str
    core: DigestInput | None = None
    sell_radar: list[RadarSell] = field(default_factory=list)
    held_ids: set[str] = field(default_factory=set)


def load_midday_digest_input(
    *,
    session_date: str | None = None,
    reports_dir=REPORTS_DIR,
) -> MiddayDigestInput:
    session = session_date or date.today().isoformat()
    inp = MiddayDigestInput(session_date=session)
    inp.core = load_digest_input(session_date=session, reports_dir=reports_dir)
    stamp = session.replace("-", "")
    sell_text = ""
    path = reports_dir / f"{stamp}_sell_signal_radar.md"
    if path.is_file():
        sell_text = path.read_text(encoding="utf-8")
    elif (reports_dir / "sell_signal_radar.md").is_file():
        sell_text = (reports_dir / "sell_signal_radar.md").read_text(encoding="utf-8")
    if sell_text:
        inp.sell_radar = parse_sell_radar(sell_text)
    inp.held_ids = set(inp.core.held_ids if inp.core else ())
    return inp


def format_intraday_midday_digest(
    inp: MiddayDigestInput,
    *,
    holdings_digest: str | None = None,
    holdings_error: str | None = None,
) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        f"【11:00 盤中持倉脈動 · {inp.session_date}】",
        f"產出 {now}",
        "",
        "量測：現價＝富邦即時報價；RRG 盤中＝1m K @ 輪詢分鐘（缺 K 時 tick fallback）",
        "",
    ]
    append_holdings_section(lines, holdings_digest, error=holdings_error)

    held_sells = [
        s for s in inp.sell_radar if not inp.held_ids or s.stock_id in inp.held_ids
    ]
    if held_sells:
        lines.extend(["■ 持倉監控（sell signal radar · advisory）", ""])
        for s in held_sells:
            lines.append(f"  {s.stock_id} {s.stock_name} · sell · ref {s.ref} · {s.detail}")
        lines.append("")

    lines.extend(
        [
            "■ 紀律",
            "  · 09:00–09:04 只觀察、不賣",
            "  · 13:02 盤中研究摘要（VCP + RRG）",
            "  · 13:20 後停止自動減碼",
            "",
            "系統 advisory · 人工確認後下單",
        ]
    )
    return "\n".join(lines)


def write_midday_digest(text: str, *, session_date: str, reports_dir=REPORTS_DIR):
    from pathlib import Path

    reports_dir = Path(reports_dir)
    stamp = session_date.replace("-", "")
    reports_dir.mkdir(parents=True, exist_ok=True)
    dated = reports_dir / f"{stamp}_intraday_midday_digest.md"
    latest = reports_dir / "intraday_midday_digest.md"
    dated.write_text(text, encoding="utf-8")
    latest.write_text(text, encoding="utf-8")
    return dated
