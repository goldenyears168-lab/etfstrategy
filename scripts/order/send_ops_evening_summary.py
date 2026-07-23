#!/usr/bin/env python3
"""夜間觀測波 · 一封摘要信（20:40 ops-console-evening-sync 尾端）.

前置 job（20:00–20:35）設 RUN_ALERT_EMAIL=0：細節上牆 Inbox，不個別 SMTP。
本腳本彙整當日 ops.digests / snapshots，寄一封摘要（重大／RED 仍由原路徑即時寄）。

  PYTHONPATH=src .venv/bin/python scripts/order/send_ops_evening_summary.py
  PYTHONPATH=src .venv/bin/python scripts/order/send_ops_evening_summary.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from notify_email import send_alert  # noqa: E402
from ops_console_sync import (  # noqa: E402
    ensure_dotenv,
    supabase_ops_configured,
    upsert_digest,
)
from project_dotenv import load_project_dotenv  # noqa: E402

_TPE = ZoneInfo("Asia/Taipei")
SITE = "https://haoshi-quant-ops.pages.dev"


def _get(url: str, key: str) -> list[dict]:
    req = urllib.request.Request(
        url,
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
            "Accept-Profile": "public",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode())
    return data if isinstance(data, list) else []


def main() -> int:
    ap = argparse.ArgumentParser(description="Evening ops one-mail summary")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--asof", default="", help="YYYY-MM-DD · default today TPE")
    args = ap.parse_args()

    load_project_dotenv()
    ensure_dotenv()
    if os.environ.get("RUN_OPS_EVENING_SUMMARY_EMAIL", "1").strip() in (
        "0",
        "false",
        "False",
    ):
        print("evening summary email disabled: RUN_OPS_EVENING_SUMMARY_EMAIL=0")
        return 0

    asof = (args.asof or "").strip() or datetime.now(_TPE).date().isoformat()
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    lines: list[str] = [
        f"夜間觀測摘要 · {asof}",
        f"網站：{SITE}",
        "細節已上牆 Inbox／知識庫；本信為收斂摘要（20:40）。",
        "",
    ]
    n_warn = 0
    n_red = 0

    if supabase_ops_configured() and url and key:
        digests = _get(
            f"{url}/rest/v1/ops_digests?select=digest_key,subject,severity,asof,channel"
            f"&asof=gte.{asof}T00:00:00%2B08:00&order=asof.asc&limit=80",
            key,
        )
        # Prefer today's wall items; skip this summary's own key if re-run
        bullets: list[str] = []
        for d in digests:
            dk = str(d.get("digest_key") or "")
            if dk.startswith("evening-summary:"):
                continue
            if dk.startswith("announce:"):
                continue
            sev = str(d.get("severity") or "info")
            if sev == "warn":
                n_warn += 1
            if sev in ("red", "error"):
                n_red += 1
            subj = d.get("subject") or dk
            bullets.append(f"- [{sev}] {subj}")
        if bullets:
            lines.append(f"今日 Inbox 條目（{len(bullets)}）")
            lines.extend(bullets)
        else:
            lines.append("今日 Inbox 尚無條目（或尚未上牆）。")

        snaps = _get(
            f"{url}/rest/v1/ops_snapshots?select=kind,asof,payload"
            f"&or=(kind.eq.thermo,kind.eq.watch,kind.eq.risk,kind.eq.today)"
            f"&order=asof.desc&limit=8",
            key,
        )
        seen: set[str] = set()
        lines.append("")
        lines.append("快照")
        for s in snaps:
            kind = str(s.get("kind") or "")
            if kind in seen:
                continue
            seen.add(kind)
            payload = s.get("payload") or {}
            if not isinstance(payload, dict):
                payload = {}
            if kind == "thermo":
                lines.append(
                    f"- 溫度計：{payload.get('temp_pct')}% {payload.get('lamp')} "
                    f"（asof {payload.get('asof_date') or asof}）"
                )
            elif kind == "watch":
                lines.append(f"- Watch 訊號：{payload.get('n_fires', payload.get('fires', '—'))}")
            elif kind == "risk":
                lines.append(
                    f"- 風控：{payload.get('level') or payload.get('risk_level') or payload.get('status') or '—'}"
                )
            elif kind == "today":
                lines.append(
                    f"- Today：risk={payload.get('risk_level')} "
                    f"thermo={payload.get('thermo_temp_pct')} "
                    f"watch={payload.get('watch_fires')}"
                )
    else:
        lines.append("（Supabase 未設定 · 僅提示開網站）")

    lines.extend(
        [
            "",
            "時段：18:30 資料 → 20:00–20:35 觀測上牆 → 20:40 本摘要＋溫度計當日。",
            "重大／Detach RED 仍即時另寄，不併入本封延遲。",
        ]
    )
    body = "\n".join(lines)
    if n_red:
        subject = f"[股票研究] 夜間摘要 · {asof} · RED×{n_red}"
    elif n_warn:
        subject = f"[股票研究] 夜間摘要 · {asof} · 留意×{n_warn}"
    else:
        subject = f"[股票研究] 夜間摘要 · {asof} · 平靜"

    digest_key = f"evening-summary:{asof}"
    if not args.dry_run:
        # Force SMTP for this one mail even if wave set RUN_ALERT_EMAIL=0
        prev = os.environ.get("RUN_ALERT_EMAIL")
        os.environ["RUN_ALERT_EMAIL"] = "1"
        try:
            send_alert(subject, body)
        finally:
            if prev is None:
                os.environ.pop("RUN_ALERT_EMAIL", None)
            else:
                os.environ["RUN_ALERT_EMAIL"] = prev
        # Stable key for Inbox (send_alert also wrote alert: hash)
        upsert_digest(
            digest_key=digest_key,
            subject=subject,
            body_md=body,
            severity="red" if n_red else ("warn" if n_warn else "info"),
            channel="evening_summary",
            meta={"source": "send_ops_evening_summary", "asof": asof},
        )
        print(f"sent + wall: {subject}")
    else:
        print(f"dry-run: {subject}\n{body}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except urllib.error.HTTPError as exc:
        print(f"HTTP {exc.code}: {exc.read()[:300]!r}", file=sys.stderr)
        raise SystemExit(1) from exc
