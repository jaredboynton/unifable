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
    "./unitrace.sh",
    "tools/unitrace.sh --brief auth",
    "/tmp/unitrace.sh",
    "bash unitrace.sh",
    "bash ./tools/unitrace.sh --json",
    "sh unitrace.sh",
    "zsh /tmp/unitrace.sh",
    "FOO=bar ./unitrace.sh",
    "env FOO=bar ./unitrace.sh",
    "./unisearch.sh",
    "tools/unisearch.sh 'task goal'",
    "/tmp/unisearch.sh",
    "bash unisearch.sh",
    "bash ./tools/unisearch.sh 'task goal'",
    "sh unisearch.sh",
    "zsh /tmp/unisearch.sh",
    "FOO=bar ./unisearch.sh",
    "env FOO=bar ./unisearch.sh",
    "ls && rg foo",
    "ls | rg foo",
    "unifable restate 'do the thing well'",
    "unifable add-task --title t --check true",
    "unifable set-primary --title p --check true",
    "unifable add-frontier --title f --check true",
    "unifable-spec restate 'do the thing well'",
    "python3 scripts/gate/spec.py restate 'do the thing well'",
    # Read-only python -c inspection: pure read/parse/print, no write/process/network.
    'python3 -c "import json; print(1)"',
    "python3 -c 'import json,sys; d=json.load(open(\"f.json\")); print(d[\"k\"])'",
    'python -c "print(open(\'x.txt\').read())"',
    'python3 -c "import sys; [print(l) for l in open(\'a.log\')]"',
    "unifable-spec add-task --title t --check true",
    "python3 scripts/gate/spec.py add-task --title t --check true",
    "rg foo | head",
    "rg foo | head -20",
    "ls && rg foo | head",
    "head -20 f",
    "wc -l scripts/bump_version.py",
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
    "git ls-remote origin",
    "git ls-files --stage",
    "git for-each-ref --format='%(refname)' refs/heads",
    "git rev-list -1 HEAD",
    "ast-grep scan -p 'foo($$$)' -l py",
    "sg run -p 'class $C' -l py src/",
    "echo hi",
    "echo hi | head",
    "echo hi > /dev/null",
    "grep -r foo .",
    "grep -E 'pattern' file.py",
    "rg UNIFABLE_DEV scripts/gate",
    # jq is a read-only JSON processor (no file-write primitive): standalone and as a sink.
    "jq '.' file.json",
    "jq -r '.key' data.json",
    "rg --files | jq -R .",
    "rg foo src/ | jq .",
    # Read-only shell loops/conditionals: allowed when every body command is whitelisted.
    "for f in specs/*.json; do head \"$f\"; done",
    "for f in a b c; do rg foo \"$f\"; done",
    "for f in specs/*.json; do jq '.' \"$f\"; done",
    "if rg -q foo file; then ls; fi",
    "while rg -q foo file; do echo hi; done",
]

BLOCKED = [
    "",
    "cat file.py",
    "find . -name '*.py'",
    "unifable restate --goal g",
    "unifable cite --repo-context a.py:1::why",
    "unifable where",
    "unifable add-task --force",
    "python3 scripts/gate/spec.py create --goal g",
    "python3 scripts/gate/spec.py init",
    "python3 evil.py",
    "python3 scripts/gate/other.py status",
    "python3 /tmp/spec.py add-task --title t --check true",
    # Non-spec.py SCRIPT FILES stay blocked: a file's contents cannot be proven
    # read-only from the command line, so only inline -c is eligible.
    "python3 read_only_looking.py",
    # python -c that can write / spawn / reach the network stays blocked.
    'python3 -c "import subprocess; subprocess.run([\'ls\'])"',
    'python3 -c "import os; os.system(\'rm -rf x\')"',
    'python3 -c "import socket; socket.socket()"',
    'python3 -c "import urllib.request as u; u.urlopen(\'http://x\')"',
    "python3 -c \"open('out.txt','w').write('x')\"",
    'python3 -c "from pathlib import Path; Path(\'o\').write_text(\'x\')"',
    'python3 -c "import shutil; shutil.rmtree(\'d\')"',
    'python3 -c "import os; os.remove(\'f\')"',
    "unifable add-task --title t --check true && cat /etc/passwd",
    "pytest tests/ -q",
    "npm test",
    "git push --force",
    "git reflog expire --expire=now --all",
    "ast-grep run -p 'x' -r 'y' -U",
    "sg run -p 'x' -r 'y' --update",
    "echo hi | tee out.txt",
    "echo hi | rg foo",
    "echo hi | sg run -p 'x' -l py",
    "git checkout main",
    "curl https://example.com",
    "rm -rf build",
    "rg foo | cat",
    "ls && cat README.md",
    "cd subdir && cat file",
    "bash other.sh",
    "bash skills/unifusion/scripts/run_codex.sh /tmp/p /tmp/o",
    "python unitrace.sh",
    "python unisearch.sh",
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
    # Loops must not become an escape hatch: a non-whitelisted body command blocks.
    "for f in *; do cat \"$f\"; done",
    "for f in *; do rm \"$f\"; done",
    "for f in $(ls); do head \"$f\"; done",
    "while read l; do echo \"$l\"; done",
    "if true; then cat /etc/passwd; fi",
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
    assert explore_script_in_command('bash ./unisearch.sh "goal"') == "unisearch.sh"
    assert explore_script_in_command("~/.agents/skills/unitrace/scripts/unitrace.sh q") == "unitrace.sh"
    assert explore_script_in_command("rg foo") is None


@pytest.mark.parametrize(
    ("cmd", "expected"),
    [
        (
            "unifable reject-frontier --title stale",
            "not an append-only subcommand",
        ),
        (
            "unifable-spec reject-frontier --title stale",
            "not an append-only subcommand",
        ),
        (
            "python3 scripts/gate/spec.py reject-frontier --title stale",
            "not an append-only subcommand",
        ),
        (
            "unifable add-task --force",
            "spec CLI --force is not allowed",
        ),
    ],
)
def test_spec_cli_only_allows_append_only_research_commands(cmd, expected):
    allowed, reason = is_allowed_research_bash(cmd)
    assert allowed is False
    assert expected in reason
