"""Perplexity Chat Completions 共用客戶端（搜尋 + 結構化輸出）。"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

import requests

PERPLEXITY_URL = "https://api.perplexity.ai/chat/completions"
JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)
FORBIDDEN_RATING_RE = re.compile(
    r"\b(BUY|HOLD|TRIM|STRONG\s+BUY|SELL)\b|買進|賣出|加碼買|減碼賣|目標價|建議買|建議賣",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class PerplexityConfig:
    api_key: str
    model: str
    timeout: int


def get_config(*, model_env: str = "PERPLEXITY_MODEL") -> PerplexityConfig | None:
    api_key = os.environ.get("PERPLEXITY_API_KEY", "").strip()
    if not api_key:
        return None
    model = os.environ.get(model_env, os.environ.get("PERPLEXITY_MODEL", "sonar")).strip()
    if not model:
        model = "sonar"
    try:
        timeout = int(os.environ.get("PERPLEXITY_TIMEOUT", "120"))
    except ValueError:
        timeout = 120
    return PerplexityConfig(api_key=api_key, model=model, timeout=timeout)


def chat_completion(
    messages: list[dict[str, str]],
    *,
    cfg: PerplexityConfig | None = None,
    temperature: float = 0.2,
) -> str:
    conf = cfg or get_config()
    if conf is None:
        raise RuntimeError("PERPLEXITY_API_KEY 未設定")
    resp = requests.post(
        PERPLEXITY_URL,
        headers={
            "Authorization": f"Bearer {conf.api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": conf.model,
            "messages": messages,
            "temperature": temperature,
        },
        timeout=conf.timeout,
    )
    resp.raise_for_status()
    body = resp.json()
    return str(body["choices"][0]["message"]["content"])


def extract_json_payload(text: str) -> dict | list | None:
    text = text.strip()
    match = JSON_BLOCK_RE.search(text)
    if match:
        text = match.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                return None
        start = text.find("[")
        end = text.rfind("]")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                return None
    return None


def audit_narrative(text: str) -> tuple[bool, list[str]]:
    notes: list[str] = []
    if FORBIDDEN_RATING_RE.search(text):
        notes.append("投資評級/目標價")
    return (len(notes) == 0, notes)
