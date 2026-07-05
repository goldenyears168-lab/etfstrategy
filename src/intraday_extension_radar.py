"""Intraday extension radar（盤中延伸雷達）· VWAP 帶 + climax fade。

Research / Order layer 共用 · 1 分 K PIT。

Lineage（外部參考，非程式依賴）:
  - quantive3/vwap-reclaim: stretch 離 VWAP → reclaim 出場
  - VWAP ± k·σ 帶（均值回歸過熱區）
  - price-action-lib: volume_climax · gap_exhaustion
  - B combo: opening spike · climax at high · VWAP loss · no higher high
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from stock_db.kbar import KbarBar

ExtensionZone = Literal["green", "yellow", "red"]
ExitMode = Literal[
    "none",
    "yellow_vwap",
    "red_climax",
    "combo_b",
    "combo_spike",
    "heat_threshold",
    "stretch_reclaim",
    "spike_climax_sigma",
    "climax_fade",
    "climax_sell_strength",
    "vwap_failure",
    "post_peak_vwap",
]

EntryMode = Literal[
    "spike_cross",
    "climax_strength",
    "opening_spike",
    "vwap_reclaim",
]

NO_TRADE_BEFORE = "09:05"
NO_TRADE_END = "13:25"
OPENING_SPIKE_END = "09:30"
DEFAULT_VOL_Z_CLimax = 2.0
DEFAULT_VWAP_SIGMA_YELLOW = 1.5
DEFAULT_VWAP_SIGMA_RED = 2.0
DEFAULT_EXT_PREV_YELLOW = 4.0
DEFAULT_EXT_MA20_YELLOW = 12.0
DEFAULT_HEAT_EXIT = 70
DEFAULT_STRETCH_VWAP_PCT = 2.0


@dataclass(frozen=True)
class ExtensionScanConfig:
    """1 分 K 掃描參數 · sweep 用。"""

    exit_mode: ExitMode = "heat_threshold"
    entry_mode: EntryMode | None = None
    heat_threshold: int = DEFAULT_HEAT_EXIT
    vol_z_climax: float = DEFAULT_VOL_Z_CLimax
    poll_interval_min: int = 1
    min_ext_prev_pct: float = 0.0
    stretch_vwap_pct: float = DEFAULT_STRETCH_VWAP_PCT
    reclaim_vwap_pct: float = 0.3
    fade_from_high_exit_pct: float = 2.0
    vwap_sigma_red: float = DEFAULT_VWAP_SIGMA_RED
    require_opening_spike: bool = True
    no_hh_bars: int = 30
    vwap_loss_window: int = 45
    min_fade_from_high_pct: float = 0.5
    require_2sigma_exit: bool = False
    opening_spike_end: str = OPENING_SPIKE_END
    entry_min_ext_prev_pct: float | None = None
    exit_min_ext_prev_pct: float | None = None


def _heat_threshold_for(config: ExtensionScanConfig | None) -> int:
    return config.heat_threshold if config else DEFAULT_HEAT_EXIT


def _vol_z_for(config: ExtensionScanConfig | None) -> float:
    return config.vol_z_climax if config else DEFAULT_VOL_Z_CLimax


@dataclass(frozen=True)
class BarSnapshot:
    minute: str
    close: float
    volume: int
    vwap: float
    vwap_std: float
    vwap_upper_15: float
    vwap_upper_2: float
    prev_close: float
    ma20: float | None
    session_high: float
    session_high_minute: str
    vol_z: float
    ext_prev_pct: float
    ext_vwap_pct: float
    ext_vwap_sigma: float
    ext_ma20_pct: float | None
    is_session_high: bool
    is_climax_bar: bool
    below_vwap: bool
    fade_from_high_pct: float
    opening_spike: bool
    zone: ExtensionZone
    heat_score: int
    flags: tuple[str, ...] = ()


def _poll_minute_set(snaps: tuple[BarSnapshot, ...], interval_min: int) -> set[str]:
    if interval_min <= 1:
        return {s.minute for s in snaps}
    out: set[str] = set()
    for s in snaps:
        parts = s.minute.split(":")
        hh, mm = int(parts[0]), int(parts[1])
        total = (hh * 60 + mm) // interval_min * interval_min
        out.add(f"{total // 60:02d}:{total % 60:02d}:00")
    return out


@dataclass(frozen=True)
class ExtensionExitSignal:
    minute: str
    price: float
    mode: ExitMode
    zone: ExtensionZone
    heat_score: int
    flags: tuple[str, ...]


@dataclass(frozen=True)
class ExtensionEntrySignal:
    minute: str
    price: float
    mode: EntryMode
    zone: ExtensionZone
    heat_score: int
    flags: tuple[str, ...] = ()


@dataclass
class SessionScan:
    trade_date: str
    stock_id: str
    prev_close: float
    ma20: float | None
    bars: tuple[BarSnapshot, ...] = ()
    first_yellow: ExtensionExitSignal | None = None
    first_red: ExtensionExitSignal | None = None
    combo_b: ExtensionExitSignal | None = None
    combo_spike: ExtensionExitSignal | None = None
    heat_exit: ExtensionExitSignal | None = None
    stretch_reclaim: ExtensionExitSignal | None = None
    spike_climax_sigma: ExtensionExitSignal | None = None
    climax_fade: ExtensionExitSignal | None = None
    climax_sell_strength: ExtensionExitSignal | None = None
    vwap_failure: ExtensionExitSignal | None = None
    post_peak_vwap: ExtensionExitSignal | None = None


def _minute_ge(a: str, b: str) -> bool:
    return a >= b


def _rolling_std(values: list[float], window: int) -> float:
    if len(values) < 2:
        return 0.0
    tail = values[-window:]
    if len(tail) < 2:
        return 0.0
    mean = sum(tail) / len(tail)
    var = sum((x - mean) ** 2 for x in tail) / len(tail)
    return math.sqrt(var) if var > 0 else 0.0


def _vol_z(volumes: list[int], idx: int, window: int = 20) -> float:
    if idx < 1:
        return 0.0
    start = max(0, idx - window)
    hist = [float(v) for v in volumes[start:idx] if v > 0]
    if not hist:
        return 0.0
    avg = sum(hist) / len(hist)
    if avg <= 0:
        return 0.0
    return float(volumes[idx]) / avg


def _classify_zone(
    *,
    ext_vwap_sigma: float,
    ext_prev_pct: float,
    ext_ma20_pct: float | None,
    is_climax_bar: bool,
    below_vwap: bool,
    is_session_high: bool,
    heat_score: int,
    heat_threshold: int = DEFAULT_HEAT_EXIT,
) -> ExtensionZone:
    if heat_score >= heat_threshold:
        return "red"
    if is_climax_bar and is_session_high:
        return "yellow"
    if ext_vwap_sigma >= DEFAULT_VWAP_SIGMA_RED:
        return "red"
    if ext_vwap_sigma >= DEFAULT_VWAP_SIGMA_YELLOW or ext_prev_pct >= DEFAULT_EXT_PREV_YELLOW:
        return "yellow"
    if ext_ma20_pct is not None and ext_ma20_pct >= DEFAULT_EXT_MA20_YELLOW:
        return "yellow"
    return "green"


def _heat_score(
    *,
    ext_prev_pct: float,
    ext_ma20_pct: float | None,
    ext_vwap_sigma: float,
    is_climax_bar: bool,
    below_vwap: bool,
    opening_spike: bool,
    fade_from_high_pct: float,
) -> int:
    score = 0
    if ext_prev_pct >= 3.0:
        score += 10
    if ext_prev_pct >= DEFAULT_EXT_PREV_YELLOW:
        score += 15
    if ext_prev_pct >= 5.0:
        score += 10
    if ext_ma20_pct is not None and ext_ma20_pct >= DEFAULT_EXT_MA20_YELLOW:
        score += 20
    if ext_vwap_sigma >= DEFAULT_VWAP_SIGMA_YELLOW:
        score += 10
    if ext_vwap_sigma >= DEFAULT_VWAP_SIGMA_RED:
        score += 15
    if is_climax_bar:
        score += 20
    if below_vwap and fade_from_high_pct <= -1.0:
        score += 15
    if opening_spike:
        score += 10
    if fade_from_high_pct <= -3.0:
        score += 10
    return min(100, score)


def build_bar_snapshots(
    bars: tuple[KbarBar, ...],
    *,
    prev_close: float,
    ma20: float | None = None,
    vol_z_climax: float = DEFAULT_VOL_Z_CLimax,
    heat_threshold: int = DEFAULT_HEAT_EXIT,
) -> tuple[BarSnapshot, ...]:
    """PIT 掃描當日 1 分 K · 輸出每 bar 延伸狀態。"""
    if prev_close <= 0:
        return ()

    tradeable = [
        b
        for b in bars
        if _minute_ge(b.minute, NO_TRADE_BEFORE) and b.minute <= NO_TRADE_END
    ]
    if not tradeable:
        return ()

    out: list[BarSnapshot] = []
    closes: list[float] = []
    volumes: list[int] = []
    cum_pv = 0.0
    cum_v = 0.0
    session_high = 0.0
    session_high_minute = ""
    peak_px = 0.0

    for i, bar in enumerate(tradeable):
        vol = int(bar.volume or 0)
        tp = (bar.high + bar.low + bar.close) / 3.0
        w = float(vol) if vol > 0 else 1.0
        cum_pv += tp * w
        cum_v += w
        vwap = cum_pv / cum_v if cum_v > 0 else bar.close

        closes.append(bar.close)
        volumes.append(vol)

        if bar.high > session_high:
            session_high = bar.high
            session_high_minute = bar.minute

        std = max(_rolling_std(closes, 20), bar.close * 0.002)
        vz = _vol_z(volumes, i)
        ext_prev = (bar.close / prev_close - 1.0) * 100.0
        ext_vwap = (bar.close / vwap - 1.0) * 100.0 if vwap > 0 else 0.0
        ext_vwap_sigma = (bar.close - vwap) / std if std > 1e-9 else 0.0
        ext_ma20 = ((bar.close / ma20 - 1.0) * 100.0) if ma20 and ma20 > 0 else None
        is_high = bar.high >= session_high * 0.998
        is_climax = is_high and vz >= vol_z_climax
        below = bar.close < vwap
        opening_spike = _minute_ge(bar.minute, NO_TRADE_BEFORE) and bar.minute <= f"{OPENING_SPIKE_END}:00" and ext_prev >= 5.0
        if bar.close >= peak_px:
            peak_px = bar.close
            peak_minute = bar.minute
        fade = (bar.close / peak_px - 1.0) * 100.0 if peak_px > 0 else 0.0
        heat = _heat_score(
            ext_prev_pct=ext_prev,
            ext_ma20_pct=ext_ma20,
            ext_vwap_sigma=ext_vwap_sigma,
            is_climax_bar=is_climax,
            below_vwap=below,
            opening_spike=opening_spike,
            fade_from_high_pct=fade,
        )
        flags: list[str] = []
        if opening_spike:
            flags.append("opening_spike")
        if is_climax:
            flags.append("climax_at_high")
        if below:
            flags.append("below_vwap")
        if ext_vwap_sigma >= DEFAULT_VWAP_SIGMA_RED:
            flags.append("vwap_2sigma")
        zone = _classify_zone(
            ext_vwap_sigma=ext_vwap_sigma,
            ext_prev_pct=ext_prev,
            ext_ma20_pct=ext_ma20,
            is_climax_bar=is_climax,
            below_vwap=below,
            is_session_high=is_high,
            heat_score=heat,
            heat_threshold=heat_threshold,
        )
        out.append(
            BarSnapshot(
                minute=bar.minute,
                close=bar.close,
                volume=vol,
                vwap=vwap,
                vwap_std=std,
                vwap_upper_15=vwap + DEFAULT_VWAP_SIGMA_YELLOW * std,
                vwap_upper_2=vwap + DEFAULT_VWAP_SIGMA_RED * std,
                prev_close=prev_close,
                ma20=ma20,
                session_high=session_high,
                session_high_minute=session_high_minute,
                vol_z=vz,
                ext_prev_pct=round(ext_prev, 3),
                ext_vwap_pct=round(ext_vwap, 3),
                ext_vwap_sigma=round(ext_vwap_sigma, 3),
                ext_ma20_pct=round(ext_ma20, 3) if ext_ma20 is not None else None,
                is_session_high=is_high,
                is_climax_bar=is_climax,
                below_vwap=below,
                fade_from_high_pct=round(fade, 3),
                opening_spike=opening_spike,
                zone=zone,
                heat_score=heat,
                flags=tuple(flags),
            )
        )
    return tuple(out)


def _minutes_after_peak(bars: tuple[BarSnapshot, ...], peak_minute: str, within: int) -> tuple[BarSnapshot, ...]:
    if not peak_minute:
        return ()
    out: list[BarSnapshot] = []
    seen_peak = False
    for b in bars:
        if b.minute == peak_minute:
            seen_peak = True
            continue
        if not seen_peak:
            continue
        out.append(b)
        if len(out) >= within:
            break
    return tuple(out)


def _sig_from_snap(s: BarSnapshot, mode: ExitMode, extra: tuple[str, ...] = ()) -> ExtensionExitSignal:
    return ExtensionExitSignal(
        minute=s.minute,
        price=s.close,
        mode=mode,
        zone=s.zone,
        heat_score=s.heat_score,
        flags=s.flags + extra,
    )


def _combo_exit(
    snaps: tuple[BarSnapshot, ...],
    *,
    mode: ExitMode,
    min_spike_pct: float,
    vol_z_climax: float,
    require_opening: bool,
    no_hh_bars: int,
    vwap_loss_window: int,
    min_fade_pct: float,
) -> ExtensionExitSignal | None:
    if not snaps:
        return None
    peak_snap = max(snaps, key=lambda s: s.close)
    after_peak = [s for s in snaps if s.minute > peak_snap.minute]
    vwap_loss_bars = [
        b
        for b in after_peak[:vwap_loss_window]
        if b.below_vwap and b.fade_from_high_pct <= -min_fade_pct
    ]
    no_hh = all(b.close <= peak_snap.close for b in after_peak[:no_hh_bars])
    had_opening = (
        peak_snap.minute <= f"{OPENING_SPIKE_END}:00" and peak_snap.ext_prev_pct >= min_spike_pct
    )
    had_spike = peak_snap.ext_prev_pct >= min_spike_pct
    spike_ok = had_opening if require_opening else had_spike
    climax = (
        peak_snap.is_climax_bar
        or peak_snap.vol_z >= vol_z_climax
        or peak_snap.heat_score >= 70
    )
    if spike_ok and climax and vwap_loss_bars and no_hh:
        first_below = vwap_loss_bars[0]
        return _sig_from_snap(first_below, mode, ("combo",))
    return None


def _peak_confirm_exit(
    snaps: tuple[BarSnapshot, ...],
    *,
    mode: ExitMode,
    min_spike_pct: float,
    vol_z_climax: float,
    vwap_sigma_red: float,
    require_climax: bool,
    require_2sigma: bool,
    fade_exit_pct: float,
    allow_vwap_loss: bool,
) -> ExtensionExitSignal | None:
    if not snaps:
        return None
    peak_snap = max(snaps, key=lambda s: s.close)
    if peak_snap.ext_prev_pct < min_spike_pct:
        return None
    if require_climax and not (
        peak_snap.is_climax_bar or peak_snap.vol_z >= vol_z_climax
    ):
        return None
    if require_2sigma and peak_snap.ext_vwap_sigma < vwap_sigma_red:
        return None
    after_peak = [s for s in snaps if s.minute > peak_snap.minute]
    for s in after_peak:
        if s.fade_from_high_pct <= -fade_exit_pct:
            return _sig_from_snap(s, mode, ("climax_fade",))
        if allow_vwap_loss and s.below_vwap and s.fade_from_high_pct <= -0.5:
            return _sig_from_snap(s, mode, ("post_peak_vwap",))
    return None


def _climax_sell_strength_exit(
    snaps: tuple[BarSnapshot, ...],
    *,
    min_spike_pct: float,
    vol_z_climax: float,
    vwap_sigma_red: float,
    require_2sigma: bool,
) -> ExtensionExitSignal | None:
    """Minervini sell-into-strength · 尖峰 climax bar 當下出場（非等回落）。"""
    for s in snaps:
        if s.ext_prev_pct < min_spike_pct:
            continue
        if not (s.is_climax_bar or s.vol_z >= vol_z_climax):
            continue
        if require_2sigma and s.ext_vwap_sigma < vwap_sigma_red:
            continue
        if not s.is_session_high:
            continue
        return _sig_from_snap(s, "climax_sell_strength", ("sell_strength",))
    return None


def scan_session(
    bars: tuple[KbarBar, ...],
    *,
    trade_date: str,
    stock_id: str,
    prev_close: float,
    ma20: float | None = None,
    poll_interval_min: int = 5,
    heat_threshold: int = DEFAULT_HEAT_EXIT,
    config: ExtensionScanConfig | None = None,
) -> SessionScan:
    """單日全 session 掃描 · 產出各 exit 模式最早觸發點。"""
    cfg = config or ExtensionScanConfig(
        poll_interval_min=poll_interval_min,
        heat_threshold=heat_threshold,
    )
    vol_z = cfg.vol_z_climax
    heat_threshold = cfg.heat_threshold
    snaps = build_bar_snapshots(
        bars,
        prev_close=prev_close,
        ma20=ma20,
        vol_z_climax=vol_z,
        heat_threshold=heat_threshold,
    )
    scan = SessionScan(
        trade_date=trade_date,
        stock_id=stock_id,
        prev_close=prev_close,
        ma20=ma20,
        bars=snaps,
    )
    if not snaps:
        return scan

    poll_set = _poll_minute_set(snaps, cfg.poll_interval_min)
    min_spike = cfg.min_ext_prev_pct if cfg.min_ext_prev_pct > 0 else 5.0

    max_ext_vwap = 0.0
    stretch_armed = False
    sigma_armed = False

    for s in snaps:
        if s.minute not in poll_set and s.minute != snaps[-1].minute:
            continue
        sig = ExtensionExitSignal(
            minute=s.minute,
            price=s.close,
            mode="none",
            zone=s.zone,
            heat_score=s.heat_score,
            flags=s.flags,
        )
        if scan.first_yellow is None and s.zone in ("yellow", "red"):
            scan.first_yellow = ExtensionExitSignal(
                **{**sig.__dict__, "mode": "yellow_vwap"}
            )
        if scan.first_red is None and s.zone == "red":
            scan.first_red = ExtensionExitSignal(
                **{**sig.__dict__, "mode": "red_climax"}
            )
        if scan.heat_exit is None and s.heat_score >= heat_threshold:
            scan.heat_exit = ExtensionExitSignal(
                **{**sig.__dict__, "mode": "heat_threshold"}
            )

        if s.ext_vwap_pct > max_ext_vwap:
            max_ext_vwap = s.ext_vwap_pct
        if s.ext_vwap_pct >= cfg.stretch_vwap_pct or s.ext_vwap_sigma >= cfg.vwap_sigma_red:
            stretch_armed = True
        if (
            scan.stretch_reclaim is None
            and stretch_armed
            and max_ext_vwap >= cfg.stretch_vwap_pct
            and (max_ext_vwap - s.ext_vwap_pct) >= cfg.reclaim_vwap_pct
        ):
            scan.stretch_reclaim = _sig_from_snap(s, "stretch_reclaim", ("reclaim",))

        if s.ext_vwap_sigma >= cfg.vwap_sigma_red and s.ext_prev_pct >= min_spike:
            sigma_armed = True
        if scan.vwap_failure is None and sigma_armed and s.below_vwap:
            scan.vwap_failure = _sig_from_snap(s, "vwap_failure", ("vwap_fail",))

    scan.combo_b = _combo_exit(
        snaps,
        mode="combo_b",
        min_spike_pct=min_spike,
        vol_z_climax=vol_z,
        require_opening=True,
        no_hh_bars=cfg.no_hh_bars,
        vwap_loss_window=cfg.vwap_loss_window,
        min_fade_pct=cfg.min_fade_from_high_pct,
    )
    scan.combo_spike = _combo_exit(
        snaps,
        mode="combo_spike",
        min_spike_pct=min_spike,
        vol_z_climax=vol_z,
        require_opening=False,
        no_hh_bars=cfg.no_hh_bars,
        vwap_loss_window=cfg.vwap_loss_window,
        min_fade_pct=cfg.min_fade_from_high_pct,
    )
    scan.spike_climax_sigma = _peak_confirm_exit(
        snaps,
        mode="spike_climax_sigma",
        min_spike_pct=min_spike,
        vol_z_climax=vol_z,
        vwap_sigma_red=cfg.vwap_sigma_red,
        require_climax=True,
        require_2sigma=True,
        fade_exit_pct=max(1.0, cfg.fade_from_high_exit_pct * 0.5),
        allow_vwap_loss=True,
    )
    scan.climax_fade = _peak_confirm_exit(
        snaps,
        mode="climax_fade",
        min_spike_pct=min_spike,
        vol_z_climax=vol_z,
        vwap_sigma_red=cfg.vwap_sigma_red,
        require_climax=True,
        require_2sigma=False,
        fade_exit_pct=cfg.fade_from_high_exit_pct,
        allow_vwap_loss=False,
    )
    scan.post_peak_vwap = _peak_confirm_exit(
        snaps,
        mode="post_peak_vwap",
        min_spike_pct=min_spike,
        vol_z_climax=vol_z,
        vwap_sigma_red=cfg.vwap_sigma_red,
        require_climax=False,
        require_2sigma=False,
        fade_exit_pct=99.0,
        allow_vwap_loss=True,
    )
    scan.climax_sell_strength = _climax_sell_strength_exit(
        snaps,
        min_spike_pct=min_spike,
        vol_z_climax=vol_z,
        vwap_sigma_red=cfg.vwap_sigma_red,
        require_2sigma=cfg.require_2sigma_exit,
    )

    return scan


def pick_exit_for_mode(scan: SessionScan, mode: ExitMode) -> ExtensionExitSignal | None:
    if mode == "none":
        return None
    if mode == "yellow_vwap":
        return scan.first_yellow
    if mode == "red_climax":
        return scan.first_red
    if mode == "combo_b":
        return scan.combo_b
    if mode == "combo_spike":
        return scan.combo_spike
    if mode == "heat_threshold":
        return scan.heat_exit
    if mode == "stretch_reclaim":
        return scan.stretch_reclaim
    if mode == "spike_climax_sigma":
        return scan.spike_climax_sigma
    if mode == "climax_fade":
        return scan.climax_fade
    if mode == "climax_sell_strength":
        return scan.climax_sell_strength
    if mode == "vwap_failure":
        return scan.vwap_failure
    if mode == "post_peak_vwap":
        return scan.post_peak_vwap
    return None


def _entry_sig_from_snap(s: BarSnapshot, mode: EntryMode, extra: tuple[str, ...] = ()) -> ExtensionEntrySignal:
    return ExtensionEntrySignal(
        minute=s.minute,
        price=s.close,
        mode=mode,
        zone=s.zone,
        heat_score=s.heat_score,
        flags=s.flags + extra,
    )


def _spike_cross_entry(
    snaps: tuple[BarSnapshot, ...],
    *,
    poll_set: set[str],
    min_spike_pct: float,
) -> ExtensionEntrySignal | None:
    for s in snaps:
        if s.minute not in poll_set:
            continue
        if s.ext_prev_pct >= min_spike_pct:
            return _entry_sig_from_snap(s, "spike_cross", ("spike",))
    return None


def _climax_strength_entry(
    snaps: tuple[BarSnapshot, ...],
    *,
    poll_set: set[str],
    min_spike_pct: float,
    vol_z_climax: float,
) -> ExtensionEntrySignal | None:
    for s in snaps:
        if s.minute not in poll_set:
            continue
        if s.ext_prev_pct < min_spike_pct:
            continue
        if not (s.is_climax_bar or s.vol_z >= vol_z_climax):
            continue
        if not s.is_session_high:
            continue
        return _entry_sig_from_snap(s, "climax_strength", ("climax",))
    return None


def _opening_spike_entry(
    snaps: tuple[BarSnapshot, ...],
    *,
    poll_set: set[str],
    min_spike_pct: float,
    opening_end: str = OPENING_SPIKE_END,
) -> ExtensionEntrySignal | None:
    end_minute = opening_end if opening_end.endswith(":00") else f"{opening_end}:00"
    for s in snaps:
        if s.minute not in poll_set:
            continue
        if not (_minute_ge(s.minute, NO_TRADE_BEFORE) and s.minute <= end_minute):
            continue
        if s.ext_prev_pct >= min_spike_pct:
            return _entry_sig_from_snap(s, "opening_spike", ("opening",))
    return None


def _vwap_reclaim_entry(
    snaps: tuple[BarSnapshot, ...],
    *,
    poll_set: set[str],
    min_spike_pct: float,
    stretch_vwap_pct: float,
    reclaim_vwap_pct: float,
) -> ExtensionEntrySignal | None:
    max_ext_vwap = 0.0
    stretch_armed = False
    for s in snaps:
        if s.minute not in poll_set:
            continue
        if s.ext_vwap_pct > max_ext_vwap:
            max_ext_vwap = s.ext_vwap_pct
        if s.ext_vwap_pct >= stretch_vwap_pct and s.ext_prev_pct >= min_spike_pct:
            stretch_armed = True
        if (
            stretch_armed
            and max_ext_vwap >= stretch_vwap_pct
            and (max_ext_vwap - s.ext_vwap_pct) >= reclaim_vwap_pct
            and not s.below_vwap
        ):
            return _entry_sig_from_snap(s, "vwap_reclaim", ("reclaim",))
    return None


def find_first_extension_entry(
    bars: tuple[KbarBar, ...],
    *,
    prev_close: float,
    ma20: float | None,
    config: ExtensionScanConfig,
) -> ExtensionEntrySignal | None:
    """1 分 K PIT · 當日最早延伸進場點（與 exit 共用 BarSnapshot 特徵）。"""
    if config.entry_mode is None:
        return None
    cfg = config
    snaps = build_bar_snapshots(
        bars,
        prev_close=prev_close,
        ma20=ma20,
        vol_z_climax=cfg.vol_z_climax,
        heat_threshold=cfg.heat_threshold,
    )
    if not snaps:
        return None
    poll_set = _poll_minute_set(snaps, cfg.poll_interval_min)
    entry_min = (
        cfg.entry_min_ext_prev_pct
        if cfg.entry_min_ext_prev_pct is not None and cfg.entry_min_ext_prev_pct > 0
        else (cfg.min_ext_prev_pct if cfg.min_ext_prev_pct > 0 else 4.0)
    )
    mode = config.entry_mode
    if mode == "spike_cross":
        return _spike_cross_entry(snaps, poll_set=poll_set, min_spike_pct=entry_min)
    if mode == "climax_strength":
        return _climax_strength_entry(
            snaps, poll_set=poll_set, min_spike_pct=entry_min, vol_z_climax=cfg.vol_z_climax
        )
    if mode == "opening_spike":
        return _opening_spike_entry(
            snaps,
            poll_set=poll_set,
            min_spike_pct=entry_min,
            opening_end=cfg.opening_spike_end,
        )
    if mode == "vwap_reclaim":
        return _vwap_reclaim_entry(
            snaps,
            poll_set=poll_set,
            min_spike_pct=entry_min,
            stretch_vwap_pct=cfg.stretch_vwap_pct,
            reclaim_vwap_pct=cfg.reclaim_vwap_pct,
        )
    return None


def find_first_extension_exit(
    bars: tuple[KbarBar, ...],
    *,
    prev_close: float,
    ma20: float | None,
    config: ExtensionScanConfig,
) -> ExtensionExitSignal | None:
    """1 分 K PIT · 當日最早延伸出場點（poll_interval_min=1 即逐 bar）。"""
    scan = scan_session(
        bars,
        trade_date="",
        stock_id="",
        prev_close=prev_close,
        ma20=ma20,
        config=config,
    )
    sig = pick_exit_for_mode(scan, config.exit_mode)
    if sig is None:
        return None
    if config.min_ext_prev_pct > 0:
        snap = next((b for b in scan.bars if b.minute == sig.minute), None)
        if snap is None or snap.ext_prev_pct < config.min_ext_prev_pct:
            return None
    return sig
