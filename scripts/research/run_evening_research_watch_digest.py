#!/usr/bin/env python3
"""夜間研究觀測 · 合併成一封信（專家池 + 松山 + 新店）.

週一至五 20:00 launchd 入口。每項（達標或今日無訊號）都寫進同一封，
證明排程有跑；不下單。

分點跟單觸發：Top1 ≥1.5億 ∩ !mega；郵件另標當下%/25%（最佳標準）。

  PYTHONPATH=src python3 scripts/research/run_evening_research_watch_digest.py
  PYTHONPATH=src python3 scripts/research/run_evening_research_watch_digest.py --force-email
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
sys.path.insert(0, str(ROOT / "scripts" / "research"))

from notify_email import send_alert  # noqa: E402
from project_dotenv import load_project_dotenv  # noqa: E402
from stock_db import DATA_DIR, DEFAULT_DB_PATH, connect  # noqa: E402

import run_expert_pool_watch as pool  # noqa: E402
import run_songshan_follow_watch as song  # noqa: E402
import tier_a_evening_watch as tier_a  # noqa: E402

STATE_NAME = "evening_research_watch_digest_state.json"
REPORT_DIR = (
    ROOT
    / "reports"
    / "research"
    / "branch-footprint-screen"
    / "evening_watch"
)


def _email_enabled() -> bool:
    """On if either flag is on (default both on)."""
    for key in ("RUN_EVENING_WATCH_EMAIL", "RUN_EXPERT_POOL_EMAIL", "RUN_SONGSHAN_FOLLOW_EMAIL"):
        if key in os.environ:
            if os.environ[key].strip() in ("0", "false", "False"):
                continue
            return True
    # if all explicitly 0, off; else default on
    offs = []
    for key in ("RUN_EVENING_WATCH_EMAIL", "RUN_EXPERT_POOL_EMAIL", "RUN_SONGSHAN_FOLLOW_EMAIL"):
        if key in os.environ:
            offs.append(os.environ[key].strip() in ("0", "false", "False"))
    if offs and all(offs):
        return False
    return True


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


def collect_pool_section(
    conn,
    spec: pool.PoolSpec,
    *,
    asof_arg: str,
    refresh_n: int,
    yellow_annotation: bool = False,
) -> dict:
    tape_note = "db-only"
    if refresh_n > 0:
        days = list(
            reversed(
                list(pool.list_recent_trading_days(conn, stock_id=spec.stock_id, n=refresh_n))
            )
        )
        print(f"[{spec.stock_id}] refresh tape days={days}")
        counts = pool.refresh_stock_tape(conn, spec.stock_id, days)
        ok = sum(1 for v in counts.values() if v >= 0)
        tape_note = f"finmind refresh {ok}/{len(days)} days"

    asof = pool.resolve_asof(conn, spec, asof_arg)
    if not asof:
        return {
            "id": spec.stock_id,
            "title": f"{spec.stock_name}（{spec.stock_id}）專家池",
            "asof": None,
            "fired": False,
            "summary": "無核心分點 tape",
            "body": f"{spec.stock_name}（{spec.stock_id}）\n【今日無訊號】無核心分點 tape。",
        }

    yday = pool.prev_trading_day(conn, spec.stock_id, asof)
    events_today = pool.core_nets_on(conn, spec, asof)
    events_yday = pool.core_nets_on(conn, spec, yday) if yday else []
    fired = pool.evaluate_rules(spec, events_today, events_yday, yday=yday)
    email_hits = [f for f in fired if f.get("email")]
    rule_lb, _, _ = pool.rule_labels(spec)
    sig_close = pool.close_on(conn, spec.stock_id, asof)
    closes = pool.closes_through(conn, spec.stock_id, asof, n=5)
    sma5 = (sum(closes) / len(closes)) if len(closes) >= 5 else None
    registry = pool.load_registry_by_sid()
    yellow_on = pool.yellow_flag_enabled(cli=yellow_annotation)
    yellow = pool.maybe_detect_yellow(
        conn,
        spec,
        asof=asof,
        yday=yday,
        registry=registry,
        enabled=yellow_on,
    )
    if yellow_on:
        print(
            f"[{spec.stock_id}] yellow_annotation="
            f"{'FIRE' if yellow else 'quiet'} (research-only)"
        )
    detail = pool.format_alert_body(
        spec=spec,
        asof=asof,
        fired=fired,
        tape_note=tape_note,
        sig_close=sig_close,
        sma5=sma5,
        registry=registry,
        yellow=yellow,
        include_ops_ref=False,  # once at digest footer
    )
    if email_hits:
        head = f"【有訊號】{spec.stock_name}（{spec.stock_id}）· {asof} · {rule_lb}"
    else:
        head = f"【今日無訊號】{spec.stock_name}（{spec.stock_id}）· {asof} · {rule_lb}"
    body = f"{head}\nTape：{tape_note}\n\n{detail}"
    pool.write_daily_report(spec=spec, asof=asof, fired=fired, body=body)
    return {
        "id": spec.stock_id,
        "title": f"{spec.stock_name}專家池",
        "asof": asof,
        "fired": bool(email_hits),
        "yellow": bool(yellow),
        "summary": "觸發" if email_hits else "無訊號",
        "body": body,
    }


def collect_songshan_section(conn, *, asof_arg: str, do_refresh: bool) -> dict:
    return song.run_branch_watch(
        conn, branch_key="songshan", asof_arg=asof_arg, do_refresh=do_refresh
    )


def collect_xindian_section(conn, *, asof_arg: str, do_refresh: bool) -> dict:
    return song.run_branch_watch(
        conn, branch_key="xindian", asof_arg=asof_arg, do_refresh=do_refresh
    )


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="夜間研究觀測合併 digest")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--asof", default="")
    ap.add_argument("--refresh-days", type=int, default=5)
    ap.add_argument("--no-refresh", action="store_true")
    ap.add_argument("--state-dir", type=Path, default=DATA_DIR / "scratch")
    ap.add_argument("--force-email", action="store_true")
    ap.add_argument("--bootstrap-only", action="store_true")
    ap.add_argument(
        "--yellow-annotation",
        action="store_true",
        help=(
            "Top10 light2 黃燈研究註記寫入專家池區塊"
            "（或設 EXPERT_POOL_YELLOW_EMAIL_ANNOTATION=1；預設關）"
        ),
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="只寫 report／印摘要，不寄信、不寫 digest state",
    )
    return ap.parse_args()


def main() -> int:
    load_project_dotenv()
    args = parse_args()
    refresh_n = 0 if args.no_refresh else max(0, int(args.refresh_days))
    state_path = args.state_dir / STATE_NAME
    state = load_state(state_path)
    notified: dict = state.setdefault("notified", {})

    conn = connect(args.db)
    try:
        sections: list[dict] = []
        # Expert pools first
        for sid, spec in pool.POOLS.items():
            try:
                sections.append(
                    collect_pool_section(
                        conn,
                        spec,
                        asof_arg=args.asof,
                        refresh_n=refresh_n,
                        yellow_annotation=bool(args.yellow_annotation),
                    )
                )
            except Exception as exc:  # noqa: BLE001
                print(f"[{sid}] ERROR: {exc}", file=sys.stderr)
                sections.append(
                    {
                        "id": sid,
                        "title": f"{getattr(spec, 'stock_name', sid)}專家池",
                        "asof": None,
                        "fired": False,
                        "summary": f"ERROR {exc}",
                        "body": f"【錯誤】{sid}: {exc}",
                    }
                )

        # Songshan + Xindian (≥1.5億 ∩ !mega)
        for collector, err_id, err_title in (
            (collect_songshan_section, "9217", "凱基松山"),
            (collect_xindian_section, "9661", "富邦新店"),
        ):
            try:
                sections.append(
                    collector(
                        conn,
                        asof_arg=args.asof,
                        do_refresh=not args.no_refresh,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                print(f"[{err_id}] ERROR: {exc}", file=sys.stderr)
                sections.append(
                    {
                        "id": err_id,
                        "title": err_title,
                        "asof": None,
                        "fired": False,
                        "summary": f"ERROR {exc}",
                        "body": f"【錯誤】{err_title}: {exc}",
                    }
                )

        # Tier A independent entry (strong) + seat-exit (always brush)
        asofs_so_far = [s["asof"] for s in sections if s.get("asof")]
        tier_asof = (
            args.asof.strip()
            or (max(asofs_so_far) if asofs_so_far else "")
            or song.latest_trading_day(conn)
        )
        try:
            sections.extend(tier_a.collect_sections(conn, tier_asof))
        except Exception as exc:  # noqa: BLE001
            print(f"[tier-a] ERROR: {exc}", file=sys.stderr)
            sections.append(
                {
                    "id": "tier-a",
                    "title": "Tier A 獨立增量",
                    "asof": tier_asof or None,
                    "fired": False,
                    "summary": f"ERROR {exc}",
                    "body": f"【錯誤】Tier A 獨立增量: {exc}",
                }
            )
    finally:
        conn.close()

    asofs = [s["asof"] for s in sections if s.get("asof")]
    asof = max(asofs) if asofs else (args.asof.strip() or datetime.now().date().isoformat())
    any_fire = any(bool(s.get("fired")) for s in sections)
    n_fire = sum(1 for s in sections if s.get("fired"))

    lines = [
        f"夜間研究觀測合併 · {asof}",
        f"產生：{datetime.now().isoformat(timespec='seconds')}",
        f"項目數：{len(sections)} · 有訊號：{n_fire}",
        "性質：Research 觀測 · 未採納 · 不下單",
        "",
        "—— 總覽 ——",
    ]
    for s in sections:
        mark = "★" if s.get("fired") else "·"
        lines.append(f"{mark} {s['title']}: {s['summary']}")
    lines.append("")
    lines.append("=" * 48)
    for s in sections:
        lines.append("")
        lines.append(s["body"])
        lines.append("")
        lines.append("-" * 48)
    lines.append("")
    lines.extend(pool.format_ops_reference_footer())
    lines.append("腳本：scripts/research/run_evening_research_watch_digest.py")
    body = "\n".join(lines)

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report = REPORT_DIR / f"digest_{asof.replace('-', '')}.md"
    report.write_text(
        f"# 夜間觀測 digest · {asof}\n\n```\n{body}\n```\n",
        encoding="utf-8",
    )
    print(f"report={report}")
    for s in sections:
        yel = " yellow=FIRE" if s.get("yellow") else ""
        print(
            f"  {s['title']}: fired={s.get('fired')} asof={s.get('asof')} "
            f"{s.get('summary')}{yel}"
        )

    if args.dry_run:
        print("dry-run: skip email and state")
        return 0

    digest_key = f"digest|{asof}"
    if args.bootstrap_only or (not notified and not args.force_email):
        notified[digest_key] = {
            "at": datetime.now().isoformat(timespec="seconds"),
            "bootstrap": True,
            "n_fire": n_fire,
        }
        state["bootstrapped_asof"] = asof
        save_state(state_path, state)
        print("bootstrap state written (no email)")
        return 0

    prev = notified.get(digest_key) or {}
    # bootstrap-only marks must NOT block the real 20:00 send
    if prev and not prev.get("bootstrap") and not args.force_email:
        print(f"already sent digest for {asof}; skip email")
        return 0

    if not _email_enabled():
        print("email skipped: evening/expert/songshan flags all off or blocked")
        notified[digest_key] = {
            "at": datetime.now().isoformat(timespec="seconds"),
            "skipped_email": True,
            "n_fire": n_fire,
        }
        save_state(state_path, state)
        return 0

    if any_fire:
        subject = f"[ETF研究] 夜間觀測 · {asof} · 有訊號×{n_fire}"
    else:
        subject = f"[ETF研究] 夜間觀測 · {asof} · 今日皆無訊號"

    send_alert(subject, body)
    print(f"email sent: {subject}")
    notified[digest_key] = {
        "at": datetime.now().isoformat(timespec="seconds"),
        "n_fire": n_fire,
        "bootstrap": False,
    }
    state["updated_at"] = datetime.now().isoformat(timespec="seconds")
    save_state(state_path, state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
