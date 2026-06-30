import assert from "node:assert/strict";
import path from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";
import {
  deriveSeedPaths,
  extractMapPaths,
  requiredSeedPaths,
  shouldStopExplore,
} from "../lib/rt-map-seed.mjs";

const WORKSPACE = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const REPO_ROOT = path.resolve(WORKSPACE, "../..");

test("extractMapPaths parses map lines", () => {
  const map = `# tandem
## scripts/unitrace.sh
scripts/unitrace.sh:1-40  main def
scripts/gemini-trace.mjs:10-80 run def
`;
  const paths = extractMapPaths(map);
  assert.ok(paths.includes("scripts/unitrace.sh"));
  assert.ok(paths.includes("scripts/gemini-trace.mjs"));
});

test("requiredSeedPaths includes unitrace.sh for unitrace questions", () => {
  const paths = requiredSeedPaths("How does unitrace.sh work end to end?", WORKSPACE);
  assert.ok(paths.includes("scripts/unitrace.sh"));
  assert.ok(paths.includes("scripts/trace-rt.sh"));
  assert.ok(paths.includes("scripts/realtime-trace.mjs"));
});

test("requiredSeedPaths resolves nested unitrace paths from the repo root", () => {
  const paths = requiredSeedPaths("How does trace-rt.sh work end to end?", REPO_ROOT);
  assert.ok(paths.includes("skills/unitrace/scripts/trace-rt.sh"));
  assert.ok(paths.includes("skills/unitrace/scripts/realtime-trace.mjs"));
  assert.ok(!paths.includes("skills/unitrace/scripts/unitrace.sh"));
});

test("deriveSeedPaths ranks question-named scripts first", () => {
  const map = `scripts/cursor-acp-trace.mjs:1-50 foo def
scripts/unitrace.sh:1-40 main def
scripts/gemini-trace.mjs:1-80 run def`;
  const paths = deriveSeedPaths("How does unitrace.sh work end to end?", map, WORKSPACE, { max: 4 });
  assert.equal(paths[0], "scripts/unitrace.sh");
  assert.ok(paths.includes("scripts/trace-rt.sh"));
});

test("deriveSeedPaths skips archive and test paths for non-archive implementation questions", () => {
  const map = `skills/unitrace/scripts/archive/cursor-acp-trace.mjs:1-50 foo def
skills/unitrace/scripts/test-trace-rt.sh:1-60 replay
skills/unitrace/scripts/trace-rt.sh:1-40 main def`;
  const paths = deriveSeedPaths("How does trace-rt.sh work end to end?", map, REPO_ROOT, { max: 4 });
  assert.ok(!paths.includes("skills/unitrace/scripts/archive/cursor-acp-trace.mjs"));
  assert.ok(!paths.includes("skills/unitrace/scripts/test-trace-rt.sh"));
});

test("deriveSeedPaths falls back to query tokens for non-trace questions", () => {
  const map = `scripts/gate/pack_router.py:1-40 router\nscripts/gate/ledger.py:1-40 ledger`;
  const paths = deriveSeedPaths("How does the pack router choose an inline discipline?", map, REPO_ROOT, { max: 4 });
  assert.ok(paths.includes("scripts/gate/pack_router.py"));
});

test("deriveSeedPaths prefers ts twin over js when both exist", () => {
  const map = `gateway/src/generated/scope-matrix.js:1-40 scope\ngateway/src/generated/scope-matrix.ts:1-40 scope`;
  const paths = deriveSeedPaths("How does gateway scope enforcement work?", map, path.resolve(REPO_ROOT, "../kepler"), { max: 4 });
  assert.ok(!paths.includes("gateway/src/generated/scope-matrix.js"));
  assert.ok(paths.includes("gateway/src/generated/scope-matrix.ts"));
});

test("shouldStopExplore when required seeds and min reads met", () => {
  const filesRead = new Set(["scripts/unitrace.sh", "scripts/trace-rt.sh", "scripts/realtime-trace.mjs", "scripts/map.mjs"]);
  assert.equal(
    shouldStopExplore({
      filesRead,
      question: "How does unitrace.sh work end to end?",
      workspace: WORKSPACE,
      toolTurnCount: 1,
      minReads: 4,
      stopReads: 6,
      stopToolCalls: 2,
    }),
    true
  );
});

test("shouldStopExplore false when required seed missing", () => {
  const filesRead = new Set(["scripts/trace-rt.sh", "scripts/map.mjs"]);
  assert.equal(
    shouldStopExplore({
      filesRead,
      question: "How does unitrace.sh work end to end?",
      workspace: WORKSPACE,
      toolTurnCount: 1,
      minReads: 4,
      stopReads: 6,
      stopToolCalls: 2,
    }),
    false
  );
});

test("preflightExploreExecCode rejects syntax errors", async () => {
  const { preflightExploreExecCode } = await import("../lib/rt-map-seed.mjs");
  const bad = preflightExploreExecCode("const [a, b = await tools.grep({});");
  assert.equal(bad.ok, false);
  assert.match(bad.error, /syntax/i);
});

test("parseMapLineRanges extracts path spans", async () => {
  const { parseMapLineRanges } = await import("../lib/rt-map-seed.mjs");
  const ranges = parseMapLineRanges("scripts/unitrace.sh:1-40 main\nscripts/foo.mjs:10-20");
  assert.deepEqual(ranges[0], { path: "scripts/unitrace.sh", start_line: 1, end_line: 40, label: "main" });
});
