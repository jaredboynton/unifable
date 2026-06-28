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

# 3) Real ~/.local/bin symlink when present (refresh via runtime_sync into a
#    sandboxed UNIFABLE_HOME/BIN_DIR so the test never mutates the user's real
#    ~/.local/bin; no --source -> scans the real cache roots, mirroring the
#    SessionStart hook path)
SYNC="$ROOT/scripts/gate/runtime_sync.py"
UFHOME="$TMP/.unifable"; UFBIN="$TMP/local-bin"
mkdir -p "$UFBIN"
if [ -f "$SYNC" ]; then
  UNIFABLE_HOME="$UFHOME" UNIFABLE_BIN_DIR="$UFBIN" \
    python3 "$SYNC" >/dev/null 2>&1 || true
fi
if [ -x "$UFBIN/unifable-hook" ]; then
  OUT3="$(run_hook "$UFBIN/unifable-hook" 2>&1)" || RC3=$?
  RC3="${RC3:-0}"
  if [ "$RC3" -ne 0 ]; then
    fail "sandboxed unifable-hook exit $RC3; output: $OUT3"
  else
    VERDICT3="$(codex_verdict "$OUT3")"
    if [ "$VERDICT3" = "FAIL" ]; then
      fail "sandboxed unifable-hook verdict FAIL; output: $OUT3"
    else
      pass "sandboxed unifable-hook exit 0 verdict=$VERDICT3"
    fi
  fi
else
  pass "sandboxed unifable-hook not seeded (no cache; skipped)"
fi

# 4) Runtime survives cache-version deletion (sandboxed; never touches real ~/.unifable)
TMP_RT="$(mktemp -d "${TMPDIR:-/tmp}/unifable-hook-rt-sync.XXXXXX")"
SANDBOX_HOME="$TMP_RT/unifable-home"
SANDBOX_BINDIR="$TMP_RT/local-bin"
SANDBOX_CACHE="$TMP_RT/cache/unifable/unifable"
mkdir -p "$SANDBOX_BINDIR" "$SANDBOX_CACHE"
RT_VERSION="$(python3 -c "import json; print(json.load(open('${ROOT}/.claude-plugin/plugin.json'))['version'])")"
RT_VDIR="$SANDBOX_CACHE/$RT_VERSION"
mkdir -p "$RT_VDIR"
for _rt_dir in hooks scripts bin setup packs; do
  cp -R "$ROOT/$_rt_dir" "$RT_VDIR/$_rt_dir"
done
if UNIFABLE_HOME="$SANDBOX_HOME" UNIFABLE_BIN_DIR="$SANDBOX_BINDIR" \
  UNIFABLE_CACHE_ROOTS="$SANDBOX_CACHE" HOME="$TMP_RT" \
  python3 "$ROOT/scripts/gate/runtime_sync.py" --source "$RT_VDIR" >/dev/null 2>&1 \
  && [ -x "$SANDBOX_BINDIR/unifable-hook" ]; then
  # Marketplace upgrade deletes the old cache version dir.
  rm -rf "$RT_VDIR"
  OUT4="$(env -i HOME="$TMP_RT" USER="${USER:-$(id -un)}" PATH="/usr/bin:/bin:/usr/sbin:/sbin" \
    UNIFABLE_HOME="$SANDBOX_HOME" \
    "$SANDBOX_BINDIR/unifable-hook" router.sh <<<"$PAYLOAD" 2>&1)" || RC4=$?
  RC4="${RC4:-0}"
  if [ "$RC4" -ne 0 ]; then
    fail "cache deletion exit $RC4 (expected 0; the exit-127 bug); output: $OUT4"
  else
    VERDICT4="$(codex_verdict "$OUT4")"
    if [ "$VERDICT4" = "FAIL" ]; then
      fail "cache deletion codex verdict FAIL; output: $OUT4"
    else
      pass "runtime survives cache-version deletion verdict=$VERDICT4"
    fi
  fi
else
  fail "runtime sync seed failed for cache-deletion case"
fi
rm -rf "$TMP_RT"

rm -rf "$TMP"

echo ""
if [ "$FAIL" -eq 0 ]; then
  echo "RESULT: all dispatch checks passed"
  exit 0
fi
echo "RESULT: dispatch checks failed"
exit 1
