// map-lib.mjs — shared repo map utilities for explore map prefetch.
//
// Zero npm dependencies. Node 18+.

import { createHash } from "node:crypto";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

export const MAP_MODES = new Set(["none", "pagerank", "sigmap", "tandem"]);

const SKIP_NAMES = new Set([
  ".git", ".svn", ".hg", ".bzr",
  "node_modules", "bower_components", ".pnpm", ".yarn", "vendor", "packages", "Pods", ".bundle",
  "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".venv", "venv", ".tox", ".nox", ".eggs",
  "dist", "build", "out", "output", "target", "_build", ".next", ".nuxt", ".output", ".vercel", ".netlify",
  ".cache", ".parcel-cache", ".turbo", ".nx", ".gradle",
  ".idea", ".vscode", ".vs",
  "coverage", ".coverage", "htmlcov", ".nyc_output",
  "tmp", "temp", ".tmp", ".temp",
]);

const SKIP_EXTENSIONS = [".min.js", ".min.css", ".bundle.js", ".wasm", ".so", ".dll", ".pyc", ".map", ".js.map"];

const SOURCE_EXTENSIONS = new Set([
  ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx",
  ".go", ".rs", ".sh", ".bash", ".java", ".c", ".cpp", ".h", ".hpp",
  ".rb", ".php", ".swift", ".kt", ".scala", ".md",
]);

const MAX_FILES = 5000;

function shouldSkipName(name) {
  if (SKIP_NAMES.has(name)) return true;
  if (name.startsWith(".") && name !== ".") return true;
  for (const ext of SKIP_EXTENSIONS) {
    if (name.endsWith(ext)) return true;
  }
  return false;
}

function isSourceFile(relPath) {
  const ext = path.extname(relPath).toLowerCase();
  return SOURCE_EXTENSIONS.has(ext);
}

export function resolveRepoRoot(root) {
  return path.resolve(root || process.cwd());
}

// Enumerate source files plus provenance the prefetch needs to bail on
// pathological trees: viaGit (git ls-files succeeded) and truncated (hit the
// maxFiles cap). A non-git tree that hits the cap is a home dir or cache, not a
// project worth mapping.
export function listRepoFilesMeta(repoRoot, { maxFiles = MAX_FILES } = {}) {
  const root = resolveRepoRoot(repoRoot);
  const git = spawnSync("git", ["-C", root, "ls-files", "-z"], { encoding: "buffer", maxBuffer: 64 * 1024 * 1024 });
  if (git.status === 0 && git.stdout?.length) {
    const all = git.stdout
      .toString("utf8")
      .split("\0")
      .filter(Boolean)
      .filter((rel) => !shouldSkipName(path.basename(rel)) && isSourceFile(rel));
    if (all.length) {
      return { files: all.slice(0, maxFiles), viaGit: true, truncated: all.length > maxFiles };
    }
  }

  const results = [];
  let hitCap = false;
  function walk(dir, depth) {
    if (results.length >= maxFiles || depth > 8) return;
    let entries;
    try {
      entries = fs.readdirSync(dir, { withFileTypes: true });
    } catch {
      return;
    }
    for (const ent of entries) {
      if (results.length >= maxFiles) {
        hitCap = true;
        break;
      }
      if (shouldSkipName(ent.name)) continue;
      const full = path.join(dir, ent.name);
      const rel = path.relative(root, full);
      if (rel.startsWith("..")) continue;
      if (ent.isDirectory()) walk(full, depth + 1);
      else if (isSourceFile(rel)) results.push(rel.split(path.sep).join("/"));
    }
  }
  walk(root, 0);
  return { files: results.sort(), viaGit: false, truncated: hitCap || results.length >= maxFiles };
}

export function listRepoFiles(repoRoot, opts = {}) {
  return listRepoFilesMeta(repoRoot, opts).files;
}

export function readRepoFile(repoRoot, relPath) {
  const abs = path.join(resolveRepoRoot(repoRoot), relPath);
  try {
    return fs.readFileSync(abs, "utf8");
  } catch {
    return null;
  }
}

