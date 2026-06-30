import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import {
  DOC_SCORE_INSTRUCTIONS,
  SCORE_INSTRUCTIONS,
  fileClass,
  retrieveCandidates,
  runFastPath,
} from "../search-fast.mjs";
import { verdict } from "../bench/search-multiformat-ab.mjs";

// AST install is network/slow; the line-window fallback covers code here.
process.env.UNITRACE_AST_SKIP_INSTALL = "1";

function makeRepo() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "uni-multifmt-"));
  fs.writeFileSync(
    path.join(dir, "guide.md"),
    "# Project guide\n\nThe frobnicate option enables fast retries.\nUnrelated trailing prose.\n",
  );
  fs.writeFileSync(path.join(dir, "settings.json"), '{\n  "frobnicate": true\n}\n');
  fs.writeFileSync(path.join(dir, "core.py"), "def frobnicate():\n    return 1\n");
  fs.writeFileSync(path.join(dir, "logo.png"), "not really a png but excluded by ext\n");
  return dir;
}

test("fileClass routes code, docs/data, and excludes binaries/fixtures", () => {
  assert.equal(fileClass("src/foo.py"), "code");
  assert.equal(fileClass("a/b/core.ts"), "code");
  assert.equal(fileClass("AGENTS.md"), "doc");
  assert.equal(fileClass("docs/guide.md"), "doc");
  assert.equal(fileClass("config/app.json"), "doc");
  assert.equal(fileClass("data/x.yaml"), "doc");
  assert.equal(fileClass("README"), "doc");
  assert.equal(fileClass("Dockerfile"), "doc");
  assert.equal(fileClass("logo.png"), null);
  assert.equal(fileClass("deps.lock"), null);
  assert.equal(fileClass("bundle.min.js"), null);
  assert.equal(fileClass("pkg/fixtures/sample.md"), null);
  // Lockfiles (json/yaml-named) are machine output, never an answer.
  assert.equal(fileClass("package-lock.json"), null);
  assert.equal(fileClass("pnpm-lock.yaml"), null);
  assert.equal(fileClass("npm-shrinkwrap.json"), null);
  // Secrets excluded even though --hidden would surface them.
  assert.equal(fileClass(".env"), null);
  assert.equal(fileClass(".env.local"), null);
  assert.equal(fileClass("config/.env.production"), null);
  assert.equal(fileClass("server.pem"), null);
  assert.equal(fileClass("deploy.key"), null);
  // .env templates carry no real values -> searchable.
  assert.equal(fileClass(".env.example"), "doc");
  assert.equal(fileClass("config/.env.sample"), "doc");
});

