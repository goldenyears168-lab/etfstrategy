"""View-ready screen_status for strategy daily brief snapshots."""

from __future__ import annotations


def screen_status(kind: str, text_zh: str) -> dict[str, str]:
    return {"kind": kind, "text_zh": text_zh}


def copytrade_screen_status(signal_count: int) -> dict[str, str]:
    if signal_count > 0:
        return screen_status("active", f"今日 {signal_count} 檔跟單訊號")
    return screen_status("empty", "今日無跟單訊號")


def rrg_screen_status(
    *,
    intraday: bool,
    mono_count: int,
    fresh_count: int,
    slots_label: str | None,
) -> dict[str, str]:
    prefix = "盤中預估" if intraday else "收盤版"
    if slots_label:
        active = mono_count > 0 or fresh_count > 0
        kind = "active" if active else "empty"
        return screen_status(kind, f"{prefix} {slots_label} 槽")
    total = mono_count + fresh_count
    if total > 0:
        return screen_status("active", f"今日 {total} 檔候選（{prefix}）")
    return screen_status("empty", f"{prefix}尚未有入選")


def vcp_screen_status(candidate_count: int) -> dict[str, str]:
    if candidate_count > 0:
        return screen_status("active", f"今日 {candidate_count} 檔候選")
    return screen_status("empty", "今日無候選")
