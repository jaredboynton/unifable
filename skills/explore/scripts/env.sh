#!/usr/bin/env bash
# Shared environment loading for explore shell wrappers.

explore_trim_env_field() {
  local value="$1"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  printf '%s' "$value"
}

explore_load_skill_env() {
  local skill_dir="$1"
  local env_file="$skill_dir/.env"
  local line key value

  [ -f "$env_file" ] || return 0

  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%$'\r'}"
    line="$(explore_trim_env_field "$line")"
    case "$line" in
      ''|'#'*) continue ;;
      export\ *) line="${line#export }" ;;
    esac

    case "$line" in
      *=*) ;;
      *) continue ;;
    esac

    key="$(explore_trim_env_field "${line%%=*}")"
    value="$(explore_trim_env_field "${line#*=}")"

    case "$key" in
      ''|[!A-Za-z_]*|*[!A-Za-z0-9_]*) continue ;;
    esac

    if [ "${#value}" -ge 2 ]; then
      case "$value" in
        \"*\") value="${value:1:${#value}-2}" ;;
        \'*\') value="${value:1:${#value}-2}" ;;
      esac
    fi

    if [ -z "${!key:-}" ]; then
      export "$key=$value"
    fi
  done < "$env_file"
}
