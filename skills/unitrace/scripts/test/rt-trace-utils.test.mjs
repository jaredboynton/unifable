import assert from "node:assert/strict";
import test from "node:test";
import { compactMapBlock, extractMapBlock, extractQuestion, questionNeedsComparison } from "../lib/rt-trace-utils.mjs";

test("extractMapBlock anchors on REPO MAP header not inline mention", () => {
  const prompt = `Explore the codebase.
1. Orient from REPO MAP, then explore_exec.

REPO MAP:
scripts/unitrace.sh:1-40 main
scripts/gemini-trace.mjs:1-80 run

QUESTION: How does unitrace.sh work?`;
  const block = extractMapBlock(prompt);
  assert.match(block, /scripts\/unitrace\.sh:1-40/);
  assert.doesNotMatch(block, /Orient from/);
});

test("extractQuestion reads trailing QUESTION marker", () => {
  assert.equal(extractQuestion("foo\nQUESTION: bar baz"), "bar baz");
});

test("compactMapBlock keeps path:line headers only", () => {
  const compact = compactMapBlock("# map\nscripts/unitrace.sh:1-40 main def\nscripts/foo.mjs:10-20 x");
  assert.equal(compact, "scripts/unitrace.sh:1-40\nscripts/foo.mjs:10-20");
});

test("questionNeedsComparison detects contrast queries", () => {
  assert.equal(questionNeedsComparison("unitrace.sh vs trace-rt"), true);
  assert.equal(questionNeedsComparison("How does unitrace.sh work?"), false);
});
