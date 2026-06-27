# hooks - agent notes

## Scope

These rules apply to Claude/Codex hook entrypoints and hook wiring.

## Rules

- Hooks are the load-bearing enforcement layer. If behavior must happen, wire it
  through `hooks/hooks.json` or the appropriate host hook entrypoint.
- Hook scripts must fail open on malformed input or internal errors unless a
  user-facing safety cap explicitly handles the block.
- Keep host-specific IO and output-shape handling here; host-agnostic policy
  belongs in `scripts/gate/`.
- Hook output must match the host contract exactly. Avoid extra narration.

## Enforced gates (skippable vs forced)

| Mechanism | Type | Skippable? |
|---|---|---|
| Evidence gate (`pre_tool_use.py` + `scripts/gate/spec.py`) | PreToolUse | No — blocks edits/delegation/non-whitelisted research Bash until the spec validates |
| Groundedness breaker (`scripts/gate/groundedness.py`, wired in `pre_tool_use.py`) | PreToolUse | No — blocks mutation tools on an unproven confident claim |
| Completion gate (`gate_stop.py`) | Stop | No — blocks finishing without the evidence spec |

Optional grounding commands and verifier subagents are intentionally NOT shipped;
use these three for load-bearing behavior.

## Hook wiring

- `hooks/hooks.json` — the binding. Maps host events to gate scripts via
  `${CLAUDE_PLUGIN_ROOT}`. Adding a hook means adding it here, not just writing the
  script.
- `session_start.py` — SessionStart: refreshes the stable `~/.unifable` runtime,
  then injects the thin judge-relationship frame via `additionalContext`
  (`scripts/gate/context_block.py`): a director judge guides the model step by step
  and it restates the goal first; the per-tool director supplies step-by-step
  guidance at runtime. Ships only when the plugin is enabled; setup.sh / install
  scripts strip stale blocks.
- `pre_tool_use.py` — PreToolUse entrypoint: evidence gate + protected paths + the
  groundedness breaker, which doubles as the stepwise director (per-tool directive +
  tool scope, enforced via `scripts/gate/tool_scope.py`). Fail-open on malformed
  input by design.
- `gate_post_tool.py` — PostToolUse: logs real activity (read_paths, fetched_urls,
  ran_commands) and verification results into the ledger; the breaker's release gate
  and citation checks read this log.
- `gate_stop.py` — Stop: completion gate (spec present, verification ran,
  promise-no-act guard). On allow-stop emit `{}` (or `systemMessage` only for user
  escalations); inject the spec digest via `additionalContext` only when
  `decision: block` — Stop `additionalContext` re-engages the session on Claude Code.

## Verification

- Run targeted hook tests plus `python3 -m py_compile` on touched hook files.
- For output-shape changes, regenerate and check `docs/generated/`.
