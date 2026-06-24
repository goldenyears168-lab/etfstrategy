"""supabase_health_check · report formatting and check aggregation."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from supabase_health_check import (
    HealthCheck,
    format_report,
    overall_ok,
    run_health_checks,
)


class TestSupabaseHealthCheck(unittest.TestCase):
    def test_overall_ok_fails_on_fail_level_only(self) -> None:
        checks = [
            HealthCheck("a", True, "ok", "fine"),
            HealthCheck("b", False, "warn", "stale flag"),
            HealthCheck("c", True, "ok", "fine"),
        ]
        self.assertTrue(overall_ok(checks))
        checks.append(HealthCheck("d", False, "fail", "missing brief"))
        self.assertFalse(overall_ok(checks))

    def test_format_report_shows_fail_summary(self) -> None:
        report = format_report(
            "2026-06-20",
            [
                HealthCheck("daily_briefs:1630", False, "fail", "缺 regime_daily"),
            ],
        )
        self.assertIn("trade_date=2026-06-20", report)
        self.assertIn("FAIL", report)
        self.assertIn("regime_daily", report)

    @patch("supabase_health_check._get_rows")
    def test_check_briefs_intraday_uses_session_date(self, mock_get: object) -> None:
        from supabase_health_check import _check_briefs

        def fake_get(table: str, *, params: dict[str, str]) -> list[dict[str, str]]:
            if params.get("brief_type") == "eq.rrg_mono_intraday":
                self.assertEqual(params.get("snapshot_json->>session_date"), "eq.2026-06-22")
                return [{"brief_type": "rrg_mono_intraday", "synced_at": "2026-06-22T16:00:00"}]
            return [
                {"brief_type": "vcp_funnel_specs", "synced_at": "2026-06-22T16:00:00"},
                {"brief_type": "vcp_pivot_gate", "synced_at": "2026-06-22T16:00:00"},
                {"brief_type": "vcp_coil_close", "synced_at": "2026-06-22T16:00:00"},
            ]

        mock_get.side_effect = fake_get
        check = _check_briefs("2026-06-22", slot="1300")
        self.assertEqual(check.level, "ok")
        self.assertIn("session_date=2026-06-22", check.detail)

    @patch("supabase_health_check._check_signal_hits", return_value=None)
    @patch("supabase_health_check._check_strategy_registry")
    @patch("supabase_health_check._check_highlight_alert")
    @patch("supabase_health_check._check_highlight_rows")
    @patch("supabase_health_check._check_briefs")
    @patch("supabase_health_check.supabase_configured", return_value=False)
    def test_run_health_checks_stops_when_unconfigured(
        self,
        _configured: object,
        _briefs: object,
        _rows: object,
        _alert: object,
        _registry: object,
        _signal: object,
    ) -> None:
        checks = run_health_checks("2026-06-20", check_1300=False)
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0].name, "supabase:config")
        self.assertEqual(checks[0].level, "fail")

    @patch("supabase_health_check._check_signal_hits", return_value=None)
    @patch(
        "supabase_health_check._check_strategy_registry",
        return_value=HealthCheck("site_content:registry", True, "ok", "5 軌"),
    )
    @patch(
        "supabase_health_check._check_highlight_alert",
        return_value=HealthCheck("daily_highlight_alert", True, "ok", "headline"),
    )
    @patch(
        "supabase_health_check._check_highlight_rows",
        return_value=HealthCheck("stock_daily_highlight", True, "ok", "100 檔"),
    )
    @patch(
        "supabase_health_check._check_briefs",
        return_value=HealthCheck("daily_briefs:1630", True, "ok", "齊全"),
    )
    @patch("supabase_health_check.supabase_configured", return_value=True)
    @patch.dict("os.environ", {"RUN_SUPABASE_RESEARCH_SYNC": "0"}, clear=False)
    def test_run_health_checks_warns_when_sync_off(
        self,
        _configured: object,
        _briefs: object,
        _rows: object,
        _alert: object,
        _registry: object,
        _signal: object,
    ) -> None:
        checks = run_health_checks("2026-06-20", check_1300=False)
        env_checks = [c for c in checks if c.name.startswith("env:")]
        self.assertTrue(any(c.level == "warn" for c in env_checks))
        self.assertTrue(overall_ok(checks))


if __name__ == "__main__":
    unittest.main()
