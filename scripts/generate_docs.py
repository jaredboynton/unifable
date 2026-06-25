#!/usr/bin/env python3
"""Generate model-visible hook output and judge prompt reference docs."""

from __future__ import annotations

import argparse
import copy
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
GATE_DIR = ROOT / "scripts" / "gate"
HOOKS_DIR = ROOT / "hooks"
GENERATED_DIR = ROOT / "docs" / "generated"
ROOT_TOKEN = "${REPO_ROOT}"
PLUGIN_ROOT_TOKEN = "${CLAUDE_PLUGIN_ROOT}"

for path in (str(GATE_DIR), str(HOOKS_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

import breaker_judges  # noqa: E402
import classify_task  # noqa: E402
import codex_judge  # noqa: E402
import completion_handoff  # noqa: E402
import context_block  # noqa: E402
import gate_prompt_effort  # noqa: E402
import gate_stop  # noqa: E402
import grade_override  # noqa: E402
import heavy_workflow  # noqa: E402
import hook_output  # noqa: E402
import loop_release  # noqa: E402
import pretool_block  # noqa: E402
import spec as spec_mod  # noqa: E402
from atomicio import write_text_atomic  # noqa: E402

DOCS: tuple[tuple[str, str], ...] = (
    ("claude-hookoutputs.md", "claude"),
    ("codex-hookoutputs.md", "codex"),
    ("judgeprompts.md", "judge"),
)


@dataclass(frozen=True)
class HookSpec:
    event: str
    matcher: str
    command: str
    timeout: Any
    status_message: str


@dataclass(frozen=True)
class HookScenario:
    name: str
    event: str
    stdout: dict[str, Any] | None = None
    stderr: str = ""
    exit_code: int = 0


@dataclass(frozen=True)
class JudgePrompt:
    name: str
    source: str
    schema_name: str
    system: str
    user: str
    schema: dict[str, Any]
    transport: dict[str, Any]


def _normalize_text(text: str) -> str:
    return text.replace(str(ROOT), ROOT_TOKEN)


def _normalize_obj(obj: Any) -> Any:
    if isinstance(obj, str):
        return _normalize_text(obj)
    if isinstance(obj, list):
        return [_normalize_obj(item) for item in obj]
    if isinstance(obj, tuple):
        return [_normalize_obj(item) for item in obj]
    if isinstance(obj, dict):
        return {_normalize_obj(key): _normalize_obj(value) for key, value in obj.items()}
    return obj


def _json(obj: Any) -> str:
    return json.dumps(_normalize_obj(obj), ensure_ascii=False, indent=2, sort_keys=True)


def _fence(value: str, lang: str = "") -> str:
    return f"```{lang}\n{_normalize_text(value).rstrip()}\n```"


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _hook_command_name(command: str) -> str:
    for token in (
        "router.sh",
        "gate_prompt.py",
        "gate_prompt_effort.py",
        "pre_tool_use.py",
        "gate_post_tool.py",
        "test_after_edit.py",
        "gate_stop.py",
    ):
        if token in command:
            return token
    return command


def collect_hook_specs(host: str) -> list[HookSpec]:
    config_path = ROOT / ("hooks/hooks.json" if host == "claude" else ".codex-plugin/hooks.json")
    config = _read_json(config_path)
    out: list[HookSpec] = []
    for event, groups in (config.get("hooks") or {}).items():
        for group in groups or []:
            matcher = str(group.get("matcher", ""))
            for hook in group.get("hooks") or []:
                out.append(
                    HookSpec(
                        event=str(event),
                        matcher=matcher,
                        command=str(hook.get("command") or ""),
                        timeout=hook.get("timeout", ""),
                        status_message=str(hook.get("statusMessage") or ""),
                    )
                )
    return out


def _run_router_fixture() -> dict[str, Any]:
    """Render a sample UserPromptSubmit router (pack signal) context.

    The prompt is chosen to hit every route keyword so the reference doc catalogs
    every pack. The live router caps how many packs fire per prompt
    (pack_router._MAX_PACKS); for documentation completeness this fixture renders
    all matched packs via format_context rather than the capped route_prompt path.
    """
    prompt = "debug html architecture implement subagent"
    import pack_router

    routes = pack_router.load_manifest(ROOT)
    matched = pack_router.match_routes(prompt, routes)
    if not matched:
        return {}
    return {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": pack_router.format_context(matched, packs_root=str(ROOT)),
        }
    }


def _sample_stop_payload(host: str) -> dict[str, Any]:
    payload = {
        "decision": "block",
        "reason": "breaker CLOSED: 1 task(s) not validated (T1).",
    }
    validate_ctx = (
        "Action required:\n"
        "  T1 [--] Generated docs are reproducible and current\n"
        "    judge: Run python3 scripts/generate_docs.py --check."
    )
    return hook_output.finalize_stop_payload(
        payload,
        validate_ctx=validate_ctx,
        digest_path="docs/generated/judgeprompts.md",
        host=host,  # type: ignore[arg-type]
    )


def _hook_scenarios(host: str) -> list[HookScenario]:
    normal_context = classify_task.context_for_mode("normal", [])
    quick_context = classify_task.context_for_mode("quick", [])
    deep_context = classify_task.context_for_mode("deep", ["uncertainty"])
    effort_context = gate_prompt_effort._playbook_context()
    if len(effort_context) > 12_000:
        effort_context = effort_context[:12_000].rstrip() + "\n...[truncated in generated docs]"

    post_context = ""
    test_context = (
        "PASS (pytest -q): tests passed\ncommand: python3 -m pytest tests/test_generate_docs.py -q"
    )
    breaker_context = "Breaker open: the flagged claim is grounded. Write/Edit/Bash are unrestricted again."
    session_start_context = context_block.build_session_context()

    return [
        HookScenario(
            name="SessionStart thin judge-relationship frame",
            event="SessionStart",
            stdout={
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": session_start_context,
                }
            },
        ),
        HookScenario(
            name="UserPromptSubmit router signal pack context",
            event="UserPromptSubmit",
            stdout=_run_router_fixture(),
        ),
        HookScenario(
            name="UserPromptSubmit grade context: quick",
            event="UserPromptSubmit",
            stdout={
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": quick_context,
                }
            },
        ),
        HookScenario(
            name="UserPromptSubmit grade context: normal",
            event="UserPromptSubmit",
            stdout={
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": normal_context,
                }
            },
        ),
        HookScenario(
            name="UserPromptSubmit grade context: deep",
            event="UserPromptSubmit",
            stdout={
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": deep_context + "\n\n" + heavy_workflow.heavy_workflow_brief(),
                }
            },
        ),
        HookScenario(
            name="UserPromptSubmit effort playbook injection",
            event="UserPromptSubmit",
            stdout={
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": effort_context,
                }
            },
        ),
        HookScenario(
            name="PreToolUse missing evidence spec block",
            event="PreToolUse",
            stderr=pretool_block.GATE_PREFIX
            + pretool_block.format_spec_missing_block(
                "STANDARD",
                "sample-session",
                spec_mod.contract_string("STANDARD", require_evidence=True, evidence_profile="code"),
            ),
            exit_code=2,
        ),
        HookScenario(
            name="PreToolUse Bash research whitelist block",
            event="PreToolUse",
            stderr=pretool_block.GATE_PREFIX
            + pretool_block.format_bash_research_block(
                "npm is not in the Bash research whitelist",
                "sample-session",
            ),
            exit_code=2,
        ),
        HookScenario(
            name="PreToolUse delegation block",
            event="PreToolUse",
            stderr=pretool_block.GATE_PREFIX + pretool_block.format_delegation_block("Task", "sample-session"),
            exit_code=2,
        ),
        HookScenario(
            name="PreToolUse allow with breaker notification",
            event="PreToolUse",
            stdout={
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "additionalContext": breaker_context,
                }
            },
        ),
        HookScenario(
            name="PostToolUse spec notification context",
            event="PostToolUse",
            stdout={
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": post_context,
                }
            },
        ),
        HookScenario(
            name="PostToolUse test-after-edit context",
            event="PostToolUse",
            stdout={
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": test_context,
                }
            },
        ),
        HookScenario(
            name="Stop evidence-spec block",
            event="Stop",
            stdout=_sample_stop_payload(host),
        ),
        HookScenario(
            name="Stop goal-mode block",
            event="Stop",
            stdout={
                "decision": "block",
                "reason": "Stop hook feedback:\n[G001] insufficient evidence in transcript",
            },
        ),
        HookScenario(
            name="Stop fail-open warning",
            event="Stop",
            stdout={"systemMessage": "Stop hook failed open: sample error"},
        ),
    ]


