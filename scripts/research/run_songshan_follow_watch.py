#!/usr/bin/env python3
"""分點跟單觀測 · 夜間提醒（observe only · 不下單）.

松山（songshan）觸發（對齊研究冠軍母體）:
  五日累積買進 ≥ 0.5億 ∩ 五日淨比 ≥ 0.95 ∩ !mega
  （`跟單松山·五日累積淨比95`；進場另見 entry_25m_nonfail 提示）
  強度標註（非觸發）：當日或五日 ≥ 1.5億 →「強印」

新店（xindian）觸發（暫維持舊尺）:
  Top1 買超金額 ≥ 1.5億 ∩ !mega

說明（非觸發）: 當下%/25%（25%＝舊研究標籤）

  PYTHONPATH=src python3 scripts/research/run_songshan_follow_watch.py
  PYTHONPATH=src python3 scripts/research/run_songshan_follow_watch.py --branch xindian
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from notify_email import send_alert  # noqa: E402
from project_dotenv import load_project_dotenv  # noqa: E402
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402

SOURCE = "finmind"
# Research champion mother set (Songshan)
RESEARCH_BUY_FLOOR = 0.5e8
RESEARCH_NET_MIN = 0.95
# Legacy / strength badge
STRENGTH_FLOOR = 1.5e8
ABS_FLOOR = STRENGTH_FLOOR  # xindian + share labels
SHARE_STANDARD = 0.25  # research best-practice label; not a fire gate
ENV_FLAG = "RUN_SONGSHAN_FOLLOW_EMAIL"
MEGA_PATH = (
    ROOT
    / "reports"
    / "research"
    / "branch-footprint-screen"
    / "ab58_xMega_copytrade"
    / "mega_blacklist_v1.json"
)
REPORT_DIR = (
    ROOT
    / "reports"
    / "research"
    / "branch-footprint-screen"
    / "ab58_xMega_copytrade"
    / "watch"
)

BRANCH_SPECS: dict[str, dict[str, str]] = {
    "songshan": {
        "id": "9217",
        "name": "凱基-松山",
        "backfill_key": "kgi-songshan",
        "state": "songshan_follow_watch_state.json",
        "rule_tag": "R_5d_net95_xMega",
        "signal_mode": "research_5d",
    },
    "xindian": {
        "id": "9661",
        "name": "富邦-新店",
        "backfill_key": "fubon-xindian",
        "state": "xindian_follow_watch_state.json",
        "rule_tag": "R_1p5_xMega",
        "signal_mode": "top1_1p5",
    },
}

# Backward-compatible defaults (松山)
BRANCH_ID = BRANCH_SPECS["songshan"]["id"]
BRANCH_NAME = BRANCH_SPECS["songshan"]["name"]
STATE_NAME = BRANCH_SPECS["songshan"]["state"]


STOCK_NAMES = {
    "2330": "台積電",
    "2337": "旺宏",
    "6147": "頎邦",
    "2408": "南亞科",
    "2492": "華新科",
    "3006": "晶豪科",
    "2344": "華邦電",
    "3481": "群創",
    "8299": "群聯",
    "3706": "神達",
    "6271": "同欣電",
    "2472": "立隆電",
    "2360": "致茂",
    "2301": "光寶科",
    "6182": "合晶",
}


def _email_enabled() -> bool:
    v = os.environ.get(ENV_FLAG, "1").strip()
    return v not in ("0", "false", "False")


def load_mega() -> set[str]:
    if not MEGA_PATH.exists():
        raise FileNotFoundError(f"missing MEGA list: {MEGA_PATH}")
    raw = json.loads(MEGA_PATH.read_text(encoding="utf-8"))
    return {str(x) for x in raw["symbols"]}


def load_state(path: Path) -> dict:
    if not path.exists():
        return {"notified": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"notified": {}}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = datetime.now().isoformat(timespec="seconds")
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def latest_trading_day(
    conn, *, end: str | None = None, branch_id: str = BRANCH_ID
) -> str | None:
    """Prefer latest branch tape day; fall back to 2330 bars."""
    params: list[object] = [SOURCE, branch_id]
    end_clause = ""
    if end:
        end_clause = " AND trade_date<=?"
        params.append(end)
    row = conn.execute(
        f"""
        SELECT MAX(trade_date) FROM stock_broker_branch_daily
        WHERE source=? AND securities_trader_id=?{end_clause}
        """,
        params,
    ).fetchone()
    branch_d = str(row[0]) if row and row[0] else None

    params2: list[object] = [SOURCE]
    end_clause2 = ""
    if end:
        end_clause2 = " AND trade_date<=?"
        params2.append(end)
    row2 = conn.execute(
        f"""
        SELECT MAX(trade_date) FROM stock_daily_bars
        WHERE stock_id='2330' AND source=? AND open>0 AND close>0{end_clause2}
        """,
        params2,
    ).fetchone()
    bar_d = str(row2[0]) if row2 and row2[0] else None
    candidates = [d for d in (branch_d, bar_d) if d]
    return max(candidates) if candidates else None


def branch_stock_ids(conn, *, asof: str, branch_id: str = BRANCH_ID) -> list[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT stock_id
        FROM stock_broker_branch_daily
        WHERE source=? AND securities_trader_id=? AND trade_date=?
          AND buy>0
          AND length(stock_id)=4
          AND stock_id GLOB '[0-9][0-9][0-9][0-9]'
          AND stock_id NOT GLOB '00*'
        """,
        (SOURCE, branch_id, asof),
    ).fetchall()
    return [str(r[0]) for r in rows]


