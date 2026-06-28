# Explore scripts/lib - agent notes

## Scope

These rules apply to shared runtime helpers under `scripts/lib/`. Keep wrapper
and top-level orchestration notes in `scripts/AGENTS.md`.

## Architecture

- `realtime_client.mjs` is the low-level Realtime WebSocket client.
- `rt-agent-session.mjs` owns hot-session reuse, prewarm, pruning, and reconnect
  recovery.
- `rt-pick-passages.mjs` and `rt-rehydrate-submit.mjs` implement pointer submit
  and passage rehydration.
- `rt-web-run.mjs` and `rt-web-run-tools.mjs` bridge Realtime function calls to
  Codex alpha/search.
- `trace-schema.mjs` and `websearch-schema.mjs` define structured output
  validation.

## AST and Rehydration Rules

- Use `expandLineRange(absPath, startLine, endLine)` for code windows that
  should snap to enclosing syntax nodes.
- Pass meaningful matched ranges into `expandLineRange`; do not collapse a
  multi-line evidence cluster to one guessed anchor before AST expansion.
- Clamp hydrated windows after AST expansion so a large enclosing node cannot
  flood the model context.
- Keep comment stripping configurable through the existing `UNITRACE_*_STRIP_*`
  environment switches.

## Realtime Helper Rules

- Reuse `RtAgentSession` for hot socket, prewarm, and single reconnect retry
  behavior instead of creating one-off WebSocket loops.
- Keep `finish` or `submit` tools in required-tool loops so
  `tool_choice: "required"` always has a valid call target.
- Preserve structured submit validation and reask behavior when changing
  `trace-schema.mjs`, `websearch-schema.mjs`, or submit helpers.
- Use Codex alpha/search through the existing web-run helpers; do not add a new
  Responses submit transport for trace without a benchmark-backed plan.

## Verification

- AST changes: run `node --test scripts/test/ast-context.test.mjs scripts/test/rt-pick-passages.test.mjs`.
- Session changes: run `node --test scripts/test/rt-agent-session.test.mjs scripts/test/realtime-frame.test.mjs`.
- Submit/schema changes: run the matching `trace-schema`, `websearch-schema`,
  and `rt-rehydrate-*` tests under `scripts/test/`.
