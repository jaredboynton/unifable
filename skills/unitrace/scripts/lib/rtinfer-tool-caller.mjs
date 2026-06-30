import { rtinferAsk } from "./rtinfer-client.mjs";

function normalizeToolSpec(spec) {
  const fn = spec?.function || spec;
  return {
    name: String(fn?.name || ""),
    description: String(fn?.description || "").trim(),
    parameters: fn?.parameters && typeof fn.parameters === "object"
      ? fn.parameters
      : { type: "object", properties: {}, additionalProperties: false },
  };
}

function toolCatalogText(toolSpecs) {
  return toolSpecs.map((tool) => [
    `TOOL: ${tool.name}`,
    tool.description || "(no description)",
    `PARAMETERS JSON SCHEMA: ${JSON.stringify(tool.parameters)}`,
  ].join("\n")).join("\n\n");
}

export function serializeToolLoopMessages(messages) {
  const parts = [];
  for (const [idx, msg] of (messages || []).entries()) {
    const n = idx + 1;
    if (!msg || typeof msg !== "object") continue;
    if (msg.role === "user") {
      parts.push(`[${n}] USER\n${String(msg.content || "").trim()}`);
      continue;
    }
    if (msg.role === "assistant") {
      const lines = [];
      if (typeof msg.content === "string" && msg.content.trim()) {
        lines.push(`TEXT:\n${msg.content.trim()}`);
      }
      if (Array.isArray(msg.tool_calls) && msg.tool_calls.length) {
        lines.push("TOOL_CALLS:");
        for (const call of msg.tool_calls) {
          lines.push(`- id=${call.id} name=${call.function?.name} arguments=${call.function?.arguments ?? ""}`);
        }
      }
      parts.push(`[${n}] ASSISTANT\n${lines.join("\n").trim() || "(empty)"}`);
      continue;
    }
    if (msg.role === "tool") {
      parts.push(
        `[${n}] TOOL_RESULT call_id=${msg.tool_call_id || ""}\n${String(msg.content || "").trim()}`
      );
      continue;
    }
    parts.push(`[${n}] ${String(msg.role || "UNKNOWN").toUpperCase()}\n${String(msg.content || "").trim()}`);
  }
  return parts.join("\n\n");
}

export function buildToolTurnSchema(toolSpecs, { finishOnly = false } = {}) {
  const names = [...new Set(toolSpecs.map((tool) => tool.name).filter(Boolean))];
  return {
    type: "object",
    additionalProperties: false,
    required: ["tool_calls"],
    properties: {
      assistant_text: {
        type: "string",
        description: "Optional internal note. Prefer empty string unless the transcript explicitly needs a short assistant message.",
      },
      tool_calls: {
        type: "array",
        minItems: finishOnly ? 1 : 0,
        maxItems: finishOnly ? 1 : Math.max(1, Math.min(8, names.length * 2)),
        items: {
          type: "object",
          additionalProperties: false,
          required: ["id", "name", "arguments_json"],
          properties: {
            id: {
              type: "string",
              description: "A fresh call id for this assistant turn, such as call_1.",
            },
            name: {
              type: "string",
              enum: names,
              description: finishOnly
                ? "Must be the finish tool."
                : "One of the available tool names.",
            },
            arguments_json: {
              type: "string",
              description: "A valid JSON object string matching the selected tool's parameters schema exactly.",
            },
          },
        },
      },
    },
  };
}

function buildUserPrompt(messages, toolSpecs, { finishOnly = false } = {}) {
  const mode = finishOnly
    ? "Return exactly one finish tool call. If there are no findings, finish with an empty files string."
    : "Return the next assistant turn as tool calls. Do not execute tools yourself; only describe the calls.";
  return [
    "AVAILABLE TOOLS:",
    toolCatalogText(toolSpecs),
    "",
    "TRANSCRIPT (oldest first):",
    serializeToolLoopMessages(messages) || "(empty)",
    "",
    mode,
    "Every tool_calls[].arguments_json value must be a valid JSON object string for that tool.",
    "Do not invent tool names or file paths beyond what the transcript already established.",
  ].join("\n");
}

export function convertStructuredToolTurn(parsed) {
  const toolCalls = Array.isArray(parsed?.tool_calls) ? parsed.tool_calls : [];
  return {
    content:
      typeof parsed?.assistant_text === "string" && parsed.assistant_text.trim()
        ? parsed.assistant_text
        : null,
    tool_calls: toolCalls.map((call, idx) => ({
      id: String(call?.id || `call_${idx + 1}`),
      type: "function",
      function: {
        name: String(call?.name || ""),
        arguments: String(call?.arguments_json || "{}"),
      },
    })),
  };
}

function debugLog(enabled, namespace, message) {
  if (!enabled) return;
  try {
    process.stderr.write(`[daemon] ns=${namespace} served rtinfer=1 direct=0\n`);
    process.stderr.write(`[rtinfer-tool] ns=${namespace} ${message}\n`);
  } catch {
    // ignore stderr failures
  }
}

export function createRtinferToolCaller({
  systemPrompt,
  toolSpecs,
  finishToolName = "finish",
  model,
  reasoningEffort,
  namespace = "tool-loop",
  schemaName = "tool_turn",
  addendum = "",
  debug = false,
  askFn = rtinferAsk,
  fallback = null,
} = {}) {
  const allTools = (toolSpecs || []).map(normalizeToolSpec).filter((tool) => tool.name);
  const finishTools = allTools.filter((tool) => tool.name === finishToolName);

  async function call(messages, meta = {}) {
    const finishOnly = Boolean(meta.finishOnly);
    const selectedTools = finishOnly ? finishTools : allTools;
    if (!selectedTools.length) {
      return { content: null, tool_calls: [] };
    }

    const parsed = await askFn({
      system: [systemPrompt, addendum, "Return structured tool calls only."].filter(Boolean).join("\n\n"),
      user: buildUserPrompt(messages, selectedTools, { finishOnly }),
      schema: buildToolTurnSchema(selectedTools, { finishOnly }),
      schemaName,
      model,
      reasoningEffort,
    });

    if (!parsed) {
      if (typeof fallback === "function") return fallback(messages, meta);
      return null;
    }

    debugLog(debug, namespace, `turn tools=${selectedTools.map((tool) => tool.name).join(",")}`);
    return convertStructuredToolTurn(parsed);
  }

  call.close = () => {
    if (fallback && typeof fallback.close === "function") fallback.close();
  };
  call.warm = async () => {
    if (fallback && typeof fallback.warm === "function") return fallback.warm();
  };
  return call;
}
