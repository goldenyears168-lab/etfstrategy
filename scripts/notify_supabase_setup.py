#!/usr/bin/env python3
"""Email after Supabase stock research sync is live on 好時官網預約 project."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from project_dotenv import load_project_dotenv
from notify_email import send_alert


def main() -> int:
    load_project_dotenv()
    body = """股市研究 · Supabase 已就緒

【專案】好時官網預約 · lzaomqzsiqudkojokevr（名稱維持不變）
https://supabase.com/dashboard/project/lzaomqzsiqudkojokevr/editor

【資料表分離】
  · 官網預約：public.booking_logs, public.product_* …
  · 股市研究：stock_research.daily_briefs（獨立 schema）

【已回填】
  · 1300 vcp_funnel_specs（2026-06-20）
  · 1630 etf_daily（2026-06-21）
  · 1630 regime_daily（2026-06-18）
  · rrg_mono_intraday：本機尚無檔案，下次 13:00 排程會自動同步

【查詢範例】
  select trade_date, schedule_slot, brief_type, title
  from stock_research.daily_briefs
  order by trade_date desc;

【本機 .env 請補】（Settings → API → service_role）
  SUPABASE_URL=https://lzaomqzsiqudkojokevr.supabase.co
  SUPABASE_PROJECT_REF=lzaomqzsiqudkojokevr
  SUPABASE_SERVICE_ROLE_KEY=<service_role>
  RUN_SUPABASE_RESEARCH_SYNC=1

之後 13:00 / 16:30 排程完成會自動 upsert + 原有 Gmail 通知。
"""
    try:
        send_alert("[ETF研究] Supabase 股市研究同步已就緒", body)
        print("email sent")
        return 0
    except Exception as exc:
        print(f"email failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
