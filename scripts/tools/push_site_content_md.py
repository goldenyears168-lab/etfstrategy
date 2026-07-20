#!/usr/bin/env python3
"""Push site_content Markdown from git HEAD blobs (content + registry · §7.4).

Reads ``supabase/site/**/*.md`` from the current git ``HEAD`` tree (no working-tree
``supabase/site/`` required). Upserts ``stock_research.site_content`` including
013 registry columns when present in frontmatter.

Usage:
  PYTHONPATH=src .venv/bin/python scripts/push_site_content_md.py
  PYTHONPATH=src .venv/bin/python scripts/push_site_content_md.py --page research_case_copytrade
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from project_dotenv import load_project_dotenv
from site_content_sync import SitePage, upsert_site_page
from supabase_research_sync import dashboard_url, supabase_configured

_GIT_SITE_PREFIX = "supabase/site/"


def _git_list_site_md() -> list[str]:
    out = subprocess.check_output(
        ["git", "ls-tree", "-r", "--name-only", "HEAD", "supabase/site"],
        cwd=ROOT,
        text=True,
    )
    paths: list[str] = []
    for line in out.splitlines():
        if not line.endswith(".md") or line.endswith("README.md"):
            continue
        paths.append(line)
    return sorted(paths)


def _git_show(relpath: str) -> str:
    return subprocess.check_output(
        ["git", "show", f"HEAD:{relpath}"],
        cwd=ROOT,
        text=True,
    )


def load_pages_from_git(page_filter: str | None = None) -> list[SitePage]:
    from site_content_sync import _page_from_file  # noqa: PLC0415
    import tempfile  # noqa: PLC0415

    pages: list[SitePage] = []
    for relpath in _git_list_site_md():
        text = _git_show(relpath)
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".md",
            encoding="utf-8",
            delete=False,
        ) as tmp:
            tmp.write(text)
            tmp_path = Path(tmp.name)
        try:
            page = _page_from_file(tmp_path)
        finally:
            tmp_path.unlink(missing_ok=True)
        if page_filter and page.page_id != page_filter:
            continue
        pages.append(page)
    pages.sort(key=lambda p: p.sort_order)
    return pages


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Push site_content from git HEAD MD")
    parser.add_argument("--page", dest="page_id", help="Only upsert this page_id")
    args = parser.parse_args(argv)

    load_project_dotenv()
    if not supabase_configured():
        print(
            "Supabase 未設定：請在 .env 加入 SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY",
            file=sys.stderr,
        )
        return 2

    if not _git_list_site_md():
        print("git HEAD 無 supabase/site/*.md", file=sys.stderr)
        return 2

    pages = load_pages_from_git(args.page_id)
    if not pages:
        print("no pages matched", file=sys.stderr)
        return 2

    uploaded: list[str] = []
    for page in pages:
        upsert_site_page(page)
        uploaded.append(page.page_id)
        print(f"  + {page.page_id}")

    print(f"uploaded: {len(uploaded)}")
    print(f"dashboard: {dashboard_url()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
