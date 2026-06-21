"""etfedge.xyz MCP client (Streamable HTTP, Bearer token)."""

from __future__ import annotations

import json
import time
from typing import Any

import requests

DEFAULT_MCP_URL = "https://mcp.etfedge.xyz/mcp"
DEFAULT_MIN_INTERVAL_S = 3.2


class EtfedgeMcpError(RuntimeError):
    pass


class EtfedgeMcpClient:
    """Minimal sync client for etfedge MCP tools."""

    def __init__(
        self,
        token: str,
        *,
        url: str = DEFAULT_MCP_URL,
        min_interval_s: float = DEFAULT_MIN_INTERVAL_S,
        timeout_s: float = 120.0,
    ) -> None:
        token = token.strip()
        if not token:
            raise EtfedgeMcpError(
                "ETFEDGE_MCP_TOKEN is required. "
                "Connect https://mcp.etfedge.xyz/mcp via Cursor/Claude (GitHub OAuth), "
                "or set ETFEDGE_MCP_TOKEN in .env."
            )
        self.url = url.rstrip("/")
        self.token = token
        self.min_interval_s = min_interval_s
        self.timeout_s = timeout_s
        self.session_id: str | None = None
        self._req_id = 0
        self._last_call_at = 0.0
        self._http = requests.Session()

    def _next_id(self) -> int:
        self._req_id += 1
        return self._req_id

    def _headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        return headers

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_call_at
        if elapsed < self.min_interval_s:
            time.sleep(self.min_interval_s - elapsed)

    def _parse_sse(self, text: str) -> dict[str, Any]:
        last: dict[str, Any] | None = None
        for raw_line in text.replace("\r\n", "\n").split("\n"):
            line = raw_line.strip()
            if not line.startswith("data: "):
                continue
            payload = line[6:]
            try:
                last = json.loads(payload)
            except json.JSONDecodeError:
                continue
        if last is None:
            raise EtfedgeMcpError("MCP SSE response had no parseable data events")
        return last

    def _parse_body(self, resp: requests.Response) -> dict[str, Any]:
        if resp.headers.get("Mcp-Session-Id"):
            self.session_id = resp.headers["Mcp-Session-Id"]
        if resp.status_code == 202:
            return {}
        ct = resp.headers.get("content-type", "")
        if "text/event-stream" in ct:
            return self._parse_sse(resp.text)
        try:
            return resp.json()
        except json.JSONDecodeError as exc:
            raise EtfedgeMcpError(f"MCP response is not JSON: {resp.text[:200]}") from exc

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._throttle()
        resp = self._http.post(
            self.url,
            json=payload,
            headers=self._headers(),
            timeout=self.timeout_s,
        )
        self._last_call_at = time.monotonic()
        if resp.status_code == 401:
            raise EtfedgeMcpError(
                "MCP authentication failed (401). Refresh ETFEDGE_MCP_TOKEN via GitHub OAuth."
            )
        if resp.status_code >= 400:
            detail = resp.text[:300]
            raise EtfedgeMcpError(f"MCP HTTP {resp.status_code}: {detail}")
        body = self._parse_body(resp)
        if "error" in body:
            err = body["error"]
            raise EtfedgeMcpError(f"MCP error {err.get('code')}: {err.get('message')}")
        return body

    @staticmethod
    def _extract_tool_payload(body: dict[str, Any]) -> Any:
        result = body.get("result") or {}
        if "structuredContent" in result:
            return result["structuredContent"]
        for block in result.get("content") or []:
            if block.get("type") != "text":
                continue
            text = (block.get("text") or "").strip()
            if not text:
                continue
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text
        return result

    def connect(self) -> None:
        init_id = self._next_id()
        self._post(
            {
                "jsonrpc": "2.0",
                "id": init_id,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "etf-holdings-import", "version": "1.0"},
                },
            }
        )
        self._post(
            {
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
                "params": {},
            }
        )

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        req_id = self._next_id()
        body = self._post(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments or {}},
            }
        )
        return self._extract_tool_payload(body)

    def get_etf_holdings(self, etf: str) -> dict[str, Any]:
        return self.call_tool("get_etf_holdings", {"etf": etf})

    def get_etf_buy_delta(self, etf: str, start_date: str, end_date: str) -> dict[str, Any]:
        return self.call_tool(
            "get_etf_buy_delta",
            {"etf": etf, "start_date": start_date, "end_date": end_date},
        )

    def get_stock_history(self, etf: str, stock_code: str, days: int = 365) -> list[dict[str, Any]]:
        payload = self.call_tool(
            "get_stock_history",
            {"etf": etf, "stock_code": stock_code, "days": days},
        )
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            for key in ("result", "history", "rows"):
                rows = payload.get(key)
                if isinstance(rows, list):
                    return rows
        raise EtfedgeMcpError(f"unexpected get_stock_history payload: {type(payload)}")
