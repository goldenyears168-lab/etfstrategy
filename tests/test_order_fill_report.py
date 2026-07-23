"""Plain-language order fill email helpers."""

from __future__ import annotations

import unittest

from order.order_fill_report import (
    format_fill_body,
    format_fill_subject,
    row_should_notify,
)


class TestOrderFillReport(unittest.TestCase):
    def test_filled_row_subject_and_body(self) -> None:
        row = {
            "symbol": "2377",
            "stock_name": "微星",
            "side": "buy",
            "action_kind": "entry",
            "lifecycle_status": "filled",
            "status": "submitted",
            "quantity_shares": 134,
            "filled_qty": 134,
            "avg_fill_price": 149.5,
            "ask_price": 148.5,
            "notional_twd": 20033,
            "order_no": "oA822",
            "client_intent_id": "rrg-mono-swap-accel_20260713_0930_2377_entry_buy",
            "session_date": "2026-07-13",
            "poll_minute": "09:30",
            "note": "avg_accel @ 09:30 confirm=1",
        }
        self.assertTrue(row_should_notify(row))
        subject = format_fill_subject([row], sleeve="C18acc")
        self.assertIn("[ORDER|FILLED]", subject)
        self.assertIn("2377", subject)
        body = format_fill_body([row], sleeve="C18acc", include_holdings=False)
        self.assertIn("已成交", body)
        self.assertIn("微星", body)
        self.assertIn("134", body)
        self.assertIn("149.5", body)
        self.assertIn("oA822", body)
        self.assertNotIn("dry-run intent", body)

    def test_dry_run_not_notified(self) -> None:
        row = {"symbol": "2377", "status": "dry_run", "side": "buy"}
        self.assertFalse(row_should_notify(row))

    def test_holdings_section_fields(self) -> None:
        from order.order_fill_report import format_holdings_status_section

        text = format_holdings_status_section(
            [
                {
                    "stock_id": "2377",
                    "stock_name": "微星",
                    "shares": 134,
                    "current_price": 146.5,
                    "prev_close": 148.0,
                    "daily_pct": -1.01,
                    "cost_price": 149.0,
                    "cost_basis": 19966.0,
                    "market_value": 19631.0,
                    "unrealized_pnl": -383,
                    "pnl_pct": -1.92,
                    "price_source": "fubon",
                }
            ],
            cash_available=2_000_000,
            total_market_value=19631.0,
            total_unrealized_pnl=-383,
        )
        self.assertIn("2377", text)
        self.assertIn("微星", text)
        self.assertIn("134", text)
        self.assertIn("今日漲跌", text)
        self.assertIn("成本均價", text)
        self.assertIn("投資成本", text)
        self.assertIn("市值", text)
        self.assertIn("損益", text)
        self.assertIn("損益率", text)

    def test_pool_digest_mixed_blocker(self) -> None:
        from order.order_fill_report import format_c18acc_pool_digest

        subject, body = format_c18acc_pool_digest(
            session_date="2026-07-13",
            poll_minute="13:00",
            pool_as_of="2026-07-13",
            pool=[
                {"stock_id": "5876", "stock_name": "上海商銀", "seg_last": 0.5, "disp": 1.0},
                {"stock_id": "3706", "stock_name": "神達", "seg_last": 0.7, "disp": 1.2},
            ],
            slots=[
                {
                    "slot": 0,
                    "stock_id": "2377",
                    "stock_name": "微星",
                    "entry_date": "2026-07-13",
                    "entry_px": 148.5,
                }
            ],
            lead="3706 神達",
            lead_drift="領先標的已換人",
            entry_gate="有標的正在靠近買點",
            blocker="3706：spread_class=mixed · avoid gate",
            spread_gate_snapshot={"3706": "mixed", "5876": "mixed", "2377": "aligned"},
            entry_confirm={"3706": 2, "5876": 2},
            actions_n=0,
        )
        self.assertIn("POOL", subject)
        self.assertIn("13:00", subject)
        self.assertIn("3706", body)
        self.assertIn("mixed", body)
        self.assertIn("avoid mixed", body)
        self.assertIn("神達", body)


if __name__ == "__main__":
    unittest.main()
