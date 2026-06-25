import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";
import {
  aggregateTraceScores,
  annotateGrounding,
  classifyCitationGrounded,
  computeQualityIndex,
  extractTraceCitations,
  scoreTraceOutput,
  scoreTraceSections,
} from "../lib/bench-trace-scorer.mjs";

const FIXTURES = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../fixtures/bench-trace-samples",
);

function readFixture(name) {
  return fs.readFileSync(path.join(FIXTURES, name), "utf8");
}

test("extractTraceCitations counts line-start fences", () => {
  const md = readFixture("cursor-line-fences.md");
  const c = extractTraceCitations(md);
  assert.ok(c.uniqueCitations >= 10);
  assert.ok(c.byKind.lineStartFence >= 10);
});

test("extractTraceCitations counts inline fences for gemini", () => {
  const md = readFixture("gemini-inline-fences.md");
  const c = extractTraceCitations(md);
  assert.ok(c.uniqueCitations >= 8);
  assert.ok(c.byKind.inlineFence >= 8);
});

test("extractTraceCitations counts path-first refs", () => {
  const md = readFixture("gemini-path-first.md");
  const c = extractTraceCitations(md);
  assert.ok(c.uniqueCitations >= 4);
  assert.ok(c.byKind.pathFirst >= 3);
});

test("scoreTraceSections counts headings and semantic markers", () => {
  const cursor = scoreTraceSections(readFixture("cursor-line-fences.md"));
  assert.ok(cursor.sectionScore >= 5);
  const gemini = scoreTraceSections(readFixture("gemini-path-first.md"));
  assert.ok(gemini.sectionScore >= 2);
});

test("dedup identical citation ranges", () => {
  const md = "see ```1:2:scripts/a.sh``` and again ```1:2:scripts/a.sh```";
  const c = extractTraceCitations(md);
  assert.equal(c.uniqueCitations, 1);
});

test("structured json sidecar merges citations", () => {
  const tmp = path.join(FIXTURES, "..", "..", ".cache", "bench-trace-scorer-test.json");
  fs.mkdirSync(path.dirname(tmp), { recursive: true });
  fs.writeFileSync(
    tmp,
    JSON.stringify({
      code_passages: [
        { file_path: "scripts/foo.mjs", start_line: 10, end_line: 20 },
      ],
    }),
  );
  const score = scoreTraceOutput({
    markdown: "minimal",
    structuredJsonPath: tmp,
  });
  assert.equal(score.uniqueCitations, 1);
  assert.equal(score.citeStructured, 1);
});

test("computeQualityIndex weighted blend", () => {
  const q = computeQualityIndex({
    uniqueCitations: 20,
    sectionScore: 8,
    completenessScore: 3,
    uniquePaths: 8,
  });
  assert.equal(q, 100);
});

test("computeQualityIndex rewards grounded over header-only citations", () => {
  const base = { uniqueCitations: 4, sectionScore: 8, completenessScore: 3, uniquePaths: 4 };
  const grounded = computeQualityIndex({ ...base, groundedCitations: 4 });
  const headerOnly = computeQualityIndex({ ...base, groundedCitations: 0 });
  assert.ok(grounded > headerOnly, `grounded ${grounded} should beat header-only ${headerOnly}`);
  // Omitting groundedCitations must preserve the legacy (all-grounded) score.
  assert.equal(computeQualityIndex(base), grounded);
});

test("classifyCitationGrounded: shape strategy rejects 1:3 file-top spans", () => {
  const header = { path: "scripts/x.mjs", startLine: 1, endLine: 3 };
  const body = { path: "scripts/x.mjs", startLine: 40, endLine: 60 };
  assert.equal(classifyCitationGrounded(header, { strategy: "shape" }), false);
  assert.equal(classifyCitationGrounded(body, { strategy: "shape" }), true);
});

