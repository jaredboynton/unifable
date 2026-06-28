#!/usr/bin/env bash
# unifable — Codex installer (native plugin).
#
# The SUPPORTED path is the Codex plugin CLI (mirrors Claude's /plugin):
#   codex plugin marketplace add jaredboynton/unifable
#   codex plugin add unifable@unifable
# This script reproduces that non-interactively: register the marketplace, install +
# enable the plugin (so Codex loads .codex-plugin/plugin.json -> .codex-plugin/hooks.json
# with ${PLUGIN_ROOT} paths), then MIGRATE OFF the legacy skill+hooks.json install so the
# gate is not double-registered. Everything is backed up. Takes effect on next Codex launch.
#
# Source override: UNIFABLE_SOURCE (default "jaredboynton/unifable"). Set to a local path
# for dev (e.g. UNIFABLE_SOURCE="$PWD"); the marketplace then tracks that path.
#
# Usage: bash install/codex.sh
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
CONFIG="$CODEX_HOME/config.toml"
HOOKS_JSON="$CODEX_HOME/hooks.json"
SKILL_OLD="$CODEX_HOME/skills/unifable"  # legacy, pre-native-plugin
SOURCE="${UNIFABLE_SOURCE:-jaredboynton/unifable}"
MKT="unifable"; PLUG="unifable"; KEY="$PLUG@$MKT"

command -v python3 >/dev/null 2>&1 || { echo "unifable: python3 is required."; exit 1; }
command -v codex   >/dev/null 2>&1 || { echo "unifable: codex CLI is required (native plugin install)."; exit 1; }

echo "unifable → Codex (native plugin $KEY)"
echo "  source: $SOURCE"
echo "  config: $CONFIG"

ts="$(python3 -c 'import time;print(int(time.time()))')"

# 1) Register the marketplace + install the plugin via the supported CLI.
#    `marketplace add` is idempotent on the marketplace name; re-running refreshes it.
codex plugin marketplace add "$SOURCE" >/dev/null 2>&1 \
  && echo "  ✓ marketplace '$MKT' registered" \
  || { echo "  ! 'codex plugin marketplace add $SOURCE' failed — run it manually, then re-run."; exit 1; }
# `add` no-ops on an already-registered marketplace; `upgrade` refreshes the git snapshot to
# the latest commit (needed when re-running to pick up a new release).
codex plugin marketplace upgrade "$MKT" >/dev/null 2>&1 && echo "  ✓ marketplace '$MKT' refreshed to latest" || true

codex plugin remove "$KEY" >/dev/null 2>&1 || true
codex plugin add "$KEY" >/dev/null 2>&1 \
  && echo "  ✓ plugin '$KEY' installed (cached under $CODEX_HOME/plugins/cache/$MKT/$PLUG/)" \
  || { echo "  ! 'codex plugin add $KEY' failed — run it manually, then re-run."; exit 1; }

# 2) Force-enable in config.toml. `codex plugin add` may leave enabled=false; the plugin's
#    hooks only load when [plugins."unifable@unifable"] enabled = true. Idempotent text edit.
[ -f "$CONFIG" ] && cp "$CONFIG" "$CONFIG.unifable-bak.$ts"
python3 - "$CONFIG" "$KEY" <<'PY'
import sys, re
config, key = sys.argv[1], sys.argv[2]
try:
    text = open(config).read()
except FileNotFoundError:
    text = ""
header = f'[plugins."{key}"]'
lines = text.splitlines()
out, i, found = [], 0, False
while i < len(lines):
    line = lines[i]
    if line.strip() == header:
        found = True
        out.append(line)
        i += 1
        wrote_enabled = False
        # rewrite the section body until the next [section] header
        while i < len(lines) and not lines[i].lstrip().startswith("["):
            if re.match(r"\s*enabled\s*=", lines[i]):
                out.append("enabled = true")
                wrote_enabled = True
            else:
                out.append(lines[i])
            i += 1
        if not wrote_enabled:
            out.append("enabled = true")
    else:
        out.append(line)
        i += 1
