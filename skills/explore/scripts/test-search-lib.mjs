import assert from "node:assert/strict";
import path from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";
import {
  fileExistsOnDisk,
  formatFinishRejection,
  parseFinishFiles,
  readFinishFiles,
  rgGrep,
  runSearch,
  validateFinishFiles,
  buildInitialState,
  pathsFromToolHistory,
} from "./search-lib.mjs";
import { detectAstBinary } from "./ast-context.mjs";

const FIXTURE = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "fixtures/search-mini-repo");

test("validateFinishFiles accepts existing repo-relative paths", () => {
  const v = validateFinishFiles(FIXTURE, "hooks/gate_stop.py:1-5");
  assert.equal(v.kind, "ok");
  assert.equal(v.files.length, 1);
});

test("validateFinishFiles rejects hallucinated paths", () => {
  const v = validateFinishFiles(FIXTURE, "src/quantum_flux_capacitor.py:1-10");
  assert.equal(v.kind, "rejected");
  assert.deepEqual(v.listedPaths, ["src/quantum_flux_capacitor.py"]);
});

test("validateFinishFiles treats empty files as legitimate no-findings", () => {
  const v = validateFinishFiles(FIXTURE, "");
  assert.equal(v.kind, "empty");
  assert.deepEqual(v.files, []);
});

test("formatFinishRejection names missing paths", () => {
  const v = validateFinishFiles(FIXTURE, "missing.py");
  const msg = formatFinishRejection(FIXTURE, v);
  assert.match(msg, /FINISH REJECTED/);
  assert.match(msg, /missing.py/);
});

test("rgGrep is case-insensitive by default", () => {
  const res = rgGrep(FIXTURE, { pattern: "fail.open" });
  const joined = res.lines.join("\n");
  assert.match(joined, /fail open/i);
});

test("runSearch mock rejects bad finish then accepts good finish", async () => {
  let call = 0;
  const files = await runSearch("disputes on stop fail-open", {
    repoRoot: FIXTURE,
    debug: false,
    callModel: async (messages) => {
      call += 1;
      if (call === 1) {
        return {
          content: null,
          tool_calls: [{
            id: "tc1",
            type: "function",
            function: {
              name: "finish",
              arguments: JSON.stringify({ files: "src/quantum_flux_capacitor.py:1-5" }),
            },
          }],
        };
      }
      if (call === 2) {
        return {
          content: null,
          tool_calls: [{
            id: "tc2",
            type: "function",
            function: {
              name: "grep_search",
              arguments: JSON.stringify({ pattern: "dispute" }),
            },
          }],
        };
      }
      return {
        content: null,
        tool_calls: [{
          id: "tc3",
          type: "function",
          function: {
            name: "finish",
            arguments: JSON.stringify({ files: "hooks/gate_stop.py:1-8" }),
          },
        }],
      };
    },
  });
  assert.ok(files);
  assert.equal(files.length, 1);
  assert.equal(files[0].path, "hooks/gate_stop.py");
  assert.ok(call >= 3);
});

test("parseFinishFiles handles line ranges", () => {
  const parsed = parseFinishFiles("scripts/gate/spec.py:1-3,10-12");
  assert.equal(parsed.length, 1);
  assert.deepEqual(parsed[0].lines, [[1, 3], [10, 12]]);
});

test("fileExistsOnDisk resolves repo-relative paths", () => {
  assert.equal(fileExistsOnDisk(FIXTURE, "scripts/gate/spec.py"), true);
  assert.equal(fileExistsOnDisk(FIXTURE, "nope.py"), false);
});

test("buildInitialState includes repo_map block when provided", () => {
  const state = buildInitialState(FIXTURE, "disputes", { mapText: "# sigmap\nhooks/gate_stop.py:4" });
  assert.match(state, /<repo_map>/);
  assert.match(state, /gate_stop/);
});

test("buildInitialState caps repo_structure when a map is present", () => {
  // With a map, depth is 1 and the listing is dir-skeleton capped; assert it is
  // materially smaller than the no-map (depth-2, 200-entry) listing.
  const withMap = buildInitialState(FIXTURE, "disputes", { mapText: "# m\nx" });
  const noMap = buildInitialState(FIXTURE, "disputes", {});
  const lines = (s) => s.match(/<repo_structure>\n([\s\S]*?)<\/repo_structure>/)[1].split("\n").length;
  assert.ok(lines(withMap) <= lines(noMap));
  assert.ok(lines(withMap) <= 64); // repoRoot + <=60 entries + optional omitted line
});

test("runSearch rejects implicit empty finish when tool history confirmed paths", async () => {
  let call = 0;
  const files = await runSearch("disputes", {
    repoRoot: FIXTURE,
    callModel: async (messages) => {
      call += 1;
      if (call === 1) {
        return {
          content: null,
          tool_calls: [{
            id: "tc1",
            type: "function",
            function: {
              name: "finish",
              arguments: JSON.stringify({ files: "src/quantum_flux_capacitor.py:1-5" }),
            },
          }],
        };
      }
      if (call === 2) {
        return {
          content: null,
          tool_calls: [{
            id: "tc2",
            type: "function",
            function: {
              name: "grep_search",
              arguments: JSON.stringify({ pattern: "dispute" }),
            },
          }],
        };
      }
      if (call === 3) {
        return { content: '{"files":""}', tool_calls: [] };
      }
      return {
        content: null,
        tool_calls: [{
          id: "finish1",
          type: "function",
          function: {
            name: "finish",
            arguments: JSON.stringify({ files: "hooks/gate_stop.py:4-6" }),
          },
        }],
      };
    },
  });
  assert.ok(files?.length);
  assert.equal(files[0].path, "hooks/gate_stop.py");
  assert.ok(call >= 4);
});

test("runSearch falls back to confirmed paths when finish extension fails", async () => {
  let call = 0;
  const files = await runSearch("disputes", {
    repoRoot: FIXTURE,
    callModel: async (messages, meta) => {
      call += 1;
      if (meta?.finishOnly) {
        return { content: '{"files":""}', tool_calls: [] };
      }
      if (call === 1) {
        return {
          content: null,
          tool_calls: [{
            id: "tc1",
            type: "function",
            function: {
              name: "grep_search",
              arguments: JSON.stringify({ pattern: "dispute" }),
            },
          }],
        };
      }
      for (let t = 2; t <= 8; t++) {
        if (call === t) return { content: '{"files":""}', tool_calls: [] };
      }
      return { content: '{"files":""}', tool_calls: [] };
    },
  });
  assert.ok(files?.length);
  assert.equal(files[0].path, "hooks/gate_stop.py");
});

test("pathsFromToolHistory finds ./ prefixed grep paths", () => {
  const paths = pathsFromToolHistory(
    [{ role: "tool", content: "./hooks/gate_stop.py:4:def adjudicate_dispute" }],
    FIXTURE,
  );
  assert.deepEqual(paths, ["hooks/gate_stop.py"]);
});

test("readFinishFiles expands narrow ranges to enclosing AST nodes", () => {
  if (!detectAstBinary()) {
    test.skip("ast-grep not installed");
    return;
  }
  const refs = readFinishFiles(FIXTURE, [{ path: "hooks/gate_stop.py", lines: [[5, 5]] }]);
  assert.equal(refs.length, 1);
  assert.equal(refs[0].startLine, 4);
  assert.equal(refs[0].endLine, 6);
  assert.match(refs[0].content, /def adjudicate_dispute/);
});
