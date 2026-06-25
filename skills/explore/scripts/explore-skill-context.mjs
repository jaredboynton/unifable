// explore-skill-context.mjs — build websearch EXPLORE_SKILL_CONTEXT from SKILL.md + scripts/.
//
// Single source of truth: SKILL.md tool table + section blurbs; augmented by on-disk modules.
// Cached by SKILL.md mtime + scripts dir mtime.

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const DEFAULT_SKILL_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

const MODULE_CAPABILITIES = [
  {
    files: ["ast-context.mjs"],
    label: "AST context (ast-grep)",
    skipRecommend: "AST-aware grep/finish expansion and map signature extraction via ast-grep (ast-context.mjs, map-ast-extract.mjs)",
  },
  {
    files: ["map-pagerank.mjs", "map-sigmap.mjs", "map-ast-extract.mjs", "map-lib.mjs"],
    label: "Map prefetch engines",
    skipRecommend: "map prefetch (pagerank/sigmap/tandem via map.sh; ast-grep signatures for uncovered langs)",
  },
  {
    files: ["realtime-search.mjs", "search-rt.mjs", "search.sh"],
    label: "Realtime semantic search (Codex OAuth)",
    skipRecommend: "gpt-realtime-2 agentic ripgrep search loop (search.sh / search-rt.mjs)",
  },
  {
    files: ["realtime-trace.mjs", "trace-rt.sh", "trace.sh"],
    label: "Realtime trace (Codex OAuth)",
    skipRecommend: "gpt-realtime-2 trace loop (trace.sh / trace-rt.sh)",
  },
];

const cache = new Map();

function readText(filePath) {
  try {
    return fs.readFileSync(filePath, "utf8");
  } catch {
    return "";
  }
}

function scriptsMtimeMs(scriptsDir) {
  let max = 0;
  try {
    for (const name of fs.readdirSync(scriptsDir)) {
      if (!name.endsWith(".sh") && !name.endsWith(".mjs")) continue;
      try {
        max = Math.max(max, fs.statSync(path.join(scriptsDir, name)).mtimeMs);
      } catch {
        /* skip */
      }
    }
  } catch {
    return 0;
  }
  return max;
}

function parseFrontmatterVersion(skillMd) {
  return skillMd.match(/^\s*version:\s*"([^"]+)"/m)?.[1] ?? "unknown";
}

