#!/usr/bin/env bash
# unifable uninstall — remove prior static blocks from the host's memory file
# and uninstall the spec CLI bin.
#
# The operating-mode context is now delivered by the SessionStart hook, so
# uninstall removes any legacy UNIFABLE / UNIFABLE-ORCH / FABLIZE blocks left
# from a prior release (migration cleanup). Hook entries are removed separately:
#   Claude: uninstall the plugin (/plugin)
#   Codex:  codex plugin remove unifable@unifable
#
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
  printf "unifable — uninstall scope: [l]ocal / [g]lobal: "
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
new = cur
for tag in ("UNIFABLE", "UNIFABLE-ORCH", "FABLIZE"):
    new = re.sub(r"\n*<!-- " + tag + r":BEGIN.*?" + tag + r":END -->\n?", "\n", new, flags=re.S)
new = new.rstrip()
p.write_text(new + ("\n" if new else ""), encoding="utf-8")
print("  removed static block(s)" if new != cur else "  no static block found (already clean)")
PY

echo "unifable uninstall complete ($host/$scope)."
B="${UNIFABLE_BIN_DIR:-$HOME/.local/bin}"
rm -f "$B/unifable" "$B/unifable-hook" "$B/unifable-spec" "$B/unifusion" "$B/unitrace" "$B/unisearch"
echo "  removed (if present): $B/unifable, $B/unifable-hook, $B/unifable-spec, $B/unifusion, $B/unitrace, $B/unisearch"
echo "  Claude: also run /plugin to uninstall the plugin (removes its hooks)."
echo "  Codex:  run 'codex plugin remove unifable@unifable'. If upgrading from a legacy skill install, also remove unifable entries from ~/.codex/hooks.json (back up first)."
