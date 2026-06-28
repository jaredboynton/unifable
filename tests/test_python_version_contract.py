#!/usr/bin/env python3
"""Wave 2 contract: a single, shared Python 3.12+ interpreter gate.

The migration makes Python 3.12 the one supported implementation runtime. Every
entrypoint (setup, runtime sync, hook dispatch, compat shims) must reject an
older interpreter with ONE identical message, sourced from the shared
`unifable_runtime` package — never a per-launcher string. These tests pin the
shared gate's behavior and that the message is exactly one canonical template.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import unifable_runtime as rt  # noqa: E402


def test_min_python_is_312():
    assert rt.MIN_PYTHON == (3, 12)


def test_current_interpreter_is_supported():
    # The dev/CI interpreter must itself satisfy the contract.
    assert rt.is_supported_python()
    assert sys.version_info[:2] >= rt.MIN_PYTHON


@pytest.mark.parametrize("ver", [(3, 12), (3, 13), (4, 0)])
def test_supported_versions_pass(ver):
    assert rt.is_supported_python(ver)
    rt.require_supported_python(ver)  # must not raise


@pytest.mark.parametrize("ver", [(3, 11), (3, 9), (2, 7), (3, 0)])
def test_unsupported_versions_rejected(ver):
    assert not rt.is_supported_python(ver)
    with pytest.raises(SystemExit) as exc:
        rt.require_supported_python(ver)
    assert exc.value.code == 1


def test_error_message_is_single_canonical_template():
    # One template, parameterized only by version numbers — no per-call wording.
    msg = rt.python_version_error((3, 11))
    assert "3.12+" in msg
    assert "3.11" in msg
    # Same template, different found-version, identical prefix/suffix shape.
    other = rt.python_version_error((3, 9))
    assert msg.replace("3.11", "X") == other.replace("3.9", "X")


def test_error_text_matches_module_constant():
    rendered = rt.python_version_error((3, 11))
    expected = rt.PYTHON_VERSION_ERROR.format(
        min_major=3, min_minor=12, cur_major=3, cur_minor=11
    )
    assert rendered == expected


def test_require_prints_message_to_stderr_in_subprocess():
    # A child interpreter that pretends to be 3.11 must exit 1 and print the message.
    code = (
        f"import sys; sys.path.insert(0, {str(REPO)!r}); import unifable_runtime as rt;"
        " rt.require_supported_python((3, 11))"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert proc.returncode == 1
    assert "requires Python 3.12+" in proc.stderr
    assert "found 3.11" in proc.stderr
