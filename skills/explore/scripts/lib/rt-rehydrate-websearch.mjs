// rt-rehydrate-websearch.mjs — FETCH INDEX + pointer submit rehydration for RT websearch.
import { normalizeWireUrl } from "./explore-wire-format.mjs";

function quoteSafe(text) {
  return String(text || "").replace(/\|/g, "/").replace(/\s+/g, " ").trim();
}

export function buildSearchCatalogPacket(searchCatalog, { goal } = {}) {
  const lines = [
    "Round 2: fetch promising URLs from the search catalog below.",
    "Call exa_fetch with url_indices only — do NOT pass raw URLs.",
    "Batch multiple indices in one exa_fetch call.",
    "",
  ];
  if (goal) lines.push(`GOAL: ${goal}`, "");
  lines.push("SEARCH CATALOG (use catalog_index in exa_fetch url_indices):", "");
  if (!searchCatalog.length) {
    lines.push("(empty — run exa_search first)");
  } else {
    for (const entry of searchCatalog) {
      lines.push(
        `[${entry.catalogIndex}] ${entry.url}${entry.title ? ` — ${entry.title}` : ""}${entry.query ? ` (query: ${entry.query})` : ""}`,
      );
    }
  }
  return lines.join("\n");
}

export function buildFetchIndex(fetchLog, { maxUrls, previewExcerpts = 2 } = {}) {
  const lines = [
    "FETCH INDEX (cite url_index + excerpt_index in citation_refs; host rehydrates URLs and quotes):",
    "Cite every fetched source that supports a claim — up to one citation_refs entry per url_index/excerpt_index pair.",
    "",
  ];
  const cap = maxUrls ?? fetchLog.length;
  const slice = fetchLog.slice(0, cap);
  for (let i = 0; i < slice.length; i += 1) {
    const entry = slice[i];
    lines.push(`[${entry.fetchIndex}] ${entry.url}${entry.title ? ` (${entry.title})` : ""}`);
    const previews = (entry.excerpts || []).slice(0, previewExcerpts);
    for (let j = 0; j < previews.length; j += 1) {
      const preview = previews[j].length > 120 ? `${previews[j].slice(0, 120)}…` : previews[j];
      lines.push(`  excerpt[${j}]: "${preview}"`);
    }
    if ((entry.excerpts || []).length > previewExcerpts) {
      lines.push(`  ... (${entry.excerpts.length - previewExcerpts} more excerpts)`);
    }
    lines.push("");
  }
  if (fetchLog.length > slice.length) {
    lines.push(`... (${fetchLog.length - slice.length} more fetched URLs omitted from index)`, "");
  }
  return lines.join("\n");
}

export function citationTokens(citationRefs, fetchLog) {
  const tokens = [];
  const seen = new Set();
  for (const cite of citationRefs || []) {
    const entry = fetchLog[cite.url_index];
    if (!entry) continue;
    const url = normalizeWireUrl(entry.url);
    const excerpt = entry.excerpts?.[cite.excerpt_index] || "";
    const urlKey = url;
    if (!seen.has(urlKey)) {
      seen.add(urlKey);
      tokens.push(`<url:${url}>`);
    }
    if (excerpt) {
      tokens.push(`<quote:${url}|${quoteSafe(excerpt.slice(0, 500))}>`);
    }
  }
  return tokens;
}

export function renderWebsearchWire(pointer, fetchLog) {
  const tokens = citationTokens(pointer.citation_refs, fetchLog);
  const tokenBlock = tokens.length ? `\n${tokens.join("\n")}` : "";

  return [
    "SECTION ExecutiveSummary",
    String(pointer.executive_summary || "").trim(),
    "",
    "SECTION InScopeFindings",
    String(pointer.in_scope_findings || "").trim(),
    tokenBlock,
    "",
    "SECTION AdjacentOutOfScope",
    String(pointer.adjacent_out_of_scope || "").trim(),
    "",
    "SECTION PriorArt",
    String(pointer.prior_art || "").trim(),
    "",
    "SECTION GapsRisks",
    String(pointer.gaps_risks || "").trim(),
    "",
    "SECTION RecommendedNextSteps",
    String(pointer.recommended_next_steps || "").trim(),
    "",
  ].join("\n");
}

export function rehydrateWebsearchPointer(pointer, fetchLog) {
  return renderWebsearchWire(pointer, fetchLog);
}
