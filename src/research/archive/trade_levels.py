"""手動價位 → 風險報酬比（不產生目標價建議，僅算 R:R）。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from stock_db import DATA_DIR, PROJECT_ROOT

DEFAULT_LEVELS_PATH = DATA_DIR / "manual_trade_levels.json"


@dataclass(frozen=True)
class TradeLevel:
    stock_id: str
    entry: float
    stop: float
    target: float
    note: str = ""

    @property
    def risk_pct(self) -> float | None:
        if self.entry <= 0:
            return None
        risk = (self.entry - self.stop) / self.entry * 100.0
        return round(risk, 2) if self.stop < self.entry else None

    @property
    def reward_pct(self) -> float | None:
        if self.entry <= 0:
            return None
        reward = (self.target - self.entry) / self.entry * 100.0
        return round(reward, 2) if self.target > self.entry else None

    @property
    def risk_reward(self) -> float | None:
        risk_amt = self.entry - self.stop
        reward_amt = self.target - self.entry
        if risk_amt <= 0 or reward_amt <= 0:
            return None
        return round(reward_amt / risk_amt, 2)

    @property
    def valid(self) -> bool:
        return self.entry > self.stop and self.target > self.entry


def load_manual_trade_levels(path: Path | None = None) -> list[TradeLevel]:
    p = path or DEFAULT_LEVELS_PATH
    if not p.exists():
        return []
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    items = raw if isinstance(raw, list) else raw.get("levels", [])
    out: list[TradeLevel] = []
    for item in items:
        if not isinstance(item, dict) or not item.get("stock_id"):
            continue
        try:
            out.append(
                TradeLevel(
                    stock_id=str(item["stock_id"]).strip(),
                    entry=float(item["entry"]),
                    stop=float(item["stop"]),
                    target=float(item["target"]),
                    note=str(item.get("note", ""))[:80],
                )
            )
        except (TypeError, ValueError):
            continue
    return out


def levels_for_stocks(
    stock_ids: list[str],
    path: Path | None = None,
) -> list[TradeLevel]:
    by_id = {lv.stock_id: lv for lv in load_manual_trade_levels(path)}
    return [by_id[sid] for sid in stock_ids if sid in by_id]


def levels_path_hint() -> str:
    try:
        return str(DEFAULT_LEVELS_PATH.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(DEFAULT_LEVELS_PATH)
