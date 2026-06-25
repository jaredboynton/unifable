// map-pagerank.mjs — Aider-style def/ref graph + personalized PageRank map.

import {
  charBudgetFromTokens,
  fitRankedToBudget,
  formatMapLine,
  listRepoFiles,
  mentionedIdentsFromQuery,
  readRepoFile,
  renderMapHeader,
} from "./map-lib.mjs";
import { extractSignatures } from "./map-sigmap.mjs";

/** @typedef {{ rel: string, line: number, name: string, kind: "def"|"ref" }} Tag */

function extractRefs(content, defs) {
  const defNames = new Set(defs.map((d) => d.name));
  const refs = [];
  const identRe = /\b[A-Za-z_][A-Za-z0-9_]{2,}\b/g;
  let match;
  while ((match = identRe.exec(content)) !== null) {
    const name = match[0];
    if (!defNames.has(name)) continue;
    if (["true", "false", "null", "undefined", "None", "self", "this"].includes(name)) continue;
    const line = content.slice(0, match.index).split(/\r?\n/).length;
    refs.push({ name, line, kind: "ref" });
  }
  return refs;
}

export function extractTagsForFile(relPath, content, options = {}) {
  const defs = extractSignatures(relPath, content, options).map((s) => ({
    rel: relPath,
    line: s.line,
    name: s.name,
    kind: "def",
  }));
  const refs = extractRefs(content, defs).map((r) => ({
    rel: relPath,
    line: r.line,
    name: r.name,
    kind: "ref",
  }));
  return [...defs, ...refs];
}

export function buildTagIndex(repoRoot, files) {
  /** @type {Tag[]} */
  const all = [];
  const extractOpts = { repoRoot, fileCount: files.length };
  for (const rel of files) {
    const content = readRepoFile(repoRoot, rel);
    if (content == null) continue;
    all.push(...extractTagsForFile(rel, content, extractOpts));
  }
  return all;
}

function identStyleWeight(name) {
  let mul = 1.0;
  const isSnake = name.includes("_") && /[a-zA-Z]/.test(name);
  const isKebab = name.includes("-") && /[a-zA-Z]/.test(name);
  const isCamel = /[a-z]/.test(name) && /[A-Z]/.test(name);
  if ((isSnake || isKebab || isCamel) && name.length >= 8) mul *= 10;
  if (name.startsWith("_")) mul *= 0.1;
  return mul;
}

export function buildPagerankGraph(tags, query) {
  const defines = new Map();
  const references = new Map();
  const definitions = new Map();

  for (const tag of tags) {
    if (tag.kind === "def") {
      if (!defines.has(tag.name)) defines.set(tag.name, new Set());
      defines.get(tag.name).add(tag.rel);
      const key = `${tag.rel}\0${tag.name}`;
      if (!definitions.has(key)) definitions.set(key, []);
      definitions.get(key).push(tag);
    } else {
      if (!references.has(tag.name)) references.set(tag.name, []);
      references.get(tag.name).push(tag.rel);
    }
  }

  const mentioned = mentionedIdentsFromQuery(query);
  const relFiles = new Set(tags.map((t) => t.rel));
  const personalize = new Map();
  const basePers = relFiles.size ? 100 / relFiles.size : 1;

  for (const rel of relFiles) {
    let score = 0;
    const base = rel.split(/[/\\]/).pop()?.replace(/\.[^.]+$/, "") || "";
    const parts = new Set(rel.split(/[/\\]/));
    parts.add(base);
    for (const m of mentioned) {
      if (parts.has(m) || base.includes(m)) score += basePers;
    }
    if (score > 0) personalize.set(rel, score);
  }

  /** @type {Map<string, Map<string, number>>} */
  const outEdges = new Map();
  function addEdge(from, to, weight) {
    if (!outEdges.has(from)) outEdges.set(from, new Map());
    const m = outEdges.get(from);
    m.set(to, (m.get(to) || 0) + weight);
  }

  for (const [ident, definers] of defines) {
    if (!references.has(ident)) {
      for (const definer of definers) addEdge(definer, definer, 0.1);
    }
  }

  const idents = [...defines.keys()].filter((k) => references.has(k));
  for (const ident of idents) {
    let mul = identStyleWeight(ident);
    if (mentioned.has(ident.toLowerCase())) mul *= 10;
    if ((defines.get(ident)?.size || 0) > 5) mul *= 0.1;

    const refCounts = new Map();
    for (const ref of references.get(ident) || []) {
      refCounts.set(ref, (refCounts.get(ref) || 0) + 1);
    }
    for (const [referencer, numRefs] of refCounts) {
      const scaled = Math.sqrt(numRefs) * mul;
      for (const definer of defines.get(ident) || []) {
        addEdge(referencer, definer, scaled);
      }
    }
  }

  const nodes = [...relFiles];
  return { outEdges, nodes, personalize, definitions };
}

export function runPagerank(graph, { iterations = 20, damping = 0.85 } = {}) {
  const { outEdges, nodes, personalize, definitions } = graph;
  if (!nodes.length) return [];

  const rank = new Map(nodes.map((n) => [n, 1 / nodes.length]));
  const persSum = [...personalize.values()].reduce((a, b) => a + b, 0) || 0;
  const persNorm = new Map();
  for (const n of nodes) {
    persNorm.set(n, persSum ? (personalize.get(n) || 0) / persSum : 1 / nodes.length);
  }

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
      for (const [dst, w] of outs) {
        next.set(dst, (next.get(dst) || 0) + (damping * srcRank * w) / total);
      }
    }
    for (const n of nodes) rank.set(n, next.get(n) || 0);
  }

  /** @type {{ rel: string, name: string, line: number, kind: string, score: number }[]} */
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
        for (const tag of tags) {
          rankedDefs.push({ rel, name, line: tag.line, kind: tag.kind, score: edgeRank });
        }
      }
    }
  }

  rankedDefs.sort((a, b) => b.score - a.score || a.rel.localeCompare(b.rel) || a.line - b.line);

  const seen = new Set();
  const deduped = [];
  for (const item of rankedDefs) {
    const k = `${item.rel}:${item.name}:${item.line}`;
    if (seen.has(k)) continue;
    seen.add(k);
    deduped.push(item);
  }
  return deduped;
}

function groupRankedTags(rankedTags) {
  const byFile = new Map();
  for (const tag of rankedTags) {
    if (!byFile.has(tag.rel)) byFile.set(tag.rel, []);
    byFile.get(tag.rel).push(tag);
  }
  return [...byFile.entries()]
    .map(([rel, tags]) => ({ rel, tags, score: tags.reduce((s, t) => s + t.score, 0) }))
    .sort((a, b) => b.score - a.score || a.rel.localeCompare(b.rel));
}

function renderPagerankSlice(groups) {
  const lines = [renderMapHeader("pagerank")];
  for (const group of groups) {
    lines.push(`## ${group.rel}`);
    for (const tag of group.tags.slice(0, 10)) {
      lines.push(formatMapLine(group.rel, tag.line, tag.line, tag.name, tag.kind));
    }
  }
  return lines.join("\n");
}

export function generatePagerankMap(repoRoot, query, options = {}) {
  const budgetChars = options.budgetChars ?? charBudgetFromTokens(options.budgetTokens ?? 1024);
  const files = options.files ?? listRepoFiles(repoRoot);
  const tags = buildTagIndex(repoRoot, files);
  const graph = buildPagerankGraph(tags, query);
  const rankedTags = runPagerank(graph);
  const groups = groupRankedTags(rankedTags);
  return fitRankedToBudget(groups, renderPagerankSlice, budgetChars);
}