def render_hook_doc(host: str) -> str:
    title = "Claude Hook Outputs" if host == "claude" else "Codex Hook Outputs"
    config_path = "hooks/hooks.json" if host == "claude" else ".codex-plugin/hooks.json"
    lines = [
        f"# {title}",
        "",
        "_Generated by `python3 scripts/generate_docs.py`._",
        "",
        f"Source hook config: `{config_path}`.",
        "",
        "## Registered Hooks",
        "",
        "| Event | Matcher | Command | Timeout | Status Message |",
        "|---|---|---|---:|---|",
    ]
    for hook in collect_hook_specs(host):
        lines.append(
            "| "
            + " | ".join(
                [
                    hook.event,
                    hook.matcher or "(empty)",
                    f"`{_hook_command_name(hook.command)}`",
                    str(hook.timeout),
                    hook.status_message or "",
                ]
            )
            + " |"
        )
    lines.extend(["", "## Rendered Model-Visible Outputs", ""])
    for scenario in _hook_scenarios(host):
        lines.extend([f"### {scenario.name}", "", f"Event: `{scenario.event}`", ""])
        if scenario.stdout is not None:
            lines.extend(["stdout:", _fence(_json(scenario.stdout), "json"), ""])
        if scenario.stderr:
            lines.extend(["stderr:", _fence(scenario.stderr, "text"), ""])
        lines.extend(["exit code:", _fence(str(scenario.exit_code), "text"), ""])
    return "\n".join(lines).rstrip() + "\n"


