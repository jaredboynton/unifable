// rt-web-run-tools.mjs — Realtime web_run dispatch via Codex alpha/search.
import { normalizeWireUrl } from "./explore-wire-format.mjs";
import { ALPHA_MODEL_OUTPUT_CAP } from "./codex-alpha-search-client.mjs";
import {
  WEB_RUN_TOOL_NAME,
  buildWebRunToolSpec,
  callAlphaSearch,
  callResponsesWebSearch,
  parseWebRunArguments,
  webRunCommandsFromArgs,
} from "./rt-web-run.mjs";
const EXCERPT_CHUNK = Number(process.env.UNISEARCH_WS_EXCERPT_CHUNK) || 400;
const EXCERPT_MAX = Number(process.env.UNISEARCH_WS_FETCH_EXCERPT_MAX) || 1200;

export function createWebsearchContext() {
  return {
    searchCatalog: [],
    fetchLog: [],
    searchCount: 0,
    fetchCount: 0,
  };
}

export function chunkExcerpts(text, chunkSize = EXCERPT_CHUNK) {
  const normalized = String(text || "").replace(/\s+/g, " ").trim();
  if (!normalized) return [""];
  const chunks = [];
  for (let i = 0; i < normalized.length && chunks.length < 20; i += chunkSize) {
    let chunk = normalized.slice(i, i + chunkSize);
    if (chunk.length > EXCERPT_MAX) chunk = `${chunk.slice(0, EXCERPT_MAX)}…`;
    chunks.push(chunk);
  }
  return chunks.length ? chunks : [""];
}

export function parseArguments(raw) {
  if (raw == null || raw === "") return {};
  if (typeof raw === "object") return raw;
  try {
    return JSON.parse(String(raw));
  } catch {
    return {};
  }
}

const URL_RE = /https?:\/\/[^\s)\]>"]+/g;
const MARKDOWN_LINK_RE = /\[[^\]]*\]\((https?:\/\/[^)]+)\)/g;
const FALLBACK_URL = "https://web.search/";
const DEFAULT_ALPHA_MAX_OUTPUT = Number(process.env.UNISEARCH_WS_ALPHA_MAX_OUTPUT_TOKENS) || ALPHA_MODEL_OUTPUT_CAP;
const COALESCE_WEB_RUN = process.env.UNISEARCH_WS_COALESCE_WEB_RUN !== "0";
// Open the top-K discovered URLs in small parallel batches so each page gets the
// full output budget instead of competing for one truncated call.
const OPEN_BATCH_SIZE = Math.max(1, Number(process.env.UNISEARCH_WS_OPEN_BATCH) || 2);

const QUERY_STOPWORDS = new Set([
  "the", "a", "an", "and", "or", "of", "to", "in", "for", "on", "with", "by", "is",
  "are", "be", "as", "at", "from", "that", "this", "it", "how", "what", "why", "can",
  "should", "use", "using", "via", "over", "into", "your", "you", "one", "not", "but",
]);

export function queryTerms(query) {
  const seen = new Set();
  const terms = [];
  for (const raw of String(query || "").toLowerCase().matchAll(/[a-z0-9][a-z0-9._/-]{2,}/g)) {
    const t = raw[0].replace(/[._/-]+$/, "");
    if (t.length < 3 || QUERY_STOPWORDS.has(t) || seen.has(t)) continue;
    seen.add(t);
    terms.push(t);
  }
  return terms;
}

