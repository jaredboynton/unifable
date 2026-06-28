#!/usr/bin/env bash
set -euo pipefail

TRACE_SH="${TRACE_UNDER_TEST:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/trace-cursor.sh}"
TRACE_CMD=(env -u CURSOR_CONVERSATION_ID -u CURSOR_AGENT)
WORKSPACE="${UNITRACE_TEST_WORKSPACE:-$(cd "$(dirname "$TRACE_SH")/../../.." && pwd)}"
RUN_ROOT="${TMPDIR:-/tmp}/explore-trace-inputs.$$"
FAKE_BIN="$RUN_ROOT/bin"
FAKE_HOME_LOG="$RUN_ROOT/cursor-home.log"

assert_logged_home() {
  local expected="$1"
  local logged resolved_expected
  logged="$(sed -n '1p' "$FAKE_HOME_LOG")"
  logged="$(cd -P "$logged" && pwd)"
  resolved_expected="$(cd -P "$expected" && pwd)"
  [ "$logged" = "$resolved_expected" ]
}

cleanup() {
  rm -rf "$RUN_ROOT"
}
trap cleanup EXIT

mkdir -p "$FAKE_BIN" "$RUN_ROOT/user/.claude" "$RUN_ROOT/user/.cursor"
printf '{"token":"fake-test-auth"}\n' > "$RUN_ROOT/user/.cursor/auth.json"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'set -euo pipefail' \
  'echo "$HOME" >> "$FAKE_CURSOR_HOME_LOG"' \
  'case " $* " in' \
  '  *" acp "*)' \
  '    echo "fake cursor-agent acp should not run in this test" >&2' \
  '    exit 70' \
  '    ;;' \
  'esac' \
  'echo "## Flow"' \
  'echo "Stub trace result."' \
  'echo' \
  'echo "## Code references"' \
  'echo' \
  'echo "## Key files"' \
  'echo "- stub"' \
  > "$FAKE_BIN/cursor-agent"
chmod +x "$FAKE_BIN/cursor-agent"

ROUTE_SCRIPT_DIR="$RUN_ROOT/route-scripts"
ROUTE_WORKSPACE="$RUN_ROOT/route-workspace"
ROUTE_OTHER_WORKSPACE="$RUN_ROOT/route-other-workspace"
ROUTE_ARGS_LOG="$RUN_ROOT/route-search.args"
TRACE_CURSOR_SH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/trace-cursor.sh"
mkdir -p "$ROUTE_SCRIPT_DIR" "$ROUTE_WORKSPACE" "$ROUTE_OTHER_WORKSPACE"
cp "$TRACE_CURSOR_SH" "$ROUTE_SCRIPT_DIR/trace-cursor.sh"
cp "$(dirname "$TRACE_CURSOR_SH")/hermetic-home.sh" "$ROUTE_SCRIPT_DIR/hermetic-home.sh"
cp "$(dirname "$TRACE_CURSOR_SH")/explore-hydrate.sh" "$ROUTE_SCRIPT_DIR/explore-hydrate.sh"
chmod +x "$ROUTE_SCRIPT_DIR/trace-cursor.sh"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'set -euo pipefail' \
  ': > "$ROUTE_SEARCH_ARGS_LOG"' \
  'for arg in "$@"; do' \
  '  printf "%s\n" "$arg" >> "$ROUTE_SEARCH_ARGS_LOG"' \
  'done' \
  > "$ROUTE_SCRIPT_DIR/search.sh"
chmod +x "$ROUTE_SCRIPT_DIR/search.sh"

(
  cd "$ROUTE_WORKSPACE"
  CURSOR_CONVERSATION_ID=route ROUTE_SEARCH_ARGS_LOG="$ROUTE_ARGS_LOG" \
    UNITRACE_WORKSPACE="$ROUTE_OTHER_WORKSPACE" \
    "$ROUTE_SCRIPT_DIR/trace-cursor.sh" "cursor route probe" \
    >"$RUN_ROOT/route.stdout" 2>"$RUN_ROOT/route.stderr"
)
grep -q 'routed to search.sh' "$RUN_ROOT/route.stderr"
expected_route_root="$(cd "$ROUTE_WORKSPACE" && pwd)"
[ "$(sed -n '1p' "$ROUTE_ARGS_LOG")" = "--root" ]
[ "$(sed -n '2p' "$ROUTE_ARGS_LOG")" = "$expected_route_root" ]
[ "$(sed -n '3p' "$ROUTE_ARGS_LOG")" = "cursor route probe" ]
[ -z "$(sed -n '4p' "$ROUTE_ARGS_LOG")" ]

