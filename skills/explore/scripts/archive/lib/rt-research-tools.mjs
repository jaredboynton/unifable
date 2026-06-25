// rt-research-tools.mjs — Realtime websearch explore tools (research_exec).
import { runResearchExec } from "./rt-research-runtime.mjs";

const RESEARCH_EXEC_TOOL = {
  type: "function",
  name: "research_exec",
  description:
    "Run external research JavaScript with nested tools.exa_search and tools.exa_fetch. Use Promise.all for parallel searches and batch fetches. Return a JSON-serializable summary object.",
  parameters: {
    type: "object",
    properties: {
      code: {
        type: "string",
        description: "Async JS body. tools.exa_search({query,numResults}) and tools.exa_fetch({urls,maxCharacters}) are available.",
      },
    },
    required: ["code"],
    additionalProperties: false,
  },
};

export function buildResearchToolSchemas() {
  return [RESEARCH_EXEC_TOOL];
}

export function dispatchResearchTool(name, args, ctx = {}, options = {}) {
  const a = args && typeof args === "object" ? args : {};
  switch (name) {
    case "research_exec": {
      if (typeof a.code !== "string" || !a.code.trim()) {
        return { ok: false, error: "research_exec: code required" };
      }
      return runResearchExec(a.code, ctx, { deadlineMs: options.deadlineMs });
    }
    default:
      return { ok: false, error: `unknown tool: ${name}` };
  }
}

export async function dispatchResearchToolBatch(calls, ctx = {}, options = {}) {
  return Promise.all(
    calls.map(async (call) => {
      const args = parseArguments(call.arguments);
      const result = await dispatchResearchTool(call.name, args, ctx, options);
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
