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
CLAUDE_DIR="$HOME/.claude"
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
st["outputStyle"] = "Fable"          # unifable: Fable orchestrator posture, always on
backup_save(st_p, st)
print(f"  ✓ registered + enabled {key}; outputStyle=Fable; legacy fablize removed from plugin state")
PY

# 2b) Ship the Fable output style (the orchestrator posture; always-on on Claude, set as
#     outputStyle above). Backed up if a prior fable.md exists.
OS_SRC="$CACHE_DIR/output-styles/fable.md"; OS_DST="$CLAUDE_DIR/output-styles/fable.md"
if [ -f "$OS_SRC" ]; then
  mkdir -p "$CLAUDE_DIR/output-styles"
  [ -f "$OS_DST" ] && cp "$OS_DST" "$OS_DST.unifable-bak.$ts"
  cp "$OS_SRC" "$OS_DST"
  echo "  ✓ Fable output style installed → $OS_DST"
else
  echo "  ! output-styles/fable.md not found in cache; outputStyle set but file not shipped"
fi

# 3) Leave the legacy fablize dirs on disk: deleting them mid-session would break the
#    hooks the CURRENT Claude process already loaded from them. They are disabled in the
#    plugin state above and ignored on next restart. Clean up afterwards if desired:
#      rm -rf "$PLUGINS/marketplaces/fablize" "$PLUGINS/cache/fablize"
echo "  · legacy fablize disabled in state; on-disk dirs left intact (safe to delete after restart)"

# 4) Migrate off the old CLAUDE.md operating block. The operating-mode context is now
#    delivered by the SessionStart hook (no CLAUDE.md injection). Strip any prior
#    UNIFABLE / FABLIZE blocks so upgrading users do not carry stale static context.
CLAUDE_MD="$CLAUDE_DIR/CLAUDE.md"
if [ -f "$CLAUDE_MD" ]; then
  cp "$CLAUDE_MD" "$CLAUDE_MD.unifable-bak.$ts"
  python3 - "$CLAUDE_MD" <<'PY'
import sys, re, pathlib
p = pathlib.Path(sys.argv[1])
cur = p.read_text(encoding="utf-8")
new = cur
for tag in ("UNIFABLE", "UNIFABLE-ORCH", "FABLIZE"):
    new = re.sub(r"<!-- " + tag + r":BEGIN.*?" + tag + r":END -->\n?", "", new, flags=re.S)
new = new.rstrip()
if new != cur:
    p.write_text(new + ("\n" if new else ""), encoding="utf-8")
    print("  ✓ CLAUDE.md: stripped prior static block(s) (context now via SessionStart hook)")
else:
    print("  · CLAUDE.md: no prior static block (already clean)")
PY
fi

bash "$CACHE_DIR/setup/install-bin.sh" "$CACHE_DIR" >/dev/null 2>&1 \
  && echo "  ✓ unifable-spec linked into ~/.local/bin" \
  || echo "  ! unifable-spec install skipped (run: bash $CACHE_DIR/setup/install-bin.sh $CACHE_DIR)"

echo "unifable: Claude install complete. RESTART Claude Code for the plugin swap to take effect."
echo "  Operating-mode context is delivered by the SessionStart hook (no CLAUDE.md block)."
echo "  Fallback if it does not appear: /plugin marketplace add jaredboynton/unifable && /plugin install unifable@unifable"
