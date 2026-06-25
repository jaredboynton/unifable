import assert from "node:assert/strict";
import path from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";
import { extractSignatures, extractSignaturesRegex, generateSigmapMap } from "../map-sigmap.mjs";

const FIXTURE = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../fixtures/search-mini-repo");

test("extractSignatures finds python defs", () => {
  const content = "def adjudicate_dispute(task):\n    pass\n";
  const sigs = extractSignaturesRegex("hooks/gate_stop.py", content);
  assert.equal(sigs.length, 1);
  assert.equal(sigs[0].name, "adjudicate_dispute");
});

test("extractSignatures regex path with EXPLORE_MAP_AST=0", () => {
  const prev = process.env.EXPLORE_MAP_AST;
  process.env.EXPLORE_MAP_AST = "0";
  try {
    const content = "def adjudicate_dispute(task):\n    pass\n";
    const sigs = extractSignatures("hooks/gate_stop.py", content);
    assert.equal(sigs.length, 1);
    assert.equal(sigs[0].name, "adjudicate_dispute");
  } finally {
    if (prev === undefined) delete process.env.EXPLORE_MAP_AST;
    else process.env.EXPLORE_MAP_AST = prev;
  }
});

test("generateSigmapMap is deterministic", () => {
  const q = "disputes fail open";
  const a = generateSigmapMap(FIXTURE, q, { budgetTokens: 512 });
  const b = generateSigmapMap(FIXTURE, q, { budgetTokens: 512 });
  assert.equal(a, b);
  assert.match(a, /gate_stop/);
});
