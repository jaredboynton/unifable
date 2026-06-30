# unifusion — agent notes

Maintainer notes for the **unifusion** skill itself. Runtime instructions live in `SKILL.md`.

## What it is

Unifusion is now a **single-root Droid orchestration flow**.

- The caller writes the user's question to a temp file.
- `scripts/unifusion.sh` builds a factual-only shared context brief when possible.
- That script launches **one** `droid exec` root run.
- The root Droid uses Task to fan out the architect droids in parallel:
  - `architect`
  - `architect-opus`
  - `architect-glm`
  - `architect-kimi`
- The root Droid then reads the architect reports, synthesizes them directly, returns the final answer, and
  the shell script persists analysis/final/provenance artifacts.

Gemini is not part of the active Droid-native panel.

## Active files

- `scripts/unifusion.sh` — active entrypoint
- `scripts/resolve_session.sh` — host-agnostic transcript resolver
- `scripts/summarize_session.sh` — best-effort factual session brief
- `scripts/compact-full-transcript.mjs` — transcript compaction / summarization engine
- `scripts/save_run.sh` — provenance writer

## Archived path

- `scripts/archive/unifusion_parallel_cli.sh` — the pre-Droid multi-CLI fan-out entrypoint, kept only for
  reference

Legacy runner scripts remain in `scripts/` for reference:

- `run_claude.sh`
- `run_codex.sh`
- `run_gemini.sh`
- `run_kimi.sh`
- `run_glm.sh`
- `run_agy.sh`

They are **not** on the active Unifusion path.

## Architecture

The active shell path is:

1. Validate question file and create a run directory.
2. Build `context.md` with `summarize_session.sh` when possible.
3. Build `panel_prompt.md` with factual context plus the verbatim task.
4. Build a `droid_prompt.md` that instructs the root Droid to:
   - read the panel prompt
   - launch the architect droids in parallel via Task
   - read the resulting architect reports
   - return `[FINAL]...[/FINAL]` plus `[ANALYSIS]...[/ANALYSIS]`
5. Parse the root Droid's JSON result into `final.md` and `analysis.md`.
7. Save provenance with `save_run.sh`.

## Constraints

- Keep the shared context **factual only**. No proposed approach belongs in the brief.
- Keep the user's task **verbatim**.
- Keep the active panel defined through custom droids, not hardcoded external CLI runners.
- Prefer Exa-backed and primary-source research paths in the architect droids.
- Do not reintroduce Gemini into the active panel unless its role is intentionally restored.

## Testing

- `bash -n scripts/*.sh`
- `node --check scripts/compact-full-transcript.mjs`
- `bash scripts/selfcheck.sh`

`bash scripts/unifusion.sh /tmp/q.md /tmp/ufrun` is the real smoke test, but it performs paid model calls.

## Safe-change rules

- `SKILL.md` and this file should describe only the **current** active behavior.
- If the active path changes, archive the old one under `scripts/archive/` instead of leaving two
  "current" entrypoints.
- Do not widen provenance writes beyond `${UNIFABLE_DATA:-~/.unifable}/unifusion-runs/`.
