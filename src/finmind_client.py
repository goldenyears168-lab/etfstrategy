"""FinMind API 共用客戶端（data + tick snapshot）。"""

from __future__ import annotations

import os
from datetime import date

import requests

FINMIND_DATA_URL = "https://api.finmindtrade.com/api/v4/data"
FINMIND_TICK_SNAPSHOT_URL = (
    "https://api.finmindtrade.com/api/v4/taiwan_stock_tick_snapshot"
)
FINMIND_FUTURES_SNAPSHOT_URL = (
    "https://api.finmindtrade.com/api/v4/taiwan_futures_snapshot"
)
FINMIND_TRADING_DAILY_REPORT_URL = (
    "https://api.finmindtrade.com/api/v4/taiwan_stock_trading_daily_report"
)


def finmind_token() -> str:
    from project_dotenv import finmind_token_from_env

    return finmind_token_from_env()


def finmind_headers() -> dict[str, str]:
    token = finmind_token()
    if not token:
        return {}
    return {"Authorization": f"Bearer {token}"}


def fetch_finmind_json(
    params: dict,
    *,
    url: str = FINMIND_DATA_URL,
    timeout: float = 60,
) -> dict:
    """GET FinMind JSON.

    ``/api/v4/data`` uses ``token`` query param (not Bearer): Bearer has triggered
    ``ip banned`` for this account while the same token in params works.
    Snapshot endpoints still use ``Authorization: Bearer``.
    """
    req_params = dict(params)
    headers: dict[str, str] = {}
    token = finmind_token()
    if token and url.rstrip("/").endswith("/data"):
        req_params.setdefault("token", token)
    elif token:
        headers = finmind_headers()
    resp = requests.get(
        url,
        params=req_params,
        headers=headers,
        timeout=timeout,
    )
    try:
        resp.raise_for_status()
    except requests.HTTPError as exc:
        # requests.HTTPError.__str__ 包含完整 request URL（含 token query param）；
        # 用 status code + response body 重包一份不含 URL 的錯誤，避免 token 外流到 log/終端。
        raise RuntimeError(
            f"FinMind HTTP {resp.status_code}: {resp.text[:500]}"
        ) from exc
    payload = resp.json()
    if payload.get("status") != 200:
        raise RuntimeError(payload.get("msg", "FinMind error"))
    return payload


def fetch_finmind(
    dataset: str,
    data_id: str,
    start: date,
    end: date,
    *,
    timeout: float = 60,
) -> list[dict]:
    """依 dataset + data_id + 日期區間拉資料列。"""
    payload = fetch_finmind_json(
        {
            "dataset": dataset,
            "data_id": data_id,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
        },
        timeout=timeout,
    )
    return payload.get("data") or []


def fetch_finmind_dataset(
    dataset: str,
    *,
    data_id: str | None = None,
    start: date | None = None,
    end: date | None = None,
    timeout: float = 60,
) -> list[dict]:
    """通用 dataset 查詢（如 TaiwanStockInfo 無 data_id）。"""
    params: dict[str, str] = {"dataset": dataset}
    if data_id is not None:
        params["data_id"] = data_id
    if start is not None:
        params["start_date"] = start.isoformat()
    if end is not None:
        params["end_date"] = end.isoformat()
    payload = fetch_finmind_json(params, timeout=timeout)
    return payload.get("data") or []


def fetch_taiwan_various_indicators_5s(
    trade_date: date,
    *,
    timeout: float = 60,
) -> list[dict]:
    """FinMind ``TaiwanVariousIndicators5Seconds`` · single trading day.

    Columns: ``date`` (``YYYY-MM-DD HH:MM:SS`` Asia/Taipei), ``TAIEX`` (float).
    Dataset is single-day per request (``start_date`` = ``end_date``).
    """
    return fetch_finmind_dataset(
        "TaiwanVariousIndicators5Seconds",
        start=trade_date,
        end=trade_date,
        timeout=timeout,
    )


def fetch_taiwan_stock_statistics_of_order_book_and_trade(
    trade_date: date,
    *,
    timeout: float = 60,
) -> list[dict]:
    """FinMind ``TaiwanStockStatisticsOfOrderBookAndTrade`` · 每 5 秒委託成交統計。

    全市場彙總（無 ``data_id``，帶 data_id 會回 400 ``don't provide``），
    僅提供當天資料（單日），首筆 ``Time`` 為 ``09:00:00``（不含 08:30-09:00 盤前試撮）。
    Columns: ``Time``, ``TotalBuyOrder``, ``TotalBuyVolume``, ``TotalSellOrder``,
    ``TotalSellVolume``, ``TotalDealOrder``, ``TotalDealVolume``, ``TotalDealMoney``, ``date``。
    """
    return fetch_finmind_dataset(
        "TaiwanStockStatisticsOfOrderBookAndTrade",
        start=trade_date,
        end=trade_date,
        timeout=timeout,
    )


