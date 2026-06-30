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
