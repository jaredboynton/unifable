import assert from "node:assert/strict";
import path from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";
import {
  extractFunctionCalls,
  extractOutputText,
  parseJsonFromResponse,
  providerError,
} from "../lib/xai_client.mjs";

test("extractFunctionCalls pulls function_call items", () => {
  const calls = extractFunctionCalls({
    output: [
      { type: "function_call", call_id: "c1", name: "grep", arguments: '{"pattern":"foo"}' },
    ],
  });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].name, "grep");
});

test("extractOutputText reads message content blocks", () => {
  const text = extractOutputText({
    output: [
      {
        type: "message",
        content: [{ type: "output_text", text: "hello" }],
      },
    ],
  });
  assert.equal(text, "hello");
});

test("parseJsonFromResponse parses structured message", () => {
  const obj = parseJsonFromResponse({
    output: [
      {
        type: "message",
        content: [{ type: "output_text", text: '{"foo":1}' }],
      },
    ],
  });
  assert.deepEqual(obj, { foo: 1 });
});

test("providerError formats error object", () => {
  assert.match(providerError({ error: { code: "x", message: "bad" } }, 400), /x: bad/);
});

const FIXTURE = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../fixtures/search-mini-repo");
const REPLAY = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../fixtures/grok-trace-replay.json");

test("grok-trace replay fixture path exists", () => {
  assert.ok(REPLAY.endsWith("grok-trace-replay.json"));
  assert.ok(FIXTURE.endsWith("search-mini-repo"));
});
