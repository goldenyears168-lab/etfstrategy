#!/usr/bin/env python3
"""Write ops.snapshots kinds for private ops console (watch / risk / thermo / …).

Local order-ops JSON remains ``scripts/order/write_ops_snapshot.py`` (no --kind).

Examples:
  PYTHONPATH=src .venv/bin/python scripts/order/write_ops_console_snapshot.py --kind all
  PYTHONPATH=src .venv/bin/python scripts/order/write_ops_console_snapshot.py --kind thermo --dry-run
  PYTHONPATH=src .venv/bin/python scripts/order/write_ops_console_snapshot.py --kind watch --also-digest
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ops_console_snapshots import BUILDERS, KINDS  # noqa: E402
from ops_console_sync import (  # noqa: E402
    insert_snapshot,
    now_tpe_iso,
    supabase_ops_configured,
    upsert_digest,
)
from project_dotenv import load_project_dotenv  # noqa: E402


def _digest_for(kind: str, payload: dict[str, Any]) -> tuple[str, str, str] | None:
    """Optional Inbox wall row · (digest_key, subject, severity)."""
    asof = now_tpe_iso()[:10]
    if kind == "watch":
        n = (payload.get("evening") or {}).get("n_fires") or 0
        return (
            f"ops-watch:{asof}",
            f"自選 Watch · 訊號 {n}",
            "warn" if n else "info",
        )
    if kind == "risk":
        level = str(payload.get("level") or "UNKNOWN")
        sev = str(payload.get("severity") or "info")
        if sev == "red":
            sev = "red"
        elif sev == "warn":
            sev = "warn"
        else:
            sev = "info"
        return (f"ops-risk:{asof}", f"風控 · Detach {level}", sev)
    if kind == "thermo":
        lamp = payload.get("lamp") or "—"
        temp = payload.get("temp_pct")
        temp_s = f"{temp}%" if temp is not None else "—"
        sev = "info"
        if isinstance(temp, (int, float)) and temp >= 90:
            sev = "warn"
        if isinstance(temp, (int, float)) and temp >= 95:
            sev = "major"
        return (f"ops-thermo:{asof}", f"大跌溫度計 · {temp_s} {lamp}", sev)
    if kind == "branches":
        n = payload.get("n_evening_fires") or 0
        return (f"ops-branches:{asof}", f"分點／專家池 · 觸發 {n}", "info")
    if kind == "stage_heatmap":
        counts = payload.get("counts_30w") or {}
        n_s2 = counts.get("2") or counts.get(2) or 0
        return (f"ops-stage-heatmap:{asof}", f"Weinstein Stage · S2 {n_s2} 檔", "info")
    if kind == "rotation":
        if not payload.get("present"):
            return None
        tb = payload.get("top_buy") or []
        ts = payload.get("top_sell") or []
        nm = lambda r: r.get("name") or r.get("stock_id")  # noqa: E731
        buy_s = "、".join(nm(r) for r in tb[:3]) or "—"
        sell_s = "、".join(nm(r) for r in ts[:3]) or "—"
        return (
            f"ops-rotation:{payload.get('asof') or asof}",
            f"外資輪動 · 買 {buy_s} / 賣 {sell_s}",
            "info",
        )
    if kind == "chip_macro":
        if not payload.get("present"):
            return None
        ptxt = payload.get("position_txt") or "—"
        stage = payload.get("stage") or "—"
        z60 = payload.get("z60")
        z_s = f"z60 {z60:+.2f}" if isinstance(z60, (int, float)) else "z60 —"
        return (
            f"ops-chip-macro:{payload.get('asof_date') or asof}",
            f"籌碼風控 · {ptxt}（{stage}, {z_s}）",
            "info",
        )
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Upsert ops.snapshots for haoshi-quant-ops console pages"
    )
    parser.add_argument(
        "--kind",
        default="all",
        help=f"one of {','.join(KINDS)} or all (default)",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--also-digest",
        action="store_true",
        help="also upsert a compact ops.digests row for Inbox",
    )
    parser.add_argument(
        "--print-payload",
        action="store_true",
        help="print full JSON payload (large for thermo body)",
    )
    args = parser.parse_args(argv)

    load_project_dotenv()
    kind_arg = args.kind.strip().lower()
    if kind_arg == "all":
        kinds = list(KINDS)
    elif kind_arg in BUILDERS:
        kinds = [kind_arg]
    else:
        print(f"錯誤：未知 kind={args.kind!r} · 可用 {list(KINDS)}|all", file=sys.stderr)
        return 1

    if not args.dry_run and not supabase_ops_configured():
        print("錯誤：SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY 未設定", file=sys.stderr)
        return 2

    for kind in kinds:
        payload = BUILDERS[kind]()
        summary = {
            "kind": kind,
            "schema": payload.get("schema"),
            "keys": sorted(payload.keys())[:12],
        }
        if kind == "watch":
            summary["n_fires"] = (payload.get("evening") or {}).get("n_fires")
            summary["n_pools"] = payload.get("n_expert_pools")
        elif kind == "risk":
            summary["level"] = payload.get("level")
        elif kind == "thermo":
            summary["temp_pct"] = payload.get("temp_pct")
            summary["lamp"] = payload.get("lamp")
        elif kind == "branches":
            summary["n_fires"] = payload.get("n_evening_fires")
            summary["n_pools"] = payload.get("n_expert_pools")
        elif kind == "today":
            summary["risk_level"] = payload.get("risk_level")
            summary["thermo_temp_pct"] = payload.get("thermo_temp_pct")
        elif kind == "stage_heatmap":
            summary["asof"] = payload.get("asof")
            summary["counts_30w"] = payload.get("counts_30w")
            summary["n_rows"] = len(payload.get("rows") or [])

        print(json.dumps(summary, ensure_ascii=False))
        if args.print_payload:
            print(json.dumps(payload, ensure_ascii=False, indent=2)[:4000])

        if args.dry_run:
            continue

        insert_snapshot(kind=kind, payload=payload)
        print(f"ops.snapshots ok · kind={kind}")

        if args.also_digest:
            dig = _digest_for(kind, payload)
            if dig:
                key, subject, severity = dig
                body = payload.get("body_md") or payload.get("preview_md")
                if not body:
                    body = (
                        f"# {subject}\n\n```json\n"
                        + json.dumps(
                            {k: payload.get(k) for k in list(payload)[:8]},
                            ensure_ascii=False,
                            indent=2,
                        )
                        + "\n```\n"
                    )
                upsert_digest(
                    digest_key=key,
                    body_md=str(body)[:20000],
                    subject=subject,
                    severity=severity,
                    meta={"source": "write_ops_console_snapshot", "kind": kind},
                )
                print(f"ops.digests ok · {key}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
