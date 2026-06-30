import assert from "node:assert/strict";
import path from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";
import { mergeProseWithPassages, pickCodePassages } from "../lib/rt-pick-passages.mjs";

const WORKSPACE = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");

test("pickCodePassages returns grounded spans from read cache", () => {
  const filesRead = new Set(["scripts/unitrace.sh"]);
  const readCache = new Map([
    ["scripts/unitrace.sh", "1|#!/usr/bin/env bash\n2|set -e\n3|# trace entry\n"],
  ]);
  const passages = pickCodePassages({
    workspace: WORKSPACE,
    filesRead,
    readCache,
    seedPaths: ["scripts/unitrace.sh"],
    question: "How does unitrace.sh work?",
    maxPassages: 3,
  });
  assert.ok(passages.length >= 1);
  assert.equal(passages[0].file_path, "scripts/unitrace.sh");
  assert.ok(passages[0].start_line >= 1);
  assert.ok(passages[0].end_line >= passages[0].start_line);
});

test("mergeProseWithPassages attaches code_passages and manifest", () => {
  const filesRead = new Set(["scripts/unitrace.sh"]);
  const merged = mergeProseWithPassages(
    { opening_summary: "summary", flow_steps: ["step"], sections: [], key_files: [] },
    [{ file_path: "scripts/unitrace.sh", start_line: 1, end_line: 3, rationale: "entry" }],
    filesRead,
    1
  );
  assert.equal(merged.code_passages.length, 1);
  assert.deepEqual(merged.grounding_manifest.files_read, ["scripts/unitrace.sh"]);
});

test("pickCodePassages prefers source files over docs for implementation questions", () => {
  const filesRead = new Set(["README.md", "scripts/unitrace.sh"]);
  const readCache = new Map([
    ["README.md", "1|# Overview\n2|Trace docs\n"],
    ["scripts/unitrace.sh", "1|#!/usr/bin/env bash\n2|exec trace-rt.sh\n"],
  ]);
  const passages = pickCodePassages({
    workspace: WORKSPACE,
    filesRead,
    readCache,
    question: "How does unitrace.sh hand off to trace-rt.sh?",
    maxPassages: 2,
  });
  assert.equal(passages[0].file_path, "scripts/unitrace.sh");
});
