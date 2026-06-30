import assert from "node:assert/strict";
import test from "node:test";

import {
  buildToolTurnSchema,
  createRtinferToolCaller,
  serializeToolLoopMessages,
} from "../lib/rtinfer-tool-caller.mjs";

const TOOLS = [
  {
    type: "function",
    function: {
      name: "grep_search",
      description: "Search the repo",
      parameters: {
        type: "object",
        additionalProperties: false,
        required: ["pattern"],
        properties: { pattern: { type: "string" } },
      },
    },
  },
  {
    type: "function",
    function: {
      name: "finish",
      description: "Return final files",
      parameters: {
        type: "object",
        additionalProperties: false,
        required: ["files"],
        properties: { files: { type: "string" } },
      },
    },
  },
];

test("buildToolTurnSchema restricts tool names and finish-only cardinality", () => {
  const normal = buildToolTurnSchema(TOOLS.map((t) => t.function));
  assert.deepEqual(normal.properties.tool_calls.items.properties.name.enum, ["grep_search", "finish"]);
  const finishOnly = buildToolTurnSchema([TOOLS[1].function], { finishOnly: true });
  assert.equal(finishOnly.properties.tool_calls.minItems, 1);
  assert.equal(finishOnly.properties.tool_calls.maxItems, 1);
  assert.deepEqual(finishOnly.properties.tool_calls.items.properties.name.enum, ["finish"]);
});

test("serializeToolLoopMessages preserves tool history context", () => {
  const text = serializeToolLoopMessages([
    { role: "user", content: "find auth" },
    {
      role: "assistant",
      tool_calls: [{ id: "call_1", function: { name: "grep_search", arguments: "{\"pattern\":\"auth\"}" } }],
    },
    { role: "tool", tool_call_id: "call_1", content: "{\"ok\":true}" },
  ]);
  assert.match(text, /\[1\] USER/);
  assert.match(text, /name=grep_search/);
  assert.match(text, /TOOL_RESULT call_id=call_1/);
});

test("createRtinferToolCaller converts structured daemon output to chat tool calls", async () => {
  let seen = null;
  const caller = createRtinferToolCaller({
    systemPrompt: "You are a search agent.",
    toolSpecs: TOOLS,
    askFn: async (req) => {
      seen = req;
      return {
        assistant_text: "",
        tool_calls: [
          { id: "call_7", name: "grep_search", arguments_json: "{\"pattern\":\"auth\"}" },
        ],
      };
    },
  });
  const out = await caller([{ role: "user", content: "find auth" }]);
  assert.equal(seen.schemaName, "tool_turn");
  assert.equal(seen.model, undefined);
  assert.deepEqual(out.tool_calls, [
    {
      id: "call_7",
      type: "function",
      function: { name: "grep_search", arguments: "{\"pattern\":\"auth\"}" },
    },
  ]);
});

test("createRtinferToolCaller falls back when daemon path returns null", async () => {
  const caller = createRtinferToolCaller({
    systemPrompt: "You are a search agent.",
    toolSpecs: TOOLS,
    askFn: async () => null,
    fallback: async () => ({ content: null, tool_calls: [{ id: "f1", type: "function", function: { name: "finish", arguments: "{\"files\":\"\"}" } }] }),
  });
  const out = await caller([{ role: "user", content: "find nothing" }], { finishOnly: true });
  assert.equal(out.tool_calls[0].function.name, "finish");
});