def _sample_spec() -> dict[str, Any]:
    return {
        "restated_goal": "Generate docs for hook outputs and judge prompts.",
        "tasks": [
            {
                "id": "T1",
                "title": "Generated docs are current",
                "check": "python3 scripts/generate_docs.py --check",
                "status": "pending",
                "added_by": "agent",
                "exit": 0,
                "output": "docs are current\n",
                "judge_reason": "",
            },
            {
                "id": "T2",
                "title": "Judge-owned stale requirement",
                "check": "python3 -c 'print(1)'",
                "status": "failed",
                "added_by": "judge",
                "exit": 1,
                "output": "sample failure\n",
                "judge_reason": "sample reason",
            },
        ],
        "repo_context": [{"cite": "scripts/generate_docs.py:1", "why": "generator entrypoint"}],
        "prior_art": [{"cite": "https://git-scm.com/docs/githooks", "why": "commit hook behavior"}],
    }


def _sample_task(kind: str = "requirement") -> dict[str, Any]:
    task = {
        "id": "T1",
        "title": "Generated docs are current",
        "check": "python3 scripts/generate_docs.py --check",
        "status": "pending",
        "added_by": "agent",
    }
    if kind != "requirement":
        task["approach_kind"] = kind
    return task


def _sample_heavy_spec_for_comparison() -> dict[str, Any]:
    """Spec with >=2 terminal frontiers (>=1 accepted) so the comparison fires."""
    return {
        "restated_goal": "Generate docs for hook outputs and judge prompts.",
        "tasks": [
            {
                "id": "T1",
                "title": "Streaming doc generator",
                "check": "python3 scripts/generate_docs.py --check",
                "status": "accepted_approach",
                "approach_kind": "frontier",
                "added_by": "agent",
                "exit": 0,
                "output": "5 passed\n",
                "judge_reason": "viable: fast and comprehensive",
            },
            {
                "id": "T2",
                "title": "Batch doc generator",
                "check": "python3 scripts/generate_docs_batch.py --check",
                "status": "accepted_approach",
                "approach_kind": "frontier",
                "added_by": "agent",
                "exit": 0,
                "output": "3 passed\n",
                "judge_reason": "viable: simpler but slower",
            },
            {
                "id": "T3",
                "title": "Static site fallback",
                "check": "python3 scripts/static_docs.py --check",
                "status": "rejected_approach",
                "approach_kind": "primary",
                "added_by": "agent",
            },
        ],
        "repo_context": [{"cite": "scripts/generate_docs.py:1", "why": "generator entrypoint"}],
        "prior_art": [{"cite": "https://git-scm.com/docs/githooks", "why": "commit hook behavior"}],
    }


