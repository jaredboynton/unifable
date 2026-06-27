#!/usr/bin/env python3
"""Facade-API guard for scripts/gate/spec.py.

spec.py is split into focused sub-modules (spec_schema, spec_io, spec_validation,
spec_contracts, spec_tasks, spec_judge, spec_stop_validate, spec_cli) and kept as a
thin re-export facade. This test pins the public + test-relied import surface so a
missing re-export fails loudly the moment a symbol drops off the facade.

Runs under pytest or standalone (python3 tests/test_spec_facade_api.py).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "gate"))

import spec as spec_mod  # noqa: E402

# Every symbol any production or test caller resolves through the `spec` module
# (public API + the underscore internals tests import or patch). Sourced from a
# repo-wide scan of `from spec import ...` / `import spec as spec_mod; spec_mod.X`.
FACADE_SYMBOLS = (
    # schema / fake-evidence / citation helpers
    "SPEC_SCHEMA",
    "GRADES",
    "FAKE_MARKERS",
    "check_fake_evidence",
    "is_path_line",
    "is_source_url",
    "repo_context_parts",
    "repo_context_of",
    "prior_art_parts",
    "spec_template",
    # validation / contracts
    "repo_maintenance_waives_prior_art",
    "validate_spec",
    "contract_string",
    "format_spec_validation_block",
    # io / session helpers
    "resolve_session_id",
    "resolve_session_id_with_source",
    "canonical_project_root",
    "dir_hash",
    "session_dir",
    "spec_path",
    "format_spec_location",
    "load_spec",
    "save_spec",
    "ensure_spec_scaffold",
    "_safe_session",
    # task model
    "find_task",
    "all_tasks_validated",
    "is_brittle_version_pinned_requirement",
    "detect_requirement_fragmentation",
    "append_frontier_task",
    "set_primary_task",
    "_new_task",
    "_task_is_pending",
    "_apply_supersedes_bundle",
    "_filter_judge_new_requirements",
    "_current_requirements_payload",
    "JUDGE_MAX_UNRESOLVED_ADDED",
    # judge surface
    "judge_task",
    "judge_all_tasks",
    "judge_tasks",
    "judge_reconcile_spec",
    "judge_hint",
    "judge_discover_frontiers",
    "judge_frontier_comparison",
    "judge_heal_own_requirements",
    "_apply_adjustments",
    "_build_validate_all_user",
    "_evidence_payload",
    "_judge_context",
    "_judge_user",
    "_judge_system_for_task",
    "_judge_system_with_transcript",
    "_render_judge_transcript",
    "_validate_all_system",
    "_normalize_hint",
    "_normalize_new_requirements",
    "_normalize_reconcile_actions",
    "_apply_reconcile_actions",
    "_FRONTIER_JUDGE_SCHEMA",
    "_JUDGE_CORE_GUIDANCE",
    "_RECONCILE_SCHEMA",
    "notify_spec_update",
    # stop-path validation
    "auto_validate_spec",
    "_validate_one_task",
    "_apply_check_result",
    "is_runnable_check",
    "run_check",
    "heal_judge_owned_requirements",
    "deterministic_heal_judge_requirements",
    # cli
    "main",
    "_apply_cli_context",
    "_cmd_add_task",
    "_cmd_add_frontier",
    "_cmd_set_primary",
    "_cmd_restate",
    "_cmd_doctor_session_env",
)


def test_facade_exports_all_symbols():
    missing = [name for name in FACADE_SYMBOLS if not hasattr(spec_mod, name)]
    assert not missing, f"spec facade is missing re-exports: {missing}"


def test_facade_symbols_importable_directly():
    # `from spec import X` must resolve for every pinned symbol.
    import importlib

    mod = importlib.import_module("spec")
    for name in FACADE_SYMBOLS:
        assert getattr(mod, name, None) is not None, f"spec.{name} resolved to None"


if __name__ == "__main__":
    test_facade_exports_all_symbols()
    test_facade_symbols_importable_directly()
    print("OK: spec facade exposes", len(FACADE_SYMBOLS), "symbols")
