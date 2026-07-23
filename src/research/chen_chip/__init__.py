"""Chen-style chip features + same-day whale alignment (research only).

Vendor logic retained from:
  - vendor/tw-institutional-stocker (Apache-2.0 · aiinpocket)
  - vendor/cftc-cot (MIT · victorKariuki)
See reports/research/chen_chip_sameday/THIRD_PARTY_NOTICES.md.
"""

from research.chen_chip.features import build_chip_feature_frame
from research.chen_chip.signals import apply_chip_rule, default_chen_rule, sweep_grid

__all__ = [
    "build_chip_feature_frame",
    "apply_chip_rule",
    "default_chen_rule",
    "sweep_grid",
]
