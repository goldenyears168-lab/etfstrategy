#!/usr/bin/env bash
# Thin entry for docs / manual smoke; production uses Application Support launcher.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
exec bash "${HOME}/Library/Application Support/com.jackm4.etf/cursor-agent-worker.sh"
