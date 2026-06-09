"""開盤執行政策：ref_price vs open_price → 市價／限價（E0）。"""

from __future__ import annotations

from dataclasses import dataclass

from investment_policy import InvestmentPolicy

ORDER_PENDING_OPEN = "pending_open"
ORDER_MARKET_ROD = "market_rod"
ORDER_LIMIT_ROD = "limit_rod"


@dataclass(frozen=True)
class OpenExecutionDecision:
    order_type_effective: str
    note: str


def resolve_open_execution(
    *,
    ref_price: float,
    open_price: float,
    ips: InvestmentPolicy,
) -> OpenExecutionDecision:
    if ips.open_execution_policy != "compare_at_open":
        return OpenExecutionDecision(
            ORDER_LIMIT_ROD,
            f"限價 @ {ref_price:.2f}（policy={ips.open_execution_policy}）",
        )
    if ref_price >= open_price:
        return OpenExecutionDecision(
            ips.favorable_open_action,
            f"參考 {ref_price:.2f} >= 開盤 {open_price:.2f} → 市價 ROD",
        )
    return OpenExecutionDecision(
        ips.unfavorable_open_action,
        f"參考 {ref_price:.2f} < 開盤 {open_price:.2f} → 限價 @ {ref_price:.2f}",
    )


def hypothetical_execution_note(
    ref_price: float,
    *,
    assumed_open: float | None = None,
    ips: InvestmentPolicy,
) -> str:
    """報告用：無開盤價時標示若開盤=X 會如何（驗收 §21.11 #7）。"""
    if assumed_open is None:
        assumed_open = ref_price
    d = resolve_open_execution(
        ref_price=ref_price, open_price=assumed_open, ips=ips
    )
    return (
        f"假設開盤={assumed_open:.2f} → {d.order_type_effective}；"
        f"開盤<{ref_price:.2f} 則 {ORDER_MARKET_ROD}，開盤>{ref_price:.2f} 則 {ORDER_LIMIT_ROD}"
    )