def fetch_taiwan_stock_trading_daily_report(
    *,
    trade_date: date | str,
    securities_trader_id: str | None = None,
    data_id: str | None = None,
    timeout: float = 60,
) -> list[dict]:
    """FinMind 分點日報（單日）。可依券商代碼或股票代碼查詢。

    Endpoint: ``/api/v4/taiwan_stock_trading_daily_report``（非 ``/data``）。
    """
    if not securities_trader_id and not data_id:
        raise ValueError("need securities_trader_id or data_id")
    day = (
        trade_date
        if isinstance(trade_date, str)
        else trade_date.isoformat()
    )
    params: dict[str, str] = {"date": day[:10]}
    if securities_trader_id:
        params["securities_trader_id"] = str(securities_trader_id)
    if data_id:
        params["data_id"] = str(data_id)
    payload = fetch_finmind_json(
        params,
        url=FINMIND_TRADING_DAILY_REPORT_URL,
        timeout=timeout,
    )
    return payload.get("data") or []


def fetch_tick_snapshots(
    data_ids: list[str],
    *,
    timeout: float = 30,
) -> tuple[list[dict], str | None]:
    """盤中 tick snapshot；需有效 FINMIND_TOKEN。

    FinMind 批次 data_id 若任一代號異常可能整包失敗；改逐檔查詢。
    """
    if not finmind_token():
        return [], "未設定 FINMIND_TOKEN"
    rows: list[dict] = []
    last_err: str | None = None
    for data_id in data_ids:
        sid = str(data_id).strip()
        if not sid:
            continue
        try:
            resp = requests.get(
                FINMIND_TICK_SNAPSHOT_URL,
                headers=finmind_headers(),
                params={"data_id": sid},
                timeout=timeout,
            )
            payload = resp.json()
        except requests.RequestException as exc:
            last_err = str(exc)
            continue
        if payload.get("status") != 200:
            last_err = payload.get("msg", f"HTTP {resp.status_code}")
            continue
        rows.extend(payload.get("data") or [])
    if not rows and last_err:
        return [], last_err
    return rows, None


def fetch_all_tick_snapshots(*, timeout: float = 30) -> tuple[list[dict], str | None]:
    """全市場 tick snapshot（單次呼叫，data_id 留空 = 全部個股，官方文件說明支援）。

    比逐檔查詢（見 fetch_tick_snapshots）省下上千次 API 呼叫，適合需要「全市場覆蓋」
    的場景（例如盤前試撮 snapshot 收集器）。若失敗，呼叫端可自行 fallback 回逐檔查詢。
    """
    if not finmind_token():
        return [], "未設定 FINMIND_TOKEN"
    try:
        resp = requests.get(
            FINMIND_TICK_SNAPSHOT_URL,
            headers=finmind_headers(),
            timeout=timeout,
        )
        payload = resp.json()
    except requests.RequestException as exc:
        return [], str(exc)
    if payload.get("status") != 200:
        return [], payload.get("msg", f"HTTP {resp.status_code}")
    return payload.get("data") or [], None


def fetch_futures_snapshots(
    data_ids: list[str],
    *,
    timeout: float = 30,
) -> tuple[list[dict], str | None]:
    """期貨即時 snapshot；需有效 FINMIND_TOKEN（通常 Sponsor tier）。

    FinMind 批次 ``data_id=[A,B]`` 時，若任一代號無資料會整包回空；改為逐一代號查詢再合併。
    """
    if not finmind_token():
        return [], "未設定 FINMIND_TOKEN"
    rows: list[dict] = []
    last_err: str | None = None
    for data_id in data_ids:
        sid = str(data_id).strip()
        if not sid:
            continue
        try:
            resp = requests.get(
                FINMIND_FUTURES_SNAPSHOT_URL,
                headers=finmind_headers(),
                params={"data_id": sid},
                timeout=timeout,
            )
            payload = resp.json()
        except requests.RequestException as exc:
            last_err = str(exc)
            continue
        if payload.get("status") != 200:
            last_err = payload.get("msg", f"HTTP {resp.status_code}")
            continue
        rows.extend(payload.get("data") or [])
    if not rows and last_err:
        return [], last_err
    return rows, None
