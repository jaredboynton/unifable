import assert from "node:assert/strict";
import path from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";
import { parseLineSpec, toolReadRange } from "../lib/htools.mjs";
import { runExploreExec } from "../lib/rt-explore-runtime.mjs";

const FIXTURE = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../fixtures/search-mini-repo");

test("parseLineSpec parses comma-separated ranges", () => {
  assert.deepEqual(parseLineSpec("10-40,80"), [[10, 40], [80, 80]]);
});

test("toolReadRange returns numbered lines for a range", () => {
  const r = toolReadRange(FIXTURE, "hooks/gate_stop.py", { start_line: 1, end_line: 3 });
  assert.equal(r.ok, true);
  assert.match(r.content, /^1\|/m);
  assert.equal(r.start_line, 1);
});

test("runExploreExec grep returns hits array", async () => {
  const r = await runExploreExec(
    FIXTURE,
    `
const hits = await tools.grep({ pattern: "adjudicate" });
const found = hits.hits.find(h => h.path.endsWith("gate_stop.py"));
return { hitCount: hits.hitCount, found: Boolean(found) };
`
  );
  assert.equal(r.ok, true);
  assert.ok(r.result.hitCount >= 1);
  assert.equal(r.result.found, true);
});

test("runExploreExec parallel grep and read", async () => {
  const reads = [];
  const r = await runExploreExec(
    FIXTURE,
    `
const [hits, entry] = await Promise.all([
  tools.grep({ pattern: "adjudicate" }),
  tools.read({ path: "hooks/gate_stop.py", start_line: 1, end_line: 5 }),
]);
return { hitCount: hits.hitCount, entryPath: entry.path, hasPreview: Boolean(entry.preview) };
`,
    { onRead: (rel) => reads.push(rel) }
  );
  assert.equal(r.ok, true);
  assert.ok(r.result.hitCount >= 1);
  assert.equal(r.result.entryPath, "hooks/gate_stop.py");
  assert.equal(r.result.hasPreview, true);
  assert.ok(reads.includes("hooks/gate_stop.py"));
});

test("runExploreExec batch_read tracks reads with slim paths", async () => {
  const reads = [];
  const r = await runExploreExec(
    FIXTURE,
    `
const out = await tools.batch_read({
  reads: [
    { path: "hooks/gate_stop.py", start_line: 1, end_line: 2 },
    { path: "README.md", start_line: 1, end_line: 2 },
  ],
});
return { count: out.count, paths: out.paths.map(p => p.path) };
`,
    { onRead: (rel) => reads.push(rel) }
  );
  assert.equal(r.ok, true);
  assert.equal(r.result.count, 2);
  assert.deepEqual(reads.sort(), ["README.md", "hooks/gate_stop.py"].sort());
});

test("runExploreExec shell rg works", async () => {
  const r = await runExploreExec(
    FIXTURE,
    `
const out = await tools.shell({ command: "rg -n adjudicate hooks" });
return { exitCode: out.exitCode, hasMatch: out.stdout_preview.includes("adjudicate") };
`
  );
  assert.equal(r.ok, true);
  assert.equal(r.result.exitCode, 0);
  assert.equal(r.result.hasMatch, true);
});

test("runExploreExec rejects empty code", async () => {
  const r = await runExploreExec(FIXTURE, "   ");
  assert.equal(r.ok, false);
});

test("runExploreExec preflight rejects invalid syntax", async () => {
  const r = await runExploreExec(FIXTURE, "const [x, = 1;");
  assert.equal(r.ok, false);
  assert.match(r.error, /syntax/i);
});

test("runExploreExec times out", async () => {
  const r = await runExploreExec(
    FIXTURE,
    `
await new Promise((resolve) => setTimeout(resolve, 500));
return "late";
`,
    { deadlineMs: Date.now() + 50 }
  );
  assert.equal(r.ok, false);
  assert.match(r.error, /timed out/i);
});

test("runExploreExec caps oversized result with summary", async () => {
  const prev = process.env.EXPLORE_RT_EXEC_RESULT_MAX;
  process.env.EXPLORE_RT_EXEC_RESULT_MAX = "2000";
  try {
    const r = await runExploreExec(
      FIXTURE,
      `
return { items: Array.from({ length: 200 }, (_, i) => ({ path: "hooks/gate_stop.py", note: "x".repeat(200), i })) };
`
    );
    assert.equal(r.ok, true);
    assert.equal(r.result.truncated, true);
    assert.ok(r.result.summary || r.result.pathsSeen);
  } finally {
    if (prev === undefined) delete process.env.EXPLORE_RT_EXEC_RESULT_MAX;
    else process.env.EXPLORE_RT_EXEC_RESULT_MAX = prev;
  }
});

test("runExploreExec grep shape error includes hint", async () => {
  const r = await runExploreExec(
    FIXTURE,
    `
const g = await tools.grep({ pattern: "adjudicate" });
return g.find(x => x);
`
  );
  assert.equal(r.ok, false);
  assert.match(r.error, /is not a function/i);
  assert.match(r.hint, /hits\.find/i);
});