// Strip alpha open-output markers (citeNN† anchors, L<n>: line prefixes) and
// collapse whitespace so spans read as page prose, not wire noise.
function cleanSpan(s) {
  return String(s)
    .replace(/cite\d+†/g, " ")
    .replace(/\bL\d+:\s?/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

// Navigation/footer/link-list boilerplate that pollutes proof if quoted. Doc
// pages (e.g. platform.openai.com) often open to a menu whose link text contains
// query-ish tokens; this keeps span selection on actual content.
function isBoilerplate(s) {
  const lower = s.toLowerCase();
  if (/(skip to content|cookie|newsletter|subscribe now|sign in|log in|navigation menu|all rights reserved|terms of (use|service)|privacy policy|toggle navigation)/.test(lower)) {
    return true;
  }
  const seps = (s.match(/[•|]|\s\*\s|·/g) || []).length;
  const sentences = (s.match(/[a-z0-9][.!?](\s|$)/g) || []).length;
  // Many list/menu separators with no sentence structure = nav/link list.
  if (seps >= 4 && sentences <= 1) return true;
  // Doc-index / menu: many tokens, almost no sentences, high Title-Case ratio
  // (e.g. "Home API Docs API reference Codex ChatGPT Apps SDK Resources").
  const words = s.split(/\s+/).filter(Boolean);
  if (words.length >= 10 && sentences <= 1) {
    const caps = words.filter((w) => /^[A-Z][A-Za-z]/.test(w)).length;
    if (caps / words.length >= 0.4) return true;
  }
  return false;
}

// Select the page spans that best match the query terms, so citations quote
// query-relevant evidence rather than a page's boilerplate/nav. Falls back to
// the leading content spans when the query gives no signal.
export function selectQuerySpans(text, query, { maxSpans = 3, spanChars = 480 } = {}) {
  const body = String(text || "").replace(/\r/g, "");
  if (!body.trim()) return [];
  const terms = queryTerms(query);
  // Split per line/paragraph so a single nav or cookie line does not contaminate
  // (and discard) the real content lines around it in the same opened page.
  const allBlocks = body
    .split(/\n+/)
    .map(cleanSpan)
    .filter((b) => b.length >= 40);
  if (!allBlocks.length) return chunkExcerpts(body).slice(0, maxSpans);
  const blocks = allBlocks.filter((b) => !isBoilerplate(b));
  // All blocks are nav/boilerplate (e.g. a doc-index SPA): no citable content.
  if (!blocks.length) return [];
  if (!terms.length) return blocks.slice(0, maxSpans).map((b) => b.slice(0, spanChars));

  const scored = blocks.map((block, idx) => {
    const lower = block.toLowerCase();
    let distinct = 0;
    let total = 0;
    for (const t of terms) {
      const hits = lower.split(t).length - 1;
      if (hits > 0) { distinct += 1; total += hits; }
    }
    return { block, idx, score: distinct * 10 + total };
  });
  const matched = scored.filter((s) => s.score > 0).sort((a, b) => b.score - a.score || a.idx - b.idx);
  const chosen = (matched.length ? matched : scored.slice(0, maxSpans)).slice(0, maxSpans);
  chosen.sort((a, b) => a.idx - b.idx);
  return chosen.map((s) => s.block.slice(0, spanChars));
}

export function buildWebRunToolSchemas({ allowOpen = false } = {}) {
  return [buildWebRunToolSpec({ allowOpen })];
}

export function extractUrlsFromText(text) {
  const seen = new Set();
  const urls = [];
  const add = (raw) => {
    const cleaned = String(raw || "").replace(/[.,;:!?)]+$/, "");
    const norm = normalizeWireUrl(cleaned);
    if (norm && !seen.has(norm)) {
      seen.add(norm);
      urls.push(norm);
    }
  };
  for (const match of String(text || "").matchAll(MARKDOWN_LINK_RE)) add(match[1]);
  for (const match of String(text || "").matchAll(URL_RE)) add(match[0]);
  return urls;
}

export function excerptForUrl(text, url) {
  const body = String(text || "");
  const norm = normalizeWireUrl(url);
  const idx = body.indexOf(url) >= 0 ? body.indexOf(url) : body.indexOf(norm);
  if (idx >= 0) {
    const start = Math.max(0, idx - 240);
    const end = Math.min(body.length, idx + url.length + 480);
    return body.slice(start, end).replace(/\s+/g, " ").trim();
  }
  return body.replace(/\s+/g, " ").trim();
}

function addToFetchLog(ctx, { url, title, text, excerpts, opened = false }) {
  const norm = normalizeWireUrl(url);
  const pickExcerpts = (body) => (excerpts && excerpts.length ? excerpts : chunkExcerpts(body));
  let idx = ctx.fetchLog.findIndex((e) => normalizeWireUrl(e.url) === norm);
  if (idx >= 0) {
    const existing = ctx.fetchLog[idx];
    if (opened) existing.opened = true;
    if (String(text || "").length > String(existing.text || "").length) {
      existing.text = String(text || "");
      existing.excerpts = pickExcerpts(text);
    } else if (excerpts && excerpts.length) {
      existing.excerpts = excerpts;
    }
    return idx;
  }
  idx = ctx.fetchLog.length;
  ctx.fetchLog.push({
    fetchIndex: idx,
    url: norm,
    title: title || "",
    text: String(text || ""),
    excerpts: pickExcerpts(text),
    opened,
  });
  return idx;
}

// Restrict the submit candidate set to citable evidence: pages actually opened
// (real fetched content) plus high-authority sources, dropping low-authority
// snippet-only entries that otherwise leak into citations as noise. Falls back
// to the full log if the gate would leave too few sources. fetchIndex is
// renumbered to stay consistent with the submit packet and pointer validation.
export function pruneFetchLogForSubmit(ctx, { minAuthority = 7, minKeep = 3 } = {}) {
  const log = ctx.fetchLog || [];
  const kept = log.filter((e) => e.opened || scoreUrlAuthority(e.url) >= minAuthority);
  const finalLog = kept.length >= Math.min(minKeep, log.length) ? kept : log;
  finalLog.forEach((e, i) => { e.fetchIndex = i; });
  ctx.fetchLog = finalLog;
  return finalLog;
}

export function populateFetchLogFromAlphaOutput(ctx, output, { query = "" } = {}) {
  const text = String(output || "").trim();
  if (!text) return [];

  const urls = extractUrlsFromText(text);
  const indices = [];
  if (urls.length) {
    for (const url of urls) {
      const excerpt = cleanSpan(excerptForUrl(text, url));
      indices.push(addToFetchLog(ctx, { url, title: query, text: excerpt }));
    }
  } else {
    indices.push(addToFetchLog(ctx, { url: FALLBACK_URL, title: query, text }));
  }
  return indices;
}

// Matches the `open` command's per-source header, e.g.
//   Source: open({"ref_id":"https://example.com", ...})
const OPEN_SOURCE_RE = /open\(\s*\{[^}]*?"ref_id"\s*:\s*"([^"]+)"[^}]*\}\s*\)/g;
const LINE_DUMP_RE = /^L\d+:\s?(.*)$/gm;

