"""Load project-root .env (override stale shell placeholders)."""

from __future__ import annotations

import os
import shlex
from pathlib import Path

from stock_db import PROJECT_ROOT

_PLACEHOLDER_TOKENS = frozenset({"your_token_here", "changeme", ""})


def _parse_dotenv_line(raw: str) -> tuple[str, str] | None:
    line = raw.strip()
    if not line or line.startswith("#") or "=" not in line:
        return None
    key, value = line.split("=", 1)
    key = key.strip()
    if not key:
        return None
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return key, value


def parse_dotenv_file(path: Path) -> list[tuple[str, str]]:
    if not path.is_file():
        return []
    pairs: list[tuple[str, str]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        parsed = _parse_dotenv_line(raw)
        if parsed is not None:
            pairs.append(parsed)
    return pairs


def shell_export_dotenv(path: Path | None = None) -> str:
    """產生可 eval 的 export 區塊（正確處理含空格的值）。"""
    env_path = path or (PROJECT_ROOT / ".env")
    return "\n".join(
        f"export {key}={shlex.quote(value)}" for key, value in parse_dotenv_file(env_path)
    )


def load_project_dotenv(
    path: Path | None = None,
    *,
    override: bool = True,
) -> None:
    """載入 .env；預設覆寫 shell 內殘留的 placeholder（如 FINMIND_TOKEN=your_token_here）。"""
    env_path = path or (PROJECT_ROOT / ".env")
    for key, value in parse_dotenv_file(env_path):
        if override:
            os.environ[key] = value
        else:
            os.environ.setdefault(key, value)


def finmind_token_from_env() -> str:
    """FINMIND_TOKEN；若 shell 為 placeholder 則先重載 .env。"""
    token = os.environ.get("FINMIND_TOKEN", "").strip()
    if token not in _PLACEHOLDER_TOKENS:
        return token
    load_project_dotenv(override=True)
    return os.environ.get("FINMIND_TOKEN", "").strip()
