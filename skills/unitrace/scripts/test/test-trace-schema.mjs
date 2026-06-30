import assert from "node:assert/strict";
import path from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";
import {
  traceProviderSchema,
  validateTraceObject,
  applyGroundingManifest,
  safeRelPath,
  MAX_CODE_PASSAGES,
} from "../lib/trace-schema.mjs";
import { renderTraceStructured } from "../lib/render-trace-structured.mjs";

const FIXTURE = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../fixtures/search-mini-repo");
const READ_FILE = "hooks/gate_stop.py";
const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../../..");

function sampleTrace(overrides = {}) {
  return {
    opening_summary: "Stop gate lives in gate_stop.py.",
    flow_steps: ["gate_stop.py defines stop handler"],
    comparison_tables: [],
    sections: [{ heading: "gate_stop", body: "Defines stop behavior." }],
    key_files: [{ path: READ_FILE, role: "stop gate" }],
    code_passages: [{
      file_path: READ_FILE,
      start_line: 1,
      end_line: 3,
      rationale: "Entry point",
    }],
    grounding_manifest: { files_read: [READ_FILE], tool_turns: 2 },
    ...overrides,
  };
}

test("traceProviderSchema has required top-level keys", () => {
  const schema = traceProviderSchema();
  assert.ok(schema.required.includes("opening_summary"));
  assert.ok(schema.required.includes("code_passages"));
  assert.equal(schema.additionalProperties, false);
});

test("traceProviderSchema can constrain code passage paths to files read", () => {
  const schema = traceProviderSchema({
    allowedCodePassagePaths: ["scripts/grok-trace.mjs", READ_FILE, READ_FILE],
  });
  const filePathSchema = schema.properties.code_passages.items.properties.file_path;
  assert.equal(schema.properties.code_passages.minItems, 1);
  assert.equal(schema.properties.code_passages.maxItems, MAX_CODE_PASSAGES);
  assert.deepEqual(filePathSchema.enum, [READ_FILE, "scripts/grok-trace.mjs"]);
});

test("safeRelPath rejects traversal", () => {
  assert.equal(safeRelPath(FIXTURE, "../etc/passwd"), null);
  assert.equal(safeRelPath(FIXTURE, READ_FILE), READ_FILE);
});

test("validateTraceObject accepts grounded trace", () => {
  const data = sampleTrace();
  const filesRead = new Set([READ_FILE]);
  assert.equal(validateTraceObject(data, { workspace: FIXTURE, filesRead, toolTurns: 2 }), null);
});

test("validateTraceObject rejects ungrounded code_passage", () => {
  const data = sampleTrace({
    code_passages: [{
      file_path: "scripts/gate/spec.py",
      start_line: 1,
      end_line: 2,
      rationale: "not read",
    }],
  });
  const filesRead = new Set([READ_FILE]);
  const err = validateTraceObject(data, { workspace: FIXTURE, filesRead, toolTurns: 2 });
  assert.match(err, /not read during explore/);
});

test("applyGroundingManifest overwrites manifest", () => {
  const filesRead = new Set([READ_FILE]);
  const out = applyGroundingManifest(sampleTrace(), filesRead, 5);
  assert.deepEqual(out.grounding_manifest.files_read, [READ_FILE]);
  assert.equal(out.grounding_manifest.tool_turns, 5);
});

test("renderTraceStructured produces markdown sections", () => {
  const md = renderTraceStructured(FIXTURE, sampleTrace());
  assert.match(md, /Stop gate lives/);
  assert.match(md, /## Flow/);
  assert.match(md, /## Key files/);
  assert.match(md, /## gate_stop/);
  assert.match(md, /## Code references/);
  assert.match(md, /```1:3:hooks\/gate_stop\.py/);
});