if not found:
    if out and out[-1].strip() != "":
        out.append("")
    out.append(header)
    out.append("enabled = true")
open(config, "w").write("\n".join(out) + "\n")
print(f"  ✓ {header} enabled = true")
PY

# 3) Migrate OFF the legacy install so the gate is not double-registered.
#    (a) strip unifable entries from the global ~/.codex/hooks.json (kept for non-unifable hooks),
#    (b) remove the old ~/.codex/skills/unifable copy if it exists. Both backed up.
if [ -f "$HOOKS_JSON" ]; then
  cp "$HOOKS_JSON" "$HOOKS_JSON.unifable-bak.$ts"
  python3 - "$HOOKS_JSON" <<'PY'
import json, sys
p = sys.argv[1]
try:
    data = json.load(open(p))
except Exception:
    sys.exit(0)
hooks = data.get("hooks", {})
def is_unifable(entry):
    blob = json.dumps(entry)
    return ("unifable" in blob) or ("fable-inject" in blob) \
        or ("gate_prompt" in blob) or ("gate_post_tool" in blob) or ("gate_stop" in blob) \
        or ("pre_tool_use" in blob) or ("test_after_edit" in blob) or ("router.sh" in blob)
for event, groups in list(hooks.items()):
    kept = [g for g in groups if not is_unifable(g)]
    if kept:
        hooks[event] = kept
    else:
        del hooks[event]
json.dump(data, open(p, "w"), indent=2); open(p, "a").write("\n")
print("  ✓ stripped legacy unifable entries from ~/.codex/hooks.json")
PY
fi
if [ -d "$SKILL_OLD" ]; then
  mv "$SKILL_OLD" "$SKILL_OLD.unifable-bak.$ts"
  echo "  ✓ legacy ~/.codex/skills/unifable retired (backup: $SKILL_OLD.unifable-bak.$ts)"
fi

# 4) Remove the upstream fivetaku fablize skill dir if present (replace, not coexist).
if [ -d "$CODEX_HOME/skills/fablize" ]; then
  rm -rf "$CODEX_HOME/skills/fablize"
  echo "  ✓ removed legacy ~/.codex/skills/fablize"
fi

# 5) Seed the stable ~/.unifable runtime + bin links (so `unifable`/`unifusion`
#    work on PATH before the first SessionStart fires) and strip any prior
#    <!-- UNIFABLE --> / <!-- UNIFABLE-ORCH --> / <!-- FABLIZE --> static block
#    from ~/.codex/AGENTS.md (legacy migration cleanup — context is now delivered
#    by the SessionStart hook, not a static block). The SessionStart hook keeps
#    ~/.unifable and the bin links current on every session thereafter.
MEMFILE="$CODEX_HOME/AGENTS.md"
mkdir -p "$(dirname "$MEMFILE")"; touch "$MEMFILE"
ts="$(python3 -c 'import time;print(int(time.time()))')"
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
CACHE_SYNC="$(find "$CODEX_HOME/plugins/cache/$MKT/$PLUG" -maxdepth 4 -name runtime_sync.py -path '*/scripts/gate/*' 2>/dev/null | sort | tail -1)"
if [ -n "$CACHE_SYNC" ]; then
  CACHE_ROOT="$(dirname "$(dirname "$(dirname "$CACHE_SYNC")")")"
  UNIFABLE_BIN_DIR="$HOME/.local/bin" python3 "$CACHE_SYNC" --source "$CACHE_ROOT" >/dev/null 2>&1 \
    && echo "  ✓ unifable runtime seeded under ~/.unifable; unifable + unifusion linked into $HOME/.local/bin" \
    || echo "  ! runtime sync skipped (will self-heal on next SessionStart)"
else
  echo "  ! runtime_sync.py not found under $CODEX_HOME/plugins/cache/$MKT/$PLUG — bin link skipped (self-heals on next SessionStart)"
fi

echo "unifable: Codex native-plugin install complete. RESTART Codex; the plugin loads its own hooks."
echo "  Verify: codex plugin list   (expect '$KEY' enabled)"