export function estimateTokens(text) {
  return Math.ceil((text || "").length / 4);
}

export function charBudgetFromTokens(tokens) {
  return Math.max(256, tokens * 4);
}

export function tokenizeQuery(query) {
  return (query || "")
    .toLowerCase()
    .replace(/[^a-z0-9_/.-]+/g, " ")
    .split(/\s+/)
    .filter((t) => t.length >= 2);
}

export function mentionedIdentsFromQuery(query) {
  const tokens = tokenizeQuery(query);
  const idents = new Set(tokens);
  for (const t of tokens) {
    if (t.includes("/")) {
      for (const part of t.split("/")) {
        if (part.length >= 2) idents.add(part);
      }
    }
    const base = t.replace(/\.[a-z0-9]+$/i, "");
    if (base.length >= 2) idents.add(base);
  }
  return idents;
}

export function repoFingerprint(repoRoot) {
  const root = resolveRepoRoot(repoRoot);
  const git = spawnSync("git", ["-C", root, "rev-parse", "HEAD"], { encoding: "utf8" });
  if (git.status === 0) {
    return git.stdout.trim();
  }
  let latest = 0;
  for (const rel of listRepoFiles(root, { maxFiles: 200 })) {
    try {
      latest = Math.max(latest, fs.statSync(path.join(root, rel)).mtimeMs);
    } catch {
      /* ignore */
    }
  }
  return `mtime:${Math.floor(latest)}`;
}

export function cacheDirFor(repoRoot, mode) {
  const root = resolveRepoRoot(repoRoot);
  const id = createHash("sha256").update(root).digest("hex").slice(0, 16);
  return path.join(os.homedir(), ".cache", "explore", "maps", id, mode);
}

export function cacheKey(repoRoot, query, budgetChars) {
  const fp = repoFingerprint(repoRoot);
  const qh = createHash("sha256").update(query || "").digest("hex").slice(0, 12);
  return `${fp}-${qh}-${budgetChars}.txt`;
}

export function readMapCache(repoRoot, mode, query, budgetChars) {
  const dir = cacheDirFor(repoRoot, mode);
  const file = path.join(dir, cacheKey(repoRoot, query, budgetChars));
  try {
    const st = fs.statSync(file);
    const text = fs.readFileSync(file, "utf8");
    return { text, mtimeMs: st.mtimeMs, fromCache: true };
  } catch {
    return null;
  }
}

export function writeMapCache(repoRoot, mode, query, budgetChars, text) {
  const dir = cacheDirFor(repoRoot, mode);
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(path.join(dir, cacheKey(repoRoot, query, budgetChars)), text, "utf8");
}

export function fitRankedToBudget(rankedItems, renderItems, budgetChars) {
  if (!rankedItems.length) return "";
  let lower = 0;
  let upper = rankedItems.length;
  let best = "";
  let bestLen = 0;
  while (lower <= upper) {
    const mid = Math.floor((lower + upper) / 2);
    const slice = rankedItems.slice(0, Math.max(1, mid));
    const rendered = renderItems(slice);
    const len = rendered.length;
    const pctErr = Math.abs(len - budgetChars) / Math.max(1, budgetChars);
    if (len <= budgetChars && len >= bestLen) {
      best = rendered;
      bestLen = len;
    }
    if (pctErr < 0.15) {
      best = rendered;
      break;
    }
    if (len < budgetChars) lower = mid + 1;
    else upper = mid - 1;
  }
  return best;
}

export function renderMapHeader(mode) {
  return `# ${mode}`;
}

export function formatMapLine(relPath, startLine, endLine, name, kind) {
  const range = startLine && endLine ? `:${startLine}-${endLine}` : "";
  const suffix = name ? `  ${name}${kind ? ` ${kind}` : ""}` : "";
  return `${relPath}${range}${suffix}`;
}

export function wrapRepoMapBlock(mode, body) {
  const tag = mode === "tandem" ? "tandem" : mode;
  return `<repo_map ${tag}="true">\n${body}\n</repo_map>`;
}
