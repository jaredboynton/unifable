#!/usr/bin/env bash
# explore-hydrate.sh — shared post-run hydration for explore wire format.
# shellcheck shell=bash
explore_hydrate_trace_output() {
  local workspace="$1"
  local in_file="$2"
  local out_file="$3"
  local script_dir="$4"
  local raw_preserve="${5:-}"

  if [ "${EXPLORE_WIRE_FORMAT:-0}" = "1" ]; then
    if [ -n "$raw_preserve" ] && [ -f "$in_file" ]; then
      cp -f "$in_file" "$raw_preserve"
    fi
    if node "$script_dir/lib/rehydrate-explore-wire.mjs" --mode trace --workspace "$workspace" --file "$in_file" > "$out_file" 2>/dev/null \
       && [ -s "$out_file" ]; then
      return 0
    fi
    rm -f "$out_file"
    return 1
  fi

  if command -v python3 >/dev/null 2>&1; then
    if python3 "$script_dir/expand_citations.py" "$workspace" < "$in_file" > "$out_file" 2>/dev/null \
       && [ -s "$out_file" ]; then
      return 0
    fi
    rm -f "$out_file"
  fi
  cp -f "$in_file" "$out_file"
  return 0
}

explore_hydrate_websearch_output() {
  local in_file="$1"
  local out_file="$2"
  local script_dir="$3"
  local raw_preserve="${4:-}"

  if [ "${EXPLORE_WIRE_FORMAT:-0}" = "1" ]; then
    if [ -n "$raw_preserve" ] && [ -f "$in_file" ]; then
      cp -f "$in_file" "$raw_preserve"
    fi
    if node "$script_dir/lib/rehydrate-explore-wire.mjs" --mode websearch --file "$in_file" > "$out_file" 2>/dev/null \
       && [ -s "$out_file" ]; then
      return 0
    fi
    rm -f "$out_file"
    return 1
  fi

  cp -f "$in_file" "$out_file"
  return 0
}
