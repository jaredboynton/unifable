import assert from "node:assert/strict";
import test from "node:test";
import { FINISH_RESPONSE_SCHEMA } from "../cerebras-search.mjs";
import { pathsFromToolHistory } from "../search-lib.mjs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const FIXTURE = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../fixtures/search-mini-repo");

test("FINISH_RESPONSE_SCHEMA is strict-mode compatible", () => {
  assert.equal(FINISH_RESPONSE_SCHEMA.additionalProperties, false);
  assert.deepEqual(FINISH_RESPONSE_SCHEMA.required, ["files"]);
});

test("pathsFromToolHistory strips ./ prefix from ripgrep paths", () => {
  const messages = [{
    role: "tool",
    content: "./scripts/gate/spec.py-1-#!/usr/bin/env python3\n./scripts/gate/spec.py:4:def resolve_frontier(task):",
  }];
  const paths = pathsFromToolHistory(messages, FIXTURE);
  assert.ok(paths.includes("scripts/gate/spec.py"));
});

test("pathsFromToolHistory includes read tool call paths", () => {
  const messages = [{
    role: "assistant",
    tool_calls: [{
      id: "t1",
      type: "function",
      function: { name: "read", arguments: JSON.stringify({ path: "scripts/gate/spec.py" }) },
    }],
  }];
  assert.deepEqual(pathsFromToolHistory(messages, FIXTURE), ["scripts/gate/spec.py"]);
});

test("pathsFromToolHistory ignores missing paths", () => {
  const messages = [{ role: "tool", content: "./src/nope.py:1:hello" }];
  assert.deepEqual(pathsFromToolHistory(messages, FIXTURE), []);
});
