"""Tests for order.fubon_orders enum mapping (no live order)."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from order.fubon_orders import _map_enum, order_master_enabled


class TestOrderMasterEnabled(unittest.TestCase):
    def test_unset_defaults_to_disabled(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ORDER_MASTER_ENABLED", None)
            self.assertFalse(order_master_enabled())

    def test_zero_is_disabled(self) -> None:
        with patch.dict(os.environ, {"ORDER_MASTER_ENABLED": "0"}):
            self.assertFalse(order_master_enabled())

    def test_garbage_value_is_disabled(self) -> None:
        with patch.dict(os.environ, {"ORDER_MASTER_ENABLED": "please"}):
            self.assertFalse(order_master_enabled())

    def test_one_is_enabled(self) -> None:
        with patch.dict(os.environ, {"ORDER_MASTER_ENABLED": "1"}):
            self.assertTrue(order_master_enabled())

    def test_true_is_enabled(self) -> None:
        with patch.dict(os.environ, {"ORDER_MASTER_ENABLED": "true"}):
            self.assertTrue(order_master_enabled())


class TestFubonEnumMapping(unittest.TestCase):
    def test_is_open_order_status_zero(self) -> None:
        from types import SimpleNamespace

        from order.fubon_orders import is_open_order

        self.assertTrue(is_open_order(SimpleNamespace(status=0)))
        self.assertTrue(is_open_order(SimpleNamespace(status=10)))
        self.assertFalse(is_open_order(SimpleNamespace(status=50)))

    def test_time_in_force_rod(self) -> None:
        from fubon_neo.constant import TimeInForce

        self.assertIs(_map_enum(TimeInForce, "rod", field="time_in_force"), TimeInForce.ROD)

    def test_market_type_intraday_odd(self) -> None:
        from fubon_neo.constant import MarketType

        self.assertIs(
            _map_enum(MarketType, "intraday_odd", field="market_type"),
            MarketType.IntradayOdd,
        )

    def test_price_type_market(self) -> None:
        from fubon_neo.constant import PriceType

        self.assertIs(_map_enum(PriceType, "market", field="price_type"), PriceType.Market)


if __name__ == "__main__":
    unittest.main()
