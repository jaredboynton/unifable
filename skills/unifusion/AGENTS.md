# unifusion — agent notes

Maintainer notes for the **unifusion** skill itself. Runtime instructions live in `SKILL.md`.

## What it is

Unifusion is an **OpenCode serve + parallel attach** flow.

- The caller writes the user's question to a temp file.
- `scripts/unifusion.sh` builds a factual-only shared context brief when possible.
- That script starts **one** `opencode serve` daemon with the skill-local config
  (`opencode/opencode.json`), which is merged over the user's global OpenCode config (providers, auth, and
  the Exa MCP come from the user's global setup).
- It fans out the four architect agents as parallel `opencode run --attach` threads, one server session
  each. Fan-out is deterministic at the shell level; there is no root orchestrator spending reasoning tokens
  deciding to parallelize.
  - `architect` — GPT-5.5 (`openai-ws/gpt-5.5`)
  - `architect-opus` — Opus 4.8 (`anthropic/claude-opus-4-8`)
  - `architect-glm` — GLM-5.2 (`zai-coding-plan/glm-5.2`)
  - `architect-kimi` — Kimi K2.7 (`kimi-for-coding/k2p7`)
- Each thread's final assistant message is captured (via `opencode/parse_events.py`) as that panelist's
  report. Architects are read-only; the shell captures stdout rather than letting them write files.
- A final `unifusion-synth` (GPT-5.5) thread runs on the same daemon with every report **inlined into its
  prompt**, and returns `[FINAL]...[/FINAL]` plus `[ANALYSIS]...[/ANALYSIS]`. The shell parses those into
  `final.md`/`analysis.md` and persists provenance.

Gemini is not part of the active panel.

## Active files

- `scripts/unifusion.sh` — active entrypoint (serve + parallel attach + synth + cleanup)
- `opencode/opencode.json` — skill-local OpenCode config: secret-free `openai-ws/gpt-5.5` provider block plus
  the 5 agents (4 read-only architects + `unifusion-synth`), each pinned to its model with a `{file:...}`
  prompt reference resolved relative to the config
- `opencode/architect_prompt.md` — shared frontier-research architect prompt (returns the report as its final
  message; does not write files)
- `opencode/synth_prompt.md` — synthesis prompt (reads inlined reports, emits FINAL/ANALYSIS)
- `opencode/parse_events.py` — extracts the final assistant text from an `opencode run --format json` NDJSON
  event stream (groups `type=="text"` parts by messageID, takes the last message)
- `scripts/resolve_session.sh` — host-agnostic transcript resolver
- `scripts/summarize_session.sh` — best-effort factual session brief
- `scripts/compact-full-transcript.mjs` — transcript compaction / summarization engine
- `scripts/save_run.sh` — provenance writer

## Archived paths

- `scripts/archive/unifusion_droid.sh` — the Droid-native (`droid exec` root orchestrator) entrypoint
- `scripts/archive/unifusion_parallel_cli.sh` — the pre-Droid multi-CLI fan-out entrypoint

Legacy per-CLI runner scripts remain in `scripts/` for reference and are **not** on the active path:
`run_claude.sh`, `run_codex.sh`, `run_gemini.sh`, `run_kimi.sh`, `run_glm.sh`, `run_agy.sh`.

## Hard-won OpenCode facts (do not relearn these the slow way)

- `opencode run` **hangs at `init`** unless stdin is redirected. The script pipes the prompt file on stdin
  (`<"$panel_prompt"`), which both feeds the message and satisfies the stdin requirement.
- `OPENCODE_CONFIG` **merges** with the user's global config; it does not replace it. Auth and the Exa MCP
  are inherited from global, so the skill config does not (and must not) hardcode the Exa API key.
- `opencode run --attach <url>` requires a **pre-created session**: `POST <url>/session` returns `{id}`,
  passed as `--session <id>`. Attach will not auto-create one ("Session not found").
- `--format json` output is **NDJSON**; assistant prose is in events with `type=="text"` and
  `part.type=="text"`. Reasoning/tool/step events are separate.
- The synth agent **cannot read files outside the repo cwd** (opencode auto-rejects `external_directory` in
  headless mode). Reports are inlined into the synth prompt instead of passed as paths.
- `opencode serve` spawns `opencode acp` worker children that get orphaned on client exit. Cleanup snapshots
  pre-existing opencode PIDs and kills only the new ones, so any ambient opencode daemon the user runs is
  left alone.

## Constraints

- Keep the shared context **factual only**. No proposed approach belongs in the brief.
- Keep the user's task **verbatim**.
- Keep the active panel defined through the OpenCode agents in `opencode/opencode.json`, not hardcoded CLI
  runners.
- Keep GPT-5.5 as the default synthesis model.
- Prefer Exa-backed and primary-source research paths in the architect prompt.
- Do not reintroduce Gemini into the active panel unless its role is intentionally restored.
- Do not store secrets in the skill config or prompts (Exa comes from the user's global config).

## Testing

- `bash -n scripts/*.sh`
- `node --check scripts/compact-full-transcript.mjs`
- `uvx ruff check opencode/parse_events.py`
- `bash scripts/selfcheck.sh`

`bash scripts/unifusion.sh /tmp/q.md /tmp/ufrun` is the real smoke test, but it performs paid model calls.
Run it **synchronously** (do not detach it across a tool-call boundary, or the daemon's process group gets
killed mid-run). `UNIFUSION_AGENTS="architect-glm:glm5.2:"` runs a single cheap architect for pipeline checks.

## Safe-change rules

- `SKILL.md` and this file should describe only the **current** active behavior.
- If the active path changes, archive the old one under `scripts/archive/` instead of leaving two
  "current" entrypoints.
- Do not widen provenance writes beyond `${UNIFABLE_DATA:-~/.unifable}/unifusion-runs/`.
