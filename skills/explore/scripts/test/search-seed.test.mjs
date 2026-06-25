import assert from "node:assert/strict";
import path from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";
import {
  codeSymbolIdents,
  pickDefHit,
  seedSearchHits,
} from "../search-seed.mjs";

const FIXTURE = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../fixtures/search-mini-repo");

test("codeSymbolIdents keeps code symbols, drops plain English", () => {
  const idents = codeSymbolIdents("how does adjudicate_dispute handle the gateStop and FAIL_OPEN cases");
  assert.ok(idents.includes("adjudicate_dispute"));
  assert.ok(idents.includes("gateStop"));
  assert.ok(idents.includes("FAIL_OPEN"));
  assert.ok(!idents.includes("does"));
  assert.ok(!idents.includes("handle"));
});

test("pickDefHit scores a declaration above a call site", () => {
  const matches = [
    { line: 10, text: "    adjudicate_dispute(task)" },
    { line: 4, text: "def adjudicate_dispute(task):" },
  ];
  const best = pickDefHit(matches, "adjudicate_dispute");
  assert.equal(best.line, 4);
  assert.ok(best.score >= 3);
});

test("seedSearchHits hydrates the definition window for a query symbol", () => {
  const seeds = seedSearchHits(FIXTURE, "where is adjudicate_dispute defined");
  assert.ok(seeds.length >= 1);
  const hit = seeds.find((s) => s.path.endsWith("gate_stop.py"));
  assert.ok(hit, "expected a seed in gate_stop.py");
  assert.ok(hit.content.includes("def adjudicate_dispute"));
  assert.ok(hit.startLine <= 4 && hit.endLine >= 4);
});

test("seedSearchHits strips preamble (comments/shebang/docstring) by default", () => {
  const seeds = seedSearchHits(FIXTURE, "where is adjudicate_dispute defined");
  const hit = seeds.find((s) => s.path.endsWith("gate_stop.py"));
  assert.ok(hit, "expected a seed in gate_stop.py");
  // Code survives; shebang, docstring, and the inline comment are stripped.
  assert.ok(hit.content.includes("def adjudicate_dispute"));
  assert.ok(!hit.content.includes("#!/usr/bin/env"));
  assert.ok(!hit.content.includes("fail open on internal errors"));
  assert.ok(!/Stop hook: adjudicates/.test(hit.content));
});

test("seedSearchHits keeps preamble when stripping disabled", () => {
  const prev = process.env.EXPLORE_SEARCH_STRIP_COMMENTS;
  process.env.EXPLORE_SEARCH_STRIP_COMMENTS = "0";
  try {
    const seeds = seedSearchHits(FIXTURE, "where is adjudicate_dispute defined");
    const hit = seeds.find((s) => s.path.endsWith("gate_stop.py"));
    assert.ok(hit && hit.content.includes("fail open on internal errors"));
  } finally {
    if (prev === undefined) delete process.env.EXPLORE_SEARCH_STRIP_COMMENTS;
    else process.env.EXPLORE_SEARCH_STRIP_COMMENTS = prev;
  }
});

test("seedSearchHits returns empty when disabled", () => {
  const prev = process.env.EXPLORE_SEARCH_SEED;
  process.env.EXPLORE_SEARCH_SEED = "0";
  try {
    assert.deepEqual(seedSearchHits(FIXTURE, "adjudicate_dispute"), []);
  } finally {
    if (prev === undefined) delete process.env.EXPLORE_SEARCH_SEED;
    else process.env.EXPLORE_SEARCH_SEED = prev;
  }
});

test("seedSearchHits returns empty when query has no code symbols", () => {
  assert.deepEqual(seedSearchHits(FIXTURE, "how does the thing work"), []);
});
