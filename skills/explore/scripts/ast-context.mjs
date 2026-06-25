// ast-context.mjs — AST-aware line-range expansion via ast-grep (pure Node, zero deps).
//
// Prefers ast-grep (single native binary). Auto-installs via brew or npm @ast-grep/cli.
// Env:
//   EXPLORE_AST_CONTEXT=0       disable AST expansion (default: on)
//   EXPLORE_AST_SKIP_INSTALL=1  detect only; do not auto-install

import { spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { makeLineHider } from "./lib/code-line.mjs";

// Strip non-load-bearing lines (comments, headers, imports, blank/brace lines)
// from code windows sent to the model, mirroring trace's stripPreamble. Default
// on; EXPLORE_SEARCH_STRIP_COMMENTS=0 disables.
export function stripCommentsEnabled() {
  return process.env.EXPLORE_SEARCH_STRIP_COMMENTS !== "0";
}

const EXT_LANG = {
  ".py": "python",
  ".js": "javascript",
  ".mjs": "javascript",
  ".cjs": "javascript",
  ".jsx": "javascript",
  ".ts": "typescript",
  ".tsx": "typescript",
  ".go": "go",
  ".rs": "rust",
  ".java": "java",
  ".kt": "kotlin",
  ".rb": "ruby",
  ".sh": "bash",
  ".bash": "bash",
  ".zsh": "bash",
  ".c": "c",
  ".h": "c",
  ".cpp": "cpp",
  ".cc": "cpp",
  ".hpp": "cpp",
  ".cs": "csharp",
  ".swift": "swift",
  ".lua": "lua",
  ".php": "php",
};

export const LANG_PATTERNS = {
  python: [
    "class $NAME $$$: $BODY",
    "def $NAME($$$): $BODY",
    "async def $NAME($$$): $BODY",
  ],
  javascript: [
    "class $NAME $$$ { $$$ }",
    "function $NAME($$$) { $$$ }",
    "export function $NAME($$$) { $$$ }",
    "export async function $NAME($$$) { $$$ }",
    "const $NAME = ($$$) => $$$",
    "export const $NAME = ($$$) => $$$",
  ],
  typescript: [
    "class $NAME $$$ { $$$ }",
    "function $NAME($$$) { $$$ }",
    "export function $NAME($$$) { $$$ }",
    "export async function $NAME($$$) { $$$ }",
    "const $NAME = ($$$) => $$$",
    "interface $NAME $$$ { $$$ }",
    "type $NAME = $$$",
  ],
  go: ["func $NAME($$$) $$$ { $$$ }", "type $NAME $$$ { $$$ }"],
  rust: ["fn $NAME($$$) $$$ { $$$ }", "impl $$$ { $$$ }", "struct $NAME $$$ { $$$ }"],
  java: ["class $NAME $$$ { $$$ }", "$MOD $NAME($$$) { $$$ }"],
  kotlin: ["class $NAME $$$ { $$$ }", "fun $NAME($$$) $$$ { $$$ }"],
  ruby: ["class $NAME $$$; $$$ end", "def $NAME $$$; $$$ end", "module $NAME $$$; $$$ end"],
  bash: ["function $NAME($$$) { $$$ }", "$NAME() { $$$ }"],
  c: ["$TYPE $NAME($$$) { $$$ }"],
  cpp: ["class $NAME $$$ { $$$ }", "$TYPE $NAME($$$) { $$$ }"],
  csharp: ["class $NAME $$$ { $$$ }", "$TYPE $NAME($$$) { $$$ }"],
  swift: ["class $NAME $$$ { $$$ }", "func $NAME($$$) $$$ { $$$ }"],
  lua: ["function $NAME($$$) $$$ end"],
  php: ["function $NAME($$$) { $$$ }", "class $NAME $$$ { $$$ }"],
};

const nodeCache = new Map();

export function astContextEnabled() {
  return process.env.EXPLORE_AST_CONTEXT !== "0";
}

function hasCommand(name) {
  const res = spawnSync("which", [name], { encoding: "utf8" });
  return res.status === 0 && Boolean(res.stdout?.trim());
}

export function detectAstBinary() {
  for (const name of ["ast-grep", "sg"]) {
    if (hasCommand(name)) return name;
  }
  return null;
}

export function ensureAstTool({ install = true } = {}) {
  const found = detectAstBinary();
  if (found) return { ok: true, binary: found, installed: false };
  if (!install || process.env.EXPLORE_AST_SKIP_INSTALL === "1") {
    return { ok: false, binary: null, installed: false, reason: "ast-grep not found" };
  }

  if (process.platform === "darwin" && hasCommand("brew")) {
    spawnSync("brew", ["install", "ast-grep"], { stdio: "inherit" });
  } else if (hasCommand("npm")) {
    spawnSync("npm", ["install", "-g", "@ast-grep/cli"], { stdio: "inherit" });
  }

  const binary = detectAstBinary();
  if (binary) return { ok: true, binary, installed: true };
  return { ok: false, binary: null, installed: false, reason: "auto-install failed (try: brew install ast-grep)" };
}

export function langForPath(filePath) {
  return EXT_LANG[path.extname(filePath).toLowerCase()] || null;
}

function parseJsonStream(stdout) {
  const nodes = [];
  for (const line of stdout.split(/\r?\n/)) {
    if (!line.trim()) continue;
    try {
      const row = JSON.parse(line);
      const start = row?.range?.start?.line;
      const end = row?.range?.end?.line;
      if (typeof start !== "number" || typeof end !== "number") continue;
      nodes.push({ startLine: start + 1, endLine: end + 1 });
    } catch {
      /* skip malformed */
    }
  }
  return nodes;
}

function runAstPatterns(binary, absPath, lang) {
  const patterns = LANG_PATTERNS[lang];
  if (!patterns?.length) return [];
  const nodes = [];
  for (const pattern of patterns) {
    const res = spawnSync(binary, ["run", "--lang", lang, "-p", pattern, absPath, "--json=stream"], {
      encoding: "utf8",
      maxBuffer: 8 * 1024 * 1024,
    });
    if (res.status !== 0 && !res.stdout) continue;
    nodes.push(...parseJsonStream(res.stdout || ""));
  }
  return mergeNodeRanges(nodes);
}

function mergeNodeRanges(nodes) {
  if (!nodes.length) return [];
  const sorted = [...nodes].sort((a, b) => a.startLine - b.startLine || a.endLine - b.endLine);
  const out = [];
  for (const node of sorted) {
    const last = out[out.length - 1];
    if (last && node.startLine <= last.endLine + 1 && node.endLine <= last.endLine) continue;
    out.push(node);
  }
  return out;
}

export function listAstNodes(absPath, { binary = null, lang = null } = {}) {
  const resolvedLang = lang || langForPath(absPath);
  if (!resolvedLang) return [];
  const bin = binary || detectAstBinary();
  if (!bin) return [];

  let mtime = 0;
  try { mtime = fs.statSync(absPath).mtimeMs; } catch { return []; }
  const key = `${absPath}:${mtime}:${resolvedLang}`;
  if (nodeCache.has(key)) return nodeCache.get(key);

  const nodes = runAstPatterns(bin, absPath, resolvedLang);
  nodeCache.set(key, nodes);
  return nodes;
}

export function findEnclosingNode(nodes, line) {
  let best = null;
  for (const node of nodes) {
    if (line < node.startLine || line > node.endLine) continue;
    if (!best) { best = node; continue; }
    const bestSpan = best.endLine - best.startLine;
    const nodeSpan = node.endLine - node.startLine;
    if (nodeSpan < bestSpan || (nodeSpan === bestSpan && node.startLine > best.startLine)) {
      best = node;
    }
  }
  return best;
}

export function expandLineRange(absPath, startLine, endLine, { binary = null } = {}) {
  if (!astContextEnabled()) return { startLine, endLine, expanded: false };
  const lang = langForPath(absPath);
  if (!lang) return { startLine, endLine, expanded: false };

  const bin = binary || detectAstBinary();
  if (!bin) return { startLine, endLine, expanded: false };

  const nodes = listAstNodes(absPath, { binary: bin, lang });
  if (!nodes.length) return { startLine, endLine, expanded: false };

  let s = startLine;
  let e = endLine;
  let expanded = false;
  for (const line of [startLine, endLine, Math.floor((startLine + endLine) / 2)]) {
    const node = findEnclosingNode(nodes, line);
    if (!node) continue;
    if (node.startLine < s || node.endLine > e) expanded = true;
    s = Math.min(s, node.startLine);
    e = Math.max(e, node.endLine);
  }
  return { startLine: s, endLine: e, expanded };
}

export function expandFinishRanges(repoRoot, relPath, ranges) {
  if (!astContextEnabled() || !Array.isArray(ranges)) return ranges;
  const abs = path.isAbsolute(relPath) ? relPath : path.resolve(repoRoot, relPath);
  if (!langForPath(abs)) return ranges;
  ensureAstTool({ install: process.env.EXPLORE_AST_SKIP_INSTALL !== "1" });

  const out = [];
  for (const [s, e] of ranges) {
    const exp = expandLineRange(abs, s, e);
    out.push([exp.startLine, exp.endLine]);
  }
  return out;
}

const RG_MATCH = /^(.+?):(\d+)[:-](.*)$/;

function parseRgMatchLine(line) {
  const m = line.match(RG_MATCH);
  if (!m) return null;
  return { file: m[1], line: parseInt(m[2], 10), text: m[3] };
}

function readLines(absPath, startLine, endLine, { strip = false } = {}) {
  let raw;
  try { raw = fs.readFileSync(absPath, "utf8"); } catch { return ""; }
  const all = raw.split(/\r?\n/);
  const s = Math.max(1, startLine);
  const e = Math.min(endLine, all.length);
  if (!strip) return all.slice(s - 1, e).join("\n");
  // Feed the hider from line 1 so a block comment opened above the window is
  // tracked; emit only survivors within [s, e].
  const hide = makeLineHider(absPath);
  const out = [];
  for (let i = 1; i <= e; i++) {
    const hidden = hide(all[i - 1] ?? "");
    if (i < s || hidden) continue;
    out.push(all[i - 1] ?? "");
  }
  return out.join("\n");
}

const HYDRATE_PAD = Math.max(0, parseInt(process.env.EXPLORE_GREP_HYDRATE_PAD || "8", 10));
const HYDRATE_MAX_SPAN = Math.max(10, parseInt(process.env.EXPLORE_GREP_HYDRATE_MAX_SPAN || "60", 10));

function fileLineCount(absPath) {
  try { return fs.readFileSync(absPath, "utf8").split(/\r?\n/).length; } catch { return 0; }
}

// Hydrate each grep hit with surrounding code so the model can cite ranges
// without a separate read turn: prefer the enclosing AST node, fall back to a
// clamped line window. Unsupported files (no known language) are left to `read`.
export function enrichGrepLines(repoRoot, lines, { maxBlocks = 12 } = {}) {
  if (!astContextEnabled() || !lines?.length) return lines;
  ensureAstTool({ install: process.env.EXPLORE_AST_SKIP_INSTALL !== "1" });
  const binary = detectAstBinary();

  const matches = [];
  for (const line of lines) {
    const hit = parseRgMatchLine(line);
    if (hit) matches.push(hit);
  }
  if (!matches.length) return lines;

  const blocks = [];
  const acceptedByFile = new Map(); // rel -> [[startLine, endLine]] already hydrated
  for (const hit of matches) {
    if (blocks.length >= maxBlocks) break;
    const rel = hit.file.startsWith("/") ? path.relative(repoRoot, hit.file) : hit.file;
    const abs = path.isAbsolute(hit.file) ? hit.file : path.resolve(repoRoot, rel);
    if (!langForPath(abs)) continue;

    const accepted = acceptedByFile.get(rel) || [];
    if (accepted.some(([as, ae]) => hit.line >= as && hit.line <= ae)) continue;

    let s = hit.line;
    let e = hit.line;
    if (binary) {
      const exp = expandLineRange(abs, hit.line, hit.line, { binary });
      s = exp.startLine;
      e = exp.endLine;
    }
    if (s === hit.line && e === hit.line) {
      s = Math.max(1, hit.line - HYDRATE_PAD);
      e = hit.line + HYDRATE_PAD;
    }
    const total = fileLineCount(abs);
    if (total) e = Math.min(e, total);
    if (e - s + 1 > HYDRATE_MAX_SPAN) e = s + HYDRATE_MAX_SPAN - 1;

    const content = readLines(abs, s, e, { strip: stripCommentsEnabled() });
    if (!content.trim()) continue;
    accepted.push([s, e]);
    acceptedByFile.set(rel, accepted);
    blocks.push({ path: rel, startLine: s, endLine: e, content });
  }

  if (!blocks.length) return lines;
  const suffix = [
    "",
    "--- ast context ---",
    ...blocks.flatMap((b) => [
      `[${b.startLine}:${b.endLine}:${b.path}]`,
      b.content,
      "",
    ]),
  ];
  return [...lines, ...suffix];
}

export function enrichFileMatches(repoRoot, fileMatches, { maxBlocks = 12 } = {}) {
  if (!astContextEnabled() || !fileMatches?.length) return fileMatches;
  ensureAstTool({ install: process.env.EXPLORE_AST_SKIP_INSTALL !== "1" });
  const binary = detectAstBinary();
  if (!binary) return fileMatches;

  const astBlocks = [];
  const seen = new Set();
  for (const fm of fileMatches) {
    for (const m of fm.matches) {
      if (astBlocks.length >= maxBlocks) break;
      const rel = fm.file.startsWith("/") ? path.relative(repoRoot, fm.file) : fm.file;
      const abs = path.isAbsolute(fm.file) ? fm.file : path.resolve(repoRoot, rel);
      const key = `${rel}:${m.lineNumber}`;
      if (seen.has(key)) continue;
      seen.add(key);

      const exp = expandLineRange(abs, m.lineNumber, m.lineNumber, { binary });
      if (!exp.expanded) continue;
      const content = readLines(abs, exp.startLine, exp.endLine, { strip: stripCommentsEnabled() });
      if (!content.trim()) continue;
      astBlocks.push({
        file: fm.file,
        lineNumber: exp.startLine,
        content: `[ast ${exp.startLine}:${exp.endLine}]\n${content}`,
      });
    }
  }

  if (!astBlocks.length) return fileMatches;
  const byFile = new Map(fileMatches.map((fm) => [fm.file, { file: fm.file, matches: [...fm.matches] }]));
  for (const block of astBlocks) {
    if (!byFile.has(block.file)) byFile.set(block.file, { file: block.file, matches: [] });
    byFile.get(block.file).matches.push({ lineNumber: block.lineNumber, content: block.content });
  }
  return [...byFile.values()];
}

function main(argv) {
  const cmd = argv[0];
  if (cmd === "--ensure") {
    const res = ensureAstTool({ install: true });
    if (res.ok) {
      process.stdout.write(`ast-grep ready (${res.binary}${res.installed ? ", installed" : ""})\n`);
      process.exit(0);
    }
    process.stderr.write(`${res.reason || "ast-grep unavailable"}\n`);
    process.exit(1);
  }
  if (cmd === "--check") {
    const res = ensureAstTool({ install: false });
    if (res.ok) {
      process.stdout.write(`${res.binary}\n`);
      process.exit(0);
    }
    process.exit(1);
  }
  process.stderr.write("usage: node ast-context.mjs --ensure|--check\n");
  process.exit(2);
}

const __self = fileURLToPath(import.meta.url);
if (process.argv[1] && path.resolve(process.argv[1]) === __self) {
  main(process.argv.slice(2));
}
