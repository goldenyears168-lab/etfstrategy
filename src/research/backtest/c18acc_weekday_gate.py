"""C18acc · execution weekday gate (entry / swap / exit defer · PIT-safe)."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date as date_cls, timedelta
from typing import Any, Literal

EntryMode = Literal["baseline", "E0", "E1", "E2", "E3", "E4", "E5"]
SwapMode = Literal["baseline", "S0", "S1", "S2", "S3"]
ExitMode = Literal["baseline", "X0", "X1", "X2", "X3"]
MaxHoldExitAlign = Literal["calendar", "friday"]
FridaySwapPool = Literal["baseline", "union_mono", "mono_tier2"]


@dataclass
class WeekdayGateConfig:
    variant_id: str = "W0"
    entry_mode: EntryMode = "baseline"
    swap_mode: SwapMode = "baseline"
    exit_mode: ExitMode = "baseline"
    signal_expire_trade_days: int = 2
    # 預設 max_hold（例 10 日）到期後，僅在週五 poll 出場；swap / S2 force 不受限
    max_hold_exit_align: MaxHoldExitAlign = "calendar"
    # 週五 swap challenger margin（例 0.03 取代預設 0.05）
    friday_swap_margin: float | None = None
    # 週五擴大 challenger 池（fresh ∪ mono_tier2 · 不須 accel>0）
    friday_swap_pool: FridaySwapPool = "baseline"
    # 週五允許更多 swap（例 2 · 覆蓋 config.max_swaps_per_day）
    friday_max_swaps: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def is_weekday_gate_active(gate: WeekdayGateConfig | None) -> bool:
    if gate is None:
        return False
    return not (
        gate.entry_mode in ("baseline", "E0")
        and gate.swap_mode in ("baseline", "S0")
        and gate.exit_mode in ("baseline", "X0")
        and gate.max_hold_exit_align == "calendar"
        and gate.friday_swap_margin is None
        and gate.friday_swap_pool == "baseline"
        and gate.friday_max_swaps is None
    )


def _iso_weekday(iso: str) -> int:
    return date_cls.fromisoformat(iso).weekday()


def _align_to_trade_date(full_dates: list[str], target_iso: str) -> str | None:
    for d in full_dates:
        if d >= target_iso:
            return d
    return None


def _next_trade_after(full_dates: list[str], after: str) -> str | None:
    for d in full_dates:
        if d > after:
            return d
    return None


def _this_week_friday_iso(signal_iso: str) -> str:
    d = date_cls.fromisoformat(signal_iso)
    return (d + timedelta(days=(4 - d.weekday()))).isoformat()


def _next_week_friday_iso(signal_iso: str) -> str:
    d = date_cls.fromisoformat(signal_iso)
    wd = d.weekday()
    if wd == 4:
        return (d + timedelta(days=7)).isoformat()
    this_fri = d + timedelta(days=(4 - wd))
    return (this_fri + timedelta(days=7)).isoformat()


def entry_target_date(
    gate: WeekdayGateConfig,
    signal_date: str,
    full_dates: list[str],
) -> str | None:
    """First allowed entry trade date for a signal on signal_date."""
    mode = gate.entry_mode
    if mode in ("baseline", "E0"):
        return signal_date

    wd = _iso_weekday(signal_date)
    if mode == "E1":
        if wd == 4:
            return signal_date
        return _align_to_trade_date(full_dates, _this_week_friday_iso(signal_date))
    if mode == "E2":
        return _align_to_trade_date(full_dates, _next_week_friday_iso(signal_date))
    if mode == "E3":
        if wd == 0:
            return _next_trade_after(full_dates, signal_date)
        return signal_date
    if mode == "E4":
        if wd == 4:
            mon = date_cls.fromisoformat(signal_date) + timedelta(days=3)
            return _align_to_trade_date(full_dates, mon.isoformat())
        return signal_date
    if mode == "E5":
        if wd <= 1:
            wed = date_cls.fromisoformat(signal_date) + timedelta(days=(2 - wd))
            return _align_to_trade_date(full_dates, wed.isoformat())
        return signal_date
    return signal_date


def entry_allowed_today(gate: WeekdayGateConfig, signal_date: str, as_of: str) -> bool:
    target = entry_target_date(gate, signal_date, [signal_date, as_of])
    return target == as_of


def swap_allowed_today(gate: WeekdayGateConfig, as_of: str) -> bool:
    mode = gate.swap_mode
    if mode in ("baseline", "S0"):
        return True
    wd = _iso_weekday(as_of)
    if mode in ("S1", "S2"):
        return wd == 4
    if mode == "S3":
        return wd != 0
    return True


def trading_days_between(full_dates: list[str], start: str, end: str) -> int:
    if start > end:
        return 0
    return sum(1 for d in full_dates if start < d <= end)


def signal_expired(
    gate: WeekdayGateConfig,
    signal_date: str,
    as_of: str,
    full_dates: list[str],
) -> bool:
    lag = trading_days_between(full_dates, signal_date, as_of)
    return lag > gate.signal_expire_trade_days


def should_defer_max_hold_default_to_friday(gate: WeekdayGateConfig, as_of: str) -> bool:
    """H1 · max_hold 預設到期日對齊週五（非額外 overlay · 不含 force/swap）。"""
    if gate.max_hold_exit_align != "friday":
        return False
    return _iso_weekday(as_of) != 4


def swap_margin_for_day(
    gate: WeekdayGateConfig | None,
    default_margin: float,
    as_of: str,
) -> float:
    if gate is not None and gate.friday_swap_margin is not None and _iso_weekday(as_of) == 4:
        return float(gate.friday_swap_margin)
    return default_margin


def friday_swap_pool_override(
    gate: WeekdayGateConfig | None,
    default_pool: str,
    as_of: str,
) -> str:
    """週五 swap pool override · union_mono = fresh ∪ mono_tier2（無 accel 門檻）。"""
    if gate is None or gate.friday_swap_pool == "baseline" or _iso_weekday(as_of) != 4:
        return default_pool
    if gate.friday_swap_pool == "mono_tier2":
        return "mono_tier2"
    if gate.friday_swap_pool == "union_mono":
        return "fresh_union_mono"
    return default_pool


def max_swaps_for_day(
    gate: WeekdayGateConfig | None,
    default_max_swaps: int,
    as_of: str,
) -> int:
    if gate is not None and gate.friday_max_swaps is not None and _iso_weekday(as_of) == 4:
        return max(0, int(gate.friday_max_swaps))
    return default_max_swaps


def should_defer_exit_to_friday(
    gate: WeekdayGateConfig,
    as_of: str,
    full_dates: list[str],
) -> bool:
    """X1 · max_hold / quad_force within 2 trade days of Friday → defer."""
    if gate.exit_mode != "X1":
        return False
    wd = _iso_weekday(as_of)
    if wd == 4:
        return False
    fri_iso = _align_to_trade_date(full_dates, _this_week_friday_iso(as_of))
    if fri_iso is None or fri_iso <= as_of:
        return False
    days = trading_days_between(full_dates, as_of, fri_iso)
    return 0 < days <= 2


def should_anti_weekend_force_exit(
    gate: WeekdayGateConfig,
    as_of: str,
    *,
    hold_days: int,
    eff_min_hold: int,
) -> bool:
    """X3 · Friday poll force flat when not min_hold locked."""
    if gate.exit_mode != "X3":
        return False
    if _iso_weekday(as_of) != 4:
        return False
    return hold_days >= eff_min_hold


def week_friday_key(as_of: str) -> str:
    return _this_week_friday_iso(as_of)


def pending_row_to_scan_row(rec: dict[str, Any]) -> Any:
    from rrg_mono_daily_brief import ScanRow

    return ScanRow(
        stock_id=str(rec["stock_id"]),
        stock_name=str(rec.get("stock_name") or ""),
        fresh=True,
        mono=False,
        seg_last=float(rec["seg_last"]),
        disp=float(rec.get("disp") or 0.0),
        segs=list(rec.get("segs") or []),
        quadrants=[],
        rs_ratio=0.0,
        rs_momentum=float(rec.get("rs_momentum") or 0.0),
        daily_pct=None,
    )


def leg_fingerprint(period: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(period.get("stock_id", "")),
        str(period.get("entry_date", "")),
        str(period.get("exit_date", "")),
        str(period.get("exit_reason") or ""),
    )


def scan_row_to_pending(row: Any) -> dict[str, Any]:
    from rrg_mono_daily_brief import ScanRow

    if isinstance(row, ScanRow):
        return {
            "stock_id": row.stock_id,
            "stock_name": row.stock_name,
            "seg_last": row.seg_last,
            "disp": row.disp,
            "rs_momentum": row.rs_momentum,
            "segs": row.segs,
        }
    return dict(row)


def queue_entry_candidates(
    pending: list[dict[str, Any]],
    *,
    gate: WeekdayGateConfig,
    signal_date: str,
    full_dates: list[str],
    rows: list[Any],
    max_n: int,
) -> tuple[int, int]:
    """Queue up to max_n entry candidates · return (deferred, expired)."""
    target = entry_target_date(gate, signal_date, full_dates)
    if target is None:
        return 0, max_n
    deferred = 0
    expired = 0
    held_ids = {str(p["row"]["stock_id"]) for p in pending}
    for row in rows[:max_n]:
        rec = scan_row_to_pending(row)
        sid = str(rec["stock_id"])
        if sid in held_ids:
            continue
        held_ids.add(sid)
        pending.append(
            {
                "signal_date": signal_date,
                "target_date": target,
                "row": rec,
            }
        )
        deferred += 1
    return deferred, expired


def flush_pending_entries(
    pending: list[dict[str, Any]],
    *,
    gate: WeekdayGateConfig,
    as_of: str,
    full_dates: list[str],
) -> tuple[list[dict[str, Any]], int, list[dict[str, Any]]]:
    """Split pending into remaining (future) · expired count · ready for as_of."""
    remaining: list[dict[str, Any]] = []
    expired = 0
    ready: list[dict[str, Any]] = []
    for pe in pending:
        sig = str(pe["signal_date"])
        target = str(pe["target_date"])
        if signal_expired(gate, sig, as_of, full_dates):
            expired += 1
            continue
        if as_of < target:
            remaining.append(pe)
            continue
        ready.append(pe)
    return remaining, expired, ready
