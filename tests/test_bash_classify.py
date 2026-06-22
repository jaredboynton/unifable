#!/usr/bin/env python3
"""Unit tests for the Bash research whitelist (scripts/gate/bash_classify.py)."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts", "gate"))
from bash_classify import is_allowed_research_bash  # noqa: E402


ALLOWED = [
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
    "ls && rg foo",
    "ls | rg foo",
    "unifable restate 'do the thing well'",
    "unifable add-task --title t --check true",
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
]

BLOCKED = [
    "",
    "echo hi",
    "cat file.py",
    "head -20 f",
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
    "git diff --stat",
    "curl https://example.com",
    "rm -rf build",
    "echo hi > /dev/null",
    "rg foo | cat",
    "ls && cat README.md",
    "bash other.sh",
    "python trace.sh",
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
