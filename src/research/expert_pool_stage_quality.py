"""Weinstein Stage 2 品質分層 · research email annotation only (not Order entry).

觀察／品質分層／確認：在專家池 watch 綠燈信附加「技術面（Weinstein weekly stage）」
品質標記 —— Stage 2（advancing）視為確認（S2_confirm），Stage 1/3/4 視為降權
（non_S2_deweight）。純觀察註解，不影響主訊號、不改寄信條件、不當進出場門檻。

Enable via env EXPERT_POOL_STAGE_QUALITY=0 to disable; default ON.
"""
from __future__ import annotations

import os
from typing import Any

import pandas as pd

from stage_analysis import STAGE_NAMES, classify_weinstein_stage

SOURCE = "finmind"
MIN_DAILY_BARS = 200
STAGE_LOOKBACK_DAYS = 300  # ≈60 weekly bars · buffer above classify_weinstein_stage's 42-week floor

_DEWEIGHT_STAGES = frozenset({1, 3, 4})


def flag_enabled() -> bool:
    """True unless EXPERT_POOL_STAGE_QUALITY explicitly falsy. Default on."""
    raw = os.environ.get("EXPERT_POOL_STAGE_QUALITY", "1").strip()
    return raw not in ("0", "false", "False", "no", "NO")


