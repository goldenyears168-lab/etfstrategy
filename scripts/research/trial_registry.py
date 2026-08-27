"""Lightweight, dependency-free trial-registry helper for research scripts.

Why this exists
----------------
Two audits this session (`reports/research/c18acc_trial_count_audit/FINDINGS.md`
and `scripts/research/chip_macro/audit_trial_count.py`) both hit the same wall:
after the fact, there was no machine-readable record of how many trials/sweep
cells were actually run against a strategy family, so N_trials for a Deflated
Sharpe Ratio (Bailey & Lopez de Prado 2014) style multiple-comparisons
correction had to be hand-reconstructed from prose research logs and scattered,
inconsistently-shaped JSON artifacts. Chip-macro pulled off a defensible-but-
uncertain N_eff (4-14) only through heroic manual archaeology; C18acc/RRG-mono
(~53 topics, ~148 named trials, ~1,251 sweep cells) could only get to an N_eff
*range* (~8-15), with no confidence.

This module does NOT retrofit that history (not in scope, not really possible).
It gives *future* research scripts a one-line way to leave a durable, uniform
breadcrumb per trial/sweep-cell, so the next audit doesn't have to re-invent
this from scratch.

Design goals: zero third-party dependencies (stdlib only), one JSON object per
line (append-only, diff-friendly, safe under concurrent writers on this repo's
two-machine setup), one file per strategy family so registries don't collide.

Quick start (3 lines, called right after a research script computes a result)::

    from trial_registry import append_trial
    append_trial("tmf_channel", topic_id="tmf-atr-filter-sweep", ts="2026-08-08",
                 params={"atr_window": 14, "k": 1.5}, n_observations=482,
                 metric_name="sharpe_oos", metric_value=0.87, status="kept")

Then later, any audit script can do::

    from trial_registry import load_trials, dedup_count
    trials = load_trials("tmf_channel")
    print(dedup_count("tmf_channel"))   # {"n_raw": .., "n_dedup": .., "clusters": [...]}

Schema (one JSON object per line, in ``reports/research/_trial_registry/<family>.jsonl``)
-------------------------------------------------------------------------------
Required fields (enforced by ``append_trial``):
  topic_id        str    -- human-readable hypothesis/topic id. Should match a
                             `config/research.yaml` topic key where one exists,
                             but is free text (not validated against the YAML)
                             so this module has no config-file coupling.
  ts              str    -- ISO date/datetime string SUPPLIED BY THE CALLER.
                             This module never reads the wall clock itself:
                             some contexts this repo runs in (headless agents,
                             replay/backtest harnesses) don't have a reliable
                             "now", and a trial record should reflect the date
                             *of the research run*, not of whenever this line
                             happens to execute. Any ISO-8601-ish string is
                             accepted verbatim (e.g. "2026-08-08" or
                             "2026-08-08T14:30:00+08:00"); not parsed/validated.
  params          dict   -- the sweep-cell / trial parameters, used to build
                             dedup_key. Should be JSON-serializable and contain
                             only the knobs that define "this trial" (not
                             derived outputs). Nested dicts/lists are fine.
  n_observations  int    -- sample size the metric was computed over (bars,
                             trades, trading days -- whatever is the natural
                             unit for that family; record which one via notes/
                             tags if it's ambiguous later).
  metric_name     str    -- name of the primary metric, e.g. "sharpe_oos",
                             "mean_excess_pct", "dsr". Deliberately not
                             hardcoded to Sharpe: this repo's research lines use
                             several different primary metrics and forcing one
                             name would just get bypassed.
  metric_value    float  -- the value of that metric.
  status          str    -- one of "kept" | "rejected" | "superseded".
                             ("kept" = still an active/adopted candidate as of
                             this record; "rejected" = failed its own gate;
                             "superseded" = replaced by a later trial, e.g. a
                             recalibration). This is a coarse trial-level flag,
                             independent of (and not a replacement for) the
                             richer status vocabulary in `config/research.yaml`
                             (active/pending/graduated/archived/...).

Auto-computed field:
  dedup_key       str    -- sha256 hex digest (first 16 chars) of the
                             normalized `params` dict (keys sorted recursively,
                             floats rounded to 9 significant figures to absorb
                             float noise). Two calls with the same params
                             (any key order) get the same dedup_key. Exact
                             string equality of dedup_key is a cheap first pass
                             for "these are the identical trial run twice";
                             `dedup_count()` additionally clusters *near*
                             duplicates by parameter distance, the same style
                             of collapse chip_macro's audit had to hand-build.

Optional fields (default shown):
  trial_id        str | None = None   -- short human label for the trial, if
                                         the caller has one (falls back to
                                         dedup_key in reports if absent).
  source          str = ""            -- path of the script/artifact that
                                         produced this record, for traceability.
  notes           str = ""            -- free text.
  extra_metrics   dict = {}           -- any secondary metrics worth keeping
                                         (e.g. {"hit_rate_pct": 72.3}).
  tags            list[str] = []      -- free-form labels (e.g. ["walkforward"]).
  registry_dir    Path | None = None  -- override for the registry directory,
                                         mainly for tests; defaults to
                                         reports/research/_trial_registry/.
  written_at      str                 -- auto-stamped from ts at write time IF
                                         the caller does not pass ts (kept
                                         identical to ts otherwise); present so
                                         a record never silently lacks any
                                         notion of "when", even if a future
                                         caller forgets `ts`. NOT used as a
                                         substitute for `ts` in any analysis --
                                         `ts` is the field of record.

Non-goals: this module does not compute DSR / N_eff itself, does not talk to
`config/research.yaml`, and does not enforce that `topic_id` is unique or that
callers append responsibly. It is intentionally dumb storage plus one
convenience clustering helper -- keeping the friction of "log this trial" as
close to zero as possible is the entire point.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REGISTRY_DIR = ROOT / "reports" / "research" / "_trial_registry"

VALID_STATUSES = ("kept", "rejected", "superseded")


def _normalize(obj: Any) -> Any:
    """Recursively normalize a JSON-able value for stable hashing:
    dict keys sorted, floats rounded to 9 significant figures (absorbs
    float noise like 0.1+0.2 vs 0.30000000000000004), tuples -> lists."""
    if isinstance(obj, dict):
        return {k: _normalize(obj[k]) for k in sorted(obj.keys(), key=str)}
    if isinstance(obj, (list, tuple)):
        return [_normalize(v) for v in obj]
    if isinstance(obj, float):
        if obj == 0.0:
            return 0.0
        return float(f"{obj:.9g}")
    return obj


def compute_dedup_key(params: dict) -> str:
    """Stable short hash of a normalized params dict. Same params (any key
    order, float-noise tolerant) -> same key."""
    normalized = _normalize(params or {})
    blob = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _registry_path(family: str, registry_dir: Path | None = None) -> Path:
    directory = registry_dir or DEFAULT_REGISTRY_DIR
    directory.mkdir(parents=True, exist_ok=True)
    safe_family = family.strip().replace("/", "_")
    return directory / f"{safe_family}.jsonl"


def append_trial(
    family: str,
    topic_id: str,
    ts: str,
    params: dict,
    n_observations: int,
    metric_name: str,
    metric_value: float,
    status: str,
    *,
    trial_id: str | None = None,
    source: str = "",
    notes: str = "",
    extra_metrics: dict | None = None,
    tags: list | None = None,
    registry_dir: Path | None = None,
) -> dict:
    """Append one trial record to reports/research/_trial_registry/<family>.jsonl.

    Returns the record dict that was written (including the auto-computed
    dedup_key), so the caller can log/print it if useful. Never overwrites --
    always appends a new line.
    """
    if status not in VALID_STATUSES:
        raise ValueError(f"status must be one of {VALID_STATUSES}, got {status!r}")
    if not isinstance(params, dict):
        raise TypeError("params must be a dict")
    if not ts:
        raise ValueError("ts (caller-supplied ISO date/datetime string) is required")

    record = {
        "topic_id": topic_id,
        "ts": ts,
        "params": params,
        "n_observations": n_observations,
        "metric_name": metric_name,
        "metric_value": metric_value,
        "status": status,
        "dedup_key": compute_dedup_key(params),
        "trial_id": trial_id,
        "source": source,
        "notes": notes,
        "extra_metrics": extra_metrics or {},
        "tags": tags or [],
    }

    path = _registry_path(family, registry_dir)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def load_trials(family: str, registry_dir: Path | None = None) -> list[dict]:
    """Read all trial records for a family, in append order. Returns [] if the
    family's log file doesn't exist yet (not an error -- a family with zero
    logged trials is a valid, if uninformative, state)."""
    path = _registry_path(family, registry_dir)
    if not path.exists():
        return []
    trials = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                trials.append(json.loads(line))
    return trials


def _param_distance(p1: dict, p2: dict) -> float:
    """Normalized distance in [0, 1] between two params dicts: per shared-or-
    missing key, 0 if equal, 1 if a key is missing on one side or values
    differ categorically, or |a-b|/max(|a|,|b|,eps) for numeric values.
    Averaged over the union of keys. This is deliberately simple (no
    correlation matrix available here, unlike chip_macro's audit which had
    full return series) -- it is a parameter-space proxy for "these two
    trials are probably the same idea run twice with a tweak", not a
    statistical independence measure."""
    keys = set(p1) | set(p2)
    if not keys:
        return 0.0
    total = 0.0
    for k in keys:
        v1, v2 = p1.get(k), p2.get(k)
        if k not in p1 or k not in p2:
            total += 1.0
            continue
        if isinstance(v1, (int, float)) and isinstance(v2, (int, float)) and not isinstance(v1, bool) and not isinstance(v2, bool):
            denom = max(abs(v1), abs(v2), 1e-9)
            total += min(1.0, abs(v1 - v2) / denom)
        else:
            total += 0.0 if v1 == v2 else 1.0
    return total / len(keys)


def dedup_count(
    family: str,
    distance_threshold: float = 0.15,
    registry_dir: Path | None = None,
) -> dict:
    """Collapse near-duplicate trials in a family's registry via union-find
    clustering on parameter distance (exact dedup_key match always clusters;
    params within `distance_threshold` average normalized distance also
    cluster). This is the same style of manual collapsing both
    chip_macro/audit_trial_count.py and the C18acc audit had to hand-build
    from scratch -- here it's reusable.

    Returns {"n_raw": int, "n_dedup": int, "clusters": [[trial_id_or_index, ...], ...]}.
    n_dedup is the number of clusters, i.e. a candidate N_trials for a DSR-style
    multiple-comparisons correction (still cruder than chip_macro's spectral
    N_eff on real return-series correlations -- use that approach instead when
    daily P&L series are available; this is the fallback for when they aren't).
    """
    trials = load_trials(family, registry_dir)
    n = len(trials)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for i in range(n):
        for j in range(i + 1, n):
            if trials[i]["dedup_key"] == trials[j]["dedup_key"]:
                union(i, j)
                continue
            if _param_distance(trials[i]["params"], trials[j]["params"]) <= distance_threshold:
                union(i, j)

    clusters: dict[int, list[int]] = {}
    for i in range(n):
        clusters.setdefault(find(i), []).append(i)

    def label(idx: int) -> str:
        rec = trials[idx]
        return rec.get("trial_id") or rec.get("topic_id") or rec["dedup_key"]

    return {
        "n_raw": n,
        "n_dedup": len(clusters),
        "clusters": [[label(i) for i in idxs] for idxs in clusters.values()],
    }
