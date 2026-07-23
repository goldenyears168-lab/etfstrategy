#!/usr/bin/env python3
"""處置股專家池跟單 · 夜間觀測（observe only · 不下單）.

週一至五 20:35 launchd。當專家池分點對「第二次處置」窗內個股
買進達門檻，且通過 T0 濾網時寄信；內文提醒 T+1 開盤追價規則。

  PYTHONPATH=src:scripts/research .venv/bin/python \\
    scripts/research/run_second_disp_expert_pool_watch.py
  PYTHONPATH=src:scripts/research .venv/bin/python \\
    scripts/research/run_second_disp_expert_pool_watch.py --force-email
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import urllib.request
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts" / "research"))

from notify_email import send_alert  # noqa: E402
from project_dotenv import load_project_dotenv  # noqa: E402

CFG_PATH = ROOT / "config" / "second_disp_expert_pool_watch.json"
TWSE_CACHE = (
    ROOT
    / "reports/research/branch-footprint-screen/second_disp_top30_l1h7"
    / "twse_second_disp_2026.json"
)
REPORT_DIR = (
    ROOT
    / "reports/research/branch-footprint-screen/second_disp_top30_l1h7"
    / "watch"
)
STATE_NAME = "second_disp_expert_pool_watch_state.json"
SOURCE = "finmind"
KB_REL = (
    "reports/research/branch-footprint-screen/second_disp_top30_l1h7/"
    "處置股專家池跟單策略.md"
)


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def load_cfg() -> dict:
    return json.loads(CFG_PATH.read_text(encoding="utf-8"))


def email_enabled(cfg: dict) -> bool:
    key = (cfg.get("email") or {}).get("env_flag") or "RUN_SECOND_DISP_EXPERT_POOL_EMAIL"
    if key in os.environ:
        return os.environ[key].strip() not in ("0", "false", "False")
    return True


def roc_to_iso(s: str) -> str:
    y, m, d = s.strip().split("/")
    return f"{int(y) + 1911}-{m}-{d}"


def fetch_twse_second(force: bool = False) -> list[dict]:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    TWSE_CACHE.parent.mkdir(parents=True, exist_ok=True)
    if TWSE_CACHE.exists() and not force:
        age_h = (datetime.now().timestamp() - TWSE_CACHE.stat().st_mtime) / 3600.0
        if age_h < 20:
            return json.loads(TWSE_CACHE.read_text(encoding="utf-8"))
    end = datetime.now().strftime("%Y%m%d")
    url = (
        "https://www.twse.com.tw/rwd/zh/announcement/punish"
        f"?response=json&startDate=20260101&endDate={end}"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            payload = json.loads(resp.read().decode())
    except Exception as exc:  # noqa: BLE001
        log(f"TWSE fetch failed: {exc}; using cache if any")
        if TWSE_CACHE.exists():
            return json.loads(TWSE_CACHE.read_text(encoding="utf-8"))
        raise
    rows = []
    for row in payload.get("data") or []:
        if row[7] != "第二次處置":
            continue
        a, b = row[6].replace("～", "~").split("~")
        sid = str(row[2])
        if len(sid) != 4 or not sid.isdigit():
            continue
        rows.append(
            {
                "announce": roc_to_iso(row[1]),
                "sid": sid,
                "name": row[3],
                "p_start": roc_to_iso(a),
                "p_end": roc_to_iso(b),
            }
        )
    TWSE_CACHE.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    return rows


def merge_episodes(rows: list[dict]) -> list[dict]:
    by: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by[r["sid"]].append(r)
    out: list[dict] = []
    for sid, lst in by.items():
        lst.sort(key=lambda x: x["announce"])
        cur = None
        for e in lst:
            if cur is None:
                cur = {
                    "sid": sid,
                    "name": e["name"],
                    "announce0": e["announce"],
                    "p_start": e["p_start"],
                    "p_end": e["p_end"],
                    "n_ann": 1,
                }
                continue
            gap = (
                datetime.fromisoformat(e["announce"])
                - datetime.fromisoformat(cur["p_end"])
            ).days
            if e["announce"] <= cur["p_end"] or gap <= 5:
                cur["n_ann"] += 1
                cur["p_end"] = max(cur["p_end"], e["p_end"])
            else:
                out.append(cur)
                cur = {
                    "sid": sid,
                    "name": e["name"],
                    "announce0": e["announce"],
                    "p_start": e["p_start"],
                    "p_end": e["p_end"],
                    "n_ann": 1,
                }
        if cur:
            out.append(cur)
    return out


def connect_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def latest_asof(conn: sqlite3.Connection, asof_arg: str | None) -> str | None:
    if asof_arg:
        return asof_arg
    row = conn.execute(
        """
        SELECT MAX(trade_date) AS d FROM stock_daily_bars
        WHERE source=? AND stock_id='2330'
        """,
        (SOURCE,),
    ).fetchone()
    return str(row["d"]) if row and row["d"] else None


def active_disp(episodes: list[dict], day: str) -> dict[str, dict]:
    out = {}
    for ep in episodes:
        if ep["p_start"] <= day <= ep["p_end"]:
            out[ep["sid"]] = ep
    return out


def load_closes(
    conn: sqlite3.Connection, sids: list[str], end: str, n_lookback: int = 30
) -> dict[tuple[str, str], float]:
    if not sids:
        return {}
    q = ",".join("?" * len(sids))
    cal = [
        r[0]
        for r in conn.execute(
            """
            SELECT trade_date FROM stock_daily_bars
            WHERE source=? AND stock_id='2330' AND trade_date<=?
            ORDER BY trade_date DESC LIMIT ?
            """,
            (SOURCE, end, n_lookback + 5),
        )
    ]
    if not cal:
        return {}
    start = cal[-1]
    out: dict[tuple[str, str], float] = {}
    for sid, d, c in conn.execute(
        f"""
        SELECT stock_id, trade_date, close FROM stock_daily_bars
        WHERE source=? AND stock_id IN ({q})
          AND trade_date BETWEEN ? AND ? AND close>0
        """,
        (SOURCE, *sids, start, end),
    ):
        out[(str(sid), str(d))] = float(c)
    return out


def load_ix_ohlc(
    conn: sqlite3.Connection, day: str
) -> tuple[float, float] | None:
    row = conn.execute(
        """
        SELECT open, close FROM daily_bars
        WHERE code='IX0001' AND date=?
          AND open>0 AND close>0
        ORDER BY CASE source
          WHEN 'yahoo' THEN 0 WHEN 'tej' THEN 1 WHEN 'finmind' THEN 2 ELSE 9 END
        LIMIT 1
        """,
        (day,),
    ).fetchone()
    if not row:
        row = conn.execute(
            """
            SELECT open, close FROM stock_daily_bars
            WHERE source=? AND stock_id='IX0001' AND trade_date=?
              AND open>0 AND close>0
            """,
            (SOURCE, day),
        ).fetchone()
    if not row:
        return None
    return float(row[0]), float(row[1])


def calendar_before(
    conn: sqlite3.Connection, day: str, n: int
) -> list[str]:
    rows = conn.execute(
        """
        SELECT trade_date FROM stock_daily_bars
        WHERE source=? AND stock_id='2330' AND trade_date<=?
        ORDER BY trade_date DESC LIMIT ?
        """,
        (SOURCE, day, n + 1),
    ).fetchall()
    days = [str(r[0]) for r in rows]
    days.reverse()
    return days


def pct(a: float | None, b: float | None) -> float | None:
    if a is None or b is None or a <= 0:
        return None
    return (b / a - 1.0) * 100.0


def seat_buys(
    conn: sqlite3.Connection,
    branch_id: str,
    day: str,
    disp_sids: list[str],
    abs_floor: float,
) -> list[dict]:
    if not disp_sids:
        return []
    q = ",".join("?" * len(disp_sids))
    sql = f"""
        SELECT b.stock_id, b.buy, p.close, b.buy * p.close AS buy_amt
        FROM stock_broker_branch_daily b
        JOIN stock_daily_bars p
          ON p.stock_id=b.stock_id AND p.trade_date=b.trade_date AND p.source=?
        WHERE b.source=?
          AND b.securities_trader_id=?
          AND b.trade_date=?
          AND b.buy>0 AND p.close>0
          AND b.stock_id IN ({q})
          AND b.buy * p.close >= ?
        ORDER BY buy_amt DESC
    """
    out = []
    for r in conn.execute(
        sql, (SOURCE, SOURCE, branch_id, day, *disp_sids, abs_floor)
    ):
        out.append(
            {
                "stock_id": str(r["stock_id"]),
                "stock_name": str(r["stock_id"]),
                "buy_shares": float(r["buy"]),
                "close": float(r["close"]),
                "buy_amt": float(r["buy_amt"]),
            }
        )
    return out


def soft_flag_ids(cfg: dict) -> list[dict]:
    return list((cfg.get("t0_filters") or {}).get("soft_flags") or [])


def eval_soft_flags(cfg: dict, feat: dict) -> tuple[list[str], list[str]]:
    """Return (soft_labels, track_improve_ids that fired)."""
    soft: list[str] = []
    track_ids: list[str] = []
    for rule in soft_flag_ids(cfg):
        rid = str(rule.get("id") or "")
        label = str(rule.get("label") or rid)
        fired = False
        if rid == "r3_ge15_and_ix_le0":
            r = feat.get("r3")
            ix = feat.get("ix_day")
            thr_r = float(rule.get("r3_pct_ge", 15))
            thr_ix = float(rule.get("ix_day_pct_le", 0))
            if r is not None and ix is not None and r >= thr_r and ix <= thr_ix:
                fired = True
        elif rid == "r5_ge15_and_ix_le0":
            # legacy id
            r = feat.get("r5")
            ix = feat.get("ix_day")
            thr_r = float(rule.get("r5_pct_ge", 15))
            thr_ix = float(rule.get("ix_day_pct_le", 0))
            if r is not None and ix is not None and r >= thr_r and ix <= thr_ix:
                fired = True
        elif rid == "r10_ge40":
            r10 = feat.get("r10")
            if r10 is not None and r10 >= float(rule.get("r10_pct_ge", 40)):
                fired = True
        elif rid == "ix_day_le_m1p5":
            ix = feat.get("ix_day")
            if ix is not None and ix <= float(rule.get("ix_day_pct_le", -1.5)):
                fired = True
        if fired:
            soft.append(label)
            if rule.get("track_improve"):
                track_ids.append(rid)
    return soft, track_ids


def improve_rule_cleared(cfg: dict, feat: dict, track_ids: list[str]) -> bool:
    """True if none of the tracked remind rules still fire."""
    if not track_ids:
        return False
    _, still = eval_soft_flags(cfg, feat)
    return not any(t in still for t in track_ids)


def build_features(
    conn: sqlite3.Connection,
    stock_id: str,
    day: str,
    closes: dict[tuple[str, str], float],
    cal: list[str],
    ix_day: float | None,
) -> dict:
    if day not in cal:
        return {
            "r3": None,
            "r5": None,
            "r10": None,
            "ix_day": ix_day,
            "day_ret": None,
        }
    i = cal.index(day)
    c0 = closes.get((stock_id, day))
    c3 = closes.get((stock_id, cal[i - 3])) if i >= 3 else None
    c5 = closes.get((stock_id, cal[i - 5])) if i >= 5 else None
    c10 = closes.get((stock_id, cal[i - 10])) if i >= 10 else None
    row = conn.execute(
        """
        SELECT open, close FROM stock_daily_bars
        WHERE source=? AND stock_id=? AND trade_date=?
        """,
        (SOURCE, stock_id, day),
    ).fetchone()
    day_ret = None
    if row and row[0] and row[1]:
        day_ret = pct(float(row[0]), float(row[1]))
    return {
        "r3": pct(c3, c0),
        "r5": pct(c5, c0),
        "r10": pct(c10, c0),
        "ix_day": ix_day,
        "day_ret": day_ret,
        "close": c0,
    }


def prev_trading_day(cal: list[str], day: str) -> str | None:
    if day not in cal:
        return None
    i = cal.index(day)
    return cal[i - 1] if i >= 1 else None


def trading_age(cal: list[str], older: str, newer: str) -> int | None:
    if older not in cal or newer not in cal:
        return None
    return cal.index(newer) - cal.index(older)


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


def format_email(
    cfg: dict,
    asof: str,
    hit_rows: list[dict],
    improved_rows: list[dict],
) -> tuple[str, str]:
    prefix = (cfg.get("email") or {}).get("subject_prefix") or "處置股專家池"
    n_hit = len(hit_rows)
    n_remind = sum(1 for r in hit_rows if r.get("soft"))
    n_imp = len(improved_rows)
    bits = [f"訊號 {n_hit}"]
    if n_remind:
        bits.append(f"含提醒 {n_remind}")
    if n_imp:
        bits.append(f"條件轉好 {n_imp}")
    subject = f"[股票研究] {prefix} · {asof} · {'／'.join(bits)}"
    lines = [
        f"處置股專家池跟單策略 · 觀測日 {asof}",
        "Research observe only · 不下單",
        "",
        "【時間軸】",
        "· 訊號日 T0＝分點達門檻買進日（收盤後 20:35 判定）",
        "· 原協議進場＝T+1 開盤；唯一硬條件＝開盤相對昨收 >+5% → 不做",
        "· 「條件轉好」＝昨日有短線熱提醒、今日盤後已解除 → 已錯過原 T+1 開，",
        "  僅供延遲評估（自行決定是否改看下一開）",
        "",
        "【T0 提醒（不擋信）】近3日漲≥15% 且台指當日≤0%；另：r10≥40%／台指≤−1.5%",
        "",
    ]
    if hit_rows:
        lines.append(f"【今日訊號 · {n_hit}】")
        for r in hit_rows:
            soft = (" ⚠ " + "；".join(r["soft"])) if r.get("soft") else ""
            lines.append(
                f"- {r['stock_id']} {r['stock_name']} · "
                f"{r['branch_name']}(`{r['branch_id']}`) "
                f"買 {r['buy_amt']/1e8:.2f}億≥{r['abs_floor_yi']:g}億"
                f"{soft}"
            )
            feat = r["feat"]
            fb = []
            if feat.get("r3") is not None:
                fb.append(f"r3={feat['r3']:+.1f}%")
            if feat.get("r10") is not None:
                fb.append(f"r10={feat['r10']:+.1f}%")
            if feat.get("ix_day") is not None:
                fb.append(f"台指日={feat['ix_day']:+.2f}%")
            if fb:
                lines.append(f"    {' · '.join(fb)}")
        lines.append("")
    if improved_rows:
        lines.append(f"【昨日提醒・今日條件轉好 · {n_imp}】")
        lines.append("（訊號日已過；原 L1H7＝訊號翌日開盤，此為延遲提醒）")
        for r in improved_rows:
            lines.append(
                f"- {r['stock_id']} {r['stock_name']} · "
                f"{r['branch_name']}(`{r['branch_id']}`) "
                f"訊號日 {r['signal_date']} 買 {r['buy_amt']/1e8:.2f}億"
            )
            lines.append(
                f"    昨日提醒：{'；'.join(r.get('prior_soft') or [])}"
            )
            feat = r["feat"]
            fb = []
            if feat.get("r3") is not None:
                fb.append(f"今r3={feat['r3']:+.1f}%")
            if feat.get("ix_day") is not None:
                fb.append(f"今台指日={feat['ix_day']:+.2f}%")
            if fb:
                lines.append(f"    {' · '.join(fb)}")
        lines.append("")
    lines.extend(
        [
            "【開盤硬規則】相對昨收 > +5% → 不做；其餘可持有 L1H7",
            "外資席僅參考；優先本土席",
            "",
            f"知識庫：{KB_REL}",
            "設定：config/second_disp_expert_pool_watch.json",
            "腳本：scripts/research/run_second_disp_expert_pool_watch.py",
        ]
    )
    return subject, "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=None)
    ap.add_argument("--asof", default=None, help="YYYY-MM-DD override")
    ap.add_argument("--force-email", action="store_true")
    ap.add_argument("--refresh-twse", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    load_project_dotenv()  # project-root .env（勿傳目錄 path）
    cfg = load_cfg()
    seats = cfg.get("seats") or []
    improve_cfg = cfg.get("improve_recheck") or {}
    improve_on = bool(improve_cfg.get("enabled", True))
    max_age = int(improve_cfg.get("max_age_trading_days", 2))

    db = args.db
    if db is None:
        for p in (ROOT / "data/stocks.db", ROOT / "data/scratch/rolling_mvp_snapshot.db"):
            if p.exists():
                db = p
                break
    if db is None:
        raise FileNotFoundError("no stocks.db")

    twse_rows = fetch_twse_second(force=args.refresh_twse)
    episodes = merge_episodes(twse_rows)
    conn = connect_db(db)
    asof = latest_asof(conn, args.asof)
    if not asof:
        log("no asof")
        return 1
    disp = active_disp(episodes, asof)
    disp_sids = sorted(disp.keys())
    log(f"asof={asof} active_second_disp={len(disp_sids)} seats={len(seats)}")

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    state_path = REPORT_DIR / STATE_NAME
    state = load_state(state_path)
    notified = state.setdefault("notified", {})
    pending: list[dict] = list(state.get("pending_improve") or [])

    if not disp_sids:
        log("no active second-disp stocks today")
        (REPORT_DIR / f"watch_{asof}.json").write_text(
            json.dumps(
                {
                    "asof": asof,
                    "hits": [],
                    "improved": [],
                    "note": "no_active_disp",
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return 0

    cal_full = calendar_before(conn, asof, 40)

    need_sids = set(disp_sids)
    for p in pending:
        need_sids.add(str(p.get("stock_id")))
    need_sids.add("2330")
    closes = load_closes(conn, sorted(need_sids), asof, n_lookback=45)
    ix_ohlc = load_ix_ohlc(conn, asof)
    ix_day = pct(ix_ohlc[0], ix_ohlc[1]) if ix_ohlc else None

    hit_rows: list[dict] = []
    new_pending: list[dict] = []
    for seat in seats:
        bid = str(seat["id"])
        floor = float(seat["abs_floor_yi"]) * 1e8
        buys = seat_buys(conn, bid, asof, disp_sids, floor)
        for b in buys:
            feat = build_features(conn, b["stock_id"], asof, closes, cal_full, ix_day)
            soft, track_ids = eval_soft_flags(cfg, feat)
            ep = disp[b["stock_id"]]
            row = {
                "branch_id": bid,
                "branch_name": seat.get("name") or bid,
                "tag": seat.get("tag") or "",
                "abs_floor_yi": float(seat["abs_floor_yi"]),
                "stock_id": b["stock_id"],
                "stock_name": ep.get("name") or b["stock_name"] or b["stock_id"],
                "buy_amt": b["buy_amt"],
                "disp_p_start": ep["p_start"],
                "disp_p_end": ep["p_end"],
                "feat": feat,
                "soft": soft,
                "track_ids": track_ids,
                "signal_date": asof,
            }
            hit_rows.append(row)
            if track_ids and improve_on:
                new_pending.append(
                    {
                        "signal_date": asof,
                        "stock_id": row["stock_id"],
                        "stock_name": row["stock_name"],
                        "branch_id": bid,
                        "branch_name": row["branch_name"],
                        "abs_floor_yi": row["abs_floor_yi"],
                        "buy_amt": row["buy_amt"],
                        "prior_soft": soft,
                        "track_ids": track_ids,
                    }
                )

    # Recheck pending from prior signal days (not today)
    improved_rows: list[dict] = []
    kept_pending: list[dict] = []
    if improve_on:
        for p in pending:
            sig = str(p.get("signal_date") or "")
            if sig == asof:
                kept_pending.append(p)
                continue
            age = trading_age(cal_full, sig, asof)
            if age is None or age < 1 or age > max_age:
                continue  # drop expired / unknown
            sid = str(p["stock_id"])
            feat = build_features(conn, sid, asof, closes, cal_full, ix_day)
            if improve_rule_cleared(cfg, feat, list(p.get("track_ids") or [])):
                improved_rows.append(
                    {
                        **p,
                        "feat": feat,
                        "recheck_date": asof,
                        "age_trading_days": age,
                    }
                )
            else:
                kept_pending.append(p)

    # merge new pending (dedupe by signal|branch|stock)
    def pend_key(x: dict) -> str:
        return f"{x.get('signal_date')}|{x.get('branch_id')}|{x.get('stock_id')}"

    pending_map = {pend_key(x): x for x in kept_pending}
    for x in new_pending:
        pending_map[pend_key(x)] = x
    # drop improved from pending
    for x in improved_rows:
        pending_map.pop(pend_key(x), None)
    state["pending_improve"] = list(pending_map.values())

    payload = {
        "asof": asof,
        "ix_day": ix_day,
        "n_hits": len(hit_rows),
        "n_remind": sum(1 for r in hit_rows if r.get("soft")),
        "n_improved": len(improved_rows),
        "hits": hit_rows,
        "improved": improved_rows,
        "pending_improve": state["pending_improve"],
    }
    out_path = REPORT_DIR / f"watch_{asof}.json"
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log(
        f"hits={len(hit_rows)} remind={payload['n_remind']} "
        f"improved={len(improved_rows)} pending={len(state['pending_improve'])} → {out_path}"
    )

    key = (
        f"{asof}:hits={len(hit_rows)}:imp={len(improved_rows)}:"
        f"rem={payload['n_remind']}"
    )
    already = key in notified
    should_mail = bool(hit_rows or improved_rows) or args.force_email
    if already and not args.force_email:
        log(f"already notified {key}; skip email")
        should_mail = False

    if should_mail and not args.dry_run and email_enabled(cfg):
        subject, body = format_email(cfg, asof, hit_rows, improved_rows)
        if not hit_rows and not improved_rows:
            subject = (
                f"[股票研究] {(cfg.get('email') or {}).get('subject_prefix')} · {asof} · 無訊號"
            )
            body = (
                f"處置股專家池 · {asof}\n今日無達門檻買進（或無活躍第二次處置股）。\n"
                f"知識庫：{KB_REL}\n"
            )
        send_alert(subject, body)
        notified[key] = datetime.now().isoformat(timespec="seconds")
        save_state(state_path, state)
        log(f"email sent: {subject}")
    elif should_mail and args.dry_run:
        subject, body = format_email(cfg, asof, hit_rows, improved_rows)
        save_state(state_path, state)
        log(f"dry-run email subject={subject}\n{body}")
    else:
        save_state(state_path, state)
        if not email_enabled(cfg):
            log("email disabled by env")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
