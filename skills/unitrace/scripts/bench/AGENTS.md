<coding_guidelines>
# bench/ — unitrace search + borrow live benches

Live A/B harnesses for the unitrace search path and the shared-daemon rtinfer
borrow. These are LIVE benches: they spawn real `search.sh` / `trace-rt.sh`
processes and need Codex auth (`codex login`) plus a reachable daemon or
`cse-toold` rtinfer endpoint. They are EXCLUDED from `just test-all` (only the
deterministic `test/*.test.mjs` unit tests gate). Per repo policy, the harness
prints results to `bench/results/<ts>/{raw.json,summary.md}` and exits nonzero on
a FAIL verdict so it can gate a default flip.

## Layout

| File | Role |
|---|---|
| `search-multiformat-ab.mjs` / `.sh` | search borrow proof + multiformat config sweep |
| `trace-ab.mjs` | trace/nav variant matrix; `borrow-off`/`borrow-on` pair |
| `borrow-callers-ab.mjs` | enhance (+ opt-in websearch) borrow on/off, judged |
| `borrow-proof.sh` | aggregator: every caller × both corpora -> one PASS/FAIL |
| `corpus/multiformat/` | synthetic labeled corpus (code/doc/config/data + secrets) |
| `queries/multiformat.jsonl`, `queries/unifable.jsonl` | labeled query sets (kept OUT of the searched roots) |
| `borrow-callers-prompts.json` | enhance/websearch prompt sets |

Query files live under `queries/`, never inside a searched corpus root — for the
real-repo corpus the root IS the repo, so an in-tree queries file would pollute
retrieval and inflate find-rate.

## Verdict contract (mechanical, auditable)

The borrow is PREFERRED, never required: every arm is fail-open. The gate proves
the borrow does not REGRESS quality or latency, and that it ACTUALLY served (not
a silent fall-through to the UDS pool). Thresholds live in a `THRESHOLDS` const
at the top of each harness:

- `served-rate >= 90%` on any borrow-on arm. Below that, no `cse-toold` was
  reached and the run is marked INVALID (not PASS). Attribution comes from the
  `[daemon] ns=<ns> served rtinfer=N uds=M` stderr marker emitted under
  `UNITRACE_DAEMON_DEBUG=1` (or `UNITRACE_SEARCH_DEBUG=1`).
- find-rate(rtinfer) >= find-rate(uds); top1-rate(rtinfer) >= top1-rate(uds).
- p95(rtinfer) <= p95(uds) + 10% (search); med wall +10% (trace) / +15% (enhance).
- error-rate == 0 on every arm. Secret leaks are reported as a non-blocking
  WARNING, never a gate failure: search intentionally surfaces lexically-matched
  files (that retrieval policy is owned by `search-fast.mjs`, not this borrow
  gate). A borrow arm leaking MORE than the `uds` control is still called out in
  the warnings so a true borrow-induced regression stays visible.
- fail-open arm `rtinfer-absent` (borrow on, endpoint pinned dead) must match the
  `uds` control within 5 points and serve 0 via rtinfer (proves no hang / no
  probe storm when `cse-toold` is gone).

## Multiformat config sweep

`search-multiformat-ab.mjs` carries config-sweep arms (`docbudget0/2/8`,
`nullfb-off`, `floor3/floor4`) so the single best multiformat config is chosen by
labeled find/top1 + negative-query latency, not guessed. The winner is the config
that maximizes find/top1 without regressing negative-query p50. Run e.g.
`--variants baseline,docbudget2,docbudget8,nullfb-off,floor3,floor4` on both
corpora, read `summary.md`, pick the winner, and record it in `CHANGELOG.md`.

## Promotion procedure (gated on `borrow-proof.sh` PASS)

Do NOT flip the default or remove any flag until `borrow-proof.sh` returns
OVERALL: PASS on a host with a live `cse-toold`. Steps, in order:

1. PROVE. `bash skills/unitrace/scripts/bench/borrow-proof.sh` (add
   `--with-websearch` for the live web caller). Confirm OVERALL: PASS and
   served-rate >= 90% on every borrow-on arm. Archive the `results/<ts>/` paths.
2. FLIP + SOAK. Change the `rtinferEnabled()` default in
   `skills/unitrace/scripts/lib/rtinfer-client.mjs` to ON, keep the flag as an
   escape hatch for one release, and record the proof report paths + swept
   multiformat winner in `CHANGELOG.md`. Bump version via `just version`.
3. REMOVE + LOCK. After the soak release with no regressions:
   - delete `UNITRACE_DAEMON_RTINFER` (+ the legacy `UNITRACE_SEARCH_RTINFER`
     alias); make `rtinferTry` always-attempt (still fail-open to UDS).
   - hardcode the swept multiformat winner as fixed defaults in `search-fast.mjs`
     and drop the override reads it replaced (`UNITRACE_SEARCH_FAST_NULL_FALLBACK`,
     `UNITRACE_SEARCH_FAST_MAX_DOC_FILES`, and `UNITRACE_SEARCH_SCORE_MIN` if a
     fixed value wins).
   - KEEP: `CSE_RTINFER_URL` (discovery override), the presence-hint gate, the
     contract major-match, and the UDS + agentic fallback. "Remove the option"
     means remove the OPT-OUT, never the fallback.
   - update `test/rtinfer-client.test.mjs` (drop the flag enable/disable cases ->
     assert always-attempt + fail-open) and `test/search-multiformat.test.mjs`
     (drop the null-fallback-off case -> assert the locked behavior).
4. RE-VERIFY. Re-run `borrow-proof.sh` on the locked defaults to confirm the
   proven numbers reproduce, then `just test-all` green.

## Conventions

- No emojis anywhere (output, code, comments, results).
- New variants: add to the `VARIANTS`/`ARMS` map; the harness runs whatever
  `--variants`/`--callers` lists.
- Stale-socket hygiene before a matrix: `rm -f ~/.unifable/searchd/*.sock
  ~/.unifable/searchd/*.lock` (the aggregator does this for you).
</coding_guidelines>
