"""L7 事件排名（手動 JSON + DB + 7 日窗口；指數調整類不進 Event Top）。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import sqlite3

from stock_db import DATA_DIR, PROJECT_ROOT

DEFAULT_EVENTS_PATH = DATA_DIR / "manual_events.json"
CATALYST_TYPES = frozenset(
    {
        "PRODUCT_CYCLE",
        "SUPPLY_CHAIN",
        "POLICY",
        "CAPX",
        "EARNINGS",
        "SELL_SIDE",
        "VALUATION",
        "INDEX_REBALANCE",
    }
)
INDUSTRY_CATALYST_TYPES = CATALYST_TYPES - {"INDEX_REBALANCE"}

EXPLAINS_WEIGHT = {
    "HIGH": 1.25,
    "MED": 1.0,
    "LOW": 0.75,
    "NONE": 0.5,
}
POLARITY_WEIGHT = {
    "BULL": 1.1,
    "BEAR": 1.0,
    "NEUTRAL": 0.9,
}

# headline / type 命中則視為指數調整（不進 Event Top、不進催化子分）
_INDEX_RE = re.compile(
    r"MSCI|指數調整|成分股調整|被動資金|權重調整|台灣50|台灣領袖|"
    r"FTSE|富時|調整生效|納入指數|剔除指數|INDEX\s*REBALANCE",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CatalystEvent:
    stock_id: str
    event_date: date
    catalyst_type: str
    headline: str
    polarity: str = "NEUTRAL"
    explains_etf_add: str = "NONE"
    confidence: int = 50
    sources: list[dict] | None = None

    @property
    def score(self) -> float:
        return score_event(self)


@dataclass(frozen=True)
class RankedEvent:
    rank: int
    stock_id: str
    event: CatalystEvent
    event_score: float


def is_index_rebalance_headline(headline: str, *, catalyst_type: str = "") -> bool:
    text = f"{headline} {catalyst_type}"
    return bool(_INDEX_RE.search(text))


def is_index_rebalance_event(event: CatalystEvent) -> bool:
    """MSCI / 指數成分調整等總經標的 — 不當個股產業催化。"""
    if event.catalyst_type == "INDEX_REBALANCE":
        return True
    return is_index_rebalance_headline(event.headline, catalyst_type=event.catalyst_type)


def normalize_catalyst_type(raw: str, headline: str = "") -> str:
    """正規化類型；MSCI 類關鍵字 → INDEX_REBALANCE。"""
    ctype = str(raw or "EARNINGS").upper()
    if ctype == "INDEX_REBALANCE" or _INDEX_RE.search(headline):
        return "INDEX_REBALANCE"
    if ctype not in CATALYST_TYPES:
        return "EARNINGS"
    return ctype


def _parse_date(value: str) -> date:
    return date.fromisoformat(str(value)[:10])


def load_manual_events(path: Path | None = None) -> list[CatalystEvent]:
    """讀取 `data/manual_events.json`；缺檔或格式錯誤回傳空列表。"""
    p = path or DEFAULT_EVENTS_PATH
    if not p.exists():
        return []
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    items = raw if isinstance(raw, list) else raw.get("events", [])
    out: list[CatalystEvent] = []
    for item in items:
        if not isinstance(item, dict) or not item.get("stock_id"):
            continue
        headline = str(item.get("headline", ""))[:80]
        ctype = normalize_catalyst_type(
            str(item.get("catalyst_type", "EARNINGS")),
            headline,
        )
        polarity = str(item.get("polarity", "NEUTRAL")).upper()
        if polarity not in POLARITY_WEIGHT:
            polarity = "NEUTRAL"
        explains = str(item.get("explains_etf_add", "NONE")).upper()
        if explains not in EXPLAINS_WEIGHT:
            explains = "NONE"
        try:
            conf = int(item.get("confidence", 50))
        except (TypeError, ValueError):
            conf = 50
        conf = max(0, min(100, conf))
        out.append(
            CatalystEvent(
                stock_id=str(item["stock_id"]).strip(),
                event_date=_parse_date(str(item["event_date"])),
                catalyst_type=ctype,
                headline=headline,
                polarity=polarity,
                explains_etf_add=explains,
                confidence=conf,
                sources=item.get("sources") if isinstance(item.get("sources"), list) else None,
            )
        )
    return out


def score_event(
    event: CatalystEvent,
    *,
    as_of: date | None = None,
    window_days: int = 7,
) -> float:
    """7 日內產業/基本面事件分；指數調整類固定 0。"""
    if is_index_rebalance_event(event):
        return 0.0
    today = as_of or date.today()
    age = (today - event.event_date).days
    if age < 0 or age > window_days:
        return 0.0
    recency = 1.0 - (age / max(window_days, 1))
    conf = event.confidence / 100.0
    return (
        conf
        * (0.55 + 0.45 * recency)
        * POLARITY_WEIGHT.get(event.polarity, 1.0)
        * EXPLAINS_WEIGHT.get(event.explains_etf_add, 0.5)
    )


def filter_events_in_window(
    events: list[CatalystEvent],
    *,
    as_of: date | None = None,
    window_days: int = 7,
    pool_stock_ids: set[str] | None = None,
    industry_only: bool = True,
) -> list[CatalystEvent]:
    today = as_of or date.today()
    start = today - timedelta(days=window_days)
    kept: list[CatalystEvent] = []
    for ev in events:
        if industry_only and is_index_rebalance_event(ev):
            continue
        if ev.event_date < start or ev.event_date > today:
            continue
        if pool_stock_ids is not None and ev.stock_id not in pool_stock_ids:
            continue
        if score_event(ev, as_of=today, window_days=window_days) <= 0:
            continue
        kept.append(ev)
    return kept


def rank_events(
    events: list[CatalystEvent],
    *,
    top_n: int = 10,
    as_of: date | None = None,
    window_days: int = 7,
    pool_stock_ids: set[str] | None = None,
) -> list[RankedEvent]:
    """依 stock_id 取最高分產業事件後排名（排除 MSCI/指數調整）。"""
    filtered = filter_events_in_window(
        events,
        as_of=as_of,
        window_days=window_days,
        pool_stock_ids=pool_stock_ids,
        industry_only=True,
    )
    best_by_stock: dict[str, CatalystEvent] = {}
    best_score: dict[str, float] = {}
    today = as_of or date.today()
    for ev in filtered:
        sc = score_event(ev, as_of=today, window_days=window_days)
        prev = best_score.get(ev.stock_id)
        if prev is None or sc > prev:
            best_by_stock[ev.stock_id] = ev
            best_score[ev.stock_id] = sc

    ranked = sorted(best_score.items(), key=lambda x: x[1], reverse=True)[:top_n]
    return [
        RankedEvent(rank=i + 1, stock_id=sid, event=best_by_stock[sid], event_score=sc)
        for i, (sid, sc) in enumerate(ranked)
    ]


def catalyst_event_id(ev: CatalystEvent) -> str:
    import hashlib

    raw = f"{ev.stock_id}|{ev.event_date}|{ev.catalyst_type}|{ev.headline}"
    return hashlib.sha256(raw.encode()).hexdigest()[:20]


def row_to_catalyst_event(row: sqlite3.Row) -> CatalystEvent:
    sources = None
    if row["sources_json"]:
        try:
            sources = json.loads(row["sources_json"])
        except json.JSONDecodeError:
            sources = None
    headline = row["headline"]
    ctype = normalize_catalyst_type(row["catalyst_type"], headline)
    return CatalystEvent(
        stock_id=row["stock_id"],
        event_date=_parse_date(row["event_date"]),
        catalyst_type=ctype,
        headline=headline,
        polarity=row["polarity"],
        explains_etf_add=row["explains_etf_add"],
        confidence=int(row["confidence"]),
        sources=sources if isinstance(sources, list) else None,
    )


def load_catalyst_events_from_db(
    conn: sqlite3.Connection,
    *,
    stock_ids: set[str] | None = None,
    window_days: int = 7,
    as_of: date | None = None,
) -> list[CatalystEvent]:
    from stock_db import load_catalyst_events

    ids = list(stock_ids) if stock_ids else None
    ref = (as_of or date.today()).isoformat()
    rows = load_catalyst_events(
        conn, stock_ids=ids, window_days=window_days, as_of=ref
    )
    return [row_to_catalyst_event(r) for r in rows]


def manual_events_enabled() -> bool:
    import os

    return os.environ.get("USE_MANUAL_EVENTS", "0").strip() == "1"


def load_all_catalyst_events(
    conn: sqlite3.Connection | None,
    path: Path | None = None,
    *,
    pool_stock_ids: set[str] | None = None,
    window_days: int = 7,
    as_of: date | None = None,
) -> list[CatalystEvent]:
    """合併 DB catalyst_events；manual JSON 在 USE_MANUAL_EVENTS=1 或 path 非預設檔時併入。"""
    by_id: dict[str, CatalystEvent] = {}
    if conn is not None:
        for ev in load_catalyst_events_from_db(
            conn,
            stock_ids=pool_stock_ids,
            window_days=window_days,
            as_of=as_of,
        ):
            by_id[catalyst_event_id(ev)] = ev
    merge_manual = manual_events_enabled()
    if path is not None:
        try:
            merge_manual = merge_manual or path.resolve() != DEFAULT_EVENTS_PATH.resolve()
        except OSError:
            merge_manual = True
    if merge_manual:
        manual_path = path if path is not None else DEFAULT_EVENTS_PATH
        for ev in load_manual_events(manual_path):
            if pool_stock_ids is not None and ev.stock_id not in pool_stock_ids:
                continue
            by_id.setdefault(catalyst_event_id(ev), ev)
    return list(by_id.values())


def events_path_hint() -> str:
    try:
        return str(DEFAULT_EVENTS_PATH.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(DEFAULT_EVENTS_PATH)


def purge_index_rebalance_from_db(conn: sqlite3.Connection) -> int:
    """刪除 catalyst_events 中 MSCI/指數調整類（含 headline 命中）。"""
    try:
        rows = conn.execute("SELECT * FROM catalyst_events").fetchall()
    except sqlite3.OperationalError:
        return 0
    ids: list[str] = []
    for row in rows:
        ev = row_to_catalyst_event(row)
        if is_index_rebalance_event(ev):
            ids.append(row["event_id"])
    if not ids:
        return 0
    conn.executemany(
        "DELETE FROM catalyst_events WHERE event_id = ?",
        [(eid,) for eid in ids],
    )
    conn.commit()
    return len(ids)
