"""slot_backtest_summary · schema v2 tests."""

from __future__ import annotations

import unittest

from research.backtest.slot_backtest_summary import (
    NOTIONAL_SSOT,
    SCHEMA_VERSION,
    SlotBacktestConfig,
    build_summary_payload,
    normalize_summary_payload,
    resolve_total_capital_from_raw,
)


class SlotBacktestSummaryTests(unittest.TestCase):
    def test_resolve_total_capital_explicit(self) -> None:
        self.assertEqual(
            resolve_total_capital_from_raw({"n_slots": 3, "total_capital_ntd": 100_000}),
            100_000.0,
        )

    def test_resolve_total_capital_legacy_per_slot(self) -> None:
        self.assertEqual(
            resolve_total_capital_from_raw({"n_slots": 3, "capital_ntd": 10_000}),
            30_000.0,
        )

    def test_resolve_total_capital_legacy_total_semantics(self) -> None:
        self.assertEqual(
            resolve_total_capital_from_raw(
                {"n_slots": 3, "capital_ntd": 60_000, "capital_semantics": "total"}
            ),
            60_000.0,
        )

    def test_from_yaml_dict_uses_total_capital(self) -> None:
        cfg = SlotBacktestConfig.from_yaml_dict(
            {"n_slots": 5, "total_capital_ntd": 50_000, "date_start": "2026-01-01"}
        )
        assert cfg is not None
        self.assertEqual(cfg.total_capital_ntd, 50_000.0)
        self.assertEqual(cfg.capital_per_slot_ntd, 10_000.0)
        self.assertEqual(cfg.capital_ntd, 10_000.0)

    def test_from_yaml_dict_defaults_to_comparison_notional(self) -> None:
        cfg = SlotBacktestConfig.from_yaml_dict(
            {"n_slots": 3, "date_start": "2026-01-01", "date_end": "2026-06-30"}
        )
        assert cfg is not None
        self.assertEqual(cfg.resolved_total_capital_ntd, 100_000.0)

    def test_build_summary_payload_v2(self) -> None:
        cfg = SlotBacktestConfig(
            date_start="2026-01-01",
            date_end="2026-06-30",
            n_slots=3,
        )
        payload = build_summary_payload(
            track_id="rrg-mono-hold7",
            config=cfg,
            summary={"mean_excess_pct": 1.5, "n_periods": 10},
            source_module="rrg_mono_backtest",
        )
        self.assertEqual(payload["schema_version"], SCHEMA_VERSION)
        self.assertEqual(payload["total_capital_ntd"], 100_000.0)
        self.assertEqual(payload["capital_per_slot_ntd"], round(100_000.0 / 3, 2))
        self.assertEqual(payload["notional_ssot"], NOTIONAL_SSOT)
        self.assertIn("ranking_metrics", payload)

    def test_normalize_v1_no_capital_uses_ssot(self) -> None:
        legacy = {"track_id": "x", "n_slots": 3, "summary": {}}
        out = normalize_summary_payload(legacy)
        self.assertEqual(out["total_capital_ntd"], 100_000.0)

    def test_normalize_v1_per_slot_legacy(self) -> None:
        legacy = {
            "track_id": "x",
            "n_slots": 3,
            "capital_ntd": 10_000,
            "summary": {},
        }
        out = normalize_summary_payload(legacy)
        self.assertEqual(out["schema_version"], SCHEMA_VERSION)
        self.assertEqual(out["total_capital_ntd"], 30_000.0)


if __name__ == "__main__":
    unittest.main()
