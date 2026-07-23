"""Chip fire rules (Chen baseline + sweep grid)."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from itertools import product

import pandas as pd


@dataclass(frozen=True)
class ChipRule:
    name: str
    foreign_streak_min: int = 1
    trust_streak_min: int = 0
    three_streak_min: int = 0
    foreign_pctl20_min: float = 0.0  # 0 = off
    require_foreign_positive: bool = True
    require_trust_positive: bool = False
    max_margin_surge: bool = True  # reject if margin_surge
    tx_pos_streak_min: int = 0  # literal net>0 streak (often dead on TX)
    tx_rising_streak_min: int = 0  # day-over-day net OI improvement
    require_branch_consensus: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def default_chen_rule() -> ChipRule:
    """Chen-ish baseline: foreign streak + no margin surge + TX OI rising."""
    return ChipRule(
        name="chen_baseline",
        foreign_streak_min=2,
        trust_streak_min=0,
        three_streak_min=0,
        foreign_pctl20_min=0.5,
        require_foreign_positive=True,
        require_trust_positive=False,
        max_margin_surge=True,
        tx_pos_streak_min=0,
        tx_rising_streak_min=1,
        require_branch_consensus=False,
    )


def apply_chip_rule(df: pd.DataFrame, rule: ChipRule) -> pd.Series:
    m = pd.Series(True, index=df.index)
    if rule.require_foreign_positive:
        m &= df["foreign_net"] > 0
    if rule.require_trust_positive:
        m &= df["trust_net"] > 0
    if rule.foreign_streak_min > 0:
        m &= df["foreign_streak"] >= rule.foreign_streak_min
    if rule.trust_streak_min > 0:
        m &= df["trust_streak"] >= rule.trust_streak_min
    if rule.three_streak_min > 0:
        m &= df["three_streak"] >= rule.three_streak_min
    if rule.foreign_pctl20_min > 0:
        m &= df["foreign_pctl20"].fillna(0) >= rule.foreign_pctl20_min
    if rule.max_margin_surge:
        m &= ~df["margin_surge"].fillna(False)
    if rule.tx_pos_streak_min > 0:
        m &= df["tx_foreign_net_pos_streak"].fillna(0) >= rule.tx_pos_streak_min
    if rule.tx_rising_streak_min > 0:
        col = "tx_foreign_net_rising_streak"
        if col in df.columns:
            m &= df[col].fillna(0) >= rule.tx_rising_streak_min
        else:
            m &= False
    return m.fillna(False)


def sweep_grid() -> list[ChipRule]:
    rules: list[ChipRule] = [default_chen_rule()]
    for fs, ts, pctl, txr, surge in product(
        (1, 2, 3),
        (0, 1, 2),
        (0.0, 0.5, 0.7),
        (0, 1, 2),
        (True, False),
    ):
        if fs == 1 and ts == 0 and pctl == 0.0 and txr == 0 and not surge:
            continue
        name = f"fs{fs}_ts{ts}_p{pctl:.1f}_txr{txr}_ms{int(surge)}"
        rules.append(
            ChipRule(
                name=name,
                foreign_streak_min=fs,
                trust_streak_min=ts,
                foreign_pctl20_min=pctl,
                require_foreign_positive=True,
                require_trust_positive=ts > 0,
                max_margin_surge=surge,
                tx_pos_streak_min=0,
                tx_rising_streak_min=txr,
            )
        )
    seen: set[str] = set()
    out: list[ChipRule] = []
    for r in rules:
        if r.name in seen:
            continue
        seen.add(r.name)
        out.append(r)
    return out