// Parse alpha `open` output (real page content) into fetchLog page text.
// Each opened source begins with a `Source: open({"ref_id":"URL"})` header
// followed by `L<n>: <text>` line-dump lines. Falls back to snippet parsing
// when no recognizable open blocks are present (e.g. a pure search output).
export function populateFetchLogFromOpenOutput(ctx, output, { query = "" } = {}) {
  const text = String(output || "");
  if (!text.trim()) return [];

  const headers = [];
  const re = new RegExp(OPEN_SOURCE_RE.source, "g");
  let m;
  while ((m = re.exec(text)) !== null) {
    headers.push({ url: m[1], blockStart: m.index, contentStart: re.lastIndex });
  }

  if (!headers.length) {
    return populateFetchLogFromAlphaOutput(ctx, text, { query });
  }

  const indices = [];
  // Snippet URLs that appear before the first opened page (search remainder).
  const preamble = text.slice(0, headers[0].blockStart);
  if (extractUrlsFromText(preamble).length) {
    indices.push(...populateFetchLogFromAlphaOutput(ctx, preamble, { query }));
  }

  for (let i = 0; i < headers.length; i += 1) {
    const end = i + 1 < headers.length ? headers[i + 1].blockStart : text.length;
    const block = text.slice(headers[i].contentStart, end);
    const lines = [];
    for (const lm of block.matchAll(LINE_DUMP_RE)) lines.push(lm[1]);
    const pageText = (lines.length ? lines.join("\n") : block).replace(/[ \t]+\n/g, "\n").trim();
    if (!pageText) continue;
    const excerpts = selectQuerySpans(pageText, query);
    // Skip pages whose opened content is entirely navigation/boilerplate (e.g.
    // doc-index SPAs) so nav menus are never cited as proof.
    if (!excerpts.length) continue;
    indices.push(addToFetchLog(ctx, { url: headers[i].url, title: query, text: pageText, excerpts, opened: true }));
    ctx.fetchCount = (ctx.fetchCount || 0) + 1;
  }
  return indices;
}

