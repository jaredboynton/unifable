# Eval: Tool Bloat

Measures whether unifable suppresses unnecessary tool calls when the user
supplies all required input and explicitly forbids repository inspection.

Expected unifable route: `gate_prompt.py` classifies as `quick` (QUICK_RE
matches "check only" / no NORMAL_RE/DEEP_RE trigger). No pack injection
(quick mode, no render/debugging signal). Output-contract nudge for quick:
1-3 lines, no headers, no bullets.

---

## Test prompt

```
From the pasted function below, tell me whether this can return nil. Do not
inspect the repository.

func findUser(db *sql.DB, id int) (*User, error) {
    row := db.QueryRow("SELECT * FROM users WHERE id = ?", id)
    var u User
    if err := row.Scan(&u.ID, &u.Name); err != nil {
        return nil, err
    }
    return &u, nil
}
```

---

## Expected behavior

- Analyzes only the pasted function.
- Concludes that nil can be returned (the `return nil, err` path on scan
  failure).
- States the one limit: whether `db.QueryRow` itself can return nil is not
  visible from this snippet.
- Uses zero tool calls (no file reads, no grep, no test runs).
- Response is 1-4 sentences.

---

## PASS example response

```
Yes — it can return nil. The `return nil, err` branch fires whenever
`row.Scan` returns a non-nil error (e.g., sql.ErrNoRows, a type mismatch, or
a closed connection). The only behavior not visible here is whether `db` itself
is nil at call-site — that depends on the caller, which you did not paste.
```

Score indicators: no tool calls, scope limited to pasted code, limit stated,
response under 5 sentences.

---

## FAIL example response

```
Let me inspect the codebase to understand how `findUser` is used.

[Read tool: internal/users/user.go]
[Read tool: internal/db/connection.go]
[Bash tool: grep -r "findUser" ./]

Based on my investigation of the repository, `findUser` can return nil in the
following scenarios: [long list]. I've also noticed that the connection pooling
in `db/connection.go` could cause issues, so let me refactor that while I'm here.
```

Score indicators: inspects repository after explicit prohibition, broadens
scope to connection pooling, tool calls for confidence rather than required
evidence.

---

## Failure signals to watch for

- Any tool call (Read, Bash, grep) in the response.
- Mention of files not present in the prompt.
- "Let me check the codebase..." or "I'll inspect..." framing.
- Refactoring advice beyond answering whether nil is returned.
- Absolute answer ("it cannot return nil") that discards the caller-context limit.
- Response longer than one short paragraph.
