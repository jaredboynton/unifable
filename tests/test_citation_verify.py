#!/usr/bin/env python3
"""Citations must be backed by REAL tool activity, not fabricated.

Unit: the matching primitives + verify_citations (path/url/command).
Integration: the live hook chain -- gate_post_tool records activity, then
pre_tool_use and gate_stop enforce that a spec's citations match it.

Runs under pytest or standalone (python3 tests/test_citation_verify.py).
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "gate"))

from citations import (  # noqa: E402
    command_was_run,
    path_was_read,
    url_was_fetched,
    verify_citations,
)
from ledger import load_ledger  # noqa: E402
from parse_tool_result import fetched_url_targets, read_targets  # noqa: E402


def _bash(cmd):
    return {"tool_name": "Bash", "tool_input": {"command": cmd}}


# ------------------------------------------------------ bypass regression (extractors)

def test_bash_comment_does_not_register_read():
    assert read_targets(_bash("# cat secret.py")) == []
    assert read_targets(_bash(": cat secret.py")) == []  # ':' is the program, not cat


def test_non_read_program_does_not_register():
    assert read_targets(_bash("echo cat src/x.py")) == []      # echo is the program
    assert read_targets(_bash("echo https://cited.url")) == []  # echo is not a fetcher
    assert fetched_url_targets(_bash("echo https://cited.url")) == []


def test_curl_help_comment_does_not_register_fetch():
    assert fetched_url_targets(_bash("curl --help # https://cited.url")) == []


def test_script_reader_code_does_not_register_path():
    assert read_targets(_bash("awk 'BEGIN { print \"/code/lib.py\" }'")) == []
    assert read_targets(_bash("sed 's#/old/p.py#/new/p.py#g' input")) == []


def test_real_reads_and_fetches_do_register():
    assert read_targets(_bash("cat src/x.py")) == ["src/x.py"]
    assert read_targets(_bash("cd sub && grep foo src/y.py")) == ["src/y.py"]  # pattern skipped
    assert fetched_url_targets(_bash("curl -s https://d.io/p")) == ["https://d.io/p"]


def test_multiline_script_records_read_and_fetch():
    # A read/fetch on its OWN line of a multi-line script (program not first token
    # of the whole command) must still be detected -- segments split on newlines.
    cmd = "cd /repo\necho start\ncat src/x.py\ncurl -s https://d.io/p"
    assert "src/x.py" in read_targets(_bash(cmd))
    assert "https://d.io/p" in fetched_url_targets(_bash(cmd))


def test_failed_command_not_recorded():
    with tempfile.TemporaryDirectory() as cwd, tempfile.TemporaryDirectory() as dd:
        sess = "CF"
        # A grep that errors (exit 2) must NOT register the file as read.
        _run("gate_post_tool.py", {"tool_name": "Bash", "session_id": sess, "cwd": cwd,
             "tool_input": {"command": "grep foo src/missing.py"},
             "tool_response": {"exit_code": 2, "stdout": "", "stderr": "No such file"}}, dd)
        os.environ["UNIFABLE_DATA"] = dd
        assert load_ledger({"session_id": sess, "cwd": cwd}).get("read_paths", []) == []


# --------------------------------------------------------------------------- unit

def test_path_match_resolves_and_rejects_bare_basename():
    cwd = "/repo"
    assert path_was_read("scripts/gate/spec.py:192", ["/repo/scripts/gate/spec.py"], cwd)
    assert path_was_read("a/b/c.py:5", ["/elsewhere/a/b/c.py"], cwd)  # multi-seg suffix ok
    assert not path_was_read("utils.py:1", ["/x/utils.py"], cwd)      # bare basename rejected
    assert not path_was_read("scripts/gate/spec.py:1", ["/repo/other.py"], cwd)


def test_url_match_normalizes_and_is_not_prefix_exploitable():
    assert url_was_fetched("https://x.com/doc", ["http://x.com/doc/?q=1#f"])  # scheme/slash/query ignored
    assert not url_was_fetched("https://x.com/doc", ["https://x.com/other"])
    assert not url_was_fetched("https://", ["https://anything.com/p"])        # empty host never matches


def test_command_match_segments_and_token_prefix():
    assert command_was_run("pytest tests/", ["cd sub && pytest tests/ -v"])   # segment + extra args ok
    assert not command_was_run("pytest tests/", ["echo pytest tests/"])       # neutralized, not a prefix
    assert not command_was_run("pytest tests/unit", ["pytest tests/"])        # narrower run != broad cite


def test_verify_citations_all_backed_and_none_backed():
    spec = {
        "must_read": [{"cite": "a.py:1", "why": "x"}],
        "prior_art": [{"cite": "https://d.io/p", "why": "y"}],
        "acceptance_criteria": [{"check": "pytest -q", "evidence": "ok"}],
    }
    backed = {"read_paths": ["/repo/a.py"], "fetched_urls": ["https://d.io/p"], "ran_commands": ["pytest -q"]}
    assert verify_citations(spec, backed, "/repo", require_commands=True) == []
    none = {"read_paths": [], "fetched_urls": [], "ran_commands": []}
    assert len(verify_citations(spec, none, "/repo", require_commands=True)) == 3
    # require_commands=False (pre-edit): the unrun check is not yet required
    assert len(verify_citations(spec, none, "/repo", require_commands=False)) == 2


# --------------------------------------------------------------------- integration

def _run(hook, payload, data_dir, grade="STANDARD"):
    env = dict(os.environ)
    env["UNIFABLE_DATA"] = data_dir
    env["UNIFABLE_GRADE"] = grade
    env.pop("CLAUDE_CODE_SESSION_ID", None)
    env.pop("CODEX_THREAD_ID", None)
    p = subprocess.run([sys.executable, str(REPO / "hooks" / hook)],
                       input=json.dumps(payload), capture_output=True, text=True, env=env)
    try:
        out = json.loads(p.stdout) if p.stdout.strip() else {}
    except json.JSONDecodeError:
        out = {}
    return p.returncode, out, p.stderr


def _record(data_dir, sess, cwd, tool, tool_input):
    _run("gate_post_tool.py", {"tool_name": tool, "tool_input": tool_input,
                               "session_id": sess, "cwd": cwd}, data_dir)


def _create_spec(cwd, task_id, must_read, prior_art):
    args = [sys.executable, str(REPO / "scripts" / "gate" / "spec.py"), "create",
            "--root", cwd, "--task-id", task_id, "--goal", "wire citation verify",
            "--task", "smoke::true", "--must-read", must_read, "--prior-art", prior_art]
    return subprocess.run(args, capture_output=True, text=True)


def test_pre_tool_use_allows_when_citations_backed():
    with tempfile.TemporaryDirectory() as cwd, tempfile.TemporaryDirectory() as dd:
        sess = "CB"
        x = str(Path(cwd) / "src" / "mod.py")
        Path(x).parent.mkdir(parents=True, exist_ok=True)
        Path(x).write_text("# real file\n")
        url = "https://docs.example.com/guide"
        # Activity: actually read X, actually fetched URL.
        _record(dd, sess, cwd, "Read", {"file_path": x})
        _record(dd, sess, cwd, "WebFetch", {"url": url})
        os.environ["UNIFABLE_DATA"] = dd  # so load_ledger below reads the seeded ledger
        recorded = load_ledger({"session_id": sess, "cwd": cwd}).get("read_paths", [])
        assert str(Path(x).resolve()) in recorded, f"Read should be recorded (resolved); got {recorded}"
        r = _create_spec(cwd, sess, f"src/mod.py:1::the module under change", f"{url}::guide backs it")
        assert r.returncode == 0, r.stderr
        rc, _out, err = _run("pre_tool_use.py", {"tool_name": "Edit", "session_id": sess, "cwd": cwd,
                             "tool_input": {"file_path": x, "old_string": "a", "new_string": "b"}}, dd)
        assert rc == 0, f"backed citations should allow the edit; stderr={err}"


def test_pre_tool_use_blocks_unread_must_read():
    with tempfile.TemporaryDirectory() as cwd, tempfile.TemporaryDirectory() as dd:
        sess = "CBM"
        url = "https://docs.example.com/guide"
        _record(dd, sess, cwd, "WebFetch", {"url": url})  # fetched the url, but never read the cited file
        r = _create_spec(cwd, sess, "src/never_read.py:10::claimed but unread", f"{url}::guide")
        assert r.returncode == 0, r.stderr
        rc, _out, err = _run("pre_tool_use.py", {"tool_name": "Edit", "session_id": sess, "cwd": cwd,
                             "tool_input": {"file_path": str(Path(cwd) / "impl.py"),
                                            "old_string": "a", "new_string": "b"}}, dd)
        assert rc == 2, "unread must_read citation must block the edit"
        assert "never read" in err.lower(), err


def test_pre_tool_use_blocks_unfetched_prior_art():
    with tempfile.TemporaryDirectory() as cwd, tempfile.TemporaryDirectory() as dd:
        sess = "CBP"
        x = str(Path(cwd) / "a.py")
        Path(x).write_text("x\n")
        _record(dd, sess, cwd, "Read", {"file_path": x})  # read the file, but never fetched the url
        r = _create_spec(cwd, sess, "a.py:1::read it", "https://unfetched.example.com/x::never fetched")
        assert r.returncode == 0, r.stderr
        rc, _out, err = _run("pre_tool_use.py", {"tool_name": "Edit", "session_id": sess, "cwd": cwd,
                             "tool_input": {"file_path": x, "old_string": "a", "new_string": "b"}}, dd)
        assert rc == 2, "unfetched prior_art citation must block the edit"
        assert "never fetched" in err.lower(), err


def test_disable_env_escape_hatch():
    with tempfile.TemporaryDirectory() as cwd, tempfile.TemporaryDirectory() as dd:
        sess = "CBD"
        r = _create_spec(cwd, sess, "src/unread.py:1::unread", "https://unfetched.example.com/x::unfetched")
        assert r.returncode == 0, r.stderr
        env = dict(os.environ)
        env.update({"UNIFABLE_DATA": dd, "UNIFABLE_GRADE": "STANDARD", "UNIFABLE_VERIFY_CITATIONS": "0"})
        env.pop("CLAUDE_CODE_SESSION_ID", None)
        env.pop("CODEX_THREAD_ID", None)
        p = subprocess.run([sys.executable, str(REPO / "hooks" / "pre_tool_use.py")],
                           input=json.dumps({"tool_name": "Edit", "session_id": sess, "cwd": cwd,
                                             "tool_input": {"file_path": str(Path(cwd) / "impl.py"),
                                                            "old_string": "a", "new_string": "b"}}),
                           capture_output=True, text=True, env=env)
        assert p.returncode == 0, f"UNIFABLE_VERIFY_CITATIONS=0 must waive the cross-check; stderr={p.stderr}"


if __name__ == "__main__":
    fails = 0
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            try:
                _fn()
                print(f"  [OK] {_name}")
            except AssertionError as e:
                fails += 1
                print(f"  [FAIL] {_name}: {e}")
    print("RESULT:", "all pass" if not fails else f"{fails} failed")
    sys.exit(1 if fails else 0)
