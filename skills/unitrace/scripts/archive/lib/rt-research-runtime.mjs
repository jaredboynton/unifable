// rt-research-runtime.mjs — research_exec sandbox with direct Exa REST tools.
import { normalizeWireUrl } from "./explore-wire-format.mjs";

const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const DEFAULT_EXEC_TIMEOUT_MS = Number(process.env.UNISEARCH_WS_EXEC_TIMEOUT_MS) || 25_000;
const DEFAULT_FETCH_MAX_CHARS = Number(process.env.UNISEARCH_WS_FETCH_MAX_CHARS) || 3000;
const SEARCH_RESULT_CAP = 20;
const FETCH_URL_CAP = 8;
const EXCERPT_MAX = Number(process.env.UNISEARCH_WS_FETCH_EXCERPT_MAX) || 1200;

function execResultMax() {
  const v = Number(process.env.UNISEARCH_WS_EXEC_RESULT_MAX);
  return Number.isFinite(v) && v > 0 ? v : 32_000;
}

function summarizeForModel(value, depth = 0) {
  if (value == null) return value;
  if (typeof value === "string") {
    return value.length > 1200 ? `${value.slice(0, 1200)}…` : value;
  }
  if (typeof value !== "object") return value;
  if (Array.isArray(value)) {
    const cap = depth === 0 ? 30 : 15;
    return value.slice(0, cap).map((v) => summarizeForModel(v, depth + 1));
  }
  const out = {};
  for (const [k, v] of Object.entries(value)) {
    out[k] = summarizeForModel(v, depth + 1);
  }
  return out;
}

function capResult(value) {
  const summarized = summarizeForModel(value);
  let text;
  try {
    text = JSON.stringify(summarized);
  } catch {
    text = String(summarized);
  }
  const max = execResultMax();
  if (text.length <= max) return summarized;
  return {
    truncated: true,
    message: "research_exec result exceeded size cap",
    preview: text.slice(0, Math.min(max, 8000)),
  };
}

function preflightResearchExecCode(code) {
  const src = String(code || "");
  const bad = /\b(eval\s*\(|new\s+Function|require\s*\(|import\s+|process\.|child_process|globalThis\.fetch\s*\()/i;
  if (bad.test(src)) {
    return { ok: false, error: "research_exec: disallowed pattern in code" };
  }
  return { ok: true };
}

function resolveExaApiKey() {
  const key = process.env.EXA_API_KEY || "";
  if (!key.trim()) return null;
  return key.trim();
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

function trimExcerpt(text, max = EXCERPT_MAX) {
  const s = String(text || "").replace(/\s+/g, " ").trim();
  return s.length <= max ? s : `${s.slice(0, max)}…`;
}

function buildTools(apiKey, ctx) {
  const trackSearch = (query, results) => {
    ctx.searchCount = (ctx.searchCount || 0) + 1;
    for (const r of results) {
      ctx.searchHits.push({ url: r.url, title: r.title, query });
    }
  };

  const trackFetch = (url, text) => {
    const norm = normalizeWireUrl(url);
    ctx.urlsFetched.add(norm);
    ctx.fetchLog.set(norm, trimExcerpt(text));
  };

  return Object.freeze({
    exa_search: async (args = {}) => {
      const query = String(args.query || "").trim();
      if (!query) throw new Error("exa_search: query required");
      const numResults = Math.min(
        Math.max(Number(args.numResults ?? args.num_results ?? 10) || 10, 1),
        SEARCH_RESULT_CAP,
      );
      const data = await exaRequest("/search", { query, numResults, type: "auto" }, apiKey);
      const results = (data.results || []).slice(0, SEARCH_RESULT_CAP).map((r) => ({
        url: r.url,
        title: r.title || "",
        score: r.score,
      }));
      trackSearch(query, results);
      return { ok: true, query, resultCount: results.length, results };
    },
    exa_fetch: async (args = {}) => {
      const urls = Array.isArray(args.urls) ? args.urls.map(String).filter(Boolean) : [];
      if (!urls.length) throw new Error("exa_fetch: urls array required");
      const maxCharacters = Math.min(
        Math.max(Number(args.maxCharacters ?? args.max_characters ?? DEFAULT_FETCH_MAX_CHARS) || DEFAULT_FETCH_MAX_CHARS, 500),
        10_000,
      );
      const batch = urls.slice(0, FETCH_URL_CAP);
      const data = await exaRequest(
        "/contents",
        { urls: batch, text: { maxCharacters } },
        apiKey,
      );
      const pages = [];
      for (const item of data.results || []) {
        const url = item.url || item.id || "";
        const text = item.text || item.content || "";
        if (url) trackFetch(url, text);
        pages.push({
          url,
          title: item.title || "",
          text_preview: trimExcerpt(text, 800),
        });
      }
      return { ok: true, fetched: pages.length, pages };
    },
  });
}

export function createResearchContext() {
  return {
    urlsFetched: new Set(),
    searchHits: [],
    fetchLog: new Map(),
    searchCount: 0,
    toolTurns: 0,
  };
}

export async function runResearchExec(code, ctx = {}, { deadlineMs } = {}) {
  if (!code || !String(code).trim()) return { ok: false, error: "research_exec: empty code" };
  const preflight = preflightResearchExecCode(code);
  if (!preflight.ok) return preflight;

  const apiKey = resolveExaApiKey();
  if (!apiKey) return { ok: false, error: "research_exec: EXA_API_KEY not set" };

  const timeoutMs = deadlineMs && deadlineMs > 0
    ? Math.min(deadlineMs - Date.now(), DEFAULT_EXEC_TIMEOUT_MS)
    : DEFAULT_EXEC_TIMEOUT_MS;
  if (timeoutMs <= 0) return { ok: false, error: "research_exec: deadline exceeded" };

  const tools = buildTools(apiKey, ctx);
  let fn;
  try {
    fn = new AsyncFunction("tools", `"use strict";\n${code}`);
  } catch (e) {
    return { ok: false, error: `research_exec compile error: ${e.message}` };
  }

  let timer;
  try {
    const result = await Promise.race([
      fn(tools),
      new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error("research_exec timed out")), timeoutMs);
      }),
    ]);
    ctx.toolTurns = (ctx.toolTurns || 0) + 1;
    return { ok: true, result: capResult(result) };
  } catch (e) {
    return { ok: false, error: e.message || String(e) };
  } finally {
    if (timer) clearTimeout(timer);
  }
}

export function shouldStopResearch(ctx, {
  stopSearches = Number(process.env.UNISEARCH_WS_STOP_SEARCHES) || 4,
} = {}) {
  return (ctx.searchCount || 0) >= stopSearches;
}
