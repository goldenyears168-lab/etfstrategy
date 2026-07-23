#!/usr/bin/env python3
"""分點 tape 每日補檔（POOLS ∪ 富邦持倉）· 18:30 launchd · observe only.

先把「常用分點」（專家池 POOLS＋目前富邦持倉）的當日分點 tape 補進 DB，
讓 20:00 起的專家池／持倉賣方監控排程可以直接讀 DB（--no-refresh），
不用各自重複打 FinMind、也不會被彼此的補檔時間差卡住。

失敗判定（觀測用，非下單）：
  - 補檔過程「真的出錯」（例外／API 失敗／DB 寫入失敗）→ 一律寄信
  - 當日資料筆數＝0 的股票數 > 一半 → 視為系統性異常（如 FinMind 尚未
    公布或來源出狀況），寄信；個別 1、2 檔 0 筆（可能單純無分點交易）
    不寄信，避免誤報。

  .venv-fubon/bin/python scripts/order/run_branch_tape_prewarm.py
  .venv-fubon/bin/python scripts/order/run_branch_tape_prewarm.py \\
    --holdings 2327:50 --no-fubon --dry-run --refresh-days 2
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts" / "research"))
sys.path.insert(0, str(ROOT / "scripts" / "order"))

from notify_email import send_alert  # noqa: E402
from project_dotenv import load_project_dotenv  # noqa: E402
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402

import run_expert_pool_watch as pool  # noqa: E402
import run_holdings_branch_sell_monitor as holdings_mod  # noqa: E402

ENV_FLAG = "RUN_BRANCH_TAPE_PREWARM_EMAIL"


def _email_enabled() -> bool:
    if ENV_FLAG in os.environ:
        return os.environ[ENV_FLAG].strip() not in ("0", "false", "False")
    return True


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="分點 tape 每日補檔（POOLS ∪ 富邦持倉）· observe only"
    )
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument(
        "--refresh-days", type=int, default=2, help="每檔補近 n 個日曆平日（預設 2）"
    )
    ap.add_argument(
        "--holdings",
        default="",
        help="離線持倉 SID:shares[,…]（搭配 --no-fubon；不影響 POOLS 部分）",
    )
    ap.add_argument("--no-fubon", action="store_true", help="不呼叫富邦 API（僅補 POOLS）")
    ap.add_argument("--dry-run", action="store_true", help="只印 stdout · 不寄信")
    ap.add_argument("--force-email", action="store_true", help="即使無異常也寄一封摘要信")
    return ap.parse_args()


def collect_target_sids(args: argparse.Namespace) -> dict[str, str]:
    """sid -> display name，來源＝POOLS ∪ 持倉（去重）。"""
    out: dict[str, str] = {}
    for sid, spec in pool.POOLS.items():
        out[str(sid)] = str(spec.stock_name or sid)

    holdings: list = []
    if args.holdings:
        try:
            holdings = holdings_mod.parse_holdings_cli(args.holdings)
        except ValueError as exc:
            print(f"警告：--holdings 格式錯誤，略過 — {exc}", file=sys.stderr)
    elif not args.no_fubon:
        try:
            holdings = holdings_mod.fetch_fubon_holdings()
        except (FileNotFoundError, ValueError, RuntimeError, ImportError) as exc:
            print(f"警告：富邦持倉無法取得，僅補 POOLS — {exc}", file=sys.stderr)

    for h in holdings:
        out.setdefault(h.stock_id, h.stock_name or h.stock_id)
    return out


def main() -> int:
    load_project_dotenv()
    args = parse_args()

    targets = collect_target_sids(args)
    if not targets:
        print("錯誤：POOLS 與持倉皆空，無股票可補", file=sys.stderr)
        return 1

    conn = connect(args.db)
    today_s = date.today().isoformat()
    hard_fails: list[str] = []
    zero_today: list[str] = []
    ok_today: list[str] = []
    try:
        for sid, name in sorted(targets.items()):
            days = list(
                reversed(pool.list_recent_trading_days(conn, stock_id=sid, n=args.refresh_days))
            )
            counts = pool.refresh_stock_tape(conn, sid, days)
            print(f"[{sid}] {name} refresh days={days} counts={counts}")
            if any(v == -1 for v in counts.values()):
                hard_fails.append(f"{name}（{sid}）")
            today_n = counts.get(today_s)
            if today_n is not None:
                if today_n == 0:
                    zero_today.append(f"{name}（{sid}）")
                else:
                    ok_today.append(f"{name}（{sid}）")
    finally:
        conn.close()

    n_total = len(targets)
    majority_zero = len(zero_today) > n_total / 2
    should_alert = bool(hard_fails) or majority_zero

    summary = (
        f"分點 tape 補檔 · {today_s}\n"
        f"targets: {n_total} 檔（POOLS ∪ 富邦持倉）· 今日有資料 {len(ok_today)} 檔 · "
        f"今日 0 筆 {len(zero_today)} 檔 · 補檔出錯 {len(hard_fails)} 檔\n"
    )
    print()
    print(summary)

    if hard_fails:
        print("補檔出錯：" + "、".join(hard_fails))
    if zero_today:
        print("今日 0 筆：" + "、".join(zero_today))

    if (should_alert or args.force_email) and not args.dry_run:
        if not _email_enabled():
            print(f"email skipped: {ENV_FLAG}=0")
        else:
            if hard_fails and majority_zero:
                tag = "補檔出錯＋大量0筆"
            elif hard_fails:
                tag = "補檔出錯"
            elif majority_zero:
                tag = "大量0筆"
            else:
                tag = "摘要"
            subject = f"[ETF研究][分點補檔][{tag}] {today_s}"
            body_lines = [summary]
            if hard_fails:
                body_lines.append("補檔出錯（例外／API／DB 寫入失敗）：")
                body_lines.extend(f"  - {x}" for x in hard_fails)
            if zero_today:
                body_lines.append("今日分點資料筆數＝0：")
                body_lines.extend(f"  - {x}" for x in zero_today)
            body_lines.append("")
            body_lines.append("腳本：scripts/order/run_branch_tape_prewarm.py")
            send_alert(subject, "\n".join(body_lines))
            print(f"email sent: {subject}")
    elif args.dry_run:
        print("dry-run: email skipped")

    return 1 if hard_fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
