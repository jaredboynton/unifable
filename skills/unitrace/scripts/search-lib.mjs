import { spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { enrichGrepLines, expandFinishRanges, stripCommentsEnabled } from "./ast-context.mjs";
import { makeLineHider } from "./lib/code-line.mjs";

export const AGENT_CONFIG = {
  MAX_TURNS: 8,
  FINISH_EXTENSION_TURNS: 3,
  MAX_CONTEXT_CHARS: 321600,
  MAX_OUTPUT_LINES: 200,
  MAX_LIST_RESULTS: 500,
  MAX_READ_LINES: 800,
  MAX_LIST_DEPTH: 3,
  LIST_TIMEOUT_MS: 2000,
};

const SKIP_NAMES = new Set([
  ".git", ".svn", ".hg", ".bzr",
  "node_modules", "bower_components", ".pnpm", ".yarn", "vendor", "packages", "Pods", ".bundle",
  "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".venv", "venv", ".tox", ".nox", ".eggs",
  "dist", "build", "out", "output", "target", "_build", ".next", ".nuxt", ".output", ".vercel", ".netlify",
  ".cache", ".parcel-cache", ".turbo", ".nx", ".gradle",
  ".idea", ".vscode", ".vs",
  "coverage", ".coverage", "htmlcov", ".nyc_output",
  "tmp", "temp", ".tmp", ".temp",
  "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "bun.lockb", "Cargo.lock", "Gemfile.lock", "poetry.lock",
]);

const SKIP_EXTENSIONS = [".min.js", ".min.css", ".bundle.js", ".wasm", ".so", ".dll", ".pyc", ".map", ".js.map"];

const BUILTIN_EXCLUDES = [
  ".git", ".svn", ".hg", ".bzr",
  "node_modules", ".pnpm", ".yarn", "vendor", "Pods", ".bundle",
  "__pycache__", ".venv", "venv",
  "dist", "build", "out", "target", ".next", ".nuxt",
  ".cache", ".turbo",
  "*.min.js", "*.min.css", "*.wasm", "*.so", "*.dll", "*.pyc", "*.map", "*.js.map",
];

export const SYSTEM_PROMPT = [
  "You are a code-search agent operating over a local repository through tools.",
  "Use list_directory, grep_search, read, and glob to locate code relevant to the user's query.",
  "Every path in finish MUST be a repo-relative path you confirmed exists via grep_search, glob, or read in this session.",
  "Never invent or guess paths. Absolute paths outside the repo are rejected.",
  "When you have found relevant code, call the finish tool with path:start-end lines (one file per line).",
  "When nothing in the repo matches the query, call finish with an empty files string.",
  "Never answer in plain assistant text -- always end by calling finish.",
].join(" ");

const TRUNCATED = "[truncated for context limit]";

export function debugLog(enabled, ...parts) {
  if (!enabled) return;
  process.stderr.write(`[search] ${parts.join(" ")}\n`);
}

function shouldSkip(name) {
  if (SKIP_NAMES.has(name)) return true;
  if (name.startsWith(".") && name !== ".") return true;
  for (const ext of SKIP_EXTENSIONS) {
    if (name.endsWith(ext)) return true;
  }
  return false;
}

export function resolveUnderRoot(root, p) {
  if (!p || p === ".") return root;
  const abs = path.isAbsolute(p) ? p : path.resolve(root, p);
  const rel = path.relative(root, abs);
  if (rel.startsWith("..")) throw new Error(`path "${p}" escapes repository root`);
  return abs;
}

function toRepoRelative(root, abs) {
  return path.relative(root, abs);
}

function runRg(args, cwd) {
  const result = spawnSync("rg", args, { cwd, encoding: "utf8", maxBuffer: 10 * 1024 * 1024 });
  if (result.error) {
    return { stdout: "", stderr: result.error.message, exitCode: -1 };
  }
  return { stdout: result.stdout || "", stderr: result.stderr || "", exitCode: result.status ?? 0 };
}

export function rgGrep(repoRoot, params) {
  let abs;
  try { abs = resolveUnderRoot(repoRoot, params.path || "."); } catch (e) {
    return { lines: [], error: `[PATH ERROR] ${e.message}` };
  }
  if (!fs.existsSync(abs)) return { lines: [] };
  const target = abs === repoRoot ? "." : toRepoRelative(repoRoot, abs);
  const args = [
    "--no-config", "--no-heading", "--with-filename", "--line-number",
    "--color=never", "--trim", "--max-columns=400",
    "-C", "1",
    ...(params.case_sensitive === true ? [] : ["--ignore-case"]),
    ...(params.glob ? ["--glob", params.glob] : []),
    ...BUILTIN_EXCLUDES.flatMap((e) => ["-g", `!${e}`]),
    params.pattern,
    target || ".",
  ];
  const res = runRg(args, repoRoot);
  if (res.exitCode === -1) return { lines: [], error: `[RIPGREP NOT AVAILABLE] rg failed: ${res.stderr}` };
  if (res.exitCode !== 0 && res.exitCode !== 1) return { lines: [], error: `[RIPGREP ERROR] exit ${res.exitCode}: ${res.stderr}` };
  let lines = res.stdout.trim().split(/\r?\n/).filter(Boolean);
  // Cap raw matches BEFORE hydrating so the appended AST context survives.
  if (lines.length > AGENT_CONFIG.MAX_OUTPUT_LINES) {
    lines = [...lines.slice(0, AGENT_CONFIG.MAX_OUTPUT_LINES), `... (truncated at ${AGENT_CONFIG.MAX_OUTPUT_LINES} of ${lines.length} lines)`];
  }
  lines = enrichGrepLines(repoRoot, lines);
  return { lines };
}

function rgRead(repoRoot, params) {
  let abs;
  try { abs = resolveUnderRoot(repoRoot, params.path); } catch (e) {
    return { lines: [], error: `[PATH ERROR] ${e.message}` };
  }
  let stat;
  try { stat = fs.statSync(abs); } catch { return { lines: [], error: `[FILE NOT FOUND] "${params.path}" not found` }; }
  if (!stat.isFile()) return { lines: [], error: `[FILE NOT FOUND] "${params.path}" is not a file` };
  let raw;
  try { raw = fs.readFileSync(abs, "utf8"); } catch (e) { return { lines: [], error: `[READ ERROR] ${e.message}` }; }
  const all = raw.split(/\r?\n/);
  const total = all.length;
  let s = 1, e = total;
  if (typeof params.start === "number" && params.start > 0) s = params.start;
  if (typeof params.end === "number" && params.end >= s) e = Math.min(params.end, total);
  // Strip non-load-bearing lines but keep real line numbers, so finish ranges
  // still map to disk. Feed the hider from line 1 to track block-comment state.
  const hide = stripCommentsEnabled() ? makeLineHider(abs) : null;
  const out = [];
  for (let i = 1; i <= e; i++) {
    const hidden = hide ? hide(all[i - 1] ?? "") : false;
    if (i < s || hidden) continue;
    out.push(`${i}|${all[i - 1] ?? ""}`);
  }
  if (out.length > AGENT_CONFIG.MAX_READ_LINES) {
    out.splice(AGENT_CONFIG.MAX_READ_LINES, out.length - AGENT_CONFIG.MAX_READ_LINES,
      `... (truncated at ${AGENT_CONFIG.MAX_READ_LINES} of ${out.length} lines)`);
  }
  return { lines: out };
}

function rgListDirectory(repoRoot, params) {
  let abs;
  try { abs = resolveUnderRoot(repoRoot, params.path || "."); } catch { return []; }
  let stat;
  try { stat = fs.statSync(abs); } catch { return []; }
  if (!stat.isDirectory()) return [];
  const maxResults = params.maxResults ?? AGENT_CONFIG.MAX_LIST_RESULTS;
  const maxDepth = params.maxDepth ?? AGENT_CONFIG.MAX_LIST_DEPTH;
  const results = [];
  const startTime = Date.now();
  function walk(dir, depth) {
    if (Date.now() - startTime > AGENT_CONFIG.LIST_TIMEOUT_MS) return;
    if (depth > maxDepth || results.length >= maxResults) return;
    let entries;
    try { entries = fs.readdirSync(dir, { withFileTypes: true }); } catch { return; }
    for (const entry of entries) {
      if (results.length >= maxResults) break;
      if (shouldSkip(entry.name)) continue;
      const full = path.join(dir, entry.name);
      const isDir = entry.isDirectory();
      results.push({ name: entry.name, path: toRepoRelative(repoRoot, full), type: isDir ? "dir" : "file", depth });
      if (isDir) walk(full, depth + 1);
    }
  }
  walk(abs, 0);
  return results;
}

function rgGlob(repoRoot, params) {
  let abs;
  try { abs = params.path ? resolveUnderRoot(repoRoot, params.path) : repoRoot; } catch (e) {
    return { files: [], searchDir: repoRoot, totalFound: 0, error: `[PATH ERROR] ${e.message}` };
  }
  const target = abs === repoRoot ? "." : toRepoRelative(repoRoot, abs);
  const args = [
    "--no-config", "--files", "--color=never",
    "-g", params.pattern,
    ...BUILTIN_EXCLUDES.flatMap((e) => ["-g", `!${e}`]),
    target || ".",
  ];
  const res = runRg(args, repoRoot);
  if (res.exitCode === -1) return { files: [], searchDir: abs, totalFound: 0, error: "[RIPGREP NOT AVAILABLE]" };
  if (res.exitCode !== 0 && res.exitCode !== 1) return { files: [], searchDir: abs, totalFound: 0, error: `[GLOB ERROR] exit ${res.exitCode}` };
  const relFiles = res.stdout.trim().split(/\r?\n/).filter(Boolean);
  const withMtime = relFiles.map((f) => {
    const full = path.resolve(repoRoot, f);
    let mtime = 0;
    try { mtime = fs.statSync(full).mtimeMs; } catch {}
    return { file: full, mtime };
  }).sort((a, b) => b.mtime - a.mtime);
  return { files: withMtime.slice(0, 100).map((x) => x.file), searchDir: abs, totalFound: withMtime.length };
}

export function parseLines(s) {
  if (!s || typeof s !== "string") return null;
  const ranges = [];
  for (const part of s.split(",")) {
    const t = part.trim();
    if (!t) continue;
    const m = t.match(/^(\d+)(?:-(\d+))?$/);
    if (!m) continue;
    const a = parseInt(m[1], 10), b = m[2] ? parseInt(m[2], 10) : parseInt(m[1], 10);
    if (Number.isFinite(a) && Number.isFinite(b) && a > 0 && b >= a) ranges.push([a, b]);
  }
  return ranges.length ? ranges : null;
}

export function mergeRanges(ranges) {
  if (!ranges.length) return [];
  const sorted = [...ranges].sort((a, b) => a[0] - b[0]);
  const merged = [];
  let [cs, ce] = sorted[0];
  for (let i = 1; i < sorted.length; i++) {
    const [s, e] = sorted[i];
    if (s <= ce + 1) { ce = Math.max(ce, e); }
    else { merged.push([cs, ce]); cs = s; ce = e; }
  }
  merged.push([cs, ce]);
  return merged;
}

export function parseFinishFiles(filesStr) {
  const files = [];
  for (const line of (filesStr || "").trim().split(/\n/)) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    const searchFrom = /^[A-Za-z]:/.test(trimmed) ? 2 : 0;
    const colonIdx = trimmed.indexOf(":", searchFrom);
    if (colonIdx === -1) {
      files.push({ path: trimmed, lines: "*" });
      continue;
    }
    const filePath = trimmed.slice(0, colonIdx);
    const rangesPart = trimmed.slice(colonIdx + 1);
    if (!rangesPart.trim() || rangesPart.trim() === "*") {
      files.push({ path: filePath, lines: "*" });
      continue;
    }
    const ranges = parseLines(rangesPart);
    files.push({ path: filePath, lines: ranges && ranges.length ? mergeRanges(ranges) : "*" });
  }
  return files;
}

