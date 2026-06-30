# unifusion

**Run one Droid root orchestrator that launches a panel of frontier-research architect droids in parallel,
then synthesizes them into one evidence-backed recommendation.**

Unifusion is now a **Droid-native panel-and-synthesis harness** in the unifable family. The active path is
no longer "spawn a pile of external CLIs and have the current Claude session judge them." Instead, one
`droid exec` root session handles orchestration, parallel subagents, synthesis, and final output.

## Active flow

| Stage | Artifact | Role |
|---|---|---|
| Resolve brief | `resolve_session.sh` → `summarize_session.sh` → `compact-full-transcript.mjs` | best-effort factual-only session brief |
| Build prompt | `scripts/unifusion.sh` | writes one canonical `panel_prompt.md` with context + verbatim task |
| Orchestrate | `droid exec` root run | launches architect droids via Task in parallel, then synthesizes them in the root session |
| Architect panel | `architect`, `architect-opus`, `architect-glm`, `architect-kimi` | independent frontier-research reads on the same task |
| Save | `save_run.sh` | provenance bundle under `~/.unifable/unifusion-runs/` |

## Active panel

| Droid | Backing model | Purpose |
|---|---|---|
| `architect` | GPT-5.5 | frontier-research architecture read |
| `architect-opus` | Opus 4.8 | frontier-research architecture read |
| `architect-glm` | GLM-5.2 | frontier-research architecture read |
| `architect-kimi` | Kimi K2.7 | frontier-research architecture read |

Gemini is not part of the active panel.

## Entry point

```bash
bash scripts/unifusion.sh /tmp/unifusion_question.txt
```

The script prints a manifest with:

- `RUN_DIR`
- `PANEL_PROMPT`
- `ANALYSIS`
- `FINAL`
- `PROVENANCE`
- one `PANELIST ...` line per architect report

## Notes

- The session brief is still **factual state only**.
- The user's task is still passed **verbatim**.
- The old multi-CLI fan-out entrypoint is archived at `scripts/archive/unifusion_parallel_cli.sh`.
- Legacy runner scripts such as `run_claude.sh`, `run_codex.sh`, `run_gemini.sh`, `run_kimi.sh`, and `run_glm.sh`
  are retained for reference, not on the active Unifusion path.
