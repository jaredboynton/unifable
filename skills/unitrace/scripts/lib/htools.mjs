// Zero-dependency read-only tool executors for the Cursor harness.
// Every path is confined to the workspace root (no escape via .. or symlink),
// matching cursor-agent ask-mode's read-only intent (--exclude-tools shell/edit/delete).
import { readFileSync, readdirSync, lstatSync, realpathSync, statSync } from "node:fs";
import { isAbsolute, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { execFileSync } from "node:child_process";
import { enrichFileMatches } from "../ast-context.mjs";
import { makeLineHider } from "./code-line.mjs";

const MAX_READ_BYTES = 200_000;   // cap returned file content
const MAX_READ_LINES = 500;       // cap lines returned per range read
const MAX_GREP_MATCHES = 300;     // cap returned grep matches
export const MAX_SHELL_BYTES = 120_000;  // cap returned shell output
const SHELL_TIMEOUT_MS = 30_000;
// Read-only command allowlist (non-mutating). Anything else is rejected so the
// model falls back to structured tools — never executed.
// Read-only command allowlist, aligned with unifable's research whitelist
// (scripts/gate/bash_classify.py: cd/ls/glob/rg + head/tail/wc/sort/uniq pipeline
// sinks + read-only git) and broadened with non-mutating tools useful for code
// exploration. `find` is intentionally excluded (per project guidance; use fd/rg).
const READONLY_CMDS = new Set([
  "ls", "cat", "head", "tail", "grep", "rg", "egrep", "fgrep", "fd", "wc",
  "sort", "uniq", "cut", "awk", "tr", "echo", "printf", "pwd", "file", "stat",
  "basename", "dirname", "realpath", "readlink", "tree", "which", "date", "nl",
  "column", "jq", "yq", "du", "df", "sed", "test", "[", "true", "uname", "hostname",
]);
const GIT_READONLY_SUBS = new Set([
  "log", "status", "diff", "show", "blame", "ls-files", "rev-parse", "branch",
  "cat-file", "describe", "shortlog", "tag", "remote", "config", "ls-tree", "reflog", "merge-base",
]);
const SHELL_WRAPPERS = new Set(["command", "nice", "nohup", "time", "stdbuf", "env"]); // NB: sudo NOT allowed
const DANGEROUS_ASSIGN = new Set([
  "PATH", "IFS", "LD_PRELOAD", "LD_LIBRARY_PATH", "DYLD_INSERT_LIBRARIES",
  "DYLD_LIBRARY_PATH", "BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS", "PS4", "GLOBIGNORE", "CDPATH",
]);

// Detect live command/process substitution outside single quotes (single quotes
// are literal in the shell). Mirrors unifable bash_classify._command_substitution_reason.
function hasCommandSubstitution(text) {
  let inSingle = false, inDouble = false;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (inSingle) { if (ch === "'") inSingle = false; continue; }
    if (ch === "\\") { i++; continue; }
    if (ch === "'") { inSingle = true; continue; }
    if (ch === '"') { inDouble = !inDouble; continue; }
    if (ch === "`") return true;
    if (ch === "$" && text[i + 1] === "(") return true;
    if (!inDouble && (ch === "<" || ch === ">") && text[i + 1] === "(") return true;
  }
  return false;
}

function capOut(s) {
  if (!s) return "";
  return s.length > MAX_SHELL_BYTES ? s.slice(0, MAX_SHELL_BYTES) + "\n…[truncated]" : s;
}
const IGNORE_DIRS = new Set([".git", ".hg", ".svn", "node_modules", "dist", "build", ".next", ".turbo", "coverage", ".cache"]);

function safeReal(p) {
  try { return realpathSync(p); } catch { return undefined; }
}

// Resolve a server-supplied path (absolute or workspace-relative) and confine to root.
export function confine(root, p) {
  const rootReal = safeReal(root) || resolve(root);
  const abs = isAbsolute(p) ? p : resolve(rootReal, p);
  const real = safeReal(abs) || abs;
  const rel = relative(rootReal, real);
  if (rel === "" || (!rel.startsWith("..") && !isAbsolute(rel))) return real;
  return null; // outside workspace
}

