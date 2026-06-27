---
description: Set up unifable always-on (install the CLI + record state; all context ships via hooks, no CLAUDE.md/AGENTS.md blocks).
---

Run the unifable setup. Ask only once, up front.

## Step 1 — Ask whether/where to set up (one question)

Use AskUserQuestion. **Phrase the question and options in the user's current conversation language** (detect it from recent messages).
- **Question (meaning, translate to the user's language):** "Set up unifable?"
- **Options (meaning, translate):**
  1. "Local — this project only (recommended)"
  2. "Global — all projects"
  3. "Cancel"

If the user picks "Cancel", stop and do nothing.

## Step 2 — Run setup (no second prompt)

The user already consented in Step 1. For "Local" or "Global", run setup:

```bash
bash ${CLAUDE_PLUGIN_ROOT}/setup/setup.sh <local|global>
```

`setup.sh` is host-aware: it installs the unifable CLI entrypoints (`unifable`, `unifable-hook`, and the legacy `unifable-spec` alias) into `~/.local/bin`, strips any prior `<!-- UNIFABLE -->` / `<!-- UNIFABLE-ORCH -->` / `<!-- FABLIZE -->` static block from the host memory file (legacy migration cleanup — all context is now delivered by hooks, nothing is injected into CLAUDE.md/AGENTS.md), and writes `~/.unifable/progress.json`. Report the result briefly.
