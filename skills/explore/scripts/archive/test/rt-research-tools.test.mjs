import test from "node:test";
import assert from "node:assert/strict";
import {
  buildResearchToolSchemas,
  dispatchResearchTool,
  extractFunctionCalls,
  parseArguments,
} from "../lib/rt-research-tools.mjs";

test("buildResearchToolSchemas exposes research_exec", () => {
  const tools = buildResearchToolSchemas();
  assert.equal(tools.length, 1);
  assert.equal(tools[0].name, "research_exec");
});

test("extractFunctionCalls parses function_call items", () => {
  const calls = extractFunctionCalls({
    output: [
      { type: "function_call", call_id: "c1", name: "research_exec", arguments: "{\"code\":\"return 1\"}" },
    ],
  });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].name, "research_exec");
});

test("parseArguments handles object and JSON string", () => {
  assert.deepEqual(parseArguments({ code: "x" }), { code: "x" });
  assert.deepEqual(parseArguments("{\"code\":\"y\"}"), { code: "y" });
});

test("dispatchResearchTool rejects unknown tool", async () => {
  const result = await dispatchResearchTool("other", {}, createCtx());
  assert.equal(result.ok, false);
  assert.match(result.error, /unknown tool/);
});

function createCtx() {
  return {
    urlsFetched: new Set(),
    searchHits: [],
    fetchLog: new Map(),
    searchCount: 0,
    toolTurns: 0,
  };
}