if (
  cd "$ROUTE_WORKSPACE"
  CURSOR_CONVERSATION_ID=route ROUTE_SEARCH_ARGS_LOG="$RUN_ROOT/route-rejected.args" \
    "$ROUTE_SCRIPT_DIR/trace-cursor.sh" --root "$ROUTE_OTHER_WORKSPACE" "cursor route probe" \
    >"$RUN_ROOT/route-rejected.stdout" 2>"$RUN_ROOT/route-rejected.stderr"
); then
  echo "expected routed --root override to be rejected" >&2
  exit 1
fi
grep -q 'unknown flag --root' "$RUN_ROOT/route-rejected.stderr"
[ ! -e "$RUN_ROOT/route-rejected.args" ]

NO_ROUTE_SCRIPT_DIR="$RUN_ROOT/no-route-scripts"
mkdir -p "$NO_ROUTE_SCRIPT_DIR"
for script in trace.sh trace-gemini.sh trace-rt.sh; do
  cp "$(dirname "${BASH_SOURCE[0]}")/$script" "$NO_ROUTE_SCRIPT_DIR/$script"
  chmod +x "$NO_ROUTE_SCRIPT_DIR/$script"
done
for script in trace.sh trace-gemini.sh trace-rt.sh; do
  if (
    cd "$ROUTE_WORKSPACE"
    CURSOR_CONVERSATION_ID=route \
      "$NO_ROUTE_SCRIPT_DIR/$script" "no route probe" \
      >"$RUN_ROOT/no-route-$script.stdout" 2>"$RUN_ROOT/no-route-$script.stderr"
  ); then
    :
  fi
  if grep -q 'routed to search.sh' "$RUN_ROOT/no-route-$script.stderr" 2>/dev/null; then
    echo "expected $script not to route to search.sh when CURSOR_CONVERSATION_ID is set" >&2
    exit 1
  fi
done

if PATH="$FAKE_BIN:$PATH" HOME="$RUN_ROOT/user/.claude/" FAKE_CURSOR_HOME_LOG="$FAKE_HOME_LOG" \
  UNITRACE_TRANSPORT=cli UNITRACE_FORMAT=raw UNITRACE_WORKSPACE="$WORKSPACE" \
  UNITRACE_OUT="$RUN_ROOT/invalid/out.md" UNITRACE_RUN_ID='../escape' \
  "${TRACE_CMD[@]}" "$TRACE_SH" "invalid run id probe" >"$RUN_ROOT/invalid.stdout" 2>"$RUN_ROOT/invalid.stderr"; then
  echo "expected invalid run id to fail" >&2
  exit 1
fi
grep -q 'invalid run id' "$RUN_ROOT/invalid.stderr"
[ ! -e "$RUN_ROOT/escape" ]

if PATH="$FAKE_BIN:$PATH" HOME="$RUN_ROOT/user/.claude/" FAKE_CURSOR_HOME_LOG="$FAKE_HOME_LOG" \
  UNITRACE_TRANSPORT=cli UNITRACE_FORMAT=raw UNITRACE_WORKSPACE="$WORKSPACE" \
  UNITRACE_OUT='relative-last.md' UNITRACE_RUN_ID='relative-path-probe' \
  "${TRACE_CMD[@]}" "$TRACE_SH" "relative path probe" >"$RUN_ROOT/relative.stdout" 2>"$RUN_ROOT/relative.stderr"; then
  echo "expected relative UNITRACE_OUT to fail" >&2
  exit 1
fi
grep -q 'UNITRACE_OUT must be an absolute path' "$RUN_ROOT/relative.stderr"

if PATH="$FAKE_BIN:$PATH" HOME="$RUN_ROOT/user/.claude/" FAKE_CURSOR_HOME_LOG="$FAKE_HOME_LOG" \
  UNITRACE_OUT="$RUN_ROOT/await/out.md" "${TRACE_CMD[@]}" "$TRACE_SH" --await \
  >"$RUN_ROOT/await.stdout" 2>"$RUN_ROOT/await.stderr"; then
  echo "expected --await to be rejected" >&2
  exit 1
fi
grep -q 'unknown flag --await' "$RUN_ROOT/await.stderr"

if PATH="$FAKE_BIN:$PATH" HOME="$RUN_ROOT/user/.claude/" FAKE_CURSOR_HOME_LOG="$FAKE_HOME_LOG" \
  UNITRACE_OUT="$RUN_ROOT/wait/out.md" "${TRACE_CMD[@]}" "$TRACE_SH" --wait \
  >"$RUN_ROOT/wait.stdout" 2>"$RUN_ROOT/wait.stderr"; then
  echo "expected --wait to be rejected" >&2
  exit 1
fi
grep -q 'unknown flag --wait' "$RUN_ROOT/wait.stderr"

