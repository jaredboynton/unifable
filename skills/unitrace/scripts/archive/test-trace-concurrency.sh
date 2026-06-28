#!/usr/bin/env bash
set -euo pipefail

TRACE_SH="${TRACE_UNDER_TEST:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/trace-cursor.sh}"
TRACE_CMD=(env -u CURSOR_CONVERSATION_ID -u CURSOR_AGENT)
WORKSPACE="${UNITRACE_TEST_WORKSPACE:-$(cd "$(dirname "$TRACE_SH")/../../.." && pwd)}"
RUN_ROOT="${TMPDIR:-/tmp}/explore-trace-concurrency.$$"
OUT_FILE="$RUN_ROOT/shared-trace.md"
FAKE_BIN="$RUN_ROOT/bin"
FAKE_HOME_LOG="$RUN_ROOT/cursor-home.log"

cleanup() {
  rm -rf "$RUN_ROOT"
}
trap cleanup EXIT

mkdir -p "$RUN_ROOT" "$FAKE_BIN" "$RUN_ROOT/.claude" "$RUN_ROOT/.cursor"
printf '{"token":"fake-test-auth"}\n' > "$RUN_ROOT/.cursor/auth.json"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'set -euo pipefail' \
  'echo "$HOME" >> "$FAKE_CURSOR_HOME_LOG"' \
  'case " $* " in' \
  '  *" acp "*)' \
  '    echo '\''fake cursor-agent acp should not run in this test'\'' >&2' \
  '    exit 70' \
  '    ;;' \
  'esac' \
  'sleep 0.2' \
  'echo '\''## Flow'\''' \
  'echo '\''Stub trace result.'\''' \
  'echo' \
  'echo '\''## Code references'\''' \
  'echo' \
  'echo '\''## Key files'\''' \
  'echo '\''- stub'\''' \
  > "$FAKE_BIN/cursor-agent"
chmod +x "$FAKE_BIN/cursor-agent"

PATH="$FAKE_BIN:$PATH" HOME="$RUN_ROOT/.claude/" FAKE_CURSOR_HOME_LOG="$FAKE_HOME_LOG" \
  UNITRACE_TRANSPORT=cli UNITRACE_FORMAT=raw UNITRACE_WORKSPACE="$WORKSPACE" UNITRACE_OUT="$OUT_FILE" \
  "${TRACE_CMD[@]}" "$TRACE_SH" "Answer tersely: concurrency probe one." \
  >"$RUN_ROOT/one.stdout" 2>"$RUN_ROOT/one.stderr" &
pid_one=$!

deadline=$(( $(date +%s) + 20 ))
while ! compgen -G "$RUN_ROOT/runs/*/running" >/dev/null; do
  if ! kill -0 "$pid_one" 2>/dev/null; then
    wait "$pid_one" || true
    echo "first trace exited before publishing run state" >&2
    sed -n '1,80p' "$RUN_ROOT/one.stderr" >&2 || true
    exit 1
  fi
  if [ "$(date +%s)" -ge "$deadline" ]; then
    echo "timed out waiting for first trace run state" >&2
    kill "$pid_one" 2>/dev/null || true
    wait "$pid_one" 2>/dev/null || true
    exit 1
  fi
  sleep 0.1
done

PATH="$FAKE_BIN:$PATH" HOME="$RUN_ROOT/.claude/" FAKE_CURSOR_HOME_LOG="$FAKE_HOME_LOG" \
  UNITRACE_TRANSPORT=cli UNITRACE_FORMAT=raw UNITRACE_WORKSPACE="$WORKSPACE" UNITRACE_OUT="$OUT_FILE" \
  "${TRACE_CMD[@]}" "$TRACE_SH" "Answer tersely: concurrency probe two." \
  >"$RUN_ROOT/two.stdout" 2>"$RUN_ROOT/two.stderr" &
pid_two=$!

status_one=0
status_two=0
wait "$pid_one" || status_one=$?
wait "$pid_two" || status_two=$?

if [ "$status_one" -ne 0 ] || [ "$status_two" -ne 0 ]; then
  echo "expected both concurrent traces to succeed, got one=$status_one two=$status_two" >&2
  echo "--- one.stderr ---" >&2
  sed -n '1,120p' "$RUN_ROOT/one.stderr" >&2 || true
  echo "--- two.stderr ---" >&2
  sed -n '1,120p' "$RUN_ROOT/two.stderr" >&2 || true
  exit 1
fi

if grep -R "No such file or directory\|no trace output captured" "$RUN_ROOT" >/dev/null 2>&1; then
  echo "trace output contained a concurrency failure marker" >&2
  grep -R "No such file or directory\|no trace output captured" "$RUN_ROOT" >&2 || true
  exit 1
fi

grep -q '\[explore: full trace saved to ' "$RUN_ROOT/one.stdout"
grep -q '\[explore: full trace saved to ' "$RUN_ROOT/two.stdout"
grep -q '^## Flow' "$RUN_ROOT/one.stdout"
grep -q '^## Flow' "$RUN_ROOT/two.stdout"
grep -q '^UNITRACE_RUN_ID=' "$RUN_ROOT/one.stdout"
grep -q '^UNITRACE_RUN_ID=' "$RUN_ROOT/two.stdout"
if grep -q '^UNITRACE_RUN_ID=' "$RUN_ROOT/one.stderr"; then
  echo "trace surfaced run id before the completed result" >&2
  exit 1
fi
if grep -q '^UNITRACE_RUN_ID=' "$RUN_ROOT/two.stderr"; then
  echo "trace surfaced run id before the completed result" >&2
  exit 1
fi
if sed -n '1p' "$RUN_ROOT/one.stdout" | grep -q '^UNITRACE_RUN_ID='; then
  echo "successful trace stdout began with only the run id instead of trace content" >&2
  exit 1
fi
if sed -n '1p' "$RUN_ROOT/two.stdout" | grep -q '^UNITRACE_RUN_ID='; then
  echo "successful trace stdout began with only the run id instead of trace content" >&2
  exit 1
fi
if grep -q "$RUN_ROOT/.claude" "$FAKE_HOME_LOG"; then
  echo "cursor-agent inherited Claude config HOME instead of the real user home" >&2
  cat "$FAKE_HOME_LOG" >&2
  exit 1
fi
[ ! -e "$RUN_ROOT/.latest-run" ]
normalize_path() {
  cd -P "$1" 2>/dev/null && pwd || printf '%s' "$1"
}
expected_home="$(normalize_path "$RUN_ROOT/hermetic-home")"
logged_homes="$(sort -u "$FAKE_HOME_LOG" | while IFS= read -r line; do normalize_path "$line"; done | sort -u | tr -d '\n')"
if [ "$logged_homes" != "$expected_home" ]; then
  echo "unexpected cursor-agent HOME values" >&2
  cat "$FAKE_HOME_LOG" >&2
  exit 1
fi

echo "trace concurrency regression passed"
