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
    resp = requests.get(
        url,
        params=params,
        headers=finmind_headers(),
        timeout=timeout,
    )
    resp.raise_for_status()
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
