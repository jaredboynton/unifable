// rt-exa-tools.mjs — top-level Realtime Exa tools (search + fetch by catalog index).
import { normalizeWireUrl } from "./explore-wire-format.mjs";

const DEFAULT_FETCH_MAX_CHARS = Number(process.env.EXPLORE_WS_FETCH_MAX_CHARS) || 3000;
const SEARCH_RESULT_CAP = 20;
const FETCH_URL_CAP = 8;
const EXCERPT_CHUNK = Number(process.env.EXPLORE_WS_EXCERPT_CHUNK) || 400;
const EXCERPT_MAX = Number(process.env.EXPLORE_WS_FETCH_EXCERPT_MAX) || 1200;

const EXA_SEARCH_TOOL = {
  type: "function",
  name: "exa_search",
  description:
    "Search the web via Exa. Fire multiple exa_search calls in parallel with varied queries before fetching. Returns catalog indices for promising URLs.",
  parameters: {
    type: "object",
    properties: {
      query: { type: "string", description: "Search query." },
      numResults: { type: "integer", description: "Number of results (default 10, max 20)." },
    },
    required: ["query"],
    additionalProperties: false,
  },
};

const EXA_FETCH_TOOL = {
  type: "function",
  name: "exa_fetch",
  description:
    "Fetch page content for URLs from the search catalog by index. Batch url_indices in one call. Only indices from SEARCH CATALOG are valid.",
  parameters: {
    type: "object",
    properties: {
      url_indices: {
        type: "array",
        items: { type: "integer", minimum: 0 },
        description: "Search catalog indices to fetch (from Round 1 results).",
      },
    },
    required: ["url_indices"],
    additionalProperties: false,
  },
};

export function buildSearchToolSchemas() {
  return [EXA_SEARCH_TOOL];
}

export function buildFetchToolSchemas() {
  return [EXA_FETCH_TOOL];
}

export function createWebsearchContext() {
  return {
    searchCatalog: [],
    fetchLog: [],
    searchCount: 0,
    fetchCount: 0,
  };
}

function resolveExaApiKey() {
  const key = process.env.EXA_API_KEY || "";
  return key.trim() || null;
}

