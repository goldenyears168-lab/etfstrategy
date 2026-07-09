"""C18acc 持倉 · Extension radar overlay（dry-run log · optional intent）。

Round3 採納 I36：combo_spike · spike≥4% · fade≥1% · 1m PIT（無信號不動）。
X4 預警：heat≥70 可另開 C18ACC_EXTENSION_WATCH_HEAT。
OR watch：OR_EXT_WATCH_MODE=1 偵測 opening ext≥3%（只看不賣 · 寫 CSV log）。
  O3 ≡ I36：81 watch hits · 13 先於 combo · 不改賣出邏輯（see config/research.yaml）。
"""

from __future__ import annotations

import csv
import json
import os
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from intraday_extension_radar import (
    DEFAULT_HEAT_EXIT,
    ExitMode,
    ExtensionExitSignal,
    ExtensionScanConfig,
    BarSnapshot,
    build_bar_snapshots,
    pick_exit_for_mode,
    scan_session,
)
from stock_db import PROJECT_ROOT
from research.backtest.c18acc_extension_exit import _ma20, _prior_close
from research.backtest.rrg_mono_score_swap_c import _trading_days_between
from stock_db.kbar import kbar_mainline_day_has_data, load_kbar_day_5m_bars

OVERLAY_LOG_PATH = PROJECT_ROOT / "logs" / "c18acc_extension_overlay.log"
OR_EXT_WATCH_CSV_PATH = PROJECT_ROOT / "reports" / "research" / "rrg" / "c18acc_or_ext_watch_log.csv"
# Round3 I36 · hybrid backtest +0.30pp vs I0（2024–2026）
DEFAULT_EXIT_MODE: ExitMode = "combo_spike"
DEFAULT_MIN_SPIKE_PCT = 4.0
DEFAULT_MIN_FADE_PCT = 1.0
DEFAULT_POLL_INTERVAL_MIN = 1
# OR watch · X4 only · O3 ≡ I36 · see cz_track5_or_fade_verdict_2026.json
DEFAULT_OR_WATCH_MIN_EXT_PCT = 3.0
OR_WATCH_OPENING_END = "09:30:00"


@dataclass(frozen=True)
class ExtensionAlert:
    stock_id: str
    stock_name: str
    minute: str
    price: float
    heat_score: int
    zone: str
    exit_mode: str
    ext_prev_pct: float
    flags: tuple[str, ...]
    note: str

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["flags"] = list(self.flags)
        return d


@dataclass(frozen=True)
class OrExtWatchEvent:
    """OR+ext≥3% opening-period watch hit · X4 only · does NOT trigger a sell."""

    stock_id: str
    stock_name: str
    session: str
    or_minute: str
    or_ext_pct: float
    i36_triggered: bool
    notes: str = ""


def _detect_or_ext_watch(
    snaps: tuple[BarSnapshot, ...],
    *,
    min_ext_pct: float = DEFAULT_OR_WATCH_MIN_EXT_PCT,
    opening_end: str = OR_WATCH_OPENING_END,
) -> tuple[str, float] | None:
    """Return (minute, ext_prev_pct) of the first opening-period bar with ext≥min_ext_pct."""
    for s in snaps:
        if s.minute > opening_end:
            break
        if s.ext_prev_pct >= min_ext_pct:
            return s.minute, s.ext_prev_pct
    return None


def append_or_ext_watch_csv(
    events: list[OrExtWatchEvent],
    *,
    csv_path: Path | None = None,
) -> Path:
    """Append OR watch events to the research CSV log (watch-only, no sell)."""
    path = csv_path or OR_EXT_WATCH_CSV_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not path.exists() or path.stat().st_size == 0
    with path.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=["session", "stock_id", "stock_name", "or_minute", "or_ext_pct", "i36_triggered", "notes"],
        )
        if write_header:
            writer.writeheader()
        for ev in events:
            writer.writerow(
                {
                    "session": ev.session,
                    "stock_id": ev.stock_id,
                    "stock_name": ev.stock_name,
                    "or_minute": ev.or_minute,
                    "or_ext_pct": round(ev.or_ext_pct, 2),
                    "i36_triggered": ev.i36_triggered,
                    "notes": ev.notes,
                }
            )
    return path


