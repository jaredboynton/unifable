#!/usr/bin/env bash
# Refresh generated hook-output and judge-prompt docs before a commit.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

python3 scripts/generate_docs.py
python3 scripts/generate_docs.py --check

if [ "${STAGE_GENERATED_DOCS:-1}" = "1" ] && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  git add docs/generated/claude-hookoutputs.md \
    docs/generated/codex-hookoutputs.md \
    docs/generated/judgeprompts.md
fi
