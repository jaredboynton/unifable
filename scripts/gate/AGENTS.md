# scripts/gate - agent notes

## Scope

These rules apply to host-agnostic gate policy, judge clients, ledger state, and
runtime helpers.

## Rules

- Keep this package host-agnostic. Claude/Codex-specific IO belongs in `hooks/`
  or install/setup code.
- Gate internals must fail open on their own bugs and bound enforcement loops
  with explicit caps.
- State writes go through the existing SQLite/WAL helpers or atomic file helpers;
  do not add ad hoc persistence.
- Judge prompts, hook copy, and router text are part of model interaction
  surface; keep them concise, concrete, and covered by tests or generated docs.

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
