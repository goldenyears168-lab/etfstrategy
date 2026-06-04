"""L7 事件排名（P1 stub：手動 JSON + 7 日窗口，無新聞 API）。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

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
    }
)
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
        ctype = str(item.get("catalyst_type", "EARNINGS")).upper()
        if ctype not in CATALYST_TYPES:
            ctype = "EARNINGS"
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
                headline=str(item.get("headline", ""))[:80],
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
    """7 日內事件分；越新、信心越高、explains_etf_add 越高 → 分數越高。"""
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
) -> list[CatalystEvent]:
    today = as_of or date.today()
    start = today - timedelta(days=window_days)
    kept: list[CatalystEvent] = []
    for ev in events:
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
    """依 stock_id 取最高分事件後排名（每檔至多一筆代表事件）。"""
    filtered = filter_events_in_window(
        events,
        as_of=as_of,
        window_days=window_days,
        pool_stock_ids=pool_stock_ids,
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


def events_path_hint() -> str:
    try:
        return str(DEFAULT_EVENTS_PATH.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(DEFAULT_EVENTS_PATH)
