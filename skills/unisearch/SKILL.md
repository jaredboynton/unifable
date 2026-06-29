---
name: unisearch
description: >-
  External web research via unisearch.sh (gpt-realtime-2 + Codex alpha/search
  web_run). Use for up-to-date facts, library/API docs, prior art, version
  checks, and any question that needs the open web with grounded source URLs.
metadata:
  author: Jared Boynton
  version: "0.9.0"
  argument-hint: <research goal>
---

# Unisearch — Web Research

Run from the repo root. The skill installs to the stable runtime at
`~/.unifable/current/skills/unisearch` (refreshed to the newest version
on each session); invoke it there regardless of which CLI or plugin cache is
active:

```bash
~/.unifable/current/skills/unisearch/scripts/unisearch.sh "<research goal>"
```

A global `unisearch` launcher is also installed into `~/.local/bin` by the
unifable installer / SessionStart runtime sync, so `unisearch "<research goal>"`
works from any cwd whether or not the plugin is enabled (as long as
`~/.unifable/current` has been seeded once).

Default path is gpt-realtime-2 via `websearch-rt.sh`: two rounds (web_run via
Codex `alpha/search`, then pointer submit) with search reasoning `low` and
submit `low`. Requires Codex OAuth (`codex login`) and `curl`.

The implementation is shared with the sibling **unitrace** skill (deep trace +
fast locate); this skill is the external-research entrypoint. The complementary
codebase tools (`unitrace.sh`, `search.sh`) live in `unitrace`.
