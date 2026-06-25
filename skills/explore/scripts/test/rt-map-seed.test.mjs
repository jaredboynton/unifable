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

test("extractMapPaths parses map lines", () => {
  const map = `# tandem
## scripts/trace.sh
scripts/trace.sh:1-40  main def
scripts/gemini-trace.mjs:10-80 run def
`;
  const paths = extractMapPaths(map);
  assert.ok(paths.includes("scripts/trace.sh"));
  assert.ok(paths.includes("scripts/gemini-trace.mjs"));
});

test("requiredSeedPaths includes trace.sh for trace questions", () => {
  const paths = requiredSeedPaths("How does trace.sh work end to end?", WORKSPACE);
  assert.ok(paths.includes("scripts/trace.sh"));
  assert.ok(paths.includes("scripts/trace-rt.sh"));
  assert.ok(paths.includes("scripts/realtime-trace.mjs"));
});

test("deriveSeedPaths ranks question-named scripts first", () => {
  const map = `scripts/cursor-acp-trace.mjs:1-50 foo def
scripts/trace.sh:1-40 main def
scripts/gemini-trace.mjs:1-80 run def`;
  const paths = deriveSeedPaths("How does trace.sh work end to end?", map, WORKSPACE, { max: 4 });
  assert.equal(paths[0], "scripts/trace.sh");
  assert.ok(paths.includes("scripts/trace-rt.sh"));
});

test("shouldStopExplore when required seeds and min reads met", () => {
  const filesRead = new Set(["scripts/trace.sh", "scripts/trace-rt.sh", "scripts/realtime-trace.mjs", "scripts/map.mjs"]);
  assert.equal(
    shouldStopExplore({
      filesRead,
      question: "How does trace.sh work end to end?",
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
      question: "How does trace.sh work end to end?",
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
  const ranges = parseMapLineRanges("scripts/trace.sh:1-40 main\nscripts/foo.mjs:10-20");
  assert.deepEqual(ranges[0], { path: "scripts/trace.sh", start_line: 1, end_line: 40 });
});
