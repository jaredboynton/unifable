import test from "node:test";
import assert from "node:assert/strict";
import { isPreambleLine, makeLineHider } from "../lib/code-line.mjs";

// Run a source block through the stateful hider and return, for each line, the
// trimmed text plus whether the model would see it (false) or it is hidden (true).
function classify(path, src) {
  const hide = makeLineHider(path);
  return src.split("\n").map((line) => ({ text: line.trim(), hidden: hide(line) }));
}
const find = (rows, needle) => rows.find((r) => r.text.includes(needle));

test("isPreambleLine flags blanks, comments, single-line imports, bare punctuation", () => {
  for (const l of ["", "   ", "// note", "# note", "import x from 'y';", "export { a } from './m';", "});", "{"]) {
    assert.equal(isPreambleLine(l), true, `expected preamble: ${JSON.stringify(l)}`);
  }
  for (const l of ["const FETCH_URL_CAP = 8;", "function go() {", "  return cap;", "export const V = 1;"]) {
    assert.equal(isPreambleLine(l), false, `expected non-preamble: ${JSON.stringify(l)}`);
  }
});

test("makeLineHider hides multi-line import name lists but keeps real definitions", () => {
  const src = [
    "import {",
    "  RealtimeConnection,",
    "  RealtimeError,",
    '} from "./lib/realtime_client.mjs";',
    'import fs from "node:fs";',
    "const FETCH_URL_CAP = 8;",
    "export function doThing() {",
    "  return FETCH_URL_CAP;",
    "}",
  ].join("\n");
  const rows = classify("x.mjs", src);
  assert.equal(find(rows, "import {").hidden, true);
  assert.equal(find(rows, "RealtimeConnection").hidden, true);
  assert.equal(find(rows, "RealtimeError").hidden, true);
  assert.equal(find(rows, "realtime_client.mjs").hidden, true);
  assert.equal(find(rows, 'import fs from "node:fs"').hidden, true);
  assert.equal(find(rows, "const FETCH_URL_CAP = 8;").hidden, false);
  assert.equal(find(rows, "export function doThing()").hidden, false);
  assert.equal(find(rows, "return FETCH_URL_CAP;").hidden, false);
});

test("makeLineHider hides multi-line export re-export lists, not export definitions", () => {
  const src = [
    "export {",
    "  doThing,",
    '} from "./x.mjs";',
    "export const VALUE = 42;",
    "export class Widget {}",
  ].join("\n");
  const rows = classify("x.mjs", src);
  assert.equal(find(rows, "export {").hidden, true);
  assert.equal(find(rows, "doThing,").hidden, true);
  assert.equal(find(rows, "x.mjs").hidden, true);
  assert.equal(find(rows, "export const VALUE").hidden, false);
  assert.equal(find(rows, "export class Widget").hidden, false);
});

test("makeLineHider tracks multi-line block comments", () => {
  const src = [
    "/* a banner",
    "   spanning lines */",
    "const real = 1;",
  ].join("\n");
  const rows = classify("x.mjs", src);
  assert.equal(find(rows, "a banner").hidden, true);
  assert.equal(find(rows, "spanning lines").hidden, true);
  assert.equal(find(rows, "const real = 1;").hidden, false);
});

test("makeLineHider does not treat object literals as import blocks", () => {
  const src = [
    "const opts = {",
    "  retries: 3,",
    "};",
  ].join("\n");
  const rows = classify("x.mjs", src);
  // `const opts = {` is real code (non-preamble) and must stay visible.
  assert.equal(find(rows, "const opts = {").hidden, false);
  assert.equal(find(rows, "retries: 3,").hidden, false);
});
