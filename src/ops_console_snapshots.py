"""Build payloads for private ops console ``ops.snapshots`` kinds.

Kinds: ``watch`` · ``risk`` · ``thermo`` · ``branches`` (and optional ``today``).
Reads local monorepo reports / order snapshots; does not call brokers.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from stock_db import PROJECT_ROOT

ROOT = PROJECT_ROOT
EVENING_DIR = ROOT / "reports/research/branch-footprint-screen/evening_watch"
EXPERT_POOL_DIR = ROOT / "reports/research/branch-footprint-screen/expert_pool"
SECOND_DISP_DIR = (
    ROOT / "reports/research/branch-footprint-screen/second_disp_top30_l1h7"
)
THERMO_STABLE = ROOT / "reports/daily/crash_thermometer.md"
DETACH_LATEST = ROOT / "reports/order/snapshots/us_tw_5m_sell_gate_latest.json"
TIER_A_CFG = ROOT / "config/tier_a_evening_watch.json"
SECOND_DISP_CFG = ROOT / "config/second_disp_expert_pool_watch.json"


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def _latest_glob(dir_path: Path, pattern: str) -> Path | None:
    if not dir_path.is_dir():
        return None
    files = sorted(dir_path.glob(pattern), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


def _mtime_iso(path: Path | None) -> str | None:
    if path is None or not path.is_file():
        return None
    return datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")


def _parse_evening_digest(text: str) -> dict[str, Any]:
    """Pull overview fires from evening digest markdown."""
    fires: list[dict[str, str]] = []
    quiet_n = 0
    asof = None
    m_asof = re.search(r"夜間觀測 digest · (\d{4}-\d{2}-\d{2})", text)
    if m_asof:
        asof = m_asof.group(1)
    m_n = re.search(r"項目數：(\d+)\s*·\s*有訊號：(\d+)", text)
    n_items = int(m_n.group(1)) if m_n else None
    n_fires = int(m_n.group(2)) if m_n else None
    in_overview = False
    for line in text.splitlines():
        s = line.strip()
        if "總覽" in s and s.startswith("——"):
            in_overview = True
            continue
        if not in_overview:
            continue
        if s.startswith("——") and "總覽" not in s:
            break
        if s.startswith("★"):
            label = s.lstrip("★").strip()
            name, _, detail = label.partition(":")
            fires.append({"label": name.strip(), "detail": detail.strip() or "觸發"})
        elif s.startswith("·"):
            quiet_n += 1
    return {
        "asof": asof,
        "n_items": n_items,
        "n_fires": n_fires if n_fires is not None else len(fires),
        "n_quiet": quiet_n,
        "fires": fires,
    }


def build_watch_payload() -> dict[str, Any]:
    sources: list[dict[str, Any]] = []
    evening_path = _latest_glob(EVENING_DIR, "digest_*.md")
    evening: dict[str, Any] = {"present": False}
    if evening_path:
        body = evening_path.read_text(encoding="utf-8", errors="replace")
        parsed = _parse_evening_digest(body)
        evening = {
            "present": True,
            "path": str(evening_path.relative_to(ROOT)),
            "mtime": _mtime_iso(evening_path),
            **parsed,
            "preview_md": "\n".join(body.splitlines()[:80]),
        }
        sources.append({"kind": "evening_digest", "path": evening["path"]})

    pools: list[dict[str, Any]] = []
    if EXPERT_POOL_DIR.is_dir():
        for spec_path in sorted(EXPERT_POOL_DIR.glob("*/watch_spec.json")):
            spec = _read_json(spec_path) or {}
            sid = str(spec.get("stock_id") or spec_path.parent.name)
            latest_watch = _latest_glob(spec_path.parent, "watch_*.md")
            pools.append(
                {
                    "stock_id": sid,
                    "stock_name": spec.get("stock_name"),
                    "watch_md": (
                        str(latest_watch.relative_to(ROOT)) if latest_watch else None
                    ),
                    "watch_mtime": _mtime_iso(latest_watch),
                }
            )
        sources.append({"kind": "expert_pool_specs", "n": len(pools)})

    second: dict[str, Any] = {"present": False}
    sd_json = _latest_glob(SECOND_DISP_DIR, "watch_*.json")
    if sd_json:
        raw = _read_json(sd_json) or {}
        second = {
            "present": True,
            "path": str(sd_json.relative_to(ROOT)),
            "mtime": _mtime_iso(sd_json),
            "asof": raw.get("asof"),
            "n_hits": raw.get("n_hits", len(raw.get("hits") or [])),
            "n_remind": raw.get("n_remind"),
            "n_improved": raw.get("n_improved"),
            "hits": (raw.get("hits") or [])[:30],
            "note": raw.get("note"),
        }
        sources.append({"kind": "second_disp_watch", "path": second["path"]})
    elif SECOND_DISP_CFG.is_file():
        cfg = _read_json(SECOND_DISP_CFG) or {}
        second = {
            "present": True,
            "path": str(SECOND_DISP_CFG.relative_to(ROOT)),
            "mtime": _mtime_iso(SECOND_DISP_CFG),
            "title": cfg.get("title"),
            "status": cfg.get("status"),
            "n_hits": 0,
            "hits": [],
            "note": "尚無 watch_*.json；僅設定檔",
        }
        sources.append({"kind": "second_disp_cfg", "path": second["path"]})

    tier_a: dict[str, Any] = {"present": False}
    if TIER_A_CFG.is_file():
        cfg = _read_json(TIER_A_CFG) or {}
        tier_a = {
            "present": True,
            "path": str(TIER_A_CFG.relative_to(ROOT)),
            "updated": cfg.get("updated"),
            "entry_watch": cfg.get("entry_watch") or [],
            "exit_watch": cfg.get("exit_watch") or [],
            "note": cfg.get("note"),
        }
        sources.append({"kind": "tier_a_cfg", "path": tier_a["path"]})

    return {
        "schema": "ops-watch-v1",
        "title": "自選／專家池 Watch",
        "sources": sources,
        "evening": evening,
        "expert_pools": pools,
        "n_expert_pools": len(pools),
        "second_disp": second,
        "tier_a": tier_a,
    }


def build_risk_payload() -> dict[str, Any]:
    detach_raw = _read_json(DETACH_LATEST)
    detach: dict[str, Any]
    if detach_raw:
        latest = detach_raw.get("latest") if isinstance(detach_raw.get("latest"), dict) else {}
        detach = {
            "present": True,
            "path": str(DETACH_LATEST.relative_to(ROOT)),
            "mtime": _mtime_iso(DETACH_LATEST),
            "ok": detach_raw.get("ok"),
            "level": detach_raw.get("level"),
            "action_hint": detach_raw.get("action_hint"),
            "session_date": detach_raw.get("session_date"),
            "strategy_id": detach_raw.get("strategy_id"),
            "as_of": detach_raw.get("as_of"),
            "first_red_poll": detach_raw.get("first_red_poll"),
            "first_yellow_poll": detach_raw.get("first_yellow_poll"),
            "auto_order": detach_raw.get("auto_order"),
            "rebuy": detach_raw.get("rebuy"),
            "rule": detach_raw.get("rule"),
            "latest": {
                "poll": latest.get("poll"),
                "tw_from_open": latest.get("tw_from_open"),
                "nq_from_open": latest.get("nq_from_open"),
                "spread_30m": latest.get("spread_30m"),
                "gap_pulse": latest.get("gap_pulse"),
                "sync_pulse": latest.get("sync_pulse"),
                "red_now": latest.get("red_now"),
                "yellow_now": latest.get("yellow_now"),
            },
        }
        level = str(detach.get("level") or "UNKNOWN")
    else:
        detach = {
            "present": False,
            "path": str(DETACH_LATEST.relative_to(ROOT)),
            "note": "尚無 us_tw_5m_sell_gate_latest.json（mini detach-gate 寫入後顯示）",
        }
        level = "UNKNOWN"

    severity = "info"
    if level in {"RED", "RED_LATCHED"}:
        severity = "red"
    elif level in {"YELLOW", "YELLOW_LATCHED"}:
        severity = "warn"

    notes: list[str] = []
    if detach.get("present") and detach.get("action_hint"):
        notes.append(str(detach["action_hint"]))
    if not detach.get("present"):
        notes.append("Detach gate 快照缺失")

    return {
        "schema": "ops-risk-v1",
        "title": "風控 · Detach Gate",
        "severity": severity,
        "level": level,
        "detach_gate": detach,
        "notes": notes,
    }


def _parse_thermo_md(text: str) -> dict[str, Any]:
    asof = None
    m_asof = re.search(r"大跌溫度計 · (\d{4}-\d{2}-\d{2})", text)
    if m_asof:
        asof = m_asof.group(1)
    temp_pct = None
    lamp = None
    m_temp = re.search(
        r"溫度百分位[：:]\s*([0-9.]+)%\s*([🔴🟠🟡🟢])\s*(\S+)",
        text,
    )
    if m_temp:
        temp_pct = float(m_temp.group(1))
        lamp = f"{m_temp.group(2)} {m_temp.group(3)}"
    consensus = None
    m_c = re.search(r"共識家數[：:]\s*([0-9]+/[0-9]+)", text)
    if m_c:
        consensus = m_c.group(1)
    score = None
    m_s = re.search(r"複合分數[^：\n]*[：:]\s*([0-9.]+)", text)
    if m_s:
        score = float(m_s.group(1))
    trend: list[dict[str, Any]] = []
    in_trend = False
    for line in text.splitlines():
        if "近30個交易日趨勢" in line:
            in_trend = True
            continue
        if in_trend and line.startswith("====="):
            break
        if not in_trend:
            continue
        m = re.match(
            r"(\d{4}-\d{2}-\d{2})\s+([0-9.]+)%\s+(\S+)\s+(\S+)\s+(\d+/\d+)\s+([0-9.]+)",
            line.strip(),
        )
        if m:
            trend.append(
                {
                    "date": m.group(1),
                    "temp_pct": float(m.group(2)),
                    "lamp": f"{m.group(3)} {m.group(4)}",
                    "consensus": m.group(5),
                    "score": float(m.group(6)),
                }
            )
    return {
        "asof_date": asof,
        "temp_pct": temp_pct,
        "lamp": lamp,
        "consensus": consensus,
        "score": score,
        "trend": trend[:14],
    }


def build_thermo_payload() -> dict[str, Any]:
    stamped = _latest_glob(ROOT / "reports/daily", "*_crash_thermometer.md")
    path = THERMO_STABLE if THERMO_STABLE.is_file() else stamped
    if path is None or not path.is_file():
        return {
            "schema": "ops-thermo-v1",
            "title": "大跌溫度計",
            "present": False,
            "note": "尚無 reports/daily/crash_thermometer.md",
        }
    body = path.read_text(encoding="utf-8", errors="replace")
    parsed = _parse_thermo_md(body)
    return {
        "schema": "ops-thermo-v1",
        "title": "大跌溫度計",
        "present": True,
        "path": str(path.relative_to(ROOT)),
        "mtime": _mtime_iso(path),
        **parsed,
        "body_md": body[:12000],
    }


def build_branches_payload() -> dict[str, Any]:
    watch = build_watch_payload()
    evening = watch.get("evening") or {}
    second = watch.get("second_disp") or {}
    pools = watch.get("expert_pools") or []
    fired_labels = [f.get("label") for f in (evening.get("fires") or []) if f.get("label")]
    return {
        "schema": "ops-branches-v1",
        "title": "分點／處置／專家池",
        "n_expert_pools": len(pools),
        "evening_asof": evening.get("asof"),
        "evening_fires": evening.get("fires") or [],
        "n_evening_fires": evening.get("n_fires") or 0,
        "fired_labels": fired_labels,
        "second_disp": {
            "asof": second.get("asof"),
            "n_hits": second.get("n_hits"),
            "n_remind": second.get("n_remind"),
            "note": second.get("note"),
            "hits_preview": (second.get("hits") or [])[:15],
            "present": second.get("present"),
        },
        "tier_a_entry_n": len((watch.get("tier_a") or {}).get("entry_watch") or []),
        "sources": watch.get("sources") or [],
        "preview_md": evening.get("preview_md"),
    }


def build_today_payload() -> dict[str, Any]:
    watch = build_watch_payload()
    risk = build_risk_payload()
    thermo = build_thermo_payload()
    return {
        "schema": "ops-today-v1",
        "title": "Today 總覽",
        "watch_fires": (watch.get("evening") or {}).get("n_fires"),
        "watch_asof": (watch.get("evening") or {}).get("asof"),
        "risk_level": risk.get("level"),
        "risk_severity": risk.get("severity"),
        "thermo_temp_pct": thermo.get("temp_pct"),
        "thermo_lamp": thermo.get("lamp"),
        "thermo_asof": thermo.get("asof_date"),
        "n_expert_pools": watch.get("n_expert_pools"),
        "links": {
            "watch": "/watch",
            "risk": "/risk",
            "thermo": "/thermo",
            "branches": "/branches",
        },
    }


BUILDERS: dict[str, Any] = {
    "watch": build_watch_payload,
    "risk": build_risk_payload,
    "thermo": build_thermo_payload,
    "branches": build_branches_payload,
    "today": build_today_payload,
}

KINDS = tuple(BUILDERS.keys())
