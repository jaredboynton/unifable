#!/usr/bin/env bash
# unifable — Codex installer.
#
# Codex implements Claude-Code-compatible hooks but has no plugin marketplace,
# so installation is: (1) copy the skill into ~/.codex/skills/unifable, and
# (2) merge unifable's hook entries into the global ~/.codex/hooks.json without
# disturbing any non-unifable hooks. Idempotent: re-running strips prior
# fablize/unifable entries first. Existing PreToolUse/SessionStart hooks (shell
# guards, reminders, etc.) are preserved untouched.
#
# Usage: bash install/codex.sh
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
SKILL_DST="$CODEX_HOME/skills/unifable"
HOOKS_JSON="$CODEX_HOME/hooks.json"

command -v python3 >/dev/null 2>&1 || { echo "unifable: python3 is required."; exit 1; }
command -v rsync   >/dev/null 2>&1 || { echo "unifable: rsync is required."; exit 1; }

echo "unifable → Codex"
echo "  repo:  $REPO"
echo "  skill: $SKILL_DST"
echo "  hooks: $HOOKS_JSON"

# 1) Copy the skill payload (exclude VCS / caches / host state).
mkdir -p "$SKILL_DST"
rsync -a --delete \
  --exclude '.git' --exclude '.gitignore' --exclude '__pycache__' --exclude '.omc' --exclude '.DS_Store' \
  --exclude '.claude-plugin' --exclude 'commands' --exclude 'docs' \
  --exclude 'README.md' --exclude 'CHANGELOG.md' \
  "$REPO"/ "$SKILL_DST"/
echo "  ✓ skill copied (Codex payload: hooks/ scripts/ packs/ agents/ setup/ install/ SKILL.md)"

# 2) Merge hook entries into ~/.codex/hooks.json (backup first).
ts="$(python3 -c 'import time;print(int(time.time()))')"
[ -f "$HOOKS_JSON" ] && cp "$HOOKS_JSON" "$HOOKS_JSON.unifable-bak.$ts" && echo "  backup: $HOOKS_JSON.unifable-bak.$ts"

python3 "$REPO/install/merge_hooks.py" "$HOOKS_JSON"

# 3) Remove the legacy fivetaku fablize skill dir if present (replace, not coexist).
if [ -d "$CODEX_HOME/skills/fablize" ]; then
  rm -rf "$CODEX_HOME/skills/fablize"
  echo "  ✓ removed legacy ~/.codex/skills/fablize"
fi

# 4) Operating block in ~/.codex/AGENTS.md — opt-in. The hooks alone deliver the
#    gate; this only adds the always-on routing text. Enable with UNIFABLE_BLOCK=1.
if [ "${UNIFABLE_BLOCK:-0}" = "1" ] && [ -f "$SKILL_DST/setup/setup.sh" ]; then
  bash "$SKILL_DST/setup/setup.sh" global codex >/dev/null 2>&1 \
    && echo "  ✓ operating block injected into ~/.codex/AGENTS.md" \
    || echo "  ! setup.sh block injection skipped"
else
  echo "  · operating block not injected (set UNIFABLE_BLOCK=1 to add the always-on routing text)"
fi

echo "unifable: Codex install complete. Trust the new hooks via /hooks on next launch."
