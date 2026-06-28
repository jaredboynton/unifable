"""Spec CLI argument-shape ergonomics: positional fallback + actionable errors.

Locks in the fix for the wall where `unifable add-task '<title>'` (no --check, or
positional title) dead-ended on a bare argparse dump. The natural two-positional
form must work, and the single-arg attempt must print a copy-pasteable correct
command instead of a generic usage error.
"""

import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts", "gate"))

from spec_io import load_spec  # noqa: E402

_SPEC_PY = os.path.join(os.path.dirname(__file__), "..", "scripts", "gate", "spec.py")


def _git_init(path):
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)


def _run(argv, repo, sid, data_dir):
    env = dict(os.environ)
    env["CLAUDE_CODE_SESSION_ID"] = sid
    env["UNIFABLE_DATA"] = str(data_dir)
    return subprocess.run(
        [sys.executable, _SPEC_PY, *argv],
        cwd=str(repo),
        env=env,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _git_init(r)
    return r


@pytest.fixture
def data(tmp_path, monkeypatch):
    d = tmp_path / "data"
    d.mkdir()
    # The subprocess writes under UNIFABLE_DATA; load_spec runs in-process, so
    # the test process must resolve the same data root to read it back.
    monkeypatch.setenv("UNIFABLE_DATA", str(d))
    return d


def test_add_task_positional_creates_task(repo, data):
    res = _run(["add-task", "parser handles empty input", "true"], repo, "sess-pos", data)
    assert res.returncode == 0, res.stderr
    assert "Added T1" in res.stdout
    loaded = load_spec(str(repo), "sess-pos")
    assert loaded is not None
    assert loaded["tasks"][0]["title"] == "parser handles empty input"
    assert loaded["tasks"][0]["check"] == "true"


def test_add_task_flag_form_still_works(repo, data):
    res = _run(["add-task", "--title", "flag form", "--check", "true"], repo, "sess-flag", data)
    assert res.returncode == 0, res.stderr
    loaded = load_spec(str(repo), "sess-flag")
    assert loaded["tasks"][0]["title"] == "flag form"


def test_add_task_only_title_gives_actionable_error(repo, data):
    res = _run(["add-task", "only a title"], repo, "sess-bad", data)
    assert res.returncode == 2
    assert "unifable add-task --title" in res.stderr
    assert "--check" in res.stderr
    # No spec/task should have been created from the failed attempt.
    assert load_spec(str(repo), "sess-bad") is None


def test_restate_accepts_goal_flag_alias(repo, data):
    # restate uses a positional goal; --goal is accepted as an alias so the
    # common mistake succeeds instead of dead-ending on an unrecognized arg.
    res = _run(["restate", "--goal", "ship the thing well"], repo, "sess-restate", data)
    assert res.returncode == 0, res.stderr
    loaded = load_spec(str(repo), "sess-restate")
    assert loaded["restated_goal"] == "ship the thing well"


def test_second_restate_acks_already_satisfied(repo, data):
    # First restate clears goal_seeded; the second must emit a distinguishable
    # ack that the redundant-detection regex recognizes, so the PostToolUse steer
    # can stop the redundant repeat.
    from model_notify import _RESTATE_REDUNDANT_RE

    first = _run(["restate", "establish the goal in my own words"], repo, "sess-twice", data)
    assert first.returncode == 0, first.stderr
    assert not _RESTATE_REDUNDANT_RE.search(first.stdout)
    second = _run(["restate", "a thinner restatement"], repo, "sess-twice", data)
    assert second.returncode == 0, second.stderr
    assert _RESTATE_REDUNDANT_RE.search(second.stdout)
    assert second.stdout != first.stdout


def test_error_prog_is_unifable(repo, data):
    # The error prog name must match the CLI the model actually typed.
    res = _run(["add-task"], repo, "sess-prog", data)
    assert res.returncode == 2
    assert "unifable add-task" in res.stderr
    assert "spec.py" not in res.stderr.split("\n")[0]
