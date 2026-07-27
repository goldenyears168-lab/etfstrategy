"""Re-run order submit under ``.venv-fubon`` when host Python > 3.13.

Radar / screen jobs often run under ``.venv`` (3.14). Fubon Neo is supported
on 3.8–3.13; calling connect in-process from 3.14 either raises our version
gate or risks native teardown crashes. This helper serializes a payload and
executes ``python -m order.fubon_subprocess`` with ``.venv-fubon``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from stock_db import PROJECT_ROOT

_FUBON_PY_MAX = (3, 13)


def host_needs_fubon_venv() -> bool:
    # Root fix: pytest must stay in-process so mocks work (never spawn .venv-fubon).
    from order.live_submit_guard import under_test_runner

    if under_test_runner():
        return False
    # Already inside .venv-fubon worker — never re-spawn (avoids recursion when
    # parent exported FUBON_FORCE_SUBPROCESS=1 into the child env).
    if os.environ.get("FUBON_SUBPROCESS_INNER", "").strip() == "1":
        return False
    if os.environ.get("FUBON_FORCE_SUBPROCESS", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    ):
        return True
    # 3.14+ cannot load Neo in-process; 3.13 .venv often lacks the wheel
    # (kept only in .venv-fubon) — spawn worker whenever import is missing.
    if sys.version_info[:2] > _FUBON_PY_MAX:
        return True
    try:
        import fubon_neo  # noqa: F401
    except ImportError:
        return True
    return False


def fubon_python_bin() -> Path:
    p = PROJECT_ROOT / ".venv-fubon" / "bin" / "python"
    if not p.is_file():
        raise FileNotFoundError(f"missing Fubon venv python: {p}")
    return p


def run_fubon_order_worker(payload: dict[str, Any], *, timeout_sec: float = 120.0) -> Any:
    """Run worker under .venv-fubon; return deserialized ``result`` field."""
    from order.live_submit_guard import assert_live_submit_allowed, under_test_runner

    if under_test_runner():
        raise RuntimeError("fubon_subprocess_blocked:under_test_runner")
    # Refuse before spawning · pytest under 3.14 used to bypass in-process mocks here
    if not bool(payload.get("dry_run")):
        assert_live_submit_allowed(session_date=str(payload.get("session_date") or "") or None)
    py = fubon_python_bin()
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        suffix=".json",
        prefix="fubon_order_",
        dir=str(PROJECT_ROOT / "reports" / "order"),
        delete=False,
    ) as fh:
        json.dump(payload, fh, ensure_ascii=False)
        req_path = Path(fh.name)
    out_path = req_path.with_suffix(".out.json")
    try:
        env = {
            **os.environ,
            "PYTHONPATH": str(PROJECT_ROOT / "src"),
            "FUBON_SUBPROCESS_INNER": "1",
        }
        proc = subprocess.run(
            [
                str(py),
                "-m",
                "order.fubon_subprocess",
                "--request",
                str(req_path),
                "--output",
                str(out_path),
            ],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            env=env,
        )
        if proc.returncode != 0:
            # Worker writes its real reason to out_path before exiting non-zero
            # (see _worker_main). Prefer that over a bare "exit N" so callers and
            # logs can see the true cause (e.g. Fubon connect/login failure).
            reason = ""
            try:
                raw_err = json.loads(out_path.read_text(encoding="utf-8"))
                if isinstance(raw_err, dict):
                    reason = str(raw_err.get("reason") or "").strip()
            except (OSError, json.JSONDecodeError):
                reason = ""
            err = (
                reason
                or (proc.stderr or proc.stdout or "").strip()
                or f"exit {proc.returncode}"
            )
            raise RuntimeError(f"fubon_subprocess_failed:{err}")
        raw = json.loads(out_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or "result" not in raw:
            raise RuntimeError("fubon_subprocess_bad_output")
        if raw.get("status") == "error":
            raise RuntimeError(str(raw.get("reason") or "worker_error"))
        return raw["result"]
    finally:
        req_path.unlink(missing_ok=True)
        out_path.unlink(missing_ok=True)


def _worker_main(argv: list[str] | None = None) -> int:
    import argparse
    from types import SimpleNamespace

    parser = argparse.ArgumentParser(prog="order.fubon_subprocess")
    parser.add_argument("--request", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    req = json.loads(Path(args.request).read_text(encoding="utf-8"))
    kind = str(req.get("kind") or "")
    try:
        if kind in {"abc_v3_f1_entry", "abc_v3_f1_position"}:
            raise RuntimeError(
                f"abc_order_removed:{kind} · ABC removed from Order layer 2026-07-16"
            )
        elif kind == "c18acc_actions":
            from order.c18acc_order import process_c18acc_orders

            actions = [SimpleNamespace(**row) for row in (req.get("actions") or [])]
            result = process_c18acc_orders(
                actions,
                session_date=str(req["session_date"]),
                poll_minute=str(req.get("poll_minute") or ""),
                dry_run=bool(req.get("dry_run")) if req.get("dry_run") is not None else None,
            )
        elif kind == "detach_half_flatten":
            from order.detach_gate_order import run_half_flatten

            result = run_half_flatten(
                session_date=str(req.get("session_date") or "") or None,
                dry_run=bool(req.get("dry_run")) if req.get("dry_run") is not None else None,
                auto_submit=(
                    bool(req.get("auto_submit")) if req.get("auto_submit") is not None else None
                ),
                order_enabled=(
                    bool(req.get("order_enabled"))
                    if req.get("order_enabled") is not None
                    else None
                ),
                force=bool(req.get("force")),
            )
        elif kind == "leading_dip_entry":
            from order.leading_dip_ledger import load_ledger
            from order.leading_dip_order import _submit_entry
            from order.leading_dip_order_config import load_leading_dip_order_config

            conf = load_leading_dip_order_config()
            ledger_path = Path(str(req["ledger_path"]))
            result = _submit_entry(
                row=dict(req.get("row") or {}),
                conf=conf,
                session_date=str(req["session_date"]),
                poll_minute=str(req.get("poll_minute") or ""),
                trading_dates=[str(x) for x in (req.get("trading_dates") or [])],
                ledger=load_ledger(ledger_path),
                ledger_path=ledger_path,
                dry=bool(req.get("dry_run")),
                strategy_id=str(req.get("strategy_id") or ""),
                user_def=str(req.get("user_def") or ""),
                budget_twd=int(req.get("budget_twd") or 0),
                sleeve=str(req.get("sleeve") or "main"),
            )
        elif kind == "leading_dip_exits":
            from order.leading_dip_ledger import load_ledger
            from order.leading_dip_order import _process_exits
            from order.leading_dip_order_config import load_leading_dip_order_config

            conf = load_leading_dip_order_config()
            ledger_path = Path(str(req["ledger_path"]))
            result = _process_exits(
                ledger=load_ledger(ledger_path),
                ledger_path=ledger_path,
                conf=conf,
                user_def=str(req.get("user_def") or ""),
                session_date=str(req["session_date"]),
                poll_minute=str(req.get("poll_minute") or ""),
                dry=bool(req.get("dry_run")),
            )
        elif kind == "holdings_status":
            from stock_db import DEFAULT_DB_PATH, connect
            from order.holdings_pulse import build_holdings_pulse
            from order.order_fill_report import holdings_status_rows_from_pulse

            conn = connect(DEFAULT_DB_PATH)
            try:
                pulse = build_holdings_pulse(
                    conn,
                    sync_rrg_intraday=False,
                    refresh_morning_risk=False,
                )
                result = {
                    "rows": holdings_status_rows_from_pulse(pulse),
                    "cash_available": pulse.cash_available,
                    "total_market_value": pulse.total_market_value,
                    "total_unrealized_pnl": pulse.total_unrealized_pnl,
                    "error": pulse.fubon_error,
                }
            finally:
                conn.close()
        else:
            raise ValueError(f"unknown worker kind: {kind}")
        Path(args.output).write_text(
            json.dumps(
                {"status": "ok", "result": _jsonable(result)},
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        return 0
    except Exception as exc:  # noqa: BLE001 — worker boundary
        Path(args.output).write_text(
            json.dumps({"status": "error", "reason": str(exc), "result": None}, ensure_ascii=False)
            + "\n",
            encoding="utf-8",
        )
        return 1


def _jsonable(obj: Any) -> Any:
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items() if k != "applied_actions"}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(x) for x in obj]
    if hasattr(obj, "__dict__"):
        return _jsonable(vars(obj))
    return str(obj)


if __name__ == "__main__":
    raise SystemExit(_worker_main())
