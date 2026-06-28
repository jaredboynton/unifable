# AGENTS.md — unifable

Development guide for agents working ON the unifable plugin. This repo IS the
harness: hooks + skills that force grounded, evidence-gated behavior on Claude
Code and Codex. Product overview and the full hook table live in
[README.md](README.md) — do not duplicate them here.

## Prime directive: enforced behavior ships as a hook, never a skill

The orchestrator is an LLM. Anything left to its discretion can be skipped, and
under load it will be. Therefore:

- Every behavior that MUST happen ships as a deterministic hook wired in
  `hooks/hooks.json`. A hook runs on the host's critical path and returns a
  blocking exit code; the model cannot route around it.
- Skills and subagents are ADVISORY only — they MUST NOT be the sole mechanism
  enforcing anything load-bearing.
- If a behavior matters, force it (hook) or accept that it will not happen.

The enforced load-bearing gates are the evidence gate, the groundedness breaker,
and the completion gate. Per-hook wiring and the skippable-vs-enforced contrast
table live in [hooks/AGENTS.md](hooks/AGENTS.md).

## Commands

```bash
# full gate suite (pytest + eval_gate_proof + test_gate_robustness): just test-all
# dev deps: uv run --with-requirements requirements-dev.txt
# parallel pytest only: just test-parallel   serial profile: just test-profile

# a single gate's tests
python3 -m pytest tests/test_groundedness_breaker.py -q

# compile-check the hot path before committing
python3 -m py_compile hooks/pre_tool_use.py scripts/gate/groundedness.py scripts/gate/ledger.py

# bump the plugin version everywhere (all 4 plugin dirs)
just version 1.21.1          # or: just version patch|minor|major
```

## Release conventions (repo-wide)

- Version bumps touch ALL manifests together: `.claude-plugin/`, `.codex-plugin/`,
  `.devin-plugin/`, `.factory-plugin/` (`plugin.json` + `marketplace.json`). Do not
  hand-edit them: run `just version <X.Y.Z>` (or
  `just version patch|minor|major`), which sets every version field in one pass via
  `scripts/bump_version.py` and exits nonzero if any straggler of the old version
  remains in the managed set.
- Releases MUST follow the `$release` flow end to end: update `CHANGELOG.md`, run
  the version bump, regenerate generated docs, run `just test-all`, commit, push
  `main`, create/push the `vX.Y.Z` tag, create the GitHub Release, and verify the
  remote branch, tag, and release.
- Every release MUST have changelog notes before the commit; `CHANGELOG.md` is the
  durable source (version/date, concise user-visible changes, verification). The
  GitHub Release body mirrors those notes.
- No emojis anywhere (output, commits, code, comments, docs).

## Benches and probes

Live latency / concurrency benchmarks go in `probes/` — not under `scripts/`.
Name them `probes/bench_<topic>.py` (or `probe_*` when diagnostic). Results
land in `probes/bench/results/` when the script writes artifacts. Excluded from
`just test-all` and the wait-audit scan (`scripts/audit_waits.py`). Do not add
bench scripts at repo root or in `scripts/gate/`.

Harness cost/latency A/B for the plugin itself stays in `benchmark/`. Unitrace-skill
trace A/B stays in `skills/unitrace/scripts/bench/`.

Gate-core conventions (fail-open, host-agnostic, failing-first tests, the pointer +
rehydrate rule, the 256k judge cap) live in
[scripts/gate/AGENTS.md](scripts/gate/AGENTS.md).

## Where to look

| Topic | Path |
|---|---|
| Product overview, hook table | [README.md](README.md) |
| Changelog / release notes | [CHANGELOG.md](CHANGELOG.md) |
| Hook wiring + enforcement layer | [hooks/AGENTS.md](hooks/AGENTS.md) |
| Gate core (policy, judge, ledger, conventions) | [scripts/gate/AGENTS.md](scripts/gate/AGENTS.md) |
| Janitor + alive-registry (`~/.unifable/alive/`) | [scripts/gate/AGENTS.md](scripts/gate/AGENTS.md#janitor--alive-registry), [scripts/gate/janitor.py](scripts/gate/janitor.py), [scripts/gate/process_host.py](scripts/gate/process_host.py) |
| Evidence-gate design | [docs/evidence-gate-design.md](docs/evidence-gate-design.md) |
| Pack routing (inline discipline) | [packs/router-manifest.json](packs/router-manifest.json), [scripts/gate/pack_router.py](scripts/gate/pack_router.py) |
| Generated hook/judge reference | [docs/generated/](docs/generated/), [docs/generated-docs-plan.md](docs/generated-docs-plan.md) |
| Eval rubric + scenarios | [docs/evals/](docs/evals/), [tests/eval_rubric.md](tests/eval_rubric.md) |
| Live benches / latency probes | [probes/](probes/) (`bench_*`, excluded from `just test-all`) |
| Other scoped notes | [scripts/AGENTS.md](scripts/AGENTS.md), [tests/AGENTS.md](tests/AGENTS.md), [docs/AGENTS.md](docs/AGENTS.md) |
