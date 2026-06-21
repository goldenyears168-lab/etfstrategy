"""Fubon Neo API session · login / realtime."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from execution_config import account_block, load_execution_config
from project_dotenv import load_project_dotenv
from stock_db import PROJECT_ROOT

_FUBON_PY_MAX = (3, 13)


def check_python_version() -> None:
    if sys.version_info[:2] > _FUBON_PY_MAX:
        major, minor = sys.version_info[:2]
        raise RuntimeError(
            f"Python {major}.{minor} 不受富邦 Neo SDK 支援（官方：3.8–3.13）。"
            " 請用 .venv-fubon/bin/python 執行 execution 腳本。"
        )


def _resolve_cert(path_str: str) -> Path:
    p = Path(path_str)
    return p if p.is_absolute() else PROJECT_ROOT / p


def _login_is_success(res: Any) -> bool:
    ok = getattr(res, "is_success", None)
    if ok is None:
        ok = getattr(res, "isSuccess", False)
    return bool(ok)


@dataclass
class FubonSession:
    sdk: Any
    accounts: list[Any]

    @property
    def primary(self) -> Any:
        if not self.accounts:
            raise RuntimeError("No Fubon accounts returned after login")
        return self.accounts[0]

    def init_realtime(self) -> None:
        self.sdk.init_realtime()


def connect_fubon(*, realtime: bool = False, load_env: bool = True) -> FubonSession:
    """Login via .env + config/execution.yaml; optional init_realtime()."""
    check_python_version()
    if load_env:
        load_project_dotenv()

    cfg = load_execution_config()
    acct_cfg = account_block(cfg)

    user_id = os.environ.get(acct_cfg.get("user_id_env", "FUBON_USER_ID"), "").strip()
    if not user_id:
        user_id = "N125801238"

    password = os.environ.get(acct_cfg.get("password_env", "FUBON_PASSWORD"), "").strip()
    cert_env = acct_cfg.get("cert_path_env", "FUBON_CERT_PATH")
    cert_default = acct_cfg.get("default_cert_path", f"CAFubon/{user_id}/{user_id}.pfx")
    cert_path = _resolve_cert(os.environ.get(cert_env, cert_default))
    cert_pass = os.environ.get(
        acct_cfg.get("cert_password_env", "FUBON_CERT_PASSWORD"), user_id
    ).strip()
    api_key = os.environ.get(acct_cfg.get("api_key_env", "FUBON_API_KEY"), "").strip()

    if not cert_path.is_file():
        raise FileNotFoundError(f"Fubon cert not found: {cert_path}")

    from fubon_neo.sdk import FubonSDK

    sdk = FubonSDK()
    cert_str = str(cert_path)

    if api_key:
        res = sdk.apikey_login(user_id, api_key, cert_str, cert_pass)
    else:
        if not password:
            raise ValueError(
                "FUBON_PASSWORD 未設定；或改用 FUBON_API_KEY（見 .env.example）"
            )
        res = sdk.login(user_id, password, cert_str, cert_pass)

    if not _login_is_success(res):
        msg = getattr(res, "message", "") or "login failed"
        raise RuntimeError(f"Fubon login failed: {msg}")

    accounts = list(getattr(res, "data", []) or [])
    session = FubonSession(sdk=sdk, accounts=accounts)
    if realtime:
        session.init_realtime()
    return session


def account_label(acc: Any) -> str:
    branch = getattr(acc, "branch_no", getattr(acc, "branchNo", "?"))
    number = getattr(acc, "account", "?")
    name = getattr(acc, "name", "?")
    kind = getattr(acc, "account_type", getattr(acc, "accountType", "?"))
    return f"{name} · {branch}-{number} · {kind}"
