import assert from "node:assert/strict";
import path from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";
import {
  NAV_SCHEMA,
  admitsDocs,
  buildNavIndex,
  dedupNavProposals,
  extractUsageSymbols,
  focusRootsFor,
  hydrateFromPaths,
  importFollowSeeds,
} from "../lib/rt-explore-nav.mjs";

const FIXTURE = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../fixtures/search-mini-repo");

test("NAV_SCHEMA shape: required keys and nested read_paths", () => {
  assert.deepEqual(NAV_SCHEMA.required, ["grep_terms", "read_paths", "done"]);
  assert.equal(NAV_SCHEMA.properties.read_paths.items.required[0], "path");
  assert.equal(NAV_SCHEMA.additionalProperties, false);
});

test("dedupNavProposals unions terms (case-insensitive) and paths, computes allDone", () => {
  const results = [
    { grep_terms: ["retry", "Backoff"], read_paths: [{ path: "a.rs", start_line: 1, end_line: 10 }], done: false },
    { grep_terms: ["RETRY", "policy"], read_paths: [{ path: "a.rs", start_line: 1, end_line: 10 }, { path: "b.rs" }], done: true },
    null,
    "garbage",
  ];
  const { terms, paths, allDone, validCount } = dedupNavProposals(results);
  assert.deepEqual(terms.map((t) => t.toLowerCase()).sort(), ["backoff", "policy", "retry"]);
  assert.equal(terms.length, 3); // retry/RETRY collapsed
  assert.equal(paths.length, 2); // a.rs:1-10 deduped, b.rs kept
  assert.equal(validCount, 2);
  assert.equal(allDone, false); // first navigator not done
});

test("dedupNavProposals allDone true only when every valid nav is done", () => {
  const { allDone } = dedupNavProposals([{ grep_terms: [], read_paths: [], done: true }, { grep_terms: [], read_paths: [], done: true }]);
  assert.equal(allDone, true);
  const empty = dedupNavProposals([]);
  assert.equal(empty.allDone, false);
});

test("hydrateFromPaths reads real files via htools and tracks them, rejects escapes", () => {
  const tracked = [];
  const onRead = (rel, content) => tracked.push({ rel, content });
  const added = hydrateFromPaths(
    FIXTURE,
    [
      { path: "hooks/gate_stop.py" },
      { path: "../../../etc/passwd" }, // confined out
      { path: "does/not/exist.py" },
    ],
    onRead,
  );
  assert.equal(added, 1);
  assert.equal(tracked.length, 1);
  assert.equal(tracked[0].rel, "hooks/gate_stop.py");
  assert.ok(tracked[0].content.length > 0);
});

test("hydrateFromPaths filters archive reads unless explicitly allowed", () => {
  const tracked = [];
  const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../../..");
  const added = hydrateFromPaths(
    repoRoot,
    [
      { path: "skills/unitrace/scripts/archive/cursor-acp-trace.mjs" },
      { path: "skills/unitrace/scripts/unitrace.sh" },
    ],
    (rel, content) => tracked.push({ rel, content }),
    { focusRoots: ["skills/unitrace/scripts"], archiveOk: false, wireOk: false },
  );
  assert.equal(added, 1);
  assert.equal(tracked[0].rel, "skills/unitrace/scripts/unitrace.sh");
});

test("buildNavIndex renders a READ INDEX with seed ordering", () => {
  const readCache = new Map([
    ["b.rs", "1|fn beta() {}\n2|  body"],
    ["a.rs", "10|fn alpha() {}\n11|  body"],
  ]);
  const idx = buildNavIndex(readCache, ["a.rs"], 14);
  assert.match(idx, /READ INDEX/);
  // seedPaths ranks a.rs first even though b.rs inserted first.
  assert.ok(idx.indexOf("a.rs") < idx.indexOf("b.rs"));
});

test("focusRootsFor widens generated src seeds to the src root", () => {
  const roots = focusRootsFor("access enforcement", [
    "gateway/src/generated/access-matrix.ts",
    "crates/app-server/src/middleware/audit.rs",
  ]);
  assert.ok(Array.isArray(roots));
  assert.ok(roots.includes("gateway/src"));
  assert.ok(roots.includes("crates/app-server/src"));
});

test("extractUsageSymbols derives function symbols from seeded excerpts", () => {
  const readCache = new Map([
    ["a.js", "10|export async function buildSubmitPacket() {}\n"],
    ["b.js", "20|const seedExploreReads = () => {}\n"],
  ]);
  const out = extractUsageSymbols(readCache, ["a.js", "b.js"], { max: 4 });
  assert.deepEqual(out.map((s) => s.symbol), ["buildSubmitPacket", "seedExploreReads"]);
});

test("admitsDocs opens the doc lane only for behavior/header/fallback framings", () => {
  assert.equal(admitsDocs("How does the rate limiter work, from route handling to limit headers or fallback behavior?"), true);
  assert.equal(admitsDocs("Trace the end-to-end request forwarding"), true);
  assert.equal(admitsDocs("How does unitrace.sh hand off to the realtime tracer?"), false);
  assert.equal(admitsDocs("Where is buildSubmitPacket defined?"), false);
});

test("importFollowSeeds follows imports from a source anchor to load-bearing files", () => {
  const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../../..");
  const anchor = "skills/unitrace/scripts/realtime-trace.mjs";
  const added = [];
  const followed = importFollowSeeds({
    workspace: repoRoot,
    question: "How does trace-rt rehydrate pointers and render the final markdown trace?",
    anchors: [anchor],
    focusRoots: focusRootsFor("How does trace-rt rehydrate pointers and render markdown?", [anchor]),
    archiveOk: false,
    wireOk: false,
    testsOk: false,
    onRead: (rel) => { if (!added.includes(rel)) added.push(rel); },
    readCache: new Map(),
  });
  // realtime-trace.mjs imports both rt-rehydrate-submit.mjs and render-trace-structured.mjs.
  assert.ok(followed.includes("skills/unitrace/scripts/lib/rt-rehydrate-submit.mjs"));
  assert.ok(followed.includes("skills/unitrace/scripts/lib/render-trace-structured.mjs"));
  // Reads are tracked and confined to the focus root.
  assert.ok(added.every((rel) => rel.startsWith("skills/unitrace/scripts/")));
});

test("importFollowSeeds is a no-op for doc-only anchors and respects the disable flag", () => {
  const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../../..");
  const docOnly = importFollowSeeds({
    workspace: repoRoot,
    question: "release flow",
    anchors: ["skills/unitrace/AGENTS.md"],
    focusRoots: [],
    archiveOk: false, wireOk: false, testsOk: false,
    onRead: () => {},
    readCache: new Map(),
  });
  assert.deepEqual(docOnly, []);
  process.env.UNITRACE_RT_IMPORT_FOLLOW = "0";
  try {
    const disabled = importFollowSeeds({
      workspace: repoRoot,
      question: "render markdown",
      anchors: ["skills/unitrace/scripts/realtime-trace.mjs"],
      focusRoots: ["skills/unitrace/scripts"],
      archiveOk: false, wireOk: false, testsOk: false,
      onRead: () => {},
      readCache: new Map(),
    });
    assert.deepEqual(disabled, []);
  } finally {
    delete process.env.UNITRACE_RT_IMPORT_FOLLOW;
  }
});
