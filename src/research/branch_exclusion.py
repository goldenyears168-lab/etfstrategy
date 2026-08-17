"""造市／綜合流量分點排除清單的單一載入點（research only）。

SSOT 檔：`reports/research/branch-footprint-screen/market_maker_branch_exclusion_v1.json`
（檔名固定停在 `_v1`，實際版本看檔內 `version` 欄位；v1.2 起 n=22）。

**為什麼需要這個模組**：清單原本被 4 支腳本各自用 3 行 `json.loads(...)["symbols"]`
讀取，而真正在跑分點比較的框架（`research.backtest.branch_copytrade_fair_compare`）
根本沒讀它。結果是同一個結論被重複推導了三次——

  * `whale-branch-l1h7-discovery`（2026-07）：8440／1650 全母體判顯著負向 p<1e-4
  * `foreign-desk-universe-bakeoff`（2026-07-25）：H-U2 摩通／摩士 lift 為負，
    結論「分點 alpha 應回本土專家池而非外資席賽馬」
  * `foreign-branch-copytrade`（2026-08-17）：10 支外資分點全掃，再次全滅

所以任何分點候選篩選都應該在**建立候選池之前**先問這裡，而不是等跑出負向或
邊緣訊號才發現。清單是**警示而非硬擋**——重測既有成員是合法研究行為，只是
必須是有意識的選擇。
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from report_paths import REPORTS_RESEARCH

EXCLUSION_PATH = (
    REPORTS_RESEARCH / "branch-footprint-screen" / "market_maker_branch_exclusion_v1.json"
)


@lru_cache(maxsize=1)
def load_exclusion(path: str | None = None) -> dict:
    """讀入完整清單 JSON（含 methodology_note／criterion_v2／provenance）。"""
    p = Path(path) if path else EXCLUSION_PATH
    return json.loads(p.read_text(encoding="utf-8"))


def exclusion_index(path: str | None = None) -> dict[str, dict]:
    """trader_id → symbol record。"""
    return {s["trader_id"]: s for s in load_exclusion(path)["symbols"]}


def excluded_trader_ids(path: str | None = None) -> set[str]:
    """僅取 trader_id 集合（等價於各腳本原本自行手刻的 loader）。"""
    return set(exclusion_index(path))


def is_excluded(trader_id: str, path: str | None = None) -> bool:
    return str(trader_id) in exclusion_index(path)


def check_trader_ids(
    trader_ids: list[str] | tuple[str, ...], path: str | None = None
) -> list[dict]:
    """回傳其中已被列入排除的成員紀錄（保持傳入順序）。"""
    idx = exclusion_index(path)
    return [idx[str(t)] for t in trader_ids if str(t) in idx]


def format_warning(records: list[dict], path: str | None = None) -> str:
    """把命中的排除成員整理成一段可直接 print 到 stderr 的警示文字。"""
    if not records:
        return ""
    meta = load_exclusion(path)
    lines = [
        f"⚠️  {len(records)} 支分點已在造市／綜合流量排除清單內"
        f"（{meta.get('version')} · n={meta.get('n')}）：",
    ]
    for r in records:
        lines.append(
            f"    {r['trader_id']} {r.get('name', '')} [{r.get('branch_type', '')}] "
            f"hhi_res={r.get('hhi_res')}"
        )
        note = str(r.get("note") or "").strip()
        if note:
            lines.append(f"        {note[:160]}{'…' if len(note) > 160 else ''}")
    lines.append(
        "    清單語意是警示不是硬擋；若這次就是要重測既有成員，請在報告 notes 寫明理由。"
    )
    lines.append(f"    SSOT: {EXCLUSION_PATH}")
    return "\n".join(lines)
