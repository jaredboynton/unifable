// bench-websearch-scorer.mjs — scoring helpers for websearch quality bench.
// Shares aggregate stats with trace benches via bench-scorer-common.mjs.

import {
  countMarkdownHeadings,
  isNonEmpty,
  median,
  percentile,
} from "./bench-scorer-common.mjs";
import {
  extractQuoteTokens,
  isExploreWireFormat,
  sectionScoreWire,
  WEBSEARCH_SECTIONS,
  wireComplianceScore,
} from "./explore-wire-format.mjs";

const SECTION_MARKERS = [
  /executive summary/i,
  /in-scope findings/i,
  /adjacent\s*\/\s*out-of-scope|adjacent \/ out-of-scope/i,
  /prior art/i,
  /gaps,?\s*risks|gaps and risks/i,
  /recommended next steps/i,
];

export function extractUrls(text) {
  const body = text || "";
  const urls = [];
  for (const m of body.matchAll(/https?:\/\/[^\s)\]>,"']+/gi)) urls.push(m[0]);
  for (const m of body.matchAll(/<url:(https?:\/\/[^>]+)>/g)) urls.push(m[1].trim());
  for (const q of extractQuoteTokens(body)) urls.push(q.url);
  return [...new Set(urls)];
}

export function sectionScoreWireWebsearch(text) {
  return sectionScoreWire(text, WEBSEARCH_SECTIONS);
}

export function sectionScore(text) {
  let n = 0;
  for (const re of SECTION_MARKERS) {
    if (re.test(text || "")) n += 1;
  }
  return n;
}

export function nextStepsSection(text) {
  const m = (text || "").match(/recommended next steps([\s\S]*)/i);
  if (m) return m[1];
  const wire = (text || "").match(/SECTION RecommendedNextSteps\n([\s\S]*?)(?=^SECTION |$)/m);
  return wire ? wire[1] : "";
}

export function matchesAny(text, patterns = []) {
  if (!patterns?.length) return false;
  return patterns.some((p) => new RegExp(p, "i").test(text || ""));
}

export function sectionHeadingScore(text) {
  return countMarkdownHeadings(text || "").total;
}

export function scoreWebsearchOutput(text, expect = {}) {
  const body = text || "";
  const empty = !isNonEmpty(body);
  const wire = isExploreWireFormat(body);
  const urls = extractUrls(body);
  const sections = wire ? sectionScoreWireWebsearch(body) : sectionScore(body);
  const sectionHeadings = sectionHeadingScore(body);
  const wireCompliance = wire ? wireComplianceScore(body, "websearch") : null;
  const nextSteps = nextStepsSection(body);

  const urlCount = urls.length;
  const minUrls = expect.minUrls ?? 1;
  const urlsOk = urlCount >= minUrls;

  const urlPatternsOk = !expect.urlPatterns?.length
    || expect.urlPatterns.some((p) => new RegExp(p, "i").test(body));

  const forbiddenHits = (expect.forbiddenNextStepPatterns || []).filter((p) =>
    new RegExp(p, "i").test(nextSteps || body),
  );
  const scopeOk = forbiddenHits.length === 0;

  const sectionsOk = sections >= (expect.minSections ?? 4);

  const pass = !empty && urlsOk && urlPatternsOk && scopeOk && sectionsOk;

  return {
    pass,
    empty,
    urlCount,
    urlsOk,
    urlPatternsOk,
    sections,
    sectionHeadings,
    sectionsOk,
    scopeOk,
    forbiddenHits,
    chars: body.length,
    wireFormat: wire,
    wireCompliance,
  };
}

export function aggregateWebsearchScores(rows) {
  const n = rows.length || 1;
  return {
    count: rows.length,
    passRate: rows.filter((r) => r.pass).length / n,
    emptyRate: rows.filter((r) => r.empty).length / n,
    avgUrlCount: rows.reduce((s, r) => s + (r.urlCount || 0), 0) / n,
    avgSections: rows.reduce((s, r) => s + (r.sections || 0), 0) / n,
    avgSectionHeadings: rows.reduce((s, r) => s + (r.sectionHeadings || 0), 0) / n,
    scopePassRate: rows.filter((r) => r.scopeOk).length / n,
    medianWebsearchMs: median(rows.map((r) => r.websearchMs || 0)),
    p95WebsearchMs: percentile(rows.map((r) => r.websearchMs || 0), 0.95),
  };
}
