// explore-wire-format.mjs — parse and validate explore wire plaintext tokens.
//
// Wire format: plaintext only; SECTION PascalName headers; embedded tokens:
//   <file:repo/rel/path.ext:start-end>
//   <url:https://...>
//   <quote:https://...|excerpt up to 500 chars>

import { existsSync } from "node:fs";
import { join } from "node:path";
import { MAX_SPAN, safeRelPath } from "./trace-schema.mjs";

export const FILE_TOKEN_RE = /<file:([^:>]+):(\d+)-(\d+)>/g;
export const URL_TOKEN_RE = /<url:(https?:\/\/[^>]+)>/g;
export const QUOTE_TOKEN_RE = /<quote:(https?:\/\/[^>|]+)\|([^>]{0,500})>/g;
export const SECTION_LINE_RE = /^SECTION\s+([A-Za-z][A-Za-z0-9]*)\s*$/;

export const TRACE_SECTIONS = [
  "Overview",
  "Flow",
  "KeyFiles",
  "CodeReferences",
];

export const WEBSEARCH_SECTIONS = [
  "ExecutiveSummary",
  "InScopeFindings",
  "AdjacentOutOfScope",
  "PriorArt",
  "GapsRisks",
  "RecommendedNextSteps",
];

export function isExploreWireFormat(text) {
  const body = text || "";
  if (/^SECTION\s+[A-Za-z]/m.test(body)) return true;
  if (FILE_TOKEN_RE.test(body)) return true;
  FILE_TOKEN_RE.lastIndex = 0;
  if (URL_TOKEN_RE.test(body)) return true;
  URL_TOKEN_RE.lastIndex = 0;
  if (QUOTE_TOKEN_RE.test(body)) return true;
  QUOTE_TOKEN_RE.lastIndex = 0;
  return false;
}

export function extractFileTokens(text) {
  const tokens = [];
  const seen = new Set();
  for (const m of (text || "").matchAll(new RegExp(FILE_TOKEN_RE.source, "g"))) {
    const path = m[1].trim();
    const start = Number(m[2]);
    const end = Number(m[3]);
    const key = `${path}:${start}-${end}`;
    if (seen.has(key)) continue;
    seen.add(key);
    tokens.push({ kind: "file", path, startLine: start, endLine: end, raw: m[0] });
  }
  return tokens;
}

export function extractUrlTokens(text) {
  const tokens = [];
  const seen = new Set();
  for (const m of (text || "").matchAll(new RegExp(URL_TOKEN_RE.source, "g"))) {
    const url = m[1].trim();
    if (seen.has(url)) continue;
    seen.add(url);
    tokens.push({ kind: "url", url, raw: m[0] });
  }
  for (const m of (text || "").matchAll(new RegExp(QUOTE_TOKEN_RE.source, "g"))) {
    const url = m[1].trim();
    if (seen.has(url)) continue;
    seen.add(url);
    tokens.push({ kind: "url", url, raw: m[0], fromQuote: true });
  }
  return tokens;
}

export function extractQuoteTokens(text) {
  const tokens = [];
  for (const m of (text || "").matchAll(new RegExp(QUOTE_TOKEN_RE.source, "g"))) {
    tokens.push({
      kind: "quote",
      url: m[1].trim(),
      excerpt: m[2].trim(),
      raw: m[0],
    });
  }
  return tokens;
}

export function parseExploreWire(text) {
  const lines = (text || "").split("\n");
  const sections = [];
  const allTokens = [];
  let current = { name: "_preamble", bodyLines: [] };

  const flush = () => {
    const body = current.bodyLines.join("\n").trim();
    const fileTokens = extractFileTokens(body);
    const urlTokens = extractUrlTokens(body);
    const quoteTokens = extractQuoteTokens(body);
    const tokens = [...fileTokens, ...urlTokens, ...quoteTokens];
    sections.push({
      name: current.name,
      body,
      tokens,
      fileTokens,
      urlTokens,
      quoteTokens,
    });
    allTokens.push(...tokens);
  };

  for (const line of lines) {
    const sec = line.match(SECTION_LINE_RE);
    if (sec) {
      flush();
      current = { name: sec[1], bodyLines: [] };
      continue;
    }
    current.bodyLines.push(line);
  }
  flush();

  return {
    sections: sections.filter((s) => s.name !== "_preamble" || s.body.length > 0),
    tokens: allTokens,
    fileTokens: extractFileTokens(text),
    urlTokens: extractUrlTokens(text),
    quoteTokens: extractQuoteTokens(text),
  };
}

