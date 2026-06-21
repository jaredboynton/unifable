#!/usr/bin/env python3
"""Unit tests for the Bash create/mutate classifier (scripts/gate/bash_classify.py).

Denylist contract: default ALLOW; only known create/mutate/install/network-mutating
shapes are flagged. Read, search, inspect, and test/validation runners stay allowed."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts", "gate"))
from bash_classify import is_mutating_bash  # noqa: E402


MUTATING = [
    "rm -rf build",
    "rmdir x",
    "mv a b",
    "cp a b",
    "mkdir -p x/y",
    "touch newfile",
    "dd if=a of=b",
    "ln -s a b",
    "truncate -s 0 f",
    "chmod +x f",
    "chown me f",
    "tee out.txt",
    "install -m 0755 a b",
    "patch < p.diff",
    "echo hi > out.txt",
    "echo hi >> out.txt",
    "cat a 1>out.txt",
    "sed -i s/a/b/ f.py",
    "sed -i.bak s/a/b/ f.py",
    "perl -i -pe s/a/b/ f.py",
    "git add .",
    "git commit -m wip",
    "git push origin main",
    "git reset --hard",
    "git checkout main",
    "git switch -c feat",
    "git restore f",
    "git stash",
    "git rebase main",
    "git init",
    "pip install requests",
    "pip3 uninstall y",
    "npm install",
    "npm i",
    "npm ci",
    "pnpm add z",
    "yarn add z",
    "brew install z",
    "apt-get install z",
    "cargo install x",
    "go install ./...",
    "docker run img",
    "docker build -t x .",
    "kubectl apply -f f.yaml",
    "terraform apply",
    "find . -delete",
    "find . -type f -exec rm {} ;",
    "curl -o out.txt https://x",
    "curl -X POST https://x -d body",
    "wget https://x -O f",
    "ls && rm x",
    "cat f; mv a b",
    "FOO=bar rm x",
    "sudo rm -rf /x",
    "/bin/rm x",
    "xargs rm < list",
    "true | tee log",
]

READONLY = [
    "",
    "echo hi",
    "ls -la",
    "cat file.py",
    "head -20 f",
    "tail -f log",
    "wc -l f",
    "grep -r foo .",
    "rg foo src/",
    "stat f",
    "file f",
    "tree",
    "git diff --stat",
    "git status",
    "git log --oneline -10",
    "git show HEAD",
    "git blame f",
    "git fetch origin",
    "pytest tests/ -q",
    "python3 -m pytest",
    "python3 scripts/gate/spec.py init --task-id x",
    "npm test",
    "npm run build",
    "cargo test",
    "cargo build",
    "cargo clippy",
    "go test ./...",
    "make test",
    "docker ps",
    "curl https://example.com",
    "echo hi > /dev/null",
    "grep foo f 2>&1",
    "diff a b",
    "jq . data.json",
    "find . -name '*.py'",
    "find . -exec grep foo {} ;",
    "true && echo ok",
]


@pytest.mark.parametrize("cmd", MUTATING)
def test_mutating_commands_block(cmd):
    mutating, reason = is_mutating_bash(cmd)
    assert mutating, f"expected MUTATING but allowed: {cmd!r}"
    assert reason, f"mutating command should carry a reason: {cmd!r}"


@pytest.mark.parametrize("cmd", READONLY)
def test_readonly_commands_allow(cmd):
    mutating, reason = is_mutating_bash(cmd)
    assert not mutating, f"expected ALLOW but blocked ({reason}): {cmd!r}"


def test_non_string_is_allowed():
    assert is_mutating_bash(None) == (False, "")
    assert is_mutating_bash(123) == (False, "")
