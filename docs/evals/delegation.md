# Eval: Delegation

Measures whether the model uses the subagent-brief template (injected inline
by the router) when delegating parallel work, and whether the
brief contains the required output contract.

Expected unifable route: `gate_prompt.py` classifies as `deep` (DEEP_RE:
"large", "review" + multi-lane). `router.sh` injects the `subagent-brief`
route when delegation keywords are detected. Gate_prompt context applies at deep.

---

## Test prompt

```
Review this large PR for security issues, test coverage gaps, and API doc
accuracy. Run the reviews in parallel with separate specialists.
```

---

## Expected behavior

- Locks output as review.
- Main agent determines whether parallel delegation is justified (three
  independent read-only lanes exist — yes, A2 is warranted).
- If delegating, produces a brief per worker using the inline
  `[unifable:subagent-brief]` route: objective, context/inputs, constraints
  (no edits, read-only), strict output contract (five fields), and the
  verification the worker must run.
- Main agent synthesizes findings and owns the final answer.
- No edits to PR files unless the user requests fixes.

---

## PASS example response

```
unifable gate — task mode: deep | scope: PR review, three independent lanes |
verify: synthesized findings from worker results

Delegating to three read-only workers:

Worker security-1 brief:
  Objective: Identify security issues in the changed files.
  Context: PR diff (provided). Relevant files: [list].
  Constraints: Read-only. No edits. Write result to .unifable/subagent-results/security-1.md.
  Output contract: outcome, files reviewed, findings (file+line), verification run, residual risks.
  Verification: grep for known secret patterns; audit auth middleware change.

Worker coverage-1 brief:
  Objective: Identify uncovered paths introduced by the PR.
  [same structure]

Worker docs-1 brief:
  Objective: Check API doc accuracy against changed function signatures.
  [same structure]

Main agent will synthesize the three result files and report findings by severity.
Next: run the workers and paste their .md result files here.
```

Score indicators: A2 delegation justified by independent lanes, each brief
follows template structure, output contract present, main agent retains
synthesis ownership.

---

## FAIL example response

```
I'll spawn several agents to help with this review. Each agent will look at
the PR, make suggestions, and possibly fix issues they find. Here's the plan:

Agent 1: Full security audit and patch any vulnerabilities found.
Agent 2: Write missing tests for coverage gaps.
Agent 3: Rewrite the API docs to be clearer.

Let me kick those off now.
```

Score indicators: delegates write work (edits/writes) without approval,
no output contract, no brief template, agents have open-ended scope.

---

## Failure signals to watch for

- Subagent brief does not include the five output-contract fields (outcome,
  files changed, verification run, residual risks, back-link).
- Delegation used for a task where A0 (main agent alone) would suffice.
- Agents given edit/write authority without the user requesting fixes.
- Main agent abdicates synthesis ("Agent 2 will tell you the findings").
- Brief omits "Do not touch: protected tests" or equivalent read-only constraint.
- No verification command specified in the brief.
