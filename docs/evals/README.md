# Behavioral Evaluation Suite

This suite measures whether unifable's prompt discipline actually changes model
behavior in the intended direction. It is a MANUAL regression harness — no
automated model calls. Each eval is run by a human who pastes the prompt into a
live session, observes the response, and scores it against the rubric.

---

## When to run

Run after any change to:

- `hooks/gate_prompt.py` or `scripts/gate/classify_task.py` (classification logic)
- `hooks/router.sh` (inline discipline injection triggers)
- `packs/router-manifest.json` (route definitions and inline body content)
- `setup/unifable-block.md` (the injected system block)

Run a baseline comparison: one session with unifable installed, one without. The
delta between scores is the signal. A single session score without a baseline
tells you nothing about the harness's effect.

---

## How to run a single eval

1. Open a Claude Code session or Codex session with unifable installed.
2. Open `docs/evals/<scenario>.md`. Read the test prompt and the expected behavior.
3. Paste the test prompt exactly as written into the session. Do not add framing.
4. Read the full response carefully.
5. Score it on each rubric dimension using `tests/eval_rubric.md` (0, 1, or 2 per
   dimension).
6. Compare against the PASS example and the FAIL example in the eval file.
7. Check the listed failure signals. Any present signal auto-fails that dimension.
8. Record the score and note the lowest-scoring dimension — repeated lows in one
   dimension indicate the next routing fix to make.

---

## Running a baseline comparison

1. Install unifable on branch A (or uninstalled baseline).
2. Run the full suite; record scores per dimension.
3. Switch to the branch under test (unifable installed / updated).
4. Re-run the same prompts in fresh sessions.
5. Compare dimension-by-dimension deltas.

A harness improvement shows a positive delta on the dimensions it targets. A
neutral delta means the pack text changed model phrasing but not behavior. A
negative delta is a regression.

---

## Eval files in this directory

| File | Scenario |
|---|---|
| `over-scope.md` | Does the model stay within the stated scope of a small edit? |
| `output-drift.md` | Does it lead with outcome and hold the locked output form? |
| `tool-bloat.md` | Does it avoid unnecessary tool calls when none are required? |
| `grounding-stop-gate.md` | Does it stop or caveat rather than fabricate grounding? |
| `route-disclosure.md` | Does the task-mode line appear for normal/deep work and stay absent for quick? |
| `delegation.md` | Does it use a subagent brief with a proper output contract when delegating? |
| `renderable-verification.md` | Does it run a render artifact in the real renderer before declaring done? |
| `uncertainty-research.md` | Does a hedged prompt trigger evidence-first behavior rather than a glib answer? |

---

## Rubric

See `tests/eval_rubric.md` for the full scoring rubric (dimensions, 0-2 anchors,
pass threshold).

---

## Notes

- Always use a fresh session per prompt. Reusing a session carries context that
  inflates scores.
- If a PASS response uses wording that matches the pack text almost verbatim, mark
  the session as contaminated and re-run with a paraphrased prompt before recording.
- Score the response that was actually produced, not what you expected. If the
  model improvises a better behavior not described in the PASS example, score it
  on the dimension anchors, not the example text.
