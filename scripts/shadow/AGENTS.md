# scripts/shadow - agent notes

## Scope

These rules apply to shadow logging and analysis helpers.

## Rules

- Keep shadow code observational; it must not become an enforcement path.
- Scrub or avoid sensitive payloads before writing logs or reports.
- Analysis output should be reproducible from the collected input files.

## Verification

- Run targeted shadow tests or a small fixture analysis after behavior changes.
