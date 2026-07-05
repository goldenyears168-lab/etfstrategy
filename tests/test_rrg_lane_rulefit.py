"""Tests for RRG lane RuleFit discovery (P2)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from sklearn.tree import DecisionTreeClassifier

from research.backtest.rrg_lane_rulefit import (
    evaluate_lane_candidate,
    extract_tree_rules,
    mask_from_rule_atoms,
    miss_list_recall,
)


def _mini_panel() -> pd.DataFrame:
    rows = []
    for i in range(200):
        rows.append(
            {
                "sid": "2330" if i % 2 == 0 else "2317",
                "date": f"2020-01-{(i % 28) + 1:02d}",
                "ret_3d": 5.0 if i % 10 == 0 else 0.5,
                "min_ret_3d": -1.0,
                "atom_imp": 1 if i % 3 == 0 else 0,
                "atom_st7p": 1 if i % 10 == 0 else 0,
                "atom_gap1": 1,
                "atom_flat": 1 if i % 10 == 0 else 0,
            }
        )
    return pd.DataFrame(rows)


def test_mask_from_rule_atoms():
    df = _mini_panel()
    m = mask_from_rule_atoms(df, ["imp", "gap1"])
    assert m.sum() == len(df[df["atom_imp"] == 1])


def test_extract_tree_rules_positive_only():
    df = _mini_panel()
    cols = ["atom_imp", "atom_st7p", "atom_gap1", "atom_flat"]
    X = df[cols].to_numpy(dtype=np.int8)
    y = (df["ret_3d"] >= 3.0).to_numpy(dtype=np.int8)
    clf = DecisionTreeClassifier(max_depth=3, min_samples_leaf=5, random_state=0)
    clf.fit(X, y)
    rules = extract_tree_rules(clf, cols, min_leaf_n=5, min_pos_rate=0.2, min_atoms=1)
    assert isinstance(rules, list)
    for r in rules:
        assert len(r["rule_atoms"]) >= 1
        assert all(a in {"imp", "st7p", "gap1", "flat"} for a in r["rule_atoms"])


def test_miss_list_recall():
    df = _mini_panel()
    miss = [{"sid": "2330", "signal_date": "2020-01-01"}]
    df.loc[(df["sid"] == "2330") & (df["date"] == "2020-01-01"), "atom_imp"] = 1
    df.loc[(df["sid"] == "2330") & (df["date"] == "2020-01-01"), "atom_st7p"] = 1
    df.loc[(df["sid"] == "2330") & (df["date"] == "2020-01-01"), "atom_gap1"] = 1
    r = miss_list_recall(df, ["imp", "st7p", "gap1"], miss)
    assert r["n_hit"] == 1


def test_evaluate_lane_candidate_gates():
    df = _mini_panel()
    gates = {"mean_3d_min": 3.0, "win_3d_min": 0.5, "loss5_rate_max": 0.5, "loss8_rate_max": 0.5, "n_min": 5}
    ev = evaluate_lane_candidate(
        df, ["st7p", "gap1"], gates=gates, strict_day_sets=[], lane_defs=[], years=1.0
    )
    assert ev["n"] > 0
    assert "pass_gates" in ev