export function toolRead(root, path) {
  const abs = confine(root, path);
  if (!abs) return { ok: false, reason: `path outside workspace: ${path}` };
  let st;
  try { st = statSync(abs); } catch { return { ok: false, reason: `not found: ${path}` }; }
  if (st.isDirectory()) return { ok: false, reason: `is a directory: ${path}` };
  let buf;
  try { buf = readFileSync(abs); } catch (e) { return { ok: false, reason: `read failed: ${e.message}` }; }
  const truncated = buf.length > MAX_READ_BYTES;
  const content = buf.subarray(0, MAX_READ_BYTES).toString("utf8");
  const totalLines = content.length ? content.split("\n").length : 0;
  return { ok: true, path, content, totalLines, fileSize: st.size, truncated };
}

export function parseLineSpec(s) {
  if (!s || typeof s !== "string") return null;
  const ranges = [];
  for (const part of s.split(",")) {
    const t = part.trim();
    if (!t) continue;
    const m = t.match(/^(\d+)(?:-(\d+))?$/);
    if (!m) continue;
    const a = parseInt(m[1], 10);
    const b = m[2] ? parseInt(m[2], 10) : parseInt(m[1], 10);
    if (Number.isFinite(a) && Number.isFinite(b) && a > 0 && b >= a) ranges.push([a, b]);
  }
  return ranges.length ? ranges : null;
}

function mergeLineRanges(ranges) {
  if (!ranges.length) return [];
  const sorted = [...ranges].sort((a, b) => a[0] - b[0]);
  const merged = [];
  let [cs, ce] = sorted[0];
  for (let i = 1; i < sorted.length; i++) {
    const [s, e] = sorted[i];
    if (s <= ce + 1) ce = Math.max(ce, e);
    else { merged.push([cs, ce]); [cs, ce] = [s, e]; }
  }
  merged.push([cs, ce]);
  return merged;
}

function resolveReadRanges(args = {}) {
  if (args.lines) {
    const parsed = parseLineSpec(String(args.lines));
    if (parsed) return mergeLineRanges(parsed);
  }
  const start = typeof args.start_line === "number" ? args.start_line
    : typeof args.start === "number" ? args.start : null;
  const end = typeof args.end_line === "number" ? args.end_line
    : typeof args.end === "number" ? args.end : null;
  if (start && end && start > 0 && end >= start) return [[start, end]];
  if (start && start > 0 && !end) return [[start, start]];
  return null;
}

export function toolReadRange(root, path, args = {}) {
  const abs = confine(root, path);
  if (!abs) return { ok: false, reason: `path outside workspace: ${path}` };
  let st;
  try { st = statSync(abs); } catch { return { ok: false, reason: `not found: ${path}` }; }
  if (st.isDirectory()) return { ok: false, reason: `is a directory: ${path}` };
  let raw;
  try { raw = readFileSync(abs, "utf8"); } catch (e) { return { ok: false, reason: `read failed: ${e.message}` }; }
  const all = raw.split(/\r?\n/);
  const totalLines = all.length;
  const hide = args.stripPreamble ? makeLineHider(path) : null;
  const ranges = resolveReadRanges(args);
  if (!ranges) {
    const wholeLines = [];
    let firstKept = 0;
    let lastKept = 0;
    for (let i = 1; i <= totalLines; i++) {
      if (wholeLines.length >= MAX_READ_LINES) break;
      const rawLine = all[i - 1] ?? "";
      if (hide && hide(rawLine)) continue;
      wholeLines.push(i + "|" + rawLine);
      if (!firstKept) firstKept = i;
      lastKept = i;
    }
    const truncated = wholeLines.length >= MAX_READ_LINES;
    return {
      ok: true,
      path,
      content: wholeLines.join("\n"),
      start_line: firstKept || 1,
      end_line: lastKept || totalLines,
      total_lines: totalLines,
      file_size: st.size,
      truncated,
    };
  }
  const lines = [];
  let startLine = 0;
  let endLine = 0;
  for (const [s, e] of ranges) {
    const actualS = Math.max(1, Math.min(s, totalLines));
    const actualE = Math.max(actualS, Math.min(e, totalLines));
    // When stripping, feed the hider from line 1 so multi-line block-comment
    // state opened before the window is tracked; emit only lines in [actualS,e].
    const rangeHide = args.stripPreamble ? makeLineHider(path) : null;
    const loopStart = rangeHide ? 1 : actualS;
    for (let i = loopStart; i <= actualE; i++) {
      if (lines.length >= MAX_READ_LINES) break;
      const rawLine = all[i - 1] ?? "";
      const hidden = rangeHide ? rangeHide(rawLine) : false;
      if (i < actualS) continue;
      if (hidden) continue;
      lines.push(i + "|" + rawLine);
      if (!startLine) startLine = i;
      endLine = i;
    }
    if (lines.length >= MAX_READ_LINES) break;
  }
  if (!startLine) {
    startLine = Math.max(1, Math.min(ranges[0][0], totalLines));
    endLine = Math.max(startLine, Math.min(ranges[ranges.length - 1][1], totalLines));
  }
  const truncated = lines.length >= MAX_READ_LINES;
  return {
    ok: true,
    path,
    content: lines.join("\n"),
    start_line: startLine,
    end_line: endLine,
    total_lines: totalLines,
    file_size: st.size,
    truncated,
  };
}

