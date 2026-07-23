"""Build whale event calendar from expert_pool knowledge trades."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

TIER_A = ["2327", "2303", "3443", "6669", "2383", "2408", "3189", "3037"]
TIER_B = ["2337", "3481", "3653", "6239", "6223", "3665", "6515"]
ROOT_DEFAULT = Path(__file__).resolve().parents[3]
TRADES_DIR = (
    ROOT_DEFAULT
    / "reports/research/branch-footprint-screen/expert_pool/knowledge/trades"
)


def load_whale_events(
    stock_ids: list[str] | None = None,
    trades_dir: Path | None = None,
) -> pd.DataFrame:
    sids = stock_ids or TIER_A
    tdir = Path(trades_dir or TRADES_DIR)
    rows: list[dict] = []
    for sid in sids:
        path = tdir / f"{sid}.json"
        if not path.exists():
            continue
        doc = json.loads(path.read_text(encoding="utf-8"))
        proto = doc.get("protocol") or {}
        for tr in doc.get("trades") or []:
            rows.append(
                {
                    "sid": sid,
                    "name": doc.get("stock_name"),
                    "tier": doc.get("tier"),
                    "signal_date": str(tr.get("signal_date")),
                    "yday": str(tr.get("yday") or ""),
                    "entry_date_l1": str(tr.get("entry_date") or ""),
                    "exit_date_l1": str(tr.get("exit_date") or ""),
                    "hold_bars_recorded": tr.get("hold_bars"),
                    "r_adj_pct_l1": tr.get("r_adj_pct"),
                    "r_pct_l1": tr.get("r_pct"),
                    "mode": proto.get("mode"),
                    "floor": proto.get("floor"),
                    "floor_amt": proto.get("floor_amt"),
                    "policy": proto.get("policy"),
                    "thin": bool(doc.get("thin")),
                }
            )
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values(["sid", "signal_date"]).reset_index(drop=True)
    return df
