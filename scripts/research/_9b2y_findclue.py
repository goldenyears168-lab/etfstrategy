#!/usr/bin/env python3
"""找出線索（集中度 0.798 / 當沖度 0.668 / 日筆數 8 / 120 檔）出自哪個快取。"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

D = Path("reports/research/chip-signal-daily-horizon")
for f in sorted(D.glob("branch_*.pkl")):
    if f.stat().st_size > 5_000_000:
        continue
    try:
        d = pd.read_pickle(f)
    except Exception as exc:  # noqa: BLE001
        print(f"{f.name}: 讀不了 {exc}")
        continue
    if not isinstance(d, pd.DataFrame):
        print(f"{f.name}: {type(d)}")
        continue
    hit = None
    for col in d.columns:
        if d[col].dtype == object:
            try:
                if (d[col].astype(str) == "9B2Y").any():
                    hit = col
                    break
            except Exception:  # noqa: BLE001
                pass
    if hit is None and "9B2Y" in map(str, d.index):
        print(f"{f.name}: index 命中 {list(d.columns)}")
        print(d.loc[["9B2Y"]].to_string())
        continue
    if hit:
        print(f"\n### {f.name} (欄 {hit}) shape={d.shape}")
        print(d[d[hit].astype(str) == "9B2Y"].to_string())
