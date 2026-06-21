"""Job completion email via Gmail SMTP or optional GAS webhook."""

from __future__ import annotations

import os
import smtplib
from email.mime.text import MIMEText
from pathlib import Path

import requests


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _gmail_password() -> str:
    return _env("GMAIL_APP_PASSWORD") or _env("GMAIL_APP")


def _smtp_send(subject: str, body: str) -> None:
    to_addr = _env("GMAIL_NOTIFY_TO") or _env("GMAIL_USER")
    from_addr = _env("GMAIL_NOTIFY_FROM") or _env("GMAIL_USER")
    password = _gmail_password()
    if not all([to_addr, from_addr, password]):
        raise RuntimeError(
            "Gmail 未設定。請在 .env 加入 GMAIL_USER、GMAIL_APP_PASSWORD、"
            "GMAIL_NOTIFY_TO（寄件可用同帳號）。"
            "App 密碼：https://myaccount.google.com/apppasswords"
        )
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
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


def send_alert(subject: str, body: str) -> None:
    """短訊息通知（Gmail SMTP 或 NOTIFY_WEBHOOK_URL）。"""
    if _env("NOTIFY_WEBHOOK_URL"):
        _webhook_send(subject, body, success=True)
    else:
        _smtp_send(subject, body)


def send_job_result(
    *,
    subject_prefix: str,
    success: bool,
    log_path: str | Path,
    extra: str = "",
) -> None:
    status = "成功" if success else "失敗"
    subject = f"[ETF研究] {subject_prefix} · {status}"
    log_tail = ""
    path = Path(log_path)
    if path.is_file():
        text = path.read_text(encoding="utf-8", errors="replace")
        log_tail = text[-4000:] if len(text) > 4000 else text
    body = f"{subject_prefix} {status}\n\n{extra.strip()}\n\n--- log tail ---\n{log_tail}".strip()
    if _env("NOTIFY_WEBHOOK_URL"):
        _webhook_send(subject, body, success=success)
    else:
        _smtp_send(subject, body)
