import assert from "node:assert/strict";
import test from "node:test";
import {
  aggregateScores,
  hitAtK,
  lineIou,
  recommendMode,
  scoreSearchResult,
} from "../lib/bench-scorer.mjs";

test("hitAtK detects expected path", () => {
  const paths = ["hooks/gate_stop.py", "scripts/gate/spec.py"];
  assert.equal(hitAtK(paths, "hooks/gate_stop.py", 1), true);
  assert.equal(hitAtK(paths, "scripts/gate/spec.py", 1), false);
  assert.equal(hitAtK(paths, "scripts/gate/spec.py", 5), true);
});

test("lineIou computes overlap", () => {
  assert.equal(lineIou({ startLine: 1, endLine: 10, path: "a.py" }, { startLine: 5, endLine: 15, path: "a.py" }), 6 / 15);
});

test("recommendMode picks tandem when criteria met", () => {
  const summary = {
    none: { hit1: 0.5, medianTotalMs: 1000 },
    pagerank: { hit1: 0.55, medianTotalMs: 1200 },
    sigmap: { hit1: 0.52, medianTotalMs: 900 },
    tandem: { hit1: 0.6, medianTotalMs: 1800 },
  };
  const rec = recommendMode(summary);
  assert.equal(rec.pick, "tandem");
});

test("aggregateScores averages metrics", () => {
  const agg = aggregateScores([
    { hit1: true, hit5: true, lineIou: 1, empty: false, mapMs: 10, searchMs: 20 },
    { hit1: false, hit5: true, lineIou: 0.5, empty: false, mapMs: 20, searchMs: 30 },
  ]);
  assert.equal(agg.count, 2);
  assert.equal(agg.hit1, 0.5);
});