export function fileExistsOnDisk(repoRoot, p) {
  const rel = p.startsWith("/") ? path.relative(repoRoot, p) : p;
  const abs = path.isAbsolute(p) ? p : path.resolve(repoRoot, rel);
  try { return fs.statSync(abs).isFile(); } catch { return false; }
}

export function validateFinishFiles(repoRoot, filesStr) {
  const raw = (filesStr ?? "").trim();
  if (!raw) {
    return { kind: "empty", files: [] };
  }
  const parsed = parseFinishFiles(raw);
  if (!parsed.length) {
    return { kind: "empty", files: [] };
  }
  const valid = parsed.filter((f) => fileExistsOnDisk(repoRoot, f.path));
  if (valid.length > 0) {
    return { kind: "ok", files: valid, invalidPaths: parsed.filter((f) => !valid.some((v) => v.path === f.path)).map((f) => f.path) };
  }
  return { kind: "rejected", files: [], listedPaths: parsed.map((f) => f.path), raw };
}

export function formatFinishRejection(repoRoot, validation) {
  const listed = (validation.listedPaths || []).join(", ") || validation.raw || "(none)";
  return [
    `[FINISH REJECTED] None of the listed paths exist under ${repoRoot}.`,
    `Listed: ${listed}`,
    "Use grep_search, glob, or read to find real repo-relative paths from tool output, then call finish again.",
  ].join("\n");
}

