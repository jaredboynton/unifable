# Map PageRank ranking-loop performance

The repo-map prefetch (`skills/explore/scripts/map.mjs`) hung on large trees. The
final ranking loop in `runPagerank` (`map-pagerank.mjs`) rescanned every
definition for every graph edge -- `O(edges x definitions)` -- which on a 2.1 GB
non-git tree (`/Users/jaredboynton/.claude-`, capped at 5000 source files)
produces 855,273 edges x 37,354 definitions and never finishes.

## Measured graph (5000-file cap on the pathological tree)

| metric | value |
|---|---|
| files | 5000 |
| tags | 134,106 |
| nodes | 3,147 |
| edges | 855,273 |
| definitions | 37,354 |
| walk | 76 ms |
| buildTagIndex | 2270 ms |
| buildPagerankGraph | 97 ms |
| power iteration (20x) | 432 ms |

`scripts/bench-rank.mjs` (scratchpad harness) instrumented the stages and the
three ranking variants. Output of all three is byte-identical (verified by
signature `rel:name:line` join and equal counts at both scales).

## Three approaches compared (final ranking loop only)

| approach | rank @400 files | rank @5000 files | output |
|---|---|---|---|
| **A0** current `O(E x D)` nested scan | 196.7 ms | never finishes (~hours) | baseline |
| **A1** per-file definition index `Map<rel, tags[]>` | 6.7 ms | 2965.6 ms | identical |
| **A2** file-rank accumulation (max incoming edge rank per file, one pass over defs) | 0.7 ms | **36.7 ms** | identical |

A1 is the obvious index fix: look up `defsByFile.get(dst)` instead of scanning
all definitions per edge. It removes the quadratic factor but still materializes
one ranked-def row per (edge, def-in-dst) and then sorts that whole array, so at
855k edges it still spends ~3 s -- over the 2 s target for the ranking step
alone.

A2 collapses each file's incoming edge ranks to a single score (their max --
which is exactly what the prior sort-desc + dedup-by-`rel:name:line` already
selected per definition), then makes one pass over the definition index attaching
that score. It emits ~37k rows instead of millions, so the sort is trivial.

**Selected: A2.** 80x faster than A1 at scale, provably identical output (the
deduped score of a definition is the max edge rank into its file under both the
old per-edge scan and A2), `O(E + D)`. Equivalence and the no-regression bound
are locked by tests in `test/map-pagerank.test.mjs`
(`O(E+D) ranking matches the O(E x D) reference output exactly`, and a dense
synthetic-graph timing guard).

Primary-source basis: aider's `repomap.py` distributes rank per out-edge and
accumulates `ranked_definitions[(dst, ident)] += src_rank * weight / total_weight`
-- O(edges), no per-definition rescan
(https://github.com/Aider-AI/aider/blob/main/aider/repomap.py, cross-checked via
https://github.com/NousResearch/hermes-agent/issues/535). aider keeps no node or
edge cap; the cap below is our own graceful-degradation guard.

## Guardrails (deterministic, no timeout)

The algorithm fix removes the hang itself. Two deterministic guards bound the
remaining pathological cases without a wall-clock timeout (timeouts mask the
underlying issue):

1. **Huge non-git bail** (`map.mjs` `generateMapText`): a non-git tree that hits
   the 5000-file cap is a home dir or cache, never a useful prefetch target. The
   prefetch enumerates once via `listRepoFilesMeta` and returns an empty map
   before the multi-second tag-extraction pass. Override with
   `EXPLORE_MAP_ALLOW_HUGE=1`; cap tunable via `EXPLORE_MAP_MAX_FILES`.
2. **Graph node/edge cap** (`buildPagerankGraph` -> `overCap`): a tree under the
   file cap can still produce a pathologically dense symbol graph. Past
   `EXPLORE_PAGERANK_MAX_NODES` (6000) or `EXPLORE_PAGERANK_MAX_EDGES` (2,000,000)
   the graph is flagged and `runPagerank` returns `[]`.

`trace-rt.sh` already fails open: if `map.mjs` errors or emits nothing,
`MAP_BLOCK` stays empty and no map is injected. No timeout is wired (by direction
-- it would only paper over a slow path; the O(E) fix + bail make it unnecessary).

## End-to-end validation (`map.mjs --mode tandem --no-cache`)

| scenario | result | time |
|---|---|---|
| pathological `.claude-` tree (non-git, 5000-file cap) | bail, empty map, `skipped: huge-non-git` | **162 ms** |
| same tree, `EXPLORE_MAP_ALLOW_HUGE=1` | full map completes (was an infinite hang before) | 3.59 s |
| unifable repo (git) | real 4216-byte map | **407 ms** |
| kepler repo (git, 1484 files) | real 4546-byte map | **1.74 s** |

The pathological tree now returns in 162 ms instead of hanging; real
repositories produce a real map well under 2 s. The `ALLOW_HUGE` run is included
only to demonstrate the algorithmic hang is gone -- the prefetch never takes that
path because the bail fires first.