def refresh_missing_ohlc(conn, *, asof: str, stock_ids: list[str]) -> str:
    """Fetch FinMind TaiwanStockPrice for stocks missing close on asof."""
    from datetime import date as date_cls

    if not stock_ids:
        return "no branch stocks"
    missing: list[str] = []
    for sid in stock_ids:
        row = conn.execute(
            """
            SELECT 1 FROM stock_daily_bars
            WHERE stock_id=? AND trade_date=? AND source=? AND close>0
            LIMIT 1
            """,
            (sid, asof, SOURCE),
        ).fetchone()
        if row is None:
            missing.append(sid)
    row2330 = conn.execute(
        """
        SELECT 1 FROM stock_daily_bars
        WHERE stock_id='2330' AND trade_date=? AND source=? AND close>0 LIMIT 1
        """,
        (asof, SOURCE),
    ).fetchone()
    if row2330 is None and "2330" not in missing:
        missing.append("2330")
    if not missing:
        return "ohlc complete"
    try:
        from finmind_client import fetch_finmind
        from stock_db.market import upsert_stock_daily_bars
    except Exception as exc:  # noqa: BLE001
        return f"ohlc refresh import fail: {exc}"
    d0 = date_cls.fromisoformat(asof)
    ok = 0
    # Prefer larger tape names first (amount proxy = shares when close missing).
    # Cap raised: 5d research needs priced rows or signals silently vanish.
    for sid in missing[:80]:
        try:
            raw = fetch_finmind("TaiwanStockPrice", sid, d0, d0)
            bars = []
            for item in raw or []:
                d = str(item.get("date") or item.get("Date") or "")[:10]
                if d != asof:
                    continue
                o = float(item.get("open") or item.get("Open") or 0)
                c = float(item.get("close") or item.get("Close") or 0)
                if c <= 0:
                    continue
                bars.append(
                    {
                        "stock_id": sid,
                        "trade_date": d,
                        "open": o,
                        "high": float(item.get("max") or item.get("high") or c),
                        "low": float(item.get("min") or item.get("low") or c),
                        "close": c,
                        "volume": float(
                            item.get("Trading_Volume") or item.get("volume") or 0
                        ),
                        "source": SOURCE,
                    }
                )
            if bars:
                upsert_stock_daily_bars(conn, bars)
                ok += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  ohlc {sid} {asof}: FAIL {exc}")
    return f"ohlc refreshed {ok}/{len(missing)} missing"


