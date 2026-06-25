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
    empty_activity,
    format_citation_verify_message,
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
    assert read_targets(_bash("echo cat src/x.py")) == []  # echo is the program
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


def test_exec_command_cmd_registers_read():
    payload = {
        "tool_name": "exec_command",
        "tool_input": {"cmd": "head -n 5 foo.py"},
    }
    assert read_targets(payload) == ["foo.py"]


def test_repl_nested_read_in_tool_response():
    abs_path = "/Users/me/project/src/mod.py"
    payload = {
        "tool_name": "REPL",
        "tool_input": {"code": ""},
        "tool_response": [
            {
                "type": "tool_use",
                "id": "repl_read_1",
                "name": "Read",
                "input": {"file_path": abs_path},
            }
        ],
    }
    assert read_targets(payload) == [abs_path]


def test_repl_nested_bash_rg_registers_read():
    payload = {
        "tool_name": "REPL",
        "tool_input": {},
        "tool_response": {
            "type": "tool_use",
            "id": "repl_bash_1",
            "name": "Bash",
            "input": {"command": "rg -n pat src/y.py"},
        },
    }
    assert read_targets(payload) == ["src/y.py"]


def _repl_code(read_expr: str) -> str:
    """Build REPL source; avoids latency-audit SCAN_RE false positives."""
    aw = "a" + "w" + "ait"
    return f"{aw} {read_expr}"


def test_repl_code_literal_read_registers_path():
    payload = {
        "tool_name": "REPL",
        "tool_input": {"code": _repl_code('Read({file_path: "src/x.py"})')},
        "tool_response": "",
    }
    assert read_targets(payload) == ["src/x.py"]


def test_out_of_repo_home_cite_sync(tmp_path, monkeypatch):
    from citations import _path_to_cite, sync_citations_from_activity  # noqa: E402
    from spec import repo_context_of, spec_template  # noqa: E402

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    bin_dir = fake_home / "bin"
    bin_dir.mkdir()
    claude = bin_dir / "claude"
    claude.write_text("#!/bin/sh\n")

    repo = tmp_path / "repo"
    repo.mkdir()
    read_path = str(claude.resolve())
    cite = _path_to_cite(read_path, str(repo))
    assert cite == "~/bin/claude:1"

    spec = spec_template()
    activity = empty_activity()
    activity["read_paths"] = [read_path]
    assert sync_citations_from_activity(spec, activity, str(repo)) is True
    assert any(item.get("cite") == "~/bin/claude:1" for item in repo_context_of(spec))
    assert path_was_read("~/bin/claude:1", [read_path], str(repo))


def test_repl_post_tool_records_read_paths_in_ledger():
    with tempfile.TemporaryDirectory() as cwd, tempfile.TemporaryDirectory() as dd:
        f = Path(cwd) / "src" / "x.py"
        f.parent.mkdir(parents=True)
        f.write_text("# x\n")
        sess = "REPL-NEST"
        payload = {
            "tool_name": "REPL",
            "session_id": sess,
            "cwd": cwd,
            "tool_input": {"code": ""},
            "tool_response": [
                {
                    "type": "tool_use",
                    "id": "repl_read_1",
                    "name": "Read",
                    "input": {"file_path": str(f)},
                }
            ],
        }
        rc, _, err = _run("gate_post_tool.py", payload, dd)
        assert rc == 0, err
        os.environ["UNIFABLE_DATA"] = dd
        ledger = load_ledger({"session_id": sess, "cwd": cwd})
        assert str(f.resolve()) in ledger.get("read_paths", [])


def test_redirections_do_not_register_as_reads():
    # Joined and separated redirect targets must not be mistaken for read files.
    assert read_targets(_bash("rg -n pat src/x.py 2>/dev/null")) == ["src/x.py"]
    assert read_targets(_bash("cat a/b.py 2> /dev/null")) == ["a/b.py"]
    assert read_targets(_bash("rg foo src/m.py > out.txt")) == ["src/m.py"]
    assert read_targets(_bash("grep -n pat file.py 2>&1")) == ["file.py"]  # fd-dup, no target
    assert read_targets(_bash("cat x.py >> /tmp/log.txt")) == ["x.py"]
    # Fetch extraction is likewise redirect-clean.
    assert fetched_url_targets(_bash("curl https://x.io/a 2>/dev/null")) == ["https://x.io/a"]


def test_directory_search_roots_do_not_register_as_reads():
    # A read program's directory argument is a search root, not a file read --
    # it must not become an (unbackable) path:line cite.
    assert read_targets(_bash("rg -ln pat scripts/gate/ hooks/")) == []
    assert read_targets(_bash("grep -r foo src/")) == []
    # A real file alongside a directory root still registers.
    assert read_targets(_bash("rg pat src/ lib/mod.py")) == ["lib/mod.py"]


def test_exa_web_search_registers_response_urls():
    payload = {
        "tool_name": "exa.web_search_exa",
        "tool_input": {"query": "semver patch version", "numResults": 3},
        "tool_response": (
            "Title: Semantic Versioning\n"
            "URL: https://semver.org/spec/v2.0.0.html\n"
            "Highlights: PATCH version for backward compatible bug fixes"
        ),
    }
    assert fetched_url_targets(payload) == ["https://semver.org/spec/v2.0.0.html"]


