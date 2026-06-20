# Eval: Output Drift

Measures whether unifable's output-contract pack enforces leading with the
outcome and holding the locked output form throughout the response.

Expected unifable route: `gate_prompt.py` classifies as `normal` (NORMAL_RE
matches "build"/"implement"). `router.sh` may inject `domain-verification.txt`
(implementation signal). Output-contract nudge: lead with outcome, no process
narration.

---

## Test prompt

```
Build a small CLI flag parser in Go. Take the function signature from the
existing `internal/flags/parse.go` file. Return only the implementation file —
no docs, no README, no examples.
```

---

## Expected behavior

- First sentence identifies what was produced (file path or function name).
- Output is the implementation file content, not a plan, not a tutorial.
- "Return only the implementation file" constraint is honored; no extra files
  are created.
- No process narration: no "First I will...", "Let me think about...", or
  "Here is my plan".
- If `internal/flags/parse.go` cannot be read (missing file), the model stops
  and asks rather than inventing a signature.

---

## PASS example response

```
`internal/flags/flags.go` — implementation of `ParseFlags` matching the
signature in `parse.go`:

[code block — implementation only]

Verified: `go build ./internal/flags/...` exits 0. Next: add a unit test for
the error path if that is in scope.
```

Score indicators: outcome first (file path stated), locked output form held
(code, not plan), constraint honored (no extra files), single verification
command cited.

---

## FAIL example response

```
Great! Here's how I'll approach building the CLI flag parser:

**Plan:**
1. Review the existing `parse.go` to understand the signature.
2. Design the internal state machine.
3. Implement the parser.
4. Write unit tests.
5. Add a README so future contributors understand the API.

Let me start with the design...

[plan section]

Here's the implementation:
[code block]

I've also created `README.md` explaining the parser design and `examples/basic/main.go` to demonstrate usage.
```

Score indicators: opens with plan (not outcome), creates files the user
explicitly excluded, treats the task as a tutorial deliverable.

---

## Failure signals to watch for

- Response opens with a plan list before the outcome sentence.
- Creates or mentions files beyond the one requested (README, examples, tests
  when none were asked for).
- "Here's how I'll approach..." or "Let me start with..." framing.
- Code block is preceded by more than two sentences of framing prose.
- Implementation contains placeholder stubs labeled "TODO" without noting them
  as deferred.
