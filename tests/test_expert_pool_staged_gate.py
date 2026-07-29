"""Unit tests · expert-pool staged gap/05/25 gate classifiers."""

from __future__ import annotations

import os
from unittest.mock import patch

from order.expert_pool_staged_gate import (
    classify_e05,
    classify_e25,
    classify_open,
    detect_stage,
    gap_pct,
    run_staged_gate,
)


def test_gap_pct() -> None:
    assert abs(gap_pct(717.06, 703.0) - 2.0) < 0.01
    assert abs(gap_pct(731.12, 703.0) - 4.0) < 0.01


def test_classify_open_green_yellow_red() -> None:
    assert classify_open(1.5) == "buy_open"
    assert classify_open(2.5) == "wait_05"
    assert classify_open(3.5) == "wait_25"  # > chase_gc 3%
    assert classify_open(4.5) == "wait_25"


def test_classify_e05_perfect_vs_defer() -> None:
    # gap 2.5 yellow · last flat · perfect
    assert classify_e05(2.5, 100.0, 100.2, 99.8, 100.0) == "buy_05"
    # deep red at 05
    assert classify_e05(2.5, 100.0, 100.0, 97.0, 97.5) == "wait_25"
    # gap too large for perfect
    assert classify_e05(3.5, 100.0, 100.1, 99.9, 100.0) == "wait_25"


def test_classify_e25_skip_fail_and_ret() -> None:
    # fail_break: high ≥1% and last < open
    assert classify_e25(100.0, 101.5, 99.0) == "skip"
    # ret25 ≤ -2%
    assert classify_e25(100.0, 100.5, 97.5) == "skip"
    # stable
    assert classify_e25(100.0, 100.8, 100.2) == "buy_25"


def test_detect_stage_windows() -> None:
    assert detect_stage("09:00") == "open"
    assert detect_stage("09:02") == "open"
    assert detect_stage("09:05") == "e05"
    assert detect_stage("09:25") == "e25"
    assert detect_stage("09:10") is None
    assert detect_stage("10:00") is None


def test_order_master_switch_off_forces_dry_run() -> None:
    env = {
        "ORDER_EP_STAGED_GATE_ENABLED": "1",
        "ORDER_EP_STAGED_GATE_DRY_RUN": "0",
    }
    with patch.dict(os.environ, env, clear=False):
        os.environ.pop("ORDER_MASTER_ENABLED", None)
        # 09:10 is outside all stage windows -> returns before touching Fubon,
        # but dry_run/enabled are already resolved by then.
        out = run_staged_gate(session_date="2026-07-15", now_hm="09:10")
        assert out["dry_run"] is True


def test_order_master_switch_on_respects_individual_flag() -> None:
    env = {
        "ORDER_EP_STAGED_GATE_ENABLED": "1",
        "ORDER_EP_STAGED_GATE_DRY_RUN": "0",
        "ORDER_MASTER_ENABLED": "1",
    }
    with patch.dict(os.environ, env, clear=False):
        out = run_staged_gate(session_date="2026-07-15", now_hm="09:10")
        assert out["dry_run"] is False


def test_order_master_switch_does_not_override_explicit_dry_run() -> None:
    env = {
        "ORDER_EP_STAGED_GATE_ENABLED": "1",
        "ORDER_EP_STAGED_GATE_DRY_RUN": "1",
    }
    with patch.dict(os.environ, env, clear=False):
        os.environ.pop("ORDER_MASTER_ENABLED", None)
        out = run_staged_gate(session_date="2026-07-15", now_hm="09:10", dry_run=False)
        assert out["dry_run"] is False
