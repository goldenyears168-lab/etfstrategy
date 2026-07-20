#!/usr/bin/env python3
"""Upload supabase/site/*.md → stock_research.site_content (§7.4 · local tree)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from project_dotenv import load_project_dotenv
from site_content_sync import SITE_DIR, load_all_pages, sync_all_site_content
from supabase_research_sync import dashboard_url, supabase_configured


def main() -> int:
    load_project_dotenv()
    if not supabase_configured():
        print(
            "Supabase 未設定：請在 .env 加入 SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY",
            file=sys.stderr,
        )
        return 2

    if not SITE_DIR.is_dir():
        print(
            f"缺少 {SITE_DIR} · 改用 scripts/push_site_content_md.py（從 git HEAD 讀取）",
            file=sys.stderr,
        )
        return 2

    pages = load_all_pages()
    print(f"source: {SITE_DIR}")
    print(f"pages:  {len(pages)}")

    uploaded = sync_all_site_content()
    print(f"uploaded: {len(uploaded)}")
    print(f"dashboard: {dashboard_url()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