export function toolBatchReadRanges(root, reads) {
  if (!Array.isArray(reads) || reads.length === 0) {
    return { ok: false, reason: "batch_read needs a non-empty reads array" };
  }
  const parts = [];
  for (const entry of reads.slice(0, 60)) {
    const path = String(entry?.path || "");
    if (!path) {
      parts.push("[error: missing path]");
      continue;
    }
    const r = toolReadRange(root, path, entry);
    parts.push(`===== ${path} =====\n` + (r.ok ? r.content + (r.truncated ? "\n…[truncated]" : "") : `[error: ${r.reason}]`));
  }
  return { ok: true, text: parts.join("\n\n"), count: Math.min(reads.length, 60) };
}

// Read many files in one call (batch_read MCP tool). Collapses N serial read
// turns into one. Read-only: each path goes through toolRead (workspace-confined).
export function toolBatchRead(root, paths) {
  if (!Array.isArray(paths) || paths.length === 0) return { ok: false, reason: "batch_read needs a non-empty paths array" };
  const parts = [];
  for (const p of paths.slice(0, 60)) {
    const r = toolRead(root, String(p));
    parts.push(`===== ${p} =====\n` + (r.ok ? r.content + (r.truncated ? "\n…[truncated]" : "") : `[error: ${r.reason}]`));
  }
  return { ok: true, text: parts.join("\n\n"), count: Math.min(paths.length, 60) };
}

export function toolLs(root, path) {
  const target = path && path.trim() ? path : root;
  const abs = confine(root, target);
  if (!abs) return { ok: false, reason: `path outside workspace: ${path}` };
  let entries;
  try { entries = readdirSync(abs).sort((a, b) => a.localeCompare(b)); }
  catch (e) { return { ok: false, reason: `ls failed: ${e.message}` }; }
  const dirs = [];
  const files = [];
  for (const name of entries) {
    let s;
    try { s = lstatSync(join(abs, name)); } catch { continue; }
    if (s.isSymbolicLink()) continue;
    if (s.isDirectory()) { if (!IGNORE_DIRS.has(name)) dirs.push(name); }
    else if (s.isFile()) files.push(name);
  }
  return { ok: true, absPath: abs, dirs, files };
}

export function toolGrep(root, args) {
  const { pattern } = args;
  if (!pattern) return { ok: false, reason: "empty pattern" };
  const searchPath = args.path && args.path.trim() ? confine(root, args.path) : root;
  if (!searchPath) return { ok: false, reason: `path outside workspace: ${args.path}` };

  const rgArgs = ["--line-number", "--no-heading", "--with-filename", "--color", "never", "-S"];
  if (args.caseInsensitive) rgArgs.push("-i");
  if (args.glob) rgArgs.push("-g", args.glob);
  if (args.type) rgArgs.push("-t", args.type);
  rgArgs.push("--max-count", "50");
  rgArgs.push("-e", pattern, ".");

  let out = "";
  try {
    out = execFileSync("rg", rgArgs, { cwd: searchPath, encoding: "utf8", maxBuffer: 16 * 1024 * 1024, stdio: ["ignore", "pipe", "ignore"] });
  } catch (e) {
    // rg exits 1 when no matches — that's a valid empty result, not an error.
    if (e.status === 1) return { ok: true, pattern, path: searchPath, fileMatches: [], clientTruncated: false };
    if (e.code === "ENOENT") return { ok: false, reason: "ripgrep (rg) not available" };
    if (e.stdout) out = e.stdout.toString();
    else return { ok: false, reason: `grep failed: ${e.message}` };
  }

  const byFile = new Map();
  let count = 0;
  let truncated = false;
  for (const line of out.split("\n")) {
    if (!line) continue;
    if (count >= MAX_GREP_MATCHES) { truncated = true; break; }
    // format: relpath:lineno:content
    const m = /^(.*?):(\d+):(.*)$/.exec(line);
    if (!m) continue;
    const file = m[1];
    const lineNumber = Number(m[2]);
    const content = m[3];
    if (!byFile.has(file)) byFile.set(file, []);
    byFile.get(file).push({ lineNumber, content });
    count++;
  }
  const fileMatches = [...byFile.entries()].map(([file, matches]) => ({ file, matches }));
  return { ok: true, pattern, path: searchPath, fileMatches: enrichFileMatches(root, fileMatches), clientTruncated: truncated };
}

