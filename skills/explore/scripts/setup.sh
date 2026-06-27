#!/usr/bin/env bash
# Preflight checks for the explore skill.
#
# Unlike a vendored single-binary skill, explore depends on the Cursor Agent
# CLI (a system tool that requires an authenticated session) plus Node.js for
# the default ACP transport. This script checks those prerequisites, installs
# cursor-agent via the official installer if it is missing, and reports auth
# status. It never requires sudo and is safe to re-run.
#
# Env:
#   EXPLORE_SKIP_INSTALL=1   check only; do not auto-install cursor-agent
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
# shellcheck source=hermetic-home.sh
. "$SCRIPT_DIR/hermetic-home.sh"
explore_apply_hermetic_default
# shellcheck source=env.sh
. "$SCRIPT_DIR/env.sh"
explore_load_skill_env "$SKILL_DIR"

ok()   { printf '  [ok]   %s\n' "$1"; }
warn() { printf '  [warn] %s\n' "$1" >&2; }
fail() { printf '  [fail] %s\n' "$1" >&2; }

problems=0

echo "explore setup: checking prerequisites"

# Node.js (required for the default acp transport).
if command -v node >/dev/null 2>&1; then
  ok "node $(node --version)"
else
  fail "node not found on PATH (required for EXPLORE_TRANSPORT=acp)."
  warn "install Node.js, or run traces with EXPLORE_TRANSPORT=cli (needs jq)."
  problems=$((problems + 1))
fi

# Cursor Agent CLI (required).
if command -v cursor-agent >/dev/null 2>&1; then
  ok "cursor-agent on PATH ($(command -v cursor-agent))"
else
  if [ "${EXPLORE_SKIP_INSTALL:-0}" = "1" ]; then
    fail "cursor-agent not found and EXPLORE_SKIP_INSTALL=1; skipping install."
    problems=$((problems + 1))
  else
    warn "cursor-agent not found; installing via official installer..."
    curl https://cursor.com/install -fsS | bash
    # The installer drops the binary in ~/.local/bin.
    case ":$PATH:" in
      *":$HOME/.local/bin:"*) : ;;
      *) export PATH="$HOME/.local/bin:$PATH" ;;
    esac
    if command -v cursor-agent >/dev/null 2>&1; then
      ok "cursor-agent installed ($(command -v cursor-agent))"
      warn "ensure ~/.local/bin is on your PATH in your shell rc."
    else
      fail "cursor-agent still not found after install."
      problems=$((problems + 1))
    fi
  fi
fi

# Auth status (required for traces to run).
if command -v cursor-agent >/dev/null 2>&1; then
  if status_out="$(cursor-agent status 2>&1)"; then
    if printf '%s' "$status_out" | grep -qi "logged in"; then
      ok "$(printf '%s' "$status_out" | grep -i 'logged in' | head -1 | sed 's/^[^A-Za-z]*//')"
    else
      warn "cursor-agent is not logged in. Run: cursor-agent login"
      warn "(or set CURSOR_API_KEY in the environment)."
      problems=$((problems + 1))
    fi
  else
    warn "could not query cursor-agent status. Run: cursor-agent login"
    problems=$((problems + 1))
  fi
fi

# Hermetic HOME bootstrap (default trace isolation).
if command -v cursor-agent >/dev/null 2>&1; then
  explore_real="$(explore_real_home)"
  explore_base="$(cd "$explore_real/.cache/explore" 2>/dev/null && pwd || true)"
  if [ -z "$explore_base" ]; then
    explore_base="$explore_real/.cache/explore"
    mkdir -p "$explore_base"
    explore_base="$(cd "$explore_base" && pwd)"
  fi
  explore_hermetic="$(explore_hermetic_home_dir "$explore_base")"
  if explore_ensure_hermetic_home "$explore_real" "$explore_hermetic" >/dev/null; then
    ok "hermetic HOME ready ($explore_hermetic)"
  else
    warn "hermetic HOME not bootstrapped (auth.json missing). Run: cursor-agent login"
    problems=$((problems + 1))
  fi
fi

# jq (optional: clean final-answer extraction for the cli transport).
if command -v jq >/dev/null 2>&1; then
  ok "jq $(jq --version 2>/dev/null)"
else
  warn "jq not found (optional). The cli transport falls back to raw output; the default acp transport does not need it."
fi

# --- search.sh prerequisites (rg + Codex OAuth; gpt-realtime-2) ---
echo
echo "explore setup: checking search.sh prerequisites"

