"""Job completion email via Gmail SMTP or optional GAS webhook."""

from __future__ import annotations

import hashlib
import mimetypes
import os
import smtplib
from datetime import datetime
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

_TPE = ZoneInfo("Asia/Taipei")


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _gmail_password() -> str:
    return _env("GMAIL_APP_PASSWORD") or _env("GMAIL_APP")


def _smtp_send(
    subject: str,
    body: str,
    *,
    html_body: str | None = None,
    inline_images: list[tuple[str, Path]] | None = None,
    to: str | None = None,
) -> None:
    to_addr = (to or "").strip() or _env("GMAIL_NOTIFY_TO") or _env("GMAIL_USER")
    from_addr = _env("GMAIL_NOTIFY_FROM") or _env("GMAIL_USER")
    password = _gmail_password()
    if not all([to_addr, from_addr, password]):
        raise RuntimeError(
            "Gmail 未設定。請在 .env 加入 GMAIL_USER、GMAIL_APP_PASSWORD、"
            "GMAIL_NOTIFY_TO（寄件可用同帳號）。"
            "App 密碼：https://myaccount.google.com/apppasswords"
        )

    images = [(cid, Path(p)) for cid, p in (inline_images or []) if Path(p).is_file()]
    if html_body or images:
        msg: MIMEMultipart | MIMEText = MIMEMultipart("related")
        msg["Subject"] = subject
        msg["From"] = from_addr
        msg["To"] = to_addr
        alt = MIMEMultipart("alternative")
        msg.attach(alt)
        alt.attach(MIMEText(body, "plain", "utf-8"))
        if html_body:
            alt.attach(MIMEText(html_body, "html", "utf-8"))
        for cid, path in images:
            ctype, _ = mimetypes.guess_type(str(path))
            _main, subtype = (ctype or "image/png").split("/", 1)
            img = MIMEImage(path.read_bytes(), _subtype=subtype)
            img.add_header("Content-ID", f"<{cid}>")
            img.add_header("Content-Disposition", "inline", filename=path.name)
            msg.attach(img)
    else:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = from_addr
        msg["To"] = to_addr

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60) as smtp:
        smtp.login(from_addr, password)
        smtp.send_message(msg)


def _webhook_send(subject: str, body: str, *, success: bool) -> None:
    url = _env("NOTIFY_WEBHOOK_URL")
    if not url:
        raise RuntimeError("NOTIFY_WEBHOOK_URL is empty")
    secret = _env("NOTIFY_WEBHOOK_SECRET")
    payload = {
        "subject": subject,
        "body": body,
        "success": success,
        "secret": secret,
    }
    resp = requests.post(url, json=payload, timeout=30)
    resp.raise_for_status()


def _maybe_ops_digest_alert(subject: str, body: str) -> None:
    """Parallel Inbox wall for send_alert (evening watches, thermo, …).

    Gated by RUN_OPS_DIGEST_SYNC (default on). Never raises — email path wins.
    job_notify / notify_job_result already walls separately via job: keys.
    """
    if os.environ.get("RUN_OPS_DIGEST_SYNC", "1").strip() in ("0", "false", "False"):
        return
    try:
        from ops_console_sync import supabase_ops_configured, upsert_digest
    except Exception as exc:  # noqa: BLE001
        import sys

        print(f"ops digest import skip: {exc}", file=sys.stderr)
        return
    if not supabase_ops_configured():
        return

    day = datetime.now(_TPE).date().isoformat()
    digest_key = f"alert:{hashlib.sha1(f'{day}|{subject}'.encode()).hexdigest()[:16]}"
    text = (body or "").strip()
    if len(text) > 20000:
        text = text[:20000] + "\n\n…(truncated)"
    low = subject.lower()
    severity = "info"
    if any(tok in low for tok in ("失敗", "fail", "error", "拒單", "w0_hard", "有訊號")):
        severity = "warn"
    if "red_latched" in low or ("detach" in low and "red" in low):
        severity = "red"
    try:
        upsert_digest(
            digest_key=digest_key,
            body_md=text or f"# {subject}\n",
            subject=subject,
            channel="email",
            severity=severity,
            meta={"source": "send_alert"},
        )
        print(f"ops.digests ok · {digest_key}")
    except Exception as exc:  # noqa: BLE001
        import sys

        print(f"ops digest skip: {exc}", file=sys.stderr)


def send_alert(
    subject: str,
    body: str,
    *,
    html_body: str | None = None,
    inline_images: list[tuple[str, Path]] | None = None,
    to: str | None = None,
) -> None:
    """短訊息通知（Gmail SMTP 或 NOTIFY_WEBHOOK_URL）。

    html_body / inline_images / to 僅走 SMTP；若同時設了 webhook 且需要圖文，改走 SMTP.
    When RUN_OPS_DIGEST_SYNC=1 and ops credentials exist, also upsert ops.digests
    (before SMTP so a mail failure can still leave an Inbox trail).

    RUN_ALERT_EMAIL=0 → skip SMTP/webhook（仍可上牆）；用於夜間觀測波收斂成 20:40 一封摘要。
    """
    _maybe_ops_digest_alert(subject, body)
    if _env("RUN_ALERT_EMAIL") in ("0", "false", "False"):
        print(f"email skipped: RUN_ALERT_EMAIL=0 · subject={subject}")
        return
    rich = bool(html_body or inline_images or to)
    if _env("NOTIFY_WEBHOOK_URL") and not rich:
        _webhook_send(subject, body, success=True)
        return
    _smtp_send(
        subject,
        body,
        html_body=html_body,
        inline_images=inline_images,
        to=to,
    )


def send_job_result(
    *,
    subject_prefix: str,
    success: bool,
    log_path: str | Path,
    extra: str = "",
) -> None:
    status = "成功" if success else "失敗"
    subject = f"[股票研究] {subject_prefix} · {status}"
    log_tail = ""
    path = Path(log_path)
    if path.is_file() and os.environ.get("JOB_NOTIFY_LOG_TAIL", "1").strip() not in (
        "0",
        "false",
        "False",
    ):
        text = path.read_text(encoding="utf-8", errors="replace")
        log_tail = text[-4000:] if len(text) > 4000 else text
    body_parts = [f"{subject_prefix} {status}", extra.strip()]
    if log_tail:
        body_parts.extend(["", "--- log tail ---", log_tail])
    body = "\n\n".join(p for p in body_parts if p).strip()
    if _env("NOTIFY_WEBHOOK_URL"):
        _webhook_send(subject, body, success=success)
    else:
        _smtp_send(subject, body)