function trimPreview(text, max = 800) {
  const s = String(text || "").replace(/\s+/g, " ").trim();
  return s.length <= max ? s : `${s.slice(0, max)}…`;
}

export function mergeWebRunSearchQueries(calls) {
  const merged = [];
  const seen = new Set();
  for (const call of calls) {
    let args;
    try {
      args = parseWebRunArguments(parseArguments(call.arguments));
      const commands = webRunCommandsFromArgs(args);
      for (const q of commands.search_query || []) {
        const key = String(q.q || "").trim().toLowerCase();
        if (!key || seen.has(key)) continue;
        seen.add(key);
        merged.push(q);
      }
    } catch {
      continue;
    }
  }
  return merged;
}

const AUTHORITY_QUERIES_ON = process.env.UNISEARCH_WS_AUTHORITY_QUERIES !== "0";
const AUTHORITY_QUERY_DOMAINS = (process.env.UNISEARCH_WS_AUTHORITY_QUERY_DOMAINS
  || "arxiv.org,github.com,aclanthology.org,openreview.net")
  .split(",").map((s) => s.trim()).filter(Boolean);

// Bias discovery toward primary sources by adding domain-scoped variants of the
// strongest query (e.g. the same query restricted to arxiv.org / github.com).
// alpha's keyword search otherwise skews to blogs/aggregators; this is how the
// candidate pool comes to include the papers and repos that Exa's neural search
// surfaces. Model queries are preserved; a few slots are reserved for variants.
export function augmentQueriesForAuthority(queries, { domains = AUTHORITY_QUERY_DOMAINS, max = 8 } = {}) {
  if (!AUTHORITY_QUERIES_ON || !queries.length || !domains.length) return queries.slice(0, max);
  const base = queries.find((q) => !(q.domains && q.domains.length)) || queries[0];
  const variants = domains.map((d) => ({ q: base.q, domains: [d] }));
  const keepN = Math.max(1, max - variants.length);
  const out = [];
  const seen = new Set();
  for (const q of [...queries.slice(0, keepN), ...variants]) {
    if (!q.q) continue;
    const key = `${String(q.q).toLowerCase()}|${(q.domains || []).join(",")}`;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(q);
  }
  return out.slice(0, max);
}

export function mergeWebRunOpenUrls(calls) {
  const merged = [];
  const seen = new Set();
  for (const call of calls) {
    try {
      const commands = webRunCommandsFromArgs(parseWebRunArguments(parseArguments(call.arguments)));
      for (const op of commands.open || []) {
        const url = normalizeWireUrl(op.ref_id);
        if (!url || seen.has(url)) continue;
        seen.add(url);
        merged.push(url);
      }
    } catch {
      continue;
    }
  }
  return merged;
}

async function executeWebRunSearch(commands, ctx, { authPathOverride } = {}) {
  const querySummary = (commands.search_query || [])
    .map((q) => q.q)
    .filter(Boolean)
    .join("; ");
  const hasOpen = Array.isArray(commands.open) && commands.open.length > 0;
  const hasSearch = Array.isArray(commands.search_query) && commands.search_query.length > 0;

  const result = await callAlphaSearch({
    authPathOverride,
    searchModel: process.env.UNITRACE_SEARCH_MODEL || "gpt-5.4",
    commands,
    maxOutputTokens: DEFAULT_ALPHA_MAX_OUTPUT,
  });
  // When open is requested the output carries real page content; parse that
  // (it also recovers search-snippet URLs from the preamble). Otherwise treat
  // the output as search snippets.
  const fetchIndices = hasOpen
    ? populateFetchLogFromOpenOutput(ctx, result.output, { query: querySummary })
    : populateFetchLogFromAlphaOutput(ctx, result.output, { query: querySummary });
  if (hasSearch) ctx.searchCount = (ctx.searchCount || 0) + 1;
  return {
    ok: true,
    query: querySummary,
    query_count: (commands.search_query || []).length,
    open_count: hasOpen ? commands.open.length : 0,
    output_preview: trimPreview(result.output),
    urls_found: fetchIndices.length,
    fetch_indices: fetchIndices,
  };
}

function chunk(list, size) {
  const out = [];
  for (let i = 0; i < list.length; i += size) out.push(list.slice(i, i + size));
  return out;
}