def _daily_bars(conn: Any, stock_id: str, asof: str, *, limit: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT trade_date, open, high, low, close
        FROM stock_daily_bars
        WHERE stock_id = ? AND source = ? AND trade_date <= ? AND close > 0
        ORDER BY trade_date DESC
        LIMIT ?
        """,
        (stock_id, SOURCE, asof, max(1, limit)),
    ).fetchall()
    out: list[dict] = []
    for r in reversed(rows):
        out.append(
            {
                "date": str(r[0]),
                "open": float(r[1]),
                "high": float(r[2]),
                "low": float(r[3]),
                "close": float(r[4]),
            }
        )
    return out


def _note_zh(
    *,
    stage: int,
    stage_name: str,
    ta_quality: str,
    extension_pct: float | None,
    insufficient: bool,
    ix_stage: int | None = None,
    dual_s2: bool | None = None,
) -> str:
    ext_s = f"{extension_pct:+.1f}%" if isinstance(extension_pct, (int, float)) else "—"
    if ta_quality == "S2_confirm":
        base = (
            f"技術品質（Weinstein）Stage {stage}（{stage_name}）· 相對30週均線 {ext_s}"
            " · S2_confirm（週線第2階／確認）"
        )
        if dual_s2 is True:
            return base + " · dual_S2（個股∧大盤同為 Stage2／E5 強化）"
        if dual_s2 is False and ix_stage is not None:
            return (
                base
                + f" · 大盤 Stage {ix_stage} 非同步（E5 觀察：跨股研究建議降權，非擋單）"
            )
        return base
    if ta_quality == "non_S2_deweight":
        return (
            f"技術品質（Weinstein）Stage {stage}（{stage_name}）· 相對30週均線 {ext_s}"
            " · non_S2_deweight（非 Stage 2／建議降權）"
        )
    reason = "週線資料不足" if insufficient else "尚無明確階段"
    return f"技術品質（Weinstein）Stage {stage}（{stage_name}）· unknown（{reason}）"


def _ix_daily_bars(conn: Any, asof: str, *, limit: int) -> list[dict]:
    """IX0001 daily OHLC for Weinstein（yahoo→tej→finmind 優先，與專案慣例一致）。"""
    rows = conn.execute(
        """
        SELECT date, open, high, low, close FROM daily_bars
        WHERE code='IX0001' AND date<=? AND close>0
          AND open IS NOT NULL AND high IS NOT NULL AND low IS NOT NULL
        ORDER BY date DESC,
          CASE source WHEN 'yahoo' THEN 0 WHEN 'tej' THEN 1 WHEN 'finmind' THEN 2 ELSE 3 END
        LIMIT ?
        """,
        (asof, max(1, limit)),
    ).fetchall()
    # dedupe by date keep first (best source)
    seen: set[str] = set()
    out: list[dict] = []
    for r in rows:
        d = str(r[0])[:10]
        if d in seen:
            continue
        seen.add(d)
        out.append(
            {
                "date": d,
                "open": float(r[1]),
                "high": float(r[2]),
                "low": float(r[3]),
                "close": float(r[4]),
            }
        )
    out.reverse()
    return out


def detect_stage_quality(
    conn: Any,
    *,
    stock_id: str,
    asof: str,
) -> dict | None:
    """Weekly Weinstein stage → S2 品質分層 payload；None only when no bars at all."""
    bars = _daily_bars(conn, stock_id, asof, limit=STAGE_LOOKBACK_DAYS)
    if not bars:
        return None

    df = pd.DataFrame(bars)
    result = classify_weinstein_stage(df)
    stage = int(result.get("stage") or 0)
    stage_name = str(result.get("stage_name") or STAGE_NAMES.get(stage, "unknown"))
    extension_raw = result.get("extension_pct")
    extension_pct = float(extension_raw) if isinstance(extension_raw, (int, float)) else None

    if stage == 2:
        ta_quality = "S2_confirm"
        follow_weight = "full"
    elif stage in _DEWEIGHT_STAGES:
        ta_quality = "non_S2_deweight"
        follow_weight = "deweight"
    else:
        ta_quality = "unknown"
        follow_weight = "unknown"

    ix_stage: int | None = None
    dual_s2: bool | None = None
    try:
        ix_bars = _ix_daily_bars(conn, asof, limit=STAGE_LOOKBACK_DAYS)
        if len(ix_bars) >= MIN_DAILY_BARS:
            ix_res = classify_weinstein_stage(pd.DataFrame(ix_bars))
            ix_stage = int(ix_res.get("stage") or 0)
            if stage == 2:
                dual_s2 = ix_stage == 2
                if dual_s2 is False:
                    # soft observe: keep S2_confirm label but mark soft deweight hint
                    follow_weight = "soft_deweight"
    except Exception:
        ix_stage = None
        dual_s2 = None

    insufficient = bool(result.get("error")) or len(bars) < MIN_DAILY_BARS
    note_zh = _note_zh(
        stage=stage,
        stage_name=stage_name,
        ta_quality=ta_quality,
        extension_pct=extension_pct,
        insufficient=insufficient,
        ix_stage=ix_stage,
        dual_s2=dual_s2,
    )
    return {
        "weinstein_stage": stage,
        "weinstein_stage_name": stage_name,
        "extension_pct": extension_pct,
        "ta_quality": ta_quality,
        "follow_weight": follow_weight,
        "ix_weinstein_stage": ix_stage,
        "dual_s2_confirm": dual_s2,
        "asof": asof[:10],
        "note_zh": note_zh,
    }


def format_stage_quality_lines(sq: dict | None) -> list[str]:
    """Research-only block；清楚標「觀察註解 · 非下單訊號」。"""
    if not sq:
        return []
    lines = [
        "—— 研究註記·技術品質分層（Weinstein Stage · 觀察註解 · 非下單訊號）——",
        f"  {sq.get('note_zh') or ''}",
    ]
    if sq.get("ix_weinstein_stage") is not None:
        lines.append(
            f"  大盤 IX0001 Weinstein Stage {sq['ix_weinstein_stage']}"
            + (
                " · dual_S2 確認"
                if sq.get("dual_s2_confirm") is True
                else (
                    " · 與個股不同步（E5 觀察）"
                    if sq.get("dual_s2_confirm") is False
                    else ""
                )
            )
        )
    lines += [
        "  用途：技術面品質分層／確認參考 · 不影響主訊號、不改寄信條件、不當進出場門檻",
        "",
    ]
    return lines