def top1_on(conn, *, asof: str, branch_id: str = BRANCH_ID) -> dict | None:
    """Amount Top1 for branch on asof; buy_amt = buy×close."""
    row = conn.execute(
        """
        WITH priced AS (
          SELECT b.stock_id,
                 b.buy AS buy_shares,
                 b.buy * p.close AS buy_amt,
                 p.close
          FROM stock_broker_branch_daily b
          JOIN stock_daily_bars p
            ON p.stock_id=b.stock_id
           AND p.trade_date=b.trade_date
           AND p.source=?
          WHERE b.source=?
            AND b.securities_trader_id=?
            AND b.trade_date=?
            AND b.buy>0
            AND p.close>0
            AND length(b.stock_id)=4
            AND b.stock_id GLOB '[0-9][0-9][0-9][0-9]'
            AND b.stock_id NOT GLOB '00*'
        ),
        ranked AS (
          SELECT *,
                 SUM(buy_amt) OVER () AS day_buy_amt,
                 ROW_NUMBER() OVER (ORDER BY buy_amt DESC, stock_id) AS rk
          FROM priced
        )
        SELECT stock_id, buy_shares, buy_amt, close, day_buy_amt,
               buy_amt / day_buy_amt AS buy_share
        FROM ranked
        WHERE rk=1 AND day_buy_amt>0
        """,
        (SOURCE, SOURCE, branch_id, asof),
    ).fetchone()
    if not row:
        return None
    return {
        "stock_id": str(row["stock_id"]),
        "buy_shares": float(row["buy_shares"]),
        "buy_amt": float(row["buy_amt"]),
        "close": float(row["close"]),
        "day_buy_amt": float(row["day_buy_amt"]),
        "buy_share": float(row["buy_share"]),
    }


def try_refresh_branch_tape(
    asof: str,
    *,
    backfill_key: str = "kgi-songshan",
    start: str | None = None,
) -> str:
    """Best-effort FinMind refresh for branch around asof; never fail the watch."""
    try:
        import subprocess

        start_d = start or asof
        cmd = [
            str(ROOT / ".venv" / "bin" / "python"),
            str(ROOT / "scripts" / "research" / "backfill_broker_branch_tape.py"),
            "--branches",
            backfill_key,
            "--start",
            start_d,
            "--end",
            asof,
            "--force",
        ]
        proc = subprocess.run(
            cmd,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
            env={**os.environ, "PYTHONPATH": f"{ROOT}/src"},
        )
        if proc.returncode == 0:
            return f"finmind refresh ok ({start_d}→{asof})"
        tail = (proc.stderr or proc.stdout or "")[-400:]
        return f"finmind refresh rc={proc.returncode}: {tail}"
    except Exception as exc:  # noqa: BLE001
        return f"refresh error: {exc}"


def last_n_trading_days(conn, *, asof: str, n: int = 5) -> list[str]:
    """Last n TW trading days on/before asof (calendar from 2330 bars)."""
    rows = conn.execute(
        """
        SELECT trade_date FROM stock_daily_bars
        WHERE stock_id='2330' AND source=? AND trade_date<=? AND close>0
        ORDER BY trade_date DESC
        LIMIT ?
        """,
        (SOURCE, asof, max(1, n)),
    ).fetchall()
    return [str(r[0]) for r in reversed(rows)]