test("retrieval surfaces hidden config/templates but never real secrets", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "uni-hidden-"));
  try {
    fs.writeFileSync(path.join(dir, ".env.example"), "FROBNICATE_MODE=fast\n");
    fs.writeFileSync(path.join(dir, ".env"), "FROBNICATE_MODE=secretvalue\n");
    fs.mkdirSync(path.join(dir, ".github", "workflows"), { recursive: true });
    fs.writeFileSync(path.join(dir, ".github", "workflows", "ci.yml"), "name: frobnicate-ci\n");
    const { candidates } = await retrieveCandidates(dir, "frobnicate mode");
    assert.ok(candidates.some((c) => c.path.endsWith(".env.example")), "hidden template should be reachable via --hidden");
    assert.ok(candidates.some((c) => c.path.endsWith("ci.yml")), "hidden .github workflow should be reachable");
    assert.ok(!candidates.some((c) => c.path.endsWith("/.env") || c.path === ".env"), "real .env must never become a candidate");
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("doc span floor: a doc answer survives a flood of higher-scoring code spans", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "uni-starve-"));
  try {
    for (let i = 0; i < 6; i++) {
      fs.writeFileSync(path.join(dir, `mod${i}.py`), `def frobnicate_${i}():\n    return frobnicate_${i}\n`);
    }
    fs.writeFileSync(path.join(dir, "NOTES.md"), "# Notes\n\nThe frobnicate flag toggles fast mode.\n");
    const { candidates } = await retrieveCandidates(dir, "frobnicate", { maxSpans: 2 });
    assert.equal(candidates.length, 2);
    assert.ok(candidates.some((c) => c.path.endsWith("NOTES.md")), "doc span must be reserved against code starvation");
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("retrieveCandidates surfaces doc, data, AND code files with class tags", async () => {
  const dir = makeRepo();
  try {
    const { candidates } = await retrieveCandidates(dir, "the frobnicate option");
    const md = candidates.find((c) => c.path.endsWith("guide.md"));
    const json = candidates.find((c) => c.path.endsWith("settings.json"));
    const py = candidates.find((c) => c.path.endsWith("core.py"));
    assert.ok(md, "markdown answer should be a candidate");
    assert.equal(md.cls, "doc");
    assert.ok(md.content.includes("frobnicate"));
    assert.ok(json, "json answer should be a candidate");
    assert.equal(json.cls, "doc");
    assert.ok(py, "code answer should be a candidate");
    assert.equal(py.cls, "code");
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("runFastPath finds a markdown-only answer and scores it with the doc rubric", async () => {
  const dir = makeRepo();
  try {
    let captured = [];
    const daemon = {
      warmDaemonPool: async () => {},
      daemonAsk: async () => null,
      daemonAskBatch: async (_ns, requests) => {
        captured = requests;
        return requests.map((r) => ({ score: /guide\.md/.test(r.user) ? 9 : 1 }));
      },
    };
    const files = await runFastPath(dir, "the frobnicate option", { daemon });
    assert.ok(Array.isArray(files), "expected a finish file list, not a fallback");
    assert.ok(files.some((f) => f.path.endsWith("guide.md")), "markdown answer should be returned");

    const mdReq = captured.find((r) => /guide\.md/.test(r.user));
    const pyReq = captured.find((r) => /core\.py/.test(r.user));
    assert.equal(mdReq.system, DOC_SCORE_INSTRUCTIONS, "doc candidate must use the doc rubric");
    if (pyReq) assert.equal(pyReq.system, SCORE_INSTRUCTIONS, "code candidate must use the code rubric");
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("runFastPath null-fallback: nothing clears the floor -> null by default, [] when disabled", async () => {
  const dir = makeRepo();
  try {
    const daemonLow = {
      warmDaemonPool: async () => {},
      daemonAsk: async () => null,
      daemonAskBatch: async (_ns, requests) => requests.map(() => ({ score: 1 })),
    };
    const def = await runFastPath(dir, "the frobnicate option", { daemon: daemonLow });
    assert.equal(def, null, "default should fall back to the agentic loop");

    const prev = process.env.UNITRACE_SEARCH_FAST_NULL_FALLBACK;
    process.env.UNITRACE_SEARCH_FAST_NULL_FALLBACK = "0";
    try {
      const off = await runFastPath(dir, "the frobnicate option", { daemon: daemonLow });
      assert.deepEqual(off, [], "with fallback disabled, a scored-but-no-survivor pool returns []");
    } finally {
      if (prev === undefined) delete process.env.UNITRACE_SEARCH_FAST_NULL_FALLBACK;
      else process.env.UNITRACE_SEARCH_FAST_NULL_FALLBACK = prev;
    }
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("runFastPath A1 tiebreak: equal model scores fall back to retrieval score, not path order", async () => {
  // Two files tie at the same model score. The alphabetically-LATER file
  // (zeta.py) carries the stronger retrieval signal (more distinct rare terms),
  // so it must rank #1 -- proving the tiebreak uses the retrieval score, not
  // a[0].localeCompare(b[0]) which would wrongly put alpha.py first.
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "uni-tiebreak-"));
  try {
    // alpha.py: single matching term -> lower retrieval score.
    fs.writeFileSync(path.join(dir, "alpha.py"), "def widget():\n    return 1\n");
    // zeta.py: two distinct matching terms -> higher retrieval score.
    fs.writeFileSync(path.join(dir, "zeta.py"), "def widget_sprocket():\n    return sprocket\n");
    const daemon = {
      warmDaemonPool: async () => {},
      daemonAsk: async () => null,
      // Score BOTH survivors identically so the model score cannot break the tie.
      daemonAskBatch: async (_ns, requests) => requests.map(() => ({ score: 8 })),
    };
    const files = await runFastPath(dir, "widget sprocket", { daemon });
    assert.ok(Array.isArray(files) && files.length > 0, "expected a finish list");
    assert.ok(files[0].path.endsWith("zeta.py"), `expected zeta.py first by retrieval score, got ${files[0].path}`);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("UNITRACE_SEARCH_FAST_EXCLUDE keeps a directory out of retrieval", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "uni-exclude-"));
  try {
    fs.mkdirSync(path.join(dir, "bench", "queries"), { recursive: true });
    // The contaminant: a file naming the query verbatim, lexically strong.
    fs.writeFileSync(path.join(dir, "bench", "queries", "set.jsonl"), '{"query":"frobnicate option enabled","gold":"core.py"}\n');
    fs.writeFileSync(path.join(dir, "core.py"), "def frobnicate():\n    return 1\n");
    const prev = process.env.UNITRACE_SEARCH_FAST_EXCLUDE;
    process.env.UNITRACE_SEARCH_FAST_EXCLUDE = "bench";
    try {
      const { candidates } = await retrieveCandidates(dir, "frobnicate option");
      assert.ok(!candidates.some((c) => c.path.includes("bench/")), "excluded dir must not yield candidates");
      assert.ok(candidates.some((c) => c.path.endsWith("core.py")), "real file should still be a candidate");
    } finally {
      if (prev === undefined) delete process.env.UNITRACE_SEARCH_FAST_EXCLUDE;
      else process.env.UNITRACE_SEARCH_FAST_EXCLUDE = prev;
    }
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("retrieval de-prioritizes test files so the implementation is not evicted by its own suite", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "uni-testdeprio-"));
  try {
    fs.mkdirSync(path.join(dir, "scripts"), { recursive: true });
    fs.mkdirSync(path.join(dir, "tests"), { recursive: true });
    // The implementation: the definition of the searched symbol.
    fs.writeFileSync(path.join(dir, "scripts", "widget.py"), "def widget_handler():\n    return widget_handler\n");
    // The test file: exercises the symbol via many calls (refs), as real suites do.
    fs.writeFileSync(
      path.join(dir, "tests", "test_widget.py"),
      "def test_widget():\n" + Array.from({ length: 12 }, () => "    assert widget_handler()\n").join(""),
    );
    const { candidates } = await retrieveCandidates(dir, "widget handler");
    const implIdx = candidates.findIndex((c) => c.path.endsWith("scripts/widget.py"));
    const testIdx = candidates.findIndex((c) => c.path.includes("test_widget.py"));
    assert.ok(implIdx >= 0, "implementation must be retrieved");
    assert.ok(implIdx < testIdx || testIdx < 0, `implementation (idx ${implIdx}) should rank above its test (idx ${testIdx})`);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

function summary(overrides) {
  return {
    name: "rtinfer",
    total: 10,
    posTotal: 8,
    negTotal: 2,
    errors: 0,
    found: 8,
    top1: 8,
    leaks: 0,
    fallback: 0,
    findRate: 100,
    top1Rate: 100,
    fallbackRate: 0,
    errorRate: 0,
    servedRtinfer: 10,
    servedDirect: 0,
    servedRate: 100,
    p50: 900,
    p95: 1200,
    negP50: 800,
    negP95: 900,
    rows: [],
    ...overrides,
  };
}

test("rtinfer-only verdict gates directly against labeled objective thresholds", () => {
  const ok = verdict([summary({})], { corpus: "multiformat" });
  assert.equal(ok.pass, true);

  const bad = verdict([summary({ top1Rate: 40 })], { corpus: "multiformat" });
  assert.equal(bad.pass, false);
  assert.match(bad.reasons.join("\n"), /top1-rate 40% < objective 90%/);
});

test("rtinfer-absent is a bounded fail-open smoke, not a full quality parity bench", () => {
  const v = verdict([
    summary({ name: "agentic-fallback", findRate: 100, top1Rate: 100, p95: 1200, servedRtinfer: 0, servedRate: 0 }),
    summary({ name: "rtinfer-absent", findRate: 0, top1Rate: 0, p95: 1300, servedRtinfer: 0, servedRate: 0 }),
  ], { corpus: "multiformat" });
  assert.equal(v.pass, true);

  const served = verdict([summary({ name: "rtinfer-absent", servedRtinfer: 1, servedRate: 100 })], { corpus: "multiformat" });
  assert.equal(served.pass, false);
  assert.match(served.reasons.join("\n"), /served 1 via rtinfer/);
});