def _fake_response(schema_name: str, schema: dict[str, Any], user: str) -> dict[str, Any]:
    if schema_name == "grade_classify":
        return {"mode": "normal", "risk_flags": [], "reason": "sample", "evidence_profile": "code"}
    if schema_name == "judge_heal":
        return {"adjust_requirements": [], "reason": "sample"}
    if schema_name == "validate_all":
        try:
            payload = json.loads(user)
            ids = [str(t.get("id")) for t in payload.get("tasks_to_adjudicate", [])]
        except Exception:
            ids = ["T1"]
        return {"task_verdicts": [{"id": tid, "verdict": 0, "reason": "sample judge rejection"} for tid in ids]}
    if schema_name == "frontier_discover":
        return {"frontiers": [], "reason": "sample"}
    if schema_name == "frontier_comparison":
        return {"selected_id": "T1", "selection_rationale": "T1 has more comprehensive test coverage"}
    if schema_name == "hint":
        return {"hint": "Run the generated-docs check and inspect the first diff."}
    if schema_name == "loop_release":
        return {"suicide_loop": False, "lift": "none", "reason": "sample"}
    if schema_name == "completion_handoff":
        return {
            "ok_to_stop": False,
            "reason": "Agent deferred autonomous investigation.",
            "steering": "Read the transcript and report findings.",
            "blocked_on_user_only": False,
        }

    props = schema.get("properties") if isinstance(schema, dict) else {}
    if isinstance(props, dict) and "grounded" in props:
        return {
            "load_bearing": 1,
            "grounded": 0,
            "needed": "Read the referenced file and cite the relevant line.",
            "provisional_release": 0,
            "lift_reason": "",
            "lift_scope": "",
        }
    if isinstance(props, dict) and "drift_level" in props:
        return {"drift_level": 0, "feedback": ""}
    if isinstance(props, dict) and "steering" in props:
        return {"load_bearing": 0, "verdict": 0, "steering": "", "claim": ""}
    if isinstance(props, dict) and "outcome" in props:
        return {"verdict": 0, "outcome": "still_viable", "reason": "sample"}
    return {"verdict": 0, "reason": "sample"}


def _transport(system: str, user: str, schema: dict[str, Any], schema_name: str) -> dict[str, Any]:
    return codex_judge.render_structured_request(
        system,
        user,
        schema,
        schema_name=schema_name,
    )


