#!/usr/bin/env bash
# unifable — Claude Code installer (convenience).
#
# The SUPPORTED path is the interactive command:
#   /plugin marketplace add jaredboynton/unifable
#   /plugin install unifable@unifable
# This script reproduces that on-disk so it can run non-interactively: it clones the
# marketplace, registers + enables the plugin, disables/removes the legacy fablize, and
# strips any prior CLAUDE.md operating block (the operating-mode context is now delivered
# by the SessionStart hook, not a static block). Everything is backed up. Takes effect on
# next Claude Code restart. If the plugin does not appear, fall back to the command above.
#
# Usage: bash install/claude.sh
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
PLUGINS="$CLAUDE_DIR/plugins"
URL="https://github.com/jaredboynton/unifable.git"
MKT="unifable"; PLUG="unifable"
VERSION="$(python3 -c "import json;print(json.load(open('$REPO/.claude-plugin/plugin.json'))['version'])")"
SHA="$(git -C "$REPO" rev-parse HEAD)"
MKT_DIR="$PLUGINS/marketplaces/$MKT"
CACHE_DIR="$PLUGINS/cache/$MKT/$PLUG/$VERSION"

command -v python3 >/dev/null 2>&1 || { echo "unifable: python3 required."; exit 1; }
echo "unifable → Claude Code  (plugin $PLUG@$MKT v$VERSION, $SHA)"

# 1) Clone the marketplace (from local repo for determinism) and set GitHub origin.
rm -rf "$MKT_DIR"; mkdir -p "$(dirname "$MKT_DIR")"
git clone -q "$REPO" "$MKT_DIR"
git -C "$MKT_DIR" remote set-url origin "$URL"
# Cache the plugin payload (source ":./" => plugin root == marketplace root).
rm -rf "$CACHE_DIR"; mkdir -p "$CACHE_DIR"
rsync -a --exclude '.git' "$MKT_DIR"/ "$CACHE_DIR"/
echo "  ✓ marketplace cloned + plugin cached"

# 2) Register + enable in Claude's plugin state (backup each file). Remove legacy fablize.
ts="$(python3 -c 'import time;print(int(time.time()))')"
python3 - "$PLUGINS" "$CLAUDE_DIR" "$MKT" "$PLUG" "$VERSION" "$SHA" "$URL" "$MKT_DIR" "$CACHE_DIR" "$ts" <<'PY'
import json, os, sys, shutil, datetime
plugins, claude_dir, mkt, plug, version, sha, url, mkt_dir, cache_dir, ts = sys.argv[1:11]
key = f"{plug}@{mkt}"

def load(p):
    return json.load(open(p)) if os.path.exists(p) else {}
def backup_save(p, data):
    if os.path.exists(p):
        shutil.copy(p, f"{p}.unifable-bak.{ts}")
    json.dump(data, open(p, "w"), indent=2); open(p, "a").write("\n")

# known_marketplaces.json
km_p = f"{plugins}/known_marketplaces.json"; km = load(km_p)
km.pop("fablize", None)  # remove legacy
km[mkt] = {"source": {"source": "git", "url": url},
           "installLocation": mkt_dir,
           "lastUpdated": datetime.datetime.now(datetime.timezone.utc).isoformat()}
backup_save(km_p, km)

# installed_plugins.json (v2: {"version":2,"plugins":{...}})
ip_p = f"{plugins}/installed_plugins.json"; ip = load(ip_p) or {"version": 2, "plugins": {}}
ip.setdefault("plugins", {})
ip["plugins"].pop("fablize@fablize", None)  # remove legacy
now = datetime.datetime.now(datetime.timezone.utc).isoformat()
ip["plugins"][key] = [{"scope": "user", "installPath": cache_dir, "version": version,
                       "installedAt": now, "lastUpdated": now, "gitCommitSha": sha}]
backup_save(ip_p, ip)

# settings.json enabledPlugins
st_p = f"{claude_dir}/settings.json"; st = load(st_p)
ep = st.setdefault("enabledPlugins", {})
ep.pop("fablize@fablize", None)  # remove legacy
ep[key] = True
st["alwaysThinkingEnabled"] = True  # unifable: deliberate thinking on by default
st["outputStyle"] = "mute"          # unifable: mute by default (silent between tools; Fable remains available)
backup_save(st_p, st)
print(f"  ✓ registered + enabled {key}; outputStyle=mute; legacy fablize removed from plugin state")
PY

# 2b) Ship the output styles. mute.md is the default (outputStyle=mute above);
#     fable.md ships alongside so the Fable orchestrator posture stays selectable.
#     Each is backed up if a prior copy exists.
mkdir -p "$CLAUDE_DIR/output-styles"
for os in mute.md fable.md; do
  OS_SRC="$CACHE_DIR/output-styles/$os"; OS_DST="$CLAUDE_DIR/output-styles/$os"
  if [ -f "$OS_SRC" ]; then
    [ -f "$OS_DST" ] && cp "$OS_DST" "$OS_DST.unifable-bak.$ts"
    cp "$OS_SRC" "$OS_DST"
    echo "  ✓ output style installed → $OS_DST"
  else
    echo "  ! output-styles/$os not found in cache; skipping"
  fi
done

# 3) Leave the legacy fablize dirs on disk: deleting them mid-session would break the
#    hooks the CURRENT Claude process already loaded from them. They are disabled in the
#    plugin state above and ignored on next restart. Clean up afterwards if desired:
#      rm -rf "$PLUGINS/marketplaces/fablize" "$PLUGINS/cache/fablize"
echo "  · legacy fablize disabled in state; on-disk dirs left intact (safe to delete after restart)"

# 4) Delegate the shared setup tail to setup.sh so the bin link, CLAUDE.md block
#    strip, and ~/.unifable/progress.json state record all stay owned in one place
#    (no drift between the installer and setup.sh). Idempotent; takes effect next
#    session. setup.sh is host-aware (auto-detects claude from the cache path).
bash "$CACHE_DIR/setup/setup.sh" global claude

echo "unifable: Claude install complete. RESTART Claude Code for the plugin swap to take effect."
echo "  Operating-mode context is delivered by the SessionStart hook (no CLAUDE.md block)."
echo "  Fallback if it does not appear: /plugin marketplace add jaredboynton/unifable && /plugin install unifable@unifable"
