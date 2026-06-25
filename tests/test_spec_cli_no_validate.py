"""The `validate` spec CLI subcommand is removed.

Spec/task validation is automatic at Stop (auto_validate_spec in the completion
gate); the model must never trigger it. A standalone `unifable validate` command
only reported structural validity, which contradicted the Stop completion breaker
(spec structurally valid, breaker still CLOSED on unvalidated tasks) and trapped
the model in a reconcile loop. These tests lock the command out for good.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts", "gate"))

import model_notify  # noqa: E402
import spec  # noqa: E402


def test_validate_subcommand_is_not_registered():
    # argparse rejects an unregistered subcommand with SystemExit (invalid choice).
    with pytest.raises(SystemExit):
        spec.main(["validate", "--grade", "STANDARD"])
    with pytest.raises(SystemExit):
        spec.main(["validate", "--grade", "STANDARD", "--require-evidence"])


def test_cmd_validate_handler_removed():
    assert not hasattr(spec, "_cmd_validate")


def test_validate_not_recognized_as_spec_cli_subcommand():
    sub, _tid = model_notify.parse_spec_cli_invocation("unifable validate --grade STANDARD")
    assert sub is None
    # A real append-only subcommand still parses, proving the regex still works.
    sub_ok, _ = model_notify.parse_spec_cli_invocation("unifable add-task --title x --check true")
    assert sub_ok == "add-task"
