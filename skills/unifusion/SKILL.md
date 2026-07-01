---
name: unifusion
description: >-
  Answer a hard technical question by running a panel of frontier-research architects in parallel on one
  warm OpenCode daemon, then synthesizing their findings into one evidence-backed final answer. The active
  panel is GPT-5.5, Opus 4.8, GLM-5.2, and Kimi K2.7, each run as its own `opencode run --attach` thread
  against a single `opencode serve`; a final GPT-5.5 synthesis thread reads every report and returns the
  answer. The script also writes analysis/final artifacts plus a timestamped provenance record. Use whenever
  the user asks to "run it through Unifusion", wants a multi-model / panel / ensemble answer, or wants the
  best current approach grounded in code evidence, official docs, flagship GitHub repos, benchmarks, or
  research papers.
---

# Unifusion

Unifusion runs a frontier-research architecture panel on **OpenCode**. One `opencode serve` daemon is started
with a skill-local config; the four architect agents each run as their own parallel `opencode run --attach`
thread (deterministic shell-level fan-out, one warm daemon, no root-orchestrator reasoning tax); a final
`unifusion-synth` thread reads every architect report and returns the answer. The daemon is killed when the
run finishes.

The active architect panel is:

- `architect` — GPT-5.5 (`openai-ws/gpt-5.5`, variant medium)
- `architect-opus` — Opus 4.8 (`anthropic/claude-opus-4-8`)
- `architect-glm` — GLM-5.2 (`zai-coding-plan/glm-5.2`)
- `architect-kimi` — Kimi K2.7 (`kimi-for-coding/k2p7`)

Synthesis is `unifusion-synth` — GPT-5.5. Gemini is not part of the active panel.

Throughout, `<skill_dir>` is the directory containing this `SKILL.md`.

## Prerequisites

- `opencode` CLI installed and authenticated (`~/.local/share/opencode/auth.json`) for the openai-ws,
  anthropic, zai-coding-plan, and kimi-for-coding providers.
- The skill config (`<skill_dir>/opencode/opencode.json`) is merged over the user's global OpenCode config,
  so the Exa MCP and provider auth come from the user's global setup.

## Step 1 — Write the question, verbatim

Write the user's request **exactly as asked** to a temp file under `/tmp/`. Do not summarize, rewrite, or
pre-digest it.

```bash
cat > /tmp/unifusion_question.txt <<'EOF'
<the user's question, verbatim>
EOF
```

## Step 2 — Run Unifusion

```bash
bash <skill_dir>/scripts/unifusion.sh /tmp/unifusion_question.txt
```

That one command:

- builds a best-effort **factual-only** session brief when a current host transcript can be resolved
- assembles the shared `panel_prompt.md`
- starts one warm `opencode serve` daemon (skill-local config)
- fans out the four architect agents as parallel `opencode run --attach` threads, one session each, and
  captures each thread's final message as its report
- runs one `unifusion-synth` thread on the same daemon that reads the inlined reports and returns
  `[FINAL]`/`[ANALYSIS]`
- kills the daemon (and any `opencode acp` workers it spawned) and writes:
  - `analysis.md`
  - `final.md`
  - a provenance markdown record under `~/.unifable/unifusion-runs/`

The script prints a manifest. The lines you care about are:

```text
RUN_DIR=/tmp/unifusion-panel.XXXXXX
PANEL_PROMPT=/.../panel_prompt.md
ANALYSIS=/.../analysis.md
FINAL=/.../final.md
PROVENANCE=/.../2026-..._opencode-....md
PANELIST gpt5.5 ok /.../reports/architect.md
PANELIST opus4.8 ok /.../reports/architect-opus.md
...
```

## Step 3 — Present the result

Read `FINAL=` and present that answer. Use `ANALYSIS=` when you need the deeper audit trail. If one or more
panelists were dropped, say so explicitly instead of treating absence as agreement.

## Notes

- The shared session brief is still **state only**, not a proposed solution.
- The architects are read-only (no write/edit/patch/bash); the shell captures each thread's final message as
  its report. The synth thread reads the reports **inlined into its prompt** because OpenCode auto-rejects
  reads outside the repo cwd in headless mode.
- Reasoning effort knobs: `UNIFUSION_ARCH_TIMEOUT` (per-architect seconds, default 900),
  `UNIFUSION_SYNTH_TIMEOUT` (default 600), `UNIFUSION_AGENTS` (comma list of `agent:label:variant` to
  override the panel), `UNIFUSION_SAVE_RUN=0` to skip provenance.
- The old Droid-native entrypoint is archived at `scripts/archive/unifusion_droid.sh`; the pre-Droid
  multi-CLI fan-out is at `scripts/archive/unifusion_parallel_cli.sh`.
