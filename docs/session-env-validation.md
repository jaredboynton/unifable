# Empirical Validation Protocol: UNIFABLE_STRICT_SESSION

This document is the short reproducible protocol + success criteria for deciding whether it is safe to set UNIFABLE_STRICT_SESSION=1 for a given host (as a default in its installer, or as documented policy).

It operationalizes the claim:

> Turn it on (UNIFABLE_STRICT_SESSION=1) when:
> - You trust that Bash always gets the same session env as the active conversation (e.g. host injects it into every Bash subprocess)
> - You want to forbid agents from hand-copying --task-id at all and force env-only binding
>
> That's a good host policy, not a safe default for the CLI everywhere.

## Prerequisites (instrumentation already landed)
- `unifable-spec where` emits on stderr:
  `UNIFABLE_SESSION_RESOLVED=<id> SOURCE=<source>`
  (source = payload | env:CLAUDE_CODE_SESSION_ID | env:CODEX_THREAD_ID | env:CURSOR_CONVERSATION_ID | env:CURSOR_SESSION_ID | default | none)
- Analyzer: `python3 scripts/measure_session_env.py <log>`

## Reproducible Protocol (per host)

1. Start a fresh session on the target host. Trigger a non-LIGHT task so the prompt hook scaffolds the spec and prints the exact `--task-id <KEY>` value the agent should use. Record that KEY as the "prompt-scaffolded expected id". Also note any `session_id` visible in host logs/payload for the turn if available.

2. For each of N (>=10 recommended) Bash probes in the session, execute (via the agent's Bash tool or manual if allowed):
   ```
   unifable-spec where 2>&1 ; echo '---ENV---' ; env | grep -E 'CLAUDE_CODE_SESSION_ID|CODEX_THREAD_ID|CURSOR_CONVERSATION_ID|CURSOR_SESSION_ID' || true
   ```
   Variations to include (label the outputs):
   - From repo root.
   - After `cd sub/dir && ...`
   - Multiple turns in same conversation.
   - After session resume.
   - Inside a delegated Task/Agent (if it shares the id; capture separately).
   - With no env (agent must pass `--task-id THE-KEY-FROM-PROMPT` explicitly): `unifable-spec where --task-id THE-KEY-FROM-PROMPT 2>&1 ; ...`

3. Capture the full tool result (stdout + stderr) for each probe. Save per session/host into a log (one file or tagged blocks).

4. (Optional) Also capture the hook PreToolUse payload's `session_id` for the Bash turn, if the host exposes transcripts/logs.

5. Run the analyzer:
   ```
   python3 scripts/measure_session_env.py my-claude-probes.log
   ```
   Manually cross-check the `UNIFABLE_SESSION_RESOLVED` values against the prompt-scaffolded KEY for that session.

6. Repeat for other hosts. Keep results separate.

## Success Criteria (example; adjust after first 20-30 probes)

- env present in >= 95% of probes where the host is expected to inject (Claude Code / Codex with injection).
- Of the present cases, resolved id matches an observed env var value in >= 95%.
- 0 probes where:
  - env was correct (matched prompt KEY), and
  - an explicit `--task-id` that also matched the prompt KEY was passed, and
  - strict would have rejected it (i.e. the explicit != env, but both were actually correct for the conversation).
- Stable across cd/subdir/resume/delegation (no regression in match rate).
- Per-host reporting only (never pool Claude Code + Codex + Cursor numbers).

If criteria hold for a host, it is reasonable to document "safe to set UNIFABLE_STRICT_SESSION=1 for <host>" (e.g. in its install wrapper or docs). Otherwise keep opt-in and/or request host changes to inject the session id into Bash tool envs.

If env is frequently absent, agents legitimately need the value from the prompt scaffold; forbidding hand-copy via strict would break those flows unless the host starts injecting the env.

## Initial Trials Performed (this workspace)

Simulated runs (using current code + forced env + subdir + absent + explicit) were executed via the shell to validate the probe + analyzer pipeline. Example collection and analysis output are in /tmp/probe-collection/ (or re-generate with the commands in AGENTS.md).

Observed in sims:
- When env set, SOURCE reports the env var and resolved matches.
- Subdir does not affect env resolution (as expected; canonical root is separate concern).
- Absent env + explicit --task-id still emits SOURCE=none (so we can count "explicit required").
- Analyzer produces per-host table + match/absent rates.

Real trials on Claude Code and Codex (and Cursor if applicable) must be performed in those hosts using the protocol above. Update this doc or AGENTS.md with the measured % and decision.

## Example Decision Output (to record after real trials)

Host: Claude Code
- 18 probes, 18 present (100%), 18/18 match (100%)
- 0 absent
- stable across subdir/resume
Decision: safe to default-on for Claude Code (add to install/claude.sh or document as recommended). Consider setting in the plugin cache env or wrapper.

Host: Codex
- 12 probes, 9 present (75%), 8/9 match
- 3 absent (agent used prompt --task-id)
Decision: keep opt-in for now; file issue with Codex for env injection into tool shells. Document as host policy only.

(Replace with actual numbers.)

## Example Results from Initial Instrumentation Validation (simulated collections)

These were generated inside the development environment by forcing env vars and running the probe + analyzer (to validate the measurement pipeline itself). They are illustrative only.

Claude-flavored collection (3 probes):
- 3 present (100%), 3 match (100%)
- no absent

Codex-flavored collection (2 probes):
- 1 present (50%), 1 match
- 1 absent (explicit --task-id case observed)

Cursor runtime observation (real `env` inside Cursor Bash tool):
- Exposes `CURSOR_CONVERSATION_ID` (value matches the conversation, e.g. 50ee8553-...).
- Does **not** set `CURSOR_SESSION_ID`.
Before the fix below, `resolve_session_id` (CLI path, no payload) would report SOURCE=none/default for Cursor.
After adding `CURSOR_CONVERSATION_ID` to the list, it will report SOURCE=env:CURSOR_CONVERSATION_ID and auto-bind correctly.

Decision illustration (do not treat as final):
- Claude Code: meets example threshold in sim -> candidate for host policy "on".
- Codex: mixed/absent seen -> keep opt-in; investigate env injection.
- Cursor: uses CURSOR_CONVERSATION_ID (now recognized). Once the name is in the resolver, env binding works; strict policy can be considered per the measured match rates.

Real trials on the actual hosts (following steps 1-6) are required before any policy change.

## Files
- Probe command: documented in AGENTS.md
- Emission: scripts/gate/spec.py (resolve_session_id_with_source + _cmd_where)
- Analyzer: scripts/measure_session_env.py
- This protocol: docs/session-env-validation.md

Do not change the default behavior of strict in the CLI (it remains opt-in via the env var). This protocol produces the data to decide when a host installer can/should flip it on.