mkdir -p "$RUN_ROOT/shared-runs/unrelated"
PATH="$FAKE_BIN:$PATH" HOME="$RUN_ROOT/user/.claude/" FAKE_CURSOR_HOME_LOG="$FAKE_HOME_LOG" \
  UNITRACE_TRANSPORT=cli UNITRACE_FORMAT=raw UNITRACE_WORKSPACE="$WORKSPACE" \
  UNITRACE_OUT="$RUN_ROOT/cleanup/out.md" UNITRACE_RUNS_DIR="$RUN_ROOT/shared-runs" \
  UNITRACE_RUN_TTL_SECONDS=0 UNITRACE_RUN_ID='cleanup-guard-probe' \
  "${TRACE_CMD[@]}" "$TRACE_SH" "cleanup guard probe" >"$RUN_ROOT/cleanup.stdout" 2>"$RUN_ROOT/cleanup.stderr"
[ -d "$RUN_ROOT/shared-runs/unrelated" ]

PATH="$FAKE_BIN:$PATH" HOME="$RUN_ROOT/user/.claude/" FAKE_CURSOR_HOME_LOG="$FAKE_HOME_LOG" \
  UNITRACE_TRANSPORT=cli UNITRACE_FORMAT=raw UNITRACE_WORKSPACE="$WORKSPACE" \
  UNITRACE_OUT="$RUN_ROOT/no-latest/out.md" UNITRACE_LATEST_FILE="$RUN_ROOT/missing/latest/.latest-run" \
  UNITRACE_RUN_ID='no-latest-probe' \
  "${TRACE_CMD[@]}" "$TRACE_SH" "no latest pointer probe" >"$RUN_ROOT/no-latest.stdout" 2>"$RUN_ROOT/no-latest.stderr"
[ ! -e "$RUN_ROOT/missing/latest/.latest-run" ]
[ ! -e "$RUN_ROOT/no-latest/.latest-run" ]

: > "$FAKE_HOME_LOG"
PATH="$FAKE_BIN:$PATH" HOME="$RUN_ROOT/user/.claude/" FAKE_CURSOR_HOME_LOG="$FAKE_HOME_LOG" \
  UNITRACE_HERMETIC_HOME=0 UNITRACE_TRANSPORT=cli UNITRACE_FORMAT=raw UNITRACE_WORKSPACE="$WORKSPACE" \
  UNITRACE_OUT="$RUN_ROOT/home/out.md" UNITRACE_RUN_ID='home-normalize-probe' \
  "${TRACE_CMD[@]}" "$TRACE_SH" "home normalize probe" >"$RUN_ROOT/home.stdout" 2>"$RUN_ROOT/home.stderr"

grep -q '^UNITRACE_RUN_ID=home-normalize-probe$' "$RUN_ROOT/home.stdout"
if grep -q "$RUN_ROOT/user/.claude" "$FAKE_HOME_LOG"; then
  echo "cursor-agent inherited Claude config HOME" >&2
  cat "$FAKE_HOME_LOG" >&2
  exit 1
fi
expected_home="$(cd -P "$RUN_ROOT/user" && pwd)"
assert_logged_home "$expected_home"

: > "$FAKE_HOME_LOG"
PATH="$FAKE_BIN:$PATH" HOME="$RUN_ROOT/user/.claude/" FAKE_CURSOR_HOME_LOG="$FAKE_HOME_LOG" \
  UNITRACE_TRANSPORT=cli UNITRACE_FORMAT=raw UNITRACE_WORKSPACE="$WORKSPACE" \
  UNITRACE_OUT="$RUN_ROOT/hermetic-default/out.md" UNITRACE_RUN_ID='hermetic-default-probe' \
  "${TRACE_CMD[@]}" "$TRACE_SH" "hermetic default probe" >"$RUN_ROOT/hermetic-default.stdout" 2>"$RUN_ROOT/hermetic-default.stderr"
assert_logged_home "$RUN_ROOT/hermetic-default/hermetic-home"
if grep -q "$RUN_ROOT/user/.claude" "$FAKE_HOME_LOG"; then
  echo "default hermetic mode leaked real Claude HOME" >&2
  cat "$FAKE_HOME_LOG" >&2
  exit 1
fi

: > "$FAKE_HOME_LOG"
mkdir -p "$RUN_ROOT/custom-home"
PATH="$FAKE_BIN:$PATH" HOME="$RUN_ROOT/user/.claude/" FAKE_CURSOR_HOME_LOG="$FAKE_HOME_LOG" \
  UNITRACE_CURSOR_HOME="$RUN_ROOT/custom-home" UNITRACE_TRANSPORT=cli UNITRACE_FORMAT=raw \
  UNITRACE_WORKSPACE="$WORKSPACE" UNITRACE_OUT="$RUN_ROOT/hermetic-override/out.md" \
  UNITRACE_RUN_ID='hermetic-override-probe' \
  "${TRACE_CMD[@]}" "$TRACE_SH" "hermetic override probe" >"$RUN_ROOT/hermetic-override.stdout" 2>"$RUN_ROOT/hermetic-override.stderr"
assert_logged_home "$RUN_ROOT/custom-home"

echo "trace input regression passed"
