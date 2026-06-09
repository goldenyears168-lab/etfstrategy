#!/usr/bin/env python3
"""收盤研究後段編排：News → Catalyst → Score → Bundle → Memo → Perplexity。

由 daily_sync.sh 在 holdings changes 之後呼叫；步驟計畫可單元測試。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from project_config import (
    DEFAULT_NEWS_LOOKBACK_DAYS,
    DEFAULT_PERPLEXITY_VERIFY_MAX,
    ETF_CODES_HOLDINGS,
    csv_codes,
    parse_etf_codes,
)
from stock_db import PROJECT_ROOT

StepRunner = Callable[["PipelineStep"], bool]


@dataclass(frozen=True)
class PipelineStep:
    label: str
    argv: tuple[str, ...]
    skip_reason: str | None = None

    @property
    def enabled(self) -> bool:
        return self.skip_reason is None


@dataclass
class EveningPipelineConfig:
    python: Path
    src: Path
    etf_codes: tuple[str, ...] = field(default_factory=lambda: ETF_CODES_HOLDINGS)
    quiet: bool = False
    show_report: bool = False
    run_news_sync: bool = False
    run_catalyst_engine: bool = False
    run_score_engine: bool = False
    run_export_ai_bundle: bool = False
    run_memo: bool = False
    memo_use_llm: bool = False
    run_perplexity_summary: bool = False
    run_perplexity_verify: bool = False
    perplexity_api_key: str = ""
    news_lookback_days: int = DEFAULT_NEWS_LOOKBACK_DAYS
    perplexity_verify_max: int = DEFAULT_PERPLEXITY_VERIFY_MAX

    @classmethod
    def from_environ(
        cls,
        *,
        python: Path | None = None,
        src: Path | None = None,
        etf_codes: tuple[str, ...] | None = None,
        quiet: bool = False,
        show_report: bool = False,
        env: dict[str, str] | None = None,
    ) -> EveningPipelineConfig:
        e = env if env is not None else os.environ
        src_path = src or (PROJECT_ROOT / "src")
        py = python or (PROJECT_ROOT / ".venv" / "bin" / "python")
        return cls(
            python=py,
            src=src_path,
            etf_codes=etf_codes or ETF_CODES_HOLDINGS,
            quiet=quiet,
            show_report=show_report,
            run_news_sync=e.get("RUN_NEWS_SYNC", "0") == "1",
            run_catalyst_engine=e.get("RUN_CATALYST_ENGINE", "0") == "1",
            run_score_engine=e.get("RUN_SCORE_ENGINE", "0") == "1",
            run_export_ai_bundle=e.get("RUN_EXPORT_AI_BUNDLE", "0") == "1",
            run_memo=e.get("RUN_MEMO", "0") == "1",
            memo_use_llm=e.get("MEMO_USE_LLM", "0") == "1",
            run_perplexity_summary=e.get("RUN_PERPLEXITY_SUMMARY", "0") == "1",
            run_perplexity_verify=e.get("RUN_PERPLEXITY_VERIFY", "0") == "1",
            perplexity_api_key=e.get("PERPLEXITY_API_KEY", "").strip(),
            news_lookback_days=int(
                e.get("NEWS_LOOKBACK_DAYS", str(DEFAULT_NEWS_LOOKBACK_DAYS))
            ),
            perplexity_verify_max=int(
                e.get("PERPLEXITY_VERIFY_MAX", str(DEFAULT_PERPLEXITY_VERIFY_MAX))
            ),
        )


def _py(cfg: EveningPipelineConfig, script: str, *args: str) -> tuple[str, ...]:
    return (str(cfg.python), str(cfg.src / script), *args)


def _etf_arg(cfg: EveningPipelineConfig) -> tuple[str, ...]:
    return ("--etf-codes", csv_codes(cfg.etf_codes))


def _score_argv(cfg: EveningPipelineConfig) -> tuple[str, ...]:
    args: list[str] = ["--sync-db", *_etf_arg(cfg)]
    if cfg.show_report:
        args.append("--human")
    elif cfg.quiet:
        args.append("--quiet")
    return tuple(_py(cfg, "score_engine.py", *args))


def plan_evening_steps(cfg: EveningPipelineConfig) -> list[PipelineStep]:
    """依 env 旗標產生收盤研究步驟（純函式，可測）。"""
    steps: list[PipelineStep] = []
    codes = csv_codes(cfg.etf_codes)

    if cfg.run_news_sync:
        if not cfg.perplexity_api_key:
            steps.append(
                PipelineStep(
                    "catalyst news (Perplexity)",
                    (),
                    skip_reason="PERPLEXITY_API_KEY 未設定",
                )
            )
        else:
            news_args = [
                "--sync-db",
                *_etf_arg(cfg),
                "--lookback-days",
                str(cfg.news_lookback_days),
            ]
            if cfg.show_report:
                news_args.append("--report")
            elif cfg.quiet:
                news_args.append("--quiet")
            steps.append(
                PipelineStep(
                    "catalyst news (Perplexity)",
                    _py(cfg, "sync_catalyst_news.py", *news_args),
                )
            )

    if cfg.run_catalyst_engine:
        steps.append(
            PipelineStep(
                "catalyst events",
                _py(cfg, "catalyst_engine.py", *_etf_arg(cfg), "--sync-db"),
            )
        )

    if cfg.run_score_engine:
        steps.append(
            PipelineStep("investment score", _score_argv(cfg)),
        )
        if cfg.run_export_ai_bundle:
            bundle_args = list(_etf_arg(cfg))
            if cfg.show_report or cfg.quiet:
                bundle_args.extend(["--quiet", "--no-print-prompts"])
            steps.append(
                PipelineStep(
                    "AI bundle export (JSON + 提示詞)",
                    _py(cfg, "export_ai_bundle.py", *bundle_args),
                )
            )
        steps.append(
            PipelineStep(
                "position review (持倉賣出雷達)",
                _py(cfg, "position_review.py", "--report"),
            )
        )
    else:
        steps.append(
            PipelineStep(
                "investment score",
                (),
                skip_reason="RUN_SCORE_ENGINE=0",
            )
        )

    if cfg.run_memo:
        memo_args = ["--sync-db"]
        if cfg.memo_use_llm:
            memo_args.append("--use-llm")
        steps.append(
            PipelineStep("investment memo", _py(cfg, "investment_memo.py", *memo_args)),
        )

    if cfg.perplexity_api_key:
        if cfg.run_perplexity_summary:
            steps.append(
                PipelineStep(
                    "evening summary (Perplexity)",
                    _py(
                        cfg,
                        "perplexity_evening.py",
                        "--summary",
                        "--report",
                        *_etf_arg(cfg),
                    ),
                )
            )
        if cfg.run_perplexity_verify:
            steps.append(
                PipelineStep(
                    "catalyst verify (Perplexity)",
                    _py(
                        cfg,
                        "perplexity_evening.py",
                        "--verify",
                        "--report",
                        "--apply-confidence",
                        *_etf_arg(cfg),
                        "--verify-max",
                        str(cfg.perplexity_verify_max),
                    ),
                )
            )
            if cfg.run_score_engine:
                steps.append(
                    PipelineStep(
                        "investment score (post-verify)",
                        _score_argv(cfg),
                    )
                )

    return steps


def default_runner(step: PipelineStep) -> bool:
    if not step.enabled:
        return True
    proc = subprocess.run(
        list(step.argv),
        env={
            **os.environ,
            "HTTP_PROXY": "",
            "HTTPS_PROXY": "",
            "ALL_PROXY": "",
            "http_proxy": "",
            "https_proxy": "",
            "all_proxy": "",
        },
    )
    return proc.returncode == 0


def run_evening_pipeline(
    cfg: EveningPipelineConfig,
    *,
    runner: StepRunner = default_runner,
    log: Callable[[str], None] | None = print,
) -> int:
    """執行步驟；選用步驟失敗回 0（與 daily_sync aux 語意一致）。"""
    _log = log or (lambda _: None)
    aux_failed = False
    for step in plan_evening_steps(cfg):
        if not step.enabled:
            _log(f"--- {step.label} ---")
            _log(f"  SKIP（{step.skip_reason}）")
            continue
        _log(f"--- {step.label} ---")
        if not runner(step):
            _log(f"WARN: {step.label}")
            aux_failed = True
        else:
            _log(f"OK: {step.label}")
    return 0 if not aux_failed else 0  # aux 不拉高 exit code


def main() -> int:
    parser = argparse.ArgumentParser(description="收盤研究後段 pipeline")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--show-report", action="store_true")
    parser.add_argument("--etf-codes", default=csv_codes(ETF_CODES_HOLDINGS))
    parser.add_argument("--dry-run", action="store_true", help="只印步驟不執行")
    args = parser.parse_args()

    cfg = EveningPipelineConfig.from_environ(
        quiet=args.quiet,
        show_report=args.show_report,
        etf_codes=parse_etf_codes(args.etf_codes, default=ETF_CODES_HOLDINGS),
    )

    if args.dry_run:
        for step in plan_evening_steps(cfg):
            if step.enabled:
                print(f"[run] {step.label}: {' '.join(step.argv)}")
            else:
                print(f"[skip] {step.label}: {step.skip_reason}")
        return 0

    return run_evening_pipeline(cfg)


if __name__ == "__main__":
    raise SystemExit(main())
