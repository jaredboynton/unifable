---
name: unifusion
description: >-
  Answer a hard technical question by running one Droid root orchestrator that launches a panel of
  frontier-research architect droids in parallel, then synthesizes their findings into one evidence-backed
  final answer. The active panel is GPT-5.5, Opus 4.8, GLM-5.2, and Kimi K2.7 via custom droids
  (`architect`, `architect-opus`, `architect-glm`, `architect-kimi`). The root Droid synthesizes their
  reports directly and the script also writes analysis/final artifacts plus a timestamped provenance record.
  Use whenever the user asks to "run it through Unifusion", wants a multi-model / panel / ensemble answer,
  or wants the best current approach grounded in code evidence, official docs, flagship GitHub repos,
  benchmarks, or research papers.
---

# Unifusion

Unifusion now runs through **one `droid exec` root session**. That root droid reads the verbatim task,
launches several **frontier-research architect droids in parallel**, waits for their reports, synthesizes
them in the root session, and returns a final answer. The current host session no longer hand-judges the
panel afterward; the synthesis happens inside the Droid run.

The active architect panel is:

- `architect` — GPT-5.5
- `architect-opus` — Opus 4.8
- `architect-glm` — GLM-5.2
- `architect-kimi` — Kimi K2.7

Gemini is not part of the active Droid-native panel.

Throughout, `<skill_dir>` is the directory containing this `SKILL.md`.

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
- launches **one** Droid root orchestrator
- has that root orchestrator fan out the architect droids with the same task
- synthesizes the architect reports in the root Droid response
- writes:
  - `analysis.md`
  - `final.md`
  - a provenance markdown record under `~/.unifable/unifusion-runs/`

The script prints a manifest. The lines you care about are:

```text
RUN_DIR=/tmp/unifusion-panel.XXXXXX
PANEL_PROMPT=/.../panel_prompt.md
ANALYSIS=/.../analysis.md
FINAL=/.../final.md
PROVENANCE=/.../2026-..._droidexec-....md
PANELIST gpt5.5 ok /Users/.../.factory/reviews/.../architect.md
PANELIST opus4.8 ok /Users/.../.factory/reviews/.../architect-opus.md
...
```

## Step 3 — Present the result

Read `FINAL=` and present that answer. Use `ANALYSIS=` when you need the deeper audit trail. If one or more
panelists were dropped, say so explicitly instead of treating absence as agreement.

## Notes

- The shared session brief is still **state only**, not a proposed solution.
- The old multi-CLI fan-out script is archived at `scripts/archive/unifusion_parallel_cli.sh`.
- The Droid-native path depends on the user's configured custom droids and Factory models.