export function readFinishFiles(repoRoot, files) {
  const results = [];
  for (const f of files) {
    const rel = f.path.startsWith("/") ? path.relative(repoRoot, f.path) : f.path;
    const abs = path.isAbsolute(f.path) ? f.path : path.resolve(repoRoot, rel);
    let raw;
    try { raw = fs.readFileSync(abs, "utf8"); } catch { continue; }
    const all = raw.split(/\r?\n/);
    const total = all.length;
    if (f.lines === "*" || !Array.isArray(f.lines)) {
      results.push({ path: rel, startLine: 1, endLine: total, content: all.join("\n") });
    } else {
      const merged = mergeRanges(expandFinishRanges(repoRoot, rel, f.lines));
      for (const [s, e] of merged) {
        const actualS = Math.max(1, Math.min(s, total));
        const actualE = Math.max(actualS, Math.min(e, total));
        results.push({ path: rel, startLine: actualS, endLine: actualE, content: all.slice(actualS - 1, actualE).join("\n") });
      }
    }
  }
  return results;
}

function extractPathFromCommand(cmd) {
  if (!cmd) return ".";
  const m = cmd.match(/(?:ls|find)\s+(-\S+\s+)*([^\s-][^\s]*)/);
  return m ? m[2] : ".";
}

function safeJSON(s) {
  try { return JSON.parse(s); } catch { return {}; }
}