# ripgrep (required for search.sh agentic ripgrep loop).
if command -v rg >/dev/null 2>&1; then
  ok "rg $(rg --version | head -1)"
else
  fail "rg (ripgrep) not found on PATH (required for search.sh)."
  warn "install: brew install ripgrep  (macOS)"
  warn "         apt install ripgrep   (Debian/Ubuntu)"
  warn "         https://github.com/BurntSushi/ripgrep#installation"
  problems=$((problems + 1))
fi

# search.sh runs search-rt.mjs on Bun when present, falling back to Node.
if command -v bun >/dev/null 2>&1; then
  ok "bun available for search-rt.mjs ($(bun --version))"
elif command -v node >/dev/null 2>&1; then
  ok "node available for search-rt.mjs ($(node --version))"
else
  fail "bun or node not found on PATH (required for search.sh)"
  problems=$((problems + 1))
fi

# search.sh runs on gpt-realtime-2 (Codex OAuth via search-rt.mjs); auth is the shared Codex check below.
if [ -f "${EXPLORE_CODEX_AUTH_PATH:-${HOME}/.codex/auth.json}" ]; then
  ok "Codex auth for search.sh (gpt-realtime-2 via search-rt.mjs)"
else
  warn "Codex auth not found. search.sh (RT) will fail without it. Run: codex login"
fi

# --- websearch stack prerequisites ---
echo
echo "explore setup: checking websearch.sh (default RT path)"

if [ -f "${EXPLORE_CODEX_AUTH_PATH:-${HOME}/.codex/auth.json}" ]; then
  ok "Codex auth for websearch-rt.sh (${EXPLORE_CODEX_AUTH_PATH:-${HOME}/.codex/auth.json})"
else
  warn "Codex auth not found. websearch.sh (RT default) will fail without it."
  warn "Run: codex login"
fi

# --- map.sh + bench prerequisites ---
echo
echo "explore setup: checking map.sh prerequisites"

if command -v node >/dev/null 2>&1; then
  ok "node available for map.mjs"
else
  fail "node not found (required for map.sh)"
  problems=$((problems + 1))
fi

if command -v git >/dev/null 2>&1; then
  ok "git on PATH (map file enumeration)"
else
  warn "git not found; map.sh falls back to directory walk"
fi

ok "map engines: pagerank (def/ref graph) + sigmap-style TF-IDF + ast-grep signatures for uncovered langs"

# --- ast-context (ast-grep for search hit expansion + map signature extraction) ---
echo
echo "explore setup: checking ast-context prerequisites"

if command -v node >/dev/null 2>&1; then
  if node "$SCRIPT_DIR/ast-context.mjs" --check >/dev/null 2>&1; then
    ok "ast-grep on PATH ($(node "$SCRIPT_DIR/ast-context.mjs" --check)) — search AST context + map signature extraction"
  elif [ "${EXPLORE_SKIP_INSTALL:-0}" = "1" ]; then
    warn "ast-grep not found and EXPLORE_SKIP_INSTALL=1; AST context expansion disabled until installed."
  else
    warn "ast-grep not found; installing via ast-context.mjs..."
    if node "$SCRIPT_DIR/ast-context.mjs" --ensure; then
      ok "ast-grep ready ($(node "$SCRIPT_DIR/ast-context.mjs" --check))"
    else
      warn "ast-grep install failed. search still works; AST context expansion disabled."
    fi
  fi
else
  warn "node not found; skipping ast-grep setup"
fi

# --- trace-rt.sh prerequisites (gpt-realtime-2 + Codex OAuth) ---
echo
echo "explore setup: checking trace.sh / trace-rt.sh prerequisites"

if command -v node >/dev/null 2>&1; then
  ok "node available for realtime-trace.mjs"
else
  fail "node not found (required for trace-rt.sh)"
  problems=$((problems + 1))
fi

codex_auth="${EXPLORE_CODEX_AUTH_PATH:-${HOME}/.codex/auth.json}"
if [ -f "$codex_auth" ]; then
  if grep -q '"access_token"' "$codex_auth" 2>/dev/null; then
    ok "Codex auth present ($codex_auth)"
  else
    warn "Codex auth file exists but tokens.access_token missing. Run: codex login"
    problems=$((problems + 1))
  fi
else
  warn "Codex auth not found at $codex_auth. trace-rt.sh requires `codex login`."
  problems=$((problems + 1))
fi

echo
if [ "$problems" -eq 0 ]; then
  echo "explore setup: ready."
else
  echo "explore setup: $problems issue(s) above need attention." >&2
  exit 1
fi
