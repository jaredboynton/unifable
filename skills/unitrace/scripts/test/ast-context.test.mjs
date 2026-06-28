import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";
import {
  detectAstBinary,
  enrichGrepLines,
  expandFinishRanges,
  expandLineRange,
  findEnclosingNode,
  langForPath,
  listAstNodes,
} from "../ast-context.mjs";

const FIXTURE = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../fixtures/search-mini-repo");
const GATE = path.join(FIXTURE, "hooks/gate_stop.py");

test("langForPath maps python extension", () => {
  assert.equal(langForPath("hooks/gate_stop.py"), "python");
  assert.equal(langForPath("scripts/search-lib.mjs"), "javascript");
});

test("expandLineRange expands narrow hit to enclosing function", () => {
  const binary = detectAstBinary();
  if (!binary) {
    test.skip("ast-grep not installed");
    return;
  }
  const exp = expandLineRange(GATE, 5, 5, { binary });
  assert.equal(exp.startLine, 4);
  assert.equal(exp.endLine, 6);
  assert.equal(exp.expanded, true);
});

test("findEnclosingNode picks smallest containing node", () => {
  const binary = detectAstBinary();
  if (!binary) {
    test.skip("ast-grep not installed");
    return;
  }
  const nodes = listAstNodes(GATE, { binary });
  assert.ok(nodes.length >= 1);
  const node = findEnclosingNode(nodes, 5);
  assert.ok(node);
  assert.equal(node.startLine, 4);
  assert.equal(node.endLine, 6);
});

test("expandFinishRanges merges AST bounds for finish slices", () => {
  const binary = detectAstBinary();
  if (!binary) {
    test.skip("ast-grep not installed");
    return;
  }
  const expanded = expandFinishRanges(FIXTURE, "hooks/gate_stop.py", [[5, 5]]);
  assert.deepEqual(expanded, [[4, 6]]);
});

test("enrichGrepLines appends ast context block", () => {
  const binary = detectAstBinary();
  if (!binary) {
    test.skip("ast-grep not installed");
    return;
  }
  const lines = ["hooks/gate_stop.py:5:    # fail open on internal errors"];
  const out = enrichGrepLines(FIXTURE, lines);
  assert.ok(out.length > lines.length);
  assert.match(out.join("\n"), /--- ast context ---/);
  assert.match(out.join("\n"), /\[4:6:hooks\/gate_stop\.py\]/);
  assert.match(out.join("\n"), /def adjudicate_dispute/);
});

test("enrichGrepLines leaves unsupported files unchanged", () => {
  const tmp = path.join(FIXTURE, ".ast-test.md");
  fs.writeFileSync(tmp, "# hello\n");
  try {
    const lines = ["README.md:1:# hello"];
    const out = enrichGrepLines(FIXTURE, lines);
    assert.equal(out.length, lines.length);
  } finally {
    fs.unlinkSync(tmp);
  }
});