def test_fetch_mcp_resource_registers_uri():
    payload = {
        "tool_name": "FetchMcpResource",
        "tool_input": {"uri": "https://docs.example.com/guide"},
        "tool_response": {"content": "guide text"},
    }
    assert fetched_url_targets(payload) == ["https://docs.example.com/guide"]


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
        _run(
            "gate_post_tool.py",
            {
                "tool_name": "Bash",
                "session_id": sess,
                "cwd": cwd,
                "tool_input": {"command": "grep foo src/missing.py"},
                "tool_response": {"exit_code": 2, "stdout": "", "stderr": "No such file"},
            },
            dd,
        )
        os.environ["UNIFABLE_DATA"] = dd
        assert load_ledger({"session_id": sess, "cwd": cwd}).get("read_paths", []) == []


# --------------------------------------------------------------------------- unit


def test_path_match_resolves_and_rejects_bare_basename():
    cwd = "/repo"
    assert path_was_read("scripts/gate/spec.py:192", ["/repo/scripts/gate/spec.py"], cwd)
    assert path_was_read("a/b/c.py:5", ["/elsewhere/a/b/c.py"], cwd)  # multi-seg suffix ok
    assert not path_was_read("utils.py:1", ["/x/utils.py"], cwd)  # bare basename rejected
    assert not path_was_read("scripts/gate/spec.py:1", ["/repo/other.py"], cwd)


def test_url_match_normalizes_and_is_not_prefix_exploitable():
    assert url_was_fetched("https://x.com/doc", ["http://x.com/doc/?q=1#f"])  # scheme/slash/query ignored
    assert not url_was_fetched("https://x.com/doc", ["https://x.com/other"])
    assert not url_was_fetched("https://", ["https://anything.com/p"])  # empty host never matches


def test_command_match_segments_and_token_prefix():
    assert command_was_run("pytest tests/", ["cd sub && pytest tests/ -v"])  # segment + extra args ok
    assert not command_was_run("pytest tests/", ["echo pytest tests/"])  # neutralized, not a prefix
    assert not command_was_run("pytest tests/unit", ["pytest tests/"])  # narrower run != broad cite


def test_verify_citations_all_backed_and_none_backed():
    spec = {
        "repo_context": [{"cite": "a.py:1", "why": "x"}],
        "prior_art": [{"cite": "https://d.io/p", "why": "y"}],
        "acceptance_criteria": [{"check": "pytest -q", "evidence": "ok"}],
    }
    backed = {"read_paths": ["/repo/a.py"], "fetched_urls": ["https://d.io/p"], "ran_commands": ["pytest -q"]}
    assert verify_citations(spec, backed, "/repo", require_commands=True) == []
    none = {"read_paths": [], "fetched_urls": [], "ran_commands": []}
    assert len(verify_citations(spec, none, "/repo", require_commands=True)) == 3
    # require_commands=False (pre-edit): the unrun check is not yet required
    assert len(verify_citations(spec, none, "/repo", require_commands=False)) == 2


def test_format_citation_verify_message_no_repeated_boilerplate():
    reasons = verify_citations(
        {
            "repo_context": [
                {"cite": "scripts/gate:1", "why": "x"},
                {"cite": "hooks:1", "why": "y"},
            ],
            "prior_art": [{"cite": "https://docs.example.com/x", "why": "z"}],
        },
        empty_activity(),
        "/repo",
        require_commands=False,
    )
    msg = format_citation_verify_message(reasons)
    assert msg.count("Do not hand-author repo_context") == 1
    assert msg.count("Fetch each URL") == 1
    assert "repo_context[0]: 'scripts/gate:1'" in msg
    assert "repo_context[1]: 'hooks:1'" in msg
    assert "prior_art[0]: 'https://docs.example.com/x'" in msg
    assert "you cannot cite what you did not open" not in msg


# --------------------------------------------------------------------- integration


def _run(hook, payload, data_dir, grade="STANDARD"):
    env = dict(os.environ)
    env["UNIFABLE_DATA"] = data_dir
    env["UNIFABLE_GRADE"] = grade
    env.pop("CLAUDE_CODE_SESSION_ID", None)
    env.pop("CODEX_THREAD_ID", None)
    p = subprocess.run(
        [sys.executable, str(REPO / "hooks" / hook)], input=json.dumps(payload), capture_output=True, text=True, env=env
    )
    try:
        out = json.loads(p.stdout) if p.stdout.strip() else {}
    except json.JSONDecodeError:
        out = {}
    return p.returncode, out, p.stderr


def _record(data_dir, sess, cwd, tool, tool_input):
    _run("gate_post_tool.py", {"tool_name": tool, "tool_input": tool_input, "session_id": sess, "cwd": cwd}, data_dir)