function parseToolTable(skillMd) {
  const tools = [];
  for (const row of skillMd.matchAll(/^\|\s*\*\*[^|]+\*\*\s*\|\s*`([^`]+)`\s*\|/gm)) {
    tools.push(row[1]);
  }
  return tools;
}

function parseSection(skillMd, heading) {
  const re = new RegExp(`^## ${heading.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\s*$`, "m");
  const start = skillMd.search(re);
  if (start === -1) return "";
  const rest = skillMd.slice(start);
  const next = rest.slice(rest.indexOf("\n") + 1).search(/^## /m);
  return next === -1 ? rest : rest.slice(0, next + rest.indexOf("\n") + 1);
}

function firstParagraph(section) {
  const lines = section.split("\n").slice(1);
  const out = [];
  for (const line of lines) {
    if (!line.trim()) {
      if (out.length) break;
      continue;
    }
    if (line.startsWith("#") || line.startsWith("```")) break;
    out.push(line.trim());
  }
  return out.join(" ").replace(/\s+/g, " ").trim();
}

function extractBoldFeatures(section) {
  return [...section.matchAll(/^\*\*([^*]+):\*\*\s*(.+)$/gm)].map((m) => `${m[1]}: ${m[2].trim()}`);
}

function discoverModules(scriptsDir) {
  const found = [];
  const skip = [];
  for (const mod of MODULE_CAPABILITIES) {
    if (mod.files.every((f) => fs.existsSync(path.join(scriptsDir, f)))) {
      found.push(mod.label);
      skip.push(mod.skipRecommend);
    }
  }
  return { found, skip };
}

function summarizeToolSection(skillMd, heading, toolName) {
  const section = parseSection(skillMd, heading);
  if (!section) return null;
  const bits = [firstParagraph(section), ...extractBoldFeatures(section)].filter(Boolean);
  if (!bits.length) return `- ${toolName} — present in skill`;
  return `- ${toolName} — ${bits.join(" ")}`;
}

export function buildExploreSkillContext(skillDir = DEFAULT_SKILL_DIR) {
  const resolved = path.resolve(skillDir);
  const skillMdPath = path.join(resolved, "SKILL.md");
  const scriptsDir = path.join(resolved, "scripts");

  let skillMtime = 0;
  try {
    skillMtime = fs.statSync(skillMdPath).mtimeMs;
  } catch {
    return fallbackContext(resolved);
  }

  const cacheKey = `${skillMdPath}:${skillMtime}:${scriptsMtimeMs(scriptsDir)}`;
  if (cache.has(cacheKey)) return cache.get(cacheKey);

  const skillMd = readText(skillMdPath);
  const version = parseFrontmatterVersion(skillMd);
  const tableTools = parseToolTable(skillMd);
  const { found: moduleLabels, skip: skipRecommend } = discoverModules(scriptsDir);

  const sectionSummaries = [
    summarizeToolSection(skillMd, "search.sh — fast in-repo code location", "search.sh"),
    summarizeToolSection(skillMd, "map.sh — token-budgeted repo map prefetch", "map.sh"),
    summarizeToolSection(skillMd, "trace-rt.sh — deep behavioral understanding (gpt-realtime-2)", "trace-rt.sh"),
    summarizeToolSection(skillMd, "websearch.sh — external research RT default (gpt-realtime-2)", "websearch.sh"),
    summarizeToolSection(skillMd, "websearch-rt.sh — external research direct RT entry", "websearch-rt.sh"),
  ].filter(Boolean);

  const defaultTraceLine =
    "- trace.sh — default deep trace (gpt-realtime-2 via trace-rt.sh; explore low / submit minimal).";
  const stackLines = sectionSummaries.length
    ? [defaultTraceLine, ...sectionSummaries]
    : [
        defaultTraceLine,
        ...tableTools.map((t) => `- ${t} — listed in SKILL.md tool table`),
      ];

  const skipLines = [
    "Do NOT recommend re-implementing capabilities already listed below unless proposing a clearly different technique or a targeted fix to a documented gap.",
    ...skipRecommend.map((s) => `- Do NOT re-recommend: ${s}.`),
  ];

  const text = `Default consumer context (apply when the task involves improving agent skills, codebase exploration, tracing, or search tooling):
- Explore skill v${version} (generated from SKILL.md + scripts/ on disk).
- Read-only stack: locate code (search.sh default RT / search-rt.mjs), prefetch repo structure (map.sh), understand behavior (trace.sh default RT / trace-rt.sh), gather external prior art (websearch.sh default RT / websearch-rt.sh).
- Tool table (SKILL.md): ${tableTools.join(", ") || "see SKILL.md"}.
- Existing stack (shell + Node, zero npm deps):
${stackLines.join("\n")}
${moduleLabels.length ? `- Verified modules on disk: ${moduleLabels.join(", ")}.` : ""}
${skipLines.join("\n")}
- Prefer additions that are shell/Node scripts, optional MCP servers, or thin wrappers — not mandatory heavy infra (always-on daemons, embedding pipelines, multi-GB Docker registries) unless the task explicitly asks for them.`;

  cache.set(cacheKey, text);
  return text;
}

function fallbackContext(skillDir) {
  return `Default consumer context (apply when the task involves improving agent skills, codebase exploration, tracing, or search tooling):
- Explore skill at ${skillDir} (SKILL.md unreadable; using minimal fallback).
- Tools: search.sh (default RT), map.sh, trace.sh (default RT), websearch.sh (default RT), websearch-rt.sh.
- Prefer shell/Node additions; avoid mandatory heavy infra unless explicitly requested.`;
}

export function clearExploreSkillContextCache() {
  cache.clear();
}
