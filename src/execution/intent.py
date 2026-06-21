"""ExecutionIntent · 策略層 → 執行層契約（JSON SSOT）。

策略 / research 腳本只需寫入符合 schema 的 JSON，勿 import execution package。
執行層讀取後轉為富邦 Neo Order 並送出。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

SCHEMA_VERSION = "execution-intent-v1"

Side = Literal["buy", "sell"]
PriceTypeName = Literal["limit", "market", "reference", "limit_up", "limit_down"]
MarketTypeName = Literal["common", "odd", "intraday_odd", "emg"]
TimeInForceName = Literal["rod", "ioc", "fok"]
OrderTypeName = Literal["stock", "daytrade", "margin", "short"]


@dataclass
class ExecutionIntent:
    """單筆委託意圖。

    二選一指定數量：
    - ``side`` + ``quantity_shares``：明確買賣股數（delta）
    - ``target_shares``：目標庫存；執行層依現有庫存計算 delta
    """

    symbol: str
    side: Side | None = None
    quantity_shares: int | None = None
    target_shares: int | None = None
    price: str | None = None
    price_type: PriceTypeName = "limit"
    market_type: MarketTypeName = "common"
    time_in_force: TimeInForceName = "rod"
    order_type: OrderTypeName = "stock"
    user_def: str | None = None
    note: str | None = None

    def validate(self) -> None:
        sym = str(self.symbol or "").strip()
        if not sym:
            raise ValueError("symbol 不可為空")

        has_delta = self.side is not None and self.quantity_shares is not None
        has_target = self.target_shares is not None
        if has_delta and has_target:
            raise ValueError(f"{sym}: 不可同時指定 quantity_shares 與 target_shares")
        if not has_delta and not has_target:
            raise ValueError(f"{sym}: 需指定 (side + quantity_shares) 或 target_shares")
        if has_delta:
            if self.side not in ("buy", "sell"):
                raise ValueError(f"{sym}: side 須為 buy 或 sell")
            qty = int(self.quantity_shares or 0)
            if qty <= 0:
                raise ValueError(f"{sym}: quantity_shares 須 > 0")
        else:
            tgt = int(self.target_shares or 0)
            if tgt < 0:
                raise ValueError(f"{sym}: target_shares 不可為負")
        if self.price_type == "limit" and not str(self.price or "").strip():
            raise ValueError(f"{sym}: price_type=limit 時須提供 price")


@dataclass
class ExecutionIntentBatch:
    schema_version: str
    strategy_id: str
    as_of: str
    intents: list[ExecutionIntent] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(
                f"不支援 schema_version={self.schema_version!r}，預期 {SCHEMA_VERSION}"
            )
        if not str(self.strategy_id or "").strip():
            raise ValueError("strategy_id 不可為空")
        if not str(self.as_of or "").strip():
            raise ValueError("as_of 不可為空")
        if not self.intents:
            raise ValueError("intents 不可為空")
        for item in self.intents:
            item.validate()


@dataclass(frozen=True)
class ResolvedOrder:
    """策略意圖解析後、即將送出的委託（仍為股數）。"""

    symbol: str
    side: Side
    quantity_shares: int
    price: str | None
    price_type: PriceTypeName
    market_type: MarketTypeName
    time_in_force: TimeInForceName
    order_type: OrderTypeName
    user_def: str | None
    note: str | None
    source: Literal["delta", "target"]
    current_shares: int | None = None
    target_shares: int | None = None


def _intent_from_dict(raw: dict[str, Any]) -> ExecutionIntent:
    return ExecutionIntent(
        symbol=str(raw.get("symbol") or "").strip(),
        side=(str(raw["side"]).lower() if raw.get("side") is not None else None),
        quantity_shares=(
            int(raw["quantity_shares"]) if raw.get("quantity_shares") is not None else None
        ),
        target_shares=(
            int(raw["target_shares"]) if raw.get("target_shares") is not None else None
        ),
        price=(str(raw["price"]) if raw.get("price") is not None else None),
        price_type=str(raw.get("price_type") or "limit").lower(),  # type: ignore[arg-type]
        market_type=str(raw.get("market_type") or "common").lower(),  # type: ignore[arg-type]
        time_in_force=str(raw.get("time_in_force") or "rod").lower(),  # type: ignore[arg-type]
        order_type=str(raw.get("order_type") or "stock").lower(),  # type: ignore[arg-type]
        user_def=(str(raw["user_def"]) if raw.get("user_def") is not None else None),
        note=(str(raw["note"]) if raw.get("note") is not None else None),
    )


def load_intent_batch(path: Path | str) -> ExecutionIntentBatch:
    p = Path(path)
    raw = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("intent 檔案須為 JSON object")
    intents_raw = raw.get("intents") or []
    if not isinstance(intents_raw, list):
        raise ValueError("intents 須為 array")
    batch = ExecutionIntentBatch(
        schema_version=str(raw.get("schema_version") or ""),
        strategy_id=str(raw.get("strategy_id") or ""),
        as_of=str(raw.get("as_of") or ""),
        intents=[_intent_from_dict(x) for x in intents_raw if isinstance(x, dict)],
        metadata=dict(raw.get("metadata") or {}),
    )
    batch.validate()
    return batch


def dump_intent_batch(batch: ExecutionIntentBatch, path: Path | str) -> None:
    payload = {
        "schema_version": batch.schema_version,
        "strategy_id": batch.strategy_id,
        "as_of": batch.as_of,
        "metadata": batch.metadata,
        "intents": [asdict(x) for x in batch.intents],
    }
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def resolve_intents(
    batch: ExecutionIntentBatch,
    holdings_by_symbol: dict[str, int],
) -> list[ResolvedOrder]:
    """將策略意圖解析為可下單列；target 模式會扣掉現有庫存。"""
    batch.validate()
    out: list[ResolvedOrder] = []
    for intent in batch.intents:
        if intent.quantity_shares is not None and intent.side is not None:
            out.append(
                ResolvedOrder(
                    symbol=intent.symbol,
                    side=intent.side,
                    quantity_shares=int(intent.quantity_shares),
                    price=intent.price,
                    price_type=intent.price_type,
                    market_type=intent.market_type,
                    time_in_force=intent.time_in_force,
                    order_type=intent.order_type,
                    user_def=intent.user_def,
                    note=intent.note,
                    source="delta",
                )
            )
            continue

        current = int(holdings_by_symbol.get(intent.symbol, 0))
        target = int(intent.target_shares or 0)
        delta = target - current
        if delta == 0:
            continue
        out.append(
            ResolvedOrder(
                symbol=intent.symbol,
                side="buy" if delta > 0 else "sell",
                quantity_shares=abs(delta),
                price=intent.price,
                price_type=intent.price_type,
                market_type=intent.market_type,
                time_in_force=intent.time_in_force,
                order_type=intent.order_type,
                user_def=intent.user_def,
                note=intent.note,
                source="target",
                current_shares=current,
                target_shares=target,
            )
        )
    return out
