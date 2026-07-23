"""Top10 light2 yellow · research email annotation only (not Order entry).

Yellow = continuous 2 trading days, each with ≥2 Top10 *light* branches,
and neither day has any Top10 ≥1億. Predicts confirm ≥0.5億 / first heavy onset.

Stats frozen from:
  reports/.../expert_pool/TOP10_PRECURSOR_TIGHTEN.md (thick_h3 light2)
  reports/.../expert_pool/hypotheses/H_A_confirm05_enter.md (≤2d confirm)

Enable via env EXPERT_POOL_YELLOW_EMAIL_ANNOTATION=1 or CLI --yellow-annotation.
Default OFF — never opens positions.
"""
from __future__ import annotations

from typing import Any

# Frozen Top10 (H_D / TOP10_PRECURSOR_TIGHTEN SSOT)
TOP10_TIDS: tuple[str, ...] = (
    "5850",
    "779Z",
    "779c",
    "9216",
    "9A81",
    "8840",
    "5854",
    "9268",
    "9227",
    "1260",
)

LIGHT_LO = 5_000_000.0  # 500萬
LIGHT_HI = 50_000_000.0  # 0.5億 (exclusive upper for light band)
HEAVY = 100_000_000.0  # 1億

# H_A confirm_Top10 ≤2d ≈67%; thick light2 →≥0.5億@≤3d ≈83% (tighten report)
HIST_CONFIRM_LE2D_PCT = 67
HIST_ONSET_1_5D_PCT = 35  # light2 → quiet5 onset@1–5 ≈34–37%

# Legacy watch pools missing hard_n in registry but in H_D thick universe
_LEGACY_THICK_SIDS = frozenset({"2344", "8046", "8358"})

SOURCE = "finmind"


def flag_enabled(*, cli: bool = False) -> bool:
    """True only when CLI set or env truthy. Default false."""
    if cli:
        return True
    import os

    raw = os.environ.get("EXPERT_POOL_YELLOW_EMAIL_ANNOTATION", "0").strip()
    return raw not in ("", "0", "false", "False", "no", "NO")


def eligible_sid(sid: str, registry: dict[str, dict] | None) -> bool:
    """adopted ∩ (hard_n≥3 | legacy thick fallback)."""
    row = (registry or {}).get(sid)
    if not row:
        return sid in _LEGACY_THICK_SIDS
    status = str(row.get("status") or "")
    if status and status != "adopted":
        return False
    hard = row.get("hard_n")
    if hard is None:
        return sid in _LEGACY_THICK_SIDS
    try:
        return int(hard) >= 3
    except (TypeError, ValueError):
        return False


def _day_light_heavy(
    events: list[dict],
) -> tuple[int, bool, list[dict]]:
    """Return (n_light, has_heavy, light_branch_rows)."""
    lights: list[dict] = []
    heavy = False
    for e in events:
        amt = float(e.get("net_amt") or 0.0)
        if amt >= HEAVY:
            heavy = True
        elif LIGHT_LO <= amt < LIGHT_HI:
            lights.append(e)
    return len(lights), heavy, lights


def top10_nets_on(
    conn: Any,
    *,
    stock_id: str,
    trade_date: str,
    close: float | None,
) -> list[dict]:
    if close is None or close <= 0:
        return []
    placeholders = ",".join("?" * len(TOP10_TIDS))
    rows = conn.execute(
        f"""
        SELECT securities_trader_id, securities_trader, net
        FROM stock_broker_branch_daily
        WHERE stock_id = ? AND trade_date = ? AND source = ?
          AND securities_trader_id IN ({placeholders})
          AND net > 0
        """,
        (stock_id, trade_date, SOURCE, *TOP10_TIDS),
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        net = float(r["net"])
        tid = str(r["securities_trader_id"])
        out.append(
            {
                "tid": tid,
                "name": str(r["securities_trader"] or tid),
                "net": net,
                "net_amt": net * close,
                "close": close,
                "date": trade_date,
            }
        )
    return sorted(out, key=lambda x: -x["net_amt"])


def detect_top10_light2_yellow(
    conn: Any,
    *,
    stock_id: str,
    asof: str,
    yday: str | None,
    close_asof: float | None,
    close_yday: float | None,
) -> dict | None:
    """Return yellow payload if light2 fires; else None."""
    if not yday:
        return None
    today = top10_nets_on(conn, stock_id=stock_id, trade_date=asof, close=close_asof)
    prev = top10_nets_on(conn, stock_id=stock_id, trade_date=yday, close=close_yday)
    n_t, heavy_t, lights_t = _day_light_heavy(today)
    n_y, heavy_y, lights_y = _day_light_heavy(prev)
    if heavy_t or heavy_y:
        return None
    if n_t < 2 or n_y < 2:
        return None
    return {
        "rule": "top10_light2_yellow",
        "asof": asof,
        "yday": yday,
        "n_light_asof": n_t,
        "n_light_yday": n_y,
        "lights_asof": lights_t,
        "lights_yday": lights_y,
    }


def format_annotation_lines(yellow: dict | None) -> list[str]:
    """One research-only block; never phrased as a buy."""
    if not yellow:
        return []
    n_t = yellow.get("n_light_asof")
    n_y = yellow.get("n_light_yday")
    return [
        "—— 研究註記·輕量共識黃燈（非買訊 · 非自動下單）——",
        (
            f"Top10 連續2日各≥2家輕量（今{n_t}/昨{n_y}）· 兩日皆無≥1億"
        ),
        (
            f"歷史上≈{HIST_CONFIRM_LE2D_PCT}% 於≤2日達半億"
            f"（厚池 light2 · H_A）；≈{HIST_ONSET_1_5D_PCT}% 於1–5日見首次重本 onset"
            "（誤報仍過半）"
        ),
        "用途：提高警覺／等半億或 expert 綠燈確認 · 不當進場觸發",
        "",
    ]
