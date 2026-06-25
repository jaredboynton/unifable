# Plan: extract minimal gpt-realtime-2 explore skill into the unifable plugin

**Target:** `~/__devlocal/unifable/skills/explore/` (plugin skill convention is `skills/<name>/{SKILL.md,scripts/}`, mirrors existing `skills/unifusion/`).
**Scope:** only the winning gpt-realtime-2 entrypoints + their runtime deps. No benchmarks, no retired backends (Exa), no alternate-transport (Cerebras submit), no Gemini/cursor/gk variants.

## Key finding: search.sh is NOT gpt-realtime-2

`scripts/search.sh:2` — "fast semantic code search via Cerebras gpt-oss-120b + ripgrep". It hits `api.cerebras.ai` (`search.sh` env block), not gpt-realtime-2. Its closure is 9 self-contained files (`search.mjs`, `search-lib.mjs`, `cerebras-search.mjs`, `ast-context.mjs`, `map*.mjs`) requiring `CEREBRAS_API_KEY`.

It conflicts with the "all gpt-realtime-2 things" constraint. **Recommendation: exclude search.sh** from the minimal bundle (ship trace + websearch only). Include it only if you explicitly want the Cerebras path too — then add `CEREBRAS_API_KEY` to setup preflight.

## Dependency closure (verified)

Computed by transitive `import`/`export ... from "./"` walk from each entry root (resolver run in `scripts/`, 2026-06-25):
- **trace.sh** -> trace-rt.sh -> `realtime-trace.mjs` + `map.mjs` + `lib/explore-output-prompt.mjs` + `lib/rt-trace-utils.mjs` = **26 .mjs**
- **websearch.sh** -> websearch-rt.sh -> `realtime-websearch.mjs` + `websearch-lib.mjs` + `lib/explore-output-prompt.mjs` = **26 .mjs**
- Union (trace + websearch, excluding search) = ~35 .mjs

## 4 prunable dead edges (sever to satisfy "only winning choices")

The static closure drags in retired/bench code through narrow imports. Each is a small surgical edit:

| File pulled in | Via | Live use? | Action |
|---|---|---|---|
| `lib/rt-exa-tools.mjs` | `realtime-websearch.mjs:12-14` imports only `createWebsearchContext` | exa search/fetch fns uncalled (grep clean); only the ctx helper is live | Move `createWebsearchContext` into a live lib (e.g. `rt-web-run-tools.mjs`), drop file |
| `lib/rt-submit-cerebras.mjs` | `realtime-trace.mjs:32`, called only at `:522` when `transport==="cerebras"` | default transport is `rt`; cerebras is a debug override | Remove the `cerebras` branch (`realtime-trace.mjs:159,522-523`), drop file |
| `lib/bench-trace-scorer.mjs` | `lib/render-trace-structured.mjs:43` imports `extractTraceCitations` | citation extraction is live; the rest of the scorer is bench-only | Move `extractTraceCitations` into `render-trace-structured.mjs`, drop file |
| `lib/bench-scorer-common.mjs` | only via `bench-trace-scorer.mjs:17` | dead once above is severed | Drop file |

After severing: union shrinks to ~31 .mjs, zero benchmark/exa/cerebras references.

## Files to copy (trace + websearch, post-prune)

Wrappers (`scripts/`): `trace.sh`, `trace-rt.sh`, `websearch.sh`, `websearch-rt.sh`, `env.sh`, `setup.sh`, `map.sh`.
Entry .mjs: `realtime-trace.mjs`, `realtime-websearch.mjs`, `websearch-lib.mjs`, `map.mjs`, `map-lib.mjs`, `map-pagerank.mjs`, `map-sigmap.mjs`, `map-ast-extract.mjs`, `ast-context.mjs`, `repo-context.mjs`, `explore-skill-context.mjs`.
`lib/`: `realtime_client.mjs`, `rt-agent-session.mjs`, `rt-session-utils.mjs`, `rt-explore-runtime.mjs`, `rt-tools.mjs`, `rt-trace-utils.mjs`, `rt-map-seed.mjs`, `rt-pipeline-seed.mjs`, `rt-pick-passages.mjs`, `rt-rehydrate-submit.mjs`, `rt-rehydrate-websearch.mjs`, `rehydrate-explore-wire.mjs`, `render-trace-structured.mjs`, `explore-output-prompt.mjs`, `explore-wire-format.mjs`, `htools.mjs`, `code-line.mjs`, `trace-schema.mjs`, `websearch-schema.mjs`, `codex-alpha-search-client.mjs`, `codex-responses-client.mjs`, `rt-web-run-tools.mjs`, `rt-web-run.mjs`.

**Exclude:** all `bench-*.sh`/`bench-*.mjs`, all `test-*.sh`, `probe-*.sh`, `*-gemini.*`, `trace-cursor.sh`, `trace-gk.sh`, `search*.{sh,mjs}`, `cerebras-search.mjs`, `search-lib.mjs`, `rt-exa-tools.mjs`, `rt-submit-cerebras.mjs`, `bench-trace-scorer.mjs`, `bench-scorer-common.mjs`, `benchmarks/`, `docs/`.

## SKILL.md (rewrite minimal)

New `skills/explore/SKILL.md`: keep frontmatter (name/description/version), document only `trace.sh` and `websearch.sh` (gpt-realtime-2). Drop the `EXPLORE_WS_BACKEND=exa` mention (`SKILL.md:28` — retired) and any search.sh section. Carry forward the swarm note from `AGENTS.md:13`.

## Caller migration

Old absolute path `~/.agents/skills/explore/scripts/*.sh` is referenced in `SKILL.md` and likely in user muscle memory. Options:
1. **Plugin-relative (preferred):** since it becomes a plugin skill, callers invoke via the skill, no hardcoded path.
2. **Compat shims:** leave 2-line wrappers at `~/.agents/skills/explore/scripts/{trace,websearch}.sh` that `exec ~/__devlocal/unifable/skills/explore/scripts/<same>.sh "$@"`.

## Verification gate

After copy + prune, in the new dir:
```
node --input-type=module -e '<closure resolver>'   # assert 0 MISSING, 0 bench/exa/cerebras files
node --check on every copied .mjs                  # syntax
EXPLORE_*_REASONING_EFFORT=... ./scripts/trace.sh "smoke question"   # live e2e
./scripts/websearch.sh "smoke goal"
```
None pass until the resolver reports zero missing imports and zero excluded-path files.

## Open questions for user

1. Include search.sh (Cerebras) anyway, or exclude per the gpt-realtime-2 constraint? (plan assumes exclude)
2. Do the 4 dead-edge prunes (cleaner, ~5 small edits), or lift-and-shift the closure as-is (faster, carries retired code)?
3. Compat shims at the old path, or plugin-relative only?
