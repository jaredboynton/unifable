#!/usr/bin/env bash
# _unifusion_lib.sh — shared helpers for the Unifusion panelist runners.
#
# Sourced (not executed) by run_codex.sh and run_gemini.sh:
#   SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
#   . "$SCRIPT_DIR/_unifusion_lib.sh"
#
# Why this exists: macOS has no `timeout`/`gtimeout` (those ship with GNU coreutils,
# not installed here). _run_with_timeout reproduces GNU `timeout` semantics with a
# small self-contained perl fork+alarm wrapper: it sends SIGTERM on the deadline,
# then SIGKILL after a 2s grace, returns the command's real exit status, and returns
# 124 when the command was killed for running over time.

# Default per-panelist budget in seconds; override with UNIFUSION_TIMEOUT.
UNIFUSION_TIMEOUT="${UNIFUSION_TIMEOUT:-300}"

# Exa MCP endpoint injected into clean-room panelist configs (cb/codex/devin throwaways).
# Override with UNIFUSION_EXA_MCP_URL when rotating keys.
UNIFUSION_EXA_MCP_URL="${UNIFUSION_EXA_MCP_URL:-https://mcp.exa.ai/mcp?exaApiKey=93b180fe-b949-451c-afd0-47c6bcca335f}"

have() { command -v "$1" >/dev/null 2>&1; }

# _unifusion_write_claude_exa_mcp <json_path> — Claude `--mcp-config` file (exa only).
_unifusion_write_claude_exa_mcp() {
  local path="${1:?path required}"
  mkdir -p "$(dirname "$path")"
  printf '%s\n' "{\"mcpServers\":{\"exa\":{\"type\":\"http\",\"url\":\"${UNIFUSION_EXA_MCP_URL}\"}}}" > "$path"
}

# _kimi_bin — print the path to the real kimi binary, never the shell alias (often `kimi --yolo`,
# which conflicts with print mode: "Cannot combine --prompt with --yolo").
# Precedence: UNIFUSION_KIMI_BIN > ~/.kimi-code/bin/kimi > command -P kimi (bash, ignores aliases).
_kimi_bin() {
  if [ -n "${UNIFUSION_KIMI_BIN:-}" ] && [ -x "${UNIFUSION_KIMI_BIN:-}" ]; then
    printf '%s\n' "$UNIFUSION_KIMI_BIN"
    return 0
  fi
  if [ -x "${HOME}/.kimi-code/bin/kimi" ]; then
    printf '%s\n' "${HOME}/.kimi-code/bin/kimi"
    return 0
  fi
  if command -P kimi >/dev/null 2>&1; then
    command -P kimi
    return 0
  fi
  return 1
}
have_kimi() { _kimi_bin >/dev/null 2>&1; }

# _run_with_timeout SECONDS cmd [args...]
# Exit status = the command's own status, or 124 if it was killed for timing out.
# Child runs in its own process group so timeout/signals reap the whole subtree.
_run_with_timeout() {
  local secs="$1"; shift
  perl -e '
    use POSIX ();
    my $secs = shift @ARGV;
    my $pid = fork();
    exit 127 unless defined $pid;
    if ($pid == 0) { POSIX::setpgid(0, 0); exec @ARGV or exit 127; }  # child: own pgroup, become the command
    POSIX::setpgid($pid, $pid);                                       # race-proof: set it from the parent too
    my $reap = sub {
      kill("TERM", -$pid); kill("TERM", $pid);   # negative pid => the whole process group
      sleep 2;
      kill("KILL", -$pid); kill("KILL", $pid);
    };
    local $SIG{ALRM} = $reap;                          # deadline => terminate the child group
    local $SIG{TERM} = sub { $reap->(); exit 143; };   # wrapper killed => take the group with us
    local $SIG{INT}  = sub { $reap->(); exit 130; };
    alarm $secs;
    waitpid($pid, 0);
    my $rc = $?;
    alarm 0;
    exit 124 if ($rc & 127);   # killed by a signal (our TERM/KILL) => timed out
    exit($rc >> 8);            # otherwise propagate the command exit code
  ' "$secs" "$@"
}