// F3: second retrieval backend. Codex /responses web_search is a different
// engine from alpha/search; running it alongside the alpha fanout diversifies
// the candidate pool by engine, not just by query phrasing.
export async function hostResponsesSearch(ctx, { authPathOverride, goal, timeoutMs } = {}) {
  try {
    // Best-effort secondary backend for engine diversity. Bound it tightly: the
    // RT WebSocket sits idle (no reader) while the ensemble awaits this call, and
    // the server idle-closes a long-silent socket — a 30s wait kills the later
    // submit, while the ~5-12s window the primary fanout uses survives. Keep this
    // backend inside that proven-survivable window so it can never starve submit.
    const result = await callResponsesWebSearch({
      authPathOverride,
      searchModel: process.env.UNITRACE_SEARCH_MODEL || "gpt-5.4",
      userPrompt: String(goal || ""),
      timeoutMs: timeoutMs ?? (Number(process.env.UNISEARCH_WS_ENSEMBLE_RESPONSES_TIMEOUT_MS) || 10000),
    });
    const idx = populateFetchLogFromAlphaOutput(ctx, result.output, { query: "responses-web-search" });
    ctx.searchCount = (ctx.searchCount || 0) + 1;
    return { ok: true, urls: idx.length };
  } catch (e) {
    return { ok: false, error: e.message || String(e) };
  }
}

// F1: parallel multi-strategy fanout. Run several strategy-diverse alpha
// searches concurrently into one shared fetchLog so the candidate pool spans
// papers, repos/docs, production systems and standards at once — far broader
// than a single query batch. Each strategy supplies its own queries (often
// domain-scoped). Failures are isolated per strategy.
export async function hostFanoutSearch(ctx, { authPathOverride, strategies = [] } = {}) {
  const searchModel = process.env.UNITRACE_SEARCH_MODEL || "gpt-5.4";
  return Promise.all(
    strategies.map(async (strat) => {
      const queries = (strat.queries || []).filter((q) => q && q.q);
      if (!queries.length) return { label: strat.label, ok: false, urls: 0 };
      try {
        const result = await callAlphaSearch({
          authPathOverride,
          searchModel,
          commands: { search_query: queries },
          maxOutputTokens: DEFAULT_ALPHA_MAX_OUTPUT,
        });
        const idx = populateFetchLogFromAlphaOutput(ctx, result.output, { query: strat.label });
        ctx.searchCount = (ctx.searchCount || 0) + 1;
        return { label: strat.label, ok: true, urls: idx.length };
      } catch (e) {
        return { label: strat.label, ok: false, error: e.message || String(e) };
      }
    }),
  );
}

// Domain authority tiers — bias which discovered URLs get opened toward primary
// sources (official docs, source repos, peer-reviewed venues, standards bodies)
// and away from aggregators/blogs. This closes the source-authority gap when the
// search returns many candidates but only the top-K can be opened.
const AUTHORITY_TIERS = [
  [12, [/(^|\.)openai\.com$/, /(^|\.)arxiv\.org$/]],
  [11, [/(^|\.)openreview\.net$/, /(^|\.)usenix\.org$/, /(^|\.)aclanthology\.org$/, /(^|\.)acm\.org$/, /(^|\.)ietf\.org$/, /(^|\.)w3\.org$/, /(^|\.)nips\.cc$/, /(^|\.)neurips\.cc$/, /(^|\.)mlr\.press$/]],
  [10, [/(^|\.)github\.com$/, /\.edu$/, /\.gov$/]],
  [8, [/(^|\.)microsoft\.com$/, /(^|\.)research\.google$/, /(^|\.)googleblog\.com$/, /(^|\.)huggingface\.co$/, /(^|\.)nvidia\.com$/, /(^|\.)ar5iv\./]],
  [2, [/(^|\.)apis\.io$/, /(^|\.)medium\.com$/, /(^|\.)reddit\.com$/, /(^|\.)spacefrontiers\.org$/, /(^|\.)substack\.com$/, /(^|\.)quora\.com$/]],
];

