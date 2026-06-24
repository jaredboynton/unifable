#!/usr/bin/env bash
# unifable setup — install the spec CLI + record state, and migrate off the
# old static CLAUDE.md/AGENTS.md block injection.
#
# The operating-mode context is now delivered by the SessionStart hook
# (hooks/session_start.py via scripts/gate/context_block.py), so setup no
# longer writes blocks into the host's memory file. This keeps unifable
# scoped to sessions where the plugin is actually enabled and avoids
# polluting context for other CLI tools that read the same memory file.
#
# This script still:
#   * installs the unifable-spec bin into ~/.local/bin,
#   * strips any prior UNIFABLE / FABLIZE block from the host memory file
#     (migration cleanup for users upgrading from a block-injecting release),
#   * records setup state in ~/.unifable/progress.json.
#
# Usage: setup.sh [global|local] [claude|codex]
#   no args = interactive scope; host auto-detected from the plugin root path.
set -euo pipefail

ROOT="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"

command -v python3 >/dev/null 2>&1 || { echo "unifable: python3 is required."; exit 1; }

# --- host detection (overridable by $2) ---
host="${2:-}"
if [ -z "$host" ]; then
  if [ -n "${CLAUDE_PLUGIN_ROOT:-}" ] || printf '%s' "$ROOT" | grep -q "/.claude/"; then
    host=claude
  elif printf '%s' "$ROOT" | grep -q "/.codex/"; then
    host=codex
  else
    host=claude
  fi
fi
case "$host" in claude) MEMNAME="CLAUDE.md";; codex) MEMNAME="AGENTS.md";; *) echo "unifable: host must be claude or codex"; exit 1;; esac

# --- scope ---
scope="${1:-}"
if [ -z "$scope" ]; then
  printf "unifable — setup scope: [l]ocal (this project, recommended) / [g]lobal (all projects): "
  read -r ans
  case "$ans" in g*|G*) scope=global;; l*|L*|"") scope=local;; *) echo "cancelled"; exit 1;; esac
fi
case "$scope" in
  global) case "$host" in claude) MEMFILE="$HOME/.claude/$MEMNAME";; codex) MEMFILE="$HOME/.codex/$MEMNAME";; esac;;
  local)  MEMFILE="$PWD/$MEMNAME";;
  *) echo "unifable: scope must be global or local"; exit 1;;
esac
echo "unifable → $host / $scope"

bash "$ROOT/setup/install-bin.sh" "$ROOT"

# --- migration: strip prior UNIFABLE / UNIFABLE-ORCH / FABLIZE blocks from the
#     host memory file so upgrading users do not carry stale static context.
mkdir -p "$(dirname "$MEMFILE")"; touch "$MEMFILE"
ts=$(python3 -c "import time;print(int(time.time()))")
if [ -s "$MEMFILE" ]; then
  cp "$MEMFILE" "$MEMFILE.unifable-bak.$ts" && echo "  backup: $MEMFILE.unifable-bak.$ts"
  python3 - "$MEMFILE" <<'PY'
import sys, re, pathlib
p = pathlib.Path(sys.argv[1])
cur = p.read_text(encoding="utf-8")
new = cur
for tag in ("UNIFABLE", "UNIFABLE-ORCH", "FABLIZE"):
    new = re.sub(r"<!-- " + tag + r":BEGIN.*?" + tag + r":END -->\n?", "", new, flags=re.S)
new = new.rstrip()
if new != cur:
    p.write_text(new + ("\n" if new else ""), encoding="utf-8")
    print(f"  stripped prior static block(s) from {p.name} (context now delivered via SessionStart hook)")
else:
    print(f"  {p.name}: no prior static block (already clean)")
PY
fi

# Record setup state so the skill won't auto-run setup again.
mkdir -p "$HOME/.unifable"
python3 - "$scope" "$ts" "$host" <<'PY'
import json, sys, os
p = os.path.expanduser("~/.unifable/progress.json")
json.dump({"setup_done": True, "scope": sys.argv[1], "host": sys.argv[3], "version": "1.9.79", "ts": int(sys.argv[2])}, open(p, "w"))
PY

echo "unifable setup complete ($host/$scope) — applies from the next session."
echo "  Operating-mode context is delivered by the SessionStart hook (no CLAUDE.md/AGENTS.md block)."
echo "  state: ~/.unifable/progress.json"
echo "  Uninstall: bash $ROOT/setup/uninstall.sh $scope"
