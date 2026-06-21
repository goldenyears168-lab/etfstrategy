#!/usr/bin/env python3
"""Complete etfedge MCP GitHub OAuth and write ETFEDGE_MCP_TOKEN to .env.

Requires: pip install fastmcp 'py-key-value-aio[disk]' diskcache
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MCP_URL = "https://mcp.etfedge.xyz/mcp"
TOKEN_DIR = Path.home() / ".config" / "etfedge" / "oauth-tokens"


def _update_env(env_path: Path, token: str) -> None:
    lines: list[str] = []
    if env_path.is_file():
        lines = env_path.read_text(encoding="utf-8").splitlines()
    key = "ETFEDGE_MCP_TOKEN"
    new_line = f"{key}={token}"
    replaced = False
    out: list[str] = []
    for line in lines:
        if line.strip().startswith(f"{key}="):
            out.append(new_line)
            replaced = True
        else:
            out.append(line)
    if not replaced:
        if out and out[-1].strip():
            out.append("")
        out.append("# etfedge MCP（scripts/import_etfedge_holdings.py）")
        out.append(new_line)
    env_path.write_text("\n".join(out) + "\n", encoding="utf-8")


async def _login_and_fetch_token(mcp_url: str) -> str:
    try:
        from fastmcp import Client
        from fastmcp.client.auth import OAuth
        from key_value.aio.adapters.pydantic import PydanticAdapter
        from key_value.aio.stores.disk import DiskStore
        from mcp.shared.auth import OAuthToken
    except ImportError as exc:
        raise SystemExit(
            "Missing OAuth dependencies. Run:\n"
            "  .venv/bin/pip install fastmcp 'py-key-value-aio[disk]' diskcache"
        ) from exc

    TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    store = DiskStore(directory=str(TOKEN_DIR))
    oauth = OAuth(token_storage=store, callback_timeout=300.0)

    print(f"Connecting to {mcp_url}")
    print("If browser opens, sign in with GitHub and approve etfedge MCP access.")
    async with Client(mcp_url, auth=oauth) as client:
        tools = await client.list_tools()
        print(f"OK: {len(tools)} MCP tools available")

    adapter = PydanticAdapter[OAuthToken](
        default_collection="mcp-oauth-token",
        key_value=store,
        pydantic_model=OAuthToken,
        raise_on_validation_error=True,
    )
    tokens = await adapter.get(key=f"{mcp_url}/tokens")
    if tokens is None or not tokens.access_token:
        raise SystemExit("OAuth finished but access_token was not stored")
    return str(tokens.access_token)


def main() -> int:
    parser = argparse.ArgumentParser(description="etfedge MCP GitHub OAuth login")
    parser.add_argument(
        "--mcp-url",
        default=DEFAULT_MCP_URL,
        help=f"etfedge MCP endpoint (default: {DEFAULT_MCP_URL})",
    )
    parser.add_argument(
        "--env",
        type=Path,
        default=ROOT / ".env",
        help="Path to .env (default: project .env)",
    )
    parser.add_argument(
        "--print-only",
        action="store_true",
        help="Print token to stdout instead of writing .env",
    )
    args = parser.parse_args()

    token = asyncio.run(_login_and_fetch_token(args.mcp_url.rstrip("/")))
    if args.print_only:
        print(token)
        return 0

    _update_env(args.env, token)
    print(f"Wrote ETFEDGE_MCP_TOKEN to {args.env}")
    print(f"OAuth tokens cached at {TOKEN_DIR}")
    print("Next: .venv/bin/python scripts/import_etfedge_holdings.py --dry-run")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
