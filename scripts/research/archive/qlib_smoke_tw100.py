#!/usr/bin/env python3
"""Qlib smoke test for TW100 export · run with .venv-qlib (Python 3.11)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_QLIB_URI = ROOT / "data" / "qlib" / "tw100"
DEFAULT_VENV = ROOT / ".venv-qlib"


def _ensure_qlib_venv() -> None:
    if sys.version_info >= (3, 13):
        print(
            "ERROR: microsoft qlib 需 Python 3.8–3.12；請用 .venv-qlib:\n"
            f"  {DEFAULT_VENV / 'bin' / 'python'} {Path(__file__).name}",
            file=sys.stderr,
        )
        raise SystemExit(2)


def run_smoke(
    provider_uri: Path,
    *,
    sample_instrument: str = "2330",
    start: str = "2015-01-05",
    end: str = "2015-01-20",
    alpha158: bool = False,
) -> dict:
    import qlib
    from qlib.config import REG_CN
    from qlib.data import D

    qlib.init(provider_uri=str(provider_uri.resolve()), region=REG_CN)

    calendar = D.calendar(start_time=start, end_time=end, freq="day")
    instruments = D.list_instruments(D.instruments("all"), start_time=start, end_time=end, freq="day", as_list=True)

    close_df = D.features([sample_instrument], ["$close", "$factor"], start_time=start, end_time=end)
    n_close = int(len(close_df))

    result: dict = {
        "schema": "qlib_smoke_tw100-v1",
        "provider_uri": str(provider_uri.resolve()),
        "region": "cn",
        "calendar_days": len(calendar),
        "calendar_head": [str(x)[:10] for x in calendar[:3]],
        "instruments_in_window": len(instruments),
        "sample_instrument": sample_instrument,
        "sample_close_rows": n_close,
        "sample_close_head": (
            close_df.head(3).reset_index().to_dict(orient="records") if n_close else []
        ),
        "passed": n_close > 0 and len(calendar) > 0,
    }

    if alpha158:
        try:
            from qlib.contrib.data.handler import Alpha158

            handler = Alpha158(
                instruments=[sample_instrument],
                start_time=start,
                end_time=end,
                fit_start_time=start,
                fit_end_time=end,
            )
            feat = handler.fetch(col_set="feature")
            result["alpha158_feature_rows"] = int(len(feat))
            result["alpha158_feature_cols"] = int(feat.shape[1]) if len(feat) else 0
            result["alpha158_passed"] = len(feat) > 0
            result["passed"] = result["passed"] and result["alpha158_passed"]
        except Exception as exc:  # noqa: BLE001
            result["alpha158_error"] = str(exc)
            result["alpha158_passed"] = False
            result["passed"] = False

    return result


def main() -> int:
    _ensure_qlib_venv()

    p = argparse.ArgumentParser(description="Smoke test data/qlib/tw100 with microsoft qlib")
    p.add_argument("--provider-uri", type=Path, default=DEFAULT_QLIB_URI)
    p.add_argument("--instrument", default="2330")
    p.add_argument("--start", default="2015-01-05")
    p.add_argument("--end", default="2015-01-20")
    p.add_argument("--alpha158", action="store_true", help="Also load Alpha158 handler (slower)")
    p.add_argument("--json-out", type=Path, default=None)
    args = p.parse_args()

    if not args.provider_uri.is_dir():
        print(f"ERROR: provider_uri 不存在: {args.provider_uri}", file=sys.stderr)
        return 1

    payload = run_smoke(
        args.provider_uri,
        sample_instrument=args.instrument,
        start=args.start,
        end=args.end,
        alpha158=args.alpha158,
    )
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    print(text)
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(text + "\n", encoding="utf-8")

    if not payload.get("passed"):
        print("FAIL: qlib smoke test", file=sys.stderr)
        return 1
    print("OK: qlib smoke test passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
