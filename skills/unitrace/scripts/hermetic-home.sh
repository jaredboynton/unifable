#!/usr/bin/env bash
# Shared hermetic HOME helpers for explore trace runs.

explore_normalize_cursor_home() {
  local home="$1"
  while [ "$home" != "/" ] && [ "${home%/}" != "$home" ]; do
    home="${home%/}"
  done
  if [ "$(basename "$home")" = ".claude" ]; then
    local parent
    if parent="$(cd -P "$(dirname "$home")" 2>/dev/null && pwd)"; then
      printf '%s\n' "$parent"
    else
      dirname "$home"
    fi
  else
    printf '%s\n' "$home"
  fi
}

explore_real_home() {
  explore_normalize_cursor_home "${HOME}"
}

explore_apply_hermetic_default() {
  case "${UNITRACE_HERMETIC_HOME-}" in
    0) export UNITRACE_HERMETIC_HOME=0 ;;
    *) export UNITRACE_HERMETIC_HOME=1 ;;
  esac
}

explore_hermetic_home_dir() {
  printf '%s/hermetic-home\n' "$1"
}

explore_link_hermetic_keychains() {
  local real_home="$1"
  local hermetic_dir="$2"
  local keychains_target="$real_home/Library/Keychains"

  [ "$(uname -s)" = "Darwin" ] || return 0
  [ -d "$keychains_target" ] || return 0

  mkdir -p "$hermetic_dir/Library"
  ln -sfn "$keychains_target" "$hermetic_dir/Library/Keychains"
}

explore_ensure_hermetic_home() {
  local real_home="$1"
  local hermetic_dir="$2"
  local auth_target="$real_home/.cursor/auth.json"

  mkdir -p "$hermetic_dir/.cursor"

  if [ ! -f "$auth_target" ]; then
    printf 'explore: hermetic home requires %s; run: cursor-agent login\n' "$auth_target" >&2
    return 1
  fi

  ln -sf "$auth_target" "$hermetic_dir/.cursor/auth.json"

  if [ -f "$real_home/.cursor/agent-cli-state.json" ]; then
    ln -sf "$real_home/.cursor/agent-cli-state.json" "$hermetic_dir/.cursor/agent-cli-state.json"
  else
    rm -f "$hermetic_dir/.cursor/agent-cli-state.json"
  fi

  explore_link_hermetic_keychains "$real_home" "$hermetic_dir"

  printf '{"version":1,"hooks":{}}\n' > "$hermetic_dir/.cursor/hooks.json"

  printf '%s\n' "$hermetic_dir"
}

explore_resolve_cursor_home() {
  local base_dir="$1"
  local real_home="$2"

  if [ -n "${UNITRACE_CURSOR_HOME:-}" ]; then
    explore_normalize_cursor_home "$UNITRACE_CURSOR_HOME"
    return 0
  fi

  if [ "${UNITRACE_HERMETIC_HOME:-1}" != "0" ]; then
    explore_ensure_hermetic_home "$real_home" "$(explore_hermetic_home_dir "$base_dir")"
    return 0
  fi

  printf '%s\n' "$real_home"
}