export function scoreUrlAuthority(url) {
  let host;
  try { host = new URL(String(url)).hostname.toLowerCase(); } catch { return 4; }
  for (const [score, patterns] of AUTHORITY_TIERS) {
    if (patterns.some((re) => re.test(host))) return score;
  }
  // Doc/developer subdomains of any host get a modest primary-source bonus.
  if (/^(docs|developers?|platform|api|spec)\./.test(host)) return 7;
  return 4;
}

// Order discovered URLs so the most authoritative are opened first; stable on ties.
export function rankUrlsByAuthority(urls) {
  return urls
    .map((url, idx) => ({ url, idx, score: scoreUrlAuthority(url) }))
    .sort((a, b) => b.score - a.score || a.idx - b.idx)
    .map((e) => e.url);
}

// Host-driven fetch: open the top-K already-discovered URLs to replace their
// search-snippet excerpts with real page content, then keep the query-matched
// spans as proof. Opens in small parallel batches so each page gets the full
// output budget instead of competing for one truncated call.
export async function hostOpenTopUrls(ctx, {
  authPathOverride, cap = 8, query = "", batchSize = OPEN_BATCH_SIZE,
} = {}) {
  const candidates = (ctx.fetchLog || [])
    .map((e) => e.url)
    .filter((u) => u && u !== FALLBACK_URL);
  const urls = rankUrlsByAuthority(candidates).slice(0, cap);
  if (!urls.length) return { ok: false, opened: 0, reason: "no URLs to open" };

  const batches = chunk(urls, Math.max(1, batchSize));
  const outputs = await Promise.all(
    batches.map((batch) =>
      callAlphaSearch({
        authPathOverride,
        searchModel: process.env.UNITRACE_SEARCH_MODEL || "gpt-5.4",
        commands: { open: batch.map((ref_id) => ({ ref_id })) },
        maxOutputTokens: DEFAULT_ALPHA_MAX_OUTPUT,
      })
        .then((r) => r.output)
        .catch(() => ""),
    ),
  );

  const fetchIndices = [];
  for (const output of outputs) {
    if (output) fetchIndices.push(...populateFetchLogFromOpenOutput(ctx, output, { query }));
  }
  return { ok: true, requested: urls.length, opened: fetchIndices.length, batches: batches.length, fetch_indices: fetchIndices };
}

export async function dispatchWebRunTool(name, args, ctx, { authPathOverride } = {}) {
  if (name !== WEB_RUN_TOOL_NAME) {
    return { ok: false, error: `unknown tool: ${name}` };
  }

  let commands;
  try {
    commands = webRunCommandsFromArgs(parseWebRunArguments(args));
  } catch (e) {
    return { ok: false, error: e.message || String(e) };
  }

  try {
    return await executeWebRunSearch(commands, ctx, { authPathOverride });
  } catch (e) {
    return { ok: false, error: e.message || String(e) };
  }
}

export async function dispatchWebRunToolBatch(calls, ctx, { authPathOverride } = {}) {
  if (!calls.length) return [];

  if (COALESCE_WEB_RUN && calls.length >= 1) {
    const mergedQueries = augmentQueriesForAuthority(mergeWebRunSearchQueries(calls));
    const mergedOpen = mergeWebRunOpenUrls(calls);
    if (!mergedQueries.length && !mergedOpen.length) {
      return calls.map((call) => ({
        call,
        args: parseArguments(call.arguments),
        result: { ok: false, error: "web_run: no valid search_query or open entries" },
      }));
    }
    const mergedCommands = {};
    if (mergedQueries.length) mergedCommands.search_query = mergedQueries;
    if (mergedOpen.length) mergedCommands.open = mergedOpen.map((ref_id) => ({ ref_id }));
    try {
      const result = await executeWebRunSearch(
        mergedCommands,
        ctx,
        { authPathOverride },
      );
      return calls.map((call) => ({
        call,
        args: parseArguments(call.arguments),
        result,
      }));
    } catch (e) {
      const err = { ok: false, error: e.message || String(e) };
      return calls.map((call) => ({ call, args: parseArguments(call.arguments), result: err }));
    }
  }

  return Promise.all(
    calls.map(async (call) => {
      const args = parseArguments(call.arguments);
      const result = await dispatchWebRunTool(call.name, args, ctx, { authPathOverride });
      return { call, args, result };
    }),
  );
}
