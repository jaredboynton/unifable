---
name: unifable
description: A harness that makes the model behave like Fable — it enforces seeing a task through to the end, with evidence and verification, as procedure. Use when starting a multi-step task (2+ sequential stories), long autonomous work, debugging or root-cause investigation, building render/executable artifacts (HTML, SVG, games, charts), or when the user says "unifable", "see it through", "verify as you go", "split into goals".
---

# unifable — run the model like Fable

> Principle: a harness cannot raise a model's ceiling. It makes the model go all the way to its own ceiling — by enforcing verification, completion, and investigation as procedure. When the capability ceiling is the blocker (open-ended creative detail, self-driven discovery), escalate (§4).
>
> Apply only what the task signals (smallest matching discipline; overlap only when genuinely multi-category). When installed always-on, this routing is automatic.

## 0. First run — set up automatically (once)

Before doing the requested task, check whether unifable has been onboarded on this machine:

```bash
cat ~/.unifable/progress.json 2>/dev/null
```

- If the file **exists** — skip onboarding, go straight to the task.
- If it is **missing** — onboard once with a single question to the user. **Phrase the question and options in the user's current conversation language** (detect it from recent messages — Korean, English, Japanese, etc.).
  - **Question (meaning, translate to the user's language):** "Set up unifable?"
  - **Options (meaning, translate):** "Local — this project only (recommended)" / "Global — all projects" / "Skip".
  - On **Local/Global** — run setup (it injects the operating block into AGENTS.md, writes progress.json — all in one), then continue with the task:
    ```bash
    bash ~/.codex/skills/unifable/setup/setup.sh <local|global>
    ```
  - On **Skip** — record it so it won't ask again, then continue:
    ```bash
    mkdir -p ~/.unifable && printf '{"setup_done":false,"skipped":true}' > ~/.unifable/progress.json
    ```

This means the user can just invoke `$unifable` (or trigger it) without running setup first — the first run onboards itself, once, with one question.

## 1. Multi-story loop (2+ sequential stories)

Decompose into sequential stories and complete one at a time, producing evidence as you go. Self-contained — no external goal system required. Run from the repo root; state persists in `./.unifable/` (resume with `status` even across sessions).

```bash
python3 ~/.codex/skills/unifable/scripts/goals.py create --brief "<summary>" \
  --goal "title::verifiable objective" --goal "title::..."   # the last goal must be a verification story
python3 ~/.codex/skills/unifable/scripts/goals.py next         # activate a story + handoff
# ... work that story only ...
python3 ~/.codex/skills/unifable/scripts/goals.py checkpoint --id G001 --status complete --evidence "<concrete evidence>"
# the final story is a verification gate: --verify-cmd "<command>" --verify-evidence "<result>" are required
python3 ~/.codex/skills/unifable/scripts/goals.py status       # first command when resuming
```

Rules: `complete` requires non-empty evidence; the final goal cannot complete without a verify command and its result (the engine refuses). If blocked, record `--status blocked` and report. Single-step tasks skip this loop.

## 2. Deep investigation (debugging / unknown cause / review)

Read and follow `~/.codex/skills/unifable/packs/investigation-protocol.txt`: reproduce first → form 3+ competing hypotheses → gather evidence per hypothesis → trace the full causal chain (removing the symptom is not removing the defect) → verify before and after → report the hypotheses you rejected. For reviews, report everything including low-confidence findings and filter in a separate step.

## 3. Verification grounding (render/executable artifacts — always)

For artifacts whose correctness only shows when run (HTML, SVG, games, UI, charts), follow `~/.codex/skills/unifable/packs/verification-grounding-pack.txt`: run it in the real renderer → observe the actual output → fix what the observation reveals → re-run. A static parse confirms well-formed, not correct.

## 3-1. Working style (always)

Lead with the outcome. Stay within the requested scope (no incidental refactors or abstractions). Ground every completion claim in a tool result from this session. Confirm before destructive or hard-to-reverse actions.

## 4. At the capability ceiling (escalate)

Signals you have hit the model's ceiling: stuck on the same problem 2+ times; open-ended creation where detail itself is the value; deep review that needs out-of-spec discovery. These are capability, not procedure, and a harness cannot fill them. In order: (1) reasoning effort already scales with difficulty — recommend raising reasoning effort to the user to push the current model to its ceiling; (2) **reactive effort delegation** — if the blocker is a bounded, hard *slice* (not the whole task), delegate just that slice to a background subagent (Task tool) with high effort: package the evidence (symptoms, attempts, failure point, repro, the specific sub-question) as the subagent prompt, force a structured return, then resume with its result as authoritative. This is the only real per-task effort knob in a normal session. **Opt-in, and not yet proven on real work** (the shadow layer in the docs measures whether it helps): use it for a genuinely stuck slice, not routinely, and **never trigger it from risk/deep classification alone** — that over-escalates simple high-risk tasks (false-escalate); (3) if still short, hand off to a stronger model in a fresh session with the same evidence package; (4) otherwise report the limit honestly and name where a human must step in.

## Install (always-on, optional)

Run once: `bash ~/.codex/skills/unifable/setup/setup.sh` → choose local (recommended) or global. Uninstall: `bash ~/.codex/skills/unifable/setup/uninstall.sh`. The UserPromptSubmit router, PostToolUse observation gate, and Stop completion gate are registered in `~/.codex/hooks.json` (trust them via `/hooks` on first run).
