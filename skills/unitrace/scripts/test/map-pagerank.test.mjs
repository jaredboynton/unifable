import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";
import { generatePagerankMap, runPagerank, buildPagerankGraph, buildTagIndex } from "../map-pagerank.mjs";
import { generateMapText } from "../map.mjs";
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

// Reference oracle: the original O(edges x definitions) ranking, kept only here
// to lock that the O(E + D) rewrite in runPagerank produces identical output.
function powerIterReference(graph, { iterations = 20, damping = 0.85 } = {}) {
  const { outEdges, nodes, personalize } = graph;
  const rank = new Map(nodes.map((n) => [n, 1 / nodes.length]));
  const persSum = [...personalize.values()].reduce((a, b) => a + b, 0) || 0;
  const persNorm = new Map();
  for (const n of nodes) persNorm.set(n, persSum ? (personalize.get(n) || 0) / persSum : 1 / nodes.length);
  for (let i = 0; i < iterations; i += 1) {
    const next = new Map();
    for (const node of nodes) next.set(node, (1 - damping) * (persNorm.get(node) || 0));
    for (const src of nodes) {
      const srcRank = rank.get(src) || 0;
      const outs = outEdges.get(src);
      if (!outs || outs.size === 0) {
        const share = (damping * srcRank) / nodes.length;
        for (const n of nodes) next.set(n, (next.get(n) || 0) + share);
        continue;
      }
      let total = 0;
      for (const w of outs.values()) total += w;
      for (const [dst, w] of outs) next.set(dst, (next.get(dst) || 0) + (damping * srcRank * w) / total);
    }
    for (const n of nodes) rank.set(n, next.get(n) || 0);
  }
  return rank;
}

function rankOldReference(graph) {
  const { outEdges, nodes, definitions } = graph;
  const rank = powerIterReference(graph);
  const rankedDefs = [];
  for (const src of nodes) {
    const srcRank = rank.get(src) || 0;
    const outs = outEdges.get(src);
    if (!outs) continue;
    let total = 0;
    for (const w of outs.values()) total += w;
    for (const [dst, w] of outs) {
      const edgeRank = total ? (srcRank * w) / total : 0;
      for (const [key, tags] of definitions) {
        const [rel, name] = key.split("\0");
        if (rel !== dst) continue;
        for (const tag of tags) rankedDefs.push({ rel, name, line: tag.line, kind: tag.kind, score: edgeRank });
      }
    }
  }
  rankedDefs.sort((a, b) => b.score - a.score || a.rel.localeCompare(b.rel) || a.line - b.line);
  const seen = new Set();
  const out = [];
  for (const it of rankedDefs) {
    const k = `${it.rel}:${it.name}:${it.line}`;
    if (seen.has(k)) continue;
    seen.add(k);
    out.push(it);
  }
  return out;
}

const sig = (r) => r.map((x) => `${x.rel}:${x.name}:${x.line}`).join("|");

test("O(E+D) ranking matches the O(E x D) reference output exactly", () => {
  const files = listRepoFiles(FIXTURE);
  const tags = buildTagIndex(FIXTURE, files);
  const graph = buildPagerankGraph(tags, "adjudicate dispute on stop");
  const got = runPagerank(graph);
  const want = rankOldReference(graph);
  assert.ok(got.length > 0);
  assert.equal(got.length, want.length);
  assert.equal(sig(got), sig(want));
  // file-max invariant: every definition in a file carries that file's score.
  const byFile = new Map();
  for (const d of got) {
    if (byFile.has(d.rel)) assert.equal(d.score, byFile.get(d.rel));
    else byFile.set(d.rel, d.score);
  }
});

test("buildPagerankGraph flags over-cap graphs and runPagerank skips ranking", () => {
  const files = listRepoFiles(FIXTURE);
  const tags = buildTagIndex(FIXTURE, files);
  const ok = buildPagerankGraph(tags, "dispute", { maxNodes: 100000, maxEdges: 100000000 });
  assert.equal(ok.overCap, false);
  assert.ok(runPagerank(ok).length > 0);
  const capped = buildPagerankGraph(tags, "dispute", { maxNodes: 1 });
  assert.equal(capped.overCap, true);
  assert.deepEqual(runPagerank(capped), []);
  const edgeCapped = buildPagerankGraph(tags, "dispute", { maxEdges: 0 });
  assert.equal(edgeCapped.overCap, true);
  assert.deepEqual(runPagerank(edgeCapped), []);
});

test("ranking stays linear on a dense synthetic graph (no O(E x D) regression)", () => {
  // ~400 files all referencing a shared symbol set => tens of thousands of edges.
  // The old nested scan would take seconds here; the linear path must not.
  const tags = [];
  const shared = Array.from({ length: 40 }, (_, k) => `shared_symbol_${k}`);
  for (let i = 0; i < 400; i += 1) {
    const rel = `src/mod_${i}.js`;
    tags.push({ rel, line: 1, name: `localDef_${i}`, kind: "def" });
    for (const s of shared) {
      tags.push({ rel, line: 2, name: s, kind: i % 7 === 0 ? "def" : "ref" });
    }
  }
  const graph = buildPagerankGraph(tags, "shared_symbol_3");
  const t0 = process.hrtime.bigint();
  const ranked = runPagerank(graph);
  const ms = Number(process.hrtime.bigint() - t0) / 1e6;
  assert.ok(ranked.length > 0);
  assert.ok(ms < 1000, `ranking took ${ms.toFixed(1)}ms, expected < 1000ms`);
});

test("generateMapText bails on a huge non-git tree (fail-open to empty)", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "map-bail-"));
  try {
    for (let i = 0; i < 6; i += 1) {
      fs.writeFileSync(path.join(dir, `f${i}.js`), `export function fn${i}(){ return ${i}; }\n`);
    }
    // Cap below the file count on a non-git dir => pathological signal => bail.
    const prev = process.env.UNITRACE_MAP_MAX_FILES;
    process.env.UNITRACE_MAP_MAX_FILES = "3";
    try {
      const bailed = await generateMapText(dir, "fn", { mode: "tandem", noCache: true });
      assert.equal(bailed.text, "");
      assert.equal(bailed.skipped, "huge-non-git");
      // Override lets the same tree map.
      process.env.UNITRACE_MAP_ALLOW_HUGE = "1";
      const mapped = await generateMapText(dir, "fn", { mode: "sigmap", noCache: true });
      assert.notEqual(mapped.skipped, "huge-non-git");
    } finally {
      if (prev === undefined) delete process.env.UNITRACE_MAP_MAX_FILES;
      else process.env.UNITRACE_MAP_MAX_FILES = prev;
      delete process.env.UNITRACE_MAP_ALLOW_HUGE;
    }
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});