export function executeTool(repoRoot, name, args) {
  switch (name) {
    case "grep_search": {
      if (!args.pattern) return "[GREP ERROR] pattern required";
      const res = rgGrep(repoRoot, { pattern: args.pattern, path: args.path || ".", glob: args.glob, case_sensitive: args.case_sensitive });
      if (res.error) return res.error;
      let out = res.lines.join("\n") || "no matches";
      if (args.limit && typeof args.limit === "number") {
        const lines = out.split("\n");
        if (lines.length > args.limit) out = lines.slice(0, args.limit).join("\n") + `\n... (truncated at ${args.limit} lines)`;
      }
      return out;
    }
    case "read": {
      if (!args.path) return "[READ ERROR] path required";
      const readArgs = { path: args.path };
      if (typeof args.lines === "string") {
        const ranges = parseLines(args.lines);
        if (ranges) { readArgs.start = ranges[0][0]; readArgs.end = ranges[ranges.length - 1][1]; }
      } else if (typeof args.start === "number") {
        readArgs.start = args.start;
        if (typeof args.end === "number") readArgs.end = args.end;
      }
      const res = rgRead(repoRoot, readArgs);
      return res.error || res.lines.join("\n") || "(empty file)";
    }
    case "list_directory": {
      const dirPath = extractPathFromCommand(args.command || ".");
      const entries = rgListDirectory(repoRoot, { path: dirPath });
      if (!entries.length) return "empty";
      return entries.map((e) => path.join(repoRoot, e.path)).join("\n");
    }
    case "glob": {
      if (!args.pattern) return "[GLOB ERROR] pattern required";
      const res = rgGlob(repoRoot, { pattern: args.pattern, path: args.path });
      if (res.error) return res.error;
      if (!res.files.length) return "no matches";
      const header = `Found ${res.totalFound} file(s) matching "${args.pattern}" within ${res.searchDir}, sorted by modification time (newest first):`;
      const body = res.files.join("\n");
      const trunc = res.totalFound > res.files.length ? `\n[${res.totalFound - res.files.length} files truncated]` : "";
      return `${header}\n---\n${body}\n---${trunc}`;
    }
    default:
      return `[UNKNOWN TOOL] ${name}`;
  }
}

function msgSize(m) {
  if (m.role === "tool") return (m.content || "").length;
  if (m.role === "assistant") {
    let s = typeof m.content === "string" ? m.content.length : 0;
    if (m.tool_calls) s += m.tool_calls.reduce((a, tc) => a + tc.function.name.length + tc.function.arguments.length, 0);
    return s;
  }
  return (m.content || "").length;
}

function enforceContextLimit(messages) {
  const total = () => messages.reduce((s, m) => s + msgSize(m), 0);
  if (total() <= AGENT_CONFIG.MAX_CONTEXT_CHARS) return;
  let firstUserSkipped = false;
  for (let i = 0; i < messages.length; i++) {
    if (total() <= AGENT_CONFIG.MAX_CONTEXT_CHARS) break;
    const m = messages[i];
    if (m.role === "tool" && m.content !== TRUNCATED) {
      messages[i] = { role: "tool", tool_call_id: m.tool_call_id, content: TRUNCATED };
    } else if (m.role === "user") {
      if (!firstUserSkipped) { firstUserSkipped = true; continue; }
      if (m.content !== TRUNCATED) messages[i] = { role: "user", content: TRUNCATED };
    }
  }
}

function codeDirHints(repoRoot) {
  const hints = [];
  for (const name of ["hooks", "scripts", "src", "lib", "pkg", "internal"]) {
    try {
      if (fs.statSync(path.join(repoRoot, name)).isDirectory()) hints.push(name + "/");
    } catch {}
  }
  return hints.length ? `\n<code_dirs>${hints.join(" ")}</code_dirs>` : "";
}

function formatSeedHits(seedHits) {
  if (!Array.isArray(seedHits) || !seedHits.length) return "";
  const blocks = seedHits.map((s) => `${s.startLine}:${s.endLine}:${s.path}\n${s.content}`);
  return [
    "\n<seed_hits>",
    "Pre-fetched definitions for code symbols in your query (already confirmed on disk).",
    "Cite these directly in finish when they answer the query; only grep/read for what is missing.",
    "You may call finish on turn 1 if these suffice.",
    ...blocks.map((b) => `---\n${b}`),
    "</seed_hits>",
  ].join("\n");
}

