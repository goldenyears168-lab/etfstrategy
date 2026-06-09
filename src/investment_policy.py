"""IPS：投資政策靜態檔（E0 · Pre-Broker）。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - 早盤報 fallback
    yaml = None  # type: ignore[assignment]

from stock_db import DATA_DIR, PROJECT_ROOT

DEFAULT_POLICY_PATH = DATA_DIR / "investment_policy.yaml"
EXAMPLE_POLICY_PATH = PROJECT_ROOT / "config" / "investment_policy.example.yaml"

DEFAULTS: dict = {
    "version": "ips-v1",
    "capital_ntd": 100_000.0,
    "max_single_weight_pct": 40.0,
    "max_theme_weight_pct": 50.0,
    "max_daily_positions": 5,
    "daily_weight_mode": "equal",
    "equal_position_weight_pct": 20.0,
    "exclude_entry_signals": ["暫不進場", "乖離過大"],
    "exclude_pm_buckets": ["回避"],
    "exclude_chip_tags": ["外資賣超背離", "籌碼背離", "同步賣超"],
    "tsm_adr_block_new_tech_pct": -2.0,
    "require_evening_sync_ok": True,
    "min_risk_reward": 1.5,
    "breakout_buffer_pct": 0.5,
    "pull_buffer_pct": 1.0,
    "wait_buffer_pct": 0.75,
    "stop_buffer_pct": 2.0,
    "breakout_risk_pct": 3.0,
    "open_execution_policy": "compare_at_open",
    "open_price_source": "auction_0900",
    "favorable_open_action": "market_rod",
    "unfavorable_open_action": "limit_rod",
    "score_patience_bands": [
        {"min_score": 75, "discount_pct": 2.0},
        {"min_score": 70, "discount_pct": 3.0},
        {"min_score": 65, "discount_pct": 3.5},
        {"min_score": 60, "discount_pct": 3.5},
        {"min_score": 0, "discount_pct": 4.5},
    ],
    "entry_patience_multiplier": {
        "突破": 1.0,
        "拉回": 0.9,
        "觀望": 1.05,
    },
    "min_limit_discount_pct": 2.0,
    "max_limit_discount_pct": 6.0,
    "vol_strong_near_high_pct": -5.0,
    "vol_strong_near_high_adj": -0.5,
    "vol_far_from_high_pct": -8.0,
    "vol_far_from_high_adj": 0.5,
    "vol_beta_high": 1.3,
    "vol_beta_high_adj": 0.5,
    "vol_beta_low": 1.1,
    "vol_beta_low_adj": -0.25,
    "vol_gap_wide_pct": 0.5,
    "vol_gap_wide_adj": 0.5,
    "vol_tsm_adr_weak_adj": 0.5,
    "vol_swing_high_pct": 3.5,
    "vol_swing_high_adj": 0.35,
    "vol_swing_low_pct": 2.0,
    "vol_swing_low_adj": -0.2,
    "max_open_gap_pct": 3.0,
    "gap_block_new_entry_pct": 0.0,
    "gap_size_multiplier": 0.5,
    "adr_weak_size_scale": 1.0,
    "allow_intraday_overwrite_approved": False,
    "sizing_mode": "equal_cap",
    "risk_budget_pct_per_trade": 1.0,
    "use_stop_distance_for_qty": False,
}


@dataclass(frozen=True)
class ScorePatienceBand:
    min_score: float
    discount_pct: float


@dataclass(frozen=True)
class InvestmentPolicy:
    version: str
    capital_ntd: float
    max_single_weight_pct: float
    max_theme_weight_pct: float
    max_daily_positions: int
    daily_weight_mode: str
    equal_position_weight_pct: float
    exclude_entry_signals: frozenset[str]
    exclude_pm_buckets: frozenset[str]
    exclude_chip_tags: frozenset[str]
    tsm_adr_block_new_tech_pct: float
    require_evening_sync_ok: bool
    min_risk_reward: float
    breakout_buffer_pct: float
    pull_buffer_pct: float
    wait_buffer_pct: float
    stop_buffer_pct: float
    breakout_risk_pct: float
    open_execution_policy: str
    open_price_source: str
    favorable_open_action: str
    unfavorable_open_action: str
    score_patience_bands: tuple[ScorePatienceBand, ...]
    entry_patience_multiplier: dict[str, float]
    min_limit_discount_pct: float
    max_limit_discount_pct: float
    vol_strong_near_high_pct: float
    vol_strong_near_high_adj: float
    vol_far_from_high_pct: float
    vol_far_from_high_adj: float
    vol_beta_high: float
    vol_beta_high_adj: float
    vol_beta_low: float
    vol_beta_low_adj: float
    vol_gap_wide_pct: float
    vol_gap_wide_adj: float
    vol_tsm_adr_weak_adj: float
    vol_swing_high_pct: float
    vol_swing_high_adj: float
    vol_swing_low_pct: float
    vol_swing_low_adj: float
    max_open_gap_pct: float
    gap_block_new_entry_pct: float
    gap_size_multiplier: float
    adr_weak_size_scale: float
    allow_intraday_overwrite_approved: bool
    sizing_mode: str
    risk_budget_pct_per_trade: float
    use_stop_distance_for_qty: bool
    source_path: str

    @classmethod
    def from_dict(cls, raw: dict, *, source_path: str = "") -> InvestmentPolicy:
        base = raw or {}
        gap_raw = base.get("gap_controls") or {}
        if isinstance(gap_raw, dict):
            base = {**base, **gap_raw}
        eval_raw = base.get("evaluation_defaults") or {}
        if isinstance(eval_raw, dict):
            base = {**base, **eval_raw}
        sizing_raw = base.get("sizing") or {}
        if isinstance(sizing_raw, dict):
            if "mode" in sizing_raw and "sizing_mode" not in base:
                base["sizing_mode"] = sizing_raw["mode"]
            base = {**base, **sizing_raw}
        merged = {**DEFAULTS, **base}
        bands_raw = merged.get("score_patience_bands") or DEFAULTS["score_patience_bands"]
        bands = tuple(
            ScorePatienceBand(
                min_score=float(b["min_score"]),
                discount_pct=float(b["discount_pct"]),
            )
            for b in bands_raw
        )
        bands = tuple(sorted(bands, key=lambda b: b.min_score, reverse=True))
        mult_raw = merged.get("entry_patience_multiplier") or DEFAULTS["entry_patience_multiplier"]
        return cls(
            version=str(merged["version"]),
            capital_ntd=float(merged["capital_ntd"]),
            max_single_weight_pct=float(merged["max_single_weight_pct"]),
            max_theme_weight_pct=float(merged["max_theme_weight_pct"]),
            max_daily_positions=max(1, int(merged["max_daily_positions"])),
            daily_weight_mode=str(merged["daily_weight_mode"]),
            equal_position_weight_pct=float(merged["equal_position_weight_pct"]),
            exclude_entry_signals=frozenset(merged.get("exclude_entry_signals") or []),
            exclude_pm_buckets=frozenset(merged.get("exclude_pm_buckets") or []),
            exclude_chip_tags=frozenset(merged.get("exclude_chip_tags") or []),
            tsm_adr_block_new_tech_pct=float(merged["tsm_adr_block_new_tech_pct"]),
            require_evening_sync_ok=bool(merged["require_evening_sync_ok"]),
            min_risk_reward=float(merged["min_risk_reward"]),
            breakout_buffer_pct=float(merged["breakout_buffer_pct"]),
            pull_buffer_pct=float(merged["pull_buffer_pct"]),
            wait_buffer_pct=float(merged["wait_buffer_pct"]),
            stop_buffer_pct=float(merged["stop_buffer_pct"]),
            breakout_risk_pct=float(merged["breakout_risk_pct"]),
            open_execution_policy=str(merged["open_execution_policy"]),
            open_price_source=str(merged["open_price_source"]),
            favorable_open_action=str(merged["favorable_open_action"]),
            unfavorable_open_action=str(merged["unfavorable_open_action"]),
            score_patience_bands=bands,
            entry_patience_multiplier={str(k): float(v) for k, v in mult_raw.items()},
            min_limit_discount_pct=float(merged["min_limit_discount_pct"]),
            max_limit_discount_pct=float(merged["max_limit_discount_pct"]),
            vol_strong_near_high_pct=float(merged["vol_strong_near_high_pct"]),
            vol_strong_near_high_adj=float(merged["vol_strong_near_high_adj"]),
            vol_far_from_high_pct=float(merged["vol_far_from_high_pct"]),
            vol_far_from_high_adj=float(merged["vol_far_from_high_adj"]),
            vol_beta_high=float(merged["vol_beta_high"]),
            vol_beta_high_adj=float(merged["vol_beta_high_adj"]),
            vol_beta_low=float(merged["vol_beta_low"]),
            vol_beta_low_adj=float(merged["vol_beta_low_adj"]),
            vol_gap_wide_pct=float(merged["vol_gap_wide_pct"]),
            vol_gap_wide_adj=float(merged["vol_gap_wide_adj"]),
            vol_tsm_adr_weak_adj=float(merged["vol_tsm_adr_weak_adj"]),
            vol_swing_high_pct=float(merged["vol_swing_high_pct"]),
            vol_swing_high_adj=float(merged["vol_swing_high_adj"]),
            vol_swing_low_pct=float(merged["vol_swing_low_pct"]),
            vol_swing_low_adj=float(merged["vol_swing_low_adj"]),
            max_open_gap_pct=float(merged["max_open_gap_pct"]),
            gap_block_new_entry_pct=float(merged["gap_block_new_entry_pct"]),
            gap_size_multiplier=float(merged["gap_size_multiplier"]),
            adr_weak_size_scale=float(merged["adr_weak_size_scale"]),
            allow_intraday_overwrite_approved=bool(
                merged["allow_intraday_overwrite_approved"]
            ),
            sizing_mode=str(merged.get("sizing_mode") or merged.get("mode") or "equal_cap"),
            risk_budget_pct_per_trade=float(merged["risk_budget_pct_per_trade"]),
            use_stop_distance_for_qty=bool(merged["use_stop_distance_for_qty"]),
            source_path=source_path,
        )


def uses_risk_budget_sizing(ips: InvestmentPolicy) -> bool:
    return ips.sizing_mode == "risk_budget" or ips.use_stop_distance_for_qty


def compute_risk_budget_qty(
    *,
    suggested_ntd: float,
    ref_price: float,
    stop_price: float | None,
    size_scale: float,
    ips: InvestmentPolicy,
) -> int:
    """Phase 3：min(qty_risk, qty_cap)；equal_cap 時僅 qty_cap。"""
    if ref_price <= 0:
        return 0
    qty_cap = int(suggested_ntd * size_scale // ref_price)
    if not uses_risk_budget_sizing(ips):
        return qty_cap
    if stop_price is None or ref_price <= stop_price:
        return 0
    per_share_risk = ref_price - stop_price
    if per_share_risk <= 0:
        return 0
    risk_ntd = ips.capital_ntd * ips.risk_budget_pct_per_trade / 100.0
    qty_risk = int(risk_ntd // per_share_risk)
    return min(qty_risk, qty_cap)


def score_patience_discount_pct(
    investment_score: float,
    entry_signal: str,
    ips: InvestmentPolicy,
) -> float:
    """評分越高 → 折扣越小（越願意接近 anchor 掛單）。"""
    discount = ips.score_patience_bands[-1].discount_pct if ips.score_patience_bands else 10.0
    for band in ips.score_patience_bands:
        if investment_score >= band.min_score:
            discount = band.discount_pct
            break
    mult = ips.entry_patience_multiplier.get(entry_signal, 1.0)
    return round(discount * mult, 2)


def compute_limit_discount_pct(
    *,
    investment_score: float,
    entry_signal: str,
    ips: InvestmentPolicy,
    dist_from_52w_high_pct: float | None = None,
    beta: float | None = None,
    tx_gap_pct: float | None = None,
    tsm_adr_pct: float | None = None,
    atr14_pct: float | None = None,
    avg_range_pct_14d: float | None = None,
    realized_vol_pct_14d: float | None = None,
) -> tuple[float, str]:
    """A+B 基礎折扣 + 波動／環境微調，封頂 min～max。"""
    base = score_patience_discount_pct(investment_score, entry_signal, ips)
    vol_adj = 0.0
    notes: list[str] = [f"評分基礎 {base:.2f}%"]

    if dist_from_52w_high_pct is not None:
        if dist_from_52w_high_pct >= ips.vol_strong_near_high_pct:
            vol_adj += ips.vol_strong_near_high_adj
            notes.append(f"近52週高 {dist_from_52w_high_pct:+.1f}% {ips.vol_strong_near_high_adj:+.2f}%")
        elif dist_from_52w_high_pct <= ips.vol_far_from_high_pct:
            vol_adj += ips.vol_far_from_high_adj
            notes.append(f"離52週高 {dist_from_52w_high_pct:+.1f}% {ips.vol_far_from_high_adj:+.2f}%")

    if beta is not None:
        if beta >= ips.vol_beta_high:
            vol_adj += ips.vol_beta_high_adj
            notes.append(f"beta {beta:.2f} {ips.vol_beta_high_adj:+.2f}%")
        elif beta <= ips.vol_beta_low:
            vol_adj += ips.vol_beta_low_adj
            notes.append(f"beta {beta:.2f} {ips.vol_beta_low_adj:+.2f}%")

    if tx_gap_pct is not None and abs(tx_gap_pct) >= ips.vol_gap_wide_pct:
        vol_adj += ips.vol_gap_wide_adj
        notes.append(f"台指gap {tx_gap_pct:+.2f}% {ips.vol_gap_wide_adj:+.2f}%")

    if (
        tsm_adr_pct is not None
        and tsm_adr_pct <= ips.tsm_adr_block_new_tech_pct
    ):
        vol_adj += ips.vol_tsm_adr_weak_adj
        notes.append(f"TSM ADR {tsm_adr_pct:+.2f}% {ips.vol_tsm_adr_weak_adj:+.2f}%")

    swing_parts: list[str] = []
    swing_vals: list[float] = []
    if atr14_pct is not None:
        swing_parts.append(f"ATR14 {atr14_pct:.2f}%")
        swing_vals.append(atr14_pct)
    if avg_range_pct_14d is not None:
        swing_parts.append(f"振幅 {avg_range_pct_14d:.2f}%")
        swing_vals.append(avg_range_pct_14d)
    if realized_vol_pct_14d is not None:
        swing_parts.append(f"RV {realized_vol_pct_14d:.2f}%")
        swing_vals.append(realized_vol_pct_14d)
    if swing_vals:
        swing = max(swing_vals)
        swing_adj = 0.0
        if swing >= ips.vol_swing_high_pct:
            swing_adj = ips.vol_swing_high_adj
        elif swing <= ips.vol_swing_low_pct:
            swing_adj = ips.vol_swing_low_adj
        if swing_adj != 0.0:
            vol_adj += swing_adj
        notes.append(
            f"{' · '.join(swing_parts)} → swing {swing:.2f}% {swing_adj:+.2f}%"
        )

    total = round(base + vol_adj, 2)
    total = max(ips.min_limit_discount_pct, min(ips.max_limit_discount_pct, total))
    notes.append(f"合計 {total:.2f}%")
    return total, " · ".join(notes)


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