// Decide whether a shell command is read-only (non-mutating + no arbitrary-exec
// escape). Returns { allowed, reason?, cmd? }. Uses parsed simple_commands when present.
export function shellIsReadOnly(args) {
  const cmd = (args.command && args.command.trim()) || (args.simpleCommands || []).join(" && ");
  if (!cmd.trim()) return { allowed: false, reason: "empty command" };
  if (hasCommandSubstitution(cmd)) return { allowed: false, reason: "command/process substitution not allowed" };
  if (/>\s*(?!&\d|\/dev\/null\b)\S/.test(cmd)) return { allowed: false, reason: "output redirection not allowed" };
  if (/\b(rm|mv|cp|mkdir|rmdir|touch|chmod|chown|ln|dd|tee|truncate|kill|pkill|shutdown|reboot|mkfifo|install|sudo)\b/.test(cmd))
    return { allowed: false, reason: "mutating/privileged command detected" };
  const segments = (args.simpleCommands && args.simpleCommands.length)
    ? args.simpleCommands
    : cmd.split(/\|\||&&|;|\||\n/).map((s) => s.trim()).filter(Boolean);
  for (const seg of segments) {
    const toks = seg.replace(/^[(\s]+/, "").split(/\s+/).filter(Boolean);
    let i = 0;
    // skip VAR=val prefixes; reject dangerous ones that change command resolution
    while (i < toks.length && /^[A-Za-z_][A-Za-z0-9_]*=/.test(toks[i])) {
      const name = toks[i].split("=")[0];
      if (DANGEROUS_ASSIGN.has(name)) return { allowed: false, reason: `unsafe assignment: ${name}` };
      i++;
    }
    while (i < toks.length && SHELL_WRAPPERS.has(toks[i].split("/").pop())) i++; // skip wrappers (not sudo)
    if (i >= toks.length) continue;
    const base = toks[i].split("/").pop();
    if (base === "git") {
      const sub = toks[i + 1] || "";
      if (!GIT_READONLY_SUBS.has(sub)) return { allowed: false, reason: `git ${sub} not read-only` };
      continue;
    }
    if (base === "sed" && /\s-i\b/.test(seg)) return { allowed: false, reason: "sed -i not allowed" };
    if (!READONLY_CMDS.has(base)) return { allowed: false, reason: `command not allowed: ${base}` };
  }
  return { allowed: true, cmd };
}

export function toolShell(root, args) {
  const verdict = shellIsReadOnly(args);
  if (!verdict.allowed) return { ok: false, rejected: true, reason: verdict.reason, command: args.command || "" };
  const cwd = (args.workingDirectory && confine(root, args.workingDirectory)) || root;
  const cmd = verdict.cmd;
  try {
    const stdout = execFileSync("bash", ["-c", cmd], {
      cwd, encoding: "utf8", timeout: SHELL_TIMEOUT_MS, maxBuffer: 16 * 1024 * 1024,
      stdio: ["ignore", "pipe", "pipe"],
    });
    return { ok: true, command: cmd, workingDirectory: cwd, exitCode: 0, stdout: capOut(stdout), stderr: "" };
  } catch (e) {
    if (e.code === "ETIMEDOUT") return { ok: true, command: cmd, workingDirectory: cwd, exitCode: 124, stdout: capOut(e.stdout?.toString() || ""), stderr: "command timed out" };
    const exitCode = typeof e.status === "number" ? e.status : 1;
    return { ok: true, command: cmd, workingDirectory: cwd, exitCode, stdout: capOut(e.stdout?.toString() || ""), stderr: capOut(e.stderr?.toString() || "") };
  }
}
