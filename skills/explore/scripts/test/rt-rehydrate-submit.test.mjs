import assert from "node:assert/strict";
import path from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";
import {
  buildReadIndex,
  orderReadCacheEntries,
  rehydratePointerSubmit,
} from "../lib/rt-rehydrate-submit.mjs";
import { validateTraceObject } from "../lib/trace-schema.mjs";

const WORKSPACE = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../fixtures/search-mini-repo");

test("orderReadCacheEntries prioritizes seed paths", () => {
  const readCache = new Map([
    ["hooks/other.py", "1|x\n"],
    ["hooks/gate_stop.py", "1|#!/usr/bin/env python\n2|def adjudicate_dispute():\n"],
  ]);
  const ordered = orderReadCacheEntries(readCache, ["hooks/gate_stop.py"]);
  assert.equal(ordered[0][0], "hooks/gate_stop.py");
});

test("buildReadIndex emits excerpt_index lines", () => {
  const ordered = [
    ["hooks/gate_stop.py", "1|#!/usr/bin/env python\n2|def adjudicate_dispute():\n"],
  ];
  const index = buildReadIndex(ordered);
  assert.match(index, /\[0\] hooks\/gate_stop\.py/);
  assert.match(index, /READ INDEX/);
});

test("rehydratePointerSubmit maps citation_spans to code_passages", () => {
  const filesRead = new Set(["hooks/gate_stop.py"]);
  const readCache = new Map([
    ["hooks/gate_stop.py", "1|#!/usr/bin/env python\n2|def adjudicate_dispute():\n3|    pass\n"],
  ]);
  const pointer = {
    opening_summary: "Stop gate.",
    flow_steps: ["gate_stop adjudicates"],
    sections: [{ heading: "gate", body: "Stop hook." }],
    key_files: [{ path: "hooks/gate_stop.py", role: "stop gate" }],
    citation_spans: [{
      excerpt_index: 0,
      start_line: 1,
      end_line: 2,
      rationale: "entry and adjudicate",
    }],
  };
  const merged = rehydratePointerSubmit({
    pointer,
    orderedPaths: ["hooks/gate_stop.py"],
    workspace: WORKSPACE,
    filesRead,
    readCache,
    toolTurns: 1,
    question: "where is stop handled?",
  });
  assert.equal(merged.code_passages.length, 1);
  assert.equal(merged.code_passages[0].file_path, "hooks/gate_stop.py");
  assert.equal(merged.code_passages[0].start_line, 1);
  assert.ok(merged.code_passages[0].end_line >= 2);
  assert.equal(validateTraceObject(merged, { workspace: WORKSPACE, filesRead, toolTurns: 1 }), null);
});

test("rehydratePointerSubmit falls back when citations invalid", () => {
  const filesRead = new Set(["hooks/gate_stop.py"]);
  const readCache = new Map([
    ["hooks/gate_stop.py", "1|#!/usr/bin/env python\n2|def adjudicate_dispute():\n"],
  ]);
  const merged = rehydratePointerSubmit({
    pointer: {
      opening_summary: "Stop gate.",
      flow_steps: ["step"],
      sections: [],
      key_files: [],
      citation_spans: [{ excerpt_index: 99, start_line: 1, end_line: 2, rationale: "bad index" }],
    },
    orderedPaths: ["hooks/gate_stop.py"],
    workspace: WORKSPACE,
    filesRead,
    readCache,
    toolTurns: 1,
    question: "stop?",
  });
  assert.ok(merged.code_passages.length >= 1);
});
