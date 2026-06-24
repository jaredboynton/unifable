#!/usr/bin/env bash
# unifable always-on setup — inject the operating block into the host's memory file
# (idempotent, with backup). Host-aware:
#   * Claude Code  -> CLAUDE.md   (hooks auto-register from hooks.json on plugin install)
#   * Codex        -> AGENTS.md   (hooks registered in ~/.codex/hooks.json by install/codex.sh)
# It also strips any legacy FABLIZE block, so installing unifable cleans up a prior fablize.
# Usage: setup.sh [global|local] [claude|codex]
#   no args = interactive scope; host auto-detected from the plugin root path.
set -euo pipefail

ROOT="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
BLOCK_TPL="$ROOT/setup/unifable-block.md"

command -v python3 >/dev/null 2>&1 || { echo "unifable: python3 is required."; exit 1; }
[ -f "$BLOCK_TPL" ] || { echo "unifable: block template not found ($BLOCK_TPL)"; exit 1; }

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
  printf "unifable — inject the operating block into: [l]ocal (this project, recommended) / [g]lobal (all projects): "
  read -r ans
  case "$ans" in g*|G*) scope=global;; l*|L*|"") scope=local;; *) echo "cancelled"; exit 1;; esac
fi
case "$scope" in
  global) case "$host" in claude) MEMFILE="$HOME/.claude/$MEMNAME";; codex) MEMFILE="$HOME/.codex/$MEMNAME";; esac;;
  local)  MEMFILE="$PWD/$MEMNAME";;
  *) echo "unifable: scope must be global or local"; exit 1;;
esac
echo "unifable → $host / $scope ($MEMFILE)"

bash "$ROOT/setup/install-bin.sh" "$ROOT"

mkdir -p "$(dirname "$MEMFILE")"; touch "$MEMFILE"
ts=$(python3 -c "import time;print(int(time.time()))")
cp "$MEMFILE" "$MEMFILE.unifable-bak.$ts" && echo "  backup: $MEMFILE.unifable-bak.$ts"

# Substitute __PLUGIN_ROOT__ -> real path; strip prior UNIFABLE *and* legacy FABLIZE
# blocks, then inject idempotently.
python3 - "$MEMFILE" "$BLOCK_TPL" "$ROOT" <<'PY'
import sys, re, pathlib
md, tpl, root = sys.argv[1], sys.argv[2], sys.argv[3]
gate = pathlib.Path(root) / "scripts" / "gate"
sys.path.insert(0, str(gate))
from research_bash_guidance import explore_trace_inline_md, explore_trace_list_item_md
p = pathlib.Path(md)
cur = p.read_text(encoding="utf-8") if p.exists() else ""
block = pathlib.Path(tpl).read_text(encoding="utf-8").strip()
block = block.replace("__PLUGIN_ROOT__", root)
block = block.replace("__EXPLORE_TRACE_LIST__", explore_trace_list_item_md())
block = block.replace("__EXPLORE_TRACE_INLINE__", explore_trace_inline_md())
cur = re.sub(r"<!-- UNIFABLE:BEGIN.*?UNIFABLE:END -->\n?", "", cur, flags=re.S)
cur = re.sub(r"<!-- FABLIZE:BEGIN.*?FABLIZE:END -->\n?", "", cur, flags=re.S).rstrip()
p.write_text((cur + "\n\n" + block + "\n") if cur else (block + "\n"), encoding="utf-8")
print(f"  ✓ {pathlib.Path(md).name}: UNIFABLE operating block injected (legacy FABLIZE block removed)")
PY

# Record setup state so the skill won't auto-run setup again.
mkdir -p "$HOME/.unifable"
python3 - "$scope" "$ts" "$host" <<'PY'
import json, sys, os
p = os.path.expanduser("~/.unifable/progress.json")
json.dump({"setup_done": True, "scope": sys.argv[1], "host": sys.argv[3], "version": "1.9.77", "ts": int(sys.argv[2])}, open(p, "w"))
PY

echo "unifable setup complete ($host/$scope) — applies from the next session."
echo "  state: ~/.unifable/progress.json"
echo "  Uninstall: bash $ROOT/setup/uninstall.sh $scope"
