# AGENTS.md — unifable

Development guide for agents working ON the unifable plugin. This repo IS the
harness: hooks + skills that force grounded, evidence-gated behavior on Claude
Code and Codex. Product overview and the full hook table live in
[README.md](README.md) — do not duplicate them here.

## Prime directive: enforced behavior ships as a hook, never a skill

The orchestrator is an LLM. Anything left to its discretion can be skipped, and
under load it will be. Therefore:

- Every behavior that MUST happen ships as a deterministic hook wired in
  `hooks/hooks.json` (UserPromptSubmit / PreToolUse / PostToolUse / Stop). A hook
  runs on the host's critical path and returns a blocking exit code; the model
  cannot route around it.
- Skills and subagents are ADVISORY only. The orchestrator MAY invoke them and
  MAY skip them. They MUST NOT be the sole mechanism enforcing anything
  load-bearing.
- If a behavior matters, it MUST be forced — not optional, not circumventable,
  not "the model should run `/x` first." Build it as a hook or accept that it
  will not happen.

Worked contrast:

| Mechanism | Type | Skippable? |
|---|---|---|
| Evidence gate (`hooks/pre_tool_use.py` + `scripts/gate/spec.py`) | PreToolUse hook | No — blocks edits/delegation/non-whitelisted research Bash until the spec validates |
| Groundedness breaker (`scripts/gate/groundedness.py`, wired in `pre_tool_use.py`) | PreToolUse hook | No — blocks mutation tools on an unproven confident claim |
| Completion gate (`hooks/gate_stop.py`) | Stop hook | No — blocks finishing without the evidence spec |

Optional grounding commands and verifier subagents are intentionally not part of
the shipped harness. Use the enforced evidence gate, groundedness breaker, and
completion gate for load-bearing behavior.

## How enforcement is wired

- `hooks/hooks.json` — the binding. Maps host events to gate scripts via
  `${CLAUDE_PLUGIN_ROOT}`. Adding a hook means adding it here, not just writing
  the script.
- `hooks/pre_tool_use.py` — the PreToolUse entrypoint: evidence gate + protected
  paths + the groundedness breaker. Fail-open on malformed input by design.
- `hooks/gate_post_tool.py` — PostToolUse: logs real activity (read_paths,
  fetched_urls, ran_commands) and verification results into the ledger. The
  breaker's release gate and citation checks read this log.
- `hooks/gate_stop.py` — Stop: completion gate (spec present, verification ran,
  promise-no-act guard). On allow-stop, emit `{}` (or `systemMessage` only for user
  escalations); inject the spec digest via `additionalContext` only when `decision: block`
  — Stop `additionalContext` re-engages the session on Claude Code.
- `scripts/gate/` — host-agnostic core, no host imports:
  `spec.py` (evidence spec validate), `ledger.py` (per-session state),
  `citations.py` (cite-vs-activity check), `groundedness.py` (arm/disarm judge),
  `codex_judge.py` (gpt-realtime-2 client), `classify_task.py`,
  `bash_classify.py`, `parse_tool_result.py`, `verify_state.py`.

## Commands

```bash
# full gate suite is run by pre-commit; do not run these individually before commit
# unless debugging a specific failing check.

# a single gate's tests
python3 -m pytest tests/test_groundedness_breaker.py -q

# compile-check the hot path before committing
python3 -m py_compile hooks/pre_tool_use.py scripts/gate/groundedness.py scripts/gate/ledger.py

# bump the plugin version everywhere (all 4 plugin dirs + setup/setup.sh)
just version 1.9.62          # or: just version patch|minor|major

# Session env probe: validate that the shell subprocess receives the
# same session id as the hook/prompt scaffold (see resolve_session_id).
# Compare UNIFABLE_SESSION_RESOLVED from `where` against the host env vars.
UNIFABLE_DEV=1 unifable where 2>&1 ; echo '---ENV---' ; env | grep -E 'CLAUDE_CODE_SESSION_ID|CODEX_THREAD_ID|CURSOR_CONVERSATION_ID|CURSOR_SESSION_ID' || true
```

## Conventions

- New gate logic MUST land with failing-first tests under `tests/` and MUST NOT
  weaken or delete an existing protected test to make a suite pass.
- Gate scripts MUST fail open: any internal error leaves tools unblocked. A gate
  that hard-locks a session on its own bug is worse than no gate. The breaker's
  safety cap (`BREAKER_MAX_BLOCKS`) is the pattern — bound every enforcement loop.
- `scripts/gate/` MUST stay host-agnostic (no Claude-only or Codex-only imports);
  host wiring lives in `hooks/` and `install/`.
- Version bumps touch ALL manifests together:
  `.claude-plugin/`, `.codex-plugin/`, `.devin-plugin/`, `.factory-plugin/`
  (`plugin.json` + `marketplace.json`) and `setup/setup.sh`. Do not hand-edit
  them: run `just version <X.Y.Z>` (or `just version patch|minor|major`), which
  sets every version field in one pass via `scripts/bump_version.py` and exits
  nonzero if any straggler of the old version remains in the managed set.
- No emojis anywhere (output, commits, code, comments, docs).

## Where to look

| Topic | Path |
|---|---|
| Product overview, hook table | [README.md](README.md) |
| Evidence-gate design | [docs/evidence-gate-design.md](docs/evidence-gate-design.md) |
| Roadmap | [docs/unifable-v2-plan.md](docs/unifable-v2-plan.md) |
| Eval rubric + scenarios | [docs/evals/](docs/evals/), [tests/eval_rubric.md](tests/eval_rubric.md) |
| Subagent brief / output contract | [packs/subagent-brief.md](packs/subagent-brief.md), [packs/output-contract.txt](packs/output-contract.txt) |
| Investigation protocol | [packs/investigation-protocol.txt](packs/investigation-protocol.txt) |
