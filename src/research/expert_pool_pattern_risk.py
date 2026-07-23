"""Expert-pool T-day pattern risk · research email annotation only.

At 20:00 (signal day T) we can only score **T-day** morphology / branch size.
T+1 open / 5m / 25m belong to the next-morning staged funnel — not this module.

Freeze (from avoid_morphology H_CROSS / H_COMBO / H_BRANCH / H_PER_STOCK · 2026-07-23):
  - HOT5: ret5≥20% · cross explore (OOS unstable alone)
  - T_REJECT: (black ∧ close_pos<0.33) OR body<−3% · 2303 significant; pool weak
  - MAX_TODAY≥5億 · cross branch-structure arm
  - Stock notes: only emit when that sid has research support; else omit

Never blocks email · never Order entry.
"""
from __future__ import annotations

from typing import Any

SOURCE = "finmind"
HOT5_RET = 0.20
WEAK_POS = 0.33
BODY_REJECT = -0.03
MAX_SEAT_YI = 5.0  # 億

# sid → stock-specific flags allowed in email (others stay cross-only)
_STOCK_EXTRA: dict[str, tuple[str, ...]] = {
    "2303": ("T_REJECT", "HOT5"),
    "2344": ("HOT5", "MAX_TODAY_5YI"),
    "6515": ("T_WEAK_CLOSE",),  # direction-only · label as 未顯著
    "2327": ("T_BODY_LT3",),  # direction-only · 未顯著
    # 8046: THREE_TODAY_BIG_YANG reversed in-sample → do not emit
}


def _hist_bars(conn: Any, stock_id: str, asof: str, n: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT trade_date, open, high, low, close
        FROM stock_daily_bars
        WHERE stock_id = ? AND source = ? AND trade_date <= ? AND close > 0
        ORDER BY trade_date DESC
        LIMIT ?
        """,
        (stock_id, SOURCE, asof, n),
    ).fetchall()
    out = []
    for r in reversed(rows):
        out.append(
            {
                "trade_date": str(r[0]),
                "open": float(r[1]),
                "high": float(r[2]),
                "low": float(r[3]),
                "close": float(r[4]),
            }
        )
    return out


def _t_features(bars: list[dict]) -> dict[str, Any] | None:
    if len(bars) < 2:
        return None
    t = bars[-1]
    prev5 = bars[-6:-1] if len(bars) >= 6 else []
    o, h, l, c = t["open"], t["high"], t["low"], t["close"]
    rng = h - l
    close_pos = (c - l) / rng if rng > 0 else None
    body = c / o - 1 if o else None
    black = c < o
    ret5 = None
    if len(prev5) == 5 and prev5[0]["close"] > 0:
        ret5 = prev5[-1]["close"] / prev5[0]["close"] - 1
    return {
        "asof": t["trade_date"],
        "black": black,
        "close_pos": close_pos,
        "body": body,
        "ret5": ret5,
        "ret_t": (c / bars[-2]["close"] - 1) if bars[-2]["close"] else None,
    }


def detect_pattern_risk(
    conn: Any,
    *,
    stock_id: str,
    asof: str,
    today_core_events: list[dict] | None = None,
) -> dict[str, Any] | None:
    """Return risk payload if any frozen T-day flag fires; else None."""
    bars = _hist_bars(conn, stock_id, asof, 30)
    if not bars or bars[-1]["trade_date"] != asof[:10]:
        return None
    feat = _t_features(bars)
    if not feat:
        return None

    flags: list[dict[str, str]] = []
    ret5 = feat.get("ret5")
    if ret5 is not None and ret5 >= HOT5_RET:
        flags.append(
            {
                "id": "HOT5",
                "level": "explore",
                "text": (
                    f"HOT5：訊號前5日漲 {ret5*100:+.1f}%≥+20%"
                    "（全池探索；時間OOS不穩 · 華邦方向對）"
                ),
            }
        )

    close_pos = feat.get("close_pos")
    body = feat.get("body")
    black = bool(feat.get("black"))
    t_reject = (black and close_pos is not None and close_pos < WEAK_POS) or (
        body is not None and body < BODY_REJECT
    )
    if t_reject:
        level = "sid_strong" if stock_id == "2303" else "explore"
        note = (
            "聯電本股顯著"
            if stock_id == "2303"
            else "全池效應弱 · 僅作警覺"
        )
        flags.append(
            {
                "id": "T_REJECT",
                "level": level,
                "text": (
                    f"T_REJECT：收黑={black} close_pos="
                    f"{close_pos if close_pos is not None else float('nan'):.2f} "
                    f"body={(body or 0)*100:+.1f}%（{note}）"
                ),
            }
        )

    if (
        stock_id == "6515"
        and black
        and close_pos is not None
        and close_pos < 0.20
    ):
        flags.append(
            {
                "id": "T_WEAK_CLOSE",
                "level": "sid_weak",
                "text": (
                    f"T弱收：close_pos={close_pos:.2f}（穎崴方向對 · 本股未達顯著）"
                ),
            }
        )

    if stock_id == "2327" and body is not None and body < BODY_REJECT:
        flags.append(
            {
                "id": "T_BODY_LT3",
                "level": "sid_weak",
                "text": (
                    f"T body={body*100:+.1f}%<−3%（國巨方向對 · 本股未達顯著）"
                ),
            }
        )

    max_yi = 0.0
    max_name = ""
    for e in today_core_events or []:
        yi = float(e.get("net_amt") or 0.0) / 1e8
        if yi > max_yi:
            max_yi = yi
            max_name = str(e.get("name") or e.get("tid") or "")
    if max_yi >= MAX_SEAT_YI:
        flags.append(
            {
                "id": "MAX_TODAY_5YI",
                "level": "cross",
                "text": (
                    f"單席今買 {max_name} {max_yi:.1f}億≥5億"
                    "（分點結構跨組臂 · Δmed約−3.7pp）"
                ),
            }
        )

    # Deduplicate by id (stock extras may overlap T_REJECT)
    seen: set[str] = set()
    uniq: list[dict[str, str]] = []
    for f in flags:
        if f["id"] in seen:
            continue
        # Drop stock-extra that isn't allowed for this sid (HOT5 always ok)
        allowed = _STOCK_EXTRA.get(stock_id)
        if f["id"] in ("T_WEAK_CLOSE", "T_BODY_LT3") and (
            not allowed or f["id"] not in allowed
        ):
            continue
        seen.add(f["id"])
        uniq.append(f)

    if not uniq:
        return None
    return {
        "asof": asof[:10],
        "stock_id": stock_id,
        "flags": uniq,
        "features": {
            "ret5": feat.get("ret5"),
            "black": feat.get("black"),
            "close_pos": feat.get("close_pos"),
            "body": feat.get("body"),
            "max_today_yi": max_yi,
        },
    }


def format_pattern_risk_lines(payload: dict[str, Any] | None) -> list[str]:
    """Research-only block: 共識有、圖形有風險."""
    if not payload or not payload.get("flags"):
        return []
    lines = [
        "—— 研究註記·專家有共識但圖形有風險（非自動擋單）——",
        "主訊號仍寄出；下列為 T 日形態／分點金額警覺（研究凍結 · 未採納 Order）：",
    ]
    for f in payload["flags"]:
        lines.append(f"  · [{f.get('level','?')}] {f['text']}")
    lines.append(
        "建議：降權或改限價；次日再看開盤／09:05／09:25 漏斗確認 · 不當取消共識"
    )
    lines.append("")
    return lines