export function buildInitialState(repoRoot, query, { mapText = "", seedHits = [] } = {}) {
  const hasMap = Boolean(mapText);
  // The structure dump is for orientation only. When a map (and/or seeds) are
  // present they carry the orientation signal, so cap the listing hard to keep
  // turn-1 prefill small; without a map, keep a fuller tree so the no-map path
  // is not starved. Override with UNITRACE_SEARCH_STRUCTURE_MAXLINES.
  const maxLines = Math.max(10, parseInt(
    process.env.UNITRACE_SEARCH_STRUCTURE_MAXLINES || (hasMap ? "60" : "200"), 10));
  const entries = rgListDirectory(repoRoot, {
    path: ".",
    maxDepth: hasMap ? 1 : 2,
    maxResults: maxLines * 2,
  });
  // With a map, prefer directories (skeleton) over individual files so the cap
  // spends its budget on structure, not leaf noise.
  const ranked = hasMap
    ? [...entries].sort((a, b) => (a.type === b.type ? 0 : a.type === "dir" ? -1 : 1))
    : entries;
  const kept = ranked.slice(0, maxLines);
  const lines = [repoRoot, ...kept.map((e) => path.join(repoRoot, e.path))];
  if (kept.length < ranked.length) lines.push(`... (${ranked.length - kept.length} more entries omitted; use list_directory/glob)`);
  const turnTag = `You have used 0 turns and have ${AGENT_CONFIG.MAX_TURNS} remaining`;
  const budget = `<context_budget>0% (0K/${Math.floor(AGENT_CONFIG.MAX_CONTEXT_CHARS / 1000)}K chars)</context_budget>`;
  const finishRule = "\n<finish_rule>finish paths must be repo-relative and confirmed by tools in this session.</finish_rule>";
  const mapBlock = mapText ? `\n<repo_map>\n${mapText}\n</repo_map>` : "";
  const seedBlock = formatSeedHits(seedHits);
  return `<repo_structure>\n${lines.join("\n")}\n</repo_structure>${codeDirHints(repoRoot)}${mapBlock}${seedBlock}${finishRule}\n\n<search_string>\n${query}\n</search_string>\n${budget}\n${turnTag}`;
}

function formatTurnMessage(turn) {
  const remaining = AGENT_CONFIG.MAX_TURNS - turn;
  if (remaining <= 0) {
    return "\nFinal turn: call finish now with repo-relative paths from your tool results, or finish with empty files if nothing matched.";
  }
  if (remaining === 1) {
    return `\nYou have used ${turn} turns; 1 turn remains. Call finish on the next response with confirmed paths, or empty files if nothing matched.`;
  }
  return `\nYou have used ${turn} turn${turn === 1 ? "" : "s"} and have ${remaining} remaining`;
}

export const TOOL_SPECS = [
  {
    type: "function",
    function: {
      name: "list_directory",
      description: "Execute ls or find commands to explore directory structure. Max 500 results. Common junk directories are excluded automatically.",
      parameters: {
        type: "object",
        properties: {
          command: { type: "string", description: "Full ls or find command (e.g. ls -la src/, find . -maxdepth 2 -type f -name '*.py')." },
        },
        required: ["command"],
        additionalProperties: false,
      },
    },
  },
  {
    type: "function",
    function: {
      name: "grep_search",
      description: "Search for a regex pattern in file contents. Returns matching lines (with paths and line numbers) AND a hydrated '--- ast context ---' section: the enclosing function/class or a surrounding code window for each hit. The hydrated context is usually enough to cite finish ranges without a separate read. Case-insensitive by default. Respects .gitignore.",
      parameters: {
        type: "object",
        properties: {
          pattern: { type: "string", description: "Regex pattern to search for in file contents." },
          path: { type: "string", description: "File or directory to search in. Defaults to repository root." },
          glob: { type: "string", description: "Glob pattern to filter files (e.g. '*.py', '*.{ts,tsx}')." },
          case_sensitive: { type: "boolean", description: "Set true for case-sensitive search. Default false." },
          limit: { type: "integer", description: "Limit output to first N matching lines." },
        },
        required: ["pattern"],
        additionalProperties: false,
      },
    },
  },
  {
    type: "function",
    function: {
      name: "glob",
      description: "Find files by name/extension using glob patterns. Returns absolute paths sorted by modification time (newest first). Max 100 results.",
      parameters: {
        type: "object",
        properties: {
          pattern: { type: "string", description: "Glob pattern to match files (e.g. '*.py', 'src/**/*.js')." },
          path: { type: "string", description: "Directory to search in. Defaults to repository root." },
        },
        required: ["pattern"],
        additionalProperties: false,
      },
    },
  },
  {
    type: "function",
    function: {
      name: "read",
      description: "Read entire files or specific line ranges. Path may be repo-relative or absolute under the repository root.",
      parameters: {
        type: "object",
        properties: {
          path: { type: "string", description: "File path to read." },
          lines: { type: "string", description: "Optional line range (e.g. '1-50' or '1-20,45-80'). Omit to read entire file." },
        },
        required: ["path"],
        additionalProperties: false,
      },
    },
  },
  {
    type: "function",
    function: {
      name: "finish",
      description: "Submit the final answer: relevant files and line ranges, or empty files if nothing matched.",
      parameters: {
        type: "object",
        properties: {
          files: {
            type: "string",
            description: "One file per line as path:lines (e.g. 'src/auth.py:1-15,25-50\\nsrc/user.py'). Empty string if no relevant code exists.",
          },
        },
        required: ["files"],
        additionalProperties: false,
      },
    },
  },
];

