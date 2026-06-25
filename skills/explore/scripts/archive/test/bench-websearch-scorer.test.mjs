import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import {
  aggregateWebsearchScores,
  nextStepsSection,
  scoreWebsearchOutput,
  sectionScore,
} from "../lib/bench-websearch-scorer.mjs";

const SAMPLE = `# Report

### 1. Executive Summary
Brief.

### 2. In-scope findings
- Item https://modelcontextprotocol.io/docs

### 3. Adjacent / out-of-scope
- SWE-ReX

### 4. Prior art / GitHub repos
- https://github.com/example/x

### 5. Gaps, risks, or conflicting claims
- stale indexes

### 6. Recommended next steps
- Add LSP bridge when go-to-def needed
`;

test("sectionScore counts prompt sections", () => {
  assert.equal(sectionScore(SAMPLE), 6);
});

test("scoreWebsearchOutput passes wire format sample", () => {
  const wire = readFileSync(
    new URL("../fixtures/explore-wire-samples/websearch-wire.txt", import.meta.url),
    "utf8",
  );
  const score = scoreWebsearchOutput(wire, { minUrls: 1, minSections: 4 });
  assert.equal(score.wireFormat, true);
  assert.ok(score.urlCount >= 2);
  assert.ok(score.sections >= 4);
  assert.equal(score.pass, true);
});

test("scoreWebsearchOutput passes well-formed MCP answer", () => {
  const s = scoreWebsearchOutput(SAMPLE, {
    minUrls: 2,
    urlPatterns: ["modelcontextprotocol"],
    minSections: 4,
  });
  assert.equal(s.pass, true);
  assert.equal(s.urlCount, 2);
});

test("scoreWebsearchOutput fails forbidden next-step patterns", () => {
  const bad = SAMPLE.replace(
    "Add LSP bridge",
    "integrate ast-grep into search.sh",
  );
  const s = scoreWebsearchOutput(bad, {
    minUrls: 2,
    forbiddenNextStepPatterns: ["integrate ast-grep"],
  });
  assert.equal(s.scopeOk, false);
  assert.equal(s.pass, false);
});

test("nextStepsSection extracts tail", () => {
  assert.match(nextStepsSection(SAMPLE), /LSP bridge/);
});

test("aggregateWebsearchScores summarizes rows", () => {
  const agg = aggregateWebsearchScores([
    { pass: true, empty: false, urlCount: 3, sections: 6, scopeOk: true, websearchMs: 1000 },
    { pass: false, empty: false, urlCount: 1, sections: 3, scopeOk: true, websearchMs: 2000 },
  ]);
  assert.equal(agg.passRate, 0.5);
  assert.equal(agg.medianWebsearchMs, 1500);
});