def _env_flag(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default).strip().lower() not in ("0", "false", "no")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def overlay_scan_config(
    *,
    exit_mode: str | None = None,
    heat_threshold: int | None = None,
    poll_interval_min: int | None = None,
    min_spike_pct: float | None = None,
    min_fade_pct: float | None = None,
) -> ExtensionScanConfig:
    mode_raw = (exit_mode or os.environ.get("C18ACC_EXTENSION_MODE", DEFAULT_EXIT_MODE)).strip()
    mode: ExitMode = mode_raw if mode_raw in _EXIT_MODES else DEFAULT_EXIT_MODE  # type: ignore[assignment]
    return ExtensionScanConfig(
        exit_mode=mode,
        heat_threshold=heat_threshold
        if heat_threshold is not None
        else _env_int("C18ACC_EXTENSION_HEAT", DEFAULT_HEAT_EXIT),
        poll_interval_min=poll_interval_min
        if poll_interval_min is not None
        else _env_int("C18ACC_EXTENSION_POLL_MIN", DEFAULT_POLL_INTERVAL_MIN),
        min_ext_prev_pct=min_spike_pct
        if min_spike_pct is not None
        else _env_float("C18ACC_EXTENSION_MIN_SPIKE_PCT", DEFAULT_MIN_SPIKE_PCT),
        min_fade_from_high_pct=min_fade_pct
        if min_fade_pct is not None
        else _env_float("C18ACC_EXTENSION_MIN_FADE_PCT", DEFAULT_MIN_FADE_PCT),
        require_opening_spike=False,
    )


_EXIT_MODES = frozenset(
    {
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
    }
)

_ONE_MINUTE_MODES = frozenset(
    {
        "combo_spike",
        "combo_b",
        "climax_fade",
        "spike_climax_sigma",
        "climax_sell_strength",
        "stretch_reclaim",
        "vwap_failure",
        "post_peak_vwap",
    }
)


def overlay_enabled() -> bool:
    return _env_flag("RUN_C18ACC_EXTENSION_OVERLAY", "0")


def poll_interval_for_now(now: datetime | None = None) -> int:
    """combo_spike 等 1m 模式全日逐 bar；heat 舊法 09:06–09:30 用 1m。"""
    mode = os.environ.get("C18ACC_EXTENSION_MODE", DEFAULT_EXIT_MODE).strip()
    if mode in _ONE_MINUTE_MODES:
        return _env_int("C18ACC_EXTENSION_POLL_MIN", DEFAULT_POLL_INTERVAL_MIN)
    now = now or datetime.now()
    if now.hour == 9 and now.minute < 30:
        return 1
    return _env_int("C18ACC_EXTENSION_POLL_MIN", 5)


def _alert_note(sig: ExtensionExitSignal, ext_prev: float, scan_cfg: ExtensionScanConfig) -> str:
    if sig.mode == "combo_spike":
        return (
            f"X3 combo_spike @ {sig.minute} ext_prev={ext_prev:.1f}% "
            f"fade≥{scan_cfg.min_fade_from_high_pct}% px={sig.price:.2f}"
        )
    if sig.mode == "combo_b":
        return f"X3 combo_b @ {sig.minute} ext_prev={ext_prev:.1f}% px={sig.price:.2f}"
    if sig.mode == "heat_threshold":
        return (
            f"X4 heat≥{scan_cfg.heat_threshold} @ {sig.minute} "
            f"ext_prev={ext_prev:.1f}% px={sig.price:.2f}"
        )
    return f"extension {sig.mode} @ {sig.minute} ext_prev={ext_prev:.1f}% px={sig.price:.2f}"


