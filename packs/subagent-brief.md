<!-- unifable pack: subagent brief template (fill one file per dispatched worker) -->

# Subagent Brief

**Dispatched by:** [orchestrator task ID or story ID]
**Worker ID:** [assigned at dispatch]
**Date:** [YYYY-MM-DD]

---

## Objective

[One sentence. State the concrete deliverable — not the approach. Example: "Refactor the auth middleware to return a typed Result<Session, AuthError> instead of throwing."]

---

## Context and Inputs

- Relevant files: [list absolute paths]
- Relevant notes or prior decisions: [link or paste key facts only — do not paste full transcripts]
- Dependency state: [what must be true before this worker starts; name the blocking story or task ID]

---

## Constraints

- Scope is limited to the files listed above. Read any file needed for understanding; edit only those explicitly in scope.
- Do not touch: protected tests. Any test file under [path] is read-only. The only valid change to a test is a corrected equivalent that is equally strict.
- Do not change interfaces or contracts outside this task's scope without escalating to the orchestrator.
- [Add any domain-specific constraint, e.g. "Do not alter the public API surface of src/client.ts"]

---

## Strict Output Contract

On completion, write a result file at `.unifable/subagent-results/[worker-id].md` containing:

1. **Outcome:** one sentence — pass or fail.
2. **Files changed:** list with one-line description of each change.
3. **Verification run:** exact command(s) executed and exact output observed.
4. **Residual risks:** any assumption unverified in this session; any deferred check.
5. **Back-link:** the dispatching task ID this result belongs to.

Do not summarize reasoning. Do not include unrequested prose. The orchestrator reads only these five fields.

---

## Verification the Worker Must Run

Before writing the result file, the worker must execute and paste the output of:

```
[exact command — e.g. "cargo test --test integration_auth" or "pytest tests/test_auth.py -v"]
```

A result file without this section filled in is incomplete and the orchestrator will re-queue the task.
