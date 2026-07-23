#!/usr/bin/env python3
"""Tier A independent entry / seat-exit snippets for evening digest (observe only)."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
CFG_PATH = ROOT / "config" / "tier_a_evening_watch.json"
YI = 1e8


def load_cfg(path: Path = CFG_PATH) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _close(conn: sqlite3.Connection, sid: str, d: str) -> float | None:
    row = conn.execute(
        """
        SELECT close FROM stock_daily_bars
        WHERE stock_id=? AND trade_date=?
        """,
        (sid, d),
    ).fetchone()
    if not row or row[0] is None:
        return None
    return float(row[0])


def _prev_day(conn: sqlite3.Connection, d: str) -> str | None:
    row = conn.execute(
        """
        SELECT MAX(trade_date) FROM stock_daily_bars
        WHERE trade_date < ?
        """,
        (d,),
    ).fetchone()
    return str(row[0]) if row and row[0] else None


def _seat_rows(
    conn: sqlite3.Connection, sid: str, d: str, seats: list[str], source: str = "finmind"
) -> dict[str, dict]:
    if not seats:
        return {}
    ph = ",".join("?" * len(seats))
    df = pd.read_sql_query(
        f"""
        SELECT securities_trader_id AS tid, securities_trader AS tname,
               buy, sell, net
        FROM stock_broker_branch_daily
        WHERE source=? AND stock_id=? AND trade_date=?
          AND securities_trader_id IN ({ph})
        """,
        conn,
        params=[source, sid, d, *seats],
    )
    close = _close(conn, sid, d) or 0.0
    out: dict[str, dict] = {t: {"net": 0.0, "buy": 0.0, "sell": 0.0, "amt": 0.0, "sell_amt": 0.0, "name": t} for t in seats}
    if df.empty:
        return out
    for r in df.itertuples():
        tid = str(r.tid)
        net = float(r.net or 0)
        buy = float(r.buy or 0)
        sell = float(r.sell or 0)
        out[tid] = {
            "net": net,
            "buy": buy,
            "sell": sell,
            "amt": net * close,
            "sell_amt": sell * close,
            "name": str(r.tname or tid),
        }
    return out


def _bh_chg(conn: sqlite3.Connection, sid: str, d: str) -> float | None:
    """Prefer research cache; else skip (None)."""
    cache = ROOT / "reports/research/whale_chip_precursor/cache/holding_shares_per_tier_a.csv"
    if not cache.exists():
        return None
    try:
        h = pd.read_csv(cache)
    except OSError:
        return None
    h["sid"] = h["sid"].astype(str)
    h["d"] = h["d"].astype(str)
    sub = h[h["sid"] == sid].sort_values("d")
    if sub.empty or "big_holder_pct_chg" not in sub.columns:
        return None
    # asof backward
    sub = sub[sub["d"] <= d]
    if sub.empty:
        return None
    v = sub.iloc[-1]["big_holder_pct_chg"]
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _three_streak(conn: sqlite3.Connection, sid: str, d: str) -> float | None:
    """Optional: from chip feature if available via institutional streaks — skip if heavy."""
    # Keep evening path light: only used by exit-related 6669 entry which is NOT in entry_watch.
    return None


def eval_entry(conn: sqlite3.Connection, spec: dict, asof: str) -> dict:
    sid = spec["sid"]
    yday = _prev_day(conn, asof)
    seats = list(spec["seats"])
    today = _seat_rows(conn, sid, asof, seats)
    ymap = _seat_rows(conn, sid, yday, seats) if yday else {t: {"amt": 0.0} for t in seats}
    kind = spec["kind"]
    fired = False
    detail_parts: list[str] = []

    if kind == "accel_0p5_bh":
        tid = seats[0]
        a0 = float(today[tid]["amt"])
        a1 = float(ymap[tid].get("amt", 0.0))
        floor = float(spec.get("floor_yi", 0.5)) * YI
        bh = _bh_chg(conn, sid, asof)
        bh_ok = bh is not None and bh > 0
        fired = (a1 > 0) and (a0 > a1) and (a0 >= floor) and bh_ok
        detail_parts.append(
            f"{tid} amt_D={a0/YI:.2f}億 amt_D-1={a1/YI:.2f}億 floor={floor/YI:.1f}億 "
            f"bhchg={bh if bh is not None else 'NA'}"
        )
    elif kind == "bh_seat_accel":
        tid = seats[0]
        a0 = float(today[tid]["amt"])
        a1 = float(ymap[tid].get("amt", 0.0))
        bh = _bh_chg(conn, sid, asof)
        bh_ok = bh is not None and bh > 0
        fired = bh_ok and (a1 > 0) and (a0 > a1)
        detail_parts.append(
            f"bhchg={bh if bh is not None else 'NA'} · {tid} amt_D={a0/YI:.2f}億 amt_D-1={a1/YI:.2f}億"
        )
    elif kind == "cobuy_both_net":
        a, b = seats[0], seats[1]
        fired = (
            float(today[a]["amt"]) > 0
            and float(today[b]["amt"]) > 0
            and float(ymap[a].get("amt", 0.0)) > 0
            and float(ymap[b].get("amt", 0.0)) > 0
        )
        detail_parts.append(
            f"{a}:{today[a]['amt']/YI:.2f}/{ymap[a].get('amt',0)/YI:.2f}億 · "
            f"{b}:{today[b]['amt']/YI:.2f}/{ymap[b].get('amt',0)/YI:.2f}億 (D / D-1)"
        )
    else:
        detail_parts.append(f"unknown kind={kind}")

    hold_h = int(spec.get("hold_h") or 7)
    role = str(spec.get("role") or "observe")
    extra = ""
    if sid == "2408":
        extra = (
            f"凍結：主出場=固定 H{hold_h}（深研 robust_win vs Whale L1H7）· "
            "賣訊僅次要黃燈，非主 alpha\n"
        )
    return {
        "id": f"indep-entry-{sid}",
        "sid": sid,
        "title": f"獨立進場 · {spec['name']}（{sid}）",
        "asof": asof,
        "fired": bool(fired),
        "summary": (f"觸發→明日L1 · 持有H{hold_h}" if fired else "未觸發"),
        "body": (
            f"{'【有訊號】' if fired else '【今日無訊號】'}獨立進場 · {spec['name']}（{sid}）· {asof}\n"
            f"規則：{spec['label']}\n"
            f"明細：{'; '.join(detail_parts)}\n"
            f"持有：L1 進場後約 H={hold_h} 交易日收盤檢視（role={role}）\n"
            f"{extra}"
            f"用法：Research 觀測 · 未採納 · 不下單 · 與專家池共識分開看\n"
        ),
    }


def eval_exit(conn: sqlite3.Connection, spec: dict, asof: str) -> dict:
    sid = spec["sid"]
    seats = list(spec["seats"])
    today = _seat_rows(conn, sid, asof, seats)
    kind = spec["kind"]
    fired = False
    detail_parts: list[str] = []

    if kind == "hold_calendar":
        hold_h = int(spec.get("hold_h") or 10)
        # Always list as reminder; "fired" marks primary policy presence for digest sorting.
        fired = True
        detail_parts.append(
            f"主出場政策=固定 H{hold_h}：自獨立 L1 進場日起算第 {hold_h} 個交易日收盤檢視／減倉觀察"
        )
    elif kind == "sell_ge_1yi":
        tid = seats[0]
        sa = float(today[tid]["sell_amt"])
        floor = float(spec.get("floor_yi", 1.0)) * YI
        fired = sa >= floor
        detail_parts.append(f"{tid} sell_amt={sa/YI:.2f}億（門檻 {floor/YI:.1f}億） amt={today[tid]['amt']/YI:.2f}億")
    elif kind == "both_flip":
        a, b = seats[0], seats[1]
        fired = float(today[a]["amt"]) < 0 and float(today[b]["amt"]) < 0
        detail_parts.append(
            f"{a} amt={today[a]['amt']/YI:.2f}億 · {b} amt={today[b]['amt']/YI:.2f}億"
        )
    else:
        detail_parts.append(f"unknown kind={kind}")

    prio = str(spec.get("priority") or "normal")
    title_prefix = "持有日曆" if kind == "hold_calendar" else "分點出場觀察"
    return {
        "id": f"seat-exit-{sid}-{spec.get('rule_id', kind)}",
        "sid": sid,
        "title": f"{title_prefix} · {spec['name']}（{sid}）",
        "asof": asof,
        "fired": bool(fired),
        "summary": (
            "H* 日曆提醒"
            if kind == "hold_calendar"
            else ("轉賣／轉弱觸發" if fired else "未觸發（仍列示）")
        ),
        "body": (
            f"{'【有訊號】' if fired else '【今日無訊號】'}{title_prefix} · {spec['name']}（{sid}）· {asof}\n"
            f"規則：{spec['label']}\n"
            f"明細：{'; '.join(detail_parts)}\n"
            f"優先：{prio}\n"
            f"用法：無倉也刷 · Research 觀測 · 未採納 · 不下單\n"
        ),
    }


def collect_sections(conn: sqlite3.Connection, asof: str, cfg: dict | None = None) -> list[dict]:
    cfg = cfg or load_cfg()
    sections: list[dict] = []
    sections.append(
        {
            "id": "tier-a-banner",
            "title": "Tier A 獨立規則（增量）",
            "asof": asof,
            "fired": False,
            "summary": "下列進場／出場與專家池分開",
            "body": (
                f"—— Tier A 獨立增量 · {asof} ——\n"
                "進場觀察：3037／2408／2383\n"
                "2408 南亞科：主凍結 = 進場規則 + 固定 H10（非賣訊主出場）\n"
                "出場／持有：2408 H10 日曆＋賣超次要黃燈；2327／6669 僅觀察\n"
                "性質：Research · 未採納 · 不下單\n"
            ),
        }
    )
    for spec in cfg.get("entry_watch", []):
        try:
            sections.append(eval_entry(conn, spec, asof))
        except Exception as exc:  # noqa: BLE001
            sections.append(
                {
                    "id": f"indep-entry-{spec.get('sid')}",
                    "title": f"獨立進場 · {spec.get('name')}",
                    "asof": asof,
                    "fired": False,
                    "summary": f"ERROR {exc}",
                    "body": f"【錯誤】獨立進場 {spec}: {exc}",
                }
            )
    for spec in cfg.get("exit_watch", []):
        try:
            sections.append(eval_exit(conn, spec, asof))
        except Exception as exc:  # noqa: BLE001
            sections.append(
                {
                    "id": f"seat-exit-{spec.get('sid')}",
                    "title": f"分點出場 · {spec.get('name')}",
                    "asof": asof,
                    "fired": False,
                    "summary": f"ERROR {exc}",
                    "body": f"【錯誤】分點出場 {spec}: {exc}",
                }
            )
    return sections
