import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";
import {
  deriveSeedPaths,
  extractMapPaths,
  phraseDefSeeds,
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
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "unitrace-seed-twin-"));
  try {
    fs.mkdirSync(path.join(dir, "gateway", "src", "generated"), { recursive: true });
    fs.writeFileSync(path.join(dir, "gateway", "src", "generated", "access-matrix.js"), "module.exports = {};\n");
    fs.writeFileSync(path.join(dir, "gateway", "src", "generated", "access-matrix.ts"), "export const accessMatrix = {};\n");
    const map = `gateway/src/generated/access-matrix.js:1-40 access\ngateway/src/generated/access-matrix.ts:1-40 access`;
    const paths = deriveSeedPaths("How does gateway access enforcement work?", map, dir, { max: 4 });
    assert.ok(!paths.includes("gateway/src/generated/access-matrix.js"));
    assert.ok(paths.includes("gateway/src/generated/access-matrix.ts"));
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("phraseDefSeeds bridges a prose bigram to its camelCase definition", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "unitrace-phrase-seed-"));
  try {
    fs.mkdirSync(path.join(dir, "lib"), { recursive: true });
    // The load-bearing file: defines buildSubmitPacket. The question never names
    // it as a symbol, only as the prose phrase "submit packet".
    fs.writeFileSync(
      path.join(dir, "lib", "trace-core.mjs"),
      "export function buildSubmitPacket(args) {\n  return { ...args };\n}\n",
    );
    // A lexical look-alike that must NOT be seeded: different word, no def match.
    fs.writeFileSync(
      path.join(dir, "lib", "unrelated.mjs"),
      "// submit packet mentioned in a comment but no definition here\n",
    );
    const added = [];
    const out = phraseDefSeeds({
      workspace: dir,
      question: "How does the pipeline build the submit packet for a trace?",
      onRead: (rel) => added.push(rel),
    });
    assert.ok(out.includes("lib/trace-core.mjs"), `expected trace-core seeded, got ${out.join(",")}`);
    assert.ok(!out.includes("lib/unrelated.mjs"), "comment-only file must not be seeded");
    assert.deepEqual(out, added);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("phraseDefSeeds abstains when no bigram resolves to a definition", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "unitrace-phrase-none-"));
  try {
    fs.mkdirSync(path.join(dir, "lib"), { recursive: true });
    fs.writeFileSync(path.join(dir, "lib", "x.mjs"), "export const x = 1;\n");
    const out = phraseDefSeeds({
      workspace: dir,
      question: "How does the widget frobnicate the doohickey?",
      onRead: () => {},
    });
    assert.deepEqual(out, []);
    // Kill-switch respected.
    process.env.UNITRACE_RT_PHRASE_SEED = "0";
    try {
      assert.deepEqual(
        phraseDefSeeds({ workspace: dir, question: "build the submit packet", onRead: () => {} }),
        [],
      );
    } finally {
      delete process.env.UNITRACE_RT_PHRASE_SEED;
    }
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
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