function parseImplicitFinish(content) {
  let candidateStr = content || "";
  try {
    const parsed = JSON.parse(content);
    if (parsed && typeof parsed.files === "string") candidateStr = parsed.files;
  } catch { /* not JSON */ }
  return candidateStr;
}

function appendTurnBudget(messages, turn) {
  const totalChars = messages.reduce((s, m) => s + msgSize(m), 0);
  const usedK = Math.floor(totalChars / 1000);
  const maxK = Math.floor(AGENT_CONFIG.MAX_CONTEXT_CHARS / 1000);
  const pct = Math.floor(totalChars / AGENT_CONFIG.MAX_CONTEXT_CHARS * 100);
  const budget = `<context_budget>${pct}% (${usedK}K/${maxK}K chars)</context_budget>`;
  messages.push({ role: "user", content: formatTurnMessage(turn) + "\n" + budget });
}

const FINISH_ONLY_PROMPT = [
  "Search turns are exhausted.",
  "Call finish NOW with repo-relative path:line ranges copied from your prior tool results,",
  "or call finish with an empty files string if nothing in the repo matched.",
  "Do not call grep_search, read, glob, or list_directory.",
].join(" ");

function normalizeRepoPath(p) {
  return (p || "").replace(/^\.\//, "").replace(/^\/+/, "");
}

const PATH_IN_TOOL_OUTPUT = /(?:^|\n)(?:\.\/)?([A-Za-z0-9_.-]+(?:\/[A-Za-z0-9_.-]+)+\.(?:py|js|mjs|ts|tsx|rs|go|java|rb|sh|md))(?::|-)\d+/g;

export function pathsFromToolHistory(messages, repoRoot) {
  const paths = new Set();
  for (const m of messages) {
    if (m.role === "assistant" && m.tool_calls) {
      for (const tc of m.tool_calls) {
        const args = safeJSON(tc.function?.arguments);
        if (tc.function?.name === "read" && args.path) {
          const rel = normalizeRepoPath(args.path);
          if (fileExistsOnDisk(repoRoot, rel)) paths.add(rel);
        }
        if (tc.function?.name === "grep_search" && args.path && args.path !== ".") {
          const rel = normalizeRepoPath(args.path);
          if (fileExistsOnDisk(repoRoot, rel)) paths.add(rel);
        }
      }
    }
    if (m.role !== "tool" || typeof m.content !== "string") continue;
    for (const match of m.content.matchAll(PATH_IN_TOOL_OUTPUT)) {
      const p = normalizeRepoPath(match[1]);
      if (fileExistsOnDisk(repoRoot, p)) paths.add(p);
    }
  }
  return [...paths].slice(0, 12);
}

function formatEmptyFinishRejection(repoRoot, messages) {
  const hints = pathsFromToolHistory(messages, repoRoot);
  if (!hints.length) return null;
  return [
    "[FINISH REJECTED] Empty finish, but these repo-relative paths were confirmed in prior tool output:",
    ...hints.map((p) => `- ${p}`),
    "Call finish with path:line ranges from those files. Do not return empty files when matches exist.",
  ].join("\n");
}

function finishOnlyPrompt(messages, repoRoot) {
  const hints = pathsFromToolHistory(messages, repoRoot);
  if (!hints.length) return FINISH_ONLY_PROMPT;
  return `${FINISH_ONLY_PROMPT}\nConfirmed paths from your tool output (use these in finish):\n${hints.map((p) => `- ${p}`).join("\n")}`;
}

async function resolveFinishFromResponse(repoRoot, response, messages, debug) {
  const toolCalls = response.tool_calls || [];
  if (toolCalls.length === 0) {
    const candidateStr = parseImplicitFinish(response.content || "");
    const validation = validateFinishFiles(repoRoot, candidateStr);
    if (validation.kind === "ok") {
      debugLog(debug, `implicit finish ok (${validation.files.length} files)`);
      return { files: validation.files, reason: "implicit_finish" };
    }
    if (validation.kind === "empty") {
      debugLog(debug, "defer implicit empty (use finish extension for empty/no-match)");
      return null;
    }
    return null;
  }

  const finishCall = toolCalls.find((tc) => tc.function.name === "finish");
  if (!finishCall) return null;

  const args = safeJSON(finishCall.function.arguments);
  const validation = validateFinishFiles(repoRoot, args.files ?? "");
  debugLog(debug, `finish kind=${validation.kind}`);

  if (validation.kind === "empty") {
    if (pathsFromToolHistory(messages, repoRoot).length > 0) {
      debugLog(debug, "reject finish_empty (confirmed paths in tool history)");
      return null;
    }
    return { files: [], reason: "finish_empty" };
  }
  if (validation.kind === "ok") return { files: validation.files, reason: "finish_ok" };
  return null;
}

function fallbackFinishFromHistory(messages, repoRoot) {
  const hints = pathsFromToolHistory(messages, repoRoot);
  if (!hints.length) return null;
  return {
    files: hints.map((p) => ({ path: p, lines: "*" })),
    reason: "history_fallback",
  };
}

async function runFinishExtension(repoRoot, messages, invokeModel, debug) {
  for (let ext = 1; ext <= AGENT_CONFIG.FINISH_EXTENSION_TURNS; ext++) {
    debugLog(debug, `finish extension ${ext}/${AGENT_CONFIG.FINISH_EXTENSION_TURNS}`);
    messages.push({ role: "user", content: finishOnlyPrompt(messages, repoRoot) });
    enforceContextLimit(messages);

    let response;
    try {
      response = await invokeModel(messages, { finishOnly: true });
    } catch (e) {
      debugLog(debug, `finish extension api error: ${e.message}`);
      return null;
    }

    const toolCalls = response.tool_calls || [];
    messages.push({
      role: "assistant",
      content: response.content,
      ...(toolCalls.length > 0 ? { tool_calls: toolCalls } : {}),
    });

    const resolved = await resolveFinishFromResponse(repoRoot, response, messages, debug);
    if (resolved) return resolved;

    if (toolCalls.length === 0) {
      const emptyReject = formatEmptyFinishRejection(repoRoot, messages);
      if (emptyReject && parseImplicitFinish(response.content || "") === "") {
        messages.push({ role: "user", content: emptyReject });
      }
      debugLog(debug, "finish extension: structured/implicit response not accepted");
      continue;
    }

    for (const tc of toolCalls) {
      if (tc.function.name === "finish") {
        const args = safeJSON(tc.function.arguments);
        const validation = validateFinishFiles(repoRoot, args.files ?? "");
        const emptyReject = validation.kind === "empty" ? formatEmptyFinishRejection(repoRoot, messages) : null;
        messages.push({
          role: "tool",
          tool_call_id: tc.id,
          content: emptyReject
            || (validation.kind === "rejected"
              ? formatFinishRejection(repoRoot, validation)
              : "[FINISH ERROR] finish call was not accepted; retry with repo-relative paths from prior tool output."),
        });
        continue;
      }
      messages.push({
        role: "tool",
        tool_call_id: tc.id,
        content: `[FINISH ONLY] Do not call ${tc.function.name}. Search is complete. Call finish with repo-relative path:line ranges from prior tool results, or empty files if nothing matched.`,
      });
    }
  }
  return null;
}

export async function runSearch(query, options = {}) {
  const repoRoot = path.resolve(options.repoRoot || process.cwd());
  const debug = Boolean(options.debug);
  const callModel = options.callModel;
  if (typeof callModel !== "function") {
    throw new Error("runSearch requires options.callModel");
  }

  const invokeModel = (messages, meta = {}) => {
    if (callModel.length >= 2) return callModel(messages, meta);
    return callModel(messages);
  };

  const messages = [];
  messages.push({ role: "user", content: buildInitialState(repoRoot, query, { mapText: options.mapText || "", seedHits: options.seedHits || [] }) });

  let finishFiles = null;
  let exitReason = "exhausted";

  for (let turn = 1; turn <= AGENT_CONFIG.MAX_TURNS; turn++) {
    enforceContextLimit(messages);
    debugLog(debug, `turn ${turn}/${AGENT_CONFIG.MAX_TURNS} start`);

    let response;
    try {
      response = await invokeModel(messages);
    } catch (e) {
      debugLog(debug, `model call failed on turn ${turn}: ${e.message}`);
      exitReason = "api_error";
      break;
    }

    const toolCalls = response.tool_calls || [];
    messages.push({
      role: "assistant",
      content: response.content,
      ...(toolCalls.length > 0 ? { tool_calls: toolCalls } : {}),
    });

    if (toolCalls.length === 0) {
      const resolved = await resolveFinishFromResponse(repoRoot, response, messages, debug);
      if (resolved) {
        finishFiles = resolved.files;
        exitReason = resolved.reason;
        break;
      }
      const emptyReject = formatEmptyFinishRejection(repoRoot, messages);
      if (emptyReject && parseImplicitFinish(response.content || "") === "") {
        debugLog(debug, `turn ${turn} rejected implicit empty finish`);
        if (turn === AGENT_CONFIG.MAX_TURNS) break;
        messages.push({ role: "user", content: emptyReject });
        continue;
      }
      debugLog(debug, `turn ${turn} no tool calls; content=${(response.content || "").slice(0, 120)}`);
      if (turn === AGENT_CONFIG.MAX_TURNS) break;
      messages.push({
        role: "user",
        content: "You must call tools to search the repository, then call finish with confirmed repo-relative paths. Plain text answers are not accepted.",
      });
      continue;
    }

    const finishCall = toolCalls.find((tc) => tc.function.name === "finish");
    const otherCalls = toolCalls.filter((tc) => tc.function.name !== "finish");

    if (finishCall) {
      const args = safeJSON(finishCall.function.arguments);
      const validation = validateFinishFiles(repoRoot, args.files ?? "");
      debugLog(debug, `turn ${turn} finish kind=${validation.kind}`);

      if (validation.kind === "empty") {
        const emptyReject = formatEmptyFinishRejection(repoRoot, messages);
        if (emptyReject) {
          messages.push({ role: "tool", tool_call_id: finishCall.id, content: emptyReject });
          debugLog(debug, `turn ${turn} finish_empty rejected (confirmed paths exist)`);
          if (turn === AGENT_CONFIG.MAX_TURNS) break;
          appendTurnBudget(messages, turn);
          continue;
        }
        finishFiles = [];
        exitReason = "finish_empty";
        messages.push({ role: "tool", tool_call_id: finishCall.id, content: "finish accepted: no relevant code found." });
        break;
      }
      if (validation.kind === "ok") {
        finishFiles = validation.files;
        exitReason = "finish_ok";
        messages.push({ role: "tool", tool_call_id: finishCall.id, content: `finish accepted: ${validation.files.length} file(s).` });
        break;
      }

      messages.push({
        role: "tool",
        tool_call_id: finishCall.id,
        content: formatFinishRejection(repoRoot, validation),
      });
      debugLog(debug, `turn ${turn} finish rejected: ${(validation.listedPaths || []).join(", ")}`);
    }

    for (const tc of otherCalls) {
      const args = safeJSON(tc.function.arguments);
      const output = executeTool(repoRoot, tc.function.name, args);
      debugLog(debug, `turn ${turn} tool ${tc.function.name}`);
      messages.push({ role: "tool", tool_call_id: tc.id, content: output });
    }

    if (turn === AGENT_CONFIG.MAX_TURNS) break;
    appendTurnBudget(messages, turn);
  }

  if (finishFiles === null) {
    exitReason = "max_turns";
    const emptyReject = formatEmptyFinishRejection(repoRoot, messages);
    if (emptyReject) {
      messages.push({ role: "user", content: emptyReject });
    }
    const extended = await runFinishExtension(repoRoot, messages, invokeModel, debug);
    if (extended) {
      finishFiles = extended.files;
      exitReason = extended.reason;
    } else {
      const fallback = fallbackFinishFromHistory(messages, repoRoot);
      if (fallback) {
        finishFiles = fallback.files;
        exitReason = fallback.reason;
        debugLog(debug, `history fallback finish (${finishFiles.length} files)`);
      }
    }
  }

  debugLog(debug, `exit reason=${exitReason} files=${finishFiles === null ? "null" : finishFiles.length}`);
  return finishFiles;
}

export function printResults(refs, jsonMode = false) {
  if (jsonMode) {
    process.stdout.write(JSON.stringify(refs, null, 2) + "\n");
    return;
  }
  if (!refs.length) {
    process.stdout.write("No relevant results found.\n");
    return;
  }
  process.stdout.write("## Code references\n\n");
  for (const ref of refs) {
    process.stdout.write(`\`\`\`${ref.startLine}:${ref.endLine}:${ref.path}\n${ref.content}\n\`\`\`\n\n`);
  }
  process.stdout.write("## Key files\n\n");
  const seen = new Set();
  for (const ref of refs) {
    if (!seen.has(ref.path)) { process.stdout.write(`- \`${ref.path}\`\n`); seen.add(ref.path); }
  }
  process.stdout.write("\n");
}
