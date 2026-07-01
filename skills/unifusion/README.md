# unifusion

**Run a panel of frontier-research architect agents in parallel on one warm OpenCode daemon, then
synthesize them into one evidence-backed recommendation.**

Unifusion is an **OpenCode panel-and-synthesis harness** in the unifable family. `scripts/unifusion.sh`
starts a single `opencode serve` daemon, fans out the architects as parallel `opencode run --attach`
threads (one session each), captures each thread's final message as that panelist's report, and runs one
`unifusion-synth` thread to merge them. Fan-out is deterministic at the shell level; there is no root
orchestrator spending reasoning tokens deciding to parallelize.

## Active flow

| Stage | Artifact | Role |
|---|---|---|
| Resolve brief | `resolve_session.sh` -> `summarize_session.sh` -> `compact-full-transcript.mjs` | best-effort factual-only session brief |
| Build prompt | `scripts/unifusion.sh` | writes one canonical `panel_prompt.md` with context + verbatim task |
| Serve | `opencode serve` (skill-local `opencode/opencode.json`) | one warm headless daemon for the whole run |
| Architect panel | parallel `opencode run --attach --auto` threads | independent frontier-research reads on the same task |
| Synthesize | `unifusion-synth` thread with reports inlined into its prompt | merges the panel into `ANALYSIS` + `FINAL` |
| Save | `save_run.sh` | provenance bundle under `~/.unifable/unifusion-runs/` |

## Active panel

| Agent | Backing model | Purpose |
|---|---|---|
| `architect` | GPT-5.5 (`openai-ws/gpt-5.5`) | frontier-research architecture read |
| `architect-opus` | Opus 4.8 on Bedrock (`amazon-bedrock/us.anthropic.claude-opus-4-8`) | frontier-research architecture read |
| `architect-glm` | GLM-5.2 (`zai-coding-plan/glm-5.2`) | frontier-research architecture read |
| `architect-kimi` | Kimi K2.7 (`kimi-for-coding/k2p7`) | frontier-research architecture read |

Synthesis is `unifusion-synth` (GPT-5.5). Gemini is not part of the active panel.

## Entry point

```bash
bash scripts/unifusion.sh /tmp/unifusion_question.txt
```

The script prints a manifest with:

- `RUN_DIR`, `SERVER_URL`, `SLUG`
- `PANEL_PROMPT`, `SYNTH_PROMPT`
- `ANALYSIS`, `FINAL`, `PROVENANCE`
- one `PANELIST <label> ok|dropped:<reason> <report>` line per architect

## Notes

- The session brief is **factual state only**; the user's task is passed **verbatim**.
- Every architect and the synth thread run with `--auto`, so read/grep/webfetch/external_directory requests
  that are not explicitly denied are approved. Without it, headless attach runs auto-reject cross-repo reads
  and tool-heavy panelists (notably Opus) stall out with no final answer.
- Opus routes through Bedrock. Override the provider/model without editing the config via
  `UNIFUSION_OPUS_MODEL` (default `amazon-bedrock/us.anthropic.claude-opus-4-8`).
- When a panelist drops, the manifest names the cause (`dropped:timeout`, `dropped:exit-N`,
  `dropped:empty-events`, `dropped:error:<code>`, `dropped:parse-empty`) instead of an undifferentiated
  `missing`, and the dropped panelist's log tail is echoed to stderr.
- The old Droid-native entrypoint is archived at `scripts/archive/unifusion_droid.sh`; the pre-Droid
  multi-CLI fan-out is at `scripts/archive/unifusion_parallel_cli.sh`. Legacy runner scripts (`run_claude.sh`,
  `run_codex.sh`, `run_gemini.sh`, `run_kimi.sh`, `run_glm.sh`) are retained for reference, not on the active
  path.
