#!/usr/bin/env python3
"""收盤後匯出 research_context.json + prompt_evening_full.txt（外部 LLM）。"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from research_context import (
    REPORTS_DIR,
    build_llm_prompts,
    build_research_context,
    print_llm_prompts_cli,
    write_evening_prompt_file,
)
from research_universe import DEFAULT_ETF_CODES, parse_etf_codes
from stock_db import DEFAULT_DB_PATH, PROJECT_ROOT, connect


@dataclass(frozen=True)
class BundleExportResult:
    json_path: Path | None
    prompt_evening_full_path: Path | None


def export_ai_bundle(
    conn,
    etf_codes: tuple[str, ...],
    *,
    as_of_date: str | None = None,
    quiet: bool = False,
    print_prompts: bool = True,
    write_json: bool = True,
    write_prompt: bool = True,
) -> BundleExportResult:
    ctx = build_research_context(conn, etf_codes, as_of_date=as_of_date)
    as_of = ctx.get("as_of_date") or date.today().isoformat()
    stamp = as_of.replace("-", "")
    prompts = build_llm_prompts(ctx)

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    json_path: Path | None = None
    prompt_path: Path | None = None

    if write_json:
        json_path = REPORTS_DIR / f"{stamp}_research_context.json"
        json_path.write_text(
            json.dumps(ctx, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    if write_prompt:
        prompt_path = write_evening_prompt_file(prompts, reports_dir=REPORTS_DIR)

    result = BundleExportResult(
        json_path=json_path,
        prompt_evening_full_path=prompt_path,
    )

    if quiet:
        return result

    if print_prompts:
        print_llm_prompts_cli(prompts)
    elif write_json or write_prompt:
        print("")
        print("=== AI 研究包（已寫入 reports/）===")
        if json_path:
            print(f"  JSON     {json_path.relative_to(PROJECT_ROOT)}")
        if prompt_path:
            print(f"  LLM 提示詞 {prompt_path.relative_to(PROJECT_ROOT)}")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="匯出 research_context.json + prompt_evening_full.txt"
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--etf-codes", default=",".join(DEFAULT_ETF_CODES))
    parser.add_argument("--as-of", default=None, help="investment_scores.as_of_date")
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="不輸出終端提示詞",
    )
    parser.add_argument(
        "--no-print-prompts",
        action="store_true",
        help="不於終端印出提示詞全文",
    )
    parser.add_argument(
        "--json-only",
        action="store_true",
        help="僅寫 research_context.json",
    )
    parser.add_argument(
        "--prompt-only",
        action="store_true",
        help="僅寫 prompt_evening_full.txt",
    )
    args = parser.parse_args()

    codes = parse_etf_codes(args.etf_codes)
    conn = connect(args.db)
    try:
        export_ai_bundle(
            conn,
            codes,
            as_of_date=args.as_of,
            quiet=args.quiet,
            print_prompts=not args.no_print_prompts and not args.quiet,
            write_json=not args.prompt_only,
            write_prompt=not args.json_only,
        )
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
