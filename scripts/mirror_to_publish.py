#!/usr/bin/env python3
"""Mirror existing reports/daily/* artifacts into reports/publish/ (website layer VFP)."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from project_dotenv import load_project_dotenv
from website_publish import (
    discover_publish_dates,
    publish_etf_daily,
    publish_regime_daily,
    publish_vcp_funnel_specs,
    sync_strategy_catalog,
)


def _parse_dir_stamp(name: str) -> date | None:
    if len(name) != 8 or not name.isdigit():
        return None
    return date(int(name[:4]), int(name[4:6]), int(name[6:8]))


def mirror_legacy_to_publish() -> list[str]:
    actions: list[str] = []
    daily = ROOT / "reports" / "daily"

    etf = daily / "etf-daily" / "daily_brief.md"
    if etf.is_file():
        text = etf.read_text(encoding="utf-8")
        import re

        m = re.search(r"(\d{4}-\d{2}-\d{2})", text[:400])
        day = m.group(1) if m else date.today().isoformat()
        publish_etf_daily(text, day)
        actions.append(f"etf_daily → publish/facts/etf-daily ({day})")

    regime = daily / "regime"
    latest_md = regime / "daily_brief.md"
    if latest_md.is_file():
        md = latest_md.read_text(encoding="utf-8")
        import re

        m = re.search(r"(\d{4}-\d{2}-\d{2})", md[:400])
        day = m.group(1) if m else date.today().isoformat()
        html = (regime / "daily_brief.html").read_text(encoding="utf-8") if (regime / "daily_brief.html").is_file() else None
        embed = (regime / "daily_brief.embed.html").read_text(encoding="utf-8") if (regime / "daily_brief.embed.html").is_file() else None
        publish_regime_daily(md, day, content_html=html, embed_html=embed)
        actions.append(f"regime latest → publish/regime ({day})")

    snap_root = regime / "snapshots"
    if snap_root.is_dir():
        for child in sorted(snap_root.iterdir()):
            if not child.is_dir():
                continue
            d = _parse_dir_stamp(child.name)
            if d is None:
                continue
            md_path = child / "daily_brief.md"
            if not md_path.is_file():
                continue
            md = md_path.read_text(encoding="utf-8")
            html_path = child / "daily_brief.html"
            embed_path = child / "daily_brief.embed.html"
            publish_regime_daily(
                md,
                d.isoformat(),
                content_html=html_path.read_text(encoding="utf-8") if html_path.is_file() else None,
                embed_html=embed_path.read_text(encoding="utf-8") if embed_path.is_file() else None,
            )
            actions.append(f"regime snapshot {d.isoformat()}")

    for path in sorted(daily.glob("*_vcp_funnel_specs_daily_brief.md")):
        stamp = path.name.split("_", 1)[0]
        d = _parse_dir_stamp(stamp)
        if d is None:
            continue
        publish_vcp_funnel_specs(path.read_text(encoding="utf-8"), d.isoformat())
        actions.append(f"vcp_funnel_specs {d.isoformat()}")

    sync_strategy_catalog()
    actions.append("strategy/catalog.md from config/strategy.yaml")

    return actions


def main() -> int:
    load_project_dotenv()
    actions = mirror_legacy_to_publish()
    dates = discover_publish_dates(60)
    print(f"mirrored {len(actions)} items")
    for a in actions:
        print(f"  + {a}")
    print(f"publish dates ({len(dates)}): {', '.join(d.isoformat() for d in dates)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