def _capture_call(name: str, source: str, fn: Callable[[], Any]) -> list[JudgePrompt]:
    captured: list[JudgePrompt] = []
    original = codex_judge.ask_structured

    def fake(system: str, user: str, schema: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        schema_name = str(kwargs.get("schema_name") or "result")
        captured.append(
            JudgePrompt(
                name=name,
                source=source,
                schema_name=schema_name,
                system=str(system),
                user=str(user),
                schema=copy.deepcopy(schema),
                transport=_transport(str(system), str(user), copy.deepcopy(schema), schema_name),
            )
        )
        return _fake_response(schema_name, schema, user)

    codex_judge.ask_structured = fake  # type: ignore[assignment]
    try:
        fn()
    finally:
        codex_judge.ask_structured = original  # type: ignore[assignment]
    return captured


def _capture_direct(
    name: str,
    source: str,
    schema_name: str,
    fn: Callable[[Callable[[str, str, dict[str, Any]], dict[str, Any]]], Any],
) -> JudgePrompt:
    captured: list[JudgePrompt] = []

    def fake(system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        captured.append(
            JudgePrompt(
                name=name,
                source=source,
                schema_name=schema_name,
                system=str(system),
                user=str(user),
                schema=copy.deepcopy(schema),
                transport=_transport(str(system), str(user), copy.deepcopy(schema), schema_name),
            )
        )
        return _fake_response(schema_name, schema, user)

    fn(fake)
    if not captured:
        raise RuntimeError(f"judge prompt case did not capture: {name}")
    return captured[0]


def collect_judge_prompts() -> list[JudgePrompt]:
    sample_spec = _sample_spec()
    standard_task = _sample_task()
    frontier_task = _sample_task("frontier")
    primary_task = _sample_task("primary")

    cases: list[JudgePrompt] = []
    cases += _capture_call(
        "Grade classifier",
        "scripts/gate/grade_override.py",
        lambda: grade_override.judge_grade_classify(
            "Implement generated docs for hook outputs.",
            restated_goal="Generate docs.",
            task_summary=[{"id": "T1", "title": "Generated docs", "kind": "requirement", "status": "pending"}],
        ),
    )
    cases += _capture_call(
        "Judge-owned requirement self-heal",
        "scripts/gate/spec.py",
        lambda: spec_mod.judge_heal_own_requirements(copy.deepcopy(sample_spec)),
    )
    cases += _capture_call(
        "Batch requirement validation",
        "scripts/gate/spec.py",
        lambda: spec_mod.judge_all_tasks(
            copy.deepcopy(sample_spec),
            [{"task": copy.deepcopy(standard_task), "kind": "validate", "exit_code": 0, "output": "docs are current\n"}],
            transcript='<record line="000001" role="assistant">sample</record>',
        ),
    )
    cases += _capture_call(
        "Single requirement validation",
        "scripts/gate/spec.py",
        lambda: spec_mod.judge_task(copy.deepcopy(sample_spec), copy.deepcopy(standard_task), 0, "docs are current\n"),
    )
    cases += _capture_call(
        "Frontier approach validation",
        "scripts/gate/spec.py",
        lambda: spec_mod.judge_task(copy.deepcopy(sample_spec), copy.deepcopy(frontier_task), 1, "experiment failed\n"),
    )
    cases += _capture_call(
        "Frontier comparison",
        "scripts/gate/spec.py",
        lambda: spec_mod.judge_frontier_comparison(
            _sample_heavy_spec_for_comparison(),
        ),
    )
    cases += _capture_call(
        "Primary approach validation",
        "scripts/gate/spec.py",
        lambda: spec_mod.judge_task(copy.deepcopy(sample_spec), copy.deepcopy(primary_task), 0, "primary passed\n"),
    )
    cases += _capture_call(
        "Frontier discovery",
        "scripts/gate/spec.py",
        lambda: spec_mod.judge_discover_frontiers(
            copy.deepcopy(sample_spec),
            {"read_paths": ["scripts/generate_docs.py"], "fetched_urls": ["https://git-scm.com/docs/githooks"]},
        ),
    )
    cases += _capture_call(
        "Dispute adjudication",
        "scripts/gate/spec.py",
        lambda: spec_mod.judge_dispute(
            copy.deepcopy(sample_spec),
            copy.deepcopy(standard_task),
            "The requirement was superseded by generated-doc checks.",
        ),
    )
    cases += _capture_call(
        "Advisory stuck hint",
        "scripts/gate/spec.py",
        lambda: spec_mod.judge_hint(
            copy.deepcopy(sample_spec),
            signal="Stop has blocked repeatedly.",
            recent="python3 scripts/generate_docs.py --check",
        ),
    )
    cases.append(
        _capture_direct(
            "Groundedness arm",
            "scripts/gate/breaker_judges.py",
            "groundedness",
            lambda judge: breaker_judges.arm_judge(
                '<record line="000001" role="assistant">The docs are already current.</record>',
                events=[],
                judge=judge,
            ),
        )
    )
    cases.append(
        _capture_direct(
            "Groundedness release",
            "scripts/gate/breaker_judges.py",
            "groundedness",
            lambda judge: breaker_judges.disarm_judge(
                "The docs are already current.",
                '<record line="000002" role="tool">python3 scripts/generate_docs.py --check passed</record>',
                user_goal="Generate docs.",
                judge=judge,
            ),
        )
    )
    cases.append(
        _capture_direct(
            "Provisional lift monitor",
            "scripts/gate/breaker_judges.py",
            "groundedness",
            lambda judge: breaker_judges.monitor_provisional_judge(
                "The docs are already current.",
                "Run the generated-docs check.",
                '<record line="000003" role="tool">checking docs</record>',
                "Bash",
                user_goal="Generate docs.",
                judge=judge,
            ),
        )
    )
    cases += _capture_call(
        "Completion loop release",
        "scripts/gate/loop_release.py",
        lambda: loop_release.judge_completion_loop_release(
            copy.deepcopy(sample_spec),
            {
                "completion_stop_blocks": 4,
                "completion_stall_blocks": 3,
                "loop_same_set_streak": 3,
                "loop_episode_id": "T1",
            },
            signal="same requirement keeps failing",
            recent="python3 scripts/generate_docs.py --check",
        ),
    )
    cases += _capture_call(
        "Completion handoff",
        "scripts/gate/completion_handoff.py",
        lambda: completion_handoff.judge_completion_handoff(
            '<record line="000001" role="assistant">Want me to read the transcript?</record>',
            user_goal="Analyze benchmark overhead.",
            last_text="Want me to read the transcript?",
            grade="STANDARD",
            recent_activity="ran: python3 benchmark/run.py",
        ),
    )
    return cases


def render_judge_prompts() -> str:
    lines = [
        "# Judge Prompts",
        "",
        "_Generated by `python3 scripts/generate_docs.py`._",
        "",
        "Each section is captured from the production call path with a synthetic request. The generator replaces `codex_judge.ask_structured` during capture, so this document is offline and does not call the live judge.",
        "",
    ]
    for case in collect_judge_prompts():
        lines.extend(
            [
                f"## {case.name}",
                "",
                f"Source: `{case.source}`",
                "",
                f"Schema name: `{case.schema_name}`",
                "",
                "### System",
                "",
                _fence(case.system, "text"),
                "",
                "### User",
                "",
                _fence(case.user, "json" if case.user.lstrip().startswith("{") else "text"),
                "",
                "### Function Schema",
                "",
                _fence(_json(case.schema), "json"),
                "",
                "### Realtime Request Shape",
                "",
                _fence(_json(case.transport), "json"),
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def render_all_docs() -> dict[str, str]:
    return {
        "claude-hookoutputs.md": render_hook_doc("claude"),
        "codex-hookoutputs.md": render_hook_doc("codex"),
        "judgeprompts.md": render_judge_prompts(),
    }


def write_docs(output_dir: Path = GENERATED_DIR) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name, text in render_all_docs().items():
        path = output_dir / name
        write_text_atomic(path, text)
        written.append(path)
    return written


def check_docs(output_dir: Path = GENERATED_DIR) -> tuple[bool, list[str]]:
    rendered = render_all_docs()
    problems: list[str] = []
    for name, expected in rendered.items():
        path = output_dir / name
        try:
            actual = path.read_text(encoding="utf-8")
        except OSError:
            problems.append(f"missing {path}")
            continue
        if actual != expected:
            problems.append(f"stale {path}")
    return not problems, problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if generated docs are missing or stale")
    parser.add_argument("--output-dir", default=str(GENERATED_DIR), help="directory for generated Markdown")
    args = parser.parse_args(argv)

    output_dir = Path(args.output_dir)
    if args.check:
        ok, problems = check_docs(output_dir)
        if ok:
            print("generated docs are current")
            return 0
        for problem in problems:
            print(problem, file=sys.stderr)
        print("run: python3 scripts/generate_docs.py", file=sys.stderr)
        return 1

    written = write_docs(output_dir)
    for path in written:
        print(path.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
