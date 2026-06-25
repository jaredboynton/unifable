import assert from "node:assert/strict";
import path from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";
import { generatePagerankMap, runPagerank, buildPagerankGraph, buildTagIndex } from "../map-pagerank.mjs";
import { listRepoFiles } from "../map-lib.mjs";

const FIXTURE = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../fixtures/search-mini-repo");

test("pagerank graph returns ranked defs", () => {
  const files = listRepoFiles(FIXTURE);
  const tags = buildTagIndex(FIXTURE, files);
  assert.ok(tags.length > 0);
  const graph = buildPagerankGraph(tags, "adjudicate dispute");
  const ranked = runPagerank(graph);
  assert.ok(ranked.length > 0);
});

test("generatePagerankMap includes fixture paths", () => {
  const out = generatePagerankMap(FIXTURE, "disputes on stop", { budgetTokens: 512 });
  assert.match(out, /pagerank/);
  assert.match(out, /gate_stop|spec/);
});
