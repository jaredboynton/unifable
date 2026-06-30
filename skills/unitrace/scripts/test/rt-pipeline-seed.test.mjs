import assert from "node:assert/strict";
import path from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";
import { pipelineSeedReads } from "../lib/rt-pipeline-seed.mjs";

const WORKSPACE = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const REPO_ROOT = path.resolve(WORKSPACE, "../..");

test("pipelineSeedReads adds submit/render helpers for deep trace-rt questions", () => {
  const filesRead = new Set();
  const seen = [];
  const added = pipelineSeedReads(
    "How does trace-rt turn a question into a final rendered trace, including submit and pointer rehydrate?",
    WORKSPACE,
    filesRead,
    (rel, content) => {
      filesRead.add(rel);
      seen.push({ rel, content });
    },
  );
  assert.ok(added.includes("scripts/lib/rt-rehydrate-submit.mjs"));
  assert.ok(added.includes("scripts/lib/render-trace-structured.mjs"));
  assert.ok(seen.every((entry) => entry.content.length > 0));
});

test("pipelineSeedReads resolves nested unitrace files from the repo root", () => {
  const filesRead = new Set();
  const added = pipelineSeedReads(
    "How does trace-rt turn a question into a final rendered trace, including submit and pointer rehydrate?",
    REPO_ROOT,
    filesRead,
    (rel) => filesRead.add(rel),
  );
  assert.ok(added.includes("skills/unitrace/scripts/lib/rt-rehydrate-submit.mjs"));
  assert.ok(added.includes("skills/unitrace/scripts/lib/render-trace-structured.mjs"));
});
