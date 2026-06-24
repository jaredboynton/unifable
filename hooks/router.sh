#!/usr/bin/env bash
# unifable UserPromptSubmit router — thin wrapper for scripts/gate/pack_router.py
# Routing data lives in packs/router-manifest.json (priority field reserved for future use).
# stdin: JSON {"prompt": "..."}. stdout: extra context (only when a signal matches). Always exits 0.
set -uo pipefail
ROOT="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
export CLAUDE_PLUGIN_ROOT="$ROOT"
export PLUGIN_ROOT="$ROOT"
export UNIFABLE_PLUGIN_ROOT="$ROOT"
exec python3 "$ROOT/scripts/gate/pack_router.py"