def scan_held_extension_alerts(
    conn: sqlite3.Connection,
    *,
    slots: list[dict[str, Any]],
    session: str,
    poll_minute: str,
    full_dates: list[str],
    min_hold_days: int = 5,
    exit_mode: str | None = None,
    heat_threshold: int | None = None,
    poll_interval_min: int | None = None,
    scan_config: ExtensionScanConfig | None = None,
) -> list[ExtensionAlert]:
    if not overlay_enabled() or not slots:
        return []

    scan_cfg = scan_config or overlay_scan_config(
        exit_mode=exit_mode,
        heat_threshold=heat_threshold,
        poll_interval_min=poll_interval_min,
    )
    confirm_mode = scan_cfg.exit_mode
    watch_heat = _env_int("C18ACC_EXTENSION_WATCH_HEAT", 0)
    or_watch_mode = _env_flag("OR_EXT_WATCH_MODE", "0")
    or_watch_min_ext = _env_float("OR_EXT_WATCH_MIN_EXT_PCT", DEFAULT_OR_WATCH_MIN_EXT_PCT)

    alerts: list[ExtensionAlert] = []
    or_watch_events: list[OrExtWatchEvent] = []
    for pos in slots:
        sid = str(pos["stock_id"])
        entry = str(pos.get("entry_date") or "")
        if not entry:
            continue
        if _trading_days_between(full_dates, entry, session) < min_hold_days:
            continue
        if not kbar_mainline_day_has_data(conn, sid, session):
            continue
        prev_close = _prior_close(conn, sid, session, full_dates)
        if prev_close is None:
            continue
        bars = load_kbar_day_5m_bars(conn, sid, session)
        if not bars:
            continue
        ma20 = _ma20(conn, sid, session, full_dates)
        scan = scan_session(
            bars,
            trade_date=session,
            stock_id=sid,
            prev_close=prev_close,
            ma20=ma20,
            config=scan_cfg,
        )

        # OR+ext≥3% watch (X4 · no sell) — O3 ≡ I36 proven in Track 5
        if or_watch_mode:
            or_hit = _detect_or_ext_watch(scan.bars, min_ext_pct=or_watch_min_ext)
            if or_hit:
                i36_check = pick_exit_for_mode(scan, "combo_spike")
                or_watch_events.append(
                    OrExtWatchEvent(
                        stock_id=sid,
                        stock_name=str(pos.get("stock_name") or ""),
                        session=session,
                        or_minute=or_hit[0],
                        or_ext_pct=or_hit[1],
                        i36_triggered=i36_check is not None,
                        notes=f"poll_minute={poll_minute}",
                    )
                )

        watch_scan = None
        sig = pick_exit_for_mode(scan, confirm_mode)
        if sig is None and watch_heat > 0:
            watch_cfg = ExtensionScanConfig(
                exit_mode="heat_threshold",
                heat_threshold=watch_heat,
                poll_interval_min=scan_cfg.poll_interval_min,
                min_ext_prev_pct=scan_cfg.min_ext_prev_pct,
            )
            watch_scan = scan_session(
                bars,
                trade_date=session,
                stock_id=sid,
                prev_close=prev_close,
                ma20=ma20,
                config=watch_cfg,
            )
            sig = pick_exit_for_mode(watch_scan, "heat_threshold")
        if sig is None:
            continue
        if sig.minute > poll_minute:
            continue
        bar_scan = watch_scan if watch_scan is not None else scan
        snap = next((b for b in bar_scan.bars if b.minute == sig.minute), None)
        ext_prev = snap.ext_prev_pct if snap else 0.0
        if scan_cfg.min_ext_prev_pct > 0 and ext_prev < scan_cfg.min_ext_prev_pct:
            continue
        alerts.append(
            ExtensionAlert(
                stock_id=sid,
                stock_name=str(pos.get("stock_name") or ""),
                minute=sig.minute,
                price=sig.price,
                heat_score=sig.heat_score,
                zone=sig.zone,
                exit_mode=sig.mode,
                ext_prev_pct=ext_prev,
                flags=sig.flags,
                note=_alert_note(sig, ext_prev, scan_cfg),
            )
        )
    if or_watch_mode and or_watch_events:
        append_or_ext_watch_csv(or_watch_events)
    return alerts


def append_overlay_log(
    *,
    session: str,
    poll_minute: str,
    alerts: list[ExtensionAlert],
    log_path: Path | None = None,
) -> Path:
    path = log_path or OVERLAY_LOG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().isoformat(timespec="seconds")
    if not alerts:
        line = f"{ts} | session={session} | minute={poll_minute} | alerts=0\n"
    else:
        bits = [
            f"{a.stock_id}@{a.minute}:{a.exit_mode}:px={a.price:.2f}"
            for a in alerts
        ]
        line = (
            f"{ts} | session={session} | minute={poll_minute} | "
            f"alerts={len(alerts)} | {';'.join(bits)}\n"
        )
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line)
    return path


def write_overlay_snapshot(
    *,
    session: str,
    poll_minute: str,
    alerts: list[ExtensionAlert],
) -> Path | None:
    if not alerts:
        return None
    out_dir = PROJECT_ROOT / "reports" / "order" / "extension_overlay"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"extension_overlay_{session.replace('-', '')}_{poll_minute.replace(':', '')}.json"
    scan_cfg = overlay_scan_config()
    payload = {
        "session_date": session,
        "poll_minute": poll_minute,
        "dry_run": _env_flag("C18ACC_EXTENSION_DRY_RUN", "1"),
        "exit_mode": scan_cfg.exit_mode,
        "min_spike_pct": scan_cfg.min_ext_prev_pct,
        "min_fade_pct": scan_cfg.min_fade_from_high_pct,
        "poll_interval_min": scan_cfg.poll_interval_min,
        "watch_heat": _env_int("C18ACC_EXTENSION_WATCH_HEAT", 0),
        "alerts": [a.to_dict() for a in alerts],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path