test("annotateGrounding counts grounded vs header-only", () => {
  const info = annotateGrounding(
    [
      { path: "a.mjs", startLine: 1, endLine: 3 },
      { path: "a.mjs", startLine: 50, endLine: 70 },
    ],
    { strategy: "shape" },
  );
  assert.equal(info.groundedCitations, 1);
  assert.equal(info.ungroundedCitations, 1);
  assert.equal(info.groundednessRatio, 0.5);
});

test("scoreTraceOutput: header-only citation scores below a real-code span (content)", () => {
  const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
  const writeJson = (name, passages) => {
    const tmp = path.join(root, ".cache", name);
    fs.mkdirSync(path.dirname(tmp), { recursive: true });
    fs.writeFileSync(tmp, JSON.stringify({ code_passages: passages }));
    return tmp;
  };
  // htools.mjs lines 1-3 are header (comment/import); a mid-file span is real code.
  const headerJson = writeJson("grounding-header.json", [
    { file_path: "scripts/lib/htools.mjs", start_line: 1, end_line: 3 },
  ]);
  const realJson = writeJson("grounding-real.json", [
    { file_path: "scripts/lib/htools.mjs", start_line: 134, end_line: 160 },
  ]);
  const md = "## Flow\n## Key files\n## Code references";
  const header = scoreTraceOutput({ markdown: md, structuredJsonPath: headerJson, workspace: root, grounding: "content" });
  const real = scoreTraceOutput({ markdown: md, structuredJsonPath: realJson, workspace: root, grounding: "content" });
  assert.equal(header.groundedCitations, 0);
  assert.equal(real.groundedCitations, 1);
  assert.ok(real.qualityIndex > header.qualityIndex, `real ${real.qualityIndex} should beat header ${header.qualityIndex}`);
});

test("aggregateTraceScores medians", () => {
  const agg = aggregateTraceScores([
    { empty: false, wallS: 10, chars: 1000, uniqueCitations: 5, sectionScore: 4, completenessScore: 2, qualityIndex: 50 },
    { empty: false, wallS: 20, chars: 2000, uniqueCitations: 10, sectionScore: 6, completenessScore: 3, qualityIndex: 70 },
  ]);
  assert.equal(agg.medianWallS, 15);
  assert.equal(agg.medianUniqueCitations, 7.5);
});

test("render-trace-structured consumer stays compatible with extractTraceCitations", async () => {
  // render-trace-structured.mjs:countRenderedCitations reads byKind.{lineStartFence,
  // refLabel,inlineFence,pathFirst} and uniqueCitations from extractTraceCitations.
  const { countRenderedCitations } = await import("../lib/render-trace-structured.mjs");
  const md = "ref <ref1>\n```1:3:scripts/a.sh```\nand again ```5:9:scripts/b.mjs```";
  const c = countRenderedCitations(md);
  for (const key of ["lineStart", "refs", "inline", "pathFirst", "total"]) {
    assert.ok(key in c, `countRenderedCitations missing ${key}`);
    assert.equal(typeof c[key], "number");
  }
  assert.ok(c.total >= 1);
});

test("scoreTraceOutput scores wire file tokens", () => {
  const wire = fs.readFileSync(
    path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../fixtures/explore-wire-samples/trace-wire.txt"),
    "utf8",
  );
  const score = scoreTraceOutput({ raw: wire });
  assert.equal(score.wireFormat, true);
  assert.ok(score.uniqueCitations >= 3);
  assert.ok(score.sectionScore >= 3);
});

test("scoreTraceOutput on fixtures meets ballpark", () => {
  const cursor = scoreTraceOutput({ markdown: readFixture("cursor-line-fences.md") });
  assert.ok(cursor.uniqueCitations >= 10);
  assert.ok(cursor.sectionScore >= 5);

  const geminiInline = scoreTraceOutput({ markdown: readFixture("gemini-inline-fences.md") });
  assert.ok(geminiInline.uniqueCitations >= 8);
  assert.ok(geminiInline.sectionScore >= 2);

  const geminiPath = scoreTraceOutput({ markdown: readFixture("gemini-path-first.md") });
  assert.ok(geminiPath.uniqueCitations >= 4);
  assert.ok(geminiPath.sectionScore >= 2);
});
