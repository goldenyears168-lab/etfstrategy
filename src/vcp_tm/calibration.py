"""Load VCP-TM calibrated parameters from YAML."""

from __future__ import annotations

from pathlib import Path

import yaml

from dataclasses import fields

from stock_db import PROJECT_ROOT
from vcp_tm.params import VcpTmParams

DEFAULT_CALIBRATION = PROJECT_ROOT / "config" / "vcp_tm_calibrated.yaml"


def load_vcp_tm_params(path: Path | None = None) -> VcpTmParams:
    cal_path = path or DEFAULT_CALIBRATION
    if not cal_path.is_file():
        return VcpTmParams()
    raw = yaml.safe_load(cal_path.read_text(encoding="utf-8")) or {}
    p = raw.get("params") or {}
    known = {f.name for f in fields(VcpTmParams)}
    filtered = {k: v for k, v in p.items() if k in known}
    return VcpTmParams(**filtered)


def load_min_composite(path: Path | None = None, default: float = 65.0) -> float:
    cal_path = path or DEFAULT_CALIBRATION
    if not cal_path.is_file():
        return default
    raw = yaml.safe_load(cal_path.read_text(encoding="utf-8")) or {}
    return float(raw.get("min_composite") or default)
