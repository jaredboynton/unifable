#!/usr/bin/env python3
"""Unit tests for the Bash research whitelist (scripts/gate/bash_classify.py)."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts", "gate"))
from bash_classify import explore_script_in_command, is_allowed_research_bash  # noqa: E402

ALLOWED = [
    "cd subdir",
    "cd /abs/path",
    "cd .. && rg foo",
    "cd src; ls",
    "ls",
    "ls -la",
    "/bin/ls -la src",
    "glob '**/*.py'",
    "rg foo src/",
    "rg --files",
    "./trace.sh",
    "tools/trace.sh --brief auth",
    "/tmp/trace.sh",
    "bash trace.sh",
    "bash ./tools/trace.sh --json",
    "sh trace.sh",
    "zsh /tmp/trace.sh",
    "FOO=bar ./trace.sh",
    "env FOO=bar ./trace.sh",
    "./websearch.sh",
    "tools/websearch.sh 'task goal'",
    "/tmp/websearch.sh",
    "bash websearch.sh",
    "bash ./tools/websearch.sh 'task goal'",
    "sh websearch.sh",
    "zsh /tmp/websearch.sh",
    "FOO=bar ./websearch.sh",
    "env FOO=bar ./websearch.sh",
    "ls && rg foo",
    "ls | rg foo",
    "unifable restate 'do the thing well'",
    "unifable add-task --title t --check true",
    "unifable set-primary --title p --check true",
    "unifable add-frontier --title f --check true",
    "unifable-spec restate 'do the thing well'",
    "python3 scripts/gate/spec.py restate 'do the thing well'",
    "unifable dispute --task T1 --evidence proof",
    "unifable-spec add-task --title t --check true",
    "unifable-spec dispute --task T1 --evidence proof",
    "python3 scripts/gate/spec.py add-task --title t --check true",
    "python3 scripts/gate/spec.py dispute --task T1 --evidence proof",
    "rg foo | head",
    "rg foo | head -20",
    "ls && rg foo | head",
    "head -20 f",
    "wc -l setup/setup.sh",
    "tail -5 README.md",
    "sort -u paths.txt",
    "uniq counts.txt",
    # Standalone variable-declaration segments: a long path assigned once and
    # reused is a legit research pattern (the codex-thread bug). The assignment
    # segment carries no executable, but the OTHER segments do.
    "T=value; rg --files",
    "T=/Users/me/some/long/path; rg -n '.' \"$T/run_data.sh\"",
    "A=1 B=2; rg foo",
    "export T=value; rg --files",
    'DIR=/tmp/x; ls -la "$DIR"',
    "T=value rg --files",
    # Plain variable expansion ($VAR) is not command substitution.
    'rg "$HOME" .',
    "bash ~/.claude/skills/unifusion/scripts/unifusion.sh /tmp/q.txt",
    "bash skills/unifusion/scripts/save_run.sh slug /tmp/q.md /tmp/a.md /tmp/f.md /tmp/run",
    "./summarize_session.sh /tmp/ctx.md",
    "zsh /path/to/unifusion/scripts/resolve_session.sh --path",
    "bash ./tools/unifusion.sh /tmp/q.txt",
    "git status --short",
    "git diff --stat",
    "git log -1 --oneline",
    "git rev-parse --show-toplevel",
    "git -C /repo status",
    "cd subdir && git diff",
    "git add .",
    "git commit -m 'wip'",
    "git push origin main",
    "rg UNIFABLE_DEV scripts/gate",
]

BLOCKED = [
    "",
    "echo hi",
    "cat file.py",
    "grep -r foo .",
    "find . -name '*.py'",
    "unifable validate --grade STANDARD",
    "unifable-spec validate --grade STANDARD",
    "unifable validate-task --task T1",
    "unifable restate --goal g",
    "unifable cite --repo-context a.py:1::why",
    "unifable where",
    "unifable add-task --force",
    "python3 scripts/gate/spec.py validate --grade STANDARD",
    "python3 scripts/gate/spec.py create --goal g",
    "python3 scripts/gate/spec.py init",
    "python3 evil.py",
    "python3 scripts/gate/other.py status",
    "python3 /tmp/spec.py add-task --title t --check true",
    "unifable add-task --title t --check true && cat /etc/passwd",
    "pytest tests/ -q",
    "npm test",
    "git push --force",
    "git checkout main",
    "curl https://example.com",
    "rm -rf build",
    "echo hi > /dev/null",
    "rg foo | cat",
    "ls && cat README.md",
    "cd subdir && cat file",
    "bash other.sh",
    "bash skills/unifusion/scripts/run_codex.sh /tmp/p /tmp/o",
    "python trace.sh",
    "python websearch.sh",
    # Command/process substitution executes arbitrary commands -> must stay blocked,
    # now with an explicit, clear reason (previously blocked only by parser fallout).
    "T=$(printf x); rg --files",
    'T="$(printf x)"; rg --files',
    "T=`printf x`; rg --files",
    "rg $(cat /etc/passwd) .",
    'ls "$(whoami)"',
    "T=<(cat x); rg foo",
    # Dangerous declarations can alter command resolution.
    "PATH=/tmp; rg --files",
    "IFS=x; rg foo",
    # Operator diagnostics env is not agent-adjustable, even around allowed commands.
    "UNIFABLE_DEV=1 rg --files",
    "env UNIFABLE_DEV=1 rg --files",
    "export UNIFABLE_DEV=1; rg --files",
    "unset UNIFABLE_DEV; rg --files",
    # A bare declaration keyword with no command is not useful research.
    "export",
]


@pytest.mark.parametrize("cmd", ALLOWED)
def test_whitelisted_commands_allow(cmd):
    allowed, reason = is_allowed_research_bash(cmd)
    assert allowed, f"expected ALLOW but blocked ({reason}): {cmd!r}"
    assert reason == ""


@pytest.mark.parametrize("cmd", BLOCKED)
def test_non_whitelisted_commands_block(cmd):
    allowed, reason = is_allowed_research_bash(cmd)
    assert not allowed, f"expected BLOCK but allowed: {cmd!r}"
    assert reason


def test_non_string_is_blocked():
    assert is_allowed_research_bash(None) == (False, "empty command")
    assert is_allowed_research_bash(123) == (False, "empty command")


def test_explore_script_in_command_detects_websearch_and_trace():
    assert explore_script_in_command('bash ./websearch.sh "goal"') == "websearch.sh"
    assert explore_script_in_command("~/.agents/skills/explore/scripts/trace.sh q") == "trace.sh"
    assert explore_script_in_command("rg foo") is None
