# scripts/gate - agent notes

## Scope

These rules apply to host-agnostic gate policy, judge clients, ledger state, and
runtime helpers.

## Core files (host-agnostic, no host imports)

- `db.py` — the single WAL SQLite store backing ledger/breaker/spec/findings;
  fail-open, BEGIN IMMEDIATE writes, lazy one-time JSON import.
- `spec.py` — evidence spec validate. `ledger.py` — per-session state, shimmed
  over `db.py`.
- `citations.py` — cite-vs-activity check. `groundedness.py` — arm/disarm judge,
  doubling as the stepwise director.
- `codex_judge.py` — gpt-realtime-2 client. `classify_task.py`, `bash_classify.py`,
  `parse_tool_result.py`, `verify_state.py`.
- `file_refs.py` — FILE INDEX pointer + rehydrate for judge directive/steering.

No Realtime fallback is wired today (maybe Bedrock `nvidia.nemotron-nano-3-30b`
later — 256K context, cheap on-demand). The judge needs Codex OAuth + Realtime now.

## Rules

- Keep this package host-agnostic. Claude/Codex-specific IO belongs in `hooks/`
  or install/setup code.
- New gate logic MUST land with failing-first tests under `tests/` and MUST NOT
  weaken or delete an existing protected test to make a suite pass.
- Gate internals must fail open on their own bugs and bound enforcement loops
  with explicit caps.
- State writes go through the existing SQLite/WAL helpers or atomic file helpers;
  do not add ad hoc persistence.
- Judge prompts, hook copy, and router text are part of model interaction
  surface; keep them concise, concrete, and covered by tests or generated docs.
- Whenever a verbatim value (file path, command, symbol, identifier) must reach the
  model losslessly, use the pointer + rehydrate pattern, never a model-typed string:
  hand the model a numbered index of the real values, have it reference one by
  integer pointer in its structured output, then rehydrate the exact value
  host-side. Models truncate and paraphrase long identifiers; an integer pointer
  cannot. See `file_refs.py` and the explore READ INDEX / `excerpt_index`
  (`skills/explore/scripts/lib/rt-rehydrate-submit.mjs`).
- gpt-realtime-2 hard-caps each Realtime `input_text` field at 256,000 chars
  (char-driven, not token-driven; validated live: 255,900 chars OK, 256,100 rejected
  with `string_above_max_length`). Enforced client-side by `JUDGE_MAX_MESSAGE_CHARS`
  + `cap_judge_message` in `transcript_tail.py`; oversized payloads surface as a
  structured `error`/`response.failed` frame (handled in `codex_judge._ask_once`),
  not a socket drop. Do not raise this cap.

## Verification

- Run focused tests for touched modules and `python3 -m py_compile` on edited
  Python files.
- Run `just test-all` before release.

## Judge transcript compression

`transcript_tail.py` renders session JSONL for gpt-realtime-2 judges using the patchpress
`tool-use-format` semantics (compact Edit/Write diffs) plus age-based compression on old tool
outputs and formatted edits. Knobs (all optional):

| Env | Default | Meaning |
|---|---|---|
| `UNIFABLE_JUDGE_TOOL_OUTPUT_STRATEGY` | `mask` | `headtail`, `dspc`, or `mask` for old tool outputs |
| `UNIFABLE_JUDGE_TOOL_OUTPUT_KEEP_RECENT` | `64` | Recent records left uncompressed |
| `UNIFABLE_JUDGE_TOOL_OUTPUT_MIN_CHARS` | `2400` | Minimum body size before compression |
| `UNIFABLE_JUDGE_TOOL_USE_COMPRESS_MIN_CHARS` | `800` | Minimum formatted edit size before re-compress |
| `UNIFABLE_JUDGE_TRANSCRIPT_CWD_PREFIX` | (unset) | Strip absolute paths in edit headers |

Implementation: `tool_use_format.py`, `tool_output_compress.py`.
