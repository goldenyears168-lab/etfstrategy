"""Tests for order.fubon_orders enum mapping (no live order)."""

from __future__ import annotations

import unittest

from order.fubon_orders import _map_enum


class TestFubonEnumMapping(unittest.TestCase):
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