def scan_5d_net95(
    conn,
    *,
    asof: str,
    branch_id: str,
    mega: set[str],
    buy_floor: float = RESEARCH_BUY_FLOOR,
    net_min: float = RESEARCH_NET_MIN,
) -> list[dict]:
    """Stocks with 5d buy≥floor and 5d net_ratio≥net_min, excluding mega.

    Amounts = shares × that day's close. Ranked by buy_5d desc.
    """
    days = last_n_trading_days(conn, asof=asof, n=5)
    if len(days) < 5:
        return []
    placeholders = ",".join("?" * len(days))
    rows = conn.execute(
        f"""
        SELECT b.stock_id,
               SUM(b.buy * p.close) AS buy_5d,
               SUM(b.sell * p.close) AS sell_5d,
               SUM(CASE WHEN b.trade_date=? THEN b.buy * p.close ELSE 0 END) AS buy_day,
               SUM(CASE WHEN b.trade_date=? THEN b.sell * p.close ELSE 0 END) AS sell_day,
               MAX(CASE WHEN b.trade_date=? THEN p.close ELSE NULL END) AS close
        FROM stock_broker_branch_daily b
        JOIN stock_daily_bars p
          ON p.stock_id=b.stock_id
         AND p.trade_date=b.trade_date
         AND p.source=?
        WHERE b.source=?
          AND b.securities_trader_id=?
          AND b.trade_date IN ({placeholders})
          AND p.close>0
          AND length(b.stock_id)=4
          AND b.stock_id GLOB '[0-9][0-9][0-9][0-9]'
          AND b.stock_id NOT GLOB '00*'
        GROUP BY b.stock_id
        HAVING buy_5d >= ?
        """,
        (asof, asof, asof, SOURCE, SOURCE, branch_id, *days, buy_floor),
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        sid = str(r["stock_id"])
        if sid in mega:
            continue
        buy_5d = float(r["buy_5d"] or 0)
        sell_5d = float(r["sell_5d"] or 0)
        if buy_5d <= 0:
            continue
        net = (buy_5d - sell_5d) / buy_5d
        if net < net_min:
            continue
        buy_day = float(r["buy_day"] or 0)
        sell_day = float(r["sell_day"] or 0)
        day_net = (buy_day - sell_day) / buy_day if buy_day > 0 else None
        close = float(r["close"] or 0)
        out.append(
            {
                "stock_id": sid,
                "buy_5d": buy_5d,
                "sell_5d": sell_5d,
                "net_ratio": net,
                "buy_amt": buy_day,  # day amount (compat with old top1 field)
                "buy_day": buy_day,
                "sell_day": sell_day,
                "day_net": day_net,
                "close": close,
                "window_start": days[0],
                "window_end": days[-1],
                "day_whale": bool(
                    buy_day >= buy_floor and day_net is not None and day_net >= net_min
                ),
                "strength_1p5": bool(
                    buy_day >= STRENGTH_FLOOR or buy_5d >= STRENGTH_FLOOR
                ),
            }
        )
    out.sort(key=lambda x: (-x["buy_5d"], x["stock_id"]))
    return out


def evaluate_top1_1p5(top1: dict | None, mega: set[str]) -> dict:
    """Legacy fire: Top1 ≥1.5億 and not mega (xindian)."""
    if top1 is None:
        return {
            "fired": False,
            "reason": "無可用分點 tape / Top1",
            "top1": None,
            "candidates": [],
            "mode": "top1_1p5",
        }
    sid = top1["stock_id"]
    name = STOCK_NAMES.get(sid, sid)
    if sid in mega:
        return {
            "fired": False,
            "reason": f"Top1 {sid} {name} ∈ MEGA 黑名單（已剔除）",
            "top1": top1,
            "stock_name": name,
            "candidates": [],
            "mode": "top1_1p5",
        }
    if top1["buy_amt"] < STRENGTH_FLOOR:
        return {
            "fired": False,
            "reason": (
                f"金額不足：當下 {top1['buy_amt']/1e8:.2f}億 "
                f"< 目標 {STRENGTH_FLOOR/1e8:.1f}億"
            ),
            "top1": top1,
            "stock_name": name,
            "candidates": [],
            "mode": "top1_1p5",
        }
    share = float(top1["buy_share"])
    vs25 = share / SHARE_STANDARD if SHARE_STANDARD else float("nan")
    return {
        "fired": True,
        "reason": (
            f"達標：≥{STRENGTH_FLOOR/1e8:.1f}億 ∩ !mega · "
            f"當下 {top1['buy_amt']/1e8:.2f}億 · "
            f"share {share:.1%}（{share:.1%}/{SHARE_STANDARD:.0%}＝{vs25:.2f}×；"
            f"最佳標準 {SHARE_STANDARD:.0%}）"
        ),
        "top1": top1,
        "stock_name": name,
        "candidates": [],
        "mode": "top1_1p5",
        "strength_1p5": True,
    }


def evaluate_research_5d(
    candidates: list[dict],
    *,
    top1: dict | None,
) -> dict:
    """Songshan research fire: 5d buy≥0.5億 ∩ net≥0.95 ∩ !mega (pre-filtered)."""
    if not candidates:
        top_note = ""
        if top1 is not None:
            top_note = (
                f"；當日 Top1 {top1['stock_id']} "
                f"{top1['buy_amt']/1e8:.2f}億"
            )
        return {
            "fired": False,
            "reason": (
                f"未達五日累積≥{RESEARCH_BUY_FLOOR/1e8:.1f}億∩淨比≥{RESEARCH_NET_MIN:.0%}"
                f"∩!mega{top_note}"
            ),
            "top1": top1,
            "candidates": [],
            "mode": "research_5d",
        }
    primary = candidates[0]
    sid = primary["stock_id"]
    name = STOCK_NAMES.get(sid, sid)
    strength = " · 強印≥1.5億" if primary.get("strength_1p5") else ""
    day_tag = " · 當日大戶印" if primary.get("day_whale") else ""
    return {
        "fired": True,
        "reason": (
            f"達標：五日累積淨比95 ∩ !mega · "
            f"{sid} {name} 五日買 {primary['buy_5d']/1e8:.2f}億 · "
            f"淨比 {primary['net_ratio']:.1%}"
            f"{day_tag}{strength}"
        ),
        "top1": {
            **primary,
            "buy_share": 0.0,
            "day_buy_amt": primary.get("buy_day") or 0.0,
        },
        "stock_name": name,
        "candidates": candidates,
        "mode": "research_5d",
        "strength_1p5": bool(primary.get("strength_1p5")),
        "day_whale": bool(primary.get("day_whale")),
    }


def evaluate(top1: dict | None, mega: set[str]) -> dict:
    """Backward-compatible alias → top1 1.5億 rule."""
    return evaluate_top1_1p5(top1, mega)


def _share_line(share: float) -> str:
    vs25 = share / SHARE_STANDARD if SHARE_STANDARD else float("nan")
    return (
        f"  當下占比：{share:.1%} · 相對最佳標準："
        f"{share:.1%}/{SHARE_STANDARD:.0%}＝{vs25:.2f}×"
        f"（最佳標準＝{SHARE_STANDARD:.0%}；非觸發條件）"
    )


def format_entry_hint_lines(*, branch_key: str = "songshan") -> list[str]:
    """T+1 human entry hints. Songshan carries research champion path; xindian lighter."""
    lines = [
        "—— 進場提示（人工 · 非自動送單 · 夜信無法預判隔日開盤）——",
    ]
    if branch_key == "songshan":
        lines += [
            "  · 夜信觸發＝跟單松山·五日累積淨比95 ∩ !mega（五日買≥0.5億∩淨≥95%）",
            "  · ≥1.5億僅為「強印」標註，不再當唯一觸發門檻",
            "  · 進場冠軍：+ entry_25m_nonfail（T+1≈09:25：先衝開+1%又收跌破開 → 放棄）",
            "  · 加嚴可選：訊號日已是當日大戶印（單日買≥0.5億∩淨≥95%）再做",
            "  · 持有：抱股型 L1H7；出清型可跟賣累積~90%隔日（見說明書）",
            "  · 限價備援：訊號收×1.01；勿套用專家池「追價≤3%跳過」",
        ]
    else:
        lines += [
            "  · 觸發尺＝≥1.5億 ∩ !mega（新店暫維持；松山已改五日累積淨比95）",
            "  · 松山「25m 非 fail」冠軍路徑不自動套用本席",
            "  · 限價備援：訊號收×1.01；持有觀測預設 H7（研究 · 未採納）",
        ]
    lines.append("")
    return lines


def format_body(
    *,
    asof: str,
    result: dict,
    tape_note: str,
    branch_id: str = BRANCH_ID,
    branch_name: str = BRANCH_NAME,
    branch_key: str = "songshan",
) -> str:
    top1 = result.get("top1")
    mode = result.get("mode") or (
        "research_5d" if branch_key == "songshan" else "top1_1p5"
    )
    if mode == "research_5d":
        trigger_line = (
            "觸發規則：跟單松山·五日累積淨比95 ∩ !mega"
            f"（五日買≥{RESEARCH_BUY_FLOOR/1e8:.1f}億 ∩ 淨比≥{RESEARCH_NET_MIN:.0%}）"
        )
        name_line = (
            "命名：研究覆蓋冠軍母體 · 進場另加 entry_25m_nonfail · "
            f"≥{STRENGTH_FLOOR/1e8:.1f}億＝強印標註"
        )
    else:
        trigger_line = (
            f"觸發規則：Top1 買超金額 ≥ {STRENGTH_FLOOR/1e8:.1f}億 "
            "∩ 標的 ∉ mega_blacklist_v1"
        )
        name_line = "命名：跟單·1.5億非Mega（新店舊尺）"
    lines = [
        f"{branch_name}（{branch_id}）跟單觀測 · {asof}",
        "",
        trigger_line,
        name_line,
        "說明欄位：五日買／淨比／當日買；當下%/25%（舊標籤，非松山觸發門檻）",
        "建議：訊號日收盤後標記 → 次日依進場提示跟 · 持有 7 交易日（研究觀測 · 未採納 · 不下單）",
        f"Tape：{tape_note}",
        "",
    ]
    if result["fired"]:
        t = top1
        assert t is not None
        if mode == "research_5d":
            tags = []
            if result.get("day_whale"):
                tags.append("當日大戶印")
            if result.get("strength_1p5"):
                tags.append("強印≥1.5億")
            tag_s = f"（{' · '.join(tags)}）" if tags else ""
            lines += [
                f"【主訊號】五日累積淨比95 達標{tag_s}",
                f"  標的：{result.get('stock_name', t['stock_id'])}（{t['stock_id']}）",
                f"  五日買：{float(t.get('buy_5d') or 0)/1e8:.2f} 億"
                f"（門檻 ≥ {RESEARCH_BUY_FLOOR/1e8:.1f} 億）",
                f"  五日淨比：{float(t.get('net_ratio') or 0):.1%}"
                f"（門檻 ≥ {RESEARCH_NET_MIN:.0%}）",
                f"  當日買：{float(t.get('buy_day') or t.get('buy_amt') or 0)/1e8:.2f} 億",
            ]
            dn = t.get("day_net")
            if dn is not None:
                lines.append(f"  當日淨比：{float(dn):.1%}")
            if t.get("close"):
                lines.append(f"  收盤：{float(t['close']):.2f}")
            if t.get("window_start"):
                lines.append(
                    f"  五日窗：{t.get('window_start')} → {t.get('window_end')}"
                )
            lines.append("")
            cands = list(result.get("candidates") or [])[1:6]
            if cands:
                lines.append("  其餘達標（按五日買）：")
                for c in cands:
                    cn = STOCK_NAMES.get(c["stock_id"], c["stock_id"])
                    extra = []
                    if c.get("day_whale"):
                        extra.append("當日印")
                    if c.get("strength_1p5"):
                        extra.append("強印")
                    ex = f" · {'/'.join(extra)}" if extra else ""
                    lines.append(
                        f"  · {cn}（{c['stock_id']}）五日買 {c['buy_5d']/1e8:.2f}億"
                        f" 淨比 {c['net_ratio']:.1%}{ex}"
                    )
                lines.append("")
        else:
            share = float(t.get("buy_share") or 0)
            lines += [
                "【主訊號】達標（≥1.5億 ∩ !mega）",
                f"  標的：{result.get('stock_name', t['stock_id'])}（{t['stock_id']}）",
                f"  當下金額：{t['buy_amt']/1e8:.2f} 億"
                f"（目標 ≥ {STRENGTH_FLOOR/1e8:.1f} 億）",
                _share_line(share),
                f"  收盤：{t['close']:.2f}",
                f"  當日分點買超合計：{float(t.get('day_buy_amt') or 0)/1e8:.2f} 億",
                "",
            ]
    else:
        if mode == "research_5d":
            lines += [
                "【今日無訊號】排程已執行；未達五日累積淨比95 ∩ !mega。",
                f"  原因：{result['reason']}",
                "",
            ]
        else:
            lines += [
                "【今日無訊號】排程已執行；未達 ≥1.5億 ∩ !mega。",
                f"  原因：{result['reason']}",
                "",
            ]
        if top1 is not None and mode != "research_5d":
            share = float(top1.get("buy_share") or 0)
            lines += [
                "當日 Top1（未達標明細）：",
                f"  {result.get('stock_name', top1['stock_id'])}（{top1['stock_id']}）",
                f"  當下金額：{top1['buy_amt']/1e8:.2f} 億"
                f"（目標 ≥ {STRENGTH_FLOOR/1e8:.1f} 億）",
                _share_line(share),
                "",
            ]
        elif top1 is not None and mode == "research_5d":
            lines += [
                "當日 Top1（參考）：",
                f"  {STOCK_NAMES.get(top1['stock_id'], top1['stock_id'])}"
                f"（{top1['stock_id']}）"
                f" 當日買 {top1['buy_amt']/1e8:.2f} 億",
                "",
            ]
    lines += format_entry_hint_lines(branch_key=branch_key)
    lines += [
        "參考：reports/.../凱基松山_凱基信義_跟單說明書.md · liao_songshan_20260721/TAPE_FRONTIER.md",
        "腳本：scripts/research/run_songshan_follow_watch.py",
    ]
    return "\n".join(lines)


def write_report(
    asof: str,
    body: str,
    fired: bool,
    *,
    branch_key: str = "songshan",
) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / f"{branch_key}_{asof.replace('-', '')}.md"
    md = [
        f"# {BRANCH_SPECS.get(branch_key, {}).get('name', branch_key)}跟單觀測 · {asof}",
        "",
        f"- status: `{'TRIGGERED' if fired else 'no_signal'}`",
        f"- generated: `{datetime.now().isoformat(timespec='seconds')}`",
        "",
        "```",
        body,
        "```",
        "",
    ]
    path.write_text("\n".join(md), encoding="utf-8")
    return path


def run_branch_watch(
    conn,
    *,
    branch_key: str,
    asof_arg: str = "",
    do_refresh: bool = True,
) -> dict:
    """Evaluate one branch; returns digest section dict."""
    spec = BRANCH_SPECS[branch_key]
    branch_id = spec["id"]
    branch_name = spec["name"]
    mode = spec.get("signal_mode") or "top1_1p5"
    mega = load_mega()
    asof = asof_arg.strip() or latest_trading_day(conn, branch_id=branch_id)
    if not asof:
        return {
            "id": branch_id,
            "title": f"{branch_name}跟單",
            "asof": None,
            "fired": False,
            "summary": "無交易日",
            "body": f"{branch_name}\n【今日無訊號】DB 無可用交易日。",
        }
    tape_note = "db-only"
    days5 = last_n_trading_days(conn, asof=asof, n=5)
    if do_refresh:
        start = days5[0] if days5 else asof
        tape_note = try_refresh_branch_tape(
            asof, backfill_key=spec["backfill_key"], start=start
        )
        days5 = last_n_trading_days(conn, asof=asof, n=5)
    sids = branch_stock_ids(conn, asof=asof, branch_id=branch_id)
    ohlc_note = refresh_missing_ohlc(conn, asof=asof, stock_ids=sids)
    tape_note = f"{tape_note}; {ohlc_note}"
    top1 = top1_on(conn, asof=asof, branch_id=branch_id)
    if mode == "research_5d":
        cands = scan_5d_net95(conn, asof=asof, branch_id=branch_id, mega=mega)
        result = evaluate_research_5d(cands, top1=top1)
        title = f"{branch_name} 五日累積淨比95"
    else:
        result = evaluate_top1_1p5(top1, mega)
        title = f"{branch_name} ≥1.5億×!mega"
    body = format_body(
        asof=asof,
        result=result,
        tape_note=tape_note,
        branch_id=branch_id,
        branch_name=branch_name,
        branch_key=branch_key,
    )
    write_report(asof, body, bool(result["fired"]), branch_key=branch_key)
    return {
        "id": branch_id,
        "title": title,
        "asof": asof,
        "fired": bool(result["fired"]),
        "summary": result.get("reason") or ("達標" if result["fired"] else "無訊號"),
        "body": body,
    }


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="分點跟單觀測 · ≥1.5億 ∩ !mega")
    ap.add_argument(
        "--branch",
        choices=sorted(BRANCH_SPECS.keys()),
        default="songshan",
        help="songshan=凱基松山 · xindian=富邦新店",
    )
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--asof", default="", help="YYYY-MM-DD")
    ap.add_argument("--no-refresh", action="store_true")
    ap.add_argument("--state-dir", type=Path, default=ROOT / "data" / "scratch")
    ap.add_argument("--force-email", action="store_true")
    ap.add_argument("--bootstrap-only", action="store_true")
    return ap.parse_args()


