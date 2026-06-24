#!/usr/bin/env bash
# Regression: unifable-hook must dispatch from ~/.local/bin symlink without exit 1.
# Run: bash tests/test_unifable_hook_dispatch.sh
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REPO_HOOK="$ROOT/bin/unifable-hook"
PAYLOAD='{"prompt":"debug this failing test","session_id":"dispatch-test","cwd":"'"$ROOT"'"}'
FAIL=0

pass() { echo "[PASS] $1"; }
fail() { echo "[FAIL] $1"; FAIL=1; }

codex_verdict() {
  local trimmed
  trimmed="$(printf '%s' "$1" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
  if [ -z "$trimmed" ]; then
    printf '%s' "OK_EMPTY"
    return 0
  fi
  if python3 -c "import json,sys; json.loads(sys.stdin.read())" <<<"$trimmed" 2>/dev/null; then
    if python3 -c "import json,sys; v=json.loads(sys.stdin.read()); sys.exit(0 if isinstance(v,dict) else 1)" <<<"$trimmed" 2>/dev/null; then
      printf '%s' "OK_JSON"
      return 0
    fi
  fi
  case "$trimmed" in
    '{'*|'['*) printf '%s' "FAIL" ;;
    *) printf '%s' "OK_PLAINTEXT" ;;
  esac
}

run_hook() {
  local hook_bin="$1"
  shift
  env -i HOME="$HOME" USER="${USER:-$(id -un)}" PATH="/usr/bin:/bin:/usr/sbin:/sbin" \
    "$@" \
    "$hook_bin" router.sh <<<"$PAYLOAD"
}

# 1) Symlink-from-bindir (mimics install-bin.sh)
TMP="$(mktemp -d "${TMPDIR:-/tmp}/unifable-hook-dispatch.XXXXXX")"
BINDIR="$TMP/bin"
mkdir -p "$BINDIR"
ln -sf "$REPO_HOOK" "$BINDIR/unifable-hook"
chmod +x "$REPO_HOOK"

OUT="$(run_hook "$BINDIR/unifable-hook" 2>&1)" || RC=$?
RC="${RC:-0}"
if [ "$RC" -ne 0 ]; then
  fail "symlink bindir exit $RC (expected 0); stderr/stdout: $OUT"
else
  VERDICT="$(codex_verdict "$OUT")"
  if [ "$VERDICT" = "FAIL" ]; then
    fail "symlink bindir codex verdict FAIL; output: $OUT"
  else
    pass "symlink bindir exit 0 verdict=$VERDICT"
  fi
fi

# 2) Stale UNIFABLE_PLUGIN_ROOT must not break dispatch
STALE="$HOME/.codex/plugins/cache/unifable/unifable/1.9.59"
OUT2="$(run_hook "$BINDIR/unifable-hook" UNIFABLE_PLUGIN_ROOT="$STALE" 2>&1)" || RC2=$?
RC2="${RC2:-0}"
if [ "$RC2" -ne 0 ]; then
  fail "stale env exit $RC2 (expected 0); output: $OUT2"
else
  pass "stale UNIFABLE_PLUGIN_ROOT exit 0"
fi

# 3) Real ~/.local/bin symlink when present (refresh from repo first)
if [ -x "$ROOT/setup/install-bin.sh" ]; then
  bash "$ROOT/setup/install-bin.sh" "$ROOT" >/dev/null 2>&1 || true
fi
if [ -x "$HOME/.local/bin/unifable-hook" ]; then
  OUT3="$(run_hook "$HOME/.local/bin/unifable-hook" 2>&1)" || RC3=$?
  RC3="${RC3:-0}"
  if [ "$RC3" -ne 0 ]; then
    fail "~/.local/bin/unifable-hook exit $RC3; output: $OUT3"
  else
    VERDICT3="$(codex_verdict "$OUT3")"
    if [ "$VERDICT3" = "FAIL" ]; then
      fail "~/.local/bin codex verdict FAIL; output: $OUT3"
    else
      pass "~/.local/bin/unifable-hook exit 0 verdict=$VERDICT3"
    fi
  fi
else
  pass "~/.local/bin/unifable-hook not installed (skipped)"
fi

rm -rf "$TMP"

echo ""
if [ "$FAIL" -eq 0 ]; then
  echo "RESULT: all dispatch checks passed"
  exit 0
fi
echo "RESULT: dispatch checks failed"
exit 1
