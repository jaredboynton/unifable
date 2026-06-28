// Realtime trace explore tools: single explore_exec entry (code_mode-style).
import { runExploreExec } from "./rt-explore-runtime.mjs";

const UNITRACE_EXEC_TOOL = {
  type: "function",
  name: "explore_exec",
  description:
    "Run read-only explore JavaScript with nested tools.grep/read/batch_read/list_dir/shell. Use Promise.all for parallel discovery and targeted line-range reads. Return a JSON-serializable summary object.",
  parameters: {
    type: "object",
    properties: {
      code: {
        type: "string",
        description: "Async JS body. tools.grep/read/batch_read/list_dir/shell are available. Use return or final expression.",
      },
    },
    required: ["code"],
    additionalProperties: false,
  },
};

export function buildExploreToolSchemas() {
  return [UNITRACE_EXEC_TOOL];
}

export const TOOL_SCHEMAS = buildExploreToolSchemas();

export function dispatchTool(name, args, workspace, ctx = {}) {
  const a = args && typeof args === "object" ? args : {};
  switch (name) {
    case "explore_exec": {
      if (typeof a.code !== "string" || !a.code.trim()) {
        return { ok: false, error: "explore_exec: code required" };
      }
      return runExploreExec(workspace, a.code, {
        deadlineMs: ctx.deadlineMs,
        onRead: ctx.onRead,
      });
    }
    default:
      return { ok: false, error: `unknown tool: ${name}` };
  }
}

export async function dispatchToolBatch(calls, workspace, ctx = {}) {
  return Promise.all(
    calls.map(async (call) => {
      const args = parseArguments(call.arguments);
      const result = await dispatchTool(call.name, args, workspace, ctx);
      return { call, args, result };
    })
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
