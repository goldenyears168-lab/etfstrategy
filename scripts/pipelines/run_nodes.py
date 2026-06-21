#!/usr/bin/env python3
"""Execute a pipeline phase from config/pipelines/*.yaml (daily_close evening_research)."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_PIPELINE = ROOT / "config" / "pipelines" / "daily_close.yaml"


@dataclass(frozen=True)
class PipelineNode:
    node_id: str
    label: str
    module: str
    args: tuple[str, ...]
    args_if_quiet: tuple[str, ...]
    env_flag: str | None
    env_default: str
    fail_kind: str


def _load_phase(path: Path, phase_id: str) -> tuple[dict[str, Any], list[PipelineNode]]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    for phase in raw.get("phases") or []:
        if phase.get("id") != phase_id:
            continue
        nodes: list[PipelineNode] = []
        for body in phase.get("nodes") or []:
            if not isinstance(body, dict):
                continue
            nodes.append(
                PipelineNode(
                    node_id=str(body["id"]),
                    label=str(body["label"]),
                    module=str(body["module"]),
                    args=tuple(str(a) for a in (body.get("args") or [])),
                    args_if_quiet=tuple(str(a) for a in (body.get("args_if_quiet") or [])),
                    env_flag=str(body["env_flag"]) if body.get("env_flag") else None,
                    env_default=str(body.get("env_default", "1")),
                    fail_kind=str(body.get("fail_kind", "aux")),
                )
            )
        return raw, nodes
    raise SystemExit(f"unknown pipeline phase: {phase_id}")


def _env_enabled(flag: str | None, default: str) -> bool:
    if not flag:
        return True
    return os.environ.get(flag, default) == "1"


def _log_line(message: str, *, log_file: Path | None, quiet: bool, show_report: bool) -> None:
    if quiet and not show_report:
        if log_file is not None:
            with log_file.open("a", encoding="utf-8") as fh:
                fh.write(message + "\n")
        return
    print(message)
    if log_file is not None:
        with log_file.open("a", encoding="utf-8") as fh:
            fh.write(message + "\n")


def _resolve_args(node: PipelineNode, *, quiet: bool) -> tuple[str, ...]:
    args = list(node.args)
    if quiet and node.args_if_quiet:
        args.extend(node.args_if_quiet)
    return tuple(args)


def _run_node(
    node: PipelineNode,
    *,
    python: Path,
    root: Path,
    log_file: Path | None,
    quiet: bool,
    show_report: bool,
) -> bool:
    """Return True on success."""
    t0 = time.time()
    _log_line(f"--- {node.label} ---", log_file=log_file, quiet=quiet, show_report=show_report)

    module_path = root / node.module
    cmd = [
        str(python),
        str(module_path),
        *_resolve_args(node, quiet=quiet),
    ]
    env = os.environ.copy()
    for key in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        env.pop(key, None)

    if quiet and not show_report:
        with log_file.open("a", encoding="utf-8") if log_file else open(os.devnull, "w") as out:
            proc = subprocess.run(cmd, cwd=root, env=env, stdout=out, stderr=subprocess.STDOUT)
        ok = proc.returncode == 0
    else:
        proc = subprocess.run(cmd, cwd=root, env=env)
        ok = proc.returncode == 0

    elapsed = int(time.time() - t0)
    status = "OK" if ok else "WARN"
    _log_line(f"{status}: {node.label} ({elapsed}s)", log_file=log_file, quiet=quiet, show_report=show_report)
    return ok


def execute_phase(
    *,
    pipeline_path: Path,
    phase_id: str,
    python: Path,
    root: Path,
    log_file: Path | None,
    quiet: bool,
    show_report: bool,
    state_file: Path | None,
) -> int:
    _, nodes = _load_phase(pipeline_path, phase_id)
    aux_failed = False
    holdings_failed = False

    for node in nodes:
        if not _env_enabled(node.env_flag, node.env_default):
            skip_flag = node.env_flag or "?"
            _log_line(f"--- {node.label} ---", log_file=log_file, quiet=quiet, show_report=show_report)
            _log_line(f"  SKIP（{skip_flag}=0）", log_file=log_file, quiet=quiet, show_report=show_report)
            continue
        ok = _run_node(
            node,
            python=python,
            root=root,
            log_file=log_file,
            quiet=quiet,
            show_report=show_report,
        )
        if not ok:
            if node.fail_kind == "holdings":
                holdings_failed = True
            else:
                aux_failed = True

    if state_file is not None:
        lines: list[str] = []
        if aux_failed:
            lines.append("aux_failed=1")
        if holdings_failed:
            lines.append("holdings_failed=1")
        if lines:
            state_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return 1 if holdings_failed else 0


def list_phase_nodes(pipeline_path: Path, phase_id: str) -> list[PipelineNode]:
    _, nodes = _load_phase(pipeline_path, phase_id)
    return nodes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a pipeline phase from YAML.")
    parser.add_argument(
        "--pipeline",
        type=Path,
        default=DEFAULT_PIPELINE,
        help="Pipeline YAML path",
    )
    parser.add_argument(
        "--phase",
        default="evening_research",
        help="Phase id inside the pipeline YAML",
    )
    parser.add_argument("--python", type=Path, default=ROOT / ".venv/bin/python")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--log-file", type=Path, default=None)
    parser.add_argument("--quiet", choices=("0", "1"), default="0")
    parser.add_argument("--show-report", choices=("0", "1"), default="0")
    parser.add_argument(
        "--state-file",
        type=Path,
        default=None,
        help="Write aux_failed=1 / holdings_failed=1 markers for the shell wrapper",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="Print node ids for the phase and exit",
    )
    args = parser.parse_args(argv)

    if args.list:
        for node in list_phase_nodes(args.pipeline, args.phase):
            print(node.node_id)
        return 0

    return execute_phase(
        pipeline_path=args.pipeline,
        phase_id=args.phase,
        python=args.python,
        root=args.root,
        log_file=args.log_file,
        quiet=args.quiet == "1",
        show_report=args.show_report == "1",
        state_file=args.state_file,
    )


if __name__ == "__main__":
    raise SystemExit(main())
