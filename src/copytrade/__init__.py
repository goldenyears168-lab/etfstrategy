"""Copytrade track · signal iteration (mainline-safe, no backtest deps)."""

from copytrade.branch_signals import (
    BRANCH_BUY_ACTION,
    DEFAULT_TRADER_ID_FUBON_XINDIAN,
    DEFAULT_TRADER_ID_KGI_CHENGZHONG,
    DEFAULT_TRADER_ID_YUANTA_SONGJIANG,
    SHARES_PER_LOT,
    group_branch_signals_by_date,
    iter_branch_buy_signals,
    mean_net,
)
from copytrade.signals import (
    ADD_ACTIONS,
    INITIATION_ACTION,
    REPEAT_ADD_ACTION,
    CopytradeSignal,
    filter_grouped_signals,
    group_signals_by_date,
    iter_copytrade_signals,
    snapshot_pairs,
)

__all__ = [
    "ADD_ACTIONS",
    "BRANCH_BUY_ACTION",
    "DEFAULT_TRADER_ID_FUBON_XINDIAN",
    "DEFAULT_TRADER_ID_KGI_CHENGZHONG",
    "DEFAULT_TRADER_ID_YUANTA_SONGJIANG",
    "INITIATION_ACTION",
    "REPEAT_ADD_ACTION",
    "SHARES_PER_LOT",
    "CopytradeSignal",
    "filter_grouped_signals",
    "group_branch_signals_by_date",
    "group_signals_by_date",
    "iter_branch_buy_signals",
    "iter_copytrade_signals",
    "mean_net",
    "snapshot_pairs",
]
