#!/usr/bin/env bash
# Offline smoke test for trace-gemini.sh (Gemini CLI) with a fake gemini binary.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/explore-trace-gm-test.XXXXXX")"
FAKE_BIN="$WORK_DIR/bin"
RUNS_DIR="$WORK_DIR/runs"

mkdir -p "$FAKE_BIN" "$RUNS_DIR"

cat > "$FAKE_BIN/gemini" <<'SH'
#!/bin/sh
# Fake gemini: emit json response with trace markdown.
printf '%s\n' '{"response":"## Flow\nFake trace for offline test.\n\n## Code references\n\n```1:5:scripts/trace-gemini.sh\n#!/usr/bin/env bash\n# explore/trace-gemini.sh\n"}'
SH
chmod +x "$FAKE_BIN/gemini"

export PATH="$FAKE_BIN:$PATH"
export EXPLORE_GM_BIN="$FAKE_BIN/gemini"
export EXPLORE_MAP_MODE=none
export EXPLORE_WORKSPACE="$ROOT"
export EXPLORE_RUNS_DIR="$RUNS_DIR"
export EXPLORE_RUN_ID=trace-gm-offline-test

set +e
OUT="$(
  env -u CURSOR_CONVERSATION_ID "$SCRIPT_DIR/trace-gemini.sh" "How does trace.sh work?" 2>"$WORK_DIR/err"
)"
status=$?
set -e

if [ "$status" -ne 0 ]; then
  printf 'trace-gm offline test failed (exit %s)\n' "$status" >&2
  cat "$WORK_DIR/err" >&2 || true
  exit 1
fi

if ! printf '%s' "$OUT" | grep -q 'EXPLORE_RUN_ID=trace-gm-offline-test'; then
  printf 'trace-gm offline test: missing run id footer\n' >&2
  exit 1
fi

if ! printf '%s' "$OUT" | grep -q 'Fake trace for offline test'; then
  printf 'trace-gm offline test: missing trace body\n' >&2
  exit 1
fi

if [ ! -s "$RUNS_DIR/trace-gm-offline-test/out.md" ]; then
  printf 'trace-gm offline test: out.md missing\n' >&2
  exit 1
fi

node --test "$SCRIPT_DIR/test/test-gemini-trace.mjs"

printf 'test-trace-gm.sh: ok\n'
