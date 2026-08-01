"""白名單作者部落格每日抓取器 — 純 Python, 確定性, launchd 安全。

只做「抓取 + 落庫」這件確定性的事 (無 LLM):
  1. GET /blog/daily-featured-data?page=1..N  (免登入 JSON)
  2. 過濾白名單 6 位 (docs/wantgoo-intelligence-audit.md §9 定案)
  3. 新 postId → GET /blog/{memberId}/post/{postId}/detail 取全文
  4. append-only 存進 data/wantgoo_loop.db  table blog_posts (status='staged')

擷取(把 story 變成 author_predictions 列)是**另一支** extract_predictions.py 做的 (需 LLM)。
兩段拆開 = 抓取穩定可排程、擷取可用 Claude session 或 API 隨後補。

白名單 (§9): 407988 華安/茲安 · 203663 摸金 · 343059 講客人 · 98845 小博士 · 144623 特派員 · 203664 X檔案。

用法:
  python fetch_blog_posts.py            # 抓今天精選 + 落庫
  python fetch_blog_posts.py --pages 3  # 多撈幾頁 (回補近幾日)
  python fetch_blog_posts.py --list     # 印出已 staged、尚未擷取的貼文
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))
from stock_db import DATA_DIR  # noqa: E402

DB = DATA_DIR / "wantgoo_loop.db"

WHITELIST = {
    "407988": "玩股華安/茲安",
    "203663": "玩股摸金",
    "343059": "玩股講客人",
    "98845": "玩股小博士",
    "144623": "玩股特派員",
    "203664": "玩股X檔案",
}

FEATURED = "https://www.wantgoo.com/blog/daily-featured-data?page={page}"
DETAIL = "https://www.wantgoo.com/blog/{mid}/post/{pid}/detail"

HDR = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*", "Accept-Language": "zh-TW,zh;q=0.9",
    "Referer": "https://www.wantgoo.com/blog", "X-Requested-With": "XMLHttpRequest",
    "Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS blog_posts (
  post_id     TEXT NOT NULL,
  member_id   TEXT NOT NULL,
  author_name TEXT,
  title       TEXT,
  summary     TEXT,
  publish_ms  INTEGER,
  publish_date TEXT,
  views       INTEGER,
  recommends  INTEGER,
  is_sell     INTEGER,          -- 付費文旗標 (isSell)
  story_text  TEXT,             -- 全文純文字 (HTML 去標籤)
  fetched_at  TEXT,
  extracted   INTEGER DEFAULT 0,-- 0=待擷取, 1=已跑 extractor (含"見文無可計分")
  PRIMARY KEY (member_id, post_id)
);
CREATE INDEX IF NOT EXISTS idx_bp_extracted ON blog_posts(extracted, publish_date);
"""


def _init(con):
    con.executescript(SCHEMA)


def _clean_html(html: str) -> str:
    if not html:
        return ""
    txt = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    txt = re.sub(r"<br\s*/?>", "\n", txt, flags=re.I)
    txt = re.sub(r"</p>", "\n", txt, flags=re.I)
    txt = re.sub(r"<[^>]+>", "", txt)
    txt = re.sub(r"&nbsp;", " ", txt)
    txt = re.sub(r"\n{3,}", "\n\n", txt)
    return txt.strip()


def _ms_to_date(ms) -> str | None:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).astimezone(
            timezone(__import__("datetime").timedelta(hours=8))).date().isoformat()
    except Exception:
        return None


def fetch(pages: int = 1, timeout: int = 30) -> dict:
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB); _init(con)
    seen_wl, new_posts, dup = 0, 0, 0
    sess = requests.Session(); sess.headers.update(HDR)
    for page in range(1, pages + 1):
        try:
            items = sess.get(FEATURED.format(page=page), timeout=timeout).json()
        except Exception as e:
            print(f"  ! featured page {page} failed: {type(e).__name__}: {str(e)[:80]}")
            continue
        for it in items:
            mid = str(it.get("memberId"))
            if mid not in WHITELIST:
                continue
            seen_wl += 1
            pid = str(it.get("postId"))
            exists = con.execute("SELECT 1 FROM blog_posts WHERE member_id=? AND post_id=?",
                                 (mid, pid)).fetchone()
            if exists:
                dup += 1
                continue
            story = ""
            try:
                d = sess.get(DETAIL.format(mid=mid, pid=pid), timeout=timeout).json()
                story = _clean_html(d.get("story") or d.get("content") or "")
            except Exception as e:
                print(f"  ! detail {mid}/{pid} failed: {type(e).__name__}: {str(e)[:60]}")
            con.execute(
                "INSERT OR IGNORE INTO blog_posts (post_id,member_id,author_name,title,summary,"
                "publish_ms,publish_date,views,recommends,is_sell,story_text,fetched_at,extracted) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0)",
                (pid, mid, it.get("nickName") or WHITELIST[mid], it.get("title"), it.get("summary"),
                 it.get("publishTime"), _ms_to_date(it.get("publishTime")), it.get("views"),
                 it.get("recommends"), int(bool(it.get("isSell"))), story,
                 datetime.now(timezone.utc).isoformat(timespec="seconds")))
            new_posts += 1
            print(f"  + {WHITELIST[mid]} [{pid}] {(it.get('title') or '')[:40]} "
                  f"({len(story)} chars{' · 付費' if it.get('isSell') else ''})")
    con.commit()
    pending = con.execute("SELECT COUNT(*) FROM blog_posts WHERE extracted=0").fetchone()[0]
    con.close()
    return {"whitelist_seen": seen_wl, "new": new_posts, "dup": dup, "pending_extract": pending}


def list_pending() -> None:
    if not DB.exists():
        print("(no db yet — run fetch first)"); return
    con = sqlite3.connect(DB); _init(con)
    rows = con.execute("SELECT publish_date,author_name,post_id,is_sell,length(story_text),title "
                       "FROM blog_posts WHERE extracted=0 ORDER BY publish_ms DESC").fetchall()
    con.close()
    print(f"=== {len(rows)} post(s) staged, awaiting extraction ===")
    for d, a, pid, sell, n, t in rows:
        print(f"  {d} | {a} [{pid}]{' 付費' if sell else ''} | {n or 0}c | {(t or '')[:44]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=1)
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args()
    if a.list:
        list_pending(); return
    r = fetch(pages=a.pages)
    print(f"fetch done: whitelist_seen={r['whitelist_seen']} new={r['new']} dup={r['dup']} "
          f"pending_extract={r['pending_extract']}")


if __name__ == "__main__":
    main()
