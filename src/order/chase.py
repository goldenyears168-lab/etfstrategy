"""Intraday odd chase · 盤中零股限價追賣一（開盤窗 · 每分鐘一輪）。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Literal

from stock_db import PROJECT_ROOT

CHASE_TAG = "chase_open"
SCHEMA_VERSION = "order-chase-v1"

SymbolStatus = Literal["active", "filled", "timeout", "disabled"]


@dataclass
class ChaseSpec:
    strategy_id: str
    budget_twd_per_symbol: int
    symbols: list[str]
    max_rounds: int = 5
    market_type: str = "intraday_odd"

    def validate(self) -> None:
        if not self.symbols:
            raise ValueError("symbols 不可為空")
        if self.budget_twd_per_symbol <= 0:
            raise ValueError("budget_twd_per_symbol 須 > 0")
        if self.max_rounds <= 0:
            raise ValueError("max_rounds 須 > 0")


@dataclass
class SymbolChaseState:
    status: SymbolStatus = "active"
    rounds: int = 0
    order_no: str | None = None
    target_shares: int = 0
    limit_price: float | None = None
    filled_shares: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "rounds": self.rounds,
            "order_no": self.order_no,
            "target_shares": self.target_shares,
            "limit_price": self.limit_price,
            "filled_shares": self.filled_shares,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> SymbolChaseState:
        return cls(
            status=str(raw.get("status") or "active"),  # type: ignore[assignment]
            rounds=int(raw.get("rounds") or 0),
            order_no=(str(raw["order_no"]) if raw.get("order_no") else None),
            target_shares=int(raw.get("target_shares") or 0),
            limit_price=(
                float(raw["limit_price"]) if raw.get("limit_price") is not None else None
            ),
            filled_shares=int(raw.get("filled_shares") or 0),
        )


@dataclass
class ChaseSessionState:
    trade_date: str
    strategy_id: str
    budget_twd_per_symbol: int
    max_rounds: int
    symbols: dict[str, SymbolChaseState] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "trade_date": self.trade_date,
            "strategy_id": self.strategy_id,
            "budget_twd_per_symbol": self.budget_twd_per_symbol,
            "max_rounds": self.max_rounds,
            "symbols": {k: v.to_dict() for k, v in self.symbols.items()},
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ChaseSessionState:
        sym_raw = raw.get("symbols") or {}
        symbols = {
            str(k): SymbolChaseState.from_dict(v if isinstance(v, dict) else {})
            for k, v in sym_raw.items()
        }
        return cls(
            trade_date=str(raw.get("trade_date") or ""),
            strategy_id=str(raw.get("strategy_id") or ""),
            budget_twd_per_symbol=int(raw.get("budget_twd_per_symbol") or 0),
            max_rounds=int(raw.get("max_rounds") or 5),
            symbols=symbols,
        )


def default_state_path(trade_date: str | None = None) -> Path:
    d = trade_date or date.today().strftime("%Y-%m-%d")
    stamp = d.replace("-", "")
    return PROJECT_ROOT / "reports" / "order" / "chase_state" / f"{stamp}.json"


def load_chase_spec(path: Path | str) -> ChaseSpec:
    p = Path(path)
    raw = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("chase spec 須為 JSON object")
    if raw.get("schema_version") not in (SCHEMA_VERSION, "order-intent-v1"):
        # legacy file: read metadata.symbols stock ids
        if raw.get("schema_version") == "order-intent-v1":
            meta = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
            syms = meta.get("symbols") or []
            ids = [str(s.get("stock_id")) for s in syms if isinstance(s, dict) and s.get("stock_id")]
            spec = ChaseSpec(
                strategy_id=str(raw.get("strategy_id") or "scheduled-open"),
                budget_twd_per_symbol=int(meta.get("budget_twd_per_symbol") or 10000),
                symbols=ids,
                max_rounds=int(meta.get("max_rounds") or 5),
            )
            spec.validate()
            return spec
    symbols = raw.get("symbols") or []
    if symbols and isinstance(symbols[0], dict):
        ids = [str(s.get("stock_id")) for s in symbols if isinstance(s, dict)]
    else:
        ids = [str(s) for s in symbols]
    spec = ChaseSpec(
        strategy_id=str(raw.get("strategy_id") or "scheduled-open"),
        budget_twd_per_symbol=int(raw.get("budget_twd_per_symbol") or 10000),
        symbols=ids,
        max_rounds=int(raw.get("max_rounds") or 5),
        market_type=str(raw.get("market_type") or "intraday_odd"),
    )
    spec.validate()
    return spec


def load_session_state(path: Path) -> ChaseSessionState | None:
    if not path.is_file():
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        return None
    return ChaseSessionState.from_dict(raw)


def save_session_state(path: Path, state: ChaseSessionState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(state.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def init_session_state(spec: ChaseSpec, trade_date: str) -> ChaseSessionState:
    return ChaseSessionState(
        trade_date=trade_date,
        strategy_id=spec.strategy_id,
        budget_twd_per_symbol=spec.budget_twd_per_symbol,
        max_rounds=spec.max_rounds,
        symbols={sym: SymbolChaseState() for sym in spec.symbols},
    )


def shares_for_budget(budget_twd: int, price: float) -> int:
    if price <= 0:
        raise ValueError("price 須 > 0")
    return max(1, int(budget_twd // price))
