import assert from "node:assert/strict";
import path from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";
import {
  buildExploreToolSchemas,
  dispatchTool,
  dispatchToolBatch,
  extractFunctionCalls,
  parseArguments,
} from "../lib/rt-tools.mjs";
import { jwtExpUnix, providerError } from "../lib/realtime_client.mjs";

const FIXTURE = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../fixtures/search-mini-repo");

test("buildExploreToolSchemas returns explore_exec only", () => {
  const schemas = buildExploreToolSchemas();
  assert.equal(schemas.length, 1);
  assert.equal(schemas[0].name, "explore_exec");
});

test("dispatchTool explore_exec reads gate_stop.py", async () => {
  const reads = [];
  const r = await dispatchTool(
    "explore_exec",
    {
      code: `
const entry = await tools.read({ path: "hooks/gate_stop.py", start_line: 1, end_line: 8 });
return { path: entry.path };
`,
    },
    FIXTURE,
    { onRead: (rel) => reads.push(rel) }
  );
  assert.equal(r.ok, true);
  assert.equal(r.result.path, "hooks/gate_stop.py");
  assert.deepEqual(reads, ["hooks/gate_stop.py"]);
});

test("dispatchToolBatch runs multiple explore_exec calls", async () => {
  const calls = [
    {
      call_id: "c1",
      name: "explore_exec",
      arguments: JSON.stringify({ code: 'return await tools.grep({ pattern: "adjudicate" });' }),
    },
    {
      call_id: "c2",
      name: "explore_exec",
      arguments: JSON.stringify({ code: 'return await tools.read({ path: "hooks/gate_stop.py", start_line: 1, end_line: 3 });' }),
    },
  ];
  const out = await dispatchToolBatch(calls, FIXTURE);
  assert.equal(out.length, 2);
  assert.equal(out[0].result.ok, true);
  assert.equal(out[1].result.ok, true);
});

test("extractFunctionCalls pulls explore_exec items", () => {
  const calls = extractFunctionCalls({
    output: [
      { type: "function_call", call_id: "c1", name: "explore_exec", arguments: '{"code":"return 1;"}' },
      { type: "function_call", call_id: "c2", name: "explore_exec", arguments: '{"code":"return 2;"}' },
    ],
  });
  assert.equal(calls.length, 2);
  assert.equal(calls[0].name, "explore_exec");
});

test("parseArguments handles invalid json", () => {
  assert.deepEqual(parseArguments("not-json"), {});
  assert.deepEqual(parseArguments('{"code":"return 1;"}'), { code: "return 1;" });
});

test("providerError formats dict errors", () => {
  assert.match(providerError({ code: "x", message: "bad" }), /x: bad/);
});

test("jwtExpUnix parses exp from synthetic jwt", () => {
  const payload = Buffer.from(JSON.stringify({ exp: 2000000000 })).toString("base64url");
  const token = `hdr.${payload}.sig`;
  assert.equal(jwtExpUnix(token), 2000000000);
});

test("extractMapBlock ignores inline REPO MAP mention", async () => {
  const { extractMapBlock } = await import("../lib/rt-trace-utils.mjs");
  const prompt = `Orient from REPO MAP, then read.

REPO MAP:
scripts/foo.mjs:1-10

QUESTION: test`;
  assert.equal(extractMapBlock(prompt), "scripts/foo.mjs:1-10");
});
