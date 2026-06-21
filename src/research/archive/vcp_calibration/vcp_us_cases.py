"""Load config/vcp_us_cases.yaml for US VCP gold-standard benchmark."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import yaml

from stock_db import PROJECT_ROOT

DEFAULT_CASES_PATH = PROJECT_ROOT / "config" / "vcp_us_cases.yaml"
BAR_WARMUP_DAYS = 400


@dataclass(frozen=True)
class VcpUsCase:
    case_id: str
    ticker: str
    company: str
    period_label: str
    literature_start: date
    literature_end: date
    source: str = ""

    def contains(self, as_of: date) -> bool:
        return self.literature_start <= as_of <= self.literature_end


@dataclass(frozen=True)
class VcpUsCasesConfig:
    benchmark: str
    model_id: str
    score_thresholds: tuple[float, ...]
    forward_days: tuple[int, ...]
    sample_every: int
    cases: tuple[VcpUsCase, ...]

    @property
    def tickers(self) -> tuple[str, ...]:
        seen: set[str] = set()
        out: list[str] = []
        for c in self.cases:
            if c.ticker not in seen:
                seen.add(c.ticker)
                out.append(c.ticker)
        return tuple(out)

    def fetch_start(self, end: date) -> date:
        earliest = min(c.literature_start for c in self.cases)
        return earliest - timedelta(days=BAR_WARMUP_DAYS)

    def cases_for_ticker(self, ticker: str) -> tuple[VcpUsCase, ...]:
        return tuple(c for c in self.cases if c.ticker == ticker.upper())


def _parse_date(value: str) -> date:
    return date.fromisoformat(str(value)[:10])


def load_vcp_us_cases(path: Path | None = None) -> VcpUsCasesConfig:
    cfg_path = path or DEFAULT_CASES_PATH
    raw: dict[str, Any] = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}

    cases: list[VcpUsCase] = []
    for row in raw.get("cases") or []:
        cases.append(
            VcpUsCase(
                case_id=str(row["id"]),
                ticker=str(row["ticker"]).upper(),
                company=str(row.get("company", "")),
                period_label=str(row.get("period_label", "")),
                literature_start=_parse_date(row["literature_start"]),
                literature_end=_parse_date(row["literature_end"]),
                source=str(row.get("source", "")),
            )
        )

    thresholds = tuple(float(x) for x in (raw.get("score_thresholds") or [65, 80]))
    forward = tuple(int(x) for x in (raw.get("forward_days") or [20, 60]))
    return VcpUsCasesConfig(
        benchmark=str(raw.get("benchmark") or "SPY"),
        model_id=str(raw.get("model_id") or "vcp-nse-port"),
        score_thresholds=thresholds,
        forward_days=forward,
        sample_every=int(raw.get("sample_every") or 5),
        cases=tuple(cases),
    )


def summarize_case_hits(
    signals: list[dict],
    config: VcpUsCasesConfig,
    *,
    score_thresholds: tuple[float, ...] | None = None,
) -> list[dict]:
    """Per gold-standard case: hit counts by threshold, in vs out of literature window."""
    thresholds = score_thresholds or config.score_thresholds
    rows: list[dict] = []
    for case in config.cases:
        ticker_signals = [s for s in signals if s["ticker"] == case.ticker]
        row: dict = {
            "case_id": case.case_id,
            "ticker": case.ticker,
            "period_label": case.period_label,
            "literature": f"{case.literature_start} ~ {case.literature_end}",
            "source": case.source,
        }
        for thr in thresholds:
            thr_key = int(thr) if thr == int(thr) else thr
            qualified = [
                s
                for s in ticker_signals
                if float(s["composite_score"]) >= thr
            ]
            in_lit = [
                s
                for s in qualified
                if case.contains(date.fromisoformat(s["as_of"]))
            ]
            row[f"hits_{thr_key}_total"] = len(qualified)
            row[f"hits_{thr_key}_in_literature"] = len(in_lit)
            row[f"overlap_{thr_key}"] = len(in_lit) > 0
        rows.append(row)
    return rows
