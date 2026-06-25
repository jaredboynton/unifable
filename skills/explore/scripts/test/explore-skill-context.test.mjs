import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";
import {
  buildExploreSkillContext,
  clearExploreSkillContextCache,
} from "../explore-skill-context.mjs";

const SKILL_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");

test("buildExploreSkillContext includes version and default trace from SKILL.md", () => {
  clearExploreSkillContextCache();
  const ctx = buildExploreSkillContext(SKILL_DIR);
  assert.match(ctx, /Explore skill v0\.9\./);
  assert.match(ctx, /search\.sh/);
  assert.match(ctx, /map\.sh/);
  assert.match(ctx, /trace-rt\.sh/);
  assert.match(ctx, /trace\.sh — default deep trace \(gpt-realtime-2 via trace-rt\.sh/);
  assert.match(ctx, /websearch\.sh/);
});

test("buildExploreSkillContext lists verified modules on disk", () => {
  clearExploreSkillContextCache();
  const ctx = buildExploreSkillContext(SKILL_DIR);
  assert.match(ctx, /AST context \(ast-grep\)/);
  assert.match(ctx, /Realtime semantic search \(Codex OAuth\)/);
  assert.match(ctx, /Do NOT re-recommend: AST-aware grep/);
  assert.match(ctx, /Do NOT re-recommend: gpt-realtime-2 agentic ripgrep search loop/);
});

test("buildExploreSkillContext lists verified modules from scripts/", () => {
  clearExploreSkillContextCache();
  const ctx = buildExploreSkillContext(SKILL_DIR);
  assert.match(ctx, /ast-context\.mjs/);
  assert.match(ctx, /search\.sh \/ search-rt\.mjs/);
  assert.match(ctx, /trace\.sh \/ trace-rt\.sh/);
});

test("buildExploreSkillContext caches until SKILL.md changes", () => {
  clearExploreSkillContextCache();
  const a = buildExploreSkillContext(SKILL_DIR);
  const b = buildExploreSkillContext(SKILL_DIR);
  assert.equal(a, b);
});

test("buildExploreSkillContext fallback when SKILL.md missing", () => {
  clearExploreSkillContextCache();
  const tmp = fs.mkdtempSync(path.join(SKILL_DIR, ".tmp-skill-"));
  try {
    const ctx = buildExploreSkillContext(tmp);
    assert.match(ctx, /minimal fallback/);
  } finally {
    fs.rmSync(tmp, { recursive: true, force: true });
  }
});
