"""open_execution_policy：開盤比對。"""

from __future__ import annotations

import unittest

from investment_policy import InvestmentPolicy
from open_execution_policy import ORDER_LIMIT_ROD, ORDER_MARKET_ROD, resolve_open_execution


class TestOpenExecutionPolicy(unittest.TestCase):
    def test_favorable_open_market(self) -> None:
        ips = InvestmentPolicy.from_dict({})
        d = resolve_open_execution(ref_price=1000.0, open_price=990.0, ips=ips)
        self.assertEqual(d.order_type_effective, ORDER_MARKET_ROD)

    def test_unfavorable_open_limit(self) -> None:
        ips = InvestmentPolicy.from_dict({})
        d = resolve_open_execution(ref_price=1000.0, open_price=1010.0, ips=ips)
        self.assertEqual(d.order_type_effective, ORDER_LIMIT_ROD)


if __name__ == "__main__":
    unittest.main()