export function sectionScoreWire(text, expectedSections = null) {
  const names = new Set(
    parseExploreWire(text).sections.map((s) => s.name).filter((n) => n !== "_preamble"),
  );
  if (expectedSections?.length) {
    return expectedSections.filter((n) => names.has(n)).length;
  }
  return names.size;
}

export function lintExploreWire(text) {
  const issues = [];
  const body = text || "";
  if (/^#{1,6}\s/m.test(body)) issues.push("markdown headings detected");
  if (/```/.test(body)) issues.push("fenced code blocks detected");
  if (/```mermaid\b/.test(body)) issues.push("mermaid detected");
  if (/^\s*\|.*\|\s*$/m.test(body)) issues.push("markdown table detected");
  return { ok: issues.length === 0, issues };
}

export function validateTraceWire(parsed, workspace, { allowedPaths = null } = {}) {
  const errors = [];
  const allowed = allowedPaths ? new Set(allowedPaths) : null;

  for (const tok of parsed.fileTokens) {
    const rel = safeRelPath(workspace, tok.path);
    if (!rel) {
      errors.push(`invalid file path: ${tok.path}`);
      continue;
    }
    if (allowed && !allowed.has(rel) && !allowed.has(tok.path)) {
      errors.push(`file not in allowed set: ${tok.path}`);
    }
    if (!Number.isFinite(tok.startLine) || !Number.isFinite(tok.endLine)) {
      errors.push(`invalid span: ${tok.path}:${tok.startLine}-${tok.endLine}`);
      continue;
    }
    if (tok.startLine < 1 || tok.endLine < tok.startLine) {
      errors.push(`invalid line range: ${tok.path}:${tok.startLine}-${tok.endLine}`);
      continue;
    }
    if (tok.endLine - tok.startLine + 1 > MAX_SPAN) {
      errors.push(`span too large: ${tok.path}:${tok.startLine}-${tok.endLine}`);
    }
    if (workspace && !existsSync(join(workspace, rel))) {
      errors.push(`file not found: ${tok.path}`);
    }
  }

  return { ok: errors.length === 0, errors };
}

export function normalizeWireUrl(url) {
  try {
    const u = new URL(String(url).trim());
    let href = u.href;
    if (href.endsWith("/") && u.pathname !== "/") href = href.slice(0, -1);
    return href;
  } catch {
    return String(url || "").trim();
  }
}

export function validateWebsearchWire(parsed, { urlsFetched = null } = {}) {
  const errors = [];
  for (const q of parsed.quoteTokens) {
    if (q.excerpt.length > 500) errors.push(`quote excerpt too long: ${q.url}`);
  }
  if (urlsFetched) {
    const fetched = new Set([...urlsFetched].map(normalizeWireUrl));
    const citeUrls = [
      ...parsed.urlTokens.map((t) => t.url),
      ...parsed.quoteTokens.map((t) => t.url),
    ];
    for (const raw of citeUrls) {
      const norm = normalizeWireUrl(raw);
      if (!fetched.has(norm)) {
        errors.push(`url not fetched during explore: ${raw}`);
      }
    }
  }
  return { ok: errors.length === 0, errors };
}

export function wireComplianceScore(text, mode = "trace") {
  const parsed = parseExploreWire(text);
  const lint = lintExploreWire(text);
  const expected = mode === "websearch" ? WEBSEARCH_SECTIONS : TRACE_SECTIONS;
  const sections = sectionScoreWire(text, expected);
  return {
    isWire: isExploreWireFormat(text),
    lintOk: lint.ok,
    lintIssues: lint.issues,
    sectionScore: sections,
    fileTokenCount: parsed.fileTokens.length,
    urlTokenCount: parsed.urlTokens.length,
    quoteTokenCount: parsed.quoteTokens.length,
  };
}
