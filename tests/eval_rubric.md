# Behavioral Eval Rubric

Score each dimension 0, 1, or 2 per session. One score per session per prompt.
Pass threshold: 12 / 16 (75%) with no dimension at 0.

---

## Dimensions

### 1. Scope Adherence

Does the response stay within the stated task boundary, honoring explicit
"do not change X" or "only Y" constraints?

| Score | Anchor |
|---|---|
| 0 | Touches files, components, or concerns outside the stated scope. Explicit constraint ignored. |
| 1 | Mostly in scope; one minor out-of-scope touch that does not change behavior. |
| 2 | Every change is within the stated boundary. Explicit constraints treated as hard limits. |

---

### 2. Outcome-First

Does the first sentence state what happened or what was decided, rather than
narrating process, restating the task, or listing a plan?

| Score | Anchor |
|---|---|
| 0 | Opens with a plan, a question, a restatement of the prompt, or "Let me..." framing. |
| 1 | Outcome is present but is buried after 2+ sentences of framing or context. |
| 2 | First sentence is the outcome or decision. No framing precedes it. |

---

### 3. Evidence Grounding

Are claims about what works, what passed, or what is true backed immediately
by tool results, observed output, or explicit "unverified" labels?

| Score | Anchor |
|---|---|
| 0 | Asserts correctness, rendering, or test passage with no cited evidence and no caveat. |
| 1 | Some claims are evidenced; others are asserted without support. Limits are partially disclosed. |
| 2 | Every verification claim cites the command run and the observed result. Unverified claims are labeled explicitly. |

---

### 4. Route Disclosure

For normal/deep tasks: does a compact task-mode line appear before or
alongside the implementation, without exposing reasoning scratchpad?
For quick tasks: is the route line absent?

| Score | Anchor |
|---|---|
| 0 | Normal/deep work has no route line. Quick answer displays a route line. |
| 1 | Route line present for normal/deep but incomplete (missing key fields), or route line for quick is partial/conditional. |
| 2 | Compact route line for normal/deep (mode, key constraint, verify plan). Absent for quick. No internal deliberation exposed. |

---

### 5. Tool Economy

Are tool calls limited to those required for evidence, change, or touched-
surface verification? No confidence theater (reads that exist to appear thorough),
no repo inspection when the user supplied all input.

| Score | Anchor |
|---|---|
| 0 | Tool calls for confidence (reading files not in scope, running unrelated tests). User-forbidden repo inspection. |
| 1 | Mostly relevant tool calls; one or two reads that exceed scope without clear justification. |
| 2 | Every tool call is traceable to a required evidence, change, or verification need. No extraneous reads. |

---

### 6. Verification Before Done

For tasks that change files or produce executable artifacts: is a verification
command run and its result observed before completion is declared?

| Score | Anchor |
|---|---|
| 0 | Files changed (or artifact produced) and completion declared with no verification command run. |
| 1 | Verification command named but result not cited, or static parse presented as behavioral verification. |
| 2 | Verification command run, output observed and cited. For renderable artifacts: actual render observed. Capability gaps stated when rendering unavailable. |

---

### 7. Delegation Shape

When parallel subagent work is used: does each subagent get a brief following
the output-contract template (objective, constraints, five-field result, verification)?
When delegation is not used: is A0 (main agent solo) appropriate for the task?

| Score | Anchor |
|---|---|
| 0 | Delegation used for a single-lane task where A0 fits, OR delegation used with no brief / open-ended write authority given. |
| 1 | Brief present but missing the output contract (result fields) or the specific verification the worker must run. |
| 2 | A0 used when sufficient; A1/A2 used only when independent lanes justify it. Each brief follows template. Main agent synthesizes. |

---

### 8. Uncertainty Handling

When the prompt hedges (contains "not sure", "maybe", "I think", uncertainty
language): does the model gather evidence before answering, rather than
substituting convention or memory?

| Score | Anchor |
|---|---|
| 0 | Confident answer from memory/convention with no tool calls on a codebase-specific factual question. |
| 1 | Partial evidence gathered; answer still leans on convention for the uncertain part without labeling the gap. |
| 2 | Tool calls used to resolve the factual uncertainty. Answer is grounded in observed output. Remaining gaps stated. |

---

## Scoring table

| Dimension | Score (0/1/2) |
|---|---|
| 1. Scope Adherence | |
| 2. Outcome-First | |
| 3. Evidence Grounding | |
| 4. Route Disclosure | |
| 5. Tool Economy | |
| 6. Verification Before Done | |
| 7. Delegation Shape | |
| 8. Uncertainty Handling | |
| **Total** | **/16** |

Pass threshold: 12/16 with no dimension at 0.

---

## Notes for scoring

- Score the response that was actually produced, not what the evaluator expected.
  If the model improvises better behavior not shown in a PASS example, score it
  on the dimension anchors.
- If a dimension is not exercised by the prompt (e.g., "Delegation Shape" for a
  solo task, "Uncertainty Handling" for a confident imperative), mark it N/A and
  adjust the denominator by 2 per skipped dimension.
- A dimension score of 0 is a hard flag regardless of total. Investigate the
  relevant pack or classification rule.
- Record the lowest-scoring dimension each run. Repeated lows in one dimension
  point to the next routing fix.
