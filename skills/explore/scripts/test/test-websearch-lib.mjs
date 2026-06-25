import path from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";
import assert from "node:assert/strict";
import {
  buildWebsearchPrompt,
  buildWebsearchSearchPrompt,
  buildWebsearchFetchPrompt,
  buildWebsearchExplorePrompt,
  hasContent,
  stripAgyOutput,
} from "../websearch-lib.mjs";
import { buildExploreSkillContext } from "../explore-skill-context.mjs";

const SKILL_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");

test("buildWebsearchSearchPrompt uses exa_search constraints", () => {
  const prompt = buildWebsearchSearchPrompt("agentic ripgrep search", { skillContext: false });
  assert.match(prompt, /exa_search/);
  assert.match(prompt, /parallel exa_search/);
  assert.doesNotMatch(prompt, /research_exec/);
  assert.doesNotMatch(prompt, /Exa MCP only/);
});

test("buildWebsearchFetchPrompt uses exa_fetch catalog indices", () => {
  const prompt = buildWebsearchFetchPrompt("MCP protocol", { skillContext: false });
  assert.match(prompt, /exa_fetch/);
  assert.match(prompt, /url_indices/);
  assert.match(prompt, /SEARCH CATALOG/);
});

test("buildWebsearchExplorePrompt delegates to search prompt", () => {
  const prompt = buildWebsearchExplorePrompt("test goal", { skillContext: false });
  assert.match(prompt, /exa_search/);
});

test("buildWebsearchPrompt includes goal and output rules", () => {
  const prompt = buildWebsearchPrompt("agentic ripgrep search", { skillContext: false });
  assert.match(prompt, /agentic ripgrep search/);
  assert.match(prompt, /Do not narrate your steps or tool calls/);
  assert.match(prompt, /Search via Exa MCP only/);
  assert.match(prompt, /fire multiple Exa search queries in parallel/);
  assert.match(prompt, /collect every promising URL/);
  assert.match(prompt, /batch-fetch all collected URLs at once/);
  assert.match(prompt, /frontier but viable/);
  assert.match(prompt, /reproducible/);
});

test("buildWebsearchPrompt default excludes explore skill inventory", () => {
  const prompt = buildWebsearchPrompt("JWT rotation best practices", { skillContext: false });
  assert.doesNotMatch(prompt, /search\.sh/);
  assert.doesNotMatch(prompt, /map\.sh/);
  assert.doesNotMatch(prompt, /Do NOT re-recommend: AST-aware/);
  assert.doesNotMatch(prompt, /generated from SKILL\.md \+ scripts\/ on disk/);
});

test("buildWebsearchPrompt welcomes obscure repos and enforces generic scope discipline", () => {
  const prompt = buildWebsearchPrompt("improve authentication middleware", { skillContext: false });
  assert.match(prompt, /obscure, niche, single-author/);
  assert.match(prompt, /Do not filter by stars/);
  assert.match(prompt, /explicit fit verdict: In scope \| Adjacent/);
  assert.match(prompt, /problem domain stated in the task/);
  assert.match(prompt, /Do NOT conflate adjacent domains/);
  assert.match(prompt, /Adjacent \/ out-of-scope/);
  assert.doesNotMatch(prompt, /read-only exploration\/tracing skills/);
  assert.doesNotMatch(prompt, /Do NOT recommend patch-execution sandboxes/);
});

test("buildWebsearchPrompt opt-in explore context from SKILL.md", () => {
  const ctx = buildExploreSkillContext(SKILL_DIR);
  const prompt = buildWebsearchPrompt("improve explore skill", {
    skillContext: true,
    skillDir: SKILL_DIR,
  });
  assert.ok(prompt.includes(ctx));
  assert.match(prompt, /generated from SKILL\.md \+ scripts\/ on disk/);
  assert.match(prompt, /search\.sh/);
  assert.match(prompt, /Do NOT re-recommend: Cerebras json_schema strict finish/);
});

test("hasContent rejects whitespace-only", () => {
  assert.equal(hasContent(""), false);
  assert.equal(hasContent("\n  \n"), false);
  assert.equal(hasContent("x"), true);
});

test("stripAgyOutput removes ANSI and control chars", () => {
  const raw = "\x1b[31mhello\x1b[0m\r\n^D";
  assert.equal(stripAgyOutput(raw), "hello\n");
});
