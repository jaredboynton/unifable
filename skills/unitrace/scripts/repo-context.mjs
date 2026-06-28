// repo-context.mjs — lightweight caller-repo context from AGENTS.md / CLAUDE.md / README.
//
// Zero npm dependencies. Used by websearch to ground research in the caller workspace
// without coupling to explore skill internals.

import fs from "node:fs";
import path from "node:path";

const DOC_CANDIDATES = ["AGENTS.md", "CLAUDE.md", "README.md"];
const MAX_LINES = 80;
const MAX_BULLETS = 12;

function readText(filePath) {
  try {
    return fs.readFileSync(filePath, "utf8");
  } catch {
    return "";
  }
}

function extractBullets(lines) {
  const bullets = [];
  let inWhereToLook = false;

  for (const raw of lines) {
    const line = raw.trimEnd();
    const trimmed = line.trim();

    if (/^#{1,3}\s+WHERE TO LOOK/i.test(trimmed)) {
      inWhereToLook = true;
      continue;
    }
    if (inWhereToLook && /^#{1,3}\s+/.test(trimmed) && !/^#{1,3}\s+WHERE TO LOOK/i.test(trimmed)) {
      inWhereToLook = false;
    }

    if (inWhereToLook && trimmed.startsWith("|") && trimmed.includes("|")) {
      const cells = trimmed.split("|").map((c) => c.trim()).filter(Boolean);
      if (cells.length >= 2 && !/^[-—]+$/.test(cells[0]) && !/^task$/i.test(cells[0])) {
        bullets.push(`${cells[0]}: ${cells.slice(1).join(" — ")}`);
      }
      continue;
    }

    if (/^[-*]\s+\*\*/.test(trimmed)) {
      bullets.push(trimmed.replace(/^[-*]\s+/, "").replace(/\*\*/g, ""));
      continue;
    }
    if (/^[-*]\s+/.test(trimmed) && trimmed.length > 4) {
      bullets.push(trimmed.replace(/^[-*]\s+/, ""));
      continue;
    }

    const heading = trimmed.match(/^#{1,3}\s+(.+)$/);
    if (heading && !/^(overview|structure|setup|notes)$/i.test(heading[1])) {
      bullets.push(heading[1]);
    }
  }

  return [...new Set(bullets)].slice(0, MAX_BULLETS);
}

function findDocFile(workspace) {
  for (const name of DOC_CANDIDATES) {
    const filePath = path.join(workspace, name);
    if (fs.existsSync(filePath)) return { name, filePath };
  }
  return null;
}

export function buildRepoContext(workspace) {
  const resolved = path.resolve(workspace || process.cwd());
  const doc = findDocFile(resolved);
  if (!doc) return "";

  const lines = readText(doc.filePath).split("\n").slice(0, MAX_LINES);
  const bullets = extractBullets(lines);
  if (!bullets.length) return "";

  const bulletBlock = bullets.map((b) => `- ${b}`).join("\n");
  return `Caller context (from ${doc.name} in ${resolved}):
- Workspace: ${resolved}
- Existing capabilities (from docs):
${bulletBlock}
- Do NOT re-recommend capabilities already documented above unless proposing a clearly different technique or a targeted fix to a documented gap.`;
}
