"""Load supabase/site/*.md and upsert to stock_research.site_content."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yaml

from stock_db import PROJECT_ROOT
from supabase_research_sync import _headers, _rest_url, supabase_configured

_TPE = ZoneInfo("Asia/Taipei")
_TABLE = "site_content"
SITE_DIR = PROJECT_ROOT / "supabase" / "site"

_REQUIRED_META = ("page_id", "layer_id", "title", "tab_label_zh", "tab_label_en", "sort_order")


@dataclass(frozen=True)
class SitePage:
    page_id: str
    layer_id: str
    title: str
    content_md: str
    role: str | None = None
    data_sources: str | None = None
    web_v1: str | None = None
    tab_label_zh: str | None = None
    tab_label_en: str | None = None
    sort_order: int = 0
    content_html: str | None = None


def parse_site_markdown(path: Path) -> tuple[dict[str, object], str]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        raise ValueError(f"{path.name}: missing YAML frontmatter (---)")
    _, fm, body = text.split("---", 2)
    meta = yaml.safe_load(fm) or {}
    if not isinstance(meta, dict):
        raise ValueError(f"{path.name}: frontmatter must be a mapping")
    return meta, body.lstrip("\n")


def _page_from_file(path: Path) -> SitePage:
    meta, body = parse_site_markdown(path)
    for key in _REQUIRED_META:
        if key not in meta:
            raise ValueError(f"{path.name}: frontmatter missing {key!r}")
    return SitePage(
        page_id=str(meta["page_id"]),
        layer_id=str(meta["layer_id"]),
        title=str(meta["title"]),
        content_md=body,
        role=str(meta["role"]) if meta.get("role") is not None else None,
        data_sources=str(meta["data_sources"]) if meta.get("data_sources") is not None else None,
        web_v1=str(meta["web_v1"]) if meta.get("web_v1") is not None else None,
        tab_label_zh=str(meta["tab_label_zh"]),
        tab_label_en=str(meta["tab_label_en"]),
        sort_order=int(meta["sort_order"]),
        content_html=str(meta["content_html"]) if meta.get("content_html") else None,
    )


def load_all_pages() -> list[SitePage]:
    """Read all supabase/site/**/*.md except README.md."""
    if not SITE_DIR.is_dir():
        raise FileNotFoundError(f"site content dir missing: {SITE_DIR}")
    pages: list[SitePage] = []
    for path in sorted(SITE_DIR.rglob("*.md")):
        if path.name.upper() == "README.MD":
            continue
        pages.append(_page_from_file(path))
    pages.sort(key=lambda p: p.sort_order)
    return pages


def build_all_pages() -> list[SitePage]:
    """Alias for sync script."""
    return load_all_pages()


def _site_content_url() -> str:
    base = _rest_url().rsplit("/", 1)[0]
    return f"{base}/{_TABLE}"


def upsert_site_page(page: SitePage) -> None:
    payload = {
        "page_id": page.page_id,
        "layer_id": page.layer_id,
        "title": page.title,
        "content_md": page.content_md,
        "content_html": page.content_html,
        "role": page.role,
        "data_sources": page.data_sources,
        "web_v1": page.web_v1,
        "tab_label_zh": page.tab_label_zh,
        "tab_label_en": page.tab_label_en,
        "sort_order": page.sort_order,
        "updated_at": datetime.now(_TPE).isoformat(),
    }
    resp = requests.post(
        _site_content_url(),
        headers=_headers(),
        json=payload,
        params={"on_conflict": "page_id"},
        timeout=120,
    )
    if resp.status_code >= 400:
        raise RuntimeError(
            f"Supabase site_content upsert failed ({page.page_id}): "
            f"{resp.status_code} {resp.text[:500]}"
        )


_RETIRED_PAGE_IDS = ("layer_execution",)


def delete_site_page(page_id: str) -> None:
    resp = requests.delete(
        _site_content_url(),
        headers=_headers(),
        params={"page_id": f"eq.{page_id}"},
        timeout=60,
    )
    if resp.status_code >= 400:
        raise RuntimeError(
            f"Supabase site_content delete failed ({page_id}): "
            f"{resp.status_code} {resp.text[:500]}"
        )


def sync_all_site_content() -> list[str]:
    if not supabase_configured():
        raise RuntimeError("Supabase 未設定")
    for page_id in _RETIRED_PAGE_IDS:
        delete_site_page(page_id)
    uploaded: list[str] = []
    for page in load_all_pages():
        upsert_site_page(page)
        uploaded.append(page.page_id)
    return uploaded
