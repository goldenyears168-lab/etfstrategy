"""研究層 IPS：capital / pm_watchlist 篩選（非 E0 執行）。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore[assignment]

from stock_db import DATA_DIR, PROJECT_ROOT

DEFAULT_POLICY_PATH = DATA_DIR / "investment_policy.yaml"
EXAMPLE_POLICY_PATH = PROJECT_ROOT / "config" / "investment_policy.example.yaml"

DEFAULTS: dict = {
    "version": "ips-v2-research",
    "capital_ntd": 100_000.0,
    "max_daily_positions": 5,
    "equal_position_weight_pct": 40.0,
    "exclude_pm_buckets": ["回避"],
}


@dataclass(frozen=True)
class InvestmentPolicy:
    version: str
    capital_ntd: float
    max_daily_positions: int
    equal_position_weight_pct: float
    exclude_pm_buckets: frozenset[str]
    source_path: str = ""

    @classmethod
    def from_dict(cls, raw: dict, *, source_path: str = "") -> InvestmentPolicy:
        merged = {**DEFAULTS, **(raw or {})}
        return cls(
            version=str(merged["version"]),
            capital_ntd=float(merged["capital_ntd"]),
            max_daily_positions=int(merged["max_daily_positions"]),
            equal_position_weight_pct=float(merged["equal_position_weight_pct"]),
            exclude_pm_buckets=frozenset(str(x) for x in (merged.get("exclude_pm_buckets") or [])),
            source_path=source_path,
        )


def _load_yaml_dict(path: Path) -> dict:
    if yaml is None:
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def load_investment_policy(path: Path | None = None) -> InvestmentPolicy:
    p = path or DEFAULT_POLICY_PATH
    if yaml is None:
        return InvestmentPolicy.from_dict(
            DEFAULTS, source_path="built-in defaults (PyYAML 未安裝)"
        )
    if p.exists():
        return InvestmentPolicy.from_dict(_load_yaml_dict(p), source_path=str(p))
    if EXAMPLE_POLICY_PATH.exists():
        return InvestmentPolicy.from_dict(
            _load_yaml_dict(EXAMPLE_POLICY_PATH),
            source_path=f"{EXAMPLE_POLICY_PATH} (fallback)",
        )
    return InvestmentPolicy.from_dict(DEFAULTS, source_path="built-in defaults")
