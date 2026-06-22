#!/usr/bin/env bash
# unifable uninstall — remove the UNIFABLE operating block from the host's memory file (idempotent).
# Host-aware: Claude -> CLAUDE.md, Codex -> AGENTS.md. Hook entries are removed separately
# (Claude: uninstall the plugin; Codex: re-run nothing — edit ~/.codex/hooks.json or see install/codex.sh).
# Usage: uninstall.sh [global|local] [claude|codex]
set -euo pipefail

ROOT="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"

host="${2:-}"
if [ -z "$host" ]; then
  if [ -n "${CLAUDE_PLUGIN_ROOT:-}" ] || printf '%s' "$ROOT" | grep -q "/.claude/"; then host=claude
  elif printf '%s' "$ROOT" | grep -q "/.codex/"; then host=codex
  else host=claude; fi
fi
case "$host" in claude) MEMNAME="CLAUDE.md";; codex) MEMNAME="AGENTS.md";; *) echo "unifable: host must be claude or codex"; exit 1;; esac

scope="${1:-}"
if [ -z "$scope" ]; then
  printf "unifable — remove the operating block from: [l]ocal / [g]lobal: "
  read -r ans
  case "$ans" in g*|G*) scope=global;; *) scope=local;; esac
fi
case "$scope" in
  global) case "$host" in claude) MEMFILE="$HOME/.claude/$MEMNAME";; codex) MEMFILE="$HOME/.codex/$MEMNAME";; esac;;
  local)  MEMFILE="$PWD/$MEMNAME";;
  *) echo "unifable: scope must be global or local"; exit 1;;
esac
[ -f "$MEMFILE" ] || { echo "unifable: $MEMFILE not found — nothing to remove."; exit 0; }

python3 - "$MEMFILE" <<'PY'
import sys, re, pathlib
p = pathlib.Path(sys.argv[1])
cur = p.read_text(encoding="utf-8")
new = re.sub(r"\n*<!-- UNIFABLE:BEGIN.*?UNIFABLE:END -->\n?", "\n", cur, flags=re.S)
p.write_text(new, encoding="utf-8")
print("  ✓ UNIFABLE block removed" if new != cur else "  = no UNIFABLE block (already removed)")
PY

echo "unifable uninstall complete ($host/$scope)."
rm -f "${UNIFABLE_BIN_DIR:-$HOME/.local/bin}/unifable"
rm -f "${UNIFABLE_BIN_DIR:-$HOME/.local/bin}/unifable-spec"
echo "  removed: ${UNIFABLE_BIN_DIR:-$HOME/.local/bin}/unifable (if present)"
echo "  removed: ${UNIFABLE_BIN_DIR:-$HOME/.local/bin}/unifable-spec (if present)"
echo "  Claude: also run /plugin to uninstall the plugin (removes its hooks)."
echo "  Codex:  remove unifable entries from ~/.codex/hooks.json (commands referencing skills/unifable/hooks)."
