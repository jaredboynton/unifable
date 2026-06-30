import assert from "node:assert/strict";
import path from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";
import {
  traceProviderSchema,
  validateTraceObject,
  applyGroundingManifest,
  safeRelPath,
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
  assert.equal(schema.properties.code_passages.maxItems, 5);
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

test("validateTraceObject enforces seed+submit question boundaries", () => {
  const filesRead = new Set([
    "skills/unitrace/scripts/lib/rt-map-seed.mjs",
    "skills/unitrace/scripts/lib/rt-explore-nav.mjs",
    "skills/unitrace/scripts/realtime-trace.mjs",
  ]);
  const data = {
    opening_summary: "summary",
    flow_steps: ["seedExploreReads populates seed state", "buildSubmitPacket consumes it"],
    comparison_tables: [],
    sections: [{ heading: "seed", body: "seed" }],
    key_files: [],
    code_passages: [
      { file_path: "skills/unitrace/scripts/lib/rt-map-seed.mjs", start_line: 300, end_line: 320, rationale: "seed" },
      { file_path: "skills/unitrace/scripts/lib/rt-explore-nav.mjs", start_line: 350, end_line: 370, rationale: "nav" },
      { file_path: "skills/unitrace/scripts/realtime-trace.mjs", start_line: 632, end_line: 700, rationale: "submit packet" },
    ],
    grounding_manifest: {
      files_read: [...filesRead].sort(),
      tool_turns: 2,
    },
  };
  assert.equal(
    validateTraceObject(data, {
      workspace: REPO_ROOT,
      filesRead,
      toolTurns: 2,
      question: "How does the nav explore path seed files and then build the submit packet for a final trace?",
    }),
    null,
  );
});

test("validateTraceObject rejects seed+submit drift into downstream submit transport", () => {
  const filesRead = new Set([
    "skills/unitrace/scripts/lib/rt-map-seed.mjs",
    "skills/unitrace/scripts/lib/rt-explore-nav.mjs",
    "skills/unitrace/scripts/realtime-trace.mjs",
  ]);
  const data = {
    opening_summary: "summary",
    flow_steps: ["seed", "submit"],
    comparison_tables: [],
    sections: [{ heading: "seed", body: "seed" }],
    key_files: [],
    code_passages: [
      { file_path: "skills/unitrace/scripts/lib/rt-map-seed.mjs", start_line: 300, end_line: 320, rationale: "seed" },
      { file_path: "skills/unitrace/scripts/lib/rt-explore-nav.mjs", start_line: 350, end_line: 370, rationale: "nav" },
      { file_path: "skills/unitrace/scripts/realtime-trace.mjs", start_line: 916, end_line: 955, rationale: "wire submit" },
    ],
    grounding_manifest: {
      files_read: [...filesRead].sort(),
      tool_turns: 2,
    },
  };
  assert.match(
    validateTraceObject(data, {
      workspace: REPO_ROOT,
      filesRead,
      toolTurns: 2,
      question: "How does the nav explore path seed files and then build the submit packet for a final trace?",
    }),
    /buildSubmitPacket|downstream submit transport/,
  );
});
