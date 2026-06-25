// rt-web-run.mjs — Realtime web_run bridge via Codex alpha/search or /responses web_search.
import { randomUUID } from "node:crypto";
import { RealtimeError } from "./realtime_client.mjs";
import {
  ALPHA_MODEL_OUTPUT_CAP,
  buildAlphaSearchBody,
  postAlphaSearch,
} from "./codex-alpha-search-client.mjs";
import {
  buildCodexResponsesBody,
  newCodexRequestIds,
  postCodexResponses,
} from "./codex-responses-client.mjs";

export const WEB_RUN_TOOL_NAME = "web_run";

export function buildWebRunToolSpec({ allowOpen = false } = {}) {
  const properties = {
    search_query: {
      type: "array",
      description: "Batch multiple web search queries in one call (recommended: 4-8 varied phrasings).",
      minItems: 1,
      maxItems: 8,
      items: {
        type: "object",
        properties: {
          q: { type: "string", description: "Search query text." },
          recency: { type: "integer", description: "Optional recency filter in days." },
          domains: {
            type: "array",
            items: { type: "string" },
            description: "Optional domain allowlist.",
          },
        },
        required: ["q"],
        additionalProperties: false,
      },
    },
  };
  if (allowOpen) {
    properties.open = {
      type: "array",
      description:
        "Fetch the full text of specific URLs to read real page content (not just search snippets). Pass the most relevant 3-6 URLs.",
      minItems: 1,
      maxItems: 8,
      items: { type: "string", description: "Absolute URL to open and read." },
    };
  }
  return {
    type: "function",
    name: WEB_RUN_TOOL_NAME,
    description: allowOpen
      ? "Read the live web. Use search_query to discover sources (one call, 4-8 varied queries), and open to fetch the full text of the most relevant URLs for real evidence."
      : "Search the live web. Prefer ONE call with 4-8 varied search_query entries (different angles/phrasings) rather than many separate calls.",
    parameters: {
      type: "object",
      properties,
      additionalProperties: false,
    },
  };
}

export function parseWebRunArguments(raw) {
  if (raw == null || raw === "") return {};
  if (typeof raw === "object") return raw;
  try {
    return JSON.parse(String(raw));
  } catch (e) {
    throw new RealtimeError(`web_run arguments JSON parse failed: ${e.message}`);
  }
}

export function webRunCommandsFromArgs(args) {
  const a = args && typeof args === "object" ? args : {};
  const commands = {};
  if (Array.isArray(a.search_query) && a.search_query.length) {
    commands.search_query = a.search_query;
  }
  const openUrls = (Array.isArray(a.open) ? a.open : [])
    .map((u) => (typeof u === "string" ? u : u?.ref_id || u?.url))
    .map((u) => String(u || "").trim())
    .filter(Boolean);
  if (openUrls.length) {
    commands.open = openUrls.map((ref_id) => ({ ref_id }));
  }
  if (!commands.search_query && !commands.open) {
    throw new RealtimeError("web_run requires at least one search_query { q } entry or open URL");
  }
  return commands;
}

export function promptFromSearchCommands(commands, fallbackPrompt = "") {
  const queries = commands?.search_query;
  if (!Array.isArray(queries) || !queries.length) return fallbackPrompt;
  const lines = queries.map((q) => {
    const parts = [`Search: ${q.q}`];
    if (q.recency != null) parts.push(`recency=${q.recency}d`);
    if (Array.isArray(q.domains) && q.domains.length) parts.push(`domains=${q.domains.join(",")}`);
    return parts.join(" ");
  });
  return `${lines.join("\n")}\n\nUse web search and answer with citations.`;
}

export async function callAlphaSearch({
  authPathOverride,
  searchModel,
  commands,
  sessionId = randomUUID(),
  externalWebAccess = true,
  maxOutputTokens = Number(process.env.EXPLORE_WS_ALPHA_MAX_OUTPUT_TOKENS) || ALPHA_MODEL_OUTPUT_CAP,
} = {}) {
  const body = buildAlphaSearchBody({
    sessionId,
    model: searchModel,
    commands,
    externalWebAccess,
    maxOutputTokens,
  });

  const result = await postAlphaSearch({ authPathOverride, body });
  return {
    output: result.output,
    encryptedOutput: result.encryptedOutput,
    request: { body },
  };
}

export async function callResponsesWebSearch({
  authPathOverride,
  searchModel,
  userPrompt,
  reasoningEffort = "low",
  externalWebAccess = true,
  ids = newCodexRequestIds(),
  timeoutMs,
} = {}) {
  const body = buildCodexResponsesBody({
    model: searchModel,
    promptText: userPrompt,
    ids,
    reasoningEffort,
    toolChoice: "required",
    tools: [{ type: "web_search", external_web_access: externalWebAccess }],
    instructions:
      "You are a focused research assistant. Use the web_search tool, then answer succinctly with source URLs.",
    requestKind: "rt_web_run",
    harness: "explore",
  });

  const result = await postCodexResponses({ authPathOverride, ids, body, ...(timeoutMs != null ? { timeoutMs } : {}) });
  if (!result.outputText) {
    throw new RealtimeError("codex responses web_search produced no output text");
  }

  return {
    output: result.outputText,
    responseId: result.responseId,
    events: result.events,
    request: { ids, body },
  };
}
