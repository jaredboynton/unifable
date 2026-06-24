# Empirical Validation Protocol: Session Env Binding

The spec CLI always resolves session id from the host environment (no `--task-id`
flag). This protocol measures whether Bash tool subprocesses receive the same
session env as the hook/prompt scaffold.

## Prerequisites (instrumentation already landed)
- `unifable doctor session-env` emits on stderr:
  `UNIFABLE_SESSION_RESOLVED=<id> SOURCE=<source>`
  (source = payload | env:CLAUDE_CODE_SESSION_ID | env:CODEX_THREAD_ID | env:CURSOR_CONVERSATION_ID | env:CURSOR_SESSION_ID | default | none)
- Analyzer: `python3 scripts/measure_session_env.py <log>`

## Reproducible Protocol (per host)

1. Start a fresh session on the target host. Trigger a non-LIGHT task so the
   prompt hook scaffolds the spec. Record the conversation/session id from host
   logs or payload if available.

2. For each of N (>=10 recommended) Bash probes in the session, execute (via the
   agent's Bash tool or manual if allowed):
   ```
   unifable doctor session-env 2>&1 ; echo '---ENV---' ; env | grep -E 'CLAUDE_CODE_SESSION_ID|CODEX_THREAD_ID|CURSOR_CONVERSATION_ID|CURSOR_SESSION_ID' || true
   ```
   Variations to include (label the outputs):
   - From repo root.
   - After `cd sub/dir && ...`
   - Multiple turns in same conversation.
   - After session resume.
   - Inside a delegated Task/Agent (if it shares the id; capture separately).

3. Capture the full tool result (stdout + stderr) for each probe. Save per
   session/host into a log (one file or tagged blocks).

4. (Optional) Also capture the hook PreToolUse payload's `session_id` for the
   Bash turn, if the host exposes transcripts/logs.

5. Run the analyzer:
   ```
   python3 scripts/measure_session_env.py my-claude-probes.log
   ```
   Manually cross-check the `UNIFABLE_SESSION_RESOLVED` values against the
   expected conversation id for that session.

6. Repeat for other hosts. Keep results separate.

## Success Criteria (example; adjust after first 20-30 probes)

- env present in >= 95% of probes where the host is expected to inject (Claude
  Code / Codex / Cursor with injection).
- Of the present cases, resolved id matches an observed env var value in >= 95%.
- Stable across cd/subdir/resume/delegation (no regression in match rate).
- Per-host reporting only (never pool Claude Code + Codex + Cursor numbers).

If criteria fail for a host, file an issue with that host for env injection into
Bash tool shells. The CLI cannot accept a hand-copied session id.

## Cursor runtime observation

Real `env` inside Cursor Bash tool exposes `CURSOR_CONVERSATION_ID` (value
matches the conversation). It does not set `CURSOR_SESSION_ID`. The resolver
checks `CURSOR_CONVERSATION_ID` first among Cursor vars.

## Files
- Probe command: `unifable doctor session-env`
- Emission: scripts/gate/spec.py (resolve_session_id_with_source + _cmd_doctor_session_env)
- Analyzer: scripts/measure_session_env.py
- This protocol: docs/session-env-validation.md
