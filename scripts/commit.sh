#!/usr/bin/env bash
# CI-friendly release commit helper.
#
# Default behavior is for a clean CI checkout: bump the plugin version, run the
# documented gate checks, commit the bump, and optionally push with PUSH=1.
#
# Usage:
#   scripts/commit.sh [patch|minor|major|X.Y.Z]
#
# Environment:
#   VERSION=patch|minor|major|X.Y.Z   default version argument when $1 is absent
#   COMMIT_MESSAGE="..."              override commit message
#   PUSH=1                            push the resulting commit
#   REMOTE=origin                     push remote
#   BRANCH=<branch>                   push branch; defaults to current/GitHub ref
#   ALLOW_DIRTY=1                     include pre-existing tracked changes
#   INCLUDE_UNTRACKED=1               with ALLOW_DIRTY=1, include untracked files

# cleanup-traps: not-applicable -- this helper runs foreground commands only; it
# does not spawn background processes that need cleanup on signal.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

VERSION_ARG="${1:-${VERSION:-patch}}"
REMOTE="${REMOTE:-origin}"
BRANCH="${BRANCH:-${GITHUB_REF_NAME:-$(git branch --show-current)}}"
PUSH="${PUSH:-0}"
ALLOW_DIRTY="${ALLOW_DIRTY:-0}"
INCLUDE_UNTRACKED="${INCLUDE_UNTRACKED:-0}"

managed_paths=(
  ".claude-plugin/plugin.json"
  ".claude-plugin/marketplace.json"
  ".codex-plugin/plugin.json"
  ".codex-plugin/marketplace.json"
  ".devin-plugin/plugin.json"
  ".devin-plugin/marketplace.json"
  ".factory-plugin/plugin.json"
  ".factory-plugin/marketplace.json"
  "setup/setup.sh"
  "AGENTS.md"
  "docs/generated/claude-hookoutputs.md"
  "docs/generated/codex-hookoutputs.md"
  "docs/generated/judgeprompts.md"
)

current_version() {
  python3 - <<'PY'
import json
print(json.load(open(".claude-plugin/plugin.json"))["version"])
PY
}

last_subject="$(git log -1 --pretty=%s 2>/dev/null || true)"
if printf '%s\n' "$last_subject" | grep -Eq '\[skip release\]|\[skip ci\]'; then
  echo "commit.sh: latest commit is marked to skip release; exiting."
  exit 0
fi

if [ -n "$(git status --porcelain)" ] && [ "$ALLOW_DIRTY" != "1" ]; then
  cat >&2 <<'EOF'
commit.sh: working tree is dirty.
Set ALLOW_DIRTY=1 to include pre-existing tracked changes in the release commit.
Set INCLUDE_UNTRACKED=1 as well if untracked files should be included.
EOF
  git status --short >&2
  exit 2
fi

if [ -n "${GITHUB_ACTIONS:-}" ]; then
  git config user.name "${GIT_AUTHOR_NAME:-github-actions[bot]}"
  git config user.email "${GIT_AUTHOR_EMAIL:-41898282+github-actions[bot]@users.noreply.github.com}"
fi

old_version="$(current_version)"
python3 scripts/bump_version.py "$VERSION_ARG"
new_version="$(current_version)"

STAGE_GENERATED_DOCS=0 bash scripts/pre-commit-generated-docs.sh
python3 -m py_compile \
  hooks/pre_tool_use.py \
  hooks/gate_stop.py \
  scripts/generate_docs.py \
  scripts/gate/codex_judge.py \
  scripts/gate/groundedness.py \
  scripts/gate/ledger.py \
  scripts/gate/spec.py
python3 -m pytest tests/ -q --ignore=tests/test_gate_robustness.py
python3 tests/test_gate_robustness.py
python3 tests/eval_gate_proof.py

if [ "$ALLOW_DIRTY" = "1" ]; then
  git add -u
  if [ "$INCLUDE_UNTRACKED" = "1" ]; then
    git add -A
  fi
else
  for path in "${managed_paths[@]}"; do
    [ -e "$path" ] && git add "$path"
  done
fi

if git diff --cached --quiet; then
  echo "commit.sh: no staged changes after version check ($old_version -> $new_version)."
  exit 0
fi

message="${COMMIT_MESSAGE:-release: ${new_version} [skip release]}"
git commit -m "$message"

if [ "$PUSH" = "1" ]; then
  if [ -z "$BRANCH" ]; then
    echo "commit.sh: cannot push because branch is empty; set BRANCH=<name>." >&2
    exit 2
  fi
  git push "$REMOTE" "HEAD:$BRANCH"
else
  echo "commit.sh: commit created; set PUSH=1 to push."
fi