def main() -> int:
    load_project_dotenv()
    args = parse_args()
    spec = BRANCH_SPECS[args.branch]
    state_path = args.state_dir / spec["state"]
    state = load_state(state_path)
    notified: dict = state.setdefault("notified", {})

    conn = connect(args.db)
    try:
        section = run_branch_watch(
            conn,
            branch_key=args.branch,
            asof_arg=args.asof,
            do_refresh=not args.no_refresh,
        )
        asof = section.get("asof") or ""
        fired = bool(section.get("fired"))
        reason = section.get("summary") or ""
        body = section.get("body") or ""
        print(f"branch={args.branch} asof={asof} fired={fired} reason={reason}")

        digest_key = f"digest|{asof}"
        signal_key = f"{spec['rule_tag']}|{asof}"

        if args.bootstrap_only or (not notified and not args.force_email):
            notified[digest_key] = {
                "at": datetime.now().isoformat(timespec="seconds"),
                "bootstrap": True,
                "fired": fired,
            }
            if fired:
                notified[signal_key] = {
                    "at": datetime.now().isoformat(timespec="seconds"),
                    "bootstrap": True,
                }
            state["bootstrapped_asof"] = asof
            save_state(state_path, state)
            print("bootstrap state written (no email)")
            return 0

        already = digest_key in notified and not args.force_email
        if already and not (fired and signal_key not in notified):
            print(f"already notified for {asof}; skip email")
            return 0

        if not _email_enabled():
            print(f"email skipped: {ENV_FLAG}=0")
            notified[digest_key] = {
                "at": datetime.now().isoformat(timespec="seconds"),
                "skipped_email": True,
                "fired": fired,
            }
            save_state(state_path, state)
            return 0

        label = spec["name"]
        if fired:
            if spec.get("signal_mode") == "research_5d":
                subject = f"[股票研究] {label}五日累積淨比95達標 · {asof}"
            else:
                subject = f"[股票研究] {label}≥1.5億×!mega達標 · {asof}"
        else:
            subject = f"[股票研究] {label}跟單觀測 · {asof} · 今日無訊號"

        send_alert(subject, body)
        print(f"email sent: {subject}")
        notified[digest_key] = {
            "at": datetime.now().isoformat(timespec="seconds"),
            "fired": fired,
        }
        if fired:
            notified[signal_key] = {
                "at": datetime.now().isoformat(timespec="seconds"),
            }
        if spec.get("signal_mode") == "research_5d":
            state["rule"] = (
                "五日累積≥0.5億∩淨比≥95%∩!mega；≥1.5億＝強印標註；"
                "進場提示 entry_25m_nonfail"
            )
        else:
            state["rule"] = "≥1.5億 ∩ !mega（share 僅說明；最佳標準 25%）"
        state["branch_id"] = spec["id"]
        save_state(state_path, state)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