async function exaRequest(path, body, apiKey) {
  const resp = await fetch(`https://api.exa.ai${path}`, {
    method: "POST",
    headers: {
      "x-api-key": apiKey,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(body),
  });
  const text = await resp.text();
  if (!resp.ok) {
    throw new Error(`Exa ${path} HTTP ${resp.status}: ${text.slice(0, 400)}`);
  }
  try {
    return JSON.parse(text);
  } catch {
    throw new Error(`Exa ${path} returned non-JSON`);
  }
}

function trimPreview(text, max = 800) {
  const s = String(text || "").replace(/\s+/g, " ").trim();
  return s.length <= max ? s : `${s.slice(0, max)}…`;
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

function findCatalogIndex(ctx, url) {
  const norm = normalizeWireUrl(url);
  return ctx.searchCatalog.findIndex((e) => normalizeWireUrl(e.url) === norm);
}

function addToSearchCatalog(ctx, { url, title, score, query }) {
  const norm = normalizeWireUrl(url);
  let idx = findCatalogIndex(ctx, norm);
  if (idx >= 0) {
    return idx;
  }
  idx = ctx.searchCatalog.length;
  ctx.searchCatalog.push({
    catalogIndex: idx,
    url: norm,
    title: title || "",
    score,
    query: query || "",
  });
  return idx;
}

function addToFetchLog(ctx, { url, title, text }) {
  const norm = normalizeWireUrl(url);
  let idx = ctx.fetchLog.findIndex((e) => normalizeWireUrl(e.url) === norm);
  if (idx >= 0) {
    return idx;
  }
  idx = ctx.fetchLog.length;
  ctx.fetchLog.push({
    fetchIndex: idx,
    url: norm,
    title: title || "",
    text: String(text || ""),
    excerpts: chunkExcerpts(text),
  });
  return idx;
}

export async function dispatchExaSearch(args, ctx) {
  const query = String(args?.query || "").trim();
  if (!query) return { ok: false, error: "exa_search: query required" };

  const apiKey = resolveExaApiKey();
  if (!apiKey) return { ok: false, error: "exa_search: EXA_API_KEY not set" };

  const numResults = Math.min(
    Math.max(Number(args.numResults ?? args.num_results ?? 10) || 10, 1),
    SEARCH_RESULT_CAP,
  );

  try {
    const data = await exaRequest("/search", { query, numResults, type: "auto" }, apiKey);
    const hits = [];
    for (const r of (data.results || []).slice(0, SEARCH_RESULT_CAP)) {
      const catalogIndex = addToSearchCatalog(ctx, {
        url: r.url,
        title: r.title,
        score: r.score,
        query,
      });
      hits.push({
        catalog_index: catalogIndex,
        url: normalizeWireUrl(r.url),
        title: r.title || "",
        score: r.score,
      });
    }
    ctx.searchCount = (ctx.searchCount || 0) + 1;
    return { ok: true, query, resultCount: hits.length, hits };
  } catch (e) {
    return { ok: false, error: e.message || String(e) };
  }
}

export async function dispatchExaFetch(args, ctx) {
  const indices = Array.isArray(args?.url_indices) ? args.url_indices : [];
  if (!indices.length) return { ok: false, error: "exa_fetch: url_indices array required" };

  const apiKey = resolveExaApiKey();
  if (!apiKey) return { ok: false, error: "exa_fetch: EXA_API_KEY not set" };

  const urls = [];
  const resolved = [];
  for (const raw of indices.slice(0, FETCH_URL_CAP)) {
    const idx = Number(raw);
    if (!Number.isInteger(idx) || idx < 0 || idx >= ctx.searchCatalog.length) {
      return { ok: false, error: `exa_fetch: invalid catalog index ${raw}` };
    }
    const entry = ctx.searchCatalog[idx];
    urls.push(entry.url);
    resolved.push(idx);
  }

  const maxCharacters = Math.min(
    Math.max(Number(args.maxCharacters ?? args.max_characters ?? DEFAULT_FETCH_MAX_CHARS) || DEFAULT_FETCH_MAX_CHARS, 500),
    10_000,
  );

  try {
    const data = await exaRequest(
      "/contents",
      { urls, text: { maxCharacters } },
      apiKey,
    );
    const pages = [];
    for (const item of data.results || []) {
      const url = item.url || item.id || "";
      const text = item.text || item.content || "";
      const fetchIndex = addToFetchLog(ctx, {
        url,
        title: item.title,
        text,
      });
      pages.push({
        fetch_index: fetchIndex,
        catalog_index: findCatalogIndex(ctx, url),
        url: normalizeWireUrl(url),
        title: item.title || "",
        excerpt_count: ctx.fetchLog[fetchIndex]?.excerpts?.length || 0,
        text_preview: trimPreview(text),
      });
    }
    ctx.fetchCount = (ctx.fetchCount || 0) + 1;
    return { ok: true, fetched: pages.length, pages, url_indices: resolved };
  } catch (e) {
    return { ok: false, error: e.message || String(e) };
  }
}

export async function dispatchExaTool(name, args, ctx) {
  switch (name) {
    case "exa_search":
      return dispatchExaSearch(args, ctx);
    case "exa_fetch":
      return dispatchExaFetch(args, ctx);
    default:
      return { ok: false, error: `unknown tool: ${name}` };
  }
}

export async function dispatchExaToolBatch(calls, ctx) {
  return Promise.all(
    calls.map(async (call) => {
      const args = parseArguments(call.arguments);
      const result = await dispatchExaTool(call.name, args, ctx);
      return { call, args, result };
    }),
  );
}

export function extractFunctionCalls(response) {
  const out = [];
  const items = response?.output;
  if (!Array.isArray(items)) return out;
  for (const item of items) {
    if (!item || item.type !== "function_call") continue;
    const callId = item.call_id || item.id;
    const name = item.name;
    if (callId && name) {
      out.push({ call_id: String(callId), name: String(name), arguments: String(item.arguments || "") });
    }
  }
  return out;
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

export function shouldStopSearch(ctx, { stopSearches = Number(process.env.EXPLORE_WS_STOP_SEARCHES) || 4 } = {}) {
  return (ctx.searchCount || 0) >= stopSearches;
}
