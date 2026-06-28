import assert from "node:assert/strict";
import path from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";
import { detectAstBinary } from "../ast-context.mjs";
import {
  AST_MAP_LANGS,
  clearMapAstCache,
  extractAstSignatures,
  shouldUseAstForFile,
} from "../map-ast-extract.mjs";
import { extractSignatures, extractSignaturesRegex } from "../map-sigmap.mjs";

const FIXTURE = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../fixtures/map-ast-repo");
const JAVA = path.join(FIXTURE, "src/GateService.java");
const RUBY = path.join(FIXTURE, "lib/frontier.rb");

test("shouldUseAstForFile auto skips regex langs", () => {
  const prev = process.env.UNITRACE_MAP_AST;
  process.env.UNITRACE_MAP_AST = "auto";
  try {
    assert.equal(shouldUseAstForFile("scripts/map.mjs"), false);
    assert.equal(shouldUseAstForFile("hooks/gate_stop.py"), false);
    assert.equal(shouldUseAstForFile("src/GateService.java"), true);
  } finally {
    if (prev === undefined) delete process.env.UNITRACE_MAP_AST;
    else process.env.UNITRACE_MAP_AST = prev;
  }
});

test("shouldUseAstForFile mode 0 disables ast", () => {
  const prev = process.env.UNITRACE_MAP_AST;
  process.env.UNITRACE_MAP_AST = "0";
  try {
    assert.equal(shouldUseAstForFile("src/GateService.java"), false);
  } finally {
    if (prev === undefined) delete process.env.UNITRACE_MAP_AST;
    else process.env.UNITRACE_MAP_AST = prev;
  }
});

test("extractSignaturesRegex returns empty for java", () => {
  const content = "public class Foo { void bar() {} }\n";
  assert.deepEqual(extractSignaturesRegex("src/Foo.java", content), []);
});

test("extractAstSignatures finds java and ruby defs", async (t) => {
  if (!detectAstBinary()) {
    t.skip("ast-grep not installed");
    return;
  }
  clearMapAstCache();

  const javaSigs = extractAstSignatures(JAVA);
  assert.ok(javaSigs.some((s) => s.name === "resolve_frontier"));
  assert.ok(javaSigs.some((s) => s.name === "GateService"));

  const rubySigs = extractAstSignatures(RUBY);
  assert.ok(rubySigs.some((s) => s.name === "adjudicate_dispute"));
  assert.ok(rubySigs.some((s) => s.name === "Frontier") || rubySigs.some((s) => s.name === "Nested"));
});

test("extractSignatures uses ast path for java fixture", async (t) => {
  if (!detectAstBinary()) {
    t.skip("ast-grep not installed");
    return;
  }
  clearMapAstCache();
  const prev = process.env.UNITRACE_MAP_AST;
  process.env.UNITRACE_MAP_AST = "auto";
  try {
    const content = "public class Foo { void bar() {} }\n";
    const sigs = extractSignatures("src/GateService.java", content, { repoRoot: FIXTURE });
    assert.ok(sigs.some((s) => s.name === "resolve_frontier"));
    assert.equal(sigs[0].lang, "java");
  } finally {
    if (prev === undefined) delete process.env.UNITRACE_MAP_AST;
    else process.env.UNITRACE_MAP_AST = prev;
  }
});

test("extractAstSignatures returns empty when binary missing", () => {
  clearMapAstCache();
  const sigs = extractAstSignatures(JAVA, { binary: null });
  assert.deepEqual(sigs, []);
});

test("extractSignatures returns empty for java without ast-grep", () => {
  const prev = process.env.UNITRACE_MAP_AST;
  process.env.UNITRACE_MAP_AST = "auto";
  clearMapAstCache();
  try {
    const content = "public class Foo { void bar() {} }\n";
    const sigs = extractSignatures("src/Foo.java", content, {
      repoRoot: FIXTURE,
      binary: null,
    });
    assert.deepEqual(sigs, []);
  } finally {
    if (prev === undefined) delete process.env.UNITRACE_MAP_AST;
    else process.env.UNITRACE_MAP_AST = prev;
  }
});

test("AST_MAP_LANGS covers expected langs", () => {
  for (const lang of ["java", "kotlin", "ruby", "csharp", "swift", "php"]) {
    assert.ok(AST_MAP_LANGS.has(lang));
  }
});
