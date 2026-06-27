// Guards the load-bearing SYNTH_SYSTEM in enhance-prompt.mjs. Bench-validated
// 2026-06-27 (/tmp/enhance-bench/bench-synth.mjs, docs/evals/prompt-enhance.md):
// the worked few-shot example is the SOLE quality driver (3.50 -> 4.00 / 4), and
// Realtime caches the prefix at ANY size so there is NO 1024-token floor to chase.
// These tests pin both levers: the few-shot MUST stay (quality), and the prefix
// MUST stay lean (no padding that just slows cold calls for zero gain).

import { test } from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath, pathToFileURL } from "node:url";
import path from "node:path";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const { SYNTH_SYSTEM } = await import(pathToFileURL(path.join(HERE, "..", "enhance-prompt.mjs")).href);

test("SYNTH_SYSTEM is a non-empty string", () => {
  assert.equal(typeof SYNTH_SYSTEM, "string");
  assert.ok(SYNTH_SYSTEM.length > 500, `SYNTH_SYSTEM too short (${SYNTH_SYSTEM.length}); few-shot likely removed`);
});

test("SYNTH_SYSTEM keeps the worked few-shot example (the quality driver)", () => {
  // The few-shot teaches the "Area N" decomposition + concrete path:line density.
  // bench-synth showed removing it drops quality 4.00 -> 3.50.
  assert.ok(/WORKED EXAMPLE/i.test(SYNTH_SYSTEM), "missing WORKED EXAMPLE header");
  assert.ok(/Area 1/i.test(SYNTH_SYSTEM), "few-shot must demonstrate the 'Area N' decomposition");
  assert.ok(/trace\.sh:\d/.test(SYNTH_SYSTEM), "few-shot must cite a concrete path:line range");
});

test("SYNTH_SYSTEM keeps the core grounding rules", () => {
  assert.ok(/repo-specific commands/i.test(SYNTH_SYSTEM), "missing the no-repo-command rule");
  assert.ok(/provided code windows/i.test(SYNTH_SYSTEM), "missing the cite-from-windows rule");
  assert.ok(/Never invent a path/i.test(SYNTH_SYSTEM), "missing the no-hallucinated-paths rule");
});

test("SYNTH_SYSTEM stays LEAN (no padding past the quality plateau)", () => {
  // LEAN (rules + one worked example) benchmarks ~2500 chars and reaches the same
  // quality (4.00) as the ~3620-char FAT variant, with the fastest cold time.
  // Realtime caches at any prefix size, so crossing 1024 is NOT a goal -- cap the
  // prefix to keep cold calls fast and reject FAT-style bloat that earns nothing.
  assert.ok(
    SYNTH_SYSTEM.length <= 3300,
    `SYNTH_SYSTEM bloated to ${SYNTH_SYSTEM.length} chars (>3300); bench showed extra text past the few-shot earns no quality -- trim it`,
  );
});