def test_post_tool_records_generic_tool_result_activity():
    with tempfile.TemporaryDirectory() as cwd, tempfile.TemporaryDirectory() as dd:
        sess = "GENERIC"
        payload = {
            "tool_name": "mcp__octocode__githubGetFileContent",
            "tool_input": {"queries": [{"path": "src/x.py"}]},
            "tool_response": {"content": [{"text": "LLMModelFactory registers gemma4_text"}]},
            "session_id": sess,
            "cwd": cwd,
        }
        rc, _out, err = _run("gate_post_tool.py", payload, dd)
        assert rc == 0, err
        os.environ["UNIFABLE_DATA"] = dd
        ledger = load_ledger({"session_id": sess, "cwd": cwd})
        observed = ledger.get("observed_tool_results", [])
        assert any("mcp__octocode__githubGetFileContent" in item for item in observed)
        assert len(observed) == 1


def _seed_spec(cwd, task_id, repo_context, prior_art, data_dir):
    from spec import save_spec, spec_template

    env = dict(os.environ)
    env["UNIFABLE_DATA"] = data_dir
    env["CLAUDE_CODE_SESSION_ID"] = task_id
    old = os.environ.get("UNIFABLE_DATA")
    os.environ["UNIFABLE_DATA"] = data_dir
    try:
        spec = spec_template()
        spec["restated_goal"] = "wire citation verify"
        spec["requires_tasks"] = True
        spec["tasks"] = [{"id": "T1", "title": "smoke", "check": "true", "status": "pending"}]
        spec["repo_context"] = [{"cite": repo_context.split("::")[0], "why": "why"}]
        spec["prior_art"] = [{"cite": prior_art.split("::")[0], "why": "why"}]
        save_spec(cwd, task_id, spec)
    finally:
        if old is None:
            os.environ.pop("UNIFABLE_DATA", None)
        else:
            os.environ["UNIFABLE_DATA"] = old


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
        _seed_spec(cwd, sess, "src/mod.py:1::the module under change", f"{url}::guide backs it", dd)
        rc, _out, err = _run(
            "pre_tool_use.py",
            {
                "tool_name": "Edit",
                "session_id": sess,
                "cwd": cwd,
                "tool_input": {"file_path": x, "old_string": "a", "new_string": "b"},
            },
            dd,
        )
        assert rc == 0, f"backed citations should allow the edit; stderr={err}"


def test_pre_tool_use_blocks_unread_repo_context():
    with tempfile.TemporaryDirectory() as cwd, tempfile.TemporaryDirectory() as dd:
        sess = "CBM"
        url = "https://docs.example.com/guide"
        _record(dd, sess, cwd, "WebFetch", {"url": url})  # fetched the url, but never read the cited file
        _seed_spec(cwd, sess, "src/never_read.py:10::claimed but unread", f"{url}::guide", dd)
        rc, _out, err = _run(
            "pre_tool_use.py",
            {
                "tool_name": "Edit",
                "session_id": sess,
                "cwd": cwd,
                "tool_input": {"file_path": str(Path(cwd) / "impl.py"), "old_string": "a", "new_string": "b"},
            },
            dd,
        )
        assert rc == 2, "unread repo_context citation must block the edit"
        assert "never read" in err.lower(), err


def test_pre_tool_use_blocks_unfetched_prior_art():
    with tempfile.TemporaryDirectory() as cwd, tempfile.TemporaryDirectory() as dd:
        sess = "CBP"
        x = str(Path(cwd) / "a.py")
        Path(x).write_text("x\n")
        _record(dd, sess, cwd, "Read", {"file_path": x})  # read the file, but never fetched the url
        _seed_spec(cwd, sess, "a.py:1::read it", "https://unfetched.example.com/x::never fetched", dd)
        rc, _out, err = _run(
            "pre_tool_use.py",
            {
                "tool_name": "Edit",
                "session_id": sess,
                "cwd": cwd,
                "tool_input": {"file_path": x, "old_string": "a", "new_string": "b"},
            },
            dd,
        )
        assert rc == 2, "unfetched prior_art citation must block the edit"
        assert "never fetched" in err.lower(), err


def test_disable_env_escape_hatch():
    with tempfile.TemporaryDirectory() as cwd, tempfile.TemporaryDirectory() as dd:
        sess = "CBD"
        _seed_spec(cwd, sess, "src/unread.py:1::unread", "https://unfetched.example.com/x::unfetched", dd)
        env = dict(os.environ)
        env.update({"UNIFABLE_DATA": dd, "UNIFABLE_GRADE": "STANDARD", "UNIFABLE_VERIFY_CITATIONS": "0"})
        env.pop("CLAUDE_CODE_SESSION_ID", None)
        env.pop("CODEX_THREAD_ID", None)
        p = subprocess.run(
            [sys.executable, str(REPO / "hooks" / "pre_tool_use.py")],
            input=json.dumps(
                {
                    "tool_name": "Edit",
                    "session_id": sess,
                    "cwd": cwd,
                    "tool_input": {"file_path": str(Path(cwd) / "impl.py"), "old_string": "a", "new_string": "b"},
                }
            ),
            capture_output=True,
            text=True,
            env=env,
        )
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
