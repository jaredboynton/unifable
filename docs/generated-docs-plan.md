# Generated Docs Plan

This repository exposes model-visible hook output and judge prompt surfaces through generated Markdown.

## Outputs

- `docs/generated/claude-hookoutputs.md` lists Claude hook registrations and rendered model-visible hook payloads.
- `docs/generated/codex-hookoutputs.md` lists Codex hook registrations and rendered model-visible hook payloads.
- `docs/generated/judgeprompts.md` lists each judge prompt captured from the production call path, including system text, user text, function schema, and Realtime request shape.

## Source Of Truth

- Hook registrations come from `hooks/hooks.json` for Claude and `.codex-plugin/hooks.json` for Codex.
- Hook output examples are rendered by `scripts/generate_docs.py` from the production hook helpers where possible.
- Judge prompt examples are captured by `scripts/generate_docs.py` by temporarily replacing `codex_judge.ask_structured` and calling the production judge entrypoints offline.
- Realtime transport examples use `codex_judge.render_structured_request`, the same event renderer used by `codex_judge.ask_structured`.

## Commit-Time Refresh

- `.pre-commit-config.yaml` defines a local `generate-docs` hook that runs `python3 scripts/generate_docs.py`, checks the output with `python3 scripts/generate_docs.py --check`, and stages the generated Markdown.
- `scripts/pre-commit-generated-docs.sh` regenerates the Markdown, runs `python3 scripts/generate_docs.py --check`, and stages the generated files when used from a Git hook.
- `scripts/commit.sh` runs the same refresh before its compile and test gate, then stages the generated Markdown in clean release runs.
- `just generated-docs` is the manual refresh command.

## Verification

- `python3 -m pytest tests/test_generate_docs.py -q` checks hook registration coverage, judge schema coverage, host-specific Stop rendering, deterministic rendering, and write/check round trips.
- `python3 scripts/generate_docs.py --check` fails when the checked-in Markdown differs from the renderer.
- `pre-commit run --all-files` proves the commit-time hook path runs successfully.
